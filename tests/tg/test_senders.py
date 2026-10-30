"""The managed senders on the one outbox: scripts/tg and scripts/tg-outbox. While the live adapter runs, tg's
text goes into the live outbox (urgent by default, --soft / TG_SOFT=1 non-urgent) and the direct sender never runs;
otherwise (no adapter, a switch, a closed gate) the direct path is exactly the old one, shadow row included. A row the
adapter fails is reported, never re-sent directly."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from outboxlib import OWNER, Consumer, gate_on, rows, stop_fake_adapter
from tg_testlib import PY, REPO, TG, stub, wait_for

OUTBOX = REPO / "scripts" / "tg-outbox"


def run_tg(home: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    stub(home, "sender", f'printf "%s\\n" "$@" >> "{home}/sent.txt"; exit 0')
    stub(home, "usage", f'echo "$@" >> "{home}/usage.txt"')
    e = {**os.environ, "TG_SEND_BIN": str(home / "stub" / "sender"), "TG_OUTBOX_BIN": str(OUTBOX), **(env or {})}
    return subprocess.run(["sh", str(TG), *args], capture_output=True, env=e, timeout=60)


def run_outbox(*args: str, stdin: bytes = b"") -> subprocess.CompletedProcess:
    return subprocess.run([PY, "-I", str(OUTBOX), *args], input=stdin, capture_output=True, timeout=60)


def usage_path(home: Path) -> str:
    assert wait_for(lambda: (home / "usage.txt").exists())
    return (home / "usage.txt").read_text()


def test_tg_goes_through_the_outbox_while_the_live_adapter_runs(home):
    p = gate_on(home)
    try:
        with Consumer(home):
            r = run_tg(home, "disk", "3 is hot — Übung")
            r2 = run_tg(home, "--soft", "weekly report")
            r3 = run_tg(home, "news", env={"TG_SOFT": "1"})
            r4 = run_tg(home, "--urgent", "now", env={"TG_SOFT": "1"})
    finally:
        stop_fake_adapter(p)
    assert [x.returncode for x in (r, r2, r3, r4)] == [0, 0, 0, 0], r.stderr
    assert not (home / "sent.txt").exists()  # the direct sender never ran
    rs = rows(home)
    assert [json.loads(x["body"])["text"] for x in rs] == ["disk 3 is hot — Übung", "weekly report", "news", "now"]
    assert [x["urgent"] for x in rs] == [1, 0, 0, 1]
    assert all(x["status"] == "sent" and x["chat"] == OWNER and json.loads(x["body"])["sender"] == "tg" for x in rs)
    assert "path=outbox" in usage_path(home)
    assert rows(home, "SELECT * FROM outbox WHERE mode='shadow'") == []  # the live row is the record


def test_tg_direct_path_unchanged_without_a_live_adapter(home):
    r = run_tg(home, "--soft", "hello", "there")
    assert r.returncode == 0
    assert (home / "sent.txt").read_text().splitlines() == ["hello", "there"]  # flag stripped, text as before
    assert "path=direct" in usage_path(home)
    assert rows(home) == []


def test_tg_switch_and_closed_gate_go_direct(home):
    p = gate_on(home)
    try:
        (home / ".config/tg-adapter/direct-tg").touch()
        assert run_tg(home, "one").returncode == 0
        (home / ".config/tg-adapter/direct-tg").unlink()
        cfg = home / ".config/tg-adapter/live.json"
        cfg.write_text(cfg.read_text().replace('"send": true', '"send": false'))
        assert run_tg(home, "two").returncode == 0
    finally:
        stop_fake_adapter(p)
    assert (home / "sent.txt").read_text().splitlines() == ["one", "two"]
    assert rows(home) == []


def test_tg_failed_row_is_reported_not_resent(home):
    p = gate_on(home)
    try:
        with Consumer(
            home, decide=lambda r: ("failed", "Call to 'sendMessage' failed! (403: Forbidden: bot was blocked)")
        ):
            r = run_tg(home, "x")
    finally:
        stop_fake_adapter(p)
    assert r.returncode == 1 and b"bot was blocked" in r.stderr
    assert not (home / "sent.txt").exists()
    assert "--outcome fail" in usage_path(home)


def test_tg_falls_back_when_the_adapter_dies_before_sending(home):
    p = gate_on(home)
    stop_fake_adapter(p)  # PID file left behind: the shell pre-check passes, the gate does not
    r = run_tg(home, "late")
    assert r.returncode == 0 and (home / "sent.txt").read_text() == "late\n"
    assert rows(home) == []


def test_outbox_cli_voice_file_buttons_and_status(home, tmp_path):
    ogg = tmp_path / "v.ogg"
    ogg.write_bytes(b"OggS")
    p = gate_on(home)
    try:
        with Consumer(home):
            r = run_outbox(
                "send",
                "tg-voice",
                "--file",
                str(ogg),
                "--caption",
                "on my way",
                "--duration",
                "7",
                "--silent",
                "--reply-to",
                "41",
            )
            b = run_outbox("send", "tg-buttons", "--buttons", '[[{"text":"Yes","data":"x:yes"}]]', stdin=b"Pick")
        g = run_outbox("gate", "tg-voice")
        s = run_outbox("status", "--json")
    finally:
        stop_fake_adapter(p)
    assert r.returncode == 0 and r.stdout.strip().isdigit(), r.stderr
    assert b.returncode == 0
    v, k = rows(home)
    assert json.loads(v["files"]) == [str(ogg)]
    assert json.loads(v["body"]) == {
        "text": "",
        "caption": "on my way",
        "duration": 7,
        "silent": True,
        "reply_to": 41,
        "sender": "tg-voice",
    }
    assert json.loads(k["body"])["buttons"] == [[{"text": "Yes", "data": "x:yes"}]]
    assert g.returncode == 0 and g.stdout.startswith(b"outbox:")
    st = json.loads(s.stdout)
    assert st["gates"]["tg"]["outbox"] is True
    assert st["rows_24h"]["tg-voice"] == {"sent": 1}
    assert run_outbox("gate", "tg").returncode == 1  # adapter gone
    assert run_outbox("send", "tg", "--text", "x").returncode == 75
    assert run_outbox("send", "tg", "--text", "  ").returncode == 2
    assert run_outbox("send", "tg-voice", "--file", str(tmp_path / "missing.ogg")).returncode == 2
