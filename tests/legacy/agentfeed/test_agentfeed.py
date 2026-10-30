#!/usr/local/bin/python3.13 -I
"""agentfeed tests: the ONE privacy filter clean() (fixtures with a secret, a vault answer, a path, an IBAN, his
personal data, message quotes, code / command lines; fail closed without a redactor, with a secure fill whose key is
missing, on any error), the transcript Reader (tool names + own descriptions only, ok/error, prose filtered, the result
as a length, tokens/cost, no input/output/prompt text anywhere), the list (fake agent-watch + traces; a renamed agent's
title, still filtered) and the CLI.

Run: tests/legacy/agentfeed/run.sh (~/opt/assistant). Default: the live ~/bin/lib/agentfeed.py;
AGENTFEED_PATH=/copy/agentfeed.py tests a copy (`assistant ci` passes its staged overlay file: then the identical copy
in the would-be lib/ beside it, next to the helpers it imports), AGENTFEED_LIB=/dir a copy in that dir.
Everything runs in a temp dir: fixture secret sources (SECRETSCAN_SOURCES), the synthetic profile, an empty secure-fill
state; nothing reads the real transcripts, secrets or profile."""

import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

_PATH = os.environ.get("AGENTFEED_PATH")
LIBDIR = os.environ.get("AGENTFEED_LIB") or (os.path.dirname(_PATH) if _PATH else os.path.expanduser("~/bin/lib"))
if _PATH and os.path.isfile(os.path.join(LIBDIR, "lib", "agentfeed.py")):
    # the CI overlay: the same rendered copy in its would-be lib/, beside the helpers it imports (ownermask & co.)
    LIBDIR = os.path.join(LIBDIR, "lib")
T = tempfile.mkdtemp(prefix="agentfeed-test-")
FAILS, COUNT = [], [0]


def ok(cond, name):
    COUNT[0] += 1
    if not cond:
        FAILS.append(name)
        print("FAIL", name)


FAKE = "AgentfeedFixture-" + "7c6b5a4f3e2d1c0b9a88"  # a "secret" only inside the fixture sources
SRC, PROJ, SF = (os.path.join(T, d) for d in ("srcs", "projects", "securefill"))
for d in (SRC + "/.secrets", PROJ, SF):
    os.makedirs(d)
with open(SRC + "/.secrets/fixture.json", "w") as fh:
    json.dump({"api_key": FAKE}, fh)
for _pf in ("profile", "profile-tests"):
    _pf = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), _pf, "fixtures", "profile.json")
    if os.path.isfile(_pf):
        os.environ["ASSISTANT_PROFILE"] = _pf
        break
PROFILE = json.load(open(os.environ["ASSISTANT_PROFILE"])) if os.environ.get("ASSISTANT_PROFILE") else {}
os.environ.update(
    SECRETSCAN_SOURCES=SRC,
    SECUREFILL_STATE=SF,
    AGENTFEED_PROJECTS=PROJ,
    AGENTFEED_SECRETSCAN_DIR=os.path.expanduser("~/bin"),
)
sys.path.insert(0, LIBDIR)
AF = importlib.import_module("agentfeed")
clean = AF.clean

# ---------------------------------------------------------------- 1. the filter: the four required fixtures
r = clean(f"Calling the API with {FAKE} now, then checking the reply code.")
ok(r is None or FAKE not in r, "secret: the fixture secret never comes through")
ok(
    clean(f"export API_KEY={FAKE}") is None or FAKE not in (clean(f"export API_KEY={FAKE}") or ""),
    "secret: key=value form",
)
for t in (
    "The vault says the landlord confirmed the move for Friday afternoon.",
    "Vault answer: she arrives at 18:00 with two friends.",
    "vault ask returned that the contract ends in March.",
    "According to tgread the group decided on Saturday.",
):
    ok(clean(t) is None, f"vault answer dropped: {t[:40]}")
r = clean("Patched /home/node/bin/flowd and ~/opt/dash/index.html, then bin/lib/agentfeed.py for the new tab.")
ok(
    r is not None and "/home/node" not in r and "~/opt" not in r and "bin/lib" not in r and r.count("[path]") == 3,
    f"path: masked, the line kept ({r!r})",
)
# fake PII is built at runtime (the codebase precheck scans ~/opt for literal IBANs, cards, IPs, phones, e-mails)
_ib1 = " ".join(["CH93", "0076", "2011", "6238", "5295", "7"])
_ib2 = " ".join(["DE89", "3704", "0044", "0532", "0130", "00"])
for iban in (_ib1, _ib1.replace(" ", ""), _ib2):
    r = clean(f"Paid the invoice to {iban} yesterday morning.")
    ok(
        r is None or (iban not in r and iban.replace(" ", "")[4:12] not in r.replace(" ", "")),
        f"IBAN masked ({iban[:4]})",
    )
r = clean(f"The account {_ib1} is set as the default for the transfer setup.")
ok(r is None or "0076" not in r, "IBAN: digits gone")

# ---------------------------------------------------------------- 2. the rest of the policy
own = PROFILE.get("owner", {})
if own.get("first_name") and own.get("email"):
    full = f"{own['first_name']} {own['last_name']}"
    r = clean(f"Filled the form for {full}, reachable at {own['email']} or {own['phone']}, all good now.")
    ok(
        r is None or (own["last_name"] not in r and own["email"] not in r and own["phone"][-5:] not in r),
        f"personal data: name, e-mail, phone masked ({r!r})",
    )
home = PROFILE.get("home", {})
if home.get("street"):
    r = clean(f"Delivery goes to {home['street']}, {home.get('postcode', '')} {home.get('town', '')} as planned.")
    ok(r is None or home["street"] not in r, "personal data: street masked")
for t in (
    "> are you coming tonight?",
    "Anna wrote that she is late",
    "He said he will pay later.",
    'Sam: "I will be there at eight tonight, keep a seat for me please"',
    "Checked the mail from the landlord about the deposit.",
    "The chat shows two new plans for Sunday.",
    "New message in the family group about the visit.",
    "Sie schrieb, dass sie später kommt.",
    "| name | amount |",
    "password reset for the shop account went fine",
):
    ok(clean(t) is None, f"quote / mail / chat line dropped: {t[:36]}")
r = clean(
    'Running it now:\n```\nrm -rf /tmp/x && curl -H "Authorization: Bearer abc" https://x.example/y\n```\n'
    "Done with the cleanup step."
)
ok(r == "Running it now:\nDone with the cleanup step.", f"code block dropped, prose kept ({r!r})")
r = clean("Ran `git status --short` and `agent-watch` to check the state.")
ok(r == "Ran [code] and agent-watch to check the state.", f"inline code: a command masked, a bare name kept ({r!r})")
r = clean("See https://example.com/booking?id=123 and www.example.org/x for the details page.")
ok(r is not None and "example" not in r and r.count("[link]") == 2, f"links masked ({r!r})")
_mail = "@".join(["someone", "example.net"])
r = clean(f"Mailed {_mail} about it.")
ok(r is None, "a line about mail is dropped")
r = clean(f"Contact point is {_mail} for the next step.")
ok(r is not None and "example.net" not in r, f"e-mail masked ({r!r})")
_ph1, _ph2 = " ".join(["+41", "79", "123", "45", "67"]), " ".join(["079", "123", "45", "67"])
r = clean(f"Call {_ph1} or {_ph2} to confirm the slot.")
ok(r is not None and "123 45" not in r, f"phone numbers masked ({r!r})")
r = clean("The total came to CHF 1'240.50 and 300 EUR for the second part.")
ok(r is not None and "240" not in r and "300" not in r, f"amounts masked ({r!r})")
_ip = ".".join(["192", "168", "1", "20"])
_card = " ".join(["4111"] + ["1111"] * 3)
_ahv = ".".join(["756", "1234", "5678", "97"])
r = clean(f"The server at {_ip} answered, card {_card} declined, AHV {_ahv} ok.")
ok(r is not None and "192.168" not in r and "4111" not in r and "756.1234" not in r, f"IP, card, AHV masked ({r!r})")
r = clean("Session 70160b71-d076-47b3-8a72-318ec26261eb and token-ish aGVsbG8gd29ybGQgdGhpcyBpcyBsb25n here.")
ok(r is not None and "70160b71" not in r and "aGVsbG8" not in r, f"ids masked ({r!r})")
ok(
    clean("Now wiring the agents list into the page.") == "Now wiring the agents list into the page.",
    "plain prose kept as is",
)
ok(
    clean("Tests pass: 42 of 42, dates like 2026-10-28 and 16:05 stay.")
    == "Tests pass: 42 of 42, dates like 2026-10-28 and 16:05 stay.",
    "small numbers, dates and times stay",
)
r = clean("\n".join(f"Step {i} of the build is done and checked." for i in range(9)))
ok(r is not None and r.count("\n") == 2, "at most 3 lines")
r = clean("word " * 120)
ok(r is not None and len(r) <= AF.MAX_LINE and r.endswith("…"), "a long line is cut")
ok(clean("[path] [link]") is None and clean("/home/node/x") is None, "a line with nothing but masks is dropped")
ok(clean("") is None and clean(None) is None and clean(42) is None, "empty / non-text -> None")

# ---------------------------------------------------------------- 3. fail closed
ss, sf, om = AF._mods()
orig = om.mask
om.mask = lambda t: (_ for _ in ()).throw(RuntimeError("boom"))
ok(clean("Now wiring the agents list into the page.") is None, "fail closed: a masker error drops the text")
om.mask = orig
orig_live, orig_key = sf.live, sf._fp_key
sf.live = lambda task=None: {"given": {"x": 1}}
sf._fp_key = lambda: None
ok(clean("Now wiring the agents list into the page.") is None, "fail closed: a live secure fill without its key")
sf.live, sf._fp_key = orig_live, orig_key
ok(clean("Now wiring the agents list into the page.") is not None, "back to normal after the fault")
empty = os.path.join(T, "noss")
os.makedirs(empty)
env = dict(os.environ, AGENTFEED_SECRETSCAN_DIR=empty, PYTHONPATH="")
lib_copy = os.path.join(T, "lib")
os.makedirs(lib_copy)
shutil.copy(os.path.join(LIBDIR, "agentfeed.py"), lib_copy)  # alone: no secretscan, securefill or ownermask next to it
p = subprocess.run(
    [sys.executable, "-I", os.path.join(lib_copy, "agentfeed.py"), "clean"],
    input="Plain words here, fine.",
    capture_output=True,
    text=True,
    env=env,
    timeout=30,
)
ok(
    p.returncode == 1 and p.stdout == "",
    f"fail closed: without the redactors nothing is printed ({p.returncode} {p.stderr[-200:]})",
)
p = subprocess.run(
    [sys.executable, "-I", os.path.join(LIBDIR, "agentfeed.py"), "clean"],
    input="Plain words here, fine.",
    capture_output=True,
    text=True,
    timeout=30,
)
ok(p.returncode == 0 and p.stdout.strip() == "Plain words here, fine.", "CLI clean: kept text, exit 0")

# ---------------------------------------------------------------- 4. the transcript reader
SESS = "aaaabbbb-0000-1111-2222-333344445555"
SUB = os.path.join(PROJ, SESS, "subagents")
os.makedirs(SUB)
AID = "a0123456789abcdef"
PROMPT = "ZZPROMPT build the thing with " + FAKE
CMD = f'curl -H "x-key: {FAKE}" https://ZZURL.example/api && cat /home/node/ZZPATH'
OUT = "ZZOUTPUT the vault replied that Anna is pregnant " + FAKE
RESULT = "ZZRESULT the final report with all the details"


def rec(kind, content, t, **m):
    msg = dict(role=kind, content=content, **m)
    return {"type": kind, "timestamp": f"2026-10-28T10:00:{t:02d}.000Z", "message": msg, "sessionId": SESS}


U = {"input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 100}
recs = [
    rec("user", PROMPT, 0),
    rec(
        "assistant",
        [{"type": "thinking", "thinking": "ZZTHINK secret plan"}],
        1,
        id="m1",
        model="claude-opus-5-5",
        stop_reason="tool_use",
        usage=U,
    ),
    rec(
        "assistant",
        [{"type": "text", "text": "Checking the API first.\nThe vault says ZZVAULTLINE"}],
        1,
        id="m1",
        model="claude-opus-5-5",
        stop_reason="tool_use",
        usage=U,
    ),
    rec(
        "assistant",
        [{"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": CMD, "description": "Probe the API"}}],
        1,
        id="m1",
        model="claude-opus-5-5",
        stop_reason="tool_use",
        usage=U,
    ),
    rec("user", [{"type": "tool_result", "tool_use_id": "tu1", "content": OUT, "is_error": True}], 2),
    rec(
        "assistant",
        [{"type": "tool_use", "id": "tu2", "name": "Read", "input": {"file_path": "/home/node/ZZREAD.md"}}],
        3,
        id="m2",
        model="claude-opus-5-5",
        stop_reason="tool_use",
        usage=U,
    ),
    rec("user", [{"type": "tool_result", "tool_use_id": "tu2", "content": "ZZFILECONTENT"}], 4),
    rec(
        "assistant",
        [{"type": "tool_use", "id": "tu3", "name": "mcp__hub__vault_ask", "input": {"q": "ZZVAULTQ"}}],
        5,
        id="m3",
        model="claude-opus-5-5",
        stop_reason="tool_use",
        usage=U,
    ),
    rec("user", [{"type": "tool_result", "tool_use_id": "tu3", "content": "ZZVAULTANSWER"}], 6),
    rec(
        "assistant",
        [
            {
                "type": "tool_use",
                "id": "tu4",
                "name": "Bash",
                "input": {"command": "ZZCMD2", "description": "Ask vault about ZZDESCV", "run_in_background": True},
            }
        ],
        7,
        id="m4",
        model="claude-opus-5-5",
        stop_reason="tool_use",
        usage=U,
    ),
    rec(
        "assistant",
        [
            {
                "type": "tool_use",
                "id": "tu5",
                "name": "Agent",
                "input": {"description": "Check /home/node/ZZAGENTPATH layout", "prompt": "ZZAGENTPROMPT"},
            }
        ],
        8,
        id="m5",
        model="claude-opus-5-5",
        stop_reason="tool_use",
        usage=U,
    ),
    rec(
        "assistant",
        [
            {
                "type": "tool_use",
                "id": "tu6",
                "name": "mcp__plugin_telegram-plus_telegram__reply",
                "input": {"text": "ZZTGTEXT"},
            }
        ],
        9,
        id="m6",
        model="claude-opus-5-5",
        stop_reason="tool_use",
        usage=U,
    ),
    rec(
        "assistant",
        [{"type": "text", "text": RESULT}],
        10,
        id="m7",
        model="claude-opus-5-5",
        stop_reason="end_turn",
        usage=U,
    ),
]
path = os.path.join(SUB, f"agent-{AID}.jsonl")
with open(path, "w") as fh:
    for r_ in recs[:5]:
        fh.write(json.dumps(r_) + "\n")
    fh.write('{"type":"api-request-blob","message":{"content":[{"type":"text","text":"ZZBLOB"}]}}\n')
rd = AF.Reader(path)
evs = rd.read()
with open(path, "a") as fh:
    for r_ in recs[5:]:
        fh.write(json.dumps(r_) + "\n")
    fh.write(
        '{"type":"assistant","message":{"content":[{"type":"text","text":"ZZPARTIAL'
    )  # a record still being written
evs += rd.read()
dump = json.dumps(evs)
for z in (
    "ZZPROMPT",
    "ZZTHINK",
    "ZZVAULTLINE",
    "ZZURL",
    "ZZPATH",
    "ZZOUTPUT",
    "ZZREAD",
    "ZZFILECONTENT",
    "ZZVAULTQ",
    "ZZVAULTANSWER",
    "ZZCMD2",
    "ZZDESCV",
    "ZZAGENTPATH",
    "ZZAGENTPROMPT",
    "ZZTGTEXT",
    "ZZRESULT",
    "ZZBLOB",
    "ZZPARTIAL",
    FAKE,
    "curl",
    "/home/node",
):
    ok(z not in dump, f"reader: {z[:14]} never in the events")
kinds = [e["k"] for e in evs]
ok(kinds[0] == "start" and kinds[-1] == "end", f"reader: start first, end last ({kinds})")
tools = [(e["n"], e.get("d")) for e in evs if e["k"] == "tool"]
ok(
    tools
    == [
        ("Bash", "Probe the API"),
        ("Read", "read a file"),
        ("hub.vault_ask", None),
        ("Bash", "shell command (background)"),
        ("Agent", "Check [path] layout"),
        ("telegram.reply", None),
    ],
    f"reader: tool names + own descriptions only ({tools})",
)
says = [e["x"] for e in evs if e["k"] == "say"]
ok(says == ["Checking the API first."], f"reader: prose filtered line by line ({says})")
end = [e for e in evs if e["k"] == "end"][0]
ok(end["len"] == len(RESULT) and "x" not in end, "reader: the result is a length only")
res = {e["ref"]: e["ok"] for e in evs if e["k"] == "res"}
m = AF.merged(evs)
st = [(x["n"], x.get("ok")) for x in m if x["k"] == "tool"]
ok(
    st[0] == ("Bash", False) and st[1] == ("Read", True) and st[3][1] is None and rd.errors == 1,
    f"reader: ok / error from is_error, pending without a result ({st})",
)
ok(
    rd.tools == 6 and rd.tokens() == 7 * 1130 and rd.cost() > 0 and rd.model == "claude-opus-5-5",
    f"reader: counts, tokens per message id ({rd.tools} {rd.tokens()} {rd.cost():.4f})",
)
ok(
    AF.step_line(rd.last) == f"finished (result {len(RESULT)} chars, not shown)",
    "reader: doing-now line of a finished agent",
)
ok(
    AF.tool_label("mcp__claude_ai_Google_Calendar__list_events", {})[0] == "google_calendar.list_events"
    and AF.tool_label("mcp__browser__browser_click", {"ref": "x"}) == ("browser.click", None)
    and AF.tool_label("Skill", {"skill": "browser"}) == ("Skill", "use skill browser")
    and AF.tool_label("Skill", {"skill": "../x y"}) == ("Skill", "use a skill")
    and AF.tool_label("Grep", {"pattern": "ZZ"}) == ("Grep", "search files"),
    "tool labels",
)

# main session: user records are kinds, never text
MAINP = os.path.join(PROJ, SESS + ".jsonl")
with open(MAINP, "w") as fh:
    for r_ in (
        rec("user", '<channel source="telegram" chat_id="1">ZZOWNER hi</channel>', 20),
        rec(
            "assistant",
            [{"type": "tool_use", "id": "x1", "name": "Bash", "input": {"command": "ls", "description": "List files"}}],
            21,
            id="q1",
            stop_reason="tool_use",
        ),
        rec("user", "[heartbeat] ZZHB", 22),
        rec(
            "user",
            "<task-notification><task-id>a1</task-id><status>completed</status><result>ZZNOTE</result></task-notification>",
            23,
        ),
        rec("assistant", [{"type": "text", "text": "ZZMAINEND reply"}], 24, id="q2", stop_reason="end_turn"),
    ):
        fh.write(json.dumps(r_) + "\n")
mr, mev = AF.steps("main")
d = json.dumps(mev)
ok(
    all(z not in d for z in ("ZZOWNER", "ZZHB", "ZZNOTE", "ZZMAINEND")),
    "main: owner text, heartbeat, notification, reply never shown",
)
ok(
    [e.get("x") for e in mev if e["k"] == "in"] == ["Telegram message", "heartbeat", "agent update"],
    "main: user records as kinds",
)

# ---------------------------------------------------------------- 5. the list (fake agent-watch + traces)
AW = os.path.join(T, "aw")
TR = os.path.join(T, "tr")
rows = {
    "now": 1793210000,
    "hours": 6,
    "agents": [
        {
            "id": AID,
            "session": SESS,
            "status": "done",
            "kind": "final",
            "start": 1793200000,
            "last": 1793200010,
            "description": "Check the vault for ZZTITLE",
            "type": "general-purpose",
            "effort": "xhigh",
            "depth": 1,
            "parent": None,
        },
        {
            "id": "a99999999999999999",
            "session": SESS,
            "status": "running",
            "kind": "tool_use",
            "start": 1,
            "last": 1,
            "description": "missing transcript",
            "type": None,
            "effort": None,
            "depth": 1,
            "parent": None,
        },
        {
            "id": "../../etc",
            "session": SESS,
            "status": "running",
            "kind": "x",
            "start": 1,
            "last": 1,
            "description": "x",
        },
    ],
}
with open(AW, "w") as fh:
    fh.write("#!/bin/sh\ncat <<'EOF'\n" + json.dumps(rows) + "\nEOF\n")
with open(TR, "w") as fh:
    fh.write(
        "#!/bin/sh\necho '"
        + json.dumps([{"id": AID, "trigger": "owner", "trigger_kind": "message", "model": "claude-opus-5-5"}])
        + "'\n"
    )
os.chmod(AW, 0o755)
os.chmod(TR, 0o755)
AF.AW_BIN, AF.TRACES_BIN = AW, TR
snap = AF.snapshot(6, now=1793210000)
a = snap["agents"][0] if snap["agents"] else {}
ok(
    len(snap["agents"]) == 1 and a.get("id") == AID,
    f"list: bad ids and missing transcripts skipped ({len(snap['agents'])})",
)
ok(
    a.get("title") == "agent " + AID[:8] and "ZZTITLE" not in json.dumps(snap),
    "list: a title the filter drops -> agent <id>",
)
ok(
    a.get("tools") == 6
    and a.get("errors") == 1
    and a.get("model") == "Opus 5.5"
    and a.get("effort") == "xhigh"
    and a.get("trigger") == "owner · message"
    and a.get("result_len") == len(RESULT)
    and a.get("runtime_s") == 10,
    f"list: counts, model, effort, trigger, result length, runtime ({a})",
)
ok(
    snap["main"] and snap["main"]["trigger"] == "agent update" and snap["main"]["now"].startswith("finished"),
    f"list: main row, the newest turn trigger ({snap['main']})",
)
readers = {}
AF.snapshot(6, readers=readers, now=1793210000)
ok(path in readers and MAINP in readers, "list: readers kept between calls")


def listed(**kw):
    """(title, the whole snapshot as JSON) for the agent row with these fields changed (title=None: no title key)."""
    row = dict(rows["agents"][0], **kw)
    if row.get("title") is None:
        row.pop("title", None)
    with open(AW, "w") as fh:
        fh.write("#!/bin/sh\ncat <<'EOF'\n" + json.dumps(dict(rows, agents=[row])) + "\nEOF\n")
    s = AF.snapshot(6, now=1793210000)
    return (s["agents"][0]["title"] if s["agents"] else None), json.dumps(s)


# a renamed agent (tg-status title; agent-watch --json "title") shows its title, still through clean()
t, js = listed(title="Disk check", description="Check the vault for ZZTITLE")
ok(t == "Disk check" and "ZZTITLE" not in js, f"title: the override wins over the description ({t!r})")
t, js = listed(title="Disk check", description="Wire the agents tab")
ok(t == "Disk check" and "Wire the agents" not in js, f"title: shown instead of the spawn description ({t!r})")
t, _ = listed(title="Wire the agents tab", description="Wire the agents tab")
ok(t == "Wire the agents tab", f"title: not renamed (title = description) -> the description ({t!r})")
t, _ = listed(title=None, description="Wire the agents tab")
ok(t == "Wire the agents tab", f"title: no title field (older agent-watch) -> the description ({t!r})")
t, _ = listed(title="", description="Wire the agents tab")
ok(t == "Wire the agents tab", f"title: empty title -> the description ({t!r})")
t, js = listed(title=f"Rotate {FAKE} now", description="Wire the agents tab")
ok(FAKE not in js, f"title: a secret in the title never comes through ({t!r})")
t, js = listed(title="Fix /home/node/bin/flowd and the tab", description="Wire the agents tab")
ok(t is not None and "[path]" in t and "/home/node" not in js, f"title: a path in the title masked ({t!r})")
t, js = listed(title="Mail triage ZZOVR", description="Wire the agents tab")
ok(t == "Wire the agents tab" and "ZZOVR" not in js, f"title: one the filter drops -> the description ({t!r})")
t, js = listed(title="Vault answer ZZOVR", description="Check the vault for ZZTITLE")
ok(t == "agent " + AID[:8] and "ZZOVR" not in js and "ZZTITLE" not in js, f"title: both dropped -> agent <id> ({t!r})")
t, js = listed(title="Disk check\nsecond line ZZ2ND", description="Wire the agents tab")
ok(t == "Disk check" and "ZZ2ND" not in js, f"title: one line only ({t!r})")

with open(AW, "w") as fh:
    fh.write("#!/bin/sh\nexit 1\n")
try:
    AF.snapshot(6, now=1793210000)
    ok(False, "list: agent-watch failure raises")
except RuntimeError:
    ok(True, "list: agent-watch failure raises")

shutil.rmtree(T, ignore_errors=True)
print(f"{COUNT[0] - len(FAILS)} passed, {len(FAILS)} failed")
sys.exit(1 if FAILS else 0)
