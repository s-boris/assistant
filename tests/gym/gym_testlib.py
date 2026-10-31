"""Shared helpers for the gym tests: the staged tools, synthetic Jefit exports (fixtures/, made-up data), a run
environment per test (Env: temp state, token and config paths; HOME is the module's temp home, tests/gym/conftest.py),
the loopback read-API stub (Stub), the routine-API stub (WStub), a fake hub (FakeHub) and a synthetic log (make_db).

The repo's gym and tg-cmd are staged once per process in a temp bin dir whose lib/ links the repo's core and
scripts/lib/legacy modules (as deployed in ~/bin/lib; nothing from the live ~/bin)."""

import atexit
import base64
import datetime as dt
import http.server
import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
_STAGED: list[Path] = []


def staged() -> Path:
    """bin/gym + bin/tg-cmd + bin/lib (core, the legacy lib modules), staged once per process."""
    if not _STAGED:
        d = Path(tempfile.mkdtemp(prefix="gym-bin-"))
        atexit.register(shutil.rmtree, d, True)
        (d / "lib").mkdir()
        for n in ("gym", "tg-cmd"):
            shutil.copy2(REPO / "scripts" / n, d / n)
        (d / "lib" / "core").symlink_to(REPO / "core")
        for f in sorted((REPO / "scripts" / "lib" / "legacy").glob("*.py")):
            (d / "lib" / f.name).symlink_to(f)
        _STAGED.append(d)
    return _STAGED[0]


HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")
KG, LATER, LB, MINIMAL = (
    os.path.join(FIX, f) for f in ("jefit-kg.csv", "jefit-kg-later.csv", "jefit-lb.csv", "jefit-minimal.csv")
)
TOKEN = "tok_TESTONLY_0123456789abcdef"
SECRET_VALUE = "valueThatMustNotLeak"


def load_module():
    loader = importlib.machinery.SourceFileLoader("gym_mod", str(staged() / "gym"))
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
            [sys.executable, str(staged() / "gym"), *args],
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
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
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
        leak = TOKEN in json.dumps(a) or TOKEN in base64.b64decode(a.get("body_b64") or "").decode()
        path = urllib.parse.urlsplit(a["url"]).path
        FakeHub.seen.append((self.path, a["handle"], a["method"], path, leak))
        if self.path == "/egress/check":
            return self._json(200, {"decision": "allow"})
        hs = {k: v.replace("hub:secret:gym", TOKEN) for k, v in a["headers"]}
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
