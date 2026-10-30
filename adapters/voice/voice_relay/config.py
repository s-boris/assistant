"""Configuration from VOICE_* environment variables. Secrets are read from files at use time, never logged."""
import json
import os
from dataclasses import dataclass, field, fields

HOME = os.path.expanduser("~")


def _p(*parts):
    return os.path.join(HOME, *parts)


@dataclass
class Config:
    provider: str = "gemini"                  # gemini (primary) | openai
    public_host: str = "0.0.0.0"              # page + websocket (goes behind the reverse proxy)
    public_port: int = 8790
    internal_port: int = 8791                 # 127.0.0.1 only: voice-reply, login links, status
    public_origin: str = ""                   # e.g. https://voice.example.org; empty = same-host check (local)
    trusted_proxies: tuple = ()               # peers whose X-Forwarded-For is believed
    secure_cookie: bool = True                # False only for plain-http local tests
    state_dir: str = field(default_factory=lambda: _p(".local", "state", "voice-relay"))
    local_token_file: str = field(default_factory=lambda: _p(".secrets", "voice-local-token"))
    gemini_key_file: str = field(default_factory=lambda: _p(".secrets", "gemini-api-key"))
    openai_key_file: str = field(default_factory=lambda: _p(".secrets", "openai-api-key"))
    gemini_model: str = "gemini-3.8-live"     # verify the id in AI Studio at go-live
    gemini_fallbacks: tuple = ("gemini-2.5-flash-native-audio-latest",)   # tried in order when Gemini rejects
                                              # the model at setup (gone / not found / unsupported); () = none
    thinking_cue: bool = True                 # soft two-note cue while he waits on a "now" hand-off and the
    thinking_after_s: float = 1.5             # line has been quiet this long; again every thinking_every_s
    thinking_every_s: float = 2.5             # (local audio to him only, never sent to the model)
    resume_window_s: float = 900.0            # a new call this soon after the last one continues its conversation
                                              # (Gemini session resumption handle, memory only); 0 = off.
                                              # Never after the assistant's goodbye (end_call / closing flow)
    resume_fallback_s: float = 3.0            # a resumed call: no model audio or tool call this long after the
                                              # opening or his words → reopen fresh in the same call (0 = off)
    gemini_ws_url: str = ("wss://generativelanguage.googleapis.com/ws/"
                          "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent")
    gemini_voice: str = "Puck"
    gemini_rest_url: str = "https://generativelanguage.googleapis.com/v1beta"   # models.get (canary)
    gemini_async: bool = True                 # NON_BLOCKING ask_assistant (async function calling)
    openai_model: str = "gpt-realtime"
    openai_ws_url: str = "wss://api.openai.com/v1/realtime"
    openai_voice: str = "marin"
    plugin_url: str = "http://127.0.0.1:8792/voice"   # telegram-plus voice inbox (staged patch)
    inbox_dir: str = field(default_factory=lambda: _p("inbox"))
    handoff_order: tuple = ("channel", "inbox")       # add "worker" to try a claude -p lane first
    worker_cmd: str = ""                      # e.g. "claude -p --model haiku" (run from ~/jobs/voice-relay)
    worker_cwd: str = field(default_factory=lambda: _p("jobs", "voice-relay"))
    worker_timeout_s: float = 18.0
    sync_wait_s: float = 8.0                  # "now" answers within this are spoken as the tool result; later
                                              # → "takes a bit longer" and the answer comes live when it lands
    slow_cue_max_s: float = 30.0              # ...the thinking cue goes on (half as often) up to this after the ask
    opening_prefetch: bool = True             # outbound pick-up: generate the greeting during the hold, play at its end
    greet_inbound: bool = True                # HIS calls (tg-in, app): a short line the moment the line is up (greet.py)
    greet_text: str = "Hey, was gibt's?"      # ...the line (1-3 words), cached as TTS audio in the call voice
    greet_tts_models: tuple = ("gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts")   # tried in order (both GA); () = earcon only
    greet_delay_s: float = 0.3                # ...after the line connected (his phone switches to the call), then it plays
    greet_barge_ms: int = 150                 # ...his voice this long over it cuts it off (local barge-in)
    reconnect_tries: int = 2                  # Gemini socket dropped mid-call: reconnect (resumption handle) this often
    reconnect_timeout_s: float = 6.0          # ...each attempt at most this long (0 tries = end the call as before)
    reconnect_buffer_s: float = 4.0           # ...his newest audio kept meanwhile and sent once the line is back
    link_resume: bool = True                  # phone app: phonegw holds the call through a dropped app connection
                                              # (link lost/back frames): timers pause, back = "lost you for a moment"
    link_answer_wait_s: float = 8.0           # ...a late answer waits this long for the line (voice-reply waits 10 s)
    max_session_s: int = 1800                 # one call/page session
    max_minutes_per_day: int = 30             # all sessions together (cost cap on our side)
    max_live_sessions: int = 2
    max_asks_per_min: int = 10
    mask_amounts: bool = True                 # money amounts never go to the voice provider
    session_days: int = 30                    # login cookie lifetime
    link_minutes: int = 15                    # one-time login link lifetime
    prompt_file: str = field(default_factory=lambda: os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "prompts", "voice-agent.md"))
    fast_lane: bool = True                    # read-only quick tools inside the call (fastlane.py)
    fast_timeout_s: float = 3.0               # per quick tool, capped at 3 s; then ask_assistant takes over
    fast_bin_dir: str = field(default_factory=lambda: _p("bin"))
    fast_cal_max_age_h: float = 36.0          # calcache older than this → hand off instead
    profile_file: str = field(default_factory=lambda: os.environ.get("ASSISTANT_PROFILE") or _p(
        ".config", "assistant", "profile.json"))
    weather_url: str = "https://api.open-meteo.com/v1/forecast"
    geocode_url: str = "https://geocoding-api.open-meteo.com/v1/search"
    track_id: bool = True                     # identify_track: name the music from his call audio (track.py)
    track_cmd: str = field(default_factory=lambda: _p("bin", "trackid"))
    track_buffer_s: float = 20.0              # rolling buffer of his incoming audio (memory only, per call)
    track_min_s: float = 6.0                  # less audio than this: listen first
    track_more_s: float = 10.0                # no sure match: listen this much more, retry once
    track_timeout_s: float = 20.0             # one trackid run, at most
    decisions: bool = True                    # decision round tools (decisions.py): decision_deck + record_pick
    decision_deck_cmd: str = field(default_factory=lambda: _p("bin", "decision-deck") + " --json")
    decision_timeout_s: float = 5.0
    phone_open: bool = True                   # navigate + open_on_phone: open nav/links/SBB/apps on his phone
    phone_cmd: str = field(default_factory=lambda: _p("bin", "phone"))   # (phoneopen.py; `phone open ... --json`)
    phone_open_wait_s: int = 6                # --wait: how long the CLI waits for the app to open it
    phone_open_timeout_s: float = 15.0        # hard cap on one `phone open` run (app wait + Telegram fallback)
    recap_grace_s: float = 90.0               # after hang-up: wait for late answers, then write the recap note
    tg_owner_id: int = 0                      # call bridge: the only Telegram user allowed to call
    tg_config_file: str = field(default_factory=lambda: _p(".secrets", "voice-tg.json"))
    end_call: bool = True                     # end_call tool: the assistant hangs up after a goodbye
    # ending a call well (open-mic calls with transcripts; session.py _idle + closing.py). Never while a tool runs or
    # an answer he waits for is pending (only idle_pending_max_s of silence ends such a call).
    closing: bool = True                      # his thanks/done → "anything else?"; his no/goodbye → goodbye + end_call
    closing_nudge_s: float = 3.0              # he said thanks/bye and the model stayed silent this long → [call] nudge
    closing_bye_s: float = 8.0                # silence after "anything else?" that followed his thanks → goodbye
    idle_prompt_s: float = 20.0               # silent this long after a completed answer → "anything else?" (0 = off)
    idle_unanswered_s: float = 60.0           # ...when no answer completed since he last spoke (or nothing yet)
    idle_bye_s: float = 10.0                  # ...then this long more → goodbye + hang up (0 = off)
    bye_fallback_s: float = 4.0               # after a goodbye: no end_call from the model this long → the relay hangs up
    idle_pending_max_s: float = 300.0         # an answer he waits for is pending: silent this long → "rest on Telegram"
    hangup_tail_s: float = 0.5                # after the goodbye audio has played
    opening_wait_s: float = 1.5               # outbound pick-up: no word from him this long → greet him
    opening_quiet_s: float = 0.5              # ...he said hello: greet once he has been quiet this long
    opening_max_s: float = 4.0                # ...at most this long after pick-up, whatever he does
    voice_rms: float = 500.0                  # his incoming PCM16 louder than this (RMS) counts as speech
    vad_silence_ms: int = 500                 # Gemini end of speech: this much silence ends his turn
    files_inbox: bool = True                  # media the owner sends to the call account → files_dir (bigfiles.py)
    files_dir: str = field(default_factory=lambda: _p("inbox", "files"))
    files_max_mb: int = 2048                  # MTProto user accounts download up to 2 GiB
    files_min_free_gb: float = 20.0           # refuse when free space would drop below this
    files_timeout_s: float = 10800.0          # one download, at most
    # hub mode (hubmode.py; claude-hub-voice): hand-offs, notes and voice.* tools through the hub
    hub_mode: bool = False
    hub_url: str = ""                         # hubd, e.g. http://claude-hub:8700
    hub_gw_url: str = ""                      # the hub gateway, e.g. http://claude-hub:8701
    hub_token_file: str = ""                  # this container's hub caller token (Cedar principal voice)
    tg_access_file: str = field(default_factory=lambda: _p(".claude", "channels", "telegram", "access.json"))
    tg_session_dir: str = ""                  # the call account's session dir (writable); "" = ~/.secrets
    # link listener (hub mode only; 0 = off): the phone gateway in another container reaches app calls
    # over the brain network: /internal/ticket + /internal/status with the link token (never the local token), /ws
    # for app tickets only (server.link_app)
    link_host: str = "0.0.0.0"
    link_port: int = 0
    link_token_file: str = ""

    @classmethod
    def from_env(cls, env=None, **over):
        env = os.environ if env is None else env
        kw = {}
        for f in fields(cls):
            raw = env.get("VOICE_" + f.name.upper())
            if raw is None:
                continue
            if f.type in ("int", int):
                kw[f.name] = int(raw)
            elif f.type in ("float", float):
                kw[f.name] = float(raw)
            elif f.type in ("bool", bool):
                kw[f.name] = raw.strip().lower() in ("1", "true", "yes", "on")
            elif f.type in ("tuple", tuple):
                kw[f.name] = tuple(x.strip() for x in raw.split(",") if x.strip())
            else:
                kw[f.name] = raw
        kw.update(over)
        return cls(**kw)


def read_secret(path):
    """First line of a 0600 secret file, or '' if missing. Never printed."""
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.readline().strip()
    except OSError:
        return ""


def owner_telegram_id(cfg):
    """The owner's Telegram user id: config, else the first allowFrom entry of the bot's access.json (read-only)."""
    if cfg.tg_owner_id:
        return int(cfg.tg_owner_id)
    try:
        with open(cfg.tg_access_file, encoding="utf-8") as fh:
            allow = json.load(fh).get("allowFrom") or []
        return int(allow[0]) if allow else 0
    except (OSError, ValueError, TypeError):
        return 0
