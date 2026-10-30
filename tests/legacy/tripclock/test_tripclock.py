"""Offline tests for tripclock (lib + CLI): synthetic trip, temp config and flight-watch dir, fake clock.

python3 -I test_tripclock.py        TRIPCLOCK_BIN / TRIPCLOCK_LIB point at a copy (default: ~/bin)
"""

import datetime as dt
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from zoneinfo import ZoneInfo

BIN = os.environ.get("TRIPCLOCK_BIN", os.path.expanduser("~/bin/tripclock"))
LIB = os.environ.get("TRIPCLOCK_LIB", os.path.join(os.path.dirname(os.path.realpath(BIN)), "lib", "tripclock.py"))
HOME_TZ = ZoneInfo("Europe/Zurich")
TRIP_TZ = "Asia/Dubai"
UTC = dt.UTC

spec = importlib.util.spec_from_file_location("tripclock_t", LIB)
tc = importlib.util.module_from_spec(spec)
sys.path.insert(0, os.path.dirname(os.path.realpath(LIB)))
spec.loader.exec_module(tc)

# Out: ZRH 15.01 21:30 (+01) -> DOH 05:10 (+03); DOH 07:00 -> DXB 09:15 (+04). Back: DXB 29.01 23:00 (+04) ->
# DOH 23:15 (+03); DOH 30.01 01:30 -> ZRH 06:05 (+01).
LEGS = [
    {
        "id": "a1",
        "no": "QR1",
        "from": "ZRH",
        "to": "DOH",
        "dep": "2027-01-15T21:30:00+01:00",
        "arr": "2027-01-16T05:10:00+03:00",
    },
    {
        "id": "a2",
        "no": "QR2",
        "from": "DOH",
        "to": "DXB",
        "dep": "2027-01-16T07:00:00+03:00",
        "arr": "2027-01-16T09:15:00+04:00",
    },
    {
        "id": "b1",
        "no": "QR3",
        "from": "DXB",
        "to": "DOH",
        "dep": "2027-01-29T23:00:00+04:00",
        "arr": "2027-01-29T23:15:00+03:00",
    },
    {
        "id": "b2",
        "no": "QR4",
        "from": "DOH",
        "to": "ZRH",
        "dep": "2027-01-30T01:30:00+03:00",
        "arr": "2027-01-30T06:05:00+01:00",
    },
]


def at(s, z=HOME_TZ):
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=z)


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.cfg = os.path.join(self.d.name, "trips.json")
        self.fw = os.path.join(self.d.name, "flights")
        os.makedirs(self.fw)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("TRIPCLOCK")}
        self.env.update(
            TRIPCLOCK_CONFIG=self.cfg, TRIPCLOCK_FLIGHTS=self.fw, TRIPCLOCK_HOME_TZ=HOME_TZ.key, HOME=self.d.name
        )
        self._old = {k: os.environ.get(k) for k in ("TRIPCLOCK_CONFIG", "TRIPCLOCK_FLIGHTS", "TRIPCLOCK_OFF")}
        os.environ.update(TRIPCLOCK_CONFIG=self.cfg, TRIPCLOCK_FLIGHTS=self.fw)
        os.environ.pop("TRIPCLOCK_OFF", None)

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.d.cleanup()

    def trip(self, **kw):
        t = {"name": "Test trip", "tz": TRIP_TZ, "from": "2027-01-15", "to": "2027-01-30"}
        t.update(kw)
        with open(self.cfg, "w") as f:
            json.dump({"trips": [t]}, f)

    def flights(self, legs, state=None):
        with open(os.path.join(self.fw, "flights.json"), "w") as f:
            json.dump({"flights": legs}, f)
        if state is not None:
            with open(os.path.join(self.fw, "state.json"), "w") as f:
                json.dump({"flights": state}, f)

    def z(self, s, z=HOME_TZ):
        return tc.zone_name(at(s, z), HOME_TZ)

    def cli(self, *args, now=None):
        env = dict(self.env)
        if now:
            env["TRIPCLOCK_NOW"] = now
        return subprocess.run([BIN] + list(args), capture_output=True, text=True, env=env, timeout=30)


class Legs(Base):
    def setUp(self):
        super().setUp()
        self.trip()
        self.flights(LEGS)

    def test_home_before_and_after(self):
        self.assertEqual(self.z("2027-01-10 12:00"), HOME_TZ.key)
        self.assertEqual(self.z("2027-02-02 12:00"), HOME_TZ.key)

    def test_in_the_air_keeps_departure_zone(self):
        self.assertEqual(self.z("2027-01-15 23:00"), HOME_TZ.key)
        self.assertEqual(self.z("2027-01-16 03:09"), HOME_TZ.key)  # 05:09 (+03), one minute before landing

    def test_switch_at_each_landing(self):
        self.assertEqual(self.z("2027-01-16 03:10"), "Asia/Qatar")  # landed DOH 05:10 (+03)
        self.assertEqual(self.z("2027-01-16 06:14"), "Asia/Qatar")
        self.assertEqual(self.z("2027-01-16 06:15"), TRIP_TZ)  # landed DXB 09:15 (+04)
        self.assertEqual(self.z("2027-01-29 20:14"), TRIP_TZ)
        self.assertEqual(self.z("2027-01-29 21:15"), "Asia/Qatar")  # back via DOH
        self.assertEqual(self.z("2027-01-30 06:04"), "Asia/Qatar")
        self.assertEqual(self.z("2027-01-30 06:05"), HOME_TZ.key)  # landed home

    def test_estimated_landing_moves_the_switch(self):
        self.flights(LEGS, {"a2": {"live": {"arr_est": "2027-01-16T10:00:00+04:00"}}})
        self.assertEqual(self.z("2027-01-16 06:30"), "Asia/Qatar")
        self.assertEqual(self.z("2027-01-16 07:00"), TRIP_TZ)

    def test_switch_list(self):
        sw = tc.switches(HOME_TZ)
        self.assertEqual([k for _, k, _ in sw], ["Asia/Qatar", TRIP_TZ, "Asia/Qatar", HOME_TZ.key])
        self.assertEqual(sw[1][0], at("2027-01-16 09:15", ZoneInfo(TRIP_TZ)).astimezone(UTC))

    def test_quiet_hours_follow_him(self):
        self.assertTrue(tc.in_quiet(at("2027-01-20 21:30"), 0, 8, HOME_TZ))  # 00:30 there
        self.assertFalse(tc.in_quiet(at("2027-01-20 05:30"), 0, 8, HOME_TZ))  # 08:30 there
        self.assertFalse(tc.in_quiet(at("2027-01-10 21:30"), 0, 8, HOME_TZ))  # at home
        end = tc.quiet_end(at("2027-01-20 22:00"), 8, HOME_TZ)
        self.assertEqual(end, at("2027-01-21 08:00", ZoneInfo(TRIP_TZ)))
        self.assertEqual(end.astimezone(HOME_TZ).strftime("%H:%M"), "05:00")

    def test_quiet_end_over_the_return_landing(self):
        end = tc.quiet_end(at("2027-01-30 06:10"), 8, HOME_TZ)  # just landed: home zone again
        self.assertEqual(end, at("2027-01-30 08:00"))

    def test_wall_and_local(self):
        self.assertEqual(tc.wall(dt.date(2027, 1, 20), dt.time(6), HOME_TZ), at("2027-01-20 06:00", ZoneInfo(TRIP_TZ)))
        self.assertEqual(tc.wall(dt.date(2027, 1, 10), dt.time(6), HOME_TZ), at("2027-01-10 06:00"))
        self.assertEqual(tc.local(at("2027-01-20 09:00"), HOME_TZ).strftime("%H:%M"), "12:00")
        self.assertTrue(tc.away(at("2027-01-20 09:00"), HOME_TZ))
        self.assertFalse(tc.away(at("2027-01-15 20:00"), HOME_TZ))

    def test_off_switch(self):
        os.environ["TRIPCLOCK_OFF"] = "1"
        self.assertEqual(self.z("2027-01-20 12:00"), HOME_TZ.key)

    def test_flights_outside_the_window_are_ignored(self):
        self.flights(
            LEGS
            + [
                {
                    "no": "XX9",
                    "from": "ZRH",
                    "to": "DXB",
                    "dep": "2027-03-01T10:00:00+01:00",
                    "arr": "2027-03-01T19:00:00+04:00",
                }
            ]
        )
        self.assertEqual(self.z("2027-03-02 12:00"), HOME_TZ.key)


class Other(Base):
    def test_no_config_is_home(self):
        self.assertEqual(self.z("2027-01-20 12:00"), HOME_TZ.key)
        self.assertEqual(tc.switches(HOME_TZ), [])

    def test_broken_config_is_home_and_status_says_so(self):
        with open(self.cfg, "w") as f:
            f.write("{nope")
        self.assertEqual(self.z("2027-01-20 12:00"), HOME_TZ.key)
        r = self.cli("status", now="2027-01-20 12:00")
        self.assertEqual(r.returncode, 2)
        self.assertIn("config error", r.stdout)

    def test_dates_only(self):
        self.trip()
        self.assertEqual(self.z("2027-01-14 23:59"), HOME_TZ.key)
        self.assertEqual(self.z("2027-01-15 00:00"), TRIP_TZ)
        self.assertEqual(self.z("2027-01-30 20:59"), TRIP_TZ)  # 23:59 there on the last day
        self.assertEqual(self.z("2027-01-30 21:00"), HOME_TZ.key)

    def test_explicit_start_end_beat_legs(self):
        self.trip(start="2027-01-16T12:00:00+04:00", end="2027-01-29T12:00:00+04:00")
        self.flights(LEGS)
        self.assertEqual(self.z("2027-01-16 06:30"), HOME_TZ.key)
        self.assertEqual(self.z("2027-01-16 09:00"), TRIP_TZ)
        self.assertEqual(self.z("2027-01-29 09:00"), HOME_TZ.key)

    def test_unknown_airport_uses_offset(self):
        self.trip()
        legs = [
            dict(LEGS[0], to="QQQ", arr="2027-01-16T07:10:00+05:00"),
            dict(LEGS[1], **{"from": "QQQ", "to": "QQR", "dep": "2027-01-16T09:00:00+05:00"}),
        ]
        self.flights(legs)
        self.assertEqual(self.z("2027-01-16 03:30"), "Etc/GMT-5")
        self.assertEqual(self.z("2027-01-16 07:00"), TRIP_TZ)  # QQR +04:00 = the trip zone

    def test_cli_tz_now_quiet_status(self):
        self.trip()
        self.flights(LEGS)
        self.assertEqual(self.cli("tz", "--at", "2027-01-20 09:00").stdout.strip(), TRIP_TZ)
        self.assertEqual(self.cli("tz", now="2027-01-10 09:00").stdout.strip(), HOME_TZ.key)
        self.assertEqual(self.cli("tz", "--at", "2027-01-16T06:30:00+01:00").stdout.strip(), TRIP_TZ)
        self.assertEqual(
            self.cli("tz", "--at", f"@{int(at('2027-01-16 04:00').timestamp())}").stdout.strip(), "Asia/Qatar"
        )
        out = self.cli("now", now="2027-01-20 09:00").stdout
        self.assertIn("12:00 " + TRIP_TZ, out)
        self.assertIn("home 09:00", out)
        js = json.loads(self.cli("now", "--json", now="2027-01-20 09:00").stdout)
        self.assertEqual((js["local"], js["away"], js["offset"]), ("2027-01-20 12:00", True, "UTC+04:00"))
        self.assertEqual(self.cli("quiet", now="2027-01-20 22:00").returncode, 0)
        self.assertEqual(self.cli("quiet", now="2027-01-10 22:00").returncode, 1)
        self.assertEqual(self.cli("quiet", "--hours", "22-6", now="2027-01-10 22:00").returncode, 0)
        st = json.loads(self.cli("status", "--json", now="2027-01-20 09:00").stdout)
        self.assertEqual((st["zone"], st["trip"], st["away"]), (TRIP_TZ, "Test trip", True))
        self.assertEqual(len(st["switches"]), 4)
        self.assertEqual(st["next_switch"], at("2027-01-29 23:15", ZoneInfo("Asia/Qatar")).astimezone(UTC).isoformat())
        txt = self.cli("status", now="2027-01-20 09:00").stdout
        self.assertIn("next:", txt)
        self.assertEqual(self.cli("bogus").returncode, 2)
        self.assertEqual(self.cli("tz", "--at", "garbage").returncode, 2)

    def test_sync_keeps_past_legs(self):
        self.trip()
        self.flights(LEGS, {"a2": {"live": {"arr_est": "2027-01-16T09:40:00+04:00"}}})
        r = self.cli("sync")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("4 new leg", r.stdout)
        self.assertEqual(oct(os.stat(self.cfg).st_mode & 0o777), "0o600")
        self.flights(LEGS[2:])  # flight-watch dropped the past legs
        self.assertEqual(self.z("2027-01-16 06:30"), "Asia/Qatar")  # snapshot: arr_est 09:40 kept
        self.assertEqual(self.z("2027-01-16 06:45"), TRIP_TZ)
        r = self.cli("sync", "--quiet")
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        with open(self.cfg) as f:
            self.assertEqual(len(json.load(f)["trips"][0]["legs"]), 4)


if __name__ == "__main__":
    unittest.main(verbosity=1)
