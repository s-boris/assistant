import http.server
import json
import threading
import urllib.error
import urllib.request

import pytest

from core import http as web


class Handler(http.server.BaseHTTPRequestHandler):
    hits: list = []
    plan: list = []  # per request: (status, headers, body)

    def log_message(self, *a):
        pass

    def _serve(self):
        n = self.headers.get("Content-Length")
        body_in = self.rfile.read(int(n)) if n else b""
        Handler.hits.append(
            {
                "path": self.path,
                "ua": self.headers.get("User-Agent"),
                "method": self.command,
                "x": self.headers.get("X-Test"),
                "body": body_in,
                "auth": self.headers.get("Authorization"),
            }
        )
        status, headers, body = Handler.plan.pop(0) if Handler.plan else (200, {}, b'{"ok": true}')
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _serve
    do_POST = _serve


@pytest.fixture
def server(monkeypatch):
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(k, raising=False)
    Handler.hits, Handler.plan = [], []
    s = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=s.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{s.server_address[1]}"
    s.shutdown()
    s.server_close()


def test_default_ua_and_json(server):
    assert web.get_json(server + "/a") == {"ok": True}
    assert Handler.hits[-1]["ua"] == web.DEFAULT_UA
    assert web.get_json(server + "/b", ua="mine/1") == {"ok": True}
    assert Handler.hits[-1]["ua"] == "mine/1"
    assert web.get_text(server + "/c", headers={"User-Agent": "hdr/2", "X-Test": "1"}) == '{"ok": true}'
    assert Handler.hits[-1]["ua"] == "hdr/2" and Handler.hits[-1]["x"] == "1"


def test_request_object_headers_kept(server):
    req = urllib.request.Request(server + "/r", headers={"User-Agent": "req/3"})
    with web.open(req, timeout=5) as r:
        assert json.load(r) == {"ok": True}
    assert Handler.hits[-1]["ua"] == "req/3"
    assert web.get_bytes(server + "/p", data=b"x=1", max_bytes=4) == b'{"ok'
    assert Handler.hits[-1]["method"] == "POST" and Handler.hits[-1]["body"] == b"x=1"


def test_http_error_unchanged_without_retries(server):
    Handler.plan = [(404, {}, b'{"errors": [{"message": "no such stop"}]}')]
    with pytest.raises(urllib.error.HTTPError) as ei:
        web.open(server + "/missing")
    assert ei.value.code == 404 and json.load(ei.value)["errors"][0]["message"] == "no such stop"
    assert len(Handler.hits) == 1


def test_retries_on_503_and_retry_after(server, monkeypatch):
    waits = []
    monkeypatch.setattr(web.time, "sleep", waits.append)
    Handler.plan = [(503, {}, b"busy"), (429, {"Retry-After": "7"}, b"slow"), (200, {}, b'"done"')]
    assert web.get_json(server + "/x", retries=2, backoff=0.5) == "done"
    assert len(Handler.hits) == 3 and waits == [0.5, 7.0]


def test_no_retry_on_4xx_and_gives_up(server, monkeypatch):
    monkeypatch.setattr(web.time, "sleep", lambda s: None)
    Handler.plan = [(403, {}, b"no")]
    with pytest.raises(urllib.error.HTTPError):
        web.open(server + "/f", retries=3)
    assert len(Handler.hits) == 1
    Handler.hits.clear()
    Handler.plan = [(500, {}, b"")] * 3
    with pytest.raises(urllib.error.HTTPError) as ei:
        web.open(server + "/g", retries=2)
    assert ei.value.code == 500 and len(Handler.hits) == 3


def test_connection_error_retried_then_raised(monkeypatch):
    for k in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(k, raising=False)
    waits = []
    monkeypatch.setattr(web.time, "sleep", waits.append)
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens there now
    with pytest.raises(urllib.error.URLError):
        web.open(f"http://127.0.0.1:{port}/", retries=2, backoff=1, timeout=2)
    assert waits == [1, 2]


def test_call_json_token_file_body_and_errors(server, tmp_path):
    tf = tmp_path / "tok"
    tf.write_text("s3cret\n")
    Handler.plan = [(202, {}, b'{"queued": 1}'), (401, {}, b'{"error": "unauthorized"}'), (500, {}, b"not json")]
    assert web.call_json("POST", server + "/v", {"a": 1}, token_file=tf) == (202, {"queued": 1})
    h = Handler.hits[-1]
    assert h["method"] == "POST" and json.loads(h["body"]) == {"a": 1} and h["auth"] == "Bearer s3cret"
    assert web.call_json("GET", server + "/s", token="t") == (401, {"error": "unauthorized"})
    assert Handler.hits[-1]["auth"] == "Bearer t" and Handler.hits[-1]["body"] == b""
    assert web.call_json("GET", server + "/x") == (500, None)  # not JSON: the status alone, never an exception
    assert web.call_json("GET", server + "/y", token_file=tmp_path / "missing") == (0, None)
    assert len(Handler.hits) == 3  # an unreadable token file sends nothing
    assert web.call_json("GET", "http://127.0.0.1:9/") == (0, None)  # unreachable
