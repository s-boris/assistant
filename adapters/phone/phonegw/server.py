"""phonegw serve: the app API over mTLS (127.0.0.1:tls_port, reached through the front), the internal API for my tools
(127.0.0.1:internal_port, Bearer) and the front (:front_port). One asyncio process."""
import asyncio
import collections
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import signal
import time

from aiohttp import WSMsgType, web

from . import __version__, bridge, export, pki
from . import config as C
from .db import DB
from .front import Front
from .public import Guard
from .lint import lint
from .m2 import M2, M2b
from .m4 import M4
from .reminders import Reminders
from .voice import Voice
from .opener import Opener
from .guards import Guards
from .quiet import Quiet
from .schema import Protocol, ProtocolError
from .usagefwd import UsageForwarder
from .util import gen_code, normalize_code, now_ms, now_s, short_id, ulid, write_private

log = logging.getLogger("phonegw")
LEVELS = ("urgent", "timed", "decision", "info")

# HTTPS batch endpoints -> the c2s types each accepts. Mirrors the app's GatewayApi.pathFor (ack/action/chat/card.answer
# have their own path, every other c2s type goes to /v1/events), so anything the outbox queued offline lands here later.
BATCH_ALLOWED = {
    "/v1/events": frozenset({"event", "state", "hello", "selftest", "crash", "answer", "share", "usage", "call.state",
                              "open.result"}),
    "/v1/ack": frozenset({"ack"}),
    "/v1/action": frozenset({"action"}),
    "/v1/chat": frozenset({"chat"}),
    "/v1/card-answer": frozenset({"card.answer"}),
}

VERBS = ("seen", "done", "snooze", "call", "crate", "ha_off", "open")
UNLOCK = {"done", "snooze", "ha_off"}
OPEN_PATHS = {"/v1/health", "/v1/pair"}
TAG_RE = re.compile(r"[^a-z0-9_:.-]+")
ARG_RE = re.compile(r"^[A-Za-z0-9_:.+-]{1,64}$")
# One-tap links (push.link): https without userinfo, geo:, google.navigation:; same pattern as protocol s2c.push.json.
LINK_RE = re.compile(r"^(https://[A-Za-z0-9.-]+(:[0-9]{1,5})?([/?#][^\s\x00-\x1f\x7f]*)?"
                     r"|geo:[^\s\x00-\x1f\x7f]+|google\.navigation:[^\s\x00-\x1f\x7f]+)$")


def safe_link(url):
    return isinstance(url, str) and 6 <= len(url) <= 2048 and bool(LINK_RE.fullmatch(url))


def jerr(status, error, msg="", **kw):
    body = {"error": error}
    if msg:
        body["msg"] = msg[:200]
    body.update(kw)
    return web.json_response(body, status=status)


def p95(vals):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    return vals[min(len(vals) - 1, int(round(0.95 * (len(vals) - 1))))]


def clean_tag(tag):
    t = TAG_RE.sub("-", str(tag).lower()).strip("-")[:48]
    return t if t and t[0].isalpha() else ("t-" + t)[:48] if t else None


APP_MSG_ID = re.compile(r"m_[0-9A-HJKMNP-TV-Z]{26}")


def chat_buttons(rows):
    """Telegram-style rows -> the app's {text, data} only (style/url keys dropped, callback_data = data); anything
    malformed passes through unchanged so the schema check still reports it."""
    if not isinstance(rows, list):
        return rows
    out = []
    for row in rows:
        if not isinstance(row, list):
            return rows
        out.append([{"text": b.get("text"), "data": b.get("data") or b.get("callback_data")} if isinstance(b, dict) else b
                    for b in row])
    return out


class Conn:
    def __init__(self, ws, dev_id):
        self.ws, self.dev = ws, dev_id
        self.lock = asyncio.Lock()
        self.sent = set()
        self.replaying = True
        self.since = time.time()
        self.ack_buf = []
        self.ack_task = None
        self.route = "lan"            # lan (LAN/WG through the front) | public (internet path)


class Gateway(M2, M2b, M4, Reminders, Voice, Guards, Quiet, Opener):
    def __init__(self, paths, cfg=None):
        self.paths = paths
        self.usage_fwd = None          # app usage frames -> analytics spool (on_usage; built on first use)
        self.cfg = cfg or C.load(paths)
        self.db = DB(paths.db)
        self.proto = Protocol(paths.protocol)
        with open(paths.token) as fh:
            self.token = fh.read().strip()
        self.conns = {}
        self.waiters = {}
        self.peers = {}
        self.pair_rl = collections.defaultdict(collections.deque)
        self.last_data = {}
        self.seen_write = {}
        self.front = None
        self.front_task = None
        self.guard = Guard(self.cfg["public"])
        self.public_site = None
        self.public_ctx = None
        self.public_checked = 0.0
        self.public_noted = {}
        self.public_sess = {}         # device -> its session on the internet route (public_open / public_close)
        self.ext_runner = None
        self.last_route = {}
        self.runners = []
        self.tasks = set()
        self.ports = {}
        self.started = time.time()
        self.m2_init()
        self.voice_init()

    # ------------------------------------------------------------ lifecycle
    async def start(self, front=True):
        ext = web.Application(middlewares=[self.auth_mw], client_max_size=1 << 20)
        ext.add_routes([web.get("/v1/health", self.x_health), web.post("/v1/pair", self.x_pair),
                        web.get("/v1/ws", self.x_ws), web.get("/v1/sync", self.x_sync),
                        web.post("/v1/events", self.x_batch), web.post("/v1/ack", self.x_batch),
                        web.post("/v1/action", self.x_batch), web.post("/v1/chat", self.x_batch),
                        web.post("/v1/card-answer", self.x_batch), web.get("/v1/geo/{pack}", self.x_geo),
                        web.post("/v1/share", self.x_share), web.get("/v1/app/latest", self.x_app_latest),
                        web.get("/v1/app/apk/{code}", self.x_app_apk),
                        web.get("/v1/voice", self.x_voice), web.post("/v1/trackid", self.x_trackid),
                        web.get("/v1/trip/{what}", self.x_trip), web.post("/v1/divelog", self.x_divelog),
                        web.get("/v1/reminders", self.x_reminders), web.post("/v1/reminders", self.x_reminders_op)])
        intl = web.Application(middlewares=[self.bearer_mw], client_max_size=1 << 20)
        intl.add_routes([web.post("/internal/push", self.i_push), web.post("/internal/push/cancel", self.i_cancel),
                         web.post("/internal/chat", self.i_chat), web.get("/internal/status", self.i_status),
                         web.get("/internal/devices", self.i_devices), web.post("/internal/pair-code", self.i_pair_code),
                         web.post("/internal/revoke", self.i_revoke), web.post("/internal/probe", self.i_probe),
                         web.get("/internal/pushes", self.i_pushes), web.get("/internal/file", self.i_file),
                         web.get("/internal/now", self.i_now), web.post("/internal/ask", self.i_ask),
                         web.post("/internal/widget", self.i_widget), web.post("/internal/card", self.i_card),
                         web.post("/internal/card/remove", self.i_card_rm), web.get("/internal/cards", self.i_cards),
                         web.get("/internal/geofences", self.i_geofences_get),
                         web.post("/internal/geofences", self.i_geofences), web.post("/internal/alarms", self.i_alarms),
                         web.post("/internal/routine", self.i_routine), web.post("/internal/geo", self.i_geo),
                         web.post("/internal/update", self.i_update),
                         web.post("/internal/ring", self.i_ring), web.post("/internal/open", self.i_open),
                         web.get("/internal/nfc", self.i_nfc), web.post("/internal/nfc", self.i_nfc),
                         web.get("/internal/trip", self.i_trip), web.post("/internal/trip", self.i_trip),
                         web.get("/internal/export", self.i_export)])
        for name, app, host, port, ssl_ctx in (
                ("tls", ext, self.cfg["tls_bind"], self.cfg["tls_port"], pki.server_context(self.paths)),
                ("internal", intl, self.cfg.get("internal_bind") or "127.0.0.1", self.cfg["internal_port"], None)):
            runner = web.AppRunner(app, access_log=None, handle_signals=False)
            await runner.setup()
            site = web.TCPSite(runner, host, int(port), ssl_context=ssl_ctx, reuse_address=True)
            await site.start()
            self.runners.append(runner)
            self.ports[name] = runner.addresses[0][1]
            if name == "tls":
                self.ext_runner = runner
        if self.public_wanted():
            await self.public_on()
        if front:
            await self.front_on()
        self.spawn(self.housekeeping())
        if self.cfg.get("widgets_auto", True):
            self.widget_task = self.spawn(self.widget_loop())
        log.info("phonegw %s up (tls %s, internal %s, front %s)", __version__, self.ports["tls"], self.ports["internal"],
                 "on" if self.front else "off")

    def front_wanted(self):
        return bool(self.cfg.get("front")) and not os.path.exists(self.paths.front_off)

    async def front_on(self):
        if self.front or not self.front_wanted():
            return
        f = Front(dict(self.cfg, tls_port=self.ports["tls"]), self.peers, self.guard, self.ports)
        try:
            await f.start()
            self.front = f
            self.ports["front"] = f.port
        except OSError as e:
            log.warning("front: port %s busy (%s); retrying every 30 s", self.cfg["front_port"], type(e).__name__)
            if not self.front_task or self.front_task.done():
                self.front_task = self.spawn(self._front_retry())

    async def _front_retry(self):
        while not self.front and self.front_wanted():
            await asyncio.sleep(30)
            if self.front_wanted() and not self.front:
                f = Front(dict(self.cfg, tls_port=self.ports["tls"]), self.peers, self.guard, self.ports)
                try:
                    await f.start()
                    self.front = f
                    self.ports["front"] = f.port
                except OSError:
                    continue

    async def front_off(self):
        if self.front:
            await self.front.stop()
            self.front = None
            self.ports.pop("front", None)
            log.info("front off")

    async def reload(self):
        rev = int(self.cfg.get("config_rev") or 1)
        self.cfg = C.load(self.paths)
        self.guard.update(self.cfg["public"])
        if self.public_wanted():
            await self.public_on()
        else:
            await self.public_off()
        if self.front_wanted():
            await self.front_on()
        else:
            await self.front_off()
        if int(self.cfg.get("config_rev") or 1) != rev:     # e.g. `phonegw public on` added the internet URL
            for conn in list(self.conns.values()):
                try:
                    await self.send_direct(conn, "config", C.app_config(self.cfg))
                except (ConnectionError, RuntimeError) as e:
                    log.warning("config push: %s", type(e).__name__)

    # ------------------------------------------------------------ internet path (public.py)
    def public_wanted(self):
        pc = self.cfg.get("public") or {}
        return bool(pc.get("enabled")) and bool(pc.get("host")) and bool(pc.get("proxy_ips"))

    async def public_on(self):
        """Start the public TLS site (127.0.0.1:public.tls_port, CERT_REQUIRED, DNS-only cert) on the app runner."""
        if self.public_site or not self.ext_runner:
            return
        pc = self.cfg["public"]
        try:
            pki.issue_public(self.paths, pc["host"])
            self.public_ctx = pki.public_context(self.paths)
            before = set(map(tuple, self.ext_runner.addresses))
            site = web.TCPSite(self.ext_runner, "127.0.0.1", int(pc["tls_port"]), ssl_context=self.public_ctx,
                               reuse_address=True)
            await site.start()
        except Exception as e:
            log.warning("public site not started (%s)", type(e).__name__)
            return
        new = [a for a in map(tuple, self.ext_runner.addresses) if a not in before]
        self.public_site = site
        self.ports["public"] = new[0][1] if new else int(pc["tls_port"])
        self.public_checked = time.time()
        log.info("public path on (site port %s)", self.ports["public"])

    async def public_off(self):
        if self.front:
            self.front.drop_public()
        if self.public_site:
            try:
                await self.public_site.stop()
            except Exception as e:
                log.warning("public site stop: %s", type(e).__name__)
            self.public_site = None
            self.ports.pop("public", None)
            log.info("public path off")
        for dev_id, c in list(self.conns.items()):     # live public sockets go too (the app falls back to LAN/WG)
            if c.route == "public":
                self.spawn(c.ws.close(code=4002, message=b"public off"))

    def is_public(self, request):
        t = request.transport
        sock = t.get_extra_info("sockname") if t else None
        return bool(sock) and self.public_site is not None and sock[1] == self.ports.get("public")

    def public_tick(self):
        """Housekeeping: renew the public cert under 30 days (daily check), one inbox note per spike per day."""
        if self.public_site and time.time() - self.public_checked > 86400:
            self.public_checked = time.time()
            if pki.issue_public(self.paths, self.cfg["public"]["host"]):
                self.public_ctx.load_cert_chain(self.paths.public_pem, self.paths.public_key)
                log.info("public cert renewed")
        sp, today = self.cfg["public"].get("spike") or {}, self.guard.status()["today"]
        for key, lim in (("refused", sp.get("refused_d")), ("bans", sp.get("bans_d"))):
            day = time.strftime("%Y-%m-%d")
            if lim and today.get(key, 0) > int(lim) and self.public_noted.get(key) != day:
                self.public_noted[key] = day
                self.note("public-spike", "# phonegw public path: %s spike\n\n%s today: %d (threshold %d). Counters: `phone "
                                         "status`. Off switch: `phonegw public off`.\n" % (key, key, today[key], int(lim)))

    def public_grace_s(self):
        return float((self.cfg.get("public") or {}).get("session_grace_s", 120))

    def public_open(self, dev_id):
        """A socket on the internet route. A session already open, or closed less than the grace ago (handover: the
        app's new socket replaces a half-open one; a drop and the retry; home and straight back at the edge of the
        Wi-Fi), goes on: no new use. Otherwise a session starts: one `connect`."""
        s = self.public_sess.get(dev_id)
        if s is not None:
            if s["timer"]:
                s["timer"].cancel()
            s.update(closed=None, timer=None, socks=s["socks"] + 1)
            return
        self.public_sess[dev_id] = {"start": time.time(), "closed": None, "timer": None, "socks": 1}
        self.public_usage("connect")

    def public_close(self, dev_id, conn):
        """A socket on the internet route closed. Replaced by a newer one there: the session goes on. Else it ends
        once the grace passes without a new internet socket (public_end)."""
        s = self.public_sess.get(dev_id)
        cur = self.conns.get(dev_id)
        if s is None or s["closed"] is not None or (cur is not None and cur is not conn and cur.route == "public"):
            return
        s["closed"] = time.time()
        grace = self.public_grace_s()
        if grace <= 0:
            self.public_end(dev_id)
        else:
            s["timer"] = asyncio.get_running_loop().call_later(grace, self.public_end, dev_id)

    def public_end(self, dev_id):
        """The session is over: one `disconnect`, ms = its time on the route (first socket open to last closed)."""
        s = self.public_sess.get(dev_id)
        if s is None or s["closed"] is None:
            return
        del self.public_sess[dev_id]
        if s["timer"]:
            s["timer"].cancel()
        log.info("device %s: internet session over (%d s, %d socket(s))", dev_id, s["closed"] - s["start"], s["socks"])
        self.public_usage("disconnect", ms=int((s["closed"] - s["start"]) * 1000))

    def public_flush(self):
        """stop(): end every session now (open ones at this moment), none waits for a grace that never comes."""
        now = time.time()
        for dev_id, s in list(self.public_sess.items()):
            if s["closed"] is None:
                s["closed"] = now
            self.public_end(dev_id)

    def public_usage(self, outcome, ms=None):
        if self.usage_fwd is None:
            self.usage_fwd = UsageForwarder(state=self.cfg.get("usage_state"), enabled=self.cfg.get("usage", True))
        try:
            self.usage_fwd.event("phonegw.public", "invoke", outcome=outcome, ms=ms)
        except Exception as e:
            log.warning("public usage: %s", type(e).__name__)

    async def stop(self):
        try:
            await self.voice_stop()             # in-app calls end now (the relay hears the socket close)
        except Exception:
            pass
        await self.public_off()
        await self.front_off()
        for c in list(self.conns.values()):
            try:
                await c.ws.close(code=1001, message=b"server stopping")
            except Exception:
                pass
        self.public_flush()
        for t in list(self.tasks):
            t.cancel()
        for r in self.runners:
            await r.cleanup()
        self.db.c.close()

    def spawn(self, coro):
        t = asyncio.ensure_future(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)
        return t

    async def housekeeping(self):
        while True:
            await asyncio.sleep(60)
            try:
                self.db.expire()
                self.check_offline()
                self.battery_tick()
                self.public_tick()
                await self.release_held()
            except Exception as e:
                log.warning("housekeeping: %s", type(e).__name__)

    # ------------------------------------------------------------ helpers
    def ws_silence_s(self):
        """A socket silent this long is closed. The app pings every keepalive_s, but its battery mode (saver or < 15 %)
        and the trip roaming saver (TripCore) stretch that to 540 s, so the floor is 540: 2 x 540 + 30 = 1110 s."""
        return 2 * max(int(self.cfg["keepalive_s"]), 540) + 30

    def client_ip(self, request):
        peer = request.transport.get_extra_info("peername") if request.transport else None
        if not peer:
            return "?"
        return self.peers.get(peer[1], peer[0])

    def touch(self, dev_id):
        now = now_s()
        if now - self.seen_write.get(dev_id, 0) >= 30:
            self.seen_write[dev_id] = now
            self.db.x("UPDATE devices SET last_seen=? WHERE id=?", now, dev_id)

    def env_of(self, row):
        env = {"v": 1, "id": row["id"], "t": row["t"], "ts": row["created"] // 1000, "ack": bool(row["ack"]),
               "ttl": max(1, row["expires"] - now_s()), "body": json.loads(row["body"])}
        if row["key"]:
            env["key"] = row["key"]
        return env

    def reject_note(self, dev_id, env, msg):
        """A frame from the app that the gateway refuses (schema mismatch): the app keeps it in its outbox and replays
        it on every connect, so his tap is stuck until the gateway accepts it. One `phone-reject` note per frame type
        and problem per day (msg names schema keys only, never payload values)."""
        t = env.get("t") if isinstance(env, dict) and isinstance(env.get("t"), str) else "?"
        key = "%s|%s" % (t[:40], str(msg)[:120])
        day = time.strftime("%Y-%m-%d")
        seen, _ = self.db.get_state("_gw", "reject_noted", {})
        seen = seen if isinstance(seen, dict) and seen.get("day") == day else {"day": day, "keys": []}
        if key in seen["keys"]:
            return
        seen["keys"] = (seen["keys"] + [key])[-50:]
        self.db.put_state("_gw", "reject_noted", seen)
        try:
            self.note("reject", "# Phone app frame refused\n\nDevice %s sent a `%s` frame the gateway refuses: %s.\n"
                      "The app keeps it and replays it on every connect, so whatever he tapped has not arrived. "
                      "Fix the mismatch (protocol/schema + phonegw), restart the gateway, then check gw.log for no "
                      "new `rejected a frame` lines.\n" % (dev_id, t[:40], str(msg)[:200]))
        except OSError:
            pass

    def note(self, kind, text):
        os.makedirs(self.paths.inbox, exist_ok=True)
        path = os.path.join(self.paths.inbox, "phone-%s-%s-%s.md" % (kind, time.strftime("%Y%m%d-%H%M%S"), secrets.token_hex(2)))
        write_private(path, text)
        return path

    async def send_direct(self, conn, t, body, ack=False):
        env = {"v": 1, "id": ulid(), "t": t, "ts": now_s(), "ack": ack, "body": body}
        self.proto.check("s2c", env)
        async with conn.lock:
            await conn.ws.send_str(json.dumps(env, ensure_ascii=False))

    async def deliver(self, dev_id, msg_id):
        conn = self.conns.get(dev_id)
        if not conn:
            return False
        if conn.replaying:
            return True     # the replay loop picks it up in order
        row = self.db.one("SELECT * FROM outbox WHERE id=?", msg_id)
        if not row or row["body"] is None:
            return True
        return await self._send_row(conn, row)

    async def _send_row(self, conn, row):
        async with conn.lock:
            if row["id"] in conn.sent:
                return True
            try:
                await conn.ws.send_str(json.dumps(self.env_of(row), ensure_ascii=False))
            except (ConnectionError, RuntimeError):
                return False
            conn.sent.add(row["id"])
        now = now_ms()
        self.db.mark_sent(row["id"], now)
        self.last_data[conn.dev] = now
        if row["t"] in ("push", "probe"):
            self.db.x("UPDATE push_log SET delivered_ms=? WHERE id=? AND delivered_ms IS NULL", now - row["created"], row["id"])
        return True

    async def queue(self, dev_id, t, body, ttl, key=None, ack=True, msg_id=None, gate=True):
        """Validate, store in the outbox, send if online. -> (id, online). A push in quiet hours / on a sleep-in morning
        (quiet.py) -> (id, "held") or (id, "dropped") when moot; a push gets its show_until when it goes out."""
        msg_id = msg_id or ulid()
        verdict, rel = self.hold_verdict(body) if t == "push" and gate else ("send", None)
        if t == "push" and "show_until" not in body:     # auto limit from the moment it goes out (a release for a held one)
            body = dict(body, show_until=self.show_until_for(body.get("level"), ttl, rel or now_s(), live=body.get("live")))
        env = {"v": 1, "id": msg_id, "t": t, "ts": now_s(), "ack": ack, "ttl": int(ttl), "body": body}
        if key:
            env["key"] = key
        self.proto.check("s2c", env)
        if verdict == "hold":
            self.hold_push(dev_id, msg_id, body, ttl, key, rel)
            return msg_id, "held"
        if verdict == "drop":
            return msg_id, "dropped"
        if t == "push" and key:
            self.drop_held(key, dev_id)
        self.db.enqueue(dev_id, msg_id, t, body, ttl, key, ack)
        online = await self.deliver(dev_id, msg_id)
        return msg_id, online

    async def wait_ack(self, ids, timeout):
        for i in ids:     # the ack may have landed while the send was still awaiting
            r = self.db.one("SELECT created, sent, acked FROM outbox WHERE id=? AND acked>0", i)
            if r:
                return {"id": i, "acked_ms": r["acked"] - r["created"], "rtt_ms": r["acked"] - (r["sent"] or r["created"])}
        futs = []
        loop = asyncio.get_running_loop()
        for i in ids:
            f = self.waiters.get(i) or loop.create_future()
            self.waiters[i] = f
            futs.append(f)
        try:
            done, _ = await asyncio.wait(futs, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            return next(iter(done)).result() if done else None
        finally:
            for i in ids:
                self.waiters.pop(i, None)

    async def on_ack(self, dev_id, ids):
        at = now_ms()
        for r in self.db.ack(dev_id, ids, at):
            if r["t"] in ("push", "probe"):
                self.db.x("UPDATE push_log SET acked_ms=?, delivered_ms=COALESCE(delivered_ms, ?) WHERE id=?",
                          at - r["created"], (r["sent"] or at) - r["created"], r["id"])
            f = self.waiters.get(r["id"])
            if f and not f.done():
                f.set_result({"id": r["id"], "acked_ms": at - r["created"], "rtt_ms": at - (r["sent"] or r["created"])})

    # ------------------------------------------------------------ middlewares
    @web.middleware
    async def auth_mw(self, request, handler):
        public = self.is_public(request)
        if request.path in OPEN_PATHS and not public:          # the internet path has no open paths
            return await handler(request)
        sslobj = request.transport.get_extra_info("ssl_object") if request.transport else None
        der = sslobj.getpeercert(binary_form=True) if sslobj else None
        if not der:
            return jerr(401, "cert", "client certificate required")
        dev = self.db.device_by_fp(pki.fingerprint(der))
        peer = request.transport.get_extra_info("peername") if public else None
        if not dev or dev["status"] != "active":
            log.warning("refused a client certificate (unknown or revoked device)%s", " on the public path" if public else "")
            if peer:
                self.guard.mark(peer[1], False)
            return jerr(403, "device", "device unknown or revoked")
        if public:
            if peer:
                self.guard.mark(peer[1], True)
            if request.path in (self.cfg["public"].get("deny_paths") or []):
                return jerr(404, "not_found")
        request["device"] = dev
        request["route"] = "public" if public else "lan"
        return await handler(request)

    @web.middleware
    async def bearer_mw(self, request, handler):
        h = request.headers.get("Authorization", "")
        if not (h.startswith("Bearer ") and hmac.compare_digest(h[7:].strip().encode(), self.token.encode())):
            return jerr(401, "auth")
        return await handler(request)

    # ------------------------------------------------------------ app API
    async def x_health(self, request):
        return web.json_response({"ok": True, "api": 1})

    def rate(self, ip, per_min):
        q, now = self.pair_rl[ip], time.monotonic()
        while q and q[0] < now - 60:
            q.popleft()
        if len(q) >= per_min:
            return True
        q.append(now)
        return False

    async def x_pair(self, request):
        pc = self.cfg["pair"]
        if self.rate(self.client_ip(request), pc["per_ip_per_min"]):
            return jerr(429, "rate", retry_s=60)
        try:
            body = await request.json()
            self.proto.check_http("pair.request", body)
        except ProtocolError as e:
            return jerr(422, "schema", e.msg, problems=e.problems)
        except Exception:
            return jerr(400, "bad_json")
        code = normalize_code(body["code"])
        now = now_s()
        h = hashlib.sha256((code or "").encode()).hexdigest()
        row = self.db.one("SELECT * FROM pair_codes WHERE code_sha256=? AND used=0 AND expires>? AND attempts<?",
                          h, now, int(pc["max_attempts"])) if code else None
        if not row:
            # brute-force guard: every wrong try counts against every live code (5 tries and it's dead)
            self.db.x("UPDATE pair_codes SET attempts=attempts+1 WHERE used=0 AND expires>?", now)
            log.warning("pair: wrong, used or expired code")
            return jerr(403, "code", "pairing code wrong, used or expired")
        try:
            if body.get("bio_pub"):
                pki.parse_spki(body["bio_pub"])
            dev_id = "d_" + short_id(6)
            cert, fp = pki.issue_device(self.paths, body["pub"], dev_id, int(self.cfg["device_cert_days"]))
        except pki.PairError as e:
            return jerr(422, e.args[0], "public key must be EC P-256 SubjectPublicKeyInfo (base64 DER)")
        self.db.x("UPDATE pair_codes SET used=1 WHERE code_sha256=?", h)
        self.db.x("INSERT INTO devices(id, name, model, cert_sha256, bio_pub, created, last_seen, app_code, status) "
                  "VALUES(?,?,?,?,?,?,?,?,'active')", dev_id, row["name"] or body["name"], body.get("model"), fp,
                  body.get("bio_pub"), now, None, body.get("app_code"))
        with open(self.paths.ca_pem) as fh:
            ca = fh.read()
        resp = {"device_id": dev_id, "cert": cert, "ca": ca, "config": C.app_config(self.cfg)}
        self.proto.check_http("pair.response", resp)
        log.info("paired device %s", dev_id)
        return web.json_response(resp)

    async def x_ws(self, request):
        dev = request["device"]
        dev_id = dev["id"]
        ws = web.WebSocketResponse(autoping=False, max_msg_size=self.proto.max_frame, compress=False)
        await ws.prepare(request)
        old = self.conns.get(dev_id)
        conn = Conn(ws, dev_id)
        conn.route = request.get("route", "lan")
        self.last_route[dev_id] = conn.route
        if conn.route == "public":
            self.public_open(dev_id)
        self.conns[dev_id] = conn
        if old:
            self.spawn(old.ws.close(code=4000, message=b"replaced"))
        silence = self.ws_silence_s()
        log.info("device %s connected", dev_id)
        try:
            self.touch(dev_id)
            await self.send_direct(conn, "hello", {"server": "phonegw/" + __version__, "device_id": dev_id, "time": now_s(),
                                                   "tz": self.cfg["tz"], "config_rev": int(self.cfg["config_rev"]),
                                                   "pending": self.db.pending_count(dev_id)})
            await self.send_direct(conn, "config", C.app_config(self.cfg))
            while True:      # replay in order; anything queued meanwhile is caught by the next query
                rows = [r for r in self.db.pending(dev_id) if r["id"] not in conn.sent]
                if not rows:
                    conn.replaying = False
                    break
                for r in rows:
                    if not await self._send_row(conn, r):
                        raise ConnectionError
            while True:
                try:
                    msg = await ws.receive(timeout=silence)
                except asyncio.TimeoutError:
                    log.info("device %s silent for %s s: closing", dev_id, silence)
                    await ws.close(code=4001, message=b"silent")
                    break
                if msg.type == WSMsgType.PING:
                    await ws.pong(msg.data)
                    self.touch(dev_id)
                    continue
                if msg.type == WSMsgType.PONG:
                    self.touch(dev_id)
                    continue
                if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
                    break
                if msg.type != WSMsgType.TEXT:
                    await self.send_direct(conn, "error", {"code": "schema", "ref": None, "msg": "text frames only"})
                    continue
                await self.on_frame(conn, dev, msg.data)
        except (ConnectionError, RuntimeError, asyncio.CancelledError):
            pass
        finally:
            if self.conns.get(dev_id) is conn:
                self.conns.pop(dev_id, None)
                self.db.x("UPDATE devices SET last_seen=? WHERE id=?", now_s(), dev_id)
            if conn.ack_task:
                conn.ack_task.cancel()
            if conn.route == "public":
                self.public_close(dev_id, conn)
            log.info("device %s disconnected%s", dev_id, " (public)" if conn.route == "public" else "")
        return ws

    async def on_frame(self, conn, dev, data):
        try:
            env = json.loads(data)
        except ValueError:
            await self.send_direct(conn, "error", {"code": "bad_json", "ref": None})
            return
        ref = env.get("id") if isinstance(env, dict) and isinstance(env.get("id"), str) and len(env["id"]) <= 40 else None
        try:
            self.proto.check("c2s", env)
        except ProtocolError as e:
            log.info("device %s: rejected a frame (%s)", dev["id"], e.msg)
            self.reject_note(dev["id"], env, e.msg)
            await self.send_direct(conn, "error", {"code": e.code, "ref": ref, "msg": e.msg[:200]})
            return
        self.last_data[dev["id"]] = now_ms()
        self.touch(dev["id"])
        await self.handle_c2s(dev, env)
        if env.get("ack") and env["t"] != "ack":
            conn.ack_buf.append(env["id"])
            if not conn.ack_task or conn.ack_task.done():
                conn.ack_task = self.spawn(self._flush_acks(conn))

    async def _flush_acks(self, conn):
        await asyncio.sleep(0.2)
        ids, conn.ack_buf = conn.ack_buf[:256], conn.ack_buf[256:]
        if ids:
            try:
                await self.send_direct(conn, "ack", {"ids": ids})
            except (ConnectionError, RuntimeError):
                pass
        if conn.ack_buf:
            conn.ack_task = self.spawn(self._flush_acks(conn))

    async def handle_c2s(self, dev, env):
        """-> True if a duplicate (already processed)."""
        dev_id, t, b = dev["id"], env["t"], env["body"]
        if t == "ack":
            await self.on_ack(dev_id, b["ids"])
            return False
        if self.db.seen_inbound(env["id"], dev_id, t):
            return True
        if t == "hello":
            self.db.x("UPDATE devices SET app_code=?, model=COALESCE(?, model) WHERE id=?", b.get("code"), b.get("model"), dev_id)
            self.db.put_state(dev_id, "hello", b)
            await self.m2_hello(dev_id)
        elif t == "state":
            await self.m2_state(dev_id, b)
            if "app" in b:
                self.battery_ingest(dev_id, b)
        elif t == "event" and not self.m4_accept(dev_id, b):
            pass                                   # health.daily while config health is off -> dropped, not stored
        elif t == "event":
            self.db.x("INSERT OR IGNORE INTO events(id, device_id, kind, at, body, received) VALUES(?,?,?,?,?,?)",
                      env["id"], dev_id, b["kind"], b.get("at") or env["ts"], json.dumps(b), now_s())
            await self.m2_event(dev_id, env)
            await self.m4_event(dev_id, env)
        elif t == "answer":
            self.m2_answer(dev_id, b)
        elif t == "share":
            if self.paperwork(b):
                log.warning("device %s: share refused (paperwork)", dev_id)
            else:
                await self.on_share(dev_id, b)
        elif t == "action":
            await self.on_action(dev_id, b)
        elif t == "chat":
            await self.on_chat(dev_id, b)
        elif t == "card.answer":
            self.on_card_answer(dev_id, b)
        elif t == "selftest":
            self.on_selftest(dev_id, b)
        elif t == "crash":
            self.on_crash(dev_id, b)
        elif t == "usage":
            self.on_usage(env)
        elif t == "call.state":
            await self.on_call_state(dev_id, b)
        elif t == "open.result":
            self.on_open_result(dev_id, b)
        return False

    async def on_action(self, dev_id, b):
        row = self.db.one("SELECT * FROM push_log WHERE id=? AND device_id=?", b["push_id"], dev_id)
        if not row or not hmac.compare_digest(row["nonce"] or "", b["nonce"]):
            log.warning("device %s: action for an unknown push or nonce: ignored", dev_id)
            return
        offered = json.loads(row["verbs"] or "[]")
        verb, arg = b["verb"], b.get("arg")
        if verb != "dismissed":
            match = [a for a in offered if a["verb"] == verb]
            if match:
                arg = match[0].get("arg")
            elif verb == "open":
                arg = None          # a tap on the notification body (every push has one): counts as seen, no tool runs
            else:
                log.warning("device %s: verb %s was not offered: ignored", dev_id, verb)
                return
        if row["verb"]:
            return      # a nonce works once
        self.db.x("UPDATE push_log SET verb=?, arg=?, action_at=? WHERE id=?", verb, arg, now_ms(), row["id"])
        await self.on_ack(dev_id, [row["id"]])
        log.info("device %s: action %s on push %s", dev_id, verb, row["tag"])
        act = {"push_id": row["id"], "nonce": row["nonce"], "tag": row["tag"], "level": row["level"], "verb": verb, "arg": arg,
               "at": b.get("at") or now_s(), "device_id": dev_id}
        if self.cfg.get("bridge", True):
            self.spawn(bridge.run(act, int(self.cfg["hook_timeout_s"])))
        await self.run_hook(act)

    async def run_hook(self, payload):
        argv = self.cfg.get("action_hook")
        if os.environ.get("PHONEGW_ACTION_HOOK"):
            argv = [os.environ["PHONEGW_ACTION_HOOK"]]
        if not argv:
            return None
        try:
            p = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
                                                     stderr=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(p.communicate(json.dumps(payload).encode()), int(self.cfg["hook_timeout_s"]))
            if p.returncode:
                log.warning("action hook exit %s", p.returncode)
            return p.returncode
        except (OSError, asyncio.TimeoutError) as e:
            log.warning("action hook failed: %s", type(e).__name__)
            return None

    async def on_chat(self, dev_id, b):
        self.db.x("INSERT OR IGNORE INTO chat(id, at, dir, text, reply_to) VALUES(?,?,?,?,?)", b["msg_id"], now_s(), "in",
                  b["text"], b.get("reply_to"))
        task = b["text"] if not b.get("data") else "[button %s] %s" % (b["data"], b["text"])
        log.info("device %s: chat in (%d chars)", dev_id, len(b["text"]))
        if await self.plugin_handoff("chat", task, msg_id=b["msg_id"], device_id=dev_id):   # Bearer token: m2
            return "plugin"
        self.note("chat", "# Phone chat (his app)\n\nFrom his phone app (device %s), msg_id %s, %s.\n"
                          "Treat it like his Telegram message. Answer with: phone chat --reply-to %s \"...\"\n\n%s\n"
                  % (dev_id, b["msg_id"], time.strftime("%d.%m %H:%M"), b["msg_id"], task))
        return "note"

    def on_selftest(self, dev_id, b):
        for c in b["checks"]:
            c.setdefault("ok", None)            # the app's serializer drops null fields: missing = unknown
        self.db.put_state(dev_id, "selftest", b)
        reds = sorted({c["id"] for c in b["checks"] if c["ok"] is False and not c.get("optional")})   # optional: no note
        day = time.strftime("%Y-%m-%d")
        noted, _ = self.db.get_state(dev_id, "selftest_noted", {})
        done = set(noted.get("ids", [])) if noted.get("day") == day else set()
        new = [r for r in reds if r not in done]
        if new:
            self.note("selftest", "# Phone self-test: red checks\n\nDevice %s: %s.\nGreen: %d of %d. Fix steps are in the app's "
                                  "Settings checklist (spec §4.6).\n" % (dev_id, ", ".join(new),
                                                                          sum(1 for c in b["checks"] if c["ok"]), len(b["checks"])))
            self.db.put_state(dev_id, "selftest_noted", {"day": day, "ids": sorted(done | set(new))})

    def on_usage(self, env):
        """App analytics -> the usage spool, off the event loop (a short file append; failures only logged)."""
        if self.usage_fwd is None:
            self.usage_fwd = UsageForwarder(state=self.cfg.get("usage_state"), enabled=self.cfg.get("usage", True))
        try:
            asyncio.get_running_loop().run_in_executor(None, self.usage_fwd.forward, env["id"], env["body"])
        except RuntimeError:
            self.usage_fwd.forward(env["id"], env["body"])

    def on_crash(self, dev_id, b):
        day = time.strftime("%Y-%m-%d")
        st, _ = self.db.get_state(dev_id, "crash_noted", {})
        n = st.get("n", 0) if st.get("day") == day else 0
        if n < 5:
            self.note("crash", "# Phone app crash\n\nDevice %s, app %s: %s at %s.\n" % (dev_id, b["app"], b["exc"], b.get("where") or "?"))
        self.db.put_state(dev_id, "crash_noted", {"day": day, "n": n + 1})

    async def x_sync(self, request):
        dev_id = request["device"]["id"]
        after = request.query.get("after")
        rows = self.db.pending(dev_id, after=after, limit=201)
        more = len(rows) > 200
        out = []
        for r in rows[:200]:
            out.append(self.env_of(r))
            self.db.mark_sent(r["id"])
            if r["t"] in ("push", "probe"):
                self.db.x("UPDATE push_log SET delivered_ms=? WHERE id=? AND delivered_ms IS NULL", now_ms() - r["created"], r["id"])
        self.touch(dev_id)
        return web.json_response({"messages": out, "more": more, "config_rev": int(self.cfg["config_rev"])})

    async def x_batch(self, request):
        dev = request["device"]
        allowed = BATCH_ALLOWED[request.path]
        try:
            body = await request.json()
            self.proto.check_http("batch.request", body)
        except ProtocolError as e:
            return jerr(422, "schema", e.msg, problems=e.problems)
        except Exception:
            return jerr(400, "bad_json")
        problems, good = [], []
        for i, env in enumerate(body["items"]):
            try:
                self.proto.check("c2s", env)
                if env["t"] not in allowed:
                    problems.append("items.%d: type not allowed on %s" % (i, request.path))
                    self.reject_note(dev["id"], env, "type not allowed on %s" % request.path)
                else:
                    good.append(env)
            except ProtocolError as e:
                problems.append("items.%d %s" % (i, e.msg))
                self.reject_note(dev["id"], env, e.msg)
        # The valid items are handled even when others are refused: one bad frame must not hold back the rest of an
        # offline queue. A refused batch stays in the app's outbox and comes back; the good ones then count as dups.
        acc = dup = 0
        for env in good:
            if await self.handle_c2s(dev, env):
                dup += 1
            else:
                acc += 1
        if good:
            self.touch(dev["id"])
        if problems:
            log.info("device %s: refused %d of %d frames on %s (%s)", dev["id"], len(problems), len(body["items"]),
                     request.path, problems[0])
            return jerr(422, "schema", problems[0], problems=problems[:20])
        return web.json_response({"ok": True, "accepted": acc, "dup": dup})

    # ------------------------------------------------------------ internal API
    async def _json(self, request):
        try:
            b = await request.json()
            return b if isinstance(b, dict) else None
        except Exception:
            return None

    async def i_push(self, request):
        r = await self._json(request)
        if r is None:
            return jerr(400, "usage", "JSON object expected")
        level = r.get("level")
        title, body = str(r.get("title") or ""), str(r.get("body") or "")
        if level not in LEVELS or not title:
            return jerr(400, "usage", "level urgent|timed|decision|info and a title are required")
        actions = r.get("actions") or []
        if not isinstance(actions, list) or len(actions) > 3:
            return jerr(400, "usage", "at most 3 actions")
        out_actions = []
        for a in actions:
            if not isinstance(a, dict) or a.get("verb") not in VERBS:
                return jerr(403, "verb", "verb not allowed (allowed: %s)" % ", ".join(VERBS))
            label = str(a.get("label") or a["verb"])[:24]
            x = {"verb": a["verb"], "label": label, "unlock": a["verb"] in UNLOCK}
            if a.get("arg") is not None:
                if not ARG_RE.match(str(a["arg"])):
                    return jerr(403, "arg", "action arg must match [A-Za-z0-9_:.+-]{1,64}")
                x["arg"] = str(a["arg"])
            out_actions.append(x)
        car = bool(r.get("car"))                   # Android Auto counts as the lock screen
        t2, b2, problems, counts = lint(title, body, level == "urgent" or car)
        if problems:
            return jerr(422, "lint", "not lock-screen safe: say it without identifiers, amounts, links or long numbers",
                        problems=problems)
        tag = clean_tag(r.get("tag") or "claude-" + hashlib.sha1(title.encode()).hexdigest()[:8])
        nonce = secrets.token_hex(4)
        pb = {"level": level, "channel": level, "tag": tag, "title": t2[:120], "body": b2[:1000],
              "private": bool(r.get("private")) or level != "urgent", "sticky": bool(r.get("sticky")),
              "full_screen": bool(r.get("full_screen")) and level == "urgent", "nonce": nonce}
        if out_actions:
            pb["actions"] = out_actions
        if car:
            pb["car"] = True
        if r.get("live"):
            lv = r["live"]
            if not (isinstance(lv, dict) and lv.get("kind") == "countdown" and isinstance(lv.get("until"), int)):
                return jerr(400, "usage", "live = {kind: countdown, until: epoch}")
            pb["live"] = {"kind": "countdown", "until": lv["until"]}
        if r.get("deep_link"):
            pb["deep_link"] = str(r["deep_link"])
        link = r.get("link")
        if link:
            if not isinstance(link, dict) or not safe_link(link.get("url")):
                return jerr(403, "link", "link url must be https:// (no userinfo), geo: or google.navigation:, "
                                         "no spaces, at most 2048 chars")
            pb["link"] = {"url": link["url"]}
            if str(link.get("label") or "").strip():
                pb["link"]["label"] = str(link["label"]).strip()[:24]
        try:
            ttl = int(r.get("ttl") or self.cfg["ttl_s"][level])
        except (TypeError, ValueError):
            return jerr(400, "usage", "ttl in seconds")
        if r.get("night_ok"):
            pb["night_ok"] = True                  # G5: may go out (and sound) in quiet hours: the Buds guard and alike
        try:
            if r.get("show_until") or r.get("show_s"):
                _, rel = self.hold_verdict(pb)     # show_s counts from the moment it goes out
                pb["show_until"] = self.show_until_for(level, ttl, rel or now_s(), req=r)
                if pb["show_until"] <= now_s():
                    return jerr(400, "usage", "show_until is in the past")
        except (TypeError, ValueError):
            return jerr(400, "usage", "show_until = epoch s, show_s = seconds")
        devs = self.db.active_devices()
        if not devs:
            return web.json_response({"ok": False, "status": "no_device", "tag": tag})
        if "link" in pb:                           # older apps would open themselves on the tap, not the link
            devs = [d for d in devs if "link" in (self.db.get_state(d["id"], "hello", {})[0].get("caps") or [])]
            if not devs:
                return web.json_response({"ok": False, "status": "unsupported", "tag": tag})
        ids, online, gated = [], False, None
        for d in devs:
            try:
                mid, on = await self.queue(d["id"], "push", pb, ttl, key="push:" + tag)
            except ProtocolError as e:
                return jerr(400, "usage", e.msg, problems=e.problems)
            if on in ("held", "dropped"):          # G5: quiet hours / sleep-in morning
                gated = on
                if on == "dropped":
                    continue
                on = False
            self.db.x("INSERT INTO push_log(id, at, level, tag, device_id, nonce, verbs) VALUES(?,?,?,?,?,?,?)",
                      mid, now_ms(), level, tag, d["id"], nonce,
                      json.dumps([{"verb": a["verb"], "arg": a.get("arg")} for a in out_actions]))
            ids.append(mid)
            online = online or on
        if gated:
            row = self.db.one("SELECT MIN(release) rel FROM held WHERE key=?", "push:" + tag)
            res = {"ok": True, "status": gated, "tag": tag, "ids": ids, "online": False, "masked": counts,
                   "hold_until": row["rel"] if row and gated == "held" else None}
            if ids:
                res.update(id=ids[0], nonce=nonce)
            if "link" in pb:
                res["link"] = True
            return web.json_response(res)
        res = {"ok": True, "id": ids[0], "ids": ids, "tag": tag, "nonce": nonce, "online": online, "masked": counts}
        if "link" in pb:
            res["link"] = True                     # `phone link`: an older gateway drops the link silently
        wait = min(float(r.get("wait") or 0), 120.0)
        if not online:
            res["status"] = "queued"
        elif wait > 0:
            got = await self.wait_ack(ids, wait)
            res["status"] = "acked" if got else "timeout"
            if got:
                res["acked_ms"] = got["acked_ms"]
        else:
            res["status"] = "sent"
        return web.json_response(res)

    async def i_cancel(self, request):
        r = await self._json(request) or {}
        tag = clean_tag(r.get("tag") or "")
        if not tag:
            return jerr(400, "usage", "tag required")
        dropped = self.db.drop_unsent_tag("push:" + tag) + self.drop_held("push:" + tag)
        ids = []
        for d in self.db.active_devices():
            mid, _ = await self.queue(d["id"], "push.cancel", {"tag": tag}, self.cfg["ttl_s"]["cancel"], key="cancel:" + tag)
            ids.append(mid)
        return web.json_response({"ok": True, "tag": tag, "dropped_unsent": dropped, "ids": ids})

    async def i_chat(self, request):
        r = await self._json(request)
        if not r or not str(r.get("text") or "").strip():
            return jerr(400, "usage", "text required")
        msg_id = "m_" + ulid()
        body = {"msg_id": msg_id, "text": str(r["text"])[:4000]}
        if r.get("reply_to"):
            if APP_MSG_ID.fullmatch(str(r["reply_to"])):
                body["reply_to"] = str(r["reply_to"])
            else:                                # e.g. a hand-off id (share, card pick): not an app message
                log.info("chat: reply_to is not an app message id; sent without it")
        if r.get("buttons"):
            body["buttons"] = chat_buttons(r["buttons"])
        devs = self.db.active_devices()
        if not devs:
            return web.json_response({"ok": False, "status": "no_device"})
        ids, online = [], False
        for d in devs:
            try:
                mid, on = await self.queue(d["id"], "chat", body, self.cfg["ttl_s"]["chat"])
            except ProtocolError as e:
                return jerr(400, "usage", e.msg, problems=e.problems)
            ids.append(mid)
            online = online or on
        self.db.x("INSERT INTO chat(id, at, dir, text, reply_to) VALUES(?,?,?,?,?)", msg_id, now_s(), "out", body["text"],
                  body.get("reply_to"))
        res = {"ok": True, "msg_id": msg_id, "id": ids[0], "online": online, "status": "sent" if online else "queued"}
        wait = min(float(r.get("wait") or 0), 120.0)
        if online and wait > 0:
            got = await self.wait_ack(ids, wait)
            res["status"] = "acked" if got else "timeout"
        return web.json_response(res)

    async def i_pair_code(self, request):
        r = await self._json(request) or {}
        code = gen_code()
        ttl = int(self.cfg["pair"]["code_ttl_s"])
        exp = now_s() + ttl
        self.db.x("INSERT INTO pair_codes(code_sha256, expires, attempts, used, name) VALUES(?,?,0,0,?)",
                  hashlib.sha256(normalize_code(code).encode()).hexdigest(), exp, (str(r.get("name") or "")[:32] or None))
        log.info("pair code issued (valid %d s)", ttl)
        return web.json_response({"ok": True, "code": code, "expires": exp, "ttl_s": ttl})

    async def i_revoke(self, request):
        r = await self._json(request) or {}
        dev_id = str(r.get("device_id") or "")
        if not self.db.device(dev_id):
            return jerr(404, "device", "no such device")
        self.db.x("UPDATE devices SET status='revoked' WHERE id=?", dev_id)
        c = self.conns.pop(dev_id, None)
        if c:
            await c.ws.close(code=4003, message=b"revoked")
        log.info("device %s revoked", dev_id)
        return web.json_response({"ok": True, "device_id": dev_id, "closed": bool(c)})

    def device_info(self, d):
        dev_id = d["id"]
        hello, _ = self.db.get_state(dev_id, "hello", {})
        batt, batt_at = self.db.get_state(dev_id, "battery", None)
        st, st_at = self.db.get_state(dev_id, "selftest", None)
        conn = self.conns.get(dev_id)
        probes = self.db.all("SELECT tag, at, acked_ms, delivered_ms FROM push_log WHERE device_id=? AND level='probe' "
                             "ORDER BY at DESC LIMIT 40", dev_id)
        recent = [p["acked_ms"] for p in probes if p["tag"] == "probe"][:20]
        pushes = [r["acked_ms"] for r in self.db.all(
            "SELECT acked_ms FROM push_log WHERE device_id=? AND level!='probe' AND acked_ms IS NOT NULL AND at>?",
            dev_id, now_ms() - 30 * 86400 * 1000)]
        info = {"id": dev_id, "name": d["name"], "model": d["model"], "status": d["status"], "online": bool(conn),
                "route": conn.route if conn else None, "last_route": self.last_route.get(dev_id),
                "connected_s": int(time.time() - conn.since) if conn else None, "last_seen": d["last_seen"],
                "app": hello.get("app"), "app_code": d["app_code"], "sdk": hello.get("sdk"),
                "rtt_ms": recent[0] if recent else None, "probe_p95_ms": p95(recent),
                "push_p95_ms": p95(pushes), "pushes_acked_30d": len(pushes), "keepalive_s": int(self.cfg["keepalive_s"]),
                "battery_band": (batt or {}).get("band"), "battery_level": (batt or {}).get("level"),
                "battery_exempt": hello.get("battery_exempt"), "perms": hello.get("perms"),
                "pending": self.db.pending_count(dev_id),
                "idle_probes": [{"tag": p["tag"], "at": p["at"] // 1000, "acked_ms": p["acked_ms"]}
                                for p in probes if (p["tag"] or "").startswith("idle-")][:8]}
        try:
            info["app_budget"] = self.battery_view(dev_id)
        except Exception as e:                       # status must never fail on the budget view
            log.warning("battery_view: %s", type(e).__name__)
        if st:
            info["selftest"] = {"green": sum(1 for c in st["checks"] if c["ok"]), "total": len(st["checks"]),
                                "red": [c["id"] for c in st["checks"] if c["ok"] is False and not c.get("optional")],
                                "optional_red": [c["id"] for c in st["checks"] if c["ok"] is False and c.get("optional")],
                                "unknown": [c["id"] for c in st["checks"] if c["ok"] is None], "at": st_at}
        return info

    async def i_status(self, request):
        return web.json_response({
            "ok": True, "server": "phonegw/" + __version__, "pid": os.getpid(), "uptime_s": int(time.time() - self.started),
            "ports": self.ports, "front": self.front.status() if self.front else {"on": False, "wanted": self.front_wanted()},
            "held": self.held_count(), "devices": [self.device_info(d) for d in self.db.devices()]})

    async def i_devices(self, request):
        return web.json_response({"ok": True, "devices": [
            {"id": d["id"], "name": d["name"], "model": d["model"], "status": d["status"], "created": d["created"],
             "last_seen": d["last_seen"], "online": d["id"] in self.conns, "app_code": d["app_code"]} for d in self.db.devices()]})

    async def i_export(self, request):
        """GET /internal/export?since=MS: the allow-listed slice the brain mirrors (phonegw/export.py)."""
        return web.json_response(export.export(self.db, request.query.get("since")))

    async def i_pushes(self, request):
        try:
            limit = max(1, min(int(request.query.get("limit", "20")), 200))
        except ValueError:
            limit = 20
        q, args = "SELECT id, at, level, tag, delivered_ms, acked_ms, verb, arg, action_at, device_id FROM push_log", []
        conds = []
        if request.query.get("id"):
            conds.append("id=?")
            args.append(request.query["id"])
        if request.query.get("tag"):
            conds.append("tag=?")
            args.append(clean_tag(request.query["tag"]))
        if not request.query.get("probes"):
            conds.append("level!='probe'")
        if conds:
            q += " WHERE " + " AND ".join(conds)
        rows = self.db.all(q + " ORDER BY at DESC LIMIT ?", *args, limit)
        return web.json_response({"ok": True, "pushes": [dict(r) for r in rows]})

    async def probe_once(self, dev_id, tag, timeout=10):
        mid, online = await self.queue(dev_id, "probe", {"probe_id": "p_" + ulid()}, self.cfg["ttl_s"]["probe"])
        self.db.x("INSERT INTO push_log(id, at, level, tag, device_id) VALUES(?,?,?,?,?)", mid, now_ms(), "probe", tag, dev_id)
        if not online:
            return None
        got = await self.wait_ack([mid], timeout)
        return got["rtt_ms"] if got else None

    async def i_probe(self, request):
        r = await self._json(request) or {}
        devs = [d for d in self.db.active_devices() if d["id"] in self.conns]
        if not devs:
            return web.json_response({"ok": False, "status": "offline"})
        dev_id = devs[0]["id"]
        if r.get("idle"):
            marks = [int(m) for m in r["idle"] if 0 < int(m) <= 180][:6]
            self.spawn(self.idle_probes(dev_id, marks))
            return web.json_response({"ok": True, "status": "scheduled", "device_id": dev_id, "idle_min": marks})
        n = max(1, min(int(r.get("n") or 5), 50))
        gap = max(0.0, min(float(r.get("gap_s") or 0.3), 10.0))
        rtts = []
        for i in range(n):
            rtts.append(await self.probe_once(dev_id, "probe"))
            if i < n - 1:
                await asyncio.sleep(gap)
        ok = [x for x in rtts if x is not None]
        return web.json_response({"ok": True, "status": "done", "device_id": dev_id, "rtt_ms": rtts, "lost": len(rtts) - len(ok),
                                  "p50_ms": sorted(ok)[len(ok) // 2] if ok else None, "p95_ms": p95(ok)})

    async def idle_probes(self, dev_id, marks):
        """One probe each time the socket has been idle (no data frames) for m minutes: WG/NAT wake-up latency."""
        for m in marks:
            deadline = time.time() + m * 60 + 3 * 3600
            while time.time() < deadline:
                last = self.last_data.get(dev_id, 0) / 1000
                if dev_id in self.conns and time.time() - last >= m * 60:
                    break
                await asyncio.sleep(5)
            rtt = await self.probe_once(dev_id, "idle-%dm" % m, timeout=60) if dev_id in self.conns else None
            log.info("idle probe %d min: %s", m, "%s ms" % rtt if rtt is not None else "no ack")


async def serve(paths, front=True, ready=None):
    gw = Gateway(paths)
    if os.environ.get("PHONEGW_HUB_MODE") == "1":       # claude-hub-phone (phoneloader): every way into the brain via the hub
        from . import hubmode
        hubmode.install(gw)
    await gw.start(front=front)
    write_private(paths.pid, str(os.getpid()) + "\n", 0o644)
    if ready:
        ready(gw)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for s in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(s, stop.set)
    loop.add_signal_handler(signal.SIGHUP, lambda: gw.spawn(gw.reload()))
    await stop.wait()
    log.info("stopping")
    await gw.stop()
    if getattr(gw, "hub", None) is not None:          # hub mode's client session
        await gw.hub.close()
    try:
        if open(paths.pid).read().strip() == str(os.getpid()):
            os.unlink(paths.pid)
    except OSError:
        pass
