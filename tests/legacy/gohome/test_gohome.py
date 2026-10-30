"""Offline tests for ~/bin/gohome: a loopback fake transport API (locations, connections by departure/arrival) with a
fictional timetable, a fictional profile and fake place/calendar files. Nothing leaves the box.
Test a copy before installing: GOHOME_BIN=/path/to/copy ./run.sh"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import urllib.parse
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BIN = os.environ.get("GOHOME_BIN") or os.path.expanduser("~/bin/gohome")
TMP = tempfile.mkdtemp(prefix="gohome-tests-")
HOME_Q = "9999 Testdorf, Musterweg 7"  # home_place.query('home') with the geocoder off
STATIONS = ["Alpha Hbf", "Alpha Nord", "Gamma, Messeplatz", "Betaburg", "Delta"]
LOG = []
passed = failed = 0


def check(name, got, want=True):
    global passed, failed
    if got == want:
        passed += 1
        print(f"ok   - {name}")
    else:
        failed += 1
        print(f"FAIL - {name}\n       got:  {got!r}\n       want: {want!r}")


def iso(d, hm):
    """'2031-03-05', '22:37' (+ '+1' for the next day) -> the API's local time string."""
    day = datetime.strptime(d, "%Y-%m-%d") + timedelta(days=1 if hm.endswith("+1") else 0)
    return day.strftime("%Y-%m-%d") + "T" + hm[:5] + ":00+0100"


def ride(cat, num, a, at_a, b, at_b, pl=None):
    return {
        "journey": {"category": cat, "number": num, "to": b},
        "departure": {"station": {"name": a}, "departure": at_a, "platform": pl},
        "arrival": {"station": {"name": b}, "arrival": at_b},
    }


def walk(at, mins=3):
    return {
        "walk": {"duration": mins * 60},
        "journey": None,
        "departure": {"departure": None},
        "arrival": {"location": {"name": HOME_Q}, "arrival": at},
    }


def timetable(origin, d="2031-03-05"):
    """Fictional connections (Wed 05.03.2031) from origin to the home door, by departure."""
    out = []
    if origin in ("Alpha Hbf", "Alpha Nord"):
        for dep, arr, bus in [
            ("21:07", "22:05", "22:10"),
            ("21:37", "22:35", "22:40"),
            ("22:07", "23:05", "23:10"),
            ("22:37", "23:35", "23:40"),
            ("23:07", "00:05+1", "00:10+1"),
            ("23:52", "00:50+1", "00:55+1"),
            ("05:07+1", "06:05+1", "06:10+1"),
            ("05:37+1", "06:35+1", "06:40+1"),
        ]:
            out.append(
                {
                    "from": {"departure": iso(d, dep)},
                    "to": {"arrival": iso(d, bus)},
                    "sections": [
                        ride("S", "7", origin, iso(d, dep), "Testdorf, Musterweg", iso(d, arr), "3"),
                        walk(iso(d, bus)),
                    ],
                }
            )
        # an earlier-leaving but later-arriving option (pareto drops it)
        out.append(
            {
                "from": {"departure": iso(d, "22:05")},
                "to": {"arrival": iso(d, "23:50")},
                "sections": [
                    ride("B", "9", origin, iso(d, "22:05"), "Testdorf, Musterweg", iso(d, "23:45")),
                    walk(iso(d, "23:50")),
                ],
            }
        )
    elif origin == "Gamma, Messeplatz":
        for dep, chg, arr in [("22:12", "22:30", "23:45"), ("23:12", "00:15+1", "01:20+1")]:
            out.append(
                {
                    "from": {"departure": iso(d, dep)},
                    "to": {"arrival": iso(d, arr)},
                    "sections": [
                        ride("T", "4", origin, iso(d, dep), "Gamma", iso(d, dep[:3] + "20"), "B"),
                        ride(
                            "IC",
                            "5",
                            "Gamma",
                            iso(d, chg),
                            "Testdorf, Musterweg",
                            iso(d, arr[:3] + "15" + arr[5:]),
                            "8",
                        ),
                        walk(iso(d, arr)),
                    ],
                }
            )
    elif origin == "Betaburg":
        out.append(
            {
                "from": {"departure": iso(d, "22:20")},
                "to": {"arrival": iso(d, "23:00")},
                "sections": [
                    ride("RE", "12", origin, iso(d, "22:20"), "Testdorf, Musterweg", iso(d, "22:55"), "1"),
                    walk(iso(d, "23:00")),
                ],
            }
        )
    return sorted(out, key=lambda c: c["from"]["departure"])


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        LOG.append((u.path, q))
        if u.path.endswith("/locations"):
            w = q.get("query", "").lower().split()
            body = {
                "stations": [{"name": s} for s in STATIONS if any(x in s.lower() for x in w)]
                or [{"name": "Faraway Ost"}]
            }  # the real API always guesses something
        elif u.path.endswith("/connections"):
            when = q["date"] + "T" + q["time"] + ":00+0100"
            cs = timetable(q["from"])
            if q.get("isArrivalTime"):
                cs = [c for c in cs if c["to"]["arrival"] <= when][-int(q["limit"]) :]
            else:
                cs = [c for c in cs if c["from"]["departure"] >= when][: int(q["limit"])]
            body = {"connections": cs}
        else:
            self.send_response(404)
            self.end_headers()
            return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)


srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=srv.serve_forever, daemon=True).start()
API = f"http://127.0.0.1:{srv.server_address[1]}/v1"


def write(name, obj):
    p = os.path.join(TMP, name)
    with open(p, "w") as f:
        json.dump(obj, f)
    return p


PROFILE = write(
    "profile.json",
    {
        "home": {"street": "Musterweg 7", "postcode": "9999", "town": "Testdorf", "town_aliases": ["Testdorf Bahnhof"]},
        "work": {"town": "Betaburg"},
        "locale": {"tz": "Europe/Zurich"},
    },
)
VENUES = write(
    "venues.json", {"club zeta": {"stop": "Alpha Nord", "walk_min": 4, "town": "Alpha", "aliases": ["zeta"]}}
)
NO_PLACE = write("place-none.json", {"place": {"kind": "away", "town": None}})


def ev(title, start, end, town=None, travel=None, train=None, all_day=False):
    return {
        "start": f"2031-03-{start}:00+01:00",
        "end": f"2031-03-{end}:00+01:00",
        "all_day": all_day,
        "title": title,
        "town": town,
        "free": True,
        "flags": {"travel": travel},
        "train": train,
    }


def run(*args, now="2031-03-05 22:10", place=NO_PLACE, cal=None, api=None, **env):
    e = {
        **os.environ,
        "GOHOME_API": api or API,
        "GOHOME_NOW": now,
        "ASSISTANT_PROFILE": PROFILE,
        "HOME_PLACE_GEO": "off",
        "HOME_PLACE_CACHE": os.path.join(TMP, "hp.json"),
        "GOHOME_VENUES": VENUES,
        "GOHOME_PLACE": place or "off",
        "GOHOME_CALCACHE": cal or "off",
        "http_proxy": "",
        "https_proxy": "",
        "no_proxy": "127.0.0.1",
        **env,
    }
    p = subprocess.run([BIN, *args], capture_output=True, text=True, env=e, timeout=30)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def lines(out):
    return out.splitlines()


print("# typed stop")
del LOG[:]
rc, out, err = run("Alpha Hbf")
L = lines(out)
check("rc 0", rc, 0)
check("head names the stop and the time", L[0], "🏠 Home from Alpha Hbf · 22:10")
check("3 next, the last one among them: 4 lines", len(L), 4)
check(
    "next 1: stop, platform, line, changes, door arrival",
    L[1].split(" https://")[0],
    "22:37 Alpha Hbf Pl. 3 · S7 · direct → 🏠 23:40",
)
check(
    "next 3 by departure, the dominated 22:05 bus left out (and nothing before now)",
    [x[:5] for x in L[1:4]],
    ["22:37", "23:07", "23:52"],
)
check(
    "last tonight = the 23:52 reaching the door by 04:45, tagged in the list",
    L[3].split(" https://")[0],
    "23:52 Alpha Hbf Pl. 3 · S7 · direct → 🏠 00:55 · 🌙 last",
)
check(
    "every connection line carries one SBB app link",
    [x.count("https://app.sbbmobile.ch/timetable?") for x in L[1:4]],
    [1, 1, 1],
)
check(
    "SBB link: stop names only (from = first stop, to = last stop)",
    "from=Alpha%20Hbf&to=Testdorf%2C%20Musterweg&time=" in L[1],
    True,
)
check("the address never shows (the API gets it, the card does not)", "Musterweg 7" in out, False)
check(
    "the API was asked for the home address door to door",
    {q["to"] for p, q in LOG if p.endswith("/connections")},
    {HOME_Q},
)
check(
    "one arrive-by search for the last connection (by 04:45 next morning)",
    [(q["date"], q["time"]) for p, q in LOG if p.endswith("/connections") and q.get("isArrivalTime")],
    [("2031-03-06", "04:45")],
)
check("the card stays at <= 8 lines", len(L) <= 8, True)

rc, out, _ = run("Alpha Hbf", now="2031-03-05 21:00")
L = lines(out)
check(
    "earlier: last tonight on its own line",
    L[-1].split(" https://")[0],
    "🌙 Last tonight: 23:52 Alpha Hbf Pl. 3 · S7 · direct → 🏠 00:55",
)
check("earlier: 1 head + 3 next + 1 last", len(L), 5)
check("earlier: no 🌙 tag in the next 3", any("🌙" in x for x in L[1:4]), False)

rc, out, _ = run("Alpha Hbf", now="2031-03-06 01:30")
L = lines(out)
check("after the last one: says it left", L[-1], "🌙 The last one home by 04:45 left at 23:52.")
check("after the last one: the next are the morning ones", [x[:5] for x in L[1:3]], ["05:07", "05:37"])

rc, out, _ = run("Gamma, Messeplatz")
L = lines(out)
check(
    "changes and lines chained",
    L[1].split(" https://")[0],
    "22:12 Gamma, Messeplatz Pl. B · Tram 4 › IC5 · 1 change → 🏠 23:45",
)
check("a wait >= 30 min at a change is shown", "⏳ 55 min at Gamma" in L[2], True)
check("a short wait is not", "⏳" in L[1], False)
check("the SBB link ends at the last stop", "to=Testdorf%2C%20Musterweg" in L[1], True)

rc, out, _ = run("alpha")
check("a town name resolves to its station", lines(out)[0], "🏠 Home from Alpha Hbf · 22:10")
rc, out, _ = run("Nowhere Special")
check(
    "unknown stop: says so, no guessed route",
    out,
    "🚏 No stop called “Nowhere Special” found.\nSend /gohome <stop> with the station name.",
)
check("unknown stop: rc 0", rc, 0)
rc, out, _ = run("Delta")
check(
    "no connection at all: says so",
    lines(out)[1:],
    ["No connection home found from Delta.", "Check the stop name: /gohome <stop>."],
)

print("# home")
check('"home" -> you are home', run("home")[1], "You're home 🏠")
check("the home town -> you are home", run("Testdorf")[1], "You're home 🏠")
check(
    "the phone says home -> you are home",
    run(place=write("p-home.json", {"place": {"kind": "home"}}))[1],
    "You're home 🏠",
)
cal_now = write(
    "cal-now.json",
    [
        ev(
            "Rave Night",
            "05T21:00",
            "06T04:00",
            town="Gamma",
        ),
        ev(
            "🚆 Gamma, Messeplatz → Home",
            "06T01:12",
            "06T02:20",
            town="Gamma",
            travel="train",
            train={"from": "Gamma, Messeplatz", "to": "home"},
        ),
    ],
)
check(
    "home wins over a calendar event",
    run(place=write("p-home2.json", {"place": {"kind": "home"}}), cal=cal_now)[1],
    "You're home 🏠",
)
rc, out, _ = run(place=write("p-hometown.json", {"place": {"kind": "away", "town": "Testdorf"}}))
check("away but in the home town: close to home", lines(out)[0], "🏠 You're in Testdorf, close to home.")

print("# calendar")
rc, out, _ = run(cal=cal_now)
check(
    "event under way: its planned leg home gives the stop",
    lines(out)[0],
    "🏠 Home from Rave Night (Gamma, Messeplatz, calendar) · 22:10",
)
check("event: connections from that stop", lines(out)[1][:23], "22:12 Gamma, Messeplatz")
rc, out, _ = run(cal=cal_now, now="2031-03-06 06:30")
check("event ended 2.5 h ago: still used", "Rave Night" in lines(out)[0], True)
cal_old = write("cal-old.json", [ev("Dinner", "05T18:00", "05T19:00", town="Gamma")])
rc, out, _ = run(
    cal=cal_old,
    now="2031-03-05 22:10",
    place=write("p-beta.json", {"place": {"kind": "away", "town": "Betaburg", "fix_age_min": 5}}),
)
check("event ended over 3 h ago: the phone town instead", lines(out)[0], "🏠 Home from Betaburg (phone) · 22:10")
cal_venue = write("cal-venue.json", [ev("Night at Club Zeta", "05T21:00", "06T03:00", town="Alpha")])
check(
    "no leg: the venue map stop",
    lines(run(cal=cal_venue)[1])[0],
    "🏠 Home from Night at Club Zeta (Alpha Nord, calendar) · 22:10",
)
cal_town = write("cal-town.json", [ev("Concert", "05T20:00", "05T23:00", town="Alpha")])
check(
    "no leg, no venue: the event town", lines(run(cal=cal_town)[1])[0], "🏠 Home from Concert (Alpha, calendar) · 22:10"
)
check("event town -> its station for the connections", lines(run(cal=cal_town)[1])[1][:15], "22:37 Alpha Hbf")
cal_skip = write(
    "cal-skip.json",
    [
        ev(
            "🚆 Alpha Hbf → Gamma",
            "05T21:30",
            "05T22:30",
            town="Alpha",
            travel="train",
            train={"from": "Alpha Hbf", "to": "Gamma"},
        ),
        ev("Home party", "05T20:00", "05T23:00", town="Testdorf"),
        ev("Holiday", "05T00:00", "06T00:00", town="Gamma", all_day=True),
        ev("Somewhere", "05T20:00", "05T23:00", town=None),
    ],
)
check(
    "travel legs, home-town, all-day and town-less events are not where he is",
    run(cal=cal_skip)[1],
    "📍 Where are you? Neither the calendar nor your phone says.\nSend /gohome <stop>, e.g. a station name.",
)
check("a typed stop wins over the calendar", lines(run("Betaburg", cal=cal_now)[1])[0], "🏠 Home from Betaburg · 22:10")

print("# phone")
check(
    "zone Work without a town: the profile work town",
    lines(run(place=write("p-work.json", {"place": {"kind": "zone", "zone": "Work", "town": None}}))[1])[0],
    "🏠 Home from Betaburg (phone) · 22:10",
)
check(
    "an old fix says how old",
    lines(run(place=write("p-old.json", {"place": {"kind": "away", "town": "Betaburg", "fix_age_min": 190}}))[1])[0],
    "🏠 Home from Betaburg (phone, 3 h old) · 22:10",
)
check(
    "the vault answered: labelled vault, not phone",
    lines(
        run(
            place=write(
                "p-vault.json", {"source": "vault", "place": {"kind": "away", "town": "Betaburg", "fix_age_min": 190}}
            )
        )[1]
    )[0],
    "🏠 Home from Betaburg (vault, 3 h old) · 22:10",
)
check(
    "no place, no event: asks for a stop",
    run()[1],
    "📍 Where are you? Neither the calendar nor your phone says.\nSend /gohome <stop>, e.g. a station name.",
)
check("place read failing (bad file) = unknown", run(place=os.path.join(TMP, "missing.json"))[1].startswith("📍"), True)

print("# json, errors, usage")
rc, out, _ = run("Alpha Hbf", "--json")
j = json.loads(out)
check("json: keys", sorted(j), ["home", "last", "near", "next", "now", "origin", "source", "unknown_stop"])
check("json: a known stop has no unknown_stop", j["unknown_stop"], None)
rc, out, _ = run("Nowhere Special", "--json")
j2 = json.loads(out)
check(
    "json: unknown stop stays JSON (rc 0, no origin, the name in unknown_stop, no route)",
    (rc, j2["origin"], j2["unknown_stop"], j2["next"], j2["last"], j2["home"]),
    (0, None, "Nowhere Special", [], None, False),
)
j3 = json.loads(run("--json")[1])
check(
    "json: no place, no event = origin null, nothing else set",
    (j3["home"], j3["near"], j3["origin"], j3["unknown_stop"], j3["next"]),
    (False, None, None, None, []),
)
j4 = json.loads(run("--json", place=write("p-hometown-j.json", {"place": {"kind": "away", "town": "Testdorf"}}))[1])
check("json: in the home town = near set, origin null", (j4["near"], j4["origin"]), ("Testdorf", None))
check(
    "json: next 3 with stop/lines/link",
    [(c["stop"], c["lines"], c["link"].startswith("https://app.sbbmobile.ch/")) for c in j["next"]],
    [("Alpha Hbf", ["S7"], True)] * 3,
)
check("json: last", j["last"]["dep"][11:16], "23:52")
c0 = j["next"][0]
check(
    "json: last mile (final stop, its arrival, the walk to the door)",
    (c0["off"], c0["off_arr"][11:16], c0["walk_min"], c0["arr"][11:16]),
    ("Testdorf, Musterweg", "23:35", 5, "23:40"),
)
rc, out, err = run("Alpha Hbf", api="http://127.0.0.1:9/v1")
check("API down: rc 1", rc, 1)
check("API down: one stderr line", err.startswith("gohome: transport API unreachable") and "\n" not in err, True)
check("API down: nothing on stdout", out, "")
check("two arguments: rc 2", run("Alpha", "Hbf")[0], 2)
check("--help: rc 0", run("--help")[0], 0)

srv.shutdown()
for f in os.listdir(TMP):
    os.unlink(os.path.join(TMP, f))
os.rmdir(TMP)
print(f"pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
