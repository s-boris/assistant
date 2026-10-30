#!/usr/bin/python3 -I
"""Offline tests for webagent (CLI, lib, PreToolUse guard hook): classifier, redaction, the task state machine with
journal-verified approvals, the cards' one-time approvals (ap:<nonce>, poll and bridge mode), expiry, back-out, the
guard's tiers and the usage events. Fake journal, access file, browser
and usage spool in a temp dir; no network, no real browser. Fixture card numbers and IBANs are generated at run time.
Run: ./run.sh  (copies: WEBAGENT_LIB=<lib dir> WEBAGENT_CLI=<script> WEBAGENT_HOOK=<hook>)"""
import json
import os
import shutil
import pwd
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

HOME = os.path.expanduser("~")
LIB = os.environ.get("WEBAGENT_LIB") or os.path.join(HOME, "bin", "lib")
CLI = os.environ.get("WEBAGENT_CLI") or os.path.join(HOME, "bin", "webagent")
# the installed hook lives in the account's real home (test-all runs suites with a temp HOME that has no .claude)
HOOK = os.environ.get("WEBAGENT_HOOK") or os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".claude", "hooks",
                                                       "webagent-guard.sh")
TMP = tempfile.mkdtemp(prefix="webagent-test-")
# securefill's fill-session hooks stay off and point at a dead local port with a fake token: no test ever reaches the
# real vault (it would send him real ✅ cards). Session tests live in securefill/test_securefill.py (fake vault).
ENV = dict(os.environ, WEBAGENT_LIB=LIB, WEBAGENT_STATE=os.path.join(TMP, "state"),
           WEBAGENT_JOURNAL=os.path.join(TMP, "journal.jsonl"), WEBAGENT_ACCESS=os.path.join(TMP, "access.json"),
           WEBAGENT_BROWSER_BIN=os.path.join(TMP, "browser"), USAGE_STATE=os.path.join(TMP, "usage"),
           WEBAGENT_USAGE_LIB=os.path.join(HOME, "bin", "lib"), WEBAGENT_FILL_SESSION="off",
           SECUREFILL_VAULT_URL="http://127.0.0.1:9", SECUREFILL_VAULT_TOKEN_FILE=os.path.join(TMP, "vault-token"),
           SECUREFILL_STATE=os.path.join(TMP, "securefill"), SECUREFILL_USAGE="off",
           # H15 Pilot 1: the hub workflow mirror stays off (switch file in TMP) and any hub call goes to a dead port
           WEBAGENT_WF_SWITCH=os.path.join(TMP, "wf-webtask"), HUB_URL="http://127.0.0.1:9",
           WEBAGENT_WF_RUN_SWITCH=os.path.join(TMP, "wf-webtask-run"),  # P3-6: webtask.run mirror, off
           HUB_TOKEN_FILE=os.path.join(TMP, "hub-token"))
os.environ.pop("HUB_SCHEMA", None)
os.environ.update(ENV)
sys.path.insert(0, LIB)
import webagent as W  # noqa: E402

with open(ENV["WEBAGENT_ACCESS"], "w") as f:
    json.dump({"allowFrom": ["1000001"]}, f)
with open(ENV["SECUREFILL_VAULT_TOKEN_FILE"], "w") as f:
    f.write("test-token")
with open(ENV["HUB_TOKEN_FILE"], "w") as f:
    f.write("test-hub-token")
with open(ENV["WEBAGENT_BROWSER_BIN"], "w") as f:
    f.write('#!/bin/sh\necho \'{"url": "http://192.0.2.10:6080/vnc.html?autoconnect=1", "verified": true}\'\n')
os.chmod(ENV["WEBAGENT_BROWSER_BIN"], 0o755)
SHOT = os.path.join(TMP, "shot.png")
with open(SHOT, "wb") as f:
    f.write(b"\x89PNG\r\n\x1a\n" + b"\0" * 32)


def luhn_number(prefix, length=16):
    """A Luhn-valid test number built at run time (no card-like literal in the source)."""
    body = [int(c) for c in prefix] + [0] * (length - len(prefix) - 1)
    for check in range(10):
        digits = body + [check]
        s = sum((d * 2 - 9 if d > 4 else d * 2) if i % 2 else d for i, d in enumerate(reversed(digits)))
        if s % 10 == 0:
            return "".join(map(str, digits))


def iban(country="CH", bban="00762" + "0" * 7 + "12345"):
    num = "".join(str(int(c, 36)) for c in bban + country + "00")
    return f"{country}{98 - int(num) % 97:02d}{bban}"


def read(p):
    with open(p) as f:
        return f.read()


def run(*args, env=None, inp=None):
    r = subprocess.run([sys.executable, "-I", CLI, *args], capture_output=True, text=True, env=dict(ENV, **(env or {})),
                       input=inp, timeout=60)
    return r.returncode, r.stdout, r.stderr


def jrun(*args, **kw):
    rc, o, e = run(*args, **kw)
    return rc, (json.loads(o) if o.strip().startswith(("{", "[")) else o), e


def journal(kind, meta, owner=True, ago=0):
    meta = dict({"ts": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - ago))}, **meta)
    with open(ENV["WEBAGENT_JOURNAL"], "a") as f:
        f.write(json.dumps({"kind": kind, "owner": owner, "meta": meta}) + "\n")


def ap_db():
    return os.path.join(ENV["WEBAGENT_STATE"], "approvals.db")


def ap_row(nonce):
    con = sqlite3.connect(ap_db())
    con.row_factory = sqlite3.Row
    try:
        r = con.execute("SELECT * FROM approvals WHERE nonce = ?", (nonce,)).fetchone()
        return dict(r) if r else None
    finally:
        con.close()


def ap_tap(nonce, yn="y", user="1000001", chat="1000001", owner=True, msg="70"):
    """His tap on a card's ap:<nonce> button as the inbound journal records it (poll mode)."""
    journal("button", {"callback_data": f"ap:{nonce}:{yn}", "message_id": msg, "user_id": user, "chat_id": chat},
            owner=owner)


def hook(event, env=None):
    r = subprocess.run(["/bin/sh", HOOK], input=json.dumps(event) if not isinstance(event, str) else event,
                       capture_output=True, text=True, env=dict(ENV, **(env or {})), timeout=30)
    return r.returncode, r.stdout.strip()


def click(element, target="e12"):
    return {"tool_name": "mcp__browser__browser_click", "tool_input": {"element": element, "target": target}}


def denied(res):
    return bool(res) and res["hookSpecificOutput"]["permissionDecision"] == "deny"


_N = [0]


def uniq(prefix="Test task"):
    """A fresh title: tasks in these tests are separate jobs (the same title + site would be a retry of one job)."""
    _N[0] += 1
    return f"{prefix} {_N[0]}"


def new_task(*extra, url="https://httpbin.org/forms/post", title=None):
    rc, o, e = run("start", title or uniq(), "--url", url, *extra)
    assert rc == 0, e
    return o.strip()


def reset():
    shutil.rmtree(ENV["WEBAGENT_STATE"], ignore_errors=True)
    open(ENV["WEBAGENT_JOURNAL"], "w").close()


class Classifier(unittest.TestCase):
    COMMIT = [("Jetzt kaufen", "hard", "pay"), ("Zahlungspflichtig bestellen", "hard", "pay"),
              ("Bestellung absenden", "hard", "pay"), ("Place order", "hard", "pay"), ("Buy now", "hard", "pay"),
              ("Pay CHF 12.90", "hard", "pay"), ("Submit order", "hard", "pay"), ("Sofort-Kaufen", "hard", "pay"),
              ("Passer la commande", "hard", "pay"), ("Acquista ora", "hard", "pay"), ("Jetzt buchen", "hard", "book"),
              ("Book now", "hard", "book"), ("Réserver", "hard", "book"), ("Prenota", "hard", "book"),
              ("Konto löschen", "hard", "delete"), ("Delete account", "hard", "delete"),
              ("Supprimer", "hard", "delete"), ("Abo kündigen", "hard", "cancel"),
              ("Cancel subscription", "hard", "cancel"), ("Résilier", "hard", "cancel"),
              ("Disdici abbonamento", "hard", "cancel"), ("Submit", "soft", "send"), ("Senden", "soft", "send"),
              ("Nachricht absenden", "soft", "send"), ("Envoyer", "soft", "send"), ("Invia", "soft", "send"),
              ("Bestätigen", "soft", "confirm"), ("Confirm", "soft", "confirm"), ("Conferma", "soft", "confirm"),
              ("Newsletter abbestellen", "soft", "unsubscribe"), ("Unsubscribe", "soft", "unsubscribe"),
              ("Se désabonner", "soft", "unsubscribe"), ("Änderungen speichern", "soft", "settings"),
              ("Save changes", "soft", "settings"), ("Konto erstellen", "soft", "signup"),
              ("Sign up", "soft", "signup"), ("Entfernen", "soft", "remove"),
              ("Kauf abschließen", "hard", "pay"), ("“Submit order” button", "hard", "pay")]
    SAFE = ["In den Warenkorb", "Zur Kasse", "Weiter", "Suchen", "Search", "Abbrechen", "Cancel", "Annulla",
            "Order history", "Purchase history", "E-Book", "Ordina per prezzo", "Save 20%", "Facebook", "PayPal",
            "Bestellübersicht", "Einkaufen", "Alle Cookies akzeptieren", "Cookie-Einstellungen speichern",
            "Submit search", "Filter löschen", "Weiter zur Zahlung", "Continue to payment", "Anmelden mit Google?", ""]

    def test_commit(self):
        for text, tier, cat in self.COMMIT:
            c = W.classify(text)
            self.assertTrue(c["commit"], text)
            self.assertEqual((c["tier"], c["category"]), (tier, cat), text)

    def test_safe(self):
        for text in self.SAFE:
            c = W.classify(text)
            if text.startswith("Anmelden"):            # log in: soft unsubscribe-ish words must not fire on it
                self.assertNotEqual(c["tier"], "hard", text)
                continue
            self.assertFalse(c["commit"], f"{text}: {c}")

    def test_abbestellen_is_not_an_order(self):
        self.assertEqual(W.classify("Abbestellen")["tier"], "soft")

    def test_cli_check_exit_codes(self):
        self.assertEqual(run("check", "Jetzt kaufen")[0], 3)
        self.assertEqual(run("check", "In den Warenkorb")[0], 0)


class Redaction(unittest.TestCase):
    def test_card_iban_cvc_secrets(self):
        card = luhn_number("4111")
        spaced = " ".join(card[i:i + 4] for i in range(0, 16, 4))
        ib = iban()
        s = W.redact(f"paid with {spaced} cvc 123, IBAN {ib}, password=hunter2x, token: abc.def eyJhbGciOiJIUzI1."
                     f"eyJzdWIiOiIxMjM0NTY3.SflKxwRJSMeKKF2QT4f and order 2026-10-21 #12345")
        for bad in (card[:8], spaced[:9], ib[4:12], "hunter2x", "eyJhbG"):
            self.assertNotIn(bad, s)
        self.assertIn("[card]", s)
        self.assertIn("[iban]", s)
        self.assertIn("#12345", s)                   # short order numbers stay

    def test_not_luhn_stays(self):
        self.assertIn("1234567890123", W.redact("ref 1234567890123"))

    def test_url(self):
        u = W.clean_url("https://user:pw@shop.example/checkout?step=2&session=abc123&token=xyz#access_token=q")
        self.assertEqual(u, "https://shop.example/checkout?step=2&session=x&token=x")
        self.assertNotIn(luhn_number("4111"), W.clean_url("https://x.example/p?q=" + luhn_number("4111")))


class Flow(unittest.TestCase):
    def setUp(self):
        reset()

    def test_dry_httpbin_end_to_end(self):
        tid = new_task("--dry", "--kind", "form")
        card = luhn_number("5500")
        self.assertEqual(run("step", tid, f"filled customer name, phone, size; note {card}", "--kind", "fill")[0], 0)
        rc, o, e = jrun("stop", tid, "--label", "Submit order", "--what", "posts the test form to httpbin (echo only)",
                        "--shot", SHOT, "--target", "button:has-text('Submit order')")
        self.assertEqual(rc, 0, e)
        c = o["card"]
        self.assertEqual(c["chat_id"], "1000001")
        n = W.load(tid)["pending"]["nonce"]
        self.assertRegex(n, r"^[0-9a-f]{32}$")
        self.assertEqual(c["buttons"][0], [{"text": "✅ Do it", "data": f"ap:{n}:y"},
                                           {"text": "❌ Stop", "data": f"ap:{n}:n"}])
        self.assertEqual(len(c["buttons"]), 1)          # dry: no live browser link
        self.assertTrue(c["text"].startswith("🧪 DRY RUN"))
        self.assertLessEqual(len(c["text"].splitlines()), 10)
        self.assertTrue(os.path.isfile(c["files"][0]) and c["files"][0].startswith(ENV["WEBAGENT_STATE"]))
        raw = read(W.path_of(tid))
        self.assertNotIn(card, raw)
        # pending: the hard click is denied, approval needs proof
        self.assertTrue(denied(W.guard(click("Submit order button"))))
        self.assertEqual(run("approve", tid)[0], 5)
        self.assertEqual(jrun("resume", tid)[0], 5)
        rc, o, e = jrun("approve", tid, "--simulate")
        self.assertEqual((rc, o["state"], o["via"]), (0, "approved", "simulated"), e)
        rc, o, _ = jrun("resume", tid)
        self.assertEqual((rc, o["execute"]), (0, True))
        self.assertTrue(denied(W.guard(click("Delete account"))))        # a different commit stays blocked
        self.assertIsNone(W.guard(click("“Submit order” button")))      # the approved one passes once
        self.assertEqual(W.load(tid)["state"], "executing")
        self.assertTrue(denied(W.guard(click("Submit order"))))          # single use
        rc, o, e = jrun("done", tid, "--shot", SHOT, "--note", "httpbin echoed the form")
        self.assertEqual((rc, o["state"], o["result"]["committed"]), (0, "done", True), e)
        self.assertTrue(o["card"]["text"].startswith("🧪 ✅ Done"))
        st = W.stats(include_dry=True)
        self.assertEqual((st["tasks"], st["completed"], st["approvals"], st["abandon_rate"]), (1, 1, 1, 0.0))
        self.assertEqual(W.stats()["tasks"], 0)                          # dry runs stay out of the real metric

    def test_live_needs_his_tap(self):
        tid = new_task("--kind", "unsubscribe", url="https://news.example/unsubscribe")
        rc, o, _ = jrun("stop", tid, "--label", "Unsubscribe", "--what", "ends the newsletter", "--shot", SHOT)
        self.assertEqual(o["card"]["buttons"][1][0]["text"], "🖥 Open my browser")
        self.assertEqual(run("approve", tid, "--simulate")[0], 3)
        journal("button", {"callback_data": f"wa:ok:{tid}", "message_id": "9"}, owner=False)   # not him
        journal("button", {"callback_data": "wa:ok:wa000000", "message_id": "9"})            # other task
        self.assertEqual(run("approve", tid)[0], 5)
        journal("button", {"callback_data": f"wa:ok:{tid}", "message_id": "10"})
        rc, o, e = jrun("approve", tid)
        self.assertEqual((rc, o["via"]), (0, "button"), e)

    def test_thumbs_on_card_and_typed(self):
        tid = new_task(url="https://shop.example/x")
        run("stop", tid, "--label", "Save changes", "--what", "saves the new delivery address", "--shot", SHOT)
        self.assertEqual(run("sent", tid, "--msg", "77")[0], 0)
        journal("reaction", {"reacted_to_message_id": "76", "reaction": "👍"})
        self.assertEqual(run("approve", tid)[0], 5)
        journal("reaction", {"reacted_to_message_id": "77", "reaction": "👍"})
        self.assertEqual(jrun("approve", tid)[1]["via"], "reaction")
        tid2 = new_task(url="https://shop.example/y")
        run("stop", tid2, "--label", "Senden", "--what", "sends the form", "--shot", SHOT)
        journal("message", {"message_id": "80"})
        self.assertEqual(jrun("approve", tid2, "--typed", "80")[1]["via"], "typed")
        journal("button", {"callback_data": "shop:buy:galaxus", "message_id": "81"}, ago=3)   # an older tap
        tid3 = new_task("--kind", "order", url="https://www.galaxus.ch/de/checkout")
        self.assertEqual(run("stop", tid3, "--label", "Jetzt kaufen", "--what", "3 items, invoice", "--shot", SHOT)[0], 2)
        run("stop", tid3, "--label", "Jetzt kaufen", "--what", "3 items, invoice", "--shot", SHOT,
            "--summary", "3 items: filters, liners, batteries", "--amount", "CHF 61.40")
        self.assertEqual(run("approve", tid3, "--button", "shop:buy:galaxus")[0], 5)
        journal("button", {"callback_data": "shop:buy:galaxus", "message_id": "82"})
        self.assertEqual(jrun("approve", tid3, "--button", "shop:buy:galaxus")[1]["via"], "button")

    def test_expiry(self):
        tid = new_task()
        run("stop", tid, "--label", "Submit", "--what", "x", "--shot", SHOT, env={"WEBAGENT_PENDING_TTL": "1"})
        time.sleep(1.3)
        journal("button", {"callback_data": f"wa:ok:{tid}", "message_id": "11"})
        self.assertEqual(run("approve", tid)[0], 4)
        self.assertEqual(W.load(tid)["state"], "expired")
        self.assertEqual(jrun("resume", tid)[0], 4)

    def test_abort_backout(self):
        tid = new_task(url="https://shop.example/cart")
        run("step", tid, "added the item to the cart", "--kind", "click", "--side-effect", "1 item in the cart")
        run("stop", tid, "--label", "Jetzt kaufen", "--what", "orders 1 item", "--shot", SHOT)
        rc, o, _ = jrun("abort", tid)
        self.assertEqual((rc, o["close_tab_url"], o["side_effects"]), (0, "https://shop.example/cart",
                                                                     ["1 item in the cart"]))
        journal("button", {"callback_data": f"wa:ok:{tid}", "message_id": "12"})
        self.assertEqual(run("approve", tid)[0], 6)

    def test_dry_on_a_real_shop_never_clicks(self):
        tid = new_task("--dry", url="https://www.galaxus.ch/de/s1/product/x")
        run("stop", tid, "--label", "In den Warenkorb und bestellen", "--what", "x", "--shot", SHOT)
        run("approve", tid, "--simulate")
        rc, o, _ = jrun("resume", tid)
        self.assertEqual((rc, o["execute"]), (0, False))
        self.assertIn("dry run", o["why"])

    def test_continue_after_commit(self):
        tid = new_task("--dry")
        run("stop", tid, "--label", "Submit", "--what", "x", "--shot", SHOT)
        run("approve", tid, "--simulate")
        W.guard(click("Submit button"))
        rc, o, _ = jrun("done", tid, "--continue")
        self.assertEqual(o["state"], "running")
        self.assertIsNone(W.load(tid)["pending"])

    def test_usage_events(self):
        spool = os.path.join(ENV["USAGE_STATE"], "spool")
        shutil.rmtree(spool, ignore_errors=True)
        tid = new_task(url="https://httpbin.org/forms/post")
        run("stop", tid, "--label", "Submit order", "--what", "x", "--shot", SHOT)
        run("sent", tid, "--msg", "90")
        journal("button", {"callback_data": f"wa:ok:{tid}", "message_id": "91"})
        run("approve", tid)
        W.guard(click("Submit order"))
        run("done", tid)
        kinds = []
        for p in os.listdir(spool):
            for line in read(os.path.join(spool, p)).splitlines():
                d = json.loads(line)
                if d.get("feature") == W.FEATURE or d.get("f") == W.FEATURE:
                    kinds.append(d.get("kind") or d.get("k"))
        self.assertEqual(kinds, ["invoke", "notify_sent", "tap", "exec"], kinds)


class Approvals(unittest.TestCase):
    """v2 P7: each stop's one-time approval (ap:<nonce>:y|n on the card): his tap from the inbound journal (poll mode)
    or decided by tg-adapter (bridge mode), consumed once for exactly that stop; ❌, replaced cards and aborts settle
    it; without the approvals library the card keeps wa:ok/wa:no."""

    def setUp(self):
        reset()

    def stop(self, tid, *extra, env=None):
        rc, o, e = jrun("stop", tid, "--label", "Unsubscribe", "--what", "ends the newsletter", "--shot", SHOT, *extra,
                        env=env)
        self.assertEqual(rc, 0, e)
        return o, W.load(tid)["pending"]["nonce"]

    def test_poll_mode_tap_is_consumed_once(self):
        tid = new_task("--kind", "unsubscribe", url="https://news.example/unsubscribe?u=1", title="Test task")
        o, n = self.stop(tid)
        self.assertEqual(o["card"]["buttons"][0], [{"text": "✅ Do it", "data": f"ap:{n}:y"},
                                                   {"text": "❌ Stop", "data": f"ap:{n}:n"}])
        r = ap_row(n)
        self.assertEqual((r["status"], r["ref"], r["summary"]), ("pending", f"webagent:{tid}", "“Unsubscribe” · Test task"))
        self.assertAlmostEqual(r["expires"] - r["created"], 900, delta=1)
        self.assertAlmostEqual(W.load(tid)["expires"], r["expires"], delta=2)
        with open(ap_db(), "rb") as f:
            self.assertNotIn(b"news.example", f.read())                 # only the action's hash is stored
        self.assertEqual(os.stat(ap_db()).st_mode & 0o777, 0o600)
        self.assertEqual(run("sent", tid, "--msg", "70")[0], 0)
        self.assertEqual((ap_row(n)["chat"], ap_row(n)["message_id"]), ("1000001", 70))
        self.assertEqual(run("approve", tid)[0], 5)                       # no tap yet
        ap_tap(n, user="1000002")                                         # allowlisted, not him
        ap_tap(n, chat="-100123")                                         # his id, another chat
        ap_tap(n, owner=False)
        self.assertEqual(run("approve", tid)[0], 5)
        ap_tap(n)
        rc, o, e = jrun("approve", f"ap:{n}:y")                           # the callback data finds the task
        self.assertEqual((rc, o["id"], o["via"]), (0, tid, "button"), e)
        r = ap_row(n)
        self.assertEqual((r["status"], r["decided_via"], r["used_by"]), ("approved", "journal", f"webagent:{tid}"))
        self.assertIsNotNone(r["used_ts"])
        self.assertEqual(W.load(tid)["approval"]["nonce"], n)
        self.assertEqual(run("approve", tid)[0], 6)                       # approved already: no second use
        self.assertIsNone(W.guard(click("Unsubscribe")))                  # the approved click passes once
        self.assertTrue(denied(W.guard(click("Unsubscribe"))))

    def test_bridge_mode_decision_by_the_adapter(self):
        tid = new_task(url="https://forms.example/b")
        _, n = self.stop(tid)
        con = sqlite3.connect(ap_db())                                    # what tg-adapter writes on his tap
        con.execute("UPDATE approvals SET status = 'approved', decided_ts = ?, decided_by = '1000001', "
                    "decided_via = 'adapter', chat = '1000001', message_id = 71 WHERE nonce = ?", (time.time(), n))
        con.commit()
        con.close()
        rc, o, e = jrun("approve", tid)
        self.assertEqual((rc, o["via"]), (0, "button"), e)
        self.assertEqual(ap_row(n)["used_by"], f"webagent:{tid}")
        self.assertEqual(W.load(tid)["approval"]["msg"], "71")

    def test_his_decline_settles_it(self):
        tid = new_task(url="https://forms.example/d")
        _, n = self.stop(tid)
        run("sent", tid, "--msg", "72")
        ap_tap(n, "n", msg="72")
        journal("button", {"callback_data": f"wa:ok:{tid}", "message_id": "72"})   # an older-style yes
        journal("reaction", {"reacted_to_message_id": "72", "reaction": "\U0001f44d"})
        rc, _, e = run("approve", tid)
        self.assertEqual(rc, 5)
        self.assertIn("declined", e)
        rc, o, e = jrun("abort", f"ap:{n}:n")
        self.assertEqual(rc, 0, e)
        self.assertEqual(W.load(tid)["stopped_by"], "button")
        self.assertEqual(ap_row(n)["status"], "denied")

    def test_restop_and_abort_cancel_the_open_nonce(self):
        tid = new_task(url="https://forms.example/r")
        _, n1 = self.stop(tid)
        _, n2 = self.stop(tid)
        self.assertNotEqual(n1, n2)
        self.assertEqual((ap_row(n1)["status"], ap_row(n2)["status"]), ("cancelled", "pending"))
        ap_tap(n1)                                                        # a tap on the replaced card
        self.assertEqual(run("approve", tid)[0], 5)
        self.assertEqual(run("approve", f"ap:{n1}:y")[0], 2)              # no task carries the replaced nonce
        run("abort", tid, "--reason", "agent")
        self.assertEqual(ap_row(n2)["status"], "cancelled")

    def test_the_approval_covers_exactly_that_step(self):
        tid = new_task(url="https://forms.example/a")
        _, n = self.stop(tid)
        ap_tap(n)
        t = W.load(tid)
        t["pending"]["label"] = "Delete account"                         # the step changed after his ✅
        W.save(t)
        rc, _, e = run("approve", tid)
        self.assertEqual(rc, 5)
        self.assertIn("another action", e)
        self.assertEqual((ap_row(n)["refused"], ap_row(n)["used_ts"]), (1, None))

    def test_expired(self):
        tid = new_task(url="https://forms.example/e")
        _, n = self.stop(tid, env={"WEBAGENT_PENDING_TTL": "1"})
        time.sleep(1.3)
        ap_tap(n)
        self.assertEqual(run("approve", tid)[0], 4)
        r = ap_row(n)
        self.assertLess(r["expires"], time.time())
        self.assertIsNone(r["used_ts"])

    def test_without_the_approvals_library_the_card_keeps_wa_buttons(self):
        lib = os.path.join(TMP, "lib-noap")
        os.makedirs(lib, exist_ok=True)
        shutil.copy(os.path.join(LIB, "webagent.py"), os.path.join(lib, "webagent.py"))   # a copy: it finds its own dir
        tid = new_task(url="https://forms.example/n")
        o, n = self.stop(tid, env={"WEBAGENT_LIB": lib, "WEBAGENT_USAGE_LIB": lib})
        self.assertIsNone(n)
        self.assertEqual(o["card"]["buttons"][0][0]["data"], f"wa:ok:{tid}")
        journal("button", {"callback_data": f"wa:ok:{tid}", "message_id": "73"})
        self.assertEqual(run("approve", tid)[0], 0)


def age(tid, seconds, **fields):
    """Make the task look idle for `seconds` (and set fields) without touching the clock (save(touch=False))."""
    t = W.load(tid)
    t["updated"] = time.time() - seconds
    t.update(fields)
    W.save(t, touch=False)
    return t


class FakeSessions:
    """W._fill_session stand-in: the fill session's state per op 'state', the session task list, the 'end' calls."""

    def __init__(self, state=None, tasks=()):
        self.state, self.tasks, self.ended = state, list(tasks), []

    def __call__(self, op, t, **kw):
        if op == "state":
            return self.state
        if op == "tasks":
            return list(self.tasks)
        if op == "end":
            self.ended.append((t["id"], kw.get("why")))
            return {"state": "ended", "note": None}
        return None


class Waiting(unittest.TestCase):
    """A running task waiting for his ✅ in the vault bot (securefill fill / session start exit 10) does not go stale
    before the request's deadline; his decision ends the wait; a stale task with a live fill session can be revived
    (explicit, logged) within 2 h instead of being redone."""

    def setUp(self):
        reset()
        self.real_fs = W._fill_session

    def tearDown(self):
        W._fill_session = self.real_fs

    def test_the_wait_pauses_the_stale_clock(self):
        tid = new_task(url="https://shop.example/checkout")
        age(tid, W.RUNNING_TTL + 300)
        self.assertEqual(W.effective(W.load(tid)), "stale")                 # without a wait: stale
        age(tid, 0)
        self.assertIsNotNone(W.wait_owner(tid, time.time() + 600, "his ✅ in the vault bot"))
        t = age(tid, W.RUNNING_TTL + 300)
        self.assertEqual(W.effective(t), "running")                          # waiting: not stale
        rc, o, e = jrun("status", tid)
        self.assertEqual((rc, o["state"], o["waiting"]["for"]), (0, "running", "his ✅ in the vault bot"), e)
        self.assertEqual(o["waiting"]["until"], W.fmt_local(t["waiting"]["until"]))
        rc, o, _ = run("status")
        self.assertIn("waiting for his vault ✅ until", o)
        self.assertEqual(run("step", tid, "read the checkout page", "--kind", "read")[0], 0)   # the agent can go on
        self.assertEqual(W.load(tid)["state"], "running")
        # past the deadline the 45 min count from it, not from the last update
        t = age(tid, W.RUNNING_TTL + 300, waiting=dict(W.load(tid)["waiting"], until=time.time() - 60))
        self.assertEqual(W.effective(t), "running")
        self.assertIsNone(W.public(t)["waiting"])                           # the deadline is over: not shown
        t = age(tid, W.RUNNING_TTL + 300, waiting=dict(t["waiting"], until=time.time() - W.RUNNING_TTL - 60))
        self.assertEqual(W.effective(t), "stale")
        self.assertEqual(run("step", tid, "x")[0], 6)

    def test_only_a_running_task_waits_and_never_long(self):
        tid = new_task(url="https://shop.example/a")
        at = time.time()
        W.wait_owner(tid, at + 99999)
        self.assertLessEqual(W.load(tid)["waiting"]["until"], at + W.WAIT_MAX + 5)
        W.wait_owner(tid, None)                                              # unknown deadline: the vault's 15 min
        w = W.load(tid)["waiting"]
        self.assertAlmostEqual(w["until"], time.time() + W.WAIT_DEFAULT, delta=5)
        self.assertEqual(sum(1 for h in W.load(tid)["history"] if h[3].startswith("waiting")), 1)   # logged once
        run("stop", tid, "--label", "Submit", "--what", "x", "--shot", SHOT)
        t = W.load(tid)
        self.assertNotIn("waiting", t)                                       # any move ends the wait
        self.assertIsNone(W.wait_owner(tid, at + 600))                       # pending: the card's 15 min stay
        self.assertNotIn("waiting", W.load(tid))
        self.assertLess(W.load(tid)["expires"], time.time() + W.PENDING_TTL + 5)

    def test_his_decision_ends_the_wait(self):
        tid = new_task(url="https://shop.example/b")
        W.wait_owner(tid, time.time() + 600)
        age(tid, 100)
        self.assertIsNotNone(W.wait_over(tid, "active"))                    # the agent's own call: clock restarts now
        t = W.load(tid)
        self.assertNotIn("waiting", t)
        self.assertAlmostEqual(t["updated"], time.time(), delta=5)
        self.assertEqual(t["history"][-1][2:], ["running", "wait over: active"])
        self.assertIsNone(W.wait_over(tid, "active"))                       # no wait: nothing written
        # sweep: his decision while nobody runs a fill; an expired request keeps the clock where it was
        W.wait_owner(tid, time.time() + 600)
        dl = time.time() - 120
        age(tid, 1000, waiting=dict(W.load(tid)["waiting"], until=dl))
        W._fill_session = FakeSessions("pending")
        W.sweep()
        self.assertIn("waiting", W.load(tid))                                # still undecided
        W._fill_session = FakeSessions("expired")
        W.sweep()
        t = W.load(tid)
        self.assertNotIn("waiting", t)
        self.assertAlmostEqual(t["updated"], dl, delta=2)                    # not extended by the sweep
        W.wait_owner(tid, time.time() + 600)
        age(tid, 1000)
        W._fill_session = FakeSessions("active")
        W.sweep()
        self.assertAlmostEqual(W.load(tid)["updated"], time.time(), delta=5)   # his ✅: time to resume the agent

    def test_securefill_marks_and_ends_the_wait(self):
        import importlib.util                       # the copy in LIB, whatever sys.path holds by now
        spec = importlib.util.spec_from_file_location("securefill_under_test", os.path.join(LIB, "securefill.py"))
        securefill = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(securefill)
        tid = new_task(url="https://shop.example/c")
        dl = time.time() + 700
        iso = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(dl))
        securefill.task_wait(tid, {"waiting": True, "session": "pending", "decide_by": iso}, 10)
        self.assertAlmostEqual(W.load(tid)["waiting"]["until"], dl, delta=2)
        securefill.task_wait(tid, {"session": "pending"}, 3)                # a vault error: still waiting
        self.assertIn("waiting", W.load(tid))
        securefill.task_wait(tid, {"session": "denied"}, 5)
        self.assertNotIn("waiting", W.load(tid))
        self.assertEqual(W.load(tid)["history"][-1][3], "wait over: denied")

    def test_revive_a_stale_task_with_a_live_session(self):
        spool = os.path.join(ENV["USAGE_STATE"], "spool")
        shutil.rmtree(spool, ignore_errors=True)
        tid = new_task(url="https://shop.example/d")
        run("step", tid, "filled the passenger page", "--kind", "fill")
        age(tid, W.RUNNING_TTL + 600)
        rc, _, e = run("step", tid, "x")
        self.assertEqual(rc, 6)
        self.assertIn(f"webagent revive {tid}", e)                         # the refusal names the way back
        t = W.load(tid)
        self.assertEqual(t["state"], "stale")
        self.assertAlmostEqual(t["stale_at"], time.time() - 600, delta=5)  # when it went stale, not when written
        self.assertIn(f"webagent revive {tid}", jrun("resume", tid)[1]["why"])
        rc, o, _ = jrun("status", tid)
        self.assertEqual((o["state"], o["revive_until"]), ("stale", W.fmt_local(t["stale_at"] + W.REVIVE_WINDOW)))
        self.assertIn(tid, run("status")[1])                                # listed while revivable
        rc, _, e = run("revive", tid)                                       # no fill session (hooks off): refused
        self.assertEqual(rc, 6)
        self.assertIn("fill session is gone", e)
        W._fill_session = FakeSessions("denied")
        with self.assertRaises(SystemExit) as cm:
            W.revive(tid)
        self.assertEqual(cm.exception.code, 6)
        W._fill_session = FakeSessions("active")
        t = W.revive(tid)
        self.assertEqual(t["state"], "running")
        self.assertEqual(t["history"][-1][1:], ["stale", "running", "revive (fill session active)"])
        self.assertEqual(t["revived"][-1]["fill_session"], "active")
        self.assertNotIn("stale_at", t)
        self.assertEqual(run("step", tid, "back on the passenger page", "--kind", "read")[0], 0)
        with self.assertRaises(SystemExit) as cm:
            W.revive(tid)                                                   # running: nothing to revive
        self.assertEqual(cm.exception.code, 6)
        evs = [json.loads(x) for p in os.listdir(spool) for x in read(os.path.join(spool, p)).splitlines()]
        kinds = [d.get("kind") or d.get("k") for d in evs if W.FEATURE in (d.get("feature"), d.get("f"))]
        self.assertIn("run", kinds)
        # a revive is a healthy run: outcome ok, the state rides in attrs (any other run outcome counts as a failed
        # run + an error in the usage store)
        runs = [(d.get("o"), (d.get("at") or {}).get("state")) for d in evs if d.get("f") == W.FEATURE and d.get("k") == "run"]
        self.assertEqual(runs, [("ok", "revived")])
        # the stop after a revive still needs its own card and his ✅
        run("stop", tid, "--label", "Jetzt bezahlen", "--what", "pays", "--shot", SHOT, "--summary", "x",
            "--amount", "CHF 1.00")
        self.assertEqual(run("approve", tid)[0], 5)

    def test_revive_limits(self):
        W._fill_session = FakeSessions("active")
        late = new_task(url="https://shop.example/e")
        age(late, W.RUNNING_TTL + W.REVIVE_WINDOW + 60)
        with self.assertRaises(SystemExit) as cm:
            W.revive(late)                                                  # stale for over 2 h
        self.assertEqual(cm.exception.code, 4)
        ex = new_task("--dry")
        run("stop", ex, "--label", "Submit", "--what", "x", "--shot", SHOT)
        run("approve", ex, "--simulate")
        W.guard(click("Submit button"))
        self.assertEqual(W.load(ex)["state"], "executing")
        age(ex, W.RUNNING_TTL + 60)
        with self.assertRaises(SystemExit) as cm:
            W.revive(ex)                                                    # the approved step may have run
        self.assertEqual(cm.exception.code, 6)
        # an older record (no stale_at): the timeout's history entry counts
        old = new_task(url="https://shop.example/f")
        t = W.load(old)
        t["state"] = "stale"
        t["history"].append([round(time.time() - 300, 1), "running", "stale", "timeout"])
        W.save(t, touch=False)
        self.assertAlmostEqual(W.revive_until(W.load(old)), time.time() - 300 + W.REVIVE_WINDOW, delta=2)
        self.assertEqual(W.revive(old)["state"], "running")

    def test_sweep_keeps_a_revivable_session_and_abort_ends_it(self):
        keep = new_task(url="https://shop.example/g")
        age(keep, W.RUNNING_TTL + 60)
        gone = new_task(url="https://shop.example/h")
        age(gone, W.RUNNING_TTL + W.REVIVE_WINDOW + 60)
        fs = FakeSessions("active", tasks=[keep, gone])
        W._fill_session = fs
        moved = {m["id"]: m["state"] for m in W.sweep()}
        self.assertEqual(moved, {keep: "stale", gone: "stale"})
        self.assertEqual(fs.ended, [(gone, "expired")])                    # past the window: ended (report follows)
        rc, o, e = jrun("abort", keep, "--reason", "agent")                # the main session lets it go
        self.assertEqual((rc, o["was"]), (0, "stale"), e)
        self.assertEqual(W.load(keep)["state"], "aborted")


def usage_rows():
    """(kind, outcome) of the web-agent usage events in the spool, oldest first."""
    spool, rows = os.path.join(ENV["USAGE_STATE"], "spool"), []
    for p in sorted(os.listdir(spool)) if os.path.isdir(spool) else []:
        for line in read(os.path.join(spool, p)).splitlines():
            d = json.loads(line)
            if d.get("feature") == W.FEATURE or d.get("f") == W.FEATURE:
                rows.append((d.get("kind") or d.get("k"), d.get("outcome") or d.get("o")))
    return rows


class Retries(unittest.TestCase):
    """24.10: a retry of the same job (same title + site) supersedes the unfinished earlier attempt instead of leaving
    it to go stale or closing it as his stop; stats count jobs, causes and dry-run guard denies apart."""

    def setUp(self):
        reset()
        shutil.rmtree(os.path.join(ENV["USAGE_STATE"], "spool"), ignore_errors=True)
        W._fill_session = FakeSessions(None)

    def test_a_retry_supersedes_the_running_or_stale_attempt(self):
        a = new_task("--kind", "booking", url="https://shop.air.example/book", title="Flight BRU-MXP 6.11")
        run("step", a, "filled the passenger page", "--kind", "fill")
        rc, o, e = jrun("start", "Flight BRU-MXP 6.11", "--url", "https://pay.air.example/x", "--kind", "booking",
                        "--json")
        self.assertEqual(rc, 0, e)
        b = o["id"]
        ta, tb = W.load(a), W.load(b)
        self.assertEqual((ta["state"], ta["history"][-1][3], ta["retried_as"]), ("aborted", "retry", b))
        self.assertEqual(tb["retry_of"], a)
        self.assertNotIn(("tap", "stop"), usage_rows())                  # not counted as his stop
        self.assertIn(("abandoned", "retry"), usage_rows())
        age(b, W.RUNNING_TTL + 60)                                          # the second attempt goes stale too
        c = new_task("--kind", "booking", url="https://shop.air.example/book", title="flight  bru-mxp 6.11")
        self.assertEqual((W.load(b)["state"], W.load(b)["retried_as"]), ("aborted", c))
        self.assertEqual(W.load(c)["retry_of"], b)
        run("done", c, "--note", "booked")
        st = W.stats(by_cause=True)
        self.assertEqual((st["tasks"], st["completed"], st["jobs"], st["jobs_completed"], st["retried"], st["stale"],
                          st["owner_stops"]), (3, 1, 1, 1, 2, 0, 0))
        self.assertEqual(st["causes"], {"superseded": 2})
        self.assertEqual({r["id"]: (r["job"], r["retried_as"], r["site"]) for r in st["unfinished"]},
                         {a: (a, b, "air.example"), b: (a, c, "air.example")})
        d = new_task("--kind", "booking", url="https://shop.air.example/book", title="Flight BRU-MXP 6.11")
        self.assertIsNone(W.load(d)["retry_of"])                            # after a done attempt: a new job
        self.assertEqual(W.stats()["jobs"], 2)

    def test_auto_refuses_what_only_the_session_should_end(self):
        a = new_task("--kind", "order", url="https://shop.example/cart", title="Order filters")
        run("stop", a, "--label", "Buy now", "--what", "orders", "--shot", SHOT, "--summary", "2 filters",
            "--amount", "CHF 20.00")
        n = W.load(a)["pending"]["nonce"]
        rc, o, e = run("start", "Order filters", "--url", "https://shop.example/cart", "--kind", "order")
        self.assertEqual(rc, 6, e)                                          # an open card: stop again or replace it
        self.assertIn(f"webagent stop {a}", e)
        self.assertEqual(W.load(a)["state"], "pending")
        rc, o, e = run("start", "Order filters", "--url", "https://shop.example/cart", "--kind", "order",
                       "--retry-of", "none")
        self.assertEqual(rc, 0, e)                                          # a separate task: nothing touched
        sep = o.strip()
        self.assertEqual((W.load(a)["state"], W.load(sep)["retry_of"]), ("pending", None))
        run("abort", sep, "--reason", "agent")
        rc, o, e = run("start", "Order filters", "--url", "https://shop.example/cart", "--kind", "order",
                       "--retry-of", a)
        self.assertEqual(rc, 0, e)                                          # explicit: the card is cancelled
        self.assertEqual((W.load(a)["state"], W.load(a)["history"][-1][3]), ("aborted", "retry"))
        if n:
            self.assertEqual(ap_row(n)["status"], "cancelled")
        # a stale attempt whose fill session is still live: revive it (no second vault ✅)
        s = new_task("--kind", "booking", url="https://hotel.example/b", title="Hotel 21.11")
        age(s, W.RUNNING_TTL + 60, fill_session={"state": "active", "note": None})
        rc, o, e = run("start", "Hotel 21.11", "--url", "https://hotel.example/b", "--kind", "booking")
        self.assertEqual(rc, 6, e)
        self.assertIn(f"webagent revive {s}", e)
        # approved: let it finish
        p = new_task("--dry", url="https://httpbin.org/forms/post", title="Dry form")
        run("stop", p, "--label", "Submit", "--what", "x", "--shot", SHOT)
        run("approve", p, "--simulate")
        rc, o, e = run("start", "Dry form", "--url", "https://httpbin.org/forms/post", "--dry")
        self.assertEqual(rc, 6, e)
        self.assertIn("approved", e)
        rc, o, e = run("start", "Dry form", "--url", "https://httpbin.org/forms/post")   # live is another job
        self.assertEqual(rc, 0, e)
        self.assertIsNone(W.load(o.strip())["retry_of"])

    def test_a_card_ended_without_his_no_is_not_his_stop(self):
        a = new_task("--kind", "unsubscribe", url="https://news.example/u")
        run("stop", a, "--label", "Unsubscribe", "--what", "ends the newsletter", "--shot", SHOT)
        run("sent", a, "--msg", "301")
        rc, o, e = jrun("abort", a)                                         # default reason owner, no tap from him
        self.assertEqual((rc, o["stopped_by"]), (0, "session"), e)
        self.assertIn(("abandoned", "session"), usage_rows())
        self.assertNotIn(("tap", "stop"), usage_rows())
        b = new_task("--kind", "unsubscribe", url="https://news2.example/u")
        run("stop", b, "--label", "Unsubscribe", "--what", "ends the newsletter", "--shot", SHOT)
        run("sent", b, "--msg", "302")
        n = W.load(b)["pending"].get("nonce")
        if n:
            ap_tap(n, "n", msg="302")
        else:
            journal("button", {"callback_data": f"wa:no:{b}", "message_id": "302"})
        rc, o, e = jrun("abort", b)
        self.assertEqual(rc, 0, e)
        self.assertNotEqual(o["stopped_by"], "session")
        self.assertIn(("tap", "stop"), usage_rows())
        st = W.stats(by_cause=True)
        self.assertEqual((st["owner_stops"], st["session_stops"], st["abandon_rate"]), (1, 1, 0.5))
        self.assertEqual(st["causes"], {"session_stop": 1, "owner_stop": 1})

    def test_older_records_group_by_title_and_site_and_dry_denies_apart(self):
        os.makedirs(ENV["WEBAGENT_STATE"], exist_ok=True)
        t0 = time.time() - 3 * 86400
        base = {"v": 1, "title": "Flight", "kind": "booking", "mode": "live", "url": "https://shop.air.example/",
                "start_host": None, "agent": None, "shots": [], "pending": None, "approval": None, "result": None,
                "card_msg": None, "stops": 0}

        def rec(tid, at, state, steps, history, **kw):
            t = dict(base, id=tid, created=at, updated=at + 60 * max(len(steps) - 1, 0), state=state, history=history,
                     steps=[{"t": at + 60 * i, "kind": k, "desc": "x"} for i, k in enumerate(steps)], **kw)
            W.save(t, touch=False)

        rec("wa000001", t0, "running", ["navigate", "fill"], [])           # never closed: stale after a fill
        rec("wa000002", t0 + 9000, "aborted", ["fill"], [[t0 + 9100, "running", "pending", "confirm"],
                                                         [t0 + 9160, "pending", "aborted", "owner"]],
            stops=1, stopped_by="button", pending={"tier": "soft", "category": "confirm"})
        rec("wa000003", t0 + 9300, "aborted", ["fill"], [[t0 + 9400, "running", "pending", "pay"],
                                                         [t0 + 10400, "pending", "aborted", "owner"]],
            stops=1, stopped_by="session", pending={"tier": "hard", "category": "pay"})
        rec("wa000004", t0 + 12000, "done", ["fill", "click"], [[t0 + 12300, "running", "pending", "pay"],
                                                                [t0 + 12400, "pending", "approved", "button"],
                                                                [t0 + 12500, "executing", "done", "ok"]],
            stops=1, result={"ok": True, "committed": True})
        rec("wa000005", t0 + 20000, "running", ["read"], [[t0 + 20100, "running", "running", "waiting: fill session"]],
            title="Order", url="https://parts.example/cart", kind="order")
        rec("wa000006", t0 + 30000, "done", [], [[t0 + 30010, "running", "pending", "pay"],
                                                 [t0 + 30020, "pending", "approved", "simulated"],
                                                 [t0 + 30030, "executing", "done", "ok"]], mode="dry",
            url="https://httpbin.org/forms/post", title="Dry")
        with open(os.path.join(ENV["WEBAGENT_STATE"], "guard.log"), "w") as f:
            for at, agent in ((t0 + 9500, False), (t0 + 30015, True)):       # old records: no dry flag
                f.write(json.dumps({"tool": "click", "agent": agent, "active": 0, "tier": "hard", "cat": "pay",
                                    "decision": "deny", "t": at}) + "\n")
        st = W.stats(by_cause=True)
        self.assertEqual((st["tasks"], st["completed"], st["jobs"], st["jobs_completed"]), (5, 1, 2, 1))
        self.assertEqual((st["owner_stops"], st["session_stops"], st["stale"]), (1, 1, 2))
        self.assertEqual(st["causes"], {"stale_after_fill": 1, "owner_stop": 1, "session_stop": 1,
                                        "stale_after_wait": 1})
        self.assertEqual((st["guard_blocks"], st["guard_blocks_dry"]), (1, 1))
        self.assertEqual([g["caller"] for g in st["guard"]], ["main", "agent"])
        rows = {r["id"]: r for r in st["unfinished"]}
        self.assertEqual((rows["wa000001"]["job"], rows["wa000001"]["retried_as"], rows["wa000001"]["idle_min"]),
                         ("wa000001", "wa000002", 45))
        self.assertTrue(rows["wa000003"]["job_done"])
        self.assertFalse(rows["wa000005"]["job_done"])
        for r in st["unfinished"]:                                          # structured fields only
            self.assertFalse({"title", "url", "label", "desc", "what"} & set(r))
        rc, out, e = run("stats", "--by-cause")
        self.assertEqual(rc, 0, e)
        self.assertIn("jobs (a task + its retries): 2 · 1 completed", out)
        self.assertNotIn("Flight", out)
        # new deny records carry the flag: a rehearsal's block is not a live block
        d = new_task("--dry", url="https://httpbin.org/forms/post")
        self.assertTrue(denied(W.guard(click("Jetzt kaufen"))))
        with open(os.path.join(ENV["WEBAGENT_STATE"], "guard.log")) as f:
            self.assertIs(json.loads(f.readlines()[-1])["dry"], True)
        run("abort", d, "--reason", "agent")
        self.assertTrue(denied(W.guard(click("Jetzt kaufen"))))
        with open(os.path.join(ENV["WEBAGENT_STATE"], "guard.log")) as f:
            self.assertIs(json.loads(f.readlines()[-1])["dry"], False)
        self.assertEqual((W.stats()["guard_blocks"], W.stats()["guard_blocks_dry"]), (2, 2))


class Guard(unittest.TestCase):
    def setUp(self):
        reset()

    def test_idle_tiers(self):
        self.assertIsNone(W.guard(click("Send message")))
        self.assertTrue(denied(W.guard(click("Jetzt kaufen"))))
        self.assertTrue(denied(W.guard(click("anything", target="button.buy-now"))))
        self.assertIsNone(W.guard(click("Alle Cookies akzeptieren")))
        self.assertIsNone(W.guard({"tool_name": "mcp__browser__browser_press_key", "tool_input": {"key": "Enter"}}))
        self.assertIsNone(W.guard({"tool_name": "Bash", "tool_input": {"command": "echo buy now"}}))

    def test_active_task_tiers(self):
        new_task()
        ev = lambda n, ti: {"tool_name": f"mcp__browser__browser_{n}", "tool_input": ti}  # noqa: E731
        self.assertTrue(denied(W.guard(click("Send message"))))
        self.assertTrue(denied(W.guard(ev("press_key", {"key": "Enter"}))))
        self.assertIsNone(W.guard(ev("press_key", {"key": "Tab"})))
        self.assertTrue(denied(W.guard(ev("evaluate", {"function": "() => document.forms[0].requestSubmit()"}))))
        self.assertIsNone(W.guard(ev("evaluate", {"function": "() => location.href"})))
        self.assertTrue(denied(W.guard(ev("mouse_click_xy", {"x": 10, "y": 10}))))
        self.assertIsNone(W.guard(ev("type", {"element": "Search box", "target": "e3", "text": "x", "submit": True})))
        self.assertTrue(denied(W.guard(ev("type", {"element": "Message", "target": "e4", "text": "x", "submit": True}))))
        self.assertIsNone(W.guard(ev("type", {"element": "Message", "target": "e4", "text": "x"})))
        self.assertIsNone(W.guard(click("Weiter")))

    def test_booking_soft_steps_run_hard_stops(self):
        tid = new_task("--kind", "booking", url="https://shop.example/booking")
        for soft in ("Confirm and continue", "Submit passenger details", "Bestätigen", "Save details", "Weiter"):
            self.assertIsNone(W.guard(click(soft)), soft)                 # intermediate steps run unasked
        for hard in ("Pay now", "Book now", "Jetzt kaufen", "Delete account", "Cancel booking"):
            self.assertTrue(denied(W.guard(click(hard))), hard)          # hard commits still need his ✅
        self.assertTrue(denied(W.guard(click("Sign up for our newsletter"))))   # signup is not an auto step
        self.assertTrue(denied(W.guard(click("I accept the fare rules checkbox"))))  # boxes: --tick on the card
        other = new_task("--kind", "form", url="https://forms.example/x")       # another task's soft step: stop
        self.assertTrue(denied(W.guard(click("Confirm and continue"))))
        run("abort", other, "--reason", "agent")
        self.assertIsNone(W.guard(click("Confirm and continue")))
        rc, o, e = jrun("stop", tid, "--label", "Pay now", "--what", "pays the flight", "--shot", SHOT,
                        "--amount", "CHF 127.00")
        self.assertEqual(rc, 2, o)                                        # the final card needs a summary
        rc, o, e = jrun("stop", tid, "--label", "Pay now", "--what", "pays the flight", "--shot", SHOT, "--card",
                        "--amount", "CHF 127.00", "--summary", "AZ 153 BRU-LIN Fri 6.11 20:15, 1 adult, Light fare",
                        "--declined", "insurance, seat, bags", "--tick", "I accept the fare rules",
                        "--tick", "I have read the privacy notice")
        self.assertEqual(rc, 0, e)
        txt = o["card"]["text"]
        for want in ("AZ 153 BRU-LIN", "Declined: insurance, seat, bags", "Total: CHF 127.00",
                     "Ticks: I accept the fare rules; I have read the privacy notice", "and ticks these boxes"):
            self.assertIn(want, txt)
        self.assertTrue(denied(W.guard(click("Pay now"))))                # pending: still nothing without his tap
        st = W.load(tid)
        self.assertEqual(st["pending"]["ticks"], ["I accept the fare rules", "I have read the privacy notice"])

    def test_restop_replaces_card(self):
        tid = new_task("--kind", "booking", url="https://shop.example/b2")
        run("stop", tid, "--label", "Pay now", "--what", "pays", "--shot", SHOT, "--card", "--amount", "CHF 9.00",
            "--summary", "one test seat")
        run("sent", tid, "--msg", "100")
        time.sleep(1.1)
        rc, o, e = jrun("stop", tid, "--label", "Pay now", "--what", "pays", "--shot", SHOT, "--card", "--amount",
                        "CHF 9.00", "--summary", "one test seat", "--tick", "I accept the fare rules")
        self.assertEqual(rc, 0, e)
        self.assertIn("Ticks: I accept the fare rules", o["card"]["text"])
        run("sent", tid, "--msg", "101")
        journal("button", {"callback_data": f"wa:ok:{tid}", "message_id": "100"})
        self.assertEqual(run("approve", tid)[0], 5)                        # the old card's tap doesn't count
        journal("button", {"callback_data": f"wa:ok:{tid}", "message_id": "101"})
        self.assertEqual(run("approve", tid)[0], 0)

    def test_tick_label_checks(self):
        tid = new_task("--kind", "form", url="https://forms.example/t")
        card = luhn_number("4111")
        rc, o, e = jrun("stop", tid, "--label", "Submit", "--what", "x", "--shot", SHOT, "--tick", f"card {card}")
        self.assertEqual(rc, 2)
        rc, o, e = jrun("stop", tid, "--label", "Submit", "--what", "x", "--shot", SHOT, "--tick", "x" * 301)
        self.assertEqual(rc, 2)
        rc, o, e = jrun("stop", tid, "--label", "Submit", "--what", "x", "--shot", SHOT, "--tick", "I agree  to the terms")
        self.assertEqual(rc, 0)
        self.assertIn("Ticks: I agree to the terms", o["card"]["text"])
        journal("button", {"callback_data": f"wa:ok:{tid}", "message_id": "90"})
        run("approve", tid)
        r = jrun("resume", tid)[1]
        self.assertEqual(r["ticks"], ["I agree to the terms"])
        self.assertIn(f"securefill tick {tid}", r["why"])

    def test_hook_wrapper(self):
        rc, out = hook(click("Zahlungspflichtig bestellen"))
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(hook(click("In den Warenkorb")), (0, ""))
        self.assertEqual(hook("not json"), (0, ""))
        self.assertEqual(hook(click("Jetzt kaufen"), env={"WEBAGENT_LIB": os.path.join(TMP, "missing")}), (0, ""))

    def test_guard_log_has_no_labels(self):
        W.guard(click("Jetzt kaufen bei Muster AG"))
        log = read(os.path.join(ENV["WEBAGENT_STATE"], "guard.log"))
        self.assertIn('"deny"', log)
        self.assertNotIn("Muster", log)


class FakeHub:
    """The hub's /wf API, faked on a loopback port: records every request, answers like hubwfapi (or `status`)."""

    def __init__(self):
        import http.server
        import threading

        hub = self
        self.requests, self.status = [], 200

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                hub.requests.append({"path": self.path, "body": body, "auth": self.headers.get("Authorization")})
                if hub.status != 200:
                    out = {"error": "workflows_off", "hint": "the engine is off"}
                elif self.path == "/wf/start":
                    out = {"id": body.get("id"), "name": body.get("name"), "status": "PENDING"}
                else:
                    out = {"sent": True, "id": self.path.split("/")[2], "topic": body.get("topic")}
                data = json.dumps(out).encode()
                self.send_response(hub.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def wf_log():
    p = os.path.join(ENV["WEBAGENT_STATE"], "wf.log")
    return [json.loads(x) for x in read(p).splitlines()] if os.path.exists(p) else []


class WorkflowMirror(unittest.TestCase):
    """H15 Pilot 1: with wf-webtask on, each stop's approval wait is mirrored into the hub's webtask.approval
    workflow; decisions are sent to it; the hub being down or off changes nothing; switch off = no hub call."""

    def setUp(self):
        reset()
        self.hub = FakeHub()
        self.env = {"HUB_URL": self.hub.url}
        if os.path.exists(ENV["WEBAGENT_WF_SWITCH"]):
            os.remove(ENV["WEBAGENT_WF_SWITCH"])

    def tearDown(self):
        self.hub.close()
        if os.path.exists(ENV["WEBAGENT_WF_SWITCH"]):
            os.remove(ENV["WEBAGENT_WF_SWITCH"])

    def on(self):
        open(ENV["WEBAGENT_WF_SWITCH"], "w").close()

    def task(self):
        rc, o, e = run("start", uniq(), "--url", "https://shop.example/cart", env=self.env)
        self.assertEqual(rc, 0, e)
        return o.strip()

    def stop(self, tid, label="Jetzt kaufen"):
        rc, o, e = jrun("stop", tid, "--label", label, "--what", "orders it", "--shot", SHOT, env=self.env)
        self.assertEqual(rc, 0, e)
        return W.load(tid)["pending"]["nonce"]

    def test_switch_off_makes_no_hub_call(self):
        tid = self.task()
        n = self.stop(tid)
        ap_tap(n)
        self.assertEqual(run("approve", tid, env=self.env)[0], 0)
        self.assertEqual(run("abort", tid, env=self.env)[0], 0)
        self.assertEqual(self.hub.requests, [])
        self.assertEqual(wf_log(), [])
        self.assertNotIn("wf", jrun("status", tid, "--json", env=self.env)[1])

    def test_start_replace_and_approve(self):
        self.on()
        tid = self.task()
        n1 = self.stop(tid)
        r = self.hub.requests
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0]["path"], "/wf/start")
        self.assertEqual(r[0]["auth"], "Bearer test-hub-token")
        b = r[0]["body"]
        self.assertEqual((b["name"], b["id"]), ("webtask.approval", f"webtask:{tid}:1"))
        self.assertEqual(b["args"], {"task": tid, "stop": 1, "ttl": 900, "notify": ["expired"], "nonce": n1})
        self.assertEqual(jrun("status", tid, "--json", env=self.env)[1]["wf"], f"webtask:{tid}:1")
        n2 = self.stop(tid, "Zahlungspflichtig bestellen")
        paths = [x["path"] for x in r]
        self.assertEqual(paths[1:], [f"/wf/webtask:{tid}:1/send", "/wf/start"])
        self.assertEqual(r[1]["body"], {"topic": "decision", "message": {"decision": "replaced", "via": "webagent"},
                                        "key": f"webtask:{tid}:1:replaced"})
        self.assertEqual(r[2]["body"]["id"], f"webtask:{tid}:2")
        self.assertEqual(r[2]["body"]["args"]["nonce"], n2)
        ap_tap(n2)
        self.assertEqual(run("approve", tid, env=self.env)[0], 0)
        self.assertEqual(r[3]["path"], f"/wf/webtask:{tid}:2/send")
        self.assertEqual(r[3]["body"]["message"], {"decision": "approved", "via": "button"})
        self.assertEqual(r[3]["body"]["key"], f"webtask:{tid}:2:approved")
        self.assertEqual([x["outcome"] for x in wf_log()], ["ok"] * 4)
        self.assertNotIn("orders it", read(os.path.join(ENV["WEBAGENT_STATE"], "wf.log")))

    def test_abort_sends_stopped(self):
        self.on()
        tid = self.task()
        self.stop(tid)
        self.assertEqual(run("abort", tid, env=self.env)[0], 0)
        last = self.hub.requests[-1]
        self.assertEqual(last["path"], f"/wf/webtask:{tid}:1/send")
        self.assertEqual(last["body"]["message"], {"decision": "stopped", "via": "owner"})

    def test_hub_down_or_off_changes_nothing(self):
        self.on()
        for env, want in (({"HUB_URL": "http://127.0.0.1:9"}, "HubDown"), (self.env, "HubDown:503")):
            self.hub.status = 503
            reset()
            rc, o, e = run("start", uniq(), "--url", "https://shop.example/cart", env=env)
            tid = o.strip()
            rc, o, e = jrun("stop", tid, "--label", "Jetzt kaufen", "--what", "x", "--shot", SHOT, env=env)
            self.assertEqual(rc, 0, e)
            self.assertIn("ap:", json.dumps(o))
            t = W.load(tid)
            self.assertEqual(t["state"], "pending")
            self.assertNotIn("wf", t["pending"])
            ap_tap(t["pending"]["nonce"])
            self.assertEqual(run("approve", tid, env=env)[0], 0)
            self.assertEqual(W.load(tid)["state"], "approved")
            log = wf_log()
            self.assertEqual([(x["op"], x["outcome"]) for x in log], [("start", want)])


class WholeTaskWorkflow(unittest.TestCase):
    """P3-6: with wf-webtask-run on, start starts the hub's webtask.run workflow (webtask:<id>) and each move sends it
    one message on topic "task" (ids, counts and states only); off = no call; the hub down changes nothing."""

    def setUp(self):
        reset()
        self.hub = FakeHub()
        self.env = {"HUB_URL": self.hub.url}
        for k in ("WEBAGENT_WF_SWITCH", "WEBAGENT_WF_RUN_SWITCH"):
            if os.path.exists(ENV[k]):
                os.remove(ENV[k])

    def tearDown(self):
        self.hub.close()
        for k in ("WEBAGENT_WF_SWITCH", "WEBAGENT_WF_RUN_SWITCH"):
            if os.path.exists(ENV[k]):
                os.remove(ENV[k])

    def msgs(self, tid):
        return [x["body"]["message"] for x in self.hub.requests if x["path"] == f"/wf/webtask:{tid}/send"]

    def test_off_no_call_on_mirrors_the_task(self):
        rc, o, e = run("start", uniq(), "--url", "https://shop.example/cart", "--kind", "order", env=self.env)
        tid = o.strip()
        self.assertEqual(self.hub.requests, [])
        self.assertNotIn("wf_run", W.load(tid))
        reset()
        open(ENV["WEBAGENT_WF_RUN_SWITCH"], "w").close()
        rc, o, e = run("start", uniq(), "--url", "https://shop.example/cart", "--kind", "order", env=self.env)
        self.assertEqual(rc, 0, e)
        tid = o.strip()
        b = self.hub.requests[0]["body"]
        self.assertEqual((b["name"], b["id"]), ("webtask.run", f"webtask:{tid}"))
        self.assertEqual(b["args"], {"task": tid, "kind": "order", "dry": False, "pending_ttl": 900,
                                     "approved_ttl": 600, "idle": 2700, "revive": 7200, "wait_max": 3600,
                                     "notify": ["stale", "expired", "failed"]})
        self.assertEqual(jrun("status", tid, "--json", env=self.env)[1]["wf_run"], f"webtask:{tid}")
        self.assertEqual(run("step", tid, "opened the cart", "--kind", "navigate", env=self.env)[0], 0)
        rc, o, e = jrun("stop", tid, "--label", "Jetzt kaufen", "--what", "orders it", "--shot", SHOT, "--summary",
                        "1 item", "--amount", "CHF 12.00", env=self.env)
        self.assertEqual(rc, 0, e)
        n = W.load(tid)["pending"]["nonce"]
        ap_tap(n)
        self.assertEqual(run("approve", tid, env=self.env)[0], 0)
        from unittest import mock
        with mock.patch.dict(os.environ, self.env):
            W.wf_task(tid, "commit", stop=1)  # the browser service's call after a token commit
        self.assertEqual(run("done", tid, "--note", "ordered", env=self.env)[0], 0)
        got = self.msgs(tid)
        self.assertEqual([m["op"] for m in got], ["step", "stop", "decision", "commit", "done"])
        self.assertEqual(got[0], {"op": "step", "n": 1})
        self.assertEqual(got[1], {"op": "stop", "stop": 1, "nonce": n})
        self.assertEqual(got[2], {"op": "decision", "decision": "approved", "via": "button", "stop": 1})
        self.assertEqual(got[4], {"op": "done", "ok": True, "cont": False, "committed": False})
        keys = [x["body"]["key"] for x in self.hub.requests if x["path"].endswith("/send")]
        self.assertEqual(len(keys), len(set(keys)))
        log = read(os.path.join(ENV["WEBAGENT_STATE"], "wf.log"))
        self.assertNotIn("orders it", log)
        self.assertNotIn("Jetzt kaufen", json.dumps(self.hub.requests))

    def test_abort_and_dry_and_hub_down(self):
        open(ENV["WEBAGENT_WF_RUN_SWITCH"], "w").close()
        open(ENV["WEBAGENT_WF_SWITCH"], "w").close()  # Pilot 1 on: it reports card expiry, so run does not
        rc, o, e = run("start", uniq("Dry task"), "--url", "https://httpbin.org/forms/post", "--dry", env=self.env)
        tid = o.strip()
        self.assertEqual(self.hub.requests[0]["body"]["args"]["notify"], [])
        self.assertTrue(self.hub.requests[0]["body"]["args"]["dry"])
        self.assertEqual(run("abort", tid, env=self.env)[0], 0)
        self.assertEqual(self.msgs(tid)[-1], {"op": "abort", "reason": "owner"})
        reset()
        rc, o, e = run("start", uniq("Task"), "--url", "https://shop.example/", env=self.env)
        self.assertEqual(self.hub.requests[-1]["body"]["args"]["notify"], ["stale", "failed"])
        self.hub.status = 503
        reset()
        rc, o, e = run("start", uniq("Task"), "--url", "https://shop.example/", env=self.env)
        self.assertEqual(rc, 0, e)
        tid = o.strip()
        self.assertNotIn("wf_run", W.load(tid))
        self.assertEqual(run("step", tid, "x", env=self.env)[0], 0)
        self.assertEqual(W.load(tid)["state"], "running")


class HubJournal(unittest.TestCase):
    """v2 H6: while the Telegram transport runs on the hub (no journal is written), the journal proofs come from the
    hub's rows (core.hub.tg_journal over POST /tg/q, faked on a loopback port); only with the default journal or
    WEBAGENT_HUB=on, never with WEBAGENT_HUB=off; the hub down = the file alone."""

    def setUp(self):
        import http.server
        import threading

        reset()
        now = time.time()
        ts = lambda ago: time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now - ago))  # noqa: E731
        self.rows = {"tg.button": [{"id": 5, "payload": json.dumps({"user_id": 1000001, "chat_id": 1000001,
                                                                      "message_id": 71, "callback_data": "wa:ok:t1",
                                                                      "event_type": "button", "ts": ts(5)})},
                                   {"id": 6, "payload": json.dumps({"user_id": 2000002, "chat_id": 2000002,
                                                                      "message_id": 72, "callback_data": "wa:no:t1",
                                                                      "event_type": "button", "ts": ts(3)})}],
                     "tg.reaction": [{"id": 7, "payload": json.dumps({"user_id": 1000001, "chat_id": 1000001,
                                                                        "reacted_to_message_id": 90, "reaction": "\U0001f44e",
                                                                        "event_type": "reaction", "ts": ts(2)})}]}
        hub, self.asked = self, []

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                hub.asked.append(body)
                data = json.dumps({"rows": hub.rows.get(body["params"]["kind"], [])}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.env = {"HUB_URL": f"http://127.0.0.1:{self.srv.server_address[1]}", "WEBAGENT_HUB": "on"}
        self.t = {"id": "t1", "pending": {"set_at": now - 60}, "card_msg": "90", "replaced_msgs": []}
        self.old = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)

    def tearDown(self):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.srv.shutdown()
        self.srv.server_close()

    def test_tap_from_the_hub(self):
        ev = W._journal_evidence(self.t, "ok")
        self.assertEqual((ev or {}).get("via"), "button")
        self.assertEqual(ev["msg"], "71")
        self.assertTrue(all(b["name"] == "kind_payloads" for b in self.asked))
        self.assertEqual(sorted(b["params"]["kind"] for b in self.asked), ["tg.button", "tg.message", "tg.reaction"])

    def test_reaction_from_the_hub_and_not_his_tap(self):
        ev = W._journal_evidence(self.t, "no")
        self.assertEqual((ev or {}).get("via"), "reaction")   # the wa:no tap is someone else's

    def test_off_and_hub_down(self):
        os.environ["WEBAGENT_HUB"] = "off"
        self.assertIsNone(W._journal_evidence(self.t, "ok"))
        self.assertEqual(self.asked, [])
        os.environ.pop("WEBAGENT_HUB")                        # WEBAGENT_JOURNAL is set: the file alone
        self.assertIsNone(W._journal_evidence(self.t, "ok"))
        self.assertEqual(self.asked, [])
        os.environ.update(WEBAGENT_HUB="on", HUB_URL="http://127.0.0.1:9")
        self.assertIsNone(W._journal_evidence(self.t, "ok"))
        journal("button", {"callback_data": "wa:ok:t1", "message_id": "73", "user_id": "1000001"})
        self.assertEqual((W._journal_evidence(self.t, "ok") or {}).get("msg"), "73")


class FakeApprovalsHub:
    """The hub's /approvals API (hubapprovals), faked on a loopback port: mints, attaches, reads, consumes once and
    cancels; decide(nonce) is his tap as the hub's Telegram adapter records it. Every other POST answers no rows."""

    def __init__(self):
        import http.server
        import secrets
        import threading

        hub = self
        self.rows, self.calls = {}, []

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def answer(self, out, code=200):
                data = json.dumps(out).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                hub.calls.append(("GET", self.path, {}))
                r = hub.rows.get(self.path.split("/")[-1]) if self.path.startswith("/approvals/") else None
                self.answer(dict(r, state=hub.state(r)) if r else {"error": "not_found"}, 200 if r else 404)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                hub.calls.append(("POST", self.path, body))
                parts, t = self.path.strip("/").split("/"), time.time()
                if parts == ["approvals"]:
                    n = secrets.token_hex(16)
                    hub.rows[n] = {"nonce": n, "action_sha": body["action_sha"], "summary": body["summary"],
                                   "ref": body.get("ref"), "chat": body.get("chat"), "message_id": None,
                                   "status": "pending", "created": t, "expires": t + body["ttl"], "decided_ts": None,
                                   "decided_via": None, "used_ts": None, "used_by": None}
                    return self.answer({"nonce": n, "expires": t + body["ttl"], "summary": body["summary"],
                                        "ref": body.get("ref")})
                if parts[0] != "approvals":
                    return self.answer({"rows": []})
                r = hub.rows.get(parts[1])
                if parts[2] == "attach":
                    ok = bool(r) and r["status"] == "pending"
                    if ok:
                        r["chat"], r["message_id"] = body["chat"], body["message_id"]
                    return self.answer({"ok": ok})
                if parts[2] == "cancel":
                    ok = bool(r) and r["status"] == "pending"
                    if ok:
                        r["status"] = "cancelled"
                    return self.answer({"ok": ok})
                if parts[2] == "consume":
                    if not r:
                        return self.answer({"code": 5, "info": {"nonce": parts[1], "state": "unknown"}})
                    if r["used_ts"] is not None:
                        return self.answer({"code": 4, "info": {"state": "used", "why": "used already"}})
                    if r["status"] != "approved" or r["action_sha"] != body["action_sha"]:
                        return self.answer({"code": 5, "info": {"state": hub.state(r), "why": "not his"}})
                    r["used_ts"], r["used_by"] = t, body["by"]
                    return self.answer({"code": 0, "info": {"state": "used", "why": "approved by him"}})
                return self.answer({"error": "bad"}, 400)

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    @staticmethod
    def state(r):
        if r["used_ts"] is not None:
            return "used"
        return "expired" if r["status"] == "pending" and time.time() >= r["expires"] else r["status"]

    def decide(self, nonce, status="approved"):
        self.rows[nonce].update(status=status, decided_ts=time.time(), decided_via="adapter")

    def paths(self, method="POST"):
        return [p for m, p, _ in self.calls if m == method and p.startswith("/approvals")]

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class HubApprovals(unittest.TestCase):
    """v2 H6 (fix 23.10): with the hub's Telegram transport on (hub-tg), each stop's approval is minted, attached,
    read, consumed and cancelled on the hub, where his tap is decided; never in a local store, which no tap can
    reach (the dead-✅ bug: webagent passed its own connection and so skipped the hub). The hub down = wa:ok/wa:no
    buttons. Scratch HOME (ASSISTANT_TEST=1) with the switch; WEBAGENT_HUB=on = the library's default store."""

    def setUp(self):
        reset()
        self.hub = FakeApprovalsHub()
        self.home = tempfile.mkdtemp(prefix="home-", dir=TMP)
        os.makedirs(os.path.join(self.home, ".config", "assistant"))
        with open(os.path.join(self.home, ".config", "assistant", "hub-tg"), "w") as f:
            f.write("on\n")
        self.env = {"HOME": self.home, "ASSISTANT_TEST": "1", "HUB_URL": self.hub.url, "WEBAGENT_HUB": "on"}

    def tearDown(self):
        self.hub.close()
        shutil.rmtree(self.home, ignore_errors=True)

    def local_stores(self):
        """Approval stores outside the hub: beside the task state, or the scratch HOME's default one."""
        d = os.path.join(self.home, ".local", "state", "assistant")
        return [p for p in (ap_db(), d) if os.path.exists(p)]

    def stop(self, tid, label="Unsubscribe", env=None):
        rc, o, e = jrun("stop", tid, "--label", label, "--what", "ends the newsletter", "--shot", SHOT,
                        env=env or self.env)
        self.assertEqual(rc, 0, e)
        return o, W.load(tid)["pending"]["nonce"]

    def test_minted_attached_and_consumed_on_the_hub(self):
        tid = new_task("--kind", "unsubscribe", url="https://news.example/unsubscribe?u=1")
        o, n = self.stop(tid)
        self.assertIn(n, self.hub.rows)                                    # the hub minted the card's nonce
        self.assertEqual(o["card"]["buttons"][0][0]["data"], f"ap:{n}:y")
        mint = [b for m, p, b in self.hub.calls if p == "/approvals"]
        self.assertEqual(len(mint), 1)
        self.assertEqual(mint[0]["ref"], f"webagent:{tid}")
        self.assertNotIn("news.example", json.dumps(mint[0]))              # only the action's sha256 leaves
        self.assertEqual(self.local_stores(), [])                          # no local store at all
        self.assertEqual(run("sent", tid, "--msg", "70", env=self.env)[0], 0)
        self.assertEqual((self.hub.rows[n]["chat"], self.hub.rows[n]["message_id"]), ("1000001", 70))
        self.assertEqual(run("approve", tid, env=self.env)[0], 5)          # no tap yet
        self.hub.decide(n)                                                 # his ✅, recorded by the hub's adapter
        rc, o, e = jrun("approve", f"ap:{n}:y", env=self.env)
        self.assertEqual((rc, o["id"], o["via"]), (0, tid, "button"), e)
        self.assertEqual((self.hub.rows[n]["used_by"], W.load(tid)["approval"]["nonce"]), (f"webagent:{tid}", n))
        consumed = [b for m, p, b in self.hub.calls if p == f"/approvals/{n}/consume"]
        self.assertEqual([b["action_sha"] for b in consumed], [self.hub.rows[n]["action_sha"]])
        self.assertEqual(run("approve", tid, env=self.env)[0], 6)          # approved already: no second use
        self.assertEqual(self.local_stores(), [])

    def test_his_no_and_a_replaced_card_on_the_hub(self):
        tid = new_task(url="https://forms.example/b")
        _, old = self.stop(tid)
        _, n = self.stop(tid, label="Submit")                              # a re-stop replaces the card
        self.assertNotEqual(old, n)
        self.assertIn(f"/approvals/{old}/cancel", self.hub.paths())
        self.assertEqual(self.hub.rows[old]["status"], "cancelled")
        self.hub.decide(n, "denied")
        self.assertEqual(run("approve", tid, env=self.env)[0], 5)
        self.assertEqual(self.hub.rows[n]["used_ts"], None)
        self.assertEqual(self.local_stores(), [])

    def test_hub_down_falls_back_to_wa_buttons(self):
        tid = new_task(url="https://forms.example/c")
        o, n = self.stop(tid, env=dict(self.env, HUB_URL="http://127.0.0.1:9"))
        self.assertIsNone(n)
        self.assertEqual(o["card"]["buttons"][0][0]["data"], f"wa:ok:{tid}")
        self.assertEqual(self.local_stores(), [])


if __name__ == "__main__":
    try:
        unittest.main(verbosity=1)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
