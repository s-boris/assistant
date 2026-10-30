#!/usr/bin/env python3
"""webagent: checkpoints, the commit-action classifier and the approval gate for web tasks in my headed Chrome.

A web task = one browser subagent working in its own tab. It stops before any committing action (pay, order, book,
delete, cancel a contract, send, submit, confirm, save settings, unsubscribe, sign up) and waits for the owner's
approval on Telegram; only then it performs exactly that one action and verifies the result.

State: STATE/<id>.json per task (STATE = $WEBAGENT_STATE or ~/.local/state/webagent; dir 0700, files 0600), screenshots
in STATE/shots/<id>/. Free text goes through redact() (card numbers, IBANs, CVCs, key=value secrets, tokens, URL query
secrets); typed form values are never accepted, only field names.

States: running -> pending (stop: approval card ready; expires after 15 min) -> approved (his tap; valid 10 min) ->
executing (the guard let the one approved action through) -> done | failed. Other exits: aborted (his stop, or an
error), expired (pending or approved past its time), stale (running or executing with no update for 45 min).
`done --continue` loops back to running for a further step.

Waiting for his vault ✅: when `securefill fill` / `session start` exits 10 (the task's fill session waits for his ✅ in
the vault bot), securefill marks the running task as waiting (wait_owner: `waiting` = since + until, the request's
deadline, at most 60 min ahead). The stale clock pauses until then and restarts from there; his decision (session
active, denied, expired) ends the wait (wait_over, also from `sweep`). Only a running task waits; a card's 15 min and
an approval's 10 min never stretch. `revive` brings a stale task back to running within 2 h of going stale when it was
running (not executing) and its fill session is still live (pending or active): logged in its history and as a usage
run; the next committing step still needs its own card and his ✅. Sweep keeps a stale task's live fill session for that
window (no fill can use it while the task is stale; it ends at its own limit).

Guard (PreToolUse hook ~/.claude/hooks/webagent-guard.sh -> guard_hook()): a click or Enter-submit whose label reads
as a HARD commit (pay, order, book, delete, cancel a contract) is denied for every caller unless an approved task's
pending action matches it; SOFT commits (send, submit, confirm, save, unsubscribe, sign up, remove), Enter, JS clicks
and coordinate clicks are denied while a task is running or approved. The guard fails open; it is a second layer
behind the protocol in ~/.claude/skills/web-agent/SKILL.md.

Approvals: each stop makes a one-time approval (~/bin/lib/approvals: 128-bit nonce bound to the sha256 of the
stop's canonical action, same 15-min expiry); the card's buttons are ap:<nonce>:y|n. approve consumes it (once, same
action, before the expiry), whether tg-adapter decided his tap or it sits in the telegram-plus inbound journal. The
older proofs still count while the nonce is undecided: wa:ok:<id> taps, 👍 on the card, his typed yes, another flow's
yes button. Without the approvals library (or its store) the card falls back to wa:ok/wa:no buttons. Live runs use
the library's default store, never a connection of their own: the hub while hub approvals are on (hub-approvals, or
hub-tg: the hub's Telegram adapter decides his tap there), else the local one.

Hub workflow mirror (switch WF_SWITCH = ~/.config/assistant/wf-webtask, absent = off = no hub
call at all): each stop also starts the hub's durable `webtask.approval` workflow (DBOS, id webtask:<id>:<stop>, the
stop's nonce and 15-min window), which survives hub and brain restarts and reports an unanswered card's expiry as a
ledger event; approve / abort / a replacing stop send it the decision (approved / stopped / replaced). Advisory only:
this file's state machine stays the authority, and every hub call fails open (one line in STATE/wf.log: op, workflow
id, outcome code; never text).

Whole task as a workflow (switch WF_RUN_SWITCH = ~/.config/assistant/wf-webtask-run, absent = off = no
hub call): `start` also starts the hub's `webtask.run` workflow (id webtask:<id>, t["wf_run"]) and every move sends it
one message (wf_task: lease from `abx start --task`, step, wait/wait over, stop, decision approved, commit from the
browser service, done, abort, revive; ids, counts and states only). It holds the task's last step boundary across hub
restarts and writes the receipt row at the end. Advisory and fail-open like the mirror: the approval flow (one card, his
✅, this file's states and webagent-guard) is unchanged.
"""
import datetime as dt
import fcntl
import glob
import json
import os
import re
import secrets
import shutil
import statistics
import subprocess
import sys
import time
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

HOME = os.path.expanduser("~")


def _p(env, *rel):
    return os.environ.get(env) or os.path.join(HOME, *rel)


STATE = _p("WEBAGENT_STATE", ".local/state/webagent")
JOURNAL = _p("WEBAGENT_JOURNAL", ".claude/channels/telegram/inbound-journal.jsonl")
ACCESS = _p("WEBAGENT_ACCESS", ".claude/channels/telegram/access.json")
BROWSER = _p("WEBAGENT_BROWSER_BIN", "bin/browser")
USAGE_LIB = _p("WEBAGENT_USAGE_LIB", "bin/lib")
PENDING_TTL = int(os.environ.get("WEBAGENT_PENDING_TTL") or 900)
APPROVED_TTL = int(os.environ.get("WEBAGENT_APPROVED_TTL") or 600)
RUNNING_TTL = int(os.environ.get("WEBAGENT_RUNNING_TTL") or 2700)
WAIT_DEFAULT = 900                          # the vault's fill-session request lifetime, when its deadline is unknown
WAIT_MAX = int(os.environ.get("WEBAGENT_WAIT_MAX") or 3600)              # the longest pause of the stale clock
REVIVE_WINDOW = int(os.environ.get("WEBAGENT_REVIVE_WINDOW") or 7200)    # revive: at most this long after going stale
RETRY_WINDOW = int(os.environ.get("WEBAGENT_RETRY_WINDOW") or 86400)     # same title + site within this = one job
WF_SWITCH = _p("WEBAGENT_WF_SWITCH", ".config/assistant/wf-webtask")    # hub workflow mirror (absent = off)
WF_RUN_SWITCH = _p("WEBAGENT_WF_RUN_SWITCH", ".config/assistant/wf-webtask-run")  # the whole task as webtask.run (absent = off)
WF_TIMEOUT = 5.0
SESSION_LIVE = ("pending", "active")        # securefill's live fill-session states
KEEP_DAYS, SHOT_DAYS = 90, 7
DRY_HOSTS = {h.strip() for h in (os.environ.get("WEBAGENT_DRY_HOSTS") or "httpbin.org,www.httpbin.org").split(",")
             if h.strip()}
FEATURE = "skill.web-agent"
ACTIVE = ("running", "approved", "executing")
OPEN = ("running", "pending", "approved", "executing")
KINDS = ("form", "cancel", "booking", "order", "order-check", "account", "unsubscribe", "other")
SUMMARY_KINDS = ("booking", "order")       # soft steps run without a stop; one final card with a summary
AUTO_SOFT = ("send", "confirm", "settings")  # the soft categories such a running task clicks itself
TICK_MAX = 300
CATEGORY_TITLE = {"pay": "Ready to order / pay", "book": "Ready to book", "delete": "Ready to delete",
                  "cancel": "Ready to cancel", "send": "Ready to send", "confirm": "Ready to confirm",
                  "unsubscribe": "Ready to unsubscribe", "settings": "Ready to save changes",
                  "signup": "Ready to sign up", "remove": "Ready to remove", "other": "Ready for the last step"}


# ---------------------------------------------------------------- redaction (nothing secret is ever stored)
def _luhn(d):
    s, alt = 0, False
    for ch in reversed(d):
        n = int(ch)
        if alt:
            n = n * 2 - 9 if n > 4 else n * 2
        s, alt = s + n, not alt
    return s % 10 == 0


CARD_RE = re.compile(r"(?<!\d)(?:\d[ \-]?){12,18}\d(?!\d)")
IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,4})?\b")
CVC_RE = re.compile(r"(?i)\b(cvc2?|cvv2?|cvn|csc|security code|prüfnummer|kartenprüfnummer)(\W{0,3})\d{3,4}\b")
KV_RE = re.compile(r"(?i)\b(password|passwort|passwd|pwd|kennwort|pin|otp|tan|token|api[_-]?key|secret|cvc|cvv)"
                   r"(\s*[:=]\s*)(\S+)")
JWT_RE = re.compile(r"\beyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}")
TOKEN_RE = re.compile(r"(?<![\w/.-])(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{32,}(?![\w-])")
SECRET_Q = re.compile(r"(?i)(token|key|secret|session|^sid$|auth|pass|pwd|sig|^code$|otp|ticket|nonce|^state$|mail"
                      r"|iban|card|^cc|cvc|cvv|phone|tel)")


def redact(s, limit=300):
    """Mask card numbers (Luhn), IBANs, CVCs, key=value secrets, JWTs and long tokens; drop control chars."""
    if s is None:
        return None
    s = unicodedata.normalize("NFC", str(s))
    s = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", s)
    s = JWT_RE.sub("[token]", s)
    s = KV_RE.sub(lambda m: m.group(1) + m.group(2) + "[secret]", s)
    s = CVC_RE.sub(lambda m: m.group(1) + m.group(2) + "[cvc]", s)
    s = CARD_RE.sub(lambda m: "[card]" if 13 <= len(re.sub(r"\D", "", m.group(0))) <= 19
                    and _luhn(re.sub(r"\D", "", m.group(0))) else m.group(0), s)
    s = IBAN_RE.sub("[iban]", s)
    s = TOKEN_RE.sub("[token]", s)
    return s[:limit]


def clean_url(u):
    """URL without userinfo and fragment; secret-looking query values replaced by x; then redact()."""
    if not u:
        return None
    try:
        p = urlsplit(str(u).strip())
    except ValueError:
        return redact(u, 300)
    if p.scheme not in ("http", "https", "about", "file"):
        return redact(u, 300)
    host = p.hostname or ""
    if p.port:
        host += f":{p.port}"
    q = urlencode([(k, "x" if SECRET_Q.search(k) else redact(v, 200))
                   for k, v in parse_qsl(p.query, keep_blank_values=True)])
    return urlunsplit((p.scheme, host, redact(p.path, 300), q, ""))[:500]


def host_of(u):
    try:
        return (urlsplit(u or "").hostname or "").lower()
    except ValueError:
        return ""


# ---------------------------------------------------------------- the commit-action classifier (DE / EN / FR / IT)
def _rx(words):
    return re.compile(r"(?<!\w)(?:" + "|".join(words) + r")(?!\w)")


HARD = {
    "pay": [r"zahlungspflichtig(?:\s+\w+)?", r"kostenpflichtig(?:\s+\w+)?", r"(?:jetzt\s+|sofort\s+)?(?:be)?zahlen",
            r"(?:jetzt\s+|sofort[\s-]*)?kaufen", r"kauf\s+abschliessen", r"(?:jetzt\s+|nach|vor)?bestellen",
            r"bestellung\s+(?:abschicken|absenden|abschliessen|aufgeben|bestätigen)",
            r"(?:zahlung|überweisung|auftrag)\s+(?:ausführen|freigeben|bestätigen)", r"überweisen",
            r"pay(?:\s+now)?", r"buy(?:\s+now|\s+again)?", r"purchase(?!\s+(?:history|details|overview))",
            r"place\s+(?:your\s+|my\s+|the\s+)?order", r"order\s+now", r"submit\s+(?:your\s+|my\s+|the\s+)?order",
            r"complete\s+(?:your\s+|my\s+|the\s+)?(?:purchase|order|payment|checkout)",
            r"confirm\s+(?:and\s+pay|your\s+order|order|purchase|payment)", r"checkout\s+(?:and|&)\s+pay",
            r"start\s+(?:my\s+|your\s+)?(?:free\s+)?trial", r"(?:send|transfer)\s+money",
            r"commander", r"acheter", r"payer", r"(?:valider|confirmer|passer)\s+(?:la\s+|ma\s+|votre\s+)?commande",
            r"ordina(?:re)?(?!\s+per)", r"acquista(?:re)?", r"compra(?:re)?", r"paga(?:re)?",
            r"conferma\s+(?:l'ordine|ordine|l'acquisto|acquisto|il\s+pagamento|pagamento|e\s+paga)"],
    "book": [r"(?:jetzt\s+|verbindlich\s+)?buchen", r"buchung\s+(?:abschliessen|bestätigen|absenden)",
             r"(?:verbindlich\s+|jetzt\s+)?reservieren", r"book(?:\s+now)?", r"reserve(?:\s+now)?",
             r"(?:confirm|complete)\s+(?:the\s+|your\s+|my\s+)?(?:booking|reservation)",
             r"réserver", r"confirmer\s+(?:la\s+)?réservation", r"prenota(?:re)?",
             r"conferma\s+(?:la\s+)?prenotazione"],
    "delete": [r"löschen", r"delete", r"erase", r"deactivate",
               r"(?:konto|account|profil|profile)\s+(?:schliessen|deaktivieren|auflösen)",
               r"close\s+(?:my\s+|your\s+|the\s+)?account", r"supprimer", r"effacer", r"elimina(?:re)?"],
    "cancel": [r"kündigen", r"stornieren", r"widerrufen",
               r"(?:abo|abonnement|vertrag|mitgliedschaft|bestellung|buchung|auftrag)\s+(?:beenden|auflösen)",
               r"cancel\s+(?:my\s+|your\s+|the\s+|this\s+)?(?:subscription|membership|plan|contract|order|booking"
               r"|reservation|account)", r"terminate", r"end\s+(?:my\s+|your\s+)?(?:subscription|membership|contract)",
               r"résilier", r"annuler\s+(?:l'|mon\s+|ma\s+|la\s+|votre\s+)?(?:abonnement|commande|réservation"
               r"|contrat)", r"disdici", r"disdetta",
               r"annulla\s+(?:l'|il\s+|la\s+)?(?:abbonamento|ordine|prenotazione|contratto)"],
}
SOFT = {
    "send": [r"senden", r"absenden", r"abschicken", r"einreichen", r"übermitteln", r"posten", r"veröffentlichen",
             r"(?:inserat|anzeige)\s+(?:aufgeben|schalten)", r"antworten", r"submit", r"send", r"publish", r"reply",
             r"share", r"post\s+(?:comment|reply|review|it|now|message)", r"envoyer", r"soumettre", r"publier",
             r"répondre", r"invia(?:re)?", r"pubblica(?:re)?", r"rispondi"],
    "confirm": [r"bestätigen", r"zustimmen", r"ich\s+stimme\s+zu", r"akzeptieren", r"annehmen", r"einverstanden",
                r"freigeben", r"verbindlich", r"confirm", r"i\s+agree", r"agree", r"accept", r"approve",
                r"authori[sz]e", r"confirmer", r"valider", r"accepter", r"j'accepte", r"conferma(?:re)?",
                r"accetta(?:re)?", r"accetto", r"autorizza"],
    "unsubscribe": [r"abmelden", r"abbestellen", r"austragen", r"unsubscribe", r"opt[\s-]?out",
                    r"(?:se\s+)?désabonner", r"(?:se\s+)?désinscrire", r"désinscription", r"disiscriviti",
                    r"annulla\s+(?:l')?iscrizione"],
    "settings": [r"speichern", r"übernehmen", r"save(?!\s*(?:\d|chf|€|\$|up\s+to))", r"apply\s+changes",
                 r"update\s+(?:my\s+|your\s+)?(?:address|password|email|e-mail|settings|profile|details|payment)",
                 r"change\s+(?:my\s+|your\s+)?(?:password|email|e-mail|address|settings)", r"enregistrer",
                 r"sauvegarder", r"salva(?:re)?"],
    "signup": [r"registrieren", r"konto\s+(?:erstellen|eröffnen|anlegen)", r"abonnieren", r"sign\s+up", r"register",
               r"create\s+(?:an\s+|my\s+|your\s+)?account", r"subscribe", r"join(?:\s+now)?", r"s'inscrire",
               r"s'abonner", r"créer\s+(?:un|mon)\s+compte", r"registrati", r"iscriviti", r"abbonati"],
    "remove": [r"entfernen", r"remove", r"retirer", r"rimuovi", r"cancella(?:re)?", r"discard", r"verwerfen"],
}
HARD_RX = {k: _rx(v) for k, v in HARD.items()}
SOFT_RX = {k: _rx(v) for k, v in SOFT.items()}
CONSENT_RX = _rx([r"cookies?", r"consent", r"datenschutz-?einstellungen", r"einwilligung", r"tracking",
                  r"privacy\s+(?:settings|preferences|choices)"])
SEARCH_RX = _rx([r"such(?:e|en|feld|begriff)?", r"search", r"recherche(?:r)?", r"cerca(?:re)?", r"filter(?:n)?",
                 r"filtres?", r"filtri", r"sort(?:ieren)?", r"query", r"eingabe", r"auswahl"])
CHECKBOX_RX = re.compile(r"check\s?-?box|kontrollk[aä]stchen|h[aä]kchen|casella di (?:spunta|controllo)|case [aà] cocher")
REF_RX = re.compile(r"^(?:[a-z]?\d+|f\d+e\d+|e\d+)$", re.I)


def norm(s):
    s = unicodedata.normalize("NFKC", str(s or "")).lower()
    s = s.replace("ß", "ss").replace("­", "").replace("’", "'").replace("`", "'")
    s = s.replace("e-book", "ebook")
    return re.sub(r"\s+", " ", s).strip()


def classify(text):
    """{'commit', 'tier': hard|soft|None, 'category', 'matched': [...], 'exempt': None|consent|search}."""
    t = norm(text)
    out = {"commit": False, "tier": None, "category": None, "matched": [], "exempt": None}
    if not t:
        return out
    hard = [(c, m.group(0)) for c, rx in HARD_RX.items() for m in rx.finditer(t)]
    soft = [(c, m.group(0)) for c, rx in SOFT_RX.items() for m in rx.finditer(t)]
    if CONSENT_RX.search(t) and (hard or soft):
        out["exempt"] = "consent"
        return out
    if SEARCH_RX.search(t):                       # "Submit search", "Filter löschen", "Auswahl übernehmen"
        hard = [(c, w) for c, w in hard if c != "delete"]
        if (hard or soft) and not hard:
            out["exempt"] = "search"
            return out
        soft = []
    found = hard or soft
    if not found:
        return out
    out.update(commit=True, tier="hard" if hard else "soft", category=found[0][0],
               matched=sorted({w for _, w in hard + soft}))
    return out


def label_text(ti):
    """What a browser tool call is about: its element description plus a selector-style target (not a bare ref)."""
    el = str(ti.get("element") or "")
    tg = str(ti.get("target") or ti.get("ref") or "")
    return (el + " " + ("" if REF_RX.match(tg.strip()) else tg)).strip()


# ---------------------------------------------------------------- state files
def now():
    return time.time()


def _ensure():
    os.makedirs(STATE, mode=0o700, exist_ok=True)


def path_of(tid):
    if not re.fullmatch(r"wa[0-9a-f]{6}", tid or ""):
        raise SystemExit(f"webagent: bad task id {tid!r}")
    return os.path.join(STATE, f"{tid}.json")


class Lock:
    def __init__(self, tid):
        _ensure()
        self.f = open(os.path.join(STATE, f".{tid}.lock"), "a")

    def __enter__(self):
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def load(tid):
    try:
        with open(path_of(tid)) as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def save(t, touch=True):
    _ensure()
    if touch:
        t["updated"] = now()
    p = path_of(t["id"])
    tmp = f"{p}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(t, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def all_tasks():
    out = []
    for p in sorted(glob.glob(os.path.join(STATE, "wa*.json"))):
        try:
            with open(p) as f:
                out.append(json.load(f))
        except (OSError, ValueError):
            continue
    return out


def _idle_from(t):
    """Where the stale clock starts: the last update, or the end of his vault ✅ wait when that is later."""
    base = t.get("updated") or 0
    w = t.get("waiting") or {}
    if t.get("state") == "running" and w.get("until"):
        base = max(base, w["until"])
    return base


def effective(t, at=None):
    """The state after timeouts, without writing: pending/approved -> expired, running/executing -> stale (a running
    task waiting for his vault ✅ only after that wait's deadline + RUNNING_TTL)."""
    at = at or now()
    s = t.get("state")
    if s == "pending" and at > (t.get("expires") or 0):
        return "expired"
    if s == "approved" and at > ((t.get("approval") or {}).get("until") or 0):
        return "expired"
    if s in ("running", "executing") and at - _idle_from(t) > RUNNING_TTL:
        return "stale"
    return s


def _move(t, to, why=""):
    t.setdefault("history", []).append([round(now(), 1), t.get("state"), to, why])
    t["state"] = to
    t.pop("waiting", None)                           # any move ends a wait for his vault ✅


def stale_since(t):
    """When the task went stale (None while it is not)."""
    if effective(t) != "stale":
        return None
    if t.get("state") != "stale":                    # not persisted yet
        return _idle_from(t) + RUNNING_TTL
    if t.get("stale_at"):
        return t["stale_at"]
    for h in reversed(t.get("history") or []):       # older records: when the timeout was written
        if len(h) > 2 and h[2] == "stale":
            return h[0]
    return t.get("updated")


def _stale_from(t):
    """The state a stale task went stale from (running or executing)."""
    if t.get("state") != "stale":                    # not persisted yet: the state it still holds
        return t.get("state")
    for h in reversed(t.get("history") or []):
        if len(h) > 2 and h[2] == "stale":
            return h[1]
    return t.get("state")


def revive_until(t):
    """Until when `revive` may bring a stale task back (it went stale while running), else None."""
    since = stale_since(t)
    if since is None or _stale_from(t) != "running":
        return None
    return since + REVIVE_WINDOW


def _apply_timeouts(t):
    eff = effective(t)
    if eff != t.get("state"):
        was = t.get("state")
        if eff == "stale":
            t["stale_at"] = round(_idle_from(t) + RUNNING_TTL, 1)
        _move(t, eff, "timeout")
        if eff == "expired":
            _usage("ignored" if was == "pending" else "abandoned", t, outcome=was)
        else:
            _usage("abandoned", t, outcome=was)
        return True
    return False


def get(tid, *states):
    """Load under the caller's lock, apply timeouts (saved), check the state; exit 6 on a wrong state."""
    t = load(tid)
    if not t:
        die(f"no task {tid}", 2)
    if _apply_timeouts(t):
        save(t)
    if states and t["state"] not in states:
        hint = ""
        if t["state"] == "stale" and (revive_until(t) or 0) > now():
            hint = f"; with its fill session still live the main session can `webagent revive {tid}` (until " \
                   f"{fmt_local(revive_until(t))})"
        die(f"task {tid} is {t['state']} (needs {' or '.join(states)}){hint}", 4 if t["state"] == "expired" else 6)
    return t


def die(msg, code=1):
    print(f"webagent: {msg}", file=sys.stderr)
    raise SystemExit(code)


def fmt_local(ts):
    try:
        sys.path.insert(0, USAGE_LIB)
        import tripclock                                    # his zone (trip-aware); the profile zone outside trips
        return tripclock.local(dt.datetime.fromtimestamp(ts, dt.timezone.utc)).strftime("%H:%M")
    except Exception:
        try:
            import assistant_profile                        # the profile's locale.tz (never raises)
            return dt.datetime.fromtimestamp(ts, assistant_profile.tz()).strftime("%H:%M")
        except Exception:
            return time.strftime("%H:%M", time.localtime(ts))


# ---------------------------------------------------------------- usage events (skipped for dry runs)
def _usage(kind, t, outcome=None, ms=None, ref=None, attrs=None):
    if t.get("mode") == "dry" and os.environ.get("WEBAGENT_USAGE_DRY") != "1":
        return False
    try:
        if USAGE_LIB not in sys.path:
            sys.path.insert(0, USAGE_LIB)
        import usage
        a = {"kind": t.get("kind"), "cat": (t.get("pending") or {}).get("category")}
        a.update(attrs or {})
        return usage.log(FEATURE, kind, channel="telegram", ms=ms, outcome=outcome, ref=ref,
                         attrs={k: v for k, v in a.items() if v is not None})
    except Exception:
        return False


# ---------------------------------------------------------------- task operations
DATA_KINDS = ("booking", "form")            # tasks that type his details: the fill session is asked at the start


def _fill_session(op, t, **kw):
    """securefill's fill-session hooks (start / end / state / tasks); off with WEBAGENT_FILL_SESSION=off.
    Never raises, never prints."""
    if os.environ.get("WEBAGENT_FILL_SESSION", "").lower() in ("off", "0", "no"):
        return None
    try:
        sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
        import securefill
        return securefill.for_webagent(op, t["id"] if t else None, **kw)
    except Exception:                                  # noqa: BLE001 - the web task goes on without it
        return None


def _set_fs(tid, fs):
    if fs:
        with Lock(tid):
            t = load(tid)
            if t:
                t["fill_session"] = fs
                save(t)
                return t
    return None


def start(title, url=None, kind="other", dry=False, agent=None, data="auto", minutes=None, retry_of="auto"):
    """A new task. data: yes / no / auto (= kinds booking + form): ask the vault for the task's fill session now
    (one ✅ in the vault bot; securefill fill asks lazily otherwise). retry_of: auto (an unfinished earlier attempt
    of the same job, same title + site within 24 h, is superseded and linked), an id, or none."""
    prior = _retry_target(title, url, dry, retry_of)
    t = _start(title, url, kind, dry, agent)
    if prior:
        t = _link_retry(prior, t)
    if data == "yes" or (data == "auto" and t["kind"] in DATA_KINDS):
        t = _set_fs(t["id"], _fill_session("start", t, minutes=minutes)) or t
    return t


def _start(title, url=None, kind="other", dry=False, agent=None):
    _ensure()
    tid = "wa" + secrets.token_hex(3)
    while os.path.exists(path_of(tid)):
        tid = "wa" + secrets.token_hex(3)
    t = {"v": 1, "id": tid, "title": redact(title, 120), "kind": kind if kind in KINDS else "other",
         "mode": "dry" if dry else "live", "state": "running", "created": now(), "updated": now(),
         "url": clean_url(url), "tab_url": clean_url(url), "start_host": host_of(url), "agent": redact(agent, 60), "steps": [], "shots": [],
         "pending": None, "approval": None, "result": None, "card_msg": None, "stops": 0, "history": [],
         "retry_of": None}
    with Lock(tid):
        save(t)
    wid = _wf_run_start(t)
    if wid:
        with Lock(tid):
            cur = load(tid) or t
            cur["wf_run"] = wid
            save(cur, touch=False)
            t = cur
    _usage("invoke", t, attrs={"dry": dry})
    return t


# ---------------------------------------------------------------- retries: one job, several attempts
SLD2 = {"co", "com", "net", "org", "gov", "ac", "edu"}


def _site(host):
    """The registrable part of a host, roughly: shop.ita-airways.com -> ita-airways.com, www.x.co.uk -> x.co.uk."""
    host = (host or "").lower().strip(".")
    if re.fullmatch(r"[\d.]+|.*:.*", host):                  # an IP address (v4, v6) is its own site
        return host
    parts = [p for p in host.split(".") if p]
    if len(parts) >= 3 and parts[-2] in SLD2 and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _job_key(t):
    """One job = same mode, same site and the same (normalized) title."""
    return t.get("mode"), _site(host_of(t.get("url")) or t.get("start_host")), norm(t.get("title"))


def _prior_attempt(t, tasks):
    """The attempt t retries: its recorded retry_of (tasks since 24.10 always carry the key; None = a new job), else,
    for older records, the latest earlier task of the same job started within RETRY_WINDOW, unless that one is done."""
    if "retry_of" in t:
        return next((x for x in tasks if t["retry_of"] and x.get("id") == t["retry_of"]), None)
    key, c = _job_key(t), t.get("created") or 0
    if not key[1] or not key[2]:
        return None
    prev = [x for x in tasks if x.get("id") != t.get("id") and _job_key(x) == key
            and c - RETRY_WINDOW <= (x.get("created") or 0) < c]
    p = max(prev, key=lambda x: x.get("created") or 0) if prev else None
    return None if not p or effective(p) == "done" else p


def _retry_target(title, url, dry, retry_of="auto"):
    """Before a new task: the unfinished earlier attempt of the same job it replaces (None = a new job). auto refuses
    (exit 6) while that attempt holds something to end first: an open card, an approved or executing step, or a stale
    task that `revive` brings back without a second vault ✅. An explicit id replaces any open or closed attempt
    except one that is executing or done."""
    mode = "dry" if dry else "live"
    rv = str(retry_of or "auto").strip()
    if rv.lower() == "none":
        return None
    if rv.lower() != "auto":
        p = load(resolve(rv)) or die(f"no task {rv}", 2)
        eff = effective(p)
        if eff in ("done", "executing") or p.get("mode") != mode:
            die(f"task {p['id']} is {p.get('mode')} and {eff}: not an attempt this task can replace", 6)
        return p
    p = _prior_attempt({"mode": mode, "url": clean_url(url), "start_host": host_of(url), "title": redact(title, 120),
                        "created": now()}, all_tasks())
    if not p:
        return None
    eff, pid = effective(p), p["id"]
    alt = f"; a fresh attempt = --retry-of {pid}, a separate task = --retry-of none"
    if eff == "pending":
        die(f"{pid} (same title and site) has an open card until {fmt_local(p.get('expires'))}: a corrected card = "
            f"`webagent stop {pid}` again" + alt + " (either cancels that card)", 6)
    if eff in ("approved", "executing"):
        die(f"{pid} (same title and site) is {eff}: let it finish or end it first" + alt, 6)
    ru = revive_until(p)
    if eff == "stale" and ru and ru > now() and (p.get("fill_session") or {}).get("state") in SESSION_LIVE:
        die(f"{pid} (same title and site) went stale with its fill session live: `webagent revive {pid}` (no second "
            f"vault ✅, until {fmt_local(ru)})" + alt, 6)
    return p


def _link_retry(p, t):
    """t replaces the earlier attempt p: an open or stale p is aborted (reason retry: its card, approval and fill
    session end; not an owner stop); both are linked (p.retried_as, t.retry_of)."""
    if effective(p) in OPEN + ("stale",):
        abort(p["id"], "retry")
    with Lock(p["id"]):
        cur = load(p["id"])
        if cur:
            cur["retried_as"] = t["id"]
            save(cur, touch=False)
    with Lock(t["id"]):
        cur = load(t["id"]) or t
        cur["retry_of"] = p["id"]
        save(cur, touch=False)
    return cur


def _securefill_leak(path):
    """True when a live secure-fill value is readable in the image (securefill's OCR check; no-op without fills)."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
        import securefill
        return securefill.check_image(path)
    except ImportError:
        return False


def _keep_shot(t, src, label):
    if not src:
        return None
    if not os.path.isfile(src):
        die(f"screenshot not found: {src}", 2)
    if _securefill_leak(src):
        die("this screenshot shows personal data from a secure fill: take it with `securefill shot "
            f"{t['id']}` (masked and checked) instead", 3)
    d = os.path.join(STATE, "shots", t["id"])
    os.makedirs(d, mode=0o700, exist_ok=True)
    ext = os.path.splitext(src)[1].lower() if os.path.splitext(src)[1].lower() in (".png", ".jpg", ".jpeg",
                                                                                     ".webp") else ".png"
    dst = os.path.join(d, f"{len(t.get('shots') or []) + 1:02d}-{label}{ext}")
    shutil.copyfile(src, dst)
    os.chmod(dst, 0o600)
    t.setdefault("shots", []).append(dst)
    return dst


def step(tid, desc, url=None, shot=None, kind="note", side_effect=None):
    with Lock(tid):
        t = get(tid, "running", "executing")
        rec = {"t": round(now(), 1), "kind": redact(kind, 20), "desc": redact(desc, 200)}
        if url:
            rec["url"] = t["url"] = t["tab_url"] = clean_url(url)
        if shot:
            rec["shot"] = _keep_shot(t, shot, "step")
        if side_effect:
            rec["side_effect"] = redact(side_effect, 120)
        t["steps"].append(rec)
        save(t)
    wf_task(tid, "step", t, n=len(t["steps"]))
    return t


def wait_owner(tid, until=None, why="fill session"):
    """securefill: the running task now waits for his ✅ in the vault bot (fill / session start exit 10). The stale clock
    pauses until `until` (the request's deadline; unknown -> WAIT_DEFAULT; at most WAIT_MAX ahead) and restarts from
    there. Only a running task (never a card's or an approval's window). -> the task, or None when it is not running."""
    at = now()
    until = until if isinstance(until, (int, float)) and until > at else at + WAIT_DEFAULT
    until = min(until, at + WAIT_MAX)
    with Lock(tid):
        t = load(tid)
        if not t or effective(t) != "running":
            return None
        w = t.get("waiting") or {}
        if not w:
            t.setdefault("history", []).append([round(at, 1), "running", "running", "waiting: " + redact(why, 40)])
        t["waiting"] = {"for": redact(why, 40), "since": w.get("since") or round(at, 1), "until": round(until, 1)}
        save(t)
    wf_task(tid, "wait", t, until=round(until, 1))
    return t


def wait_over(tid, outcome, touch=True):
    """His vault ✅ request is decided (session active, denied, expired, over): the running task stops waiting. touch
    (securefill, the agent's own call): the stale clock restarts now; else (sweep) it restarts at the wait's deadline,
    or now if that is still ahead. -> the task, or None (no wait, or the task is no longer running)."""
    with Lock(tid):
        t = load(tid)
        if not t or not t.get("waiting") or effective(t) != "running":
            return None
        w = t.pop("waiting")
        t.setdefault("history", []).append([round(now(), 1), "running", "running",
                                            "wait over: " + redact(str(outcome), 20)])
        if not touch:
            t["updated"] = max(t.get("updated") or 0, min(w.get("until") or 0, now()))
        save(t, touch)
    wf_task(tid, "wait", t)
    return t


def revive(tid):
    """A stale task back to running: within REVIVE_WINDOW of going stale, only one that went stale while running (not
    executing), and only while its fill session is still live (pending or active), e.g. he tapped the vault ✅ after
    the task timed out. Logged (history + a usage run). No card or approval is revived: the next committing step needs
    its own card and his ✅. Exit codes in the CLI: 2 no task, 4 too late, 6 not revivable."""
    def check(t):
        if t["state"] != "stale":
            die(f"task {tid} is {t['state']}: only a stale task can be revived", 6)
        if _stale_from(t) != "running":
            die(f"task {tid} went stale while {_stale_from(t)} (the approved step may have run): check the page; "
                "anything further is a new task", 6)
        until = revive_until(t)
        if not until or now() > until:
            die(f"task {tid} is stale for more than {REVIVE_WINDOW // 60} min: start a new task", 4)

    with Lock(tid):
        t = get(tid)
    check(t)
    fs = _fill_session("state", t)                   # a vault poll: outside the lock
    if fs not in SESSION_LIVE:
        fs = (fs.get("state") or "unknown") if isinstance(fs, dict) else (fs or "gone")
        die(f"task {tid}: its fill session is {fs}: nothing live to go on with; start a new task (or `webagent abort "
            f"{tid} --reason agent`)", 6)
    with Lock(tid):
        t = get(tid)
        check(t)
        at = now()
        stale_s = int(at - (stale_since(t) or at))
        _move(t, "running", f"revive (fill session {fs})")
        t.pop("stale_at", None)
        t.setdefault("revived", []).append({"at": round(at, 1), "stale_s": stale_s, "fill_session": fs})
        save(t)
    # a revive is a healthy run: outcome ok (any other run outcome counts as a failed run + an error in the usage
    # store); the state rides in attrs
    _usage("run", t, outcome="ok", attrs={"state": "revived", "session": fs, "stale_s": stale_s})
    wf_task(tid, "revive", t)
    return t


def _tick_labels(ticks):
    """The exact checkbox labels his ✅ covers (securefill ticks them): stored verbatim, so no secrets, no edits."""
    out = []
    for x in ticks or []:
        x = re.sub(r"\s+", " ", str(x or "")).strip()
        if not x:
            continue
        if len(x) > TICK_MAX:
            die(f"--tick label longer than {TICK_MAX} chars: pass the checkbox's exact visible label", 2)
        if redact(x, TICK_MAX) != x:
            die("--tick label looks like it carries a number or secret: pass only the checkbox's visible label", 2)
        if x not in out:
            out.append(x)
    if len(out) > 8:
        die("at most 8 --tick labels", 2)
    return out


# ---------------------------------------------------------------- approvals
def _ap():
    """The approvals library next to this one (~/bin/lib/approvals), or None: cards then use wa:ok/wa:no buttons."""
    try:
        import approvals
        return approvals
    except Exception:
        return None


def _ap_con(A):
    """The approvals store for one call. None (live runs) = the library's own default: the hub while hub approvals are
    on (switch hub-approvals, or hub-tg: his taps then reach only the hub's adapter, which decides them there), else
    the live local store. A connection passed in skips the hub, so a card minted there can never be decided by his
    tap. Tests: APPROVALS_DB, else beside the task state when WEBAGENT_STATE is set; WEBAGENT_HUB=on gives them the
    library default too (with a scratch HOME and HUB_URL)."""
    if os.environ.get("WEBAGENT_HUB") == "on" or not (os.environ.get("APPROVALS_DB")
                                                      or os.environ.get("WEBAGENT_STATE")):
        return None
    if os.environ.get("APPROVALS_DB"):
        return A.connect()
    return A.connect(os.path.join(STATE, "approvals.db"))


def _action(t):
    """The canonical action his ✅ on this stop covers (only its sha256 is stored)."""
    p = t.get("pending") or {}
    return json.dumps(["webagent", t["id"], t.get("stops", 0), p.get("category"), p.get("label"), p.get("target"),
                       p.get("url"), p.get("amount"), p.get("ticks") or [], bool(p.get("card"))],
                      ensure_ascii=False, separators=(",", ":"))


def _ap_summary(t):
    p = t.get("pending") or {}
    return " · ".join(x for x in (f"“{p.get('label')}”", p.get("amount"), t.get("title")) if x)


def _new_approval(t):
    """A pending approval for the stop's action (TTL = PENDING_TTL), or None when the store can't make one."""
    A = _ap()
    if A is None:
        return None
    try:
        con = _ap_con(A)
        try:
            return A.new(_action(t), _ap_summary(t), PENDING_TTL, ref=f"webagent:{t['id']}", con=con)["nonce"]
        finally:
            if con is not None:
                con.close()
    except Exception:
        return None


def _ap_call(fn):
    """Run fn(A, con) on the approvals store (con None = the library's default, _ap_con); None when the library, the
    store or the hub is unavailable."""
    A = _ap()
    if A is None:
        return None
    try:
        con = _ap_con(A)
        try:
            return fn(A, con)
        finally:
            if con is not None:
                con.close()
    except Exception:
        return None


def _nonce_evidence(t, verb):
    """His tap on the card's ap:<nonce> buttons. ok: consumes the approval for exactly this stop (once). Returns
    (evidence or None, settled, why): settled = the nonce decided it (declined, replaced, expired, used), so the older
    proofs don't count either; not settled (no tap yet, no store) = they may."""
    n = (t.get("pending") or {}).get("nonce")
    if not n:
        return None, False, None
    owner = owner_chat()

    def go(A, con):
        A.sync(n, journal=JOURNAL, owner=owner, con=con)
        r = A.get(n, con=con)
        if r is None:
            return None, False, None
        st = A.state(r)
        msg = str(r.get("message_id") or t.get("card_msg") or "")
        if verb == "no":
            if st == "denied":
                return {"via": "button", "msg": msg, "at": r.get("decided_ts") or now(), "nonce": n}, True, None
            return None, False, None
        if st == "pending":
            return None, False, None
        if st != "approved":
            return None, True, {"denied": "he declined it (❌ on the card)", "cancelled": "that card was replaced",
                                "expired": "the approval expired", "used": "that approval was used already"}.get(st, st)
        code, info = A.consume(n, _action(t), f"webagent:{t['id']}", journal=JOURNAL, owner=owner, con=con)
        if code == 0:
            return {"via": "button", "msg": msg, "at": r.get("decided_ts") or now(), "nonce": n}, True, None
        return None, True, info.get("why") or f"approval refused ({code})"

    return _ap_call(go) or (None, False, None)


def _cancel_approval(nonce):
    if nonce:
        _ap_call(lambda A, con: A.cancel(nonce, con=con))


# ---------------------------------------------------------------- hub workflow mirror
def wf_on():
    return os.path.exists(WF_SWITCH)


def _wf_log(op, wid, outcome):
    try:
        _ensure()
        with open(os.path.join(STATE, "wf.log"), "a") as f:
            f.write(json.dumps({"ts": round(now(), 1), "op": op, "wf": wid, "outcome": outcome}) + "\n")
    except OSError:
        pass


def _wf(op, method, path, body, wid):
    """One call to the hub's /wf API (core.hub, the brain's token). Fail-open: the answer or None; the outcome code
    (ok, or the error's class and HTTP code) goes to STATE/wf.log."""
    try:
        try:
            from core import hub
        except ImportError:                                  # a lib copy without core: the deployed one
            sys.path.append(os.path.join(HOME, "bin", "lib"))
            from core import hub
        got = hub.call(method, path, body, timeout=WF_TIMEOUT)
        _wf_log(op, wid, "ok")
        return got
    except Exception as e:
        code = getattr(e, "code", None) or (re.match(r"\d{3}", str(e)) or [None])[0]
        _wf_log(op, wid, type(e).__name__ + (f":{code}" if code else ""))
        return None


def _wf_start(t):
    """Start this stop's approval wait on the hub; the workflow id, or None (switch off, or the hub refused)."""
    if not wf_on():
        return None
    p = t.get("pending") or {}
    wid = f"webtask:{t['id']}:{t.get('stops', 0)}"
    args = {"task": t["id"], "stop": int(t.get("stops", 0)), "ttl": max(30, min(3600, int(PENDING_TTL))),
            "notify": [] if t.get("mode") == "dry" else ["expired"]}
    if p.get("nonce"):
        args["nonce"] = p["nonce"]
    got = _wf("start", "POST", "/wf/start", {"name": "webtask.approval", "args": args, "id": wid}, wid)
    return wid if got is not None else None


def _wf_decide(wid, decision, via):
    """Tell the stop's workflow how its wait ended (approved, stopped, replaced). Once per decision (idempotency key)."""
    if not wid or not wf_on():
        return
    _wf(decision, "POST", f"/wf/{wid}/send", {"topic": "decision", "message": {"decision": decision,
        "via": str(via or "webagent")[:40]}, "key": f"{wid}:{decision}"}, wid)


def wf_run_on():
    return os.path.exists(WF_RUN_SWITCH)


def _wf_run_start(t):
    """The task as the hub's webtask.run workflow (id webtask:<id>); the id, or None (switch off, hub refused)."""
    if not wf_run_on():
        return None
    wid = f"webtask:{t['id']}"
    dry = t.get("mode") == "dry"
    args = {"task": t["id"], "kind": t.get("kind") if t.get("kind") in KINDS else "other", "dry": dry,
            "pending_ttl": max(30, min(3600, PENDING_TTL)), "approved_ttl": max(30, min(3600, APPROVED_TTL)),
            "idle": max(60, min(14400, RUNNING_TTL)), "revive": max(0, min(14400, REVIVE_WINDOW)),
            "wait_max": max(0, min(3600, WAIT_MAX)),
            # the mirror's webtask.approval reports an unanswered card already while its switch is on
            "notify": [] if dry else (["stale", "failed"] if wf_on() else ["stale", "expired", "failed"])}
    got = _wf("run.start", "POST", "/wf/start", {"name": "webtask.run", "args": args, "id": wid}, wid)
    return wid if got is not None else None


def wf_task(tid, op, t=None, **fields):
    """One message to the task's webtask.run workflow (ids, counts and states only, never text). Fail-open; a
    no-op while the switch is off or the task has no workflow."""
    if not wf_run_on():
        return
    t = t or load(tid)
    wid = (t or {}).get("wf_run")
    if not wid:
        return
    msg = {"op": op, **{k: v for k, v in fields.items() if v is not None}}
    key = f"{wid}:{op}:{int(now() * 1000)}"[:128]
    _wf("run." + op, "POST", f"/wf/{wid}/send", {"topic": "task", "message": msg, "key": key}, wid)


def resolve(ref):
    """A task id, or a card's callback data ap:<nonce>:y|n (or the bare nonce): the task whose card carries it."""
    m = re.fullmatch(r"(?:ap:)?([0-9a-f]{32})(?::[ynx])?", str(ref or "").strip().lower())
    if not m:
        return ref
    for t in all_tasks():
        if (t.get("pending") or {}).get("nonce") == m.group(1):
            return t["id"]
    die(f"no web task carries approval {m.group(1)[:8]}…", 2)


def stop(tid, label, what, shot, target=None, url=None, category=None, verify=None, card=False, amount=None,
         ticks=None, summary=None, declined=None):
    """The agent reached a committing action: record it, open the 15-min approval window, build the card.
    Booking/order tasks stop once, at the final hard step (pay/book/order), with a full summary."""
    if card and not re.search(r"\d", amount or ""):
        die("--card needs --amount (the exact total he approves, e.g. 'CHF 127.00')", 2)
    if amount and not re.search(r"\d", amount):
        die("--amount needs the exact total, e.g. 'CHF 127.00'", 2)
    ticks = _tick_labels(ticks)
    with Lock(tid):
        t = get(tid, "running", "pending")         # pending: a corrected card replaces the unanswered one
        c = classify(label + " " + (target or ""))
        cat = category or c["category"] or "other"
        if t.get("kind") in SUMMARY_KINDS and (card or c["tier"] == "hard" or cat in ("pay", "book")) \
                and not (summary and amount):
            die(f"the final {t['kind']} card needs --summary \"what is booked/ordered\" and --amount \"the exact "
                "total\" (plus --declined \"extras declined\" and --tick \"<exact T&C box label>\" per required box)", 2)
        if url:
            t["url"] = t["tab_url"] = clean_url(url)
        old_nonce = (t.get("pending") or {}).get("nonce") if t["state"] == "pending" else None
        old_wf = (t.get("pending") or {}).get("wf") if t["state"] == "pending" else None
        t["pending"] = {"label": redact(label, 80), "what": redact(what, 220), "target": redact(target, 160),
                        "category": cat, "tier": c["tier"] or "manual", "matched": c["matched"],
                        "url": t["url"], "verify": redact(verify, 160), "set_at": round(now(), 1),
                        "shot": _keep_shot(t, shot, "stop"), "card": bool(card),
                        "amount": redact(amount, 40) if amount else None, "ticks": ticks,
                        "summary": redact(summary, 300), "declined": redact(declined, 200)}
        t["expires"] = now() + PENDING_TTL
        if t["state"] == "pending" and t.get("card_msg"):
            t["replaced_msgs"] = (t.get("replaced_msgs") or []) + [str(t["card_msg"])]
        t["approval"], t["card_msg"] = None, None
        t["stops"] = t.get("stops", 0) + 1
        t["pending"]["nonce"] = _new_approval(t)
        _move(t, "pending", cat)
        save(t)
    _cancel_approval(old_nonce)                      # taps on the replaced card approve nothing
    _wf_decide(old_wf, "replaced", "webagent")
    wid = _wf_start(t)
    if wid:
        with Lock(tid):
            cur = load(tid)
            if cur and cur.get("state") == "pending" and cur.get("stops") == t.get("stops"):
                cur["pending"]["wf"] = wid
                save(cur, touch=False)
                t = cur
    wf_task(tid, "stop", t, stop=int(t.get("stops", 0)), nonce=(t.get("pending") or {}).get("nonce"))
    if card and _fill_session("state", t) in (None, "ended", "expired", "cancelled", "dead"):
        # no live fill session: the vault asks now, next to this card (a denied one is not asked again)
        t = _set_fs(tid, _fill_session("start", t)) or t
    return t


def owner_chat():
    try:
        with open(ACCESS) as f:
            a = json.load(f)
        return str((a.get("allowFrom") or [None])[0] or "")
    except (OSError, ValueError):
        return ""


def browser_link():
    try:
        r = subprocess.run([BROWSER, "link", "--json"], capture_output=True, text=True, timeout=30)
        d = json.loads(r.stdout or "{}")
        return d.get("url") if d.get("verified") else None
    except Exception:
        return None


def card(t, link="auto"):
    """The approval card for the telegram reply tool: text, files (the screenshot), buttons."""
    p = t.get("pending") or {}
    steps = [s["desc"] for s in t.get("steps", []) if s.get("kind") in ("fill", "select", "click", "navigate")]
    done = f"{len(t.get('steps', []))} steps"
    if steps:
        n = 6 if p.get("summary") else 3
        done += " · " + "; ".join(steps[-n:])
    url = None
    if link == "auto" and t.get("mode") == "live":
        url = browser_link()
    elif link not in ("auto", "off", None):
        url = link
    lines = []
    if t.get("mode") == "dry":
        lines.append("\U0001f9ea DRY RUN · nothing real is sent")
    lines += [f"\U0001f310 {CATEGORY_TITLE.get(p.get('category'), CATEGORY_TITLE['other'])}: {t.get('title')}", ""]
    if p.get("summary"):
        lines.append(f"\U0001f4cb {p['summary']}")
    if p.get("declined"):
        lines.append(f"Declined: {p['declined']}")
    if p.get("amount"):
        lines.append(f"Total: {p['amount']}")
    if p.get("ticks"):
        lines.append("Ticks: " + "; ".join(p["ticks"]))
    lines += [f"What happens: {p.get('what')}",
              f"Button: “{p.get('label')}” on {host_of(p.get('url') or t.get('url')) or 'the page'}",
              f"Done so far: {done}"[:420 if p.get("summary") else 230]]
    if p.get("card"):
        fs = _fill_session("state", t)
        if fs == "active":
            lines += ["\U0001f4b3 This ✅ is also your card OK: the vault puts the card straight into the page "
                      "(Claude never sees it) and reports what was shared at the end"]
        elif fs == "pending":
            lines += ["\U0001f4b3 Your card: also ✅ the vault's fill request (vault bot, once for this task), "
                      "then the card goes straight into the page"]
        else:
            lines += ["\U0001f4b3 With your card: the vault asks you once more (✅ there, same amount), "
                      "then the card goes straight into the page"]
    lines += [
              f"Expires {fmt_local(t.get('expires') or now())} · ✅ runs exactly this one step"
              + (" and ticks these boxes" if p.get("ticks") else "")]
    n = p.get("nonce")
    yes, no = (f"ap:{n}:y", f"ap:{n}:n") if n else (f"wa:ok:{t['id']}", f"wa:no:{t['id']}")
    buttons = [[{"text": "✅ Do it", "data": yes}, {"text": "❌ Stop", "data": no}]]
    if url:
        buttons.append([{"text": "\U0001f5a5 Open my browser", "url": url}])
    return {"chat_id": owner_chat(), "text": "\n".join(lines), "files": [p["shot"]] if p.get("shot") else [],
            "buttons": buttons}


def sent(tid, msg):
    with Lock(tid):
        t = get(tid, "pending")
        t["card_msg"] = str(msg)
        save(t)
    n = (t.get("pending") or {}).get("nonce")
    owner = owner_chat()
    if n and owner and str(msg).isdigit():           # poll mode: the card for the expiry check (tg-adapter does it itself)
        _ap_call(lambda A, con: A.attach(n, owner, int(msg), con=con))
    _usage("notify_sent", t, ref=f"tg:{msg}")
    return t


def _iso(s):
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def evidence(t, verb, typed_msg=None, button=None):
    """His ✅/❌ on the card's ap:<nonce> buttons first (_nonce_evidence; a decided nonce settles it), then the older
    proofs in the telegram-plus inbound journal (_journal_evidence)."""
    ev, settled, _ = _nonce_evidence(t, verb)
    if ev or settled:
        return ev
    return _journal_evidence(t, verb, typed_msg, button)


def _hub_journal(since):
    """hub-tg: the hub's Telegram rows from `since` on as journal entries (core.hub.tg_journal; the hub's
    adapter writes no journal), owner from ACCESS. Only for the default journal (WEBAGENT_JOURNAL unset) or
    WEBAGENT_HUB=on; WEBAGENT_HUB=off, hub-tg off or the hub down: []."""
    mode = os.environ.get("WEBAGENT_HUB", "")
    if mode == "off" or (mode != "on" and os.environ.get("WEBAGENT_JOURNAL")):
        return []
    try:
        try:
            from core import hub
        except ImportError:                                  # a lib copy without core: the deployed one
            sys.path.append(os.path.join(HOME, "bin", "lib"))
            from core import hub
        if mode != "on" and not hub.tg_hub():
            return []
        return hub.tg_journal(max(0.0, since), kinds=("tg.button", "tg.reaction", "tg.message"),
                              owner_id=owner_chat() or None, timeout=3.0)
    except Exception:                                        # noqa: BLE001 - the journal file alone
        return []


def _journal_evidence(t, verb, typed_msg=None, button=None):
    """His tap (wa:<verb>:<id>, or `button` = another flow's own yes button such as shop:buy:galaxus), a thumbs
    reaction on the card, or (typed_msg) his message, after the stop, as the telegram-plus inbound journal recorded
    them, or the hub while the Telegram transport runs there (_hub_journal) (owner entries only; voice and app
    hand-offs are not owner entries)."""
    since = ((t.get("pending") or {}).get("set_at") or 0) - 1     # Telegram stamps whole seconds
    want = f"wa:{verb}:{t['id']}"
    thumbs = ("\U0001f44d", "\U0001f44c", "✅") if verb == "ok" else ("\U0001f44e", "❌")
    try:
        with open(JOURNAL) as f:
            lines = f.readlines()[-500:]
    except OSError:
        lines = []
    entries = []
    for line in lines:
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    entries += _hub_journal(since)
    for d in reversed(entries):                                   # newest first
        if not isinstance(d, dict) or d.get("owner") is not True:
            continue
        m = d.get("meta") or {}
        ts = _iso(m.get("ts")) or _iso(d.get("ts")) or _iso(d.get("at"))
        if ts is None or ts < since:
            continue
        if d.get("kind") == "button" and m.get("callback_data") in (want, button):
            if m.get("callback_data") == want and str(m.get("message_id") or "") in (t.get("replaced_msgs") or []):
                continue                                         # a tap on a card a re-stop replaced
            return {"via": "button", "msg": str(m.get("message_id") or ""), "at": ts}
        if (d.get("kind") == "reaction" and t.get("card_msg")
                and str(m.get("reacted_to_message_id")) == str(t["card_msg"]) and m.get("reaction") in thumbs):
            return {"via": "reaction", "msg": str(t["card_msg"]), "at": ts}
        if typed_msg and d.get("kind") == "message" and str(m.get("message_id")) == str(typed_msg):
            return {"via": "typed", "msg": str(typed_msg), "at": ts}
    return None


def approve(tid, simulate=False, typed=None, button=None):
    with Lock(tid):
        t = get(tid, "pending")
        if simulate:
            if t.get("mode") != "dry":
                die("--simulate is only for dry runs; a live task needs his tap", 3)
            ev = {"via": "simulated", "msg": None, "at": now()}
        else:
            ev, settled, why = _nonce_evidence(t, "ok")
            if not ev and not settled:
                ev = _journal_evidence(t, "ok", typed, button)
            if not ev:
                die(f"no approval from him for {tid}: {why}" if why else
                    f"no approval from him for {tid} yet (✅ on the card, \U0001f44d on the card, or --typed MSG_ID "
                    "of his yes)", 5)
        t["approval"] = {"via": ev["via"], "msg": ev["msg"], "at": round(now(), 1), "until": now() + APPROVED_TTL,
                         "label": t["pending"]["label"], "url": t["pending"]["url"], "consumed": None,
                         "nonce": ev.get("nonce")}
        _move(t, "approved", ev["via"])
        save(t)
    _usage("tap", t, outcome="ok", ref=f"tg:{t['card_msg']}" if t.get("card_msg") else None,
           ms=int((now() - t["pending"]["set_at"]) * 1000),
           attrs={"via": ev["via"], "ticks": len(t["pending"].get("ticks") or [])})
    _wf_decide(t["pending"].get("wf"), "approved", ev["via"])
    wf_task(tid, "decision", t, decision="approved", via=str(ev["via"])[:40], stop=int(t.get("stops", 0)))
    return t


def resume(tid):
    """What the executing agent may do now. Exit codes in the CLI: 0 go, 4 expired, 5 not approved yet, 6 closed."""
    t = load(tid)
    if not t:
        die(f"no task {tid}", 2)
    with Lock(tid):
        t = get(tid)
    s = t["state"]
    p = t.get("pending") or {}
    host = host_of(p.get("url") or t.get("url"))
    out = {"id": tid, "state": s, "mode": t["mode"], "title": t["title"], "tab_url": t.get("tab_url"),
           "label": p.get("label"), "target": p.get("target"), "what": p.get("what"), "verify": p.get("verify"),
           "category": p.get("category"), "execute": False, "card": bool(p.get("card")), "ticks": p.get("ticks") or []}
    if s == "approved":
        out["approved_until"] = fmt_local(t["approval"]["until"])
        if t["mode"] == "dry" and host not in DRY_HOSTS:
            out["why"] = f"dry run on {host}: do NOT click; record `webagent done {tid} --ok --note 'dry: not run'`"
        else:
            out["execute"] = True
            if p.get("card"):
                out["why"] = (f"approved card payment: run `securefill commit {tid} --tab-url <your tab's location.href>`"
                              " (the card comes from the vault through the task's fill session: no further ✅ once "
                              "it is live, exit 10 = his ✅ in the vault bot still open; it ticks the listed boxes, "
                              "fills the card, clicks the label once and screenshots the result); never click it "
                              "yourself, then `webagent done`")
                return out, 0
            out["why"] = ("approved: re-select your tab (location.href = tab_url), "
                          + (f"run `securefill tick {tid} --tab-url <location.href>` (ticks exactly the listed boxes), "
                             if p.get("ticks") else "")
                          + "re-find the element by label, click it with element = the label, then screenshot + "
                            "webagent done")
        return out, 0
    out["why"] = {"pending": "waiting for his tap", "expired": "expired: do nothing; a fresh run needs a new card",
                  "executing": "already executing (the approved click went through)"}.get(s, f"task is {s}")
    if s == "stale" and (revive_until(t) or 0) > now():
        out["why"] += (f": report it; the main session can `webagent revive {tid}` while its fill session is live "
                       f"(until {fmt_local(revive_until(t))})")
    return out, {"pending": 5, "expired": 4}.get(s, 6)


def done(tid, ok=True, shot=None, note=None, cont=False):
    with Lock(tid):
        t = get(tid, "running", "approved", "executing")
        appr = t.get("approval") or {}
        t["result"] = {"ok": bool(ok), "note": redact(note, 240), "at": round(now(), 1),
                       "shot": _keep_shot(t, shot, "result") if shot else None,
                       "committed": t["state"] == "executing"}
        to = "running" if (cont and ok) else ("done" if ok else "failed")
        _move(t, to, "continue" if cont else ("ok" if ok else "fail"))
        if to == "running":
            t["pending"], t["approval"], t["expires"] = None, None, None
        save(t)
    ms = int((now() - appr["at"]) * 1000) if appr.get("at") else int((now() - t["created"]) * 1000)
    _usage("exec", t, outcome="ok" if ok else "fail", ms=ms, attrs={"committed": t["result"]["committed"]})
    wf_task(tid, "done", t, ok=bool(ok), cont=to == "running", committed=bool(t["result"]["committed"]))
    if to != "running":                                # the fill session ends; the vault bot sends him the report
        t = _set_fs(tid, _fill_session("end", t, why="done" if ok else "failed")) or t
    return t


def result_card(t):
    r = t.get("result") or {}
    head = "✅ Done" if r.get("ok") else "⚠️ Didn't work"
    if t.get("mode") == "dry":
        head = "\U0001f9ea " + head + " (dry run)"
    text = f"{head}: {t['title']}" + (f"\n{r['note']}" if r.get("note") else "")
    return {"chat_id": owner_chat(), "text": text, "files": [r["shot"]] if r.get("shot") else []}


def abort(tid, reason="owner", typed=None):
    with Lock(tid):
        t = get(tid, *OPEN, "stale")                # stale: ends the fill session sweep keeps for `revive`
        was = t["state"]
        if reason == "owner" and was in ("pending", "approved"):
            ev = evidence(t, "no", typed)
            t["stopped_by"] = ev["via"] if ev else "session"
        _move(t, "aborted", reason)
        save(t)
    _cancel_approval((t.get("pending") or {}).get("nonce"))  # a tap on its card approves nothing now
    if was in ("pending", "approved"):
        _wf_decide((t.get("pending") or {}).get("wf"), "stopped", reason)
    wf_task(tid, "abort", t, reason=str(reason)[:40])
    if reason == "owner" and t.get("stopped_by") == "session":    # no ❌, 👎 or typed no from him: not his tap
        _usage("abandoned", t, outcome="session", ref=f"tg:{t['card_msg']}" if t.get("card_msg") else None)
    elif reason == "owner":
        _usage("tap" if was in ("pending", "approved") else "dismissed", t, outcome="stop",
               ref=f"tg:{t['card_msg']}" if t.get("card_msg") else None)
    else:
        _usage("error" if reason == "error" else "abandoned", t, outcome=reason)
    t = _set_fs(tid, _fill_session("end", t, why="aborted")) or t
    side = [s["side_effect"] for s in t.get("steps", []) if s.get("side_effect")]
    return t, {"id": tid, "state": "aborted", "was": was, "stopped_by": t.get("stopped_by"),
               "close_tab_url": t.get("tab_url"), "side_effects": side,
               "back_out": "close your own tab without clicking anything (never the last tab: navigate it to "
                           "about:blank); leave reversible side effects as they are and list them for him"}


def sweep():
    """Persist timeouts, prune finished tasks after 90 days and their screenshots after 7. Returns transitions."""
    moved, at = [], now()
    for t in all_tasks():
        tid = t.get("id")
        if not tid:
            continue
        if t.get("waiting") and effective(t) == "running":   # his vault ✅ decided meanwhile: the wait is over
            fs = _fill_session("state", t)                  # (a vault poll: outside the lock)
            if fs in ("active", "denied", "expired", "ended", "dead", "cancelled"):
                wait_over(tid, fs, touch=fs == "active")      # his ✅: time for the main session to resume
        with Lock(tid):
            t = load(tid)
            if not t:
                continue
            if _apply_timeouts(t):
                save(t)
                moved.append({"id": tid, "state": t["state"], "close_tab_url": t.get("tab_url")})
            end = (t.get("history") or [[t.get("updated")]])[-1][0] or t.get("updated") or at
            if t["state"] not in OPEN and at - end > SHOT_DAYS * 86400:
                shutil.rmtree(os.path.join(STATE, "shots", tid), ignore_errors=True)
            if t["state"] not in OPEN and at - end > KEEP_DAYS * 86400:
                os.remove(path_of(tid))
                try:
                    os.remove(os.path.join(STATE, f".{tid}.lock"))
                except OSError:
                    pass
    for sid in _fill_session("tasks", None) or []:     # fill sessions of closed tasks end (the report follows)
        t = load(sid)
        if t and (revive_until(t) or 0) > at:           # a stale task's session stays for `revive` (unusable while
            continue                                   # stale; it ends at its own limit)
        if not t or effective(t) not in OPEN:
            st = effective(t) if t else "expired"
            _fill_session("end", {"id": sid}, why=st if st in ("done", "failed", "aborted") else "expired")
    return moved


def public(t):
    """A status view: no screenshots' content, only paths and counts."""
    p = t.get("pending") or {}
    w = t.get("waiting") or {}
    w = w if t.get("state") == "running" and effective(t) == "running" and (w.get("until") or 0) > now() else {}
    ru = revive_until(t)
    out = {"id": t["id"], "state": effective(t), "mode": t["mode"], "kind": t["kind"], "title": t["title"],
            "url": t.get("url"), "steps": len(t.get("steps", [])), "stops": t.get("stops", 0),
            "pending": {k: p.get(k) for k in ("label", "category", "what", "shot")} if p else None,
            "expires": fmt_local(t["expires"]) if t.get("expires") and t["state"] == "pending" else None,
            "created": time.strftime("%Y-%m-%d %H:%M", time.localtime(t["created"])),
            "result": t.get("result"), "fill_session": t.get("fill_session"),
            "waiting": {"for": w.get("for"), "until": fmt_local(w["until"])} if w else None,
            "revive_until": fmt_local(ru) if ru and ru > now() else None}
    if p.get("wf"):
        out["wf"] = p["wf"]
    if t.get("wf_run"):
        out["wf_run"] = t["wf_run"]
    return out


def _ended_at(t):
    """When a task stopped moving: now while open, when it went stale, else its last move."""
    eff = effective(t)
    if eff in OPEN:
        return now()
    if eff == "stale":
        return stale_since(t) or t.get("updated") or 0
    if eff == "expired" and t.get("state") != "expired":
        return t.get("expires") if t.get("state") == "pending" else (t.get("approval") or {}).get("until") or 0
    h = t.get("history") or []
    return (h[-1][0] if h else None) or t.get("updated") or 0


def _cause(t):
    """Why an unfinished task ended, from its own fields (no page text, no labels)."""
    eff = effective(t)
    last = next((h for h in reversed(t.get("history") or []) if len(h) > 3), None)
    if eff == "aborted":
        why, was = (last[3], last[1]) if last and last[2] == "aborted" else ("", "")
        if why == "retry":
            return "superseded"
        if was in ("pending", "approved") and why == "owner":
            return "session_stop" if t.get("stopped_by") == "session" else "owner_stop"
        return "error" if why == "error" else "abort_running"
    if eff == "stale":
        st = t.get("steps") or []
        if any(len(h) > 3 and str(h[3]).startswith("waiting") for h in t.get("history") or []):
            return "stale_after_wait"                # waited for his vault ✅ (paused clock), then nothing came
        return "stale_after_fill" if st and st[-1].get("kind") == "fill" else "stale_no_step"
    if eff == "expired":
        return "approval_expired" if (t.get("state") == "approved" or (last and last[1] == "approved")) \
            else "card_expired"
    return eff


def _guard_denies(cut, tasks):
    """Guard denies since cut: {t, caller, tool, tier, cat, dry}. Records before 24.10 carry no dry flag: a deny
    while only dry tasks were open counts as dry."""
    win = [(t.get("mode"), t.get("created") or 0, _ended_at(t) + 5) for t in tasks]
    rows = []
    try:
        with open(os.path.join(STATE, "guard.log")) as f:
            lines = f.readlines()
    except OSError:
        return rows
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("decision") != "deny" or (r.get("t") or 0) < cut:
            continue
        dry = r.get("dry")
        if dry is None:
            modes = {m for m, a, b in win if a <= r["t"] <= b}
            dry = modes == {"dry"}
        rows.append({"t": fmt_local(r["t"]), "caller": "agent" if r.get("agent") else "main", "tool": r.get("tool"),
                     "tier": r.get("tier"), "cat": r.get("cat"), "active": r.get("active"), "dry": bool(dry)})
    return rows


def stats(days=30, include_dry=False, by_cause=False):
    """Counts per task and per job (a job = a task and its retries: retry_of, or for older records the same title +
    site within 24 h). by_cause adds one row per unfinished task and per guard deny (ids, kinds, states, sites; no
    titles, labels, URLs or page text)."""
    cut = now() - days * 86400
    every = [t for t in all_tasks() if t.get("created", 0) >= cut - RETRY_WINDOW]
    ts = sorted((t for t in every if t.get("created", 0) >= cut and (include_dry or t.get("mode") != "dry")),
                key=lambda t: t.get("created") or 0)
    final = {}
    for t in ts:
        final[effective(t)] = final.get(effective(t), 0) + 1
    carded = [t for t in ts if t.get("stops")]
    hist = [h for t in ts for h in t.get("history", [])]
    approvals = sum(1 for h in hist if h[2] == "approved")
    stops = [t for t in ts for h in t.get("history", []) if len(h) > 3 and h[2] == "aborted" and h[3] == "owner"
             and h[1] in ("pending", "approved")]
    owner_stops = sum(1 for t in stops if t.get("stopped_by") != "session")
    expired = sum(1 for h in hist if h[2] == "expired") + sum(1 for t in ts if effective(t) == "expired"
                                                                   and t.get("state") != "expired")
    lat = []
    for t in ts:
        a, p = t.get("approval") or {}, t.get("pending") or {}
        if a.get("at") and p.get("set_at"):
            lat.append(a["at"] - p["set_at"])
    completed = sum(1 for t in ts if effective(t) == "done")
    committed = sum(1 for t in ts if (t.get("result") or {}).get("committed"))
    job, succ = {}, {}
    for t in ts:
        p = _prior_attempt(t, ts)
        job[t["id"]] = job.get(p["id"], p["id"]) if p else t["id"]
        if p:
            succ[p["id"]] = t["id"]
    jobs_done = {job[t["id"]] for t in ts if effective(t) == "done"}
    njobs = len(set(job.values()))
    guard = _guard_denies(cut, every)
    out = {"days": days, "tasks": len(ts), "completed": completed, "committed": committed,
           "reached_card": len(carded), "approvals": approvals, "owner_stops": owner_stops,
           "session_stops": len(stops) - owner_stops, "expired": expired,
           "failed": final.get("failed", 0), "stale": final.get("stale", 0), "open": sum(final.get(s, 0) for s in OPEN),
           "retried": len(succ), "jobs": njobs, "jobs_completed": len(jobs_done),
           "abandon_rate": round((owner_stops + expired) / len(carded), 2) if carded else None,
           "completion_rate": round(completed / len(ts), 2) if ts else None,
           "job_completion_rate": round(len(jobs_done) / njobs, 2) if njobs else None,
           "median_approval_s": round(statistics.median(lat)) if lat else None,
           "guard_blocks": sum(1 for g in guard if include_dry or not g["dry"]),
           "guard_blocks_dry": sum(1 for g in guard if g["dry"])}
    if by_cause:
        rows = []
        for t in ts:
            eff = effective(t)
            if eff == "done" or eff in OPEN:
                continue
            st, p = t.get("steps") or [], t.get("pending") or {}
            end = _ended_at(t)
            rows.append({"id": t["id"], "kind": t.get("kind"), "mode": t.get("mode"), "state": eff, "cause": _cause(t),
                         "site": _site(host_of(t.get("url")) or t.get("start_host")),
                         "created": fmt_local(t.get("created")), "steps": len(st),
                         "last_step": st[-1].get("kind") if st else None,
                         "idle_min": round((end - st[-1]["t"]) / 60) if st and end else None,
                         "stop": f"{p.get('tier')}/{p.get('category')}" if p else None,
                         "card_sent": bool(t.get("card_msg")), "stopped_by": t.get("stopped_by"),
                         "job": job[t["id"]], "job_done": job[t["id"]] in jobs_done, "retried_as": succ.get(t["id"])})
        causes = {}
        for r in rows:
            causes[r["cause"]] = causes.get(r["cause"], 0) + 1
        out.update(causes=causes, unfinished=rows, guard=guard)
    return out


# ---------------------------------------------------------------- the PreToolUse guard
JS_ACTION = re.compile(r"\.(?:click|submit|requestSubmit|dispatchEvent)\s*\(|\bfetch\s*\(|XMLHttpRequest|sendBeacon"
                       r"|\.form\b[^;]*\.submit", re.I)
ENTER = {"enter", "numpadenter", "return"}
TOOL = "mcp__browser__browser_"


def _glog(rec):
    try:
        _ensure()
        rec["t"] = round(now(), 1)
        with open(os.path.join(STATE, "guard.log"), "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def _matches(t, text, target):
    p = t.get("pending") or {}
    if p.get("target") and target and str(target).strip() == p["target"]:
        return True
    lab = norm(p.get("label"))
    return bool(lab) and lab in norm(text)


def _consume(text, target, tool):
    """An approved task whose pending action is this one: mark it executing (one use) and return it."""
    for t in all_tasks():
        if t.get("state") != "approved" or effective(t) != "approved" or not _matches(t, text, target):
            continue
        with Lock(t["id"]):
            t = load(t["id"])
            if not t or effective(t) != "approved":
                continue
            t["approval"]["consumed"] = {"at": round(now(), 1), "tool": tool[len(TOOL):]}
            _move(t, "executing", "guard")
            save(t)
            return t
    return None


def _deny(reason, rec):
    _glog(dict(rec, decision="deny"))
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": "webagent-guard: " + reason}}


def guard(event):
    """Decide one browser tool call. Returns None (allow) or a hook deny object."""
    tool = str(event.get("tool_name") or "")
    if not tool.startswith(TOOL):
        return None
    ti = event.get("tool_input") or {}
    name = tool[len(TOOL):]
    tasks = [(t, effective(t)) for t in all_tasks()]
    active = [t for t, e in tasks if e in ACTIVE]
    opn = [t for t, e in tasks if e in OPEN]
    rec = {"tool": name, "agent": bool(event.get("agent_id")), "active": len(active),
           "dry": bool(opn) and all(t.get("mode") == "dry" for t in opn)}   # stats: rehearsal denies apart
    hint = ("stop first: `webagent stop <id> --label \"<button text>\" --what \"<what happens>\" --shot <png>` and "
            "end your turn; the main session sends him the approval card (skill web-agent).")
    if name in ("click", "type"):
        if name == "type" and not ti.get("submit"):
            return None
        text = label_text(ti)
        if name == "type" and SEARCH_RX.search(norm(text)) and not classify(text)["tier"] == "hard":
            return None
        c = classify(text)
        rec.update(tier=c["tier"], cat=c["category"], matched=c["matched"], exempt=c["exempt"])
        if not c["commit"]:
            if name == "type" and active:
                return _deny("Enter-submit is off during a web task: click the labelled submit/send button "
                             "instead (that click is classified).", rec)
            return None
        if c["tier"] == "soft" and not active:
            return None
        if CHECKBOX_RX.search(norm(text)):
            return _deny("terms/consent boxes are not clicked during a web task: list each required one on the final "
                         "stop with --tick \"<exact label>\"; after his ✅ securefill ticks exactly those.", rec)
        if c["tier"] == "soft" and c["category"] in AUTO_SOFT and all(
                a.get("kind") in SUMMARY_KINDS and effective(a) == "running" for a in active):
            _glog(dict(rec, decision="allow", why="auto_soft"))     # booking/order: intermediate steps run unasked
            return None
        cardt = [a for a in active if effective(a) == "approved" and (a.get("pending") or {}).get("card")
                 and _matches(a, text, ti.get("target"))]
        if cardt:
            return _deny(f"card payment step: run `securefill commit {cardt[0]['id']}` (it fills the card from the "
                         "vault and clicks this button itself)", rec)
        t = _consume(text, ti.get("target"), tool)
        if t:
            _glog(dict(rec, decision="allow", task=t["id"]))
            return None
        appr = [a for a in active if effective(a) == "approved"]
        if appr:
            return _deny(f"this {c['category']} action does not match the approved one "
                         f"(“{appr[0]['pending']['label']}”). Use exactly that label as element, or stop "
                         "and ask again for a different action.", rec)
        return _deny(f"“{text[:80]}” reads as a committing action ({c['category']}: "
                     f"{', '.join(c['matched'][:3])}). Nothing like it runs without his ✅. " + hint, rec)
    if not active:
        return None
    if name == "press_key" and str(ti.get("key") or "").replace(" ", "").lower() in ENTER:
        return _deny("Enter is off during a web task (it can submit a form): use browser_type with submit only "
                     "for search fields, or click the labelled button.", rec)
    if name == "evaluate" and JS_ACTION.search(str(ti.get("function") or "")):
        return _deny("JS clicks/submits/requests are off during a web task: act through browser_click with the "
                     "element's visible label, so the commit check sees it.", rec)
    if name == "mouse_click_xy":
        return _deny("coordinate clicks are off during a web task: click by snapshot ref with element = the visible "
                     "label; for canvas widgets hand over with the \U0001f5a5 browser link.", rec)
    return None


def guard_hook(event):
    try:
        return guard(event)
    except Exception as e:                                   # fail open: the protocol is the first layer
        _glog({"decision": "error", "err": type(e).__name__})
        return None
