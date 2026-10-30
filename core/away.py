"""core.away: the one calendar rule for "he sleeps away from home". Stdlib only.

Input: the calendar cache's events (calcache's events.json "events": start/end as local ISO; all-day events as
YYYY-MM-DD with the end exclusive; flags.away_overnight set by calcache). A night is named by the date it starts on.
It is away when an away_overnight all-day event covers that date, or an away_overnight timed event runs across the
midnight after it (calcache's own per-day "away overnight" rule). Consecutive away nights make a trip.

Readers: damp-watch (trip mode: trip_min_days nights, away from the first night to the last), cat-corner (trip mode
from the calendar: 2+ nights, away from the first night to the day back; its hand-entered trips win on overlap).

  nights(events)              sorted away-night dates
  trips(events, min_nights)   [{from, last_night, back, nights}] per run of >= min_nights consecutive away nights
                              (dates; back = the day after the last night, the day he comes home), sorted
  events(cache)               the events list of a loaded cache dict ([] for anything else)
  load(path)                  events(the JSON at path); [] when it is missing or unreadable
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterable
from typing import Any

__all__ = ["MAX_SPAN_DAYS", "events", "load", "nights", "trips"]

MAX_SPAN_DAYS = 366  # one event never adds more nights than this (a malformed end date stays harmless)


def _day(v: Any) -> dt.date:
    return dt.date.fromisoformat(str(v)[:10])


def _wall(v: Any) -> dt.datetime:
    """Local wall-clock time of an ISO stamp (its offset dropped: midnights are counted on the local calendar)."""
    return dt.datetime.fromisoformat(str(v)).replace(tzinfo=None)


def _event_nights(ev: dict[str, Any]) -> list[dt.date]:
    if ev.get("all_day"):
        a, b = _day(ev["start"]), _day(ev["end"])
        n = min(max((b - a).days, 1), MAX_SPAN_DAYS)
        return [a + dt.timedelta(days=i) for i in range(n)]
    s, e = _wall(ev["start"]), _wall(ev["end"])
    out: list[dt.date] = []
    d = s.date()
    while len(out) < MAX_SPAN_DAYS and dt.datetime.combine(d + dt.timedelta(days=1), dt.time()) < e:
        out.append(d)
        d += dt.timedelta(days=1)
    return out


def nights(evs: Iterable[Any]) -> list[dt.date]:
    out: set[dt.date] = set()
    for ev in evs:
        if not isinstance(ev, dict):
            continue
        flags = ev.get("flags")
        if not (isinstance(flags, dict) and flags.get("away_overnight") is True):
            continue
        try:
            out.update(_event_nights(ev))
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    return sorted(out)


def trips(evs: Iterable[Any], min_nights: int = 2) -> list[dict[str, Any]]:
    need = max(1, int(min_nights))
    runs: list[list[dt.date]] = []
    for d in nights(evs):
        if runs and d - runs[-1][-1] == dt.timedelta(days=1):
            runs[-1].append(d)
        else:
            runs.append([d])
    return [
        {"from": r[0], "last_night": r[-1], "back": r[-1] + dt.timedelta(days=1), "nights": len(r)}
        for r in runs
        if len(r) >= need
    ]


def events(cache: Any) -> list[Any]:
    ev = cache.get("events") if isinstance(cache, dict) else None
    return ev if isinstance(ev, list) else []


def load(path: str) -> list[Any]:
    try:
        with open(path, encoding="utf-8") as f:
            return events(json.load(f))
    except (OSError, ValueError):
        return []
