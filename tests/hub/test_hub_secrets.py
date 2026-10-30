"""Hub-held secrets: hubredact (the hub's secret store, redaction, fingerprints), hubd's wiring, the release switch,
`hub secrets` (pull, paste). Synthetic values only; no hub, no network, no real secret."""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HUB = REPO / "packages" / "hub"


def _mod(name: str, tag: str = "h10"):
    spec = importlib.util.spec_from_file_location(f"{name}_{tag}_{secrets.token_hex(4)}", HUB / f"{name}.py")
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


hr = _mod("hubredact")
TEST_CARD = "4111111111111111"  # the public Visa test number  secretscan:allow
PEM_HEAD = "-----BEGIN OPENSSH PRIVATE KEY-----"  # a header only, no key  secretscan:allow


def tok(n: int = 32) -> str:
    return secrets.token_urlsafe(n)[:n]


def _secret_dir(tmp: Path, files: dict[str, str], mode: int = 0o600) -> Path:
    d = tmp / "secrets"
    d.mkdir(mode=0o700)
    for n, t in files.items():
        (d / n).write_text(t)
        os.chmod(d / n, mode)
    return d


# ------------------------------------------------------------------------------------------------ parsing + store


def test_parse_follows_secretscans_rules():
    t, k1, k2, card = tok(40), tok(24), tok(30), TEST_CARD
    got = {n: v for _, n, v in hr.parse("f", t)}
    assert got == {"f": t}  # a dedicated token file: the whole file
    got = {n: v for _, n, v in hr.parse("e", f"TELEGRAM_BOT_TOKEN={k1}")}
    assert got["e:TELEGRAM_BOT_TOKEN"] == k1  # one KEY=VALUE line: the value too
    env = f"# comment\nURL=http://example.invalid\nAPI_KEY={k1}\nPORT=8080\n{k2}\n"
    got = {n: v for _, n, v in hr.parse("x.env", env)}
    assert got == {"x.env:API_KEY": k1, "x.env:line5": k2}
    js = json.dumps({"radarr": {"key": k1, "url": "http://x.invalid"}, "card": {"number": card}, "user": "admin"})
    got = {(kind, n): v for kind, n, v in hr.parse("media.json", js)}
    assert got == {("str", "media.json:radarr.key"): k1, ("digits", "media.json:card.number"): card}
    pem = PEM_HEAD + "\n" + base64.b64encode(os.urandom(48)).decode() + "\n-----END x-----\n"
    assert [n for _, n, _ in hr.parse("id", pem)] == ["id:line2"]
    assert hr.parse("short", "abc") == [] and hr.parse("pin", "1234") == []


def test_store_loads_once_refuses_bad_modes_but_still_redacts(tmp_path):
    good, loose, js = tok(), tok(), json.dumps({"radarr": {"key": tok(28)}})
    d = _secret_dir(tmp_path, {"vault_token": good, "media.json": js})
    (d / "loose").write_text(loose)
    os.chmod(d / "loose", 0o644)
    (d / ".hidden").write_text(tok())
    (d / "x.new").write_text(tok())
    (d / "link").symlink_to(d / "vault_token")
    st = hr.Store.load(str(d))
    assert st.names() == ["loose", "media.json", "vault_token"]
    assert st.refused == {"loose": "mode 0644", "link": "symlink"}
    assert st.value("vault_token") == good
    assert st.field("media.json", "radarr.key") == json.loads(js)["radarr"]["key"]
    with pytest.raises(PermissionError):
        st.value("loose")
    with pytest.raises(KeyError):
        st.value("nope")
    (d / "vault_token").write_text("changed")  # values live in memory: no re-read
    assert st.value("vault_token") == good
    state = json.dumps(st.state())
    assert st.state()["refused"] == ["link", "loose"] and good not in state and loose not in state
    out, names = hr.Redactor(st).text(f"a {loose} b {good}")
    assert loose not in out and good not in out and sorted(names) == ["loose", "vault_token"]


# ------------------------------------------------------------------------------------------------ redaction


def test_every_encoding_is_masked():
    v = tok(40)
    st = hr.Store.from_texts({"t": v, "pw.env": "PASSWORD=" + "p@ss w/rd&x=" + tok(10)})
    pw = st.field("pw.env", "PASSWORD")
    r = hr.Redactor(st)
    cases = [
        f"x {v} y",
        base64.b64encode(v.encode()).decode(),
        base64.urlsafe_b64encode(v.encode()).decode().rstrip("="),
        "Authorization: Basic " + base64.b64encode(f"admin:{v}".encode()).decode(),
        "Basic " + base64.b64encode(f"ab:{v}".encode()).decode(),
        v.encode().hex(),
        v.encode().hex().upper(),
        "https://h.invalid/?p=" + urllib.parse.quote(pw, safe=""),
        "q=" + urllib.parse.quote_plus(pw),
        "q=" + re.sub(r"%[0-9A-F]{2}", lambda m: m.group().lower(), urllib.parse.quote(pw, safe="")),
        json.dumps({"x": pw}),
    ]
    for c in cases:
        out, names = r.text(c)
        assert names, c[:24]
        for form in (v, pw, base64.b64encode(v.encode()).decode().rstrip("="), v.encode().hex()):
            assert form not in out


def test_obj_walks_keys_lists_and_leaves_plain_text_alone():
    v = tok(36)
    r = hr.Redactor(hr.Store.from_texts({"t": v}))
    o, names = r.obj({"ok": True, "text": f"t {v}", "data": {v: [v, 1, None, ("a", v)]}, "n": 3})
    assert names == ["t"] * 4 and v not in json.dumps(o)
    assert o["n"] == 3 and o["data"]["[redacted:t]"][1:3] == [1, None]
    plain = "def f(x):\n    return x + 1  # ordinary text\n" * 200
    assert r.text(plain) == (plain, [])
    assert len(hr.Redactor(hr.Store.from_texts({"short": "abc123"}))) == 0  # under MIN_NEEDLE: nothing to search


def test_longest_needle_wins_and_empty_store_is_identity():
    a = tok(20)
    b = a + tok(20)
    r = hr.Redactor(hr.Store.from_texts({"a": a, "b": b}))
    assert r.text(f"[{b}]") == ("[[redacted:b]]", ["b"])
    assert hr.Redactor(hr.Store()).obj({"x": "y"}) == ({"x": "y"}, [])


# ------------------------------------------------------------------------------------------------ fingerprints


def test_fingerprint_export_carries_no_value(tmp_path):
    v, pw, card = tok(48), "Summer2026x", TEST_CARD
    st = hr.Store.from_texts({"t": v, "u.env": f"UOS_PASS={pw}", "c.json": json.dumps({"number": card})})
    key = hr.fp_key(str(tmp_path))
    assert hr.fp_key(str(tmp_path)) == key and oct(os.stat(tmp_path / hr.FP_KEY_FILE).st_mode & 0o777) == "0o600"
    x = st.fingerprints(key)
    body = json.dumps(x)
    for s in (v, pw, card, v[:12], card[:8]):
        assert s not in body
    items = {it["name"]: it for it in x["items"]}
    assert items["hub:t"]["n"] == len(v) and items["hub:t"]["a"] == hr.anchor(v)
    import hashlib
    import hmac

    assert items["hub:t"]["fp"] == hmac.new(key, v.encode(), hashlib.sha256).hexdigest()
    assert items["hub:c.json:number"]["kind"] == "digits" and "a" not in items["hub:c.json:number"]
    assert "hub:u.env:UOS_PASS" in x["weak"] and "hub:t" not in x["weak"]
    assert x["key"] == key.hex() and x["w"] == hr.ANCHOR_W


# ------------------------------------------------------------------------------------------------ hubd + gateway


def test_hubd_wires_the_store_route_and_health(tmp_path, monkeypatch):
    v = tok(40)
    d = _secret_dir(tmp_path, {"token_brain": "B" * 40, "ha_token": v})
    monkeypatch.setenv("HUB_SECRETS", str(d))
    monkeypatch.setenv("HUB_STATE", str(tmp_path / "state"))
    (tmp_path / "state").mkdir()
    hubd = _mod("hubd", "h10d")
    st = hubd.secrets_state()
    assert st["state"] == "ok" and st["files"] == 2 and st["refused"] == 0
    assert hubd.fingerprints("tg")[0] == 403
    code, x = hubd.fingerprints("brain")
    assert code == 200 and v not in json.dumps(x) and {i["name"] for i in x["items"]} >= {"hub:ha_token"}
    out, names = hubd.redact_fn()({"ok": True, "text": v})
    assert v not in json.dumps(out) and names == ["ha_token"]
    monkeypatch.setattr(hubd, "REDACTOR", None)  # present but broken: fail closed
    with pytest.raises(RuntimeError):
        hubd.redact_fn()({"ok": True})
    monkeypatch.setattr(hubd, "hr", None)  # a release without hubredact: nothing changes
    monkeypatch.setattr(hubd, "STORE", None)
    monkeypatch.setattr(hubd, "REDACT_ERROR", "absent")
    assert hubd.redact_fn() is None and hubd.secrets_state() == {"state": "absent", "why": "absent"}
    assert hubd.fingerprints("brain")[0] == 503


def test_release_switch_ships_hubredact(tmp_path):
    sys.path.insert(0, str(REPO / "packages"))
    try:
        from hubkit import release as R
    finally:
        sys.path.remove(str(REPO / "packages"))
    assert R.H10_IN_RELEASE is True  # activated: the next hub release waits for his ✅ on the digest
    assert all((REPO / s).is_file() for s in R.H10_FILES.values())
    rel = tmp_path / "rel"
    (rel / "app").mkdir(parents=True)
    for n in ("hubverify", "hubsecrets", "hubapprovals"):
        (rel / "app" / f"{n}.py").write_bytes((HUB / f"{n}.py").read_bytes())
    off = R.protected(rel)
    (rel / "app" / "hubredact.py").write_bytes((HUB / "hubredact.py").read_bytes())
    on = R.protected(rel)
    assert "app/hubredact.py" in on["files"] and "app/hubredact.py" not in off["files"]
    assert on["digest"] != off["digest"] and len(on["action_sha"]) == 64


# ------------------------------------------------------------------------------------------------ hub secrets


@pytest.fixture
def hubpkg(hubhome, monkeypatch):
    monkeypatch.syspath_prepend(str(REPO / "packages"))
    monkeypatch.syspath_prepend(str(REPO))
    from hubkit import secrets as S

    return S


def test_list_refuses_telegram_exceptions_and_reserved(hubpkg, tmp_path):
    f = tmp_path / "l"
    for line, ok in [
        ("ha_token .secrets/ha-admin-token ha", True),
        ("media.json .secrets/media.json media", True),
        ("dispatcher_key .ssh/id_ed25519 server", True),
        ("x .secrets/tg-live-bot.env", False),
        ("x .claude/channels/telegram/.env", False),
        ("x .secrets/hub-token", False),
        ("x .secrets/phonegw/server.key", False),
        ("x .secrets/voice-tg.session", False),
        ("x .config/voice-relay.env", False),
        ("x .secrets/retired/ha-token", False),
        ("token_x .secrets/a", False),
        ("pg_password .secrets/a", False),
        ("x /etc/shadow", False),
        ("x .secrets/../etc", False),
    ]:
        f.write_text(line + "\n")
        if ok:
            assert hubpkg.read_list(f)
        else:
            with pytest.raises(hubpkg.HubError):
                hubpkg.read_list(f)
    f.write_text("a .secrets/a\na .secrets/b\n")
    with pytest.raises(hubpkg.HubError):
        hubpkg.read_list(f)
    for line, ok in [
        ("dash_basicauth .secrets/x.txt dash userpass", True),
        ("dash_basicauth .secrets/x.txt - userpass", True),
        ("dash_basicauth .secrets/x.txt dash copy", True),
        ("dash_basicauth .secrets/x.txt dash base64", False),
        ("dash_basicauth .secrets/x.txt dash userpass more", False),
    ]:
        f.write_text(line + "\n")
        if ok:
            assert hubpkg.read_list(f)[0][3] == line.split()[3]
        else:
            with pytest.raises(hubpkg.HubError):
                hubpkg.read_list(f)
    f.write_text("ha_token .secrets/ha-admin-token ha\n")
    assert hubpkg.read_list(f) == [("ha_token", ".secrets/ha-admin-token", "ha", "copy")]


def _paste_run(paste: str, tmp_path: Path):
    """The rendered paste adapted to this user (uid seam, own temp file) with a docker stub: (script, env, log)."""
    stub = tmp_path / "stub"
    stub.mkdir(exist_ok=True)
    (stub / "docker").write_text('#!/bin/sh\necho "docker $*" >> "$STUB_LOG"\n')
    os.chmod(stub / "docker", 0o755)
    uid, gid = str(os.getuid()), str(os.getgid())
    script = paste.replace("-o 10100 -g 10100", f"-o {uid} -g {gid}").replace('"600 10100"', f'"600 {uid}"')
    script = script.replace("chown 10100:10100", f"chown {uid}:{gid}")
    script = script.replace("/tmp/claude-hub-secrets.sh", str(tmp_path / "paste.sh"))  # the paste's own temp file
    log = tmp_path / "log"
    return script, dict(os.environ, PATH=f"{stub}:{os.environ['PATH']}", STUB_LOG=str(log)), log


def test_paste_userpass_row_writes_one_user_password_line(hubpkg, hubhome, tmp_path):
    """A userpass row (dash: the 2-line "user:" / "password:" login file) lands in the hub as ONE "user:password"
    line without a newline (what a Basic slot takes), 0600; CR, quotes, "=" and blanks are handled; a rerun says
    unchanged and restarts nothing; a file without both keys is skipped and the hub's copy stays. A fake file with
    random values only."""
    sec = hubhome / ".secrets"
    sec.mkdir()
    u, p = "u" + tok(10), tok(20) + ":" + tok(6)  # a colon inside the password stays
    (sec / "fake-login.txt").write_bytes(
        f"# fake\r\nuser: \"{u}\"  \r\npassword = '{p}'\r\nuser: {tok(8)}\r\n".encode()  # the first match wins
    )
    v1 = tok(40)
    (sec / "ha-admin-token").write_text(v1)
    lst = tmp_path / "secrets.h10"
    lst.write_text("dash_basicauth .secrets/fake-login.txt dash userpass\nha_token .secrets/ha-admin-token ha\n")
    kit = hubhome / ".local/share/claude-hub/secrets-kit"
    info = hubpkg.render(hubpkg.read_list(lst), REPO, out=kit)
    paste = info["paste"]
    assert "@@" not in paste and info["lines"] <= 25 and u not in paste and p not in paste
    # copy rows render as before ("DEST SRC"), so an existing list keeps its kit sum
    assert (kit / "secrets.h10").read_text() == (
        "dash_basicauth .secrets/fake-login.txt userpass\nha_token .secrets/ha-admin-token\n"
    )
    hub_root = tmp_path / "appdata" / "claude-hub"
    (hub_root / "secrets").mkdir(parents=True)
    (hub_root / "compose.yaml").write_text("x")
    script, env, log = _paste_run(paste, tmp_path)
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0 and "STOPPED" not in r.stderr, r.stderr[-400:]
    assert "dash_basicauth: copied (one userpass line)" in r.stdout and "ha_token: copied" in r.stdout
    got = hub_root / "secrets" / "dash_basicauth"
    assert got.read_bytes() == f"{u}:{p}".encode()
    assert oct(os.stat(got).st_mode & 0o777) == "0o600" and not (hub_root / "secrets" / ".dash_basicauth.new").exists()
    assert (hub_root / "secrets" / "ha_token").read_text() == v1
    for v in (u, p, v1):
        assert v not in r.stdout + r.stderr
    assert "docker restart claude-hub" in log.read_text()
    log.unlink()
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0 and "dash_basicauth: unchanged" in r.stdout and "ha_token: unchanged" in r.stdout
    assert not log.exists() and not (hub_root / "secrets" / ".dash_basicauth.new").exists()
    (sec / "fake-login.txt").write_text(f"password: {tok(12)}\n")  # no user line: skipped, the hub keeps its copy
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0 and "dash_basicauth: no user/password lines" in r.stderr, r.stderr[-400:]
    assert got.read_bytes() == f"{u}:{p}".encode() and not log.exists()


def test_paste_renders_and_copies_by_path(hubpkg, hubhome, tmp_path):
    """The rendered paste, run under bash as this user (uid seam), copies 0600 by path, skips the missing, never
    prints a value, restarts the hub only when something changed."""
    sec = hubhome / ".secrets"
    sec.mkdir()
    v1, v2 = tok(40), tok(40)
    (sec / "ha-admin-token").write_text(v1)
    (sec / "media.json").write_text(json.dumps({"emby": {"key": v2}}))
    lst = tmp_path / "secrets.h10"
    lst.write_text("ha_token .secrets/ha-admin-token ha\nmedia.json .secrets/media.json media\nmissing .secrets/nope\n")
    info = hubpkg.render(hubpkg.read_list(lst), REPO, out=hubhome / ".local/share/claude-hub/secrets-kit")
    paste = info["paste"]
    assert "@@" not in paste and info["lines"] <= 25 and v1 not in paste
    hub_root = tmp_path / "appdata" / "claude-hub"
    (hub_root / "secrets").mkdir(parents=True)
    (hub_root / "compose.yaml").write_text("x")
    (hub_root / "secrets" / "voice").mkdir()  # other kits' entries (dirs, other modes) are not this paste's to check
    (hub_root / "secrets" / "token_browser").write_text("x")
    os.chmod(hub_root / "secrets" / "token_browser", 0o640)
    script, env, _ = _paste_run(paste, tmp_path)
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr[-400:]
    assert "ha_token: copied" in r.stdout and "media.json: copied" in r.stdout and "missing" in r.stderr
    assert (hub_root / "secrets" / "ha_token").read_text() == v1
    assert oct(os.stat(hub_root / "secrets" / "ha_token").st_mode & 0o777) == "0o600"
    assert v1 not in r.stdout + r.stderr and v2 not in r.stdout + r.stderr
    assert "docker restart claude-hub" in (tmp_path / "log").read_text()
    (tmp_path / "log").unlink()
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0 and "ha_token: unchanged" in r.stdout and not (tmp_path / "log").exists()
    (hubhome / ".local/share/claude-hub/secrets-kit/secrets.h10").write_text("ha_token .secrets/media.json\n")
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=60)
    assert "list changed" in r.stderr and "copied" not in r.stdout  # (a paste's exit code is its rm's)


class _Fake(BaseHTTPRequestHandler):
    answer: tuple[int, dict] = (200, {})

    def do_GET(self):  # noqa: N802
        code, body = self.answer
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def test_pull_writes_0600_and_keeps_the_old_file_on_a_bad_answer(hubpkg, hubhome, tmp_path, monkeypatch):
    (tmp_path / "brain-token").write_text("B" * 40)
    good = hr.Store.from_texts({"t": tok(40)}).fingerprints(os.urandom(32))
    srv = HTTPServer(("127.0.0.1", 0), _Fake)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    monkeypatch.setenv("HUB_URL", f"http://127.0.0.1:{srv.server_port}")
    try:
        _Fake.answer = (200, good)
        assert hubpkg.pull(None) == 0
        f = hubpkg.fp_file()
        assert json.loads(f.read_text()) == good and oct(f.stat().st_mode & 0o777) == "0o600"
        _Fake.answer = (503, {"error": "secrets_off"})
        assert hubpkg.pull(None) == 1
        _Fake.answer = (200, {"v": 1, "alg": "hmac-sha256", "key": "zz", "items": []})
        assert hubpkg.pull(None) == 1
        assert json.loads(f.read_text()) == good
    finally:
        srv.shutdown()
