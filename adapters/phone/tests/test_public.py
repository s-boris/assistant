"""Internet path: PROXY v1 from the web VM (127.0.0.2 here), real-IP limits and bans, the CERT_REQUIRED public site,
no pairing and no open paths on it, the LAN path unchanged, `phonegw public on|off`, config push on a rev bump."""
import asyncio
import datetime as dt
import glob
import json
import os
import ssl
import subprocess

import aiohttp
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from conftest import PHONE_BIN, FakePhone
from phonegw import cli, pki
from phonegw import config as C
from phonegw.public import Guard, ProxyError, parse_proxy_v1, read_proxy

HOST = "app.test"
VM = "127.0.0.2"                 # the "web VM" (Linux answers on all of 127/8)
REAL = "203.0.113.7"             # documentation range: the phone's internet address
PUB = {"enabled": True, "host": HOST, "port": 8443, "proxy_ips": [VM], "tls_port": 0,
       "limits": {"new_per_ip": 12, "window_s": 60, "conc_per_ip": 6, "conc_total": 16, "handshake_s": 3},
       "ban": {"fails": 3, "window_s": 600, "ban_s": 3600, "trust_s": 86400}, "session_grace_s": 0.3}
ON = [{"public": PUB}]


class Relay:
    """Fake nginx stream server: accepts on 127.0.0.1:<port>, connects to the front from the VM address, sends the
    PROXY v1 line (unless header=False), then splices."""

    def __init__(self, front_port, real=REAL, header=True):
        self.front_port, self.real, self.header = front_port, real, header
        self.server = None

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        return self

    @property
    def base(self):
        return "https://127.0.0.1:%d" % self.server.sockets[0].getsockname()[1]

    async def handle(self, r, w):
        ur, uw = await asyncio.open_connection("127.0.0.1", self.front_port, local_addr=(VM, 0))
        if self.header:
            uw.write(("PROXY TCP4 %s 192.0.2.1 40000 8443\r\n" % self.real).encode())

        async def pump(a, b):
            try:
                while True:
                    d = await a.read(65536)
                    if not d:
                        break
                    b.write(d)
                    await b.drain()
            except Exception:
                pass
            finally:
                try:
                    b.close()
                except Exception:
                    pass
        await asyncio.gather(pump(r, uw), pump(ur, w))

    async def stop(self):
        self.server.close()


async def get(phone, base, path, cert=True, method="GET", ctx=None):
    async with aiohttp.ClientSession() as s:
        async with s.request(method, base + path, json={} if method == "POST" else None,
                             ssl=ctx or phone.ctx(cert), server_hostname=HOST) as r:
            return r.status


async def refused(coro):
    try:
        await asyncio.wait_for(coro, 8)
    except (aiohttp.ClientError, ssl.SSLError, ConnectionError, OSError):
        return True
    return False


async def settle():
    await asyncio.sleep(0.2)


async def is_closed(r):
    try:
        return await asyncio.wait_for(r.read(100), 5) == b""
    except ConnectionResetError:
        return True


# ---------------------------------------------------------------- PROXY parsing (unit)
def test_parse_proxy_v1():
    assert parse_proxy_v1(b"PROXY TCP4 203.0.113.7 192.0.2.1 40000 8443") == "203.0.113.7"
    assert parse_proxy_v1(b"PROXY TCP6 2001:db8::1 2001:db8::2 1 443") == "2001:db8::1"
    for bad in (b"PROXY UNKNOWN", b"PROXY TCP4 1.2.3 4.5.6.7 1 2", b"PROXY TCP4 2001:db8::1 1.2.3.4 1 2",
                b"PROXY TCP4 1.2.3.4 5.6.7.8 1 99999", b"PROXY TCP4 1.2.3.4 5.6.7.8 01 2", b"GET / HTTP/1.1",
                b"PROXY TCP4 1.2.3.4 5.6.7.8 1 2 extra", b"PROXY TCP4 \xff.2.3.4 5.6.7.8 1 2"):
        with pytest.raises(ProxyError):
            parse_proxy_v1(bad)


async def test_read_proxy_split_long_slow():
    r = asyncio.StreamReader()
    r.feed_data(b"7 192.0.2.1 40000 8443\r\n\x16\x03\x01")
    ip, rest = await read_proxy(r, b"PROXY TCP4 203.0.113.", 1)          # header split over two reads
    assert ip == REAL and rest == b"\x16\x03\x01"
    r = asyncio.StreamReader()
    r.feed_data(b"x" * 200)
    with pytest.raises(ProxyError, match="long"):
        await read_proxy(r, b"PROXY TCP4 ", 1)
    with pytest.raises(ProxyError, match="slow"):
        await read_proxy(asyncio.StreamReader(), b"PROXY TCP4 ", 0.2)


# ---------------------------------------------------------------- guard (unit)
def test_guard_limits_bans_and_trust():
    g = Guard(dict(C.DEFAULTS["public"], enabled=True, host=HOST, proxy_ips=[VM],
                   limits={"new_per_ip": 3, "window_s": 60, "conc_per_ip": 2, "conc_total": 3, "handshake_s": 10},
                   ban={"fails": 2, "window_s": 600, "ban_s": 3600, "trust_s": 86400}))
    assert g.is_proxy(VM) and not g.is_proxy("127.0.0.1")
    assert g.admit("a", 0) is None and g.admit("a", 1) is None
    assert g.admit("a", 2) == "limited"                    # 2 open per IP
    assert g.admit("b", 3) is None and g.admit("c", 4) == "limited"   # 3 open in total
    for ip in ("a", "a", "b"):
        g.release(ip)
    assert g.total == 0 and g.admit("a", 5) is None        # the 3rd new connection this minute
    g.release("a")
    assert g.admit("a", 6) == "limited"                    # the 4th: 3 new per minute per IP
    g.fail("x", 10)
    g.fail("x", 11)
    assert g.admit("x", 12) == "banned" and "x" in g.banned and g.stats["bans"] == 1
    g.opened(1, "me")
    g.mark(1, True)                                        # authenticated -> trusted: failures never ban it
    for t in range(5):
        g.fail("me")
    assert "me" not in g.banned


# ---------------------------------------------------------------- PKI
def test_public_cert_is_dns_only(tmp_path):
    from conftest import make_paths
    paths, _ = make_paths(tmp_path)
    pki.init_ca(paths)
    assert pki.issue_public(paths, HOST) is True
    dns, ips, days = pki.public_info(paths)
    assert dns == [HOST] and ips == [] and days >= 700
    assert pki.issue_public(paths, HOST) is False             # idempotent
    assert pki.issue_public(paths, "other.test") is True       # host changed -> reissued
    assert oct(os.stat(paths.public_key).st_mode & 0o777) == "0o600"


# ---------------------------------------------------------------- end to end through the front
@pytest.mark.parametrize("env", ON, indirect=True)
async def test_public_device_cert_health_ws_route_and_real_ip(env, phone):
    relay = await Relay(env.gw.ports["front"]).start()
    try:
        assert await get(phone, relay.base, "/v1/health") == 200
        await phone.close()
        phone.session = aiohttp.ClientSession()
        phone.ws = await phone.session.ws_connect(relay.base.replace("https", "wss") + "/v1/ws", ssl=phone.ctx(),
                                                  server_hostname=HOST)
        phone.reader = asyncio.ensure_future(phone._read())
        await phone.send("hello", {"app": "0.4.6", "code": 16, "sdk": 37, "model": "SM-X000Z", "caps": ["push"],
                                   "perms": {}, "battery_exempt": True})
        assert (await phone.recv("hello"))["body"]["device_id"] == phone.device_id
        assert (await phone.recv("config"))["body"]["urls"]
        assert REAL in env.gw.peers.values()                  # the real IP from PROXY is what the gateway sees (client_ip)
        st, s = await env.call("GET", "/internal/status")
        d = [x for x in s["devices"] if x["id"] == phone.device_id][0]
        assert d["route"] == "public" and d["online"]
        pub = s["front"]["public"]
        assert pub["enabled"] and pub["site"] and pub["counters"]["ok"] >= 2 and s["front"]["sources"]["proxy"] >= 2
        out = await asyncio.to_thread(subprocess.run, [PHONE_BIN, "status"], env=env.cli_env(), capture_output=True,
                                      text=True, timeout=20)
        assert "online via internet" in out.stdout and "internet path: on · live 1" in out.stdout
        await phone.close()
        await asyncio.sleep(0.6)                               # the session ends once the grace (0.3 s) has passed
        spool = glob.glob(os.path.join(env.gw.cfg["usage_state"], "spool", "app-*.jsonl"))
        evs = [json.loads(line) for f in spool for line in open(f)]
        assert [e.get("o") for e in evs if e.get("f") == "phonegw.public"] == ["connect", "disconnect"]
        assert not any(REAL in json.dumps(e) for e in evs)    # no IPs in analytics
    finally:
        await relay.stop()


async def ws_pub(phone, relay):
    """One more internet-route socket from the same device (the app reconnecting after a network change)."""
    s = aiohttp.ClientSession()
    ws = await s.ws_connect(relay.base.replace("https", "wss") + "/v1/ws", ssl=phone.ctx(), server_hostname=HOST)
    return s, ws


def pub_events(env):
    spool = sorted(glob.glob(os.path.join(env.gw.cfg["usage_state"], "spool", "app-*.jsonl")))
    return [e for e in (json.loads(line) for f in spool for line in open(f)) if e.get("f") == "phonegw.public"]


@pytest.mark.parametrize("env", [{"public": dict(PUB, session_grace_s=1.0)}], indirect=True)
async def test_public_usage_is_one_session_not_one_use_per_socket(env, phone):
    """Mobile handovers make the app open a new socket (the gateway drops the half-open old one), a socket dies and
    comes back seconds later, the app moves home and straight back at the edge of the Wi-Fi: all one session on the
    internet route = one connect and one disconnect (ms = the session's time on the route), not a use and a
    "quick retry" per socket. A socket after the grace starts a new session; stop() ends open sessions at once."""
    relay = await Relay(env.gw.ports["front"]).start()
    socks = []
    try:
        socks.append(await ws_pub(phone, relay))               # the session starts
        await settle()
        socks.append(await ws_pub(phone, relay))               # handover: replaces the first socket
        await settle()
        await socks[-1][1].close()                              # the socket dies ...
        await settle()
        socks.append(await ws_pub(phone, relay))               # ... and is back within the grace
        await settle()
        await phone.connect()                                   # moves home (LAN) ...
        await settle()
        socks.append(await ws_pub(phone, relay))               # ... and back to the internet route
        await settle()
        assert [e["o"] for e in pub_events(env)] == ["connect"]
        await socks[-1][1].close()
        await settle()
        assert [e["o"] for e in pub_events(env)] == ["connect"]  # not over before the grace has passed
        await asyncio.sleep(1.3)
        evs = pub_events(env)
        assert [e["o"] for e in evs] == ["connect", "disconnect"]
        assert "ms" not in evs[0] and evs[1]["ms"] >= 1000      # the whole session, not the last socket
        socks.append(await ws_pub(phone, relay))               # after the grace: a new session
        await settle()
        assert [e["o"] for e in pub_events(env)] == ["connect", "disconnect", "connect"]
        env.gw.cfg["public"]["session_grace_s"] = 60
        await socks[-1][1].close()
        await settle()
        assert [e["o"] for e in pub_events(env)] == ["connect", "disconnect", "connect"]
        env.gw.public_flush()                                   # what stop() does: no session left open
        assert [e["o"] for e in pub_events(env)] == ["connect", "disconnect", "connect", "disconnect"]
        env.gw.public_flush()
        assert len(pub_events(env)) == 4
    finally:
        for s, ws in socks:
            await ws.close()
            await s.close()
        await relay.stop()


@pytest.mark.parametrize("env", ON, indirect=True)
async def test_public_refuses_no_cert_and_foreign_ca(env, phone, tmp_path):
    relay = await Relay(env.gw.ports["front"], real="198.51.100.1").start()
    try:
        assert await refused(get(phone, relay.base, "/v1/health", cert=False))
        # a client cert from another CA
        ca_key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "evil")])
        key = ec.generate_private_key(ec.SECP256R1())
        now = dt.datetime.now(dt.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(1).not_valid_before(now - dt.timedelta(minutes=1)).not_valid_after(now + dt.timedelta(days=1))
                .sign(ca_key, hashes.SHA256()))
        (tmp_path / "e.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (tmp_path / "e.key").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                           serialization.NoEncryption()))
        ctx = ssl.create_default_context(cafile=env.paths.ca_pem)
        ctx.load_cert_chain(str(tmp_path / "e.pem"), str(tmp_path / "e.key"))
        assert await refused(get(phone, relay.base, "/v1/health", ctx=ctx))
        await settle()
        c = env.gw.guard.status()["counters"]
        assert c.get("refused", 0) + c.get("banned_hits", 0) >= 2 and not c.get("ok")    # aiohttp retries a refused GET once
    finally:
        await relay.stop()


@pytest.mark.parametrize("env", ON, indirect=True)
async def test_public_has_no_pairing_and_revoked_gets_403_everywhere(env, phone):
    relay = await Relay(env.gw.ports["front"]).start()
    try:
        assert await get(phone, relay.base, "/v1/pair", method="POST") == 404
        assert await refused(get(phone, relay.base, "/v1/pair", cert=False, method="POST"))
        st, _ = await env.call("POST", "/internal/revoke", {"device_id": phone.device_id})
        assert st == 200
        for path in ("/v1/health", "/v1/sync", "/v1/pair"):
            assert await get(phone, relay.base, path, method="POST" if path == "/v1/pair" else "GET") == 403
        assert (await phone.http("GET", "/v1/health", cert=False))[0] == 200     # LAN path unchanged
    finally:
        await relay.stop()


@pytest.mark.parametrize("env", ON, indirect=True)
async def test_proxy_header_from_other_source_closed(env):
    r, w = await asyncio.open_connection("127.0.0.1", env.gw.ports["front"])   # 127.0.0.1 is LAN-allowed, not a proxy
    w.write(b"PROXY TCP4 203.0.113.7 192.0.2.1 40000 8443\r\n\x16\x03\x01")
    assert await is_closed(r)
    w.close()
    assert env.gw.front.status()["sources"]["proxy_foreign"] == 1


@pytest.mark.parametrize("env", ON, indirect=True)
async def test_bare_tls_from_vm_closed_http_from_vm_reaches_novnc(env, phone):
    relay = await Relay(env.gw.ports["front"], header=False).start()
    try:
        assert await refused(get(phone, relay.base, "/v1/health"))         # TLS without PROXY: never the LAN path
        r, w = await asyncio.open_connection("127.0.0.1", env.gw.ports["front"], local_addr=(VM, 0))
        w.write(b"GET /vnc.html HTTP/1.0\r\nHost: browser\r\n\r\n")         # browser view behind nginx: as before
        data = await asyncio.wait_for(r.read(), 5)
        w.close()
        assert data.endswith(b"fake novnc")
        src = env.gw.front.status()["sources"]
        assert src["proxy_bare_tls"] >= 1 and src["proxy_http"] == 1
    finally:
        await relay.stop()


@pytest.mark.parametrize("env", ON, indirect=True)
async def test_plain_http_after_proxy_closed(env):
    r, w = await asyncio.open_connection("127.0.0.1", env.gw.ports["front"], local_addr=(VM, 0))
    w.write(b"PROXY TCP4 198.51.100.2 192.0.2.1 40000 8443\r\nGET /vnc.html HTTP/1.0\r\n\r\n")
    assert await is_closed(r)
    w.close()
    for bad in (b"PROXY TCP4 garbage\r\n\x16", b"PROXY " + b"9" * 120):
        r, w = await asyncio.open_connection("127.0.0.1", env.gw.ports["front"], local_addr=(VM, 0))
        w.write(bad)
        assert await is_closed(r)
        w.close()
    src = env.gw.front.status()["sources"]
    assert src["public_not_tls"] == 1 and src["proxy_bad"] == 2


@pytest.mark.parametrize("env", ON, indirect=True)
async def test_failures_ban_the_real_ip(env, phone):
    relay = await Relay(env.gw.ports["front"], real="198.51.100.9").start()
    try:
        for _ in range(3):                                     # ban.fails = 3
            assert await refused(get(phone, relay.base, "/v1/health", cert=False))
        await settle()
        assert await refused(get(phone, relay.base, "/v1/health"))         # even with a good cert: banned for 1 h
        st = env.gw.front.status()
        assert st["public"]["banned_now"] == 1 and st["sources"]["public_banned"] >= 1
        other = await Relay(env.gw.ports["front"], real="198.51.100.10").start()
        assert await get(phone, other.base, "/v1/health") == 200          # only that IP
        await other.stop()
    finally:
        await relay.stop()


@pytest.mark.parametrize("env", [{"public": dict(PUB, limits=dict(PUB["limits"], handshake_s=0.5))}], indirect=True)
async def test_stalled_handshake_is_cut(env):
    r, w = await asyncio.open_connection("127.0.0.1", env.gw.ports["front"], local_addr=(VM, 0))
    w.write(b"PROXY TCP4 198.51.100.3 192.0.2.1 40000 8443\r\n\x16\x03\x01")   # a ClientHello that never finishes
    assert await is_closed(r)
    w.close()
    await settle()
    assert env.gw.front.status()["sources"]["public_timeout"] == 1
    assert env.gw.guard.status()["counters"]["refused"] == 1


@pytest.mark.parametrize("env", [{"public": dict(PUB, enabled=False)}], indirect=True)
async def test_off_by_default_then_on_then_off(env, phone):
    assert "public" not in env.gw.ports
    relay = await Relay(env.gw.ports["front"]).start()
    try:
        assert await refused(get(phone, relay.base, "/v1/health"))
        assert env.gw.front.status()["sources"]["public_off"] == 1
        cfg = C.load(env.paths)
        cfg["public"]["enabled"] = True
        C.save(env.paths, cfg)
        await env.gw.reload()
        assert env.gw.ports.get("public")
        assert await get(phone, relay.base, "/v1/health") == 200
        await phone.close()
        phone.session = aiohttp.ClientSession()
        phone.ws = await phone.session.ws_connect(relay.base.replace("https", "wss") + "/v1/ws", ssl=phone.ctx(),
                                                  server_hostname=HOST)
        phone.reader = asyncio.ensure_future(phone._read())
        await phone.recv("hello")
        cfg["public"]["enabled"] = False
        C.save(env.paths, cfg)
        await env.gw.reload()
        assert "public" not in env.gw.ports
        with pytest.raises(ConnectionError):                   # the live public socket is closed at once
            while True:
                await phone.recv(timeout=5)
        assert await refused(get(phone, relay.base, "/v1/health"))
    finally:
        await relay.stop()


@pytest.mark.parametrize("env", [{"public": dict(PUB, enabled=False)}], indirect=True)
async def test_cli_public_on_off_and_config_push(env, phone, monkeypatch, capsys):
    for k, v in env.env.items():
        monkeypatch.setenv(k, v)
    await phone.connect()
    await phone.recv("config")
    rev = C.load(env.paths)["config_rev"]
    assert cli.main(["public", "on"]) == 0
    cfg = C.load(env.paths)
    assert cfg["public"]["enabled"] and cfg["urls"][-1] == "https://%s:8443" % HOST and cfg["config_rev"] == rev + 1
    assert cfg["urls"][0] == "https://127.0.0.1:6080"          # LAN first, internet last
    await env.gw.reload()                                       # what SIGHUP does
    body = (await phone.recv("config"))["body"]
    assert body["rev"] == rev + 1 and body["urls"][-1] == "https://%s:8443" % HOST
    assert cli.main(["public", "on"]) == 0 and C.load(env.paths)["config_rev"] == rev + 1     # idempotent
    assert cli.main(["public", "status"]) == 0
    assert "public: on" in capsys.readouterr().out
    assert cli.main(["public", "off", "--drop-url"]) == 0
    cfg = C.load(env.paths)
    assert not cfg["public"]["enabled"] and C.public_url(cfg) not in cfg["urls"] and cfg["config_rev"] == rev + 2
    disk = json.load(open(env.paths.config))
    assert disk["public"]["host"] == HOST                      # hand-set keys kept


async def test_cli_public_on_needs_host(env, monkeypatch, capsys):
    for k, v in env.env.items():
        monkeypatch.setenv(k, v)
    assert cli.main(["public", "on"]) == 2
    assert not C.load(env.paths)["public"]["enabled"]
