"""hubfeed: the hub's read-only status feed. Stdlib only; runs in hubd's event loop.

One snapshot of states, counts, ids and timings (never message text, names, summaries or secrets), recomputed in the
background and pushed to every stream listener whenever it changes:
  parts      the parts registry PARTS (id = the dashboard map's data-part id): containers, adapters, the gateway and
             its tool domains, each with a state (STATES), a short detail word and since (epoch of the last change);
             plus brain-registered watch parts (a TCP probe with a TTL: the done-when's harmless test target)
  gateway    the gateway: state, tools, executor, enforced domains; calls / errors / denies for 1 h and 24 h,
             p95 ms (24 h) and per domain (24 h)
  approvals  the hub store: pending, decided in 24 h; `store` = which store is live (from the brain's report);
             live_pending = the live store's pending count (the brain's `approvals list` count while that store is
             the in-container one and its report is fresh, else the hub's)
  tg         the hub outbox by status, deliveries and receipts (24 h), consumers seen; `brain` = the brain's report of
             its in-container transport (sent / failed / duplicates / receipts 24 h, p95 s) with its time
  events     open ledger events (new + listed) and new
  traces     Phoenix: traces 1 h / 24 h, spans 24 h, error spans 24 h, p95 trace latency 24 h (ms)
  ci         the CI runner, from the spool hubci shares with it: slots (its heartbeat), set (the brain's slot count or
             null), busy (jobs running), queued (jobs waiting), heartbeat age (s), toolchain ok; counts only

Listener (HUB_FEED_PORT, default 8702, "off" = none; not published: the brain's ssh reverse tunnel carries it to
the web VM, where nginx serves it behind the dashboard's login as dash.<domain>/hub/ -> /feed/):
  GET /feed | /feed/            the snapshot (JSON)
  GET /feed/stream              Server-Sent Events: "retry", then event "feed" (id = seq, data = the snapshot) on every
                                change; ": ping" every PING_S. At most MAX_CLIENTS at once (503 beyond).
  GET /feed/health              {"ok", "seq", "clients", "at"}
Nothing else is served there (404; non-GET 405). No token is read or accepted on that port.

Brain-token routes on hubd's API (hubd dispatches /feed* here after its token check):
  GET  /feed                    the snapshot (brain or tg)
  POST /feed/brain              the brain's report {stores?, tg?, approvals?: {pending}, parts?, ttl?} (numbers, words,
                                ids)
  POST /feed/watch              {"id", "tcp": "host:port", "label"?, "ttl"? (30..3600)} | {"id", "remove": true}

Env: HUB_FEED_PORT, HUB_FEED_PROBE_S (parts, default 5), HUB_FEED_STATS_S (10), HUB_FEED_TRACES_S (60),
HUB_FEED_PROJECT (Phoenix project, default "brain"; "*" = every project), PHOENIX_URL, HUB_SPOOL, HUB_BIND; hubd's
HUB_FEED_BROWSER (claude-hub-browser's probe target, default claude-hub-browser:9223, "off" = none).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import time
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

VERSION = 1
STATES = ("ok", "warn", "down", "off", "planned", "unknown")
PING_S = 15.0
MAX_CLIENTS = 16
MAX_WATCH = 8
WATCH_TTL = (30, 3600)
BRAIN_TTL = (30, 3600)
FAULTS = ("unavailable", "timeout", "internal")  # gateway codes that say a domain is broken (not the caller's input)
FAULT_WINDOW = 900.0
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
HOST_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,62}$")
WORD_RE = re.compile(r"^[A-Za-z0-9 .:/_+-]{0,40}$")
STORES = ("events", "outbox", "approvals")
BRAIN_TG = ("sent_24h", "failed_24h", "dupes_24h", "send_p95_s", "receipts_24h", "receipt_p95_s", "dropped_24h")


@dataclass(frozen=True)
class Part:
    id: str  # the map's data-part id
    label: str
    kind: str  # container | adapter | service | domain | watch
    probe: str  # self | pg | phoenix | ci | tg | voice | browser | phone | planned | gateway | brain | domain:<name>


# The registry. Containers of the claude-hub compose project, the gateway, the adapters; the gateway's tool domains
# are added at run time from its registry (domain:<name>). The adapter containers (claude-hub-tg, -voice, -phone) are
# read from their loader's last report to hubd (POST /<name>/hold/seen every poll: loader_probe); claude-hub-browser
# has no such report, so its CDP service gets a TCP connect (hubd's "browser" probe).
PARTS: tuple[Part, ...] = (
    Part("claude-hub", "claude-hub", "container", "self"),
    Part("claude-hub-pg", "claude-hub-pg", "container", "pg"),
    Part("claude-hub-phoenix", "claude-hub-phoenix", "container", "phoenix"),
    Part("claude-hub-ci", "claude-hub-ci", "container", "ci"),
    Part("claude-hub-tg", "claude-hub-tg", "container", "tg"),
    Part("claude-hub-voice", "claude-hub-voice", "container", "voice"),
    Part("claude-hub-browser", "claude-hub-browser", "container", "browser"),
    Part("claude-hub-phone", "claude-hub-phone", "container", "phone"),
    Part("hub-gateway", "gateway", "service", "gateway"),
    Part("tg-adapter", "Telegram adapter", "adapter", "brain"),
)
PROBED = ("pg", "phoenix", "ci", "tg", "voice", "browser", "phone")  # the probes hubd injects (Feed's `probes`)
LOADER_FRESH = 60.0  # a loader reports every poll (5 s); silent this long = its container is gone or stuck

COUNTS_SQL = (
    "SELECT"
    " (SELECT count(*) FROM approvals WHERE status = 'pending' AND expires > %(now)s) AS ap_pending,"
    " (SELECT count(*) FROM approvals WHERE decided_ts > %(d1)s) AS ap_decided_24h,"
    " (SELECT count(*) FROM outbox WHERE status = 'queued') AS ob_queued,"
    " (SELECT count(*) FROM outbox WHERE status = 'sending') AS ob_sending,"
    " (SELECT count(*) FROM outbox WHERE status = 'failed' AND ts > %(d1)s) AS ob_failed_24h,"
    " (SELECT count(*) FROM outbox WHERE status = 'sent' AND sent_ts > %(d1)s) AS ob_sent_24h,"
    " (SELECT count(*) FROM deliveries WHERE sent_ts > %(d1)s) AS dl_sent_24h,"
    " (SELECT count(*) FROM deliveries WHERE receipt_ts > %(d1)s) AS dl_receipts_24h,"
    " (SELECT count(*) FROM consumers WHERE seen > %(c)s) AS consumers,"
    " (SELECT count(*) FROM events WHERE status IN ('new', 'listed')) AS ev_open,"
    " (SELECT count(*) FROM events WHERE status = 'new') AS ev_new"
)

TRACES_GQL = (
    "query($s1: DateTime!, $s24: DateTime!, $e: DateTime!) { projects { edges { node { name"
    " t1: traceCount(timeRange: {start: $s1, end: $e}) t24: traceCount(timeRange: {start: $s24, end: $e})"
    " s24: recordCount(timeRange: {start: $s24, end: $e})"
    " e24: recordCount(timeRange: {start: $s24, end: $e}, filterCondition: \"status_code == 'ERROR'\")"
    " p95: latencyMsQuantile(probability: 0.95, timeRange: {start: $s24, end: $e}) } } } }"
)


class BadInput(ValueError):
    pass


def _env_f(name: str, default: float, lo: float) -> float:
    try:
        return max(lo, float(os.environ.get(name, default)))
    except ValueError:
        return default


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _int(v: Any) -> int | None:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _pct(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    return round(s[min(len(s) - 1, int(p * len(s)))], 1)


def tcp_probe(host: str, port: int, timeout: float = 2.0) -> str:
    """ok when a TCP connect succeeds; down on refusal, timeout or a name that no longer resolves (a stopped
    container leaves the docker DNS)."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return "ok"
    except OSError:
        return "down"


def loader_probe(seen: dict[str, Any] | None, fresh: float = LOADER_FRESH) -> str:
    """An adapter container's word from its loader's last report to hubd ({state, release, why, ts, age_s}, memory
    only): ok while running and heard within `fresh` s; held (the brain's stop), starting, idle, down or conflict as
    reported; "silent" when the last report is older; "no report" before the first one (a hubd restart forgets)."""
    if not isinstance(seen, dict) or not seen:
        return "no report"
    age = seen.get("age_s")
    if isinstance(age, bool) or not isinstance(age, (int, float)) or age >= fresh:
        return "silent"
    st = seen.get("state")
    if st == "running":
        return "ok"
    return st if st in ("held", "starting", "idle", "down", "conflict") else "unknown"


def probe_state(v: str) -> str:
    """A probe's word -> a part state (STATES)."""
    if v == "ok":
        return "ok"
    if v in ("unset", "unknown", "no report"):
        return "unknown"
    if v in ("stale", "starting", "idle"):
        return "warn"
    if v == "held":
        return "off"
    return "down"


def phoenix_traces(url: str, project: str, now: float, timeout: float = 5.0) -> dict[str, Any]:
    body = json.dumps(
        {"query": TRACES_GQL, "variables": {"s1": _iso(now - 3600), "s24": _iso(now - 86400), "e": _iso(now + 60)}}
    ).encode()
    req = urllib.request.Request(
        url.rstrip("/") + "/graphql", data=body, method="POST", headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.load(r)
    edges = (((d or {}).get("data") or {}).get("projects") or {}).get("edges") or []
    nodes = [e.get("node") or {} for e in edges if isinstance(e, dict)]
    if project != "*":
        nodes = [n for n in nodes if n.get("name") == project]
    if not nodes:
        return {"state": "unknown", "why": "no such project"}
    out: dict[str, Any] = {"state": "ok"}
    for k, name in (("t1", "traces_1h"), ("t24", "traces_24h"), ("s24", "spans_24h"), ("e24", "error_spans_24h")):
        out[name] = sum(_int(n.get(k)) or 0 for n in nodes)
    p95 = [float(n["p95"]) for n in nodes if isinstance(n.get("p95"), (int, float))]
    out["p95_ms_24h"] = round(max(p95)) if p95 else None
    return out


def ci_runner(spool: str, now: float) -> dict[str, Any]:
    """The CI runner's slots and queue (the ci block): its heartbeat, the brain's slot setting and the job files in the
    spool's jobs/ (waiting) and run/ (running). State: ok with a heartbeat under 60 s, warn when older, unknown when
    there is none."""

    def load(*p: str) -> dict[str, Any]:
        try:
            with open(os.path.join(spool, *p), encoding="utf-8") as f:
                v = json.load(f)
        except (OSError, ValueError):
            return {}
        return v if isinstance(v, dict) else {}

    def count(d: str) -> int | None:
        try:
            return sum(1 for n in os.listdir(os.path.join(spool, "ci", d)) if n.endswith(".json"))
        except OSError:
            return None

    hb = load("ci-heartbeat.json")
    ts = hb.get("ts") if isinstance(hb.get("ts"), (int, float)) else None
    age = round(max(0.0, now - ts), 1) if ts is not None else None
    jobs = hb.get("jobs")
    running = count("run")
    return {
        "state": "unknown" if age is None else ("ok" if age < 60 else "warn"),
        "slots": _int(hb.get("slots")),
        "set": _int(load("ci", "slots.json").get("slots")),
        "busy": running if running is not None else (len(jobs) if isinstance(jobs, list) else 0),
        "queued": count("jobs"),
        "heartbeat_s": age,
        "toolchain_ok": hb.get("toolchain_ok") if isinstance(hb.get("toolchain_ok"), bool) else None,
    }


def gateway_stats(db: Any, now: float) -> dict[str, Any]:
    """Counts from the gateway's decision log (sqlite3 connection, the loop's thread). Test calls are left out."""
    d1, h1, f0 = now - 86400, now - 3600, now - FAULT_WINDOW
    marks = ",".join("?" * len(FAULTS))
    q = (
        "SELECT domain, count(*), sum(code != 'ok'), sum(decision = 'deny'), sum(ts > ?), sum(ts > ? AND code != 'ok'),"
        f" sum(ts > ? AND decision = 'deny'), sum(ts > ? AND code IN ({marks})) FROM calls WHERE ts > ? AND test = 0"
        " GROUP BY domain"
    )
    doms: dict[str, Any] = {}
    tot: dict[str, int] = dict.fromkeys(
        ("calls_1h", "errors_1h", "denies_1h", "calls_24h", "errors_24h", "denies_24h"), 0
    )
    for dom, n, err, den, n1, e1, dn1, faults in db.execute(q, (h1, h1, h1, f0, *FAULTS, d1)).fetchall():
        doms[str(dom or "?")] = {"calls_24h": n, "errors_24h": err or 0, "faults_15m": faults or 0}
        for k, v in (("calls_24h", n), ("errors_24h", err), ("denies_24h", den), ("calls_1h", n1),
                     ("errors_1h", e1), ("denies_1h", dn1)):  # fmt: skip
            tot[k] += v or 0
    ms = [r[0] for r in db.execute("SELECT ms_total FROM calls WHERE ts > ? AND test = 0", (d1,)) if r[0] is not None]
    return {**tot, "p95_ms_24h": _pct(ms, 0.95), "domains": dict(sorted(doms.items()))}


class Feed:
    """The feed. Sources are injected by hubd (so this module needs neither the driver nor the gateway):
    probes        {"pg"|"phoenix"|"ci"|"tg"|"voice"|"browser"|"phone": fn} -> "ok" | other word (sync, run in the
                  executor; probe_state maps the word)
    query_one     async (sql, params) -> row dict (the live schema), or None without a store
    gateway       () -> tools.hubgw.Running | None
    release       the running release id"""

    def __init__(
        self,
        *,
        probes: dict[str, Callable[[], str]],
        query_one: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any] | None]] | None,
        gateway: Callable[[], Any],
        release: str = "",
        phoenix_url: str | None = None,
    ) -> None:
        self.probes, self.query_one, self.gateway, self.release = probes, query_one, gateway, release
        self.phoenix_url = os.environ.get("PHOENIX_URL", "") if phoenix_url is None else phoenix_url
        self.project = os.environ.get("HUB_FEED_PROJECT", "brain") or "brain"
        self.probe_s = _env_f("HUB_FEED_PROBE_S", 5.0, 0.2)
        self.stats_s = _env_f("HUB_FEED_STATS_S", 10.0, 0.2)
        self.traces_s = _env_f("HUB_FEED_TRACES_S", 60.0, 1.0)
        self.spool = os.environ.get("HUB_SPOOL", "/var/spool/hub")
        self.parts: dict[str, dict[str, Any]] = {}
        self.watch: dict[str, dict[str, Any]] = {}
        self.brain: dict[str, Any] | None = None
        self.blocks: dict[str, Any] = {"gateway": {"state": "unknown"}, "approvals": {}, "tg": {}, "events": {},
                                       "traces": {"state": "unknown"}, "ci": {"state": "unknown"}}  # fmt: skip
        self.seq = 0
        self.at = 0.0
        self.snap: dict[str, Any] = {}
        self._sig = ""
        self.clients: set[asyncio.Queue[str]] = set()
        self.server: asyncio.AbstractServer | None = None
        self.port: int | None = None
        self.errors: dict[str, str] = {}

    # ------------------------------------------------------------------------------------------ state

    def _set(self, pid: str, part: Part, state: str, detail: str = "") -> None:
        cur = self.parts.get(pid)
        if cur is None or cur["state"] != state:
            since = time.time()
        else:
            since = cur["since"]
        self.parts[pid] = {"id": pid, "label": part.label, "kind": part.kind, "state": state,
                           "detail": detail[:40], "since": round(since, 1)}  # fmt: skip

    def _gw(self) -> Any:
        try:
            return self.gateway()
        except Exception:
            return None

    async def probe_parts(self) -> None:
        loop = asyncio.get_running_loop()
        names = [k for k in PROBED if k in self.probes]
        got = await asyncio.gather(*(loop.run_in_executor(None, self.probes[k]) for k in names), return_exceptions=True)
        res = {k: (v if isinstance(v, str) else "unknown") for k, v in zip(names, got, strict=True)}
        wparts = list(self.watch.items())
        wgot = await asyncio.gather(
            *(loop.run_in_executor(None, tcp_probe, w["host"], w["port"]) for _, w in wparts), return_exceptions=True
        )
        gw = self._gw()
        brief: dict[str, Any] = {}
        if gw is not None:
            try:
                brief = gw.brief()
            except Exception as e:
                brief = {"state": "error", "why": type(e).__name__}
        now = time.time()
        for p in PARTS:
            if p.probe == "self":
                self._set(p.id, p, "ok", self.release[:12])
            elif p.probe in res:
                v = res[p.probe]
                self._set(p.id, p, probe_state(v), v)
            elif p.probe in PROBED:  # hubd injects no such probe (a test, an older hubd)
                self._set(p.id, p, "unknown", "no probe")
            elif p.probe == "planned":
                self._set(p.id, p, "planned", "planned")
            elif p.probe == "gateway":
                gs = brief.get("state") if gw is not None else "down"
                st = "ok" if gs == "ok" else "off" if gs == "off" else "warn" if gs == "policy invalid" else "down"
                self._set(p.id, p, st, str(gs or "down"))
            elif p.probe == "brain":
                bp = ((self.brain or {}).get("parts") or {}).get(p.id)
                fresh = self.brain is not None and now - self.brain["at"] < self.brain["ttl"]
                if bp and fresh:
                    self._set(p.id, p, bp["state"], bp.get("detail", ""))
                else:
                    self._set(p.id, p, "unknown", "no report" if not self.brain else "report stale")
        # the gateway's tool domains, from its registry
        doms: list[str] = []
        if gw is not None:
            try:
                doms = sorted({str(s.domain) for s in gw.gw.reg.typed.values()})
            except Exception:
                doms = []
        gstats = (self.blocks.get("gateway") or {}).get("domains") or {}
        gw_ok = self.parts.get("hub-gateway", {}).get("state") == "ok"
        live_doms = set()
        for d in doms:
            pid = f"domain-{d}"
            live_doms.add(pid)
            faults = (gstats.get(d) or {}).get("faults_15m") or 0
            st = ("warn" if faults else "ok") if gw_ok else "down"
            self._set(pid, Part(pid, d, "domain", f"domain:{d}"), st, f"{faults} faults 15m" if faults else "")
        for pid in [k for k, v in self.parts.items() if v["kind"] == "domain" and k not in live_doms]:
            del self.parts[pid]
        # watch parts
        mono = time.monotonic()
        for (wid, w), v in zip(wparts, wgot, strict=True):
            if mono > w["until"]:
                self.watch.pop(wid, None)
                continue
            st = v if isinstance(v, str) else "unknown"
            self._set(wid, Part(wid, w["label"], "watch", "tcp"), st, "tcp")
        for pid in [k for k, v in self.parts.items() if v["kind"] == "watch" and k not in self.watch]:
            del self.parts[pid]

    async def stats(self) -> None:
        now = time.time()
        gw = self._gw()
        g: dict[str, Any] = {"state": self.parts.get("hub-gateway", {}).get("state", "unknown")}
        if gw is not None:
            try:
                h = gw.gw.health()
                g.update(tools=h.get("tools"), executor=bool((h.get("executor") or {}).get("online")),
                         enforce=list((h.get("mode") or {}).get("enforce") or []))  # fmt: skip
                g.update(gateway_stats(gw.gw.store.db, now))
            except Exception as e:
                self.errors["gateway"] = type(e).__name__
        self.blocks["gateway"] = g
        row = None
        if self.query_one is not None:
            try:
                row = await asyncio.wait_for(
                    self.query_one(COUNTS_SQL, {"now": now, "d1": now - 86400, "c": now - 120}), 5
                )
                self.errors.pop("store", None)
            except Exception as e:
                self.errors["store"] = type(e).__name__
        r = {k: _int(v) for k, v in (row or {}).items()}
        hub_ok = row is not None
        self.blocks["approvals"] = {
            "state": "ok" if hub_ok else "unknown",
            "pending": r.get("ap_pending"),
            "decided_24h": r.get("ap_decided_24h"),
        }
        self.blocks["tg"] = {
            "state": "ok" if hub_ok else "unknown",
            "hub": {k[3:]: r.get(k) for k in ("ob_queued", "ob_sending", "ob_failed_24h", "ob_sent_24h")}
            | {
                "deliveries_24h": r.get("dl_sent_24h"),
                "receipts_24h": r.get("dl_receipts_24h"),
                "consumers": r.get("consumers"),
            },  # fmt: skip
        }
        self.blocks["events"] = {"open": r.get("ev_open"), "new": r.get("ev_new")}
        try:
            self.blocks["ci"] = ci_runner(self.spool, now)
            self.errors.pop("ci", None)
        except Exception as e:
            self.errors["ci"] = type(e).__name__

    async def traces(self) -> None:
        if not self.phoenix_url:
            self.blocks["traces"] = {"state": "unknown", "why": "unset"}
            return
        loop = asyncio.get_running_loop()
        try:
            t = await loop.run_in_executor(None, phoenix_traces, self.phoenix_url, self.project, time.time())
        except Exception as e:
            t = {"state": "down", "why": type(e).__name__}
        self.blocks["traces"] = t

    def compose(self) -> dict[str, Any]:
        """The snapshot: the hub's own blocks with the brain's report laid over them (which store is live per block,
        and its in-container Telegram transport)."""
        b = self.brain
        stores = (b or {}).get("stores") or {}
        btg = dict((b or {}).get("tg") or {})
        if btg and b:
            btg["at"] = round(b["at"], 1)
            btg["fresh"] = time.time() - b["at"] < b["ttl"]
        blocks = {k: dict(v) for k, v in self.blocks.items()}
        blocks["approvals"]["store"] = stores.get("approvals")
        fresh = bool(b) and time.time() - b["at"] < b["ttl"]
        bpend = (b or {}).get("approvals_pending")
        if stores.get("approvals") == "sqlite":
            blocks["approvals"]["live_pending"] = bpend if fresh else None
        else:
            blocks["approvals"]["live_pending"] = blocks["approvals"].get("pending")
        blocks["tg"].update(store=stores.get("outbox"), brain=btg or None)
        if b and b.get("slo") is not None:  # the brain's SLO tiles, while fresh
            blocks["slo"] = {"tiles": b["slo"], "at": round(b["at"], 1), "fresh": fresh}
        blocks["events"]["store"] = stores.get("events")
        return {
            "v": VERSION,
            "release": self.release[:12],
            "probe_s": self.probe_s,
            "parts": sorted(self.parts.values(), key=lambda p: (p["kind"] != "container", p["kind"], p["id"])),
            **blocks,
            "brain": {"at": round(b["at"], 1), "ttl": b["ttl"]} if b else None,
            "errors": dict(sorted(self.errors.items())),
        }

    def publish(self) -> bool:
        """Re-compose; a changed snapshot gets the next seq and goes to every listener. True when it changed."""
        body = self.compose()
        sig = json.dumps(body, sort_keys=True, separators=(",", ":"))
        self.at = time.time()
        if sig == self._sig:
            self.snap["at"] = round(self.at, 1)
            return False
        self._sig = sig
        self.seq += 1
        self.snap = {"seq": self.seq, "at": round(self.at, 1), **body}
        frame = f"event: feed\nid: {self.seq}\ndata: {json.dumps(self.snap, separators=(',', ':'))}\n\n"
        for q in list(self.clients):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(frame)
        return True

    async def tick(self, now: float, last: dict[str, float]) -> None:
        if now - last.get("traces", -1e9) >= self.traces_s:
            last["traces"] = now
            await self.traces()
        if now - last.get("stats", -1e9) >= self.stats_s:
            last["stats"] = now
            await self.stats()
        await self.probe_parts()
        self.publish()

    async def run(self, stopping: asyncio.Event) -> None:
        last: dict[str, float] = {}
        while not stopping.is_set():
            try:
                await self.tick(time.monotonic(), last)
                self.errors.pop("loop", None)
            except Exception as e:  # never let the loop die; the snapshot says why
                self.errors["loop"] = type(e).__name__
                print(json.dumps({"event": "feed_error", "error": type(e).__name__}), flush=True)
            try:
                await asyncio.wait_for(stopping.wait(), self.probe_s)
            except TimeoutError:
                pass

    # ------------------------------------------------------------------------------------------ brain-token API

    def brain_report(self, a: dict[str, Any]) -> dict[str, Any]:
        bad = set(a) - {"stores", "tg", "approvals", "parts", "ttl", "rid", "slo"}
        if bad:
            raise BadInput(f"unknown keys: {sorted(bad)}")
        stores = a.get("stores") or {}
        if not isinstance(stores, dict) or any(
            k not in STORES or v not in ("hub", "sqlite") for k, v in stores.items()
        ):
            raise BadInput(f"stores: {{{'|'.join(STORES)}: hub|sqlite}}")
        tg = a.get("tg") or {}
        if not isinstance(tg, dict) or set(tg) - set(BRAIN_TG):
            raise BadInput(f"tg: keys {BRAIN_TG}")
        for k, v in tg.items():
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v < 1e9):
                raise BadInput(f"tg.{k}: a number >= 0 or null")
        ap = a.get("approvals") or {}
        if not isinstance(ap, dict) or set(ap) - {"pending"}:
            raise BadInput("approvals: {pending}")
        pend = ap.get("pending")
        if pend is not None and (isinstance(pend, bool) or not isinstance(pend, int) or not 0 <= pend < 1_000_000):
            raise BadInput("approvals.pending: a count >= 0 or null")
        brain_ids = {p.id for p in PARTS if p.probe == "brain"}
        parts = a.get("parts") or {}
        if not isinstance(parts, dict):
            raise BadInput("parts: an object")
        clean: dict[str, dict[str, str]] = {}
        for pid, v in parts.items():
            if pid not in brain_ids:
                raise BadInput(f"parts: only {sorted(brain_ids)}")
            if not isinstance(v, dict) or v.get("state") not in STATES:
                raise BadInput(f"parts.{pid}.state: one of {STATES}")
            det = v.get("detail", "")
            if not isinstance(det, str) or not WORD_RE.match(det):
                raise BadInput(f"parts.{pid}.detail: up to 40 plain characters")
            clean[pid] = {"state": v["state"], "detail": det}
        ttl = a.get("ttl", 180)
        if isinstance(ttl, bool) or not isinstance(ttl, int) or not BRAIN_TTL[0] <= ttl <= BRAIN_TTL[1]:
            raise BadInput(f"ttl: {BRAIN_TTL[0]}..{BRAIN_TTL[1]} s")
        slo = _slo_tiles(a.get("slo"))
        self.brain = {"at": time.time(), "ttl": ttl, "stores": dict(stores), "tg": dict(tg), "parts": clean,
                      "approvals_pending": pend, "slo": slo}  # fmt: skip
        return {"ok": True, "ttl": ttl}

    def set_watch(self, a: dict[str, Any]) -> dict[str, Any]:
        wid = a.get("id")
        if (
            not isinstance(wid, str)
            or not ID_RE.match(wid)
            or wid in {p.id for p in PARTS}
            or wid.startswith("domain-")
        ):
            raise BadInput("id: 2-41 of [a-z0-9-], not a registry part")
        if a.get("remove"):
            gone = self.watch.pop(wid, None) is not None
            self.parts.pop(wid, None)
            return {"ok": True, "removed": gone}
        host, _, port = str(a.get("tcp") or "").rpartition(":")
        if not HOST_RE.match(host) or not port.isdigit() or not 0 < int(port) < 65536:
            raise BadInput("tcp: host:port (a docker name or address)")
        ttl = a.get("ttl", 600)
        if isinstance(ttl, bool) or not isinstance(ttl, int) or not WATCH_TTL[0] <= ttl <= WATCH_TTL[1]:
            raise BadInput(f"ttl: {WATCH_TTL[0]}..{WATCH_TTL[1]} s")
        label = a.get("label", wid)
        if not isinstance(label, str) or not WORD_RE.match(label):
            raise BadInput("label: up to 40 plain characters")
        if wid not in self.watch and len(self.watch) >= MAX_WATCH:
            raise BadInput(f"at most {MAX_WATCH} watch parts")
        self.watch[wid] = {"host": host, "port": int(port), "label": label or wid, "until": time.monotonic() + ttl}
        return {"ok": True, "id": wid, "ttl": ttl}

    async def api(self, method: str, path: str, who: str, body: bytes) -> tuple[int, Any]:
        """hubd's /feed* routes (the caller is authenticated already)."""
        if path in ("/feed", "/feed/") and method == "GET":
            return 200, self.snap or {"seq": 0}
        if method != "POST" or path not in ("/feed/brain", "/feed/watch"):
            return 404, {"error": "not_found"}
        if who != "brain":
            return 403, {"error": "forbidden", "hint": f"{path} is not the {who} caller's"}
        try:
            a = json.loads(body or b"{}")
        except ValueError:
            return 400, {"error": "bad_json"}
        if not isinstance(a, dict):
            return 400, {"error": "bad_json", "hint": "a JSON object"}
        try:
            out = self.brain_report(a) if path == "/feed/brain" else self.set_watch(a)
        except BadInput as e:
            return 400, {"error": "bad_request", "hint": str(e)[:300]}
        await self.probe_parts()
        self.publish()
        return 200, out

    def health(self) -> dict[str, Any]:
        return {"state": "ok" if self.server is not None else "off", "port": self.port, "seq": self.seq,
                "clients": len(self.clients), "watch": len(self.watch)}  # fmt: skip

    # ------------------------------------------------------------------------------------------ the listener

    async def serve(self, bind: str, port: int) -> int:
        self.server = await asyncio.start_server(self._handle, bind, port, reuse_address=True)
        port_: int = self.server.sockets[0].getsockname()[1]
        self.port = port_
        return port_

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            for q in list(self.clients):  # wakes each stream so it ends
                while q.full():
                    q.get_nowait()
                q.put_nowait("")
            self.server = None

    @staticmethod
    async def _send(w: asyncio.StreamWriter, status: int, obj: Any) -> None:
        reason = {200: "OK", 404: "Not Found", 405: "Method Not Allowed", 503: "Service Unavailable"}.get(
            status, "Error"
        )
        data = json.dumps(obj, separators=(",", ":")).encode()
        w.write(
            f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nCache-Control: no-store\r\n"
            f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode()
            + data
        )
        await w.drain()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), 10)
            parts = line.decode("latin-1").rstrip("\r\n").split(" ")
            while True:  # headers: read and dropped (no token, no cookie is ever used here)
                h = await asyncio.wait_for(reader.readline(), 10)
                if h in (b"\r\n", b"\n", b""):
                    break
            if len(parts) != 3:
                return
            method, target = parts[0], parts[1].split("?", 1)[0]
            if method != "GET":
                await self._send(writer, 405, {"error": "method"})
            elif target in ("/feed", "/feed/"):
                await self._send(writer, 200, self.snap or {"seq": 0})
            elif target == "/feed/health":
                await self._send(writer, 200, {"ok": True, "seq": self.seq, "clients": len(self.clients),
                                               "at": round(self.at, 1)})  # fmt: skip
            elif target == "/feed/stream":
                await self._stream(writer)
            else:
                await self._send(writer, 404, {"error": "not_found"})
        except (TimeoutError, asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            writer.close()

    async def _stream(self, w: asyncio.StreamWriter) -> None:
        if len(self.clients) >= MAX_CLIENTS:
            await self._send(w, 503, {"error": "busy"})
            return
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=4)
        self.clients.add(q)
        try:
            w.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-store\r\n"
                b"X-Accel-Buffering: no\r\nConnection: keep-alive\r\n\r\nretry: 3000\n\n"
            )
            if self.snap:
                w.write(
                    f"event: feed\nid: {self.seq}\ndata: {json.dumps(self.snap, separators=(',', ':'))}\n\n".encode()
                )
            await w.drain()
            while self.server is not None:
                try:
                    frame = await asyncio.wait_for(q.get(), PING_S)
                except TimeoutError:
                    frame = ": ping\n\n"
                if not frame:
                    break
                w.write(frame.encode())
                await w.drain()
        finally:
            self.clients.discard(q)


__all__ = ["PARTS", "STATES", "BadInput", "Feed", "Part", "gateway_stats", "phoenix_traces", "tcp_probe"]


SLO_IDS = ("tap_ack", "first_reply", "event_message", "vault_answer", "job_start_lag", "reminder_late", "joined_traces")
SLO_STATUS = ("ok", "miss", "no-data")


def _num_or_none(v: Any, hi: float) -> bool:
    return v is None or (not isinstance(v, bool) and isinstance(v, (int, float)) and 0 <= v < hi)


def _slo_tiles(raw: Any) -> list[dict[str, Any]] | None:
    """The brain's SLO tiles (inbox.slo.feed_tiles): <= 12 of {id, value, target, op, unit, n, status,
    window_days}, numbers and fixed tokens only. None = no tiles sent (the brain's switch is off)."""
    if raw is None:
        return None
    if not isinstance(raw, list) or len(raw) > 12:
        raise BadInput("slo: a list of <= 12 tiles")
    out = []
    for t in raw:
        if not isinstance(t, dict) or set(t) - {"id", "value", "target", "op", "unit", "n", "status", "window_days"}:
            raise BadInput("slo: tiles {id, value, target, op, unit, n, status, window_days}")
        if t.get("id") not in SLO_IDS or t.get("status") not in SLO_STATUS or t.get("op") not in ("<", "<=", ">="):
            raise BadInput(f"slo: id one of {SLO_IDS}, status one of {SLO_STATUS}, op < <= >=")
        if t.get("unit") not in ("s", "share"):
            raise BadInput("slo: unit s|share")
        if not all(_num_or_none(t.get(k), 1e9) for k in ("value", "target", "n", "window_days")):
            raise BadInput("slo: value, target, n, window_days: numbers >= 0 or null")
        out.append({k: t.get(k) for k in ("id", "value", "target", "op", "unit", "n", "status", "window_days")})
    return out
