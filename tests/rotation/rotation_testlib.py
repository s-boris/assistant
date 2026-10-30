"""Helpers for the rotation tests (stub commands, transcript entries)."""

from __future__ import annotations

import json
import stat
from pathlib import Path

SID = "11111111-2222-3333-4444-555555555555"


def stub(path: Path, body: str) -> str:
    path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def entry(kind: str, ts: str, content) -> str:
    return json.dumps({"type": kind, "timestamp": ts, "message": {"content": content}})
