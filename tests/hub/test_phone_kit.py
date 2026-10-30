"""Offline: the phone gateway as claude-hub-phone (kit, loader, hold, events, bindings, principal phone).
Every test runs with a scratch HOME; nothing here touches the live gateway, its config, keys or pairing state."""

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from hubkit import kit as K
from hubkit import release as R

REPO = Path(__file__).resolve().parents[2]
RID = "abcdef123456"


def _mod(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


PL = _mod("phoneloader", REPO / "ops/hub/phoneloader.py")
sys.path.insert(0, str(REPO / "packages" / "hub"))


@pytest.fixture(autouse=True)
def _scratch_home(tmp_path, monkeypatch):
    """Every test here runs with a scratch HOME and the test-mode guard (never the real home's files)."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("ASSISTANT_TEST", "1")
    for v in ("HUB_PHONE_BRIDGE_URL", "HUB_PHONE_LOCAL_TOKEN", "HUB_PHONE_INBOX", "HUB_PHONE_STATE",
              "HUB_PHONE_BIN_DIR", "HUB_PHONE_CLI_TOKEN", "PHONEGW_CONFIG", "PHONEGW_STATE",
              "HUB_BRIDGE_STORE_FILE"):  # fmt: skip
        monkeypatch.delenv(v, raising=False)


def _yaml(p: Path):
    return pytest.importorskip("yaml").safe_load(p.read_text())


def test_compose_override_policy():
    ov = _yaml(REPO / "ops/hub/compose.phone.yaml")
    assert set(ov["services"]) == {"hub", "phone"}
    v = ov["services"]["phone"]
    assert "network_mode" not in v and "privileged" not in v
    assert v["ports"] == ["@@FRONT_PORT@@:@@FRONT_PORT@@"]  # D-x: the front alone (the public route ends on the web VM)
    assert v["command"][:4] == ["python3", "-I", "-B", "/opt/hub-loader/phoneloader.py"]
    assert set(v["secrets"]) == set(ov["secrets"]) == {"token_phone", "phone_voice_link_token"}
    assert v["networks"] == ["hub", "brain"]
    ro = {m["target"]: m.get("read_only") for m in v["volumes"]}
    assert ro["/opt/hub-loader"] and ro["/srv/releases"] and ro["/srv/phone/config"]
    assert not ro["/srv/phone/state"] and not ro["/srv/phone/secrets"]
    base = _yaml(REPO / "ops/hub/compose.yaml")["services"]["phone"]
    assert base["profiles"] == ["phone"] and base["networks"] == ["hub"] and "ports" not in base
    assert base["read_only"] is True and base["cap_drop"] == ["ALL"]
    live = json.loads((REPO / "ops/hub/phone-live.json").read_text())
    assert live["hub"]["token_file"] == "/run/secrets/token_phone" and live["gw"]["chat_plugin"] is True
    assert live["gw"]["voice_internal_url"].startswith("http://claude-hub-voice:")


def _home(tmp_path: Path, gw: bool = True) -> Path:
    h = Path(os.environ["HOME"])
    assert h == tmp_path / "home"  # the scratch home, never the real one
    if gw:
        (h / "opt" / "phonegw").mkdir(parents=True, exist_ok=True)
        (h / ".config" / "phonegw").mkdir(parents=True, exist_ok=True)
        (h / ".config" / "phonegw" / "config.json").write_text(  # the live shape: front_port default, public.port set
            json.dumps({"public": {"enabled": True, "port": 8443}})
        )
    return h


def test_phone_paste_renders_names_only(tmp_path):
    h = _home(tmp_path)
    files, info = K.build_phone(REPO, {"KIT_DIR": "/k", "HUB_ROOT": "/mnt/hub", "BOT_HOME": "/mnt/x/claude-bot/home"})
    assert info["state"] == "rendered" and info["ports"] == [6080]  # public.port is the web VM's listener, not ours
    assert info["paste_lines"] <= K.PASTE_MAX and info["rollback_lines"] <= K.PASTE_MAX
    paste = files["phone-install.sh"].decode()
    assert "@@" not in paste and "'.secrets/phonegw/ca.key'" in paste and "'.secrets/phonegw/public.pem'" in paste
    assert '"$B/.secrets/phone-cli-token" "$H/secrets/phone/internal.token"' in paste  # the CLI's own token
    assert (
        '"$B/.phonegw-off"' in paste
        and "[o]pt/phonegw serve" in paste
        and "python:3.11.16-slim-bookworm@sha256:" in paste
    )
    compose = files["compose.phone.yaml"].decode()
    assert '"6080:6080"' in compose and "8443" not in compose
    assert "/p.py 6080 >" in paste and "8443" not in paste and "8443" not in files["phone-rollback.sh"].decode()
    # a stopped earlier run: its leftover override.yml.new goes, and the gateway state is copied afresh every run
    assert 'rm -f "$N"' in paste and '[ -s "$H/phone/state/gw/phonegw.db" ] ||' not in paste
    assert 'mv "$G" "$G.prev"' in paste and 'cp -a "$B/.local/state/phonegw/." "$G/"' in paste
    blob = (
        files["compose.phone.yaml"]
        + files["loader/phoneloader.py"]
        + files["phone/live.json"]
        + files["phone/ports.py"]
    )
    assert hashlib.sha256(blob).hexdigest()[:16] in paste
    assert "docker-compose.override.yml.p38-off" in files["phone-rollback.sh"].decode()
    (h / ".config" / "phonegw" / "config.json").write_text("{broken")
    assert K.build_phone(REPO, {"KIT_DIR": "/k", "HUB_ROOT": "/h", "BOT_HOME": "/b"})[1]["state"] == "not rendered"


def test_phone_kit_view_target_is_the_hub_browser(tmp_path):
    """The front's 🖥 view goes to claude-hub-browser's noVNC on the hub network."""
    _home(tmp_path)
    files, _info = K.build_phone(REPO, {"KIT_DIR": "/k", "HUB_ROOT": "/h", "BOT_HOME": "/b"})
    gw = json.loads(files["phone/live.json"])["gw"]
    assert (gw["novnc_host"], gw["novnc_port"]) == ("claude-hub-browser", 6080)


def _move(cfg: dict, *ports: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-I", str(REPO / "ops/hub/phone-ports.py"), *ports],
                          input=json.dumps(cfg), capture_output=True, text=True)  # fmt: skip


def _bot(*ports: dict) -> dict:
    return {"services": {"bot": {"ports": list(ports)}}}


FRONT = {"mode": "ingress", "target": 6080, "published": "6080", "protocol": "tcp"}  # `docker compose config` shape


def test_port_mover_bot_publishes_only_the_front():
    """The live claude-bot (23.10): 6080/tcp alone, no host IP. Moving it leaves bot without ports."""
    r = _move(_bot(FRONT), "6080")
    assert r.returncode == 0 and "ports: !reset []" in r.stdout and "6080" not in r.stdout.split("services:")[1]
    # the old two-port call (front + the public route's 8443) is refused before anything changes
    r = _move(_bot(FRONT), "6080", "8443")
    assert r.returncode == 3 and r.stdout == "" and "8443" in r.stderr


def test_port_mover_keeps_the_other_ports():
    other = {"target": 9000, "published": "9000", "host_ip": "127.0.0.1", "protocol": "tcp", "mode": "ingress"}
    r = _move(_bot(FRONT, other), "6080")
    assert r.returncode == 0 and "ports: !override" in r.stdout and '"published": "9000"' in r.stdout
    assert '"host_ip": "127.0.0.1"' in r.stdout and '"6080"' not in r.stdout
    for cfg, argv in ((_bot(other), ("6080",)), (_bot(), ("6080",)), (_bot(FRONT), ()), (_bot(FRONT), ("x",))):
        r = _move(cfg, *argv)  # bot does not publish what is asked (or nothing is asked): the paste stops untouched
        assert r.returncode == 3 and r.stdout == "", (cfg, argv)


def test_release_phone_tree_sources():
    src = R.phone_sources(REPO)
    assert src[R.PHONE_ENTRY] == REPO / "adapters/phone/run.py"
    assert "phone/app/phonegw/server.py" in src and "phone/app/protocol/schema/index.json" in src
    assert src["phone/app/answer_filter.py"] == REPO / "adapters/voice/voice_relay/answer_filter.py"
    assert not any("/tests/" in k or k.endswith(".lock") for k in src)
    assert R.PHONE_IN_RELEASE is True  # in every release (tests/hub/test_phone_release.py)


def test_schema_snapshot_matches_the_app_repo():
    app = Path(os.environ.get("PHONEGW_PROTOCOL_SRC") or Path.home().parent / "x") / "schema"
    real = Path("/home/node/opt/phone-app/claude/protocol/schema")
    app = real if real.is_dir() else app
    if not app.is_dir():
        pytest.skip("no app repo here")
    mine = REPO / "adapters/phone/protocol/schema"
    names = sorted(p.name for p in app.glob("*.json"))
    assert names == sorted(p.name for p in mine.glob("*.json"))
    assert all((app / n).read_bytes() == (mine / n).read_bytes() for n in names), "refresh adapters/phone/protocol"


def _release(root: Path, body: str = "import time\ntime.sleep(60)\n") -> Path:
    d = root / RID
    (d / "phone").mkdir(parents=True)
    (d / "phone" / "run.py").write_text(body)
    files = {"phone/run.py": {"sha256": hashlib.sha256(body.encode()).hexdigest()}}
    (d / "release.json").write_text(
        json.dumps({"format": 1, "rid": RID, "entry": {"phone": "phone/run.py"}, "files": files})
    )
    (root / "CURRENT").write_text(f"{RID} 1\n")
    return d


def test_loader_verify_env_and_config_seed(tmp_path):
    d = _release(tmp_path / "rel")
    assert PL.verify_release(str(tmp_path / "rel"), RID) is None
    cfgd = tmp_path / "cfg"
    cfgd.mkdir()
    live = {
        "hub": {"url": "http://h:1", "gw_url": "http://h:2", "token_file": "/t"},
        "env": {"PHONEGW_X": True, "OTHER": "1"},
        "gw": {"internal_bind": "0.0.0.0", "public": {"enabled": True}},
    }
    (cfgd / "live.json").write_text(json.dumps(live))
    lo = PL.Loader(str(tmp_path / "rel"), str(tmp_path / "st"), str(cfgd / "live.json"), poll=0.1, healthy=0.0)
    env = lo.env(RID)
    assert env["PHONEGW_X"] == "true" and "OTHER" not in env and env["PHONEGW_HUB_MODE"] == "1"
    assert env["PHONEGW_STATE"] == str(tmp_path / "st" / "gw") and env["PHONEGW_SECRETS"] == "/srv/phone/secrets"
    assert env["PHONEGW_PROTOCOL"] == str((tmp_path / "rel").resolve() / RID / "phone/app/protocol")
    dest = tmp_path / "st" / "gw" / "config.json"
    dest.parent.mkdir(parents=True)
    assert "seed" in lo.seed_config(str(dest))  # nothing to seed from: the gateway does not start
    (cfgd / "gw.json").write_text(json.dumps({"front_port": 6080, "public": {"port": 8443, "host": "x"}, "trip": "on"}))
    assert lo.seed_config(str(dest)) == ""
    got = json.loads(dest.read_text())
    assert got["internal_bind"] == "0.0.0.0" and got["public"] == {"port": 8443, "host": "x", "enabled": True}
    got["trip"] = "off"  # the gateway's own later write is kept; the overlay is laid on again
    dest.write_text(json.dumps(got))
    assert lo.seed_config(str(dest)) == "" and json.loads(dest.read_text())["trip"] == "off"
    assert oct(dest.stat().st_mode & 0o777) == "0o600"
    (d / "phone" / "run.py").write_text("tampered")
    assert "sha256" in PL.verify_release(str(tmp_path / "rel"), RID)


class _Hold(BaseHTTPRequestHandler):
    held = False
    seen: list = []

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        type(self).seen.append(body.get("state"))
        out = json.dumps({"held": type(self).held, "why": "t"}).encode()
        self.send_response(200)
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


def test_loader_hold_stops_and_keeps_the_gateway_down(tmp_path):
    _release(tmp_path / "rel")
    srv = HTTPServer(("127.0.0.1", 0), _Hold)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    tok = tmp_path / "tok"
    tok.write_text("t" * 40)
    cfg = tmp_path / "cfg" / "live.json"
    cfg.parent.mkdir()
    cfg.write_text(json.dumps({"hub": {"url": f"http://127.0.0.1:{srv.server_port}", "token_file": str(tok)}}))
    (cfg.parent / "gw.json").write_text("{}")
    lo = PL.Loader(str(tmp_path / "rel"), str(tmp_path / "st"), str(cfg), poll=0.1, healthy=0.0)
    try:
        lo.step()
        assert lo.child is not None and lo.child.poll() is None
        assert _Hold.seen[-1] in ("starting", "running", "idle", "down")
        _Hold.held = True
        lo.step()
        assert lo.child is None and lo.status["state"] == "held" and _Hold.seen[-1] == "held"
        srv.shutdown()  # the hub goes away: a new loader keeps the hold it was given
        lo2 = PL.Loader(str(tmp_path / "rel"), str(tmp_path / "st"), str(cfg), poll=0.1, healthy=0.0)
        lo2.step()
        assert lo2.child is None and lo2.status["state"] == "held"
    finally:
        _Hold.held = False
        lo.stop_child()
        srv.server_close()


def test_hub_phone_hold_api(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_STATE", str(tmp_path))
    hp = pytest.importorskip("hubphone")
    assert hp.hold_api("POST", "/phone/hold", "tg", "", b"{}")[0] == 403
    assert hp.hold_api("POST", "/phone/hold", "phone", "", b'{"held": true}')[0] == 403
    assert hp.hold_api("POST", "/phone/hold/seen", "voice", "", b'{"state": "held"}')[0] == 403
    st, d = hp.hold_api("POST", "/phone/hold", "brain", "", b'{"held": true, "why": "x"}')
    assert st == 200 and d["held"] and d["changed"]
    st, d = hp.hold_api("POST", "/phone/hold/seen", "phone", "", b'{"state": "held", "release": "abcdef1"}')
    assert st == 200 and d["held"]
    assert hp.hold_api("GET", "/phone/hold", "brain", "", b"")[1]["loader"]["state"] == "held"
    (tmp_path / "phone-hold.json").write_text("{broken")
    assert hp.hold()["held"] is True  # fails closed
    assert {r[1] for r in hp.routes(frozenset({"brain"}), frozenset({"phone"}))} == {
        "/phone/event",
        "/phone/poll",
        "/phone/ack",
    }
    assert "phone.tap" in hp.KINDS and "phone.chat" in hp.KINDS and "phone.zone" in hp.KINDS


def test_hub_phone_start_refused_while_the_gateway_may_run(monkeypatch, capsys):
    from hubkit import phone as HP

    monkeypatch.setattr(HP, "gateway_running", lambda: False)
    a = type("A", (), {"force": False, "why": "w", "wait": 0.0})()
    assert HP.start(a) == 2 and "refused" in capsys.readouterr().out  # no ~/.phonegw-off


def test_tap_bindings_match_the_gateway_bridge():
    """The brain's bindings run what the gateway's bridge ran in claude-bot (same argv, same stdin)."""
    from hubkit import phone as HP

    sys.path.insert(0, str(REPO / "adapters" / "phone"))
    try:
        from phonegw import bridge
    finally:
        sys.path.pop(0)
    acts = [
        {"push_id": 3, "nonce": "ab12", "tag": "t", "level": "urgent", "verb": "dismissed", "arg": ""},
        {"push_id": 4, "nonce": "cd34", "tag": "remind", "level": "timed", "verb": "done", "arg": "r:1a2b3c4d"},
        {"push_id": 5, "nonce": "ef56", "tag": "x", "level": "info", "verb": "call", "arg": ""},
        {"push_id": 6, "nonce": "0a0b", "tag": "tid", "level": "info", "verb": "crate", "arg": "tid-abc_1"},
        {"push_id": 7, "nonce": "0c0d", "tag": "x", "level": "info", "verb": "open", "arg": ""},
    ]
    for a in acts:
        mine = [(argv, data) for argv, data in HP.tap_plan(a)]
        theirs = [([os.path.basename(argv[0]), *argv[1:]], data) for argv, data in bridge.plan(a)]
        assert mine == theirs, a
    assert set(HP.EFFECTS) == set(bridge.EFFECT_TOOLS)
    for name in HP.EFFECTS:
        assert [os.path.basename(bridge.EFFECT_TOOLS[name](["x"])[0])] + bridge.EFFECT_TOOLS[name](["x"])[
            1:3
        ] == HP.EFFECTS[name](["x"])[:3]


def test_inbox_runs_bindings_delivers_chat_else_a_note(tmp_path, monkeypatch):
    from hubkit import phone as HP

    bins = tmp_path / "bin"
    bins.mkdir()
    log = tmp_path / "ran.log"
    for n in ("remind", "push", "talk-button", "phone-routine"):
        (bins / n).write_text(f'#!/bin/sh\necho {n} "$@" >> {log}\ncat >/dev/null\n')
        (bins / n).chmod(0o755)
    monkeypatch.setenv("HUB_PHONE_BIN_DIR", str(bins))
    monkeypatch.setenv("HUB_PHONE_INBOX", str(tmp_path / "inbox"))
    monkeypatch.setenv("HUB_PHONE_STATE", str(tmp_path / "st"))
    (tmp_path / "st").mkdir()
    tap = {"kind": "phone.tap", "payload": {"action": {"level": "timed", "verb": "snooze", "arg": "r:1a2b3c4d"}}}
    assert HP.deliver(tap) == "ran" and log.read_text() == "remind act snooze r:1a2b3c4d\n"
    assert HP.deliver({"kind": "phone.effect", "payload": {"name": "routine_leaving", "args": []}}) == "ran"
    assert HP.deliver({"kind": "phone.effect", "payload": {"name": "rm -rf", "args": []}}) == "dropped"
    assert HP.deliver({"kind": "phone.tap", "payload": {"action": {"verb": "crate", "arg": "tid-;x"}}}) == "dropped"
    got = []

    class Bridge(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            got.append(
                (self.headers.get("Authorization"), json.loads(self.rfile.read(int(self.headers["content-length"]))))
            )
            self.send_response(202)
            self.send_header("content-length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Bridge)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    tok = tmp_path / "lt"
    tok.write_text("local-token-x\n")
    monkeypatch.setenv("HUB_PHONE_BRIDGE_URL", f"http://127.0.0.1:{srv.server_port}/voice")
    monkeypatch.setenv("HUB_PHONE_LOCAL_TOKEN", str(tok))
    note = {"name": "phone-chat-1.md", "text": "# Phone chat"}
    chat = {"kind": "phone.chat", "payload": {"task": "hi", "msg_id": "m1", "device_id": "d1", "note": note}}
    assert HP.deliver(chat) == "channel"
    assert got[0][0] == "Bearer local-token-x" and got[0][1]["source"] == "app" and got[0][1]["msg_id"] == "m1"
    srv.shutdown()
    srv.server_close()
    assert HP.deliver(chat) == "note"
    text = (tmp_path / "inbox" / "phone-chat-1.md").read_text()
    assert text.startswith("# Phone chat") and HP.DNI_PHONE in text  # core.notes.write_relayed: the note contract
    assert HP.deliver({"kind": "phone.zone", "payload": {"zone": "home", "prev": "away", "at": 1}}) == "kept"
    assert json.loads((tmp_path / "st" / "zone.json").read_text())["zone"] == "home"


def test_app_chat_in_hub_mode_is_the_bridges_hub_row_without_a_local_token(tmp_path, monkeypatch, capsys):
    """The bridge reads the hub: his app chat becomes the bridge.handoff row its inbox would write (voiceinbox.ts
    appEvent + bridge.ts handoff) through POST /tg/bridge-event; no ~/.secrets/voice-local-token needed. A bridge on
    its own store without that file: one stderr line, then the note."""
    from hubkit import handoff as HO
    from hubkit import phone as HP

    from core import hub

    sent = []
    monkeypatch.setattr(hub, "call", lambda m, p, b=None, timeout=5.0: sent.append((m, p, b)) or {"id": 9})
    monkeypatch.setattr(HO, "_ts", lambda: "2026-10-24T08:00:00.005Z")
    store = tmp_path / "telegram-store"
    store.write_text("hub\n")
    monkeypatch.setenv("HUB_BRIDGE_STORE_FILE", str(store))
    monkeypatch.setenv("HUB_PHONE_LOCAL_TOKEN", str(tmp_path / "missing"))
    monkeypatch.setenv("HUB_PHONE_INBOX", str(tmp_path / "inbox"))
    note = {"name": "phone-chat-2.md", "text": "# Phone chat"}
    mid = "m_01HZX3K9QW8E7R6T5Y4U3I2O1P"
    chat = {"kind": "phone.chat", "payload": {"task": "hi there", "msg_id": mid, "device_id": "dev_1", "note": note}}
    assert HP.deliver(chat) == "channel"
    ((m, path, row),) = sent
    inbox_id = row["payload"]["inbox_id"]
    assert (m, path) == ("POST", "/tg/bridge-event") and re.fullmatch(r"a[0-9a-f]{8}", inbox_id)
    assert {k: v for k, v in row.items() if k != "rid"} == {
        "kind": "bridge.handoff", "subject": "app hand-off", "labels": ["tg", "bridge", "app"], "delivery": "now",
        "idem_key": f"bridge.handoff:app:{inbox_id}", "trace_id": None,
        "payload": {"chat_id": "app", "via": "app", "user": "owner (app)", "ts": "2026-10-24T08:00:00.005Z",
                    "app_msg_id": mid, "inbox_id": inbox_id, "device_id": "dev_1",
                    "text": f"📱 (app) hi there\n(His phone-app chat: treat it as his message. Answer with: phone chat "
                            f"\"…\" --reply-to {mid} — short, phone-sized. Approvals for risky steps still go through "
                            "Telegram.)"},
    }  # fmt: skip
    store.write_text("sqlite\n")  # not the hub store: the bridge's local inbox, which needs the token file
    assert HP.deliver(chat) == "note" and len(sent) == 1
    assert "hub phone inbox: no " in capsys.readouterr().err


def test_hub_phone_token_prep_is_0600_and_kept(tmp_path, capsys):
    from hubkit import phone as HP

    a = type("A", (), {"token_cmd": "prep"})()
    assert HP.token(a) == 0
    f = Path(os.environ["HOME"]) / ".secrets" / "phone-cli-token"
    first = f.read_text()
    assert oct(f.stat().st_mode & 0o777) == "0o600" and len(first.strip()) == 64
    assert HP.token(a) == 0 and f.read_text() == first  # kept
    assert first.strip() not in capsys.readouterr().out  # never printed


def test_principal_phone_reads_only_its_lookups_and_is_always_enforced():
    import tools.domains.phone  # noqa: F401
    import tools.domains.voice  # noqa: F401
    from tools import hubgw, policy
    from tools.core import REGISTRY

    assert hubgw.CALLER_PRINCIPAL["phone"] == "phone" and "phone" in hubgw.ALWAYS_ENFORCE
    assert "phone" not in hubgw.ADAPTER_RUN  # part 2: the phone.* tools stay brain-run (profile, Telegram, usage)
    assert not [t for t in hubgw.RUNNER if t.startswith("phone.")]
    eng = policy.Engine(REGISTRY)
    p = policy.Principal(kind="phone")
    for name, ok in (("phone.lookup", True), ("phone.act", True), ("phone.push", False), ("phone.open", False),
                     ("voice.lookup", False), ("reminders.add", False)):  # fmt: skip
        spec = REGISTRY.get(name)
        if spec is None:
            continue
        assert eng.decide(p, spec, approved=False, tainted=set()).allowed is ok, name


def test_phone_lookup_runs_only_gateway_lookups(tmp_path, monkeypatch):
    from tools.core import ToolError
    from tools.domains import phone as D

    d = tmp_path / "fake"
    d.mkdir()
    (d / "tripclock").write_text("#!/bin/sh\necho Europe/Zurich\n")
    (d / "tripclock").chmod(0o755)
    monkeypatch.setenv("TOOLS_PHONE_LOOKUP_DIR", str(d))
    assert D.lookup(D.LookupIn(argv=["tripclock", "tz"])).out.strip() == "Europe/Zurich"
    (d / "phone-produce").write_text('#!/bin/sh\necho "{\\"kind\\": \\"$1\\"}"\n')
    (d / "phone-produce").chmod(0o755)
    assert json.loads(D.lookup(D.LookupIn(argv=["phone-produce", "board"], timeout_s=120)).out) == {"kind": "board"}
    for bad in (["remind", "add", "x"], ["tripclock"], ["sh", "-c", "id"], ["divelog", "add", "--quick", "x"]):
        with pytest.raises(ToolError):
            D.lookup(D.LookupIn(argv=bad))


def test_phone_act_runs_only_the_gateway_writes(tmp_path, monkeypatch):
    from tools.core import ToolError
    from tools.domains import phone as D

    d = tmp_path / "fake"
    d.mkdir()
    log = tmp_path / "act.log"
    for n, body in (
        ("remind", f'echo "$REMIND_VIA $*" >> {log}'),
        ("health", f"cat >> {log}"),
        ("trip-pack", 'echo "<html>$1</html>" > "$3"'),
        ("trackid", 'wc -c < "$1"'),
        ("phone", 'printf clipdata > "$5"'),  # phone file get ID --out PATH
    ):
        (d / n).write_text(f"#!/bin/sh\n{body}\n")
        (d / n).chmod(0o755)
    monkeypatch.setenv("TOOLS_PHONE_LOOKUP_DIR", str(d))
    monkeypatch.setenv("TOOLS_PHONE_BIN", str(d / "phone"))
    assert D.act(D.ActIn(argv=["remind", "done", "r:1a2b3c4d"])).rc == 0
    assert D.act(D.ActIn(argv=["health", "ingest", "--received", "17"], stdin='{"steps": 1}')).rc == 0
    assert log.read_text() == 'app done r:1a2b3c4d\n{"steps": 1}'
    assert D.act(D.ActIn(argv=["trip-pack", "Trip", "--out", "@out"])).file == "<html>Trip</html>\n"
    targv = ["trackid", "@file", "--json", "--source", "app", "--min-store", "medium"]
    tid = D.act(D.ActIn(argv=targv, file="tid-1/c.m4a"))
    assert tid.out.strip() == "8"  # the clip came from the gateway (`phone file get`)
    for bad in (
        D.ActIn(argv=["remind", "list"]),
        D.ActIn(argv=["health", "ingest", "--received", "x1"]),
        D.ActIn(argv=["trip-pack", "Trip", "--out", "/etc/passwd"]),
        D.ActIn(argv=["trackid", "@file", "--json", "--source", "app", "--min-store", "medium"]),
        D.ActIn(argv=["sh", "-c", "id"]),
    ):
        with pytest.raises(ToolError):
            D.act(bad)


def test_inbox_fetches_share_files_and_folds_usage(tmp_path, monkeypatch):
    from hubkit import phone as HP

    bins = tmp_path / "bin"
    bins.mkdir()
    (bins / "phone").write_text('#!/bin/sh\nprintf "%s" "$3" > "$5"\n')
    (bins / "phone").chmod(0o755)
    monkeypatch.setenv("HUB_PHONE_BIN_DIR", str(bins))
    monkeypatch.setenv("HUB_PHONE_INBOX", str(tmp_path / "inbox"))
    monkeypatch.setenv("HUB_PHONE_BRIDGE_URL", "http://127.0.0.1:9/voice")  # down: the share becomes a note
    share = {"share_id": "sh1", "device_id": "d1", "kind": "file", "intent": "save", "hint": "keep it and say where",
             "at": 1, "files": [{"id": "sh1/01-a.pdf", "name": "01-a.pdf", "mime": "application/pdf", "bytes": 4},
                                {"id": "../etc", "name": "x"}]}  # fmt: skip
    assert HP.deliver({"kind": "phone.share", "payload": share}) == "note"
    got = tmp_path / "inbox" / "files" / "phone-sh1-01-a.pdf"
    assert got.read_text() == "sh1/01-a.pdf"
    note = next((tmp_path / "inbox").glob("phone-share-*.md")).read_text()
    assert "keep it and say where" in note and "phone-sh1-01-a.pdf" in note and HP.DNI_PHONE in note
    lib = tmp_path / "usage.py"
    lib.write_text(
        "import json\nSPOOLED = []\ndef off():\n    return False\n"
        "def record(feature, kind, **kw):\n    return {'feature': feature, 'kind': kind, **kw}\n"
        f"def _spool(rec, src=None):\n    open({str(tmp_path / 'spool.jsonl')!r}, 'a').write(json.dumps(rec) + '\\n')\n"
        "    return True\n"
    )
    monkeypatch.setenv("HUB_PHONE_USAGE_LIB", str(lib))
    usage = {"env_id": "u1", "app": "0.4.9", "dropped": 2, "gw": {"feature": "phonegw.public", "outcome": "connect"},
             "items": [{"feature": "app.board", "kind": "view", "surface": "screen"},
                       {"feature": "app.route.wifi", "kind": "tap"}, {"feature": "x", "kind": "bogus"}]}  # fmt: skip
    assert HP.deliver({"kind": "phone.usage", "payload": usage}) == "kept"
    recs = [json.loads(x) for x in (tmp_path / "spool.jsonl").read_text().splitlines()]
    assert [(r["feature"], r["kind"], r["actor"]) for r in recs] == [
        ("app.board", "invoke", "owner"),
        ("app.route.wifi", "tap", "system"),
        ("phonegw.public", "invoke", "owner"),
        ("app", "error", "system"),
    ]
    assert recs[0]["ext_id"] == "pg:u:u1:0" and recs[0]["attrs"]["view"] is True


def test_phone_cli_defaults_to_the_hub_gateway(tmp_path):
    from importlib.machinery import SourceFileLoader

    home = Path(os.environ["HOME"])
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PHONEGW_", "PHONE_"))}

    def cli():
        loader = SourceFileLoader("phonecli", str(REPO / "adapters/phone/bin/phone"))
        m = importlib.util.module_from_spec(importlib.util.spec_from_loader("phonecli", loader))
        loader.exec_module(m)
        return m.URL, m.TOKEN_FILE, m.LIVE

    old = dict(os.environ)
    try:
        os.environ.clear()
        os.environ.update(env)
        assert cli() == ("http://claude-hub-phone:8796", str(home / ".secrets/phone-cli-token"), True)  # live
        os.environ["PHONEGW_TOKEN_FILE"] = str(tmp_path / "t")
        assert cli()[2] is False  # a test gateway's token: no Telegram, no usage
    finally:
        os.environ.clear()
        os.environ.update(old)
