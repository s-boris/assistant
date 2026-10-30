"""Telegram call bridge: a second audio front end next to the web page. Private 1:1 calls from the owner to a
second Telegram *user* account (py-tgcalls/NTgCalls), wired to the same VoiceSession core.

Safety: only the owner's user id may call (everyone else is declined before any audio is touched); the account
must be a user account (bot sessions and bot tokens are refused: the bot belongs to the telegram-plus plugin and
is never polled from here); its credentials live in ~/.secrets (api_id/api_hash + session file).
The real client (PyTgCallsClient) needs a session made by `voice-tg login`; it never logs in by itself.
"""
import asyncio
import json
import logging
import os
import time
from collections import deque

from .answer_filter import filter_answer
from .bigfiles import media_kind
from .audio import frames
from . import decisions
from .calllog import CallLog
from .greet import Greeter
from .session import FrontEnd, VoiceSession

log = logging.getLogger("voice_relay.tgcall")
FRAME_BYTES = 960  # 10 ms of 48 kHz PCM16 mono
RING_STATUS = {"TimedOutAnswer": "no_answer", "TimeoutError": "no_answer", "CallDeclined": "declined",
               "CallBusy": "busy"}   # py-tgcalls 3.0.0 exceptions from an outgoing play(); else "failed"
HEARD = {True: "He answered with a short hello (you did not catch it).", False: "He has not said anything yet.",
         None: "He may say hello first; you won't hear it."}      # None: greeting prefetched during the pick-up hold


def heard_note(heard):
    return HEARD[None if heard is None else bool(heard)]
GREET_ONCE = ("Greet him only once. If he talks over the greeting, don't start it again: answer what he said, "
              "and add why you called only if you haven't said it yet. Then talk with him as usual.")
OPENING = ("[outbound call] Your assistant rang him and he just picked up. {heard} Why it called (a note for you, "
           "not a script: never read it out word for word): {reason}. Say \"Hi, it's Claude\" and why you're "
           "calling in one short natural sentence of your own. " + GREET_ONCE)
OPENING_SAY = ("[outbound call] Your assistant rang him and he just picked up. {heard} Say \"Hi, it's Claude\" and "
               "why you're calling, in about these words: \"{say}\". " + GREET_ONCE)
OPENING_NO_REASON = ("[outbound call] Your assistant rang him and he just picked up. {heard} Say \"Hi, it's "
                     "Claude\" and ask briefly what he needs. " + GREET_ONCE)


def _clean(text):
    """A caller's machine-style reason ("test call: you asked …", "reminder - X") reads as one clause."""
    t = " ".join(str(text or "").split()).strip(" .")
    return t.replace(": ", ", ").replace(" - ", ", ")


def opening_text(reason="", say="", heard=False):
    h = heard_note(heard)
    if say:
        return OPENING_SAY.format(say=say.strip(" ."), heard=h)
    if reason:
        return OPENING.format(reason=_clean(reason), heard=h)
    return OPENING_NO_REASON.format(heard=h)


class BridgeRefused(Exception):
    pass


class CallFrontEnd(FrontEnd):
    in_rate = 48000
    out_rate = 48000

    def __init__(self, client, call_id):
        self.client, self.call_id = client, call_id
        self.q = deque()
        self.wake = asyncio.Event()
        self.closed = False
        self.pacer = asyncio.ensure_future(self._pace())

    async def play(self, pcm):
        if len(pcm) % FRAME_BYTES:
            pcm += b"\0" * (FRAME_BYTES - len(pcm) % FRAME_BYTES)
        self.q.extend(frames(pcm, 48000, 10))
        self.wake.set()

    async def flush(self):
        self.q.clear()

    async def event(self, ev):
        pass  # transcripts are not kept or logged on the call path

    async def hangup(self, reason):
        if self.closed:
            return
        self.closed = True
        self.pacer.cancel()
        await self.client.hangup(self.call_id)

    async def drained(self):
        """Returns once the queued audio has been sent (the pacer plays it in real time)."""
        while self.q and not self.closed:
            await asyncio.sleep(0.02)

    async def _pace(self):
        """Send queued audio in real time (10 ms frames), like a microphone would."""
        due = time.monotonic()
        while not self.closed:
            if not self.q:
                self.wake.clear()
                await self.wake.wait()
                due = time.monotonic()
                continue
            await self.client.send_frame(self.call_id, self.q.popleft())
            due += 0.01
            delay = due - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)


class CallBridge:
    def __init__(self, cfg, client, owner_id, handoff, instructions_fn, provider_factory, max_calls_per_hour=6,
                 max_out_per_hour=3, ring_timeout_s=45, greeter=None):
        self.cfg, self.client, self.owner_id, self.handoff = cfg, client, int(owner_id or 0), handoff
        # his calls open with the cached pick-up line (greet.py); the relay passes its own (autorefresh on)
        self.greeter = greeter if greeter is not None else (
            Greeter(cfg) if getattr(cfg, "greet_inbound", False) else None)
        self.provider_failures, self.last_failure, self._noted = 0, "", 0.0
        self.instructions_fn, self.provider_factory = instructions_fn, provider_factory
        self.max_calls_per_hour = max_calls_per_hour
        self.calls = {}
        self.history = deque()
        self.rejected = 0
        self.max_out_per_hour, self.ring_timeout_s = max_out_per_hour, ring_timeout_s
        self.out_history = deque()
        self.ringing = False
        self.ringing_id = None   # call-log id of the outbound call while it rings

    async def start(self):
        if not self.owner_id:
            raise BridgeRefused("no owner id configured")
        me = await self.client.start()
        if me.get("is_bot"):
            raise BridgeRefused("this is a bot session; the bridge runs only on a Telegram user account")
        if int(me.get("id", 0)) == self.owner_id:
            raise BridgeRefused("this is the owner's own account; use the second account")
        self.client.callee = self.owner_id   # the client dials this id and no other (checked again there)
        self.client.on_incoming(self._incoming)
        self.client.on_frames(self._frames)
        self.client.on_ended(self._ended)
        return me

    async def _incoming(self, call_id, caller_id):
        if int(caller_id) != self.owner_id:
            self.rejected += 1
            CallLog(self.cfg, "tg-in", "declined: not the owner").end("declined")
            await self.client.decline(call_id)
            return
        now = time.monotonic()
        while self.history and self.history[0] < now - 3600:
            self.history.popleft()
        if self.calls or self.ringing or len(self.history) >= self.max_calls_per_hour:
            CallLog(self.cfg, "tg-in", "owner; declined: %s" % ("busy" if self.calls or self.ringing
                                                                else "hourly cap")).end("declined")
            await self.client.decline(call_id)
            return
        self.history.append(now)
        t_in = time.monotonic()
        clog = CallLog(self.cfg, "tg-in", "owner")
        fe = CallFrontEnd(self.client, call_id)
        provider = self.provider_factory(self.cfg, self.instructions_fn(), "open")
        session = VoiceSession(self.cfg, provider, fe, self.handoff, limit_s=self.cfg.max_session_s, log=clog,
                               on_end=lambda s, _secs: self._forget(call_id, s))
        self.calls[call_id] = session
        try:
            await session.start()        # the provider first: a dead provider (billing, outage) never picks up
        except Exception as e:
            from .providers import ProviderError
            self.calls.pop(call_id, None)
            self.provider_failures += 1
            self.last_failure = str(e)[:200] if isinstance(e, ProviderError) else type(e).__name__
            fe.closed = True             # nothing to hang up: the call was never accepted
            await self.client.decline(call_id)
            await session.close("provider failed: %s" % self.last_failure)
            self._failure_note()
            return
        t_acc = time.monotonic()
        try:
            await self.client.accept(call_id)    # returns once the media is connected (py-tgcalls waits for it)
        except Exception:
            self.calls.pop(call_id, None)
            await session.close("accept failed")
            return
        up = time.monotonic()
        clog.event("answer", provider_ms=round((t_acc - t_in) * 1000), connect_ms=round((up - t_acc) * 1000))
        session.line_up = True
        if self.greeter is not None and getattr(self.cfg, "greet_inbound", False):
            pcm, via = self.greeter.audio(CallFrontEnd.out_rate)
            session.greet_inbound(pcm, via, t0=up)

    def _forget(self, call_id, session):
        if self.calls.get(call_id) is session:   # the session ended on its own (time limit, provider)
            self.calls.pop(call_id, None)

    def live_ids(self):
        """Call-log ids of the calls on now (and one ringing): `voice-relay calls` tells "still on" from "lost"."""
        ids = [getattr(s.log, "id", None) for s in self.calls.values()]
        return [i for i in ids + ([self.ringing_id] if self.ringing else []) if i]

    async def ping(self, timeout=10):
        """Read-only check of the call account's session on the bridge's own live connection (get_me), for
        voicecheck: never a second client on the session file. {"ok": bool, "error": short type name}."""
        try:
            me = await asyncio.wait_for(self.client.ping(), timeout)
        except asyncio.TimeoutError:
            return {"ok": False, "error": "timeout"}
        except Exception as e:  # AuthKeyUnregistered, SessionRevoked, UserDeactivated, ConnectionError ...
            return {"ok": False, "error": type(e).__name__}
        if (me or {}).get("is_bot") or int((me or {}).get("id", 0)) == self.owner_id:
            return {"ok": False, "error": "wrong account"}
        return {"ok": True, "error": ""}

    async def call_owner(self, reason="", say=""):
        """Ring the owner. The target is always self.owner_id (access.json allowFrom[0]), never a parameter.
        Provider first (a dead provider never rings), then ring up to ring_timeout_s; on pick-up the usual
        session runs and opens with the reason. Returns {"status": answered | busy | declined | no_answer |
        in_call | rate_limited | failed, ...}. say: optional spoken-friendly words for why (else the model
        paraphrases reason)."""
        uid = self.owner_id
        if uid <= 0:
            return {"status": "failed", "detail": "no owner id"}
        reason = filter_answer(" ".join(str(reason or "").split())[:200], self.cfg.mask_amounts).text if reason else ""
        say = filter_answer(" ".join(str(say or "").split())[:200], self.cfg.mask_amounts).text if say else ""
        now = time.monotonic()
        while self.out_history and self.out_history[0] < now - 3600:
            self.out_history.popleft()
        if self.calls or self.ringing:
            return {"status": "in_call", "detail": "a call is already on"}
        deck = None
        if decisions.is_trigger(reason):
            try:
                deck = await decisions.load_deck(self.cfg)
            except Exception as e:
                return {"status": "failed", "detail": "decision deck: %s" % (str(e) or type(e).__name__)[:160]}
            if not deck["items"]:
                return {"status": "nothing", "detail": "no open either/or decisions; nobody was called"}
        if len(self.out_history) >= self.max_out_per_hour:
            CallLog(self.cfg, "tg-out", "outbound to the owner", reason=reason or None).end("rate limited")
            return {"status": "rate_limited", "detail": "max %d outbound calls per hour" % self.max_out_per_hour}
        self.out_history.append(now)
        self.ringing = True
        clog = CallLog(self.cfg, "tg-out", "outbound to the owner", reason=reason or None, say=say or None)
        self.ringing_id = clog.id
        try:
            fe = CallFrontEnd(self.client, uid)
            provider = self.provider_factory(self.cfg, self.instructions_fn(), "open")
            session = VoiceSession(self.cfg, provider, fe, self.handoff, limit_s=self.cfg.max_session_s, log=clog,
                                   on_end=lambda s, _secs: self._forget(uid, s))
            try:
                await session.start()
            except Exception as e:
                from .providers import ProviderError
                self.provider_failures += 1
                self.last_failure = str(e)[:200] if isinstance(e, ProviderError) else type(e).__name__
                fe.closed = True
                await session.close("provider failed: %s" % self.last_failure)
                return {"status": "failed", "detail": "voice provider did not start (%s)" % self.last_failure,
                        "call": clog.id}
            t0 = time.monotonic()
            try:
                await asyncio.wait_for(self.client.dial(uid, self.ring_timeout_s), self.ring_timeout_s + 15)
            except Exception as e:
                status = RING_STATUS.get(type(e).__name__, "failed")
                ring_s = round(time.monotonic() - t0, 1)
                clog.event("ring", outcome=status, ring_s=ring_s, **self._connect_fields(),
                           error=None if status != "failed" else "%s: %s" % (type(e).__name__, str(e)[:160]))
                fe.closed = status != "failed"   # declined/busy/timeout: py-tgcalls already discarded the call
                await session.close(status.replace("_", " "))
                res = {"status": status, "ring_s": ring_s, "call": clog.id}
                if status == "failed":
                    res["detail"] = type(e).__name__
                return res
        finally:
            self.ringing, self.ringing_id = False, None
        ring_s = round(time.monotonic() - t0, 1)
        clog.event("ring", outcome="answered", ring_s=ring_s, **self._connect_fields())
        self.calls[uid] = session
        if deck is not None:
            session.open_after_pickup(lambda heard: decisions.opening(deck, heard_note(heard)), deck)
        else:
            session.open_after_pickup(lambda heard: opening_text(reason, say, heard))
        res = {"status": "answered", "ring_s": ring_s, "call": clog.id}
        if deck is not None:
            res["decisions"] = len(deck["items"])
        return res

    def _connect_fields(self):
        """accept_to_connect_s: his accept -> media connected (or -> given up) when the client measured it."""
        lc = getattr(self.client, "last_connect", None)
        if isinstance(lc, dict) and lc.get("accept_to_connect_s") is not None:
            return {"accept_to_connect_s": lc["accept_to_connect_s"]}
        return {}

    def _failure_note(self):
        """At most one ~/inbox note per 30 min while calls can't start, so the main session can tell the owner."""
        now = time.time()
        if now - self._noted < 1800:
            return
        self._noted = now
        try:
            os.makedirs(self.cfg.inbox_dir, exist_ok=True)
            path = os.path.join(self.cfg.inbox_dir, "voicerelay-call-failed-%s.md" % time.strftime("%Y%m%d-%H%M%S"))
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("voice-relay: the owner's Telegram call was declined because the voice provider did not "
                         "start (%s). Calls stay down until that is fixed; tell him on Telegram.\n" % self.last_failure)
        except OSError:
            pass

    async def _frames(self, call_id, pcm):
        s = self.calls.get(call_id)
        if s is not None and not s.closed:
            await s.audio_in(pcm)

    async def _ended(self, call_id):
        s = self.calls.pop(call_id, None)
        if s is not None:
            await s.close("hangup", by="him")   # DISCARDED/BUSY from Telegram: he hung up (or his line dropped)


def load_userbot_config(path):
    """~/.secrets/voice-tg.json: {"api_id", "api_hash"[, "session_file", "phone"]} (0600). Refuses bot tokens.
    session_file defaults to ~/.secrets/voice-tg.session (where `voice-tg login` puts it)."""
    st = os.stat(path)
    if st.st_mode & 0o077:
        raise BridgeRefused("%s must be 0600" % path)
    with open(path, encoding="utf-8") as fh:
        conf = json.load(fh)
    if any(k in conf for k in ("bot_token", "token", "bot")):
        raise BridgeRefused("bot tokens are not allowed here: user account only")
    for k in ("api_id", "api_hash"):
        if not conf.get(k):
            raise BridgeRefused("missing " + k)
    secrets_dir = os.path.realpath(os.path.expanduser(os.environ.get("VOICE_TG_SESSION_DIR") or "~/.secrets")) + os.sep
    sf = os.path.realpath(os.path.expanduser(conf.get("session_file") or os.path.join(secrets_dir, "voice-tg.session")))
    if os.environ.get("VOICE_TG_SESSION_DIR"):  # hub mode: the session lives in the container's session dir
        sf = os.path.join(secrets_dir, os.path.basename(sf))
    if not sf.startswith(secrets_dir):
        raise BridgeRefused("the session file must live in ~/.secrets")
    conf["session_file"] = sf
    return conf


class PyTgCallsClient:
    """Real CallClient: Kurigram (pyrogram) user session + py-tgcalls 3 / NTgCalls 3 (checked against
    py-tgcalls 3.0.0 + ntgcalls 3.0.5). Private calls only: an incoming call is a ChatUpdate INCOMING_CALL whose
    chat_id is the caller's user id; play() on it accepts, leave_call() on it declines (discards the request).
    Audio both ways is raw PCM16 48 kHz MONO (AudioParameters(48000, 1); AudioQuality.HIGH would be stereo):
    we send 10 ms frames with send_frame(MICROPHONE), and the caller's audio arrives as INCOMING StreamFrames.
    The session must already be authorized (voice-tg login): pyrogram's interactive login prompt never runs here.
    client_cls / calls_cls are for tests (fakes); the defaults are pyrogram.Client and pytgcalls.PyTgCalls."""
    rate = 48000
    channels = 1

    def __init__(self, conf, client_cls=None, calls_cls=None):
        self.conf = conf
        self.callee = None   # set by CallBridge.start: the owner id, the only number dial() rings
        self._client_cls, self._calls_cls = client_cls, calls_cls
        self._in = self._fr = self._end = None
        self.app = self.tg = None
        self.accepted_at = {}    # uid -> monotonic time he accepted an outgoing call (confirm_call hook)
        self.last_connect = None  # after dial(): {"connected": bool, "accept_to_connect_s": float | None}

    def _make(self):
        sf = self.conf["session_file"]
        if not os.path.isfile(sf):
            raise BridgeRefused("no session file: log in first (voice-tg login start / finish)")
        if os.stat(sf).st_mode & 0o077:
            raise BridgeRefused("the session file must be 0600")
        if self._client_cls is None:
            from pyrogram import Client
            self._client_cls = Client
        if self._calls_cls is None:
            from pytgcalls import PyTgCalls
            self._calls_cls = PyTgCalls
        # updates must stay on (py-tgcalls refuses no_updates). The only message handler is on_media (big-file
        # inbox): private media from the owner's id; every other message is filtered out unread
        self.app = self._client_cls(os.path.basename(sf).removesuffix(".session"), api_id=int(self.conf["api_id"]),
                                    api_hash=str(self.conf["api_hash"]), workdir=os.path.dirname(sf))
        self.tg = self._calls_cls(self.app)
        self._hook_accept()

    def _hook_accept(self):
        """Timestamp his accept of an outgoing call: py-tgcalls calls its mtproto bridge's confirm_call right after
        the callee accepted, then waits for the media connection (ntgcalls gives up after a fixed 10 s)."""
        app = getattr(self.tg, "_app", None)
        orig = getattr(app, "confirm_call", None)
        if orig is None or getattr(orig, "_vr_hooked", False):
            return

        async def confirm_call(chat_id, *a, **k):
            self.accepted_at[int(chat_id)] = time.monotonic()
            return await orig(chat_id, *a, **k)
        confirm_call._vr_hooked = True
        app.confirm_call = confirm_call

    def _note_connect(self, uid, ok, err=None):
        t = self.accepted_at.pop(int(uid), None)
        secs = round(time.monotonic() - t, 1) if t is not None else None
        self.last_connect = {"connected": ok, "accept_to_connect_s": secs}
        if ok:
            log.info("outgoing call connected %ss after accept", secs)
        elif secs is not None:
            log.warning("outgoing call: accepted, media did not connect after %ss (%s)", secs, err)

    async def start(self):
        from pytgcalls import filters as fl
        from pytgcalls.types import ChatUpdate, Device, Direction
        self._make()
        authorized = await self.app.connect()  # True only for a logged-in session
        await self.app.disconnect()
        if not authorized:
            raise BridgeRefused("the session is not logged in: run voice-tg login start / finish")
        self.tg.on_update(fl.chat_update(ChatUpdate.Status.INCOMING_CALL))(self._on_incoming)
        self.tg.on_update(fl.stream_frame(Direction.INCOMING, Device.MICROPHONE | Device.SPEAKER))(self._on_frames)
        self.tg.on_update(fl.chat_update(ChatUpdate.Status.DISCARDED_CALL | ChatUpdate.Status.BUSY_CALL))(
            self._on_ended)
        await self.tg.start()  # starts the pyrogram client (already authorized, so no prompt)
        me = await self.app.get_me()
        return {"id": me.id, "is_bot": bool(me.is_bot)}

    async def ping(self):
        """get_me on the running client (raises when the session was revoked or the connection is gone)."""
        if self.app is None or not getattr(self.app, "is_connected", False):
            raise ConnectionError("not connected")
        me = await self.app.get_me()
        return {"id": me.id, "is_bot": bool(me.is_bot)}

    async def stop(self):
        if self.app is not None and getattr(self.app, "is_connected", False):
            await self.app.stop()

    async def _on_incoming(self, _client, u):
        if self._in is not None and int(u.chat_id) > 0:  # p2p only; group/conference invites are ignored
            await self._in(int(u.chat_id), int(u.chat_id))

    async def _on_frames(self, _client, u):
        if self._fr is None:
            return
        for f in u.frames:
            await self._fr(int(u.chat_id), f.frame)

    async def _on_ended(self, _client, u):
        if self._end is not None:
            await self._end(int(u.chat_id))

    def on_incoming(self, cb):
        self._in = cb

    def on_frames(self, cb):
        self._fr = cb

    def on_ended(self, cb):
        self._end = cb

    def on_media(self, cb, owner_id, group=7):
        """Big-file inbox (bigfiles.py) on this same live connection: cb(PyroMedia) for each private, incoming
        message with media from owner_id only. The filter runs before anything is read or downloaded; other
        senders never reach cb. Its own handler group, so py-tgcalls' raw handler (group -9999) is untouched."""
        from pyrogram.handlers import MessageHandler
        owner_id = int(owner_id or 0)
        if owner_id <= 0 or self.app is None:
            raise BridgeRefused("file inbox: no owner id or no client")
        app = self.app

        async def handler(_client, m):
            kind = media_kind(m)
            if kind is not None:
                cb(PyroMedia(app, m, kind))

        self.app.add_handler(MessageHandler(handler, owner_media_filter(owner_id)), group)

    def _params(self):
        from pytgcalls.types.raw import AudioParameters
        return AudioParameters(self.rate, self.channels)

    async def accept(self, cid):
        from pytgcalls.types import ExternalMedia, MediaStream, RecordStream
        await self.tg.play(int(cid), MediaStream(ExternalMedia.AUDIO, self._params(),
                                                 video_flags=MediaStream.Flags.IGNORE))
        await self.tg.record(int(cid), RecordStream(True, self._params()))

    async def dial(self, uid, timeout):
        """Outgoing private call: play() on a user id requests the call and waits up to CallConfig.timeout for
        the answer; raises TimedOutAnswer (and discards the call), CallDeclined or CallBusy."""
        if self.callee is None or int(self.callee) <= 0 or int(uid) != int(self.callee):
            raise BridgeRefused("outbound calls go to the owner only")
        from pytgcalls.types import CallConfig, ExternalMedia, MediaStream, RecordStream
        self.accepted_at.pop(int(uid), None)
        self.last_connect = None
        try:
            await self.tg.play(int(uid), MediaStream(ExternalMedia.AUDIO, self._params(),
                                                     video_flags=MediaStream.Flags.IGNORE),
                               CallConfig(timeout=int(timeout)))
        except BaseException as e:
            self._note_connect(uid, False, type(e).__name__)
            if type(e).__name__ not in RING_STATUS:
                # e.g. TelegramServerError (media never connected after his accept): py-tgcalls raises without
                # stopping or discarding a private call, so his phone would sit in a dead call. Hang up actively.
                await self._leave(uid)
            raise
        self._note_connect(uid, True)
        await self.tg.record(int(uid), RecordStream(True, self._params()))

    async def decline(self, cid):
        await self._leave(cid)

    async def hangup(self, cid):
        await self._leave(cid)

    async def _leave(self, cid):
        try:
            await self.tg.leave_call(int(cid))
            return
        except Exception as e:  # NotInCallError: the media side is gone (ntgcalls dropped a failed connection)
            log.info("leave_call: %s", type(e).__name__)
        app = getattr(self.tg, "_app", None)   # ... but the call itself may still ring/sit open on his phone
        if app is None or not hasattr(app, "discard_call"):
            return
        try:
            await app.discard_call(int(cid), False)  # phone.discardCall (hangup); no-op once the call is gone
        except Exception as e:
            log.info("discard_call: %s", type(e).__name__)

    async def send_frame(self, cid, pcm):
        from pytgcalls.types import Device
        await self.tg.send_frame(int(cid), Device.MICROPHONE, pcm)


def owner_media_filter(owner_id):
    """pyrogram filter: an incoming message in the private chat with owner_id, sent by owner_id, carrying media."""
    from pyrogram import filters
    owner_id = int(owner_id)

    async def check(_flt, _client, m):
        u, c = getattr(m, "from_user", None), getattr(m, "chat", None)
        return bool(owner_id > 0 and not getattr(m, "outgoing", False) and u is not None and c is not None
                    and int(u.id) == owner_id and int(c.id) == owner_id and media_kind(m) is not None)

    return filters.create(check, "OwnerMedia")


class PyroMedia:
    """One owner message with media, as FileInbox (bigfiles.py) sees it."""

    def __init__(self, app, m, kind):
        self._app, self._m, self.kind = app, m, kind
        obj = getattr(m, kind)
        self.id, self.chat_id, self.from_id = int(m.id), int(m.chat.id), int(m.from_user.id)
        self.file_name = str(getattr(obj, "file_name", None) or "")
        self.mime = str(getattr(obj, "mime_type", None) or ("image/jpeg" if kind == "photo" else ""))
        self.size = int(getattr(obj, "file_size", None) or 0)
        self.duration = int(getattr(obj, "duration", None) or 0)
        self.caption = str(getattr(m, "caption", None) or "")

    async def download(self, path, guard):
        """Download to path; guard(current, total) returning False aborts (pyrogram's stop_transmission)."""
        app = self._app

        async def progress(current, total):
            if await guard(current, total) is False:
                app.stop_transmission()

        return await self._m.download(file_name=path, progress=progress)

    async def reply(self, text):
        from pyrogram import enums
        await self._m.reply(text, parse_mode=enums.ParseMode.DISABLED)
