"""Mock Telegram Bot API for tg-voice tests (127.0.0.1 only, fake token).

  python3 -I mock_bot.py PORT_FILE LOG_FILE MODE_FILE

Logs one JSON line per request: {path, fields, files: {name: {filename, type, size}}}.
MODE_FILE (re-read per request): ok = every call succeeds; forbid = sendVoice answers
VOICE_MESSAGES_FORBIDDEN, other methods succeed; err = 400 "chat not found".
"""

import email.parser
import email.policy
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

port_file, log_file, mode_file = sys.argv[1:4]


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        ctype = self.headers.get("Content-Type", "")
        fields, files = {}, {}
        if ctype.startswith("multipart/"):
            msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
                b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + body
            )
            for part in msg.iter_parts():
                name = part.get_param("name", header="content-disposition")
                data = part.get_payload(decode=True) or b""
                if part.get_filename():
                    files[name] = {"filename": part.get_filename(), "type": part.get_content_type(), "size": len(data)}
                else:
                    fields[name] = data.decode()
        with open(log_file, "a") as f:
            f.write(json.dumps({"path": self.path, "fields": fields, "files": files}) + "\n")
        mode = open(mode_file).read().strip()
        method = self.path.rsplit("/", 1)[-1]
        if mode == "err" or (mode == "forbid" and method == "sendVoice"):
            desc = "Bad Request: chat not found" if mode == "err" else "Bad Request: VOICE_MESSAGES_FORBIDDEN"
            code, out = 400, {"ok": False, "error_code": 400, "description": desc}
        else:
            code, out = 200, {"ok": True, "result": {"message_id": 4242}}
        data = json.dumps(out).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
with open(port_file, "w") as f:
    f.write(str(srv.server_address[1]))
srv.serve_forever()
