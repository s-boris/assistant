"""Offline tests for cat-corner: stub `ha` (states) + history fixture + stub `tg`; synthetic pet, no network."""

import datetime as dt
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from zoneinfo import ZoneInfo

BIN = os.environ.get("CAT_CORNER_BIN", os.path.expanduser("~/bin/cat-corner"))
TZ = ZoneInfo("Europe/Zurich")
UTC = dt.UTC
NOW = dt.datetime(2026, 11, 18, 10, 0, tzinfo=TZ)  # a Wednesday

HA_STUB = r"""#!/usr/bin/env python3
import json, os, sys
db = os.environ['STUB_DB']
with open(db + '.log', 'a') as f:
    f.write(' '.join(sys.argv[1:]) + '\n')
d = json.load(open(db))
if d.get('down'):
    sys.exit(1)
if sys.argv[1] != 'list':
    sys.exit(2)
print(json.dumps(d['states']))
"""
TG_STUB = '#!/bin/sh\nprintf "%s\\n---\\n" "$1" >> "$STUB_TG"\nprintf "%s\\n" "${TG_SOFT:-}" >> "$STUB_TG.soft"\n'

PET = "sensor.mochi_visits_today"
ROBOT = "litter_robot_4"
AWAY = ZoneInfo("Asia/Dubai")  # synthetic trip zone: home + 3 h in November
TRIP = {"from": "2026-11-16", "to": "2026-11-25", "tz": "Asia/Dubai"}  # holds NOW


def iso(t):
    return t.astimezone(UTC).isoformat()


def day0(now, idx):
    d = now.astimezone(TZ).date() - dt.timedelta(days=idx)
    return dt.datetime.combine(d, dt.time(0), TZ)


class World:
    """Synthetic HA: current states + recorder history for a robot, one pet, a feeder and a fountain."""

    def __init__(self, now=NOW, days=16, visits=lambda i: 4, weight_lb=lambda i: 8.2, feeder=True):
        self.now, self.hist = now, {}
        h = self.hist
        st = f"sensor.{ROBOT}_status_code"
        total, alt = 0, 0
        for i in range(days, -1, -1):
            ds = day0(now, i)
            span = (now - ds - dt.timedelta(hours=1)) if i == 0 else dt.timedelta(hours=23)
            h.setdefault(PET, []).append([iso(ds + dt.timedelta(minutes=1)), "0"])
            n = visits(i)
            for k in range(n):
                t = ds + dt.timedelta(minutes=30) + span * k / max(n, 1)
                if t >= now:
                    break
                total += 1
                alt ^= 1
                h[PET].append([iso(t), str(k + 1)])
                h.setdefault("sensor.mochi_weight", []).append([iso(t), str(round(weight_lb(i) + 0.02 * alt, 2))])
                h.setdefault(st, []).extend(
                    [
                        [iso(t), "cd"],
                        [iso(t + dt.timedelta(minutes=2)), "ccp"],
                        [iso(t + dt.timedelta(minutes=5)), "rdy"],
                    ]
                )
                h.setdefault(f"sensor.{ROBOT}_waste_drawer", []).append(
                    [iso(t + dt.timedelta(minutes=6)), str(10 + (total * 2) % 70)]
                )
            if feeder:
                fd = "sensor.acme_feeder_today_s_feeding_quantity_weight"
                h.setdefault(fd, []).append([iso(ds + dt.timedelta(minutes=1)), "0"])
                for hr, g in ((8, 10), (18, 20)):
                    if ds + dt.timedelta(hours=hr) < now:
                        h[fd].append([iso(ds + dt.timedelta(hours=hr)), str(g)])
        start = iso(day0(now, days))
        if feeder:
            h["binary_sensor.acme_feeder_food_status"] = [[start, "off"]]
            h["binary_sensor.acme_fountain_water_lack_warning"] = [[start, "off"]]
            h["binary_sensor.acme_feeder_feeding_schedule"] = [[start, "on"]]
            h["binary_sensor.acme_feeder_food_dispenser"] = [[start, "off"]]
        self.feeder = feeder
        # meals at 08:00 and 18:00 local: the feeder's last / next feed timestamps
        slots = [day0(now, i) + dt.timedelta(hours=hr) for i in (1, 0, -1) for hr in (8, 18)]
        self.last_feed = max(t for t in slots if t <= now)
        self.next_feed = min(t for t in slots if t > now)

    def add(self, eid, minutes_ago, state):
        self.hist.setdefault(eid, []).append([iso(self.now - dt.timedelta(minutes=minutes_ago)), state])
        self.hist[eid].sort()

    def last(self, eid, default="unknown"):
        return self.hist[eid][-1][1] if self.hist.get(eid) else default

    def states(self):
        def s(eid, state, name, **attrs):
            return {
                "entity_id": eid,
                "state": state,
                "name": name,
                "area": "Bathroom",
                "hidden": False,
                "attributes": attrs,
            }

        out = [
            s(
                f"sensor.{ROBOT}_waste_drawer",
                self.last(f"sensor.{ROBOT}_waste_drawer", "10"),
                "Litter-Robot 4 Waste drawer",
                unit_of_measurement="%",
            ),
            s(
                f"sensor.{ROBOT}_status_code",
                self.last(f"sensor.{ROBOT}_status_code", "rdy"),
                "Litter-Robot 4 Status code",
            ),
            s(f"sensor.{ROBOT}_litter_level", "55.0", "Litter-Robot 4 Litter level", unit_of_measurement="%"),
            s(
                f"sensor.{ROBOT}_pet_weight",
                self.last("sensor.mochi_weight", "8.2"),
                "Litter-Robot 4 Pet weight",
                unit_of_measurement="lb",
            ),
            s(f"select.{ROBOT}_globe_light", "auto", "Litter-Robot 4 Globe light"),
            s(f"sensor.{ROBOT}_sleep_mode_start_time", "unknown", "Litter-Robot 4 Sleep mode start time"),
            s(
                f"vacuum.{ROBOT}_litter_box",
                "unavailable" if self.last(f"sensor.{ROBOT}_status_code") == "unavailable" else "docked",
                "Litter-Robot 4 Litter box",
            ),
            s(PET, self.last(PET, "0"), "Mochi Visits today", unit_of_measurement="visits", state_class="total"),
            s("sensor.mochi_weight", self.last("sensor.mochi_weight", "8.2"), "Mochi Weight", unit_of_measurement="lb"),
            s("sensor.kitchen_temperature", "21.5", "Kitchen temperature", unit_of_measurement="°C"),
        ]
        if self.feeder:
            out += [
                s(
                    "binary_sensor.acme_feeder_food_status",
                    self.last("binary_sensor.acme_feeder_food_status"),
                    "Acme Feeder Food Status",
                    device_class="problem",
                ),
                s(
                    "sensor.acme_feeder_today_s_feeding_quantity_weight",
                    self.last("sensor.acme_feeder_today_s_feeding_quantity_weight", "0"),
                    "Acme Feeder Today's Feeding Quantity (Weight)",
                    unit_of_measurement="g",
                ),
                s(
                    "sensor.acme_feeder_today_s_feeding_quantity_volume",
                    "40",
                    "Acme Feeder vol",
                    unit_of_measurement="mL",
                ),
                s(
                    "sensor.acme_feeder_next_feed_time",
                    iso(self.next_feed) if self.next_feed else "unknown",
                    "Acme Feeder Next Feed Time",
                    device_class="timestamp",
                ),
                s(
                    "sensor.acme_feeder_last_feed_time",
                    iso(self.last_feed) if self.last_feed else "unknown",
                    "Acme Feeder Last Feed Time",
                    device_class="timestamp",
                ),
                s(
                    "binary_sensor.acme_feeder_feeding_schedule",
                    self.last("binary_sensor.acme_feeder_feeding_schedule"),
                    "Acme Feeder Feeding Schedule",
                ),
                s("binary_sensor.acme_feeder_today_s_feeding_schedule", "off", "Acme Feeder Today's Feeding Schedule"),
                s(
                    "binary_sensor.acme_feeder_food_dispenser",
                    self.last("binary_sensor.acme_feeder_food_dispenser"),
                    "Acme Feeder Food Dispenser",
                    device_class="problem",
                ),
                s(
                    "binary_sensor.acme_fountain_water_lack_warning",
                    self.last("binary_sensor.acme_fountain_water_lack_warning"),
                    "Acme Fountain Water lack warning",
                    device_class="problem",
                ),
            ]
        return out


class CatCorner(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.d = self.t.name

        def j(*p):
            return os.path.join(self.d, *p)

        for name, body in (("ha", HA_STUB), ("tg", TG_STUB)):
            with open(j(name), "w") as f:
                f.write(body)
            os.chmod(j(name), 0o755)
        self.db, self.tgf, self.fix, self.sd = j("db.json"), j("tg.txt"), j("hist.json"), j("state")
        self.cal = j("cal")  # calendar cache dir (calcache layout); empty = no calendar trips
        self.env = dict(
            os.environ,
            HA_BIN=j("ha"),
            TG_BIN=j("tg"),
            STUB_DB=self.db,
            STUB_TG=self.tgf,
            CAT_CORNER_STATE_DIR=self.sd,
            CAT_CORNER_CONFIG=j("cfg.json"),
            CAT_CORNER_HISTORY_FIXTURE=self.fix,
            OWNER_TZ="Europe/Zurich",
            HA_AUDIT_BIN=j("nope"),
            CALCACHE_DIR=self.cal,
        )

    def tearDown(self):
        self.t.cleanup()

    def load(self, w, down=False):
        with open(self.db, "w") as f:
            json.dump({"down": down, "states": w.states()}, f)
        with open(self.fix, "w") as f:
            json.dump(w.hist, f)

    def run_cc(self, *args, now=NOW):
        env = dict(self.env, CAT_CORNER_NOW=str(now.timestamp()))
        return subprocess.run(
            [sys.executable, BIN, *args], capture_output=True, text=True, env=env, timeout=60, stdin=subprocess.DEVNULL
        )

    def tg(self):
        try:
            with open(self.tgf) as f:
                return [m.strip() for m in f.read().split("---\n") if m.strip()]
        except OSError:
            return []

    def cron(self, w, now=None, **kw):
        now = now or w.now
        self.load(w, **kw)
        r = self.run_cc("--cron", now=now)
        return r

    # ------------------------------------------------------------------ discovery / plumbing
    def test_discover(self):
        self.load(World())
        r = self.run_cc("discover", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        e = json.loads(r.stdout)
        self.assertEqual(e["robot"]["drawer"], f"sensor.{ROBOT}_waste_drawer")
        self.assertEqual(e["robot"]["vacuum"], f"vacuum.{ROBOT}_litter_box")
        self.assertEqual([p["name"] for p in e["pets"]], ["Mochi"])
        self.assertEqual(e["pets"][0]["weight"], "sensor.mochi_weight")
        self.assertEqual(e["feeders"][0]["dispensed"], "sensor.acme_feeder_today_s_feeding_quantity_weight")
        self.assertEqual(e["feeders"][0]["food_low"], "binary_sensor.acme_feeder_food_status")
        self.assertEqual(e["fountains"][0]["water_low"], "binary_sensor.acme_fountain_water_lack_warning")
        self.load(World(feeder=False))
        e = json.loads(self.run_cc("discover", "--json").stdout)
        self.assertEqual((e["feeders"], e["fountains"]), ([], []))

    def test_normal_silent_and_history(self):
        w = World()
        r = self.cron(w)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.tg(), [])
        with open(os.path.join(self.sd, "history.jsonl")) as f:
            rows = [json.loads(x) for x in f]
        full = [x for x in rows if x["complete"]]
        self.assertGreaterEqual(len(full), 14)
        self.assertTrue(all(x["pets"][PET]["visits"] == 4 for x in full), [x["pets"][PET] for x in full])
        self.assertTrue(all(x["cycles"] == 4 for x in full))
        self.assertTrue(all(x["feeders"]["acme_feeder"]["food_g"] == 20 for x in full))
        self.assertAlmostEqual(full[-1]["pets"][PET]["weight_kg"], 8.21 * 0.45359237, places=2)
        self.assertGreaterEqual(sum(x["drawer_empties"] for x in full), 1)
        self.assertFalse(rows[-1]["complete"])  # today
        with open(self.db + ".log") as f:
            log = f.read().split("\n")
        self.assertTrue(all(x.startswith("list ") for x in log if x), log)  # reads only, never a write verb

    def test_status(self):
        self.load(World())
        r = self.run_cc("status")
        self.assertEqual(r.returncode, 0, r.stderr)
        for s in (
            "Litter-Robot 4: ready",
            "Mochi:",
            "kg",
            "usual ~4",
            "Feeder Acme Feeder",
            "food ok",
            "Fountain Acme Fountain: water ok",
            "Problems: none",
        ):
            self.assertIn(s, r.stdout)
        self.assertFalse(os.path.exists(self.sd))  # status writes nothing
        self.load(World(feeder=False))
        r = self.run_cc("status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Feeder", r.stdout)

    def test_ha_down(self):
        self.load(World(), down=True)
        r = self.run_cc("--cron")
        self.assertEqual(r.returncode, 1)
        self.assertIn("HA unreachable", r.stderr)
        self.assertEqual(self.tg(), [])

    def test_dry_run_writes_and_sends_nothing(self):
        w = World()
        w.add(f"sensor.{ROBOT}_waste_drawer", 5, "95")
        self.load(w)
        r = self.run_cc("--cron", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("would send:", r.stdout)
        self.assertIn("drawer at 95 %", r.stdout)
        self.assertEqual(self.tg(), [])
        self.assertFalse(os.path.exists(self.sd))

    # ------------------------------------------------------------------ problems
    def test_drawer_dedupe_and_renotify(self):
        w = World()
        w.add(f"sensor.{ROBOT}_waste_drawer", 5, "92")
        self.cron(w)
        self.assertEqual(len(self.tg()), 1)
        self.assertIn("drawer at 92 %", self.tg()[0])
        self.cron(w, now=NOW + dt.timedelta(minutes=10))
        self.cron(w, now=NOW + dt.timedelta(hours=5))
        self.assertEqual(len(self.tg()), 1)
        self.cron(w, now=NOW + dt.timedelta(hours=25))
        self.assertEqual(len(self.tg()), 2)

    def test_drawer_full_code(self):
        w = World()
        w.add(f"sensor.{ROBOT}_status_code", 3, "dfs")
        self.cron(w)
        self.assertEqual(len(self.tg()), 1)
        self.assertIn("drawer full", self.tg()[0])
        self.assertNotIn("fault", self.tg()[0])

    def test_quiet_hours_hold(self):
        night = dt.datetime(2026, 11, 18, 3, 0, tzinfo=TZ)
        w = World(now=night)
        w.add(f"sensor.{ROBOT}_waste_drawer", 10, "96")
        self.cron(w)
        self.assertEqual(self.tg(), [])
        self.cron(w, now=night + dt.timedelta(hours=4))
        self.assertEqual(self.tg(), [])
        self.cron(w, now=night + dt.timedelta(hours=5, minutes=5))
        self.assertEqual(len(self.tg()), 1)

    def test_fault_after_30_min(self):
        w = World()
        w.add(f"sensor.{ROBOT}_status_code", 20, "otf")
        self.cron(w)
        self.assertEqual(self.tg(), [])
        w = World()
        w.add(f"sensor.{ROBOT}_status_code", 40, "otf")
        self.cron(w)
        self.assertEqual(len(self.tg()), 1)
        self.assertIn("over-torque fault since 09:20 (40 min)", self.tg()[0])

    def test_offline(self):
        w = World()
        w.add(f"sensor.{ROBOT}_status_code", 45, "unavailable")
        self.cron(w)
        self.assertEqual(len(self.tg()), 1)
        self.assertIn("offline", self.tg()[0])

    def test_no_visit(self):
        w = World(visits=lambda i: 0 if i < 2 else 4)
        self.cron(w)
        msgs = self.tg()
        self.assertEqual(len(msgs), 1, msgs)
        self.assertIn("No litter box visit from Mochi in 40 h (last Mo 16.11 17:45)", msgs[0])
        self.assertIn("worth checking on Mochi", msgs[0])
        self.cron(w, now=NOW + dt.timedelta(hours=2))
        self.assertEqual(len(self.tg()), 1)  # once, then one reminder after 18 h
        self.cron(w, now=NOW + dt.timedelta(hours=19))  # 05:00: quiet hours, held
        self.assertEqual(len(self.tg()), 1)
        self.cron(w, now=NOW + dt.timedelta(hours=23))
        self.assertEqual(len(self.tg()), 2)

    def test_visits_high(self):
        w = World(visits=lambda i: 10 if i == 0 else 4)
        self.cron(w)
        msgs = self.tg()
        self.assertEqual(len(msgs), 1, msgs)
        self.assertIn("Mochi used the litter box 10x today (usually ~4 a day)", msgs[0])
        self.cron(w, now=NOW + dt.timedelta(minutes=30))
        self.assertEqual(len(self.tg()), 1)

    def test_visits_high_needs_baseline(self):
        w = World(days=4, visits=lambda i: 10 if i == 0 else 4)
        self.cron(w)
        self.assertEqual(self.tg(), [])

    def test_weight_change(self):
        w = World(weight_lb=lambda i: 7.6 if i < 2 else 8.2)
        self.cron(w)
        msgs = self.tg()
        self.assertEqual(len(msgs), 1, msgs)
        self.assertIn("Mochi's weight: 3.46 kg vs usual 3.72 kg (-7 %)", msgs[0])
        w = World(weight_lb=lambda i: 8.0 if i < 2 else 8.2)
        self.t.cleanup()
        self.setUp()
        self.cron(w)
        self.assertEqual(self.tg(), [])

    def test_feeder_food_low(self):
        w = World()
        w.add("binary_sensor.acme_feeder_food_status", 20, "on")
        self.cron(w)
        self.assertEqual(self.tg(), [])
        w = World()
        w.add("binary_sensor.acme_feeder_food_status", 40, "on")
        w.add("binary_sensor.acme_fountain_water_lack_warning", 50, "on")
        self.cron(w)
        msgs = self.tg()
        self.assertEqual(len(msgs), 1, msgs)  # ONE text for several problems
        self.assertIn("Acme Feeder: food low", msgs[0])
        self.assertIn("Acme Fountain: water low", msgs[0])

    def test_tg_failure_retries(self):
        w = World()
        w.add(f"sensor.{ROBOT}_waste_drawer", 5, "93")
        self.load(w)
        env_tg = self.env["TG_BIN"]
        self.env["TG_BIN"] = "/bin/false"
        r = self.run_cc("--cron")
        self.assertEqual(r.returncode, 1)
        self.env["TG_BIN"] = env_tg
        self.run_cc("--cron", now=NOW + dt.timedelta(minutes=10))
        self.assertEqual(len(self.tg()), 1)

    # ------------------------------------------------------------------ week
    def test_week(self):
        self.load(World())
        r = self.run_cc("week", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        W = json.loads(r.stdout)
        self.assertEqual(W["pets"][0]["visits"], [4] * 7)
        self.assertEqual(W["pets"][0]["visits_prev_median"], 4)
        self.assertEqual(W["robot"]["cycles"], 28)
        self.assertEqual(W["feeders"][0]["avg_g"], 20)
        self.assertEqual(W["off"], [])
        self.assertEqual(self.run_cc("week", "--digest").stdout, "")
        txt = self.run_cc("week").stdout
        self.assertIn("Mochi visits: We 4 · Th 4", txt)
        self.assertIn("Tu 4\n", txt)  # 7 days ending yesterday (a Tuesday)
        self.assertIn("cycles 28", txt)

    def test_week_digest_when_off(self):
        self.load(World(days=24, visits=lambda i: 8 if 1 <= i <= 7 else 4))
        r = self.run_cc("week", "--digest")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Mochi: visits up 100 % (8/day vs 4)", r.stdout)

    # ------------------------------------------------------------------ trip mode
    def config(self, **c):
        with open(self.env["CAT_CORNER_CONFIG"], "w") as f:
            json.dump(c, f)

    def calm(self, w):
        """World with the drawer at 30 % (its synthetic readings reach the trip drawer threshold)."""
        w.add(f"sensor.{ROBOT}_waste_drawer", 1, "30")
        return w

    def alerts_only(self):
        """tg texts without the daily check line (tests that are not about it)."""
        out = []
        for m in self.tg():
            m = "\n".join(x for x in m.split("\n") if "Daily cat check" not in x).strip()
            if m:
                out.append(m)
        return out

    def test_trip_stricter_drawer_and_renotify(self):
        w = World()
        w.add(f"sensor.{ROBOT}_waste_drawer", 5, "80")
        self.cron(w)
        self.assertEqual(self.tg(), [])  # normal mode: 80 % is fine
        self.config(trips=[TRIP], trip_thresholds={"ok_hour": 23})
        self.cron(w, now=NOW + dt.timedelta(minutes=10))
        self.assertEqual(len(self.alerts_only()), 1)
        self.assertIn("drawer at 80 %", self.alerts_only()[0])
        self.cron(w, now=NOW + dt.timedelta(hours=7))
        self.assertEqual(len(self.alerts_only()), 1)
        self.cron(w, now=NOW + dt.timedelta(hours=8, minutes=20))  # trip renotify 8 h (normal 24 h)
        self.assertEqual(len(self.alerts_only()), 2)

    def test_trip_past_or_future_is_off(self):
        w = World()
        w.add(f"sensor.{ROBOT}_waste_drawer", 5, "80")
        self.config(
            trips=[
                {"from": "2026-11-01", "to": "2026-11-10", "tz": "Asia/Dubai"},
                {"from": "2026-11-19", "to": "2026-11-30"},
                {"from": "bad"},
                {"to": "2026-11-21"},
            ]
        )
        self.cron(w)
        self.assertEqual(self.tg(), [])

    def test_trip_quiet_hours_in_trip_zone(self):
        self.config(trips=[TRIP], trip_thresholds={"ok_hour": 23})
        eve = dt.datetime(2026, 11, 18, 21, 30, tzinfo=TZ)  # 00:30 trip time: quiet there
        w = World(now=eve)
        w.add(f"sensor.{ROBOT}_waste_drawer", 10, "93")
        self.cron(w)
        self.assertEqual(self.tg(), [])
        early = dt.datetime(2026, 11, 19, 5, 10, tzinfo=TZ)  # 08:10 trip time: no longer quiet there
        self.cron(w, now=early)
        self.assertEqual(len(self.alerts_only()), 1)
        self.assertIn("drawer at 93 %", self.alerts_only()[0])

    def test_trip_times_shown_in_trip_zone(self):
        self.config(trips=[TRIP], trip_thresholds={"ok_hour": 23})
        w = World()
        w.add(f"sensor.{ROBOT}_status_code", 40, "otf")
        self.cron(w)
        self.assertIn("over-torque fault since 12:20 (40 min)", self.alerts_only()[0])  # 09:20 home = 12:20 away
        r = self.run_cc("status")
        self.assertIn("Trip mode on until 25.11 (times in Asia/Dubai", r.stdout)
        self.assertIn("· We 18.11 13:00", r.stdout)

    def test_trip_daily_check(self):
        self.config(trips=[TRIP])
        before = dt.datetime(2026, 11, 18, 5, 50, tzinfo=TZ)  # 08:50 trip time
        w = self.calm(World(now=before))
        w.next_feed = None  # a static world never feeds: keep the meal check out of this test
        self.cron(w)
        self.assertEqual(self.tg(), [])
        self.cron(w, now=before + dt.timedelta(minutes=20))  # 09:10 trip time
        msgs = self.tg()
        self.assertEqual(len(msgs), 1, msgs)
        self.assertIn("🐾 Daily cat check: all OK", msgs[0])
        self.assertIn("Mochi: 4 visits yesterday, last", msgs[0])
        self.assertIn("drawer", msgs[0])
        self.assertIn("fed yesterday 21:00", msgs[0])  # 18:00 home = 21:00 trip time
        self.assertIn("water ok", msgs[0])
        self.assertEqual(len(msgs[0].split("\n")), 1)
        self.assertEqual(open(self.tgf + ".soft").read(), "1\n")  # the daily check alone: tg --soft
        self.cron(w, now=before + dt.timedelta(hours=3))
        self.assertEqual(len(self.tg()), 1, self.tg())  # once a day
        self.cron(w, now=before + dt.timedelta(days=1, minutes=20))
        self.assertEqual(len(self.tg()), 2)

    def test_trip_daily_check_lists_open_problems(self):
        self.config(trips=[TRIP])
        w = World()
        w.add(f"sensor.{ROBOT}_waste_drawer", 5, "82")
        self.cron(w)
        msgs = self.tg()
        self.assertEqual(len(msgs), 1, msgs)  # ONE text: the alert + the daily line
        self.assertIn("drawer at 82 %", msgs[0])
        self.assertIn("Daily cat check: 1 open (drawer)", msgs[0])
        self.assertEqual(open(self.tgf + ".soft").read(), "\n")  # a cat problem in it: plain tg (urgent)

    def test_trip_no_visit_earlier(self):
        w = World(visits=lambda i: 0 if i < 1 else 4)  # last visit yesterday 17:45 = 16 h + today's 10 h
        self.cron(w)
        self.assertEqual(self.tg(), [])  # normal: 30 h
        self.config(trips=[TRIP], trip_thresholds={"ok_hour": 23})
        self.cron(w, now=NOW + dt.timedelta(minutes=10))
        self.assertEqual(self.tg(), [])  # 16 h: still under 20 h
        self.cron(w, now=NOW + dt.timedelta(hours=4, minutes=30))
        self.assertEqual(len(self.alerts_only()), 1)
        self.assertIn("No litter box visit from Mochi in 20 h", self.alerts_only()[0])

    def test_trip_meal_not_dispensed(self):
        self.config(trips=[TRIP], trip_thresholds={"ok_hour": 23})
        w = self.calm(World())  # 10:00, next meal 18:00
        self.cron(w)
        with open(os.path.join(self.sd, "state.json")) as f:
            st = json.load(f)
        self.assertEqual(st["meals"]["acme_feeder"]["pending"], [iso(w.next_feed)])
        meal = w.next_feed
        w.next_feed = meal + dt.timedelta(hours=14)  # the feeder moved on to tomorrow 08:00, nothing dispensed
        self.cron(w, now=meal + dt.timedelta(minutes=30))
        self.assertEqual(self.tg(), [])  # within the 45 min grace
        self.cron(w, now=meal + dt.timedelta(minutes=50))
        msgs = self.alerts_only()
        self.assertEqual(len(msgs), 1, msgs)
        self.assertIn("Acme Feeder: the 21:00 meal was not dispensed", msgs[0])
        self.cron(w, now=meal + dt.timedelta(minutes=60))
        self.assertEqual(len(self.alerts_only()), 1)
        w.last_feed = meal + dt.timedelta(minutes=65)  # someone fed by hand: the problem ends
        self.cron(w, now=meal + dt.timedelta(minutes=70))
        with open(os.path.join(self.sd, "state.json")) as f:
            st = json.load(f)
        self.assertNotIn("missed", st["meals"]["acme_feeder"])
        self.assertFalse(st["alerts"]["meal:acme_feeder"]["active"])

    def test_trip_meal_dispensed_is_silent(self):
        self.config(trips=[TRIP], trip_thresholds={"ok_hour": 23})
        w = self.calm(World())
        self.cron(w)
        meal = w.next_feed
        w.next_feed, w.last_feed = meal + dt.timedelta(hours=14), meal + dt.timedelta(seconds=40)
        self.cron(w, now=meal + dt.timedelta(minutes=50))
        self.assertEqual(self.tg(), [])
        with open(os.path.join(self.sd, "state.json")) as f:
            m = json.load(f)["meals"]["acme_feeder"]
        self.assertEqual(m["pending"], [iso(w.next_feed)])
        self.assertNotIn("missed", m)

    def test_trip_schedule_off_dispenser_offline(self):
        w = World()
        w.add("binary_sensor.acme_feeder_feeding_schedule", 20, "off")
        w.add("binary_sensor.acme_feeder_food_dispenser", 20, "on")
        w.add("binary_sensor.acme_fountain_water_lack_warning", 40, "unavailable")
        self.cron(w)
        self.assertEqual(self.tg(), [])  # trip-only checks
        self.t.cleanup()
        self.setUp()
        self.config(trips=[TRIP], trip_thresholds={"ok_hour": 23})
        self.cron(w)
        msgs = self.alerts_only()
        self.assertEqual(len(msgs), 1, msgs)
        self.assertIn("feeding schedule is OFF", msgs[0])
        self.assertIn("dispenser problem", msgs[0])
        self.assertIn("Acme Fountain offline since 12:20", msgs[0])

    def test_trip_ha_down(self):
        self.config(trips=[TRIP], trip_thresholds={"ok_hour": 23})
        w = World()
        r = self.cron(w, down=True)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.tg(), [])
        self.cron(w, now=NOW + dt.timedelta(hours=1), down=True)
        self.assertEqual(self.tg(), [])
        self.cron(w, now=NOW + dt.timedelta(hours=2, minutes=5), down=True)
        msgs = self.tg()
        self.assertEqual(len(msgs), 1, msgs)
        self.assertIn("Home Assistant unreachable since 13:00", msgs[0])
        self.cron(w, now=NOW + dt.timedelta(hours=3), down=True)
        self.assertEqual(len(self.tg()), 1)  # once per outage
        self.cron(w, now=NOW + dt.timedelta(hours=3, minutes=10))
        self.assertEqual(len(self.tg()), 2)
        self.assertIn("reachable again", self.tg()[1])
        self.cron(w, now=NOW + dt.timedelta(hours=3, minutes=20))
        self.assertEqual(len(self.tg()), 2)

    def test_trip_daily_check_when_ha_down(self):
        self.config(trips=[TRIP])
        t = dt.datetime(2026, 11, 18, 6, 10, tzinfo=TZ)  # 09:10 trip time
        r = self.cron(World(now=t), down=True)
        self.assertEqual(r.returncode, 1)
        msgs = self.tg()
        self.assertEqual(len(msgs), 1, msgs)
        self.assertIn("Daily cat check: can't confirm, Home Assistant unreachable", msgs[0])
        self.cron(World(now=t), now=t + dt.timedelta(minutes=10), down=True)
        self.assertEqual(len(self.tg()), 1)

    def test_ha_down_outside_trip_writes_nothing(self):
        self.cron(World(), down=True)
        self.assertFalse(os.path.exists(self.sd))

    def test_trip_status_cmd(self):
        self.load(World())
        r = self.run_cc("trip", "status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("no upcoming trip", r.stdout)
        self.config(trips=[TRIP], thresholds={"litter_low_pct": 50})
        r = self.run_cc("trip", "status", now=NOW - dt.timedelta(days=5))
        self.assertIn("next trip 16.11–25.11 (Asia/Dubai, hand-entered), starts in 3 days", r.stdout)
        r = self.run_cc("trip", "status")
        for s in (
            "Trip mode: ON, 16.11–25.11 (hand-entered) · now 13:00 Asia/Dubai",
            "drawer ≥ 75 % (normal 90)",
            "litter ≤ 50 % (normal 50)",
            "no visit > 20 h (normal 30)",
            "Quiet hours: 00:00–08:00 trip time (= 21:00–05:00 home time)",
            "Daily check: 09:00 trip time",
            "meal not dispensed",
            "feeding schedule off",
            "dispenser problem",
        ):
            self.assertIn(s, r.stdout)
        self.assertNotIn("Not possible", r.stdout)
        J = json.loads(self.run_cc("trip", "status", "--json").stdout)
        self.assertTrue(J["active"])
        self.assertEqual(J["thresholds"]["drawer_pct"], {"away": 75, "normal": 90})
        self.assertTrue(all(c["available"] for c in J["checks"]))
        self.load(World(feeder=False))
        r = self.run_cc("trip", "status")
        self.assertIn("Not possible (no entity): meal not dispensed", r.stdout)
        self.load(World(), down=True)
        r = self.run_cc("trip", "status")
        self.assertEqual(r.returncode, 0)
        self.assertIn("HA unreachable", r.stdout)
        self.assertFalse(os.path.exists(self.sd))  # status writes nothing
        self.assertEqual(self.run_cc("week", "status").returncode, 2)

    # ------------------------------------------------------------------ calendar trips (core.away)
    def calendar(self, *events):
        os.makedirs(self.cal, exist_ok=True)
        with open(os.path.join(self.cal, "events.json"), "w") as f:
            json.dump({"version": 1, "tz": "Europe/Zurich", "events": list(events)}, f)

    @staticmethod
    def cal_ev(a, b, away=True, abroad=False):
        """A calcache event: all-day for YYYY-MM-DD (end exclusive), else timed (local ISO)."""
        return {
            "id": "e",
            "start": a,
            "end": b,
            "all_day": len(a) == 10,
            "title": "t",
            "town": None,
            "free": False,
            "flags": {
                "travel": None,
                "busy_evening": False,
                "away_overnight": away,
                "abroad": abroad,
                "id_check": False,
            },
        }

    def two_nights_away(self):
        """A 2-night stay (all-day 21.-23.11, end exclusive), an evening flight on 22.11 that lands before midnight,
        an unflagged all-day marker and a lone night out a week earlier (one night: no trip)."""
        e = self.cal_ev
        self.calendar(
            e("2026-11-21", "2026-11-23", abroad=True),
            e("2026-11-22", "2026-11-23", away=False, abroad=True),
            e("2026-11-22T20:15:00+01:00", "2026-11-22T21:45:00+01:00"),
            e("2026-11-14T20:00:00+01:00", "2026-11-15T06:00:00+01:00"),
        )

    def test_calendar_trip_status(self):
        self.load(World())
        self.two_nights_away()
        r = self.run_cc("trip", "status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("next trip 21.11–23.11 (home time, calendar, 2 nights), starts in 3 days", r.stdout)
        self.assertNotIn("Upcoming", r.stdout)
        J = json.loads(self.run_cc("trip", "status", "--json").stdout)
        self.assertFalse(J["active"])
        self.assertEqual(
            J["trip"], {"from": "2026-11-21", "to": "2026-11-23", "tz": None, "source": "calendar", "nights": 2}
        )
        on = dt.datetime(2026, 11, 23, 10, 0, tzinfo=TZ)  # the day back is still in the window
        self.assertIn(
            "Trip mode: ON, 21.11–23.11 (calendar, 2 nights) · now 10:00 home time",
            self.run_cc("trip", "status", now=on).stdout,
        )
        self.assertIn("Trip mode: off", self.run_cc("trip", "status", now=on + dt.timedelta(days=1)).stdout)
        self.config(calendar_min_nights=3)
        self.assertIn("no upcoming trip", self.run_cc("trip", "status").stdout)
        self.config(calendar_min_nights=0)
        self.assertIn("no upcoming trip", self.run_cc("trip", "status").stdout)

    def test_calendar_trip_runs_trip_mode(self):
        t = dt.datetime(2026, 11, 22, 8, 40, tzinfo=TZ)
        w = World(now=t)
        w.add(f"sensor.{ROBOT}_waste_drawer", 5, "80")
        w.next_feed = None
        self.cron(w)
        self.assertEqual(self.tg(), [])  # no trip: 80 % is fine
        self.two_nights_away()
        self.cron(w, now=t + dt.timedelta(minutes=10))
        self.assertEqual(len(self.alerts_only()), 1, self.tg())
        self.assertIn("drawer at 80 %", self.alerts_only()[0])
        self.assertFalse(any("Daily cat check" in m for m in self.tg()))  # 09:00 home time not reached
        self.cron(w, now=t + dt.timedelta(minutes=30))  # 09:10 home time
        self.assertTrue(any("Daily cat check" in m for m in self.tg()), self.tg())

    def test_hand_entered_trip_wins_on_overlap(self):
        self.load(World())
        e = self.cal_ev
        # overlaps the hand-entered 16.-25.11 (Asia/Dubai): dropped; 28.-30.11 does not: kept
        self.calendar(e("2026-11-24", "2026-11-27"), e("2026-11-28", "2026-11-30"))
        self.config(trips=[TRIP])
        r = self.run_cc("trip", "status")
        self.assertIn("Trip mode: ON, 16.11–25.11 (hand-entered) · now 13:00 Asia/Dubai", r.stdout)
        self.assertIn("Upcoming: 16.11–25.11 hand-entered · 28.11–30.11 calendar, 2 nights", r.stdout)
        self.assertNotIn("24.11", r.stdout)
        later = dt.datetime(2026, 11, 26, 10, 0, tzinfo=TZ)  # inside the dropped calendar trip only
        self.assertIn("Trip mode: off · next trip 28.11–30.11", self.run_cc("trip", "status", now=later).stdout)
        J = json.loads(self.run_cc("trip", "status", "--json").stdout)
        self.assertEqual(
            [(u["from"], u["source"]) for u in J["upcoming"]], [("2026-11-16", "config"), ("2026-11-28", "calendar")]
        )

    def test_broken_calendar_cache_is_no_trip(self):
        self.load(World())
        os.makedirs(self.cal)
        with open(os.path.join(self.cal, "events.json"), "w") as f:
            f.write("{broken")
        r = self.run_cc("trip", "status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("no upcoming trip", r.stdout)


class Units(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        loader = importlib.machinery.SourceFileLoader("cat_corner_mod", BIN)
        spec = importlib.util.spec_from_loader("cat_corner_mod", loader)
        cls.m = importlib.util.module_from_spec(spec)
        sys.dont_write_bytecode = True
        loader.exec_module(cls.m)

    def test_json_floats_survive_the_privacy_gate(self):
        # a weight baseline median like 4.0315 has 4 decimals: the gate would print it as <coord> (broken JSON for
        # the voice relay's quick tool cat); jsonable rounds floats to 3 decimals first
        J = self.m.jsonable({"pets": [{"weight_base": (4.0315, 2), "weight_now": 3.824, "n": 3}]})
        out = self.m.gate(json.dumps(J))
        self.assertNotIn("<coord>", out)
        self.assertEqual(json.loads(out)["pets"][0]["weight_base"], [4.032, 2])

    def test_counter_resets(self):
        t0 = dt.datetime(2026, 11, 1, tzinfo=UTC)

        def m(x):
            return t0 + dt.timedelta(minutes=x)

        ser = [(t0, "5"), (m(5), "0"), (m(60), "1"), (m(70), "unavailable"), (m(80), "3"), (m(90), "1")]
        incs = self.m.counter_incs(ser, t0)
        self.assertEqual([v for _, v in incs], [1, 2, 1])
        # a series that starts inside the window counts its first value (the day's count so far)
        self.assertEqual([v for _, v in self.m.counter_incs([(m(30), "2"), (m(40), "3")], t0)], [2, 1])

    def test_trip_thresholds(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "cfg.json")
            with open(p, "w") as f:
                json.dump(
                    {
                        "thresholds": {"litter_low_pct": 60, "drawer_pct": 70},
                        "trip_thresholds": {"renotify_h": 12},
                        "tz": "Asia/Dubai",
                        "trips": [
                            {"from": "2026-12-01", "to": "2026-12-03"},
                            {"from": "2026-11-01", "to": "2026-11-02", "tz": "Nowhere/Bad"},
                            {"from": "2026-11-05", "to": "2026-11-04"},
                        ],
                    },
                    f,
                )
            old, self.m.CONFIG = self.m.CONFIG, p
            try:
                c = self.m.cfg()
            finally:
                self.m.CONFIG = old
        tt = c["trip_thresholds"]
        self.assertEqual((tt["litter_low_pct"], tt["drawer_pct"], tt["renotify_h"]), (60, 70, 12))  # stricter wins
        self.assertEqual((tt["no_visit_h"], tt["low_min"], tt["ok_hour"]), (20, 10, 9))
        self.assertEqual(c["thresholds"]["no_visit_h"], 30)
        self.assertEqual(
            [(str(t["from"]), t["tz_name"]) for t in c["trips"]], [("2026-11-01", None), ("2026-12-01", "Asia/Dubai")]
        )
        on = self.m.active_trip(c["trips"], dt.datetime(2026, 12, 3, 22, 59, tzinfo=UTC), TZ)
        self.assertEqual(str(on["to"]), "2026-12-03")
        self.assertIsNone(self.m.active_trip(c["trips"], dt.datetime(2026, 12, 3, 23, 1, tzinfo=UTC), TZ))

    def test_discover_trip_entities(self):
        states = {e["entity_id"]: e for e in World().states()}
        f = self.m.discover(states)["feeders"]
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["schedule"], "binary_sensor.acme_feeder_feeding_schedule")  # not "today's"
        self.assertEqual(f[0]["dispenser"], "binary_sensor.acme_feeder_food_dispenser")
        self.assertEqual(f[0]["last_feed"], "sensor.acme_feeder_last_feed_time")

    def test_cfg_trips_stay_hand_entered(self):
        """cat-supplies reads cfg()['trips'] for its default trip: calendar trips only join 'all_trips'."""
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "cal"))
            with open(os.path.join(d, "cal", "events.json"), "w") as f:
                json.dump({"events": [CatCorner.cal_ev("2026-11-05", "2026-11-07")]}, f)
            with open(os.path.join(d, "cfg.json"), "w") as f:
                json.dump({"trips": [TRIP]}, f)
            old = self.m.CONFIG, self.m.CALCACHE
            self.m.CONFIG, self.m.CALCACHE = os.path.join(d, "cfg.json"), os.path.join(d, "cal", "events.json")
            try:
                c = self.m.cfg()
            finally:
                self.m.CONFIG, self.m.CALCACHE = old
        self.assertEqual([t["source"] for t in c["trips"]], ["config"])
        self.assertEqual(
            [(t["from"].isoformat(), t["to"].isoformat(), t["source"]) for t in c["all_trips"]],
            [("2026-11-05", "2026-11-07", "calendar"), ("2026-11-16", "2026-11-25", "config")],
        )

    def test_quiet(self):
        th = dict(self.m.DEFAULTS)
        self.assertTrue(self.m.is_quiet(dt.datetime(2026, 11, 18, 7, 59, tzinfo=TZ), TZ, th))
        self.assertFalse(self.m.is_quiet(dt.datetime(2026, 11, 18, 8, 0, tzinfo=TZ), TZ, th))


if __name__ == "__main__":
    unittest.main(verbosity=1)
