"""Offline tests for ~/bin/voice-plan (with the real ~/bin/lists on temp state and a stub remind).
Never touches real state: VOICE_PLAN_DIR, LISTS_DB and a fixture profile (ASSISTANT_PROFILE) point into a temp
dir; VOICE_PLAN_REMIND is a stub that logs its arguments. VOICE_PLAN_BIN / LISTS_BIN test copies before
installing. Run: python3 -I test_voice_plan.py
"""

import glob
import hashlib
import json
import os
import stat
import subprocess
import tempfile
import unittest

BIN = os.environ.get("VOICE_PLAN_BIN") or os.path.expanduser("~/bin/voice-plan")
LISTS = os.environ.get("LISTS_BIN") or os.path.expanduser("~/bin/lists")
REAL_LISTS = os.path.expanduser("~/.local/state/lists/lists.json")
REAL_PLANS = os.path.expanduser("~/.local/state/voice-plan")
NOW = "2026-03-02 10:00"  # a Monday
ZONE = "Europe/Berlin"  # same rules as the owner zone; any fixed zone works


def digest(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except FileNotFoundError:
        return None


def plan_files():
    return sorted(glob.glob(os.path.join(REAL_PLANS, "*.json")))


REAL_BEFORE = (digest(REAL_LISTS), plan_files())

GOOD = [
    {"kind": "reminder", "when": "2026-03-03 09:00", "text": "Call the garage"},
    {
        "kind": "event",
        "start": "2026-03-07 14:00",
        "end": "2026-03-07 23:30",
        "title": "Wedding",
        "location": "Some Hall, Main St 1, 1000 Town",
        "description": "bring the gift",
    },
    {"kind": "event", "start": "2026-03-20", "end": "2026-03-22", "title": "Trip"},
    {"kind": "list", "list": "Einkauf", "items": ["Milch", "Eier", "Brot"]},
    {"kind": "note", "text": "Idea: a gift tracker"},
]


class VoicePlanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="voice-plan-tests.")
        t = self.tmp.name
        self.state = os.path.join(t, "vp")
        self.lists_db = os.path.join(t, "lists.json")
        self.rlog = os.path.join(t, "remind.log")
        self.stub = os.path.join(t, "remind")
        with open(self.stub, "w") as f:
            f.write(
                f'#!/bin/sh\n[ -e "{t}/remind-fail" ] && {{ echo "remind: boom" >&2; exit 1; }}\n'
                f'for a in "$@"; do printf "%s|" "$a"; done >> "{self.rlog}"; echo >> "{self.rlog}"\necho added\n'
            )
        os.chmod(self.stub, 0o755)
        self.profile = os.path.join(t, "profile.json")
        with open(self.profile, "w") as f:
            json.dump({"locale": {"tz": ZONE}}, f)

    def tearDown(self):
        self.tmp.cleanup()

    def vp(self, *args, now=NOW, stdin=None, shop=""):
        env = dict(
            os.environ,
            VOICE_PLAN_DIR=self.state,
            VOICE_PLAN_NOW=now,
            VOICE_PLAN_REMIND=self.stub,
            VOICE_PLAN_LISTS=LISTS,
            LISTS_DB=self.lists_db,
            LISTS_NOW=now,
            ASSISTANT_PROFILE=self.profile,
            VOICE_PLAN_SHOP=shop,
            USAGE_STATE=os.path.join(self.tmp.name, "usage"),
            USAGE_TEST="1",
        )
        p = subprocess.run(
            [BIN, *args], env=env, input=stdin if stdin is not None else "", capture_output=True, text=True, timeout=60
        )
        return p.returncode, p.stdout.strip(), p.stderr.strip()

    def write(self, obj, name="plan.json"):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w") as f:
            json.dump(obj, f)
        return path

    def render(self, obj=GOOD, *extra, now=NOW):
        rc, out, err = self.vp("render", self.write(obj), "--json", *extra, now=now)
        self.assertEqual(rc, 0, err)
        return json.loads(out)

    def stored(self, pid):
        with open(os.path.join(self.state, pid + ".json")) as f:
            return json.load(f)

    def remind_calls(self):
        try:
            with open(self.rlog) as f:
                return [ln.rstrip("\n").rstrip("|").split("|") for ln in f if ln.strip()]
        except FileNotFoundError:
            return []

    # ---------------------------------------------------------------- render

    def test_render_card_and_buttons(self):
        rc, out, err = self.vp("render", self.write(GOOD), "--transcript", "the words")
        self.assertEqual(rc, 0, err)
        text, _, btn = out.rpartition("\n\n")
        lines = text.splitlines()
        self.assertEqual(lines[0], "📋 5 actions:")
        self.assertEqual(lines[1], "1. ⏰ Tomorrow 09:00 · Call the garage")
        self.assertEqual(lines[2], "2. 📅 Sat 7 Mar 14:00–23:30 · Wedding @ Some Hall")
        self.assertEqual(lines[3], "3. 📅 Fri 20 Mar → Sun 22 Mar · Trip")
        self.assertEqual(lines[4], "4. 🛒 shopping +3: Milch, Eier, Brot")
        self.assertEqual(lines[5], "5. 📝 Idea: a gift tracker")
        rows = json.loads(btn)
        pid = rows[0][0]["data"].split(":")[2]
        self.assertEqual([b["data"] for b in rows[0]], [f"vp:ok:{pid}", f"vp:fix:{pid}", f"vp:no:{pid}"])
        self.assertEqual([b["text"] for b in rows[0]], ["✅ Do all", "✏️ Fix", "✖"])
        plan = self.stored(pid)
        self.assertEqual((plan["status"], plan["transcript"], plan["created"]), ("pending", "the words", NOW))
        self.assertEqual(plan["actions"][3]["list"], "shopping")
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.state, pid + ".json")).st_mode), 0o600)

    def test_render_json_object_form_and_stdin(self):
        j = self.render({"actions": [{"kind": "note", "text": "hi"}], "transcript": "said hi"})
        self.assertEqual(j["text"], "📋 1 action:\n1. 📝 hi")
        self.assertEqual(self.stored(j["id"])["transcript"], "said hi")
        rc, out, err = self.vp("render", "-", "--json", stdin=json.dumps([{"kind": "note", "text": "x"}]))
        self.assertEqual(rc, 0, err)
        rc, out, err = self.vp(
            "render", self.write([{"kind": "note", "text": "y"}]), "--transcript", "-", "--json", stdin="from stdin\n"
        )
        self.assertEqual(self.stored(json.loads(out)["id"])["transcript"], "from stdin")

    def test_weekday_labels_and_year(self):
        j = self.render(
            [
                {"kind": "reminder", "when": "2026-03-02 18:00", "text": "a"},
                {"kind": "reminder", "when": "2026-03-05 08:00", "text": "b"},
                {"kind": "event", "start": "2027-01-04", "title": "c"},
                {"kind": "event", "start": "2026-03-06 22:00", "end": "2026-03-07 03:00", "title": "d"},
            ]
        )
        lines = j["text"].splitlines()
        self.assertEqual(lines[1], "1. ⏰ Today 18:00 · a")
        self.assertEqual(lines[2], "2. ⏰ Thu 5 Mar 08:00 · b")
        self.assertEqual(lines[3], "3. 📅 Mon 4 Jan 2027 (all day) · c")
        self.assertEqual(lines[4], "4. 📅 Fri 6 Mar 22:00–03:00 · d")

    def test_offset_times_convert_to_owner_zone(self):
        j = self.render(
            [
                {"kind": "reminder", "when": "2026-03-03T08:00:00Z", "text": "a"},
                {"kind": "event", "start": "2026-07-01T10:00:00+00:00", "title": "b"},
            ]
        )
        acts = j["actions"]
        self.assertEqual(acts[0]["when"], "2026-03-03 09:00")  # winter: +1
        self.assertEqual((acts[1]["start"], acts[1]["end"]), ("2026-07-01 12:00", "2026-07-01 13:00"))

    def test_long_lines_and_many_items_shortened(self):
        j = self.render(
            [
                {"kind": "list", "list": "todo", "items": [f"item {i}" for i in range(9)]},
                {"kind": "note", "text": "word " * 40},
            ]
        )
        lines = j["text"].splitlines()
        self.assertIn("+3 more", lines[1])
        self.assertTrue(lines[2].endswith("…"))
        self.assertLessEqual(len(lines[2]), 85)

    # ---------------------------------------------------------------- validation

    def test_validation_reports_everything_and_saves_nothing(self):
        bad = [
            {"kind": "reminder", "when": "2026-03-01 09:00", "text": ""},
            {"kind": "event", "title": "x", "start": "2026-03-05 10:00", "end": "2026-03-05 09:00", "tilte": 1},
            {"kind": "foo"},
            {"kind": "list", "list": "todo", "items": []},
            {"kind": "event", "start": "2026-03-05", "end": "2026-03-05 10:00", "title": "y"},
            {"kind": "event", "start": "2026-03-01", "title": "z"},
            {"kind": "event", "start": "2026-03-02 10:00", "title": "now"},
            {"kind": "reminder", "when": "tomorrow 9", "text": "q"},
            {"kind": "list", "list": "a/b", "items": ["x"]},
            {"text": "no kind"},
            {"kind": "event", "start": "2026-02-30 10:00", "title": "bad day"},
        ]
        rc, out, err = self.vp("render", self.write(bad))
        self.assertEqual(rc, 1)
        for want in (
            "#1 reminder: time 2026-03-01 09:00 is in the past; missing text",
            '#2 event: unknown field "tilte"; end is not after start',
            '#3 foo: unknown kind "foo"',
            "#4 list: missing items",
            '#5 event: all-day end "2026-03-05 10:00" must be a date',
            "#6 event: date 2026-03-01 is in the past",
            "#7 event: start 2026-03-02 10:00 is in the past",
            '#8 reminder: when "tomorrow 9" is not',
            "#9 list: bad list name",
            "#10 action: missing kind",
            '#11 event: start "2026-02-30 10:00" is not a valid',
        ):
            self.assertIn(want, err)
        self.assertFalse(glob.glob(os.path.join(self.state, "*.json")))

    def test_validation_shapes(self):
        for raw, want in (
            ("not json", "not JSON"),
            ("[]", "no actions"),
            ("{}", "no actions"),
            (json.dumps({"actions": [{"kind": "note", "text": "a"}], "x": 1}), 'unknown top-level field "x"'),
            (json.dumps([{"kind": "note", "text": "a"}] * 21), "at most 20"),
        ):
            rc, _, err = self.vp("render", "-", stdin=raw)
            self.assertEqual(rc, 1, raw)
            self.assertIn(want, err)
        self.assertEqual(self.vp("render")[0], 2)
        self.assertEqual(self.vp("render", "/nonexistent/x.json")[0], 2)

    # ---------------------------------------------------------------- run

    def test_shopping_items_go_to_shop(self):
        slog = os.path.join(self.tmp.name, "shop.log")
        shop = os.path.join(self.tmp.name, "shop")
        with open(shop, "w") as f:
            f.write(
                f'#!/bin/sh\nprintf "%s|" "$@" >> "{slog}"; echo >> "{slog}"\n'
                'case "$2" in Eier) echo "already on the list: #2 Eier";; Brot) echo "shop: boom" >&2; exit 1;; '
                '*) echo "added #1 $2 · in HA";; esac\n'
            )
        os.chmod(shop, 0o755)
        pid = self.render(GOOD)["id"]
        rc, out, err = self.vp("run", pid, shop=shop)
        self.assertEqual(rc, 1, err)
        self.assertIn("✗ 4. ", out)
        self.assertIn("failed: Brot", out)
        with open(slog) as f:
            self.assertEqual(f.read().splitlines(), ["add|Milch|", "add|Eier|", "add|Brot|"])
        if os.path.exists(self.lists_db):
            with open(self.lists_db) as f:
                self.assertNotIn("shopping", json.load(f)["lists"])
        self.assertEqual([r["status"] for r in self.stored(pid)["results"]], ["ok", "todo", "todo", "failed", "ok"])

    def test_run_executes_and_prints_events(self):
        pid = self.render(GOOD)["id"]
        rc, out, err = self.vp("run", pid)
        self.assertEqual(rc, 0, err)
        lines = out.splitlines()
        self.assertEqual(lines[0], "✓ 1. ⏰ Tomorrow 09:00 · Call the garage")
        self.assertTrue(lines[1].startswith("✓ 4. 🛒 shopping: +3"))
        self.assertEqual(lines[2], "✓ 5. 📝 Idea: a gift tracker")
        events = [json.loads(ln) for ln in lines if ln.startswith("{")]
        self.assertEqual(
            events[0],
            {
                "n": 2,
                "summary": "Wedding",
                "startTime": "2026-03-07T14:00:00",
                "endTime": "2026-03-07T23:30:00",
                "allDay": False,
                "location": "Some Hall, Main St 1, 1000 Town",
                "description": "bring the gift",
            },
        )
        self.assertEqual(
            events[1],
            {
                "n": 3,
                "summary": "Trip",
                "startTime": "2026-03-20T00:00:00",
                "endTime": "2026-03-23T00:00:00",
                "allDay": True,
            },
        )
        self.assertEqual(
            self.remind_calls(), [["add", "--owner", "--tag", f"vp:{pid}", "2026-03-03 09:00", "Call the garage"]]
        )
        with open(self.lists_db) as f:
            lists = json.load(f)["lists"]
        self.assertEqual([i["text"] for i in lists["shopping"]], ["Milch", "Eier", "Brot"])
        self.assertEqual([i["text"] for i in lists["notes"]], ["Idea: a gift tracker"])
        plan = self.stored(pid)
        self.assertEqual(plan["status"], "done")
        self.assertEqual([r["status"] for r in plan["results"]], ["ok", "todo", "todo", "ok", "ok"])
        # a second tap is refused and changes nothing
        rc, _, err = self.vp("run", pid)
        self.assertEqual(rc, 3)
        self.assertIn("is done", err)
        self.assertEqual(len(self.remind_calls()), 1)
        self.assertEqual(self.vp("drop", pid)[0], 3)

    def test_run_json_and_list_dedupe(self):
        subprocess.run(
            [LISTS, "add", "shopping", "milch"],
            env=dict(os.environ, LISTS_DB=self.lists_db, LISTS_NOW=NOW, ASSISTANT_PROFILE=self.profile),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
            check=True,
        )
        pid = self.render(
            [
                {"kind": "list", "list": "groceries", "items": ["Milch", "Käse"]},
                {"kind": "event", "start": "2026-03-04 19:00", "title": "Dinner"},
            ]
        )["id"]
        rc, out, err = self.vp("run", pid, "--json")
        self.assertEqual(rc, 0, err)
        j = json.loads(out)
        self.assertEqual((j["id"], j["status"], j["failed"]), (pid, "done", 0))
        self.assertIn("already there: milch", j["results"][0]["detail"])
        self.assertEqual(
            j["events"],
            [
                {
                    "n": 2,
                    "summary": "Dinner",
                    "startTime": "2026-03-04T19:00:00",
                    "endTime": "2026-03-04T20:00:00",
                    "allDay": False,
                }
            ],
        )

    def test_run_refuses_stale_plan(self):
        pid = self.render(GOOD)["id"]
        rc, _, err = self.vp("run", pid, now="2026-03-03 09:30")
        self.assertEqual(rc, 4)
        self.assertIn("#1 ⏰", err)
        self.assertEqual(self.remind_calls(), [])
        self.assertEqual(self.stored(pid)["status"], "pending")
        self.assertFalse(os.path.exists(self.lists_db))

    def test_run_failure_is_reported_and_others_still_run(self):
        pid = self.render(GOOD)["id"]
        open(os.path.join(self.tmp.name, "remind-fail"), "w").close()
        rc, out, _ = self.vp("run", pid)
        self.assertEqual(rc, 1)
        self.assertIn("✗ 1. ⏰ Tomorrow 09:00 · Call the garage (remind: boom)", out)
        self.assertIn("✓ 4.", out)
        plan = self.stored(pid)
        self.assertEqual((plan["status"], plan["results"][0]["status"]), ("done", "failed"))
        self.assertEqual(self.vp("run", pid)[0], 3)

    # ---------------------------------------------------------------- drop / replace / show / prune

    def test_drop(self):
        pid = self.render(GOOD)["id"]
        rc, out, _ = self.vp("drop", pid)
        self.assertEqual((rc, out), (0, f"dropped plan {pid}"))
        self.assertEqual(self.vp("run", pid)[0], 3)
        self.assertEqual(self.vp("drop", pid)[0], 3)
        self.assertEqual(self.remind_calls(), [])

    def test_replace(self):
        old = self.render(GOOD)["id"]
        new = self.render([{"kind": "note", "text": "fixed"}], "--replace", old)["id"]
        self.assertNotEqual(old, new)
        o = self.stored(old)
        self.assertEqual((o["status"], o["replaced_by"]), ("dropped", new))
        self.assertEqual(self.stored(new)["status"], "pending")
        rc, _, err = self.vp("render", self.write(GOOD), "--replace", "abcdef")
        self.assertEqual(rc, 2)
        self.assertIn("no plan abcdef", err)

    def test_show(self):
        pid = self.render(GOOD)["id"]
        rc, out, _ = self.vp("show", pid)
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("📋 5 actions:\n1. ⏰"))
        self.vp("drop", pid)
        self.assertTrue(self.vp("show", pid)[1].startswith("📋 5 actions: (dropped)"))
        self.assertEqual(json.loads(self.vp("show", pid, "--json")[1])["id"], pid)
        self.assertEqual(self.vp("show", "abcdef")[0], 2)
        self.assertEqual(self.vp("show", "../etc")[0], 2)
        self.assertEqual(self.vp("run", "XYZ")[0], 2)

    def test_old_plans_pruned(self):
        old = self.render([{"kind": "note", "text": "a"}], now="2026-02-10 10:00")["id"]
        keep = self.render([{"kind": "note", "text": "b"}], now="2026-02-20 10:00")["id"]
        self.render([{"kind": "note", "text": "c"}], now="2026-02-25 10:01")
        self.assertFalse(os.path.exists(os.path.join(self.state, old + ".json")))
        self.assertTrue(os.path.exists(os.path.join(self.state, keep + ".json")))

    def test_no_bytecode_next_to_lists(self):
        cache = os.path.join(os.path.dirname(os.path.realpath(LISTS)), "__pycache__")
        before = set(os.listdir(cache)) if os.path.isdir(cache) else set()
        self.render(GOOD)
        after = set(os.listdir(cache)) if os.path.isdir(cache) else set()
        self.assertEqual(after - before, set())

    def test_usage(self):
        self.assertEqual(self.vp()[0], 2)
        self.assertEqual(self.vp("frob")[0], 2)
        rc, out, _ = self.vp("--help")
        self.assertEqual(rc, 0)
        self.assertIn("voice-plan render FILE", out)

    # ---------------------------------------------------------------- photo plans (skill vision-actions)

    def usage_events(self):
        out = []
        for path in glob.glob(os.path.join(self.tmp.name, "usage", "spool", "*.jsonl")):
            with open(path) as f:
                out += [json.loads(ln) for ln in f if ln.strip()]
        return out

    def test_warranty_card_run_and_reminder(self):
        plan = {
            "source": "photo",
            "class": "label",
            "actions": [
                {"kind": "warranty", "product": "Espresso machine X1", "shop": "Some Shop", "bought": "2026-02-27"}
            ],
        }
        j = self.render(plan)
        self.assertEqual(
            j["text"], "📷 1 action:\n1. 🛡 Espresso machine X1 (Some Shop) · warranty to 27.02.2028 · ⏰ 28.01.2028"
        )
        st = self.stored(j["id"])
        self.assertEqual((st["source"], st["class"]), ("photo", "label"))
        self.assertEqual(st["actions"][0]["remind"], "2028-01-28 09:00")
        rc, out, err = self.vp("run", j["id"])
        self.assertEqual(rc, 0, err)
        self.assertIn("✓ 1. 🛡 Espresso machine X1", out)
        calls = self.remind_calls()
        self.assertEqual(calls[0][:5], ["add", "--owner", "--tag", f"vp:{j['id']}", "2028-01-28 09:00"])
        self.assertIn("ends 27.02.2028", calls[0][5])
        rc, out, _ = self.vp("show", j["id"])
        with open(self.lists_db) as f:
            items = json.load(f)["lists"]["warranty"]
        texts = [
            i["text"] if isinstance(i, dict) else i
            for i in (items.get("items", items) if isinstance(items, dict) else items)
        ]
        self.assertTrue(
            any("Espresso machine X1 · Some Shop · bought 27.02.2026 · warranty to 27.02.2028" in t for t in texts),
            texts,
        )
        ev = self.usage_events()
        kinds = [(e.get("f"), e.get("k"), e.get("o")) for e in ev]
        self.assertIn(("skill.vision-actions", "invoke", "card"), kinds)
        self.assertIn(("skill.vision-actions", "exec", "ok"), kinds)
        self.assertNotIn("Espresso", json.dumps(ev))  # tokens only, never text

    def test_warranty_defaults_and_validation(self):
        # 36 months, reminder already due -> the next 09:00 (NOW is 10:00 -> tomorrow)
        j = self.render([{"kind": "warranty", "product": "Old drill", "bought": "2024-03-20", "remind_days": 30}])
        self.assertEqual(self.stored(j["id"])["actions"][0]["remind"], "2026-03-03 09:00")
        self.assertTrue(j["text"].startswith("📋 1 action:"))
        bad = [
            {"kind": "warranty", "product": "A", "bought": "2020-01-01"},
            {"kind": "warranty", "product": "B", "bought": "2026-04-01"},
            {"kind": "warranty", "product": "C", "bought": "01.03.2026"},
            {"kind": "warranty", "product": "D", "bought": "2026-03-01", "months": 0},
            {"kind": "warranty", "bought": "2026-03-01", "serial": "x"},
        ]
        rc, _, err = self.vp("render", self.write(bad))
        self.assertEqual(rc, 1)
        for frag in (
            "#1 warranty: the warranty ended 2022-01-01",
            "#2 warranty: bought 2026-04-01 is in the future",
            '#3 warranty: bought "01.03.2026" must be a date',
            "#4 warranty: months must be a whole number",
            '#5 warranty: unknown field "serial"; missing product',
        ):
            self.assertIn(frag, err)
        rc, _, err = self.vp("render", self.write({"source": "fax", "actions": [{"kind": "note", "text": "x"}]}))
        self.assertEqual(rc, 1)
        self.assertIn('source "fax"', err)
        rc, _, err = self.vp("render", self.write({"class": "Not A Token", "actions": [{"kind": "note", "text": "x"}]}))
        self.assertEqual(rc, 1)

    def test_photo_drop_logs_dismissed_voice_plans_log_nothing(self):
        j = self.render({"source": "photo", "class": "poster", "actions": [{"kind": "note", "text": "x"}]})
        self.assertEqual(self.vp("drop", j["id"])[0], 0)
        v = self.render([{"kind": "note", "text": "y"}])
        self.assertEqual(self.vp("run", v["id"])[0], 0)
        kinds = [(e.get("f"), e.get("k")) for e in self.usage_events()]
        self.assertEqual(kinds, [("skill.vision-actions", "invoke"), ("skill.vision-actions", "dismissed")])


class NoLeak(unittest.TestCase):
    def test_zz_real_state_untouched(self):
        self.assertEqual((digest(REAL_LISTS), plan_files()), REAL_BEFORE)


if __name__ == "__main__":
    unittest.main(verbosity=1)
