"""cron-back (29.10): the registry after the move, render_cron, and the cut-over / rollback script against stubs."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from core import schedules

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "cron-back"
JOBWRAP_JOBS = {"test-all", "calcache-age", "gym-weekly", "gym-recap", "unraid-update-check"}


def test_registry_hub_keeps_only_real_workflows():
    reg = schedules.load()
    back = [e for e in reg.jobs.values() if e.target == schedules.CRON_BACK]
    if not back:
        pytest.skip("the registry flip (cron-back B) is not in this tree yet")
    assert sorted(e.name for e in reg.jobs.values() if e.runs_on == "hub") == ["eventwf-sync", "remind-hub"]
    assert len(back) >= 97
    for e in back:
        assert e.runs_on == "cron" and e.line == e.name, e.name
        if e.argv and e.argv[0] != "~/bin/jobwrap" and e.name != "tg-inbox-prune":  # these keep their old line
            assert e.cron_line == schedules.render_cron(e), e.name
            assert " cron-fail --after " in e.cron_line and " flock -n -E 0 " in e.cron_line, e.name


def test_render_cron_shapes():
    e = schedules.Entry(name="x", line="x", cron="*/5 * * * *", cls="M", group="g", queue="fast", where="brain",
                        runs_on="cron", argv=("~/bin/x", "a%b"), timeout=60, after=0, feature="f",
                        owner_facing=False, why="w", target=schedules.CRON_BACK, cron_line="*/5 * * * * x",
                        out="x/cron.log", err="out", env=(("CRON", "1"),), ok=(3,))  # fmt: skip
    line = schedules.render_cron(e, home="/h")
    assert line == ('*/5 * * * * mkdir -p "$HOME/.local/state/x" && cron-fail --after 0 --ok 3 x flock -n -E 0 '
                    '"$HOME/.local/state/cron-fail/x.run" timeout -k 10 60 env CRON=1 /h/bin/x a\\%b '
                    '>> "$HOME/.local/state/x/cron.log" 2>&1')  # fmt: skip
    assert "/h/.local" not in line  # JOBS.md: "$HOME" in redirects, never a literal home path
    with pytest.raises(ValueError):
        schedules.render_cron(schedules.Entry(**{**e.__dict__, "argv": (), "shell": True}))


REG = """schema = 1
[jobs.a]
line = "a"
cron = "*/5 * * * *"
class = "M"
group = "g"
queue = "fast"
where = "brain"
runs_on = "cron"
argv = ["~/bin/a"]
timeout = 60
after = 2
feature = "f"
owner_facing = false
why = "w"
target = "cron-back 29.10"
cron_line = "*/5 * * * * cron-fail --after 2 a /h/bin/a >/dev/null 2>&1"

[jobs.b]
line = "b"
cron = "7 3 * * *"
class = "K"
group = "g"
where = "cron"
runs_on = "cron"
argv = ["~/bin/b"]
timeout = 60
after = 1
feature = "f"
owner_facing = false
why = "w"
target = "cron-back 29.10"
cron_line = "7 3 * * * cron-fail b /h/bin/b >/dev/null 2>&1"

[jobs.c]
line = "c"
cron = "*/5 * * * *"
class = "T"
group = "g"
queue = "critical"
where = "brain"
runs_on = "hub"
argv = ["~/bin/c"]
timeout = 60
after = 1
feature = "f"
owner_facing = false
why = "w"
target = "hub move"
cron_line = "*/5 * * * * /h/bin/c"
"""

TOOL = r"""#!/usr/bin/env python3
import json, sys, os
st = os.environ["FAKE_STATE"]
s = json.load(open(st))
s["calls"].append(sys.argv[1:3] + [json.loads(sys.argv[3]).get("state", "") if sys.argv[3] != "{}" else ""])
if sys.argv[2] == "workflow.schedules":
    print(json.dumps({"data": {"schedules": [{"name": "job:" + j, "job": j} for j in s["hub"]]}}))
else:
    b = json.loads(sys.argv[3])
    if b["job"] in s.get("hub_fail", []):
        json.dump(s, open(st, "w")); sys.exit(1)
    if b["state"] == "absent":
        s["hub"] = [j for j in s["hub"] if j != b["job"]]
    elif b["job"] not in s["hub"]:
        s["hub"].append(b["job"])
json.dump(s, open(st, "w"))
"""

ASSISTANT = r"""#!/usr/bin/env python3
import json, sys, os
st = os.environ["FAKE_STATE"]; ct = os.environ["CRON_BACK_CRONTAB"]
s = json.load(open(st)); s["calls"].append(sys.argv[1:3])
json.dump(s, open(st, "w"))
lines = open(ct).read().splitlines()
if sys.argv[2] == "add":
    if sys.argv[3] in s.get("cron_fail", []):
        sys.exit(1)
    lines.append(f"{sys.argv[4]} {sys.argv[5]}  # assistant:{sys.argv[3]}")
elif sys.argv[2] == "rm":
    lines = [x for x in lines if not x.endswith("# assistant:" + sys.argv[3])]
else:
    print("cron check: clean"); sys.exit(0)
open(ct, "w").write("\n".join(lines) + "\n")
"""


@pytest.fixture
def env(tmp_path):
    (tmp_path / "reg.toml").write_text(REG)
    for n, body in (("tool", TOOL), ("assistant", ASSISTANT)):
        p = tmp_path / n
        p.write_text(body.replace("/usr/bin/env python3", sys.executable))
        p.chmod(0o755)
    (tmp_path / "crontab").write_text('MAILTO=""\n')
    (tmp_path / "state.json").write_text(json.dumps({"hub": ["a", "c"], "calls": []}))
    return {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "ASSISTANT_SCHEDULES": str(tmp_path / "reg.toml"),
            "CRON_BACK_TOOL": str(tmp_path / "tool"), "CRON_BACK_ASSISTANT": str(tmp_path / "assistant"),
            "CRON_BACK_CRONTAB": str(tmp_path / "crontab"), "CRON_BACK_STATE": str(tmp_path / "st"),
            "FAKE_STATE": str(tmp_path / "state.json"), "CRON_RUNS_DIR": str(tmp_path / "runs"),
            "CRON_BACK_CRONTAB_BIN": "/bin/true", "CRON_BACK_NO_COMMIT": "1", "CRON_BACK_PACE": "0.001"}  # fmt: skip


def run(env, *args):
    return subprocess.run([sys.executable, str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=60)


def state(env):
    return json.loads(Path(env["FAKE_STATE"]).read_text())


def tags(env):
    return [
        ln.rsplit(":", 1)[1] for ln in Path(env["CRON_BACK_CRONTAB"]).read_text().splitlines() if "# assistant:" in ln
    ]


def test_apply_moves_each_job_hub_first_and_is_idempotent(env):
    assert run(env, "status").returncode == 1  # b is on neither runner before
    r = run(env, "apply")
    assert r.returncode == 0, r.stderr
    s = state(env)
    assert s["hub"] == ["c"] and tags(env) == ["a", "b"]
    assert ["call", "workflow.schedule", "absent"] in s["calls"] and ["cron", "add"] not in s["calls"]  # own write
    n = len(s["calls"])
    assert run(env, "apply").returncode == 0 and tags(env) == ["a", "b"]
    assert [c for c in state(env)["calls"][n:] if c[:2] != ["call", "workflow.schedules"]] == [["cron", "check"]]
    assert run(env, "status").returncode == 0
    assert "MISSING" not in run(env, "verify").stdout


def test_failed_cron_add_puts_the_hub_schedule_back(env):
    env = dict(env, CRON_BACK_CRONTAB_BIN="/bin/false")  # the crontab install fails: old file back, hub back
    r = run(env, "apply", "--only", "a")
    assert r.returncode == 1 and "restored" in r.stderr
    assert "a" in state(env)["hub"] and "a" not in tags(env)


def test_rollback_removes_the_cron_line_before_the_hub_goes_live(env):
    assert run(env, "apply").returncode == 0
    r = run(env, "rollback")
    assert r.returncode == 0, r.stderr
    s = state(env)
    assert sorted(s["hub"]) == ["a", "c"] and tags(env) == ["b"]  # b is class K: stays in cron
    assert ["call", "workflow.schedule", "live"] in s["calls"] and ["cron", "rm"] not in s["calls"]
    assert "stays in cron" in r.stdout
    assert run(env, "rollback").returncode == 0 and sorted(state(env)["hub"]) == ["a", "c"]


def test_only_rejects_unknown_jobs(env):
    r = run(env, "apply", "--only", "c")
    assert r.returncode != 0 and "not a moved job" in (r.stderr + r.stdout)
