"""Local fixture HTTP server for watch tests: http.server over a temp dir, plus
/code/NNN (that status; 429 adds Retry-After: 7200) and /slow/<path> (2.5 s delay, then the file).
Logs every request (path, status, User-Agent, If-Modified-Since) to LOGFILE. Writes its port to PORTFILE."""
import functools, http.server, sys, time

ROOT, PORTFILE, LOGFILE = sys.argv[1], sys.argv[2], sys.argv[3]


class H(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *a):
        code = a[1] if len(a) > 1 else '?'
        with open(LOGFILE, 'a') as f:
            f.write(f"{self.path.split('?')[0]} {code} UA={self.headers.get('User-Agent')} "
                    f"IMS={self.headers.get('If-Modified-Since')}\n")

    def do_GET(self):
        path = self.path.split('?')[0]
        if path.startswith('/code/'):
            code = int(path.split('/')[2])
            self.send_response(code)
            if code == 429:
                self.send_header('Retry-After', '7200')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if path.startswith('/slow/'):
            time.sleep(2.5)
            self.path = self.path[len('/slow'):]
        return super().do_GET()


srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(H, directory=ROOT))
with open(PORTFILE, 'w') as f:
    f.write(str(srv.server_address[1]))
srv.serve_forever()
