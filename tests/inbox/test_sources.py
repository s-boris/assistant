"""Running work (agent-watch table or JSON) and the incremental compaction scan."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from inbox import sources
from inbox_testlib import AGENT_TABLE, fake_bin

from core import db


def test_agent_table_parse() -> None:
    a = sources.parse_agent_table(AGENT_TABLE)
    assert [(x["id"], x["state"], x["silent_min"]) for x in a] == [
        ("a1545e9bee6fea8aa", "running", 0),
        ("ac490683e1cb7d37e", "stalled", 20),
    ]
    assert a[0]["desc"] == "T event ledger + agenda"
    assert sources.parse_agent_table("garbage") == []


def test_agents_json_none_and_failure(home: Path) -> None:
    fake_bin(
        home,
        "agent-watch",
        'echo \'{"agents": [{"id": "x1", "state": "running", "silent_min": 1,'
        ' "description": "J"}, {"id": "x2", "state": "done"}]}\'',
    )
    assert sources.agents() == [{"id": "x1", "state": "running", "silent_min": 1, "desc": "J"}]
    fake_bin(home, "agent-watch", "echo 'agent-watch: (none)'")
    assert sources.agents() == []
    fake_bin(home, "agent-watch", "exit 3")
    assert sources.agents() is None
    fake_bin(home, "agent-watch", "sleep 3")
    t0 = time.monotonic()
    assert sources.agents(timeout=0.3) is None and time.monotonic() - t0 < 2


def boundary(ts: str, pre: int, post: int) -> str:
    return json.dumps(
        {
            "type": "system",
            "subtype": "compact_boundary",
            "timestamp": ts,
            "compactMetadata": {"trigger": "auto", "preTokens": pre, "postTokens": post, "durationMs": 51000},
        }
    )


def test_compaction_scan_incremental_and_content_free(home: Path) -> None:
    proj = Path(os.environ["ASSISTANT_PROJECT_DIR"])
    f = proj / "sess-1.jsonl"
    private = json.dumps({"type": "user", "message": {"content": 'PRIVATE-TEXT mentions "compact_boundary"'}})
    f.write_text(
        "\n".join([private, boundary("2031-06-15T10:00:00.000Z", 367000, 25000), private]) + "\n", encoding="utf-8"
    )
    con = db.connect()
    r = sources.scan_compactions(con, time.time())
    assert r["found"] == 1
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(boundary("2031-06-15T12:00:00.000Z", 370000, 30000) + "\n" + '{"type": "assistant", "partial')
    r = sources.scan_compactions(con, time.time())
    assert r["found"] == 1 and r["read"] < 400  # only the new bytes
    with open(f, "a", encoding="utf-8") as fh:  # the partial line completes later
        fh.write('": 1}\n' + boundary("2031-06-15T14:00:00.000Z", 371000, 31000) + "\n")
    assert sources.scan_compactions(con, time.time())["found"] == 1
    comps = db.meta_get(con, "compactions")
    assert [c["pre"] for c in comps] == [367000, 370000, 371000]
    assert sources.last_compaction(con)["post"] == 31000
    raw = json.dumps(db.meta_get(con, "compactions")) + json.dumps(db.meta_get(con, "compact_scan"))
    assert "PRIVATE" not in raw
    assert sources.scan_compactions(con, time.time())["found"] == 0  # idempotent


def test_compaction_scan_no_transcript(home: Path) -> None:
    assert sources.scan_compactions(db.connect(), time.time()) == {"file": None, "read": 0, "found": 0}
