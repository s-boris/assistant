"""Offline tests for `gym plan`: a synthetic log (made-up values) and an in-memory loopback Jefit routine API."""

import base64
import copy
import datetime as dt
import email.utils
import http.server
import json
import os
import re
import sqlite3
import stat
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request

import test_gym as base

NOW = "2026-10-18"
# name -> (jefit numeric id, weight kg, reps, sessions); weight 0 = bodyweight
LOG = {
    "Barbell Squat": (12, 100.0, 5, 8),
    "Barbell Bench Press": (2, 80.0, 6, 8),
    "Cable Lat Pulldown (Wide Grip)": (86, 60.0, 10, 8),
    "Machine Seated Leg Curl": (161, 40.0, 10, 6),
    "Dumbbell Lateral Raise": (32, 10.0, 12, 8),
    "Dumbbell Tricep Extension": (510, 20.0, 10, 8),
    "Cable Tricep Pushdown (Rope)": (159, 30.0, 12, 4),
    "Weighted Crunch": (803, 10.0, 12, 8),
    "Barbell Deadlift": (93, 120.0, 5, 6),
    "Barbell Military Press (Seated)": (755, 40.0, 6, 6),
    "Machine Leg Press": (132, 150.0, 10, 8),
    "Calf Press On Leg Press": (140, 120.0, 12, 8),
    "Cable Seated Row": (21, 60.0, 10, 8),
    "Dumbbell Incline Bench Press": (31, 25.0, 10, 8),
    "Dumbbell Alternating Preacher Curl": (1008, 15.0, 10, 8),
    "Hanging Leg Raise": (61, 0.0, 12, 8),
    "Machine Hack Squat": (700, 120.0, 8, 3),
}
REFS = [("u_000000000101", "Old Split"), ("u_000000000102", "Another Routine")]


def jid(name):
    return "d_" + str(LOG[name][0]).zfill(12)


def make_db(path, m):
    """A log of weekly full-body sessions: every exercise gets its sets on its last N sessions."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(m.SCHEMA)
    now = dt.date.fromisoformat(NOW)
    xid = {}
    for name, (nid, *_rest) in LOG.items():
        c = m.classify(name)
        cur = conn.execute(
            "INSERT INTO exercises (name, muscle, secondary, equipment, pattern, side, kind) VALUES (?,?,?,?,?,?,?)",
            (name, c["muscle"], ",".join(c["secondary"]), c["equipment"], c["pattern"], c["side"], c["kind"]),
        )
        xid[name] = cur.lastrowid
        conn.execute("INSERT INTO exercise_keys VALUES (?,?)", (f"jefit:1:{nid}", cur.lastrowid))
    for k in range(8):
        d = now - dt.timedelta(days=7 * (8 - k))
        add_session(conn, d, [(n, w, r) for n, (_, w, r, cnt) in LOG.items() if k >= 8 - cnt], xid)
    conn.commit()
    return conn, xid


def add_session(conn, date, items, xid, sets=3):
    ts = int(dt.datetime.combine(date, dt.time(18), dt.UTC).timestamp())
    sid = conn.execute(
        "INSERT INTO sessions (date, start_ts, duration_s, source) VALUES (?,?,?,'api')", (date.isoformat(), ts, 3000)
    ).lastrowid
    for pos, (name, w, reps) in enumerate(items):
        lid = conn.execute(
            "INSERT INTO exercise_logs (session_id, exercise_id, pos) VALUES (?,?,?)", (sid, xid[name], pos)
        ).lastrowid
        for i in range(sets):
            r = reps[i] if isinstance(reps, list) else reps
            conn.execute(
                "INSERT INTO sets (log_id, idx, weight_kg, reps, e1rm_kg) VALUES (?,?,?,?,?)",
                (lid, i, w or None, r, round(w * (1 + r / 30), 2) if w else None),
            )


class WStub(http.server.BaseHTTPRequestHandler):
    """In-memory Jefit routines with the web app's write semantics (made-up ids and values)."""

    routines, log, next_id, read_only, throttle = {}, [], 1000, False, None

    def log_message(self, *a):
        pass

    @classmethod
    def reset(cls):
        cls.routines, cls.log, cls.next_id, cls.read_only, cls.throttle = {}, [], 1000, False, None
        for rid, name in REFS:
            cls.routines[rid] = {
                "id": rid,
                "name": name,
                "description": "",
                "last_updated": 1700000000,
                "days": [
                    {
                        "id": rid.replace("u_0000", "u_0001"),
                        "name": "Day",
                        "index": 1,
                        "day_of_week": 7,
                        "week": 0,
                        "sort_order": 0,
                        "day_exercises": [],
                    }
                ],
            }

    @classmethod
    def nid(cls):
        cls.next_id += 1
        return "u_%012d" % cls.next_id  # noqa: UP031

    def _send(self, code, body=None, headers=None):
        raw = json.dumps(body if body is not None else {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _find(self, kind, oid):
        for r in WStub.routines.values():
            if kind == "routine" and r["id"] == oid:
                return r, None, None
            for d in r["days"]:
                if kind == "day" and d["id"] == oid:
                    return r, d, None
                for e in d["day_exercises"]:
                    if kind == "de" and e["id"] == oid:
                        return r, d, e
        return None, None, None

    def _handle(self, method):
        body = None
        if method in ("POST", "PATCH"):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        WStub.log.append((method, self.path, body))
        if self.headers.get("Authorization") != f"Bearer {base.TOKEN}":
            return self._send(401)
        if method != "GET" and WStub.read_only:
            return self._send(403, {"error": "insufficient_scope"})
        t = WStub.throttle  # {'from': nth request counted, 'left': 429s to give, 'after': Retry-After, 'get': bool}
        if t and (method != "GET" or t.get("get")):
            t["seen"] = t.get("seen", 0) + 1
            if t["seen"] >= t["from"] and t["left"] > 0:
                t["left"] -= 1
                return self._send(
                    429, {"error": "rate_limited"}, {"Retry-After": t["after"]} if t.get("after") is not None else None
                )
        p = self.path.split("?")[0]
        m = re.fullmatch(
            r"/api/v2/(user/routines|routines|days|day_exercises)(?:/(u_\d{12}))?(/days|/day_exercises)?", p
        )
        if not m:
            return self._send(404, {"message": "not found"})
        coll, oid, sub = m.groups()
        data = (body or {}).get("data") or {}
        if coll == "user/routines":
            if method == "GET":
                return self._send(
                    200,
                    {
                        "data": [
                            {
                                "id": r["id"],
                                "name": r["name"],
                                "last_updated": r["last_updated"],
                                "days": [{"id": d["id"]} for d in r["days"]],
                            }
                            for r in WStub.routines.values()
                        ]
                    },
                )
            rid = WStub.nid()
            WStub.routines[rid] = {
                "id": rid,
                "name": "New Routine",
                "description": "",
                "last_updated": 1,
                "day_indexing_style": "number",
                "days": [self._day({"name": "Day 1", "index": 1})],
            }
            return self._send(200, {"data": WStub.routines[rid]})
        kind = {"routines": "routine", "days": "day", "day_exercises": "de"}[coll]
        r, d, e = self._find(kind, oid)
        if r is None:
            return self._send(404, {"message": "not found"})
        obj = {"routine": r, "day": d, "de": e}[kind]
        if method == "GET" and not sub:
            return self._send(200, {"data": obj, "unit_system": "metric"})
        r["last_updated"] += 1
        if method == "DELETE":
            if kind == "routine":
                del WStub.routines[oid]
            elif kind == "day":
                r["days"].remove(d)
            else:
                d["day_exercises"].remove(e)
                for i, x in enumerate(sorted(d["day_exercises"], key=lambda x: x["index"])):
                    x["index"] = i
            return self._send(200)
        if method == "POST" and sub == "/days":
            day = self._day(data)
            r["days"].append(day)
            return self._send(200, {"data": day})
        if method == "POST" and sub == "/day_exercises":
            de = {
                "id": WStub.nid(),
                "exercise": {"id": data["exercise"]["id"]},
                "index": len(d["day_exercises"]),
                "count": 3,
                "number_of_reps": [8, 8, 8],
                "rest_time": 0,
                "sets": [self._set(i) for i in range(3)],
            }
            d["day_exercises"].append(de)
            return self._send(200, {"data": de})
        if method == "PATCH":
            for k, v in data.items():
                if kind == "de" and k == "count":
                    obj["sets"] = (obj["sets"] + [self._set(i) for i in range(len(obj["sets"]), v)])[:v]
                    obj["count"] = v
                elif kind == "de" and k == "sets":  # positional: {} keeps, an object sets, length = new count
                    old = obj["sets"]
                    obj["sets"] = [
                        dict(old[i] if i < len(old) else self._set(i), **s, index=i) for i, s in enumerate(v)
                    ]
                    obj["count"] = len(v)
                else:
                    obj[k] = v
            return self._send(200, {"data": obj})
        return self._send(405)

    @staticmethod
    def _day(data):
        return {
            "id": WStub.nid(),
            "name": data.get("name", "New Day"),
            "index": data.get("index", 1),
            "day_of_week": data.get("day_of_week", 7),
            "week": 0,
            "sort_order": 0,
            "day_exercises": [],
        }

    @staticmethod
    def _set(i):
        return {
            "id": WStub.nid(),
            "index": i,
            "type": "default",
            "weight_lbs": None,
            "reps": 8,
            "rest_time": 60,
            "duration": 0,
        }

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PATCH(self):
        self._handle("PATCH")

    def do_DELETE(self):
        self._handle("DELETE")

    do_PUT = do_POST


def writes():
    return [(m, p) for m, p, _ in WStub.log if m != "GET"]


class FakeHub(http.server.BaseHTTPRequestHandler):
    """The hub's egress routes for these tests: /egress/check answers allow; /egress/fetch puts the made-up token in
    place of the placeholder and forwards to the Jefit stub. seen: (route, handle, method, path, token leaked?)."""

    seen = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        a = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        leak = base.TOKEN in json.dumps(a) or base.TOKEN in base64.b64decode(a.get("body_b64") or "").decode()
        path = urllib.parse.urlsplit(a["url"]).path
        FakeHub.seen.append((self.path, a["handle"], a["method"], path, leak))
        if self.path == "/egress/check":
            return self._json(200, {"decision": "allow"})
        hs = {k: v.replace("hub:secret:gym", base.TOKEN) for k, v in a["headers"]}
        body = base64.b64decode(a["body_b64"]) if a.get("body_b64") else None
        req = urllib.request.Request(a["url"], data=body, headers=hs, method=a["method"])
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                st, reason, heads, raw = r.status, r.reason, list(r.headers.items()), r.read()
        except urllib.error.HTTPError as e:
            st, reason, heads, raw = e.code, e.reason, list(e.headers.items()), e.read()
        self._json(200, {"status": st, "reason": reason, "headers": heads, "body_b64": base64.b64encode(raw).decode()})

    def _json(self, code, d):
        raw = json.dumps(d).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def plan_routine():
    rs = [r for r in WStub.routines.values() if r["name"] == "Assistant plan"]
    assert len(rs) == 1, [r["name"] for r in WStub.routines.values()]
    return rs[0]


def day_exs(r, i):
    d = sorted(r["days"], key=lambda d: d["index"])[i]
    return sorted(d["day_exercises"], key=lambda e: e["index"])


class Plan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = base.load_module()
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), WStub)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        WStub.reset()
        self.e = base.Env(now=NOW)
        self.e.env.update(
            JEFIT_API_BASE=self.base,
            GYM_WRITE_DELAY="0",
            GYM_RETRY_BASE="0",
            GYM_TOKEN_RW_FILE=os.path.join(self.e.dir, "secrets", "jefit-token-rw"),
        )
        self.conn, self.xid = make_db(self.e.env["GYM_DB"], self.m)
        self.state = os.path.dirname(self.e.env["GYM_DB"])

    def tearDown(self):
        self.conn.close()
        self.e.close()

    def draft(self, *extra):
        return json.loads(self.e.run("plan", "draft", "--minutes", "60", "--json", *extra).stdout)

    def plan_file(self):
        with open(os.path.join(self.state, "plan.json")) as fh:
            return json.load(fh)

    def test_draft_picks_from_log_and_saves_privately(self):
        p = self.draft()
        a, b = p["days"]
        self.assertEqual(
            [x["name"] for x in a["exercises"]],
            [
                "Barbell Squat",
                "Barbell Bench Press",
                "Cable Lat Pulldown (Wide Grip)",
                "Machine Seated Leg Curl",
                "Dumbbell Lateral Raise",
                "Dumbbell Tricep Extension",
                "Weighted Crunch",
            ],
        )
        self.assertEqual([x["label"] for x in a["exercises"]], ["1", "2", "3a", "3b", "4a", "4b", "5"])
        self.assertEqual(b["exercises"][0]["name"], "Barbell Deadlift")
        self.assertNotIn("Machine Hack Squat", json.dumps(p))  # fewer sessions than the barbell squat
        x = {x["name"]: x for d in p["days"] for x in d["exercises"]}
        # bench 80 x 6 -> e1RM 96; 4-6 range starts at 96 / (1 + 7/30) = 77.8 -> 77.5 (barbell step 2.5)
        self.assertEqual((x["Barbell Bench Press"]["weight_kg"], x["Barbell Bench Press"]["reps"]), (77.5, 5))
        self.assertEqual((x["Barbell Squat"]["sets"], x["Barbell Squat"]["lo"], x["Barbell Squat"]["hi"]), (3, 4, 6))
        self.assertEqual(x["Machine Leg Press"]["weight_kg"], 140.0)  # 150 x 10 -> 200 / 1.4 = 142.9 -> step 5
        self.assertEqual(x["Barbell Squat"]["inc_kg"], 5.0)
        self.assertEqual(x["Machine Leg Press"]["inc_kg"], 10.0)
        self.assertIsNone(x["Hanging Leg Raise"]["weight_kg"])
        self.assertEqual(x["Barbell Bench Press"]["jefit_id"], jid("Barbell Bench Press"))
        mode = os.stat(os.path.join(self.state, "plan.json")).st_mode
        self.assertEqual(stat.S_IMODE(mode), 0o600)
        self.assertEqual(p["volume"]["chest"], 6.0)
        card = self.e.run("plan", "show").stdout
        self.assertIn("3a Cable Lat Pulldown (Wide Grip)  3×8–12 @ 55 kg", card)
        self.assertIn("not pushed yet", card)
        self.assertEqual(WStub.log, [])  # drafting never calls the API

    def test_draft_swap_minutes_and_dry_run(self):
        p = self.draft("--swap", "Dumbbell Tricep Extension=Cable Tricep Pushdown (Rope)")
        self.assertIn("Cable Tricep Pushdown (Rope)", [x["name"] for x in p["days"][0]["exercises"]])
        self.assertIn("not in your log", self.e.run("plan", "draft", "--swap", "A=Nope", code=2).stderr)
        self.assertIn("Old name=New name", self.e.run("plan", "draft", "--swap", "nonsense", code=2).stderr)
        short = json.loads(self.e.run("plan", "draft", "--minutes", "40", "--json", "--dry-run").stdout)
        self.assertTrue(any("left out" in n for n in short["notes"]))
        self.assertEqual(self.plan_file()["minutes"], 60)  # --dry-run did not overwrite
        other = base.Env(now=NOW)
        self.assertIn("no plan yet", other.run("plan", "push", code=2).stderr)
        other.close()

    def test_push_dry_run_sends_nothing(self):
        self.draft()
        self.e.token()
        out = self.e.run("plan", "push").stdout
        self.assertEqual(WStub.log, [])
        self.assertIn("Dry run: nothing sent", out)
        self.assertIn('POST   /api/v2/user/routines {"create_day":true}', out)
        self.assertIn('PATCH  /api/v2/routines/{T} {"data":{"name":"Assistant test"}}', out)
        self.assertIn(
            'POST   /api/v2/days/{{D1}}/day_exercises {{"data":{{"exercise":{{"id":"{}"}}}}}}'.format(
                jid("Barbell Squat")
            ),
            out,
        )
        self.assertIn(f'"weight_lbs":{round(77.5 / 0.45359237, 4)}', out)  # 77.5 kg in pounds
        self.assertNotIn(base.TOKEN, out)
        self.assertNotIn("/users/", out)
        res = json.loads(self.e.run("plan", "push", "--json").stdout)
        self.assertTrue(res["dry_run"] and res["test_sequence"])
        self.assertEqual({r["step"] for r in res["requests"]}, {"test", "plan"})

    def test_apply_tests_then_creates_only_its_routine(self):
        self.draft()
        self.e.token()
        refs_before = copy.deepcopy({rid: WStub.routines[rid] for rid, _ in REFS})
        out = self.e.run("plan", "push", "--apply").stdout
        self.assertIn("Set it as the current plan", out)
        self.assertIn("tap Sync on the Progress tab", out)  # the phone app pulls only on Sync
        w = writes()
        self.assertTrue(all(not any(rid in p for rid, _ in REFS) for _, p in w))  # never touched his routines
        self.assertEqual({rid: WStub.routines[rid] for rid, _ in REFS}, refs_before)
        test_id = re.search(r"u_\d{12}", w[1][1]).group(0)  # the PATCH that named the test routine
        self.assertEqual(w[0], ("POST", "/api/v2/user/routines"))
        self.assertIn(("DELETE", f"/api/v2/routines/{test_id}"), w)
        self.assertNotIn(test_id, WStub.routines)
        r = plan_routine()
        self.assertIn("[gym-plan]", r["description"])
        self.assertEqual(
            [d["name"] for d in sorted(r["days"], key=lambda d: d["index"])],
            ["A · Squat + Bench Press", "B · Deadlift + Military Press"],
        )
        a = day_exs(r, 0)
        self.assertEqual(a[1]["exercise"]["id"], jid("Barbell Bench Press"))
        self.assertEqual((a[1]["count"], len(a[1]["sets"]), a[1]["number_of_reps"]), (3, 3, [5, 5, 5]))
        self.assertAlmostEqual(a[1]["sets"][0]["weight_lbs"], 77.5 / 0.45359237, places=3)
        self.assertEqual(a[2]["rest_time"], 30)  # 3a: switch to the pair partner
        with open(os.path.join(self.state, "plan-pushed.json")) as fh:
            pushed = json.load(fh)
        self.assertEqual((pushed["status"], pushed["routine_id"]), ("complete", r["id"]))
        self.assertTrue(pushed["verified"])
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.state, "plan-push.log")).st_mode), 0o600)
        WStub.log = []
        self.assertIn("Jefit already matches", self.e.run("plan", "push").stdout)
        res = json.loads(self.e.run("plan", "push", "--apply", "--json").stdout)
        self.assertEqual(writes(), [])  # nothing to change, no second test
        self.assertFalse(res["app_sync"])  # no write, no Sync hint
        self.assertFalse(res["dry_run"])

    def test_update_progresses_and_pushes_only_the_diff(self):
        self.draft()
        self.e.token()
        self.e.run("plan", "push", "--apply")
        r = plan_routine()
        bench_de = day_exs(r, 0)[1]["id"]
        later = dt.date.fromisoformat(NOW) + dt.timedelta(days=2)
        add_session(self.conn, later, [("Barbell Bench Press", 77.5, 6), ("Barbell Squat", 92.5, 5)], self.xid)
        self.conn.commit()
        WStub.log = []
        dry = self.e.run("plan", "update", now="2026-10-21").stdout
        self.assertIn("Barbell Bench Press: 20.10. top of the range on every set: +2.5 kg", dry)
        self.assertIn("Not saved", dry)
        self.assertEqual(self.plan_file()["version"], 1)
        res = json.loads(self.e.run("plan", "update", "--apply", "--json", now="2026-10-21").stdout)
        self.assertEqual(res["version"], 2)
        self.assertTrue(res["push"]["app_sync"])  # changed in Jefit: Sync hint
        self.assertIn("Barbell Squat: target 6 reps", res["changes"])
        self.assertTrue(all(m == "PATCH" for m, _ in writes()))
        self.assertEqual(
            {p for _, p in writes()}
            - {f"/api/v2/day_exercises/{bench_de}", f"/api/v2/day_exercises/{day_exs(r, 0)[0]['id']}"},
            set(),
        )
        bench = day_exs(plan_routine(), 0)[1]
        self.assertAlmostEqual(bench["sets"][0]["weight_lbs"], 80 / 0.45359237, places=3)
        self.assertEqual(bench["number_of_reps"], [4, 4, 4])
        self.assertEqual(self.plan_file()["version"], 2)
        self.assertTrue(os.listdir(os.path.join(self.state, "plan-history")))

    def test_stalled_lift_is_swapped(self):
        self.draft()
        self.e.token()
        self.e.run("plan", "push", "--apply")
        for k in range(3):  # squat at the plan weight but stuck at 4 reps: three exposures without progress
            add_session(
                self.conn, dt.date(2026, 10, 20) + dt.timedelta(days=7 * k), [("Barbell Squat", 92.5, 4)], self.xid
            )
        self.conn.commit()
        WStub.log = []
        res = json.loads(self.e.run("plan", "update", "--apply", "--json", now="2026-11-05").stdout)
        self.assertIn("Barbell Squat → Machine Hack Squat (stalled 3 sessions)", res["changes"])
        a = day_exs(plan_routine(), 0)
        self.assertEqual(a[0]["exercise"]["id"], jid("Machine Hack Squat"))
        self.assertEqual(len(a), 7)
        self.assertIn("DELETE", {m for m, _ in writes()})
        self.assertNotIn(("POST", "/api/v2/user/routines"), writes())

    def test_guard_refuses_a_routine_changed_by_hand(self):
        self.draft()
        self.e.token()
        self.e.run("plan", "push", "--apply")
        plan_routine()["name"] = "My own now"
        self.draft("--swap", "Dumbbell Tricep Extension=Cable Tricep Pushdown (Rope)")
        WStub.log = []
        p = self.e.run("plan", "push", "--apply", code=1)
        self.assertIn("not touching it", p.stderr)
        self.assertEqual(writes(), [])

    def test_read_only_token_stops_at_the_first_write(self):
        self.draft()
        self.e.token()
        WStub.read_only = True
        p = self.e.run("plan", "push", "--apply", code=1)
        self.assertIn("write scope", p.stderr)
        self.assertNotIn(base.TOKEN, p.stdout + p.stderr)
        self.assertEqual(writes(), [("POST", "/api/v2/user/routines")])
        self.assertEqual(len(WStub.routines), 2)

    def egress_env(self, mode):
        """The live default token file under a throwaway HOME, the egress switch for "gym", a fake hub."""
        home = os.path.join(self.e.dir, "home")
        os.makedirs(os.path.join(home, ".config", "assistant"))
        with open(os.path.join(home, ".config", "assistant", "egress.json"), "w") as fh:
            json.dump({"handles": {"gym": mode}}, fh)
        hub = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeHub)
        threading.Thread(target=hub.serve_forever, daemon=True).start()
        self.addCleanup(hub.server_close)
        self.addCleanup(hub.shutdown)
        FakeHub.seen = []
        self.e.env.update(
            HOME=home,
            GYM_TOKEN_FILE=os.path.join(home, ".secrets", "jefit-token"),
            ASSISTANT_TEST="1",
            HUB_URL=f"http://127.0.0.1:{hub.server_address[1]}",
        )
        self.e.token()
        with open(os.path.join(home, ".secrets", "hub-token"), "w") as fh:
            fh.write("hub-tok\n")

    def test_push_with_the_read_token_goes_by_handle_through_the_hub(self):
        self.draft()
        self.egress_env("hub")
        self.e.run("plan", "push", "--apply")
        self.assertTrue(plan_routine())
        fetched = [(h, m, p) for v, h, m, p, _ in FakeHub.seen if v == "/egress/fetch"]
        self.assertEqual({h for h, _, _ in fetched}, {"gym"})
        self.assertEqual([(m, p) for _, m, p in fetched if m != "GET"], writes())  # every write went by the hub
        self.assertFalse(any(leak for *_, leak in FakeHub.seen))  # the token never left the "brain"

    def test_push_in_hub_mode_needs_no_token_file(self):
        self.draft()
        self.egress_env("hub")
        os.remove(self.e.env["GYM_TOKEN_FILE"])  # the hub holds the token
        self.e.run("plan", "push", "--apply")
        self.assertTrue(plan_routine())
        fetched = [(m, p) for v, h, m, p, _ in FakeHub.seen if v == "/egress/fetch" and h == "gym"]
        self.assertEqual([x for x in fetched if x[0] != "GET"], writes())
        self.assertFalse(any(leak for *_, leak in FakeHub.seen))

    def test_push_in_local_mode_without_the_token_file_is_a_clear_error(self):
        self.draft()
        self.egress_env("local")
        os.remove(self.e.env["GYM_TOKEN_FILE"])
        p = self.e.run("plan", "push", "--apply", code=1)
        self.assertIn("core.egress gym, local mode", p.stderr)
        self.assertEqual((writes(), FakeHub.seen), ([], []))

    def test_push_in_shadow_mode_checks_every_write(self):
        self.draft()
        self.egress_env("shadow")
        self.e.run("plan", "push", "--apply")
        checked = [(m, p) for v, _, m, p, _ in FakeHub.seen if v == "/egress/check" and m != "GET"]
        self.assertEqual(checked, writes())
        self.assertFalse(any(leak for *_, leak in FakeHub.seen))

    def test_writer_allowlist(self):
        w = self.m.PlanWriter({"base": self.base}, base.TOKEN, delay=0)
        w.owned.add("u_000000000555")
        self.assertTrue(w.allowed("POST", "/api/v2/user/routines"))
        self.assertTrue(w.allowed("PATCH", "/api/v2/routines/u_000000000555"))
        self.assertFalse(w.allowed("PATCH", "/api/v2/routines/u_000000000101"))  # not ours
        self.assertFalse(w.allowed("DELETE", "/api/v2/routines/u_000000000555"))  # owned but not deletable
        self.assertFalse(w.allowed("POST", "/api/v2/update-setting"))
        self.assertFalse(w.allowed("POST", "/api/v2/routines/u_000000000555/publish"))
        with self.assertRaises(self.m.PlanError):
            w.call("DELETE", "/api/v2/routines/u_000000000101")
        self.assertEqual(WStub.log, [])

    def pushed(self):
        with open(os.path.join(self.state, "plan-pushed.json")) as fh:
            return json.load(fh)

    def journal(self):
        with open(os.path.join(self.state, "plan-push.log")) as fh:
            return [json.loads(x) for x in fh]

    def test_429_is_retried_and_each_exercise_is_one_post_and_one_patch(self):
        p = self.draft()
        self.e.token()
        WStub.throttle = {"from": 12, "left": 2, "after": "0"}
        out = self.e.run("plan", "push", "--apply").stdout
        self.assertIn("2 rate-limit retries", out)
        self.assertEqual([j["status"] for j in self.journal()].count(429), 2)
        self.assertEqual(self.pushed()["status"], "complete")
        n = sum(len(d["exercises"]) for d in p["days"])
        fields = {"count", "sets", "number_of_reps", "rest_time"}
        batched = {
            pa for m, pa, b in WStub.log if m == "PATCH" and "/day_exercises/" in pa and set(b["data"]) == fields
        }
        self.assertEqual(len(batched), n)  # all four fields in one PATCH
        plan_writes = [w for w in writes() if w[0] != "GET"]
        self.assertEqual(len(plan_writes), 5 + 2 + 2 * n + 2 + 2)  # test 5, create+name, day patch+add, 2 retries
        self.assertEqual(len(day_exs(plan_routine(), 0)), len(p["days"][0]["exercises"]))

    def test_push_stopped_half_way_resumes_in_the_same_routine(self):
        p = self.draft()
        self.e.token()
        refs_before = copy.deepcopy({rid: WStub.routines[rid] for rid, _ in REFS})
        WStub.throttle = {"from": 20, "left": 99}  # write 20 is an exercise PATCH: never passes
        err = self.e.run("plan", "push", "--apply", code=1).stderr
        self.assertIn("still rate-limited", err)
        self.assertEqual(self.pushed()["status"], "incomplete")
        half = plan_routine()
        self.assertLess(sum(len(d["day_exercises"]) for d in half["days"]), sum(len(d["exercises"]) for d in p["days"]))
        self.assertIn("stopped half-way", self.e.run("plan", "show").stdout)
        dry = self.e.run("plan", "push").stdout
        self.assertIn("Resume:", dry)
        WStub.throttle, WStub.log = None, []
        out = self.e.run("plan", "push", "--apply").stdout
        self.assertIn("resumed the half-done push", out)
        self.assertIn("Set it as the current plan", out)
        w = writes()
        self.assertNotIn(("POST", "/api/v2/user/routines"), w)  # no second routine, no rebuild
        self.assertFalse([x for x in w if x[0] == "DELETE"])
        self.assertLess(len(w), 2 * sum(len(d["exercises"]) for d in p["days"]))
        r = plan_routine()
        self.assertEqual(r["id"], half["id"])
        for i, d in enumerate(p["days"]):
            self.assertEqual([e["exercise"]["id"] for e in day_exs(r, i)], [x["jefit_id"] for x in d["exercises"]])
            self.assertTrue(
                all(
                    e["count"] == x["sets"] and len(e["sets"]) == x["sets"]
                    for e, x in zip(day_exs(r, i), d["exercises"], strict=False)
                )
            )
        self.assertEqual(self.pushed()["status"], "complete")
        self.assertEqual({rid: WStub.routines[rid] for rid, _ in REFS}, refs_before)
        WStub.log = []
        self.e.run("plan", "push", "--apply")
        self.assertEqual(writes(), [])

    def test_leftovers_are_deleted_only_when_this_tool_created_them(self):
        self.draft()
        self.e.token()
        mine, his = "u_000000009001", "u_000000009002"
        for rid in (mine, his):
            WStub.routines[rid] = {
                "id": rid,
                "name": "Assistant test",
                "description": "",
                "last_updated": 5,
                "days": [],
            }
        os.makedirs(self.state, exist_ok=True)
        with open(os.path.join(self.state, "plan-push.log"), "a") as fh:
            fh.write(
                json.dumps({"method": "POST", "path": "/api/v2/user/routines", "status": 201, "new_id": mine}) + "\n"
            )
        err = self.e.run("plan", "push", "--apply", code=1).stderr
        self.assertIn("did not create", err)
        self.assertEqual(writes(), [])
        del WStub.routines[his]
        out = self.e.run("plan", "push", "--apply").stdout
        self.assertIn("deleted leftover Assistant test", out)
        self.assertEqual(writes()[0], ("DELETE", f"/api/v2/routines/{mine}"))
        self.assertNotIn(mine, WStub.routines)
        plan_routine()

    def test_created_ids_from_the_journal(self):
        path = os.path.join(self.e.dir, "push.log")
        with open(path, "w") as fh:
            for j in (
                {"method": "POST", "path": "/api/v2/user/routines", "status": 201},  # older line: no new_id
                {"method": "PATCH", "path": "/api/v2/routines/u_000000000777", "status": 200},
                {"method": "PATCH", "path": "/api/v2/routines/u_000000000778", "status": 200},
                {"method": "POST", "path": "/api/v2/user/routines", "status": 429},
                {"method": "PATCH", "path": "/api/v2/routines/u_000000000779", "status": 200},
                {"method": "POST", "path": "/api/v2/user/routines", "status": 201, "new_id": "u_000000000780"},
            ):
                fh.write(json.dumps(j) + "\n")
            fh.write("not json\n")
        self.assertEqual(self.m.created_routine_ids(path), {"u_000000000777", "u_000000000780"})

    def test_writer_retry_after_backoff_and_pacing(self):
        waits = []
        w = self.m.PlanWriter({"base": self.base}, base.TOKEN, delay=0, sleep=waits.append, backoff=5)
        WStub.throttle = {"from": 1, "left": 2, "after": "7", "get": True}
        w.call("GET", "/api/v2/user/routines")
        self.assertEqual((waits, w.retries), ([7.0, 7.0], 2))
        waits.clear()
        WStub.throttle = {"from": 1, "left": 99, "get": True}  # no Retry-After: 5 s doubling, cap 60
        with self.assertRaises(self.m.PlanError) as cm:
            w.call("GET", "/api/v2/user/routines")
        self.assertIn("still rate-limited", str(cm.exception))
        self.assertEqual(waits, [5, 10, 20, 40, 60, 60])
        WStub.throttle = {"from": 1, "left": 1, "after": "900", "get": True}
        with self.assertRaises(self.m.PlanError) as cm:
            w.call("GET", "/api/v2/user/routines")
        self.assertIn("asks to wait 15 min", str(cm.exception))
        WStub.throttle, waits[:] = None, []
        w = self.m.PlanWriter({"base": self.base}, base.TOKEN, delay=1.0, sleep=waits.append, backoff=0)
        for _ in range(3):
            w.call("GET", "/api/v2/user/routines")
        self.assertEqual(len(waits), 2)  # one pause before each later request
        self.assertTrue(all(0.5 < x <= 1.0 for x in waits), waits)
        now = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=30)
        self.assertAlmostEqual(self.m.retry_after_s(email.utils.format_datetime(now, usegmt=True)), 30, delta=3)
        self.assertEqual(
            (self.m.retry_after_s("12"), self.m.retry_after_s("soon"), self.m.retry_after_s(None)), (12.0, None, None)
        )
        self.assertNotIn(base.TOKEN, str(cm.exception))


class PlanNext(unittest.TestCase):
    """`gym plan next` (the instant /gym card): local data only, no API."""

    @classmethod
    def setUpClass(cls):
        cls.m = base.load_module()

    def setUp(self):
        self.e = base.Env(now=NOW)
        self.e.env.update(JEFIT_API_BASE="http://127.0.0.1:9")  # any API call would fail the run
        self.e.env["GYM_CALCACHE"] = "off"  # no live calendar in the card
        self.conn, self.xid = make_db(self.e.env["GYM_DB"], self.m)
        self.state = os.path.dirname(self.e.env["GYM_DB"])

    def tearDown(self):
        self.conn.close()
        self.e.close()

    def log(self, day, names):
        add_session(self.conn, dt.date.fromisoformat(day), [(n, LOG[n][1], LOG[n][2]) for n in names], self.xid)
        self.conn.commit()

    def plan(self):
        self.e.run("plan", "draft", "--minutes", "60", "--json")
        with open(os.path.join(self.state, "plan.json")) as fh:
            return json.load(fh)

    def test_no_plan_gives_the_summary_line(self):
        out = self.e.run("plan", "next").stdout
        self.assertEqual(out.splitlines()[0], "🏋️ Gym · no plan yet")
        self.assertRegex(
            out.splitlines()[1], r"^\d+ sessions in 4 wk \([\d.]+/wk\) · streak \d+ wk · last 11\.10\. \(7 d ago\)$"
        )

    def test_empty_log_and_no_plan(self):
        e = base.Env(now=NOW)
        e.env["GYM_CALCACHE"] = "off"
        try:
            self.assertEqual(e.run("plan", "next").stdout.strip(), "🏋️ Gym · no plan and no sessions logged yet")
        finally:
            e.close()

    def test_alternates_from_the_last_session(self):
        a, b = self.plan()["days"]

        def names(d):
            return [x["name"] for x in d["exercises"]]

        out = self.e.run("plan", "next").stdout  # last = full-body session: a tie
        self.assertTrue(out.startswith("🏋️ Next: A · "), out)
        self.assertIn("Last session 11.10. (7 d ago) · streak ", out)
        self.log("2026-10-20", names(a)[:4])  # partly A
        out = self.e.run("plan", "next", now="2026-10-21").stdout
        self.assertTrue(out.startswith("🏋️ Next: B · "), out)
        self.assertIn("Last session 20.10. (A, yesterday)", out)
        self.assertIn("1  Barbell Deadlift  3×4–6 @ ", out)
        self.assertIn("6b Hanging Leg Raise  2×8–12 @ bodyweight", out)
        self.assertIn("a/b = pair", out)
        self.assertIn("Plan v1 · not in Jefit yet", out)
        self.log("2026-10-22", names(b))
        out = self.e.run("plan", "next", now="2026-10-22").stdout
        self.assertTrue(out.startswith("🏋️ Next: A · "), out)
        self.assertIn("(B, today)", out)
        self.assertIn("3a Cable Lat Pulldown (Wide Grip)  3×8–12 @ ", out)
        self.assertLess(len(out), 1500)

    def test_push_state_and_sync_time(self):
        self.plan()
        with open(os.path.join(self.state, "plan-pushed.json"), "w") as fh:
            json.dump({"status": "complete", "plan_version": 1, "pushed_at": "2026-10-18T10:00:00"}, fh)
        self.conn.execute("INSERT INTO imports (at, source) VALUES ('2026-10-18T16:15:00+00:00', 'api')")
        self.conn.commit()
        last = self.e.run("plan", "next").stdout.strip().splitlines()[-1]
        self.assertEqual(last, "Plan v1 · in Jefit · log synced 18.10. 16:15")  # OWNER_TZ=UTC in tests
        with open(os.path.join(self.state, "plan-pushed.json"), "w") as fh:
            json.dump({"status": "complete", "plan_version": 0}, fh)
        self.assertIn("Jefit has v0", self.e.run("plan", "next").stdout)

    def test_json_block_for_the_dashboard(self):
        d = json.loads(self.e.run("plan", "next", "--json").stdout)  # no plan yet
        self.assertIsNone(d["plan"])
        self.assertIsNone(d["next"])
        self.assertIsNone(d["free_evenings"])  # GYM_CALCACHE=off
        self.assertEqual(d["last_session"], {"date": "2026-10-11", "days_ago": 7, "day": None})
        self.assertEqual(len(d["weeks"]), 8)
        self.assertEqual(d["week"], {"start": "2026-10-12", "sessions": 0, "target": None, "status": "open"})
        self.assertEqual([x["lift"] for x in d["lifts"]], ["squat", "bench", "deadlift"])
        a, _b = self.plan()["days"]
        self.log("2026-10-20", [x["name"] for x in a["exercises"]][:4])  # partly A
        d = json.loads(self.e.run("plan", "next", "--json", now="2026-10-21").stdout)
        self.assertEqual(
            d["week"], {"start": "2026-10-19", "sessions": 1, "target": d["plan"]["days_per_week"], "status": "gym"}
        )
        self.assertEqual(d["last_session"], {"date": "2026-10-20", "days_ago": 1, "day": "A"})
        self.assertEqual((d["next"]["key"], d["next"]["lead"]), ("B", "Barbell Deadlift"))
        self.assertGreater(d["streak_weeks"], 0)
        sq, bench, dl = d["lifts"]
        self.assertEqual(
            (sq["exercise"], sq["last"], sq["last_date"], sq["change"]), ("Barbell Squat", 116.7, "2026-10-20", 0.0)
        )  # 100 kg × 5, same as 4 weeks ago
        self.assertEqual(bench["exercise"], "Barbell Bench Press")
        self.assertEqual((dl["last_date"], dl["change"]), ("2026-10-11", 0.0))
        self.assertTrue(all(p[0] >= "2026-07-29" for p in sq["series"]) and sq["series"][-1] == ["2026-10-20", 116.7])
        d = json.loads(self.e.run("plan", "next", "--json", now="2026-12-01").stdout)  # nothing in the last 4 weeks
        self.assertEqual([x["change"] for x in d["lifts"]], [None, None, None])
        self.assertEqual(d["week"]["sessions"], 0)

    def test_plan_next_is_listed_in_usage(self):
        self.assertIn("draft | show | next | push", self.e.run("plan", code=None).stderr)


class Travel(unittest.TestCase):
    """`gym plan travel` / `push --travel` / `return`: bodyweight trip plan in its own routine, re-entry after."""

    @classmethod
    def setUpClass(cls):
        cls.m = base.load_module()
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), WStub)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        WStub.reset()
        self.e = base.Env(now=NOW)
        self.e.env.update(
            JEFIT_API_BASE=self.base,
            GYM_WRITE_DELAY="0",
            GYM_RETRY_BASE="0",
            GYM_CALCACHE="off",
            GYM_TOKEN_RW_FILE=os.path.join(self.e.dir, "secrets", "jefit-token-rw"),
            GYM_TRIPS=os.path.join(self.e.dir, "trips.json"),
        )
        self.conn, self.xid = make_db(self.e.env["GYM_DB"], self.m)
        self.state = os.path.dirname(self.e.env["GYM_DB"])

    def tearDown(self):
        self.conn.close()
        self.e.close()

    def travel(self, *extra):
        return json.loads(
            self.e.run("plan", "travel", "--from", "2026-11-20", "--to", "2026-12-06", "--json", *extra).stdout
        )

    def read(self, name):
        with open(os.path.join(self.state, name)) as fh:
            return json.load(fh)

    def push_main(self):
        self.e.run("plan", "draft", "--minutes", "60", "--json")
        self.e.token()
        self.e.run("plan", "push", "--apply")

    def test_travel_draft_is_bodyweight_from_the_catalogue(self):
        self.e.run("plan", "draft", "--minutes", "60", "--json")
        main_before = self.read("plan.json")
        p = self.travel()
        catalogue = {t[1] for c in self.m.TRAVEL_EX.values() for t in c}
        exs = [x for d in p["days"] for x in d["exercises"]]
        self.assertEqual((len(p["days"]), p["name"], p["kit"]), (2, "Assistant travel", []))
        self.assertTrue(
            all(x["jefit_id"] in catalogue and x["weight_kg"] is None and x["equipment"] == "bodyweight" for x in exs)
        )
        self.assertTrue(all(d["est_min"] <= 40 for d in p["days"]))
        self.assertEqual(p["trip"], {"name": None, "from": "2026-11-20", "to": "2026-12-06", "days": 17, "sessions": 5})
        a = {x["name"]: x for x in p["days"][0]["exercises"]}
        self.assertEqual((a["Split Squat"]["for"], a["Push-Up"]["for"]), ("Squat", "Bench Press"))
        self.assertEqual((a["Split Squat"]["sets"], a["Split Squat"]["lo"], a["Split Squat"]["hi"]), (3, 8, 12))
        self.assertFalse(any("Band" in x["name"] or x["name"] == "Pull-Up" for x in exs))
        for d in p["days"]:  # no exercise twice on one day
            self.assertEqual(len({x["name"] for x in d["exercises"]}), len(d["exercises"]))
        f = os.path.join(self.state, "plan-travel.json")
        self.assertEqual(stat.S_IMODE(os.stat(f).st_mode), 0o600)
        self.assertEqual(self.read("plan.json"), main_before)  # the main plan is not touched
        out = self.e.run("plan", "show", "--travel").stdout
        self.assertIn('Plan "Assistant travel" v1', out)
        self.assertIn("(17 days): ~5 sessions A B A B A", out)
        self.assertIn("Split Squat  3×8–12 @ bodyweight  rest 1:30  (for Squat)", out)
        self.assertIn("Jefit: not pushed yet", out)
        self.assertEqual(WStub.log, [])
        self.assertEqual(self.travel()["version"], 2)
        self.e.run("plan", "travel", "--from", "2026-11-20", "--to", "2026-12-06", "--dry-run")
        self.assertEqual(self.read("plan-travel.json")["version"], 2)  # a dry run saves nothing

    def test_travel_kit_days_and_trip_dates(self):
        with open(self.e.env["GYM_TRIPS"], "w") as fh:
            json.dump(
                {
                    "trips": {
                        "old": {"name": "Past", "from": "2026-01-01", "to": "2026-01-10"},
                        "isl": {"name": "Island", "from": "2026-11-20", "to": "2026-12-06"},
                    }
                },
                fh,
            )
        p = json.loads(self.e.run("plan", "travel", "--days-per-week", "3", "--band", "--bar", "--json").stdout)
        names = {x["name"] for d in p["days"] for x in d["exercises"]}
        self.assertEqual(
            (len(p["days"]), p["kit"], p["trip"]["name"], p["trip"]["sessions"]), (3, ["band", "bar"], "Island", 7)
        )
        self.assertTrue({"Pull-Up", "Band Reverse Fly", "Band Shoulder Press", "Band Alternating Bicep Curl"} <= names)
        self.assertNotIn("Back Extension (Prone)", names)
        self.assertTrue(all(x["equipment"] in ("band", "bar", "bodyweight") for d in p["days"] for x in d["exercises"]))
        self.assertIn("@ band", self.e.run("plan", "show", "--travel").stdout)
        os.remove(self.e.env["GYM_TRIPS"])
        r = self.e.run("plan", "travel", code=2)
        self.assertIn("give --from and --to", r.stderr)
        self.assertIn(
            "before --from", self.e.run("plan", "travel", "--from", "2026-12-06", "--to", "2026-11-20", code=2).stderr
        )

    def test_travel_push_dry_run_sends_nothing(self):
        self.assertIn("no travel plan yet", self.e.run("plan", "push", "--travel", code=2).stderr)
        self.travel()
        self.e.token()
        out = self.e.run("plan", "push", "--travel").stdout
        self.assertEqual(WStub.log, [])
        self.assertIn("Dry run: nothing sent. `gym plan push --travel --apply` would send", out)
        self.assertIn('"name":"Assistant travel"', out)
        self.assertIn("[gym-travel]", out)
        self.assertIn('{"data":{"exercise":{"id":"d_000000001341"}}}', out)  # Split Squat
        self.assertIn("After --apply: In the Jefit app, tap Sync", out)
        self.assertNotIn('Assistant plan"', out.split("After --apply")[0])

    def test_travel_push_never_touches_his_routines_or_the_main_plan(self):
        self.push_main()
        main = plan_routine()
        self.travel()
        before = copy.deepcopy(WStub.routines)
        WStub.log = []
        out = self.e.run("plan", "push", "--travel", "--apply").stdout
        self.assertIn('Jefit routine "Assistant travel" matches the travel plan', out)
        self.assertIn('switch back to "Assistant plan" after', out)
        self.assertIn('"Assistant travel" then shows under All Plans', out)  # Sync reminder
        tr = [r for r in WStub.routines.values() if r["name"] == "Assistant travel"]
        self.assertEqual(len(tr), 1)
        tr = tr[0]

        def own_ids(r):
            return {r["id"]} | {d["id"] for d in r["days"]} | {e["id"] for d in r["days"] for e in d["day_exercises"]}

        ours = own_ids(tr)
        for m, p in writes():
            self.assertTrue(p == "/api/v2/user/routines" or set(re.findall(r"u_\d{12}", p)) <= ours, (m, p))
        self.assertEqual({k: v for k, v in WStub.routines.items() if k != tr["id"]}, before)
        self.assertIn("[gym-travel]", tr["description"])
        plan = self.read("plan-travel.json")
        self.assertEqual(
            [[e["exercise"]["id"] for e in day_exs(tr, i)] for i in range(2)],
            [[x["jefit_id"] for x in d["exercises"]] for d in plan["days"]],
        )
        self.assertTrue(all(s.get("weight_lbs") is None for i in range(2) for e in day_exs(tr, i) for s in e["sets"]))
        self.assertFalse(any(m == "PATCH" and p.endswith(main["id"]) for m, p in writes()))
        self.assertEqual(self.read("plan-travel-pushed.json")["status"], "complete")
        self.assertEqual(self.read("plan-pushed.json")["routine_id"], main["id"])  # main push state untouched
        # redraft with a band: only a diff inside the travel routine
        self.travel("--band")
        before, ours_before = copy.deepcopy(WStub.routines), own_ids(WStub.routines[tr["id"]])
        WStub.log = []
        self.e.run("plan", "push", "--travel", "--apply")
        self.assertNotIn(("POST", "/api/v2/user/routines"), writes())
        ours = ours_before | own_ids(WStub.routines[tr["id"]])
        self.assertTrue(writes() and all(set(re.findall(r"u_\d{12}", p)) <= ours for _, p in writes()))
        self.assertEqual(
            {k: v for k, v in WStub.routines.items() if k != tr["id"]},
            {k: v for k, v in before.items() if k != tr["id"]},
        )
        # the main plan's push leaves the travel routine alone
        travel_now = copy.deepcopy(WStub.routines[tr["id"]])
        WStub.log = []
        self.e.run("plan", "push", "--apply")
        self.assertEqual(writes(), [])
        self.assertEqual(WStub.routines[tr["id"]], travel_now)

    def test_travel_push_refuses_a_hand_made_routine_of_that_name(self):
        self.push_main()
        WStub.routines["u_000000000555"] = {
            "id": "u_000000000555",
            "name": "Assistant travel",
            "description": "",
            "last_updated": 1700000000,
            "days": [],
        }
        self.travel()
        WStub.log = []
        r = self.e.run("plan", "push", "--travel", "--apply", code=1)
        self.assertIn("did not create", r.stderr)
        self.assertEqual(writes(), [])

    def test_return_drops_weights_and_writes_only_with_apply(self):
        self.push_main()
        plan = self.read("plan.json")
        WStub.log = []
        ret = json.loads(self.e.run("plan", "return", "--from", "2026-12-07", "--json").stdout)
        self.assertEqual(WStub.log, [])  # a draft sends nothing
        self.assertEqual((ret["from"], ret["to"], ret["applied"]), ("2026-12-07", "2026-12-20", {}))
        self.assertEqual(self.read("plan.json"), plan)
        bench = {
            w["week"]: next(x for d in w["plan"]["days"] for x in d["exercises"] if x["name"] == "Barbell Bench Press")
            for w in ret["weeks"]
        }
        pb = next(x for d in plan["days"] for x in d["exercises"] if x["name"] == "Barbell Bench Press")
        ref = 80 * (1 + 6 / 30) / (1 + (5 + 2) / 30)  # last sets 80 × 6 at the plan's 4-6 reps
        for wk, f in ((1, 0.85), (2, 0.90)):
            self.assertEqual(bench[wk]["weight_kg"], min(pb["weight_kg"], (ref * f // 2.5) * 2.5))
        self.assertEqual((bench[1]["sets"], bench[2]["sets"], bench[1]["reps"]), (2, 3, pb["lo"]))
        self.assertLess(bench[1]["weight_kg"], bench[2]["weight_kg"])
        self.assertLessEqual(bench[2]["weight_kg"], pb["weight_kg"])
        squat = next(x for d in ret["weeks"][0]["plan"]["days"] for x in d["exercises"] if x["name"] == "Barbell Squat")
        self.assertEqual(squat["weight_kg"], 80.0)  # 100 × 5: e1RM 116.7 → 94.6 × 0.85
        out = self.e.run("plan", "return", "--from", "2026-12-07").stdout
        self.assertIn("Draft only", out)
        self.assertIn("Barbell Bench Press  wk1 2×4 @ 65", out)
        # --apply: week 1 into "Assistant plan" only
        refs = copy.deepcopy({rid: WStub.routines[rid] for rid, _ in REFS})
        out = self.e.run("plan", "return", "--from", "2026-12-07", "--apply", "--week", "1").stdout
        self.assertIn("holds return week 1", out)
        self.assertIn("tap Sync", out)
        r = plan_routine()
        b = next(e for e in day_exs(r, 0) if e["exercise"]["id"] == jid("Barbell Bench Press"))
        self.assertEqual(b["count"], 2)
        self.assertAlmostEqual(b["sets"][0]["weight_lbs"], 65 / 0.45359237, places=3)
        self.assertEqual({rid: WStub.routines[rid] for rid, _ in REFS}, refs)
        self.assertEqual(self.read("plan.json"), plan)
        self.assertIn("1", self.read("plan-return.json")["applied"])
        # the plan push afterwards puts the plan weights back
        self.e.run("plan", "push", "--apply")
        b = next(e for e in day_exs(plan_routine(), 0) if e["exercise"]["id"] == jid("Barbell Bench Press"))
        self.assertEqual(b["count"], 3)
        self.assertAlmostEqual(b["sets"][0]["weight_lbs"], pb["weight_kg"] / 0.45359237, places=3)

    def test_return_from_defaults_to_the_day_after_the_trip_and_update_skips_it(self):
        self.push_main()
        self.travel()
        ret = json.loads(self.e.run("plan", "return", "--json").stdout)
        self.assertEqual(ret["from"], "2026-12-07")
        self.e.run("plan", "return", "--apply", "--week", "1")
        for day in ("2026-12-08", "2026-12-15"):  # deliberately light sessions
            add_session(self.conn, dt.date.fromisoformat(day), [("Barbell Bench Press", 60.0, 3)], self.xid)
        self.conn.commit()
        res = json.loads(self.e.run("plan", "update", "--json", now="2026-12-21").stdout)
        self.assertFalse(any("-10 %" in c for c in res["changes"]), res["changes"])
        self.assertEqual(res["sessions"], 0)


class OtherActivity(unittest.TestCase):
    """Dance (shuffle dancing, rave nights) from calcache titles: persisted per event, shown next to the sessions,
    never in the streak; a week with dance and no gym is 'active'."""

    @classmethod
    def setUpClass(cls):
        cls.m = base.load_module()

    def setUp(self):
        self.e = base.Env(now=NOW)
        self.e.env.update(JEFIT_API_BASE="http://127.0.0.1:9")
        self.conn, self.xid = make_db(self.e.env["GYM_DB"], self.m)
        self.state = os.path.dirname(self.e.env["GYM_DB"])
        self.feed = os.path.join(self.e.dir, "feed.json")
        cal = os.path.join(self.e.dir, "calcache")
        with open(cal, "w") as fh:  # stub: `calcache show --days 21 --json` prints the feed
            fh.write(f'#!/bin/sh\n[ "$1" = show ] || exit 2\ncat "{self.feed}"\n')
        rr = os.path.join(self.e.dir, "rave-radar")
        with open(rr, "w") as fh:
            fh.write('#!/bin/sh\necho \'[{"name": "Nova Pulse", "aliases": ["NPX"]}]\'\n')
        for f in (cal, rr):
            os.chmod(f, 0o755)
        self.e.env.update(GYM_CALCACHE=cal, GYM_RAVE_RADAR=rr)
        self.feed_with(os.path.join(base.FIX, "calcache-dance.json"))

    def tearDown(self):
        self.conn.close()
        self.e.close()

    def feed_with(self, path=None):
        with open(self.feed, "w") as fh:
            fh.write(open(path).read() if path else "[]")

    def nxt(self, now):
        return json.loads(self.e.run("plan", "next", "--json", now=now).stdout)

    def test_classify(self):
        evs = {x["id"]: x for x in json.load(open(os.path.join(base.FIX, "calcache-dance.json")))}

        def names():
            return ["nova pulse"]

        def k(i, a=names):
            return self.m.activity_kind(evs[i], a)

        self.assertEqual(k("ev-shuffle"), ("shuffle", 1.0))
        self.assertEqual(k("ev-artist"), ("rave", 6.0))  # artist with a typo
        self.assertIsNone(k("ev-artist", lambda: []))  # no rave-radar: no match
        self.assertEqual(k("ev-openair"), ("rave", 12.0))  # 14 h capped at 12
        for i in ("ev-leg", "ev-drinks", "ev-dinner", "ev-fest"):  # travel leg, < 2 h, no words, all-day
            self.assertIsNone(k(i), i)

    def test_persists_past_events_and_counts_finished_ones(self):
        d = self.nxt("2026-10-14T21:30")  # class still running
        self.assertEqual(d["other_activity"]["this_week"]["shuffle"], {"count": 0, "hours": 0.0})
        d = self.nxt("2026-10-14")  # end of that day
        self.assertEqual(
            d["other_activity"]["this_week"],
            {"shuffle": {"count": 1, "hours": 1.0}, "rave": {"count": 0, "hours": 0.0}},
        )
        path = os.path.join(self.state, "activity.json")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        raw = open(path).read()
        self.assertNotIn("Nova", raw)  # no titles stored
        self.assertEqual(json.loads(raw)["since"], "2026-10-14")
        self.feed_with()  # all left calcache's window
        d = self.nxt("2026-10-18T12:00")
        oa = d["other_activity"]
        self.assertEqual(oa["this_week"], {"shuffle": {"count": 1, "hours": 1.0}, "rave": {"count": 1, "hours": 6.0}})
        self.assertEqual(len(oa["weeks"]), 8)
        self.assertEqual(sorted(json.load(open(path))["events"]), ["ev-artist", "ev-shuffle"])  # future one dropped
        self.assertEqual(d["week"]["sessions"], 0)  # dance is not gym
        self.assertEqual(d["week"]["status"], "active")
        d2 = self.nxt("2026-10-20")
        self.assertGreater(d["streak_weeks"], 0)
        self.assertEqual(d2["streak_weeks"], 0)  # a dance-only week is no gym week
        st = {w["start"]: w["status"] for w in d2["weeks"]}
        self.assertEqual((st["2026-10-12"], st["2026-10-19"]), ("active", "open"))
        st = {w["start"]: w["status"] for w in self.nxt("2026-10-27")["weeks"]}
        self.assertEqual((st["2026-10-12"], st["2026-10-19"]), ("active", "missed"))
        self.assertEqual(d2["other_activity"]["total"]["rave"], {"count": 1, "hours": 6.0})

    def test_cards_stats_review(self):
        out = self.e.run("plan", "next", now="2026-10-18").stdout
        self.assertIn("\nDance: 🕺 shuffle 1 h · 🪩 rave 6 h this week · 8 wk: shuffle 1× 1 h, rave 1× 6 h", out)
        self.e.run("plan", "draft", "--minutes", "60", "--json")
        out = self.e.run("plan", "next", now="2026-10-20").stdout.splitlines()
        self.assertEqual(out[2], "Dance (8 wk): shuffle 1× 1 h, rave 1× 6 h")  # right under the last-session line
        s = json.loads(self.e.run("stats", "--json", now="2026-10-18").stdout)
        self.assertEqual(s["other_activity"]["this_week"]["rave"]["hours"], 6.0)
        self.assertIn(
            "Dance (not in the streak): shuffle 1× 1 h, rave 1× 6 h in 8 wk",
            self.e.run("stats", now="2026-10-18").stdout,
        )
        self.assertIn(
            "Also active: shuffle 1× 1 h, rave 1× 6 h dancing in 4 wk", self.e.run("review", now="2026-10-18").stdout
        )
        self.e.env["GYM_CALCACHE"] = "off"  # off: the stored file still counts
        self.assertIn("Dance: ", self.e.run("plan", "next", now="2026-10-18").stdout)


class EgressNoFile(unittest.TestCase):
    """The live default token file under a throwaway HOME, removed: hub mode syncs by handle through the fake
    hub (which puts the made-up token in place of the placeholder for the read-API stub); local mode is a clear
    error before anything is sent; status says how the token goes."""

    @classmethod
    def setUpClass(cls):
        cls.api = http.server.ThreadingHTTPServer(("127.0.0.1", 0), base.Stub)
        cls.hub = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeHub)
        for s in (cls.api, cls.hub):
            threading.Thread(target=s.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        for s in (cls.api, cls.hub):
            s.shutdown()
            s.server_close()

    def setUp(self):
        base.Stub.log, base.Stub.scheme = [], "Bearer"
        base.Stub.start_ts = int(dt.datetime(2026, 10, 12, 18, 0, tzinfo=dt.UTC).timestamp())
        FakeHub.seen = []
        self.e = base.Env(now=NOW)
        self.addCleanup(self.e.close)
        home = os.path.join(self.e.dir, "home")
        os.makedirs(os.path.join(home, ".config", "assistant"))
        os.makedirs(os.path.join(home, ".secrets"))
        with open(os.path.join(home, ".secrets", "hub-token"), "w") as fh:
            fh.write("hub-tok\n")
        self.switch = os.path.join(home, ".config", "assistant", "egress.json")
        self.e.env.update(
            HOME=home,
            GYM_TOKEN_FILE=os.path.join(home, ".secrets", "jefit-token"),
            ASSISTANT_TEST="1",
            JEFIT_API_BASE=f"http://127.0.0.1:{self.api.server_address[1]}",
            HUB_URL=f"http://127.0.0.1:{self.hub.server_address[1]}",
        )

    def mode(self, m):
        with open(self.switch, "w") as fh:
            json.dump({"handles": {"gym": m}}, fh)

    def test_hub_mode_syncs_without_the_token_file(self):
        self.mode("hub")
        self.assertFalse(os.path.exists(self.e.env["GYM_TOKEN_FILE"]))
        p = self.e.run("sync", "--json")
        out = json.loads(p.stdout)
        self.assertEqual((out["new"], out["skipped"]), (2, 1))
        self.assertTrue(base.Stub.log and all(m == "GET" for m, *_ in base.Stub.log))
        self.assertTrue(FakeHub.seen and {(v, h) for v, h, *_ in FakeHub.seen} == {("/egress/fetch", "gym")})
        self.assertFalse(any(leak for *_, leak in FakeHub.seen))
        self.assertNotIn(base.TOKEN, p.stdout + p.stderr)
        self.assertIn("Jefit token: by handle (hub)", self.e.run("status").stdout)
        self.assertEqual(json.loads(self.e.run("status", "--json").stdout)["token"], "by handle (hub)")

    def test_local_and_shadow_mode_without_the_token_file_are_a_clear_error(self):
        for m in ("local", "shadow"):
            self.mode(m)
            p = self.e.run("sync", code=3)
            self.assertIn(f"core.egress gym, {m} mode", p.stdout)
            self.assertIn("hub mode needs no file", p.stdout)
            self.assertEqual((base.Stub.log, FakeHub.seen), ([], []))
            self.assertIn("Jefit token: missing", self.e.run("status").stdout)

    def test_hub_mode_with_the_file_still_goes_by_handle(self):
        self.mode("hub")
        self.e.token()
        self.e.run("sync", "--json")
        self.assertTrue(FakeHub.seen and all(h == "gym" for _, h, *_ in FakeHub.seen))
        self.assertFalse(any(leak for *_, leak in FakeHub.seen))  # the placeholder went, never the value


if __name__ == "__main__":
    unittest.main()
