"""core.http: the one urllib client: User-Agent, timeout, optional retries with backoff. Stdlib only.

  open(url, data=None, headers=None, method=None, timeout=20, retries=0, backoff=1.0, ua=None)
        -> the urllib response (use it as a context manager); after the last try urllib's own errors propagate
           unchanged (urllib.error.HTTPError / URLError, TimeoutError, OSError), so callers keep their handling
  get_bytes(url, max_bytes=None, **kw) / get_text(url, max_bytes=None, encoding="utf-8", errors="replace", **kw)
  get_json(url, **kw)                  json.load of the body (ValueError when it is not JSON)
  call_json(method, url, body=None, token=None, token_file=None, headers=None, timeout=10, retries=0)
        -> (status, json or None): an internal JSON API (the hub, an adapter, the bridge) with a bearer token
           (given, or the first line of token_file; never printed); never raises for the far side: any HTTP answer
           is (status, body), unreachable / timeout / unreadable token file is (0, None)

url may be a str or a urllib.request.Request (its headers are kept; `headers` adds to them). A request without a
User-Agent gets `ua` or DEFAULT_UA. Proxies come from the environment as with urllib.request.urlopen (tests point
them at a local fake). Retries (retries=N means up to N more tries) happen on HTTP 429/500/502/503/504 (Retry-After
honoured up to 60 s), connection errors and timeouts, never on other HTTP errors; the wait doubles from `backoff`
seconds (max 30). Default retries=0: one try, exactly like a bare urlopen.
"""

from __future__ import annotations

import email.utils
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

__all__ = ["DEFAULT_UA", "RETRY_CODES", "call_json", "get_bytes", "get_json", "get_text", "open"]

DEFAULT_UA = "assistant-core/1 (personal automation; python urllib)"
RETRY_CODES = frozenset({429, 500, 502, 503, 504})
_MAX_WAIT = 30.0
_MAX_RETRY_AFTER = 60.0


def _request(
    url: str | urllib.request.Request,
    data: bytes | None,
    headers: dict[str, str] | None,
    method: str | None,
    ua: str | None,
) -> urllib.request.Request:
    if isinstance(url, urllib.request.Request):
        req = url
        if data is not None:
            req.data = data
        if method is not None:
            req.method = method
    else:
        req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if not req.has_header("User-agent"):
        req.add_header("User-Agent", ua or DEFAULT_UA)
    return req


def _retry_after(e: urllib.error.HTTPError) -> float | None:
    raw = e.headers.get("Retry-After") if e.headers is not None else None
    if not raw:
        return None
    raw = raw.strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - time.time())


def open(  # mirrors urllib.request.urlopen (the module never needs the builtin)
    url: str | urllib.request.Request,
    data: bytes | None = None,
    *,
    headers: dict[str, str] | None = None,
    method: str | None = None,
    timeout: float = 20.0,
    retries: int = 0,
    backoff: float = 1.0,
    ua: str | None = None,
) -> Any:
    req = _request(url, data, headers, method, ua)
    attempt = 0
    while True:
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            if attempt >= retries or e.code not in RETRY_CODES:
                raise
            ra = _retry_after(e)
            wait = min(_MAX_RETRY_AFTER, ra) if ra is not None else min(_MAX_WAIT, backoff * 2**attempt)
            e.close()
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt >= retries:
                raise
            wait = min(_MAX_WAIT, backoff * 2**attempt)
        attempt += 1
        if wait > 0:
            time.sleep(wait)


def get_bytes(url: str | urllib.request.Request, *, max_bytes: int | None = None, **kw: Any) -> bytes:
    with open(url, **kw) as r:
        return r.read() if max_bytes is None else r.read(max_bytes)


def get_text(
    url: str | urllib.request.Request,
    *,
    max_bytes: int | None = None,
    encoding: str = "utf-8",
    errors: str = "replace",
    **kw: Any,
) -> str:
    return get_bytes(url, max_bytes=max_bytes, **kw).decode(encoding, errors)


def get_json(url: str | urllib.request.Request, **kw: Any) -> Any:
    with open(url, **kw) as r:
        return json.load(r)


def call_json(
    method: str,
    url: str,
    body: Any = None,
    *,
    token: str | None = None,
    token_file: str | os.PathLike[str] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
    retries: int = 0,
) -> tuple[int, Any]:
    """One call to an internal JSON API: (HTTP status, parsed body or None); (0, None) when it can't be reached."""
    h = {"Accept": "application/json", **(headers or {})}
    if token_file is not None:
        try:
            token = Path(token_file).read_text(encoding="utf-8").splitlines()[0].strip()
        except (OSError, IndexError, UnicodeDecodeError):
            return 0, None
    if token:
        h["Authorization"] = "Bearer " + token
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    try:
        with open(url, data, headers=h, method=method, timeout=timeout, retries=retries) as r:
            st, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        try:
            st, raw = e.code, e.read()
        except OSError:
            st, raw = e.code, b""
        finally:
            e.close()
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return 0, None
    try:
        return st, (json.loads(raw) if raw else None)
    except ValueError:
        return st, None
