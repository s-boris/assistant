"""brain-watch: live (no switch), --dry-run, the kill switch; stall/api_error on
events.db facts, session/bridge/agents/fallback watches, the bridge's redelivery request and the notice path (outbox,
push, direct)."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from inbox_testlib import PY, REPO

from core import db, locks

BW = REPO / "scripts" / "brain-watch"
T0 = 1949576400.0  # 2031-10-12 15:00 Zurich (afternoon)
MIN = 60
SAFEGUARDS = "API Error: Opus 5's safeguards flagged this message. This sometimes happens with safe conversations."
ZURICH = ZoneInfo("Europe/Zurich")


class Env:
    def __init__(self, home: Path) -> None:
        self.home = home
        self.now = T0
        self.pd = Path(os.environ["ASSISTANT_PROJECT_DIR"])
        self.transcript = self.pd / "sess-1.jsonl"
        self.transcript.write_text("", encoding="utf-8")
        self.agents = home / "agents.json"
        self.lease = home / "lease.json"
        self.start = home / "start-claude"
        self.start.mkdir()
        self.quiet = ""
        self.dry_state = True  # dry runs keep their own state (BW_DRY_STATE) so a sequence plays out
        self.con = db.connect()
        self.session("idle")
        self.bridge(redeliver=True)
        for name in ("phone", "tgdirect", "restart"):
            p = home / f"stub-{name}"
            p.write_text(
                f'#!/bin/sh\necho "$*" >> "{home}/{name}.log"\n'
                f'exit "$(cat "{home}/{name}.rc" 2>/dev/null || echo 0)"\n',
                encoding="utf-8",
            )
            p.chmod(0o755)

    def session(self, status: str = "idle", started_ago: float = 3600) -> None:
        a = {
            "pid": 4242,
            "cwd": str(self.home / "workspace"),
            "kind": "interactive",
            "startedAt": int((self.now - started_ago) * 1000),
            "sessionId": "sess-1",
            "status": status,
        }
        self.agents.write_text(json.dumps([a]), encoding="utf-8")

    def absent(self) -> None:
        self.agents.write_text("[]", encoding="utf-8")

    def bridge(self, *, redeliver: bool = True, pid: int | None = None) -> None:
        d: dict[str, Any] = {"version": "0.3.0", "mode": "bridge", "bot": "live", "pid": pid or os.getpid()}
        if redeliver:
            d["redeliver"] = 1
        self.lease.write_text(json.dumps(d), encoding="utf-8")

    def row(self, ago: float, kind: str = "tg.message", mid: int = 700) -> int:
        with db.tx(self.con):
            cur = self.con.execute(
                "INSERT INTO events(ts, source, kind, subject, payload, delivery)"
                " VALUES(?, 'tg-adapter', ?, 'owner says', ?, 'now')",
                (self.now - ago, kind, json.dumps({"message_id": mid})),
            )
        return int(cur.lastrowid or 0)

    def receipt(self, eid: int, how: str = "channel") -> None:
        with db.tx(self.con):
            self.con.execute(
                "INSERT INTO deliveries(event_id, receipt_ts, how) VALUES(?, ?, ?)", (eid, self.now - 3 * MIN, how)
            )
            self.con.execute("UPDATE events SET status='acked', ack_by='channel' WHERE id=?", (eid,))

    def reply(self, eid: int) -> None:
        with db.tx(self.con):
            self.con.execute(
                "INSERT INTO outbox(ts, bot, chat, op, body, reply_to_event, status)"
                " VALUES(?, 'b', 'c', 'send', 'ok', ?, 'sent')",
                (self.now, eid),
            )

    def seen(self, eid: int, kind: str = "user") -> None:
        tag = (
            f'<channel source="plugin:telegram-plus:telegram" message_id="7" event_id="{eid}">\nowner says\n</channel>'
        )
        line = (
            {"type": kind, "message": {"role": "user", "content": tag}}
            if kind == "user"
            else {"type": kind, "attachment": {"content": tag}}
        )
        with self.transcript.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, separators=(",", ":")) + "\n")

    def api_error(self, text: str, uid: str, synthetic: bool = True, **extra: Any) -> None:
        line = {
            "type": "assistant",
            "uuid": uid,
            "isApiErrorMessage": synthetic,
            "message": {
                "model": "<synthetic>" if synthetic else "claude-opus-5-5",
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
                **extra,
            },
        }
        with self.transcript.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, separators=(",", ":")) + "\n")

    def switch(self, name: str, on: bool = True) -> None:
        p = self.home / ".config/assistant" / name
        if on:
            p.touch()
        else:
            p.unlink(missing_ok=True)

    def mode(self, mode: str, log: list[tuple[float, str]] | None = None, kill_switch: bool | None = None) -> None:
        (self.start / "telegram-mode").write_text(mode + "\n", encoding="utf-8")
        ks = self.home / ".telegram-official"
        if kill_switch if kill_switch is not None else mode == "official":
            ks.touch()
        else:
            ks.unlink(missing_ok=True)
        if log is not None:
            lines = [datetime.fromtimestamp(t, ZURICH).strftime("%Y-%m-%d %H:%M:%S") + " " + s for t, s in log]
            (self.start / "bridge.log").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def rc(self, name: str, code: int) -> None:
        (self.home / f"{name}.rc").write_text(str(code), encoding="utf-8")

    def calls(self, name: str) -> list[str]:
        p = self.home / f"{name}.log"
        return p.read_text(encoding="utf-8").splitlines() if p.exists() else []

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "TZ": "Europe/Zurich",
            "ASSISTANT_NOW": str(self.now),
            "BW_AGENTS_JSON": str(self.agents),
            "BW_WORKSPACE": str(self.home / "workspace"),
            "BW_BRIDGE_LEASE": str(self.lease),
            "BW_START_STATE": str(self.start),
            "BW_PHONE_BIN": str(self.home / "stub-phone"),
            "BW_TG_DIRECT": str(self.home / "stub-tgdirect"),
            "BW_RESTART_BIN": str(self.home / "stub-restart"),
            "BW_QUIET": self.quiet,
            "BW_OUTBOX_WAIT": "0.3",
            **({"BW_DRY_STATE": str(self.home / "dry-state.json")} if self.dry_state else {}),
        }
        return subprocess.run([PY, "-I", str(BW), *args], capture_output=True, text=True, env=env, timeout=60)

    def at(self, t: float, *args: str) -> list[dict[str, Any]]:
        self.now = t
        r = self.run(*(args or ("--dry-run",)), "--json")
        assert r.returncode == 0, r.stderr
        return json.loads(r.stdout)["acts"]

    def live(self, t: float) -> list[dict[str, Any]]:
        self.now = t
        r = self.run("--json")
        assert r.returncode == 0, r.stderr
        out = json.loads(r.stdout)
        assert out["mode"] == "live"
        return out["acts"]

    def meta(self, k: str) -> Any:
        r = self.con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return json.loads(r[0]) if r else None


def acts(xs: list[dict[str, Any]], what: str) -> list[dict[str, Any]]:
    return [a for a in xs if a["act"] == what]


def texts(xs: list[dict[str, Any]]) -> list[str]:
    return [a["text"] for a in acts(xs, "notify")]


@pytest.fixture
def env(home: Path) -> Iterator[Env]:
    e = Env(home)
    yield e
    e.con.close()


# ------------------------------------------------------------------------------------------------ switches


def test_live_by_default_dry_run_saves_nothing_and_off(env: Env) -> None:
    env.dry_state = False  # a plain dry run, as by hand: no state of its own
    env.row(4 * MIN)
    r = env.run("--dry-run", "--json")
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert out["mode"] == "dry-run" and acts(out["acts"], "redeliver")
    assert json.loads(env.run("--shadow", "--json").stdout)["mode"] == "dry-run"
    st = env.home / ".local/state/assistant"
    assert not (st / "brain-watch.json").exists() and not (st / "brain-watch-shadow.json").exists()
    assert "DRY-RUN would redeliver" in (st / "brain-watch.log").read_text(encoding="utf-8")
    assert env.meta("bridge.redeliver") is None  # nothing done
    env.switch("brain-watch-live")  # a leftover switch file changes nothing
    r = env.run("--json")
    assert r.returncode == 0 and json.loads(r.stdout)["mode"] == "live" and (st / "brain-watch.json").exists()
    assert env.meta("bridge.redeliver")["why"] == "stalled"
    assert not (st / "brain-watch-shadow.json").exists()
    env.switch("brain-watch-off")
    assert env.run("--json").stdout == "" and env.run("--dry-run").stdout == ""
    s = env.run("--status")
    assert s.returncode == 0 and s.stdout.startswith("mode: off")


def test_healthy_nothing_to_do(env: Env) -> None:
    e = env.row(10 * MIN)
    env.receipt(e)
    env.reply(e)
    assert env.at(T0) == []
    assert env.at(T0 + 30 * MIN) == []
    r = env.run("--status")
    assert r.returncode == 0 and r.stdout.startswith("mode: live")
    assert "without a receipt ≥ 3 min: 0" in r.stdout and "session: idle" in r.stdout and "(redeliver)" in r.stdout
    st = json.loads(env.run("--status", "--json").stdout)
    assert st["bridge"]["redeliver"] == 1 and st["telegram_mode"] == "none" and st["incidents"] == {}


def test_lock_busy_run_exits_quietly(env: Env) -> None:
    env.row(4 * MIN)
    fd = locks.acquire(env.home / ".local/state/assistant/brain-watch.lock", wait=0)
    try:
        r = env.run("--dry-run")
        assert (r.returncode, r.stdout) == (0, "")
    finally:
        locks.release(fd)
    assert acts(env.at(T0), "redeliver")


# ------------------------------------------------------------------------------------------------ stall (dry run)


def test_stall_idle_redeliver_then_notice_then_resolved(env: Env) -> None:
    e = env.row(4 * MIN)
    a = env.at(T0)
    assert acts(a, "redeliver") == [{"act": "redeliver", "ids": [e], "why": "stalled"}]
    assert not acts(a, "notify") and not acts(a, "restart")
    assert env.at(T0 + 2 * MIN) == []  # within 3 min of the re-send: wait
    a = env.at(T0 + 4 * MIN)
    (t,) = texts(a)
    assert "hasn't reached my session (re-sent (dry run: assumed))" in t and "Remote Control" in t
    assert acts(a, "notify")[0]["urgent"] is True
    assert acts(a, "redeliver") == [{"act": "redeliver", "ids": [e], "why": "stalled, again"}]
    assert acts(a, "decision") == [{"act": "decision", "incident": "stall", "result": "no restart: switch off"}]
    assert not acts(a, "restart")
    env.seen(e)  # the session saw it: the transcript counts as the receipt
    a = env.at(T0 + 6 * MIN)
    assert acts(a, "receipt") == [{"act": "receipt", "ids": [e], "how": "transcript"}]
    assert acts(a, "resolve") and "Back to normal" in texts(a)[0]
    log = (env.home / ".local/state/assistant/brain-watch.log").read_text(encoding="utf-8")
    assert "DRY-RUN would redeliver" in log and "DRY-RUN would notify" in log and "owner says" not in log


def test_stall_asks_at_most_three_times(env: Env) -> None:
    e = env.row(4 * MIN)
    whys = []
    for m in range(0, 40, 4):
        whys += [x["why"] for x in acts(env.at(T0 + m * MIN), "redeliver")]
    assert whys == ["stalled", "stalled, again", "stalled, again"]
    assert e


def test_transcript_checked_first(env: Env) -> None:
    e = env.row(5 * MIN)
    env.seen(e, kind="attachment")
    a = env.at(T0)
    assert [x["act"] for x in a] == ["receipt"]
    assert env.at(T0 + MIN) == []  # noted once


def test_young_rows_and_starting_sessions_wait(env: Env) -> None:
    env.row(2 * MIN)
    assert env.at(T0) == []
    env.row(5 * MIN, mid=701)
    env.session("idle", started_ago=60)
    assert env.at(T0) == []


def test_busy_session_long_wait_notice_once_never_restart(env: Env) -> None:
    env.switch("brain-watch-restart")
    env.session("busy")
    env.row(4 * MIN)
    assert env.at(T0) == []
    a = env.at(T0 + 7 * MIN)
    assert len(texts(a)) == 1 and "still waiting" in texts(a)[0] and not acts(a, "restart")
    assert env.at(T0 + 8 * MIN) == []


def test_dialog_waiting_long_wait_notice(env: Env) -> None:
    env.session("waiting")
    env.row(11 * MIN)
    (t,) = texts(env.at(T0))
    assert "stuck on a dialog" in t


# ------------------------------------------------------------------------------------------------ api errors


def test_api_error_redeliver_once_then_tell_once(env: Env) -> None:
    e = env.row(4 * MIN)
    env.receipt(e)
    env.seen(e)
    env.api_error(SAFEGUARDS, "err-1")
    a = env.at(T0)
    assert acts(a, "redeliver") == [{"act": "redeliver", "ids": [e], "why": "API error #101715"}]
    assert not acts(a, "notify")
    assert env.at(T0 + MIN) == []  # the same error entry: no second re-send
    env.seen(e)
    env.api_error(SAFEGUARDS, "err-2")
    a = env.at(T0 + 2 * MIN)
    n = texts(a)
    assert len(n) == 1 and "failed twice" in n[0] and "safety filter" in n[0] and not acts(a, "restart")
    env.api_error(SAFEGUARDS, "err-3")
    assert env.at(T0 + 12 * MIN) == []  # told only once


def test_api_error_cases_that_do_nothing(env: Env) -> None:
    before = env.row(4 * MIN)  # an error from before the message
    env.api_error(SAFEGUARDS, "old")
    env.receipt(before)
    env.seen(before)
    talk = env.row(4 * MIN, mid=701)  # the model merely talking about the errors
    env.receipt(talk)
    env.seen(talk)
    env.api_error('I added "safeguards flagged" to the matcher.', "m-1", synthetic=False)
    assert env.at(T0) == []


def test_api_error_but_answered(env: Env) -> None:
    answered = env.row(4 * MIN, mid=702)  # answered after the error (a reply row for it in the outbox)
    env.receipt(answered)
    env.seen(answered)
    env.api_error(SAFEGUARDS, "err-x")
    env.reply(answered)
    assert env.at(T0) == []


def test_api_error_refusal_and_busy(env: Env) -> None:
    e = env.row(4 * MIN)
    env.receipt(e)
    env.seen(e)
    env.api_error("", "msg-1", synthetic=False, stop_reason="refusal")
    env.session("busy")
    assert env.at(T0) == []  # busy: already on something new
    env.session("idle")
    assert acts(env.at(T0 + MIN), "redeliver")


# ------------------------------------------------------------------------------------------------ restarts


def test_restart_only_with_its_switch_and_within_limits(env: Env) -> None:
    env.switch("brain-watch-restart")
    env.row(4 * MIN)
    env.at(T0)
    a = env.at(T0 + 4 * MIN)
    assert len(acts(a, "restart")) == 1 and "restarting my session" in texts(a)[0]
    a = env.at(T0 + 10 * MIN)
    assert not acts(a, "restart")  # ≤ 1 per 15 min
    assert len(acts(env.at(T0 + 20 * MIN), "restart")) == 1
    a = env.at(T0 + 40 * MIN)  # a third would exceed 2 per incident
    assert not acts(a, "restart") and any("needs you" in t for t in texts(a))
    assert not any("needs you" in t for t in texts(env.at(T0 + 60 * MIN)))


def test_live_restart_runs_the_restart_command(env: Env) -> None:
    env.switch("brain-watch-restart")
    env.absent()
    env.live(T0)
    a = env.live(T0 + 6 * MIN)
    assert acts(a, "restart")[0]["rc"] == 0
    assert env.calls("restart") == ["auto-recover: the main session is gone since 15:00"]


# ------------------------------------------------------------------------------------------------ a dry run never acts


def test_dry_run_never_writes_the_store_or_sends(env: Env) -> None:
    e = env.row(4 * MIN)
    env.seen(e)
    before = env.con.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]
    env.at(T0)
    env.row(4 * MIN, mid=701)
    env.absent()
    for m in (4, 9, 15):
        env.at(T0 + m * MIN)
    c = sqlite3.connect(env.home / ".local/state/assistant/events.db")
    assert c.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == before
    assert c.execute("SELECT COUNT(*) FROM meta WHERE k LIKE 'bridge.%'").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
    c.close()
    assert env.calls("phone") == env.calls("tgdirect") == env.calls("restart") == []
    assert not (env.home / ".local/state/assistant/brain-watch.json").exists()
    assert not (env.home / ".local/state/assistant/brain-watch-shadow.json").exists()


def test_missing_store_is_quiet(home: Path) -> None:
    r = subprocess.run(
        [PY, "-I", str(BW), "--dry-run"], capture_output=True, text=True, env={**os.environ, "ASSISTANT_NOW": str(T0)}
    )
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    assert not (home / ".local/state/assistant/events.db").exists()


# ------------------------------------------------------------------------------------------------ live: the bridge


def test_live_redeliver_request_then_ack_decides_the_notice(env: Env) -> None:
    e = env.row(4 * MIN)
    a = env.live(T0)
    (r,) = acts(a, "redeliver")
    req = env.meta("bridge.redeliver")
    assert r["ok"] is True and req["ids"] == [e] and req["why"] == "stalled" and req["rid"] == r["rid"]
    with db.tx(env.con):
        db.meta_set(env.con, "bridge.redeliver.ack", {"rid": req["rid"], "at": T0 + 1, "sent": [e], "skipped": {}})
    a = env.live(T0 + 4 * MIN)
    (n,) = acts(a, "notify")
    assert "re-sent, still no receipt" in n["text"] and n["via"] == "push" and n["urgent"] is True
    assert env.calls("phone")[0].startswith("push --level urgent --title Telegram watchdog --body Your 14:56 message")
    assert env.meta("bridge.redeliver")["why"] == "stalled, again"


def test_live_bridge_that_never_takes_the_request(env: Env) -> None:
    env.row(4 * MIN)
    env.live(T0)
    assert "the bridge didn't take the re-send request" in texts(env.live(T0 + 4 * MIN))[0]


def test_live_bridge_without_redelivery_says_so(env: Env) -> None:
    env.bridge(redeliver=False)
    env.row(4 * MIN)
    env.live(T0)
    assert "this bridge can't re-send" in texts(env.live(T0 + 4 * MIN))[0]


def test_live_transcript_receipt_is_written(env: Env) -> None:
    e = env.row(5 * MIN)
    env.seen(e)
    a = env.live(T0)
    assert acts(a, "receipt") == [{"act": "receipt", "ids": [e], "how": "transcript", "ok": True}]
    how = env.con.execute("SELECT how, receipt_ts FROM deliveries WHERE event_id=?", (e,)).fetchone()
    assert how[0] == "transcript" and how[1] == T0
    assert env.live(T0 + 5 * MIN) == []


# ---------------------------------------------------------------------------------------------- session, bridge, agents


def test_session_down_after_five_minutes_then_back(env: Env) -> None:
    env.absent()
    assert env.at(T0) == []
    assert env.at(T0 + 4 * MIN) == []
    a = env.at(T0 + 5 * MIN)
    (n,) = acts(a, "notify")
    assert "down since 15:00" in n["text"] and n["urgent"] is False and n["tag"] == "session_down"
    assert env.at(T0 + 6 * MIN) == []
    env.session("idle", started_ago=30)  # back (starting counts as present)
    a = env.at(T0 + 8 * MIN)
    assert acts(a, "resolve") and "Back to normal: my main session is back." in texts(a)[0]


def test_session_down_waits_out_quiet_hours_unless_he_writes(env: Env) -> None:
    env.quiet = "14-16"
    env.absent()
    env.at(T0)
    a = env.at(T0 + 6 * MIN)
    assert acts(a, "defer") and not acts(a, "notify")
    env.row(4 * MIN)  # a message of his waits now: tell him at once
    a = env.at(T0 + 7 * MIN)
    (n,) = acts(a, "notify")
    assert n["urgent"] is True and "1 message(s) of yours wait for it" in n["text"]


def test_deferred_notice_dropped_when_it_ends_in_quiet_hours(env: Env) -> None:
    env.quiet = "14-16"
    env.absent()
    env.at(T0)
    assert acts(env.at(T0 + 6 * MIN), "defer")
    env.session("idle")
    a = env.at(T0 + 9 * MIN)
    assert acts(a, "resolve") and not acts(a, "notify")


def test_bridge_down_when_the_lease_holder_is_gone(env: Env) -> None:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    env.bridge(pid=p.pid)  # a dead holder: the file outlives it
    assert env.at(T0) == []
    a = env.at(T0 + 5 * MIN)
    (n,) = acts(a, "notify")
    assert "without its Telegram bridge since 15:00" in n["text"] and n["urgent"] is False
    assert acts(a, "decision")[0]["result"] == "no restart: switch off"
    env.row(4 * MIN)  # his message can't get through while the bridge is down: no stall re-sends, the notice is enough
    assert not acts(env.at(T0 + 9 * MIN), "redeliver")
    env.bridge()
    a = env.at(T0 + 10 * MIN)
    assert acts(a, "resolve") and "bridge is back" in texts(a)[0]


def test_agents_failing_is_never_session_down(env: Env) -> None:
    env.agents.write_text("not json", encoding="utf-8")
    assert env.at(T0) == []
    assert env.at(T0 + 10 * MIN) == []  # unknown is not absent: no session_down, no bridge_down
    a = env.at(T0 + 15 * MIN)
    assert [x["tag"] for x in acts(a, "notify")] == ["agents"] and "claude agents --json" in texts(a)[0]
    assert not acts(env.at(T0 + 16 * MIN), "notify")
    env.session("idle")
    assert "sees my session again" in texts(env.at(T0 + 17 * MIN))[0]


# ------------------------------------------------------------------------------------------------ Telegram modes


def test_poll_mode_stands_down(env: Env) -> None:
    env.mode("poll")
    env.row(10 * MIN)
    assert [x["act"] for x in env.at(T0)] == ["stand_down"]
    assert env.at(T0 + MIN) == []


def test_fallback_told_once_unless_start_claude_texted(env: Env) -> None:
    env.mode("official", [(T0 - 3600, "bridge start"), (T0 - 3 * MIN, "fallback: tg-adapter live did not start")])
    env.row(10 * MIN)  # stuck from before the fallback: not a stall in official mode
    a = env.at(T0)
    (n,) = acts(a, "notify")
    assert "fell back to the official plugin at 14:57 (tg-adapter live did not start)" in n["text"]
    assert n["urgent"] is False and not acts(a, "redeliver")
    assert env.at(T0 + MIN) == []
    env.mode(
        "official", [(T0 + 2 * MIN, "fallback: the session failed to start twice"), (T0 + 2 * MIN, "fallback texted")]
    )
    assert acts(env.at(T0 + 5 * MIN), "fallback") == [{"act": "fallback", "told": "start-claude texted him"}]
    env.mode("official", [(T0 - 7200, "bridge start")])  # the kill switch by hand: not news
    assert env.at(T0 + 9 * MIN) == []


def test_fallback_seen_before_the_next_start_writes_official(env: Env) -> None:
    # two fast failures: start-claude made the kill switch after claude exited; telegram-mode still says bridge
    env.mode(
        "bridge", [(T0 - 600, "bridge start"), (T0 - 5 * MIN, "fallback: the session failed to start twice")], True
    )
    env.row(10 * MIN)
    a = env.at(T0)
    assert [x["tag"] for x in acts(a, "notify")] == ["fallback"] and not acts(a, "redeliver")
    env.mode("bridge", [(T0 - 5 * MIN, "fallback: x"), (T0 + MIN, "bridge start")], False)  # back on the bridge
    assert acts(env.at(T0 + 2 * MIN), "redeliver")


def test_fallback_waits_for_start_claudes_own_text_and_quiet_hours(env: Env) -> None:
    env.mode("official", [(T0 - 30, "fallback: no tg-adapter live config")])
    assert env.at(T0) == []  # its own text gets 2 min
    env.quiet = "14-16"
    assert acts(env.at(T0 + 3 * MIN), "defer")
    env.quiet = ""
    assert len(acts(env.at(T0 + 4 * MIN), "notify")) == 1


# ---------------------------------------------------------------------------------------------- the notice path (live)


def test_live_notice_push_then_direct_then_retries(env: Env) -> None:
    env.absent()
    env.live(T0)
    a = env.live(T0 + 5 * MIN)
    assert acts(a, "notify")[0]["via"] == "push"  # no live adapter here: the outbox is closed
    assert env.calls("tgdirect") == []
    env.session("idle")
    env.live(T0 + 6 * MIN)
    env.absent()
    env.rc("phone", 7)
    env.live(T0 + 60 * MIN)
    a = env.live(T0 + 65 * MIN)
    n = acts(a, "notify")[0]
    assert n["via"] == "direct" and n["tried"] == "outbox:direct,push:7"
    assert env.calls("tgdirect")[0].startswith("⚠️ (auto) My main session has been down since 16:00")
    env.session("idle")
    env.live(T0 + 66 * MIN)
    env.absent()
    env.rc("tgdirect", 1)
    env.live(T0 + 120 * MIN)
    assert acts(env.live(T0 + 125 * MIN), "notify")[0]["via"] == "none"
    assert acts(env.live(T0 + 126 * MIN), "notify")[0]["via"] == "none"  # tried again
    assert acts(env.live(T0 + 127 * MIN), "notify")[0]["via"] == "none"
    assert not acts(env.live(T0 + 128 * MIN), "notify")  # 3 tries in all


def test_live_notice_through_the_outbox_urgent_row_withdrawn_when_it_ends(env: Env) -> None:
    cfg = env.home / ".config/tg-adapter"
    cfg.mkdir(parents=True)
    (cfg / "live.json").write_text(json.dumps({"mode": "live", "send": True, "allow_from": ["111"]}), encoding="utf-8")
    fake = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", "tg-adapter"])
    try:
        sd = env.home / ".local/state/tg-adapter/live"
        sd.mkdir(parents=True)
        (sd / "adapter.pid").write_text(f"{fake.pid}\ntg-adapter\n", encoding="utf-8")
        env.row(4 * MIN)
        env.absent()
        env.live(T0)
        a = env.live(T0 + 5 * MIN)
        (n,) = acts(a, "notify")
        assert n["via"] == "outbox-held" and n["urgent"] is True  # nobody sends here: it stays queued
        row = env.con.execute("SELECT * FROM outbox WHERE id=?", (n["row"],)).fetchone()
        body = json.loads(row["body"])
        assert (row["op"], row["urgent"], row["chat"], body["sender"]) == ("send", 1, "111", "brain-watch")
        assert env.calls("phone") == []
        env.session("idle")
        a = env.live(T0 + 6 * MIN)
        assert acts(a, "withdraw") and not acts(a, "notify")
        assert env.con.execute("SELECT status FROM outbox WHERE id=?", (n["row"],)).fetchone()[0] == "skipped"
    finally:
        fake.kill()
        fake.wait()


def test_notice_cap(env: Env) -> None:
    env.absent()
    sent = 0
    for k in range(6):
        t = T0 + k * 40 * MIN
        env.absent()
        env.at(t)
        sent += len(acts(env.at(t + 5 * MIN), "notify"))
        env.session("idle")
        sent += len(acts(env.at(t + 6 * MIN), "notify"))
    assert sent == 6
