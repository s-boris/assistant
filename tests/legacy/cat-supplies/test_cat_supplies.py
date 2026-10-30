#!/usr/bin/env python3
"""Tests for ~/bin/cat-supplies (fixtures only: stub ha + shop, history fixture; no HA, no shopping list)."""

import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

BIN = os.environ.get("CAT_SUPPLIES_BIN", os.path.expanduser("~/bin/cat-supplies"))
CC = os.path.expanduser("~/bin/cat-corner")
UTC = dt.UTC
NOW = dt.datetime(2026, 10, 20, 10, 0, tzinfo=UTC)

HA_STUB = """#!/usr/bin/env python3
import json, os, sys
if os.environ.get('STUB_HA_DOWN'):
    sys.exit(1)
print(open(os.environ['STUB_DB']).read())
"""
SHOP_STUB = """#!/usr/bin/env python3
import json, os, sys
with open(os.environ['STUB_SHOP_LOG'], 'a') as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1] == 'list':
    print(os.environ.get('STUB_SHOP_LIST', ''))
else:
    print('added #9 ' + sys.argv[-1])
"""


def st(eid, state, unit=None, name=None):
    a = {"unit_of_measurement": unit} if unit else {}
    return {"entity_id": eid, "state": str(state), "attributes": a, "name": name or eid}


STATES = [
    st("sensor.lr4_waste_drawer", 40, "%", "LR4 Waste drawer"),
    st("sensor.lr4_litter_level", 80, "%"),
    st("sensor.lr4_status_code", "rdy"),
    st("sensor.lr4_pet_weight", 8.8, "lb"),
    st("sensor.mia_visits_today", 3),
    st("sensor.mia_weight", 4.0, "kg"),
    st("binary_sensor.granary_feeder_food_status", "off"),
    st("sensor.granary_feeder_today_s_feeding_quantity_weight", 0, "g"),
    st("sensor.granary_feeder_today_s_feeding_times", 0),
    st("sensor.granary_feeder_next_feed_time", "2026-10-20T11:00:00+00:00"),
    st("sensor.granary_feeder_next_feed_quantity_weight", 20, "g"),
    st("sensor.granary_feeder_desiccant_remaining_days", 5, "d"),
    st("number.granary_feeder_desiccant_frequency", 14, "Days"),
    st("sensor.granary_feeder_battery_level", "low"),
    st("binary_sensor.fount_water_lack_warning", "off"),
    st("sensor.fount_filter_remaining", 50, "%"),
    st("binary_sensor.fount_replace_filter", "off"),
]


def hist():
    t0 = NOW - dt.timedelta(days=4)

    def h(d, hh=0):
        return (t0 + dt.timedelta(days=d, hours=hh)).isoformat()

    return {
        # 40 g/day: two 20 g meals each day, counter resets at midnight
        "sensor.granary_feeder_today_s_feeding_quantity_weight": sum(
            [[[h(d), "0"], [h(d, 8), "20"], [h(d, 18), "40"]] for d in range(4)], []
        ),
        # litter: 60 -> 52 (2 days), top-up to 90, 90 -> 82 (2 days): 16 % in 4 days = 4 %/day
        "sensor.lr4_litter_level": [
            [h(0), "60"],
            [h(1), "56"],
            [h(2), "52"],
            [h(2, 1), "90"],
            [h(3), "86"],
            [(NOW - dt.timedelta(minutes=1)).isoformat(), "82"],
        ],
        # drawer: 70 -> 80, emptied to 0, 0 -> 10 (unavailable ignored): 20 % in 4 days = 5 %/day
        "sensor.lr4_waste_drawer": [
            [h(0), "70"],
            [h(1), "80"],
            [h(2), "0"],
            [h(2, 5), "unavailable"],
            [(NOW - dt.timedelta(minutes=1)).isoformat(), "10"],
        ],
    }


class T(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="cat-supplies-test-")
        self.addCleanup(shutil.rmtree, self.d, True)

        def j(n):
            return os.path.join(self.d, n)

        for n, body in (("ha", HA_STUB), ("shop", SHOP_STUB)):
            with open(j(n), "w") as f:
                f.write(body)
            os.chmod(j(n), 0o755)
        with open(j("db.json"), "w") as f:
            json.dump(STATES, f)
        with open(j("hist.json"), "w") as f:
            json.dump(hist(), f)
        self.cfgp, self.log = j("config.json"), j("shop.log")
        self.config({})
        self.env = dict(
            os.environ,
            HA_BIN=j("ha"),
            SHOP_BIN=j("shop"),
            STUB_DB=j("db.json"),
            STUB_SHOP_LOG=self.log,
            CAT_CORNER_HISTORY_FIXTURE=j("hist.json"),
            CAT_CORNER_CONFIG=self.cfgp,
            CAT_CORNER_STATE_DIR=j("state"),
            CAT_CORNER_BIN=CC,
            OWNER_TZ="Europe/Zurich",
            CAT_CORNER_NOW=str(NOW.timestamp()),
            HA_AUDIT_BIN=j("nope"),
        )

    def config(self, supplies, trips=True):
        c = {"supplies": supplies}
        if trips:
            c["trips"] = [{"from": "2026-11-10", "to": "2026-11-26", "tz": "Asia/Tokyo"}]
        with open(self.cfgp, "w") as f:
            json.dump(c, f)

    def run_(self, *args, **env):
        return subprocess.run(
            [sys.executable, BIN, *args], capture_output=True, text=True, env=dict(self.env, **env), timeout=60
        )

    def js(self, *args, **env):
        r = self.run_("--json", *args, **env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def items(self, o):
        return {it["key"]: it for it in o["items"]}

    def shop_calls(self):
        try:
            with open(self.log) as f:
                return [json.loads(x) for x in f]
        except OSError:
            return []

    def test_rates_and_needs(self):
        o = self.js()
        R, I = o["rates"], self.items(o)  # noqa: E741
        self.assertEqual(o["trip"]["days"], 16)
        self.assertAlmostEqual(R["feeder_g_day"], 40, delta=0.5)
        self.assertAlmostEqual(R["litter_pct_day"], 4.0, delta=0.1)
        self.assertAlmostEqual(R["drawer_pct_day"], 5.0, delta=0.1)
        self.assertAlmostEqual(I["dry_food"]["need"], 0.77, delta=0.01)  # 40 g x 16 x 1.2
        self.assertAlmostEqual(I["litter"]["need"], 3.5, delta=0.05)  # 4 % x 16 x 1.2 x 0.045 kg
        self.assertEqual(I["drawer_bags"]["need"], 3)  # 96 % -> 1 sitter change
        self.assertEqual(R["litter_topups"], 3)  # 64 % / 30 % band
        # filter 50 % at 30 days: empty in 15 d (04.11); that one still has 80 % on departure, enough for 16 d + 20 %
        self.assertEqual(R["filter_changes"], ["2026-11-04"])
        self.assertEqual(I["filter"]["need"], 2)
        o2 = self.js("--trip", "2026-11-20:2026-12-08")  # longer and later: a fresh filter on departure too
        self.assertEqual(o2["rates"]["filter_changes"], ["2026-11-04", "2026-11-20"])
        self.assertEqual(I["desiccant"]["need"], 3)  # now-ish + departure + mid-trip
        self.assertIn("batteries", I)
        self.assertTrue(all(it["status"] == "unknown" for it in o["items"]))

    def test_low_feeder_share_asks_about_wet_food(self):
        h = hist()
        h["sensor.granary_feeder_today_s_feeding_quantity_weight"] = [
            [(NOW - dt.timedelta(days=3)).isoformat(), "0"],
            [(NOW - dt.timedelta(days=2)).isoformat(), "10"],
            [(NOW - dt.timedelta(days=1)).isoformat(), "0"],
            [(NOW - dt.timedelta(hours=2)).isoformat(), "20"],
        ]
        with open(self.env["CAT_CORNER_HISTORY_FIXTURE"], "w") as f:
            json.dump(h, f)
        o = self.js()
        self.assertTrue(any("wet food" in q for q in o["questions"]))
        self.config({"wet_per_day": 2})
        self.assertEqual(self.items(self.js())["wet_food"]["need"], 39)  # 2 x 16 x 1.2 -> ceil

    def test_stock_ok_short_and_questions(self):
        self.config(
            {
                "stock": {
                    "litter_kg": 10,
                    "fountain_filters": 1,
                    "dry_food_kg": 2,
                    "drawer_bags": 50,
                    "desiccant_packs": 5,
                    "feeder_batteries": 3,
                }
            }
        )
        o = self.js()
        I = self.items(o)  # noqa: E741
        self.assertEqual(I["litter"]["status"], "ok")
        self.assertEqual((I["filter"]["status"], I["filter"]["short"]), ("short", 1))
        self.assertFalse(any(q.startswith("What is at home") for q in o["questions"]))
        lines = o["shop_lines"]
        self.assertEqual([ln["key"] for ln in lines], ["filter"])

    def test_shop_adds_only_known_short_lines(self):
        self.config({"stock": {"fountain_filters": 1}, "shop_note": "trip; 11.11 basket"})
        r = self.run_("--shop")
        self.assertEqual(r.returncode, 0, r.stderr)
        adds = [c for c in self.shop_calls() if c[0] == "add"]
        self.assertEqual(len(adds), 1)
        self.assertIn("PetKit Eversweet 3 Pro replacement filters", adds[0])
        self.assertIn("--note", adds[0])
        self.assertIn("11.11", adds[0][adds[0].index("--note") + 1])

    def test_shop_dry_run_and_assume_none_and_on_list(self):
        r = self.run_("--shop", "--assume-none", "--dry-run", STUB_SHOP_LIST="#1 Desiccant packs (feeder) · Galaxus")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse([c for c in self.shop_calls() if c[0] == "add"])  # dry run: nothing added
        would = [x for x in r.stdout.splitlines() if x.startswith("would run:")]
        self.assertEqual(len(would), 5)  # all but desiccant (on the list)
        self.assertFalse(any("Desiccant" in x for x in would))
        self.assertTrue(
            any('"Clumping clay cat litter (Litter-Robot 4), ~3.5 kg"' in x and "--qty" not in x for x in would)
        )
        self.assertTrue(any("--qty 2 " in x and "filters" in x for x in would))
        self.assertIn("already on the shopping list", r.stdout)

    def test_card_text(self):
        r = self.run_("--trip", "2026-11-10:2026-11-26")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("trip 10.11–26.11 (16 days, +20 %)", r.stdout)
        self.assertIn("Ask him (not yet)", r.stdout)
        self.assertIn("Shopping lines (dry run)", r.stdout)

    def test_marks_from_nfc_tags(self):
        mp = os.path.join(os.path.dirname(self.cfgp), "marks.json")
        at = (NOW - dt.timedelta(days=2)).astimezone().strftime("%Y-%m-%d %H:%M")
        r = self.run_("mark", "filter", "--at", at, CAT_SUPPLIES_MARKS=mp)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(oct(os.stat(mp).st_mode & 0o777), "0o600")
        self.assertEqual(self.run_("mark", "desiccant", "--at", at, CAT_SUPPLIES_MARKS=mp).returncode, 0)
        self.assertEqual(self.run_("mark", "nope", CAT_SUPPLIES_MARKS=mp).returncode, 2)
        r = self.run_("marks", "--json", CAT_SUPPLIES_MARKS=mp)
        self.assertEqual(set(json.loads(r.stdout)), {"filter", "desiccant"})
        o = self.js(CAT_SUPPLIES_MARKS=mp)
        R = o["rates"]
        self.assertAlmostEqual(R["filter_pct_now"], 93.3, delta=0.5)  # 2 of 30 days since the marked change
        self.assertTrue(any("PetKit app" in c for c in o["checks"]))
        self.assertIn("filter", R["marks"])
        if R.get("desiccant_days_left") is not None:
            self.assertGreater(R["desiccant_days_left"], 0)
        txt = self.run_(CAT_SUPPLIES_MARKS=mp).stdout
        self.assertIn("🏷 Tags:", txt)
        # no marks file: forecast unchanged (HA's 50 %)
        self.assertAlmostEqual(self.js(CAT_SUPPLIES_MARKS=mp + ".none")["rates"]["filter_pct_now"], 50, delta=0.1)

    def test_usage_and_ha_down(self):
        self.assertEqual(self.run_("--trip", "2026-11-26:2026-11-10").returncode, 2)
        self.assertEqual(self.run_("--trip", "soon").returncode, 2)
        self.config({}, trips=False)
        self.assertEqual(self.run_().returncode, 2)
        self.assertEqual(self.run_("--trip", "2026-11-10:2026-11-26", STUB_HA_DOWN="1").returncode, 1)


if __name__ == "__main__":
    unittest.main()
