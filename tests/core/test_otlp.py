import http.server
import json
import struct
import threading

from core import otlp

SECRET = "SECRET-BODY-TEXT with spaces"
TID = "0af7651916cd43dd8448eb211c80319c"
SID = "b7ad6b7169203331"
PID = "00f067aa0ba902b7"


def kv(k, v):
    if isinstance(v, bool):
        return {"key": k, "value": {"boolValue": v}}
    if isinstance(v, int):
        return {"key": k, "value": {"intValue": str(v)}}
    if isinstance(v, float):
        return {"key": k, "value": {"doubleValue": v}}
    return {"key": k, "value": {"stringValue": v}}


def body(spans, res=None):
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": res or [kv("service.name", "claude-code"), kv("user.email", "a@b.invalid")]},
                "scopeSpans": [{"scope": {"name": "com.anthropic.claude_code", "version": "2.1.0"}, "spans": spans}],
            }
        ]
    }


def llm_span(**extra):
    sp = {
        "traceId": TID,
        "spanId": SID,
        "parentSpanId": PID,
        "name": "claude_code.llm_request",
        "kind": 1,
        "startTimeUnixNano": "1700000000000000000",
        "endTimeUnixNano": "1700000002000000000",
        "attributes": [
            kv("model", "claude-opus-5-5[1m]"),
            kv("input_tokens", 10),
            kv("output_tokens", 5),
            kv("cache_read_tokens", 1000),
            kv("cache_creation_tokens", 20),
            kv("ttft_ms", 800),
            kv("success", True),
            kv("session.id", "11111111-2222-3333-4444-555555555555"),
            kv("user_prompt", SECRET),
            kv("error", "boom: " + SECRET),
            kv("user.id", 12345),
            kv("file_path", "/home/x/notes.txt"),
            kv("full_command", "cat secret"),
            kv("tool_name", SECRET),  # a kept key with a non-token value is dropped too
            kv("bash_argv0", "TOKEN=abc"),  # "=" is not token-shaped
            kv("effort", "high"),
            kv("organization.id", "org-1"),
            kv("user.account_uuid", "u-1"),
            kv("gen_ai.response.id", "msg_1"),
        ],
        "events": [{"name": "exception", "attributes": [kv("exception.message", SECRET)]}],
        "status": {"code": 2, "message": SECRET},
    }
    sp.update(extra)
    return sp


# ------------------------------------------------------------------------------------------------- a tiny decoder
def _varint(b, i):
    n = s = 0
    while True:
        x = b[i]
        i += 1
        n |= (x & 0x7F) << s
        s += 7
        if not x & 0x80:
            return n, i


def fields(b):
    i, out = 0, []
    while i < len(b):
        tag, i = _varint(b, i)
        f, w = tag >> 3, tag & 7
        if w == 0:
            v, i = _varint(b, i)
        elif w == 1:
            v, i = b[i : i + 8], i + 8
        elif w == 5:
            v, i = b[i : i + 4], i + 4
        else:
            n, i = _varint(b, i)
            v, i = b[i : i + n], i + n
        out.append((f, w, v))
    return out


def one(b, f):
    return [v for ff, _, v in fields(b) if ff == f]


def attrs(b, f):
    out = {}
    for kvb in one(b, f):
        k = one(kvb, 1)[0].decode()
        av = fields(one(kvb, 2)[0])[0]
        if av[0] == 1:
            out[k] = av[2].decode()
        elif av[0] == 2:
            out[k] = bool(av[2])
        elif av[0] == 3:
            out[k] = av[2] - (1 << 64) if av[2] >= 1 << 63 else av[2]
        elif av[0] == 4:
            out[k] = struct.unpack("<d", av[2])[0]
    return out


# ------------------------------------------------------------------------------------------------- tests
def test_sanitize_keeps_ids_numbers_tokens_only():
    batches, dropped = otlp.sanitize(body([llm_span()]))
    assert otlp.count(batches) == 1
    sp = batches[0]["scopes"][0]["spans"][0]
    assert sp["trace_id"] == bytes.fromhex(TID) and sp["span_id"] == bytes.fromhex(SID)
    assert sp["parent_id"] == bytes.fromhex(PID) and sp["status"] == 2
    a = sp["attrs"]
    assert a["model"] == "claude-opus-5-5[1m]" and a["ttft_ms"] == 800 and a["success"] is True
    assert a["openinference.span.kind"] == "LLM" and a["llm.model_name"] == "claude-opus-5-5[1m]"
    assert a["llm.token_count.prompt"] == 1030 and a["llm.token_count.total"] == 1035
    assert a["llm.token_count.prompt_details.cache_read"] == 1000
    assert a["effort"] == "high"
    for k in (
        "user_prompt",
        "error",
        "user.id",
        "file_path",
        "full_command",
        "tool_name",
        "bash_argv0",
        "organization.id",
        "user.account_uuid",
        "gen_ai.response.id",
    ):
        assert k not in a
    assert batches[0]["resource"] == {"service.name": "claude-code"}
    assert dropped["resource user.email"] == 1 and dropped["claude_code.llm_request user_prompt"] == 1
    assert dropped["claude_code.llm_request <events>"] == 1
    assert SECRET not in json.dumps(dropped) and SECRET not in repr(batches)


def test_encode_roundtrip_and_no_secret():
    batches, _ = otlp.sanitize(body([llm_span(), llm_span(spanId="1111111111111111", parentSpanId="")]))
    raw = otlp.encode(batches, {"openinference.project.name": "brain"})
    assert SECRET.encode() not in raw and b"a@b.invalid" not in raw and b"/home/" not in raw
    rs = one(raw, 1)
    assert len(rs) == 1
    res = attrs(one(rs[0], 1)[0], 1)
    assert res == {"service.name": "claude-code", "openinference.project.name": "brain"}
    ss = one(rs[0], 2)[0]
    assert one(one(ss, 1)[0], 1)[0] == b"com.anthropic.claude_code"
    spans = one(ss, 2)
    assert len(spans) == 2
    s0 = spans[0]
    assert one(s0, 1)[0] == bytes.fromhex(TID) and one(s0, 4)[0] == bytes.fromhex(PID)
    assert one(s0, 5)[0] == b"claude_code.llm_request"
    assert struct.unpack("<Q", one(s0, 7)[0])[0] == 1700000000000000000
    assert one(one(s0, 15)[0], 3)[0] == 2
    a = attrs(s0, 9)
    assert a["input_tokens"] == 10 and a["llm.provider"] == "anthropic"
    assert one(spans[1], 4) == []  # no parent = a root span


def test_bad_ids_names_and_shapes():
    spans = [
        llm_span(traceId="zz"),
        llm_span(traceId="0" * 32),
        llm_span(name="has space " + SECRET),
        llm_span(traceId="CvdlGRbNQ92ESOshHIAxnA==", spanId="t61rcWkgMzE=", kind="SPAN_KIND_SERVER"),
        "junk",
    ]
    batches, dropped = otlp.sanitize(body(spans))
    got = batches[0]["scopes"][0]["spans"]
    assert [s["name"] for s in got] == ["span", "claude_code.llm_request"]
    assert got[1]["trace_id"] == bytes.fromhex(TID) and got[1]["kind"] == 2
    assert dropped["<span without ids>"] == 2
    assert otlp.sanitize(None) == ([], {}) and otlp.sanitize({"resourceSpans": "x"}) == ([], {})


def test_negative_and_float_values():
    sp = llm_span(attributes=[kv("delta", -5), kv("ratio", 0.5), kv("customer.id", 7), kv("nan", float("nan"))])
    batches, dropped = otlp.sanitize(body([sp]))
    a = attrs(one(one(one(otlp.encode(batches), 1)[0], 2)[0], 2)[0], 9)
    assert a["delta"] == -5 and a["ratio"] == 0.5 and "customer.id" not in a and "nan" not in a


def test_make_span_and_traceparent():
    assert otlp.parse_traceparent(f"00-{TID}-{PID}-01") == (TID, PID)
    assert otlp.parse_traceparent(f"00-{'0' * 32}-{PID}-01") is None
    assert otlp.parse_traceparent("junk") is None and otlp.parse_traceparent(None) is None
    sp = otlp.make_span("gateway.reminders.list", TID, PID, 10.0, 10.5, {"gateway.code": "ok", "x": None})
    batches, dropped = otlp.sanitize(body([sp]))
    s = batches[0]["scopes"][0]["spans"][0]
    assert s["parent_id"] == bytes.fromhex(PID) and s["end_ns"] - s["start_ns"] == 500_000_000
    assert s["attrs"]["openinference.span.kind"] == "TOOL" and s["attrs"]["gateway.code"] == "ok"
    assert list(dropped) == ["resource user.email"]


def test_emit_posts_json_and_respects_switches(monkeypatch):
    hits = []

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            hits.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    ep = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        sp = otlp.make_span("gateway.x", TID)
        monkeypatch.setenv("ASSISTANT_TEST", "1")
        monkeypatch.delenv("ASSISTANT_OTLP_ENDPOINT", raising=False)
        monkeypatch.delenv("ASSISTANT_OTLP", raising=False)
        assert otlp.emit([sp], "gw") is False  # tests: off unless an endpoint is set
        monkeypatch.setenv("ASSISTANT_OTLP_ENDPOINT", ep)
        assert otlp.emit([sp], "gw") is True
        monkeypatch.setenv("ASSISTANT_OTLP", "off")
        assert otlp.emit([sp], "gw") is False
        monkeypatch.delenv("ASSISTANT_OTLP")
        assert otlp.emit([sp], "gw", endpoint="http://127.0.0.1:9") is False  # nothing there: no raise
    finally:
        srv.shutdown()
    assert len(hits) == 1 and hits[0][0] == "/v1/traces"
    b = hits[0][1]["resourceSpans"][0]
    assert b["resource"]["attributes"][0]["value"]["stringValue"] == "gw"
    assert b["scopeSpans"][0]["spans"][0]["name"] == "gateway.x"


def test_emit_is_off_inside_pytest_unless_an_endpoint_is_set(monkeypatch):
    """A plain `pytest` run (no ASSISTANT_TEST) never reaches the live receiver: the tests' fixed traceparents would
    pile every run into one long trace. PYTEST_CURRENT_TEST is pytest's own, inherited by its subprocesses."""
    hits = []

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            hits.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    ep = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        sp = otlp.make_span("gateway.x", TID)
        monkeypatch.setattr(otlp, "DEFAULT_ENDPOINT", ep)  # stands in for the live receiver
        for k in ("ASSISTANT_TEST", "ASSISTANT_OTLP", "ASSISTANT_OTLP_ENDPOINT"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/x.py::t (call)")
        assert otlp.emit([sp], "gw") is False
        monkeypatch.delenv("PYTEST_CURRENT_TEST")
        assert otlp.emit([sp], "gw") is True  # outside a test: the default endpoint
        monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/x.py::t (call)")
        monkeypatch.setenv("ASSISTANT_OTLP_ENDPOINT", ep)
        assert otlp.emit([sp], "gw") is True  # a test's own receiver
    finally:
        srv.shutdown()
    assert len(hits) == 2
