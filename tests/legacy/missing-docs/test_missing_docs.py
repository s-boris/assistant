#!/usr/local/bin/python3.13 -I
"""Tests for ~/bin/missing-docs. Run: python3 -I ~/opt/missing-docs-tests/test_missing_docs.py
Uses a fake paperless (MDOC_PAPERLESS), a fake remind (MDOC_REMIND), temp config/state/inbox and MDOC_NOW."""

import datetime as dt
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

SCRIPT = os.environ.get("MISSING_DOCS_BIN") or os.path.expanduser("~/bin/missing-docs")
REAL_CONFIG = os.path.expanduser("~/.config/missing-docs/expected.json")


def load_module():
    loader = importlib.machinery.SourceFileLoader("missing_docs", SCRIPT)
    spec = importlib.util.spec_from_loader("missing_docs", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


md = load_module()
D = dt.date


def doc(i, created, title="", type_="", tags=(), corr=""):
    return {
        "id": i,
        "created": D.fromisoformat(created),
        "title": title,
        "type": type_,
        "tags": list(tags),
        "correspondent": corr,
    }


PAYSLIP = {
    "key": "payslip",
    "title": "Lohnabrechnung",
    "cadence": "monthly",
    "due_days_after_period": 10,
    "period_from_title": True,
    "match": [{"types": ["Lohnabrechnung"]}],
    "active": True,
}
LOHNAUSWEIS = {
    "key": "lohnausweis",
    "title": "Lohnausweis",
    "cadence": "yearly",
    "due": "02-28",
    "window_start": "12-01",
    "match": [{"types": ["Lohnausweis"]}],
    "active": True,
}
P3A = {
    "key": "pillar3a-cert",
    "title": "3a",
    "cadence": "yearly",
    "due": "01-31",
    "window_start": "12-01",
    "match": [{"tags": ["3a"]}],
    "active": "auto",
    "guess": True,
}
BANK = {
    "key": "bank-yearend",
    "title": "Bank",
    "cadence": "yearly",
    "due": "02-28",
    "window_start": "12-20",
    "window_end": "03-31",
    "match": [{"types": ["Kontoauszug"]}],
    "active": "auto",
}
SERAFE = {
    "key": "serafe",
    "title": "Serafe",
    "cadence": "yearly",
    "due": "12-31",
    "window_start": "01-01",
    "profile_due": {"key": "serafe_invoice_date", "plus_days": 30},
    "match": [{"correspondents": ["Serafe"]}],
    "active": "auto",
}
CFG = {
    "defaults": {"alert_after_days": 14, "lookback": {"monthly": 3, "yearly": 2}},
    "expected": [PAYSLIP, LOHNAUSWEIS, P3A, BANK, SERAFE],
}


class Unit(unittest.TestCase):
    def test_title_period(self):
        tp = md.title_period
        self.assertEqual(tp("Lohnabrechnung für Juli 2024"), (2024, 7))
        self.assertEqual(tp("Lohnabrechnung für Marz 2024"), (2024, 3))
        self.assertEqual(tp("Lohnabrechnung März 2022"), (2022, 3))
        self.assertEqual(tp("Salary slip December 2025"), (2025, 12))
        self.assertEqual(tp("Lohn 2026-05"), (2026, 5))
        self.assertEqual(tp("Lohn 05.2026"), (2026, 5))
        self.assertIsNone(tp("Arbeitsvertrag"))
        self.assertIsNone(tp("Maibaum 2024 Fest"))  # no word boundary after 'mai'

    def test_group_match_and_or(self):
        e = {"match": [{"types": ["Rechnung"], "tags": ["Krankenkasse"]}, {"title_regex": "(?i)serafe"}]}
        self.assertTrue(md.matches(e, doc(1, "2026-01-01", type_="rechnung", tags=["Krankenkasse"])))
        self.assertFalse(md.matches(e, doc(2, "2026-01-01", type_="Rechnung", tags=["Steuern"])))
        self.assertTrue(md.matches(e, doc(3, "2026-01-01", title="SERAFE AG Rechnung")))

    def test_yearly_windows(self):
        occ = {o["period"]: o for o in md.yearly_occurrences(LOHNAUSWEIS, D(2026, 10, 13))}
        self.assertEqual(
            (occ["2026"]["start"], occ["2026"]["end"], occ["2026"]["due"]),
            (D(2025, 12, 1), D(2026, 12, 1), D(2026, 2, 28)),
        )
        b = {o["period"]: o for o in md.yearly_occurrences(BANK, D(2026, 10, 13))}["2026"]
        self.assertEqual((b["start"], b["end"]), (D(2025, 12, 20), D(2026, 4, 1)))
        s = {o["period"]: o for o in md.yearly_occurrences(SERAFE, D(2026, 10, 13))}["2026"]
        self.assertEqual((s["start"], s["end"], s["due"]), (D(2026, 1, 1), D(2027, 1, 1), D(2026, 12, 31)))
        self.assertEqual(md.mmdd(2027, "02-29"), D(2027, 2, 28))

    def test_payslip_overdue(self):
        docs = [
            doc(1, "2026-06-23", "Lohnabrechnung für Juni 2026", "Lohnabrechnung"),
            doc(2, "2026-09-02", "Lohnabrechnung August 2026", "Lohnabrechnung"),
        ]  # title month wins
        r = md.evaluate(PAYSLIP, docs, {}, CFG, D(2026, 10, 13))
        st = {o["period"]: o["status"] for o in r["occurrences"]}
        self.assertEqual(st, {"2026-10": "pending", "2026-09": "missing", "2026-08": "ok", "2026-07": "missing"})
        self.assertEqual([o["period"] for o in r["alert"]], ["2026-07"])  # Sep only 3 d late: no alert
        self.assertEqual(r["alert"][0]["late_days"], 64)

    def test_tracking_starts_at_first_doc(self):
        docs = [doc(1, "2026-04-23", "Lohnabrechnung April 2026", "Lohnabrechnung")]
        r = md.evaluate(PAYSLIP, docs, {}, CFG, D(2026, 5, 20))
        self.assertEqual({o["period"] for o in r["occurrences"]}, {"2026-05", "2026-04"})  # nothing before April

    def test_auto_activation(self):
        r = md.evaluate(P3A, [], {}, CFG, D(2027, 3, 1))
        self.assertFalse(r["active"])
        self.assertEqual(r["alert"], [])
        self.assertEqual(md.status_text(r), "off (auto: nothing filed yet)")
        docs = [doc(5, "2026-01-10", "Bescheinigung", "Kontoauszug", ["3a"])]
        r = md.evaluate(P3A, docs, {}, CFG, D(2027, 3, 1))
        self.assertTrue(r["active"])
        st = {o["period"]: o["status"] for o in r["occurrences"]}
        self.assertEqual(st, {"2027": "missing", "2026": "ok"})  # 2025 is before tracking started
        self.assertEqual(r["alert"][0]["late_days"], 29)

    def test_snooze_not_needed_waive(self):
        docs = [doc(1, "2026-05-23", "Lohnabrechnung Mai 2026", "Lohnabrechnung")]
        ref = D(2026, 10, 13)
        r = md.evaluate(PAYSLIP, docs, {"payslip": {"snoozed_until": "2026-10-20"}}, CFG, ref)
        self.assertEqual(r["alert"], [])
        self.assertIn("snoozed to 2026-10-20", md.status_text(r))
        r = md.evaluate(PAYSLIP, docs, {"payslip": {"snoozed_until": "2026-10-13"}}, CFG, ref)  # expired
        self.assertTrue(r["alert"])
        r = md.evaluate(PAYSLIP, docs, {"payslip": {"not_needed": True}}, CFG, ref)
        self.assertEqual((r["alert"], md.status_text(r)), ([], "not needed"))
        r = md.evaluate(PAYSLIP, docs, {"payslip": {"waived": ["2026-07", "2026-08"]}}, CFG, ref)
        self.assertEqual(r["alert"], [])

    def test_validate(self):
        self.assertEqual(md.validate(CFG), [])
        bad = {
            "expected": [
                dict(LOHNAUSWEIS, due="2-28"),
                dict(PAYSLIP, key="Pay Slip", match=[{}]),
                dict(P3A, key="lohnausweis", active="yes"),
            ]
        }
        errs = "\n".join(md.validate(bad))
        for frag in ("due must be MM-DD", "key must be", "empty match group", "duplicate key", "active must be"):
            self.assertIn(frag, errs)

    @unittest.skipUnless(
        os.path.exists(REAL_CONFIG),
        "no live ~/.config/missing-docs/expected.json (owner config, not part of a checkout)",
    )
    def test_real_config_valid(self):
        with open(REAL_CONFIG) as f:
            self.assertEqual(md.validate(json.load(f)), [])


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.inbox, self.state = os.path.join(t, "inbox"), os.path.join(t, "state")
        os.makedirs(self.inbox)
        self.docs_file, self.remind_log = os.path.join(t, "docs.json"), os.path.join(t, "remind.log")
        self.cfg = os.path.join(t, "expected.json")
        with open(self.cfg, "w") as f:
            json.dump(CFG, f)
        fake_pl = os.path.join(t, "paperless")
        with open(fake_pl, "w") as f:
            f.write(f"#!/bin/sh\n[ -f {t}/fail ] && {{ echo 'vault down' >&2; exit 3; }}\ncat {self.docs_file}\n")
        fake_rm = os.path.join(t, "remind")
        with open(fake_rm, "w") as f:
            f.write(f'#!/bin/sh\necho "$@" >> {self.remind_log}\n')
        os.chmod(fake_pl, 0o755)
        os.chmod(fake_rm, 0o755)
        self.env = dict(
            os.environ,
            MDOC_CONFIG=self.cfg,
            MDOC_STATE_DIR=self.state,
            MDOC_INBOX=self.inbox,
            MDOC_PAPERLESS=fake_pl,
            MDOC_REMIND=fake_rm,
            MDOC_PROFILE=os.path.join(t, "profile.json"),
            MDOC_NOW="2026-10-13",
        )
        self.set_docs(
            [
                {
                    "id": 1,
                    "created": "2026-06-23",
                    "title": "Lohnabrechnung für Juni 2026",
                    "document_type": "Lohnabrechnung",
                    "tags": ["Lohnabrechnung"],
                    "correspondent": None,
                },
                {
                    "id": 2,
                    "created": "2026-01-23",
                    "title": "Lohnausweis secret-title-xyz",
                    "document_type": "Lohnausweis",
                    "tags": [],
                    "correspondent": None,
                },
            ]
        )

    def tearDown(self):
        self.tmp.cleanup()

    def set_docs(self, docs):
        with open(self.docs_file, "w") as f:
            json.dump({"results": docs}, f)

    def run_md(self, *args, **env):
        return subprocess.run(
            [sys.executable, "-I", SCRIPT, *args], capture_output=True, text=True, env=dict(self.env, **env), timeout=60
        )

    def note(self, name="missing-docs.md"):
        p = os.path.join(self.inbox, name)
        if not os.path.exists(p):
            return None
        with open(p) as f:
            return f.read()

    def test_table(self):
        r = self.run_md()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("MISSING 2026-07, 2026-08, 2026-09 (64 d late)", r.stdout)
        self.assertIn("off (auto: nothing filed yet)", r.stdout)
        j = json.loads(self.run_md("--json", "--key", "lohnausweis").stdout)
        self.assertEqual([x["key"] for x in j["results"]], ["lohnausweis"])

    def test_cron_note_once_overdue(self):
        r = self.run_md("--cron")
        self.assertEqual(r.returncode, 0, r.stderr)
        n = self.note()
        self.assertIn("`payslip`", n)
        self.assertIn("2026-07, 2026-08 (64 d late)", n)
        self.assertNotIn("2026-09", n)  # only 3 d late
        for data in ("mdoc:filed:KEY", "mdoc:nn:KEY", "mdoc:snz:KEY"):
            self.assertIn(data, n)
        self.assertNotIn("secret-title-xyz", n)  # no document titles in the note
        self.assertEqual(os.listdir(self.inbox), ["missing-docs.md"])

    def test_cron_silent_when_fine(self):
        self.set_docs(
            [
                {
                    "id": i,
                    "created": f"2026-{m:02d}-23",
                    "title": f"Lohnabrechnung 2026-{m:02d}",
                    "document_type": "Lohnabrechnung",
                    "tags": [],
                    "correspondent": None,
                }
                for i, m in enumerate(range(6, 10), 1)
            ]
            + [
                {
                    "id": 20,
                    "created": "2026-01-23",
                    "title": "Lohnausweis",
                    "document_type": "Lohnausweis",
                    "tags": [],
                    "correspondent": None,
                }
            ]
        )
        r = self.run_md("--cron")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(os.listdir(self.inbox), [])

    def test_cron_failure_note(self):
        open(os.path.join(self.tmp.name, "fail"), "w").close()
        r = self.run_md("--cron")
        self.assertEqual(r.returncode, 1)
        self.assertIn("vault down", self.note("missing-docs-error.md"))
        self.assertIsNone(self.note())

    def test_snooze_then_cron_silent(self):
        r = self.run_md("--snooze", "payslip", "14")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("snoozed until 2026-10-27", r.stdout)
        with open(self.remind_log) as f:
            log = f.read()
        self.assertIn("cancel --tag mdoc:payslip", log)
        self.assertIn("add --tag mdoc:payslip 2026-10-27 09:30", log)
        self.run_md("--cron")
        self.assertIsNone(self.note())
        self.run_md("--cron", MDOC_NOW="2026-10-28")  # snooze over
        self.assertIsNotNone(self.note())

    def test_mark_not_needed_and_reset(self):
        r = self.run_md("--mark-not-needed", "payslip")
        self.assertIn("not needed for 2026-09, 2026-08, 2026-07", r.stdout)
        self.run_md("--cron")
        self.assertIsNone(self.note())
        self.run_md("--cron", MDOC_NOW="2026-11-30")  # October is a new period → alerts again
        self.assertIn("2026-10", self.note())
        self.run_md("--mark-not-needed", "payslip", "--forever")
        os.remove(os.path.join(self.inbox, "missing-docs.md"))
        self.run_md("--cron", MDOC_NOW="2026-11-30")
        self.assertIsNone(self.note())
        self.run_md("--reset", "payslip")
        self.run_md("--cron")
        self.assertIsNotNone(self.note())

    def test_unknown_key(self):
        r = self.run_md("--snooze", "nope", "3")
        self.assertEqual(r.returncode, 2)
        self.assertIn("unknown key", r.stderr)

    def test_profile_due(self):
        with open(self.env["MDOC_PROFILE"], "w") as f:
            json.dump({"serafe_invoice_date": "2025-04-15"}, f)
        self.set_docs(
            [
                {
                    "id": 9,
                    "created": "2025-04-10",
                    "title": "Rechnung",
                    "document_type": "Rechnung",
                    "tags": [],
                    "correspondent": "Serafe",
                }
            ]
        )
        j = json.loads(self.run_md("--json", "--key", "serafe", MDOC_NOW="2026-06-01").stdout)["results"][0]
        st = {o["period"]: (o["status"], o["due"]) for o in j["occurrences"]}
        self.assertEqual(st["2026"], ("missing", "2026-05-15"))
        self.assertEqual(st["2025"][0], "ok")
        self.assertEqual(j["alert"][0]["late_days"], 17)


if __name__ == "__main__":
    unittest.main(verbosity=1)
