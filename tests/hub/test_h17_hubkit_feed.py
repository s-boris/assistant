"""The feed's brain side (hubkit.feed, `hub feed`): the tunnel keeper (a fake ssh: never a real tunnel), the brain's
report mapped from `events metrics --tg` and `tg-adapter status` (fake bins), the push to a fake hub, the kill switch,
and that test mode refuses the real ssh and the real hub."""

from __future__ import annotations

import http.server
import json
import os
import threading
import time
from pathlib import Path

import pytest
from hubkit import HubError, feed
from hubkit import cli as hubcli

METRICS = {"T2": {"dropped": 1}, "T3": {"n": 9, "p95_s": 2.5}, "T4": {"sent": 30, "failed": 0, "duplicates": 2,
                                                                       "p95_s": 0.4}}  # fmt: skip


def script(path: Path, body: str) -> str:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return str(path)


class FakeHub:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []
        self.old_hub = False  # a hub release before the approvals count: 400 on that key
        self.no_slo = False
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                outer.posts.append((self.path, body))
                if outer.old_hub and "approvals" in body:
                    return self._send({"error": "bad_request", "hint": "unknown keys: ['approvals']"}, 400)
                if outer.no_slo and "slo" in body:  # a hub release without the slo key
                    return self._send({"error": "bad_request", "hint": "unknown keys: ['slo']"}, 400)
                self._send({"ok": True})

            def do_GET(self):
                parts = [{"id": "claude-hub", "state": "ok"}, {"id": "t-x", "state": "down"}]
                gw = {"state": "ok"}
                self._send(
                    {"seq": 3, "at": 1.0, "parts": parts, "gateway": gw, "approvals": {}, "tg": {}, "traces": {}}
                )

            def _send(self, obj, code=200):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.srv.shutdown()


@pytest.fixture
def env(hubhome, tmp_path, monkeypatch):
    hub = FakeHub()
    (tmp_path / "brain-token").write_text("t" * 32)
    os.chmod(tmp_path / "brain-token", 0o600)
    bins = tmp_path / "fakebin"
    bins.mkdir()
    monkeypatch.setenv("HUB_URL", hub.url)
    monkeypatch.setenv("HUB_FEED_URL", hub.url)
    monkeypatch.setenv("HUB_FEED_VM", "tester@vm.example.invalid")
    monkeypatch.setenv("HUB_FEED_VM_PORT", "18791")
    monkeypatch.setenv("HUB_FEED_SSH", script(bins / "ssh", 'echo "$@" >> "$(dirname "$0")/ssh.args"\nsleep 30\n'))
    monkeypatch.setenv("HUB_FEED_EVENTS_BIN", script(bins / "events", f"echo '{json.dumps(METRICS)}'\n"))
    rows = [{"nonce": "n1", "summary": "Pay SECRET-SUMMARY", "ref": "r", "state": "pending"},
            {"nonce": "n2", "summary": "x", "ref": "r", "state": "expired"}]  # fmt: skip
    monkeypatch.setenv("HUB_FEED_APPROVALS_BIN", script(bins / "approvals", f"echo '{json.dumps(rows)}'\n"))
    monkeypatch.setenv("HUB_FEED_TGA_BIN", script(bins / "tga", 'echo \'{"configured": true, "running": true}\'\n'))
    yield {"hub": hub, "bins": bins, "home": hubhome}
    feed.tunnel_stop()
    hub.close()


def test_report_maps_counts_and_states_only(env):
    r = feed.report()
    assert r["stores"] == {"events": "hub", "outbox": "hub", "approvals": "hub"}  # a hub is set (HUB_URL)
    assert r["tg"] == {"sent_24h": 30, "failed_24h": 0, "dupes_24h": 2, "send_p95_s": 0.4, "receipts_24h": 9,
                       "receipt_p95_s": 2.5, "dropped_24h": 1}  # fmt: skip
    assert r["parts"] == {"tg-adapter": {"state": "ok", "detail": "running"}}
    assert r["approvals"] == {"pending": 1} and "SECRET-SUMMARY" not in json.dumps(r)
    script(env["bins"] / "approvals", "exit 1\n")
    assert "approvals" not in feed.report()  # no list: no number (never a guess)
    script(env["bins"] / "tga", 'echo \'{"configured": true, "running": false, "switch_off": true}\'\n')
    assert feed.report()["parts"]["tg-adapter"]["state"] == "off"
    hub = '"hub": {"consumers": [{"caller": "tg", "age": %s, "bot": "live"}]}'  # the adapter runs in claude-hub-tg
    tga = 'echo \'{"configured": true, "running": false, "switch_off": true, %s}\'\n'
    script(env["bins"] / "tga", tga % (hub % "0.4"))
    assert feed.report()["parts"]["tg-adapter"] == {"state": "ok", "detail": "in claude-hub-tg"}
    script(env["bins"] / "tga", tga % (hub % "300"))
    assert feed.report()["parts"]["tg-adapter"] == {"state": "down", "detail": "claude-hub-tg not polling"}
    script(env["bins"] / "tga", "exit 1\n")
    assert feed.report()["parts"]["tg-adapter"] == {"state": "unknown", "detail": "status failed"}


def _until(pred, timeout: float = 15.0):
    """pred()'s first truthy value, polled until timeout (the fake ssh runs on its own: under host load its first
    line lands a while after Popen returns). A deterministic wait on the condition, never a fixed sleep."""
    end = time.monotonic() + timeout
    while True:
        v = pred()
        if v or time.monotonic() > end:
            return v
        time.sleep(0.02)


def test_ensure_starts_the_tunnel_once_and_pushes(env):
    did = feed.ensure()
    assert did and did[0].startswith("tunnel started")
    pid = _until(feed.tunnel_pid)
    assert pid
    f = env["bins"] / "ssh.args"
    args = _until(lambda: f.is_file() and f.read_text().endswith("\n") and f.read_text())
    assert args, "the fake ssh never wrote its arguments"
    assert "-R 127.0.0.1:18791:claude-hub:8702 tester@vm.example.invalid" in args and "ExitOnForwardFailure=yes" in args
    assert feed.ensure() == []  # alive: no second tunnel
    assert feed.tunnel_pid() == pid
    paths = [p for p, _ in env["hub"].posts]
    assert paths == ["/feed/brain", "/feed/brain"]
    assert env["hub"].posts[0][1]["tg"]["sent_24h"] == 30


def test_push_to_an_older_hub_drops_only_the_approvals_count(env):
    env["hub"].old_hub = True
    sent = feed.push()
    assert "approvals" not in sent and sent["tg"]["sent_24h"] == 30
    assert [("approvals" in b) for _, b in env["hub"].posts] == [True, False]


def test_kill_switch_stops_the_tunnel_and_the_pushes(env, capsys):
    feed.ensure()
    assert feed.tunnel_pid()
    assert hubcli.main(["feed", "off"]) == 0
    assert feed.tunnel_pid() is None
    n = len(env["hub"].posts)
    assert feed.ensure() == []
    assert len(env["hub"].posts) == n and feed.tunnel_pid() is None
    assert hubcli.main(["feed", "on"]) == 0
    assert feed.ensure()[0].startswith("tunnel started")


def test_show_watch_and_status(env, capsys):
    assert hubcli.main(["feed"]) == 0
    out = capsys.readouterr().out
    assert "feed seq 3" in out and "t-x down" in out
    assert hubcli.main(["feed", "watch", "t-x", "claude-bot:18799", "--ttl", "120", "--label", "test target"]) == 0
    assert env["hub"].posts[-1] == ("/feed/watch", {"id": "t-x", "tcp": "claude-bot:18799", "ttl": 120,
                                                    "label": "test target"})  # fmt: skip
    assert hubcli.main(["feed", "unwatch", "t-x"]) == 0
    assert env["hub"].posts[-1] == ("/feed/watch", {"id": "t-x", "remove": True})
    assert hubcli.main(["feed", "watch", "only-id"]) == 2
    assert hubcli.main(["feed", "status", "--json"]) == 0
    assert feed.status()["feed"] == "ok"


def test_test_mode_refuses_real_ssh_and_real_feed(env, monkeypatch):
    monkeypatch.delenv("HUB_FEED_SSH")
    with pytest.raises(HubError):
        feed.tunnel_argv()
    monkeypatch.delenv("HUB_FEED_URL")
    with pytest.raises(HubError):
        feed.feed_url()


def test_bad_overrides_are_refused(env, monkeypatch):
    monkeypatch.setenv("HUB_FEED_TARGET", "claude-hub:8702; rm -rf /")
    with pytest.raises(HubError):
        feed.marker()
    monkeypatch.setenv("HUB_FEED_TARGET", "claude-hub:8702")
    monkeypatch.setenv("HUB_FEED_VM", "-oProxyCommand=x")
    with pytest.raises(HubError):
        feed.web_vm()
    monkeypatch.setenv("HUB_FEED_VM_PORT", "80")
    with pytest.raises(HubError):
        feed.vm_port()


def test_slo_tiles_ride_the_report_while_fresh(env, monkeypatch):
    """The cached SLO tiles go into the report while the cache is fresh; a hub release without the key gets the rest
    of the report."""
    import time as _t

    home = Path(os.environ["HOME"])
    cache = home / ".local" / "state" / "slo" / "tiles.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    tile = {"id": "tap_ack", "value": 0.2, "target": 1.0, "op": "<", "unit": "s", "n": 5, "status": "ok",
            "window_days": 7, "label": "tap -> ack", "note": None}  # fmt: skip
    cache.write_text(json.dumps({"at": _t.time() - 7200, "tiles": [tile]}))
    assert "slo" not in feed.report()  # stale cache
    cache.write_text(json.dumps({"at": _t.time(), "tiles": [tile]}))
    r = feed.report()
    assert r["slo"] == [{k: tile[k] for k in ("id", "value", "target", "op", "unit", "n", "status", "window_days")}]
    env["hub"].no_slo = True
    sent = feed.push()
    assert "slo" not in sent and sent["tg"]["sent_24h"] == 30
    assert [("slo" in b) for _, b in env["hub"].posts] == [True, False]
