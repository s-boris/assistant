"""voicecheck plumbing: Gemini canary (fake REST + Live endpoints), the bridge's session ping, /internal/health."""
import asyncio
import json

import aiohttp
from aiohttp import web

from voice_relay import canary
from voice_relay.config import Config
from voice_relay.fakes import FakeCallClient, _serve
from voice_relay.tgcall import CallBridge


class FakeGoogle:
    """models.get + a Live socket. live: ok | billing (1011 close) | silent | wrongfirst."""
    def __init__(self, key="k-good", models=("gemini-live-x",), live="ok"):
        self.key, self.models, self.live = key, set(models), live
        self.setups, self.after_setup = [], []

    async def start(self):
        app = web.Application()
        app.router.add_get("/v1beta/models/{m}", self.model)
        app.router.add_get("/ws", self.ws)
        self.runner, self.port = await _serve(app)

    async def model(self, req):
        if req.headers.get("x-goog-api-key") != self.key:
            return web.json_response({"error": {"code": 400, "status": "INVALID_ARGUMENT",
                                                "details": [{"reason": "API_KEY_INVALID"}]}}, status=400)
        m = req.match_info["m"]
        if m not in self.models:
            return web.json_response({"error": {"code": 404, "status": "NOT_FOUND"}}, status=404)
        return web.json_response({"name": "models/" + m, "version": "v-1",
                                  "supportedGenerationMethods": ["bidiGenerateContent"]})

    async def ws(self, req):
        ws = web.WebSocketResponse()
        await ws.prepare(req)
        msg = await ws.receive()
        self.setups.append(json.loads(msg.data)["setup"])
        if self.live == "billing":
            await ws.close(code=1011, message=b"Your prepayment credits are depleted. Please go to AI Studio")
        elif self.live == "wrongfirst":
            await ws.send_str(json.dumps({"serverContent": {}}))
        elif self.live == "ok":
            await ws.send_bytes(json.dumps({"setupComplete": {}}).encode())
        if self.live != "silent":
            async for m in ws:
                self.after_setup.append(m.data)
        else:
            await asyncio.sleep(3)
        return ws


def run_canary(fg, key="k-good", model="gemini-live-x", live=True, timeout=None):
    async def main():
        await fg.start()
        try:
            cfg = Config(gemini_model=model, gemini_rest_url="http://127.0.0.1:%d/v1beta" % fg.port,
                         gemini_ws_url="ws://127.0.0.1:%d/ws" % fg.port)
            if timeout:
                async with aiohttp.ClientSession() as http:
                    return await canary.live_check(cfg, key, http, timeout=timeout)
            return await canary.run(cfg, live=live, key=key)
        finally:
            await fg.runner.cleanup()
    return asyncio.run(main())


def test_canary_ok_minimal_setup_and_no_turn():
    fg = FakeGoogle()
    r = run_canary(fg)
    assert (r["key"], r["listed"], r["version"], r["bidi"], r["live"]) == ("ok", True, "v-1", True, "ok")
    assert canary.ok(r) and "k-good" not in json.dumps(r)
    assert fg.setups == [{"model": "models/gemini-live-x", "generationConfig": {"responseModalities": ["AUDIO"]}}]
    assert fg.after_setup == []                                   # no audio, no text, no turn after the setup


def test_canary_billing_bad_key_missing_model_and_no_live():
    r = run_canary(FakeGoogle(live="billing"))
    assert r["live"] == "billing" and "1011" in r["detail"] and "prepayment" in r["detail"] and not canary.ok(r)
    r = run_canary(FakeGoogle(), key="k-bad")
    assert r["key"] == "invalid" and r["live"] == "skipped" and "API_KEY_INVALID" in r["detail"]
    assert "k-bad" not in json.dumps(r)
    r = run_canary(FakeGoogle(), model="gemini-gone")
    assert (r["key"], r["listed"], r["live"]) == ("ok", False, "skipped") and not canary.ok(r)
    fg = FakeGoogle(live="billing")
    r = run_canary(fg, live=False)
    assert r["live"] == "skipped" and canary.ok(r) and fg.setups == []
    assert canary.classify_close(1008, "models/x is not found for API version") == "model"
    assert canary.classify_close(1011, "Internal error") == "rejected"


def test_canary_timeout_unexpected_and_unreachable():
    assert run_canary(FakeGoogle(live="silent"), timeout=0.5)[0] == "timeout"
    assert run_canary(FakeGoogle(live="wrongfirst"), timeout=2)[0] == "rejected"
    cfg = Config(gemini_model="m", gemini_rest_url="http://127.0.0.1:9/v1beta", gemini_ws_url="ws://127.0.0.1:9/ws")
    r = asyncio.run(canary.run(cfg, key="k"))
    assert r["key"] == "error" and r["live"] == "skipped" and "REST" in r["detail"]
    assert canary.run is not None and asyncio.run(canary.run(Config(provider="openai"), key="k"))["live"] == "skipped"


def _bridge(client):
    return CallBridge(Config(), client, owner_id=777, handoff=None, instructions_fn=lambda *a: "",
                      provider_factory=None)


def test_bridge_ping_states():
    c = FakeCallClient(me_id=4242)
    assert asyncio.run(_bridge(c).ping()) == {"ok": True, "error": ""}
    c.ping_error = type("AuthKeyUnregistered", (Exception,), {})()
    assert asyncio.run(_bridge(c).ping()) == {"ok": False, "error": "AuthKeyUnregistered"}
    assert asyncio.run(_bridge(FakeCallClient(me_id=777)).ping())["error"] == "wrong account"
    assert asyncio.run(_bridge(FakeCallClient(is_bot=True)).ping())["error"] == "wrong account"

    class Slow(FakeCallClient):
        async def ping(self):
            await asyncio.sleep(5)
    assert asyncio.run(_bridge(Slow()).ping(timeout=0.2)) == {"ok": False, "error": "timeout"}


def test_internal_health_endpoint(tmp_path):
    from util import TOKEN, Rig

    async def get(r, token=TOKEN):
        async with aiohttp.ClientSession() as s:
            async with s.get("http://127.0.0.1:%d/internal/health" % r.iport,
                             headers={"Authorization": "Bearer " + token}) as resp:
                return resp.status, (await resp.json())

    async def main():
        r = await Rig.make(str(tmp_path))
        try:
            assert (await get(r, "wrong"))[0] == 401
            st, body = await get(r)                                    # no bridge yet
            assert st == 200 and body["tg"] == {"ok": False, "error": "bridge off"}
            c = FakeCallClient(me_id=4242)
            r.relay._bridge, r.relay._tg_state = _bridge(c), "on"
            st, body = await get(r)
            assert body == {"tg": {"ok": True, "error": ""}, "tg_calls": "on"}
            c.ping_error = ConnectionError("gone")
            assert (await get(r))[1]["tg"] == {"ok": False, "error": "ConnectionError"}
            for _ in range(4):
                await get(r)
            assert (await get(r))[0] == 429                           # rate-limited (6/min)
        finally:
            await r.close()
    asyncio.run(main())
