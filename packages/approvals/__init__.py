"""approvals: one-time approvals the owner gives with a tap. Stdlib only.

A flow that needs his OK for one exact action asks for an approval: a 128-bit nonce bound to the sha256 of a canonical
action string (the string itself is never stored), a summary of at most 80 characters, and an expiry (TTL 15 min by
default). The card carries ✅/❌ buttons with the callback data ap:<nonce>:y|n. His tap decides it:
  - tg-adapter (the hub's): the adapter records the decision itself and writes an approval.decided event;
  - telegram-plus 0.1.22 (poll mode): his tap lands in the plugin's inbound journal; consume / status / wait read the
    decision from there (owner entries of his user id only, inside the approval's lifetime).
consume(nonce, action) succeeds exactly once, only for the same action, only before the expiry.

  db_path()                                 APPROVALS_DB, else ~/.local/state/assistant/approvals.db (core.paths)
  connect(path=None)                        open, create on first use (directory 0700, database 0600, WAL)
  new(action, summary, ttl=900, ref=None)   -> {nonce, expires, summary, ref, buttons}; live callers pass no con:
                                            a con on the default store while hub approvals are on raises
  attach(nonce, chat, message_id)           the card that carries it (the newest wins: the expiry edit uses it)
  get(nonce) / state(row, now)              the row / its effective state (pending past expires = expired; used)
  sync(nonce)                               poll mode: a decision from his tap in the inbound journal
  consume(nonce, action, by)                -> (code, info): 0 ok (once) · 3 expired · 4 used · 5 unknown / not his
  cancel(nonce)                             pending -> cancelled (a replaced card); taps on it are refused
  wait(nonce, timeout)                      -> (code, info): 0 approved · 3 expired · 5 denied / cancelled / unknown ·
                                            6 still pending at the timeout
  audit()                                   T8 violations: used without approval, used or decided after expiry
  buttons(nonce, yes, no) / parse_data(d)   the card's ✅/❌ row / "ap:<nonce>:y" -> (nonce, "y")

Integrity: one uid runs the brain's side, so this is a tripwire, not a boundary: vault-guard (rule
approvals-state) denies Bash/Write/Edit on the store, and there is no verb that records a decision. Decisions come only
from his tap: the adapter's callback query from his user id, or his entry in the plugin's inbound journal.
Env (tests): APPROVALS_DB, APPROVALS_JOURNAL (the inbound journal), APPROVALS_ACCESS (the access file: owner = the first
allowFrom). With ASSISTANT_TEST=1 every path must lie outside the real home (core.paths.guard).
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from core import paths

__all__ = [
    "DDL",
    "DEFAULT_TTL",
    "EXIT_EXPIRED",
    "EXIT_NOT_HIS",
    "EXIT_OK",
    "EXIT_PENDING",
    "EXIT_USED",
    "access_path",
    "action_sha",
    "attach",
    "audit",
    "buttons",
    "cancel",
    "connect",
    "consume",
    "db_path",
    "get",
    "journal_path",
    "new",
    "owner_id",
    "parse_data",
    "prune",
    "rows",
    "state",
    "sync",
    "wait",
]

EXIT_OK, EXIT_EXPIRED, EXIT_USED, EXIT_NOT_HIS, EXIT_PENDING = 0, 3, 4, 5, 6
DEFAULT_TTL = 900
MIN_TTL, MAX_TTL = 1, 86_400
SUMMARY_MAX = 80
KEEP_DAYS = 30
ACTION_MAX = 8192
JOURNAL_TAIL = 500
NONCE_RE = re.compile(r"[0-9a-f]{32}")
DATA_RE = re.compile(r"ap:([0-9a-f]{32}):([yn])")
REF_RE = re.compile(r"[\w:.@/+-]{1,80}")
YES, NO = "✅ Approve", "❌ Decline"
STATES = ("pending", "approved", "denied", "expired", "cancelled")

# schemas/approvals.sql verbatim (a test diffs the two); tg-adapter keeps the same DDL in adapters/tg/src/approvals.sql.
DDL = (
    "-- approvals.db (v2 Phase 1 P7): one-time approvals the owner gives with a tap on a ✅/❌ card. 0600, WAL.\n"
    "-- Live: ~/.local/state/assistant/approvals.db; tg-adapter's test instance keeps its own beside its state.\n"
    "-- Writers: tg-adapter (cards it sends, his taps, the expiry edit) and the approvals library/CLI (new, consume, "
    "cancel,\n"
    "-- poll-mode decisions read from the telegram-plus inbound journal). Never by hand: vault-guard rule "
    "approvals-state.\n"
    "-- The action itself is never stored: only its sha256 and a summary of at most 80 characters.\n"
    "CREATE TABLE IF NOT EXISTS approvals(nonce TEXT PRIMARY KEY, action_sha TEXT NOT NULL, summary TEXT NOT NULL "
    "DEFAULT '',\n"
    "  ref TEXT, chat TEXT, message_id INTEGER, created REAL NOT NULL, expires REAL NOT NULL,\n"
    "  status TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|denied|expired|cancelled\n"
    "  decided_ts REAL, decided_by TEXT, decided_via TEXT,  -- his tap: when, his user id, adapter|journal\n"
    "  used_ts REAL, used_by TEXT,  -- consume: once, before expires\n"
    "  refused INTEGER NOT NULL DEFAULT 0,  -- consume attempts refused (wrong action, not approved)\n"
    "  card TEXT);  -- the card's expiry edit: edited|failed\n"
    "CREATE INDEX IF NOT EXISTS approvals_status ON approvals(status, expires);\n"
)

# T8 (plan §7): an approval used without his yes, used at or after its expiry, or decided at or after its expiry.
AUDIT_SQL = (
    "SELECT COUNT(*) FROM approvals WHERE (used_ts IS NOT NULL AND (used_ts >= expires OR status != 'approved'"
    " OR decided_ts IS NULL)) OR (decided_ts IS NOT NULL AND decided_ts >= expires)"
)


# ---------------------------------------------------------------- paths and the store
def db_path() -> Path:
    p = os.environ.get("APPROVALS_DB", "")
    return paths.guard(os.path.expanduser(p)) if p else paths.state("approvals.db")


def journal_path() -> Path:
    p = os.environ.get("APPROVALS_JOURNAL", "")
    if p:
        return paths.guard(os.path.expanduser(p))
    return paths.guard(paths.home() / ".claude" / "channels" / "telegram" / "inbound-journal.jsonl")


def access_path() -> Path:
    p = os.environ.get("APPROVALS_ACCESS", "")
    if p:
        return paths.guard(os.path.expanduser(p))
    return paths.guard(paths.home() / ".claude" / "channels" / "telegram" / "access.json")


def owner_id(access: str | os.PathLike[str] | None = None) -> str:
    """The owner's Telegram user id: the first allowFrom entry of the access file ("" when unreadable)."""
    try:
        with open(access or access_path(), encoding="utf-8") as f:
            a = json.load(f)
        return str((a.get("allowFrom") or [""])[0] or "")
    except (OSError, ValueError, AttributeError, IndexError):
        return ""


def connect(path: str | os.PathLike[str] | None = None, *, timeout: float = 5.0) -> sqlite3.Connection:
    """Open the store (created on first use: directory 0700, files 0600 via umask 077, WAL, synchronous FULL)."""
    p = Path(path) if path else db_path()
    old = os.umask(0o077)
    try:
        p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        con = sqlite3.connect(os.fspath(p), timeout=timeout, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=FULL")
        con.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
        con.executescript(DDL)
        for f in (p, Path(f"{p}-wal"), Path(f"{p}-shm")):
            with contextlib.suppress(OSError):
                if f.exists():
                    os.chmod(f, 0o600)
        return con
    finally:
        os.umask(old)


@contextlib.contextmanager
def tx(con: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
    except BaseException:
        con.execute("ROLLBACK")
        raise
    con.execute("COMMIT")


@contextlib.contextmanager
def _open(con: sqlite3.Connection | None) -> Iterator[sqlite3.Connection]:
    if con is not None:
        yield con
        return
    c = connect()
    try:
        yield c
    finally:
        c.close()


# ---------------------------------------------------------------- small helpers
def _hub() -> bool:
    """Mint, consume and read on the hub (core.hub.backend; the local store only where no hub is configured): his
    taps reach only the hub's adapter, which records them on the hub and never mints, so an approval made in the
    local store could never be decided."""
    from core import hub

    return hub.backend("approvals") == "hub"


def _is_live(con: sqlite3.Connection) -> bool:
    """con is open on the default store (db_path()). Minting there while hub approvals are on makes a card no tap can
    decide (a caller's own connection would skip the hub); scratch stores stay free."""
    try:
        f = con.execute("PRAGMA database_list").fetchone()[2]
        return bool(f) and Path(f).resolve() == db_path().resolve()
    except (sqlite3.Error, OSError, TypeError, IndexError, RuntimeError):
        return False


def _rid() -> str:
    from core import hub

    return hub.new_rid()


def _hcall(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """A hub call; None for 404. Fails closed: an unreachable hub raises (nothing is asked for, approved or used)."""
    from core import hub

    try:
        return hub.call(method, path, body, timeout=8.0)
    except hub.Refused as e:
        if e.code == 404:
            return None
        raise ValueError(f"approvals: the hub refused it ({e})") from None
    except hub.HubError as e:
        raise RuntimeError(f"approvals: hub unreachable ({e}); nothing was asked for or used") from None


def action_sha(action: str) -> str:
    return hashlib.sha256(action.encode("utf-8")).hexdigest()


def one_line(s: str, n: int) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def norm_nonce(v: str) -> str:
    """A nonce as given: the 32 hex chars, or the callback data ap:<nonce>:y|n."""
    v = str(v or "").strip().lower()
    m = DATA_RE.fullmatch(v)
    return m.group(1) if m else v


def parse_data(data: str) -> tuple[str, str] | None:
    m = DATA_RE.fullmatch(str(data or ""))
    return (m.group(1), m.group(2)) if m else None


def buttons(nonce: str, yes: str = YES, no: str = NO) -> list[list[dict[str, str]]]:
    return [[{"text": yes, "data": f"ap:{nonce}:y"}, {"text": no, "data": f"ap:{nonce}:n"}]]


def _ts(v: Any) -> float | None:
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        pass
    try:
        return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _dict(r: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(r) if r is not None else None


def state(row: dict[str, Any], now: float | None = None) -> str:
    """Effective state: used once consumed; a pending row at or past its expiry is expired."""
    if row.get("used_ts") is not None:
        return "used"
    if row["status"] == "pending" and (now if now is not None else time.time()) >= row["expires"]:
        return "expired"
    return str(row["status"])


def local_hm(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


# ---------------------------------------------------------------- create, attach, read
def new(
    action: str,
    summary: str,
    ttl: int = DEFAULT_TTL,
    *,
    ref: str | None = None,
    chat: str | None = None,
    yes: str = YES,
    no: str = NO,
    con: sqlite3.Connection | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """A pending approval for this exact action. The action string is hashed, never stored."""
    if not isinstance(action, str) or not action.strip():
        raise ValueError("the action is empty")
    if len(action) > ACTION_MAX:
        raise ValueError(f"the action is longer than {ACTION_MAX} characters")
    summ = one_line(summary, SUMMARY_MAX)
    if not summ:
        raise ValueError("the summary is empty")
    if not MIN_TTL <= int(ttl) <= MAX_TTL:
        raise ValueError(f"ttl must be {MIN_TTL}-{MAX_TTL} seconds")
    if ref is not None and not REF_RE.fullmatch(ref):
        raise ValueError("ref: 1-80 of letters, digits and :.@/+-_")
    if con is not None and _is_live(con) and _hub():
        raise RuntimeError("approvals: hub approvals are on: mint without a connection (his tap is decided on the hub)")
    if con is None and _hub():  # the hub mints the nonce; only the action's sha256 leaves this process
        got = _hcall("POST", "/approvals", {"action_sha": action_sha(action), "summary": summ, "ttl": int(ttl),
                                             "ref": ref, "chat": chat, "rid": _rid()})  # fmt: skip
        if got is None:
            raise RuntimeError("approvals: the hub refused the request")
        exp = float(got["expires"])
        return {"nonce": got["nonce"], "expires": exp, "expires_local": local_hm(exp), "summary": got["summary"],
                "ref": got.get("ref"), "buttons": buttons(got["nonce"], yes, no)}  # fmt: skip
    t = now if now is not None else time.time()
    nonce = secrets.token_hex(16)
    with _open(con) as c, tx(c):
        c.execute(
            "INSERT INTO approvals(nonce, action_sha, summary, ref, chat, created, expires)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (nonce, action_sha(action), summ, ref, chat, t, t + int(ttl)),
        )
        prune(c, t)
    exp = t + int(ttl)
    return {
        "nonce": nonce,
        "expires": exp,
        "expires_local": local_hm(exp),
        "summary": summ,
        "ref": ref,
        "buttons": buttons(nonce, yes, no),
    }


def attach(nonce: str, chat: str, message_id: int | str, *, con: sqlite3.Connection | None = None) -> bool:
    """Record the card (chat + message id) of a pending approval; the newest card wins. False: unknown or decided."""
    n = norm_nonce(nonce)
    if con is None and _hub():
        if not NONCE_RE.fullmatch(n):
            return False
        got = _hcall("POST", f"/approvals/{n}/attach", {"chat": str(chat), "message_id": int(message_id)})
        return bool(got and got.get("ok"))
    with _open(con) as c, tx(c):
        cur = c.execute(
            "UPDATE approvals SET chat = ?, message_id = ? WHERE nonce = ? AND status = 'pending' AND (chat IS NULL"
            " OR chat = ?)",
            (str(chat), int(message_id), n, str(chat)),
        )
        return cur.rowcount == 1


def get(nonce: str, *, con: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    n = norm_nonce(nonce)
    if not NONCE_RE.fullmatch(n):
        return None
    if con is None and _hub():
        got = _hcall("GET", f"/approvals/{n}")
        return {k: v for k, v in got.items() if k != "state"} if got else None
    with _open(con) as c:
        return _dict(c.execute("SELECT * FROM approvals WHERE nonce = ?", (n,)).fetchone())


def rows(*, every: bool = False, con: sqlite3.Connection | None = None, now: float | None = None) -> list[dict]:
    t = now if now is not None else time.time()
    if con is None and _hub():
        return list((_hcall("GET", "/approvals" + ("?every=1" if every else "")) or {}).get("rows") or [])
    with _open(con) as c:
        q = "SELECT * FROM approvals" + ("" if every else " WHERE status = 'pending' AND expires > ?")
        q += " ORDER BY created"
        return [dict(r) for r in c.execute(q, () if every else (t,)).fetchall()]


def prune(con: sqlite3.Connection, now: float | None = None, days: int = KEEP_DAYS) -> int:
    """Rows created more than `days` ago go (their summaries with them). Call inside a transaction."""
    t = now if now is not None else time.time()
    return con.execute("DELETE FROM approvals WHERE created < ?", (t - days * 86_400,)).rowcount


# ---------------------------------------------------------------- poll mode: his tap from the inbound journal
def _journal_taps(nonce: str, journal: str | os.PathLike[str]) -> list[tuple[float, str, dict[str, Any]]]:
    """(ts, y|n, meta) of every owner button entry carrying ap:<nonce>:y|n in the journal's tail, oldest first."""
    try:
        with open(journal, encoding="utf-8") as f:
            lines = f.readlines()[-JOURNAL_TAIL:]
    except OSError:
        return []
    out: list[tuple[float, str, dict[str, Any]]] = []
    for line in lines:
        if nonce not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict) or d.get("kind") != "button" or d.get("owner") is not True:
            continue
        meta = d.get("meta")
        m: dict[str, Any] = meta if isinstance(meta, dict) else {}
        p = parse_data(str(m.get("callback_data") or ""))
        if not p or p[0] != nonce:
            continue
        ts = _ts(m.get("ts")) or _ts(d.get("ts")) or _ts(d.get("at"))
        if ts is not None:
            out.append((ts, p[1], m))
    out.sort(key=lambda x: x[0])
    return out


def sync(
    nonce: str,
    *,
    journal: str | os.PathLike[str] | None = None,
    owner: str | None = None,
    con: sqlite3.Connection | None = None,
) -> str | None:
    """Poll mode: a pending approval's decision from his first tap in the telegram-plus inbound journal (his user id,
    in the card's chat, else his private chat; between creation and expiry; Telegram stamps whole seconds, so 1 s of
    slack before creation). Returns the new status, or None when nothing changed. Hub mode: None (decisions reach
    the hub only from the tg caller, his tap; never from this process)."""
    n = norm_nonce(nonce)
    if not NONCE_RE.fullmatch(n):
        return None
    if con is None and _hub():
        return None
    with _open(con) as c:
        r = _dict(c.execute("SELECT * FROM approvals WHERE nonce = ?", (n,)).fetchone())
        if not r or r["status"] != "pending":
            return None
        who = owner if owner is not None else owner_id()
        if not who:
            return None
        try:
            taps = _journal_taps(n, journal or journal_path())
        except paths.RealHomeRefused:
            return None
        for ts, yn, m in taps:
            if str(m.get("user_id") or "") != who:
                continue
            if str(m.get("chat_id") or "") != str(r["chat"] or who):  # the card's chat, else his DM (= his id)
                continue
            if not r["created"] - 1 <= ts < r["expires"]:
                continue
            st = "approved" if yn == "y" else "denied"
            with tx(c):
                cur = c.execute(
                    "UPDATE approvals SET status = ?, decided_ts = ?, decided_by = ?, decided_via = 'journal'"
                    " WHERE nonce = ? AND status = 'pending'",
                    (st, ts, who, n),
                )
            return st if cur.rowcount == 1 else None
    return None


# ---------------------------------------------------------------- consume, cancel, wait
def _info(r: dict[str, Any], now: float, **kw: Any) -> dict[str, Any]:
    return {
        "nonce": r["nonce"],
        "state": state(r, now),
        "summary": r["summary"],
        "ref": r["ref"],
        "expires": r["expires"],
        "expires_local": local_hm(r["expires"]),
        "decided_via": r["decided_via"],
        **kw,
    }


def consume(
    nonce: str,
    action: str,
    by: str = "cli",
    *,
    journal: str | os.PathLike[str] | None = None,
    owner: str | None = None,
    con: sqlite3.Connection | None = None,
    now: float | None = None,
) -> tuple[int, dict[str, Any]]:
    """Use his approval for this exact action, once. (0, info) the first time; 3 expired; 4 used already; 5 unknown,
    not decided, declined, cancelled or another action (not his approval for this)."""
    t = now if now is not None else time.time()
    n = norm_nonce(nonce)
    if not NONCE_RE.fullmatch(n):
        return EXIT_NOT_HIS, {"nonce": n, "state": "unknown", "why": "not a nonce"}
    if con is None and _hub():  # the hub checks and uses it, once; here only the sha256 of the action
        sha = action_sha(action) if isinstance(action, str) else ""
        got = _hcall("POST", f"/approvals/{n}/consume", {"action_sha": sha, "by": one_line(by, 80) or "cli",
                                                          "rid": _rid()})  # fmt: skip
        info = dict((got or {}).get("info") or {"nonce": n, "state": "unknown", "why": "no answer"})
        if isinstance(info.get("expires"), (int, float)):
            info["expires_local"] = local_hm(float(info["expires"]))
        return int((got or {}).get("code", EXIT_NOT_HIS)), info
    with _open(con) as c:
        sync(n, journal=journal, owner=owner, con=c)
        with tx(c):
            r = _dict(c.execute("SELECT * FROM approvals WHERE nonce = ?", (n,)).fetchone())
            if r is None:
                return EXIT_NOT_HIS, {"nonce": n, "state": "unknown", "why": "no such approval"}
            if r["used_ts"] is not None:
                return EXIT_USED, _info(r, t, why=f"used already ({r['used_by']})")
            if t >= r["expires"]:
                if r["status"] == "pending":
                    c.execute("UPDATE approvals SET status = 'expired' WHERE nonce = ? AND status = 'pending'", (n,))
                    r["status"] = "expired"
                return EXIT_EXPIRED, _info(r, t, why="expired")
            if r["status"] != "approved":
                c.execute("UPDATE approvals SET refused = refused + 1 WHERE nonce = ?", (n,))
                why = {
                    "pending": "not decided yet",
                    "denied": "he declined",
                    "cancelled": "cancelled (replaced card)",
                    "expired": "expired",
                }.get(r["status"], r["status"])
                return (EXIT_EXPIRED if r["status"] == "expired" else EXIT_NOT_HIS), _info(r, t, why=why)
            if not isinstance(action, str) or action_sha(action) != r["action_sha"]:
                c.execute("UPDATE approvals SET refused = refused + 1 WHERE nonce = ?", (n,))
                return EXIT_NOT_HIS, _info(r, t, why="another action: his approval does not cover it")
            cur = c.execute(
                "UPDATE approvals SET used_ts = ?, used_by = ? WHERE nonce = ? AND status = 'approved'"
                " AND used_ts IS NULL AND expires > ?",
                (t, one_line(by, 80) or "cli", n, t),
            )
            if cur.rowcount != 1:
                return EXIT_USED, _info(r, t, why="used already")
            r["used_ts"], r["used_by"] = t, by
            return EXIT_OK, _info(r, t, why="approved by him", used_by=by)


def cancel(nonce: str, *, con: sqlite3.Connection | None = None, now: float | None = None) -> bool:
    """pending -> cancelled (a newer card replaced it, or the flow ended): his taps on it are refused from now on."""
    n = norm_nonce(nonce)
    t = now if now is not None else time.time()
    if con is None and _hub():
        got = _hcall("POST", f"/approvals/{n}/cancel", {}) if NONCE_RE.fullmatch(n) else None
        return bool(got and got.get("ok"))
    with _open(con) as c, tx(c):
        cur = c.execute(
            "UPDATE approvals SET status = 'cancelled', decided_ts = NULL WHERE nonce = ? AND status = 'pending'"
            " AND expires > ?",
            (n, t),
        )
        return cur.rowcount == 1


def wait(
    nonce: str,
    timeout: float = 60.0,
    *,
    interval: float = 0.5,
    journal: str | os.PathLike[str] | None = None,
    owner: str | None = None,
    path: str | os.PathLike[str] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Wait for his decision (never consumes): 0 approved · 3 expired · 5 declined, cancelled or unknown · 6 still
    pending when the timeout ran out. Reads the store afresh each round (a restarted adapter loses nothing)."""
    n = norm_nonce(nonce)
    end = time.monotonic() + max(0.0, timeout)
    hub_mode = path is None and _hub()
    while True:
        if hub_mode:
            r = get(n) if NONCE_RE.fullmatch(n) else None
        else:
            c = connect(path)
            try:
                sync(n, journal=journal, owner=owner, con=c)
                r = _dict(c.execute("SELECT * FROM approvals WHERE nonce = ?", (n,)).fetchone())
            finally:
                c.close()
        t = time.time()
        if r is None:
            return EXIT_NOT_HIS, {"nonce": n, "state": "unknown", "why": "no such approval"}
        st = state(r, t)
        if st in ("approved", "used"):
            return EXIT_OK, _info(r, t)
        if st == "expired":
            return EXIT_EXPIRED, _info(r, t)
        if st in ("denied", "cancelled"):
            return EXIT_NOT_HIS, _info(r, t)
        if time.monotonic() >= end:
            return EXIT_PENDING, _info(r, t, why="still waiting for his tap")
        time.sleep(min(interval, max(0.05, end - time.monotonic())))


def audit(*, con: sqlite3.Connection | None = None) -> dict[str, Any]:
    """T8 (plan §7): approvals used without his yes, used or decided at or after expiry. 0 is the target."""
    if con is None and _hub():
        got = _hcall("GET", "/approvals/audit") or {}
        return {k: got.get(k) for k in ("ok", "violations", "approvals", "used", "refused_attempts")}
    with _open(con) as c:
        n = int(c.execute(AUDIT_SQL).fetchone()[0])
        total = int(c.execute("SELECT COUNT(*) FROM approvals").fetchone()[0])
        used = int(c.execute("SELECT COUNT(*) FROM approvals WHERE used_ts IS NOT NULL").fetchone()[0])
        refused = int(c.execute("SELECT COALESCE(SUM(refused), 0) FROM approvals").fetchone()[0])
    return {"ok": n == 0, "violations": n, "approvals": total, "used": used, "refused_attempts": refused}
