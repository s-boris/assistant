"""voice-relay: the phone page + websocket (public, behind the reverse proxy) and a local-only internal API.

Public (cfg.public_host:public_port): /login (one-time link or passphrase), /, /static/*, /api/ticket, /ws.
Internal (127.0.0.1:internal_port, bearer = ~/.secrets/voice-local-token): /internal/answer, /internal/link,
/internal/status, /internal/health (get_me on the call account's live connection, for voicecheck), /internal/call, /internal/ticket (phone app calls: phonegw's /v1/voice proxy gets a one-use
ticket for /ws from loopback; reason/say = an in-app ring he answered, opened like an outbound call; app calls
don't count toward the daily web-page minutes). Usage: voice-relay serve [--fake] [--tg-calls] | init | link | passwd | reply ID TEXT | status
"""
import argparse
import asyncio
import dataclasses
import hashlib
import hmac
import json
import os
import secrets
import sys
import tempfile
import time
from collections import defaultdict, deque
from datetime import date, datetime

import aiohttp
from aiohttp import WSMsgType, web

from .config import Config, owner_telegram_id, read_secret
from .handoff import Handoff
from .providers import ProviderError, make_provider
from .answer_filter import filter_answer
from .calllog import CallLog
from .greet import Greeter
from .session import FrontEnd, VoiceSession

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "static")
PUBLIC_STATIC = {"app.css"}
PRIVATE_STATIC = {"app.js", "worklet.js"}
COOKIE = "vr_sid"
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; "
       "media-src 'self' blob:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
SEC_HEADERS = {"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
               "X-Frame-Options": "DENY", "Cache-Control": "no-store", "Permissions-Policy": "microphone=(self)"}


def sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


class Limiter:
    def __init__(self):
        self.hits = defaultdict(deque)

    def allow(self, key, limit, window, record=True):
        now = time.monotonic()
        q = self.hits[key]
        while q and q[0] <= now - window:
            q.popleft()
        if len(q) >= limit:
            return False
        if record:
            q.append(now)
        return True

    def hit(self, key):
        self.hits[key].append(time.monotonic())


class State:
    """Small 0600 JSON files in state_dir: login sessions (hashed), daily usage, passphrase hash."""
    def __init__(self, cfg):
        self.dir = cfg.state_dir
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        self.sessions = self._load("sessions.json")
        self.usage = self._load("usage.json")
        self.passphrase = self._load("passphrase.json")

    def _load(self, name):
        try:
            with open(os.path.join(self.dir, name), encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def save(self, name, obj):
        path = os.path.join(self.dir, name)
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)
        os.replace(tmp, path)

    def seconds_today(self):
        return float(self.usage.get(date.today().isoformat(), 0))

    def add_usage(self, seconds):
        d = date.today().isoformat()
        self.usage = {d: self.seconds_today() + max(0.0, seconds)}
        self.save("usage.json", self.usage)


def hash_passphrase(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    return {"salt": salt, "hash": hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1).hex()}


def instructions(cfg):
    with open(cfg.prompt_file, encoding="utf-8") as fh:
        base = fh.read().strip()
    now = datetime.now().astimezone()
    return base + "\n\nNow: %s (UTC%s)." % (now.strftime("%A %d %B %Y, %H:%M"), now.strftime("%z"))


def redirect(loc):
    return web.Response(status=303, headers={"Location": loc})


class WebFrontEnd(FrontEnd):
    in_rate = 16000
    out_rate = 24000

    def __init__(self, ws):
        self.ws = ws

    async def _j(self, obj):
        if not self.ws.closed:
            try:
                await self.ws.send_str(json.dumps(obj))
            except (ConnectionResetError, RuntimeError):
                pass

    async def play(self, pcm):
        if not self.ws.closed:
            try:
                await self.ws.send_bytes(pcm)
            except (ConnectionResetError, RuntimeError):
                pass

    async def flush(self):
        await self._j({"type": "flush"})

    async def event(self, ev):
        await self._j(ev)

    async def hangup(self, reason):
        await self._j({"type": "hangup", "reason": reason})
        if not self.ws.closed:
            try:
                await asyncio.wait_for(self.ws.close(), 3)
            except (asyncio.TimeoutError, ConnectionResetError, RuntimeError):
                pass


class Relay:
    def __init__(self, cfg, provider_factory=make_provider):
        self.cfg = cfg
        self.st = State(cfg)
        self.handoff = Handoff(cfg)
        self.lim = Limiter()
        self.links = {}      # sha(token) -> expiry
        self.tickets = {}    # sha(ticket) -> (expiry, session hash)
        self.app_tickets = {}  # sha(ticket) -> {"exp", "reason", "say"} (phone app, /internal/ticket)
        self.app_live = {}     # app VoiceSession -> its call-log id
        self.live = set()
        self.provider_factory = provider_factory
        self.greeter = Greeter(cfg)    # his calls' pick-up line (greet.py); run() turns on background generation

    # --- helpers -------------------------------------------------------------------------------
    def client_ip(self, request):
        peer = request.remote or ""
        if peer in self.cfg.trusted_proxies:
            xff = [x.strip() for x in request.headers.get("X-Forwarded-For", "").split(",") if x.strip()]
            for ip in reversed(xff):
                if ip not in self.cfg.trusted_proxies:
                    return ip
        return peer

    def session_of(self, request):
        sid = request.cookies.get(COOKIE)
        rec = self.st.sessions.get(sha(sid)) if sid else None
        if not rec or rec.get("exp", 0) < time.time():
            return None
        return sha(sid)

    def origin_ok(self, request):
        o = request.headers.get("Origin", "")
        if not o:
            return False
        if self.cfg.public_origin:
            return o == self.cfg.public_origin.rstrip("/")
        return o.split("://", 1)[-1] == request.host

    def login_allowed(self, ip):
        return (self.lim.allow("fail:" + ip, 5, 900, record=False)
                and self.lim.allow("fail:*", 30, 3600, record=False))

    def login_failed(self, ip):
        self.lim.hit("fail:" + ip)
        self.lim.hit("fail:*")

    def new_session(self):
        sid = secrets.token_urlsafe(32)
        now = time.time()
        self.st.sessions[sha(sid)] = {"exp": now + self.cfg.session_days * 86400, "created": now}
        for k in sorted(self.st.sessions, key=lambda k: self.st.sessions[k]["created"])[:-20]:
            self.st.sessions.pop(k, None)
        self.st.save("sessions.json", self.st.sessions)
        resp = redirect("/")
        resp.set_cookie(COOKIE, sid, max_age=self.cfg.session_days * 86400, httponly=True,
                        secure=self.cfg.secure_cookie, samesite="Strict", path="/")
        return resp

    # --- public app ------------------------------------------------------------------------------
    def public_app(self):
        @web.middleware
        async def mw(request, handler):
            resp = await handler(request)
            if not resp.prepared:
                resp.headers.update(SEC_HEADERS)
            return resp

        app = web.Application(middlewares=[mw], client_max_size=16 * 1024)
        app.router.add_get("/login", self.login_get)
        app.router.add_post("/login", self.login_post)
        app.router.add_post("/logout", self.logout)
        app.router.add_get("/", self.index)
        app.router.add_get("/static/{name}", self.static)
        app.router.add_post("/api/ticket", self.ticket)
        app.router.add_get("/ws", self.ws)
        return app

    async def login_get(self, request):
        ip = self.client_ip(request)
        tok = request.query.get("t", "")
        if tok:
            if not self.login_allowed(ip):
                return web.Response(status=429, text="Too many attempts. Try later.")
            exp = self.links.pop(sha(tok), 0)
            if exp < time.time():
                self.login_failed(ip)
                return web.Response(status=403, text="This link is used or expired. Ask for a new one.")
            return self.new_session()
        form = ('<form method="post" action="/login"><input type="password" name="pw" autocomplete="current-password"'
                ' placeholder="Passphrase" required><button>Log in</button></form>') if self.st.passphrase else ""
        html = ('<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,'
                'initial-scale=1"><title>Voice login</title><link rel="stylesheet" href="/static/app.css"></head>'
                '<body class="login"><h1>Voice</h1><p>Ask your assistant on Telegram for a login link.</p>%s'
                '</body></html>') % form
        return web.Response(text=html, content_type="text/html")

    async def login_post(self, request):
        ip = self.client_ip(request)
        if not self.origin_ok(request) or not self.login_allowed(ip):
            return web.Response(status=429, text="Too many attempts. Try later.")
        pw = (await request.post()).get("pw", "")
        rec = self.st.passphrase
        ok = bool(rec) and isinstance(pw, str) and 0 < len(pw) <= 200 and hmac.compare_digest(
            hash_passphrase(pw, rec["salt"])["hash"], rec["hash"])
        if not ok:
            self.login_failed(ip)
            return web.Response(status=403, text="Wrong passphrase.")
        return self.new_session()

    async def logout(self, request):
        s = self.session_of(request)
        if s and self.origin_ok(request):
            self.st.sessions.pop(s, None)
            self.st.save("sessions.json", self.st.sessions)
        resp = redirect("/login")
        resp.del_cookie(COOKIE, path="/")
        return resp

    async def index(self, request):
        if not self.session_of(request):
            return redirect("/login")
        return web.FileResponse(os.path.join(STATIC, "index.html"))

    async def static(self, request):
        name = request.match_info["name"]
        if name not in PUBLIC_STATIC | PRIVATE_STATIC:
            return web.Response(status=404)
        if name in PRIVATE_STATIC and not self.session_of(request):
            return web.Response(status=401)
        return web.FileResponse(os.path.join(STATIC, name))

    async def ticket(self, request):
        s = self.session_of(request)
        if not s:
            return web.json_response({"error": "login"}, status=401)
        if request.headers.get("X-Voice") != "1" or not self.origin_ok(request):
            return web.json_response({"error": "forbidden"}, status=403)
        if not self.lim.allow("ticket:" + s, 10, 60):
            return web.json_response({"error": "slow down"}, status=429)
        t = secrets.token_urlsafe(24)
        now = time.time()
        self.tickets = {k: v for k, v in self.tickets.items() if v[0] > now}
        self.tickets[sha(t)] = (now + 60, s)
        return web.json_response({"ticket": t, "provider": self.cfg.provider})

    async def ws(self, request, link=False):
        t = request.query.get("ticket", "")
        app = self.app_tickets.pop(sha(t), None) if t else None
        if link and app is None:    # the link listener: app tickets only, never a page login
            return web.Response(status=403, text="forbidden")
        if app is not None:     # phone app (phonegw on loopback, or over the link): the ticket alone, no page login or Origin
            if app["exp"] < time.time() or (not link and (request.remote or "") not in ("127.0.0.1", "::1")):
                return web.Response(status=403, text="forbidden")
            remaining = self.cfg.max_session_s
        else:
            rec = self.tickets.pop(sha(t), None) if t else None
            if not rec or rec[0] < time.time() or not self.origin_ok(request) or rec[1] not in self.st.sessions:
                return web.Response(status=403, text="forbidden")
            remaining = self.cfg.max_minutes_per_day * 60 - self.st.seconds_today()
        if len(self.live) >= self.cfg.max_live_sessions:
            return web.Response(status=429, text="busy")
        if remaining < 30:
            return web.Response(status=429, text="daily limit reached")
        ws = web.WebSocketResponse(max_msg_size=256 * 1024, heartbeat=25)
        await ws.prepare(request)
        fe = WebFrontEnd(ws)
        session = None
        end_reason = None
        try:
            first = await ws.receive(timeout=10)
            start = json.loads(first.data) if first.type == WSMsgType.TEXT else {}
            mode = "open" if app is not None or start.get("mode") == "open" else "ptt"
            provider = self.provider_factory(self.cfg, instructions(self.cfg), mode)
            if app is not None:
                clog = CallLog(self.cfg, "app-out" if app["reason"] or app["say"] else "app",
                               "phone app (mTLS device via phonegw)", **{k: app[k] for k in ("reason", "say") if app[k]})
                session = VoiceSession(self.cfg, provider, fe, self.handoff, limit_s=remaining, log=clog,
                                       on_end=lambda s, _secs: self.app_live.pop(s, None))
                self.app_live[session] = clog.id
            else:
                session = VoiceSession(self.cfg, provider, fe, self.handoff, limit_s=min(self.cfg.max_session_s, remaining),
                                       on_end=lambda _s, secs: self.st.add_usage(secs),
                                       log=CallLog(self.cfg, "page", "page login (%s)" % mode))
            self.live.add(session)
            await session.start()
            if app is not None and (app["reason"] or app["say"]):
                from .tgcall import opening_text      # he answered an in-app ring: open like an outbound call
                session.open_after_pickup(lambda heard: opening_text(app["reason"], app["say"], heard))
            elif app is not None:                     # he started a call in the app: live now, the pick-up line
                session.line_up = True
                if self.cfg.greet_inbound:
                    pcm, via = self.greeter.audio(fe.out_rate)
                    session.greet_inbound(pcm, via)
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    if len(msg.data) <= 64000:
                        await session.audio_in(msg.data)
                elif msg.type == WSMsgType.TEXT:
                    try:
                        c = json.loads(msg.data)
                    except ValueError:
                        continue
                    kind = c.get("type")
                    if kind == "ptt":
                        await session.ptt(bool(c.get("down")))
                    elif kind == "text":
                        text = str(c.get("text", ""))[:1000]
                        if text.strip():
                            await session.text_in(text)
                    elif kind == "link" and app is not None:     # phonegw: the app's connection dropped / is back
                        if c.get("state") == "lost":
                            await session.link_lost()
                        elif c.get("state") == "back":
                            await session.link_back(c.get("gap_ms"))
                    elif kind == "hangup":
                        if app is not None and c.get("why") == "app_lost":
                            end_reason = "app lost"         # phonegw's grace ran out: not his hang-up
                        break
                if session.closed:
                    break
        except ProviderError as e:
            await fe.event({"type": "error", "message": str(e)})
        except (asyncio.TimeoutError, ValueError, TypeError):
            pass
        finally:
            if session is not None:
                # shielded: a dropped peer can cancel this handler; the close still finishes and the slot frees
                closing = asyncio.ensure_future(session.close(end_reason or ("app closed" if app is not None
                                                                             else "page closed")))
                try:
                    await asyncio.shield(closing)
                finally:
                    self.live.discard(session)
                    self.app_live.pop(session, None)
            if not ws.closed:
                await ws.close()
        return ws

    # --- link app (hub mode: the phone gateway in another container) ----------------------------------
    def link_app(self):
        """What phonegw's /v1/voice needs, for a phonegw outside this container (claude-hub-phone): POST
        /internal/ticket and GET /internal/status with the link token (VOICE_LINK_TOKEN_FILE; the local token is no
        good here, and no token file = everything 401), GET /ws for app tickets only. No loopback check on /ws: the
        one-use 30 s ticket, minted only with a token, is the check. Nothing else (no call, answer, link, health)."""
        @web.middleware
        async def auth(request, handler):
            if request.path != "/ws":
                want = read_secret(self.cfg.link_token_file) if self.cfg.link_token_file else ""
                got = request.headers.get("Authorization", "")
                got = got[7:] if got.startswith("Bearer ") else ""
                if not want or not hmac.compare_digest(got.encode(), want.encode()):
                    return web.json_response({"error": "unauthorized"}, status=401)
            return await handler(request)

        async def ws(request):
            return await self.ws(request, link=True)

        app = web.Application(middlewares=[auth], client_max_size=64 * 1024)
        app.router.add_post("/internal/ticket", self.i_ticket)
        app.router.add_get("/internal/status", self.i_status)
        app.router.add_get("/ws", ws)
        return app

    # --- internal app (127.0.0.1 only) -------------------------------------------------------------
    def internal_app(self):
        @web.middleware
        async def auth(request, handler):
            want = read_secret(self.cfg.local_token_file)
            got = request.headers.get("Authorization", "")
            got = got[7:] if got.startswith("Bearer ") else ""
            if not want or not hmac.compare_digest(got.encode(), want.encode()):
                return web.json_response({"error": "unauthorized"}, status=401)
            return await handler(request)

        app = web.Application(middlewares=[auth], client_max_size=64 * 1024)
        app.router.add_post("/internal/answer", self.i_answer)
        app.router.add_post("/internal/link", self.i_link)
        app.router.add_get("/internal/status", self.i_status)
        app.router.add_get("/internal/health", self.i_health)
        app.router.add_post("/internal/call", self.i_call)
        app.router.add_post("/internal/ticket", self.i_ticket)
        return app

    async def i_ticket(self, request):
        """One-use /ws ticket for the phone app (phonegw's /v1/voice proxy), valid 30 s, loopback /ws only.
        {via: "app", reason?, say?}: reason/say = he answered an in-app ring (Gemini opens like an outbound call)."""
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict) or body.get("via") != "app":
            return web.json_response({"error": "need via=app"}, status=400)
        if not self.lim.allow("app-ticket", 20, 60):
            return web.json_response({"error": "slow down"}, status=429)
        if len(self.live) >= self.cfg.max_live_sessions:
            return web.json_response({"error": "busy"}, status=429)
        clean = lambda v: filter_answer(" ".join(str(v or "").split())[:200], self.cfg.mask_amounts).text if v else ""
        t = secrets.token_urlsafe(24)
        now = time.time()
        self.app_tickets = {k: v for k, v in self.app_tickets.items() if v["exp"] > now}
        self.app_tickets[sha(t)] = {"exp": now + 30, "reason": clean(body.get("reason")), "say": clean(body.get("say"))}
        return web.json_response({"ticket": t, "ws": "/ws", "expires_s": 30, "provider": self.cfg.provider})

    async def i_answer(self, request):
        try:
            body = await request.json()
            vid, text, more = str(body["id"])[:32], str(body["text"])[:4000], body.get("more") is True
        except (ValueError, KeyError, TypeError, AttributeError):
            return web.json_response({"error": "need id and text"}, status=400)
        return web.json_response(await self.handoff.answer(vid, text, more=more))

    async def i_call(self, request):
        """Ring the owner (the bridge's owner id only); waits for the ring outcome (~45 s max)."""
        try:
            body = await request.json()
        except ValueError:
            body = {}
        bridge = getattr(self, "_bridge", None)
        if bridge is None:
            return web.json_response({"status": "unavailable",
                                      "detail": "call bridge %s" % (getattr(self, "_tg_state", None) or "off")})
        body = body if isinstance(body, dict) else {}
        reason, say = str(body.get("reason") or "")[:300], str(body.get("say") or "")[:300]
        return web.json_response(await bridge.call_owner(reason, say) if say else await bridge.call_owner(reason))

    async def i_link(self, request):
        tok = secrets.token_urlsafe(24)
        now = time.time()
        self.links = {k: v for k, v in self.links.items() if v > now}
        self.links[sha(tok)] = now + self.cfg.link_minutes * 60
        path = "/login?t=" + tok
        return web.json_response({"path": path, "url": (self.cfg.public_origin.rstrip("/") + path)
                                  if self.cfg.public_origin else path, "expires_min": self.cfg.link_minutes})

    async def i_health(self, request):
        """Call account session check for voicecheck: get_me on the bridge's own connection (no second client)."""
        if not self.lim.allow("health", 6, 60):
            return web.json_response({"error": "slow down"}, status=429)
        bridge = getattr(self, "_bridge", None)
        if bridge is None or not hasattr(bridge, "ping"):
            tg = {"ok": False, "error": "bridge %s" % (getattr(self, "_tg_state", None) or "off")}
        else:
            tg = await bridge.ping()
        return web.json_response({"tg": tg, "tg_calls": getattr(self, "_tg_state", None)})

    async def i_status(self, request):
        # live = web sessions + Telegram calls on or ringing (restart only at 0); call_ids = their call-log ids
        bridge = getattr(self, "_bridge", None)
        call_ids = bridge.live_ids() if bridge is not None and hasattr(bridge, "live_ids") else []
        app_ids = list(self.app_live.values())    # phone-app sessions (also in self.live)
        return web.json_response({"provider": self.cfg.provider, "live": len(self.live) + len(call_ids),
                                  "call_ids": call_ids + app_ids, "app_calls": len(app_ids),
                                  "pending": self.handoff.summary(),
                                  "minutes_today": round(self.st.seconds_today() / 60, 1),
                                  "minutes_cap": self.cfg.max_minutes_per_day,
                                  "tg_calls": getattr(self, "_tg_state", None),
                                  "files_inbox": getattr(self, "_files_state", None),
                                  "files": self._files.status() if getattr(self, "_files", None) else None,
                                  "call_on": bool(getattr(getattr(self, "_bridge", None), "calls", None)),
                                  "greet": self.greeter.status() if self.cfg.greet_inbound else None,
                                  "call_failures": getattr(getattr(self, "_bridge", None), "provider_failures", 0),
                                  "last_call_failure": getattr(getattr(self, "_bridge", None), "last_failure", "")})

    async def start_call_bridge(self, client=None):
        """Telegram call bridge on the second user account (opt-in: serve --tg-calls). Returns (bridge, state).
        Refusals (no/unauthorized session, bot or owner account, no owner id) leave the web relay running."""
        from .tgcall import BridgeRefused, CallBridge, PyTgCallsClient, load_userbot_config
        try:
            client = client or PyTgCallsClient(load_userbot_config(self.cfg.tg_config_file))
        except BridgeRefused as e:
            return None, "refused: %s" % e
        except Exception as e:
            return None, "failed: %s" % type(e).__name__
        bridge = CallBridge(self.cfg, client, owner_telegram_id(self.cfg), self.handoff,
                            lambda: instructions(self.cfg), self.provider_factory, greeter=self.greeter)
        try:
            await bridge.start()
            self._files_state = self._start_file_inbox(client, bridge.owner_id)
            return bridge, "on"
        except Exception as e:
            stop = getattr(client, "stop", None)
            if stop is not None:
                try:
                    await stop()
                except Exception:
                    pass
            return None, ("refused: %s" % e) if isinstance(e, BridgeRefused) else "failed: %s" % type(e).__name__

    def _start_file_inbox(self, client, owner_id):
        """Big-file inbox (bigfiles.py) on the call bridge's own live client: no second session, no login."""
        self._files = None
        if not self.cfg.files_inbox or not hasattr(client, "on_media"):
            return "off"
        from .bigfiles import FileInbox
        try:
            files = FileInbox(self.cfg, owner_id)
            client.on_media(files.handle, owner_id)
        except Exception as e:
            return "failed: %s" % type(e).__name__
        self._files = files
        return "on"

    async def run(self, announce=None, tg_calls=False, call_client=None):
        runners = []
        bridge = None
        sites = [(self.public_app(), self.cfg.public_host, self.cfg.public_port),
                 (self.internal_app(), "127.0.0.1", self.cfg.internal_port)]
        if self.cfg.hub_mode and self.cfg.link_port:    # the phone gateway in another container
            sites.append((self.link_app(), self.cfg.link_host, self.cfg.link_port))
        for app, host, port in sites:
            r = web.AppRunner(app, access_log=None, shutdown_timeout=2)
            await r.setup()
            await web.TCPSite(r, host, port).start()
            runners.append(r)
        hub_tasks = []
        if self.cfg.hub_mode:                   # claude-hub-voice (hubmode.py)
            from . import hubmode
            hub_tasks = hubmode.install(self)
            self.handoff.hub = self.hub
        if self.cfg.greet_inbound:              # the pick-up line: from disk, else generated now in the background
            self.greeter.autorefresh = True
            self.greeter.refresh_soon()
        if tg_calls:
            bridge, state = await self.start_call_bridge(call_client)
            self._tg_state, self._bridge = state, bridge
            announce = dict(announce or {}, tg_calls=state, files_inbox=getattr(self, "_files_state", None))
        if len(sites) > 2:
            announce = dict(announce or {}, link=self.cfg.link_port)
        print(json.dumps(dict(event="listening", ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), public=self.cfg.public_port, internal=self.cfg.internal_port,
                              provider=self.cfg.provider, **(announce or {}))), flush=True)
        try:
            await asyncio.Event().wait()
        finally:
            for r in runners:
                await r.cleanup()
            if bridge is not None and hasattr(bridge.client, "stop"):
                await bridge.client.stop()
            await self.handoff.close()
            for t in hub_tasks:
                t.cancel()
            if getattr(self, "hub", None) is not None:
                await self.hub.close()


async def serve_fake(cfg, delay, slow_delay):
    """Local demo/E2E: a fake Gemini Live server + a fake session hand-off that answers by itself. Outside hub mode the
    local token, state and inbox move to a temp dir as well (never the live relay's files). In hub mode (the loader's
    fixed container paths) they stay where the loader put them, so the loader's token opens the
    internal API and the hub outbox is the one the relay ships; only Gemini and the key are fake."""
    from .fakes import FakeGemini, FakePluginInbox
    hub = bool(cfg.hub_mode)
    if hub:
        os.makedirs(cfg.state_dir, mode=0o700, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="voice-relay-fake-", dir=cfg.state_dir if hub else None)
    key_file = os.path.join(tmp, "gemini-key")
    tok_file = cfg.local_token_file if hub and read_secret(cfg.local_token_file) else os.path.join(tmp, "local-token")
    for path, val in ((key_file, "fake-key"), (tok_file, secrets.token_urlsafe(24))):
        if path == cfg.local_token_file:
            continue  # the loader's token (hub mode): kept as it is
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(val + "\n")
    gem = FakeGemini(key="fake-key")
    gport = await gem.start()
    inbox = FakePluginInbox(read_secret(tok_file), "http://127.0.0.1:%d" % cfg.internal_port, delay=delay,
                            slow_delay=slow_delay)
    pport = await inbox.start()
    paths = {} if hub else {"state_dir": os.path.join(tmp, "state"), "inbox_dir": os.path.join(tmp, "inbox")}
    cfg = dataclasses.replace(cfg, provider="gemini", gemini_ws_url="ws://127.0.0.1:%d/ws" % gport,
                              gemini_key_file=key_file, local_token_file=tok_file, secure_cookie=False,
                              plugin_url="http://127.0.0.1:%d/voice" % pport, greet_tts_models=(), **paths)
    relay = Relay(cfg)
    tok = secrets.token_urlsafe(24)
    relay.links[sha(tok)] = time.time() + 600
    await relay.run(announce={"fake": True, "login_path": "/login?t=" + tok, "tmp": tmp})


CALL_EXIT = {"answered": 0, "busy": 3, "declined": 3, "no_answer": 3, "in_call": 4, "rate_limited": 4,
             "nothing": 6}


def _internal(cfg, method, path, body=None, timeout=10):
    import urllib.request
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (cfg.internal_port, path), method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + read_secret(cfg.local_token_file),
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _relay_env(path=None):
    """VOICE_* lines of the relay's env file (a shell running the CLI doesn't have them in its environment): the CLI
    builds the same config as the running relay (e.g. the voice of the pick-up line). Values are never printed."""
    path = path or os.environ.get("VOICE_RELAY_ENV") or os.path.expanduser("~/.config/voice-relay.env")
    out = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                k, sep, v = line.strip().partition("=")
                if sep and k.startswith("VOICE_") and k.replace("_", "").isalnum():
                    out[k] = v.strip().strip("'\"")
    except OSError:
        pass
    return out


def _usage_cli(feature, n):
    try:
        from .closing import usage_log
        usage_log()(feature, "invoke", channel="shell", outcome="ok", attrs={"calls": n})
    except Exception:
        pass


def from_hub(args, timeout=60):
    """The call log lives in claude-hub-voice: outside it (cfg.hub_mode off) `calls` and `stats` read it through the
    hub gateway's voice.calls (`voice-hub calls ARGS`: the call digests and quality numbers, metadata only).
    Returns (exit code, data or None); 2 = not reachable (said on stderr)."""
    import subprocess
    vh = os.environ.get("VOICE_HUB_BIN") or os.path.expanduser("~/bin/voice-hub")
    if not os.access(vh, os.X_OK):
        print("voice-relay: the call log is in claude-hub-voice, but %s is missing" % vh, file=sys.stderr)
        return 2, None
    try:
        p = subprocess.run([vh, "calls", *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        print("voice-relay: voice-hub calls: %s" % type(e).__name__, file=sys.stderr)
        return 2, None
    if p.returncode != 0:
        sys.stderr.write(p.stderr)
        return p.returncode or 2, None
    try:
        d = json.loads(p.stdout)
    except ValueError:
        d = None
    if not isinstance(d, dict):
        print("voice-relay: voice-hub calls: no JSON", file=sys.stderr)
        return 2, None
    return 0, d


def hub_calls(a):
    """`calls` outside claude-hub-voice: the newest calls from the hub relay's log, one block each (metadata only),
    and for outgoing calls whether he answered; --json = voice.calls' data as is."""
    rc, d = from_hub(["--last", str(max(1, a.last))] + (["--out"] if a.out else []))
    if rc:
        return rc
    if a.json:
        print(json.dumps(d))
        return 0
    from .calllog import format_digest, format_outgoing
    calls = [c for c in d.get("calls") or [] if isinstance(c, dict)]
    if not calls:
        print("no %scalls in the hub relay's log (14 days)" % ("outgoing " if a.out else ""))
        return 1
    live = d.get("live") if isinstance(d.get("live"), list) else None
    print("\n\n".join(format_digest(c, live) for c in calls))
    print("\n" + format_outgoing(d.get("outgoing")))
    print("(the hub relay's call log, metadata only: no words, reasons or tool arguments)")
    return 0


def hub_stats(a):
    """`stats` outside claude-hub-voice: the hub relay's quality numbers (voice.calls' stats)."""
    from .calllog import KEEP_DAYS, format_quality
    days = a.days if 0 < a.days <= KEEP_DAYS else KEEP_DAYS
    rc, d = from_hub(["--last", "0", "--days", "%g" % days])
    if rc:
        return rc
    q = d.get("stats")
    if not isinstance(q, dict) or not isinstance(q.get("calls"), int):
        print("voice-relay: the hub sent no call numbers", file=sys.stderr)
        return 2
    _usage_cli("voice.stats", q["calls"])
    print(json.dumps(q, indent=1) if a.json else format_quality(q, days))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="voice-relay")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--fake", action="store_true", help="fake Gemini + fake session hand-off (local test)")
    s.add_argument("--tg-calls", action="store_true", help="also answer the owner's Telegram calls (second account)")
    s.add_argument("--fake-delay", type=float, default=0.05)
    s.add_argument("--fake-slow-delay", type=float, default=3.0)
    s.add_argument("--port", type=int)
    s.add_argument("--internal-port", type=int)
    s.add_argument("--sync-wait", type=float)
    sub.add_parser("init")
    sub.add_parser("link")
    sub.add_parser("status")
    h = sub.add_parser("health", help="ask the running relay whether the call account's session works (get_me on "
                                      "its live connection); exit 0 ok, 1 not ok, 2 relay down")
    h.add_argument("--json", action="store_true")
    mo = sub.add_parser("models", help="Live-capable Gemini models for this key (models.list, free); "
                                       "marks the configured model and its fallbacks")
    mo.add_argument("--json", action="store_true")
    ca = sub.add_parser("canary", help="Gemini key/model (models.get, free) + billing (one minimal Live setup, "
                                       "closed at once, no turn); exit 0 ok, 1 problem")
    ca.add_argument("--no-live", action="store_true", help="models.get only (no Live setup)")
    ca.add_argument("--json", action="store_true")
    sub.add_parser("passwd")
    g = sub.add_parser("greet", help="the cached pick-up line for his calls (text, voice, TTS model, length); "
                                     "--refresh = generate it now (Gemini TTS); exit 0 clip ready, 1 none (earcon)")
    g.add_argument("--refresh", action="store_true")
    g.add_argument("--json", action="store_true")
    c = sub.add_parser("calls", help="the last call(s): outside claude-hub-voice from the hub relay's log through "
                                     "voice.calls (metadata only: kind, time, answered or not, talk turns, tools, "
                                     "end; no words), inside it the full local log")
    c.add_argument("--last", type=int, default=1)
    c.add_argument("--out", action="store_true", help="outgoing calls only (the relay rang him: answered or not)")
    c.add_argument("--json", action="store_true", help="the hub's voice.calls data as is (outside the container)")
    qs = sub.add_parser("stats", help="call quality numbers (counts and timings, no text): the hub relay's log "
                                      "outside claude-hub-voice, the local one inside it")
    qs.add_argument("--days", type=float, default=14)
    qs.add_argument("--json", action="store_true")
    fi = sub.add_parser("files", help="list recent big files the owner sent to the call account (local log)")
    fi.add_argument("--last", type=int, default=10)
    fi.add_argument("--json", action="store_true")
    f = sub.add_parser("fastlane", help="run the read-only quick tools once (live) and print their latency")
    f.add_argument("tools", nargs="*")
    f.add_argument("--json", action="store_true", help="tool, ms, ok and the error only (no answer text)")
    o = sub.add_parser("call", help="ring the owner (only him); exit 0 answered, 3 busy/declined/no answer, "
                                    "4 in a call or rate-limited, 5 unavailable/failed, 2 relay down; --reason decisions = decision "
                                    "round from decision-deck, exit 6 when there is nothing to decide (no ring)")
    o.add_argument("--reason", default="")
    o.add_argument("--say", default="", help="spoken-friendly words for why you call (else the reason is paraphrased)")
    r = sub.add_parser("reply", help="answer a hand-off; --more = a first part now, the rest follows with another reply")
    r.add_argument("--more", action="store_true", help="speak this part now and keep the hand-off open")
    r.add_argument("id")
    r.add_argument("text", nargs="+")
    a = ap.parse_args(argv)
    over = {k: v for k, v in (("public_port", getattr(a, "port", None)),
                              ("internal_port", getattr(a, "internal_port", None)),
                              ("sync_wait_s", getattr(a, "sync_wait", None))) if v is not None}
    cfg = Config.from_env(**over)
    if a.cmd == "serve":
        from .logmask import setup_serve_logging
        setup_serve_logging(secrets=(read_secret(cfg.local_token_file),))
        try:
            asyncio.run(serve_fake(cfg, a.fake_delay, a.fake_slow_delay) if a.fake else Relay(cfg).run(tg_calls=a.tg_calls))
        except KeyboardInterrupt:
            pass
        return 0
    if a.cmd == "init":
        os.makedirs(cfg.state_dir, mode=0o700, exist_ok=True)
        if not read_secret(cfg.local_token_file):
            fd = os.open(cfg.local_token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(secrets.token_urlsafe(32) + "\n")
        print(json.dumps({"token_file": cfg.local_token_file, "state_dir": cfg.state_dir}))
        return 0
    if a.cmd in ("calls", "stats") and not cfg.hub_mode:    # the log is in claude-hub-voice (its relay: hub mode)
        return hub_calls(a) if a.cmd == "calls" else hub_stats(a)
    if a.cmd == "calls":
        from .calllog import print_calls
        try:
            live = _internal(cfg, "GET", "/internal/status", timeout=3).get("call_ids") or []
        except (OSError, ValueError):
            live = None                   # relay down: an open call's end can't come any more
        return print_calls(cfg, a.last, live=live, outgoing=a.out, as_json=a.json)
    if a.cmd == "stats":
        from .calllog import calls_dir, format_quality, load_calls, quality
        q = quality(load_calls(calls_dir(cfg), 100000), since=time.time() - a.days * 86400 if a.days > 0 else None)
        _usage_cli("voice.stats", q["calls"])
        print(json.dumps(q, indent=1) if a.json else format_quality(q, a.days if a.days > 0 else None))
        return 0
    if a.cmd == "greet":
        gr = Greeter(Config.from_env(dict(_relay_env(), **os.environ), **over))   # the relay's voice + line
        if a.refresh or not gr.load():
            asyncio.run(gr.ensure(force=a.refresh))
        st = gr.status()
        print(json.dumps(st) if a.json else (
            "pick-up line %r, voice %s: %s" % (st["text"], st["voice"], "%d ms clip (%s, %s)" % (
                st["ms"], st["model"], st["created"]) if st["clip"] else "no clip, the earcon plays (%s)" % (
                st["error"] or "not generated"))))
        return 0 if st["clip"] else 1
    if a.cmd == "files":
        from .bigfiles import print_files
        return print_files(cfg, a.last, a.json)
    if a.cmd == "fastlane":
        from .fastlane import measure
        res = measure(cfg, a.tools or None)
        if a.json:
            print(json.dumps([{"tool": r["tool"], "ms": r["ms"], "ok": r["ok"],
                               "error": "" if r["ok"] else str(r["text"])[:120]} for r in res]))
            return 0
        for r in res:
            print("%-13s %5d ms  %s  %s" % (r["tool"], r["ms"], "ok " if r["ok"] else "ERR", r["text"]))
        return 0
    if a.cmd == "health":
        try:
            res = _internal(cfg, "GET", "/internal/health", timeout=20)
        except (OSError, ValueError) as e:
            code = getattr(e, "code", None)
            res = {"error": "relay down" if code is None else "HTTP %s" % code}
            print(json.dumps(res) if a.json else res["error"])
            return 1 if code == 429 else 2
        tg = res.get("tg") or {}
        print(json.dumps(res) if a.json else ("call account ok" if tg.get("ok") else "call account: %s" % tg.get("error")))
        return 0 if tg.get("ok") else 1
    if a.cmd == "models":
        from . import canary
        try:
            ms = asyncio.run(canary.live_models(cfg))
        except Exception as e:
            print("models: %s" % (str(e) or type(e).__name__)[:160], file=sys.stderr)
            return 1
        chain = [cfg.gemini_model] + list(cfg.gemini_fallbacks or ())
        for m in ms:
            m["role"] = ("configured" if m["model"] == chain[0] else "fallback %d" % chain.index(m["model"])
                         if m["model"] in chain else "")
        missing = [c for c in chain if c not in {m["model"] for m in ms}]
        if a.json:
            print(json.dumps({"models": ms, "missing": missing}))
        else:
            for m in ms:
                print("%-44s %-30s %s" % (m["model"], m["version"], m["role"]))
            for c in missing:
                print("%-44s %-30s %s" % (c, "NOT LISTED", "configured" if c == chain[0] else "fallback"))
        return 0 if chain[0] not in missing else 1
    if a.cmd == "canary":
        from . import canary
        res = asyncio.run(canary.run(cfg, live=not a.no_live))
        print(json.dumps(res) if a.json else "%s %s: key %s, listed %s, version %s, live %s%s" % (
            res["provider"], res["model"], res["key"], res["listed"], res["version"] or "-", res["live"],
            (" (%s)" % res["detail"]) if res["detail"] else ""))
        return 0 if canary.ok(res) else 1
    if a.cmd == "passwd":
        pw = sys.stdin.readline().rstrip("\n")
        if len(pw) < 12:
            print("passphrase: 12+ characters", file=sys.stderr)
            return 2
        st = State(cfg)
        st.save("passphrase.json", hash_passphrase(pw))
        print("passphrase set (restart the relay)")
        return 0
    try:
        if a.cmd == "link":
            print(_internal(cfg, "POST", "/internal/link", {})["url"])
            return 0
        if a.cmd == "call":
            res = _internal(cfg, "POST", "/internal/call", dict({"reason": a.reason}, **({"say": a.say} if a.say else {})),
                            timeout=120)
            print(json.dumps(res))
            return CALL_EXIT.get(res.get("status"), 5)
        if a.cmd == "status":
            print(json.dumps(_internal(cfg, "GET", "/internal/status")))
            return 0
        res = _internal(cfg, "POST", "/internal/answer", {"id": a.id, "text": " ".join(a.text), "more": a.more})
        print(json.dumps(res))
        if res.get("delivered") in ("sync", "live"):
            return 0
        print("not delivered: send the answer on Telegram", file=sys.stderr)
        return 3
    except OSError as e:
        print("voice-relay not reachable: %s" % type(e).__name__, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
