"""loop.reflex: revise keeps every version's definition, history, and the read-only review (firings, rates,
the linked eval case's last result, keep | revise | retire with the numbers, the E6 done-when counts)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from loop import reflex
from loop_testlib import PY

REPO_ROOT = Path(__file__).resolve().parents[2]
WHY = "the same answer every time: a watched deal under target gets the standard card"


def add(name: str = "deal-card", **kw: object) -> dict:
    a = dict(kind="note.deals-*", tool="reminders.add", why=WHY, case="case-a")
    a.update(kw)
    return reflex.add(name, **a)  # type: ignore[arg-type]


@pytest.fixture
def evstate(env: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = env / ".local" / "state" / "evals"
    (d / "runs" / "r1").mkdir(parents=True)
    (d / "runs" / "r2").mkdir(parents=True)
    (d / "history.jsonl").write_text(
        json.dumps({"run_id": "r1", "ts": 1.0, "scores": {"case-a": 1.0}})
        + "\n"
        + json.dumps({"run_id": "r2", "ts": 2.0, "scores": {"case-a": 0.2, "case-x": 1.0}})
        + "\n"
    )
    (d / "runs" / "r1" / "result.json").write_text(json.dumps({"cases": [{"id": "case-a", "passed": True}]}))
    (d / "runs" / "r2" / "result.json").write_text(json.dumps({"cases": [{"id": "case-a", "passed": False}]}))
    monkeypatch.setenv("EVALS_STATE", str(d))
    corpus = Path(os.environ["EVALS_CORPUS"])
    (corpus / "cases" / "20-more.yaml").write_text(
        'cases:\n  - id: case-b\n    area: home\n    msg: "hi"\n    expect:\n      - {judge: "ok"}\n'
    )
    return d


def test_review_empty_store_is_zero(env: Path) -> None:
    rv = reflex.review()
    assert rv["reflexes"] == [] and rv["e6"] == {
        "reflexes": 0,
        "with_eval_case": 0,
        "with_firings": 0,
        "revised": 0,
        "retired": 0,
        "done": False,
    }
    assert not (env / ".local" / "state" / "loop" / "reflex.db").exists()  # read-only: no store created


def test_revise_keeps_history(env: Path, evstate: Path) -> None:
    add(when='{"payload.shop": "galaxus*"}')
    reflex.fire("deal-card", "misfire", event="1")
    with pytest.raises(reflex.ReflexError, match="--why"):
        reflex.revise("deal-card", "short", when="{}")
    with pytest.raises(reflex.ReflexError, match="nothing changes"):
        reflex.revise("deal-card", "misfired on every brack note in 7 d")
    with pytest.raises(reflex.ReflexError, match="no eval case"):
        reflex.revise("deal-card", "misfired on every brack note in 7 d", case="nope")
    with pytest.raises(reflex.ReflexError, match="no active"):
        reflex.revise("other", "misfired on every brack note in 7 d", when="{}")
    r = reflex.revise("deal-card", "1 of 1 firings misfired: shop glob too wide", when='{"payload.shop": "galaxus"}')
    assert r["version"] == 2 and r["when"] == {"payload.shop": "galaxus"} and r["eval_case"] == "case-a"
    reflex.fire("deal-card", "ok", event="2")
    assert [x["version"] for x in reflex.log("deal-card")] == [2, 1]
    r = reflex.revise("deal-card", "eval case b covers the narrower rule", case="case-b")
    assert r["version"] == 3 and r["eval_case"] == "case-b"
    reflex.rm("deal-card", "replaced by the deals card binding")
    h = reflex.history("deal-card")
    assert [v["version"] for v in h["versions"]] == [1, 2, 3]
    assert [v["end_op"] for v in h["versions"]] == ["revise", "revise", "rm"]
    assert h["versions"][0]["when"] == {"payload.shop": "galaxus*"}
    assert [x["op"] for x in h["log"]] == ["add", "revise", "revise", "rm"]
    assert add()["version"] == 4
    h = reflex.history("deal-card")
    assert [v["version"] for v in h["versions"]] == [1, 2, 3, 4] and h["versions"][-1]["status"] == "active"
    with pytest.raises(reflex.ReflexError, match="no reflex"):
        reflex.history("nope")


def test_review_suggestions_and_e6(env: Path, evstate: Path) -> None:
    add("calm", case="case-b")  # no eval result for case-b: eval None, too little data -> keep
    add("noisy")  # case-a's last result (r2) failed
    for i in range(6):
        reflex.fire("noisy", "misfire" if i < 4 else "ok", event=str(i))
    rv = reflex.review(days=30)
    by = {x["name"]: x for x in rv["reflexes"]}
    assert by["calm"]["suggestion"] == "keep" and by["calm"]["eval"] is None and by["calm"]["n"] == 0
    n = by["noisy"]
    assert n["n"] == 6 and n["misfire"] == 4 and n["hit_rate"] == round(2 / 6, 3)
    assert n["eval"] == {"run": "r2", "ts": n["eval"]["ts"], "score": 0.2, "passed": False}
    assert n["suggestion"] == "retire" and "4/6" in n["reason"]
    assert rv["e6"]["reflexes"] == 2 and rv["e6"]["with_firings"] == 1 and rv["e6"]["done"] is False
    reflex.fire("calm", "ok")
    reflex.revise("noisy", "4 of 6 firings misfired in 30 d", when='{"kind": "x"}')
    rv = reflex.review()
    by = {x["name"]: x for x in rv["reflexes"]}
    assert by["noisy"]["n"] == 0 and by["noisy"]["by_version"] == {"v1": {"misfire": 4, "ok": 2}}
    assert by["noisy"]["suggestion"] == "revise"  # the eval case still fails
    assert rv["e6"] == {
        "reflexes": 2,
        "with_eval_case": 2,
        "with_firings": 2,
        "revised": 1,
        "retired": 0,
        "done": True,
    }


def test_review_skips_an_api_error_night(env: Path, evstate: Path) -> None:
    """evals keeps a case whose run ended in an API error (status "error": usage limit, 529) out of the history
    scores, so the review reads the last run that scored it, never the errored night as a failure."""
    (evstate / "runs" / "r3").mkdir()
    (evstate / "runs" / "r3" / "result.json").write_text(
        json.dumps(
            {
                "totals": {"ran": 2, "errored": 1, "scored": 1},
                "cases": [
                    {"id": "case-a", "status": "error", "score": None, "infra": {"kind": "usage_limit"}},
                    {"id": "case-x", "status": "pass", "score": 1.0, "passed": True},
                ],
            }
        )
    )
    with (evstate / "history.jsonl").open("a") as f:
        f.write(json.dumps({"run_id": "r3", "ts": 3.0, "errored": 1, "scores": {"case-x": 1.0}}) + "\n")
    add("noisy")
    ev = {x["name"]: x for x in reflex.review()["reflexes"]}["noisy"]["eval"]
    assert ev["run"] == "r2" and ev["score"] == 0.2  # r3 errored for case-a: r2 decides, not a 0 from r3


@pytest.mark.parametrize(
    "n,ok,mis,err,ev,age,idle,want",
    [
        (10, 2, 6, 2, None, 10, 0, "retire"),
        (0, 0, 0, 0, None, 40, None, "retire"),
        (3, 3, 0, 0, None, 40, 31, "retire"),
        (10, 8, 2, 0, None, 10, 0, "revise"),
        (10, 7, 0, 3, None, 10, 0, "revise"),
        (3, 3, 0, 0, {"passed": False, "run": "r", "score": 0}, 2, 0, "revise"),
        (2, 2, 0, 0, None, 2, 0, "keep"),
        (10, 9, 1, 0, {"passed": True}, 10, 0, "keep"),
    ],
)
def test_suggest_rules(n, ok, mis, err, ev, age, idle, want) -> None:  # noqa: ANN001
    assert reflex._suggest(n, ok, mis, err, ev, age, idle)[0] == want


def test_cli_review_revise_history(env: Path, evstate: Path) -> None:
    run = lambda *a: subprocess.run(  # noqa: E731
        [PY, "-I", str(REPO_ROOT / "scripts" / "reflex"), *a],
        capture_output=True,
        text=True,
        env=dict(os.environ),
    )
    r = run("review")
    assert r.returncode == 0 and "E6: 0 reflexes" in r.stdout
    assert (
        run(
            "add", "deal-card", "--kind", "note.deals-*", "--tool", "reminders.add", "--why", WHY, "--case", "case-a"
        ).returncode
        == 0
    )
    r = run("add", "x2", "--kind", "k", "--tool", "reminders.add", "--why", WHY)
    assert r.returncode == 2 and "--case" in r.stderr  # an eval case is required
    r = run("revise", "deal-card", "--why", "misfired twice on brack notes", "--kind", "note.deals-hdd*", "--json")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["version"] == 2
    r = run("history", "deal-card")
    assert r.returncode == 0 and "ended" in r.stdout and "current" in r.stdout
    r = run("review", "--json")
    d = json.loads(r.stdout)
    assert d["e6"]["revised"] == 1 and d["reflexes"][0]["eval"]["run"] == "r2"
