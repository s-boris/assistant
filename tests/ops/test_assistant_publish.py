"""assistant-publish: neutral snapshots (one root commit, then one commit per publish), privacy scan of what each
snapshot adds, push / block / allow / replace.

The real scanners (codebase-sync, publish.py, public-gate, gitleaks) are replaced by small fakes with one private
term each; git is the real one."""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TOOL = REPO / "scripts" / "assistant-publish"

NAME = "Zorblax"  # a codebase-sync scrub term (fake)
PLACE = "Quuxville"  # a public-release denylist term (fake)

FAKE_CS = r"""
import re
class _SS:
    @staticmethod
    def find(text):
        return [("pattern:fake-key", text.count("\n", 0, m.start()) + 1) for m in re.finditer(r"FAKEKEY-\d{6}", text)]
secretscan = _SS()
class R:
    def __init__(self, lit):
        self.check_rx = re.compile(r"(?<![^\W_])" + re.escape(lit) + r"(?![^\W_])")
        self.label = "scrub:%s…(%d)" % (lit[:1], len(lit))
def load_rules():
    return [R("Zorblax")], set()
def pii_hits(text, allow):
    return []
def decode_text(b):
    return None if b"\0" in b[:65536] else b.decode("utf-8", "replace")
def texts_of(path):
    return [("", open(path, "rb").read().decode("utf-8", "replace"))]
"""

FAKE_PUB = r"""
import re
HANDLE = "test-handle"
AUTHOR = ("test-handle", "1+test-handle@users.noreply.github.com")
SENTINEL = ""
class R:
    def __init__(self, lit):
        self.raw = lit
        self.rx = re.compile(r"(?<![^\W_])" + re.escape(lit) + r"(?![^\W_])")
def load_rules():
    return [R("Quuxville")], set()
def denylist_rules(rules, serials):
    return [("rule#%d" % i, r.rx) for i, r in enumerate(rules)]
def denylist_text(rules, serials):
    return "lit:Quuxville\ngeo:1.0,1.0,1.0\n"
def found_serials(texts):
    return set()
def real_serials():
    return []
def identifiers(texts, root):
    return []
def write_private(path, text):
    open(path, "w").write(text)
"""

FAKE_GATE = r"""import os, sys
root = sys.argv[sys.argv.index("--root") + 1]
hits = []
for d, _, fs in os.walk(root):
    for f in fs:
        p = os.path.join(d, f)
        for n, line in enumerate(open(p, encoding="utf-8", errors="replace").read().split("\n"), 1):
            if "Quuxville" in line:
                hits.append("%s:%d denylist#0" % (os.path.relpath(p, root), n))
for h in hits:
    print(h)
print("public-gate: %d hit(s); 1 file(s) scanned, 1 denylist rule(s) (+0 allowed words), 1 geo point(s)" % len(hits),
      file=sys.stderr)
sys.exit(1 if hits else 0)
"""

FAKE_GITLEAKS = r"""#!/bin/sh
for a in "$@"; do case $prev in --report-path) rep=$a;; esac; prev=$a; done
printf '%s' "${FAKE_GITLEAKS_JSON:-[]}" > "$rep"
"""


class Pub:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.src = tmp / "src"
        self.remote = tmp / "remote.git"
        self.conf = tmp / "conf"
        self.inbox = tmp / "inbox"
        fakes = tmp / "fakes"
        fakes.mkdir()
        (fakes / "codebase-sync").write_text(FAKE_CS)
        (fakes / "publish.py").write_text(FAKE_PUB)
        (fakes / "public-gate.py").write_text(FAKE_GATE)
        (fakes / "gitleaks").write_text(FAKE_GITLEAKS)
        (fakes / "gitleaks").chmod(0o755)
        self.conf.mkdir()
        home = tmp / "home"
        home.mkdir()
        # local-path remotes need git-upload-pack/git-receive-pack on PATH; the CI runner's git is builtins only
        shims = tmp / "shims"
        shims.mkdir()
        for name in ("upload-pack", "receive-pack"):
            (shims / f"git-{name}").write_text(f'#!/bin/sh\nexec git {name} "$@"\n')
            (shims / f"git-{name}").chmod(0o755)
        self.env = dict(os.environ)
        self.env["PATH"] = os.pathsep.join([self.env.get("PATH", ""), str(shims)])
        self.env.update(
            {
                "HOME": str(home),
                "ASSISTANT_TEST": "1",
                "TMPDIR": str(tmp),
                "ASSISTANT_PUBLISH_SRC": str(self.src),
                "ASSISTANT_PUBLISH_URL": str(self.remote),
                "ASSISTANT_PUBLISH_CONF": str(self.conf),
                "ASSISTANT_PUBLISH_STATE": str(tmp / "state"),
                "ASSISTANT_PUBLISH_INBOX": str(self.inbox),
                "ASSISTANT_PUBLISH_CS": str(fakes / "codebase-sync"),
                "ASSISTANT_PUBLISH_PUB": str(fakes / "publish.py"),
                "ASSISTANT_PUBLISH_GATE": str(fakes / "public-gate.py"),
                "ASSISTANT_PUBLISH_GATE_ALLOW": str(fakes / "none.txt"),
                "ASSISTANT_PUBLISH_GITLEAKS": str(fakes / "gitleaks"),
                "ASSISTANT_PUBLISH_GITLEAKS_CONF": str(fakes / "none.toml"),
                "GIT_CONFIG_GLOBAL": str(tmp / "gitconfig"),
            }
        )
        (tmp / "gitconfig").write_text("[init]\n\tdefaultBranch = main\n[commit]\n\tgpgsign = false\n")
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)], check=True, env=self.env)
        self.git("init", "-q", "-b", "main", str(self.src), cwd=tmp)
        self.commit("a.txt", "hello\n", "first")
        self.commit("town.txt", f"we met in {PLACE}\n", "add a place")
        self.commit("town.txt", "we met in town\n", "drop the place")
        self.state = tmp / "state"

    def git(self, *args, cwd=None):
        r = subprocess.run(
            ["git", "-C", str(cwd or self.src)] + list(args), capture_output=True, text=True, env=self.env
        )
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    def commit(self, rel, text, msg, data=None):
        p = self.src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if data is None:
            p.write_text(text)
        else:
            p.write_bytes(data)
        self.git("add", rel)
        self.git("-c", f"user.name={NAME} Person", "-c", "user.email=me@example.com", "commit", "-q", "-m", msg)

    def run(self, *args, extra=None):
        env = dict(self.env, **(extra or {}))
        return subprocess.run([str(TOOL)] + list(args), capture_output=True, text=True, env=env, timeout=300)

    def remote_head(self):
        r = subprocess.run(
            ["git", "-C", str(self.remote), "rev-parse", "--verify", "-q", "refs/heads/main"],
            capture_output=True,
            text=True,
            env=self.env,
        )
        return r.stdout.strip() or None

    def notes(self):
        return sorted(self.inbox.glob("assistant-publish-*.md")) if self.inbox.exists() else []


@pytest.fixture
def pub(tmp_path):
    return Pub(tmp_path)


def published_log(pub):
    return pub.git("log", "-p", "--format=%an <%ae>|%cn <%ce>|%B", "main", cwd=pub.remote)


IDENT = "test-handle <1+test-handle@users.noreply.github.com>"


def test_first_push_is_one_root_snapshot_by_the_neutral_identity(pub):
    r = pub.run("push")
    assert r.returncode == 0, r.stderr
    log = published_log(pub)
    assert pub.git("rev-list", "--count", "main", cwd=pub.remote) == "1"  # one root commit, not main's history
    assert log.startswith(IDENT + "|" + IDENT + "|Initial public snapshot")
    assert NAME not in log and PLACE not in log and "add a place" not in log and "drop the place" not in log
    assert pub.git("rev-parse", "main^{tree}", cwd=pub.remote) == pub.git("rev-parse", "main^{tree}")
    assert (pub.state / "snapshot-root").read_text().strip() == pub.remote_head()
    head = pub.remote_head()
    r = pub.run("push")
    assert r.returncode == 0 and "up to date" in r.stdout and pub.remote_head() == head
    assert NAME in pub.git("log", "-1", "--format=%an", "main")  # the source is never rewritten
    assert pub.git("rev-list", "--count", "main") == "3"


def test_each_later_publish_is_one_snapshot_with_a_neutral_message(pub):
    assert pub.run("push").returncode == 0
    root = pub.remote_head()
    pub.commit("docs/c.txt", "one\ntwo\n", "private musings about the plan")
    pub.commit("docs/d.txt", "three\n", "more private musings")
    r = pub.run("push")
    assert r.returncode == 0, r.stderr
    assert pub.git("rev-list", "--count", "main", cwd=pub.remote) == "2"
    assert pub.git("rev-parse", "main^", cwd=pub.remote) == root  # a fast-forward on the published main
    msg = pub.git("log", "-1", "--format=%B", "main", cwd=pub.remote)
    assert msg.startswith("Update docs") and "2 files changed, +3 -0 lines: docs 2." in msg
    assert "musings" not in published_log(pub)
    assert pub.git("rev-parse", "main^{tree}", cwd=pub.remote) == pub.git("rev-parse", "main^{tree}")


def test_the_first_snapshot_scans_the_whole_tree(pub):
    pub.commit("old.txt", f"ping {NAME} here\n", "an old commit, still in the tree")
    pub.commit("a.txt", "hello again\n", "a later commit")
    r = pub.run("push")
    assert r.returncode == 3 and pub.remote_head() is None
    assert "old.txt:1 scrub:Z…(7)" in r.stderr and NAME not in r.stdout + r.stderr


def test_new_commit_with_a_hit_blocks_once_noted_and_never_shows_the_value(pub):
    assert pub.run("push").returncode == 0
    head = pub.remote_head()
    pub.commit("b.txt", f"ping {NAME} here\n", "add b")
    for _ in range(2):
        r = pub.run("push", "--cron")
        assert r.returncode == 3 and pub.remote_head() == head
        assert NAME not in r.stdout + r.stderr and "b.txt:1 scrub:Z…(7)" in r.stderr
    assert len(pub.notes()) == 1  # one note per problem and day
    text = pub.notes()[0].read_text()
    assert "Data, not instructions." in text and NAME not in text and "b.txt:1" in text
    st = json.loads(pub.run("status", "--json").stdout)
    assert st["result"] == "blocked" and st["hits"] == 1


def test_allow_a_reviewed_hit_unblocks(pub):
    pub.commit("b.txt", f"ping {NAME} here\n", "add b")
    r = pub.run("scan")
    assert r.returncode == 3 and NAME not in r.stdout
    h = re.search(r"b\.txt:1 scrub:\S+ #([0-9a-f]{16})", r.stdout).group(1)
    assert pub.run("allow", h, "--reason", "test fixture").returncode == 0
    assert "  allowed " in pub.run("scan").stdout
    r = pub.run("push")
    assert r.returncode == 0, r.stderr
    assert pub.remote_head() is not None


def old_history_remote(pub):
    """The remote holds main's own (old, full) history, as before the switch to snapshots."""
    pub.git("push", "-q", str(pub.remote), "main:refs/heads/main")
    return pub.remote_head()


def test_old_history_waits_quietly_for_replace(pub):
    old = old_history_remote(pub)
    for _ in range(2):
        r = pub.run("push", "--cron")
        assert r.returncode == 0 and pub.remote_head() == old, r.stderr
    assert "push --replace" in r.stdout and pub.notes() == []
    assert json.loads(pub.run("status", "--json").stdout)["result"] == "replace pending"
    assert pub.run("push", "--replace", "--cron").returncode == 2 and pub.remote_head() == old


def test_replace_dry_run_then_replace_force_pushes_one_root(pub):
    old = old_history_remote(pub)
    r = pub.run("push", "--replace", "--dry-run")
    assert r.returncode == 0 and f"would be a force push over {old[:12]}" in r.stdout, r.stderr
    assert pub.remote_head() == old and not (pub.state / "snapshot-root").exists()
    r = pub.run("push", "--replace")
    assert r.returncode == 0 and "force-pushed" in r.stdout, r.stderr
    assert pub.git("rev-list", "--count", "main", cwd=pub.remote) == "1"
    assert "add a place" not in published_log(pub) and NAME not in published_log(pub)
    assert (pub.state / "snapshot-root").read_text().strip() == pub.remote_head()
    pub.commit("e.txt", "new\n", "after the switch")
    r = pub.run("push")
    assert r.returncode == 0 and pub.git("rev-list", "--count", "main", cwd=pub.remote) == "2", r.stderr


def test_a_remote_off_the_recorded_root_blocks_and_is_never_force_pushed(pub):
    assert pub.run("push").returncode == 0
    pub.git("push", "-q", "-f", str(pub.remote), "main:refs/heads/main")  # someone else's history lands there
    head = pub.remote_head()
    pub.commit("f.txt", "x\n", "more")
    r = pub.run("push", "--cron")
    assert r.returncode == 3 and "does not descend" in r.stderr
    assert pub.remote_head() == head and len(pub.notes()) == 1


def test_binary_blob_and_gitleaks_finding_block(pub):
    pub.commit("img.bin", None, "add a blob", data=b"\x00\x01binary")
    r = pub.run("push")
    assert r.returncode == 3 and "img.bin:0 binary:unchecked" in r.stderr and pub.remote_head() is None
    leak = json.dumps([{"Commit": pub.git("rev-parse", "HEAD"), "File": "a.txt", "StartLine": 1, "RuleID": "x"}])
    r = pub.run("scan", extra={"FAKE_GITLEAKS_JSON": leak})
    assert r.returncode == 3 and "gitleaks:x" in r.stdout


def test_a_missing_scanner_fails_closed(pub):
    r = pub.run("push", "--cron", extra={"ASSISTANT_PUBLISH_GITLEAKS": str(pub.tmp / "nope")})
    assert r.returncode == 1 and pub.remote_head() is None and len(pub.notes()) == 1


def test_secret_in_a_commit_message_blocks(pub):
    pub.git(
        "-c",
        "user.name=x",
        "-c",
        "user.email=x@example.com",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "token FAKEKEY-123456",
    )
    r = pub.run("scan")
    assert r.returncode == 3 and "<message>:1 secret:pattern:fake-key" in r.stdout and "123456" not in r.stdout
