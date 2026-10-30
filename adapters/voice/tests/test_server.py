import asyncio
import json
import time

from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from util import TOKEN, Rig, until
from voice_relay import server as srv
from voice_relay.server import COOKIE, hash_passphrase


def run(coro):
    return asyncio.run(coro)


async def pub_client(r):
    c = TestClient(TestServer(r.relay.public_app()))
    await c.start_server()
    return c


def origin(c):
    return "http://%s:%d" % (c.server.host, c.server.port)


async def login(r, c):
    res = await r.relay.i_link(None)
    path = json.loads(res.body)["path"]
    resp = await c.get(path, allow_redirects=False)
    assert resp.status == 303
    return path, resp


def test_unauthenticated_access_and_headers(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path))
        c = await pub_client(r)
        try:
            resp = await c.get("/", allow_redirects=False)
            assert resp.status == 303 and resp.headers["Location"] == "/login"
            assert (await c.get("/static/app.js")).status == 401
            assert (await c.get("/static/app.css")).status == 200
            assert (await c.get("/static/server.py")).status == 404
            assert (await c.get("/static/..%2Fvoice_relay%2Fserver.py")).status == 404
            resp = await c.get("/login")
            assert "frame-ancestors 'none'" in resp.headers["Content-Security-Policy"]
            assert resp.headers["Cache-Control"] == "no-store" and resp.headers["X-Frame-Options"] == "DENY"
            assert (await c.post("/internal/answer", json={"id": "x", "text": "y"})).status in (404, 405)
            assert (await c.post("/api/ticket", headers={"X-Voice": "1", "Origin": origin(c)})).status == 401
        finally:
            await c.close()
            await r.close()
    run(main())


def test_one_time_link_login_and_reuse(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path))
        c = await pub_client(r)
        try:
            path, resp = await login(r, c)
            cookie = resp.headers["Set-Cookie"]
            assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
            assert (await c.get("/", allow_redirects=False)).status == 200
            assert (await c.get("/static/app.js")).status == 200
            c.session.cookie_jar.clear()
            assert (await c.get(path, allow_redirects=False)).status == 403      # used once
            stored = json.load(open(tmp_path / "state" / "sessions.json"))
            sid = resp.cookies[COOKIE].value
            assert sid not in json.dumps(stored)                                  # only hashes on disk
        finally:
            await c.close()
            await r.close()
    run(main())


def test_login_rate_limit_and_passphrase(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path))
        r.relay.st.passphrase = hash_passphrase("correct horse battery")
        c = await pub_client(r)
        try:
            o = {"Origin": origin(c)}
            assert (await c.post("/login", data={"pw": "nope"}, headers=o)).status == 403
            assert (await c.post("/login", data={"pw": "correct horse battery"})).status == 429  # no Origin
            ok = await c.post("/login", data={"pw": "correct horse battery"}, headers=o, allow_redirects=False)
            assert ok.status == 303 and COOKIE in ok.cookies
            for _ in range(4):
                await c.get("/login?t=bogus")
            assert (await c.get("/login?t=bogus")).status == 429
            assert (await c.post("/login", data={"pw": "correct horse battery"}, headers=o)).status == 429
        finally:
            await c.close()
            await r.close()
    run(main())


def test_ticket_rules_and_websocket_session(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path), max_live_sessions=1)
        c = await pub_client(r)
        try:
            await login(r, c)
            o = origin(c)
            assert (await c.post("/api/ticket", headers={"Origin": o})).status == 403              # no X-Voice
            assert (await c.post("/api/ticket", headers={"X-Voice": "1", "Origin": "https://evil.example"})).status == 403
            t = (await (await c.post("/api/ticket", headers={"X-Voice": "1", "Origin": o})).json())["ticket"]
            assert (await c.get("/ws?ticket=nope", headers={"Origin": o})).status == 403
            ws = await c.ws_connect("/ws?ticket=" + t, headers={"Origin": o})
            await ws.send_str(json.dumps({"type": "start", "mode": "ptt"}))
            first = json.loads((await ws.receive(timeout=5)).data)
            assert first["type"] == "state" and first["provider"] == "gemini"
            t2 = (await (await c.post("/api/ticket", headers={"X-Voice": "1", "Origin": o})).json())["ticket"]
            assert (await c.get("/ws?ticket=" + t2, headers={"Origin": o})).status == 429         # live cap
            assert (await c.get("/ws?ticket=" + t, headers={"Origin": o})).status == 403          # reused
            await ws.send_str(json.dumps({"type": "text", "text": "what's on tomorrow"}))
            said, audio = "", 0
            end = time.monotonic() + 5
            while not ("Fake answer" in said and audio) and time.monotonic() < end:
                m = await ws.receive(timeout=5)
                if m.type == WSMsgType.BINARY:
                    audio += len(m.data)
                elif m.type == WSMsgType.TEXT:
                    ev = json.loads(m.data)
                    if ev["type"] == "transcript" and ev["who"] == "assistant":
                        said += ev["text"]
            assert "Fake answer" in said and "(phone withheld)" in said and audio > 0
            await ws.send_str(json.dumps({"type": "hangup"}))
            await ws.close()
            assert await until(lambda: not r.relay.live)
            assert r.relay.st.seconds_today() > 0
            for _ in range(8):
                await c.post("/api/ticket", headers={"X-Voice": "1", "Origin": o})
            assert (await c.post("/api/ticket", headers={"X-Voice": "1", "Origin": o})).status == 429
        finally:
            await c.close()
            await r.close()
    run(main())


def test_daily_minutes_cap(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path), max_minutes_per_day=1)
        r.relay.st.add_usage(45)
        c = await pub_client(r)
        try:
            await login(r, c)
            o = origin(c)
            t = (await (await c.post("/api/ticket", headers={"X-Voice": "1", "Origin": o})).json())["ticket"]
            resp = await c.get("/ws?ticket=" + t, headers={"Origin": o})
            assert resp.status == 429 and "daily limit" in await resp.text()
        finally:
            await c.close()
            await r.close()
    run(main())


def test_internal_api_auth_and_cli_reply(tmp_path, monkeypatch):
    async def main():
        r = await Rig.make(str(tmp_path))
        try:
            import aiohttp
            base = "http://127.0.0.1:%d" % r.iport
            async with aiohttp.ClientSession() as s:
                assert (await s.get(base + "/internal/status")).status == 401
                assert (await s.get(base + "/internal/status", headers={"Authorization": "Bearer wrong"})).status == 401
                ok = await s.get(base + "/internal/status", headers={"Authorization": "Bearer " + TOKEN})
                assert ok.status == 200 and (await ok.json())["live"] == 0
            monkeypatch.setenv("VOICE_INTERNAL_PORT", str(r.iport))
            monkeypatch.setenv("VOICE_LOCAL_TOKEN_FILE", r.cfg.local_token_file)
            loop = asyncio.get_running_loop()
            rc = await loop.run_in_executor(None, srv.main, ["reply", "v00000000", "nobody", "asked"])
            assert rc == 3                                       # unknown id → not delivered → Telegram
            monkeypatch.setenv("VOICE_INTERNAL_PORT", "9")
            assert await loop.run_in_executor(None, srv.main, ["reply", "v1", "x"]) == 2
        finally:
            await r.close()
    run(main())
