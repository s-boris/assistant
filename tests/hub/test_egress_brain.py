"""The brain's side of the egress injector: core.egress (local, shadow and hub mode) end to end through the
hub's injector (hubpolicy_egress with the repo's policy) behind a loopback fake hub, and `hub egress` (endpoints,
modes). Synthetic values only; no real hub, no real secret."""

from __future__ import annotations

import argparse
import importlib.util
import json
import secrets
import stat
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HUB = REPO / "packages" / "hub"
HUB_TOKEN = "T" * 40


def _mod(name: str):
    spec = importlib.util.spec_from_file_location(f"{name}_p312b_{secrets.token_hex(4)}", HUB / f"{name}.py")
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


eg = _mod("hubpolicy_egress")
hr = _mod("hubredact")


def tok(n: int = 32) -> str:
    return secrets.token_urlsafe(n)[:n]


def _serve(handler: type[BaseHTTPRequestHandler]) -> tuple[ThreadingHTTPServer, str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


class Far(BaseHTTPRequestHandler):
    seen: list[dict] = []

    def log_message(self, *a):
        pass

    big: bytes = b""

    def do_GET(self):
        Far.seen.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}})
        if self.path.split("?")[0] == "/api/v3/movie" and Far.big:  # a list over the hub's 8 MiB cap
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(Far.big)))
            self.end_headers()
            self.wfile.write(Far.big)
            return
        if self.path.endswith("/missing"):
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"message": "not found"}')
            return
        data = json.dumps({"auth": self.headers.get("Authorization", ""), "key": self.headers.get("X-Api-Key", "")})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data.encode())


class Hub(BaseHTTPRequestHandler):
    inj = None
    seen: list[dict] = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        a = json.loads(body)
        Hub.seen.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": a, "raw": body})
        if self.headers.get("Authorization") != f"Bearer {HUB_TOKEN}":
            code, x = 401, {"error": "unauthorized"}
        elif self.path == "/egress/fetch":
            code, x = Hub.inj.fetch(a)
        elif self.path == "/egress/check":
            code, x = Hub.inj.check(a)
        else:
            code, x = 404, {"error": "not_found"}
        data = json.dumps(x).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """HOME with local key files, a far side, and a fake hub running the real injector on the repo's policy."""
    Far.seen, Hub.seen, Far.big = [], [], b""
    far, far_url = _serve(Far)
    vals = {"ha": tok(40), "radarr": tok(32)}
    home = tmp_path / "home"
    (home / ".secrets").mkdir(parents=True)
    (home / ".secrets" / "ha-admin-token").write_text(vals["ha"] + "\n")
    (home / ".secrets" / "media.json").write_text(json.dumps({"radarr": {"url": far_url, "key": vals["radarr"]}}))
    store = hr.Store.from_texts({
        "ha_token": vals["ha"],
        "media.json": json.dumps({"radarr": {"url": far_url, "key": vals["radarr"]}}),
        "egress.endpoints": f"ha {far_url}\n",
    })  # fmt: skip
    (tmp_path / "state").mkdir()
    Hub.inj = eg.Injector.load(str(HUB / "policy" / "egress.toml"), store, hr.Redactor(store), str(tmp_path / "state"))
    hub, hub_url = _serve(Hub)
    tf = tmp_path / "hub-token"
    tf.write_text(HUB_TOKEN)
    for k, v in {"HOME": str(home), "ASSISTANT_TEST": "1", "HUB_URL": hub_url, "HUB_TOKEN_FILE": str(tf)}.items():
        monkeypatch.setenv(k, v)
    from core import egress as E

    yield E, far_url, vals, home
    far.shutdown()
    hub.shutdown()
    far.server_close()
    hub.server_close()


def test_local_mode_reads_the_local_copy(world):
    E, far, vals, home = world
    assert E.mode("ha") == "local" and E.secret("ha") == vals["ha"] and E.secret("radarr") == vals["radarr"]
    with pytest.raises(E.Missing):
        E.secret("vault")
    with pytest.raises(ValueError):
        E.secret("nope")
    with E.open(far + "/api/states", "ha", headers={"Authorization": f"Bearer {E.secret('ha')}"}) as r:
        assert json.load(r)["auth"] == f"Bearer {vals['ha']}"  # direct: no hub involved
    assert Hub.seen == []
    (home / ".config" / "assistant").mkdir(parents=True)
    (home / ".config" / "assistant" / "egress.json").write_text("{broken")
    assert E.mode("ha") == "local"  # a broken switch = local


def test_hub_mode_holds_no_value_and_gets_a_masked_answer(world):
    E, far, vals, home = world
    E.set_mode("ha", "hub")
    E.set_mode("radarr", "hub")
    assert E.mode("ha") == "hub" and E.mode("sonarr") == "local"
    assert E.secret("ha") == "hub:secret:ha"
    (home / ".secrets" / "ha-admin-token").unlink()  # the local copy is gone: hub mode needs none
    req = urllib.request.Request(far + "/api/states", headers={"Authorization": "Bearer " + E.secret("ha")})
    with E.open(req, "ha") as r:
        got = json.load(r)
        assert r.status == 200 and r.getcode() == 200 and r.getheader("content-type") == "application/json"
    assert got["auth"] == "Bearer [redacted:ha_token]"  # the far side echoed the value: masked on the way back
    assert Far.seen[-1]["headers"]["authorization"] == f"Bearer {vals['ha']}"  # it did get the value
    assert all(vals["ha"].encode() not in s["raw"] for s in Hub.seen)  # the brain never sent it
    with pytest.raises(urllib.error.HTTPError) as ei:
        E.open(far + "/api/v3/movie/missing", "radarr", headers={"X-Api-Key": E.secret("radarr")})
    assert ei.value.code == 404 and b"not found" in ei.value.read()
    with pytest.raises(E.Refused) as ri:
        E.open("http://198.51.100.9:8123/api/states", "ha", headers={"Authorization": "Bearer " + E.secret("ha")})
    assert ri.value.code == 403 and ri.value.error == "credential_endpoint_mismatch"
    assert isinstance(ri.value, urllib.error.URLError)  # the tools' `except URLError` still catches it


def test_shadow_mode_sends_direct_and_asks_the_hub_with_the_placeholder(world):
    E, far, vals, home = world
    E.set_mode("default", "shadow")
    assert E.secret("ha") == vals["ha"]
    with E.open(far + "/api/error_log?x=1", "ha", headers={"Authorization": f"Bearer {E.secret('ha')}"}) as r:
        assert json.load(r)["auth"] == f"Bearer {vals['ha']}"
    checks = [s for s in Hub.seen if s["path"] == "/egress/check"]
    assert len(checks) == 1 and vals["ha"].encode() not in checks[0]["raw"]
    assert ["Authorization", "Bearer hub:secret:ha"] in checks[0]["body"]["headers"]
    st = Hub.inj.status()
    assert st["bindings"]["ha"]["count"].get("check:would_deny") == 1  # /api/error_log is not in the seeded rules


def test_shadow_without_a_hub_token_still_sends(world, monkeypatch, tmp_path):
    E, far, vals, home = world
    E.set_mode("ha", "shadow")
    monkeypatch.setenv("HUB_TOKEN_FILE", str(tmp_path / "no-such-token"))
    with E.open(far + "/api/states", "ha", headers={"Authorization": f"Bearer {E.secret('ha')}"}) as r:
        assert json.load(r)["auth"] == f"Bearer {vals['ha']}"  # the call went out; only the check was skipped
    assert [s for s in Hub.seen if s["path"] == "/egress/check"] == []
    E.set_mode("ha", "hub")
    with pytest.raises(urllib.error.URLError):  # hub mode without a token: a URLError, like an unreachable hub
        E.open(far + "/api/states", "ha", headers={"Authorization": "Bearer " + E.secret("ha")})


def test_an_unreachable_hub_is_a_urlerror(world, monkeypatch):
    E, far, vals, home = world
    E.set_mode("ha", "hub")
    monkeypatch.setenv("HUB_URL", "http://127.0.0.1:9")
    with pytest.raises(urllib.error.URLError):
        E.open(far + "/api/states", "ha", headers={"Authorization": "Bearer " + E.secret("ha")})


def test_hub_egress_endpoints_and_use(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    prof = tmp_path / "profile.json"
    prof.write_text(
        json.dumps({"services": {"ha_url": "http://198.51.100.7:8123/"}, "network": {"domain": "example.org"}})
    )
    for k, v in {"HOME": str(home), "ASSISTANT_TEST": "1", "ASSISTANT_PROFILE": str(prof),
                 "HUB_URL": "http://127.0.0.1:9"}.items():  # fmt: skip
        monkeypatch.setenv(k, v)
    monkeypatch.syspath_prepend(str(REPO / "packages"))
    from hubkit import egress as X

    ns = argparse.Namespace(set=["tracearr=http://198.51.100.8:3000"], write=True)
    assert X.endpoints(ns) == 1  # gym still missing
    out = capsys.readouterr().out
    assert "198.51" not in out and "example.org" not in out
    assert ["gym", "missing"] in [ln.split() for ln in out.splitlines()]
    f = home / ".config" / "claude-hub" / "egress.endpoints"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    rows = [ln.split() for ln in f.read_text().splitlines() if not ln.startswith("#")]
    assert rows == [["ha", "http://198.51.100.7:8123"], ["tracearr", "http://198.51.100.8:3000"],
                    ["dash", "https://dash.example.org"]]  # fmt: skip
    with pytest.raises(X.HubError):
        X.render_endpoints(["ha=ftp://x"])
    with pytest.raises(X.HubError):
        X.render_endpoints(["nope=http://x"])
    assert X.use(argparse.Namespace(handle="vault", mode="shadow")) == 0
    assert X.use(argparse.Namespace(handle=None, mode=None)) == 0
    assert ["vault", "shadow"] in [ln.split() for ln in capsys.readouterr().out.splitlines()]
    assert X.status(argparse.Namespace(json=False)) == 3  # the hub is unreachable here


def _big_list(key: str) -> list[dict]:
    """~10 MB of movie-like records (pretty-printed by the far side, as Radarr does), one echoing the key."""
    items = [{"id": i, "title": f"t{i}", "overview": "o" * 3000, "movieFile": {"size": i * 1000}} for i in range(3300)]
    items[2000]["echo"] = key
    return items


def test_hub_mode_reads_a_list_over_the_cap_in_pages(world):
    E, far, vals, home = world
    E.set_mode("radarr", "hub")
    items = _big_list(vals["radarr"])
    Far.big = json.dumps(items, indent=2).encode()
    assert len(Far.big) > 8 << 20
    n = len(Hub.seen)
    with E.open(far + "/api/v3/movie", "radarr", headers={"X-Api-Key": E.secret("radarr")}, timeout=60) as r:
        got = json.load(r)
        assert r.status == 200 and r.getheader("content-type") == "application/json"
    calls = Hub.seen[n:]
    assert "page" not in calls[0]["body"] and [c["body"].get("page", {}).get("from") for c in calls[1:]][0] == 0
    assert len(calls) >= 3  # one refused (over the cap), then at least two pages
    assert [m["id"] for m in got] == list(range(3300)) and got[5] == items[5]
    assert got[2000]["echo"].startswith("[redacted:")  # masked page by page
    assert all(vals["radarr"].encode() not in c["raw"] for c in Hub.seen)


def test_an_older_hub_without_paging_keeps_the_refusal(world, tmp_path):
    E, far, vals, home = world
    E.set_mode("radarr", "hub")
    pol = (HUB / "policy" / "egress.toml").read_text()
    assert "max_upstream = " in pol
    p = tmp_path / "old.toml"
    p.write_text("\n".join(x for x in pol.splitlines() if not x.startswith("max_upstream")))
    Hub.inj = eg.Injector.load(str(p), Hub.inj.store, Hub.inj.redactor, str(tmp_path / "state"))
    Far.big = json.dumps(_big_list("x"), indent=2).encode()
    with pytest.raises(E.Refused) as ri:
        E.open(far + "/api/v3/movie", "radarr", headers={"X-Api-Key": E.secret("radarr")}, timeout=60)
    assert ri.value.code == 502 and ri.value.error == "response_too_large"
