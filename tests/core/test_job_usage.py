"""Jobs the harness runs itself (hub schedules through workflow.job, `keepers ensure`) write cron-fail's usage
run row: one spool line per real run (job.<name>, k run, ch/a cron, ms, ok | rc<N>), only for jobs with failure
handling (after > 0), none for a shadow tick; src hubjob / keepers. Temp HOMEs and a temp USAGE_STATE only."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from core import jobfail, watchjobs  # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / "bin").mkdir(parents=True)
    (h / "inbox").mkdir()
    ev = tmp_path / "events-stub"
    ev.write_text("#!/bin/sh\nexit 0\n")
    ev.chmod(0o755)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("ASSISTANT_TEST", "1")
    monkeypatch.setenv("JOB_EVENTS", str(ev))
    monkeypatch.setenv("USAGE_STATE", str(tmp_path / "product"))
    monkeypatch.delenv("USAGE", raising=False)
    for k in ("HUB_URL", "HUB_SCHEMA"):
        monkeypatch.delenv(k, raising=False)
    return h


def spool(src: str) -> list[dict]:
    d = Path(os.environ["USAGE_STATE"]) / "spool"
    return [json.loads(x) for p in sorted(d.glob(f"{src}-*.jsonl")) for x in p.read_text().splitlines() if x]


def script(home: Path, name: str, body: str) -> Path:
    s = home / "bin" / name
    s.write_text("#!/bin/sh\n" + body + "\n")
    s.chmod(0o755)
    return s


def test_the_line_is_cron_fails(home, tmp_path):
    """Same keys and values as cron-fail's own spool line for the same job and exit codes (src, ts, ms aside)."""
    env = {**os.environ, "CRON_FAIL_EVENTS": os.environ["JOB_EVENTS"]}
    for rc in (0, 3, 5):
        subprocess.run(["bash", str(REPO / "scripts" / "cron-fail"), "--after", "2", "--ok", "3", "jx", "/bin/sh", "-c",
                        f"exit {rc}"], env=env, capture_output=True, timeout=60, check=False)  # fmt: skip
        assert jobfail.usage_run("jx", rc, after=2, ok=(3,), ms=12.7, src="hubjob")
    cron, hub = spool("cron"), spool("hubjob")
    assert len(cron) == len(hub) == 3
    for c, h in zip(cron, hub, strict=True):
        assert set(c) == set(h)
        assert {k: v for k, v in c.items() if k not in ("src", "ts", "ms")} == {
            k: v for k, v in h.items() if k not in ("src", "ts", "ms")
        }
    assert [h["o"] for h in hub] == ["ok", "ok", "rc5"] and hub[0]["f"] == "job.jx" and hub[0]["ms"] == 12
    assert {(h["k"], h["ch"], h["a"], h["src"]) for h in hub} == {("run", "cron", "cron", "hubjob")}
    d = Path(os.environ["USAGE_STATE"]) / "spool"
    assert all((p.stat().st_mode & 0o777) == 0o600 for p in d.glob("hubjob-*.jsonl"))


def test_no_line_without_failure_handling_or_when_off(home, monkeypatch):
    assert jobfail.usage_run("model-drift", 1, after=0, ms=5, src="hubjob") is False  # exit 1 = a finding
    assert jobfail.usage_run("bad name!", 0, after=1, ms=5, src="hubjob") is False
    assert jobfail.usage_run("jx", 0, after=1, ms=5, src="Bad Src") is False
    monkeypatch.setenv("USAGE", "off")
    assert jobfail.usage_run("jx", 0, after=1, ms=5, src="hubjob") is False
    assert not (Path(os.environ["USAGE_STATE"]) / "spool").exists()


def test_hub_job_live_runs_write_one_row_each_shadow_none(home):
    from tools.domains import workflow as W

    j = watchjobs.runnable("fx-watch")
    assert j.after == 3
    out = home / "seen"
    script(home, "fx-watch", f'echo "$USAGE_JOB $HUB_JOB" >> {out}\nexit 0')
    assert W.job(W.JobIn(name="fx-watch")).mode == "shadow"  # a shadow tick: no run, no row
    assert W.job(W.JobIn(name="fx-watch", mode="shadow", slot="x")).mode == "shadow"
    assert spool("hubjob") == []
    assert W.job(W.JobIn(name="fx-watch", mode="live")).exit == 0
    script(home, "fx-watch", "exit 7")
    assert W.job(W.JobIn(name="fx-watch", mode="live")).exit == 7
    rows = spool("hubjob")
    assert [(r["f"], r["k"], r["ch"], r["a"], r["o"]) for r in rows] == [
        ("job.fx-watch", "run", "cron", "cron", "ok"),
        ("job.fx-watch", "run", "cron", "cron", "rc7"),
    ]
    assert out.read_text().split() == ["fx-watch", "fx-watch"]  # USAGE_JOB: its texts are credited to the job
    assert jobfail.streak("fx-watch")["count"] == 1


def test_hub_job_without_failure_handling_writes_no_row(home):
    from tools.domains import workflow as W

    assert watchjobs.runnable("watch").after == 0  # never ran under cron-fail: its exit codes have no ok meaning
    script(home, "watch", "exit 1")
    assert W.job(W.JobIn(name="watch", mode="live")).exit == 1
    assert spool("hubjob") == []


def test_hub_job_that_cannot_start_is_a_failed_run(home):
    """cron-fail saw the shell's 127 for a missing command: a failed row and a counted streak, as there."""
    from tools.core import ToolError
    from tools.domains import workflow as W

    assert watchjobs.runnable("tg-status").argv[0] == "~/bin/tg-status"  # no shell, no timeout wrapper: OSError
    with pytest.raises(ToolError) as e:
        W.job(W.JobIn(name="tg-status", mode="live"))
    assert e.value.code == "failed" and "FileNotFoundError" in e.value.message
    assert watchjobs.runnable("fx-watch").argv[0] == "timeout"  # `timeout` itself answers 127
    assert W.job(W.JobIn(name="fx-watch", mode="live")).exit == 127
    assert [(r["f"], r["o"]) for r in spool("hubjob")] == [("job.tg-status", "rc127"), ("job.fx-watch", "rc127")]
    assert jobfail.streak("tg-status")["rc"] == 127 and jobfail.streak("tg-status")["count"] == 1


KREG = """schema = 1
tz = "UTC"

[jobs.fake-keeper]
line = "fake-keeper"
cron = "* * * * *"
class = "K"
queue = ""
where = "cron"
argv = ["~/bin/fake", "ensure"]
timeout = 30
after = {after}
feature = "t"
why = "test"
target = "schedule registry"
keeper = true
every = 1
cron_line = "* * * * * cron-fail --after 2 fake-keeper ~/bin/fake ensure >/dev/null 2>&1"
"""


@pytest.mark.parametrize("after", [2, 0])
def test_keeper_runs_write_the_row(home, tmp_path, after):
    reg = tmp_path / "s.toml"
    reg.write_text(KREG.format(after=after))
    script(home, "fake", '[ -e "$HOME/fail" ] && exit 4\nexit 0')
    env = {**os.environ, "ASSISTANT_SCHEDULES": str(reg)}

    def ensure() -> None:
        r = subprocess.run([sys.executable, "-I", str(REPO / "scripts" / "keepers"), "ensure"], env=env,
                           capture_output=True, text=True, timeout=60, check=False)  # fmt: skip
        assert r.returncode == 0, r.stderr

    ensure()
    (home / "fail").write_text("1")
    ensure()
    rows = spool("keepers")
    if after:
        assert [(r["f"], r["a"], r["o"], r["src"]) for r in rows] == [
            ("job.fake-keeper", "cron", "ok", "keepers"),
            ("job.fake-keeper", "cron", "rc4", "keepers"),
        ]
    else:
        assert rows == []
