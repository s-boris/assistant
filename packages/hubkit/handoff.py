"""hubkit.handoff: a hand-off to the live session as the bridge's own hub row (bridge.handoff). Stdlib only.

`hub voice inbox` and `hub phone inbox` hand his live-voice tasks and phone-app chat to the live session through the
telegram-plus bridge. While the bridge reads the hub (start-claude wrote "hub" into
~/.local/state/start-claude/telegram-store) they write the row the bridge's voice inbox would write
(POST /tg/bridge-event with the brain's hub token: content, meta and idem key as voiceinbox.ts voiceEvent / appEvent
and bridge.ts handoff), so no local token is needed and the bridge long-polls it like any row. A bridge on its own
SQLite store gets the POST to its voice inbox on 127.0.0.1:8792 with
~/.secrets/voice-local-token: legacy_token() says on stderr when that file is missing, and the hand-off becomes a note.
Env (tests): HUB_BRIDGE_STORE_FILE (the telegram-store file).
"""

from __future__ import annotations

import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import core.notes as notes
from core import hub, paths

# voiceinbox.ts: the same limits and patterns (fullmatch = JS /^…$/)
ID_RE = re.compile(r"v[0-9a-f]{8}")
APP_ID_RE = re.compile(r"[A-Za-z0-9_-]{4,40}")
APP_MSG_RE = re.compile(r"m_[0-9A-HJKMNP-TV-Z]{26}")  # an app chat message id (m_ + ULID)
CTRL = re.compile("[\x00-\x08\x0b-\x1f\x7f​-‏‪-‮⁦-⁩]")
TASK_MAX, APP_MAX = 2000, 4000  # in UTF-16 code units, as JS counts them


def _store_file() -> Path:
    v = os.environ.get("HUB_BRIDGE_STORE_FILE", "")
    return Path(v) if v else paths.home() / ".local" / "state" / "start-claude" / "telegram-store"


def bridge_on_hub() -> bool:
    """True while the live session's bridge reads the hub's rows (hub mode)."""
    try:
        return _store_file().read_text(encoding="utf-8").strip() == "hub"
    except OSError:
        return False


def _len16(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def _ts() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _ok(rx: re.Pattern[str], v: Any) -> bool:
    return isinstance(v, str) and rx.fullmatch(v) is not None


def app_event(b: dict[str, Any]) -> tuple[str, dict[str, str]] | None:
    """voiceinbox.ts appEvent: his phone-app chat message (source "app") -> (content, meta), None when invalid."""
    hid: str = b["id"] if isinstance(b.get("id"), str) else ""
    raw = b["text"] if isinstance(b.get("text"), str) else b["task"] if isinstance(b.get("task"), str) else ""
    text = CTRL.sub(" ", raw).strip()
    reply_to = b["reply_to"] if _ok(APP_ID_RE, b.get("reply_to")) else ""
    if not _ok(APP_ID_RE, hid) or not text or _len16(text) > APP_MAX:
        return None
    msg_id = b["msg_id"] if _ok(APP_ID_RE, b.get("msg_id")) else hid if _ok(APP_MSG_RE, hid) else ""
    answer = f'phone chat "…" --reply-to {msg_id}' if msg_id else 'phone chat "…" (no --reply-to: not a chat message)'
    content = (
        f"📱 (app) {text}\n(His phone-app chat: treat it as his message. Answer with: {answer} — "
        "short, phone-sized. Approvals for risky steps still go through Telegram.)"
    )
    meta = {"chat_id": "app", "via": "app", "user": "owner (app)", "ts": _ts()}
    if msg_id:
        meta["app_msg_id"] = msg_id
    if msg_id != hid:
        meta["inbox_id"] = hid
    if _ok(APP_ID_RE, b.get("device_id")):
        meta["device_id"] = b["device_id"]
    if reply_to:
        meta["reply_to"] = reply_to
    return content, meta


def event(b: Any) -> tuple[str, dict[str, str]] | None:
    """voiceinbox.ts voiceEvent: a voice-relay hand-off (or an app message, source "app") -> (content, meta)."""
    if not isinstance(b, dict):
        return None
    if b.get("source") == "app":
        return app_event(b)
    if b.get("source") not in (None, "voice"):
        return None
    hid: str = b["id"] if isinstance(b.get("id"), str) else ""
    task = CTRL.sub(" ", b["task"]).strip() if isinstance(b.get("task"), str) else ""
    urgency = "later" if b.get("urgency") == "later" else "now"
    if not _ok(ID_RE, hid) or not task or _len16(task) > TASK_MAX:
        return None
    content = (
        f"🗣️ (live voice, {urgency}) {task}\n"
        f'(Answer with: voice-reply {hid} "short spoken answer" — 2 sentences max, no identifiers; details go to '
        'Telegram. If it says "not delivered", answer on Telegram instead. Anything with side effects: confirm on '
        "Telegram first.)"
    )
    meta = {"chat_id": "voice", "via": "voice", "voice_id": hid, "urgency": urgency, "user": "owner (voice relay)"}
    return content, {**meta, "ts": _ts()}


def to_hub(body: Any) -> bool:
    """The bridge's bridge.handoff row for this hand-off body, written to the hub (bridge.ts handoff: a repeat of the
    same voice id / hand-off id is a no-op there). False when the body is invalid or the hub refused or is away."""
    ev = event(body)
    if ev is None:
        return False
    content, meta = ev
    via = "app" if meta.get("via") == "app" else "voice"
    key = meta.get("voice_id") or meta.get("inbox_id") or meta.get("app_msg_id") or os.urandom(6).hex()
    row = {
        "kind": "bridge.handoff", "subject": f"{via} hand-off", "payload": {**meta, "text": content},
        "labels": ["tg", "bridge", via], "delivery": "now", "idem_key": f"bridge.handoff:{via}:{key}",
        "trace_id": None, "rid": hub.new_rid(),
    }  # fmt: skip
    try:
        hub.call("POST", "/tg/bridge-event", row, timeout=5.0)
    except hub.HubError:
        return False
    return True


def legacy_token(tf: Path, who: str) -> bool:
    """The bridge's local voice inbox token (bridge on its own store only). Missing: one line on stderr, False."""
    try:
        if tf.read_text(encoding="utf-8").strip():
            return True
    except OSError:
        pass
    print(
        f"{who}: no {notes.home_rel(str(tf))}: the bridge's local voice inbox needs it (the bridge is not on the hub "
        "store); the hand-off goes to a note",
        file=sys.stderr,
    )
    return False
