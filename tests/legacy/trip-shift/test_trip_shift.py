"""Offline tests for trip-shift: a temp reminder store, the real `remind` (or REMIND_BIN) on it, a synthetic
profile zone and trip config. No network, no real state, no tg."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

BIN = os.environ.get("TRIP_SHIFT_BIN") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "scripts", "trip-shift"
)
REMIND = os.environ.get("REMIND_BIN", os.path.expanduser("~/bin/remind"))
NOW = "2026-11-10 12:00"  # home wall time
TRIP = {"from": "2026-11-16", "to": "2026-11-25", "tz": "Asia/Dubai"}  # +3 h vs home in November


class TripShift(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        d = self.t.name
        self.db, self.cfg = os.path.join(d, "rem.json"), os.path.join(d, "cfg.json")
        prof = os.path.join(d, "profile.json")
        with open(prof, "w") as f:
            json.dump({"locale": {"tz": "Europe/Berlin"}}, f)
        with open(self.cfg, "w") as f:
            json.dump({"trips": [TRIP]}, f)
        tg = os.path.join(d, "tg")
        with open(tg, "w") as f:
            f.write("#!/bin/sh\nexit 1\n")  # never used: nothing here fires reminders
        os.chmod(tg, 0o755)
        self.calls = os.path.join(d, "calls.log")  # FAKE voice-relay: logs its args, never rings anyone
        relay = os.path.join(d, "fake-voice-relay")
        with open(relay, "w") as f:
            f.write(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{self.calls}"\nexit 0\n')
        os.chmod(relay, 0o755)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("REMIND_", "TRIP_SHIFT_", "OWNER_TZ"))}
        self.env.update(
            REMIND_DB=self.db,
            REMIND_BIN=REMIND,
            REMIND_NOW=NOW,
            REMIND_INBOX=os.path.join(d, "inbox"),
            REMIND_TG=tg,
            REMIND_FIRED_LOG=os.path.join(d, "fired.log"),
            TRIP_SHIFT_CONFIG=self.cfg,
            ASSISTANT_PROFILE=prof,
            REMIND_CALL_CMD=f"{relay} call",
            REMIND_BACK_OFFER="0",
        )

    def tearDown(self):
        self.t.cleanup()

    def remind(self, *a):
        r = subprocess.run(
            [sys.executable, REMIND, *a],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=60,
            stdin=subprocess.DEVNULL,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def ts(self, *a):
        return subprocess.run(
            [sys.executable, BIN, *a],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=60,
            stdin=subprocess.DEVNULL,
        )

    def store(self):
        with open(self.db) as f:
            return json.load(f)

    def seed(self):
        self.remind("add", "--owner", "--tag", "x:1", "2026-11-18 22:30", "call the bank")  # 01:30 there
        self.remind("add", "--owner", "2026-11-21 02:00", "early check-in")  # 05:00 there
        self.remind("add", "--owner", "2026-11-19 09:00", "water the plants")  # 12:00 there: fine
        self.remind("add", "2026-11-18 23:00", "my own note")  # not an owner one
        self.remind("add", "--owner", "2026-11-30 22:30", "after the trip")  # outside the window
        self.remind("add", "--owner", "--every", "weekly", "2026-11-16 21:15", "weekly review")  # 00:15 there

    def test_dry_run_proposal(self):
        self.seed()
        before = self.store()
        r = self.ts()
        self.assertEqual(r.returncode, 0, r.stderr)
        o = r.stdout
        self.assertIn("Trip 16.11–25.11 · Asia/Dubai (+3 h vs home) · quiet 00:00–08:00 there", o)
        self.assertIn("Owner reminders in the window: 5 · ringing in the quiet hours there: 3", o)
        self.assertIn('Wed 18.11 22:30 home = Thu 19.11 01:30 there  "call the bank"  [tag x:1]', o)
        self.assertIn("→ Wed 18.11 19:30 home = Wed 18.11 22:30 there (same clock time there)", o)
        self.assertIn('Sat 21.11 02:00 home = Sat 21.11 05:00 there  "early check-in"', o)
        self.assertIn("→ Sat 21.11 05:00 home = Sat 21.11 08:00 there (end of the quiet hours there)", o)
        self.assertIn('recurring "weekly review" (every Mon): 2 in the window ring at night there', o)
        self.assertNotIn("plants", o)
        self.assertNotIn("my own note", o)
        self.assertNotIn("after the trip", o)
        self.assertIn("Dry run: nothing changed.", o)
        self.assertEqual(self.store(), before)

    def test_morning_policy_and_json(self):
        self.seed()
        r = self.ts("--morning")
        self.assertIn("→ Thu 19.11 05:00 home = Thu 19.11 08:00 there (end of the quiet hours there)", r.stdout)
        J = json.loads(self.ts("--json").stdout)
        self.assertEqual(J[0]["trip"], {"from": "2026-11-16", "to": "2026-11-25", "tz": "Asia/Dubai"})
        self.assertEqual([p["new"] for p in J[0]["oneoff"]], ["2026-11-18T19:30:00", "2026-11-21T05:00:00"])

    def test_apply_keeps_tags_and_text(self):
        self.seed()
        r = self.ts("--apply")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.count("applied: moved "), 2, r.stdout)
        st = {x["text"]: x for x in self.store()}
        self.assertEqual(
            (st["call the bank"]["at"], st["call the bank"]["tag"], st["call the bank"]["owner"]),
            ("2026-11-18 19:30", "x:1", True),
        )
        self.assertEqual(st["early check-in"]["at"], "2026-11-21 05:00")
        self.assertNotIn("tag", st["early check-in"])
        self.assertEqual(st["weekly review"]["at"], "2026-11-16 21:15")  # recurring: untouched
        self.assertEqual(st["water the plants"]["at"], "2026-11-19 09:00")
        self.assertEqual(len(self.store()), 6)
        r = self.ts()
        self.assertIn("ringing in the quiet hours there: 1", r.stdout)  # only the recurring one is left

    def test_apply_moves_by_id_even_with_shared_text(self):
        self.remind("add", "--owner", "2026-11-18 22:30", "pay")
        self.remind("add", "--owner", "2026-12-20 10:00", "pay the rent")
        ids = {x["text"]: x["id"] for x in self.store()}
        r = self.ts("--apply")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        st = {x["text"]: x for x in self.store()}
        self.assertEqual((st["pay"]["at"], st["pay"]["id"]), ("2026-11-18 19:30", ids["pay"]))
        self.assertEqual(st["pay the rent"]["at"], "2026-12-20 10:00")

    def test_apply_keeps_call_flag_and_rings_at_new_time(self):
        self.remind(
            "add", "--call", "--tag", "trip:wake", "2026-11-21 02:00", "wake up for the dive boat"
        )  # 05:00 there
        before = self.store()[0]
        r = self.ts()
        self.assertIn("[📞 call]", r.stdout)
        r = self.ts("--apply")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("📞 call kept", r.stdout)
        after = self.store()
        self.assertEqual(len(after), 1)
        a = after[0]
        self.assertEqual(a["at"], "2026-11-21 05:00")
        self.assertEqual({k: v for k, v in a.items() if k != "at"}, {k: v for k, v in before.items() if k != "at"})
        self.assertTrue(a["call"] and a["owner"] and a["tag"] == "trip:wake")
        self.env["REMIND_NOW"] = "2026-11-21 02:00"  # old time: nothing rings any more
        subprocess.run(
            [sys.executable, REMIND, "due"], env=self.env, capture_output=True, timeout=60, stdin=subprocess.DEVNULL
        )
        self.assertFalse(os.path.exists(self.calls))
        self.env["REMIND_NOW"] = "2026-11-21 05:00"  # new time: the fake relay is called once
        subprocess.run(
            [sys.executable, REMIND, "due"], env=self.env, capture_output=True, timeout=60, stdin=subprocess.DEVNULL
        )
        with open(self.calls) as f:
            log = f.read().splitlines()
        self.assertEqual(len(log), 1, log)
        self.assertTrue(log[0].startswith("call --reason wake up for the dive boat"), log)

    def test_apply_gives_legacy_entry_an_id(self):
        with open(self.db, "w") as f:
            json.dump([{"at": "2026-11-18 22:30", "text": "old style", "owner": True, "call": True}], f)
        r = self.ts("--apply")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        (x,) = self.store()
        self.assertEqual((x["at"], x["call"]), ("2026-11-18 19:30", True))
        self.assertRegex(x["id"], "^[0-9a-f]{8}$")

    def test_flags_override_config_and_nothing_to_shift(self):
        self.remind("add", "--owner", "2026-11-18 22:30", "call the bank")
        r = self.ts("--from", "2026-11-17", "--to", "2026-11-19", "--tz", "America/New_York")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("(-6 h vs home)", r.stdout)
        self.assertIn("Nothing to shift.", r.stdout)
        self.assertEqual(self.ts("--from", "2026-11-17").returncode, 2)
        self.assertEqual(self.ts("--from", "2026-11-17", "--to", "2026-11-19", "--tz", "Nowhere/X").returncode, 2)
        self.assertEqual(self.ts("--quiet", "8-8").returncode, 2)

    def test_past_trip_and_no_config(self):
        with open(self.cfg, "w") as f:
            json.dump({"trips": [{"from": "2026-10-01", "to": "2026-10-05", "tz": "Asia/Dubai"}]}, f)
        r = self.ts()
        self.assertEqual(r.returncode, 0)
        self.assertIn("No current or upcoming trip", r.stdout)
        os.unlink(self.cfg)
        self.assertIn("No current or upcoming trip", self.ts().stdout)

    def test_quiet_window_over_midnight(self):
        self.remind("add", "--owner", "2026-11-18 20:00", "late call")  # 23:00 there
        r = self.ts("--quiet", "22-7")
        self.assertIn("→ Wed 18.11 17:00 home = Wed 18.11 20:00 there (same clock time there)", r.stdout)
        r = self.ts("--quiet", "22-7", "--morning")
        self.assertIn("→ Thu 19.11 04:00 home = Thu 19.11 07:00 there", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=1)
