#!/usr/bin/env python3
"""hubd: the hub's API. The loader runs it from a release (`python3 -I -B -u app/hubd.py`); only it
touches Postgres (hubstore, psycopg vendored in the release's vendor/). asyncio, HTTP/1.1 with keep-alive, JSON.

  GET  /health     no token. 200 while it serves: {"ok", "release", "role", "service", "since", "kit", "loader",
                   "loader_expected", "last_good", "bad", "checks": {"pg", "phoenix", "ci"}, "ci_release",
                   "store": {"state", "schema", "migrations"}, "api": {"n", "p50_ms", "p95_ms", "errors"}}.
                   Ids, states, counts and timings only: never content, never secrets.
  GET  /whoami     the caller a token belongs to.
  Everything else needs "Authorization: Bearer <token>" (HUB_SECRETS/token_<caller>). Callers: brain (the brain
  container: its scripts, CLIs and hooks) and tg (the Telegram adapter). The ledger, outbox writes, approval
  requests and consumes are the brain's; outbox consuming, approval decisions (his taps) and card edits are tg's.
  "X-Hub-Schema: t_<name>" works on a test schema (created on first use, dropped with POST /admin/drop).
  Writes take "rid" (a request id): a retry with the same rid gets the first answer and writes nothing.

  POST /events/emit · /events/update · GET /events/<id>            ledger typed writes and one row
  POST /ledger/snapshot · /ledger/apply                            brain-side units of work (hubstore docs)
  POST /outbox · GET /outbox/<id> · POST /outbox/<id>/withdraw     the senders' side
  POST /outbox/queued · /outbox/<id>/claim · /outbox/<id>/set · /outbox/<id>/fail · /outbox/interrupted
       /outbox/dupes · GET /outbox/consumers                       the consumer's side (tg)
  POST /approvals · GET /approvals[?every=1] · GET /approvals/audit · GET /approvals/<nonce>
  POST /approvals/<nonce>/attach|consume|cancel (brain) · /approvals/<nonce>/decide|card · /approvals/expire (tg)
  Every approval route is also decided by Cedar, LOG-ONLY (tools.policy decide_api, policy.cedar "approvals-*"):
  the brain requests, consumes and cancels; his tap (tg) decides, cards and expires; both read and attach. The route
  table above keeps enforcing; each decision goes to STATE/approvals-policy.jsonl (op, caller, principal, decision,
  policy ids: never nonces, actions or summaries) and /health "approvals_policy" counts them.
  POST /admin/import · GET /admin/dump?table=T&after=K&limit=N · GET /admin/counts · POST /admin/drop
  POST /admin/restart                                              exit 0 after answering; the loader restarts it
  /tg/*  the Telegram transport (hubtg): the adapter's rows, meta, jobs and presence (tg); the bridge's inbound
         poll, deliveries and receipts, the companion's jobs, named reads for brain-side readers, bindings (brain);
         raw file transfer GET /tg/files/<in|out>/<name> · POST /tg/files (both). Full list: hubtg's docstring.
  GET  /tg/hold (both) · POST /tg/hold {held, why} (brain) · POST /tg/hold/seen {state, release, why} (tg)
  GET  /secrets/fingerprints (brain)  HMAC fingerprints of the hub's secrets for secretscan (never a value;
       503 secrets_off in a release without hubredact). /health "secrets": counts (files, values, refused, needles)
  GET  /egress/status · POST /egress/check · POST /egress/fetch (brain)  the egress injector (hubpolicy_egress,
       protected): placeholder handles in, the credential swapped in only toward its bound endpoint, answers masked
       (503 egress_off in a release without it). /health "egress": counts
         the brain's emergency stop for claude-hub-tg (`hub tg stop|start|status`): a flag in STATE/tg-hold.json
         (no store needed; unreadable = held); tgloader reports on /seen every poll, stops the adapter while held
         and starts none. GET answers {held, why, ts, by, loader: {state, release, why, ts, age_s} | null}.

The status feed (hubfeed): GET /feed (brain, tg), POST /feed/brain and /feed/watch (brain) here; the read-only
listener on HUB_FEED_PORT (8702, "off" = none) serves /feed, /feed/stream (SSE) and /feed/health without a token.

Workflows (hubwfapi + hubwf: DBOS Transact on this Postgres, schema "dbos"): /wf/* (brain) start, read, message
and cancel durable workflows and edit the watch schedules; hubwfapi's docstring lists them. The engine has its own
switch (HUB_STATE/workflows.json, `hub wf engine on|off`; no file = off) and loads DBOS only while on; /health
carries its brief state under "workflows". A failed launch leaves the API above untouched.

The gateway (tools.hubgw: FastMCP over streamable HTTP + Cedar) runs in this event loop on HUB_GW_PORT (8701,
"off" = not started); /health carries its brief state under "gateway". A gateway that fails to start leaves the
API above untouched (the ledger keeps working); /health then says why.

Errors: {"error": code, "hint": text} with 400 bad request · 401 · 403 not this caller's · 404 · 409 conflict (re-run
the unit of work / retry) · 503 store unavailable. Env: HUB_RELEASE, HUB_PORT, HUB_SECRETS, HUB_SPOOL, HUB_KIT,
HUB_LOADER, HUB_LOADER_SHA, HUB_LOADER_STATE, PG_HOST, PG_PORT, PG_DB, PG_USER (password: HUB_SECRETS/pg_password or
PG_PASSWORD_FILE), PHOENIX_URL; tests: HUB_VENDOR, HUB_SQL_DIR, HUB_BIND.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import signal
import socket
import sys
import time
import urllib.parse
import urllib.request
from collections import deque
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.environ.get("HUB_VENDOR", ""), os.path.join(_HERE, "..", "vendor")):
    if _p and os.path.isdir(_p) and os.path.abspath(_p) not in sys.path:
        sys.path.insert(0, os.path.abspath(_p))

import hubfeed  # noqa: E402  (the read-only status feed and its stream listener)
import hubsecrets  # noqa: E402  (protected: the secret loader)
import hubverify  # noqa: E402  (protected: the release verifier)
import hubwfapi  # noqa: E402  (the workflow engine's switch and API; stdlib, DBOS loads only while on)

GW: Any = None  # tools.hubgw.Running once the gateway serves
FEED: Any = None  # hubfeed.Feed once it runs (HUB_FEED_PORT "off" = never)
WF: Any = None  # hubwfapi.Engine once hubd runs
GW_ERROR = ""
hs: Any = None  # hubstore, or None without the driver: /health still answers (store unavailable), routes say 503
hci: Any = None  # hubci (the CI runner's hub side; needs hubstore)
DRIVER_ERROR = ""
BOARD_ERROR = ""
try:
    import hubci as _hubci  # noqa: E402
    import hubstore as _hubstore  # noqa: E402

    hs, hci = _hubstore, _hubci
except Exception as _e:
    DRIVER_ERROR = f"{type(_e).__name__}"
hvoice: Any = None  # hubvoice (the voice relay's hold and events); own import like hubtg
VOICE_ERROR = ""
hphone: Any = None  # hubphone (the phone gateway's hold and events); own import like hubvoice
PHONE_ERROR = ""
hbrowser: Any = None  # hubbrowser (claude-hub-browser's token check reads and commit receipts)
BROWSER_ERROR = ""
htg: Any = None  # hubtg (the Telegram transport); its own import, so a fault there never takes the rest down
TG_ERROR = ""
if hs is not None:
    try:
        import hubvoice as _hubvoice  # noqa: E402

        hvoice = _hubvoice
    except Exception as _e:
        VOICE_ERROR = f"{type(_e).__name__}"
if hs is not None:
    try:
        import hubphone as _hubphone  # noqa: E402

        hphone = _hubphone
    except Exception as _e:
        PHONE_ERROR = f"{type(_e).__name__}"
hboard: Any = None  # hubboard (the board as records); needs hubstore
if hs is not None:
    try:
        import hubboard as _hubboard  # noqa: E402

        hboard = _hubboard
    except Exception as _e:
        BOARD_ERROR = f"{type(_e).__name__}"
if hs is not None:
    try:
        import hubbrowser as _hubbrowser  # noqa: E402

        hbrowser = _hubbrowser
    except Exception as _e:
        BROWSER_ERROR = f"{type(_e).__name__}"
if hs is not None:
    try:
        import hubtg as _hubtg  # noqa: E402

        htg = _hubtg
    except Exception as _e:
        TG_ERROR = f"{type(_e).__name__}"

CHECK_TTL = 10.0
CI_FRESH = 60.0
MAX_BODY = 8 << 20
MAX_BODY_CI = 24 << 20  # POST /ci/jobs: a cold-mirror CI bundle (the whole tree, ~5 MB at 29.10) as base64
KEEPALIVE = 30.0
OP_TIMEOUT = 20.0
RELEASE = os.environ.get("HUB_RELEASE", "")
SINCE = time.time()
BRAIN, TG = frozenset({"brain"}), frozenset({"tg"})
VOICE = frozenset({"voice"})
PHONE = frozenset({"phone"})
BROWSER = frozenset({"browser"})  # claude-hub-browser (token_browser): /browser/approval + /browser/commit only
BOTH = BRAIN | TG
REASON = {
    200: "OK",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    409: "Conflict",
    413: "Payload Too Large",
    500: "Internal Server Error",
    503: "Service Unavailable",
}


SECRETS = os.environ.get("HUB_SECRETS", "/run/hub-secrets")
TOKENS = hubsecrets.load_tokens(SECRETS)

# hubredact (protected) holds the secrets folder in memory, masks its values in every gateway result and exports
# HMAC fingerprints to the brain (GET /secrets/fingerprints). A release ships it only with hubkit.release's
# H10_IN_RELEASE (his ✅ on the protected digest); without it nothing here changes. With it, a store or redactor that
# fails to build fails closed: the gateway answers `internal` rather than an unmasked result.
hr: Any = None
STORE: Any = None
REDACTOR: Any = None
REDACT_ERROR = ""
try:
    import hubredact as _hubredact  # noqa: E402

    hr = _hubredact
except ImportError:
    REDACT_ERROR = "absent"
except Exception as _e:
    REDACT_ERROR = f"{type(_e).__name__}"
if hr is not None:
    try:
        STORE = hr.Store.load(SECRETS)
        REDACTOR = hr.Redactor(STORE)
        print(json.dumps({"event": "secrets_loaded", **STORE.state(), "needles": len(REDACTOR)}), flush=True)
    except Exception as _e:
        STORE, REDACTOR, REDACT_ERROR = None, None, f"{type(_e).__name__}"


def _redact_refused(res: Any) -> tuple[Any, list[str]]:
    raise RuntimeError("hubredact present but its store did not build")


def redact_fn() -> Any:
    """What the gateway masks results with: the redactor; a refusing stub when hubredact is here but broken; None
    when the release has no hubredact."""
    if REDACTOR is not None:
        return REDACTOR.obj
    return _redact_refused if hr is not None else None


def secrets_state() -> dict[str, Any]:
    if STORE is not None:  # /health needs no token: counts only (the refused names come with the brain's export)
        st = STORE.state()
        return {"state": "ok", **st, "refused": len(st["refused"]), "needles": len(REDACTOR) if REDACTOR else 0}
    return {"state": "absent" if hr is None else "failed", "why": REDACT_ERROR}


def fingerprints(who: str) -> tuple[int, Any]:
    if who != "brain":
        return 403, {"error": "forbidden", "hint": "/secrets/fingerprints is the brain caller's"}
    if STORE is None:
        return 503, {"error": "secrets_off", "hint": f"no secret store in this release ({REDACT_ERROR or 'failed'})"}
    try:
        key = hr.fp_key(state_dir())
    except OSError as e:
        return 503, {"error": "fp_key", "hint": f"the fingerprint key is unavailable ({type(e).__name__})"}
    return 200, STORE.fingerprints(key)


def caller_of(header: str | None) -> str | None:
    if not header or not header.startswith("Bearer "):
        return None
    got = header[7:].strip().encode()
    who = None
    for name, tok in TOKENS.items():  # compare against every token: no early exit
        if hmac.compare_digest(got, tok):
            who = name
    return who


# ------------------------------------------------------------------------------------------------ health


_checks: dict[str, Any] = {"at": -1e9, "value": {}}


def _pg_tcp() -> str:
    host = os.environ.get("PG_HOST", "")
    if not host:
        return "unset"
    if host.startswith("/"):
        return "ok" if os.path.exists(host) else "down"
    try:
        with socket.create_connection((host, int(os.environ.get("PG_PORT", "5432"))), timeout=1.5):
            return "ok"
    except (OSError, ValueError):
        return "down"


def _phoenix() -> str:
    url = os.environ.get("PHOENIX_URL", "")
    if not url:
        return "unset"
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/healthz", timeout=2) as r:
            return "ok" if r.status == 200 else "down"
    except OSError:
        return "down"


def _ci() -> tuple[str, str | None]:
    spool = os.environ.get("HUB_SPOOL", "")
    if not spool:
        return "unset", None
    p = os.path.join(spool, "ci-heartbeat.json")
    try:
        age = time.time() - os.stat(p).st_mtime
        with open(p, encoding="utf-8") as f:
            rel = json.load(f).get("release")
    except (OSError, ValueError, AttributeError):
        return "missing", None
    return ("ok" if age < CI_FRESH else "stale"), rel if isinstance(rel, str) else None


def _seen_probe(mod: Any) -> str:
    """claude-hub-voice / -phone for the feed: their loader's last /hold/seen report (unset = module not loaded)."""
    return "unset" if mod is None else hubfeed.loader_probe(mod.loader_seen())


def _browser_probe() -> str:
    """claude-hub-browser for the feed: a TCP connect to its CDP service (HUB_FEED_BROWSER host:port, "off" = unset);
    the container reports nothing to hubd, and a connect sends nothing."""
    v = os.environ.get("HUB_FEED_BROWSER", "claude-hub-browser:9223")
    host, _, port = v.rpartition(":")
    if v == "off" or not host or not port.isdigit():
        return "unset"
    return hubfeed.tcp_probe(host, int(port))


def loader_state() -> dict[str, Any]:
    try:
        with open(os.environ.get("HUB_LOADER_STATE", ""), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def state_dir() -> str:
    """The hub's own state folder (HUB_ROOT/state): the brain can't write it, only ask hubd to."""
    return os.environ.get("HUB_STATE") or os.path.dirname(os.environ.get("HUB_LOADER_STATE", "")) or "/var/lib/hub"


# ------------------------------------------------------------------------------------------------ egress
# The egress injector (hubpolicy_egress + app/policy/egress.toml, both protected): the brain's tools send requests with
# placeholder handles and the hub swaps in the credential, only toward the handle's bound endpoint (core.egress on the
# brain side). A release carries it only with hubkit.release's EGRESS_IN_RELEASE (his ✅ on the protected digest). It
# needs hubredact (else it stays off). Routes (brain caller): GET /egress/status, POST /egress/check
# (the decision only, nothing sent), POST /egress/fetch (send). /health "egress": counts only.
hegress: Any = None
EGRESS: Any = None
EGRESS_ERROR = ""
EGRESS_LIMIT = 8  # concurrent egress calls (each runs in a worker thread)
_EGRESS_SEM: asyncio.Semaphore | None = None
try:
    import hubpolicy_egress as _hegress  # noqa: E402

    hegress = _hegress
except ImportError:
    EGRESS_ERROR = "absent"
except Exception as _e:
    EGRESS_ERROR = f"{type(_e).__name__}"
if hegress is not None:
    if STORE is None or REDACTOR is None:
        EGRESS_ERROR = "no secret store"
    else:
        try:
            EGRESS = hegress.Injector.load(os.path.join(_HERE, "policy", "egress.toml"), STORE, REDACTOR, state_dir())
            print(json.dumps({"event": "egress_loaded", **EGRESS.brief()}), flush=True)
        except Exception as _e:
            EGRESS, EGRESS_ERROR = None, f"{type(_e).__name__}"


def egress_state() -> dict[str, Any]:
    if EGRESS is not None:
        return EGRESS.brief()
    return {"state": "absent" if hegress is None else "failed", "why": EGRESS_ERROR}


async def egress_api(method: str, path: str, who: str, body: bytes) -> tuple[int, Any]:
    global _EGRESS_SEM
    if who != "brain":
        return 403, {"error": "forbidden", "hint": f"{path} is the brain caller's"}
    if EGRESS is None:
        return 503, {"error": "egress_off", "hint": f"no egress injector in this release ({EGRESS_ERROR or 'failed'})"}
    if path == "/egress/status" and method == "GET":
        return 200, EGRESS.status()
    if method != "POST" or path not in ("/egress/check", "/egress/fetch"):
        return 404, {"error": "not_found"}
    try:
        a = json.loads(body or b"{}")
    except ValueError:
        return 400, {"error": "bad_json"}
    if path == "/egress/check":
        return EGRESS.check(a)
    if _EGRESS_SEM is None:
        _EGRESS_SEM = asyncio.Semaphore(EGRESS_LIMIT)
    async with _EGRESS_SEM:
        return await asyncio.to_thread(EGRESS.fetch, a)


# ------------------------------------------------------------------------------------------------ tg hold
# The brain's emergency stop for claude-hub-tg. The flag lives in STATE/tg-hold.json (no store: it answers while
# Postgres is down). tgloader posts /tg/hold/seen every poll with its own state and gets the flag back: held = it stops
# the adapter and starts none (it keeps the last answer in its own state, so a restart with the hub away stays down).
TG_HOLD_FILE = "tg-hold.json"
LOADER_STATES = ("starting", "running", "down", "conflict", "idle", "held")
LOADER_RID = re.compile(r"^[0-9a-f]{7,40}$")
TG_LOADER: dict[str, Any] = {}  # tgloader's last report (memory only; a restart forgets it): state, release, why, ts


def _text(v: Any, n: int = 200) -> str:
    return re.sub(r"[\x00-\x1f\x7f]", " ", v if isinstance(v, str) else "")[:n]


def tg_hold() -> dict[str, Any]:
    """{held, why, ts, by}. No file = not held; an unreadable or malformed file holds (the stop fails closed)."""
    try:
        with open(os.path.join(state_dir(), TG_HOLD_FILE), encoding="utf-8") as f:
            d = json.load(f)
    except FileNotFoundError:
        return {"held": False, "why": "", "ts": None, "by": None}
    except (OSError, ValueError):
        return {"held": True, "why": "hold file unreadable", "ts": None, "by": None}
    if not isinstance(d, dict) or not isinstance(d.get("held"), bool):
        return {"held": True, "why": "hold file malformed", "ts": None, "by": None}
    ts = d.get("ts")
    ok_ts = isinstance(ts, (int, float)) and not isinstance(ts, bool)
    return {"held": d["held"], "why": _text(d.get("why")), "ts": ts if ok_ts else None, "by": _text(d.get("by"), 16)}


def tg_hold_write(held: bool, why: str, who: str) -> dict[str, Any]:
    d = {"held": held, "why": why, "ts": round(time.time(), 3), "by": who}
    os.makedirs(state_dir(), exist_ok=True)
    p = os.path.join(state_dir(), TG_HOLD_FILE)
    tmp = f"{p}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)
    return d


def tg_loader_seen() -> dict[str, Any] | None:
    if not TG_LOADER:
        return None
    return {**TG_LOADER, "age_s": round(time.time() - TG_LOADER["ts"], 1)}


def tg_hold_api(method: str, path: str, who: str, schema: str, body: bytes) -> tuple[int, Any]:
    """GET /tg/hold (both) · POST /tg/hold {held, why} (brain) · POST /tg/hold/seen {state, release, why} (tg)."""
    if path == "/tg/hold" and method == "GET":
        return 200, {**tg_hold(), "loader": tg_loader_seen()}
    if method != "POST" or path not in ("/tg/hold", "/tg/hold/seen"):
        return 404, {"error": "not_found"}
    try:
        a = json.loads(body or b"{}")
    except ValueError:
        return 400, {"error": "bad_json"}
    if not isinstance(a, dict):
        return 400, {"error": "bad_json", "hint": "a JSON object"}
    if path == "/tg/hold":
        if who != "brain":
            return 403, {"error": "forbidden", "hint": f"{path} is not the {who} caller's"}
        if schema:
            return 400, {"error": "bad_request", "hint": "the tg hold is live only (no X-Hub-Schema)"}
        held = a.get("held")
        if not isinstance(held, bool):
            return 400, {"error": "bad_request", "hint": "held: true or false"}
        before = tg_hold()
        try:
            d = tg_hold_write(held, _text(a.get("why")), who)
        except OSError as e:
            return 503, {"error": "state_unwritable", "hint": type(e).__name__}
        print(json.dumps({"event": "tg_hold", "held": held, "was": before["held"], "caller": who}), flush=True)
        return 200, {**d, "changed": before["held"] != held, "loader": tg_loader_seen()}
    if who != "tg":
        return 403, {"error": "forbidden", "hint": f"{path} is not the {who} caller's"}
    st, rel = a.get("state"), a.get("release")
    if st not in LOADER_STATES:
        return 400, {"error": "bad_request", "hint": f"state: one of {', '.join(LOADER_STATES)}"}
    if rel is not None and not (isinstance(rel, str) and LOADER_RID.match(rel)):
        return 400, {"error": "bad_request", "hint": "release: a release id or null"}
    if TG_LOADER.get("state") != st or TG_LOADER.get("release") != rel:
        print(json.dumps({"event": "tg_loader", "state": st, "release": rel}), flush=True)
    TG_LOADER.update(state=st, release=rel, why=_text(a.get("why")), ts=round(time.time(), 3))
    return 200, tg_hold()


class Stats:
    def __init__(self) -> None:
        self.ms: deque[float] = deque(maxlen=2000)
        self.n = 0
        self.errors = 0

    def add(self, ms: float, err: bool) -> None:
        self.ms.append(ms)
        self.n += 1
        self.errors += 1 if err else 0

    def summary(self) -> dict[str, Any]:
        s = sorted(self.ms)

        def pct(p: float) -> float | None:
            return round(s[min(len(s) - 1, int(p * len(s)))], 2) if s else None

        return {"n": self.n, "p50_ms": pct(0.5), "p95_ms": pct(0.95), "errors": self.errors}


class Hub:
    def __init__(self) -> None:
        self.store: Any = None  # hubstore.Store once the driver loaded
        self.stats = Stats()
        self.store_state: dict[str, Any] = {"at": -1e9, "value": {"state": "starting"}}
        self.stopping = asyncio.Event()

    def conninfo(self) -> dict[str, Any]:
        pw = hubsecrets.read_secret("pg_password", "PG_PASSWORD_FILE")
        kw: dict[str, Any] = {
            "host": os.environ.get("PG_HOST", "pg"),
            "port": int(os.environ.get("PG_PORT", "5432")),
            "dbname": os.environ.get("PG_DB", "hub"),
            "user": os.environ.get("PG_USER", "hub"),
            "application_name": "hubd",
        }
        if pw:
            kw["password"] = pw
        return kw

    async def start(self) -> None:
        if hs is not None:
            self.store = hs.Store(self.conninfo())
            try:
                await asyncio.wait_for(self.store.ensure(hs.LIVE), 15)
            except Exception as e:  # the API still starts; /health says why, each call retries
                print(json.dumps({"event": "store_start_failed", "error": type(e).__name__}), flush=True)

    async def store_health(self) -> dict[str, Any]:
        if time.monotonic() - self.store_state["at"] < CHECK_TTL:
            return self.store_state["value"]
        if self.store is None:
            v: dict[str, Any] = {"state": "no driver", "why": DRIVER_ERROR}
        else:
            try:
                migs = await asyncio.wait_for(self.store.migrated(hs.LIVE), 5)
                v = {"state": "ok", "schema": hs.LIVE, "migrations": migs}
            except Exception as e:
                v = {"state": "down", "why": type(e).__name__}
        self.store_state = {"at": time.monotonic(), "value": v}
        return v

    def gateway_state(self) -> dict[str, Any]:
        if GW is not None:
            try:
                return GW.brief()
            except Exception as e:
                return {"state": "error", "why": type(e).__name__}
        return {"state": "off" if GW_ERROR == "off" else "down", "why": GW_ERROR or "starting"}

    async def health(self) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        if time.monotonic() - _checks["at"] >= CHECK_TTL or not _checks["value"]:
            pg, ph, (ci, ci_rel) = await asyncio.gather(
                loop.run_in_executor(None, _pg_tcp),
                loop.run_in_executor(None, _phoenix),
                loop.run_in_executor(None, _ci),
            )
            _checks.update(at=time.monotonic(), value={"pg": pg, "phoenix": ph, "ci": ci, "ci_release": ci_rel})
        c = dict(_checks["value"])
        ci_rel = c.pop("ci_release", None)
        st = loader_state()
        store = await self.store_health()
        return {
            "ok": all(v == "ok" for v in c.values()) and store.get("state") == "ok",
            "release": RELEASE,
            "role": "hub",
            "service": "hubd",
            "since": round(SINCE, 3),
            "kit": os.environ.get("HUB_KIT", ""),
            "loader": os.environ.get("HUB_LOADER_SHA", ""),
            "loader_expected": os.environ.get("HUB_LOADER", ""),
            "last_good": st.get("last_good"),
            "bad": sorted(st.get("bad") or {}),
            "checks": c,
            "ci_release": ci_rel,
            "callers": len(TOKENS),
            "store": store,
            "protected": dict(hubverify.STATE),
            "ci_runner": hci.counts() if hci is not None else None,
            "api": self.stats.summary(),
            "gateway": self.gateway_state(),
            "feed": FEED.health() if FEED is not None else {"state": "off"},
            "tg": htg.brief() if htg is not None else {"state": "down", "why": TG_ERROR or DRIVER_ERROR},
            "tg_hold": {"held": tg_hold()["held"], "loader": TG_LOADER.get("state")},
            "secrets": secrets_state(),
            "egress": egress_state(),
            "workflows": WF.brief() if WF is not None else {"state": "off", "why": "not started"},
            "approvals_policy": APPOL.brief(),
        }

    # ------------------------------------------------------------------------------------------ routes

    async def dispatch(self, method: str, target: str, headers: dict[str, str], body: bytes) -> tuple[int, Any]:
        u = urllib.parse.urlsplit(target)
        path, query = u.path, {k: v[-1] for k, v in urllib.parse.parse_qs(u.query).items()}
        if path == "/health" and method == "GET":
            return 200, await self.health()
        who = caller_of(headers.get("authorization"))
        if who is None:
            return 401, {"error": "unauthorized"}
        if path == "/whoami" and method == "GET":
            return 200, {"caller": who, "release": RELEASE}
        if path == "/feed" or path.startswith("/feed/"):  # the feed (no store needed: it answers without pg)
            if FEED is None:
                return 503, {"error": "feed_off", "hint": "HUB_FEED_PORT is off or the feed failed to start"}
            return await FEED.api(method, path, who, body)
        if path == "/secrets/fingerprints" and method == "GET":  # no store needed
            return fingerprints(who)
        if path.startswith("/egress/"):  # the egress injector (no store needed)
            return await egress_api(method, path, who, body)
        if path == "/wf" or path.startswith("/wf/"):  # workflows (DBOS keeps its own pool; no hubstore tx)
            if WF is None:
                return 503, {"error": "workflows_off", "hint": "hubd is starting"}
            sch = headers.get("x-hub-schema") or ""
            return await WF.api(method, path, who, "" if sch == "hub" else sch, query, body)
        if path in ("/voice/hold", "/voice/hold/seen"):  # the brain's stop for claude-hub-voice
            if hvoice is None:
                return 503, {"error": "voice_off", "hint": VOICE_ERROR or "hubvoice not loaded"}
            sch = headers.get("x-hub-schema") or ""
            return hvoice.hold_api(method, path, who, "" if sch == "hub" else sch, body)
        if path in ("/phone/hold", "/phone/hold/seen"):  # the brain's stop for claude-hub-phone
            if hphone is None:
                return 503, {"error": "phone_off", "hint": PHONE_ERROR or "hubphone not loaded"}
            sch = headers.get("x-hub-schema") or ""
            return hphone.hold_api(method, path, who, "" if sch == "hub" else sch, body)
        if path in ("/tg/hold", "/tg/hold/seen"):  # the brain's stop for claude-hub-tg (no store needed)
            sch = headers.get("x-hub-schema") or ""
            return tg_hold_api(method, path, who, "" if sch == "hub" else sch, body)
        for m, rx, fn, allowed in ROUTES:
            if m != method:
                continue
            g = re.fullmatch(rx, path)
            if g is None:
                continue
            op = ap_op(m, rx)
            if op is not None:  # Cedar, log-only; the caller check below still decides
                APPOL.check(who, op, (headers.get("x-hub-schema") or hs.LIVE) != hs.LIVE if hs else False)
            if who not in allowed:
                return 403, {"error": "forbidden", "hint": f"{path} is not the {who} caller's"}
            if self.store is None:
                return 503, {"error": "store_unavailable", "hint": f"no database driver ({DRIVER_ERROR})"}
            try:
                a = json.loads(body or b"{}") if body else {}
            except ValueError:
                return 400, {"error": "bad_json"}
            if not isinstance(a, dict):
                return 400, {"error": "bad_json", "hint": "a JSON object"}
            schema = headers.get("x-hub-schema") or hs.LIVE
            if not hs.SCHEMA_RE.match(schema):
                return 400, {"error": "bad_schema"}
            tmo = OP_TIMEOUT + (htg.LONG_MAX + 2 if htg is not None and fn.__name__ in htg.LONG else 0)
            try:
                return 200, await asyncio.wait_for(fn(self, who, schema, a, query, *g.groups()), tmo)
            except hs.BadRequest as e:
                return 400, {"error": "bad_request", "hint": str(e)[:300]}
            except hs.Forbidden as e:
                return 403, {"error": "forbidden", "hint": str(e)[:300]}
            except hs.Conflict as e:
                return 409, {"error": "conflict", "hint": str(e)[:300]}
            except NotFound as e:
                return 404, {"error": "not_found", "hint": str(e)[:300]}
            except hci.NotFound as e:
                return 404, {"error": "not_found", "hint": str(e)[:300]}
            except TgNotFound as e:
                return 404, {"error": "not_found", "hint": str(e)[:300]}
            except BrowserNotFound as e:
                return 404, {"error": "not_found", "hint": str(e)[:300]}
            except hs.psycopg.errors.UniqueViolation:
                return 409, {"error": "conflict", "hint": "a concurrent duplicate: retry"}
            except (hs.psycopg.OperationalError, TimeoutError, OSError) as e:
                return 503, {"error": "store_unavailable", "hint": type(e).__name__}
            except hs.psycopg.Error as e:
                print(json.dumps({"event": "store_error", "path": path, "error": type(e).__name__}), flush=True)
                return 500, {"error": "store_error", "hint": type(e).__name__}
        return 404, {"error": "not_found"}

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while not self.stopping.is_set():
                line = await asyncio.wait_for(reader.readline(), KEEPALIVE)
                if not line:
                    break
                parts = line.decode("latin-1").rstrip("\r\n").split(" ")
                if len(parts) != 3:
                    break
                method, target, version = parts
                headers: dict[str, str] = {}
                while True:
                    h = await asyncio.wait_for(reader.readline(), 10)
                    if h in (b"\r\n", b"\n", b""):
                        break
                    k, _, v = h.decode("latin-1").partition(":")
                    headers[k.strip().lower()] = v.strip()
                if htg is not None and target.startswith("/tg/files"):  # streamed both ways, never whole in memory
                    t0 = time.perf_counter()
                    keep = await htg.files_raw(method, target, headers, reader, writer,
                                               caller_of(headers.get("authorization")))  # fmt: skip
                    self.stats.add((time.perf_counter() - t0) * 1000, False)
                    if not keep or version != "HTTP/1.1":
                        break
                    continue
                try:
                    n = int(headers.get("content-length") or 0)
                except ValueError:
                    n = -1
                if n < 0 or n > (MAX_BODY_CI if target.split("?", 1)[0] == "/ci/jobs" else MAX_BODY):
                    await self.respond(writer, 413, {"error": "too_large"}, False)
                    break
                body = await asyncio.wait_for(reader.readexactly(n), 30) if n else b""
                t0 = time.perf_counter()
                try:
                    status, obj = await self.dispatch(method, target, headers, body)
                except Exception as e:  # never leak details
                    print(json.dumps({"event": "handler_error", "error": type(e).__name__}), flush=True)
                    status, obj = 500, {"error": "internal"}
                if not target.startswith("/health"):
                    self.stats.add((time.perf_counter() - t0) * 1000, status >= 500)
                keep = version == "HTTP/1.1" and headers.get("connection", "").lower() != "close"
                await self.respond(writer, status, obj, keep)
                if not keep:
                    break
                if status == 200 and target == "/admin/restart" and method == "POST":
                    break
        except (TimeoutError, asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            writer.close()

    async def respond(self, w: asyncio.StreamWriter, status: int, obj: Any, keep: bool) -> None:
        data = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()
        head = (
            f"HTTP/1.1 {status} {REASON.get(status, 'Error')}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(data)}\r\nConnection: {'keep-alive' if keep else 'close'}\r\n\r\n"
        )
        w.write(head.encode() + data)
        await w.drain()


class BrowserNotFound(Exception):  # hubbrowser's 404 (its own class: hubbrowser may be absent)
    pass


if hbrowser is not None:
    BrowserNotFound = hbrowser.NotFound  # type: ignore[misc]  # noqa: F811


class NotFound(Exception):
    pass


TgNotFound: Any = htg.NotFound if htg is not None else NotFound


def _int(v: Any, name: str) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        raise hs.BadRequest(f"{name}: a number") from None


# Handlers: (hub, caller, schema, body, query, *groups) -> JSON


async def r_emit(h: Hub, who: str, s: str, a: dict, q: dict) -> Any:
    async with h.store.tx(s) as c:
        return await hs.once(c, a.get("rid"), who, "emit", lambda: hs.emit(c, a))


async def r_update(h: Hub, who: str, s: str, a: dict, q: dict) -> Any:
    async with h.store.tx(s) as c:
        return await hs.once(c, a.get("rid"), who, "update", lambda: hs.update(c, a))


async def r_event(h: Hub, who: str, s: str, a: dict, q: dict, eid: str) -> Any:
    async with h.store.tx(s) as c:
        r = await hs.one(c, "SELECT * FROM events WHERE id=%s", (int(eid),))
    if r is None:
        raise NotFound(f"event {eid}")
    return r


async def r_snapshot(h: Hub, who: str, s: str, a: dict, q: dict) -> Any:
    async with h.store.tx(s) as c:
        return await hs.snapshot(c, a)


async def r_apply(h: Hub, who: str, s: str, a: dict, q: dict) -> Any:
    async with h.store.tx(s) as c:
        return await hs.once(c, a.get("rid"), who, "apply", lambda: hs.apply(c, a))


async def r_enqueue(h: Hub, who: str, s: str, a: dict, q: dict) -> Any:
    async with h.store.tx(s) as c:
        return await hs.once(c, a.get("rid"), who, "outbox", lambda: hs.outbox_enqueue(c, a))


async def r_outbox(h: Hub, who: str, s: str, a: dict, q: dict, oid: str) -> Any:
    async with h.store.tx(s) as c:
        r = await hs.outbox_get(c, int(oid))
    if r is None:
        raise NotFound(f"outbox row {oid}")
    return r


async def r_withdraw(h: Hub, who: str, s: str, a: dict, q: dict, oid: str) -> Any:
    async with h.store.tx(s) as c:
        return await hs.outbox_withdraw(c, int(oid), str(a.get("why") or "withdrawn"))


async def r_queued(h: Hub, who: str, s: str, a: dict, q: dict) -> Any:
    async with h.store.tx(s) as c:
        return await hs.outbox_queued(c, who, list(a.get("bots") or []))


async def r_claim(h: Hub, who: str, s: str, a: dict, q: dict, oid: str) -> Any:
    async with h.store.tx(s) as c:
        return await hs.outbox_claim(c, int(oid), _int(a.get("attempts", 1), "attempts"))


async def r_set(h: Hub, who: str, s: str, a: dict, q: dict, oid: str) -> Any:
    async with h.store.tx(s) as c:
        return await hs.outbox_set(c, int(oid), a.get("fields") or {})


async def r_fail(h: Hub, who: str, s: str, a: dict, q: dict, oid: str) -> Any:
    async with h.store.tx(s) as c:
        return await hs.outbox_fail(c, int(oid), a.get("fields") or {}, bool(a.get("partial")))


async def r_interrupted(h: Hub, who: str, s: str, a: dict, q: dict) -> Any:
    async with h.store.tx(s) as c:
        return await hs.outbox_interrupted(c, list(a.get("bots") or []))


async def r_dupes(h: Hub, who: str, s: str, a: dict, q: dict) -> Any:
    async with h.store.tx(s) as c:
        return await hs.outbox_dupes(c, a)


async def r_consumers(h: Hub, who: str, s: str, a: dict, q: dict) -> Any:
    async with h.store.tx(s) as c:
        return await hs.consumers(c)


async def r_ap_request(h: Hub, who: str, s: str, a: dict, q: dict) -> Any:
    async with h.store.tx(s) as c:
        return await hs.once(c, a.get("rid"), who, "approval", lambda: hs.approval_request(c, a))


async def r_ap_list(h: Hub, who: str, s: str, a: dict, q: dict) -> Any:
    async with h.store.tx(s) as c:
        return await hs.approval_list(c, q.get("every") in ("1", "true"))


async def r_ap_audit(h: Hub, who: str, s: str, a: dict, q: dict) -> Any:
    async with h.store.tx(s) as c:
        return await hs.approval_audit(c)


async def r_ap_expire(h: Hub, who: str, s: str, a: dict, q: dict) -> Any:
    async with h.store.tx(s) as c:
        return await hs.approval_expire(c, a)


async def r_ap_get(h: Hub, who: str, s: str, a: dict, q: dict, nonce: str) -> Any:
    async with h.store.tx(s) as c:
        r = await hs.approval_get(c, nonce)
    if r is None:
        raise NotFound("no such approval")
    return r


def _ap(fn_name: str, op: str | None = None):
    async def r(h: Hub, who: str, s: str, a: dict, q: dict, nonce: str) -> Any:
        fn = getattr(hs, fn_name)
        async with h.store.tx(s) as c:
            if op:
                return await hs.once(c, a.get("rid"), who, op, lambda: fn(c, nonce, a))
            return await fn(c, nonce, a)

    return r


async def r_import(h: Hub, who: str, s: str, a: dict, q: dict) -> Any:
    async with h.store.tx(s) as c:
        return await hs.import_rows(c, a)


async def r_dump(h: Hub, who: str, s: str, a: dict, q: dict) -> Any:
    async with h.store.tx(s) as c:
        return await hs.dump(c, q.get("table", ""), q.get("after"), _int(q.get("limit", 1000), "limit"))


async def r_counts(h: Hub, who: str, s: str, a: dict, q: dict) -> Any:
    async with h.store.tx(s) as c:
        return await hs.counts(c)


async def r_drop(h: Hub, who: str, s: str, a: dict, q: dict) -> Any:
    return {"dropped": await h.store.drop_schema(str(a.get("schema") or ""))}


async def r_restart(h: Hub, who: str, s: str, a: dict, q: dict) -> Any:
    print(json.dumps({"event": "restart_requested", "caller": who}), flush=True)
    asyncio.get_running_loop().call_later(0.2, h.stopping.set)
    return {"restarting": True}


N = r"([0-9a-f]{32})"
ROUTES: list[tuple[str, str, Any, frozenset[str]]] = [
    ("POST", r"/events/emit", r_emit, BRAIN),
    ("POST", r"/events/update", r_update, BRAIN),
    ("GET", r"/events/(\d{1,18})", r_event, BOTH),
    ("POST", r"/ledger/snapshot", r_snapshot, BRAIN),
    ("POST", r"/ledger/apply", r_apply, BRAIN),
    ("POST", r"/outbox", r_enqueue, BOTH),
    ("GET", r"/outbox/consumers", r_consumers, BOTH),
    ("GET", r"/outbox/(\d{1,18})", r_outbox, BOTH),
    ("POST", r"/outbox/(\d{1,18})/withdraw", r_withdraw, BRAIN),
    ("POST", r"/outbox/queued", r_queued, TG),
    ("POST", r"/outbox/(\d{1,18})/claim", r_claim, TG),
    ("POST", r"/outbox/(\d{1,18})/set", r_set, TG),
    ("POST", r"/outbox/(\d{1,18})/fail", r_fail, TG),
    ("POST", r"/outbox/interrupted", r_interrupted, TG),
    ("POST", r"/outbox/dupes", r_dupes, TG),
    ("POST", r"/approvals", r_ap_request, BRAIN),
    ("GET", r"/approvals", r_ap_list, BOTH),
    ("GET", r"/approvals/audit", r_ap_audit, BOTH),
    ("POST", r"/approvals/expire", r_ap_expire, TG),
    ("GET", rf"/approvals/{N}", r_ap_get, BOTH),
    ("POST", rf"/approvals/{N}/attach", _ap("approval_attach"), BOTH),
    ("POST", rf"/approvals/{N}/consume", _ap("approval_consume", "consume"), BRAIN),
    ("POST", rf"/approvals/{N}/cancel", _ap("approval_cancel"), BRAIN),
    ("POST", rf"/approvals/{N}/decide", _ap("approval_decide", "decide"), TG),
    ("POST", rf"/approvals/{N}/card", _ap("approval_card"), TG),
    ("POST", r"/admin/import", r_import, BRAIN),
    ("GET", r"/admin/dump", r_dump, BRAIN),
    ("GET", r"/admin/counts", r_counts, BRAIN),
    ("POST", r"/admin/drop", r_drop, BRAIN),
    ("POST", r"/admin/restart", r_restart, BRAIN),
]
if htg is not None:  # the Telegram transport
    ROUTES += htg.routes(BRAIN, TG, BOTH)
if hvoice is not None:  # the voice relay's hand-offs and call results
    ROUTES += hvoice.routes(BRAIN, VOICE)
if hphone is not None:  # what his app hands the brain (taps, chat, shares, notes, zones, usage)
    ROUTES += hphone.routes(BRAIN, PHONE)
if hbrowser is not None:  # the browser container's service checks his approval and claims the receipt
    ROUTES += hbrowser.routes(BROWSER)
if hboard is not None:  # the board's records (the brain's `board` CLI writes, TODO.md renders them)
    ROUTES += hboard.routes(BRAIN)
if hci is not None:  # the CI runner's jobs and signed records, the protected-set check
    ROUTES += [
        ("POST", r"/ci/jobs", hci.r_submit, BRAIN),
        ("GET", r"/ci/jobs/([0-9a-f]{16})", hci.r_job, BRAIN),
        ("GET", r"/ci/records", hci.r_records, BRAIN),
        ("POST", r"/ci/verify", hci.r_verify, BRAIN),
        ("GET", r"/ci/slots", hci.r_slots_get, BRAIN),
        ("POST", r"/ci/slots", hci.r_slots_set, BRAIN),
        ("GET", r"/protected/([0-9a-f]{64})", hci.r_protected, BRAIN),
    ]


async def verify_release(hub: Hub) -> bool:
    """hubverify before serving: a protected set nobody accepted (his ✅, the console line, the first baseline)
    stops this release (exit 3: the loader marks the attempt bad and runs the last good release)."""

    async def _lookup(sha: str) -> str | None:
        return await hci.approved_nonce(hub, sha)

    lookup = _lookup if hub.store is not None and hci is not None else None
    rel_dir = os.environ.get("HUB_RELEASE_DIR") or os.path.dirname(_HERE)
    ok, info = await hubverify.gate(rel_dir, state_dir(), lookup, RELEASE)
    print(
        json.dumps(
            {
                "event": "protected_ok" if ok else "protected_refused",
                "release": RELEASE,
                "digest": info["digest"][:16],
                "how": info["how"],
                "files": info["files"],
            }
        ),
        flush=True,
    )
    return ok


class ApprovalsPolicy:
    """Cedar over the approval API, log-only. Never blocks a call and never fails one: an engine that is
    missing or broken only counts as an error. The gateway's engine when the gateway runs, else its own."""

    KIND = {"brain": "brain", "tg": "binding", "ci": "ci"}
    LOG_MAX = 2 << 20

    def __init__(self) -> None:
        self.engine: Any = None
        self.error = ""
        self.n = {"allow": 0, "deny": 0, "errors": 0}
        self.last_deny: dict[str, Any] | None = None
        state = os.environ.get("HUB_STATE") or os.path.dirname(os.environ.get("HUB_LOADER_STATE", "")) or "/var/lib/hub"
        self.log_path = os.path.join(state, "approvals-policy.jsonl")

    def load(self) -> None:
        try:
            if GW is not None:
                self.engine = GW.gw.engine
                return
            up = os.path.abspath(os.path.join(_HERE, ".."))
            if not os.path.isdir(os.path.join(_HERE, "tools")) and os.path.isdir(os.path.join(up, "tools")):
                sys.path.append(up)  # repo source (tests): packages/tools beside packages/hub
            import importlib

            core: Any = importlib.import_module("tools.core")
            pol: Any = importlib.import_module("tools.policy")
            self.engine = pol.Engine(core.load())
        except Exception as e:
            self.engine, self.error = None, type(e).__name__
        print(json.dumps({"event": "approvals_policy", "engine": bool(self.engine), "error": self.error}), flush=True)

    def check(self, who: str, op: str, test: bool) -> None:
        try:
            import importlib

            pol: Any = importlib.import_module("tools.policy")
            kind = self.KIND.get(who)
            if self.engine is None or kind is None:
                self.n["errors"] += 1
                return
            d = self.engine.decide_api(pol.Principal(kind), op)
            dec = "allow" if d.allowed else "deny"
            self.n[dec] += 1
            self.n["errors"] += 1 if d.errors else 0
            row = {"ts": round(time.time(), 3), "op": op, "caller": who, "principal": kind, "decision": dec,
                   "policies": list(d.policies), "mode": "log", "test": test}  # fmt: skip
            if not d.allowed:
                self.last_deny = {k: row[k] for k in ("ts", "op", "caller", "principal", "policies")}
            self._append(row)
        except Exception:
            self.n["errors"] += 1

    def _append(self, row: dict[str, Any]) -> None:
        try:
            if os.path.exists(self.log_path) and os.path.getsize(self.log_path) > self.LOG_MAX:
                with open(self.log_path, "rb") as f:
                    keep = f.read()[-(self.LOG_MAX // 2) :]
                keep = keep[keep.find(b"\n") + 1 :]
                with open(self.log_path + ".tmp", "wb") as f:
                    f.write(keep)
                os.replace(self.log_path + ".tmp", self.log_path)
            fd = os.open(self.log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, separators=(",", ":")) + "\n")
        except OSError:
            self.n["errors"] += 1

    def brief(self) -> dict[str, Any]:
        return {"mode": "log", "engine": bool(self.engine), "error": self.error or None,
                "sha": getattr(self.engine, "sha", None), **self.n, "last_deny": self.last_deny}  # fmt: skip


APPOL = ApprovalsPolicy()


def ap_op(method: str, rx: str) -> str | None:
    """The approval API action of a route (None: not an approval route)."""
    if not rx.startswith("/approvals"):
        return None
    tail = rx.rsplit("/", 1)[-1]
    if tail in ("attach", "consume", "cancel", "decide", "card", "expire"):
        return tail
    return "request" if (method, rx) == ("POST", "/approvals") else "read"


async def start_gateway() -> None:
    """The gateway in this loop; never fatal for hubd."""
    global GW, GW_ERROR
    port = os.environ.get("HUB_GW_PORT", "8701")
    if port == "off":
        GW_ERROR = "off"
        return
    try:
        import importlib

        hubgw: Any = importlib.import_module("tools.hubgw")
        GW = await hubgw.start(TOKENS, port=int(port), bind=os.environ.get("HUB_BIND", "0.0.0.0"), redact=redact_fn())
        print(json.dumps({"event": "gateway_start", "port": GW.port, "tools": len(GW.gw.reg.typed),
                          "policy": GW.gw.engine.sha, "valid": not GW.gw.invalid}), flush=True)  # fmt: skip
    except Exception as e:  # the API keeps serving; /health says why
        GW_ERROR = f"{type(e).__name__}"
        print(json.dumps({"event": "gateway_failed", "error": GW_ERROR}), flush=True)


async def load_approvals_policy(gw_task: asyncio.Task) -> None:
    """After the gateway started (or did not): its engine, else a hubd-own one, off the event loop."""
    try:
        await gw_task
    except Exception:
        pass
    await asyncio.get_running_loop().run_in_executor(None, APPOL.load)


async def start_feed(hub: Hub) -> asyncio.Task | None:
    """The status feed: its refresh loop and its read-only listener (HUB_FEED_PORT); never fatal for hubd."""
    global FEED
    port = os.environ.get("HUB_FEED_PORT", "8702")
    if port == "off":
        return None

    async def query_one(q: str, params: dict[str, Any]) -> dict[str, Any] | None:
        if hub.store is None:
            return None
        async with hub.store.tx(hs.LIVE) as c:
            return await hs.one(c, q, params)

    try:
        f = hubfeed.Feed(
            probes={
                "pg": _pg_tcp,
                "phoenix": _phoenix,
                "ci": lambda: _ci()[0],
                "tg": lambda: hubfeed.loader_probe(tg_loader_seen()),
                "voice": lambda: _seen_probe(hvoice),
                "phone": lambda: _seen_probe(hphone),
                "browser": _browser_probe,
            },
            query_one=query_one if hs is not None else None,
            gateway=lambda: GW,
            release=RELEASE,
        )
        got = await f.serve(os.environ.get("HUB_BIND", "0.0.0.0"), int(port))
        FEED = f
        print(json.dumps({"event": "feed_start", "port": got}), flush=True)
        return asyncio.create_task(f.run(hub.stopping))
    except Exception as e:
        print(json.dumps({"event": "feed_failed", "error": type(e).__name__}), flush=True)
        return None


def start_workflows(hub: Hub) -> asyncio.Task:
    """The workflow engine: hooks its steps call on this loop (hub approvals read, ledger emit, the gateway), the switch
    watcher. Never fatal for hubd: a failed launch shows on /health and is retried."""
    global WF

    async def approval_state(nonce: str) -> str | None:
        if hub.store is None or hs is None:
            return None
        async with hub.store.tx(hs.LIVE) as c:
            r = await hs.approval_get(c, nonce)
        return r.get("state") if r else None

    async def emit(row: dict[str, Any]) -> dict[str, Any]:
        if hub.store is None or hs is None:
            raise RuntimeError("no store")
        async with hub.store.tx(hs.LIVE) as c:
            return await hs.emit(c, row)

    async def gateway(
        name: str, args: dict[str, Any], timeout_s: float | None = None, meta: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if GW is None:
            return {"ok": False, "code": "unavailable"}
        kw: dict[str, Any] = {"meta": meta} if meta else {}
        if timeout_s is None:
            return await GW.gw.call(name, args, caller="hub", **kw)
        return await GW.gw.call(name, args, caller="hub", timeout_s=timeout_s, **kw)

    WF = hubwfapi.Engine(state_dir(), hub.conninfo, {"approval_state": approval_state, "emit": emit,
                                                     "gateway": gateway})  # fmt: skip
    return asyncio.create_task(WF.run(hub.stopping))


async def amain() -> int:
    hub = Hub()
    await hub.start()
    if not await verify_release(hub):
        if hub.store is not None:
            await hub.store.close()
        return 3
    port = int(os.environ.get("HUB_PORT", "8700"))
    srv = await asyncio.start_server(hub.handle, os.environ.get("HUB_BIND", "0.0.0.0"), port, reuse_address=True)
    gw_task = asyncio.create_task(start_gateway())
    ap_task = asyncio.create_task(load_approvals_policy(gw_task))  # noqa: F841  (kept referenced till amain ends)
    feed_task = await start_feed(hub)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, hub.stopping.set)
    print(
        json.dumps(
            {
                "event": "hubd_start",
                "release": RELEASE,
                "port": port,
                "callers": len(TOKENS),
                "driver": "ok" if hs else DRIVER_ERROR,
            }
        ),
        flush=True,
    )
    bg = asyncio.create_task(hci.collector(hub, hub.stopping)) if hci is not None else None
    tg_bg = asyncio.create_task(htg.housekeeping(hub, hub.stopping)) if htg is not None else None
    wf_task = start_workflows(hub)
    async with srv:
        await hub.stopping.wait()
    if bg is not None:
        await bg
    if tg_bg is not None:
        await tg_bg
    try:
        await asyncio.wait_for(wf_task, 15)
    except Exception:
        pass
    srv.close()
    gw_task.cancel()
    if FEED is not None:
        await FEED.close()
    if feed_task is not None:
        feed_task.cancel()
    if GW is not None:
        await GW.stop()
    if hub.store is not None:
        await hub.store.close()
    print(json.dumps({"event": "hubd_stop", "release": RELEASE}), flush=True)
    return 0


def main() -> int:
    rc = asyncio.run(amain())
    if WF is not None and WF.launched:  # DBOS's executor threads may sit in a recv: the state is in Postgres, end now
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
