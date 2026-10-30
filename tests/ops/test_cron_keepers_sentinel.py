"""`assistant cron tag|check` (comment-only, idempotent, backup), `keepers ensure` (restart drill, off switch,
cadence, failure streak -> job.failed) and `hub-sentinel check` against a fake hub (3 min, one push, one note, one
recovery line). Temp HOMEs, stubs for crontab, push and events; never the real hub or a real push."""

from __future__ import annotations

import http.server
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TOOL = REPO / "ops" / "assistant.py"
REG = """schema = 1
tz = "UTC"

[jobs.alpha]
line = "alpha"
cron = "*/5 * * * *"
class = "M"
queue = "fast"
argv = ["~/bin/alpha", "run"]
timeout = 60
after = 0
feature = "t"
why = "test"
target = "hub move"
cron_line = "*/5 * * * * ~/bin/alpha run >/dev/null 2>&1"

[jobs.beta]
line = "beta"
cron = "0 3 * * *"
class = "C"
queue = "fast"
argv = []
shell = true
timeout = 60
after = 0
feature = "t"
why = "test"
target = "hub move"
cron_line = "0 3 * * * echo \\"cue $(date +\\\\%F)\\" > \\"$HOME/inbox/cue-$(date +\\\\%s).md\\""
"""
CRON = """MAILTO=""
# jobs
*/5 * * * * ~/bin/alpha run >/dev/null 2>&1
0 3 * * * echo "cue $(date +\\%F)" > "$HOME/inbox/cue-$(date +\\%s).md"
* * * * * /bin/true  # assistant:other
"""


def test_cron_tag_comment_only_idempotent(env, tmp_path):
    reg = tmp_path / "s.toml"
    reg.write_text(REG)
    ct = env.home / "crontab"
    ct.write_text(CRON)
    x = {"ASSISTANT_SCHEDULES": str(reg)}
    r = env.run("cron", "check", extra=x)
    assert r.returncode == 3 and "3 untracked" in r.stdout and "2 missing" in r.stdout, r.stdout + r.stderr
    r = env.run("cron", "tag", "--dry-run", extra=x)
    assert r.returncode == 0 and "would tag 2" in r.stdout and ct.read_text() == CRON, r.stdout + r.stderr
    r = env.run("cron", "tag", extra=x)
    assert r.returncode == 0, r.stdout + r.stderr
    new = ct.read_text()
    old_l, new_l = CRON.split("\n"), new.split("\n")
    changed = [(a, b) for a, b in zip(old_l, new_l, strict=True) if a != b]
    assert [b[len(a) :] for a, b in changed] == ["  # assistant:alpha", "  # assistant:beta"]
    backups = list(env.state("crontab-backups").iterdir())
    assert len(backups) == 1 and backups[0].read_text() == CRON and (backups[0].stat().st_mode & 0o777) == 0o600
    assert env.cron_log.read_text().strip() == str(ct)
    r = env.run("cron", "tag", extra=x)
    assert r.returncode == 0 and "nothing to tag" in r.stdout and ct.read_text() == new
    r = env.run("cron", "check", "--json", extra=x)
    d = json.loads(r.stdout)
    assert r.returncode == 3 and d["untracked"] == [5] and d["missing"] == [] and d["changed"] == []
    assert env.run("cron", "rm", "alpha", extra=x).returncode == 0  # tagged lines are `assistant cron rm` targets
    assert "alpha" not in ct.read_text()


def test_cron_tag_refuses_a_line_the_shell_would_not_end(env, tmp_path):
    reg = tmp_path / "s.toml"
    reg.write_text(REG.replace("~/bin/alpha run >/dev/null 2>&1", "~/bin/alpha 'run >/dev/null"))
    ct = env.home / "crontab"
    text = "*/5 * * * * ~/bin/alpha 'run >/dev/null\n"
    ct.write_text(text)
    r = env.run("cron", "tag", extra={"ASSISTANT_SCHEDULES": str(reg)})
    assert r.returncode == 0 and "nothing to tag" in r.stdout and ct.read_text() == text


# ------------------------------------------------------------------------------------------------ keepers

KEEPER = """#!/bin/sh
# a fake daemon keeper: `fake ensure` starts `sleep` unless its pid is alive
pidf="$HOME/fake.pid"
[ -n "$FAIL_KEEPER" ] && [ -e "$HOME/fail" ] && { echo "keeper said no" >&2; exit 4; }
if [ -f "$pidf" ] && kill -0 "$(cat "$pidf")" 2>/dev/null; then exit 0; fi
nohup sleep 300 >/dev/null 2>&1 &
echo $! > "$pidf"
echo started >> "$HOME/starts"
"""


def kreg(every: int = 1, after: int = 2, err: str = "fake/cron.err") -> str:
    return f"""schema = 1
tz = "UTC"

[jobs.fake-keeper]
line = "fake-keeper"
cron = "* * * * *"
class = "K"
queue = ""
where = "cron"
argv = ["~/bin/fake", "ensure"]
err = "{err}"
dirs = ["fake"]
timeout = 30
after = {after}
feature = "t"
why = "test"
target = "schedule registry"
keeper = true
every = {every}
cron_line = "* * * * * cron-fail --after {after} fake-keeper ~/bin/fake ensure >/dev/null 2>&1"
"""


@pytest.fixture
def kenv(tmp_path):
    h = tmp_path / "home"
    (h / "bin").mkdir(parents=True)
    (h / "inbox").mkdir()
    (h / "bin" / "fake").write_text(KEEPER)
    (h / "bin" / "fake").chmod(0o755)
    ev = tmp_path / "events-stub"
    ev.write_text(f'#!/bin/sh\necho "$@" >> {tmp_path}/events.log\nexit 0\n')
    ev.chmod(0o755)
    reg = tmp_path / "s.toml"
    reg.write_text(kreg())
    env = {**os.environ, "HOME": str(h), "ASSISTANT_TEST": "1", "ASSISTANT_SCHEDULES": str(reg), "JOB_EVENTS": str(ev),
           "FAIL_KEEPER": "1"}  # fmt: skip
    yield {"home": h, "env": env, "reg": reg, "evlog": tmp_path / "events.log"}
    pid = h / "fake.pid"
    if pid.exists():
        try:
            os.kill(int(pid.read_text()), signal.SIGTERM)
        except (OSError, ValueError):
            pass


def keepers(k, *args):
    return subprocess.run([sys.executable, "-I", str(REPO / "scripts" / "keepers"), *args], env=k["env"],
                          capture_output=True, text=True, timeout=60)  # fmt: skip


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def test_keeper_restart_drill(kenv):
    h = kenv["home"]
    assert keepers(kenv, "ensure").returncode == 0
    pid = int((h / "fake.pid").read_text())
    assert alive(pid)
    assert keepers(kenv, "ensure").returncode == 0  # alive: left alone
    assert int((h / "fake.pid").read_text()) == pid and (h / "starts").read_text().count("started") == 1
    os.kill(pid, signal.SIGKILL)  # the drill: the daemon dies
    for _ in range(50):
        if not alive(pid):
            break
        time.sleep(0.05)
    t0 = time.monotonic()
    assert keepers(kenv, "ensure").returncode == 0
    new = int((h / "fake.pid").read_text())
    assert new != pid and alive(new) and time.monotonic() - t0 < 10
    st = json.loads(keepers(kenv, "status", "--json").stdout)
    assert st[0]["name"] == "fake-keeper" and st[0]["last"]["rc"] == 0 and not st[0]["off"]


def test_keeper_off_switch_and_cadence(kenv):
    h = kenv["home"]
    assert keepers(kenv, "off", "fake-keeper", "--why", "drill").returncode == 0
    assert keepers(kenv, "ensure").returncode == 0 and not (h / "fake.pid").exists()
    assert keepers(kenv, "on", "fake-keeper").returncode == 0
    kenv["reg"].write_text(kreg(every=5))
    off_minute = time.mktime((2026, 10, 23, 10, 3, 0, 0, 0, -1))
    assert keepers(kenv, "ensure", "--now", str(off_minute)).returncode == 0 and not (h / "fake.pid").exists()
    on_minute = time.mktime((2026, 10, 23, 10, 5, 0, 0, 0, -1))
    assert keepers(kenv, "ensure", "--now", str(on_minute)).returncode == 0 and (h / "fake.pid").exists()
    assert keepers(kenv, "off", "nosuch").returncode == 2


def test_keeper_failure_streak_emits_job_failed_then_recovered(kenv):
    h = kenv["home"]
    (h / "fail").write_text("1")
    for _ in range(3):
        assert keepers(kenv, "ensure").returncode == 0
    log = kenv["evlog"].read_text()
    assert log.count("--kind job.failed") == 1 and "fake-keeper failed (hub job)" in log
    (h / "fail").unlink()
    assert keepers(kenv, "ensure").returncode == 0
    assert kenv["evlog"].read_text().count("--kind job.recovered") == 1


@pytest.mark.parametrize(("err", "where"), [("fake/cron.err", "fake/cron.err"), ("out", "keepers/fake-keeper.err")])
def test_keeper_failure_note_names_the_file_that_holds_its_stderr(kenv, err, where):
    """The note's stderr path is where the run's stderr went: the registry's own file, or (old line `cron-fail ...
    >/dev/null 2>&1`, err "out" without a stdout file) keepers/NAME.err as cron-fail kept it, never jobs/NAME.err."""
    h = kenv["home"]
    kenv["reg"].write_text(kreg(err=err))
    (h / "fail").write_text("1")
    for _ in range(2):
        assert keepers(kenv, "ensure").returncode == 0
    assert "keeper said no" in (h / ".local" / "state" / where).read_text()
    notes = list((h / "inbox").glob("fake-keeper-jobfail-*.md"))
    assert len(notes) == 1, notes
    body = notes[0].read_text()
    assert f"Its stderr: ~/.local/state/{where} " in body and "state/jobs/" not in body, body


# ------------------------------------------------------------------------------------------------ hub sentinel


class FakeHub:
    def __init__(self) -> None:
        self.state = {"code": 200}
        st = self.state

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                body = json.dumps({"ok": st["code"] == 200}).encode()
                self.send_response(st["code"])
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):  # quiet
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


@pytest.fixture
def sent(tmp_path):
    h = tmp_path / "home"
    (h / "inbox").mkdir(parents=True)
    push = tmp_path / "push"
    push.write_text(f'#!/bin/sh\nprintf "%s\\n" "$1" >> {tmp_path}/pushes.log\n')
    push.chmod(0o755)
    hub = FakeHub()
    env = {**os.environ, "HOME": str(h), "ASSISTANT_TEST": "1", "HUB_URL": hub.url, "HUB_SENTINEL_PUSH": str(push)}
    yield {"home": h, "env": env, "hub": hub, "pushes": tmp_path / "pushes.log"}
    hub.close()


def sentinel(s, *args, now: float | None = None):
    a = [*args] + (["--now", str(now)] if now is not None else [])
    return subprocess.run([sys.executable, "-I", str(REPO / "scripts" / "hub-sentinel"), *a], env=s["env"],
                          capture_output=True, text=True, timeout=60)  # fmt: skip


def pushes(s) -> list[dict]:
    p = s["pushes"]
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def test_sentinel_down_three_minutes_one_push_one_note_one_recovery(sent):
    t = 1_800_000_000.0
    assert sentinel(sent, "check", now=t).returncode == 0 and pushes(sent) == []
    sent["hub"].state["code"] = 503
    for dt in (60, 120, 180 + 59):  # down from t+60: under 3 min -> silent
        assert sentinel(sent, "check", now=t + dt).returncode == 0
    assert pushes(sent) == [] and not list((sent["home"] / "inbox").iterdir())
    assert sentinel(sent, "check", now=t + 60 + 180).returncode == 0  # 3 min down
    assert [p["title"] for p in pushes(sent)] == ["Hub down"] and pushes(sent)[0]["level"] == "info"
    notes = list((sent["home"] / "inbox").iterdir())
    assert len(notes) == 1 and notes[0].name.startswith("hub-sentinel-") and "not instructions" in notes[0].read_text()
    for dt in (300, 360, 900):  # still down: nothing more
        sentinel(sent, "check", now=t + dt)
    assert len(pushes(sent)) == 1 and len(list((sent["home"] / "inbox").iterdir())) == 1
    st = json.loads(sentinel(sent, "status", "--json").stdout)
    assert st["down_since"] == int(t + 60) and st["alerted"]
    sent["hub"].state["code"] = 200
    assert sentinel(sent, "check", now=t + 960).returncode == 0
    assert [p["title"] for p in pushes(sent)] == ["Hub down", "Hub back"] and "Recovered" in notes[0].read_text()
    sentinel(sent, "check", now=t + 1020)
    assert len(pushes(sent)) == 2 and "down_since" not in json.loads(sentinel(sent, "status", "--json").stdout)


def test_sentinel_short_blip_and_unreachable_and_off(sent):
    t = 1_800_000_000.0
    sent["hub"].state["code"] = 500
    sentinel(sent, "check", now=t)
    sent["hub"].state["code"] = 200
    sentinel(sent, "check", now=t + 120)  # back before 3 min: silent
    assert pushes(sent) == []
    sent["env"]["HUB_URL"] = "http://127.0.0.1:9"  # nothing listens: down
    sentinel(sent, "check", now=t + 200)
    sentinel(sent, "check", now=t + 400)
    assert [p["title"] for p in pushes(sent)] == ["Hub down"]
    cfg = sent["home"] / ".config" / "assistant"
    cfg.mkdir(parents=True)
    (cfg / "hub-sentinel-off").write_text("")
    sent["env"]["HUB_URL"] = sent["hub"].url
    sentinel(sent, "check", now=t + 500)
    assert len(pushes(sent)) == 1  # off: not even the recovery line


CRIT_REG = """schema = 1
tz = "UTC"

[jobs.crit]
line = "crit"
cron = "* * * * *"
class = "X"
group = "critical"
queue = "critical"
runs_on = "hub"
argv = ["~/bin/crit"]
timeout = 30
after = 3
feature = "t"
why = "test"
target = "critical jobs"
cron_line = "* * * * * ~/bin/crit >/dev/null 2>&1"

[jobs.crit5]
line = "crit5"
cron = "*/5 * * * *"
class = "X"
group = "critical"
queue = "critical"
runs_on = "hub"
argv = ["~/bin/crit5"]
timeout = 30
after = 3
feature = "t"
why = "test"
target = "critical jobs"
cron_line = "*/5 * * * * ~/bin/crit5 >/dev/null 2>&1"

[jobs.oncron]
line = "oncron"
cron = "* * * * *"
class = "X"
group = "critical"
queue = "critical"
runs_on = "cron"
argv = ["~/bin/oncron"]
timeout = 30
after = 3
feature = "t"
why = "test"
target = "critical jobs"
cron_line = "* * * * * ~/bin/oncron >/dev/null 2>&1"

[jobs.watchy]
line = "watchy"
cron = "* * * * *"
class = "W"
group = "watch"
queue = "fast"
runs_on = "hub"
argv = ["~/bin/watchy"]
timeout = 30
after = 3
feature = "t"
why = "test"
target = "critical jobs"
cron_line = "* * * * * ~/bin/watchy >/dev/null 2>&1"
"""


def test_sentinel_runs_the_critical_hub_jobs_while_the_hub_is_down(sent, tmp_path):
    """Hub down >= 3 min -> the critical hub jobs due this minute run from here, each slot once; not the cron ones,
    not other groups, not before 3 min, not once the hub is back; the off switch stops them."""
    h = sent["home"]
    (h / "bin").mkdir()
    for n in ("crit", "crit5", "oncron", "watchy"):
        f = h / "bin" / n
        f.write_text(f'#!/bin/sh\necho "{n} $HUB_JOB $HUB_SENTINEL_SLOT" >> "$HOME/ran.log"\n')
        f.chmod(0o755)
    reg = tmp_path / "schedules.toml"
    reg.write_text(CRIT_REG)
    sent["env"].update(ASSISTANT_SCHEDULES=str(reg), HUB_SENTINEL_JOBS_SPAWN="inline", TZ="UTC")

    def ran() -> list[str]:
        p = h / "ran.log"
        return p.read_text().split("\n")[:-1] if p.exists() else []

    t = 1_800_000_000.0 // 300 * 300  # a */5 minute
    sent["hub"].state["code"] = 503
    for dt in (0, 60, 120):  # under 3 min: nothing runs
        assert sentinel(sent, "check", now=t + dt).returncode == 0
    assert ran() == []
    sentinel(sent, "check", now=t + 180)
    sentinel(sent, "check", now=t + 190)  # same minute again: once
    assert ran() == [f"crit crit {int(t + 180)}"]
    sentinel(sent, "check", now=t + 300)  # crit5's minute
    assert sorted(ran()[1:]) == [f"crit crit {int(t + 300)}", f"crit5 crit5 {int(t + 300)}"]
    st = json.loads(sentinel(sent, "status", "--json").stdout)
    assert st["down_since"] == int(t)
    assert json.loads((h / ".local" / "state" / "hub-sentinel" / "jobs.json").read_text())["runs"] == 3
    assert "critical jobs: 3 run(s)" in sentinel(sent, "status").stdout
    cfg = h / ".config" / "assistant"
    cfg.mkdir(parents=True)
    (cfg / "hub-sentinel-jobs-off").write_text("")
    sentinel(sent, "check", now=t + 360)
    assert len(ran()) == 3  # jobs off: the probe still runs
    (cfg / "hub-sentinel-jobs-off").unlink()
    sent["hub"].state["code"] = 200
    sentinel(sent, "check", now=t + 420)  # back: nothing from here
    assert len(ran()) == 3
    note = next((h / "inbox").iterdir()).read_text()
    assert "Critical jobs: 3 run(s) from here." in note
    # only the critical hub jobs may run through run-job
    assert sentinel(sent, "run-job", "oncron", "--slot", "1").returncode == 2
    assert len(ran()) == 3
