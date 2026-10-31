"""The schedule registry (core/schedules.toml, every periodic job as data) against a crontab, the comment-only tag
edit, the failure streaks (core.jobfail -> job.failed / job.recovered in the ledger and the agenda), the registry-wide
job executor (workflow.job). Temp HOMEs only; the events CLI runs from the repo on its local SQLite store."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from core import jobfail, schedules, watchjobs  # noqa: E402

EVENTS = REPO / "scripts" / "events"


def crontab_of(reg: schedules.Registry, tagged: bool = True, every: bool = False) -> str:
    """The crontab the registry describes (one line per tag, in file order), with or without the tags; every = also
    the lines of jobs that run elsewhere now (hub, keepers, planned), as each cron_line was."""
    out = ['MAILTO=""', "PATH=/usr/bin:/bin", "# a comment line stays", ""]
    for tag, es in reg.lines.items():
        if es[0].runs_on != "cron" and not every:
            continue
        out.append(es[0].cron_line + (f"  # assistant:{tag}" if tagged else ""))
        out.append("")
    return "\n".join(out) + "\n"


@pytest.fixture(scope="module")
def reg() -> schedules.Registry:
    return schedules.load()


def cron_line_count(reg: schedules.Registry) -> int:
    return sum(1 for es in reg.lines.values() if es[0].runs_on == "cron")


def test_registry_census_shape(reg):
    """The census as data: every entry whatever runs it now (the 135 census lines / 138 jobs + the registry's
    keepers, hub-sentinel and drill-fail, the hub reminders' remind-hub, the event workflows' eventwf-sync +
    eventwf-fallback, the session rotation's rotate, the loop's slo-cache, media-remux, and the 5 lines registered
    after the census: voice-inbox, phone-inbox, ci-nightly (K), media-digest-daily/-weekly (M), then usage-watch (K)
    and board-eval (M), minus board-done, tasks-sync and the retired board-reality, remind-due, practice,
    readlater-update and browser-update (removed; Chrome lives in the hub's browser image); dayline (M) is back with
    the restored dashboard (30.10), plus the hourly scorecard (M, 30.10), the land queue's worker (K, 30.10) and
    the nightly arr-restart (M) and media-remux-tv (M); a new registry entry updates these counts).
    Which runner has a job (cron, hub, keepers, window) moves with each switch-over, so that split is checked for
    consistency, not pinned."""
    # + media-remux-tv (M, 31.10)
    # cron-back 29.10: + cron-runs (K), and the two shared lines (damp/window-watch, scraper-canary/deadlines-validate)
    # are one line per job again
    assert len(reg.lines) == 150 and len(reg.jobs) == 150
    by_class: dict[str, set] = {}
    for e in reg.jobs.values():
        by_class.setdefault(e.cls, set()).add(e.line)
    # plan §6.1, lines per class (+ keepers, hub-sentinel, eventwf-fallback, voice-inbox, phone-inbox, ci-nightly and
    # usage-watch in K, drill-fail, the two media digests and board-eval in M; no R line is left)
    assert {c: len(v) for c, v in by_class.items()} == {"W": 11, "X": 4, "T": 10, "C": 8, "V": 11, "M": 86,
                                                        "K": 20}  # fmt: skip
    assert sorted(e.name for e in reg.keepers()) == ["browser-ensure", "cputemp-log", "eventwf-fallback", "flowd",
                                                     "hub-feed", "llm-idle-check", "tg-adapter-ensure-live",
                                                     "tool-worker",
                                                     "whisper-server"]  # fmt: skip
    # runs_on: the drill alone has no runner; `keepers` runs only keepers, and only while its own line is in cron; a
    # K job never moves to the hub; a hub job is one a hub schedule can run (movable, on a queue)
    assert [e.name for e in reg.jobs.values() if e.runs_on == "planned"] == ["drill-fail"]
    for e in reg.jobs.values():
        if e.runs_on == "keepers":
            assert e.keeper and reg.jobs["keepers"].runs_on == "cron", e.name
        if e.keeper:
            assert e.runs_on in ("cron", "keepers") and e.cls == "K" and e.every > 0, e.name
        if e.cls == "K":
            assert e.runs_on in ("cron", "keepers", "retired"), e.name
        if e.runs_on == "hub":
            assert e.movable() is None and e.queue_name, e.name
    assert reg.jobs["keepers"].runs_on == reg.jobs["hub-sentinel"].runs_on == "cron"
    # the cron line count drops with every switch-over (the hub moves); the K lines always stay (plan end state: 5)
    k_lines = {e.line for e in reg.jobs.values() if e.cls == "K" and e.runs_on == "cron"}
    assert len(k_lines) >= 5
    assert len({e.line for e in reg.jobs.values() if e.runs_on == "cron"}) == cron_line_count(reg) >= len(k_lines)
    for e in reg.jobs.values():
        assert e.why and e.target and e.feature
        assert (e.queue == "") == (e.cls in ("K", "R"))
        if e.cls in ("X", "T"):
            assert e.queue == "critical", e.name
        if e.movable() is None:
            assert e.argv and not e.shell
    assert sum(n for _, n in schedules.QUEUES.values()) == 8


def test_parity_tag_and_idempotence(reg):
    n = cron_line_count(reg)
    tagged = crontab_of(reg)
    p = schedules.parity(tagged, reg)
    assert p.clean and p.active == n and p.tagged == n
    plain = crontab_of(reg, tagged=False)
    p0 = schedules.parity(plain, reg)
    assert len(p0.untracked) == n and len(p0.missing) == n
    new, done = schedules.tag_text(plain, reg)
    assert len(done) == n and new == tagged
    assert schedules.comment_only(plain, new) == []
    assert schedules.tag_text(new, reg) == (new, [])  # idempotent: a second run changes nothing
    # the byte diff touches comments only: dropping the tags gives back the old bytes
    assert "\n".join(schedules.TAG_RE.sub("", ln) for ln in new.split("\n")) == plain
    # a job that runs elsewhere now (hub, keepers) keeps no cron line: its old line back in cron = two runners
    full = schedules.parity(crontab_of(reg, every=True), reg)
    elsewhere = sorted(t for t, es in reg.lines.items() if es[0].runs_on != "cron")
    assert sorted(t for _, t, _ in full.changed) == elsewhere and not full.missing and not full.untracked
    assert all("two runners" in why for _, _, why in full.changed)
    # untagged, `assistant cron tag` leaves it untracked: it tags only the lines cron still runs
    _, done_all = schedules.tag_text(crontab_of(reg, tagged=False, every=True), reg)
    assert sorted(t for _, t in done_all) == sorted(t for t, es in reg.lines.items() if es[0].runs_on == "cron")


def test_parity_reports_untracked_missing_changed(reg):
    text = crontab_of(reg)
    lines = text.split("\n")
    # two K lines (they stay in cron whatever moves to the hub)
    i = next(k for k, ln in enumerate(lines) if ln.endswith("# assistant:brain-backup"))
    lines[i] = lines[i].replace("47 3 * * *", "49 3 * * *", 1)  # schedule edited by hand
    j = next(k for k, ln in enumerate(lines) if ln.endswith("# assistant:brain-backup-verify"))
    del lines[j]  # a registry line gone from cron
    lines.append("*/3 * * * * /bin/true")  # a line nobody registered
    p = schedules.parity("\n".join(lines), reg)
    assert [t for _, t, _ in p.changed] == ["brain-backup"] and p.changed[0][2] == "schedule differs"
    assert p.missing == ["brain-backup-verify"] and [r for _, r in p.untracked] == ["*/3 * * * * /bin/true"]
    assert not p.clean


@pytest.mark.parametrize(
    ("cmd", "ok"),
    [('echo "a # b" > x', True), ("echo 'x", False), ("echo a\\", False), ('x "$(date +\\%F)" >> y 2>&1', True),
     ("a %b", False), ("cat <<EOF", False), ("x $(y 'a)' b)", True), ("f() { echo; }", True), ("a ) b", False),
     ("echo ok # already a comment", True)],
)  # fmt: skip
def test_end_state(cmd, ok):
    assert schedules.end_state_ok(cmd) is ok


def test_comment_only_refuses_real_edits():
    old = "* * * * * /bin/a\n"
    assert schedules.comment_only(old, "* * * * * /bin/b  # assistant:a\n")
    assert schedules.comment_only(old, "* * * * * /bin/a\n# x\n")
    assert schedules.comment_only("* * * * * echo 'x\n", "* * * * * echo 'x  # assistant:a\n")
    assert schedules.comment_only(old, "* * * * * /bin/a  # assistant:a\n") == []


def test_broken_registry_is_refused(tmp_path):
    bad = tmp_path / "s.toml"
    bad.write_text('schema = 1\n[jobs.x]\ncron = "* * *"\nclass = "M"\ncron_line = "* * * x"\n')
    with pytest.raises(schedules.RegistryError):
        schedules.load(bad)


def test_watchjobs_is_the_registry_view(reg):
    assert sorted(watchjobs.JOBS) == sorted(e.name for e in reg.jobs.values() if e.cls == "W")
    tw = watchjobs.JOBS["train-watch"]
    assert tw.argv[:3] == ("timeout", "240", "~/bin/train-watch") and tw.after == 3 and tw.queue == "jobs-fast"
    assert all("cron-fail" not in j.argv for j in watchjobs.JOBS.values())
    lines = watchjobs.cron_lines(crontab_of(reg, every=True))  # cron-back 29.10: one line each again
    assert lines["damp-watch"] and lines["window-watch"] and lines["damp-watch"][0][0] != lines["window-watch"][0][0]
    with pytest.raises(ValueError):
        watchjobs.runnable("bills-week")  # a shell line: needs a wrapper first
    with pytest.raises(ValueError):
        watchjobs.runnable("tool-worker")  # K: stays in cron
    assert watchjobs.runnable("vault-git-sync").env == (("CRON", "1"),)


# ------------------------------------------------------------------------------------------------ failure streaks


def py_shim(script: Path, d: Path) -> Path:
    """An executable that runs a repo script with this interpreter (the hub runner has no /usr/bin/python3 for the
    scripts' shebang)."""
    p = d / f"{script.name}-py"
    p.write_text(f'#!/bin/sh\nexec "{sys.executable}" -I "{script}" "$@"\n')
    p.chmod(0o755)
    return p


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / "inbox").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("ASSISTANT_TEST", "1")
    monkeypatch.setenv("JOB_EVENTS", str(py_shim(EVENTS, tmp_path)))
    for k in ("HUB_URL", "HUB_SCHEMA", "ASSISTANT_KINDS"):
        monkeypatch.delenv(k, raising=False)
    return h


def events(home: Path, *args: str) -> str:
    r = subprocess.run([sys.executable, "-I", str(EVENTS), *args], capture_output=True, text=True, timeout=60,
                       env={**os.environ, "HOME": str(home)}, check=False)  # fmt: skip
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_streak_after_ok_and_recovery(home):
    t = 1_800_000_000
    assert jobfail.record("j1", 3, after=2, ok=(3,), now=t) is None  # --ok 3: a good run
    assert jobfail.record("j1", 1, after=2, now=t + 60) is None  # 1st failure: counted, silent
    assert jobfail.record("j1", 1, after=2, now=t + 120) == "job.failed"  # the after-th: one event
    assert jobfail.record("j1", 2, after=2, now=t + 180) is None  # same streak: silent
    rows = json.loads(events(home, "list", "--all", "--json"))
    failed = [r for r in rows if r["kind"] == "job.failed"]
    assert len(failed) == 1 and failed[0]["payload"]["job"] == "j1" and failed[0]["payload"]["runs"] == 2
    note = home / "inbox" / failed[0]["idem_key"]
    assert note.exists() and "Data from" not in note.read_text() and "not instructions" in note.read_text()
    # the agenda of the next beat names it
    ag = events(home, "agenda")
    assert "j1 failed (hub job)" in ag
    assert jobfail.record("j1", 0, after=2, now=t + 240) == "job.recovered"
    assert "Recovered" in note.read_text()
    rows = json.loads(events(home, "list", "--all", "--json"))
    assert [r["kind"] for r in rows if r["kind"].startswith("job.")].count("job.recovered") == 1
    assert jobfail.streak("j1") == {}
    assert jobfail.record("j1", 0, after=2, now=t + 300) is None  # no streak: nothing


def test_a_slow_ledger_logs_a_timeout_not_a_missing_events(home, monkeypatch):
    """28.10 18:46 (host load ~150): `events emit` outran its 20 s and emit-errors.log said "exit 127" (not found)."""

    def slow(*a, **kw):
        raise subprocess.TimeoutExpired(a[0], 20)

    run = subprocess.run
    monkeypatch.setattr(jobfail.subprocess, "run", slow)
    assert jobfail._run(home, ["emit"]) == 124
    monkeypatch.setattr(jobfail.subprocess, "run", run)
    monkeypatch.setenv("JOB_EVENTS", str(home / "no-such-events"))
    assert jobfail._run(home, ["emit"]) == 127


def test_after_zero_never_emits(home):
    for i in range(5):
        assert jobfail.record("quiet", 1, after=0, now=1_800_000_000 + i) is None
    assert not list((home / "inbox").iterdir())


def test_executor_forced_failure_reaches_the_agenda(home, monkeypatch):
    """workflow.job live on a registry job whose script fails `after` times: one job.failed in the agenda."""
    from tools.domains import workflow as W

    (home / "bin").mkdir()
    s = home / "bin" / "fx-watch"
    s.write_text("#!/bin/sh\necho boom >&2\nexit 7\n")
    s.chmod(0o755)
    j = watchjobs.JOBS["fx-watch"]
    assert j.after == 3
    outs = [W.job(W.JobIn(name="fx-watch", mode="live")) for _ in range(j.after)]
    assert [o.exit for o in outs] == [7, 7, 7] and [o.event for o in outs] == [None, None, "job.failed"]
    assert (home / ".local/state/jobs/fx-watch.err").read_text().strip() == "boom"
    assert "fx-watch failed (hub job)" in events(home, "agenda")
    s.write_text("#!/bin/sh\nexit 0\n")
    assert W.job(W.JobIn(name="fx-watch", mode="live")).event == "job.recovered"
    r = W.job(W.JobIn(name="vault-git-sync"))
    assert r.mode == "shadow" and r.would_run.startswith("~/bin/vault-git-sync")
    from tools.core import ToolError

    with pytest.raises(ToolError) as e:
        W.job(W.JobIn(name="bills-week", mode="live"))
    assert e.value.code == "invalid_args"


def test_registry_zone_is_the_owners_unless_the_file_names_one(tmp_path, monkeypatch):
    body = (
        '[jobs.x]\nline = "x"\ncron = "0 3 * * *"\nclass = "M"\nqueue = "slow"\nwhere = "brain"\nruns_on = "cron"\n'
        'argv = ["~/bin/x"]\ncron_line = "0 3 * * * x"\n'
    )
    f = tmp_path / "s.toml"
    f.write_text("schema = 1\n" + body)
    monkeypatch.setenv("ASSISTANT_TZ", "America/New_York")
    assert schedules.load(f).tz == "America/New_York"  # no zone in the file: the owner's (core.todo.owner_tz)
    f.write_text('schema = 1\ntz = "Asia/Tokyo"\n' + body)
    assert schedules.load(f).tz == "Asia/Tokyo"
    assert "tz" not in tomllib.loads(schedules.DEFAULT.read_text())  # the repo's registry names no zone
