"""Hub mode: the gateway in claude-hub-phone reaches the brain only through the hub. A fake hub (hubd's
/phone/event and the gateway's /mcp) on loopback; the real gateway from the env fixture. No brain CLI runs here."""
import asyncio
import json
import os

import aiohttp
import pytest
from aiohttp import web

from phonegw import bridge, hubmode, m2


class FakeHub:
    def __init__(self, gw_internal, gw_token):
        self.events, self.calls, self.fetched, self.act_at = [], [], [], []
        self.down = False
        self.rate_limited, self.refused = 0, 0          # the next N phone_act calls answer rate_limited
        self.internal, self.gw_token = gw_internal, gw_token
        self.lookups = {("tripclock", "tz"): "Asia/Dubai\n",
                        ("sleepin", "status", "--json", "--days", "1"): json.dumps({"active": True, "until": "10:30",
                                                                                     "date": "2026-01-03"}),
                        ("remind", "list", "--json", "--mine"): json.dumps({"items": []}),
                        ("phone-produce", "board"): json.dumps({"rows": [1]}),
                        ("decision-deck", "--json"): json.dumps({"items": [{"id": "d1"}]})}  # fmt: skip

    async def event(self, request):
        if self.down:
            return web.json_response({"error": "down"}, status=503)
        assert request.headers["Authorization"] == "Bearer hub-token-x"
        self.events.append(await request.json())
        return web.json_response({"id": len(self.events)})

    async def mcp(self, request):
        body = await request.json()
        name, args = body["params"]["name"], body["params"]["arguments"]
        if name == "phone_act" and self.rate_limited:   # the brain gateway refuses before the tool runs
            self.rate_limited -= 1
            self.refused += 1
            return web.json_response({"jsonrpc": "2.0", "id": body["id"], "result": {
                "content": [{"type": "text", "text": "rate_limited: phone.act: over 60/min (hint: wait)"}],
                "isError": True}})  # fmt: skip
        self.calls.append((name, args))
        if name == "phone_act":
            self.act_at.append(asyncio.get_running_loop().time())
        data = {"rc": 0, "out": ""}
        if name == "phone_lookup":
            data["out"] = self.lookups.get(tuple(args["argv"]), "")
        elif args.get("file"):  # phone.act's @file: the brain fetches the clip from the gateway first
            async with aiohttp.ClientSession() as s:
                async with s.get(self.internal + "/internal/file", params={"id": args["file"]},
                                 headers={"Authorization": "Bearer " + self.gw_token}) as r:
                    self.fetched.append((r.status, await r.read()))
            data["out"] = json.dumps({"status": "match", "track": {"artist": "A", "title": "T"}})
        elif args["argv"][0] == "trip-pack":
            data["file"] = "<html>pack</html>"
        return web.json_response({"jsonrpc": "2.0", "id": body["id"],
                                  "result": {"content": [{"type": "text", "text": "x"}], "structuredContent": data,
                                             "isError": False}})  # fmt: skip


@pytest.fixture
async def hubbed(env, tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "run", bridge.run)          # install() patches the module: restored after the test
    monkeypatch.setattr(bridge, "run_tool", bridge.run_tool)
    monkeypatch.setattr(hubmode, "SHIP_EVERY", 3600.0)      # the tests drive the outbox by hand
    tok = tmp_path / "hub-token"
    tok.write_text("hub-token-x\n")
    internal = "http://127.0.0.1:%d" % env.gw.ports["internal"]
    fake = FakeHub(internal, env.gw.token)
    app = web.Application()
    app.add_routes([web.post("/phone/event", fake.event), web.post("/mcp", fake.mcp)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = "http://127.0.0.1:%d" % runner.addresses[0][1]
    hub = hubmode.install(env.gw, {"PHONEGW_HUB_URL": url, "PHONEGW_HUB_GW_URL": url, "PHONEGW_HUB_TOKEN_FILE": str(tok)})
    await asyncio.sleep(0.2)                                # the quiet cache's first refresh
    yield env, fake, hub
    await hub.close()
    await runner.cleanup()


async def test_taps_effects_chat_and_usage_become_events(hubbed):
    env, fake, _ = hubbed
    act = {"push_id": "p1", "nonce": "n1", "tag": "remind", "level": "timed", "verb": "done", "arg": "r:1a2b3c4d"}
    assert await bridge.run(act) == [("hub", 0)]
    assert await bridge.run_tool("routine_leaving", []) == ("hub", 0)
    assert await bridge.run_tool("rm -rf", []) == ("rm -rf", None)
    assert await env.gw.plugin_handoff("chat", "hi there", msg_id="m1", device_id="d1") is True
    env.gw.on_usage({"id": "u1", "body": {"app": "0.4.9", "items": [{"feature": "app.board", "kind": "view",
                                                                     "surface": "screen", "text": "never"}]}})
    await asyncio.sleep(0.2)
    env.gw.public_usage("connect", 12)
    await asyncio.sleep(0.2)
    kinds = [e["kind"] for e in fake.events]
    assert kinds == ["phone.tap", "phone.effect", "phone.chat", "phone.usage", "phone.usage"]
    assert fake.events[4]["payload"]["gw"] == {"feature": "phonegw.public", "outcome": "connect", "ms": 12}
    assert fake.events[0]["payload"]["action"]["arg"] == "r:1a2b3c4d"
    assert fake.events[2]["payload"] == {"what": "chat", "task": "hi there", "msg_id": "m1", "device_id": "d1"}
    assert fake.events[3]["payload"]["items"] == [{"feature": "app.board", "kind": "view", "surface": "screen"}]


async def test_notes_ship_and_a_hub_outage_keeps_events(hubbed):
    env, fake, hub = hubbed
    p = env.gw.note("selftest", "# Phone self-test: red checks\n")
    os.utime(p, (0, 0))
    fake.down = True
    assert await env.gw.plugin_handoff("card", "[card c1] x", device_id="d1") is True   # kept: it arrives later
    pend = [n for n in os.listdir(env.gw.paths.inbox) if n.startswith("pending-")]
    assert len(pend) == 1 and hub.kept == 1
    for n in pend:
        os.utime(os.path.join(env.gw.paths.inbox, n), (0, 0))
    assert await env.gw.hub_outbox.once() == 0
    fake.down = False
    assert await env.gw.hub_outbox.once() == 2
    assert sorted(e["kind"] for e in fake.events) == ["phone.chat", "phone.note"]
    assert os.listdir(env.gw.paths.inbox) == []


async def test_brain_reads_and_writes_go_through_the_gateway(hubbed, tmp_path):
    env, fake, _ = hubbed
    gw = env.gw
    assert str(gw.his_tz()) == "Asia/Dubai" and gw.sleepin_release(gw.his_tz()) is not None
    rc, out, _ = await gw.rem_run(["list", "--json", "--mine"])
    assert rc == 0 and json.loads(out) == {"items": []}
    rc, _, _ = await gw.rem_run(["done", "r:1a2b3c4d"])
    assert rc == 0 and fake.calls[-1] == ("phone_act", {"argv": ["remind", "done", "r:1a2b3c4d"], "timeout_s": 25.0})
    assert await gw.produce("board") == {"rows": [1]}
    assert await gw.deck_items() == [{"id": "d1"}]
    out_file = tmp_path / "pack.html.new"
    rc, _ = await gw.run_bin(["/x/trip-pack", "Trip", "--out", str(out_file)], 30)
    assert rc == 0 and out_file.read_text() == "<html>pack</html>"
    assert fake.calls[-1][1]["argv"] == ["trip-pack", "Trip", "--out", "@out"]
    assert await gw.health_ingest({"day": "2026-01-02", "steps": 1}) is True
    assert fake.calls[-1][1]["argv"][:3] == ["health", "ingest", "--received"] and "steps" in fake.calls[-1][1]["stdin"]
    clip = tmp_path / "clip.m4a"
    clip.write_bytes(b"a" * 4096)
    rc, out = await gw.trackid_run(str(clip), 30)
    assert rc == 0 and json.loads(out)["status"] == "match"
    assert fake.fetched == [(200, b"a" * 4096)]                     # the brain fetched the clip over /internal/file
    assert not [d for d in os.listdir(gw.shares_dir()) if d.startswith("tid-")]   # and it is gone again


async def test_a_burst_of_writes_is_paced_under_the_gateway_rate(hubbed, monkeypatch):
    """28.10 15:21: a reconnect fired 140 phone.act in 8 s; the gateway (60/min) refused 70 and they were lost."""
    env, fake, hub = hubbed
    monkeypatch.setattr(hubmode, "ACT_RATE", (5, 0.5))
    res = await asyncio.gather(*[env.gw.health_ingest({"day": "2026-01-02", "steps": i}) for i in range(12)])
    assert res == [True] * 12 and len(fake.act_at) == 12 and fake.refused == 0
    at = fake.act_at
    assert all(at[i + 5] - at[i] >= 0.45 for i in range(len(at) - 5))   # never more than 5 in any 0.5 s window
    assert hub.act_waits > 0


async def test_rate_limited_writes_are_retried_other_refusals_are_not(hubbed, monkeypatch):
    env, fake, hub = hubbed
    monkeypatch.setattr(hubmode, "ACT_BACKOFF", (0.05, 0.05))
    fake.rate_limited = 2
    rc, _, _ = await env.gw.rem_run(["done", "r:1a2b3c4d"])
    assert rc == 0 and fake.refused == 2 and hub.act_retries == 2
    assert fake.calls[-1] == ("phone_act", {"argv": ["remind", "done", "r:1a2b3c4d"], "timeout_s": 25.0})
    fake.rate_limited, n = 3, len(fake.calls)                             # still limited after the last pause: given up
    assert await env.gw.health_ingest({"day": "2026-01-02", "steps": 1}) is False
    assert fake.refused == 5 and len(fake.calls) == n


async def test_internal_file_serves_share_files_only(hubbed):
    env, _, _ = hubbed
    d = os.path.join(env.gw.shares_dir(), "s1")
    os.makedirs(d)
    with open(os.path.join(d, "01-a.pdf"), "wb") as fh:
        fh.write(b"%PDF")
    h = {"Authorization": "Bearer " + env.gw.token}
    base = "http://127.0.0.1:%d/internal/file" % env.gw.ports["internal"]
    async with aiohttp.ClientSession() as s:
        async with s.get(base, params={"id": "s1/01-a.pdf"}, headers=h) as r:
            assert r.status == 200 and await r.read() == b"%PDF"
        for bad in ("../x", "s1/../../phonegw.db", "s1", "..%2f/x", "s1/missing"):
            async with s.get(base, params={"id": bad}, headers=h) as r:
                assert r.status in (400, 404), bad
        async with s.get(base, params={"id": "s1/01-a.pdf"}) as r:
            assert r.status == 401


async def test_shares_and_zone_changes_become_events(hubbed):
    env, fake, _ = hubbed
    meta = {"share_id": "sh1", "kind": "text", "intent": "summary", "text": "ü" * 4000}
    assert await env.gw.on_share("d1", meta) == "new"
    assert await env.gw.on_share("d1", meta) == "dup"
    p = fake.events[-1]["payload"]
    assert fake.events[-1]["kind"] == "phone.share" and p["files"][0]["id"] == "sh1/00-text.txt"
    assert len(p["text"].encode()) <= 1500 and p["hint"] == m2.NOTE_HINT["summary"]  # a retired intent: plain share
    await env.gw.m2_event("d1", {"ts": 1, "body": {"kind": "zone", "zone": "home", "dir": "enter", "at": 1}})
    await asyncio.sleep(0.2)
    z = [e for e in fake.events if e["kind"] == "phone.zone"]
    assert len(z) == 1 and z[0]["payload"]["zone"] == "home" and "lat" not in json.dumps(z[0]["payload"])
