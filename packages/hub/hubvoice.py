"""hubvoice: the voice relay's side of the hub. hubd imports it next to hubtg.

GET  /voice/hold (brain, voice) · POST /voice/hold {held, why} (brain, live only) · POST /voice/hold/seen
     {state, release, why} (voice): the brain's emergency stop for claude-hub-voice (`hub voice stop|start|status`),
     a flag in STATE/voice-hold.json (no store needed; unreadable = held). voiceloader reports on /seen every poll,
     stops the relay while held and starts none.
POST /voice/event {kind, subject, payload, idem_key, rid}   (voice) the relay's hand-offs and call results as
     ledger rows: kind voice.handoff | voice.result | voice.recap | voice.decisions | voice.file | voice.alert,
     source 'voice-adapter', status new. Never his speech beyond what a hand-off carries; text is nulled with the
     Telegram rows (prune).
POST /voice/poll {after, wait ≤ 25, limit ≤ 100}             (brain) voice.* rows after id `after` (long poll):
     the brain-side voice inbox delivers hand-offs to the live session.
POST /voice/ack {ids}                                        (brain) rows handled (status acked).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Any

hs: Any = None  # hubstore; the hold needs no store (tests), hubd imports this only with one
try:
    import hubstore as _hubstore

    hs = _hubstore
except ImportError:
    pass

HOLD_FILE = "voice-hold.json"
LOADER_STATES = ("starting", "running", "down", "idle", "held")
LOADER_RID = re.compile(r"^[0-9a-f]{7,40}$")
KINDS = ("voice.handoff", "voice.result", "voice.recap", "voice.decisions", "voice.file", "voice.alert")
PAYLOAD_MAX = 16 << 10
LOADER: dict[str, Any] = {}  # voiceloader's last report (memory only)
_WAKE: asyncio.Event | None = None


def _state_dir() -> str:
    return os.environ.get("HUB_STATE") or os.path.dirname(os.environ.get("HUB_LOADER_STATE", "")) or "/var/lib/hub"


def _text(v: Any, n: int = 200) -> str:
    return re.sub(r"[\x00-\x1f\x7f]", " ", v if isinstance(v, str) else "")[:n]


def hold() -> dict[str, Any]:
    """{held, why, ts, by}. No file = not held; an unreadable or malformed file holds (the stop fails closed)."""
    try:
        with open(os.path.join(_state_dir(), HOLD_FILE), encoding="utf-8") as f:
            d = json.load(f)
    except FileNotFoundError:
        return {"held": False, "why": "", "ts": None, "by": None}
    except (OSError, ValueError):
        return {"held": True, "why": "hold file unreadable", "ts": None, "by": None}
    if not isinstance(d, dict) or not isinstance(d.get("held"), bool):
        return {"held": True, "why": "hold file malformed", "ts": None, "by": None}
    ts = d.get("ts")
    ok_ts = isinstance(ts, (int, float)) and not isinstance(ts, bool)
    return {"held": d["held"], "why": _text(d.get("why")), "ts": ts if ok_ts else None, "by": _text(d.get("by"), 16)}


def _hold_write(held: bool, why: str, who: str) -> dict[str, Any]:
    d = {"held": held, "why": why, "ts": round(time.time(), 3), "by": who}
    os.makedirs(_state_dir(), exist_ok=True)
    p = os.path.join(_state_dir(), HOLD_FILE)
    tmp = f"{p}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)
    return d


def loader_seen() -> dict[str, Any] | None:
    return {**LOADER, "age_s": round(time.time() - LOADER["ts"], 1)} if LOADER else None


def hold_api(method: str, path: str, who: str, schema: str, body: bytes) -> tuple[int, Any]:
    if who not in ("brain", "voice"):
        return 403, {"error": "forbidden", "hint": f"{path} is not the {who} caller's"}
    if path == "/voice/hold" and method == "GET":
        return 200, {**hold(), "loader": loader_seen()}
    if method != "POST" or path not in ("/voice/hold", "/voice/hold/seen"):
        return 404, {"error": "not_found"}
    try:
        a = json.loads(body or b"{}")
    except ValueError:
        return 400, {"error": "bad_json"}
    if not isinstance(a, dict):
        return 400, {"error": "bad_json", "hint": "a JSON object"}
    if path == "/voice/hold":
        if who != "brain":
            return 403, {"error": "forbidden", "hint": f"{path} is not the {who} caller's"}
        if schema:
            return 400, {"error": "bad_request", "hint": "the voice hold is live only (no X-Hub-Schema)"}
        held = a.get("held")
        if not isinstance(held, bool):
            return 400, {"error": "bad_request", "hint": "held: true or false"}
        before = hold()
        try:
            d = _hold_write(held, _text(a.get("why")), who)
        except OSError as e:
            return 503, {"error": "state_unwritable", "hint": type(e).__name__}
        print(json.dumps({"event": "voice_hold", "held": held, "was": before["held"], "caller": who}), flush=True)
        return 200, {**d, "changed": before["held"] != held, "loader": loader_seen()}
    if who != "voice":
        return 403, {"error": "forbidden", "hint": f"{path} is not the {who} caller's"}
    st, rel = a.get("state"), a.get("release")
    if st not in LOADER_STATES:
        return 400, {"error": "bad_request", "hint": f"state: one of {', '.join(LOADER_STATES)}"}
    if rel is not None and not (isinstance(rel, str) and LOADER_RID.match(rel)):
        return 400, {"error": "bad_request", "hint": "release: a release id or null"}
    if LOADER.get("state") != st or LOADER.get("release") != rel:
        print(json.dumps({"event": "voice_loader", "state": st, "release": rel}), flush=True)
    LOADER.update(state=st, release=rel, why=_text(a.get("why")), ts=round(time.time(), 3))
    return 200, hold()


def brief() -> dict[str, Any]:
    return {"held": hold()["held"], "loader": LOADER.get("state")}


# ------------------------------------------------------------------------------------------------ events


def _wake() -> asyncio.Event:
    global _WAKE
    if _WAKE is None:
        _WAKE = asyncio.Event()
    return _WAKE


def _fire() -> None:
    global _WAKE
    ev, _WAKE = _wake(), asyncio.Event()
    ev.set()


async def r_event(h: Any, who: str, s: str, a: dict, q: dict) -> Any:
    kind = a.get("kind")
    if kind not in KINDS:
        raise hs.BadRequest(f"kind: one of {', '.join(KINDS)}")
    payload = a.get("payload") if a.get("payload") is not None else {}
    if not isinstance(payload, dict):
        raise hs.BadRequest("payload: an object")
    ptext = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(ptext) > PAYLOAD_MAX:
        raise hs.BadRequest(f"payload: at most {PAYLOAD_MAX} bytes")
    idem = a.get("idem_key")
    if not isinstance(idem, str) or not 1 <= len(idem) <= 300:
        raise hs.BadRequest("idem_key: 1-300 characters")
    row = (time.time(), kind, _text(a.get("subject"), 500), ptext, "now", "voice:" + idem)

    async def do() -> dict[str, Any]:
        r = await hs.one(
            c,
            "INSERT INTO events(ts, source, kind, subject, next, payload, labels, delivery, idem_key, status)"
            " VALUES (%s, 'voice-adapter', %s, %s, '', %s, '[\"owner_text\"]', %s, %s, 'new')"
            " ON CONFLICT (idem_key) DO NOTHING RETURNING id",
            row,
        )
        return {"id": r["id"] if r else None}

    async with h.store.tx(s) as c:
        res = await hs.once(c, a.get("rid"), who, "voice.event", do)
    _fire()
    return res


async def _after(h: Any, s: str, after: int, limit: int) -> list[dict[str, Any]]:
    async with h.store.tx(s) as c:
        return await hs.rows(
            c,
            "SELECT id, ts, kind, subject, payload, status FROM events WHERE source = 'voice-adapter' AND id > %s"
            " AND status = 'new' ORDER BY id LIMIT %s",
            (after, limit),
        )


async def r_poll(h: Any, who: str, s: str, a: dict, q: dict) -> Any:
    after = a.get("after", 0)
    if not isinstance(after, int) or isinstance(after, bool) or after < 0:
        raise hs.BadRequest("after: an event id (0 = from the start)")
    wait = a.get("wait", 0)
    if not isinstance(wait, (int, float)) or isinstance(wait, bool) or not 0 <= wait <= 25:
        raise hs.BadRequest("wait: 0-25 seconds")
    limit = a.get("limit", 100)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise hs.BadRequest("limit: 1-100")
    end = time.monotonic() + float(wait)
    while True:
        ev = _wake()
        got = await _after(h, s, after, limit)
        left = end - time.monotonic()
        if got or left <= 0:
            for r in got:
                try:
                    r["payload"] = json.loads(r["payload"] or "{}")
                except ValueError:
                    r["payload"] = {}
            return {"rows": got}
        try:
            await asyncio.wait_for(ev.wait(), left)
        except TimeoutError:
            pass


async def r_ack(h: Any, who: str, s: str, a: dict, q: dict) -> Any:
    ids = a.get("ids")
    if not isinstance(ids, list) or not ids or len(ids) > 500 or not all(isinstance(i, int) for i in ids):
        raise hs.BadRequest("ids: 1-500 event ids")
    async with h.store.tx(s) as c:
        n = await hs.run(
            c, "UPDATE events SET status = 'acked' WHERE source = 'voice-adapter' AND id = ANY(%s) AND status = 'new'",
            (ids,),
        )  # fmt: skip
    return {"n": n}


def routes(brain: frozenset[str], voice: frozenset[str]) -> list[tuple[str, str, Any, frozenset[str]]]:
    return [
        ("POST", r"/voice/event", r_event, voice),
        ("POST", r"/voice/poll", r_poll, brain),
        ("POST", r"/voice/ack", r_ack, brain),
    ]
