"""memory2 (the fact store: sync, search, extract, review queue, promote), kb (cited answers over it) and the
memory-recall hook core (memory2_core.hook_main).

Isolated: HOME is tmp_path/home and every memory2/recall/kb path points under it (MEMORY2_HOME/_STATE/_CONF,
RECALL_HOME, SECRETSCAN_SOURCES); the default store under that HOME must stay absent (a code path that ignored
MEMORY2_STATE would create it). The repo's memory2, kb and recall are staged in a bin dir whose lib/ holds the repo's
memory2, memory2_core, assistant_profile and core (as deployed; nothing from the live ~/bin), beside secretscan.py.
A fake embedding server (hashed bag of words) and a fake OpenAI-style LLM run on loopback threads.

No wall-clock bounds: under CI load (the suites run niced, side by side) a fresh interpreter's start alone passed the
old 400/700 ms marks. The hook's budget is checked by what it did: with an embedder that never answers it comes
back with text hits and sem 0, and the embed timeout it passes stays inside its budget. Synthetic fixtures only."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PROJ = ".claude/projects/-home-node-workspace"
CLEAR = ("MEMORY2_", "RECALL_", "KB_", "SECRETSCAN_", "USAGE", "CLAUDE_CONFIG_DIR", "ASSISTANT_PROFILE")

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
MEMORY = (
    '---\nname: margay-colours\ndescription: "Owner likes the margay colour scheme on cards"\n---\n\n'
    "Use margay colours on every card.\n"
)
CORE_DOC = (
    "# Facts core\n\n## Server & hardware\n"
    "- Next disk <= 16 TB (bigger needs bigger parity) — live check, 19.10\n\n"
    "## Done / declined ledger\n- 20.10 declined: quokka briefings\n"
)
QUOKKA = (
    '<channel source="telegram" chat_id="1" message_id="5" user="o" ts="x">should I pitch quokka briefings again?'
    "</channel>"
)


# ------------------------------------------------------------------ fakes


def bow(text: str) -> list[float]:
    text = re.sub(r"^(title: .*? \| text: |task: search result \| query: )", "", text)
    v = [0.0] * 64
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        if len(w) < 3:
            continue
        v[int(hashlib.md5(w[:5].encode()).hexdigest(), 16) % 64] += 1.0
    n = sum(x * x for x in v) ** 0.5 or 1.0
    return [x / n for x in v]


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        b = json.dumps(obj).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        except OSError:  # the client gave up (the hook's budget): nothing to answer
            pass

    def _hold(self):
        if self.server.hang is not None:
            self.server.hang.wait(60)  # never answers in time: released when the test ends

    def do_GET(self):
        self._hold()
        self._send({"status": "ok", "model_path": "/x/fake.gguf"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        self._hold()
        if self.path.endswith("/embeddings"):
            self._send({"data": [{"index": i, "embedding": bow(t)} for i, t in enumerate(body["input"])]})
            return
        sys_msg = body["messages"][0]["content"]
        user = body["messages"][-1]["content"]
        if sys_msg.startswith("You turn one message"):
            owner = user.split("Owner:", 1)[-1]
            if "never" in owner.lower():
                items = [
                    {
                        "kind": "decision",
                        "subject": "gnu pings",
                        "confidence": 0.95,
                        "text": "He never wants pings about the gnu tracker.",
                    }
                ]
            elif "feature idea" in owner:
                items = []
            else:
                items = [{"kind": "fact", "subject": "misc", "text": owner.strip()[:80], "confidence": 0.6}]
            out = {"items": items}
        else:
            out = {"relation": "consistent", "confidence": 0.5}
        self._send({"choices": [{"message": {"content": json.dumps(out)}}]})


class Server:
    def __init__(self, hang: bool = False):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        self.srv.hang = threading.Event() if hang else None
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def stop(self):
        if self.srv.hang is not None:
            self.srv.hang.set()
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture(scope="module")
def server():
    s = Server()
    yield s
    s.stop()


def rec(kind, text, ts, mid=None):
    if kind == "owner":
        c = f'<channel source="telegram" chat_id="1" message_id="{mid}" user="o" ts="{ts}">{text}</channel>'
        return {"type": "user", "timestamp": ts, "message": {"content": c}}
    return {"type": "assistant", "timestamp": ts, "message": {"content": [{"type": "text", "text": text}]}}


# ------------------------------------------------------------------ the staged tools and their sandbox


def _launch(path: Path) -> list[str]:
    """A script by its shebang; the CI runner has no /usr/bin/python3 (kb's, recall's): this interpreter then."""
    first = path.read_text(encoding="utf-8").split("\n", 1)[0]
    parts = first[2:].split() if first.startswith("#!") else []
    if parts and parts[0].startswith("/") and "python" in parts[0] and not os.path.exists(parts[0]):
        return [sys.executable, *parts[1:], str(path)]
    return [str(path)]


def stage(d: Path) -> Path:
    """bin/{memory2,kb,recall,secretscan.py} + bin/lib/{memory2,memory2_core,assistant_profile,core} from the repo."""
    b = d / "bin"
    (b / "lib").mkdir(parents=True)
    for n in ("memory2", "kb", "recall"):
        shutil.copy2(REPO / "scripts" / n, b / n)
    shutil.copy2(REPO / "scripts" / "secretscan_py", b / "secretscan.py")
    (b / "lib" / "core").symlink_to(REPO / "core")
    for n in ("memory2.py", "memory2_core.py", "assistant_profile.py"):
        (b / "lib" / n).symlink_to(REPO / "scripts" / "lib" / "legacy" / n)
    return b


class M2:
    def __init__(self, root: Path, url: str):
        self.t = root
        self.bin = stage(root)
        self.core = self.bin / "lib" / "memory2_core.py"
        self.h = root / "home"
        for d in ("workspace", PROJ + "/memory", "inbox", "reports"):
            (self.h / d).mkdir(parents=True)
        self.w("workspace/FACTS.md", FACTS)
        self.w(PROJ + "/memory/margay-colours.md", MEMORY)
        self.w(PROJ + "/memory/MEMORY.md", "- [Margay](margay-colours.md) — index line\n")
        self.state = root / "state"
        env = {k: v for k, v in os.environ.items() if not k.startswith(CLEAR)}
        env.update(
            HOME=str(self.h),
            MEMORY2_HOME=str(self.h),
            MEMORY2_STATE=str(self.state),
            MEMORY2_CONF=str(root / "conf"),
            MEMORY2_RECALL=str(self.bin / "recall"),
            MEMORY2_EMBED_URL=url,
            MEMORY2_LLM="off",
            USAGE="off",
            SECRETSCAN_SOURCES=str(root),
            MEMORY2_HOOK_CWD=str(self.h / "workspace"),
            RECALL_HOME=str(self.h),
            RECALL_EMBED="off",
            MEMORY2_NOW=str(time.mktime((2026, 10, 21, 12, 0, 0, 0, 0, -1))),
        )
        self.env = env

    def w(self, rel: str, text: str) -> Path:
        p = self.h / rel
        p.write_text(text, encoding="utf-8")
        return p

    def read(self, rel: str) -> str:
        return (self.h / rel).read_text(encoding="utf-8")

    def run(self, *args, env=None, rc=0, prog="memory2") -> str:
        p = subprocess.run(
            [*_launch(self.bin / prog), *args],
            env=env or self.env,
            capture_output=True,
            text=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
        )
        if rc is not None:
            assert p.returncode == rc, p.stdout + p.stderr
        return p.stdout

    def kb(self, *args, **kw) -> str:
        return self.run(*args, prog="kb", **kw)

    def db(self, sql: str, *a) -> list:
        with sqlite3.connect(self.state / "mem.db") as c:
            return c.execute(sql, a).fetchall()

    def hook_raw(self, payload: dict | str, env=None) -> subprocess.CompletedProcess:
        data = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run(
            [sys.executable, "-I", "-S", str(self.core)],
            input=data,
            env=env or self.env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def hook(self, prompt: str, cwd=None, env=None) -> subprocess.CompletedProcess:
        payload = {"prompt": prompt, "cwd": str(cwd or self.h / "workspace"), "hook_event_name": "UserPromptSubmit"}
        return self.hook_raw(payload, env)

    def ctx(self, p: subprocess.CompletedProcess) -> str:
        assert p.returncode == 0, p.stderr
        return json.loads(p.stdout)["hookSpecificOutput"]["additionalContext"] if p.stdout else ""

    def last_log(self) -> list[str]:
        """hook.log's last line: ts ms hits sem why mode chars."""
        return (self.state / "hook.log").read_text().strip().split("\n")[-1].split()

    def transcript(self, name: str, recs: list) -> Path:
        p = self.h / PROJ / name
        with p.open("a") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")
        return p


@pytest.fixture
def m2(tmp_path: Path, server: Server):
    m = M2(tmp_path, server.url)
    yield m
    # run.sh's old guard: nothing wrote the default store (a path that ignores MEMORY2_STATE)
    assert not (m.h / ".local" / "state" / "memory2").exists()


@pytest.fixture
def synced(m2: M2) -> M2:
    m2.run("sync")
    return m2


# ------------------------------------------------------------------ sync and search


def test_sync_parses_facts_ledger_memory(m2):
    assert "6 active facts" in m2.run("sync")
    led = m2.db("SELECT verb, thing, date, source FROM facts WHERE origin='ledger' ORDER BY line")
    assert led[0] == ("declined", "quokka briefings", "2026-10-20", "FACTS.md:13")
    assert led[1][:2] == ("done", "Disk 12 added")
    assert m2.db("SELECT subject, date, source FROM facts WHERE text LIKE 'Next disk%'")[0] == (
        "Server & hardware",
        "2026-10-19",
        "FACTS.md:5",
    )
    assert m2.db("SELECT subject, source FROM facts WHERE origin='memory'") == [
        ("margay-colours", "memory/margay-colours.md:5")
    ]
    assert m2.db("SELECT COUNT(*) FROM facts WHERE vec IS NOT NULL")[0][0] == 6
    assert oct((m2.state / "mem.db").stat().st_mode & 0o777) == "0o600"


def test_removed_line_becomes_gone_never_deleted(synced):
    synced.w(
        "workspace/FACTS.md", FACTS.replace("- Wombat dashboard sits behind basic auth — dashboard fix, 20.10\n", "")
    )
    assert "1 gone" in synced.run("sync")
    assert synced.db("SELECT status FROM facts WHERE text LIKE 'Wombat%'") == [("gone",)]
    assert synced.run("search", "wombat dashboard auth", rc=1) == ""


def test_search_hybrid_and_floor(synced):
    out = synced.run("search", "which tracker runs at night on the heron box")
    assert "FACTS.md:6" in out.split("\n")[0]
    synced.run("search", "pelican recipe for dinner", rc=1)
    lex = dict(synced.env, MEMORY2_EMBED_URL="http://127.0.0.1:1")  # embedder down: text hits only
    assert "FACTS.md:6" in synced.run("search", "gnu tracker nightly", env=lex)


# ------------------------------------------------------------------ the hook


def test_hook_injects_relevant_facts(synced):
    c = synced.ctx(synced.hook(QUOKKA))
    assert "quokka briefings" in c
    assert "[ledger 20.10 declined]" in c
    assert "(FACTS.md:13)" in c
    assert len(c) < 1000
    last = synced.last_log()
    assert (last[2], last[4]) == ("1", "ok")


def test_hook_skips_other_cwd_heartbeats_taps_and_short(synced):
    assert synced.hook("quokka briefings again", cwd=synced.t).stdout == ""
    assert synced.hook("[heartbeat] quokka briefings and the gnu tracker").stdout == ""
    tap = (
        '<channel source="telegram" chat_id="1" message_id="5" user="o" ts="x" event_type="button" '
        'button_text="Yes">quokka briefings</channel>'
    )
    assert synced.hook(tap).stdout == ""
    assert synced.hook("ok thanks").stdout == ""


def test_hook_kill_switches(synced):
    assert synced.hook("quokka briefings again please").stdout
    assert "off" in synced.run("hook", "off")
    assert synced.hook("quokka briefings again please").stdout == ""
    synced.run("hook", "on")
    assert synced.hook("quokka briefings again", env=dict(synced.env, MEMORY2_HOOK="off")).stdout == ""


def test_hook_gives_up_on_a_hanging_embedder(synced):
    """The budget, by behaviour: an embedder that never answers still gets text hits back, unsemantic (had the hook
    waited for it, it would hang until the test's 30 s timeout)."""
    hang = Server(hang=True)
    try:
        p = synced.hook("gnu tracker nightly heron", env=dict(synced.env, MEMORY2_EMBED_URL=hang.url))
    finally:
        hang.stop()
    assert "gnu tracker" in synced.ctx(p)
    last = synced.last_log()
    assert (last[3], last[4]) == ("0", "ok")


def test_hook_embed_timeout_stays_inside_the_budget(synced):
    """hook_main hands embed() at most 0.25 s and never more than what is left of its budget (or skips it)."""
    code = (
        "import io, json, sys\n"
        f"sys.path.insert(0, {str(synced.core.parent)!r})\n"
        "import memory2_core as c\n"
        "seen = []\n"
        "def fake(texts, timeout=10.0, url=None):\n"
        "    seen.append(timeout)\n"
        "    raise OSError('down')\n"
        "c.embed = fake\n"
        "out = io.StringIO()\n"
        "c.hook_main(stdin=io.StringIO(sys.argv[1]), stdout=out, budget_s=float(sys.argv[2]))\n"
        "print(json.dumps({'seen': seen, 'out': out.getvalue()}))\n"
    )
    payload = json.dumps({"prompt": "gnu tracker nightly heron", "cwd": str(synced.h / "workspace")})
    for budget in (0.30, 5.0, 0.0):
        p = subprocess.run(
            [sys.executable, "-I", "-S", "-c", code, payload, str(budget)],
            env=synced.env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert p.returncode == 0, p.stderr
        r = json.loads(p.stdout)
        assert all(0 < t <= min(0.25, budget) for t in r["seen"]), r
        assert "gnu tracker" in r["out"]  # text hits either way
        if budget == 0.0:
            assert r["seen"] == []  # nothing left: no embed call at all


def test_hook_fails_open(synced):
    (synced.state / "mem.db").write_bytes(b"not a database")
    p = synced.hook("gnu tracker nightly heron")
    assert (p.returncode, p.stdout) == (0, "")
    assert "err:" in (synced.state / "hook.log").read_text()
    assert synced.hook_raw("{not json").returncode == 0


# ------------------------------------------------------------------ extract and the review queue


def recs():
    return [
        rec("me", "Should I keep sending you pings about the gnu tracker?", "2026-10-21T08:00:00Z"),
        rec("owner", "From now on never send me pings about the gnu tracker.", "2026-10-21T08:01:00Z", 11),
        rec("owner", "can you check the heron box?", "2026-10-21T08:02:00Z", 12),
        rec("owner", "remind me at 5 to water the plants", "2026-10-21T08:03:00Z", 13),
        rec("owner", "I have a feature idea.", "2026-10-21T08:04:00Z", 14),
        rec("owner", "My therapist says I should sleep more, I have a session on Monday.", "2026-10-21T08:05:00Z", 15),
        rec(
            "owner",
            '<channel source="telegram" chat_id="1" message_id="16" user="o" ts="x" event_type="button" '
            'button_text="✅ Apply all 3" message_text="Sort 3 docs?"></channel>',
            "2026-10-21T08:06:00Z",
            16,
        ),
        rec("owner", "Actually the wombat dashboard is public now, no basic auth.", "2026-10-21T08:07:00Z", 17),
    ]


def test_extract_first_run_starts_at_the_end_then_incremental(m2):
    m2.transcript("s1.jsonl", recs())
    assert "turns 0" in m2.run("extract")
    m2.transcript(
        "s1.jsonl", [rec("owner", "I decided to keep the heron box on power save.", "2026-10-21T09:00:00Z", 20)]
    )
    out = m2.run("extract")
    assert "turns 1" in out
    assert "cands 1" in out
    assert "auto 0" in out  # no local model: never auto-accepted
    assert oct((m2.state / "candidates.jsonl").stat().st_mode & 0o777) == "0o600"


def test_extract_backfill_classifies_filters_and_dedupes(m2):
    m2.transcript("s1.jsonl", recs())
    m2.transcript("s2.jsonl", recs()[:2])  # a resumed session repeats turns
    lines = m2.run("extract", "--dry-run", "--backfill", "1").strip().split("\n")
    assert json.loads(lines[-1])["dropped_sensitive"] == 1
    texts = "\n".join(lines[:-1])
    assert texts.count("never send me pings") == 1
    assert "check the heron" not in texts  # question
    assert "water the plants" not in texts  # command
    assert "Apply all" not in texts  # one-off tap
    assert "therapist" not in texts  # sensitive
    assert re.search(r"decision +0\.8\d .*never send me pings", texts)
    assert re.search(r"correction .*wombat dashboard", texts)
    assert not (m2.state / "candidates.jsonl").exists()  # a dry run writes nothing


def test_extract_llm_auto_accept_conflict_and_review(m2, server):
    m2.transcript("s1.jsonl", recs())
    out = m2.run("extract", "--llm", "--backfill", "1", env=dict(m2.env, MEMORY2_LLM_URL=server.url))
    assert "auto 1" in out
    assert "llm_none 1" in out  # "I have a feature idea." is not durable
    auto = m2.db("SELECT origin, kind, text, status, source FROM facts WHERE origin='auto'")
    assert auto[0][:4] == ("auto", "decision", "He never wants pings about the gnu tracker.", "active")
    assert "tg#11" in auto[0][4]
    rv = m2.run("review")
    assert "auto decision: He never wants pings" in rv
    assert "conflict" in rv  # wombat: public now vs behind basic auth
    assert "negation differs" in rv
    assert "auto decision, unreviewed" in m2.hook("any news about pings for the gnu tracker").stdout


def test_judge_ledger_reversal_numbers_and_duplicates(m2):
    code = (
        "import json, sys\n"
        "import memory2 as m\n"
        "led = {'origin': 'ledger', 'verb': 'declined', 'thing': 'quokka briefings',\n"
        "       'text': '20.10 declined: quokka briefings', 'source': 'FACTS.md:15'}\n"
        "fact = {'origin': 'facts', 'text': 'Next disk <= 16 TB (bigger needs bigger parity)',\n"
        "        'source': 'FACTS.md:5'}\n"
        "r = [m.judge({'text': 'Yes, send me the quokka briefings every morning'}, led, 0.6),\n"
        "     m.judge({'text': 'No quokka briefings please'}, led, 0.6),\n"
        "     m.judge({'text': \"Picked 'Skip' on: Want quokka briefings on Sundays?\"}, led, 0.6),\n"
        "     m.judge({'text': 'The next disk can be 18 TB now (bigger parity)'}, fact, 0.7),\n"
        "     m.judge({'text': 'Next disk <= 16 TB (bigger needs bigger parity)'}, fact, 0.95)]\n"
        "print(json.dumps(r))\n"
    )
    p = subprocess.run(
        [sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(m2.core.parent)!r})\n" + code],
        env=m2.env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert p.returncode == 0, p.stderr
    reversal, no, tap, tb, dup = json.loads(p.stdout)
    assert reversal[0] == "conflict"
    assert no is None
    assert tap is None
    assert tb[0] == "conflict"
    assert "tb differs" in tb[1]
    assert dup[0] == "duplicate"


@pytest.fixture
def queue(m2: M2, server: Server) -> tuple[M2, dict]:
    m2.transcript(
        "s1.jsonl",
        [
            rec("owner", "From now on never send me pings about the gnu tracker.", "2026-10-21T08:01:00Z", 11),
            rec("owner", "I have a spare heron box in the cellar.", "2026-10-21T08:02:00Z", 12),
        ],
    )
    m2.run("extract", "--llm", "--backfill", "1", env=dict(m2.env, MEMORY2_LLM_URL=server.url))
    return m2, {r[1]: r[0] for r in m2.db("SELECT id, kind FROM review WHERE status='open'")}


def test_promote_section_and_ledger_keep_every_line(queue):
    m2, items = queue
    before = m2.read("workspace/FACTS.md").split("\n")
    assert "promoted to FACTS.md:7" in m2.run("promote", items["pending"], "--section", "Server")
    after = m2.read("workspace/FACTS.md").split("\n")
    assert len(after) == len(before) + 1
    assert re.match(r"^- .*spare heron box.* — owner \(tg#12\), 21\.10$", after[6])
    assert [x for x in after if x != after[6]] == before
    m2.run("promote", items["auto"], "--ledger", "decided: gnu tracker pings (never)")
    led = m2.read("workspace/FACTS.md").split("\n")
    assert led[led.index("Newest first, one line each.") + 1] == "- 21.10 decided: gnu tracker pings (never)"
    assert m2.db("SELECT COUNT(*) FROM review WHERE status='open'")[0][0] == 0
    assert m2.db("SELECT status FROM facts WHERE origin='auto'") == [("promoted",), ("promoted",)]
    m2.run("promote", items["auto"], "--section", "Nope", rc=1)


def test_reject_and_digest_once_a_day(queue):
    m2, items = queue
    m2.run("reject", items["auto"])
    assert m2.db("SELECT status FROM facts WHERE origin='auto'") == [("rejected",)]
    m2.run("digest", "--cron")
    inbox = m2.h / "inbox"
    assert os.listdir(inbox) == ["memory2-review-2026-10-21.md"]
    assert "spare heron box" in (inbox / "memory2-review-2026-10-21.md").read_text()
    (inbox / "memory2-review-2026-10-21.md").unlink()
    m2.run("digest", "--cron")
    assert os.listdir(inbox) == []
    assert "1 open" in m2.run("digest")


# ------------------------------------------------------------------ kb


def test_kb_ask_cites_facts_and_passages_and_eval(synced):
    synced.w(
        "reports/heron-2026-10-12.md", "# Heron box\n\nThe heron box backup runs the gnu tracker export at night.\n"
    )
    out = synced.kb("ask", "when does the gnu tracker run on the heron box")
    lines = out.strip().split("\n")
    assert lines[1].startswith("F1 [fact]"), out
    assert "(workspace/FACTS.md:6)" in lines[1]
    assert any("reports/heron-2026-10-12.md:3" in x for x in lines), out
    assert "passages" in json.loads(synced.kb("ask", "heron box backup", "--json"))
    ev = synced.t / "eval.json"
    ev.write_text(
        json.dumps(
            {
                "items": [
                    {"q": "gnu tracker nightly", "expect": ["workspace/FACTS.md"], "fact": "gnu tracker"},
                    {"q": "heron box backup export", "expect": ["reports/heron"]},
                ]
            }
        )
    )
    res = json.loads(synced.kb("eval", "--json", env=dict(synced.env, KB_EVAL=str(ev))))
    assert res["kb"]["hit@3"] == 1.0  # settled facts come first, then passages
    assert res["facts"]["hit@1"] == 1.0
    assert "recall" in res
    synced.kb("ask", "pelican recipe xylophone", rc=1)


# ------------------------------------------------------------------ core mode (FACTS-core.md imported)


@pytest.fixture
def cored(synced: M2) -> M2:
    synced.w("workspace/FACTS-core.md", CORE_DOC)
    return synced


def test_full_mode_when_facts_md_is_imported(cored):
    cored.w("workspace/CLAUDE.md", "# rules\n@FACTS.md\n")
    c = cored.ctx(cored.hook("which next disk size fits, bigger parity?"))
    assert "settled facts that may bear" in c
    assert "Next disk <= 16 TB" in c
    assert cored.last_log()[5] == "full"


def test_core_mode_skips_loaded_lines_and_keeps_ledger_details(cored):
    cored.w("workspace/CLAUDE.md", "# rules\n@FACTS-core.md\n")
    c = cored.ctx(cored.hook("quokka briefings again, next disk?"))
    assert "not in the loaded FACTS core" in c
    assert "Next disk <= 16 TB" not in c  # the core holds it already
    assert "he reads the news himself" in c  # ledger details come with the pack
    last = cored.last_log()
    assert (last[4], last[5], int(last[6])) == ("ok", "core", len(c))


def test_core_mode_override_cap_and_budget(cored):
    extra = "".join(f"- Wombat feeder {i} runs at {i:02d}:00 on the heron box — live, 20.10\n" for i in range(20))
    cored.w("workspace/FACTS.md", FACTS.replace("## Apps & services\n", "## Apps & services\n" + extra))
    cored.run("sync")
    env = dict(cored.env, MEMORY2_HOOK_MODE="core", MEMORY2_CORE_BUDGET="700")
    c = cored.ctx(cored.hook("wombat feeder heron box runs", env=env))
    assert "not in the loaded FACTS core" in c
    assert len(c) <= 700
    assert c.count("\n- [") <= 8
    c = cored.ctx(cored.hook("wombat feeder heron box runs", env=dict(env, MEMORY2_CORE_BUDGET="20000")))
    assert 4 < c.count("\n- [") <= 8, c  # more than the full mode's 4, at most CORE_K


def test_main_session_after_a_cd_via_the_transcript(cored):
    conf = cored.h / ".claude"
    ws = os.path.realpath(cored.h / "workspace")
    proj = conf / "projects" / re.sub(r"[^A-Za-z0-9]", "-", ws)
    env = dict(cored.env, CLAUDE_CONFIG_DIR=str(conf))
    base = {
        "prompt": "should I pitch quokka briefings again?",
        "cwd": str(cored.t),
        "hook_event_name": "UserPromptSubmit",
    }
    assert "quokka" in cored.ctx(cored.hook_raw(dict(base, transcript_path=str(proj / "s1.jsonl")), env))
    other = conf / "projects" / "-somewhere-else" / "s2.jsonl"
    assert cored.hook_raw(dict(base, transcript_path=str(other)), env).stdout == ""
    assert cored.hook_raw(base, env).stdout == ""


def test_qvec_cache_stands_in_for_the_embedder(cored):
    q = "task: search result | query: should I pitch quokka briefings again?"
    cache = cored.t / "qvec.json"
    cache.write_text(json.dumps({hashlib.sha256(q.encode()).hexdigest(): bow(q)}))
    env = dict(cored.env, MEMORY2_EMBED_URL="http://127.0.0.1:1", MEMORY2_QVEC_CACHE=str(cache))
    assert "quokka" in cored.ctx(cored.hook("should I pitch quokka briefings again?", env=env))
    assert cored.last_log()[3] == "1"  # semantic, from the cache
    cored.hook("gnu tracker nightly heron please", env=env)
    assert cored.last_log()[3] == "0"  # not cached, embedder down: text only
