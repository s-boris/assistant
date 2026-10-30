"""Outbox test helpers: a fake live adapter (a process whose PID file and command line look like tg-adapter's), the live
config, and a fake outbox consumer that claims queued rows the way the bun adapter does (queued -> sending only while
still queued) and ends them sent or failed."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

OWNER = "111"


def live_config(home: Path, **extra: Any) -> Path:
    p = home / ".config/tg-adapter/live.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    cfg = {
        "mode": "live",
        "bot": "livebot",
        "token_file": "~/tok/live.env",
        "allow_from": [OWNER],
        "send": True,
        **extra,
    }
    p.write_text(json.dumps(cfg))
    return p


def start_fake_adapter(home: Path) -> subprocess.Popen:
    """A process named tg-adapter (argv[0]) holding the live PID file, as ctl.running_pid / core.tg read it."""
    p = subprocess.Popen(
        ["tg-adapter", "-c", "import time; time.sleep(120)"],
        executable=sys.executable,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    d = home / ".local/state/tg-adapter/live"
    d.mkdir(parents=True, exist_ok=True)
    (d / "adapter.pid").write_text(f"{p.pid}\ntg-adapter\n")
    end = time.monotonic() + 30  # what core.tg.adapter_running reads, before anyone asks (no race under load)
    while b"tg-adapter" not in _cmdline(p.pid) and time.monotonic() < end:
        time.sleep(0.01)
    return p


def _cmdline(pid: int) -> bytes:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return b""


def stop_fake_adapter(p: subprocess.Popen) -> None:
    """Gone and reaped before it returns: a "stale PID file" check right after must not see it alive (flaky at
    load ~13 with a 5 s wait whose timeout was swallowed, 29.10)."""
    try:
        p.send_signal(signal.SIGKILL)
    except OSError:
        pass
    p.wait(60)


def gate_on(home: Path) -> subprocess.Popen:
    live_config(home)
    return start_fake_adapter(home)


def events_db(home: Path) -> Path:
    return home / ".local/state/assistant/events.db"


def rows(home: Path, sql: str = "SELECT * FROM outbox WHERE mode='live' ORDER BY id", args=()) -> list[dict]:
    p = events_db(home)
    if not p.exists():
        return []
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()


class Consumer:
    """Ends queued live rows like the adapter: decide(row) -> ("sent", ids) | ("failed", error) | None (leave it)."""

    def __init__(self, home: Path, decide=None, delay: float = 0.0):
        self.home, self.delay = home, delay
        self.decide = decide or (lambda row: ("sent", [1000 + row["id"]]))
        self.seen: list[dict] = []
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> Consumer:
        self._t.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._t.join(5)

    def _run(self) -> None:
        while not self._stop.is_set():
            p = events_db(self.home)
            if p.exists():
                try:
                    self._pass(p)
                except sqlite3.OperationalError:
                    pass
            time.sleep(0.03)

    def _pass(self, p: Path) -> None:
        con = sqlite3.connect(p, timeout=5, isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            for r in con.execute("SELECT * FROM outbox WHERE status='queued' AND mode='live' ORDER BY id").fetchall():
                row = dict(r)
                verdict = self.decide(row)
                if verdict is None:
                    continue
                claimed = con.execute(
                    "UPDATE outbox SET status='sending', attempts=attempts+1 WHERE id=? AND status='queued'",
                    (row["id"],),
                ).rowcount
                if not claimed:
                    continue
                self.seen.append(row)
                if self.delay:
                    time.sleep(self.delay)
                if verdict[0] == "sent":
                    ids = verdict[1]
                    con.execute(
                        "UPDATE outbox SET status='sent', sent_ts=?, tg_message_id=?, result=? WHERE id=?",
                        (time.time(), ids[-1] if ids else None, json.dumps({"ids": ids, "notes": []}), row["id"]),
                    )
                else:
                    con.execute("UPDATE outbox SET status='failed', last_error=? WHERE id=?", (verdict[1], row["id"]))
        finally:
            con.close()


def stub_env(home: Path, **extra: str) -> dict[str, str]:
    return {**os.environ, **extra}
