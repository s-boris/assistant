#!/usr/bin/env python3
"""Deeper tests for ~/bin/deadlines (run.sh runs this after its own sections; also runs standalone).

Covers every subcommand's less-travelled paths: date edge cases (month ends, 29 Feb, DST days, today/tomorrow,
past due), empty or missing data/profile/ledger, paperless down / timing out / answering junk (via
fake_paperless.py), malformed data entries (validate reports them instead of crashing), privacy (no document
content or identifiers in checklist output; paperless is only asked tier-1 fields) and the --json shapes.

Fictional fixtures only (canton ZZ). Everything runs on temp state; DEADLINES_BIN picks the script under test.
"""

import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from datetime import date, datetime
from importlib.machinery import SourceFileLoader

HERE = os.path.dirname(os.path.abspath(__file__))
D = os.environ.get("DEADLINES_BIN") or os.path.expanduser("~/bin/deadlines")
FX_DATA = os.path.join(HERE, "fixtures", "ch-zz.json")
FX_PROF = os.path.join(HERE, "fixtures", "profile.json")
FAKE = os.path.join(HERE, "fake_paperless.py")
if not os.environ.get("ASSISTANT_PROFILE"):
    for cand in (
        os.path.join(HERE, "..", "profile", "fixtures", "profile.json"),
        os.path.join(HERE, "..", "profile-tests", "fixtures", "profile.json"),
    ):
        if os.path.exists(cand):
            os.environ["ASSISTANT_PROFILE"] = os.path.abspath(cand)
            break
T = tempfile.mkdtemp(prefix="deadlines-deep.")
TG = os.path.join(T, "tg")
with open(TG, "w") as f:
    f.write("#!/bin/sh\nexit 0\n")
os.chmod(TG, 0o755)
BASE_ENV = {
    "DEADLINES_DATA": FX_DATA,
    "DEADLINES_PROFILE": os.path.join(T, "profile.json"),
    "DEADLINES_STATE": os.path.join(T, "ledger", "synced.json"),
    "DEADLINES_PAPERLESS": FAKE,
    "DEADLINES_CANARY_STATE": os.path.join(T, "no-canary.json"),
    "DEADLINES_INBOX": os.path.join(T, "inbox"),
    "DEADLINES_VALIDATE_STATE": os.path.join(T, "vstate"),
    "FAKE_PAPERLESS_LOG": os.path.join(T, "paperless.log"),
    "REMIND_DB": os.path.join(T, "rem.json"),
    "REMIND_INBOX": os.path.join(T, "rinbox"),
    "REMIND_TG": TG,
}
for k in (
    "DEADLINES_TODAY",
    "DEADLINES_NOW",
    "REMIND_NOW",
    "FAKE_PAPERLESS_FAIL",
    "FAKE_PAPERLESS_HANG",
    "FAKE_PAPERLESS_GARBAGE",
    "FAKE_PAPERLESS_FIXTURE",
):
    os.environ.pop(k, None)
os.environ.update(BASE_ENV)


def reset_state():
    with open(FX_PROF) as f:
        prof = json.load(f)
    with open(BASE_ENV["DEADLINES_PROFILE"], "w") as f:
        json.dump(prof, f)
    for p in (BASE_ENV["REMIND_DB"], BASE_ENV["DEADLINES_STATE"], BASE_ENV["FAKE_PAPERLESS_LOG"]):
        if os.path.exists(p):
            os.remove(p)


def set_prof(**kv):
    p = BASE_ENV["DEADLINES_PROFILE"]
    with open(p) as f:
        d = json.load(f)
    d.update(kv)
    with open(p, "w") as f:
        json.dump(d, f)


def write_json(name, obj):
    p = os.path.join(T, name)
    with open(p, "w") as f:
        json.dump(obj, f) if not isinstance(obj, str) else f.write(obj)
    return p


def run(*args, timeout=60, **env):
    e = dict(os.environ)
    e.update({k: str(v) for k, v in env.items()})
    r = subprocess.run([D, *args], capture_output=True, text=True, timeout=timeout, env=e)
    return r.returncode, r.stdout, r.stderr


def load(**env):
    """A fresh copy of the script as a module, with env applied first (module constants read env at import)."""
    old = {k: os.environ.get(k) for k in env}
    os.environ.update({k: str(v) for k, v in env.items()})
    try:
        return SourceFileLoader(f"dl{id(env)}", D).load_module()
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class env:
    """Temporarily set environment variables (read at call time by now_local and the paperless subprocess)."""

    def __init__(self, **kv):
        self.kv = {k: str(v) for k, v in kv.items()}

    def __enter__(self):
        self.old = {k: os.environ.get(k) for k in self.kv}
        os.environ.update(self.kv)

    def __exit__(self, *exc):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


M = load()
LIST_KEYS = {"id", "due", "act_by", "days_left", "title", "category", "active", "note", "covered_by", "verified"}
CL_KEYS = {"id", "title", "status", "evidence", "active", "note", "from", "when", "why"}
STATUSES = {"found", "maybe", "missing", "n/a", "skipped", "error"}


class Base(unittest.TestCase):
    def setUp(self):
        reset_state()

    def assertClean(self, rc, err, want_rc):
        self.assertEqual(rc, want_rc, err)
        self.assertNotIn("Traceback", err)


class Subcommands(Base):
    def test_no_subcommand_is_list(self):
        rc, out, err = run(DEADLINES_TODAY="2026-11-01")
        self.assertClean(rc, err, 0)
        self.assertTrue(out.startswith("Deadlines (CH / canton ZZ"), out[:80])

    def test_unknown_subcommand_is_usage_error(self):
        rc, _, err = run("frobnicate")
        self.assertEqual(rc, 2)
        self.assertIn("invalid choice", err)

    def test_show_dated_item(self):
        rc, out, err = run("show", "tax-return", DEADLINES_TODAY="2027-03-01")
        self.assertClean(rc, err, 0)
        self.assertIn("date rule: 03-31", out)
        self.assertIn("next: Wed 2027-03-31, in 30 days", out)
        self.assertIn("sync reminders: 24 d before, 7 d before at 09:00", out)
        self.assertIn("source:", out)
        self.assertIn("moves to the next working day", out)

    def test_show_amounts_and_placeholders(self):
        rc, out, err = run("show", "pillar3a", DEADLINES_TODAY="2026-11-01")
        self.assertClean(rc, err, 0)
        self.assertIn("amounts: ", out)
        self.assertNotIn("{amount}", out)
        self.assertNotIn("{year}", out)

    def test_show_covered_item_has_no_sync_line(self):
        rc, out, err = run("show", "kk-switch", DEADLINES_TODAY="2026-11-01")
        self.assertClean(rc, err, 0)
        self.assertIn("handled by: kk-compare", out)
        self.assertNotIn("sync reminders", out)

    def test_show_profile_dated_item(self):
        set_prof(w8ben_signed="2024-05-10")
        rc, out, err = run("show", "w8ben", DEADLINES_TODAY="2026-11-01")
        self.assertClean(rc, err, 0)
        self.assertIn("date from profile: w8ben_signed = '2024-05-10'", out)
        self.assertIn("31 Dec of that year", out)

    def test_show_unknown_id_lists_items(self):
        rc, _, err = run("show", "nope")
        self.assertClean(rc, err, 1)
        self.assertIn("no item 'nope'", err)
        self.assertIn("tax-return", err)

    def test_list_zero_days_none(self):
        rc, out, err = run("list", "--days", "0", DEADLINES_TODAY="2026-10-20")
        self.assertClean(rc, err, 0)
        self.assertIn("(none)", out)

    def test_list_all_shows_unknown_dates(self):
        rc, out, err = run("list", "--all", DEADLINES_TODAY="2026-10-20")
        self.assertClean(rc, err, 0)
        self.assertIn("Date unknown", out)
        self.assertIn('[profile "halbtax_expiry"]', out)
        rc, out2, _ = run("list", DEADLINES_TODAY="2026-10-20")
        self.assertNotIn("Date unknown", out2)
        self.assertIn("--all adds", out2)

    def test_validate_strict_fixture(self):
        rc, out, err = run("validate", "--strict", DEADLINES_TODAY="2026-10-15")
        self.assertIn(rc, (0, 1))
        self.assertNotIn("Traceback", err)
        self.assertEqual(rc == 0, "warn:" not in out, out)


class JsonShapes(Base):
    def test_list_json(self):
        rc, out, err = run("list", "--json", DEADLINES_TODAY="2026-10-20")
        self.assertClean(rc, err, 0)
        rows = json.loads(out)
        self.assertTrue(rows)
        for r in rows:
            self.assertEqual(set(r), LIST_KEYS)
            date.fromisoformat(r["due"])
            date.fromisoformat(r["act_by"])
            self.assertGreaterEqual(r["days_left"], 0)
            self.assertTrue(r["active"])
            self.assertNotIn("{", r["title"])
        self.assertEqual([r["act_by"] for r in rows], sorted(r["act_by"] for r in rows))
        self.assertEqual(rows[0]["days_left"], (date.fromisoformat(rows[0]["act_by"]) - date(2026, 10, 20)).days)

    def test_list_json_all_marks_inactive(self):
        p = write_json(
            "ifdata.json",
            {
                "deadlines": [
                    {"id": "cr", "title": "Crypto list", "date": "12-31", "if": "crypto"},
                    {"id": "kk", "title": "KK", "date": "11-30", "covered_by": "kk-compare"},
                ]
            },
        )
        rc, out, err = run("list", "--json", DEADLINES_DATA=p, DEADLINES_TODAY="2026-10-20")
        self.assertEqual([r["id"] for r in json.loads(out)], ["kk"])
        rc, out, err = run("list", "--json", "--all", DEADLINES_DATA=p, DEADLINES_TODAY="2026-10-20")
        rows = {r["id"]: r for r in json.loads(out)}
        self.assertFalse(rows["cr"]["active"])
        self.assertEqual(rows["cr"]["note"], 'if applicable: profile "crypto" unknown')
        self.assertEqual(rows["kk"]["covered_by"], "kk-compare")
        set_prof(crypto=False)
        rc, out, err = run("list", "--json", "--all", DEADLINES_DATA=p, DEADLINES_TODAY="2026-10-20")
        self.assertIn("= no", {r["id"]: r for r in json.loads(out)}["cr"]["note"])

    def test_list_json_empty(self):
        p = write_json("empty.json", {"updated": "2026-01-01", "deadlines": []})
        rc, out, err = run("list", "--json", DEADLINES_DATA=p)
        self.assertClean(rc, err, 0)
        self.assertEqual(json.loads(out), [])

    def test_checklist_json(self):
        rc, out, err = run("checklist", "tax", "2026", "--json", DEADLINES_TODAY="2027-02-25")
        self.assertClean(rc, err, 0)
        d = json.loads(out)
        self.assertEqual(set(d), {"tax_year", "checked", "items"})
        self.assertEqual(d["tax_year"], 2026)
        self.assertEqual(d["checked"], "2027-02-25")
        for it in d["items"]:
            self.assertEqual(set(it), CL_KEYS)
            self.assertIn(it["status"], STATUSES)
            self.assertIsInstance(it["active"], bool)
        st = {it["id"]: it["status"] for it in d["items"]}
        self.assertEqual(st["tax-form"], "n/a")
        self.assertEqual(st["lohnausweis"], "found")

    def test_checklist_json_offline(self):
        rc, out, err = run("checklist", "tax", "2026", "--json", "--offline")
        d = json.loads(out)
        self.assertTrue(all(it["status"] == "skipped" for it in d["items"]))
        self.assertFalse(os.path.exists(BASE_ENV["FAKE_PAPERLESS_LOG"]), "offline still called paperless")


class Dates(Base):
    def rows(self, today, *extra):
        rc, out, err = run("list", "--json", *extra, DEADLINES_TODAY=today)
        self.assertClean(rc, err, 0)
        return {r["id"]: r for r in reversed(json.loads(out))}  # first occurrence per id wins

    def test_today_is_zero_days_left(self):
        self.assertEqual(self.rows("2026-12-31")["pillar3a"]["days_left"], 0)

    def test_tomorrow_is_one_day_left(self):
        self.assertEqual(self.rows("2026-12-30")["pillar3a"]["days_left"], 1)

    def test_past_due_drops_to_next_year(self):
        r = self.rows("2027-01-01")["pillar3a"]
        self.assertEqual(r["act_by"], "2027-12-31")
        self.assertEqual(r["days_left"], 364)

    def test_now_local_overrides(self):
        with env(DEADLINES_TODAY="2026-03-29"):
            self.assertEqual(M.now_local(), datetime(2026, 3, 29, 7, 0))
        with env(DEADLINES_NOW="2026-10-25 02:30", DEADLINES_TODAY="2026-01-01"):  # NOW wins
            self.assertEqual(M.now_local(), datetime(2026, 10, 25, 2, 30))
        self.assertIsNone(M.now_local().tzinfo)

    def test_add_months_edges(self):
        self.assertEqual(M.add_months(date(2028, 3, 31), -1), date(2028, 2, 29))
        self.assertEqual(M.add_months(date(2027, 1, 31), 1), date(2027, 2, 28))
        self.assertEqual(M.add_months(date(2026, 12, 15), 1), date(2027, 1, 15))
        self.assertEqual(M.add_months(date(2026, 8, 31), -14), date(2025, 6, 30))

    def test_shift_edges(self):
        self.assertEqual(M.shift(date(2028, 1, 1), "after"), date(2028, 1, 3))  # Sat + Berchtoldstag Sun
        self.assertEqual(M.shift(date(2027, 1, 2), "before"), date(2026, 12, 31))  # back over New Year
        self.assertEqual(M.shift(date(2027, 3, 26), "before"), date(2027, 3, 25))  # Good Friday
        self.assertEqual(M.shift(date(2026, 11, 29), None), date(2026, 11, 29))  # Sunday, no rule
        self.assertEqual(M.shift(date(2026, 11, 30), "before"), date(2026, 11, 30))  # working day unchanged

    def test_mmdd_29_feb_clamps_in_common_years(self):
        it = {"id": "leap", "title": "L", "date": "02-29"}
        occ = M.occurrences(it, {}, date(2027, 1, 1), date(2028, 12, 31))
        self.assertEqual([o["due"] for o in occ], [date(2027, 2, 28), date(2028, 2, 29)])

    def test_repeat_anniversary_on_29_feb(self):
        it = {"id": "a", "title": "A", "date_from_profile": "k", "repeat_years": True}
        occ = M.occurrences(it, {"k": "2028-02-29"}, date(2029, 1, 1), date(2031, 12, 31))
        self.assertEqual([o["due"] for o in occ], [date(2029, 2, 28), date(2030, 2, 28), date(2031, 2, 28)])

    def test_window_end_29_feb_and_year_wrap(self):
        self.assertIn("28 Feb 2027", M.window_txt({"window_end": "02-29"}, {"year": 2027, "due": date(2027, 1, 20)}))
        self.assertEqual(
            M.window_txt({"window_end": "01-31"}, {"year": 2026, "due": date(2026, 12, 1)}), "01 Dec – 31 Jan 2027"
        )

    def test_one_off_date_and_years_filter(self):
        it = {"id": "o", "title": "O", "date": "2027-05-03"}
        self.assertEqual(len(M.occurrences(it, {}, date(2026, 1, 1), date(2028, 12, 31))), 1)
        it = {"id": "y", "title": "Y", "date": "06-01", "years": [2027]}
        self.assertEqual([o["year"] for o in M.occurrences(it, {}, date(2026, 1, 1), date(2028, 12, 31))], [2027])

    def test_bad_profile_date_is_ignored(self):
        it = {"id": "p", "title": "P", "date_from_profile": "k"}
        self.assertEqual(M.occurrences(it, {"k": "soon"}, date(2026, 1, 1), date(2027, 1, 1)), [])
        set_prof(halbtax_expiry="next spring")
        rc, out, err = run("list", "--all", DEADLINES_TODAY="2026-10-20")
        self.assertClean(rc, err, 0)

    def test_dst_days_keep_wall_clock_time(self):
        data = {
            "default_time": "09:00",
            "deadlines": [
                {"id": "spring", "title": "S", "date": "03-30", "remind": [{"before": 1}]},
                {"id": "autumn", "title": "A", "date": "10-26", "remind": [{"before": 1}]},
            ],
        }
        want = M.plan(data, {}, datetime(2026, 3, 1, 12, 0), 300)
        self.assertEqual(want["deadline:spring:2026-03-30:1d"]["at"], "2026-03-29 09:00")  # DST starts
        self.assertEqual(want["deadline:autumn:2026-10-26:1d"]["at"], "2026-10-25 09:00")  # DST ends

    def test_next_slot_on_dst_day(self):
        self.assertEqual(M.next_slot(datetime(2026, 10, 25, 8, 30), "09:00"), datetime(2026, 10, 26, 9, 0))
        self.assertEqual(M.next_slot(datetime(2026, 3, 29, 7, 0), "09:00"), datetime(2026, 3, 29, 9, 0))

    def test_reminder_text_today_and_singular(self):
        it = {"id": "x", "title": "Pay {year}", "action": "Do it."}
        o = {"act_by": date(2026, 12, 31), "due": date(2026, 12, 31), "year": 2026}
        self.assertEqual(M.reminder_text(it, o, 0, {}), "📅 Pay 2026: Thu 31 Dec (today). Do it.")
        self.assertIn("(in 1 day)", M.reminder_text(it, o, 1, {}))
        self.assertIn("(in 2 days)", M.reminder_text(it, o, 2, {}))
        long = dict(it, action="word " * 80)
        self.assertLessEqual(len(M.reminder_text(long, o, 3, {})), 400)
        self.assertTrue(M.reminder_text(long, o, 3, {}).endswith("…"))

    def test_render_placeholders(self):
        it = {"amounts": {"2026": "CHF 1"}, "vars": {"who": "ZZ office"}}
        self.assertEqual(
            M.render("{amount} {prev}-{next} {who} {unknown}", it, 2026), "CHF 1 2025-2027 ZZ office {unknown}"
        )
        self.assertIn("amount for 2027 not in data yet", M.render("{amount}", it, 2027))

    def test_plan_lead_zero_and_late_nudge(self):
        data = {"deadlines": [{"id": "z", "title": "Z", "date": "12-31", "remind": [{"before": 30}, {"before": 0}]}]}
        want = M.plan(data, {}, datetime(2026, 12, 20, 12, 0), 30)
        self.assertEqual(sorted(want), ["deadline:z:2026-12-31:0d"])
        data["deadlines"][0]["remind"] = [{"before": 30}]
        want = M.plan(data, {}, datetime(2026, 12, 20, 12, 0), 30)
        self.assertEqual(list(want), ["deadline:z:2026-12-31:late"])
        self.assertEqual(want["deadline:z:2026-12-31:late"]["at"], "2026-12-21 09:00")
        self.assertEqual(M.plan(data, {}, datetime(2026, 12, 20, 12, 0), 30, created={"deadline:z:2026-12-31:30d"}), {})


class StateAndFiles(Base):
    def test_missing_data_file(self):
        rc, _, err = run("list", DEADLINES_DATA=os.path.join(T, "nope.json"))
        self.assertClean(rc, err, 1)
        self.assertIn("deadlines: missing", err)

    def test_data_without_deadlines_list(self):
        p = write_json("nolist.json", {"updated": "2026-01-01"})
        for cmd in (["list"], ["show", "x"], ["sync", "--dry-run"]):
            rc, _, err = run(*cmd, DEADLINES_DATA=p)
            self.assertClean(rc, err, 1)
            self.assertIn("'deadlines' must be a list", err)
        rc, out, err = run("validate", DEADLINES_DATA=p)
        self.assertClean(rc, err, 1)
        self.assertIn("ERROR: no 'deadlines' list", out)

    def test_data_not_json_object_or_invalid(self):
        rc, _, err = run("list", DEADLINES_DATA=write_json("arr.json", []))
        self.assertClean(rc, err, 1)
        rc, _, err = run("list", DEADLINES_DATA=write_json("bad.json", '{"deadlines": ['))
        self.assertClean(rc, err, 1)
        self.assertIn("not valid JSON", err)

    def test_missing_profile_means_unknown(self):
        rc, out, err = run(
            "list", "--all", DEADLINES_PROFILE=os.path.join(T, "noprof.json"), DEADLINES_TODAY="2026-10-20"
        )
        self.assertClean(rc, err, 0)
        self.assertIn('[profile "w8ben_signed"]', out)
        rc, _, err = run("list", DEADLINES_PROFILE=write_json("arrprof.json", []))
        self.assertClean(rc, err, 1)

    def test_empty_deadlines(self):
        p = write_json("empty.json", {"updated": "2026-01-01", "deadlines": []})
        rc, out, err = run("list", DEADLINES_DATA=p)
        self.assertIn("(none)", out)
        rc, out, err = run("sync", DEADLINES_DATA=p, DEADLINES_TODAY="2026-10-20")
        self.assertClean(rc, err, 0)
        self.assertIn("nothing to change", out)
        rc, out, err = run("validate", DEADLINES_DATA=p)
        self.assertClean(rc, err, 0)

    def test_sync_creates_ledger_dir_and_quiet_rerun(self):
        self.assertFalse(
            os.path.exists(os.path.dirname(BASE_ENV["DEADLINES_STATE"])) and os.path.exists(BASE_ENV["DEADLINES_STATE"])
        )
        rc, out, err = run("sync", DEADLINES_NOW="2026-10-20 10:00")
        self.assertClean(rc, err, 0)
        self.assertIn("added", out)
        with open(BASE_ENV["DEADLINES_STATE"]) as f:
            self.assertTrue(json.load(f)["created"])
        rc, out, err = run("sync", "--quiet", DEADLINES_NOW="2026-10-20 10:00")
        self.assertClean(rc, err, 0)
        self.assertEqual(out, "")

    def test_sync_prunes_old_ledger_entries(self):
        os.makedirs(os.path.dirname(BASE_ENV["DEADLINES_STATE"]), exist_ok=True)
        write_json(
            "ledger/synced.json",
            {"created": {"deadline:old:2024-03-31:7d": "2024-03-01 09:00", "deadline:odd": "2026-01-01 09:00"}},
        )
        rc, out, err = run("sync", DEADLINES_NOW="2026-10-20 10:00")
        self.assertClean(rc, err, 0)
        with open(BASE_ENV["DEADLINES_STATE"]) as f:
            created = json.load(f)["created"]
        self.assertNotIn("deadline:old:2024-03-31:7d", created)
        self.assertIn("deadline:odd", created)  # unparseable tags are kept, never crash the prune

    def test_corrupt_ledger_is_a_clean_error(self):
        os.makedirs(os.path.dirname(BASE_ENV["DEADLINES_STATE"]), exist_ok=True)
        write_json("ledger/synced.json", "")
        rc, _, err = run("sync", DEADLINES_NOW="2026-10-20 10:00")
        self.assertClean(rc, err, 1)
        self.assertIn("not valid JSON", err)
        self.assertFalse(os.path.exists(BASE_ENV["REMIND_DB"]), "sync touched reminders despite a bad ledger")

    def test_remind_failing(self):
        rc, _, err = run("sync", DEADLINES_REMIND="/bin/false", DEADLINES_NOW="2026-10-20 10:00")
        self.assertClean(rc, err, 1)
        self.assertIn("`remind list` failed", err)

    def test_dry_run_writes_nothing(self):
        rc, out, err = run("sync", "--dry-run", DEADLINES_NOW="2026-10-20 10:00")
        self.assertClean(rc, err, 0)
        self.assertIn("would add", out)
        self.assertFalse(os.path.exists(BASE_ENV["DEADLINES_STATE"]))
        self.assertFalse(os.path.exists(BASE_ENV["REMIND_DB"]))


class Paperless(Base):
    def calls(self):
        try:
            with open(BASE_ENV["FAKE_PAPERLESS_LOG"]) as f:
                return f.read().splitlines()
        except FileNotFoundError:
            return []

    def test_down_json(self):
        rc, out, err = run("checklist", "tax", "2026", "--json", FAKE_PAPERLESS_FAIL=1)
        self.assertClean(rc, err, 0)
        items = json.loads(out)["items"]
        errs = [i for i in items if i["status"] == "error"]
        self.assertTrue(errs)
        self.assertTrue(all("paperless unavailable" in i["evidence"] for i in errs))

    def test_binary_missing(self):
        rc, out, err = run("checklist", "tax", "2026", DEADLINES_PAPERLESS=os.path.join(T, "no-paperless"))
        self.assertClean(rc, err, 0)
        self.assertIn("[!]", out)
        self.assertIn("paperless unavailable", out)

    def test_timeout_gives_up_after_first(self):
        m = load()
        real = subprocess.run

        def short(*a, **kw):
            kw["timeout"] = 1
            return real(*a, **kw)

        m.subprocess = types.SimpleNamespace(run=short, TimeoutExpired=subprocess.TimeoutExpired)
        os.environ["FAKE_PAPERLESS_HANG"] = "5"
        try:
            import contextlib
            import io

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = m.cmd_checklist(types.SimpleNamespace(kind="tax", year=2026, offline=False, json=True, md=False))
        finally:
            os.environ.pop("FAKE_PAPERLESS_HANG")
        self.assertEqual(rc, 0)
        items = json.loads(buf.getvalue())["items"]
        self.assertTrue(any("timed out" in i["evidence"] for i in items if i["status"] == "error"))
        self.assertEqual(len(self.calls()), 1)

    def test_garbage_answers(self):
        rc, out, err = run("checklist", "tax", "2026", "--json", FAKE_PAPERLESS_GARBAGE=1)
        self.assertClean(rc, err, 0)
        st = {i["status"] for i in json.loads(out)["items"]}
        self.assertLessEqual(st, {"missing", "n/a"})

    def test_paperless_fields_junk(self):
        m = load()
        with env(FAKE_PAPERLESS_GARBAGE="1"):
            self.assertEqual(m.paperless_fields(101), {})
        self.assertEqual(m.period_years({"period_years": ["x", None, 1990, "2026", 2027, 2028]}), [2026, 2027])


class Privacy(Base):
    def test_no_content_or_identifiers_in_checklist(self):
        # planted, obviously fake identifiers, built at runtime (never stored in the repo)
        iban = "CH" + "00 " + " ".join(["0000"] * 4) + " 0"
        ahv = "756" + ".0000.0000.00"
        policy = "Police " + "Nr. TEST-" + "000111"
        with open(os.path.join(HERE, "fixture.json")) as f:
            docs = json.load(f)
        for d in docs:
            d["content"] += f"\nIBAN {iban}\nAHV {ahv}\n{policy}\nSECRET-BODY-MARKER"
        fx = write_json("planted.json", docs)
        outs = [
            run("checklist", "tax", "2026", *x, FAKE_PAPERLESS_FIXTURE=fx, DEADLINES_TODAY="2027-02-25")
            for x in ([], ["--json"], ["--md"])
        ]
        for rc, out, err in outs:
            self.assertClean(rc, err, 0)
            for s in (iban, ahv, policy, "SECRET-BODY-MARKER", "Saldovortrag", "Zins brutto"):
                self.assertNotIn(s, out + err)
        with open(BASE_ENV["FAKE_PAPERLESS_LOG"]) as f:
            calls = f.read().splitlines()
        self.assertTrue(calls)
        for c in calls:
            verb = c.split()[0]
            self.assertIn(verb, ("search", "fields"), c)
            self.assertNotIn("--content", c)
            if verb == "fields":
                self.assertTrue(c.endswith("--fields " + M.FIELDS), c)

    def test_list_and_show_no_doc_lookup(self):
        run("list", "--all")
        run("show", "tax-return")
        self.assertFalse(os.path.exists(BASE_ENV["FAKE_PAPERLESS_LOG"]))


class Malformed(Base):
    def validate(self, items, **extra):
        p = write_json("mal.json", {"updated": "2026-10-01", "deadlines": items, **extra})
        rc, out, err = run(
            "validate", DEADLINES_DATA=p, DEADLINES_PROFILE=write_json("p0.json", {}), DEADLINES_TODAY="2026-10-15"
        )
        self.assertNotIn("Traceback", err)
        return rc, out

    def ok_item(self, **kv):
        return {"id": "i", "title": "T", "date": "12-31", "sources": [{"url": "https://www.example.org/"}], **kv}

    def test_good_item_passes(self):
        rc, out = self.validate([self.ok_item()])
        self.assertEqual(rc, 0, out)

    def test_each_malformed_entry_is_reported(self):
        cases = [
            (self.ok_item(date=1231), "date must be a string"),
            (self.ok_item(date="31.12."), "bad date '31.12.'"),
            (self.ok_item(date="02-30"), "impossible date '02-30'"),
            (self.ok_item(date=None), "needs date or date_from_profile"),
            (self.ok_item(title=""), "no title"),
            (self.ok_item(remind=[7]), "remind.before must be a non-negative int"),
            (self.ok_item(remind=[{"before": -1}]), "remind.before must be a non-negative int"),
            (self.ok_item(remind=[{"before": "7"}]), "remind.before must be a non-negative int"),
            (self.ok_item(remind=7), "remind.before must be a non-negative int"),
            (self.ok_item(sources=["https://www.example.org/"]), "sources must be a list of objects"),
            (self.ok_item(id=""), "item without id"),
        ]
        for item, want in cases:
            with self.subTest(want=want, item=item):
                rc, out = self.validate([item])
                self.assertEqual(rc, 1, out)
                self.assertIn(want, out)

    def test_non_object_entry_and_duplicates(self):
        rc, out = self.validate(["oops", self.ok_item(), self.ok_item()])
        self.assertEqual(rc, 1)
        self.assertIn("deadlines entries #0 are not objects", out)
        self.assertIn("duplicate deadline ids", out)

    def test_bad_checklist_regex(self):
        rc, out = self.validate(
            [self.ok_item()],
            checklists={
                "tax": {
                    "title": "t",
                    "items": [{"id": "c", "title": "C", "paperless": {"queries": ["x"], "confirm": "(unclosed"}}],
                }
            },
        )
        self.assertEqual(rc, 1)
        self.assertIn("checklist tax/c: bad regex", out)

    def test_cron_reports_malformed_without_crash(self):
        p = write_json("mal2.json", {"updated": "2026-10-01", "deadlines": [self.ok_item(date=5)]})
        rc, out, err = run("validate", "--cron", DEADLINES_DATA=p, DEADLINES_TODAY="2026-10-15")
        self.assertClean(rc, err, 0)
        self.assertEqual(out, "")
        with open(os.path.join(BASE_ENV["DEADLINES_INBOX"], "deadlines-validate-2026-10-15.md")) as f:
            self.assertIn("ERROR: i: date must be a string", f.read())

    def test_list_refuses_item_without_id(self):
        p = write_json("noid.json", {"deadlines": [{"title": "T", "date": "12-31"}]})
        rc, _, err = run("list", DEADLINES_DATA=p)
        self.assertClean(rc, err, 1)
        self.assertIn("run `deadlines validate`", err)

    def test_unknown_checklist(self):
        rc, _, err = run("checklist", "nope", "2026")
        self.assertClean(rc, err, 1)
        self.assertIn("no checklist 'nope'", err)


if __name__ == "__main__":
    try:
        res = unittest.main(exit=False, verbosity=1).result
    finally:
        subprocess.run(["rm", "-rf", "--", T])
    n = res.testsRun
    bad = len(res.failures) + len(res.errors)
    print(f"test_deep: {n - bad} passed, {bad} failed")
    sys.exit(1 if bad else 0)
