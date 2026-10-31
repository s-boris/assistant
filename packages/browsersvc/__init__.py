"""browsersvc: the browser service. One process in front of Chrome's CDP: the browser MCP, abx and securefill reach
the headed Chrome through it, and a committing action needs a hub approval token. Stdlib only.

What it does: a CDP front on 127.0.0.1:PORT (9223) for Chrome's CDP on UPSTREAM (127.0.0.1:9222). HTTP /json/* is
passed through with the WebSocket URLs rewritten to the front; every WebSocket (browser or page target) is relayed
frame by frame. Client commands are watched for the two ways a page action commits:
  - Input.dispatchMouseEvent mousePressed (left): the element under the point (DOM.getNodeForLocation, then its
    accessible role + name from Accessibility.getPartialAXTree; no page script runs) is classified with the web
    agent's commit classifier;
  - Input.dispatchKeyEvent Enter (keyDown/rawKeyDown): the focused element, the same way.
The mouseReleased / char that belong to a decided press follow its decision. Everything else passes unchanged.

Decision (Gate.decide), the web agent's rules (webagent.guard) plus the token:
  not a commit, a consent/search exemption, or a soft commit while no web task is active -> allow;
  a booking/order task's intermediate soft step (send/confirm/settings while every active task is a running
  booking/order task) -> allow; a terms checkbox during a task -> deny (his ✅ covers --tick boxes; securefill ticks);
  Enter in a text field during a task -> deny;
  any other commit -> the approved (or executing) web task whose pending label equals its name, and that task's TOKEN:
  his hub-held approval for exactly this stop (state used, used by webagent:<task>, the sha256 of the task's
  canonical action, used within APPROVED_WINDOW) plus a single-use receipt on the hub (ledger row webtask.commit,
  idem key commit:<nonce>: the first claim wins, a second is refused). No match or no token -> deny.
A dry-run task (`webagent start --dry`) on a dry host passes with its simulated approval and no hub row.
An element the service cannot resolve (a cross-origin frame, a detached node) is allowed and logged "unresolved":
webagent-guard and abx's own gate stay the backstop, as does securefill's redaction.

Modes (Gate's mode_fn; the container's comes from its live.json, the default reads the file SWITCH =
~/.config/assistant/browsersvc holding one word; absent = off):
  off      the gate is not consulted.
  shadow   the service decides, but forwards everything; a deny is logged as would-deny.
  enforce  a deny answers the command with a CDP error (-32000 "browsersvc: ...") and the click never reaches Chrome.

Log: STATE/svc.log (JSON lines: time, mode, kind, decision, why, tier, category, task id, token outcome; never a
label, URL or page text). audit() counts committing actions that passed without a consumed token (done-when: 0).
Env (tests): BROWSERSVC_SWITCH, BROWSERSVC_STATE, BROWSERSVC_PORT, BROWSERSVC_UPSTREAM (host:port).

Chrome runs in its own container (claude-hub-browser):
  - route() names the container's service (HUB_CDP, http://claude-hub-browser:9223) for every client; ok only while
    it answers (fail closed: nothing reaches Chrome around the gate).
  - In the container the Service runs with BROWSERSVC_BIND (0.0.0.0: the brain and hub networks; Chrome's own
    CDP stays on the container's loopback), BROWSERSVC_ADVERTISE (the host:port written into /json answers, e.g.
    claude-hub-browser:9223) and BROWSERSVC_SEAL=1: the CDP cookie dumps (SEALED_METHODS) are refused in every mode
    and cookie headers and cookie lists are cut from the network events (COOKIE_EVENTS), so his logins can be used by
    driving the browser but not copied out of it.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import re
import secrets
import struct
import time
from collections.abc import Callable
from typing import Any

MODES = ("off", "shadow", "enforce")
PORT = 9223
UPSTREAM = "127.0.0.1:9222"
APPROVED_WINDOW = 600 + 120  # webagent's approved window (10 min) + slack: the click comes after his ✅ was used
QUERY_TIMEOUT = 3.0
INJECT_BASE = 1_900_000_000  # ids of the service's own CDP queries (clients count up from 1)
MAX_MESSAGE = 256 * 1024 * 1024
HUB_CDP = "http://claude-hub-browser:9223"
SEALED_METHODS = frozenset({"Network.getAllCookies", "Network.getCookies", "Storage.getCookies"})
COOKIE_EVENTS = (b'"Network.requestWillBeSentExtraInfo"', b'"Network.responseReceivedExtraInfo"',
                 b'"Network.responseReceived"', b'"Network.requestWillBeSent"')  # fmt: skip
COOKIE_KEYS = ("associatedCookies", "blockedCookies", "exemptedCookies", "cookiePartitionKey")
COOKIE_HEADERS = ("cookie", "set-cookie", "cookie2", "set-cookie2")
DENY_QUIET = 10.0  # the same denial on the same session within this many seconds is logged once
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
CLICKABLE = ("button", "link", "menuitem", "menuitemcheckbox", "menuitemradio", "tab", "option", "checkbox", "radio",
             "switch", "combobox", "textbox", "searchbox", "spinbutton", "listbox", "treeitem", "gridcell")  # fmt: skip
TEXTISH = ("textbox", "searchbox", "combobox", "spinbutton")
ENTER_KEYS = ("Enter", "NumpadEnter")


# ------------------------------------------------------------------------------------------------ paths and the switch


def _home() -> str:
    return os.path.expanduser("~")


def switch_path() -> str:
    return os.environ.get("BROWSERSVC_SWITCH") or os.path.join(_home(), ".config", "assistant", "browsersvc")


def state_dir() -> str:
    return os.environ.get("BROWSERSVC_STATE") or os.path.join(_home(), ".local", "state", "assistant", "browsersvc")


def port() -> int:
    return int(os.environ.get("BROWSERSVC_PORT") or PORT)


def upstream() -> tuple[str, int]:
    h, _, p = (os.environ.get("BROWSERSVC_UPSTREAM") or UPSTREAM).rpartition(":")
    return h or "127.0.0.1", int(p)


def hub_cdp() -> str:
    return (os.environ.get("BROWSER_HUB_CDP") or HUB_CDP).rstrip("/")


def bind() -> str:
    return os.environ.get("BROWSERSVC_BIND") or "127.0.0.1"


def advertise(listen: int) -> str:
    return os.environ.get("BROWSERSVC_ADVERTISE") or f"127.0.0.1:{listen}"


def sealed() -> bool:
    return os.environ.get("BROWSERSVC_SEAL", "") == "1"


def up_url(base: str, timeout: float = 2.0) -> bool:
    from core import http  # noqa: PLC0415  (late, like up(): the container's service never calls this)

    try:
        with http.open(f"{base.rstrip('/')}/json/version", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def strip_cookies(data: bytes) -> bytes:
    """A network event without its cookies: cookie lists dropped, Cookie / Set-Cookie headers removed (any case)."""
    try:
        msg = json.loads(data)
    except ValueError:
        return data
    p = msg.get("params") if isinstance(msg, dict) else None
    if not isinstance(p, dict):
        return data
    hit = False
    for k in COOKIE_KEYS:
        if k in p:
            p.pop(k)
            hit = True
    for holder in (p, p.get("request"), p.get("response")):
        if not isinstance(holder, dict):
            continue
        for hk in ("headers", "requestHeaders"):
            h = holder.get(hk)
            if isinstance(h, dict):
                for name in [n for n in h if str(n).lower() in COOKIE_HEADERS]:
                    h.pop(name)
                    hit = True
        for tk in ("headersText", "requestHeadersText"):
            if isinstance(holder.get(tk), str) and "ookie" in holder[tk]:
                holder.pop(tk)
                hit = True
    return json.dumps(msg, separators=(",", ":")).encode() if hit else data


def mode() -> str:
    try:
        with open(switch_path(), encoding="utf-8") as f:
            m = f.read().strip().split()[0]
    except (OSError, IndexError):
        return "off"
    return m if m in MODES else "off"


def set_mode(m: str) -> None:
    if m not in MODES:
        raise ValueError(f"mode: one of {', '.join(MODES)}")
    p = switch_path()
    if m == "off":
        with contextlib.suppress(FileNotFoundError):
            os.unlink(p)
        return
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = f"{p}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(m + "\n")
    os.replace(tmp, p)


def up(p: int | None = None, timeout: float = 1.0) -> bool:
    from core import http  # noqa: PLC0415  (late: abx and securefill import this module on every call)

    try:
        with http.open(f"http://127.0.0.1:{p or port()}/json/version", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def route(check: Callable[[], bool] | None = None) -> dict[str, Any]:
    """The CDP endpoint a client (abx, securefill, the browser MCP's config) uses: the container's service. ok False =
    it does not answer: the client must refuse (nothing reaches Chrome around the gate)."""
    hc = hub_cdp()
    alive = (check or (lambda: up_url(hc)))()
    return {"mode": "hub", "cdp": hc, "via": "hub" if alive else "hub (down)",
            "port": int(hc.rpartition(":")[2] or 9223), "ok": alive}  # fmt: skip


# ------------------------------------------------------------------------------------------------ the log


def log(rec: dict[str, Any]) -> None:
    try:
        d = state_dir()
        os.makedirs(d, mode=0o700, exist_ok=True)
        rec = {"ts": round(time.time(), 1), **rec}
        with open(os.path.join(d, "svc.log"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def records(hours: float = 24.0) -> list[dict[str, Any]]:
    since = time.time() - hours * 3600
    out = []
    try:
        with open(os.path.join(state_dir(), "svc.log"), encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and (r.get("ts") or 0) >= since:
                    out.append(r)
    except OSError:
        pass
    return out


def audit(hours: float = 24.0) -> dict[str, Any]:
    """Counts over the log. unguarded = committing actions that reached Chrome without a consumed token (enforce: must
    stay 0); would_deny = shadow-mode denials (what enforce would have stopped)."""
    rs = [r for r in records(hours) if r.get("kind") in ("click", "enter")]
    commits = [r for r in rs if r.get("tier")]
    passed = [r for r in commits if r.get("decision") in ("allow", "would-deny")]
    tokened = [r for r in passed if r.get("token") in ("ok", "dry")]
    exempt = [r for r in passed if r.get("why") in ("auto_soft", "soft, no task")]
    return {
        "hours": hours,
        "decisions": len(rs),
        "commits": len(commits),
        "with_token": len(tokened),
        "exempt": len(exempt),
        "unguarded": len([r for r in passed if r not in tokened and r not in exempt]),
        "denied": len([r for r in rs if r.get("decision") == "deny"]),
        "would_deny": len([r for r in rs if r.get("decision") == "would-deny"]),
        "unresolved": len([r for r in rs if r.get("why") == "unresolved"]),
    }


# ------------------------------------------------------------------------------------------------ WebSocket frames


def _xor(data: bytes, key: bytes) -> bytes:
    if not data:
        return data
    n = len(data)
    k = (key * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(k, "big")).to_bytes(n, "big")


def frame(opcode: int, payload: bytes, mask: bool) -> bytes:
    n = len(payload)
    head = bytes([0x80 | opcode])
    mbit = 0x80 if mask else 0
    if n < 126:
        head += bytes([mbit | n])
    elif n < 65536:
        head += bytes([mbit | 126]) + struct.pack("!H", n)
    else:
        head += bytes([mbit | 127]) + struct.pack("!Q", n)
    if mask:
        key = secrets.token_bytes(4)
        return head + key + _xor(payload, key)
    return head + payload


async def read_message(r: asyncio.StreamReader) -> tuple[int, bytes]:
    """One message: (opcode, payload); fragments joined; control frames come back as they are (8 close, 9 ping,
    10 pong). Raises EOFError at the end of the stream."""
    parts: list[bytes] = []
    first = 0
    total = 0
    while True:
        try:
            b0, b1 = await r.readexactly(2)
        except (asyncio.IncompleteReadError, ConnectionError):
            raise EOFError from None
        fin, op, masked, n = b0 & 0x80, b0 & 0x0F, b1 & 0x80, b1 & 0x7F
        if n == 126:
            (n,) = struct.unpack("!H", await r.readexactly(2))
        elif n == 127:
            (n,) = struct.unpack("!Q", await r.readexactly(8))
        if n > MAX_MESSAGE or total + n > MAX_MESSAGE:
            raise EOFError("message too big")
        key = await r.readexactly(4) if masked else b""
        data = await r.readexactly(n) if n else b""
        if masked:
            data = _xor(data, key)
        if op >= 8:
            return op, data
        if op:
            first = op
        parts.append(data)
        total += n
        if fin:
            return first, b"".join(parts)


async def read_head(r: asyncio.StreamReader) -> tuple[str, dict[str, str]]:
    raw = await r.readuntil(b"\r\n\r\n")
    lines = raw.decode("latin-1").split("\r\n")
    hdr = {}
    for ln in lines[1:]:
        k, sep, v = ln.partition(":")
        if sep:
            hdr[k.strip().lower()] = v.strip()
    return lines[0], hdr


def accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()  # noqa: S324 (RFC 6455)


async def ws_connect(host: str, p: int, path: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    r, w = await asyncio.open_connection(host, p, limit=2**20)
    key = base64.b64encode(secrets.token_bytes(16)).decode()
    w.write((f"GET {path} HTTP/1.1\r\nHost: {host}:{p}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
             f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())  # fmt: skip
    await w.drain()
    status, hdr = await read_head(r)
    if " 101 " not in status + " " or hdr.get("sec-websocket-accept") != accept_key(key):
        w.close()
        raise ConnectionError(f"upstream refused the WebSocket ({status[:60]})")
    return r, w


# ------------------------------------------------------------------------------------------------ the gate


class Decision:
    def __init__(self, allow: bool, why: str, c: dict[str, Any] | None = None, task: str | None = None,
                 token: str | None = None) -> None:  # fmt: skip
        self.allow, self.why, self.task, self.token = allow, why, task, token
        self.passed: dict[str, Any] | None = None  # the task whose token this commit used (its workflow is told)
        c = c or {}
        self.tier, self.cat = c.get("tier"), c.get("category")

    def rec(self) -> dict[str, Any]:
        return {"why": self.why, "tier": self.tier, "cat": self.cat, "task": self.task, "token": self.token}


class Gate:
    """Decides one click / Enter. tasks: the web agent's tasks and classifier (WebagentTasks or a fake); tokens: the
    hub approval token check (HubTokens or a fake); notify(task, op): tells the task's workflow (best effort)."""

    def __init__(self, tasks: Any, tokens: Any, mode_fn: Callable[[], str] = mode,
                 notify: Callable[[dict[str, Any], str], None] | None = None) -> None:  # fmt: skip
        self.tasks, self.tokens, self.mode_fn, self.notify = tasks, tokens, mode_fn, notify

    def decide(self, kind: str, ev: dict[str, Any]) -> Decision:
        if not ev.get("resolved"):
            return Decision(True, "unresolved")
        label, role = str(ev.get("label") or ""), str(ev.get("role") or "")
        active = self.tasks.active()
        c = self.tasks.classify(label)
        if kind == "enter" and not c.get("commit"):
            if active and role in TEXTISH and not self.tasks.search(label):
                return Decision(False, "enter-submit during a web task", c)
            return Decision(True, "not a commit", c)
        if not c.get("commit"):
            return Decision(True, "exempt: " + c["exempt"] if c.get("exempt") else "not a commit", c)
        if c.get("tier") == "soft" and not active:
            return Decision(True, "soft, no task", c)
        if active and (role == "checkbox" or self.tasks.checkbox(label)):
            return Decision(False, "terms box during a web task (--tick)", c)
        if self.tasks.auto_soft(c, active):
            return Decision(True, "auto_soft", c)
        t = self.tasks.match(label, role)
        if t is None:
            return Decision(False, "no approved web task matches", c)
        ok, why = self.tokens.take(t, self.tasks)
        d = Decision(ok, "token" if ok else why, c, task=t.get("id"),
                     token=("dry" if why == "dry" else "ok") if ok else "missing")  # fmt: skip
        d.passed = t if ok else None
        return d


class WebagentTasks:
    """The web agent's live tasks and classifier (~/bin/lib/webagent.py)."""

    def __init__(self, wa: Any = None) -> None:
        if wa is None:
            import webagent  # noqa: PLC0415  # pyright: ignore[reportMissingImports]  (~/bin/lib, live)

            wa = webagent
        self.wa = wa

    def active(self) -> list[dict[str, Any]]:
        return [t for t in self.wa.all_tasks() if self.wa.effective(t) in self.wa.ACTIVE]

    def classify(self, text: str) -> dict[str, Any]:
        return self.wa.classify(text)

    def search(self, text: str) -> bool:
        return bool(self.wa.SEARCH_RX.search(self.wa.norm(text)))

    def checkbox(self, text: str) -> bool:
        return bool(self.wa.CHECKBOX_RX.search(self.wa.norm(text)))

    def auto_soft(self, c: dict[str, Any], active: list[dict[str, Any]]) -> bool:
        return bool(
            c.get("tier") == "soft"
            and c.get("category") in self.wa.AUTO_SOFT
            and active
            and all(a.get("kind") in self.wa.SUMMARY_KINDS and self.wa.effective(a) == "running" for a in active)
        )

    def match(self, text: str, role: str = "") -> dict[str, Any] | None:
        """The approved (or executing) task whose label equals this element's accessible name (text) exactly; a
        toggle or text field (role) never matches."""
        for t in self.wa.all_tasks():
            if (
                self.wa.effective(t) in ("approved", "executing")
                and t.get("approval")
                and self.wa._matches(t, text, None, role or None, [text])  # noqa: SLF001
            ):
                return t
        return None

    def action(self, t: dict[str, Any]) -> str:
        return self.wa._action(t)  # noqa: SLF001

    def dry_ok(self, t: dict[str, Any]) -> bool:
        p = t.get("pending") or {}
        return (
            t.get("mode") == "dry"
            and (t.get("approval") or {}).get("via") == "simulated"
            and self.wa.host_of(p.get("url") or t.get("url")) in self.wa.DRY_HOSTS
        )


class HubTokens:
    """The token: his hub-held approval for exactly this stop, used once by webagent approve, plus a single-use
    receipt on the hub. approvals = the approvals library (hub mode reads the hub); emit = core.events.emit."""

    def __init__(self, approvals: Any = None, emit: Callable[..., Any] | None = None,
                 clock: Callable[[], float] = time.time) -> None:  # fmt: skip
        if approvals is None:
            import approvals as _a  # noqa: PLC0415

            approvals = _a
        if emit is None:
            from core import events  # noqa: PLC0415

            emit = events.emit
        self.A, self.emit, self.clock = approvals, emit, clock

    def take(self, t: dict[str, Any], tasks: Any) -> tuple[bool, str]:
        tid = str(t.get("id") or "")
        if t.get("mode") == "dry":
            return (True, "dry") if tasks.dry_ok(t) else (False, "dry run off its dry host")
        nonce = (t.get("approval") or {}).get("nonce") or (t.get("pending") or {}).get("nonce")
        if not nonce:
            return False, "no hub approval token (a card without ✅ buttons: stop again for a new card)"
        try:
            row = self.A.get(nonce)
        except Exception as e:  # the hub unreachable: fail closed
            return False, f"token unverified ({type(e).__name__})"
        if not row:
            return False, "no such approval"
        st = self.A.state(row)
        if st != "used" or row.get("used_by") != f"webagent:{tid}":
            return False, f"his approval was not used for this task ({st})"
        if row.get("action_sha") != self.A.action_sha(tasks.action(t)):
            return False, "another action: his approval does not cover it"
        if self.clock() - float(row.get("used_ts") or 0) > APPROVED_WINDOW:
            return False, "his approval is too old"
        try:
            subj = f"web task {tid}: approved commit passed the browser service"
            res = self.emit(kind="webtask.commit", subject=subj,
                            payload={"task": tid, "stop": t.get("stops"), "by": "browsersvc"}, delivery="mute",
                            source="browsersvc", note=False, idem_key=f"commit:{nonce}")  # fmt: skip
        except Exception as e:
            return False, f"receipt not recorded ({type(e).__name__})"
        if getattr(res, "dup", False):
            return False, "token used already"
        if getattr(res, "id", None) is None:
            return False, "receipt not recorded (hub spool)"
        return True, "ok"


# ------------------------------------------------------------------------------------------------ the proxy


class Conn:
    """One relayed WebSocket: the client side, the upstream side, and the service's own queries upstream."""

    def __init__(self, cw: asyncio.StreamWriter, uw: asyncio.StreamWriter) -> None:
        self.cw, self.uw = cw, uw
        self.cw_lock = asyncio.Lock()
        self.next_id = INJECT_BASE
        self.waiting: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.last_deny: tuple[Any, float] = (None, 0.0)
        self.held: dict[str, bool] = {}  # sessionId -> the last press/keydown decision (release/char follow it)

    async def to_client(self, op: int, data: bytes) -> None:
        async with self.cw_lock:
            self.cw.write(frame(op, data, mask=False))
            await self.cw.drain()

    async def to_upstream(self, op: int, data: bytes) -> None:
        self.uw.write(frame(op, data, mask=True))
        await self.uw.drain()

    async def query(self, method: str, params: dict[str, Any], sid: str | None) -> dict[str, Any]:
        self.next_id += 1
        qid = self.next_id
        msg: dict[str, Any] = {"id": qid, "method": method, "params": params}
        if sid:
            msg["sessionId"] = sid
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.waiting[qid] = fut
        try:
            await self.to_upstream(1, json.dumps(msg).encode())
            got = await asyncio.wait_for(fut, QUERY_TIMEOUT)
        finally:
            self.waiting.pop(qid, None)
        if "error" in got:
            raise LookupError(str((got.get("error") or {}).get("message") or "error")[:80])
        return got.get("result") or {}

    def answer(self, data: bytes) -> bool:
        """An upstream message: True when it answers one of the service's own queries (then it is not relayed)."""
        if not self.waiting:
            return False
        m = re.match(rb"\{\s*\"id\"\s*:\s*(\d{10,})", data)
        if not m:
            return False
        fut = self.waiting.get(int(m.group(1)))
        if fut is None:
            return False
        try:
            got = json.loads(data)
        except ValueError:
            got = {"error": {"message": "bad json"}}
        if not fut.done():
            fut.set_result(got if isinstance(got, dict) else {})
        return True


async def describe(conn: Conn, sid: str | None, bid: int) -> dict[str, Any]:
    """An element's role + accessible name (the nearest clickable ancestor's), from CDP alone (no page script)."""
    ax = await conn.query("Accessibility.getPartialAXTree", {"backendNodeId": bid, "fetchRelatives": True}, sid)
    nodes = ax.get("nodes") or []
    by_id = {n.get("nodeId"): n for n in nodes}
    cur = next((n for n in nodes if n.get("backendDOMNodeId") == bid), None)
    seen = 0
    best = None
    while cur is not None and seen < 12:
        role = str((cur.get("role") or {}).get("value") or "")
        name = str((cur.get("name") or {}).get("value") or "")
        if best is None and name and not cur.get("ignored"):
            best = (role, name)
        if role in CLICKABLE:
            return {"resolved": True, "role": role, "label": name[:300]}
        cur = by_id.get(cur.get("parentId"))
        seen += 1
    if best:
        return {"resolved": True, "role": best[0], "label": best[1][:300]}
    return {"resolved": False}


async def resolve_point(conn: Conn, sid: str | None, x: float, y: float) -> dict[str, Any]:
    try:
        r = await conn.query("DOM.getNodeForLocation", {"x": int(x), "y": int(y), "includeUserAgentShadowDOM": False,
                                                        "ignorePointerEventsNone": True}, sid)  # fmt: skip
        bid = r.get("backendNodeId")
        if not isinstance(bid, int):
            return {"resolved": False}
        return await describe(conn, sid, bid)
    except (LookupError, TimeoutError, ConnectionError):
        return {"resolved": False}


async def resolve_focus(conn: Conn, sid: str | None) -> dict[str, Any]:
    try:
        r = await conn.query("Runtime.evaluate", {"expression": "document.activeElement", "returnByValue": False}, sid)
        oid = (r.get("result") or {}).get("objectId")
        if not oid:
            return {"resolved": False}
        d = await conn.query("DOM.describeNode", {"objectId": oid}, sid)
        with contextlib.suppress(Exception):
            await conn.query("Runtime.releaseObject", {"objectId": oid}, sid)
        bid = (d.get("node") or {}).get("backendNodeId")
        if not isinstance(bid, int):
            return {"resolved": False}
        return await describe(conn, sid, bid)
    except (LookupError, TimeoutError, ConnectionError):
        return {"resolved": False}


def _quiet(fn: Callable[..., Any], *a: Any) -> None:
    with contextlib.suppress(Exception):
        fn(*a)


class Service:
    def __init__(self, gate: Gate, listen: int | None = None, up_addr: tuple[str, int] | None = None) -> None:
        self.gate = gate
        self.listen = listen or port()
        self.up_host, self.up_port = up_addr or upstream()
        self.server: asyncio.Server | None = None
        self.seal = sealed()

    async def start(self) -> None:
        self.server = await asyncio.start_server(self.handle, bind(), self.listen, limit=2**20)

    async def serve(self) -> None:
        await self.start()
        assert self.server is not None
        log({"kind": "service", "decision": "start", "why": f"mode {self.gate.mode_fn()}"})
        async with self.server:
            await self.server.serve_forever()

    async def handle(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            line, hdr = await read_head(r)
            parts = line.split(" ")
            if len(parts) < 2:
                return
            method, path = parts[0], parts[1]
            if hdr.get("upgrade", "").lower() == "websocket":
                await self.relay(r, w, path, hdr)
            else:
                await self.http(w, method, path)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError, EOFError, OSError):
            pass
        finally:
            with contextlib.suppress(Exception):
                w.close()

    def _rewrite(self, body: bytes) -> bytes:
        src = f"{self.up_host}:{self.up_port}".encode()
        dst = advertise(self.listen).encode()
        body = body.replace(src, dst).replace(f"localhost:{self.up_port}".encode(), dst)
        return body

    async def http(self, w: asyncio.StreamWriter, method: str, path: str) -> None:
        if not path.startswith("/json"):
            w.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await w.drain()
            return
        ur, uw = await asyncio.open_connection(self.up_host, self.up_port)
        uw.write(f"{method} {path} HTTP/1.1\r\nHost: {self.up_host}:{self.up_port}\r\nConnection: close\r\n"
                 "Content-Length: 0\r\n\r\n".encode())  # fmt: skip
        await uw.drain()
        status, hdr = await read_head(ur)
        n = hdr.get("content-length")
        body = await (ur.readexactly(int(n)) if n and n.isdigit() else ur.read())
        uw.close()
        body = self._rewrite(body)
        ctype = hdr.get("content-type", "application/json; charset=UTF-8")
        w.write(f"{status}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n"
                .encode() + body)  # fmt: skip
        await w.drain()

    async def relay(self, cr: asyncio.StreamReader, cw: asyncio.StreamWriter, path: str, hdr: dict[str, str]) -> None:
        try:
            ur, uw = await ws_connect(self.up_host, self.up_port, path)
        except (OSError, ConnectionError, asyncio.IncompleteReadError):
            cw.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await cw.drain()
            return
        acc = accept_key(hdr.get("sec-websocket-key", ""))
        cw.write((f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                  f"Sec-WebSocket-Accept: {acc}\r\n\r\n").encode())  # fmt: skip
        await cw.drain()
        conn = Conn(cw, uw)
        down = asyncio.create_task(self.pump_down(ur, conn))
        try:
            await self.pump_up(cr, conn)
        finally:
            down.cancel()
            with contextlib.suppress(Exception):
                uw.close()

    async def pump_down(self, ur: asyncio.StreamReader, conn: Conn) -> None:
        try:
            while True:
                op, data = await read_message(ur)
                if op == 1 and conn.answer(data):
                    continue
                if op == 9:
                    conn.uw.write(frame(10, data, mask=True))
                    continue
                if self.seal and op == 1 and any(m in data[:300] for m in COOKIE_EVENTS):
                    data = strip_cookies(data)
                await conn.to_client(op, data)
                if op == 8:
                    return
        except (EOFError, ConnectionError, OSError, asyncio.IncompleteReadError):
            with contextlib.suppress(Exception):
                conn.cw.close()

    async def pump_up(self, cr: asyncio.StreamReader, conn: Conn) -> None:
        while True:
            try:
                op, data = await read_message(cr)
            except EOFError:
                return
            if op == 9:
                await conn.to_client(10, data)
                continue
            if self.seal and op == 1 and b"Cookies" in data:
                reply = self.sealed_cmd(data)
                if reply is not None:
                    await conn.to_client(1, reply)
                    continue
            if op == 1 and b'"Input.dispatch' in data:
                reply = await self.check(conn, data)
                if reply is not None:
                    await conn.to_client(1, reply)
                    continue
            await conn.to_upstream(op, data)
            if op == 8:
                return

    async def check(self, conn: Conn, data: bytes) -> bytes | None:
        """None = forward the command; bytes = the CDP error answered instead (enforce mode, denied)."""
        try:
            msg = json.loads(data)
        except ValueError:
            return None
        if not isinstance(msg, dict):
            return None
        method, p, sid = msg.get("method"), msg.get("params") or {}, msg.get("sessionId")
        key = str(sid or "")
        kind = None
        if method == "Input.dispatchMouseEvent" and p.get("button", "left") in ("left", "none"):
            if p.get("type") == "mousePressed":
                kind = "click"
            elif p.get("type") == "mouseReleased" and key in conn.held:
                return self._follow(conn, msg, key)
        elif method == "Input.dispatchKeyEvent" and (p.get("key") in ENTER_KEYS or p.get("code") in ENTER_KEYS
                                                     or p.get("text") == "\r"):  # fmt: skip
            if p.get("type") in ("keyDown", "rawKeyDown"):
                kind = "enter"
            elif key in conn.held:
                return self._follow(conn, msg, key, keep=p.get("type") == "char")
        if kind is None:
            return None
        if kind == "click":
            ev = await resolve_point(conn, sid, float(p.get("x") or 0), float(p.get("y") or 0))
        else:
            ev = await resolve_focus(conn, sid)
        try:
            d = await asyncio.get_running_loop().run_in_executor(None, self.gate.decide, kind, ev)
        except Exception as e:  # the gate itself broke: enforce fails closed, shadow lets it through
            d = Decision(False, f"gate error ({type(e).__name__})")
        m = self.gate.mode_fn()
        decision = "allow" if d.allow else ("deny" if m == "enforce" else "would-deny")
        rec = {"mode": m, "kind": kind, "decision": decision, **d.rec()}
        sig = (key, decision, d.why)
        if decision == "allow" or conn.last_deny[0] != sig or time.monotonic() - conn.last_deny[1] > DENY_QUIET:
            log(rec)  # a client retrying a denied click (Playwright does, until its timeout) is logged once
        if decision != "allow":
            conn.last_deny = (sig, time.monotonic())
        if d.passed is not None and self.gate.notify is not None:  # best effort, never in the click's way
            asyncio.get_running_loop().run_in_executor(None, _quiet, self.gate.notify, d.passed, "commit")
        conn.held[key] = d.allow or m != "enforce"
        return None if conn.held[key] else self._error(msg, d.why)

    def sealed_cmd(self, data: bytes) -> bytes | None:
        """The answer to a cookie dump (always refused in the container, every mode), else None (forward)."""
        try:
            msg = json.loads(data)
        except ValueError:
            return None
        if not isinstance(msg, dict) or msg.get("method") not in SEALED_METHODS:
            return None
        log({"mode": self.gate.mode_fn(), "kind": "cookies", "decision": "deny", "why": "cookie export sealed"})
        return self._error(msg, "cookie export is sealed in claude-hub-browser")

    def _follow(self, conn: Conn, msg: dict[str, Any], key: str, keep: bool = False) -> bytes | None:
        ok = conn.held[key] if keep else conn.held.pop(key)
        return None if ok else self._error(msg, "the press it belongs to was denied")

    @staticmethod
    def _error(msg: dict[str, Any], why: str) -> bytes:
        out: dict[str, Any] = {"id": msg.get("id"), "error": {"code": -32000, "message": f"browsersvc: {why}"}}
        if msg.get("sessionId"):
            out["sessionId"] = msg["sessionId"]
        return json.dumps(out).encode()
