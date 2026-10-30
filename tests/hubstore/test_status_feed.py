"""The status feed against a throwaway Postgres + hubd (hubfix): its snapshot and stream on its own listener, a
watched part whose service stops shows as down on the stream within seconds, the brain's report, caller rules, and
that the listener serves the feed and nothing else (no token is read there, no content leaves)."""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest
from hubfix import Hubd, Pg, free_port, missing, vendor_tree

from core import http as web

pytestmark = pytest.mark.skipif(bool(missing()), reason=missing() or "")

ALLOWED_TOP = {"seq", "at", "v", "release", "probe_s", "parts", "gateway", "approvals", "tg", "events", "traces",
               "brain", "errors", "ci"}  # fmt: skip


@pytest.fixture(scope="module")
def hub(tmp_path_factory):
    root = tmp_path_factory.mktemp("h17")
    pg = Pg(root).start()
    fport = free_port()
    env = {
        "HUB_FEED_PORT": str(fport),
        "HUB_FEED_PROBE_S": "0.3",
        "HUB_FEED_STATS_S": "0.3",
        "PHOENIX_URL": "",
        "HUB_FEED_BROWSER": f"127.0.0.1:{fport}",
    }  # claude-hub-browser's TCP probe: the feed's own listener
    h = Hubd(root, pg, vendor_tree(root / "vendor"), extra_env=env).start()
    h.feed = f"http://127.0.0.1:{fport}"  # type: ignore[attr-defined]
    yield h
    h.stop()
    pg.discard()


def get(url: str, headers: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with web.open(req, timeout=5) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def snapshot(hub, until=lambda s: bool(s.get("parts")), wait: float = 10.0) -> dict:
    end = time.monotonic() + wait
    s: dict = {}
    while time.monotonic() < end:
        s = get(hub.feed + "/feed")[1]
        if until(s):
            return s
        time.sleep(0.1)
    raise AssertionError(f"feed never matched: {json.dumps(s)[:600]}")


def part(s: dict, pid: str) -> dict | None:
    return next((p for p in s.get("parts") or [] if p["id"] == pid), None)


class Stream:
    """An SSE reader on the feed port: collects (seq, snapshot) per "feed" event in a thread."""

    def __init__(self, url: str) -> None:
        self.r = web.open(url + "/feed/stream", timeout=30)
        assert self.r.headers["Content-Type"] == "text/event-stream"
        self.events: list[tuple[float, dict]] = []
        self.t = threading.Thread(target=self.read, daemon=True)
        self.t.start()

    def read(self) -> None:
        ev, data = None, []
        try:
            for raw in self.r:
                line = raw.decode().rstrip("\n")
                if line.startswith("event:"):
                    ev = line[6:].strip()
                elif line.startswith("data:"):
                    data.append(line[5:].strip())
                elif not line:
                    if ev == "feed" and data:
                        self.events.append((time.monotonic(), json.loads("".join(data))))
                    ev, data = None, []
        except (OSError, ValueError):
            pass

    def wait_for(self, pred, wait: float = 10.0) -> tuple[float, dict]:
        end = time.monotonic() + wait
        seen = 0
        while time.monotonic() < end:
            for t, s in self.events[seen:]:
                if pred(s):
                    return t, s
            seen = len(self.events)
            time.sleep(0.05)
        raise AssertionError(
            "no matching feed event; last: " + (json.dumps(self.events[-1][1])[:500] if self.events else "none")
        )

    def close(self) -> None:
        self.r.close()


def test_snapshot_registry_states_and_no_content(hub):
    s = snapshot(hub, lambda s: (part(s, "claude-hub-pg") or {}).get("state") == "ok")
    assert set(s) <= ALLOWED_TOP
    # the CI runner block: counts and states only (no job names or branches)
    assert set(s.get("ci") or {}) <= {"state", "slots", "set", "busy", "queued", "heartbeat_s", "toolchain_ok"}
    assert part(s, "claude-hub")["state"] == "ok"
    assert part(s, "claude-hub-tg")["state"] == "unknown"  # no tgloader report yet
    assert part(s, "hub-gateway")["state"] in ("off", "down")  # HUB_GW_PORT=off here
    assert part(s, "tg-adapter")["state"] == "unknown"  # no brain report yet
    assert s["traces"]["state"] == "unknown"
    assert s["approvals"]["pending"] == 0 and s["tg"]["hub"]["queued"] == 0
    blob = json.dumps(s)
    for tok in hub.tokens.values():
        assert tok not in blob
    # the brain-token route answers the same snapshot
    code, body = hub.call("GET", "/feed")
    assert code == 200 and body["seq"] >= 1
    assert hub.call("GET", "/feed", who="tg")[0] == 200
    h = hub.call("GET", "/health")[1]
    assert h["feed"]["state"] == "ok" and h["feed"]["port"]


def test_pending_approval_counted_without_its_summary(hub):
    code, body = hub.call(
        "POST", "/approvals", {"action_sha": "c" * 64, "summary": "Pay the secret invoice", "ttl": 60}
    )
    assert code == 200
    s = snapshot(hub, lambda s: (s.get("approvals") or {}).get("pending") == 1)
    assert "secret invoice" not in json.dumps(s) and body["nonce"] not in json.dumps(s)
    hub.call("POST", f"/approvals/{body['nonce']}/cancel", {})


def test_stream_reflects_a_stopped_target_within_seconds(hub):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    port = srv.getsockname()[1]
    st = Stream(hub.feed)
    try:
        code, body = hub.call("POST", "/feed/watch", {"id": "t-target", "tcp": f"127.0.0.1:{port}", "ttl": 60,
                                                      "label": "test target"})  # fmt: skip
        assert code == 200, body
        st.wait_for(lambda s: (part(s, "t-target") or {}).get("state") == "ok")
        t0 = time.monotonic()
        srv.close()  # "the container stops"
        t1, s = st.wait_for(lambda s: (part(s, "t-target") or {}).get("state") == "down")
        assert t1 - t0 < 5.0
        assert part(s, "t-target")["kind"] == "watch"
        # removed: the part leaves the feed
        assert hub.call("POST", "/feed/watch", {"id": "t-target", "remove": True})[1]["removed"] is True
        st.wait_for(lambda s: part(s, "t-target") is None)
    finally:
        st.close()
        srv.close()


def test_brain_report_and_its_rules(hub):
    rep = {"stores": {"events": "hub", "outbox": "sqlite", "approvals": "sqlite"},
           "tg": {"sent_24h": 12, "failed_24h": 0, "dupes_24h": 1, "send_p95_s": 0.5, "receipts_24h": 9,
                  "receipt_p95_s": 2.1, "dropped_24h": 0},
           "parts": {"tg-adapter": {"state": "ok", "detail": "running"}}, "approvals": {"pending": 3},
           "ttl": 120}  # fmt: skip
    assert hub.call("POST", "/feed/brain", rep)[0] == 200
    s = snapshot(hub, lambda s: (part(s, "tg-adapter") or {}).get("state") == "ok")
    assert s["tg"]["brain"]["sent_24h"] == 12 and s["tg"]["brain"]["fresh"] is True
    assert s["tg"]["store"] == "sqlite" and s["approvals"]["store"] == "sqlite" and s["events"]["store"] == "hub"
    assert s["approvals"]["live_pending"] == 3
    assert hub.call("POST", "/feed/brain", rep, who="tg")[0] == 403
    assert hub.call("POST", "/feed/watch", {"id": "x-1", "tcp": "127.0.0.1:9"}, who="tg")[0] == 403
    for bad in ({"text": "hello"}, {"tg": {"last_message": 1}}, {"tg": {"sent_24h": "12"}},
                {"parts": {"claude-hub": {"state": "ok"}}}, {"parts": {"tg-adapter": {"state": "fine"}}},
                {"parts": {"tg-adapter": {"state": "ok", "detail": "<script>"}}}, {"stores": {"outbox": "redis"}},
                {"ttl": 5}):  # fmt: skip
        assert hub.call("POST", "/feed/brain", bad)[0] == 400, bad
    for bad in ({"id": "claude-hub", "tcp": "127.0.0.1:1"}, {"id": "Bad_Id", "tcp": "127.0.0.1:1"},
                {"id": "ok-id", "tcp": "http://x:1"}, {"id": "ok-id", "tcp": "127.0.0.1:0"},
                {"id": "ok-id", "tcp": "127.0.0.1:1", "ttl": 99999}):  # fmt: skip
        assert hub.call("POST", "/feed/watch", bad)[0] == 400, bad


def test_listener_serves_the_feed_only(hub):
    assert get(hub.feed + "/health")[0] == 404
    assert get(hub.feed + "/approvals", {"Authorization": f"Bearer {hub.tokens['brain']}"})[0] == 404
    assert get(hub.feed + "/feed/health")[1]["ok"] is True
    assert get(hub.feed + "/feed/")[0] == 200
    req = urllib.request.Request(hub.feed + "/feed", data=b"{}", method="POST")
    with pytest.raises(urllib.error.HTTPError) as e:
        web.open(req, timeout=5)
    assert e.value.code == 405


def test_adapter_containers_from_loader_reports_and_tcp(hub):
    """claude-hub-tg reads ok once tgloader reports running on /tg/hold/seen, off while it reports held;
    claude-hub-browser is a TCP connect (here: to the feed's own listener)."""
    s = snapshot(hub, lambda s: (part(s, "claude-hub-browser") or {}).get("state") == "ok")
    assert part(s, "claude-hub-voice")["state"] in ("unknown", "down") and part(s, "claude-hub-phone")
    code, _ = hub.call("POST", "/tg/hold/seen", {"state": "running", "release": "abcdef1", "why": ""}, who="tg")
    assert code == 200
    s = snapshot(hub, lambda s: (part(s, "claude-hub-tg") or {}).get("state") == "ok")
    code, _ = hub.call("POST", "/tg/hold/seen", {"state": "held", "release": None}, who="tg")
    assert code == 200
    s = snapshot(hub, lambda s: (part(s, "claude-hub-tg") or {}).get("state") == "off")
    assert part(s, "claude-hub-tg")["detail"] == "held"
