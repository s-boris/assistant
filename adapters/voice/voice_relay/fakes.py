"""Fakes for tests and the local demo: a Gemini Live protocol fake, the session hand-off (patched plugin +
main session answering via voice-reply), and a Telegram call client."""
import asyncio
import base64
import json
import time
from collections import defaultdict

import aiohttp
from aiohttp import web

from .audio import frames, tone


async def _serve(app):
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=0.5)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    return runner, runner.addresses[0][1]


class FakeGemini:
    """Speaks the Gemini Live BidiGenerateContent JSON protocol (binary frames, like the real API).
    PTT: activityEnd after audio → a scripted utterance. Open mic: each 1 s of audio → an utterance.
    Text → utterance. Greetings get small talk; "bye …" → a goodbye + end_call; anything else → ask_assistant tool
    call. Mute resumption (seen live on Gemini 3.8 after a call the model ended with end_call): a handle issued to a
    session that answered an end_call is poisoned, and a session resumed from it (or from any handle while
    mute_resumed is set) never speaks: his audio/text only gets an input transcript + "interrupted", prompts nothing;
    its own new handle is poisoned too."""
    def __init__(self, key="fake-key", script=None, models=None):
        self.key = key
        self.script = list(script or [])
        self.models = models             # None = any model id; else only these (others: close 1008 "not found")
        self.handles = set()             # resumption handles handed out; any other handle is refused
        self.log = {"setups": [], "tool_responses": [], "injects": [], "texts": [], "audio_bytes": 0, "auth_fail": 0,
                    "notes": [], "rejected": []}
        self.calls = 0
        self.live = set()                # open sockets (drop() closes them: a network blip / a 1011 from Gemini)
        self.fail_setups = 0             # refuse this many next setups (close 1011 before setupComplete)
        self.poisoned = set()            # handles whose resumed session is mute (the session answered an end_call)
        self.mute_resumed = False        # True: every resumed session is mute

    async def drop(self, code=1011):
        """Close every open session socket from the server side, like Gemini's 1011 or a dead uplink."""
        for ws in list(self.live):
            await ws.close(code=code, message=b"Internal error encountered.")

    async def start(self):
        app = web.Application()
        app.router.add_get("/ws", self.ws)
        self.runner, self.port = await _serve(app)
        return self.port

    async def stop(self):
        await self.runner.cleanup()

    async def ws(self, request):
        if request.headers.get("x-goog-api-key") != self.key:
            self.log["auth_fail"] += 1
            return web.Response(status=401)
        ws = web.WebSocketResponse(max_msg_size=16 << 20)
        await ws.prepare(request)

        async def send(obj):
            await ws.send_bytes(json.dumps(obj).encode())

        heard, mode = 0, "ptt"
        self.live.add(ws)
        try:
            return await self._serve_ws(ws, send)
        finally:
            self.live.discard(ws)

    async def _serve_ws(self, ws, send):
        heard, mode, mine, mute = 0, "ptt", None, False
        async for msg in ws:
            d = json.loads(msg.data)
            if "setup" in d:
                self.log["setups"].append(d["setup"])
                if self.fail_setups > 0:
                    self.fail_setups -= 1
                    self.log["rejected"].append("setup")
                    await ws.close(code=1011, message=b"Internal error encountered.")
                    return ws
                model = str(d["setup"].get("model", "")).split("/", 1)[-1]
                handle = (d["setup"].get("sessionResumption") or {}).get("handle")
                if self.models is not None and model not in self.models:
                    self.log["rejected"].append(model)
                    await ws.close(code=1008, message=("models/%s is not found for API version v1beta, or is not "
                                                       "supported for bidiGenerateContent." % model).encode())
                    return ws
                if handle and handle not in self.handles:
                    self.log["rejected"].append("handle")
                    await ws.close(code=1008, message=b"Invalid session resumption handle.")
                    return ws
                auto = d["setup"].get("realtimeInputConfig", {}).get("automaticActivityDetection", {})
                mode = "ptt" if auto.get("disabled") else "open"
                mute = bool(handle) and (self.mute_resumed or handle in self.poisoned)
                if mute:
                    self.log.setdefault("mute_sessions", 0)
                    self.log["mute_sessions"] += 1
                await send({"setupComplete": {}})
                new = mine = "h-%d" % (len(self.handles) + 1)
                self.handles.add(new)
                if mute:
                    self.poisoned.add(new)
                await send({"sessionResumptionUpdate": {"newHandle": new, "resumable": True}})
            elif mute and ("realtimeInput" in d or "clientContent" in d):
                ri = d.get("realtimeInput") or {}
                if "audio" in ri:
                    n = len(base64.b64decode(ri["audio"]["data"]))
                    heard += n
                    self.log["audio_bytes"] += n
                    self.log["mute_audio"] = self.log.get("mute_audio", 0) + n
                    if mode == "open" and heard >= 32000 and self.script:
                        heard = 0
                        await send({"serverContent": {"interrupted": True}})
                        await send({"serverContent": {"inputTranscription": {"text": self.script.pop(0)}}})
                elif "text" in ri:
                    await send({"serverContent": {"inputTranscription": {"text": ri["text"]}}})
                else:
                    text = " ".join(p.get("text", "") for t in (d.get("clientContent") or {}).get("turns", [])
                                    for p in t.get("parts", []))
                    if text:
                        self.log.setdefault("mute_prompts", []).append(text)
            elif "realtimeInput" in d:
                ri = d["realtimeInput"]
                if "audio" in ri:
                    n = len(base64.b64decode(ri["audio"]["data"]))
                    heard += n
                    self.log["audio_bytes"] += n
                    if mode == "open" and heard >= 32000 and self.script:
                        heard = 0
                        await self._utter(send, self.script.pop(0))
                elif "activityStart" in ri:
                    heard = 0
                elif "activityEnd" in ri or "audioStreamEnd" in ri:
                    if heard > 0:
                        await self._utter(send, self.script.pop(0) if self.script else "what's on my calendar tomorrow")
                    heard = 0
                elif "text" in ri:
                    self.log["texts"].append(ri["text"])
                    await self._utter(send, ri["text"], echo=False)
            elif "toolResponse" in d:
                for fr in d["toolResponse"].get("functionResponses", []):
                    self.log["tool_responses"].append(fr)
                    r = fr.get("response") or {}
                    if fr.get("name") == "end_call":
                        if mine:
                            self.poisoned.add(mine)          # this conversation now resumes mute
                        if r.get("scheduling") == "SILENT" or not r.get("say"):
                            continue
                    await self._say(send, r.get("answer") or r.get("say") or r.get("error") or "ok")
            elif "clientContent" in d:
                text = " ".join(p.get("text", "") for t in d["clientContent"].get("turns", []) for p in t.get("parts", []))
                if d["clientContent"].get("turnComplete") is False:     # context only: the model stays quiet
                    self.log["notes"].append(text)
                    continue
                self.log["injects"].append(text)
                await self._say(send, text.replace("[assistant result] ", ""))
        return ws

    async def _utter(self, send, text, echo=True):
        if echo:
            await send({"serverContent": {"inputTranscription": {"text": text}}})
        low = text.lower()
        if low.startswith(("hi", "hello", "hallo", "hey")):
            await self._say(send, "Hi! What can I do for you?")
            return
        self.calls += 1
        if low.startswith("bye"):            # "bye …" → goodbye + end_call
            await self._say(send, "Goodbye!")
            await send({"toolCall": {"functionCalls": [{"id": "fc-%d" % self.calls, "name": "end_call",
                                                        "args": {"reason": "he said bye"}}]}})
            return
        await send({"serverContent": {"outputTranscription": {"text": "Let me check. "}}})
        if low.startswith("pick "):           # "pick ITEM CHOICE…" → record_pick
            _, item, choice = (text.split(None, 2) + ["", ""])[:3]
            await send({"toolCall": {"functionCalls": [{"id": "fc-%d" % self.calls, "name": "record_pick",
                                                        "args": {"item": item, "choice": choice}}]}})
            return
        if low.startswith("quick "):          # "quick weather" → that quick (fast-lane) tool
            await send({"toolCall": {"functionCalls": [{"id": "fc-%d" % self.calls, "name": low.split()[1],
                                                        "args": {}}]}})
            return
        await send({"toolCall": {"functionCalls": [{"id": "fc-%d" % self.calls, "name": "ask_assistant", "args": {
            "task": text, "urgency": "later" if "later" in low else "now"}}]}})

    async def _say(self, send, text):
        await send({"serverContent": {"outputTranscription": {"text": text}}})
        pcm = tone(24000, 200)
        await send({"serverContent": {"modelTurn": {"parts": [{"inlineData": {
            "mimeType": "audio/pcm;rate=24000", "data": base64.b64encode(pcm).decode()}}]}}})
        await send({"serverContent": {"turnComplete": True}})


class FakePluginInbox:
    """Stands in for the patched telegram-plus voice inbox AND the main session: receives a task, then answers it
    through the relay's internal API like `voice-reply` would. Tasks containing 'slow' wait slow_delay."""
    def __init__(self, token, relay_internal, delay=0.05, slow_delay=3.0, answer_fn=None, auto=True):
        self.token, self.relay_internal = token, relay_internal
        self.delay, self.slow_delay, self.auto = delay, slow_delay, auto
        self.answer_fn = answer_fn or (lambda task: "Fake answer about: %s. Call 079 123 45 67 for details." % task)
        self.tasks, self.results = [], []

    async def start(self):
        app = web.Application()
        app.router.add_post("/voice", self.voice)
        self.runner, self.port = await _serve(app)
        return self.port

    async def stop(self):
        await self.runner.cleanup()

    async def voice(self, request):
        if request.headers.get("Authorization") != "Bearer " + self.token:
            return web.json_response({"error": "unauthorized"}, status=401)
        body = await request.json()
        body["t_received"] = time.monotonic()
        self.tasks.append(body)
        if self.auto:
            asyncio.ensure_future(self._answer(body))
        return web.json_response({"ok": True}, status=202)

    async def _answer(self, body):
        await asyncio.sleep(self.slow_delay if "slow" in body["task"].lower() else self.delay)
        await self.reply(body["id"], self.answer_fn(body["task"]))

    async def reply(self, vid, text, more=False):
        async with aiohttp.ClientSession() as s:
            async with s.post(self.relay_internal + "/internal/answer", json={"id": vid, "text": text, "more": more},
                              headers={"Authorization": "Bearer " + self.token}) as r:
                res = await r.json()
        self.results.append(res)
        return res


class TimedOutAnswer(Exception):
    pass


class CallDeclined(Exception):
    pass


class CallBusy(Exception):
    pass


class FakeCallClient:
    """The CallClient interface (see tgcall.py) without Telegram: tests ring, speak and hang up.
    dial_outcome for outbound calls: answer | no_answer | declined | busy | error."""
    rate = 48000

    def __init__(self, me_id=4242, is_bot=False, dial_outcome="answer", dial_delay=0.0):
        self.me = {"id": me_id, "is_bot": is_bot}
        self.callee, self.dialed = None, []
        self.dial_outcome, self.dial_delay = dial_outcome, dial_delay
        self.accepted, self.declined, self.hung = [], [], []
        self.sent = defaultdict(bytearray)
        self.frame_times = defaultdict(list)

    async def start(self):
        return dict(self.me)

    async def ping(self):
        if getattr(self, "ping_error", None):
            raise self.ping_error
        return dict(self.me)

    def on_incoming(self, cb):
        self._in = cb

    def on_frames(self, cb):
        self._fr = cb

    def on_ended(self, cb):
        self._end = cb

    async def accept(self, cid):
        self.accepted.append(cid)

    async def decline(self, cid):
        self.declined.append(cid)

    async def dial(self, uid, timeout):
        self.dialed.append(uid)
        if self.callee is None or int(uid) != int(self.callee):
            raise RuntimeError("outbound calls go to the owner only")
        await asyncio.sleep(self.dial_delay)
        exc = {"no_answer": TimedOutAnswer, "declined": CallDeclined, "busy": CallBusy,
               "error": RuntimeError}.get(self.dial_outcome)
        if exc:
            raise exc()

    async def hangup(self, cid):
        self.hung.append(cid)

    async def send_frame(self, cid, pcm):
        self.sent[cid] += pcm
        self.frame_times[cid].append(time.monotonic())

    async def ring(self, cid, caller_id):
        await self._in(cid, caller_id)

    async def speak(self, cid, pcm48):
        for f in frames(pcm48, 48000, 10):
            await self._fr(cid, f)

    async def end(self, cid):
        await self._end(cid)
