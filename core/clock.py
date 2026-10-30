"""core.clock: the one clock. Stdlib only.

  now()        epoch seconds (float)
  now_dt(tz)   aware datetime (tz default: the local zone)
  today(tz)    date

ASSISTANT_NOW overrides the clock for tests: epoch seconds ("1792951200"), or ISO 8601
("2026-10-21T19:00:00+02:00"; a naive value is read as local time). An unparsable value raises ValueError
instead of silently using the real time.
"""

from __future__ import annotations

import datetime as _dt
import os
import time

__all__ = ["now", "now_dt", "today"]


def _override() -> float | None:
    raw = os.environ.get("ASSISTANT_NOW", "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        pass
    try:
        d = _dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(f"ASSISTANT_NOW is neither epoch seconds nor ISO 8601: {raw!r}") from e
    if d.tzinfo is None:
        d = d.astimezone()  # naive = local time
    return d.timestamp()


def now() -> float:
    o = _override()
    return time.time() if o is None else o


def now_dt(tz: _dt.tzinfo | None = None) -> _dt.datetime:
    d = _dt.datetime.fromtimestamp(now(), tz=_dt.UTC)
    return d.astimezone(tz) if tz is not None else d.astimezone()


def today(tz: _dt.tzinfo | None = None) -> _dt.date:
    return now_dt(tz).date()
