"""Helpers for tests/loop (fakes, fixtures data). All values are fake."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

# the managed scripts' interpreter (their shebang); the hub's CI runner has none: its own Python
PY = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable

SIG = {
    "traces": {
        "owner_turns": 120,
        "owner_first_p50": 9000,
        "owner_first_p95": 52000,
        "tools": 10000,
        "errors": 150,
        "error_rate": 0.015,
        "error_counts": {"Bash:exit_code": 90, "Bash:hook:vault-guard": 30, "WebFetch:timeout": 6},
        "hook_blocks": 30,
        "by_trigger": {"heartbeat": {"main_cost": 1.5, "agent_cost": 2.5}},
    },
    "beats": {
        "beats": {
            "beat_turns": 25,
            "tokens": {"mean": 1000000, "p95": 2000000},
            "tokens_per_day": 25000000,
            "context_per_call": 240000,
            "cost_per_day": 7.9,
        }
    },
    "product": {
        "meta": {"owner_uses": 900},
        "features": [
            {
                "id": "gym.recap",
                "name": "gym recap",
                "area": "fitness",
                "owner_facing": True,
                "flags": ["broken"],
                "uses": 0,
                "errors": 40,
                "owner_errors": 0,
                "complaints": 0,
                "retries": 0,
                "metric": "one recap",
            },
            {
                "id": "skill.vault",
                "name": "vault",
                "area": "privacy",
                "owner_facing": True,
                "flags": ["friction"],
                "uses": 40,
                "errors": 0,
                "owner_errors": 0,
                "complaints": 4,
                "retries": 2,
                "metric": "no complaint",
            },
            {
                "id": "market.watchlist",
                "name": "market watchlist",
                "area": "misc",
                "owner_facing": True,
                "flags": ["broken"],
                "uses": 3,
                "errors": 9,
                "owner_errors": 1,
                "complaints": 0,
                "retries": 0,
            },
            {"id": "internal.x", "name": "x", "owner_facing": False, "flags": ["broken"], "errors": 99},
        ],
    },
    "evals": {
        "run_id": "r1",
        "score": 0.9,
        "passed": 9,
        "ran": 10,
        "regression": True,
        "delta": -0.1,
        "new_fail": ["case-a"],
        "fixed": [],
        "line": "evals r1: 0.90",
    },
    "policy": {
        "domains": {"home": {"calls": 50, "denies": 2, "unexpected": 1, "mode": "log", "ready": False}},
        "denies": [{"id": 7, "tool": "ha.set", "principal": "subagent", "acked": None, "reason": "x"}],
        "policy": {"valid": True},
    },
    "ledger": [{"id": "r1", "date": "2026-10-22", "verb": "removed", "thing": "market watchlist feature"}],
    "tools": ["reminders.add", "ha.set", "vault.ask"],
    "policy_check": {
        "reminders.add": {"allowed": True, "approval_needed": False},
        "ha.set": {"allowed": True, "approval_needed": True},
        "vault.ask": {"allowed": False, "reason": "no reads of private data"},
    },
}

FAKE = r"""#!{py}
import json, os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
fx = Path(os.environ["LOOP_FIXTURES"])
with open(fx / "calls.log", "a") as f:
    f.write(json.dumps([name] + sys.argv[1:]) + "\n")
sig = json.loads((fx / "sig.json").read_text())
a = sys.argv[1:]
def out(o):
    print(json.dumps(o)); sys.exit(0)
if name == "traces" and a[:1] == ["agents"]:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    t0 = datetime.strptime(a[a.index("--since") + 1], "%Y-%m-%d %H:%M").replace(
        tzinfo=ZoneInfo(os.environ.get("ASSISTANT_TZ") or "UTC")).timestamp()
    out([x for x in sig.get("agents", []) if x["start_ts"] >= t0])
if name == "traces": out(sig["traces"])
if name == "events": out(sig["beats"])
if name == "product": out(sig["product"])
if name == "evals" and a[:1] == ["summary"]: out(sig["evals"])
if name == "evals" and a[:1] == ["list"]:
    import yaml
    ids = []
    cdir = Path(os.environ["EVALS_CORPUS"]) / "cases"
    for p in sorted(cdir.glob("*.yaml")):
        doc = yaml.safe_load(p.read_text()) or {{}}
        for c in doc.get("cases") or []:
            ids.append({{"id": c["id"], "area": c.get("area"), "tags": c.get("tags")}})
    out(ids)
if name == "tool" and a[:1] == ["show"]:
    if a[1] in sig["tools"]: out({{"name": a[1]}})
    print("unknown tool", file=sys.stderr); sys.exit(2)
if name == "tool" and a[:2] == ["policy", "check"]: out(sig["policy_check"].get(a[2], {{"allowed": False}}))
if name == "tool" and a[:1] == ["policy"]: out(sig["policy"])
if name == "memory" and a[:1] == ["ledger"]: out(sig["ledger"])
if name == "memory": out({{"op": a[0], "id": "rNEW0000001"}})
if name == "memory2": print("ok"); sys.exit(0)
print("fake: unhandled", a, file=sys.stderr); sys.exit(9)
"""


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def make_mem_db(p: Path) -> None:
    con = sqlite3.connect(p)
    con.executescript(
        """
        CREATE TABLE facts(rowid INTEGER PRIMARY KEY, id TEXT UNIQUE, kind TEXT, subject TEXT, text TEXT, status TEXT);
        CREATE TABLE review(id TEXT PRIMARY KEY, cand_id TEXT, kind TEXT, fact_id TEXT, other_id TEXT, reason TEXT,
            score REAL, status TEXT, created TEXT, resolved TEXT);
        CREATE TABLE records(id TEXT PRIMARY KEY, doc TEXT, kind TEXT, section TEXT, pos REAL, text TEXT, claim TEXT,
            src TEXT, verb TEXT, thing TEXT, details TEXT, day TEXT, source TEXT, verified_at TEXT, valid_from TEXT,
            valid_to TEXT, scope TEXT, status TEXT, supersedes TEXT, superseded_by TEXT, origin TEXT, created TEXT,
            updated TEXT);
        """
    )
    rows = [
        (
            "rDEC0000001",
            "decision",
            "Done / declined ledger",
            "20.10 declined: zumba class prep (he said no)",
            "declined",
            "zumba class prep",
            "20.10",
            None,
            "2026-10-21T10:00:00",
        ),
        (
            "rFACT000001",
            "fact",
            "Home & HA",
            "Kitchen kettle: Brand A, 1.5 l — fixture, 10.10",
            None,
            None,
            None,
            "2026-10-10",
            "2026-10-10T10:00:00",
        ),
        (
            "rFACT000002",
            "fact",
            "Apps & services",
            "Gizmo bot: answers in English — fixture, 15.10",
            None,
            None,
            None,
            "2026-10-15",
            "2026-10-15T10:00:00",
        ),
    ]
    for rid, kind, sec, text, verb, thing, day, vf, created in rows:
        con.execute(
            "INSERT INTO records (id, doc, kind, section, pos, text, verb, thing, day, valid_from, status, created)"
            " VALUES (?, 'FACTS.md', ?, ?, 1, ?, ?, ?, ?, ?, 'active', ?)",
            (rid, kind, sec, text, verb, thing, day, vf, created),
        )
    con.execute(
        "INSERT INTO facts (id, kind, subject, text, status) VALUES ('fGIZMO', 'fact', 'gizmo',"
        " 'Gizmo bot: answers in English — fixture, 15.10', 'active')"
    )
    con.execute(
        "INSERT INTO review VALUES ('rv1', 'cGIZMO', 'conflict', NULL, 'fGIZMO', 'negation differs', 0.6,"
        " 'open', '2026-10-21 12:00', NULL)"
    )
    con.commit()
    con.close()


CANDS = [
    # a correction the brain already covered with a ledger row the same day
    {
        "id": "cZUMBA",
        "kind": "correction",
        "subject": "zumba class role",
        "text": "He does not prep the zumba class.",
        "cues": ["correction"],
        "strong": True,
        "conf": 0.81,
        "ts": "2026-10-20 13:31",
        "status": "auto",
        "raw": "PLANTEDRAW private words",
        "ctx": "PLANTEDCTX context words",
    },
    # a correction of an older fact, linked by memory2's review
    {
        "id": "cGIZMO",
        "kind": "correction",
        "subject": "gizmo bot",
        "text": "Gizmo bot answers in German now.",
        "cues": ["correction"],
        "strong": True,
        "conf": 0.81,
        "ts": "2026-10-21 11:55",
        "status": "conflict",
        "raw": "PLANTEDRAW",
        "ctx": "PLANTEDCTX",
    },
    # a preference with a strong correction cue and nothing related on file
    {
        "id": "cLAMP",
        "kind": "preference",
        "subject": "porch lamp schedule",
        "text": "Porch lamp stays off after 23:00.",
        "cues": ["correction"],
        "strong": True,
        "conf": 0.81,
        "ts": "2026-10-21 17:16",
        "status": "review",
        "raw": "PLANTEDRAW",
        "ctx": "PLANTEDCTX",
    },
    # not a correction
    {
        "id": "cPREF",
        "kind": "preference",
        "subject": "tea",
        "text": "Likes tea.",
        "cues": ["preference"],
        "strong": True,
        "conf": 0.9,
        "ts": "2026-10-21 18:00",
        "status": "review",
        "raw": "PLANTEDRAW",
        "ctx": "x",
    },
    # a correction outside the window
    {
        "id": "cOLD",
        "kind": "correction",
        "subject": "old thing",
        "text": "Old.",
        "cues": ["correction"],
        "strong": True,
        "conf": 0.9,
        "ts": "2026-09-01 10:00",
        "status": "auto",
        "raw": "PLANTEDRAW",
        "ctx": "x",
    },
]


def calls(home: Path) -> list[list[str]]:
    p = Path(os.environ["LOOP_FIXTURES"]) / "calls.log"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def set_sig(**kw: object) -> None:
    p = Path(os.environ["LOOP_FIXTURES"]) / "sig.json"
    d = json.loads(p.read_text())
    d.update(kw)
    p.write_text(json.dumps(d))
