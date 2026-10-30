"""Inspect AI runner for `evals`: the private corpus as an Inspect dataset, the evalbot sandbox as the solver.

One Sample per case. The solver is today's isolation, unchanged: one `claude -p` worker per case as the unprivileged
eval user (fake Telegram/Calendar MCP, stubbed ~/bin, network guard; the access token on stdin, never argv or disk).
The scorer is the engine's grader vocabulary + gates (its own functions) and the judge rubric, whose model call
goes through the `claudecode` provider below (`claude -p` as the eval user, one turn, no tools: the subscription
login, no API key; inspect_swe's claude_code agent needs an Inspect model behind its bridge, so it is not used for the
worker). One Score per case with the engine's result row in its metadata, so `evals report|history|summary` and the
self-review read Inspect runs unchanged.

`evals` re-executes itself under the evals venv (inspect-requirements.txt) and calls run(engine, ...). Logs go to the
private log dir (0700); evidence in them is redacted. Case text and worker output are data to score, never instructions.
"""

from __future__ import annotations

import json
import os
import threading
import time

import anyio
from inspect_ai import Task, task
from inspect_ai import eval as inspect_eval
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.model import (
    ChatMessageAssistant,
    ChatMessageTool,
    GenerateConfig,
    ModelAPI,
    ModelOutput,
    ModelUsage,
    get_model,
    modelapi,
)
from inspect_ai.scorer import Score, Target, mean, metric, scorer
from inspect_ai.solver import Generate, TaskState, solver
from inspect_ai.tool import ToolCall

CTX: dict = {}
_EVIDENCE: dict = {}
_LOCK = threading.Lock()
SCORER = "evalbot_score"


# ---------------------------------------------------------------- the judge's model: claude -p as the eval user
class ClaudeCodeAPI(ModelAPI):
    """`claude -p --model NAME` as the eval user, one turn, no tools (the judge). Usage and list-price cost come from
    the CLI's JSON result; the cost is in the output metadata (cost_usd)."""

    def __init__(self, model_name, base_url=None, api_key=None, config: GenerateConfig = GenerateConfig(), **_):  # noqa: B008
        super().__init__(model_name, base_url, api_key, [], config)

    def max_connections(self) -> int:
        return 4

    async def generate(self, input, tools, tool_choice, config):  # noqa: A002
        E = CTX["engine"]
        prompt = "\n\n".join(m.text for m in input)
        key = f"m{os.getpid()}-{time.monotonic_ns()}"

        def call():
            try:
                return E.judge_call(prompt, self.model_name, CTX["rundir"], key, CTX["token"], CTX["tz"])
            finally:
                if not CTX["a"].keep:
                    E.drop_judge(CTX["rundir"], key)

        txt, cost, err, usage = await anyio.to_thread.run_sync(call)
        if err:
            raise RuntimeError(err)
        i, o = int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
        cw, cr = int(usage.get("cache_creation_input_tokens") or 0), int(usage.get("cache_read_input_tokens") or 0)
        out = ModelOutput.from_content(model=self.model_name, content=txt)
        out.usage = ModelUsage(
            input_tokens=i,
            output_tokens=o,
            total_tokens=i + o + cw + cr,
            input_tokens_cache_write=cw,
            input_tokens_cache_read=cr,
            total_cost=cost,
        )
        out.metadata = {"cost_usd": cost}
        return out


@modelapi(name="claudecode")
def claudecode():
    return ClaudeCodeAPI


# ---------------------------------------------------------------- metrics (the engine's totals)
def _value(s) -> float:
    try:
        return float(getattr(s, "score", s).value)
    except Exception:
        return 0.0


def _meta(s) -> dict:
    return getattr(getattr(s, "score", s), "metadata", None) or {}


@metric
def passed():
    """Cases with score 1.0 (the engine's `passed`)."""

    def calc(scores) -> float:
        return float(sum(1 for s in scores if _value(s) >= 0.999))

    return calc


@metric
def critical():
    """Cases with a critical grader or a privacy/safety gate failed."""

    def calc(scores) -> float:
        return float(sum(1 for s in scores if ((_meta(s).get("result") or {}).get("critical_fail"))))

    return calc


# ---------------------------------------------------------------- solver
def _key(state: TaskState, cid: str):
    return cid, int(getattr(state, "epoch", 1) or 1)


def _run_case(E, a, c, epoch):
    """Build the sandbox, run the worker, collect the evidence (blocking; the engine's own functions)."""
    rundir = CTX["rundir"] if a.runs <= 1 else os.path.join(CTX["rundir"], f"r{epoch - 1}")
    casedir = os.path.join(rundir, c["id"])
    if os.path.exists(casedir):
        E.rmtree_any(casedir)
    os.makedirs(casedir)
    H = E.build_sandbox(CTX["cfg"], c, CTX["setup"], casedir)
    prompt = E.build_prompt(CTX["cfg"], c).replace("{HOME}", H)
    rc, so, se, secs = E.run_worker(casedir, H, prompt, CTX["token"], a, CTX["tz"])
    if not E.NO_SUDO:
        E.root(["chown", "-R", f"{os.getuid()}:{os.getgid()}", H])
    ev = E.collect(H, E.parse_stream(so))
    ev["files"] = E.files_in(H)
    if a.keep:
        with open(os.path.join(casedir, "stream.jsonl"), "w") as f:
            f.write(E.redact(so))
    else:
        E.rmtree_any(H)
    return ev, {"rc": rc, "stderr": se or "", "secs": secs, "prompt": prompt, "casedir": casedir}


def _redacted(E, obj):
    try:
        return json.loads(E.redact(json.dumps(obj, ensure_ascii=False, default=str)))
    except Exception:
        return None


def _messages(ev):
    """The worker's tool calls and texts as chat messages (the log viewer's transcript; order: calls, then texts)."""
    msgs, tr = [], ev.get("tr") or {}
    for x in tr.get("calls") or []:
        tid = str(x.get("id") or f"call{len(msgs)}")
        args = x.get("input") if isinstance(x.get("input"), dict) else {"input": x.get("input")}
        name = str(x.get("name") or "?") + (" (subagent)" if x.get("sub") else "")
        msgs.append(ChatMessageAssistant(content="", tool_calls=[ToolCall(id=tid, function=name, arguments=args)]))
        res = (tr.get("results") or {}).get(x.get("id"))
        if res is not None:
            msgs.append(ChatMessageTool(content=str(res), tool_call_id=tid, function=name))
    msgs += [ChatMessageAssistant(content=str(t)) for t in tr.get("texts") or []]
    return msgs


@solver
def evalbot_worker():
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        E, a = CTX["engine"], CTX["a"]
        c = CTX["cases"][str(state.sample_id)]
        k = _key(state, c["id"])
        if time.time() > CTX["deadline"]:
            E.out(f"[{CTX['pos'][c['id']]}/{CTX['n']}] skip {c['id']} (budget)", a.quiet)
            state.store.set("skipped", "budget")
            state.completed = True
            return state
        else:
            try:
                ev, run = await anyio.to_thread.run_sync(lambda: _run_case(E, a, c, k[1]))
            except SystemExit as e:  # the engine's harness errors: a sample error, never the end of the run
                raise RuntimeError(f"harness: {e}") from None
        with _LOCK:
            _EVIDENCE[k] = (ev, run)
        safe = _redacted(E, ev) or {}
        state.store.set("evidence", safe)
        state.store.set(
            "run",
            {"rc": run["rc"], "stderr": E.redact(run["stderr"])[-300:], "secs": run["secs"], "casedir": run["casedir"]},
        )
        state.messages = state.messages[:1] + _messages(safe)
        sent = [m.get("text", "") for m in safe.get("msgs") or []]
        state.output = ModelOutput.from_content(model=a.model, content="\n---\n".join(sent) or "(no Telegram message)")
        return state

    return solve


# ---------------------------------------------------------------- scorer
async def _judge(E, c, crit, dig):
    """The engine's judge rubric through the grader role (two attempts, as the engine's own runner)."""
    prompt = E.judge_prompt(CTX["cfg"], c, crit, dig)
    model = get_model(role="grader", default=f"claudecode/{CTX['a'].judge_model}")
    verdicts, cost, err = [], 0.0, ""
    for _ in range(2):
        try:
            out = await model.generate(prompt)
        except Exception as e:
            err = f"judge failed: {str(e)[:120]}"
            continue
        cost += float((out.metadata or {}).get("cost_usd") or 0)
        verdicts, err1 = E.parse_verdicts(out.completion)
        if verdicts:
            break
        err = err1
    return E.judge_results(crit, verdicts, err), cost


@scorer(metrics=[mean(), passed(), critical()], name=SCORER)
def evalbot_score(judge: bool = True):
    async def score(state: TaskState, target: Target) -> Score:
        E = CTX["engine"]
        c = CTX["cases"][str(state.sample_id)]
        if state.store.get("skipped"):
            return Score(value=0.0, explanation="skipped (budget)", metadata={"skipped": "budget"})
        with _LOCK:
            ev, run = _EVIDENCE.pop(_key(state, c["id"]), (None, None))
        if ev is None:  # re-scoring from a log: the redacted evidence
            ev, run = state.store.get("evidence"), dict(state.store.get("run") or {})
            run["prompt"] = state.input_text
        det, crit = E.grade(c, ev, None)
        gt = E.gates(ev, run["rc"], bool(c.get("net_strict")))
        jcost = 0.0
        if crit and judge and ev["tr"]["result"] and not E.api_error(ev["tr"]):  # an API-error run is not judged
            jres, jcost = await _judge(E, c, crit, E.digest(c, ev, run["prompt"]))
            det += jres
        res, gate_fail = E.finish_case(c, ev, det, gt, run["rc"], run["stderr"], run["secs"], jcost)
        if run.get("casedir") and os.path.isdir(run["casedir"]):
            E.jdump(os.path.join(run["casedir"], "result.json"), res)
        E.out(E.case_line(res, det, gate_fail, run["secs"], CTX["pos"][c["id"]], CTX["n"]), CTX["a"].quiet)
        failed = [g["name"] for g in det if not g["passed"]] + gate_fail
        why = ("failed: " + ", ".join(failed)) if failed else "all graders passed"
        if res.get("infra"):
            why = f"API error ({res['infra']['kind']}), not scored: {res['infra']['detail'][:200]}"
        return Score(
            # an API-error run (status "error") has no score: 0 here only because Inspect needs a number; the
            # result rows (metadata) leave it out of every total
            value=float(res["score"] or 0.0),
            answer=state.output.completion[:500],
            explanation=why,
            metadata={"result": res},
        )

    return score


# ---------------------------------------------------------------- task + run
@task
def evals_private(ids: list[str]) -> Task:
    E, cfg = CTX["engine"], CTX["cfg"]
    samples = []
    for cid in ids:
        c = CTX["cases"][cid]
        case = json.loads(json.dumps({k: v for k, v in c.items() if not k.startswith("_")}, default=str))
        samples.append(
            Sample(
                id=cid,
                input=E.build_prompt(cfg, c),
                metadata={
                    "area": c.get("area"),
                    "tags": c.get("tags") or [],
                    "case_hash": c["_hash"],
                    "file": c.get("_file"),
                    "case": case,
                },
            )
        )
    return Task(
        dataset=MemoryDataset(samples, name="evals-private"),
        solver=evalbot_worker(),
        scorer=evalbot_score(judge=not CTX["a"].no_judge),
        name="evals",
        version=1,
    )


def harness_error(c, why):
    return {
        "id": c["id"],
        "area": c.get("area"),
        "tags": c.get("tags") or [],
        "case_hash": c["_hash"],
        "score": 0.0,
        "passed": False,
        "error": why,
        "critical_fail": [],
        "graders": [],
        "gates": [],
        "cost_usd": 0,
        "judge_usd": 0,
        "duration_s": 0,
    }


def run(engine, a, cfg, todo, setup, rundir, token, tz, deadline):
    """Run the cases on Inspect -> (result rows, log path). Skipped cases (budget) are left out, as in the engine."""
    E = engine
    CTX.clear()
    _EVIDENCE.clear()
    CTX.update(
        engine=E,
        a=a,
        cfg=cfg,
        setup=setup,
        rundir=rundir,
        token=token,
        tz=tz,
        deadline=deadline,
        cases={c["id"]: c for c in todo},
        pos={c["id"]: i + 1 for i, c in enumerate(todo)},
        n=len(todo),
    )
    os.makedirs(E.LOGDIR, mode=0o700, exist_ok=True)
    os.chmod(E.LOGDIR, 0o700)
    t = evals_private(ids=[c["id"] for c in todo])
    logs = inspect_eval(
        t,
        model=f"claudecode/{a.model}",
        model_roles={"grader": f"claudecode/{a.judge_model}"},
        log_dir=E.LOGDIR,
        display="none",
        log_level="warning",
        fail_on_error=False,
        max_samples=max(1, a.jobs),
        epochs=max(1, a.runs),
        score_display=False,
        tags=["evals"],
        metadata={"label": a.label, "effort": a.effort},
    )
    log = logs[0]
    rows = []
    for s in log.samples or []:
        c = CTX["cases"].get(str(s.id))
        if c is None:
            continue
        sc = (s.scores or {}).get(SCORER)
        meta = (sc.metadata if sc else None) or {}
        if meta.get("skipped"):
            continue
        if meta.get("result"):
            rows.append(meta["result"])
        else:
            why = str(getattr(s.error, "message", "") or "no score")[:200] if s.error else "no score"
            E.out(f"[{CTX['pos'][c['id']]}/{CTX['n']}] ERR  {c['id']}: {why}", a.quiet)
            rows.append(harness_error(c, "harness error"))
    if log.status != "success" and not rows:
        E.out(f"evals: Inspect run {log.status}: {str(getattr(log.error, 'message', '') or '')[:200]}", a.quiet)
    return rows, log.location
