"""agent_titles: owner-readable titles for subagents, shared by the status board (tg-status) and agent-watch.

A subagent shows its spawn description unless it was renamed: one JSON file maps an agent id to a short title,
and both tools show title(aid, description). Set or clear one with `tg-status title ID|PREFIX ["Short title"]`.

  FILE                 ~/.local/state/agent-titles/titles.json (AGENT_TITLES_FILE=<path> elsewhere: tests)
                       {"<agent id>": {"title": "Disk check", "t": <epoch when set>}}
  load()               {agent id: title}; {} when the file is missing or broken (never raises)
  title(aid, fallback) the override, else fallback
  clean(text)          one line (whitespace collapsed, control characters dropped), at most MAX chars ("…" when cut)
  put(aid, text)       set (text) or clear (empty text) under the file's lock; entries older than KEEP_DAYS go;
                       returns the stored title, or None when cleared
Stdlib + core (jsonstate: atomic 0600 writes; locks: flock with a timeout).
"""

import os
import re
import time

from core import jsonstate, locks

MAX = 40
KEEP_DAYS = 14
ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def path() -> str:
    return os.environ.get("AGENT_TITLES_FILE") or os.path.join(
        os.path.expanduser("~"), ".local/state/agent-titles/titles.json"
    )


def load() -> dict:
    data = jsonstate.load(path(), {})
    if not isinstance(data, dict):
        return {}
    out = {}
    for aid, v in data.items():
        t = v.get("title") if isinstance(v, dict) else None
        if isinstance(t, str) and t.strip():
            out[str(aid)] = t
    return out


def title(aid, fallback: str, titles: dict | None = None) -> str:
    t = (load() if titles is None else titles).get(str(aid)) if aid else None
    return t or fallback


def clean(text: str) -> str:
    t = re.sub(r"[\x00-\x1f\x7f\u2028\u2029]+", " ", str(text or ""))
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > MAX:
        t = t[: MAX - 1].rstrip() + "…"
    return t


def _fresh(v, now: float) -> bool:
    try:
        return isinstance(v, dict) and now - float(v.get("t") or 0) < KEEP_DAYS * 86400
    except (TypeError, ValueError):
        return False


def put(aid: str, text: str, now: float | None = None) -> str | None:
    if not ID_RE.fullmatch(aid or ""):
        raise ValueError(f"not an agent id: {aid!r}")
    now = time.time() if now is None else now
    t = clean(text)
    p = path()
    with locks.hold(p + ".lock", wait=10.0, dir_mode=0o700) as fd:
        if fd is None:
            raise OSError(f"{p} is locked, try again")
        data = jsonstate.load(p, {})
        if not isinstance(data, dict):
            data = {}
        data = {k: v for k, v in data.items() if k != aid and _fresh(v, now)}
        if t:
            data[aid] = {"title": t, "t": round(now, 1)}
        jsonstate.save(p, data, mode=0o600, dir_mode=0o700, indent=1, ensure_ascii=False, sort_keys=True)
    return t or None
