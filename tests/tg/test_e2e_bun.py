"""End to end with the real bun adapter (adapters/tg) against the mock Bot API on a unix socket (no TCP port):
`tg-adapter ensure` starts it detached, an inbound message becomes a row, the outbox rules hold a non-urgent send in
quiet hours while an urgent one goes, status reads the adapter's own counts, stop ends it cleanly. Skipped when bun
or the adapter's node_modules are missing (CI installs them in its bun step)."""

from __future__ import annotations

import json
import os
import shutil
import signal
import sqlite3
import subprocess
from pathlib import Path

import pytest
from tg_testlib import REPO, run_cli, wait_for

ADAPTER = REPO / "adapters" / "tg"
BUN = shutil.which("bun") or "/usr/local/bin/bun"
pytestmark = pytest.mark.skipif(
    not (os.access(BUN, os.X_OK) and (ADAPTER / "node_modules").is_dir()),
    reason="bun or adapters/tg/node_modules missing",
)


def rows(home: Path, sql: str) -> list[tuple]:
    p = home / ".local/state/tg-adapter/test.db"
    if not p.exists():
        return []
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        return con.execute(sql).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()


def test_ensure_runs_the_real_adapter_offline(home, tmp_path, monkeypatch):
    sock = tmp_path / "bot.sock"
    calls = tmp_path / "calls.json"
    mock = subprocess.Popen([BUN, "test/mockserve.ts", str(sock), str(calls), "ping"], cwd=ADAPTER)
    try:
        assert wait_for(sock.exists, 10)
        tok = home / "tok.env"
        tok.write_text("TELEGRAM_BOT_TOKEN=4242:TEST-not-a-real-token\n")  # secretscan:allow (fake token)
        tok.chmod(0o600)
        cfg = home / ".config/tg-adapter/config.json"
        cfg.parent.mkdir(parents=True)
        cfg.write_text(
            json.dumps(
                {
                    "mode": "test",
                    "bot": "testadapter_bot",
                    "token_file": "~/tok.env",
                    "allow_from": ["111"],
                    "api_root": "http://mock.invalid",
                    "api_unix_socket": str(sock),
                    "send": True,
                    "poll_timeout_s": 1,
                    "outbox_interval_ms": 50,
                    "quiet_hours": "0-24",
                }
            )
        )
        monkeypatch.setenv("TG_ADAPTER_DIR", str(ADAPTER))
        monkeypatch.setenv("TG_ADAPTER_TEST", "1")
        r = run_cli("ensure")
        assert r.returncode == 0 and r.stdout.startswith(b"tg-adapter: started (pid "), r.stdout + r.stderr
        assert wait_for(lambda: rows(home, "SELECT kind FROM events") == [("tg.message",)], 15)
        assert run_cli("enqueue", "send", "--chat", "111", "--body", '{"text":"held at night"}').returncode == 0
        assert run_cli("enqueue", "send", "--chat", "111", "--body", '{"text":"urgent"}', "--urgent").returncode == 0

        def sent() -> list[str]:
            try:
                return [c["params"].get("text") for c in json.loads(calls.read_text()) if c["method"] == "sendMessage"]
            except (OSError, ValueError):
                return []

        assert wait_for(lambda: sent() == ["urgent"], 15)
        assert rows(home, "SELECT status FROM outbox ORDER BY id") == [("queued",), ("sent",)]
        s = json.loads(run_cli("status", "--json").stdout)
        assert (s["running"], s["token_present"], s["adapter"]["events"]) == (True, True, {"tg.message": 1})
        assert s["adapter"]["outbox"] == {"queued": 1, "sent": 1}
        assert run_cli("ensure").stdout.startswith(b"tg-adapter: running (pid ")
        r = run_cli("stop")
        assert r.returncode == 0 and r.stdout.startswith(b"tg-adapter: stopped pid ")
        log = (home / ".local/state/tg-adapter/test/ensure.log").read_text()
        assert wait_for(lambda: " exit 0" in (home / ".local/state/tg-adapter/test/ensure.log").read_text(), 10), log
        assert "polling as @testadapter_bot" in (home / ".local/state/tg-adapter/test/adapter.log").read_text()
    finally:
        mock.send_signal(signal.SIGTERM)
        mock.wait(10)
