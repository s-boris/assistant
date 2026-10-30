"""core.ciblock: shared CI blockers — what stops EVERY land, not one branch's own red. Stdlib only.

A shared blocker is a suite red on main ("fail-fast: X red on main"), the hub runner refusing a submit (e.g. a bundle
over its cap) or the runner giving no signed record for reasons outside the branch (lost job, error, unsigned). The
land path (ops/assistant.py) reports each one here the moment it sees it; the first sighting, and again after
DEDUPE_S while it is still seen, says "emit" (the caller raises one ci.blocked agenda event, ~/inbox/ci-blocked-*.md).

  key_for(kind, detail) -> str          stable key: "suite:<suite>", "runner-submit:<why, numbers as #>", "runner:..."
  one_line(text, limit=240) -> str      one line, $HOME-relative, secret-looking values masked, cut to limit
  seen(key, branch, text, now=None) -> {"key", "emit", "new", "first_seen", "count"}
  clear(keys=None, prefix=None, now=None, why="") -> [cleared keys]   (keys None and prefix None: all)
  current() -> [entries]                the blockers not cleared yet, oldest first
  stats(hours=24, now=None) -> {"n", "secs", "minutes", "longest": {"key", "secs"} | None, "open"}
                                        from the log: blockers active in the window, the union of their blocked
                                        time (clipped to the window; an uncleared one counts until now) and the
                                        longest single one
  summary_line(st) -> "CI blocked: N blockers, M min total (longest: X)"

State: ~/.local/state/assistant/ci-blockers.json (current, 0600) and ci-blockers.jsonl (one line per first-seen and
per cleared: {"ts", "op": "seen"|"cleared", "key", ...}; the land log deploys.jsonl sits beside them). One lock
(ci-blockers.lock) serialises concurrent lands. seen/clear raise on I/O errors: callers wrap them (a blocker alert
must never fail or slow a land).
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from . import clock, jsonstate, locks, notes, paths

__all__ = ["DEDUPE_S", "clear", "current", "key_for", "one_line", "seen", "since", "stats", "summary_line"]

DEDUPE_S = 2 * 3600  # a blocker still seen re-raises its event at most this often
STATE = "ci-blockers.json"
LOG = "ci-blockers.jsonl"
LOCK = "ci-blockers.lock"

_SECRETISH = re.compile(
    r"(?i)\b(token|secret|passw(?:or)?d|bearer|authorization|api[_-]?key|cookie|session)\b(\s*[=:]\s*|\s+)\S+"
)
_LONG = re.compile(r"[A-Za-z0-9_\-+/]{32,}={0,2}")
_HEX = re.compile(r"[0-9a-f]+")


def _mask_long(m: re.Match[str]) -> str:
    w = m.group(0)
    return w if _HEX.fullmatch(w) or "/" in w.strip("/") and not re.search(r"\d", w) else "***"


def one_line(text: Any, limit: int = 240) -> str:
    s = " ".join(str(text or "").split())
    s = notes.home_rel(s)
    s = _SECRETISH.sub(lambda m: f"{m.group(1)}=***", s)
    s = _LONG.sub(_mask_long, s)  # long token-like strings; lowercase hex ids (a tree, a commit) stay
    return s if len(s) <= limit else s[: limit - 1] + "…"


def key_for(kind: str, detail: Any) -> str:
    d = one_line(detail, 400).lower()
    d = re.sub(r"\d+(?:\.\d+)?", "#", d)  # "5 MB (limit 5 MB)" and "6 MB (limit 5 MB)" are the same blocker
    d = re.sub(r"[^a-z0-9#:()., _-]+", " ", d)
    d = re.sub(r"\s+", " ", d).strip()[:80]
    return f"{kind}:{d}" if d else kind


def _p(name: str):
    return paths.state(name)


def _load() -> dict[str, dict[str, Any]]:
    d = jsonstate.load(_p(STATE), {})
    return {k: v for k, v in d.items() if isinstance(v, dict)} if isinstance(d, dict) else {}


def _save(d: dict[str, dict[str, Any]]) -> None:
    jsonstate.save(_p(STATE), d, newline=True, mode=0o600, indent=1, sort_keys=True)


def _log(rec: dict[str, Any]) -> None:
    p = _p(LOG)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, sort_keys=True, ensure_ascii=False) + "\n")


def seen(key: str, branch: str, text: str, now: float | None = None) -> dict[str, Any]:
    now = clock.now() if now is None else float(now)
    line = one_line(text)
    with locks.hold(_p(LOCK), wait=30):
        d = _load()
        e = d.get(key)
        new = e is None
        if new:
            e = {"key": key, "first_seen": round(now, 3), "last_emit": 0.0, "count": 0}
            _log({"ts": round(now, 3), "op": "seen", "key": key, "branch": one_line(branch, 80), "text": line})
        e.update(last_seen=round(now, 3), branch=one_line(branch, 80), text=line, count=int(e.get("count") or 0) + 1)
        emit = now - float(e.get("last_emit") or 0) >= DEDUPE_S
        if emit:
            e["last_emit"] = round(now, 3)
        d[key] = e
        _save(d)
    return {"key": key, "emit": emit, "new": new, "first_seen": e["first_seen"], "count": e["count"]}


def clear(keys=None, prefix: str | None = None, now: float | None = None, why: str = "") -> list[str]:
    now = clock.now() if now is None else float(now)
    with locks.hold(_p(LOCK), wait=30):
        d = _load()
        if keys is None and prefix is None:
            gone = list(d)
        else:
            want = set(keys or ())
            gone = [k for k in d if k in want or (prefix is not None and k.startswith(prefix))]
        if not gone:
            return []
        for k in gone:
            e = d.pop(k)
            first = float(e.get("first_seen") or now)
            _log({"ts": round(now, 3), "op": "cleared", "key": k, "first_seen": first,
                  "secs": round(max(0.0, now - first), 1), "why": one_line(why, 120)})  # fmt: skip
        _save(d)
    return sorted(gone)


def current() -> list[dict[str, Any]]:
    return sorted(_load().values(), key=lambda e: float(e.get("first_seen") or 0))


def _intervals(now: float) -> list[tuple[str, float, float]]:
    """(key, start, end) per blocker occurrence from the log; an occurrence with no cleared line ends now."""
    try:
        lines = _p(LOG).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    open_: dict[str, float] = {}
    out = []
    for ln in lines:
        try:
            r = json.loads(ln)
            ts, key, op = float(r["ts"]), str(r["key"]), r.get("op")
        except (ValueError, KeyError, TypeError):
            continue
        if op == "seen":
            open_.setdefault(key, ts)
        elif op == "cleared":
            out.append((key, float(r.get("first_seen") or open_.get(key, ts)), ts))
            open_.pop(key, None)
    out += [(k, s, now) for k, s in open_.items()]
    return out


def stats(hours: float = 24, now: float | None = None) -> dict[str, Any]:
    now = clock.now() if now is None else float(now)
    lo = now - hours * 3600
    clipped = [(k, max(s, lo), min(e, now)) for k, s, e in _intervals(now) if e > lo and s < now]
    clipped = [x for x in clipped if x[2] >= x[1]]
    total, cur_s, cur_e = 0.0, 0.0, -1.0  # the union: two blockers at once count once
    for _k, s, e in sorted(clipped, key=lambda x: x[1]):
        if s > cur_e:
            total += max(0.0, cur_e - cur_s)
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    total += max(0.0, cur_e - cur_s)
    longest = max(clipped, key=lambda x: x[2] - x[1], default=None)
    return {
        "hours": hours,
        "n": len({k for k, _s, _e in clipped}),
        "secs": round(total, 1),
        "minutes": round(total / 60),
        "longest": {"key": longest[0], "secs": round(longest[2] - longest[1], 1)} if longest else None,
        "open": len(current()),
    }


def _dur(secs: float) -> str:
    m = round(secs / 60)
    return f"{m} min" if m < 120 else f"{m // 60} h {m % 60:02d} min"


def summary_line(st: dict[str, Any]) -> str:
    lg = st.get("longest")
    tail = f"{lg['key']}, {_dur(lg['secs'])}" if lg else "none"
    return f"CI blocked: {st.get('n', 0)} blockers, {st.get('minutes', 0)} min total (longest: {tail})"


def since(ts: float) -> str:
    return time.strftime("%d.%m %H:%M", time.localtime(float(ts or 0)))
