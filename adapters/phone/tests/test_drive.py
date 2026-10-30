"""The drive flag (flags.drive_state + now.drive_view): a real stored start, the newest car report wins, timers end a
drive without a frame. Includes the 22.10 stuck pattern (car "off" event without its state frame: the flag stayed on
and its start slid to "14 minutes ago" on every read). Offline; flags and epoch seconds only."""
import datetime as dt

from phonegw import flags
from phonegw import now as N
from phonegw.util import now_s, ulid

T0 = 1792675000          # a drive start (epoch s)
MIN = 60


class Ctx:
    """flags.Ctx over plain dicts: state = {key: (value, at)}, events = [(kind, body)]."""

    def __init__(self, now, state=None, events=None, cfg=None):
        self.t, self.st, self.evs, self.c = now, dict(state or {}), list(events or []), cfg or {}

    def now(self):
        return self.t

    def state(self, key):
        return self.st.get(key, (None, None))

    def events(self, kind, since_s):
        return sorted([b for k, b in self.evs if k == kind and (b.get("at") or 0) >= since_s], key=lambda b: b["at"])

    def fences(self):
        return []

    def cfg(self):
        return self.c

    def store(self):
        """What the gateway does on a car/activity frame: keep the evaluation as flag.drive."""
        self.st["flag.drive"] = (flags.drive_state(self), self.t)
        return self.st["flag.drive"][0]


def car_on(at):
    return ("car", {"kind": "car", "state": "on", "at": at})


def car_off(at):
    return ("car", {"kind": "car", "state": "off", "at": at})


def still(since, at=None):
    return ({"kind": "still", "since": since}, at or since)


def view(ctx, stored=None):
    return N.drive_view(stored, ctx, ctx.t, dt.timezone.utc)


# ---------------------------------------------------------------------------------------------- start
def test_since_is_the_real_start_and_does_not_slide():
    c = Ctx(T0 + 14 * MIN, {"car": ({"projection": True, "bt": True}, T0)}, [car_on(T0)])
    d = c.store()
    assert d["active"] and d["since"] == T0 and d["minutes"] == 14 and d["why"] == "car"
    for later in (20, 45, 90):
        c.t = T0 + later * MIN
        v = view(c)
        assert v["active"] and v["since"] == N.iso(T0, dt.timezone.utc) and v["minutes"] == later


def test_since_from_the_car_state_when_no_event_is_left():
    c = Ctx(T0 + 3 * MIN, {"car": ({"projection": False, "bt": True}, T0)})
    assert flags.drive_state(c)["since"] == T0


def test_vehicle_to_car_handover_keeps_the_start():
    c = Ctx(T0 + MIN, {"activity": ({"kind": "vehicle", "since": T0}, T0 + 30)})
    assert c.store()["since"] == T0 and c.store()["why"] == "vehicle"
    c.t = T0 + 5 * MIN                                     # the car's Bluetooth connects a few minutes in
    c.st["car"] = ({"projection": False, "bt": True}, c.t)
    c.evs.append(car_on(c.t))
    d = c.store()
    assert d["active"] and d["since"] == T0 and d["why"] == "car"
    c.t = T0 + 20 * MIN                                    # activity flips to still at a light: still the same drive
    c.st["activity"] = still(T0 + 19 * MIN)
    d = c.store()
    assert d["active"] and d["since"] == T0


def test_a_new_car_on_after_a_lost_off_is_a_new_drive():
    c = Ctx(T0 + MIN, {"car": ({"projection": True, "bt": True}, T0)}, [car_on(T0)])
    c.store()
    c.t = T0 + 2 * 3600                                    # its off never arrived; two hours later the car is on again
    c.evs.append(car_on(c.t - 30))
    d = c.store()
    assert d["active"] and d["since"] == c.t - 30 and d["minutes"] == 0


# ---------------------------------------------------------------------------------------------- the stuck pattern
def stuck(now):
    """22.10: car on (state + event), the off event arrives late WITHOUT its state frame; the old flag said
    {active: true, minutes: 14}; activity last reported 'still' half a day before the drive."""
    return Ctx(now, {"car": ({"projection": True, "bt": True}, T0 + 2),
                     "activity": still(T0 - 14 * 3600), "flag.activity_last": still(T0 - 14 * 3600),
                     "flag.drive": ({"active": True, "minutes": 14}, T0 + 870)},
               [car_on(T0 - 1236), car_off(T0 - 709), car_on(T0), car_off(T0 + 606)])


def test_stuck_pattern_the_off_event_ends_the_drive():
    for later in (15, 20, 4 * 60, 8 * 60, 13 * 60, 17 * 60):      # 19:28 ... 08:23 next morning in the incident
        c = stuck(T0 + later * MIN)
        d = flags.drive_state(c)
        assert d["active"] is False and d["since"] is None and d["minutes"] is None, later
        assert view(c, c.st["flag.drive"][0]) == {"active": False, "since": None, "minutes": None}


def test_stuck_pattern_old_code_slid_but_the_view_never_reuses_stored_minutes():
    c = stuck(T0 + 6 * 3600)
    c.evs = [e for e in c.evs if e[1]["at"] < T0 + 600]         # even without the off event ...
    c.st["bt"] = (["watch"], T0 + 9 * 3600)                     # ... a later bt list without the car ends it
    c.t = T0 + 10 * 3600
    assert flags.drive_state(c) == {"active": False, "since": None, "minutes": None, "until": None, "why": "off"}
    v = view(c, {"active": True, "minutes": 14})
    assert v["active"] is False


def test_lost_off_and_no_later_report_ends_at_the_max():
    c = stuck(T0 + 5 * 3600)
    c.evs = [e for e in c.evs if e[1]["at"] < T0 + 600]
    d = flags.drive_state(c)
    assert d["active"] and d["since"] == T0 and d["until"] == T0 + 6 * 3600
    c.t = T0 + 6 * 3600
    assert flags.drive_state(c)["active"] is False and flags.drive_state(c)["why"] == "max"


# ---------------------------------------------------------------------------------------------- timers
def drive_with(activity, act_at, now):
    return Ctx(now, {"car": ({"projection": True, "bt": True}, T0), "activity": (activity, act_at)}, [car_on(T0)])


def test_still_for_15_minutes_after_the_start_ends_it():
    c = drive_with({"kind": "still", "since": T0 + 30 * MIN}, T0 + 30 * MIN, T0 + 44 * MIN)
    d = flags.drive_state(c)
    assert d["active"] and d["until"] == T0 + 45 * MIN
    c.t = T0 + 45 * MIN
    assert flags.drive_state(c)["active"] is False and flags.drive_state(c)["why"] == "still"
    assert view(c)["active"] is False


def test_walking_for_5_minutes_ends_it():
    c = drive_with({"kind": "walking", "since": T0 + 30 * MIN}, T0 + 30 * MIN, T0 + 34 * MIN)
    assert flags.drive_state(c)["active"]
    c.t = T0 + 35 * MIN
    assert flags.drive_state(c)["why"] == "walking" and view(c)["active"] is False


def test_still_from_before_the_drive_does_not_end_it():
    c = drive_with({"kind": "still", "since": T0 - 3600}, T0 - 3600, T0 + 50 * MIN)
    d = flags.drive_state(c)
    assert d["active"] and d["since"] == T0 and d["minutes"] == 50


def test_tilting_keeps_the_last_real_activity():
    c = drive_with({"kind": "tilting", "since": T0 + 31 * MIN}, T0 + 31 * MIN, T0 + 46 * MIN)
    c.st["flag.activity_last"] = still(T0 + 30 * MIN)
    assert flags.drive_state(c)["why"] == "still"


def test_vehicle_only_goes_stale_after_20_minutes_without_a_report():
    c = Ctx(T0 + 19 * MIN, {"activity": ({"kind": "vehicle", "since": T0}, T0)})
    assert flags.drive_state(c)["active"] and flags.drive_state(c)["why"] == "vehicle"
    c.t = T0 + 20 * MIN
    assert flags.drive_state(c)["active"] is False and flags.drive_state(c)["why"] == "stale"


def test_a_car_drive_does_not_go_stale_while_the_phone_is_silent():
    c = Ctx(T0 + 90 * MIN, {"car": ({"projection": True, "bt": True}, T0)}, [car_on(T0)])
    d = flags.drive_state(c)
    assert d["active"] and d["minutes"] == 90


def test_timers_follow_the_flags_config():
    c = drive_with({"kind": "still", "since": T0 + 30 * MIN}, T0 + 30 * MIN, T0 + 36 * MIN)
    c.c = {"drive_still_off_min": 5, "buds_still_min": 1}
    assert flags.drive_state(c)["why"] == "still"


def test_stored_flag_without_a_ctx():
    tz = dt.timezone.utc
    assert N.drive_view({"active": True, "minutes": 14}, None, T0, tz)["active"] is False        # old format
    good = {"active": True, "since": T0, "minutes": 0, "until": T0 + 3600, "why": "car"}
    assert N.drive_view(good, None, T0 + 600, tz) == {"active": True, "since": N.iso(T0, tz), "minutes": 10}
    assert N.drive_view(good, None, T0 + 3600, tz)["active"] is False


# ---------------------------------------------------------------------------------------------- through the gateway
async def post(phone, t, body):
    item = {"v": 1, "id": ulid(), "t": t, "ts": now_s(), "body": body}
    st, j = await phone.http("POST", "/v1/events", {"items": [item]})
    assert st == 200, j


async def test_gateway_off_event_without_its_state_frame_ends_the_drive(env, phone):
    t = now_s()
    await post(phone, "state", {"car": {"projection": True, "bt": True}, "bt": ["car"],
                                "activity": {"kind": "still", "since": t - 14 * 3600},
                                "place": {"zone": "away", "band": "region", "since": t - 3600, "fix_age_min": 1}})
    await post(phone, "event", {"kind": "car", "state": "on", "at": t - 600})
    st, v = await env.call("GET", "/internal/now")
    assert st == 200 and v["drive"]["active"] is True and v["drive"]["minutes"] == 10
    assert v["drive"]["since"] == N.iso(t - 600, N.tz_of(env.gw.cfg))
    stored = env.gw.db.get_state(phone.device_id, "flag.drive")[0]
    assert stored["since"] == t - 600 and stored["why"] == "car"
    await post(phone, "event", {"kind": "car", "state": "off", "at": now_s()})   # queued off; its state frame was lost
    st, v = await env.call("GET", "/internal/now")
    assert v["drive"] == {"active": False, "since": None, "minutes": None}
    assert env.gw.db.get_state(phone.device_id, "flag.drive")[0]["active"] is False


async def test_gateway_old_stuck_flag_is_not_trusted(env, phone):
    t = now_s()
    await post(phone, "state", {"car": {"projection": True, "bt": True},
                                "activity": {"kind": "still", "since": t - 9000}})
    await post(phone, "event", {"kind": "car", "state": "off", "at": now_s()})
    env.gw.db.put_state(phone.device_id, "flag.drive", {"active": True, "minutes": 14})      # the 22.10 stored flag
    st, v = await env.call("GET", "/internal/now")
    assert v["drive"]["active"] is False
    st, j = await env.call("POST", "/internal/ask", {"q": "car"})
    assert j["result"]["drive"]["active"] is False
