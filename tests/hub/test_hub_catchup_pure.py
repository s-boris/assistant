"""Hub schedule catch-up, pure: the registry's catch_up field and cadence (core.schedules), hubwf's verdict on a
schedule's latest tick after a launch (catch_up_verdict and friends), and its tick math (cron_ticks, croniter: from
the CI toolchain, or the cached hub vendor wheels, else those cases skip). hubwf imports DBOS and SQLAlchemy at the
top, so it loads with stand-ins for both here; the real thing (a hubd + Postgres, a hub down across a daily tick)
is tests/hubstore/test_hub_catchup.py."""

from __future__ import annotations

import importlib.util
import os
import sys
import tomllib
import types
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from core import schedules  # noqa: E402

H = 3600.0
DAY = 24 * H
ZRH = ZoneInfo("Europe/Zurich")


@pytest.fixture(scope="module")
def wf():
    fake_dbos = types.ModuleType("dbos")

    class DBOS:  # a stand-in: its decorators are identities
        @staticmethod
        def step(*a, **kw):
            return lambda f: f

        @staticmethod
        def workflow(*a, **kw):
            return lambda f: f

    fake_dbos.DBOS = DBOS  # type: ignore[attr-defined]
    fake_dbos.SetWorkflowID = object  # type: ignore[attr-defined]
    saved = {k: sys.modules.get(k) for k in ("dbos", "sqlalchemy")}
    sys.modules["dbos"] = fake_dbos
    sys.modules["sqlalchemy"] = types.ModuleType("sqlalchemy")
    try:
        spec = importlib.util.spec_from_file_location("hubwf_catchup", REPO / "packages/hub/hubwf.py")
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        yield mod
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


@pytest.fixture(scope="module")
def cron(wf):
    """cron_ticks with croniter importable: the toolchain's, else the cached hub vendor wheels (pure Python)."""
    try:
        import croniter  # noqa: F401
    except ImportError:
        cache = Path(os.environ.get("HUB_VENDOR_CACHE") or Path.home() / ".cache" / "assistant" / "hub-vendor")
        pins = tomllib.loads((REPO / "ops/hub/vendor.toml").read_text()).get("wheel") or []
        wheels = [cache / p["file"] for p in pins if p["file"].split("-")[0] in ("croniter", "python_dateutil", "six")]
        if len(wheels) < 3 or not all(w.is_file() for w in wheels):
            pytest.skip("croniter: neither installed nor its vendor wheels cached")
        sys.path[:0] = [str(w) for w in wheels]
    return wf.cron_ticks


def at(day: int, hh: int, mm: int = 0, ss: int = 0) -> float:
    return datetime(2026, 10, day, hh, mm, ss, tzinfo=ZRH).timestamp()


# ------------------------------------------------------------------------------------------------ the registry


@pytest.mark.parametrize(
    ("expr", "want"),
    [
        ("40 7 * * *", "daily"),
        ("23 9 1 * *", "daily"),
        ("40 6 * * 1", "daily"),
        ("40 18 * * 1-5", "daily"),
        ("0 0 1 1 *", "daily"),
        ("7 */24 * * *", "daily"),
        ("41 9,23 * * *", "hourly"),
        ("7 8-23/3 * * *", "hourly"),
        ("38 */2 * * *", "hourly"),
        ("0 * * * *", "hourly"),
        ("*/5 * * * *", "minute"),
        ("3-59/10 * * * *", "minute"),
        ("4-59/10 8-22 * * *", "minute"),
        ("0,30 7 * * *", "minute"),
        ("* * * * *", "minute"),
        ("61 7 * * *", ""),
        ("7 * * *", ""),
    ],
)
def test_cadence(expr, want):
    assert schedules.cadence(expr) == want


def test_registry_catch_up_defaults_and_overrides():
    reg = schedules.load()
    by = reg.jobs
    assert by["hwtrend"].catch_up is None and by["hwtrend"].catches_up  # daily: on by default
    assert by["ha-package-lint"].catches_up  # weekly
    assert by["deadlines-sync"].catches_up  # monthly
    assert not by["tg-status"].catches_up  # minute-level: never
    assert not by["health-tag"].catches_up  # twice a day: off unless set
    # daily jobs a late run would hurt are off (their registry lines say why)
    for name in ("events-tg-metrics", "dayline", "self-report", "container-watch"):
        assert by[name].catch_up is False and not by[name].catches_up, name


def _registry(tmp_path: Path, extra: str, cron: str = "40 7 * * *") -> Path:
    p = tmp_path / "schedules.toml"
    p.write_text(
        'schema = 1\ntz = "Europe/Zurich"\n\n[jobs.x]\n'
        f'cron = "{cron}"\nclass = "M"\nqueue = "fast"\nargv = ["~/bin/x"]\n'
        f'cron_line = "{cron} /home/node/bin/x"\n{extra}'
    )
    return p


def test_registry_catch_up_validation(tmp_path):
    assert schedules.load(_registry(tmp_path, "catch_up = false\n")).jobs["x"].catches_up is False
    hourly = schedules.load(_registry(tmp_path, "catch_up = true\n", cron="41 9,23 * * *")).jobs["x"]
    assert hourly.catch_up is True and hourly.catches_up
    with pytest.raises(schedules.RegistryError, match="catch_up: true or false"):
        schedules.load(_registry(tmp_path, 'catch_up = "yes"\n'))
    with pytest.raises(schedules.RegistryError, match="minute-level"):
        schedules.load(_registry(tmp_path, "catch_up = true\n", cron="*/5 * * * *"))


# ------------------------------------------------------------------------------------------------ hubwf: the verdict


def test_on_and_grace(wf):
    assert wf.CATCHUP_MAX == 6 * H
    assert wf.catch_up_on({}, DAY) and wf.catch_up_on({}, 23 * H) and wf.catch_up_on({}, 7 * DAY)
    assert not wf.catch_up_on({}, 12 * H) and not wf.catch_up_on({}, H) and not wf.catch_up_on({}, 300)
    assert not wf.catch_up_on({"catch_up": False}, DAY)
    assert wf.catch_up_on({"catch_up": True}, 2 * H) and wf.catch_up_on({"catch_up": True}, H)
    assert not wf.catch_up_on({"catch_up": True}, 600)  # minute-level never catches up
    assert wf.catch_up_grace(DAY) == 6 * H and wf.catch_up_grace(7 * DAY) == 6 * H
    assert wf.catch_up_grace(2 * H) == H and wf.catch_up_grace(H) == 0.5 * H


def test_verdicts(wf):
    tick = at(23, 7, 40)
    v = lambda now, ctx=None, last=tick - DAY, newest=tick - DAY + 5, exists=False, gap=DAY: wf.catch_up_verdict(  # noqa: E731
        tick, gap, now, ctx or {}, last, newest, exists
    )
    # a hub restart at 07:38-07:50 across today's 07:40: it ran yesterday, so today's tick runs once, late
    assert v(tick + 600) == "run"
    assert v(tick + 6 * H) == "run" and v(tick + 6 * H + 1) == "late"
    assert v(tick + 600, ctx={"catch_up": False}) == "off"
    assert v(tick + 600, gap=12 * H) == "off"  # twice a day: off by default
    assert v(tick + 600, ctx={"catch_up": True}, gap=12 * H) == "run"
    assert v(tick + 2 * H, ctx={"catch_up": True}, gap=4 * H) == "run"  # every 4 h: its grace is 2 h
    assert v(tick + 2 * H + 1, ctx={"catch_up": True}, gap=4 * H) == "late"
    assert v(tick + 600, last=tick) == "fired"  # the scheduler fired it before the restart
    assert v(tick + 600, exists=True) == "ran"  # that slot's workflow exists (a second launch, a backfill)
    assert v(tick + 600, newest=tick + 300) == "ran"  # a trigger ran it since
    # `since` (when its cron, zone or mode were set) decides whether the tick is its own
    assert v(tick + 600, last=None, newest=None, ctx={"since": tick - 60}) == "run"
    assert v(tick + 600, last=None, newest=None, ctx={"since": tick + 60}) == "new"  # made after the tick
    assert v(tick + 600, ctx={"since": tick + 60}) == "new"  # fired before, but its cron was edited after the tick
    # one made before `since` was kept: it fired before, or a run of it is older than the tick; else new
    assert v(tick + 600, last=None, newest=tick - 3 * H) == "run"
    assert v(tick + 600, last=None, newest=None) == "new"
    assert v(tick + 600, last=None, newest=None, ctx={"since": True}) == "new"


def test_once_per_schedule_across_launches(wf):
    """The scheduler's own workflow id per slot: the first launch after the tick runs it; every later launch sees
    it (exists, or a run newer than the tick) and leaves it. Older missed ticks are never run."""
    tick = at(23, 7, 40)
    first = wf.catch_up_verdict(tick, DAY, tick + 300, {}, tick - DAY, tick - DAY + 4, False)
    again = wf.catch_up_verdict(tick, DAY, tick + 900, {}, tick - DAY, tick + 301, True)
    assert (first, again) == ("run", "ran")


# ------------------------------------------------------------------------------------------------ hubwf: tick math


def test_cron_ticks_daily_weekly_and_minute(cron):
    prev, gap = cron("40 7 * * *", "Europe/Zurich", at(23, 7, 50))
    assert prev.isoformat() == "2026-10-23T07:40:00+02:00" and gap >= 23 * H
    prev, gap = cron("40 7 * * *", "Europe/Zurich", at(23, 7, 30))
    assert prev.isoformat() == "2026-10-22T07:40:00+02:00"
    prev, gap = cron("40 6 * * 1", "Europe/Zurich", at(23, 12))
    assert prev.isoformat() == "2026-10-19T06:40:00+02:00" and gap == 7 * DAY
    _, gap = cron("41 9,23 * * *", "Europe/Zurich", at(23, 12))
    assert gap == 10 * H
    _, gap = cron("*/5 * * * *", "Europe/Zurich", at(23, 12, 1))
    assert gap == 300
    _, gap = cron("*/2 * * * * *", None, at(23, 12, 1))  # DBOS's six fields: seconds first
    assert gap == 2


def test_cron_ticks_across_dst(cron):
    """25.10.2026: Zurich goes back to CET; the daily slot keeps its wall time and offset, as the scheduler writes it,
    and the 25 h day still counts as daily."""
    prev, gap = cron("40 7 * * *", "Europe/Zurich", datetime(2026, 10, 25, 8, 0, tzinfo=ZRH).timestamp())
    assert prev.isoformat() == "2026-10-25T07:40:00+01:00" and gap >= 23 * H


def test_simulated_downtime_across_a_daily_tick(wf, cron):
    """strike-watch (40 7 * * *): the hub is down 07:35-07:50 on 23.10. Its last fire was 22.10 07:40; the launch
    at 07:50 runs 23.10 07:40 once; a second restart at 08:10 leaves it; a hub down until 14:00 (past 6 h) skips it;
    a hub down for two days runs only the latest missed tick."""
    expr, tz = "40 7 * * *", "Europe/Zurich"
    last = datetime(2026, 10, 22, 7, 40, tzinfo=ZRH).timestamp()
    for now, newest, exists, want in (
        (at(23, 7, 50), last + 3, False, "run"),
        (at(23, 8, 10), at(23, 7, 50, 1), True, "ran"),
        (at(23, 14, 0), last + 3, False, "late"),
        (at(23, 13, 39), last + 3, False, "run"),
    ):
        tick, gap = cron(expr, tz, now)
        assert tick.isoformat() == "2026-10-23T07:40:00+02:00"
        assert wf.catch_up_verdict(tick.timestamp(), gap, now, {}, last, newest, exists) == want, now
    now = at(25, 7, 45)  # down from 22.10 07:39 to 25.10 07:45: 23.10 and 24.10 are gone, 25.10 runs
    tick, gap = cron(expr, tz, now)
    assert tick.isoformat() == "2026-10-25T07:40:00+01:00"
    assert wf.catch_up_verdict(tick.timestamp(), gap, now, {}, last, last + 3, False) == "run"
    # a minute-level schedule down across many ticks: never
    tick, gap = cron("*/5 * * * *", tz, at(23, 7, 50, 30))
    assert wf.catch_up_verdict(tick.timestamp(), gap, at(23, 7, 50, 30), {}, at(23, 7, 30), None, False) == "off"
