"""agent-watch (the zero-token subagent watchdog): classification, stall notes, task notifications, nested agents,
owner progress lines, quiet hours, tg failures, tags, forks, corrupt state / crash / lock, a large project, the
permission-stuck label with redaction, renamed agents, and that no transcript content ever leaks.

Isolated: HOME is tmp_path/home, the project, state, inbox, tg stub and titles file are temp paths (AGENT_WATCH_*,
AGENT_TITLES_FILE), the clock is faked (AGENT_WATCH_NOW; a Monday 10:00 Zurich), the redactor is this tree's
secretscan_py. The repo's agent-watch is staged in a bin dir whose lib/ has the repo's core, agent_titles and
assistant_profile modules (no profile: its fallback zone); nothing from the live ~/bin. Synthetic transcripts come
from agent_watch_fixture.py, and each of their messages carries SECRET_MARK: no output, note, text or state may hold
it (checked when each test ends)."""

import importlib.util
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from core import locks

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "agent_watch_fixture", Path(__file__).with_name("agent_watch_fixture.py")
)
fx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fx)

ZRH = ZoneInfo("Europe/Zurich")


def ep(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=ZRH).timestamp())


BASE = ep("2026-10-12 10:00")  # a Monday, 10:00 Zurich


def m(n: float) -> int:
    """epoch at BASE + n minutes"""
    return int(BASE + n * 60)


def aid(s: str) -> str:
    """aid('b1') -> 'ab100000000000001' (as the shell suite's id())"""
    return f"a{s}{1:0{16 - len(s)}d}"


def stage(d: Path) -> Path:
    b = d / "bin"
    (b / "lib").mkdir(parents=True)
    shutil.copy2(REPO / "scripts" / "agent-watch", b / "agent-watch")
    (b / "lib" / "core").symlink_to(REPO / "core")  # agent_titles reads through core.jsonstate
    (b / "lib" / "agent_titles.py").symlink_to(REPO / "scripts" / "lib" / "agent_titles.py")
    (b / "lib" / "assistant_profile.py").symlink_to(REPO / "scripts" / "lib" / "legacy" / "assistant_profile.py")
    return b


class Box:
    def __init__(self, tmp: Path):
        self.t = tmp
        self.home = tmp / "home"
        self.home.mkdir()
        self.aw = stage(tmp) / "agent-watch"
        self.proj, self.state, self.inbox = tmp / "proj", tmp / "state", tmp / "inbox"
        self.tglog, self.tgsoft, self.tgfail = tmp / "tg.log", tmp / "tg.soft", tmp / "tg-fail"
        self.titles = tmp / "titles.json"
        tg = tmp / "tg"
        tg.write_text(
            f'#!/bin/sh\n[ -e "{self.tgfail}" ] && exit 1\nprintf \'%s\\n\' "$*" >> "{self.tglog}"\n'
            f'printf \'%s\\n\' "${{TG_SOFT:-}}" > "{self.tgsoft}"\n'
        )
        tg.chmod(0o755)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("AGENT_WATCH_", "AGENT_TITLES_", "TG_SOFT"))}
        env.update(
            HOME=str(self.home),
            AGENT_WATCH_PROJECT=str(self.proj),
            AGENT_WATCH_STATE_DIR=str(self.state),
            AGENT_WATCH_INBOX=str(self.inbox),
            AGENT_WATCH_TG=str(tg),
            AGENT_TITLES_FILE=str(self.titles),
            AGENT_WATCH_SECRETSCAN=str(REPO / "scripts" / "secretscan_py"),
        )
        self.env = env
        self.seen: list[str] = []  # every output, note, text and state file (the leak check)
        self.now = m(0)
        self.proj.mkdir()

    # the fixture
    def agent(self, session, a, kind, mtime, *opts):
        fx.main(["agent", str(self.proj), session, a, kind, str(mtime), *map(str, opts)])

    def grow(self, session, a, kind, mtime):
        fx.main(["grow", str(self.proj), session, a, kind, str(mtime)])

    def notif(self, session, a, status, ts, *opts):
        fx.main(["notif", str(self.proj), session, a, status, str(ts), *opts])

    # the tool
    def run(self, *args, now=None, **env):
        e = dict(self.env, AGENT_WATCH_NOW=str(self.now if now is None else now), **env)
        r = subprocess.run([str(self.aw), *args], env=e, capture_output=True, text=True, timeout=30)
        self.seen += [r.stdout, r.stderr]
        return r

    def out(self, *args, **kw) -> str:
        return self.run(*args, **kw).stdout

    def at(self, n):
        self.now = m(n)

    def state_of(self, a) -> str | None:
        for line in self.out("status").splitlines():
            f = line.split()
            if f and f[0] == a:
                return f[1]
        return None

    def status_line(self, a) -> str:
        return next((x for x in self.out("status").splitlines() if a in x), "")

    def notes(self, pat: str = "") -> int:
        return len([p for p in self.inbox.glob("*")]) if not pat else len([p for p in self.inbox.glob(f"*{pat}*")])

    def note(self, pat: str) -> str:
        return "".join(p.read_text() for p in sorted(self.inbox.glob(f"agent-watch-{pat}*")))

    def tgn(self) -> int:
        return len(self.tglog.read_text().splitlines()) if self.tglog.exists() else 0

    def tglast(self) -> str:
        return self.tglog.read_text().splitlines()[-1] if self.tglog.exists() else ""

    def log(self) -> str:
        p = self.state / "agent-watch.log"
        return p.read_text() if p.exists() else ""

    def st(self) -> dict:
        return json.loads((self.state / "state.json").read_text())

    def _collect(self):
        for d in (self.inbox, self.state):
            for p in d.glob("*") if d.is_dir() else ():
                if p.is_file():
                    self.seen.append(p.read_text(errors="replace"))
        if self.tglog.exists():
            self.seen.append(self.tglog.read_text())

    def reset(self):
        self._collect()
        for p in (self.proj, self.state, self.inbox):
            shutil.rmtree(p, ignore_errors=True)
        for p in (self.tglog, self.tgfail):
            p.unlink(missing_ok=True)
        self.proj.mkdir()


@pytest.fixture
def box(tmp_path):
    b = Box(tmp_path)
    yield b
    b._collect()
    leaked = [s for s in b.seen if fx.SECRET_MARK in s]
    assert not leaked, "transcript content reached output, notes, texts or state"
    # the defaults under HOME (where a run writes when an override is not honoured) stay unused
    for p in (".local/state/agent-watch", "inbox", ".local/state/agent-titles"):
        assert not (b.home / p).exists(), p


def test_missing_dirs_and_help(box):
    shutil.rmtree(box.proj)
    r = box.run("run")
    assert (r.returncode, r.stdout + r.stderr) == (0, "")
    assert "(none)" in box.out("status")
    assert not box.inbox.exists()
    assert box.run("help").returncode == 0
    assert box.run("bogus").returncode == 2


def test_classification_one_note_per_stall_and_rearm(box):
    a = box.agent
    a("S1", aid("1"), "tool_use", m(-2), "--start", m(-10), "--desc", "Running one")
    a("S1", aid("2"), "final", m(-30), "--desc", "Finished one")
    a("S1", aid("3"), "interrupted", m(-40))
    a("S1", aid("4"), "tool_result", m(-16), "--start", m(-50), "--desc", "Stuck one")
    a("S1", aid("5"), "thinking", m(-200), "--desc", "Too old")
    a("S1", aid("6"), "final", m(-20), "--trailing-attachment")
    a("S1", aid("7"), "tool_use", m(-1), "--partial")
    a("S1", aid("8"), "final", m(-5), "--big", 700000)
    a("S1", aid("9"), "thinking", m(-3), "--no-meta")
    st = box.out("status")
    assert box.state_of(aid("1")) == "running"  # tool_use, 2 min silent
    assert box.state_of(aid("2")) == "done"  # end_turn text
    assert box.state_of(aid("3")) == "interrupted"
    assert box.state_of(aid("4")) == "stalled"  # 16 min silent
    assert aid("5") not in st  # outside the 2 h window
    assert box.state_of(aid("6")) == "done"  # trailing attachment record skipped
    assert box.state_of(aid("7")) == "running"  # half-written last line ignored
    assert box.state_of(aid("8")) == "done"  # 700 KB final record
    assert box.state_of(aid("9")) == "running"  # no meta.json
    assert "ID" in st
    assert "50m" in next(x for x in st.splitlines() if aid("4") in x)
    assert st.splitlines()[2].split()[0] == aid("4")  # stalled sorts first
    assert not (box.state / "state.json").exists()  # status is read-only
    rows = {r["id"]: r for r in json.loads(box.out("status", "--json"))["agents"]}
    r4 = rows[aid("4")]
    assert (r4["status"], rows[aid("2")]["status"], r4["session"]) == ("stalled", "done", "S1")
    assert sorted(r4) == [
        "depth", "description", "effort", "id", "kind", "last", "parent", "session", "start", "status", "title", "type",
    ]  # fmt: skip
    assert not (box.state / "state.json").exists()  # status --json is read-only

    box.run("run")
    assert (box.notes(), box.notes(f"stalled-{aid('4')}")) == (1, 1)
    note = box.note("stalled-")
    assert aid("4") in note
    assert '"Stuck one"' in note
    assert "no transcript growth for 16 min" in note
    assert "got a tool result, model call pending" in note
    assert "not owner-facing" in note  # untagged
    assert box.tgn() == 0  # untagged agents never text
    box.run("run")
    assert box.notes() == 1  # same run again: no duplicate
    box.at(10)
    box.run("run")
    assert box.notes() == 1
    box.at(20)
    box.run("run")  # the running ones stall later: one note each
    assert [box.notes(), *(box.notes(f"stalled-{aid(i)}") for i in "179")] == [4, 1, 1, 1]
    assert [box.notes(aid(i)) for i in "236"] == [0, 0, 0]  # finished/interrupted: never noted

    # the stall re-arms once the agent grows again
    box.at(21)
    box.grow("S1", aid("4"), "tool_use", m(21))
    box.run("run")
    assert box.state_of(aid("4")) == "running"
    assert f"resumed {aid('4')} (was stalled)" in box.log()
    box.at(30)
    box.run("run")
    assert box.notes(f"stalled-{aid('4')}") == 1  # 9 min silent
    box.at(37)
    box.run("run")
    assert box.notes(f"stalled-{aid('4')}") == 2  # 16 min silent again
    assert "waiting on a tool call" in (box.inbox / f"agent-watch-stalled-{aid('4')}-{m(37)}.md").read_text()
    box.grow("S1", aid("4"), "final", m(38))
    box.at(50)
    box.run("run")
    assert (box.state_of(aid("4")), box.notes(f"stalled-{aid('4')}")) == ("done", 2)


def test_parent_session_task_notifications(box):
    a, n = box.agent, box.notif
    a("S1", aid("b1"), "tool_use", m(-20))
    n("S1", aid("b1"), "killed", m(-20))
    a("S1", aid("b2"), "tool_result", m(-20), "--start", m(-70))
    n("S1", aid("b2"), "completed", m(-60))  # older than its last write: it was resumed
    a("S1", aid("b3"), "tool_use", m(-25))
    n("S1", aid("b3"), "failed", m(-25), "--delivered")  # only the delivered user record
    a("S1", aid("b4"), "tool_use", m(-3))
    n("S1", "bshelltask", "completed", m(-3))  # a background shell task: ignored
    box.run("run")
    assert box.state_of(aid("b1")) == "killed"
    assert box.state_of(aid("b2")) == "stalled"
    assert box.state_of(aid("b3")) == "failed"
    assert (box.notes(), box.notes(aid("b2"))) == (1, 1)  # only the resumed one
    assert box.state_of(aid("b4")) == "running"
    off1 = next(iter(box.st()["offsets"].values()))["off"]
    n("S1", aid("b4"), "stopped", m(-3))
    box.at(1)
    box.run("run")
    assert box.state_of(aid("b4")) == "stopped"  # picked up incrementally
    off2 = next(iter(box.st()["offsets"].values()))["off"]
    assert off2 > off1
    box.grow("S1", aid("b4"), "tool_use", m(2))
    box.at(2)
    box.run("run")
    assert box.state_of(aid("b4")) == "running"  # grows after the notification (SendMessage resume)


def test_nested_agents_whose_parent_ended(box):
    box.agent("S1", aid("p1"), "final", m(-29))
    box.notif("S1", aid("p1"), "stopped", m(-30))
    box.agent("S1", aid("c1"), "tool_use", m(-31), "--parent", aid("p1"))
    box.agent("S1", aid("c2"), "tool_use", m(-31), "--parent", aid("p9"))
    box.agent("S1", aid("c3"), "tool_use", m(-2), "--parent", aid("p1"))
    box.run("run")
    assert box.state_of(aid("c1")) == "orphaned"
    assert box.state_of(aid("c2")) == "stalled"  # parent unknown
    assert box.state_of(aid("c3")) == "running"
    assert (box.notes(aid("c1")), box.notes(aid("c2"))) == (0, 1)


def test_owner_progress_lines(box):
    d1, d2 = aid("d1"), aid("d2")
    box.agent("S1", d1, "tool_use", m(0), "--start", m(0), "--desc", "Build the thing")
    box.agent("S1", d2, "tool_use", m(0), "--start", m(-40), "--desc", "Untagged long one")
    assert box.out("tag", "ad1", "owner", "Fix the printer").strip() == f'tagged {d1} owner: "Fix the printer"'

    def step(n):
        box.at(n)
        box.grow("S1", d1, "tool_use", m(n))
        box.grow("S1", d2, "tool_use", m(n))
        box.run("run")

    step(5)
    step(10)
    assert box.tgn() == 0  # nothing before ~15 min
    step(15)
    assert box.tglast() == "⏳ Fix the printer: still working (15 min)"
    assert box.tgsoft.read_text().strip() == "1"  # non-urgent
    step(20)
    step(25)
    assert box.tgn() == 1
    step(30)
    assert box.tglast() == "⏳ Fix the printer: still working (30 min)"
    box.now = m(44) + 30
    box.grow("S1", d1, "tool_use", box.now)
    box.run("run")
    assert box.tgn() == 3  # cron jitter: 14.5 min later still counts as ~15
    assert "Untagged" not in box.tglog.read_text()
    assert "Fix the printer (⏳10:44)" in box.status_line(d1)
    box.at(62)
    box.run("run")
    assert box.tgn() == 3  # stalled: no progress text
    note = box.note(f"stalled-{d1}")
    assert 'owner-facing, title "Fix the printer"' in note
    assert "tell the owner" in note
    box.grow("S1", d1, "final", m(63))
    box.at(80)
    box.run("run")
    assert (box.tgn(), box.notes(f"stalled-{d1}")) == (3, 1)  # finished: no text, no note
    assert box.out("tag", d2, "owner").strip() == f'tagged {d2} owner: "Untagged long one"'
    assert box.out("untag", d2).strip() == f"untagged {d2}"


def test_quiet_hours(box):
    e1, s = aid("e1"), ep("2026-10-12 23:30")
    box.agent("S1", e1, "tool_use", s, "--start", s, "--desc", "Night job")
    box.run("tag", e1, "owner", "Night job", now=s)

    def qstep(when):
        box.now = ep(when)
        box.grow("S1", e1, "tool_use", box.now)
        box.run("run")

    qstep("2026-10-12 23:50")
    assert box.tgn() == 1  # 23:50 is awake
    for t in ("2026-10-13 00:05", "2026-10-13 03:00", "2026-10-13 07:55"):
        qstep(t)
    assert box.tgn() == 1  # no texts 00:00-08:00
    qstep("2026-10-13 08:00")
    assert box.tglast() == "⏳ Night job: still working (510 min)"


def test_tg_failure(box):
    f1 = aid("f1")
    box.agent("S1", f1, "tool_use", m(0), "--start", m(-20))
    box.run("tag", f1, "owner", "Flaky")
    box.tgfail.touch()
    box.run("run")
    assert box.notes("tg-failed") == 1
    box.at(5)
    box.grow("S1", f1, "tool_use", m(5))
    box.run("run")
    assert (box.notes("tg-failed"), box.tgn()) == (1, 0)  # retried, no second note
    box.tgfail.unlink()
    box.at(6)
    box.grow("S1", f1, "tool_use", m(6))
    box.run("run")
    assert box.tglast() == "⏳ Flaky: still working (26 min)"


def test_tag_errors(box):
    box.agent("S1", "aaa1000000000001x", "tool_use", m(0))
    box.agent("S1", "aaa2000000000001x", "tool_use", m(0))
    assert box.run("tag", "aaa", "owner", "X").returncode == 1  # ambiguous prefix
    assert box.run("tag", "zzzz", "owner", "X").returncode == 1  # unknown prefix
    assert box.run("tag", "x;rm", "owner", "X").returncode == 1  # junk id
    assert box.run("tag", "aaa1", "me", "X").returncode == 2  # unknown role
    r = box.run("tag", "a0123456789abcdef", "owner", "Early")
    assert "no transcript for it yet" in r.stdout + r.stderr
    assert 'a0123456789abcdef  owner  "Early"' in box.out("tags")
    assert box.run("untag", "nope").returncode == 1


def test_same_agent_in_two_session_dirs(box):
    box.agent("OLD", aid("j1"), "final", m(-50))
    box.agent("NEW", aid("j1"), "tool_use", m(-1))
    assert box.out("status").count(aid("j1")) == 1
    assert box.state_of(aid("j1")) == "running"  # the newest copy wins


def test_corrupt_state_crash_note_and_lock(box):
    box.agent("S1", aid("k1"), "tool_use", m(-1))
    box.state.mkdir()
    (box.state / "state.json").write_text("{garbage")
    r = box.run("status")
    assert aid("k1") in r.stdout + r.stderr
    assert box.run("run").returncode == 0
    assert list(box.state.glob("state.json.bad-*"))  # moved aside
    box.st()  # the fresh one is valid JSON
    (box.state / "state.json.tmp").mkdir()  # save_state can't write: a crash
    assert box.run("run").returncode == 1
    assert box.notes("crash") == 1
    box.run("run")
    assert box.notes("crash") == 1  # the same crash again: no second note
    assert "Traceback" in box.log()
    (box.state / "state.json.tmp").rmdir()
    (box.state / "state.json").unlink()
    with locks.hold(box.state / "lock", wait=0) as fd:
        assert fd is not None
        r = box.run("run")
    assert r.returncode == 0  # locked: exits at once
    assert not (box.state / "state.json").exists()  # and did nothing


def test_large_project_is_scanned_incrementally(box):
    """300 old agents, a 20 MB transcript and a 25 MB parent session: the tail-only read and the cached parent
    offset are checked by behaviour (the run's wall time is not: it flaked under load)."""
    fx.main(["filler", str(box.proj), "300", str(m(-3000))])
    l1 = aid("l1")
    box.agent("BIG", l1, "tool_use", m(-1), "--big", 300000)
    p, parent = box.proj / "BIG" / "subagents" / f"agent-{l1}.jsonl", box.proj / "BIG.jsonl"
    body = p.read_text().splitlines()
    pad = json.dumps({"type": "user", "message": {"role": "user", "content": "x" * 20000}})
    p.write_text(body[0] + "\n" + (pad + "\n") * 1000 + "\n".join(body[1:]) + "\n")
    parent.write_text((pad + "\n") * 1250)
    os.utime(p, (m(-1), m(-1)))
    assert box.run("run").returncode == 0
    off = box.st()["offsets"][str(parent)]["off"]
    assert off == parent.stat().st_size  # the parent session read to its end once
    assert box.run("run").returncode == 0
    assert box.st()["offsets"][str(parent)]["off"] == off
    assert box.state_of(l1) == "running"
    assert " f0000" not in box.out("status")  # old filler agents ignored


def _parallel_and_answered(d: Path, t: float, id4: str, id5: str) -> None:
    def iso(x):
        return datetime.fromtimestamp(x, UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def a(tid, cmd, x):
        block = {"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": cmd}}
        return {"type": "assistant", "timestamp": iso(x), "message": {"role": "assistant", "stop_reason": "tool_use",
                                                                       "content": [block]}}  # fmt: skip

    def u(tid, x):
        res = {"type": "tool_result", "tool_use_id": tid, "content": "ok"}
        return {"type": "user", "timestamp": iso(x), "message": {"role": "user", "content": [res]}}

    go = {"type": "user", "timestamp": iso(t), "message": {"role": "user", "content": "go"}}
    hm = [{"type": "thinking", "thinking": "hm"}]
    think = {"type": "assistant", "timestamp": iso(t + 3), "message": {"role": "assistant", "stop_reason": None,
                                                                        "content": hm}}  # fmt: skip
    files = {
        # parallel calls: A answered, B pending
        id4: [
            go,
            a("ta", "sleep 100 # first-call", t + 1),
            a("tb", "cd /x; rm -rf out/* # second-call", t + 1),
            u("ta", t + 101),
        ],
        # answered, then thinking
        id5: [go, a("tc", "echo answered-call", t + 1), u("tc", t + 2), think],
    }
    for k, recs in files.items():
        p = d / f"agent-{k}.jsonl"
        p.write_text("".join(json.dumps(r) + "\n" for r in recs))
        os.utime(p, (t + 101, t + 101))


def test_permission_stuck(box):
    fake = "gh" + "p" + "_" + "abcdefghijklmnopqrstuvwxyz" + "0123456789AB"  # built at runtime: no token literal
    p1, p2, p3, p4, p5 = (aid(f"p{i}") for i in range(1, 6))
    box.agent("P", p1, "tool_use", m(-16), "--start", m(-30), "--desc", "Rm one",
              "--bash-cmd", "D=/x/scan\nrm -rf $D/*; ls $D")  # fmt: skip
    box.agent("P", p2, "tool_use", m(-16), "--start", m(-30),
              "--bash-cmd", f"curl -s -H token={fake} https://example.invalid/{'y' * 120}")  # fmt: skip
    box.agent("P", p3, "tool_use", m(-16), "--start", m(-30))  # a pending non-Bash call
    _parallel_and_answered(box.proj / "P" / "subagents", m(-20), p4, p5)
    box.run("run")
    assert (box.notes(f"permission-stuck-{p1}"), box.notes(f"stalled-{p1}")) == (1, 0)
    n1 = box.note(f"permission-stuck-{p1}")
    assert "permission-stuck (likely approval prompt)" in n1
    assert "`D=/x/scan rm -rf $D/*; ls $D`" in n1  # newlines flattened
    assert f"TaskStop {p1}" in n1
    assert "subagent-bash-hangs-2026-10-16.md" in n1
    assert f"stalled {p1} silent 16m permission-stuck" in box.log()
    n2 = box.note(f"permission-stuck-{p2}")
    assert fake[:6] not in n2  # redacted
    assert "[redacted]" in n2
    c2 = next(x for x in n2.splitlines() if x.startswith("- command"))
    c2 = c2.split("`", 1)[1].rstrip("`")
    assert 40 <= len(c2) <= 83  # cut to 80 chars (+...)
    assert (box.notes(f"stalled-{p3}"), box.notes(f"permission-stuck-{p3}")) == (1, 0)
    n4 = box.note(f"permission-stuck-{p4}")
    assert "second-call" in n4  # the unanswered parallel call is shown
    assert "first-call" not in n4
    assert (box.notes(f"stalled-{p5}"), box.notes(f"permission-stuck-{p5}")) == (1, 0)
    assert "rm -rf" not in (box.state / "state.json").read_text()  # no command text in state


def test_permission_stuck_threshold(box):
    q1, q2 = aid("q1"), aid("q2")
    box.agent("Q", q1, "tool_use", m(-2), "--start", m(-10), "--bash-cmd", "rm -f $T/*")
    box.run("run", AGENT_WATCH_STALL_MIN="1")
    assert (box.notes(f"stalled-{q1}"), box.notes(f"permission-stuck-{q1}")) == (1, 0)  # pending 2 min (< 3)
    box.agent("Q", q2, "tool_use", m(-2), "--start", m(-10), "--bash-cmd", "rm -f $T/*")
    box.run("run", AGENT_WATCH_STALL_MIN="1", AGENT_WATCH_PERM_MIN="1")
    assert box.notes(f"permission-stuck-{q2}") == 1


def test_permission_stuck_redactor_fails(box):
    false = box.t / "false"
    false.write_text("#!/bin/sh\nexit 1\n")
    false.chmod(0o755)
    box.agent("R", aid("r1"), "tool_use", m(-16), "--start", m(-30), "--bash-cmd", "rm -rf $D/* # raw-r1")
    box.run("run", AGENT_WATCH_SECRETSCAN=str(false))
    box.agent("R", aid("r2"), "tool_use", m(-16), "--start", m(-30), "--bash-cmd", "rm -rf $D/* # raw-r2")
    box.run("run", AGENT_WATCH_SECRETSCAN=str(box.t / "nonexistent" / "secretscan"))
    nr = box.note("permission-stuck-")
    assert nr.count("redaction failed") == 2  # both noted with a placeholder
    assert "raw-r" not in nr


def test_renamed_agents(box):
    e1, e2, e3 = aid("e1"), aid("e2"), aid("e3")
    box.agent("S1", e1, "tool_use", m(0), "--start", m(0), "--desc", "Build the thing")
    box.agent("S1", e2, "tool_use", m(0), "--start", m(0), "--desc", "Second one")
    box.agent("S1", e3, "tool_use", m(0), "--start", m(0), "--desc", "Third one")

    def last_col(a):
        return box.status_line(a).split("  ")[-1]

    def titles(**kv):
        box.titles.write_text(json.dumps({k: {"title": v, "t": box.now} for k, v in kv.items()}))

    assert last_col(e1) == "Build the thing"  # no titles file: the spawn description
    titles(**{e1: "Printer fix", e2: "Renamed two"})
    assert "  Printer fix (was: Build the thing)" in box.status_line(e1)
    rows = {r["id"]: r for r in json.loads(box.out("status", "--json"))["agents"]}
    assert (rows[e1]["title"], rows[e1]["description"], rows[e3]["title"]) == (
        "Printer fix", "Build the thing", "Third one",
    )  # fmt: skip
    assert box.out("tag", "ae1", "owner", "Owner tag title").strip() == f'tagged {e1} owner: "Owner tag title"'
    assert box.out("tag", "ae2", "owner").strip() == f'tagged {e2} owner: "Renamed two"'  # the renamed title
    assert box.out("tag", "ae3", "owner").strip() == f'tagged {e3} owner: "Third one"'  # the spawn description
    assert "  Printer fix  " in box.status_line(e1)  # OWNER TAG column: the rename wins over the tag's title
    for n in (5, 10, 15):
        box.at(n)
        for x in (e1, e2, e3):
            box.grow("S1", x, "tool_use", m(n))
        box.run("run")
    tg = box.tglog.read_text()
    assert tg.count("Printer fix: still working (15 min)") == 1
    assert "⏳ Renamed two: still working (15 min)" in tg
    assert "⏳ Third one: still working (15 min)" in tg
    assert "Owner tag title" not in tg
    box.titles.write_text("not json {")
    assert last_col(e1) == "Build the thing"  # broken titles file: no crash
    box.titles.write_text(json.dumps({e1: "flat string", e2: {"title": "   "}}))
    assert [last_col(e1), last_col(e2)] == ["Build the thing", "Second one"]  # malformed entries ignored
    box.at(20)
    box.run("run")
    assert box.notes("crash") == 0
