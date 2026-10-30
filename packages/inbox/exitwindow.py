"""inbox.exitwindow: the exit-review window, read-only counts.

The window closes when all four bars hold since its start:
  beats          >= 20    `events metrics --beats --since START --json`: beats.beat_turns
  owner_turns    >= 100   traces.db turns with trigger 'owner%' since START
  gateway_calls  >= 200   gateway.db calls since START
  weekly_cycle   7 days   7 days since START, and every live weekly hub schedule fired since START (info: x/y)

`start(at)` opens it once (~/.local/state/slo/window.json, 0600; `force` opens a new one). `status()` -> {"start",
"elapsed_days", "bars": [{id, label, n, bar, ok, eta}], "ready", "eta"}: eta = when a bar is met at the rate so far
(None without a rate), the window's eta = the latest of them. A failing source = n None and the error type in note,
never a made-up number. Source overrides (tests): SLO_TRACES_DB, SLO_GATEWAY_DB, SLO_EVENTS_BIN, SLO_TOOL_BIN,
SLO_WINDOW.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from core import paths

from . import slo

BARS: tuple[tuple[str, str, int], ...] = (
    ("beats", "beats", 20),
    ("owner_turns", "his turns", 100),
    ("gateway_calls", "gateway calls", 200),
)
WEEK = 7 * 86400.0


def window_path() -> Path:
    return slo._p("SLO_WINDOW", paths.home() / ".local" / "state" / "slo" / "window.json")


def load() -> dict[str, Any] | None:
    try:
        d = json.loads(window_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and isinstance(d.get("start"), (int, float)) else None


def start(at: float | None = None, force: bool = False, why: str = "") -> dict[str, Any]:
    cur = load()
    if cur is not None and not force:
        return cur
    d = {"start": float(at if at is not None else time.time()), "why": why, "opened": time.time()}
    p = window_path()
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = p.with_name(f".window.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, p)
    return d


# ---------------------------------------------------------------- counts since the start
def n_beats(since: float) -> int:
    d = slo._json_cmd([slo._bin("SLO_EVENTS_BIN", "events"), "metrics", "--beats", "--since", str(int(since)),
                       "--json"], timeout=120)  # fmt: skip
    n = ((d or {}).get("beats") or {}).get("beat_turns")
    if not isinstance(n, int):
        raise ValueError("no beats.beat_turns")
    return n


def n_owner_turns(since: float) -> int:
    con = slo._ro(slo._p("SLO_TRACES_DB", paths.home() / ".local" / "state" / "traces" / "traces.db"))
    try:
        return int(con.execute("SELECT count(*) FROM turns WHERE trigger LIKE 'owner%' AND start_ts >= ?",
                               (since,)).fetchone()[0])  # fmt: skip
    finally:
        con.close()


def n_gateway_calls(since: float) -> int:
    con = slo._ro(slo._p("SLO_GATEWAY_DB", paths.home() / ".local" / "state" / "tools" / "gateway.db"))
    try:
        return int(con.execute("SELECT count(*) FROM calls WHERE ts >= ?", (since,)).fetchone()[0])
    finally:
        con.close()


def _weekly(cron: str) -> bool:
    f = cron.split()
    return len(f) == 5 and f[4] != "*" and f[2] == "*" and f[3] == "*"


def weekly_fired(since: float) -> tuple[int, int, list[str]]:
    """(fired since START, live weekly hub schedules, the names not fired yet)."""
    d = slo._json_cmd([slo._bin("SLO_TOOL_BIN", "tool"), "call", "workflow.schedules", '{"format": "detailed"}',
                       "--json"], timeout=60)  # fmt: skip
    rows = ((d or {}).get("data") or {}).get("schedules")
    if not isinstance(rows, list):
        raise ValueError("no schedules")
    weekly = [r for r in rows if isinstance(r, dict) and r.get("mode") == "live" and r.get("status") == "ACTIVE"
              and _weekly(str(r.get("cron") or ""))]  # fmt: skip
    left = []
    for r in weekly:
        at = slo._epoch(r.get("last_fired"))
        if at is None or at < since:
            left.append(str(r.get("name")))
    return len(weekly) - len(left), len(weekly), sorted(left)


def _eta(start: float, now: float, n: int | None, bar: int) -> float | None:
    if n is None:
        return None
    if n >= bar:
        return now
    if n <= 0 or now <= start:
        return None
    return start + (now - start) * bar / n


def status(now: float | None = None) -> dict[str, Any]:
    w = load()
    if w is None:
        return {"open": False, "note": "no window: `slo window start`"}
    t0, now = float(w["start"]), float(now if now is not None else time.time())
    fns = {"beats": n_beats, "owner_turns": n_owner_turns, "gateway_calls": n_gateway_calls}
    bars: list[dict[str, Any]] = []
    for bid, label, bar in BARS:
        n, note = None, ""
        try:
            n = fns[bid](t0)
        except Exception as e:  # noqa: BLE001  (a failing source = no number, never a made-up one)
            note = type(e).__name__
        bars.append({"id": bid, "label": label, "n": n, "bar": bar, "ok": n is not None and n >= bar,
                     "eta": _eta(t0, now, n, bar), "note": note})  # fmt: skip
    wk: dict[str, Any] = {"id": "weekly_cycle", "label": "weekly cycle", "n": round((now - t0) / 86400, 2),
                          "bar": 7, "eta": t0 + WEEK, "note": ""}  # fmt: skip
    fired_ok = True
    try:
        fired, total, left = weekly_fired(t0)
        wk.update(fired=fired, weekly=total, not_fired=left[:12])
        fired_ok = fired == total
    except Exception as e:  # noqa: BLE001
        wk["note"] = f"weekly schedules: {type(e).__name__}"
        fired_ok = False
    wk["ok"] = now - t0 >= WEEK and fired_ok
    bars.append(wk)
    etas = [b["eta"] for b in bars]
    return {
        "open": True,
        "start": t0,
        "why": w.get("why") or "",
        "elapsed_days": round((now - t0) / 86400, 2),
        "bars": bars,
        "ready": all(b["ok"] for b in bars),
        "eta": None if any(e is None for e in etas) else max(etas),
    }


def _ts(v: float | None) -> str:
    return "-" if v is None else datetime.fromtimestamp(v).strftime("%d.%m %H:%M")


def render(s: dict[str, Any]) -> str:
    if not s.get("open"):
        return f"exit window: {s.get('note')}"
    out = [f"exit window since {_ts(s['start'])} · {s['elapsed_days']:g} d"]
    for b in s["bars"]:
        n = "-" if b["n"] is None else f"{b['n']:g}"
        more = f" · weekly hub schedules fired {b['fired']}/{b['weekly']}" if "weekly" in b else ""
        out.append(f"  {'ok' if b['ok'] else '..':3} {b['label']:14} {n:>7} / {b['bar']:<4} eta {_ts(b['eta'])}{more}"
                   + (f" · {b['note']}" if b.get("note") else ""))  # fmt: skip
    out.append(f"ready: {'yes' if s['ready'] else 'no'} · closes ~{_ts(s['eta'])}")
    return "\n".join(out)
