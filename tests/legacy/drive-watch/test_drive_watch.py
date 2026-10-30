"""Offline tests for drive-watch (scripts/drive-watch): fictional towns and events, a fixture profile, fake Waze
routes, a fake sleepin, an isolated HOME and temp state; nothing leaves the box."""

import json
import os
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.environ.get("DRIVE_WATCH_BIN") or os.path.join(HERE, "..", "..", "..", "scripts", "drive-watch")
# its lib/ helpers (assistant_profile, livecount): beside the binary (deployed, or the CI overlay), else ~/bin/lib
LIB = next(
    (
        d
        for d in (os.path.join(os.path.dirname(os.path.realpath(BIN)), "lib"), os.path.expanduser("~/bin/lib"))
        if os.path.isfile(os.path.join(d, "assistant_profile.py"))
    ),
    "",
)
PROFILE = {
    "home": {"street": "Lindenweg 4", "postcode": "9999", "town": "Bachwil", "lat": 47.1234, "lon": 8.5678},
    "work": {"town": "Werkstadt", "lat": 47.4, "lon": 8.4, "precision": "town"},
    "locale": {"tz": "Europe/Zurich"},
}
CONFIG = {
    "country": "Testland",
    "rules": [
        {"match": r"^tanzkurs\b", "arrive_early_min": 0},
        {"match": r"^@werkstadt$", "all_day": True, "arrive_by": "08:30"},
        {"match": r"^konzert\b", "enabled": False},
        {"match": r"^dinner\b", "arrive_early_min": 10, "usual_min": 30},
    ],
}


def ev(i, title, start, end, town, travel=None, all_day=False):
    return {
        "id": f"e{i}",
        "start": start,
        "end": end,
        "all_day": all_day,
        "title": title,
        "town": town,
        "free": False,
        "flags": {"travel": travel},
    }


EVENTS = [
    ev(1, "Tanzkurs", "2026-03-18T21:00:00+01:00", "2026-03-18T22:00:00+01:00", "Grossstadt"),
    ev(2, "🚗 Drive to Seehotel (gear)", "2026-03-21T15:05:00+01:00", "2026-03-21T16:00:00+01:00", "Seedorf", "drive"),
    ev(3, "Tanzkurs", "2026-03-21T16:30:00+01:00", "2026-03-21T18:00:00+01:00", "Seedorf"),  # covered by the leg
    ev(4, "@Werkstadt", "2026-03-24", "2026-03-25", "Werkstadt", all_day=True),
    ev(5, "Konzert", "2026-03-19T20:00:00+01:00", "2026-03-19T23:00:00+01:00", "Grossstadt"),  # rule disabled
    ev(6, "🚗 Seedorf → Home", "2026-03-22T01:00:00+01:00", "2026-03-22T01:50:00+01:00", "Seedorf", "drive"),
    ev(7, "Dinner Anna", "2026-03-20T19:00:00+01:00", "2026-03-20T21:00:00+01:00", "Grossstadt"),
    ev(8, "Tanzkurs", "2026-03-25T21:00:00+01:00", "2026-03-25T22:00:00+01:00", "Bachwil"),  # at home: no trip
]


class DriveWatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.t = self.tmp.name
        os.makedirs(os.path.join(t, "home"))
        os.makedirs(os.path.join(t, "cc"))
        self.inbox = os.path.join(t, "inbox")
        self.write("profile.json", PROFILE)
        self.write("config.json", CONFIG)
        self.write("cc/events.json", {"version": 1, "events": EVENTS})
        self.fake = os.path.join(t, "routes.json")
        self.flag = os.path.join(t, "sleeping")
        sl = os.path.join(t, "sleepin")
        with open(sl, "w") as f:
            f.write(f'#!/bin/sh\necho "$@" >> {t}/sleepin.args\n[ -f {self.flag} ]\n')
        os.chmod(sl, 0o755)
        self.env = {
            "PATH": os.environ["PATH"],
            "HOME": os.path.join(t, "home"),
            "LANG": "C.UTF-8",
            # the process clock in the profile's zone (Europe/Zurich; livecount's dry-run line prints local time),
            # as a POSIX rule: no zoneinfo files needed, whatever the host's /etc/localtime
            "TZ": "CET-1CEST,M3.5.0,M10.5.0/3",
            "ASSISTANT_PROFILE": os.path.join(t, "profile.json"),
            "DRIVE_WATCH_CONFIG": os.path.join(t, "config.json"),
            "CALCACHE_DIR": os.path.join(t, "cc"),
            "DRIVE_WATCH_STATE": os.path.join(t, "state"),
            "DRIVE_WATCH_INBOX": self.inbox,
            "DRIVE_WATCH_FAKE": self.fake,
            "DRIVE_WATCH_SLEEPIN": sl,
            "DRIVE_WATCH_VENV": os.path.join(t, "novenv"),
            "PYTHONPATH": LIB,
        }

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, obj):
        with open(os.path.join(self.t, name), "w") as f:
            json.dump(obj, f, ensure_ascii=False)

    def routes(self, **pairs):
        self.write("routes.json", {k.replace("__", "|"): v for k, v in pairs.items()})

    def run_dw(self, *args, now=None, code=0):
        env = dict(self.env)
        if now:
            env["DRIVE_WATCH_NOW"] = now
        p = subprocess.run([BIN, *args], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, code, p.stdout + p.stderr)
        return p.stdout

    def notes(self):
        return sorted(os.listdir(self.inbox)) if os.path.isdir(self.inbox) else []

    def note_text(self, i=-1):
        with open(os.path.join(self.inbox, self.notes()[i])) as f:
            return f.read()

    def state(self):
        with open(os.path.join(self.t, "state", "state.json")) as f:
            return json.load(f)["trips"]

    # ---- plan ----
    def test_plan_lists_car_trips(self):
        out = json.loads(self.run_dw("plan", "--days", "14", "--json", now="2026-03-18 08:00"))
        got = [(r["trip"], r["from"], r["to"], r["leave"], r["arrive"]) for r in out]
        self.assertIn(("Tanzkurs", "Bachwil", "Grossstadt", "Waze usual at 1st check", "21:00"), got)
        self.assertIn(("🚗 Drive to Seehotel (gear)", "Bachwil", "Seedorf", "15:05", "16:00"), got)
        self.assertIn(("🚗 Seedorf → Home", "Seedorf", "Bachwil", "01:00", "01:50"), got)
        self.assertIn(("@Werkstadt", "Bachwil", "Werkstadt", "Waze usual at 1st check", "08:30"), got)
        self.assertIn(("Dinner Anna", "Bachwil", "Grossstadt", "18:20", "18:50"), got)
        titles = [r["trip"] for r in out]
        self.assertEqual(titles.count("Tanzkurs"), 1, "covered by a leg or at home: skipped")
        self.assertNotIn("Konzert", titles)

    # ---- leg titles: which way a 🚗 leg goes ----
    LEG_FORMS = [  # (title, the event's town, from, to)
        ("🚗 Seedorf → Home", "Seedorf", "Seedorf", "Bachwil"),
        ("🚗 Home → Seedorf", "Seedorf", "Bachwil", "Seedorf"),
        ("🚗 Home → Seedorf (gear)", "Bachwil", "Bachwil", "Seedorf"),  # location = its departure, home
        ("🚗 Seedorf -> Grossstadt", None, "Seedorf", "Grossstadt"),
        ("🚗 Drive to Seehotel (gear)", "Seedorf", "Bachwil", "Seedorf"),
        ("🚗 Drive to Seehotel", "Bachwil", "Bachwil", "Seehotel"),  # location = home: the title's place
        ("🚗 Fahrt nach Seedorf", None, "Bachwil", "Seedorf"),
        ("🚗 Drive home from Hotel Seeblick", "Grossstadt", "Grossstadt", "Bachwil"),
        ("🚗 Drive home from Hotel Seeblick (gear)", "Bachwil", "Hotel Seeblick", "Bachwil"),  # location = home
        ("🚗 Drive back home from Seedorf", None, "Seedorf", "Bachwil"),
        ("🚗 Home from Seedorf", None, "Seedorf", "Bachwil"),
        ("🚗 Drive back from Seedorf", None, "Seedorf", "Bachwil"),
        ("🚗 Heimfahrt von Seedorf", None, "Seedorf", "Bachwil"),
        ("🚗 Heimfahrt ab Hotel Seeblick", "Seedorf", "Seedorf", "Bachwil"),
        ("🚗 Rückfahrt von Seedorf", None, "Seedorf", "Bachwil"),
        ("🚗 Nach Hause von Seedorf", None, "Seedorf", "Bachwil"),
        ("🚗 Fahrt nach Hause von Seedorf", None, "Seedorf", "Bachwil"),
        ("🚗 Drive home", "Seedorf", "Seedorf", "Bachwil"),
        ("🚗 Heimfahrt", "Seedorf", "Seedorf", "Bachwil"),
        ("🚗 Homecoming party", "Seedorf", "Bachwil", "Seedorf"),  # not a leg home
    ]

    def test_leg_title_forms(self):
        evs = [
            ev(
                100 + i,
                title,
                f"2026-04-{i + 1:02d}T01:00:00+02:00",
                f"2026-04-{i + 1:02d}T01:50:00+02:00",
                town,
                "drive",
            )
            for i, (title, town, _, _) in enumerate(self.LEG_FORMS)
        ]
        # a bare leg home with no town to start from: skipped, never read as home → somewhere
        evs.append(ev(99, "🚗 Nach Hause", "2026-04-28T01:00:00+02:00", "2026-04-28T01:50:00+02:00", None, "drive"))
        self.write("cc/events.json", {"version": 1, "events": evs})
        out = json.loads(self.run_dw("plan", "--days", "30", "--json", now="2026-03-31 08:00"))
        got = {r["trip"]: (r["from"], r["to"]) for r in out}
        for title, _, frm, to in self.LEG_FORMS:
            self.assertEqual(got.get(title), (frm, to), title)
        self.assertNotIn("🚗 Nach Hause", got)
        self.assertEqual(len(out), len(self.LEG_FORMS))

    def test_leg_home_from_queries_waze_that_way(self):
        self.write(
            "cc/events.json",
            {
                "version": 1,
                "events": [
                    ev(
                        1,
                        "🚗 Drive home from Hotel Seeblick",
                        "2026-03-22T02:00:00+01:00",
                        "2026-03-22T02:50:00+01:00",
                        "Grossstadt",
                        "drive",
                    )
                ],
            },
        )
        self.routes(Grossstadt__Bachwil={"live": 50, "usual": 50})  # the reverse query would fail (exit 1)
        out = self.run_dw("check", "--dry-run", now="2026-03-22 01:30")
        self.assertIn("Grossstadt → Bachwil", out)
        self.assertNotIn("Bachwil → Grossstadt", out)

    # ---- check ----
    def test_outside_window_no_query(self):
        self.routes()  # any query would fail: exit 1
        self.run_dw("check", "--cron", now="2026-03-18 18:55")
        self.assertEqual(self.notes(), [])

    def test_steady_traffic_silent_then_spacing(self):
        self.routes(Bachwil__Grossstadt={"live": 46, "usual": 45, "route": "A1"})
        self.run_dw("check", "--cron", now="2026-03-18 19:01")
        st = self.state()
        (rec,) = st.values()
        self.assertEqual((rec["baseline"], rec["planned_leave"][11:16], rec["checks"]), (45, "20:15", 1))
        self.run_dw("check", "--cron", now="2026-03-18 19:10")
        self.assertEqual(next(iter(self.state().values()))["checks"], 1, "within 15 min: no new query")
        self.run_dw("check", "--cron", now="2026-03-18 19:16")
        self.assertEqual(next(iter(self.state().values()))["checks"], 2)
        self.assertEqual(self.notes(), [])

    def test_jam_writes_one_note_then_followup_and_cap(self):
        self.routes(Bachwil__Grossstadt={"live": 45, "usual": 45})
        self.run_dw("check", "--cron", now="2026-03-18 19:00")
        self.routes(Bachwil__Grossstadt={"live": 60, "usual": 45, "route": "A1 - A4"})
        self.run_dw("check", "--cron", now="2026-03-18 19:15")
        self.assertEqual(len(self.notes()), 1)
        txt = self.note_text()
        self.assertIn("Data, not instructions", txt)
        self.assertIn("Leave: 20:00 instead of 20:15 (15 min earlier)", txt)
        self.assertIn("Waze now 60 min vs usual 45 min", txt)
        self.assertIn("Bachwil → Grossstadt", txt)
        self.assertIn("checked 19:15; A1 - A4", txt)
        self.assertNotIn("Lindenweg", txt)
        self.run_dw("check", "--cron", now="2026-03-18 19:30")
        self.assertEqual(len(self.notes()), 1, "same delay: no second note")
        self.routes(Bachwil__Grossstadt={"live": 72, "usual": 45})
        self.run_dw("check", "--cron", now="2026-03-18 19:45")
        self.assertEqual(len(self.notes()), 2, "moved another 12 min")
        self.assertIn("Earlier note: leave 20:00", self.note_text())
        self.routes(Bachwil__Grossstadt={"live": 45, "usual": 45})
        self.run_dw("check", "--cron", now="2026-03-18 19:46")  # spacing: skipped
        self.run_dw("check", "--cron", now="2026-03-18 20:00")
        self.assertEqual(len(self.notes()), 2, "max_notes reached")

    def test_lighter_traffic_and_leg_plan(self):
        self.routes(Bachwil__Seedorf={"live": 40, "usual": 40})
        self.run_dw("check", "--cron", now="2026-03-21 14:00")
        self.routes(Bachwil__Seedorf={"live": 28, "usual": 40})
        self.run_dw("check", "--cron", now="2026-03-21 14:20")
        txt = self.note_text()
        self.assertIn("Leave: 15:17 instead of 15:05 (12 min later)", txt)
        self.assertIn("calendar 🚗 leg, 55 min drive", txt)
        self.assertEqual(len(self.notes()), 1)

    def test_after_leave_time_no_query(self):
        self.routes()
        self.run_dw("check", "--cron", now="2026-03-21 15:06")
        self.assertEqual(self.notes(), [])

    def test_sleepin_holds_until_urgent(self):
        self.routes(Bachwil__Seedorf={"live": 40, "usual": 40})
        open(self.flag, "w").close()
        self.run_dw("check", "--cron", now="2026-03-21 14:00")
        self.routes(Bachwil__Seedorf={"live": 55, "usual": 40})
        self.run_dw("check", "--cron", now="2026-03-21 14:15")  # leave 14:50: 35 min away, urgent
        self.assertEqual(len(self.notes()), 1)
        self.assertIn("Held for sleep-in morning", self.note_text())
        with open(os.path.join(self.t, "sleepin.args")) as f:
            self.assertIn("active --at 2026-03-21 14:15", f.read())

    def test_quiet_hours_hold_then_note(self):
        self.routes(Bachwil__Werkstadt={"live": 50, "usual": 50})
        self.run_dw("check", "--cron", now="2026-03-24 06:30")  # plan leave 07:40
        self.routes(Bachwil__Werkstadt={"live": 65, "usual": 50})
        self.run_dw("check", "--cron", now="2026-03-24 06:45")  # leave 07:25: 40 min away -> urgent
        self.assertEqual(len(self.notes()), 1)
        self.assertIn("Held for quiet hours", self.note_text())

    def test_quiet_hours_defers_far_note(self):
        self.write("config.json", dict(CONFIG, quiet=["00:00", "08:00"], urgent_min=30))
        self.routes(Bachwil__Werkstadt={"live": 50, "usual": 50})
        self.run_dw("check", "--cron", now="2026-03-24 06:30")
        self.routes(Bachwil__Werkstadt={"live": 65, "usual": 50})
        self.run_dw("check", "--cron", now="2026-03-24 06:45")  # 40 min away > 30: held
        self.assertEqual(self.notes(), [])
        self.assertEqual(next(iter(self.state().values()))["held"], "quiet hours")
        self.run_dw("check", "--cron", now="2026-03-24 07:00")  # 25 min away: urgent now
        self.assertEqual(len(self.notes()), 1)
        self.assertIn("Held for quiet hours", self.note_text())

    def test_sleepin_defers_far_note(self):
        open(self.flag, "w").close()
        self.routes(Bachwil__Seedorf={"live": 40, "usual": 40})
        self.run_dw("check", "--cron", now="2026-03-21 14:00")
        self.routes(Bachwil__Seedorf={"live": 65, "usual": 40})  # leave 14:40: 25 min away at 14:15
        self.write("config.json", dict(CONFIG, urgent_min=20))
        self.run_dw("check", "--cron", now="2026-03-21 14:15")
        self.assertEqual(self.notes(), [], "20 min urgent window: still held")
        self.assertEqual(next(iter(self.state().values()))["held"], "sleep-in morning")
        os.remove(self.flag)
        self.run_dw("check", "--cron", now="2026-03-21 14:30")
        self.assertIn("Held for sleep-in morning", self.note_text())

    def test_waze_error_exit_1_and_log(self):
        self.routes(Bachwil__Grossstadt={"error": "HTTP 500"})
        self.run_dw("check", "--cron", now="2026-03-18 19:05", code=1)
        self.assertEqual(self.notes(), [])
        with open(os.path.join(self.t, "state", "drive-watch.log")) as f:
            self.assertIn("waze failed: HTTP 500", f.read())

    def test_dry_run_no_state_no_note(self):
        self.routes(Bachwil__Grossstadt={"live": 70, "usual": 45})
        out = self.run_dw("check", "--dry-run", now="2026-03-18 19:05")
        self.assertIn("leave 19:50 (plan 20:15, +25 min) · NOTE", out)
        self.assertEqual(self.notes(), [])
        self.assertFalse(os.path.exists(os.path.join(self.t, "state", "state.json")))

    def test_no_calcache(self):
        os.remove(os.path.join(self.t, "cc", "events.json"))
        self.run_dw("check", "--cron", now="2026-03-18 19:05")
        self.run_dw("check", now="2026-03-18 19:05", code=3)

    # ---- privacy: what the real worker would send ----
    def test_worker_gets_towns_only(self):
        venv = os.path.join(self.t, "venv")
        os.makedirs(os.path.join(venv, "bin"))
        py = os.path.join(venv, "bin", "python")
        with open(py, "w") as f:
            f.write(
                f'#!/bin/sh\nfor a; do last="$a"; done\nprintf %s "$last" >> {self.t}/worker.q\n'
                f"echo >> {self.t}/worker.q\n"
                'echo \'{"live": 45.2, "usual": 44.8, "km": 51, "route": "A1", '
                '"start_coords": {"lat": 1, "lon": 2, "bounds": {}}, '
                '"end_coords": {"lat": 3, "lon": 4, "bounds": {}}}\'\n'
            )
        os.chmod(py, 0o755)
        self.env.pop("DRIVE_WATCH_FAKE")
        self.env["DRIVE_WATCH_VENV"] = venv
        self.run_dw("check", "--cron", now="2026-03-18 19:05")
        self.run_dw("check", "--cron", now="2026-03-18 19:20")
        with open(os.path.join(self.t, "worker.q")) as f:
            qs = [json.loads(x) for x in f.read().split("\n") if x.strip()]
        self.assertEqual(len(qs), 2)
        q = qs[0]
        self.assertEqual((q["start"], q["end"]), ("Bachwil, Testland", "Grossstadt, Testland"))
        self.assertEqual(q["bias"], {"lat": 47.4, "lon": 8.4})
        self.assertEqual(q["time_delta"], 0, "live traffic only shows for a departure now")
        blob = json.dumps(qs)
        for private in ("Lindenweg", "9999", "47.1234", "8.5678"):
            self.assertNotIn(private, blob)
        self.assertIn("start_coords", qs[1], "town coordinates cached for the next query")

    def test_route_refuses_streets(self):
        self.routes(Bachwil__Grossstadt={"live": 45, "usual": 44})
        self.run_dw("route", "Lindenweg 4", "Grossstadt", code=2)
        out = self.run_dw("route", "Bachwil", "Grossstadt")
        self.assertIn("live 45 min, usual 44 min", out)

    def test_status(self):
        self.routes(Bachwil__Grossstadt={"live": 45, "usual": 45})
        self.run_dw("check", "--cron", now="2026-03-18 19:05")
        out = self.run_dw("status", now="2026-03-18 19:06")
        self.assertIn("Tanzkurs: Bachwil → Grossstadt", out)
        self.assertIn("1 checks · 0 notes", out)
        self.assertEqual(oct(os.stat(os.path.join(self.t, "state", "state.json")).st_mode & 0o777), "0o600")


PHONE_STUB = """#!/bin/sh
echo "$*" >> "$FAKE_PHONE_LOG"
rc=$(cat "$FAKE_PHONE_RC" 2>/dev/null || echo 0)
if [ "$rc" = 4 ]; then echo '{"ok": false, "status": "no_device"}'; else echo '{"ok": true, "status": "sent"}'; fi
exit $rc
"""


class Live(DriveWatch):
    """The phone app's Live Update chip (livecount) with a stub phone: 60 min before the leave time, updated when Waze
    moves it, ended once it has passed; nothing without LIVECOUNT_PHONE in test state."""

    def setUp(self):
        super().setUp()
        self.plog, self.prc = os.path.join(self.t, "phone.log"), os.path.join(self.t, "phone.rc")
        stub = os.path.join(self.t, "phone")
        with open(stub, "w") as f:
            f.write(PHONE_STUB)
        os.chmod(stub, 0o755)
        self.env.update(
            LIVECOUNT_PHONE=stub,
            LIVECOUNT_DIR=os.path.join(self.t, "live"),
            FAKE_PHONE_LOG=self.plog,
            FAKE_PHONE_RC=self.prc,
        )

    def calls(self):
        try:
            with open(self.plog) as f:
                return f.read().splitlines()
        except OSError:
            return []

    @staticmethod
    def epoch(s):
        import datetime as dt
        from zoneinfo import ZoneInfo

        return int(dt.datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo("Europe/Zurich")).timestamp())

    def test_chip_from_60_min_before_updates_and_ends(self):
        self.routes(Bachwil__Grossstadt={"live": 45, "usual": 45})
        self.run_dw("check", "--cron", now="2026-03-18 19:01")
        self.assertEqual(self.calls(), [], "leave 20:15 is 74 min away")
        self.run_dw("check", "--cron", now="2026-03-18 19:16")
        c = self.calls()
        self.assertEqual(len(c), 1, c)
        self.assertTrue(
            c[0].startswith("push --level info --title Leave by 20:15 --body Traffic as usual --tag live:drive:"), c
        )
        self.assertIn(f"--live countdown:{self.epoch('2026-03-18 20:15')}", c[0])
        self.assertNotIn("Grossstadt", c[0])
        self.routes(Bachwil__Grossstadt={"live": 60, "usual": 45})
        self.run_dw("check", "--cron", now="2026-03-18 19:31")
        c = self.calls()
        self.assertEqual(len(c), 2, c)
        self.assertIn("--title Leave by 20:00 --body Traffic: leave 15 min earlier", c[1])
        self.assertIn(f"--live countdown:{self.epoch('2026-03-18 20:00')}", c[1])
        self.assertEqual(c[0].split("--tag ")[1].split()[0], c[1].split("--tag ")[1].split()[0], "same tag: updated")
        self.run_dw("check", "--cron", now="2026-03-18 19:35")
        self.assertEqual(len(self.calls()), 2, "unchanged: no push")
        self.run_dw("check", "--cron", now="2026-03-18 20:01")
        c = self.calls()
        self.assertEqual(len(c), 3, c)
        self.assertTrue(c[2].startswith("cancel live:drive:"), c)

    def test_no_device_silent(self):
        with open(self.prc, "w") as f:
            f.write("4")
        self.routes(Bachwil__Grossstadt={"live": 45, "usual": 45})
        out = self.run_dw("check", "--cron", now="2026-03-18 19:16")
        self.assertEqual(out, "")
        self.run_dw("check", "--cron", now="2026-03-18 19:20")
        self.assertEqual(len(self.calls()), 1, "backed off")

    def test_no_stub_no_phone(self):
        self.env.pop("LIVECOUNT_PHONE")
        self.routes(Bachwil__Grossstadt={"live": 45, "usual": 45})
        self.run_dw("check", "--cron", now="2026-03-18 19:16")
        self.assertEqual(self.calls(), [])
        self.assertFalse(os.path.exists(os.path.join(self.t, "live")))

    def test_dry_run_prints_chip(self):
        self.routes(Bachwil__Grossstadt={"live": 45, "usual": 45})
        out = self.run_dw("check", "--dry-run", now="2026-03-18 19:16")
        self.assertIn('live: start chip "Leave by 20:15" (Traffic as usual) until 20:15', out)
        self.assertEqual(self.calls(), [])


for _n in [n for n in dir(DriveWatch) if n.startswith("test")]:
    setattr(Live, _n, None)  # Live reuses DriveWatch's fixtures, not its tests


if __name__ == "__main__":
    unittest.main(verbosity=1)
