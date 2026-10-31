#!/usr/bin/env python3
"""Tests for ~/bin/calcache (+ train-watch sync --from-cache and the 06:40 crontab cue). Fictional events and a
fixture profile only (home town "Bachwil", work town "Werkstadt")."""

import json
import os
import pwd
import re
import stat
import subprocess
import tempfile
import unittest

HOME = pwd.getpwuid(os.getuid()).pw_dir  # the real home: its bin/lib (CI runs suites under a temp HOME)
CC = os.environ.get("CALCACHE_BIN") or os.path.join(HOME, "bin/calcache")
TW = os.environ.get("TRAIN_WATCH_BIN") or os.path.join(HOME, "bin/train-watch")
# the 06:40 cue: calcache-age's ~/crontab line (registry cron_line; cron-back 29.10, jobwrap retired): the tree's copy
# in CI, else the repo checkout's
TREE = os.environ.get("ASSISTANT_CI_TREE") or os.path.join(HOME, "opt", "assistant")
REGISTRY = os.path.join(TREE, "core", "schedules.toml")
NOW = "2026-03-02T09:00:00+01:00"  # a Monday; window 02.03 -> 23.03

PROFILE = {
    "home": {"street": "Lindenweg 4", "postcode": "9999", "town": "Bachwil", "town_aliases": ["Bachwil Dorf"]},
    "work": {"town": "Werkstadt"},
    "locale": {"tz": "Europe/Zurich"},
}


def link(a, b):
    from urllib.parse import quote

    return f"https://app.sbbmobile.ch/timetable?from={quote(a)}&to={quote(b)}&time=1772000000"


def timed(i, title, s, e, loc=None, desc=None, **kw):
    ev = {
        "id": f"ev{i}",
        "status": "confirmed",
        "summary": title,
        "start": {"dateTime": s, "timeZone": "Europe/Zurich"},
        "end": {"dateTime": e, "timeZone": "Europe/Zurich"},
    }
    if loc:
        ev["location"] = loc
    if desc:
        ev["description"] = desc
    ev.update(kw)
    return ev


def allday(i, title, s, e, loc=None, **kw):
    ev = {
        "id": f"ev{i}",
        "status": "confirmed",
        "summary": title,
        "start": {"date": f"{s}T00:00:00Z"},
        "end": {"date": f"{e}T00:00:00Z"},
    }
    if loc:
        ev["location"] = loc
    ev.update(kw)
    return ev


EVENTS = [
    timed(1, "Old thing", "2026-03-01T10:00:00+01:00", "2026-03-01T11:00:00+01:00"),
    allday(2, "@Werkstadt", "2026-03-03", "2026-03-04", transparency="transparent"),
    timed(
        3,
        "Climbing",
        "2026-03-03T19:00:00+01:00",
        "2026-03-03T21:00:00+01:00",
        "Kletterhalle Nord, Felsstrasse 12, 4051 Basel",
        "Bring shoes. Secret door code 1234.",
    ),
    timed(
        4,
        "🚆 Home → Basel SBB",
        "2026-03-03T17:30:00+01:00",
        "2026-03-03T18:40:00+01:00",
        "Bahnhof Bachwil, Gleis 2",
        "17:30 walk 5 min from home › 17:40 S33 Bachwil pl 2 › 18:05 IC3 Zürich HB › 18:40 Basel SBB · 1 change\n"
        "SBB: " + link("Bachwil", "Basel SBB"),
    ),
    timed(
        5,
        "🚆 Basel SBB → Home",
        "2026-03-03T22:10:00+01:00",
        "2026-03-03T23:30:00+01:00",
        "Basel SBB, Gleis 7",
        "22:10 IC3 Basel SBB pl 7 › 23:10 Bus 5 Bachwil › walk 3 min › 23:30 home\nSBB: "
        + link("Basel SBB", "Bachwil"),
    ),
    timed(
        6,
        "Rave",
        "2026-03-06T22:00:00+01:00",
        "2026-03-07T06:00:00+01:00",
        "Halle 7, Hafenstrasse 3, 8005 Zürich",
        transparency="transparent",
    ),
    timed(7, "Movie night", "2026-03-05T20:00:00+01:00", "2026-03-06T05:00:00+01:00", "9999 Bachwil"),
    allday(8, "Ski weekend", "2026-03-07", "2026-03-09", "Hotel Alpenblick, 3920 Zermatt"),
    allday(9, "Lyon visit", "2026-03-12", "2026-03-13", "Musée X, Rue de la Paix 3, 69002 Lyon, France"),
    timed(10, "✈️ ZRH → LIS", "2026-03-14T07:00:00+01:00", "2026-03-14T09:30:00+01:00", "Zürich Flughafen"),
    timed(11, "🚗 Drive to the lake", "2026-03-15T10:00:00+01:00", "2026-03-15T11:00:00+01:00", "Seeweg 5"),
    timed(12, "Cancelled lunch", "2026-03-04T12:00:00+01:00", "2026-03-04T13:00:00+01:00", status="cancelled"),
    timed(
        13,
        "Declined meeting",
        "2026-03-04T14:00:00+01:00",
        "2026-03-04T15:00:00+01:00",
        attendees=[{"email": "x@example.org", "self": True, "responseStatus": "declined"}],
    ),
    timed(14, "Far future", "2026-03-30T10:00:00+02:00", "2026-03-30T11:00:00+02:00"),
    timed(15, "Dinner", "2026-03-04T19:30:00+01:00", "2026-03-04T22:00:00+01:00", "Luzern"),
    timed(
        16,
        "🚆 Bern → Thun",
        "2026-03-20T09:00:00+01:00",
        "2026-03-20T09:20:00+01:00",
        "Bern, Gleis 3",
        "09:00 S1 Bern pl 3 › 09:20 Thun",
    ),
    timed(
        17,
        "🚆 Bachwil → Bern",
        "2026-03-28T08:00:00+01:00",
        "2026-03-28T09:30:00+01:00",
        "Bahnhof Bachwil",
        "08:00 IC8 Bachwil › 09:30 Bern\nSBB: " + link("Bachwil", "Bern"),
    ),
]


class CalCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.dir = os.path.join(t, "cc")
        prof = os.path.join(t, "profile.json")
        with open(prof, "w") as f:
            json.dump(PROFILE, f)
        self.env = dict(
            os.environ,
            CALCACHE_DIR=self.dir,
            CALCACHE_NOW=NOW,
            ASSISTANT_PROFILE=prof,
            TRAIN_WATCH_DIR=os.path.join(t, "trains"),
            TRAIN_WATCH_INBOX=os.path.join(t, "inbox"),
            TRAIN_WATCH_NOW=NOW,
            HOME_PLACE_GEO="off",
            HOME_PLACE_CACHE=os.path.join(t, "hp.json"),
        )
        self.raw = self.write("raw.json", {"events": EVENTS, "timeZone": "Europe/Zurich"})

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, data):
        p = os.path.join(self.tmp.name, name)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        return p

    def run_cc(self, *args, stdin=None, env=None):
        return subprocess.run([CC, *args], env=env or self.env, input=stdin, capture_output=True, text=True, timeout=60)

    def imp(self, *extra):
        r = self.run_cc("import", self.raw, *extra)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def cache(self):
        with open(os.path.join(self.dir, "events.json"), encoding="utf-8") as f:
            return json.load(f)

    def ev(self, i):
        return next(e for e in self.cache()["events"] if e["id"] == f"ev{i}")

    # ------------------------------------------------------------------ import
    def test_import_counts_window_and_skips(self):
        r = self.imp()
        self.assertIn("cached 12 events", r.stdout)
        ids = {e["id"] for e in self.cache()["events"]}
        for gone in ("ev1", "ev12", "ev13", "ev14", "ev17"):
            self.assertNotIn(gone, ids)
        c = self.cache()
        self.assertEqual(c["window"]["start"][:10], "2026-03-02")
        self.assertEqual(c["window"]["end"][:10], "2026-03-23")

    def test_file_modes_and_no_private_text(self):
        self.imp()
        p = os.path.join(self.dir, "events.json")
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.dir).st_mode), 0o700)
        txt = open(p, encoding="utf-8").read()
        for bad in (
            "Felsstrasse",
            "Hafenstrasse",
            "Rue de la Paix",
            "Seeweg",
            "Lindenweg",
            "door code",
            "Bring shoes",
            "Alpenblick",
            "Kletterhalle",
            '"description"',
            '"location"',
        ):
            self.assertNotIn(bad, txt)

    def test_towns(self):
        self.imp()
        self.assertEqual(self.ev(3)["town"], "Basel")
        self.assertEqual(self.ev(2)["town"], "Werkstadt")  # @Town marker
        self.assertEqual(self.ev(4)["town"], "Bachwil")  # station of the home town
        self.assertEqual(self.ev(5)["town"], "Basel")
        self.assertEqual(self.ev(7)["town"], "Bachwil")
        self.assertEqual(self.ev(8)["town"], "Zermatt")
        self.assertEqual(self.ev(9)["town"], "Lyon")
        self.assertEqual(self.ev(15)["town"], "Luzern")
        self.assertEqual(self.ev(16)["town"], "Bern")
        self.assertIsNone(self.ev(11)["town"])  # a street alone is never a town

    def test_flags(self):
        self.imp()
        f = {i: self.ev(i)["flags"] for i in (2, 3, 4, 6, 7, 8, 9, 10, 11, 15)}
        self.assertEqual(f[4]["travel"], "train")
        self.assertEqual(f[10]["travel"], "flight")
        self.assertEqual(f[11]["travel"], "drive")
        self.assertIsNone(f[3]["travel"])
        self.assertTrue(f[3]["busy_evening"] and f[15]["busy_evening"] and f[4]["busy_evening"])
        self.assertFalse(f[11]["busy_evening"] or f[2]["busy_evening"])
        self.assertTrue(f[6]["away_overnight"])  # night in Zürich
        self.assertFalse(f[7]["away_overnight"])  # night at home
        self.assertTrue(f[8]["away_overnight"])  # 2-day all-day elsewhere
        self.assertTrue(f[9]["away_overnight"])  # all-day abroad
        self.assertTrue(f[10]["away_overnight"])  # ✈️
        self.assertFalse(f[3]["away_overnight"] or f[2]["away_overnight"])
        self.assertTrue(self.ev(2)["free"] and self.ev(6)["free"])
        self.assertFalse(self.ev(3)["free"])
        self.assertEqual(self.ev(2)["start"], "2026-03-03")
        self.assertTrue(self.ev(2)["all_day"])

    def test_abroad_and_id_flags(self):
        raw = self.write(
            "raw2.json",
            {
                "events": [
                    timed(
                        31,
                        "Club night",
                        "2026-03-06T23:00:00+01:00",
                        "2026-03-07T06:00:00+01:00",
                        "8005 Zürich",
                        "Doors 23:00. 18+ only, bring an official photo ID.",
                    ),
                    timed(
                        32,
                        "Gig",
                        "2026-03-07T20:00:00+01:00",
                        "2026-03-07T23:00:00+01:00",
                        "8005 Zürich",
                        "Idea: bring the ID card",
                    ),
                    timed(33, "Bar (Ausweis)", "2026-03-08T20:00:00+01:00", "2026-03-08T23:00:00+01:00"),
                    timed(
                        34,
                        "Concert",
                        "2026-03-09T20:00:00+01:00",
                        "2026-03-09T23:00:00+01:00",
                        desc="Age 1890s band, 180+ minutes",
                    ),
                    allday(35, "\U0001f1e7\U0001f1ea Customer visit (Werkstadt)", "2026-03-12", "2026-03-13"),
                    allday(36, "\U0001f1e8\U0001f1ed Swiss day", "2026-03-13", "2026-03-14"),
                    allday(
                        9, "Lyon visit", "2026-03-12", "2026-03-13", "Musée X, Rue de la Paix 3, 69002 Lyon, France"
                    ),
                ],
                "timeZone": "Europe/Zurich",
            },
        )
        r = self.run_cc("import", raw)
        self.assertEqual(r.returncode, 0, r.stderr)
        f = {i: self.ev(i)["flags"] for i in (31, 32, 33, 34, 35, 36, 9)}
        self.assertTrue(f[31]["id_check"] and f[32]["id_check"] and f[33]["id_check"])
        self.assertFalse(f[34]["id_check"])  # "1890s", "180+" are not age limits
        self.assertTrue(f[35]["abroad"] and f[9]["abroad"])  # foreign flag / foreign country in the location
        self.assertFalse(f[36]["abroad"] or f[31]["abroad"])  # home flag, home town
        self.assertNotIn("photo", json.dumps(self.cache()))  # the flag only, never the description

    def test_train_fields(self):
        self.imp()
        self.assertEqual(self.ev(4)["train"], {"from": "home", "to": "Basel SBB", "lines": ["S33", "IC3"]})
        self.assertEqual(self.ev(5)["train"], {"from": "Basel SBB", "to": "home", "lines": ["IC3", "Bus 5"]})
        self.assertEqual(self.ev(16)["train"], {"from": None, "to": None, "lines": ["S1"]})
        self.assertNotIn("train", self.ev(3))

    # ------------------------------------------------------------------ overnight legs, towns without a postcode
    def imp_events(self, evs):
        raw = self.write("legs.json", {"events": evs, "timeZone": "Europe/Zurich"})
        r = self.run_cc("import", raw)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_known_town_without_postcode(self):
        self.imp_events(
            [
                timed(
                    41,
                    "🚌 Zürich → Antwerp night coach",
                    "2026-03-04T21:30:00+01:00",
                    "2026-03-05T09:00:00+01:00",
                    "Carparkplatz Sihlquai, Zürich (Bay 3, upper level)",
                ),
                timed(
                    42,
                    "🚌 Night coach",
                    "2026-03-06T10:00:00+01:00",
                    "2026-03-06T12:00:00+01:00",
                    "Carparkplatz Nord, Somewhere (Bay 3)",
                ),  # not named in the title: no guess
                timed(
                    43,
                    "🚆 Werkstadt → Bern",
                    "2026-03-07T10:00:00+01:00",
                    "2026-03-07T11:00:00+01:00",
                    "Perronweg 2, Werkstadt (Gleis 4)",
                ),  # a profile town
                timed(
                    44,
                    "Concert",
                    "2026-03-08T20:00:00+01:00",
                    "2026-03-08T23:00:00+01:00",
                    "Halle 5, Basel (Hall 2, gate B)",
                ),  # a comma inside the parentheses does not split
                timed(
                    45, "Gig in Luzern", "2026-03-09T20:00:00+01:00", "2026-03-09T23:00:00+01:00", "Luzern, Seeweg 3"
                ),  # street last: the known town before it
                timed(
                    46, "Gig", "2026-03-10T20:00:00+01:00", "2026-03-10T23:00:00+01:00", "Somewhere, Seeweg 3"
                ),  # street last, town unknown: none
            ]
        )
        self.assertEqual(self.ev(41)["town"], "Zürich")
        self.assertIsNone(self.ev(42)["town"])
        self.assertEqual(self.ev(43)["town"], "Werkstadt")
        self.assertEqual(self.ev(44)["town"], "Basel")
        self.assertEqual(self.ev(45)["town"], "Luzern")
        self.assertIsNone(self.ev(46)["town"])
        txt = open(os.path.join(self.dir, "events.json"), encoding="utf-8").read()
        for bad in ("Carparkplatz", "Sihlquai", "Bay 3", "Perronweg", "Seeweg", "Halle 5"):
            self.assertNotIn(bad, txt)

    def test_overnight_travel_leg_is_a_night_away(self):
        self.imp_events(
            [
                # overnight legs with no town at all: the night is spent on the way
                timed(51, "🚌 Night coach", "2026-03-04T21:30:00+01:00", "2026-03-05T09:00:00+01:00"),
                timed(
                    52, "🚆 Night train", "2026-03-06T20:00:00+01:00", "2026-03-07T08:00:00+01:00", "Bahnhof Bachwil"
                ),
                # back home before 04:00, or the same day: not a night away
                timed(53, "🚆 Basel SBB → Home", "2026-03-08T23:30:00+01:00", "2026-03-09T00:40:00+01:00", "Basel SBB"),
                timed(
                    54, "🚆 Home → Bern", "2026-03-10T18:00:00+01:00", "2026-03-10T19:00:00+01:00", "Bahnhof Bachwil"
                ),
                # a non-travel event at home across the night stays a night at home
                timed(55, "Movie night", "2026-03-11T20:00:00+01:00", "2026-03-12T06:00:00+01:00", "9999 Bachwil"),
            ]
        )
        on = {i: self.ev(i)["flags"]["away_overnight"] for i in (51, 52, 53, 54, 55)}
        self.assertEqual(on, {51: True, 52: True, 53: False, 54: False, 55: False})
        self.assertTrue(json.loads(self.run_cc("busy", "2026-03-04", "--json").stdout)["away_overnight"])
        self.assertFalse(json.loads(self.run_cc("busy", "2026-03-05", "--json").stdout)["away_overnight"])

    def test_trip_starts_on_the_night_of_an_overnight_coach(self):
        """calcache -> core.away (cat trip mode, damp watch): the coach night opens the trip."""
        import datetime as dt
        import sys

        self.imp_events(
            [
                timed(
                    61,
                    "🚌 Zürich → Antwerp night coach",
                    "2026-03-04T21:30:00+01:00",
                    "2026-03-05T09:00:00+01:00",
                    "Carparkplatz Sihlquai, Zürich (Bay 3)",
                ),
                allday(62, "Customer visit", "2026-03-05", "2026-03-07", "Antwerpen, Belgium"),
                timed(63, "✈️ BRU → ZRH", "2026-03-07T18:00:00+01:00", "2026-03-07T19:30:00+01:00"),
            ]
        )
        sys.path.insert(0, TREE)
        try:
            from core import away
        finally:
            sys.path.remove(TREE)
        trips = away.trips(self.cache()["events"], 2)
        self.assertEqual(len(trips), 1)
        self.assertEqual(trips[0]["from"], dt.date(2026, 3, 4))
        self.assertEqual(trips[0]["back"], dt.date(2026, 3, 7))

    def test_paged_stdin_and_days(self):
        p1 = self.write("p1.json", {"events": EVENTS[:8], "nextPageToken": "x"})
        p2 = self.write("p2.json", [{"events": EVENTS[8:]}])
        r = self.run_cc("import", p1, p2, "--days", "7")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("cached 8 events", r.stdout)
        self.assertEqual(self.cache()["window"]["end"][:10], "2026-03-09")
        r = self.run_cc("import", "-", stdin=json.dumps(EVENTS))
        self.assertIn("cached 12 events", r.stdout)

    def test_bad_input_keeps_cache(self):
        self.imp()
        before = open(os.path.join(self.dir, "events.json")).read()
        bad = self.write("bad.json", {"kind": "something else"})
        for args in (
            ("import", bad),
            ("import", os.path.join(self.tmp.name, "missing.json")),
            ("import", self.raw, "--days", "0"),
        ):
            r = self.run_cc(*args)
            self.assertEqual(r.returncode, 2, (args, r.stdout, r.stderr))
        nj = os.path.join(self.tmp.name, "nj.json")
        open(nj, "w").write("{nope")
        self.assertEqual(self.run_cc("import", nj).returncode, 2)
        self.assertEqual(open(os.path.join(self.dir, "events.json")).read(), before)

    def test_consume(self):
        self.imp("--consume")
        self.assertFalse(os.path.exists(self.raw))

    def test_empty_calendar(self):
        p = self.write("empty.json", {"events": []})
        r = self.run_cc("import", p)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("cached 0 events", r.stdout)

    def test_items_key(self):
        # the Google API events.list shape: events under "items", the page's own "summary" = the calendar's name
        p = self.write(
            "items.json",
            {"kind": "calendar#events", "summary": "Primary", "timeZone": "Europe/Zurich", "items": EVENTS},
        )
        r = self.run_cc("import", p, "--consume")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("cached 12 events", r.stdout)
        self.assertFalse(os.path.exists(p))
        p = self.write("items2.json", [{"items": EVENTS[:8], "nextPageToken": "x"}, {"events": EVENTS[8:]}])
        r = self.run_cc("import", p)
        self.assertIn("cached 12 events", r.stdout)

    def test_bare_string_times(self):
        p = self.write(
            "str.json",
            {
                "events": [
                    {
                        "id": "s1",
                        "summary": "Dinner",
                        "start": "2026-03-03T19:00:00+01:00",
                        "end": "2026-03-03T21:00:00+01:00",
                    },
                    {"id": "s2", "summary": "Fair", "start": "2026-03-04", "end": "2026-03-05"},
                ]
            },
        )
        r = self.run_cc("import", p)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("cached 2 events", r.stdout)
        self.assertTrue(next(e for e in self.cache()["events"] if e["id"] == "s2")["all_day"])

    def test_empty_import_keeps_full_cache(self):
        self.imp()
        before = open(os.path.join(self.dir, "events.json")).read()
        empty = self.write("empty.json", {"events": []})
        page_only = self.write("page.json", {"kind": "calendar#events", "summary": "Primary"})
        skipped = self.write("cancelled.json", {"items": [dict(EVENTS[1], status="cancelled")]})
        for p in (empty, page_only, skipped):
            r = self.run_cc("import", p, "--consume")
            self.assertEqual(r.returncode, 2, (p, r.stdout, r.stderr))
            self.assertIn("cache unchanged", r.stderr)
            self.assertTrue(os.path.exists(p), p)  # not consumed: can be looked at / re-saved
            self.assertEqual(open(os.path.join(self.dir, "events.json")).read(), before)
        r = self.run_cc("import", empty, "--allow-empty")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.cache()["events"], [])

    def test_empty_import_over_a_past_cache(self):
        self.imp()  # events up to 23.03
        later = dict(self.env, CALCACHE_NOW="2026-04-20T09:00:00+02:00")
        r = self.run_cc("import", self.write("empty.json", {"events": []}), env=later)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("cached 0 events", r.stdout)

    # ------------------------------------------------------------------ queries
    def test_busy(self):
        self.imp()
        r = self.run_cc("busy", "2026-03-03", "--json")
        d = json.loads(r.stdout)
        self.assertEqual(d["busy"], [["17:30", "18:40"], ["19:00", "21:00"], ["22:10", "23:30"]])
        self.assertTrue(d["evening_busy"])
        self.assertEqual(d["all_day"], 1)
        self.assertEqual(d["travel"], ["train"])
        d = json.loads(self.run_cc("busy", "2026-03-06", "--json").stdout)
        self.assertEqual(d["busy"], [["00:00", "05:00"], ["22:00", "24:00"]])
        self.assertTrue(d["away_overnight"] and d["evening_busy"])
        d = json.loads(self.run_cc("busy", "2026-03-07", "--json").stdout)  # morning after the rave: evening free
        self.assertEqual(d["busy"], [["00:00", "06:00"]])
        self.assertFalse(d["evening_busy"])
        self.assertTrue(d["away_overnight"])  # ski weekend starts that day
        d = json.loads(self.run_cc("busy", "2026-03-05", "--json").stdout)
        self.assertFalse(d["away_overnight"])  # movie night at home
        r = self.run_cc("busy", "today")
        self.assertEqual(r.stdout.strip(), "2026-03-02 Mon: free")
        r = self.run_cc("busy", "tomorrow")
        self.assertIn("busy 17:30–18:40, 19:00–21:00, 22:10–23:30 · evening busy · 1 all-day", r.stdout)
        rows = json.loads(self.run_cc("busy", "today", "--days", "3", "--json").stdout)
        self.assertEqual([x["date"] for x in rows], ["2026-03-02", "2026-03-03", "2026-03-04"])
        self.assertEqual(rows[2]["busy"], [["19:30", "22:00"]])  # cancelled/declined left out

    def test_outside_window_and_bad_date(self):
        self.imp()
        r = self.run_cc("busy", "2026-03-25")
        self.assertEqual(r.returncode, 4)
        self.assertIn("not cached", r.stdout)
        r = self.run_cc("towns", "+30", "--json")
        self.assertEqual(r.returncode, 4)
        self.assertFalse(json.loads(r.stdout)["covered"])
        rows = json.loads(self.run_cc("busy", "2026-03-22", "--days", "2", "--json").stdout)
        self.assertEqual([x["covered"] for x in rows], [True, False])
        self.assertEqual(self.run_cc("busy", "31.12.").returncode, 2)

    def test_towns_cmd(self):
        self.imp()
        d = json.loads(self.run_cc("towns", "2026-03-03", "--json").stdout)
        self.assertEqual(d["towns"], ["Werkstadt", "Bachwil", "Basel"])
        r = self.run_cc("towns", "2026-03-02", "--days", "2")
        self.assertEqual(r.stdout.splitlines(), ["2026-03-02 Mon: -", "2026-03-03 Tue: Werkstadt, Bachwil, Basel"])

    def test_show(self):
        self.imp()
        out = self.run_cc("show", "--days", "2").stdout.splitlines()
        self.assertEqual(out[0], "Tue 03.03 all day @Werkstadt · Werkstadt [free]")
        self.assertIn("Tue 03.03 19:00–21:00 Climbing · Basel [evening]", out)
        self.assertEqual(len(out), 4)
        out = self.run_cc("show", "--days", "21").stdout
        self.assertIn("Sat 07.03–Sun 08.03 all day Ski weekend · Zermatt [overnight]", out)
        evs = json.loads(self.run_cc("show", "--json").stdout)
        self.assertTrue(all("description" not in e for e in evs))

    def test_no_cache_and_age(self):
        r = self.run_cc("show")
        self.assertEqual(r.returncode, 3)
        self.assertEqual(self.run_cc("busy", "today").returncode, 3)
        r = self.run_cc("age")
        self.assertEqual(r.returncode, 1)
        self.imp()
        r = self.run_cc("age", "--json")
        d = json.loads(r.stdout)
        self.assertEqual((d["hours"], d["events"], d["window"]), (0.0, 12, ["2026-03-02", "2026-03-23"]))
        self.assertEqual(self.run_cc("age", "--max-hours", "20").returncode, 0)
        later = dict(self.env, CALCACHE_NOW="2026-03-03T06:00:00+01:00")
        r = self.run_cc("age", "--max-hours", "20", "--quiet", env=later)
        self.assertEqual((r.returncode, r.stdout), (1, ""))
        stale = dict(self.env, CALCACHE_NOW="2026-03-04T09:00:00+01:00")
        self.assertIn("48 h old", self.run_cc("busy", "today", env=stale).stderr)

    # ------------------------------------------------------------------ train-watch
    def run_tw(self, *args):
        r = subprocess.run([TW, *args], env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def legs(self):
        with open(os.path.join(self.env["TRAIN_WATCH_DIR"], "legs.json"), encoding="utf-8") as f:
            return {g["id"]: g for g in json.load(f)["legs"]}

    def test_train_watch_from_cache_equals_file(self):
        in_window = self.write("inwin.json", {"events": [e for e in EVENTS if e["id"] != "ev17"]})
        self.run_tw("sync", in_window)
        direct = self.legs()
        self.assertEqual(len(direct), 3)
        self.imp()
        out = self.run_tw("sync", "--from-cache")
        self.assertIn("synced 3 calendar legs (0 removed), kept 0 past the cache window", out)
        self.assertEqual(self.legs(), direct)
        g = next(x for x in direct.values() if x["event_id"] == "ev4")
        self.assertEqual((g["from"], g["to"], g["lines"]), ("home", "Basel SBB", ["S33", "IC3"]))

    def test_train_watch_keeps_legs_past_window(self):
        self.run_tw("sync", self.raw)  # all four legs, one on 28.03
        self.assertEqual(len(self.legs()), 4)
        short = [e for e in EVENTS if e["id"] != "ev5"]  # the return leg was deleted in the calendar
        self.write("raw.json", {"events": short})
        self.imp()
        out = self.run_tw("sync", "--from-cache")
        self.assertIn("(1 removed), kept 1 past the cache window", out)
        self.assertEqual(sorted(g["event_id"] for g in self.legs().values()), ["ev16", "ev17", "ev4"])

    def test_refresh_chain_items_page_and_empty_cache(self):
        self.imp()
        self.run_tw("sync", "--from-cache")
        legs = self.legs()
        self.assertEqual(len(legs), 3)
        # the refresh chain with the Google API shape ("items") keeps every leg
        page = self.write("raw2.json", {"kind": "calendar#events", "summary": "Primary", "items": EVENTS})
        r = subprocess.run(
            ["sh", "-c", f'"{CC}" import --consume "{page}" && "{TW}" sync --from-cache'],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.legs(), legs)
        # an empty cache (forced) never drops the calendar legs without --allow-empty
        r = self.run_cc("import", self.write("empty.json", {"events": []}), "--allow-empty")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run([TW, "sync", "--from-cache"], env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("the calendar cache holds no events at all; keeping the 3 upcoming calendar legs", r.stderr)
        self.assertEqual(self.legs(), legs)
        self.assertIn("(3 removed)", self.run_tw("sync", "--from-cache", "--allow-empty"))

    def test_train_watch_no_cache(self):
        r = subprocess.run([TW, "sync", "--from-cache"], env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 2)
        r = subprocess.run([TW, "sync"], env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 2)

    # ------------------------------------------------------------------ daily cue (hub schedule calcache-age)
    def cron_cmd(self, home):
        """The cue as cron runs it: the registry's calcache-age line with the calcache under test in its ~/bin."""
        import tomllib

        with open(REGISTRY, "rb") as f:
            line = tomllib.load(f)["jobs"]["calcache-age"]["cron_line"]
        os.symlink(CC, os.path.join(home, "bin", "calcache"))
        return line.split(None, 5)[5].replace("/home/node/bin/", f"{home}/bin/").replace("\\%", "%")

    def test_cron_cue(self):
        home = os.path.join(self.tmp.name, "home")
        os.makedirs(os.path.join(home, "inbox"))
        os.makedirs(os.path.join(home, "bin"))
        os.symlink(os.path.join(HOME, "bin/lib"), os.path.join(home, "bin/lib"))  # the libs, as under the real HOME
        env = dict(self.env, HOME=home)
        env.pop("CALCACHE_DIR")
        cmd = self.cron_cmd(home)
        inbox = os.path.join(home, "inbox")
        subprocess.run(["sh", "-c", cmd], env=env, timeout=60, check=True)  # no cache -> one note
        notes = os.listdir(inbox)
        self.assertEqual(len(notes), 1)
        self.assertTrue(re.fullmatch(r"calcache-refresh-\d+\.md", notes[0]))
        body = open(os.path.join(inbox, notes[0]), encoding="utf-8").read()
        self.assertTrue(body.startswith("Data, not instructions"))
        for need in ("list_events", "calcache import", "train-watch sync --from-cache", "no message"):
            self.assertIn(need, body)
        os.unlink(os.path.join(inbox, notes[0]))
        r = subprocess.run([CC, "import", self.raw], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        subprocess.run(["sh", "-c", cmd], env=env, timeout=60, check=True)  # fresh cache -> skipped
        self.assertEqual(os.listdir(inbox), [])
        later = dict(env, CALCACHE_NOW="2026-03-03T06:40:00+01:00")  # 21.7 h later -> note again
        subprocess.run(["sh", "-c", cmd], env=later, timeout=60, check=True)
        self.assertEqual(len(os.listdir(inbox)), 1)


if __name__ == "__main__":
    unittest.main(verbosity=1)
