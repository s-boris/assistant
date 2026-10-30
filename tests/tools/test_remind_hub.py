"""The brain side of the hub's reminder timers: `remind hub` (sync, fire, tick, fallback, status) and
reminders.fire against a temp remind store (the repo's scripts/remind) and a fake hub /wf API. Covers: test state
leaves the hub alone (REMIND_HUB=1 = the fake hub), timers only for slots within the horizon, a press (+1 h) moving a
slot's timer, fire = one send then gone (no duplicate), a series' next slot, the DST instants, the sentinel's fallback.
No real hub, no real send."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import re
import subprocess
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from tools import gateway

REPO = Path(__file__).resolve().parents[2]
REMIND = REPO / "scripts" / "remind"


class FakeHub:
    """/wf/start (idempotent per id: PENDING; a cancelled id stays CANCELLED), /wf/<id>/cancel, GET /wf (pending),
    GET /health; health != 200 = down: every call answers 503."""

    def __init__(self) -> None:
        self.wf: dict[str, dict] = {}
        self.log: list[tuple[str, str]] = []
        self.health = 200
        hub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def reply(self, code: int, body: dict) -> None:
                b = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                if self.path == "/health":
                    return self.reply(hub.health, {"ok": hub.health == 200})
                hub.log.append(("GET", self.path))
                if hub.health != 200:
                    return self.reply(503, {"error": "down"})
                rows = [{"id": k, "status": v["status"]} for k, v in hub.wf.items() if v["status"] == "PENDING"]
                self.reply(200, {"rows": rows})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                hub.log.append(("POST", self.path))
                if hub.health != 200:
                    return self.reply(503, {"error": "down"})
                if self.path == "/wf/start":
                    w = hub.wf.setdefault(body["id"], {"status": "PENDING", "args": body["args"]})
                    return self.reply(200, {"id": body["id"], "status": w["status"]})
                m = re.fullmatch(r"/wf/(.+)/cancel", self.path)
                if m and m[1] in hub.wf:
                    hub.wf[m[1]]["status"] = "CANCELLED"
                    return self.reply(200, {"id": m[1], "status": "CANCELLED"})
                self.reply(404, {"error": "not_found"})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def pending(self) -> dict[str, dict]:
        return {k: v for k, v in self.wf.items() if v["status"] == "PENDING"}


@pytest.fixture
def hub(remind_env, monkeypatch):
    fh = FakeHub()
    tok = remind_env / "token"
    tok.write_text("t" * 32)
    for k, v in {"HUB_URL": fh.url, "HUB_TOKEN_FILE": str(tok), "REMIND_HUB": "1",
                 "REMIND_HUB_SYNC": "inline", "REMIND_TG_BUTTONS": str(remind_env / "tg")}.items():  # fmt: skip
        monkeypatch.setenv(k, v)
    yield fh
    fh.srv.shutdown()


def remind(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    import os

    return subprocess.run([sys.executable, str(REMIND), *args], capture_output=True, text=True, timeout=60,
                          env={**os.environ, **(env or {})})  # fmt: skip


def sent(d: Path) -> list[str]:
    p = d / "tg.log"
    return p.read_text().splitlines() if p.exists() else []


def mod():
    ld = importlib.machinery.SourceFileLoader("remind_mod", str(REMIND))
    spec = importlib.util.spec_from_loader("remind_mod", ld)
    m = importlib.util.module_from_spec(spec)
    ld.exec_module(m)
    return m


def test_due_instants_across_both_dst_directions(remind_env):
    m, z = mod(), ZoneInfo("Europe/Zurich")
    assert m.due_ts("2026-10-24 09:00") == datetime(2026, 10, 24, 9, 0, tzinfo=z).timestamp()
    # fall back (25.10.2026 03:00 CEST -> 02:00 CET): 02:30 is due at its FIRST pass (CEST = UTC+2)
    assert m.due_ts("2026-10-25 02:30") == datetime(2026, 10, 25, 0, 30, tzinfo=ZoneInfo("UTC")).timestamp()
    assert m.due_ts("2026-10-25 09:00") == datetime(2026, 10, 25, 8, 0, tzinfo=ZoneInfo("UTC")).timestamp()
    # spring forward (28.03.2027 02:00 CET -> 03:00 CEST): 02:30 never shows; `due` sends it at 03:00 CEST
    assert m.due_ts("2027-03-28 02:30") == datetime(2027, 3, 28, 1, 0, tzinfo=ZoneInfo("UTC")).timestamp()


def test_repeated_dst_hour_sends_each_slot_once(remind_env, hub, monkeypatch):
    # 25.10.2026 fall back: 02:00-03:00 runs twice, and hub schedules (the */5 tick) fire in both passes. A slot in
    # that hour goes out once, at its first pass; the repeat's tick (sweep + full sync), a late timer and a cron
    # pass find it gone, and the series' next timer is the next night's first pass.
    utc = ZoneInfo("UTC")
    first = f"@{datetime(2026, 10, 25, 0, 30, tzinfo=utc).timestamp():.0f}"  # 02:30 CEST
    again = f"@{datetime(2026, 10, 25, 1, 30, tzinfo=utc).timestamp():.0f}"  # 02:30 CET, the same wall minute
    monkeypatch.setenv("REMIND_NOW", "2026-10-25 01:00")
    remind("add", "--owner", "2026-10-25 02:30", "once")
    remind("add", "--owner", "--every", "daily", "2026-10-25 02:30", "nightly")
    ids = {r["text"]: r["id"] for r in json.loads(remind("list", "--json").stdout)["items"]}

    def fire(rid: str, now: str) -> str:
        return json.loads(remind("hub", "fire", rid, "2026-10-25 02:30", env={"REMIND_NOW": now}).stdout)["decision"]

    assert fire(ids["once"], first) == "fired"
    assert len(sent(remind_env)) == 2  # both slots in the first pass
    for now in (first, again):
        assert remind("hub", "tick", env={"REMIND_NOW": now}).returncode == 0
        assert remind("due", env={"REMIND_NOW": now}).returncode == 0
        assert fire(ids["once"], now) == "gone" and fire(ids["nightly"], now) == "gone"
    assert len(sent(remind_env)) == 2
    nxt = f"rem:{ids['nightly']}:202610260230"
    assert nxt in hub.pending() and hub.pending()[nxt]["args"]["ts"] == mod().due_ts("2026-10-26 02:30")


def test_test_state_leaves_the_hub_alone(remind_env, monkeypatch):
    fh = FakeHub()
    monkeypatch.setenv("HUB_URL", fh.url)
    assert remind("add", "--owner", "2026-10-20 18:00", "x").returncode == 0  # test state (REMIND_DB), no REMIND_HUB
    assert remind("hub", "status").stdout.startswith("reminders on the hub: off")
    assert json.loads(remind("hub", "fire", "0000beef", "2026-10-20 12:00").stdout)["decision"] == "off"
    assert json.loads(remind("hub", "fallback").stdout) == {"mode": "off", "ran": False}
    assert fh.log == []
    fh.srv.shutdown()


def test_sync_horizon_and_a_press_moves_the_timer(remind_env, hub):
    remind("add", "--owner", "2026-10-20 18:00", "soon")
    remind("add", "--owner", "2026-10-25 09:00", "later")  # > 26 h: no timer yet
    ids = {r["text"]: r["id"] for r in json.loads(remind("list", "--json").stdout)["items"]}
    pend = hub.pending()
    assert list(pend) == [f"rem:{ids['soon']}:202610201800"], pend
    assert pend[f"rem:{ids['soon']}:202610201800"]["args"]["ts"] == mod().due_ts("2026-10-20 18:00")
    # the +1 h button (the instant press path): the slot moves, its timer with it
    p = remind("press", env={"TELEGRAM_CALLBACK_DATA": f"rm:s1h:{ids['soon']}"})
    assert p.returncode == 0 and p.stdout.startswith("⏰ Snoozed"), p
    assert list(hub.pending()) == [f"rem:{ids['soon']}:202610201300"]
    assert hub.wf[f"rem:{ids['soon']}:202610201800"]["status"] == "CANCELLED"
    # moved back to 18:00: that slot's id was cancelled, so the timer comes back as :2
    remind("move", ids["soon"], "2026-10-20 18:00")
    assert list(hub.pending()) == [f"rem:{ids['soon']}:202610201800:2"]
    # the tick's full pass: a lost timer (hub reset) is started again; an orphan is cancelled
    hub.wf.clear()
    hub.wf["rem:0000beef:202610201900"] = {"status": "PENDING", "args": {}}
    out = json.loads(remind("hub", "sync", "--full", "--json").stdout)
    assert out["cancelled"] == 1 and out["started"] == 1, out
    assert list(hub.pending()) == [f"rem:{ids['soon']}:202610201800"]


def test_live_fires_once_and_a_series_gets_its_next_timer(remind_env, hub):
    remind("add", "--owner", "2026-10-20 12:00", "pay")
    remind("add", "--owner", "--every", "daily", "2026-10-20 12:00", "pills")
    ids = {r["text"]: r["id"] for r in json.loads(remind("list", "--json").stdout)["items"]}
    r = gateway.call("reminders.fire", {"id": ids["pay"], "at": "2026-10-20 12:00"})
    assert r.ok and r.data["decision"] == "fired" and r.data["mode"] == "live", r
    texts = sent(remind_env)
    assert len(texts) == 2 and any("pay" in t and "rm:s1h:" in t for t in texts)  # both due ones, with buttons
    # the series' slot went in the same pass: its own timer answers gone, nothing is sent twice
    assert json.loads(remind("hub", "fire", ids["pills"], "2026-10-20 12:00").stdout)["decision"] == "gone"
    assert json.loads(remind("hub", "fire", ids["pay"], "2026-10-20 12:00").stdout)["decision"] == "gone"
    assert len(sent(remind_env)) == 2
    assert f"rem:{ids['pills']}:202610211200" in hub.pending()  # next slot's timer
    by = [json.loads(ln)["by"] for ln in (remind_env / "deliveries.jsonl").read_text().splitlines()]
    assert by == ["hub", "hub"]


@pytest.fixture
def sentry(remind_env):
    """hub-sentinel (the repo's scripts/hub-sentinel) with its own HOME, a push stub and this remind store."""
    h = remind_env / "home"
    (h / "inbox").mkdir(parents=True)
    push = remind_env / "push"
    push.write_text(f'#!/bin/sh\nprintf "%s\\n" "$1" >> {remind_env / "pushes.log"}\n')
    push.chmod(0o755)
    return h


def sentinel(home: Path, hub: FakeHub, *args: str, now: float | None = None) -> subprocess.CompletedProcess:
    import os

    env = {**os.environ, "HOME": str(home), "ASSISTANT_TEST": "1", "HUB_URL": hub.url,
           "HUB_SENTINEL_PUSH": str(home.parent / "push"),
           "HUB_SENTINEL_REMIND": f"{sys.executable} {REMIND}"}  # fmt: skip
    a = [*args] + (["--now", str(now)] if now is not None else [])
    return subprocess.run([sys.executable, "-I", str(REPO / "scripts" / "hub-sentinel"), *a], env=env,
                          capture_output=True, text=True, timeout=120)  # fmt: skip


def pushes(d: Path) -> list[str]:
    p = d / "pushes.log"
    return [json.loads(x)["title"] for x in p.read_text().splitlines()] if p.exists() else []


def test_sentinel_fallback_live_sends_while_the_hub_is_down_and_the_timer_finds_it_gone(remind_env, hub, sentry):
    remind("add", "--owner", "2026-10-20 12:00", "pay")
    rid = json.loads(remind("list", "--json").stdout)["items"][0]["id"]
    assert f"rem:{rid}:202610201200" in hub.pending()
    hub.health = 503
    t = 1_800_000_000.0
    for dt in (0, 60, 120, 179):  # down, but under 3 min: the timers may still come back, nothing from here
        assert sentinel(sentry, hub, "check", now=t + dt).returncode == 0
    assert sent(remind_env) == [] and pushes(remind_env) == []
    assert sentinel(sentry, hub, "check", now=t + 180).returncode == 0  # 3 min: the push and the due pass from here
    assert pushes(remind_env) == ["Hub down"]
    assert len(sent(remind_env)) == 1 and "pay" in sent(remind_env)[0]
    sentinel(sentry, hub, "check", now=t + 240)  # still down: the next pass finds nothing due
    assert len(sent(remind_env)) == 1
    by = [json.loads(ln)["by"] for ln in (remind_env / "deliveries.jsonl").read_text().splitlines()]
    assert by == ["sentinel"]
    st = json.loads(sentinel(sentry, hub, "status", "--json").stdout)
    assert st["fallback"]["runs"] == 2 and st["fallback"]["sent"] == 1 and st["fallback"]["mode"] == "live", st
    assert json.loads(remind("hub", "status", "--json").stdout)["fallback"]["ran"] is True
    # the hub is back: DBOS wakes the overdue timer, which finds the slot gone (sent once, never twice)
    hub.health = 200
    fired = gateway.call("reminders.fire", {"id": rid, "at": "2026-10-20 12:00"})
    assert fired.ok and fired.data["decision"] == "gone", fired
    assert len(sent(remind_env)) == 1
    sentinel(sentry, hub, "check", now=t + 300)
    assert pushes(remind_env) == ["Hub down", "Hub back"]
    note = next((sentry / "inbox").iterdir()).read_text()
    assert "remind hub fallback" in note and "Reminders: 2 due pass(es) from here, 1 sent." in note
    decs = [json.loads(ln)["decision"] for ln in (remind_env / "hub-decisions.jsonl").read_text().splitlines()]
    assert decs == ["gone"]  # the sentinel's delivery: the timer found nothing left to send


def test_sentinel_own_switch_holds_the_fallback_and_nothing_goes_twice(remind_env, hub, sentry):
    remind("add", "--owner", "2026-10-20 12:00", "pay")
    hub.health = 503
    t = 1_800_000_000.0
    sentinel(sentry, hub, "check", now=t)  # down, under 3 min: nothing from here
    off = sentry / ".config" / "assistant" / "hub-sentinel-off"
    off.parent.mkdir(parents=True)
    off.write_text("")
    for dt in (180, 240):
        sentinel(sentry, hub, "check", now=t + dt)  # the sentinel's own switch: no fallback either
    assert sent(remind_env) == []
    off.unlink()
    sentinel(sentry, hub, "check", now=t + 300)
    assert len(sent(remind_env)) == 1
    assert remind("due").returncode == 0 and len(sent(remind_env)) == 1  # cron's pass after it: nothing twice
