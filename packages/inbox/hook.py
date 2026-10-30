"""inbox.hook: the UserPromptSubmit agenda hook (hooks/agenda.sh -> `events agenda --hook`). FAILS OPEN.

On a heartbeat prompt in the main session it prints {"hookSpecificOutput": {"additionalContext": <agenda>}}, marks
the listed events and writes a beats row; on anything else it prints nothing. On a Telegram channel prompt in the main
session it records delivery receipts (inbox.receipts: event_id tags -> deliveries + ack by channel; untagged 0.1.22
prompts -> the receipt baseline) and prints nothing. Any error, a locked or missing store,
or a slow source -> no output, exit 0 (the beat goes on as today), plus a failure line in the hook log and, when the
store takes it, a beats row with ok=0.

Beat detection (as traces/memory2): "[heartbeat]" or "The heartbeat plugin" within the first 300 characters.
Source: restart ("Just restarted"), notes ("N new note(s)" with N > 0), else idle.
Switches: ~/.config/assistant/agenda-off (and events-off). Main session only: the prompt's cwd is ~/workspace
($ASSISTANT_MAIN_CWD), or its transcript lies in ~/workspace's project folder (sources.project_dir()): the session's
cwd follows every `cd` in its Bash, so a beat often arrives with cwd ~/inbox or ~. Log:
~/.local/state/assistant/agenda-hook.log (trimmed at 256 KB).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime
from typing import IO, Any

from core import clock, db, events, hub, paths, todo

from . import agenda, kinds, receipts, sources

__all__ = ["beat_source", "hook_main", "render"]

HEAD = 300
NOTES_RE = re.compile(r"(\d+) new note")
LOG_MAX = 256 * 1024
SNAPSHOT_MAX_AGE = 600


def beat_source(prompt: str) -> str | None:
    head = (prompt or "")[:HEAD]
    if "[heartbeat]" not in head and "The heartbeat plugin" not in head:
        return None
    lead = (prompt or "")[:600]
    if "Just restarted" in lead:
        return "restart"
    m = NOTES_RE.search(lead)
    return "notes" if m and int(m.group(1)) > 0 else "idle"


def agenda_off() -> bool:
    return os.path.exists(paths.config("agenda-off")) or events.off()


def _main_cwd_ok(cwd: str | None) -> bool:
    want = os.environ.get("ASSISTANT_MAIN_CWD") or os.fspath(paths.home() / "workspace")
    return bool(cwd) and os.path.realpath(cwd) == os.path.realpath(want)


def _main_session_ok(data: dict[str, Any]) -> bool:
    """cwd ~/workspace, or a transcript in ~/workspace's project folder: the same sessions, also after a `cd`."""
    if _main_cwd_ok(data.get("cwd")):
        return True
    tp = data.get("transcript_path")
    if not isinstance(tp, str) or not tp:
        return False
    try:
        return os.path.realpath(os.path.dirname(tp)) == os.path.realpath(sources.project_dir())
    except Exception:
        return False


def log(status: str, msg: str) -> None:
    try:
        p = os.fspath(paths.state("agenda-hook.log"))
        events.append_private(
            p, f"{datetime.fromtimestamp(clock.now(), todo.owner_tz()):%Y-%m-%d %H:%M:%S} {status} {msg}"
        )
        if os.path.getsize(p) > LOG_MAX:
            with open(p, encoding="utf-8") as f:
                keep = f.readlines()[-1000:]
            with open(p + ".tmp", "w", encoding="utf-8") as f:
                f.writelines(keep)
            os.replace(p + ".tmp", p)
    except OSError:
        pass


def running_info(con: sqlite3.Connection | None, now: float, timeout: float) -> dict[str, Any]:
    run = sources.running(now, timeout)
    if run["agents"] is None and con is not None:
        snap = db.meta_get(con, "running")
        if isinstance(snap, dict) and now - float(snap.get("ts") or 0) <= SNAPSHOT_MAX_AGE:
            run["agents"] = snap.get("agents")
            run["stale_from"] = f"{datetime.fromtimestamp(float(snap['ts']), todo.owner_tz()):%H:%M}"
    return run


def render(
    con: sqlite3.Connection,
    now: float,
    source: str,
    *,
    write: bool,
    run: dict[str, Any] | None = None,
    board: todo.Board | None = None,
    t0: float | None = None,
) -> tuple[agenda.Agenda, int]:
    """Build the agenda. write=True: one transaction that adds the beats row, marks the listed events, finishes
    the row. write=False: a preview (beat id = the next one), nothing written."""
    tz = todo.owner_tz()
    reg = kinds.load()
    board = board if board is not None else todo.read(now=datetime.fromtimestamp(now, tz), tz=tz)
    comp = sources.last_compaction(con)

    def _build(beat_id: int, last_ts: float | None) -> agenda.Agenda:
        full = source == "restart" or bool(comp and last_ts and comp["ts"] > last_ts)
        try:
            rfx = sources.reflex_counts(last_ts, now)
        except Exception:  # fail open: the counter is optional
            rfx = None
        return agenda.build(
            con,
            now_ts=now,
            beat_id=beat_id,
            last_beat_ts=last_ts,
            reg=reg,
            tz=tz,
            board=board,
            run=run,
            comp=comp,
            full_open=full,
            reflexes=rfx,
        )

    if not write:
        nxt = con.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM beats").fetchone()[0]
        last = con.execute("SELECT MAX(ts) FROM beats WHERE ok=1").fetchone()[0]
        return _build(int(nxt), last), int(nxt)
    with db.tx(con):
        last = con.execute("SELECT MAX(ts) FROM beats WHERE ok=1").fetchone()[0]
        beat_id = events.begin_beat(con, now, source)
        ag = _build(beat_id, last)
        events.mark_listed(con, ag.listed, beat_id, now)
        ms = int((time.monotonic() - (t0 or time.monotonic())) * 1000)
        events.finish_beat(con, beat_id, ok=True, ms=ms, chars=len(ag.text), listed=ag.listed)
    return ag, beat_id


def _receipts(prompt: str, t0: float) -> None:
    """Receipts for a channel prompt; never raises, never prints (a failure is one hook-log line)."""
    try:
        res = receipts.record(prompt)
        if res["receipts"] or res["baseline"] or res["skipped"]:
            ids = ",".join(str(i) for i in res["receipts"]) or "-"
            log(
                "receipt",
                f"ids={ids} baseline={res['baseline']} skipped={res['skipped']}"
                f" ms={int((time.monotonic() - t0) * 1000)}",
            )
    except Exception as e:  # fail open
        log("receipt-fail", f"ms={int((time.monotonic() - t0) * 1000)} {type(e).__name__}: {e}"[:200])


def hook_main(stdin: IO[str] | None = None, stdout: IO[str] | None = None) -> int:
    t0 = time.monotonic()
    out = stdout or sys.stdout
    source = None
    con = None
    try:
        if agenda_off():
            return 0
        data = json.loads((stdin or sys.stdin).read() or "{}")
        prompt = str(data.get("prompt") or "")
        if "<channel" in prompt[:4000] and _main_session_ok(data):
            _receipts(prompt, t0)
        source = beat_source(prompt)
        if source is None or not _main_session_ok(data):
            return 0
        if os.environ.get("EVENTS_HOOK_TEST_SLEEP"):  # tests: a slow hook must be cut by the wrapper
            time.sleep(float(os.environ["EVENTS_HOOK_TEST_SLEEP"]))
        now = clock.now()
        if hub.backend("events") == "hub":  # the agenda on a unit of work over the hub
            cache: dict[str, Any] = {}

            def _beat(c: sqlite3.Connection) -> tuple[agenda.Agenda, int]:
                if "run" not in cache:
                    cache["run"] = running_info(c, now, timeout=0.5)
                return render(c, now, source, write=True, run=cache["run"], t0=t0)

            ag, beat_id = hub.unit(_beat, profile="hook", reserve={"beats": 1}, timeout=0.6, tries=2)
        else:
            con = db.connect(timeout=0.5)
            run = running_info(con, now, timeout=0.5)
            ag, beat_id = render(con, now, source, write=True, run=run, t0=t0)
        out.write(
            json.dumps(
                {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": ag.text}},
                ensure_ascii=False,
            )
        )
        out.flush()
        log(
            "ok",
            f"beat={beat_id} source={source} ms={int((time.monotonic() - t0) * 1000)} chars={len(ag.text)}"
            f" listed={len(ag.listed)}",
        )
        return 0
    except Exception as e:  # fail open: never block or break a beat
        msg = f"{type(e).__name__}: {e}"[:200]
        log("fail", f"source={source} ms={int((time.monotonic() - t0) * 1000)} {msg}")
        if source is not None and hub.backend("events") != "hub":  # hub: the log line is the record
            try:
                c2 = db.connect(timeout=0.2)
                with db.tx(c2):
                    bid = events.begin_beat(c2, clock.now(), source)
                    events.finish_beat(c2, bid, ok=False, ms=int((time.monotonic() - t0) * 1000), chars=0, listed=[])
                c2.close()
            except Exception:
                pass
        return 0
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
