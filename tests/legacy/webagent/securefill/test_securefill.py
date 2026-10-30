#!/usr/bin/env python3
"""securefill end to end on a local test site in a separate headless Chrome (port 9333, temp profile; the owner's
headed Chrome is never touched) with a fake vault that seals with the real vault's personal.py. Fake data only.
Covers: probe (field keys, cross-site card frame blocked until allowlisted), fill (vault ✅ wait -> exit 10, then
release -> typed, masked, readback ok, key used once, fingerprints), the redaction hook (snapshot text, a screenshot
showing a passport number withheld + its saved copy overwritten, a clean one passed), commit (web-agent ✅ + vault ✅
-> card into the cross-origin iframe + approved click, one use), refusals (other site, denied), webagent's screenshot
check, the card-file parser (class T: an older vault without fill sessions, one ✅ per fill). Class TSession: fill
sessions (lazy start exit 10 then fill, no second vault message, webagent start --kind booking|form asks itself, commit
without a further vault ✅, the end report after abort/done/session end, deny exit 5, tick under a session, the web
task waiting for his ✅ instead of going stale, `webagent revive` after a late ✅ without a second one). Class THidden:
fields a person cannot see or use (one hiding technique per decoy, hidden frames, Stripe's invisible helper input, the
page behind a modal dialog) are neither asked for nor filled, one that fades out mid-fill is not typed; a Stripe-like
visible card frame still is.
Run: python3 test_securefill.py  (copies: SECUREFILL_CLI, WEBAGENT_CLI, WEBAGENT_LIB)"""
import base64, json, os, random, shutil, socket, subprocess, sys, tempfile, time, unittest, urllib.request
HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
CLI = os.environ.get("SECUREFILL_CLI") or os.path.join(HOME, "bin", "securefill")
WA_CLI = os.environ.get("WEBAGENT_CLI") or os.path.join(HOME, "bin", "webagent")
WA_LIB = os.environ.get("WEBAGENT_LIB") or os.path.join(HOME, "bin", "lib")
SF_LIB = os.path.join(os.path.dirname(os.path.realpath(CLI)), "lib")
sys.path.insert(0, SF_LIB)
TMP = tempfile.mkdtemp(prefix="sf-test-")
RNG = random.Random(99)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def luhn_ok(d):
    t, alt = 0, False
    for ch in reversed(d):
        n = int(ch)
        if alt:
            n = n * 2 - 9 if n > 4 else n * 2
        t, alt = t + n, not alt
    return t % 10 == 0


body = "5" + "".join(str(RNG.randrange(10)) for _ in range(14))
CARD = next(body + str(d) for d in range(10) if luhn_ok(body + str(d)))
PP = "TX" + "".join(str(RNG.randrange(10)) for _ in range(7))
PHONE = "7" + "".join(str(RNG.randrange(10)) for _ in range(8))
CVC = "".join(str(RNG.randrange(1, 10)) for _ in range(3))
VALUES = {"name.title": "Mr", "name.given_names": "Testo", "name.surname": "Probewald",
          "identity.birth_date": "1988-07-14", "identity.nationality": "CH", "passport.number": PP,
          "passport.expiry_date": "2031-02-03", "contact.email": "testo@example.org", "contact.phone_cc": "+41",
          "contact.phone_national": PHONE, "address.street": "Teststrasse", "address.house_number": "12a",
          "address.postal_code": "8000", "address.city": "Zuerich", "address.country": "CH",
          "card.number": CARD, "card.exp_month": "08", "card.exp_year": "2029", "card.cvc": CVC,
          "card.holder": "TESTO PROBEWALD"}
SECRET = (CARD, PP, PHONE, CVC, "1988-07-14", "14.07.1988", "2031-02-03")
SPORT, VPORT, CPORT = free_port(), free_port(), free_port()
URL = f"http://127.0.0.1:{SPORT}/checkout"
CDP = f"http://127.0.0.1:{CPORT}"
ENV = dict(os.environ, SF_TEST_DIR=TMP, SECUREFILL_STATE=os.path.join(TMP, "state"),
           SECUREFILL_CONF=os.path.join(TMP, "conf.json"), SECUREFILL_CDP=CDP,
           SECUREFILL_VAULT_URL=f"http://127.0.0.1:{VPORT}", SECUREFILL_VAULT_TOKEN_FILE=os.path.join(TMP, "token"),
           SECUREFILL_USAGE="off", USAGE_TEST="1", WEBAGENT_STATE=os.path.join(TMP, "wa"), WEBAGENT_LIB=WA_LIB,
           WEBAGENT_DRY_HOSTS="127.0.0.1,localhost,httpbin.org", WEBAGENT_BROWSER_BIN="/bin/true",
           WEBAGENT_WF_SWITCH=os.path.join(TMP, "wf-webtask"), HUB_URL="http://127.0.0.1:9",  # H15: no hub call
           ASSISTANT_PROFILE=os.path.join(TMP, "profile.json"))   # ownermask (shot / mask): a synthetic owner profile
OWNER_PROFILE = {"owner": {"first_name": "Quirinella", "last_name": "Zwoggenbach", "email": "quirinella.zwoggen@probe-mail.example"},
                 "home": {"street": "Zwirbelstrasse 17", "postcode": "4719", "town": "Quaxingen am Brumsee"}}
PROCS = []


def run(argv, **kw):
    p = subprocess.run(argv, capture_output=True, text=True, env=kw.pop("env", ENV), timeout=kw.pop("timeout", 240), **kw)
    return p.returncode, p.stdout, p.stderr


def sf(*a, **kw):
    return run(["python3", CLI, *a], **kw)


def wa(*a):
    return run(["python3", WA_CLI, *a])


def pw(op, url, **extra):
    p = subprocess.run(["node", os.path.join(HERE, "pwtool.mjs")], input=json.dumps(dict(cdp=CDP, url=url, op=op, **extra)),
                       capture_output=True, text=True, timeout=60)
    return json.loads(p.stdout.strip().splitlines()[-1])


OPEN = []


def tab(url):
    req = urllib.request.Request(f"{CDP}/json/new?{url}", method="PUT")
    with urllib.request.urlopen(req, timeout=10) as r:
        t = json.load(r)
    time.sleep(1.5)
    OPEN.append(t["id"])
    return t["id"]


def close_tab(tid):
    try:
        urllib.request.urlopen(f"{CDP}/json/close/{tid}", timeout=5).read()
    except OSError:
        pass


def task(url=URL, dry=True):
    rc, out, err = wa("start", "Test booking", "--url", url, "--kind", "booking", *(["--dry"] if dry else []), "--json",
                        "--retry-of", "none")                    # separate tasks, not retries of one job
    assert rc == 0, err
    return json.loads(out)["id"]


def _iso(s):
    import datetime as dt
    return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()


def no_secret(tc, *texts, also=()):
    """No fake value in the texts. Short ones (the 3-digit CVC) count only as a token of their own: a random task id,
    temp dir or timestamp in a path may contain the same digits (wa1d2292-…)."""
    import re
    for t in texts:
        for s in SECRET + tuple(also):
            if len(s) < 6:
                tc.assertIsNone(re.search(r"(?<![0-9A-Za-z])" + re.escape(s) + r"(?![0-9A-Za-z])", t), f"{s!r} in {t!r}")
            else:
                tc.assertNotIn(s, t)


def setUpModule():
    with open(os.path.join(TMP, "values.json"), "w") as f:
        json.dump(VALUES, f)
    with open(ENV["ASSISTANT_PROFILE"], "w") as f:
        json.dump(OWNER_PROFILE, f)
    os.environ["ASSISTANT_PROFILE"] = ENV["ASSISTANT_PROFILE"]       # in-process calls too: never the real profile
    with open(os.path.join(TMP, "token"), "w") as f:
        f.write("test-token")
    with open(os.path.join(TMP, "conf.json"), "w") as f:
        json.dump({"psp": []}, f)
    with open(os.path.join(TMP, "sessions"), "w") as f:             # class T: the per-fill path (older vault)
        f.write("off")
    for argv in (["python3", os.path.join(HERE, "testsite.py"), str(SPORT)],
                 ["python3", os.path.join(HERE, "fakevault.py"), str(VPORT)],
                 ["google-chrome", "--headless=new", f"--remote-debugging-port={CPORT}", "--remote-debugging-address=127.0.0.1",
                  f"--user-data-dir={TMP}/chrome", "--no-sandbox", "--disable-dev-shm-usage", "--no-first-run",
                  "--disable-gpu", "--window-size=1280,1400", "about:blank"]):
        PROCS.append(subprocess.Popen(argv, env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    for _ in range(60):
        try:
            urllib.request.urlopen(f"{CDP}/json/version", timeout=2).read()
            urllib.request.urlopen(f"http://127.0.0.1:{SPORT}/plain", timeout=2).read()
            return
        except OSError:
            time.sleep(0.5)
    raise RuntimeError("test chrome/site did not start")


def tearDownModule():
    for p in PROCS:
        p.terminate()
    for p in PROCS:
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            p.kill()
    shutil.rmtree(TMP, ignore_errors=True)


class T(unittest.TestCase):
    def tearDown(self):
        while OPEN:
            close_tab(OPEN.pop())

    def test_1_probe_fill_mask_hook(self):
        tid, t = task(), tab(URL)
        rc, out, err = sf("probe", tid, "--tab-url", URL)
        self.assertEqual(rc, 0, out + err)
        p = json.loads(out)
        main = [f for f in p["frames"] if f["main"]][0]
        keys = {f["key"] for f in main["fields"]}
        for k in ("name.title", "name.given_names", "name.surname", "identity.birth_date", "identity.nationality",
                  "passport.number", "passport.expiry_date", "contact.email", "contact.phone_cc",
                  "contact.phone_national", "address.street_full", "address.house_number", "address.postal_code",
                  "address.city", "address.country"):
            self.assertIn(k, keys)
        self.assertNotIn("card.number", keys)
        self.assertEqual(p["card_frames_blocked"], [f"localhost"])           # cross-site, not on the PSP list
        # fill: first call waits for his ✅ (exit 10), the second gets the sealed box and types
        with open(os.path.join(TMP, "mode"), "w") as f:
            f.write("approve")
        rc, out1, err1 = sf("fill", tid, "--tab-url", URL, "--wait", "0")
        self.assertEqual(rc, 10, out1 + err1)
        self.assertTrue(json.loads(out1)["waiting"])
        rc, out2, err2 = sf("fill", tid, "--tab-url", URL, "--wait", "30")
        self.assertEqual(rc, 0, out2 + err2)
        res = json.loads(out2)
        self.assertTrue(res["ok"])
        bad = {k: v for k, v in res["filled"].items() if v not in ("ok", "same")}
        self.assertEqual(bad, {})
        no_secret(self, out1, err1, out2, err2)
        prev = [json.loads(x) for x in open(os.path.join(TMP, "previews.jsonl")).read().splitlines()][-1]
        self.assertIn(f"🌐 Site: 127.0.0.1", prev["lines"])
        self.assertNotIn("card.number", prev["fields"])
        self.assertIn("passport.number", prev["fields"])
        vals = pw("values", URL)
        m = vals[f"127.0.0.1:{SPORT}"]
        self.assertEqual(m["firstName"][0], "Testo")
        self.assertEqual(m["dob"][0], "14.07.1988")
        self.assertEqual(m["ppExp"][0], "2031-02-03")
        self.assertEqual(m["phone"][0], PHONE)
        self.assertEqual((m["cc"][0], m["nat"][0], m["title"][0], m["country"][0]), ("CH", "CH", "MR", "CH"))
        self.assertEqual((m["street"][0], m["houseNo"][0], m["zip"][0], m["city"][0]), ("Teststrasse", "12a", "8000", "Zuerich"))
        self.assertEqual(m["promo"][0], "")
        self.assertEqual(m["passportNo"][1], "disc")                           # masked
        self.assertEqual(vals[f"localhost:{SPORT}"]["cardnumber"][0], "")       # no card outside commit
        st = os.path.join(TMP, "state")
        self.assertEqual(os.listdir(os.path.join(st, "keys")), [])               # one-time key gone
        self.assertFalse(os.path.exists(os.path.join(st, "req", f"{tid}.json")))
        fps = os.listdir(os.path.join(st, "fp"))
        self.assertEqual(len(fps), 1)
        self.assertEqual(oct(os.stat(os.path.join(st, "fp", fps[0])).st_mode & 0o777), "0o600")
        no_secret(self, open(os.path.join(st, "fp", fps[0])).read(), open(os.path.join(st, "log.jsonl")).read())
        # the snapshot an agent would get shows the values; the hook takes them out
        snap = pw("snapshot", URL)["text"]
        self.assertIn(PP, snap)
        ev = {"tool_name": "mcp__browser__browser_snapshot", "tool_response": [{"type": "text", "text": snap + (
            f"\nPhone: +41 {PHONE[:2]} {PHONE[2:5]} {PHONE[5:7]} {PHONE[7:]}\nBorn 14 July 1988")}]}
        rc, hout, herr = run(["python3", CLI, "hook"], input=json.dumps(ev))
        self.assertEqual(rc, 0, herr)
        new = json.loads(hout)["hookSpecificOutput"]["updatedMCPToolOutput"]
        no_secret(self, json.dumps(new), also=("Testo", "Probewald", "testo@example.org", "Teststrasse", "Zuerich"))
        self.assertIn("[securefill]", new[0]["text"])
        clean = {"tool_name": "mcp__browser__browser_snapshot", "tool_response": [{"type": "text", "text": "- button \"Pay now\""}]}
        self.assertEqual(run(["python3", CLI, "hook"], input=json.dumps(clean))[1].strip(), "")
        # screenshots: a page showing the passport number as text is withheld, its saved blob overwritten
        rurl = f"http://127.0.0.1:{SPORT}/review/{PP}/14.07.1988"
        rt = tab(rurl)
        png = os.path.join(TMP, "review.png")
        pw("screenshot", rurl, path=png)
        raw = open(png, "rb").read()
        sess = os.path.join(TMP, "proj", "sess1", "tool-results")
        os.makedirs(sess)
        blob = os.path.join(sess, "mcp-browser-blob-1.png")
        shutil.copyfile(png, blob)
        ev = {"tool_name": "mcp__browser__browser_take_screenshot", "session_id": "sess1",
              "transcript_path": os.path.join(TMP, "proj", "sess1.jsonl"),
              "tool_response": [{"type": "text", "text": "### Result\n- screenshot"},
                                {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                             "data": base64.b64encode(raw).decode()}}]}
        rc, hout, _ = run(["python3", CLI, "hook"], input=json.dumps(ev))
        new = json.loads(hout)["hookSpecificOutput"]["updatedMCPToolOutput"]
        self.assertFalse(any(b.get("type") == "image" for b in new))
        self.assertNotEqual(open(blob, "rb").read(), raw)
        rc, _, _ = sf("check-image", png)
        self.assertEqual(rc, 3)
        rc, out, err = wa("stop", tid, "--label", "Weiter", "--what", "next page", "--shot", png)
        self.assertEqual(rc, 3, out + err)                                    # webagent refuses the leaky shot
        purl = f"http://127.0.0.1:{SPORT}/plain"
        pt = tab(purl)
        ppng = os.path.join(TMP, "plain.png")
        pw("screenshot", purl, path=ppng)
        ev["tool_response"][1]["source"]["data"] = base64.b64encode(open(ppng, "rb").read()).decode()
        self.assertEqual(run(["python3", CLI, "hook"], input=json.dumps(ev))[1].strip(), "")   # clean shot passes
        for x in (t, rt, pt):
            close_tab(x)

    def test_2_commit_card_one_use(self):
        with open(os.path.join(TMP, "conf.json"), "w") as f:
            json.dump({"psp": ["localhost"]}, f)                               # the test's "payment provider"
        with open(os.path.join(TMP, "mode"), "w") as f:
            f.write("approve")
        tid, t = task(), tab(URL)
        shotp = os.path.join(TMP, "plain-stop.png")
        purl = f"http://127.0.0.1:{SPORT}/plain"
        pt = tab(purl)
        pw("screenshot", purl, path=shotp)
        rc, out, err = wa("stop", tid, "--label", "Pay now", "--what", "pays the test order", "--shot", shotp,
                          "--card", "--amount", "CHF 1.00", "--summary", "test flight, 1 adult")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("CHF 1.00", json.loads(out)["card"]["text"])
        rc, out, err = sf("commit", tid, "--tab-url", URL, "--wait", "5")
        self.assertEqual(rc, 6, out + err)                                     # no web-agent ✅ yet
        self.assertEqual(wa("approve", tid, "--simulate")[0], 0)
        r = json.loads(wa("resume", tid)[1])
        self.assertTrue(r["card"] and "securefill commit" in r["why"])
        rc, out, err = sf("commit", tid, "--tab-url", URL, "--wait", "30")
        self.assertEqual(rc, 0, out + err)
        res = json.loads(out)
        no_secret(self, out, err)
        self.assertTrue(res["clicked"])
        self.assertEqual(res["after"]["path"], "/paid")
        self.assertTrue(res["filled"]["card.number [localhost]"] in ("ok", "same"))
        rec = json.loads(open(os.path.join(TMP, "record.json")).read())
        self.assertEqual(rec["cardnumber"].replace(" ", ""), CARD)
        self.assertEqual("".join(c for c in rec["exp"] if c.isdigit()), "0829")
        self.assertEqual((rec["cvc"], rec["holder"]), (CVC, "TESTO PROBEWALD"))
        prev = [json.loads(x) for x in open(os.path.join(TMP, "previews.jsonl")).read().splitlines()][-1]
        self.assertTrue(any("CHF 1.00" in x for x in prev["lines"]) and any("Pay now" in x for x in prev["lines"]))
        self.assertIn("   payment frames: localhost", prev["lines"])
        ev = {"tool_name": "mcp__browser__browser_evaluate", "tool_response": [{"type": "text", "text":
              f"value: {CARD[:4]} {CARD[4:8]} {CARD[8:12]} {CARD[12:]}\nCVC: {CVC}\nSeat 112 Row 7"}]}
        new = json.loads(run(["python3", CLI, "hook"], input=json.dumps(ev))[1])["hookSpecificOutput"]["updatedMCPToolOutput"]
        no_secret(self, json.dumps(new))
        self.assertIn("Seat 112 Row 7", new[0]["text"])                       # other numbers stay
        st = json.loads(wa("status", tid, "--json")[1])
        st = st[0] if isinstance(st, list) else st
        self.assertEqual(st["state"], "executing")
        rc, out, err = sf("commit", tid, "--tab-url", f"http://127.0.0.1:{SPORT}/paid", "--wait", "5")
        self.assertEqual(rc, 6)                                                # one use
        for x in (t, pt):
            close_tab(x)

    def test_3_refusals(self):
        with open(os.path.join(TMP, "mode"), "w") as f:
            f.write("approve")
        with open(os.path.join(TMP, "conf.json"), "w") as f:
            json.dump({"psp": []}, f)
        other = f"http://localhost:{SPORT}/checkout"
        tid, t = task(), tab(other)
        rc, out, err = sf("fill", tid, "--tab-url", other, "--wait", "5")
        self.assertEqual(rc, 3, out + err)                                     # not the task's site
        close_tab(t)
        tid2, t2 = task(), tab(URL)
        with open(os.path.join(TMP, "mode"), "w") as f:
            f.write("deny")
        rc, out, err = sf("fill", tid2, "--tab-url", URL, "--wait", "10")
        self.assertEqual(rc, 5, out + err)
        self.assertFalse(os.path.exists(os.path.join(TMP, "state", "req", f"{tid2}.json")))
        self.assertEqual(os.listdir(os.path.join(TMP, "state", "keys")), [])
        close_tab(t2)

    def _card_stop(self, *ticks):
        tid, t = task(), tab(URL)
        shotp = os.path.join(TMP, "plain-stop.png")
        purl = f"http://127.0.0.1:{SPORT}/plain"
        pt = tab(purl)
        pw("screenshot", purl, path=shotp)
        close_tab(pt)
        args = [x for lab in ticks for x in ("--tick", lab)]
        rc, out, err = wa("stop", tid, "--label", "Pay now", "--what", "pays the test order", "--shot", shotp,
                          "--card", "--amount", "CHF 1.00", "--summary", "test flight, 1 adult", *args)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(wa("approve", tid, "--simulate")[0], 0)
        return tid, t, json.loads(out)["card"]["text"]

    def test_5_ticks_listed_only(self):
        with open(os.path.join(TMP, "conf.json"), "w") as f:
            json.dump({"psp": ["localhost"]}, f)
        with open(os.path.join(TMP, "mode"), "w") as f:
            f.write("approve")
        for p in ("boxes.json", "record.json"):
            if os.path.exists(os.path.join(TMP, p)):
                os.unlink(os.path.join(TMP, p))
        # a missing and an ambiguous label: refused before the vault is asked; nothing ticked, filled or clicked
        nprev = len(open(os.path.join(TMP, "previews.jsonl")).read().splitlines()) \
            if os.path.exists(os.path.join(TMP, "previews.jsonl")) else 0
        tid, t, _ = self._card_stop("I accept the fare rules and conditions of carriage", "I accept the unicorn rules")
        rc, out, err = sf("commit", tid, "--tab-url", URL, "--wait", "10")
        self.assertEqual(rc, 3, out + err)
        res = json.loads(out)
        self.assertIn("I accept the unicorn rules", res["error"])
        self.assertEqual([p["label"] for p in res["tick_problems"]], ["I accept the unicorn rules"])
        self.assertEqual(pw("boxes", URL), {"fare": False, "privacy": False, "news": False, "ag1": False, "ag2": False,
                                            "ins": True})
        self.assertEqual(json.loads(wa("status", tid, "--json")[1])["state"], "approved")    # approval unused
        prev = open(os.path.join(TMP, "previews.jsonl")).read().splitlines() \
            if os.path.exists(os.path.join(TMP, "previews.jsonl")) else []
        self.assertEqual(len(prev), nprev)                                    # no vault request
        self.assertFalse(os.path.exists(os.path.join(TMP, "record.json")))
        vals = pw("values", URL)
        self.assertEqual(vals[f"localhost:{SPORT}"]["cardnumber"][0], "")
        wa("abort", tid, "--reason", "agent")
        close_tab(t)
        tid, t, _ = self._card_stop("I agree")
        rc, out, err = sf("probe", tid, "--tab-url", URL, "--tick", "I agree", "--tick", "I have read the privacy notice")
        self.assertEqual([(c["label"], c["matches"]) for c in json.loads(out)["tick_check"]],
                         [("I agree", 2), ("I have read the privacy notice", 1)])
        rc, out, err = sf("commit", tid, "--tab-url", URL, "--wait", "10")
        self.assertEqual(rc, 3, out + err)
        self.assertIn("ambiguous", json.loads(out)["error"])
        wa("abort", tid, "--reason", "agent")
        close_tab(t)
        # listed -> ticked (plain label, label[for] on a hidden input, star marker), an already-ticked
        # role=checkbox left as is, unlisted boxes untouched; then card + click
        tid, t, text = self._card_stop("I accept the fare rules and conditions of carriage", "I have read the privacy notice",
                                       "Add travel insurance")
        self.assertIn("Ticks: I accept the fare rules and conditions of carriage; I have read the privacy notice; "
                      "Add travel insurance", text)
        rc, out, err = sf("commit", tid, "--tab-url", URL, "--wait", "30")
        self.assertEqual(rc, 0, out + err)
        res = json.loads(out)
        no_secret(self, out, err)
        self.assertTrue(res["clicked"])
        self.assertEqual([r["result"] for r in res["ticks"]], ["ticked", "ticked", "already"])
        boxes = json.loads(open(os.path.join(TMP, "boxes.json")).read())
        self.assertEqual(boxes, {"fare": True, "privacy": True, "news": False, "ag1": False, "ag2": False, "ins": True})
        close_tab(t)

    def test_6_tick_non_card(self):
        tid, t = task(), tab(URL)
        shotp = os.path.join(TMP, "plain-stop.png")
        rc, out, err = wa("stop", tid, "--label", "Submit details", "--what", "sends the form", "--shot", shotp,
                          "--tick", "I have read the privacy notice")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(sf("tick", tid, "--tab-url", URL)[0], 6)                  # not approved yet
        self.assertEqual(wa("approve", tid, "--simulate")[0], 0)
        rc, out, err = sf("tick", tid, "--tab-url", URL)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(json.loads(out)["ticks"], [{"label": "I have read the privacy notice", "result": "ticked"}])
        self.assertEqual(pw("boxes", URL), {"fare": False, "privacy": True, "news": False, "ag1": False, "ag2": False,
                                            "ins": True})
        self.assertEqual(json.loads(wa("status", tid, "--json")[1])["state"], "approved")    # the click is the agent's
        close_tab(t)

    def test_4_card_file_parser(self):
        import securefill as S
        for raw in (json.dumps({"number": CARD, "expiry": "08/29", "cvc": CVC, "name": "TESTO PROBEWALD"}),
                    f"Card number: {CARD[:4]} {CARD[4:8]} {CARD[8:12]} {CARD[12:]}\nExpiry: 08/29\nCVC: {CVC}\nHolder: TESTO PROBEWALD",
                    f"Virtual card\n{CARD}\n08/29\nCVV: {CVC}"):
            v = S._parse_card_file(raw)
            self.assertEqual(v["card.number"], CARD)
            self.assertEqual((v["card.exp_month"].lstrip("0"), v["card.exp_year"][-2:]), ("8", "29"))
            self.assertEqual(v["card.cvc"], CVC)


def vaultbot():
    """The fake vault bot's messages so far (send/edit/answer)."""
    p = os.path.join(TMP, "vaultbot.jsonl")
    return [json.loads(x) for x in open(p).read().splitlines()] if os.path.exists(p) else []


def sends():
    return [m for m in vaultbot() if m["op"] == "send"]


def set_mode(m):
    with open(os.path.join(TMP, "mode"), "w") as f:
        f.write(m)


class TSession(unittest.TestCase):
    """Fill sessions on (the vault's FillSessions): one ✅ per web task in the vault bot (site + task + time limit,
    no field list), any stored field the pages need while it lives, the card only after the web-agent confirm card
    (no further vault ✅), the end report (field names × count, card used or not, never values)."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(TMP, "sessions"), "w") as f:
            f.write("on")
        with open(os.path.join(TMP, "conf.json"), "w") as f:
            json.dump({"psp": ["localhost"]}, f)                   # before the sessions start: their frame hosts
        cls.shot = os.path.join(TMP, "session-stop.png")
        purl = f"http://127.0.0.1:{SPORT}/plain"
        pt = tab(purl)
        pw("screenshot", purl, path=cls.shot)
        close_tab(pt)

    @classmethod
    def tearDownClass(cls):
        with open(os.path.join(TMP, "sessions"), "w") as f:
            f.write("off")
        with open(os.path.join(TMP, "conf.json"), "w") as f:
            json.dump({"psp": []}, f)

    def tearDown(self):
        while OPEN:
            close_tab(OPEN.pop())

    def start(self, kind):
        rc, out, err = wa("start", "Test session booking", "--url", URL, "--kind", kind, "--dry", "--json",
                            "--retry-of", "none")
        self.assertEqual(rc, 0, out + err)
        t = json.loads(out)
        return t["id"], t

    def report(self, n_before):
        new = [m for m in sends()[n_before:] if m["text"].startswith("🧾 Fill session ended")]
        self.assertEqual(len(new), 1, sends()[n_before:])
        no_secret(self, json.dumps(vaultbot()))
        return new[0]["text"]

    def test_1_lazy_start_then_fill_no_second_ask_abort_report(self):
        set_mode("approve")
        tid, t = self.start("other")                              # not a booking/form task: no session at start
        self.assertIsNone(t.get("fill_session"))
        n0 = len(sends())
        tb = tab(URL)
        rc, out, err = sf("fill", tid, "--tab-url", URL, "--wait", "0")
        self.assertEqual(rc, 10, out + err)                       # the first fill asks for the session
        w = json.loads(out)
        self.assertTrue(w["waiting"])
        self.assertEqual(w["session"], "pending")
        self.assertIn("passport.number", w["fields_needed"])
        ask = sends()[n0:]
        self.assertEqual(len(ask), 1)
        self.assertIn("🌐 Site: 127.0.0.1 (and its subdomains)", ask[0]["text"])
        self.assertIn("Test session booking", ask[0]["text"])
        self.assertIn("⏱ For 30 min from your ✅", ask[0]["text"])
        self.assertNotIn("passport", ask[0]["text"])               # no field list on the grant
        self.assertEqual([b[0] for b in ask[0]["buttons"][0]], ["✅ Allow 30 min", "❌ Deny"])
        rc, out, err = sf("fill", tid, "--tab-url", URL, "--wait", "30")
        self.assertEqual(rc, 0, out + err)
        res = json.loads(out)
        self.assertEqual({k: v for k, v in res["filled"].items() if v not in ("ok", "same")}, {})
        no_secret(self, out, err)
        m = pw("values", URL)[f"127.0.0.1:{SPORT}"]
        self.assertEqual((m["firstName"][0], m["passportNo"][1]), ("Testo", "disc"))
        self.assertEqual(os.listdir(os.path.join(TMP, "state", "keys")), [])
        n1, nmsg = len(sends()), len(vaultbot())
        rc, out, err = sf("fill", tid, "--tab-url", URL, "--only", "passport", "--wait", "0")
        self.assertEqual(rc, 0, out + err)                        # a 2nd fill: no new vault message at all
        self.assertEqual(len(vaultbot()), nmsg)
        rc, out, err = sf("session", "status", tid)
        st = json.loads(out)
        self.assertEqual((rc, st["session"]), (0, "active"))
        self.assertEqual(st["released"]["passport.number"], 2)
        self.assertGreater(st["seconds_left"], 1500)
        no_secret(self, out)
        rc, out, err = wa("abort", tid, "--reason", "agent")
        self.assertEqual(rc, 0, out + err)
        text = self.report(n1)
        self.assertIn("task stopped", text)
        self.assertIn("Shared (field names, never values):", text)
        self.assertIn("• passport: number ×2, expiry date ×2", text)
        self.assertIn("• name: title, given names, surname", text)
        self.assertIn("💳 Card: not used", text)
        self.assertIn("Into: 127.0.0.1", text)
        self.assertIsNone(json.loads(sf("session", "status", tid)[1]).get("session"))   # local record gone
        self.assertEqual(sf("fill", tid, "--tab-url", URL, "--wait", "0")[0], 6)          # the task is closed
        close_tab(tb)

    def test_2_booking_start_commit_without_vault_ask_done_report(self):
        set_mode("approve")
        n0 = len(sends())
        tid, t = self.start("booking")                            # webagent start asks for the session itself
        self.assertEqual(t["fill_session"]["state"], "pending")
        ask = sends()[n0:]
        self.assertEqual(len(ask), 1)
        self.assertIn("Claude asks to fill in your details", ask[0]["text"])
        self.assertIn("payment-provider host", ask[0]["text"])
        rc, out, err = sf("session", "start", tid, "--wait", "10")    # his ✅ (the fake taps on the 2nd poll)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(json.loads(out)["session"], "active")
        self.assertEqual(len(sends()), n0 + 1)                    # the same session, not a second ask
        tb = tab(URL)
        rc, out, err = sf("fill", tid, "--tab-url", URL, "--wait", "0")
        self.assertEqual(rc, 0, out + err)                        # no exit 10: the session is live already
        n1 = len(sends())
        nprev = len(open(os.path.join(TMP, "previews.jsonl")).read().splitlines()) \
            if os.path.exists(os.path.join(TMP, "previews.jsonl")) else 0
        rc, out, err = wa("stop", tid, "--label", "Pay now", "--what", "pays the test order", "--shot", self.shot,
                          "--card", "--amount", "CHF 1.00", "--summary", "test flight, 1 adult")
        self.assertEqual(rc, 0, out + err)
        card = json.loads(out)["card"]["text"]
        self.assertIn("CHF 1.00", card)
        self.assertIn("This ✅ is also your card OK", card)
        self.assertNotIn("the vault asks you once more", card)
        self.assertEqual(wa("approve", tid, "--simulate")[0], 0)
        self.assertIn("no further ✅", json.loads(wa("resume", tid)[1])["why"])
        for p in ("record.json",):
            if os.path.exists(os.path.join(TMP, p)):
                os.unlink(os.path.join(TMP, p))
        rc, out, err = sf("commit", tid, "--tab-url", URL, "--wait", "0")
        self.assertEqual(rc, 0, out + err)                        # no vault ✅ for the card: the confirm card was it
        res = json.loads(out)
        no_secret(self, out, err)
        self.assertTrue(res["clicked"])
        self.assertEqual(res["after"]["path"], "/paid")
        rec = json.loads(open(os.path.join(TMP, "record.json")).read())
        self.assertEqual(rec["cardnumber"].replace(" ", ""), CARD)
        self.assertEqual(len(sends()), n1)                        # nothing new in the vault bot
        prev = open(os.path.join(TMP, "previews.jsonl")).read().splitlines() \
            if os.path.exists(os.path.join(TMP, "previews.jsonl")) else []
        self.assertEqual(len(prev), nprev)                        # not the per-fill path
        aud = [json.loads(x) for x in open(os.path.join(TMP, "audit.jsonl")).read().splitlines()]
        rel = [a for a in aud if a["ev"] == "fill_session_release" and a["task"] == tid]
        self.assertEqual([a["card"] for a in rel], [False, True])
        self.assertEqual(rel[1]["frames"], ["localhost"])
        self.assertEqual(sf("commit", tid, "--tab-url", f"http://127.0.0.1:{SPORT}/paid", "--wait", "0")[0], 6)
        rc, out, err = wa("done", tid, "--note", "paid")
        self.assertEqual(rc, 0, out + err)
        text = self.report(n1)
        self.assertIn("task done", text)
        self.assertIn("💳 Card used 1×: CHF 1.00 · “Pay now”", text)
        self.assertIn("• 💳 card: holder, number, exp month, exp year, cvc", text)
        self.assertIn("• passport: number, expiry date", text)
        self.assertIn("Into: 127.0.0.1, localhost", text)
        close_tab(tb)

    def test_3_deny(self):
        set_mode("deny")
        try:
            tid, _ = self.start("other")
            tb = tab(URL)
            n0 = len(sends())
            rc, out, err = sf("fill", tid, "--tab-url", URL, "--wait", "10")
            self.assertEqual(rc, 5, out + err)
            self.assertEqual(json.loads(out)["session"], "denied")
            self.assertEqual(len(sends()), n0 + 1)
            self.assertEqual(pw("values", URL)[f"127.0.0.1:{SPORT}"]["firstName"][0], "")
            rc, out, err = sf("fill", tid, "--tab-url", URL, "--wait", "0")
            self.assertEqual(rc, 5, out + err)                    # not asked again by itself
            self.assertEqual(len(sends()), n0 + 1)
            self.assertEqual(os.listdir(os.path.join(TMP, "state", "keys")), [])
            wa("abort", tid, "--reason", "agent")
            self.assertEqual(len(sends()), n0 + 1)                # a denied session sends no report
            close_tab(tb)
        finally:
            set_mode("approve")

    def test_4_tick_under_session(self):
        set_mode("approve")
        tid, t = self.start("form")
        self.assertEqual(t["fill_session"]["state"], "pending")
        self.assertEqual(sf("session", "start", tid, "--wait", "10")[0], 0)
        tb = tab(URL)
        rc, out, err = wa("stop", tid, "--label", "Submit details", "--what", "sends the form", "--shot", self.shot,
                          "--tick", "I have read the privacy notice")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(wa("approve", tid, "--simulate")[0], 0)
        rc, out, err = sf("tick", tid, "--tab-url", URL)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(json.loads(out)["ticks"], [{"label": "I have read the privacy notice", "result": "ticked"}])
        self.assertTrue(pw("boxes", URL)["privacy"])
        self.assertEqual(json.loads(wa("status", tid, "--json")[1])["state"], "approved")
        n0 = len(sends())
        rc, out, err = sf("session", "end", tid, "--why", "done")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(json.loads(out)["session"], "ended")
        text = self.report(n0)
        self.assertIn("Nothing was shared.", text)
        self.assertIn("💳 Card: not used", text)
        wa("abort", tid, "--reason", "agent")
        self.assertEqual(len(sends()), n0 + 1)                    # ended already: no second report
        close_tab(tb)

    def task_rec(self, tid):
        with open(os.path.join(ENV["WEBAGENT_STATE"], f"{tid}.json")) as f:
            return json.load(f)

    def wa_env(self, env, *a):
        rc, out, err = run(["python3", WA_CLI, *a], env=env)
        return rc, (json.loads(out) if out.strip().startswith("{") else out), err

    def test_5_waiting_task_does_not_go_stale(self):
        """A task waiting for his vault ✅ does not go stale (45 min without a step; here 1 s) before its deadline."""
        set_mode("approve")
        tid, _ = self.start("other")
        tb = tab(URL)
        n0 = len(sends())
        ttl1 = dict(ENV, WEBAGENT_RUNNING_TTL="1")
        rc, out, err = sf("fill", tid, "--tab-url", URL, "--wait", "0")
        self.assertEqual(rc, 10, out + err)
        w = self.task_rec(tid)["waiting"]                         # waits until the request's deadline
        self.assertAlmostEqual(w["until"], _iso(json.loads(out)["decide_by"]), delta=2)
        time.sleep(1.5)
        rc, st, err = self.wa_env(ttl1, "status", tid)
        self.assertEqual((st["state"], bool(st["waiting"])), ("running", True), err)
        rc, out, err = sf("fill", tid, "--tab-url", URL, "--wait", "30", env=ttl1)
        self.assertEqual(rc, 0, out + err)                        # his ✅: the same task fills, no redo
        self.assertEqual(len(sends()), n0 + 1)                    # asked once
        t = self.task_rec(tid)
        self.assertNotIn("waiting", t)
        self.assertEqual([h[3] for h in t["history"]], ["waiting: his ✅ in the vault bot", "wait over: active"])
        time.sleep(1.5)
        self.assertEqual(self.wa_env(ttl1, "status", tid)[1]["state"], "stale")   # the clock runs again
        n1 = len(sends())
        self.assertEqual(wa("abort", tid, "--reason", "agent")[0], 0)
        self.report(n1)
        close_tab(tb)

    def test_6_revive_after_his_late_approval(self):
        """The wait lapsed and the task went stale; he approved anyway: revive, fill without a second ✅."""
        set_mode("approve")
        tid, _ = self.start("other")
        tb = tab(URL)
        n0 = len(sends())
        ttl1 = dict(ENV, WEBAGENT_RUNNING_TTL="1")
        rc, out, err = sf("fill", tid, "--tab-url", URL, "--wait", "0", env=dict(ENV, WEBAGENT_WAIT_MAX="1"))
        self.assertEqual(rc, 10, out + err)
        time.sleep(2.5)
        rc, out, err = sf("fill", tid, "--tab-url", URL, "--wait", "0", env=ttl1)
        self.assertEqual(rc, 6, out + err)                        # stale: refused, with the way back
        self.assertIn(f"webagent revive {tid}", json.loads(out)["error"])
        rc, out, err = sf("session", "status", tid)               # his ✅ (the fake taps on the 2nd poll)
        self.assertEqual(json.loads(out)["session"], "active", out + err)
        self.assertEqual(self.wa_env(ttl1, "sweep")[0], 0)
        self.assertEqual(self.task_rec(tid)["state"], "stale")
        self.assertEqual(len(sends()), n0 + 1)                    # sweep kept the live session (no end report)
        rc, rv, err = self.wa_env(ttl1, "revive", tid)
        self.assertEqual((rc, rv["state"], rv["fill_session"]), (0, "running", "active"), err)
        self.assertEqual(self.task_rec(tid)["history"][-1][1:], ["stale", "running", "revive (fill session active)"])
        rc, out, err = sf("fill", tid, "--tab-url", URL, "--wait", "0")
        self.assertEqual(rc, 0, out + err)                        # no second ✅ in the vault bot
        no_secret(self, out, err)
        self.assertEqual(len(sends()), n0 + 1)
        self.assertEqual(self.wa_env(ENV, "revive", tid)[0], 6)   # running: nothing to revive
        n1 = len(sends())
        self.assertEqual(wa("abort", tid, "--reason", "agent")[0], 0)
        self.assertIn("task stopped", self.report(n1))
        close_tab(tb)


NODE_HELPER = os.path.join(SF_LIB, "securefill-cdp.mjs")
# released text values (fake): names with an umlaut and a hyphen, an e-mail on an own domain, a street with weak words,
# a house number, a town with a stop word
FAKE_TEXT = {"name.given_names": "Testo Jörg", "name.surname": "Probewald-Muster", "contact.email": "testo.probe@probe-laden.example",
             "address.street": "Rue de la Gartenweg", "address.house_number": "12a", "address.postal_code": "8471",
             "address.city": "Neudorf am Testsee", "identity.birth_place": "Altstetten", "card.holder": "TESTO PROBEWALD-MUSTER"}
FAKE_WORDS = ("Testo", "Jörg", "Joerg", "Jorg", "Probewald", "Muster", "testo.probe", "Gartenweg", "8471", "Neudorf", "Testsee",
              "Altstetten", "probe-laden", "laden.example")
FAKE_PHONE = "791234567"


def no_words(tc, text, words=FAKE_WORDS):
    import re
    for w in words:
        tc.assertIsNone(re.search(r"(?<![0-9A-Za-zÀ-ÿ])" + re.escape(w) + r"(?![0-9A-Za-zÀ-ÿ])", text, re.I),
                        f"{w!r} left in {text!r}")


class TText(unittest.TestCase):
    """Released text values (names, e-mail, street, town) out of browser output, snapshot files and Bash / Read / Grep
    output: word-token fingerprints made by the browser half (op fpitems = the same code as a fill), matched by the hook.
    Own state dir and MCP output dir; no browser."""

    @classmethod
    def setUpClass(cls):
        import securefill as S
        cls.S = S
        cls.st = os.path.join(TMP, "tstate")
        cls.out = os.path.join(TMP, "mcp-out")
        os.makedirs(os.path.join(cls.st, "fp"), mode=0o700)
        os.makedirs(cls.out)
        kp = os.path.join(cls.st, "fp.key")
        key = os.urandom(32).hex()
        with open(kp, "w") as f:
            f.write(key)
        p = subprocess.run(["node", NODE_HELPER], input=json.dumps({"op": "fpitems", "fp_key_path": kp, "vals": FAKE_TEXT}),
                           capture_output=True, text=True, timeout=30)
        items = json.loads(p.stdout.strip().splitlines()[-1])["items"]
        items.append({"kind": "digits", "n": len(FAKE_PHONE), "h": S._h(bytes.fromhex(key), "digits", FAKE_PHONE)})
        cls.fp = os.path.join(cls.st, "fp", "wa000001-1.json")
        with open(cls.fp, "w") as f:
            json.dump({"v": 2, "task": "wa000001", "site": "127.0.0.1", "created": time.time(),
                       "expires": time.time() + 3600, "items": items}, f)
        cls.items_json = p.stdout
        cls.env = dict(ENV, SECUREFILL_STATE=cls.st, SECUREFILL_MCP_OUT=cls.out)
        cls.old_state = S.STATE
        S.STATE = cls.st

    @classmethod
    def tearDownClass(cls):
        cls.S.STATE = cls.old_state

    def hook(self, ev, env=None):
        rc, out, err = run(["python3", CLI, "hook"], input=json.dumps(ev), env=env or self.env)
        self.assertEqual(rc, 0, err)
        return json.loads(out) if out.strip() else None

    def red(self, text):
        return self.S.redact_text(text, self.S.live(), self.S._fp_key())[0]

    def test_1_items_hold_no_value(self):
        no_words(self, self.items_json)
        no_words(self, open(self.fp).read())
        kinds = {i["kind"] for i in json.loads(self.items_json)["items"]}
        self.assertEqual(kinds, {"tok", "seq", "email"})

    def test_2_names_mail_address_out_rest_stays(self):
        text = ("Welcome back, Testo Jörg Probewald-Muster!\nPassenger: PROBEWALD-MUSTER/TESTOMR\n"
                "E-mail: testo.probe@probe-laden.example, Mueller Joerg, JORG\n"
                "Address: Rue de la Gartenweg 12a, 8471 Neudorf am Testsee\nBorn in Altstetten.\n"
                "url: /pax?first=Testo%20J%C3%B6rg&mail=testo.probe%40probe-laden.example\n"
                "json: {\"n\": \"J\\u00f6rg\", \"m\": \"testo.probe\\u0040probe-laden.example\"} html: J&ouml;rg\n"
                "phone +41 79 123 45 67\n"
                "Seat 12A, flight AZ 153; we meet am Montag in the Rue de la Paix, for example near a shop")
        out = self.red(text)
        no_words(self, out)
        self.assertNotIn("79 123 45 67", out)
        for keep in ("Seat 12A", "AZ 153", "am Montag", "Rue de la Paix", "for example near a shop", "Mueller"):
            self.assertIn(keep, out)
        self.assertGreaterEqual(out.count("[securefill]"), 10)

    def test_3_browser_output_and_the_snapshot_file_it_names(self):
        snap = os.path.join(self.out, "page-2026-10-21T10-00-00-000Z.yml")
        with open(snap, "w") as f:
            f.write('- heading "Welcome back, Testo Probewald-Muster" [level=1] [ref=e3]\n'
                    '- cell "testo.probe@probe-laden.example" [ref=e9]\n- cell "Seat 12A" [ref=e10]\n')
        os.chmod(snap, 0o640)
        other = os.path.join(TMP, "elsewhere.yml")                    # named, but outside the MCP output dir
        with open(other, "w") as f:
            f.write("Testo Probewald\n")
        ev = {"tool_name": "mcp__browser__browser_click", "tool_input": {"element": "Next"},
              "tool_response": [{"type": "text", "text": f"### Page\n- Page URL: http://127.0.0.1/x\n### Snapshot\n- [Snapshot]({snap})\n- [x]({other})"}]}
        self.assertIsNone(self.hook(ev))                               # the output itself is clean
        body = open(snap).read()
        no_words(self, body)
        self.assertIn("Seat 12A", body)
        self.assertIn("[ref=e9]", body)
        self.assertEqual(os.stat(snap).st_mode & 0o777, 0o640)
        self.assertEqual(open(other).read(), "Testo Probewald\n")
        ev = {"tool_name": "mcp__browser__browser_find", "tool_input": {"text": "Welcome"},
              "tool_response": [{"type": "text", "text": "Found: heading \"Welcome back, Testo Probewald-Muster\" [ref=e3]"}]}
        new = self.hook(ev)["hookSpecificOutput"]["updatedMCPToolOutput"]
        no_words(self, json.dumps(new, ensure_ascii=False))
        self.assertIn("Welcome back", new[0]["text"])

    def test_4_bash_read_grep_scope(self):
        snap = os.path.join(self.out, "page-2026-10-21T10-00-01-000Z.yml")
        r = {"stdout": "- heading \"Testo Probewald\"\n- text: Seat 12A", "stderr": "", "interrupted": False, "isImage": False}
        ev = {"tool_name": "Bash", "tool_input": {"command": f"sed -n 1,40p {snap}"}, "tool_response": r}
        new = self.hook(ev)["hookSpecificOutput"]["updatedToolOutput"]
        self.assertEqual(set(new), set(r))
        self.assertEqual((new["interrupted"], new["isImage"]), (False, False))
        no_words(self, new["stdout"])
        self.assertIn("Seat 12A", new["stdout"])
        plain = {"tool_name": "Bash", "tool_input": {"command": "cat notes.txt"},
                 "tool_response": {"stdout": "Testo went home", "stderr": "", "interrupted": False}}
        self.assertIsNone(self.hook(plain))                            # main session, nothing browser: untouched
        marked = {"tool_name": "Bash", "tool_input": {"command": "cat /tmp/x.txt"},
                  "tool_response": {"stdout": "- heading \"Testo\" [level=1] [ref=e12]", "stderr": "", "interrupted": False}}
        no_words(self, self.hook(marked)["hookSpecificOutput"]["updatedToolOutput"]["stdout"])
        sub = dict(plain, agent_id="a1")
        self.assertIsNone(self.hook(sub))                              # a subagent, no live fill session
        os.makedirs(os.path.join(self.st, "sess"), exist_ok=True)
        sp = os.path.join(self.st, "sess", "wa000001.json")
        with open(sp, "w") as f:
            json.dump({"sid": "s1", "state": "active", "until": time.time() + 600}, f)
        try:
            no_words(self, self.hook(sub)["hookSpecificOutput"]["updatedToolOutput"]["stdout"])
            self.assertIsNone(self.hook(plain))                        # the main session still untouched
        finally:
            os.unlink(sp)
        rd = {"tool_name": "Read", "tool_input": {"file_path": snap},
              "tool_response": {"type": "text", "file": {"filePath": snap, "content": "1\tTesto Probewald-Muster", "numLines": 1,
                                                         "startLine": 1, "totalLines": 1}}}
        new = self.hook(rd)["hookSpecificOutput"]["updatedToolOutput"]
        self.assertEqual(new["file"]["filePath"], snap)
        no_words(self, new["file"]["content"])
        gr = {"tool_name": "Grep", "tool_input": {"pattern": "Welcome", "path": self.out, "output_mode": "content"},
              "tool_response": {"mode": "content", "content": f"{snap}:1:Welcome back, Testo", "numLines": 1, "numFiles": 1}}
        no_words(self, self.hook(gr)["hookSpecificOutput"]["updatedToolOutput"]["content"])
        big = os.path.join(TMP, "persisted-1.txt")
        with open(big, "w") as f:
            f.write("x\n" * 10 + "Testo Probewald\n")
        pb = {"tool_name": "Bash", "tool_input": {"command": f"cat {self.out}/page-*.yml"},
              "tool_response": {"stdout": "x\nTesto", "stderr": "", "interrupted": False, "persistedOutputPath": big}}
        self.hook(pb)
        no_words(self, open(big).read())

    def test_5_fail_closed_without_key(self):
        st2 = os.path.join(TMP, "tstate2")
        shutil.copytree(self.st, st2)
        with open(os.path.join(st2, "fp.key"), "w") as f:
            f.write("zz")
        env = dict(self.env, SECUREFILL_STATE=st2)
        ev = {"tool_name": "mcp__browser__browser_snapshot", "tool_response": [{"type": "text", "text": "Testo"}]}
        new = self.hook(ev, env)["hookSpecificOutput"]["updatedMCPToolOutput"]
        self.assertIn("withheld", new[0]["text"])
        b = {"tool_name": "Bash", "tool_input": {"command": f"cat {self.out}/page-1.yml"},
             "tool_response": {"stdout": "Testo", "stderr": "", "interrupted": False}}
        new = self.hook(b, env)["hookSpecificOutput"]["updatedToolOutput"]
        self.assertIn("withheld", new["stdout"])
        self.assertIs(new["interrupted"], False)
        self.assertIsNone(self.hook({"tool_name": "Bash", "tool_input": {"command": "ls"},
                                     "tool_response": {"stdout": "a", "stderr": "", "interrupted": False}}, env))

    def test_6_speed(self):
        import random as R
        rng = R.Random(5)
        vocab = ["button", "link", "Continue", "Flight", "Zurich", "Milan", "price", "CHF", "Economy", "seat", "baggage",
                 "generic", "text", "cell", "row", "listitem", "Fare", "rules", "select", "option", "Switzerland"]
        lines = []
        for i in range(1500):
            ws = " ".join(rng.choice(vocab) for _ in range(rng.randint(2, 8)))
            lines.append(f'  - {rng.choice(vocab)} "{ws}" [ref=e{i}]')
        lines[700] = '  - heading "Welcome back, Testo Probewald-Muster" [ref=e700]'
        text = "\n".join(lines)
        t0 = time.time()
        out = self.red(text)
        ms = (time.time() - t0) * 1000
        no_words(self, out)
        ev = {"tool_name": "mcp__browser__browser_snapshot", "tool_response": [{"type": "text", "text": text}]}
        t0 = time.time()
        self.hook(ev)
        hook_ms = (time.time() - t0) * 1000
        t0 = time.time()
        self.hook({"tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_response": {"stdout": "a\nb", "stderr": "",
                                                                                           "interrupted": False}})
        idle_ms = (time.time() - t0) * 1000
        print(f"\n  speed: {len(text) // 1024} KB snapshot redact {ms:.0f} ms in process, hook {hook_ms:.0f} ms; "
              f"out-of-scope Bash hook {idle_ms:.0f} ms", file=sys.stderr)
        self.assertLess(ms, 400)
        self.assertLess(idle_ms, 400)


class THidden(unittest.TestCase):
    """The field filter (shown() in securefill-cdp.mjs; per-fill vault path like class T): fields a person cannot see or
    use are neither asked for nor filled: opacity 0 on the input or an ancestor, visibility, display, zero / 1 px size,
    an sr-only clip, a collapsed overflow box, clip-path, off the page (left; above, in a fixed layer), aria-hidden,
    inert, pointer-events none, a closed <dialog>, Stripe's invisible helper input, a same-site login frame in a faded-out
    popup, a payment frame in a transparent wrapper, a hidden decoy inside the card frame, the form behind an open modal
    dialog; a field that fades out after the scan is not typed (result "hidden", left for the agent). Visible ones still
    are: a styled radio (transparent input, visible label), a transparent native select over its styled box, a field
    below the fold, one in a scroll box, and the Stripe-like card frame at commit."""
    HIDDEN = f"http://127.0.0.1:{SPORT}/hidden"
    MODAL = f"http://127.0.0.1:{SPORT}/modal"
    VISIBLE = ["address.city", "address.country", "address.house_number", "address.postal_code", "address.street_full",
               "name.given_names", "name.surname", "name.title", "name.title"]   # two title radios
    NEEDS = ["address.city", "address.country", "address.house_number", "address.postal_code", "address.street",
             "name.given_names", "name.surname", "name.title"]
    DECOYS = ("d_opacity", "d_anc_opacity", "d_visibility", "d_display", "d_zero", "d_tiny", "d_clip", "d_collapsed",
              "d_clippath", "d_offscreen", "d_fixed_off", "d_aria", "d_inert", "d_pointer", "d_dialog", "d_stripe",
              "d_select")

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(TMP, "sessions"), "w") as f:
            f.write("off")
        cls.no_psp()

    @staticmethod
    def no_psp():
        with open(os.path.join(TMP, "conf.json"), "w") as f:
            json.dump({"psp": []}, f)

    def tearDown(self):
        while OPEN:
            close_tab(OPEN.pop())
        self.no_psp()

    def assert_decoys_empty(self, vals):
        main = vals["/hidden"]
        for d in self.DECOYS:
            self.assertEqual(main[d][0], "", d)
        self.assertEqual(vals["/loginframe"]["d_frame_email"][0], "")              # the faded-out login popup's frame
        hid = vals["/stripeframe?hidden=1"]                                           # the payment frame nobody sees
        self.assertEqual({k: v[0] for k, v in hid.items()}, {k: "" for k in hid})
        self.assertEqual(vals["/stripeframe"]["d_ghost"][0], "")                    # the card frame's own decoy

    def test_1_probe_lists_only_visible_fields(self):
        tid, _ = task(self.HIDDEN), tab(self.HIDDEN)
        rc, out, err = sf("probe", tid, "--tab-url", self.HIDDEN)
        self.assertEqual(rc, 0, out + err)
        p = json.loads(out)
        main = [f for f in p["frames"] if f["main"]][0]
        self.assertEqual(sorted(f["key"] for f in main["fields"]), self.VISIBLE, main["fields"])
        self.assertEqual(sorted(main["needs"]), self.NEEDS)
        self.assertEqual(main["unknown"], 0)                                          # decoys are not even "unknown"
        sub = [f for f in p["frames"] if not f["main"]]
        hidden = sorted(f["host"] for f in sub if f.get("hidden"))
        self.assertEqual(hidden, ["127.0.0.1", "localhost"])                          # login popup + wrapped card frame
        for f in sub:
            if f.get("hidden"):
                self.assertEqual((f["fields"], f["needs"]), ([], []), f)
        card = [f for f in sub if not f.get("hidden")]
        self.assertEqual(len(card), 1)
        self.assertEqual(sorted(x["key"] for x in card[0]["fields"]), ["card.cvc", "card.exp", "card.number"])
        self.assertEqual(p["card_frames_blocked"], ["localhost"])                    # the visible one only (not on the PSP list)
        rc, out, err = sf("probe", tid, "--tab-url", self.HIDDEN)                     # stable on a second scan
        self.assertEqual(sorted(f["key"] for f in [f for f in json.loads(out)["frames"] if f["main"]][0]["fields"]), self.VISIBLE)

    def test_2_fill_asks_and_types_only_visible_fields(self):
        tid, _ = task(self.HIDDEN), tab(self.HIDDEN)
        with open(os.path.join(TMP, "mode"), "w") as f:
            f.write("approve")
        rc, out1, err1 = sf("fill", tid, "--tab-url", self.HIDDEN, "--wait", "0")
        self.assertEqual(rc, 10, out1 + err1)
        prev = [json.loads(x) for x in open(os.path.join(TMP, "previews.jsonl")).read().splitlines()][-1]
        self.assertEqual(sorted(prev["fields"]), self.NEEDS)                         # no e-mail, phone, passport, birth date
        rc, out2, err2 = sf("fill", tid, "--tab-url", self.HIDDEN, "--wait", "30")
        self.assertEqual(rc, 0, out2 + err2)
        no_secret(self, out1, err1, out2, err2)
        res = json.loads(out2)
        self.assertEqual(sorted(res["filled"]), sorted(set(self.VISIBLE)), res["filled"])
        self.assertEqual({k: v for k, v in res["filled"].items() if v not in ("ok", "same")}, {})
        vals = pw("framevalues", self.HIDDEN)
        m = vals["/hidden"]
        self.assertEqual((m["firstName"][0], m["lastName"][0], m["street"][0], m["houseNo"][0], m["zip"][0], m["city"][0]),
                         ("Testo", "Probewald", "Teststrasse", "12a", "8000", "Zuerich"))
        self.assertEqual((m["sal=MR"][1], m["sal=MS"][1]), (True, False))             # the styled radio
        self.assertEqual(m["country"][0], "CH")                                       # the transparent select
        self.assert_decoys_empty(vals)
        self.assertEqual(vals["/stripeframe"]["cardnumber"][0], "")                  # no card outside commit

    def test_3_commit_fills_the_stripe_like_card_frame_only(self):
        with open(os.path.join(TMP, "conf.json"), "w") as f:
            json.dump({"psp": ["localhost"]}, f)                                       # the test's "payment provider"
        with open(os.path.join(TMP, "mode"), "w") as f:
            f.write("approve")
        purl = f"http://127.0.0.1:{SPORT}/plain"                                   # the clean shot for the card first:
        pt = tab(purl)                                                                 # the commit's own screenshot can hang
        shotp = os.path.join(TMP, "plain-hidden.png")                                 # in a background tab of headless Chrome
        pw("screenshot", purl, path=shotp)                                             # after an earlier commit (old code too)
        close_tab(pt)
        tid, _ = task(self.HIDDEN), tab(self.HIDDEN)
        rc, out, err = wa("stop", tid, "--label", "Pay now", "--what", "pays the test order", "--shot", shotp,
                          "--card", "--amount", "CHF 1.00", "--summary", "test parts order")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(wa("approve", tid, "--simulate")[0], 0)
        rc, out, err = sf("commit", tid, "--tab-url", self.HIDDEN, "--wait", "30")
        self.assertEqual(rc, 0, out + err)
        no_secret(self, out, err)
        res = json.loads(out)
        self.assertTrue(res["clicked"])
        card = {k: v for k, v in res["filled"].items() if k.startswith("card.")}
        self.assertEqual(card, {"card.number [localhost]": "ok", "card.exp [localhost]": "ok", "card.cvc [localhost]": "ok"})
        vals = pw("framevalues", self.HIDDEN)
        sv = vals["/stripeframe"]
        self.assertEqual(sv["cardnumber"][0].replace(" ", ""), CARD)
        self.assertEqual("".join(c for c in sv["exp-date"][0] if c.isdigit()), "0829")
        self.assertEqual(sv["cvc"][0], CVC)
        self.assert_decoys_empty(vals)

    def test_4_open_modal_dialog_hides_the_page_behind(self):
        tid, _ = task(self.MODAL), tab(self.MODAL)
        rc, out, err = sf("probe", tid, "--tab-url", self.MODAL)
        self.assertEqual(rc, 0, out + err)
        main = [f for f in json.loads(out)["frames"] if f["main"]][0]
        self.assertEqual([f["key"] for f in main["fields"]], ["name.surname"], main["fields"])

    def test_5_field_hidden_after_the_scan_is_not_typed(self):
        url = f"http://127.0.0.1:{SPORT}/late"
        tid, _ = task(url), tab(url)
        with open(os.path.join(TMP, "mode"), "w") as f:
            f.write("approve")
        self.assertEqual(sf("fill", tid, "--tab-url", url, "--wait", "0")[0], 10)
        rc, out, err = sf("fill", tid, "--tab-url", url, "--wait", "30")
        self.assertEqual(rc, 0, out + err)
        no_secret(self, out, err, also=("testo@example.org",))
        res = json.loads(out)
        self.assertEqual(res["filled"], {"name.given_names": "ok", "contact.email": "hidden"})
        left = {x["field"]: x for x in res.get("left_for_agent") or []}
        self.assertEqual(left["contact.email"]["result"], "hidden")
        m = pw("framevalues", url)["/late"]
        self.assertEqual((m["firstName"][0], m["email"][0]), ("Testo", ""))


class TPickers(unittest.TestCase):
    """Custom pickers (per-fill vault path, like class T): Title as a button + listbox, Gender as button radios,
    Nationality as a typeahead, billing Country as a button opening plain buttons behind a filter box; the header's locale
    button never clicked; the title from the stored sex; what stays unset listed for the agent; a confirmation page
    showing the name: page text masked in `securefill shot`, an unmasked screenshot refused."""
    CUSTOM = f"http://127.0.0.1:{SPORT}/custom"

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(TMP, "sessions"), "w") as f:
            f.write("off")

    def tearDown(self):
        while OPEN:
            close_tab(OPEN.pop())
        with open(os.path.join(TMP, "values.json"), "w") as f:
            json.dump(VALUES, f)

    def values(self, drop=(), **extra):
        v = {k: x for k, x in VALUES.items() if k not in drop}
        v.update({k.replace("__", "."): x for k, x in extra.items()})
        with open(os.path.join(TMP, "values.json"), "w") as f:
            json.dump(v, f)

    def fill(self, tid, url):
        with open(os.path.join(TMP, "mode"), "w") as f:
            f.write("approve")
        rc, out1, err1 = sf("fill", tid, "--tab-url", url, "--wait", "0")
        self.assertEqual(rc, 10, out1 + err1)
        rc, out2, err2 = sf("fill", tid, "--tab-url", url, "--wait", "30")
        no_secret(self, out1, err1, out2, err2, also=("Testo", "Probewald", "testo@example.org"))
        return rc, json.loads(out2), out2 + err2

    def test_1_custom_pickers_title_from_sex(self):
        self.values(drop=("name.title",), identity__sex="M")
        tid, _ = task(self.CUSTOM), tab(self.CUSTOM)
        rc, out, err = sf("probe", tid, "--tab-url", self.CUSTOM)
        self.assertEqual(rc, 0, out + err)
        main = [f for f in json.loads(out)["frames"] if f["main"]][0]
        kinds = {f["key"]: f["kind"] for f in main["fields"]}
        self.assertEqual((kinds.get("name.title"), kinds.get("identity.sex"), kinds.get("identity.nationality"),
                          kinds.get("address.country")), ("popup", "xradio", "typeahead", "popup"), main["fields"])
        self.assertEqual(sum(1 for f in main["fields"] if f["key"] == "address.country"), 1)   # not the locale button
        rc, res, txt = self.fill(tid, self.CUSTOM)
        self.assertEqual(rc, 0, txt)
        for k in ("name.title", "identity.sex", "identity.nationality", "address.country", "name.given_names", "name.surname"):
            self.assertIn(res["filled"].get(k), ("ok", "same"), (k, res["filled"]))
        self.assertNotIn("name.title", res["missing_in_vault"])
        st = pw("state", self.CUSTOM)
        self.assertEqual((st["title"], st["sex"], st["nat"], st["country"], st["locale_clicked"]), ("MR", "M", "CH", "CH", False))

    def test_2_title_left_for_agent(self):
        self.values(drop=("name.title",))
        tid, _ = task(self.CUSTOM), tab(self.CUSTOM)
        rc, res, txt = self.fill(tid, self.CUSTOM)
        self.assertEqual(rc, 0, txt)
        self.assertEqual(res["filled"].get("name.title"), "no_value")
        left = {x["field"]: x for x in res.get("left_for_agent") or []}
        self.assertIn("name.title", left)
        self.assertIn("Mr / Ms", left["name.title"]["todo"])
        self.assertIn("name.title", res["missing_in_vault"])
        self.assertEqual(pw("state", self.CUSTOM)["title"], "")

    def test_3_page_text_masked_in_shot(self):
        tid, _ = task(self.CUSTOM), tab(self.CUSTOM)
        rc, res, txt = self.fill(tid, self.CUSTOM)                     # fingerprints of the current values
        self.assertEqual(rc, 0, txt)
        hurl = f"http://127.0.0.1:{SPORT}/hello"
        tab(hurl)
        raw = os.path.join(TMP, "hello-raw.png")
        pw("screenshot", hurl, path=raw)
        self.assertEqual(sf("check-image", raw)[0], 3)                  # the name is readable: refused
        rd = {"tool_name": "Read", "tool_input": {"file_path": raw},    # an agent reading the saved screenshot
              "tool_response": {"type": "image", "file": {"base64": "AAAA", "type": "image/png", "originalSize": 4}}}
        rc, hout, _ = run(["python3", CLI, "hook"], input=json.dumps(rd), env=dict(ENV, SECUREFILL_MCP_OUT=TMP))
        new = json.loads(hout)["hookSpecificOutput"]["updatedToolOutput"]
        self.assertEqual((new["type"], new["file"]["filePath"]), ("text", raw))
        self.assertIn("withheld", new["file"]["content"])
        rc, out, err = sf("shot", tid, "--tab-url", hurl)
        self.assertEqual(rc, 0, out + err)
        shot = json.loads(out)["path"]
        self.assertEqual(sf("check-image", shot)[0], 0)
        snap = pw("snapshot", hurl)["text"]
        ev = {"tool_name": "mcp__browser__browser_snapshot", "tool_response": [{"type": "text", "text": snap}]}
        rc, hout, _ = run(["python3", CLI, "hook"], input=json.dumps(ev))
        new = json.loads(hout)["hookSpecificOutput"]["updatedMCPToolOutput"]
        no_secret(self, json.dumps(new), also=("Testo", "Probewald", "testo@example.org"))
        self.assertIn("AZ 153", new[0]["text"])


WRAPPER = os.path.join(os.environ.get("STAGED_HOOKS") or os.path.join(HOME, ".claude", "hooks"), "securefill-redact.sh")
OWNER_WORDS = ("Quirinella", "Zwoggenbach", "Zwirbelstrasse", "4719", "quirinella.zwoggen", "probe-mail", "555 01 23",
               "4111 1111", "0999 9000")


def ocr_text(data):
    return subprocess.run(["tesseract", "stdin", "stdout", "--psm", "11", "-l", "eng"], input=data, capture_output=True,
                          timeout=90).stdout.decode()


def no_owner(tc, text, what=""):
    import re
    for w in OWNER_WORDS:
        tc.assertIsNone(re.search(r"(?<![0-9A-Za-z])" + re.escape(w) + r"(?![0-9A-Za-z])", text, re.I), f"{w!r} in {what}")


class TOwner(unittest.TestCase):
    """His personal data (the synthetic OWNER_PROFILE) out of browser tool output with NO secure fill anywhere: every
    browser result, always (text -> [personal], the snapshot file it names rewritten, screenshots inline and saved
    blacked out by OCR word boxes with his town kept, or withheld), Bash / Read / Grep on browser output, the card shot
    keeping his town, and fail closed (broken masker, missing or crashing hook lib, no OCR -> withheld). Own empty state
    dir (no fingerprints) and MCP output dir."""

    @classmethod
    def setUpClass(cls):
        cls.st = os.path.join(TMP, "ostate")
        os.makedirs(cls.st, mode=0o700)
        cls.out = os.path.join(TMP, "omcp-out")
        os.makedirs(cls.out)
        cls.env = dict(ENV, SECUREFILL_STATE=cls.st, SECUREFILL_MCP_OUT=cls.out, SECUREFILL_LIB=SF_LIB)
        cls.url = f"http://127.0.0.1:{SPORT}/owner"
        cls.tab = tab(cls.url)
        cls.png = os.path.join(TMP, "owner.png")
        pw("screenshot", cls.url, path=cls.png)
        with open(cls.png, "rb") as f:
            cls.raw = f.read()

    @classmethod
    def tearDownClass(cls):
        close_tab(cls.tab)

    def hook(self, ev, env=None, wrapper=False, python="python3"):
        argv = [WRAPPER] if wrapper else [python, CLI, "hook"]
        p = subprocess.run(argv, input=json.dumps(ev), capture_output=True, text=True, env=env or self.env, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr[-500:])
        return json.loads(p.stdout)["hookSpecificOutput"] if p.stdout.strip() else None

    def shot_ev(self, saved=None, sid="osess", filename=None):
        text = "### Result\n" + (f"- [Screenshot of viewport]({saved})\n" if saved else "")
        ev = {"tool_name": "mcp__browser__browser_take_screenshot", "tool_input": {"filename": filename} if filename else {},
              "session_id": sid, "transcript_path": os.path.join(TMP, "oproj", f"{sid}.jsonl"),
              "tool_response": [{"type": "text", "text": text}]}
        if not filename:
            ev["tool_response"].append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                                    "data": base64.b64encode(self.raw).decode()}})
        return ev

    def test_1_text_always_masked(self):
        self.assertFalse(os.path.exists(os.path.join(self.st, "fp")))      # no secure fill
        snap = pw("snapshot", self.url)["text"]
        self.assertIn("Zwoggenbach", snap)
        for tool, resp in (("browser_snapshot", [{"type": "text", "text": snap}]),
                           ("browser_evaluate", [{"type": "text", "text": '"Quirinella\\nZwoggenbach\\nZwirbelstrasse 17"'}]),
                           ("browser_network_requests", [{"type": "text", "text": "GET /a?mail=quirinella.zwoggen%40probe-mail.example"}]),
                           ("browser_console_messages", "Error: user Quirinella Zwoggenbach, IBAN CH96 0999 9000 0001 2345 6")):
            new = self.hook({"tool_name": f"mcp__browser__{tool}", "tool_response": resp})["updatedMCPToolOutput"]
            no_owner(self, json.dumps(new, ensure_ascii=False), tool)
            self.assertIn("[personal]", json.dumps(new))
        new = self.hook({"tool_name": "mcp__browser__browser_snapshot", "tool_response": [{"type": "text", "text": snap}]})
        txt = new["updatedMCPToolOutput"][0]["text"]
        for keep in ("AZ 153", "Lieferadresse", "Bestellung pruefen"):
            self.assertIn(keep, txt)
        clean = {"tool_name": "mcp__browser__browser_snapshot", "tool_response": [{"type": "text", "text": "- button \"Pay\""}]}
        self.assertIsNone(self.hook(clean))

    def test_2_snapshot_file_rewritten(self):
        snap = os.path.join(self.out, "page-2026-10-23T10-00-00-000Z.yml")
        with open(snap, "w") as f:
            f.write('- heading "Hallo Quirinella Zwoggenbach" [ref=e3]\n- text: Zwirbelstrasse 17, 4719 Quaxingen\n'
                    '- cell "Seat 12A" [ref=e10]\n')
        os.chmod(snap, 0o640)
        other = os.path.join(TMP, "o-elsewhere.yml")
        with open(other, "w") as f:
            f.write("Quirinella\n")
        ev = {"tool_name": "mcp__browser__browser_click", "tool_input": {"element": "Next"},
              "tool_response": [{"type": "text", "text": f"### Snapshot\n- [Snapshot]({snap})\n- [x]({other})"}]}
        self.assertIsNone(self.hook(ev))                                   # the output itself is clean
        body = open(snap).read()
        no_owner(self, body, "the snapshot file")
        self.assertIn("Seat 12A", body)
        self.assertEqual(os.stat(snap).st_mode & 0o777, 0o640)
        self.assertEqual(open(other).read(), "Quirinella\n")

    def test_3_screenshots_inline_and_saved(self):
        self.assertIn("Zwoggenbach", ocr_text(self.raw))                  # readable before
        saved = os.path.join(self.out, "page-2026-10-23T10-00-01-000Z.png")
        with open(saved, "wb") as f:
            f.write(self.raw)
        blobs = os.path.join(TMP, "oproj", "osess", "tool-results")
        os.makedirs(blobs)
        blob = os.path.join(blobs, "mcp-browser-blob-1.png")
        shutil.copyfile(self.png, blob)
        new = self.hook(self.shot_ev(saved))["updatedMCPToolOutput"]
        imgs = [b for b in new if b.get("type") == "image"]
        self.assertEqual(len(imgs), 1)
        self.assertEqual(imgs[0]["source"]["media_type"], "image/png")
        data = base64.b64decode(imgs[0]["source"]["data"])
        t = ocr_text(data)
        no_owner(self, t, "the masked screenshot")
        for keep in ("Quaxingen", "Brumsee", "AZ 153", "Lieferadresse"):  # his town stays readable
            self.assertIn(keep, t)
        self.assertTrue(any("blacked out" in b.get("text", "") for b in new))
        self.assertEqual(open(saved, "rb").read(), data)                   # the saved copy: masked too
        self.assertEqual(open(blob, "rb").read(), data)                    # Claude Code's blob copy too
        named = os.path.join(self.out, "card.png")                         # a screenshot saved under a filename only
        shutil.copyfile(self.png, named)
        new = self.hook(self.shot_ev(named, filename="card.png"))["updatedMCPToolOutput"]
        self.assertFalse([b for b in new if b.get("type") == "image"])
        self.assertIn("card.png has his personal data blacked out", json.dumps(new))
        no_owner(self, ocr_text(open(named, "rb").read()), "the saved card.png")
        raw_shape = {"tool_name": "mcp__browser__browser_take_screenshot", "tool_response": [
            {"type": "image", "data": base64.b64encode(self.raw).decode(), "mimeType": "image/png"}]}
        new = self.hook(raw_shape)["updatedMCPToolOutput"]
        no_owner(self, ocr_text(base64.b64decode(new[0]["data"])), "the MCP-shaped image")
        purl = f"http://127.0.0.1:{SPORT}/plain"                          # a clean page: untouched
        pt = tab(purl)
        ppng = os.path.join(TMP, "oplain.png")
        pw("screenshot", purl, path=ppng)
        ev = self.shot_ev()
        ev["tool_response"][1]["source"]["data"] = base64.b64encode(open(ppng, "rb").read()).decode()
        self.assertIsNone(self.hook(ev))
        close_tab(pt)

    def test_4_read_bash_grep_on_browser_output(self):
        shot = os.path.join(self.out, "ab-shot.png")
        shutil.copyfile(self.png, shot)
        rd = {"tool_name": "Read", "tool_input": {"file_path": shot},
              "tool_response": {"type": "image", "file": {"base64": base64.b64encode(self.raw).decode(), "type": "image/png",
                                                          "originalSize": len(self.raw)}}}
        new = self.hook(rd, wrapper=True)["updatedToolOutput"]
        self.assertEqual(new["type"], "image")
        self.assertEqual(new["file"]["originalSize"], len(self.raw))
        no_owner(self, ocr_text(base64.b64decode(new["file"]["base64"])), "the Read image")
        self.assertEqual(open(shot, "rb").read(), self.raw)                 # the file itself stays (his card)
        elsewhere = dict(rd, tool_input={"file_path": os.path.join(TMP, "photo.png")})
        self.assertIsNone(self.hook(elsewhere, wrapper=True))              # not browser output: untouched
        grab = dict(rd, tool_input={"file_path": "/home/x/reports/browser/screen-1.png"})   # `browser screenshot`
        no_owner(self, ocr_text(base64.b64decode(self.hook(grab, wrapper=True)["updatedToolOutput"]["file"]["base64"])),
                 "a display grab")
        snap = os.path.join(self.out, "page-2026-10-23T10-00-02-000Z.yml")
        r = {"stdout": "- heading \"Quirinella Zwoggenbach\"\n- text: Seat 12A", "stderr": "", "interrupted": False}
        for wrapper in (False, True):
            new = self.hook({"tool_name": "Bash", "tool_input": {"command": f"cat {snap}"}, "tool_response": r},
                            wrapper=wrapper)["updatedToolOutput"]
            no_owner(self, new["stdout"], "Bash")
            self.assertIn("Seat 12A", new["stdout"])
            self.assertIs(new["interrupted"], False)
        plain = {"tool_name": "Bash", "tool_input": {"command": "cat notes.txt"},
                 "tool_response": {"stdout": "Quirinella went home", "stderr": "", "interrupted": False}}
        self.assertIsNone(self.hook(plain, wrapper=True))                  # not browser output: untouched
        self.assertIsNone(self.hook(dict(plain, agent_id="a1")))
        marked = {"tool_name": "Bash", "tool_input": {"command": "cat /tmp/x.txt"},
                  "tool_response": {"stdout": "- heading \"Quirinella\" [level=1] [ref=e12]", "stderr": "", "interrupted": False}}
        no_owner(self, self.hook(marked, wrapper=True)["updatedToolOutput"]["stdout"], "snapshot-shaped Bash")
        rt = {"tool_name": "Read", "tool_input": {"file_path": snap},
              "tool_response": {"type": "text", "file": {"filePath": snap, "content": "1\tQuirinella Zwoggenbach",
                                                         "numLines": 1, "startLine": 1, "totalLines": 1}}}
        no_owner(self, self.hook(rt)["updatedToolOutput"]["file"]["content"], "Read")
        gr = {"tool_name": "Grep", "tool_input": {"pattern": "Hallo", "path": self.out, "output_mode": "content"},
              "tool_response": {"mode": "content", "content": f"{snap}:1:Hallo Quirinella", "numLines": 1, "numFiles": 1}}
        no_owner(self, self.hook(gr)["updatedToolOutput"]["content"], "Grep")
        saved = {"tool_name": "Read", "tool_input": {"file_path": "/x/tool-results/mcp-browser-browser_find-1.txt"},
                 "tool_response": {"type": "text", "file": {"filePath": "/x", "content": "Quirinella Zwoggenbach"}}}
        no_owner(self, self.hook(saved)["updatedToolOutput"]["file"]["content"], "a saved browser result")

    def test_5_card_shot_keeps_his_town(self):
        rc, out, err = wa("start", "owner card shot", "--url", self.url, "--dry", "--data", "no", "--json",
                            "--retry-of", "none")
        self.assertEqual(rc, 0, out + err)
        tid = json.loads(out)["id"]
        try:
            rc, out, err = run(["python3", CLI, "shot", tid, "--tab-url", self.url], env=self.env)
            self.assertEqual(rc, 0, out + err)
            t = ocr_text(open(json.loads(out)["path"], "rb").read())
            no_owner(self, t, "the card shot")
            for keep in ("Quaxingen", "Brumsee", "AZ 153"):
                self.assertIn(keep, t)
        finally:
            wa("abort", tid, "--reason", "agent")

    def test_6_fails_closed(self):
        ev = {"tool_name": "mcp__browser__browser_snapshot", "tool_response": [{"type": "text", "text": "Hallo Quirinella"}]}
        bash_in = {"tool_name": "Bash", "tool_input": {"command": f"cat {self.out}/page-1.yml"},
                   "tool_response": {"stdout": "Quirinella", "stderr": "", "interrupted": False}}
        bash_out = {"tool_name": "Bash", "tool_input": {"command": "ls"},
                    "tool_response": {"stdout": "a", "stderr": "", "interrupted": False}}
        bad = os.path.join(TMP, "obadlib")
        shutil.copytree(SF_LIB, bad, symlinks=True)
        with open(os.path.join(bad, "ownermask.py"), "w") as f:
            f.write("raise RuntimeError('broken')\n")
        env = dict(self.env, SECUREFILL_LIB=bad)
        new = self.hook(ev, env=env, wrapper=True)["updatedMCPToolOutput"]
        self.assertIn("withheld", new[0]["text"])
        no_owner(self, json.dumps(new), "withheld")
        self.assertIn("withheld", self.hook(bash_in, env=env, wrapper=True)["updatedToolOutput"]["stdout"])
        self.assertIsNone(self.hook(bash_out, env=env, wrapper=True))
        crash = os.path.join(TMP, "ocrashlib")
        os.makedirs(crash)
        with open(os.path.join(crash, "securefill.py"), "w") as f:
            f.write("import sys\nsys.exit(3)\n")
        for lib in (crash, os.path.join(TMP, "no-such-lib")):
            new = self.hook(ev, env=dict(self.env, SECUREFILL_LIB=lib), wrapper=True)["updatedMCPToolOutput"]
            self.assertIn("withheld", new[0]["text"])
            self.assertIsNone(self.hook(bash_out, env=dict(self.env, SECUREFILL_LIB=lib), wrapper=True))
        saved = os.path.join(self.out, "page-2026-10-23T10-00-03-000Z.png")
        shutil.copyfile(self.png, saved)
        new = self.hook(self.shot_ev(saved, sid="osess2"), env=dict(self.env, PATH="/nonexistent"),
                        python="/usr/bin/python3")["updatedMCPToolOutput"]       # no tesseract: nothing can be checked
        self.assertFalse([b for b in new if b.get("type") == "image"])
        self.assertIn("withheld", json.dumps(new))
        self.assertNotEqual(open(saved, "rb").read(), self.raw)             # the saved copy replaced by a blank

    def test_7_speed(self):
        import random as R
        rng = R.Random(6)
        vocab = ["button", "link", "Continue", "Flight", "Zurich", "Milan", "price", "CHF", "Economy", "seat", "row"]
        lines = [f'  - {rng.choice(vocab)} "{" ".join(rng.choice(vocab) for _ in range(5))}" [ref=e{i}]' for i in range(1500)]
        lines[700] = '  - heading "Hallo Quirinella Zwoggenbach" [ref=e700]'
        ev = {"tool_name": "mcp__browser__browser_snapshot", "tool_response": [{"type": "text", "text": "\n".join(lines)}]}
        t0 = time.time()
        new = self.hook(ev, wrapper=True)["updatedMCPToolOutput"]
        ms = (time.time() - t0) * 1000
        no_owner(self, new[0]["text"], "big snapshot")
        t0 = time.time()
        self.assertIsNone(self.hook({"tool_name": "Bash", "tool_input": {"command": "ls"},
                                     "tool_response": {"stdout": "a", "stderr": "", "interrupted": False}}, wrapper=True))
        idle = (time.time() - t0) * 1000
        print(f"\n  owner mask: {len(ev['tool_response'][0]['text']) // 1024} KB snapshot via the hook {ms:.0f} ms; "
              f"out-of-scope Bash {idle:.0f} ms", file=sys.stderr)
        self.assertLess(ms, 2000)
        self.assertLess(idle, 300)


if __name__ == "__main__":
    unittest.main(verbosity=2)
