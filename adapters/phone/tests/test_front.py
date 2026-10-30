"""Front routing: TLS -> gateway, HTTP -> (fake) websockify, disallowed source -> closed, limits."""
import asyncio

import aiohttp
import pytest

from conftest import FakePhone
from phonegw.front import Front


async def closed(r):
    try:
        return await asyncio.wait_for(r.read(100), 3) == b""
    except ConnectionResetError:
        return True


async def test_tls_through_front_pairs_and_connects(env):
    p = FakePhone(env)
    st, _ = await p.pair(env.code(), base=env.front)
    assert st == 200
    st, j = await p.http("GET", "/v1/health", base=env.front, cert=False)
    assert st == 200 and j == {"ok": True, "api": 1}
    await p.connect(base=env.front)
    assert (await p.recv("hello"))["body"]["device_id"] == p.device_id
    await p.close()
    assert env.gw.front.status()["sources"]["tls"] >= 2


async def test_http_through_front_reaches_novnc(env):
    url = "http://127.0.0.1:%d/vnc.html" % env.gw.ports["front"]
    async with aiohttp.ClientSession() as s:
        async with s.get(url) as r:
            assert r.status == 200 and await r.text() == "fake novnc"
    assert env.gw.front.status()["sources"]["http"] >= 1


async def test_http_closed_when_view_off(env):
    env.gw.front.cfg = dict(env.gw.front.cfg, novnc_port=1)   # nothing listens on port 1
    r, w = await asyncio.open_connection("127.0.0.1", env.gw.ports["front"])
    w.write(b"GET /vnc.html HTTP/1.1\r\nHost: x\r\n\r\n")
    data = await asyncio.wait_for(r.read(100), 3)
    assert data == b""
    w.close()


async def test_peer_ip_is_passed_to_the_gateway(env):
    seen = []
    orig = env.gw.client_ip

    def spy(req):
        ip = orig(req)
        seen.append(ip)
        return ip
    env.gw.client_ip = spy
    await FakePhone(env).pair(env.code(), base=env.front)
    assert seen == ["127.0.0.1"]


@pytest.mark.parametrize("env", [{"allow": {"lan": ".".join(("10", "0", "0", "0")) + "/8"}}], indirect=True)
async def test_disallowed_source_closed(env):
    r, w = await asyncio.open_connection("127.0.0.1", env.gw.ports["front"])
    w.write(b"\x16\x03\x01")
    assert await closed(r)
    w.close()
    st = env.gw.front.status()
    assert st["sources"]["denied"] == 1 and st["denied_private"] == {"127.0.0.0/24": 1}   # private /24s are counted (to spot the WG subnet)


async def test_per_ip_rate_limit(env):
    f = Front(dict(env.gw.cfg, front_port=0, front_limits=dict(env.gw.cfg["front_limits"], per_ip=2)), {})
    await f.start()
    try:
        results = []
        for _ in range(3):
            r, w = await asyncio.open_connection("127.0.0.1", f.port)
            w.write(b"GET /vnc.html HTTP/1.0\r\n\r\n")
            try:
                results.append(await asyncio.wait_for(r.read(), 3))
            except ConnectionResetError:
                results.append(b"")
            w.close()
        assert results[0] and results[1] and results[2] == b""
        assert f.status()["sources"]["limited"] == 1
    finally:
        await f.stop()


async def test_front_off_and_on(env):
    port = env.gw.ports["front"]
    open(env.paths.front_off, "w").close()
    await env.gw.reload()
    assert env.gw.front is None
    import os
    os.unlink(env.paths.front_off)
    env.gw.cfg["front_port"] = 0
    await env.gw.front_on()
    assert env.gw.front is not None and port
