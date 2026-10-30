"""Tests for ~/bin/memory2, ~/bin/kb and the memory-recall hook. Temp dirs only (MEMORY2_HOME/_STATE/_CONF,
RECALL_HOME, SECRETSCAN_SOURCES); a fake embedding server (hashed bag of words) and a fake OpenAI-style LLM run on
loopback threads. Synthetic fixtures only: no personal values."""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BIN = os.environ.get("MEMORY2_BIN") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "scripts", "memory2"
)
KB = os.environ.get("KB_BIN") or os.path.join(  # the tree's kb
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "scripts", "kb"
)


def _launch(path):
    """A script by its shebang; the CI runner has no /usr/bin/python3 (kb's, recall's): its own interpreter then."""
    with open(path, "rb") as f:
        first = f.readline().decode()
    parts = first[2:].split() if first.startswith("#!") else []
    if parts and parts[0].startswith("/") and "python" in parts[0] and not os.path.exists(parts[0]):
        return [sys.executable, *parts[1:], path]
    return [path]


CORE = os.path.join(os.path.dirname(os.path.realpath(BIN)), "lib", "memory2_core.py")
HOOK = os.environ.get("M2_HOOK_SCRIPT", "/home/node/.claude/hooks/memory-recall.sh")
PROJ = ".claude/projects/-home-node-workspace"

FACTS = """# Facts
Intro line.

## Server & hardware
- Next disk <= 16 TB (bigger needs bigger parity) — live check, 19.10
- The gnu tracker runs nightly at 03:10 on the heron box — live, 18.10

## Apps & services
- Wombat dashboard sits behind basic auth — dashboard fix, 20.10

## Done / declined ledger
Newest first, one line each.
- 20.10 declined: quokka briefings (he reads the news himself)
- 19.10 done: Disk 12 added (a free bay is left)
"""
MEMORY = '---\nname: margay-colours\ndescription: "Owner likes the margay colour scheme on cards"\n---\n\nUse margay colours on every card.\n'  # noqa: E501


def bow(text):
    text = re.sub(r"^(title: .*? \| text: |task: search result \| query: )", "", text)
    v = [0.0] * 64
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        if len(w) < 3:
            continue
        v[int(hashlib.md5(w[:5].encode()).hexdigest(), 16) % 64] += 1.0
    n = sum(x * x for x in v) ** 0.5 or 1.0
    return [x / n for x in v]


class Fake(BaseHTTPRequestHandler):
    hang = False

    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.server.hang:
            time.sleep(3)
        self._send({"status": "ok", "model_path": "/x/fake.gguf"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.server.hang:
            time.sleep(3)
        if self.path.endswith("/embeddings"):
            self._send({"data": [{"index": i, "embedding": bow(t)} for i, t in enumerate(body["input"])]})
            return
        sys_msg = body["messages"][0]["content"]
        user = body["messages"][-1]["content"]
        if sys_msg.startswith("You turn one message"):
            owner = user.split("Owner:", 1)[-1]
            if "never" in owner.lower():
                out = {
                    "items": [
                        {
                            "kind": "decision",
                            "subject": "gnu pings",
                            "confidence": 0.95,
                            "text": "He never wants pings about the gnu tracker.",
                        }
                    ]
                }
            elif "feature idea" in owner:
                out = {"items": []}
            else:
                out = {"items": [{"kind": "fact", "subject": "misc", "text": owner.strip()[:80], "confidence": 0.6}]}
        else:
            out = {"relation": "consistent", "confidence": 0.5}
        self._send({"choices": [{"message": {"content": json.dumps(out)}}]})


def serve(hang=False):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    srv.hang = hang
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]  # noqa: UP031


def stop(srv):
    srv.shutdown()
    srv.server_close()


def rec(kind, text, ts, mid=None):
    if kind == "owner":
        c = '<channel source="telegram" chat_id="1" message_id="%s" user="o" ts="%s">%s</channel>' % (mid, ts, text)  # noqa: UP031
        return {"type": "user", "timestamp": ts, "message": {"content": c}}
    return {"type": "assistant", "timestamp": ts, "message": {"content": [{"type": "text", "text": text}]}}


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv, cls.url = serve()

    @classmethod
    def tearDownClass(cls):
        stop(cls.srv)

    def setUp(self):
        self.t = tempfile.mkdtemp(prefix="memory2-tests-")
        self.h = os.path.join(self.t, "home")
        for d in ("workspace", PROJ + "/memory", "inbox", "reports"):
            os.makedirs(os.path.join(self.h, d))
        self.w("workspace/FACTS.md", FACTS)
        self.w(PROJ + "/memory/margay-colours.md", MEMORY)
        self.w(PROJ + "/memory/MEMORY.md", "- [Margay](margay-colours.md) — index line\n")
        self.state = os.path.join(self.t, "state")
        self.env = dict(
            os.environ,
            MEMORY2_HOME=self.h,
            MEMORY2_STATE=self.state,
            MEMORY2_CONF=os.path.join(self.t, "conf"),
            MEMORY2_EMBED_URL=self.url,
            MEMORY2_LLM="off",
            USAGE="off",
            SECRETSCAN_SOURCES=self.t,
            MEMORY2_HOOK_CWD=os.path.join(self.h, "workspace"),
            RECALL_HOME=self.h,
            RECALL_EMBED="off",
            MEMORY2_NOW=str(time.mktime((2026, 10, 21, 12, 0, 0, 0, 0, -1))),
        )
        for k in ("MEMORY2_LLM_URL", "MEMORY2_HOOK", "RECALL_STATE", "KB_EVAL"):
            self.env.pop(k, None)

    def tearDown(self):
        shutil.rmtree(self.t, ignore_errors=True)

    def w(self, rel, text, mode="w"):
        p = os.path.join(self.h, rel)
        with open(p, mode) as f:
            f.write(text)
        return p

    def run_m(self, *args, env=None, rc=0, binp=None):
        p = subprocess.run(
            [*_launch(binp or BIN), *args], env=env or self.env, capture_output=True, text=True, timeout=60
        )
        if rc is not None:
            self.assertEqual(p.returncode, rc, p.stdout + p.stderr)
        return p.stdout

    def db(self, sql, *a):
        with sqlite3.connect(os.path.join(self.state, "mem.db")) as c:
            return c.execute(sql, a).fetchall()

    def hook(self, prompt, cwd=None, env=None, timeout=10):
        payload = json.dumps(
            {"prompt": prompt, "cwd": cwd or os.path.join(self.h, "workspace"), "hook_event_name": "UserPromptSubmit"}
        )
        t0 = time.time()
        p = subprocess.run(
            [sys.executable, "-I", "-S", CORE],
            input=payload,
            env=env or self.env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return p, (time.time() - t0) * 1000


class SyncSearch(Base):
    def test_sync_parses_facts_ledger_memory(self):
        out = self.run_m("sync")
        self.assertIn("6 active facts", out)
        led = self.db("SELECT verb, thing, date, source FROM facts WHERE origin='ledger' ORDER BY line")
        self.assertEqual(led[0], ("declined", "quokka briefings", "2026-10-20", "FACTS.md:13"))
        self.assertEqual(led[1][:2], ("done", "Disk 12 added"))
        f = self.db("SELECT subject, date, source FROM facts WHERE text LIKE 'Next disk%'")[0]
        self.assertEqual(f, ("Server & hardware", "2026-10-19", "FACTS.md:5"))
        m = self.db("SELECT subject, source FROM facts WHERE origin='memory'")
        self.assertEqual(m, [("margay-colours", "memory/margay-colours.md:5")])
        self.assertEqual(self.db("SELECT COUNT(*) FROM facts WHERE vec IS NOT NULL")[0][0], 6)
        self.assertEqual(oct(os.stat(os.path.join(self.state, "mem.db")).st_mode & 0o777), "0o600")

    def test_removed_line_becomes_gone_never_deleted(self):
        self.run_m("sync")
        self.w(
            "workspace/FACTS.md",
            FACTS.replace("- Wombat dashboard sits behind basic auth — dashboard fix, 20.10\n", ""),
        )
        out = self.run_m("sync")
        self.assertIn("1 gone", out)
        self.assertEqual(self.db("SELECT status FROM facts WHERE text LIKE 'Wombat%'"), [("gone",)])
        self.assertEqual(self.run_m("search", "wombat dashboard auth", rc=1), "")

    def test_search_hybrid_and_floor(self):
        self.run_m("sync")
        out = self.run_m("search", "which tracker runs at night on the heron box")
        self.assertIn("FACTS.md:6", out.split("\n")[0])
        self.run_m("search", "pelican recipe for dinner", rc=1)
        lex = dict(self.env, MEMORY2_EMBED_URL="http://127.0.0.1:1")
        self.assertIn("FACTS.md:6", self.run_m("search", "gnu tracker nightly", env=lex))


class Hook(Base):
    def setUp(self):
        super().setUp()
        self.run_m("sync")

    def ctx(self, p):
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)["hookSpecificOutput"]["additionalContext"] if p.stdout else ""

    def test_injects_relevant_facts_fast(self):
        p, ms = self.hook(
            '<channel source="telegram" chat_id="1" message_id="5" user="o" ts="x">should I pitch '
            "quokka briefings again?</channel>"
        )
        c = self.ctx(p)
        self.assertIn("quokka briefings", c)
        self.assertIn("[ledger 20.10 declined]", c)
        self.assertIn("(FACTS.md:13)", c)
        self.assertLess(ms, 400)
        self.assertLess(len(c), 1000)
        with open(os.path.join(self.state, "hook.log")) as f:
            last = f.read().strip().split("\n")[-1].split()
        self.assertEqual((last[2], last[4]), ("1", "ok"))

    def test_skips_other_cwd_heartbeats_taps_and_short(self):
        self.assertEqual(self.hook("quokka briefings again", cwd=self.t)[0].stdout, "")
        self.assertEqual(self.hook("[heartbeat] quokka briefings and the gnu tracker")[0].stdout, "")
        tap = (
            '<channel source="telegram" chat_id="1" message_id="5" user="o" ts="x" event_type="button" '
            'button_text="Yes">quokka briefings</channel>'
        )
        self.assertEqual(self.hook(tap)[0].stdout, "")
        self.assertEqual(self.hook("ok thanks")[0].stdout, "")

    def test_kill_switches(self):
        self.assertTrue(self.hook("quokka briefings again please")[0].stdout)
        self.assertIn("off", self.run_m("hook", "off"))
        self.assertEqual(self.hook("quokka briefings again please")[0].stdout, "")
        self.run_m("hook", "on")
        self.assertEqual(self.hook("quokka briefings again", env=dict(self.env, MEMORY2_HOOK="off"))[0].stdout, "")

    def test_fail_open_and_budget(self):
        srv, url = serve(hang=True)
        try:
            p, ms = self.hook("gnu tracker nightly heron", env=dict(self.env, MEMORY2_EMBED_URL=url))
        finally:
            stop(srv)
        self.assertIn("gnu tracker", self.ctx(p))  # text hits still come back ...
        self.assertLess(ms, 700)  # ... within the budget, not after the 3 s hang
        with open(os.path.join(self.state, "mem.db"), "wb") as f:
            f.write(b"not a database")
        p, _ = self.hook("gnu tracker nightly heron")
        self.assertEqual((p.returncode, p.stdout), (0, ""))
        self.assertIn("err:", open(os.path.join(self.state, "hook.log")).read())
        p, _ = self.hook("{not json", env=self.env)
        self.assertEqual(p.returncode, 0)

    def test_shell_wrapper_fail_open(self):
        if not os.path.exists(HOOK):
            self.skipTest("hook not installed")
        env = dict(self.env, MEMORY2_CORE=os.path.join(self.t, "missing.py"))
        p = subprocess.run([HOOK], input="{}", env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual((p.returncode, p.stdout), (0, ""))
        env = dict(self.env, MEMORY2_CORE=CORE)
        payload = json.dumps({"prompt": "quokka briefings again please", "cwd": os.path.join(self.h, "workspace")})
        p = subprocess.run([HOOK], input=payload, env=env, capture_output=True, text=True, timeout=10)
        self.assertIn("quokka", p.stdout)


class Extract(Base):
    def transcript(self, name, recs):
        p = os.path.join(self.h, PROJ, name)
        with open(p, "a") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")
        return p

    def recs(self):
        return [
            rec("me", "Should I keep sending you pings about the gnu tracker?", "2026-10-21T08:00:00Z"),
            rec("owner", "From now on never send me pings about the gnu tracker.", "2026-10-21T08:01:00Z", 11),
            rec("owner", "can you check the heron box?", "2026-10-21T08:02:00Z", 12),
            rec("owner", "remind me at 5 to water the plants", "2026-10-21T08:03:00Z", 13),
            rec("owner", "I have a feature idea.", "2026-10-21T08:04:00Z", 14),
            rec(
                "owner",
                "My therapist says I should sleep more, I have a session on Monday.",
                "2026-10-21T08:05:00Z",
                15,
            ),
            rec(
                "owner",
                '<channel source="telegram" chat_id="1" message_id="16" user="o" ts="x" event_type="button" '
                'button_text="✅ Apply all 3" message_text="Sort 3 docs?"></channel>',
                "2026-10-21T08:06:00Z",
                16,
            ),
            rec("owner", "Actually the wombat dashboard is public now, no basic auth.", "2026-10-21T08:07:00Z", 17),
        ]

    def test_first_run_starts_at_the_end_then_incremental(self):
        self.transcript("s1.jsonl", self.recs())
        out = self.run_m("extract")
        self.assertIn("turns 0", out)
        self.transcript(
            "s1.jsonl", [rec("owner", "I decided to keep the heron box on power save.", "2026-10-21T09:00:00Z", 20)]
        )
        out = self.run_m("extract")
        self.assertIn("turns 1", out)
        self.assertIn("cands 1", out)
        self.assertIn("auto 0", out)  # no local model: never auto-accepted
        self.assertEqual(oct(os.stat(os.path.join(self.state, "candidates.jsonl")).st_mode & 0o777), "0o600")

    def test_backfill_classifies_filters_and_dedupes(self):
        self.transcript("s1.jsonl", self.recs())
        self.transcript("s2.jsonl", self.recs()[:2])  # a resumed session repeats turns
        out = self.run_m("extract", "--dry-run", "--backfill", "1")
        lines = out.strip().split("\n")
        st = json.loads(lines[-1])
        self.assertEqual(st["dropped_sensitive"], 1)
        texts = "\n".join(lines[:-1])
        self.assertEqual(texts.count("never send me pings"), 1)
        self.assertNotIn("check the heron", texts)  # question
        self.assertNotIn("water the plants", texts)  # command
        self.assertNotIn("Apply all", texts)  # one-off tap
        self.assertNotIn("therapist", texts)  # sensitive
        self.assertRegex(texts, r"decision +0\.8\d .*never send me pings")
        self.assertRegex(texts, r"correction .*wombat dashboard")
        self.assertFalse(os.path.exists(os.path.join(self.state, "candidates.jsonl")))  # dry run writes nothing

    def test_llm_auto_accept_conflict_and_review(self):
        srv, url = serve()
        env = dict(self.env, MEMORY2_LLM_URL=url)
        try:
            self.transcript("s1.jsonl", self.recs())
            out = self.run_m("extract", "--llm", "--backfill", "1", env=env)
        finally:
            stop(srv)
        self.assertIn("auto 1", out)
        self.assertIn("llm_none 1", out)  # "I have a feature idea." is not durable
        auto = self.db("SELECT origin, kind, text, status, source FROM facts WHERE origin='auto'")
        self.assertEqual(auto[0][:4], ("auto", "decision", "He never wants pings about the gnu tracker.", "active"))
        self.assertIn("tg#11", auto[0][4])
        rv = self.run_m("review")
        self.assertIn("auto decision: He never wants pings", rv)
        self.assertIn("conflict", rv)  # wombat: public now vs behind basic auth
        self.assertIn("negation differs", rv)
        c = self.hook("any news about pings for the gnu tracker")[0].stdout
        self.assertIn("auto decision, unreviewed", c)


class Judge(Base):
    def test_ledger_reversal_numbers_and_duplicates(self):
        sys.path.insert(0, os.path.dirname(CORE))
        os.environ.update(MEMORY2_HOME=self.h, MEMORY2_STATE=self.state)
        import memory2 as m

        led = {
            "origin": "ledger",
            "verb": "declined",
            "thing": "quokka briefings",
            "text": "20.10 declined: quokka briefings",
            "source": "FACTS.md:15",
        }
        self.assertEqual(m.judge({"text": "Yes, send me the quokka briefings every morning"}, led, 0.6)[0], "conflict")
        self.assertIsNone(m.judge({"text": "No quokka briefings please"}, led, 0.6))
        tap = {"text": "Picked 'Skip' on: Want quokka briefings on Sundays?"}
        self.assertIsNone(m.judge(tap, led, 0.6))
        fact = {"origin": "facts", "text": "Next disk <= 16 TB (bigger needs bigger parity)", "source": "FACTS.md:5"}
        j = m.judge({"text": "The next disk can be 18 TB now (bigger parity)"}, fact, 0.7)
        self.assertEqual(j[0], "conflict")
        self.assertIn("tb differs", j[1])
        self.assertEqual(
            m.judge({"text": "Next disk <= 16 TB (bigger needs bigger parity)"}, fact, 0.95)[0], "duplicate"
        )


class Queue(Base):
    def setUp(self):
        super().setUp()
        srv, url = serve()
        self.addCleanup(srv.shutdown)
        p = os.path.join(self.h, PROJ, "s1.jsonl")
        with open(p, "w") as f:
            for r in (
                rec("owner", "From now on never send me pings about the gnu tracker.", "2026-10-21T08:01:00Z", 11),
                rec("owner", "I have a spare heron box in the cellar.", "2026-10-21T08:02:00Z", 12),
            ):
                f.write(json.dumps(r) + "\n")
        self.run_m("extract", "--llm", "--backfill", "1", env=dict(self.env, MEMORY2_LLM_URL=url))
        self.items = {r[1]: r[0] for r in self.db("SELECT id, kind FROM review WHERE status='open'")}

    def test_promote_section_and_ledger_keep_every_line(self):
        before = open(os.path.join(self.h, "workspace/FACTS.md")).read().split("\n")
        out = self.run_m("promote", self.items["pending"], "--section", "Server")
        self.assertIn("promoted to FACTS.md:7", out)
        after = open(os.path.join(self.h, "workspace/FACTS.md")).read().split("\n")
        self.assertEqual(len(after), len(before) + 1)
        self.assertRegex(after[6], r"^- .*spare heron box.* — owner \(tg#12\), 21\.10$")
        self.assertEqual([x for x in after if x != after[6]], before)
        self.run_m("promote", self.items["auto"], "--ledger", "decided: gnu tracker pings (never)")
        led = open(os.path.join(self.h, "workspace/FACTS.md")).read().split("\n")
        i = led.index("Newest first, one line each.")
        self.assertEqual(led[i + 1], "- 21.10 decided: gnu tracker pings (never)")
        self.assertEqual(self.db("SELECT COUNT(*) FROM review WHERE status='open'")[0][0], 0)
        self.assertEqual(self.db("SELECT status FROM facts WHERE origin='auto'"), [("promoted",), ("promoted",)])
        self.run_m("promote", self.items["auto"], "--section", "Nope", rc=1)

    def test_reject_and_digest_once_a_day(self):
        self.run_m("reject", self.items["auto"])
        self.assertEqual(self.db("SELECT status FROM facts WHERE origin='auto'"), [("rejected",)])
        self.run_m("digest", "--cron")
        notes = os.listdir(os.path.join(self.h, "inbox"))
        self.assertEqual(notes, ["memory2-review-2026-10-21.md"])
        body = open(os.path.join(self.h, "inbox", notes[0])).read()
        self.assertIn("spare heron box", body)
        os.remove(os.path.join(self.h, "inbox", notes[0]))
        self.run_m("digest", "--cron")
        self.assertEqual(os.listdir(os.path.join(self.h, "inbox")), [])
        self.assertIn("1 open", self.run_m("digest"))


class KBTest(Base):
    def test_ask_cites_facts_and_passages_and_eval(self):
        self.run_m("sync")
        self.w(
            "reports/heron-2026-10-12.md", "# Heron box\n\nThe heron box backup runs the gnu tracker export at night.\n"
        )
        out = self.run_m("ask", "when does the gnu tracker run on the heron box", binp=KB)
        lines = out.strip().split("\n")
        self.assertTrue(lines[1].startswith("F1 [fact]"), out)
        self.assertIn("(workspace/FACTS.md:6)", lines[1])
        self.assertTrue(any("reports/heron-2026-10-12.md:3" in x for x in lines), out)
        js = json.loads(self.run_m("ask", "heron box backup", "--json", binp=KB))
        self.assertIn("passages", js)
        ev = os.path.join(self.t, "eval.json")
        with open(ev, "w") as f:
            json.dump(
                {
                    "items": [
                        {"q": "gnu tracker nightly", "expect": ["workspace/FACTS.md"], "fact": "gnu tracker"},
                        {"q": "heron box backup export", "expect": ["reports/heron"]},
                    ]
                },
                f,
            )
        res = json.loads(self.run_m("eval", "--json", binp=KB, env=dict(self.env, KB_EVAL=ev)))
        self.assertEqual(res["kb"]["hit@3"], 1.0)  # settled facts come first, then passages
        self.assertEqual(res["facts"]["hit@1"], 1.0)
        self.assertIn("recall", res)
        self.run_m("ask", "pelican recipe xylophone", binp=KB, rc=1)


class HookCore(Base):
    """v2 H16: when CLAUDE.md imports FACTS-core.md the pack skips the lines the core holds, keeps ledger details and
    takes more, wider hits; with FACTS.md imported it behaves as before. Also: main session after a cd, qvec cache."""

    CORE_DOC = (
        "# Facts core\n\n## Server & hardware\n"
        "- Next disk <= 16 TB (bigger needs bigger parity) — live check, 19.10\n\n"
        "## Done / declined ledger\n- 20.10 declined: quokka briefings\n"
    )

    def setUp(self):
        super().setUp()
        self.run_m("sync")
        self.w("workspace/FACTS-core.md", self.CORE_DOC)

    def ctx(self, p):
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)["hookSpecificOutput"]["additionalContext"] if p.stdout else ""

    def last_log(self):
        with open(os.path.join(self.state, "hook.log")) as f:
            return f.read().strip().split("\n")[-1].split()

    def call(self, payload, env=None):
        p = subprocess.run(
            [sys.executable, "-I", "-S", CORE],
            input=json.dumps(payload),
            env=env or self.env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return p

    def test_full_mode_when_facts_md_is_imported(self):
        self.w("workspace/CLAUDE.md", "# rules\n@FACTS.md\n")
        c = self.ctx(self.hook("which next disk size fits, bigger parity?")[0])
        self.assertIn("settled facts that may bear", c)
        self.assertIn("Next disk <= 16 TB", c)
        self.assertEqual(self.last_log()[5], "full")

    def test_core_mode_skips_loaded_lines_and_keeps_ledger_details(self):
        self.w("workspace/CLAUDE.md", "# rules\n@FACTS-core.md\n")
        c = self.ctx(self.hook("quokka briefings again, next disk?")[0])
        self.assertIn("not in the loaded FACTS core", c)
        self.assertNotIn("Next disk <= 16 TB", c)  # the core holds it already
        self.assertIn("he reads the news himself", c)  # ledger details come with the pack
        last = self.last_log()
        self.assertEqual((last[4], last[5], int(last[6])), ("ok", "core", len(c)))

    def test_core_mode_override_cap_and_budget(self):
        extra = "".join(f"- Wombat feeder {i} runs at {i:02d}:00 on the heron box — live, 20.10\n" for i in range(20))
        self.w("workspace/FACTS.md", FACTS.replace("## Apps & services\n", "## Apps & services\n" + extra))
        self.run_m("sync")
        env = dict(self.env, MEMORY2_HOOK_MODE="core", MEMORY2_CORE_BUDGET="700")
        c = self.ctx(self.hook("wombat feeder heron box runs", env=env)[0])
        self.assertIn("not in the loaded FACTS core", c)
        self.assertLessEqual(len(c), 700)
        self.assertLessEqual(c.count("\n- ["), 8)
        c = self.ctx(self.hook("wombat feeder heron box runs", env=dict(env, MEMORY2_CORE_BUDGET="20000"))[0])
        self.assertTrue(4 < c.count("\n- [") <= 8, c)  # more than the full mode's 4, at most CORE_K

    def test_main_session_after_a_cd_via_the_transcript(self):
        conf = os.path.join(self.h, ".claude")
        ws = os.path.realpath(os.path.join(self.h, "workspace"))
        proj = os.path.join(conf, "projects", re.sub(r"[^A-Za-z0-9]", "-", ws))
        env = dict(self.env, CLAUDE_CONFIG_DIR=conf)
        base = {
            "prompt": "should I pitch quokka briefings again?",
            "cwd": self.t,
            "hook_event_name": "UserPromptSubmit",
        }
        self.assertIn("quokka", self.ctx(self.call(dict(base, transcript_path=proj + "/s1.jsonl"), env)))
        other = os.path.join(conf, "projects", "-somewhere-else", "s2.jsonl")
        self.assertEqual(self.call(dict(base, transcript_path=other), env).stdout, "")
        self.assertEqual(self.call(base, env).stdout, "")

    def test_qvec_cache_stands_in_for_the_embedder(self):
        q = "task: search result | query: should I pitch quokka briefings again?"
        cache = os.path.join(self.t, "qvec.json")
        with open(cache, "w") as f:
            json.dump({hashlib.sha256(q.encode()).hexdigest(): bow(q)}, f)
        env = dict(self.env, MEMORY2_EMBED_URL="http://127.0.0.1:1", MEMORY2_QVEC_CACHE=cache)
        self.assertIn("quokka", self.ctx(self.hook("should I pitch quokka briefings again?", env=env)[0]))
        self.assertEqual(self.last_log()[3], "1")  # semantic, from the cache
        self.hook("gnu tracker nightly heron please", env=env)
        self.assertEqual(self.last_log()[3], "0")  # not cached, embedder down: text only


if __name__ == "__main__":
    unittest.main(verbosity=1)
