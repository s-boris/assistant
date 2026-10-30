"""whereabouts: his coarse whereabouts in the `vault ha now --json` schema, phone app first, the vault as fallback.

Library (stdlib):
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), 'lib'))
    import whereabouts
    d = whereabouts.now(max_age_min=30)        # vault-schema dict + "source": "phone" | "vault"; None = nothing answered

Order: WHEREABOUTS_ORDER (e.g. "phone,vault" or "vault"), else ~/.config/whereabouts/config.json "order" (a
consumer's own entry under "consumers" wins when `consumer=` / --for / WHEREABOUTS_FOR names it), else "phone,vault".
Freshness: the phone answers when its state is <= max_age_min old (WHEREABOUTS_MAX_AGE_MIN, default 30) and it has a
place; otherwise the next source. The vault answer is taken as it is (its age_min tells how old it is). When every
source fails, a stale phone answer is still returned (its age_min shows it); nothing at all -> None. fresh_only=True
(or WHEREABOUTS_FRESH_ONLY=1; the switched consumers use it) drops that last resort, so with a stale phone the
answer is exactly the vault's, failure included.
Sources: phone = the phonegw internal API GET /internal/now (PHONEGW_URL, PHONEGW_TOKEN_FILE; 3 s; defaults as the
`phone` CLI's: http://claude-hub-phone:8796 + ~/.secrets/phone-cli-token; no proxy; a missing token file shows in
`whereabouts status`);
vault = WHEREABOUTS_VAULT_CMD (default `vault ha now --json`; 40 s). Never prints coordinates: neither source has any.
"""
import json
import os
import shlex
import subprocess
import urllib.error
import urllib.request

HOME = os.environ.get("HOME") or os.path.expanduser("~")
CONFIG = os.path.join(HOME, ".config/whereabouts/config.json")
DEFAULT_ORDER = ("phone", "vault")
SOURCES = ("phone", "vault")


def _config():
    try:
        with open(os.environ.get("WHEREABOUTS_CONFIG") or CONFIG) as fh:
            c = json.load(fh)
        return c if isinstance(c, dict) else {}
    except (OSError, ValueError):
        return {}


def order(consumer=None):
    raw = os.environ.get("WHEREABOUTS_ORDER")
    if not raw:
        c = _config()
        consumer = consumer or os.environ.get("WHEREABOUTS_FOR")
        raw = ((c.get("consumers") or {}).get(consumer) if consumer else None) or c.get("order")
    out = [s.strip() for s in str(raw or "").split(",") if s.strip() in SOURCES]
    return tuple(out) or DEFAULT_ORDER


def gateway():
    """-> (internal API base URL, token file) as the `phone` CLI picks them (the gateway runs in claude-hub-phone)."""
    url = os.environ.get("PHONEGW_URL") or "http://claude-hub-phone:8796"
    tok = os.environ.get("PHONEGW_TOKEN_FILE") or os.path.join(HOME, ".secrets/phone-cli-token")
    return url.rstrip("/"), tok


def _egress():
    """core.egress when this run uses the live default (claude-hub-phone + ~/.secrets/phone-cli-token, no env
    override) and the handle "phone" is in hub or shadow mode, else None (direct)."""
    if os.environ.get("PHONEGW_URL") or os.environ.get("PHONEGW_TOKEN_FILE"):
        return None
    try:
        from core import egress
    except Exception:
        return None
    return egress if egress.mode("phone") in ("hub", "shadow") else None


def phone_unset():
    """Why the phone source can't answer before asking it (its token file is missing), else None."""
    _, tok = gateway()
    eg = _egress()
    if os.path.exists(tok) or (eg is not None and eg.mode("phone") == "hub"):
        return None
    return "no token file %s" % tok


def phone_now(timeout=3):
    base, tok = gateway()
    eg = _egress()
    try:
        if eg is not None:
            token = eg.secret("phone")  # hub mode: the placeholder (the hub puts the token on the wire)
        else:
            with open(tok) as fh:
                token = fh.read().strip()
        req = urllib.request.Request(base + "/internal/now", headers={"Authorization": "Bearer " + token})
        with (eg.open(req, "phone", timeout=timeout) if eg is not None
              else urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=timeout)) as r:
            d = json.loads(r.read())
    except (OSError, ValueError, KeyError, urllib.error.URLError):
        return None
    return d if isinstance(d, dict) and d.get("connection", {}).get("state") != "no_device" else None


def vault_now(timeout=40):
    argv = shlex.split(os.environ.get("WHEREABOUTS_VAULT_CMD") or "") or [os.path.join(HOME, "bin/vault"), "ha", "now", "--json"]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        if p.returncode != 0:
            return None
        d = json.loads(p.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if not isinstance(d, dict):
        return None
    d.setdefault("source", "vault")
    return d


def fresh(d, max_age_min):
    return bool(d and d.get("age_min") is not None and d["age_min"] <= max_age_min and d.get("place"))


def now(max_age_min=None, consumer=None, explain=None, fresh_only=None):
    """-> vault-schema dict with "source", or None. explain: a list that gets one line per source tried.
    fresh_only: never fall back to a stale phone answer (default: WHEREABOUTS_FRESH_ONLY=1)."""
    if max_age_min is None:
        max_age_min = int(os.environ.get("WHEREABOUTS_MAX_AGE_MIN") or 30)
    if fresh_only is None:
        fresh_only = (os.environ.get("WHEREABOUTS_FRESH_ONLY") or "").strip().lower() in ("1", "true", "yes", "on")
    log = explain if explain is not None else []
    stale = None
    for src in order(consumer):
        if src == "phone":
            d = phone_now()
            if fresh(d, max_age_min):
                log.append("phone: fresh (age %s min)" % d["age_min"])
                return d
            why = phone_unset() if d is None else None
            log.append("phone: %s" % (("no answer (%s)" % why if why else "no answer") if d is None
                                      else "stale or no place (age %s min)" % d.get("age_min")))
            if d and d.get("place"):
                stale = d
        else:
            d = vault_now()
            if d is not None:
                log.append("vault: answered (age %s min)" % d.get("age_min"))
                return d
            log.append("vault: no answer")
    if stale and fresh_only:
        log.append("stale phone answer not used (fresh only)")
        return None
    if stale:
        log.append("using the stale phone answer")
    return stale
