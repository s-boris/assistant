"""core.otlp: OTLP traces without dependencies: a privacy allowlist, OpenInference hints, protobuf encoding and a
local emit. Stdlib only.

  sanitize(body) -> (batches, dropped)
        body = one OTLP/JSON ExportTraceServiceRequest (dict). batches = [{"resource": {k: v}, "scopes":
        [{"name", "version", "spans": [span, ...]}]}], span = {"trace_id", "span_id", "parent_id" (bytes or None),
        "name", "kind", "start_ns", "end_ns", "status" (0 unset, 1 ok, 2 error), "attrs": {k: v}}.
        dropped = {"<span name> <key>": count} for every attribute left out (key names only, never values).
  encode(batches, resource=None) -> bytes
        protobuf ExportTraceServiceRequest (opentelemetry-proto v1); `resource` adds attributes to every resource
        (e.g. {"openinference.project.name": "brain"}).
  count(batches) -> int                     spans in batches
  make_span(name, trace_id, parent_id=None, start=None, end=None, attrs=None, error=False, span_id=None)
        -> one OTLP/JSON span dict (hex ids, nanosecond strings)
  emit(spans, service, endpoint=None, timeout=0.3) -> bool
        POST them as OTLP/JSON to the local receiver (`traces otel serve`); never raises, False when it is not there.
        Off when $ASSISTANT_OTLP is "off", or in a test (ASSISTANT_TEST=1, or a pytest run: PYTEST_CURRENT_TEST, which
        its subprocesses inherit too) unless $ASSISTANT_OTLP_ENDPOINT is set: a test's spans never reach the live
        receiver (a fixed test traceparent would otherwise pile every run into one long trace in Phoenix).
  parse_traceparent(s) -> (trace_id hex, span_id hex) | None

What may leave the box: span names, ids, times, status codes, numbers and booleans under plain attribute names,
and strings only for the keys in KEEP_STR when they are token-shaped (a model, a tool name, a program name, a
category; at most 120 characters of [A-Za-z0-9_.:/@+[]-], no spaces or "="). Everything else is dropped: prompts,
responses, tool input and output, file paths, full commands, error messages (Claude Code's `error`), status
messages, span events and links, user, account and organization ids, e-mail addresses, response and conversation
ids. Numbers whose key names a person or account (DENY_RE) are dropped too.
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import struct
import time
import urllib.request
from typing import Any

from core import http

__all__ = [
    "DEFAULT_ENDPOINT",
    "KEEP_STR",
    "count",
    "emit",
    "encode",
    "make_span",
    "parse_traceparent",
    "sanitize",
]

DEFAULT_ENDPOINT = "http://127.0.0.1:4318"

# String attributes that may be exported (token-shaped values only). Claude Code's spans and events, the gateway's
# spans, and the OpenInference keys this module adds.
KEEP_STR = frozenset(
    {
        # resource
        "service.name",
        "service.version",
        "openinference.project.name",
        # Claude Code (ids and categories)
        "session.id",
        "prompt.id",
        "agent_id",
        "agent.name",
        "parent_agent_id",
        "request_id",
        "model",
        "query_source",
        "query_source_safe",
        "effort",
        "stop_reason",
        "speed",
        "llm_request.context",
        "tool_name",
        "tool_name_safe",
        "tool_use_id",
        "bash_argv0",
        "bash_command_class",
        "subagent_type",
        "skill_name",
        "mcp_server_name",
        "decision",
        "source",
        "hook_event",
        "hook_name",
        "error_type",
        "error_class",
        "error.type",
        "status_code",
        "terminal.type",
        "app.version",
        "span.type",
        "os.type",
        "host.arch",
        # OTel GenAI semantic conventions (names and categories only)
        "gen_ai.operation.name",
        "gen_ai.provider.name",
        "gen_ai.system",
        "gen_ai.request.model",
        "gen_ai.tool.name",
        # the gateway
        "gateway.tool",
        "gateway.domain",
        "gateway.surface",
        "gateway.code",
        "gateway.trace_src",
        "gateway.actor",
        # OpenInference (added here)
        "openinference.span.kind",
        "llm.model_name",
        "llm.provider",
        "llm.system",
        "tool.name",
    }
)
NUM_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z0-9_]+){0,4}$")
DENY_RE = re.compile(r"(?:^|[._])(?:user|users|email|account|organization|org|customer|phone|address|ip)(?:$|[._])")
TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+\[\]-]{0,119}$")
TRACEPARENT_RE = re.compile(r"^[0-9a-f]{2}-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$")
KIND = {
    "SPAN_KIND_UNSPECIFIED": 0,
    "SPAN_KIND_INTERNAL": 1,
    "SPAN_KIND_SERVER": 2,
    "SPAN_KIND_CLIENT": 3,
    "SPAN_KIND_PRODUCER": 4,
    "SPAN_KIND_CONSUMER": 5,
}
STATUS = {"STATUS_CODE_UNSET": 0, "STATUS_CODE_OK": 1, "STATUS_CODE_ERROR": 2}
OI_KIND = {  # span name -> OpenInference span kind (Phoenix's icons, filters and LLM token columns)
    "claude_code.interaction": "AGENT",
    "claude_code.llm_request": "LLM",
    "claude_code.tool": "TOOL",
}


# ------------------------------------------------------------------------------------------------- sanitize
def _value(v: Any) -> Any:
    """The scalar inside an OTLP/JSON AnyValue; None for arrays, maps, bytes and anything odd."""
    if not isinstance(v, dict):
        return None
    if "stringValue" in v:
        return v["stringValue"] if isinstance(v["stringValue"], str) else None
    if "boolValue" in v:
        return bool(v["boolValue"])
    if "intValue" in v:
        try:
            return int(v["intValue"])
        except (TypeError, ValueError):
            return None
    if "doubleValue" in v:
        try:
            return float(v["doubleValue"])
        except (TypeError, ValueError):
            return None
    return None


def _keep(key: str, val: Any) -> bool:
    if val is None:
        return False
    if isinstance(val, str):
        return key in KEEP_STR and bool(TOKEN_RE.match(val))
    if isinstance(val, bool | int | float):
        if isinstance(val, float) and val != val:  # NaN
            return False
        return bool(NUM_KEY_RE.match(key)) and not DENY_RE.search(key)
    return False


def _attrs(lst: Any, where: str, dropped: dict[str, int]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for kv in lst if isinstance(lst, list) else []:
        if not isinstance(kv, dict):
            continue
        key = kv.get("key")
        if not isinstance(key, str):
            continue
        val = _value(kv.get("value"))
        if _keep(key, val):
            out[key] = val
        else:
            k = f"{where} {key[:60] if NUM_KEY_RE.match(key) else '<odd key>'}"
            dropped[k] = dropped.get(k, 0) + 1
    return out


def _token(v: Any) -> str:
    """v when it is a token-shaped string, else ""."""
    return v if isinstance(v, str) and TOKEN_RE.match(v) else ""


def _id(v: Any, n: int) -> bytes | None:
    """An OTLP/JSON id: hex (the spec) or base64 (protobuf-JSON mapping); None unless exactly n bytes, not all 0."""
    if not isinstance(v, str) or not v:
        return None
    b: bytes | None = None
    if len(v) == 2 * n:
        try:
            b = bytes.fromhex(v)
        except ValueError:
            b = None
    if b is None:
        try:
            b = base64.b64decode(v, validate=True)
        except (ValueError, TypeError):
            return None
    return b if len(b) == n and any(b) else None


def _int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _enrich(name: str, a: dict[str, Any]) -> None:
    """OpenInference attributes Phoenix understands, derived from kept numbers and tokens only."""
    kind = OI_KIND.get(name) or ("TOOL" if name.startswith("gateway.") else "CHAIN")
    a.setdefault("openinference.span.kind", kind)
    if kind == "LLM":
        if isinstance(a.get("model"), str):
            a["llm.model_name"] = a["model"]
        a["llm.provider"] = a["llm.system"] = "anthropic"
        inp, out = a.get("input_tokens"), a.get("output_tokens")
        cr, cw = a.get("cache_read_tokens"), a.get("cache_creation_tokens")
        if any(isinstance(x, int) for x in (inp, out, cr, cw)):
            prompt = sum(x for x in (inp, cr, cw) if isinstance(x, int))
            a["llm.token_count.prompt"] = prompt
            a["llm.token_count.completion"] = out if isinstance(out, int) else 0
            a["llm.token_count.total"] = prompt + (out if isinstance(out, int) else 0)
            if isinstance(cr, int):
                a["llm.token_count.prompt_details.cache_read"] = cr
            if isinstance(cw, int):
                a["llm.token_count.prompt_details.cache_write"] = cw
    elif kind == "TOOL":
        t = a.get("tool_name") or a.get("tool_name_safe") or a.get("gateway.tool")
        if isinstance(t, str):
            a["tool.name"] = t


def sanitize(body: Any) -> tuple[list[dict[str, Any]], dict[str, int]]:
    batches: list[dict[str, Any]] = []
    dropped: dict[str, int] = {}
    for rs in (body or {}).get("resourceSpans") or [] if isinstance(body, dict) else []:
        if not isinstance(rs, dict):
            continue
        res = _attrs((rs.get("resource") or {}).get("attributes"), "resource", dropped)
        scopes = []
        for ss in rs.get("scopeSpans") or []:
            if not isinstance(ss, dict):
                continue
            sc = ss.get("scope") or {}
            spans = []
            for sp in ss.get("spans") or []:
                if not isinstance(sp, dict):
                    continue
                tid, sid = _id(sp.get("traceId"), 16), _id(sp.get("spanId"), 8)
                if tid is None or sid is None:
                    dropped["<span without ids>"] = dropped.get("<span without ids>", 0) + 1
                    continue
                name = _token(sp.get("name")) or "span"
                a = _attrs(sp.get("attributes"), name, dropped)
                for k in ("events", "links"):
                    if sp.get(k):
                        dk = f"{name} <{k}>"
                        dropped[dk] = dropped.get(dk, 0) + (len(sp[k]) if isinstance(sp[k], list) else 1)
                _enrich(name, a)
                kind = sp.get("kind", 1)
                kind = KIND.get(kind, 1) if isinstance(kind, str) else _int(kind)
                st = sp.get("status") or {}
                code = st.get("code", 0) if isinstance(st, dict) else 0
                code = STATUS.get(code, 0) if isinstance(code, str) else _int(code)
                spans.append(
                    {
                        "trace_id": tid,
                        "span_id": sid,
                        "parent_id": _id(sp.get("parentSpanId"), 8),
                        "name": name,
                        "kind": kind if 0 <= kind <= 5 else 1,
                        "start_ns": _int(sp.get("startTimeUnixNano")),
                        "end_ns": _int(sp.get("endTimeUnixNano")),
                        "status": code if code in (0, 1, 2) else 0,
                        "attrs": a,
                    }
                )
            if spans:
                sc = sc if isinstance(sc, dict) else {}
                scopes.append(
                    {
                        "name": _token(sc.get("name")),
                        "version": _token(sc.get("version")),
                        "spans": spans,
                    }
                )
        if scopes:
            batches.append({"resource": res, "scopes": scopes})
    return batches, dropped


def count(batches: list[dict[str, Any]]) -> int:
    return sum(len(s["spans"]) for b in batches for s in b["scopes"])


# ------------------------------------------------------------------------------------------------- protobuf
def _varint(n: int) -> bytes:
    n &= (1 << 64) - 1  # int64 two's complement
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _tag(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def _ld(field: int, payload: bytes) -> bytes:
    return _tag(field, 2) + _varint(len(payload)) + payload


def _str(field: int, s: str) -> bytes:
    return _ld(field, s.encode("utf-8")) if s else b""


def _anyvalue(v: Any) -> bytes:
    if isinstance(v, bool):
        return _tag(2, 0) + _varint(1 if v else 0)
    if isinstance(v, int):
        return _tag(3, 0) + _varint(v)
    if isinstance(v, float):
        return _tag(4, 1) + struct.pack("<d", v)
    return _ld(1, str(v).encode("utf-8"))


def _kvs(field: int, attrs: dict[str, Any]) -> bytes:
    return b"".join(_ld(field, _str(1, k) + _ld(2, _anyvalue(v))) for k, v in attrs.items())


def _span(sp: dict[str, Any]) -> bytes:
    out = _ld(1, sp["trace_id"]) + _ld(2, sp["span_id"])
    if sp.get("parent_id"):
        out += _ld(4, sp["parent_id"])
    out += _str(5, sp["name"])
    out += _tag(6, 0) + _varint(sp["kind"])
    out += _tag(7, 1) + struct.pack("<Q", max(0, sp["start_ns"]))
    out += _tag(8, 1) + struct.pack("<Q", max(0, sp["end_ns"]))
    out += _kvs(9, sp["attrs"])
    if sp.get("status"):
        out += _ld(15, _tag(3, 0) + _varint(sp["status"]))
    return out


def encode(batches: list[dict[str, Any]], resource: dict[str, Any] | None = None) -> bytes:
    out = b""
    for b in batches:
        res = dict(b["resource"], **(resource or {}))
        rs = _ld(1, _kvs(1, res))
        for sc in b["scopes"]:
            scope = _str(1, sc.get("name") or "") + _str(2, sc.get("version") or "")
            rs += _ld(2, _ld(1, scope) + b"".join(_ld(2, _span(sp)) for sp in sc["spans"]))
        out += _ld(1, rs)
    return out


# ------------------------------------------------------------------------------------------------- emit
def parse_traceparent(s: Any) -> tuple[str, str] | None:
    m = TRACEPARENT_RE.match(str(s or "").strip().lower())
    if not m or m[1] == "0" * 32 or m[2] == "0" * 16:
        return None
    return m[1], m[2]


def _kv(k: str, v: Any) -> dict[str, Any]:
    if isinstance(v, bool):
        val: dict[str, Any] = {"boolValue": v}
    elif isinstance(v, int):
        val = {"intValue": str(v)}
    elif isinstance(v, float):
        val = {"doubleValue": v}
    else:
        val = {"stringValue": str(v)}
    return {"key": k, "value": val}


def make_span(
    name: str,
    trace_id: str,
    parent_id: str | None = None,
    start: float | None = None,
    end: float | None = None,
    attrs: dict[str, Any] | None = None,
    error: bool = False,
    span_id: str | None = None,
) -> dict[str, Any]:
    end = time.time() if end is None else end
    start = end if start is None else start
    sp: dict[str, Any] = {
        "traceId": trace_id,
        "spanId": span_id or secrets.token_hex(8),
        "name": name,
        "kind": 1,
        "startTimeUnixNano": str(int(start * 1e9)),
        "endTimeUnixNano": str(int(end * 1e9)),
        "attributes": [_kv(k, v) for k, v in (attrs or {}).items() if v is not None],
        "status": {"code": 2 if error else 1},
    }
    if parent_id:
        sp["parentSpanId"] = parent_id
    return sp


def emit(spans: list[dict[str, Any]], service: str, endpoint: str | None = None, timeout: float = 0.3) -> bool:
    if os.environ.get("ASSISTANT_OTLP", "").lower() == "off" or not spans:
        return False
    ep = endpoint or os.environ.get("ASSISTANT_OTLP_ENDPOINT")
    if not ep:
        if os.environ.get("ASSISTANT_TEST") == "1" or os.environ.get("PYTEST_CURRENT_TEST"):
            return False
        ep = DEFAULT_ENDPOINT
    body = {
        "resourceSpans": [
            {
                "resource": {"attributes": [_kv("service.name", service)]},
                "scopeSpans": [{"scope": {"name": service}, "spans": spans}],
            }
        ]
    }
    req = urllib.request.Request(
        ep.rstrip("/") + "/v1/traces",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with http.open(req, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception:
        return False
