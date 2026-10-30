"""A fake hub API for the brain-side hub client tests: a threaded HTTP server on 127.0.0.1 that records every request
(method, path, JSON body or raw size) and answers from per-route handlers. Never the real hub, never a real token."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

Handler = Callable[[str, str, Any], tuple[int, Any]]


class FakeHub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.routes: dict[tuple[str, str], Handler] = {}
        self.down = False
        hub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def _go(self, method: str) -> None:
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                if self.headers.get("Content-Type") == "application/octet-stream":
                    body: Any = {"_raw": len(raw), "_name": self.headers.get("X-File-Name")}
                else:
                    body = json.loads(raw) if raw else None
                path = self.path
                hub.calls.append((method, path, body))
                if hub.down:
                    code, out = 503, {"error": "unavailable"}
                else:
                    fn = hub.routes.get((method, path.split("?")[0]))
                    code, out = fn(method, path, body) if fn else (404, {"error": "not_found"})
                data = json.dumps(out).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                self._go("GET")

            def do_POST(self) -> None:
                self._go("POST")

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.t.start()

    def on(self, method: str, path: str, fn: Handler | Any) -> None:
        self.routes[(method, path)] = fn if callable(fn) else (lambda m, p, b, v=fn: (200, v))

    def bodies(self, path: str) -> list[Any]:
        return [b for m, p, b in self.calls if p.split("?")[0] == path]

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()
