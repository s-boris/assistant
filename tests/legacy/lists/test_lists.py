"""Offline tests for ~/bin/lists. Temp state only (LISTS_DB), fixed clock (LISTS_NOW).
LISTS_BIN=/path/to/copy tests a copy (default: the repo's scripts/lists). Run: tests/legacy/lists/run.sh
"""

import hashlib
import json
import os
import stat
import subprocess
import tempfile
import unittest

BIN = os.environ.get("LISTS_BIN") or os.path.expanduser("~/bin/lists")
REAL = os.path.expanduser("~/.local/state/lists/lists.json")
NOW = "2026-03-02 10:00"


def digest(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except FileNotFoundError:
        return None


REAL_BEFORE = digest(REAL)


class ListsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="lists-tests.")
        self.db = os.path.join(self.tmp.name, "state", "lists.json")

    def tearDown(self):
        self.tmp.cleanup()

    def run_lists(self, *args, now=NOW):
        env = dict(os.environ, LISTS_DB=self.db, LISTS_NOW=now)
        p = subprocess.run([BIN, *args], env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        return p.returncode, p.stdout.strip(), p.stderr.strip()

    def ok(self, *args, now=NOW):
        rc, out, err = self.run_lists(*args, now=now)
        self.assertEqual(rc, 0, f"{args}: {err}")
        return out

    def data(self):
        with open(self.db) as f:
            return json.load(f)["lists"]

    def texts(self, name):
        return [(i["text"], bool(i["done"])) for i in self.data().get(name, [])]

    # ---------------------------------------------------------------- names

    def test_aliases(self):
        for alias in ("Einkaufsliste", "groceries", "shopping list", "Einkauf", "KUPOVINA"):
            self.ok("add", alias, "x-" + alias)
        for alias in ("To-Do", "aufgaben", "to do", "todos"):
            self.ok("add", alias, "t-" + alias)
        self.ok("add", "Packing List", "Pass")
        self.ok("add", "playlist", "song")
        self.ok("add", "Gift  Ideas", "book")
        self.assertEqual(sorted(self.data()), ["gift-ideas", "packing", "playlist", "shopping", "todo"])
        self.assertEqual(len(self.data()["shopping"]), 5)
        self.assertEqual(len(self.data()["todo"]), 4)

    def test_bad_names(self):
        for bad in ("", "  ", "a/b", ".hidden", "x" * 41):
            rc, _, err = self.run_lists("add", bad, "item")
            self.assertEqual(rc, 2, bad)
            self.assertIn("bad list name", err)
        self.assertFalse(os.path.exists(self.db))

    # ---------------------------------------------------------------- add

    def test_add_dedupes_case_insensitively(self):
        out = self.ok("add", "shopping", "Milch", "  eier ", "Brot")
        self.assertIn("+3", out)
        out = self.ok("add", "einkauf", "MILCH", "Eier", "Käse", "käse")
        self.assertIn("+1 (Käse)", out)
        self.assertIn("already there: Milch, eier", out)
        self.assertEqual([t for t, _ in self.texts("shopping")], ["Milch", "eier", "Brot", "Käse"])

    def test_add_reopens_checked_item(self):
        self.ok("add", "shopping", "Milch", "Brot")
        self.ok("done", "shopping", "milch")
        out = self.ok("add", "shopping", "milch")
        self.assertIn("back on: Milch", out)
        self.assertEqual(self.texts("shopping"), [("Milch", False), ("Brot", False)])

    def test_add_needs_items(self):
        self.assertEqual(self.run_lists("add", "shopping")[0], 2)
        self.assertEqual(self.run_lists("add", "shopping", "   ")[0], 2)

    # ---------------------------------------------------------------- show / ls

    def test_show_numbers_and_checked(self):
        self.ok("add", "shopping", "Milch", "Eier", "Brot")
        self.ok("done", "shopping", "2")
        out = self.ok("show", "shopping")
        self.assertEqual(out.splitlines(), ["🛒 Shopping (2)", "1. Milch", "2. Brot", "✓ Eier"])

    def test_show_all_and_empty(self):
        self.assertEqual(self.ok("show"), "(no lists)")
        self.assertEqual(self.ok("ls"), "(no lists)")
        self.assertIn("empty", self.ok("show", "shopping"))
        self.ok("add", "packing", "Pass")
        self.ok("add", "todo", "Steuern")
        self.ok("add", "shopping", "Milch")
        blocks = self.ok("show").split("\n\n")
        self.assertEqual([b.splitlines()[0] for b in blocks], ["🛒 Shopping (1)", "☑️ Todo (1)", "🗒️ Packing (1)"])

    def test_json(self):
        self.ok("add", "shopping", "Milch", "Eier")
        self.ok("done", "shopping", "Eier")
        one = json.loads(self.ok("show", "shopping", "--json"))
        self.assertEqual(one["list"], "shopping")
        self.assertEqual(one["open"], [{"n": 1, "text": "Milch", "added": NOW}])
        self.assertEqual(one["done"], [{"text": "Eier", "done": NOW}])
        allj = json.loads(self.ok("show", "--json"))
        self.assertEqual([x["list"] for x in allj["lists"]], ["shopping"])
        self.assertEqual(json.loads(self.ok("ls", "--json")), [{"list": "shopping", "open": 1, "done": 1}])
        self.assertEqual(self.ok("ls"), "🛒 shopping: 1 open, 1 checked")

    # ---------------------------------------------------------------- done / rm / clear

    def test_done_by_number_resolves_before_changing(self):
        self.ok("add", "todo", "a", "b", "c")
        self.ok("done", "todo", "1", "2")
        self.assertEqual(self.texts("todo"), [("a", True), ("b", True), ("c", False)])

    def test_done_by_text_exact_and_part(self):
        self.ok("add", "shopping", "Milch", "Hafermilch", "Brot")
        self.ok("done", "shopping", "milch")  # exact beats the substring match
        self.assertEqual(self.texts("shopping")[0], ("Milch", True))
        self.ok("done", "shopping", "hafer")
        self.assertEqual(self.texts("shopping")[1], ("Hafermilch", True))

    def test_done_ambiguous_or_unknown_changes_nothing(self):
        self.ok("add", "shopping", "Hafermilch", "Kokosmilch", "Brot")
        before = digest(self.db)
        rc, _, err = self.run_lists("done", "shopping", "brot", "milch")
        self.assertEqual(rc, 1)
        self.assertIn("matches 2 items", err)
        rc, _, err = self.run_lists("done", "shopping", "brot", "zzz")
        self.assertEqual(rc, 1)
        self.assertIn("not on the list", err)
        rc, _, err = self.run_lists("done", "shopping", "9")
        self.assertEqual(rc, 1)
        self.assertIn("no item 9", err)
        self.assertEqual(digest(self.db), before)

    def test_rm(self):
        self.ok("add", "todo", "a", "b")
        self.assertIn("removed a", self.ok("rm", "todo", "1"))
        self.assertEqual(self.texts("todo"), [("b", False)])
        self.ok("rm", "todo", "b")
        self.assertNotIn("todo", self.data())

    def test_clear_done(self):
        self.ok("add", "shopping", "a", "b", "c")
        self.ok("done", "shopping", "a", "c")
        self.assertEqual(self.run_lists("clear", "shopping")[0], 2)
        self.assertIn("cleared 2", self.ok("clear", "shopping", "--done"))
        self.assertEqual(self.texts("shopping"), [("b", False)])

    def test_checked_items_kept_7_days(self):
        self.ok("add", "shopping", "a", "b")
        self.ok("done", "shopping", "a")
        self.assertIn("✓ a", self.ok("show", "shopping", now="2026-03-08 10:00"))
        out = self.ok("show", "shopping", now="2026-03-09 10:01")
        self.assertNotIn("a", out.replace("Shopping", ""))
        self.ok("done", "shopping", "b", now="2026-03-09 10:01")
        self.assertEqual(self.ok("ls", now="2026-03-17 10:00"), "(no lists)")

    # ---------------------------------------------------------------- storage and usage

    def test_file_mode_and_atomic(self):
        self.ok("add", "todo", "x")
        self.assertEqual(stat.S_IMODE(os.stat(self.db).st_mode), 0o600)
        self.assertFalse(os.path.exists(self.db + ".tmp"))

    def test_corrupt_file_is_left_alone(self):
        os.makedirs(os.path.dirname(self.db))
        with open(self.db, "w") as f:
            f.write("{broken")
        rc, _, err = self.run_lists("add", "todo", "x")
        self.assertEqual(rc, 1)
        self.assertIn("not valid JSON", err)
        with open(self.db) as f:
            self.assertEqual(f.read(), "{broken")

    def test_usage(self):
        self.assertEqual(self.run_lists()[0], 2)
        self.assertEqual(self.run_lists("frobnicate")[0], 2)
        self.assertEqual(self.run_lists("show", "a", "b")[0], 2)
        rc, out, _ = self.run_lists("--help")
        self.assertEqual(rc, 0)
        self.assertIn("lists add LIST", out)


class NoLeak(unittest.TestCase):
    def test_zz_real_store_untouched(self):
        self.assertEqual(digest(REAL), REAL_BEFORE)


if __name__ == "__main__":
    unittest.main(verbosity=1)
