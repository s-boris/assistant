"""codebase-sync (sanitized export -> git remote), fully offline: a fake HOME, a fake scrub list, a local bare repo as
"origin" and the repo's script staged next to its secretscan.py, as deployed. Values that look like PII or tokens are
assembled at runtime so this file stays clean when it is exported or published."""

import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

EMAIL = "jane.owner@" + "mailbox.ch"
PHONE = "+41 79 " + "987 65 43"
PHONE2 = "079 " + "222 33 44"
PHONE3 = "079 " + "333 44 55"
PHONE4 = "079 " + "444 55 66"
CHAT = "9876" + "54321"
NAME = "Zorro" + "name"
STREET = "Fakestrasse" + " 7"
TOKEN = "7654321:" + "AAHfakefakefakefakefakefakefake12345"
SVALUE = "svcvalue" + "Q8w7e6r5t4y3u2i1"
GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]


class Sync:
    def __init__(self, t: Path):
        self.t = t
        self.h = t / "home"
        self.x = t / "export"
        self.r = t / "remote.git"
        self.a = self.h / "opt" / "assistant"
        self.inbox = t / "inbox"
        self.state = t / "state"
        self.pb = self.state / "pending-block.json"
        self.gate_conf = t / "gate.conf"
        self.tmp = t / "tmp"
        self.tmp.mkdir()
        self.bin = t / "csbin"  # codebase-sync imports secretscan from its own dir, as deployed (bin/secretscan.py)
        self.bin.mkdir()
        shutil.copy(ROOT / "scripts" / "codebase-sync", self.bin / "codebase-sync")
        shutil.copy(ROOT / "scripts" / "secretscan_py", self.bin / "secretscan.py")
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.h),
            "TMPDIR": str(self.tmp),
            "LANG": "C.UTF-8",
            "CODEBASE_SYNC_HOME": str(self.h),
            "CODEBASE_SYNC_EXPORT": str(self.x),
            "CODEBASE_SYNC_SCRUB": str(t / "scrub.txt"),
            "CODEBASE_SYNC_INBOX": str(self.inbox),
            "CODEBASE_SYNC_STATE": str(self.state),
            "CODEBASE_SYNC_GATE_CONF": str(self.gate_conf),  # no such file until a test writes one: no gate
            "CODEBASE_SYNC_MODE": "report",
            "SECRETSCAN_SOURCES": str(self.h),
            "GIT_CONFIG_GLOBAL": "/dev/null",
        }

    def __call__(self, *args, **env):
        e = dict(self.env, **{f"CODEBASE_SYNC_{k.upper()}": str(v) for k, v in env.items()})
        r = subprocess.run(
            [sys.executable, "-I", str(self.bin / "codebase-sync"), *args],
            env=e,
            capture_output=True,
            text=True,
            timeout=120,
        )
        r.out = r.stdout + r.stderr
        return r

    def w(self, rel, text, base=None):
        p = (base or self.h) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def git(self, *args, cwd=None):
        return subprocess.run(
            ["git", *GIT_ID, *args], cwd=cwd, env=self.env, capture_output=True, text=True, check=False
        )

    def rhead(self):
        r = self.git("--git-dir", str(self.r), "rev-parse", "-q", "--verify", "main")
        return r.stdout.strip() or "none"

    def xhead(self):
        return self.git("-C", str(self.x), "rev-parse", "HEAD").stdout.strip()

    def remote_has(self, path):
        return self.git("--git-dir", str(self.r), "cat-file", "-e", f"main:{path}").returncode == 0

    def remote_show(self, path):
        return self.git("--git-dir", str(self.r), "show", f"main:{path}").stdout

    def notes(self):
        return len(list(self.inbox.glob("codebase-sync-*.md"))) if self.inbox.is_dir() else 0

    def note_text(self):
        return "".join(p.read_text() for p in self.inbox.glob("codebase-sync-*.md"))

    def age(self):
        """Age the pending block by 31 min (the default persistence window is 30)."""
        d = json.loads(self.pb.read_text())
        d["first"] -= 31 * 60
        self.pb.write_text(json.dumps(d))

    def first(self):
        return json.loads(self.pb.read_text())["first"]

    def last_run(self):
        return json.loads((self.state / "last-run.json").read_text())

    def tree_has(self, root, needle):
        for p in root.rglob("*"):
            if ".git" in p.relative_to(root).parts or not p.is_file() or p.is_symlink():
                continue
            if needle in p.read_bytes().decode("utf-8", "replace"):
                return True
        return False

    def add_publish_paths(self):
        """A plugin file the repo tracks and a link onto it from bin/, fixture jsonl and repo-owned files."""
        self.w("plugins/p1/ops/hb", "gate\n")
        self.w("plugins/p1/ops/hb", "gate\n", base=self.a)
        self.git("-C", str(self.a), "add", "-A")
        self.git("-C", str(self.a), "commit", "-qm", "hb")
        (self.h / "bin/hb").symlink_to("../plugins/p1/ops/hb")  # leaves bin/, but maps onto an exported file
        self.w("opt/demo-tests/fixtures/s.jsonl", '{"type": "user", "text": "synthetic"}\n')
        self.w("opt/demo-tests/other.jsonl", '{"x": 1}\n')
        self.w("docs/guide.md", "# guide\n", base=self.x)
        self.w("tools/t.sh", "echo t\n", base=self.x)
        self.w(".github/workflows/ci.yml", "name: ci\n", base=self.x)
        self.w("LICENSE", "license\n", base=self.x)

    def clean_public(self):
        """Strip the scrub-list name from every public path, so enforce mode has nothing to block."""
        self.w("bin/tool", "#!/bin/sh\necho hi\n")
        for rel in (".claude/skills/demo/SKILL.md", "opt/demo-tests/fixtures/f.txt", "plugins/p1/server.ts"):
            p = self.h / rel
            p.write_text(p.read_text().replace(NAME, "PersonX"))
        (self.h / "opt/demo-tests/fixtures/page.html.gz").write_bytes(gzip.compress(b"shop page PersonX\n", mtime=0))


@pytest.fixture
def cs(tmp_path):
    s = Sync(tmp_path)
    h = s.h
    s.w(
        "bin/tool",
        f"#!/bin/sh\n# owner: {NAME}, mail {EMAIL}, phone {PHONE}, chat {CHAT}, home {STREET}, 1234 Exampletown\n"
        "echo hi\n",
    ).chmod(0o755)
    s.w(".claude/skills/demo/SKILL.md", f"# skill about {NAME}\n")
    s.w(".claude/agents/a.md", "agent\n")
    s.w(".claude/hooks/h.sh", "hook\n")
    s.w(".claude/settings.json", json.dumps({"env": {"FOO": f"bar-{NAME}"}, "model": "x"}) + "\n")
    s.w(".claude/channels/telegram/access.json", json.dumps({"allowFrom": [CHAT]}) + "\n")
    s.w(".secrets/svc.json", json.dumps({"key": SVALUE}) + "\n")
    s.w(".claude/projects/-home-node-workspace/memory/m.md", f"private memory about {NAME}\n")
    s.w("workspace/CLAUDE.md", f"# how I operate, chat {CHAT}\n")
    s.w("workspace/CHANGELOG.md", "- 2026-01-01: did things\n")
    s.w("workspace/TODO.md", "secret todo\n")
    s.w("crontab", "* * * * * true\n")
    s.w("boot.sh", "#!/bin/sh\n")
    s.w(
        "opt/dash/index.html",
        '<html><script id="dash-data" type="application/json" data-mode="fallback">'
        f'{{"todo":"{NAME}"}}</script></html>\n',
    )
    s.w("opt/dash/data.json", '{"private": 1}\n')
    s.w("opt/demo-tests/fixtures/f.txt", f"fixture for {NAME}\n")
    (h / "opt/demo-tests/fixtures/page.html.gz").write_bytes(gzip.compress(f"shop page {NAME}\n".encode(), mtime=0))
    (h / "opt/demo-tests/fixtures/pic.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    s.w("opt/demo-tests/data.env", "x\n")
    (h / "opt/demo-tests/outside-link").symlink_to("/etc/passwd")
    # ~/plugins comes from the harness repo (plugins/, deployed byte for byte): its tracked names say which live
    # files are exported; a live-only file (generated, e.g. typings) stays out
    s.w("plugins/.gitignore", "node_modules/\n")
    s.w("plugins/p1/server.ts", f"plugin {NAME}\n")
    s.w("plugins/p1/node_modules/x/i.js", "junk\n")
    s.w("plugins/p1/tsconfig.json", "{}\n")
    s.w("plugins/.gitignore", "node_modules/\n", base=s.a)
    s.w("plugins/p1/server.ts", "plugin\n", base=s.a)
    s.git("init", "-q", str(s.a))
    s.git("-C", str(s.a), "add", "-A")
    s.git("-C", str(s.a), "commit", "-qm", "init")
    s.inbox.mkdir()
    s.git("init", "-q", "--bare", "--initial-branch=main", str(s.r))
    s.x.mkdir()
    s.git("init", "-q", "--initial-branch=main", str(s.x))
    s.git("-C", str(s.x), "remote", "add", "origin", str(s.r))
    (tmp_path / "scrub.txt").write_text(
        "# test scrub list\n"
        f"{NAME}\tPersonX\n"
        f"i:{EMAIL}\towner@example.com\n"
        "re:(?i)fakestr(?:asse|\\.)?\\s*7\\b\tExamplestrasse 1\n"
        "re:\\+41 79 98[7] 65 43\t<phone>\n"
    )
    return s


@pytest.fixture
def synced(cs):
    r = cs()
    assert r.returncode == 0, r.out
    return cs


def test_first_sync_scrubs_excludes_and_pushes(cs):
    h, x = cs.h, cs.x
    # work copies (edit on a copy + atomic mv): a dot-copy in bin, *.new / *.tmp anywhere; plus a WIP file an older
    # sync already swept into the export: the next sync must drop it
    for rel in ("bin/.tool.new", "bin/.tool.edit", "bin/tool.tmp", "bin/.swept.new"):
        cs.w(rel, "#!/bin/sh\n")
    cs.w(".claude/skills/demo/SKILL.md.new", "draft\n")
    cs.w("bin/.swept.new", "old\n", base=x)

    r = cs()
    assert r.returncode == 0, r.out
    for rel in ("bin/.tool.new", "bin/.tool.edit", "bin/tool.tmp", "claude/skills/demo/SKILL.md.new"):
        assert not (x / rel).exists(), rel
    assert not (x / "bin/.swept.new").exists() and not cs.remote_has("bin/.swept.new")
    assert not re.search(r"tool\.new|tool\.tmp|tool\.edit", r.out)
    assert cs.rhead() != "none"

    tool = (x / "bin/tool").read_text()
    assert "PersonX" in tool and NAME not in tool
    assert "owner@example.com" in tool and EMAIL.lower() not in tool.lower()
    assert "<phone>" in tool and "987 65 43" not in tool
    assert "100000001" in tool and not cs.tree_has(x, CHAT)  # chat id from access.json replaced everywhere
    assert "Examplestrasse 1" in tool and "Fakestrasse" not in tool
    live = (h / "bin/tool").read_text()
    assert NAME in live and EMAIL in live  # the original untouched
    assert os.access(x / "bin/tool", os.X_OK)

    assert "PersonX" in (x / "claude/skills/demo/SKILL.md").read_text()
    assert (x / "claude/agents/a.md").is_file() and (x / "claude/hooks/h.sh").is_file()
    assert json.loads((x / "claude/settings.json").read_text())["env"]["FOO"] == "<redacted>"
    assert 'data-mode="fallback">{}</script>' in (x / "dash/index.html").read_text()
    assert not (x / "dash/data.json").exists()
    assert (x / "workspace/CLAUDE.md").is_file() and not (x / "workspace/TODO.md").exists()
    assert not cs.tree_has(x, "private memory") and not (x / ".secrets").exists() and not cs.tree_has(x, SVALUE)
    assert not (x / "claude/channels").exists() and not list(x.rglob("access.json"))
    assert (x / "tests/demo/fixtures/f.txt").is_file()
    assert not (x / "tests/demo/fixtures/pic.png").exists() and not (x / "tests/demo/data.env").exists()
    assert not (x / "tests/demo/outside-link").exists() and not (x / "tests/demo/outside-link").is_symlink()
    assert gzip.decompress((x / "tests/demo/fixtures/page.html.gz").read_bytes()) == b"shop page PersonX\n"
    assert "PersonX" in (x / "plugins/p1/server.ts").read_text() and (x / "plugins/.gitignore").is_file()
    assert not (x / "plugins/p1/node_modules").exists()
    assert not (x / "plugins/p1/tsconfig.json").exists()  # live-only, the repo does not track it
    assert os.access(x / ".git/hooks/pre-commit", os.X_OK)  # secretscan pre-commit hook


def test_idempotent_and_repo_readme_kept(synced):
    h1 = synced.rhead()
    synced()
    assert synced.rhead() == h1
    synced.w("README.md", "# readme\n", base=synced.x)
    synced()
    synced()
    assert (synced.x / "README.md").is_file() and "readme" in synced.remote_show("README.md")


def test_unlisted_pii_blocks_and_notes_after_30_min(synced):
    cs = synced
    h2 = cs.rhead()
    cs.w("bin/tool2", f"call {PHONE2}\n")
    r = cs("--cron")
    assert r.returncode == 3, r.out
    assert cs.rhead() == h2 and not (cs.x / "bin/tool2").exists()
    assert cs.notes() == 0 and cs.pb.stat().st_size > 0  # first cron block: pending, no note yet
    assert cs("--cron").returncode == 3 and cs.notes() == 0  # again within 30 min
    cs.age()
    assert cs("--cron").returncode == 3 and cs.notes() == 1
    assert cs.rhead() == h2
    assert "222 33 44" not in cs.note_text()  # the note never carries the value
    n1 = len(list(cs.inbox.iterdir()))
    cs("--cron")
    assert len(list(cs.inbox.iterdir())) == n1  # same problem again: no second note
    with open(cs.t / "scrub.txt", "a") as f:
        f.write(f"allow:{PHONE2}\n")
    cs()
    assert cs.rhead() != h2 and "222 33 44" in (cs.x / "bin/tool2").read_text()  # allow: lets a known fake through


def test_token_and_secret_value_refused(synced):
    cs = synced
    h3 = cs.rhead()
    cs.w("bin/tool3", f"BOT_TOKEN={TOKEN}\n")
    assert cs("--cron").returncode == 3 and cs.rhead() == h3
    for root in (cs.x, cs.inbox, cs.state):
        assert not cs.tree_has(root, TOKEN), root
    (cs.h / "bin/tool3").unlink()
    cs.w("bin/tool4", f"v={SVALUE}\n")  # one of my real secret values (SECRETSCAN_SOURCES/.secrets)
    assert cs().returncode == 3 and cs.rhead() == h3


def test_check_mode_writes_nothing_and_missing_scrub_list_refuses(synced):
    cs = synced
    h = cs.rhead()
    cs.w("bin/tool5", "new\n")
    assert cs("check").returncode == 0
    assert not (cs.x / "bin/tool5").exists() and cs.rhead() == h
    assert cs(scrub=cs.t / "missing").returncode == 1 and cs.rhead() == h
    assert cs("check", mode="bogus").returncode == 1  # unknown mode


def test_report_mode_manifest(synced):
    cs = synced
    cs.add_publish_paths()
    h4 = cs.rhead()
    r = cs()
    assert r.returncode == 0 and cs.rhead() != h4, r.out
    for rel in ("docs/guide.md", "tools/t.sh", ".github/workflows/ci.yml", "LICENSE"):
        assert (cs.x / rel).is_file(), rel  # repo-owned dirs and files kept across a sync
    assert "guide" in cs.remote_show("docs/guide.md") and "ci" in cs.remote_show(".github/workflows/ci.yml")
    # report mode: the jsonl fixture and the cross-dir link still left out
    assert not (cs.x / "tests/demo/fixtures/s.jsonl").exists() and not (cs.x / "bin/hb").is_symlink()
    assert (cs.x / "plugins/p1/ops/hb").is_file()

    m = cs("manifest")
    assert m.returncode == 0, m.out
    assert "would block  bin/tool " in m.out and "would block  claude/skills/demo/SKILL.md" in m.out
    assert "would block  workspace/" not in m.out  # private paths are scrubbed, never listed as blocking
    assert "would add    tests/demo/fixtures/s.jsonl" in m.out and "other.jsonl" not in m.out
    assert "would keep   bin/hb (symlink)" in m.out and "outside-link" not in m.out
    assert NAME not in m.out and not cs.tree_has(cs.state, NAME)
    man = cs.last_run()["manifest"]
    assert man["mode"] == "report" and man["private"] >= 2 and man["would_block"] >= 4

    cs.w("docs/leak.md", f"# doc for {NAME}\n", base=cs.x)
    assert cs().returncode == 3  # a repo-owned file with a term is checked too
    assert not cs.remote_has("docs/leak.md")


def test_enforce_mode(synced):
    cs = synced
    cs.add_publish_paths()
    cs()
    h5 = cs.rhead()
    assert cs("--cron", mode="enforce").returncode == 3 and cs.rhead() == h5  # a term in a public path
    for root in (cs.x, cs.inbox, cs.state):
        assert not cs.tree_has(root, NAME), root
    cs.clean_public()
    r = cs(mode="enforce")
    assert r.returncode == 0 and cs.rhead() != h5, r.out
    claude_md = (cs.x / "workspace/CLAUDE.md").read_text()
    assert "100000001" in claude_md and CHAT not in claude_md  # a private path is still scrubbed
    assert (cs.x / "tests/demo/fixtures/s.jsonl").is_file() and not (cs.x / "tests/demo/other.jsonl").exists()
    assert os.readlink(cs.x / "bin/hb") == "../plugins/p1/ops/hb" and not (cs.x / "tests/demo/outside-link").exists()
    (cs.h / "opt/demo-tests/fixtures/p2.html.gz").write_bytes(gzip.compress(f"page {NAME}\n".encode(), mtime=0))
    assert cs(mode="enforce").returncode == 3  # a public .gz with a hit blocks instead of a silent drop
    assert not (cs.x / "tests/demo/fixtures/p2.html.gz").exists()


FAKEGATE = """#!/bin/sh
d=$(dirname "$0"); printf '%s\\n' "$*" > "$d/gate-args"
[ -e "$d/gate-block" ] || exit 0
echo "  gate denylist (1 private rules): 1 hits"; echo "    bin/tool:2 rule#7"; echo "BLOCKED (1)"; exit 3
"""

PGATE = """#!/bin/bash
d=$(dirname "$0"); printf '%s\\n' "$*" > "$d/pgate-args"
git -C "$4" ls-tree -r --name-only "$6" > "$d/pgate-files" 2>&1
[ -e "$d/pgate-block" ] || { echo "demo @tree: all fast gates passed"; exit 0; }
echo "  gate denylist (1 private rules): 1 hits"; echo "    bin/tool:2 rule#7"; echo "BLOCKED (1)"; exit 3
"""


def _script(p: Path, text: str) -> Path:
    p.write_text(text)
    p.chmod(0o755)
    return p


def test_enforce_gate(synced):
    """Enforce mode + the public-path gate (~/.config/codebase-sync/gate): the staged tree must pass it first."""
    cs = synced
    cs.clean_public()
    assert cs(mode="enforce").returncode == 0
    gate = _script(cs.t / "fakegate", FAKEGATE)
    cs.gate_conf.write_text(f"# gate command\n{gate} gate demo\n")
    (cs.t / "gate-block").touch()
    h9, x9 = cs.rhead(), cs.xhead()
    with open(cs.h / "bin/tool", "a") as f:
        f.write("echo two\n")
    assert cs("--cron", mode="enforce").returncode == 3
    assert cs.notes() == 0 and "gate" in cs.pb.read_text()  # first gate block: pending
    cs.age()
    r = cs("--cron", mode="enforce")
    assert r.returncode == 3 and cs.rhead() == h9 and cs.xhead() == x9, r.out
    assert re.fullmatch(
        rf"gate demo --src {re.escape(str(cs.x))} --rev [0-9a-f]{{40}}\n", (cs.t / "gate-args").read_text()
    )
    assert cs.git("-C", str(cs.x), "diff", "--cached", "--name-only").stdout.strip() == ""  # nothing left staged
    note = cs.note_text()
    assert "public-path gate blocked it" in note and "bin/tool:2 rule#7" in note
    assert "gate" in cs.last_run()["result"]
    assert cs("check", mode="report").returncode == 0  # the gate is enforce-only
    (cs.t / "gate-block").unlink()
    r = cs(mode="enforce")
    assert r.returncode == 0 and cs.rhead() != h9, r.out

    cs.gate_conf.write_text(f"{cs.t / 'no-such-gate'} gate demo\n")
    h9 = cs.rhead()
    with open(cs.h / "bin/tool", "a") as f:
        f.write("echo three\n")
    r = cs(mode="enforce")
    assert r.returncode == 3 and cs.rhead() == h9 and "could not run" in r.out
    cs.gate_conf.write_text("# only a comment\n")
    r = cs(mode="enforce")
    assert r.returncode == 3 and "no gate command" in r.out
    cs.gate_conf.unlink()
    r = cs(mode="enforce")
    assert r.returncode == 0 and cs.rhead() != h9, r.out


def test_precheck(synced):
    """precheck: check + the gate on a throwaway tree; no lock, no state, no note, the export repo untouched."""
    cs = synced
    cs.clean_public()
    assert cs(mode="enforce").returncode == 0
    pgate = _script(cs.t / "pgate", PGATE)
    cs.gate_conf.write_text(f"{pgate} gate demo\n")
    (cs.t / "pgate-block").touch()
    shutil.rmtree(cs.inbox)
    x10, h10 = cs.xhead(), cs.rhead()
    st10 = hashlib.md5((cs.state / "last-run.json").read_bytes()).hexdigest()
    with open(cs.h / "bin/tool", "a") as f:
        f.write("echo four\n")
    r = cs("precheck", "--cron", mode="enforce")
    assert r.returncode == 3 and "\n  bin/tool:2 rule#7\n" in "\n" + r.stdout and "WOULD BLOCK" in r.out, r.out
    args = (cs.t / "pgate-args").read_text()
    assert re.fullmatch(r"gate demo --src /\S+ --rev [0-9a-f]{40}\n", args) and f"--src {cs.x} " not in args
    assert "bin/tool" in (cs.t / "pgate-files").read_text().splitlines()
    assert not cs.inbox.exists() or not list(cs.inbox.iterdir())  # never a note, even with --cron
    assert cs.xhead() == x10 and cs.rhead() == h10
    assert cs.git("-C", str(cs.x), "status", "--porcelain").stdout == ""
    assert hashlib.md5((cs.state / "last-run.json").read_bytes()).hexdigest() == st10
    assert not list(cs.tmp.glob("codebase-precheck-*"))  # the throwaway repo removed
    (cs.t / "pgate-block").unlink()
    r = cs("precheck", mode="enforce")
    assert r.returncode == 0 and re.search(r"clean .*gate passed", r.out), r.out
    (cs.t / "pgate-args").unlink()
    assert cs("precheck", mode="report").returncode == 0 and not (cs.t / "pgate-args").exists()  # no gate
    cs.gate_conf.write_text(f"{cs.t / 'no-such-gate'} gate demo\n")
    r = cs("precheck", mode="enforce")
    assert r.returncode == 1 and "could not run" in r.out
    cs.gate_conf.unlink()
    r = cs("precheck", mode="enforce")
    assert r.returncode == 0 and "not configured" in r.out
    with open(cs.h / "bin/tool", "a") as f:
        f.write(f"echo {EMAIL}\n")
    r = cs("precheck", mode="enforce")
    assert r.returncode == 3 and re.search(r"(?m)^  bin/tool:\d+ ", r.out) and EMAIL not in r.out


def test_cron_block_debounce(synced):
    """--cron: the same hit set (file:line:rule) over 2 runs >= 30 min apart; a clean run clears it; other hits
    restart the clock; manual runs report at once; --recheck runs only while a block is pending."""
    cs = synced
    cs.w("bin/extra", "echo ok\n")  # a clean change for the clean runs to push
    r = cs("--cron", "--recheck")
    assert r.returncode == 0 and r.out == "" and not cs.pb.exists()
    cs.w("bin/tool5", f"call {PHONE3}\n")
    r = cs()
    assert r.returncode == 3 and "BLOCKED" in r.out and not cs.pb.exists() and cs.notes() == 0
    assert cs("--cron").returncode == 3 and cs.notes() == 0 and cs.pb.stat().st_size > 0
    assert "333 44 55" not in cs.pb.read_text()
    (cs.h / "bin/tool5").unlink()
    h11 = cs.rhead()
    assert cs("--cron").returncode == 0 and not cs.pb.exists() and cs.notes() == 0  # block then clean
    cs.w("bin/tool5", f"call {PHONE3}\n")
    cs("--cron")
    cs.age()
    assert cs("--cron", "--recheck").returncode == 3 and cs.notes() == 1
    shutil.rmtree(cs.inbox)
    cs.pb.unlink()
    cs("--cron")
    cs.age()
    with open(cs.h / "bin/tool5", "a") as f:
        f.write(f"call {PHONE4}\n")
    assert cs("--cron").returncode == 3 and cs.notes() == 0  # changed hit set: the clock restarts
    cs.age()
    cs("--cron")
    assert cs.notes() == 1
    f1 = cs.first()
    p = cs.h / "bin/tool5"
    p.write_text(p.read_text().replace("call ", "ring "))
    cs("--cron")
    assert cs.first() == f1  # same file:line:rule, other text: same block
    p.rename(cs.h / "bin/tool6")
    cs("--cron")
    assert cs.first() != f1  # the same values in another file: another hit set
    (cs.h / "bin/tool6").unlink()
    assert cs("--cron").returncode == 0 and not cs.pb.exists() and cs.rhead() != h11


def test_assistant_repo_only_as_pointer(synced):
    """The harness repo has its own public repo (assistant-publish): only a pointer README under assistant/, never
    its code (tracked or not); the deployed core.releases copies stay out."""
    cs = synced
    a = cs.t / "harness"
    cs.w("core/paths.py", "X = 1\n", base=a)
    cs.git("init", "-q", str(a))
    cs.git("-C", str(a), "add", "-A")
    cs.git("-C", str(a), "commit", "-qm", "init")
    cs.w("core/new.py", f"owner {NAME}\n", base=a)
    cs.w("bin/lib/core.releases/r1/core/paths.py", "X = 1\n")
    cs.clean_public()
    r = cs(mode="enforce", assistant_src=a)
    assert r.returncode == 0, r.out
    assert [p.name for p in (cs.x / "assistant").iterdir()] == ["README.md"]
    assert "assistant-publish" in (cs.x / "assistant/README.md").read_text()
    assert not (cs.x / "bin/lib/core.releases").exists()
