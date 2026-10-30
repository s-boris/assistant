"""hubgate: the browser service's inputs inside claude-hub-browser. Stdlib only.

- tasks(): the web agent's live tasks and classifier, as browsersvc.WebagentTasks over the brain's webagent module
  and task files, both mounted read-only.
- BrowserTokens: the same token rule as browsersvc.HubTokens (his approval used by webagent:<task> for exactly this
  stop's action, used within the window, then a single-use receipt), but checked here with this container's own hub
  caller (token_browser) on the hub's two /browser routes (packages/hub/hubbrowser.py): no brain token, no other
  hub route. The hub unreachable = no token (fail closed).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

WINDOW = 600 + 120  # browsersvc.APPROVED_WINDOW


class HubClient:
    def __init__(self, url: str, token_file: str, timeout: float = 4.0) -> None:
        self.url, self.token_file, self.timeout = url.rstrip("/"), token_file, timeout

    def _token(self) -> str:
        with open(self.token_file, encoding="utf-8") as f:
            return f.read().strip()

    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self._token()}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"null")
            except ValueError:
                return e.code, None


class BrowserTokens:
    def __init__(self, client: Any, clock: Callable[[], float] = time.time) -> None:
        self.client, self.clock = client, clock

    def take(self, t: dict[str, Any], tasks: Any) -> tuple[bool, str]:
        tid = str(t.get("id") or "")
        if t.get("mode") == "dry":
            return (True, "dry") if tasks.dry_ok(t) else (False, "dry run off its dry host")
        nonce = (t.get("approval") or {}).get("nonce") or (t.get("pending") or {}).get("nonce")
        if not nonce:
            return False, "no hub approval token (a card without ✅ buttons: stop again for a new card)"
        nonce = str(nonce).strip().lower()
        try:
            st, row = self.client.call("GET", f"/browser/approval/{nonce}")
        except Exception as e:  # the hub unreachable: fail closed
            return False, f"token unverified ({type(e).__name__})"
        if st == 404:
            return False, "no such approval"
        if st != 200 or not isinstance(row, dict):
            return False, f"token unverified (hub {st})"
        state = row.get("state")
        if state != "used" or row.get("used_by") != f"webagent:{tid}":
            return False, f"his approval was not used for this task ({state})"
        if row.get("action_sha") != hashlib.sha256(tasks.action(t).encode("utf-8")).hexdigest():
            return False, "another action: his approval does not cover it"
        if self.clock() - float(row.get("used_ts") or 0) > WINDOW:
            return False, "his approval is too old"
        body = {"task": tid, "stop": t.get("stops") if isinstance(t.get("stops"), int) else None, "nonce": nonce,
                "rid": secrets.token_hex(8)}  # fmt: skip
        try:
            st, res = self.client.call("POST", "/browser/commit", body)
        except Exception as e:
            return False, f"receipt not recorded ({type(e).__name__})"
        if st != 200 or not isinstance(res, dict):
            return False, f"receipt not recorded (hub {st})"
        if res.get("dup"):
            return False, "token used already"
        if res.get("id") is None:
            return False, "receipt not recorded"
        return True, "ok"


def load_webagent(lib_dir: str, state_dir: str) -> Any:
    """The brain's webagent module from the read-only mount (its task files from state_dir)."""
    os.environ["WEBAGENT_STATE"] = state_dir
    path = os.path.join(lib_dir, "webagent.py")
    spec = importlib.util.spec_from_file_location("webagent", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"no webagent module at {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["webagent"] = mod
    spec.loader.exec_module(mod)
    return mod


def gate(bs: Any, cfg: dict[str, Any], mode_fn: Callable[[], str]) -> Any:
    """browsersvc.Gate for the container: the mounted web agent + this container's hub tokens."""
    wa = load_webagent(cfg["webagent"]["lib"], cfg["webagent"]["state"])
    client = HubClient(cfg["hub"]["url"], cfg["hub"]["token_file"])
    return bs.Gate(bs.WebagentTasks(wa), BrowserTokens(client), mode_fn=mode_fn, notify=None)
