"""hubkit.phonemirror mirrors the gateway's GET /internal/export into a SQLite file the brain's readers
(usage.py, board, dash-data) query with their own SQL, once a minute."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from hubkit import phonemirror as PM

NOW = int(time.time())
EXPORT = {
    "ok": True, "schema": 1, "at": NOW, "since": (NOW - 8 * 86400) * 1000,
    "devices": [{"id": "d_A", "status": "active", "last_seen": NOW * 1000, "app_code": 49},
                {"id": "d_B", "status": "revoked", "last_seen": 1, "app_code": 3}],
    "state": [{"device_id": "d_A", "k": "place", "v": json.dumps({"zone": "home", "town": "T"}), "at": NOW},
              {"device_id": "_gw", "k": "app_latest", "v": json.dumps({"name": "0.4.9", "code": 49}), "at": NOW}],
    "events": [{"id": "e1", "device_id": "d_A", "kind": "tile", "at": NOW, "body": json.dumps({"name": "sleepin:on"})}],
    "push_log": [{"id": "p1", "at": NOW * 1000, "level": "timed", "tag": "rem-1", "delivered_ms": NOW * 1000,
                  "acked_ms": None, "verb": "done", "action_at": NOW}],
    "cards": [{"id": "c1", "kind": "decision", "created": NOW, "answered": NOW}],
    "chat": [{"id": "m1", "at": NOW, "dir": "in"}],
    "inbound": [{"id": "i1", "t": "crash", "at": NOW}],
}  # fmt: skip


@pytest.fixture(autouse=True)
def _scratch_home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / ".secrets").mkdir(parents=True)
    (h / ".secrets" / "phone-cli-token").write_text("cli-token\n")
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("ASSISTANT_TEST", "1")
    for v in ("HUB_PHONE_MIRROR", "HUB_PHONE_STATE", "HUB_PHONE_CLI_TOKEN", "PHONEGW_URL", "PHONEGW_TOKEN_FILE"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(PM, "_last_try", 0.0)
    return h


@pytest.fixture
def gateway(monkeypatch):
    seen = []

    class Gw(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            seen.append((self.path, self.headers.get("Authorization")))
            ok = self.headers.get("Authorization") == "Bearer cli-token" and self.path.startswith("/internal/export?")
            body = json.dumps(EXPORT if ok else {"ok": False}).encode()
            self.send_response(200 if ok else 401)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Gw)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("PHONEGW_URL", f"http://127.0.0.1:{srv.server_address[1]}")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")  # the opener must ignore proxies
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    yield seen
    srv.shutdown()


def test_mirror_answers_the_readers_sql(_scratch_home, gateway):
    assert PM.mirror_path() == _scratch_home / ".local/state/claude-hub/phonegw-mirror.db"
    assert PM.refresh() == "ok"
    f = PM.mirror_path()
    assert f.stat().st_mode & 0o777 == 0o600
    path, auth = gateway[0]
    assert auth == "Bearer cli-token" and path.startswith("/internal/export?since=")
    since = int(path.split("=", 1)[1]) / 1000
    assert abs(since - (time.time() - 8 * 86400)) < 60
    con = sqlite3.connect(f"file:{f}?mode=ro", uri=True)
    # usage.py _phone / collect_phonegw, board p_app_installed, dash-data sys_phone: their SQL verbatim
    assert con.execute("SELECT id FROM devices WHERE status='active' ORDER BY last_seen DESC LIMIT 1").fetchone() == (
        "d_A",
    )
    assert len(con.execute("SELECT device_id, k, v, at FROM state").fetchall()) == 2
    assert con.execute("SELECT id, app_code FROM devices WHERE status='active'").fetchall() == [("d_A", 49)]
    row = con.execute("SELECT v FROM state WHERE device_id='_gw' AND k='app_latest'").fetchone()
    assert json.loads(row[0]) == {"name": "0.4.9", "code": 49}
    assert con.execute("SELECT id, kind, at, body FROM events WHERE at >= ?", (NOW - 7 * 86400,)).fetchall() == [
        ("e1", "tile", NOW, json.dumps({"name": "sleepin:on"}))
    ]
    assert (
        len(
            con.execute(
                "SELECT id, at, level, tag, delivered_ms, acked_ms, verb, action_at FROM push_log WHERE at >= ?",
                ((NOW - 7 * 86400) * 1000,),
            ).fetchall()
        )
        == 1
    )
    assert con.execute("SELECT id, kind, created, answered FROM cards WHERE created >= ?", (0,)).fetchall() == [
        ("c1", "decision", NOW, NOW)
    ]
    assert con.execute("SELECT id, at, dir FROM chat WHERE at >= ?", (0,)).fetchall() == [("m1", NOW, "in")]
    assert con.execute("SELECT id, t, at FROM inbound WHERE at > ? ORDER BY at", (0,)).fetchall() == [
        ("i1", "crash", NOW)
    ]
    con.close()
    assert PM.refresh() == "fresh" and len(gateway) == 1  # once a minute
    os.utime(f, (time.time() - 61, time.time() - 61))
    PM._last_try = 0.0  # noqa: SLF001 (a new minute in the same process)
    assert PM.refresh() == "ok" and len(gateway) == 2
    assert [p.name for p in f.parent.iterdir()] == [f.name]  # no temp file left


def test_a_failed_fetch_keeps_the_old_mirror(_scratch_home, gateway, monkeypatch):
    assert PM.refresh() == "ok"
    before = PM.mirror_path().read_bytes()
    (_scratch_home / ".secrets" / "phone-cli-token").write_text("wrong\n")
    assert PM.refresh(force=True) == "failed"
    monkeypatch.setenv("PHONEGW_URL", "http://127.0.0.1:9")
    assert PM.refresh(force=True) == "failed"
    (_scratch_home / ".secrets" / "phone-cli-token").unlink()
    assert PM.refresh(force=True) == "failed"
    assert PM.mirror_path().read_bytes() == before


def test_inbox_once_refreshes_the_mirror(_scratch_home, monkeypatch):
    from hubkit import phone as HP

    calls = []
    monkeypatch.setattr(HP.phonemirror, "refresh", lambda force=False: calls.append(force) or "fresh")
    monkeypatch.setattr(HP.hub, "call", lambda *a, **k: {"rows": []})
    monkeypatch.setenv("HUB_PHONE_STATE", str(_scratch_home / "st"))
    assert HP.inbox_once(0.0) == (0, {}) and calls == [False]


def _hub_mode(monkeypatch, refuse=None):
    """Handle "phone" in hub mode, core.egress.open faked (the hub's answer = the export, or a refusal)."""
    import io

    from core import egress

    calls = []
    monkeypatch.setattr(PM, "URL_DEFAULT", os.environ["PHONEGW_URL"])
    monkeypatch.delenv("PHONEGW_URL", raising=False)
    monkeypatch.setattr(egress, "mode", lambda h: "hub")

    def fake_open(req, handle, timeout=20.0):
        calls.append((req.full_url, req.get_header("Authorization"), handle))
        if refuse:
            raise egress.Refused(502, refuse)
        return io.BytesIO(json.dumps(EXPORT).encode())

    monkeypatch.setattr(egress, "open", fake_open)
    tok = PM._token_file()
    tok.parent.mkdir(parents=True, exist_ok=True)
    tok.write_text("cli-token\n", encoding="utf-8")
    return calls


def test_fetch_by_handle_in_hub_mode(_scratch_home, gateway, monkeypatch):
    calls = _hub_mode(monkeypatch)
    assert PM.fetch().get("ok")
    assert calls and calls[0][1] == "Bearer hub:secret:phone" and calls[0][2] == "phone"
    assert gateway == []


def test_fetch_over_the_hub_cap_goes_direct(_scratch_home, gateway, monkeypatch):
    calls = _hub_mode(monkeypatch, refuse="response_too_large")
    assert PM.fetch().get("ok")
    assert len(calls) == 1 and len(gateway) == 1 and gateway[0][1] == "Bearer cli-token"


def test_fetch_other_refusal_is_not_retried(_scratch_home, gateway, monkeypatch):
    from core import egress

    _hub_mode(monkeypatch, refuse="egress_denied")
    with pytest.raises(egress.Refused):
        PM.fetch()
    assert gateway == []


def test_inbox_once_rides_over_a_hub_restart(monkeypatch):
    """A hub deploy restarts the gateway for a few seconds: the per-minute `hub phone inbox --once --wait 25` polls
    again every RETRY_S s with what is left of its wait (was: exit 3 on every hub deploy); still down: HubDown."""
    import argparse

    from hubkit import phone as HP

    from core import hub

    clock = [1000.0]
    calls = []

    def fake_once(wait):
        calls.append(wait)
        if len(calls) == 1:
            clock[0] += 8.0
            raise hub.HubDown("RemoteDisconnected")
        clock[0] += wait
        return 0, {}

    monkeypatch.setattr(HP, "inbox_once", fake_once)
    monkeypatch.setattr(HP.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(HP.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    assert HP.inbox(argparse.Namespace(once=True, wait=25.0)) == 0
    assert calls == [25.0, 12.0]  # 25 - 8 - 5

    def down(wait):
        clock[0] += 1.0
        raise hub.HubDown("ConnectionRefusedError")

    monkeypatch.setattr(HP, "inbox_once", down)
    with pytest.raises(hub.HubDown):
        HP.inbox(argparse.Namespace(once=True, wait=25.0))
    with pytest.raises(hub.HubDown):
        HP.inbox(argparse.Namespace(once=True, wait=0.0))
