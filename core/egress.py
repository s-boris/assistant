"""core.egress: HTTP credentials by handle, not by file. Stdlib only.

A tool that calls an HTTP API with a credential asks for it by handle and sends through here, so the key can leave
the brain's container one handle at a time without touching the tool again:
  secret(handle) -> str        what goes where the API wants its credential. local and shadow mode: the value itself,
                               read here from ~/.secrets as the tools did; hub mode: the placeholder
                               "hub:secret:<handle>" (the brain never sees the value)
  open(url_or_request, handle, data=None, *, headers=None, method=None, timeout=20.0) -> response
                               local: core.http.open (direct). shadow: the same, plus the hub's decision
                               for that request (POST /egress/check with the value swapped back to the placeholder;
                               never fails or slows the call by more than CHECK_TIMEOUT). hub: POST /egress/fetch: the
                               hub swaps the placeholder for the value only toward the handle's bound endpoint and
                               answers masked. The response is file-like (read, status, getcode, headers, getheader,
                               a context manager). A GET whose answer is over the hub's cap (response_too_large) is
                               asked again in JSON-array pages ("page": {"from": N}, for a binding with max_upstream)
                               and joined into one compact JSON array, so the caller reads it as one answer. Like
                               urllib, a status >= 400 raises urllib.error.HTTPError (in hub
                               mode a 3xx too: the hub never follows a redirect); a hub refusal raises Refused (a
                               URLError: code, error, hint); an unreachable hub raises urllib.error.URLError.
  mode(handle) -> "local" | "shadow" | "hub"
                               ~/.config/assistant/egress.json {"default": MODE, "handles": {HANDLE: MODE}}; no file
                               (or a broken one) = local; `hub egress use HANDLE MODE` sets it.
  HANDLES                      handle -> (its file in ~/.secrets, a dotted JSON field or None): the local copies. The
                               hub's side (the policy's bindings) names the same handles.
Values are never printed, logged or put into an exception here.
"""

from __future__ import annotations

import base64
import email.message
import io
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from core import http, paths

__all__ = ["HANDLES", "MODES", "PLACEHOLDER", "Missing", "Refused", "Response", "mode", "open", "secret", "set_mode"]

PLACEHOLDER = "hub:secret:"
MODES = ("local", "shadow", "hub")
SWITCH = "egress.json"
CHECK_TIMEOUT = 1.5
MAX_PAGES = 64  # a paged GET: at most this many hub calls (and 2 tries when the list moves under it)
HUB_SLACK = 10.0  # the hub's own call may take the tool's timeout; its answer comes this much later at most
HANDLE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
HANDLES: dict[str, tuple[str, str | None]] = {
    "ha": ("ha-admin-token", None),
    "radarr": ("media.json", "radarr.key"),
    "sonarr": ("media.json", "sonarr.key"),
    "seerr": ("media.json", "seerr.key"),
    "emby": ("media.json", "emby.key"),
    "bazarr": ("media.json", "bazarr.key"),
    "sabnzbd": ("media.json", "sabnzbd.key"),
    "tracearr": ("tracearr-key", None),
    "vault": ("vault-token", None),
    "gym": ("jefit-token", None),
    "phone": ("phone-cli-token", None),
    "dash": ("browser-basicauth.txt", None),
}


class Missing(KeyError):
    """No local copy of the handle's credential (local / shadow mode)."""


class Refused(urllib.error.URLError):
    """The hub refused the request (it was not sent): code (HTTP status of the hub), error, hint."""

    def __init__(self, code: int, error: str, hint: str = "") -> None:
        super().__init__(f"egress {code} {error}" + (f": {hint}" if hint else ""))
        self.code, self.error, self.hint = code, error, hint


# ------------------------------------------------------------------------------------------------ the switch


def _switch() -> dict[str, Any]:
    try:
        d = json.loads(paths.config(SWITCH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def mode(handle: str) -> str:
    d = _switch()
    hs = d.get("handles") if isinstance(d.get("handles"), dict) else {}
    m = (hs or {}).get(handle, d.get("default", "local"))
    return m if m in MODES else "local"


def set_mode(handle: str, m: str) -> dict[str, Any]:
    """Write one handle's mode ("default" = every handle without its own); returns the new switch."""
    if m not in MODES or (handle != "default" and handle not in HANDLES):
        raise ValueError("handle (or default) and a mode: local | shadow | hub")
    d = _switch()
    hs = dict(d.get("handles") or {}) if isinstance(d.get("handles"), dict) else {}
    if handle == "default":
        d["default"] = m
    else:
        hs[handle] = m
    d["handles"] = hs
    f = paths.config(SWITCH)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f".{f.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(d, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, f)
    return d


# ------------------------------------------------------------------------------------------------ values


def _local(handle: str) -> str:
    if handle not in HANDLES:
        raise Missing(handle)
    name, field = HANDLES[handle]
    try:
        text = (paths.home() / ".secrets" / name).read_text(encoding="utf-8").strip()
    except OSError:
        raise Missing(handle) from None
    if field is None:
        if not text:
            raise Missing(handle)
        return text
    try:
        o: Any = json.loads(text)
    except ValueError:
        raise Missing(handle) from None
    for k in field.split("."):
        o = o.get(k) if isinstance(o, dict) else None
    if not isinstance(o, str) or not o.strip():
        raise Missing(handle)
    return o.strip()


def secret(handle: str) -> str:
    if not HANDLE_RE.match(handle) or handle not in HANDLES:
        raise ValueError(f"unknown egress handle {handle!r}")
    return PLACEHOLDER + handle if mode(handle) == "hub" else _local(handle)


# ------------------------------------------------------------------------------------------------ sending


class Response(io.BytesIO):
    """What hub mode answers with: enough of http.client.HTTPResponse for the tools."""

    def __init__(self, url: str, status: int, reason: str, headers: list[list[str]], body: bytes) -> None:
        super().__init__(body)
        self.url, self.status, self.code, self.reason, self.msg = url, status, status, reason, reason
        m = email.message.Message()
        for k, v in headers:
            m[k] = v
        self.headers = m

    def getcode(self) -> int:
        return self.status

    def getheader(self, name: str, default: str | None = None) -> str | None:
        return self.headers.get(name, default)

    def getheaders(self) -> list[tuple[str, str]]:
        return list(self.headers.items())

    def info(self) -> email.message.Message:
        return self.headers

    def geturl(self) -> str:
        return self.url


def _parts(url: str | urllib.request.Request, data: bytes | None, headers: dict[str, str] | None,
           method: str | None) -> tuple[str, str, list[tuple[str, str]], bytes | None]:  # fmt: skip
    if isinstance(url, urllib.request.Request):
        req = url
        body = data if data is not None else req.data
        if body is not None and not isinstance(body, bytes):
            raise TypeError("egress: the request body must be bytes")
        hs = list(req.header_items())
        u, m = req.full_url, method or req.get_method()
    else:
        body, hs, u = data, [], url
        m = method or ("POST" if data is not None else "GET")
    for k, v in (headers or {}).items():
        hs = [(a, b) for a, b in hs if a.lower() != k.lower()] + [(k, v)]
    return u, m.upper(), hs, body


def _hub(path: str, body: dict[str, Any], timeout: float) -> tuple[int, dict[str, Any]]:
    from core import hub  # late: hub imports a lot that local mode never needs

    data = json.dumps(body).encode()
    try:
        hdrs = {"Authorization": f"Bearer {hub._token()}", "Content-Type": "application/json"}  # no token: HubDown
        with http.open(hub.url() + path, data=data, headers=hdrs, method="POST", timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            got = json.loads(e.read() or b"{}")
        except ValueError:
            got = {}
        e.close()
        return e.code, got if isinstance(got, dict) else {}
    except hub.HubDown as e:
        raise urllib.error.URLError(f"egress: hub unreachable ({e})") from None
    except ValueError:
        raise urllib.error.URLError("egress: the hub's answer is not JSON") from None


def _unsub(text: str, value: str, ph: str) -> str:
    """The local value back to the placeholder (plain, URL-encoded, Basic auth), for a shadow check."""
    out = text.replace(value, ph)
    for q in (urllib.parse.quote(value, safe=""), urllib.parse.quote_plus(value, safe="")):
        out = out.replace(q, urllib.parse.quote(ph, safe=""))
    b64 = base64.b64encode(value.encode("utf-8")).decode("ascii")
    return out.replace(b64, ph)


def _shadow(handle: str, u: str, m: str, hs: list[tuple[str, str]], timeout: float) -> None:
    try:
        value = _local(handle)
        ph = PLACEHOLDER + handle
        url = _unsub(u, value, ph)
        heads = [[k, _unsub(v, value, ph)] for k, v in hs]
        blob = json.dumps([url, heads])
        if value in blob or urllib.parse.quote(value, safe="") in blob:
            return  # never send the value along with a check
        _hub(
            "/egress/check", {"handle": handle, "method": m, "url": url, "headers": heads}, min(timeout, CHECK_TIMEOUT)
        )
    except Exception:  # noqa: BLE001
        return  # a shadow check never fails the call (no hub token, hub down, a refused path under test, ...)


def _paged(req: dict[str, Any], timeout: float) -> tuple[int, dict[str, Any], bytes | None]:
    """A GET the hub found too large, asked again page by page -> (code, answer, joined body or None).
    A refusal (an older hub answers response_too_large again; not_pageable, ...) or an answer without "page" (the far
    side's error, a body that fits) comes back as it is, body None: open() handles it as an unpaged answer."""
    code, ans = 502, {"error": "page_unstable", "hint": "the list changed while it was read in pages"}
    for _ in range(2):  # the list moved under us (total changed): once more from the start
        items: list[Any] = []
        start, total = 0, None
        for _ in range(MAX_PAGES):
            code, ans = _hub("/egress/fetch", dict(req, page={"from": start}), timeout + HUB_SLACK)
            pg = ans.get("page") if code == 200 else None
            if not isinstance(pg, dict):
                return code, ans, None
            try:
                got = json.loads(base64.b64decode(ans.get("body_b64") or ""))
                nxt, tot, frm = pg.get("next"), int(pg["total"]), int(pg["from"])
            except (KeyError, TypeError, ValueError):
                raise urllib.error.URLError("egress: a malformed page from the hub") from None
            if total is None:
                total = tot
            if tot != total or frm != start or not isinstance(got, list):
                break
            items += got
            if nxt is None:
                if len(items) != total:
                    break
                ans.pop("body_b64", None)
                return code, ans, json.dumps(items, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            if not isinstance(nxt, int) or nxt != start + len(got) or nxt <= start:
                raise urllib.error.URLError("egress: a page that does not move on")
            start = nxt
        else:
            raise urllib.error.URLError(f"egress: over {MAX_PAGES} pages")
    return 502, {"error": "page_unstable", "hint": "the list changed while it was read in pages"}, None


def open(  # mirrors core.http.open
    url: str | urllib.request.Request,
    handle: str,
    data: bytes | None = None,
    *,
    headers: dict[str, str] | None = None,
    method: str | None = None,
    timeout: float = 20.0,
) -> Any:
    if handle not in HANDLES:
        raise ValueError(f"unknown egress handle {handle!r}")
    md = mode(handle)
    u, m, hs, body = _parts(url, data, headers, method)
    if md != "hub":
        if md == "shadow":
            _shadow(handle, u, m, hs, timeout)
        return http.open(urllib.request.Request(u, data=body, headers=dict(hs), method=m), timeout=timeout)
    req: dict[str, Any] = {"handle": handle, "method": m, "url": u, "headers": [[k, v] for k, v in hs],
                           "timeout": timeout}  # fmt: skip
    if body is not None:
        req["body_b64"] = base64.b64encode(body).decode("ascii")
    code, ans = _hub("/egress/fetch", req, timeout + HUB_SLACK)
    raw: bytes | None = None
    if code == 502 and ans.get("error") == "response_too_large" and m == "GET":
        code, ans, raw = _paged(req, timeout)
    if code != 200:
        raise Refused(code, str(ans.get("error") or "error"), str(ans.get("hint") or ""))
    try:
        if raw is None:
            raw = base64.b64decode(ans.get("body_b64") or "")
        status = int(ans["status"])
        heads = [[str(k), str(v)] for k, v in ans.get("headers") or []]
    except (KeyError, TypeError, ValueError):
        raise urllib.error.URLError("egress: a malformed answer from the hub") from None
    resp = Response(u, status, str(ans.get("reason") or ""), heads, raw)
    if status >= 300:
        raise urllib.error.HTTPError(u, status, resp.reason, resp.headers, resp)
    return resp
