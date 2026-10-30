"""hubkit.phonemirror: the brain's read-only copy of the phone gateway's database tables. Stdlib.

The gateway runs in claude-hub-phone and its phonegw.db is not mounted here; the brain's readers (~/bin/lib/usage.py,
board, dash-data) default to this mirror and run their SQL unchanged: same table and column names, only the
allow-listed subset the gateway's GET /internal/export hands out (adapters/phone/phonegw/export.py: devices, the
readers' state keys cut to their fields, tile events, push_log, cards, chat direction, inbound; no text, coordinates
or nonces).

`hub phone inbox` calls refresh() after each poll: at most once a minute (the mirror's mtime). The file is rebuilt
whole and swapped in atomically (0600), so a reader holding it open keeps a consistent copy. A failed fetch keeps the
old mirror (readers judge staleness by the rows' own times).
Env (tests): HUB_PHONE_MIRROR (file), HUB_PHONE_STATE (its dir), PHONEGW_URL, PHONEGW_TOKEN_FILE / HUB_PHONE_CLI_TOKEN.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from core import paths

EVERY = 60.0
DAYS = 8  # usage reads 7 days back
URL_DEFAULT = "http://claude-hub-phone:8796"
TABLES = {
    "devices": ("id TEXT PRIMARY KEY", "status TEXT", "last_seen INT", "app_code INT"),
    "state": ("device_id TEXT", "k TEXT", "v TEXT", "at INT"),
    "events": ("id TEXT PRIMARY KEY", "device_id TEXT", "kind TEXT", "at INT", "body TEXT"),
    "push_log": (
        "id TEXT PRIMARY KEY",
        "at INT",
        "level TEXT",
        "tag TEXT",
        "delivered_ms INT",
        "acked_ms INT",
        "verb TEXT",
        "action_at INT",
    ),  # fmt: skip
    "cards": ("id TEXT PRIMARY KEY", "kind TEXT", "created INT", "answered INT"),
    "chat": ("id TEXT PRIMARY KEY", "at INT", "dir TEXT"),
    "inbound": ("id TEXT PRIMARY KEY", "t TEXT", "at INT"),
}


def mirror_path() -> Path:
    v = os.environ.get("HUB_PHONE_MIRROR", "")
    if v:
        return Path(v)
    d = os.environ.get("HUB_PHONE_STATE", "")
    return (Path(d) if d else paths.home() / ".local" / "state" / "claude-hub") / "phonegw-mirror.db"


def _token_file() -> Path:
    v = os.environ.get("PHONEGW_TOKEN_FILE") or os.environ.get("HUB_PHONE_CLI_TOKEN") or ""
    return Path(v) if v else paths.home() / ".secrets" / "phone-cli-token"


def _by_handle() -> bool:
    """The live default (claude-hub-phone + ~/.secrets/phone-cli-token) goes by handle "phone" through
    core.egress in hub/shadow mode; a test's own URL or token file stays direct."""
    from core import egress

    v = os.environ.get("PHONEGW_TOKEN_FILE") or os.environ.get("HUB_PHONE_CLI_TOKEN")
    return not v and not os.environ.get("PHONEGW_URL") and egress.mode("phone") in ("hub", "shadow")


def fetch(timeout: float = 5.0) -> dict[str, Any]:
    """GET /internal/export from the hub's gateway (no proxy: claude-hub-phone is on the Docker network). By handle
    "phone" in hub mode; an export over the hub's 8 MiB cap (response_too_large) is read again direct."""
    from core import egress

    since = int((time.time() - DAYS * 86400) * 1000)
    url = (os.environ.get("PHONEGW_URL") or URL_DEFAULT).rstrip("/") + f"/internal/export?since={since}"
    raw = None
    if _by_handle():
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + egress.secret("phone")})
        try:
            with egress.open(req, "phone", timeout=timeout) as r:
                raw = r.read(16 << 20)
        except egress.Refused as e:
            if e.error != "response_too_large":
                raise
    if raw is None:
        token = _token_file().read_text(encoding="utf-8").strip()
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as r:
            raw = r.read(16 << 20)
    d = json.loads(raw)
    if not isinstance(d, dict) or not d.get("ok") or d.get("schema") != 1:
        raise ValueError("not an export")
    return d


def write(d: dict[str, Any], path: Path | None = None) -> int:
    """The export -> a fresh SQLite file swapped in atomically. -> rows written."""
    f = path or mirror_path()
    f.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = f.with_name(f".{f.name}.{os.getpid()}.tmp")
    tmp.unlink(missing_ok=True)
    os.close(os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600))
    n = 0
    try:
        con = sqlite3.connect(tmp)
        try:
            for t, cols in TABLES.items():
                con.execute(f"CREATE TABLE {t}({', '.join(cols)})")
                names = [c.split()[0] for c in cols]
                rows = [tuple(r.get(c) for c in names) for r in d.get(t) or [] if isinstance(r, dict)]
                con.executemany(f"INSERT OR REPLACE INTO {t} VALUES({', '.join('?' * len(names))})", rows)
                n += len(rows)
            con.execute("CREATE TABLE mirror(k TEXT PRIMARY KEY, v TEXT)")
            con.executemany(
                "INSERT INTO mirror VALUES(?, ?)",
                [("fetched", str(round(time.time()))), ("export_at", str(d.get("at"))), ("schema", "1")],
            )
            con.commit()
        finally:
            con.close()
        os.replace(tmp, f)
    finally:
        tmp.unlink(missing_ok=True)
    return n


def due(now: float | None = None) -> bool:
    """The mirror is missing or a minute old."""
    try:
        return (time.time() if now is None else now) - mirror_path().stat().st_mtime >= EVERY
    except OSError:
        return True


_last_try = 0.0


def refresh(force: bool = False) -> str:
    """-> fresh | ok | failed. Never raises (the inbox goes on)."""
    global _last_try
    now = time.time()
    if not force and (not due(now) or now - _last_try < EVERY):
        return "fresh"
    _last_try = now
    try:
        write(fetch())
        return "ok"
    except (OSError, ValueError, urllib.error.URLError, sqlite3.Error):
        return "failed"
