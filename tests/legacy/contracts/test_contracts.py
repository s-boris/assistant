#!/usr/local/bin/python3.13 -I
"""Tests for ~/bin/contracts (CONTRACTS_BIN overrides the script under test).

A fake paperless (CONTRACTS_PAPERLESS) serves synthetic metadata and cards and logs every call; temp state dir,
CONTRACTS_NOW pins today, ASSISTANT_PROFILE is a synthetic profile. The real kk-compare supplies the KVG deadline."""

import importlib.machinery
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

HOME = os.path.expanduser("~")
BIN = os.environ.get("CONTRACTS_BIN") or os.path.join(HOME, "bin", "contracts")
KK = os.path.join(HOME, "bin", "kk-compare")

FAKE = r"""#!/usr/local/bin/python3.13 -I
import json, os, sys
d = os.path.dirname(os.path.realpath(__file__))
with open(os.path.join(d, "calls.log"), "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
a = sys.argv[1:]
if a[:1] == ["recent"]:
    n = int(a[a.index("-n") + 1])
    docs = json.load(open(os.path.join(d, "recent.json")))
    print(json.dumps({"results": docs[:n]}))
elif a[:1] == ["get"] and "--json" in a:
    fail = open(os.path.join(d, "fail")).read().split() if os.path.exists(os.path.join(d, "fail")) else []
    if a[1] in fail:
        print("vault: upstream timeout", file=sys.stderr); sys.exit(3)
    cards = json.load(open(os.path.join(d, "cards.json")))
    print(json.dumps(cards[a[1]]))
else:
    print("fake paperless: refused " + " ".join(a), file=sys.stderr); sys.exit(2)
"""

DOCS, CARDS = [], {}


def doc(created, title, typ=None, corr=None, tags=(), amount=None, currency="CHF", due=None):
    i = len(DOCS) + 1
    DOCS.append(
        {
            "id": i,
            "created": created,
            "added": created + "T10:00:00",
            "title": title,
            "correspondent": corr,
            "document_type": typ,
            "tags": list(tags),
            "url": f"http://x/{i}",
        }
    )
    CARDS[str(i)] = {
        "id": i,
        "created": created,
        "title": title,
        "correspondent": corr,
        "document_type": typ,
        "tags": list(tags),
        "amount": amount,
        "currency": currency,
        "due_date": due,
        "_warnings": [],
    }
    return i


# telecom: monthly, one card fails
TELCO = [doc(f"2026-{m:02d}-05", f"Rechnung {m:02d}/2026", "Rechnung", "Swisscom", amount=64.90) for m in range(3, 11)]
FAIL_ID = TELCO[3]
# health insurer by title (no correspondent), monthly premiums + a policy
HEALTH_POLICY = doc("2025-11-20", "Helsana Police 2026", "Police", tags=["Krankenkasse"])
HEALTH = [
    doc(f"2026-{m:02d}-01", f"Helsana Prämienrechnung 2026-{m:02d}", "Rechnung", tags=["Krankenkasse"], amount=420.50)
    for m in range(1, 11)
]
# household insurance: yearly
HOUSE = [
    doc(d, "Mobiliar Hausratversicherung Police", "Police", amount=310.0)
    for d in ("2024-01-10", "2025-01-12", "2026-01-09")
]
# power: quarterly, kind other
POWER = [
    doc(d, "Stromrechnung Quartal", "Rechnung", "EWB Energie", amount=150.0)
    for d in ("2025-12-31", "2026-03-31", "2026-06-30", "2026-09-30")
]
# stopped monthly statements
OLD = [doc(f"2022-{m:02d}-01", f"Kostenübersicht 2022-{m - 1:02d}-30", "Police") for m in range(4, 10)]
# cancelled subscription
NETFLIX = [doc(d, "Netflix Rechnung", "Rechnung", amount=18.90) for d in ("2026-05-02", "2026-06-02", "2026-07-02")]
NETFLIX_END = doc("2026-07-10", "Netflix Kündigungsbestätigung", "Kündigung")
# rent: one contract document
RENT = doc("2025-06-23", "Mietvertrag Wohnung Musterstrasse 7", "Vertrag", tags=["Wohnen"])
# an old policy whose title is only the owner's name
OWNER_POLICY = doc("2020-04-21", "Police Testy Tester 2020-04", "Police", tags=["Krankenkasse"])
# employer: everything skipped, including its Vertrag
EMP = [
    doc("2026-09-25", "Lohnabrechnung 09", "Lohnabrechnung", "Acme Werke"),
    doc("2026-08-25", "Lohnabrechnung 08", "Lohnabrechnung", "Acme Werke"),
    doc("2026-02-01", "Personalreglement 2026", "Vertrag", "Acme Werke"),
]
TAX = doc("2026-05-10", "Steuerveranlagung 2024", "Behörde")
# mandatory fee: yearly, never in the notice list
SERAFE = [doc(d, "Serafe Rechnung", "Rechnung", "Serafe", amount=335.0) for d in ("2025-04-15", "2026-04-14")]
# EUR subscription
SPOTIFY = [doc(f"2026-{m:02d}-01", "Spotify Abo", None, amount=12.99, currency="EUR") for m in (8, 9, 10)]
# one-off agreements: hidden from card, totals and notice; a Krankenkasse mis-tag on a Vertrag is not health
RENTAL = doc("2026-04-13", "Mietwagen Rental Agreement", "Vertrag", amount=325.0, currency="USD")
MISTAG = doc("2026-10-02", "Investment Proposal", "Vertrag", tags=["Krankenkasse"])
OWNER_CONCAT = doc("2016-10-18", "TesterHans", "Vertrag")
EMP_NOCORR = doc("2022-02-09", "Arbeitsvertrag Beispiel", "Vertrag")
# housing: a payment slip and a cancellation confirmation, each alone
RENT_SLIP = doc("2025-06-24", "ESR September", "Vertrag", tags=["Wohnen"], amount=1790.0)
PARKING_END = doc("2024-01-11", "Kündigungsbestätigung", "Vertrag", tags=["Wohnen"])
# policy with irregular dates: assumed yearly
MOTOR = [
    doc("2023-09-03", "AXA Motorfahrzeugversicherung", "Police", amount=500.0),
    doc("2025-09-29", "AXA Antrag", "Police"),
    doc("2026-03-11", "AXA Police 2", "Police", amount=602.46),
]
# one landlord, differing titles, same day
LANDLORD = [
    doc("2025-06-23", "SRQ Vertragszusatz Mietvertrag", "Vertrag", tags=["Wohnen"]),
    doc("2025-06-23", "SRQ Katzenbewilligung", "Behörde", tags=["Wohnen"]),
]


def load_module():
    loader = importlib.machinery.SourceFileLoader("contracts_mod", BIN)
    spec = importlib.util.spec_from_loader("contracts_mod", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class Base(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp(prefix="contracts-test-")
        self.fake_dir = os.path.join(self.t, "fake")
        os.makedirs(self.fake_dir)
        self.fake = os.path.join(self.fake_dir, "paperless")
        with open(self.fake, "w") as f:
            f.write(FAKE)
        os.chmod(self.fake, 0o755)
        with open(os.path.join(self.fake_dir, "recent.json"), "w") as f:
            json.dump(list(reversed(DOCS)), f)
        with open(os.path.join(self.fake_dir, "cards.json"), "w") as f:
            json.dump(CARDS, f)
        with open(os.path.join(self.fake_dir, "fail"), "w") as f:
            f.write(str(FAIL_ID))
        prof = os.path.join(self.t, "profile.json")
        with open(prof, "w") as f:
            json.dump({"owner": {"first_name": "Testy", "last_name": "Tester"}}, f)
        os.chmod(prof, 0o600)
        self.state = os.path.join(self.t, "state")
        self.env = dict(
            os.environ,
            CONTRACTS_STATE_DIR=self.state,
            CONTRACTS_PAPERLESS=self.fake,
            CONTRACTS_NOW="2026-10-18",
            CONTRACTS_KK=KK,
            ASSISTANT_PROFILE=prof,
            CONTRACTS_PROVIDERS=os.path.join(self.t, "providers.json"),
            KK_OFFLINE="1",
            KK_DATA_DIR=os.path.join(self.t, "kkdata"),
        )

    def tearDown(self):
        shutil.rmtree(self.t, ignore_errors=True)

    def run_c(self, *args, ok=True, env=None):
        r = subprocess.run(
            [BIN, *args], capture_output=True, text=True, timeout=120, env=env or self.env, stdin=subprocess.DEVNULL
        )
        if ok:
            self.assertEqual(r.returncode, 0, f"{args}: {r.stderr}")
        return r

    def calls(self):
        with open(os.path.join(self.fake_dir, "calls.log")) as f:
            return [json.loads(x) for x in f if x.strip()]

    def contracts(self):
        return {c["name"]: c for c in json.loads(self.run_c("show", "--json", "--all").stdout)["contracts"]}


class TestPure(unittest.TestCase):
    def test_guess_cadence(self):
        m = load_module()
        from datetime import date, timedelta

        d0 = date(2026, 1, 1)
        self.assertEqual(m.guess_cadence([d0]), (None, "none"))
        self.assertEqual(m.guess_cadence([d0 + timedelta(days=30 * i) for i in range(4)]), ("monthly", "dates"))
        self.assertEqual(m.guess_cadence([d0, d0 + timedelta(days=91)]), ("quarterly", "dates?"))
        self.assertEqual(
            m.guess_cadence([d0, d0 + timedelta(days=183), d0 + timedelta(days=365)]), ("half-yearly", "dates")
        )
        self.assertEqual(m.guess_cadence([d0, d0 + timedelta(days=364), d0 + timedelta(days=731)]), ("yearly", "dates"))
        self.assertEqual(m.guess_cadence([d0, d0 + timedelta(days=600)]), (None, "irregular"))

    def test_name_tokens_strip_digits_and_generic(self):
        m = load_module()
        self.assertEqual(
            m.name_tokens("SRQ 1396.01.0302 Vertragszusatz zum Mietvertrag 2025"),
            ["SRQ", "Vertragszusatz", "Mietvertrag"],
        )
        self.assertEqual(m.name_tokens("document - 2024-01-11T140101.526"), [])

    def test_money(self):
        m = load_module()
        self.assertEqual(m.money(5046.0, 0), "5'046")
        self.assertEqual(m.money(64.9), "64.90")


class TestScan(Base):
    def test_scan_groups_and_estimates(self):
        r = self.run_c("scan")
        self.assertIn("failed 1", r.stdout)
        self.assertIn(f"#{FAIL_ID}", r.stderr)
        c = self.contracts()
        tel = c["Swisscom"]
        self.assertEqual((tel["kind"], tel["cadence"], tel["status"]), ("telecom", "monthly", "active"))
        self.assertEqual(tel["last_amount"], 64.9)
        self.assertEqual(tel["yearly"], 778.8)
        self.assertEqual(tel["next"], "2026-11-04")
        self.assertEqual(tel["cards_missing"], 1)
        h = c["Helsana"]
        self.assertEqual((h["kind"], h["cadence"], h["yearly"]), ("health", "monthly", 5046.0))
        self.assertIn(HEALTH_POLICY, h["docs"])
        mob = c["Mobiliar"]
        self.assertEqual(
            (mob["kind"], mob["cadence"], mob["cadence_source"], mob["yearly"], mob["next"]),
            ("insurance", "yearly", "dates", 310.0, "2027-01-09"),
        )
        ewb = c["EWB Energie"]
        self.assertEqual((ewb["cadence"], ewb["yearly"], ewb["kind"]), ("quarterly", 600.0, "other"))
        self.assertEqual((c["Serafe"]["kind"], c["Serafe"]["cadence"]), ("fee", "yearly"))
        self.assertEqual(c["Spotify"]["currency"], "EUR")
        self.assertEqual(c["Kostenübersicht"]["status"], "maybe-ended")
        self.assertEqual(c["Kostenübersicht"]["cadence"], "monthly")
        self.assertEqual(c["Netflix"]["status"], "cancelled")
        self.assertEqual(c["Netflix"]["next"], None)
        rent = c["Mietvertrag Wohnung Musterstrasse"]
        self.assertEqual((rent["kind"], rent["status"], rent["cadence"]), ("rent", "unknown", None))
        own = c["Krankenkasse"]  # health-insurance policy without insurer name: one Krankenkasse group
        self.assertEqual((own["kind"], own["cadence_source"], own["status"]), ("health", "assumed", "maybe-ended"))
        # employer and tax documents are not contracts
        all_docs = {i for x in c.values() for i in x["docs"]}
        self.assertFalse(all_docs & set(EMP + [TAX, EMP_NOCORR]))
        self.assertFalse(any("Acme" in n or "Tester" in n or "Testy" in n for n in c))
        rent = c["Mietwagen Rental Agreement"]
        self.assertEqual((rent["kind"], rent["status"]), ("other", "one-off"))
        inv = c["Investment Proposal"]
        self.assertEqual((inv["kind"], inv["status"]), ("other", "one-off"))
        axa = c["AXA"]
        self.assertEqual(
            (axa["kind"], axa["cadence"], axa["cadence_source"], axa["status"], axa["yearly"]),
            ("insurance", "yearly", "assumed", "active", 602.46),
        )
        self.assertEqual((c["SRQ"]["kind"], len(c["SRQ"]["docs"]), c["SRQ"]["events"]), ("rent", 2, 1))
        self.assertEqual((c["ESR"]["kind"], c["ESR"]["status"]), ("rent", "unknown"))
        self.assertEqual(
            (c["Kündigungsbestätigung"]["kind"], c["Kündigungsbestätigung"]["status"]), ("rent", "cancelled")
        )

    def test_totals_per_currency(self):
        self.run_c("scan")
        out = json.loads(self.run_c("show", "--json").stdout)
        self.assertEqual(out["totals"], {"CHF": round(778.8 + 5046 + 310 + 600 + 335 + 602.46, 2), "EUR": 155.88})

    def test_only_cards_never_content(self):
        self.run_c("scan")
        self.run_c("scan", "--refresh")
        for call in self.calls():
            self.assertIn(call[0], ("recent", "get"))
            self.assertFalse({"--content", "--reason", "--text"} & set(call), call)

    def test_card_cache(self):
        self.run_c("scan")
        n1 = len([c for c in self.calls() if c[0] == "get"])
        self.assertEqual(n1, len({i for x in self.contracts().values() for i in x["docs"]}))
        self.run_c("scan")
        gets2 = [c for c in self.calls() if c[0] == "get"][n1:]
        self.assertEqual(gets2, [["get", str(FAIL_ID), "--json"]])  # only the failed card is retried
        self.run_c("scan", "--refresh")
        self.assertEqual(len([c for c in self.calls() if c[0] == "get"]), n1 * 2 + 1)

    def test_state_private(self):
        self.run_c("scan")
        self.assertEqual(stat.S_IMODE(os.stat(self.state).st_mode), 0o700)
        for f in ("cards.json", "contracts.json"):
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.state, f)).st_mode), 0o600, f)

    def test_paperless_down(self):
        env = dict(self.env, CONTRACTS_PAPERLESS=os.path.join(self.t, "missing"))
        r = self.run_c("scan", ok=False, env=env)
        self.assertNotEqual(r.returncode, 0)


class TestShow(Base):
    def test_no_scan_yet(self):
        r = self.run_c("show", ok=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no scan yet", r.stderr)

    def test_card(self):
        self.run_c("scan")
        out = self.run_c("show").stdout
        self.assertIn("💰 Top costs", out)
        self.assertIn("1. Helsana — CHF 420.50 · ≈5'046/yr · monthly", out)
        self.assertIn("CHF 7'672/yr", out)
        self.assertIn("one-off hidden", out)
        self.assertNotIn("Rental", out)
        self.assertIn("EUR 156/yr", out)
        self.assertIn("maybe ended?", out)
        self.assertIn("Kostenübersicht — monthly, last 01.09.2022", out)
        self.assertIn("Netflix (10.07.2026)", out)
        self.assertIn("Kündigungsbestätigung (11.01.2024)", out)
        self.assertIn("KVG switch: notice must arrive by Mon 30.11. (43 d)", out)
        self.assertNotIn("Testy", out)
        self.assertNotIn("Tester", out)
        self.assertLessEqual(len(out.splitlines()), 25)
        for line in out.splitlines():
            self.assertLessEqual(len(line), 120, line)

    def test_all_lists_keys(self):
        self.run_c("scan")
        out = self.run_c("show", "--all").stdout
        self.assertIn("[p:swisscom]", out)  # a known provider keys as p: even with a correspondent
        self.assertIn("[c:ewb energie]", out)
        self.assertIn("[p:helsana]", out)
        self.assertIn("[t:kostenubersicht]", out)


class TestNotice(Base):
    def test_notice(self):
        self.run_c("scan")
        items = json.loads(self.run_c("notice", "--json").stdout)["items"]
        names = [i["name"] for i in items]
        self.assertEqual(items[0]["name"], "Helsana")
        self.assertEqual((items[0]["rule"], items[0]["deadline"], items[0]["days_left"]), ("kvg", "2026-11-30", 43))
        self.assertIn("supplementary (VVG): notice period unknown — needs his ✅ per document", items[0]["text"])
        self.assertNotIn("Serafe", names)  # mandatory fee
        self.assertNotIn("Netflix", names)  # cancelled
        self.assertNotIn("Mietwagen Rental Agreement", names)  # one-off
        self.assertNotIn("Investment Proposal", names)
        mob = next(i for i in items if i["name"] == "Mobiliar")
        self.assertEqual(mob["rule"], "unknown")
        self.assertEqual(mob["text"], f"notice period unknown — needs his ✅ per document (#{HOUSE[-1]})")
        self.assertIsNone(mob["deadline"])
        txt = self.run_c("notice").stdout
        self.assertIn("KVG basic: notice must ARRIVE by Mon 30.11.2026 (43 d", txt)

    def test_deadline_rolls_to_next_year(self):
        self.run_c("scan")
        env = dict(self.env, CONTRACTS_NOW="2026-12-05")
        items = json.loads(self.run_c("notice", "--json", env=env).stdout)["items"]
        self.assertEqual(items[0]["deadline"], "2027-11-30")


class TestSet(Base):
    def test_overrides(self):
        self.run_c("scan")
        self.run_c("set", "Kostenübersicht", "--ignore")
        self.assertNotIn("Kostenübersicht", self.run_c("show").stdout)
        self.run_c("set", "t:kostenubersicht", "--unignore")
        self.assertIn("Kostenübersicht", self.run_c("show").stdout)
        self.run_c("set", "mobiliar", "--notice", "3 months to 31.12.")
        mob = next(i for i in json.loads(self.run_c("notice", "--json").stdout)["items"] if i["name"] == "Mobiliar")
        self.assertEqual((mob["rule"], mob["text"]), ("owner", "3 months to 31.12."))
        self.run_c("set", "EWB", "--cadence", "monthly")
        self.assertEqual(self.contracts()["EWB Energie"]["yearly"], 1800.0)
        self.run_c("set", "Swisscom", "--ended")
        self.assertEqual(self.contracts()["Swisscom"]["status"], "ended")
        self.run_c("set", "Spotify", "--merge-into", "p:swisscom")
        self.assertNotIn("Spotify", self.contracts())
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.state, "overrides.json")).st_mode), 0o600)
        r = self.run_c("set", "nosuchthing", "--ignore", ok=False)
        self.assertEqual(r.returncode, 2)


class TestMatching(unittest.TestCase):
    """Provider keys, bank and car-hire kinds, stale and foreign one-offs, health premiums (pure)."""

    @classmethod
    def setUpClass(cls):
        cls.m = load_module()

    def d(self, created, title, typ="Vertrag", corr=None, tags=(), amount=None, currency=None, i=1):
        return {
            "id": i,
            "created": created,
            "title": title,
            "correspondent": corr,
            "document_type": typ,
            "tags": list(tags),
            "amount": amount,
            "currency": currency,
            "card": True,
        }

    def c(self, key, docs):
        from datetime import date

        return self.m.contract(key, {"key": key, "names": [key] * len(docs), "docs": docs}, {}, date(2026, 10, 18))

    def test_provider_key_from_correspondent(self):
        m = self.m
        self.assertEqual(
            m.group_key(self.d("2026-01-01", "Police 2", corr="AXA Versicherungen AG")),
            ("p:axa", "AXA Versicherungen AG"),
        )
        self.assertEqual(m.group_key(self.d("2026-01-01", "AXA Police 2"))[0], "p:axa")
        self.assertEqual(m.group_key(self.d("2026-01-01", "Rechnung", corr="EWB Energie"))[0], "c:ewb energie")
        self.assertEqual(
            m.group_key(self.d("2025-10-01", "Versicherungspolicen", "Police", tags=["Krankenkasse"])),
            ("g:health", "Krankenkasse"),
        )
        self.assertNotEqual(
            m.group_key(self.d("2026-10-02", "Proposal", "Vertrag", tags=["Krankenkasse"]))[0], "g:health"
        )  # a Krankenkasse tag on a Vertrag is not trusted

    def test_bank_and_car_hire_kinds(self):
        m = self.m
        self.assertEqual(
            m.kind_of(self.d("2025-02-17", "deposit insurance information", "Police", "PostFinance")), "bank"
        )
        self.assertEqual(m.kind_of(self.d("2026-04-13", "ENTERPRISE RENTAL AGREEMENT")), "rental")
        self.assertEqual(m.kind_of(self.d("2024-07-30", "Vertrag", corr="Goldcar")), "rental")
        self.assertNotEqual(m.kind_of(self.d("2026-01-01", "Avis de débit", "Rechnung")), "rental")
        bank = self.c(
            "p:postfinance",
            [self.d("2025-02-17", "fees", corr="PostFinance", amount=0.5, currency="USD", i=i) for i in (1, 2)],
        )
        self.assertEqual(bank["status"], "account")
        car = self.c("p:goldcar", [self.d("2024-07-30", "Vertrag", corr="Goldcar")])
        self.assertEqual((car["kind"], car["status"]), ("rental", "one-off"))
        self.assertEqual(m.notices([bank, car], m.today()), [])

    def test_foreign_single_event_and_stale(self):
        abroad = self.c(
            "t:acuerdo", [self.d("2026-04-05", "Acuerdo firmado", tags=["Wohnen"], amount=325.0, currency="USD")]
        )
        self.assertEqual(abroad["status"], "one-off")
        old = self.c("p:yallo", [self.d("2018-11-23", "SIGNED POA", corr="Yallo")])
        self.assertEqual((old["kind"], old["status"]), ("telecom", "maybe-ended"))
        recent = self.c("t:mietvertrag", [self.d("2025-06-23", "Mietvertrag Wohnung", tags=["Wohnen"])])
        self.assertEqual(recent["status"], "unknown")

    def test_health_policy_amount_is_a_monthly_premium(self):
        h = self.c(
            "g:health",
            [
                self.d("2020-04-21", "Police", "Police", tags=["Krankenkasse"], amount=264.95, i=1),
                self.d(
                    "2025-10-01",
                    "Versicherungspolicen",
                    "Police",
                    tags=["Krankenkasse"],
                    amount=429.6,
                    currency="CHF",
                    i=2,
                ),
            ],
        )
        self.assertEqual((h["status"], h["premium"], h["yearly"]), ("active", "monthly?", 5155.2))
        self.assertIn("CHF 429.60/mo? · ≈5'155/yr", self.m.cost_txt(h))
        big = self.c(
            "g:health", [self.d("2025-10-01", "Police", "Police", tags=["Krankenkasse"], amount=5155.2, currency="CHF")]
        )
        self.assertEqual((big["premium"], big["yearly"]), (None, 5155.2))

    def test_policy_terms_are_not_contracts(self):
        self.assertFalse(self.m.is_relevant(self.d("2025-12-28", "Refund policy", "Police"), set()))


class TestEbillAndRefetch(Base):
    def test_private_providers(self):
        with open(os.path.join(self.t, "providers.json"), "w") as f:
            json.dump({"Examplebank": ["Examplebank", "bank"], "bad": ["Bad", "nonsense"]}, f)
        self.run_c("scan")
        r = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                "import importlib.machinery as m, sys; "
                "mod = m.SourceFileLoader('c', sys.argv[1]).load_module(); "
                "print(mod.PROVIDERS.get('examplebank'), 'bad' in mod.PROVIDERS)",
                BIN,
            ],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=60,
        )
        self.assertEqual(r.stdout.split(), ["('Examplebank',", "'bank')", "False"], r.stderr)

    def test_metadata_change_refetches_card(self):
        self.run_c("scan")
        n1 = len([c for c in self.calls() if c[0] == "get"])
        rp, cp = os.path.join(self.fake_dir, "recent.json"), os.path.join(self.fake_dir, "cards.json")
        with open(rp) as f:
            docs = json.load(f)
        with open(cp) as f:
            cards = json.load(f)
        for d in docs:
            if d["id"] == RENT_SLIP:
                d["correspondent"] = "Verwaltung Beispiel"
        cards[str(RENT_SLIP)]["correspondent"] = "Verwaltung Beispiel"
        with open(rp, "w") as f:
            json.dump(docs, f)
        with open(cp, "w") as f:
            json.dump(cards, f)
        self.run_c("scan")
        gets = [c[1] for c in self.calls() if c[0] == "get"]
        self.assertEqual(sorted(gets[n1:]), sorted([str(FAIL_ID), str(RENT_SLIP)]))
        self.run_c("scan")
        self.assertEqual([c[1] for c in self.calls() if c[0] == "get"][len(gets) :], [str(FAIL_ID)])

    def test_ebill(self):
        self.run_c("scan")
        kkp = os.path.join(self.t, "kk.json")
        with open(kkp, "w") as f:
            json.dump({"current": {"insurer": "Helsana"}}, f)
        env = dict(self.env, CONTRACTS_KK_PROFILE=kkp)
        out = json.loads(self.run_c("ebill", "--json", env=env).stdout)
        self.assertIn("30.09.2028", out["note"])
        rows = {r["key"]: r for r in out["billers"]}
        self.assertEqual(rows["p:axa"]["ebill"], "yes")
        self.assertTrue(rows["p:axa"]["source"].startswith("https://"))
        self.assertTrue(rows["p:helsana"]["ebill"].startswith("yes"))
        self.assertEqual(rows["g:health"]["name"], "Krankenkasse (Helsana)")
        self.assertIn("property manager", rows["t:esr"]["hint"])
        self.assertIsNone(rows["c:ewb energie"]["ebill"])
        self.assertEqual(rows["p:swisscom"]["bills"], 8)
        self.assertNotIn("p:netflix", rows)  # cancelled
        self.assertFalse(any(r["status"] == "one-off" for r in out["billers"]))
        txt = self.run_c("--ebill", env=env).stdout
        self.assertIn("💳 eBill sweep", txt)
        self.assertIn("• AXA (insurance, active)", txt)
        self.assertNotIn("Tester", txt)


if __name__ == "__main__":
    unittest.main(verbosity=1)
