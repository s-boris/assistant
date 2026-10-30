"""G5 quiet-hours hold + G8 show_until: held in his quiet hours / on a sleep-in morning, released after; urgent, night_ok
and live countdowns go out; a moot push is dropped; cancel and a newer push replace a held one."""
import asyncio
import datetime as dt
import os
from zoneinfo import ZoneInfo

import pytest

from phonegw.quiet import quiet_release
from phonegw.util import now_s

ZH = ZoneInfo("Europe/Zurich")
ON = [{"quiet_hold": True}]


def at(y, mo, d, h, mi=0):
    return int(dt.datetime(y, mo, d, h, mi, tzinfo=ZH).timestamp())


# A night that is still ahead: the push API refuses a show_until in the real past, so the moot-push check needs a
# faked night after today (a fixed date rots once it has passed).
_N = (dt.datetime.now(ZH) + dt.timedelta(days=3)).date()
NIGHT = (_N.year, _N.month, _N.day)


@pytest.fixture
def fakes(env, tmp_path, monkeypatch):
    sl = tmp_path / "sleepin"
    sl.write_text("#!/bin/sh\nexit 1\n")
    sl.chmod(0o755)
    monkeypatch.setenv("SLEEPIN_BIN", str(sl))
    monkeypatch.setenv("TRIPCLOCK_BIN", "/bin/false")      # falls back to cfg tz (Europe/Zurich)
    return sl


def set_clock(gw, ts):
    gw.quiet_now = lambda: ts
    gw._sleepin_cache = (0, None)


async def push(env, **kw):
    body = {"level": "timed", "title": "Quiet test", "body": "hello", "tag": "q1"}
    body.update(kw)
    return await env.call("POST", "/internal/push", body)


async def nothing(phone, t="push", timeout=0.4):
    with pytest.raises(asyncio.TimeoutError):
        await phone.recv(t, timeout=timeout)


def test_quiet_release_window():
    assert quiet_release(at(2026, 10, 21, 3), ZH, ["00:00", "08:00"]) == at(2026, 10, 21, 8)
    assert quiet_release(at(2026, 10, 21, 8), ZH, ["00:00", "08:00"]) is None
    assert quiet_release(at(2026, 10, 20, 23, 30), ZH, ["23:00", "07:00"]) == at(2026, 10, 21, 7)
    assert quiet_release(at(2026, 10, 21, 6), ZH, ["23:00", "07:00"]) == at(2026, 10, 21, 7)
    assert quiet_release(at(2026, 10, 21, 12), ZH, ["23:00", "07:00"]) is None
    assert quiet_release(at(2026, 10, 25, 2, 30), ZH, ["00:00", "08:00"]) == at(2026, 10, 25, 8)   # clock-change night


@pytest.mark.parametrize("env", ON, indirect=True)
async def test_night_hold_and_release(env, phone, fakes):
    gw = env.gw
    await phone.connect()
    await phone.recv("hello")
    set_clock(gw, at(*NIGHT, 3))
    st, j = await push(env)
    assert st == 200 and j["status"] == "held" and j["hold_until"] == at(*NIGHT, 8)
    await nothing(phone)
    st, j = await push(env, level="info", tag="q2", title="Info at night")
    assert j["status"] == "held"
    st, j = await push(env, tag="q1", title="Newer copy")              # same tag: replaces the held one
    assert j["status"] == "held" and gw.held_count()["n"] == 2
    st, j = await env.call("POST", "/internal/push/cancel", {"tag": "q2"})
    assert j["dropped_unsent"] == 1 and gw.held_count()["n"] == 1
    await phone.recv("push.cancel")
    # urgent, night_ok and live countdowns go out at night
    st, j = await push(env, level="urgent", tag="u1", title="Urgent at night")
    assert j["status"] in ("sent", "acked")
    assert (await phone.recv("push"))["body"]["tag"] == "u1"
    st, j = await push(env, tag="buds", night_ok=True, title="Night ok")
    p = await phone.recv("push")
    assert p["body"]["tag"] == "buds" and p["body"]["night_ok"] is True
    st, j = await push(env, level="info", tag="live", title="Last train", live={"kind": "countdown", "until": now_s() + 900})
    p = await phone.recv("push")
    assert p["body"]["tag"] == "live" and abs(p["body"]["show_until"] - (now_s() + 1200)) <= 2
    # moot: its display limit ends before 08:00
    st, j = await push(env, tag="moot", show_until=at(*NIGHT, 7))
    assert j["status"] == "dropped" and "id" not in j
    # still quiet at 07:59: nothing released; 08:01: the held one goes out with show_until from the release
    assert await gw.release_held(at(*NIGHT, 7, 59)) == 0
    set_clock(gw, at(*NIGHT, 8, 1))
    assert await gw.release_held() == 1
    p = await phone.recv("push")
    assert p["body"]["title"] == "Newer copy"
    assert p["body"]["show_until"] == at(*NIGHT, 8) + 12 * 3600
    assert gw.held_count()["n"] == 0
    await nothing(phone)


@pytest.mark.parametrize("env", ON, indirect=True)
async def test_sleepin_morning_holds_until_its_end(env, phone, fakes):
    gw = env.gw
    fakes.write_text('#!/bin/sh\necho \'{"active": true, "date": "2026-10-23", "until": "12:30"}\'\n')
    await phone.connect()
    await phone.recv("hello")
    set_clock(gw, at(2026, 10, 23, 9))
    st, j = await push(env, level="decision", title="Morning question")
    assert j["status"] == "held" and j["hold_until"] == at(2026, 10, 23, 12, 30)
    set_clock(gw, at(2026, 10, 23, 12, 31))          # the fake still says active, but its end passed
    assert await gw.release_held() == 1
    assert (await phone.recv("push"))["body"]["title"] == "Morning question"
    # quiet + sleep-in: the later end wins
    gw._sleepin_cache = (0, None)
    set_clock(gw, at(2026, 10, 23, 3))
    _, j = await push(env, tag="q9")
    assert j["hold_until"] == at(2026, 10, 23, 12, 30)


@pytest.mark.parametrize("env", ON, indirect=True)
async def test_show_until_defaults_by_day(env, phone, fakes):
    gw = env.gw
    await phone.connect()
    await phone.recv("hello")
    set_clock(gw, at(2026, 10, 21, 15))
    now = now_s()
    await push(env, level="info", tag="i")
    assert abs((await phone.recv("push"))["body"]["show_until"] - (now + 86400)) <= 2
    await push(env, level="urgent", tag="u")
    assert abs((await phone.recv("push"))["body"]["show_until"] - (now + 6 * 3600)) <= 2
    await push(env, level="decision", tag="d", ttl=5 * 86400)            # a longer ttl wins over the level default
    assert abs((await phone.recv("push"))["body"]["show_until"] - (now + 5 * 86400)) <= 2
    await push(env, tag="s", show_s=600)
    assert abs((await phone.recv("push"))["body"]["show_until"] - (now + 600)) <= 2
    st, j = await push(env, tag="p", show_until=now - 5)
    assert st == 400


async def test_hold_off_in_config_sends_at_night(env, phone):
    env.gw.quiet_now = lambda: at(2026, 10, 21, 3)
    await phone.connect()
    await phone.recv("hello")
    st, j = await push(env)
    assert j["status"] in ("sent", "acked") and "show_until" in (await phone.recv("push"))["body"]


@pytest.mark.parametrize("env", ON, indirect=True)
async def test_cli_show_for_night_ok_and_held_exit_0(env, phone, fakes):
    import json as _json
    import subprocess
    from conftest import PHONE_BIN
    await phone.connect()
    await phone.recv("hello")

    def cli(*args):
        return subprocess.run([PHONE_BIN, "push", "--level", "timed", "--title", "CLI test", "--json", *args],
                              env=env.cli_env(), capture_output=True, text=True, timeout=30)

    r = await asyncio.to_thread(cli, "--tag", "c1", "--show-for", "45m", "--night-ok")
    assert r.returncode == 0, r.stderr
    b = (await phone.recv("push"))["body"]
    assert b["night_ok"] is True and abs(b["show_until"] - (now_s() + 2700)) <= 3
    set_clock(env.gw, at(2026, 10, 21, 2))
    r = await asyncio.to_thread(cli, "--tag", "c2")
    assert r.returncode == 0 and _json.loads(r.stdout)["status"] == "held", r.stdout + r.stderr
