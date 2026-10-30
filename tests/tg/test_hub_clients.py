"""Brain-side hub clients: core.hub/core.tg, receipts, brain-watch, tgmetrics, tg-bind, tg-adapter ensure/status
against a fake hub (tests/tg/fakehub.py). The hub-less SQLite store is the other suites' job."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fakehub import FakeHub
from tg_testlib import fake


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
    yield h
    h.close()


def consumer(fh: FakeHub, *ages: float, bot: str = "live") -> None:
    """GET /outbox/consumers: the hub's adapter seen `age` s ago; several ages = one per call, the last one stays."""
    left = list(ages)

    def h(m: str, p: str, b: object) -> tuple[int, object]:
        age = left.pop(0) if len(left) > 1 else left[0]
        return 200, {"consumers": [{"bot": bot, "caller": "tg", "age": age}]}

    fh.on("GET", "/outbox/consumers", h)


def test_backend_is_the_hub_unless_a_test_runs_without_one(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from core import hub

    monkeypatch.delenv("HUB_URL", raising=False)
    assert hub.backend("tg") == "sqlite" and not hub.tg_hub()
    monkeypatch.setenv("HUB_URL", "http://127.0.0.1:9")
    assert all(hub.backend(s) == "hub" for s in hub.STORES) and hub.tg_hub()


def test_gate_ignores_live_off_but_not_direct(home: Path, fh: FakeHub) -> None:
    from core import tg

    (home / ".config" / "tg-adapter" / "live.off").write_text("x")
    consumer(fh, 0.4)
    assert tg.route("tg") == ("hub", "hub: the hub outbox, its consumer drained it 0 s ago")
    (home / ".config" / "tg-adapter" / "direct-tg").write_text("x")
    assert tg.route("tg")[0] == "direct"


def test_gate_needs_the_hub_and_its_adapter(home: Path, fh: FakeHub) -> None:
    """The hub outbox is open only while the hub answers and its adapter drains it; else the direct path, before
    anything is written (no row, no spool)."""
    from core import hub, tg

    fh.down = True
    assert tg.outbox_on("tg") == (False, "hub: the hub does not answer (HubDown)")
    fh.down = False
    assert tg.outbox_on("tg") == (False, "hub: the hub does not answer (Refused)")  # 404: no such route
    consumer(fh, 95.0)
    assert tg.outbox_on("tg") == (False, "hub: no consumer drains it (last seen 95 s ago)")
    consumer(fh, 1.0, bot="test")  # the test bot's consumer is not the live one
    assert tg.outbox_on("tg") == (False, "hub: no consumer drains it")
    fh.on("GET", "/outbox/consumers", {"consumers": [{"bot": "live", "age": "soon"}, "x"]})
    assert tg.outbox_on("tg")[0] is False
    consumer(fh, 29.0)
    assert tg.outbox_on("tg")[0] is True
    fh.down = True
    out = tg.send("tg", "send", {"text": "disk 3 is hot"}, wait=0.2)
    assert (out.status, out.row_id, out.via) == ("direct", None, "")
    assert hub.spool_depth() == 0 and not [c for c in fh.calls if c[1] == "/outbox"]


def test_send_uploads_files_and_never_goes_direct(home: Path, fh: FakeHub) -> None:
    from core import tg

    consumer(fh, 1.0)
    pic = home / "card.png"
    pic.write_bytes(b"\x89PNG" + b"x" * 100)
    fh.on("POST", "/tg/files", {"ref": "hubfile:out/abc-card.png"})
    fh.on("POST", "/outbox", {"id": 10000001})
    rows = {"n": 0}

    def row(m: str, p: str, b: object) -> tuple[int, object]:
        rows["n"] += 1
        return 200, {"id": 10000001, "status": "queued", "attempts": 0, "result": None}

    fh.on("GET", "/outbox/10000001", row)
    out = tg.send("event-card", "send", {"text": "hi", "files": [str(pic)]}, files=[str(pic)], wait=0.3, grace=0.1)
    assert out.status == "queued" and out.row_id == 10000001  # queued while its adapter drains: it stays, never direct
    assert out.via == "hub"
    posted = fh.bodies("/outbox")[0]
    assert posted["files"] == ["hubfile:out/abc-card.png"] and posted["body"]["files"] == ["hubfile:out/abc-card.png"]
    assert fh.bodies("/tg/files")[0] == {"_raw": 104, "_name": "card.png"}
    assert not [c for c in fh.calls if "withdraw" in c[1]]
    fh.on("GET", "/outbox/consumers", lambda m, p, b: (503, {"error": "unavailable"}))  # the wait can't ask
    out = tg.send("event-card", "send", {"text": "hi"}, wait=0.2, grace=0.1, check_gate=False)
    assert out.status == "queued" and not [c for c in fh.calls if "withdraw" in c[1]]  # can't ask: the row stays


def test_send_withdraws_a_row_the_hub_adapter_never_claimed(home: Path, fh: FakeHub) -> None:
    """The hub's adapter stops between the gate and the wait: the unclaimed row is withdrawn (atomically on the hub),
    then the caller's direct send is the only one. A row it claimed meanwhile is never withdrawn: it ends as sent."""
    from core import tg

    consumer(fh, 0.5, 120.0)  # fresh at the gate, gone at the wait
    fh.on("POST", "/outbox", {"id": 10000002})
    fh.on("GET", "/outbox/10000002", {"id": 10000002, "status": "queued", "attempts": 0, "result": None})
    fh.on("POST", "/outbox/10000002/withdraw", {"ok": True})
    out = tg.send("tg", "send", {"text": "backup failed"}, urgent=True, wait=0.2)
    assert (out.status, out.row_id, out.via) == ("direct", 10000002, "hub")
    assert out.error == "the hub's adapter does not drain the outbox"
    why = "tg: the hub's adapter does not drain the outbox; sent directly"
    assert fh.bodies("/outbox/10000002/withdraw") == [{"why": why}]

    consumer(fh, 0.5, 120.0)
    st = {"withdrawn": False}
    fh.on("POST", "/outbox", {"id": 10000003})

    def row(m: str, p: str, b: object) -> tuple[int, object]:
        s = "sent" if st["withdrawn"] else "queued"
        return 200, {"id": 10000003, "status": s, "result": json.dumps({"ids": [55]}) if s == "sent" else None}

    def wd(m: str, p: str, b: object) -> tuple[int, object]:
        st["withdrawn"] = True  # the adapter claimed it first: the hub refuses the withdraw
        return 200, {"ok": False}

    fh.on("GET", "/outbox/10000003", row)
    fh.on("POST", "/outbox/10000003/withdraw", wd)
    out = tg.send("tg", "send", {"text": "late"}, wait=0.2, grace=2.0)
    assert (out.status, out.ids) == ("sent", [55])


def test_bot_call_withdraws_on_the_hub(home: Path, fh: FakeHub) -> None:
    """bot_call (event-card, tg-status) withdraws a row that got no answer from the store it went to: the hub."""
    from core import tg

    consumer(fh, 0.5)
    fh.on("POST", "/outbox", {"id": 10000004})
    fh.on("GET", "/outbox/10000004", {"id": 10000004, "status": "queued", "attempts": 0, "result": None})
    fh.on("POST", "/outbox/10000004/withdraw", {"ok": True})
    with pytest.raises(tg.TgError, match=r"row #10000004 withdrawn"):
        tg.bot_call("sendMessage", {"text": "board"}, sender="tg-status", hold=False, timeout=0.2)
    assert len(fh.bodies("/outbox/10000004/withdraw")) == 1
    fh.on("POST", "/outbox/10000004/withdraw", lambda m, p, b: (503, {"error": "unavailable"}))
    with pytest.raises(tg.TgError, match=r"row #10000004 still pending"):  # can't withdraw: it may still go out
        tg.bot_call("sendMessage", {"text": "board"}, sender="tg-status", hold=False, timeout=0.2)


def test_send_spools_when_hub_down_and_replays_in_order(home: Path, fh: FakeHub) -> None:
    from core import hub, tg

    fh.down = True  # after the gate (bot_call's callers ask it once per run): a write the hub can't take is spooled
    a = tg.send("tg", "send", {"text": "one"}, wait=0.2, check_gate=False)
    b = tg.send("tg", "send", {"text": "two"}, wait=0.2, check_gate=False)
    assert (a.status, a.row_id, b.status) == ("queued", None, "queued") and "spooled" in a.error
    assert hub.spool_depth() == 2
    fh.down = False
    fh.on("POST", "/outbox", {"id": 7})
    fh.calls.clear()  # the tries while it was down answered 503
    assert hub.drain(5) == (2, 0)
    assert [x["body"]["text"] for x in fh.bodies("/outbox")] == ["one", "two"]


def test_receipts_hub_mode(home: Path, fh: FakeHub) -> None:
    from inbox import receipts

    fh.on(
        "POST",
        "/tg/deliveries/receipt",
        lambda m, p, b: (200, {"ok": b["event_id"] != 9, "result": "kind", "first": True}),
    )
    prompt = '<channel source="plugin:telegram-plus:telegram" event_id="5" message_id="77">hi</channel>' \
             '<channel source="plugin:telegram-plus:telegram" event_id="9">x</channel>'  # fmt: skip
    res = receipts.record(prompt, now=1000.0, timeout=30)  # the hook's 0.3 s would spool under CI load
    assert res["receipts"] == [5] and res["skipped"] == 1
    sent = fh.bodies("/tg/deliveries/receipt")
    assert sent[0]["message_id"] == "77" and sent[0]["how"] == "channel" and "message_id" not in sent[1]
    fh.down = True
    assert receipts.record(prompt, now=1001.0)["spooled"] == 2  # fail-open: spooled, the hook goes on


def test_brainwatch_reads_the_hub(home: Path, fh: FakeHub, monkeypatch: pytest.MonkeyPatch) -> None:
    from inbox import brainwatch

    now = time.time()

    def q(m: str, p: str, b: dict) -> tuple[int, object]:
        if b["name"] == "undelivered":
            assert b["params"]["before_ts"] == pytest.approx(now - brainwatch.STALL_S)
            return 200, {"rows": [{"id": 3, "kind": "tg.message", "ts": now - 400, "status": "new"}]}
        assert b["name"] == "received_unanswered"
        return 200, {"rows": [{"id": 4, "kind": "tg.message", "ts": now - 500}]}

    fh.on("POST", "/tg/q", q)
    f = brainwatch.Facts(None, now)
    assert [r["id"] for r in f.pending()] == [3]
    assert [r["id"] for r in f.received_unanswered()] == [4]
    fh.on("POST", "/tg/deliveries/receipt", {"ok": True, "result": "ok", "first": True})
    w = brainwatch.Watch(f, brainwatch._load(str(home / "bw.json")), live=True)
    w.transcript_receipts([3])
    assert fh.bodies("/tg/deliveries/receipt")[0]["how"] == "transcript"
    fh.down = True
    assert brainwatch.Facts(None, now).pending() == []  # hub down: nothing judged this run


def test_tgmetrics_from_the_hub(home: Path, fh: FakeHub) -> None:
    from inbox import tgmetrics

    t0 = 1_000_000.0

    def q(m: str, p: str, b: dict) -> tuple[int, object]:
        n, pa = b["name"], b["params"]
        rows: list[dict] = []
        if n == "channel_rows":
            rows = [{"id": 1, "kind": "tg.button", "ts": t0 + 1, "receipt_ts": t0 + 2, "how": "channel"},
                    {"id": 2, "kind": "tg.message", "ts": t0 + 3, "receipt_ts": None, "how": None}]  # fmt: skip
        elif n == "kind_payloads" and pa["kind"] == "tg.button":
            rows = [{"id": 1, "payload": json.dumps({"ack_ms": 40, "update_id": 10, "bot": "b"})}]
        elif n == "source_payloads":
            rows = [{"id": 1, "payload": json.dumps({"update_id": 10, "bot": "b"})},
                    {"id": 2, "payload": json.dumps({"update_id": 11, "bot": "b"})}]  # fmt: skip
        elif n == "outbox_live":
            rows = [{"id": 5, "ts": t0 + 4, "bot": "live", "chat": "1", "op": "send", "body": "{}", "status": "sent",
                     "sent_ts": t0 + 4.5}]  # fmt: skip
        elif n == "kind_count":
            rows = [{"n": 0}]
        return 200, {"rows": rows}

    fh.on("POST", "/tg/q", q)
    fh.on("GET", "/tg/meta", {"meta": {}})
    res = tgmetrics.compute(sqlite3.connect(":memory:"), t0, t0 + 3600, now=t0 + 3600, logs={}, tz=ZoneInfo("UTC"))
    assert res["store"] == "hub"
    assert res["T1"]["n"] == 1 and res["T4"]["n"] == 1
    assert res["T2"]["ok"] is False  # row 2 has no receipt
    fh.on("POST", "/tg/prune-text", lambda m, p, b: (200, {"n": 2000 if len(fh.bodies("/tg/prune-text")) < 2 else 5,
                                                            "more": len(fh.bodies("/tg/prune-text")) < 2}))  # fmt: skip
    assert tgmetrics.prune_text(sqlite3.connect(":memory:"), t0 + 90 * 86400) == 2005
    assert "keep_keys" in fh.bodies("/tg/prune-text")[0]


def test_tg_journal_reads_the_hub_as_journal_entries(home: Path, fh: FakeHub) -> None:
    from core import hub

    rows = {
        "tg.message": [
            {"id": 9, "payload": json.dumps({"user_id": 123, "chat_id": 123, "message_id": 70, "text": "/night",
                                             "handled": "night", "event_type": "message", "update_id": 5, "bot": "b",
                                             "ts": "2026-10-23T00:30:00.000Z"})},
            {"id": 4, "payload": json.dumps({"user_id": 77, "chat_id": 77, "message_id": 3, "text": "hi",
                                             "ts": "2026-10-23T00:10:00.000Z"})},
            {"id": 5, "payload": "not json"},
        ],
        "tg.button": [{"id": 6, "payload": json.dumps({"user_id": 123, "chat_id": 123, "message_id": 71,
                                                       "callback_data": "call:talk", "handled": "talk", "ack_ok": True,
                                                       "event_type": "button", "ts": "2026-10-23T00:20:00.000Z"})}],
        "bridge.handoff": [{"id": 8, "payload": json.dumps({"via": "voice", "chat_id": "voice", "text": "q",
                                                            "ts": "2026-10-23T00:25:00.000Z"})}],
    }  # fmt: skip
    fh.on("POST", "/tg/q", lambda m, p, b: (200, {"rows": rows.get(b["params"]["kind"], [])}))
    got = hub.tg_journal(100.0, 200.0)
    assert [e["id"] for e in got] == ["hub:4", "hub:6", "hub:8", "hub:9"]  # oldest row first; a bad payload skipped
    other, tap, voice, cmd = got
    assert other["owner"] is False and tap["owner"] is True and voice["owner"] is False and cmd["owner"] is True
    assert cmd["kind"] == "instant" and cmd["content"] == "/night" and cmd["meta"]["handled"] == "night"
    assert "text" not in cmd["meta"] and "bot" not in cmd["meta"] and "update_id" not in cmd["meta"]
    assert cmd["meta"]["message_id"] == "70" and cmd["message_id"] == "70" and cmd["ts"] == "2026-10-23T00:30:00.000Z"
    assert tap["kind"] == "button" and tap["meta"]["ack_ok"] == "true"  # a bound button stays a button
    assert voice["kind"] == "message" and voice["meta"]["via"] == "voice"
    asked = fh.bodies("/tg/q")
    assert {b["params"]["kind"] for b in asked} == set(hub.JOURNAL_KINDS)
    assert all(b["name"] == "kind_payloads" and b["params"]["since"] == 100.0 and b["params"]["until"] == 200.0
               for b in asked)  # fmt: skip
    assert [e["id"] for e in hub.tg_journal(100.0, kinds=["tg.button"])] == ["hub:6"]
    with pytest.raises(ValueError):
        hub.tg_journal(100.0, kinds=["outbox.sent"])
    fh.down = True
    with pytest.raises(hub.HubDown):
        hub.tg_journal(100.0)


def test_tg_bind_on_the_hub(
    home: Path, fh: FakeHub, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from tgadapter import bind

    monkeypatch.setenv("TG_ADAPTER_TOOLS", str(Path(__file__).resolve().parents[2] / "schemas" / "tools.toml"))
    rows = [{"id": 4, "kind": "command", "key": "night", "tool": "tg-status.show", "args": "[]", "note": 1,
             "on_error": "reply", "why": "w", "created": 1.0, "expires": None, "active": 1}]  # fmt: skip
    fh.on("POST", "/tg/q", lambda m, p, b: (200, {"rows": rows}))
    fh.on("GET", "/tg/meta", {"meta": {}})
    fh.on("POST", "/tg/bindings/upsert", lambda m, p, b: (200, {"ids": [9] * len(b["rows"])}))
    fh.on("POST", "/tg/bindings/retire", {"n": 1})
    assert bind.main(["list", "--json", "--instance", "live"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["key"] == "night"
    assert fh.bodies("/tg/bindings/upsert") == []  # a read writes nothing
    assert bind.main(["add", "command", "status", "tg-status.show", "--why", "board", "--instance", "live"]) == 0
    up = fh.bodies("/tg/bindings/upsert")
    assert len(up) == 1 and [r["key"] for r in up[0]["rows"]] == ["status"]
    assert bind.main(["rm", "#4", "--instance", "live"]) == 0
    assert fh.bodies("/tg/bindings/retire")[0]["id"] == 4


def test_ensure_hub_mode_keeps_adapter_down_and_companion_up(home: Path, fake_adapter: Path, fh: FakeHub) -> None:
    from tgadapter import ctl

    cfgd = home / ".config" / "tg-adapter"
    code, msg = ctl.ensure(instance="live")  # not hub mode: the in-container adapter starts as today
    assert code == 0 and ctl.running_pid("live")
    (cfgd / "live.hub").write_text("x")
    code, msg = ctl.ensure(instance="live")
    assert code == 0 and "companion started" in msg, msg
    assert not ctl.running_pid("live") and (cfgd / "live.off").exists() and ctl.companion_pid("live")
    code, msg = ctl.ensure(instance="live")
    assert msg.startswith("hub mode: companion running")
    fh.on("GET", "/outbox/consumers", {"consumers": [{"bot": "live", "caller": "tg", "age": 1.5}]})
    fh.on("GET", "/tg/meta", lambda m, p, b: (200, {"meta": {"offset:b": "70"} if "offset" in p else {}}))
    s = ctl.status("live")
    assert s["hub"]["companion_pid"] and s["hub"]["meta"] == {"offset:b": "70"} and s["adapter"] is None
    assert "hub mode (live.hub)" in ctl.render_status(s)
    calls = (home / "fake-bun" / "calls").read_text()
    assert "companion --instance live" in calls and "run --instance live" in calls
    (cfgd / "live.companion-off").write_text("x")
    ctl.stop(instance="live")
    assert not ctl.companion_pid("live")
    fake(home, "run", "serve")
