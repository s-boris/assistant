"""Internet path: nginx on the web VM passes <public.host>:<public.port> through as raw TLS with a PROXY v1 header in front.
The front accepts PROXY only from public.proxy_ips, reads the real client IP, applies per-IP limits and bans (this guard),
then splices to the public TLS site (127.0.0.1:public.tls_port: TLS 1.3, CERT_REQUIRED, DNS-only cert from the phonegw CA).
The gateway's auth middleware marks each public connection ok (active device) or denied (unknown/revoked: 403); a
connection that ends or reaches handshake_s without either counts as a failed handshake. Off unless public.enabled."""
import asyncio
import collections
import ipaddress
import logging
import time

log = logging.getLogger("phonegw.public")

MAX_PROXY = 107                    # PROXY v1 line incl. CRLF (spec maximum)
SIG = b"PROXY "


class ProxyError(Exception):
    pass


def parse_proxy_v1(line):
    """b'PROXY TCP4 src dst sport dport' (CRLF stripped) -> the source IP as str. Anything else -> ProxyError."""
    try:
        s = line.decode("ascii")
    except UnicodeDecodeError:
        raise ProxyError("ascii")
    parts = s.split(" ")
    if len(parts) != 6 or parts[0] != "PROXY" or parts[1] not in ("TCP4", "TCP6"):
        raise ProxyError("format")
    try:
        src, dst = ipaddress.ip_address(parts[2]), ipaddress.ip_address(parts[3])
    except ValueError:
        raise ProxyError("ip")
    if src.version != dst.version or src.version != (4 if parts[1] == "TCP4" else 6):
        raise ProxyError("family")
    for p in parts[4:]:
        if not p.isdigit() or int(p) > 65535 or (len(p) > 1 and p[0] == "0"):
            raise ProxyError("port")
    return str(src)


async def read_proxy(reader, buf, timeout):
    """buf = bytes already read (starts with 'PROXY '). -> (real ip, rest after the CRLF). Reads more only until the
    CRLF, never past MAX_PROXY bytes of header, within `timeout` seconds in total. ProxyError on anything off."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while b"\r\n" not in buf[:MAX_PROXY]:
        if len(buf) >= MAX_PROXY:
            raise ProxyError("long")
        left = deadline - loop.time()
        if left <= 0:
            raise ProxyError("slow")
        try:
            chunk = await asyncio.wait_for(reader.read(MAX_PROXY - len(buf)), left)
        except asyncio.TimeoutError:
            raise ProxyError("slow")
        if not chunk:
            raise ProxyError("eof")
        buf += chunk
    i = buf.index(b"\r\n")
    return parse_proxy_v1(buf[:i]), buf[i + 2:]


class Guard:
    """Per-real-IP admission, failure counting and bans for the public path, plus its counters (phone status)."""

    def __init__(self, pcfg):
        self.recent = collections.defaultdict(collections.deque)   # ip -> connect times (monotonic)
        self.conc = collections.Counter()                           # ip -> open public connections
        self.total = 0
        self.fails = collections.defaultdict(collections.deque)    # ip -> failure times
        self.banned = {}                                            # ip -> banned until (monotonic)
        self.trusted = {}                                           # ip -> until: authenticated recently, never banned
        self.conns = {}                                             # upstream port -> {"ip", "state": None|ok|denied}
        self.stats = collections.Counter()
        self.day = time.strftime("%Y-%m-%d")
        self.today = collections.Counter()                          # refused / bans today (spike notes)
        self.update(pcfg)

    def update(self, pcfg):
        self.cfg = pcfg
        self.lim = pcfg["limits"]
        self.ban_cfg = pcfg["ban"]
        self.proxies = {str(ipaddress.ip_address(i)) for i in (pcfg.get("proxy_ips") or [])}

    @property
    def enabled(self):
        return bool(self.cfg.get("enabled")) and bool(self.cfg.get("host"))

    def is_proxy(self, ip):
        return str(ip) in self.proxies

    def _count(self, key, n=1):
        self.stats[key] += n
        d = time.strftime("%Y-%m-%d")
        if d != self.day:
            self.day, self.today = d, collections.Counter()
        if key in ("refused", "denied", "bans"):
            self.today[key] += n

    def admit(self, ip, now=None):
        """-> None if the connection may proceed (and counts it as open), else the reason it is refused."""
        now = time.monotonic() if now is None else now
        until = self.banned.get(ip)
        if until is not None:
            if until > now:
                self._count("banned_hits")
                return "banned"
            self.banned.pop(ip, None)
        if self.total >= int(self.lim["conc_total"]) or self.conc[ip] >= int(self.lim["conc_per_ip"]):
            self._count("limited")
            return "limited"
        q = self.recent[ip]
        while q and q[0] < now - float(self.lim["window_s"]):
            q.popleft()
        if len(q) >= int(self.lim["new_per_ip"]):
            self._count("limited")
            return "limited"
        q.append(now)
        if len(self.recent) > 4096:
            for k in [k for k, v in self.recent.items() if not v][:2048]:
                self.recent.pop(k, None)
        self.conc[ip] += 1
        self.total += 1
        return None

    def opened(self, port, ip):
        self.conns[port] = {"ip": ip, "state": None}

    def mark(self, port, ok):
        """From the auth middleware: this public connection presented an active device (ok) or not (denied: a fail)."""
        c = self.conns.get(port)
        if c is None or c["state"] == "ok":
            return
        if ok:
            c["state"] = "ok"
            self.trusted[c["ip"]] = time.monotonic() + float(self.ban_cfg.get("trust_s", 86400))
            self._count("ok")
        elif c["state"] is None:
            c["state"] = "denied"
            self._count("denied")
            self.fail(c["ip"])

    def state(self, port):
        c = self.conns.get(port)
        return c["state"] if c else None

    def release(self, ip):
        """Give back the slot admit() took (no verdict: e.g. the public site could not be reached)."""
        self.conc[ip] -= 1
        if self.conc[ip] <= 0:
            self.conc.pop(ip, None)
        self.total = max(0, self.total - 1)

    def closed(self, port, ip):
        """Connection over: release its slot; no ok/denied mark = a failed or abandoned handshake (counts as a fail)."""
        self.release(ip)
        c = self.conns.pop(port, None)
        if c is None or c["state"] is None:
            self._count("refused")
            self.fail(ip)

    def fail(self, ip, now=None):
        now = time.monotonic() if now is None else now
        if self.trusted.get(ip, 0) > now:            # his own address (authenticated within trust_s): never banned
            return
        self.trusted.pop(ip, None)
        q = self.fails[ip]
        while q and q[0] < now - float(self.ban_cfg["window_s"]):
            q.popleft()
        q.append(now)
        if len(q) >= int(self.ban_cfg["fails"]) and ip not in self.banned:
            self.banned[ip] = now + float(self.ban_cfg["ban_s"])
            self.fails.pop(ip, None)
            self._count("bans")
            log.info("public: banned %s for %d s after %d failures", ip, int(self.ban_cfg["ban_s"]), int(self.ban_cfg["fails"]))
        if len(self.fails) > 4096:
            for k in [k for k, v in self.fails.items() if not v or v[-1] < now - float(self.ban_cfg["window_s"])][:2048]:
                self.fails.pop(k, None)

    def status(self):
        now = time.monotonic()
        return {"enabled": self.enabled, "open": self.total, "banned_now": sum(1 for u in self.banned.values() if u > now),
                "counters": dict(self.stats), "today": dict(self.today)}
