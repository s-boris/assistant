"""Hub mode: the gateway inside claude-hub-phone (PHONEGW_HUB_MODE=1 plus PHONEGW_HUB_URL,
PHONEGW_HUB_GW_URL and PHONEGW_HUB_TOKEN_FILE, set by ops/hub/phoneloader.py). The gateway keeps its app protocol,
pairing, pushes, fronts and state; only the ways into the brain container change (it has none of the brain's CLIs):
  taps     bridge.run (a push button he tapped) -> hub event phone.tap; bridge.run_tool (a flag effect) -> phone.effect
  chat     plugin_handoff (an app chat message, a decision-card pick) -> phone.chat; the brain's `hub phone inbox`
           hands it to the live session (source app -> via=app), else a note
  shares   on_share -> phone.share (metadata, short text); the files stay here and the brain fetches them over the
           internal API (GET /internal/file, `phone file get`)
  notes    what the gateway writes into its inbox dir (PHONEGW_INBOX: self-test reds, crashes, offline, budget, flag
           notes, decisions) -> phone.note, removed once the hub has them (Shipper)
  zone     his coarse place changing (zone names only, never coordinates) -> phone.zone
  usage    the app's usage frames -> phone.usage (the brain folds them into its usage spool)
  lookups  quiet hours (tripclock tz, sleepin status), the reminders list and preview, the decision deck, trip
           conditions and the widget producers (`phone-produce KIND`) -> the hub gateway's phone.lookup (principal
           phone, read-only, run in the brain container)
  writes   reminder changes and their analytics line, the divelog quick add, the health ingest, the trip pack and
           Track ID -> phone.act (effect home, allow-listed argv, run in the brain; the trip pack comes back as the
           result's file, Track ID's clip is fetched from here over GET /internal/file)
The phone.* tools (status, pushes, push, link, open, chat) stay the brain's: its `phone` CLI reaches this gateway's
internal API with a token of its own. An event the hub does not take is kept in the inbox dir as pending-*.json and
retried by the Shipper (a tap or a chat message survives a hub restart). Nothing here sees a key: the hub token file
is read at use, never logged.
"""
import asyncio
import collections
import datetime as dt
import json
import logging
import os
import re
import secrets
import shutil
import time
from zoneinfo import ZoneInfo

import aiohttp

from . import bridge
from . import now as N
from . import quiet as Q
from .util import now_s, write_private
from .m2 import NOTE_HINT

log = logging.getLogger("phonegw.hub")
NOTE_NAME = re.compile(r"^phone-[A-Za-z0-9._-]{1,120}\.md$")
PENDING = re.compile(r"^pending-[0-9]+-[0-9a-f]{8}\.json$")
TEXT_INLINE = 6000          # longer shared text travels as a file of the share: the event stays under 16 KiB
USAGE_KEYS = ("feature", "kind", "surface", "level", "ms", "outcome", "at")
QUIET_EVERY = 60.0
SHIP_EVERY = 2.0
CLAIM_PAD = 10.0
# phone.act's gateway rate is 60/min. A reconnect replays many frames at once (each spawns its write: 140 acts in 8 s
# after the 28.10 hub deploy, 70 refused rate_limited and lost), so writes are paced under the rate and a
# rate_limited answer (refused before the tool runs: safe to resend) is retried after a pause.
ACT_RATE = (50, 60.0)
ACT_BACKOFF = (20.0, 40.0)


def clip(text, nbytes):
    """text cut to at most nbytes of UTF-8 (the hub takes payloads up to 16 KiB)."""
    b = text.encode()
    return text if len(b) <= nbytes else b[:nbytes].decode("utf-8", "ignore")


class HubError(Exception):
    pass


def _token(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.readline().strip()
    except (OSError, TypeError, ValueError):
        return ""


class Hub:
    """The hub as the gateway sees it: events (hubd /phone/event) and gateway tools (MCP, principal phone)."""

    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.url = (env.get("PHONEGW_HUB_URL") or "http://claude-hub:8700").rstrip("/")
        self.gw_url = (env.get("PHONEGW_HUB_GW_URL") or "http://claude-hub:8701").rstrip("/")
        self.token_file = env.get("PHONEGW_HUB_TOKEN_FILE") or "/run/secrets/token_phone"
        self._http = None
        self._rpc = 0
        self.sent = 0
        self.kept = 0
        self.calls = 0
        self._acts = collections.deque()            # monotonic start times of the writes in the last ACT_RATE window
        self._act_lock = asyncio.Lock()
        self.act_waits = 0
        self.act_retries = 0

    async def http(self):
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession()
        return self._http

    async def close(self):
        if self._http is not None:
            await self._http.close()

    async def post(self, url, body, timeout=10.0, headers=None):
        tok = _token(self.token_file)
        if not tok:
            raise HubError("no hub token")
        s = await self.http()
        h = {"Authorization": "Bearer " + tok, **(headers or {})}
        try:
            async with s.post(url, json=body, headers=h, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                text = await r.text()
                try:
                    data = json.loads(text) if text else {}
                except ValueError:
                    data = {}
                return r.status, data
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            raise HubError(type(e).__name__) from None

    async def event_now(self, kind, subject, payload, idem):
        """One phone.* row on the hub -> True when it took it (a repeat idem_key counts as taken)."""
        st, d = await self.post(self.url + "/phone/event",
                                {"kind": kind, "subject": str(subject)[:200], "payload": payload, "idem_key": idem,
                                 "rid": "pe-" + secrets.token_hex(8)}, timeout=5.0)  # fmt: skip
        if st == 200 and isinstance(d, dict):
            self.sent += 1
            return True
        if st == 400:                               # never retried: the hub refused the shape
            log.warning("hub event %s refused (%s)", kind, str(d.get("error") or d)[:120] if isinstance(d, dict) else "?")
            return None
        raise HubError("http %s" % st)

    async def tool(self, name, args, timeout=10.0):
        """One gateway call (MCP tools/call, stateless JSON) -> (ok, structured data, text)."""
        self._rpc += 1
        self.calls += 1
        st, d = await self.post(self.gw_url + "/mcp",
                                {"jsonrpc": "2.0", "id": self._rpc, "method": "tools/call",
                                 "params": {"name": name, "arguments": args}},
                                timeout=timeout, headers={"Accept": "application/json, text/event-stream"})  # fmt: skip
        r = d.get("result") if isinstance(d, dict) else None
        if st != 200 or not isinstance(r, dict):
            raise HubError("gateway http %s" % st)
        text = "".join(c.get("text", "") for c in r.get("content") or [] if isinstance(c, dict))
        return not r.get("isError"), r.get("structuredContent") or {}, text

    async def lookup(self, argv, timeout=10.0):
        """A read-only brain CLI through phone.lookup -> (exit code or None when it could not run, stdout)."""
        t = max(1.0, min(float(timeout), 180.0))
        try:
            ok, data, _ = await self.tool("phone_lookup", {"argv": [str(a) for a in argv], "timeout_s": t},
                                          timeout=t + CLAIM_PAD + 20)  # fmt: skip
        except HubError as e:
            log.warning("lookup %s: hub %s", argv[0], e)
            return None, ""
        if not ok:
            log.warning("lookup %s: refused or failed", argv[0])
            return None, ""
        return int(data.get("rc", 1)), str(data.get("out") or "")

    async def _act_slot(self):
        """Wait until a write fits under ACT_RATE (first come, first served): a burst is paced, not refused."""
        n, window = ACT_RATE
        async with self._act_lock:
            while True:
                now = time.monotonic()
                while self._acts and now - self._acts[0] >= window:
                    self._acts.popleft()
                if len(self._acts) < n:
                    self._acts.append(now)
                    return
                self.act_waits += 1
                await asyncio.sleep(window - (now - self._acts[0]) + 0.01)

    async def act(self, argv, timeout=60.0, stdin=None, file=None):
        """A brain write through phone.act -> (exit code or None, stdout, stderr, the @out file's text or None)."""
        t = max(1.0, min(float(timeout), 180.0))
        args = {"argv": [str(a) for a in argv], "timeout_s": t}
        if stdin is not None:
            args["stdin"] = stdin
        if file:
            args["file"] = file
        for n in range(len(ACT_BACKOFF) + 1):
            await self._act_slot()
            try:
                ok, data, text = await self.tool("phone_act", args, timeout=t + CLAIM_PAD + 20)
            except HubError as e:
                log.warning("act %s: hub %s", argv[0], e)
                return None, "", "", None
            if ok:
                break
            code = text.split(":", 1)[0].strip()[:40] or "?"     # the gateway's error code only, never the text
            if code != "rate_limited" or n == len(ACT_BACKOFF):
                log.warning("act %s: refused or failed (%s)", argv[0], code)
                return None, "", "", None
            self.act_retries += 1
            await asyncio.sleep(ACT_BACKOFF[n])
        f = data.get("file")
        return int(data.get("rc", 1)), str(data.get("out") or ""), str(data.get("err") or ""), f if isinstance(f, str) else None


class Outbox:
    """Events out, kept on disk when the hub does not take them; notes the gateway wrote, shipped as phone.note."""

    def __init__(self, gw, hub):
        self.gw, self.hub = gw, hub
        self.notes = 0

    @property
    def dir(self):
        return self.gw.paths.inbox

    def keep(self, kind, subject, payload, idem):
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        name = "pending-%d-%s.json" % (time.time_ns(), secrets.token_hex(4))
        write_private(os.path.join(self.dir, name), json.dumps({"kind": kind, "subject": subject, "payload": payload,
                                                                 "idem": idem}))  # fmt: skip
        self.hub.kept += 1

    async def event(self, kind, subject, payload, idem):
        """-> True sent, False kept for a retry, None refused by the hub (dropped, logged)."""
        try:
            return await self.hub.event_now(kind, subject, payload, idem)
        except HubError as e:
            log.warning("hub event %s: %s; kept for a retry", kind, e)
            self.keep(kind, subject, payload, idem)
            return False

    async def once(self):
        try:
            names = sorted(os.listdir(self.dir))
        except OSError:
            return 0
        n = 0
        for name in names:
            p = os.path.join(self.dir, name)
            try:
                if time.time() - os.path.getmtime(p) < 1.0:
                    continue                        # still being written
                with open(p, encoding="utf-8") as fh:
                    text = fh.read(64000)
            except OSError:
                continue
            if PENDING.match(name):
                try:
                    e = json.loads(text)
                    args = (str(e["kind"]), str(e.get("subject") or ""), e.get("payload") or {}, str(e.get("idem") or name))
                except (ValueError, KeyError, TypeError):
                    os.unlink(p)
                    continue
            elif NOTE_NAME.match(name):
                args = ("phone.note", name, {"note": {"name": name, "text": clip(text, 12000)}}, "note:" + name)
            else:
                continue
            try:
                took = await self.hub.event_now(*args)
            except HubError:
                return n
            os.unlink(p)                            # taken, or refused for good (logged)
            n += 1
            self.notes += args[0] == "phone.note"
        return n

    async def loop(self):
        while True:
            try:
                await self.once()
            except Exception as e:                  # the shipper never dies
                log.warning("outbox: %s", type(e).__name__)
            await asyncio.sleep(SHIP_EVERY)


class QuietCache:
    """quiet.py's two brain reads (his time zone while away, the sleep-in guard) as a cache refreshed in the
    background, so the hold verdict (sync) never waits on the hub."""

    def __init__(self, gw, hub):
        self.gw, self.hub = gw, hub
        self.tz = None
        self.sleepin = None                         # sleepin status --json: {"active", "until", "date"} when active
        self.at = 0.0

    def his_tz(self):
        return self.tz or N.tz_of(self.gw.cfg)

    def sleepin_release(self, tz):
        d = self.sleepin
        if not d:
            return None
        try:
            y, mo, da = (int(x) for x in str(d["date"]).split("-"))
            h, m = Q._hm(d["until"])
            return int(dt.datetime(y, mo, da, h, m, tzinfo=tz).timestamp())
        except Exception:
            return None

    async def refresh(self):
        rc, out = await self.hub.lookup(["tripclock", "tz"], 10)
        if rc == 0 and out.strip():
            try:
                self.tz = ZoneInfo(out.strip().split()[0])
            except Exception:
                pass
        rc, out = await self.hub.lookup(["sleepin", "status", "--json", "--days", "1"], 10)
        if rc is not None:
            try:
                d = json.loads(out) if rc == 0 else {}
            except ValueError:
                d = {}
            self.sleepin = d if isinstance(d, dict) and d.get("active") and d.get("until") and d.get("date") else None
        self.at = time.time()

    async def loop(self):
        while True:
            try:
                await self.refresh()
            except Exception as e:
                log.warning("quiet cache: %s", type(e).__name__)
            await asyncio.sleep(QUIET_EVERY)


def usage_payload(env):
    b = env.get("body") or {}
    items = [{k: it.get(k) for k in USAGE_KEYS if isinstance(it.get(k), (str, int, float))}
             for it in (b.get("items") or [])[:60] if isinstance(it, dict)]
    return {"env_id": str(env.get("id") or "")[:40], "app": str(b.get("app") or "")[:20], "items": items,
            "dropped": int(b.get("dropped") or 0)}


def install(gw, env=None):
    """Hub mode on: every way into the brain goes through the hub. -> the Hub (tests) after spawning its tasks."""
    hub = Hub(env)
    out = Outbox(gw, hub)
    qc = QuietCache(gw, hub)
    gw.hub, gw.hub_outbox, gw.hub_quiet = hub, out, qc
    gw.cfg["chat_plugin"] = True                    # app chat, card picks and shares go out at once (as events)

    async def tap(a, timeout=30):
        act = {k: a.get(k) for k in ("push_id", "nonce", "tag", "level", "verb", "arg", "at", "device_id")}
        r = await out.event("phone.tap", "tap %s %s" % (a.get("verb"), a.get("tag") or ""), {"action": act},
                            "tap:%s:%s" % (a.get("push_id"), a.get("verb")))  # fmt: skip
        return [("hub", 0 if r else None)]

    async def effect(name, args, timeout=60):
        if name not in bridge.EFFECT_TOOLS:
            return (name, None)
        r = await out.event("phone.effect", "effect " + name, {"name": name, "args": [str(x)[:40] for x in args][:3]},
                            "effect:%s:%s" % (name, secrets.token_hex(6)))  # fmt: skip
        return ("hub", 0 if r else None)

    async def handoff(what, task, **extra):
        body = {"what": what, "task": clip(str(task), 12000)}
        body.update({k: str(v)[:80] for k, v in extra.items() if v and k in ("msg_id", "device_id", "card_id")})
        r = await out.event("phone.chat", "app %s" % what, body, "chat:%s" % (extra.get("msg_id") or secrets.token_hex(8)))
        return r is not None        # kept for a retry = taken (it reaches the session late); refused -> the caller's note

    async def on_share(dev_id, meta, stored=None):
        sid = meta["share_id"]
        if gw.db.one("SELECT 1 FROM shares WHERE id=?", sid):
            return "dup"
        files = list(stored or [])
        text = str(meta.get("text") or "")
        if len(text.encode()) > TEXT_INLINE:
            d = os.path.join(gw.shares_dir(), re.sub(r"[^A-Za-z0-9._-]+", "_", sid)[:64])
            os.makedirs(d, mode=0o700, exist_ok=True)
            p = os.path.join(d, "00-text.txt")
            write_private(p, text)
            files.insert(0, (p, "text/plain", len(text.encode())))
            text = clip(text, 1500)
        gw.db.x("INSERT INTO shares(id, at, kind, path, bytes, mime) VALUES(?,?,?,?,?,?)", sid, now_s(), meta["kind"],
                os.path.dirname(files[0][0]) if files else None, sum(f[2] for f in files), files[0][1] if files else None)
        base = gw.shares_dir()
        payload = {"share_id": sid, "device_id": dev_id, "kind": meta["kind"], "intent": meta["intent"],
                   "url": str(meta.get("url") or "")[:2000] or None, "text": text or None, "at": now_s(),
                   "paperwork": bool(gw.paperwork(meta)), "hint": NOTE_HINT.get(meta["intent"], "see what he wants"),
                   "files": [{"id": os.path.relpath(f[0], base), "name": os.path.basename(f[0]), "mime": f[1],
                              "bytes": f[2]} for f in files][:20]}  # fmt: skip
        await out.event("phone.share", "share %s" % meta["kind"], payload, "share:" + sid)
        log.info("device %s: share %s (%s, %d file(s)) -> hub", dev_id, meta["kind"], meta["intent"], len(files))
        return "new"

    def place(dev_id):
        cur, _ = gw.db.get_state(dev_id, "place", None)
        return (cur or {}).get("zone")

    def wrap_zone(fn):
        async def wrapped(dev_id, *a, **kw):
            before = place(dev_id)
            res = await fn(dev_id, *a, **kw)
            after = place(dev_id)
            if after and after != before:
                at = now_s()
                gw.spawn(out.event("phone.zone", "zone " + after, {"zone": after, "prev": before, "at": at,
                                                                   "device_id": dev_id},
                                   "zone:%s:%s:%s" % (dev_id, after, at)))  # fmt: skip
            return res
        return wrapped

    def on_usage(env_):
        p = usage_payload(env_)
        if p["items"] or p["dropped"]:
            gw.spawn(out.event("phone.usage", "usage " + p["env_id"], p, "usage:" + p["env_id"]))

    def public_usage(outcome, ms=None):
        """The public route's own counters (phonegw.public connect/refuse...): no IPs, no text."""
        eid = "gw-%d-%s" % (now_s(), secrets.token_hex(3))
        g = {"feature": "phonegw.public", "outcome": str(outcome)[:40], "ms": ms if isinstance(ms, (int, float)) else None}
        gw.spawn(out.event("phone.usage", "usage public", {"env_id": eid, "app": "", "items": [], "dropped": 0, "gw": g},
                           "usage:" + eid))  # fmt: skip

    async def rem_run(args, timeout=25):
        argv = ["remind"] + [str(a) for a in args]
        if args and args[0] in ("list", "when"):
            rc, o = await hub.lookup(argv, timeout)
            return rc, o, ""
        rc, o, err, _ = await hub.act(argv, timeout)
        return rc, o, err

    async def rem_fire(argv):
        await hub.act(["usage"] + list(argv[1:]), 10)

    async def health_ingest(b):
        rc, _, _, _ = await hub.act(["health", "ingest", "--received", str(now_s())], 60,
                                    stdin=json.dumps(b, ensure_ascii=False))  # fmt: skip
        gw.health_ingested = getattr(gw, "health_ingested", 0) + (rc == 0)
        if rc != 0:
            log.warning("health ingest: exit %s", rc)
        return rc == 0

    async def run_bin(argv, timeout=120, stdin=None):
        name = os.path.basename(argv[0])
        if name == "trip-pack" and len(argv) == 4 and argv[2] == "--out":
            rc, _, _, f = await hub.act(["trip-pack", argv[1], "--out", "@out"], timeout)
            if f is not None:
                write_private(argv[3], f)
            return rc, b""
        if name == "divelog":
            rc, o, _, _ = await hub.act(["divelog"] + list(argv[1:]), timeout)
            return rc, o.encode()
        if name == "trip-conditions":
            rc, o = await hub.lookup(["trip-conditions"] + list(argv[1:]), timeout)
            return rc, o.encode()
        log.warning("hub mode: %s has no hub path", name)
        return None, b""

    async def produce(kind, *args, timeout=40):
        rc, o = await hub.lookup(["phone-produce", kind] + [str(a) for a in args], timeout)
        if rc != 0:
            return None
        try:
            return json.loads(o)
        except ValueError:
            return None

    async def deck_items():
        rc, o = await hub.lookup(["decision-deck", "--json"], 30)
        try:
            return json.loads(o).get("items") if rc == 0 else None
        except (ValueError, AttributeError):
            return None

    async def trackid_run(path, timeout):
        tid = "tid-" + secrets.token_hex(6)
        d = os.path.join(gw.shares_dir(), tid)
        os.makedirs(d, mode=0o700)
        try:
            dst = os.path.join(d, os.path.basename(path))
            shutil.copyfile(path, dst)
            os.chmod(dst, 0o600)
            rc, o, _, _ = await hub.act(["trackid", "@file", "--json", "--source", "app", "--min-store", "medium"],
                                        timeout, file="%s/%s" % (tid, os.path.basename(path)))  # fmt: skip
        finally:
            shutil.rmtree(d, ignore_errors=True)
        if rc is None:
            raise OSError("hub")
        return rc, o.encode()

    bridge.run, bridge.run_tool = tap, effect
    gw.plugin_handoff, gw.on_share, gw.on_usage, gw.public_usage = handoff, on_share, on_usage, public_usage
    gw.m2_event, gw.m2_state = wrap_zone(gw.m2_event), wrap_zone(gw.m2_state)
    gw.his_tz, gw.sleepin_release = qc.his_tz, qc.sleepin_release
    gw.rem_run, gw._rem_fire, gw.health_ingest, gw.run_bin = rem_run, rem_fire, health_ingest, run_bin
    gw.produce, gw.deck_items, gw.trackid_run = produce, deck_items, trackid_run
    gw.spawn(out.loop())
    gw.spawn(qc.loop())
    log.info("hub mode: events -> %s, tools -> %s", hub.url, hub.gw_url)
    return hub
