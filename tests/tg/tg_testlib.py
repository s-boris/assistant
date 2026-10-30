"""Helpers for the tg-adapter tests: stub commands, the fake bun (adapter process stand-in), config/token
writers, runners for scripts/tg-adapter and DB readers."""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TG_ADAPTER = REPO / "scripts" / "tg-adapter"
TG = REPO / "scripts" / "tg"
# the managed scripts' interpreter (their shebang), here and in the hub CI runner (no /usr/bin/python3 there)
PY = "/usr/local/bin/python3.13" if os.path.exists("/usr/local/bin/python3.13") else sys.executable

FAKE_BUN = r"""#!/usr/bin/python3
# Stand-in for `bun`: "install --frozen-lockfile" makes node_modules; "<cli> check|run|status" act like the adapter.
import json, os, signal, sys, time
if sys.argv[1] == "install":
    os.makedirs("node_modules", exist_ok=True)
    sys.exit(int(os.environ.get("FAKE_INSTALL_RC", "0")))
cmd = sys.argv[2]
home = os.environ["HOME"]
ctl = os.path.join(home, "fake-bun")
def setting(name, default):
    try:
        return open(os.path.join(ctl, name)).read().strip()
    except OSError:
        return default
with open(os.path.join(ctl, "calls"), "a") as f:
    f.write(" ".join(sys.argv[2:]) + "\n")
if cmd == "check":
    rc = int(setting("check_rc", "0"))
    if rc:
        print("tg-adapter: config is not valid JSON: x", file=sys.stderr)
    sys.exit(rc)
if cmd == "status":
    print(json.dumps({"offset": 7, "events": {"tg.message": 1}, "outbox": {}, "callback_ack_ms": {"n": 0}}))
    sys.exit(0)
inst = sys.argv[sys.argv.index("--instance") + 1] if "--instance" in sys.argv else "test"
if cmd == "companion":  # H6: the brain-side companion of a hub-mode adapter
    if len(sys.argv) > 3 and sys.argv[3] == "status":
        print(json.dumps({"running": True, "jobs": 3, "failed": 0}))
        sys.exit(0)
    state = os.path.join(home, ".local/state/tg-adapter", inst)
    os.makedirs(state, exist_ok=True)
    pidf = os.path.join(state, "companion.pid")
    open(pidf, "w").write(f"{os.getpid()}\ntg-adapter-companion\n")
    def cbye(*_):
        os.remove(pidf)
        sys.exit(0)
    signal.signal(signal.SIGTERM, cbye)
    for _ in range(600):
        time.sleep(0.1)
    cbye()
if cmd == "run":
    state = os.path.join(home, ".local/state/tg-adapter", inst)
    os.makedirs(state, exist_ok=True)
    pidf = os.path.join(state, "adapter.pid")
    mode = setting("run", "serve")
    open(pidf, "w").write(f"{os.getpid()}\ntg-adapter\n")
    def bye(*_):
        os.remove(pidf)
        sys.exit(0)
    signal.signal(signal.SIGTERM, bye)
    if mode.startswith("exit:"):
        time.sleep(0.2)
        os.remove(pidf)
        sys.exit(int(mode[5:]))
    for _ in range(600):
        time.sleep(0.1)
    bye()
sys.exit(2)
""".replace("#!/usr/bin/python3\n", f"#!{sys.executable}\n", 1)  # the hub runner has no /usr/bin/python3


def stub(h: Path, name: str, body: str) -> Path:
    p = h / "stub" / name
    p.write_text(f"#!/bin/sh\n{body}\n")
    p.chmod(0o755)
    return p


def kill_adapter(h: Path) -> None:
    for f in ("adapter.pid", "test/adapter.pid", "live/adapter.pid", "test/companion.pid", "live/companion.pid"):
        try:
            pid = int((h / ".local/state/tg-adapter" / f).read_text().split()[0])
            os.kill(pid, signal.SIGKILL)
        except (OSError, ValueError, IndexError):
            pass


def fake(home: Path, name: str, value: str) -> None:
    (home / "fake-bun" / name).write_text(value)


def fake_calls(home: Path, raw: bool = False) -> list[str]:
    """The fake bun's calls; the default instance's "--instance test" left out unless raw."""
    try:
        lines = (home / "fake-bun" / "calls").read_text().splitlines()
    except OSError:
        return []
    return lines if raw else [ln.replace(" --instance test", "") for ln in lines]


def write_config(home: Path, **extra) -> Path:
    import json

    p = home / ".config" / "tg-adapter" / "config.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    cfg = {"mode": "test", "bot": "testadapter_bot", "token_file": "~/tok/test.env", "allow_from": ["111"], **extra}
    p.write_text(json.dumps(cfg))
    return p


def write_token(home: Path, mode: int = 0o600) -> Path:
    p = home / "tok" / "test.env"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("TELEGRAM_BOT_TOKEN=4242:TEST-not-a-real-token\n")  # secretscan:allow (fake token)
    p.chmod(mode)
    return p


def run_cli(*args: str, stdin: bytes | None = None, env: dict | None = None, timeout: float = 60):
    return subprocess.run(
        [PY, "-I", str(TG_ADAPTER), *args],
        input=stdin,
        capture_output=True,
        env={**os.environ, **(env or {})},
        timeout=timeout,
    )


def db_rows(home: Path, sql: str, args=()) -> list[sqlite3.Row]:
    p = home / ".local/state/assistant/events.db"
    if not p.exists():
        return []
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(sql, args).fetchall()
    except sqlite3.OperationalError:  # created, schema not committed yet (a background writer)
        return []
    finally:
        con.close()


def wait_for(pred, secs: float = 8.0) -> bool:
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return bool(pred())
