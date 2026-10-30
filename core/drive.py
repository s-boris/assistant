"""core.drive: the one freshness rule for his phone's drive flag. Stdlib only.

Every reader of a drive flag goes through it: `driving` (exit 0 = driving; car-mode voice answers), drive-brief,
lastcall, event-card, context, the usage context (lib/usage.py) and the hub tool vault.ha_now (an inlined copy:
the hub release has no core/; tests/tools/test_vault.py holds it to verdict). An answer in the
`vault ha now --json` schema (whereabouts, the phone gateway's /internal/now, the vault) says he drives only when
its drive is active
({"active": true, ...} or a plain true) AND the answer is at most max_age_min() minutes old (its age_min;
DRIVING_MAX_AGE_MIN, default 60). Older = unknown, never "driving": a phone that went silent mid-drive, or a flag
stuck on for hours, must not keep him driving. An answer without age_min counts (the producer gave no age). The
gateway's own timers (phonegw flags.drive_state: still 15 min, walking 5, vehicle-only stale 20, max 6 h) end a
drive between frames; this rule is the readers' half.

  verdict(answer)                  ("yes", minutes | None) | ("no", None) | ("unknown", None)
  active(answer)                   True only for "yes"
  stored(value, at, now)           a stored flag.drive row (the phonegw DB or its mirror): active, its until (when
                                   the row carries one) not passed, and the row at most max_age_min() old
"""

from __future__ import annotations

import os
from typing import Any, TypeGuard

__all__ = ["MAX_AGE_MIN", "max_age_min", "verdict", "active", "stored"]

MAX_AGE_MIN = 60


def max_age_min() -> int:
    raw = os.environ.get("DRIVING_MAX_AGE_MIN", "").strip()
    try:
        return int(raw) if raw else MAX_AGE_MIN
    except ValueError:
        return MAX_AGE_MIN


def _num(v: Any) -> TypeGuard[int | float]:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def verdict(answer: Any, max_age: int | None = None) -> tuple[str, int | None]:
    if not isinstance(answer, dict):
        return "unknown", None
    d = answer.get("drive")
    if isinstance(d, dict):
        on, mins = d.get("active"), d.get("minutes")
    elif isinstance(d, bool):
        on, mins = d, None
    else:
        return "unknown", None
    if on is None:
        return "unknown", None
    age = answer.get("age_min")
    if _num(age) and age > (max_age_min() if max_age is None else max_age):
        return "unknown", None
    if on is True:
        return "yes", int(mins) if _num(mins) else None
    return "no", None


def active(answer: Any, max_age: int | None = None) -> bool:
    return verdict(answer, max_age)[0] == "yes"


def stored(value: Any, at: Any, now: float, max_age: int | None = None) -> bool:
    if not isinstance(value, dict) or value.get("active") is not True or not _num(at) or not at:
        return False
    at = at / 1000 if at > 1e11 else at
    until = value.get("until")
    if _num(until) and until and now >= (until / 1000 if until > 1e11 else until):
        return False
    return active({"drive": {"active": True}, "age_min": max(0, int(now - at) // 60)}, max_age)
