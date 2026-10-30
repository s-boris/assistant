#!/usr/bin/env python3
"""Offline tests for ~/bin/dash-growth (the dash suite's growth case covers last-commit-wins and the vault repo).

Fixture work trees and brain repos in a temp dir, a pinned clock (DASH_GROWTH_NOW), every path via DASH_GROWTH_*:
  1. normal output shape: parts, live today point, loc per part, counts, langs, claude_md_kb, --show
  2. missing / partial inputs: no tree, no brain, empty brain repo, corrupt or half-valid state file, odd dash data
  3. empty data: an empty tree gives one all-zero live day
  4. DST days: commits just after local midnight on both switch nights land on the local day
  5. month and year boundaries: days carried forward across the turn, a live point that went stale is rebuilt
  6. --if-stale on the pinned clock
  7. privacy: the output is numbers, dates and fixed labels only (no file names, no file contents)
  8. the monorepo (~/opt/assistant): live and from its own git history; tests/ and hooks/ in their parts, the rest
     with the scripts; a managed ~/bin copy is counted once (as a script, not as lines again)
Run: ~/opt/dash-growth-tests/run.sh      GROWTH_BIN=/path/to/copy tests a copy before installing.
"""

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from zoneinfo import ZoneInfo

REAL_HOME = os.path.expanduser("~")
GB = os.environ.get("GROWTH_BIN", os.path.join(REAL_HOME, "bin", "dash-growth"))
HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE = next(
    (
        p
        for p in (
            os.path.join(HERE, "..", "profile-tests", "fixtures", "profile.json"),
            os.path.join(HERE, "..", "profile", "fixtures", "profile.json"),
        )
        if os.path.isfile(p)
    ),
    None,
)
Z = ZoneInfo("Europe/Zurich")
U = dt.UTC
FAILS, PASSES = [], 0
PRIVATE = [
    "anna.planted@example.org",
    "CH93 0076 2011 6238 5295 7",
    "Plantedstrasse 12",
    "secret-script-name",
    "private-note",
    "Zoltanina",
]


def ok(cond, name):
    global PASSES
    if cond:
        PASSES += 1
    else:
        FAILS.append(name)
        print("FAIL", name)


def w(base, rel, text, mode=0o644):
    p = os.path.join(base, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(text)
    os.chmod(p, mode)


class Case:
    def __init__(self, root):
        self.root = root
        self.tree = os.path.join(root, "home")
        self.brain = os.path.join(root, "brain.git")
        self.out = os.path.join(root, "state", "dash-growth.json")
        self.dash = os.path.join(root, "state", "dash", "data.json")
        os.makedirs(self.tree)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("DASH_GROWTH_")}
        self.env.update(
            HOME=self.tree,
            DASH_GROWTH_OUT=self.out,
            DASH_GROWTH_BRAIN=self.brain,
            DASH_GROWTH_TREE=self.tree,
            DASH_GROWTH_VAULT=os.path.join(root, "no-vault"),
            DASH_GROWTH_DASHDATA=self.dash,
        )
        if PROFILE:
            self.env["ASSISTANT_PROFILE"] = os.path.abspath(PROFILE)

    def git_init(self):
        subprocess.run(["git", "init", "-q", "--bare", self.brain], check=True, capture_output=True)

    def commit(self, when):
        """Commit the whole tree at instant `when` (aware datetime)."""
        stamp = when.isoformat()
        env = dict(
            os.environ,
            GIT_AUTHOR_DATE=stamp,
            GIT_COMMITTER_DATE=stamp,
            GIT_AUTHOR_NAME="t",
            GIT_COMMITTER_NAME="t",
            GIT_AUTHOR_EMAIL="t@example.invalid",
            GIT_COMMITTER_EMAIL="t@example.invalid",
        )
        g = ["git", "--git-dir", self.brain, "--work-tree", self.tree]
        subprocess.run(g + ["add", "-A"], env=env, check=True, capture_output=True, cwd=self.tree)
        subprocess.run(
            g + ["commit", "-q", "--allow-empty", "-m", "snap"], env=env, check=True, capture_output=True, cwd=self.tree
        )

    def run(self, now, *args):
        e = dict(self.env, DASH_GROWTH_NOW=now.isoformat())
        return subprocess.run(
            [GB, *args], capture_output=True, text=True, env=e, timeout=120, stdin=subprocess.DEVNULL, cwd=self.root
        )

    def state(self):
        try:
            with open(self.out) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None


def days_of(st):
    return {d["date"]: d for d in (st or {}).get("days", [])}


def basic_tree(c, scripts=2):
    for i in range(scripts):
        w(
            c.tree, f"bin/tool{i}", f"#!/usr/bin/env python3\nprint({i})\n\nprint(2)\n", 0o755
        )  # distinct: identical files count once
    w(c.tree, "bin/helper.sh", "echo hi\n", 0o644)  # not executable: lines yes, script count no
    w(c.tree, ".claude/skills/demo/SKILL.md", "---\nname: demo\n---\nUse it.\n")
    w(c.tree, ".claude/agents/helper.md", "agent prompt\n")
    w(c.tree, "workspace/CLAUDE.md", "x" * 2048 + "\n")
    w(c.tree, "opt/demo-tests/test_demo.py", "assert True\n")
    w(c.tree, "crontab", "SHELL=/bin/bash\n# comment\n*/5 * * * * a\n0 3 * * * b\n")


# ------------------------------------------------------------------------------------------- 8. monorepo
def test_mono(root):
    c = Case(root)
    mono = os.path.join(c.tree, "opt", "assistant")
    c.env["DASH_GROWTH_MONO"] = mono
    c.git_init()
    w(c.tree, "bin/tool0", "#!/usr/bin/env python3\nprint(0)\n", 0o755)  # 2 lines, unmanaged
    w(c.tree, "workspace/CLAUDE.md", "x\n")
    c.commit(dt.datetime(2026, 10, 27, 12, 0, tzinfo=Z))
    body = "import os\nprint(1)\nprint(2)\n"
    w(mono, "scripts/mtool", "#!/usr/bin/env python3\n" + body, 0o755)  # 4 lines in the repo
    w(mono, "tests/legacy/mtool/test_mtool.py", "assert True\nassert 1\n")  # 2 test lines
    w(mono, "hooks/guard.py", "x = 1\n")  # 1 hook line
    w(mono, "README.md", "# docs are not code\n")
    w(mono, "tests/fixtures/data/big.py", "skipped = 1\n")  # data/ is skipped everywhere
    genv = dict(
        os.environ,
        GIT_AUTHOR_NAME="t",
        GIT_COMMITTER_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.invalid",
        GIT_COMMITTER_EMAIL="t@example.invalid",
        GIT_AUTHOR_DATE="2026-10-28T12:00:00+01:00",
        GIT_COMMITTER_DATE="2026-10-28T12:00:00+01:00",
    )
    for cmd in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "mono"]):
        subprocess.run(["git", "-C", mono] + cmd, env=genv, check=True, capture_output=True)
    w(
        c.tree, "bin/mtool", "#!/usr/bin/env python3\n# managed: ~/opt/assistant — edit there\n" + body, 0o755
    )  # the installed copy
    r = c.run(dt.datetime(2026, 10, 29, 10, 0, tzinfo=Z), "--backfill")
    ok(r.returncode == 0, f"mono: --backfill exits 0 ({r.stderr[-300:]})")
    days = days_of(c.state())
    live, d28, d27 = days.get("2026-10-29") or {}, days.get("2026-10-28") or {}, days.get("2026-10-27") or {}
    ok(
        live.get("loc", {}).get("scripts") == 2 + 4,
        f"mono: live scripts = ~/bin + the monorepo, managed copy once ({live.get('loc')})",
    )
    ok(
        live.get("loc", {}).get("tests") == 2 and live.get("loc", {}).get("hooks") == 1,
        "mono: its tests/ and hooks/ in their parts",
    )
    ok(
        live.get("counts", {}).get("scripts") == 2,
        f"mono: the Scripts count stays ~/bin's executables ({live.get('counts')})",
    )
    ok(
        d27.get("loc", {}).get("scripts") == 2,
        f"mono: before the monorepo's first commit only ~/bin ({d27.get('loc')})",
    )
    ok(
        d28.get("loc", {}).get("scripts") == 2 + 4 and d28.get("loc", {}).get("tests") == 2,
        f"mono: past days from the monorepo's own git history ({d28.get('loc')})",
    )
    ok(
        any(p["key"] == "scripts" and "monorepo" in p["label"] for p in c.state().get("parts", [])),
        "mono: the scripts part says it holds the monorepo",
    )


# ------------------------------------------------------------------------------------------- 1. shape
def test_shape(root):
    c = Case(root)
    basic_tree(c)
    now = dt.datetime(2026, 10, 18, 11, 0, tzinfo=Z)
    r = c.run(now)
    st = c.state()
    ok(r.returncode == 0 and isinstance(st, dict), f"shape: exits 0 and writes the state ({r.stderr[-300:]})")
    if not st:
        return
    ok(
        st["version"] == 1 and st["updated"] == "2026-10-18 11:00" and st["updated_epoch"] == int(now.timestamp()),
        "shape: version, updated and updated_epoch from the pinned clock",
    )
    ok(
        [p["key"] for p in st["parts"]] == ["scripts", "vault", "tests", "skills", "agents", "hooks", "notes", "other"],
        "shape: parts in stack order",
    )
    t = days_of(st).get("2026-10-18") or {}
    ok(
        list(days_of(st)) == ["2026-10-18"] and t.get("source") == "live" and t.get("at") == "2026-10-18 11:00",
        "shape: no git history -> one live point for today",
    )
    ok(
        t.get("loc", {}).get("scripts") == 3 * 2 + 1
        and t["loc"]["skills"] == 4
        and t["loc"]["agents"] == 1
        and t["loc"]["tests"] == 1
        and t["loc"]["notes"] == 1
        and t["loc_total"] == sum(t["loc"].values()),
        f"shape: non-blank lines per part ({t.get('loc')})",
    )
    ok(t.get("counts") == {"scripts": 2, "skills": 1, "agents": 1, "cron": 2}, f"shape: counts ({t.get('counts')})")
    ok(t.get("claude_md_kb") == 2.0, "shape: CLAUDE.md size in KB")
    ok(
        t.get("langs", {}).get("Python", 0) == 7
        and t["langs"].get("Shell") == 1
        and t["langs"].get("Markdown (prompts)") == 6,
        f"shape: lines by language ({t.get('langs')})",
    )
    r = c.run(now, "--show")
    ok(
        r.returncode == 0 and r.stdout.startswith("2026-10-18 live") and "cron 2" in r.stdout,
        "--show: one line per day",
    )
    r2 = c.run(now, "--stdout")
    ok(
        r2.returncode == 0 and json.loads(r2.stdout)["days"][-1]["loc_total"] == t["loc_total"],
        "--stdout: same point, printed",
    )


# ------------------------------------------------------------------------------------------- 2+3. missing / empty
def test_missing(root):
    now = dt.datetime(2026, 10, 18, 9, 0, tzinfo=Z)
    c = Case(os.path.join(root, "empty"))
    r = c.run(now)
    st = c.state()
    d = days_of(st).get("2026-10-18") or {}
    ok(
        r.returncode == 0
        and list(days_of(st)) == ["2026-10-18"]
        and d.get("loc_total") == 0
        and d.get("counts") == {"scripts": 0, "skills": 0, "agents": 0, "cron": 0}
        and d.get("claude_md_kb") is None
        and st.get("activity") == {},
        f"empty: no tree, no brain, no dash data -> one zero day ({r.stderr[-200:]})",
    )
    r = c.run(now, "--backfill")
    ok(
        r.returncode != 0 and "Traceback" not in r.stderr and "backfill failed" in r.stderr,
        f"missing: --backfill without a brain repo fails with one clear line (rc {r.returncode})",
    )
    c = Case(os.path.join(root, "emptyrepo"))
    basic_tree(c)
    c.git_init()
    r = c.run(now)
    ok(
        r.returncode == 0 and list(days_of(c.state())) == ["2026-10-18"],
        "missing: a brain repo without commits -> today only",
    )
    # corrupt and half-valid state files
    for name, text in (
        ("garbage", "{not json"),
        ("list", "[1, 2]"),
        ("wrong-version", '{"version": 9, "days": [{"date": "2026-01-01"}]}'),
        (
            "nulls",
            json.dumps(
                {
                    "version": 1,
                    "days": [
                        None,
                        {"source": "git"},
                        {"date": "yesterday"},
                        7,
                        {"date": "2026-10-17", "source": "git", "loc_total": 5},
                    ],
                    "activity": {"2026-10-16": None, "2026-10-15": {"heartbeats": 2}},
                }
            ),
        ),
        ("days-dict", json.dumps({"version": 1, "days": {"2026-10-17": {}}, "activity": []})),
    ):
        c = Case(os.path.join(root, name))
        basic_tree(c)
        w(c.root, "state/dash-growth.json", text)
        r = c.run(now)
        st = c.state()
        ok(
            r.returncode == 0
            and "2026-10-18" in days_of(st)
            and all(re.match(r"^\d{4}-\d\d-\d\d$", d) for d in days_of(st)),
            f'partial state "{name}": exits 0, only well-formed days kept ({r.stderr.strip()[-200:]})',
        )
        if name == "nulls":
            ok(
                st["activity"] == {"2026-10-15": {"heartbeats": 2}},
                f"partial state: null activity rows dropped ({st['activity']})",
            )
            r = c.run(now, "--show")
            ok(r.returncode == 0, f"partial state: --show copes ({r.stderr[-200:]})")
    # dash-data's daily counts with odd types
    c = Case(os.path.join(root, "dash"))
    w(
        c.root,
        "state/dash/data.json",
        json.dumps(
            {
                "tokens": {
                    "daily": [
                        {
                            "date": "2026-10-17",
                            "turns": {"heartbeat": 4, "telegram": 2, "you (app/terminal)": 1},
                            "subagent_runs": 3,
                        },
                        {"date": "2026-10-16", "turns": ["x"], "subagent_runs": "many"},
                        {"date": "nope"},
                        None,
                        {"date": "2026-10-15", "turns": {"heartbeat": "5", "telegram": None}},
                    ]
                }
            }
        ),
    )
    r = c.run(now)
    act = (c.state() or {}).get("activity", {})
    ok(
        r.returncode == 0
        and act.get("2026-10-17") == {"heartbeats": 4, "owner_msgs": 3, "subagents": 3}
        and act.get("2026-10-16") == {"heartbeats": 0, "owner_msgs": 0, "subagents": 0}
        and act.get("2026-10-15") == {"heartbeats": 5, "owner_msgs": 0, "subagents": 0}
        and "nope" not in act,
        f"missing: odd dash-data counts become zeros, never a crash ({r.stderr.strip()[-200:]})",
    )
    for name, text in (
        ("dash-garbage", "{"),
        ("dash-list", "[]"),
        ("dash-daily-dict", '{"tokens": {"daily": {"a": 1}}}'),
    ):
        c = Case(os.path.join(root, name))
        w(c.root, "state/dash/data.json", text)
        r = c.run(now)
        ok(r.returncode == 0 and (c.state() or {}).get("activity") == {}, f"missing: {name} -> no activity, exit 0")


# ------------------------------------------------------------------------------------------- 4. DST
def test_dst(root):
    c = Case(root)
    c.git_init()
    w(c.tree, "bin/a", "#!/bin/sh\necho a\n", 0o755)
    c.commit(dt.datetime(2026, 10, 24, 21, 30, tzinfo=U))  # 23:30 CEST Sat 24.10
    w(c.tree, "bin/b", "#!/bin/sh\necho b\n", 0o755)
    c.commit(dt.datetime(2026, 10, 24, 22, 30, tzinfo=U))  # 00:30 CEST Sun 25.10 (fall-back day)
    w(c.tree, "bin/c", "#!/bin/sh\necho c\n", 0o755)
    c.commit(dt.datetime(2026, 10, 25, 1, 30, tzinfo=U))  # 02:30 CET, the repeated hour
    w(c.tree, "bin/d", "#!/bin/sh\necho d\n", 0o755)
    c.commit(dt.datetime(2026, 10, 25, 23, 30, tzinfo=U))  # 00:30 CET Mon 26.10
    now = dt.datetime(2026, 10, 27, 10, 0, tzinfo=Z)
    r = c.run(now, "--backfill")
    ds = days_of(c.state())
    ok(
        r.returncode == 0 and list(ds) == ["2026-10-24", "2026-10-25", "2026-10-26", "2026-10-27"],
        f"dst autumn: one point per local day ({list(ds)}; {r.stderr[-200:]})",
    )
    sc = {k: v["counts"]["scripts"] for k, v in ds.items()}
    ok(
        sc == {"2026-10-24": 1, "2026-10-25": 3, "2026-10-26": 4, "2026-10-27": 4},
        f"dst autumn: after-midnight commits count for the new local day, the last of the 25 h day wins ({sc})",
    )
    c = Case(os.path.join(root, "spring"))
    c.git_init()
    w(c.tree, "bin/a", "#!/bin/sh\necho a\n", 0o755)
    c.commit(dt.datetime(2027, 3, 27, 22, 59, tzinfo=U))  # 23:59 CET Sat 27.03
    w(c.tree, "bin/b", "#!/bin/sh\necho b\n", 0o755)
    c.commit(dt.datetime(2027, 3, 27, 23, 1, tzinfo=U))  # 00:01 CET Sun 28.03 (spring-forward day)
    w(c.tree, "bin/c", "#!/bin/sh\necho c\n", 0o755)
    c.commit(dt.datetime(2027, 3, 28, 21, 59, tzinfo=U))  # 23:59 CEST Sun 28.03
    w(c.tree, "bin/d", "#!/bin/sh\necho d\n", 0o755)
    c.commit(dt.datetime(2027, 3, 28, 22, 1, tzinfo=U))  # 00:01 CEST Mon 29.03
    r = c.run(dt.datetime(2027, 3, 30, 8, 0, tzinfo=Z), "--backfill")
    sc = {k: v["counts"]["scripts"] for k, v in days_of(c.state()).items()}
    ok(
        r.returncode == 0 and sc == {"2027-03-27": 1, "2027-03-28": 3, "2027-03-29": 4, "2027-03-30": 4},
        f"dst spring: the 23 h day keeps its first and last minute ({sc})",
    )


# ------------------------------------------------------------------------------------------- 5. month / year
def test_month(root):
    c = Case(root)
    c.git_init()
    w(c.tree, "bin/a", "#!/bin/sh\necho a\n", 0o755)
    c.commit(dt.datetime(2026, 10, 30, 12, 0, tzinfo=Z))
    w(c.tree, "bin/b", "#!/bin/sh\necho b\n", 0o755)
    c.commit(dt.datetime(2026, 11, 2, 0, 5, tzinfo=Z))
    w(c.tree, "bin/c", "#!/bin/sh\necho c\n", 0o755)
    r = c.run(dt.datetime(2026, 11, 3, 7, 0, tzinfo=Z))  # a plain run: the first of the day rebuilds past days
    ds = days_of(c.state())
    src = {k: v["source"] for k, v in ds.items()}
    ok(
        r.returncode == 0
        and src
        == {
            "2026-10-30": "git",
            "2026-10-31": "git (carried)",
            "2026-11-01": "git (carried)",
            "2026-11-02": "git",
            "2026-11-03": "live",
        },
        f"month: days carried across the turn ({src})",
    )
    ok(
        ds["2026-10-31"]["loc"] == ds["2026-10-30"]["loc"]
        and ds["2026-11-01"]["counts"]["scripts"] == 1
        and ds["2026-11-02"]["counts"]["scripts"] == 2
        and ds["2026-11-03"]["counts"]["scripts"] == 3,
        "month: carried days repeat the last state, the live day sees the tree",
    )
    # next day: yesterday's live point is replaced by its git state
    r = c.run(dt.datetime(2026, 11, 4, 0, 1, tzinfo=Z))
    ds = days_of(c.state())
    ok(
        r.returncode == 0
        and ds["2026-11-03"]["source"] == "git (carried)"
        and ds["2026-11-04"]["source"] == "live"
        and len(ds) == 6,
        f"month: a stale live point is rebuilt from git on the next day ({ {k: v['source'] for k, v in ds.items()} })",
    )
    c = Case(os.path.join(root, "year"))
    c.git_init()
    w(c.tree, "bin/a", "#!/bin/sh\necho a\n", 0o755)
    c.commit(dt.datetime(2026, 12, 31, 23, 59, tzinfo=Z))
    w(c.tree, "bin/b", "#!/bin/sh\necho b\n", 0o755)
    c.commit(dt.datetime(2027, 1, 1, 0, 0, tzinfo=Z))
    r = c.run(dt.datetime(2027, 1, 2, 12, 0, tzinfo=Z), "--backfill")
    sc = {k: v["counts"]["scripts"] for k, v in days_of(c.state()).items()}
    ok(
        r.returncode == 0 and sc == {"2026-12-31": 1, "2027-01-01": 2, "2027-01-02": 2},
        f"year: 23:59 vs 00:00 on New Year ({sc})",
    )


# ------------------------------------------------------------------------------------------- 6. --if-stale
def test_if_stale(root):
    c = Case(root)
    basic_tree(c)
    now = dt.datetime(2026, 10, 18, 10, 0, tzinfo=Z)
    c.run(now)
    w(c.tree, "bin/extra", "#!/bin/sh\necho x\n", 0o755)
    r = c.run(now + dt.timedelta(minutes=30), "--if-stale", "55")
    ok(r.returncode == 0 and c.state()["updated"] == "2026-10-18 10:00", "--if-stale: a 30 min old point is kept")
    r = c.run(now + dt.timedelta(minutes=56), "--if-stale", "55")
    st = c.state()
    ok(
        r.returncode == 0
        and st["updated"] == "2026-10-18 10:56"
        and days_of(st)["2026-10-18"]["counts"]["scripts"] == 3,
        "--if-stale: a 56 min old point is refreshed",
    )
    r = c.run(dt.datetime(2026, 10, 19, 0, 10, tzinfo=Z), "--if-stale", "55")
    ok(r.returncode == 0 and "2026-10-19" in days_of(c.state()), "--if-stale: a new day always gets its point")


# ------------------------------------------------------------------------------------------- 7. privacy
def test_privacy(root):
    c = Case(root)
    c.git_init()
    body = "\n".join(f"# {p}" for p in PRIVATE) + "\n"
    w(c.tree, "bin/secret-script-name", "#!/bin/sh\n" + body, 0o755)
    w(c.tree, "workspace/private-note.md", body)
    w(c.tree, ".claude/skills/Zoltanina/SKILL.md", body)
    w(c.tree, "opt/claude-vault/app/data/x.py", body)  # vault data: never read
    w(c.tree, "crontab", "# " + PRIVATE[0] + "\n* * * * * x\n")
    c.commit(dt.datetime(2026, 10, 17, 12, 0, tzinfo=Z))
    r = c.run(dt.datetime(2026, 10, 18, 12, 0, tzinfo=Z))
    text = open(c.out).read() if os.path.exists(c.out) else ""
    ok(r.returncode == 0 and text, "privacy: run exits 0")
    ok(not [p for p in PRIVATE if p in text], "privacy: no file name or content in the output")
    st = json.loads(text or "{}")
    strs = set()

    def walk(o, key=None):
        if isinstance(o, dict):
            for k, v in o.items():
                strs.add(("key", k))
                walk(v, k)
        elif isinstance(o, list):
            for v in o:
                walk(v, key)
        elif isinstance(o, str):
            strs.add((key, o))

    walk(st)
    labels = {p["label"] for p in st.get("parts", [])} | {p["key"] for p in st.get("parts", [])}
    langs = {"Python", "Shell", "JavaScript", "TypeScript", "HTML", "CSS", "Markdown (prompts)", "Other"}
    allowed_keys = (
        {
            "version",
            "updated",
            "updated_epoch",
            "parts",
            "days",
            "activity",
            "note",
            "key",
            "label",
            "date",
            "source",
            "at",
            "loc_total",
            "loc",
            "files",
            "counts",
            "langs",
            "claude_md_kb",
            "scripts",
            "skills",
            "agents",
            "cron",
            "heartbeats",
            "owner_msgs",
            "subagents",
        }
        | labels
        | langs
    )
    bad = []
    for k, v in strs:
        if k == "key":
            if v not in allowed_keys and not re.match(r"^\d{4}-\d\d-\d\d$", v):
                bad.append(v)
        elif k in ("date", "updated", "at"):
            if not re.match(r"^\d{4}-\d\d-\d\d( \d\d:\d\d)?$", v):
                bad.append(v)
        elif k == "source":
            if v not in ("git", "git (carried)", "live"):
                bad.append(v)
        elif k not in ("label", "key", "note"):
            bad.append(v)
    ok(not bad, f"privacy: every string is a date, a fixed label or a language ({bad[:5]})")
    vault_loc = days_of(st).get("2026-10-18", {}).get("loc", {}).get("vault")
    ok(vault_loc == 0, "privacy: the vault's data/ folder is never counted")


# ------------------------------------------------------------------------------------------- 7. a clock ahead
def test_future(root):
    """A run under a fake clock months ahead wrote points up to that day and an `updated` in the future; the next real
    run drops every point after today, treats the future `updated` as stale and rebuilds the past days from git."""
    c = Case(root)
    c.git_init()
    basic_tree(c, scripts=1)
    c.commit(dt.datetime(2026, 10, 18, 12, 0, tzinfo=Z))
    w(c.tree, "bin/late", "#!/bin/sh\necho late\n", 0o755)
    c.commit(dt.datetime(2026, 10, 19, 12, 0, tzinfo=Z))
    c.run(dt.datetime(2027, 3, 28, 12, 0, tzinfo=Z), "--if-stale", "55")  # the poisoned run
    st = c.state()
    st["activity"] = dict(
        st.get("activity") or {}, **{"2027-01-01": {"heartbeats": 1, "owner_msgs": 1, "subagents": 1}}
    )
    for d in st["days"]:  # what the fake clock left for the real past: a carried day, as if 19.10 had no commit
        if d["date"] == "2026-10-19":
            d["source"], d["counts"] = "git (carried)", dict(d["counts"], scripts=99)
    with open(c.out, "w") as f:
        json.dump(st, f)
    real = dt.datetime(2026, 10, 20, 12, 0, tzinfo=Z)
    r = c.run(real, "--if-stale", "55")
    st = c.state()
    ds = days_of(st)
    ok(
        r.returncode == 0
        and max(ds) == "2026-10-20"
        and st["updated"] == "2026-10-20 12:00"
        and st["updated_epoch"] == int(real.timestamp()),
        f"future: points after today dropped, `updated` refreshed ({max(ds)}, {st['updated']})",
    )
    ok(
        all(k <= "2026-10-20" for k in st["activity"]),
        f"future: activity after today dropped ({sorted(st['activity'])[-1:]})",
    )
    ok(
        ds["2026-10-19"]["source"] == "git" and ds["2026-10-19"]["counts"]["scripts"] == 2,
        f"future: past days rebuilt from git ({ds['2026-10-19']['source']}, {ds['2026-10-19']['counts']['scripts']} scripts)",  # noqa: E501
    )
    st["updated"], st["updated_epoch"] = "2026-10-21 12:00", int(real.timestamp()) + 86400  # only `updated` ahead
    with open(c.out, "w") as f:
        json.dump(st, f)
    r = c.run(real + dt.timedelta(minutes=10), "--if-stale", "55")
    ok(
        r.returncode == 0 and c.state()["updated"] == "2026-10-20 12:10",
        "future: an `updated` in the future is stale, not fresh",
    )
    r = c.run(real + dt.timedelta(minutes=20), "--if-stale", "55")
    ok(r.returncode == 0 and c.state()["updated"] == "2026-10-20 12:10", "future: a fresh real point is still kept")


def main():
    root = tempfile.mkdtemp(prefix="dash-growth-tests-")
    try:
        for name, fn in (
            ("shape", test_shape),
            ("missing", test_missing),
            ("dst", test_dst),
            ("month", test_month),
            ("stale", test_if_stale),
            ("privacy", test_privacy),
            ("future", test_future),
            ("mono", test_mono),
        ):
            sub = os.path.join(root, name)
            os.makedirs(sub)
            fn(sub)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print(f"{PASSES} passed, {len(FAILS)} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
