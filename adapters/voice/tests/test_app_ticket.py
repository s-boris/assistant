"""Phone app calls: /internal/ticket (loopback bearer) -> /ws with the ticket alone (no page login, no Origin),
open mode, outbound opening for an answered in-app ring, not counted toward the daily page minutes."""
import asyncio
import json
import time

import aiohttp
from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from util import TOKEN, Rig, until
from voice_relay.audio import tone


def run(coro):
    return asyncio.run(coro)


async def clients(r):
    pub = TestClient(TestServer(r.relay.public_app()))
    await pub.start_server()
    return pub


async def ticket(r, body, token=TOKEN):
    async with aiohttp.ClientSession() as s:
        async with s.post("http://127.0.0.1:%d/internal/ticket" % r.iport, json=body,
                          headers={"Authorization": "Bearer " + token}) as resp:
            return resp.status, await resp.json()


async def status(r):
    async with aiohttp.ClientSession() as s:
        async with s.get("http://127.0.0.1:%d/internal/status" % r.iport,
                         headers={"Authorization": "Bearer " + TOKEN}) as resp:
            return await resp.json()


async def frames_until(ws, pred, timeout=5.0):
    """Collect frames until pred(events, audio_bytes) holds."""
    evs, audio = [], 0
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            m = await ws.receive(timeout=max(0.05, end - time.monotonic()))
        except asyncio.TimeoutError:
            break
        if m.type == WSMsgType.BINARY:
            audio += len(m.data)
        elif m.type == WSMsgType.TEXT:
            evs.append(json.loads(m.data))
        else:
            break
        if pred(evs, audio):
            break
    return evs, audio


def test_ticket_rules(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path))
        try:
            assert (await ticket(r, {"via": "app"}, token="wrong"))[0] == 401
            assert (await ticket(r, {"via": "page"}))[0] == 400
            assert (await ticket(r, ["x"]))[0] == 400
            st, body = await ticket(r, {"via": "app"})
            assert st == 200 and body["ws"] == "/ws" and body["expires_s"] == 30 and len(body["ticket"]) >= 24
            c = await clients(r)
            try:
                assert (await c.get("/ws?ticket=nope")).status == 403
                # a page ticket path still needs Origin + login: an app ticket is not a page ticket and vice versa
                assert (await c.post("/api/ticket", headers={"X-Voice": "1"})).status == 401
                # expired app ticket
                st, body = await ticket(r, {"via": "app"})
                for v in r.relay.app_tickets.values():
                    v["exp"] = time.time() - 1
                assert (await c.get("/ws?ticket=" + body["ticket"])).status == 403
            finally:
                await c.close()
        finally:
            await r.close()
    run(main())


def test_app_session_open_mode_no_page_minutes(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path), script=["hello there"], max_live_sessions=1)
        c = await clients(r)
        try:
            _, body = await ticket(r, {"via": "app"})
            ws = await c.ws_connect("/ws?ticket=" + body["ticket"])       # no Origin, no cookie
            await ws.send_str(json.dumps({"type": "start"}))
            evs, _ = await frames_until(ws, lambda e, a: any(x["type"] == "state" for x in e))
            live = [x for x in evs if x["type"] == "state"][0]
            assert live["state"] == "live" and live["mode"] == "open"         # always open mic for the app
            s = await status(r)
            assert s["live"] == 1 and s["app_calls"] == 1 and len(s["call_ids"]) == 1
            # busy: the live cap also covers app tickets
            assert (await ticket(r, {"via": "app"}))[0] == 429
            # reused ticket refused
            assert (await c.get("/ws?ticket=" + body["ticket"])).status == 403
            # 1 s of 16 kHz audio -> the fake answers "hello there" with audio at 24 kHz
            pcm = tone(16000, 1000)
            for i in range(0, len(pcm), 1280):
                await ws.send_bytes(pcm[i:i + 1280])
            evs, audio = await frames_until(ws, lambda e, a: a > 0 and any(x["type"] == "turn" for x in e))
            assert audio > 0 and any(x["type"] == "transcript" and x["who"] == "assistant" for x in evs)
            assert r.gem.log["audio_bytes"] >= 32000
            await ws.send_str(json.dumps({"type": "hangup"}))
            await ws.close()
            assert await until(lambda: not r.relay.live)
            s = await status(r)
            assert s["live"] == 0 and s["call_ids"] == [] and s["app_calls"] == 0
            assert r.relay.st.seconds_today() == 0                         # app calls: no page minutes
            logs = list((tmp_path / "state").rglob("*.jsonl"))
            kinds = [json.loads(l).get("kind") for f in logs for l in open(f) if l.strip()]
            assert "app" in kinds
        finally:
            await c.close()
            await r.close()
    run(main())


def test_answered_ring_opens_like_an_outbound_call(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path), opening_wait_s=0.2)
        c = await clients(r)
        try:
            _, body = await ticket(r, {"via": "app", "reason": "Your last train leaves in 25 min", "say": ""})
            t0 = time.monotonic()
            ws = await c.ws_connect("/ws?ticket=" + body["ticket"])
            await ws.send_str(json.dumps({"type": "start", "mode": "open"}))
            evs, audio = await frames_until(ws, lambda e, a: a > 0)
            first_audio_ms = (time.monotonic() - t0) * 1000
            assert audio > 0, evs
            opening = r.gem.log["injects"][0]
            assert opening.startswith("[outbound call]") and "last train" in opening
            assert first_audio_ms < 2000
            t1 = time.monotonic()
            await ws.close()
            assert await until(lambda: not r.relay.live, timeout=3)
            assert time.monotonic() - t1 < 1.0          # a dropped app socket frees the slot at once
            logs = list((tmp_path / "state").rglob("*.jsonl"))
            recs = [json.loads(l) for f in logs for l in open(f) if l.strip()]
            assert any(x.get("kind") == "app-out" for x in recs)
        finally:
            await c.close()
            await r.close()
    run(main())


def test_link_listener_serves_phonegw_from_another_container(tmp_path):
    """Hub mode: the link port (the phone gateway in another container) mints app tickets and reads status with the
    link token only, runs /ws for an app ticket from a peer that isn't 127.0.0.1 (here 127.0.0.2), and has nothing
    else; the loopback /ws still refuses that peer."""
    from voice_relay.fakes import _serve

    async def main():
        (tmp_path / "link").write_text("link-token-for-tests\n")
        r = await Rig.make(str(tmp_path), script=["hello there"], link_token_file=str(tmp_path / "link"))
        lrun, lport = await _serve(r.relay.link_app())
        prun, pport = await _serve(r.relay.public_app())
        base, link = "http://127.0.0.1:%d" % lport, {"Authorization": "Bearer link-token-for-tests"}
        try:
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(local_addr=("127.0.0.2", 0))) as s:
                async def post(path, headers, body=None):
                    async with s.post(base + path, json={"via": "app"} if body is None else body, headers=headers) as rp:
                        return rp.status, (await rp.json()) if rp.content_type == "application/json" else None

                assert (await post("/internal/ticket", {"Authorization": "Bearer " + TOKEN}))[0] == 401  # local token
                assert (await post("/internal/ticket", {}))[0] == 401
                assert (await post("/internal/call", link, {"reason": "x"}))[0] == 404     # not on the link
                assert (await post("/internal/answer", link, {"id": "1", "text": "x"}))[0] == 404
                async with s.get(base + "/internal/status", headers=link) as rp:
                    assert rp.status == 200 and (await rp.json())["live"] == 0
                st, body = await post("/internal/ticket", link)
                _, other = await post("/internal/ticket", link)
                assert st == 200 and body["ws"] == "/ws"
                async with s.get(base + "/ws?ticket=nope") as rp:                       # page or no ticket: 403
                    assert rp.status == 403
                ws = await s.ws_connect("ws://127.0.0.1:%d/ws?ticket=%s" % (lport, body["ticket"]))
                await ws.send_str(json.dumps({"type": "start"}))
                evs, _ = await frames_until(ws, lambda e, a: any(x["type"] == "state" for x in e))
                assert [x for x in evs if x["type"] == "state"][0]["state"] == "live"
                await ws.send_str(json.dumps({"type": "hangup"}))
                await ws.close()
                async with s.get("http://127.0.0.1:%d/ws?ticket=%s" % (pport, other["ticket"])) as rp:
                    assert rp.status == 403                                             # loopback /ws: not 127.0.0.2
        finally:
            await lrun.cleanup()
            await prun.cleanup()
            await r.close()
    run(main())


def test_link_listener_without_a_token_file_refuses_everything(tmp_path):
    from voice_relay.fakes import _serve

    async def main():
        r = await Rig.make(str(tmp_path))                     # link_token_file "" (the default)
        lrun, lport = await _serve(r.relay.link_app())
        try:
            async with aiohttp.ClientSession() as s:
                for tok in ("", TOKEN):
                    async with s.post("http://127.0.0.1:%d/internal/ticket" % lport, json={"via": "app"},
                                      headers={"Authorization": "Bearer " + tok}) as rp:
                        assert rp.status == 401
        finally:
            await lrun.cleanup()
            await r.close()
    run(main())
