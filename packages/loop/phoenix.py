"""loop.phoenix: Arize Phoenix (the hub's trace store) as a first-class source of the setup-health pass, and the place
its findings are written back to. Stdlib only. Numbers, span names, ids and codes only: never prompt, message, tool
input or output text (the forwarder already keeps it out of Phoenix; this module only asks for names, counts,
timings, status codes and ids).

  stats(now) -> {"names": {span name: {"n","err","p50","p95","tok"} 24 h + "base": the same per day over the 7 days
                before}, "tools": {tool: {"n","p95"} + base}, "cost": {"now","base"}, "trace_p95": {"now","base"},
                "errors": [{"name","trace_id","span_id","node"}] (examples), "slow": [{"trace_id","ms","spans"}]}
  link(trace_id, node=None) -> the Phoenix UI link of a trace (the span selected when its node id is known)
  annotate(findings) -> n        one span annotation "setup-health" per finding that carries a span (upsert by id)
  dataset_append(name, rows) -> n   rows = [{"input", "output", "meta", "span_id"}] -> a Phoenix dataset (append;
                                created on first use); span ids already sent are skipped (state)
  track_proposals(proposals, made, landed) -> lines   before/after experiments of loop proposals on the dataset
                                "loop-proposal-metrics": a "before" run when proposed, an "after" run with a
                                target-met evaluation once it landed

Config: the traces CLI's (TRACES_PHOENIX_URL / TRACES_PHOENIX_PROJECT, else ~/.config/traces/phoenix.json {"url",
"project", "ui"}; default http://claude-hub-phoenix:6006, project "brain"). "ui" is the base of report links (default
the API url). Switches: ~/.config/traces/phoenix-off (stops the forwarder) also stops reads here;
~/.config/self-review/phoenix-writes-off (or HEALTH_PHOENIX_WRITES=off) stops annotations, datasets and experiments.
Tests: ASSISTANT_TEST=1 without HEALTH_PHOENIX_URL = no network (enabled() is False); tests replace _http.
"""

from __future__ import annotations

import json
import os
import urllib.parse
from datetime import UTC, datetime
from typing import Any

from core import paths

from . import common

DEFAULT_URL = "http://claude-hub-phoenix:6006"
DEFAULT_PROJECT = "brain"
ANNOTATION = "setup-health"
DS_FAILING = "setup-health-failing-spans"
DS_SLOW = "setup-health-slow-traces"
DS_LOOP = "loop-proposal-metrics"
SAMPLE_PAGES = 12  # names: the newest 12k spans of the day (plus every error span)
MAX_NAMES = 40
CHUNK = 4  # span names per aggregate query (10 aliases each; Phoenix allows 50 per query)
STATE = "phoenix-health.json"


class PhoenixError(RuntimeError):
    pass


def _cfg() -> dict[str, Any]:
    try:
        d = json.loads((paths.home() / ".config" / "traces" / "phoenix.json").read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def url() -> str:
    return (
        os.environ.get("HEALTH_PHOENIX_URL") or os.environ.get("TRACES_PHOENIX_URL") or _cfg().get("url") or DEFAULT_URL
    ).rstrip("/")


def project() -> str:
    return os.environ.get("TRACES_PHOENIX_PROJECT") or _cfg().get("project") or DEFAULT_PROJECT


def ui() -> str:
    return (os.environ.get("HEALTH_PHOENIX_UI") or _cfg().get("ui") or url()).rstrip("/")


def enabled() -> tuple[bool, str]:
    if paths.test_mode() and not os.environ.get("HEALTH_PHOENIX_URL") and _http is _real_http:
        return False, "test mode"
    if (paths.home() / ".config" / "traces" / "phoenix-off").exists():
        return False, "phoenix-off"
    return True, ""


def writes_on() -> bool:
    if os.environ.get("HEALTH_PHOENIX_WRITES", "").lower() == "off":
        return False
    return not (paths.home() / ".config" / "self-review" / "phoenix-writes-off").exists()


# ------------------------------------------------------------------------------------------------ transport
def _real_http(
    method: str, path: str, params: dict[str, Any] | None = None, body: Any = None, timeout: float = 30
) -> Any:
    from core import http as corehttp

    q = urllib.parse.urlencode(params or {}, doseq=True)
    data = json.dumps(body).encode() if body is not None else None
    with corehttp.open(
        url() + path + ("?" + q if q else ""),
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data is not None else None,
        timeout=timeout,
    ) as r:
        raw = r.read()
    return json.loads(raw) if raw[:1] in (b"{", b"[") else raw.decode("utf-8", "replace")


_http = _real_http


def gql(query: str, var: dict[str, Any] | None = None, timeout: float = 60) -> dict[str, Any]:
    d = _http("POST", "/graphql", body={"query": query, "variables": var or {}}, timeout=timeout)
    if not isinstance(d, dict) or d.get("errors"):
        msg = (d.get("errors") or [{}])[0].get("message", "?") if isinstance(d, dict) else "not JSON"
        raise PhoenixError(f"graphql: {str(msg)[:120]}")
    return d.get("data") or {}


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _q(s: str) -> str:
    return "'" + str(s).replace("\\", "\\\\").replace("'", "\\'") + "'"


_PID: dict[str, str] = {}


def project_id() -> str:
    p = project()
    if p not in _PID:
        d = gql("{ projects { edges { node { id name } } } }")
        for e in (d.get("projects") or {}).get("edges") or []:
            _PID[e["node"]["name"]] = e["node"]["id"]
        if p not in _PID:
            raise PhoenixError(f"no project {p}")
    return _PID[p]


def link(trace_id: str | None, node: str | None = None) -> str | None:
    if not trace_id:
        return None
    try:
        pid = project_id()
    except Exception:
        return None
    u = f"{ui()}/projects/{pid}/traces/{trace_id}"
    return u + (f"?selectedSpanNodeId={urllib.parse.quote(node)}" if node else "")


# ------------------------------------------------------------------------------------------------ reads
def _spans_page(pid: str, tr: dict[str, str], after: str | None, cond: str | None, extra: str = "") -> dict[str, Any]:
    q = (
        "query($id: ID!, $tr: TimeRange, $after: String, $c: String){ node(id:$id){ ... on Project { "
        "spans(first: 1000, timeRange:$tr, after:$after, filterCondition:$c){ pageInfo { hasNextPage endCursor } "
        "edges { span: node { id name statusCode startTime context { traceId spanId } " + extra + "} } } } } }"
    )
    return gql(q, {"id": pid, "tr": tr, "after": after, "c": cond})["node"]["spans"]


def _sample(pid: str, tr: dict[str, str], cond: str | None, pages: int) -> list[dict[str, Any]]:
    out, after = [], None
    for _ in range(pages):
        sp = _spans_page(pid, tr, after, cond)
        out += [e["span"] for e in sp.get("edges") or []]
        if not (sp.get("pageInfo") or {}).get("hasNextPage"):
            break
        after = sp["pageInfo"]["endCursor"]
    return out


def _aggregate(
    pid: str, names: list[str], d1: dict[str, str], d7: dict[str, str], field: str = "name", extra_cond: str = ""
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for i in range(0, len(names), CHUNK):
        part, parts = names[i : i + CHUNK], []
        for j, n in enumerate(part):
            f = f"{field} == {_q(n)}" + (f" and {extra_cond}" if extra_cond else "")
            fe = f + " and status_code == 'ERROR'"
            for w, tr in (("a", "$d1"), ("b", "$d7")):
                c, ce = json.dumps(f), json.dumps(fe)
                parts.append(
                    f"{w}n{j}: recordCount(timeRange:{tr}, filterCondition:{c}) "
                    f"{w}e{j}: recordCount(timeRange:{tr}, filterCondition:{ce}) "
                    f"{w}m{j}: spanLatencyMsQuantile(probability:0.5, timeRange:{tr}, filterCondition:{c}) "
                    f"{w}p{j}: spanLatencyMsQuantile(probability:0.95, timeRange:{tr}, filterCondition:{c}) "
                    f"{w}t{j}: tokenCountTotal(timeRange:{tr}, filterCondition:{c})"
                )
        d = gql(
            "query($id: ID!, $d1: TimeRange, $d7: TimeRange){ node(id:$id){ ... on Project { "
            + " ".join(parts)
            + " } } }",
            {"id": pid, "d1": d1, "d7": d7},
            timeout=90,
        )["node"]
        for j, n in enumerate(part):

            def g(k: str, _d: dict[str, Any] = d) -> float:
                v = _d.get(k)
                return float(v) if isinstance(v, (int, float)) else 0.0

            out[n] = {
                "n": int(g(f"an{j}")),
                "err": int(g(f"ae{j}")),
                "p50": round(g(f"am{j}")),
                "p95": round(g(f"ap{j}")),
                "tok": int(g(f"at{j}")),
                "base": {
                    "n": round(g(f"bn{j}") / 7, 1),
                    "err": round(g(f"be{j}") / 7, 1),
                    "p50": round(g(f"bm{j}")),
                    "p95": round(g(f"bp{j}")),
                    "tok": round(g(f"bt{j}") / 7),
                    "n7": int(g(f"bn{j}")),
                },
            }
    return out


def stats(now: float | None = None) -> dict[str, Any]:
    now = now if now is not None else common.now()
    pid = project_id()
    d1 = {"start": iso(now - 86400), "end": iso(now)}
    d7 = {"start": iso(now - 8 * 86400), "end": iso(now - 86400)}
    sample = _sample(pid, d1, None, SAMPLE_PAGES)
    errs = _sample(pid, d1, "status_code == 'ERROR'", 3)
    counts: dict[str, int] = {}
    for s in sample:
        counts[s["name"]] = counts.get(s["name"], 0) + 1
    err_names = sorted({s["name"] for s in errs})
    names = err_names + [n for n, c in sorted(counts.items(), key=lambda x: -x[1]) if c >= 10 and n not in err_names]
    by_name = _aggregate(pid, names[:MAX_NAMES], d1, d7)
    examples, seen, first = [], set(), {}
    for s in errs:
        t = _ts(s.get("startTime"))
        if t and t < first.get(s["name"], float("inf")):
            first[s["name"]] = t
    for s in errs:  # newest first: one example error span per name, for links and annotations
        if s["name"] in seen:
            continue
        seen.add(s["name"])
        examples.append(
            {
                "name": s["name"],
                "trace_id": s["context"]["traceId"],
                "span_id": s["context"]["spanId"],
                "node": s["id"],
                "first_ts": first.get(s["name"]),
            }
        )
    # tools: the tool spans carry tool_name (a name, never input); the newest 2k say which tools ran
    tools: dict[str, dict[str, Any]] = {}
    try:
        tl = _http(
            "GET",
            f"/v1/projects/{urllib.parse.quote(project())}/spans",
            {"name": ["claude_code.tool"], "start_time": iso(now - 86400), "limit": 1000},
            timeout=30,
        )
        tn: dict[str, int] = {}
        for s in (tl.get("data") or []) if isinstance(tl, dict) else []:
            a = s.get("attributes") or {}
            t = a.get("tool_name") or (a.get("tool") or {}).get("name") if isinstance(a, dict) else None
            if isinstance(t, str) and t:
                tn[t] = tn.get(t, 0) + 1
        top = [t for t, _ in sorted(tn.items(), key=lambda x: -x[1])[:12]]
        tools = _aggregate(pid, top, d1, d7, field="tool_name", extra_cond="name == 'claude_code.tool'")
    except Exception:
        tools = {}
    agg = gql(
        "query($id: ID!, $d1: TimeRange, $d7: TimeRange){ node(id:$id){ ... on Project { "
        "c1: costSummary(timeRange:$d1){ total { cost } } c7: costSummary(timeRange:$d7){ total { cost } } "
        "l1: latencyMsQuantile(probability:0.95, timeRange:$d1) l7: latencyMsQuantile(probability:0.95, "
        "timeRange:$d7) t1: traceCount(timeRange:$d1) t7: traceCount(timeRange:$d7) } } }",
        {"id": pid, "d1": d1, "d7": d7},
    )["node"]

    def cost(k: str) -> float:
        return float(((agg.get(k) or {}).get("total") or {}).get("cost") or 0)

    slow = []
    try:
        tr = _http(
            "GET",
            f"/v1/projects/{urllib.parse.quote(project())}/traces",
            {"start_time": iso(now - 86400), "sort": "latency_ms", "order": "desc", "limit": 5},
            timeout=30,
        )
        for t in (tr.get("data") or []) if isinstance(tr, dict) else []:
            tid = t.get("trace_id") or (t.get("context") or {}).get("trace_id")
            st, en = _ts(t.get("start_time")), _ts(t.get("end_time"))
            ms = t.get("latency_ms") or (int((en - st) * 1000) if st and en else None)
            if tid:
                slow.append({"trace_id": tid, "ms": ms, "root": t.get("root_span_name") or t.get("name")})
    except Exception:
        slow = []
    return {
        "names": by_name,
        "tools": tools,
        "cost": {"now": round(cost("c1"), 2), "base": round(cost("c7") / 7, 2)},
        "trace_p95": {"now": round(float(agg.get("l1") or 0)), "base": round(float(agg.get("l7") or 0))},
        "traces": {"now": int(agg.get("t1") or 0), "base": round(int(agg.get("t7") or 0) / 7)},
        "errors": examples,
        "slow": slow,
        "sampled": len(sample),
    }


def _ts(s: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------------------------------------ writes
def _state() -> dict[str, Any]:
    st = common.load(STATE, {})
    return st if isinstance(st, dict) else {}


def _save(st: dict[str, Any]) -> None:
    common.save(STATE, st)


def annotate(findings: list[dict[str, Any]]) -> int:
    """One span annotation per finding that names an example span: label new|open, score = now / baseline (or now),
    explanation = the code-made finding line (already scrubbed by the caller). Upserted by the finding id."""
    data = []
    for f in findings:
        sp = f.get("span_id")
        if not sp:
            continue
        now, base = f.get("now"), f.get("base")
        score = (
            (float(now) / float(base))
            if isinstance(now, (int, float)) and isinstance(base, (int, float)) and base
            else (float(now) if isinstance(now, (int, float)) else None)
        )
        data.append(
            {
                "span_id": sp,
                "name": ANNOTATION,
                "annotator_kind": "CODE",
                "identifier": f["id"],
                "result": {
                    "label": "new" if f.get("new") else "open",
                    "score": round(score, 3) if score is not None else None,
                    "explanation": str(f.get("msg") or "")[:400],
                },
                "metadata": {
                    "source": f.get("source"),
                    "metric": f.get("metric"),
                    "finding": f["id"],
                    "day": common.today().isoformat(),
                },
            }
        )
    if not data:
        return 0
    _http("POST", "/v1/span_annotations", {"sync": "false"}, {"data": data}, timeout=30)
    return len(data)


def _dataset_id(name: str) -> str | None:
    d = _http("GET", "/v1/datasets", {"name": name, "limit": 10}, timeout=20)
    for x in (d.get("data") or []) if isinstance(d, dict) else []:
        if x.get("name") == name:
            return x.get("id")
    return None


def _upload(
    name: str, desc: str, inputs: list[Any], outputs: list[Any], meta: list[Any], span_ids: list[str] | None = None
) -> dict[str, Any]:
    body: dict[str, Any] = {"name": name, "description": desc, "inputs": inputs, "outputs": outputs, "metadata": meta}
    if span_ids:
        body["span_ids"] = span_ids
    action = "append" if _dataset_id(name) else "create"
    d = _http("POST", "/v1/datasets/upload", {"sync": "true"}, dict(body, action=action), timeout=60)
    return (d.get("data") or {}) if isinstance(d, dict) else {}


def dataset_append(name: str, desc: str, rows: list[dict[str, Any]], key: str = "span_id") -> int:
    st = _state()
    sent = st.setdefault("sent", {}).setdefault(name, [])
    new = [r for r in rows if r.get(key) and r[key] not in sent]
    if not new:
        return 0
    span_ids = [r["span_id"] for r in new] if key == "span_id" else None
    _upload(name, desc, [r["input"] for r in new], [r["output"] for r in new], [r["meta"] for r in new], span_ids)
    st["sent"][name] = (sent + [r[key] for r in new])[-3000:]
    _save(st)
    return len(new)


def _examples(ds: str) -> list[dict[str, Any]]:
    d = _http("GET", f"/v1/datasets/{urllib.parse.quote(ds)}/examples", None, timeout=30)
    ex = ((d.get("data") or {}).get("examples") or []) if isinstance(d, dict) else []
    return [e for e in ex if isinstance(e, dict)]


def _experiment(
    ds: str, ex_id: str, name: str, meta: dict[str, Any], output: dict[str, Any], evals: list[tuple[str, float, str]]
) -> str:
    t = iso(common.now())
    e = _http(
        "POST",
        f"/v1/datasets/{urllib.parse.quote(ds)}/experiments",
        None,
        {"name": name, "metadata": meta, "repetitions": 1},
        timeout=30,
    )
    eid = ((e.get("data") or {}).get("id")) if isinstance(e, dict) else None
    if not eid:
        raise PhoenixError("experiment not created")
    r = _http(
        "POST",
        f"/v1/experiments/{urllib.parse.quote(eid)}/runs",
        None,
        {"dataset_example_id": ex_id, "output": output, "repetition_number": 1, "start_time": t, "end_time": t},
        timeout=30,
    )
    rid = ((r.get("data") or {}).get("id")) if isinstance(r, dict) else None
    for nm, score, label in evals if rid else []:
        _http(
            "POST",
            "/v1/experiment_evaluations",
            None,
            {
                "experiment_run_id": rid,
                "name": nm,
                "annotator_kind": "CODE",
                "start_time": t,
                "end_time": t,
                "result": {"score": score, "label": label},
            },
            timeout=30,
        )
    return eid


def track_proposals(proposals: dict[str, dict[str, Any]], made: list[dict[str, Any]]) -> list[str]:
    """Before/after checks of loop proposals as Phoenix experiments (mutates the proposals' "phx" field; the caller
    saves them). New proposal -> one dataset example (the metric, its target) + a "before" experiment run with the
    value at proposal time. Landed and re-measured (metric_after set) -> an "after" run with evaluations
    target-met (1/0) and moved (share of the gap closed, 0..1)."""
    L: list[str] = []
    for p in made:
        m = p.get("metric") or {}
        row = {
            "input": {
                "proposal": p["id"],
                "metric": m.get("id"),
                "name": m.get("name"),
                "unit": m.get("unit"),
                "target": m.get("target"),
                "dir": m.get("dir", "down"),
            },
            "output": {"before": m.get("now")},
            "meta": {"key": p.get("key"), "day": p.get("day"), "branch": p.get("branch")},
            "pid": p["id"],
        }
        _upload(
            DS_LOOP,
            "Loop proposals: the metric each one must move (before/after experiments)",
            [row["input"]],
            [row["output"]],
            [row["meta"]],
        )
        ds = _dataset_id(DS_LOOP)
        ex = next((e for e in _examples(ds) if (e.get("input") or {}).get("proposal") == p["id"]), None) if ds else None
        if not ds or not ex:
            continue
        eid = _experiment(
            ds,
            ex["id"],
            f"loop {p['id']} before",
            {"proposal": p["id"], "phase": "before"},
            {"value": m.get("now")},
            [],
        )
        p["phx"] = {"dataset": ds, "example": ex["id"], "before": eid}
        L.append(f"{p['id']}: Phoenix experiment 'loop {p['id']} before' ({m.get('id')} {m.get('now')})")
    for p in proposals.values():
        ph = p.get("phx") or {}
        if p.get("status") != "landed" or not ph.get("example") or ph.get("after") or "metric_after" not in p:
            continue
        m, after = p.get("metric") or {}, p.get("metric_after")
        before, target = m.get("now"), m.get("target")
        met = after is None or (
            isinstance(target, (int, float))
            and isinstance(after, (int, float))
            and (after <= target if m.get("dir", "down") == "down" else after >= target)
        )
        moved = 1.0
        if (
            isinstance(before, (int, float))
            and isinstance(after, (int, float))
            and isinstance(target, (int, float))
            and before != target
        ):
            moved = max(0.0, min(1.0, (before - after) / (before - target)))
        eid = _experiment(
            ph["dataset"],
            ph["example"],
            f"loop {p['id']} after",
            {"proposal": p["id"], "phase": "after"},
            {"value": after},
            [
                ("target-met", 1.0 if met else 0.0, "met" if met else "missed"),
                ("moved", round(moved, 3), "moved" if moved >= 0.3 else "flat"),
            ],
        )
        ph["after"] = eid
        L.append(
            f"{p['id']}: Phoenix experiment 'loop {p['id']} after' ({before} -> {after}, "
            f"{'target met' if met else f'moved {moved:.0%}'})"
        )
    return L


def enabled_writes() -> bool:
    ok, _ = enabled()
    return ok and writes_on()


def ms_s(ms: Any) -> str:
    if not isinstance(ms, (int, float)):
        return "?"
    return f"{ms / 1000:.1f} s" if ms >= 1000 else f"{int(ms)} ms"
