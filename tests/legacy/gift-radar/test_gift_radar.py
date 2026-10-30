#!/usr/bin/env python3
"""Tests for ~/bin/gift-radar (or GIFT_RADAR_BIN). Everything runs against temp state, a fake `vault`, a fake
`llm-local` (never called since privacy plan A: one `vault ask tg --kind interests`) and the real `remind` pointed at
a temp REMIND_DB; the real state files are checked to be untouched.
Run: ~/opt/gift-radar-tests/run.sh"""

import datetime as dt
import hashlib
import importlib.machinery
import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest

BIN = os.environ.get("GIFT_RADAR_BIN") or os.path.expanduser("~/bin/gift-radar")
gr = importlib.machinery.SourceFileLoader("gift_radar", BIN).load_module()
D = dt.date

MEMORY = textwrap.dedent("""\
    ---
    name: people-and-dates
    description: test fixture (from his calendar, 2026-10-02)
    ---
    Family:
    - Testa Person (Oct 21)
    - Mira "Mimi" Person (birthday Aug 15)
    - "Oma", his grandma (Oct 4)
    - Leapy Day (29.02.)

    Close friends:
    - Olo (Olaf Exemplar), Jun 9; party Jun 12 2027; also in a group
    - Lino (Lino Sample): bachelor party May 8 2027, wedding May 15 2027; karaoke
    - Marco: organizes the group's trip; lives at Somestrasse 2, 8304 Town (~14 min walk)
    - Newy Year, Jan 4
    """)

FAKE_VAULT = r"""#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
open(os.environ['FAKE_LOG'], 'a').write(' '.join(a) + '\n')
if os.environ.get('FAKE_VAULT_FAIL'):
    print('vault down', file=sys.stderr); sys.exit(3)
OUT = {
 'facts': json.dumps({'chat': 'Person A', 'events': [
     {'what': 'Visit the new apartment in Belgrade', 'date': '2026-09-05', 'status': 'done'},
     {'what': 'Doctor appointment', 'date': '2026-10-13', 'status': 'planned'},
     {'what': 'Old plan', 'status': 'cancelled'}],
   'places': [{'name': 'Belgrade'}], 'decisions': [{'what': 'Buy a freezer for CHF 300'}],
   'todos': [{'what': 'Set up the new router', 'due': '2026-10-23'}]}),
 'find': json.dumps({'results': [{'source': 'tg', 'text': 'Testa wants to try 3D printing'}]}),
 'search': 'Search: 1 message(s)\n2026-05-01 Person A: I wish I had a better coffee machine\n',
 'ask': json.dumps({'source': 'telegram', 'kind': 'interests', 'messages': 120, 'answer': {'interests': [
     {'phrase': 'smart home gadgets', 'topic': 'tech'}, {'phrase': '3D printing', 'topic': 'tech'},
     {'phrase': None, 'topic': 'private'}, {'phrase': 'doctor visits', 'topic': 'other'},
     {'phrase': 'trip to Belgrade', 'topic': 'travel'}, {'phrase': 'better coffee machine', 'topic': 'shopping'}]}}),
}
JOBS = {'facts': 'aaaaaaaaaaaaaaaa', 'find': 'bbbbbbbbbbbbbbbb', 'search': 'cccccccccccccccc', 'ask': 'dddddddddddddddd'}
if a[:2] == ['tg', 'find-chat']:
    print("1 chat(s) for 'x':\n      111  user       Person A · 4 matching msg(s)\n -[NUMBER]  supergroup G")
elif a[0] == '--async':
    op = a[1] if a[1] in ('facts', 'find', 'ask') else 'search'
    print(JOBS[op])
elif a[:2] == ['job', 'wait']:
    op = {v: k for k, v in JOBS.items()}[a[2]]
    if op == 'ask' and os.environ.get('FAKE_ASK_FAIL'):
        print('vault: the local LLM gave no usable JSON for the answer (answer_failed)', file=sys.stderr); sys.exit(4)
    print(OUT[op])
else:
    print('unexpected', a, file=sys.stderr); sys.exit(2)
"""  # noqa: E501

FAKE_LLM = r"""#!/usr/bin/env python3
import json, os, sys
blob = sys.stdin.read()
open(os.environ['FAKE_LOG'], 'a').write('LLM ' + str(len(blob)) + '\n')
if os.environ.get('FAKE_LLM_FAIL'):
    sys.exit(1)
print(json.dumps({'facts': ['Likes tinkering with tech and smart home gadgets', 'Wants to try 3D printing',
                            'Has a doctor appointment next week', 'Plans a trip to Belgrade',
                            'Invests in stocks', 'Wishes for a better coffee machine']}))
"""

REPORT = textwrap.dedent("""\
    # Gift ideas
    ## 1. Lino's wedding, Sat 15.05
    **Suggestions**
    1. **Vinyl** about CHF 30.
    ## 2. Testa's birthday, Wed 21.10
    **What the chats say**
    - Family. Likes tech help,
      asks about AI tools.
    - Pays CHF 50 a month for something.
    - No wishes or hints found.
    **Suggestions**
    1. **A year of ChatGPT Plus**, set up together. About CHF 220.
    2. **Wi-Fi photo frame** for the new apartment.
    ---
    """)


def md5(path):
    try:
        with open(path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()
    except OSError:
        return None


REAL = [
    os.path.expanduser("~/.local/state/gift-radar.json"),
    os.path.expanduser("~/.local/state/remind/reminders.json"),
]
REAL_BEFORE = {p: md5(p) for p in REAL}


class Unit(unittest.TestCase):
    def test_feb29_in_non_leap_year(self):
        self.assertEqual(gr.birthday_in_year(2, 29, 2027), D(2027, 2, 28))
        self.assertEqual(gr.birthday_in_year(2, 29, 2028), D(2028, 2, 29))

    def test_due_exactly_10_days(self):
        p = [{"month": 10, "day": 28}]
        self.assertEqual(gr.due_birthdays(p, D(2026, 10, 18), 10, 0), [(p[0], D(2026, 10, 28), 10)])
        self.assertEqual(gr.due_birthdays(p, D(2026, 10, 17), 10, 0), [])
        self.assertEqual(gr.due_birthdays(p, D(2026, 10, 19), 10, 0), [])

    def test_catchup_window(self):
        p = [{"month": 10, "day": 28}]
        self.assertEqual(gr.due_birthdays(p, D(2026, 10, 21), 10, 3), [(p[0], D(2026, 10, 28), 7)])
        self.assertEqual(gr.due_birthdays(p, D(2026, 10, 22), 10, 3), [])

    def test_year_wrap(self):
        p = [{"month": 1, "day": 4}]
        self.assertEqual(gr.due_birthdays(p, D(2026, 12, 25), 10, 0), [(p[0], D(2027, 1, 4), 10)])
        p = [{"month": 1, "day": 1}]
        self.assertEqual(gr.due_birthdays(p, D(2026, 12, 22), 10, 0), [(p[0], D(2027, 1, 1), 10)])
        self.assertEqual(gr.next_birthday(1, 1, D(2026, 12, 22)), D(2027, 1, 1))

    def test_feb29_due(self):
        p = [{"month": 2, "day": 29}]
        self.assertEqual(gr.due_birthdays(p, D(2027, 2, 18), 10, 0), [(p[0], D(2027, 2, 28), 10)])
        self.assertEqual(gr.due_birthdays(p, D(2027, 2, 19), 10, 0), [])
        self.assertEqual(gr.due_birthdays(p, D(2028, 2, 19), 10, 0), [(p[0], D(2028, 2, 29), 10)])
        self.assertEqual(gr.due_birthdays(p, D(2028, 2, 18), 10, 0), [])

    def test_find_dates_year_flag(self):
        self.assertEqual([x[2:] for x in gr.find_dates("party Jun 12 2027; Jun 9")], [(6, 12, True), (6, 9, False)])
        self.assertEqual(
            [x[2:] for x in gr.find_dates("21.10.2026 and 21.10. and 4 Okt")],
            [(10, 21, True), (10, 21, False), (10, 4, False)],
        )
        self.assertEqual(gr.find_dates("PersonC 12 and v4.1 and 31.02."), [])

    def test_parse_people(self):
        ps = {p["file"]: p for p in gr.parse_people(MEMORY)}
        self.assertEqual(sorted(ps), ["leapy", "mimi", "newy", "olo", "oma", "testa"])
        self.assertEqual((ps["testa"]["month"], ps["testa"]["day"], ps["testa"]["section"]), (10, 21, "Family"))
        self.assertEqual((ps["olo"]["month"], ps["olo"]["day"], ps["olo"]["name"]), (6, 9, "Olaf Exemplar"))
        self.assertEqual((ps["mimi"]["display"], ps["mimi"]["month"]), ("Mimi (Mira Person)", 8))
        self.assertEqual((ps["oma"]["display"], ps["oma"]["day"]), ("Oma", 4))
        self.assertEqual((ps["leapy"]["month"], ps["leapy"]["day"]), (2, 29))
        self.assertEqual((ps["newy"]["month"], ps["newy"]["day"], ps["newy"]["section"]), (1, 4, "Close friends"))

    def test_scrub(self):
        self.assertIsNone(gr.scrub("Has a doctor appointment"))
        self.assertIsNone(gr.scrub("Decided: buy a freezer for CHF 300"))
        self.assertIsNone(gr.scrub("[private: health] next week"))
        self.assertEqual(
            gr.scrub("Interests: plants (Monstera), investing (a group), trance raves"),
            "Interests: plants (Monstera), trance raves",
        )
        self.assertEqual(gr.scrub("Likes **3D printing**"), "Likes 3D printing")


class E2E(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp(prefix="gift-radar-tests.")
        t = self.t
        for name, body in (("vault", FAKE_VAULT), ("llm", FAKE_LLM)):
            with open(f"{t}/{name}", "w") as f:
                f.write(body)
            os.chmod(f"{t}/{name}", 0o755)
        with open(f"{t}/tg", "w") as f:
            f.write(f'#!/bin/sh\necho "$*" >> "{t}/tg.log"\n')
        os.chmod(f"{t}/tg", 0o755)
        open(f"{t}/memory.md", "w").write(MEMORY)
        os.makedirs(f"{t}/reports")
        open(f"{t}/reports/gifts-2026-10.md", "w").write(REPORT)
        json.dump(
            [
                {
                    "at": "2026-10-14 18:30",
                    "text": "Gift nudge for Testa (bday Wed 21.10): send the 3 ideas",
                    "owner": False,
                },
                {
                    "at": "2026-10-19 10:00",
                    "text": "Testa Person's birthday is Wed Oct 21: one short nudge",
                    "owner": False,
                },
                {"at": "2026-10-15 09:00", "text": "Buy the gift for Testa", "owner": True},
                {"at": "2027-10-13 10:00", "text": "Gift ideas for Testa next year", "owner": False},
            ],
            open(f"{t}/rem.json", "w"),
        )
        self.env = dict(
            os.environ,
            GIFT_RADAR_MEMORY=f"{t}/memory.md",
            GIFT_RADAR_STATE=f"{t}/state.json",
            GIFT_RADAR_INBOX=f"{t}/inbox",
            GIFT_RADAR_REPORTS=f"{t}/reports",
            GIFT_RADAR_VAULT=f"{t}/vault",
            GIFT_RADAR_LLM=f"{t}/llm",
            GIFT_RADAR_REMIND=os.path.expanduser("~/bin/remind"),
            FAKE_LOG=f"{t}/calls.log",
            REMIND_DB=f"{t}/rem.json",
            REMIND_INBOX=f"{t}/rinbox",
            REMIND_TG=f"{t}/tg",
            REMIND_NOW="2026-10-11 09:23",
        )

    def tearDown(self):
        shutil.rmtree(self.t, ignore_errors=True)

    def run_gr(self, *args, **env):
        r = subprocess.run([BIN, *args], capture_output=True, text=True, timeout=60, env=dict(self.env, **env))
        return r.returncode, r.stdout + r.stderr

    def calls(self):
        try:
            return open(f"{self.t}/calls.log").read().splitlines()
        except OSError:
            return []

    def note(self, name="testa"):
        return open(f"{self.t}/inbox/gift-radar-{name}.md").read()

    def test_run_note_dedup_and_reminder_replace(self):
        rc, out = self.run_gr("run", "--date", "2026-10-11")
        self.assertEqual(rc, 0, out)
        self.assertEqual(os.listdir(f"{self.t}/inbox"), ["gift-radar-testa.md"])
        n = self.note()
        self.assertIn("# Gift radar: Testa Person, birthday Wed 21.10.2026 (in 10 days)", n)
        for want in (
            "smart home gadgets (tech)",
            "3D printing (tech)",
            "trip to Belgrade (travel)",
            "(vault ask)",
            "Research needed",
            "WebSearch",
            "gift-radar skip testa 2026",
            "A year of ChatGPT Plus",
            "Wi-Fi photo frame",
            "Gift nudge for Testa",
        ):
            self.assertIn(want, n)
        for bad in ("doctor", "Doctor", "Invests", "CHF 300", "CHF 220", "Vinyl", "Old plan"):
            self.assertNotIn(bad, n)
        self.assertEqual(sum(1 for ln in n.splitlines() if ln.startswith("- ")), 4)  # 6 answers: 1 private, 1 scrubbed
        self.assertFalse(any(c.startswith("LLM") for c in self.calls()))  # no local LLM step here any more
        self.assertEqual(
            [c.split(" --since")[0] for c in self.calls() if c.startswith("--async")], ["--async ask tg --chat 111"]
        )  # one ask; no facts/find/search text
        st = json.load(open(f"{self.t}/state.json"))
        self.assertIn("testa-person/2026", st["notified"])
        self.assertEqual(
            st["notified"]["testa-person/2026"]["cancelled"],
            ["Gift nudge for Testa (bday Wed 21.10): send the 3 ideas"],
        )
        left = [r["text"] for r in json.load(open(f"{self.t}/rem.json"))]
        self.assertEqual(len(left), 3)
        self.assertNotIn("Gift nudge for Testa (bday Wed 21.10): send the 3 ideas", left)
        # dedup: the same day again and a catch-up day later -> nothing new, no vault calls
        before = len(self.calls())
        mtime = os.path.getmtime(f"{self.t}/inbox/gift-radar-testa.md")
        for day in ("2026-10-11", "2026-10-13"):
            rc, out = self.run_gr("run", "--date", day)
            self.assertEqual(rc, 0, out)
            self.assertIn("already notified", out)
        self.assertEqual(len(self.calls()), before)
        self.assertEqual(os.path.getmtime(f"{self.t}/inbox/gift-radar-testa.md"), mtime)

    def test_skip_and_unskip(self):
        rc, out = self.run_gr("skip", "Testa", "2026")
        self.assertEqual(rc, 0, out)
        self.assertIn("skipped: Testa Person, birthday 21.10.2026", out)
        rc, out = self.run_gr("run", "--date", "2026-10-11")
        self.assertIn("skipped", out)
        self.assertFalse(os.path.exists(f"{self.t}/inbox/gift-radar-testa.md"))
        self.assertEqual(self.calls(), [])
        self.assertEqual(len(json.load(open(f"{self.t}/rem.json"))), 4)  # nothing cancelled
        rc, out = self.run_gr("skip", "nobody", "2026")
        self.assertEqual(rc, 2)
        self.run_gr("unskip", "testa-person", "2026")
        rc, out = self.run_gr("run", "--date", "2026-10-11")
        self.assertTrue(os.path.exists(f"{self.t}/inbox/gift-radar-testa.md"), out)

    def test_skip_other_year_does_not_mute(self):
        self.run_gr("skip", "testa", "2027")
        self.run_gr("run", "--date", "2026-10-11")
        self.assertTrue(os.path.exists(f"{self.t}/inbox/gift-radar-testa.md"))

    def test_dry_run_writes_nothing_and_asks_once(self):
        rc, out = self.run_gr("run", "--dry-run", "--date", "2026-10-11")
        self.assertEqual(rc, 0, out)
        self.assertIn("would write", out)
        self.assertIn("trip to Belgrade (travel)", out)
        self.assertNotIn("doctor", out)
        self.assertFalse(os.path.exists(f"{self.t}/inbox"))
        self.assertFalse(os.path.exists(f"{self.t}/state.json"))
        self.assertEqual(len(json.load(open(f"{self.t}/rem.json"))), 4)
        c = self.calls()
        self.assertEqual(
            [x.split(" --json")[0] for x in c if x.startswith("--async")],
            ["--async ask tg --chat 111 --since 180d -n 300 --kind interests"],
        )
        self.assertFalse(any(x.startswith(("LLM", "find", "tg search", "--async facts")) for x in c), c)

    def test_ask_failure_still_writes_note_with_earlier_research(self):
        rc, out = self.run_gr("run", "--date", "2026-10-11", FAKE_ASK_FAIL="1")
        self.assertEqual(rc, 0, out)
        n = self.note()
        self.assertIn("vault ask: exit 4", n)
        self.assertIn("(earlier research)", n)
        self.assertNotIn("Belgrade", n)

    def test_vault_down_still_writes_note_with_earlier_research(self):
        rc, out = self.run_gr("run", "--date", "2026-10-11", FAKE_VAULT_FAIL="1")
        self.assertEqual(rc, 0, out)
        n = self.note()
        self.assertIn("Family. Likes tech help, asks about AI tools. (earlier research)", n)
        self.assertNotIn("Pays", n)
        self.assertNotIn("No wishes", n)
        self.assertIn("Ideas there: A year of ChatGPT Plus; Wi-Fi photo frame.", n)
        self.assertIn("find-chat: exit 3", n)

    def test_year_wrap_and_leap_end_to_end(self):
        rc, out = self.run_gr("run", "--date", "2026-12-25")
        self.assertTrue(os.path.exists(f"{self.t}/inbox/gift-radar-newy.md"), out)
        self.assertIn("birthday Mon 04.01.2027", self.note("newy"))
        self.assertIn("newy-year/2027", json.load(open(f"{self.t}/state.json"))["notified"])
        rc, out = self.run_gr("run", "--date", "2027-02-18")
        self.assertIn("birthday Sun 28.02.2027", self.note("leapy"))

    def test_nothing_due(self):
        rc, out = self.run_gr("run", "--date", "2026-10-06")
        self.assertEqual(rc, 0, out)
        self.assertIn("nothing due", out)
        self.assertEqual(self.calls(), [])

    def test_missing_memory_cron_note(self):
        rc, out = self.run_gr("run", "--cron", GIFT_RADAR_MEMORY=f"{self.t}/nope.md")
        self.assertEqual(rc, 1)
        self.assertIn("cannot read", open(f"{self.t}/inbox/gift-radar-error.md").read())

    def test_list(self):
        rc, out = self.run_gr("list", "--date", "2026-10-06", "--days", "50")
        self.assertEqual(rc, 0, out)
        self.assertIn("Testa Person", out)
        self.assertIn("radar 11.10", out)
        self.assertNotIn("Lino", out)


class Untouched(unittest.TestCase):
    def test_zz_real_state_untouched(self):
        for p, h in REAL_BEFORE.items():
            self.assertEqual(md5(p), h, p)


if __name__ == "__main__":
    unittest.main(verbosity=1)
