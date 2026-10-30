"""Fake Home Assistant for ha-backup-pull tests: websocket auth + backup/info, GET /api/backup/download/<id>.
Synthetic data only. Every request is logged so tests can check that nothing off the allowlist was asked for."""

import base64
import hashlib
import json
import struct
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = "test-token-not-real"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _refuse(self):
        self.server.log.append(("REST", self.command, self.path))
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        self._send(405, b'{"message": "not allowed in fake"}')

    do_POST = do_DELETE = do_PUT = do_PATCH = _refuse

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        self.server.log.append(("REST", "GET", self.path))
        if u.path == "/api/websocket":
            return self._websocket()
        if self.headers.get("Authorization") != "Bearer " + TOKEN:
            return self._send(401, b'{"message": "unauthorized"}')
        if u.path.startswith("/api/backup/download/"):
            bid = u.path.rsplit("/", 1)[1]
            b = next((x for x in self.server.backups if x["backup_id"] == bid), None)
            if b is None:
                return self._send(404, b'{"message": "not found"}')
            return self._send(200, b["_data"], "application/x-tar")
        return self._send(404, b'{"message": "not found"}')

    def _ws_send(self, obj):
        data = json.dumps(obj).encode()
        n = len(data)
        hdr = bytes([0x81]) + (
            bytes([n])
            if n < 126
            else bytes([126]) + struct.pack("!H", n)
            if n < 65536
            else bytes([127]) + struct.pack("!Q", n)
        )
        self.wfile.write(hdr + data)
        self.wfile.flush()

    def _ws_recv(self):
        b1, b2 = self.rfile.read(2)
        n = b2 & 0x7F
        if n == 126:
            n = struct.unpack("!H", self.rfile.read(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", self.rfile.read(8))[0]
        mask = self.rfile.read(4) if b2 & 0x80 else b"\0\0\0\0"
        return b1 & 0x0F, bytes(c ^ mask[i % 4] for i, c in enumerate(self.rfile.read(n)))

    def _websocket(self):
        key = self.headers.get("Sec-WebSocket-Key", "")
        acc = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", acc.decode())
        self.end_headers()
        self.wfile.flush()
        self._ws_send({"type": "auth_required", "ha_version": "2026.10.1"})
        while True:
            try:
                op, data = self._ws_recv()
            except (ValueError, OSError):
                return
            if op == 0x8:
                return
            if op in (0x9, 0xA):
                continue
            msg = json.loads(data)
            self.server.log.append(("WS", msg.get("type"), {k: v for k, v in msg.items() if k != "access_token"}))
            if msg.get("type") == "auth":
                ok = msg.get("access_token") == TOKEN
                self._ws_send({"type": "auth_ok" if ok else "auth_invalid"})
                if not ok:
                    return
                continue
            if msg.get("type") == "backup/info":
                pub = [{k: v for k, v in b.items() if not k.startswith("_")} for b in self.server.backups]
                res = dict(self.server.info_extra, backups=pub)
                self._ws_send({"id": msg.get("id"), "type": "result", "success": True, "result": res})
            else:
                self._ws_send(
                    {"id": msg.get("id"), "type": "result", "success": False, "error": {"code": "unknown_command"}}
                )


class FakeHA:
    def __init__(self, backups, info_extra=None):
        """backups: [{'backup_id', 'name', 'date', '_data': bytes, '_size': reported size (default len)}]"""
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.srv.daemon_threads = True
        self.srv.log = []
        self.srv.backups = [
            dict(b, agents={"hassio.local": {"size": b.get("_size", len(b["_data"])), "protected": False}})
            for b in backups
        ]
        self.srv.info_extra = info_extra or {}
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True)

    @property
    def log(self):
        return self.srv.log

    def __enter__(self):
        self.t.start()
        return self

    def __exit__(self, *a):
        self.srv.shutdown()
        self.srv.server_close()
