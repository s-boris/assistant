"""Front on :6080: source allowlist, then sniff the first byte. 0x16 (TLS ClientHello) -> phonegw TLS (127.0.0.1:tls_port);
anything else -> websockify (127.0.0.1:novnc_port) when browser view is on, else close.
The web VM (public.proxy_ips) gets its own path: plain HTTP -> noVNC (browser view behind nginx); a PROXY v1
header followed by TLS -> the public TLS site (CERT_REQUIRED) when public is on, with the real client IP for limits and
bans (public.py); TLS without a PROXY header -> closed, so nothing from the VM ever reaches the LAN path (pairing).
A PROXY header from any other source is closed."""
import asyncio
import collections
import ipaddress
import logging
import time

from .config import allow_networks
from .public import SIG, ProxyError, read_proxy
from .util import ip_of

log = logging.getLogger("phonegw.front")


class Front:
    def __init__(self, cfg, peers, guard=None, ports=None):
        self.cfg = cfg
        self.lim = cfg["front_limits"]
        self.nets = allow_networks(cfg)
        self.peers = peers              # local port of our upstream TLS socket -> real client ip (for the gateway)
        self.guard = guard              # public.Guard (None: no public path at all)
        self.ports = ports if ports is not None else {}   # the gateway's ports; "public" while the public site runs
        self.public_conns = set()       # client writers of live public connections (closed by drop_public)
        self.server = None
        self.active = 0
        self.recent = collections.defaultdict(collections.deque)
        self.stats = collections.Counter()      # per allow rule name, plus denied/limited/tls/http/novnc_off/proxy_*/public_*
        self.denied_private = collections.Counter()   # private /24s that were refused (to confirm the WG subnet)
        self.started = None

    def rule(self, ip):
        for name, net in self.nets:
            if ip.version == net.version and ip in net:
                return name
        return None

    async def start(self):
        self.server = await asyncio.start_server(self.handle, self.cfg["front_bind"], int(self.cfg["front_port"]),
                                                 reuse_address=True, backlog=128)
        self.started = time.time()
        log.info("front listening on port %s", self.cfg["front_port"])

    @property
    def port(self):
        return self.server.sockets[0].getsockname()[1] if self.server and self.server.sockets else None

    async def stop(self):
        self.drop_public()
        if self.server:
            self.server.close()
            try:
                await asyncio.wait_for(self.server.wait_closed(), 2)
            except (asyncio.TimeoutError, Exception):
                pass
            self.server = None

    def drop_public(self):
        """Close every live public connection (public off: the app falls back to LAN/WG on its next connect)."""
        for w in list(self.public_conns):
            try:
                w.close()
            except Exception:
                pass
        n = len(self.public_conns)
        self.public_conns.clear()
        return n

    def _limited(self, key):
        now = time.monotonic()
        q = self.recent[key]
        while q and q[0] < now - self.lim["window_s"]:
            q.popleft()
        if len(q) >= self.lim["per_ip"]:
            return True
        q.append(now)
        if len(self.recent) > 4096:
            for k in [k for k, v in self.recent.items() if not v][:2048]:
                self.recent.pop(k, None)
        return False

    async def _read(self, reader, n=65536):
        try:
            return await asyncio.wait_for(reader.read(n), self.lim["sniff_timeout_s"])
        except (asyncio.TimeoutError, ConnectionError):
            return b""

    async def handle(self, reader, writer):
        peer = writer.get_extra_info("peername") or ("0.0.0.0", 0)
        ip = ip_of(peer[0])
        if ip and self.guard and self.guard.is_proxy(ip):
            return await self.handle_proxy(reader, writer, ip)
        name = self.rule(ip) if ip else None
        if not name:
            self.stats["denied"] += 1
            if ip and ip.is_private:
                self.denied_private[str(ipaddress.ip_network(f"{ip}/24", strict=False))] += 1
            writer.close()
            return
        if self.active >= self.lim["max_conn"] or self._limited(str(ip)):
            self.stats["limited"] += 1
            writer.close()
            return
        self.stats[name] += 1
        self.active += 1
        try:
            first = await self._read(reader)
            if not first:
                return
            if first.startswith(SIG):           # PROXY from a source that is not the web VM: never trusted
                self.stats["proxy_foreign"] += 1
                return
            await self._upstream(reader, writer, first, str(ip))
        except Exception as e:  # never let one connection take the front down
            log.debug("front connection error: %s", type(e).__name__)
        finally:
            self.active -= 1
            self._close(writer)

    async def handle_proxy(self, reader, writer, ip):
        """A connection from the web VM: plain HTTP (browser view), or PROXY + TLS (public path); bare TLS is closed."""
        if self.active >= self.lim["max_conn"]:
            self.stats["limited"] += 1
            writer.close()
            return
        self.active += 1
        try:
            first = await self._read(reader)
            while first and len(first) < len(SIG) and SIG.startswith(first):    # a split 'PROXY ' prefix
                more = await self._read(reader)
                if not more:
                    return
                first += more
            if not first:
                return
            if first.startswith(SIG):
                self.stats["proxy"] += 1
                await self._public(reader, writer, first)
            elif first[0] == 0x16:
                self.stats["proxy_bare_tls"] += 1      # TLS from the VM without PROXY: the LAN path stays LAN-only
            elif self._limited(str(ip)):
                self.stats["limited"] += 1
            else:
                self.stats["proxy_http"] += 1
                await self._upstream(reader, writer, first, str(ip))
        except Exception as e:
            log.debug("front proxy connection error: %s", type(e).__name__)
        finally:
            self.active -= 1
            self._close(writer)

    async def _upstream(self, reader, writer, first, ip):
        """The LAN path: TLS -> the app site, anything else -> noVNC."""
        tls = first[0] == 0x16
        target = int(self.cfg["tls_port"] if tls else self.cfg["novnc_port"])
        self.stats["tls" if tls else "http"] += 1
        uw = port = None
        try:
            try:
                host = "127.0.0.1" if tls else str(self.cfg.get("novnc_host") or "127.0.0.1")   # hub: the brain's noVNC
                ur, uw = await asyncio.wait_for(asyncio.open_connection(host, target), 5)
            except (OSError, asyncio.TimeoutError):
                if not tls:
                    self.stats["novnc_off"] += 1
                return
            if tls:
                port = uw.get_extra_info("sockname")[1]
                self.peers[port] = ip
            uw.write(first)
            await self._splice(reader, writer, ur, uw, self.lim["idle_tls_s"] if tls else self.lim["idle_http_s"])
        finally:
            if port is not None:
                self.peers.pop(port, None)
            self._close(uw)

    async def _public(self, reader, writer, first):
        g = self.guard
        try:
            real, rest = await read_proxy(reader, first, float(g.cfg.get("proxy_timeout_s", 5)))
        except ProxyError as e:
            self.stats["proxy_bad"] += 1
            log.debug("public: bad PROXY header (%s)", e.args[0])
            return
        pub_port = self.ports.get("public")
        if not g.enabled or not pub_port:
            self.stats["public_off"] += 1
            return
        if not rest:
            rest = await self._read(reader)
            if not rest:
                return
        if rest[0] != 0x16:                       # only TLS after PROXY (noVNC is never reachable from the internet)
            self.stats["public_not_tls"] += 1
            g.fail(real)
            return
        why = g.admit(real)
        if why:
            self.stats["public_" + why] += 1
            return
        uw = port = None
        try:
            try:
                ur, uw = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", int(pub_port)), 5)
            except (OSError, asyncio.TimeoutError):
                self.stats["public_down"] += 1
                g.release(real)
                return
            port = uw.get_extra_info("sockname")[1]
            self.peers[port] = real
            g.opened(port, real)
            self.public_conns.add(writer)
            uw.write(rest)
            splice = asyncio.ensure_future(self._splice(reader, writer, ur, uw, self.lim["idle_tls_s"]))
            try:
                await asyncio.wait_for(asyncio.shield(splice), float(g.lim["handshake_s"]))
            except asyncio.TimeoutError:
                if g.state(port) is None:          # no authenticated request in time: cut it
                    self.stats["public_timeout"] += 1
                    splice.cancel()
                    await asyncio.gather(splice, return_exceptions=True)
                else:
                    await splice
            finally:
                if not splice.done():
                    splice.cancel()
                    await asyncio.gather(splice, return_exceptions=True)
        finally:
            if port is not None:
                g.closed(port, real)
                self.peers.pop(port, None)
            self.public_conns.discard(writer)
            self._close(uw)

    @staticmethod
    def _close(w):
        if w is not None:
            try:
                w.close()
            except Exception:
                pass

    async def _splice(self, r1, w1, r2, w2, idle):
        async def pump(r, w):
            while True:
                data = await asyncio.wait_for(r.read(65536), idle)
                if not data:
                    return
                w.write(data)
                await w.drain()

        t1 = asyncio.ensure_future(pump(r1, w2))
        t2 = asyncio.ensure_future(pump(r2, w1))
        try:
            await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (t1, t2):
                t.cancel()
            await asyncio.gather(t1, t2, return_exceptions=True)

    def status(self):
        st = {"on": self.server is not None, "port": self.port, "active": self.active, "sources": dict(self.stats),
              "denied_private": dict(self.denied_private)}
        if self.guard is not None:
            st["public"] = dict(self.guard.status(), live=len(self.public_conns), site=bool(self.ports.get("public")))
        return st
