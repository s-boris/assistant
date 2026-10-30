"""Offline tests for the post-workout recap (`gym recap`, ~/bin/lib/gymcoach.py) and tg-cmd's /gym WORD.
Synthetic log only (made-up values, test_gym_plan.make_db).
Run: python3 -m unittest test_gym_coach   (GYM_BIN=/copy/gym and TGCMD_BIN=/copy/tg-cmd test copies)."""

import datetime as dt
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import unittest

import test_gym as base
import test_gym_plan as tp

TGCMD = os.environ.get("TGCMD_BIN", os.path.expanduser("~/bin/tg-cmd"))


class Coach(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = base.load_module()

    def setUp(self):
        self.e = base.Env(now=tp.NOW)
        self.e.env.update(
            GYM_CALCACHE="off", GYM_RAVE_RADAR="off", USAGE="off", GYM_INBOX=os.path.join(self.e.dir, "inbox")
        )
        self.conn, self.xid = tp.make_db(self.e.env["GYM_DB"], self.m)
        self.state = os.path.dirname(self.e.env["GYM_DB"])

    def tearDown(self):
        self.conn.close()
        self.e.close()

    def plan(self):
        self.e.run("plan", "draft", "--minutes", "60", "--json")
        with open(os.path.join(self.state, "plan.json")) as fh:
            return json.load(fh)

    def add(self, date, items, sets=3):
        tp.add_session(self.conn, dt.date.fromisoformat(date), items, self.xid, sets=sets)
        self.conn.commit()

    def j(self, *args, now=None, code=0):
        return json.loads(self.e.run(*args, "--json", now=now, code=code).stdout)

    # ------------------------------------------------------------------ recap
    def test_recap_card(self):
        p = self.plan()
        a = p["days"][0]
        items = [
            (x["name"], (x["weight_kg"] or 0.0) + (10 if x["role"] == "main" else 0), x["hi"])
            for x in a["exercises"][:3]
        ]
        self.add("2026-10-18", items)
        d = self.j("recap")
        self.assertEqual(d["date"], "2026-10-18")
        self.assertEqual(d["day"], a["key"])
        self.assertTrue(d["prs"])
        self.assertTrue(d["skipped"])
        self.assertIn("💪 Session 18.10. · " + a["key"], d["text"])
        self.assertIn("🏆 PR:", d["text"])
        self.assertEqual(self.e.run("recap", "--date", "2026-10-17", code=3).stdout, "")

    def test_recap_cron_two_polls_then_one_note(self):
        inbox = self.e.env["GYM_INBOX"]

        def notes():
            return sorted(os.listdir(inbox)) if os.path.isdir(inbox) else []

        self.e.run("recap", "--cron")  # first run: silent baseline
        self.assertEqual(notes(), [])
        self.add("2026-10-18", [("Barbell Squat", 100.0, 5)], sets=2)
        self.e.run("recap", "--cron")  # seen once: wait
        self.assertEqual(notes(), [])
        self.add_set_to_last()  # still logging: changed
        self.e.run("recap", "--cron")
        self.assertEqual(notes(), [])
        self.e.run("recap", "--cron")  # same twice: settled
        self.assertEqual(len(notes()), 1)
        with open(os.path.join(inbox, notes()[0])) as fh:
            body = fh.read()
        self.assertIn("gym recap", body)
        self.assertNotIn("kg", body)
        self.assertNotIn("100", body)
        self.e.run("recap", "--cron")
        self.e.run("recap", "--cron")
        self.assertEqual(len(notes()), 1)  # once per session
        self.add("2026-10-01", [("Barbell Squat", 100.0, 5)])  # late backfill of an old day
        self.e.run("recap", "--cron")
        self.e.run("recap", "--cron")
        self.assertEqual(len(notes()), 1)

    def test_recap_cron_usage_runs_are_not_errors(self):
        """Every --cron poll is a healthy run: the usage store folds an exec outcome outside usage.OK_OUT into a
        failed run + an error event, so the poll state (init/pending/note/none/old) goes into attrs, not outcome."""
        loader = importlib.machinery.SourceFileLoader(
            "usage_t", os.path.join(os.path.dirname(os.path.realpath(base.BIN)), "lib", "usage.py")
        )
        spec = importlib.util.spec_from_loader("usage_t", loader)
        usage = importlib.util.module_from_spec(spec)
        loader.exec_module(usage)
        ustate = os.path.join(self.e.dir, "usage")
        self.e.env.update(USAGE="", USAGE_STATE=ustate)
        self.e.run("recap", "--cron")  # init
        self.add("2026-10-18", [("Barbell Squat", 100.0, 5)], sets=2)
        self.e.run("recap", "--cron")  # pending
        self.e.run("recap", "--cron")  # note
        self.e.run("recap", "--cron")  # none
        self.add("2026-10-20", [("Barbell Squat", 100.0, 5)])
        self.e.run("recap", "--cron", now="2026-10-30")  # late backfill: old
        self.e.run("recap", "--cron", now="2026-10-30")  # none
        rows = []
        for name in sorted(os.listdir(os.path.join(ustate, "spool"))):
            with open(os.path.join(ustate, "spool", name)) as fh:
                rows += [json.loads(x) for x in fh if x.strip()]
        rows = [r for r in rows if r.get("f") == "gym.recap" and r.get("k") == "exec"]
        self.assertEqual(len(rows), 6)
        for r in rows:
            self.assertIn(r.get("o") or "ok", usage.OK_OUT, r)
        self.assertEqual(
            [(r.get("at") or {}).get("state") for r in rows], ["init", "pending", "note", "none", "old", "none"]
        )

    def add_set_to_last(self):
        lid = self.conn.execute("SELECT MAX(id) FROM exercise_logs").fetchone()[0]
        self.conn.execute(
            "INSERT INTO sets (log_id, idx, weight_kg, reps, e1rm_kg) VALUES (?,?,?,?,?)", (lid, 9, 100.0, 5, 116.67)
        )
        self.conn.commit()


def _png(path, w=10, h=10):
    """A black RGB PNG (stdlib only: the fake card needs a real image file, not Pillow)."""
    import struct
    import zlib

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + b"\x00\x00\x00" * w for _ in range(h))
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)))
        f.write(chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class TgCmdGym(unittest.TestCase):
    """tg-cmd /gym WORD with a fake gym (prints its argv; --card writes a PNG)."""

    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.log = os.path.join(d, "log")
        png = os.path.join(d, "card.png")
        _png(png)
        with open(os.path.join(d, "gym"), "w") as fh:
            fh.write(
                f"#!/bin/sh\necho \"gym $*\" >> '{self.log}'\n"
                f'case "$*" in *--card*) echo \'{png}\';; *) echo "📈 fake $*"; echo line2;; esac\n'
            )
        os.chmod(os.path.join(d, "gym"), 0o755)
        self.env = dict(os.environ, TG_CMD_BIN=d, TG_CMD_TEST="1", USAGE="off")
        self.env.pop("TG_CMD_PICTURES", None)

    def tearDown(self):
        self.tmp.cleanup()

    def run_tg(self, *args, pictures=False):
        env = dict(self.env, **({"TG_CMD_PICTURES": "1"} if pictures else {}))
        return subprocess.run([sys.executable, TGCMD, *args], env=env, capture_output=True, text=True, timeout=60)

    def test_words(self):
        self.assertIn("📈 fake plan next", self.run_tg("gym").stdout)
        self.assertIn("📈 fake recap", self.run_tg("gym", "last").stdout)
        self.assertIn("📈 fake recap", self.run_tg("gym", "recap").stdout)
        hint = self.run_tg("gym", "zumba")
        self.assertEqual(hint.returncode, 0)
        self.assertIn("/gym recap", hint.stdout)
        if os.path.exists(self.log):  # the fake gym never ran for it
            with open(self.log) as fh:
                self.assertNotIn("zumba", fh.read())
        self.assertEqual(self.run_tg("gym", "a", "b").returncode, 2)
        self.assertIn("📈 fake recap", self.run_tg("gym", "last", pictures=True).stdout)  # text, no photo JSON
        self.assertIn("/gym ", self.run_tg("--list").stdout)  # its text argument lives in the tools registry


if __name__ == "__main__":
    unittest.main()
