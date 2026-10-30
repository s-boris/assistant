"""Fixtures: a gateway on free loopback ports with temp state/certs, a fake noVNC, and a fake phone (aiohttp client with a
test device cert from the real pairing flow). Offline, loopback only."""
import asyncio
import base64
import hashlib
import json
import os
import ssl
import sys

import aiohttp
import pytest
from aiohttp import web
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from phonegw import config as C, pki  # noqa: E402
from phonegw.server import Gateway  # noqa: E402
from phonegw.util import gen_code, normalize_code, now_s, ulid, write_private  # noqa: E402

PROTOCOL = os.environ.get("PHONEGW_PROTOCOL") or os.path.expanduser("~/opt/phone-app/claude/protocol")
PHONE_BIN = os.environ.get("PHONE_BIN") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "phone")
# The home widget producer must never reach the real HA / cat tools from a test (tests set their own fakes).
for _k in ("HA_BIN", "CAT_CORNER_BIN", "CAT_SUPPLIES_BIN", "HEALTH_BIN"):     # never the live health store
    os.environ.setdefault(_k, "/bin/false")


def make_paths(tmp):
    env = {"PHONEGW_STATE": str(tmp / "state"), "PHONEGW_SECRETS": str(tmp / "secrets"),
           "PHONEGW_CONFIG": str(tmp / "config.json"), "PHONEGW_INBOX": str(tmp / "inbox"), "PHONEGW_PROTOCOL": PROTOCOL}
    p = C.Paths(env)
    p.ensure_dirs()
    os.makedirs(p.inbox, exist_ok=True)
    return p, env


class Env:
    def __init__(self, gw, paths, env, tmp, novnc_port):
        self.gw, self.paths, self.env, self.tmp, self.novnc_port = gw, paths, env, tmp, novnc_port
        self.token = open(paths.token).read().strip()

    @property
    def tls(self):
        return "https://127.0.0.1:%d" % self.gw.ports["tls"]

    @property
    def front(self):
        return "https://127.0.0.1:%d" % self.gw.ports["front"]

    @property
    def internal(self):
        return "http://127.0.0.1:%d" % self.gw.ports["internal"]

    def code(self, ttl=600, name=None):
        c = gen_code()
        self.gw.db.x("INSERT INTO pair_codes(code_sha256, expires, attempts, used, name) VALUES(?,?,0,0,?)",
                     hashlib.sha256(normalize_code(c).encode()).hexdigest(), now_s() + ttl, name)
        return c

    async def call(self, method, path, body=None, token=None):
        async with aiohttp.ClientSession() as s:
            async with s.request(method, self.internal + path, json=body,
                                 headers={"Authorization": "Bearer " + (token or self.token)}) as r:
                return r.status, await r.json()

    def notes(self, kind):
        return sorted(f for f in os.listdir(self.paths.inbox) if f.startswith("phone-%s-" % kind))

    def cli_env(self):
        e = dict(os.environ)
        e.update({"PHONEGW_URL": self.internal, "PHONEGW_TOKEN_FILE": self.paths.token})
        return e


async def start_novnc():
    app = web.Application()
    app.router.add_get("/vnc.html", lambda r: web.Response(text="fake novnc"))
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, runner.addresses[0][1]


@pytest.fixture
async def env(tmp_path, request):
    paths, envd = make_paths(tmp_path)
    novnc, nport = await start_novnc()
    cfg = C.load(paths)
    cfg.update({"tls_port": 0, "internal_port": 0, "front_port": 0, "front_bind": "127.0.0.1", "novnc_port": nport,
                "server_ips": ["127.0.0.1"], "urls": ["https://127.0.0.1:6080"], "tz": "Europe/Zurich",
                "allow": {"loopback": "127.0.0.0/8"}, "bridge": False, "widgets_auto": False,
                "quiet_hold": False,
                "share_min_free": 0,     # host free disk never decides a test (20 GiB default -> 507 on a small /tmp)
                "usage_state": str(tmp_path / "product")})   # tests never reach my real tools (test_quiet.py turns the hold on with fakes)
    over = getattr(request, "param", None) or {}
    cfg.update(over)
    C.save(paths, cfg)
    pki.init_ca(paths)
    pki.issue_server(paths, ["127.0.0.1"], ["phonegw.home.arpa"])
    write_private(paths.token, "test-token-" + ulid() + "\n")
    gw = Gateway(paths, C.load(paths))
    await gw.start(front=True)
    e = Env(gw, paths, envd, tmp_path, nport)
    yield e
    await gw.stop()
    await novnc.cleanup()


class FakePhone:
    """The app, minus Android: EC P-256 key, pairing, mTLS WebSocket, auto-ack of frames that ask for it."""

    def __init__(self, env, name="S25"):
        self.env = env
        self.name = name
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.pub = base64.b64encode(self.key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode()
        self.cert = None
        self.device_id = None
        self.ws = None
        self.session = None
        self.frames = asyncio.Queue()
        self.auto_ack = True
        self.reader = None
        self.received = []

    def ctx(self, cert=True):
        c = ssl.create_default_context(cafile=self.env.paths.ca_pem)
        if cert and self.cert:
            d = self.env.tmp / ("phone-" + self.name)
            d.mkdir(exist_ok=True)
            (d / "cert.pem").write_text(self.cert)
            (d / "key.pem").write_bytes(self.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                               serialization.NoEncryption()))
            c.load_cert_chain(str(d / "cert.pem"), str(d / "key.pem"))
        return c

    async def pair(self, code, base=None, **extra):
        body = {"code": code, "pub": self.pub, "bio_pub": self.pub, "name": self.name, "model": "SM-X000Z", "sdk": 37,
                "app_code": 1}
        body.update(extra)
        async with aiohttp.ClientSession() as s:
            async with s.post((base or self.env.tls) + "/v1/pair", json=body, ssl=self.ctx(cert=False)) as r:
                j = await r.json()
                if r.status == 200:
                    self.cert, self.device_id = j["cert"], j["device_id"]
                return r.status, j

    async def http(self, method, path, body=None, base=None, cert=True):
        async with aiohttp.ClientSession() as s:
            async with s.request(method, (base or self.env.tls) + path, json=body, ssl=self.ctx(cert)) as r:
                try:
                    return r.status, await r.json()
                except Exception:
                    return r.status, None

    async def connect(self, base=None, hello=True, caps=None):
        self.session = aiohttp.ClientSession()
        self.ws = await self.session.ws_connect((base or self.env.tls).replace("https", "wss") + "/v1/ws",
                                                ssl=self.ctx(), autoping=True, heartbeat=None)
        self.reader = asyncio.ensure_future(self._read())
        if hello:
            await self.send("hello", {"app": "0.1.0", "code": 1, "sdk": 37, "model": "SM-X000Z", "caps": caps or ["push", "chat"],
                                      "perms": {"notifications": True}, "battery_exempt": True})
        return self

    async def _read(self):
        async for msg in self.ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            env = json.loads(msg.data)
            self.received.append(env)
            if env.get("ack") and self.auto_ack:
                await self.send("ack", {"ids": [env["id"]]})
            await self.frames.put(env)
        await self.frames.put(None)

    async def recv(self, t=None, timeout=5):
        while True:
            env = await asyncio.wait_for(self.frames.get(), timeout)
            if env is None:
                raise ConnectionError("closed")
            if t is None or env["t"] == t:
                return env

    async def send(self, t, body, ack=False, msg_id=None, raw=None):
        env = raw or {"v": 1, "id": msg_id or ulid(), "t": t, "ts": now_s(), "ack": ack, "body": body}
        await self.ws.send_str(json.dumps(env))
        return env

    async def close(self):
        if self.ws is not None:
            await self.ws.close()
        if self.reader:
            try:
                await asyncio.wait_for(self.reader, 2)
            except Exception:
                pass
        if self.session:
            await self.session.close()
        await asyncio.sleep(0.05)


@pytest.fixture
async def phone(env):
    p = FakePhone(env)
    st, _ = await p.pair(env.code())
    assert st == 200
    yield p
    await p.close()
