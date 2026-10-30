"""core.tg (one sender): the gate (switches, live config, a running live adapter), the owner's chat id, outbox
rows, waiting for the outcome, withdrawing a row the adapter has not claimed, and Bot API calls answered through the
outbox (translation, results, Telegram's reasons as TgError)."""

from __future__ import annotations

import json
import os
import time

import pytest
from outboxlib import OWNER, Consumer, gate_on, live_config, rows, start_fake_adapter, stop_fake_adapter

from core import tg


@pytest.fixture
def live(home):
    p = gate_on(home)
    yield home
    stop_fake_adapter(p)


# ------------------------------------------------------------------------------------------------ gate


def test_gate_closed_until_configured_sending_and_running(home):
    assert tg.outbox_on("tg") == (False, "live adapter not configured")
    live_config(home, send=False)
    assert tg.outbox_on("tg") == (False, "live adapter does not send (mode/send)")
    live_config(home, mode="test")
    assert tg.outbox_on("tg")[0] is False
    live_config(home)
    assert tg.outbox_on("tg") == (False, "live adapter not running")
    p = start_fake_adapter(home)
    try:
        assert tg.outbox_on("tg") == (True, "live adapter running")
        assert tg.adapter_running() == p.pid
    finally:
        stop_fake_adapter(p)
    assert tg.outbox_on("tg") == (False, "live adapter not running")  # stale PID file


def test_gate_pid_file_must_name_a_tg_adapter(home):
    live_config(home)
    d = home / ".local/state/tg-adapter/live"
    d.mkdir(parents=True)
    (d / "adapter.pid").write_text(f"{os.getpid()}\ntg-adapter\n")  # alive, but pytest's command line
    assert tg.adapter_running() is None
    p = start_fake_adapter(home)
    try:
        (d / "adapter.pid").write_text(f"{p.pid}\nsomething-else\n")
        assert tg.adapter_running() is None
        (d / "adapter.pid").write_text("garbage")
        assert tg.adapter_running() is None
    finally:
        stop_fake_adapter(p)


def test_switches_send_a_sender_or_all_the_direct_way(live):
    cfg = live / ".config/tg-adapter"
    assert tg.outbox_on("tg-status")[0] and tg.outbox_on("tg")[0]
    (cfg / "direct-tg-status").touch()
    assert tg.outbox_on("tg-status") == (False, "switch direct-tg-status")
    assert tg.outbox_on("tg")[0]
    (cfg / "direct-all").touch()
    assert tg.outbox_on("tg") == (False, "switch direct-all")
    (cfg / "direct-all").unlink()
    (cfg / "live.off").touch()
    assert tg.outbox_on("tg") == (False, "switch live.off")
    (cfg / "live.off").unlink()
    (live / ".config/assistant/events-off").touch()
    assert tg.outbox_on("tg") == (False, "ledger switch events-off")
    (live / ".config/assistant/events-off").unlink()
    assert tg.outbox_on("tg")[0]
    assert tg.outbox_on("../x")[0] is False and tg.outbox_on("")[0] is False


def test_owner_chat_sources(home, monkeypatch):
    assert tg.owner_chat() is None
    acc = home / "tgstate"
    acc.mkdir()
    (acc / "access.json").write_text(json.dumps({"allowFrom": ["222"]}))
    monkeypatch.setenv("TELEGRAM_STATE_DIR", str(acc))
    assert tg.owner_chat() == "222"  # the plugin's access file (no live config)
    other = home / "other.json"
    other.write_text(json.dumps({"allowFrom": [333, 444]}))
    live_config(home, allow_from=None, allow_from_file="~/other.json")
    assert tg.owner_chat() == "333"
    live_config(home)
    assert tg.owner_chat() == OWNER


# ------------------------------------------------------------------------------------------------ rows


def test_enqueue_row_shape_and_idem(live):
    rid = tg.enqueue("send", {"text": "hi"}, urgent=True, idem="k1", files=["/tmp/a.ogg"], sender="tg-voice")
    r = rows(live)[0]
    assert r["id"] == rid
    assert (r["bot"], r["chat"], r["op"], r["mode"], r["status"], r["urgent"], r["idem_key"]) == (
        "live",
        OWNER,
        "send",
        "live",
        "queued",
        1,
        "k1",
    )
    assert json.loads(r["body"]) == {"text": "hi", "sender": "tg-voice"}
    assert json.loads(r["files"]) == ["/tmp/a.ogg"]
    assert abs(r["ts"] - time.time()) < 30
    assert tg.enqueue("send", {"text": "again"}, idem="k1") is None
    assert len(rows(live)) == 1
    assert os.stat(live / ".local/state/assistant/events.db").st_mode & 0o777 == 0o600


def test_send_outcomes(live):
    with Consumer(
        live,
        decide=lambda r: (
            ("failed", "Call to 'sendMessage' failed! (400: Bad Request: chat not found)")
            if "bad" in r["body"]
            else ("sent", [7, 8])
        ),
    ):
        ok = tg.send("tg", "send", {"text": "fine"}, urgent=True, wait=5)
        bad = tg.send("tg", "send", {"text": "bad"}, urgent=True, wait=5)
    assert (ok.status, ok.ids, ok.direct) == ("sent", [7, 8], False)
    assert bad.status == "failed" and "chat not found" in bad.error
    (live / ".config/tg-adapter/direct-tg").touch()
    d = tg.send("tg", "send", {"text": "x"})
    assert d.direct and d.error == "switch direct-tg" and d.row_id is None
    assert len(rows(live)) == 2  # the switched send wrote no row


def test_send_queued_while_adapter_runs_withdrawn_when_it_is_gone(home):
    p = gate_on(home)
    try:
        out = tg.send("tg", "send", {"text": "held"}, wait=0.3)  # nobody consumes: held or busy
        assert (out.status, out.direct) == ("queued", False)
        assert rows(home)[0]["status"] == "queued"
        stop_fake_adapter(p)
        out2 = tg.send("tg", "send", {"text": "lost"}, wait=0.3, check_gate=False)
        assert out2.direct and out2.error == "live adapter stopped"
        r = rows(home)[1]
        assert r["status"] == "skipped" and r["last_error"].startswith("withdrawn: tg: live adapter stopped")
    finally:
        stop_fake_adapter(p)


def test_withdraw_never_takes_a_claimed_row_and_send_waits_for_it(live):
    rid = tg.enqueue("send", {"text": "a"})
    assert tg.withdraw(rid, "test") is True
    assert tg.withdraw(rid, "again") is False
    rid2 = tg.enqueue("send", {"text": "b"})
    import sqlite3

    con = sqlite3.connect(live / ".local/state/assistant/events.db", isolation_level=None)
    con.execute("UPDATE outbox SET status='sending' WHERE id=?", (rid2,))
    con.close()
    assert tg.withdraw(rid2, "test") is False
    # a claimed row at the wait's end: send() waits the grace period for its outcome (the consumer polls every 30 ms;
    # a 1 s wait leaves room for a loaded CI box to claim it, the 2.5 s delay still ends it only after the wait)
    with Consumer(live, delay=2.5):
        out = tg.send("tg", "send", {"text": "slow"}, wait=1.0, grace=8)
    assert out.status == "sent" and out.ids


def test_wait_sent_unknown_row(live):
    tg.enqueue("send", {"text": "a"})
    with pytest.raises(tg.TgError):
        tg.wait_sent(999, 0.1)


# ------------------------------------------------------------------------------------------------ bot_call


def test_bot_call_translates_send_edit_pin(live):
    with Consumer(live) as c:
        m = tg.bot_call(
            "sendMessage",
            {
                "chat_id": "999",
                "text": "<b>board</b>",
                "parse_mode": "HTML",
                "disable_notification": True,
                "link_preview_options": {"is_disabled": True},
                "reply_markup": {"inline_keyboard": [[{"text": "📞 Talk", "callback_data": "call:talk"}]]},
            },
            sender="tg-status",
            hold=False,
            dedupe=False,
            on_press="keep",
        )
        e = tg.bot_call(
            "editMessageText",
            {"message_id": 41, "text": "plain", "link_preview_options": {"is_disabled": True}},
            sender="tg-status",
        )
        pin = tg.bot_call("pinChatMessage", {"message_id": 41, "disable_notification": True}, sender="tg-status")
        unpin = tg.bot_call("unpinChatMessage", {"message_id": 41}, sender="event-card")
        alert = tg.bot_call(
            "sendMessage", {"text": "🔴 cancelled", "parse_mode": "HTML"}, sender="event-card", urgent=True
        )
        kb = tg.bot_call(
            "editMessageReplyMarkup", {"message_id": 41, "reply_markup": {"inline_keyboard": []}}, sender="x"
        )
    rs = rows(live)
    assert m == {"message_id": 1000 + rs[0]["id"]} and e == {"message_id": 1000 + rs[1]["id"]}
    assert pin is True and unpin is True and alert["message_id"] and kb["message_id"]
    bodies = [json.loads(r["body"]) for r in rs]
    assert [r["op"] for r in rs] == ["send", "edit", "pin", "pin", "send", "edit"]
    assert all(r["chat"] == OWNER for r in rs)  # chat_id in the params never wins over the owner
    assert bodies[0] == {
        "text": "<b>board</b>",
        "format": "html",
        "link_preview": "off",
        "buttons": [[{"text": "📞 Talk", "callback_data": "call:talk"}]],
        "silent": True,
        "hold": False,
        "dedupe": False,
        "on_press": "keep",
        "sender": "tg-status",
    }
    assert bodies[1] == {
        "text": "plain",
        "format": "text",
        "link_preview": "off",
        "message_id": 41,
        "sender": "tg-status",
    }
    assert bodies[2] == {"message_id": 41, "notify": False, "sender": "tg-status"}
    assert bodies[3] == {"message_id": 41, "unpin": True, "sender": "event-card"}
    assert rs[4]["urgent"] == 1 and rs[0]["urgent"] == 0 and "hold" not in bodies[4]
    assert bodies[5] == {"buttons": [], "message_id": 41, "sender": "x"}
    assert len(c.seen) == 6


def test_bot_call_errors(live):
    gone = "Call to 'editMessageText' failed! (400: Bad Request: message to edit not found)"
    with Consumer(live, decide=lambda r: ("failed", gone)):
        with pytest.raises(tg.TgError) as ei:
            tg.bot_call("editMessageText", {"message_id": 5, "text": "x"}, sender="tg-status")
    assert ei.value.code == 400 and "message to edit not found" in ei.value.desc.lower()
    with pytest.raises(tg.TgError, match="not supported"):
        tg.bot_call("sendSticker", {}, sender="tg-status")
    with pytest.raises(tg.TgError, match="never unpin all"):
        tg.bot_call("unpinChatMessage", {}, sender="tg-status")
    # nobody answers: withdrawn, so nothing goes out later
    with pytest.raises(tg.TgError, match="withdrawn"):
        tg.bot_call("sendMessage", {"text": "late"}, sender="tg-status", timeout=0.3)
    assert rows(live)[-1]["status"] == "skipped"


def test_bot_call_direct_when_the_adapter_is_gone(home):
    p = gate_on(home)
    stop_fake_adapter(p)
    with pytest.raises(tg.TgError) as ei:
        tg.bot_call("sendMessage", {"text": "x"}, sender="tg-status", timeout=0.3)
    assert ei.value.code == -1
    assert rows(home)[0]["status"] == "skipped"
