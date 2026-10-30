"""sleepin (the sleep-in guard), its gate as `tg --soft` runs it, and remind's sleep-in hold. (`tg --soft` itself:
tests/tg/test_tg_flags.py.)

Isolated: HOME is tmp_path/home (every sleepin default path lives under it; no profile, so the profile's fallback
zone), the clock is faked (--at, SLEEPIN_NOW, REMIND_NOW), `tg` is a stub first on PATH, remind runs on temp state
with a stub guard (REMIND_SLEEPIN_CMD), the hub's Telegram rows come from a fake hub on 127.0.0.1. The repo's
sleepin and remind are staged in a bin dir whose lib/ holds the repo's core, profile, tripclock and remindwhen
modules (as deployed; nothing from the live ~/bin). Fictional events and places only."""

import datetime as dt
import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CLEAR = ("SLEEPIN_", "REMIND_", "ASSISTANT_PROFILE", "HUB_URL", "TG_SOFT")

EVENTS = [
    ("a", "2026-10-21T21:00:00+02:00", "2026-10-21T22:00:00+02:00", False, "Evening class", {}),
    ("b", "2026-10-22T19:00:00+02:00", "2026-10-22T22:30:00+02:00", False, "Show", {}),
    ("c", "2026-10-22T23:07:00+02:00", "2026-10-23T00:14:00+02:00", False, "🚆 City → Home", {"travel": "train"}),
    ("d", "2026-10-24T16:00:00+02:00", "2026-10-25T02:00:00+02:00", False, "Party", {}),
    ("e", "2026-10-30T20:00:00+01:00", "2026-10-31T06:00:00+01:00", False, "Night out", {}),
    ("f", "2026-10-31T05:14:00+01:00", "2026-10-31T07:30:00+01:00", False, "🚆 Town → Home", {"travel": "train"}),
    ("g", "2026-11-02", "2026-11-04", True, "Trip", {}),
    ("h", "2026-11-05T06:00:00+01:00", "2026-11-05T08:00:00+01:00", False, "Early start", {}),
    ("i", "2026-11-06T22:00:00+01:00", "2026-11-06T23:00:00+01:00", False, "Ends at 23", {}),
    ("j", "2026-11-08T20:30:00+01:00", "2026-11-09T06:45:00+01:00", False, "✈️ Overnight flight", {"travel": "flight"}),
    ("k", "2026-11-10T09:00:00+01:00", "2026-11-11T08:00:00+01:00", False, "Long event", {}),
]
HOME_TRIP = {
    "zone": "home",
    "since": "2026-10-20 18:00",
    "trip": {"left": "2026-10-20 07:50", "back": "2026-10-20 18:00"},
}


def stage(d: Path) -> Path:
    """bin/sleepin + bin/remind + lib/ (core, assistant_profile, tripclock, remindwhen) from the repo."""
    b = d / "bin"
    (b / "lib").mkdir(parents=True)
    for n in ("sleepin", "remind"):
        shutil.copy2(REPO / "scripts" / n, b / n)
    (b / "lib" / "core").symlink_to(REPO / "core")
    for n in ("assistant_profile.py", "tripclock.py", "remindwhen.py"):
        (b / "lib" / n).symlink_to(REPO / "scripts" / "lib" / "legacy" / n)
    return b


def sh(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


class Box:
    """One temp HOME with the calendar, place state, journal and a stub tg; runs the staged sleepin / remind."""

    def __init__(self, tmp: Path):
        self.t, self.home = tmp, tmp / "home"
        self.bin = stage(tmp)
        self.cal = self.home / ".local/state/calcache/events.json"
        self.pl = self.home / ".local/state/remind/place-state.json"
        self.jr = self.home / ".claude/channels/telegram/inbound-journal.jsonl"
        self.st = self.home / ".local/state/sleepin"
        self.tglog = tmp / "tg.log"
        stubs = tmp / "stubs"
        for p in (self.cal, self.pl, self.jr):
            p.parent.mkdir(parents=True, exist_ok=True)
        stubs.mkdir()
        sh(stubs / "tg", f'[ -e "{tmp}/tg-fail" ] && exit 1\nprintf \'%s\\n\' "$*" >> "{self.tglog}"\n')
        env = {k: v for k, v in os.environ.items() if not k.startswith(CLEAR)}
        env.update(HOME=str(self.home), PATH=f"{stubs}{os.pathsep}{env.get('PATH', '/usr/bin:/bin')}")
        env["ASSISTANT_TEST"] = "1"
        self.env = env
        evs = [dict(id=i, start=s, end=e, all_day=ad, title=title, flags=fl) for i, s, e, ad, title, fl in EVENTS]
        self.cal.write_text(json.dumps({"version": 1, "events": evs}))
        self.place(HOME_TRIP)

    def run(self, *args, now=None, prog="sleepin", **env):
        e = dict(self.env, **env)
        if now:
            e["SLEEPIN_NOW"] = now
        return subprocess.run(
            [str(self.bin / prog), *args], env=e, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL
        )

    def out(self, *args, **kw):
        return self.run(*args, **kw).stdout.strip()

    def act(self, at, **env):
        """0 = hold, 1 = send."""
        return self.run("active", "--at", at, **env).returncode

    def soft(self, text, now):
        """What `tg --soft TEXT` runs (the stub tg first on PATH)."""
        return self.run("gate", "--", "tg", text, now=now, TG_SOFT="1").returncode

    def sent(self):
        return len(self.tglog.read_text().splitlines()) if self.tglog.exists() else 0

    def tg_lines(self):
        return self.tglog.read_text().splitlines()

    def qlen(self):
        try:
            return len(json.loads((self.st / "queue.json").read_text()))
        except (OSError, ValueError):
            return 0

    def holds(self):
        return json.loads((self.st / "holds.json").read_text())

    def place(self, p):
        self.pl.write_text(json.dumps(p))

    def journal(self, at, owner=True):
        with self.jr.open("a") as f:
            f.write(json.dumps({"id": f"x{at}{owner}", "at": at, "kind": "message", "owner": owner}) + "\n")

    def clear_journal(self):
        self.jr.write_text("")


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


# ---------------------------------------------------------------- calendar rule + window
@pytest.mark.parametrize(
    "at,want",
    [
        ("2026-10-22 09:00", 1),  # no late night (class ends 22:00): send
        ("2026-10-23 05:59", 1),  # train home 00:14: window from 06:00
        ("2026-10-23 06:00", 0),
        ("2026-10-23 12:29", 0),
        ("2026-10-23 12:30", 1),
        ("2026-11-03 09:00", 1),  # all-day event: no guard
        ("2026-11-05 09:00", 1),  # early start (06:00-08:00) is not a late night
        ("2026-11-07 09:00", 1),  # event ending exactly 23:00: no guard
        ("2026-11-09 09:00", 0),  # overnight flight landing 06:45: hold
        ("2026-11-11 09:00", 1),  # 23 h event: no guard
    ],
)
def test_calendar_rule_and_window(box, at, want):
    assert box.act(at) == want


def test_active_verbose_says_why(box):
    assert "HOLD until 12:30" in box.out("active", "-v", "--at", "2026-10-23 09:00")


# ---------------------------------------------------------------- DST: the clocks go back on 25.10
def test_dst_morning(box):
    assert box.act("2026-10-25 09:00") == 0  # party until 02:00 CEST
    assert box.act("2026-10-25 12:29") == 0
    assert box.act("2026-10-25 12:30") == 1
    ep = {m: int(dt.datetime(2026, 10, 25, h, m2, tzinfo=dt.UTC).timestamp()) for m, h, m2 in (
        ("e1", 10, 31), ("e2", 11, 29), ("e3", 11, 30))}  # fmt: skip
    assert box.run("active", now=f"@{ep['e1']}").returncode == 0  # 10:31Z = 11:31 CET (CEST math: 12:31)
    assert box.run("active", now=f"@{ep['e2']}").returncode == 0  # 11:29Z = 12:29 CET
    assert box.run("active", now=f"@{ep['e3']}").returncode == 1  # 11:30Z = 12:30 CET
    assert box.act("2026-10-25 02:30") == 1  # wall 02:30 (the ambiguous hour) is before the window


# ---------------------------------------------------------------- his message ends it (after the night is over)
def test_his_message_ends_it(box):
    assert box.act("2026-10-31 09:00") == 0  # late night ending 07:30 (train home)
    box.journal("2026-10-31T07:00:00.000Z")  # 08:00 local: still before the train home + 60 min (08:30)
    box.journal("2026-10-31T07:20:00.000Z", owner=False)  # not his: never counts
    assert box.act("2026-10-31 08:10") == 0
    box.journal("2026-10-31T07:40:00.000Z")  # 08:40 local
    assert box.act("2026-10-31 08:35") == 0
    assert box.act("2026-10-31 08:45") == 1
    assert "ended 08:40 (his message" in box.out("status", now="2026-10-31 08:45")
    box.clear_journal()
    voice = {"id": "v1", "at": "2026-10-31T07:50:00.000Z", "kind": "instant", "owner": False,
             "meta": {"chat_id": "voice", "via": "voice", "ts": "2026-10-31T07:50:00.000Z"}}  # fmt: skip
    box.jr.write_text(json.dumps(voice) + "\n")
    assert box.act("2026-10-31 08:55") == 1  # his live voice via the relay (08:50) ends it too
    assert box.act("2026-10-31 08:45") == 0  # before his voice event


# ---------------------------------------------------------------- place rule (remind's place-state.json)
def test_place_rule(box):
    box.place(
        {"zone": "home", "since": "2026-10-28 02:30", "trip": {"left": "2026-10-27 20:00", "back": "2026-10-28 02:30"}}
    )
    assert box.act("2026-10-28 09:00") == 0  # home at 02:30 after a night out
    box.journal("2026-10-28T05:05:00.000Z")  # 06:05 local, after home + 60 min
    assert box.act("2026-10-28 06:10") == 1
    box.clear_journal()
    box.place(
        {"zone": "home", "since": "2026-10-28 07:45", "trip": {"left": "2026-10-28 07:00", "back": "2026-10-28 07:45"}}
    )
    assert box.act("2026-10-28 09:00") == 1  # morning errand: no guard
    box.place({"zone": "away", "since": "2026-10-23 10:00", "left": "2026-10-23 10:00"})
    assert box.act("2026-10-23 09:59") == 0  # late night, still home
    assert box.act("2026-10-23 10:01") == 1  # left home 10:00
    box.place({"zone": "away", "since": "2026-10-30 19:00", "left": "2026-10-30 16:40"})
    assert box.act("2026-10-31 07:00") == 0  # still out (left the evening before)


# ---------------------------------------------------------------- hold / release
def test_hold_and_release(box):
    box.place({"zone": "away", "since": "2026-10-30 19:00", "left": "2026-10-30 16:40"})
    assert box.out("hold", "--reason", "test", now="2026-10-26 20:00") == "hold: Tue 27.10 06:00–12:30"
    assert box.act("2026-10-27 09:00") == 0
    assert oct((box.st / "holds.json").stat().st_mode & 0o777) == "0o600"
    box.run("hold", "2026-10-29", "--until", "10:00", now="2026-10-28 20:00")
    assert box.act("2026-10-29 09:59") == 0
    assert box.act("2026-10-29 10:00") == 1
    assert box.out("release", "2026-10-25", now="2026-10-24 20:00") == "released Sun 25.10: no guard that morning"
    assert box.act("2026-10-25 09:00") == 1
    assert "Sun 25.10 off" in box.out("status", now="2026-10-24 20:00")
    assert box.run("hold", "2026-13-01").returncode == 2  # bad date: usage error


# ---------------------------------------------------------------- gate / soft texts / flush
def test_gate_queue_flush(box):
    box.place({"zone": "home", "since": "2026-10-20 18:00"})
    assert (box.soft("plain now", "2026-10-22 09:00"), box.sent()) == (0, 1)  # no guard: runs now
    assert box.run("gate", "--", "sh", "-c", "exit 3", now="2026-10-22 09:00").returncode == 3
    assert box.run("gate", "--", "/nonexistent/x", now="2026-10-22 09:00").returncode == 127
    assert (box.soft("deal A", "2026-10-23 08:53"), box.sent()) == (0, 1)  # guarded morning: queued, exit 0
    box.run("gate", "--tag", "radar", "--", "tg", "report B", now="2026-10-23 09:23")
    assert box.qlen() == 2
    assert oct((box.st / "queue.json").stat().st_mode & 0o777) == "0o600"
    assert "2 waiting" in box.out("status", now="2026-10-23 09:30")
    box.run("flush", now="2026-10-23 09:30")
    assert (box.sent(), box.qlen()) == (1, 2)  # flush while it holds: nothing sent
    box.run("flush", now="2026-10-23 12:30")
    assert box.tg_lines()[1:3] == ["deal A", "report B"]  # both sent, in order
    assert box.qlen() == 0
    box.run("flush", now="2026-10-23 12:35")
    assert box.sent() == 3  # second flush: no duplicate
    assert box.holds()["2026-10-23"]["ended"]["why"] == "time"
    assert box.act("2026-10-23 09:00") == 0  # a recorded end does not leak into the morning itself

    box.soft("news C", "2026-10-31 09:00")
    out = box.out("release", "--reason", "he is up", now="2026-10-31 09:10")
    assert (out, box.tg_lines()[-1]) == ("released Sat 31.10: sent 1", "news C")
    assert box.act("2026-10-31 09:15") == 1

    box.soft("ping D", "2026-11-09 09:00")
    box.journal("2026-11-09T08:30:00.000Z")  # 09:30 local, after the landing + 60 min
    box.run("flush", now="2026-11-09 09:35")
    assert (box.tg_lines()[-1], box.qlen()) == ("ping D", 0)  # his message: the next flush sends
    e = box.holds()["2026-11-09"]["ended"]
    assert (e["why"], e["at"]) == ("his message", "2026-11-09 09:30")
    box.clear_journal()

    (box.t / "tg-fail").touch()
    box.soft("fails E", "2026-11-09 08:00")
    assert box.qlen() == 1  # queued while it holds
    for m in (40, 45, 50):
        box.run("flush", now=f"2026-11-09 12:{m}")
    notes = sorted((box.home / "inbox").glob("sleepin-failed-*"))
    assert (len(notes), box.qlen()) == (1, 0)  # 3 failed sends: one inbox note, queue empty
    assert "fails E" in notes[0].read_text()
    (box.t / "tg-fail").unlink()
    d = json.loads(box.out("status", "--json", now="2026-10-23 09:00"))
    assert (d["active"], d["until"]) == (True, "12:30")


# ---------------------------------------------------------------- remind: my nudges wait, his reminders don't
def test_remind_holds_my_nudges(box):
    rt = box.t / "remind"
    rt.mkdir()
    log, glog, asleep = rt / "tg.log", rt / "guard.log", rt / "asleep"
    sh(rt / "tg", f'printf \'%s\\n\' "$*" >> "{log}"\n')
    sh(rt / "guard", f'printf \'%s\\n\' "$*" >> "{glog}"\n[ -e "{asleep}" ]\n')
    renv = dict(
        REMIND_DB=str(rt / "rem.json"),
        REMIND_INBOX=str(rt / "inbox"),
        REMIND_TG=str(rt / "tg"),
        REMIND_SLEEPIN_CMD=str(rt / "guard"),
        REMIND_PLACE_CMD=f"cat {rt / 'here.json'}",
    )

    def r(now, *args):
        return box.run(*args, prog="remind", REMIND_NOW=now, **renv)

    def lines():
        return log.read_text().splitlines() if log.exists() else []

    for args in (
        ("--owner", "2026-10-23 09:00", "his own A"),
        ("--owner", "--tag", "bill:1", "2026-10-23 09:00", "nudge B"),
        ("--owner", "--soft", "2026-10-23 09:00", "soft C"),
        ("--owner", "--tag", "vp:x1", "2026-10-23 09:00", "voice D"),
        ("2026-10-23 09:00", "for me E"),
        ("--owner", "--tag", "deadline:x", "--every", "daily", "2026-10-23 09:00", "series F"),
        ("--call", "2026-10-23 09:00", "call G"),
    ):
        assert r("2026-10-23 08:00", "add", *args).returncode == 0
    asleep.touch()
    r("2026-10-23 09:00", "due")
    text = "\n".join(lines())
    for want in ("his own A", "voice D", "call G"):  # his own, voice-plan and call reminders go out
        assert want in text
    for held in ("nudge B", "soft C", "series F"):  # my nudges wait (tagged, --soft, tagged series)
        assert held not in text
    assert sum(f.read_text().count("for me E") for f in (rt / "inbox").iterdir()) == 1  # inbox note as usual
    assert set(glog.read_text().splitlines()) == {"--at 2026-10-23 09:00"}  # the guard asked with the fake clock
    lst = r("2026-10-23 09:00", "list").stdout.splitlines()
    assert sum(any(w in ln for w in ("nudge B", "soft C", "series F")) for ln in lst) == 3  # still in the store
    c0 = len(lines())
    r("2026-10-23 09:01", "due")
    assert len(lines()) == c0  # still asleep 09:01: nothing more
    asleep.unlink()
    r("2026-10-23 12:30", "due")
    text = "\n".join(lines())
    for want in ("nudge B", "soft C", "series F"):
        assert want in text
    assert "2026-10-24 09:00  [owner] series F" in r("2026-10-23 12:30", "list").stdout  # next slot
    assert len(lines()) == len(set(lines()))  # nothing sent twice

    # place-due: a location reminder held for quiet hours stays held on a sleep-in morning
    log.write_text("")
    here = rt / "here.json"
    here.write_text(
        json.dumps({"place": {"kind": "away", "zone": None, "band": "near", "town": "Testtown", "flag": "away"}})
    )
    r("2026-10-24 20:00", "place-due")
    r("2026-10-24 20:11", "place-due")
    r("2026-10-24 20:12", "add", "--on", "home", "place P")
    r("2026-10-24 20:14", "place-due")
    here.write_text(json.dumps({"place": {"kind": "home", "zone": "Home", "band": None, "town": None, "flag": "home"}}))
    r("2026-10-25 02:10", "place-due")
    assert lines() == []  # arrival 02:10: held (quiet hours)
    asleep.touch()
    r("2026-10-25 08:00", "place-due")
    assert lines() == []  # 08:00 on a sleep-in morning: still held
    asleep.unlink()
    r("2026-10-25 12:30", "place-due")
    assert "place P" in "\n".join(lines())  # guard over: the held location reminder goes out


# ---------------------------------------------------------------- trip clock: the guard follows his zone
def test_trip_clock(box):
    """A night out abroad (UTC+4) ending 02:00 there = 23:00 at home: a late night only on his clock."""
    box.place({"zone": "home", "since": "2026-10-20 18:00"})
    d = json.loads(box.cal.read_text())
    d["events"].append(dict(id="t1", start="2027-01-20T20:00:00+04:00", end="2027-01-21T02:00:00+04:00",
                            all_day=False, title="Night out abroad", flags={}))  # fmt: skip
    box.cal.write_text(json.dumps(d))
    tc = box.home / ".config/tripclock"
    tc.mkdir(parents=True)
    trips = tc / "trips.json"
    trips.write_text(
        json.dumps({"trips": [{"name": "t", "tz": "Asia/Dubai", "from": "2027-01-15", "to": "2027-01-30"}]})
    )
    assert box.act("2027-01-21 02:59") == 1  # 05:59 his time: send
    assert box.act("2027-01-21 03:30") == 0  # 06:30 his time: hold
    assert box.act("2027-01-21 09:29") == 0  # 12:29 his time: hold
    assert box.act("2027-01-21 09:30") == 1  # 12:30 his time: send
    assert "HOLD until 12:30" in box.out("active", "-v", "--at", "2027-01-21 05:00")
    trips.rename(tc / "trips.off")
    assert box.act("2027-01-21 05:00") == 1  # no trip: the same night ends 23:00 at home, no guard


# ---------------------------------------------------------------- the hub's Telegram rows instead of a journal
HUB_ROWS = {
    "tg.message": [
        {"id": 7, "payload": json.dumps({"user_id": 42, "chat_id": 42, "message_id": 5, "text": "x",
                                         "event_type": "message", "ts": "2026-10-31T07:40:00.000Z"})},
        {"id": 8, "payload": json.dumps({"user_id": 99, "chat_id": 99, "message_id": 6,
                                         "ts": "2026-10-31T07:30:00.000Z"})},
    ]
}  # fmt: skip


@pytest.fixture
def hub():
    """A fake hub (POST /tg/q kind_payloads) on 127.0.0.1; records each call."""
    calls = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            b = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            calls.append({"path": self.path, "body": b})
            kind = (b.get("params") or {}).get("kind")
            out = json.dumps({"rows": HUB_ROWS.get(kind, []), "truncated": False}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", calls
    srv.shutdown()
    srv.server_close()


def test_hub_rows_instead_of_journal(box, hub):
    url, calls = hub
    box.place({"zone": "home", "since": "2026-10-20 18:00"})
    (box.jr.parent / "access.json").write_text(json.dumps({"allowFrom": ["42"]}))  # the owner: first allowFrom
    (box.home / ".config/assistant").mkdir(parents=True)
    tok = box.home / ".secrets/hub-token"
    tok.parent.mkdir()
    tok.write_text("t" * 40)
    tok.chmod(0o600)
    box.clear_journal()
    box.env["HUB_URL"] = url
    assert box.act("2026-10-22 09:00") == 1  # an unguarded morning
    assert box.act("2026-10-31 07:00") == 0  # a guarded morning before its floor (08:30)
    assert calls == []  # neither asks the hub
    assert box.act("2026-10-31 08:45") == 1  # his 08:40 hub message ends it
    assert box.act("2026-10-31 08:35") == 0  # before it (the 08:30 row is not his)
    assert '"name": "kind_payloads"' in json.dumps(calls[0])  # reads kind_payloads
    assert box.act("2026-10-31 08:45", SLEEPIN_HUB="off") == 0
    assert box.act("2026-10-31 08:45", SLEEPIN_JOURNAL=str(box.jr)) == 0  # a journal set: no hub
    assert box.act("2026-10-31 08:45", SLEEPIN_HUB="on", SLEEPIN_JOURNAL=str(box.jr)) == 1
    assert box.act("2026-10-31 08:45", HUB_URL="http://127.0.0.1:9") == 0  # hub down: the file alone, no crash
    assert "hub journal: HubDown" in (box.st / "sleepin.log").read_text().splitlines()[-1]
