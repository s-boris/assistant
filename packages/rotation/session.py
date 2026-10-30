"""rotation.session: what the rotation reads about the live main session, read-only. Stdlib + core.

  agents()                       `claude agents --json` (ROTATE_AGENTS_JSON: a file standing in); None = unknown
  main_session(agents)           the workspace's interactive session {sessionId, status, startedAt, ...} or None
  transcript(session_id)         its JSONL path under the project folder (ASSISTANT_PROJECT_DIR in tests)
  last_owner_message(path)       epoch of the newest <channel …> prompt (telegram, app, voice) in the tail; None
  session_crons(path, now)       CronCreate jobs still alive (session-only: a fresh session loses them)
  has_marker(path, marker, since) whether a transcript holds a text (the hand-off marker) after `since`
Counts and timestamps only leave this module; message text is never returned.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from core import paths

TAIL_BYTES = 4_000_000
RECURRING_TTL_S = 7 * 86400  # Claude Code expires recurring CronCreate jobs after 7 days
SCHED_RX = re.compile(r"Scheduled (recurring job|one-shot task) ([0-9a-f]{6,16}) \(([^)]+)\)")


def project_dir() -> Path:
    env = os.environ.get("ASSISTANT_PROJECT_DIR", "")
    if env:
        return paths.guard(env)
    ws = os.fspath(paths.home() / "workspace")
    return paths.home() / ".claude" / "projects" / ws.replace("/", "-")


def agents() -> list[dict[str, Any]] | None:
    try:
        f = os.environ.get("ROTATE_AGENTS_JSON")
        if f:
            raw = Path(f).read_text(encoding="utf-8")
        elif paths.test_mode():
            return None
        else:
            exe = shutil.which("claude") or os.fspath(paths.home() / ".local" / "bin" / "claude")
            p = subprocess.run([exe, "agents", "--json"], capture_output=True, text=True, timeout=30, check=False)
            if p.returncode != 0:
                return None
            raw = p.stdout
        data = json.loads(raw)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return [a for a in data if isinstance(a, dict)] if isinstance(data, list) else None


def main_session(ags: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    if not ags:
        return None
    ws = os.fspath(paths.home() / "workspace")
    cands = [a for a in ags if a.get("cwd") == ws]
    return next((a for a in cands if a.get("kind") == "interactive"), None) or (cands[0] if cands else None)


def transcript(session_id: str | None) -> Path | None:
    if not session_id or not re.fullmatch(r"[0-9a-fA-F-]{8,64}", session_id):
        return None
    p = project_dir() / f"{session_id}.jsonl"
    return p if p.is_file() else None


def _iso(ts: Any) -> float | None:
    if not isinstance(ts, str):
        return None
    try:
        return _dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _texts(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    out = []
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "text" and isinstance(b.get("text"), str):
                    out.append(b["text"])
                elif b.get("type") == "tool_result":
                    out += _texts(b.get("content"))
    return out


def _tail_lines(path: Path, nbytes: int = TAIL_BYTES) -> list[bytes]:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - nbytes))
            data = f.read()
    except OSError:
        return []
    lines = data.split(b"\n")
    return lines[1:] if size > nbytes else lines


def last_owner_message(path: Path | None) -> float | None:
    """Epoch of the newest owner prompt (a user entry whose text starts with a <channel …> tag) in the tail."""
    if path is None:
        return None
    for ln in reversed(_tail_lines(path)):
        if b"<channel" not in ln:
            continue
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        if d.get("type") != "user":
            continue
        if any(t.lstrip().startswith("<channel") for t in _texts((d.get("message") or {}).get("content"))):
            return _iso(d.get("timestamp"))
    return None


def _cron_next(expr: str, after: float) -> float | None:
    """The fire time of a one-shot `M H D Mon *` expression (local time) at or after `after`, within a year."""
    f = expr.split()
    if len(f) != 5 or not all(x.isdigit() for x in f[:4]):
        return None
    mi, h, d, mo = (int(x) for x in f[:4])
    base = _dt.datetime.fromtimestamp(after)
    for year in (base.year, base.year + 1):
        try:
            t = _dt.datetime(year, mo, d, h, mi).timestamp()
        except ValueError:
            return None
        if t >= after - 60:
            return t
    return None


def session_crons(path: Path | None, now: float) -> list[dict[str, Any]]:
    """[{id, cron, recurring, created, next, prompt}] for CronCreate jobs not deleted, not fired, not expired.
    prompt = the brain's own check-in text (kept for the hand-off note, never logged)."""
    if path is None:
        return []
    created: dict[str, dict[str, Any]] = {}
    pending_inputs: dict[str, dict[str, Any]] = {}
    deleted: set[str] = set()
    try:
        fh = open(path, "rb")  # noqa: SIM115 (streamed: the main transcript runs to hundreds of MB)
    except OSError:
        return []
    with fh:
        for ln in fh:
            if b"Cron" not in ln and b"Scheduled " not in ln:
                continue
            try:
                d = json.loads(ln)
            except ValueError:
                continue
            ts = _iso(d.get("timestamp")) or 0.0
            content = (d.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use" and b.get("name") == "CronCreate":
                    pending_inputs[str(b.get("id"))] = dict(b.get("input") or {})
                elif b.get("type") == "tool_use" and b.get("name") == "CronDelete":
                    deleted.add(str((b.get("input") or {}).get("id") or ""))
                elif b.get("type") == "tool_result":
                    inp = pending_inputs.pop(str(b.get("tool_use_id")), None)
                    if inp is None:
                        continue
                    for t in _texts(b.get("content")):
                        m = SCHED_RX.search(t)
                        if m:
                            created[m.group(2)] = {
                                "id": m.group(2),
                                "cron": m.group(3),
                                "recurring": m.group(1).startswith("recurring"),
                                "created": ts,
                                "prompt": str(inp.get("prompt") or ""),
                            }
    out = []
    for cid, c in created.items():
        if cid in deleted:
            continue
        if c["recurring"]:
            if now - c["created"] > RECURRING_TTL_S:
                continue
            c["next"] = None
        else:
            nxt = _cron_next(c["cron"], c["created"])
            if nxt is None or nxt < now:
                continue  # fired (one-shots delete themselves) or unreadable
            c["next"] = nxt
        out.append(c)
    return sorted(out, key=lambda c: (c["next"] or 0, c["created"]))


def has_marker(path: Path | None, marker: str, since: float = 0.0) -> bool:
    """Whether an entry at or after `since` holds `marker` (the hand-off note's id line, seen once it was read)."""
    if path is None:
        return False
    mb = marker.encode()
    try:
        fh = open(path, "rb")  # noqa: SIM115
    except OSError:
        return False
    with fh:
        for ln in fh:
            if mb not in ln:
                continue
            try:
                ts = _iso(json.loads(ln).get("timestamp")) or 0.0
            except ValueError:
                continue
            if ts >= since:
                return True
    return False


def cron_recreated(path: Path | None, cron: str, since: float) -> bool:
    """Whether the new session scheduled the same cron expression again (CronCreate) after `since`."""
    if path is None:
        return False
    try:
        fh = open(path, "rb")  # noqa: SIM115
    except OSError:
        return False
    with fh:
        for ln in fh:
            if b"CronCreate" not in ln:
                continue
            try:
                d = json.loads(ln)
            except ValueError:
                continue
            if (_iso(d.get("timestamp")) or 0.0) < since:
                continue
            for b in (d.get("message") or {}).get("content") or []:
                if (
                    isinstance(b, dict)
                    and b.get("type") == "tool_use"
                    and b.get("name") == "CronCreate"
                    and str((b.get("input") or {}).get("cron") or "").split() == cron.split()
                ):
                    return True
    return False
