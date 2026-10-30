"""core.todo: hb-gate's Due rules, ported with its fixtures (tests/legacy/hb-gate/run.sh, section (b)), plus the
In progress / Waiting / startable reads."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from core import todo

Z = ZoneInfo("Europe/Zurich")


def zt(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=Z)


def utc(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=UTC).astimezone(Z)


def is_due(item: str, now: datetime, anchor_days_ago: int = 0) -> bool:
    """Like hb-gate's fixture: TODO.md written 1 h before `now` (or N days before)."""
    anchor = (now - timedelta(hours=1) - timedelta(days=anchor_days_ago)).date()
    at = todo.due_at(item, now.date(), anchor, Z)
    return at is None or todo.due_reached(at, now)


NOW = zt("2031-06-15 14:00")


@pytest.mark.parametrize(
    "item,want",
    [
        ("**Pay the placeholder bill** — no date", True),  # no date, no time: due now
        ("**Call** — 18:00", False),  # a time later today
        ("**Call** — 13:30", True),  # a time that passed today
        ("**Call** — 14:00", True),  # the exact minute
        ("**Trip** — 20.06", False),  # DD.MM later this year
        ("**Trip** — 15.06", True),  # DD.MM today, no time
        ("**Trip** — 16.06 09:00", False),  # DD.MM HH:MM tomorrow
        ("**Trip** — 15.06 15:30", False),  # DD.MM HH:MM later today
        ("**Trip** — 10.06.", True),  # DD.MM. in the past
        ("**Renewal** — 01.03.2032", False),  # DD.MM.YYYY next year
        ("**Renewal** — 2031-06-14", True),  # ISO date in the past
        ("**Renewal** — 2031-07-01 08:00", False),  # ISO date in the future
        ("**Odd** — 31.02", True),  # an impossible date is ignored
        ("**Two dates** — 20.06 or 14.06", True),  # the earliest date counts
        ("**Brief** — tomorrow 09:00", False),  # tomorrow, anchored on TODO.md's day
    ],
)
def test_due_rules(item: str, want: bool) -> None:
    assert is_due(item, NOW) is want


@pytest.mark.parametrize(
    "item,want",
    [
        # the board's "due HH:MM DD.MM" wins over a history time and the ISO dates in file names
        (
            "**Re-measure** — due 18:00 05.11 — write ~/reports/r/remeasure-2031-11-05.md; "
            "decisions in ~/reports/r/review-2031-10-29.md",
            "2031-11-05 18:00",
        ),
        ("**Verify fix** — due 10:00 30.10 — live since 22:04 29.10 (~/jobs/fix/apply.log)", "2031-10-30 10:00"),
        ("**Ferry 5.12** — due 18:00 03.11 — free cancel 13.11", "2031-11-03 18:00"),  # a date in the title
        ("**Swap** — 10.11 10:00 — rest: check 28.10 notes", "2031-11-10 10:00"),  # board form "— DD.MM HH:MM"
        ("**Swap** — due 10:00 10.11.2032 — x", "2032-11-10 10:00"),  # an explicit year
        ("**Report** — read ~/reports/r-2031-12-01.md and https://example.org/2031/12/01", None),  # paths: no date
        ("**Report** — 05.11, notes in ~/reports/r-2031-10-01.md", "2031-11-05 00:00"),  # no phrase: rules (b)
    ],
)
def test_board_due_phrase(item: str, want: str | None) -> None:
    now = zt("2031-10-30 06:00")
    at = todo.due_at(item, now.date(), now.date(), Z)
    assert (at and at.strftime("%Y-%m-%d %H:%M")) == want


def test_tomorrow_two_days_after_writing_is_due() -> None:
    assert is_due("**Brief** — tomorrow 09:00", NOW, anchor_days_ago=2)


@pytest.mark.parametrize(
    "now,item,want",
    [
        ("2031-12-20 14:00", "**Renew the vignette** — 05.01", False),  # next January, not overdue
        ("2031-12-20 14:00", "**Renew the vignette** — 05.01 09:00", False),
        ("2031-12-20 14:00", "**Pay** — 18.12", True),  # two days overdue
        ("2032-01-03 14:00", "**Pay the December bill** — 28.12", True),  # last week: overdue
        ("2032-01-03 14:00", "**Pay** — 10.01", False),  # next week
        ("2032-01-03 14:00", "**Pay** — 01.01.2033", False),  # an explicit year is never moved
    ],
)
def test_year_boundary(now: str, item: str, want: bool) -> None:
    assert is_due(item, zt(now)) is want


@pytest.mark.parametrize(
    "now_utc,item,want",
    [
        ("2026-10-25 00:10", "**Call** — 2026-10-25 02:30", False),  # autumn 02:10 CEST, first pass
        ("2026-10-25 01:10", "**Call** — 2026-10-25 02:30", True),  # 02:10 CET, second pass: passed at 02:30 CEST
        ("2026-10-25 01:10", "**Call** — 2026-10-25 02:50", True),
        ("2026-10-25 01:10", "**Call** — 2026-10-25 03:30", False),
        ("2027-03-28 00:59", "**Call** — 2027-03-28 02:30", False),  # spring 01:59 CET, 02:30 is skipped
        ("2027-03-28 01:00", "**Call** — 2027-03-28 02:30", True),  # 03:00 CEST: the skipped time is due at once
        ("2027-03-28 01:00", "**Call** — 2027-03-28 03:30", False),
    ],
)
def test_dst_nights(now_utc: str, item: str, want: bool) -> None:
    assert is_due(item, utc(now_utc)) is want


def test_title_cut() -> None:
    assert (
        todo.title("**A very long due item title that goes on and on and on** — now")
        == "A very long due item title that goes on…"
    )
    assert todo.title("plain item") == "plain item"


def test_sections_prefix_and_subbullets() -> None:
    secs = todo.sections("# TODO\n\n## Due today\n- **Prefix heading**\n  - sub-bullet 01.01\n\n## Ideas\n")
    assert todo.section(secs, "Due") == ["**Prefix heading**"]
    assert todo.section(secs, "Missing") == []


def board_files(home: Path, now: datetime) -> tuple[Path, Path]:
    t = home / "workspace" / "TODO.md"
    t.write_text(
        "# TODO\n\n## Due\n- **Past** — 13:30\n- **Later** — 16.06 09:00\n- **Soon** — 18:00\n\n"
        "## In progress\n- **Build** — running\n  - sub\n\n## Waiting on you\n- **W1** — x\n- **W2** — y\n\n"
        "## Ideas\n- **Idea one**\n- **Idea two** — waiting for his answer\n\n## Night backlog\n- **N1**\n",
        encoding="utf-8",
    )
    ts = (now - timedelta(hours=1)).timestamp()
    os.utime(t, (ts, ts))
    g = home / "workspace" / "GOALS.md"
    g.write_text("# Goals\n- [ ] step one\n- [ ] step two (blocked on parts)\n- [x] done\n", encoding="utf-8")
    return t, g


def test_read_board(home: Path) -> None:
    board_files(home, NOW)
    b = todo.read(now=NOW, tz=Z)
    assert b.ok
    assert [todo.title(i) for i, _ in b.due] == ["Past"]
    assert [todo.title(i) for i, _ in b.upcoming] == ["Soon", "Later"]
    assert [todo.title(i) for i in b.in_progress] == ["Build"]
    assert len(b.waiting) == 2
    assert b.startable == {"goals": 1, "ideas": 1, "night": 1}


def test_read_missing_todo(home: Path) -> None:
    b = todo.read(now=NOW, tz=Z)
    assert not b.ok and b.due == [] and b.waiting == []


def test_owner_tz_env_and_fallback(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert str(todo.owner_tz()) == "Europe/Zurich"
    monkeypatch.setenv("ASSISTANT_TZ", "Not/AZone")
    assert str(todo.owner_tz()) == "UTC"  # no profile helper in the temp HOME
