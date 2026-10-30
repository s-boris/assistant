"""One trace per request: the gateway exposes its span's traceparent to the tool function while it runs (and emits
its span under that id); workflow.job hands it to the job; the hub's job meta carries one trace per run (read as
text: hubwf needs DBOS)."""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

import pytest
from tools import gateway
from tools.core import load
from tools.domains import workflow as wf

REPO = Path(__file__).resolve().parents[2]
TP = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    (h / ".config" / "assistant").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("ASSISTANT_TEST", "1")
    monkeypatch.setenv("TOOLS_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("ASSISTANT_OTLP", "off")
    monkeypatch.delenv("TRACEPARENT", raising=False)
    return h


def test_gateway_exposes_its_span_to_the_tool(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reg = load()
    name = "traces.report"
    spec = reg.get(name)
    assert spec is not None
    seen: list[str | None] = []

    def fn(inp):  # noqa: ANN001, ANN202
        seen.append(gateway.CURRENT_TP.get())
        raise RuntimeError("stop here")  # the call reports internal; the trace context is what this test reads

    spans: list[tuple] = []
    monkeypatch.setattr(gateway, "_span", lambda *a: spans.append(a))
    monkeypatch.setitem(reg.typed, spec.name, dataclasses.replace(spec, fn=fn))
    res = gateway.call(name, {}, registry=reg, meta={"traceparent": TP})
    assert res.code == "internal" and len(seen) == 1 and seen[0] is not None
    _, tid, sid, _ = seen[0].split("-")
    assert tid == "a" * 32 == res.trace_id and len(sid) == 16
    assert spans[0][-1] == sid and spans[0][5] == "b" * 16  # the emitted span is the one handed to the tool
    assert gateway.CURRENT_TP.get() is None  # reset after the call


def test_job_env_hands_on_the_traceparent(home: Path) -> None:
    tok = gateway.CURRENT_TP.set(TP)
    try:
        env = wf._job_env("tg-status", str(home))
        assert env["TRACEPARENT"] == TP and env["HOME"] == str(home)
    finally:
        gateway.CURRENT_TP.reset(tok)
    assert "TRACEPARENT" not in wf._job_env("tg-status", str(home))  # no gateway span: nothing to hand on


def test_hub_job_meta_traces_each_run() -> None:
    src = (REPO / "packages" / "hub" / "hubwf.py").read_text()
    tree = ast.parse(src)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "job_meta")
    body = ast.unparse(fn)
    assert "HUB_JOB_TRACE" not in body and "DBOS.workflow_id" in body and "'0' * 16" in body
    run_job = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run_job")
    assert "job_meta()" in ast.unparse(run_job)
