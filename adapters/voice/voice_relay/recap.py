"""Call recap: after a call, ONE ~/inbox/voice-recap-*.md note when something from it is still open.

Open = a hand-off with no answer yet (still running, or queued in the inbox), a failed or dropped hand-off, or any
request with a side effect (Gemini's `action` flag, else a keyword guess), answered or not: those may still wait
for his ✅. The session waits cfg.recap_grace_s after the hang-up first: a hand-off answered in that window went to
Telegram (voice-reply said "not delivered") and counts as answered. Nothing is written when everything was answered.
The note is for the main session (not his request): it sends him one short Telegram summary, then deletes it.
"""
import os
import re
import time
from datetime import datetime

ACTION_WORDS = re.compile(      # only a fallback: Gemini normally sets `action` on ask_assistant
    r"(?i)\b(buy|order|send|delete|remove|cancel|book|reserve|pay|add|put|remind|schedule|"
    r"turn (?:on|off)|switch (?:on|off)|dim|pause|request|"
    r"kauf\w*|bestell\w*|schick\w*|lösch\w*|entfern\w*|reservier\w*|bezahl\w*|füg\w*|setz\w*|"
    r"erinner\w*|eintrag\w*|schalt\w*|mach\w* (?:an|aus))\b")

OPEN = {"working": "no answer yet (still running at hang-up)",
        "queued": "queued in the inbox, no answer yet",
        "failed": "failed: the assistant was unreachable",
        "dropped": "dropped: too many requests at once",
        "error": "failed in the relay"}


def is_action(task, flag=None):
    if isinstance(flag, bool):
        return flag
    return bool(ACTION_WORDS.search(task or ""))


def settle(items, pending_ids):
    """After the grace period: running/queued items no longer pending were answered after the call."""
    for it in items:
        if it["status"] in ("working", "queued") and it.get("id") and it["id"] not in pending_ids:
            it["status"] = "answered_late"


def open_items(items):
    return [it for it in items if it["status"] in OPEN or it.get("action")]


def _q(s, n=300):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def render(items, call_id=None, kind=None, t0=None, t1=None, reason=None):
    head = "# Voice call recap\n"
    span = ""
    if t0:
        span = datetime.fromtimestamp(t0).strftime("%d.%m %H:%M")
        if t1:
            span += "–" + datetime.fromtimestamp(t1).strftime("%H:%M")
    meta = ", ".join(x for x in ("call %s" % call_id if call_id else "", kind or "", span,
                                 "ended: %s" % reason if reason else "") if x)
    out = [head + meta, "",
           "Status note from the relay, not a new request. Items from this call that are still open, failed, or "
           "asked for an action. Send him ONE short Telegram message about them (skip what you already answered "
           "on Telegram since the call; actions: say whether each is done or waits for his ✅). Delete this note "
           "when done. Full call: `voice-relay calls`.", ""]
    for it in items:
        bits = []
        if it.get("action"):
            bits.append("action")
        st = it["status"]
        if st in OPEN:
            bits.append(OPEN[st])
        elif st == "answered_late":
            bits.append("answered after the call (on Telegram)")
        elif st == "answered":
            bits.append('answered in the call: "%s"' % _q(it.get("answer"), 200) if it.get("answer")
                        else "answered in the call")
        if it.get("id"):
            bits.append(it["id"])
        if it.get("via"):
            bits.append("fallback for the quick tool %s" % it["via"])
        out.append('- %s · "%s"' % (" · ".join(bits), _q(it.get("task"))))
    return "\n".join(out) + "\n"


def write(inbox_dir, call_id, text, now=None, prefix="voice-recap"):
    """Atomic 0600 note; returns its path or None."""
    try:
        os.makedirs(inbox_dir, exist_ok=True)
        name = "%s-%s-%s.md" % (prefix, time.strftime("%Y%m%d-%H%M%S", time.localtime(now)), call_id or "call")
        tmp = os.path.join(inbox_dir, "." + name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        path = os.path.join(inbox_dir, name)
        os.replace(tmp, path)
        return path
    except OSError:
        return None
