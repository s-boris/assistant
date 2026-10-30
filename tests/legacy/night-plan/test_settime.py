"""Offline tests for `night-plan settime` (lib/night_settime.py). NIGHT_PLAN_BIN=/path/to/copy tests a copy (its lib/
next to it).

A loopback fake web server serves the event pages (and robots.txt, ETags, 404s); a fake night-plan binary prints a
fixed --json plan and records its arguments; calcache is a JSON file; rave-radar state, profile, inbox and state dir
live in a temp HOME. Proxies point at a closed port, so nothing can leave the box. Fictional artists and places only.
Covers: the parser (name-first / time-first, ranges, adjacent lines, next act's start as the end, dots with "Uhr",
dates and doors ignored, accents), add (rave-radar ref, --venue form, start from the calendar, refused hosts, robots
refusal), the found note (one only, 0600, train that fits, draft update_event), spotted note, failing note, due
times and host gap, expiry, pick (prints, writes nothing), list/rm.
"""

import datetime as dt
import glob
import http.server
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest

BIN = os.environ.get("NIGHT_PLAN_BIN") or os.path.expanduser("~/bin/night-plan")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(BIN)), "lib"))
# watch's fetch rules (in-process and in the runs): the watch beside the copy under test, else ~/bin's (a CI overlay
# stages night-plan alone)
for _w in (os.path.join(os.path.dirname(BIN), "watch"), os.path.expanduser("~/bin/watch")):
    if not os.environ.get("NIGHT_SETTIME_WATCH") and os.path.isfile(_w):
        os.environ["NIGHT_SETTIME_WATCH"] = _w

PAGES, HITS = {}, []
ROBOTS = {"txt": "User-agent: *\nDisallow: /private/\n"}

FAKE_PLAN = r"""#!/usr/bin/env python3
import json, os, sys
with open(os.environ['FAKE_PLAN_ARGS'], 'a') as f:
    f.write(json.dumps(sys.argv[1:]) + '\n')
def c(dep, arr, lines, flags=()):
    return {'dep': dep, 'arr': arr, 'changes': len(lines) - 1, 'flags': list(flags),
            'rides': [{'line': l} for l in lines]}
print(json.dumps({
    'walk_min': 3,
    'after_end': c('2026-11-15T03:55:00+01:00', '2026-11-15T06:55:00+01:00', ['Bus M1', 'IR', 'S5'], ['night bus']),
    'last_back': c('2026-11-15T00:41:00+01:00', '2026-11-15T04:16:00+01:00', ['Tram 4', 'IC2', 'SN1']),
    'first_morning': c('2026-11-15T02:14:00+01:00', '2026-11-15T06:13:00+01:00', ['Bus M2', 'IR', 'S5']),
    'calendar': [{'kind': 'return', 'summary': 'Testhalle -> Home', 'startTime': '2026-11-15T03:55:00',
                  'endTime': '2026-11-15T06:55:00', 'location': 'Testhalle', 'description': 'Bus M1 > IR > S5',
                  'timeZone': 'Europe/Zurich'}]}))
"""


class Fake(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        HITS.append(self.path)
        if self.path == "/robots.txt":
            body, code = ROBOTS["txt"].encode(), 200
        elif self.path in PAGES:
            body, code = PAGES[self.path].encode(), 200
            etag = '"%x"' % (hash(PAGES[self.path]) & 0xFFFFFFFF)
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.end_headers()
                return
        else:
            body, code, etag = b"not found", 404, None
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        if code == 200 and self.path != "/robots.txt":
            self.send_header("ETag", etag)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def page(title, body):
    return f"<html><head><title>{title}</title></head><body>{body}</body></html>"


LISTING = page(
    "Nova Kestrel | Tickets",
    "<h1>Nova Kestrel</h1><p>Nova Kestrel, 14.11.26, 22:00</p>"
    "<p>Sa, 14.11 / 22:00 | TESTHALLE: | Nova Kestrel | Ada Vale</p>",
)
TIMETABLE = page(
    "Nova Kestrel | Tickets",
    "<h2>Timetable</h2><div>Ada Vale 22:00 - 01:00</div>"
    "<div>Nova Kestrel 01:00 - 03:00</div><div>Rio Tam 03:00 - 06:00</div>",
)


class Settime(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        PAGES.clear()
        HITS.clear()
        ROBOTS["txt"] = "User-agent: *\nDisallow: /private/\n"
        self.tmp = tempfile.TemporaryDirectory()
        d = self.d = self.tmp.name
        for sub in ("home", "inbox", "state", "rr", "watch"):
            os.makedirs(os.path.join(d, sub))
        with open(os.path.join(d, "profile.json"), "w") as f:
            json.dump({"locale": {"tz": "Europe/Zurich"}}, f)
        with open(os.path.join(d, "rr", "state.json"), "w") as f:
            json.dump(
                {
                    "events": {
                        "abc1234567": {
                            "id": "abc1234567",
                            "kind": "show",
                            "artist": "Nova Kestrel",
                            "title": "Nova Kestrel",
                            "date": "2026-11-14",
                            "venue": "Testhalle",
                            "city": "Testtown",
                            "url": "http://example.invalid/",
                        }
                    }
                },
                f,
            )
        self.cal = os.path.join(d, "cal.json")
        self.write_cal(leg_start="2026-11-15T05:14:00+01:00")
        self.plan = os.path.join(d, "fake-plan")
        with open(self.plan, "w") as f:
            f.write(FAKE_PLAN)
        os.chmod(self.plan, 0o755)
        self.args_f = os.path.join(d, "plan-args.jsonl")
        self.env = dict(
            os.environ,
            HOME=os.path.join(d, "home"),
            ASSISTANT_PROFILE=os.path.join(d, "profile.json"),
            NIGHT_SETTIME_STATE_DIR=os.path.join(d, "state"),
            NIGHT_SETTIME_INBOX=os.path.join(d, "inbox"),
            NIGHT_SETTIME_PLAN=self.plan,
            NIGHT_SETTIME_CALCACHE=self.cal,
            NIGHT_SETTIME_HOST_GAP="0",
            NIGHT_SETTIME_NOW="2026-11-10 12:00",
            RAVE_RADAR_STATE_DIR=os.path.join(d, "rr"),
            WATCH_STATE_DIR=os.path.join(d, "watch"),
            FAKE_PLAN_ARGS=self.args_f,
            http_proxy="http://127.0.0.1:9",
            https_proxy="http://127.0.0.1:9",
            HTTP_PROXY="http://127.0.0.1:9",
            HTTPS_PROXY="http://127.0.0.1:9",
            no_proxy="127.0.0.1",
            NO_PROXY="127.0.0.1",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def write_cal(self, leg_start=None, title="Nova Kestrl"):
        evs = [
            {
                "id": "ev1",
                "title": title,
                "start": "2026-11-14T22:00:00+01:00",
                "end": "2026-11-15T06:00:00+01:00",
                "all_day": False,
                "town": "Testtown",
                "free": True,
                "flags": {"travel": None},
            }
        ]
        if leg_start:
            evs.append(
                {
                    "id": "leg9",
                    "title": "Testtown -> Home",
                    "start": leg_start,
                    "end": "2026-11-15T07:30:00+01:00",
                    "all_day": False,
                    "town": "Testtown",
                    "flags": {"travel": "train"},
                    "train": {"from": "Testhalle", "to": "home", "lines": ["IC1"]},
                }
            )
        with open(self.cal, "w") as f:
            json.dump(evs, f)

    def run_st(self, *args, now=None, code=0):
        env = dict(self.env)
        if now:
            env["NIGHT_SETTIME_NOW"] = now
        p = subprocess.run([BIN, "settime", *args], capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(p.returncode, code, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        return p.stdout + p.stderr

    def notes(self, pat="*-settime-*.md"):
        return sorted(glob.glob(os.path.join(self.d, "inbox", pat)))

    def state(self):
        with open(os.path.join(self.d, "state", "settime.json")) as f:
            return json.load(f)

    def wid(self):
        return next(iter(self.state()["watches"]))

    # ------------------------------------------------------------ parser

    def test_parser_layouts(self):
        import night_settime as n

        start = dt.datetime(2026, 11, 14, 22, 0, tzinfo=n.TZ)
        cases = {
            "<div>Ada Vale 22:00 - 01:00</div><div>Nova Kestrel 01:00 - 03:00</div><div>Rio Tam 03:00 - 06:00</div>": (
                "01:00",
                "03:00",
            ),
            "<ul><li>23:00</li><li>Ada Vale</li><li>01:00</li><li>Nova Kestrel</li><li>03:00</li><li>Rio Tam</li></ul>": (  # noqa: E501
                "01:00",
                "03:00",
            ),
            "<p>Ada Vale</p><p>23:00</p><p>Nova Kestrel</p><p>01:30</p><p>Rio Tam</p><p>03:30</p>": ("01:30", "03:30"),
            "<p>TIMETABLE: 22:00-01:00 Ada Vale | 01:00-03:00 NOVA KESTREL | 03:00-06:00 Rio Tam</p>": (
                "01:00",
                "03:00",
            ),
            "<p>Ada Vale | 22:00-01:00 | Nova Kestrel | 01:00-03:00 | Rio Tam | 03:00-06:00</p>": ("01:00", "03:00"),
            "<p>23:00 Ada Vale</p><p>01:00 Nova Kestrel</p><p>03:00 Rio Tam</p>": ("01:00", "03:00"),
            "<p>Zeitplan</p><p>Nova Kestrel: 01.00 - 03.00 Uhr</p>": ("01:00", "03:00"),
            "<p>Nová Kestrel — 02:00–04:00</p>": ("02:00", "04:00"),
            "<p>KESTREL (1h30)</p>": ("01:30", None),
        }
        for html, (s, e) in cases.items():
            c, _, _ = n.parse(html, "Nova Kestrel", start)
            self.assertTrue(c, html)
            self.assertEqual(c[0]["start"][11:16], s, html)
            self.assertEqual(c[0].get("end", "")[11:16] or None, e, html)
            if e is None:
                self.assertTrue(c[0]["assumed_end"])
        for html in (
            "<p>Sa, 14.11 / 22:00 | TESTHALLE: | Nova Kestrel</p><p>Nova Kestrel, 14.11.26, 22:00</p>",
            "<p>Nova Kestrel 22:00</p>",
            "<p>Nova Kestrel 19.10 Testtown</p>",
            "<p>Nova Kestrel</p>",
        ):
            c, _, _ = n.parse(html, "Nova Kestrel", start)
            self.assertEqual(c, [], html)
        c, _, _ = n.parse("<p>Nova Kestrel 01:00 - 03:00</p>", "Nova Kestrel", start)
        self.assertTrue(c[0]["start"].startswith("2026-11-15T01:00"))  # after midnight = next day

    def test_parser_keywords(self):
        import night_settime as n

        start = dt.datetime(2026, 11, 14, 22, 0, tzinfo=n.TZ)
        _, kw, _ = n.parse(page("Nova Kestrel", '<img src="/m/timetable-final.jpg" alt="">'), "Nova Kestrel", start)
        self.assertTrue(any(k.startswith("img:") for k in kw))
        _, kw, _ = n.parse(
            page("Venue calendar", '<img src="/m/timetable.jpg"><p>Nova Kestrel</p>'), "Nova Kestrel", start
        )
        self.assertFalse(any(k.startswith("img:") for k in kw))  # img only on the artist's own page
        _, kw, _ = n.parse("<p>Nova Kestrel</p><p>Timetable soon</p>", "Nova Kestrel", start)
        self.assertTrue(kw)

    # ------------------------------------------------------------ add / check / notes

    def test_found_note_once(self):
        PAGES["/event"] = LISTING
        out = self.run_st("add", "abc1", f"{self.base}/event", f"{self.base}/promoter")
        self.assertIn("start 22:00 (from the calendar)", out)
        self.assertEqual(self.notes(), [])
        w = self.state()["watches"][self.wid()]
        self.assertEqual(w["status"], "watching")
        import night_settime as n

        self.assertEqual(w["until"], dt.datetime(2026, 11, 15, 2, 0, tzinfo=n.TZ).timestamp())  # start + 4 h
        PAGES["/event"] = TIMETABLE
        self.run_st("check", "--cron", now="2026-11-10 13:00")  # not due yet (every 3 h)
        self.assertEqual(self.notes(), [])
        self.run_st("check", "--cron", now="2026-11-10 15:01")
        notes = self.notes()
        self.assertEqual(len(notes), 1)
        self.assertRegex(os.path.basename(notes[0]), r"^kestrel-settime-\d+\.md$")
        self.assertEqual(stat.S_IMODE(os.stat(notes[0]).st_mode), 0o600)
        text = open(notes[0]).read()
        self.assertIn("Set: 01:00–03:00", text)
        self.assertIn("Train that fits", text)
        self.assertIn("03:55 → 06:55", text)
        self.assertIn("update_event leg9", text)
        self.assertIn('"startTime": "2026-11-15T03:55:00"', text)
        self.assertIn("Last back 00:41 leaves before the set ends", text)
        args = json.loads(open(self.args_f).read().splitlines()[-1])
        self.assertEqual(args[:1], ["abc1234567"])
        for a, b in (("--start", "22:00"), ("--end", "03:00"), ("--exit-min", "20")):
            self.assertEqual(args[args.index(a) + 1], b)
        self.assertEqual(self.state()["watches"][self.wid()]["status"], "found")
        n = len(HITS)
        self.run_st("check", "--cron", now="2026-11-11 15:01")
        self.assertEqual(len(self.notes()), 1)
        self.assertEqual(len(HITS), n)  # found = no more fetching
        self.assertIn("found", self.run_st("list"))

    def test_calendar_leg_already_right(self):
        self.write_cal(leg_start="2026-11-15T03:55:00+01:00")
        PAGES["/event"] = TIMETABLE
        self.run_st("add", "abc1234567", f"{self.base}/event", "--start", "22:00")
        text = open(self.notes()[0]).read()
        self.assertIn("Draft calendar change: none", text)

    def test_no_leg_create(self):
        self.write_cal(leg_start=None)
        PAGES["/event"] = TIMETABLE
        self.run_st("add", "abc1234567", f"{self.base}/event")
        text = open(self.notes()[0]).read()
        self.assertIn("no 🚆 leg home", text)
        self.assertIn("create_event", text)

    def test_venue_form_and_pick(self):
        PAGES["/v"] = LISTING
        self.run_st(
            "add",
            "--venue",
            "Testhalle, Testtown",
            "--date",
            "2026-11-14",
            "--artist",
            "Nova Kestrel",
            f"{self.base}/v",
            "--set-min",
            "90",
        )
        wid = self.wid()
        out = self.run_st("pick", wid, "01:30")
        self.assertIn("Set: 01:30–03:00 (end assumed: start + 90 min)", out)
        self.assertEqual(self.notes(), [])  # pick never writes
        args = json.loads(open(self.args_f).read().splitlines()[-1])
        self.assertEqual(args[:2], ["--venue", "Testhalle, Testtown"])
        self.assertEqual(self.state()["watches"][wid]["status"], "watching")
        self.run_st("pick", wid, "soon", code=2)

    def test_refused_hosts_and_robots(self):
        out = self.run_st("add", "abc1234567", "https://ra.co/events/1", code=2)
        self.assertIn("refused", out)
        out = self.run_st("add", "abc1234567", "https://www.instagram.com/x", code=2)
        PAGES["/private/p"] = TIMETABLE
        out = self.run_st("add", "abc1234567", f"{self.base}/private/p")
        self.assertIn("refused", out)
        self.assertNotIn("/private/p", HITS)
        self.assertEqual(self.notes(), [])
        self.assertEqual(self.state()["watches"][self.wid()]["sources"][0]["state"], "refused")

    def test_spotted_then_found(self):
        PAGES["/e"] = LISTING
        self.run_st("add", "abc1234567", f"{self.base}/e")
        PAGES["/e"] = page("Nova Kestrel | Tickets", '<p>Nova Kestrel</p><img src="/m/timetable.jpg">')
        self.run_st("check", "--cron", now="2026-11-10 15:01")
        notes = self.notes()
        self.assertEqual(len(notes), 1)
        self.assertIn("Timetable spotted", open(notes[0]).read())
        self.run_st("check", "--cron", now="2026-11-10 18:02")
        self.assertEqual(len(self.notes()), 1)  # spotted once
        PAGES["/e"] = TIMETABLE
        self.run_st("check", "--cron", now="2026-11-10 21:03")
        self.assertEqual(len(self.notes()), 2)

    def test_failing_note_and_expiry(self):
        self.run_st("add", "abc1234567", f"{self.base}/gone", "--until", "2026-11-12 00:00")
        t = dt.datetime(2026, 11, 10, 12, 0)
        for _i in range(1, 7):
            t += dt.timedelta(hours=3, minutes=1)
            self.run_st("check", "--cron", now=t.strftime("%Y-%m-%d %H:%M"))
        failed = self.notes("*-settime-failed-*.md")
        self.assertEqual(len(failed), 1)
        self.assertIn("/gone", open(failed[0]).read())
        n = len(HITS)
        self.run_st("check", "--cron", now="2026-11-12 01:00")
        self.assertEqual(len(HITS), n)
        self.assertEqual(self.state()["watches"][self.wid()]["status"], "expired")
        self.assertEqual(len(self.notes("*-settime-failed-*.md")), 1)

    def test_list_rm_usage(self):
        self.assertIn("no set-time watches", self.run_st("list"))
        self.run_st("add", "zzzz", f"{self.base}/e", code=2)
        self.run_st("add", "abc1234567", code=2)
        PAGES["/e"] = LISTING
        self.run_st("add", "abc1234567", f"{self.base}/e")
        wid = self.wid()
        self.assertIn(wid, self.run_st("list"))
        self.run_st("rm", wid)
        self.assertEqual(self.state()["watches"], {})
        self.run_st("rm", wid, code=2)

    def test_dry_run_saves_nothing(self):
        PAGES["/e"] = LISTING
        self.run_st("add", "abc1234567", f"{self.base}/e")
        before = self.state()
        PAGES["/e"] = TIMETABLE
        out = self.run_st("check", self.wid(), "--dry-run")
        self.assertIn("dry run", out)
        self.assertEqual(self.notes(), [])
        self.assertEqual(self.state(), before)


if __name__ == "__main__":
    unittest.main(verbosity=1)
