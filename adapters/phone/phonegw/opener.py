"""Phone actions (`phone open`): open Maps navigation, an https link, an SBB Mobile connection or an installed app on
the phone now.

The frame `open` goes out only on a live socket (never queued: a late copy must not pop Maps up an hour later) and the
app answers `open.result` (opened | notified | refused | stale | no_handler). No answer within `wait` = a stale socket
(his phone drops the tunnel when Android Auto connects, while the old socket still looks open): the gateway closes
that socket at once, so the next call sees "offline" without waiting, and the caller falls back (Telegram URL button).

Direct opens (no tap) only for nav, sbb, app and links whose host is on the allowlist (built-in maps/SBB/media hosts +
config `open.direct_hosts`); every other https link becomes a tap-to-open notification on the phone. Logs carry kind,
status and ms only (never the url, place or app name).
"""
import asyncio
import logging
import re
import urllib.parse

from .util import now_ms, now_s, ulid

log = logging.getLogger("phonegw")

KINDS = ("nav", "url", "sbb", "app")
# Hosts a link may open on without a tap (a host matches itself and its subdomains). Maps and SBB are what nav/sbb
# use; a few read-only media/reference sites. Anything else: tap-to-open notification. Extend in config.json
# "open": {"direct_hosts": [...]} (private values like his own domains live there, not here).
DIRECT_HOSTS = ("google.com", "google.ch", "maps.app.goo.gl", "goo.gl", "sbb.ch", "sbbmobile.ch", "youtube.com",
                "youtu.be", "wikipedia.org", "open.spotify.com", "imdb.com", "themoviedb.org", "meteoswiss.admin.ch")
APP_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,60}$")
# same rule as the push link (server.LINK_RE): https without userinfo, geo:, google.navigation:
LINK_RE = re.compile(r"^(https://[A-Za-z0-9.-]+(:[0-9]{1,5})?([/?#][^\s\x00-\x1f\x7f]*)?"
                     r"|geo:[^\s\x00-\x1f\x7f]+|google\.navigation:[^\s\x00-\x1f\x7f]+)$")
WAIT_DEFAULT_S = 6.0
UNTIL_GRACE_S = 60          # the app never opens a frame older than wait + this


def safe_url(url):
    return isinstance(url, str) and 6 <= len(url) <= 2048 and bool(LINK_RE.fullmatch(url))


def host_ok(host, extra=()):
    host = (host or "").lower().rstrip(".")
    return any(host == h or host.endswith("." + h) for h in tuple(DIRECT_HOSTS) + tuple(extra or ()))


def kind_url_ok(kind, url):
    """nav: google.navigation:/geo: or an https Google Maps link; sbb: an https SBB link; url: https only."""
    if not safe_url(url):
        return False
    if kind == "url":
        return url.startswith("https://")
    host = urllib.parse.urlsplit(url).hostname if url.startswith("https://") else None
    if kind == "nav":
        if url.startswith(("google.navigation:", "geo:")):
            return True
        path = urllib.parse.urlsplit(url).path
        return host in ("maps.app.goo.gl",) or (host in ("google.com", "www.google.com", "maps.google.com", "google.ch",
                                                         "www.google.ch", "maps.google.ch") and path.startswith("/maps"))
    if kind == "sbb":
        return host is not None and (host.endswith("sbbmobile.ch") or host == "sbb.ch" or host.endswith(".sbb.ch"))
    return False


def direct_ok(kind, url, cfg):
    """Policy: may this open without a tap?"""
    o = cfg.get("open") if isinstance(cfg.get("open"), dict) else {}
    if o.get("direct") is False:
        return False
    if kind in ("nav", "sbb", "app"):
        return True
    host = urllib.parse.urlsplit(url).hostname if isinstance(url, str) and url.startswith("https://") else None
    return host is not None and host_ok(host, o.get("direct_hosts") or ())


class Opener:
    """Mixin for the Gateway: POST /internal/open, c2s open.result."""

    def _open_init(self):
        if not hasattr(self, "opens"):
            self.opens = {}

    async def i_open(self, request):
        from aiohttp import web
        from .server import jerr
        self._open_init()
        r = await self._json(request)
        if r is None:
            return jerr(400, "usage", "JSON object expected")
        kind, url, app = r.get("kind"), r.get("url"), r.get("app")
        label = re.sub(r"[\x00-\x1f\x7f]+", " ", str(r.get("label") or "")).strip()[:80]
        if kind not in KINDS or not label:
            return jerr(400, "usage", "kind nav|url|sbb|app and a label are required")
        if kind == "app":
            if not isinstance(app, str) or not APP_RE.fullmatch(app.strip()) or url:
                return jerr(403, "link", "app = an app name or package (1-60 chars), no url")
            app = app.strip()
        elif not kind_url_ok(kind, url) or app:
            return jerr(403, "link", "url not allowed for this kind (nav: google.navigation:/geo:/Google Maps https; "
                                     "sbb: SBB https; url: https without user@host; no spaces, <= 2048 chars)")
        try:
            wait = min(max(float(r.get("wait") or WAIT_DEFAULT_S), 1.0), 20.0)
        except (TypeError, ValueError):
            return jerr(400, "usage", "wait in seconds")
        direct = bool(r.get("direct", True)) and direct_ok(kind, url, self.cfg)
        dev = self.first_device(r.get("device"))
        if not dev:
            return web.json_response({"ok": False, "status": "no_device", "kind": kind})
        caps = (self.db.get_state(dev["id"], "hello", {})[0] or {}).get("caps") or []
        if "open" not in caps:
            return web.json_response({"ok": False, "status": "unsupported", "kind": kind})
        conn = self.conns.get(dev["id"])
        if not conn or getattr(conn, "open_dead", False):
            return web.json_response({"ok": False, "status": "offline", "kind": kind, "direct": direct})
        open_id = "o_" + ulid()
        body = {"open_id": open_id, "kind": kind, "label": label, "direct": direct, "until": now_s() + int(wait) + UNTIL_GRACE_S}
        if kind == "app":
            body["app"] = app
        else:
            body["url"] = url
        tag = str(r.get("tag") or "")
        if re.fullmatch(r"[a-z][a-z0-9_:.-]{0,47}", tag):
            body["tag"] = tag
        fut = asyncio.get_running_loop().create_future()
        self.opens[open_id] = fut
        t0 = now_ms()
        try:
            try:
                await asyncio.wait_for(self.send_direct(conn, "open", body), timeout=min(wait, 5.0))
            except (asyncio.TimeoutError, ConnectionError, RuntimeError) as e:
                self._open_dead(dev["id"], conn, "send %s" % type(e).__name__)
                return web.json_response({"ok": False, "status": "offline", "kind": kind, "direct": direct})
            try:
                res = await asyncio.wait_for(fut, wait)
            except asyncio.TimeoutError:
                self._open_dead(dev["id"], conn, "no open.result in %.0fs" % wait)
                log.info("open %s: timeout after %d ms", kind, now_ms() - t0)
                return web.json_response({"ok": False, "status": "timeout", "kind": kind, "direct": direct,
                                          "ms": now_ms() - t0})
        finally:
            self.opens.pop(open_id, None)
        ms = now_ms() - t0
        status = res.get("result") or "unknown"
        log.info("open %s: %s (%s) in %d ms", kind, status, res.get("how") or "-", ms)
        out = {"ok": status in ("opened", "notified"), "status": status, "kind": kind, "direct": direct,
               "how": res.get("how"), "ms": ms}
        for k in ("locked", "car"):
            if res.get(k) is not None:
                out[k] = res[k]
        return web.json_response(out)

    def _open_dead(self, dev_id, conn, why):
        """A socket that swallowed an open: mark it, close it in the background (a dead peer can take the close
        handshake's full timeout), so callers see offline at once and the app reconnects."""
        if getattr(conn, "open_dead", False):
            return
        conn.open_dead = True
        log.warning("device %s: socket looks stale (%s); closing it", dev_id, why)
        try:
            self.spawn(conn.ws.close(code=4008, message=b"stale"))
        except Exception:
            pass

    def on_open_result(self, dev_id, b):
        self._open_init()
        f = self.opens.get(b.get("open_id"))
        if f and not f.done():
            f.set_result(b)
