"""Fixture HTTP server for deals tests (no real network).
GET /alt/<rest> and /mm/<rest> (query included) -> ROOT/<alt|mm>/<percent-quoted "/<rest>">, the names DEALS_SAVE_RAW
writes; any other path -> ROOT/<path> (robots.txt, plain pages); /captcha/* -> 403 with ROOT/captcha.html (a real
DataDome bot wall); /code/NNN -> that status. Logs "<path+query> <status> UA=<User-Agent>" per request to LOGFILE and
writes its port to PORTFILE."""

import http.server
import os
import sys
import urllib.parse

ROOT, PORTFILE, LOGFILE = sys.argv[1], sys.argv[2], sys.argv[3]


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        with open(LOGFILE, "a") as f:
            f.write(f"{self.path} {code} UA={self.headers.get('User-Agent')}\n")

    def do_GET(self):
        path = self.path
        if path.startswith("/captcha/"):
            with open(os.path.join(ROOT, "captcha.html"), "rb") as f:
                return self.send(403, f.read())
        if path.startswith("/code/"):
            return self.send(int(path.split("/")[2]), b"")
        site, _, rest = path.lstrip("/").partition("/")
        cands = [os.path.join(ROOT, site, urllib.parse.quote("/" + rest, safe=""))] if site in ("alt", "mm") else []
        cands.append(os.path.join(ROOT, urllib.parse.urlsplit(path).path.lstrip("/")))
        for c in cands:
            if os.path.isfile(c):
                with open(c, "rb") as f:
                    return self.send(200, f.read(), "text/plain" if c.endswith(".txt") else "text/html; charset=utf-8")
        return self.send(404, b"not found")


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
with open(PORTFILE, "w") as f:
    f.write(str(srv.server_address[1]))
srv.serve_forever()
