"""Offline tests for ~/bin/night-plan (NIGHT_PLAN_BIN=/path/to/copy tests a copy).

A loopback fake of the transport API (shape of transport.opendata.ch: /locations by query or x/y, /connections by
from/to with departure or arrival time, date, limit; times with +0100 offsets) serves a synthetic timetable; every
request is recorded. http(s)_proxy point at the fake too, so a request to the real default URL can only reach it
(refused CONNECT): nothing leaves the box even if the seam breaks. Synthetic profile, state and config only.
Covers: outbound window and change preference, later option, return paging, Pareto filter, night gap (last back,
first morning), night line / night bus flags, transfer waits and taxi gaps, --end / --leave, all-night service,
EVENT_REF (exact, prefix, unknown, ambiguous, assumed start, nearest stop), town fallback, venue not found, abroad
-> border hub, --stop / --home / config home, card format, --json calendar events, no prices, home stop never shown,
API errors without tracebacks, usage errors, help.
"""

import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse

BIN = os.environ.get("NIGHT_PLAN_BIN") or os.path.expanduser("~/bin/night-plan")
DAY, NEXT = "2026-11-14", "2026-11-15"
HOME = "47.500000,8.500000"  # the synthetic profile's address, geocoded by the fake SearchServer


def t(hhmm, day=DAY):
    return f"{day}T{hhmm}:00+0100"


def ride(cat, num, frm, dep, to, arr, pl=None, day=DAY, aday=None):
    return {
        "walk": None,
        "journey": {"category": cat, "number": num, "to": to, "name": "000001"},
        "departure": {"departure": t(dep, day), "platform": pl, "station": {"name": frm}},
        "arrival": {"arrival": t(arr, aday or day), "platform": None, "station": {"name": to}},
    }


def walk(frm, dep, to, arr, day=DAY):
    return {
        "walk": {"duration": 300},
        "journey": None,
        "departure": {"departure": t(dep, day), "platform": None, "station": {"name": frm}},
        "arrival": {"arrival": t(arr, day), "station": {"name": to}},
    }


def conn(dep, arr, sections, dday=DAY, aday=None):
    return {
        "from": {"departure": t(dep, dday), "station": {"name": "x"}},
        "to": {"arrival": t(arr, aday or dday), "station": {"name": "y"}},
        "duration": "00d01:00:00",
        "transfers": max(sum(1 for s in sections if s["journey"]) - 1, 0),
        "sections": sections,
    }


def direct(frm, to, dep, arr, dday=DAY, aday=None, cat="S", num="5", pl="1"):
    return conn(dep, arr, [ride(cat, num, frm, dep, to, arr, pl, dday, aday)], dday, aday)


def returns_from(stop):
    """A Saturday night with a gap: night lines until 01:40, a stranded 02:10, then the morning trains."""
    N = NEXT
    return [
        direct(stop, "Testdorf", "23:10", "00:05", DAY, N),
        direct(stop, "Testdorf", "23:40", "00:35", DAY, N),
        direct(stop, "Testdorf", "00:10", "01:05", N),
        conn(
            "00:40",
            "01:50",
            [
                ride("S", "7", stop, "00:40", "Midtown", "01:00", "2", N),
                ride("SN", "3", "Midtown", "01:35", "Testdorf", "01:50", "41", N),
            ],
            N,
        ),
        conn("01:30", "03:00", [ride("SN", "3", stop, "01:30", "Testdorf", "03:00", "4", N)], N),  # dominated
        conn(
            "01:40",
            "02:50",
            [
                ride("SN", "3", stop, "01:40", "Midtown", "02:10", "4", N),
                ride("BN", "N12", "Midtown", "02:20", "Testdorf", "02:50", "C", N),
            ],
            N,
        ),
        conn(
            "02:10",
            "05:40",
            [
                ride("IR", "N1", stop, "02:10", "Midtown", "02:40", "7", N),
                ride("S", "5", "Midtown", "05:10", "Testdorf", "05:40", "41", N),
            ],
            N,
        ),
        direct(stop, "Testdorf", "04:50", "05:45", N),
    ] + [
        direct(stop, "Testdorf", f"{m // 60:02d}:{m % 60:02d}", f"{(m + 55) // 60:02d}:{(m + 55) % 60:02d}", N)
        for m in (320, 350, 380, 410, 440, 470, 500, 560)
    ]  # 05:20 ... 09:20, 55 min each


def all_night_from(stop):
    out = []
    for i in range(30):  # every 30 min from 23:00 to 13:30, night lines included
        m = 23 * 60 + i * 30
        d = DAY if m < 24 * 60 else NEXT
        hh, mm = divmod(m % (24 * 60), 60)
        a = m + 40
        ad = DAY if a < 24 * 60 else NEXT
        ah, am = divmod(a % (24 * 60), 60)
        out.append(
            direct(
                stop, "Testdorf", f"{hh:02d}:{mm:02d}", f"{ah:02d}:{am:02d}", d, ad, cat="SN" if 1 <= hh < 5 else "S"
            )
        )
    return out


def outbound_to(stop):
    return [
        direct("Testdorf", stop, "21:40", "22:35", pl="2"),
        direct("Testdorf", stop, "22:10", "23:20", pl="2"),  # too early
        conn(
            "22:40",
            "23:35",
            [
                walk(HOME, "22:40", "Testdorf", "22:45"),
                ride("S", "5", "Testdorf", "22:45", "Midtown", "23:05", "3"),
                ride("T", "4", "Midtown", "23:15", stop, "23:35", "B"),
            ],
        ),
        conn(
            "22:42",
            "23:44",
            [
                ride("B", "21", "Testdorf, Post", "22:42", "A", "22:50"),
                ride("S", "6", "A", "22:55", "B", "23:10", "1"),
                ride("S", "8", "B", "23:15", "C", "23:30", "2"),
                ride("B", "2", "C", "23:35", stop, "23:44"),
            ],
        ),
        direct("Testdorf", stop, "23:10", "00:05", DAY, NEXT),
    ]


def timetable():
    st = lambda i, n, x=47.2, y=8.2, **k: {"id": i, "name": n, "coordinate": {"x": x, "y": y}, **k}  # noqa: E731
    big = st("8500010", "Bigcity")
    tt = {
        "locations": {
            "Club Nova, Bigcity": [st("8500001", "Bigcity, Club Nova"), big],
            "Allnight, Bigcity": [st("8500002", "Bigcity, Allnight")],
            "Warehouse, Bigcity": [big],
            "Bigcity, Warehouse": [big],
            "Bigcity": [big],
            "Messehalle, Bigcity": [big],
            "Bigcity, Messehalle": [],
            "47.1,8.1": [
                {"id": None, "name": "Hallenweg 2, 9999 Bigcity", "distance": 40.3},
                st("8500020", "Bigcity, Messe", distance=120),
            ],
            "Arena, Farcity": [],
            "Farcity, Arena": [],
            "Farcity": [st("8000123", "Farcity Hbf", 48.14, 11.56)],
            "Nowhere, Atlantis": [],
            "Atlantis, Nowhere": [],
            "Atlantis": [],
            "Atlantis Hbf": [],
            "Atlantis Centraal": [],
            "Bigcity, Hallentor": [st("8500030", "Bigcity, Hallentor")],
        },
        "connections": {},
        "raw": {},
    }
    c = tt["connections"]
    for stop in ("Bigcity, Club Nova", "Bigcity, Messe", "Bigcity", "Bigcity, Hallentor"):
        c[f"{HOME}|{stop}"] = outbound_to(stop)
        c[f"{stop}|{HOME}"] = returns_from(stop)
    c["Elsewhere 5|Bigcity, Club Nova"] = outbound_to("Bigcity, Club Nova")
    c["Bigcity, Club Nova|Elsewhere 5"] = returns_from("Bigcity, Club Nova")
    c[f"{HOME}|Bigcity, Allnight"] = outbound_to("Bigcity, Allnight")
    c[f"Bigcity, Allnight|{HOME}"] = all_night_from("Bigcity, Allnight")
    # the real API answers foreign stops with connections from other days: these must be ignored
    tt["raw"][f"{HOME}|Farcity Hbf"] = [direct("Testdorf", "Farcity Hbf", "16:45", "21:46", "2026-11-12")]
    hub_out = [
        conn(f"{h}:10", f"{h + 2}:30", [ride("IR", "13", "Testdorf", f"{h}:10", "St. Margrethen", f"{h + 2}:30", "5")])
        for h in (15, 16, 17, 18)
    ]
    c[f"{HOME}|St. Margrethen"] = hub_out
    c[f"St. Margrethen|{HOME}"] = returns_from("St. Margrethen")
    return tt


class Fake(http.server.BaseHTTPRequestHandler):
    tt, log, mode = None, [], ["ok"]

    def log_message(self, *a):
        pass

    def do_CONNECT(self):
        Fake.log.append("CONNECT " + self.path)
        self.send_response(403)
        self.end_headers()

    def do_GET(self):
        Fake.log.append("GET " + self.path)
        u = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        m = Fake.mode
        if m[0] == "close":
            self.close_connection = True
            return
        if m[0] == "junk":
            return self.reply(200, b"<html>maintenance</html>")
        if m[0] == "code":
            return self.reply(int(m[1]), json.dumps({"errors": [{"message": m[2]}]} if len(m) > 2 else {}).encode())
        if u.path.endswith("/SearchServer"):  # fake swisstopo geocoder for the home address
            hit = {"attrs": {"label": "Probeweg 7 <b>1234 Testdorf</b>", "lat": 47.5, "lon": 8.5}}
            other = {"attrs": {"label": "Andereweg 7 <b>1234 Testdorf</b>", "lat": 46.0, "lon": 7.0}}
            return self.reply(
                200, json.dumps({"results": [other, hit] if "Probeweg" in q.get("searchText", "") else []}).encode()
            )
        if u.path.endswith("/locations"):
            key = q.get("query") or f"{q.get('x')},{q.get('y')}"
            return self.reply(200, json.dumps({"stations": Fake.tt["locations"].get(key, [])}).encode())
        if u.path.endswith("/connections"):
            key = f"{q.get('from')}|{q.get('to')}"
            lim = int(q.get("limit") or 4)
            when = f"{q['date']}T{q['time']}"
            if key in Fake.tt["raw"]:
                sel = Fake.tt["raw"][key]
            elif q.get("isArrivalTime") == "1":
                sel = [c for c in Fake.tt["connections"].get(key, []) if c["to"]["arrival"][:16] <= when][-lim:]
            else:
                sel = [c for c in Fake.tt["connections"].get(key, []) if c["from"]["departure"][:16] >= when][:lim]
            return self.reply(200, json.dumps({"connections": sel}).encode())
        self.reply(404, b"{}")

    def reply(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class NightPlan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Fake.tt = timetable()
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.tmp = tempfile.TemporaryDirectory()
        d = cls.tmp.name
        port = cls.srv.server_address[1]
        with open(os.path.join(d, "profile.json"), "w") as f:
            json.dump({"home": {"town": "Testdorf", "street": "Probeweg 7", "postcode": "1234"}}, f)
        os.makedirs(os.path.join(d, "state"))

        def ev(i, **k):
            return {
                "id": i,
                "kind": "show",
                "artist": "DJ Nova",
                "title": "DJ Nova",
                "venue": "Messehalle",  # noqa
                "city": "Bigcity",
                "lat": 47.1,
                "lon": 8.1,
                "date": DAY,
                "url": "https://club.example/e/nova",
                "source": "x",
                **k,
            }

        with open(os.path.join(d, "state", "state.json"), "w") as f:
            json.dump(
                {
                    "events": {
                        "abcdef1234": ev("abcdef1234"),
                        "bbbb000001": ev("bbbb000001", title="Nova Night"),
                        "bbbb000002": ev("bbbb000002"),
                        "cccc000003": ev("cccc000003", date=None),
                    }
                },
                f,
            )
        cls.env = dict(
            os.environ,
            NIGHT_PLAN_API=f"http://127.0.0.1:{port}/v1",
            ROUTE_API="",
            NIGHT_PLAN_CONFIG=os.path.join(d, "none.json"),
            NIGHT_PLAN_VENUES=os.path.join(d, "none.json"),
            ASSISTANT_PROFILE=os.path.join(d, "profile.json"),
            RAVE_RADAR_STATE_DIR=os.path.join(d, "state"),
            HOME=d,
            HOME_PLACE_GEO=f"http://127.0.0.1:{port}/SearchServer",
            HOME_PLACE_CACHE=os.path.join(d, "home-place.json"),
            http_proxy=f"http://127.0.0.1:{port}",
            https_proxy=f"http://127.0.0.1:{port}",
            HTTP_PROXY=f"http://127.0.0.1:{port}",
            HTTPS_PROXY=f"http://127.0.0.1:{port}",
            no_proxy="127.0.0.1",
            NO_PROXY="127.0.0.1",
        )

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.tmp.cleanup()

    def setUp(self):
        Fake.log.clear()
        Fake.mode = ["ok"]

    def run_np(self, *args, env=None):
        p = subprocess.run(
            [BIN, *args],
            capture_output=True,
            text=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
            env={**self.env, **(env or {})},
        )
        return p.returncode, p.stdout, p.stderr

    def plan(self, *args, **kw):
        code, out, err = self.run_np(*args, "--json", **kw)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def gets(self, path):
        return [
            dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(x.split(" ", 1)[1]).query))
            for x in Fake.log
            if x.startswith("GET ") and path in x
        ]

    BASE = ("--venue", "Club Nova, Bigcity", "--date", DAY, "--start", "23:00", "--title", "DJ Nova")

    # ---------------------------------------------------------------- the main plan
    def test_card(self):
        code, out, err = self.run_np(*self.BASE)
        self.assertEqual(code, 0, err)
        lines = out.strip().split("\n")
        self.assertEqual(lines[0], "🎧 DJ Nova · Sat 14.11 · 23:00")
        self.assertEqual(lines[1], "📍 Club Nova, Bigcity · stop Bigcity, Club Nova")
        self.assertEqual(lines[2], "🚆 There 22:40 → 23:35 · 1 change · then 22:42")
        self.assertEqual(lines[3], "   22:45 S5 Testdorf pl 3 › 23:15 Tram 4 Midtown pl B › 23:35 Bigcity, Club Nova")
        self.assertEqual(lines[4], "🌙 Last back 01:40 → 02:50 · 1 change · 🌙 night bus")
        self.assertEqual(lines[5], "   01:40 SN3 Bigcity, Club Nova pl 4 › 02:20 Bus N12 Midtown pl C › 02:50 Testdorf")
        self.assertEqual(
            lines[6], "🌅 First morning 02:10 → 05:40 · 1 change · 🌙 night line · ⚠ taxi gap: 2 h 30 wait at Midtown"
        )
        self.assertLessEqual(len(lines), 10)
        self.assertNotIn("Probeweg", out)  # the home stop never shows
        self.assertNotIn("CHF", out)  # no prices

    def test_requests(self):
        self.run_np(*self.BASE)
        self.assertFalse([x for x in Fake.log if x.startswith("CONNECT")])
        cs = self.gets("/connections")
        self.assertEqual(
            cs[0],
            {
                "from": HOME,
                "to": "Bigcity, Club Nova",
                "date": DAY,
                "time": "23:45",
                "isArrivalTime": "1",
                "limit": "6",
            },
        )
        rets = [q for q in cs if q["from"] == "Bigcity, Club Nova"]
        self.assertEqual([(q["date"], q["time"]) for q in rets], [(DAY, "23:00"), (NEXT, "04:51")])  # paged
        self.assertLessEqual(len(cs), 10)

    def test_json_calendar(self):
        p = self.plan(*self.BASE)
        self.assertEqual(p["outbound"]["dep"], "2026-11-14T22:40:00+01:00")
        self.assertEqual(p["later_option"]["dep"], "2026-11-14T22:42:00+01:00")
        self.assertEqual(p["last_back"]["dep"], "2026-11-15T01:40:00+01:00")
        self.assertEqual(p["first_morning"]["dep"], "2026-11-15T02:10:00+01:00")
        self.assertEqual(p["night_gap_min"], 170)
        self.assertIsNone(p["after_end"])
        cal = p["calendar"]
        self.assertEqual([e["kind"] for e in cal], ["event", "outbound", "return"])
        for e in cal:
            self.assertTrue(
                {"summary", "startTime", "endTime", "allDay", "location", "description", "timeZone"} <= set(e)
            )  # voice-plan / create_event shape
            self.assertFalse({"start", "end"} & set(e))
            self.assertIs(e["allDay"], False)
            self.assertEqual(e["timeZone"], "Europe/Zurich")
            self.assertLess(e["startTime"], e["endTime"])
        ev, out, ret = cal
        self.assertEqual(
            (ev["summary"], ev["startTime"], ev["endTime"], ev["location"]),
            ("🎧 DJ Nova", "2026-11-14T23:00:00", "2026-11-15T01:30:00", "Club Nova, Bigcity"),
        )
        self.assertIn("End time unknown", ev["description"])
        self.assertEqual(
            (out["summary"], out["startTime"], out["endTime"], out["location"]),
            ("🚆 Home → Bigcity, Club Nova", "2026-11-14T22:40:00", "2026-11-14T23:35:00", "Testdorf, platform 3"),
        )
        self.assertTrue(out["description"].startswith("22:40 walk 5 min › 22:45 S5 Testdorf pl 3 › "))  # door
        self.assertIn("Fallback: 22:42 → 23:44", out["description"])
        for e in (out, ret):  # short leg: chain, fallback, SBB link
            self.assertLessEqual(len(e["description"].split("\n")), 4)
            self.assertNotIn("DJ Nova", e["description"])  # venue/tickets stay on the main event
        self.assertIn("details added by assistant", ev["description"])
        self.assertEqual(
            out["description"].split("\n")[-1],
            "SBB: https://app.sbbmobile.ch/timetable?from=Testdorf&to=Bigcity%2C%20Club%20Nova&time=1794692700",
        )  # stop names: the app can't resolve an address
        self.assertEqual(
            (ret["summary"], ret["startTime"], ret["endTime"]),
            ("🚆 Bigcity, Club Nova → Home", "2026-11-15T01:40:00", "2026-11-15T02:50:00"),
        )
        self.assertIn("Fallback: First morning 02:10 → 05:40", ret["description"])
        self.assertIn("SBB: https://app.sbbmobile.ch/timetable?from=", ret["description"])
        blob = json.dumps(p, ensure_ascii=False)
        self.assertNotIn("Probeweg", blob)
        self.assertNotIn("CHF", blob)

    # ---------------------------------------------------------------- private venue map
    def venues(self, data):
        path = os.path.join(self.tmp.name, "venues.json")
        with open(path, "w") as f:
            f.write(data if isinstance(data, str) else json.dumps(data))
        return {"NIGHT_PLAN_VENUES": path}

    VMAP = {
        "Secret Häll": {
            "stop": "Bigcity, Club Nova",
            "walk_min": 5,
            "address": "Hidden Lane 1, Bigcity",
            "aliases": ["the vault room"],
        }
    }

    def test_venue_map(self):
        env = self.venues(self.VMAP)
        args = ("--venue", "Secret Hall, Bigcity", "--date", DAY, "--start", "23:00", "--end", "04:00", "--title", "X")
        code, out, err = self.run_np(*args, env=env)
        self.assertEqual(code, 0, err)
        self.assertEqual(out.split("\n")[1], "📍 Secret Hall, Bigcity · stop Bigcity, Club Nova · +5 min walk")
        self.assertFalse(self.gets("/locations"))  # the map wins: no name/coordinate lookup
        cs = self.gets("/connections")
        self.assertEqual((cs[0]["to"], cs[0]["time"]), ("Bigcity, Club Nova", "23:40"))  # 45 min minus the walk
        p = self.plan(*args, env=env)
        self.assertEqual((p["stop"], p["stop_how"], p["walk_min"]), ("Bigcity, Club Nova", "map", 5))
        ev = p["calendar"][0]
        self.assertEqual(ev["location"], "Hidden Lane 1, Bigcity")
        self.assertIn("Stop: Bigcity, Club Nova (+5 min walk)", ev["description"])
        self.assertGreaterEqual(p["after_end"]["dep"], "2026-11-15T04:15:00+01:00")  # end + exit 10 + walk 5
        # aliases, the town suffix and --address / --stop overrides
        p = self.plan(
            "--venue",
            "The Vault Room Bigcity, Bigcity",
            "--date",
            DAY,
            "--start",
            "23:00",
            "--address",
            "Elsewhere 2",
            env=env,
        )
        self.assertEqual((p["stop_how"], p["calendar"][0]["location"]), ("map", "Elsewhere 2"))
        p = self.plan(
            "--venue",
            "Secret Hall, Bigcity",
            "--date",
            DAY,
            "--start",
            "23:00",
            "--stop",
            "Bigcity, Club Nova",
            env=env,
        )
        self.assertEqual((p["stop_how"], p["walk_min"]), ("given", 0))

    def test_venue_map_bad_or_unmatched(self):
        p = self.plan(*self.BASE, env=self.venues(self.VMAP))  # Club Nova is not in the map
        self.assertEqual((p["stop_how"], p["walk_min"]), ("name", 0))
        self.assertNotIn("walk", p["card"])
        p = self.plan(*self.BASE, env=self.venues({"club nova": {"walk_min": 3}, "x": "junk"}))  # no stop: ignored
        self.assertEqual(p["stop_how"], "name")
        p = self.plan(*self.BASE, env=self.venues({"club nova": {"stop": "Bigcity, Messe", "town": "Othertown"}}))
        self.assertEqual(p["stop_how"], "name")  # same name in another town
        p = self.plan(*self.BASE, env=self.venues({"club nova": {"stop": "Bigcity, Club Nova", "town": "BIGCITY"}}))
        self.assertEqual(p["stop_how"], "map")
        code, out, err = self.run_np(*self.BASE, env=self.venues("{broken"))
        self.assertEqual(code, 2)
        self.assertIn("venues.json", err)

    def test_pareto_drops_dominated(self):
        p = self.plan(*self.BASE)
        self.assertNotIn("01:30", p["card"])

    def test_end_after_morning(self):
        p = self.plan(*self.BASE, "--end", "05:00")
        self.assertEqual(p["end"], "2026-11-15T05:00:00+01:00")
        self.assertEqual(p["after_end"]["dep"], "2026-11-15T05:20:00+01:00")  # 05:00 + 10 min exit
        self.assertEqual(p["after_end"]["flags"], [])
        self.assertIn("↩ After 05:00 05:20 → 06:15 · direct", p["card"])
        self.assertIn("🎧 DJ Nova · Sat 14.11 · 23:00–05:00", p["card"])
        cal = {e["kind"]: e for e in p["calendar"]}
        self.assertEqual(cal["return"]["startTime"], "2026-11-15T05:20:00")
        self.assertEqual(cal["event"]["endTime"], "2026-11-15T05:00:00")

    def test_end_wait_and_exit_min(self):
        p = self.plan(*self.BASE, "--end", "04:00")
        self.assertEqual(p["after_end"]["dep"], "2026-11-15T04:50:00+01:00")
        self.assertEqual(p["after_end"]["flags"], ["⏳ 50 min wait after the end"])
        p = self.plan(*self.BASE, "--end", "04:00", "--exit-min", "55")
        self.assertEqual(p["after_end"]["dep"], "2026-11-15T05:20:00+01:00")

    def test_leave_taxi_gap(self):
        p = self.plan(*self.BASE, "--leave", "03:00")
        self.assertEqual(p["after_end"]["dep"], "2026-11-15T04:50:00+01:00")
        self.assertIn("⚠ taxi gap: 1 h 50 wait after 03:00", p["card"])

    def test_end_equal_to_last_back_merges(self):
        p = self.plan(*self.BASE, "--leave", "01:40")
        self.assertIn("↩ After 01:40 = last back 01:40 → 02:50", p["card"])
        self.assertEqual(p["card"].count("01:40 → 02:50"), 1)

    def test_all_night_service(self):
        p = self.plan("--venue", "Allnight, Bigcity", "--date", DAY, "--start", "23:00")
        self.assertIsNone(p["last_back"])
        self.assertIsNone(p["first_morning"])
        self.assertIn("connections all night (night network): no gap", p["notes"])
        self.assertIn("🌙 Way home: connections all night", p["card"])
        self.assertEqual([e["kind"] for e in p["calendar"]], ["event", "outbound"])
        p = self.plan("--venue", "Allnight, Bigcity", "--date", DAY, "--start", "23:00", "--end", "04:00")
        self.assertEqual(p["after_end"]["dep"], "2026-11-15T04:30:00+01:00")
        self.assertEqual(p["calendar"][-1]["kind"], "return")

    def test_arrive_window_option(self):
        p = self.plan(*self.BASE, "--arrive-after", "0-25")
        self.assertEqual(p["outbound"]["dep"], "2026-11-14T22:10:00+01:00")
        self.assertEqual(self.gets("/connections")[0]["time"], "23:25")
        p = self.plan(*self.BASE, "--arrive-after", "50-60")  # nothing inside: closest before
        self.assertEqual(p["outbound"]["dep"], "2026-11-14T22:42:00+01:00")
        self.assertTrue(any("no arrival 50-60 min" in n for n in p["notes"]))

    # ---------------------------------------------------------------- EVENT_REF and places
    def test_event_ref(self):
        p = self.plan("abcdef1234")
        self.assertEqual(p["title"], "DJ Nova")
        self.assertEqual(p["start"], "2026-11-14T22:00:00+01:00")
        self.assertTrue(any("assumed 22:00" in n for n in p["notes"]))
        self.assertEqual((p["stop"], p["stop_how"]), ("Bigcity, Messe", "near"))
        self.assertIn("📍 Messehalle, Bigcity · stop Bigcity, Messe (120 m)", p["card"])
        self.assertEqual(p["event_id"], "abcdef1234")
        self.assertIn("Info/tickets: https://club.example/e/nova", p["calendar"][0]["description"])
        self.assertIn({"x": "47.1", "y": "8.1", "type": "station"}, self.gets("/locations"))

    def test_event_ref_prefix_start_title(self):
        p = self.plan("abcd", "--start", "23:00", "--end", "05:00")
        self.assertEqual((p["event_id"], p["start"]), ("abcdef1234", "2026-11-14T23:00:00+01:00"))
        self.assertFalse(any("assumed" in n for n in p["notes"]))
        p = self.plan("bbbb000001", "--start", "23:00")
        self.assertEqual(p["title"], "DJ Nova @ Nova Night")

    def test_event_ref_errors(self):
        for ref, want in (
            ("zzzz9999", "not found"),
            ("bbbb", "ambiguous"),
            ("abc", "not found"),
            ("cccc000003", "has no date"),
        ):
            code, out, err = self.run_np(ref)
            self.assertEqual(code, 2, ref)
            self.assertIn(want, err)
        code, out, err = self.run_np("abcdef1234", env={"RAVE_RADAR_STATE_DIR": self.tmp.name + "/nope"})
        self.assertEqual(code, 2)

    def test_town_fallback(self):
        p = self.plan("--venue", "Warehouse, Bigcity", "--date", DAY, "--start", "23:00")
        self.assertEqual((p["stop"], p["stop_how"]), ("Bigcity", "town"))
        self.assertIn("ℹ venue stop unknown: routed to Bigcity station", p["card"])

    def test_stop_override(self):
        p = self.plan(
            "--venue", "Hallentor, Bigcity", "--date", DAY, "--start", "23:00", "--stop", "Bigcity, Hallentor"
        )
        self.assertEqual((p["stop"], p["stop_how"]), ("Bigcity, Hallentor", "given"))
        self.assertEqual(self.gets("/connections")[0]["to"], "Bigcity, Hallentor")

    def test_venue_not_found(self):
        code, out, err = self.run_np("--venue", "Nowhere, Atlantis", "--date", DAY, "--start", "23:00")
        self.assertEqual(code, 1)
        self.assertIn("not found in the Swiss timetable", err)
        self.assertNotIn("Traceback", err)

    def test_abroad_border_hub(self):
        p = self.plan("--venue", "Arena, Farcity", "--date", DAY, "--start", "22:00")
        self.assertEqual(p["stop"], "Farcity Hbf")
        self.assertEqual(p["border_hub"]["name"], "St. Margrethen")
        self.assertIn("stop St. Margrethen (border hub)", p["card"])
        self.assertTrue(any("abroad" in n and "St. Margrethen" in n for n in p["notes"]))
        cal = {e["kind"]: e for e in p["calendar"]}
        self.assertEqual(cal["outbound"]["summary"], "🚆 Home → St. Margrethen")
        self.assertIn(cal["outbound"]["startTime"][:10], (DAY,))  # never the other day's train
        self.assertEqual(cal["return"]["summary"], "🚆 St. Margrethen → Home")

    def test_config_border_hub(self):
        cfg = os.path.join(self.tmp.name, "hubs.json")
        with open(cfg, "w") as f:
            json.dump({"border_hubs": [["Testdorf", 47.9, 11.0], "junk"]}, f)
        code, out, err = self.run_np(
            "--venue", "Arena, Farcity", "--date", DAY, "--start", "22:00", "--json", env={"NIGHT_PLAN_CONFIG": cfg}
        )
        self.assertEqual(code, 1)  # no timetable to the config hub in the fake
        self.assertIn("no connection from home to Testdorf", err)

    def test_home_sources(self):
        self.run_np(*self.BASE, "--home", "Elsewhere 5")
        self.assertEqual(self.gets("/connections")[0]["from"], "Elsewhere 5")
        cfg = os.path.join(self.tmp.name, "cfg.json")
        with open(cfg, "w") as f:
            json.dump({"home": "Elsewhere 5", "arrive_after": [30, 45], "exit_min": 10}, f)
        Fake.log.clear()
        code, out, err = self.run_np(*self.BASE, env={"NIGHT_PLAN_CONFIG": cfg})
        self.assertEqual(code, 0, err)
        self.assertEqual(self.gets("/connections")[0]["from"], "Elsewhere 5")

    # ---------------------------------------------------------------- failures and usage
    def test_api_errors(self):
        for mode, want in (
            (["code", "500", "Server overloaded"], "transport API error: Server overloaded"),
            (["code", "429"], "transport API error: HTTP 429"),
            (["junk"], "transport API unreachable or broken"),
            (["close"], "transport API unreachable or broken"),
        ):
            Fake.mode = mode
            code, out, err = self.run_np(*self.BASE)
            self.assertEqual(code, 1, mode)
            self.assertIn(want, err)
            self.assertNotIn("Traceback", err)
        code, out, err = self.run_np(*self.BASE, env={"NIGHT_PLAN_API": "http://127.0.0.1:9/v1"})
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", err)

    def test_usage(self):
        for args in (
            (),
            ("--venue", "Club Nova, Bigcity"),
            ("--venue", "X, Y", "--date", DAY, "--start", "25:00"),
            ("--venue", "X, Y", "--date", "14.11.2026", "--start", "23:00"),
            (*self.BASE, "--arrive-after", "50-40"),
            (*self.BASE, "--end", "late"),
        ):
            code, out, err = self.run_np(*args)
            self.assertEqual(code, 2, args)
            self.assertNotIn("Traceback", err)
        self.assertEqual(Fake.log, [])

    def test_help(self):
        code, out, err = self.run_np("--help")
        self.assertEqual(code, 0)
        self.assertIn("EVENT_REF", out)
        self.assertIn("Prices     never", out)


class SbbLinkTest(unittest.TestCase):
    """lib/sbb.py next to the tested script: the timetable deep link used in leg descriptions."""

    def setUp(self):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(BIN)), "lib"))
        import sbb

        self.link, self.web = sbb.sbb_link, sbb.sbb_web_link

    def test_link(self):
        self.assertEqual(
            self.link("Testdorf Süd", "Bigcity, Hafen", "2026-11-14", "7:05"),  # app link, epoch s
            "https://app.sbbmobile.ch/timetable?from=Testdorf%20S%C3%BCd&to=Bigcity%2C%20Hafen&time=1794636300",
        )
        self.assertEqual(
            self.web("Testdorf Süd", "Bigcity, Hafen", "2026-11-14", "7:05"),
            "https://www.sbb.ch/en?stops=Testdorf+S%C3%BCd~Bigcity%2C+Hafen&day=2026-11-14&time=07_05&moment=dep",
        )
        self.assertIn(
            "stops=Testdorf_I8599001~Midtown&", self.web("Testdorf", "Midtown", "2026-11-14", "22:45", "8599001", "x1")
        )  # only numeric ids pin
        for f in (self.link, self.web):
            for bad in (
                ("", "B", "2026-11-14", "22:45"),
                ("A", "B", "14.11.2026", "22:45"),
                ("A", "B", "2026-11-14", "2245"),
            ):
                with self.assertRaises(ValueError):
                    f(*bad)


if __name__ == "__main__":
    unittest.main(verbosity=1)
