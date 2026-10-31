#!/usr/bin/env python3
"""securefill: the owner's personal data (name, identity, passport/ID, contact, address, card) from the vault straight
into a web form in my headed Chrome, without any value reaching a model context, a log, an output or a screenshot.

Flow (one web task = webagent task id; CLI ~/bin/securefill; browser half ~/bin/lib/securefill-cdp.mjs):
  session -> ONE vault ✅ per web task: POST /fill/session/start (the task's site = its start URL's registrable
            name, covering subdomains; the payment-provider hosts below as allowed frame hosts; the task title; a time
            limit, default 30 min, at most 60, from his ✅). The vault bot shows him site, task and limit (no field
            list: the pages decide), with ✅ Allow / ❌ Deny and, once live, ⏹ End now. `webagent start` asks at once
            for booking/form tasks (--data), else the first fill asks lazily. It ends at its limit, on `session end`
            (webagent done/abort/sweep), on his ⏹ End now or a vault restart; the vault bot then reports the field
            names shared (never values). A denied / expired / ended session is not asked again by itself: `session
            start` asks again. Local record STATE/sess/<task>.json (session id, site, state; no values).
  probe  -> the task's tab (same site as the task's start URL, or a payment host on the PSP allowlist): which fields
            the form has (keys like passport.number, contact.phone_national; labels; never values).
  wait   -> exit 10 (fill / session start waiting for his ✅) marks the running web task as waiting until the
            request's deadline (webagent.wait_owner: no stale timeout meanwhile); his decision (exit 0 / 4 / 5) ends the
            wait (webagent.wait_over).
  fill   -> the live session (waits up to --wait for his ✅; exit 10 = still waiting) -> one-time X25519 key (made and
            kept by the browser half, 0600, deleted on use) -> POST /fill/session/release (the page host + frame hosts
            + the field names this form needs; the vault checks them against the grant) returns a box sealed to that
            key -> the browser half opens it in memory, checks the tab is still on that host, types the values over
            CDP (only into the main frame and the frame hosts of the release), masks the inputs
            (-webkit-text-security), writes keyed fingerprints (HMAC, no values) for the redaction hook, prints
            field keys with ok/fail.
  commit -> the card at the approved committing step: needs the web-agent ✅ (webagent approve, state approved, stop
            --card --amount: his ✅ on that confirm card with the amount IS the card OK) and the live session (no
            further vault ✅); checks the listed T&C boxes (stop --tick) are each exactly one visible checkbox first,
            releases the card fields (with the amount + button, both in his report), consumes the web-agent approval
            (one use), ticks exactly those boxes, fills the card, clicks the approved button, waits, masks,
            screenshots the result page.
  tick   -> the non-card approved step's listed boxes (stop --tick), ticked over CDP (the guard never sees it); with a
            live session only in the granted site and frame hosts.
  An older vault without sessions (HTTP 404): fill and commit fall back to one vault ✅ per fill (POST /fill/request
  naming site + fields, card: + amount + button; POST /fill/release once, after his ✅).
  hook   -> PostToolUse on mcp__browser__*, Bash, Read, Grep. Always, fill or not: his personal data (ownermask.py:
            name, street + number, postcode + town, e-mail, phone, IBAN, card digits) becomes [personal] in every
            browser tool result, the snapshot / console files it names are rewritten the same way, and screenshots
            (inline and saved) get those words blacked out by OCR word boxes (his town stays readable) or are withheld
            when that fails; Bash / Read / Grep on browser output (MCP / abx output dirs, saved browser results,
            snapshots) likewise, a Read image as a masked copy. While fingerprints are live (24 h after a fill) also
            every released value becomes [securefill]: card/phone digits, passport/ID numbers, dates, labelled CVCs,
            and the words of names, e-mail, street, house number, postal code, town, birth place (keyed hashes of
            normalized word tokens: no value is stored or printed); a screenshot showing one is withheld, copies
            overwritten; Bash / Read / Grep then also on browser files or snapshots (command / path or snapshot
            markers), or any subagent call while a fill session is live. Fails closed: a masking error withholds the
            output (wrapper ~/.claude/hooks/securefill-redact.sh: a crash or timeout withholds browser output too).
  shot / mask -> also black out his profile data (ownermask.py: name, street, postcode, e-mail, phone, IBAN, card
            digits) with or without a fill; `shot` and the commit result shot keep his town readable (the card shows
            him his delivery town); ownermask failing = no screenshot; the shot is then OCR-checked for his profile
            values (owner_in_image). abx masks its own text output the same way.
  pickers-> besides inputs and <select>: custom dropdowns (button / role=combobox that opens a listbox or a list of
            buttons, with a filter box or type-to-jump), always-open listboxes, typeahead inputs and button radios, for
            choice fields only (title, sex, countries, calling code, date parts); never in site header / nav / footer.
            A title comes from the stored title, else the stored sex (and back); what stays unset is listed in the fill
            output as left_for_agent (field key, result, what to do; never a value).
  fields -> only what a person can see and use (securefill-cdp.mjs shown()): nothing transparent (opacity 0 on the
            field or an ancestor), visibility hidden, display none, under 4 px, clipped away, off the page, in a closed
            or behind an open modal dialog, aria-hidden, inert, pointer-events:none, or inside an <iframe> that is not
            shown itself; re-checked right before typing (result "hidden"). Hidden fields are neither asked for nor filled.
State ~/.local/state/securefill (0700): keys/ (one-time keys), sess/<task>.json (the task's fill session), req/<task>.json
(pending per-fill request, older vaults), fp/ (live fingerprints) and fp.key, log.jsonl (counts and outcomes only).
Tests: tests/legacy/webagent/securefill in ~/opt/assistant.
"""
import base64
import glob
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
import unicodedata

HOME = os.path.expanduser("~")
LIB = os.path.dirname(os.path.realpath(__file__))
STATE = os.environ.get("SECUREFILL_STATE") or os.path.join(HOME, ".local/state/securefill")
CONF = os.environ.get("SECUREFILL_CONF") or os.path.join(HOME, ".config/securefill/config.json")
# masked, image-checked screenshots live outside STATE so agents can pass them to `webagent stop --shot`
SHOTS = os.environ.get("SECUREFILL_SHOTS") or (os.path.join(STATE, "shots") if os.environ.get("SECUREFILL_STATE")
                                               else os.path.join(HOME, ".local/share/securefill-shots"))
NODE_HELPER = os.path.join(LIB, "securefill-cdp.mjs")
CDP = os.environ.get("SECUREFILL_CDP") or "http://127.0.0.1:" + (os.environ.get("BROWSER_CDP_PORT") or "9222")
MCP_OUT = os.environ.get("SECUREFILL_MCP_OUT") or os.path.join(HOME, ".local/share/browser/mcp-output")
FEATURE = "bin.securefill"
FP_TTL = int(os.environ.get("SECUREFILL_FP_TTL") or 86400)
REQ_TTL = 900
TEST_HOSTS = {"127.0.0.1", "localhost"}
SESSION_MIN = 30                                       # the vault's default; at most 60
LIVE = ("pending", "active")
OPEN_TASK = ("running", "pending", "approved", "executing")
# a site = the registrable name (two labels, three under these suffixes and hosting platforms); the vault refuses a
# grant on a suffix itself. Same list in securefill-cdp.mjs site().
SLD2 = {"co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "com.au", "net.au", "org.au", "co.nz", "co.jp", "com.br",
        "com.tr", "co.za", "com.mx", "com.ar", "com.sg", "co.in", "co.il", "com.cn", "com.hk", "co.kr",
        "myshopify.com", "github.io", "herokuapp.com", "netlify.app", "vercel.app", "pages.dev", "web.app",
        "firebaseapp.com", "azurewebsites.net", "cloudfront.net", "appspot.com", "wixsite.com", "blogspot.com",
        "wordpress.com", "squarespace.com", "webflow.io", "workers.dev", "onrender.com", "amazonaws.com"}
SESSION_GONE = {
    "denied": "he denied the fill session in the vault bot: nothing filled (ask him first; `securefill session start "
              "<id>` asks the vault again)",
    "expired": "the vault's fill request expired unanswered: `securefill session start <id>` asks him again",
    "ended": "the fill session ended: `securefill session start <id>` asks him again",
    "dead": "3 wrong codes killed the fill request: `securefill session start <id>` asks him again",
    "cancelled": "the fill request was cancelled: `securefill session start <id>` asks him again"}


def site_of(host):
    """The registrable site of a host (IPs and localhost as they are)."""
    h = str(host or "").lower().strip(".")
    if not h or h == "localhost" or re.fullmatch(r"[\d.]+|\[?[0-9a-f:]*:[0-9a-f:]*\]?", h):
        return h
    p = h.split(".")
    if len(p) <= 2:
        return h
    return ".".join(p[-3:]) if ".".join(p[-2:]) in SLD2 else ".".join(p[-2:])


class NoSessions(Exception):
    """The vault has no fill sessions (an older vault: HTTP 404): fall back to one ✅ per fill."""

# Payment frames and hosted payment pages that may receive card fields (exact host or *.suffix). Same-site frames of
# the task's own host are always allowed; anything else never gets a value. Overlay: CONF {"psp": [...]}.
PSP = ["js.stripe.com", "checkout.stripe.com", "*.stripe.com", "*.stripe.network",
       "*.adyen.com", "*.adyenpayments.com",
       "*.worldpay.com", "*.worldpay-online.com", "*.wpcdn.com",
       "*.braintreegateway.com", "*.braintree-api.com",
       "*.checkout.com",
       "*.datatrans.com", "*.saferpay.com", "checkout.postfinance.ch", "*.postfinance.ch", "*.wallee.com",
       "*.payrexx.com", "*.computop.com", "*.nexigroup.com", "*.nexi.it", "*.ogone.com", "*.worldline.com",
       "*.worldline-solutions.com", "*.cybersource.com", "*.realexpayments.com", "*.globalpay.com",
       "*.mollie.com", "*.unzer.com", "*.paypal.com", "*.amadeus.com", "*.amadeus.net", "*.squareup.com"]

# Per-site field rules (regex on the field's label/name/placeholder/autocomplete; first match wins over the generic
# rules). Overlay: CONF {"sites": {host: {"rules": [...], "psp": [...]}}}.
SITES = {
    # ITA Airways shop (Lufthansa Group booking platform): passenger page asks "Phone number without leading 0" with
    # the calling code picked separately; payment frames are added from `securefill probe` on the payment page.
    "shop.ita-airways.com": {"rules": [
        {"match": r"(country|calling|international|dial(l?ing)?)\s*(code|prefix)|prefisso|\bprefix\b|vorwahl", "key": "contact.phone_cc"},
        {"match": r"phone|mobile|telefono|cellulare|without leading", "key": "contact.phone_national"},
        {"match": r"date of birth|data di nascita|birth", "key": "identity.birth_date"},
        {"match": r"\btitle\b|titolo|salutation|\bsig(nor)?\b|mr\b", "key": "name.title"},
        {"match": r"first.?name|given.?name|\bnome\b", "key": "name.given_names"},
        {"match": r"last.?name|surname|cognome", "key": "name.surname"},
        {"match": r"e-?mail", "key": "contact.email"},
    ], "psp": []},
}

_digits = lambda s: re.sub(r"\D", "", str(s or ""))  # noqa: E731


def _webagent():
    sys.path.insert(0, os.environ.get("WEBAGENT_LIB") or LIB)
    import webagent
    return webagent


def _ensure(*sub):
    p = os.path.join(STATE, *sub)
    os.makedirs(p, mode=0o700, exist_ok=True)
    return p


def _shots():
    os.makedirs(SHOTS, mode=0o700, exist_ok=True)
    return SHOTS


def _conf():
    try:
        with open(CONF) as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def psp_hosts(site_host=None):
    c = _conf()
    extra = list(c.get("psp") or [])
    sites = dict(SITES, **(c.get("sites") or {}))
    if site_host and site_host in sites:
        extra += sites[site_host].get("psp") or []
    return PSP + extra


def profiles():
    return dict(SITES, **(_conf().get("sites") or {}))


def _log(rec):
    try:
        _ensure()
        rec = dict(rec, t=round(time.time(), 1))
        with open(os.path.join(STATE, "log.jsonl"), "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def usage(kind, outcome=None, ms=None, attrs=None):
    if os.environ.get("SECUREFILL_USAGE") == "off":
        return
    try:
        sys.path.insert(0, LIB)
        import usage as U
        U.log(FEATURE, kind, channel="browser", ms=ms, outcome=outcome, attrs=attrs or {})
    except Exception:
        pass


# ---------------------------------------------------------------- vault client (same config as ~/bin/vault)
class VaultErr(Exception):
    def __init__(self, msg, status=None, code=None, extra=None):
        super().__init__(msg)
        self.status, self.code, self.extra = status, code, extra or {}


def _vault_url():
    u = os.environ.get("SECUREFILL_VAULT_URL") or os.environ.get("VAULT_URL")
    if not u:
        try:
            with open(os.path.join(HOME, ".config/vault/url")) as f:
                u = f.read().strip()
        except OSError:
            u = ""
    return (u or "http://claude-vault:8300").rstrip("/")


def _vault_token_file():
    return os.environ.get("SECUREFILL_VAULT_TOKEN_FILE") or os.path.join(HOME, ".secrets/vault-token")


def _vault_token():
    with open(_vault_token_file()) as f:
        return f.read().strip()


_EGRESS = []


def _egress():
    """core.egress (~/bin/lib/core) when the token is the live default file it reads too (the vault's handle
    "vault"; local mode = the same request as before, shadow adds the hub's check, hub mode sends through the hub's
    injector), else None: a test's own token file (SECUREFILL_VAULT_TOKEN_FILE) or a copy without core keeps the
    direct path. Imported on the first vault call only: the redaction hook never loads it."""
    if not _EGRESS:
        eg = None
        if os.path.realpath(_vault_token_file()) == os.path.realpath(os.path.expanduser("~/.secrets/vault-token")):
            try:
                lib = os.path.dirname(os.path.realpath(__file__))
                if lib not in sys.path:
                    sys.path.append(lib)                   # append: ~/bin/lib never shadows anything here
                from core import egress as eg
            except Exception:                              # noqa: BLE001
                eg = None
        _EGRESS.append(eg)
    return _EGRESS[0]


def vault(method, path, body=None, timeout=45):
    """One vault call -> its JSON answer. Answers can carry his fill data: never print, log or keep them here."""
    import urllib.error                                # lazy: the redaction hook never talks to the vault
    import urllib.request
    eg = _egress()
    hub = eg is not None and eg.mode("vault") == "hub"
    tok = eg.secret("vault") if hub else _vault_token()    # hub mode: a placeholder (the hub sends the token)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(_vault_url() + path, data=data, method=method,
                                 headers={"Authorization": f"Bearer {tok}",
                                          "Content-Type": "application/json"})
    try:
        with (eg.open(req, "vault", timeout=timeout) if eg is not None
              else urllib.request.urlopen(req, timeout=timeout)) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            d = json.loads(e.read() or b"{}")
        except ValueError:
            d = {}
        d = d if isinstance(d, dict) else {}
        extra = {k: d[k] for k in ("missing", "attempts_left") if k in d}
        raise VaultErr(str(d.get("error") or d.get("message") or f"HTTP {e.code}")[:200], e.code, d.get("code"), extra)
    except (urllib.error.URLError, OSError) as e:
        if eg is not None and isinstance(e, eg.Refused):   # the hub refused it: nothing was sent
            raise VaultErr(f"vault unreachable: the hub's egress refused it ({str(e.error)[:60]})", None,
                           "unreachable")
        raise VaultErr(f"vault unreachable: {type(e).__name__}", None, "unreachable")


# ---------------------------------------------------------------- the browser half
def node(op, payload, timeout=120):
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "TZ", "SECUREFILL_PLAYWRIGHT",
                                                         "ASSISTANT_PROFILE")}   # ownermask (maskText) reads it
    p = subprocess.run(["node", NODE_HELPER], input=json.dumps(dict(payload, op=op)), capture_output=True, text=True,
                       timeout=timeout, env=env)
    lines = [ln for ln in (p.stdout or "").splitlines() if ln.startswith("{")]
    try:
        out = json.loads(lines[-1])
    except (IndexError, ValueError):
        out = {"ok": False, "error": f"browser helper failed (exit {p.returncode}, no result)"}
    out["_rc"] = p.returncode
    return out


def task_host(t):
    wa = _webagent()
    return (t.get("start_host") or wa.host_of(t.get("url")) or "").lower()


def cdp_url():
    """The CDP endpoint for fill / tick / commit: the browser service's front while its switch is on (shadow: when
    it answers; enforce: only it, else refuse), Chrome directly while off or not deployed. SECUREFILL_CDP (tests) counts
    only for a port other than the live ones (9222, 9223), so nothing routes around the service."""
    if os.environ.get("SECUREFILL_CDP") and not re.search(r":(9222|9223)(/|$)", os.environ["SECUREFILL_CDP"]):
        return os.environ["SECUREFILL_CDP"]
    for k in [k for k in os.environ if k.startswith("BROWSERSVC_")]:
        os.environ.pop(k, None)
    try:
        lib = os.path.dirname(os.path.realpath(__file__))
        if lib not in sys.path:
            sys.path.insert(0, lib)
        import browsersvc
        r = browsersvc.route()
    except Exception:
        if os.path.exists(os.path.join(HOME, ".config", "assistant", "browsersvc")):
            die("the browser service is switched on but not importable: nothing goes to Chrome around it", 1)
        return CDP
    if not r.get("ok"):
        die("the browser service is down while its switch says enforce: `browsersvc ensure` (nothing goes to Chrome "
            "around it)", 1)
    return r["cdp"]


def _base(t, tab_url=None):
    th = task_host(t)
    return {"cdp": cdp_url(), "task_host": th, "psp": psp_hosts(th), "profiles": profiles(),
            "tab_url": tab_url or None}


def die(msg, rc=1, **extra):
    print(json.dumps(dict({"ok": False, "error": msg}, **extra), ensure_ascii=False))
    raise SystemExit(rc)


def load_task(tid, states=None, vault=True):
    wa = _webagent()
    t = wa.load(tid)
    if not t:
        die(f"no web task {tid}", 2)
    eff = wa.effective(t)
    if states and eff not in states:
        hint = ""
        if eff == "stale" and hasattr(wa, "revive_until") and (wa.revive_until(t) or 0) > time.time():
            hint = f": report it; the main session can `webagent revive {tid}` while its fill session is live"
        die(f"web task {tid} is {eff} (needs {' or '.join(states)}){hint}", 4 if eff == "expired" else 6)
    if vault and t.get("mode") == "dry" and not os.environ.get("SECUREFILL_VAULT_URL"):
        die("dry runs use a test vault only (SECUREFILL_VAULT_URL); the real vault is for live tasks", 3)
    return t


# ---------------------------------------------------------------- the web task's wait for his ✅
def task_wait(tid, out, rc, deadline=None):
    """exit 10 -> the running web task waits for his ✅ until the request's deadline (decide_by; no stale timeout
    meanwhile); 0 / 4 / 5 (active, over, denied) -> the wait is over. Never raises, never prints."""
    try:
        wa = _webagent()
        if rc == 10:
            until = deadline or wa._iso((out or {}).get("decide_by"))
            wa.wait_owner(tid, until, "his ✅ in the vault bot")
        elif rc in (0, 4, 5):
            wa.wait_over(tid, (out or {}).get("session") or {0: "active", 4: "over", 5: "denied"}[rc])
    except Exception as e:                             # noqa: BLE001 - an older webagent: the task just ages
        _log({"ev": "task_wait_error", "task": tid, "err": type(e).__name__})


# ---------------------------------------------------------------- probe / fill / commit
def probe(tid, tab_url=None, ticks=None):
    t = load_task(tid)
    out = node("probe", dict(_base(t, tab_url), ticks=list(ticks or [])))
    out.pop("_rc", None)
    return out


def _req_path(tid):
    return os.path.join(_ensure("req"), f"{tid}.json")


def _load_req(tid):
    try:
        with open(_req_path(tid)) as f:
            r = json.load(f)
        return r if r.get("expires", 0) > time.time() else None
    except (OSError, ValueError):
        return None


def _save_req(tid, rec):
    p = _req_path(tid)
    fd = os.open(p + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(rec, f)
    os.replace(p + ".tmp", p)


def _drop_req(tid):
    r = None
    try:
        with open(_req_path(tid)) as f:
            r = json.load(f)
    except (OSError, ValueError):
        pass
    for p in [_req_path(tid)] + ([os.path.join(STATE, "keys", f"{r['key_id']}.key")] if r and r.get("key_id") else []):
        try:
            os.unlink(p)
        except OSError:
            pass


def _fp_key_path():
    p = os.path.join(_ensure(), "fp.key")
    if not os.path.exists(p):
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(32))
    return p


def _wanted(pr, site, card, only):
    """The vault fields this page needs (card or not, --only), and the non-main frame hosts they go to."""
    want, frames = set(), set()
    for fr in pr.get("frames") or []:
        if not fr.get("allowed"):
            continue
        n = [f for f in fr.get("needs") or [] if f.startswith("card.") == card
             and (not only or f.split(".")[0] in only or f in only)]
        want |= set(n)
        if n and not fr.get("main") and fr.get("host") != site:
            frames.add(fr["host"])
    return want, sorted(frames)


# a field the vault lacks, filled from another stored one (the title from the sex and back): released instead
DERIVE = {"name.title": ("identity.sex",), "identity.sex": ("name.title",)}
# what the agent does with a field securefill could not set (its key and result; never a value)
LEFT_HINT = {
    "no_value": "not in the vault: pick or fill it yourself (ask him if it is personal)",
    "no_option": "no option matches the stored value: pick it yourself",
    "no_list": "the picker opened no list securefill could read: pick it yourself",
    "unverified": "picked, but the page did not show it back: check it",
    "typed": "typed, but no suggestion appeared to pick: check it",
    "mismatch": "the field did not keep the value: check it (securefill fill again after a re-render)",
    "hidden": "the field was no longer visible when its turn came: nothing typed; securefill fill again once it shows",
    "timeout": "the field did not react: check it", "error": "the field did not react: check it"}


def _derive(want, have):
    """-> (want plus the stored fields that stand in for missing ones, {missing: stand-in})."""
    want, derived = set(want), {}
    for k, alts in DERIVE.items():
        if k in want and not have.get(k):
            for a in alts:
                if have.get(a):
                    want.add(a)
                    derived[k] = a
                    break
    return want, derived


def _left_for_agent(filled):
    out = []
    for k, r in sorted((filled or {}).items()):
        if r in LEFT_HINT:
            hint = LEFT_HINT[r]
            if r == "no_value" and k.split(" ")[0].split("@")[0] == "name.title":
                hint = "the vault holds neither a title nor the sex: pick Mr / Ms yourself only if he told you, else ask him"
            out.append({"field": k, "result": r, "todo": hint})
    return out


def run_fill(tid, card=False, only=None, tab_url=None, wait=200, code=None, shot=None):
    """fill (card=False) or commit (card=True). Prints one JSON result; exit 0 ok, 10 waiting for his ✅ in the vault
    bot, 2 nothing to fill / missing in the vault, 3 refused or failed, 4 expired / session over, 5 denied, 6 wrong
    task state. Uses the task's fill session; an older vault without sessions: one ✅ per fill (_legacy_fill)."""
    t0 = time.time()
    t = load_task(tid, ("approved",) if card else ("running",))
    pend = t.get("pending") or {}
    if card and not pend.get("card"):
        die("this approval was not for a card payment (webagent stop --card --amount)", 3)
    base = _base(t, tab_url)
    ticks = list(pend.get("ticks") or []) if card else []
    pr = node("probe", dict(base, ticks=ticks))
    if not pr.get("ok"):
        die(pr.get("error") or "probe failed", 3)
    if not pr.get("site_ok"):
        die(f"the tab is on {pr.get('page_host')}, not on the task's site {base['task_host']} or a payment host", 3)
    bad = [c for c in pr.get("tick_check") or [] if c.get("matches") != 1 or c.get("disabled")]
    if bad:                                            # before any vault request or approval use: fail closed
        _log({"ev": "tick_refused", "task": tid, "n": len(bad)})
        usage("exec", outcome="fail", attrs={"card": card, "ticks": len(ticks), "tick_problems": len(bad)})
        die("listed box not found or not unique: " + "; ".join(
            f"“{c['label']}” " + ("not found" if not c.get("matches") else "disabled" if c.get("disabled")
                                  else f"ambiguous ({c['matches']} boxes)") for c in bad)
            + ": nothing filled, nothing clicked (the approval is still unused)", 3, tick_problems=bad)
    site = pr["page_host"]
    want, frames = _wanted(pr, site, card, only)
    if not want:
        die("no fillable fields for this step on the page" + (" (card)" if card else ""), 2,
            blocked_frames=pr.get("card_frames_blocked") or [])
    try:
        sess, rc = ensure_session(t, wait, code)
    except NoSessions:
        return _legacy_fill(tid, t, card, only, base, pr, site, ticks, wait, code, shot, t0)
    task_wait(tid, sess, rc)                           # exit 10: the task waits for his ✅, not stale meanwhile
    if rc == 10:
        print(json.dumps(dict(sess, fields_needed=sorted(want)), ensure_ascii=False))
        raise SystemExit(10)
    if rc != 0:
        die(sess.get("error") or "no fill session", rc, **{k: v for k, v in sess.items() if k not in ("ok", "error")})
    rec = _load_sess(tid) or {}
    try:
        have = (vault("GET", "/fill/fields").get("fields") or {})
    except VaultErr as e:
        die(f"vault: {e}", 3)
    want, derived = _derive(want, have)
    missing = sorted(f for f in want if not have.get(f) and f not in derived)
    fields = sorted(f for f in want if have.get(f))
    if not fields:
        die("none of these fields are in the vault yet: he adds them with /profile in the vault bot", 2,
            missing=missing)
    frames = frames[:8]
    if card:                                           # the confirm card's approval must still hold
        tt = _webagent().load(tid)
        if not tt or _webagent().effective(tt) != "approved":
            die("the web-agent approval expired or was used meanwhile: nothing released, nothing filled", 4)
    kg = node("keygen", {"keydir": _ensure("keys")})
    if not kg.get("ok"):
        die(kg.get("error") or "keygen failed", 3)
    key_path = os.path.join(STATE, "keys", f"{kg['key_id']}.key")
    body = {"session_id": rec.get("sid"), "task": tid, "host": site, "frames": frames, "fields": fields,
            "pub": kg["pub"]}
    if card:
        body.update(card=True, amount=pend.get("amount") or "", click=pend.get("label") or "")
    try:
        res = vault("POST", "/fill/session/release", body)
    except VaultErr as e:
        _unlink(key_path)
        _log({"ev": "session_release_refused", "task": tid, "code": e.code, "card": card})
        usage("exec", outcome="fail", attrs={"card": card, "session": True, "refused": str(e.code or e.status)})
        if e.code and (e.code.startswith("session_") or e.code == "unknown_session"):
            gone = e.code[len("session_"):] if e.code.startswith("session_") else "ended"
            if gone in SESSION_GONE:
                rec["state"] = gone
                _save_sess(tid, rec)
            die(f"vault: {e}" + (f": {SESSION_GONE[gone]}" if gone in SESSION_GONE else ""), 4, code=e.code)
        die(f"vault refused the release: {e}", 2 if e.code == "fields_missing" else 3, code=e.code,
            **({"missing": e.extra["missing"]} if e.extra.get("missing") else {}))
    _log({"ev": "session_release", "task": tid, "n": len(fields), "card": card})
    if card:
        if not _consume(tid):
            _unlink(key_path)
            die("the web-agent approval expired or was used meanwhile: nothing filled", 4)
    return _type_values(tid, t, card, only, base, site, frames, ticks, pend, shot, t0, res["release_id"],
                        res["sealed"], key_path, missing)


def _unlink(p):
    try:
        os.unlink(p)
    except OSError:
        pass


def _consume(tid):
    """The card step's web-agent approval, used once (state approved -> executing)."""
    wa = _webagent()
    with wa.Lock(tid):
        tt = wa.load(tid)
        if not tt or wa.effective(tt) != "approved":
            return False
        tt["approval"]["consumed"] = {"at": round(time.time(), 1), "tool": "securefill"}
        wa._move(tt, "executing", "securefill")
        wa.save(tt)
    return True


def _type_values(tid, t, card, only, base, site, frames, ticks, pend, shot, t0, rid, sealed, key_path, missing):
    """The browser half: open the box (key used once), type, mask, fingerprints; commit: tick, click, screenshot."""
    shot_path = None
    if card or shot:
        shot_path = shot or os.path.join(_shots(), f"{tid}-{int(time.time())}.png")
    payload = dict(base, rid=rid, sealed=sealed, key_path=key_path, site=site, allow_hosts=[site] + list(frames),
                   only=only or [], card=card, fp_key_path=_fp_key_path(),
                   fp_out=os.path.join(_ensure("fp"), f"{tid}-{int(time.time())}.json"), fp_ttl=FP_TTL, task=tid,
                   fp_items=_text_items())
    if card:
        payload["click"] = {"label": pend.get("label") or "", "target": pend.get("target") or None}
        payload["shot_path"] = shot_path
        payload["ticks"] = ticks
    out = node("fill", payload, timeout=180)
    _unlink(key_path)                                  # the helper deletes it; a failed run must not leave it
    rc = out.pop("_rc", 1)
    filled = out.get("filled") or {}
    nok = sum(1 for v in filled.values() if v in ("ok", "same"))
    nt = len(out.get("ticks") or [])
    _log({"ev": "fill", "task": tid, "card": card, "ok": nok, "fail": len(filled) - nok, "clicked": out.get("clicked"),
          "ticks": nt})
    usage("exec", outcome="ok" if rc == 0 else "fail", ms=int((time.time() - t0) * 1000),
          attrs={"card": card, "fields": nok, "failed": len(filled) - nok, "clicked": bool(out.get("clicked")),
                 "ticks": nt})
    if out.get("shot"):
        leak = check_image(out["shot"]) or owner_in_image(out["shot"])
        if leak:
            os.unlink(out["shot"])
            out["shot"] = None
            out["shot_withheld"] = "the result page still showed secure-fill data or his personal data"
    out.update(missing_in_vault=missing or [])
    left = _left_for_agent(filled)
    if left:
        out["left_for_agent"] = left
    print(json.dumps(out, ensure_ascii=False, indent=1))
    raise SystemExit(0 if rc == 0 else 3)


# ---------------------------------------------------------------- fill sessions (one vault ✅ per web task)
def _sess_path(tid):
    if not re.fullmatch(r"wa[0-9a-f]{6}", str(tid or "")):
        raise ValueError("bad task id")
    return os.path.join(STATE, "sess", f"{tid}.json")


def _load_sess(tid):
    try:
        with open(_sess_path(tid)) as f:
            r = json.load(f)
        return r if isinstance(r, dict) and r.get("sid") else None
    except (OSError, ValueError):
        return None


def _save_sess(tid, rec):
    _ensure("sess")
    p = _sess_path(tid)
    fd = os.open(p + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(rec, f)
    os.replace(p + ".tmp", p)


def _drop_sess(tid):
    try:
        os.unlink(_sess_path(tid))
    except (OSError, ValueError):
        pass


def session_tasks():
    """Task ids with a local session record (for webagent sweep)."""
    return sorted(os.path.basename(p)[:-5] for p in glob.glob(os.path.join(STATE, "sess", "wa*.json")))


def _session_task(tid):
    """-> (task, None) or (None, (out, rc)): an open web task, live (or a dry run against a test vault)."""
    wa = _webagent()
    try:
        t = wa.load(tid)
    except SystemExit:
        t = None
    if not t:
        return None, ({"ok": False, "error": f"no web task {tid}"}, 2)
    eff = wa.effective(t)
    if eff not in OPEN_TASK:
        return None, ({"ok": False, "error": f"web task {tid} is {eff}: no fill session for it"},
                      4 if eff == "expired" else 6)
    if t.get("mode") == "dry" and not os.environ.get("SECUREFILL_VAULT_URL"):
        return None, ({"ok": False, "skipped": True,
                       "error": "dry runs use a test vault only (SECUREFILL_VAULT_URL): no fill session"}, 3)
    return t, None


def session_start(tid, minutes=None, wait=0, code=None, t=None):
    """Ask the vault for the task's fill session (or poll the live one). -> (out, rc): 0 active, 10 waiting for his
    ✅, 5 denied, 4 expired/ended, 3 refused, 2 no task / nothing in the vault. Raises NoSessions on an older vault."""
    if t is None:
        t, err = _session_task(tid)
        if err:
            return err
    rec = _load_sess(tid)
    if rec and rec.get("state") in LIVE:
        return session_poll(tid, rec, wait, code)
    host = task_host(t)
    site = site_of(host)
    frames = sorted({p for p in psp_hosts(host) if p})[:64]
    body = {"task": tid, "site": site, "title": t.get("title") or tid, "frames": frames,
            "minutes": int(minutes or SESSION_MIN)}
    try:
        res = vault("POST", "/fill/session/start", body, timeout=30)
    except VaultErr as e:
        if e.status == 404 and e.code == "not_found":
            raise NoSessions()
        _log({"ev": "session_refused", "task": tid, "code": e.code})
        return ({"ok": False, "error": f"vault: {e}", "code": e.code},
                2 if e.code == "no_personal_data" else 3)
    rec = {"sid": res["session_id"], "task": tid, "site": site, "frames": frames, "state": res.get("status") or "pending",
           "minutes": res.get("minutes"), "approval": res.get("approval"), "created": round(time.time(), 1)}
    _save_sess(tid, rec)
    if not res.get("existing"):
        _log({"ev": "session_request", "task": tid, "approval": rec["approval"], "minutes": rec["minutes"]})
        usage("notify_sent", attrs={"session": True, "minutes": rec["minutes"]})
    return session_poll(tid, rec, wait, code)


def session_poll(tid, rec, wait=0, code=None):
    """The live state of the task's session, waiting up to `wait` s (<= 230) while he decides. -> (out, rc)."""
    end = time.time() + max(0, min(int(wait or 0), 230))
    while True:
        left = end - time.time()
        body = {"session_id": rec["sid"], "task": tid, "wait": max(0, min(25, int(left)))}
        if code:
            body["code"] = str(code)
        try:
            r = vault("POST", "/fill/session/poll", body, timeout=60)
        except VaultErr as e:
            if e.status == 404 and e.code == "not_found":
                _drop_sess(tid)
                raise NoSessions()
            if e.code in ("unknown_session", "wrong_task"):
                _drop_sess(tid)
                return {"ok": False, "session": "gone", "error": f"vault: {e}: `securefill session start {tid}` "
                                                                  "asks him again"}, 4
            if e.code == "request_dead":
                rec["state"] = "dead"
                _save_sess(tid, rec)
                return {"ok": False, "session": "dead", "error": SESSION_GONE["dead"]}, 4
            if e.code == "wrong_code":
                return {"ok": False, "error": f"vault: {e}", "attempts_left": e.extra.get("attempts_left")}, 3
            return {"ok": False, "error": f"vault: {e}", "code": e.code}, 3
        code = None                                    # a code is used once
        st = r.get("status")
        rec.update(state=st, until=r.get("until"))
        _save_sess(tid, rec)
        if st == "active":
            return {"ok": True, "session": "active", "site": rec.get("site"), "until": r.get("until"),
                    "seconds_left": r.get("seconds_left"), "releases": r.get("releases") or 0,
                    "released": r.get("released") or {}, "card_uses": r.get("card_uses") or 0}, 0
        if st == "pending":
            if left > 1:
                continue
            return {"ok": False, "waiting": True, "session": "pending", "approval": r.get("approval"),
                    "site": rec.get("site"), "decide_by": r.get("decide_by"),
                    "next": "he taps ✅ Allow in the vault bot (once for this task" + (
                        "; or gives you the 6-digit code: --code" if r.get("approval") == "code" else "")
                    + "); then run the same command again"}, 10
        return {"ok": False, "session": st, "end_why": r.get("end_why"),
                "error": SESSION_GONE.get(st, f"the fill session is {st}")}, 5 if st == "denied" else 4


def ensure_session(t, wait=0, code=None):
    """fill/commit: the task's live session; none yet -> ask now (lazily). A denied/expired/ended one is not asked
    again by itself. -> (out, rc) like session_start; raises NoSessions on an older vault."""
    tid = t["id"]
    rec = _load_sess(tid)
    if rec is None:
        return session_start(tid, wait=wait, code=code, t=t)
    if rec.get("state") in LIVE:
        return session_poll(tid, rec, wait, code)
    st = rec.get("state")
    return {"ok": False, "session": st, "error": SESSION_GONE.get(st, f"the fill session is {st}")}, \
        5 if st == "denied" else 4


def session_end(tid, why="done", timeout=15):
    """End the task's session (the vault bot sends him the report). -> (out, rc); no session = ok."""
    try:
        rec = _load_sess(tid)
    except ValueError:
        rec = None
    if not rec:
        return {"ok": True, "session": None}, 0
    try:
        r = vault("POST", "/fill/session/end", {"session_id": rec["sid"], "task": tid, "why": why}, timeout=timeout)
    except VaultErr as e:
        if e.status == 404 or e.code in ("unknown_session", "wrong_task"):
            _drop_sess(tid)
            return {"ok": True, "session": "gone"}, 0
        _log({"ev": "session_end_failed", "task": tid, "code": e.code})
        return {"ok": False, "error": f"vault: {e}", "code": e.code}, 3   # kept: webagent sweep retries
    _drop_sess(tid)
    _log({"ev": "session_end", "task": tid, "why": why, "state": r.get("status"), "releases": r.get("releases"),
          "card_uses": r.get("card_uses")})
    usage("run", outcome="ok", attrs={"session_end": why, "releases": r.get("releases") or 0,
                                      "card": bool(r.get("card_uses"))})
    return {"ok": True, "session": r.get("status"), "end_why": r.get("end_why"), "released": r.get("released") or {},
            "card_uses": r.get("card_uses") or 0}, 0


def session_status(tid):
    t, err = _session_task(tid)
    rec = _load_sess(tid)
    if not rec:
        return ({"ok": True, "session": None} if not err else err[0]), (0 if not err else err[1])
    if rec.get("state") not in LIVE:
        return {"ok": True, "session": rec.get("state")}, 0
    try:
        return session_poll(tid, rec, 0)
    except NoSessions:
        return {"ok": True, "session": None, "vault": "no fill sessions (older vault)"}, 0


def for_webagent(op, tid, **kw):
    """webagent's hooks (start / stop --card / done / abort / sweep): never raises, never prints. -> a small dict
    for its output (no session id)."""
    try:
        if op == "tasks":
            return session_tasks()
        if op == "state":                              # the vault's view for a live one (a tap or the limit may
            rec = _load_sess(tid)                      # have changed it), the local one otherwise
            if rec and rec.get("state") in LIVE:
                out, rc = session_poll(tid, rec, 0)
                return out.get("session") or rec.get("state")
            return rec.get("state") if rec else None
        if op == "start":
            out, rc = session_start(tid, minutes=kw.get("minutes"), wait=0)
            if out.get("skipped"):
                return {"state": "skipped", "note": out.get("error")}
            st = out.get("session") or ("error" if rc not in (0, 10) else "pending")
            note = {"pending": "his ✅ in the vault bot allows filling his details on this site (once for the task)",
                    "active": "fill session live"}.get(st) or out.get("error")
            return {"state": st, "note": note}
        if op == "end":
            out, rc = session_end(tid, why=kw.get("why") or "done")
            return {"state": out.get("session") if rc == 0 else "error", "note": out.get("error")}
    except NoSessions:
        if op == "state":
            return "unsupported"
        return {"state": "unsupported", "note": "this vault has no fill sessions yet: each fill asks him in the vault "
                                                "bot"}
    except Exception as e:                             # noqa: BLE001 - the web task goes on without it
        _log({"ev": "webagent_hook_error", "op": op, "err": type(e).__name__})
        return {"state": "error", "note": f"fill session {op} failed ({type(e).__name__})"}
    return None


def _legacy_fill(tid, t, card, only, base, pr, site, ticks, wait, code, shot, t0):
    """An older vault without sessions: one ✅ per fill (POST /fill/request naming site + fields, card: amount +
    button), POST /fill/release once after his ✅."""
    wa = _webagent()
    pend = t.get("pending") or {}
    req = _load_req(tid)
    if req and (req.get("site") != site or bool(req.get("card")) != card):
        _drop_req(tid)
        req = None
    if not req:
        want = set()
        frames = []
        for fr in pr.get("frames") or []:
            if not fr.get("allowed"):
                continue
            n = [f for f in fr.get("needs") or [] if f.startswith("card.") == card
                 and (not only or f.split(".")[0] in only or f in only)]
            want |= set(n)
            if n and not fr.get("main") and fr.get("host") != site:
                frames.append(fr["host"])
        if not want:
            die("no fillable fields for this step on the page" + (" (card)" if card else ""), 2,
                blocked_frames=pr.get("card_frames_blocked") or [])
        try:
            have = (vault("GET", "/fill/fields").get("fields") or {})
        except VaultErr as e:
            die(f"vault: {e}", 3)
        want, derived = _derive(want, have)
        missing = sorted(f for f in want if not have.get(f) and f not in derived)
        fields = sorted(f for f in want if have.get(f))
        if not fields:
            die("none of these fields are in the vault yet: he adds them with /profile in the vault bot", 2,
                missing=missing)
        kg = node("keygen", {"keydir": _ensure("keys")})
        if not kg.get("ok"):
            die(kg.get("error") or "keygen failed", 3)
        body = {"fields": fields, "site": site, "frames": sorted(set(frames)), "task": tid, "pub": kg["pub"],
                "reason": f"web task {tid}: {t.get('title') or ''}"[:160]}
        if card:
            body.update(amount=pend.get("amount") or "", click=pend.get("label") or "")
        try:
            res = vault("POST", "/fill/request", body)
        except VaultErr as e:
            os.unlink(os.path.join(STATE, "keys", f"{kg['key_id']}.key"))
            die(f"vault refused the request: {e}", 3, code=e.code)
        req = {"rid": res["request_id"], "key_id": kg["key_id"], "site": site, "frames": body["frames"],
               "fields": fields, "missing": missing, "card": card, "approval": res.get("approval"),
               "created": time.time(), "expires": time.time() + REQ_TTL}
        _save_req(tid, req)
        _log({"ev": "request", "task": tid, "n": len(fields), "card": card})
        usage("notify_sent", attrs={"card": card, "n": len(fields)})
    end = time.time() + max(0, min(int(wait), 230))
    rel = None
    while True:
        left = end - time.time()
        try:
            r = vault("POST", "/fill/release", {"request_id": req["rid"], "wait": max(0, min(25, int(left))),
                                                 **({"code": code} if code else {})}, timeout=60)
        except VaultErr as e:
            _drop_req(tid)
            _log({"ev": "release_refused", "task": tid, "code": e.code})
            outcome = {"request_denied": 5, "request_expired": 4}.get(e.code or "", 3)
            task_wait(tid, {"session": "refused"}, outcome)
            usage("tap", outcome="stop" if outcome == 5 else "error", attrs={"card": card})
            die(f"vault: {e}", outcome, code=e.code)
        if r.get("status") == "released":
            rel = r
            task_wait(tid, {"session": "released"}, 0)
            break
        if left <= 1:
            task_wait(tid, r, 10, deadline=_webagent()._iso(r.get("expires_at")) or req.get("expires"))
            print(json.dumps({"ok": False, "waiting": True, "request_id": req["rid"], "fields": req["fields"],
                              "approval": r.get("approval") or req.get("approval"), "expires_at": r.get("expires_at"),
                              "next": "he taps ✅ in the vault bot (or gives you the 6-digit code: --code); then run "
                                      "the same command again"}, ensure_ascii=False))
            raise SystemExit(10)
    if card:
        with wa.Lock(tid):
            tt = wa.load(tid)
            if not tt or wa.effective(tt) != "approved":
                _drop_req(tid)
                die("the web-agent approval expired or was used meanwhile: nothing filled", 4)
            tt["approval"]["consumed"] = {"at": round(time.time(), 1), "tool": "securefill"}
            wa._move(tt, "executing", "securefill")
            wa.save(tt)
    shot_path = None
    if card or shot:
        d = _shots()
        shot_path = shot or os.path.join(d, f"{tid}-{int(time.time())}.png")
    payload = dict(base, rid=req["rid"], sealed=rel["sealed"], key_path=os.path.join(STATE, "keys", f"{req['key_id']}.key"),
                   site=site, allow_hosts=[site] + list(req.get("frames") or []), only=only or [], card=card,
                   fp_key_path=_fp_key_path(), fp_out=os.path.join(_ensure("fp"), f"{tid}-{int(time.time())}.json"),
                   fp_ttl=FP_TTL, task=tid, fp_items=_text_items())
    if card:
        payload["click"] = {"label": pend.get("label") or "", "target": pend.get("target") or None}
        payload["shot_path"] = shot_path
        payload["ticks"] = ticks
    out = node("fill", payload, timeout=180)
    _drop_req(tid)
    rc = out.pop("_rc", 1)
    filled = out.get("filled") or {}
    nok = sum(1 for v in filled.values() if v in ("ok", "same"))
    nt = len(out.get("ticks") or [])
    _log({"ev": "fill", "task": tid, "card": card, "ok": nok, "fail": len(filled) - nok, "clicked": out.get("clicked"),
          "ticks": nt})
    usage("exec", outcome="ok" if rc == 0 else "fail", ms=int((time.time() - t0) * 1000),
          attrs={"card": card, "fields": nok, "failed": len(filled) - nok, "clicked": bool(out.get("clicked")),
                 "ticks": nt})
    if out.get("shot"):
        leak = check_image(out["shot"]) or owner_in_image(out["shot"])
        if leak:
            os.unlink(out["shot"])
            out["shot"] = None
            out["shot_withheld"] = "the result page still showed secure-fill data or his personal data"
    out.update(missing_in_vault=req.get("missing") or [])
    left = _left_for_agent(filled)
    if left:
        out["left_for_agent"] = left
    print(json.dumps(out, ensure_ascii=False, indent=1))
    raise SystemExit(0 if rc == 0 else 3)


def tick(tid, tab_url=None):
    """An approved step's listed T&C boxes (webagent stop --tick), for the non-card path: exactly those, nothing else.
    Doesn't use the approval (the agent's approved click does). Exit 0 ok, 3 a box missing/ambiguous/refused, 6 state."""
    wa = _webagent()
    t = load_task(tid, ("approved",), vault=False)
    pend = t.get("pending") or {}
    ticks = list(pend.get("ticks") or [])
    if not ticks:
        die("this approval lists no boxes to tick (webagent stop --tick \"<exact label>\")", 2)
    host = wa.host_of(pend.get("url") or t.get("url"))
    if t.get("mode") == "dry" and host not in wa.DRY_HOSTS and host not in TEST_HOSTS:
        die(f"dry run on {host}: nothing is ticked", 3)
    t0 = time.time()
    out = node("tick", dict(_base(t, tab_url), ticks=ticks))
    rc = out.pop("_rc", 1)
    n = sum(1 for r in out.get("ticks") or [] if r.get("result") == "ticked")
    _log({"ev": "tick", "task": tid, "listed": len(ticks), "ticked": n, "ok": bool(out.get("ok"))})
    usage("exec", outcome="ok" if out.get("ok") else "fail", ms=int((time.time() - t0) * 1000),
          attrs={"card": False, "ticks": len(ticks), "ticked": n})
    return out, (0 if out.get("ok") and rc == 0 else 3)


def _text_mask(t, tab_url):
    """The payload part that lets the browser half black out page text showing a released value (hashes only)."""
    items = _text_items()
    return dict(_base(t, tab_url), fp_items=items, fp_key_path=_fp_key_path() if items else "")


def mask(tid, tab_url=None):
    t = load_task(tid)
    out = node("mask", _text_mask(t, tab_url))
    out.pop("_rc", None)
    return out


def shot(tid, tab_url=None, path=None):
    t = load_task(tid)
    path = path or os.path.join(_shots(), f"{tid}-{int(time.time())}.png")
    out = node("shot", dict(_text_mask(t, tab_url), path=path))
    out.pop("_rc", None)
    if out.get("ok") and check_image(path):
        os.unlink(path)
        return {"ok": False, "error": "the page still shows secure-fill data (text, not a field): no screenshot"}
    if out.get("ok") and owner_in_image(path):
        os.unlink(path)
        return {"ok": False, "error": "the page still shows his personal data (in an image or canvas): no screenshot"}
    return out


def owner_in_image(path):
    """True when his profile values (name, street, postcode, e-mail; ownermask) are readable in the image, or the
    check can't run. The page text was blacked out before the shot (maskText); this catches text in images."""
    try:
        import ownermask
        m = ownermask.Masker()
        if not m.fps:
            return False                               # no values to look for: patterns masked the page text
        t = ocr_text(path)
        if t is None:
            return True
        return bool(m.value_hits(t) or m.value_hits(t.replace(" ", "")))
    except Exception:
        return True


# ---------------------------------------------------------------- card migration (~/.secrets -> vault, once)
def _parse_card_file(raw):
    vals = {}
    try:
        d = json.loads(raw)
        if isinstance(d, dict):
            for k, v in d.items():
                kk = re.sub(r"[^a-z]", "", str(k).lower())
                if kk in ("number", "cardnumber", "pan", "card", "num"):
                    vals["card.number"] = _digits(v)
                elif kk in ("cvc", "cvv", "csc", "securitycode", "cvc2", "cvv2"):
                    vals["card.cvc"] = _digits(v)
                elif kk in ("holder", "name", "cardholder", "nameoncard", "holdername"):
                    vals["card.holder"] = str(v).strip()
                elif kk in ("expiry", "exp", "expires", "expiration", "validuntil", "valid", "expirydate"):
                    m = re.search(r"(\d{1,2})\s*[/.\-]\s*(\d{2,4})", str(v))
                    if m:
                        vals["card.exp_month"], vals["card.exp_year"] = m.group(1), m.group(2)
                elif kk in ("expmonth", "month", "expirymonth"):
                    vals["card.exp_month"] = str(v)
                elif kk in ("expyear", "year", "expiryyear"):
                    vals["card.exp_year"] = str(v)
            return vals
    except ValueError:
        pass
    for line in raw.splitlines():
        k, _, v = line.partition(":") if ":" in line else line.partition("=")
        kl = k.strip().lower()
        if v and re.search(r"cvc|cvv|security", kl):
            vals["card.cvc"] = _digits(v)
        elif v and re.search(r"expir|valid|exp\b|gültig", kl):
            m = re.search(r"(\d{1,2})\s*[/.\-]\s*(\d{2,4})", v)
            if m:
                vals["card.exp_month"], vals["card.exp_year"] = m.group(1), m.group(2)
        elif v and re.search(r"holder|name|inhaber", kl):
            vals["card.holder"] = v.strip()
        elif v and re.search(r"number|card|pan|nummer", kl) and len(_digits(v)) >= 12:
            vals["card.number"] = _digits(v)
    if "card.number" not in vals:                      # free text: the longest Luhn-valid 12..19 digit run
        for m in re.finditer(r"(?<!\d)(?:\d[ \-]?){11,18}\d(?!\d)", raw):
            d = _digits(m.group())
            if _luhn(d):
                vals["card.number"] = d
                break
    if "card.exp_month" not in vals:
        m = re.search(r"(?<!\d)(0?[1-9]|1[0-2])\s*/\s*(\d{2}|20\d{2})(?!\d)", raw)
        if m:
            vals["card.exp_month"], vals["card.exp_year"] = m.group(1), m.group(2)
    return vals


def _luhn(d):
    total, alt = 0, False
    for ch in reversed(d):
        n = int(ch)
        if alt:
            n = n * 2 - 9 if n > 4 else n * 2
        total, alt = total + n, not alt
    return len(d) >= 12 and total % 10 == 0


def migrate_card(dry=False):
    src = [p for p in (os.path.join(HOME, ".secrets", n) for n in ("payment-card.json", "payment-card.txt"))
           if os.path.exists(p)]
    if not src:
        return {"ok": False, "error": "no ~/.secrets/payment-card.json or .txt"}
    with open(src[0]) as f:
        vals = _parse_card_file(f.read())
    found = sorted(vals)
    if dry:
        return {"ok": True, "dry": True, "source": os.path.basename(src[0]), "recognized": found,
                "not_recognized": sorted({"card.number", "card.exp_month", "card.exp_year", "card.cvc",
                                          "card.holder"} - set(found))}
    try:
        res = vault("POST", "/fill/import", {"values": vals})
    except VaultErr as e:
        return {"ok": False, "error": f"vault: {e}"}
    vals = None
    _log({"ev": "migrate_card", "stored": len(res.get("stored") or [])})
    return {"ok": True, "source": os.path.basename(src[0]), **res,
            "next": "check with `securefill fields`; the local file stays until he says to delete it"}


# ---------------------------------------------------------------- fingerprints, redaction, OCR
def _fp_key():
    try:
        with open(os.path.join(STATE, "fp.key")) as f:
            return bytes.fromhex(f.read().strip())
    except (OSError, ValueError):
        return None


def _live_files(task=None):
    """The live fingerprint records (all tasks, or one task's); expired files are deleted."""
    now = time.time()
    for p in glob.glob(os.path.join(STATE, "fp", "*.json")):
        try:
            with open(p) as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(d, dict):
            continue
        if d.get("expires", 0) < now:
            try:
                os.unlink(p)
            except OSError:
                pass
            continue
        if task and d.get("task") != task:
            continue
        yield d


def live(task=None):
    """{kind: {hash: n}} of the live fingerprints, plus "seq": [((strong, {hashes}), ...)] (a text value's word tokens
    in order) and "_seqidx": {hash: [(seq, pos)]}."""
    out, seqs = {}, set()
    for d in _live_files(task):
        for it in d.get("items") or []:
            if not isinstance(it, dict):
                continue
            if it.get("kind") == "seq":
                p = it.get("p") or []
                if len(p) >= 2:
                    seqs.add(tuple((bool(x.get("s")), frozenset(x.get("h") or ())) for x in p))
            elif it.get("kind") and it.get("h"):
                out.setdefault(it["kind"], {})[it["h"]] = it.get("n")
    if seqs:
        out["seq"] = sorted(seqs, key=len)
        idx = {}
        for si, sq in enumerate(out["seq"]):
            for pos, (_, hs) in enumerate(sq):
                for h in hs:
                    idx.setdefault(h, []).append((si, pos))
        out["_seqidx"] = idx
    return out


def _text_items(task=None):
    """The live text-value items (tok / seq / email; hashes only) for the browser half's page-text mask."""
    return [it for d in _live_files(task) for it in d.get("items") or []
            if isinstance(it, dict) and it.get("kind") in ("tok", "seq", "email")]


MONTHS = {}
for i, names in enumerate((("january", "januar", "janvier", "gennaio", "jan", "jän", "janv", "genn", "gen"),
                           ("february", "februar", "février", "fevrier", "febbraio", "feb", "févr", "fevr", "febr"),
                           ("march", "märz", "maerz", "mars", "marzo", "mar", "mär", "mrz"),
                           ("april", "avril", "aprile", "apr", "avr"),
                           ("may", "mai", "maggio", "mag"),
                           ("june", "juni", "juin", "giugno", "jun", "giu"),
                           ("july", "juli", "juillet", "luglio", "jul", "juil", "lug"),
                           ("august", "août", "aout", "agosto", "aug", "ago"),
                           ("september", "septembre", "settembre", "sep", "sept", "set"),
                           ("october", "oktober", "octobre", "ottobre", "oct", "okt", "ott"),
                           ("november", "novembre", "nov"),
                           ("december", "dezember", "décembre", "decembre", "dicembre", "dec", "dez", "déc", "dic")), 1):
    for n in names:
        MONTHS[n] = i
DIGIT_RUN = re.compile(r"(?<![\d])(?:\d[ \-./]?){5,24}\d(?![\d])")
ALNUM = re.compile(r"(?<![A-Za-z0-9])(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{5,20}(?![A-Za-z0-9])")
D_ISO = re.compile(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)")
D_NUM = re.compile(r"(?<!\d)(\d{1,2})\s?[./\-]\s?(\d{1,2})\s?[./\-]\s?(\d{4}|\d{2})(?!\d)")
D_TXT1 = re.compile(r"(?<!\d)(\d{1,2})\.?\s+([A-Za-zÀ-ÿ]{3,10})\.?,?\s+(\d{4})(?!\d)")
D_TXT2 = re.compile(r"\b([A-Za-zÀ-ÿ]{3,10})\.?\s+(\d{1,2}),?\s+(\d{4})(?!\d)")
CVC_LINE = re.compile(r"cvc|cvv|csc|security|sicherheit|prüf|cryptogram|codice", re.I)
CVC_TOK = re.compile(r"(?<!\d)\d{3,4}(?!\d)")
# text values (names, e-mail, street, town): word tokens normalized like securefill-cdp.mjs tnorm() (lowercased,
# transliterated, accents stripped; a second variant with ä ö ü as ae oe ue). A word may carry %XX, \uXXXX or HTML
# escapes (URLs, JSON, network bodies): it is decoded and its words count, the whole escaped run is redacted.
WORD = re.compile(r"[^\W_]+(?:[\u0300-\u036f]+[^\W_]*)*")
RUN = re.compile(r"(?:%[0-9A-Fa-f]{2}|\\u[0-9A-Fa-f]{4}|&#x?[0-9A-Fa-f]{1,6};|&[A-Za-z]{2,8};|[^\W_]|[\u0300-\u036f])+")
ESCAPED = re.compile(r"%[0-9A-Fa-f]{2}|\\u[0-9A-Fa-f]{4}|&#?[0-9A-Za-z]{2,8};")
EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
TR1 = str.maketrans({"ß": "ss", "đ": "d", "ø": "o", "æ": "ae", "ł": "l", "œ": "oe", "þ": "th", "ı": "i"})
TR2 = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "đ": "dj"})
TITLE_TAIL = re.compile(r"(mrs|miss|mstr|mr|ms)$")


def _unmark(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.category(c).startswith("M"))


def _tnorm(w):
    l = w.lower()
    a = _unmark(l.translate(TR1))
    if any(c in l for c in "äöüđ"):
        b = _unmark(l.translate(TR2).translate(TR1))
        return (a, b) if b != a else (a,)
    return (a,)


def _decode(s):
    import html
    import urllib.parse
    if "\\u" in s:
        s = re.sub(r"\\u([0-9A-Fa-f]{4})", lambda m: chr(int(m.group(1), 16)), s)
    if "&" in s:
        s = html.unescape(s)
    if "%" in s:
        s = urllib.parse.unquote(s, errors="replace")
    return s


def _tokens(text):
    """[(start, end, word)]; a word decoded from an escaped run spans the whole run."""
    if not ESCAPED.search(text):
        return [(m.start(), m.end(), m.group()) for m in WORD.finditer(text)]
    out = []
    for m in RUN.finditer(text):
        r = m.group()
        if ESCAPED.search(r):
            out += [(m.start(), m.end(), w.group()) for w in WORD.finditer(_decode(r))]
        else:
            out.append((m.start(), m.end(), r))
    return out


def _text_hits(text, fps, key, cache=None):
    """Spans of released text values: a strong token on its own, a run of a value's tokens (with a strong one) next to
    each other, a whole e-mail address."""
    spans = []
    emails = fps.get("email") or {}
    if emails and "@" in text:
        for m in EMAIL.finditer(text):
            if _h(key, "email", m.group().lower()) in emails:
                spans.append(m.span())
    toks, idx = fps.get("tok") or {}, fps.get("_seqidx") or {}
    if not toks and not idx:
        return spans
    cache = {} if cache is None else cache
    tl = _tokens(text)
    if emails and "%40" in text.upper() or emails and "\\u0040" in text:
        dec = _decode(text)
        if any(_h(key, "email", m.group().lower()) in emails for m in EMAIL.finditer(dec)):
            for i, (a, b, w) in enumerate(tl):         # an escaped address: its run(s)
                if a < b and ESCAPED.search(text[a:b]) and "@" in _decode(text[a:b]):
                    spans.append((a, b))
    hv = []
    for a, b, w in tl:
        hs = cache.get(w)
        if hs is None:
            n = _tnorm(w)
            hs = {_h(key, "tok", v) for v in n}
            m = TITLE_TAIL.search(n[0]) if len(n[0]) >= 5 else None
            if m:                                      # DOE/JANEMS: a title glued to the name
                hs.add(_h(key, "tok", n[0][:-len(m.group(1))]))
            hs = frozenset(hs)
            cache[w] = hs
        hv.append(hs)
        if toks and not hs.isdisjoint(toks):
            spans.append((a, b))
    if idx:
        seqs = fps["seq"]

        def near(j):
            if tl[j + 1][0] <= tl[j][1]:               # words of one escaped run
                return True
            gap = text[tl[j][1]:tl[j + 1][0]]
            return len(gap) <= 3 and "\n" not in gap
        for i, hs in enumerate(hv):
            for h in hs:
                for si, pos in idx.get(h, ()):
                    sq = seqs[si]
                    j, p, strong = i, pos, sq[pos][0]
                    while p + 1 < len(sq) and j + 1 < len(tl) and near(j) and not hv[j + 1].isdisjoint(sq[p + 1][1]):
                        j, p = j + 1, p + 1
                        strong = strong or sq[p][0]
                    if j > i and strong:
                        spans.append((tl[i][0], tl[j][1]))
    return spans


def _h(key, kind, s):
    return hmac.new(key, f"{kind}:{s}".encode(), hashlib.sha256).hexdigest()[:32]


def _iso(y, m, d):
    try:
        y, m, d = int(y), int(m), int(d)
        if y < 100:
            y += 1900 if y > 40 else 2000
        import datetime
        return datetime.date(y, m, d).isoformat()
    except (ValueError, TypeError):
        return None


def find_hits(text, fps, key, cache=None):
    """Spans (start, end) in text that carry a live fingerprinted value."""
    spans = _text_hits(text, fps, key, cache) if (fps.get("tok") or fps.get("seq") or fps.get("email")) else []
    digit_sets = fps.get("digits") or {}
    lens = {n for n in digit_sets.values() if n}
    if digit_sets:
        for m in DIGIT_RUN.finditer(text):
            d = _digits(m.group())
            hit = _h(key, "digits", d) in digit_sets or any(
                _h(key, "digits", d[i:i + n]) in digit_sets for n in lens if n < len(d) for i in range(len(d) - n + 1))
            if hit:
                spans.append(m.span())
    if fps.get("alnum"):
        for m in ALNUM.finditer(text):
            if _h(key, "alnum", m.group().upper()) in fps["alnum"]:
                spans.append(m.span())
    if fps.get("date"):
        ds = fps["date"]
        for m in D_ISO.finditer(text):
            if _h(key, "date", _iso(*m.groups()) or "") in ds:
                spans.append(m.span())
        for m in D_NUM.finditer(text):
            a, b, y = m.groups()
            if any(_h(key, "date", x or "") in ds for x in (_iso(y, b, a), _iso(y, a, b))):
                spans.append(m.span())
        for rx, order in ((D_TXT1, "dmy"), (D_TXT2, "mdy")):
            for m in rx.finditer(text):
                g = m.groups()
                d, mon, y = (g[0], g[1], g[2]) if order == "dmy" else (g[1], g[0], g[2])
                mi = MONTHS.get(mon.lower().rstrip("."))
                if mi and _h(key, "date", _iso(y, mi, d) or "") in ds:
                    spans.append(m.span())
    if fps.get("cvc"):
        for line in re.finditer(r"[^\n]+", text):
            if CVC_LINE.search(line.group()):
                for m in CVC_TOK.finditer(line.group()):
                    if _h(key, "cvc", m.group()) in fps["cvc"]:
                        spans.append((line.start() + m.start(), line.start() + m.end()))
    return spans


def redact_text(text, fps, key, cache=None):
    spans = sorted(find_hits(text, fps, key, cache))
    if not spans:
        return text, 0
    merged = []                                        # overlapping spans (a name inside a seq) -> one
    for a, b in spans:
        if merged and a < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    out, last = [], 0
    for a, b in merged:
        out += [text[last:a], "[securefill]"]
        last = b
    out.append(text[last:])
    return "".join(out), len(merged)


def ocr_text(path):
    try:
        p = subprocess.run(["tesseract", path, "stdout", "--psm", "11", "-l", "eng"], capture_output=True, text=True,
                           timeout=25)
        return p.stdout or ""
    except (OSError, subprocess.TimeoutExpired):
        return None


def check_image(path, task=None):
    """True when a live secure-fill value is readable in the image (or OCR failed while values are live)."""
    fps = live(task)
    key = _fp_key()
    if not fps:
        return False
    if not key:                                        # live values, no key to look for them: treat as readable
        return True
    t = ocr_text(path)
    if t is None:
        return True
    return bool(find_hits(t, fps, key) or find_hits(t.replace(" ", ""), fps, key))


PLACEHOLDER_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNoaGgAAAMEAYFL09IQAAAAAElFTkSuQmCC")


def _overwrite_blobs(ev, img_bytes, new=None):
    """Claude Code's tool-results blob of an image (saved before hooks run) -> new (a masked copy) or a placeholder."""
    n = 0
    tp = str(ev.get("transcript_path") or "")
    sid = str(ev.get("session_id") or "")
    roots = [os.path.join(os.path.dirname(tp), sid)] if tp and sid else []
    now = time.time()
    for root in roots:
        for p in glob.glob(os.path.join(root, "**", "*blob*"), recursive=True):
            try:
                if now - os.path.getmtime(p) > 300:
                    continue
                with open(p, "rb") as f:
                    if f.read() != img_bytes:
                        continue
                with open(p, "wb") as f:
                    f.write(new or PLACEHOLDER_PNG)
                n += 1
            except OSError:
                continue
    return n


def _overwrite_copies(ev, img_bytes, texts):
    """The withheld image's copies on disk: Claude Code's tool-results blob (saved before hooks run) and the MCP's
    own screenshot file named in the output text."""
    n = _overwrite_blobs(ev, img_bytes)
    for t in texts:
        for m in re.finditer(r"(/[^\s)\]'\"]+\.(?:png|jpe?g))", t):
            p = os.path.realpath(m.group(1))
            if p.startswith(os.path.realpath(MCP_OUT) + os.sep) and os.path.isfile(p):
                try:
                    with open(p, "wb") as f:
                        f.write(PLACEHOLDER_PNG)
                    n += 1
                except OSError:
                    pass
    return n


WITHHELD = "[personal-data mask: output withheld (the redaction failed, so none of it is shown)]"
TEXT_TOOLS = ("Bash", "Read", "Grep")
OUT_KEYS = ("stdout", "stderr", "content")
# a Bash / Read / Grep call on browser output: its command or path names the MCP output, a page snapshot, securefill or
# abx (agent-browser; its screenshots sit under share/browser/ab-output), or its output is a snapshot (refs, page sections)
BROWSER_REF = re.compile(r"mcp-output|\.playwright-mcp|securefill|/browser/|page-\d{4}-\d\d-\d\dT[\d-]+Z|snapshot"
                         r"|\babx\b|ab-output|agent-browser", re.I)
SNAP_MARK = re.compile(r"\[ref=[a-z]*\d+\]|^\s*- Page (?:URL|Title):|^### (?:Page|Snapshot)\b|- \[Snapshot\]\(", re.M)
REF_FILE = re.compile(r"(/[^\s)\]'\"`<>|]+\.(?:ya?ml|md|log|txt|json|html?))")
# his personal data (ownermask), always, fill or not: every browser MCP result, and a Bash / Read / Grep call on browser
# output: the MCP / abx output dirs, the browser's data dir, `browser screenshot` display grabs (~/reports/browser),
# Claude Code's saved copies of browser results (tool-results/mcp-browser-*), a page snapshot file, or snapshot-shaped
# output (SNAP_MARK)
OWNER_REF = re.compile(r"mcp-output|\.playwright-mcp|ab-output|/share/browser/|/reports/browser/|mcp-browser-"
                       r"|page-\d{4}-\d\d-\d\dT[\d-]+Z", re.I)
IMG_FILE = re.compile(r"(/[^\s)\]'\"`<>|]+\.(?:png|jpe?g|webp))", re.I)
OM_BUDGET = 28.0                                       # seconds of OCR per hook call (the wrapper stops it at 40)
OM_IMG_MASKED = "[personal: his personal data in this screenshot is blacked out (his town stays readable)]"
OM_IMG_WITHHELD = ("[personal: screenshot withheld: it shows his personal data and could not be masked. In a web task "
                   "use `securefill shot <task>` (masked); else try a viewport or element screenshot.]")
SF_IMG_WITHHELD = ("[securefill: screenshot withheld: it showed personal data from a secure fill. Use `securefill shot "
                   "<task>` (masked, checked).]")


def _session_live():
    now = time.time()
    for p in glob.glob(os.path.join(STATE, "sess", "wa*.json")):
        try:
            with open(p) as f:
                r = json.load(f)
        except (OSError, ValueError):
            continue
        u = r.get("until") if isinstance(r, dict) else None
        if isinstance(r, dict) and r.get("state") in LIVE and not (isinstance(u, (int, float)) and u < now):
            return True
    return False


def _om():
    """ownermask with its masker built: an import, profile or matcher error raises here (the caller fails closed)."""
    if LIB not in sys.path:
        sys.path.insert(0, LIB)
    import ownermask
    ownermask.masker()
    return ownermask


def _replace_bytes(path, data):
    st = os.stat(path)
    tmp = f"{path}.om{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, st.st_mode & 0o777)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _redact_file(path, fps, key, cache, max_bytes=64 << 20, om=None):
    """A text file with live fill values (fps) or his personal data (om) redacted in place (atomic, same mode);
    unreadable or too big -> withheld."""
    try:
        st = os.stat(path)
    except OSError:
        return 0
    try:
        if st.st_size > max_bytes:
            raise ValueError("too big")
        with open(path, encoding="utf-8", errors="surrogateescape") as f:
            new = f.read()
        n = 0
        if fps:
            new, k = redact_text(new, fps, key, cache)
            n += k
        if om is not None:
            new, k = om.masker().mask(new)
            n += k
        if not n:
            return 0
    except (OSError, ValueError, UnicodeError):
        new, n = "[personal-data mask: file withheld (it could not be checked)]\n", 1
    tmp = f"{path}.sf{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, st.st_mode & 0o777)
    with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape") as f:
        f.write(new)
    os.replace(tmp, path)
    return n


def _no_touch(p):
    dirs = [os.path.join(HOME, d) for d in (".secrets", ".claude", "bin", ".ssh")] + [os.path.realpath(STATE)]
    return any(p == d or p.startswith(d + os.sep) for d in dirs)


def _named(texts, ev, rx):
    """Files a browser tool just wrote and named in its output: under the MCP output dir, or under HOME with the file
    name the agent asked for; written < 10 min ago."""
    root = os.path.realpath(MCP_OUT) + os.sep
    fn = str((ev.get("tool_input") or {}).get("filename") or "")
    out, now = set(), time.time()
    for t in texts:
        for m in rx.finditer(t):
            p = os.path.realpath(m.group(1))
            ok = p.startswith(root) or bool(fn and os.path.basename(p) == os.path.basename(fn)
                                            and p.startswith(HOME + os.sep) and not _no_touch(p))
            try:
                if ok and os.path.isfile(p) and now - os.path.getmtime(p) < 600:
                    out.add(p)
            except OSError:
                continue
    return sorted(out)


def _ref_files(texts, ev):
    """Text files a browser tool just wrote and named (the page snapshot .yml, console .log, a saved snapshot)."""
    return _named(texts, ev, REF_FILE)


def _out_texts(resp):
    texts = []

    def collect(o, k=None):
        if isinstance(o, str):
            if k in OUT_KEYS:
                texts.append(o)
        elif isinstance(o, dict):
            for kk, v in o.items():
                collect(v, kk)
        elif isinstance(o, list):
            for v in o:
                collect(v, k)
    collect(resp if isinstance(resp, dict) else {"stdout": resp})
    return texts


def _in_scope(ev, resp):
    """A Bash / Read / Grep call whose output loses live fill values: on browser files or snapshots, or a subagent's call
    while a fill session is live."""
    ti = ev.get("tool_input") or {}
    probe = " ".join(str(ti.get(k) or "") for k in ("command", "file_path", "path", "pattern", "glob"))
    if BROWSER_REF.search(probe) or os.path.realpath(MCP_OUT) in probe or MCP_OUT in probe:
        return True
    if any(SNAP_MARK.search(t) for t in _out_texts(resp)):
        return True
    return bool(ev.get("agent_id")) and _session_live()


def _owner_scope(ev, resp):
    """A Bash / Read / Grep call whose output loses his personal data (ownermask), with or without a fill: on browser
    output (OWNER_REF in its command / path, or snapshot-shaped output)."""
    ti = ev.get("tool_input") or {}
    probe = " ".join(str(ti.get(k) or "") for k in ("command", "file_path", "path", "pattern", "glob"))
    if OWNER_REF.search(probe) or os.path.realpath(MCP_OUT) in probe or MCP_OUT in probe:
        return True
    return any(SNAP_MARK.search(t) for t in _out_texts(resp))


def _fill_leak(raw):
    """True when a live secure-fill value is readable in the image bytes (OCR; check_image)."""
    fd, tmp = tempfile.mkstemp(prefix="sf-ocr-", suffix=".png", dir=_ensure("tmp"))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        return check_image(tmp)
    finally:
        os.unlink(tmp)


def _read_image(ev, resp, sf_on, om, end):
    """Read of an image: withheld while it shows a live fill value; on browser output (owner scope) his personal data
    blacked out of the image the model gets (the file on disk stays as it is), withheld when it can't be masked."""
    p = str((ev.get("tool_input") or {}).get("file_path") or "")
    agent = bool(ev.get("agent_id"))
    if sf_on and p and os.path.isfile(p) and check_image(p):
        _log({"ev": "redacted", "tool": "Read", "text": 0, "img": 1, "files": 0, "agent": agent})
        usage("redacted", attrs={"img": 1, "tool": "read"})
        return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": _read_text(p, IMG_WITHHELD)}}
    if om is None:
        return None
    f = resp.get("file") if isinstance(resp.get("file"), dict) else {}
    try:
        raw = base64.b64decode(f.get("base64") or "")
    except ValueError:
        raw = b""
    r = om.mask_image(raw, budget=max(0.0, end - time.monotonic())) if raw else {"status": "withhold"}
    if r["status"] == "clean":
        return None
    _log({"ev": "personal", "tool": "Read", "img_masked": int(r["status"] == "masked"),
          "img_withheld": int(r["status"] != "masked"), "agent": agent})
    usage("personal", attrs={"img": 1, "tool": "read", "masked": r["status"] == "masked"})
    if r["status"] == "masked":
        nf = dict(f, base64=base64.b64encode(r["data"]).decode(), type=r["mime"])
        return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": dict(resp, file=nf)}}
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": _read_text(p, OM_IMG_WITHHELD)}}


def hook(ev):
    """PostToolUse -> None (unchanged) or the hook output.
    mcp__browser__* (always): his personal data out of every text (ownermask: [personal]) and every screenshot (OCR
    word boxes blacked out, his town stays; withheld when that fails), the snapshot / console files and screenshots it
    names rewritten the same way in place; while fill fingerprints are live also every released value ([securefill];
    a screenshot showing one is withheld) -> updatedMCPToolOutput.
    Bash / Read / Grep: on browser output (_owner_scope) his personal data, and while fills are live on browser files
    or snapshots (_in_scope) the released values: stdout / stderr / content, a persisted output file, a Read image
    -> updatedToolOutput (same shape)."""
    tool = str(ev.get("tool_name") or "")
    browser = tool.startswith("mcp__browser__")
    if not browser and tool not in TEXT_TOOLS:
        return None
    resp = ev.get("tool_response")
    fps = live()
    sf_on = bool(fps) and (browser or _in_scope(ev, resp))
    om = _om() if browser or _owner_scope(ev, resp) else None
    if not sf_on and om is None:
        return None
    key = None
    if sf_on:
        key = _fp_key()
        if not key:                                    # live values but no key to find them: withhold (hook_main)
            raise RuntimeError("fingerprint key unreadable")
    end = time.monotonic() + OM_BUDGET
    if tool == "Read" and isinstance(resp, dict) and resp.get("type") == "image":
        return _read_image(ev, resp, sf_on, om, end)
    stats = {"text": 0, "img": 0, "files": 0, "personal": 0, "img_masked": 0, "img_withheld": 0}
    cache, texts, notes, seen = {}, [], [], {}

    def collect(o):                                    # the texts that may name files (not image data: the file-name
        if isinstance(o, str):                         # regexes would crawl through megabytes of base64)
            texts.append(o)
        elif isinstance(o, list):
            for x in o:
                collect(x)
        elif isinstance(o, dict) and o.get("type") != "image":
            for v in o.values():
                collect(v)
    collect(resp)

    def om_image(raw):                                 # one OCR per distinct image (inline shot = its saved file)
        h = hashlib.sha256(raw).digest()
        if h not in seen:
            seen[h] = om.mask_image(raw, budget=max(0.0, end - time.monotonic())) if raw else {"status": "withhold"}
        return seen[h]

    def image(o):
        src = o.get("source") if isinstance(o.get("source"), dict) else None
        data = (src or {}).get("data") or o.get("data")
        try:
            raw = base64.b64decode(data or "")
        except ValueError:
            raw = b""
        if sf_on and _fill_leak(raw):
            stats["img"] += 1
            _overwrite_copies(ev, raw, texts)
            return {"type": "text", "text": SF_IMG_WITHHELD}
        r = om_image(raw)
        if r["status"] == "clean":
            return o
        if r["status"] == "masked":
            stats["img_masked"] += 1
            _overwrite_blobs(ev, raw, r["data"])
            b64 = base64.b64encode(r["data"]).decode()
            n = dict(o)
            if src is not None:
                n["source"] = dict(src, data=b64, media_type=r["mime"])
            else:
                n["data"] = b64
                for mk in ("mimeType", "media_type"):
                    if mk in n:
                        n[mk] = r["mime"]
            notes.append(OM_IMG_MASKED)
            return n
        stats["img_withheld"] += 1
        _overwrite_blobs(ev, raw, None)
        return {"type": "text", "text": OM_IMG_WITHHELD}

    def walk(o, k=None):
        if isinstance(o, str):
            if not browser and k not in OUT_KEYS:
                return o
            if sf_on:
                o, n = redact_text(o, fps, key, cache)
                stats["text"] += n
            if om is not None:
                o, n = om.masker().mask(o)
                stats["personal"] += n
            return o
        if isinstance(o, list):
            return [walk(x, k) for x in o]
        if isinstance(o, dict):
            if browser and o.get("type") == "image":
                return image(o)
            return {kk: walk(v, kk) for kk, v in o.items()}
        return o
    new = walk(resp)
    files = _ref_files(texts, ev) if browser else []
    if not browser and isinstance(resp, dict) and isinstance(resp.get("persistedOutputPath"), str):
        files.append(resp["persistedOutputPath"])      # Bash's full output when too big to show inline
    for p in files:
        stats["files"] += 1 if _redact_file(p, fps if sf_on else None, key, cache, om=om) else 0
    if browser and om is not None and not stats["img"]:   # screenshots it saved (with or without a filename)
        for p in _named(texts, ev, IMG_FILE):
            try:
                with open(p, "rb") as f:
                    raw = f.read(64 << 20)
            except OSError:
                continue
            r = om_image(raw)
            if r["status"] == "clean":
                continue
            stats["files"] += 1
            if r["status"] == "masked":
                _replace_bytes(p, r["data"])
                stats["img_masked"] += 1
                notes.append(f"[personal: the saved screenshot {os.path.basename(p)} has his personal data blacked out "
                             "(his town stays readable)]")
            else:
                _replace_bytes(p, PLACEHOLDER_PNG)
                stats["img_withheld"] += 1
                notes.append(f"[personal: the saved screenshot {os.path.basename(p)} was replaced by a blank image: it "
                             "shows his personal data and could not be masked. In a web task use `securefill shot "
                             "<task>` (masked).]")
    if not any(stats.values()):
        return None
    agent = bool(ev.get("agent_id"))
    name = tool[len("mcp__browser__"):] if browser else tool
    if sf_on and (stats["text"] or stats["img"] or stats["files"]):
        _log({"ev": "redacted", "tool": name, "text": stats["text"], "img": stats["img"], "files": stats["files"],
              "agent": agent})
        usage("redacted", attrs={"text": stats["text"], "img": stats["img"], "files": stats["files"],
                                 "tool": "browser" if browser else tool.lower()})
    if om is not None and (stats["personal"] or stats["img_masked"] or stats["img_withheld"] or stats["files"]):
        _log({"ev": "personal", "tool": name, "text": stats["personal"], "img_masked": stats["img_masked"],
              "img_withheld": stats["img_withheld"], "files": stats["files"], "agent": agent})
        usage("personal", attrs={"text": stats["personal"], "img_masked": stats["img_masked"],
                                 "img_withheld": stats["img_withheld"], "files": stats["files"],
                                 "tool": "browser" if browser else tool.lower()})
    if notes:
        notes = list(dict.fromkeys(notes))
        if isinstance(new, list):
            new = new + [{"type": "text", "text": n} for n in notes]
        elif isinstance(new, str):
            new = "\n".join([new] + notes)
    elif not (stats["text"] or stats["img"] or stats["personal"] or stats["img_withheld"]):
        return None                                    # only a file was cleaned: the output itself is fine
    field = "updatedMCPToolOutput" if browser else "updatedToolOutput"
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", field: new}}


IMG_WITHHELD = ("[securefill: image withheld: it shows personal data from a secure fill. Use `securefill shot <task>` "
                "(masked, checked).]")


def _read_text(path, msg):
    """A Read result of the text kind (the shape Claude Code expects) carrying only msg."""
    return {"type": "text", "file": {"filePath": path, "content": msg, "numLines": 1, "startLine": 1, "totalLines": 1}}


def _withheld(ev):
    """The fail-closed answer: the output replaced (same shape for Bash / Read / Grep)."""
    tool = str(ev.get("tool_name") or "")
    if tool.startswith("mcp__browser__"):
        return {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                       "updatedMCPToolOutput": [{"type": "text", "text": WITHHELD}]}}
    resp = ev.get("tool_response")
    if tool == "Read" and isinstance(resp, dict) and resp.get("type") != "text":
        return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": _read_text(
            str((ev.get("tool_input") or {}).get("file_path") or ""), WITHHELD)}}

    def walk(o, k=None):
        if isinstance(o, str):
            return (WITHHELD if o else "") if k in OUT_KEYS else o
        if isinstance(o, dict):
            return {kk: walk(v, kk) for kk, v in o.items()}
        if isinstance(o, list):
            return [walk(v, k) for v in o]
        return o
    new = walk(resp) if isinstance(resp, dict) else WITHHELD
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": new}}


def hook_main():
    """Fails closed: an error while masking withholds the output of every browser tool, and of a Bash / Read / Grep call
    in scope (browser output, or browser files / snapshots while fills are live). Out of scope: unchanged."""
    try:
        ev = json.load(sys.stdin)
    except ValueError:
        return 0
    if not isinstance(ev, dict):
        return 0
    try:
        r = hook(ev)
    except Exception as e:
        tool = str(ev.get("tool_name") or "")
        if not tool.startswith("mcp__browser__"):
            if tool not in TEXT_TOOLS:
                return 0
            try:
                resp = ev.get("tool_response")
                scoped = _owner_scope(ev, resp) or (bool(glob.glob(os.path.join(STATE, "fp", "*.json")))
                                                    and _in_scope(ev, resp))
            except Exception:
                scoped = True
            if not scoped:
                return 0
        _log({"ev": "hook_error", "err": type(e).__name__, "tool": tool[:40]})
        r = _withheld(ev)
    if r:
        print(json.dumps(r, ensure_ascii=False))
    return 0


def status():
    reqs = []
    for p in glob.glob(os.path.join(STATE, "req", "*.json")):
        try:
            with open(p) as f:
                r = json.load(f)
            reqs.append({"task": os.path.basename(p)[:-5], "fields": len(r.get("fields") or []), "card": r.get("card"),
                         "site": r.get("site"), "expires_in_s": int(r.get("expires", 0) - time.time())})
        except (OSError, ValueError):
            continue
    sess = []
    for tid in session_tasks():
        r = _load_sess(tid)
        if r:
            sess.append({"task": tid, "site": r.get("site"), "state": r.get("state"), "until": r.get("until")})
    fps = live()
    return {"sessions": sess, "pending_requests": reqs, "live_fingerprints": sum(len(v) for v in fps.values()),
            "psp_hosts": len(psp_hosts()), "site_profiles": sorted(profiles())}


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "hook":
    sys.modules.setdefault("securefill", sys.modules[__name__])   # ownermask imports this module: one copy, not two
    raise SystemExit(hook_main())
