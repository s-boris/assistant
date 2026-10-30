"""core.cronruns + cron-fail's run log (cron-back 29.10): rows, failures, spans, export cursor, pruning."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from core import cronruns, otlp

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def d(tmp_path, monkeypatch):
    s = tmp_path / "cron-fail"
    s.mkdir()
    monkeypatch.setenv("CRON_RUNS_DIR", str(s))
    return s


def write(d: Path, day: str, rows: list[dict]) -> Path:
    p = d / f"runs-{day}.jsonl"
    with p.open("a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return p


def test_runs_and_failures(d):
    now = time.time()
    day = time.strftime("%Y%m%d", time.localtime(now))
    write(d, day, [{"ts": now - 50, "job": "a", "rc": 0, "ok": 1, "ms": 10},
                   {"ts": now - 40, "job": "b", "rc": 3, "ok": 0, "ms": 20}])  # fmt: skip
    (d / f"runs-{day}.jsonl").open("a").write('not json\n{"job": 1}\n')
    rows = cronruns.runs(now - 3600, now)
    assert [r["job"] for r in rows] == ["a", "b"]
    assert cronruns.failures(rows) == {"b": [now - 40]}
    assert cronruns.runs(now - 45, now)[0]["job"] == "b"


def test_spans_are_safe_and_marked(d):
    sp = cronruns.spans([{"ts": 100.0, "job": "tg-status", "rc": 2, "ok": 0, "ms": 1500}])
    assert sp[0]["name"] == "cron.tg-status" and sp[0]["status"]["code"] == 2
    assert int(sp[0]["endTimeUnixNano"]) - int(sp[0]["startTimeUnixNano"]) == 1_500_000_000
    batches, _dropped = otlp.sanitize({"resourceSpans": [{"scopeSpans": [{"spans": sp}]}]})
    kept = batches[0]["spans"][0] if batches and "spans" in batches[0] else None
    assert kept is None or kept["name"] == "cron.tg-status"


def test_export_moves_cursor_only_when_taken(d, monkeypatch):
    now = time.time()
    day = time.strftime("%Y%m%d", time.localtime(now))
    write(d, day, [{"ts": now - i, "job": "j", "rc": 0, "ok": 1, "ms": 1} for i in range(5)])
    got: list[int] = []
    up = {"ok": False}
    monkeypatch.setattr(otlp, "emit", lambda spans, service, endpoint=None, timeout=0.3: up["ok"] and not got.append(
        len(spans)))  # fmt: skip
    assert cronruns.export(now=now) == (0, 5)
    up["ok"] = True
    assert cronruns.export(now=now) == (5, 0) and got == [5]
    assert cronruns.export(now=now) == (0, 0)
    write(d, day, [{"ts": now, "job": "j", "rc": 1, "ok": 0, "ms": 1}])
    with (d / f"runs-{day}.jsonl").open("a") as f:
        f.write('{"ts": 1, "job": "half')  # a run being written: not sent yet
    assert cronruns.export(now=now) == (1, 0)


def test_export_prunes_old_files(d, monkeypatch):
    now = time.time()
    old = time.strftime("%Y%m%d", time.localtime(now - 10 * 86400))
    p = write(d, old, [{"ts": now - 10 * 86400, "job": "j", "rc": 0, "ok": 1, "ms": 1}])
    monkeypatch.setattr(otlp, "emit", lambda *a, **k: True)
    cronruns.export(now=now)
    assert not p.exists()


def test_cron_fail_writes_the_run_log_and_after_0_writes_no_note(tmp_path):
    home = tmp_path / "h"
    home.mkdir()
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "USAGE": "off", "CRON_FAIL_EVENTS": "/bin/false"}
    cf = str(REPO / "scripts" / "cron-fail")
    for _ in range(3):
        r = subprocess.run(["bash", cf, "--after", "0", "quiet", "sh", "-c", "exit 4"], env=env, timeout=30)
        assert r.returncode == 4
    r = subprocess.run(["bash", cf, "loud", "sh", "-c", "exit 1"], env=env, timeout=30)
    assert r.returncode == 1
    assert subprocess.run(["bash", cf, "--after", "x", "n", "true"], env=env, timeout=30).returncode == 2
    notes = sorted(p.name for p in (home / "inbox").glob("*.md")) if (home / "inbox").exists() else []
    assert [n.split("-failed-")[0] for n in notes] == ["loud"]
    os.environ["CRON_RUNS_DIR"] = str(home / ".local" / "state" / "cron-fail")
    try:
        rows = cronruns.runs(time.time() - 60)
    finally:
        del os.environ["CRON_RUNS_DIR"]
    assert [(r["job"], r["rc"], r["ok"]) for r in rows] == [("quiet", 4, 0)] * 3 + [("loud", 1, 0)]
