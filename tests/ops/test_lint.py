import json
from pathlib import Path

PY = b"#!/usr/bin/python3 -I\n"


def test_scan_python(tool):
    src = (
        PY
        + b"""
import fcntl, json
from urllib.request import urlopen
def _load_json(p): return json.load(open(p))
def atomic_write(p, d): pass
def write_note(t): pass
fcntl.flock(1, fcntl.LOCK_EX)
'''urlopen in a docstring does not count for flock'''
"""
    )
    assert tool.scan_source(Path("x"), src) == {"load_json", "save_atomic", "note_helpers", "urlopen", "flock"}
    assert tool.scan_source(Path("y.py"), b"s = 'flock and urlopen in text'\n") == set()


def test_scan_shell(tool):
    assert tool.scan_source(Path("s"), b"#!/bin/sh\nflock -n 9 || exit\n") == {"flock"}
    assert tool.scan_source(Path("s"), b"#!/bin/sh\npython3 - <<E\nurllib.request.urlopen(u)\nE\n") == {"urlopen"}


def test_ratchet_counts_and_overrides(tool, tmp_path):
    b = tmp_path / "bin"
    (b / "lib/core.releases/r1/core").mkdir(parents=True)
    (b / "lib/core.releases/r1/core/x.py").write_bytes(b"def load_json(): pass\n")
    (b / "one").write_bytes(PY + b"def load_json(p): pass\n")
    (b / "two").write_bytes(PY + b"import urllib.request\nurllib.request.urlopen('x')\n")
    (b / ".one.new").write_bytes(PY + b"def load_json(p): pass\n")
    (b / "lib/helper.py").write_bytes(b"def save_json(p, d): pass\n")
    c = tool.ratchet_counts(b)["counts"]
    assert c == {"load_json": 1, "save_atomic": 1, "note_helpers": 0, "urlopen": 1, "flock": 0}
    c = tool.ratchet_counts(b, {"one": PY + b"from core import x\n", "new": PY + b"def write_inbox(): pass\n"})[
        "counts"
    ]
    assert c["load_json"] == 0 and c["note_helpers"] == 1
    c = tool.ratchet_counts(b, exempt={"one": {"load_json"}})["counts"]
    assert c["load_json"] == 0


def test_ratchet_exempts_reviewed_managed_files(tool):
    root = Path(__file__).resolve().parents[2]
    assert tool.ratchet_exempt(root) == {
        "assistant": {"flock"},
        "lib/botlib.py": {"load_json", "save_atomic", "note_helpers"},  # botlib provides these helpers
        "remind": {"note_helpers", "flock"},  # write_note = the fallback; the store lock stays (owner path)
        "plans-watch": {"flock"},  # only the vault-plans note goes through the ledger
        "vault-events": {"flock"},  # only its note writers go through the ledger (guard-sensitive)
        "rave-radar": {"urlopen", "flock"},  # only its notes use the ledger; fetches + state lock in a later pass
        "unraid-health": {"flock"},  # the run lock stays
        "self-review": {"flock"},  # adopted as is; the run lock moves with a later core.locks pass
        "container-watch": {"save_atomic", "urlopen", "flock"},  # adopted as is; fetches, state, lock: a later pass
        "drive-watch": {"save_atomic"},  # adopted as is; its state + note writer: a later core pass
        # adopted as is; fetches, notes and locks move with a later core pass
        "cputemp-log": {"flock"},
        "fx-watch": {"urlopen"},
        "hwtrend": {"note_helpers"},
        "recall": {"flock"},
        "scraper-canary": {"urlopen", "note_helpers", "flock"},
        "test-all": {"flock"},
        "window-watch": {"urlopen", "note_helpers"},
        "wled-sync": {"note_helpers", "flock"},
        "gohome": {"urlopen"},  # adopted as is; its transport API fetch moves with a later core pass
        # adopted as is; store, fetches and locks move with a later core pass
        "divelog": {"load_json", "save_atomic", "flock"},
        "ha-dashboard-gen": {"urlopen"},
        "kk-compare": {"urlopen"},
        "lists": {"flock"},
        # adopted as is; JSON readers and run locks move with a later core pass
        "deadlines": {"load_json"},
        "deals": {"flock"},
        "disk-day-check": {"load_json"},
        # adopted as is; the board's Bot API call, notes and locks move with a later core pass
        "tg-status": {"urlopen", "flock"},
        "agent-watch": {"note_helpers", "flock"},
        # adopted as is; its problem-note writer moves with a later core.notes pass
        "voicecheck": {"note_helpers"},
        # adopted as is; JSON state, fetches, notes and locks move with a later core pass
        "arr-mismatch": {"flock", "note_helpers"},
        "arr-unblock": {"flock", "note_helpers", "urlopen"},
        "board-lint": {"load_json"},
        "cat-autopilot": {"load_json"},
        "cat-corner": {"load_json", "save_atomic"},
        "context": {"flock", "urlopen"},
        "dash-data": {"save_atomic", "urlopen"},
        "drive-brief": {"flock"},
        "emby": {"urlopen"},
        "gift-radar": {"flock"},
        "gym": {"urlopen"},
        "media": {"flock", "urlopen"},
        "menu-usage": {"flock"},
        "missing-docs": {"load_json"},
        "night-plan": {"load_json", "urlopen"},
        "parcels": {"flock"},
        "security-watch": {"flock"},
        "unifi-health": {"flock"},
        "unraid-capacity": {"flock", "note_helpers", "urlopen"},
        "voice-plan": {"flock"},
        # adopted as is; fetches and locks move with a later core pass
        "abx": {"urlopen"},
        "bills": {"flock"},
        "codebase-sync": {"flock"},
        "contracts": {"flock"},
        "push": {"flock"},
        "talk-button": {"flock"},
        "traces": {"flock"},
        "tts": {"urlopen"},
        # the ~/bin/lib helpers, adopted as is; a later core pass
        "lib/catstock.py": {"flock"},
        "lib/home_place.py": {"urlopen"},
        "lib/homectx.py": {"load_json", "save_atomic"},
        "lib/livecount.py": {"flock"},
        "lib/memory2.py": {"flock"},
        "lib/night_settime.py": {"flock", "note_helpers"},
        "lib/securefill.py": {"urlopen"},
        "lib/trackid_shazam.py": {"urlopen"},
        "lib/usage.py": {"flock"},
        "lib/webagent.py": {"flock"},
    }


def test_lint_bin_check_and_update(tool, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "ops/lint").mkdir(parents=True)
    base = repo / "ops/lint/bin-baseline.json"
    base.write_text(
        json.dumps({"counts": {"load_json": 0, "save_atomic": 0, "note_helpers": 0, "urlopen": 0, "flock": 0}})
    )
    monkeypatch.setenv("ASSISTANT_REPO", str(repo))
    b = tmp_path / "bin"
    b.mkdir()
    (b / "x").write_bytes(PY + b"def load_json(p): pass\n")
    assert tool.lint_bin("check", False, b) == 3
    assert tool.lint_bin("update", False, b) == 3  # never raises the baseline silently
    (b / "x").write_bytes(PY + b"pass\n")
    assert tool.lint_bin("check", False, b) == 0
    assert tool.lint_bin("update", False, b) == 0
    assert json.loads(base.read_text())["counts"]["load_json"] == 0


def test_ast_ban(tool, tmp_path):
    (tmp_path / "core").mkdir()
    (tmp_path / "core/jsonstate.py").write_text("def save_json(p, d): pass\n")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/w.py").write_text("import fcntl\ndef write_inbox(): pass\nfcntl.flock(0, 0)\n")
    (tmp_path / "ops").mkdir()
    (tmp_path / "ops/t.py").write_text("import fcntl\nfcntl.flock(0, 0)\n")
    rels = ["core/jsonstate.py", "scripts/w.py", "ops/t.py"]
    hits = tool.ast_ban(tmp_path, rels, {"ops/t.py": ["flock"]})
    assert hits == [
        "scripts/w.py:2: write_inbox outside core/ (use core helpers)",
        "scripts/w.py:3: flock outside core/ (use core helpers)",
    ]


def test_render_header(tool):
    out = tool.render(b"#!/bin/sh\necho\n", True)
    assert out.split(b"\n")[1] == tool.HEADER.encode()
    assert tool.render(out, True) == out
    assert tool.render(b"x = 1\n", True).startswith(tool.HEADER.encode())
    assert tool.render(b"{}", False) == b"{}"


def test_repo_manifest_is_valid(tool):
    root = Path(__file__).resolve().parents[2]
    m = tool.load_manifest(root)
    assert {e["dest"] for e in m["file"]} >= {"bin/assistant"} and m["core"]["dest"] == "bin/lib/core"
    src = (root / "ops/assistant.py").read_bytes()
    assert tool.render(src, True) == src  # the CLI carries its header itself (line numbers match the repo)
