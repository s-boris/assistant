"""core.todo: read TODO.md (and GOALS.md) the way hb-gate does. Stdlib only, read-only.

  read(todo_path=None, goals_path=None, now=None) -> Board   Due (reached + upcoming), In progress, Waiting, startable
  sections(text) / section(secs, name)                       "## X" headings -> top-level bullets
  due_at(item, today, anchor, tz) / due_reached(at, now)     the board's due phrase, else hb-gate's due-time rules
  title(item, n)                                             the bold title of a board line, cut to n chars
  owner_tz()                                                 $ASSISTANT_TZ, else the profile's locale.tz, else UTC

Due rules: the board's own due phrase wins (~/bin/board's due_of, what `board show` prints): "due HH:MM DD.MM"
anywhere in the line, else "**Title** — [due] DD.MM HH:MM" or "— [due] HH:MM DD.MM" right after the title; other dates
and times in the line (history notes, file names) don't count then. A line without one keeps hb-gate's rules (b),
path-like tokens (a word with a "/" and a letter) left out: no date/time = due now; a date (YYYY-MM-DD,
DD.MM[.YYYY]) = due from that day (at its first HH:MM if any); only a time = due once it passed today;
"tomorrow"/"morgen" without a date = TODO.md's mtime day + 1. DD.MM without a year = the nearest such day, across
the year boundary. A wall-clock time in the repeated autumn hour is due at its first pass, one in the skipped spring
hour at 03:00. Startable = open GOALS.md steps "- [ ]" not marked blocked, Ideas not marked blocked, every Night
backlog item.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo

from core import clock, paths

__all__ = ["BLOCKED", "Board", "due_at", "due_reached", "owner_tz", "read", "section", "sections", "startable", "title"]

BLOCKED = re.compile(
    r"\b(ask(ed)?\b|waiting|wait for|blocked|after (his|the owner|he|bookings|~)|once (he|his|the owner)"
    r"|when he\b|needs? (his|the owner|owner)|owner:? later|not picked|offered|on hold)|\bfine\.",
    re.I,
)
TIME = re.compile(r"(?<![\d:.])([01]?\d|2[0-3]):([0-5]\d)(?![\d:])")
ISO = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
DOT = re.compile(r"(?<![\w.])(\d{1,2})\.(\d{1,2})(?:\.(\d{4})(?!\d)|\.(?!\d))?(?!\w|\.\d)")
TOMORROW = re.compile(r"\b(tomorrow|morgen)\b", re.I)
_HM = r"(?P<H>[01]?\d|2[0-3]):(?P<M>[0-5]\d)(?!\d)"
_DM = r"(?P<d>\d{1,2})\.(?P<m>\d{1,2})(?:\.(?P<y>\d{4}))?(?!\d)"
DUE_PHRASE = (  # ~/bin/board DUE_RES, in its order
    re.compile(rf"\bdue\s+{_HM}\s+{_DM}"),
    re.compile(rf"^\s*\*\*[^*]+\*\*\s+—\s+(?:due\s+)?{_DM}\.?\s+{_HM}"),
    re.compile(rf"^\s*\*\*[^*]+\*\*\s+—\s+(?:due\s+)?{_HM}\s+{_DM}"),
)
PATHISH = re.compile(r"(?<!\S)(?=\S*/)(?=\S*[^\W\d_])\S+")  # ~/reports/x-2026-11-05.md, a/b.json, https://…


def owner_tz() -> tzinfo:
    """$ASSISTANT_TZ, else the profile helper's zone (assistant_profile next to core in ~/bin/lib), else UTC.
    The zone name itself lives in the profile only; never raises."""
    name = os.environ.get("ASSISTANT_TZ", "")
    if name:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    try:
        lib = os.fspath(paths.bin_dir("lib"))
        if lib not in sys.path and os.path.isfile(os.path.join(lib, "assistant_profile.py")):
            sys.path.append(lib)
        import assistant_profile  # type: ignore[import-not-found]

        return assistant_profile.tz()
    except Exception:
        return ZoneInfo("UTC")


def sections(text: str) -> dict[str, list[str]]:
    """{heading: [top-level bullet texts]} ("## X" headings; sub-bullets and prose dropped)."""
    out: dict[str, list[str]] = {}
    cur = None
    for line in text.splitlines():
        if line.startswith("## "):
            cur = line[3:].strip()
            out[cur] = []
        elif cur is not None and line.startswith("- "):
            out[cur].append(line[2:].strip())
    return out


def section(secs: dict[str, list[str]], name: str) -> list[str]:
    """The bullets of the first heading starting with `name` (case-insensitive), [] if none."""
    for k, v in secs.items():
        if k.lower().startswith(name.lower()):
            return v
    return []


def _day(d: str, m: str, y: str | None, today: date) -> date | None:
    """DD.MM[.YYYY] as a date; no year given = the nearest such day (across the year boundary too)."""
    cands = []
    for yy in [int(y)] if y else [today.year - 1, today.year, today.year + 1]:
        try:
            cands.append(date(yy, int(m), int(d)))
        except ValueError:
            pass
    return min(cands, key=lambda c: abs((c - today).days)) if cands else None


def due_at(item: str, today: date, anchor: date, tz: tzinfo) -> datetime | None:
    """When a Due item falls due (aware datetime in tz), or None = due now (no date, no time)."""
    for rx in DUE_PHRASE:
        mt = rx.search(item)
        day = _day(mt["d"], mt["m"], mt["y"], today) if mt else None
        if mt and day:
            return datetime(day.year, day.month, day.day, int(mt["H"]), int(mt["M"]), tzinfo=tz)
    item = PATHISH.sub(" ", item)
    dates: list[date] = []
    for y, m, d in ISO.findall(item):
        try:
            dates.append(date(int(y), int(m), int(d)))
        except ValueError:
            pass
    for d, m, y in DOT.findall(item):
        if day := _day(d, m, y, today):
            dates.append(day)
    times = [(int(h), int(mi)) for h, mi in TIME.findall(item)]
    if not dates and TOMORROW.search(item):
        dates = [anchor + timedelta(days=1)]
    if not dates and not times:
        return None
    day = min(dates) if dates else today
    h, mi = min(times) if times else (0, 0)
    return datetime(day.year, day.month, day.day, h, mi, tzinfo=tz)


def due_reached(at: datetime, now: datetime) -> bool:
    """Has the clock reached Due time `at`? The first instant the wall clock shows it (see the module doc)."""
    return at.replace(tzinfo=None) <= now.replace(tzinfo=None) or at.timestamp() <= now.timestamp()


def title(item: str, n: int = 40) -> str:
    m = re.match(r"\*\*(.+?)\*\*", item)
    t = (m.group(1) if m else item).strip()
    return t if len(t) <= n else t[: n - 1] + "…"


def startable(secs: dict[str, list[str]], goals_text: str) -> dict[str, int]:
    goals = sum(1 for line in goals_text.splitlines() if line.startswith("- [ ]") and not BLOCKED.search(line))
    ideas = sum(1 for i in section(secs, "Ideas") if not BLOCKED.search(i))
    night = len(section(secs, "Night backlog"))
    return {"goals": goals, "ideas": ideas, "night": night}


@dataclass
class Board:
    due: list[tuple[str, datetime | None]] = field(default_factory=list)  # reached: (item, at or None = now)
    upcoming: list[tuple[str, datetime]] = field(default_factory=list)  # not yet reached, soonest first
    in_progress: list[str] = field(default_factory=list)
    waiting: list[str] = field(default_factory=list)
    startable: dict[str, int] = field(default_factory=dict)
    ok: bool = True


def read(
    todo_path: str | os.PathLike[str] | None = None,
    goals_path: str | os.PathLike[str] | None = None,
    now: datetime | None = None,
    tz: tzinfo | None = None,
) -> Board:
    """The board from TODO.md (+ GOALS.md for startable steps). A missing TODO.md gives an empty board, ok=False."""
    tz = tz or owner_tz()
    now = now or clock.now_dt(tz)
    tp = paths.guard(todo_path if todo_path is not None else paths.home() / "workspace" / "TODO.md")
    gp = paths.guard(goals_path if goals_path is not None else paths.home() / "workspace" / "GOALS.md")
    b = Board()
    try:
        with open(tp, encoding="utf-8") as f:
            text = f.read()
        anchor = datetime.fromtimestamp(os.stat(tp).st_mtime, tz).date()
    except FileNotFoundError:
        b.ok = False
        return b
    try:
        with open(gp, encoding="utf-8") as f:
            goals_text = f.read()
    except FileNotFoundError:
        goals_text = ""
    secs = sections(text)
    for item in section(secs, "Due"):
        at = due_at(item, now.date(), anchor, tz)
        if at is None or due_reached(at, now):
            b.due.append((item, at))
        else:
            b.upcoming.append((item, at))
    b.upcoming.sort(key=lambda x: x[1].timestamp())
    b.in_progress = section(secs, "In progress")
    b.waiting = section(secs, "Waiting")
    b.startable = startable(secs, goals_text)
    return b
