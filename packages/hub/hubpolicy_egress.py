#!/usr/bin/env python3
"""hubpolicy_egress: the hub's egress injector. Stdlib only, one file. PROTECTED (hubverify:
app/hubpolicy*), and so are its bindings (app/policy/egress.toml, hubverify: app/policy/*): a release that changes
either loads only after the owner's ✅ on the protected digest. hubkit.release ships both only with EGRESS_IN_RELEASE.

The brain holds handles, not values. A brain-side tool (core.egress) builds its HTTP request as before, writes the
placeholder "hub:secret:<handle>" where the credential goes and posts the request to hubd (POST /egress/fetch, brain
caller). Here the placeholder becomes the value only when
  1. the request's scheme, host and port equal the handle's bound endpoint and its path lies under the endpoint's
     path: else 403 credential_endpoint_mismatch, in every mode, and nothing is sent;
  2. every placeholder sits in a slot the binding declares (a header in its format, a query parameter, a path
     segment, Basic auth) and names this handle: else 400 (placeholder_mismatch, placeholder_outside_slot,
     no_placeholder), nothing is sent;
  3. the method and path match one of the binding's rules (a rule may also list the values a query parameter may take
     when present). A miss is logged as would_deny and the call goes out (mode "audit", the start state), or it is
     refused 403 egress_denied (mode "enforce"). The mode is part of the protected policy: loosening it needs his ✅.
The hub sends the request itself: no proxy from the environment, redirects never followed (a 3xx comes back as it
is, so the credential never travels on), TLS verified, Accept-Encoding forced to identity (gzip/deflate answers are
decoded, any other encoding is refused because it can't be masked), at most max_response bytes. Paging: a GET
that asks for "page": {"from": N} reads up to the binding's max_upstream bytes instead (its default: max_response, so
no binding pages until its policy says so) and, when the far side answers 200 with a JSON array, answers the whole
items N.. that fit in max_response (compact JSON) plus "page": {from, next, total}; next is null after the last one.
Each page is a fresh read of the far side (nothing kept between calls); core.egress asks for pages only after a
response_too_large and joins them. Not a 200 JSON array: as without paging (over max_response: response_too_large).
The answer's status line, headers and body pass hubredact's Redactor (a latin-1 round trip: every byte that is not a
hit stays as it was) before they leave this process. Errors carry codes and exception class names only, never a URL
or a value.

Endpoints are never in a release (no host or address in the repo). They resolve from the hub's secrets folder, which
only the owner's console paste writes, through hubredact's Store (its mode, owner and link rules apply):
  "@endpoints:NAME"   the URL on the NAME line of the egress.endpoints file ("NAME URL" lines, # comments)
  "@field:DOTTED"     a URL inside the binding's own secret file (media.json: "radarr.url")
  "http(s)://…"       a literal, for docker names and public APIs only
A binding whose secret is missing or refused, or whose endpoint does not resolve, is unavailable (503): it fails
closed. Its value is read from the store per call; nothing here copies it anywhere else.

  Injector.load(policy_path, store, redactor, state_dir)   -> Injector (a policy that does not parse: no bindings,
                                        the error in status; the store and redactor are hubredact's)
  inj.check(req) -> (code, answer)      the decision only (nothing sent, no value used): POST /egress/check
  inj.fetch(req) -> (code, answer)      decide, inject, send, mask, log: POST /egress/fetch (blocking: hubd runs it in a
                                        worker thread). 200 = the far side answered: {status, reason, headers, body_b64,
                                        ms, decision, rule, redacted}; any other code = the hub refused: {error, hint}
  inj.status() -> {...}                 bindings (mode, ready or why not, endpoint, slots, rules), counters, the last
                                        refusals and would_deny calls, would_deny shapes: names, methods, paths, codes
  inj.brief() -> {...}                  counts for /health
Request (JSON): {"handle", "method", "url", "headers": {name: value} | [[name, value], …], "body_b64"?, "timeout"?,
"page"?: {"from": N} (GET only)}.
Audit: STATE/egress/audit.jsonl, one line per decision (ts, via, handle, method, path without its query, query
parameter names, decision, code, rule, status, ms, bytes, redacted hits; AUDIT_MAX, then .1 and .2). Never a value, a
query value, a header value or a body. fetch refuses (503 audit_unavailable) when its line can't be opened first.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import fnmatch
import hashlib
import json
import os
import re
import ssl
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections import deque
from dataclasses import dataclass
from typing import Any

PLACEHOLDER = "hub:secret:"
PH_RE = re.compile(r"hub:secret:([A-Za-z0-9_.-]*)")
HANDLE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")  # hubredact.FILE_NAME
FIELD_RE = re.compile(r"^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*$")
HEADER_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}$")
QNAME_RE = re.compile(r"^[A-Za-z0-9_.\[\]-]{1,64}$")
METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
MODES = ("audit", "enforce")
SLOTS = ("header", "query", "path", "basic")
ENDPOINTS_FILE = "egress.endpoints"
DROP_REQ = frozenset(
    {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "proxy-connection", "te", "trailer",
     "transfer-encoding", "upgrade", "host", "content-length", "expect", "accept-encoding"}
)  # fmt: skip
DROP_RESP = frozenset(
    {"connection", "keep-alive", "transfer-encoding", "content-length", "content-encoding", "trailer"}
)
MAX_RESPONSE = 8 << 20
MAX_UPSTREAM = 64 << 20  # the most a binding's max_upstream may read from the far side for paging
PAGE_LIMIT = 2  # paged reads at once (each holds its whole far-side answer in memory)
MAX_PAGE_FROM = 10_000_000
MAX_TIMEOUT = 120.0
DEFAULT_TIMEOUT = 30.0
MAX_URL = 8192
MAX_HEADERS = 64
MAX_HEADER_VALUE = 8192
AUDIT_DIR = "egress"
AUDIT_FILE = "audit.jsonl"
AUDIT_MAX = 16 << 20
AUDIT_KEEP = 2
RECENT = 30
SHAPES = 200
SEG_SAFE = "-._~!$&'()*+,;=:@"
CTRL = re.compile(r"[\x00-\x1f\x7f]")


class PolicyError(ValueError):
    pass


# ------------------------------------------------------------------------------------------------ the policy


@dataclass(frozen=True)
class Slot:
    kind: str  # header | query | path | basic
    name: str = ""  # the header or query parameter
    fmt: str = "{}"  # header and path: the text around the value

    def form(self, handle: str) -> str:
        return self.fmt.replace("{}", PLACEHOLDER + handle)

    def brief(self) -> str:
        return self.kind if self.kind in ("basic", "path") else f"{self.kind}:{self.name}"


@dataclass(frozen=True)
class Rule:
    methods: tuple[str, ...]
    paths: tuple[str, ...]
    query: tuple[tuple[str, tuple[str, ...]], ...]

    def path_ok(self, path: str) -> bool:
        return any(_glob(p).fullmatch(path) for p in self.paths)

    def query_ok(self, pairs: list[tuple[str, str]]) -> bool:
        for name, globs in self.query:  # a listed parameter, when present, takes only the listed values
            if not all(any(fnmatch.fnmatchcase(v, g) for g in globs) for k, v in pairs if k == name):
                return False
        return True


@dataclass(frozen=True)
class Binding:
    name: str
    secret: str
    field: str
    endpoint: str
    slots: tuple[Slot, ...]
    rules: tuple[Rule, ...]
    mode: str
    timeout: float
    max_response: int
    max_upstream: int = 0  # paging reads up to this much from the far side; 0 = max_response (no paging)

    @property
    def upstream(self) -> int:
        return max(self.max_upstream, self.max_response)


@dataclass(frozen=True)
class Endpoint:
    scheme: str
    host: str
    port: int
    base: str  # "" or "/prefix" without the trailing slash

    def text(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}{self.base}"


_GLOBS: dict[str, re.Pattern[str]] = {}


def _glob(p: str) -> re.Pattern[str]:
    """A path pattern: "*" = within one segment, "**" = anything (segments too), the rest literal."""
    rx = _GLOBS.get(p)
    if rx is None:
        out = []
        for part in re.split(r"(\*\*|\*)", p):
            out.append(".*" if part == "**" else "[^/]*" if part == "*" else re.escape(part))
        rx = _GLOBS[p] = re.compile("".join(out))
    return rx


def _keys(d: dict, allowed: set[str], where: str) -> None:
    bad = sorted(set(d) - allowed)
    if bad:
        raise PolicyError(f"{where}: unknown key(s) {', '.join(bad)}")


def _str(d: dict, k: str, where: str, default: str | None = None) -> str:
    v = d.get(k, default)
    if not isinstance(v, str):
        raise PolicyError(f"{where}: {k} must be a string")
    return v


def _slot(d: Any, where: str) -> Slot:
    if not isinstance(d, dict):
        raise PolicyError(f"{where}: a slot is a table")
    _keys(d, {"in", "name", "format"}, where)
    kind = _str(d, "in", where)
    if kind not in SLOTS:
        raise PolicyError(f"{where}: in must be one of {', '.join(SLOTS)}")
    name = _str(d, "name", where, "")
    fmt = _str(d, "format", where, "{}")
    if fmt.count("{}") != 1 or CTRL.search(fmt):
        raise PolicyError(f"{where}: format needs exactly one {{}}")
    if kind == "header" and not HEADER_RE.match(name):
        raise PolicyError(f"{where}: a header slot needs a header name")
    if kind == "query" and (not QNAME_RE.match(name) or fmt != "{}"):
        raise PolicyError(f"{where}: a query slot needs a parameter name and no format")
    if kind == "path" and (name or "/" in fmt):
        raise PolicyError(f"{where}: a path slot has a format within one segment and no name")
    if kind == "basic" and (name or fmt != "{}"):
        raise PolicyError(f"{where}: a basic slot takes neither name nor format")
    return Slot(kind, name, fmt)


def _rule(d: Any, where: str) -> Rule:
    if not isinstance(d, dict):
        raise PolicyError(f"{where}: a rule is a table")
    _keys(d, {"methods", "paths", "query"}, where)
    ms, ps, q = d.get("methods"), d.get("paths"), d.get("query", {})
    if not isinstance(ms, list) or not ms or not all(isinstance(m, str) for m in ms):
        raise PolicyError(f"{where}: methods is a non-empty list")
    methods = tuple(METHODS) if ms == ["*"] else tuple(m.upper() for m in ms)
    if any(m not in METHODS for m in methods):
        raise PolicyError(f'{where}: methods must be among {", ".join(METHODS)} (or ["*"])')
    if not isinstance(ps, list) or not ps or not all(isinstance(p, str) and p.startswith("/") for p in ps):
        raise PolicyError(f"{where}: paths is a non-empty list of absolute patterns")
    if any(CTRL.search(p) or "?" in p for p in ps):
        raise PolicyError(f"{where}: a path pattern has no query and no control characters")
    if not isinstance(q, dict) or not all(
        QNAME_RE.match(k) and isinstance(v, list) and v and all(isinstance(g, str) for g in v) for k, v in q.items()
    ):
        raise PolicyError(f"{where}: query maps a parameter name to a non-empty list of value patterns")
    return Rule(methods, tuple(ps), tuple((k, tuple(v)) for k, v in sorted(q.items())))


def parse_endpoint(url: str) -> Endpoint:
    """A bound endpoint: http(s), a host, an optional port and base path; no user, query or fragment."""
    if not isinstance(url, str) or CTRL.search(url) or len(url) > 2048:
        raise PolicyError("endpoint: not a URL")
    u = urllib.parse.urlsplit(url.strip())
    try:
        port = u.port
    except ValueError:
        raise PolicyError("endpoint: bad port") from None
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or u.query or u.fragment:
        raise PolicyError("endpoint: want http(s)://host[:port][/base]")
    base = u.path.rstrip("/")
    if base and safe_path(base):
        raise PolicyError("endpoint: bad base path")
    return Endpoint(u.scheme, u.hostname.lower(), port or (443 if u.scheme == "https" else 80), base)


def parse_policy(doc: dict) -> dict[str, Binding]:
    """The bindings of a parsed egress.toml. Strict: an unknown key, a bad slot or rule raises PolicyError (the
    injector then has no bindings at all: a typo never widens anything)."""
    _keys(doc, {"version", "defaults", "bindings"}, "policy")
    if doc.get("version") != 1:
        raise PolicyError("policy: version must be 1")
    dfl = doc.get("defaults", {})
    if not isinstance(dfl, dict):
        raise PolicyError("defaults: a table")
    _keys(dfl, {"mode", "timeout", "max_response"}, "defaults")
    out: dict[str, Binding] = {}
    bs = doc.get("bindings", {})
    if not isinstance(bs, dict):
        raise PolicyError("bindings: a table")
    for name, b in bs.items():
        w = f"bindings.{name}"
        if not HANDLE_RE.match(name) or not isinstance(b, dict):
            raise PolicyError(f"{w}: bad handle name")
        _keys(b, {"secret", "field", "endpoint", "inject", "rules", "mode", "timeout", "max_response", "max_upstream"},
              w)  # fmt: skip
        secret, fld, ep = _str(b, "secret", w), _str(b, "field", w, ""), _str(b, "endpoint", w)
        if not FILE_RE.match(secret) or (fld and not FIELD_RE.match(fld)):
            raise PolicyError(f"{w}: bad secret file or field name")
        if ep.startswith("@endpoints:"):
            if not HANDLE_RE.match(ep.split(":", 1)[1]):
                raise PolicyError(f"{w}: bad @endpoints name")
        elif ep.startswith("@field:"):
            if not FIELD_RE.match(ep.split(":", 1)[1]):
                raise PolicyError(f"{w}: bad @field name")
        else:
            parse_endpoint(ep)
        inj = b.get("inject")
        if not isinstance(inj, list) or not inj:
            raise PolicyError(f"{w}: inject is a non-empty list of slots")
        slots = tuple(_slot(s, f"{w}.inject[{i}]") for i, s in enumerate(inj))
        rl = b.get("rules", [])
        if not isinstance(rl, list):
            raise PolicyError(f"{w}: rules is a list")
        rules = tuple(_rule(r, f"{w}.rules[{i}]") for i, r in enumerate(rl))
        mode = b.get("mode", dfl.get("mode", "audit"))
        if mode not in MODES:
            raise PolicyError(f"{w}: mode must be audit or enforce")
        tmo = b.get("timeout", dfl.get("timeout", DEFAULT_TIMEOUT))
        mx = b.get("max_response", dfl.get("max_response", MAX_RESPONSE))
        if not isinstance(tmo, (int, float)) or isinstance(tmo, bool) or not 0 < tmo <= MAX_TIMEOUT:
            raise PolicyError(f"{w}: timeout in (0, {MAX_TIMEOUT:g}]")
        if not isinstance(mx, int) or isinstance(mx, bool) or not 0 < mx <= MAX_RESPONSE:
            raise PolicyError(f"{w}: max_response in (0, {MAX_RESPONSE}]")
        up = b.get("max_upstream", 0)
        if not isinstance(up, int) or isinstance(up, bool) or not (up == 0 or mx <= up <= MAX_UPSTREAM):
            raise PolicyError(f"{w}: max_upstream 0 or in [max_response, {MAX_UPSTREAM}]")
        out[name] = Binding(name, secret, fld, ep, slots, rules, mode, float(tmo), mx, up)
    return out


# ------------------------------------------------------------------------------------------------ helpers


def safe_path(path: str) -> str:
    """ "" when the path is safe to match and forward; else why not. No dot segments (raw or encoded), no empty
    segments, no backslash, no control characters, no encoded slash, backslash or NUL."""
    if not path.startswith("/") or len(path) > MAX_URL:
        return "not absolute"
    if CTRL.search(path) or "\\" in path or "//" in path:
        return "bad characters"
    low = path.lower()
    if any(x in low for x in ("%2f", "%5c", "%00")):
        return "encoded separator"
    if any(urllib.parse.unquote(s) in (".", "..") for s in path.split("/")):
        return "dot segment"
    return ""


def _dig(text: str, dotted: str) -> str | None:
    try:
        o: Any = json.loads(text)
    except ValueError:
        return None
    for k in dotted.split("."):
        if not isinstance(o, dict) or k not in o:
            return None
        o = o[k]
    return o.strip() if isinstance(o, str) else None


def _handles(text: str) -> list[str]:
    """The handles named by placeholders in text, raw and percent-decoded."""
    found = PH_RE.findall(text)
    if "%" in text or "+" in text:
        found += PH_RE.findall(urllib.parse.unquote_plus(text))
    return found


_WS = " \t\n\r"


def json_page(body: bytes, start: int, budget: int) -> tuple[bytes, int | None, int]:
    """Items start.. of a JSON array that fit in budget bytes, whole items only, compact -> (page, next, total).
    next: the first item not in the page (None: the page reaches the end). One item at a time is decoded, so memory
    stays near the body's size. ValueError: not a JSON array (or not UTF-8), start past the end, or one item alone
    over the budget."""
    text = body.decode("utf-8")  # UnicodeDecodeError is a ValueError
    dec = json.JSONDecoder()
    n, size, nxt, out = 0, 2, None, []

    def ws(i: int) -> int:
        while i < len(text) and text[i] in _WS:
            i += 1
        return i

    i = ws(0)
    if text[i : i + 1] != "[":
        raise ValueError("not a JSON array")
    i = ws(i + 1)
    if text[i : i + 1] == "]":
        i += 1
    else:
        while True:
            item, i = dec.raw_decode(text, i)
            if n >= start and nxt is None:
                s = json.dumps(item, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
                add = len(s) + (1 if out else 0)
                if size + add > budget:
                    if not out:
                        raise ValueError(f"item {n} alone is over {budget} bytes")
                    nxt = n
                else:
                    out.append(s)
                    size += add
            n += 1
            i = ws(i)
            c = text[i : i + 1]
            if c == ",":
                i = ws(i + 1)
                continue
            if c == "]":
                i += 1
                break
            raise ValueError("not a JSON array")
    if ws(i) != len(text):
        raise ValueError("data after the array")
    if start > n:
        raise ValueError(f"page start {start} past the end ({n} items)")
    return b"[" + b",".join(out) + b"]", nxt, n


def _shape(path: str) -> str:
    """A path with its ids folded, so would_deny calls group by endpoint."""
    p = re.sub(r"/[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}(?=/|$)", "/{id}", path)
    p = re.sub(r"/[0-9a-fA-F]{16,}(?=/|$)", "/{id}", p)
    return re.sub(r"/\d+(?=/|$)", "/{n}", p)[:200]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: Any, msg: Any, headers: Any, newurl: Any) -> None:  # noqa: PLR0913
        return None  # a 3xx comes back as it is: the credential never follows a redirect


def _opener() -> urllib.request.OpenerDirector:
    ctx = ssl.create_default_context()
    https = urllib.request.HTTPSHandler(context=ctx)
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect(), https)


class Refused(Exception):
    def __init__(self, code: int, error: str, hint: str = "") -> None:
        super().__init__(error)
        self.code, self.error, self.hint = code, error, hint


# ------------------------------------------------------------------------------------------------ the injector


@dataclass
class _Req:
    handle: str
    method: str
    netloc: str
    endpoint: Endpoint
    path: str
    query: str
    headers: list[tuple[str, str]]
    body: bytes | None
    timeout: float
    url: str
    page: int | None = None  # "page": {"from": N}: a paged GET (see the module doc)


class Injector:
    def __init__(self, bindings: dict[str, Binding], store: Any, redactor: Any, state_dir: str, *, sha: str = "",
                 error: str = "") -> None:  # fmt: skip
        self.bindings, self.store, self.redactor = bindings, store, redactor
        self.state_dir, self.sha, self.error = state_dir, sha, error
        self.lock = threading.Lock()
        self.count: dict[str, dict[str, int]] = {}
        self.recent: deque[dict[str, Any]] = deque(maxlen=RECENT)
        self.shapes: dict[tuple[str, str, str], list[float]] = {}
        self.audit_error = ""
        self.opener = _opener()
        self.paging = threading.BoundedSemaphore(PAGE_LIMIT)
        self.ready: dict[str, tuple[Endpoint | None, str]] = {n: self._resolve(b) for n, b in bindings.items()}

    @classmethod
    def load(cls, policy_path: str, store: Any, redactor: Any, state_dir: str) -> Injector:
        try:
            with open(policy_path, "rb") as f:
                raw = f.read(1 << 20)
            bindings = parse_policy(tomllib.loads(raw.decode("utf-8")))
            return cls(bindings, store, redactor, state_dir, sha=hashlib.sha256(raw).hexdigest()[:12])
        except FileNotFoundError:
            return cls({}, store, redactor, state_dir, error="no policy file")
        except (OSError, ValueError) as e:  # tomllib.TOMLDecodeError, PolicyError and UnicodeDecodeError too
            return cls({}, store, redactor, state_dir, error=f"{type(e).__name__}: {str(e)[:200]}")

    # -------------------------------------------------------------------------------------- resolution

    def _value(self, b: Binding) -> str:
        """The credential (KeyError / PermissionError from the store pass up)."""
        text = self.store.value(b.secret)
        if not b.field:
            return text
        v = _dig(text, b.field)
        if not v:
            raise KeyError(f"{b.secret}:{b.field}")
        return v

    def _resolve(self, b: Binding) -> tuple[Endpoint | None, str]:
        try:
            self._value(b)
        except KeyError:
            return None, "no secret"
        except PermissionError:
            return None, "secret refused"
        try:
            if b.endpoint.startswith("@endpoints:"):
                want = b.endpoint.split(":", 1)[1]
                rows = [ln.split() for ln in self.store.value(ENDPOINTS_FILE).splitlines()]
                urls = [r[1] for r in rows if len(r) == 2 and r[0] == want]
                if len(urls) != 1:
                    return None, "endpoint listed twice" if urls else "no endpoint line"
                return parse_endpoint(urls[0]), ""
            if b.endpoint.startswith("@field:"):
                url = _dig(self.store.value(b.secret), b.endpoint.split(":", 1)[1])
                return (parse_endpoint(url), "") if url else (None, "no endpoint field")
            return parse_endpoint(b.endpoint), ""
        except KeyError:
            return None, "no endpoints file"
        except PermissionError:
            return None, "endpoints file refused"
        except PolicyError:
            return None, "endpoint unusable"

    # -------------------------------------------------------------------------------------- one request

    def _parse(self, a: Any) -> tuple[Binding, _Req]:
        if not isinstance(a, dict):
            raise Refused(400, "bad_request", "a JSON object")
        h = a.get("handle")
        if not isinstance(h, str) or not HANDLE_RE.match(h):
            raise Refused(400, "bad_request", "handle")
        b = self.bindings.get(h)
        if b is None:
            raise Refused(404, "unknown_handle", f"no binding {h!r} in the egress policy")
        ep, why = self.ready.get(h, (None, "unresolved"))
        if ep is None:
            raise Refused(503, "binding_unavailable", f"{h}: {why}")
        m = a.get("method")
        if not isinstance(m, str) or m.upper() not in METHODS:
            raise Refused(400, "bad_request", f"method: one of {', '.join(METHODS)}")
        url = a.get("url")
        if not isinstance(url, str) or len(url) > MAX_URL or CTRL.search(url):
            raise Refused(400, "bad_request", "url")
        u = urllib.parse.urlsplit(url)
        try:
            port = u.port or (443 if u.scheme == "https" else 80)
        except ValueError:
            raise Refused(400, "bad_request", "url: port") from None
        if u.fragment or "@" in u.netloc:
            raise Refused(400, "bad_request", "url: no user part, no fragment")
        path = u.path or "/"
        if (u.scheme, (u.hostname or "").lower(), port) != (ep.scheme, ep.host, ep.port) or not (
            not ep.base or path == ep.base or path.startswith(ep.base + "/")
        ):
            raise Refused(403, "credential_endpoint_mismatch", f"{h} is bound to another endpoint")
        why = safe_path(path)
        if why:
            raise Refused(400, "bad_path", why)
        raw_h = a.get("headers") or {}
        pairs = list(raw_h.items()) if isinstance(raw_h, dict) else raw_h
        if not isinstance(pairs, list) or len(pairs) > MAX_HEADERS:
            raise Refused(400, "bad_request", "headers")
        headers: list[tuple[str, str]] = []
        for p in pairs:
            if not (isinstance(p, (list, tuple)) and len(p) == 2 and all(isinstance(x, str) for x in p)):
                raise Refused(400, "bad_request", "headers: name/value strings")
            k, v = p
            if not HEADER_RE.match(k) or CTRL.search(v) or len(v) > MAX_HEADER_VALUE:
                raise Refused(400, "bad_request", "headers: a bad name or value")
            if k.lower() not in DROP_REQ:
                headers.append((k, v))
        body = None
        if a.get("body_b64") is not None:
            if not isinstance(a["body_b64"], str):
                raise Refused(400, "bad_request", "body_b64")
            try:
                body = base64.b64decode(a["body_b64"], validate=True)
            except (binascii.Error, ValueError):
                raise Refused(400, "bad_request", "body_b64: not base64") from None
        tmo = a.get("timeout", b.timeout)
        if not isinstance(tmo, (int, float)) or isinstance(tmo, bool):
            raise Refused(400, "bad_request", "timeout")
        tmo = min(max(float(tmo), 0.5), b.timeout)
        page = None
        if a.get("page") is not None:
            pg = a["page"]
            f = pg.get("from") if isinstance(pg, dict) else None
            if set(pg if isinstance(pg, dict) else ()) != {"from"} or not isinstance(f, int) or isinstance(f, bool) \
                    or not 0 <= f <= MAX_PAGE_FROM:  # fmt: skip
                raise Refused(400, "bad_request", 'page: {"from": N}, N >= 0')
            if m.upper() != "GET":
                raise Refused(400, "bad_request", "page: GET only")
            page = f
        return b, _Req(h, m.upper(), u.netloc, ep, path, u.query, headers, body, tmo, url, page)

    def _decide(self, b: Binding, r: _Req) -> tuple[str, int | None]:
        """-> ("allow", rule index) | ("would_deny" | "deny", None)."""
        pairs = urllib.parse.parse_qsl(r.query, keep_blank_values=True)
        for i, rule in enumerate(b.rules):
            if r.method in rule.methods and rule.path_ok(r.path) and rule.query_ok(pairs):
                return "allow", i
        return ("deny" if b.mode == "enforce" else "would_deny"), None

    @staticmethod
    def _apply(b: Binding, value: str, r: _Req) -> tuple[str, str, list[tuple[str, str]], int]:
        """Placeholders in the binding's slots -> value: (path, query, headers, slots used)."""
        used = 0
        out_h: list[tuple[str, str]] = []
        for k, v in r.headers:
            new = None
            for s in b.slots:
                if s.kind == "header" and k.lower() == s.name.lower() and v == s.form(b.name):
                    new = s.fmt.replace("{}", value)
                elif s.kind == "basic" and k.lower() == "authorization" and v == "Basic " + s.form(b.name):
                    new = "Basic " + base64.b64encode(value.encode("utf-8")).decode("ascii")
            used += new is not None
            out_h.append((k, v if new is None else new))
        parts = r.query.split("&") if r.query else []
        for i, part in enumerate(parts):
            n, eq, v = part.partition("=")
            if not eq:
                continue
            dn, dv = urllib.parse.unquote_plus(n), urllib.parse.unquote_plus(v)
            if any(s.kind == "query" and dn == s.name and dv == s.form(b.name) for s in b.slots):
                parts[i] = f"{n}={urllib.parse.quote(value, safe='')}"
                used += 1
        segs = r.path.split("/")
        for i, seg in enumerate(segs):
            ds = urllib.parse.unquote(seg)
            for s in b.slots:
                if s.kind == "path" and ds == s.form(b.name):
                    segs[i] = urllib.parse.quote(s.fmt.replace("{}", value), safe=SEG_SAFE)
                    used += 1
                    break
        return "/".join(segs), "&".join(parts), out_h, used

    def _placeholders(self, b: Binding, r: _Req) -> None:
        """Refuse (400) a placeholder for another handle, one outside the binding's slots, or none at all."""
        texts = [r.url, *(f"{k}: {v}" for k, v in r.headers)]
        body = r.body.decode("latin-1") if r.body else ""
        if any(x != b.name for t in [*texts, body] for x in _handles(t)):
            raise Refused(400, "placeholder_mismatch", f"a request for {b.name} names another handle")
        path, query, headers, used = self._apply(b, "\x00", r)  # a marker, never the value
        if used == 0:
            slots = ", ".join(s.brief() for s in b.slots)
            raise Refused(400, "no_placeholder", f"no {PLACEHOLDER}{b.name} in a declared slot ({slots})")
        if any(_handles(t) for t in [path, query, *(f"{k}: {v}" for k, v in headers), body]):
            raise Refused(400, "placeholder_outside_slot", "a placeholder outside the binding's slots")

    def _audit_fd(self) -> int:
        d = os.path.join(self.state_dir, AUDIT_DIR)
        os.makedirs(d, mode=0o700, exist_ok=True)
        p = os.path.join(d, AUDIT_FILE)
        with self.lock:
            try:
                if os.path.getsize(p) > AUDIT_MAX:
                    for i in range(AUDIT_KEEP, 0, -1):
                        src = p if i == 1 else f"{p}.{i - 1}"
                        if os.path.exists(src):
                            os.replace(src, f"{p}.{i}")
            except OSError:
                pass
        return os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)

    def _log(self, via: str, r: _Req | None, h: str, decision: str, code: str, rule: int | None, fd: int | None = None,
             **extra: Any) -> None:  # fmt: skip
        """One audit line + the counters. decision: allow | would_deny | deny | refused | error."""
        line: dict[str, Any] = {"ts": round(time.time(), 3), "via": via, "handle": h, "decision": decision,
                                "code": code, "rule": rule}  # fmt: skip
        if r is not None:
            qn = sorted({urllib.parse.unquote_plus(p.partition("=")[0])[:64] for p in r.query.split("&") if p})
            line.update(method=r.method, path=r.path[:512], qnames=qn[:20])
        line.update(extra)
        with self.lock:
            c = self.count.setdefault(h, {})
            key = f"{via}:{decision}"
            c[key] = c.get(key, 0) + 1
            if extra.get("redacted"):
                c["redacted"] = c.get("redacted", 0) + int(extra["redacted"])
            if decision != "allow":
                self.recent.append(
                    {k: line.get(k) for k in ("ts", "via", "handle", "decision", "method", "path", "code")}
                )
            if decision in ("would_deny", "deny") and r is not None:
                k3 = (h, r.method, _shape(r.path))
                if k3 in self.shapes or len(self.shapes) < SHAPES:
                    n = self.shapes.setdefault(k3, [0, 0.0])
                    n[0] += 1
                    n[1] = line["ts"]
        data = (json.dumps(line, sort_keys=True) + "\n").encode()
        try:
            f = fd if fd is not None else self._audit_fd()
            try:
                os.write(f, data)
            finally:
                if fd is None:
                    os.close(f)
            self.audit_error = ""
        except OSError as e:
            self.audit_error = type(e).__name__

    def _front(self, via: str, a: Any) -> tuple[Binding | None, _Req | None, tuple[int, dict[str, Any]]]:
        """Parse and check the placeholders: (binding, request, _) or (None, None, the refusal), logged."""
        got = a.get("handle") if isinstance(a, dict) else None
        h = got if isinstance(got, str) and HANDLE_RE.match(got) else "?"
        r = None
        try:
            b, r = self._parse(a)
            self._placeholders(b, r)
            return b, r, (200, {})
        except Refused as e:
            self._log(via, r, h, "refused", e.error, None)
            return None, None, (e.code, {"error": e.error, "hint": e.hint})

    def check(self, a: Any) -> tuple[int, dict[str, Any]]:
        b, r, refused = self._front("check", a)
        if b is None or r is None:
            return refused
        d, rule = self._decide(b, r)
        self._log("check", r, b.name, d, "egress_denied" if d == "deny" else "", rule)
        return 200, {"decision": d, "rule": rule, "mode": b.mode}

    def fetch(self, a: Any) -> tuple[int, dict[str, Any]]:
        b, r, refused = self._front("fetch", a)
        if b is None or r is None:
            return refused
        d, rule = self._decide(b, r)
        if d == "deny":
            self._log("fetch", r, b.name, d, "egress_denied", None)
            return 403, {"error": "egress_denied", "hint": f"{r.method} {r.path[:200]} is outside {b.name}'s rules"}
        try:
            fd = self._audit_fd()
        except OSError as e:
            self.audit_error = type(e).__name__
            return 503, {"error": "audit_unavailable", "hint": "the egress audit log can't be written"}
        try:
            if r.page is None or b.upstream <= b.max_response:
                return self._send(b, r, d, rule, fd)
            if not self.paging.acquire(timeout=r.timeout):
                self._log("fetch", r, b.name, "error", "paging_busy", rule, fd)
                return 503, {"error": "paging_busy", "hint": f"{PAGE_LIMIT} paged reads already running"}
            try:
                return self._send(b, r, d, rule, fd)
            finally:
                self.paging.release()
        finally:
            os.close(fd)

    def _send(self, b: Binding, r: _Req, d: str, rule: int | None, fd: int) -> tuple[int, dict[str, Any]]:
        t0 = time.monotonic()
        cap = b.upstream if r.page is not None else b.max_response  # a paged read may take more, answers no more

        def done(code: int, ans: dict[str, Any], **extra: Any) -> tuple[int, dict[str, Any]]:
            ms = round((time.monotonic() - t0) * 1000)
            self._log("fetch", r, b.name, d if code == 200 else "error", ans.get("error", ""), rule, fd, ms=ms, **extra)
            if code == 200:
                ans.update(ms=ms, decision=d, rule=rule)
            return code, ans

        try:
            value = self._value(b)
        except (KeyError, PermissionError):
            return done(503, {"error": "binding_unavailable", "hint": f"{b.name}: secret unavailable"})
        if any(s.kind in ("header", "basic") for s in b.slots) and (CTRL.search(value) or not value.isascii()):
            return done(503, {"error": "secret_unusable", "hint": f"{b.name}: the value can't travel in a header"})
        path, query, headers, _ = self._apply(b, value, r)
        e = r.endpoint  # the host part comes from the bound endpoint, never from the brain's text (no parser games)
        host = f"[{e.host}]" if ":" in e.host else e.host
        netloc = host if e.port == (443 if e.scheme == "https" else 80) else f"{host}:{e.port}"
        req = urllib.request.Request(urllib.parse.urlunsplit((e.scheme, netloc, path, query, "")),
                                     data=r.body, method=r.method)  # fmt: skip
        for k, v in headers:
            req.add_header(k, v)
        req.add_header("Accept-Encoding", "identity")
        resp: Any
        try:
            resp = self.opener.open(req, timeout=r.timeout)
        except urllib.error.HTTPError as he:
            resp = he  # an HTTP answer (4xx/5xx, or a 3xx not followed): passed on as it is
        except TimeoutError:
            return done(504, {"error": "upstream_timeout", "hint": f"no answer in {r.timeout:g} s"})
        except urllib.error.URLError as ue:
            if isinstance(ue.reason, TimeoutError):
                return done(504, {"error": "upstream_timeout", "hint": f"no answer in {r.timeout:g} s"})
            why = type(ue.reason).__name__ if isinstance(ue.reason, BaseException) else "URLError"
            return done(502, {"error": "upstream_unreachable", "hint": why})
        except (OSError, ValueError) as oe:  # http.client.InvalidURL, RemoteDisconnected, ssl errors: the class only
            return done(502, {"error": "upstream_error", "hint": type(oe).__name__})
        try:
            status = int(getattr(resp, "status", None) or getattr(resp, "code", 0) or 0)
            reason = str(getattr(resp, "reason", "") or "")
            raw_headers = list(resp.headers.items()) if resp.headers is not None else []
            body = b"" if r.method == "HEAD" else (resp.read(cap + 1) or b"")
        except TimeoutError:
            return done(504, {"error": "upstream_timeout", "hint": "the answer stalled"})
        except (OSError, ValueError, AttributeError) as oe:  # AttributeError: an HTTPError without a body
            return done(502, {"error": "upstream_error", "hint": type(oe).__name__})
        finally:
            with contextlib.suppress(Exception):
                resp.close()
        if len(body) > cap:
            return done(502, {"error": "response_too_large", "hint": f"over {cap} bytes"})
        enc = ",".join(v.strip().lower() for k, v in raw_headers if k.lower() == "content-encoding")
        if enc and enc != "identity":
            if enc not in ("gzip", "x-gzip", "deflate"):
                return done(502, {"error": "encoded_response", "hint": "an encoding that can't be masked"})
            out = None
            for wbits in (47, -zlib.MAX_WBITS):  # gzip/zlib (auto), then raw deflate
                try:
                    dz = zlib.decompressobj(wbits)
                    out = dz.decompress(body, cap + 1)
                    if len(out) > cap or dz.unconsumed_tail:
                        return done(502, {"error": "response_too_large", "hint": f"over {cap} bytes"})
                    break
                except zlib.error:
                    out = None
            if out is None:
                return done(502, {"error": "encoded_response", "hint": "a body that does not decode"})
            body = out
        page: dict[str, Any] | None = None
        if r.page is not None and status == 200 and cap > b.max_response:
            try:
                body, nxt, total = json_page(body, r.page, b.max_response)
            except ValueError as ve:
                if len(body) > b.max_response:
                    return done(502, {"error": "response_too_large", "hint": f"over {b.max_response} bytes, and no "
                                      f"JSON array to page ({str(ve)[:80]})"})  # fmt: skip
            else:
                page = {"from": r.page, "next": nxt, "total": total}
        elif len(body) > b.max_response:
            return done(502, {"error": "response_too_large", "hint": f"over {b.max_response} bytes"})
        hits: list[str] = []
        try:
            text, h1 = self.redactor.text(body.decode("latin-1"))
            out_body = text.encode("latin-1")
            hits += h1
            out_h = []
            for k, v in raw_headers:
                if k.lower() not in DROP_RESP:
                    v2, h2 = self.redactor.text(v)
                    hits += h2
                    out_h.append([k, v2])
            reason, h3 = self.redactor.text(reason)
            hits += h3
        except Exception as xe:  # the mask failed: never the unmasked answer (fail closed)
            return done(502, {"error": "redaction_failed", "hint": type(xe).__name__})
        ans = {"status": status, "reason": reason[:200], "headers": out_h,
               "body_b64": base64.b64encode(out_body).decode("ascii"), "redacted": len(hits)}  # fmt: skip
        extra: dict[str, Any] = {}
        if page is not None:
            ans["page"] = page
            extra = {"page_from": page["from"], "page_next": page["next"], "page_total": page["total"]}
        return done(200, ans, status=status, bytes_out=len(r.body or b""), bytes_in=len(body), redacted=len(hits),
                    **extra)  # fmt: skip

    # -------------------------------------------------------------------------------------- reports

    def status(self) -> dict[str, Any]:
        with self.lock:
            count = {k: dict(v) for k, v in self.count.items()}
            recent = list(self.recent)
            shapes = sorted(self.shapes.items(), key=lambda kv: -kv[1][0])[:50]
        bs = {}
        for n, b in sorted(self.bindings.items()):
            ep, why = self.ready.get(n, (None, "unresolved"))
            bs[n] = {
                "mode": b.mode,
                "ready": ep is not None,
                "why": why,
                "endpoint": ep.text() if ep else None,
                "secret": b.secret + (f":{b.field}" if b.field else ""),
                "slots": [s.brief() for s in b.slots],
                "rules": len(b.rules),
                "count": count.get(n, {}),
            }
        return {
            "policy": {"sha": self.sha, "error": self.error, "bindings": len(self.bindings)},
            "bindings": bs,
            "recent": recent,
            "would_deny": [{"handle": h, "method": m, "path": p, "n": v[0], "last": v[1]} for (h, m, p), v in shapes],
            "audit": {"file": f"{AUDIT_DIR}/{AUDIT_FILE}", "error": self.audit_error},
        }

    def brief(self) -> dict[str, Any]:
        with self.lock:
            tot: dict[str, int] = {}
            for c in self.count.values():
                for k, v in c.items():
                    tot[k] = tot.get(k, 0) + v
        modes = [b.mode for b in self.bindings.values()]

        def n(*keys: str) -> int:
            return sum(tot.get(k, 0) for k in keys)

        return {
            "state": "ok" if not self.error else "policy_error",
            "bindings": len(self.bindings),
            "ready": sum(1 for ep, _ in self.ready.values() if ep is not None),
            "enforce": modes.count("enforce"),
            "policy": self.sha,
            "sent": n("fetch:allow", "fetch:would_deny"),
            "would_deny": n("fetch:would_deny", "check:would_deny"),
            "denied": n("fetch:deny", "check:deny"),
            "refused": n("fetch:refused", "check:refused"),
            "errors": n("fetch:error"),
            "redacted": n("redacted"),
        }
