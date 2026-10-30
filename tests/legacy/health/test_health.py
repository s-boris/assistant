"""Synthetic sleep data only: deterministic made-up nights, no real values."""

import datetime as dt
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from zoneinfo import ZoneInfo

HOME = os.path.expanduser("~")
LIB = os.environ.get("HEALTH_LIB") or os.path.join(HOME, "bin", "lib")
CLI = os.environ.get("HEALTH_CLI") or os.path.join(HOME, "bin", "health")
sys.path.insert(0, LIB)
import healthlib as H  # noqa: E402

TZ = ZoneInfo("Europe/Zurich")


def ep(d, h, m=0):
    return int(dt.datetime(d.year, d.month, d.day, h, m, tzinfo=TZ).timestamp())


def body(d, bed_h=23, bed_m=30, mins=450, steps=8000, workouts=None, deep=18):
    start = ep(d - dt.timedelta(days=1), bed_h, bed_m) if bed_h >= 12 else ep(d, bed_h, bed_m)
    return {
        "kind": "health.daily",
        "day": d.isoformat(),
        "sleep": {
            "start": start,
            "end": start + mins * 60 + 20 * 60,
            "min": mins,
            "deep_pct": deep,
            "rem_pct": 21,
            "light_pct": 55,
            "awake_min": 20,
        },
        "rhr": 54,
        "steps": steps,
        "workouts": workouts or [],
        "at": 0,
    }


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {
            "HEALTH_STATE": os.path.join(self.tmp.name, "state"),
            "HEALTH_CONFIG": os.path.join(self.tmp.name, "cfg.json"),
            "HEALTH_TZ": "Europe/Zurich",
            "GYM_DB": os.path.join(self.tmp.name, "gym.db"),
            "SLEEPIN_BIN": "/bin/false",
            "TRIPCLOCK_BIN": "/bin/false",
            "USAGE": "off",
        }
        self.old = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        self.today = dt.date(2026, 10, 21)
        self.now = ep(self.today, 9, 30)
        os.environ["HEALTH_NOW"] = str(self.now)

    def tearDown(self):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        os.environ.pop("HEALTH_NOW", None)
        self.tmp.cleanup()

    def fill(self, n=28, late=(), gym=(), short_last=False):
        con = H.connect()
        for i in range(n - 1, -1, -1):
            d = self.today - dt.timedelta(days=i)
            b = body(d, steps=8000 + (i % 3) * 500)
            if d in late:
                b = body(d, bed_h=2, bed_m=10, mins=320)
            if (d - dt.timedelta(days=1)) in gym:
                b["sleep"]["deep_pct"] = 22
            if d in gym:
                b["workouts"] = [{"type": "strength", "min": 60, "avg_hr": 120, "hour": 18}]
            if short_last and i == 0:
                b = body(d, bed_h=0, bed_m=45, mins=330)
            H.ingest(b, received=self.now - i * 86400, con=con)
        con.close()


class StoreTest(Base):
    def test_private_modes_upsert_and_retention(self):
        self.fill(5)
        st = os.stat(H.db_path())
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(H.state_dir()).st_mode), 0o700)
        con = H.connect()
        H.ingest(body(self.today, mins=400), received=self.now, con=con)
        H.ingest(body(self.today - dt.timedelta(days=200)), received=self.now, con=con)  # beyond retention
        self.assertEqual(
            con.execute("SELECT sleep_min FROM days WHERE day=?", (self.today.isoformat(),)).fetchone()[0], 400
        )
        self.assertEqual(con.execute("SELECT COUNT(*) FROM days").fetchone()[0], 5)
        self.assertIsNone(H.ingest({"kind": "zone"}, con=con))
        self.assertIsNone(H.ingest({"kind": "health.daily", "day": "x"}, con=con))
        con.close()

    def test_status_is_counts_only(self):
        self.fill(10)
        s = H.status()
        self.assertEqual((s["days"], s["nights"]), (10, 10))
        flat = json.dumps(s)
        self.assertNotIn("450", flat)  # no sleep minutes in the status
        self.assertNotIn("8000", flat)

    def test_tags_keep_kinds_never_titles(self):
        con = H.connect()
        st = {
            "date": self.today.isoformat(),
            "sources": [{"kind": "calendar", "what": "Some Party", "end": "x"}],
            "ahead": [
                {
                    "date": (self.today + dt.timedelta(days=1)).isoformat(),
                    "off": False,
                    "sources": [{"kind": "calendar", "what": "Another"}],
                },
                {"date": (self.today + dt.timedelta(days=5)).isoformat(), "sources": [{"kind": "hold"}]},
            ],
        }
        self.assertEqual(H.tag_nights(st, con, self.now), 2)
        rows = con.execute("SELECT day, kinds, tentative FROM nights ORDER BY day").fetchall()
        self.assertEqual([r[1] for r in rows], ["calendar", "calendar"])
        self.assertEqual([r[2] for r in rows], [0, 1])
        self.assertNotIn("Party", json.dumps(rows))
        con.close()


class InsightTest(Base):
    def test_baseline_debt_drift_steps(self):
        self.fill(28, short_last=True)
        rows, tags = H.load(t=self.now)
        ins = H.insights(rows, tags, TZ, self.now)
        self.assertEqual(ins["baseline_min"], 450)
        self.assertEqual(ins["debt7_min"], 120)  # one 330-min night against a 450 median
        self.assertEqual(ins["last"]["sleep_min"], 330)
        self.assertIn("steps7", ins)
        self.assertAlmostEqual(ins["steps_change_pct"], 0, delta=10)
        self.assertEqual(ins["tone"], "low")  # last night 2 h under the usual
        line = H.last_line(ins, TZ)
        self.assertTrue(line.startswith("😴 5 h 30 · bed 00:45 · 7-day debt 2 h 00"), line)
        self.assertLessEqual(len(line), 80)

    def test_baseline_needs_seven_nights(self):
        self.fill(4)
        rows, tags = H.load(t=self.now)
        ins = H.insights(rows, tags, TZ, self.now)
        self.assertIsNone(ins["baseline_min"])
        self.assertEqual(ins["baseline_missing"], 3)
        self.assertIn("baseline in 3 nights", H.last_line(ins, TZ))

    def test_late_nights_and_gym_days(self):
        late = {self.today - dt.timedelta(days=d) for d in (3, 10, 17)}
        gym = {self.today - dt.timedelta(days=d) for d in (2, 5, 9, 12)}
        self.fill(28, late=late, gym=gym)
        con = H.connect()
        for d in late:
            con.execute(
                "INSERT INTO nights(day, late, kinds, tentative, updated) VALUES(?,?,?,?,?)",
                (d.isoformat(), 1, "calendar", 0, 0),
            )
        con.commit()
        con.close()
        rows, tags = H.load(t=self.now)
        ins = H.insights(rows, tags, TZ, self.now)
        self.assertEqual(ins["late"]["n"], 3)
        self.assertEqual(ins["late"]["min"], 320)
        self.assertEqual(ins["late"]["normal_min"], 450)
        self.assertEqual(ins["gym"]["n"], 4)
        self.assertAlmostEqual(ins["gym"]["deep"], 22)
        text = "\n".join(H.card_lines(ins, TZ))
        self.assertIn("Late nights (3): 5 h 20 vs 7 h 30 usually", text)
        self.assertIn("After gym (4)", text)
        self.assertLessEqual(len(H.card_lines(ins, TZ)), 10)

    def test_drift_later_bedtimes(self):
        con = H.connect()
        for i in range(27, -1, -1):
            d = self.today - dt.timedelta(days=i)
            b = body(d, bed_h=0, bed_m=40, mins=450) if i < 7 else body(d, bed_h=23, bed_m=40, mins=450)
            H.ingest(b, received=self.now, con=con)
        con.close()
        rows, tags = H.load(t=self.now)
        ins = H.insights(rows, tags, TZ, self.now)
        self.assertEqual(ins["drift_min"], 60)
        self.assertIn("1 h 00 later", "\n".join(H.card_lines(ins, TZ)))

    def test_board_only_when_up_and_not_holding(self):
        self.fill(10)
        cfg = dict(H.DEFAULTS)
        b = H.board(t=self.now, cfg=cfg, holding=False)
        self.assertEqual(b["night"], self.today.isoformat())
        self.assertTrue(b["line"].startswith("😴 7 h 30"))
        self.assertEqual(H.board(t=self.now, cfg=cfg, holding=True), {})  # sleep-in guard holds
        self.assertEqual(H.board(t=ep(self.today, 15), cfg=cfg, holding=False), {})  # afternoon
        self.assertEqual(H.board(t=ep(self.today, 5), cfg=cfg, holding=False), {})  # before the sleep ended
        self.assertEqual(H.board(t=self.now, cfg=dict(cfg, board_line=False), holding=False), {})

    def test_empty_store_card(self):
        ins = H.insights([], {}, TZ, self.now)
        self.assertIn("No sleep data yet", H.card_lines(ins, TZ)[0])
        self.assertEqual(H.board(t=self.now, cfg=dict(H.DEFAULTS), holding=False), {})


class CliTest(Base):
    def run_cli(self, *args, stdin=""):
        return subprocess.run(
            [sys.executable, CLI] + list(args),
            input=stdin,
            capture_output=True,
            text=True,
            timeout=30,
            env=dict(os.environ),
        )

    def test_ingest_card_recap_off(self):
        p = self.run_cli("ingest", "--received", str(self.now), stdin=json.dumps(body(self.today)))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.run_cli("ingest", stdin="nope").returncode, 1)
        p = self.run_cli("card", "--no-log")
        self.assertIn("Last night 7 h 30", p.stdout)
        p = self.run_cli("recap", "--cron")  # default off: silent, nothing sent
        self.assertEqual((p.returncode, p.stdout), (0, ""))
        p = self.run_cli("config", "weekly_recap", "loud")
        self.assertEqual(p.returncode, 2)
        p = self.run_cli("status", "--json")
        self.assertEqual(json.loads(p.stdout)["config"]["weekly_recap"], "off")

    def test_recap_app_mode_calls_phone_push(self):
        self.fill(10)
        log = os.path.join(self.tmp.name, "argv")
        fake = os.path.join(self.tmp.name, "phone")
        with open(fake, "w") as fh:
            fh.write("#!/bin/sh\nprintf '%s\\n' \"$@\" > " + log + "\n")
        os.chmod(fake, 0o755)
        os.environ["PHONE_BIN"] = fake
        try:
            self.assertEqual(self.run_cli("config", "weekly_recap", "app").returncode, 0)
            self.assertEqual(self.run_cli("recap", "--cron").returncode, 0)
            with open(log) as fh:
                argv = fh.read().split("\n")
            self.assertIn("--private", argv)
            self.assertIn("😴 Your sleep week", argv)
        finally:
            os.environ.pop("PHONE_BIN", None)


if __name__ == "__main__":
    unittest.main(verbosity=1)
