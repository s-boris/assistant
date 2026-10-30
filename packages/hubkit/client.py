"""hubkit.client: the hub's /health and /whoami from the brain container. Stdlib only (core.http).

url()                      HUB_URL, else http://claude-hub:8700 (refused under ASSISTANT_TEST=1: tests set HUB_URL)
get(path, auth, timeout)   -> (status | None, json body | None, error): status None = unreachable (error says why)
health(timeout)            GET /health (no token)
whoami(timeout)            GET /whoami with the brain's token
token_state()              "ok" | "missing" | "mode 0644" ...: never the value
"""

from __future__ import annotations

import json
import os
import socket
import stat
import urllib.error

from core import http, paths

from . import DEFAULT_URL, HubError, token_file


def url() -> str:
    u = os.environ.get("HUB_URL", "").rstrip("/")
    if u:
        return u
    if paths.test_mode():
        raise HubError("ASSISTANT_TEST=1: set HUB_URL (tests never reach the real hub)", 2)
    return DEFAULT_URL


def _token() -> str | None:
    try:
        t = token_file().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return t or None


def token_state() -> str:
    p = token_file()
    try:
        st = os.stat(p)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"
    mode = stat.S_IMODE(st.st_mode)
    if mode & 0o077:
        return f"mode {mode:04o} (want 0600)"
    return "ok" if _token() else "empty"


def _why(e: BaseException) -> str:
    r = getattr(e, "reason", e)
    if isinstance(r, socket.gaierror):
        return "name not resolved (stack not installed, or this container is not on its network)"
    if isinstance(r, ConnectionRefusedError):
        return "connection refused (hub container down or starting)"
    if isinstance(r, (TimeoutError, socket.timeout)):
        return "timed out"
    return type(r).__name__


def get(path: str, auth: bool = False, timeout: float = 4.0) -> tuple[int | None, dict | None, str]:
    headers = {}
    if auth:
        tok = _token()
        if tok is None:
            return None, None, "no hub token (the install paste writes it)"
        headers["Authorization"] = f"Bearer {tok}"
    try:
        with http.open(url() + path, headers=headers, timeout=timeout) as r:
            return r.status, json.load(r), ""
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read() or b"null")
        except ValueError:
            body = None
        e.close()
        return e.code, body if isinstance(body, dict) else None, ""
    except (urllib.error.URLError, OSError) as e:
        return None, None, _why(e)
    except ValueError:
        return None, None, "answer is not JSON"


def health(timeout: float = 4.0) -> tuple[int | None, dict | None, str]:
    return get("/health", timeout=timeout)


def whoami(timeout: float = 4.0) -> tuple[int | None, dict | None, str]:
    return get("/whoami", auth=True, timeout=timeout)
