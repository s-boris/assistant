#!/usr/bin/env python3
"""Tests for ~/bin/mail-migration-check (or $MMC_SCRIPT) against fake-vault: report from the vault's aggregates,
not-deployed and still-running paths, the --job / --from-json / --quick modes, bounded waits, the address guard,
config defaults. Temp dirs only (MMC_HOME); nothing touches the real vault or ~/reports."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = os.environ.get("MMC_SCRIPT") or str(HERE.parent.parent.parent / "scripts" / "mail-migration-check")
FIX = HERE / "fixtures" / "compare.json"


class MMC(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.log = self.home / "calls.log"

    def tearDown(self):
        self.tmp.cleanup()

    def run_(self, *args, mode="ok"):
        env = {
            # this interpreter's dir first: fake-vault's `env python3` on a host without /usr/bin/python3
            "PATH": os.path.dirname(sys.executable) + ":/usr/bin:/bin",
            "MMC_HOME": str(self.home),
            "MMC_VAULT": str(HERE / "fake-vault"),
            "MMC_TODAY": "2026-10-16",
            "FAKE_MODE": mode,
            "FAKE_LOG": str(self.log),
            "FAKE_RESULT": str(FIX),
        }
        p = subprocess.run(
            [sys.executable, "-I", SCRIPT, *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
        )
        return p.returncode, p.stdout, p.stderr

    def calls(self):
        return [json.loads(x) for x in self.log.read_text().splitlines()] if self.log.exists() else []

    def report(self, name="mail-migration-2026-10-16.md"):
        return (self.home / "reports" / "admin" / name).read_text()

    def test_report_from_vault(self):
        rc, out, err = self.run_()
        self.assertEqual(rc, 0, err)
        self.assertIn("main → personal: 220 of 1200 mails missing (18 %), 980 moved, 20 only in personal", out)
        self.assertIn("worst years: 2019 200, 2018 10, 2020 10", out)
        self.assertIn("report: ", out)
        r = self.report()
        for s in (
            "# Mail migration check: main → personal (2026-10-16)",
            "| **missing in personal** | **220** (18 %) |",
            "| 2019 | 500 | 300 | 300 | 200 | 40 % |",
            "| 2019-02 | shop.example | 90 |",
            "| Sent | 150 | 50 |",
            "…@old-firma.example 980",
            "top 3 of 9 rows, 5 domains",
            "| 2019-02 | 120 |",
        ):
            self.assertIn(s, r)
        self.assertNotIn("x.y@leak.example", r)  # the address guard: domain only
        self.assertIn("Kunden …@leak.example 3", r)
        c = self.calls()
        self.assertEqual(c[0], ["--async", "gmail", "compare", "--old", "main", "--new", "personal", "--top", "150"])
        self.assertEqual(c[1][:3], ["job", "wait", "abcdef0123456789"])
        self.assertLessEqual(int(c[1][c[1].index("--max") + 1]), 230)

    def test_not_deployed(self):
        rc, out, err = self.run_(mode="old")
        self.assertEqual(rc, 3, (out, err))
        self.assertIn("no /gmail/compare yet", err)
        self.assertIn("not deployed", err)
        self.assertFalse((self.home / "reports").exists())

    def test_running_then_job(self):
        rc, out, err = self.run_("--wait", "1", mode="running")
        self.assertEqual(rc, 12, err)
        self.assertIn("mail-migration-check --job abcdef0123456789", err)
        rc, out, err = self.run_("--job", "abcdef0123456789", "--no-write")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("report:", out)
        self.assertEqual(self.calls()[-1][:3], ["job", "wait", "abcdef0123456789"])
        self.assertFalse((self.home / "reports").exists())
        rc, out, err = self.run_("--job", "nothex")
        self.assertEqual(rc, 2)

    def test_failures(self):
        rc, out, err = self.run_(mode="fail")
        self.assertEqual(rc, 1)
        self.assertIn("failed", err)
        rc, out, err = self.run_("--old", "nope", mode="refused")
        self.assertEqual(rc, 1)
        self.assertIn("vault refused", err)
        rc, out, err = self.run_("--old", "main", "--new", "main")
        self.assertEqual(rc, 2)

    def test_from_json_and_print(self):
        rc, out, err = self.run_("--from-json", str(FIX), "--print", "--out", str(self.home / "x.md"))
        self.assertEqual(rc, 0, err)
        self.assertTrue(out.startswith("# Mail migration check"))
        self.assertTrue((self.home / "x.md").is_file())
        self.assertEqual(self.calls(), [])  # no vault call at all
        job = self.home / "job.json"
        job.write_text(json.dumps({"state": "done", "result": json.loads(FIX.read_text())}))
        rc, out, err = self.run_("--from-json", str(job), "--no-write")
        self.assertEqual(rc, 0, err)
        bad = self.home / "bad.json"
        bad.write_text('{"x": 1}')
        self.assertEqual(self.run_("--from-json", str(bad))[0], 1)

    def test_quick(self):
        rc, out, err = self.run_("--quick", "--years", "2015-2019", mode="old")
        self.assertEqual(rc, 0, err)
        r = self.report("mail-migration-2026-10-16-quick.md")
        self.assertIn("| 2015 | 115 | 0 | -115 |", r)
        self.assertIn("| 2016 | 116 | ? | ? |", r)  # a failed search: unknown, not zero
        self.assertIn("| 2018 | 118 | 90 | -28 |", r)
        self.assertIn("Totals over these years: main 585, personal 180.", r)
        self.assertNotIn("Someone", r)
        self.assertTrue(all(c[:2] == ["gmail", "search"] and "[Gmail]/All Mail" in c for c in self.calls()))
        self.assertEqual(self.run_("--quick", "--years", "2019-2015")[0], 2)

    def test_config_defaults(self):
        d = self.home / ".config" / "mail-migration-check"
        d.mkdir(parents=True)
        (d / "accounts").write_text("old=workspace\nnew=Personal\n# comment\nbogus=x\n")
        rc, out, err = self.run_("--no-write")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.calls()[0][3:7], ["--old", "workspace", "--new", "personal"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
