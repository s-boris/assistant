#!/usr/bin/env python3
"""Tests for `contracts warranty` (CONTRACTS_BIN overrides the script under test).

Fictional fixture only: made-up shop invoices and receipts served by test_contracts' fake paperless (it logs every
call and refuses anything but `recent` and `get --json`). Today is pinned to 2026-10-18 (Base)."""

import fcntl
import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_contracts import BIN, Base  # noqa: E402

DOCS, CARDS = [], {}


def doc(created, title, typ=None, corr=None, amount=None):
    i = 900 + len(DOCS) + 1
    DOCS.append(
        {
            "id": i,
            "created": created,
            "added": created + "T10:00:00",
            "title": title,
            "correspondent": corr,
            "document_type": typ,
            "tags": [],
            "url": f"http://x/{i}",
        }
    )
    CARDS[str(i)] = {
        "id": i,
        "created": created,
        "title": title,
        "correspondent": corr,
        "document_type": typ,
        "tags": [],
        "amount": amount,
        "currency": "CHF",
        "due_date": None,
        "_warnings": [],
    }
    return i


TV = doc("2026-09-12", 'Rechnung LG OLED55C4 TV 55" Bestellung 87654321', "Rechnung", "Galaxus", 1299.0)
DRILL = doc("2026-08-08", "Quittung Bohrmaschine", "Quittung", "Hornbach", 89.0)  # its card fails
MAC = doc("2026-06-15", "Apple Rechnung MacBook Air", "Rechnung", "Apple", 1199.0)
BIKE = doc("2026-05-05", "Rechnung Velo Service", "Rechnung", "Velowerk Muster", 180.0)  # private shop
NOITEM = doc("2026-01-20", "Rechnung 4455667", "Rechnung", "Brack")
VACUUM = doc("2024-12-01", "Brack Rechnung Staubsauger", None, None, 349.0)  # shop + word in the title
HEADPH = doc("2024-03-02", "Quittung Kopfhörer", "Quittung", "Digitec Galaxus AG")  # expired
COFFEE = doc(
    "2024-02-29", "Interdiscount Kaufbeleg Testy Tester Kaffeemaschine 19.10.2024", "Kaufbeleg", "Interdiscount"
)  # leap day, expired
# not purchases
PRIME = doc("2026-08-01", "Amazon Prime Rechnung", "Rechnung", "Amazon", 9.0)
MOBILE = doc("2026-10-01", "Galaxus Mobile Rechnung 10/2026", "Rechnung", None, 25.0)
DUNNING = doc("2026-07-01", "Mahnung Rechnung Regal", "Mahnung", "IKEA")
TELCO = doc("2026-09-05", "Rechnung September", "Rechnung", "Sunrise", 59.0)
ICLOUD = doc("2026-09-15", "Apple Rechnung iCloud+", "Rechnung", "Apple", 3.0)
CORR_ONLY = doc("2026-04-01", "Alternate Rechnung Adresse", "Rechnung", None)
CONTRACT = doc("2025-06-23", "Mietvertrag Wohnung", "Vertrag", None)

COVERED = [TV, DRILL, MAC, BIKE, NOITEM, VACUUM]
ALL = COVERED + [HEADPH, COFFEE]


class Warranty(Base):
    def setUp(self):
        super().setUp()
        with open(os.path.join(self.fake_dir, "recent.json"), "w") as f:
            json.dump(list(reversed(DOCS)), f)
        with open(os.path.join(self.fake_dir, "cards.json"), "w") as f:
            json.dump(CARDS, f)
        with open(os.path.join(self.fake_dir, "fail"), "w") as f:
            f.write(str(DRILL))
        with open(os.path.join(self.t, "providers.json"), "w") as f:
            json.dump({"Velowerk Muster": ["Velowerk Muster", "shop"], "Examplebank": ["Examplebank", "bank"]}, f)

    def w(self, *args, env=None, ok=True):
        return self.run_c("warranty", *args, env=env, ok=ok)

    def wj(self, *args, env=None):
        return json.loads(self.w("--json", *args, env=env).stdout)

    def test_default_covered_newest_first(self):
        d = self.wj()
        self.assertEqual([i["id"] for i in d["items"]], COVERED)
        self.assertEqual((d["purchases"], d["covered"], d["source"], d["months"]), (8, 6, "live", 24))
        self.assertIn("OR 210", d["note"])

    def test_all_lists_expired_and_leap_day(self):
        items = {i["id"]: i for i in self.wj("--all")["items"]}
        self.assertEqual(sorted(items), sorted(ALL))
        self.assertEqual(items[COFFEE]["covered_until"], "2026-02-28")
        self.assertFalse(items[COFFEE]["covered"])
        self.assertEqual(items[TV]["covered_until"], "2028-09-12")
        self.assertEqual(items[HEADPH]["shop"], "Digitec Galaxus")

    def test_not_purchases(self):
        ids = {i["id"] for i in self.wj("--all")["items"]}
        for x in (PRIME, MOBILE, DUNNING, TELCO, ICLOUD, CORR_ONLY, CONTRACT):
            self.assertNotIn(x, ids)

    def test_item_names(self):
        items = {i["id"]: i for i in self.wj("--all")["items"]}
        self.assertEqual(items[TV]["item"], "LG OLED55C4 TV 55")
        self.assertEqual(items[COFFEE]["item"], "Kaffeemaschine")
        self.assertEqual(items[VACUUM]["item"], "Staubsauger")
        self.assertEqual(
            (items[VACUUM]["shop"], items[BIKE]["shop"], items[BIKE]["item"]),
            ("Brack", "Velowerk Muster", "Velo Service"),
        )
        self.assertEqual((items[NOITEM]["item"], items[NOITEM]["item_from_title"]), ("Rechnung 20.01.2026", False))

    def test_text_card(self):
        out = self.w().stdout
        lines = out.strip().splitlines()
        self.assertEqual(lines[0], "🛡 Warranty · 6 still covered (of 8)")
        self.assertEqual(lines[1], "• LG OLED55C4 TV 55 · Galaxus · CHF 1'299 · until 12.09.2028")
        self.assertEqual(lines[2], "• Bohrmaschine · Hornbach · until 08.08.2028")  # card failed: no amount
        self.assertIn("• Staubsauger · Brack · CHF 349 · ⚠️ until 01.12.2026 (44 d)", out)
        self.assertEqual(lines[-1], "24 mo = Swiss legal default (OR 210); shop/maker terms may differ.")
        for bad in ("Tester", "87654321", "Kopfhörer", "Prime"):
            self.assertNotIn(bad, out)

    def test_max_and_alias(self):
        out = self.run_c("--warranty", "--max", "2").stdout
        self.assertEqual(len([l for l in out.splitlines() if l.startswith("• ")]), 2)  # noqa: E741
        self.assertIn("… +4 more", out)

    def test_query(self):
        out = self.w("tv").stdout
        self.assertTrue(out.startswith('🛡 Warranty · "tv": 1 match\n• LG OLED55C4'), out)
        out = self.w("Kaffee").stdout  # expired matches are listed too
        self.assertIn("• Kaffeemaschine · Interdiscount · expired 28.02.2026", out)
        self.assertIn("1 match", self.w("digitec").stdout)  # the shop name matches as well
        out = self.w("waschmaschine").stdout
        self.assertIn('"waschmaschine": 0 matches', out)
        self.assertIn("No shop invoice or receipt matches.", out)
        self.assertEqual([i["id"] for i in self.wj("mac", "air")["items"]], [MAC])

    def test_cards_fetched_once_and_cached(self):
        self.w()
        gets = sorted(c[1] for c in self.calls() if c[0] == "get")
        self.assertEqual(gets, sorted(str(i) for i in ALL))
        self.w()
        gets2 = [c[1] for c in self.calls() if c[0] == "get"][len(gets) :]
        self.assertEqual(gets2, [str(DRILL)])  # only the failed one again
        for c in self.calls():
            self.assertTrue(c[0] == "recent" or c[-1] == "--json", c)
            self.assertFalse({"--content", "--reason", "--text"} & set(c), c)
        mode = os.stat(os.path.join(self.state, "cards.json")).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_budget_zero_fetches_nothing(self):
        d = self.wj("--budget", "0")
        self.assertEqual([c for c in self.calls() if c[0] == "get"], [])
        self.assertEqual(len(d["items"]), 6)
        self.assertIsNone(d["items"][0]["amount"])

    def test_busy_lock_skips_cards(self):
        os.makedirs(self.state, exist_ok=True)
        fd = os.open(os.path.join(self.state, "cards.json.lock"), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            d = self.wj()
        finally:
            os.close(fd)
        self.assertEqual([c for c in self.calls() if c[0] == "get"], [])
        self.assertEqual(len(d["items"]), 6)

    def test_paperless_down(self):
        env = dict(self.env, CONTRACTS_PAPERLESS=os.path.join(self.t, "missing"))
        r = self.w(env=env, ok=False)
        self.assertEqual(r.returncode, 3)
        self.run_c("scan")  # a cached scan → fallback to its metadata
        out = self.w(env=env).stdout
        self.assertIn("as of the last scan", out)
        self.assertIn("LG OLED55C4", out)

    def test_empty_paperless(self):
        with open(os.path.join(self.fake_dir, "recent.json"), "w") as f:
            json.dump([], f)
        out = self.w().stdout
        self.assertIn("No shop invoices or receipts in paperless yet", out)

    def test_contracts_radar_skips_shop_purchases(self):
        self.run_c("scan")
        names = set(self.contracts())
        for shop in ("Galaxus", "Hornbach", "Brack", "Interdiscount", "Digitec Galaxus AG", "Velowerk Muster"):
            self.assertNotIn(shop, names)
        self.assertIn("Apple", names)  # a provider too (subscriptions): stays in the radar
        self.assertIn("Sunrise", names)
        self.assertIn("Amazon", names)  # the Prime bill is a subscription

    def test_private_shop_not_a_provider(self):
        r = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                "import importlib.machinery as m, sys; "
                "mod = m.SourceFileLoader('c', sys.argv[1]).load_module(); "
                "print('velowerk muster' in mod.SHOPS, 'velowerk muster' in mod.PROVIDERS, "
                "'examplebank' in mod.PROVIDERS)",
                BIN,
            ],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=60,
        )
        self.assertEqual(r.stdout.split(), ["True", "False", "True"], r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=1)
