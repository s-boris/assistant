#!/usr/bin/env python3
"""Tests for ~/bin/cat-autopilot + lib/catstock.py (fixtures only: stub ha + shop, history fixture; no HA, no list).
Run: python3 ~/opt/cat-autopilot-tests/test_cat_autopilot.py  (CAT_AUTOPILOT_BIN / TG_CMD_BIN to test copies)"""

import datetime as dt
import json
import os
import shutil
import subprocess
import tempfile
import unittest

BIN = os.environ.get("CAT_AUTOPILOT_BIN", os.path.expanduser("~/bin/cat-autopilot"))
TGCMD = os.environ.get("TG_CMD_BIN", os.path.expanduser("~/bin/tg-cmd"))
UTC = dt.UTC
NOW = dt.datetime(2026, 10, 21, 10, 0, tzinfo=UTC)
DAY = 86400

HA_STUB = """#!/usr/bin/env python3
import os, sys
if os.environ.get('STUB_HA_DOWN'):
    sys.exit(1)
print(open(os.environ['STUB_DB']).read())
"""
SHOP_STUB = """#!/usr/bin/env python3
import json, os, sys
with open(os.environ['STUB_SHOP_LOG'], 'a') as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
a = sys.argv[1:]
if a[:1] == ['list']:
    print(os.environ.get('STUB_SHOP_LIST', '[]'))
elif a[:1] == ['plan']:
    print(os.environ.get('STUB_SHOP_PLAN', '{"online": []}'))
elif a[:1] == ['add']:
    n = sum(1 for _ in open(os.environ['STUB_SHOP_LOG']))
    print(json.dumps({"id": 100 + n, "text": a[-1]}))
else:
    print('ok')
"""


def st(eid, state, unit=None):
    return {
        "entity_id": eid,
        "state": str(state),
        "attributes": {"unit_of_measurement": unit} if unit else {},
        "name": eid,
    }


def states(filter_pct=30, desic=-5, battery="low", litter=80, drawer=60):
    return [
        st("sensor.lr4_waste_drawer", drawer, "%"),
        st("sensor.lr4_litter_level", litter, "%"),
        st("sensor.lr4_status_code", "rdy"),
        st("sensor.lr4_pet_weight", 8.6, "lb"),
        st("sensor.mia_visits_today", 3),
        st("sensor.mia_weight", 3.9, "kg"),
        st("binary_sensor.granary_feeder_food_status", "off"),
        st("sensor.granary_feeder_today_s_feeding_quantity_weight", 0, "g"),
        st("sensor.granary_feeder_today_s_feeding_times", 0),
        st("sensor.granary_feeder_next_feed_time", "2026-10-21T11:00:00+00:00"),
        st("sensor.granary_feeder_next_feed_quantity_weight", 10, "g"),
        st("sensor.granary_feeder_desiccant_remaining_days", desic, "d"),
        st("number.granary_feeder_desiccant_frequency", 14, "Days"),
        st("sensor.granary_feeder_battery_level", battery),
        st("binary_sensor.fount_water_lack_warning", "off"),
        st("sensor.fount_filter_remaining", filter_pct, "%"),
        st("sensor.fount_last_update", (NOW - dt.timedelta(days=3)).isoformat()),
        st("binary_sensor.fount_replace_filter", "off"),
    ]


def hist(extra=None):
    def h(d, hh=0):
        return (NOW - dt.timedelta(days=d) + dt.timedelta(hours=hh)).isoformat()

    out = {
        # 50 -> 46 over a day, top-up to 90 (3 d ago), then a noisy fall to ~78-80: about -4 %/day
        "sensor.lr4_litter_level": [
            [h(4), "50"],
            [h(3, -2), "46"],
            [h(3), "90"],
            [h(2, 12), "88"],
            [h(2), "86"],
            [h(1, 18), "80"],
            [h(1, 12), "84"],
            [h(1), "80"],
            [h(0, -12), "76"],
            [h(0, -1), "80"],
        ],
        "sensor.lr4_waste_drawer": [[h(4), "40"], [h(2), "50"], [h(0, -1), "60"]],
        "sensor.granary_feeder_today_s_feeding_quantity_weight": sum(
            [[[h(d), "0"], [h(d, 3), "10"]] for d in range(4, 0, -1)], []
        ),
        "sensor.fount_filter_remaining": [[h(3), "30"]],
        "sensor.granary_feeder_desiccant_remaining_days": [[h(3), "-2"], [h(2), "-3"], [h(1), "-4"], [h(0, -1), "-5"]],
    }
    out.update(extra or {})
    return out


CATALOGUE = {
    "checked": "21.10",
    "battery_type": "3x D alkaline",
    "shops": {
        "galaxus": {"name": "Galaxus", "delivery_days": 1},
        "brack": {"name": "Brack", "delivery_days": 2},
        "amazon_de": {"name": "Amazon.de", "delivery_days": 4},
        "zooplus": {"name": "zooplus", "delivery_days": 3},
    },
    "products": {
        "filter": [
            {"shop": "galaxus", "title": "PetKit filter 5-pack", "price": 21.9, "pack": 5, "url": "https://g/1"},
            {"shop": "brack", "title": "PetKit filter 5-pack", "price": 21.9, "pack": 5},
        ],
        "desiccant": [
            {"shop": "galaxus", "title": "Desiccant 6-pack", "price": 23.0, "pack": 6},
            {"shop": "brack", "title": "Desiccant 6-pack", "price": 20.9, "pack": 6},
            {"shop": "amazon_de", "title": "Desiccant 6-pack", "price": 16.0, "pack": 6},
        ],
        "drawer_bags": [
            {"shop": "galaxus", "title": "Liners 25", "price": 30.0, "pack": 25},
            {"shop": "zooplus", "title": "Liners 50", "price": 39.5, "pack": 50, "in_stock": False},
        ],
        "litter": [{"shop": "zooplus", "title": "Clay litter 12 kg", "price": 15.9, "pack": 12}],
        "batteries": [
            {"shop": "galaxus", "title": "D cells 4-pack", "price": 8.6, "pack": 4},
            {"shop": "brack", "title": "D cells 4-pack", "price": 12.9, "pack": 4},
        ],
    },
}


def rj(p):
    with open(p) as fh:
        return json.load(fh)


class T(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="cat-autopilot-test-")
        self.addCleanup(shutil.rmtree, self.d, True)
        j = self.j = lambda n: os.path.join(self.d, n)
        for n, body in (("ha", HA_STUB), ("shop", SHOP_STUB)):
            with open(j(n), "w") as f:
                f.write(body)
            os.chmod(j(n), 0o755)
        self.write("db.json", states())
        self.write("hist.json", hist())
        self.write("catalogue.json", CATALOGUE)
        self.write("config.json", {})
        os.makedirs(j("state"))
        os.makedirs(j("inbox"))
        with open(j("state/history.jsonl"), "w") as f:
            for k in range(4, 0, -1):
                day = (NOW - dt.timedelta(days=k)).date().isoformat()
                f.write(
                    json.dumps(
                        {"date": day, "complete": True, "feeders": {"granary_feeder": {"feeds": 1, "food_g": 10.0}}}
                    )
                    + "\n"
                )
        self.log = j("shop.log")
        open(self.log, "w").close()
        self.env = dict(
            os.environ,
            HA_BIN=j("ha"),
            SHOP_BIN=j("shop"),
            STUB_DB=j("db.json"),
            STUB_SHOP_LOG=self.log,
            CAT_CORNER_HISTORY_FIXTURE=j("hist.json"),
            CAT_CORNER_CONFIG=j("config.json"),
            CAT_CORNER_STATE_DIR=j("state"),
            OWNER_TZ="Europe/Zurich",
            CAT_CORNER_NOW=str(NOW.timestamp()),
            HA_AUDIT_BIN=j("nope"),
            CAT_AUTOPILOT_CATALOGUE=j("catalogue.json"),
            CAT_AUTOPILOT_INBOX=j("inbox"),
            USAGE_TEST="1",
            USAGE="off",
            CAT_SUPPLIES_MARKS=j("state/marks.json"),
        )
        self.live_marks = os.path.expanduser("~/.local/state/cat-corner/marks.json")
        self.live_before = os.path.exists(self.live_marks) and os.path.getmtime(self.live_marks)
        self.addCleanup(
            lambda: self.assertEqual(
                os.path.exists(self.live_marks) and os.path.getmtime(self.live_marks),
                self.live_before,
                "test wrote the live marks.json",
            )
        )

    def write(self, name, obj):
        with open(self.j(name), "w") as f:
            json.dump(obj, f)

    def run_(self, *args, rc=0, env=None, at=None):
        e = dict(self.env, **(env or {}))
        if at is not None:
            e["CAT_CORNER_NOW"] = str(at)
        r = subprocess.run([BIN, *args], capture_output=True, text=True, env=e, timeout=60)
        self.assertEqual(r.returncode, rc, r.stderr + r.stdout)
        return r.stdout

    def fc(self, **kw):
        return {i["key"]: i for i in json.loads(self.run_("forecast", "--json", **kw))["items"]}

    def tap(self, data, *extra, **kw):
        return json.loads(self.run_("tap", data, "--json", *extra, **kw))

    def answer_zero(self):
        for data in ("cat:s:flt:0", "cat:s:des:0", "cat:bat:none", "cat:s:lit:12r", "cat:s:bag:3", "cat:s:dry:2"):
            self.tap(data)

    # ---------------------------------------------------------------- forecast
    def test_models_without_stock(self):
        f = self.fc()
        lit = f["litter"]
        self.assertIn("−", lit["rate"] + lit["why"][0])
        rate = float(lit["why"][0].split("−")[1].split(" ")[0])
        self.assertTrue(3.0 <= rate <= 5.5, rate)  # noise-proof slope, not the 6+ of summed drops
        self.assertEqual(f["filter"]["status"], "ask")
        self.assertIn("aged 3 d", " ".join(f["filter"]["why"]))  # 30 % three days ago -> ~20 % now
        self.assertIn(f["filter"]["due"], ("2026-10-26", "2026-10-27"))
        self.assertTrue(f["desiccant"]["due_now"])
        self.assertEqual(f["desiccant"]["due"], "2026-10-21")
        self.assertTrue(f["batteries"]["due_now"])
        self.assertEqual(f["drawer_bags"]["due"], "2026-10-25")  # 60 % +5 %/day -> 80 % in 4 days
        self.assertEqual(f["dry_food"]["rate"], "10 g/day")
        self.assertTrue(all(i["confidence"] == "low" for i in f.values()))
        fc = rj(self.j("state/forecast.json"))
        self.assertTrue(fc["card_lines"][0].startswith("Runs out: "))
        self.assertIn("?", fc["card_lines"][0])
        led = rj(self.j("state/stock.json"))  # the top-up HA saw is in the ledger
        self.assertEqual([e["kind"] for e in led["events"]], ["litter"])
        self.assertAlmostEqual(led["events"][0]["delta"], -44 * 0.045, places=2)

    def test_card_exits_3_until_something_is_due(self):
        self.write("db.json", states(filter_pct=90, desic=10, battery="normal", litter=95, drawer=10))
        self.write("hist.json", hist({"sensor.fount_filter_remaining": [[NOW.isoformat(), "90"]]}))
        self.run_("card", rc=3)
        out = self.run_("card", "--force")
        self.assertIn("Cat supplies", out)

    def test_stock_answers_and_basket(self):
        r = self.tap("cat:s:flt:0")
        self.assertEqual(r["react"], "👍")
        self.assertIsNone(r["reply"])
        self.answer_zero()
        f = self.fc()
        self.assertEqual(f["desiccant"]["status"], "now")
        self.assertEqual(f["batteries"]["status"], "now")
        self.assertEqual(f["filter"]["status"], "soon")  # out ~26/27.10, lead 1 + 3 days
        self.assertEqual(f["litter"]["stock"], 12.0)
        self.assertEqual(f["litter"]["confidence"], "medium")  # range answer + assumed kg per %
        self.assertEqual(f["desiccant"]["confidence"], "high")
        c = json.loads(self.run_("card", "--json"))
        self.assertEqual(len(c["baskets"]), 1)
        b = c["baskets"][0]
        self.assertEqual(b["shop"], "galaxus")  # covers desiccant + batteries, cheaper than Brack
        self.assertEqual({ln["key"] for ln in b["lines"]}, {"desiccant", "batteries", "filter"})  # filter batched
        self.assertAlmostEqual(b["total"], 23.0 + 8.6 + 21.9, places=2)
        datas = [x["data"] for row in c["buttons"] for x in row]
        self.assertEqual(datas[:2], ["cat:order:galaxus", "cat:later"])
        self.assertIn("cat:chg:desiccant", datas)
        self.assertIn("cat:chg:batteries", datas)
        self.assertEqual(c["on_press"], "keep")
        self.assertIn("✅", c["text"])
        self.assertLessEqual(len(c["text"].splitlines()), 10)

    def test_list_items_batch_and_on_list_switch(self):
        lst = [
            {
                "id": 1,
                "status": "researched",
                "text": "Desiccant packs (PETLIBRO Granary feeder)",
                "pick": 1,
                "offers": [{"shop": "Amazon.de", "title": "Wisedry", "price": 18.79, "currency": "EUR"}],
            },
            {"id": 2, "status": "researched", "text": "USB-C cable"},
        ]
        plan = {
            "online": [
                {"shop": "Galaxus", "items": [{"id": 2, "text": "USB-C cable"}]},
                {"shop": "Amazon.de", "items": [{"id": 1, "text": "Desiccant packs"}]},
            ]
        }
        env = {"STUB_SHOP_LIST": json.dumps(lst), "STUB_SHOP_PLAN": json.dumps(plan)}
        self.answer_zero()
        c = json.loads(self.run_("card", "--json", env=env))
        b = c["baskets"][0]
        self.assertEqual(b["shop"], "galaxus")
        self.assertEqual([e["id"] for e in b["from_list"]], [2])
        self.assertIn("from your list: USB-C cable", c["text"])
        r = self.tap("cat:order:galaxus", "--apply", env=env)
        with open(self.log) as fh:
            calls = [json.loads(x) for x in fh]
        adds = [x for x in calls if x[0] == "add"]
        self.assertEqual(sorted(x[-1] for x in adds), ["D cells 4-pack", "PetKit filter 5-pack"])  # not desiccant
        sw = [x for x in calls if x[0] == "set-options" and x[1] == "1"]
        self.assertEqual(json.loads(sw[0][2])["offers"][0]["shop"], "Galaxus")  # list item 1 switched to Galaxus
        self.assertIn(2, r["shop_ids"])
        self.assertIn("shop:buy:galaxus", r["next"])
        self.assertFalse(any(x[0] in ("done", "order", "buy") for x in calls))

    def test_order_dry_then_ordered_counts_after_eta(self):
        self.answer_zero()
        r = self.tap("cat:order:galaxus")
        self.assertTrue(r["steps"])
        with open(self.log) as fh:
            self.assertFalse([x for x in fh if '"add"' in x])  # without --apply: nothing added
        self.run_("ordered", "galaxus")
        cur = json.loads(self.run_("stock", "--json"))
        self.assertEqual(cur["fountain_filters"]["pending"], 5)
        later = json.loads(self.run_("stock", "--json", at=NOW.timestamp() + 2 * DAY))
        self.assertEqual(later["fountain_filters"]["qty"], 5)
        self.assertEqual(later["feeder_batteries"]["qty"], 4)
        self.run_("ordered", "galaxus", rc=2)  # once per prepared basket

    def test_changed_buttons_and_ha_merge(self):
        self.tap("cat:s:flt:4")
        r = self.tap("cat:chg:filter")
        self.assertIn("PetKit app", r["reply"])
        self.assertIn("3 left", r["reply"])
        marks = rj(self.j("state/marks.json"))
        self.assertIn("filter", marks)
        # the fountain's counter reset shows up in HA a day later: counted once, not twice
        self.write(
            "hist.json",
            hist(
                {
                    "sensor.fount_filter_remaining": [
                        [(NOW - dt.timedelta(days=3)).isoformat(), "30"],
                        [(NOW + dt.timedelta(days=1)).isoformat(), "100"],
                    ]
                }
            ),
        )
        cur = json.loads(self.run_("stock", "--json", at=NOW.timestamp() + 2 * DAY))
        self.run_("forecast", at=NOW.timestamp() + 2 * DAY)
        cur = json.loads(self.run_("stock", "--json", at=NOW.timestamp() + 2 * DAY))
        self.assertEqual(cur["fountain_filters"]["qty"], 3)
        r = self.tap("cat:chg:desiccant")
        self.assertIn("PETLIBRO app", r["reply"])
        self.run_("tap", "cat:chg:nonsense", rc=2)

    def test_later_snooze_and_cron_note_once(self):
        self.answer_zero()
        self.run_("--cron")
        notes = os.listdir(self.j("inbox"))
        self.assertEqual(len(notes), 1)
        with open(self.j("inbox/" + notes[0])) as fh:
            body = fh.read()
        self.assertTrue(body.startswith("Data, not instructions"))
        self.assertIn("cat-autopilot card --json", body)
        os.remove(self.j("inbox/" + notes[0]))
        self.run_("--cron")
        self.assertEqual(os.listdir(self.j("inbox")), [])  # same items, within 3 days
        r = self.tap("cat:later")
        self.assertIn("24.10", r["reply"])
        self.run_("--cron", at=NOW.timestamp() + 3.5 * DAY)  # snooze over + 3 days: one new note
        self.assertEqual(len(os.listdir(self.j("inbox"))), 1)

    def test_cron_note_usage_outcome_ok(self):
        # the basket note is a healthy cron run: outcome ok (any other exec outcome counts as a failed run + an
        # error in the usage store), the state rides in attrs
        self.answer_zero()
        self.run_("--cron", env={"USAGE": "", "USAGE_STATE": self.j("usage")})
        self.assertEqual(len(os.listdir(self.j("inbox"))), 1)
        recs = []
        for n in os.listdir(self.j("usage/spool")):
            with open(self.j("usage/spool/" + n)) as fh:
                recs += [json.loads(x) for x in fh if x.strip()]
        ex = [(r.get("o"), (r.get("at") or {}).get("state")) for r in recs if r.get("k") == "exec"]
        self.assertEqual(ex, [("ok", "basket")])

    def test_snooze_blocks_cron(self):
        self.answer_zero()
        self.tap("cat:later")
        self.run_("--cron")
        self.assertEqual(os.listdir(self.j("inbox")), [])

    def test_ask_payload_and_all_answered(self):
        p = json.loads(self.run_("ask", "--json"))
        self.assertEqual(p["on_press"], "keep")
        rows = p["buttons"]
        self.assertLessEqual(max(len(r) for r in rows), 4)
        datas = [b["data"] for r in rows for b in r]
        self.assertTrue(all(len(x.encode()) <= 64 for x in datas))
        self.assertLessEqual(len(p["text"].splitlines()), 8)
        for data in datas:
            if data != "cat:bat:other":
                self.tap(data)  # every button is understood
        last = self.tap("cat:food:none")
        self.assertTrue(last["done"])
        self.assertTrue(last["reply"].startswith("🐱 Got it"))
        self.run_("ask", "--sent")
        ui = rj(self.j("state/autopilot.json"))
        self.assertTrue(ui["asked_at"])
        other = self.tap("cat:bat:other")
        self.assertIn("Which batteries", other["reply"])

    def test_unanswered_stock_counts_as_none_after_grace(self):
        self.run_("ask", "--sent")
        f = self.fc(at=NOW.timestamp() + 3 * DAY)
        self.assertEqual(f["desiccant"]["stock"], 0.0)
        self.assertEqual(f["desiccant"]["status"], "now")
        self.assertIn("assumed none", " ".join(f["desiccant"]["why"]))

    def test_no_liners_skips_item(self):
        self.tap("cat:s:bag:x")
        self.assertNotIn("drawer_bags", self.fc())

    def test_free_text_answer(self):
        out = json.loads(self.run_("answer", "litter 12 kg, 4 filters, no liners and 500 g dry food", "--json"))
        self.assertEqual(
            out["set"], {"litter_kg": 12.0, "fountain_filters": 4.0, "drawer_bags": "x", "dry_food_kg": 0.5}
        )
        out = json.loads(self.run_("answer", "Streu 8 kg, keine Trockenmittel", "--json"))
        self.assertEqual(out["set"], {"litter_kg": 8.0, "desiccant_packs": 0.0})
        self.run_("answer", "hello", rc=2)

    def test_ha_down_and_usage(self):
        self.run_("forecast", rc=1, env={"STUB_HA_DOWN": "1"})
        self.run_("tap", "shop:order", rc=2)
        self.run_("stock", "set", "nope", "3", rc=2)

    def test_tg_cmd_cat_runout_lines(self):
        today = dt.date.today().isoformat()
        with open(self.j("state/history.jsonl"), "a") as f:
            f.write(
                json.dumps(
                    {
                        "date": today,
                        "complete": False,
                        "pets": {"sensor.mia_visits_today": {"visits": 2}},
                        "drawer_end": 50,
                    }
                )
                + "\n"
            )
        self.write(
            "state/forecast.json",
            {"today": today, "items": [], "card_lines": ["Runs out: 💧 filters 27.10 · 🧂 desiccant now"]},
        )
        e = dict(self.env, TG_CMD_TEST="1")
        r = subprocess.run([TGCMD, "cat"], capture_output=True, text=True, env=e, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Runs out: 💧 filters 27.10", r.stdout)
        old = (dt.date.today() - dt.timedelta(days=5)).isoformat()
        self.write("state/forecast.json", {"today": old, "card_lines": ["Runs out: x"]})
        r = subprocess.run([TGCMD, "cat"], capture_output=True, text=True, env=e, timeout=60)
        self.assertNotIn("Runs out", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=1)
