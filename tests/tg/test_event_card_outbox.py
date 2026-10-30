"""event-card on the one outbox: while the live adapter runs, its Bot API calls become live outbox rows (the card
silent and never held or deduped, the Talk keyboard marked keep by the adapter, alerts urgent); Telegram's reasons
come back as its TgError so "message to edit not found" still means "send a new card"; the switch sends it direct;
no token is read in outbox mode. The direct path itself is the legacy suite's (~/opt/event-card-tests)."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys

import pytest
from outboxlib import OWNER, Consumer, gate_on, rows, stop_fake_adapter
from tg_testlib import REPO


def load(home, monkeypatch, name="event_card_p8"):
    monkeypatch.setenv("EVENT_CARD_DIR", str(home / "ec"))
    monkeypatch.delenv("EVENT_CARD_API", raising=False)
    loader = importlib.machinery.SourceFileLoader(name, str(REPO / "scripts" / "event-card"))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    saved = list(sys.path)
    try:
        loader.exec_module(mod)
    finally:
        sys.path[:] = saved
    mod.os.makedirs(mod.DIR, exist_ok=True)
    return mod


@pytest.fixture
def live(home):
    p = gate_on(home)
    yield home
    stop_fake_adapter(p)


def test_card_send_pin_edit_unpin_alert_through_the_outbox(live, monkeypatch):
    ec = load(live, monkeypatch)
    monkeypatch.setattr(ec, "creds", lambda: pytest.fail("outbox mode must not read the token"))

    def decide(r):
        b = json.loads(r["body"])
        if r["op"] == "edit" and b.get("message_id") == 999:
            return ("failed", "Call to 'editMessageText' failed! (400: Bad Request: message to edit not found)")
        return ("sent", [500 + r["id"]])

    with Consumer(live, decide=decide):
        mid = ec.send_card("<b>Night</b> card")
        ec.pin(mid)
        assert ec.edit(mid, "<b>Night</b> later", ec.talk_markup()) is True
        assert ec.edit(999, "gone card") is False  # message gone: event-card sends a new one
        ec.keep_buttons(mid)  # the adapter marks keep (on_press): no second writer on buttons.json
        ec.unpin(mid)
        ec.call("sendMessage", text="🔴 S9 cancelled", parse_mode="HTML", link_preview_options={"is_disabled": True})
    rs = rows(live)
    assert mid == 500 + rs[0]["id"]
    assert [r["op"] for r in rs] == ["send", "pin", "edit", "edit", "pin", "send"]
    b = [json.loads(r["body"]) for r in rs]
    assert b[0]["silent"] is True and b[0]["hold"] is False and b[0]["dedupe"] is False
    assert b[0]["format"] == "html" and b[0]["on_press"] == "keep" and b[0]["sender"] == "event-card"
    assert b[0]["buttons"] == [[{"text": "📞 Talk", "callback_data": "call:talk"}]]
    assert b[1] == {"message_id": mid, "notify": False, "sender": "event-card"}
    assert b[2]["message_id"] == mid and b[2]["on_press"] == "keep"
    assert b[4] == {"message_id": mid, "unpin": True, "sender": "event-card"}
    assert rs[5]["urgent"] == 1 and rs[0]["urgent"] == 0 and "silent" not in b[5]
    assert all(r["chat"] == OWNER for r in rs)
    assert not (live / ".claude").exists()


def test_switch_sends_event_card_direct(live, monkeypatch):
    (live / ".config/tg-adapter/direct-event-card").touch()
    ec = load(live, monkeypatch, "event_card_p8b")
    assert ec.outbox() is False
    with pytest.raises(OSError):  # direct path: it reads the (absent) token file; nothing was enqueued
        ec.send_card("x")
    assert rows(live) == []


def test_test_api_and_dry_run_stay_direct(live, monkeypatch):
    monkeypatch.setenv("EVENT_CARD_API", "http://127.0.0.1:9")
    loader = importlib.machinery.SourceFileLoader("event_card_p8c", str(REPO / "scripts" / "event-card"))
    spec = importlib.util.spec_from_loader("event_card_p8c", loader)
    assert spec is not None
    ec = importlib.util.module_from_spec(spec)
    saved = list(sys.path)
    try:
        loader.exec_module(ec)
    finally:
        sys.path[:] = saved
    assert ec.outbox() is False
