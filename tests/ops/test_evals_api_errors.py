"""evals: a run that ended in an API error (usage limit, 529 overloaded) is infra, not behaviour. It gets status
"error" and no score, stays out of the case / run / history scores (so out of `summary` and the reflex review), is
counted apart in the report, and a run where every case errored is "no data", never 0. Fixture streams only (limit,
529, normal pass, normal fail); the end-to-end part plays them through a fake `claude`, never a model."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
EVALS = REPO / "scripts" / "evals"
LIB = REPO / "scripts" / "lib" / "evals"
FIX = Path(__file__).resolve().parent / "fixtures" / "evals-api-errors"
REAL = Path(os.path.expanduser("~"))
VENV = Path(os.environ.get("EVALS_VENV") or REAL / ".local/share/evals/inspect-venv")
GUARD = REAL / ".local/share/evals/netguard-eval.so"

CASE = {
    "id": "x",
    "expect": [
        {"name": "sets-reminder", "tool": "Bash", "input": "remind add"},
        {"name": "one-reply-max", "replies": {"max": 1}},
        {"name": "no-ha", "no_tool": "Bash", "input": "^ha "},
    ],
}


def stream(name: str) -> str:
    return (FIX / f"{name}.jsonl").read_text(encoding="utf-8")


@pytest.fixture
def E(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    for k, v in {
        "EVALS_NO_SUDO": "1",
        "EVALS_STATE": f"{tmp_path}/state",
        "EVALS_JOBS": f"{tmp_path}/jobs",
        "EVALS_REPORTS": f"{tmp_path}/reports",
        "EVALS_CORPUS": f"{tmp_path}/corpus",
        "USAGE": "off",
        "HOME": str(tmp_path / "home"),
    }.items():
        monkeypatch.setenv(k, v)
    ns: dict = {"__file__": str(EVALS), "__name__": "evals_under_test"}
    exec(compile(EVALS.read_text(encoding="utf-8"), str(EVALS), "exec"), ns)
    return ns


def scored(E: dict, name: str, tmp_path: Path, judge: list | None = None) -> tuple[dict, float]:
    """A fixture run through the graders and finish_case -> (row, the score the old scorer gave it)."""
    H = tmp_path / f"h-{name}"
    (H / ".eval").mkdir(parents=True, exist_ok=True)
    ev = E["collect"](str(H), E["parse_stream"](stream(name)))
    det, _ = E["grade"](CASE, ev, str(H))
    det += judge or []
    gt = E["gates"](ev, 1 if E["api_error"](ev["tr"]) else 0)
    old, _, _ = E["case_score"](det, gt)
    row, _ = E["finish_case"](dict(CASE, _hash="h"), ev, det, gt, 1, "", 3.0, 0.0)
    return row, old


@pytest.mark.parametrize(
    ("name", "kind", "status"),
    [("limit", "usage_limit", 429), ("overloaded", "overloaded", 529), ("pass", None, None), ("fail", None, None)],
)
def test_fixture_streams_classify(E: dict, name: str, kind: str | None, status: int | None) -> None:
    tr = E["parse_stream"](stream(name))
    err = E["api_error"](tr)
    if kind is None:
        assert err is None and tr["api_errors"] == []
    else:
        assert err["kind"] == kind and err["status"] == status, err
        assert tr["api_errors"] and tr["result"]["api_error_status"] == status


def test_api_error_text_only_and_other_errors(E: dict) -> None:
    f = E["api_error"]
    # older CLI output: no api_error_status, no synthetic turn; the text alone decides
    assert f({"result": {"is_error": True, "result": "API Error: 529 Overloaded. try again"}})["kind"] == "overloaded"
    assert (
        f({"result": {"is_error": True, "result": "You've hit your org's monthly spend limit · ask your admin"}})[
            "kind"
        ]
        == "usage_limit"
    )
    assert f({"result": {"is_error": True, "result": "API Error: 500 Internal server error"}})["kind"] == "server_error"
    # a stream cut before its result line: the synthetic turn decides
    assert f({"result": {}, "api_errors": [{"error": "rate_limit", "text": "Rate limited"}]})["kind"] == "rate_limit"
    # errors of the run's own making stay failures
    assert f({"result": {"is_error": True, "result": "Prompt is too long"}}) is None
    assert f({"result": {"is_error": True, "result": "API Error: 400 invalid_request_error"}}) is None
    assert f({"result": {"subtype": "error_max_turns", "is_error": False, "result": ""}}) is None
    assert f({"result": {}}) is None
    # a finished run that merely talks about an overloaded server is behaviour
    assert f({"result": {"is_error": False, "result": "The server was overloaded yesterday."}}) is None


def test_finish_case_rows(E: dict, tmp_path: Path) -> None:
    lim, old = scored(E, "limit", tmp_path)
    assert 0 < old < 1  # the bug: partial checks (max / no_ graders) passed, so the case scored e.g. 0.67
    assert lim["status"] == "error" and lim["score"] is None and lim["passed"] is None
    assert lim["infra"]["kind"] == "usage_limit" and "weekly limit" in lim["infra"]["detail"]
    assert lim["critical_fail"] == []
    ovl, old = scored(E, "overloaded", tmp_path)
    assert (
        0 < old < 1
        and ovl["status"] == "error"
        and ovl["infra"]
        == {
            "kind": "overloaded",
            "status": 529,
            "detail": ovl["infra"]["detail"],
        }
    )
    ok, _ = scored(E, "pass", tmp_path)
    assert ok["status"] == "pass" and ok["score"] == 1.0 and ok["passed"] is True and "infra" not in ok
    bad, _ = scored(E, "fail", tmp_path)
    assert bad["status"] == "fail" and bad["score"] == pytest.approx(0.667) and bad["passed"] is False
    line = E["case_line"](lim, [], [], 3.0, 1, 4)
    assert "ERR" in line and "not scored" in line and "usage_limit" in line


def test_judge_api_error_makes_the_case_an_error(E: dict, tmp_path: Path) -> None:
    lim = "no verdicts: You've hit your weekly limit · resets Nov 1, 2pm (UTC)"
    j = [{"name": "judge1", "kind": "judge", "passed": False, "w": 1.0, "critical": False, "detail": lim}]
    row, _ = scored(E, "pass", tmp_path, judge=j)
    assert row["status"] == "error" and row["infra"]["kind"] == "usage_limit" and row["infra"].get("judge")
    # a real verdict that fails (its reason may say anything) stays a failure
    j = [dict(j[0], detail="overloaded reply")]
    row, _ = scored(E, "pass", tmp_path, judge=j)
    assert row["status"] == "fail" and row["score"] == 0.75


def test_totals_merge_and_no_data(E: dict, tmp_path: Path) -> None:
    rows = {n: scored(E, n, tmp_path)[0] for n in ("limit", "overloaded", "pass", "fail")}
    t = E["run_totals"](list(rows.values()), 10.0)
    assert (t["ran"], t["errored"], t["scored"], t["passed"]) == (4, 2, 2, 1)
    assert t["score"] == 0.834 and not t.get("nodata")  # the mean of the rounded rows (1.0, 0.667)
    assert t["errored_kinds"] == {"overloaded": 1, "usage_limit": 1}
    want = "score 0.83 · passed 1/2 · 2 errored (API error: overloaded ×1, usage_limit ×1), not scored"
    assert E["score_line"](t) == want
    nd = E["run_totals"]([rows["limit"], rows["overloaded"]], 1.0)
    assert nd["nodata"] is True and nd["score"] is None and nd["passed"] == 0
    assert E["score_line"](nd) == "no data: all 2 run(s) ended in an API error (overloaded ×1, usage_limit ×1)"
    assert E["run_totals"]([], 0.0)["score"] == 0.0  # nothing ran (all skipped): unchanged
    # --runs 3: the mean over the runs that finished; all errored = an error row
    m = E["merge_runs"]([rows["limit"], rows["pass"], rows["fail"]])
    assert m["status"] == "fail" and m["score"] == 0.834 and m["errored_runs"] == 1
    assert m["runs"] == [None, 1.0, pytest.approx(0.667)]
    m = E["merge_runs"]([rows["limit"], rows["overloaded"]])
    assert m["status"] == "error" and m["score"] is None and m["errored_runs"] == 2


def test_summary_and_history(E: dict, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    st = Path(E["STATE"])
    st.mkdir(parents=True)
    now = E["now"]()
    hist = [
        {
            "run_id": "r1",
            "ts": now - 7200,
            "rev": "a1",
            "ran": 2,
            "passed": 2,
            "score": 1.0,
            "scores": {"c1": 1.0, "c2": 1.0},
        },
        {
            "run_id": "r2",
            "ts": now - 3600,
            "rev": "a2",
            "ran": 2,
            "scored": 1,
            "errored": 1,
            "passed": 1,
            "score": 1.0,
            "errored_kinds": {"usage_limit": 1},
            "scores": {"c1": 1.0},
        },
    ]
    (st / "history.jsonl").write_text("".join(json.dumps(h) + "\n" for h in hist))
    s = E["summary"](26)
    assert s["errored"] == 1 and not s["regression"] and s["common"] == 1 and s["new_fail"] == []
    assert "passed 1/1; 1 errored (API error: usage_limit ×1), not scored" in s["line"]
    hist.append(
        {
            "run_id": "r3",
            "ts": now - 60,
            "rev": "a3",
            "ran": 2,
            "scored": 0,
            "errored": 2,
            "passed": 0,
            "score": None,
            "nodata": True,
            "errored_kinds": {"usage_limit": 2},
            "scores": {},
        }
    )
    (st / "history.jsonl").write_text("".join(json.dumps(h) + "\n" for h in hist))
    s = E["summary"](26)
    assert s["nodata"] and s["score"] is None and s["regression"] is False and "delta" not in s
    assert s["line"] == "evals r3 (a3): no data: all 2 run(s) ended in an API error (usage_limit ×2)"


def test_report_counts_errored_apart(E: dict, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    rows = [scored(E, n, tmp_path)[0] for n in ("limit", "overloaded", "pass", "fail")]
    for r, cid in zip(rows, ("c-limit", "c-529", "c-pass", "c-fail"), strict=True):
        r["id"], r["area"] = cid, "rem"
    run = {
        "run_id": "r9",
        "rev": "abc",
        "mode": "live",
        "model": "m",
        "effort": "high",
        "runner": "legacy",
        "totals": E["run_totals"](rows, 60.0),
        "cases": rows,
    }
    E["write_report"](run)
    md = (Path(E["REPORTS"]) / "r9.md").read_text()
    assert "Score **0.83** · passed 1/2 · 2 errored (API error: overloaded ×1, usage_limit ×1), not scored" in md
    assert "| rem | 4 | 1 | 2 | 0.83 |" in md
    fails = md.split("## Failing cases")[1].split("## Errored runs")[0]
    assert "c-fail" in fails and "c-limit" not in fails and "c-529" not in fails
    assert "| c-limit | usage_limit 429 |" in md and "| c-529 | overloaded 529 |" in md
    E["print_report"](run, failures_only=True)
    out = capsys.readouterr().out
    assert "2 errored" in out and "c-fail" in out and "c-limit" not in out
    E["print_report"](run)
    assert "ERR  c-limit" in capsys.readouterr().out
    nd = dict(run, run_id="r10", cases=rows[:2], totals=E["run_totals"](rows[:2], 5.0))
    E["write_report"](nd)
    md = (Path(E["REPORTS"]) / "r10.md").read_text()
    assert "**No data**: all 2 run(s) ended in an API error" in md and "| rem | 2 | 0 | 2 | — |" in md


# ---------------------------------------------------------------- end to end: the fixtures through a fake `claude`

CORPUS = """cases:
  - id: x-{n}
    area: rem
    msg: "fixture:{n} remind me tomorrow at 9 to water the plants"
    expect:
      - {{name: sets-reminder, tool: Bash, input: 'remind add'}}
      - {{name: one-reply-max, replies: {{max: 1}}}}
      - {{judge: 'Confirms the reminder in one short line.'}}
"""

FAKE = r"""#!/usr/bin/python3 -I
import json, re, sys
args = sys.argv[1:]
inp = sys.stdin.read()
if "--output-format" in args and args[args.index("--output-format") + 1] == "json":  # the judge
    open(JLOG, "a").write("judge\n")
    v = {"verdicts": [{"n": 1, "pass": True, "why": "fake judge"}]}
    u = {"input_tokens": 1, "output_tokens": 1}
    print(json.dumps({"result": json.dumps(v), "total_cost_usd": 0.01, "usage": u}))
    sys.exit(0)
name = re.search(r"fixture:(\w+)", inp).group(1)
text = open(FIX + "/" + name + ".jsonl").read()
sys.stdout.write(text)
sys.exit(1 if '"is_error": true' in text else 0)
"""


def _guard_ok() -> bool:
    return bool(shutil.which("gcc")) or GUARD.exists()


@pytest.fixture
def world(tmp_path: Path) -> dict:
    if not _guard_ok():
        pytest.skip("no gcc and no installed network guard")
    T = tmp_path
    H = T / "home"
    (H / "bin").mkdir(parents=True)
    for d in ("corpus/cases", "state", "jobs", "reports", "share"):
        (T / d).mkdir(parents=True)
    if not shutil.which("gcc"):
        import hashlib

        shutil.copy2(GUARD, T / "share" / "netguard-eval.so")
        sha = hashlib.sha256((LIB / "netguard_eval.c").read_bytes()).hexdigest()
        (T / "share" / "netguard-eval.so.src-sha256").write_text(sha + "\n")
    (T / "corpus/config.yaml").write_text(
        "owner_id: '100000001'\nowner_name: owner\ntz: UTC\nclock: '2030-01-02T03:04:00+00:00'\n"
        "model: fake-model\neffort: high\n"
    )
    (T / "corpus/cases/a.yaml").write_text(
        "".join(
            CORPUS.format(n=n) if i == 0 else CORPUS.format(n=n).split("\n", 1)[1]
            for i, n in enumerate(("limit", "overloaded", "pass", "fail"))
        )
    )
    fake = T / "fake-claude"
    fake.write_text(FAKE.replace("JLOG", repr(str(T / "judge.log"))).replace("FIX + ", repr(str(FIX)) + " + "))
    fake.chmod(0o755)
    brain = T / "brain"
    subprocess.run(["git", "init", "-q", "--bare", str(brain)], check=True)
    subprocess.run(
        [
            "git",
            "--git-dir",
            str(brain),
            "--work-tree",
            str(H),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t.invalid",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "x",
        ],
        check=True,
    )
    env = {
        "HOME": str(H),
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "USAGE": "off",
        "EVALS_NO_SUDO": "1",
        "EVALS_TEST_TOKEN": "fake-token",
        "EVALS_CLAUDE": str(fake),
        "EVALS_CORPUS": str(T / "corpus"),
        "EVALS_STATE": str(T / "state"),
        "EVALS_JOBS": str(T / "jobs"),
        "EVALS_REPORTS": str(T / "reports"),
        "EVALS_SHARE": str(T / "share"),
        "EVALS_LIB": str(LIB),
        "EVALS_VENV": str(VENV),
        "BRAIN_GIT_DIR": str(brain),
    }
    return {"T": T, "env": env}


def _evals(w: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(EVALS), *args], env=w["env"], capture_output=True, text=True, timeout=240)


def _last(w: dict) -> dict:
    runs = sorted((w["T"] / "state" / "runs").glob("*/result.json"))
    return json.loads(runs[-1].read_text())


def _history(w: dict) -> list[dict]:
    return [json.loads(x) for x in (w["T"] / "state" / "history.jsonl").read_text().splitlines()]


def _check_run(w: dict, runner: str) -> None:
    r = _last(w)
    assert r["runner"] == runner
    st = {c["id"]: (c["status"], c["score"]) for c in r["cases"]}
    assert st == {
        "x-limit": ("error", None),
        "x-overloaded": ("error", None),
        "x-pass": ("pass", 1.0),
        "x-fail": ("fail", pytest.approx(0.667)),
    }, st
    t = r["totals"]
    assert (t["ran"], t["errored"], t["scored"], t["passed"], t["score"]) == (4, 2, 2, 1, 0.834)
    h = _history(w)[-1]
    assert set(h["scores"]) == {"x-pass", "x-fail"} and h["errored"] == 2
    judged = (w["T"] / "judge.log").read_text().count("judge")
    assert judged == 2, "the errored runs must not be judged"
    (w["T"] / "judge.log").unlink()


def test_end_to_end_inspect_runner(world: dict) -> None:
    if not os.access(VENV / "bin" / "python", os.X_OK):
        pytest.skip(f"no evals venv at {VENV}")
    w = world
    p = _evals(w, "run", "--case", "x-*", "-j", "2")
    assert p.returncode == 0, p.stdout[-800:] + p.stderr[-800:]
    _check_run(w, "inspect")
    assert "2 errored (API error: overloaded ×1, usage_limit ×1)" in p.stdout
    rev = (w["T"] / "state" / "last-rev").read_text()
    p = _evals(w, "report", "last")
    assert "ERR  x-limit" in p.stdout and "passed 1/2" in p.stdout
    s = json.loads(_evals(w, "summary", "--json").stdout)
    assert s["errored"] == 2 and s["regression"] is False and "not scored" in s["line"]
    # a night where every run errored: no data (score None), not 0; the base rev for --changed stays
    (w["T"] / "state" / "last-rev").write_text("base0\n")
    p = _evals(w, "run", "--case", "x-limit", "--case", "x-overloaded", "--threshold", "0.9")
    assert p.returncode == 0, p.stdout[-800:] + p.stderr[-800:]
    t = _last(w)["totals"]
    assert t["nodata"] is True and t["score"] is None and t["errored"] == 2
    assert (w["T"] / "state" / "last-rev").read_text() == "base0\n" and rev
    s = json.loads(_evals(w, "summary", "--json").stdout)
    assert s["nodata"] and s["score"] is None and not s["regression"] and "no data" in s["line"]
    assert "no data" in _evals(w, "history", "-n", "1").stdout
    md = (w["T"] / "reports" / f"{_last(w)['run_id']}.md").read_text()
    assert "**No data**" in md and "Errored runs" in md


def test_no_venv_refuses(world: dict) -> None:
    """The legacy thread-pool runner is gone (29.10): without the Inspect venv a run stops and names `evals setup`."""
    w = world
    w["env"]["EVALS_VENV"] = str(w["T"] / "no-venv")
    p = _evals(w, "run", "--case", "x-pass")
    assert p.returncode != 0 and "evals setup" in (p.stdout + p.stderr), p.stdout[-400:] + p.stderr[-400:]
