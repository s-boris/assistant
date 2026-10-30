"""Fake Deezer API for trackinfo tests: /search, /track/ID, /preview/ID. Fictional tracks only.
argv: DIR. Reads DIR/ctrl.json {"tracks": [{id, artist, title, bpm, file}]} per request, logs every request
as "MONOTONIC PATH" to DIR/server.log and writes its port to DIR/port."""

import json
import os
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

D = sys.argv[1]


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        with open(os.path.join(D, "server.log"), "a") as fh:
            fh.write(f"{time.monotonic():.3f} {urllib.parse.unquote(self.path)}\n")
        with open(os.path.join(D, "ctrl.json")) as fh:
            tracks = json.load(fh)["tracks"]
        base = f"http://127.0.0.1:{self.server.server_port}"
        parts = [p for p in u.path.split("/") if p]
        if parts == ["search"]:
            q = urllib.parse.parse_qs(u.query).get("q", [""])[0].lower()
            hits = [t for t in tracks if t["artist"].lower() in q and t["title"].lower().split(" (")[0] in q]
            data = [
                {
                    "id": t["id"],
                    "title": t["title"],
                    "title_short": t["title"].split(" (")[0],
                    "artist": {"name": t["artist"]},
                    "preview": f"{base}/preview/{t['id']}",
                }
                for t in hits
            ]
            return self.send(200, {"data": data, "total": len(data)})
        if len(parts) == 2 and parts[0] == "track":
            t = next((t for t in tracks if str(t["id"]) == parts[1]), None)
            if not t:
                return self.send(200, {"error": {"type": "DataException", "message": "no data", "code": 800}})
            return self.send(
                200, {"id": t["id"], "bpm": t.get("bpm", 0), "gain": -8.5, "preview": f"{base}/preview/{t['id']}"}
            )
        if len(parts) == 2 and parts[0] == "preview":
            t = next((t for t in tracks if str(t["id"]) == parts[1]), None)
            if t:
                with open(os.path.join(D, t["file"]), "rb") as fh:
                    return self.send(200, fh.read(), "audio/wav")
        return self.send(404, b"not found", "text/plain")


srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
with open(os.path.join(D, "port.tmp"), "w") as fh:
    fh.write(str(srv.server_port))
os.replace(os.path.join(D, "port.tmp"), os.path.join(D, "port"))
srv.serve_forever()
