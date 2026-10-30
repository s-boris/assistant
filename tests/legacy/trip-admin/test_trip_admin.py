#!/usr/bin/env python3
"""Offline tests for trip-admin: fake bills/deadlines/paperless tools, temp reminder store, no network."""

import datetime as dt
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.environ.get("TRIP_ADMIN_SCRIPT") or os.path.join(HERE, "..", "..", "..", "scripts", "trip-admin")

FAKE = textwrap.dedent(r"""
    #!/usr/bin/env python3
    import json, os, sys
    fix = os.environ["FIX"]
    tool = os.path.basename(sys.argv[0])
    with open(os.path.join(fix, "calls.log"), "a") as f:
        f.write(tool + " " + " ".join(sys.argv[1:]) + "\n")
    if os.path.exists(os.path.join(fix, tool + ".fail")):
        sys.stderr.write("boom\n"); sys.exit(1)
    a = sys.argv[1:]
    if tool == "bills":
        print(open(os.path.join(fix, "bills.json")).read())
    elif tool == "deadlines":
        with open(os.path.join(fix, "deadlines.env"), "a") as f:
            f.write(os.environ.get("DEADLINES_TODAY", "-") + "\n")
        print(open(os.path.join(fix, "deadlines.json" if "--json" in a else "deadlines.txt")).read())
    elif tool == "paperless":
        if a[0] == "recent":
            print(open(os.path.join(fix, "recent.json")).read())
        elif a[0] == "get":
            print(json.dumps(json.load(open(os.path.join(fix, "cards.json")))[a[1]]))
        else:
            sys.exit(3)
""").lstrip()

BILLS = [
    {"id": 1, "payee": "Telco", "amount": 59.9, "currency": "CHF", "due": "2026-11-25", "status": "open"},
    {"id": 2, "payee": "Gym", "amount": 80, "currency": "CHF", "due": "2026-11-16", "status": "open"},
    {"id": 3, "payee": "Paid one", "amount": 10, "currency": "CHF", "due": "2026-11-22", "status": "paid"},
    {"id": 4, "payee": "Later", "amount": 10, "currency": "CHF", "due": "2027-01-15", "status": "open"},
    {"id": 5, "payee": "Old", "amount": 25, "currency": "CHF", "due": "2026-10-01", "status": "open"},
    {
        "id": 6,
        "payee": "Linked",
        "amount": 33,
        "currency": "CHF",
        "due": "2026-12-20",
        "status": "open",
        "paperless": 72,
    },
]
DEADLINES = [
    {
        "id": "kk-switch",
        "due": "2026-11-30",
        "act_by": "2026-11-30",
        "title": "KK switching notice must arrive",
        "active": True,
        "note": "",
        "covered_by": "kk-compare",
    },
    {
        "id": "tax-rate-3",
        "due": "2026-12-01",
        "act_by": "2026-12-01",
        "title": "Tax instalment 3",
        "active": True,
        "note": "",
        "covered_by": None,
    },
    {
        "id": "pillar3a",
        "due": "2026-12-31",
        "act_by": "2026-12-31",
        "title": "3a",
        "active": True,
        "note": "",
        "covered_by": None,
    },
]
DEADLINES_TXT = """Deadlines (CH), from Fri 2026-10-16, next 1 days
  (none)
  Date unknown (set it in profile.json once the owner tells me):
     -   Household fee: annual invoice (CHF 1)  [profile "serafe_invoice_date"]
     -   Rail pass renews: last day to cancel  [profile "halbtax_expiry"]
"""
RECENT = {
    "results": [
        {"id": 70, "document_type": "Rechnung", "added": "2026-10-10"},
        {"id": 71, "document_type": "Lohnabrechnung", "added": "2026-10-10"},
        {"id": 72, "document_type": "Rechnung", "added": "2026-10-10"},
        {"id": 73, "document_type": "Rechnung", "added": "2024-01-10"},
        {"id": 9, "document_type": "Police", "added": "2026-10-14"},
        {"id": 74, "document_type": None, "added": "2026-10-11"},
    ]
}
CARDS = {
    "70": {
        "id": 70,
        "document_type": "Rechnung",
        "correspondent": "Utility",
        "amount": "120.00",
        "currency": "CHF",
        "due_date": "2026-12-08",
    },
    "9": {
        "id": 9,
        "document_type": "Police",
        "correspondent": None,
        "amount": "123.45",
        "currency": "CHF",
        "due_date": None,
    },
    "74": {"id": 74, "document_type": None, "correspondent": None, "amount": None, "due_date": "2027-03-01"},
}
RECURRING = [
    {
        "title": "Health premium (monthly)",
        "every": "monthly:1",
        "kind": "check",
        "paperless": 9,
        "advice": "Check auto-pay or pay before you leave.",
    }
]
REMINDERS = [
    {
        "at": "2026-11-28 09:00",
        "owner": True,
        "text": "Tax instalment in 3 days",
        "tag": "deadline:tax-rate-3:2026-12-01:3d",
    },
    {"at": "2026-11-29 09:00", "owner": False, "text": "my own", "tag": "deadline:x"},
    {"at": "2026-11-30 09:00", "owner": True, "text": "untagged", "tag": None},
    {"at": "2026-12-17 09:00", "owner": True, "text": "after the trip", "tag": "deadline:pillar3a"},
    {"at": "2026-11-24 09:00", "owner": True, "text": "bill reminder", "tag": "bill:1"},
]


def load_module():
    loader = importlib.machinery.SourceFileLoader("trip_admin", SCRIPT)
    spec = importlib.util.spec_from_loader("trip_admin", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="trip-admin-test-")
        self.fix = os.path.join(self.tmp, "fix")
        os.makedirs(self.fix)
        for tool in ("bills", "deadlines", "paperless"):
            p = os.path.join(self.tmp, tool)
            with open(p, "w") as f:
                f.write(FAKE)
            os.chmod(p, 0o755)
        self.write("bills.json", BILLS)
        self.write("deadlines.json", DEADLINES)
        with open(os.path.join(self.fix, "deadlines.txt"), "w") as f:
            f.write(DEADLINES_TXT)
        self.write("recent.json", RECENT)
        self.write("cards.json", CARDS)
        self.write("recurring.json", RECURRING)
        self.write("reminders.json", REMINDERS)
        self.write("trip.json", {"name": "Islands", "from": "2026-11-20", "until": "2026-12-06"})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, obj):
        with open(os.path.join(self.fix, name), "w") as f:
            json.dump(obj, f)

    def run_ta(self, *args, **env_over):
        env = dict(
            os.environ,
            FIX=self.fix,
            TRIP_ADMIN_TODAY="2026-10-16",
            TRIP_ADMIN_BILLS=os.path.join(self.tmp, "bills"),
            TRIP_ADMIN_DEADLINES=os.path.join(self.tmp, "deadlines"),
            TRIP_ADMIN_PAPERLESS=os.path.join(self.tmp, "paperless"),
            TRIP_ADMIN_REMIND_DB=os.path.join(self.fix, "reminders.json"),
            TRIP_ADMIN_RECURRING=os.path.join(self.fix, "recurring.json"),
            TRIP_ADMIN_PEOPLE=os.path.join(self.fix, "people.json"),
            TRIP_ADMIN_TRIP=os.path.join(self.fix, "trip.json"),
        )
        env.update(env_over)
        p = subprocess.run(
            [sys.executable, "-I", SCRIPT, *args],
            env=env,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=60,
        )
        return p

    def calls(self):
        path = os.path.join(self.fix, "calls.log")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return f.read().splitlines()

    def items(self, p):
        return {i["source"] + "|" + i["title"]: i for i in json.loads(p.stdout)["items"]}


class TestSweep(Base):
    def deadlines_env(self):
        with open(os.path.join(self.fix, "deadlines.env")) as f:
            return f.read().split()

    def test_faked_today_reaches_deadlines(self):
        # a dry run "as of" a later day must shift deadlines' own window too, or it misses due items
        self.run_ta("--json", TRIP_ADMIN_TODAY="2026-11-13")
        self.assertEqual(self.deadlines_env(), ["2026-11-13", "2026-11-13"])

    def test_explicit_deadlines_today_wins(self):
        self.run_ta("--json", TRIP_ADMIN_TODAY="2026-11-13", DEADLINES_TODAY="2026-11-01")
        self.assertEqual(self.deadlines_env(), ["2026-11-01", "2026-11-01"])

    def test_classification(self):
        p = self.run_ta("--json")
        self.assertEqual(p.returncode, 1, p.stderr)
        it = self.items(p)
        r = json.loads(p.stdout)
        self.assertEqual((r["leave_by"], r["back"]), ("2026-11-19", "2026-12-07"))
        self.assertEqual(r["name"], "Islands")
        telco = it["bills #1|Telco"]
        self.assertEqual((telco["status"], telco["latest"]), ("before", "2026-11-19"))
        gym = it["bills #2|Gym"]
        self.assertEqual((gym["status"], gym["latest"]), ("before", "2026-11-13"))  # Mon 16.11 - 1 working day
        self.assertEqual(it["bills #5|Old"]["status"], "overdue")
        self.assertNotIn("bills #3|Paid one", it)
        self.assertNotIn("bills #4|Later", it)
        self.assertNotIn("bills #6|Linked", it)  # due after the scan window
        kk = it["deadlines kk-switch|KK switching notice must arrive"]
        self.assertEqual((kk["status"], kk["latest"]), ("before", "2026-11-19"))
        self.assertIn("handled by kk-compare", kk["note"])
        self.assertIn("registered", kk["advice"])
        tax = it["deadlines tax-rate-3|Tax instalment 3"]
        self.assertEqual((tax["status"], tax["latest"], tax["pay_by"]), ("can-wait", "2026-12-30", "2026-12-31"))
        self.assertFalse(any(k.startswith("deadlines pillar3a") for k in it))
        util = it["paperless #70|Rechnung · Utility"]
        self.assertEqual((util["status"], util["latest"], util["amount"]), ("after", "2026-12-07", "120.00"))
        prem = it["recurring (paperless #9)|Health premium (monthly)"]
        self.assertEqual(
            (prem["due"], prem["status"], prem["latest"], prem["amount"]),
            ("2026-12-01", "before", "2026-11-19", "123.45"),
        )
        self.assertEqual(prem["advice"], "Check auto-pay or pay before you leave.")
        self.assertEqual([u["missing"] for u in r["unknown"]], ["serafe_invoice_date", "halbtax_expiry"])
        self.assertEqual([x["text"] for x in r["reminders"]], ["bill reminder", "Tax instalment in 3 days"])

    def test_read_only_and_card_filter(self):
        self.run_ta("--json")
        calls = self.calls()
        verbs = {c.split()[0] + " " + c.split()[1] for c in calls}
        self.assertLessEqual(verbs, {"bills list", "deadlines list", "paperless recent", "paperless get"})
        gets = [c for c in calls if c.startswith("paperless get")]
        self.assertEqual(sorted(g.split()[2] for g in gets), ["70", "74", "9"])  # 71 payslip, 72 linked, 73 old
        self.assertEqual(sum(1 for g in gets if g.split()[2] == "9"), 1)  # cached for the recurring item

    def test_text_output(self):
        p = self.run_ta()
        self.assertEqual(p.returncode, 1)
        out = p.stdout
        self.assertIn("leave by Thu 19.11, back Mon 07.12", out)
        self.assertIn("Before you leave:", out)
        self.assertIn("Can wait until you're back:", out)
        self.assertIn("CHF 123.45", out)
        self.assertIn("Unknown dates", out)
        self.assertIn("Sat 28.11 09:00", out)
        self.assertLess(out.index("Overdue:"), out.index("Before you leave:"))

    def test_md(self):
        md = os.path.join(self.tmp, "r.md")
        p = self.run_ta("--md", md)
        self.assertEqual(p.returncode, 1)
        with open(md) as f:
            body = f.read()
        self.assertIn("# Pre-trip admin sweep: Islands", body)
        self.assertIn("| **Thu 19.11** | Telco | CHF 59.90 |", body)
        self.assertIn("## Can wait until you're back", body)

    def test_explicit_range_matching_trip_file_keeps_name(self):
        p = self.run_ta("--from", "2026-11-20", "--until", "2026-12-06", "--json", "--no-paperless")
        self.assertEqual(json.loads(p.stdout)["name"], "Islands")

    def test_explicit_range_and_no_paperless(self):
        p = self.run_ta("--from", "2026-11-23", "--until", "2026-11-27", "--no-paperless", "--json")
        r = json.loads(p.stdout)
        self.assertEqual(r["name"], "trip")
        self.assertEqual(r["sources"]["paperless"], "skipped")
        self.assertFalse(any(c.startswith("paperless recent") for c in self.calls()))
        self.assertEqual(r["leave_by"], "2026-11-20")

    def test_source_failure_isolated(self):
        open(os.path.join(self.fix, "paperless.fail"), "w").close()
        p = self.run_ta("--json")
        r = json.loads(p.stdout)
        self.assertEqual(p.returncode, 1)
        self.assertTrue(r["sources"]["paperless"].startswith("FAILED"))
        self.assertIn("bills #1|Telco", self.items(p))
        self.assertTrue(r["sources"]["recurring"].startswith("FAILED"))  # its card needs paperless too

    def test_nothing_to_do(self):
        self.write("bills.json", [])
        self.write("deadlines.json", [])
        self.write("recent.json", {"results": []})
        self.write("recurring.json", [])
        p = self.run_ta()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("Nothing to handle before you leave.", p.stdout)

    def test_usage_errors(self):
        self.assertEqual(self.run_ta("--from", "2026-12-06", "--until", "2026-11-20").returncode, 2)
        self.assertEqual(self.run_ta(TRIP_ADMIN_TRIP=os.path.join(self.tmp, "missing.json")).returncode, 2)
        self.assertEqual(self.run_ta("--from", "2026-13-40", "--until", "2026-12-01").returncode, 2)


PEOPLE = {
    "updated": "2031-10-01",
    "season": ["advent", "advent-calendar", "samichlaus", "christmas"],
    "people": [
        {"name": "Alex Example", "date": "11-27", "kind": "birthday", "source": "calendar"},
        {"name": "Alex Other", "date": "12-07", "kind": "birthday", "source": "calendar"},
        {"name": "Sam", "date": "11-19", "kind": "anniversary", "label": "wedding anniversary"},
        {"name": "Robin Test", "date": "2031-12-03", "kind": "wedding"},
        {"name": "Kim Before", "date": "11-17", "kind": "birthday"},  # one day before the window
        {"name": "Jo After", "date": "12-09", "kind": "birthday"},  # one day after the window
        {"name": "Old Wedding", "date": "2030-11-25", "kind": "wedding"},
        {"name": "Broken", "date": "13-40"},
        {"date": "11-25"},
    ],
}
TRIP_2031 = ("--from", "2031-11-21", "--until", "2031-12-05", "--no-paperless")


class TestPeople(Base):
    """Trip Fri 21.11 - Fri 05.12.2031: leave by Thu 20.11, back Mon 08.12, people window 18.11 - 08.12."""

    def run_people(self, *extra):
        self.write("people.json", PEOPLE)
        return self.run_ta(*TRIP_2031, *extra, TRIP_ADMIN_TODAY="2031-10-16")

    def test_window_names_and_act_by(self):
        p = self.run_people("--json")
        r = json.loads(p.stdout)
        got = [(x["date"], x["name"], x["what"], x["when"]) for x in r["people"]]
        self.assertEqual(
            got,
            [
                ("2031-11-19", "Sam", "wedding anniversary", "before"),
                ("2031-11-27", "Alex E.", "birthday", "away"),
                ("2031-11-30", "", "1st Advent", "away"),
                ("2031-12-01", "", "Advent calendars start", "away"),
                ("2031-12-03", "Robin", "wedding", "away"),
                ("2031-12-06", "", "St. Nicholas (Samichlaus)", "after"),
                ("2031-12-07", "Alex O.", "birthday", "after"),
            ],
        )
        by = {x["name"] or x["what"]: x for x in r["people"]}
        # Sam (Wed 19.11, before departure): 5 working days before the day itself
        self.assertEqual((by["Sam"]["act_by"], by["Sam"]["days_before"]), ("2031-11-12", 9))
        # away / after: 5 working days before the last working day before leaving (Thu 20.11)
        self.assertEqual((by["Alex E."]["act_by"], by["Alex E."]["days_before"]), ("2031-11-13", 8))
        self.assertEqual(by["Alex O."]["act_by"], "2031-11-13")
        self.assertFalse(any(x["late"] for x in r["people"]))
        self.assertIn("2 rows skipped", r["sources"]["people"])
        self.assertIn("updated 2031-10-01", r["sources"]["people"])
        self.assertNotIn("Example", p.stdout)  # first names only
        self.assertNotIn("full", p.stdout)

    def test_text_and_md(self):
        md = os.path.join(self.tmp, "p.md")
        p = self.run_people("--md", md)
        out = p.stdout
        self.assertIn("People while away (Tue 18.11 – Mon 08.12):", out)
        self.assertIn("Thu 27.11  Alex E. · birthday · while you're away", out)
        self.assertIn("Order the gift by Thu 13.11 (8 days before departure); hand it over or have it shipped", out)
        self.assertIn("Sun 30.11  1st Advent · while you're away", out)
        for word in ("Example", "Other", "Test", "Kim", "Jo ·", "Old · wedding", "Broken"):
            self.assertNotIn(word, out)
        with open(md) as f:
            body = f.read()
        self.assertIn("## People while away", body)
        self.assertIn("| Thu 27.11 | Alex E. | birthday | while you're away | **Thu 13.11** | 8 |", body)
        self.assertIn(
            "| Sat 06.12 | – | St. Nicholas (Samichlaus) | right after you're back | **Thu 13.11** | 8 |", body
        )
        self.assertNotIn("Example", body)

    def test_exit_code_unchanged_and_options(self):
        self.write("bills.json", [])
        self.write("deadlines.json", [])
        self.write("recurring.json", [])
        p = self.run_people("--json", "--no-season", "--people-margin", "0", "--gift-lead", "2")
        self.assertEqual(p.returncode, 0, p.stderr)  # people are informational
        r = json.loads(p.stdout)
        self.assertEqual([x["name"] for x in r["people"]], ["Alex", "Robin"])  # no other Alex in this window
        self.assertEqual(r["people"][0]["act_by"], "2031-11-18")  # Thu 20.11 - 2 working days

    def test_late_and_season_config(self):
        cfg = dict(PEOPLE, season=["samichlaus"])
        self.write("people.json", cfg)
        p = self.run_ta(*TRIP_2031, "--json", TRIP_ADMIN_TODAY="2031-11-17")
        r = json.loads(p.stdout)
        self.assertEqual([x["what"] for x in r["people"] if not x["name"]], ["St. Nicholas (Samichlaus)"])
        self.assertTrue(all(x["late"] for x in r["people"]))
        out = self.run_ta(*TRIP_2031, TRIP_ADMIN_TODAY="2031-11-17").stdout
        self.assertIn("Order the gift now", out)
        # a passed season lead time says "now", not a date that is already gone
        self.assertIn("If you give anything: get it now (the usual lead time has passed).", out)
        self.assertNotIn("If you give anything: have it by", out)

    def test_no_file_and_broken_file(self):
        p = self.run_ta(*TRIP_2031, "--json", TRIP_ADMIN_TODAY="2031-10-16")
        r = json.loads(p.stdout)
        self.assertTrue(r["sources"]["people"].startswith("no people file"))
        self.assertEqual(len(r["people"]), 3)  # season only: Advent, calendars, St. Nicholas
        with open(os.path.join(self.fix, "people.json"), "w") as f:
            f.write("{not json")
        p = self.run_ta(*TRIP_2031, "--json", TRIP_ADMIN_TODAY="2031-10-16")
        r = json.loads(p.stdout)
        self.assertEqual(p.returncode, 1)
        self.assertTrue(r["sources"]["people"].startswith("FAILED"))
        self.assertEqual(r["people"], [])

    def test_plain_list_file(self):
        self.write("people.json", [{"name": "Alex Example", "date": "11-27"}])
        r = json.loads(self.run_ta(*TRIP_2031, "--json", "--no-season", TRIP_ADMIN_TODAY="2031-10-16").stdout)
        self.assertEqual([(x["name"], x["what"]) for x in r["people"]], [("Alex", "birthday")])

    def test_import_calendar(self):
        self.write(
            "people.json",
            {
                "season": ["advent"],
                "people": [
                    {"name": "Sam", "date": "11-19", "kind": "anniversary"},
                    {"name": "Gone Person", "date": "01-01", "kind": "birthday", "source": "calendar"},
                ],
            },
        )
        cal = {
            "events": [
                {
                    "eventType": "BIRTHDAY",
                    "summary": "Alex Example's birthday",
                    "start": {"date": "2031-11-27T00:00:00Z"},
                    "recurringEventId": "x1",
                },
                {"eventType": "BIRTHDAY", "summary": "Happy birthday!", "start": {"date": "2031-08-12T00:00:00Z"}},
                {"eventType": "DEFAULT", "summary": "Robin Test's wedding", "start": {"date": "2031-12-03"}},
                {
                    "eventType": "DEFAULT",
                    "summary": "Jahrestag Kim",
                    "start": {"date": "2031-05-02"},
                    "recurringEventId": "x2",
                },
                {"eventType": "DEFAULT", "summary": "Dentist", "start": {"dateTime": "2031-11-25T10:00:00+01:00"}},
            ]
        }
        src = os.path.join(self.tmp, "cal.json")
        with open(src, "w") as f:
            json.dump(cal, f)
        p = self.run_ta("--import-calendar", src, TRIP_ADMIN_TODAY="2031-10-16")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("imported 3 calendar dates, kept 1 other rows", p.stdout)
        for word in ("Alex", "Robin", "Kim", "Sam"):
            self.assertNotIn(word, p.stdout + p.stderr)
        path = os.path.join(self.fix, "people.json")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        with open(path) as f:
            doc = json.load(f)
        self.assertEqual((doc["updated"], doc["season"]), ("2031-10-16", ["advent"]))
        rows = {r["name"]: (r["date"], r["kind"], r.get("source")) for r in doc["people"]}
        self.assertEqual(
            rows,
            {
                "Sam": ("11-19", "anniversary", None),
                "Alex Example": ("11-27", "birthday", "calendar"),
                "Robin Test": ("2031-12-03", "wedding", "calendar"),
                "Kim": ("05-02", "anniversary", "calendar"),
            },
        )
        # importing again is idempotent
        self.run_ta("--import-calendar", src, TRIP_ADMIN_TODAY="2031-10-16")
        with open(path) as f:
            self.assertEqual(len(json.load(f)["people"]), 4)
        self.assertEqual(self.run_ta("--import-calendar", os.path.join(self.tmp, "nope.json")).returncode, 1)


class TestDates(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_module()

    def test_easter_and_holidays(self):
        m = self.m
        self.assertEqual(m.easter(2026), dt.date(2026, 4, 5))
        self.assertEqual(m.easter(2027), dt.date(2027, 3, 28))
        self.assertIn(dt.date(2026, 4, 3), m.holidays(2026))  # Good Friday
        self.assertIn(dt.date(2026, 5, 14), m.holidays(2026))  # Ascension
        self.assertIn(dt.date(2026, 5, 25), m.holidays(2026))  # Whit Monday

    def test_workdays(self):
        m = self.m
        self.assertEqual(m.sub_workdays(dt.date(2026, 11, 30), 2), dt.date(2026, 11, 26))
        self.assertEqual(m.prev_workday(dt.date(2026, 12, 27)), dt.date(2026, 12, 24))
        self.assertEqual(m.next_workday(dt.date(2026, 12, 25)), dt.date(2026, 12, 28))
        self.assertEqual(m.sub_workdays(dt.date(2026, 12, 31), 1), dt.date(2026, 12, 30))

    def test_occurrences(self):
        m = self.m
        a, b = dt.date(2026, 11, 13), dt.date(2026, 12, 10)
        self.assertEqual(m.occurrences("monthly:1", a, b), [dt.date(2026, 12, 1)])
        self.assertEqual(m.occurrences("monthly:last", a, b), [dt.date(2026, 11, 30)])
        self.assertEqual(m.occurrences("yearly:11-30", a, b), [dt.date(2026, 11, 30)])
        self.assertEqual(m.occurrences("monthly:31", dt.date(2027, 2, 1), dt.date(2027, 2, 28)), [dt.date(2027, 2, 28)])
        with self.assertRaises(ValueError):
            m.occurrences("weekly:mon", a, b)

    def test_advent_and_people_dates(self):
        m = self.m
        self.assertEqual(m.advent1(2031), dt.date(2031, 11, 30))
        self.assertEqual(m.advent1(2033), dt.date(2033, 11, 27))  # Christmas on a Sunday
        self.assertEqual(m.advent1(2030), dt.date(2030, 12, 1))
        a, b = dt.date(2031, 12, 20), dt.date(2032, 3, 5)
        self.assertEqual(m.date_hits("02-29", a, b), [dt.date(2032, 2, 29)])
        self.assertEqual(m.date_hits("02-29", dt.date(2031, 2, 1), dt.date(2031, 3, 1)), [dt.date(2031, 2, 28)])
        self.assertEqual(m.date_hits("01-02", a, b), [dt.date(2032, 1, 2)])  # across the year end
        self.assertEqual(m.date_hits("2032-01-02", a, b), [dt.date(2032, 1, 2)])
        self.assertEqual(m.date_hits("2031-01-02", a, b), [])
        for bad in ("13-01", "02-30", "xx", "", None):
            with self.assertRaises((ValueError, TypeError)):
                m.date_hits(bad, a, b)


if __name__ == "__main__":
    unittest.main(verbosity=1)
