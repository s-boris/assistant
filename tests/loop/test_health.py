"""loop.health / loop.phoenix: signatures without text, the shared-red land detector, the CI-blocker log, the 7-day
history and new signatures, the Phoenix collector (aggregates, links, annotations, datasets, experiments) on a fake
transport, the loop's H1 candidates, the switch. All values are fake."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
from loop import common, health, phoenix, propose, signals

NOW = 1792634400.0  # 2026-10-22T03:30:00+02:00 (the env fixture's ASSISTANT_NOW)
SECRETISH = "sk-" + "Q9" * 24
DAY_ = 86400.0


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health, "_SCRUB", None)
    monkeypatch.setattr(health, "_DEPLOYS", None)
    phoenix._PID.clear()


def _state(h: Path, *p: str) -> Path:
    d = h.joinpath(".local", "state", *p)
    d.parent.mkdir(parents=True, exist_ok=True)
    return d


def _jl(p: Path, rows: list[dict[str, Any]]) -> None:
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))


# ------------------------------------------------------------------------------------------------ signatures
def test_signatures_are_classes_frames_and_phrases_never_text() -> None:
    log = "\n".join(
        [
            "Traceback (most recent call last):",
            '  File "/home/x/bin/lib/loop/review.py", line 12, in run',
            f"ValueError: the owner's dentist on Tuesday {SECRETISH}",
            "fetch failed: connection refused by host-q.invalid for Xyzzy",
            "summary: 0 errors, 3 warnings",
            "refused: no (cached)",
            "error: no such file /home/x/private/letter.pdf",
            "something else failed for Plugh's birthday",
        ]
    )
    sig = health.signatures(log)
    assert sig == {
        "ValueError @ loop/review.py:run": 1,
        "connection refused": 1,
        "no such file": 1,
        "other error line": 1,
    }
    blob = json.dumps(sig)
    for word in ("dentist", "Xyzzy", "Plugh", "letter", "host-q", SECRETISH):
        assert word not in blob


def test_scrub_masks_token_shaped_values_without_secretscan(env: Path) -> None:
    assert SECRETISH not in health.scrub(f"why {SECRETISH}")
    assert health.scrub(f"{env}/x") == "~/x"


# ------------------------------------------------------------------------------------------------ land / CI
def _lands(h: Path) -> None:
    t = NOW - 6 * 3600
    rows = [
        {"op": "land", "ok": True, "branch": "good-a", "sha": "a" * 40, "ts": t, "start": t - 60, "end": t},
        {"op": "land", "ok": False, "branch": "b1", "why": "ci", "ts": t + 600},
        {"op": "land", "ok": False, "branch": "b2", "why": "ci", "ts": t + 900, "lock_wait_secs": 30},
        {
            "op": "land",
            "ok": False,
            "branch": "b3",
            "why": "ci",
            "ts": t + 1500,
            "gate": {"submit": "HubError: bundle over the 5 MB cap for the owner's notes"},
        },
        {"op": "land", "ok": True, "branch": "b1", "sha": "b" * 40, "ts": t + 2400, "start": t + 2000, "end": t + 2400},
        {"op": "land", "ok": False, "branch": "own", "why": "ci", "ts": t + 3000},  # one branch's own red
        {"op": "deploy", "ok": False, "ts": t + 100},
        {
            "op": "land",
            "ok": True,
            "branch": "old",
            "ts": NOW - 3 * 86400,
            "start": NOW - 3 * 86400 - 60,
            "end": NOW - 3 * 86400,
        },
    ]
    _jl(_state(h, "assistant", "deploys.jsonl"), rows)


def test_land_shared_red_is_not_a_second_blocked_time(env: Path) -> None:
    """Blocked time comes only from ci-blockers.jsonl (below and the loop's "CI blocked" metric); a shared red
    run in deploys.jsonl adds no blocked_min or finding of its own, and it still keeps a common cause from
    reading as a per-branch fail rate."""
    _lands(env)
    r = health.c_land(health.Ctx("land", NOW, []))
    m = r["metrics"]
    assert m["lands"] == 6 and m["land_fail"] == 4 and m["deploy_fail"] == 1
    assert "blocked_min" not in m and "blocked" not in r["line"]
    fs = {f["key"]: f for f in r["findings"]}
    assert not [k for k in fs if "shared-red" in k] and "land:fail-rate" not in fs
    assert "land:deploy-fail" in fs
    assert r["sigs"]["land ci: HubError"] == 1 and "notes" not in json.dumps(r["sigs"])


def test_ci_blocker_log_open_blocker_is_high(env: Path) -> None:
    _jl(
        _state(env, "assistant", "ci-blockers.jsonl"),
        [
            {"ts": NOW - 3600, "op": "seen", "key": "legacy:self-review"},
            {"ts": NOW - 7200, "op": "seen", "key": "runner-submit:bundle over cap"},
            {"ts": NOW - 5400, "op": "cleared", "key": "runner-submit:bundle over cap"},
        ],
    )
    r = health.c_land(health.Ctx("land", NOW, []))
    assert r["metrics"]["ci_blockers"] == 2 and r["metrics"]["ci_blocked_min"] == 90.0
    f = [x for x in r["findings"] if x["key"].startswith("land:ci-blocker:")]
    assert len(f) == 1 and f[0]["sev"] == 3 and "legacy:self-review" in f[0]["msg"]


# ------------------------------------------------------------------------------------------------ the pass
def _hooks(h: Path, rule: str, n: int) -> None:
    lines = [
        f'2026-10-22T0{1 + i % 2}:00:0{i} HIGH block rule={rule} hash=abc why="x" cmd="rm secret"' for i in range(n)
    ]
    _state(h, "risk-check.log").write_text("\n".join(lines) + "\n")


def test_run_history_new_signatures_and_latest(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HEALTH_SOURCES", "land,hooks")
    _lands(env)
    _hooks(env, "secrets-delete", 3)
    r1 = health.run(write=True, now=NOW - 86400)  # day 1: the history starts, no "new" yet
    assert not [f for f in r1["findings"] if ":newsig:" in f["key"]]
    hist = (env / ".local/state/loop/health/history.jsonl").read_text()
    assert '"source": "hooks"' in hist and "rm secret" not in hist
    _hooks(env, "crontab-replace", 4)
    r2 = health.run(write=True, now=NOW)
    new = [f for f in r2["findings"] if f["key"].startswith("hooks:newsig:")]
    assert len(new) == 1 and "risk HIGH block crontab-replace" in new[0]["msg"]
    assert {f["id"] for f in r2["findings"]} >= {health.fid(new[0]["key"])}
    assert any(f["key"] == "hooks:risk:crontab-replace" for f in r2["findings"])
    assert len([x for x in hist.splitlines() if x]) == 2
    rows = [json.loads(x) for x in (env / ".local/state/loop/health/history.jsonl").read_text().splitlines()]
    assert sorted(x["day"] for x in rows) == ["2026-10-21", "2026-10-21", "2026-10-22", "2026-10-22"]
    sig = health.signal()
    assert sig and sig["findings"] and set(sig["findings"][0]) >= {"key", "sev", "msg"}
    md = health.markdown(r2, {new[0]["id"]})
    assert "## (h) Setup health" in md and "**new**" in md and "hooks:" in md


def test_broken_source_is_listed_and_budget_kept(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HEALTH_SOURCES", "land,slo")
    res = health.run(write=False, now=NOW)  # no `slo` binary in the fake bin dir
    assert "slo" in res["errors"] and "land" in res["readings"]


def test_switch_and_fixture(env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert health.switch_off() is None
    (env / ".config/self-review").mkdir(parents=True)
    (env / ".config/self-review/health-off").write_text("")
    assert health.switch_off() == "~/.config/self-review/health-off"
    fx = tmp_path / "fx.json"
    fx.write_text(
        json.dumps(
            {
                "findings": [
                    {
                        "key": "land:x",
                        "source": "land",
                        "metric": "m",
                        "now": 1,
                        "base": 0,
                        "sev": 2,
                        "msg": f"m {SECRETISH}",
                        "fix": "f",
                    }
                ],
                "readings": {},
            }
        )
    )
    monkeypatch.setenv("LOOP_HEALTH_FIXTURE", str(fx))
    res = health.run(write=True)
    assert res["findings"][0]["id"] == health.fid("land:x") and SECRETISH not in res["findings"][0]["msg"]


# ------------------------------------------------------------------------------------------------ loop H1
def test_loop_turns_health_findings_into_h1_candidates(env: Path) -> None:
    health._save_latest(
        {
            "findings": [
                {
                    "id": "1",
                    "key": "land:shared-red:ci",
                    "source": "land",
                    "metric": "blocked_min",
                    "now": 25,
                    "base": 0,
                    "sev": 3,
                    "msg": "every land was blocked 25 min in 24 h (ci)",
                    "fix": "fix main",
                },
                {
                    "id": "2",
                    "key": "board:x",
                    "source": "board",
                    "metric": "open",
                    "now": 9,
                    "base": 2,
                    "sev": 1,
                    "msg": "low",
                    "fix": "x",
                },
            ]
        },
        common.now(),
    )
    sig = {"health": health.signal()}
    cands = [c for c in propose.candidates(sig) if c["key"].startswith("h1:")]
    assert [c["key"] for c in cands] == ["h1:land:shared-red:ci"]
    c = cands[0]
    assert c["metric"]["id"] == "H1" and c["metric"]["now"] == 25 and c["metric"]["target"] == 0
    assert c["title"].startswith("Setup health (land): every land was blocked") and c["score"] == 5.0
    assert any("setup health (h): 2 anomalies (high 1" in x for x in signals.summary_lines(sig))


# ------------------------------------------------------------------------------------------------ Phoenix (fake)
class FakePhoenix:
    """Answers the GraphQL and REST calls loop.phoenix makes; records every write."""

    COUNTS = {  # name -> (n 24 h, err 24 h, n 7 d, err 7 d, p95 24 h, p95 7 d)
        "gateway.phone.act": (100, 40, 70, 0, 300, 120),
        "claude_code.tool": (500, 0, 3500, 0, 9000, 2000),
        "claude_code.llm_request": (800, 1, 5600, 30, 3000, 3100),
    }

    def __init__(self) -> None:
        self.writes: list[tuple[str, Any]] = []
        self.datasets: dict[str, list[dict[str, Any]]] = {}

    def __call__(self, method: str, path: str, params: Any = None, body: Any = None, timeout: float = 30) -> Any:
        if path == "/graphql":
            return self.gql(body["query"], body.get("variables") or {})
        if path.endswith("/spans"):
            return {
                "data": [{"attributes": {"tool_name": "Bash", "bash_argv0": "rm"}}] * 30
                + [{"attributes": {"tool_name": "Read"}}] * 25
            }
        if path.endswith("/traces"):
            return {
                "data": [
                    {
                        "trace_id": "f" * 32,
                        "start_time": "2026-10-22T00:00:00+00:00",
                        "end_time": "2026-10-22T00:10:00+00:00",
                    }
                ]
            }
        if path == "/v1/datasets" and method == "GET":
            name = (params or {}).get("name")
            return {"data": [{"id": f"ds-{name}", "name": name}] if name in self.datasets else []}
        if path == "/v1/datasets/upload":
            ex = self.datasets.setdefault(body["name"], [])
            for i, inp in enumerate(body["inputs"]):
                ex.append({"id": f"ex{len(ex)}", "input": inp, "output": body["outputs"][i]})
            self.writes.append((path, body))
            return {"data": {"dataset_id": f"ds-{body['name']}"}}
        if path.startswith("/v1/datasets/") and path.endswith("/examples"):
            name = path.split("/")[3][3:]
            return {"data": {"examples": self.datasets.get(name, [])}}
        if method == "POST":
            self.writes.append((path, body))
            n = len(self.writes)
            return {"data": {"id": f"id{n}"}}
        raise AssertionError(f"unexpected {method} {path}")

    def gql(self, q: str, v: dict[str, Any]) -> dict[str, Any]:
        if "projects" in q:
            return {"data": {"projects": {"edges": [{"node": {"id": "UHJvamVjdDoy", "name": "brain"}}]}}}
        if "spans(first" in q:
            if v.get("c") == "status_code == 'ERROR'":
                edges = [
                    {
                        "span": {
                            "id": "U3BhbjoxMQ==",
                            "name": "gateway.phone.act",
                            "statusCode": "ERROR",
                            "startTime": "2026-10-21T20:00:00+00:00",
                            "context": {"traceId": "a" * 32, "spanId": "b" * 16},
                        }
                    }
                ]
            else:
                edges = [
                    {
                        "span": {
                            "id": "x",
                            "name": n,
                            "statusCode": "OK",
                            "startTime": None,
                            "context": {"traceId": "c" * 32, "spanId": "d" * 16},
                        }
                    }
                    for n in self.COUNTS
                    for _ in range(12)
                ]
            return {
                "data": {"node": {"spans": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "edges": edges}}}
            }
        out: dict[str, Any] = {}
        for alias, fn, win, cond in re.findall(
            r'(\w+): (\w+)\([^)]*?timeRange:\$(d1|d7)(?:, filterCondition:"((?:[^"\\]|\\.)*)")?\)', q
        ):
            cond = cond.replace("\\'", "'").replace('\\"', '"')
            if fn == "costSummary":
                out[alias] = {"total": {"cost": 300.0 if win == "d1" else 700.0}}
                continue
            if fn in ("latencyMsQuantile", "traceCount"):
                out[alias] = 5000 if fn == "latencyMsQuantile" else 1000
                continue
            m = re.search(r"(?:name|tool_name) == '([^']+)'", cond)
            key = m.group(1) if m else ""
            row = self.COUNTS.get(key) or ((60, 0, 420, 0, 8000 if key == "Bash" else 100, 1000) if key else (0,) * 6)
            n1, e1, n7, e7, p1, p7 = row
            err = "ERROR" in cond
            if fn == "recordCount":
                out[alias] = (e1 if err else n1) if win == "d1" else (e7 if err else n7)
            elif fn == "spanLatencyMsQuantile":
                out[alias] = p1 if win == "d1" else p7
            else:
                out[alias] = 0
        return {"data": {"node": out}}


def test_phoenix_collector_links_annotates_and_appends(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakePhoenix()
    monkeypatch.setattr(phoenix, "_http", fake)
    assert phoenix.enabled() == (True, "")
    r = health.c_phoenix(health.Ctx("phoenix", NOW, []))
    fs = {f["key"]: f for f in r["findings"]}
    f = fs["phoenix:err:gateway.phone.act"]
    assert f["sev"] == 3 and f["now"] == 40.0 and f["base"] == 0.0
    assert f["link"] == (
        "http://claude-hub-phoenix:6006/projects/UHJvamVjdDoy/traces/"
        + "a" * 32
        + "?selectedSpanNodeId=U3BhbjoxMQ%3D%3D"
    )
    assert f["link"] in f["msg"] and f["span_id"] == "b" * 16
    assert "phoenix:p95:claude_code.tool" in fs and "phoenix:tool:Bash" in fs
    assert "phoenix:err:claude_code.llm_request" not in fs  # 1 error: under the floor
    assert r["sigs"] == {"span ERROR gateway.phone.act": 40, "span ERROR claude_code.llm_request": 1}
    assert "rm" not in json.dumps(r["tools"])  # tool names only, never argv
    res = {"findings": [health._clean(x) for x in r["findings"]], "readings": {"phoenix": health._public(r)}}
    lines = health.phoenix_writeback(res, {res["findings"][0]["id"]})
    ann = [b for p, b in fake.writes if p == "/v1/span_annotations"]
    assert len(ann) == 1 and ann[0]["data"][0]["identifier"] == health.fid("phoenix:err:gateway.phone.act")
    assert ann[0]["data"][0]["result"]["label"] == "new" and ann[0]["data"][0]["annotator_kind"] == "CODE"
    ups = [b for p, b in fake.writes if p == "/v1/datasets/upload"]
    assert {u["name"] for u in ups} == {phoenix.DS_FAILING, phoenix.DS_SLOW}
    assert ups[0]["action"] == "create" and ups[0]["span_ids"] == ["b" * 16]
    assert any("annotation" in x for x in lines)
    health.phoenix_writeback(res)  # same spans again: no second upload
    assert len([p for p, _ in fake.writes if p == "/v1/datasets/upload"]) == 2


def test_phoenix_off_switches(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert phoenix.enabled() == (False, "test mode")  # no network in tests unless the transport is replaced
    monkeypatch.setattr(phoenix, "_http", FakePhoenix())
    (env / ".config/traces").mkdir(parents=True)
    (env / ".config/traces/phoenix-off").write_text("")
    assert phoenix.enabled() == (False, "phoenix-off")
    (env / ".config/traces/phoenix-off").unlink()
    monkeypatch.setenv("HEALTH_PHOENIX_WRITES", "off")
    assert not phoenix.enabled_writes()


def test_phoenix_experiments_before_and_after(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakePhoenix()
    monkeypatch.setattr(phoenix, "_http", fake)
    p = {
        "id": "pabc",
        "key": "h1:x",
        "day": "2026-10-22",
        "branch": "loop/pabc-x",
        "status": "open",
        "metric": {"id": "H1", "name": "land: blocked_min", "now": 25, "target": 0, "unit": "", "dir": "down"},
    }
    props = {"pabc": p}
    lines = phoenix.track_proposals(props, [p])
    assert p["phx"]["before"] and "before" in lines[0]
    p.update(status="landed", metric_after=5)
    lines = phoenix.track_proposals(props, [])
    evals = [b for path, b in fake.writes if path == "/v1/experiment_evaluations"]
    assert {e["name"]: e["result"]["score"] for e in evals} == {"target-met": 0.0, "moved": 0.8}
    assert p["phx"]["after"] and "moved 80%" in lines[0]
    assert phoenix.track_proposals(props, []) == []  # measured once


def test_hubjobs_folds_a_shared_outage_into_one_finding(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A shared outage: 15 jobs errored together (host load ~150, reboot 18:55) and two of them read as job faults."""
    t0 = NOW - 9 * 3600
    rows = [{"id": f"sched-job:{j}-2026-10-21T18:{30 + i:02d}:00+02:00", "created": t0 + i * 120 + k * 30,
             "error": "DBOSMaxStepRetriesExceeded"} for i, j in enumerate("abcde") for k in range(2)]  # fmt: skip
    rows += [{"id": f"sched-job:z-2026-10-22T0{h}:00:00+02:00", "created": NOW - h * 3600, "error": "TimeoutError"}
             for h in (1, 2, 3)]  # fmt: skip
    ran = {"a": "ok", "b": "ok", "c": "ok", "e": "unavailable", "z": "ok"}  # d: retired since (no schedule)
    st = {"engine": {"state": "on"},
          "schedules": [{"job": j, "runs": [{"output": {"code": c}}]} for j, c in ran.items()]}  # fmt: skip

    def fake(argv: list[str], timeout: float = 60) -> Any:
        return rows if argv[:3] == ["hub", "wf", "ls"] else st if argv[:3] == ["hub", "wf", "status"] else []

    monkeypatch.setattr(health, "run_json", fake)
    r = health.c_hubjobs(health.Ctx("hubjobs", NOW, []))
    keys = sorted(f["key"] for f in r["findings"])
    assert [k for k in keys if k.startswith("hubjobs:job:")] == ["hubjobs:job:z"]  # a..e are not five job faults
    (o,) = [f for f in r["findings"] if f["key"].startswith("hubjobs:outage:")]
    assert o["now"] == 10 and o["sev"] == 2 and "across 5 jobs" in o["msg"]
    assert o["msg"].split("still failing: ")[1].split(",")[0] == "e"  # d has no schedule any more: not failing
    assert r["metrics"]["outage_runs"] == 10 and r["metrics"]["run_errors"] == 13
    st["schedules"][3]["runs"][0]["output"]["code"] = "ok"  # e ran ok again
    (o,) = [f for f in health.c_hubjobs(health.Ctx("hubjobs", NOW, []))["findings"] if "outage" in f["key"]]
    assert o["sev"] == 1 and "every job ran ok since" in o["msg"]


def test_events_asks_only_for_statuses_the_ledger_has(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`events list --status updated` exits 2 (no such status: an updated event goes back to "new"), which read as
    "`events list --json` failed for 1 status" every night; every status asked for is one of core.events.STATUSES."""
    from core import events

    asked = []

    def fake(argv: list[str], timeout: float = 60) -> Any:
        if argv[:2] == ["events", "list"]:
            st = argv[argv.index("--status") + 1]
            asked.append(st)
            if st not in events.STATUSES:
                raise RuntimeError(f"events: unknown status {st!r}")
            return [{"ts": NOW - 30 * 3600, "kind": "note.x"}] if st == "listed" else []
        return {}

    monkeypatch.setattr(health, "run_json", fake)
    r = health.c_events(health.Ctx("events", NOW, []))
    assert asked and set(asked) <= set(events.STATUSES)
    assert not [f for f in r["findings"] if "failed for" in f["msg"]]
    assert r["metrics"]["unacked_24h"] == 1


# ------------------------------------------------------------------------------------------------ phone
def _phone_mirror(p: Path, pushes: list[tuple[float, int]], hellos: list[float], fetched: float) -> None:
    import sqlite3

    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE push_log(id TEXT PRIMARY KEY, at INT, level TEXT, tag TEXT, delivered_ms INT,"
                " acked_ms INT, verb TEXT, action_at INT)")  # fmt: skip
    con.execute("CREATE TABLE inbound(id TEXT PRIMARY KEY, t TEXT, at INT)")
    con.execute("CREATE TABLE mirror(k TEXT PRIMARY KEY, v TEXT)")
    for i, (t, ms) in enumerate(pushes):
        con.execute("INSERT INTO push_log VALUES (?, ?, 'timed', 'x', 0, ?, NULL, NULL)", (f"p{i}", int(t * 1000), ms))
    con.execute(
        "INSERT INTO push_log VALUES ('probe', ?, 'probe', 'probe', 0, 999000, NULL, NULL)", (int(NOW - 60) * 1000,)
    )
    for i, t in enumerate(hellos):
        con.execute("INSERT INTO inbound VALUES (?, 'hello', ?)", (f"h{i}", int(t)))
        con.execute("INSERT INTO inbound VALUES (?, 'state', ?)", (f"s{i}", int(t)))
    con.execute("INSERT INTO mirror VALUES ('fetched', ?)", (str(int(fetched)),))
    con.commit()
    con.close()


def test_phone_collector_counts_checks_pushes_connects_crashes_and_inbox(
    env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The phone app (app + gateway + phone-produce): required red checks by id, push p95 and connects against the
    mirror's own 7 days, crash notes by time only, phone-inbox exit codes, phone.* tool errors; nothing of the
    status's place, battery or perms values leaves it."""
    base = [(NOW - DAY_ - 3600 * (2 * k + 1), 4000) for k in range(80)]  # 7 days before: 4 s
    last = [(NOW - 3600 * (k + 1), 90000) for k in range(5)]  # 24 h: 90 s
    hellos = [NOW - DAY_ - 3600 * (k + 1) * 6 for k in range(28)] + [NOW - 60 * k for k in range(1, 41)]  # 4/day, 40
    _phone_mirror(env / ".local/state/claude-hub/phonegw-mirror.db", base + last, hellos, NOW - 120)
    cr = tmp_path / "cron-runs"
    cr.mkdir()
    monkeypatch.setenv("CRON_RUNS_DIR", str(cr))
    runs = [{"ts": NOW - 60 * k, "job": "phone-inbox", "rc": 3 if k % 50 == 0 else 0, "ok": int(k % 50 != 0),
             "ms": 50000} for k in range(1, 1400)]  # fmt: skip
    (cr / "runs-20261022.jsonl").write_text("".join(json.dumps(r) + "\n" for r in runs))
    status = {
        "ok": True,
        "uptime_s": 40 * 3600,
        "devices": [
            {
                "id": "dev-1", "status": "active", "online": True, "app_code": 21, "last_seen": NOW - 5,
                "push_p95_ms": 17598, "route": "lan", "battery_level": 80, "perms": {"location": True},
                "app_budget": {"app": {"reconnects_d": 4}, "over": []},
                "selftest": {"green": 18, "total": 22, "red": ["vpn", "Bad Id!"], "optional_red": ["tiles_added"],
                             "unknown": [], "at": NOW - 7 * DAY_},
            },
            {"id": "dev-0", "status": "revoked", "selftest": {"red": ["paired"]}},
        ],
    }  # fmt: skip
    crash = [{"ts": NOW - 3600, "kind": "note.phone-crash", "subject": "Owner secret crash text"},
             {"ts": NOW - 3 * DAY_, "kind": "note.phone-crash"}]  # fmt: skip
    tools = {"days": 1, "tools": {"phone.lookup": {"calls": 4000, "errors": {"timeout": 7}},
                                  "phone.push": {"calls": 2, "errors": {"unavailable": 2}},
                                  "ha.state": {"calls": 9, "errors": {"x": 50}}}}  # fmt: skip
    asked: list[list[str]] = []

    def fake(argv: list[str], timeout: float = 60) -> Any:
        asked.append(argv)
        if argv[:2] == ["phone", "status"]:
            return status
        if argv[:3] == ["hub", "phone", "status"]:
            return {"loader": {"state": "running"}}
        if argv[:2] == ["events", "list"]:
            assert "--kind" in argv and "note.phone-crash" in argv
            return crash
        if argv[:2] == ["tool", "stats"]:
            return tools
        raise AssertionError(argv)

    monkeypatch.setattr(health, "run_json", fake)
    r = health.c_phone(health.Ctx("phone", NOW, []))
    m = r["metrics"]
    assert m["selftest_red"] == 1 and m["red:vpn"] == 1 and "red:paired" not in m and m["optional_red:tiles_added"] == 1
    assert m["selftest_green"] == 18 and m["selftest_age_h"] == 168.0 and m["app_code"] == 21
    assert m["pushes_acked"] == 5 and m["push_p95_s"] == 90.0 and m["push_p95_base_s"] == 4.0
    assert m["connects"] == 40 and m["connects_base"] == 4
    assert m["crashes"] == 1 and m["crashes_7d"] == 1 and m["gw_restarted_24h"] == 0
    assert m["inbox_failed"] == 27 and m["inbox_runs"] == 1399
    assert m["tool_errors"] == 9 and m["tool_calls"] == 4002 and m["mirror_age_min"] == 2.0
    keys = {f["key"] for f in r["findings"]}
    assert keys == {"phone:selftest:vpn", "phone:push-p95", "phone:reconnects", "phone:crashes", "phone:inbox-fail",
                    "phone:tool-errors"}  # fmt: skip
    assert r["sigs"] == {"phone-inbox exit 3": 27, "phone.lookup timeout": 7, "phone.push unavailable": 2}
    blob = json.dumps(r)
    for word in ("Owner secret", "Bad Id", "lan", "location", "dev-1", "paired"):
        assert word not in blob
    assert "red vpn" in r["line"] and "self-test 18/22" in r["line"]


def test_phone_collector_gateway_down_and_unexplained_restart(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def down(argv: list[str], timeout: float = 60) -> Any:
        if argv[:2] == ["phone", "status"]:
            raise RuntimeError("exit 7")
        return {"loader": {"state": "down"}} if argv[:2] == ["hub", "phone"] else {}

    monkeypatch.setattr(health, "run_json", down)
    r = health.c_phone(health.Ctx("phone", NOW, []))
    (g,) = [f for f in r["findings"] if f["key"] == "phone:gw-down"]
    assert g["sev"] == 2 and "loader down" in g["msg"] and r["metrics"]["gw_up"] == 0
    assert any(f["key"] == "phone:source-broken" and "mirror" in f["msg"] for f in r["findings"])  # no mirror file

    def restarted(argv: list[str], timeout: float = 60) -> Any:
        return {"ok": True, "uptime_s": 3 * 3600, "devices": []} if argv[:2] == ["phone", "status"] else {}

    monkeypatch.setattr(health, "run_json", restarted)
    keys = {f["key"] for f in health.c_phone(health.Ctx("phone", NOW, []))["findings"]}
    assert "phone:gw-restart" in keys
    _jl(
        _state(env, "assistant", "deploys.jsonl"), [{"op": "deploy", "ok": True, "target": "hub", "ts": NOW - 4 * 3600}]
    )
    monkeypatch.setattr(health, "_DEPLOYS", None)
    keys = {f["key"] for f in health.c_phone(health.Ctx("phone", NOW, []))["findings"]}
    assert "phone:gw-restart" not in keys  # a hub deploy before it explains the restart


def test_signatures_that_fold_into_one_add_their_counts(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """phone-inbox exit 3 (x15) and exit 124 (x4) both normalize to "exit N": the cluster counts 19, not the last 4."""

    def fake(ctx: health.Ctx) -> dict[str, Any]:
        return {
            "metrics": {},
            "sigs": {"phone-inbox exit 3": 15, "phone-inbox exit 124": 4},
            "findings": [],
            "line": "",
        }

    monkeypatch.setitem(health.COLLECTORS, "phone", (fake, "test"))
    res = health.run(write=False, now=NOW, only=["phone"])
    assert [(c["sig"], c["n"]) for c in res["clusters"]] == [("phone-inbox exit N", 19)]


# ------------------------------------------------------------------------------------------------ backups
def _brain_git(h: Path, ts: float) -> None:
    wt = h / "brain-wt"
    wt.mkdir(exist_ok=True)
    (wt / "f").write_text(str(ts))
    e = dict(os.environ, GIT_DIR=str(h / ".brain.git"), GIT_WORK_TREE=str(wt))
    e |= {"GIT_COMMITTER_DATE": f"@{int(ts)} +0000", "GIT_AUTHOR_DATE": f"@{int(ts)} +0000"}
    for argv in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@example.invalid",
                                                 "commit", "-q", "-m", "x"]):  # fmt: skip
        subprocess.run(["git", *argv], env=e, check=True, capture_output=True)


def _backups(h: Path, ha_ages_h: list[float], git_age_h: float, archive_day: str | None) -> None:
    d = h / "backups" / "ha"
    d.mkdir(parents=True, exist_ok=True)
    for i, a in enumerate(ha_ages_h):
        p = d / f"2026-10-{10 + i:02d}_0315_abcd{i:04d}__Automatic_backup_2026.10.2.tar"
        p.write_text(f"PRIVATE-BACKUP-BODY {SECRETISH}")  # never read: only names and mtimes
        os.utime(p, (NOW - a * 3600, NOW - a * 3600))
        (d / (p.name + ".sha256")).write_text("x")
    (d / "notes.txt").write_text("not a backup")
    _brain_git(h, NOW - git_age_h * 3600)
    log = _state(h, "brain-backup.log")
    lines = [f"secrets escrow updated {SECRETISH}"]
    if archive_day:
        lines += [f"-rw-r--r-- 1 root root 5 /mnt/user/backup/claude-bot/claude-bot-{archive_day}.tar.gz"]
    log.write_text("\n".join(lines) + "\n")


def test_backups_fresh_is_quiet(env: Path) -> None:
    _backups(env, [23.5, 47.5, 71.5, 95.5], 0.5, "2026-10-21")  # NOW = 22.10 04:00 local, before the HA pull
    r = health.c_backups(health.Ctx("backups", NOW, []))
    assert r["findings"] == []
    m = r["metrics"]
    assert m["ha_count"] == 4 and m["ha_age_h"] == 23.5 and m["brain_git_age_h"] == 0.5
    assert m["brain_archive_age_h"] == 28 and m["verify_fail_notes"] == 0
    assert "backups" in health.COLLECTORS


def test_backups_stale_few_and_failed_verify(env: Path) -> None:
    _backups(env, [40, 64], 50, "2026-10-19")
    (env / "inbox" / "brain-backup-verify-failed-2026-10-01.md").write_text("x")
    old = env / "inbox" / "brain-backup-verify-failed-2026-08-01.md"
    old.write_text("x")
    os.utime(old, (NOW - 80 * DAY_, NOW - 80 * DAY_))
    r = health.c_backups(health.Ctx("backups", NOW, []))
    by = {f["key"]: f for f in r["findings"]}
    assert set(by) == {"backups:ha:stale", "backups:ha:few", "backups:brain-git", "backups:brain-archive",
                       "backups:brain-verify"}  # fmt: skip
    assert by["backups:ha:stale"]["sev"] == 2 and by["backups:ha:stale"]["now"] == 40
    assert by["backups:ha:few"]["now"] == 2
    assert by["backups:brain-git"]["now"] == 50 and by["backups:brain-git"]["sev"] == 2
    assert by["backups:brain-archive"]["now"] == 76 and "2026-10-19" in by["backups:brain-archive"]["msg"]
    assert by["backups:brain-verify"]["now"] == 1
    assert all(f["fix"] and f["source"] == "backups" for f in r["findings"])
    blob = json.dumps(r)
    assert "PRIVATE-BACKUP-BODY" not in blob and SECRETISH not in blob and "escrow updated" not in blob


def test_backups_missing_everything_is_high(env: Path) -> None:
    r = health.c_backups(health.Ctx("backups", NOW, []))
    by = {f["key"]: f for f in r["findings"]}
    assert by["backups:ha:stale"]["sev"] == 3 and by["backups:ha:stale"]["now"] is None
    assert by["backups:brain-git"]["sev"] == 3
    assert "backups:ha:few" not in by  # zero kept is the stale finding, not a second one
    assert by["backups:brain-archive"]["now"] is None


def test_backups_run_and_report_line(env: Path) -> None:
    _backups(env, [100], 1, "2026-10-21")
    res = health.run(write=False, now=NOW, only=["backups"])
    keys = [f["key"] for f in res["findings"]]
    assert keys == ["backups:ha:stale", "backups:ha:few"] and res["findings"][0]["sev"] == 3
    md = health.markdown(res)
    assert "backups: newest HA backup" in md and "Fix: `ha-backup-pull --status`" in md
