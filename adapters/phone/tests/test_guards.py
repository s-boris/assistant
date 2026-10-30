"""Guards: Buds left-behind rules (flags.buds_left, pure) + the push over the fake phone; the app battery budget
(state.app per day -> one inbox note per streak of over-budget days)."""
import asyncio
import datetime as dt
import glob
import json
import os

import pytest

from phonegw import flags
from test_gateway_features import online

T0 = 1792500000


class FakeCtx:
    def __init__(self, now=T0, cfg=None, **state):
        self._now, self._cfg, self.st = now, cfg or {}, dict(state)

    def now(self):
        return self._now

    def state(self, key):
        return (self.st[key], self._now) if key in self.st else (None, None)

    def events(self, kind, since_s):
        return []

    def fences(self):
        return []

    def cfg(self):
        return dict(self._cfg)


def off(ctx, at, place=None, act=None):
    """Run a buds 'off' event through the engine and store the derived flag like the gateway does."""
    if place is not None:
        ctx.st["place"] = place
    if act is not None:
        ctx.st["activity"] = act
    effs = flags.FlagEngine().on_event(ctx, {"kind": "bt", "dev": "buds", "state": "off", "at": at})
    for e in effs:
        if e.kind == "derived":
            ctx.st["flag." + e.name] = e.data
    return effs


def pushes(effs):
    return [e for e in effs if e.kind == "push"]


# ---------------------------------------------------------------------------------------------------- rules (pure)
def test_leaving_office_soon_after_disconnect_alerts():
    c = FakeCtx(now=T0 + 400)
    off(c, T0, place={"zone": "office", "town": "Testtown"})
    effs = flags.FlagEngine().on_event(c, {"kind": "zone", "zone": "office", "dir": "exit", "at": T0 + 400})
    p = pushes(effs)
    assert len(p) == 1 and p[0].name == "buds_left"
    assert p[0].data == {"zone": "office", "town": "Testtown", "off_at": T0, "trigger": "zone"}
    assert json.dumps(p[0].data)                                   # flags only, JSON-serialisable


def test_office_exit_too_late_or_other_zone_or_buds_back_is_quiet():
    c = FakeCtx()
    off(c, T0, place={"zone": "office"})
    eng = flags.FlagEngine()
    assert not pushes(eng.on_event(c, {"kind": "zone", "zone": "office", "dir": "exit", "at": T0 + 11 * 60}))
    assert not pushes(eng.on_event(c, {"kind": "zone", "zone": "gym", "dir": "exit", "at": T0 + 60}))
    c.st["bt"] = ["buds", "watch"]                                 # state says they are connected again
    assert not pushes(eng.on_event(c, {"kind": "zone", "zone": "office", "dir": "exit", "at": T0 + 60}))
    del c.st["bt"]
    for e in eng.on_event(c, {"kind": "bt", "dev": "buds", "state": "on", "at": T0 + 30}):
        c.st["flag." + e.name] = e.data
    assert not pushes(eng.on_event(c, {"kind": "zone", "zone": "office", "dir": "exit", "at": T0 + 60}))


def test_home_only_the_walked_out_of_range_signature():
    eng = flags.FlagEngine()
    c = FakeCtx()
    off(c, T0, place={"zone": "home"})                              # case closed at home, he leaves 8 min later
    assert not pushes(eng.on_event(c, {"kind": "zone", "zone": "home", "dir": "exit", "at": T0 + 8 * 60}))
    assert pushes(eng.on_event(c, {"kind": "zone", "zone": "home", "dir": "exit", "at": T0 + 3 * 60}))
    c2 = FakeCtx(cfg={"buds_home_window_min": 0})                   # 0 turns the home rule off
    off(c2, T0, place={"zone": "home"})
    assert not pushes(eng.on_event(c2, {"kind": "zone", "zone": "home", "dir": "exit", "at": T0 + 60}))


def test_no_fence_still_then_moving():
    eng = flags.FlagEngine()
    c = FakeCtx()
    off(c, T0, place={"zone": "away", "town": "Basel"}, act={"kind": "still", "since": T0 - 40 * 60})
    effs = eng.on_state(c, {"activity": {"kind": "walking", "since": T0 + 120}}, {"activity": {"kind": "still"}})
    p = pushes(effs)
    assert len(p) == 1 and p[0].data["zone"] is None and p[0].data["town"] == "Basel" and p[0].data["trigger"] == "move"
    # sat down only 3 min before: on a train/bus stop, not a place he left them -> quiet
    c2 = FakeCtx()
    off(c2, T0, place={"zone": "away"}, act={"kind": "still", "since": T0 - 180})
    assert not pushes(eng.on_state(c2, {"activity": {"kind": "walking", "since": T0 + 60}}, {"activity": {"kind": "still"}}))
    # moving 6 min later is outside the 5 min window; inside a fence the exit decides
    assert not pushes(eng.on_state(c, {"activity": {"kind": "walking", "since": T0 + 6 * 60}}, {"activity": {"kind": "still"}}))
    c3 = FakeCtx()
    off(c3, T0, place={"zone": "gym"}, act={"kind": "still", "since": T0 - 3600})
    assert not pushes(eng.on_state(c3, {"activity": {"kind": "walking", "since": T0 + 60}}, {"activity": {"kind": "still"}}))


def test_other_bt_classes_ignored():
    c = FakeCtx()
    assert flags.FlagEngine().on_event(c, {"kind": "bt", "dev": "watch", "state": "off", "at": T0}) == []


# ---------------------------------------------------------------------------------------------------- push (fake phone)
@pytest.mark.parametrize("env", [{"flags": {"buds_grace_s": 0}}], indirect=True)
async def test_buds_push_once_per_disconnect(env, phone):
    await online(env, phone)
    now = int(dt.datetime.now().timestamp())
    await phone.send("event", {"kind": "zone", "zone": "office", "dir": "enter", "at": now - 3600}, ack=True)
    await phone.send("event", {"kind": "bt", "dev": "buds", "state": "off", "at": now - 120}, ack=True)
    await phone.send("event", {"kind": "zone", "zone": "office", "dir": "exit", "at": now}, ack=True)
    msg = await phone.recv("push")
    b = msg["body"]
    assert b["title"] == "🎧 Buds left behind?" and b["private"] is True and b["level"] == "timed"
    assert b["body"].startswith("Last connected ") and b["body"].endswith(" at Work.")
    assert [a["verb"] for a in b["actions"]] == ["seen", "open"] and b["deep_link"] == "claudeapp://find-buds"
    # a second exit for the same disconnect: no second push
    await phone.send("event", {"kind": "zone", "zone": "office", "dir": "enter", "at": now + 10}, ack=True)
    await phone.send("event", {"kind": "zone", "zone": "office", "dir": "exit", "at": now + 20}, ack=True)
    await asyncio.sleep(0.4)
    assert env.gw.db.one("SELECT COUNT(*) n FROM push_log WHERE tag='buds-left'")["n"] == 1


@pytest.mark.parametrize("env", [{"flags": {"buds_grace_s": 1}}], indirect=True)
async def test_buds_back_within_grace_no_push(env, phone):
    await online(env, phone)
    now = int(dt.datetime.now().timestamp())
    await phone.send("event", {"kind": "zone", "zone": "gym", "dir": "enter", "at": now - 3600}, ack=True)
    await phone.send("event", {"kind": "bt", "dev": "buds", "state": "off", "at": now - 60}, ack=True)
    await phone.send("event", {"kind": "zone", "zone": "gym", "dir": "exit", "at": now}, ack=True)
    await phone.send("event", {"kind": "bt", "dev": "buds", "state": "on", "at": now + 1}, ack=True)
    await asyncio.sleep(1.6)
    assert env.gw.db.one("SELECT COUNT(*) n FROM push_log WHERE tag='buds-left'")["n"] == 0
    assert ("buds_left", ("back",), False) in env.gw.effect_log


@pytest.mark.parametrize("env", [{"buds_guard": False, "flags": {"buds_grace_s": 0}}], indirect=True)
async def test_buds_guard_off(env, phone):
    await online(env, phone)
    now = int(dt.datetime.now().timestamp())
    await phone.send("event", {"kind": "zone", "zone": "office", "dir": "enter", "at": now - 3600}, ack=True)
    await phone.send("event", {"kind": "bt", "dev": "buds", "state": "off", "at": now - 60}, ack=True)
    await phone.send("event", {"kind": "zone", "zone": "office", "dir": "exit", "at": now}, ack=True)
    await asyncio.sleep(0.4)
    assert env.gw.db.one("SELECT COUNT(*) n FROM push_log WHERE tag='buds-left'")["n"] == 0


# ---------------------------------------------------------------------------------------------------- battery budget
def notes(env):
    return sorted(glob.glob(os.path.join(env.paths.inbox, "phone-battery-*.md")))


async def send_app(phone, day, **vals):
    await phone.send("state", {"app": dict({"fgs": True, "fgs_h": 23.5, "day": day}, **vals)})


async def test_battery_note_after_two_over_days_once_per_streak(env, phone):
    await online(env, phone)
    dev = env.gw.db.active_devices()[0]["id"]
    d = dt.date(2026, 10, 10)
    day = lambda i: (d + dt.timedelta(days=i)).isoformat()          # noqa: E731
    await send_app(phone, day(0), reconnects_d=12, loc_d=100)
    await send_app(phone, day(0), reconnects_d=140, loc_d=120)      # latest value of the day wins
    await send_app(phone, day(1), reconnects_d=150, cpu_s_d=900.5)
    await send_app(phone, day(2), reconnects_d=160)
    await send_app(phone, day(3), reconnects_d=5)
    await send_app(phone, day(4), wakeups_h=50.5)
    await send_app(phone, day(5), wakeups_h=61)
    for _ in range(50):
        if env.gw.app_days(dev, [day(5)])[day(5)]:
            break
        await asyncio.sleep(0.05)
    assert env.gw.battery_check(dev, d + dt.timedelta(days=1)) is None          # one day over: quiet
    path = env.gw.battery_check(dev, d + dt.timedelta(days=2))                  # days 0+1 both over on reconnects
    assert path and notes(env) == [path]
    text = open(path).read()
    assert "reconnects_d **over**" in text and "cpu_s_d **over**" not in text and "| 140 | 150 |" in text
    assert "socket drops" in text and "23.5" in text
    assert env.gw.battery_check(dev, d + dt.timedelta(days=3)) is None          # streak goes on: no second note
    assert env.gw.battery_check(dev, d + dt.timedelta(days=4)) is None          # day 3 under budget: streak ends
    assert env.gw.battery_check(dev, d + dt.timedelta(days=6))                  # new streak (wakeups) -> new note
    assert len(notes(env)) == 2
    st = env.gw.battery_view(dev)
    assert st["budget"]["reconnects_d"] == 48 and "noted_through" in st


async def test_battery_no_data_no_note_and_status_view(env, phone):
    await online(env, phone)
    dev = env.gw.db.active_devices()[0]["id"]
    assert env.gw.battery_check(dev, dt.date(2026, 10, 12)) is None
    env.gw.battery_tick()
    assert notes(env) == []
    info = env.gw.device_info(env.gw.db.device(dev))
    assert info["app_budget"]["over"] == [] and info["app_budget"]["app"] is None


@pytest.mark.parametrize("env", [{"battery_budget": {"reconnects_d": 10, "days": 2}}], indirect=True)
async def test_battery_budget_config_override(env, phone):
    await online(env, phone)
    dev = env.gw.db.active_devices()[0]["id"]
    assert env.gw.budget()["reconnects_d"] == 10 and env.gw.budget()["loc_d"] == 350
    await send_app(phone, "2026-10-01", reconnects_d=11)
    await send_app(phone, "2026-10-02", reconnects_d=12)
    for _ in range(50):
        if env.gw.app_days(dev, ["2026-10-02"])["2026-10-02"]:
            break
        await asyncio.sleep(0.05)
    assert env.gw.battery_check(dev, dt.date(2026, 10, 3))


async def test_app_day_and_fgs_h_are_schema_checked(env, phone):
    await online(env, phone)
    await phone.send("state", {"app": {"fgs": True, "fgs_h": 30, "day": "20.10.2026"}})
    err = await phone.recv("error")
    assert err["body"]["code"] in ("schema", "bad_frame", "invalid") or err["body"]["code"]
    assert env.gw.db.one("SELECT 1 FROM daily WHERE metric='app'") is None
