"""In-app call resume: the app's connection drops (closed, stalled, replaced) and the call stays up for voice_grace_s;
/v1/voice?resume=<id> from the same device re-attaches it to the same relay socket (link lost/back to the relay,
queued text frames replayed, audio during the gap dropped); grace over -> hangup why=app_lost to the relay; late or
foreign resumes get 4410. Fake relay on loopback, fake usage forwarder."""
import asyncio
import json

import aiohttp
import pytest
from aiohttp import WSMsgType, web

from test_voice import TOKEN, collect, voice_ws, wait_for


class LinkRelay:
    """A relay /ws that records every text frame from phonegw and lets the test send frames down."""
    def __init__(self):
        self.texts, self.up_bytes, self.sock, self.n = [], 0, None, 0

    async def start(self):
        app = web.Application()
        app.router.add_post("/internal/ticket", self.ticket)
        app.router.add_get("/ws", self.ws)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = self.runner.addresses[0][1]
        return self

    async def ticket(self, r):
        self.n += 1
        return web.json_response({"ticket": "tk%d" % self.n, "ws": "/ws", "expires_s": 30})

    async def ws(self, r):
        ws = web.WebSocketResponse()
        await ws.prepare(r)
        self.sock = ws
        await ws.receive(timeout=5)                                      # start
        await ws.send_str(json.dumps({"type": "state", "state": "live", "provider": "gemini", "mode": "open"}))
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                self.up_bytes += len(msg.data)
            elif msg.type == WSMsgType.TEXT:
                c = json.loads(msg.data)
                self.texts.append(c)
                if c.get("type") == "hangup":
                    await ws.send_str(json.dumps({"type": "hangup", "reason": "bye"}))
                    break
        await ws.close()
        return ws

    def links(self):
        return [t for t in self.texts if t.get("type") == "link"]


class Usage:
    def __init__(self):
        self.ev = []

    def event(self, feature, kind="invoke", outcome=None, ms=None, actor="owner"):
        self.ev.append((feature, outcome, ms, actor))


@pytest.fixture
async def lrelay(env, tmp_path):
    fr = await LinkRelay().start()
    tok = tmp_path / "relay-token"
    tok.write_text(TOKEN + "\n")
    env.gw.cfg.update({"voice_internal_url": "http://127.0.0.1:%d" % fr.port,
                       "voice_ws_url": "ws://127.0.0.1:%d/ws" % fr.port, "voice_token_file": str(tok),
                       "voice_grace_s": 3, "voice_stall_s": 0, "voice_heartbeat_s": 5})
    env.gw.usage_fwd = Usage()
    yield fr
    await env.gw.voice_stop()
    await fr.runner.cleanup()


async def start_call(env, phone):
    s, ws = await voice_ws(env, phone)
    texts, _ = await collect(ws, lambda t, a: any(x.get("state") == "live" for x in t))
    rid = [x for x in texts if x["type"] == "resume"][0]
    assert rid["grace_s"] == int(env.gw.cfg["voice_grace_s"]) and rid["id"].startswith("v_")
    return s, ws, rid["id"]


async def drop(s, ws):
    """The phone's line goes away (no hang-up frame)."""
    await ws.close()
    await s.close()


async def test_dropped_line_resumes_the_same_call(env, phone, lrelay):
    s, ws, rid = await start_call(env, phone)
    for _ in range(5):
        await ws.send_bytes(b"\x02\x00" * 640)
    assert await wait_for(lambda: lrelay.up_bytes == 6400)
    await drop(s, ws)
    assert await wait_for(lambda: lrelay.links() == [{"type": "link", "state": "lost"}])
    assert env.gw.voice_live[rid]["line"] == "down"
    await lrelay.sock.send_bytes(b"\x01\x00" * 2400)                       # said into the gap: dropped
    await lrelay.sock.send_str(json.dumps({"type": "status", "working": 1}))   # queued for the resume
    await lrelay.sock.send_str(json.dumps({"type": "flush"}))                # not queued: no audio to flush
    await asyncio.sleep(0.3)
    s2, ws2 = await voice_ws(env, phone, "?resume=" + rid)
    texts, audio = await collect(ws2, lambda t, a: any(x["type"] == "status" for x in t))
    assert texts[0]["type"] == "resumed" and texts[0]["id"] == rid and texts[0]["gap_ms"] >= 250
    assert [x["type"] for x in texts] == ["resumed", "status"] and audio == 0
    assert await wait_for(lambda: lrelay.links()[-1]["state"] == "back")
    back = lrelay.links()[-1]
    assert back["gap_ms"] == texts[0]["gap_ms"]
    await ws2.send_bytes(b"\x02\x00" * 640)                                  # his audio flows on the same relay socket
    assert await wait_for(lambda: lrelay.up_bytes == 6400 + 1280) and lrelay.n == 1
    assert env.gw.voice_live[rid]["line"] == "up"
    await lrelay.sock.send_bytes(b"\x01\x00" * 100)
    _, audio = await collect(ws2, lambda t, a: a > 0)
    assert audio == 200
    await ws2.send_str(json.dumps({"type": "hangup"}))
    texts, _ = await collect(ws2, lambda t, a: any(x["type"] == "hangup" for x in t))
    assert texts[-1] == {"type": "hangup", "reason": "bye"}
    assert await wait_for(lambda: not env.gw.voice_live and not env.gw.vcalls)
    feats = [(f, o, a) for f, o, _, a in env.gw.usage_fwd.ev]
    assert feats == [("phonegw.voice.resume", "ok", "system")]
    assert env.gw.usage_fwd.ev[0][2] == back["gap_ms"]
    await ws2.close()
    await s2.close()


async def test_grace_over_ends_the_call_as_app_lost_and_late_resume_is_refused(env, phone, lrelay):
    env.gw.cfg["voice_grace_s"] = 0.4
    s, ws, rid = await start_call(env, phone)
    await drop(s, ws)
    assert await wait_for(lambda: not env.gw.voice_live)
    assert lrelay.texts == [{"type": "link", "state": "lost"}, {"type": "hangup", "why": "app_lost"}]
    assert ("phonegw.voice.lost", "expired", 400, "system") in env.gw.usage_fwd.ev
    s2, ws2 = await voice_ws(env, phone, "?resume=" + rid)
    texts, _ = await collect(ws2, lambda t, a: any(x["type"] == "hangup" for x in t))
    assert texts[0]["code"] == "call_over"
    await ws2.receive(timeout=3)
    assert ws2.close_code == 4410
    await s2.close()


@pytest.mark.parametrize("rid", ["v_0123456789abcdef", "k_ABCDEFGH12", "../etc", ""])
async def test_unknown_resume_ids_are_refused(env, phone, lrelay, rid):
    s, ws = await voice_ws(env, phone, "?resume=" + rid)
    texts, _ = await collect(ws, lambda t, a: any(x["type"] == "hangup" for x in t))
    assert texts[0] == {"type": "error", "code": "call_over", "message": "That call has ended."}
    await ws.receive(timeout=3)
    assert ws.close_code == 4410 and lrelay.n == 0
    await s.close()


async def test_resume_takes_over_a_half_open_socket(env, phone, lrelay):
    s, ws, rid = await start_call(env, phone)
    s2, ws2 = await voice_ws(env, phone, "?resume=%s&gap_ms=3000" % rid)     # the phone moved on before we noticed
    texts, _ = await collect(ws2, lambda t, a: any(x["type"] == "resumed" for x in t))
    assert texts[-1]["gap_ms"] >= 3000
    m = await ws.receive(timeout=3)
    while m.type == WSMsgType.TEXT:
        m = await ws.receive(timeout=3)
    assert ws.close_code == 4409
    assert [x["state"] for x in lrelay.links()] == ["lost", "back"] and lrelay.links()[1]["gap_ms"] >= 3000
    assert len(env.gw.vcalls) == 1 and env.gw.voice_live[rid]["line"] == "up"
    await asyncio.sleep(0.2)
    assert env.gw.voice_live[rid]["line"] == "up"                         # the old socket's end changes nothing
    await ws2.send_str(json.dumps({"type": "hangup"}))
    await collect(ws2, lambda t, a: any(x["type"] == "hangup" for x in t))
    assert await wait_for(lambda: not env.gw.voice_live)
    for x in (ws, ws2, s, s2):
        await x.close()


async def test_quick_takeover_tells_the_relay_nothing(env, phone, lrelay):
    s, ws, rid = await start_call(env, phone)
    s2, ws2 = await voice_ws(env, phone, "?resume=%s&gap_ms=200" % rid)
    await collect(ws2, lambda t, a: any(x["type"] == "resumed" for x in t))
    await asyncio.sleep(0.2)
    assert lrelay.links() == []
    await ws2.send_str(json.dumps({"type": "hangup"}))
    await collect(ws2, lambda t, a: any(x["type"] == "hangup" for x in t))
    for x in (ws, ws2, s, s2):
        await x.close()


async def test_stalled_uplink_is_closed_4408_and_held(env, phone, lrelay):
    env.gw.cfg["voice_stall_s"] = 0.3
    s, ws, rid = await start_call(env, phone)
    await asyncio.sleep(0.6)                                                # no audio yet: no stall timer
    assert env.gw.voice_live[rid]["line"] == "up"
    await ws.send_bytes(b"\x02\x00" * 640)                                  # audio flowed, then stops
    m = await ws.receive(timeout=3)
    assert m.type in (WSMsgType.CLOSE, WSMsgType.CLOSED) and ws.close_code == 4408
    assert await wait_for(lambda: lrelay.links() == [{"type": "link", "state": "lost"}])
    assert env.gw.voice_live[rid]["line"] == "down"
    await s.close()
    s2, ws2 = await voice_ws(env, phone, "?resume=" + rid)
    texts, _ = await collect(ws2, lambda t, a: any(x["type"] == "resumed" for x in t))
    assert texts[0]["type"] == "resumed"
    await ws2.send_str(json.dumps({"type": "hangup"}))
    await collect(ws2, lambda t, a: any(x["type"] == "hangup" for x in t))
    await ws2.close()
    await s2.close()


async def test_relay_ends_the_call_while_the_line_is_down(env, phone, lrelay):
    s, ws, rid = await start_call(env, phone)
    await drop(s, ws)
    assert await wait_for(lambda: lrelay.links())
    await lrelay.sock.send_str(json.dumps({"type": "hangup", "reason": "goodbye"}))
    assert await wait_for(lambda: not env.gw.voice_live and not env.gw.vcalls)
    s2, ws2 = await voice_ws(env, phone, "?resume=" + rid)
    await collect(ws2, lambda t, a: any(x["type"] == "hangup" for x in t))
    await ws2.receive(timeout=3)
    assert ws2.close_code == 4410
    await s2.close()


async def test_ring_refused_while_a_call_waits_for_its_line(env, phone, lrelay):
    s, ws, rid = await start_call(env, phone)
    await drop(s, ws)
    assert await wait_for(lambda: lrelay.links())
    assert env.gw.voice_live                                               # i_ring's busy check sees the held call
    env.gw.cfg["voice_grace_s"] = 0
    await env.gw._vcall_end(env.gw.vcalls[rid], "test")
    assert not env.gw.voice_live


async def test_grace_zero_ends_on_any_drop_as_before(env, phone, lrelay):
    env.gw.cfg["voice_grace_s"] = 0
    s, ws, rid = await start_call(env, phone)
    await drop(s, ws)
    assert await wait_for(lambda: not env.gw.voice_live)
    assert lrelay.links() == []
