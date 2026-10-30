"""Pairing (code lifecycle, brute force, rate limit, key types) and mTLS per route."""
import base64
import ssl

import aiohttp
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from conftest import FakePhone


async def test_health_needs_no_cert(env):
    p = FakePhone(env)
    st, j = await p.http("GET", "/v1/health", cert=False)
    assert (st, j) == (200, {"ok": True, "api": 1})


async def test_pair_ok_and_cert_works(env):
    p = FakePhone(env)
    st, j = await p.pair(env.code(name="S25"))
    assert st == 200 and j["device_id"].startswith("d_") and j["cert"].startswith("-----BEGIN CERTIFICATE-----")
    assert j["config"]["keepalive_s"] == 240 and j["ca"] == open(env.paths.ca_pem).read()
    assert env.gw.proto.check_http("pair.response", j)
    st, j = await p.http("GET", "/v1/sync")
    assert st == 200 and j["messages"] == []
    row = env.gw.db.device(p.device_id)
    assert row["status"] == "active" and row["name"] == "S25"


async def test_routes_need_a_device_cert(env):
    p = FakePhone(env)
    for path in ("/v1/sync", "/v1/ws"):
        st, j = await p.http("GET", path, cert=False)
        assert st == 401 and j["error"] == "cert"
    st, _ = await p.http("POST", "/v1/events", {"items": []}, cert=False)
    assert st == 401


async def test_foreign_cert_fails_handshake(env, tmp_path):
    """A client cert from another CA never gets through the TLS handshake."""
    from phonegw import config as C, pki
    other = C.Paths({"PHONEGW_SECRETS": str(tmp_path / "other"), "PHONEGW_STATE": str(tmp_path / "ost"),
                     "PHONEGW_CONFIG": str(tmp_path / "oc.json")})
    other.ensure_dirs()
    pki.init_ca(other)
    p = FakePhone(env)
    p.cert, fp = pki.issue_device(other, p.pub, "d_EVIL01")
    with pytest.raises((aiohttp.ClientError, ssl.SSLError, ConnectionError)):
        async with aiohttp.ClientSession() as s:
            async with s.get(env.tls + "/v1/sync", ssl=p.ctx()) as r:
                await r.read()
                assert r.status != 200


async def test_code_single_use(env, phone):
    code = env.code()
    st, _ = await FakePhone(env).pair(code)
    assert st == 200
    st, j = await FakePhone(env).pair(code)
    assert st == 403 and j["error"] == "code"


async def test_code_expired(env):
    code = env.code(ttl=-1)
    st, j = await FakePhone(env).pair(code)
    assert st == 403


async def test_code_normalised(env):
    code = env.code()
    st, _ = await FakePhone(env).pair(code.lower().replace("-", " "))
    assert st == 200


async def test_brute_force_kills_the_code(env):
    code = env.code()
    for _ in range(5):
        st, _ = await FakePhone(env).pair("0000-0000" if code != "0000-0000" else "1111-1111")
        assert st == 403
    st, _ = await FakePhone(env).pair(code)
    assert st == 403, "5 wrong tries must kill a live code"


@pytest.mark.parametrize("env", [{"pair": {"code_ttl_s": 600, "max_attempts": 50, "per_ip_per_min": 3}}], indirect=True)
async def test_pair_rate_limit(env):
    sts = [(await FakePhone(env).pair("ZZZZ-ZZZZ"))[0] for _ in range(4)]
    assert sts == [403, 403, 403, 429]


async def test_pair_rejects_rsa_and_garbage(env):
    rsa_pub = base64.b64encode(rsa.generate_private_key(65537, 2048).public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode()
    st, j = await FakePhone(env).pair(env.code(), pub=rsa_pub)
    assert st == 422 and j["error"] == "key_type"
    st, j = await FakePhone(env).pair(env.code(), pub="A" * 60)
    assert st == 422
    st, j = await FakePhone(env).pair(env.code(), lat=47.6)
    assert st == 422 and j["error"] == "schema"


async def test_revoke_closes_and_refuses(env, phone):
    await phone.connect()
    await phone.recv("hello")
    st, j = await env.call("POST", "/internal/revoke", {"device_id": phone.device_id})
    assert st == 200 and j["closed"]
    with pytest.raises(ConnectionError):
        await phone.recv("never", timeout=3)
    st, j = await phone.http("GET", "/v1/sync")
    assert st == 403 and j["error"] == "device"


async def test_internal_api_needs_the_token(env):
    st, j = await env.call("GET", "/internal/status", token="wrong")
    assert st == 401
