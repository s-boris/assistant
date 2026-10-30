"""scripts/tg and the hub outbox: its sends take the hub outbox through the real scripts/tg-outbox while the hub
answers and its adapter drains the outbox, and go directly otherwise: the hub down, its consumer stale, or a row its
adapter never claimed (withdrawn first, then
exactly one direct send). A fake hub on 127.0.0.1 and recording stubs only: no token, nothing reaches Telegram."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fakehub import FakeHub
from tg_testlib import REPO, TG, stub, wait_for

OUTBOX = REPO / "scripts" / "tg-outbox"
ROWS = '[[{"text":"Yes","data":"x:yes:1"},{"text":"No","data":"x:no:1"}]]'


@pytest.fixture
def fh(home: Path, monkeypatch: pytest.MonkeyPatch):
    h = FakeHub()
    tok = home / ".secrets" / "hub-token"
    tok.parent.mkdir(parents=True, exist_ok=True)
    tok.write_text("t" * 40)
    tok.chmod(0o600)
    monkeypatch.setenv("HUB_URL", h.url)
    monkeypatch.setenv("HUB_TOKEN_FILE", str(tok))
    cfg = home / ".config" / "tg-adapter"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "live.json").write_text(json.dumps({"mode": "live", "send": True, "allow_from": ["123"]}))
    (cfg / "live.off").write_text("")  # the in-container adapter is off, as after the cut-over
    (cfg / "live.hub").write_text("")
    yield h
    h.close()


def consumer(fh: FakeHub, *ages: float) -> None:
    left = list(ages)

    def h(m: str, p: str, b: object) -> tuple[int, object]:
        age = left.pop(0) if len(left) > 1 else left[0]
        return 200, {"consumers": [{"bot": "live", "caller": "tg", "age": age}]}

    fh.on("GET", "/outbox/consumers", h)


def hub_row(fh: FakeHub, rid: int, status: str = "sent") -> None:
    """POST /outbox answers rid; GET /outbox/rid answers `status` (sent: message id 500 + n)."""
    fh.on("POST", "/outbox", {"id": rid})
    res = json.dumps({"ids": [500 + rid % 100]}) if status == "sent" else None
    fh.on("GET", f"/outbox/{rid}", {"id": rid, "status": status, "attempts": 0, "result": res})


def outbox_shim(home: Path) -> Path:
    """scripts/tg-outbox run by this interpreter (the hub runner has no /usr/bin/python3 for its shebang)."""
    p = home / "stub" / "tg-outbox-py"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f'#!/bin/sh\nexec "{sys.executable}" -I "{OUTBOX}" "$@"\n')
    p.chmod(0o755)
    return p


def run_tg(home: Path, *args: str, outbox: str | None = None) -> subprocess.CompletedProcess:
    if not (home / "stub" / "sender").exists():
        stub(home, "sender", f'printf "%s\\n" "$*" >> "{home}/sent.txt"; exit 0')
        stub(home, "usage", f'echo "$@" >> "{home}/usage.txt"')
        stub(home, "adapter", "cat >/dev/null; exit 0")
        stub(home, "sleepin", '[ "$1" = gate ] && [ "$2" = -- ] || exit 9; shift 2; exec "$@"')
    env = {
        **os.environ,
        "TG_SEND_BIN": str(home / "stub" / "sender"),
        "TG_OUTBOX_BIN": outbox or str(outbox_shim(home)),
        "TG_ADAPTER_BIN": str(home / "stub" / "adapter"),
        "TG_SLEEPIN_BIN": str(home / "stub" / "sleepin"),
    }
    return subprocess.run(["sh", str(TG), *args], capture_output=True, env=env, stdin=subprocess.DEVNULL, timeout=60)


def sent(home: Path) -> list[str]:
    p = home / "sent.txt"
    return p.read_text().splitlines() if p.exists() else []


def usage_paths(home: Path, n: int) -> list[str]:
    p = home / "usage.txt"
    assert wait_for(lambda: p.exists() and len(p.read_text().splitlines()) >= n)
    return [w for ln in p.read_text().splitlines() for w in ln.split() if w.startswith("path=")]


def spooled(home: Path) -> int:
    p = home / ".local/state/assistant/hub-spool.db"
    if not p.exists():
        return 0
    con = sqlite3.connect(p)
    try:
        return int(con.execute("SELECT COUNT(*) FROM spool").fetchone()[0])
    finally:
        con.close()


def test_tg_takes_the_hub_outbox_while_the_hub_adapter_drains_it(home: Path, fh: FakeHub) -> None:
    consumer(fh, 0.5)
    hub_row(fh, 10000010)
    r = run_tg(home, "disk 3", "is hot — Übung")
    r2 = run_tg(home, "--soft", "weekly report")
    assert (r.returncode, r2.returncode) == (0, 0), (r.stderr, r2.stderr)
    assert sent(home) == []  # the direct sender never ran
    posted = fh.bodies("/outbox")
    assert [(b["body"]["text"], b["body"]["sender"], b["urgent"]) for b in posted] == [
        ("disk 3 is hot — Übung", "tg", True),
        ("weekly report", "tg", False),
    ]
    assert all(b["bot"] == "live" and b["chat"] == "123" and b["rid"] for b in posted)
    assert usage_paths(home, 2) == ["path=outbox", "path=outbox"]


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq missing")
def test_tg_buttons_take_the_hub_outbox(home: Path, fh: FakeHub) -> None:
    consumer(fh, 0.5)
    hub_row(fh, 10000011)
    r = run_tg(home, "--buttons", ROWS, "pick one")
    assert r.returncode == 0, r.stderr
    (b,) = fh.bodies("/outbox")
    assert (b["body"]["text"], b["body"]["sender"]) == ("pick one", "tg-buttons")
    assert b["body"]["buttons"] == json.loads(ROWS)
    assert sent(home) == []


def test_tg_goes_direct_when_the_hub_is_down_or_its_adapter_is_gone(home: Path, fh: FakeHub) -> None:
    fh.down = True
    assert run_tg(home, "hub down").returncode == 0
    fh.down = False
    consumer(fh, 95.0)
    assert run_tg(home, "adapter gone").returncode == 0
    assert sent(home) == ["hub down", "adapter gone"]
    assert not [c for c in fh.calls if c[1] == "/outbox"] and spooled(home) == 0  # nothing written, nothing spooled
    assert usage_paths(home, 2) == ["path=direct", "path=direct"]


def test_tg_withdraws_a_row_no_adapter_claimed_then_sends_it_directly_once(home: Path, fh: FakeHub) -> None:
    consumer(fh, 0.5, 120.0)  # drained at the gate, gone by the end of the wait
    hub_row(fh, 10000012, status="queued")
    fh.on("POST", "/outbox/10000012/withdraw", {"ok": True})
    r = run_tg(home, "--soft", "late news")  # soft: a 3 s wait
    assert r.returncode == 0, r.stderr
    assert len(fh.bodies("/outbox")) == 1 and len(fh.bodies("/outbox/10000012/withdraw")) == 1
    assert sent(home) == ["late news"]
    assert usage_paths(home, 1) == ["path=direct"]


def test_tg_keeps_a_held_row_while_the_hub_adapter_drains(home: Path, fh: FakeHub) -> None:
    consumer(fh, 0.5)
    hub_row(fh, 10000013, status="queued")  # held (quiet hours) by a running adapter
    r = run_tg(home, "--soft", "digest")
    assert r.returncode == 0 and b"queued as outbox #10000013" in r.stderr
    assert sent(home) == [] and not [c for c in fh.calls if "withdraw" in c[1]]


def test_outbox_status_and_gate_show_the_hub_path(home: Path, fh: FakeHub) -> None:
    consumer(fh, 2.0)
    fh.on(
        "POST",
        "/tg/q",
        {
            "rows": [
                {"id": 1, "bot": "live", "status": "sent", "body": json.dumps({"sender": "tg", "text": "MSG-ONE"})},
                {"id": 2, "bot": "live", "status": "queued", "body": {"sender": "tg", "text": "MSG-TWO"}},
                {"id": 3, "bot": "test", "status": "sent", "body": json.dumps({"sender": "tg"})},
                {"id": 4, "bot": "live", "status": "sent", "body": json.dumps({"sender": "event-card"})},
            ],
            "truncated": False,
        },
    )
    py = [sys.executable, "-I", str(OUTBOX)]
    g = subprocess.run([*py, "gate", "tg"], capture_output=True, text=True, timeout=30)
    assert g.returncode == 0
    assert g.stdout.strip() == "outbox: hub: the hub outbox, its consumer drained it 2 s ago"
    st = subprocess.run([*py, "status", "--json"], capture_output=True, text=True, timeout=30)
    d = json.loads(st.stdout)
    assert d["rows_from"] == "hub" and d["rows_24h"] == {"tg": {"sent": 1, "queued": 1}, "event-card": {"sent": 1}}
    assert d["store"] == {
        "outbox": "hub",
        "tg": "hub",
        "hub_consumer": True,
        "hub_consumer_why": "its consumer drained it 2 s ago",
    }
    assert all(v["outbox"] for v in d["gates"].values())
    assert "MSG-" not in st.stdout  # counts only, never the text
    q = fh.bodies("/tg/q")[0]
    assert q["name"] == "outbox_live" and q["params"]["until"] - q["params"]["since"] > 86000
    txt = subprocess.run([*py, "status"], capture_output=True, text=True, timeout=30).stdout
    assert "store: outbox hub · tg hub · hub outbox: its consumer drained it 2 s ago" in txt
    assert "live outbox (hub), 24 h: " in txt
