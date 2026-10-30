"""inbox.reflexrun: `events ack` runs the reflexes the acked events match (v2 exit E6, 29.10).

Why: digest and mute events are acked in bulk, so the brain never ran `reflex match` for them and their reflexes never
fired (own-schedule-change: 23 events, 0 firings). Doing it inside the ack also saves the brain the three serial calls
(`reflex match` → `tool call` → `reflex fire`) a reflex took per beat (E7: API calls per beat).

  run(evs, *, deadline_s=20) -> list[str]
        for each event (dict with id, kind, payload) and each active reflex it matches (`reflex match KIND --payload`)
        that has no firing for this event yet: `tool call TOOL ARGS --json` through the gateway, then `reflex fire
        NAME --outcome ok|error --event ID --ms N`. Returns one line per firing for the ack's output: the reflex, the
        outcome and the tool's short answer cut to 240 chars (the brain reads it in the same call and can still act;
        the firing log itself holds ids and outcomes only, never the answer).
Fails open: any error (missing CLI, timeout, bad JSON) skips that reflex with a "skipped" line; the ack itself always
goes on. Off: ~/.config/assistant/reflex-on-ack-off. Seams: REFLEX_BIN, TOOL_BIN, LOOP_STATE (via sources.reflex_db).
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from contextlib import closing
from typing import Any

from core import paths

from . import sources

__all__ = ["run", "enabled"]

ANSWER_MAX = 240
CALL_TIMEOUT_S = 15.0


def enabled() -> bool:
    return not paths.config("reflex-on-ack-off").exists()


def _bin(env: str, name: str) -> str:
    return os.environ.get(env) or os.path.join(os.path.expanduser("~"), "bin", name)


def _sh(argv: list[str], timeout: float) -> tuple[int, str]:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout
    except (OSError, subprocess.SubprocessError):
        return 99, ""


def _fired(name: str, event_id: int) -> bool:
    p = sources.reflex_db()
    if not p.exists():
        return False
    try:
        with closing(sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=1)) as con:
            return (
                con.execute(
                    "SELECT 1 FROM firings WHERE name = ? AND event = ? LIMIT 1", (name, str(event_id))
                ).fetchone()
                is not None
            )
    except sqlite3.Error:
        return False


def _payload(e: dict[str, Any]) -> str:
    pl = e.get("payload")
    if isinstance(pl, str):
        try:
            pl = json.loads(pl or "{}")
        except ValueError:
            pl = {}
    return json.dumps(pl if isinstance(pl, dict) else {}, ensure_ascii=False)


def _short(text: str) -> str:
    one = " ".join(str(text or "").split())
    return one if len(one) <= ANSWER_MAX else one[: ANSWER_MAX - 1] + "…"


def run(evs: list[dict[str, Any]], *, deadline_s: float = 20.0) -> list[str]:
    if not evs or not enabled():
        return []
    rb, tb = _bin("REFLEX_BIN", "reflex"), _bin("TOOL_BIN", "tool")
    if not os.path.exists(rb):
        return []
    t_end = time.monotonic() + deadline_s
    out: list[str] = []
    for e in evs:
        if time.monotonic() > t_end:
            out.append("reflexes: deadline reached, the rest not run")
            break
        rc, raw = _sh([rb, "match", str(e.get("kind") or ""), "--payload", _payload(e), "--json"], 5)
        try:
            matches = json.loads(raw) if rc == 0 and raw.strip() else []
        except ValueError:
            matches = []
        if isinstance(matches, dict):
            matches = matches.get("matches") or []
        for m in matches if isinstance(matches, list) else []:
            name, tool = str(m.get("name") or ""), str(m.get("tool") or "")
            if not name or not tool or _fired(name, int(e["id"])):
                continue
            t0 = time.monotonic()
            left = max(1.0, min(CALL_TIMEOUT_S, t_end - t0))
            rc, raw = _sh([tb, "call", tool, json.dumps(m.get("args") or {}), "--json"], left)
            ms = int((time.monotonic() - t0) * 1000)
            answer, ok = "", rc == 0
            try:
                j = json.loads(raw) if raw.strip() else {}
                ok = ok and bool(j.get("ok", True))
                err = j.get("error")
                answer = j.get("text") or (f"{err.get('code')}: {err.get('message')}" if isinstance(err, dict) else "")
            except ValueError:
                answer = raw
            outcome = "ok" if ok else ("skipped" if rc == 99 else "error")
            _sh([rb, "fire", name, "--outcome", outcome, "--event", str(e["id"]), "--ms", str(ms)], 5)
            out.append(f"#{e['id']} reflex {name} → {tool} {outcome} ({ms} ms): {_short(answer) or '–'}")
    return out
