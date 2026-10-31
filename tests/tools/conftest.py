"""Fixtures for the typed tool registry: a temp remind store (the repo's scripts/remind, fixed clock, stub sender)
and a temp gateway state. Usage events go to a session temp dir (USAGE_STATE is read when usage.py is imported)."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
os.environ.setdefault("USAGE_STATE", tempfile.mkdtemp(prefix="tools-usage."))
os.environ.setdefault("TOOLS_CONFIG", tempfile.mkdtemp(prefix="tools-config."))  # the direct switches: never ~/.config
os.environ.pop("TRACEPARENT", None)


@pytest.fixture
def remind_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    tg = tmp_path / "tg"
    tg.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {tmp_path / 'tg.log'}\n")
    tg.chmod(tg.stat().st_mode | stat.S_IXUSR)
    for k, v in {
        "REMIND_DB": tmp_path / "rem.json",
        "REMIND_INBOX": tmp_path / "inbox",
        "REMIND_TG": tg,
        "REMIND_TG_BUTTONS": tg,
        "REMIND_NOW": "2026-10-20 12:00",
        "TRIPCLOCK_CONFIG": tmp_path / "trips.json",
        "TRIPCLOCK_FLIGHTS": tmp_path / "no-flights",
        "TOOLS_REMIND_BIN": REPO / "scripts" / "remind",
        "TOOLS_STATE": tmp_path / "state",
    }.items():
        monkeypatch.setenv(k, str(v))
    monkeypatch.delenv("TRACEPARENT", raising=False)
    return tmp_path


class Stubs:
    """Fake CLIs for the domain tools: each answers by the longest matching argv prefix (rules per binary) and logs
    its argv. Nothing real (vault, HA, calendar, media, browser, server, phone) is ever called."""

    BINS = {
        "assistant": "TOOLS_ASSISTANT_BIN",
        "vault": "TOOLS_VAULT_BIN",
        "ha": "TOOLS_HA_BIN",
        "calcache": "TOOLS_CALCACHE_BIN",
        "tripclock": "TOOLS_TRIPCLOCK_BIN",
        "plans-watch": "TOOLS_PLANSWATCH_BIN",
        "media": "TOOLS_MEDIA_BIN",
        "webagent": "TOOLS_WEBAGENT_BIN",
        "abx": "TOOLS_ABX_BIN",
        "securefill": "TOOLS_SECUREFILL_BIN",
        "cat-autopilot": "TOOLS_CAT_AUTOPILOT_BIN",
        "unraid-health": "TOOLS_UNRAID_HEALTH_BIN",
        "unraid-capacity": "TOOLS_UNRAID_CAPACITY_BIN",
        "container-watch": "TOOLS_CONTAINER_WATCH_BIN",
        "unraid-ssh": "TOOLS_UNRAID_SSH_BIN",
        "phone": "TOOLS_PHONE_BIN",
    }
    SCRIPT = """#!{py}
import json, os, sys
name, argv = os.path.basename(sys.argv[0]), sys.argv[1:]
with open({log!r}, "a") as f:
    f.write(json.dumps({{"bin": name, "argv": argv}}) + "\\n")
rules = json.load(open({rules!r})).get(name, [])
best = max((r for r in rules if argv[: len(r["argv"])] == r["argv"]), key=lambda r: len(r["argv"]), default=None)
if best is None:
    sys.stderr.write(name + ": no stub for " + " ".join(argv) + "\\n")
    sys.exit(2)
if best.get("sleep"):
    import time
    time.sleep(best["sleep"])
sys.stdout.write(best.get("out", ""))
sys.stderr.write(best.get("err", ""))
sys.exit(best.get("rc", 0))
"""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import json as _json
        import sys as _sys

        self._json = _json
        self.dir = root / "stubbin"
        self.dir.mkdir()
        self.log = root / "stub.log"
        self.rules_f = root / "stub-rules.json"
        self.rules: dict[str, list[dict]] = {}
        self._save()
        for name, env in self.BINS.items():
            p = self.dir / name
            p.write_text(self.SCRIPT.format(py=_sys.executable, log=str(self.log), rules=str(self.rules_f)))
            p.chmod(0o755)
            monkeypatch.setenv(env, str(p))
        monkeypatch.setenv("TOOLS_STATE", str(root / "state"))
        monkeypatch.delenv("TRACEPARENT", raising=False)

    def _save(self) -> None:
        self.rules_f.write_text(self._json.dumps(self.rules))

    def on(self, name: str, argv: list[str], out: object = "", rc: int = 0, err: str = "", sleep: float = 0) -> Stubs:
        """sleep: the fake answers after this many seconds (a slow backend)."""
        text = out if isinstance(out, str) else self._json.dumps(out)
        self.rules.setdefault(name, []).append({"argv": argv, "out": text, "rc": rc, "err": err, "sleep": sleep})
        self._save()
        return self

    def calls(self, name: str | None = None) -> list[list[str]]:
        if not self.log.exists():
            return []
        rows = [self._json.loads(x) for x in self.log.read_text().splitlines()]
        return [r["argv"] for r in rows if name is None or r["bin"] == name]


@pytest.fixture
def stubs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Stubs:
    return Stubs(tmp_path, monkeypatch)
