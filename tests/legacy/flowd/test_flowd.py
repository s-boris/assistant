#!/usr/local/bin/python3.13 -I
"""flowd tests: edge mapping, vault content shown as my session saw it (nothing from inside the vault), secret redaction,
inbox policy, fail-closed without secretscan, SSE format + resume + /live/more + health, and the kill switch.

Run: ~/opt/flowd-tests/run.sh   (FLOWD_BIN=/path/to/copy tests another copy; default ~/bin/flowd)
Everything runs in a temp dir with fixture secrets (SECRETSCAN_SOURCES) and free local ports; nothing touches the
real transcripts, inbox, tunnel or nginx (FLOWD_VM points at 127.0.0.1, where nothing listens on 22)."""  # noqa: E501

import importlib.machinery
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse

BIN = os.environ.get("FLOWD_BIN", os.path.expanduser("~/bin/flowd"))
T = tempfile.mkdtemp(prefix="flowd-test-")
FAILS, COUNT = [], [0]


def ok(cond, name):
    COUNT[0] += 1
    if not cond:
        FAILS.append(name)
        print("FAIL", name)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ---------------------------------------------------------------- fixtures
FAKE = "FlowdFixtureValue-" + "9f8e7d6c5b4a3928"  # a "secret" only inside the fixture sources
PEM = (
    "-----BEGIN "
    + "OPENSSH PRIVATE KEY-----\n"
    + "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW\n"
    + "-----END "
    + "OPENSSH PRIVATE KEY-----"
)
SRC, PROJ, INBOX, STATE = (os.path.join(T, d) for d in ("srcs", "projects", "inbox", "state"))
for d in (SRC + "/.secrets", PROJ, INBOX, STATE):
    os.makedirs(d)
with open(SRC + "/.secrets/fixture.json", "w") as fh:
    json.dump({"api_key": FAKE}, fh)
# Synthetic profile (tests never read the real one): repo layout tests/profile, live layout ~/opt/profile-tests.
for _pf in ("profile", "profile-tests"):
    _pf = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), _pf, "fixtures", "profile.json")
    if os.path.isfile(_pf):
        os.environ["ASSISTANT_PROFILE"] = _pf
        break
DASH = os.path.join(T, "data.json")
with open(DASH, "w") as fh:
    fh.write("{}")
ENV = dict(
    os.environ,
    SECRETSCAN_SOURCES=SRC,
    FLOWD_PROJECTS=PROJ,
    FLOWD_INBOX=INBOX,
    FLOWD_DASHDATA=DASH,
    FLOWD_STATE=STATE,
    FLOWD_OFF=os.path.join(T, "off"),
    FLOWD_PORT=str(free_port()),
    FLOWD_VM="flowd-test@127.0.0.1",
)
os.environ.update(ENV)
loader = importlib.machinery.SourceFileLoader("flowd", BIN)
spec = importlib.util.spec_from_loader("flowd", loader)
F = importlib.util.module_from_spec(spec)
loader.exec_module(F)

# ---------------------------------------------------------------- 1. edge mapping
CASES = [  # tool, input, target, taint, secret
    ("Bash", {"command": "ssh unraid docker ps"}, "unraid", False, False),
    ("Bash", {"command": "ssh -o BatchMode=yes web@192.0.2.20 'sudo -n nginx -t'"}, "nginx", False, False),
    ("Bash", {"command": 'proxy push "dash /live SSE"'}, "nginx", False, False),
    ("Bash", {"command": "git -C ~/opt/x push origin main"}, "github", False, False),
    ("Bash", {"command": "gh repo view someone/x"}, "github", False, False),
    ("Bash", {"command": "curl -s https://example.com/a"}, "web", False, False),
    ("Bash", {"command": "curl -s http://192.0.2.10:7878/api/v3/x"}, "unraid", False, False),
    ("Bash", {"command": 'tg "build done"'}, "telegram", False, False),
    ("Bash", {"command": "browser status"}, "browser", False, False),
    ("Bash", {"command": "cat ~/inbox/agent-watch-1.md"}, "inbox", False, False),
    ("Bash", {"command": "ls -la /tmp && wc -l x"}, "tools", False, False),
    ("Bash", {"command": 'vault ask tg "Did Anna write?"'}, "vault", True, False),
    ("Bash", {"command": "tgread search party"}, "vault", True, False),
    ("Bash", {"command": 'paperless get 12 --reason "he asked"'}, "vault", True, False),
    ("Bash", {"command": "context --json | head -5"}, "vault", True, False),
    ("Bash", {"command": "cat ~/.local/state/context/signals.json"}, "vault", True, False),
    ("Bash", {"command": "grep -c x ~/.claude/projects/-home-node-workspace/a.jsonl"}, "vault", True, False),
    ("Bash", {"command": "bills list"}, "vault", True, False),
    ("Bash", {"command": "cat ~/.secrets/x.json | jq ."}, "tools", False, True),
    ("Read", {"file_path": "/home/node/.secrets/a.txt"}, "tools", False, True),
    ("Read", {"file_path": "/home/node/opt/claude-vault/app.py"}, "vault", True, False),
    ("Read", {"file_path": os.path.expanduser("~/inbox/self-review-1.md")}, "inbox", False, False),
    ("Edit", {"file_path": "/home/node/bin/x", "old_string": "a", "new_string": "b"}, "tools", False, False),
    ("Skill", {"skill": "vault"}, "vault", True, False),
    ("mcp__plugin_telegram-plus_telegram__reply", {"chat_id": "1", "text": "hi"}, "telegram", False, False),
    ("mcp__claude_ai_Google_Calendar__list_events", {}, "calendar", False, False),
    ("mcp__browser__browser_navigate", {"url": "https://x.example"}, "browser", False, False),
    ("WebFetch", {"url": "https://x.example", "prompt": "p"}, "web", False, False),
    ("WebSearch", {"query": "q"}, "web", False, False),
    ("Agent", {"description": "d", "prompt": "p"}, "subagents", False, False),
    ("SendMessage", {"to": "a", "message": "m"}, "subagents", False, False),
]
ok(
    (F.WEB_VM, F.SERVER_HOST, F.PUBLIC) == ("web@192.0.2.20", "192.0.2.10", "dash.example.org"),
    "nginx VM, server and public name come from the (fixture) profile",
)
ok(F.VM == "flowd-test@127.0.0.1", "FLOWD_VM overrides the tunnel host")
for name, inp, target, taint, secret in CASES:
    c = F.classify(name, inp)
    ok(
        (c["target"], c["vault"], c["secret"]) == (target, taint, secret),
        f"classify {name} {json.dumps(inp)[:60]} -> {target} (got {c['target']}, vault {c['vault']}, secret {c['secret']})",  # noqa: E501
    )
    ok(F.EDGE_OF.get(c["target"], "me→" + c["target"]) in F.EDGES, f"edge of {target} is a known edge")
c = F.classify("mcp__foo__bar", {"q": 1})
ok(not c["known"] and c["text"] == "", "unknown tool: not known, no text")
ok(F.vault_kind('vault ask tg "Did Anna write?"') == "vault ask tg", "vault kind: program + allowlisted words only")
ok(F.vault_kind('paperless get 12 --reason "x"') == "paperless get (--reason)", "vault kind: flags, no args")
ok(F.vault_kind("cat ~/.claude/projects/p/a.jsonl") == "transcript read", "vault kind: transcript read")
ok(
    F.norm_edge("telegram-me") == "telegram→me"
    and F.norm_edge("me>vault") == "me→vault"
    and F.norm_edge("me->web") == "me→web"
    and F.norm_edge("x→y") is None
    and F.norm_edge("") == ""
    and F.norm_edge("*") == "*",
    "edge ids normalised and validated",
)

# ---------------------------------------------------------------- 2. processor end to end (in process)
hub, filt = F.Hub(), F.Filter()
P = F.Processor(hub, filt)
P.sess = os.path.join(PROJ, "sess1")
os.makedirs(P.sess + "/subagents")
BASE = 1760515200.0


def iso(dt):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(BASE + dt)) + ".000Z"


def U(content, dt=0, **kw):
    return dict(type="user", timestamp=iso(dt), message={"role": "user", "content": content}, **kw)


def A(blocks, dt=0):
    return dict(type="assistant", timestamp=iso(dt), message={"role": "assistant", "content": blocks})


def use(i, name, inp):
    return {"type": "tool_use", "id": i, "name": name, "input": inp}


def res(i, text, err=False):
    return {"type": "tool_result", "tool_use_id": i, "content": text, "is_error": err}


def text(t):
    return {"type": "text", "text": t}


ANSWER = "Yes: Anna confirmed the Saturday party at Bahnhofstrasse 12 starts at 19:30, bring CHF 120.50"
LONG = "\n".join(str(i) for i in range(1, 3001))
CHAN = (
    '<channel source="plugin:telegram-plus:telegram" chat_id="5550001" message_id="77" user="Alex" '
    'user_id="5550001" ts="2026-10-15T08:00:00Z">Can you check the server?</channel>'
)
with open(P.sess + "/subagents/agent-aclean1.jsonl", "w") as fh:
    fh.write(json.dumps(A([use("s1", "Bash", {"command": "ls"})])) + "\n" + json.dumps(U([res("s1", "a b")])) + "\n")
with open(P.sess + "/subagents/agent-avault1.jsonl", "w") as fh:
    fh.write(
        json.dumps(A([use("s2", "Bash", {"command": 'vault ask gmail "code?"'})]))
        + "\n"
        + json.dumps(U([res("s2", "Mila said the door code is 4711-0815-2222")]))
        + "\n"
    )
SEQ = [
    U(CHAN),
    A(
        [
            use(
                "t1",
                "Bash",
                {
                    "command": 'vault ask tg "Did Anna write about Saturday?"',
                    "description": "Ask the vault whether Anna wrote about Saturday",
                },
            )
        ],
        1,
    ),
    U([res("t1", ANSWER)], 3),
    A([text("Done. Anna confirmed the Saturday party at Bahnhofstrasse 12 starts at 19:30.")], 4),
    A(
        [
            use(
                "t2",
                "mcp__plugin_telegram-plus_telegram__reply",
                {
                    "chat_id": "5550001",
                    "text": "Anna confirmed the Saturday party at Bahnhofstrasse 12 starts at 19:30, bring CHF 120.50!",
                },
            )
        ],
        5,
    ),
    U([res("t2", "sent (id 78)")], 6),
    A([use("t3", "Bash", {"command": "tgread search party"})], 7),
    U([res("t3", "Lena: the party moves to Kreuzplatz")], 8),
    A([use("t4", "Bash", {"command": 'paperless get 12 --reason "he asked"'})], 9),
    U([res("t4", "Invoice Swisscom due 31.10.2026 CHF 89.90")], 10),
    A([use("t5", "Bash", {"command": "cat /etc/os-release; echo done", "description": "Show the OS"})], 11),
    U([res("t5", "PRETTY_NAME=Debian\nkey " + FAKE + "\n" + PEM)], 12),
    A([use("t6", "Bash", {"command": "cat ~/.secrets/fixture.json"})], 13),
    U([res("t6", '{"note": "ZZSECRETFILE"}')], 14),
    U([res("t7", "orphan result ZZORPHAN")], 15),
    A([use("t8", "mcp__foo__bar", {"q": "ZZUNKNOWNIN"})], 16),
    U([res("t8", "ZZUNKNOWNOUT")], 17),
    A([use("t9", "Bash", {"command": "seq 1 3000"})], 18),
    U([res("t9", LONG)], 19),
    U("[heartbeat] idle 12 min. Inbox: vault-events-1.md (new)\nTODO: 2 due", 20),
    A([use("t10", "Bash", {"command": "ssh unraid uptime"})], 21),
    U([res("t10", " 10:00 up 3 days")], 22),
    A([use("t11", "Bash", {"command": "false"})], 23),
    U([res("t11", "Exit code 1\n", True)], 24),
    A(
        [use("t12", "Agent", {"description": "Build widget", "prompt": "Build the widget", "run_in_background": True})],
        25,
    ),
    U(
        [res("t12", [text("Async agent launched")])],
        26,
        toolUseResult={"agentId": "aclean1", "status": "async_launched"},
    ),
    U(
        '<task-notification><task-id>aclean1</task-id><status>completed</status><summary>Agent "Build widget" completed'
        "</summary><result>Widget built ZZCLEANRESULT</result></task-notification>",
        27,
    ),
    U(
        '<task-notification><task-id>avault1</task-id><status>completed</status><summary>Agent "Mail check" completed'
        "</summary><result>Mila said the door code is 4711-0815-2222 ZZVAULTRESULT</result></task-notification>",
        28,
    ),
    U(
        '<task-notification><task-id>amissing</task-id><status>completed</status><summary>Agent "X" completed</summary>'
        "<result>ZZMISSINGRESULT</result></task-notification>",
        29,
    ),
    A([text("Mila said the door code is 4711-0815-2222, noted.")], 30),
    dict(
        type="attachment",
        timestamp=iso(31),
        attachment={"type": "queued_command", "prompt": CHAN.replace("check the server", "also check ZZQUEUED")},
    ),
    U(CHAN.replace("check the server", "also check ZZQUEUED"), 32),
    U("Plain prompt from the app ZZAPP", 33),
]
for o in SEQ:
    P.feed(o)
P.feed(A([use("u1", "Bash", {"command": "ls ~/x"})], 40), "sub:Build widget", "aother")
P.feed(U([res("u1", "x y")], 41), "sub:Build widget", "aother")

# inbox: the first pass only seeds; new notes are judged by name
with open(INBOX + "/old-note.md", "w") as fh:
    fh.write("old")
P.inbox_tick()
with open(INBOX + "/vault-events-1729000000.md", "w") as fh:
    fh.write("Noah wrote: the boat leaves from pier seven at dawn")
with open(INBOX + "/agent-watch-1729000001.md", "w") as fh:
    fh.write("agent ZZWATCH stalled for 20 min")
P.inbox_tick()
P.feed(A([text("FYI Noah wrote: the boat leaves from pier seven at dawn")], 50))

EVS = sorted((e for r in hub.rings.values() for e in r), key=lambda e: e["id"])
BLOB = json.dumps(EVS, ensure_ascii=False) + json.dumps(list(hub.full.values()), ensure_ascii=False)


def on(edge, **kw):
    return [e for e in EVS if e["edge"] == edge and all(e.get(k) == v for k, v in kw.items())]


for bad in (FAKE, "PRIVATE KEY", "ZZSECRETFILE", "ZZORPHAN", "ZZUNKNOWNIN", "ZZUNKNOWNOUT", "b3BlbnNzaC1r", "5550001"):
    ok(bad not in BLOB, f"never emitted: {bad!r}")
# owner: what my session received from the vault is shown, unmasked
for seen in (
    "Did Anna write about Saturday?",
    "whether Anna wrote",
    ANSWER,
    "Lena: the party moves to Kreuzplatz",
    "Invoice Swisscom due 31.10.2026 CHF 89.90",
    "ZZVAULTRESULT",
    "ZZMISSINGRESULT",
    "door code is 4711-0815-2222",
    "Noah wrote: the boat leaves from pier seven at dawn",
    "vault-events-1.md",
):
    ok(seen in BLOB, f"vault content my session saw is shown: {seen[:40]!r}")
ok(
    "[vault-derived" not in BLOB and "[line hidden" not in BLOB and "withheld" not in BLOB,
    "nothing masked, hidden or withheld for the vault",
)
ok(
    [e for e in F.EDGES if "vault" in e] == ["me→vault"]
    and not set(F.NODES) & {"filter", "llm", "feed", "approvals", "gmail", "paperless", "firefly"},
    "inside the vault has no edge or node: only my CLI <-> vault traffic exists",
)
ok(all(e["from"] in F.NODES and e["to"] in F.NODES for e in EVS), "every event is on a known node pair")
for good in (
    "Can you check the server?",
    "PRETTY_NAME=Debian",
    " up 3 days",
    "ZZCLEANRESULT",
    "ZZWATCH",
    "ZZAPP",
    "ZZQUEUED",
    "Build the widget",
):
    ok(good in BLOB, f"shown: {good!r}")
ok(BLOB.count("ZZQUEUED") == 2, "queued message shown once (telegram edge + API edge), not again as a user message")

# ---------------------------------------------------------------- 2b. stream filters (EvFilter; 2026-10-15)
EF = F.EvFilter
ok(EF()(EVS[0]) and not EF().active, "filter: empty filter passes everything")
for bad in (FAKE, "PRIVATE KEY", "ZZSECRETFILE", "ZZUNKNOWNIN", "ZZUNKNOWNOUT", "ZZORPHAN"):
    ok(not any(EF(q=bad)(e) for e in EVS), f"filter: q={bad!r} matches no event (search never sees hidden content)")
vault_evs = [e for e in EVS if e["edge"] == "me→vault"]
ok(
    all(any(EF(q=w)(e) for e in vault_evs) for w in ("bahnhofstrasse", "did anna", "kreuzplatz")),
    "filter: q finds vault questions and answers (shown content)",
)
ok(
    all(EF(q=w)(e) == (w in EF.hay(e)) for e in EVS for w in ("anna", "swisscom")),
    "filter: q matches exactly what the event shows",
)
ok(
    all(EF(q="server CHECK")(e) == ("server" in EF.hay(e) and "check" in EF.hay(e)) for e in EVS)
    and any(EF(q="server CHECK")(e) for e in EVS),
    "filter: q is case-insensitive, every word must match",
)
ok(
    all(e["kind"] == "message" for e in EVS if EF(kinds=["message"])(e)) and any(EF(kinds=["message"])(e) for e in EVS),
    "filter: kind",
)
ok(
    all(e["to"] == "me" for e in EVS if EF(direction="in")(e))
    and all(e["from"] == "me" for e in EVS if EF(direction="out")(e)),
    "filter: dir in/out",
)
ok(
    all(e["from"] == "telegram" for e in EVS if EF(frm="telegram")(e))
    and all(e["to"] == "tools" for e in EVS if EF(to="tools")(e)),
    "filter: from/to node",
)


def mk(**kw):
    return dict(
        {
            "edge": "me→tools",
            "from": "tools",
            "to": "me",
            "kind": "tool_result",
            "chip": "result",
            "actor": "main",
            "t": 1000.0,
            "meta": {},
        },
        **kw,
    )


ok(
    EF(err=True)(mk(meta={"exit": 2}))
    and EF(err=True)(mk(meta={"error": True}))
    and EF(err=True)(mk(chip="error"))
    and not EF(err=True)(mk(meta={"exit": 0}))
    and not EF(err=True)(mk()),
    "filter: err = exit != 0, error flag or chip error",
)
ok(EF(since=1000)(mk()) and not EF(since=1000.5)(mk()), "filter: since")
ok(
    EF(q="ls -la")(mk(meta={"cmd": "ls -la /tmp"})) and not EF(q="zzz")(mk(meta={"cmd": "ls"})),
    "filter: q sees meta strings",
)
for qs, what in (
    ({"kind": ["Bad!"]}, "kind"),
    ({"kind": [",".join(["a"] * 13)]}, "kind"),
    ({"from": ["nowhere"]}, "node"),
    ({"dir": ["up"]}, "dir"),
    ({"since": ["abc"]}, "since"),
    ({"since": ["nan"]}, "since"),
    ({"q": ["x" * 201]}, "q"),
):
    try:
        EF.parse(qs)
        ok(False, f"filter: parse rejects bad {what} {qs}")
    except ValueError:
        ok(True, f"filter: parse rejects bad {what} {qs}")
f = EF.parse(
    {"kind": ["message,reply"], "from": ["telegram"], "dir": ["in"], "err": ["1"], "since": ["5"], "q": [" A  b "]}
)
ok(
    f.echo()
    == {
        "kind": ["message", "reply"],
        "from": "telegram",
        "to": None,
        "dir": "in",
        "err": True,
        "since": 5.0,
        "q": "a b",
    },
    "filter: parse + echo",
)
vault = on("me→vault")
ok(len(vault) == 6 and all(e.get("text") for e in vault), "vault: 3 calls + 3 results, all with their text")
ok(
    [e["meta"].get("cmd") for e in vault if e["kind"] == "vault_call"]
    == ["vault ask tg", "tgread search", "paperless get (--reason)"],
    "vault calls: command kind in meta",
)
ok(
    [e for e in vault if e["kind"] == "vault_call"][0]["text"].startswith(
        'vault ask tg "Did Anna write about Saturday?"'
    ),
    "vault call: the full command",
)
r1 = [e for e in vault if e["kind"] == "vault_result"][0]
ok(
    r1["meta"].get("exit") == 0
    and r1["meta"].get("ms") == 2000
    and r1["meta"].get("bytes") == len(ANSWER)
    and r1["text"] == ANSWER
    and r1["chip"] == "answer",
    "vault result: full answer, exit code, timing and size",
)
ok(any("whether Anna" in e["meta"].get("desc", "") for e in vault), "vault call: description shown")
api_v = [e for e in on("me→anthropic") if e["meta"].get("tool") == "Bash" and ANSWER in e.get("text", "")]
ok(
    api_v and not any(e["meta"].get("hidden") in ("vault command", "vault result") for e in EVS),
    "API edge: vault calls and results with text too",
)
reply = on("telegram→me", kind="reply")
ok(len(reply) == 1 and "Bahnhofstrasse 12" in reply[0].get("text", ""), "reply quoting a vault answer is shown as sent")
out = [e for e in on("app→me", kind="output") if e.get("text", "").startswith("Done.")]
ok(out and "Anna confirmed" in out[0]["text"], "my text quoting a vault answer is shown")
mila = [e for e in on("app→me", kind="output") if "noted" in e.get("text", "")]
ok(mila and "door code" in mila[0]["text"], "quote of a subagent's vault answer is shown")
noah = [e for e in on("app→me", kind="output") if e.get("text", "").startswith("FYI")]
ok(noah and "pier seven" in noah[0]["text"], "quote of an inbox note is shown")
t5 = [e for e in on("me→tools", kind="tool_result") if "PRETTY_NAME" in e.get("text", "")]
ok(
    t5 and "[redacted]" in t5[0]["text"] and t5[0]["meta"].get("redacted", 0) >= 2,
    "secret value + private key redacted in output",
)
t6 = [e for e in on("me→tools", kind="tool_result") if e["meta"].get("hidden") == "secret file"]
ok(len(t6) == 1 and "text" not in t6[0], "output of a ~/.secrets read is hidden")
t9 = [e for e in on("me→tools", kind="tool_result") if e.get("more")]
ok(
    len(t9) == 1 and len(t9[0]["text"]) == F.TEXT_MAX and len(hub.more(t9[0]["id"])) == len(LONG),
    "long output: 2 KB + full text via more",
)
hb = on("inbox→me", kind="heartbeat")
ok(
    hb and "vault-events-1.md" in hb[0]["text"] and "TODO: 2 due" in hb[0]["text"],
    "heartbeat: shown whole, vault lines included",
)
ok(on("me→unraid", kind="tool_call") and on("me→unraid", kind="tool_result"), "ssh unraid lands on me→unraid")
err = [e for e in on("me→tools", kind="tool_result") if e["chip"] == "error"]
ok(err and err[0]["meta"].get("exit") == 1, "exit code parsed from an error result")
res_ = on("me→subagents", kind="result")
ok(
    len(res_) == 3
    and all(any(z in e.get("text", "") for e in res_) for z in ("ZZCLEANRESULT", "ZZVAULTRESULT", "ZZMISSINGRESULT")),
    "subagent results: all shown, also from a subagent that used the vault",
)
ok(
    on("me→subagents", kind="launch") and on("me→subagents", kind="launch")[0]["chip"] == "Agent",
    "agent launch on me→subagents",
)
ok(any(e["actor"] == "sub:Build widget" for e in EVS), "subagent events carry their actor")
unk = [e for e in EVS if e["meta"].get("tool") == "mcp__foo__bar"]
ok(unk and all("text" not in e for e in unk), "unknown tool: metadata only")
tg = on("telegram→me", kind="message")
ok(tg and tg[0]["chip"] == "message" and "5550001" not in json.dumps(tg), "telegram in: text without ids")
notes = on("cron→inbox")
ok(
    len(notes) == 2
    and any(
        e["chip"] == "vault-events"
        and "pier seven" in e.get("text", "")
        and e["meta"].get("name") == "vault-events-1729000000.md"
        for e in notes
    )
    and any("ZZWATCH" in e.get("text", "") for e in notes),
    "inbox: every note shown with its content",
)
P.dash()  # first look only remembers the mtime
P.dash()
with open(DASH, "w") as fh:
    fh.write('{"x": 1}')
os.utime(DASH, (time.time() + 5, time.time() + 5))
P.dash()
P.dash()
ok(len(hub.rings["cron→nginx"]) == 1, "dashboard refresh -> one cron→nginx event")
sys.path.insert(0, os.path.expanduser("~/bin"))
import secretscan  # noqa: E402  (fixture sources via SECRETSCAN_SOURCES)

ok(secretscan.scan(BLOB) == [], "secretscan finds nothing in all emitted events")
ok(
    all(set(e) <= {"id", "t", "edge", "from", "to", "kind", "chip", "actor", "text", "more", "meta"} for e in EVS),
    "event keys",
)
ok(
    all(e["from"] in F.NODES and e["to"] in F.NODES and e["edge"] in F.EDGES for e in EVS),
    "event nodes and edges known",
)
f2 = F.Filter()
ok(
    f2.clean("Zora booked table nine tonight ok") == ("Zora booked table nine tonight ok", {}),
    "plain text passes unchanged (no masking)",
)
saved = F.secretscan
F.secretscan = None
ok(
    F.Filter().clean("hello world") == (None, {"hidden": "no secretscan (fail closed)"}),
    "no secretscan: no text (fail closed)",
)
F.secretscan = saved

# ---------------------------------------------------------------- 2b. Telegram buttons and reactions (plugin schema)
# Buttons as the plugin's ButtonSpec allows them: plain strings (data = text) and {text, data|callback_data|url}.
ok(
    F.button_specs(
        [["Yes", "No"], [{"text": "Later", "data": "x:later"}, {"text": "Docs", "url": "https://x.example"}]]
    )
    == [("Yes", "Yes"), ("No", "No"), ("Later", "x:later"), ("Docs", None)],
    "buttons: strings and objects, url has no data",
)
ok(
    F.button_specs(["A", {"text": "B", "callback_data": "b:1"}]) == [("A", "A"), ("B", "b:1")],
    "buttons: a flat list is one row; callback_data alias",
)
ok(F.button_specs([[{"text": "E", "data": ""}]]) == [("E", "")], "buttons: empty data stays (plugin uses ??)")
ok(
    F.button_specs([[None, 3, {"data": "no-text"}, {"text": ""}, "ok"]]) == [("ok", "ok")]
    and F.button_specs(None) == []
    and F.button_specs("x") == [],
    "buttons: malformed entries skipped, never raised",
)
RP = "mcp__plugin_telegram-plus_telegram__reply"
ok(
    F.tool_text(RP, "reply", {"text": "Pick", "buttons": [["Yes", "No"]]}) == "Pick\n[buttons: Yes | No]",
    "reply: plain-string buttons listed",
)
ok(
    F.tool_text(RP, "reply", {"text": "Pick", "buttons": [["Yes", {"text": "Later", "data": "x:l"}]]})
    == "Pick\n[buttons: Yes | Later]",
    "reply: mixed string + object buttons listed",
)
ok(
    F.tool_text(
        "mcp__plugin_telegram-plus_telegram__edit_message",
        "edit_message",
        {"chat_id": "1", "message_id": "2", "buttons": []},
    )
    == "",
    "edit_message buttons []: no label line",
)
hub2 = F.Hub()
P2 = F.Processor(hub2, F.Filter())
P2.sess = P.sess
TGA = 'source="plugin:telegram-plus:telegram" chat_id="5550001" user="Alex" user_id="5550001" ts="2026-10-15T08:00:00Z"'
for i, o in enumerate(
    [
        f'<channel {TGA} event_type="reaction" reaction="👍" reacted_to_message_id="78" reacted_to_from="bot" '
        f'reacted_to_text="Ship it?">Reacted 👍 to your message: "Ship it?"</channel>',
        f'<channel {TGA} event_type="reaction" reaction="🔥" removed_reaction="👍" reacted_to_message_id="78">'
        f"Changed reaction from 👍 to 🔥 on message 78</channel>",
        f'<channel {TGA} event_type="reaction" removed_reaction="👎" reacted_to_message_id="78">Removed reaction 👎 from ZZRM</channel>',  # noqa: E501
        f'<channel {TGA} event_type="reaction" emoji="🎉" reacted_to_message_id="78">Reacted 🎉 ZZNOFIELD</channel>',
        f'<channel {TGA} message_id="78" event_type="button" callback_data="x:yes" button_text="Yes" '
        f'message_text="Ship it?">(pressed button: Yes)</channel>',
    ]
):
    P2.feed(U(o, 60 + i))
EV2 = sorted((e for r in hub2.rings.values() for e in r), key=lambda e: e["id"])
rx = [e for e in EV2 if e["edge"] == "telegram→me" and e["chip"] == "reaction"]
ok(
    [e.get("text") for e in rx[:3]] == ["👍", "🔥", "removed 👎"],
    f"reaction chip text from the plugin's reaction / removed_reaction attrs (got {[e.get('text') for e in rx]})",
)
ok(len(rx) == 4 and "ZZNOFIELD" in rx[3].get("text", ""), "reaction without a reaction attr: falls back to the body")
bt = [e for e in EV2 if e["edge"] == "telegram→me" and e["chip"] == "button"]
ok(len(bt) == 1 and bt[0].get("text") == "pressed: Yes", "button press: pressed label")
ok("5550001" not in json.dumps(EV2), "reaction/button events carry no ids")

# ---------------------------------------------------------------- 3. SSE server (subprocess)
port = free_port()
proj2 = os.path.join(T, "projects2")
os.makedirs(proj2)
tr = os.path.join(proj2, "sessA.jsonl")
with open(tr, "w") as fh:
    for o in (U(CHAN), A([use("b1", "Bash", {"command": "seq 1 3000"})], 1), U([res("b1", LONG)], 2)):
        fh.write(json.dumps(o) + "\n")
# Agents tab fixtures: a subagent transcript with planted private text, fake agent-watch / traces (agentfeed)
AG_SESS, AG_ID = "sessA", "a0123456789abcdef"
os.makedirs(os.path.join(proj2, AG_SESS, "subagents"))
AG_TR = os.path.join(proj2, AG_SESS, "subagents", f"agent-{AG_ID}.jsonl")
with open(AG_TR, "w") as fh:
    for o in (
        U("ZZAGPROMPT do the work " + FAKE, 0),
        A([text("Starting with the server check.")], 1),
        A([use("g1", "Bash", {"command": "cat /home/node/ZZAGCMD", "description": "Read the ZZOK config"})], 2),
        U([res("g1", "ZZAGOUT " + FAKE, err=True)], 3),
    ):
        fh.write(json.dumps(o) + "\n")
AG_AW, AG_TRC = os.path.join(T, "fake-aw"), os.path.join(T, "fake-traces")
with open(AG_AW, "w") as fh:
    fh.write(
        "#!/bin/sh\ncat <<'EOF'\n"
        + json.dumps(
            {
                "now": 0,
                "hours": 6,
                "agents": [
                    {
                        "id": AG_ID,
                        "session": AG_SESS,
                        "status": "running",
                        "kind": "tool_result",
                        "start": time.time() - 60,
                        "last": time.time(),
                        "description": "Fixture agent run",
                        "type": "general-purpose",
                        "effort": "high",
                        "depth": 1,
                        "parent": None,
                    }
                ],
            }
        )
        + "\nEOF\n"
    )
with open(AG_TRC, "w") as fh:
    fh.write("#!/bin/sh\necho '[]'\n")
os.chmod(AG_AW, 0o755)
os.chmod(AG_TRC, 0o755)
CI_ST = os.path.join(T, "ci-state")  # CI card (/live/ci): a deploy log with one green land, an empty /proc
os.makedirs(os.path.join(T, "ci-proc"))
os.makedirs(CI_ST)
with open(os.path.join(CI_ST, "deploys.jsonl"), "w") as fh:
    fh.write(
        json.dumps(
            {
                "op": "land",
                "ok": True,
                "branch": "zzci-branch",
                "land_start": time.time() - 300,
                "end": time.time() - 30,
                "ts": time.time() - 30,
                "gate": {"ci_local_secs": 120.0},
                "rerun": {"local pytest-3.13": {"ids": ["tests/a.py::test_zzci_hidden"], "ok": True}},
            }
        )
        + "\n"
    )
env = dict(
    ENV,
    FLOWD_PROJECTS=proj2,
    FLOWD_PORT=str(port),
    AGENTFEED_PROJECTS=proj2,
    AGENTFEED_AGENT_WATCH=AG_AW,
    AGENTFEED_TRACES=AG_TRC,
    SECUREFILL_STATE=os.path.join(T, "securefill"),
    CIFEED_STATE=CI_ST,
    CIFEED_PROC=os.path.join(T, "ci-proc"),
    CIFEED_GATE=os.path.join(T, "ci-gate"),
    CIFEED_CRONTAB=os.path.join(T, "ci-crontab"),
)
srv = subprocess.Popen(
    [sys.executable, "-I", BIN, "serve", "--port", str(port)],
    env=env,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.PIPE,
)


def get(path, headers=None):
    import http.client

    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", path, headers=headers or {})
    r = c.getresponse()
    return r.status, dict(r.getheaders()), r.read()


def sse_read(path, until, headers="", timeout=6, after=None):
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.settimeout(0.3)
    s.sendall(f"GET {path} HTTP/1.1\r\nHost: t\r\n{headers}\r\n".encode())
    buf, end, did = b"", time.time() + timeout, False
    while time.time() < end and not until(buf):
        if after and not did and b"event: hello" in buf:
            after()
            did = True
        try:
            d = s.recv(65536)
        except TimeoutError:
            continue
        if not d:
            break
        buf += d
    s.close()
    head, _, body = buf.partition(b"\r\n\r\n")
    return head.decode("latin-1"), body.decode("utf-8")


try:
    for _ in range(50):
        try:
            st, _, body = get("/live/health")
            if st == 200 and json.loads(body)["events"] >= 5:
                break
        except OSError:
            pass
        time.sleep(0.2)
    st, hd, body = get("/live/health")
    h = json.loads(body)
    ok(
        st == 200 and h["ok"] and h["redaction"] is True and h["clients"] == 0 and h["agentfeed"] is True,
        "health: ok, redaction on, agentfeed loaded",
    )
    ok(
        set(h)
        == {
            "ok",
            "version",
            "uptime",
            "clients",
            "events",
            "last_event_age",
            "redaction",
            "agentfeed",
            "cifeed",
            "edges",
        },
        "health: no content",
    )
    st, _, _ = get("/live?edge=nope")
    ok(st == 400, "unknown edge -> 400")
    q = "/live?edge=" + urllib.parse.quote("telegram→me") + "&pulse=1"

    def append():
        with open(tr, "a") as fh:
            for o in (
                U(CHAN.replace("check the server", "say hi ZZLIVE"), 5),
                A([use("b2", "Bash", {"command": "uptime"})], 6),
                U([res("b2", "up")], 7),
            ):
                fh.write(json.dumps(o) + "\n")

    head, body = sse_read(q, lambda b: b"ZZLIVE" in b and '"edge":"me→tools"'.encode() in b, after=append)
    ok(head.startswith("HTTP/1.0 200") or head.startswith("HTTP/1.1 200"), "SSE: 200")
    hl = head.lower()
    ok(
        "content-type: text/event-stream" in hl and "cache-control: no-store" in hl and "x-accel-buffering: no" in hl,
        "SSE: event-stream, no-store, no proxy buffering",
    )
    frames = [f for f in body.split("\n\n") if f.strip()]
    ok(frames and frames[0] == "retry: 5000", "SSE: retry first")
    ok(len(frames) > 1 and frames[1].startswith("event: hello\ndata: {"), "SSE: hello second")
    evs, pulses, good = [], [], True
    for f in frames[2:]:
        lines = f.split("\n")
        if lines[0].startswith("id: "):
            good &= (
                lines[0][4:].isdigit() and lines[1] == "event: ev" and lines[2].startswith("data: ") and len(lines) == 3
            )
            evs.append((int(lines[0][4:]), json.loads(lines[2][6:])))
        elif lines[0] == "event: pulse":
            good &= lines[1].startswith("data: ") and len(lines) == 2
            pulses.append(json.loads(lines[1][6:]))
        else:
            good = False
    ok(good, "SSE: every frame is id/event/data or event/data, one data line each")
    ok([e["text"] for _, e in evs if e["kind"] == "message"][:1] == ["Can you check the server?"], "SSE: backlog first")
    ok(any("ZZLIVE" in e.get("text", "") for _, e in evs), "SSE: live event arrives")
    ok(all(e["edge"] == "telegram→me" for _, e in evs), "SSE: only the chosen edge")
    ok(
        pulses
        and all(set(p) == {"edge", "from", "to", "kind"} for p in pulses)
        and any(p["edge"] == "me→tools" for p in pulses),
        "SSE: pulses for other edges, without content",
    )
    ids = [i for i, _ in evs]
    ok(ids == sorted(ids) and len(set(ids)) == len(ids), "SSE: ids increase")
    head, body = sse_read(
        q, lambda b: b"event: hello" in b and b.count(b"\n\n") >= 3, headers=f"Last-Event-ID: {ids[0]}\r\n", timeout=2
    )
    ok(f"id: {ids[0]}\n" not in body and f"id: {ids[-1]}\n" in body, "SSE: Last-Event-ID resumes after that event")
    head, body = sse_read("/live?edge=" + urllib.parse.quote("me→tools"), lambda b: b'"more":true' in b)
    mid = [json.loads(f.split("\n")[2][6:])["id"] for f in body.split("\n\n") if '"more":true' in f]
    st, _, b = get(f"/live/more?id={mid[0]}") if mid else (0, 0, b"{}")
    ok(st == 200 and len(json.loads(b)["text"]) == len(LONG), "/live/more returns the full text")
    ok(get("/live/more?id=1")[0] == 404, "/live/more: unknown id -> 404")
    head, body = sse_read(
        "/live?edge=" + urllib.parse.quote("telegram→me,me→tools"),
        lambda b: b"ZZLIVE" in b and b'"more":true' in b,
        timeout=3,
    )
    kinds = {json.loads(f.split("\n")[2][6:])["edge"] for f in body.split("\n\n") if f.startswith("id: ")}
    ok(kinds == {"telegram→me", "me→tools"}, "SSE: several edges in one stream (edge=a,b)")
    ok(get("/live?edge=" + urllib.parse.quote("me→tools,bogus"))[0] == 400, "SSE: one unknown edge in a list -> 400")

    def evs_of(body):
        return [json.loads(f.split("\n")[2][6:]) for f in body.split("\n\n") if f.startswith("id: ")]

    def hello_of(body):
        m = [f for f in body.split("\n\n") if f.startswith("event: hello")]
        return json.loads(m[0].split("\n")[1][6:]) if m else {}

    head, body = sse_read("/live?edge=*&kind=message", lambda b: b"event: hello" in b and b"event: ev" in b, timeout=2)
    got = evs_of(body)
    ok(got and all(e["kind"] == "message" for e in got), "SSE filter: kind=message, all edges")
    ok(hello_of(body).get("filter", {}).get("kind") == ["message"], "SSE filter: hello echoes the filter")
    head, body = sse_read("/live?edge=*&q=zzlive", lambda b: b"ZZLIVE" in b, timeout=2)
    got = evs_of(body)
    ok(
        got and all("zzlive" in F.EvFilter.hay(e) for e in got) and not any(e["edge"] == "me→tools" for e in got),
        "SSE filter: q filters the backlog server-side",
    )
    head, body = sse_read("/live?edge=*&dir=in", lambda b: b"event: hello" in b and b"event: ev" in b, timeout=2)
    got = evs_of(body)
    ok(got and all(e["to"] == "me" for e in got), "SSE filter: dir=in")
    head, body = sse_read("/live?edge=*&since=" + str(int(time.time()) + 3600), lambda b: False, timeout=1)
    ok("event: hello" in body and not evs_of(body), "SSE filter: since in the future = no backlog")
    head, body = sse_read("/live?edge=*&err=1", lambda b: False, timeout=1)
    ok("event: hello" in body and all(F.is_error(e) for e in evs_of(body)), "SSE filter: err=1 only errors")
    for bad in ("kind=Bad%21", "from=nowhere", "dir=up", "since=abc", "q=" + "x" * 201):
        ok(get("/live?edge=*&" + bad)[0] == 400, f"SSE filter: bad {bad[:12]} -> 400")
    ok(get("/elsewhere")[0] == 404, "other paths -> 404 (no static without --static)")

    # System tab's CI card: /live/ci streams the cifeed snapshot (branch names, times, counts; never test ids)
    head, body = sse_read("/live/ci", lambda b: b"event: ci" in b, timeout=8)
    ci = json.loads(body.split("event: ci\ndata: ")[1].split("\n")[0]) if "event: ci" in body else {}
    ok(
        "text/event-stream" in head
        and "event: hello" in body
        and (ci.get("lands") or [{}])[0].get("branch") == "zzci-branch"
        and ci["lands"][0].get("total_s") == 270
        and ci.get("queue", {}).get("holder") is None,
        f"ci: SSE hello + snapshot ({ci.get('lands')})",
    )
    ok(
        "test_zzci_hidden" not in body and (ci["lands"][0].get("reruns") if ci.get("lands") else None) == 1,
        "ci: reruns counted, test ids never streamed",
    )
    ok(json.loads(get("/live/health")[2]).get("cifeed") is True, "ci: health says cifeed loaded")

    # Agents tab: the list and one agent's steps, strictly filtered (agentfeed)
    head, body = sse_read("/live/agents", lambda b: b"event: agents" in b, timeout=8)
    ok("text/event-stream" in head and "event: hello" in body and "event: agents" in body, "agents: SSE hello + list")
    snap = json.loads(body.split("event: agents\ndata: ")[1].split("\n")[0]) if "event: agents" in body else {}
    row = (snap.get("agents") or [{}])[0]
    ok(
        row.get("id") == AG_ID
        and row.get("title") == "Fixture agent run"
        and row.get("status") == "running"
        and row.get("tools") == 1
        and row.get("errors") == 1
        and row.get("effort") == "high",
        f"agents: the row from agent-watch + the transcript ({row})",
    )

    def add_step():
        with open(AG_TR, "a") as fh:
            for o in (
                A([use("g2", "Read", {"file_path": "/home/node/ZZAGREAD"})], 4),
                U([res("g2", "ZZAGFILE")], 5),
                A([text("Done. ZZAGRESULT final report")], 6),
            ):
                o["message"]["stop_reason"] = (
                    "end_turn"
                    if o["type"] == "assistant" and o["message"]["content"][0]["type"] == "text"
                    else "tool_use"
                )
                fh.write(json.dumps(o) + "\n")

    head, body = sse_read("/live/agent?id=" + AG_ID, lambda b: b'"k":"end"' in b, timeout=8, after=add_step)
    ok("event: steps" in body and "event: step" in body and '"k":"end"' in body, "agent: backlog then live steps")
    ok(
        "Read the ZZOK config" in body
        and "Starting with the server check." in body
        and '"ok":false' in body
        and '"n":"Read","d":"read a file"' in body,
        "agent: own descriptions, prose, ok/error, tool names",
    )
    ok(
        not any(
            z in body
            for z in ("ZZAGPROMPT", "ZZAGCMD", "ZZAGOUT", "ZZAGREAD", "ZZAGFILE", "ZZAGRESULT", FAKE, "/home/node")
        ),
        "agent: no prompt, command, output, path, file content, result or secret",
    )
    ok(
        get("/live/agent?id=../../x")[0] == 400 and get("/live/agent?id=a999999999999999")[0] == 404,
        "agent: bad id 400, unknown 404",
    )
    head, body = sse_read("/live/agent?id=main", lambda b: b"event: steps" in b, timeout=5)
    ok(
        "event: steps" in body and "check the server" not in body and "seq 1 3000" not in body,
        "agent: the main session, kinds only (no owner text, no command)",
    )
finally:
    srv.terminate()
    try:
        err = srv.communicate(timeout=5)[1].decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        srv.kill()
        err = ""
ok("Traceback" not in err and "tick:" not in err, "server log: no errors")
ok("ZZLIVE" not in err and "Can you check" not in err and "ZZAG" not in err, "server log: no content")

# ---------------------------------------------------------------- 4. kill switch + supervisor
port3 = free_port()
# the supervisor's tunnel runs `ssh`: a stub that fails like an unreachable VM (no ssh client or network needed)
STUB = os.path.join(T, "stubbin")
os.makedirs(STUB, exist_ok=True)
with open(os.path.join(STUB, "ssh"), "w") as fh:
    fh.write("#!/bin/sh\nexit 255\n")
os.chmod(os.path.join(STUB, "ssh"), 0o755)
env3 = dict(ENV, FLOWD_PROJECTS=proj2, FLOWD_PORT=str(port3), PATH=STUB + os.pathsep + ENV.get("PATH", "/usr/bin:/bin"))


def run(*a):
    return subprocess.run([sys.executable, "-I", BIN] + list(a), env=env3, capture_output=True, text=True, timeout=30)


r = run("off")
ok(r.returncode == 0 and os.path.exists(ENV["FLOWD_OFF"]), "off: flag written")
run("ensure")
ok(
    "switched off" in run("status").stdout and not os.path.exists(os.path.join(STATE, "server.pid")),
    "off: ensure starts nothing",
)
r = run("on")
ok(
    r.returncode == 0 and not os.path.exists(ENV["FLOWD_OFF"]) and "server started" in r.stdout,
    "on: flag removed, server started",
)
up = False
for _ in range(40):
    try:
        import http.client

        c = http.client.HTTPConnection("127.0.0.1", port3, timeout=1)
        c.request("GET", "/live/health")
        up = c.getresponse().status == 200
        break
    except OSError:
        time.sleep(0.2)
ok(up, "on: server answers")
r = run("ensure")
ok("server started" not in r.stdout, "ensure: a healthy server is left alone")
r = run("off")
time.sleep(0.5)
try:
    c = http.client.HTTPConnection("127.0.0.1", port3, timeout=1)
    c.request("GET", "/live/health")
    still = True
except OSError:
    still = False
ok(not still, "off: server stopped")
ok(run("edges").stdout.split() == list(F.EDGES), "edges command")

# ---------------------------------------------------------------- tab-view beacon (usage dash.view)
import types  # noqa: E402

_logged = []
_fake_usage = types.ModuleType("usage")
_fake_usage.log = lambda *a, **kw: _logged.append((a, kw)) or True
_real_usage = sys.modules.get("usage")
sys.modules["usage"] = _fake_usage
F._SEEN_LAST.clear()
t0 = 1_800_000_000
ok(
    F.seen_log("system", t0, "load")
    and _logged[-1][1]["attrs"] == {"surface": "web", "why": "load"}
    and _logged[-1][1]["outcome"] == "system",
    "beacon: a load counts, tab name + why only",
)
ok(not F.seen_log("system", t0 + 60, "tab"), "beacon: the same tab within 5 min: once")
ok(F.seen_log("usage", t0 + 70, "tab"), "beacon: another tab he picks counts")
ok(
    not F.seen_log("system", t0 + 400, None) and not F.seen_log("system", t0 + 1000, None),
    "beacon: an old page (no why) refreshing every few min never counts while beacons keep coming",
)
ok(
    F.seen_log("system", t0 + 1000 + 1801, None) and _logged[-1][1]["attrs"]["why"] == "legacy",
    "beacon: an old page counts again after 30 min without any beacon (why legacy)",
)
ok(F.seen_log("system", t0 + 9000, "focus"), "beacon: back in view after > 30 min (focus) counts")
ok(
    F.seen_log("health", t0 + 9100, "tab") and F.seen_log("ci", t0 + 9200, "tab") and _logged[-1][1]["outcome"] == "ci",
    "beacon: the Health and CI tabs (30.10 split of System) count",
)
ok(
    not F.seen_log("system", t0 + 20000, "reload") and not F.seen_log("nope", t0 + 20000, "load"),
    "beacon: unknown why or tab refused",
)
n_before = len(_logged)
F.seen_log("today", t0 + 30000, "load")
ok(
    len(_logged) == n_before + 1 and set(_logged[-1][1]) <= {"kind", "channel", "actor", "outcome", "attrs", "src"},
    "beacon: no other fields logged",
)
if _real_usage is None:
    sys.modules.pop("usage", None)
else:
    sys.modules["usage"] = _real_usage
# the handler passes why through (GET /live/seen?v=TAB&why=WHY)
_calls = []
_orig_seen = F.seen_log
F.seen_log = lambda v, now=None, why=None: _calls.append((v, why))
try:

    class _H(F.Handler):
        def __init__(self, path):
            self.path, self.sent = path, None

        def send_json(self, code, obj):
            self.sent = (code, obj)

    for path, want in (("/live/seen?v=usage&why=focus", ("usage", "focus")), ("/live/seen?v=system", ("system", None))):
        h = _H(path)
        h.do_GET()
        ok(_calls and _calls[-1] == want and h.sent == (200, {"ok": True}), f"beacon: handler passes {path}")
finally:
    F.seen_log = _orig_seen
ok(
    F.parse_basicauth("u:p") == "u:p"
    and F.parse_basicauth('{"user": "u", "password": "p"}') == "u:p"
    and F.parse_basicauth("user=u\npassword=p") == "u:p"
    and F.parse_basicauth("u p") == "u:p",
    "basic-auth formats",
)

# ---------------------------------------------------------------- flowd probe by handle "dash" (core.egress)
# The real core.egress (the lib next to the binary) against a fake hub on loopback, in a temp HOME (ASSISTANT_TEST=1:
# core.paths refuses the real home). curl is stubbed: nothing reaches nginx.
import base64  # noqa: E402
import contextlib  # noqa: E402
import http.server  # noqa: E402
import io  # noqa: E402
import threading  # noqa: E402

EGH = os.path.join(T, "eghome")
os.makedirs(EGH + "/.config/assistant")
os.makedirs(EGH + "/.secrets")
BA_PW = "ProbeFixturePw-" + "5e4d3c2b1a"
with open(EGH + "/.secrets/browser-basicauth.txt", "w") as fh:
    fh.write("user: probe-fixture\npassword: " + BA_PW + "\n")  # the live 2-line kv form
with open(EGH + "/.secrets/hub-token", "w") as fh:
    fh.write("hub-token-fixture\n")
HUBREQ, HUBANS = [], {}


class _FakeHub(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        HUBREQ.append((self.path, json.loads(self.rfile.read(n) or b"{}")))
        code, obj = HUBANS.get(self.path, (404, {"error": "not_found"}))
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


_hub_srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeHub)
threading.Thread(target=_hub_srv.serve_forever, daemon=True).start()
_CURL = []


def _fake_run(argv, **kw):
    _CURL.append((argv, kw.get("input")))
    return subprocess.CompletedProcess(argv, 0, stdout='{"ok": true}\nHTTP 200\n', stderr="")


def probe(mode, answers=None, hub_up=True):
    """flowd probe in-process with the switch at MODE; -> (rc, printed, curl calls, hub requests)."""
    with open(EGH + "/.config/assistant/egress.json", "w") as fh:
        json.dump({"handles": {"dash": mode}} if mode else {}, fh)
    HUBREQ.clear()
    HUBANS.clear()
    HUBANS.update(answers or {})
    _CURL.clear()
    env = {
        "HOME": EGH,
        "ASSISTANT_TEST": "1",
        "HUB_TOKEN_FILE": EGH + "/.secrets/hub-token",
        "HUB_URL": f"http://127.0.0.1:{_hub_srv.server_address[1] if hub_up else free_port()}",
    }
    saved = {k: os.environ.get(k) for k in env}
    old_run, old_ba = F.subprocess.run, F.BASICAUTH
    os.environ.update(env)
    F.subprocess.run, F.BASICAUTH = _fake_run, EGH + "/.secrets/browser-basicauth.txt"
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = F.cmd_probe()
    finally:
        F.subprocess.run, F.BASICAUTH = old_run, old_ba
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return rc, buf.getvalue(), list(_CURL), list(HUBREQ)


_B64 = base64.b64encode(("probe-fixture:" + BA_PW).encode()).decode()
PUB = "https://" + F.PUBLIC + "/live/health"
# local (no switch file / "local"): today's curl through nginx, byte for byte; the hub hears nothing
for m in (None, "local"):
    rc, out, curl, reqs = probe(m)
    ok(rc == 0 and len(curl) == 1 and not reqs, f"probe {m or 'no switch'}: one curl, no hub call")
    ok(
        curl
        and curl[0][0][:6] == ["curl", "-sS", "-m", "8", "-K", "-"]
        and "--resolve" in curl[0][0]
        and curl[0][0][-1] == PUB
        and curl[0][1] == f'user = "probe-fixture:{BA_PW}"\n',
        f"probe {m or 'no switch'}: the same curl argv and basic-auth config as before",
    )
# hub: no curl, no file read; the placeholder alone in the Basic slot, toward the bound endpoint's URL
os.rename(EGH + "/.secrets/browser-basicauth.txt", EGH + "/.secrets/ba.aside")
fetch_ok = {
    "/egress/fetch": (
        200,
        {
            "status": 200,
            "reason": "OK",
            "headers": [["Content-Type", "application/json"]],
            "body_b64": base64.b64encode(b'{"ok": true}').decode(),
        },
    )
}
rc, out, curl, reqs = probe("hub", fetch_ok)
ok(rc == 0 and not curl and "HTTP 200" in out, "probe hub: 200 through the hub, no curl, without the local file")
ok(
    len(reqs) == 1
    and reqs[0][0] == "/egress/fetch"
    and reqs[0][1].get("handle") == "dash"
    and reqs[0][1].get("method") == "GET"
    and reqs[0][1].get("url") == PUB,
    "probe hub: one fetch, handle dash, GET",
)
ok(
    reqs and ["Authorization", "Basic hub:secret:dash"] in reqs[0][1].get("headers", []),
    'probe hub: Authorization = "Basic hub:secret:dash" (the slot the hub fills)',
)
rc, out, curl, reqs = probe(
    "hub", {"/egress/fetch": (200, {"status": 401, "reason": "Unauthorized", "headers": [], "body_b64": ""})}
)
ok(rc == 1 and not curl and "HTTP 401" in out, "probe hub: an upstream 401 = exit 1, HTTP 401 shown")
rc, out, curl, reqs = probe("hub", {"/egress/fetch": (403, {"error": "credential_endpoint_mismatch"})})
ok(rc == 1 and not curl and "refused" in out and "hub egress status" in out, "probe hub: a refusal = exit 1 + hint")
rc, out, curl, reqs = probe("hub", hub_up=False)
ok(rc == 1 and not curl and "unreachable" in out and "use dash local" in out, "probe hub: hub down = exit 1 + way back")
os.rename(EGH + "/.secrets/ba.aside", EGH + "/.secrets/browser-basicauth.txt")
# shadow: the local curl as before + one check that carries the placeholder, never the value (plain or base64)
rc, out, curl, reqs = probe("shadow", {"/egress/check": (200, {"decision": "allow"})})
ok(rc == 0 and len(curl) == 1 and curl[0][0][-1] == PUB, "probe shadow: the local curl still goes out")
ok(
    len(reqs) == 1
    and reqs[0][0] == "/egress/check"
    and reqs[0][1].get("handle") == "dash"
    and ["Authorization", "Basic hub:secret:dash"] in reqs[0][1].get("headers", []),
    "probe shadow: one check, placeholder",
)
ok(reqs and BA_PW not in json.dumps(reqs) and _B64 not in json.dumps(reqs), "probe shadow: the value never goes along")
rc, out, curl, reqs = probe("shadow", hub_up=False)
ok(rc == 0 and len(curl) == 1, "probe shadow: hub down never fails the local probe")
_hub_srv.shutdown()

shutil.rmtree(T, ignore_errors=True)
print(f"{COUNT[0] - len(FAILS)}/{COUNT[0]} passed" + (f", {len(FAILS)} FAILED" if FAILS else ""))
sys.exit(1 if FAILS else 0)
