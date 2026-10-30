"""Hermetic fixtures for the rotation: temp HOME (ASSISTANT_TEST=1 refuses the real one), a fake project folder
with a transcript, and stub commands for every outside read (agents, agent-watch, voice, driving, approvals, remind,
board-lint, events, tripclock, restart)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rotation_testlib import SID, entry, stub  # noqa: E402


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:

    from core.paths import protected

    assert not protected(tmp_path), "tmp_path must lie outside the real home (ci_scratch() aside)"
    h = tmp_path / "home"
    for d in ("inbox", "workspace", "bin", ".config/assistant", ".local/state/assistant", "jobs"):
        (h / d).mkdir(parents=True, exist_ok=True)
    for k in list(os.environ):
        if k.startswith(("ASSISTANT_", "ROTATE_")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("ASSISTANT_TEST", "1")
    monkeypatch.setenv("ASSISTANT_TZ", "Europe/Zurich")
    monkeypatch.setenv("TZ", "Europe/Zurich")
    proj = tmp_path / "projects"
    proj.mkdir()
    monkeypatch.setenv("ASSISTANT_PROJECT_DIR", str(proj))
    (h / "workspace" / "TODO.md").write_text(
        "# TODO\n\n## Due\n- **Morning report** — 09:00 25.10\n\n## In progress\n- **Build X** — running\n\n"
        "## Waiting on you\n- **Pick a slot** — asked\n",
        encoding="utf-8",
    )
    return h


@pytest.fixture
def world(home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A quiet night: 03:30 his time, idle session up 2 h, no agents, no call, not driving, nothing pending."""
    bins = tmp_path / "stubs"
    bins.mkdir()
    monkeypatch.setenv("ASSISTANT_NOW", "2026-10-25T03:30:00+01:00")
    now_ms = 1792895400 * 1000  # 2026-10-25T03:30+01:00
    agents = tmp_path / "agents.json"
    agents.write_text(
        json.dumps(
            [
                {
                    "cwd": str(home / "workspace"),
                    "kind": "interactive",
                    "sessionId": SID,
                    "status": "idle",
                    "startedAt": now_ms - 2 * 3600 * 1000,
                }
            ]
        )
    )
    monkeypatch.setenv("ROTATE_AGENTS_JSON", str(agents))
    monkeypatch.setenv("ROTATE_AGENT_WATCH", stub(bins / "aw", "echo 'agent-watch: subagents (none)'"))
    monkeypatch.setenv("ROTATE_VOICE", stub(bins / "voice", 'echo \'{"live": 0, "call_ids": []}\''))
    monkeypatch.setenv("ROTATE_DRIVING", stub(bins / "driving", "exit 1"))
    monkeypatch.setenv("ROTATE_APPROVALS", stub(bins / "appr", "echo '[]'"))
    monkeypatch.setenv("ROTATE_TRIPCLOCK", stub(bins / "tc", "echo Europe/Zurich"))
    remind = {
        "now": "x",
        "items": [
            {"id": "r1", "kind": "timed", "at": "2026-10-25 08:30", "owner": True, "text": "his thing", "tag": ""},
            {
                "id": "r2",
                "kind": "timed",
                "at": "2026-10-25 10:00",
                "owner": False,
                "text": "my follow-up",
                "tag": "wake",
            },
            {"id": "r3", "kind": "timed", "at": "2026-11-20 10:00", "owner": True, "text": "far away", "tag": ""},
            {"id": "r4", "kind": "place", "at": None, "owner": True, "text": "at home", "tag": ""},
        ],
    }
    rf = tmp_path / "remind.json"
    rf.write_text(json.dumps(remind))
    monkeypatch.setenv("ROTATE_REMIND", stub(bins / "remind", f"cat '{rf}'"))
    monkeypatch.setenv("ROTATE_BOARD_LINT", stub(bins / "bl", "echo '[handoff] header'; echo 'Due: Morning report'"))
    monkeypatch.setenv("ROTATE_EVENTS", stub(bins / "ev", 'echo \'{"new": [1, 2], "open": []}\''))
    restart_log = tmp_path / "restart.log"
    monkeypatch.setenv("ROTATE_RESTART", stub(bins / "restart", f"echo \"$1\" >> '{restart_log}'"))
    sc = tmp_path / "start-claude"
    sc.write_text("#!/bin/sh\nfresh=$HOME/.local/state/assistant/rotate/fresh-start\n")
    monkeypatch.setenv("ROTATE_START_CLAUDE", str(sc))
    (home / ".config" / "assistant" / "rotate-on").write_text("{}")
    tp = tmp_path / "projects" / f"{SID}.jsonl"
    tp.write_text(
        "\n".join(
            [
                entry("user", "2026-10-24T22:10:00Z", '<channel source="telegram" user="o">hi</channel>'),
                entry("assistant", "2026-10-24T22:11:00Z", [{"type": "text", "text": "ok"}]),
                entry("user", "2026-10-25T00:00:00Z", "[heartbeat] beat"),
            ]
        )
        + "\n"
    )
    return {
        "bins": bins,
        "transcript": tp,
        "restart_log": restart_log,
        "agents": agents,
        "start_claude": sc,
        "now_ms": now_ms,
    }
