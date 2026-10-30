"""WebSocket session, outbox (ack, replay, TTL, coalescing, cancel), actions + hook, chat both ways, HTTP fallback,
schema rejection, self-test and crash notes, probes."""
import asyncio
import json
import os
import stat
import sys

import pytest
from aiohttp import web

from conftest import FakePhone
from phonegw.util import ulid


async def push(env, **kw):
    body = {"level": "urgent", "title": "Test push", "body": "hello"}
    body.update(kw)
    return await env.call("POST", "/internal/push", body)


async def test_hello_config_and_push_ack(env, phone):
    await phone.connect()
    h = await phone.recv("hello")
    assert h["body"]["device_id"] == phone.device_id and h["body"]["tz"] == "Europe/Zurich"
    c = await phone.recv("config")
    assert c["body"]["keepalive_s"] == 240
    assert c["ack"] is False and "key" not in c, "config is connection state: direct on every connect, no outbox"
    st, j = await push(env, wait=5, action=None, actions=[{"label": "Seen", "verb": "seen"},
                                                         {"label": "+15 min", "verb": "snooze", "arg": "r:1a2b3c4d"}])
    assert st == 200 and j["status"] == "acked" and j["acked_ms"] < 5000
    p = await phone.recv("push")
    b = p["body"]
    assert b["private"] is False and b["channel"] == "urgent" and len(b["nonce"]) == 8
    assert [a["unlock"] for a in b["actions"]] == [False, True]
    assert env.gw.db.one("SELECT body FROM outbox WHERE id=?", p["id"])["body"] is None, "acked bodies are dropped"
    st, j = await env.call("GET", "/internal/pushes?id=" + p["id"])
    assert j["pushes"][0]["acked_ms"] is not None and j["pushes"][0]["delivered_ms"] is not None


async def test_non_urgent_is_private_and_masked(env, phone):
    await phone.connect()
    st, j = await push(env, level="info", title="Mail", body="write to someone@example.com about it")
    assert st == 200 and j["masked"]
    p = await phone.recv("push")
    assert p["body"]["private"] is True and "someone@example.com" not in p["body"]["body"]


async def test_lint_and_verbs_refused(env, phone):
    st, j = await push(env, body="CHF 120.50 due Friday")
    assert st == 422 and j["error"] == "lint"
    st, j = await push(env, actions=[{"label": "Pay", "verb": "pay"}])
    assert st == 403 and j["error"] == "verb"


async def test_offline_queue_replay_in_order_and_coalescing(env, phone):
    st, j1 = await push(env, title="first", tag="a")
    assert j1["status"] == "queued"
    await push(env, title="second", tag="b")
    await push(env, title="third", tag="a")          # replaces "first" (same tag = same key)
    await env.call("POST", "/internal/chat", {"text": "queued chat"})
    phone.auto_ack = True
    await phone.connect()
    h = await phone.recv("hello")
    assert h["body"]["pending"] == 3
    got = [await phone.recv() for _ in range(4)]
    titles = [g["body"].get("title") or g["body"].get("text") for g in got if g["t"] in ("push", "chat")]
    assert titles == ["second", "third", "queued chat"]
    await asyncio.sleep(0.3)
    assert env.gw.db.pending_count(phone.device_id) == 0


async def test_ttl_expiry(env, phone):
    await push(env, title="short", ttl=1)
    await asyncio.sleep(1.2)
    await phone.connect()
    h = await phone.recv("hello")
    assert h["body"]["pending"] == 0
    assert env.gw.db.expire() >= 0


async def test_unacked_messages_are_replayed_after_reconnect(env, phone):
    phone.auto_ack = False
    await phone.connect()
    await push(env, title="needs ack")
    p = await phone.recv("push")
    await phone.close()
    again = FakePhone(env)
    again.key, again.cert, again.device_id = phone.key, phone.cert, phone.device_id
    await again.connect()
    p2 = await again.recv("push")
    assert p2["id"] == p["id"]
    await again.close()


async def test_cancel_drops_unsent_and_tells_the_phone(env, phone):
    await push(env, title="go", tag="lasttrain")
    st, j = await env.call("POST", "/internal/push/cancel", {"tag": "lasttrain"})
    assert j["dropped_unsent"] == 1
    await phone.connect()
    c = await phone.recv("push.cancel")
    assert c["body"] == {"tag": "lasttrain"}
    assert not [r for r in phone.received if r["t"] == "push"]


async def test_wait_times_out_without_ack(env, phone):
    phone.auto_ack = False
    await phone.connect()
    await phone.recv("hello")
    st, j = await push(env, wait=0.5)
    assert j["status"] == "timeout"


async def test_action_hook_and_nonce_rules(env, phone, tmp_path):
    out = tmp_path / "hook.json"
    hook = tmp_path / "hook.sh"
    hook.write_text("#!/bin/sh\ncat > %s\n" % out)
    hook.chmod(0o755)
    env.gw.cfg["action_hook"] = [str(hook)]
    await phone.connect()
    st, j = await push(env, tag="lasttrain", actions=[{"label": "Seen", "verb": "seen"},
                                                       {"label": "+15", "verb": "snooze", "arg": "r:1a2b3c4d"}])
    p = await phone.recv("push")
    nonce = p["body"]["nonce"]
    await phone.send("action", {"push_id": p["id"], "nonce": "deadbeef", "verb": "seen"}, ack=True)       # wrong nonce
    await phone.send("action", {"push_id": p["id"], "nonce": nonce, "verb": "call"}, ack=True)            # not offered
    await phone.send("action", {"push_id": p["id"], "nonce": nonce, "verb": "snooze", "arg": "r:ffffffff"}, ack=True)
    for _ in range(50):
        if out.exists():
            break
        await asyncio.sleep(0.05)
    got = json.loads(out.read_text())
    assert got["verb"] == "snooze" and got["arg"] == "r:1a2b3c4d" and got["tag"] == "lasttrain", "arg comes from the offer"
    await phone.send("action", {"push_id": p["id"], "nonce": nonce, "verb": "seen"}, ack=True)            # nonce used
    await asyncio.sleep(0.3)
    row = env.gw.db.one("SELECT verb FROM push_log WHERE id=?", p["id"])
    assert row["verb"] == "snooze"
    acks = [r for r in phone.received if r["t"] == "ack"]
    assert acks, "the server acks the app's frames"


async def test_body_tap_open_counts_even_when_not_offered(env, phone):
    """A tap on the notification body sends `open` for every push (MainActivity / LinkActivity); it must count as
    seen so the urgent ladder stops, while other un-offered verbs stay ignored."""
    await phone.connect()
    st, j = await push(env, tag="linkpush", actions=[{"label": "Seen", "verb": "seen"}])
    p = await phone.recv("push")
    nonce = p["body"]["nonce"]
    await phone.send("action", {"push_id": p["id"], "nonce": nonce, "verb": "call"}, ack=True)            # not offered
    await phone.send("action", {"push_id": p["id"], "nonce": nonce, "verb": "open"}, ack=True)            # body tap
    for _ in range(40):
        row = env.gw.db.one("SELECT verb, arg FROM push_log WHERE id=?", p["id"])
        if row["verb"]:
            break
        await asyncio.sleep(0.05)
    assert (row["verb"], row["arg"]) == ("open", None)


async def test_http_batch_handles_good_frames_next_to_a_refused_one(env, phone):
    """One refused frame must not hold back the rest of an offline queue: the good ones are handled, the batch is
    still 422 (the app keeps it; the replay counts the good ones as dups) and one `phone-reject` note per day."""
    good = {"v": 1, "id": ulid(), "t": "event", "ts": 1, "body": {"kind": "zone", "zone": "gym", "dir": "enter"}}
    bad = {"v": 1, "id": ulid(), "t": "event", "ts": 1, "body": {"kind": "zone", "zone": "gym", "dir": "enter", "lat": 1.0}}
    st, j = await phone.http("POST", "/v1/events", {"items": [good, bad]})
    assert st == 422 and j["error"] == "schema"
    assert env.gw.db.one("SELECT COUNT(*) AS n FROM events WHERE id=?", good["id"])["n"] == 1
    st, j = await phone.http("POST", "/v1/events", {"items": [good, bad]})
    assert st == 422
    assert env.gw.db.one("SELECT COUNT(*) AS n FROM events WHERE id=?", good["id"])["n"] == 1
    notes = env.notes("reject")
    assert len(notes) == 1
    text = open(os.path.join(env.paths.inbox, notes[0])).read()
    assert "`event`" in text and "1.0" not in text


async def test_chat_round_trip_note_fallback(env, phone):
    await phone.connect()
    st, j = await env.call("POST", "/internal/chat", {"text": "Vacuum is docked.", "buttons": [[{"text": "OK", "data": "ok"}]]})
    c = await phone.recv("chat")
    assert c["body"]["text"] == "Vacuum is docked." and c["body"]["msg_id"] == j["msg_id"]
    mid = "m_" + ulid()
    await phone.send("chat", {"msg_id": mid, "text": "Thanks, start it again at 15:00", "reply_to": j["msg_id"]}, ack=True)
    for _ in range(40):
        if env.notes("chat"):
            break
        await asyncio.sleep(0.05)
    notes = env.notes("chat")
    assert len(notes) == 1
    path = os.path.join(env.paths.inbox, notes[0])
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    txt = open(path).read()
    assert "start it again" in txt and "phone chat --reply-to %s" % mid in txt
    # the same message again (app retry) is de-duplicated
    await phone.send("chat", {"msg_id": mid, "text": "Thanks, start it again at 15:00"}, ack=True, msg_id=None)
    await asyncio.sleep(0.2)


async def fake_inbox(env, token="t0k-inbox-test"):
    """telegram-plus's inbox as the plugin runs it: 401 unless Authorization: Bearer <first line of the token file>."""
    got, refused = [], []
    tf = env.tmp / "inbox-token"
    tf.write_text(token + "\nsecond line is ignored\n")

    async def inbox(request):
        if request.headers.get("Authorization") != "Bearer " + token:
            refused.append(request.headers.get("Authorization"))
            return web.json_response({"error": "unauthorized"}, status=401)
        got.append(await request.json())
        return web.json_response({"ok": True}, status=202)
    app = web.Application()
    app.router.add_post("/voice", inbox)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    env.gw.cfg.update({"chat_plugin": True, "chat_plugin_url": "http://127.0.0.1:%d/voice" % runner.addresses[0][1],
                       "chat_plugin_token_file": str(tf)})
    return got, refused, runner, tf


async def until(pred, n=60):
    for _ in range(n):
        if pred():
            return True
        await asyncio.sleep(0.05)
    return False


async def test_chat_to_plugin_inbox(env, phone):
    got, refused, runner, tf = await fake_inbox(env)
    await phone.connect()
    mid = "m_" + ulid()
    await phone.send("chat", {"msg_id": mid, "text": "hi from the app"}, ack=True)
    await until(lambda: got)
    assert got and got[0]["source"] == "app" and got[0]["task"] == "hi from the app" and got[0]["msg_id"] == mid
    assert not refused and not env.notes("chat")
    tf.write_text("another token\n")                       # a wrong token: the inbox refuses, the note fallback stays
    await phone.send("chat", {"msg_id": "m_" + ulid(), "text": "second one"}, ack=True)
    await until(lambda: env.notes("chat"))
    await runner.cleanup()
    assert refused == ["Bearer another token"] and len(got) == 1 and len(env.notes("chat")) == 1


async def test_card_answer_to_plugin_inbox(env, phone):
    got, refused, runner, tf = await fake_inbox(env)
    await phone.connect()
    deck = [{"id": "ab12", "title": "Ferry or plane", "say": "Ferry or plane?", "options": ["ferry", "plane"]},
            {"id": "cd34", "title": "Keep the watch", "say": "Keep or drop?", "options": ["keep", "drop"]}]
    st, j = await env.call("POST", "/internal/card", {"deck": True, "items": deck})
    assert j["cards"] == ["c_deck_ab12", "c_deck_cd34"]
    await phone.send("card.answer", {"card_id": "c_deck_ab12", "option": "o2", "at": 1792513200}, ack=True)
    await until(lambda: got)
    assert got and got[0]["source"] == "app" and got[0]["task"].startswith("[card c_deck_ab12] Ferry or plane → plane")
    assert "never an approval" in got[0]["task"] and got[0]["device_id"] == phone.device_id
    assert not env.notes("decisions")
    tf.write_text("wrong\n")
    await phone.send("card.answer", {"card_id": "c_deck_cd34", "option": "o1", "at": 1792513200}, ack=True)
    await until(lambda: env.notes("decisions"))
    await runner.cleanup()
    notes = env.notes("decisions")
    assert len(notes) == 1 and "Keep the watch → **keep**" in open(os.path.join(env.paths.inbox, notes[0])).read()


async def test_duplicate_frames_processed_once(env, phone):
    await phone.connect()
    ev = {"v": 1, "id": ulid(), "t": "event", "ts": 1792513200, "ack": True, "body": {"kind": "tile", "name": "leaving"}}
    await phone.send(None, None, raw=ev)
    await phone.send(None, None, raw=ev)
    await asyncio.sleep(0.4)
    assert env.gw.db.one("SELECT COUNT(*) n FROM events")["n"] == 1
    acked = [i for r in phone.received if r["t"] == "ack" for i in r["body"]["ids"]]
    assert acked.count(ev["id"]) == 2, "duplicates are still acked so the app stops resending"


async def test_ws_schema_rejection(env, phone):
    await phone.connect()
    bad = await phone.send("state", {"place": {"zone": "home", "lat": 46.9480, "lon": 7.4474}})
    e = await phone.recv("error")
    assert e["body"]["code"] == "schema" and e["body"]["ref"] == bad["id"] and "46.9480" not in json.dumps(e)
    await phone.send("state", {"battery": {"level": 50, "band": "ok", "charging": True}})
    await asyncio.sleep(0.2)
    v, _ = env.gw.db.get_state(phone.device_id, "battery")
    assert v["level"] == 50
    assert env.gw.db.get_state(phone.device_id, "place")[0] is None


async def test_http_fallback_sync_ack_events(env, phone):
    await push(env, title="via sync")
    st, j = await phone.http("GET", "/v1/sync")
    assert st == 200 and len(j["messages"]) == 1
    m = j["messages"][0]
    env.gw.proto.check("s2c", m)
    st, _ = await phone.http("POST", "/v1/ack", {"items": [{"v": 1, "id": ulid(), "t": "ack", "ts": 1, "body": {"ids": [m["id"]]}}]})
    assert st == 200
    st, j = await phone.http("GET", "/v1/sync")
    assert j["messages"] == []
    ev = {"v": 1, "id": ulid(), "t": "event", "ts": 1, "body": {"kind": "zone", "zone": "gym", "dir": "enter"}}
    st, j = await phone.http("POST", "/v1/events", {"items": [ev, ev]})
    assert (st, j["accepted"], j["dup"]) == (200, 1, 1)
    bad = {"v": 1, "id": ulid(), "t": "event", "ts": 1, "body": {"kind": "zone", "zone": "gym", "dir": "enter", "lat": 1.0}}
    st, j = await phone.http("POST", "/v1/events", {"items": [bad]})
    assert st == 422 and j["error"] == "schema"
    st, j = await phone.http("POST", "/v1/events", {"items": [{"v": 1, "id": ulid(), "t": "chat", "ts": 1,
                                                               "body": {"msg_id": "m_" + ulid(), "text": "x"}}]})
    assert st == 422, "chat does not belong on /v1/events"


async def test_selftest_and_crash_notes(env, phone):
    await phone.connect()
    checks = {"checks": [{"id": "battery_exempt", "ok": True}, {"id": "urgent_dnd_bypass", "ok": False}, {"id": "vpn", "ok": None}]}
    await phone.send("selftest", checks)
    await phone.send("selftest", checks)
    await phone.send("crash", {"exc": "java.lang.IllegalStateException", "where": "ConnectionService.kt:212", "app": "0.1.0"})
    await asyncio.sleep(0.3)
    assert len(env.notes("selftest")) == 1, "once per check per day"
    assert "urgent_dnd_bypass" in open(os.path.join(env.paths.inbox, env.notes("selftest")[0])).read()
    assert len(env.notes("crash")) == 1
    st, j = await env.call("GET", "/internal/status")
    d = j["devices"][0]
    assert d["online"] and d["selftest"]["red"] == ["urgent_dnd_bypass"] and d["app"] == "0.1.0"


async def test_selftest_optional_reds_make_no_note(env, phone):
    await phone.connect()
    await phone.send("selftest", {"checks": [{"id": "battery_exempt", "ok": True},
                                             {"id": "urgent_dnd_bypass", "ok": False, "optional": True},
                                             {"id": "widget_pinned", "ok": False, "optional": True}]})
    await asyncio.sleep(0.3)
    assert not env.notes("selftest")
    st, j = await env.call("GET", "/internal/status")
    sel = j["devices"][0]["selftest"]
    assert sel["red"] == [] and sel["optional_red"] == ["urgent_dnd_bypass", "widget_pinned"]
    await phone.send("selftest", {"checks": [{"id": "notifications", "ok": False, "optional": False},
                                             {"id": "widget_pinned", "ok": False, "optional": True}]})
    await asyncio.sleep(0.3)
    notes = env.notes("selftest")
    assert len(notes) == 1
    txt = open(os.path.join(env.paths.inbox, notes[0])).read()
    assert "notifications" in txt and "widget_pinned" not in txt


async def test_probe(env, phone):
    await phone.connect()
    st, j = await env.call("POST", "/internal/probe", {"n": 3, "gap_s": 0})
    assert j["status"] == "done" and j["lost"] == 0 and len(j["rtt_ms"]) == 3
    st, j = await env.call("GET", "/internal/status")
    assert j["devices"][0]["rtt_ms"] is not None


async def test_replaced_socket(env, phone):
    await phone.connect()
    await phone.recv("hello")
    second = FakePhone(env)
    second.key, second.cert, second.device_id = phone.key, phone.cert, phone.device_id
    await second.connect()
    await second.recv("hello")
    with pytest.raises(ConnectionError):
        await phone.recv("never", timeout=3)
    st, j = await push(env, wait=3)
    assert j["status"] == "acked"
    await second.close()


async def test_ping_keeps_last_seen(env, phone):
    await phone.connect()
    await phone.recv("hello")
    await phone.ws.ping(b"x")
    await asyncio.sleep(0.2)
    assert env.gw.db.device(phone.device_id)["last_seen"]


async def test_retention(env, phone):
    db = env.gw.db
    db.x("INSERT INTO events(id, device_id, kind, at, body, received) VALUES('old','d','zone',1,'{}',1)")
    db.x("INSERT INTO chat(id, at, dir, text, reply_to) VALUES('m_old',1,'in','x',NULL)")
    n = db.retention(env.gw.cfg["retention_days"])
    assert n["events"] == 1 and n["chat"] == 1


async def test_bridge_routes_taps_to_fake_tools(env, phone, tmp_path, monkeypatch):
    log = tmp_path / "calls.txt"
    for name in ("PUSH_BIN", "REMIND_BIN", "TALK_BUTTON_BIN"):
        f = tmp_path / name.lower()
        f.write_text("#!/bin/sh\nin=$(cat)\necho %s \"$@\" $in >> %s\n" % (name, log))
        f.chmod(0o755)
        monkeypatch.setenv(name, str(f))
    env.gw.cfg["bridge"] = True
    await phone.connect()
    st, j = await push(env, tag="rem-1a2b3c4d", actions=[{"label": "+1 h", "verb": "snooze", "arg": "r:1a2b3c4d:s1h"},
                                                          {"label": "Call me", "verb": "call"}])
    assert j["nonce"]
    p = await phone.recv("push")
    await phone.send("action", {"push_id": p["id"], "nonce": p["body"]["nonce"], "verb": "snooze", "arg": "r:1a2b3c4d:s1h"})
    for _ in range(60):
        if log.exists() and len(log.read_text().splitlines()) >= 2:
            break
        await asyncio.sleep(0.05)
    lines = log.read_text().splitlines()
    assert len(lines) == 1 and lines[0].startswith("PUSH_BIN event - ")
    sent = json.loads(lines[0][len("PUSH_BIN event - "):])
    assert sent == {"t": "action", "body": {"push_id": p["id"], "nonce": p["body"]["nonce"], "verb": "snooze",
                                            "arg": "r:1a2b3c4d:s1h"}}
    st, j = await push(env, level="timed", title="t2", actions=[{"label": "+1 h", "verb": "snooze", "arg": "r:1a2b3c4d:s1h"}])
    p = await phone.recv("push")
    await phone.send("action", {"push_id": p["id"], "nonce": p["body"]["nonce"], "verb": "snooze", "arg": "r:1a2b3c4d:s1h"})
    for _ in range(60):
        if len(log.read_text().splitlines()) >= 2:
            break
        await asyncio.sleep(0.05)
    assert log.read_text().splitlines()[1] == "REMIND_BIN act snooze r:1a2b3c4d:s1h"
    st, j = await push(env, level="timed", title="t", actions=[{"label": "Call me", "verb": "call"}])
    p = await phone.recv("push")
    await phone.send("action", {"push_id": p["id"], "nonce": p["body"]["nonce"], "verb": "call"})
    for _ in range(60):
        if len(log.read_text().splitlines()) >= 3:
            break
        await asyncio.sleep(0.05)
    assert log.read_text().splitlines()[2] == "TALK_BUTTON_BIN --json", "non-urgent taps never go through push"


async def test_share_to_plugin_inbox_and_reply_to_guard(env, phone):
    """chat_plugin on: an image share reaches the session as an app event at once (no note); a hand-off id as
    reply_to is dropped instead of failing the chat."""
    import aiohttp
    got, refused, runner, tf = await fake_inbox(env)
    await phone.connect()

    async def post(meta, files):
        fd = aiohttp.FormData()
        fd.add_field("meta", json.dumps(meta), content_type="application/json")
        for name, data, mime in files:
            fd.add_field("file", data, filename=name, content_type=mime)
        async with aiohttp.ClientSession() as s:
            async with s.post(env.tls + "/v1/share", data=fd, ssl=phone.ctx()) as r:
                return r.status, await r.json()

    meta = {"share_id": "s_cap1", "kind": "image", "intent": "what", "text": "Quick capture photo (21.10 14:03)",
            "files": [{"name": "1-photo-20261021-140300.jpg", "mime": "image/jpeg", "bytes": 5}]}
    st, j = await post(meta, [("1-photo-20261021-140300.jpg", b"\xff\xd8abc", "image/jpeg")])
    assert st == 200 and j["accepted"] == 1
    await until(lambda: got)
    t = got[0]["task"]
    assert got[0]["source"] == "app" and t.startswith("[share s_cap1] image share") and "skill vision-actions" in t
    assert "vision gate FILE" in t and "01-1-photo-20261021-140300.jpg" in t and "Quick capture photo" in t
    assert not env.notes("share") and not refused
    tf.write_text("wrong\n")                              # inbox refuses -> the note fallback
    meta2 = dict(meta, share_id="s_cap2")
    st, j = await post(meta2, [("p.jpg", b"\xff\xd8abc", "image/jpeg")])
    await until(lambda: env.notes("share"))
    await runner.cleanup()
    notes = env.notes("share")
    assert len(notes) == 1 and "s_cap2" in open(os.path.join(env.paths.inbox, notes[0])).read()
    st, j = await env.call("POST", "/internal/chat", {"text": "card follows", "reply_to": "a1b2c3d4"})
    assert st == 200 and j["ok"], j
    rows = [[{"text": "✅ Do all", "data": "vp:ok:abc123", "style": "success"}, {"text": "✖", "callback_data": "vp:no:abc123"}]]
    st, j = await env.call("POST", "/internal/chat", {"text": "📷 1 action", "buttons": rows})   # voice-plan's rows as-is
    assert st == 200 and j["ok"], j
    st, j = await env.call("POST", "/internal/chat", {"text": "bad", "buttons": [[{"text": "x"}]]})
    assert st == 400
