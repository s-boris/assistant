"""One live voice session: an audio front end (web page or Telegram call) ↔ a provider adapter, with the
ask_assistant tool wired to the hand-off. Front ends deliver/receive PCM16 mono at their own rates."""
import asyncio
import time
from collections import deque

from . import closing, decisions, fastlane, phoneopen, recap, track
from .audio import cue, resample, rms
from .calllog import NullLog, ended_by

WORKING_SAY = "It's being handled. I'll tell you when it's done."
# a "now" hand-off still open after cfg.sync_wait_s: say so plainly, keep him free to stay on or hang up
SLOW_SAY = ("This one takes a bit longer. Stay on and I'll tell you as soon as it's in, or hang up and it comes "
            "on Telegram.")
# the Gemini socket dropped mid-call and came back (_recover): the model speaks first, briefly
RECONNECTED = ("[call] The line dropped for a few seconds and is back now; the conversation continues. Say in a few "
               "words that you lost him for a moment; if he was asking something, ask him to say it again. Don't "
               "greet him again.")
RECONNECTED_FRESH = ("[call] The line dropped and came back, but your memory of this call is gone. Say sorry, you "
                     "lost him for a moment, and ask what he needs. Don't greet him again.")
MAX_RECOVERIES = 3                       # per call: a socket that keeps dying (quota, billing) ends the call
# a call within cfg.resume_window_s continues the last conversation: this context note (no turn) opens it, so the
# model knows a NEW call is on and talks (the old "wait for him" note left resumed sessions mute)
NEW_CALL_IN = ("[new call] The previous call ended; he is calling again (%s). The conversation continues and he may "
               "follow up on it: answer him as soon as he speaks, as usual.")
NEW_CALL_OUT = ("[new call] The previous call ended; your assistant is calling him again (%s). The conversation "
                "continues; you'll be told when he picks up.")
# the call ended with the assistant's goodbye (end_call or the closing flow): that conversation is over, and Gemini
# resumed such sessions mute (his speech only "interrupted", no audio, no tool call): the next call starts fresh
ASSISTANT_ENDS = frozenset(("end_call", "goodbye", "nothing_else", "thanks", "closing_silence", "asked_silence",
                            "idle_silence", "pending_timeout"))
GREET_FLOOR_MAX = 1500.0                 # pick-up line: a "noise floor" louder than this is him talking, not the line
RESUME_BUF_S = 12.0                      # his audio kept while a resumed session is unproven (replayed on fallback)


def link_back_text(gap_ms, missed=False, to_telegram=False):
    """[call] prompt when the phone app is back on the call after its connection dropped (phonegw's grace window)."""
    t = ("[call] His phone's connection dropped for about %d seconds and is back now; the conversation continues. "
         "Say in a few words that you lost him for a moment. " % max(1, int(round((gap_ms or 0) / 1000.0))))
    if to_telegram:
        t += "An answer he was waiting for went to Telegram meanwhile: tell him it's there. "
    if missed:
        t += "He missed what you said while the line was down: say it again briefly. "
    else:
        t += "If he says something now, answer that; if he was asking something you didn't catch, ask him again. "
    return t + "Don't greet him again."
_RECAPS = set()                        # recap tasks outlive their session (grace period after the hang-up)
MORE_SAY = ("This is the first part; the assistant is still working on the rest, which comes as an "
            "[assistant result]. Say this part now.")
ACTION_TAG = "[action] "                 # task prefix for the main session: a side effect → confirm card first


def _cause(why):
    """A provider close reason → a short class for usage attrs (no free text)."""
    w = str(why or "").lower()
    for code in ("1011", "1006", "1001", "1000", "1008"):
        if code in w:
            return "close_" + code
    return "go_away" if "go_away" in w else "socket" if ("socket" in w or "error" in w) else "closed"


class ResumeStore:
    """The last call's Gemini resumption handle, memory only: a call starting within cfg.resume_window_s takes it
    (once) and continues that conversation. One owner, so one slot."""
    def __init__(self):
        self.handle, self.t = None, 0.0

    def put(self, handle):
        if handle:
            self.handle, self.t = handle, time.monotonic()

    def take(self, window_s):
        h, fresh = self.handle, self.handle and window_s > 0 and time.monotonic() - self.t <= window_s
        self.handle = None
        return h if fresh else None

    def clear(self):
        self.handle, self.t = None, 0.0


def new_call_cue(outbound):
    return (NEW_CALL_OUT if outbound else NEW_CALL_IN) % time.strftime("%H:%M")


RESUME = ResumeStore()


class FrontEnd:
    """What an audio front end implements. in_rate: rate of PCM it feeds in; out_rate: rate it wants back."""
    in_rate = 16000
    out_rate = 24000

    async def play(self, pcm): pass
    async def flush(self): pass               # barge-in: drop queued assistant audio
    async def event(self, ev): pass           # transcript / status / metric (UIs show them; calls ignore)
    async def hangup(self, reason): pass
    async def drained(self): pass             # returns once queued assistant audio has been played out


class VoiceSession:
    def __init__(self, cfg, provider, frontend, handoff, limit_s=None, on_end=None, log=None, fast=None,
                 deck_loader=None, track_matcher=None, phone=None, ulog=None):
        self.cfg, self.provider, self.fe, self.handoff = cfg, provider, frontend, handoff
        self._ulog = ulog                # usage writer (closing.usage_log(): ~/bin/lib/usage.py log); tests inject
        # navigate / open_on_phone: open nav, links, SBB connections and apps on his phone (phoneopen.py)
        self.phone = phone if phone is not None else (phoneopen.PhoneOpen(cfg) if cfg.phone_open else None)
        self.fast = fast if fast is not None else (fastlane.FastLane(cfg) if cfg.fast_lane else None)
        # identify_track: his last ~20 s of incoming audio, in memory only, cleared at call end (track.py)
        self.track = track.TrackTool(cfg, frontend.in_rate, track_matcher) if cfg.track_id else None
        self.items = []                  # hand-offs of this call, for the recap note (recap.py)
        self.round = decisions.Round(cfg, deck_loader)   # decision round: deck + his picks (decisions.py)
        self.log = log or NullLog()      # per-call log (calllog.CallLog): local only
        self.limit_s = limit_s or cfg.max_session_s
        self.on_end = on_end
        self.working = set()
        self.asks = deque()
        self.tasks = set()
        self.metrics = {}
        self.started = None
        self.closed = False
        # ending from our side: end_call tool + silence fallback (open-mic calls)
        self.audio_end = 0.0             # monotonic time the queued assistant audio finishes playing (estimate)
        self.last_audio = 0.0            # last assistant audio chunk received
        self.last_activity = time.monotonic()
        self.idle_stage = 0              # 0 normal, 1 asked "anything else?", 2 said goodbye, 3 hanging up
        # closing flow (closing.py): his words since the assistant last spoke or called a tool, and what they were
        self.him_turn = ""
        self.him_t0 = self.him_t = 0.0   # first / last transcript fragment of that utterance
        self.closing = None              # closing.classify(him_turn): thanks | bye | no | None
        self.closing_asked = False       # that utterance answered an "anything else?"
        self.nudged = False              # the [call] nudge for that utterance was sent
        self.answered = False            # an assistant turn completed since he last spoke or a tool was called
        self.turn_audio = False          # the current assistant turn has produced audio
        self.asked_more = False          # the last assistant turn ended with "anything else?"
        # his thanks / no / bye answered an "anything else?" and the model's reply (no goodbye, no tool) completed:
        # closing_bye_s of quiet → goodbye, the short ending (checked again in _closing_reply_due)
        self.closing_reply = False
        self.cue_t = 0.0                 # the relay made the model speak on its own (late answer, link back)
        self.replies = 0                 # assistant turns with audio completed since he last spoke
        self.bye_wait = float(cfg.idle_bye_s)   # stage 1: this much silence → goodbye
        self.stage_why = None            # what put the call in stage 1 (closing_silence | asked_silence | idle_silence)
        self.end_why = None              # why we ended it, once decided (closing.end_why)
        self.end_flags = {"asked": 0, "nudged": 0, "fallback": False}
        self.asks_inflight = 0           # tool calls running right now (not end_call)
        self.ending = None               # the hang-up task once end_call (or the silence fallback) started it
        self.ending_t = 0.0
        self.turn_text = ""              # assistant transcript since his last words
        self.told_rest = False           # "I'll send the rest on Telegram" was already asked for
        # outbound pick-up hold + reply latency (his end of speech → first reply audio out)
        self.gated = False               # True: his audio is not forwarded yet (open_after_pickup waits for his hello)
        self.last_voice = 0.0            # last incoming frame louder than cfg.voice_rms
        self.voiced = 0                  # incoming frames louder than that, this call
        self.turn_open = False           # assistant audio of the current turn is flowing
        self.turn_end = 0.0              # when the last assistant turn ended (turn_complete / interrupted)
        self.last_tool = (None, 0.0)     # (name, time) of the last tool call
        self.waiting_on = set()          # "now" hand-offs he is waiting on in silence (the thinking cue plays)
        # greeting prefetch (outbound pick-up): the opening is generated during the hold, its audio held back here
        self.out_held = False            # True: assistant audio goes to self.held instead of the front end
        self.held = []                   # PCM (front-end rate) generated while held
        self.held_t = None               # when the first held chunk arrived
        self.held_done = False           # the held turn already completed (turn_complete came while held)
        self.greet_t0 = None             # pick-up time: the first audio out after it → call log "greet"
        self.greet_prefetch = False
        # inbound pick-up line (his calls; greet_inbound): local audio, once per call, cut when he talks over it
        self.greeted = False
        self.greet_until = 0.0           # the line is playing until then (estimate)
        self.greet_voiced_ms = 0.0       # his voice while it plays (local barge-in at cfg.greet_barge_ms)
        self.greet_pre = None            # [(rms, ms)] heard between the line coming up and the line playing
        self.greet_level = 0.0           # his voice = louder than this (voice_rms, or 3x that noise floor)
        # his incoming audio, for the call log's audio_in line (did it arrive, was there voice, did it reach the model)
        self.in_ms = 0.0
        self.voiced_ms = 0.0
        self.sent_ms = 0.0
        self.first_voice_t = None
        self.line_up = False             # the call connected (accepted / picked up / app live): audio_in is logged
        # recovery after the provider socket drops mid-call (_recover)
        self.recovering = False          # reconnecting: his audio is buffered, provider calls wait (_ready)
        self.in_buf = deque()            # his audio while reconnecting (provider rate), at most reconnect_buffer_s
        self.in_buf_bytes = 0
        self.gen = 0                     # provider connection generation (+1 per recovery)
        # the phone app's connection dropped and phonegw holds the call for its grace window (link_lost/link_back)
        self.link_down = False
        self.link_t0 = 0.0
        self.link_spoke = False          # assistant audio went out while the line was down: he missed it
        self.link_tg = False             # a late answer went to Telegram while the line was down
        # a resumed conversation (RESUME) is unproven until the model speaks or calls a tool (_resume_watch)
        self.model_live = False          # model audio or a tool call came in this call
        self.resume_armed = False        # resumed and unproven: his audio is kept, the watch may reopen fresh
        self.resume_buf = deque()        # his audio meanwhile (provider rate), newest RESUME_BUF_S
        self.resume_buf_bytes = 0
        self.opening_t = 0.0             # the last opening prompt went out (outbound pick-up)
        self.opening_last = None         # ...its text: sent again on a fresh session after a resume fallback

    def _spawn(self, coro):
        t = asyncio.ensure_future(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)
        return t

    async def start(self):
        if hasattr(self.provider, "resume_handle") and not self.provider.resume_handle:
            self.provider.resume_handle = RESUME.take(getattr(self.cfg, "resume_window_s", 0))
        await self.provider.connect()
        self.started = time.monotonic()
        if getattr(self.provider, "fallback_from", None):
            self.log.event("model_fallback", model=self.provider.model, configured=self.provider.fallback_from)
        if getattr(self.provider, "resumed", False):
            self.log.event("resumed")
            self.resume_armed = float(getattr(self.cfg, "resume_fallback_s", 0) or 0) > 0
            try:
                await self.provider.note(new_call_cue(str(getattr(self.log, "kind", "") or "") in ("tg-out",
                                                                                                    "app-out")))
            except Exception:
                pass
        self._spawn(self._pump())
        self._spawn(self._timer())
        if self.resume_armed:
            self._spawn(self._resume_watch())
        if ((self.cfg.idle_prompt_s > 0 or getattr(self.cfg, "closing", False)) and self.provider.mode != "ptt"
                and getattr(self.provider, "transcribes_input", False)):
            self._spawn(self._idle())
        await self.fe.event({"type": "state", "state": "live", "provider": self.provider.name,
                             "mode": self.provider.mode})

    async def audio_in(self, pcm):
        if self.closed:
            return
        ms = len(pcm) / 2.0 / self.fe.in_rate * 1000.0
        self.in_ms += ms
        level = rms(pcm)
        loud = level >= self.cfg.voice_rms
        if loud:
            self.last_voice = time.monotonic()
            self.voiced += 1
            self.voiced_ms += ms
            if self.first_voice_t is None:
                self.first_voice_t = self.last_voice
        if self.greet_pre is not None:
            self.greet_pre.append((level, ms))
        elif self.greet_until:
            await self._greet_barge(level, ms)
        if self.track is not None:
            self.track.add(pcm)
        if self.gated:
            return
        pcm = resample(pcm, self.fe.in_rate, self.provider.input_rate)
        if self.resume_armed:
            self._keep_resume(pcm)
        if not self.recovering:
            try:
                await self.provider.send_audio(pcm)
                self.sent_ms += ms
                return
            except Exception:                # the socket just dropped (the closed event is on its way): keep it
                if self.closed or not getattr(self.cfg, "reconnect_tries", 0):
                    return
        self._buffer_in(pcm)

    def _buffer_in(self, pcm):
        """His audio while the provider reconnects: the newest reconnect_buffer_s, sent once the line is back."""
        cap = int(float(getattr(self.cfg, "reconnect_buffer_s", 0)) * self.provider.input_rate * 2)
        if cap <= 0:
            return
        self.in_buf.append(pcm)
        self.in_buf_bytes += len(pcm)
        while self.in_buf_bytes > cap and self.in_buf:
            self.in_buf_bytes -= len(self.in_buf.popleft())

    def _keep_resume(self, pcm):
        """His audio while a resumed session is unproven: the newest RESUME_BUF_S, replayed if it stays mute."""
        cap = int(RESUME_BUF_S * self.provider.input_rate * 2)
        self.resume_buf.append(pcm)
        self.resume_buf_bytes += len(pcm)
        while self.resume_buf_bytes > cap and self.resume_buf:
            self.resume_buf_bytes -= len(self.resume_buf.popleft())

    def _model_alive(self):
        """Model audio or a tool call: the conversation works (a resumed one is proven; its kept audio goes)."""
        self.model_live = True
        if self.resume_armed:
            self.resume_armed = False
            self.resume_buf.clear()
            self.resume_buf_bytes = 0

    async def ptt(self, down):
        if self.closed:
            return
        if down:
            await self.fe.flush()
        else:
            self.metrics["t_ptt_up"] = time.monotonic()
        await self.provider.activity(down)

    async def text_in(self, text):
        if not self.closed:
            self.metrics["t_ptt_up"] = time.monotonic()
            self.log.event("typed", text=text[:1000])
            await self.provider.send_text(text[:1000])

    async def opening(self, text, prefetch=False):
        """Outbound call picked up: make the provider speak first (the opening line with the reason)."""
        if not self.closed:
            self.log.event("opening", text=text[:400], prefetch=True if prefetch else None)
            self.opening_t, self.opening_last = time.monotonic(), text
            await self.provider.prompt(text)

    def greet_inbound(self, pcm, via, t0=None):
        """He called (Telegram tg-in, phone app): the pick-up line (greet.Greeter: the cached clip, or the earcon) plays
        cfg.greet_delay_s after the line is up (t0), ONCE per call: a reconnect or a resume fallback never replays it.
        Local audio only: the model neither hears nor generates it, so no turn starts; his audio flows to the model
        all along. He speaks first (before it plays) → skipped; he talks over it → cut (_greet_barge). "Speaking" =
        louder than voice_rms and 3x the line's noise floor (its quietest fifth before the line plays, at most
        GREET_FLOOR_MAX), so a steady noisy line (car, street) neither skips nor cuts it. Call log greet{inbound, via clip|tone|skipped,
        first_audio_ms = line up → first audio, clip_ms}."""
        self.line_up = True
        if self.closed or self.greeted or not pcm:
            return None
        self.greeted, self.greet_pre = True, []
        return self._spawn(self._greet(pcm, via, time.monotonic() if t0 is None else t0))

    async def _greet(self, pcm, via, t0):
        delay = max(0.0, float(getattr(self.cfg, "greet_delay_s", 0) or 0) - (time.monotonic() - t0))
        if delay:
            await asyncio.sleep(delay)
        pre, self.greet_pre = self.greet_pre or [], None
        if self.closed:
            return
        now = time.monotonic()
        quiet = sorted(r for r, _ in pre)
        floor = min(GREET_FLOOR_MAX, quiet[int(0.2 * (len(quiet) - 1))] if quiet else 0.0)
        self.greet_level = max(float(self.cfg.voice_rms), 3.0 * floor)
        talk_ms = sum(ms for r, ms in pre if r >= self.greet_level)
        if talk_ms >= float(getattr(self.cfg, "greet_barge_ms", 150) or 150) or self.turn_open:
            self.log.event("greet", inbound=True, via="skipped", waited_ms=round((now - t0) * 1000))
            return
        dur = len(pcm) / 2.0 / self.fe.out_rate
        self.greet_until, self.greet_voiced_ms = now + dur, 0.0
        self.audio_end = max(self.audio_end, now) + dur
        await self.fe.play(pcm)
        ms = round((now - t0) * 1000)
        self.metrics["greet_ms"] = ms
        self.log.event("greet", first_audio_ms=ms, inbound=True, via=via, clip_ms=round(dur * 1000))

    async def _greet_barge(self, level, ms):
        """His voice over the pick-up line for cfg.greet_barge_ms: drop the rest of it (the model never spoke, so
        Gemini's own barge-in can't). Not once the model's audio flows: a flush would cut the model instead."""
        now = time.monotonic()
        if now >= self.greet_until or self.turn_open:
            self.greet_until = 0.0
            return
        if level >= self.greet_level:
            self.greet_voiced_ms += ms
        if self.greet_voiced_ms >= float(getattr(self.cfg, "greet_barge_ms", 150) or 150):
            left = self.greet_until - now
            self.greet_until = 0.0
            self.audio_end = now
            self.log.event("greet_cut", left_ms=round(left * 1000))
            await self.fe.flush()

    def open_after_pickup(self, text_fn, deck=None):
        """Outbound pick-up: hold his audio back from the model until he has said his hello (quiet for
        opening_quiet_s after it) or opening_wait_s passed without a word (opening_max_s at most), then the
        opening plays ONCE; text_fn(heard) builds it. Without the hold the model answered his pick-up "hello" itself
        and each interruption restarted the greeting. With cfg.opening_prefetch the opening (text_fn(None): hello or
        not unknown yet) is sent right at pick-up and its audio held back until the hold ends, so the model's
        ~0.6 s to first audio overlaps the hold instead of following it."""
        self.gated = self.line_up = True
        return self._spawn(self._open_after_pickup(text_fn, deck, time.monotonic(), self.voiced))

    async def _open_after_pickup(self, text_fn, deck, t0, v0):
        self.greet_t0 = t0
        prefetch = bool(getattr(self.cfg, "opening_prefetch", False)) and self.provider.mode != "ptt"
        if prefetch:
            if deck is not None:
                self.round.set_deck(deck, "opening")
                self.log.event("deck", items=len(deck["items"]), via="opening")
            self.out_held, self.held, self.held_t, self.held_done = True, [], None, False
            try:
                await self.opening(text_fn(None), prefetch=True)
            except Exception:
                self.out_held, prefetch = False, False
                if self.held:
                    await self._release_held()
        self.greet_prefetch = prefetch
        try:
            while not self.closed:
                now = time.monotonic()
                heard = self.voiced - v0 >= 3            # 30 ms of voice, not a click
                if (now - t0 >= self.cfg.opening_max_s
                        or (heard and now - self.last_voice >= self.cfg.opening_quiet_s)
                        or (not heard and now - t0 >= self.cfg.opening_wait_s)):
                    break
                await asyncio.sleep(0.02)
        finally:
            self.gated = False
        if self.closed:
            return
        heard = self.voiced - v0 >= 3
        if prefetch:
            self.log.event("pickup", heard=heard, waited_ms=round((time.monotonic() - t0) * 1000), prefetch=True,
                           ready_ms=round((self.held_t - t0) * 1000) if self.held_t else None)
            await self._release_held()
            return
        self.log.event("pickup", heard=heard, waited_ms=round((time.monotonic() - t0) * 1000))
        if deck is not None:
            self.round.set_deck(deck, "opening")
            self.log.event("deck", items=len(deck["items"]), via="opening")
        await self.opening(text_fn(heard))

    async def _release_held(self):
        """The pick-up hold ended: play the prefetched greeting (in order; chunks still arriving join the queue
        until it is empty), then let audio flow; a turn that already completed is closed out now."""
        while self.held:
            await self._play_out(self.held.pop(0))
        self.out_held = False
        if self.held_done:
            self.held_done = False
            self._turn_complete()
            await self.fe.event({"type": "turn"})

    async def _play_out(self, pcm):
        """One chunk of assistant audio (front-end rate) to him: turn bookkeeping, latency, the play-out estimate."""
        now = time.monotonic()
        if not self.turn_open:
            self.turn_open = True
            self.asked_more = self.closing_reply = False
            self._reply_started(now)
        if self.greet_t0 is not None:            # first audio after an outbound pick-up
            self.log.event("greet", first_audio_ms=round((now - self.greet_t0) * 1000),
                           prefetch=True if self.greet_prefetch else None)
            self.metrics["greet_ms"] = round((now - self.greet_t0) * 1000)
            self.greet_t0 = None
        self.turn_audio = True
        self.last_audio = now
        if self.link_down:
            self.link_spoke = True
        self.audio_end = max(self.audio_end, now) + len(pcm) / 2.0 / self.fe.out_rate
        await self.fe.play(pcm)

    def _turn_complete(self):
        self.turn_open, self.turn_end = False, time.monotonic()
        self.log.flush()
        self._turn_done()

    async def decision_opening(self, deck):
        """Outbound decision call picked up: the deck becomes the opening context (decisions.opening)."""
        self.round.set_deck(deck, "opening")
        self.log.event("deck", items=len(deck["items"]), via="opening")
        await self.opening(decisions.opening(deck))

    async def _pump(self):
        try:
            while not self.closed:
                if not await self._pump_once():
                    break
        finally:
            if not self.closed:
                self._spawn(self.close("provider ended"))

    async def _pump_once(self):
        """Provider events until its socket closes. True = it dropped and _recover brought it back (pump again)."""
        async for ev in self.provider.events():
                t = ev["type"]
                if t == "audio":
                    if not self.model_live:
                        self._model_alive()
                    pcm = resample(ev["pcm"], self.provider.output_rate, self.fe.out_rate)
                    if self.out_held:                # prefetched greeting: held until the pick-up hold ends
                        if self.held_t is None:
                            self.held_t = time.monotonic()
                        self.held.append(pcm)
                        continue
                    await self._play_out(pcm)
                elif t == "transcript":
                    self.log.text(ev["who"], ev["text"])
                    if ev["who"] == "user" and str(ev["text"]).strip():
                        self._him(str(ev["text"]))
                        self._user_active("speech")
                    elif ev["who"] == "assistant":
                        self.turn_text = (self.turn_text + str(ev["text"]))[-2000:]
                        if (self.answered and not self.turn_open and not self.turn_audio
                                and time.monotonic() - self.turn_end < 2.0):
                            self._classify_turn()    # the transcript's tail came after turn_complete
                    await self.fe.event({"type": "transcript", "who": ev["who"], "text": ev["text"]})
                elif t == "interrupted":
                    self.turn_open, self.turn_end = False, time.monotonic()
                    self.log.flush()
                    self.log.event("interrupted")
                    self.audio_end = time.monotonic()
                    await self.fe.flush()
                    self._user_active("barge-in")
                elif t == "turn_complete":
                    if self.out_held:
                        self.held_done = True        # closed out when the held greeting is played (_release_held)
                        continue
                    self._turn_complete()
                    await self.fe.event({"type": "turn"})
                elif t == "tool_call":
                    if not self.model_live:
                        self._model_alive()
                    self.last_tool = (ev.get("name") or "?", time.monotonic())
                    if ev.get("name") != "end_call":
                        self.answered = False
                    self.log.flush()
                    self._spawn(self._tool(ev))
                elif t == "tool_cancel":
                    self.log.flush()
                    self.log.event("tool_cancel", ids=[str(i) for i in ev.get("ids") or []])
                elif t == "go_away" and hasattr(self.provider, "reconnect"):
                    self.log.event("reconnect")
                    self.recovering = True           # planned switch: the same resumption path, no cue, no prompt
                    try:
                        await self.provider.reconnect()
                    except Exception as e:
                        self.log.event("error", message="goAway reconnect: %s" % (str(e) or type(e).__name__)[:200])
                        self.recovering = False
                        return await self._recover("go_away")
                    self.recovering = False
                    self.gen += 1
                    await self._flush_in()
                elif t == "error":
                    if ev.get("conn") is not None and ev["conn"] != getattr(self.provider, "conn", ev["conn"]):
                        continue
                    self.log.event("error", message=str(ev.get("message", ""))[:300])
                    await self.fe.event({"type": "error", "message": ev.get("message", "")})
                elif t == "closed":
                    if self.closed:
                        return False
                    if ev.get("conn") is not None and ev["conn"] != getattr(self.provider, "conn", ev["conn"]):
                        continue                     # an older socket's close (goAway switch): the new one is up
                    self.log.event("error", message="provider: %s" % ev.get("reason", "closed"))
                    return await self._recover(ev.get("reason") or "closed")
        return False

    async def _recover(self, why):
        """The provider socket dropped mid-call (network blip, a 1011 from Gemini, our uplink): reconnect with the
        session's resumption handle (the conversation continues) up to cfg.reconnect_tries times, each capped at
        reconnect_timeout_s, while the thinking cue tells him the line is still there and his audio is buffered
        (newest reconnect_buffer_s). Back → his buffered audio goes up, the model says it lost him for a moment.
        Not while hanging up. Call log reconnect{why: dropped, ok, tries, ms, resumed}; usage voice.reconnect."""
        tries = int(getattr(self.cfg, "reconnect_tries", 0) or 0)
        if (tries <= 0 or not hasattr(self.provider, "reconnect") or self.closed or self.idle_stage >= 2
                or (self.ending is not None and not self.ending.done())
                or self.metrics.get("reconnects", 0) >= MAX_RECOVERIES):
            return False
        t0 = time.monotonic()
        self.recovering = True
        if getattr(self.cfg, "thinking_cue", False):
            self.waiting_on.add("reconnect")
            self._spawn(self._thinking("reconnect"))
        ok, n = False, 0
        try:
            for n in range(1, tries + 1):
                if self.closed:
                    break
                try:
                    await asyncio.wait_for(self.provider.reconnect(), float(self.cfg.reconnect_timeout_s))
                    ok = True
                    break
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if n < tries and not self.closed:
                        await asyncio.sleep(min(2.0, 0.5 * n))
        finally:
            self.waiting_on.discard("reconnect")
            self.recovering = False
        ms = round((time.monotonic() - t0) * 1000)
        resumed = bool(getattr(self.provider, "resumed", False))
        self.log.event("reconnect", why="dropped", cause=str(why)[:80], ok=ok, tries=n, ms=ms,
                       resumed=resumed if ok else None)
        self._usage("voice.reconnect", "ok" if ok else "failed", ms=ms, attrs={"tries": n, "resumed": resumed if ok
                                                                                else None, "cause": _cause(why)})
        if not ok or self.closed:
            self.in_buf.clear()
            self.in_buf_bytes = 0
            return False
        self.gen += 1
        self.metrics["reconnects"] = self.metrics.get("reconnects", 0) + 1
        try:
            await self.provider.prompt(RECONNECTED if resumed else RECONNECTED_FRESH)
            await self._flush_in()
        except Exception:
            return False
        return True

    async def _resume_watch(self):
        """A resumed conversation that stays mute. Gemini sometimes resumes a session that never speaks again (seen
        live after a call the model ended itself: his speech only "interrupted", no audio, no tool call). No model
        audio or tool call within cfg.resume_fallback_s of the opening prompt or of his last words → reopen a fresh
        session in the same call (_resume_fallback). Ends at the model's first audio or tool call; once per call."""
        wait = float(getattr(self.cfg, "resume_fallback_s", 0) or 0)
        while not self.closed and self.resume_armed:
            await asyncio.sleep(0.05)
            if not self.resume_armed or self.recovering or self.link_down:
                continue
            trig = max(self.opening_t, self.him_t)   # his last transcribed words / the opening prompt
            if trig <= 0 and self.voiced >= 3:       # no transcript at all: his last loud frame
                trig = self.last_voice
            if trig > 0 and time.monotonic() - trig >= wait:
                await self._resume_fallback("opening" if 0 < self.opening_t >= self.him_t else "him", trig)
                return

    async def _resume_fallback(self, trigger, trig):
        """Drop the resumption handle and reconnect fresh (same call, same front end): an outbound call gets its
        opening again; otherwise his kept audio (newest RESUME_BUF_S) and what came in meanwhile go to the new
        session, so it hears what he said. Call log resume_fallback{trigger, waited_ms, ok, ms, replay_ms};
        usage voice.resume_fallback. A failed reopen goes the dropped-socket way (_recover, then the call ends)."""
        self.resume_armed = False
        replay, self.resume_buf, self.resume_buf_bytes = list(self.resume_buf), deque(), 0
        t0 = time.monotonic()
        RESUME.clear()
        self.provider.resume_handle = None
        self.recovering = True
        ok, err = False, None
        try:
            await asyncio.wait_for(self.provider.reconnect(), float(getattr(self.cfg, "reconnect_timeout_s", 0)
                                                                     or 15.0))
            ok = not getattr(self.provider, "resumed", False)
        except asyncio.CancelledError:
            self.recovering = False
            raise
        except Exception as e:
            err = (str(e) or type(e).__name__)[:160]
        if not ok:
            self.recovering = False
        ms = round((time.monotonic() - t0) * 1000)
        resend = trigger == "opening" or self.opening_last is not None
        replay_bytes = 0 if resend else sum(len(x) for x in replay)
        self.log.event("resume_fallback", trigger=trigger, waited_ms=round((t0 - trig) * 1000), ok=ok, ms=ms,
                       replay_ms=round(replay_bytes / 2.0 / self.provider.input_rate * 1000) if replay_bytes else None,
                       error=err)
        self._usage("voice.resume_fallback", "ok" if ok else "failed", ms=ms, attrs={"trigger": trigger},
                    actor="system")
        if self.closed:
            self.recovering = False
            return
        if not ok:
            self.in_buf.clear()
            self.in_buf_bytes = 0
            q = getattr(self.provider, "_q", None)       # the pump reads this queue: a dropped socket → _recover
            if q is not None:
                await q.put({"type": "closed", "reason": "resume fallback failed", "conn": self.provider.conn})
            return
        self.gen += 1
        self.metrics["resume_fallback"] = 1
        try:                                           # still "recovering": live audio queues behind the replay
            if resend:
                await self.provider.prompt(self.opening_last)
                self.in_buf.clear()                    # his words meanwhile: the new greeting asks again
                self.in_buf_bytes = 0
            else:
                for pcm in replay:
                    if self.closed:
                        return
                    await self.provider.send_audio(pcm)
                await self._flush_in()
        except Exception as e:
            self.log.event("error", message="resume fallback: %s" % (str(e) or type(e).__name__)[:160])
        finally:
            self.recovering = False

    async def link_lost(self):
        """phonegw: the app's connection dropped; it holds the call for its grace window (the app reconnects on any
        route and resumes the same call). Silence timers pause, late answers wait (link_answer_wait_s), audio the
        model sends meanwhile is noted as missed. Call log link{lost}."""
        if self.closed or self.link_down or not getattr(self.cfg, "link_resume", True):
            return
        self.link_down, self.link_t0, self.link_spoke, self.link_tg = True, time.monotonic(), False, False
        self.log.event("link", state="lost")

    async def link_back(self, gap_ms=None):
        """The app is back on the same call: the silence clock starts over and the model says it lost him for a
        moment (and repeats what he missed). Call log link{back, gap_ms, n}; usage voice.link_resume (ms = the gap)."""
        if self.closed or not self.link_down:
            return
        now = time.monotonic()
        try:
            gap = int(gap_ms) if gap_ms is not None else round((now - self.link_t0) * 1000)
        except (TypeError, ValueError):
            gap = round((now - self.link_t0) * 1000)
        gap = max(0, min(gap, 600000))
        missed, to_tg = self.link_spoke, self.link_tg
        self.link_down = False
        self.last_activity = now
        n = self.metrics["resumes"] = self.metrics.get("resumes", 0) + 1
        self.metrics["gap_ms"] = self.metrics.get("gap_ms", 0) + gap
        self.log.event("link", state="back", gap_ms=gap, n=n, missed=missed or None, telegram=to_tg or None)
        self._usage("voice.link_resume", "ok", ms=gap, attrs={"n": n, "missed": missed, "telegram": to_tg}, actor="system")
        if self.idle_stage >= 2 or (self.ending is not None and not self.ending.done()):
            return                               # the goodbye is under way: no new line
        if self.idle_stage == 1:                 # "anything else?" may have gone into the gap: the clock starts over
            self.idle_stage = 0
        self.cue_t, self.closing_reply = now, False
        await self._ready()
        if self.closed:
            return
        try:
            await self.provider.prompt(link_back_text(gap, missed, to_tg))
        except Exception:
            pass

    async def _flush_in(self):
        """His audio buffered while reconnecting → the provider, oldest first."""
        while self.in_buf and not self.closed:
            pcm = self.in_buf.popleft()
            self.in_buf_bytes -= len(pcm)
            await self.provider.send_audio(pcm)
        self.in_buf_bytes = max(0, self.in_buf_bytes)

    async def _result(self, cid, name, res, scheduling=None):
        """A tool result to the provider, after a reconnect finished (GeminiLive turns a result for a call from an
        earlier connection into an [assistant result] note)."""
        await self._ready()
        if scheduling:
            await self.provider.tool_result(cid, name, res, scheduling=scheduling)
        else:
            await self.provider.tool_result(cid, name, res)

    async def _ready(self, max_s=None):
        """Wait (bounded) while the provider reconnects, so a result or a late answer isn't sent into a dead socket."""
        end = time.monotonic() + (max_s if max_s is not None else
                                  float(getattr(self.cfg, "reconnect_timeout_s", 0) or 0) *
                                  max(1, int(getattr(self.cfg, "reconnect_tries", 0) or 0)) + 3)
        while self.recovering and not self.closed and time.monotonic() < end:
            await asyncio.sleep(0.05)

    def _usage(self, feature, outcome, ms=None, attrs=None, actor="owner"):
        """One usage event (ids, flags, numbers only); never raises."""
        try:
            kind = str(getattr(self.log, "kind", "") or "")
            fn = self._ulog or closing.usage_log()
            fn(feature, "invoke", channel="app" if kind.startswith("app") else "voice", actor=actor, ms=ms,
               outcome=outcome, attrs=dict({k: v for k, v in (attrs or {}).items() if v is not None},
                                           call_kind=kind or None))
        except Exception:
            pass

    def _reply_started(self, now):
        """First audio of an assistant turn: when he spoke since the last turn, log how long after his end of
        speech it came (relay side: Gemini's VAD + model + hand-off; the Telegram leg is not included)."""
        if self.last_voice <= max(self.turn_end, 0.0) or self.last_voice <= 0:
            return
        gap = now - self.last_voice
        if 0 <= gap <= 20:
            name, t = self.last_tool
            self.log.event("reply", after_ms=round(gap * 1000), tool=name if t >= self.last_voice else None)
        self.turn_end = now                  # one measurement per thing he said

    async def _timer(self):
        await asyncio.sleep(self.limit_s)
        await self.close("time limit")

    async def _status(self):
        await self.fe.event({"type": "status", "working": len(self.working)})

    async def _tool(self, ev):
        cid, name, args = ev.get("id", ""), ev.get("name", ""), ev.get("args") or {}
        counted = name != "end_call"
        if counted:
            self.asks_inflight += 1
        try:
            await self._tool_inner(cid, name, args)
        except asyncio.CancelledError:
            raise
        except Exception as e:           # never leave Gemini waiting on a call that died here
            self.log.event("error", message="tool %s: %s" % (name, type(e).__name__))
            if name == "ask_assistant" or name in fastlane.NAMES:
                task = str(args.get("task") or "").strip()[:2000] if name == "ask_assistant" \
                    else fastlane.fallback_task(name, args)
                if task and not any(it["task"] == task for it in self.items):
                    self.items.append({"id": None, "task": task, "status": "error",
                                       "action": recap.is_action(task, args.get("action"))})
            try:
                await self._result(cid, name, {"error": "the hand-off failed; suggest Telegram"})
            except Exception:
                pass
        finally:
            if counted:
                self.asks_inflight -= 1
                self.last_activity = time.monotonic()

    async def _tool_inner(self, cid, name, args):
        if name == "end_call" and self.cfg.end_call:
            await self._end_call(cid, args)
            return
        if self.fast is not None and name in fastlane.NAMES:
            await self._fast(cid, name, args)
            return
        if self.track is not None and name in track.NAMES:
            await self._track(cid, name)
            return
        if self.cfg.decisions and name in decisions.NAMES:
            await self._decision(cid, name, args)
            return
        if self.phone is not None and name in phoneopen.NAMES:
            await self._phone_open(cid, name, args)
            return
        if name != "ask_assistant":
            self.log.tool(name, {} if name in phoneopen.NAMES else args, cid, error="unknown tool")
            await self._result(cid, name, {"error": "unknown tool"})
            return
        task = str(args.get("task") or "").strip()[:2000]
        urgency = "later" if args.get("urgency") == "later" else "now"
        if not task:
            self.log.tool(name, args, cid, error="empty task")
            await self._result(cid, name, {"error": "empty task"})
            return
        await self._ask(cid, name, args, task, urgency, recap.is_action(task, args.get("action")))

    async def _fast(self, cid, name, args):
        """A quick read-only tool answered here (~1 s); any failure → the same question as an ask_assistant."""
        t0 = time.monotonic()
        try:
            text, err = await asyncio.wait_for(self.fast.call(name, args), fastlane.MAX_TIMEOUT_S + 0.2), None
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            text, err = None, "timeout"
        except Exception as e:
            text, err = None, (str(e) or type(e).__name__)[:120]
        ms = round((time.monotonic() - t0) * 1000)
        self.log.tool(name, args, cid, lane="fast", ms=ms, error=err)
        if err is None:
            res = {"answer": text}
            self.metrics["fast_ms"] = ms
            self.log.result(None, res)
            await self._result(cid, name, res)
            return
        task = fastlane.fallback_task(name, args)
        await self._ask(cid, name, {"task": task, "urgency": "now"}, task, "now", False, via=name)

    async def _track(self, cid, name):
        """identify_track: match the buffer now; no sure match (or too little audio) → "listening", ~10 s more
        audio, one retry whose answer comes back late. Never handed to the main session."""
        tt, t0 = self.track, time.monotonic()
        if tt.busy:
            self.log.tool(name, {}, cid, lane="track", error="busy")
            await self._result(cid, name, {"status": "working", "say": track.BUSY_SAY})
            return
        tt.busy = True
        try:
            first = None
            if tt.buf.seconds() >= self.cfg.track_min_s:
                first = await tt.attempt()
                if first[0] or first[2].get("status") == "error":
                    ms = round((time.monotonic() - t0) * 1000)
                    self.metrics["track_ms"] = ms
                    self.log.tool(name, {}, cid, lane="track", ms=ms,
                                  error=None if first[0] else str(first[2].get("error") or "error")[:120])
                    res = {"answer": first[1]}
                    self.log.result(None, res)
                    await self._result(cid, name, res)
                    return
            res = {"status": "listening", "say": track.LISTEN_SAY if first else track.LISTEN_SAY_FIRST}
            self.log.tool(name, {}, cid, lane="track", ms=round((time.monotonic() - t0) * 1000),
                          error="retry: %s" % (first[2].get("status") if first else "short buffer"))
            self.log.result(None, res)
            await self._result(cid, name, res)
            await tt.more()
            if self.closed:
                return
            hit, text, out = await tt.attempt()
            ms = round((time.monotonic() - t0) * 1000)
            self.metrics["track_ms"] = ms
            self.log.event("track_retry", ms=ms, status=out.get("status"), hit=hit, text=text[:300])
            if not self.closed:
                await self.provider.inject("identify_track: " + text)
        finally:
            tt.busy = False

    async def _decision(self, cid, name, args):
        """decision_deck / record_pick: local, no hand-off. A pick is only noted (never applied, never an approval)."""
        t0 = time.monotonic()
        try:
            await self.round.ensure(self.log)
        except Exception as e:
            err = "deck unavailable: %s" % (str(e) or type(e).__name__)[:120]
            self.log.tool(name, args, cid, lane="local", error=err)
            res = {"error": "the decision list isn't available right now; suggest Telegram"}
            self.log.result(None, res)
            await self._result(cid, name, res)
            return
        if name == "decision_deck":
            res = decisions.deck_answer(self.round.deck)
            self.log.tool(name, args, cid, lane="local", ms=round((time.monotonic() - t0) * 1000))
        else:
            it, res = self.round.record(args.get("item"), args.get("choice"))
            self.log.tool(name, args, cid, lane="local", error=res.get("error"),
                          pick=res["recorded"] if it is not None else None)
        self.log.result(None, res)
        await self._result(cid, name, res)

    async def _phone_open(self, cid, name, args):
        """navigate / open_on_phone: `phone open` here (approved as a class: no confirm card, no hand-off). The call log
        gets kind/mode/target class/route/ms only, never the place, URL or app name; the result line only its status."""
        key = "open:%s" % cid
        if getattr(self.cfg, "thinking_cue", False):
            self.waiting_on.add(key)
            self._spawn(self._thinking(key))
        try:
            res, meta = await self.phone.call(name, args)
        finally:
            self.waiting_on.discard(key)
        self.metrics["phone_open_ms"] = meta.get("ms")
        self.log.tool(name, meta.get("log_args") or {}, cid, lane="phone", ms=meta.get("ms"), route=meta.get("route"),
                      error=meta.get("error"))
        self.log.result(None, {"status": res.get("status")})
        await self._result(cid, name, res)

    async def _ask(self, cid, name, args, task, urgency, action, via=None):
        """Hand a task to the main session; the tool result goes back under the caller's id and name."""
        item = {"id": None, "task": task, "status": "failed", "action": action, "via": via, "urgency": urgency}
        self.items.append(item)
        log_name, extra = (name, {}) if via is None else ("ask_assistant", {"fallback_from": via})
        now = time.monotonic()
        while self.asks and self.asks[0] < now - 60:
            self.asks.popleft()
        if len(self.asks) >= self.cfg.max_asks_per_min:
            item["status"] = "dropped"
            self.log.tool(log_name, args, cid, error="rate limit (%d/min)" % self.cfg.max_asks_per_min, **extra)
            await self._result(cid, name, {"answer": "That's a lot at once; give me a moment."})
            return
        self.asks.append(now)
        t0 = time.monotonic()
        sent = ACTION_TAG + task if action and not task.startswith(ACTION_TAG) else task
        p = await self.handoff.submit(sent, urgency, deliver_late=self._deliver_late, owner=self)
        item["id"] = p.id
        self.metrics.update(route=p.route, handoff_submit_ms=round((time.monotonic() - t0) * 1000, 1))
        self.log.tool(log_name, args, cid, handoff=p.id, route=p.route, **extra)
        if p.route == "failed":
            res = {"answer": "I can't reach your assistant right now. Please send it on Telegram."}
        elif p.route == "inbox":
            self.working.add(p.id)
            item["status"] = "queued"
            res = {"status": "queued", "say": "Your assistant will pick this up in a few minutes; "
                                              "the answer comes on Telegram."}
        else:
            self.working.add(p.id)
            item["status"] = "working"
            await self._status()
            cue_until = None
            if urgency == "now" and getattr(self.cfg, "thinking_cue", False):
                self.waiting_on.add(p.id)
                cue_until = t0 + max(float(self.cfg.sync_wait_s), float(getattr(self.cfg, "slow_cue_max_s", 0) or 0))
                self._spawn(self._thinking(p.id, until=cue_until))
            slow = False
            try:
                ans = await self.handoff.wait(p, self.cfg.sync_wait_s if urgency == "now" else 0)
            finally:
                # a slow "now" answer keeps the (sparser) cue going until it arrives (_deliver_late) or cue_until
                if not (urgency == "now" and cue_until is not None and p.id in self.working
                        and not getattr(p, "parts", 0) and not self.closed):
                    self.waiting_on.discard(p.id)
            if ans is not None and getattr(p, "parts", 0):          # voice-reply --more: the rest follows
                self.waiting_on.discard(p.id)
                item.update(answer=ans)
                self.metrics["answer_ms"] = round((time.monotonic() - t0) * 1000, 1)
                self.log.event("partial", handoff=p.id, ms=self.metrics["answer_ms"])
                res = {"answer": ans, "more": MORE_SAY}
            elif ans is not None:
                self.waiting_on.discard(p.id)
                self.working.discard(p.id)
                item.update(status="answered", answer=ans)
                self.metrics["answer_ms"] = round((time.monotonic() - t0) * 1000, 1)
                res = {"answer": ans}
            elif urgency == "now":
                slow = True
                item["slow"] = True
                self.metrics["slow"] = self.metrics.get("slow", 0) + 1
                res = {"status": "working", "say": SLOW_SAY}
            else:
                res = {"status": "working", "say": WORKING_SAY}
            item["t0"] = t0
            self.log.event("handoff_ms", handoff=p.id, ms=round((time.monotonic() - t0) * 1000),
                           outcome="partial" if ans is not None and getattr(p, "parts", 0) else
                           "answer" if ans is not None else "slow" if slow else "background")
            if slow:
                self._usage("voice.handoff_slow", "slow", ms=round((time.monotonic() - t0) * 1000),
                            attrs={"route": p.route, "action": bool(action)})
        self.log.result(p.id, res)
        await self._result(cid, name, res)
        if "t_ptt_up" in self.metrics and "answer_ms" in self.metrics:
            await self.fe.event({"type": "metric", "tool_to_answer_ms": self.metrics["answer_ms"],
                                 "route": p.route,
                                 "speech_end_to_result_ms": round((time.monotonic() - self.metrics["t_ptt_up"])
                                                                  * 1000, 1)})
        await self._status()

    async def _thinking(self, pid, until=None):
        """While he waits on a 'now' hand-off (or the line reconnects) and nothing is playing or being said: a soft
        cue now and then, so the wait doesn't sound like a dropped line. Stops at the answer and at `until`; after
        the sync wait (a slow answer) it plays half as often."""
        pcm = cue(self.fe.out_rate)
        dur = len(pcm) / 2.0 / self.fe.out_rate
        last, t0 = 0.0, time.monotonic()
        slow_after = t0 + float(self.cfg.sync_wait_s)
        while not self.closed and pid in self.waiting_on:
            now = time.monotonic()
            if until is not None and now >= until:
                self.waiting_on.discard(pid)
                break
            every = self.cfg.thinking_every_s * (2 if now >= slow_after else 1)
            quiet = now - max(self.audio_end, self.last_voice, last, t0)
            if not self.turn_open and quiet >= (self.cfg.thinking_after_s if not last else every):
                await self.fe.play(pcm)
                last = now + dur
                self.audio_end = max(self.audio_end, last)
                self.metrics["cues"] = self.metrics.get("cues", 0) + 1
            await asyncio.sleep(0.1)

    # --- ending the call from our side -------------------------------------------------------------------------

    def _user_active(self, why):
        """He spoke: the silence clock restarts; speech well after a hang-up started (a barge-in) cancels it."""
        now = time.monotonic()
        self.last_activity, self.idle_stage, self.turn_text = now, 0, ""
        self.answered = False                # the 20 s clock waits for the answer to what he just said
        self.closing_reply, self.replies = False, 0
        if self.ending is not None and not self.ending.done() and not self.closed \
                and (why == "barge-in" or now - self.ending_t >= 1.5):
            self.ending.cancel()
            self.ending = None
            self.log.event("hangup_cancelled", why=why)
        if self.ending is None:
            self.end_why = None              # he carries on: whatever ending was planned is off

    def _him(self, text):
        """A fragment of his speech (Gemini's input transcription). One utterance runs until the assistant speaks or
        calls a tool (or he pauses > 4 s); closing.classify tells a plain thanks / goodbye / no from a request."""
        now = time.monotonic()
        if not self.him_turn or max(self.last_audio, self.last_tool[1]) > self.him_t or now - self.him_t > 4.0:
            self.him_turn, self.him_t0, self.nudged = "", now, False
            # answering an "anything else?": the last turn asked it, the relay asked it, or he talks over it
            self.closing_asked = self.asked_more or self.idle_stage == 1 or closing.asks_more(self.turn_text)
        self.him_turn = (self.him_turn + " " + text)[-400:]
        self.him_t = now
        self.closing = closing.classify(self.him_turn) if getattr(self.cfg, "closing", False) else None

    def _turn_done(self):
        """An assistant turn with audio ended: a completed answer (the silence clock starts), an "anything else?"
        (stage 1) or a goodbye after his thanks / no / bye (stage 2: end_call expected, else the relay hangs up)."""
        if not self.turn_audio:
            return
        self.turn_audio = False
        self.answered = True
        self.replies += 1
        self._classify_turn()

    def _classify_turn(self):
        if not getattr(self.cfg, "closing", False) or self.idle_stage >= 2 or self.closed:
            return
        now = time.monotonic()
        if self.closing and closing.says_bye(self.turn_text) and (self.closing == "bye" or not self.pending_now()):
            self.idle_stage, self.last_activity = 2, now
            self.end_why = self.end_why or WHY_BY_CLASS.get(self.closing, "end_call")
            self.log.event("idle", stage="said_bye", why=self.end_why)
        elif not self.asked_more and closing.asks_more(self.turn_text):
            self.asked_more = True
            if self.idle_stage != 1:
                kind = "closing_silence" if self.closing else "asked_silence"
                self._stage1(kind, self.cfg.closing_bye_s if self.closing else self.cfg.idle_bye_s, now)
                self.log.event("idle", stage="asked", why=kind)
            self.last_activity = now
        elif (self.closing and self.closing_asked and self.idle_stage == 0 and self.replies == 1
              and max(self.last_tool[1], self.cue_t) < self.him_t0):
            self.closing_reply = True        # his closing answer to "anything else?" got a reply without a goodbye

    def _stage1(self, why, wait, now):
        if self.idle_stage != 1:
            self.end_flags["asked"] += 1
        self.idle_stage, self.last_activity = 1, now
        self.stage_why, self.bye_wait = why, float(wait)

    def pending_now(self):
        """An answer he waits for in this call is not in yet (a running tool call or a 'now' hand-off still working)."""
        return self.asks_inflight > 0 or any(it.get("urgency") == "now" and it.get("status") == "working"
                                             for it in self.items)

    async def _end_call(self, cid, args):
        reason = " ".join(str(args.get("reason") or "").split())[:200]
        if self.ending is not None and not self.ending.done():
            await self._result(cid, "end_call", {"status": "already ending"}, scheduling="SILENT")
            return
        if self.end_why is None:
            self.end_why = WHY_BY_CLASS.get(self.closing, "end_call")
        pending = self.pending_now()
        say_rest = pending and not (self.told_rest or "telegram" in self.turn_text.lower())
        self.told_rest = self.told_rest or pending
        self.log.tool("end_call", {"reason": reason}, cid, lane="local", pending=pending)
        res = {"status": "ending", "say": REST_ON_TELEGRAM} if say_rest else {"status": "ending"}
        self.log.result(None, res)
        self._start_hangup("end_call", expect_speech=say_rest)
        if say_rest:
            await self._result(cid, "end_call", res)
        else:
            await self._result(cid, "end_call", res, scheduling="SILENT")

    def _start_hangup(self, why, expect_speech=False):
        self.ending_t = time.monotonic()
        self.ending = self._spawn(self._hangup_after_audio(why, expect_speech))

    async def _hangup_after_audio(self, why, expect_speech=False):
        """Wait until the goodbye has been played out (no new audio for 0.6 s, the estimate and the front end
        drained; at most ~15 s), then hangup_tail_s more, then leave the call: end reason assistant_hangup."""
        t0 = time.monotonic()
        if expect_speech:                    # the Telegram line is still to come: wait for it to start (max 6 s)
            while time.monotonic() - t0 < 6 and self.last_audio < t0:
                await asyncio.sleep(0.05)
        while time.monotonic() - t0 < 15:
            now = time.monotonic()
            if now >= self.audio_end and now - self.last_audio >= 0.6:
                break
            await asyncio.sleep(0.05)
        try:
            await asyncio.wait_for(self.fe.drained(), 5)
        except asyncio.TimeoutError:
            pass
        await asyncio.sleep(self.cfg.hangup_tail_s)
        self.log.event("hangup", by="assistant", why=why)
        await self.close("assistant_hangup", by="silence" if why == "silence" else "assistant")

    async def _idle(self):
        """The closing flow and the silence fallback (open-mic calls with transcripts). Nothing happens while a tool
        runs, the assistant is speaking or a hang-up is under way:
        - he said only thanks / done / ok (closing.classify) and the model stayed silent closing_nudge_s → [call]
          nudge: ask "anything else?" (stage 1, closing_bye_s); after a goodbye, or "no" / thanks to "anything
          else?" → say goodbye and end_call (stage 2);
        - his thanks / no / bye answered an "anything else?" and the model replied without a goodbye or a question
          (_classify_turn: closing_reply) → closing_bye_s of quiet → say goodbye and end_call (stage 2), the short
          ending; any other words are a request: the plain timers below;
        - an answer completed and he has been silent idle_prompt_s (idle_unanswered_s when none did) → ask once
          "anything else?" (stage 1, idle_bye_s); the model asking it itself is stage 1 as well (_classify_turn);
        - stage 1 and silent bye_wait → goodbye + end_call (stage 2);
        - an answer he waits for is pending: no ending; only idle_pending_max_s of silence → "I'll send the rest on
          Telegram" + goodbye;
        - stage 2 and no end_call within bye_fallback_s after the goodbye has played → the relay hangs up itself."""
        cfg = self.cfg
        ask_s, bye_s = float(cfg.idle_prompt_s), float(cfg.idle_bye_s)
        timers = [x for x in (ask_s, bye_s, cfg.closing_nudge_s, cfg.closing_bye_s, cfg.bye_fallback_s) if x > 0]
        tick = max(0.01, min(0.25, min(timers) / 6 if timers else 0.25))
        while not self.closed:
            await asyncio.sleep(tick)
            if ((self.ending is not None and not self.ending.done()) or self.asks_inflight or self.turn_open
                    or self.recovering or self.out_held or self.link_down):
                continue
            now = time.monotonic()
            if now < self.audio_end:
                continue
            quiet = now - max(self.last_activity, self.audio_end, self.last_audio)
            if self.idle_stage == 2:
                if quiet >= max(0.05, float(cfg.bye_fallback_s)):
                    self.idle_stage = 3
                    self.end_flags["fallback"] = True
                    self.log.event("idle", stage="hangup", why=self.end_why, quiet_s=round(quiet))
                    self._start_hangup("silence")
                    return
                continue
            if self.idle_stage > 2:
                continue
            pending = self.pending_now()
            if self._nudge_due(now):
                await self._nudge(now, pending)
                continue
            if pending:
                if cfg.idle_pending_max_s > 0 and quiet >= cfg.idle_pending_max_s:
                    await self._say_bye(now, "pending_timeout", True, quiet)
                continue
            if self.idle_stage == 0:
                if self._closing_reply_due(quiet):
                    await self._say_bye(now, "closing_silence", False, quiet, prompt=closing.CLOSING_BYE,
                                        stage="closing_answer", cls=self.closing)
                    continue
                wait = ask_s if self.answered else float(cfg.idle_unanswered_s)
                if ask_s > 0 and wait > 0 and quiet >= wait:
                    self._stage1("idle_silence", bye_s, now)
                    self.log.event("idle", stage="ask", quiet_s=round(quiet))
                    await self.provider.prompt(IDLE_ASK)
            elif self.idle_stage == 1 and self.bye_wait > 0 and quiet >= self.bye_wait:
                await self._say_bye(now, self.stage_why or "idle_silence", False, quiet)

    def _closing_reply_due(self, quiet):
        """His thanks / no / bye answered an "anything else?" (the model's or the relay's), the model's reply to it said
        no goodbye, asked nothing and called no tool, and closing_bye_s of quiet followed: the short ending, as after
        his thanks. A real request (any other words) never gets here: closing is None for it."""
        wait = float(self.cfg.closing_bye_s)
        return (self.closing_reply and wait > 0 and quiet >= wait and self.closing is not None and self.answered
                and max(self.last_tool[1], self.cue_t) < self.him_t0
                and not self.turn_text.rstrip().endswith("?"))

    def _nudge_due(self, now):
        """He said only closing words, the model has neither spoken nor called a tool since, and he has been quiet
        for closing_nudge_s."""
        wait = float(self.cfg.closing_nudge_s)
        if not (getattr(self.cfg, "closing", False) and self.closing is not None and not self.nudged and wait > 0
                and max(self.last_audio, self.last_tool[1]) < self.him_t0):
            return False
        heard = self.him_t if now - self.him_t >= 2 * wait else max(self.last_voice, self.him_t)   # noise: 2x at most
        return now - heard >= wait

    async def _nudge(self, now, pending):
        self.nudged = True
        cls = self.closing
        if cls == "bye" or (cls in ("no", "thanks") and self.closing_asked):   # thanks to "anything else?" = no
            self.end_flags["nudged"] += 1
            await self._say_bye(now, WHY_BY_CLASS[cls], pending, None, stage="nudge_bye",
                                prompt=closing.CLOSING_BYE_PENDING if pending else closing.CLOSING_BYE)
        elif not pending:                        # an answer is still coming: his "ok, thanks" needs no reply
            self.end_flags["nudged"] += 1
            self._stage1("closing_silence", self.cfg.closing_bye_s, now)
            self.log.event("idle", stage="nudge", why=cls)
            await self.provider.prompt(closing.CLOSING_ASK)

    async def _say_bye(self, now, why, pending, quiet, prompt=None, stage="bye", cls=None):
        self.idle_stage, self.last_activity = 2, now
        self.end_why = why
        self.told_rest = self.told_rest or pending
        self.log.event("idle", stage=stage, why=why, quiet_s=round(quiet) if quiet is not None else None,
                       pending=pending or None, cls=cls)
        await self.provider.prompt(prompt or (IDLE_BYE_PENDING if pending else IDLE_BYE))

    async def _deliver_late(self, pid, text, more=False):
        if self.closed:
            return False
        await self._ready()                      # the line is reconnecting: wait for it rather than lose the answer
        if self.link_down:                       # the phone's line is down: wait for it (bounded), else Telegram
            end = time.monotonic() + float(getattr(self.cfg, "link_answer_wait_s", 0) or 0)
            while self.link_down and not self.closed and time.monotonic() < end:
                await asyncio.sleep(0.05)
            if self.link_down and not self.closed:
                self.link_tg = True
                self.working.discard(pid)
                self.waiting_on.discard(pid)
                for it in self.items:
                    if it.get("id") == pid and not more:
                        it.update(status="answered", answer=str(text)[:600])
                self.log.event("late", handoff=pid, delivered="telegram", why="line down")
                return False                     # voice-reply: not delivered -> the answer goes on Telegram
        if self.closed:
            return False
        self.waiting_on.discard(pid)             # a slow answer's cue stops
        if self.idle_stage < 2:                  # a late answer is a new answer: the silence clock starts over
            self.idle_stage, self.last_activity = 0, time.monotonic()
        self.answered = self.closing_reply = False
        self.cue_t = time.monotonic()
        await self.provider.inject(text)
        t0 = next((it.get("t0") for it in self.items if it.get("id") == pid), None)
        self.log.event("late", handoff=pid, delivered="live", text=str(text)[:600],
                       ms=round((time.monotonic() - t0) * 1000) if t0 else None, **({"more": True} if more else {}))
        if more:
            return True
        self.working.discard(pid)
        for it in self.items:
            if it.get("id") == pid:
                it.update(status="answered", answer=str(text)[:600])
        await self._status()
        return True

    async def close(self, reason="closed", by=None):
        if self.closed:
            return
        self.closed = True
        self.resume_armed = False
        self.resume_buf.clear()
        if self.track is not None:
            self.track.clear()               # the call's audio never outlives the call
        why = closing.end_why(reason, by or ended_by(reason), self.end_why)
        self._keep_handle(why)
        if self.in_ms > 0 or self.line_up:
            # his audio: did it arrive (ms), was there voice in it, did it go to the model; no content
            self.log.event("audio_in", ms=round(self.in_ms), voiced_ms=round(self.voiced_ms),
                           sent_ms=round(self.sent_ms),
                           first_voice_ms=round((self.first_voice_t - self.started) * 1000)
                           if self.first_voice_t is not None and self.started is not None else None)
        self.log.end(reason, by, why=why)
        self._usage_end(reason, by, why)
        self.handoff.detach(self)
        if self.round.picks:
            self._decisions_note(reason)
        if self.items:
            ended = time.time()
            if self.cfg.recap_grace_s <= 0:
                self._recap(reason, ended)
            else:
                t = asyncio.ensure_future(self._recap_later(reason, ended))
                _RECAPS.add(t)
                t.add_done_callback(_RECAPS.discard)
        if self.fast is not None:
            try:
                await self.fast.close()
            except Exception:
                pass
        me = asyncio.current_task()
        for t in list(self.tasks):
            if t is not me:
                t.cancel()
        try:
            await self.provider.close()
        except Exception:
            pass
        try:
            await self.fe.hangup(reason)
        except Exception:
            pass
        if self.on_end and self.started is not None:
            self.on_end(self, time.monotonic() - self.started)

    def _keep_handle(self, why):
        """At hang-up (resume_window_s > 0): keep this conversation's resumption handle for a call soon after, only
        when it ended mid-conversation (he hung up, the line dropped). Never after the assistant's goodbye (end_call,
        the closing flow: Gemini resumed those sessions mute) and never off a resumed call whose model stayed silent
        (no chaining a dead conversation); the slot is cleared then. Call log resume_handle{kept, why?}."""
        if getattr(self.cfg, "resume_window_s", 0) <= 0:
            return
        handle = getattr(self.provider, "resume_handle", None)
        skip = None
        if why in ASSISTANT_ENDS or self.idle_stage >= 2 or self.ending is not None:
            skip = "assistant_end"
        elif getattr(self.provider, "resumed", False) and not self.model_live:
            skip = "mute_resume"
        elif not self.model_live and not self.him_t:
            skip = "empty"                   # nothing was said either way: nothing to continue
        if skip or not handle:
            RESUME.clear()
        else:
            RESUME.put(handle)
        if handle:
            self.log.event("resume_handle", kept=not skip, why=skip)

    def _usage_end(self, reason, by, why):
        """ONE usage event per call: voice.call_end, outcome = why (closing.end_why); ids, flags and numbers only.
        ref = the call's hash, as ~/bin/lib/usage.py's call-log collector keys the call's own row."""
        cid = getattr(self.log, "id", None)
        if not cid:
            return
        try:
            kind = str(getattr(self.log, "kind", "") or "")
            ref = closing.call_ref(cid)
            fn = self._ulog or closing.usage_log()
            fn("voice.call_end", "invoke", channel="app" if kind.startswith("app") else "voice", actor="assistant",
               outcome=why, ref=ref, ext_id="callend:" + ref[5:],
               attrs={"by": str(by or ended_by(reason) or "").replace(" ", "_") or None, "call_kind": kind or None,
                      "asked": self.end_flags["asked"], "nudged": self.end_flags["nudged"],
                      "fallback": self.end_flags["fallback"], "pending": self.pending_now(), "stage": self.idle_stage,
                      "resumes": self.metrics.get("resumes", 0), "gap_ms": self.metrics.get("gap_ms", 0),
                      "dur_s": int(time.monotonic() - self.started) if self.started else 0})
        except Exception:
            pass

    def _decisions_note(self, reason):
        """ONE inbox note with his spoken picks (decisions.py); written at hang-up, nothing applied."""
        try:
            text = decisions.render_note(self.round, self.log.id, getattr(self.log, "kind", None),
                                         getattr(self.log, "t0", None), time.time(), reason)
            return decisions.write_note(self.cfg.inbox_dir, self.log.id, text)
        except Exception as e:
            self.log.event("error", message="decisions note: %s" % type(e).__name__)
            return None

    async def _recap_later(self, reason, ended):
        try:
            await asyncio.sleep(self.cfg.recap_grace_s)
        except asyncio.CancelledError:      # relay shutting down: write what we know now
            self._recap(reason, ended)
            raise
        self._recap(reason, ended)

    def _recap(self, reason, ended):
        """ONE inbox note when something from this call is still open (recap.py); nothing otherwise."""
        recap.settle(self.items, set(getattr(self.handoff, "pending", {}) or {}))
        items = recap.open_items(self.items)
        if not items:
            return None
        text = recap.render(items, self.log.id, getattr(self.log, "kind", None), getattr(self.log, "t0", None),
                            ended, reason)
        return recap.write(self.cfg.inbox_dir, self.log.id, text)


REST_ON_TELEGRAM = "An answer he asked for is still on its way: say only \"I'll send the rest on Telegram\" and goodbye."
IDLE_ASK = ("[call] He has said nothing for a while. If you just asked him something, ask it again in a few words; "
            "otherwise ask him once, very briefly and in his language, whether there is anything else. Nothing more.")
WHY_BY_CLASS = {"bye": "goodbye", "no": "nothing_else", "thanks": "thanks"}
IDLE_BYE = "[call] Still no answer. Say one short goodbye, then call end_call."
IDLE_BYE_PENDING = ("[call] Still no answer. Say \"I'll send the rest on Telegram\", then one short goodbye, then "
                    "call end_call.")
