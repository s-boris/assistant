"""rotation.checks: is now a quiet slot for a fresh session? Read-only; every check fails closed.

Each check returns (ok, why): ok True passes, False blocks, None = could not tell (blocks too). Checks:
  switch        ~/.config/assistant/rotate-on exists (the owner's standing OK; `rotate on`)
  window        his wall clock (`tripclock tz`, else the profile zone) inside the window (default 03:00-05:00)
  nightly       no rotation in the last min_gap_h hours (default 20)
  session       the main session is idle (`claude agents --json`) and older than session_min_age_min (30)
  agents        no subagent running or recently stalled (`agent-watch status`, the hb-gate rule)
  owner_idle    his newest message (any channel) older than owner_idle_min (60)
  call          no live voice call (`voice-relay status`: live 0, no call ids; relay down = no call)
  drive         he is not driving (`driving` exit 1)
  approvals     no pending one-time approval (`approvals list --json`)
  crons         no session-only check-in (CronCreate) firing within cron_guard_min (30) of now
  restart       no restart note from the last 30 min in ~/inbox (a restart is still settling)
  start_claude  the installed start-claude has the fresh-start block (it honours the fresh-start flag)
Config: ~/.config/assistant/rotate.json {window, owner_idle_min, session_min_age_min, min_gap_h, cron_guard_min}.
Seams (tests): ROTATE_AGENTS_JSON, ROTATE_AGENT_WATCH, ROTATE_VOICE, ROTATE_DRIVING, ROTATE_APPROVALS,
ROTATE_TRIPCLOCK, ROTATE_START_CLAUDE.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from core import clock, jsonstate, paths, todo
from rotation import session

DEFAULTS: dict[str, Any] = {
    "window": "03:00-05:00",
    "owner_idle_min": 60,
    "session_min_age_min": 30,
    "min_gap_h": 20,
    "cron_guard_min": 30,
}
FRESH_MARKER = "rotate/fresh-start"  # the flag path start-claude's fresh-start block names
STALLED_MAX_MIN = 45
Result = tuple[bool | None, str]


def config() -> dict[str, Any]:
    got = jsonstate.load(paths.config("rotate.json"), {}) or {}
    cfg = dict(DEFAULTS)
    if isinstance(got, dict):
        cfg.update({k: v for k, v in got.items() if k in DEFAULTS})
    return cfg


def switch_file() -> Path:
    return paths.config("rotate-on")


def state_dir() -> Path:
    return paths.state("rotate")


def fresh_flag() -> Path:
    return state_dir() / "fresh-start"


def runs_file() -> Path:
    return state_dir() / "runs.jsonl"


def _cmd(env: str, default: list[str]) -> list[str]:
    v = os.environ.get(env, "")
    if v:
        return shlex.split(v)
    exe = default[0]
    p = paths.bin_dir(exe)
    return [os.fspath(p) if os.access(p, os.X_OK) else exe, *default[1:]]


def _run(argv: list[str], timeout: float = 20) -> tuple[int, str]:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        return -1, type(e).__name__
    return r.returncode, r.stdout


def his_tz() -> _dt.tzinfo:
    rc, out = _run(_cmd("ROTATE_TRIPCLOCK", ["tripclock", "tz"]), 5)
    name = out.strip().splitlines()[0] if rc == 0 and out.strip() else ""
    if name:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return todo.owner_tz()


def _window(spec: str) -> tuple[int, int] | None:
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*", str(spec))
    if not m:
        return None
    a, b = int(m[1]) * 60 + int(m[2]), int(m[3]) * 60 + int(m[4])
    return (a, b) if 0 <= a < 1440 and 0 <= b <= 1440 else None


def last_run(ok_only: bool = True) -> dict[str, Any] | None:
    try:
        with open(runs_file(), encoding="utf-8") as fh:
            rows = [json.loads(ln) for ln in fh if ln.strip()]
    except (OSError, ValueError):
        return None
    rows = [r for r in rows if r.get("op") == "rotate" and (r.get("ok") or not ok_only) and not r.get("dry")]
    return rows[-1] if rows else None


# ---------------------------------------------------------------- the checks


def c_switch(ctx: dict[str, Any]) -> Result:
    return (True, "on") if switch_file().exists() else (False, "off (rotate on --reason …)")


def c_window(ctx: dict[str, Any]) -> Result:
    w = _window(ctx["cfg"]["window"])
    if w is None:
        return None, f"bad window {ctx['cfg']['window']!r}"
    t = clock.now_dt(his_tz())
    m = t.hour * 60 + t.minute
    inside = w[0] <= m < w[1] if w[0] <= w[1] else (m >= w[0] or m < w[1])
    return inside, f"{t:%H:%M} his time, window {ctx['cfg']['window']}"


def c_nightly(ctx: dict[str, Any]) -> Result:
    r = last_run()
    if not r:
        return True, "no rotation yet"
    age_h = (ctx["now"] - float(r.get("at") or 0)) / 3600
    return age_h >= float(ctx["cfg"]["min_gap_h"]), f"last {age_h:.1f} h ago"


def c_session(ctx: dict[str, Any]) -> Result:
    s = ctx["session"]
    if ctx["agents"] is None:
        return None, "claude agents unavailable"
    if not s:
        return False, "no main session"
    age_min = (ctx["now"] - float(s.get("startedAt") or 0) / 1000) / 60
    status = str(s.get("status") or "idle")
    if status != "idle":
        return False, f"session {status}"
    return age_min >= float(ctx["cfg"]["session_min_age_min"]), f"idle, up {age_min:.0f} min"


def c_agents(ctx: dict[str, Any]) -> Result:
    rc, out = _run(_cmd("ROTATE_AGENT_WATCH", ["agent-watch", "status", "--hours", "6"]), 15)
    if rc != 0:
        return None, f"agent-watch exit {rc}"
    lines = out.splitlines()
    hdr = next((i for i, ln in enumerate(lines) if ln.split()[:2] == ["ID", "STATE"]), None)
    if hdr is None:
        return (True, "none") if any("(none)" in ln for ln in lines) else (None, "agent-watch: no table")
    busy = 0
    for ln in lines[hdr + 1 :]:
        p = ln.split()
        if len(p) < 4 or not p[3].endswith("m") or not p[3][:-1].isdigit():
            continue
        if p[1] == "running" or (p[1] == "stalled" and int(p[3][:-1]) <= STALLED_MAX_MIN):
            busy += 1
    return busy == 0, f"{busy} running"


def c_owner_idle(ctx: dict[str, Any]) -> Result:
    if ctx["transcript"] is None:
        return None, "no transcript"
    t = session.last_owner_message(ctx["transcript"])
    if t is None:
        return True, "none in the tail"
    idle = (ctx["now"] - t) / 60
    return idle >= float(ctx["cfg"]["owner_idle_min"]), f"last message {idle:.0f} min ago"


def c_call(ctx: dict[str, Any]) -> Result:
    rc, out = _run(_cmd("ROTATE_VOICE", ["voice-relay", "status"]), 20)
    if rc == 2:
        return True, "relay down"
    if rc != 0:
        return None, f"voice status exit {rc}"
    try:
        d = json.loads(out)
    except ValueError:
        return None, "voice status not JSON"
    live = d.get("live") if isinstance(d, dict) else None
    ids = d.get("call_ids") if isinstance(d, dict) else None
    busy = bool(live) or bool(ids)
    return not busy, "call live" if busy else "no call"


def c_drive(ctx: dict[str, Any]) -> Result:
    rc, _ = _run(_cmd("ROTATE_DRIVING", ["driving"]), 10)
    if rc == 0:
        return False, "driving"
    return (True, "not driving") if rc == 1 else (None, f"driving exit {rc}")


def c_approvals(ctx: dict[str, Any]) -> Result:
    rc, out = _run(_cmd("ROTATE_APPROVALS", ["approvals", "list", "--json"]), 15)
    if rc != 0:
        return None, f"approvals exit {rc}"
    try:
        d = json.loads(out or "[]")
    except ValueError:
        return None, "approvals not JSON"
    rows = d if isinstance(d, list) else (d.get("items") or [] if isinstance(d, dict) else [])
    n = sum(1 for r in rows if isinstance(r, dict) and r.get("state", "pending") == "pending")
    return n == 0, f"{n} pending"


def c_crons(ctx: dict[str, Any]) -> Result:
    crons = ctx["crons"]
    guard = float(ctx["cfg"]["cron_guard_min"]) * 60
    soon = [c for c in crons if c.get("next") and c["next"] - ctx["now"] < guard]
    return not soon, f"{len(crons)} session check-ins, {len(soon)} due within {ctx['cfg']['cron_guard_min']} min"


def c_restart(ctx: dict[str, Any]) -> Result:
    recent = 0
    try:
        for p in paths.inbox().glob("restart-*.md"):
            n = p.stem.split("-", 1)[1]
            if n.isdigit() and ctx["now"] - int(n) < 1800:
                recent += 1
    except OSError:
        return None, "inbox unreadable"
    return recent == 0, f"{recent} restart notes < 30 min"


def c_start_claude(ctx: dict[str, Any]) -> Result:
    p = Path(os.environ.get("ROTATE_START_CLAUDE") or "/usr/local/bin/start-claude")
    try:
        ok = FRESH_MARKER in p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, "start-claude unreadable"
    return ok, "fresh-start block present" if ok else "start-claude lacks the fresh-start block"


CHECKS: dict[str, Callable[[dict[str, Any]], Result]] = {
    "switch": c_switch,
    "window": c_window,
    "nightly": c_nightly,
    "session": c_session,
    "agents": c_agents,
    "owner_idle": c_owner_idle,
    "call": c_call,
    "drive": c_drive,
    "approvals": c_approvals,
    "crons": c_crons,
    "restart": c_restart,
    "start_claude": c_start_claude,
}


def context() -> dict[str, Any]:
    now = clock.now()
    ags = session.agents()
    s = session.main_session(ags)
    tp = session.transcript(str(s.get("sessionId")) if s else None)
    return {
        "now": now,
        "cfg": config(),
        "agents": ags,
        "session": s,
        "transcript": tp,
        "crons": session.session_crons(tp, now),
    }


def run_all(ctx: dict[str, Any], skip: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    out = []
    for name, fn in CHECKS.items():
        if name in skip:
            continue
        try:
            ok, why = fn(ctx)
        except Exception as e:  # noqa: BLE001 (a broken check blocks, never crashes the run)
            ok, why = None, f"error {type(e).__name__}"
        out.append({"check": name, "ok": ok, "why": why})
    return out


def passed(results: list[dict[str, Any]]) -> bool:
    return all(r["ok"] is True for r in results)
