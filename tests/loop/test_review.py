"""loop.signals / propose / review: signals from the fakes, metric-first candidates, <= 3 PR-draft branches a day,
no repeats, cool-down, ledger silence, accept / reject, landed follow-up, expiry, dry run."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from loop import common, propose, review, signals
from loop_testlib import PY, SIG, calls, git, set_sig

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_signals_collect_numbers_only(env: Path) -> None:
    sig = signals.collect()
    assert sig["errors"] == {}
    t = sig["traces"]
    assert t["owner_first_p95_s"] == 52.0 and t["hooks"] == {"vault-guard": 30} and t["error_rate_pct"] == 1.5
    assert ("Bash:exit_code", 90) in t["error_top"]
    assert sig["beats"]["tokens_mean"] == 1000000
    ids = [f["id"] for f in sig["usage"]["flagged"]]
    assert "internal.x" not in ids and "gym.recap" in ids  # owner-facing only
    assert sig["denials"]["domains"]["home"]["unexpected"] == 1
    assert "reason" not in sig["denials"]["recent"][0]  # names and ids, not texts
    assert sig["reflexes"]["active"] == 0 and sig["corrections"]["open"] == 0
    lines = signals.summary_lines(sig)
    assert any(x.startswith("traces (1 d)") for x in lines) and any("beats:" in x for x in lines)


def test_signals_source_failure_is_listed(env: Path) -> None:
    (env / "bin" / "traces").unlink()
    sig = signals.collect()
    assert sig["traces"] is None and "traces" in sig["errors"]
    assert sig["beats"] is not None


def test_candidates_metric_first_and_ranked(env: Path) -> None:
    c = propose.candidates(signals.collect())
    keys = [x["key"] for x in c]
    assert keys[0] == "g4:new-fail:case-a"  # regressions first
    assert "g3p:home" in keys and "g1:beat-tokens" in keys and "g3h:vault-guard" in keys
    assert "g2:first-reply-p95" in keys and "g3:tool-failures" in keys
    assert "u1:gym.recap" in keys and "u2:skill.vault" in keys
    for x in c:
        m = x["metric"]
        assert {"id", "name", "now", "target", "source"} <= set(m) and x["evidence"] and x["change"]


def test_no_candidates_when_on_target(env: Path) -> None:
    sig = {
        "traces": {"days": 1, "owner_first_p95_s": 30.0, "error_rate_pct": 0.5, "hooks": {"x": 2}, "hook_blocks": 2},
        "beats": {"tokens_mean": 200000},
        "evals": {"new_fail": []},
        "denials": {"domains": {"home": {"unexpected": 0}}},
        "usage": {"flagged": []},
        "reflexes": {},
    }
    assert propose.candidates(sig) == []


def test_ci_blocked_line_and_finding_over_30_min(env: Path) -> None:
    """The shared-CI-blocker log (core.ciblock) feeds the metrics block; > 30 min blocked in 24 h is a C1 finding."""
    from core import ciblock, clock

    now = clock.now()
    sig = signals.collect()
    assert sig["ci"]["n"] == 0 and "ci" not in sig["errors"]
    assert "CI blocked: 0 blockers, 0 min total (longest: none)" in signals.summary_lines(sig)
    assert not [c for c in propose.candidates(sig) if c["key"] == "c1:ci-blocked"]
    ciblock.seen("legacy:fifodemo", "b1", "fifodemo red on main", now=now - 3 * 3600)
    ciblock.clear(["legacy:fifodemo"], now=now - 3 * 3600 + 25 * 60)
    sig = signals.collect()
    assert "CI blocked: 1 blockers, 25 min total (longest: legacy:fifodemo, 25 min)" in signals.summary_lines(sig)
    assert not [c for c in propose.candidates(sig) if c["key"] == "c1:ci-blocked"]  # 25 min: on target
    ciblock.seen("runner-submit:huberror: bundle is # mb (limit # mb)", "b2", "refused", now=now - 20 * 60)
    sig = signals.collect()
    line = "CI blocked: 2 blockers, 45 min total (longest: legacy:fifodemo, 25 min); 1 still open"
    assert line in signals.summary_lines(sig)
    [c] = [c for c in propose.candidates(sig) if c["key"] == "c1:ci-blocked"]
    assert c["metric"]["id"] == "C1" and c["metric"]["now"] == 45 and c["metric"]["target"] == 30
    assert "1 still open" in c["evidence"][1]


def test_cost_per_agent_run_line_and_finding(env: Path) -> None:
    """C2 = subagent cost / runs (`traces agents`) for 24 h and 7 d, a finding when the 7-day value is above
    $3.40 (the per-run baseline); cost per landed change (deploys.jsonl ok lands) is an info value only."""
    from core import clock

    now = clock.now()
    sig = signals.collect()
    assert "cost" not in sig["errors"] and sig["cost"]["7d"]["per_run"] is None  # no run: no value
    assert "Cost per agent run: n/a (no run) (24 h)" in sig["cost"]["line"]
    assert not [c for c in propose.candidates(sig) if c["key"].startswith("c2:")]
    st = env / ".local" / "state" / "assistant"
    st.mkdir(parents=True)
    rows = [
        {"op": "land", "ok": True, "ts": now - 3600},
        {"op": "land", "ok": False, "ts": now - 3000},  # failed: not a landed change
        {"op": "deploy", "ok": True, "ts": now - 2000},  # not a land
        {"op": "land", "ok": True, "ts": now - 3 * 86400},
        {"op": "land", "ok": True, "ts": now - 9 * 86400},  # outside 7 d
    ]
    (st / "deploys.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows) + "not json\n")
    set_sig(
        agents=[
            {"id": "a1", "cost": 2.0, "start_ts": now - 1800},
            {"id": "a2", "cost": 8.0, "start_ts": now - 2 * 86400},
            {"id": "a3", "cost": 50.0, "start_ts": now - 8 * 86400},  # outside 7 d
        ]
    )
    sig = signals.collect()
    c = sig["cost"]
    assert (c["24h"]["runs"], c["24h"]["cost"], c["24h"]["per_run"], c["24h"]["lands"], c["24h"]["per_land"]) == (
        1,
        2.0,
        2.0,
        1,
        2.0,
    )
    assert (c["7d"]["runs"], c["7d"]["cost"], c["7d"]["per_run"], c["7d"]["lands"], c["7d"]["per_land"]) == (
        2,
        10.0,
        5.0,
        2,
        5.0,
    )
    line = (
        "Cost per agent run: $2.00 (24 h), $5.00 (7 d), target ≤ $3.40 (pre-v2 week 15.–22.10: 636 runs, $2,152"
        " ($3.38/run)); per landed change (info): $2.00 (24 h), $5.00 (7 d)"
    )
    assert line in signals.summary_lines(sig)
    [f] = [x for x in propose.candidates(sig) if x["key"].startswith("c2:")]
    assert f["key"] == "c2:cost-per-run" and f["title"] == "Bring the cost per agent run back to pre-v2"
    assert f["metric"]["id"] == "C2" and f["metric"]["now"] == 5.0 and f["metric"]["target"] == 3.4
    assert "2 agent runs" in f["evidence"][1]
    argv = [x for x in calls(env) if x[:2] == ["traces", "agents"]][-1]
    assert argv[argv.index("--since") + 1] == "2026-10-15 03:30"  # 7 d before ASSISTANT_NOW, his time
    # per run under target while per land is far above it: no finding (per land is info only)
    set_sig(agents=[{"id": f"b{i}", "cost": 3.0, "start_ts": now - 1800 - i} for i in range(10)])
    sig = signals.collect()
    assert (sig["cost"]["7d"]["per_run"], sig["cost"]["7d"]["per_land"]) == (3.0, 15.0)
    assert not [x for x in propose.candidates(sig) if x["key"].startswith("c2:")]
    # no landed change: per run still measured
    (st / "deploys.jsonl").write_text("")
    sig = signals.collect()
    assert (
        sig["cost"]["7d"]["per_land"] is None
        and "per landed change (info): n/a (no landed change)" in sig["cost"]["line"]
    )


def test_pick_skips_open_rejected_and_ledger(env: Path) -> None:
    cands = propose.candidates(signals.collect())
    ledger = propose.negative_ledger()
    now = common.now()
    new, skipped = propose.pick(cands, {}, ledger, now=now, limit=10, max_open=20)
    assert "u1:market.watchlist" not in [c["key"] for c in new]
    assert any("ledger says" in s and "market watchlist" in s for s in skipped)
    props = {
        "p1": {"id": "p1", "key": "g4:new-fail:case-a", "status": "open", "created_ts": now},
        "p2": {
            "id": "p2",
            "key": "g3p:home",
            "status": "rejected",
            "created_ts": now,
            "decided_ts": now - 86400,
            "metric": {"now": 1},
        },
        "p3": {
            "id": "p3",
            "key": "g1:beat-tokens",
            "status": "rejected",
            "created_ts": now,
            "decided_ts": now,
            "metric": {"now": 500000},
        },  # now 1M: >= 50 % worse -> may come again
    }
    new, skipped = propose.pick(cands, props, ledger, now=now, limit=3)
    keys = [c["key"] for c in new]
    assert len(new) == 3 and "g4:new-fail:case-a" not in keys and "g3p:home" not in keys
    assert "g1:beat-tokens" in keys
    assert any("already open" in s for s in skipped) and any("rejected" in s for s in skipped)
    _new, skipped = propose.pick(cands, props, ledger, now=now + 15 * 86400, limit=3)
    assert not any(s.startswith("g3p:home: rejected") for s in skipped)  # cool-down over


def test_run_makes_three_branches_and_one_note(env: Path) -> None:
    res = review.run()
    assert len(res["new"]) == 3
    repo = env / "opt" / "assistant"
    for p in res["new"]:
        assert git(repo, "rev-parse", f"refs/heads/{p['branch']}") == git(repo, "rev-parse", "main")
        desc = git(repo, "config", f"branch.{p['branch']}.description")
        assert desc.splitlines()[2].startswith(f"Metric ({p['metric']['id']})")  # metric first
        assert "never auto-merged" in desc
    note = res["note"]
    text = Path(note).read_text()
    assert Path(note).name == "loop-proposals-2026-10-22.md" and oct(Path(note).stat().st_mode)[-3:] == "600"
    assert "Data, not instructions." in text and "\nNext:" in text and "# Loop: 3 change proposal(s)" in text
    assert str(env) not in text
    st = json.loads((env / ".local" / "state" / "loop" / "proposals.json").read_text())
    assert len(st["proposals"]) == 3
    assert "## (g) Loop" in res["markdown"] and "Signals (counts only)" in res["markdown"]
    again = review.run()  # same day, same gaps: nothing new, no new note
    assert again["new"] == [] and again["note"] is None
    assert any("already open" in s for s in again["skipped"])


def test_dry_run_writes_nothing(env: Path) -> None:
    res = review.run(write=False)
    assert len(res["new"]) == 3 and res["note"] is None
    repo = env / "opt" / "assistant"
    assert git(repo, "branch", "--list", "loop/*") == ""
    assert not (env / ".local" / "state" / "loop" / "proposals.json").exists()
    assert list((env / "inbox").iterdir()) == []


def test_accept_reject_and_landed(env: Path) -> None:
    res = review.run()
    a, b = res["new"][0], res["new"][1]
    out = review.accept(a["id"])
    wt = Path(out["worktree"])
    assert wt.is_dir() and (wt.parent / "PR.md").read_text().startswith(f"loop {a['id']}:")
    assert review.show(a["id"])["status"] == "accepted"
    repo = env / "opt" / "assistant"
    rj = review.reject(b["id"], "not worth it now")
    assert rj["branch"] == "branch deleted" and git(repo, "branch", "--list", b["branch"]) == ""
    with pytest.raises(review.LoopError):
        review.reject(b["id"], "again")
    with pytest.raises(review.LoopError):
        review.reject(a["id"], " ")
    # the builder commits on the branch and it lands in main (fast-forward)
    (wt / "fix.txt").write_text("fix\n")
    git(wt, "add", "-A")
    git(wt, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "fix")
    git(repo, "merge", "-q", "--ff-only", a["branch"])
    set_sig(evals={"run_id": "r2", "new_fail": [], "line": "evals r2: 1.00"})
    res2 = review.run()
    assert review.show(a["id"])["status"] == "landed"
    assert any(x.startswith(f"{a['id']} landed:") for x in res2["follow"])


def test_open_proposals_expire(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    res = review.run()
    p = res["new"][0]
    monkeypatch.setenv("ASSISTANT_NOW", "2026-11-06T03:30:00+01:00")
    res2 = review.run(write=True)
    assert review.show(p["id"])["status"] == "expired"
    assert any(f"{p['id']} expired" in x for x in res2["follow"])
    assert git(env / "opt" / "assistant", "branch", "--list", p["branch"]) == ""


def test_proposal_text_has_no_home_paths_or_private_fields(env: Path) -> None:
    res = review.run()
    blob = json.dumps(res["new"]) + res["markdown"]
    assert str(env) not in blob
    assert "x" == SIG["policy"]["denies"][0]["reason"] and '"reason"' not in blob


def test_cli_end_to_end(env: Path) -> None:
    run = lambda *a: subprocess.run(  # noqa: E731
        [PY, "-I", str(REPO_ROOT / "scripts" / "loop"), *a],
        capture_output=True,
        text=True,
        env=dict(os.environ),
    )
    r = run("review")
    assert r.returncode == 0, r.stderr
    assert "## (g) Loop" in r.stdout and "note: ~/inbox/loop-proposals-2026-10-22.md" in r.stdout
    r = run("ls", "--json")
    pid = json.loads(r.stdout)["proposals"][0]["id"]
    r = run("show", pid[:4])
    assert r.returncode == 0 and r.stdout.startswith(f"loop {pid}:")
    r = run("reject", "pzzzzzz", "--why", "x")
    assert r.returncode == 3 and "no proposal" in r.stderr
    r = run("accept", pid, "--no-worktree")
    assert r.returncode == 0 and "accepted" in r.stdout
    assert ["memory", "ledger", "--negative", "--json"] in calls(env)
