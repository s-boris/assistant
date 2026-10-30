"""hubbrowser: claude-hub-browser's side of the hub. hubd imports it next to hubphone.

The browser container's service (browsersvc in claude-hub-browser) checks a committing click's token itself, with its
own caller token (token_browser, caller `browser`). It gets exactly two routes, nothing else of the hub:

GET  /browser/approval/<nonce>      (browser) the fields of one approval the token check reads: status, the effective
                                    state, expires, used_ts, used_by, action_sha. Never the summary, card or text.
POST /browser/commit {task, stop, nonce, rid}
                                    (browser) the single-use receipt of an approved commit: one ledger row
                                    `webtask.commit` (source browsersvc, delivery mute, idem key commit:<nonce>).
                                    -> {"id": N, "dup": false} for the first claim, {"id": null, "dup": true} after.
The brain keeps its own path (core.events.emit, the approvals library); this module only adds the container's.
"""

from __future__ import annotations

import re
import time
from typing import Any

hs: Any = None
try:
    import hubstore as _hubstore

    hs = _hubstore
except ImportError:  # tests import the routes table without a store
    pass

NONCE = r"([0-9a-f]{32})"
TASK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
FIELDS = ("status", "expires", "used_ts", "used_by", "action_sha")


class NotFound(Exception):
    pass


def effective(row: dict[str, Any], now: float | None = None) -> str:
    """The approvals library's state(): used once consumed; a pending row at or past its expiry is expired."""
    if row.get("used_ts") is not None:
        return "used"
    if row.get("status") == "pending" and (now if now is not None else time.time()) >= float(row.get("expires") or 0):
        return "expired"
    return str(row.get("status"))


def approval_view(row: dict[str, Any]) -> dict[str, Any]:
    out = {k: row.get(k) for k in FIELDS}
    out["state"] = effective(row)
    return out


def commit_event(a: dict[str, Any]) -> dict[str, Any]:
    """The receipt as hubstore.emit's request (fixed kind, source, delivery and idem key; validated input), else
    ValueError. The same row the brain-side service writes through core.events.emit."""
    task, nonce, stop = a.get("task"), a.get("nonce"), a.get("stop")
    if not isinstance(task, str) or not TASK.match(task):
        raise ValueError("task: a web task id")
    if not isinstance(nonce, str) or not re.fullmatch(NONCE, nonce):
        raise ValueError("nonce: 32 hex characters")
    if stop is not None and (not isinstance(stop, int) or isinstance(stop, bool) or not 0 <= stop < 1000):
        raise ValueError("stop: a small integer or null")
    return {"kind": "webtask.commit", "source": "browsersvc", "delivery": "mute",
            "subject": f"web task {task}: approved commit passed the browser service",
            "payload": {"task": task, "stop": stop, "by": "browsersvc", "where": "claude-hub-browser"},
            "idem_key": f"commit:{nonce}"}  # fmt: skip


async def r_approval(h: Any, who: str, s: str, a: dict, q: dict, nonce: str) -> Any:
    async with h.store.tx(s) as c:
        r = await hs.approval_get(c, nonce)
    if r is None:
        raise NotFound("no such approval")
    return approval_view(r)


async def r_commit(h: Any, who: str, s: str, a: dict, q: dict) -> Any:
    try:
        ev = commit_event(a)
    except ValueError as e:
        raise hs.BadRequest(str(e)) from None
    async with h.store.tx(s) as c:
        return await hs.once(c, a.get("rid"), who, "browser.commit", lambda: hs.emit(c, ev))


def routes(browser: frozenset[str]) -> list[tuple[str, str, Any, frozenset[str]]]:
    return [
        ("GET", rf"/browser/approval/{NONCE}", r_approval, browser),
        ("POST", r"/browser/commit", r_commit, browser),
    ]
