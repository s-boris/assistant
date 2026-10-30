"""inbox.sources: the agenda's context besides events. Read-only; every call has a timeout; failures return None.

  agents(timeout)            running background agents from `agent-watch status` (JSON when it offers it, else its
                             table): running, or stalled for at most 45 min, with their descriptions
  running(timeout)           as one snapshot dict {"ts", "agents"}
  scan_compactions(con, now) compact_boundary records of the main session transcript, incremental by byte offset;
                             only their metadata (time, trigger, tokens before/after, duration) is kept, in meta
  last_compaction(con)       the newest one, or None
  reflex_counts(since)       the agenda's reflex counter from ~/.local/state/loop/reflex.db (loop.reflex's store, read
                             only, 0.5 s): active reflexes, firings since `since` per reflex, misfires; None when the
                             registry is missing, empty and silent, or unreadable
The main transcript = the newest top-level *.jsonl in $ASSISTANT_PROJECT_DIR, else the Claude Code project folder of
~/workspace. Message content is never parsed or stored: a line is decoded only if it contains "compact_boundary".
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from core import clock, db, paths

__all__ = ["agents", "last_compaction", "project_dir", "reflex_counts", "running", "scan_compactions"]

STALLED_MAX_MIN = 45
KEEP_COMPACTIONS = 200
CHUNK = 8 * 1024 * 1024


def _run(argv: list[str], timeout: float) -> str | None:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def _minutes(s: str) -> int | None:
    s = s.strip()
    try:
        if s.endswith("m"):
            return int(s[:-1])
        if s.endswith("h"):
            return int(float(s[:-1]) * 60)
    except ValueError:
        return None
    return None


def parse_agent_table(out: str) -> list[dict[str, Any]]:
    lines = out.splitlines()
    hdr = next((i for i, line in enumerate(lines) if line.split()[:2] == ["ID", "STATE"]), None)
    if hdr is None:
        return []
    h = lines[hdr]
    dcol = h.find("DESCRIPTION")
    res = []
    for line in lines[hdr + 1 :]:
        p = line.split()
        if len(p) < 4:
            continue
        silent = _minutes(p[3])
        if silent is None:
            continue
        state = p[1]
        if state == "running" or (state == "stalled" and silent <= STALLED_MAX_MIN):
            desc = line[dcol:].strip() if 0 <= dcol < len(line) else ""
            res.append({"id": p[0], "state": state, "silent_min": silent, "desc": desc})
    return res


def agents(timeout: float = 0.8) -> list[dict[str, Any]] | None:
    out = _run([os.fspath(paths.bin_dir("agent-watch")), "status", "--hours", "6", "--json"], timeout)
    if out is None:
        return None
    s = out.lstrip()
    if s.startswith(("{", "[")):
        try:
            d = json.loads(s)
        except ValueError:
            return None
        items = d.get("agents", []) if isinstance(d, dict) else d
        res = []
        for a in items if isinstance(items, list) else []:
            st = a.get("state")
            silent = a.get("silent_min") or 0
            if st == "running" or (st == "stalled" and silent <= STALLED_MAX_MIN):
                res.append(
                    {
                        "id": str(a.get("id", "")),
                        "state": st,
                        "silent_min": silent,
                        "desc": str(a.get("description") or a.get("desc") or ""),
                    }
                )
        return res
    if "(none)" in out and "ID" not in out:
        return []
    return parse_agent_table(out)


def running(now: float, timeout: float = 0.8) -> dict[str, Any]:
    return {"ts": now, "agents": agents(timeout)}


# ------------------------------------------------------------------------------------------------ compactions


def project_dir() -> Path:
    env = os.environ.get("ASSISTANT_PROJECT_DIR", "")
    if env:
        return paths.guard(env)
    ws = os.fspath(paths.home() / "workspace")
    return paths.home() / ".claude" / "projects" / ws.replace("/", "-")


def _main_transcript(d: Path) -> Path | None:
    best: tuple[float, Path] | None = None
    try:
        for p in d.iterdir():
            if p.suffix != ".jsonl":
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            if p.is_file() and (best is None or st.st_mtime > best[0]):
                best = (st.st_mtime, p)
    except OSError:
        return None
    return best[1] if best else None


def _iso(ts: str) -> float | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def scan_compactions(con: sqlite3.Connection, now: float, max_bytes: int = 1 << 30) -> dict[str, Any]:
    """Fold new compact_boundary records into meta 'compactions'. Returns {"file", "read", "found"}."""
    f = _main_transcript(project_dir())
    if f is None:
        return {"file": None, "read": 0, "found": 0}
    st = db.meta_get(con, "compact_scan", {}) or {}
    raw = st.get("offsets") if isinstance(st, dict) else None
    offsets: dict[str, int] = dict(raw) if isinstance(raw, dict) else {}
    off = int(offsets.get(f.name) or 0)
    size = f.stat().st_size
    if off > size:
        off = 0
    found: list[dict[str, Any]] = []
    read = 0
    with open(f, "rb") as fh:
        fh.seek(off)
        tail = b""
        while read < max_bytes:
            chunk = fh.read(CHUNK)
            if not chunk:
                break
            read += len(chunk)
            buf = tail + chunk
            cut = buf.rfind(b"\n")
            if cut < 0:
                tail = buf
                continue
            body, tail = buf[: cut + 1], buf[cut + 1 :]
            if b'"compact_boundary"' in body:
                for line in body.split(b"\n"):
                    if b'"compact_boundary"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    if d.get("type") != "system" or d.get("subtype") != "compact_boundary":
                        continue
                    cm = d.get("compactMetadata") or {}
                    ts = _iso(d.get("timestamp") or "")
                    if ts is None:
                        continue
                    found.append(
                        {
                            "ts": ts,
                            "trigger": cm.get("trigger"),
                            "pre": cm.get("preTokens"),
                            "post": cm.get("postTokens"),
                            "ms": cm.get("durationMs"),
                        }
                    )
        new_off = off + read - len(tail)
    with db.tx(con):
        lst = db.meta_get(con, "compactions", []) or []
        seen = {round(c.get("ts", 0), 3) for c in lst}
        lst += [c for c in found if round(c["ts"], 3) not in seen]
        lst.sort(key=lambda c: c["ts"])
        db.meta_set(con, "compactions", lst[-KEEP_COMPACTIONS:])
        offsets[f.name] = new_off
        keep = dict(sorted(offsets.items(), key=lambda kv: kv[0] == f.name)[-5:])
        db.meta_set(con, "compact_scan", {"file": f.name, "offsets": keep, "ts": now})
    return {"file": f.name, "read": read, "found": len(found)}


def last_compaction(con: sqlite3.Connection) -> dict[str, Any] | None:
    lst = db.meta_get(con, "compactions", []) or []
    return lst[-1] if lst else None


def reflex_db() -> Path:
    """Same path as loop.common.reflex_db() (LOOP_STATE overrides the state dir there and here)."""
    v = os.environ.get("LOOP_STATE", "")
    return paths.guard(v) / "reflex.db" if v else paths.guard(paths.home() / ".local" / "state" / "loop" / "reflex.db")


def reflex_counts(since: float | None, now: float | None = None) -> dict[str, Any] | None:
    p = reflex_db()
    if not p.is_file():
        return None
    lo = float(since) if since else (now or clock.now()) - 86400  # the clock seam: tests pin ASSISTANT_NOW
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=0.5)
        try:
            act = con.execute("SELECT COUNT(*) FROM reflexes WHERE status = 'active'").fetchone()[0]
            by = dict(
                con.execute(
                    "SELECT name, COUNT(*) FROM firings WHERE ts > ? GROUP BY name ORDER BY 2 DESC, 1", (lo,)
                ).fetchall()
            )
            mis = con.execute("SELECT COUNT(*) FROM firings WHERE ts > ? AND outcome = 'misfire'", (lo,)).fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error:
        return None
    if not act and not by:
        return None
    return {"active": int(act), "fired": int(sum(by.values())), "by_name": by, "misfires": int(mis)}
