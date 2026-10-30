"""Provider adapters behind one small interface. Gemini Live is primary, OpenAI Realtime second.

Both run server-side over WebSocket with the long-lived key, which never leaves this box. Audio is PCM16 mono.
Normalized events (dicts) from Provider.events():
  ready | audio{pcm} | transcript{who,text} | tool_call{id,name,args} | tool_cancel{ids} | interrupted
  | turn_complete | go_away | error{message} | closed{reason}
"""
import asyncio
import base64
import json

import aiohttp

from .config import read_secret
from .decisions import TOOLS as DECISION_TOOLS
from .fastlane import TOOLS as FAST_TOOLS
from .phoneopen import TOOLS as PHONE_TOOLS
from .track import TOOLS as TRACK_TOOLS

ASK_TOOL = {
    "name": "ask_assistant",
    "description": ("Hand a request to the owner's assistant: his main agent with his calendar, home, server, "
                    "media, files, messages, mail and tools. Call it for EVERY request that needs his data, tools, "
                    "home, calendar or media, for EVERY action, and for anything live or current that no quick "
                    "tool covers (news, prices, scores, the state of things). Only small talk and general knowledge stay with you. Several requests: hand "
                    "off each one, never drop one. Never say something is done or that you'll do it without "
                    "calling this. It takes a few seconds: right before calling it, say two or three words (\"One sec.\", "
                    "\"Moment.\") so the line is not silent. Returns a short spoken answer, or says the assistant is "
                    "working on it and will report later."),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "The request, self-contained: his words plus any context "
                                                      "from this conversation that is needed."},
            "urgency": {"type": "string", "enum": ["now", "later"],
                        "description": "now: he waits for the answer in this conversation. later: it can finish "
                                       "in the background."},
            "action": {"type": "boolean",
                       "description": "true when he wants something done or changed (buy, send, add, remind, book, "
                                      "delete, turn on or off, play, set); false for a question."},
        },
        "required": ["task"],
    },
}


END_TOOL = {
    "name": "end_call",
    "description": ("Hang up this call. Only when he is done: he said bye / that's all / danke, tschuess, he "
                    "answered no to \"anything else?\", or a [call] message says to. A plain thanks, done or ok is "
                    "not a goodbye: ask whether there is anything else first. Say one short goodbye FIRST, then call "
                    "this; say nothing after it. If an answer he is waiting for is still pending, say \"I'll send "
                    "the rest on Telegram\" before the goodbye. Never call it in the middle of a conversation or "
                    "while a tool or the assistant is still working on his request."),
    "parameters": {
        "type": "object",
        "properties": {"reason": {"type": "string", "description": "A few words: why the call is over."}},
        "required": ["reason"],
    },
}


GONE_WORDS = ("not found", "not supported", "unknown model", "is not available", "deprecated", "no longer")


def model_gone(reason):
    """A setup close reason that means the model id itself is the problem (not billing, key or our config)."""
    r = (reason or "").lower()
    return "model" in r and any(w in r for w in GONE_WORDS)


class ProviderError(Exception):
    pass


def _gemini_schema(s):
    """JSON schema → the OpenAPI subset Gemini declarations use (upper-case types)."""
    out = {}
    for k, v in s.items():
        if k == "type":
            out[k] = str(v).upper()
        elif k == "properties":
            out[k] = {n: _gemini_schema(p) for n, p in v.items()}
        elif k == "items":
            out[k] = _gemini_schema(v)
        else:
            out[k] = v
    return out


class Provider:
    name = "base"
    input_rate = 16000
    output_rate = 24000

    def __init__(self, cfg, instructions, tools, mode="ptt", key=None, url=None):
        self.cfg, self.instructions, self.tools, self.mode = cfg, instructions, tools, mode
        self.key, self.url = key, url
        self._http = self._ws = None
        self._q = asyncio.Queue()
        self._reader = None
        self.conn = 0                        # sockets opened so far (events carry theirs)
        self.closed = False
        self.close_reason = ""

    async def _open(self, url, headers):
        self._http = self._http or aiohttp.ClientSession()
        try:
            self._ws = await self._http.ws_connect(url, headers=headers, heartbeat=30, max_msg_size=16 << 20)
        except (aiohttp.ClientError, OSError) as e:
            raise ProviderError("connect failed: %s" % type(e).__name__) from None

    async def _send(self, obj):
        if self._ws is None or self._ws.closed:
            raise ProviderError("not connected")
        await self._ws.send_str(json.dumps(obj))

    async def _recv_json(self, timeout=None):
        msg = await (asyncio.wait_for(self._ws.receive(), timeout) if timeout else self._ws.receive())
        if msg.type in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
            data = msg.data if isinstance(msg.data, str) else msg.data.decode("utf-8")
            return json.loads(data)
        if msg.type == aiohttp.WSMsgType.ERROR:
            raise ProviderError("socket error")
        if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED):
            self.close_reason = str(msg.extra or "").replace("\n", " ")[:160]   # e.g. a billing notice
        return None  # closed

    async def _read_loop(self):
        conn = self.conn                     # this socket's number: a late "closed" from an older one is stale
        try:
            while True:
                d = await self._recv_json()
                if d is None:
                    break
                for ev in self.normalize(d):
                    await self._q.put(ev)
        except (ProviderError, ValueError, aiohttp.ClientError) as e:
            await self._q.put({"type": "error", "message": str(e)[:200], "conn": conn})
        except asyncio.CancelledError:
            return
        code = getattr(self._ws, "close_code", None)
        await self._q.put({"type": "closed", "reason": "provider closed (%s)" % code, "conn": conn})

    def _start_reader(self):
        self.conn += 1
        self._reader = asyncio.ensure_future(self._read_loop())

    async def events(self):
        while True:
            ev = await self._q.get()
            yield ev
            if ev["type"] == "closed":
                return

    async def close(self):
        self.closed = True
        if self._reader:
            self._reader.cancel()
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        if self._http is not None:
            await self._http.close()
        await self._q.put({"type": "closed", "reason": "closed"})

    # interface
    async def connect(self): raise NotImplementedError
    async def send_audio(self, pcm): raise NotImplementedError
    async def activity(self, start): raise NotImplementedError
    async def send_text(self, text): raise NotImplementedError
    async def tool_result(self, call_id, name, result, scheduling=None): raise NotImplementedError
    async def inject(self, text): raise NotImplementedError
    async def prompt(self, text): raise NotImplementedError
    def normalize(self, d): raise NotImplementedError


class GeminiLive(Provider):
    """Gemini Live API (BidiGenerateContent over WebSocket). Primary provider."""
    name = "gemini"
    input_rate = 16000
    output_rate = 24000

    transcribes_input = True      # inputAudioTranscription: his speech arrives as user transcripts

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.resume_handle = None
        self.model = self.cfg.gemini_model       # the model this session runs on (a fallback after a rejection)
        self.fallback_from = None                # set when the configured model was rejected
        self.resumed = False                     # setup carried a handle from an earlier call and was accepted
        self.call_ids = set()                    # function calls issued on the CURRENT connection (stale after a
                                                 # reconnect: their results go in as an [assistant result] note)
        self.setup_timeout_s = 15.0

    def models(self):
        seen, out = set(), []
        for m in (self.model, self.cfg.gemini_model) + tuple(self.cfg.gemini_fallbacks or ()):
            if m and m not in seen:
                seen.add(m)
                out.append(m)
        return out

    def setup_message(self):
        auto = {"disabled": self.mode == "ptt"}
        if self.mode != "ptt":
            auto.update(silenceDurationMs=int(self.cfg.vad_silence_ms), endOfSpeechSensitivity="END_SENSITIVITY_HIGH")
        decls = []
        for t in self.tools:
            d = {"name": t["name"], "description": t["description"], "parameters": _gemini_schema(t["parameters"])}
            if self.cfg.gemini_async:
                d["behavior"] = "NON_BLOCKING"
            decls.append(d)
        setup = {
            "model": "models/" + self.model,
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": self.cfg.gemini_voice}}},
            },
            "systemInstruction": {"parts": [{"text": self.instructions}]},
            "tools": [{"functionDeclarations": decls}],
            "realtimeInputConfig": {"automaticActivityDetection": auto},
            "inputAudioTranscription": {},
            "outputAudioTranscription": {},
            "contextWindowCompression": {"slidingWindow": {}},
            "sessionResumption": {"handle": self.resume_handle} if self.resume_handle else {},
        }
        return {"setup": setup}

    async def connect(self):
        """Setup on the configured model; a resumption handle that is refused is dropped and the setup retried
        fresh; a model Gemini rejects as gone/unknown/unsupported → the next of cfg.gemini_fallbacks."""
        key = self.key or read_secret(self.cfg.gemini_key_file)
        if not key:
            raise ProviderError("no Gemini API key (%s)" % self.cfg.gemini_key_file)
        models = self.models()
        for i, model in enumerate(models):
            self.model = model
            try:
                await self._setup(key)
            except ProviderError as e:
                if self.resume_handle:               # an expired/foreign handle: same model, fresh conversation
                    self.resume_handle = None
                    try:
                        await self._setup(key)
                    except ProviderError as e2:
                        e = e2
                    else:
                        break
                if model_gone(self.close_reason) and i + 1 < len(models):
                    self.fallback_from = self.fallback_from or models[0]
                    continue
                raise e
            else:
                self.resumed = bool(self.resume_handle)
                self.call_ids = set()
                break
        self._start_reader()
        await self._q.put({"type": "ready"})

    async def _setup(self, key):
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        self.close_reason = ""
        await self._open(self.url or self.cfg.gemini_ws_url, {"x-goog-api-key": key})
        await self._send(self.setup_message())
        try:
            d = await self._recv_json(timeout=self.setup_timeout_s)
        except asyncio.TimeoutError:
            raise ProviderError("setup timed out") from None
        if not d or "setupComplete" not in d:
            why = (": " + self.close_reason) if self.close_reason else ""
            raise ProviderError("setup rejected (close code %s%s)" % (getattr(self._ws, "close_code", None), why))

    async def reconnect(self):
        """After goAway or a dropped socket: reopen with the resumption handle so the conversation continues
        (setup capped at cfg.reconnect_timeout_s; a refused handle → a fresh session on the same model)."""
        if self._reader:
            self._reader.cancel()
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        self.setup_timeout_s = float(getattr(self.cfg, "reconnect_timeout_s", 0) or 15.0)
        try:
            await self.connect()
        finally:
            self.setup_timeout_s = 15.0

    async def send_audio(self, pcm):
        await self._send({"realtimeInput": {"audio": {"data": base64.b64encode(pcm).decode(),
                                                      "mimeType": "audio/pcm;rate=%d" % self.input_rate}}})

    async def activity(self, start):
        if self.mode == "ptt":
            await self._send({"realtimeInput": {"activityStart": {}} if start else {"activityEnd": {}}})
        elif not start:
            await self._send({"realtimeInput": {"audioStreamEnd": True}})

    async def send_text(self, text):
        await self._send({"realtimeInput": {"text": text}})

    async def tool_result(self, call_id, name, result, scheduling=None):
        """scheduling (async tools): WHEN_IDLE by default; SILENT = the model says nothing about it (end_call).
        A call from an earlier connection (the socket dropped or a goAway switched it since): its id means nothing
        to the new one, so an answer / say goes in as an [assistant result] note and anything else is dropped."""
        if call_id and call_id not in self.call_ids:
            text = result.get("answer") or result.get("say") if isinstance(result, dict) else None
            if text and scheduling != "SILENT":
                await self.inject("%s: %s" % (name, text))
            return
        self.call_ids.discard(call_id)
        resp = dict(result)
        if self.cfg.gemini_async:
            resp["scheduling"] = scheduling or "WHEN_IDLE"
        await self._send({"toolResponse": {"functionResponses": [{"id": call_id, "name": name, "response": resp}]}})

    async def inject(self, text):
        await self.prompt("[assistant result] " + text)

    async def prompt(self, text):
        await self._send({"clientContent": {"turns": [{"role": "user", "parts": [{"text": text}]}],
                                            "turnComplete": True}})

    async def note(self, text):
        """Context without a turn: the model reads it and says nothing until he speaks."""
        await self._send({"clientContent": {"turns": [{"role": "user", "parts": [{"text": text}]}],
                                            "turnComplete": False}})

    def normalize(self, d):
        out = []
        sc = d.get("serverContent")
        if sc:
            if sc.get("interrupted"):
                out.append({"type": "interrupted"})
            for key, who in (("inputTranscription", "user"), ("outputTranscription", "assistant")):
                t = (sc.get(key) or {}).get("text")
                if t:
                    out.append({"type": "transcript", "who": who, "text": t})
            for part in (sc.get("modelTurn") or {}).get("parts") or []:
                inl = part.get("inlineData") or {}
                if str(inl.get("mimeType", "")).startswith("audio/pcm") and inl.get("data"):
                    out.append({"type": "audio", "pcm": base64.b64decode(inl["data"])})
            if sc.get("turnComplete"):
                out.append({"type": "turn_complete"})
        for fc in (d.get("toolCall") or {}).get("functionCalls") or []:
            if fc.get("id"):
                self.call_ids.add(fc["id"])
            out.append({"type": "tool_call", "id": fc.get("id", ""), "name": fc.get("name", ""),
                        "args": fc.get("args") or {}})
        if "toolCallCancellation" in d:
            out.append({"type": "tool_cancel", "ids": d["toolCallCancellation"].get("ids") or []})
        upd = d.get("sessionResumptionUpdate")
        if upd and upd.get("resumable") and upd.get("newHandle"):
            self.resume_handle = upd["newHandle"]
        if "goAway" in d:
            out.append({"type": "go_away"})
        return out


class OpenAIRealtime(Provider):
    """OpenAI Realtime over WebSocket (server-side). Second provider; untested against the live API."""
    name = "openai"
    input_rate = 24000
    output_rate = 24000

    async def connect(self):
        key = self.key or read_secret(self.cfg.openai_key_file)
        if not key:
            raise ProviderError("no OpenAI API key (%s)" % self.cfg.openai_key_file)
        url = (self.url or self.cfg.openai_ws_url) + "?model=" + self.cfg.openai_model
        await self._open(url, {"Authorization": "Bearer " + key})
        fmt = {"type": "audio/pcm", "rate": 24000}
        await self._send({"type": "session.update", "session": {
            "type": "realtime", "instructions": self.instructions,
            "audio": {"input": {"format": fmt, "transcription": {"model": "gpt-4o-mini-transcribe"},
                                "turn_detection": None if self.mode == "ptt" else {"type": "semantic_vad"}},
                      "output": {"format": fmt, "voice": self.cfg.openai_voice}},
            "tools": [dict(type="function", **t) for t in self.tools], "tool_choice": "auto"}})
        self._start_reader()
        await self._q.put({"type": "ready"})

    async def send_audio(self, pcm):
        await self._send({"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode()})

    async def activity(self, start):
        if self.mode != "ptt":
            return
        if start:
            await self._send({"type": "response.cancel"})
            await self._send({"type": "input_audio_buffer.clear"})
        else:
            await self._send({"type": "input_audio_buffer.commit"})
            await self._send({"type": "response.create"})

    async def send_text(self, text):
        await self._send({"type": "conversation.item.create", "item": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}})
        await self._send({"type": "response.create"})

    async def tool_result(self, call_id, name, result, scheduling=None):
        await self._send({"type": "conversation.item.create", "item": {
            "type": "function_call_output", "call_id": call_id, "output": json.dumps(result)}})
        await self._send({"type": "response.create"})

    async def inject(self, text):
        await self.prompt("[assistant result] " + text)

    async def prompt(self, text):
        await self._send({"type": "conversation.item.create", "item": {
            "type": "message", "role": "system", "content": [{"type": "input_text", "text": text}]}})
        await self._send({"type": "response.create"})

    def normalize(self, d):
        t = d.get("type", "")
        if t in ("response.output_audio.delta", "response.audio.delta"):
            return [{"type": "audio", "pcm": base64.b64decode(d.get("delta", ""))}]
        if t in ("response.output_audio_transcript.delta", "response.audio_transcript.delta"):
            return [{"type": "transcript", "who": "assistant", "text": d.get("delta", "")}]
        if t == "conversation.item.input_audio_transcription.completed":
            return [{"type": "transcript", "who": "user", "text": d.get("transcript", "")}]
        if t == "response.function_call_arguments.done":
            try:
                args = json.loads(d.get("arguments") or "{}")
            except ValueError:
                args = {}
            return [{"type": "tool_call", "id": d.get("call_id", ""), "name": d.get("name", ""), "args": args}]
        if t == "input_audio_buffer.speech_started":
            return [{"type": "interrupted"}]
        if t == "response.done":
            return [{"type": "turn_complete"}]
        if t == "error":
            return [{"type": "error", "message": str((d.get("error") or {}).get("message", ""))[:200]}]
        return []


PROVIDERS = {"gemini": GeminiLive, "openai": OpenAIRealtime}


def make_provider(cfg, instructions, mode="ptt", name=None):
    cls = PROVIDERS.get(name or cfg.provider)
    if cls is None:
        raise ProviderError("unknown provider %r" % (name or cfg.provider))
    tools = [ASK_TOOL] + (list(FAST_TOOLS) if cfg.fast_lane else []) + (list(TRACK_TOOLS) if cfg.track_id else []) \
        + (list(DECISION_TOOLS) if cfg.decisions else []) + (list(PHONE_TOOLS) if cfg.phone_open else []) \
        + ([END_TOOL] if cfg.end_call else [])
    return cls(cfg, instructions, tools, mode)
