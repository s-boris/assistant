#!/usr/bin/env python3
"""Tests for ~/bin/plans-watch: fake vault client, fake remind, temp calcache and HOME (no vault, no tokens).
Fictional people and places only. Run: bash ~/opt/plans-watch-tests/run.sh   (PLANS_WATCH_BIN=path tests a copy)"""
import json
import os
import shutil
import stat
import subprocess
import tempfile
import textwrap
import unittest

TOOL = os.environ.get("PLANS_WATCH_BIN") or os.path.expanduser("~/bin/plans-watch")
NOW = "2026-03-02T10:00:00+01:00"          # a Monday
LATER = "2026-03-02T11:00:00+01:00"        # a later run the same day (notes sort by time)

FAKE_VAULT = textwrap.dedent(r'''
    #!/usr/bin/env python3
    import json, os, sys
    fx = json.load(open(os.environ["FX"]))
    a = sys.argv[1:]
    if a[:1] == ["health"]:
        sys.exit(fx.get("health", 0))
    if a[:1] == ["--async"] or a[:2] == ["job", "wait"]:          # chat re-reads: start a job, collect it
        with open(os.environ["FX"] + ".calls", "a") as c:
            c.write(json.dumps(a) + "\n")
        if a[0] == "--async":
            if fx.get("async_fail"):
                print("vault: LLM busy", file=sys.stderr); sys.exit(75)
            print("0123456789abcdef")
            print("vault: job 0123456789abcdef started (position 1)", file=sys.stderr); sys.exit(0)
        st = fx.get("job", "done")
        if st == "running":
            print(json.dumps({"id": a[2], "state": "running"})); sys.exit(12)
        if st == "gone":
            print("vault: no such job", file=sys.stderr); sys.exit(1)
        print(json.dumps({"id": a[2], "state": "done", "op": "tg.facts", "result": fx.get("reread") or {}}))
        sys.exit(0)
    if fx.get("fail"):
        print("vault: HTTP 502 from the vault", file=sys.stderr); sys.exit(1)
    since, n = int(a[a.index("--since") + 1]), int(a[a.index("--max") + 1])
    ev = [e for e in fx["events"] if e["cursor"] > since]
    page = ev[:n]
    nxt = page[-1]["cursor"] if page else max([since] + [e["cursor"] for e in fx["events"]])
    print(json.dumps({"events": page, "next_cursor": nxt, "more": len(ev) > n}))
''').lstrip()

FAKE_REMIND = "#!/bin/sh\ncat \"$REMIND_FX\"\n"


def ev(cursor, facts, chat="Anna", chat_key="4711", chat_type="user", source="telegram", ts="2026-03-01T18:00:00+01:00",
       ref="m1"):
    return {"cursor": cursor, "source": source, "ts": ts, "chat_key": chat_key, "chat": chat, "chat_type": chat_type,
            "label": "plan_datetime", "actionable": True, "priority": False, "ref": ref, "facts": facts}


def fact(what, date, start=None, status="planned", date_text=None, kind="event", venue=None):
    f = {"kind": kind, "what": what, "status": status}
    if kind == "event":
        f.update(date=date, date_text=date_text or date)
    else:
        f.update(due=date, due_text=date_text or date)
    if start:
        f["start"] = start
    if venue:
        f["venue"] = venue
    return f


class T(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="plans-watch-test-")
        self.state = os.path.join(self.d, "state")
        self.inbox = os.path.join(self.d, "inbox")
        self.fx = os.path.join(self.d, "fx.json")
        self.rfx = os.path.join(self.d, "remind.json")
        self.cal = os.path.join(self.d, "events.json")
        vault, remind = os.path.join(self.d, "vault"), os.path.join(self.d, "remind")
        for p, body in ((vault, FAKE_VAULT), (remind, FAKE_REMIND)):
            with open(p, "w") as f:
                f.write(body)
            os.chmod(p, 0o755)
        self.env = dict(os.environ, HOME=os.path.join(self.d, "home"), PLANS_WATCH_STATE=self.state,
                        PLANS_WATCH_INBOX=self.inbox, PLANS_WATCH_CLIENT=vault, PLANS_WATCH_REMIND=remind,
                        PLANS_WATCH_CALCACHE=self.cal, PLANS_WATCH_NOW=NOW, FX=self.fx, REMIND_FX=self.rfx,
                        USAGE="off")
        places = os.path.join(self.d, "places.json")
        with open(places, "w") as f:
            json.dump({"flags": {"NL": "netherlands"}}, f)
        self.env["PLANS_WATCH_PLACES"] = places
        self.feed([])
        self.reminders([])
        self.calendar([])

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    # ------------------------------------------------------------ fixtures
    def feed(self, events, **kw):
        with open(self.fx, "w") as f:
            json.dump({"events": events, **kw}, f)

    def reminders(self, items, recent=()):
        with open(self.rfx, "w") as f:
            json.dump({"now": NOW, "items": items, "recent": list(recent)}, f)

    def calendar(self, events, start="2026-03-02T00:00:00+01:00", end="2026-03-23T00:00:00+01:00"):
        with open(self.cal, "w") as f:
            json.dump({"version": 1, "fetched": NOW, "tz": "Europe/Zurich", "window": {"start": start, "end": end},
                       "events": events}, f)

    def run_tool(self, *args, rc=0, now=None):
        env = dict(self.env, PLANS_WATCH_NOW=now) if now else self.env
        r = subprocess.run([TOOL, *args], capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, rc, r.stdout + r.stderr)
        return r

    def usage_on(self):
        """Usage events into a temp spool; False when the tool under test can't import ~/bin/lib/usage.py (the CI
        overlay's lib has the ledger package but no usage.py, so the tool never falls back to $HOME/bin/lib)."""
        lib = os.path.join(os.path.dirname(os.path.realpath(TOOL)), "lib")
        if not os.path.isfile(os.path.join(lib, "usage.py")) and os.path.isdir(os.path.join(lib, "inbox")):
            return False
        hl = os.path.join(self.env["HOME"], "bin", "lib")
        os.makedirs(hl, exist_ok=True)
        if not os.path.exists(os.path.join(hl, "usage.py")):
            os.symlink(os.path.join(os.environ.get("USAGE_LIB") or os.path.expanduser("~/bin/lib"), "usage.py"),
                       os.path.join(hl, "usage.py"))
        self.env.pop("USAGE", None)
        self.env["USAGE_STATE"] = os.path.join(self.d, "usage")
        return True

    def usage_recs(self, kind):
        sp = os.path.join(self.env["USAGE_STATE"], "spool")
        out = []
        for n in sorted(os.listdir(sp)) if os.path.isdir(sp) else []:
            with open(os.path.join(sp, n)) as f:
                out += [r for r in map(json.loads, f) if r.get("f") == "bin.plans-watch" and r.get("k") == kind]
        return out

    def notes(self):
        return sorted(os.listdir(self.inbox)) if os.path.isdir(self.inbox) else []

    def note(self, i=-1):
        with open(os.path.join(self.inbox, self.notes()[i])) as f:
            return f.read()

    def ledger(self):
        with open(os.path.join(self.state, "ledger.json")) as f:
            return json.load(f)

    def keys(self, state=None):
        return [k for k, it in self.ledger()["items"].items() if state is None or it["state"] == state]

    # ------------------------------------------------------------ tests
    def test_new_plan_makes_one_private_note_once(self):
        self.feed([ev(5, [fact("Dinner at Pizzeria Roma", "2026-03-06", "19:30", venue="Pizzeria Roma")])])
        self.run_tool("run", "--cron")
        self.assertEqual(len(self.notes()), 1)
        text = self.note()
        self.assertIn("1 new", text)
        self.assertIn("## 📅 Plans", text)
        self.assertIn("Fri 06.03 19:30 · Dinner at Pizzeria Roma, Pizzeria Roma · Anna [chat 4711]", text)
        self.assertIn("calendar that day: free", text)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.inbox, self.notes()[0])).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.state, "ledger.json")).st_mode), 0o600)
        self.assertEqual(self.ledger()["cursor"], 5)
        self.run_tool("run", "--cron")                                 # nothing new: no second note
        self.assertEqual(len(self.notes()), 1)

    def test_usage_new_plans_are_a_healthy_run(self):
        # the usage store counts any exec/run outcome but ok as a failed run + an error: the state rides in attrs
        if not self.usage_on():
            self.skipTest("usage lib not reachable from the tool under test")
        self.feed([ev(5, [fact("Dinner at Pizzeria Roma", "2026-03-06", "19:30", venue="Pizzeria Roma")])])
        self.run_tool("run", "--cron")
        ex = self.usage_recs("exec")
        self.assertEqual([(r.get("o"), r.get("at", {}).get("state"), r.get("at", {}).get("new")) for r in ex],
                         [("ok", "new", 1)], ex)

    def test_repeat_mention_merges(self):
        self.feed([ev(5, [fact("Movie night", "2026-03-07")], ref="m1"),
                   ev(6, [fact("Movie night", "2026-03-07")], ref="m2")])
        self.run_tool("run", "--cron")
        self.assertEqual(len(self.keys()), 1)
        self.assertIn("2×", self.note())

    def test_calendar_title_word_marks_it_handled(self):
        self.calendar([{"start": "2026-03-06T19:00:00+01:00", "end": "2026-03-06T22:00:00+01:00", "all_day": False,
                        "title": "🍽️ Dinner Roma", "town": "Bern"},
                       {"start": "2026-03-12", "end": "2026-03-14", "all_day": True, "title": "🇳🇱 Visit (Utrecht)",
                        "town": "Utrecht"}])
        self.feed([ev(5, [fact("Dinner bei Roma", "2026-03-06", "19:30")]),
                   ev(6, [fact("Nederland", "2026-03-13", status="confirmed")], ref="m2")])
        self.run_tool("run", "--cron")
        self.assertEqual(self.notes(), [])
        st = {it["what"]: (it["state"], it.get("as"), it.get("match")) for it in self.ledger()["items"].values()}
        self.assertEqual(st["Dinner bei Roma"], ("done", "exists", "calendar"))
        self.assertEqual(st["Nederland"], ("done", "exists", "calendar"))        # flag emoji via the places file

    def test_same_time_other_title_is_context_not_a_match(self):
        self.calendar([{"start": "2026-03-06T19:30:00+01:00", "end": "2026-03-06T21:00:00+01:00", "all_day": False,
                        "title": "Yoga", "town": "Bern"}])
        self.feed([ev(5, [fact("Dinner bei Roma", "2026-03-06", "19:30")])])
        self.run_tool("run", "--cron")
        self.assertIn("calendar that day: Yoga", self.note())

    def test_beyond_the_cache_says_check(self):
        self.feed([ev(5, [fact("Cookie baking", "2026-04-11")], chat="group", chat_key="-100200", chat_type="group")])
        self.run_tool("run", "--cron")
        text = self.note()
        self.assertIn("## 👥 Group plans and invitations", text)
        self.assertIn("calendar not cached that far", text)

    def test_owner_reminder_covers_it_my_own_needs_two_words(self):
        self.feed([ev(5, [fact("Cookie baking at Ben", "2026-03-14")], chat="group", chat_key="-100200")])
        self.reminders([{"id": "a1", "at": "2026-03-12 18:00", "text": "🍪 Cookie dough for Saturday", "owner": True}])
        self.run_tool("run", "--cron")
        self.assertEqual(self.notes(), [])
        self.assertEqual(self.ledger()["items"][self.keys()[0]]["match"], "reminder")

    def test_my_own_reminder_with_one_shared_word_is_not_enough(self):
        self.feed([ev(5, [fact("Arrival in Lyon", "2026-03-14")])])
        self.reminders([{"id": "b2", "at": "2026-03-10 09:00", "text": "trip pack: Lyon forecast", "owner": False}])
        self.run_tool("run", "--cron")
        self.assertEqual(len(self.notes()), 1)

    def test_categories_sale_deadline_group_proposal(self):
        self.feed([
            ev(5, [fact("Pre-sale", "2026-03-05", "10:00"), fact("General sale", "2026-03-06", "10:00")],
               source="gmail", chat="Fest Tickets (fest.example)", chat_key="fest.example", chat_type="email",
               ref="main:1"),
            ev(6, [fact("pay the deposit", "2026-03-09", kind="todo")], source="gmail", chat="Hotel (hotel.example)",
               chat_key="hotel.example", chat_type="email", ref="main:2"),
            ev(7, [fact("BBQ", "2026-03-08", status="proposed")], chat="group", chat_key="-300", chat_type="group"),
            ev(8, [fact("Climbing", "2026-03-10", status="proposed")], chat="Ben", chat_key="99", ref="m9")])
        self.run_tool("run", "--cron")
        text = self.note()
        for head in ("## 🎟 Ticket sales and registrations", "## ⏰ Deadlines and to-dos",
                     "## 👥 Group plans and invitations", "## ❓ Proposals"):
            self.assertIn(head, text)
        self.assertIn("Mon 09.03 due · pay the deposit", text)
        self.assertIn("[mail main:1]", text)
        cats = {it["what"]: it["cat"] for it in self.ledger()["items"].values()}
        self.assertEqual(cats, {"Pre-sale": "sale", "General sale": "sale", "pay the deposit": "deadline",
                                "BBQ": "group", "Climbing": "proposal"})

    def test_itinerary_weekdays_are_read_from_the_message_dates(self):
        # message on Sat 28.02: "Thursday 19.3. night bus ... Friday morning arrival ... Saturday evening back";
        # the vault counted the weekdays from the message day (06.03, and 28.02 which is already past)
        facts = [fact("Night bus out", "2026-03-19", "21:30", date_text="Donnerstag, 19.3."),
                 fact("Arrival Lyon", "2026-03-06", "07:30", date_text="Freitagmorgen"),
                 fact("Bus back", "2026-02-28", "22:30", date_text="Samstagabend")]
        self.feed([ev(5, facts, ts="2026-02-28T19:54:00+01:00")])
        self.run_tool("run", "--cron")
        by = {it["what"]: it for it in self.ledger()["items"].values()}
        self.assertEqual(by["Night bus out"]["date"], "2026-03-19")
        self.assertIsNone(by["Night bus out"]["date_note"])
        self.assertEqual(by["Arrival Lyon"]["date"], "2026-03-20")
        self.assertEqual(by["Arrival Lyon"]["vault_date"], "2026-03-06")
        self.assertIn("vault said Fri 06.03", by["Arrival Lyon"]["date_note"])
        self.assertEqual(by["Bus back"]["date"], "2026-03-21")
        self.assertIn("Fri 20.03 07:30 · Arrival Lyon", self.note())

    def test_masked_day_and_day_number_and_swiss_weekday(self):
        facts = [fact("Leave Bern", "2026-03-18", "21:30", date_text="Mi, 18.3."),
                 fact("Arrive Lyon", "2026-03-05", date_text="Do, [amount].")]
        self.feed([ev(5, facts, source="gmail", chat="Ben", chat_key="mail.example", chat_type="email", ref="main:7",
                      ts="2026-03-01T09:00:00+01:00"),
                   ev(6, [fact("Night train home", "2026-03-06", date_text="Freitag, 13.")], source="gmail",
                      chat="Cleo", chat_key="other.example", chat_type="email", ref="main:8",
                      ts="2026-03-01T09:05:00+01:00"),
                   ev(7, [{"kind": "event", "what": "Show", "date": None, "date_text": "dunnstig", "start": "20:00",
                           "status": "proposed"}], chat="group", chat_key="-500", chat_type="group",
                      ts="2026-03-01T19:18:00+01:00"),
                   ev(8, [fact("Feedback to Dana", "2026-03-05", kind="todo", date_text="Freitag morgen")],
                      source="gmail", chat="Dana", chat_key="dana.example", chat_type="email", ref="main:9",
                      ts="2026-03-02T09:00:00+01:00")])
        self.run_tool("run", "--cron")
        by = {it["what"]: it for it in self.ledger()["items"].values()}
        self.assertEqual(by["Arrive Lyon"]["date"], "2026-03-19")                  # Thursday after Wed 18.3.
        self.assertEqual(by["Night train home"]["date"], "2026-03-13")             # Friday the 13th, not the 6th
        self.assertIn("date fixed", by["Night train home"]["date_note"])
        self.assertEqual(by["Show"]["date"], "2026-03-05")                         # next Thursday after Sun 1.3.
        self.assertEqual(by["Feedback to Dana"]["date"], "2026-03-06")             # the vault's Thursday -> Friday
        self.assertIn("check", by["Show"]["date_note"])

    def test_vault_anchored_weekday_is_taken_as_is(self):
        f = fact("Arrival Lyon", "2026-03-20", date_text="Freitagmorgen")
        f["anchored"] = True
        self.feed([ev(5, [fact("Night bus out", "2026-03-19", "21:30", date_text="Do 19.3."), f],
                      ts="2026-02-28T19:54:00+01:00")])
        self.run_tool("run", "--cron")
        by = {it["what"]: it for it in self.ledger()["items"].values()}
        self.assertEqual(by["Arrival Lyon"]["date"], "2026-03-20")
        self.assertIsNone(by["Arrival Lyon"]["date_note"])                          # no "check" flag

    def test_explicit_dates_and_past_and_far_are_left_alone(self):
        self.feed([ev(5, [fact("Concert", "2026-03-20", date_text="20.3.2026"),
                          fact("Old party", "2026-02-20", date_text="20.2."),
                          fact("Festival", "2026-12-20", date_text="20. Dezember")])])
        self.run_tool("run", "--cron")
        self.assertEqual([it["what"] for it in self.ledger()["items"].values()], ["Concert"])

    def test_ack_and_open_and_errors(self):
        self.feed([ev(5, [fact("Dinner", "2026-03-06")])])
        self.run_tool("run", "--cron")
        k = self.keys()[0]
        self.assertIn(k, self.run_tool("open").stdout)
        self.run_tool("done", "nope", "--as", "added", rc=1)
        self.run_tool("done", k, "--as", "maybe", rc=2)
        self.run_tool("done", k, "--as", "added", "--why", "booked")
        self.assertEqual(self.run_tool("open").stdout.strip(), "nothing")
        it = self.ledger()["items"][k]
        self.assertEqual((it["state"], it["as"], it["why"]), ("done", "added", "booked"))
        self.run_tool("reopen", k)
        self.assertIn(k, self.run_tool("open").stdout)
        self.assertIn("open 1", self.run_tool("status").stdout)
        self.assertEqual(json.loads(self.run_tool("list", "--all", "--json").stdout)[0]["key"], k)

    def test_unacked_plan_comes_back_after_20h_at_most_3_notes(self):
        self.feed([ev(5, [fact("Dinner", "2026-03-20")])])
        self.run_tool("run", "--cron")
        self.run_tool("run", "--cron", now="2026-03-02T20:00:00+01:00")          # 10 h: not yet
        self.assertEqual(len(self.notes()), 1)
        self.run_tool("run", "--cron", now="2026-03-03T07:00:00+01:00")          # 21 h: again
        self.assertEqual(len(self.notes()), 2)
        self.assertIn("still open", self.note())
        self.assertIn("(noted before, not acked yet)", self.note())
        self.run_tool("run", "--cron", now="2026-03-04T05:00:00+01:00")
        self.run_tool("run", "--cron", now="2026-03-05T03:00:00+01:00")          # would be the 4th: no
        self.assertEqual(len(self.notes()), 3)
        self.assertIn(self.keys()[0], self.run_tool("open", now="2026-03-05T03:00:00+01:00").stdout)

    def test_past_plan_expires(self):
        self.feed([ev(5, [fact("Dinner", "2026-03-03")])])
        self.run_tool("run", "--cron")
        self.run_tool("run", "--cron", now="2026-03-04T08:00:00+01:00")
        self.assertEqual(self.keys("expired"), self.keys())

    def test_cancelled_after_added_comes_back(self):
        self.feed([ev(5, [fact("Dinner at Roma", "2026-03-06")])])
        self.run_tool("run", "--cron")
        k = self.keys()[0]
        self.run_tool("done", k, "--as", "added")
        self.feed([ev(5, [fact("Dinner at Roma", "2026-03-06")]),
                   ev(6, [fact("Dinner at Roma", "2026-03-06", status="cancelled")], ref="m2")])
        self.run_tool("run", "--cron", now=LATER)
        self.assertEqual(len(self.notes()), 2)
        self.assertIn("## ❌ Cancelled after I added them", self.note())
        self.assertEqual(self.ledger()["items"][k]["state"], "cancelled")

    def test_changed_time_from_another_message_is_an_update(self):
        self.feed([ev(5, [fact("Train to Basel", "2026-03-06", "18:17")], ref="m1")])
        self.run_tool("run", "--cron")
        self.feed([ev(5, [fact("Train to Basel", "2026-03-06", "18:17")], ref="m1"),
                   ev(6, [fact("Train to Basel", "2026-03-06", "18:46")], ref="m2")])
        self.run_tool("run", "--cron", now=LATER)
        text = self.note()
        self.assertIn("## 🔄 Updated plans", text)
        self.assertIn("updated: 18:46 Train to Basel — new: time 18:46 · was 18:17", text)
        self.assertEqual(len(self.keys()), 1)                                      # one key per chat and day

    def test_changed_date_is_still_hinted(self):
        self.feed([ev(5, [fact("Train to Basel", "2026-03-06", "18:17")], ref="m1")])
        self.run_tool("run", "--cron")
        self.feed([ev(5, [fact("Train to Basel", "2026-03-06", "18:17")], ref="m1"),
                   ev(6, [fact("Train to Basel", "2026-03-07", "18:17")], ref="m2")])
        self.run_tool("run", "--cron", now=LATER)
        self.assertIn("changed? was Fri 06.03 18:17", self.note())

    def test_seed_and_dry_run_write_no_note(self):
        self.feed([ev(5, [fact("Dinner", "2026-03-06")])])
        r = self.run_tool("run", "--dry-run")
        self.assertIn("Dinner", r.stdout)
        self.assertEqual(self.notes(), [])
        self.assertFalse(os.path.exists(os.path.join(self.state, "ledger.json")))
        self.run_tool("run", "--seed")
        self.assertEqual(self.notes(), [])
        it = self.ledger()["items"][self.keys()[0]]
        self.assertEqual((it["state"], it["as"]), ("done", "seeded"))
        self.feed([ev(5, [fact("Dinner", "2026-03-06")]), ev(9, [fact("Lunch", "2026-03-07")], ref="m3")])
        self.run_tool("run", "--cron")
        self.assertEqual(len(self.notes()), 1)
        self.assertIn("Lunch", self.note())
        self.assertNotIn("Dinner", self.note())

    def test_vault_errors(self):
        self.feed([], fail=True, health=3)                                        # vault down: quiet, exit 0
        self.run_tool("run", "--cron", rc=0)
        self.feed([], fail=True, health=0)                                        # vault up but failing: exit 3
        r = self.run_tool("run", "--cron", rc=3)
        self.assertIn("HTTP 502", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.state, "ledger.json")))

    # ------------------------------------------------------------ updates, re-reads, clashes, titles
    def calls(self):
        p = self.fx + ".calls"
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return [json.loads(x) for x in f]

    TRAIN = {"start": "2026-03-02T18:14:00+01:00", "end": "2026-03-02T19:23:00+01:00", "all_day": False,
             "title": "🚆 Home → Zürich HB", "town": "Zürich", "free": False, "flags": {"travel": "train"},
             "train": {"from": "Home", "to": "Zürich HB"}}
    SHOW = {"start": "2026-03-02T19:00:00+01:00", "end": "2026-03-02T22:30:00+01:00", "all_day": False,
            "title": "Concert", "town": "Zürich", "free": False, "flags": {"travel": None}}

    def test_update_after_ack_comes_back_once_per_change(self):
        m1 = ev(5, [fact("Dinner", "2026-03-02", "18:00")], chat="Timo", ref="m1", ts="2026-03-02T09:00:00+01:00")
        self.feed([m1])
        self.run_tool("run", "--cron")
        k = self.keys()[0]
        self.run_tool("done", k, "--as", "asked")
        m2 = ev(6, [fact("Meeting", "2026-03-02", "17:45", venue="Zürich HB")], chat="Timo", ref="m2",
                ts="2026-03-02T10:30:00+01:00")
        self.feed([m1, m2])
        self.run_tool("run", "--cron", now=LATER)
        self.assertEqual(len(self.notes()), 2)
        text = self.note()
        self.assertIn("1 updated", text)
        self.assertIn(f"- `{k}` Mon 02.03 18:00 · Dinner · Timo [chat 4711]", text)
        self.assertIn("updated: 17:45 Meeting, Zürich HB — new: time 17:45, place Zürich HB · was 18:00, acked asked",
                      text)
        self.assertEqual(self.keys(), [k])
        self.assertIn(k, self.run_tool("open", now=LATER).stdout)
        self.run_tool("run", "--cron", now="2026-03-02T11:05:00+01:00")       # same facts again: no second note
        self.assertEqual(len(self.notes()), 2)
        self.run_tool("done", k, "--as", "added")
        it = self.ledger()["items"][k]
        self.assertTrue(it["upd"]["acked"])
        m3 = ev(7, [fact("Meeting", "2026-03-02", "17:45", venue="Zürich HB")], chat="Timo", ref="m3",
                ts="2026-03-02T11:10:00+01:00")
        self.feed([m1, m2, m3])
        self.run_tool("run", "--cron", now="2026-03-02T11:15:00+01:00")       # nothing new in it: stays quiet
        self.assertEqual(len(self.notes()), 2)
        m4 = ev(8, [fact("Meeting", "2026-03-02", "17:30")], chat="Timo", ref="m4", ts="2026-03-02T11:20:00+01:00")
        self.feed([m1, m2, m3, m4])
        self.run_tool("run", "--cron", now="2026-03-02T11:25:00+01:00")       # the next change: once more
        self.assertEqual(len(self.notes()), 3)
        self.assertIn("new: time 17:30 · was 18:00, acked added", self.note())

    def test_skipped_plan_is_not_followed_and_siblings_stay_apart(self):
        m1 = ev(5, [fact("Gym", "2026-03-04", "18:00")], ref="m1")
        self.feed([m1])
        self.run_tool("run", "--cron")
        self.run_tool("done", self.keys()[0], "--as", "skipped")
        m2 = ev(6, [fact("Dinner", "2026-03-04", "20:00"), fact("Drinks", "2026-03-04", "22:00")], ref="m2")
        self.feed([m1, m2])
        self.run_tool("run", "--cron", now=LATER)
        self.assertEqual(len(self.keys()), 3)
        self.assertIn("2 new", self.note())
        self.assertNotIn("Updated", self.note())

    def test_chat_reread_after_it_kept_talking(self):
        self.calendar([self.TRAIN, self.SHOW])
        usage = self.usage_on()
        m1 = ev(5, [fact("Plan", "2026-03-02", "18:00")], chat="Timo", ref="m1", ts="2026-03-02T09:00:00+01:00")
        self.feed([m1])
        self.run_tool("run", "--cron")
        k = self.keys()[0]
        self.run_tool("done", k, "--as", "asked")
        talk = [dict(ev(c, [], chat="Timo", ref=f"m{c}", ts=f"2026-03-02T10:{c + 24}:00+01:00"), facts=None)
                for c in (6, 7)]                                                # 10:30, 10:31, no facts
        self.feed([m1] + talk, job="running")
        self.run_tool("run", "--cron", now="2026-03-02T10:32:00+01:00")       # still talking: wait
        self.assertEqual(self.calls(), [])
        self.run_tool("run", "--cron", now="2026-03-02T10:40:00+01:00")       # quiet 9 min: re-read
        self.assertEqual(self.calls(), [["--async", "facts", "tg", "4711", "--since", "2026-03-02", "-n", "80"]])
        self.run_tool("run", "--cron", now="2026-03-02T10:45:00+01:00")       # job still running
        self.assertEqual(len(self.notes()), 1)
        self.assertIn("re-reads: 1 running, 0 waiting", self.run_tool("status").stdout)
        reread = {"events": [{"what": "Treffen", "date": "2026-03-02", "date_text": "hüt abig",
                              "start_text": "1745-1800", "venue": "züri hb", "who": ["Timo", "Mara"],
                              "status": "proposed"},
                             {"what": "reset wo hüt abig ablauft", "date": "2026-03-02", "venue": "Stadt"},
                             {"what": "Brunch", "date": "2026-03-08", "start_text": "11 Uhr"}],
                  "places": [{"name": "züri hb"}]}
        self.feed([m1] + talk, reread=reread)
        self.run_tool("run", "--cron", now="2026-03-02T10:50:00+01:00")
        self.assertEqual(len(self.notes()), 2)
        text = self.note()
        self.assertIn("updated: 17:45–18:00 Treffen, züri hb · Stadt — new: time 17:45, place züri hb, Stadt, "
                      "people Mara · was 18:00, acked asked", text)
        self.assertNotIn("ablauft", text)                                          # a fragment is never shown
        self.assertIn("⚠ clashes with 🚆 Home → Zürich HB 18:14; Concert 19:00", text)
        self.assertEqual(self.keys(), [k])                                         # 08.03 has no plan: ignored
        self.assertEqual(self.calls()[-1][:2], ["job", "wait"])
        led = self.ledger()
        self.assertNotIn("job", led["watch"]["4711"])
        n = len(self.calls())
        self.run_tool("run", "--cron", now="2026-03-02T11:30:00+01:00")       # no new messages: no re-read
        self.assertEqual(len(self.calls()), n)
        if usage:                                                       # re-read runs: outcome ok, state in attrs
            runs = self.usage_recs("run")
            self.assertTrue(runs and all(r.get("o") == "ok" for r in runs), runs)
            self.assertIn("updated", [r.get("at", {}).get("state") for r in runs], runs)

    def test_reread_limits_and_a_lost_job(self):
        m1 = ev(5, [fact("Plan", "2026-03-03", "18:00")], chat="Timo", ref="m1", ts="2026-03-02T09:00:00+01:00")
        talk = dict(ev(6, [], chat="Timo", ref="m6", ts="2026-03-02T09:30:00+01:00"), facts=None)
        self.feed([m1, talk], job="gone")
        self.run_tool("run", "--cron")                                           # plan + talk in one batch: re-read
        self.assertEqual(len([c for c in self.calls() if c[0] == "--async"]), 1)
        self.run_tool("run", "--cron", now="2026-03-02T10:05:00+01:00")       # the vault lost the job
        self.assertNotIn("job", self.ledger()["watch"]["4711"])
        self.assertIn("pending", self.ledger()["watch"]["4711"])
        self.run_tool("run", "--cron", now="2026-03-02T10:10:00+01:00")       # inside the 15 min gap: no new job
        self.assertEqual(len([c for c in self.calls() if c[0] == "--async"]), 1)
        self.run_tool("run", "--cron", now="2026-03-02T10:16:00+01:00")       # after it: again
        self.assertEqual(len([c for c in self.calls() if c[0] == "--async"]), 2)
        far = ev(9, [fact("Hike", "2026-03-20", "08:00")], chat="Ben", chat_key="99", ref="m9")
        chat = dict(ev(10, [], chat="Ben", chat_key="99", ref="m10", ts="2026-03-02T09:00:00+01:00"), facts=None)
        self.feed([m1, talk, far, chat], job="running")
        self.run_tool("run", "--cron", now="2026-03-02T10:40:00+01:00")       # 18 days ahead: not watched
        self.assertFalse(any(c[0] == "--async" and c[3] == "99" for c in self.calls()))

    def test_refresh_off_and_dry_run_start_no_job(self):
        m1 = ev(5, [fact("Plan", "2026-03-03", "18:00")], chat="Timo", ref="m1", ts="2026-03-02T09:00:00+01:00")
        talk = dict(ev(6, [], chat="Timo", ref="m6", ts="2026-03-02T09:10:00+01:00"), facts=None)
        self.feed([m1, talk])
        self.run_tool("run", "--dry-run")
        self.env["PLANS_WATCH_REFRESH"] = "off"
        self.run_tool("run", "--cron")
        self.assertEqual(self.calls(), [])

    def test_clash_and_own_event_and_travel_leg_place(self):
        self.calendar([self.TRAIN, self.SHOW,
                       {"start": "2026-03-03T18:00:00+01:00", "end": "2026-03-03T21:00:00+01:00", "all_day": False,
                        "title": "🍽️ Znacht mit Timo", "free": False, "flags": {}},
                       {"start": "2026-03-03T18:30:00+01:00", "end": "2026-03-03T19:00:00+01:00", "all_day": False,
                        "title": "Focus time", "free": True, "flags": {}}])
        self.feed([ev(5, [fact("Meet", "2026-03-02", "17:45", venue="Zürich HB")], chat="Timo", ref="m1"),
                   ev(6, [fact("Lunch", "2026-03-02", "12:00")], chat="Ben", chat_key="99", ref="m2"),
                   ev(7, [fact("reset wo hüt abig ablauft", "2026-03-03", "18:00")], chat="Timo", ref="m3")])
        self.run_tool("run", "--cron")
        text = self.note()
        self.assertIn("Meet, Zürich HB · Timo [chat 4711] · planned\n  ⚠ clashes with 🚆 Home → Zürich HB 18:14; "
                      "Concert 19:00 · calendar that day:", text)                 # the leg's place word is no match
        self.assertIn("Lunch · Ben [chat 99] · planned\n  calendar that day:", text)
        self.assertIn("Plan with Timo (title unclear) · Timo [chat 4711] · planned\n  ⚠ clashes with Focus time "
                      "18:30 · in calendar: 🍽️ Znacht mit Timo 18:00 · calendar that day:", text)   # "free" counts
        self.assertNotIn("ablauft", text)

    def test_fragment_titles_fall_back(self):
        self.feed([ev(5, [fact("mir gönd go esse", "2026-03-05", "19:00")], chat="Timo Muster", ref="m1"),
                   ev(6, [fact("Was isch hüt los", "2026-03-06")], chat="Crew", chat_key="-300", chat_type="group",
                      ref="m2"),
                   ev(7, [fact("Konzert heute Abend", "2026-03-07", "20:00"), fact("dinner tonight", "2026-03-08")],
                      chat="Ben", chat_key="99", ref="m3"),
                   ev(8, [fact("pay the deposit", "2026-03-09", kind="todo")], source="gmail", chat="Hotel (h.example)",
                      chat_key="h.example", chat_type="email", ref="main:1")])
        self.run_tool("run", "--cron")
        text = self.note()
        self.assertIn("Thu 05.03 19:00 · Plan with Timo (title unclear) · Timo Muster", text)
        self.assertIn("Fri 06.03 · Plan in Crew (title unclear) · Crew", text)
        for ok in ("Konzert heute Abend", "dinner tonight", "pay the deposit"):
            self.assertIn(ok, text)
        self.assertNotIn("gönd", text)
        rows = json.loads(self.run_tool("open", "--json").stdout)
        self.assertEqual({r["what"]: r["title"] for r in rows}["mir gönd go esse"], "Plan with Timo")
        self.assertIn("Plan with Timo", self.run_tool("list").stdout)

    def test_help_and_usage(self):
        self.assertIn("plans-watch run", self.run_tool("--help").stdout)
        self.run_tool("frobnicate", rc=2)


if __name__ == "__main__":
    unittest.main(verbosity=1)
