"""loop.coverage and the collectors vault / parts / coverage.

The drift guard: every member of the live registries (schedules.toml job features and jobs, the hub kit's
containers, typed tool domains, egress bindings; skills and agents when the box has them) is claimed by a part of
packages/loop/coverage.toml, every part has a loop step, every step id exists (collectors, scorecard metrics, the
agent-file markers and eval areas when present). Add a skill, job feature, container, tool domain or binding without
a coverage entry and this fails. Everything else here runs on fake values."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from loop import common, coverage, health, scorecard

ROOT = Path(__file__).resolve().parents[2]
NOW = 1792634400.0  # 2026-10-22T04:00:00+02:00


def _live_home() -> Path | None:
    from core import paths

    h = paths.real_home()
    return h if (h / ".claude").is_dir() else None


def test_every_part_of_the_live_setup_has_loop_coverage() -> None:
    cov = coverage.load()
    home = _live_home()
    regs = coverage.registries(ROOT, home)
    pr = coverage.check(cov, regs, set(health.COLLECTORS), set(scorecard.WHERE), home)
    assert coverage.problem_lines(pr) == []
    assert regs["features"] and regs["jobs"] and regs["tools"] and regs["egress"]
    assert {"claude-hub", "claude-hub-tg", "claude-hub-browser", "claude-bot"} <= regs["containers"]


def test_the_map_holds_no_personal_values() -> None:
    text = coverage.default_path().read_text(encoding="utf-8")
    for bad in ("@", "/mnt/", "192.168.", "http://", "https://", "/home/"):
        assert bad not in text


def _sched() -> dict[str, dict[str, Any]]:
    return {
        "a": {"feature": "fa", "runs_on": "cron", "cron_line": "* * * * * cron-fail --after 2 a x >> a.log 2>&1"},
        "b": {"feature": "fb", "runs_on": "keepers", "cron_line": ""},
        "c": {"feature": "fc", "runs_on": "cron", "cron_line": "* * * * * c"},
        "d": {"feature": "fd", "runs_on": "cron", "cron_line": "* * * * * d"},
    }


def _cov(**over: Any) -> dict[str, Any]:
    cov = {
        "steps": {"review": {"docs": "", "h:land": ""}, "scorecard": {"cron_fail": ""}, "ideas": {"x": {}}},
        "external_containers": ["ext"],
        "part": [
            {"id": "p1", "features": ["fa", "fb"], "containers": ["c1", "ext"], "skills": ["s1"], "review": ["docs"]},
            {"id": "p2", "jobs": ["c"], "tools": ["t1"], "ideas": ["x"]},
            {"id": "p3", "status": "in-progress", "egress": ["e1"]},
            {"id": "p4", "skills": ["s1", "gone"], "review": ["h:nope"], "scorecard": ["nope"], "ideas": ["y"]},
            {"id": "p5", "agents": ["a1"]},
        ],
    }
    cov.update(over)
    return cov


def _regs() -> dict[str, Any]:
    s = _sched()
    return {
        "features": {"fa", "fb", "fc", "fd"},
        "jobs": set(s),
        "containers": {"c1", "c2"},
        "tools": {"t1"},
        "egress": {"e1", "e2"},
        "skills": {"s1", "s2"},
        "agents": {"a1"},
        "_sched": s,
    }


def test_derived_steps_come_from_the_schedule() -> None:
    rows = {r["id"]: r for r in coverage.matrix(_cov(), _regs())}
    assert rows["p1"]["review"] == ["docs", "h:joberr", "h:cronlogs", "h:hubjobs"]
    assert rows["p1"]["scorecard"] == ["cron_fail"] and rows["p1"]["jobs"] == 2
    assert rows["p2"]["review"] == ["h:joberr"] and rows["p2"]["jobs"] == 1  # claimed by job name, feature fc open


def test_check_names_every_kind_of_problem(tmp_path: Path) -> None:
    pr = coverage.check(_cov(), _regs(), collectors={"land"}, metrics={"reply_p95"}, home=None)
    assert pr["unmapped"] == {
        "features": ["fc", "fd"],  # job c is claimed by name, its feature is not
        "containers": ["c2"],  # ext is declared external
        "egress": ["e2"],
        "skills": ["s2"],
        "jobs": ["d"],
    }
    assert pr["uncovered"] == ["p5"]  # p3 is in progress, p5 has no step
    assert pr["stale"] == {"skills": ["gone"]}
    assert pr["doubled"] == ["skills:s1"]
    assert pr["unknown"] == ["p4: review h:nope", "p4: scorecard nope", "p4: ideas y"]
    assert coverage.problem_count(pr) == 6 + 1 + 1 + 1 + 3
    regs = _regs() | {"skills": None, "agents": None}  # not on this box: skipped, never a problem
    pr = coverage.check(_cov(), regs)
    assert "skills" not in pr["unmapped"] and "skills" not in pr["stale"]


def test_agent_markers_and_eval_areas_are_checked_when_present(tmp_path: Path) -> None:
    (tmp_path / ".claude" / "agents").mkdir(parents=True)
    (tmp_path / ".claude" / "agents" / "idea-scan.md").write_text("reads the thing\n")
    (tmp_path / "opt" / "evals-private").mkdir(parents=True)
    (tmp_path / "opt" / "evals-private" / "config.yaml").write_text("areas:\n  media: {covers: [x]}\n")
    cov = _cov()
    cov["steps"]["ideas"] = {"x": {"marker": "the thing"}, "y": {"marker": "gone input"}}
    cov["part"][1]["evals"] = ["media", "nope"]
    pr = coverage.check(cov, _regs(), home=tmp_path)
    assert pr["markers"] == ["idea-scan.md: ideas y ('gone input' is gone)"]
    assert "p2: evals nope" in pr["unknown"] and "p2: evals media" not in pr["unknown"]


def test_stack_rotation_takes_never_covered_then_oldest(tmp_path: Path) -> None:
    for n in ("2026-10-29-ha-paperless-arr.md", "2026-10-30-claude-code-mcp.md", "stack-news-2026-10-20.md"):
        (tmp_path / n).write_text("x")
    cov = {"steps": {"stack": {"ha": {"match": "-ha-"}, "claude-code": {"match": "claude-code"}, "emby": {}}}}
    assert coverage.stack_due(cov, tmp_path) == [("emby", ""), ("ha", "2026-10-29"), ("claude-code", "2026-10-30")]


def test_markdown_table_and_cli(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    rep = {"rows": coverage.matrix(_cov(), _regs()), "problems": coverage.check(_cov(), _regs()), "stack": []}
    rep["count"] = coverage.problem_count(rep["problems"])
    md = coverage.markdown(rep)
    assert md.splitlines()[0].startswith("| part | self-review |") and "| p3 (in progress) |" in md
    monkeypatch.setattr(coverage, "report", lambda: rep)
    from loop import cli

    assert cli.loop_main(["coverage", "--gaps"]) == 1
    assert "no part claims containers: c2" in capsys.readouterr().out


# ------------------------------------------------------------------------------------------------ collectors
def _fake_tools(answers: dict[str, Any]) -> Any:
    def fake(argv: list[str], timeout: float = 60) -> Any:
        key = argv[2] if argv[:2] == ["tool", "call"] else " ".join(argv[:2])
        v = answers.get(key)
        if isinstance(v, Exception):
            raise v
        return v

    return fake


VAULT_STATUS = {"llm": {"healthy": True, "restarts": 0, "state": "ready"}, "ha": {"error": None},
                "gmail": {"accounts": [{"account": "PRIVATE-ACCOUNT", "error": "boom"}, {"error": None}]}}  # fmt: skip
VAULT_METRICS = {
    "routes": [
        {"route": "/ask", "calls": 40, "errors": 3, "timeouts": 2, "p95_s": 9.5},
        {"route": "/tg", "calls": 10, "errors": 0, "timeouts": 0, "p95_s": 1.0},
    ],  # fmt: skip
    "llm": {"busy_share": 0.25, "dropped": 0, "wait": {"live": {"n": 5, "p95_s": 30.0}}},
    "backlog_max": 2,
}
AUDIT = [{"event": "job_done", "state": "done", "ts": 1}] * 5 + [
    {"event": "job_done", "state": "failed", "ts": 2, "query": "PRIVATE QUERY"}
] * 4


def test_vault_collector_keeps_counts_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        health,
        "run_json",
        _fake_tools({"vault status": VAULT_STATUS, "vault metrics": VAULT_METRICS, "vault audit": AUDIT}),
    )
    texts = {
        "alerts": "1 alert: PRIVATE ALERT TEXT\n",
        "logs": 'Traceback (most recent call last):\n  File "/app/app/llm.py", line 3, in run\n'
        "TimeoutError: PRIVATE CHAT NAME\n",
    }

    def run_text(argv: list[str], timeout: float = 60, system: bool = False) -> str:
        return texts["alerts" if argv[1] == "alerts" else "logs"]

    monkeypatch.setattr(health, "run_text", run_text)
    monkeypatch.setenv("LOOP_VAULT_REPO", str(tmp_path / "no-repo"))
    hist = [{"source": "vault", "day": "2026-10-20", "metrics": {"errors": 0, "audit_failed": 0, "wait_live_p95_s": 4}}]
    r = health.c_vault(health.Ctx("vault", NOW, hist))
    m = r["metrics"]
    assert m["calls"] == 50 and m["errors"] == 5 and m["audit_failed"] == 4 and m["audit_events"] == 9
    assert m["feeds_error"] == 1 and m["alerts"] == 1 and m["llm_busy_pct"] == 25.0
    assert m["commits_24h"] is None and m["open_issues"] is None
    keys = sorted(f["key"] for f in r["findings"])
    assert keys == ["vault:alerts", "vault:audit-failed", "vault:errors", "vault:feeds", "vault:wait"]
    assert any(s.startswith("TimeoutError") for s in r["sigs"])
    blob = json.dumps(r)
    assert "PRIVATE" not in blob and "/ask" in r["line"]


def test_parts_collector(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = {
        "server.docker": {
            "ok": True,
            "data": {
                "containers": [
                    {"name": "claude-hub", "state": "running"},
                    {"name": "claude-hub-tg", "state": "exited"},
                    {"name": "someone-else", "state": "exited"},
                ]
            },
        },  # fmt: skip
        "media.health": {"ok": True, "data": {"problems": [{"x": 1}], "checked": ["a", "b", "c"]}},
        "voice.status": {"ok": True, "data": {"up": False}},
        "web.stats": {"ok": True, "data": {"stats": {"tasks": 4, "failed": 2, "expired": 1, "guard_blocks": 0}}},
        "reminders.list": {
            "ok": True,
            "data": {
                "items": [
                    {"at": "2026-10-22T03:00:00+02:00", "text": "PRIVATE REMINDER"},
                    {"at": "2026-10-22T03:50:00+02:00", "text": "x"},
                    {"at": "2026-10-23T09:00:00+02:00"},
                ]
            },
        },  # fmt: skip
    }
    monkeypatch.setattr(health, "run_json", _fake_tools(answers))
    ha = "last run: 2026-10-20T06:40:01+00:00: ok: 19 findings (14 known-offline), 2 new, 0 recovered, 1 pending\n"
    monkeypatch.setattr(health, "run_text", lambda argv, timeout=60, system=False: ha)
    r = health.c_parts(health.Ctx("parts", NOW, []))
    m = r["metrics"]
    assert m["own_down"] == 1 and m["own_containers"] == 2 and m["media_problems"] == 1 and m["voice_up"] == 0
    assert m["web_failed"] == 3 and m["reminders_late"] == 1 and m["ha_new"] == 2 and m["ha_age_h"] > 26
    keys = sorted(f["key"] for f in r["findings"])
    assert keys == ["parts:ha-new", "parts:ha-stale", "parts:media", "parts:own-down", "parts:reminders-late",
                    "parts:voice", "parts:web"]  # fmt: skip
    (own,) = [f for f in r["findings"] if f["key"] == "parts:own-down"]
    assert "claude-hub-tg" in own["msg"] and "someone-else" not in own["msg"]
    assert "PRIVATE" not in json.dumps(r)
    answers.clear()
    monkeypatch.setattr(health, "run_text", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    with pytest.raises(RuntimeError, match="no part answered"):
        health.c_parts(health.Ctx("parts", NOW, []))


def test_coverage_collector(monkeypatch: pytest.MonkeyPatch) -> None:
    rep = {"rows": coverage.matrix(_cov(), _regs()), "problems": coverage.check(_cov(), _regs())}
    rep["count"] = coverage.problem_count(rep["problems"])
    monkeypatch.setattr(coverage, "report", lambda: rep)
    r = health.c_coverage(health.Ctx("coverage", NOW, []))
    (f,) = r["findings"]
    assert f["key"] == "coverage:gaps" and f["now"] == rep["count"] and "no part claims" in f["msg"]
    assert r["metrics"]["in_progress"] == 1 and r["metrics"]["parts"] == 5
    assert {"vault", "parts", "coverage"} <= set(health.COLLECTORS)
    assert common  # the loop package imports cleanly next to the collectors


def test_stack_rotation_counts_the_topic_id_next_to_its_match(tmp_path: Path) -> None:
    (tmp_path / "2026-10-30-edge.md").write_text("x")
    (tmp_path / "2026-10-28-hedge-fund.md").write_text("x")
    cov = {"steps": {"stack": {"edge": {"match": "nginx|tunnel"}, "unifi": {}}}}
    assert coverage.stack_due(cov, tmp_path) == [("unifi", ""), ("edge", "2026-10-30")]
