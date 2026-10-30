"""M3 voice: /v1/voice proxy (fake relay), in-app ring (call.ring/call.state), /v1/trackid (fake trackid), crate bridge."""
import asyncio
import json
import os
import stat
import subprocess

import aiohttp
import pytest
from aiohttp import WSMsgType, web

from conftest import PHONE_BIN
from phonegw import bridge

TOKEN = "relay-test-token"


class FakeRelay:
    """The voice relay's loopback API + /ws: ticket bodies recorded, live 24 kHz audio, a filtered junk frame."""
    def __init__(self, ticket_status=200, live=0):
        self.ticket_status, self.live = ticket_status, live
        self.tickets, self.up_bytes, self.texts, self.starts = [], 0, [], []
        self.valid = set()

    async def start(self):
        app = web.Application()
        app.router.add_post("/internal/ticket", self.ticket)
        app.router.add_get("/internal/status", self.status)
        app.router.add_get("/ws", self.ws)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = self.runner.addresses[0][1]
        return self

    def auth(self, r):
        return r.headers.get("Authorization") == "Bearer " + TOKEN

    async def ticket(self, r):
        if not self.auth(r):
            return web.json_response({"error": "unauthorized"}, status=401)
        body = await r.json()
        self.tickets.append(body)
        if self.ticket_status != 200:
            return web.json_response({"error": "busy"}, status=self.ticket_status)
        t = "tk%d" % len(self.tickets)
        self.valid.add(t)
        return web.json_response({"ticket": t, "ws": "/ws", "expires_s": 30})

    async def status(self, r):
        if not self.auth(r):
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response({"live": self.live, "call_ids": ["c1"] * self.live})

    async def ws(self, r):
        t = r.query.get("ticket")
        if t not in self.valid:
            return web.Response(status=403)
        self.valid.discard(t)
        ws = web.WebSocketResponse()
        await ws.prepare(r)
        first = await ws.receive(timeout=5)
        self.starts.append(json.loads(first.data))
        await ws.send_str(json.dumps({"type": "state", "state": "live", "provider": "gemini", "mode": "open"}))
        await ws.send_str(json.dumps({"type": "secret", "text": "must not reach the phone"}))
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                self.up_bytes += len(msg.data)
                if self.up_bytes >= 12800 and not getattr(self, "answered", False):
                    self.answered = True
                    await ws.send_str(json.dumps({"type": "transcript", "who": "assistant", "text": "Hi, it's Claude"}))
                    await ws.send_bytes(b"\x01\x00" * 2400)          # 100 ms at 24 kHz
                    await ws.send_str(json.dumps({"type": "flush"}))
            elif msg.type == WSMsgType.TEXT:
                c = json.loads(msg.data)
                self.texts.append(c)
                if c.get("type") == "hangup":
                    await ws.send_str(json.dumps({"type": "hangup", "reason": "bye"}))
                    break
        await ws.close()
        return ws


@pytest.fixture
async def relay(env, tmp_path):
    fr = await FakeRelay().start()
    tok = tmp_path / "relay-token"
    tok.write_text(TOKEN + "\n")
    env.gw.cfg.update({"voice_internal_url": "http://127.0.0.1:%d" % fr.port,
                       "voice_ws_url": "ws://127.0.0.1:%d/ws" % fr.port, "voice_token_file": str(tok),
                       "ring_min_s": 1, "ring_grace_s": 0.5, "voice_grace_s": 0})   # resume: test_voice_resume.py
    yield fr
    await env.gw.voice_stop()
    await fr.runner.cleanup()


async def voice_ws(env, phone, query=""):
    s = aiohttp.ClientSession()
    ws = await s.ws_connect(env.tls.replace("https", "wss") + "/v1/voice" + query, ssl=phone.ctx())
    return s, ws


async def collect(ws, until, timeout=5):
    texts, audio = [], 0
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        try:
            m = await ws.receive(timeout=max(0.05, end - loop.time()))
        except asyncio.TimeoutError:
            break
        if m.type == WSMsgType.TEXT:
            texts.append(json.loads(m.data))
        elif m.type == WSMsgType.BINARY:
            audio += len(m.data)
        else:
            break
        if until(texts, audio):
            break
    return texts, audio


async def test_tile_call_proxies_audio_and_filters_frames(env, phone, relay, caplog):
    s, ws = await voice_ws(env, phone)
    try:
        texts, _ = await collect(ws, lambda t, a: any(x.get("state") == "live" for x in t))
        assert [x["state"] for x in texts if x["type"] == "state"] == ["connecting", "live"]
        assert relay.tickets == [{"via": "app"}] and relay.starts == [{"type": "start", "mode": "open"}]
        await ws.send_str(json.dumps({"type": "text", "text": "typed"}))      # only hangup goes up
        await ws.send_bytes(b"\x00" * 70000)                                   # oversize frame dropped
        for _ in range(10):
            await ws.send_bytes(b"\x02\x00" * 640)                             # 40 ms at 16 kHz
        texts, audio = await collect(ws, lambda t, a: any(x["type"] == "flush" for x in t))
        assert audio == 4800 and [x["type"] for x in texts] == ["transcript", "flush"]
        assert relay.up_bytes == 12800 and relay.texts == []
        await ws.send_str(json.dumps({"type": "hangup"}))
        texts, _ = await collect(ws, lambda t, a: any(x["type"] == "hangup" for x in t))
        assert texts[-1] == {"type": "hangup", "reason": "bye"}
        assert relay.texts == [{"type": "hangup"}]
    finally:
        await ws.close()
        await s.close()
    assert not any(x in r.getMessage() for r in caplog.records for x in ("Hi, it's Claude", "must not reach"))
    assert await wait_for(lambda: not env.gw.voice_live)


async def wait_for(pred, timeout=3):
    for _ in range(int(timeout / 0.02)):
        if pred():
            return True
        await asyncio.sleep(0.02)
    return pred()


@pytest.mark.parametrize("status,code,why", [(429, 4429, "busy"), (500, 4503, "relay_refused")])
async def test_relay_refusal_maps_to_close_codes(env, phone, relay, status, code, why):
    relay.ticket_status = status
    s, ws = await voice_ws(env, phone)
    texts, _ = await collect(ws, lambda t, a: any(x["type"] == "hangup" for x in t))
    assert texts[0] == {"type": "state", "state": "connecting"} and texts[1]["code"] == why
    await ws.receive(timeout=3)
    assert ws.close_code == code
    await s.close()


async def test_relay_down_and_unknown_call(env, phone, relay):
    env.gw.cfg["voice_internal_url"] = "http://127.0.0.1:9"                  # nothing listens
    s, ws = await voice_ws(env, phone)
    texts, _ = await collect(ws, lambda t, a: any(x["type"] == "hangup" for x in t))
    assert texts[1]["code"] == "relay_down"
    await ws.receive(timeout=3)
    assert ws.close_code == 4503
    await s.close()
    s, ws = await voice_ws(env, phone, "?call_id=k_bogus12345")
    texts, _ = await collect(ws, lambda t, a: any(x["type"] == "hangup" for x in t))
    assert texts[0]["code"] == "unknown_call"
    await ws.receive(timeout=3)
    assert ws.close_code == 4404 and relay.tickets == []
    await s.close()


async def test_voice_needs_a_client_cert(env, phone, relay):
    async with aiohttp.ClientSession() as s:
        with pytest.raises(aiohttp.WSServerHandshakeError) as e:
            await s.ws_connect(env.tls.replace("https", "wss") + "/v1/voice", ssl=phone.ctx(cert=False))
        assert e.value.status == 401


async def ring(env, body):
    return await env.call("POST", "/internal/ring", body)


async def test_ring_answered_then_voice_opens_with_the_reason(env, phone, relay):
    await phone.connect()
    task = asyncio.ensure_future(ring(env, {"reason": "Your last train leaves in 25 min", "say": "Last train soon",
                                            "timeout_s": 5}))
    env_ring = await phone.recv("call.ring")
    b = env_ring["body"]
    assert b["reason"] == "Your last train leaves in 25 min" and b["timeout_s"] == 5 and env_ring["ack"] is True
    cid = b["call_id"]
    await phone.send("call.state", {"call_id": cid, "state": "ringing", "at": 1792513205}, ack=True)
    await asyncio.sleep(0.1)
    await phone.send("call.state", {"call_id": cid, "state": "answered", "at": 1792513206}, ack=True)
    st, res = await task
    assert st == 200 and res["status"] == "answered" and res["reason_shown"] is True and res["shown_ms"] is not None
    # busy while the answered call hasn't opened its voice socket yet
    assert (await ring(env, {"reason": "again"}))[1]["status"] == "in_call"
    s, ws = await voice_ws(env, phone, "?call_id=" + cid)
    await collect(ws, lambda t, a: any(x.get("state") == "live" for x in t))
    assert relay.tickets[-1] == {"via": "app", "reason": "Your last train leaves in 25 min", "say": "Last train soon"}
    await ws.send_str(json.dumps({"type": "hangup"}))
    await collect(ws, lambda t, a: any(x["type"] == "hangup" for x in t))
    await ws.close()
    await s.close()
    s, ws = await voice_ws(env, phone, "?call_id=" + cid)               # one use only
    texts, _ = await collect(ws, lambda t, a: any(x["type"] == "hangup" for x in t))
    assert texts[0]["code"] == "unknown_call"
    await s.close()


async def test_voice_open_before_the_answered_frame_counts_as_answer(env, phone, relay):
    await phone.connect()
    task = asyncio.ensure_future(ring(env, {"reason": "Leave now for the train", "timeout_s": 5}))
    cid = (await phone.recv("call.ring"))["body"]["call_id"]
    s, ws = await voice_ws(env, phone, "?call_id=" + cid)               # the race: socket first, frame later
    await collect(ws, lambda t, a: any(x.get("state") == "live" for x in t))
    assert (await task)[1]["status"] == "answered"
    await phone.send("call.state", {"call_id": cid, "state": "answered", "at": 1792513206}, ack=True)
    assert relay.tickets[-1]["reason"] == "Leave now for the train"
    await ws.close()
    await s.close()


async def test_ring_declined_missed_no_answer_and_lint(env, phone, relay):
    await phone.connect()
    for state, want in (("declined", "declined"), ("missed", "missed")):
        task = asyncio.ensure_future(ring(env, {"reason": "Disk 3 is disabled", "timeout_s": 5}))
        b = (await phone.recv("call.ring"))["body"]
        await phone.send("call.state", {"call_id": b["call_id"], "state": state, "at": 1792513205}, ack=True)
        assert (await task)[1]["status"] == want
    # identifiers / 5+ digit runs never reach the lock screen: the ring shows just "Claude"
    task = asyncio.ensure_future(ring(env, {"reason": "Code 123456 for the parcel", "timeout_s": 3}))
    b = (await phone.recv("call.ring"))["body"]
    assert b["reason"] is None
    st, res = await task                                                  # nobody answers -> cancel sent
    assert res["status"] == "no_answer"
    cancel = await phone.recv("call.cancel")
    assert cancel["body"] == {"call_id": b["call_id"]}
    assert (await ring(env, {"reason": "fourth"}))[1]["status"] == "rate_limited"     # 3 per hour
    env.gw.ring_hist.clear()
    task = asyncio.ensure_future(ring(env, {"reason": "Code 123456 is long", "show": "Disk alert", "timeout_s": 3}))
    b = (await phone.recv("call.ring"))["body"]
    assert b["reason"] == "Disk alert"                                    # --show is the lock-screen text
    await phone.send("call.state", {"call_id": b["call_id"], "state": "declined", "at": 1792513205}, ack=True)
    assert (await task)[1]["status"] == "declined"


async def test_ring_refusals(env, phone, relay):
    assert (await ring(env, {"reason": "x"}))[1]["status"] == "offline"
    await phone.connect()
    relay.live = 1
    assert (await ring(env, {"reason": "x"}))[1]["status"] == "in_call"
    env.gw.cfg["voice_internal_url"] = "http://127.0.0.1:9"
    assert (await ring(env, {"reason": "x"}))[1]["status"] == "unavailable"


async def test_call_state_schema_and_unknown_call(env, phone, relay):
    await phone.connect()
    await phone.send("call.state", {"call_id": "k_unknown123", "state": "answered", "at": 1792513205}, ack=True)
    assert (await phone.recv("ack"))["t"] == "ack"
    await phone.send("call.state", {"call_id": "k_unknown123", "state": "picked"}, ack=True)
    assert (await phone.recv("error"))["body"]["code"]


def fake_trackid(tmp_path, code=0, out=None):
    rec = tmp_path / "trackid-args.json"
    script = tmp_path / "fake-trackid"
    payload = json.dumps(out if out is not None else {
        "status": "match", "track": {"artist": "Ann Artist", "title": "Gravity"},
        "confidence": {"level": "high", "windows_matched": 2, "windows_tried": 3},
        "your_artists": [{"name": "Ann Artist", "how": "artist"}], "id": "tid-7f3a2c1b", "stored": True})
    script.write_text("#!/usr/bin/env python3\nimport json, os, sys, stat\n"
                      "p = sys.argv[1]\nst = os.stat(p)\n"
                      "json.dump({'argv': sys.argv[1:], 'mode': oct(stat.S_IMODE(st.st_mode)), "
                      "'dirmode': oct(stat.S_IMODE(os.stat(os.path.dirname(p)).st_mode)), 'size': st.st_size}, "
                      "open(%r, 'w'))\nprint(%r)\nsys.exit(%d)\n" % (str(rec), payload, code))
    script.chmod(0o755)
    return str(script), rec


async def post_clip(env, phone, data, ctype="audio/mp4"):
    async with aiohttp.ClientSession() as s:
        async with s.post(env.tls + "/v1/trackid", data=data, headers={"Content-Type": ctype}, ssl=phone.ctx()) as r:
            return r.status, await r.json()


async def test_trackid_match_crate_and_clip_deleted(env, phone, tmp_path, monkeypatch):
    script, rec = fake_trackid(tmp_path)
    monkeypatch.setenv("TRACKID_BIN", script)
    clip = os.urandom(300 * 1024)                                         # > the 1 MiB-free path, < 2 MiB cap
    st, r = await post_clip(env, phone, clip)
    assert st == 200, r
    import jsonschema
    from conftest import PROTOCOL
    jsonschema.validate(r, json.load(open(os.path.join(PROTOCOL, "schema", "http.trackid.response.json"))))
    assert r["match"] and r["artist"] == "Ann Artist" and r["confidence"] == "high" and r["followed"] is True
    assert r["id"] == "tid-7f3a2c1b" and r["push_id"] and r["nonce"]
    seen = json.loads(rec.read_text())
    assert seen["argv"][1:] == ["--json", "--source", "app", "--min-store", "medium"]
    assert seen["mode"] == "0o600" and seen["dirmode"] == "0o700" and seen["size"] == len(clip)
    assert not os.path.exists(seen["argv"][0]) and not os.path.exists(os.path.dirname(seen["argv"][0]))
    # the app's ➕ Crate = an ordinary action on that push id
    await phone.connect()
    await phone.send("action", {"push_id": r["push_id"], "nonce": r["nonce"], "verb": "crate", "arg": r["id"],
                                "at": 1792513300}, ack=True)
    await phone.recv("ack")
    row = env.gw.db.one("SELECT verb, arg FROM push_log WHERE id=?", r["push_id"])
    assert row["verb"] == "crate" and row["arg"] == "tid-7f3a2c1b"
    plan = bridge.plan({"push_id": r["push_id"], "verb": "crate", "arg": "tid-7f3a2c1b", "level": "info"})
    assert plan[0][0][1:] == ["releases", "crate", "tid-7f3a2c1b"] and plan[0][0][0].endswith("rave-radar")
    assert bridge.plan({"verb": "crate", "arg": "tid-1;rm", "level": "info"}) == []


async def test_trackid_no_match_errors_and_limits(env, phone, tmp_path, monkeypatch):
    script, _ = fake_trackid(tmp_path, code=1, out={"status": "no_match"})
    monkeypatch.setenv("TRACKID_BIN", script)
    st, r = await post_clip(env, phone, os.urandom(8192))
    assert st == 200 and r["match"] is False and r["ok"] is True
    script, _ = fake_trackid(tmp_path, code=3, out={"status": "error"})
    monkeypatch.setenv("TRACKID_BIN", script)
    assert (await post_clip(env, phone, os.urandom(8192)))[0] == 503
    assert (await post_clip(env, phone, os.urandom(8192), "text/plain"))[0] == 415
    assert (await post_clip(env, phone, os.urandom(100)))[0] == 400
    assert (await post_clip(env, phone, os.urandom((2 << 20) + 10)))[0] == 413
    # a low-confidence match is shown but not stored: no crate push
    script, _ = fake_trackid(tmp_path, out={"status": "match", "track": {"artist": "A", "title": "B"},
                                            "confidence": {"level": "low"}, "id": None, "stored": False})
    monkeypatch.setenv("TRACKID_BIN", script)
    st, r = await post_clip(env, phone, os.urandom(8192))
    assert r["match"] and r["confidence"] == "low" and r["id"] is None and r["push_id"] is None


async def test_phone_ring_cli_exit_codes(env, relay):
    import sys
    phone_bin = os.environ.get("PHONE_RING_BIN") or PHONE_BIN
    p = await asyncio.create_subprocess_exec(sys.executable, "-I", phone_bin, "ring", "--reason", "test", "--json",
                                             env=env.cli_env(), stdout=asyncio.subprocess.PIPE)
    out, _ = await p.communicate()
    assert p.returncode == 4 and json.loads(out)["status"] == "offline"
    p = await asyncio.create_subprocess_exec(sys.executable, "-I", phone_bin, "ring", env=env.cli_env(),
                                             stderr=asyncio.subprocess.PIPE)
    await p.communicate()
    assert p.returncode == 2


async def test_ring_quick_reply_reaches_my_session_or_a_note(env, phone, relay):
    from test_ws import fake_inbox, until
    got, refused, runner, tf = await fake_inbox(env)
    await phone.connect()
    task = asyncio.ensure_future(ring(env, {"reason": "Train update", "timeout_s": 5}))
    b = (await phone.recv("call.ring"))["body"]
    await phone.send("call.state", {"call_id": b["call_id"], "state": "declined", "reply": "text", "at": 1792513205}, ack=True)
    res = (await task)[1]
    assert res["status"] == "declined" and res["reply"] == "text"
    assert await until(lambda: got)
    assert got[0]["source"] == "app" and got[0]["task"].startswith("[ring %s]" % b["call_id"])
    assert "Text me instead" in got[0]["task"] and "Train update" in got[0]["task"] and not env.notes("ring-reply")
    tf.write_text("wrong\n")                                  # inbox refuses -> the note fallback
    env.gw.ring_hist.clear()
    task = asyncio.ensure_future(ring(env, {"reason": "Disk alert", "timeout_s": 5}))
    b = (await phone.recv("call.ring"))["body"]
    await phone.send("call.state", {"call_id": b["call_id"], "state": "declined", "reply": "busy", "at": 1792513205}, ack=True)
    assert (await task)[1]["reply"] == "busy"
    assert await until(lambda: env.notes("ring-reply"))
    text = open(os.path.join(env.paths.inbox, env.notes("ring-reply")[0])).read()
    assert "**Busy**" in text and "Disk alert" in text
    env.gw.ring_hist.clear()                                  # a plain decline carries no reply and hands nothing off
    task = asyncio.ensure_future(ring(env, {"reason": "x", "timeout_s": 5}))
    b = (await phone.recv("call.ring"))["body"]
    await phone.send("call.state", {"call_id": b["call_id"], "state": "declined", "at": 1792513205}, ack=True)
    assert "reply" not in (await task)[1]
    await runner.cleanup()


# ------------------------------------------------------------ call.state over the HTTPS sync path (queued offline)
def _env(t, body):
    from phonegw.util import now_s, ulid
    return {"v": 1, "id": ulid(), "t": t, "ts": now_s(), "ack": True, "body": body}


async def test_call_state_over_https_sync_resolves_the_ring(env, phone, relay):
    """The app's outbox posts a queued call.state to /v1/events (GatewayApi.pathFor): it must count like the socket frame."""
    await phone.connect()
    task = asyncio.ensure_future(ring(env, {"reason": "Disk 3 is disabled", "timeout_s": 5}))
    cid = (await phone.recv("call.ring"))["body"]["call_id"]
    st, j = await phone.http("POST", "/v1/events", {"items": [
        _env("call.state", {"call_id": cid, "state": "declined", "at": 1792513205})]})
    assert st == 200 and j == {"ok": True, "accepted": 1, "dup": 0}
    assert (await task)[1]["status"] == "declined"


async def test_late_call_state_does_not_block_queued_events(env, phone, relay):
    """A stale call.state (ring long gone) in the same batch as events: all accepted, the call.state just ignored."""
    ev = {"kind": "alarm", "next": 1792560600}
    items = [_env("call.state", {"call_id": "k_gone12345", "state": "missed", "at": 1792513205}), _env("event", ev)]
    st, j = await phone.http("POST", "/v1/events", {"items": items})
    assert st == 200 and j["accepted"] == 2
    st, j = await phone.http("POST", "/v1/events", {"items": items})            # the outbox retries: duplicates
    assert st == 200 and j["dup"] == 2


def test_every_c2s_type_has_an_https_batch_path():
    """Drift guard: each c2s type in the protocol index is accepted on the path the app's GatewayApi.pathFor uses."""
    from conftest import PROTOCOL
    from phonegw.server import BATCH_ALLOWED
    index = json.load(open(os.path.join(PROTOCOL, "schema", "index.json")))
    own = {"ack": "/v1/ack", "action": "/v1/action", "chat": "/v1/chat", "card.answer": "/v1/card-answer"}
    missing = [t for t in index["c2s"] if t not in BATCH_ALLOWED[own.get(t, "/v1/events")]]
    assert missing == []
