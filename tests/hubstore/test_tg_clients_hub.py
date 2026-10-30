"""Brain-side hub clients against the real hubd + Postgres (hubfix): receipts, brain-watch reads, tgmetrics, tg-bind,
core.tg sends and the bridges' hand-offs. Skips until the hub side (packages/hub/hubtg.py) is in the
tree."""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo

import hubfix
import pytest

REPO = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(not (REPO / "packages/hub/hubtg.py").exists(), reason="hub side of H6 not in the tree")


@pytest.fixture(scope="module")
def hubsrv(tmp_path_factory):
    why = hubfix.missing()
    if why:
        pytest.skip(why)
    root = tmp_path_factory.mktemp("hub6")
    pg = hubfix.Pg(root).start()
    hub = hubfix.Hubd(root, pg, hubfix.vendor_tree(root / "vendor")).start()
    yield hub
    hub.stop()
    pg.discard()


@pytest.fixture
def env(tmp_path, monkeypatch, hubsrv):
    schema = "t_" + uuid.uuid4().hex[:12]
    home = tmp_path / "home"
    for d in (".config/assistant", ".config/tg-adapter", "inbox", "workspace"):
        (home / d).mkdir(parents=True)
    (home / ".config/tg-adapter/live.json").write_text(json.dumps({"mode": "live", "send": True, "allow_from": ["5"]}))
    tok = tmp_path / "hub-token"
    tok.write_text(hubsrv.tokens["brain"])
    os.chmod(tok, 0o600)
    for k, v in {
        "HOME": str(home), "ASSISTANT_TEST": "1", "HUB_URL": hubsrv.url, "HUB_TOKEN_FILE": str(tok),
        "HUB_SCHEMA": schema, "ASSISTANT_MAIN_CWD": str(home / "workspace"), "TZ": "Europe/Zurich",
        "TG_ADAPTER_TOOLS": str(REPO / "schemas" / "tools.toml"),
    }.items():  # fmt: skip
        monkeypatch.setenv(k, v)

    def tg_rows(rows):
        st, got = hubsrv.call("POST", "/tg/rows", {"rows": rows, "rid": uuid.uuid4().hex}, who="tg", schema=schema)
        assert st == 200, got
        return got["ids"]

    yield home, schema, tg_rows
    hubsrv.call("POST", "/admin/drop", {"schema": schema})


def _row(kind, ts, payload, key, source="tg-adapter"):
    return {"ts": ts, "source": source, "kind": kind, "subject": "", "next": "", "payload": json.dumps(payload),
            "labels": "[]", "delivery": "now", "trace_id": None, "idem_key": key, "status": "new"}  # fmt: skip


def test_receipts_watch_metrics_on_the_hub(env):
    from inbox import brainwatch, receipts, tgmetrics

    home, schema, tg_rows = env
    now = time.time()
    a, b = tg_rows(
        [
            _row("tg.message", now - 600, {"message_id": 70, "update_id": 1, "bot": "x", "text": "hi"}, "k1"),
            _row("tg.button", now - 500, {"message_id": 71, "update_id": 2, "bot": "x", "ack_ms": 35}, "k2"),
        ]
    )
    assert sorted(r["id"] for r in receipts.undelivered_hub(now, 300)) == sorted([a, b])
    p = f'<channel source="plugin:telegram-plus:telegram" event_id="{a}" message_id="70">hi</channel>'
    assert receipts.record(p, now=now, timeout=30)["receipts"] == [a]  # the hook's 0.3 s spools under CI load
    bad = f'<channel source="plugin:telegram-plus:telegram" event_id="{b}" message_id="99">x</channel>'
    assert receipts.record(bad, now=now, timeout=30)["skipped"] == 1  # message id mismatch: no receipt
    assert [r["id"] for r in brainwatch.Facts(None, now).pending()] == [b]
    assert receipts.mark_hub(b, now, "transcript") == "ok"
    assert brainwatch.Facts(None, now).pending() == []
    res = tgmetrics.compute(sqlite3.connect(":memory:"), now - 3600, now, now=now, logs={}, tz=ZoneInfo("UTC"))
    assert res["store"] == "hub" and res["T1"]["n"] == 1 and res["T1"]["ok"] is True
    assert tgmetrics.prune_text(sqlite3.connect(":memory:"), now + 40 * 86400) == 2
    assert tgmetrics.prune_text(sqlite3.connect(":memory:"), now + 40 * 86400) == 0


def test_tg_bind_and_send_on_the_hub(env, hubsrv, capsys):
    from tgadapter import bind

    from core import tg

    home, schema, _ = env
    assert bind.main(["add", "command", "night", "tg-status.show", "--why", "board", "--instance", "live"]) == 0
    assert bind.main(["list", "--json", "--instance", "live"]) == 0
    capsys.readouterr()
    assert bind.main(["list", "--json", "--instance", "live"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["key"] for r in rows] == ["night"]
    assert bind.main(["rm", "command", "night", "--instance", "live"]) == 0
    assert bind.main(["list", "--json", "--instance", "live"]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1]) == []
    via, why = tg.route("tg")
    assert via == "direct" and why == "hub: no consumer drains it"  # its adapter never polled: direct path
    assert tg.send("tg", "send", {"text": "hello"}, wait=0.3).status == "direct"
    st, _ = hubsrv.call("POST", "/outbox/queued", {"bots": ["live"]}, who="tg", schema=schema)  # the hub's adapter
    assert st == 200
    assert tg.route("tg")[0] == "hub"
    out = tg.send("tg", "send", {"text": "hello"}, wait=0.3)
    assert out.status == "queued" and out.row_id and out.row_id >= 10_000_000  # unclaimed while it drains: it waits
    assert out.via == "hub"


def test_hand_offs_reach_the_bridges_inbound_poll_without_a_local_token(env, hubsrv):
    """`hub voice inbox` / `hub phone inbox` in hub mode: the hand-off is the bridge's own bridge.handoff row (no
    ~/.secrets/voice-local-token anywhere), which the bridge's inbound long poll returns; a repeat of the same voice
    id writes nothing twice (the bridge's idem key)."""
    from hubkit import phone as HP
    from hubkit import voice as HV

    home, schema, _ = env
    (home / ".local/state/start-claude").mkdir(parents=True)
    (home / ".local/state/start-claude/telegram-store").write_text("hub\n")
    assert not (home / ".secrets/voice-local-token").exists()
    ch = {"id": "v0123abcd", "task": "is the train on time", "urgency": "now"}
    assert HV.to_bridge(ch) and HV.to_bridge(ch)
    assert HP.to_bridge({"task": "hi from the app", "msg_id": "m_01HZX3K9QW8E7R6T5Y4U3I2O1P", "device_id": "dev_1"})
    st, got = hubsrv.call("POST", "/tg/inbound/poll", {"after": 0, "wait": 0}, who="brain", schema=schema)
    assert st == 200, got
    rows = [r for r in got["rows"] if r["kind"] == "bridge.handoff"]
    pays = [r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"]) for r in rows]
    assert [(p["via"], p["chat_id"]) for p in pays] == [("voice", "voice"), ("app", "app")]
    assert pays[0]["voice_id"] == "v0123abcd" and pays[0]["text"].startswith(
        "🗣️ (live voice, now) is the train on time\n"
    )
    assert pays[1]["app_msg_id"] == "m_01HZX3K9QW8E7R6T5Y4U3I2O1P" and pays[1]["text"].startswith("📱 (app) hi from")
