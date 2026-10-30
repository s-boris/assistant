#!/usr/local/bin/python3.13 -I
"""agentfeed: what my subagents and the main session are doing, for the dashboard's Agents tab. Token-free, local.

  agentfeed list [--hours 6] [--json]       the Agents tab rows (running / stalled agents and those that ended in the
                                            window, newest first) plus the Main row; dash-data embeds it (5-min
                                            fallback), flowd streams it live at /live/agents
  agentfeed steps ID|main [--n 40] [--json] the filtered activity stream of one transcript (flowd: /live/agent?id=)
  agentfeed clean                           the privacy filter on stdin: prints what it keeps, exit 0 kept / 1 dropped

Sources: agent-watch status --json (which agents, their state: running / stalled / done / failed / killed / stopped /
interrupted / orphaned), the transcript itself (<project>/<session>/subagents/agent-<id>.jsonl: steps, tool-call count,
model; tokens and cost per message id at traces' list prices, lib/llm_prices.py) and `traces agents --json` (the trigger
of the turn that launched the agent). Main row: the newest <project>/<session>.jsonl, its tail only.
Card title: the title the agent was renamed to (`tg-status title ID "Short title"`, lib/agent_titles.py; agent-watch's
--json "title"), else its spawn description; through clean() like every text (a title it drops -> the description).

Privacy (the page sits behind the dashboard login; still nothing private goes on it):
  shown   time, tool NAME, the short description an agent wrote itself for a Bash / Monitor call or an Agent launch,
          ok / error, the agent's own short prose lines, counts (tool calls, tokens, cost), state, trigger kind.
  never   any other tool input (command lines, paths, URLs, queries, patterns, file contents), tool outputs, prompts,
          owner or channel messages (only their kind: "Telegram message", "heartbeat"), vault answers, mail / chat
          data, thinking, secrets, and the final result: only "finished" + its length.
  clean() is the ONE filter every text passes (descriptions, prose, titles): live secure-fill values out (securefill),
  his personal data out (ownermask: name, e-mail, phone, address, IBAN, card), secrets out (secretscan.redact), code
  blocks, tables and quotes dropped, lines that mention the vault, mail, chats, messages or quote someone dropped,
  paths, links, e-mails, IBANs, phone / long numbers, amounts, IPs and long ids masked, at most 3 lines of 180 chars,
  then secretscan.scan must find nothing. Fail closed: a missing redactor, a secure fill without its key or any error
  -> None (nothing is shown). Tests: tests/legacy/agentfeed/run.sh (~/opt/assistant).
Env (tests): AGENTFEED_PROJECTS, AGENTFEED_AGENT_WATCH, AGENTFEED_TRACES, AGENTFEED_SECRETSCAN_DIR, AGENTFEED_NOW.
"""

import glob
import json
import os
import re
import subprocess
import sys
import threading
import time

LIB = os.path.dirname(os.path.realpath(__file__))
if LIB not in sys.path:
    sys.path.insert(0, LIB)
HOME = os.path.expanduser("~")
PROJ = os.environ.get("AGENTFEED_PROJECTS") or os.path.join(HOME, ".claude/projects/-home-node-workspace")
AW_BIN = os.environ.get("AGENTFEED_AGENT_WATCH") or os.path.join(HOME, "bin/agent-watch")
TRACES_BIN = os.environ.get("AGENTFEED_TRACES") or os.path.join(HOME, "bin/traces")
SS_DIR = os.environ.get("AGENTFEED_SECRETSCAN_DIR") or os.path.dirname(LIB)  # the bin dir next to lib/
FINAL = {"end_turn", "stop_sequence", "refusal"}
ID_RE = re.compile(r"^a[0-9a-f]{6,24}$")
TYPE_RE = re.compile(rb'"type": ?"(?:assistant|user)"')
MAIN_TAIL = 600_000  # bytes of the main transcript read for its steps (it grows to hundreds of MB)
MAX_LINE, MAX_LINES = 180, 3

# ------------------------------------------------------------------------------------------------ the privacy filter
_M = {}


def _mods():
    """(secretscan, securefill, ownermask); raises when one is missing (clean() then drops everything)."""
    if not _M:
        if SS_DIR not in sys.path:
            sys.path.insert(0, SS_DIR)
        # as adopted: secretscan from SS_DIR first, then the lib/ helpers
        # isort: off
        import secretscan
        import securefill
        import ownermask
        # isort: on

        _M.update(ss=secretscan, sf=securefill, om=ownermask, t=time.time())
    elif time.time() - _M["t"] > 600:  # pick up new or changed secrets
        _M["ss"]._cache = None
        _M["t"] = time.time()
    return _M["ss"], _M["sf"], _M["om"]


FENCE_RE = re.compile(r"```.*?(?:```|\Z)|~~~.*?(?:~~~|\Z)", re.S)
# a line that mentions where private content comes from, or reports what someone wrote, is dropped whole
DROP_RE = re.compile(
    r"(?i)(?<![\w-])(?:vault|tgread|\w*mail\w*|inbox|paperless|firefly|chats?|whatsapp|threema|"
    r"signal|sms|dms?|messages?|nachrichte?n?|wrote|writes|written|said|says|saying|replied|quoted?|quoting|"
    r"schrieb|schreibt|sagt|sagte|meinte|password|passwort|passphrase|credentials?|iban|card number|pin)(?![\w-])"
)
QUOTE_RE = re.compile(r'["“„«‹][^"“”»›\n]{25,}["”“»›]')
CODE_RE = re.compile(r"`([^`\n]{1,200})`")
WORD_CODE_RE = re.compile(r"[A-Za-z][\w.:+-]{0,39}")
FIRST = (  # before ownermask: a link or address is masked whole, never word by word
    (re.compile(r"(?i)\b(?:https?|ftp|file|wss?|ssh)://\S+|\bwww\.[\w-]+\.\S+"), "[link]"),
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[email]"),
)
MASKS = (
    (re.compile(r'(?<![\w~.$/\\-])(?:~|\$HOME|\.\.?)?/[^\s`\'"<>()\[\]{},;]+'), "[path]"),
    (re.compile(r'(?<![\w/])[\w.@-]+/[\w.@-]+/[^\s`\'"<>()\[\]{},;]+'), "[path]"),
    (re.compile(r"(?<![A-Za-z0-9])[A-Z]{2}\d{2}(?: ?[A-Z0-9]){11,30}(?![A-Za-z0-9])"), "[iban]"),
    (re.compile(r"(?<![\w+])(?:\+|00)\d{1,3}[ ()./-]*\d(?:[ ()./-]*\d){6,}"), "[phone]"),
    (
        re.compile(
            r"(?i)(?:CHF|EUR|USD|GBP|SFr\.?|Fr\.|€|\$|£)\s?-?\d[\d\'’.,]*(?:\s?(?:k|m|mio|million|mrd)\b)?"
            r"|\b\d[\d\'’.,]*\s?(?:CHF|EUR|USD|GBP|Fr\.|€|francs?|franken|dollars?|euros?)(?![\w])"
        ),
        "[amount]",
    ),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "[ip]"),
    (re.compile(r"\d(?:[ .\'’]?\d){7,}|\d(?:[-/]?\d){9,}"), "[number]"),
    (
        re.compile(
            r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b|\b[0-9a-fA-F]{20,}\b"
            r"|\b[A-Za-z0-9_-]{28,}\b"
        ),
        "[id]",
    ),
)
MARK_RE = re.compile(r"\[(?:path|link|email|iban|phone|amount|ip|number|id|code|personal|securefill|redacted)\]")
WORDS_RE = re.compile(r"[^\W\d_]{2,}")


def _code(m):
    s = m.group(1).strip()
    return s if WORD_CODE_RE.fullmatch(s) and "/" not in s else "[code]"


def _cut(line):
    if len(line) <= MAX_LINE:
        return line
    cut = line[: MAX_LINE - 1]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > MAX_LINE // 2 else cut).rstrip(" ,;:-") + "…"


_LOCK = threading.Lock()


def clean(text, lines=MAX_LINES):
    """The privacy filter for every text on the Agents tab (module docstring). Returns the kept text (at most `lines`
    lines of MAX_LINE chars) or None. Fail closed: any problem -> None. Serialised (flowd calls it from many
    threads)."""
    with _LOCK:
        return _clean(text, lines)


def _clean(text, lines):
    try:
        if not isinstance(text, str) or not text.strip():
            return None
        ss, sf, om = _mods()
        raw = text[:6000].replace("\r", "")
        fps = sf.live()  # live secure-fill values: out first, on the raw text
        if fps:
            key = sf._fp_key()
            if not key:
                return None
            raw = sf.redact_text(raw, fps, key)[0]
        for rx, mark in FIRST:
            raw = rx.sub(mark, raw)
        raw = om.mask(raw)  # his name, e-mail, phone, address, IBAN, card
        raw = ss.redact(raw)  # secrets and token-shaped strings
        raw = FENCE_RE.sub("\n", raw)
        out = []
        for line in raw.split("\n"):
            s = line.strip()
            if not s or s.startswith((">", "|", "<")) or re.fullmatch(r"[-=*_#|: ]+", s):
                continue  # quotes, tables, markup, rules
            if DROP_RE.search(MARK_RE.sub(" ", s)) or QUOTE_RE.search(s):
                continue
            s = re.sub(r"^#+\s*", "", s).replace("**", "").replace("__", "")
            s = CODE_RE.sub(_code, s)
            for rx, mark in MASKS:
                s = rx.sub(mark, s)
            s = re.sub(r"\s+", " ", s).strip()
            if len(WORDS_RE.findall(MARK_RE.sub(" ", s))) < 2:
                continue  # nothing left but masks
            out.append(_cut(s))
            if len(out) >= lines:
                break
        res = "\n".join(out)
        if not res or ss.scan(res):
            return None
        return res
    except Exception:
        return None


# ------------------------------------------------------------------------------------------------ tool steps
GENERIC = {
    "Read": "read a file",
    "Write": "write a file",
    "Edit": "edit a file",
    "MultiEdit": "edit a file",
    "NotebookEdit": "edit a notebook",
    "Grep": "search files",
    "Glob": "find files",
    "WebSearch": "web search",
    "WebFetch": "fetch a web page",
    "ToolSearch": "load tool definitions",
    "TodoWrite": "update the task list",
    "SendMessage": "message another agent",
    "TaskStop": "stop a task",
    "TaskOutput": "read a task's output",
    "Skill": "use a skill",
    "Artifact": "publish a page",
    "ArtifactComments": "page comments",
    "ArtifactData": "page data",
    "Monitor": "watch a background job",
    "Agent": "launch an agent",
    "Task": "launch an agent",
    "Bash": "shell command",
    "EnterWorktree": "enter a worktree",
    "ExitWorktree": "leave a worktree",
}
DESC_TOOLS = {"Bash", "Monitor", "Agent", "Task"}  # their `description` is the agent's own short label


def tool_label(name, inp):
    """(short tool name, description) for a tool_use; never any other input field."""
    name = str(name or "tool")
    inp = inp if isinstance(inp, dict) else {}
    if name.startswith("mcp__"):
        parts = name.split("__")
        srv = re.sub(r"^(?:plugin_[^_]+_|claude_ai_)", "", parts[1] if len(parts) > 2 else "mcp").lower()
        tool = parts[-1]
        tool = tool[len(srv) + 1 :] if tool.lower().startswith(srv + "_") else tool
        short = f"{srv}.{tool}"
        desc = None
    else:
        short = name
        desc = None
        if name in DESC_TOOLS:
            desc = clean(str(inp.get("description") or ""), lines=1)
        if not desc:
            desc = GENERIC.get(name)
            if name == "Skill" and re.fullmatch(r"[a-z0-9][a-z0-9:_-]{0,39}", str(inp.get("skill") or "")):
                desc = "use skill " + inp["skill"]
        if name == "Bash" and inp.get("run_in_background"):
            desc = (desc or "shell command") + " (background)"
    short = re.sub(r"[^\w.:-]", "", short)[:48] or "tool"
    return short, desc


def user_kind(text):
    """What a user record in the main session is, as a label (never its text). None = skip."""
    t = (text or "").lstrip()[:400]
    if not t or t.startswith("<system-reminder>") or t.startswith("Caveat:"):
        return None
    if "<task-notification>" in t:
        return "agent update"
    if t.startswith("[Request interrupted"):
        return "interrupted"
    if '<channel source="telegram"' in t:
        return "Telegram message"
    if "<channel source=" in t:
        return "channel message"
    if t.startswith("[heartbeat]") or "[heartbeat]" in t[:80]:
        return "heartbeat"
    if t.startswith("[agenda]"):
        return "agenda"
    if "<command-name>" in t:
        return "command"
    return "prompt"


def ts(v):
    try:
        from datetime import datetime

        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


class Reader:
    """Incremental reader of ONE transcript -> filtered step events + counters. Keeps no raw text.
    Events: {s, t, k: start|in|tool|res|say|end, ...}: tool {n: name, d: description}, res {ref: s of the tool, ok},
    say {x: filtered prose}, in {x: kind label}, end {len: chars of the final answer (never its text)}."""

    def __init__(self, path, main=False, tail=None):
        self.path, self.main = path, main
        self.pos, self.buf, self.skip = 0, b"", False
        if tail:
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            self.pos = max(0, size - tail)
            self.skip = self.pos > 0
        self.seq = 0
        self.calls = {}  # tool_use_id -> seq (until its result)
        self.tools = self.errors = 0
        self.msgs = {}  # message id -> (cost, tokens)
        self.model = None
        self.first_t = self.last_t = None
        self.result_len = self.end_mid = None
        self.last = None  # the newest tool / say / in / end event (the "doing now" line)
        self.started = False

    def read(self, cap=64 << 20):
        try:
            size = os.path.getsize(self.path)
            if size < self.pos:
                self.__init__(self.path, self.main)
                size = os.path.getsize(self.path)
            if size == self.pos:
                return []
            with open(self.path, "rb") as fh:
                fh.seek(self.pos)
                data = fh.read(min(size - self.pos, cap))
        except OSError:
            return []
        self.pos += len(data)
        lines = (self.buf + data).split(b"\n")
        self.buf = lines.pop()
        if len(self.buf) > 64 << 20:
            self.buf = b""
        if self.skip and lines:
            lines, self.skip = lines[1:], False
        out = []
        for ln in lines:
            if not TYPE_RE.search(ln):
                continue
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if isinstance(r, dict) and r.get("type") in ("assistant", "user"):
                out += self.record(r)
        return out

    def ev(self, t, k, **kw):
        self.seq += 1
        e = dict(s=self.seq, t=round(t, 1) if t else None, k=k)
        e.update((a, b) for a, b in kw.items() if b is not None)
        if k != "res":
            self.last = e
        return e

    def record(self, r):
        t = ts(r.get("timestamp")) or time.time()
        if self.first_t is None:
            self.first_t = t
        self.last_t = t
        m = r.get("message") if isinstance(r.get("message"), dict) else {}
        c = m.get("content")
        out = []
        if r["type"] == "assistant":
            if not self.model and m.get("model") and m.get("model") != "<synthetic>":
                self.model = str(m["model"])[:40]
            if not self.main and m.get("id") and isinstance(m.get("usage"), dict):
                self.msgs[m["id"]] = _usage(m.get("model") or self.model, m["usage"])
            blocks = [b for b in (c if isinstance(c, list) else []) if isinstance(b, dict)]
            final = m.get("stop_reason") in FINAL and not any(b.get("type") == "tool_use" for b in blocks)
            for b in blocks:
                if b.get("type") == "tool_use":
                    self.tools += 1
                    n, d = tool_label(b.get("name"), b.get("input"))
                    e = self.ev(t, "tool", n=n, d=d)
                    if b.get("id"):
                        self.calls[b["id"]] = e["s"]
                    out.append(e)
                elif b.get("type") == "text":
                    txt = str(b.get("text") or "")
                    if final:  # the result: its length only; one 'end' per message
                        same = self.end_mid is not None and self.end_mid == m.get("id")
                        self.result_len = (self.result_len or 0) + len(txt) if same else len(txt)
                        self.end_mid = m.get("id")
                        out.append(self.ev(t, "end", len=self.result_len, upd=True if same else None))
                    else:
                        x = clean(txt)
                        if x:
                            out.append(self.ev(t, "say", x=x))
            return out
        # user
        if r.get("isMeta"):
            return out
        if isinstance(c, list):
            texts = []
            for b in c:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_result":
                    ref = self.calls.pop(b.get("tool_use_id"), None)
                    okv = b.get("is_error") is not True
                    if not okv:
                        self.errors += 1
                    if ref:
                        out.append(self.ev(t, "res", ref=ref, ok=okv))
                elif b.get("type") == "text":
                    texts.append(str(b.get("text") or ""))
            if texts:
                out += self.user_text("\n".join(texts), t)
        elif isinstance(c, str):
            out += self.user_text(c, t)
        return out

    def user_text(self, text, t):
        if not self.main:
            if not self.started:
                self.started = True
                return [self.ev(t, "start")]
            k = user_kind(text)
            return [self.ev(t, "in", x="message from the main session" if k == "prompt" else k)] if k else []
        k = user_kind(text)
        return [self.ev(t, "in", x=k)] if k else []

    def tokens(self):
        return sum(v[1] for v in self.msgs.values())

    def cost(self):
        return sum(v[0] for v in self.msgs.values())


def _usage(model, u):
    try:
        import llm_prices

        usd, t = llm_prices.usage_cost(model, u)
        return usd, sum(int(v or 0) for v in t.values())
    except Exception:
        return 0.0, 0


def merged(events, n=None):
    """Backlog form: results folded into their tool step (ok: true/false; missing = still running)."""
    by, out = {}, []
    for e in events:
        if e["k"] == "res":
            if e.get("ref") in by:
                by[e["ref"]]["ok"] = e["ok"]
            continue
        if e["k"] == "end" and e.get("upd") and out and out[-1]["k"] == "end":
            out[-1]["len"] = e.get("len")
            continue
        e = dict(e)
        by[e["s"]] = e
        out.append(e)
    return out[-n:] if n else out


def step_line(e):
    """The one-line "doing now" text of an event."""
    if not e:
        return None
    k = e.get("k")
    if k == "tool":
        return (e.get("n") or "tool") + (": " + e["d"] if e.get("d") else "")
    if k == "say":
        return e.get("x", "").split("\n")[0]
    if k == "end":
        return f"finished (result {int(e.get('len') or 0)} chars, not shown)"
    if k == "start":
        return "started"
    if k == "in":
        return e.get("x")
    return None


# ------------------------------------------------------------------------------------------------ the list
def now_ts():
    v = os.environ.get("AGENTFEED_NOW")
    try:
        return float(v) if v else time.time()
    except ValueError:
        return time.time()


def _json_of(argv, timeout=20):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def watch_rows(hours):
    d = _json_of([AW_BIN, "status", "--hours", str(hours), "--json"])
    if not isinstance(d, dict) or not isinstance(d.get("agents"), list):
        raise RuntimeError("agent-watch status --json failed")
    return [a for a in d["agents"] if isinstance(a, dict) and ID_RE.match(str(a.get("id") or ""))]


def trace_rows(hours, now):
    from datetime import datetime

    import assistant_profile

    since = datetime.fromtimestamp(now - hours * 3600, assistant_profile.tz()).strftime("%Y-%m-%d %H:%M")
    rows = _json_of([TRACES_BIN, "agents", "--since", since, "--json", "--top", "1000"], timeout=30)
    return {r["id"]: r for r in rows if isinstance(r, dict) and r.get("id")} if isinstance(rows, list) else {}


def agent_path(aid, session=None):
    if not ID_RE.match(str(aid or "")):
        return None
    if session and re.fullmatch(r"[\w-]{8,64}", session):
        p = os.path.join(PROJ, session, "subagents", f"agent-{aid}.jsonl")
        if os.path.exists(p):
            return p
    hits = glob.glob(os.path.join(PROJ, "*", "subagents", f"agent-{aid}.jsonl"))
    return max(hits, key=os.path.getmtime) if hits else None


def main_path():
    try:
        c = [(os.path.getmtime(p), p) for p in glob.glob(os.path.join(PROJ, "*.jsonl"))]
    except OSError:
        c = []
    return max(c)[1] if c else None


def short_model(m):
    m = str(m or "")
    x = re.match(r"claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?(?:-|$)", m)
    if x:
        return x.group(1).capitalize() + " " + x.group(2) + ("." + x.group(3) if x.group(3) else "")
    return re.sub(r"[^\w.: -]", "", m)[:24] or None


def trigger_label(t):
    if not t or not t.get("trigger"):
        return None
    a, b = str(t.get("trigger")), str(t.get("trigger_kind") or "")
    a = re.sub(r"[^\w -]", "", a)[:20]
    b = re.sub(r"[^\w -]", "", b.replace("_", " "))[:24]
    return a + (" · " + b if b and b != a else "")


def last_trigger(path, nbytes=8 << 20):
    """(kind label, epoch) of the newest user record that started something (a message, heartbeat, agent update...)
    in the last nbytes of a main transcript; kinds only. (None, None) when none is found."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            fh.seek(max(0, size - nbytes))
            data = fh.read()
    except OSError:
        return None, None
    for ln in reversed(data.split(b"\n")[1:]):
        if not re.search(rb'"type": ?"user"', ln) or b'"tool_result"' in ln[:4000]:
            continue
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if not isinstance(r, dict) or r.get("type") != "user" or r.get("isMeta"):
            continue
        c = (r.get("message") or {}).get("content") if isinstance(r.get("message"), dict) else None
        if isinstance(c, list):
            if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
                continue
            c = "\n".join(str(b.get("text") or "") for b in c if isinstance(b, dict) and b.get("type") == "text")
        k = user_kind(c) if isinstance(c, str) else None
        if k:
            return k, ts(r.get("timestamp"))
    return None, None


def main_row(readers=None, now=None):
    now = now or now_ts()
    p = main_path()
    if not p:
        return None
    rd = (readers or {}).get(p)
    if rd is None:
        rd = Reader(p, main=True, tail=MAIN_TAIL)
        rd.trig, rd.turn_tools = last_trigger(p), 0
        if readers is not None:
            for k in [k for k, v in readers.items() if v.main]:
                del readers[k]
            readers[p] = rd
    for e in rd.read():
        if e["k"] == "in":
            rd.trig, rd.turn_tools = (e.get("x"), e.get("t")), 0
        elif e["k"] == "tool" and (rd.trig[1] is None or (e.get("t") or 0) >= rd.trig[1]):
            rd.turn_tools += 1
    try:
        mt = os.path.getmtime(p)
    except OSError:
        mt = rd.last_t or now
    return {
        "id": "main",
        "title": "Main session",
        "status": "working" if now - mt < 120 else "idle",
        "last": round(mt, 1),
        "now": step_line(rd.last),
        "trigger": rd.trig[0],
        "turn_start": rd.trig[1],
        "turn_tools": rd.turn_tools,
        "model": short_model(rd.model),
    }


def snapshot(hours=6, readers=None, now=None, traces=None):
    """{'now', 'hours', 'main', 'agents': [...]} for the page. readers: a dict kept between calls (flowd) so every
    transcript is read once and then incrementally; None = one-shot (dash-data). traces: a cached trace_rows() dict."""
    now = now or now_ts()
    rows = watch_rows(hours)
    tr = traces if traces is not None else trace_rows(hours, now)
    agents = []
    for a in rows:
        st = str(a.get("status") or "unknown")
        last = float(a.get("last") or 0)
        if st not in ("running", "stalled") and now - last > hours * 3600:
            continue
        p = agent_path(a["id"], a.get("session"))
        if not p:
            continue
        rd = (readers or {}).get(p)
        if rd is None:
            rd = Reader(p)
            if readers is not None:
                readers[p] = rd
        rd.read()
        t = tr.get(a["id"]) or {}
        # a renamed agent (tg-status title ID "..."; agent-watch's "title") shows its title, else the spawn description;
        # both pass clean(), and a title the filter drops falls back to the description
        desc, own = str(a.get("description") or ""), str(a.get("title") or "")
        title = (clean(own, lines=1) if own and own != desc else None) or clean(desc, lines=1)
        start = rd.first_t or a.get("start")
        end = now if st in ("running", "stalled") else (rd.last_t or last)
        agents.append(
            {
                "id": a["id"],
                "title": (title or "agent " + a["id"][:8])[:90],
                "status": st,
                "start": round(start, 1) if start else None,
                "last": round(rd.last_t or last, 1),
                "runtime_s": int(max(0, end - start)) if start else None,
                "model": short_model(rd.model or t.get("model")),
                "effort": re.sub(r"[^\w]", "", str(a.get("effort") or t.get("effort") or ""))[:8] or None,
                "type": re.sub(r"[^\w-]", "", str(a.get("type") or t.get("agent_type") or ""))[:24] or None,
                "depth": a.get("depth") if isinstance(a.get("depth"), int) else None,
                "parent": a.get("parent") if ID_RE.match(str(a.get("parent") or "")) else None,
                "tools": rd.tools,
                "errors": rd.errors,
                "tokens": rd.tokens(),
                "cost": round(rd.cost(), 4),
                "trigger": trigger_label(t),
                "now": step_line(rd.last),
                "result_len": rd.result_len,
            }
        )
    order = {"stalled": 0, "running": 0}
    agents.sort(key=lambda x: (order.get(x["status"], 1), -(x["start"] or 0)))
    if readers is not None:
        live = {agent_path(a["id"]) for a in agents}
        for k in [k for k, v in readers.items() if not v.main and k not in live]:
            del readers[k]
    return {"now": round(now, 1), "hours": hours, "main": main_row(readers, now), "agents": agents}


def steps(aid, n=200):
    """(reader, backlog events) for one agent id or 'main'."""
    if aid == "main":
        p = main_path()
        rd = Reader(p, main=True, tail=MAIN_TAIL) if p else None
    else:
        p = agent_path(aid)
        rd = Reader(p) if p else None
    if rd is None:
        return None, []
    return rd, merged(rd.read(), n)


# ------------------------------------------------------------------------------------------------ CLI
def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0
    cmd, args = argv[0], argv[1:]
    js = "--json" in args
    if cmd == "clean":
        res = clean(sys.stdin.read())
        if res:
            print(res)
        return 0 if res else 1
    if cmd == "list":
        hours = float(args[args.index("--hours") + 1]) if "--hours" in args else 6
        snap = snapshot(hours)
        if js:
            print(json.dumps(snap, ensure_ascii=False, indent=1))
            return 0
        m = snap["main"]
        if m:
            print(f"main      {m['status']:<8} {m['now'] or '-'}")
        for a in snap["agents"]:
            print(
                f"{a['id'][:9]} {a['status']:<8} {a['tools']:>4} tools {a['cost']:>7.2f} $  {a['title'][:50]}"
                f"  | {a['now'] or '-'}"
            )
        return 0
    if cmd == "steps" and args:
        n = int(args[args.index("--n") + 1]) if "--n" in args else 40
        rd, evs = steps(args[0], n)
        if rd is None:
            print("no such agent", file=sys.stderr)
            return 1
        if js:
            print(json.dumps(evs, ensure_ascii=False, indent=1))
        else:
            for e in evs:
                ok = {True: "ok", False: "ERR", None: ".."}[e.get("ok")] if e["k"] == "tool" else "  "
                print(time.strftime("%H:%M:%S", time.localtime(e["t"] or 0)), ok, e["k"].ljust(5), step_line(e))
        return 0
    print(__doc__.strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
