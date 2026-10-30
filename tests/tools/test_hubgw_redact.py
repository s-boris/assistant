"""The hub gateway masks hub-held secret values in every result (data, text, errors) and fails closed."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import secrets
from pathlib import Path
from typing import Any

from tools import hubgw
from tools.core import Domain, Registry, ToolInput, ToolOutput, tool

REPO = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("hubredact_gwtest", REPO / "packages" / "hub" / "hubredact.py")
assert SPEC and SPEC.loader
hr = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hr)
TOKENS = {"brain": b"B" * 40}


class In(ToolInput):
    x: int = 0


class Out(ToolOutput):
    n: int = 0


def reg() -> Registry:
    r = Registry()
    r.add_domain(Domain("e", "E2E", "synthetic", "test"))

    def look(a: In) -> Out:
        return Out(n=1)

    tool("e.look", effect="read", registry=r)(look)
    return r


def _gw(tmp_path: Path, redact: Any, answer: dict[str, Any]) -> hubgw.Gateway:
    gw = hubgw.Gateway(TOKENS, tmp_path / "gw", reg(), redact=redact)

    async def run(payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        return answer

    gw.exec.run = run  # type: ignore[method-assign]
    return gw


def test_results_and_errors_are_masked(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSISTANT_OTLP", "off")
    v = secrets.token_urlsafe(30)
    r = hr.Redactor(hr.Store.from_texts({"ha_token": v}))
    gw = _gw(tmp_path, r.obj, {"ok": True, "text": f"token {v}", "data": {"k": [v]}, "ms": 3})
    res = asyncio.run(gw.call("e.look", {"x": 1}, caller="brain"))
    assert res["ok"] and v not in json.dumps(res) and "[redacted:ha_token]" in res["text"]
    h = gw.health()["redaction"]
    assert h["on"] and h["results"] == 1 and h["hits"] == 2 and h["last_ts"] and "ha_token" not in json.dumps(h)
    assert gw.redacted["last"][-1]["names"] == ["ha_token"]  # names: /gw/policy (brain) only
    gw = _gw(tmp_path, r.obj, {"ok": False, "error": {"code": "upstream", "message": f"401 for {v}", "hint": ""}})
    res = asyncio.run(gw.call("e.look", {}, caller="brain"))
    assert res["code"] == "upstream" and v not in json.dumps(res)


def test_a_failing_redactor_answers_internal_never_the_result(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSISTANT_OTLP", "off")
    v = secrets.token_urlsafe(30)

    def broken(o: Any) -> tuple[Any, list[str]]:
        raise RuntimeError("boom")

    gw = _gw(tmp_path, broken, {"ok": True, "text": v, "ms": 1})
    res = asyncio.run(gw.call("e.look", {}, caller="brain"))
    assert res["code"] == "internal" and v not in json.dumps(res) and gw.health()["redaction"]["failed"] == 1


def test_no_redactor_leaves_results_as_they_were(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSISTANT_OTLP", "off")
    gw = _gw(tmp_path, None, {"ok": True, "text": "plain", "data": {"n": 1}, "ms": 1})
    res = asyncio.run(gw.call("e.look", {}, caller="brain"))
    assert res["text"] == "plain" and res["data"] == {"n": 1} and gw.health()["redaction"]["on"] is False
