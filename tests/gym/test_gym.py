"""gym: CSV import of synthetic Jefit exports, the analysis commands, the read-API sync against a loopback stub,
the API unit helpers, and the committed fixtures against their generator. No network; helpers in gym_testlib.py."""

import datetime as dt
import http.server
import importlib.machinery
import importlib.util
import json
import os
import stat
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gym_testlib import HERE, KG, LATER, LB, MINIMAL, SECRET_VALUE, TOKEN, Env, Stub, load_module  # noqa: E402


class TestUnit:
    @classmethod
    def setup_class(cls):
        cls.m = load_module()

    def test_parse_logs(self):
        p = self.m.parse_logs
        sets, st = p("40x8,40x8,42.5x5,")
        assert st == "ok"
        assert [(x["w"], x["reps"]) for x in sets] == [(40, 8), (40, 8), (42.5, 5)]
        assert p("0x0.0,0x10")[1] == "cardio"
        assert p("")[1] == "empty"
        assert p("abc")[1] == "bad"
        assert [x["dur"] for x in p("0x60,0x45", timed=True)[0]] == [60, 45]
        assert [x["dur"] for x in p("0x1:30,10x45s")[0]] == [90, 45]
        assert p("135x5", unit_factor=self.m.LB)[0][0]["w"] == 61.23
        assert [x["reps"] for x in p("0x12,0x10")[0]] == [12, 10]

    def test_epley(self):
        assert round(abs(self.m.epley(100, 5) - 116.67), 2) == 0
        assert self.m.epley(100, 1) == 100
        assert self.m.epley(100, 15) is None
        assert self.m.epley(0, 5) is None

    def test_sections(self):
        text = (
            '### NOTES ####\n\nrow_id,mynote,title\n1,"a, b\n### not a section",X\n\n#####\n'
            "### ROUTINES ###\n\na,b\n1,2\n\nc,d\n3,4\n#####\n"
        )
        sec = self.m.read_sections(text)
        assert sec["NOTES"][0]["mynote"] == "a, b\n### not a section"
        assert sec["ROUTINES"] == [{"a": "1", "b": "2"}, {"c": "3", "d": "4"}]

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
            assert (r["muscle"], r["equipment"], r["pattern"], r["kind"]) == (mu, eq, pat, kind), name
        assert c("Squat")["secondary"] == ["glutes"]
        assert c("Leg Extension")["secondary"] == []
        assert c("Bird Dog")["muscle"] == "other"
        assert c("Bird Dog", 0)["muscle"] == "abs"  # Jefit body-part code wins
        assert c("Nordic Thing", 8)["muscle"] == "hamstrings"  # upper legs refined by name
        assert c("Mystery Move", 8)["muscle"] == "quads"
        assert c("Mystery Move", "upper_legs")["muscle"] == "quads"
        assert c("Face Pull")["side"] == "back"


class TestCsvImport:
    def setup_method(self):
        self.e = Env()

    def teardown_method(self):
        self.e.close()

    def test_import_kg(self):
        out = json.loads(self.e.run("import", "csv", KG, "--json").stdout)
        assert out["sessions"] == 42
        assert out["new"] == 42
        assert out["unit"] == "kg"
        assert out["skipped"] == {"cardio": 4, "empty": 1}
        assert out["orphan_logs"] == 1
        db = self.e.db()
        assert db.execute("SELECT COUNT(*) FROM exercises").fetchone()[0] == 22
        # renamed exercise ("Bench Press" -> "Barbell Bench Press", same id) is one exercise with the latest name
        rows = db.execute("SELECT name FROM exercises WHERE name LIKE '%Bench Press%'").fetchall()
        assert rows == [("Barbell Bench Press",)]
        n = db.execute(
            "SELECT COUNT(*) FROM exercise_logs l JOIN exercises e ON e.id=l.exercise_id "
            "WHERE e.name='Barbell Bench Press'"
        ).fetchone()[0]
        assert n == 10
        mus = dict(db.execute("SELECT name, muscle FROM exercises WHERE name IN ('Bird Dog','Landmine 180')"))
        assert mus == {"Bird Dog": "abs", "Landmine 180": "abs"}
        dur = db.execute(
            "SELECT s.duration_s, s.reps FROM sets s JOIN exercise_logs l ON l.id=s.log_id "
            "JOIN exercises e ON e.id=l.exercise_id WHERE e.name='Plank' ORDER BY s.id LIMIT 3"
        ).fetchall()
        assert dur == [(60, None), (60, None), (45, None)]
        kinds = dict(db.execute("SELECT name, kind FROM exercises WHERE name IN ('Plank','Pull Up','Dips','Deadlift')"))
        assert kinds == {"Plank": "timed", "Pull Up": "bodyweight", "Dips": "bodyweight", "Deadlift": "weight"}
        body = db.execute("SELECT date, weight_kg FROM body_stats ORDER BY date").fetchall()
        assert len(body) == 6
        assert body[0] == ("2026-07-27", 82.0)
        st = db.execute("SELECT date, start_ts, duration_s FROM sessions ORDER BY date LIMIT 1").fetchone()
        assert st[0] == "2026-07-27"
        assert st[2] > 0
        assert stat.S_IMODE(os.stat(self.e.env["GYM_DB"]).st_mode) == 0o600

    def test_duplicate_and_later_import(self):
        self.e.run("import", "csv", KG)
        n0 = self.e.db().execute("SELECT (SELECT COUNT(*) FROM sessions), (SELECT COUNT(*) FROM sets)").fetchone()
        out = json.loads(self.e.run("import", "csv", KG, "--json").stdout)
        assert (out["new"], out["updated"], out["unchanged"], out["body"]) == (0, 0, 42, 0)
        n1 = self.e.db().execute("SELECT (SELECT COUNT(*) FROM sessions), (SELECT COUNT(*) FROM sets)").fetchone()
        assert n0 == n1
        out = json.loads(self.e.run("import", "csv", LATER, "--json").stdout)
        assert (out["new"], out["updated"], out["unchanged"]) == (2, 1, 41)
        db = self.e.db()
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 44
        hlr = db.execute(
            "SELECT COUNT(*) FROM sets s JOIN exercise_logs l ON l.id=s.log_id JOIN sessions x "
            "ON x.id=l.session_id JOIN exercises e ON e.id=l.exercise_id WHERE x.date='2026-10-17' "
            "AND e.name='Hanging Leg Raise'"
        ).fetchone()[0]
        assert hlr == 4

    def test_dry_run(self):
        out = json.loads(self.e.run("import", "csv", KG, "--dry-run", "--json").stdout)
        assert out["new"] == 42
        assert self.e.db().execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0

    def test_lb_and_minimal(self):
        out = json.loads(self.e.run("import", "csv", LB, "--json").stdout)
        assert (out["unit"], out["unit_source"], out["sessions"]) == ("lb", "file", 3)
        db = self.e.db()
        w = db.execute(
            "SELECT s.weight_kg, s.reps FROM sets s JOIN exercise_logs l ON l.id=s.log_id JOIN exercises e "
            "ON e.id=l.exercise_id WHERE e.name='Barbell Bench Press' ORDER BY s.id LIMIT 1"
        ).fetchone()
        assert w == (61.23, 8)
        assert db.execute("SELECT weight_kg, measures FROM body_stats").fetchone() == (79.83, '{"waist": 81.3}')
        p = self.e.run("import", "csv", MINIMAL)
        assert "assumed kg" in p.stdout
        out = json.loads(self.e.run("import", "csv", MINIMAL, "--unit", "lb", "--json").stdout)
        assert (out["new"], out["updated"]) == (0, 2)
        top = (
            self.e.db()
            .execute(
                "SELECT MAX(s.weight_kg) FROM sets s JOIN exercise_logs l ON l.id=s.log_id JOIN "
                "exercises e ON e.id=l.exercise_id WHERE e.name='Barbell Squat'"
            )
            .fetchone()[0]
        )
        assert top == 86.18  # 190 lb

    def test_not_an_export(self):
        bad = os.path.join(self.e.dir, "x.csv")
        with open(bad, "w") as fh:
            fh.write("a,b\n1,2\n")
        p = self.e.run("import", "csv", bad, code=1)
        assert "not a Jefit export" in p.stderr

    def test_exercise_override(self):
        self.e.run("import", "csv", KG)
        self.e.run("exercise", "bird dog", "--muscle", "glutes")
        self.e.run("import", "csv", LATER)
        row = self.e.db().execute("SELECT muscle, manual FROM exercises WHERE name='Bird Dog'").fetchone()
        assert row == ("glutes", 1)
        self.e.run("exercise", "Bird Dog", "--reset")
        row = self.e.db().execute("SELECT muscle, manual FROM exercises WHERE name='Bird Dog'").fetchone()
        assert row == ("abs", 0)
        self.e.run("exercise", "Nope", "--muscle", "back", code=1)
        self.e.run("exercise", "Bird Dog", "--muscle", "wings", code=2)


class TestAnalysis:
    @classmethod
    def setup_class(cls):
        cls.e = Env()
        cls.e.run("import", "csv", KG)

    @classmethod
    def teardown_class(cls):
        cls.e.close()

    def test_stats_json(self):
        r = json.loads(self.e.run("stats", "--json").stdout)
        assert r["end"] == "2026-10-18"
        weeks = {w["week"]: w for w in r["weekly"]}
        assert len(weeks) == 8
        assert weeks["2026-W37"]["sessions"] == 0
        assert weeks["2026-W42"]["sessions"] == 4
        assert r["sessions_per_week_4w"] == 4.0
        c = r["consistency"]
        assert c["gaps"][0] == {"from": "2026-09-05", "to": "2026-09-17", "days": 12}
        assert (c["streak_weeks"], c["days_since_last"]) == (5, 1)
        v = r["hard_sets_per_week_4w"]
        assert v["chest"] == 12.0  # bench warm-ups (40x10, 60x5) are not hard sets
        assert v["back"] == 7.0  # row 3 + pull up 3 + deadlift 0.5 x 2
        assert v["calves"] == 8.0
        assert r["balance"]["push_pull"] > 1.3
        lifts = {x["exercise"]: x for x in r["main_lifts"]}
        assert lifts["Barbell Bench Press"]["status"] == "plateau"
        assert lifts["Barbell Bench Press"]["weeks_since_pr"] >= 4
        assert lifts["Barbell Squat"]["status"] == "progressing"
        assert lifts["Barbell Squat"]["kg_per_week"] > 1
        assert "Standing Calf Raise" not in lifts  # isolation moves are not main lifts
        prs = {p["exercise"]: p for p in r["prs"]}
        assert round(abs(prs["Barbell Squat"]["value"] - 142.92), 2) == 0
        assert prs["Pull Up"]["type"] == "reps"
        assert prs["Plank"] == {
            "exercise": "Plank",
            "type": "seconds",
            "value": 60,
            "set": "",
            "date": "2026-07-28",
            "in_window": False,
        }

    def test_stats_text(self):
        out = self.e.run("stats", "--weeks", "4").stdout
        assert "Gym stats: 4 weeks" in out
        assert "plateau: no PR in" in out
        assert "push:pull" in out

    def test_review(self):
        out = self.e.run("review").stdout
        lines = out.strip().split("\n")
        assert len(lines) <= 12, out
        assert "not medical advice" in out
        sug = [l for l in lines if l[:3] in ("1. ", "2. ", "3. ")]  # noqa: E741
        assert len(sug) == 3, out
        assert "Barbell Bench Press: no e1RM gain" in out
        assert "Push:pull is" in out
        assert all(len(l) <= 220 for l in lines), out  # noqa: E741
        js = json.loads(self.e.run("review", "--json").stdout)
        assert len(js["suggestions"]) == 3
        assert js["suggestions"][0]["kind"] == "plateau"

    def test_review_if_trained(self):
        assert self.e.run("review", "--if-trained", "7", now="2026-11-30").stdout == ""
        assert "Gym review" in self.e.run("review", "--if-trained", "7").stdout

    def test_review_comeback(self):
        out = self.e.run("review", now="2026-11-15").stdout
        assert "Back after 29 days off" in out

    def test_chart(self):
        try:
            import PIL  # noqa: F401
        except ImportError:
            assert "skipped" in self.e.run("chart").stdout
            return
        out = os.path.join(self.e.dir, "charts")
        paths = self.e.run("chart", "--out", out).stdout.split()
        assert [os.path.basename(p) for p in paths] == ["volume.png", "e1rm.png", "sessions.png"]
        for p in paths:
            assert os.path.getsize(p) > 5000
            assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
            with open(p, "rb") as fh:
                assert fh.read(8) == b"\x89PNG\r\n\x1a\n"

    def test_empty_db(self):
        e = Env()
        try:
            assert "No sessions yet" in e.run("review").stdout
            assert "No sessions yet" in e.run("stats").stdout
            assert "0 sessions" in e.run("status").stdout
        finally:
            e.close()


class TestApi:
    @classmethod
    def setup_class(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def teardown_class(cls):
        cls.srv.shutdown()

    def setup_method(self):
        Stub.log, Stub.scheme = [], "Bearer"
        self.e = Env()
        self.e.env["JEFIT_API_BASE"] = self.base

    def teardown_method(self):
        self.e.close()

    def cfg(self):
        with open(self.e.env["GYM_API_CONFIG"]) as fh:
            return json.load(fh)

    def test_no_token(self):
        p = self.e.run("sync", code=3)
        assert "No Jefit token yet" in p.stdout
        assert Stub.log == []

    def test_token_permissions(self):
        self.e.token(0o644)
        p = self.e.run("sync", code=1)
        assert "must be 0600" in p.stderr
        assert Stub.log == []

    def test_pending(self):
        self.e.token()
        self.e.config(confirmed=False)
        p = self.e.run("sync", code=4)
        assert "API details pending" in p.stdout
        assert Stub.log == []
        assert "Jefit token: present; API endpoints confirmed: no" in self.e.run("status").stdout

    def test_probe(self):
        Stub.start_ts = int(dt.datetime(2026, 10, 12, 18, 0, tzinfo=dt.UTC).timestamp())
        self.e.token()
        p = self.e.run("sync", "--probe")
        out = p.stdout + p.stderr
        assert TOKEN not in out
        assert SECRET_VALUE not in out
        assert "/users/42" not in out  # the user id stays out of the output
        assert "API OK" in out
        assert all(m == "GET" and a == f"Bearer {TOKEN}" for m, _, a, _ in Stub.log)
        assert len(Stub.log) == 5  # token check, catalog (user id), calendar, sessions, exercises
        cfg = self.cfg()
        assert cfg == {"user_id": 42}  # defaults (auth bearer, base) are not written
        assert stat.S_IMODE(os.stat(self.e.env["GYM_API_CONFIG"]).st_mode) == 0o600
        probe = os.path.join(self.e.env["GYM_STATE_DIR"], "probe.json")
        with open(probe) as fh:
            txt = fh.read()
        assert SECRET_VALUE not in txt
        assert TOKEN not in txt
        assert "total_time" in txt
        assert stat.S_IMODE(os.stat(probe).st_mode) == 0o600
        Stub.log = []
        self.e.run("sync", "--probe")
        assert len(Stub.log) == 4  # user id known now: no catalog request

    def test_probe_auth_fallback(self):
        Stub.scheme = "Token"
        self.e.token()
        p = self.e.run("sync", "--probe")
        assert [a.split(" ")[0] for _, _, a, _ in Stub.log[:2]] == ["Bearer", "Token"]
        assert "Auth header that works: token" in p.stdout
        assert self.cfg() == {"auth": "token", "user_id": 42}

    def test_probe_refused(self):
        Stub.scheme = "Nope"
        self.e.token()
        p = self.e.run("sync", "--probe", code=1)
        assert len(Stub.log) == 3  # each auth scheme once on the token check, nothing else
        assert "refused with every auth header" in p.stdout
        assert TOKEN not in p.stdout + p.stderr

    def test_sync(self):
        self.e.env["GYM_NOW"] = "2026-10-20"
        self.e.run("import", "csv", KG)
        Stub.start_ts = self.e.db().execute("SELECT start_ts FROM sessions WHERE date='2026-10-17'").fetchone()[0]
        self.e.token()
        out = json.loads(self.e.run("sync", "--json").stdout)
        assert out["since"] == "2026-10-10"
        assert (out["sessions"], out["new"], out["updated"], out["unchanged"], out["skipped"]) == (
            2,
            1,
            1,
            0,
            1,
        )  # the cardio-only session is skipped
        assert all(m == "GET" for m, *_ in Stub.log)
        db = self.e.db()
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 43
        w = db.execute(
            "SELECT s.weight_kg FROM sets s JOIN exercise_logs l ON l.id=s.log_id JOIN sessions x ON "
            "x.id=l.session_id WHERE x.date='2026-10-17'"
        ).fetchall()
        assert w == [(86.18,), (86.18,)]  # 190 lb, warm-up dropped, replaced the CSV session (same start)
        pl = db.execute(
            "SELECT s.duration_s FROM sets s JOIN exercise_logs l ON l.id=s.log_id JOIN exercises e "
            "ON e.id=l.exercise_id JOIN sessions x ON x.id=l.session_id WHERE e.name='Plank' "
            "AND x.date='2026-10-19'"
        ).fetchall()
        assert pl == [(70,)]
        sq = db.execute(
            "SELECT s.weight_kg FROM sets s JOIN exercise_logs l ON l.id=s.log_id JOIN exercises e "
            "ON e.id=l.exercise_id JOIN sessions x ON x.id=l.session_id WHERE e.name='Barbell Squat' "
            "AND x.date='2026-10-19'"
        ).fetchall()
        assert sq == [(125.0,), (125.0,)]  # metric account, pounds on the wire
        assert db.execute("SELECT COUNT(*) FROM exercises WHERE name='Barbell Squat'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM exercises WHERE name='Rowing Machine'").fetchone()[0] == 0
        assert db.execute("SELECT muscle, equipment, kind FROM exercises WHERE name='Plank'").fetchone() == (
            "abs",
            "bodyweight",
            "timed",
        )
        Stub.log = []
        out = json.loads(self.e.run("sync", "--json").stdout)
        assert (out["new"], out["updated"], out["unchanged"], out["body"]) == (0, 0, 2, 0)
        assert not any("/api/v2/exercises" == p.split("?")[0] for _, p, _, _ in Stub.log)  # user id cached

    def test_first_sync_full_history_chunked(self):
        Stub.start_ts = int(dt.datetime(2026, 10, 1, 17, 30, tzinfo=dt.UTC).timestamp())
        self.e.token()
        self.e.config(chunk_days=2)
        out = json.loads(self.e.run("sync", "--json").stdout)
        assert out["since"] == "2026-10-01"  # earliest calendar date
        assert (out["sessions"], out["new"], out["skipped"]) == (2, 2, 1)
        paths = [p.split("?")[0] for _, p, _, _ in Stub.log]
        assert paths.count("/api/v2/users/42/sessions") == 9  # 2026-10-01 .. 10-19 in 2-day chunks
        assert paths.count("/api/v2/users/42/exercises") == 1
        db = self.e.db()
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 2
        assert db.execute("SELECT duration_s FROM sessions WHERE date='2026-10-01'").fetchone()[0] == 3100
        assert "YYYY-MM-DD" in self.e.run("sync", "--since", "2026-1-x", code=2).stderr

    def test_sync_errors_hide_token(self):
        self.e.token()
        self.e.config(auth="x-api-key")
        p = self.e.run("sync", code=1)
        assert "HTTP 401" in p.stderr
        assert TOKEN not in p.stdout + p.stderr
        self.e.env["JEFIT_API_BASE"] = "http://127.0.0.1:9"  # closed port
        p = self.e.run("sync", code=1)
        assert TOKEN not in p.stdout + p.stderr


class TestApiUnit:
    @classmethod
    def setup_class(cls):
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

        assert (lg("c1")["equip"], lg("c1")["bodypart"]) == (None, "chest")  # the name says cable
        assert lg("c2")["equip"] == "bodyweight"  # bench unmapped, body mapped
        assert (lg("c3")["equip"], lg("c3")["bodypart"], lg("c3")["xkey"]) == ("machine", "chest", "jefit:0:7")
        assert lg("missing") is None
        assert self.m.api_log({"exercise_id": "c1", "record_type": "cardio", "log_sets": sets}, "kg", cat) is None

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
        assert [s["w"] for s in lg["sets"]] == [80.0, 0]
        assert lg["record_kg"] == 100.0
        s = self.m.api_session({"id": 1, "date": 1760000000, "unit_system": "metric", "logs": []}, "lb")
        assert s["unit"] == "lb"


class TestFixtures:
    def test_fixtures_match_generator(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            loader = importlib.machinery.SourceFileLoader("genfix", os.path.join(HERE, "gen_fixtures.py"))
            spec = importlib.util.spec_from_loader("genfix", loader)
            g = importlib.util.module_from_spec(spec)
            loader.exec_module(g)
            g.write_kg(os.path.join(tmp.name, "a.csv"))
            with open(os.path.join(tmp.name, "a.csv")) as a, open(KG) as b:
                assert a.read() == b.read()
        finally:
            tmp.cleanup()
