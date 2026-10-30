#!/usr/local/bin/python3.13 -I
"""Tests for ~/bin/recall. Everything runs in temp dirs (RECALL_HOME, SECRETSCAN_SOURCES); the fake secrets are
assembled at runtime so no file here contains one. The real ~/.local/state/recall is never touched.
Also: recency ranking (a newer correction above a stale line), the FACTS.md boost, DD.MM lead dates, my own notes
with "vault" in the path (reports/vault, memory, skills) indexed while vault data/code (~/opt/claude-vault, also via
symlinks) stays out, skills' SKILL.md only, and the md re-index on an MD_VERSION bump.
SemanticTest: the embedding layer against a fake /v1/embeddings server (concept-group vectors, every input logged):
paraphrase hits, fusion order, incremental vectors (by text hash, kept over --rebuild), --embed + pruning, the live
budget, md chunks only (no transcript, never-index path or secret reaches the embedder), and the text-only fallback."""

import json
import os
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

RECALL = os.environ.get("RECALL_BIN") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "scripts", "recall"
)
# recall imports secretscan from its own dir (as deployed: ~/bin/recall beside ~/bin/secretscan.py; CI's overlay
# stages one beside its copy too). The repo's scripts/ copy has none beside it: point it at ~/bin's.
if "RECALL_SECRETSCAN_DIR" not in os.environ and not os.path.isfile(
    os.path.join(os.path.dirname(os.path.realpath(RECALL)), "secretscan.py")
):
    os.environ["RECALL_SECRETSCAN_DIR"] = os.path.expanduser("~/bin")
HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "fixtures", "session.jsonl")
PROJ = ".claude/projects/-home-node-workspace"
FAKE_VALUE = "zq" + "FAKEtok" + "7" * 12 + "XyZ"  # matched by value (from the temp ~/.secrets)
FAKE_BOT = "123456789:" + "AA" + "k" * 33  # matched by pattern (telegram-bot-token)


class RecallTest(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp(prefix="recall-tests.")
        self.addCleanup(shutil.rmtree, self.t, True)  # runs even when setUp fails
        self.h = os.path.join(self.t, "home")
        for d in (
            "workspace",
            "reports/research",
            "reports/vault",
            PROJ + "/memory",
            ".secrets",
            ".local/state/context",
            "inbox",
        ):
            os.makedirs(os.path.join(self.h, d), exist_ok=True)
        self.w(
            "workspace/CHANGELOG.md",
            "# Changelog\n\n- 2026-10-09: Added the gnu tracker.\n"
            "- 2026-10-11 14:05: Switched the heron cache to sqlite.\n  - sub note about heron\n",
        )
        self.w(
            "workspace/TODO.md", "# TODO\n## In progress\n- **Yak shaving** — running\n\nPlain paragraph about tapir.\n"
        )
        self.w("reports/research/2026-10-10-notes.md", "# Research\n\nThe gecko benchmark was slow.\n")
        self.w(PROJ + "/memory/owner.md", "Owner likes the margay colour scheme.\n")
        self.w(".secrets/test.json", json.dumps({"api_token": FAKE_VALUE}))
        # never-index paths
        self.w(".local/state/context/telegram.md", "private chat about ocelot\n")
        self.w(".local/state/context/signals.md", "signal about ocelot\n")
        self.w(".local/state/context/signals.json", '{"signals": [{"type": "plan", "topic": "ocelot"}]}\n')
        self.w("workspace/signals.json", '{"note": "copied signals about ocelot"}\n')
        # vault data/code outside my own notes (~/opt/claude-vault here), reached through a symlink from reports
        os.makedirs(os.path.join(self.h, "opt/claude-vault/data"), exist_ok=True)
        self.w("opt/claude-vault/data/raw.md", "vault raw text about ocelot\n")
        os.symlink(os.path.join(self.h, "opt/claude-vault/data/raw.md"), os.path.join(self.h, "reports/vault-raw.md"))
        os.symlink(os.path.join(self.h, ".secrets/test.json"), os.path.join(self.h, "reports/leak.md"))
        os.symlink(os.path.join(self.h, ".local/state/context/telegram.md"), os.path.join(self.h, "workspace/tg.md"))
        shutil.copy(FIXTURE, os.path.join(self.h, PROJ, "s1.jsonl"))
        self.env = dict(os.environ, RECALL_HOME=self.h, SECRETSCAN_SOURCES=self.h)
        self.env.pop("RECALL_STATE", None)
        self.env.pop("RECALL_NOW", None)
        for k in ("RECALL_EMBED_URL", "RECALL_EMBED_SERVER", "RECALL_EMBED_MODEL", "RECALL_EMBED_LIVE_S"):
            self.env.pop(k, None)
        self.env["RECALL_EMBED"] = "off"  # text search only here; SemanticTest covers the embeddings
        self.db = os.path.join(self.h, ".local/state/recall/index.db")

    def w(self, rel, text, mode="w"):
        p = os.path.join(self.h, rel)
        with open(p, mode) as f:
            f.write(text)
        return p

    def run_r(self, *args, env=None, rc=None):
        p = subprocess.run([RECALL, *args], env=env or self.env, capture_output=True, text=True, timeout=60)
        if rc is not None:
            self.assertEqual(p.returncode, rc, p.stdout + p.stderr)
        return p

    def q(self, *args):
        return self.run_r(*args).stdout

    def index(self):
        return self.run_r("--index", rc=0).stdout

    def count(self, sql="SELECT COUNT(*) FROM docs"):
        with sqlite3.connect(self.db) as db:
            return db.execute(sql).fetchone()[0]

    def ids(self):
        with sqlite3.connect(self.db) as db:
            return [r[0] for r in db.execute("SELECT id FROM docs ORDER BY id")]

    def append_jsonl(self, rec, newline=True):
        with open(os.path.join(self.h, PROJ, "s1.jsonl"), "a") as f:
            f.write(json.dumps(rec) + ("\n" if newline else ""))

    # ---------------------------------------------------------------- md
    def test_md_dated_bullet_and_location(self):
        self.index()
        out = self.q("heron")
        self.assertIn("2026-10-11 14:05 workspace/CHANGELOG.md:4", out)
        self.assertIn("sub note about heron", out)  # indented sub-bullet stays with its item

    def test_md_sources_and_dates(self):
        self.index()
        self.assertIn("2026-10-10 reports/research/2026-10-10-notes.md", self.q("gecko"))  # date from file name
        self.assertIn(PROJ + "/memory/owner.md", self.q("margay"))
        self.assertIn("workspace/TODO.md", self.q("tapir"))
        self.assertIn("workspace/TODO.md", self.q("yak"))

    def test_never_indexed_paths(self):
        self.index()
        self.assertIn("no hits", self.q("ocelot"))
        with open(self.db, "rb") as f:
            raw = f.read()
        self.assertNotIn(b"ocelot", raw)
        self.assertNotIn(b"api_token", raw)  # the symlink into ~/.secrets was not followed

    # ---------------------------------------------------------------- chat
    def test_chat_kept_parts(self):
        self.index()
        out = self.q("zanzibar")
        self.assertIn("2026-10-09 10:00 chat/owner", out)  # UTC -> Zurich
        self.assertIn("chat/owner tg#42", self.q("quokka"))
        self.assertIn("[button] Approve walrus plan", self.q("walrus"))
        self.assertIn("chat/me ", self.q("narwhal"))
        self.assertIn("chat/me>tg", self.q("axolotl"))
        out = self.q("ibex")
        self.assertIn("chat/agent", out)
        self.assertNotIn("<usage>", out)
        self.assertIn("lemur", self.q("lemur"))

    def test_chat_skipped_parts(self):
        self.index()
        for word in ("pangolin", "platypus", "wombat", "capybara", "okapi"):
            self.assertIn("no hits", self.q(word), word)
        with open(self.db, "rb") as f:
            raw = f.read()
        self.assertNotIn(b"QUJDQUJDQUJD", raw)
        self.assertNotIn(b"iVBORw0KGgo", raw)

    def test_secrets_redacted(self):
        self.w("workspace/NOTES.md", "- 2026-10-12: key is %s and bot %s for dingo\n" % (FAKE_VALUE, FAKE_BOT))  # noqa: UP031
        self.append_jsonl(
            {
                "type": "user",
                "timestamp": "2026-10-12T10:00:00Z",
                "message": {"role": "user", "content": "use token %s for the dingo bot %s" % (FAKE_VALUE, FAKE_BOT)},  # noqa: UP031
            }
        )
        self.index()
        with open(self.db, "rb") as f:
            raw = f.read()
        self.assertNotIn(FAKE_VALUE.encode(), raw)
        self.assertNotIn(FAKE_VALUE.lower().encode(), raw)
        self.assertNotIn(b"k" * 33, raw)
        out = self.q("dingo", "-n", "5")
        self.assertEqual(out.count("[redacted]"), 4, out)
        self.assertNotIn(FAKE_VALUE, out)

    def test_fail_closed_without_secretscan(self):
        env = dict(self.env, RECALL_SECRETSCAN_DIR=os.path.join(self.t, "nothing-here"))
        p = self.run_r("--index", env=env)
        self.assertEqual(p.returncode, 2)
        self.assertIn("fail-closed", p.stderr)
        self.assertEqual(self.count(), 0)
        p = self.run_r("--index", "--cron", env=env)
        self.assertEqual(len(os.listdir(os.path.join(self.h, "inbox"))), 1)

    # ---------------------------------------------------------------- incremental
    def test_jsonl_incremental_append_and_partial_line(self):
        self.index()
        n0 = self.count()
        first_ids = self.ids()
        rec = {
            "type": "assistant",
            "timestamp": "2026-10-12T09:00:00Z",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "the jackal job finished"}]},
        }
        self.append_jsonl(rec, newline=False)  # still being written
        self.index()
        self.assertEqual(self.count(), n0)
        self.assertIn("no hits", self.q("jackal"))
        with open(os.path.join(self.h, PROJ, "s1.jsonl"), "a") as f:
            f.write("\n")
        out = self.index()
        self.assertIn("+1 chunks", out)
        self.assertEqual(self.count(), n0 + 1)
        self.assertEqual(self.ids()[:n0], first_ids)  # old rows untouched
        self.assertIn("2026-10-12 11:00 chat/me", self.q("jackal"))
        self.assertIn("+0 chunks from 0 changed", self.index())

    def test_jsonl_rewritten_is_reindexed(self):
        self.index()
        p = os.path.join(self.h, PROJ, "s1.jsonl")
        with open(p, "w") as f:
            f.write(
                json.dumps(
                    {
                        "type": "user",
                        "timestamp": "2026-10-12T09:00:00Z",
                        "message": {"role": "user", "content": "only the kudu now"},
                    }
                )
                + "\n"
            )
        self.index()
        self.assertIn("no hits", self.q("zanzibar"))
        self.assertIn("kudu", self.q("kudu"))

    def test_md_change_and_delete(self):
        self.index()
        self.w("workspace/TODO.md", "# TODO\n- **Bison task** — new\n")
        os.remove(os.path.join(self.h, "reports/research/2026-10-10-notes.md"))
        self.index()
        self.assertIn("no hits", self.q("tapir"))
        self.assertIn("no hits", self.q("gecko"))
        self.assertIn("Bison", self.q("bison"))

    def test_auto_refresh_when_stale(self):
        self.index()
        with sqlite3.connect(self.db) as db:
            last = float(db.execute("SELECT v FROM meta WHERE k='last_index'").fetchone()[0])
        self.w("workspace/GOALS.md", "- 2026-10-12: Goal about the eland project\n")
        env = dict(self.env, RECALL_NOW=str(last + 10 * 60))
        self.assertIn("no hits", self.run_r("eland", env=env).stdout)  # 10 min old: no refresh
        env = dict(self.env, RECALL_NOW=str(last + 31 * 60))
        self.assertIn("workspace/GOALS.md", self.run_r("eland", env=env).stdout)

    def test_first_query_builds_index(self):
        self.assertFalse(os.path.exists(self.db))
        self.assertIn("CHANGELOG", self.q("heron"))

    # ---------------------------------------------------------------- search & output
    def test_filters(self):
        self.index()
        self.assertIn("no hits", self.q("heron", "--src", "chat"))
        self.assertIn("CHANGELOG", self.q("heron", "--src", "md"))
        self.assertIn("no hits", self.q("zanzibar", "--src", "md"))
        self.assertIn("no hits", self.q("heron", "--since", "2026-10-12"))
        self.assertIn("CHANGELOG", self.q("heron", "--since", "2026-10-11"))
        self.assertEqual(self.run_r("x", "--since", "12.10.2026").returncode, 2)

    def test_natural_question_and_fallback(self):
        self.index()
        self.assertIn("chat/me", self.q("what did we decide about the narwhal migration?"))
        out = self.q("narwhal gnu")  # no chunk has both: falls back to either
        self.assertIn("narwhal", out)
        self.assertIn("gnu", out)
        self.assertIn("CHANGELOG", self.q('"heron cache"'))
        self.assertIn("CHANGELOG", self.q("her*"))
        self.assertIn("CHANGELOG", self.q("heron NOT gecko", "--raw"))
        self.assertIn("bad query", self.run_r('"(', "--raw").stderr)

    def test_output_is_short(self):
        lines = "\n".join(
            f"- 2026-10-{1 + i % 28:02d}: item {i} about the serval rollout {'x ' * 200}" for i in range(120)
        )
        self.w("reports/serval.md", "# Serval\n" + lines + "\n")
        self.index()
        out = self.q("serval", "-n", "500")
        rows = out.strip().split("\n")
        self.assertLessEqual(len(rows), 41)
        self.assertEqual(len(rows), 41)
        self.assertTrue(all(len(r) < 320 for r in rows), max(len(r) for r in rows))
        self.assertLessEqual(len(self.q("serval").strip().split("\n")), 11)

    def test_by_date(self):
        self.index()
        out = self.q("heron OR gnu", "--raw", "--by-date").strip().split("\n")[1:]
        self.assertIn("2026-10-11", out[0])
        self.assertIn("2026-10-09", out[1])

    def test_show_with_context(self):
        self.index()
        hit = [l for l in self.q("narwhal").split("\n") if l.startswith("#")][0]  # noqa: E741
        cid = hit.split()[0][1:]
        out = self.q("--show", cid, "-C", "1")
        self.assertIn(">> #" + cid, out)
        self.assertIn("s1.jsonl"[:2], out)
        self.assertIn("Decided: the narwhal migration runs nightly at 03:00.", out)
        self.assertEqual(
            sum(ln.startswith("-- #") for ln in out.split("\n")), 2, out
        )  # one neighbour before, one after
        self.assertEqual(self.run_r("--show", "99999").returncode, 1)

    def test_permissions_and_stats(self):
        self.index()
        self.assertEqual(stat.S_IMODE(os.stat(self.db).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(self.db)).st_mode), 0o700)
        self.assertRegex(
            self.q("--stats"), r"recall index: \d+ md chunks \(4 files\), \d+ chat chunks \(1 transcripts\)"
        )

    def test_rebuild_is_idempotent(self):
        self.index()
        n = self.count()
        self.run_r("--rebuild", rc=0)
        self.assertEqual(self.count(), n)

    # ---------------------------------------------------------------- ranking, dates, own vault notes, skills
    def now_env(self, y=2026, mo=10, d=19, hh=12):
        ts = datetime(y, mo, d, hh, 0, tzinfo=ZoneInfo("Europe/Zurich")).timestamp()
        return dict(self.env, RECALL_NOW=str(int(ts))), ts

    @staticmethod
    def hits(out):
        return [line for line in out.split("\n") if line.startswith("#")]

    def test_recency_newer_correction_ranks_first(self):
        # bm25 alone puts the stale line first (short, repeats the words); the newer correction has to win
        self.w(
            "reports/research/door-notes.md",
            "# Notes\n\n- 2026-09-01 zorilla app: the zorilla panel uses the Home app.\n",
        )
        self.w(
            "workspace/NOTES.md",
            "# Door\n\n- 2026-10-19 Correction: the zorilla panel runs the old Gate Pass app, "
            "not the Home Hub app noted before.\n",
        )
        env, _ = self.now_env()
        self.run_r("--index", env=env, rc=0)
        h = self.hits(self.run_r("zorilla app", env=env).stdout)
        self.assertEqual(len(h), 2, h)
        self.assertIn("workspace/NOTES.md", h[0])
        self.assertIn("2026-09-01", h[1])
        h = self.hits(self.run_r("zorilla app", "--by-date", env=env).stdout)
        self.assertIn("2026-10-19", h[0])

    def test_recency_keeps_all_words_tier_first(self):
        # an old hit with every word still beats a fresh hit with only some of them
        self.w(
            "reports/research/2026-01-05-pangolin.md", "# Old\n\nThe pangolin firmware update broke the zebu bridge.\n"
        )
        self.w("workspace/NOTES.md", "# New\n\n- 2026-10-19 The pangolin was fed.\n")
        env, _ = self.now_env()
        self.run_r("--index", env=env, rc=0)
        h = self.hits(self.run_r("pangolin zebu", env=env).stdout)
        self.assertEqual(len(h), 2, h)
        self.assertIn("2026-01-05", h[0])

    def test_facts_md_boost(self):
        # same date; the report line matches a bit better (okapi twice), FACTS.md still has to come first
        self.w("workspace/FACTS.md", "# Facts\n\n- Intercom: the okapi panel runs the Gate Pass app\n")
        self.w("reports/research/okapi.md", "# Old note\n\n- Intercom: okapi panel (okapi) on the Gate Pass app\n")
        env, ts = self.now_env()
        for rel in ("workspace/FACTS.md", "reports/research/okapi.md"):
            os.utime(os.path.join(self.h, rel), (ts, ts))
        self.run_r("--index", env=env, rc=0)
        h = self.hits(self.run_r("okapi Gate Pass", env=env).stdout)
        self.assertIn("workspace/FACTS.md", h[0])

    def test_dd_mm_lead_dates(self):
        p = self.w(
            "workspace/PROACTIVE.md",
            "# Log\n\n- 19.10 00:05 quokka OKs from the poll\n- 2.5 Gbit quokka line\n"
            "- 1.6.3 quokka stable\n- 7.3 18:00 quokka tax\n- 05.01 quokka january\n- 3.11.2025 quokka yearly\n",
        )
        env, ts = self.now_env(2026, 12, 20)
        os.utime(p, (ts, ts))
        self.run_r("--index", env=env, rc=0)
        want = {
            "OKs": "2026-10-19 00:05 workspace/PROACTIVE.md",
            "Gbit": "2026-12-20 workspace/PROACTIVE.md",
            "stable": "2026-12-20 workspace/PROACTIVE.md",
            "tax": "2027-03-07 18:00 workspace/PROACTIVE.md",
            "january": "2027-01-05 workspace/PROACTIVE.md",
            "yearly": "2025-11-03 workspace/PROACTIVE.md",
        }
        for word, prefix in want.items():
            h = self.hits(self.run_r("quokka " + word, env=env).stdout)
            self.assertIn(" " + prefix + ":", h[0], word)

    def test_own_vault_notes_and_skills_indexed(self):
        self.w("reports/vault/plan-2026-10-15.md", "# Vault plan\n\nThe aardvark index stays in the vault.\n")
        self.w(PROJ + "/memory/ask-vault-first.md", "Ask the vault first about aardvark questions.\n")
        os.makedirs(os.path.join(self.h, ".claude/skills/vault/references"))
        self.w(
            ".claude/skills/vault/SKILL.md",
            "---\nname: vault\n---\n# Vault\n\nThe aardvark sync runs in the vault bot.\n",
        )
        self.w(".claude/skills/vault/references/extra.md", "aardvark reference detail\n")
        self.index()
        out = self.q("aardvark", "-n", "10")
        self.assertIn("reports/vault/plan-2026-10-15.md", out)
        self.assertIn(PROJ + "/memory/ask-vault-first.md", out)
        self.assertIn(".claude/skills/vault/SKILL.md", out)
        self.assertNotIn("references/extra.md", out)  # only SKILL.md per skill
        self.assertIn("no hits", self.q("ocelot"))  # the symlink into ~/opt/claude-vault stays out

    def test_vault_dirs_outside_own_notes_stay_out(self):
        os.makedirs(os.path.join(self.h, "opt/claude-vault/skills/x"))
        self.w("opt/claude-vault/skills/x/SKILL.md", "ocelot skill inside the vault repo\n")
        os.makedirs(os.path.join(self.h, ".claude/skills"), exist_ok=True)
        os.symlink(os.path.join(self.h, "opt/claude-vault/skills/x"), os.path.join(self.h, ".claude/skills/x"))
        os.symlink(os.path.join(self.h, "opt/claude-vault/data"), os.path.join(self.h, "reports/vaultdata"))
        self.index()
        self.assertIn("no hits", self.q("ocelot"))
        with open(self.db, "rb") as f:
            self.assertNotIn(b"ocelot", f.read())

    def test_agents_script_headers_and_git_logs(self):
        os.makedirs(os.path.join(self.h, ".claude/agents"), exist_ok=True)
        self.w(".claude/agents/tidy.md", "---\nname: tidy\n---\nThe tidy agent archives the quokka scripts.\n")
        os.makedirs(os.path.join(self.h, "bin/lib"), exist_ok=True)
        self.w(
            "bin/quokka",
            '#!/usr/bin/python3\n"""quokka: counts the wombat burrows per night.\n\n  quokka run   one pass\n'
            '"""\nimport os\nWORD = "numbat"\n',
        )
        self.w(
            "bin/lib/burrow.sh",
            "#!/bin/sh\n# burrow helper: digs the bilby tunnels.\n# more about tunnels\nset -e\necho numbat\n",
        )
        self.w("bin/old.bak", "# backup copy about bilby\n")
        self.w("bin/vault-thing", "#!/bin/sh\n# vault wrapper about bilby\n")
        g = dict(
            self.env,
            GIT_AUTHOR_NAME="t",
            GIT_AUTHOR_EMAIL="t@example.invalid",
            GIT_COMMITTER_NAME="t",
            GIT_COMMITTER_EMAIL="t@example.invalid",
            GIT_AUTHOR_DATE="2026-10-12T10:00:00",
            GIT_COMMITTER_DATE="2026-10-12T10:00:00",
        )
        for repo, msg in (
            ("opt/potoroo", "Taught the potoroo the bandicoot dance\n\nCo-Authored-By: X <x@example.invalid>"),
            ("opt/claude-vault", "vault commit about bilby"),
        ):
            r = os.path.join(self.h, repo)
            os.makedirs(r, exist_ok=True)
            subprocess.run(["git", "-C", r, "init", "-q"], env=g, check=True, capture_output=True)
            subprocess.run(
                ["git", "-C", r, "commit", "-q", "--allow-empty", "-m", msg], env=g, check=True, capture_output=True
            )
        self.index()
        self.assertIn(".claude/agents/tidy.md", self.q("quokka scripts archives"))
        self.assertIn("bin/quokka:2", self.q("wombat burrows"))
        self.assertIn("bin/lib/burrow.sh:2", self.q("bilby tunnels"))
        out = self.q("bilby", "-n", "20")
        for bad in ("old.bak", "vault-thing", "vault commit"):
            self.assertNotIn(bad, out)
        self.assertIn("no hits", self.q("numbat"))  # code below the header is never indexed
        out = self.q("bandicoot dance")
        self.assertIn("opt/potoroo@", out)
        self.assertIn("2026-10-12", out)
        self.assertNotIn("Co-Authored", out)
        self.index()  # unchanged sources: nothing re-indexed
        self.assertIn("+0 chunks", self.index())

    def test_md_version_bump_reindexes_md_only(self):
        self.index()
        sql = "SELECT id FROM docs WHERE src=? ORDER BY id"
        with sqlite3.connect(self.db) as db:
            md = [r[0] for r in db.execute(sql, ("md",))]
            chat = [r[0] for r in db.execute(sql, ("chat",))]
            db.execute("UPDATE meta SET v='0' WHERE k='md_version'")
        self.index()
        with sqlite3.connect(self.db) as db:
            md2 = [r[0] for r in db.execute(sql, ("md",))]
            chat2 = [r[0] for r in db.execute(sql, ("chat",))]
            ver = db.execute("SELECT v FROM meta WHERE k='md_version'").fetchone()[0]
        self.assertNotEqual(ver, "0")
        self.assertEqual(chat2, chat)  # transcripts untouched
        self.assertEqual(len(md2), len(md))
        self.assertTrue(set(md2).isdisjoint(md))  # md chunks re-inserted


CONCEPTS = [
    {"voice", "call", "calls", "phone", "video", "talk", "conversations"},
    {"doorbell", "intercom", "bell", "ring"},
    {"gnu", "tracker", "tracking"},
    {"cat", "litter", "feeder", "kitten"},
]


def fake_vec(text):
    """Concept-group counts + 8 hashed buckets (weight 0.1) + a constant: paraphrases share a concept dim."""
    words = [w.strip(".,:;!?()[]*\"'") for w in text.lower().split()]
    v = [0.0] * (len(CONCEPTS) + 9)
    for w in words:
        hit = [i for i, c in enumerate(CONCEPTS) if w in c]
        if hit:
            v[hit[0]] += 1.0
        elif w:
            v[len(CONCEPTS) + sum(map(ord, w)) % 8] += 0.1
    v[-1] = 0.05
    return v


class FakeEmbed(BaseHTTPRequestHandler):
    log = []

    def do_POST(self):
        d = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        data = []
        for i, t in enumerate(d["input"]):
            if t.startswith("task: search result | query: "):
                kind, body = "query", t.split("query: ", 1)[1]
            else:
                kind, body = "doc", t.split(" | text: ", 1)[-1]
            FakeEmbed.log.append((kind, t))
            data.append({"index": i, "embedding": fake_vec(body)})
        out = json.dumps({"data": data}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


class SemanticTest(unittest.TestCase):
    w, run_r, q, index, count = RecallTest.w, RecallTest.run_r, RecallTest.q, RecallTest.index, RecallTest.count

    def setUp(self):
        RecallTest.setUp(self)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeEmbed)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        FakeEmbed.log = []
        self.env.pop("RECALL_EMBED")
        self.env.update(RECALL_EMBED_URL="http://127.0.0.1:%d" % self.srv.server_address[1], RECALL_EMBED_LIVE_S="30")  # noqa: UP031
        self.w(
            "workspace/FACTS.md",
            "# Facts\n## Done / declined ledger\n"
            "- 20.10 decided: video calls via the spare tablet\n"
            "- 19.10 declined: intercom bridge (old model)\n",
        )
        self.state = os.path.dirname(self.db)

    def docs(self):
        return [t for k, t in FakeEmbed.log if k == "doc"]

    def queries(self):
        return [t for k, t in FakeEmbed.log if k == "query"]

    def md_chunks(self):
        return self.count("SELECT COUNT(*) FROM emb")

    def test_paraphrase_found_only_semantically(self):
        self.index()
        p = self.run_r("--lexical", "phone conversations", rc=1)
        self.assertIn("no hits", p.stdout)
        self.assertEqual(self.queries(), [])
        out = self.run_r("phone conversations", rc=0).stdout.splitlines()
        self.assertRegex(out[0], r'^recall: \d+ hits for "phone conversations" · index \d+m old · recall --show ID')
        self.assertRegex(out[1], r"^#\d+ \d{4}-\d\d-\d\d workspace/FACTS\.md:3  - 20\.10 decided: video calls via")
        self.assertNotIn("intercom", "\n".join(out))  # below the cosine floor
        self.assertEqual(self.queries(), ["task: search result | query: phone conversations"])

    def test_fusion_keeps_text_hits_first(self):
        self.w("workspace/NOTES.md", "# Notes\n\nTracking ideas for later.\n")
        self.index()
        out = self.q("gnu tracker").splitlines()
        self.assertIn("Added the gnu tracker", out[1])  # text + semantic hit stays on top
        self.assertTrue(any("Tracking ideas" in x for x in out[2:]), out)  # semantic-only hit follows
        self.assertEqual(len(self.q("gnu tracker", "-n", "1").splitlines()), 2)

    def test_incremental_vectors_and_rebuild(self):
        self.index()
        self.q("phone")
        n = self.md_chunks()
        self.assertEqual(len(self.docs()), n)  # live top-up embedded every md chunk once
        FakeEmbed.log = []
        self.q("phone")
        self.assertEqual(self.docs(), [])
        self.w("workspace/CHANGELOG.md", "- 2026-10-12: Moved the kitten feeder.\n", mode="a")
        self.index()
        self.q("phone")
        self.assertEqual(len(self.docs()), 1, self.docs())  # the file re-indexed, only the new chunk embedded
        self.assertIn("Moved the kitten feeder", self.docs()[0])
        FakeEmbed.log = []
        self.run_r("--rebuild", rc=0)
        out = self.q("cat")
        self.assertEqual(self.docs(), [])  # vectors survive --rebuild (keyed by text hash)
        self.assertIn("kitten feeder", out)

    def test_embed_pass_budget_prune_and_stats(self):
        env = dict(self.env, RECALL_EMBED_LIVE_S="0")
        self.index()
        self.assertIn("no hits", self.run_r("phone conversations", env=env, rc=1).stdout)
        self.assertEqual(self.docs(), [])
        p = self.run_r("--embed", env=env, rc=0)
        n = self.md_chunks()
        self.assertIn("embedded %d chunks" % n, p.stdout)  # noqa: UP031
        self.assertIn("%d of %d md chunks have vectors" % (n, n), p.stdout)  # noqa: UP031
        self.assertIn("video calls", self.run_r("phone conversations", env=env, rc=0).stdout)
        self.assertRegex(self.q("--stats"), r"recall semantic: %d of %d md chunks embedded \(url;" % (n, n))  # noqa: UP031
        FakeEmbed.log = []
        os.remove(os.path.join(self.h, "reports/research/2026-10-10-notes.md"))
        self.index()
        p = self.run_r("--embed", env=env, rc=0)
        self.assertIn("embedded 0 chunks", p.stdout)
        self.assertIn("1 stale vectors dropped", p.stdout)
        self.assertEqual(self.docs(), [])
        for f in ("vectors.db", "vec-matrix.npy", "vec-ids.npy", "vec-matrix.json"):
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.state, f)).st_mode), 0o600, f)

    def test_only_md_chunks_reach_the_embedder(self):
        self.w("workspace/NOTES.md", "Token note: %s and %s here.\n" % (FAKE_VALUE, FAKE_BOT))  # noqa: UP031
        self.run_r("--embed", rc=0)
        sent = "\n".join(self.docs())
        self.assertIn("video calls", sent)
        self.assertIn("Token note", sent)
        for bad in ("zanzibar", "Checking the backup job", "pangolin", "ocelot", FAKE_VALUE, FAKE_BOT, "api_token"):
            self.assertNotIn(bad, sent)
        with open(os.path.join(self.state, "vec-matrix.npy"), "rb") as f:
            self.assertNotIn(b"Token note", f.read())
        self.run_r("--src", "chat", "zanzibar", rc=0)
        self.run_r("--raw", "gnu", rc=0)
        self.assertEqual(self.queries(), [])

    def test_embedder_down_falls_back_to_text(self):
        self.index()
        for env in (
            dict(self.env, RECALL_EMBED_URL="http://127.0.0.1:1"),
            dict(self.env, RECALL_EMBED_URL="", RECALL_EMBED_MODEL=os.path.join(self.t, "none.gguf")),
        ):
            p = self.run_r("gnu tracker", env=env, rc=0)
            self.assertIn("Added the gnu tracker", p.stdout)
            self.assertIn("semantic search off", p.stderr)
            self.assertEqual(p.stderr.count("\n"), 1, p.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=1)
