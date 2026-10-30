#!/usr/local/bin/python3.13 -I
"""Loopback stand-in for the Telegram Bot API, for tests/legacy/tg-status/run.sh. Never talks to Telegram.

  mock_tg.py DIR     listens on 127.0.0.1 (ephemeral port, written to DIR/port), logs every call as one JSON line
                     to DIR/calls.jsonl: {"token", "method", "params"}.
Behaviour switches (files in DIR, checked per request):
  fail-edit-notfound   editMessageText -> 400 "message to edit not found"
  fail-html            any call with parse_mode=HTML -> 400 "can't parse entities"
  fail-429-once        the next call -> 429 retry_after=1 (the file is removed)
"""

import http.server
import json
import os
import sys

DIR = sys.argv[1]
NEXT_ID = [100]


def flag(name):
    return os.path.exists(os.path.join(DIR, name))


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            params = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            params = {}
        parts = self.path.strip("/").split("/")  # bot<token>/<method>
        token = parts[0][3:] if parts and parts[0].startswith("bot") else ""
        method = parts[1] if len(parts) > 1 else ""
        with open(os.path.join(DIR, "calls.jsonl"), "a") as f:
            f.write(json.dumps({"token": token, "method": method, "params": params}) + "\n")
        if flag("fail-429-once"):
            os.remove(os.path.join(DIR, "fail-429-once"))
            return self.reply(
                429,
                {
                    "ok": False,
                    "error_code": 429,
                    "description": "Too Many Requests: retry after 1",
                    "parameters": {"retry_after": 1},
                },
            )
        if flag("fail-html") and params.get("parse_mode") == "HTML":
            return self.reply(
                400,
                {
                    "ok": False,
                    "error_code": 400,
                    "description": "Bad Request: can't parse entities: unexpected end tag",
                },
            )
        if method == "editMessageText" and flag("fail-edit-notfound"):
            return self.reply(
                400, {"ok": False, "error_code": 400, "description": "Bad Request: message to edit not found"}
            )
        if method == "sendMessage":
            NEXT_ID[0] += 1
            return self.reply(200, {"ok": True, "result": {"message_id": NEXT_ID[0]}})
        if method == "editMessageText":
            return self.reply(200, {"ok": True, "result": {"message_id": params.get("message_id")}})
        return self.reply(200, {"ok": True, "result": True})


srv = http.server.HTTPServer(("127.0.0.1", 0), H)
with open(os.path.join(DIR, "port.tmp"), "w") as f:
    f.write(str(srv.server_address[1]))
os.replace(os.path.join(DIR, "port.tmp"), os.path.join(DIR, "port"))
srv.serve_forever()
