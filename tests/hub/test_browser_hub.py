"""His Chrome in claude-hub-browser: the kit (compose override policy, pins, the install paste: names only, the
profile copied never moved), the release's browser tree and the reuse rule, browserloader's verify, the container's
token check (hubgate) on a fake hub, the hub's two /browser routes (pure parts), browsersvc's route, cookie seal and
advertised address, and browser-hub's MCP endpoint, status, link and view on a fake container. Scratch HOME only."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import threading
import time
import tomllib
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "packages" / "hub"))


def _mod(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


BL = _mod("browserloader", REPO / "ops/hub/browserloader.py")
HG = _mod("hubgate", REPO / "adapters/browser/hubgate.py")
HB = _mod("hubbrowser", REPO / "packages/hub/hubbrowser.py")
from importlib.machinery import SourceFileLoader  # noqa: E402

_bh_loader = SourceFileLoader("browser_hub", str(REPO / "scripts/browser-hub"))
_bh_spec = importlib.util.spec_from_loader("browser_hub", _bh_loader)
assert _bh_spec is not None
BH = importlib.util.module_from_spec(_bh_spec)
_bh_loader.exec_module(BH)
import browsersvc as B  # noqa: E402
from hubkit import kit as K  # noqa: E402
from hubkit import release as R  # noqa: E402


@pytest.fixture(autouse=True)
def _scratch_home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("ASSISTANT_TEST", "1")
    for v in ("BROWSER_HUB_CDP", "BROWSERSVC_SEAL", "BROWSERSVC_BIND", "BROWSERSVC_ADVERTISE", "BROWSERSVC_SWITCH"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("BROWSERSVC_SWITCH", str(h / ".config/assistant/browsersvc"))
    monkeypatch.setenv("BROWSER_HUB_STATE", str(h / ".local/state/browser"))  # never the real state


def _yaml(p: Path):
    return pytest.importorskip("yaml").safe_load(p.read_text())


# ------------------------------------------------------------------------------------------------ kit and image


def test_compose_override_policy():
    c = _yaml(REPO / "ops/hub/compose.browser.yaml")
    s = c["services"]["browser"]
    assert set(c["services"]) == {"browser"}  # nothing else of the stack changes
    assert s["container_name"] == "claude-hub-browser" and s["profiles"] == ["browser"] and s["read_only"] is True
    assert s["cap_drop"] == ["ALL"] and s["security_opt"] == ["no-new-privileges:true"] and s["init"] is True
    assert "ports" not in s and "privileged" not in s and "devices" not in s and "network_mode" not in s
    assert not str(s["user"]).startswith("0") and "BROWSER_UID" in str(s["user"])
    assert s["networks"] == ["hub", "egress", "brain"] and s["pull_policy"] == "never"
    vols = {v["target"]: v for v in s["volumes"]}
    assert all(v.get("read_only") for t, v in vols.items() if t not in ("/srv/browser/state", "/srv/browser/profile"))
    assert not any("docker.sock" in str(v["source"]) for v in vols.values())
    assert set(s["secrets"]) == {"token_browser", "browser_ctl_token", "browser_vnc_pass"}


def test_compose_override_against_the_spec():
    js = pytest.importorskip("jsonschema")
    spec = json.loads((REPO / "ops/hub/compose-spec.json").read_text())
    js.validate(_yaml(REPO / "ops/hub/compose.browser.yaml"), spec)


def test_pins_and_dockerfile():
    p = tomllib.loads((REPO / "ops/hub/browser-pins.toml").read_text())
    main = tomllib.loads((REPO / "ops/hub/pins.toml").read_text())["image"]
    assert any(
        i["ref"] == p["base"]["ref"] and i["digest"] == p["base"]["digest"] for i in main
    )  # an already-pinned base
    assert len(p["chrome"]["sha256"]) == 64 and p["chrome"]["version"].endswith("-1")
    d = (REPO / "ops/hub/browser/Dockerfile").read_text()
    assert "sha256sum -c" in d and "USER ${BROWSER_UID}" in d and "google-chrome.list" in d  # pinned, no apt source
    assert "EXPOSE" not in d and "COPY" not in d  # no code baked in: the release runs


def _home(profile: bool = True) -> Path:
    h = Path(os.environ["HOME"])
    if profile:
        (h / ".local/share/browser/profile/Default").mkdir(parents=True, exist_ok=True)
    return h


def test_browser_paste_renders_names_only_and_copies_never_moves():
    _home()
    files, info = K.build_browser(REPO, {"KIT_DIR": "/k", "HUB_ROOT": "/mnt/hub", "BOT_HOME": "/home/bot"})
    assert info["state"] == "rendered", info
    assert info["paste_lines"] <= K.PASTE_MAX and set(files) >= {"browser-install.sh"}
    paste = files["browser-install.sh"].decode()
    assert "@@" not in paste and ".secrets/browser-ctl-token" in paste and ".secrets/novnc.txt" in paste
    assert "[u]ser-data-dir=" in paste  # refuses while the brain's Chrome runs on the profile
    assert 'tar -C "$B/$P"' in paste and " mv " not in paste and "rm -rf" not in paste  # a copy, never a move
    assert 'ls -A "$H/browser/profile"' in paste  # never over a profile the container already uses
    # token_browser is read by the hub (uid 10100, at start) and by the container (the brain's uid): owner + group
    assert 'chown "10100:$G" "$H/secrets/token_browser"; chmod 640 "$H/secrets/token_browser"' in paste
    assert 'install -m 600 $O "$B/' in paste  # the container's own two: the brain's uid, 0600
    blob = b"".join(files[n] for n in ("compose.browser.yaml", "loader/browserloader.py", "browser/live.json",
                                       "browser/build/Dockerfile", "browser/browser.env"))  # fmt: skip
    assert hashlib.sha256(blob).hexdigest()[:16] in paste
    env = files["browser/browser.env"].decode()
    assert "CHROME_SHA256=" in env and "BROWSER_BASE=python:3.11.16-slim-bookworm@sha256:" in env


def test_browser_paste_not_rendered_without_a_profile():
    _home(profile=False)
    assert K.build_browser(REPO, {"KIT_DIR": "/k", "HUB_ROOT": "/h", "BOT_HOME": "/b"})[1]["state"] == "not rendered"


# ------------------------------------------------------------------------------------------------ release + loader


def test_release_browser_tree_and_reuse_rule(tmp_path, monkeypatch):
    src = R.browser_sources(REPO)
    assert set(src) == {"browser/run.py", "browser/app/hubgate.py", "browser/app/browsersvc/__init__.py"}
    assert all(p.is_file() for p in src.values())
    assert "app/hubbrowser.py" in R.FILES and R.BROWSER_IN_RELEASE is True  # in the code, like voice (a69fab4)
    assert "browser" in R.optional(REPO)
    (tmp_path / "rel").mkdir()
    (tmp_path / "rel" / "release.json").write_text(json.dumps({"entry": {"hub": "app/hubd.py"}}))
    assert "browser" in R.missing(REPO, "rel", tmp_path)  # a kept release without it gets the tree added (_amend)
    part = tmp_path / "part"
    (part / "adapters").mkdir(parents=True)
    assert R.browser_sources(part) == {} and "browser" not in R.optional(part)  # a partial tree builds without it
    monkeypatch.setattr(R, "BROWSER_IN_RELEASE", False)
    assert "browser" not in R.optional(REPO)


def _release(root: Path, rid: str = "abc1234", entry: bool = True) -> Path:
    d = root / rid
    (d / "browser").mkdir(parents=True)
    f = d / "browser" / "run.py"
    f.write_text("print('hi')\n")
    meta = {"format": 1, "rid": rid, "entry": {"browser": "browser/run.py"} if entry else {},
            "files": {"browser/run.py": {"sha256": hashlib.sha256(f.read_bytes()).hexdigest()}}}  # fmt: skip
    (d / "release.json").write_text(json.dumps(meta))
    return d


def test_loader_verify(tmp_path):
    d = _release(tmp_path)
    assert BL.verify_release(str(tmp_path), "abc1234") is None
    (d / "browser" / "run.py").write_text("print('changed')\n")
    assert "sha256" in BL.verify_release(str(tmp_path), "abc1234")
    _release(tmp_path, "def5678", entry=False)
    assert "no browser entry" in BL.verify_release(str(tmp_path), "def5678")
    assert BL.verify_release(str(tmp_path), "../x") == "bad id"


def test_loader_runs_the_current_release_and_idles_without_one(tmp_path):
    rel, st = tmp_path / "rel", tmp_path / "st"
    rel.mkdir()
    ld = BL.Loader(str(rel), str(st), str(tmp_path / "cfg.json"))
    ld.step()
    assert json.loads((st / "loader-status.json").read_text())["state"] == "idle"
    _release(rel)
    (rel / "CURRENT").write_text("abc1234 1\n")
    ld.step()
    try:
        assert ld.child is not None and ld.rid == "abc1234"
    finally:
        ld.stop()


# ------------------------------------------------------------------------------------------------ the token check


class FakeHub:
    def __init__(self, row=None, commit=None, raise_on=None):
        self.row, self.commit, self.raise_on, self.calls = row, commit or {"id": 7, "dup": False}, raise_on, []

    def call(self, method, path, body=None):
        self.calls.append((method, path))
        if self.raise_on and path.startswith(self.raise_on):
            raise OSError("down")
        if path.startswith("/browser/approval/"):
            return (200, self.row) if self.row else (404, {"error": "not_found"})
        return 200, self.commit


class FakeTasks:
    def action(self, t):
        return json.dumps({"task": t["id"], "label": "Kaufen"})

    def dry_ok(self, t):
        return False


NONCE = "a" * 32
TASK = {"id": "wa1", "approval": {"nonce": NONCE}, "stops": 1}


def _row(**kw):
    r = {"state": "used", "used_by": "webagent:wa1", "used_ts": time.time() - 5,
         "action_sha": hashlib.sha256(FakeTasks().action(TASK).encode()).hexdigest()}  # fmt: skip
    r.update(kw)
    return r


@pytest.mark.parametrize(
    ("hub", "ok", "why"),
    [
        (FakeHub(_row()), True, "ok"),
        (FakeHub(None), False, "no such approval"),
        (FakeHub(_row(state="approved")), False, "not used for this task"),
        (FakeHub(_row(used_by="webagent:other")), False, "not used for this task"),
        (FakeHub(_row(action_sha="0" * 64)), False, "another action"),
        (FakeHub(_row(used_ts=time.time() - 3600)), False, "too old"),
        (FakeHub(_row(), commit={"id": 3, "dup": True}), False, "used already"),
        (FakeHub(_row(), raise_on="/browser/approval"), False, "unverified"),
        (FakeHub(_row(), raise_on="/browser/commit"), False, "not recorded"),
    ],
)
def test_container_token_check(hub, ok, why):
    got = HG.BrowserTokens(hub).take(dict(TASK), FakeTasks())
    assert got[0] is ok and why in got[1]


def test_hub_routes_pure():
    assert {(m, p) for m, p, _f, who in HB.routes(frozenset({"browser"}))} == {
        ("GET", r"/browser/approval/([0-9a-f]{32})"),
        ("POST", r"/browser/commit"),
    }
    assert all(who == frozenset({"browser"}) for *_x, who in HB.routes(frozenset({"browser"})))
    v = HB.approval_view({"status": "approved", "used_ts": 5.0, "used_by": "webagent:x", "action_sha": "s",
                          "expires": 1.0, "summary": "secret card text"})  # fmt: skip
    assert v["state"] == "used" and "summary" not in v
    ev = HB.commit_event({"task": "wa1", "nonce": NONCE, "stop": 2})
    assert ev["idem_key"] == f"commit:{NONCE}" and ev["kind"] == "webtask.commit" and ev["delivery"] == "mute"
    for bad in ({"task": "wa 1", "nonce": NONCE}, {"task": "wa1", "nonce": "x"}, {"task": "wa1", "nonce": NONCE,
                                                                                    "stop": True}):  # fmt: skip
        with pytest.raises(ValueError):
            HB.commit_event(bad)


# ------------------------------------------------------------------------------------------------ browsersvc


def test_route_names_the_container_for_every_client():
    r = B.route(check=lambda: True)
    assert r == {"mode": "hub", "cdp": "http://claude-hub-browser:9223", "via": "hub", "port": 9223, "ok": True}
    assert B.route(check=lambda: False)["ok"] is False  # down = fail closed, nothing reaches Chrome around the gate
    assert not hasattr(B, "host") and not hasattr(B, "set_host")


def test_cookie_seal():
    ev = {"method": "Network.requestWillBeSentExtraInfo", "params": {"requestId": "1", "associatedCookies": [
        {"cookie": {"name": "s", "value": "v"}}], "headers": {"Cookie": "s=v", "Accept": "*/*"}}}  # fmt: skip
    out = json.loads(B.strip_cookies(json.dumps(ev).encode()))
    assert "associatedCookies" not in out["params"] and out["params"]["headers"] == {"Accept": "*/*"}
    rr = {"method": "Network.responseReceived", "params": {"response": {"headers": {"set-cookie": "a=b", "x": "1"},
          "requestHeaders": {"cookie": "a=b"}, "requestHeadersText": "Cookie: a=b"}}}  # fmt: skip
    out = json.loads(B.strip_cookies(json.dumps(rr).encode()))["params"]["response"]
    assert out["headers"] == {"x": "1"} and out["requestHeaders"] == {} and "requestHeadersText" not in out
    plain = b'{"method":"Network.requestWillBeSent","params":{"request":{"headers":{"Accept":"*/*"}}}}'
    assert B.strip_cookies(plain) == plain


def test_sealed_commands_and_advertised_address(monkeypatch):
    monkeypatch.setenv("BROWSERSVC_SEAL", "1")
    monkeypatch.setenv("BROWSERSVC_ADVERTISE", "claude-hub-browser:9223")
    monkeypatch.setenv("BROWSERSVC_STATE", str(Path(os.environ["HOME"]) / "svc"))
    gate = B.Gate(tasks=None, tokens=None, mode_fn=lambda: "shadow")
    svc = B.Service(gate, listen=9223, up_addr=("127.0.0.1", 9222))
    assert svc.seal
    for m in ("Network.getAllCookies", "Storage.getCookies", "Network.getCookies"):
        err = json.loads(svc.sealed_cmd(json.dumps({"id": 4, "method": m}).encode()) or b"{}")
        assert err["id"] == 4 and "sealed" in err["error"]["message"]  # refused even in shadow mode
    assert svc.sealed_cmd(b'{"id":5,"method":"Network.setCookies"}') is None
    body = b'{"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/x"}'
    assert b"ws://claude-hub-browser:9223/devtools" in svc._rewrite(body)  # noqa: SLF001
    assert B.bind() == "127.0.0.1"  # the brain-side default stays loopback


# ------------------------------------------------------------------------------------------------ browser-hub


def test_mcp_endpoint_switch(tmp_path, monkeypatch, capsys):
    cj = tmp_path / "claude.json"
    cj.write_text(json.dumps({"mcpServers": {"browser": {"args": ["cli.js", "--cdp-endpoint", "http://127.0.0.1:9222",
                                                                   "--caps", "x"]}}, "other": 1}))  # fmt: skip
    os.chmod(cj, 0o600)
    monkeypatch.setenv("CLAUDE_JSON", str(cj))
    assert BH.main(["mcp", "hub"]) == 0
    d = json.loads(cj.read_text())
    assert d["mcpServers"]["browser"]["args"][2] == "http://claude-hub-browser:9223" and d["other"] == 1
    assert (cj.stat().st_mode & 0o777) == 0o600
    assert BH.main(["mcp", "hub"]) == 0 and "(unchanged)" in capsys.readouterr().out
    assert BH.main(["mcp", "local"]) == 2  # hub or status only


class _Ctl(BaseHTTPRequestHandler):
    view = {"on": False, "until": None, "conns": 0}

    def _send(self, body, ctype="application/json", code=200):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        if self.path == "/vnc.html":
            return self._send(b"<html>noVNC</html>", "text/html")
        if self.headers.get("Authorization") != "Bearer t0k":
            return self._send({"error": "unauthorized"}, code=401)
        self._send({"chrome": "up", "version": "Chrome/1", "svc": {"up": True, "mode": "shadow"}, "view": _Ctl.view})

    def do_POST(self):  # noqa: N802
        if self.headers.get("Authorization") != "Bearer t0k":
            return self._send({"error": "unauthorized"}, code=401)
        n = int(self.headers.get("Content-Length") or 0)
        a = json.loads(self.rfile.read(n) or b"{}")
        _Ctl.view = {"on": bool(a.get("on")), "until": time.time() + 60 * a.get("minutes", 0), "conns": 0}
        self._send(_Ctl.view)

    def log_message(self, *a):
        pass


def test_status_link_and_view_on_a_fake_container(tmp_path, monkeypatch, capsys):
    srv = HTTPServer(("127.0.0.1", 0), _Ctl)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    tok = tmp_path / "tok"
    monkeypatch.setenv("BROWSER_HUB_CTL", f"http://127.0.0.1:{srv.server_port}")
    monkeypatch.setenv("BROWSER_HUB_CTL_TOKEN", str(tok))
    monkeypatch.setenv("BROWSER_HUB_STATE", str(tmp_path / "st"))
    monkeypatch.setenv("BROWSER_LINK_HOST", "127.0.0.1")
    monkeypatch.setenv("BROWSER_VIEW_PORT", str(srv.server_port))
    # status probes the CDP service: never the real claude-hub-browser:9223 (it resolves once the container exists)
    monkeypatch.setenv("BROWSER_HUB_CDP", f"http://127.0.0.1:{srv.server_port}")
    try:
        assert BH.main(["status"]) == 3  # no token yet: the control API refuses
        capsys.readouterr()
        tok.write_text("t0k\n")
        assert BH.main(["status", "--json"]) == 0
        st = json.loads(capsys.readouterr().out)
        assert st["container"]["chrome"] == "up" and set(st) == {"route", "container"}
        assert BH.main(["link", "--json"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["verified"] and out["url"].startswith("http://127.0.0.1:") and out["where"] == "claude-hub-browser"
        assert _Ctl.view["on"] and BH.main(["stop"]) == 2
        assert BH.main(["view", "off"]) == 0 and not _Ctl.view["on"]
        assert BH.main(["ensure"]) == 0
        assert BH.main(["host", "hub"]) == 2 and BH.main(["bridge", "on"]) == 2  # unknown commands
    finally:
        srv.shutdown()
