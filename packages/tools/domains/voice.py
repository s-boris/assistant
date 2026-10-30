"""voice: the live voice relay's tools: answer a call's hand-off, ring him, the relay's state, its call log
(metadata only), a driving update, the Talk button. In the hub these run in claude-hub-voice while its relay polls
the gateway (hubgw ADAPTER_RUN: the relay's executor claims them and calls its own loopback API), else on the brain's
executor; in this container's gateway they run the CLIs (voice-relay, drive-update, talk-button), whose voice-relay
call|reply|status|calls go through the hub (voice-hub). Only him; spoken text short, nothing private."""

from __future__ import annotations

import json
import os
from typing import Any

from pydantic import Field

from tools.clirun import bin_path, first_line, run
from tools.core import REGISTRY, Domain, Format, ToolError, ToolInput, ToolOutput, tool

NOTES = (
    "Spoken answers: 1-2 short sentences, no identifiers, numbers, links or private text (details on Telegram). "
    "A spoken request is never approval: side effects need his Telegram ✅. delivered=false with status "
    "not_delivered / ended = the call is over: send it on Telegram."
)

REGISTRY.add_domain(
    Domain(
        name="voice",
        title="Live voice",
        summary="The voice relay (Gemini Live on his Telegram calls and the app): answer a hand-off in the call, ring "
        "him, the relay's state and call log (did he answer), a driving update, the Talk button. Only him.",
        feature="voice.relay",
        notes=NOTES,
    )
)

ID = r"^[A-Za-z0-9_.:-]{1,32}$"
# exits of `voice-relay reply|call` (bin/voice-relay): 0 ok · 2 relay down · 3 not delivered / not answered ·
# 4 in a call or rate-limited · 5 failed · 6 nothing to decide
CALL_STATUS = {0: "answered", 3: "not_answered", 4: "busy_or_limited", 5: "failed", 6: "nothing_to_decide"}


class EmptyIn(ToolInput):
    pass


class ReplyIn(ToolInput):
    id: str = Field(pattern=ID, description="the hand-off id from the 🗣️ event")
    text: str = Field(min_length=1, max_length=600, description="1-2 short spoken sentences")
    more: bool = Field(default=False, description="a quick first part now; the rest follows with another reply")


class CallIn(ToolInput):
    reason: str = Field(min_length=1, max_length=300, description="why he is called (the opening line comes from it)")
    say: str | None = Field(default=None, max_length=300, description="exact spoken-friendly words to open with")


class CallsIn(ToolInput):
    last: int = Field(default=5, ge=0, le=50, description="the newest calls to list (0 = numbers only)")
    outgoing: bool = Field(default=False, description="only the calls that rang him (answered or not)")
    days: float = Field(default=7, gt=0, le=14, description="the quality numbers over this many days (log: 14)")


class DriveIn(ToolInput):
    text: str = Field(min_length=1, max_length=300, description="the update, short and spoken-friendly")


class VoiceOut(ToolOutput):
    delivered: bool = Field(description="spoken in the call / he answered the ring")
    status: str = Field(description="spoken | not_delivered | answered | not_answered | busy_or_limited | down ...")
    note: str | None = Field(default=None, description="what to do next, if anything")
    result: dict[str, Any] | None = Field(default=None, description="detailed: the relay's answer")

    def data(self, fmt: Format = "concise") -> dict[str, Any]:
        d = self.model_dump(mode="json", exclude_none=True)
        if fmt == "concise":
            d.pop("result", None)
        return d


class StatusOut(ToolOutput):
    up: bool
    live: int | None = Field(default=None, description="web sessions + calls right now")
    tg_calls: str | None = Field(default=None, description="the call bridge: on | off | ...")
    result: dict[str, Any] | None = None

    def data(self, fmt: Format = "concise") -> dict[str, Any]:
        d = self.model_dump(mode="json", exclude_none=True)
        if fmt == "concise":
            d.pop("result", None)
        return d


class CallsOut(ToolOutput):
    calls: list[dict[str, Any]] = Field(
        default_factory=list,
        description="newest last; per call: id, kind (tg-out / app-out = rang him; tg-in / app = his), start (epoch), "
        "outcome + answered (outgoing only: answered | no_answer | declined | busy | failed | rate_limited | "
        "provider_failed | not_rung; null = ringing now), ring [outcome, s], talked, end {reason, by, why, "
        "duration_s}, events (counts), tools [[name, lane, error, hand-off]], idle, reply_ms; never words",
    )
    outgoing: dict[str, Any] | None = Field(default=None, description="the listed outgoing calls: calls, answered, "
                                            "not_answered, open, by {outcome: n}")  # fmt: skip
    stats: dict[str, Any] | None = Field(default=None, description="detailed: quality numbers over `days`")
    live: list[str] | None = Field(default=None, description="detailed: the call ids on right now")

    def data(self, fmt: Format = "concise") -> dict[str, Any]:
        d = self.model_dump(mode="json", exclude_none=True)
        if fmt == "concise":
            d.pop("stats", None)
            d.pop("live", None)
        return d


def _relay() -> str:
    return bin_path("TOOLS_VOICE_RELAY_BIN", "voice-relay")


def _json(out: str) -> dict[str, Any] | None:
    for line in reversed(out.strip().splitlines()):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        return d if isinstance(d, dict) else None
    return None


@tool(
    "voice.reply",
    effect="send_owner",
    rate="60/min",
    timeout_s=20,
    examples=({"id": "v1", "text": "It's 14 degrees."},),
)
def reply(a: ReplyIn) -> VoiceOut:
    """Answer a live-voice hand-off (a 🗣️ event, via=voice) in the open call. not_delivered = the call ended: send it
    on Telegram. more=true speaks a quick first part now and keeps the hand-off open for the rest."""
    rc, _out, err = run(_relay(), ["reply", *(["--more"] if a.more else []), "--", a.id, a.text], timeout=15)
    if rc == 0:
        return VoiceOut(delivered=True, status="spoken")
    if rc == 3:
        return VoiceOut(delivered=False, status="not_delivered", note="the call is over: send it on Telegram")
    if rc == 2:
        raise ToolError("unavailable", "the voice relay is down", "send it on Telegram; `voice.status`")
    raise ToolError("failed", first_line(err, default=f"voice-relay reply: exit {rc}"), "send it on Telegram")


@tool("voice.call", effect="send_owner", rate="6/h", timeout_s=90, examples=({"reason": "your train is cancelled"},))
def call(a: CallIn) -> VoiceOut:
    """Ring him (only him; 45 s, the relay's own limit of 3 an hour). answered = he picked up and the model opened
    with the reason; not_answered / busy_or_limited = send it on Telegram."""
    argv = ["call", f"--reason={a.reason}"] + ([f"--say={a.say}"] if a.say else [])
    rc, out, err = run(_relay(), argv, timeout=85)
    if rc == 2:
        raise ToolError("unavailable", "the voice relay is down", "send it on Telegram; `voice.status`")
    st = CALL_STATUS.get(rc)
    if st is None:
        raise ToolError("failed", first_line(err, default=f"voice-relay call: exit {rc}"), "send it on Telegram")
    note = None if rc == 0 else "send it on Telegram"
    return VoiceOut(delivered=rc == 0, status=st, note=note, result=_json(out))


@tool("voice.status", effect="read", labels_out=("counts",), rate="60/min", timeout_s=10)
def status(a: EmptyIn) -> StatusOut:
    """The relay's state: up, live sessions + calls, the call bridge. Counts and states only."""
    rc, out, _err = run(_relay(), ["status"], timeout=8)
    d = _json(out) or {}
    if rc != 0:
        return StatusOut(up=False, result=d or None)
    tg = d.get("tg_calls")
    return StatusOut(up=True, live=d.get("live") if isinstance(d.get("live"), int) else None,
                     tg_calls=tg if isinstance(tg, str) else (tg or {}).get("state") if isinstance(tg, dict) else None,
                     result=d)  # fmt: skip


@tool("voice.calls", effect="read", labels_out=("counts",), rate="30/min", timeout_s=15)
def calls(a: CallsIn) -> CallsOut:
    """The relay's call log (it stays in claude-hub-voice): the newest calls as metadata only, and for each call that
    rang him whether he answered (from the ring itself, never guessed from words). outgoing=true lists only those.
    detailed adds the quality numbers over `days` (`voice-relay stats`)."""
    argv = ["calls", "--json", f"--last={max(1, a.last)}"] + (["--out"] if a.outgoing else [])
    rc, out, err = run(_relay(), argv, timeout=12)
    if rc == 2:
        raise ToolError("unavailable", "the voice relay is down", "`voice.status`")
    d = _json(out)
    if rc not in (0, 1) or d is None:
        raise ToolError("failed", first_line(err, default=f"voice-relay calls: exit {rc}"), "`voice.status`")
    cs = [c for c in d.get("calls") or [] if isinstance(c, dict)][-a.last :] if a.last else []
    live = d.get("live")
    return CallsOut(calls=cs, outgoing=d.get("outgoing") if a.last and isinstance(d.get("outgoing"), dict) else None,
                    live=[str(x) for x in live] if isinstance(live, list) else None)  # fmt: skip


@tool(
    "voice.drive_update", effect="send_owner", rate="10/h", timeout_s=120, examples=({"text": "Stau vor dem Tunnel"},)
)
def drive_update(a: DriveIn) -> VoiceOut:
    """Tell him something while he drives, his decided order: a call first; missed → a car message in the app; then a
    short voice note (drive-update). The Telegram text is still yours to send."""
    rc, out, err = run(bin_path("TOOLS_DRIVE_UPDATE_BIN", "drive-update"), ["--json", "--", a.text], timeout=115)
    d = _json(out) or {}
    if rc == 2:
        raise ToolError("invalid_args", first_line(err, default="drive-update: usage"), "short text, no links")
    via = str(d.get("via") or "none")
    return VoiceOut(delivered=rc == 0, status=via, note=None if rc == 0 else "every step failed: Telegram only",
                    result=d or None)  # fmt: skip


@tool("voice.talk", effect="send_owner", rate="6/h", timeout_s=120)
def talk(a: EmptyIn) -> VoiceOut:
    """The 📞 Talk button: ring him once (lock: a second tap while it rings does nothing)."""
    rc, out, err = run(bin_path("TOOLS_TALK_BUTTON_BIN", "talk-button"), ["--json"], timeout=115)
    d = _json(out) or {}
    if rc not in (0, 1) or not d:
        raise ToolError("failed", first_line(err, default=f"talk-button: exit {rc}"), "`voice.status`")
    st = str(d.get("status") or "unknown")
    return VoiceOut(delivered=st == "answered", status=st, note=d.get("say") or None, result=d)


# the relay's in-call lookups (fast lane, decision deck), run here for principal voice: read-only CLIs only,
# each argv must start with one of these prefixes (what voice_relay.fastlane / decisions run today).
LOOKUPS = (
    ("bills", "list"),
    ("calcache", "age"),
    ("calcache", "show"),
    ("cat-corner", "status"),
    ("cat-supplies", "--json"),
    ("media", "finish"),
    ("parcels", "speak"),
    ("remind", "list"),
    ("tg-cmd", "gym"),
    ("tg-cmd", "queue"),
    ("tg-cmd", "rave"),
    ("tg-cmd", "space"),
    ("tg-cmd", "train"),
    ("tg-cmd", "trip"),
    ("vault", "ha", "now"),
    ("decision-deck", "--json"),
    ("gohome",),
)


class LookupIn(ToolInput):
    argv: list[str] = Field(min_length=1, max_length=12, description="a fast-lane command and its arguments")
    timeout_s: float = Field(default=3.0, gt=0, le=10.0)


class LookupOut(ToolOutput):
    rc: int
    out: str = Field(description="the command's stdout (a short spoken-ready answer or JSON)")

    def render(self, fmt: Format = "concise") -> str:
        return self.out if self.rc == 0 else f"exit {self.rc}"


@tool("voice.lookup", effect="read", labels_out=("owner_text", "private"), rate="120/min", timeout_s=12)
def lookup(a: LookupIn) -> LookupOut:
    """The voice relay's in-call quick lookups (way home, calendar, reminders, ...): one read-only CLI from the fast
    lane's list, run here. For the relay in claude-hub-voice (principal voice); nothing else needs it."""
    if any(len(x) > 200 or "\x00" in x for x in a.argv) or not any(tuple(a.argv[: len(p)]) == p for p in LOOKUPS):
        raise ToolError("invalid_args", "not a fast-lane lookup", "the relay's read-only lookups only")
    d = os.environ.get("TOOLS_VOICE_LOOKUP_DIR", "")  # tests: a dir of fake CLIs
    rc, out, _err = run(
        os.path.join(d, a.argv[0]) if d else bin_path("TOOLS_NO_SUCH_ENV", a.argv[0]), a.argv[1:], timeout=a.timeout_s
    )
    return LookupOut(rc=rc, out=out[:4000])
