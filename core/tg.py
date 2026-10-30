"""core.tg: one sender for the owner's Telegram. Stdlib only.

Scripts that text the owner (tg, tg-buttons, tg-voice, event-card, tg-status's board) put their message into the
hub outbox; the hub's tg-adapter (claude-hub-tg) sends it under the outbox rules (non-urgent send/poll rows wait in
quiet hours and sleep-in, the same text to the same chat within the dedupe window is skipped, urgent rows bypass the
holds; edits, pins and reactions are never held). While the outbox can't take a row, every sender keeps its direct
Bot API path.

  outbox_on(sender) -> (bool, why)   the gate: the hub outbox while the hub answers and its adapter drains it (a
                                     consumer under BOT_KEY within CONSUMER_FRESH s); closed by a switch file
                                     ~/.config/tg-adapter/direct-<sender> (or direct-all), a hub that does not answer
                                     or a stale consumer (the direct path: nothing was written, so nothing goes twice)
  owner_chat() -> str | None         the owner's chat id: the live config's allow_from[0], else allowFrom[0] of its
                                     allow_from_file, else of the telegram-plus access file
  enqueue(op, body, ...) -> int|None one outbox row (bot key "live"); None = its idem key exists; SPOOLED (0) = the
                                     hub is down: spooled (core.hub spool, op outbox), replayed in order
Local files go up first (core.hub.upload: rows carry hubfile: refs); a write the hub can't take after the gate is
spooled and never also sent directly; a row still queued at the wait stays queued while the hub's adapter drains
(held, or busy) or the hub can't be asked, and is withdrawn (then the direct path) when its consumer is gone.
Where no hub is configured (core.hub.backend "sqlite": test runs) the outbox is the in-container SQLite store's,
drained by a tg-adapter running here (live config with mode "live" and send true, its PID file, no live.off, no
events-off).
  wait_sent(row_id, timeout) -> dict the row once it is sent, failed or skipped, else as it stands at the timeout
  withdraw(row_id, why) -> bool      queued -> skipped, atomically: the adapter claims rows with the same condition,
                                     so a withdrawn row is never sent and a claimed one is never withdrawn
  send(sender, op, body, ...) -> Outcome   enqueue + wait; a row still queued while the adapter is gone is withdrawn
                                     (Outcome.direct: take the direct path)
  bot_call(method, params, sender=...)    a Bot API call (sendMessage, editMessageText, editMessageReplyMarkup,
                                     pinChatMessage, unpinChatMessage, deleteMessage) done through the outbox and
                                     answered like the Bot API; a failed row raises TgError with Telegram's reason

Rows carry body.sender (who wrote it); urgent rows (alerts) bypass the holds; body.hold = false (a board or a live
card) and body.dedupe = false turn off the holds and the text dedupe for one row. Nothing here reads a bot token.
Seams (tests): TG_ADAPTER_STATE_DIR (the adapter's state root), ASSISTANT_EVENTS_DB (the live events DB),
TELEGRAM_STATE_DIR (the telegram-plus state dir for the access-file fallback).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core import db, paths

__all__ = [
    "BOT_KEY",
    "DIRECT",
    "TERMINAL",
    "Outcome",
    "SPOOLED",
    "TgError",
    "adapter_running",
    "bot_call",
    "enqueue",
    "events_db",
    "live_config",
    "hub_consumer",
    "outbox_on",
    "owner_chat",
    "route",
    "send",
    "switch_files",
    "wait_sent",
    "withdraw",
]

BOT_KEY = "live"  # the live adapter consumes rows under its bot username and its instance name
CONSUMER_FRESH = 30.0  # the hub outbox takes a send only while its consumer asked for rows this recently
INSTANCE = "live"
TERMINAL = ("sent", "failed", "skipped")
DIRECT = 75  # exit code the CLI uses for "take the direct path"
SPOOLED = 0  # enqueue(): the hub is down, the row waits in core.hub's spool
PID_MARKER = "tg-adapter"
_SENDER_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,40}$")


class TgError(Exception):
    """A Bot API style failure: code (HTTP status from Telegram's reason, 0 = none) and desc (the reason)."""

    def __init__(self, code: int, desc: str):
        super().__init__(f"{code} {desc}")
        self.code, self.desc = code, desc


# ------------------------------------------------------------------------------------------------ paths


def _expand(p: str) -> Path:
    p = p.strip()
    return paths.home() / p[2:] if p.startswith("~/") else Path(p)


def config_dir() -> Path:
    return paths.guard(paths.home() / ".config" / "tg-adapter")


def state_dir() -> Path:
    v = os.environ.get("TG_ADAPTER_STATE_DIR", "")
    root = _expand(v) if v else paths.home() / ".local" / "state" / "tg-adapter"
    return paths.guard(root / INSTANCE)


def switch_files(sender: str) -> list[Path]:
    """The per-sender switch first, then the one for every sender."""
    d = config_dir()
    return [d / f"direct-{sender}", d / "direct-all"]


def live_config() -> dict[str, Any] | None:
    try:
        cfg = json.loads((config_dir() / f"{INSTANCE}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return cfg if isinstance(cfg, dict) else None


def events_db(cfg: dict[str, Any] | None = None) -> Path:
    """The DB the live adapter reads (its paths.ts): the config's "db", else $ASSISTANT_EVENTS_DB, else the store."""
    cfg = live_config() if cfg is None else cfg
    v = cfg.get("db") if isinstance(cfg, dict) else None
    if isinstance(v, str) and v.strip():
        return paths.guard(_expand(v))
    env = os.environ.get("ASSISTANT_EVENTS_DB", "")
    return paths.guard(_expand(env)) if env else db.default_path()


def adapter_running() -> int | None:
    """The live adapter's pid when its PID file names a running tg-adapter process (ctl.running_pid's test)."""
    try:
        first, second, *_ = (state_dir() / "adapter.pid").read_text(encoding="utf-8").split("\n") + ["", ""]
        pid = int(first)
    except (OSError, ValueError):
        return None
    if pid <= 1 or second.strip() != PID_MARKER:
        return None
    try:
        os.kill(pid, 0)
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    return pid if (b"cli.ts" in cmd or PID_MARKER.encode() in cmd) else None


# ------------------------------------------------------------------------------------------------ gate


def hub_consumer() -> tuple[bool | None, str]:
    """Does a consumer drain the hub outbox? (True, why): one asked for rows under BOT_KEY within CONSUMER_FRESH s;
    (False, why): the hub answers, none did; (None, why): the hub does not answer (down, refused, or no token)."""
    from core import hub

    try:
        got = hub.call("GET", "/outbox/consumers", timeout=1.5)
    except hub.HubError as e:
        return None, f"the hub does not answer ({type(e).__name__})"
    ages: list[float] = []
    for c in got.get("consumers") or []:
        try:
            if isinstance(c, dict) and c.get("bot") == BOT_KEY and c.get("age") is not None:
                ages.append(float(c["age"]))
        except (TypeError, ValueError):
            continue
    if ages and min(ages) < CONSUMER_FRESH:
        return True, f"its consumer drained it {min(ages):.0f} s ago"
    return False, "no consumer drains it" + (f" (last seen {min(ages):.0f} s ago)" if ages else "")


def _tg_hub() -> bool:
    from core import hub

    return hub.tg_hub()


def route(sender: str) -> tuple[str, str]:
    """("hub" | "sqlite" | "direct", why): the gate, with the path it opens."""
    on, why = outbox_on(sender)
    if not on:
        return "direct", why
    return ("hub" if why.startswith("hub") else "sqlite"), why


def outbox_on(sender: str) -> tuple[bool, str]:
    """(on, why). Cheap: a few stats and one hub call (no hub configured: two small reads, no DB)."""
    if not _SENDER_OK.match(sender or ""):
        return False, f"bad sender name {sender!r}"
    try:
        for f in switch_files(sender):
            if f.exists():
                return False, f"switch {f.name}"
        if _tg_hub():  # the hub outbox while the hub answers and its adapter drains it, else the direct path
            live, why = hub_consumer()
            return (True, f"hub: the hub outbox, {why}") if live else (False, f"hub: {why}")
        if (config_dir() / f"{INSTANCE}.off").exists():
            return False, f"switch {INSTANCE}.off"
        if paths.config("events-off").exists():
            return False, "ledger switch events-off"
        cfg = live_config()
        if cfg is None:
            return False, "live adapter not configured"
        if cfg.get("mode") != INSTANCE or cfg.get("send") is not True:
            return False, "live adapter does not send (mode/send)"
        if not adapter_running():
            return False, "live adapter not running"
    except paths.RealHomeRefused:
        raise
    except OSError as e:  # unreadable state: the direct path is the safe default
        return False, f"gate error: {type(e).__name__}"
    return True, "live adapter running"


def _first_id(v: Any) -> str | None:
    if isinstance(v, list) and v and str(v[0]).strip().lstrip("-").isdigit():
        return str(v[0]).strip()
    return None


def _access_file_owner(p: Path) -> str | None:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return _first_id(d.get("allowFrom")) if isinstance(d, dict) else None


def owner_chat(cfg: dict[str, Any] | None = None) -> str | None:
    """The owner's chat id (DM chat id = his user id); None when no source names one."""
    cfg = live_config() if cfg is None else cfg
    if isinstance(cfg, dict):
        got = _first_id(cfg.get("allow_from"))
        if got:
            return got
        af = cfg.get("allow_from_file")
        if isinstance(af, str) and af.strip():
            got = _access_file_owner(_expand(af))
            if got:
                return got
    sd = os.environ.get("TELEGRAM_STATE_DIR", "")
    base = _expand(sd) if sd else paths.home().joinpath(".claude", "channels", "telegram")
    return _access_file_owner(base / "access.json")


# ------------------------------------------------------------------------------------------------ rows


@contextlib.contextmanager
def _con(path: Path | None = None, ro: bool = False) -> Iterator[sqlite3.Connection]:
    con = db.connect(path or events_db(), ro=ro)
    try:
        yield con
    finally:
        con.close()


def enqueue(
    op: str,
    body: dict[str, Any],
    *,
    urgent: bool = False,
    idem: str | None = None,
    chat: str | None = None,
    files: list[str] | None = None,
    sender: str | None = None,
    db_path: Path | None = None,
    via: str = "sqlite",
) -> int | None:
    """One outbox row; returns its id, None when `idem` is already in the outbox, SPOOLED when the hub is down.
    chat: default the owner. via="hub": the hub outbox; "sqlite": the SQLite store's (no hub configured)."""
    chat = chat or owner_chat()
    if not chat:
        raise TgError(0, "no owner chat id (live config allow_from / access file)")
    b = dict(body)
    if sender:
        b.setdefault("sender", sender)
    if via == "hub":
        from core import hub

        row = {"op": op, "body": b, "chat": str(chat), "bot": BOT_KEY, "urgent": urgent, "idem_key": idem,
               "files": files, "rid": hub.new_rid()}  # fmt: skip
        got = hub.write("outbox", row)  # files go up first; a hub that can't take the row gets it from the spool
        if got is None:
            return SPOOLED
        return int(got["id"]) if got.get("id") is not None else None
    with _con(db_path) as con, db.tx(con):
        cur = con.execute(
            "INSERT INTO outbox(ts, bot, chat, op, body, idem_key, urgent, mode, files) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, 'live', ?) ON CONFLICT(idem_key) DO NOTHING",
            (
                time.time(),
                BOT_KEY,
                str(chat),
                op,
                json.dumps(b, ensure_ascii=False, separators=(",", ":")),
                idem,
                1 if urgent else 0,
                json.dumps(files) if files else None,
            ),
        )
        return int(cur.lastrowid or 0) if cur.rowcount else None


def _row(con: sqlite3.Connection | None, row_id: int) -> dict[str, Any] | None:
    d: dict[str, Any]
    if con is None:  # the hub outbox
        from core import hub

        try:
            full = hub.call("GET", f"/outbox/{int(row_id)}", timeout=3.0)
        except hub.Refused as e:
            if e.code == 404:
                return None
            raise
        keep = ("id", "status", "attempts", "last_error", "tg_message_id", "result", "urgent")
        d = {k: full.get(k) for k in keep}
    else:
        r = con.execute(
            "SELECT id, status, attempts, last_error, tg_message_id, result, urgent FROM outbox WHERE id=?", (row_id,)
        ).fetchone()
        if r is None:
            return None
        d = dict(r)
    try:
        res = json.loads(d["result"]) if d.get("result") else {}
    except ValueError:
        res = {}
    d["result"] = res if isinstance(res, dict) else {}
    ids = d["result"].get("ids")
    d["ids"] = [int(i) for i in ids if isinstance(i, int)] if isinstance(ids, list) else []
    if not d["ids"] and d.get("tg_message_id"):
        d["ids"] = [int(d["tg_message_id"])]
    return d


def wait_sent(
    row_id: int, timeout: float = 10.0, *, poll: float = 0.05, db_path: Path | None = None, via: str = "sqlite"
) -> dict[str, Any]:
    """The row once its status is sent, failed or skipped; else the row as it stands when `timeout` runs out."""
    deadline = time.monotonic() + max(0.0, timeout)
    if via == "hub":
        while True:
            r = _row(None, row_id)
            if r is None:
                raise TgError(0, f"outbox row #{row_id} not found")
            if r["status"] in TERMINAL or time.monotonic() >= deadline:
                return r
            time.sleep(poll)
    with _con(db_path, ro=True) as con:
        while True:
            r = _row(con, row_id)
            if r is None:
                raise TgError(0, f"outbox row #{row_id} not found")
            if r["status"] in TERMINAL or time.monotonic() >= deadline:
                return r
            time.sleep(poll)


def withdraw(row_id: int, why: str, *, db_path: Path | None = None, via: str = "sqlite") -> bool:
    """queued -> skipped (last_error "withdrawn: why"); False when the adapter already claimed or ended the row.
    The hub applies the same condition to its claims, so a row is withdrawn or claimed, never both."""
    if via == "hub":
        from core import hub

        return bool(hub.call("POST", f"/outbox/{int(row_id)}/withdraw", {"why": why}, timeout=3.0).get("ok"))
    with _con(db_path) as con, db.tx(con):
        cur = con.execute(
            "UPDATE outbox SET status='skipped', last_error=? WHERE id=? AND status='queued'",
            (f"withdrawn: {why}"[:300], row_id),
        )
        return cur.rowcount == 1


@dataclass
class Outcome:
    """What became of one send: status sent|failed|skipped|queued, or direct (take the direct path); via = the outbox
    the row went to (hub | sqlite; "" = none)."""

    status: str
    row_id: int | None = None
    ids: list[int] = field(default_factory=list)
    error: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    via: str = ""

    @property
    def direct(self) -> bool:
        return self.status == "direct"


def send(
    sender: str,
    op: str,
    body: dict[str, Any],
    *,
    urgent: bool = False,
    idem: str | None = None,
    files: list[str] | None = None,
    wait: float = 10.0,
    grace: float = 30.0,
    check_gate: bool = True,
) -> Outcome:
    """Enqueue + wait. Outcome.direct when the gate is closed, the outbox can't take the row, or the row was still
    queued at `wait` while its adapter is gone (the hub's, by its consumer age; else the local one): then
    it is withdrawn first, so the caller's direct send is the only one. A row the adapter claimed gets up to `grace`
    more seconds to end."""
    from core import hub

    if check_gate:
        via, why = route(sender)
        if via == "direct":
            return Outcome("direct", error=why)
    else:
        via = "hub" if hub.tg_hub() else "sqlite"
    tg_mode = via == "hub"
    try:
        rid = enqueue(op, body, urgent=urgent, idem=idem, files=files, sender=sender, via=via)
    except hub.Refused as e:
        if tg_mode:  # a file the hub refuses (too big, unreadable): no direct path
            return Outcome("failed", error=f"refused by the hub: {e}", via=via)
        return Outcome("direct", error=f"enqueue failed: {type(e).__name__}: {e}")
    except (sqlite3.Error, OSError, TgError, hub.HubError) as e:
        return Outcome("direct", error=f"enqueue failed: {type(e).__name__}: {e}")
    if rid is None:
        return Outcome("skipped", error=f"idem key {idem} already in the outbox", via=via)
    if tg_mode and rid == SPOOLED:
        return Outcome("queued", None, error="spooled: the hub is down; it replays in order", via=via)
    if tg_mode:  # the hub's adapter: gone only when the hub says so (a hub that can't be asked keeps the row)
        running, gone = (lambda: hub_consumer()[0] is not False), "the hub's adapter does not drain the outbox"
    else:
        running, gone = adapter_running, "live adapter stopped"
    try:
        r = wait_sent(rid, wait, via=via)
        if r["status"] == "queued":
            if running():
                return Outcome("queued", rid, via=via)  # held (quiet hours, sleep-in) or the adapter is busy
            if withdraw(rid, f"{sender}: {gone}; sent directly", via=via):
                return Outcome("direct", rid, error=gone, via=via)
        if r["status"] not in TERMINAL:  # claimed meanwhile: let it finish
            r = wait_sent(rid, grace, via=via)
    except (sqlite3.Error, OSError, hub.HubError) as e:  # the row is in: never also send it directly
        return Outcome("queued", rid, error=f"wait failed: {type(e).__name__}", via=via)
    st = r["status"] if r["status"] in TERMINAL else "queued"
    return Outcome(st, rid, r["ids"], r.get("last_error") or "", r["result"], via=via)


# ------------------------------------------------------------------------------------------------ Bot API calls

_FORMATS = {"HTML": "html", "MarkdownV2": "markdownv2"}


def _code_of(err: str) -> int:
    m = re.search(r"\((\d{3}): ", err) or re.search(r"\b(\d{3}): ", err)
    return int(m.group(1)) if m else 0


def _text_body(params: dict[str, Any]) -> dict[str, Any]:
    b: dict[str, Any] = {}
    if "text" in params:
        b["text"] = params["text"]
    pm = params.get("parse_mode")
    b["format"] = _FORMATS.get(pm, "text") if pm else "text"
    lpo = params.get("link_preview_options")
    if isinstance(lpo, dict) and lpo.get("is_disabled"):
        b["link_preview"] = "off"
    if params.get("disable_web_page_preview"):
        b["link_preview"] = "off"
    rm = params.get("reply_markup")
    if isinstance(rm, dict) and isinstance(rm.get("inline_keyboard"), list):
        b["buttons"] = rm["inline_keyboard"]
    return b


def _translate(method: str, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if method == "sendMessage":
        b = _text_body(params)
        if params.get("disable_notification"):
            b["silent"] = True
        rp = params.get("reply_parameters")
        rt = rp.get("message_id") if isinstance(rp, dict) else params.get("reply_to_message_id")
        if rt is not None:
            b["reply_to"] = int(rt)
        return "send", b
    if method in ("editMessageText", "editMessageReplyMarkup"):
        b = _text_body(params) if method == "editMessageText" else {}
        if method == "editMessageReplyMarkup":
            rm = params.get("reply_markup")
            kb = rm.get("inline_keyboard") if isinstance(rm, dict) else None
            b["buttons"] = kb if isinstance(kb, list) else []
        b["message_id"] = int(params["message_id"])
        return "edit", b
    if method == "pinChatMessage":
        return "pin", {"message_id": int(params["message_id"]), "notify": not params.get("disable_notification", False)}
    if method == "unpinChatMessage":
        if params.get("message_id") is None:
            raise TgError(0, "outbox: unpinChatMessage needs a message_id (never unpin all)")
        return "pin", {"message_id": int(params["message_id"]), "unpin": True}
    if method == "deleteMessage":
        return "delete", {"message_id": int(params["message_id"])}
    raise TgError(0, f"outbox: {method} is not supported here")


def bot_call(
    method: str,
    params: dict[str, Any],
    *,
    sender: str,
    urgent: bool = False,
    hold: bool = True,
    dedupe: bool = True,
    on_press: str | None = None,
    timeout: float = 15.0,
) -> Any:
    """A Bot API call through the live outbox, answered like the Bot API: sendMessage -> {"message_id": id},
    edits -> {"message_id": id}, pin/unpin/delete -> True. Raises TgError on a failed row (Telegram's reason in desc,
    so callers' checks such as "message to edit not found" keep working), on a row that stays queued (held or the
    adapter busy; nothing went out and it is withdrawn when it can be), and when the gate is closed (code -1: take
    the direct path). The gate is the caller's: check outbox_on() first."""
    op, body = _translate(method, params)
    if op == "send" and not hold:
        body["hold"] = False
    if op == "send" and not dedupe:
        body["dedupe"] = False
    if on_press and body.get("buttons"):
        body["on_press"] = on_press
    out = send(sender, op, body, urgent=urgent, wait=timeout, check_gate=False)
    if out.direct:
        raise TgError(-1, f"outbox: {out.error or 'direct path'}")
    if out.status == "sent":
        if op in ("send", "edit"):
            mid = out.ids[-1] if out.ids else body.get("message_id")
            return {"message_id": mid}
        return True
    if out.status == "queued":
        from core import hub

        via = out.via or "sqlite"  # the store the row went to (the hub, whatever its consumer says now)
        try:
            gone = out.row_id is not None and withdraw(out.row_id, f"{sender}: no answer within {timeout:g} s", via=via)
        except (sqlite3.Error, OSError, hub.HubError):  # can't withdraw it now: it may still go out
            gone = False
        if gone:
            raise TgError(0, f"outbox: not sent within {timeout:g} s (row #{out.row_id} withdrawn)")
        raise TgError(0, f"outbox: not sent within {timeout:g} s (row #{out.row_id} still pending)")
    err = out.error or f"outbox row #{out.row_id} {out.status}"
    raise TgError(_code_of(err), err)
