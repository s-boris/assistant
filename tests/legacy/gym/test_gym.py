"""Offline tests for gym: synthetic Jefit exports (fixtures/, made-up data) + a loopback API stub. No network."""

import datetime as dt
import http.server
import importlib.machinery
import importlib.util
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import unittest

BIN = os.environ.get("GYM_BIN", os.path.expanduser("~/bin/gym"))
HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")
KG, LATER, LB, MINIMAL = (
    os.path.join(FIX, f) for f in ("jefit-kg.csv", "jefit-kg-later.csv", "jefit-lb.csv", "jefit-minimal.csv")
)
TOKEN = "tok_TESTONLY_0123456789abcdef"
SECRET_VALUE = "valueThatMustNotLeak"


def load_module():
    loader = importlib.machinery.SourceFileLoader("gym_mod", BIN)
    spec = importlib.util.spec_from_loader("gym_mod", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


class Env:
    def __init__(self, now="2026-10-18"):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.env = dict(
            os.environ,
            GYM_STATE_DIR=os.path.join(self.dir, "state"),
            GYM_DB=os.path.join(self.dir, "state", "gym.db"),
            GYM_NOW=now,
            OWNER_TZ="UTC",
            GYM_TOKEN_FILE=os.path.join(self.dir, "secrets", "jefit-token"),
            GYM_API_CONFIG=os.path.join(self.dir, "config", "api.json"),
        )
        self.env.pop("JEFIT_API_BASE", None)

    def run(self, *args, now=None, code=0):
        env = dict(self.env, **({"GYM_NOW": now} if now else {}))
        p = subprocess.run(
            [sys.executable, BIN] + list(args),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            stdin=subprocess.DEVNULL,
        )
        if code is not None and p.returncode != code:
            raise AssertionError(f"gym {args} -> {p.returncode}\n{p.stdout}\n{p.stderr}")
        return p

    def db(self):
        return sqlite3.connect(self.env["GYM_DB"])

    def token(self, mode=0o600):
        os.makedirs(os.path.dirname(self.env["GYM_TOKEN_FILE"]), exist_ok=True)
        with open(self.env["GYM_TOKEN_FILE"], "w") as fh:
            fh.write(TOKEN + "\n")
        os.chmod(self.env["GYM_TOKEN_FILE"], mode)

    def config(self, **kw):
        os.makedirs(os.path.dirname(self.env["GYM_API_CONFIG"]), exist_ok=True)
        with open(self.env["GYM_API_CONFIG"], "w") as fh:
            json.dump(kw, fh)

    def close(self):
        self.tmp.cleanup()


class Unit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_module()

    def test_parse_logs(self):
        p = self.m.parse_logs
        sets, st = p("40x8,40x8,42.5x5,")
        self.assertEqual(st, "ok")
        self.assertEqual([(x["w"], x["reps"]) for x in sets], [(40, 8), (40, 8), (42.5, 5)])
        self.assertEqual(p("0x0.0,0x10")[1], "cardio")
        self.assertEqual(p("")[1], "empty")
        self.assertEqual(p("abc")[1], "bad")
        self.assertEqual([x["dur"] for x in p("0x60,0x45", timed=True)[0]], [60, 45])
        self.assertEqual([x["dur"] for x in p("0x1:30,10x45s")[0]], [90, 45])
        self.assertEqual(p("135x5", unit_factor=self.m.LB)[0][0]["w"], 61.23)
        self.assertEqual([x["reps"] for x in p("0x12,0x10")[0]], [12, 10])

    def test_epley(self):
        self.assertAlmostEqual(self.m.epley(100, 5), 116.67, places=2)
        self.assertEqual(self.m.epley(100, 1), 100)
        self.assertIsNone(self.m.epley(100, 15))
        self.assertIsNone(self.m.epley(0, 5))

    def test_sections(self):
        text = (
            '### NOTES ####\n\nrow_id,mynote,title\n1,"a, b\n### not a section",X\n\n#####\n'
            "### ROUTINES ###\n\na,b\n1,2\n\nc,d\n3,4\n#####\n"
        )
        sec = self.m.read_sections(text)
        self.assertEqual(sec["NOTES"][0]["mynote"], "a, b\n### not a section")
        self.assertEqual(sec["ROUTINES"], [{"a": "1", "b": "2"}, {"c": "3", "d": "4"}])

    def test_classify(self):
        c = self.m.classify
        cases = {
            "Barbell Bench Press": ("chest", "barbell", "push", "weight"),
            "Romanian Deadlift": ("hamstrings", "barbell", "legs", "weight"),
            "Lying Leg Curl": ("hamstrings", "machine", "legs", "weight"),
            "Face Pull": ("shoulders", "cable", "pull", "weight"),
            "Dumbbell Lateral Raise": ("shoulders", "dumbbell", "push", "weight"),
            "Cable Triceps Pushdown": ("triceps", "cable", "push", "weight"),
            "Close Grip Bench Press": ("triceps", "other", "push", "weight"),
            "Pull Up": ("back", "bodyweight", "pull", "bodyweight"),
            "Lat Pulldown": ("back", "cable", "pull", "weight"),
            "Plank": ("abs", "bodyweight", "core", "timed"),
            "Barbell Hip Thrust": ("glutes", "barbell", "legs", "weight"),
            "Seated Calf Raise": ("calves", "other", "legs", "weight"),
            "Cable Crossover, High to Low": ("chest", "cable", "push", "weight"),
            "Leg Extension": ("quads", "machine", "legs", "weight"),
            "Hammer Curl": ("biceps", "other", "pull", "weight"),
            "Farmer Walk": ("forearms", "other", "pull", "timed"),
        }
        for name, (mu, eq, pat, kind) in cases.items():
            r = c(name)
            self.assertEqual((r["muscle"], r["equipment"], r["pattern"], r["kind"]), (mu, eq, pat, kind), name)
        self.assertEqual(c("Squat")["secondary"], ["glutes"])
        self.assertEqual(c("Leg Extension")["secondary"], [])
        self.assertEqual(c("Bird Dog")["muscle"], "other")
        self.assertEqual(c("Bird Dog", 0)["muscle"], "abs")  # Jefit body-part code wins
        self.assertEqual(c("Nordic Thing", 8)["muscle"], "hamstrings")  # upper legs refined by name
        self.assertEqual(c("Mystery Move", 8)["muscle"], "quads")
        self.assertEqual(c("Mystery Move", "upper_legs")["muscle"], "quads")
        self.assertEqual(c("Face Pull")["side"], "back")


class CsvImport(unittest.TestCase):
    def setUp(self):
        self.e = Env()

    def tearDown(self):
        self.e.close()

    def test_import_kg(self):
        out = json.loads(self.e.run("import", "csv", KG, "--json").stdout)
        self.assertEqual(out["sessions"], 42)
        self.assertEqual(out["new"], 42)
        self.assertEqual(out["unit"], "kg")
        self.assertEqual(out["skipped"], {"cardio": 4, "empty": 1})
        self.assertEqual(out["orphan_logs"], 1)
        db = self.e.db()
        self.assertEqual(db.execute("SELECT COUNT(*) FROM exercises").fetchone()[0], 22)
        # renamed exercise ("Bench Press" -> "Barbell Bench Press", same id) is one exercise with the latest name
        rows = db.execute("SELECT name FROM exercises WHERE name LIKE '%Bench Press%'").fetchall()
        self.assertEqual(rows, [("Barbell Bench Press",)])
        n = db.execute(
            "SELECT COUNT(*) FROM exercise_logs l JOIN exercises e ON e.id=l.exercise_id "
            "WHERE e.name='Barbell Bench Press'"
        ).fetchone()[0]
        self.assertEqual(n, 10)
        mus = dict(db.execute("SELECT name, muscle FROM exercises WHERE name IN ('Bird Dog','Landmine 180')"))
        self.assertEqual(mus, {"Bird Dog": "abs", "Landmine 180": "abs"})
        dur = db.execute(
            "SELECT s.duration_s, s.reps FROM sets s JOIN exercise_logs l ON l.id=s.log_id "
            "JOIN exercises e ON e.id=l.exercise_id WHERE e.name='Plank' ORDER BY s.id LIMIT 3"
        ).fetchall()
        self.assertEqual(dur, [(60, None), (60, None), (45, None)])
        kinds = dict(db.execute("SELECT name, kind FROM exercises WHERE name IN ('Plank','Pull Up','Dips','Deadlift')"))
        self.assertEqual(kinds, {"Plank": "timed", "Pull Up": "bodyweight", "Dips": "bodyweight", "Deadlift": "weight"})
        body = db.execute("SELECT date, weight_kg FROM body_stats ORDER BY date").fetchall()
        self.assertEqual(len(body), 6)
        self.assertEqual(body[0], ("2026-07-27", 82.0))
        st = db.execute("SELECT date, start_ts, duration_s FROM sessions ORDER BY date LIMIT 1").fetchone()
        self.assertEqual(st[0], "2026-07-27")
        self.assertTrue(st[2] > 0)
        self.assertEqual(stat.S_IMODE(os.stat(self.e.env["GYM_DB"]).st_mode), 0o600)

    def test_duplicate_and_later_import(self):
        self.e.run("import", "csv", KG)
        n0 = self.e.db().execute("SELECT (SELECT COUNT(*) FROM sessions), (SELECT COUNT(*) FROM sets)").fetchone()
        out = json.loads(self.e.run("import", "csv", KG, "--json").stdout)
        self.assertEqual((out["new"], out["updated"], out["unchanged"], out["body"]), (0, 0, 42, 0))
        n1 = self.e.db().execute("SELECT (SELECT COUNT(*) FROM sessions), (SELECT COUNT(*) FROM sets)").fetchone()
        self.assertEqual(n0, n1)
        out = json.loads(self.e.run("import", "csv", LATER, "--json").stdout)
        self.assertEqual((out["new"], out["updated"], out["unchanged"]), (2, 1, 41))
        db = self.e.db()
        self.assertEqual(db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 44)
        hlr = db.execute(
            "SELECT COUNT(*) FROM sets s JOIN exercise_logs l ON l.id=s.log_id JOIN sessions x "
            "ON x.id=l.session_id JOIN exercises e ON e.id=l.exercise_id WHERE x.date='2026-10-17' "
            "AND e.name='Hanging Leg Raise'"
        ).fetchone()[0]
        self.assertEqual(hlr, 4)

    def test_dry_run(self):
        out = json.loads(self.e.run("import", "csv", KG, "--dry-run", "--json").stdout)
        self.assertEqual(out["new"], 42)
        self.assertEqual(self.e.db().execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)

    def test_lb_and_minimal(self):
        out = json.loads(self.e.run("import", "csv", LB, "--json").stdout)
        self.assertEqual((out["unit"], out["unit_source"], out["sessions"]), ("lb", "file", 3))
        db = self.e.db()
        w = db.execute(
            "SELECT s.weight_kg, s.reps FROM sets s JOIN exercise_logs l ON l.id=s.log_id JOIN exercises e "
            "ON e.id=l.exercise_id WHERE e.name='Barbell Bench Press' ORDER BY s.id LIMIT 1"
        ).fetchone()
        self.assertEqual(w, (61.23, 8))
        self.assertEqual(
            db.execute("SELECT weight_kg, measures FROM body_stats").fetchone(), (79.83, '{"waist": 81.3}')
        )
        p = self.e.run("import", "csv", MINIMAL)
        self.assertIn("assumed kg", p.stdout)
        out = json.loads(self.e.run("import", "csv", MINIMAL, "--unit", "lb", "--json").stdout)
        self.assertEqual((out["new"], out["updated"]), (0, 2))
        top = (
            self.e.db()
            .execute(
                "SELECT MAX(s.weight_kg) FROM sets s JOIN exercise_logs l ON l.id=s.log_id JOIN "
                "exercises e ON e.id=l.exercise_id WHERE e.name='Barbell Squat'"
            )
            .fetchone()[0]
        )
        self.assertEqual(top, 86.18)  # 190 lb

    def test_not_an_export(self):
        bad = os.path.join(self.e.dir, "x.csv")
        with open(bad, "w") as fh:
            fh.write("a,b\n1,2\n")
        p = self.e.run("import", "csv", bad, code=1)
        self.assertIn("not a Jefit export", p.stderr)

    def test_exercise_override(self):
        self.e.run("import", "csv", KG)
        self.e.run("exercise", "bird dog", "--muscle", "glutes")
        self.e.run("import", "csv", LATER)
        row = self.e.db().execute("SELECT muscle, manual FROM exercises WHERE name='Bird Dog'").fetchone()
        self.assertEqual(row, ("glutes", 1))
        self.e.run("exercise", "Bird Dog", "--reset")
        row = self.e.db().execute("SELECT muscle, manual FROM exercises WHERE name='Bird Dog'").fetchone()
        self.assertEqual(row, ("abs", 0))
        self.e.run("exercise", "Nope", "--muscle", "back", code=1)
        self.e.run("exercise", "Bird Dog", "--muscle", "wings", code=2)


class Analysis(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.e = Env()
        cls.e.run("import", "csv", KG)

    @classmethod
    def tearDownClass(cls):
        cls.e.close()

    def test_stats_json(self):
        r = json.loads(self.e.run("stats", "--json").stdout)
        self.assertEqual(r["end"], "2026-10-18")
        weeks = {w["week"]: w for w in r["weekly"]}
        self.assertEqual(len(weeks), 8)
        self.assertEqual(weeks["2026-W37"]["sessions"], 0)
        self.assertEqual(weeks["2026-W42"]["sessions"], 4)
        self.assertEqual(r["sessions_per_week_4w"], 4.0)
        c = r["consistency"]
        self.assertEqual(c["gaps"][0], {"from": "2026-09-05", "to": "2026-09-17", "days": 12})
        self.assertEqual((c["streak_weeks"], c["days_since_last"]), (5, 1))
        v = r["hard_sets_per_week_4w"]
        self.assertEqual(v["chest"], 12.0)  # bench warm-ups (40x10, 60x5) are not hard sets
        self.assertEqual(v["back"], 7.0)  # row 3 + pull up 3 + deadlift 0.5 x 2
        self.assertEqual(v["calves"], 8.0)
        self.assertGreater(r["balance"]["push_pull"], 1.3)
        lifts = {x["exercise"]: x for x in r["main_lifts"]}
        self.assertEqual(lifts["Barbell Bench Press"]["status"], "plateau")
        self.assertGreaterEqual(lifts["Barbell Bench Press"]["weeks_since_pr"], 4)
        self.assertEqual(lifts["Barbell Squat"]["status"], "progressing")
        self.assertGreater(lifts["Barbell Squat"]["kg_per_week"], 1)
        self.assertNotIn("Standing Calf Raise", lifts)  # isolation moves are not main lifts
        prs = {p["exercise"]: p for p in r["prs"]}
        self.assertAlmostEqual(prs["Barbell Squat"]["value"], 142.92, places=2)
        self.assertEqual(prs["Pull Up"]["type"], "reps")
        self.assertEqual(
            prs["Plank"],
            {"exercise": "Plank", "type": "seconds", "value": 60, "set": "", "date": "2026-07-28", "in_window": False},
        )

    def test_stats_text(self):
        out = self.e.run("stats", "--weeks", "4").stdout
        self.assertIn("Gym stats: 4 weeks", out)
        self.assertIn("plateau: no PR in", out)
        self.assertIn("push:pull", out)

    def test_review(self):
        out = self.e.run("review").stdout
        lines = out.strip().split("\n")
        self.assertLessEqual(len(lines), 12, out)
        self.assertIn("not medical advice", out)
        sug = [l for l in lines if l[:3] in ("1. ", "2. ", "3. ")]  # noqa: E741
        self.assertEqual(len(sug), 3, out)
        self.assertIn("Barbell Bench Press: no e1RM gain", out)
        self.assertIn("Push:pull is", out)
        self.assertTrue(all(len(l) <= 220 for l in lines), out)  # noqa: E741
        js = json.loads(self.e.run("review", "--json").stdout)
        self.assertEqual(len(js["suggestions"]), 3)
        self.assertEqual(js["suggestions"][0]["kind"], "plateau")

    def test_review_if_trained(self):
        self.assertEqual(self.e.run("review", "--if-trained", "7", now="2026-11-30").stdout, "")
        self.assertIn("Gym review", self.e.run("review", "--if-trained", "7").stdout)

    def test_review_comeback(self):
        out = self.e.run("review", now="2026-11-15").stdout
        self.assertIn("Back after 29 days off", out)

    def test_chart(self):
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.assertIn("skipped", self.e.run("chart").stdout)
            return
        out = os.path.join(self.e.dir, "charts")
        paths = self.e.run("chart", "--out", out).stdout.split()
        self.assertEqual([os.path.basename(p) for p in paths], ["volume.png", "e1rm.png", "sessions.png"])
        for p in paths:
            self.assertGreater(os.path.getsize(p), 5000)
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
            with open(p, "rb") as fh:
                self.assertEqual(fh.read(8), b"\x89PNG\r\n\x1a\n")

    def test_empty_db(self):
        e = Env()
        try:
            self.assertIn("No sessions yet", e.run("review").stdout)
            self.assertIn("No sessions yet", e.run("stats").stdout)
            self.assertIn("0 sessions", e.run("status").stdout)
        finally:
            e.close()


class Stub(http.server.BaseHTTPRequestHandler):
    """Loopback Open Training API with the real response shapes (made-up values)."""

    log = []
    start_ts = 0
    scheme = "Bearer"
    UID = 42

    def log_message(self, *a):
        pass

    def _send(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    @classmethod
    def sessions(cls):
        def lg(eid, nid, rtype, sets, unit="metric"):
            return {
                "local_id": nid * 10,
                "exercise_id": eid,
                "numeric_exercise_id": nid,
                "is_system_exercise": True,
                "record_type": rtype,
                "unit_system": unit,
                "legacy_log_string": "x",
                "record": 0.0,
                "log_sets": sets,
            }

        s = cls.start_ts
        return [
            {
                "id": 9001,
                "local_id": 1,
                "date": s,
                "total_time": 3100,
                "unit_system": "imperial",
                "logs": [
                    lg(
                        "exBENCH0000003",
                        3,
                        "weight_and_reps",
                        [
                            {"type": "warm-up", "weight": 95, "reps": 10, "interval": 60},
                            {"type": "default", "weight": 190, "reps": 5, "interval": 120},
                            {"type": "default", "weight": 190, "reps": 5, "interval": 120},
                        ],
                        unit="imperial",
                    )
                ],
            },
            {
                "id": 9002,
                "local_id": 2,
                "date": s + 2 * 86400,
                "total_time": 2800,
                "unit_system": "metric",
                "logs": [
                    lg(
                        "exSQUAT0000101",
                        101,
                        "weight_and_reps",
                        [
                            {"type": "default", "weight": 275.5778, "reps": 5},
                            {"type": "default", "weight": 275.5778, "reps": 5},
                        ],
                    ),
                    lg("exPLANK0000061", 61, "duration", [{"type": "default", "duration": 70}]),
                    lg("exROW000000900", 900, "cardio", [{"type": "default", "duration": 600, "calories": 80}]),
                ],
            },
            {
                "id": 9003,
                "local_id": 3,
                "date": s + 3 * 86400,
                "total_time": 900,
                "unit_system": "metric",
                "logs": [lg("exROW000000900", 900, "cardio", [{"type": "default", "duration": 900, "calories": 120}])],
            },
        ]

    CATALOG = [
        {
            "id": "exBENCH0000003",
            "name": "Barbell Bench Press",
            "body_parts": ["chest", "triceps", "none"],
            "equipment": ["barbell", "none"],
        },
        {
            "id": "exSQUAT0000101",
            "name": "Barbell Squat",
            "body_parts": ["upper_legs", "glutes", "none"],
            "equipment": ["barbell", "none"],
        },
        {"id": "exPLANK0000061", "name": "Plank", "body_parts": ["abs", "none"], "equipment": ["body", "none"]},
        {
            "id": "exROW000000900",
            "name": "Rowing Machine",
            "body_parts": ["cardio", "none"],
            "equipment": ["machine_cardio", "none"],
        },
    ]

    def do_GET(self):
        auth = self.headers.get("Authorization", "")
        Stub.log.append(("GET", self.path, auth, self.headers.get("X-API-Key", "")))
        if auth != f"{Stub.scheme} {TOKEN}":
            return self._send(401, None)
        path, _, q = self.path.partition("?")
        qs = dict(x.split("=", 1) for x in q.split("&") if "=" in x)
        users = f"/api/v2/users/{Stub.UID}"
        if path == "/api/v2/user/routines":
            return self._send(200, {"data": [{"id": "r1", "name": SECRET_VALUE}], "total_count": 1})
        if path == "/api/v2/exercises":
            return self._send(
                200,
                {
                    "data": [
                        {"id": "sys1", "name": SECRET_VALUE, "numeric_user_id": None},
                        {"id": "cus1", "name": "My Curl", "numeric_user_id": Stub.UID},
                    ]
                },
            )
        if path == users + "/sessions/calendar":
            return self._send(
                200,
                {
                    "data": [
                        {"date": dt.datetime.fromtimestamp(s["date"], dt.UTC).date().isoformat(), "has_logs": True}
                        for s in Stub.sessions()
                    ]
                },
            )
        if path == users + "/sessions":
            lo, hi = int(qs.get("startDate", 0)), int(qs.get("endDate", 0))
            return self._send(200, {"data": [s for s in Stub.sessions() if lo <= s["date"] <= hi]})  # inclusive edges
        if path == users + "/exercises":
            return self._send(200, {"data": Stub.CATALOG})
        return self._send(404, {"message": "404 Not Found"})

    def do_POST(self):
        Stub.log.append(("POST", self.path, "", ""))
        self._send(405, {})

    do_PUT = do_PATCH = do_DELETE = do_POST


class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        Stub.log, Stub.scheme = [], "Bearer"
        self.e = Env()
        self.e.env["JEFIT_API_BASE"] = self.base

    def tearDown(self):
        self.e.close()

    def cfg(self):
        with open(self.e.env["GYM_API_CONFIG"]) as fh:
            return json.load(fh)

    def test_no_token(self):
        p = self.e.run("sync", code=3)
        self.assertIn("No Jefit token yet", p.stdout)
        self.assertEqual(Stub.log, [])

    def test_token_permissions(self):
        self.e.token(0o644)
        p = self.e.run("sync", code=1)
        self.assertIn("must be 0600", p.stderr)
        self.assertEqual(Stub.log, [])

    def test_pending(self):
        self.e.token()
        self.e.config(confirmed=False)
        p = self.e.run("sync", code=4)
        self.assertIn("API details pending", p.stdout)
        self.assertEqual(Stub.log, [])
        self.assertIn("Jefit token: present; API endpoints confirmed: no", self.e.run("status").stdout)

    def test_probe(self):
        Stub.start_ts = int(dt.datetime(2026, 10, 12, 18, 0, tzinfo=dt.UTC).timestamp())
        self.e.token()
        p = self.e.run("sync", "--probe")
        out = p.stdout + p.stderr
        self.assertNotIn(TOKEN, out)
        self.assertNotIn(SECRET_VALUE, out)
        self.assertNotIn("/users/42", out)  # the user id stays out of the output
        self.assertIn("API OK", out)
        self.assertTrue(all(m == "GET" and a == f"Bearer {TOKEN}" for m, _, a, _ in Stub.log))
        self.assertEqual(len(Stub.log), 5)  # token check, catalog (user id), calendar, sessions, exercises
        cfg = self.cfg()
        self.assertEqual(cfg, {"user_id": 42})  # defaults (auth bearer, base) are not written
        self.assertEqual(stat.S_IMODE(os.stat(self.e.env["GYM_API_CONFIG"]).st_mode), 0o600)
        probe = os.path.join(self.e.env["GYM_STATE_DIR"], "probe.json")
        with open(probe) as fh:
            txt = fh.read()
        self.assertNotIn(SECRET_VALUE, txt)
        self.assertNotIn(TOKEN, txt)
        self.assertIn("total_time", txt)
        self.assertEqual(stat.S_IMODE(os.stat(probe).st_mode), 0o600)
        Stub.log = []
        self.e.run("sync", "--probe")
        self.assertEqual(len(Stub.log), 4)  # user id known now: no catalog request

    def test_probe_auth_fallback(self):
        Stub.scheme = "Token"
        self.e.token()
        p = self.e.run("sync", "--probe")
        self.assertEqual([a.split(" ")[0] for _, _, a, _ in Stub.log[:2]], ["Bearer", "Token"])
        self.assertIn("Auth header that works: token", p.stdout)
        self.assertEqual(self.cfg(), {"auth": "token", "user_id": 42})

    def test_probe_refused(self):
        Stub.scheme = "Nope"
        self.e.token()
        p = self.e.run("sync", "--probe", code=1)
        self.assertEqual(len(Stub.log), 3)  # each auth scheme once on the token check, nothing else
        self.assertIn("refused with every auth header", p.stdout)
        self.assertNotIn(TOKEN, p.stdout + p.stderr)

    def test_sync(self):
        self.e.env["GYM_NOW"] = "2026-10-20"
        self.e.run("import", "csv", KG)
        Stub.start_ts = self.e.db().execute("SELECT start_ts FROM sessions WHERE date='2026-10-17'").fetchone()[0]
        self.e.token()
        out = json.loads(self.e.run("sync", "--json").stdout)
        self.assertEqual(out["since"], "2026-10-10")
        self.assertEqual(
            (out["sessions"], out["new"], out["updated"], out["unchanged"], out["skipped"]), (2, 1, 1, 0, 1)
        )  # the cardio-only session is skipped
        self.assertTrue(all(m == "GET" for m, *_ in Stub.log))
        db = self.e.db()
        self.assertEqual(db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 43)
        w = db.execute(
            "SELECT s.weight_kg FROM sets s JOIN exercise_logs l ON l.id=s.log_id JOIN sessions x ON "
            "x.id=l.session_id WHERE x.date='2026-10-17'"
        ).fetchall()
        self.assertEqual(w, [(86.18,), (86.18,)])  # 190 lb, warm-up dropped, replaced the CSV session (same start)
        pl = db.execute(
            "SELECT s.duration_s FROM sets s JOIN exercise_logs l ON l.id=s.log_id JOIN exercises e "
            "ON e.id=l.exercise_id JOIN sessions x ON x.id=l.session_id WHERE e.name='Plank' "
            "AND x.date='2026-10-19'"
        ).fetchall()
        self.assertEqual(pl, [(70,)])
        sq = db.execute(
            "SELECT s.weight_kg FROM sets s JOIN exercise_logs l ON l.id=s.log_id JOIN exercises e "
            "ON e.id=l.exercise_id JOIN sessions x ON x.id=l.session_id WHERE e.name='Barbell Squat' "
            "AND x.date='2026-10-19'"
        ).fetchall()
        self.assertEqual(sq, [(125.0,), (125.0,)])  # metric account, pounds on the wire
        self.assertEqual(db.execute("SELECT COUNT(*) FROM exercises WHERE name='Barbell Squat'").fetchone()[0], 1)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM exercises WHERE name='Rowing Machine'").fetchone()[0], 0)
        self.assertEqual(
            db.execute("SELECT muscle, equipment, kind FROM exercises WHERE name='Plank'").fetchone(),
            ("abs", "bodyweight", "timed"),
        )
        Stub.log = []
        out = json.loads(self.e.run("sync", "--json").stdout)
        self.assertEqual((out["new"], out["updated"], out["unchanged"], out["body"]), (0, 0, 2, 0))
        self.assertFalse(any("/api/v2/exercises" == p.split("?")[0] for _, p, _, _ in Stub.log))  # user id cached

    def test_first_sync_full_history_chunked(self):
        Stub.start_ts = int(dt.datetime(2026, 10, 1, 17, 30, tzinfo=dt.UTC).timestamp())
        self.e.token()
        self.e.config(chunk_days=2)
        out = json.loads(self.e.run("sync", "--json").stdout)
        self.assertEqual(out["since"], "2026-10-01")  # earliest calendar date
        self.assertEqual((out["sessions"], out["new"], out["skipped"]), (2, 2, 1))
        paths = [p.split("?")[0] for _, p, _, _ in Stub.log]
        self.assertEqual(paths.count("/api/v2/users/42/sessions"), 9)  # 2026-10-01 .. 10-19 in 2-day chunks
        self.assertEqual(paths.count("/api/v2/users/42/exercises"), 1)
        db = self.e.db()
        self.assertEqual(db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 2)
        self.assertEqual(db.execute("SELECT duration_s FROM sessions WHERE date='2026-10-01'").fetchone()[0], 3100)
        self.assertIn("YYYY-MM-DD", self.e.run("sync", "--since", "2026-1-x", code=2).stderr)

    def test_sync_errors_hide_token(self):
        self.e.token()
        self.e.config(auth="x-api-key")
        p = self.e.run("sync", code=1)
        self.assertIn("HTTP 401", p.stderr)
        self.assertNotIn(TOKEN, p.stdout + p.stderr)
        self.e.env["JEFIT_API_BASE"] = "http://127.0.0.1:9"  # closed port
        p = self.e.run("sync", code=1)
        self.assertNotIn(TOKEN, p.stdout + p.stderr)


class ApiUnit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_module()

    def test_api_log_equipment(self):
        cat = {
            "c1": {"name": "Cable Crossover", "body_parts": ["chest", "none"], "equipment": ["machine_strength"]},
            "c2": {"name": "Lying Leg Lift", "body_parts": ["abs"], "equipment": ["bench", "body"]},
            "c3": {"name": "Seated Fly", "body_parts": ["none", "chest"], "equipment": ["machine_strength"]},
        }
        sets = [{"type": "default", "weight": 20, "reps": 10}]

        def lg(e):
            return self.m.api_log(
                {
                    "exercise_id": e,
                    "numeric_exercise_id": 7,
                    "is_system_exercise": False,  # noqa
                    "log_sets": sets,
                },
                "kg",
                cat,
            )

        self.assertEqual((lg("c1")["equip"], lg("c1")["bodypart"]), (None, "chest"))  # the name says cable
        self.assertEqual(lg("c2")["equip"], "bodyweight")  # bench unmapped, body mapped
        self.assertEqual((lg("c3")["equip"], lg("c3")["bodypart"], lg("c3")["xkey"]), ("machine", "chest", "jefit:0:7"))
        self.assertIsNone(lg("missing"))
        self.assertIsNone(self.m.api_log({"exercise_id": "c1", "record_type": "cardio", "log_sets": sets}, "kg", cat))

    def test_api_weights_are_pounds(self):
        # Jefit sends set weights in lb even when the log says unit_system "metric" (80 kg -> 176.37)
        x = {
            "exercise_id": "c1",
            "numeric_exercise_id": 2,
            "unit_system": "metric",
            "record": 220.4623,
            "log_sets": [{"type": "default", "weight": 176.37, "reps": 5}, {"type": "default", "reps": 12}],
        }
        cat = {"c1": {"name": "Barbell Bench Press", "body_parts": ["chest"], "equipment": ["barbell"]}}
        lg = self.m.api_log(x, self.m.API_DEFAULTS["unit"], cat)
        self.assertEqual([s["w"] for s in lg["sets"]], [80.0, 0])
        self.assertEqual(lg["record_kg"], 100.0)
        s = self.m.api_session({"id": 1, "date": 1760000000, "unit_system": "metric", "logs": []}, "lb")
        self.assertEqual(s["unit"], "lb")


class Fixtures(unittest.TestCase):
    def test_fixtures_match_generator(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            loader = importlib.machinery.SourceFileLoader("genfix", os.path.join(HERE, "gen_fixtures.py"))
            spec = importlib.util.spec_from_loader("genfix", loader)
            g = importlib.util.module_from_spec(spec)
            loader.exec_module(g)
            g.write_kg(os.path.join(tmp.name, "a.csv"))
            with open(os.path.join(tmp.name, "a.csv")) as a, open(KG) as b:
                self.assertEqual(a.read(), b.read())
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=1)
