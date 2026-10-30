#!/usr/bin/python3 -I
"""Offline tests for ~/bin/evals and ~/bin/lib/evals (no model calls, no sudo, no network).
EVALS_BIN=/copy/of/evals tests a copy (its lib/evals next to it, else ~/bin/lib/evals)."""
import json
import os
import pwd
import shutil
import socket
import subprocess
import sys
import tempfile

TREE = os.environ.get("ASSISTANT_CI_TREE")  # `assistant ci`: the tree's script + lib when no EVALS_BIN seam is set
BIN = os.environ.get("EVALS_BIN") or (os.path.join(TREE, "scripts", "evals") if TREE else os.path.expanduser("~/bin/evals"))
LIB = os.environ.get("EVALS_LIB") or os.path.join(os.path.dirname(os.path.realpath(BIN)), "lib", "evals")
if not os.path.isdir(LIB) and TREE:
    LIB = os.path.join(TREE, "scripts", "lib", "evals")
if not os.path.isdir(LIB):
    LIB = os.path.expanduser("~/bin/lib/evals")
os.environ["EVALS_LIB"] = LIB
T = tempfile.mkdtemp(prefix="evals-tests-")
FAILS = []


def ip(*parts):
    return ".".join(map(str, parts))


def check(name, cond, info=""):
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else f"  [{str(info)[:300]}]"))
    if not cond:
        FAILS.append(name)


def load_engine():
    os.environ.update({"EVALS_NO_SUDO": "1", "EVALS_STATE": f"{T}/state", "EVALS_JOBS": f"{T}/jobs",
                       "EVALS_REPORTS": f"{T}/reports", "EVALS_CORPUS": f"{T}/corpus", "USAGE": "off"})
    ns = {"__file__": BIN, "__name__": "evals_under_test"}
    exec(compile(open(BIN).read(), BIN, "exec"), ns)
    return ns


def sandbox(case):
    H = os.path.join(T, "real", "jobs", "r", "c", "home")
    if os.path.exists(H):
        shutil.rmtree(H)
    os.makedirs(os.path.join(H, ".eval", "help"))
    os.makedirs(os.path.join(H, "bin"))
    shutil.copy2(os.path.join(LIB, "evalstub.py"), os.path.join(H, "bin", "_evalstub"))
    os.chmod(os.path.join(H, "bin", "_evalstub"), 0o755)
    for n in ("remind", "ha", "tripclock", "vault", "tg"):
        os.symlink("_evalstub", os.path.join(H, "bin", n))
    with open(os.path.join(H, ".eval", "help", "remind.txt"), "w") as f:
        f.write("Durable reminders (usage header)\n")
    json.dump(case, open(os.path.join(H, ".eval", "case.json"), "w"))
    return H


def run(H, *cmd, inp=None):
    env = {"HOME": H, "PATH": f"{H}/bin:/usr/bin:/bin", "LANG": "C.UTF-8"}
    return subprocess.run(list(cmd), env=env, capture_output=True, text=True, input=inp, timeout=30)


# ------------------------------------------------------------ stub dispatcher
H = sandbox({"clock": "2030-01-02T03:04:00+01:00", "state": {"remind": ["r1111 · 2030-01-05 08:00 · seeded item"]},
             "stubs": [{"cmd": "ha", "match": "^state", "out": "light.x on", "exit": 0}]})
r = run(H, "remind", "add", "2030-01-03 09:00", "water the plants")
check("stub: remind add answers with the time", r.returncode == 0 and "2030-01-03 09:00" in r.stdout, r)
r = run(H, "remind", "list")
check("stub: remind list shows seeded + added items", "seeded item" in r.stdout and "water the plants" in r.stdout, r.stdout)
run(H, "remind", "rm", "r1111")
check("stub: remind rm removes by id", "seeded item" not in run(H, "remind", "list").stdout)
check("stub: canned rule wins", run(H, "ha", "state", "light.x").stdout.strip() == "light.x on")
check("stub: --help shows the real header", "Durable reminders" in run(H, "remind", "--help").stdout)
check("stub: tripclock uses the case clock", run(H, "tripclock", "now").stdout.startswith("2030-01-02 03:04"))
r = run(H, "vault", "ask", "tg", "--text", "--reason", "he asked")
check("stub: vault --text exits 10 (approval pending)", r.returncode == 10, r)
calls = [json.loads(l) for l in open(os.path.join(H, ".eval", "stubs.jsonl"))]
check("stub: every call recorded", len(calls) == 8 and calls[0]["cmd"] == "remind" and calls[0]["args"][0] == "add", len(calls))
r = run(H, "tg", "hello", inp="piped text")
last = json.loads(open(os.path.join(H, ".eval", "stubs.jsonl")).read().splitlines()[-1])
check("stub: stdin recorded", last.get("stdin") == "piped text", last)

# a rule with "after" applies only once an earlier call's full line matched (a status that changes after resume)
_HA = os.path.join(T, "after", "home")
os.makedirs(os.path.join(_HA, ".eval"))
os.makedirs(os.path.join(_HA, "bin"))
shutil.copy2(os.path.join(LIB, "evalstub.py"), os.path.join(_HA, "bin", "_evalstub"))
os.chmod(os.path.join(_HA, "bin", "_evalstub"), 0o755)
os.symlink("_evalstub", os.path.join(_HA, "bin", "webagent"))
json.dump({"stubs": [{"cmd": "webagent", "match": "^resume", "out": "w12: order placed"},
                     {"cmd": "webagent", "match": "^status", "after": r"^webagent resume\b", "out": "w12 done"},
                     {"cmd": "webagent", "match": "^status", "out": "w12 waiting"}]},
          open(os.path.join(_HA, ".eval", "case.json"), "w"))
check("stub: an after-rule waits for its earlier call", run(_HA, "webagent", "status", "w12").stdout.strip() == "w12 waiting")
run(_HA, "webagent", "resume", "w12")
check("stub: an after-rule applies once the earlier call ran", run(_HA, "webagent", "status", "w12").stdout.strip() == "w12 done")

# the hub's typed tools parse the real CLIs' output shapes (v2 H8/H9): remind, vault jobs, phone/ha --json
for n in ("phone", "approvals"):
    os.symlink("_evalstub", os.path.join(H, "bin", n))
r = run(H, "remind", "add", "--owner", "--via", "telegram", "2030-01-04 10:00", "dry cleaning")
check("stub: remind add prints the CLI's 'added' line", r.stdout.startswith("added owner reminder for Fri 2030-01-04 10:00"), r.stdout)
d = json.loads(run(H, "remind", "list", "--json").stdout)
it = [i for i in d["items"] if i["text"] == "dry cleaning"]
check("stub: remind list --json items with 8-hex ids", it and len(it[0]["id"]) == 8 and it[0]["kind"] == "timed"
      and it[0]["at"] == "2030-01-04 10:00", d)
r = run(H, "remind", "cancel", it[0]["id"] if it else "x")
check("stub: remind cancel prints 'cancelled:' lines", r.stdout.startswith("cancelled: 2030-01-04 10:00  dry cleaning"), r.stdout)
check("stub: remind cancel of an unknown id fails", run(H, "remind", "cancel", "ffffffff").returncode == 1)
jid = run(H, "vault", "--async", "ask", "tg", "--search", "x", "did he confirm?").stdout.strip()
j = json.loads(run(H, "vault", "job", "wait", jid, "--max", "20", "--json").stdout or "{}")
check("stub: vault --async gives a job, job wait the default answer", len(jid) == 16 and j.get("state") == "done"
      and "nothing clear" in j.get("result", {}).get("answer", ""), (jid, j))
check("stub: phone push --json acks", json.loads(run(H, "phone", "push", "--json", "--title=x").stdout)["status"] == "acked")
check("stub: ha read --json answers []", run(H, "ha", "find", "lamp", "--json").stdout.strip() == "[]")
check("stub: approvals consume refuses without his decision", run(H, "approvals", "consume", "ab12cd34", "--action", "a").returncode == 5)

# memory set|decide|supersede|show: the sandbox FACTS.md / FACTS-core.md change like the real CLI's render, the
# report names the row ids and line numbers (no read-back needed), one ledger-shaped string works for decide
os.symlink("_evalstub", os.path.join(H, "bin", "memory"))
os.makedirs(os.path.join(H, "workspace"), exist_ok=True)
_facts = ("# Facts\n\n## Network & TV\n- Living room: LG C7 + Streamer — [[tv]]\n- Pi-hole VM\n\n"
          "## Done / declined ledger\nNewest first.\n- 01.01 done: old thing\n")
_core = ("# Facts core\n\n## Network & TV\n- Living room: LG C7 + Streamer — [[tv]]\n\n## Done / declined ledger\n"
         "Newest first.\n- 01.01 done: old thing\n")
open(os.path.join(H, "workspace", "FACTS.md"), "w").write(_facts)
open(os.path.join(H, "workspace", "FACTS-core.md"), "w").write(_core)
rf = lambda n: open(os.path.join(H, "workspace", n)).read()
old_id = run(H, "memory", "show", "--grep", "LG C7").stdout.split()[0]
r = run(H, "memory", "supersede", old_id, "Living room: LG C9 + Streamer", "--src", "live")
new_id = r.stdout.split("new row ")[1].split()[0] if "new row " in r.stdout else ""
check("stub memory: supersede rewrites both files and reports ids + line numbers",
      r.returncode == 0 and "- Living room: LG C9 + Streamer — live, 02.01" in rf("FACTS.md")
      and "LG C7" not in rf("FACTS.md") and "LG C9" in rf("FACTS-core.md")
      and f"replaces {old_id}" in r.stdout and "FACTS.md line 4 (Network & TV) now reads:" in r.stdout
      and "FACTS-core.md line 4 (re-rendered): the same line" in r.stdout and "no read-back needed" in r.stdout, r.stdout)
r = run(H, "memory", "show", "--grep", "LG C9")
check("stub memory: show finds the new row under its id", r.stdout.startswith(new_id) and "LG C9" in r.stdout, r.stdout)
r = run(H, "memory", "show", old_id)
check("stub memory: show OLD_ID gives its history and the new row", "[superseded]" in r.stdout and new_id in r.stdout, r.stdout)
r = run(H, "memory", "supersede", old_id, "again")
check("stub memory: superseding an old id is refused like the real CLI", r.returncode == 3 and "superseded" in r.stdout, r)
r = run(H, "memory", "decide", "21.10 declined: Ryanair flights (never again, any trip)")
check("stub memory: decide takes one ledger-shaped string",
      r.returncode == 0 and "Newest first.\n- 21.10 declined: Ryanair flights (never again, any trip)\n- 01.01" in rf("FACTS.md")
      and "- 21.10 declined: Ryanair" in rf("FACTS-core.md") and "FACTS.md line 9 (Done / declined ledger)" in r.stdout, r.stdout)
r = run(H, "memory", "decide", "declined", "Wizz Air", "--details", "same", "--date", "20.10")
check("stub memory: decide VERB THING still works", r.returncode == 0 and "- 20.10 declined: Wizz Air (same)" in rf("FACTS.md"), r)
r = run(H, "memory", "decide", "no Ryanair ever")
check("stub memory: decide garbage = usage error with an example", r.returncode == 2
      and 'memory decide "21.10 declined: Ryanair flights (never again)"' in r.stdout, r)
r = run(H, "memory", "decide", "maybe", "thing")
check("stub memory: decide with a bad verb is refused (exit 3)", r.returncode == 3, r)
before = rf("FACTS.md")
r = run(H, "memory", "set", "--section", "network", "Switch is 1 Gbit", "--src", "live", "--dry-run")
check("stub memory: --dry-run writes nothing", rf("FACTS.md") == before and "would read" in r.stdout, r.stdout)
r = run(H, "memory", "set", "--section", "network", "Switch is 1 Gbit", "--src", "live, 21.10")
check("stub memory: set appends to the section (a dated --src keeps its date)",
      "- Pi-hole VM\n- Switch is 1 Gbit — live, 21.10\n\n## Done" in rf("FACTS.md") and "FACTS.md line 6" in r.stdout, r.stdout)
check("stub memory: set without --src is refused", run(H, "memory", "set", "--section", "network", "x").returncode == 3)
r = run(H, "memory", "show", "--grep", "nothing like this")
check("stub memory: show with no match says so", "no active rows match" in r.stdout, r.stdout)

# ------------------------------------------------------------ fake MCP
reqs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "reply", "arguments": {"chat_id": "1", "text": "hi"}}}]
r = run(H, "/usr/bin/python3", "-I", os.path.join(LIB, "fake_mcp.py"), "telegram", inp="\n".join(json.dumps(x) for x in reqs) + "\n")
resp = [json.loads(l) for l in r.stdout.splitlines() if l.strip()]
check("mcp: three responses (notification unanswered)", len(resp) == 3, r.stdout[:300])
check("mcp: telegram instructions served", "prompt injection" in resp[0]["result"].get("instructions", ""))
names = {t["name"] for t in resp[1]["result"]["tools"]}
check("mcp: production tool names", {"reply", "react", "send_poll", "delete_message"} <= names, names)
check("mcp: reply answered", "sent" in resp[2]["result"]["content"][0]["text"])
rec = [json.loads(l) for l in open(os.path.join(H, ".eval", "mcp.jsonl"))]
check("mcp: call recorded", rec and rec[-1]["tool"] == "reply" and rec[-1]["input"]["text"] == "hi", rec)
json.dump({"calendar": [{"id": "a", "summary": "A", "start": "2030-01-02T10:00:00+01:00", "end": "2030-01-02T11:00:00+01:00"},
                        {"id": "b", "summary": "B", "start": "2030-02-02T10:00:00+01:00"}]},
          open(os.path.join(H, ".eval", "case.json"), "w"))
reqs = [{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "list_events", "arguments":
         {"timeMin": "2030-01-02T00:00:00+01:00", "timeMax": "2030-01-03T00:00:00+01:00"}}}]
r = run(H, "/usr/bin/python3", "-I", os.path.join(LIB, "fake_mcp.py"), "calendar", inp=json.dumps(reqs[0]) + "\n")
evs = json.loads(json.loads(r.stdout)["result"]["content"][0]["text"])["events"]
check("mcp: calendar window filter", [e["id"] for e in evs] == ["a"], evs)


def cal(name, args):
    r = run(H, "/usr/bin/python3", "-I", os.path.join(LIB, "fake_mcp.py"), "calendar",
            inp=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}) + "\n")
    res = json.loads(r.stdout)["result"]
    return json.loads(res["content"][0]["text"]) if not res["isError"] else {"error": res["content"][0]["text"]}


W = {"timeMin": "2030-01-02T00:00:00+01:00", "timeMax": "2030-01-03T00:00:00+01:00"}
new = cal("create_event", {"summary": "Dinner", "start": {"dateTime": "2030-01-02T19:30:00+01:00"},
                           "end": {"dateTime": "2030-01-02T21:30:00+01:00"}, "location": "Bellini"})
check("mcp: create_event returns the full event", new.get("id") == "evt_new_1" and new.get("location") == "Bellini", new)
ids = [e["id"] for e in cal("list_events", W)["events"]]
check("mcp: created event listed (separate server process)", ids == ["a", "evt_new_1"], ids)
cal("create_event", {"summary": "Train", "start": "2030-01-02T18:40:00+01:00", "end": "2030-01-02T19:20:00+01:00"})
ids = [e["id"] for e in cal("list_events", W)["events"]]
check("mcp: second create gets a new id, list sorted by start", ids == ["a", "evt_new_2", "evt_new_1"], ids)
cal("update_event", {"eventId": "evt_new_1", "summary": "Dinner with M"})
check("mcp: update_event persists", cal("get_event", {"eventId": "evt_new_1"}).get("summary") == "Dinner with M")
cal("delete_event", {"eventId": "evt_new_2"})
ids = [e["id"] for e in cal("search_events", {"q": "dinner"})["events"]]
check("mcp: delete + search see the state", ids == ["evt_new_1"], ids)
check("mcp: unknown event is an error", "error" in cal("get_event", {"eventId": "nope"}))

# ------------------------------------------------------------ guard hook
REAL = os.path.join(T, "real")


def hook(tool, inp):
    env = {"HOME": H, "EVALS_REAL_HOME": REAL, "PATH": "/usr/bin:/bin"}
    p = subprocess.run(["/usr/bin/python3", "-I", os.path.join(LIB, "guard_hook.py")], env=env, capture_output=True, text=True,
                       input=json.dumps({"tool_name": tool, "tool_input": inp}), timeout=10, cwd=H)
    return p.stdout


def upd(out):
    try:
        return json.loads(out)["hookSpecificOutput"]["updatedInput"]
    except Exception:
        return {}


check("guard: real-home Read mapped into the sandbox", upd(hook("Read", {"file_path": f"{REAL}/workspace/TODO.md"})).get("file_path") == f"{H}/workspace/TODO.md")
check("guard: sandbox Read untouched", hook("Read", {"file_path": f"{H}/workspace/TODO.md"}) == "")
check("guard: real-home path in Bash mapped", upd(hook("Bash", {"command": f"cat {REAL}/notes.md | head", "description": "d"})) == {"command": f"cat {H}/notes.md | head", "description": "d"})
check("guard: sandbox path in Bash untouched", hook("Bash", {"command": f"ls {H}/inbox && remind list"}) == "")
check("guard: lookalike prefix untouched", hook("Bash", {"command": f"ls {REAL}js"}) == "")
check("guard: Glob pattern mapped", upd(hook("Glob", {"pattern": f"{REAL}/inbox/*.md"})).get("pattern") == f"{H}/inbox/*.md")
check("guard: remaps logged", len(open(os.path.join(H, ".eval", "guard.log")).read().splitlines()) >= 3)
_env = {"HOME": H, "EVALS_REAL_HOME": os.path.dirname(H), "PATH": "/usr/bin:/bin"}
_p = subprocess.run(["/usr/bin/python3", "-I", os.path.join(LIB, "guard_hook.py")], env=_env, capture_output=True, text=True,
                    input=json.dumps({"tool_name": "Bash", "tool_input": {"command": f"cat {H}/inbox/a.md"}}), timeout=10, cwd=H)
check("guard: sandbox nested in the real home untouched", _p.stdout == "", _p.stdout)
check("guard: relative tilde stays in the sandbox", hook("Read", {"file_path": "~/workspace/TODO.md"}) == "")

# ------------------------------------------------------------ network guard
so = os.path.join(T, "ng.so")
_built = os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".local/share/evals/netguard-eval.so")
if shutil.which("gcc"):
    b = subprocess.run(["gcc", "-shared", "-fPIC", "-O2", "-o", so, os.path.join(LIB, "netguard_eval.c"), "-ldl"], capture_output=True, text=True)
    check("netguard: builds", b.returncode == 0, b.stderr)
elif os.path.exists(_built):  # no C compiler in the container: probe the installed build instead
    print("skip netguard build (no gcc): probing the installed guard")
    shutil.copy2(_built, so)
    b = subprocess.CompletedProcess([], 0)
else:
    print("skip netguard build + probe (no gcc, no installed guard)")
    b = subprocess.CompletedProcess([], 1)
if b.returncode == 0:
    targets = [(ip(127, 0, 0, 1), 9), (ip(10, 1, 2, 3), 80), (ip(192, 168, 7, 7), 22), (ip(149, 154, 167, 220), 443)]
    code = ("import socket,sys\nres=[]\nfor h,p in " + repr(targets) + ":\n"
            "    s=socket.socket(); s.settimeout(1)\n    try: s.connect((h,p)); res.append('open')\n"
            "    except OSError as e: res.append(e.errno)\n    s.close()\nprint(res)\n")
    log = os.path.join(T, "net.log")
    env = dict(os.environ, LD_PRELOAD=so + (" " + os.environ["LD_PRELOAD"] if os.environ.get("LD_PRELOAD") else ""), NETGUARD_LOG=log)
    p = subprocess.run(["/usr/bin/python3", "-I", "-c", code], env=env, capture_output=True, text=True, timeout=20)
    check("netguard: loopback, LAN, Telegram all ENETUNREACH", p.stdout.strip() == "[101, 101, 101, 101]", p.stdout + p.stderr)
    check("netguard: blocked attempts logged", os.path.exists(log) and len(open(log).read().splitlines()) == 4)

# ------------------------------------------------------------ engine (imported, no runs)
os.makedirs(f"{T}/corpus/cases")
open(f"{T}/corpus/config.yaml", "w").write("owner_id: '42'\nclock: '2030-01-02T03:04:00+01:00'\nglobal_paths: [workspace/CLAUDE.md]\n"
                                           "areas:\n  rem: {covers: ['bin/remind']}\n")
open(f"{T}/corpus/cases/a.yaml", "w").write(
    "cases:\n  - {id: c1, area: rem, msg: 'remind me', expect: [{stub: '^remind add'}]}\n"
    "  - {id: c2, area: other, tags: [core], msg: 'hi', expect: [{replies: 1}]}\n  - {id: c3, area: other, msg: 'x'}\n")
E = load_engine()
cfg, cases = E["load_corpus"]()
check("engine: corpus loads with hashes", [c["id"] for c in cases] == ["c1", "c2", "c3"] and all(c["_hash"] for c in cases))
tag = E["channel_tag"](cfg, {"meta": {"via": "app", "app_msg_id": "am-1"}}, "hello")
check("engine: channel tag", tag.startswith('<channel source="plugin:telegram-plus:telegram" chat_id="42"') and 'via="app"' in tag
      and tag.endswith(">hello</channel>"), tag)
st = {"text": "a\nb\nc", "details": "", "buttons": [], "kind": "reply"}
ev = {"tr": {"calls": [{"id": "1", "name": "Bash", "input": {"command": "remind add '2030-01-03 09:00' x"}, "sub": False},
                       {"id": "2", "name": "Read", "input": {"file_path": "/x/bill.jpg"}, "sub": False}],
             "texts": ["done"], "result": {"subtype": "success", "result": "ok"}, "results": {}, "init": {}},
      "stubs": [{"line": "remind add '2030-01-03 09:00' x", "cmd": "remind", "args": ["add"]}],
      "mcp": [{"server": "telegram", "tool": "reply", "input": {"text": "a\nb\nc"}}], "net": [], "guard": [], "msgs": [st]}
case = {"id": "t", "expect": [{"stub": r"^remind add\b.*2030-01-03 09:00"}, {"no_stub": "^tg "}, {"tool": "Read", "input": "bill", "max": 0, "critical": True},
                              {"replies": {"min": 1, "max": 1}}, {"reply_lines": 2}, {"reply_re": "b"}, {"no_reply_re": "secret"},
                              {"mcp": r"telegram\.reply"}, {"order": ["Bash", "Read"]}, {"trace_re": "done"}, {"judge": "be nice", "w": 2}]}
res, judge = E["grade"](case, ev, H)
got = {g["kind"]: g["passed"] for g in res}
check("grader: stub/no_stub/mcp/order/trace pass", got["stub"] and got["no_stub"] and got["mcp"] and got["order"] and got["trace_re"], res)
check("grader: tool max 0 fails + critical", not got["tool"] and [g for g in res if g["kind"] == "tool"][0]["critical"])
check("grader: reply_lines 2 fails on 3 lines", not got["reply_lines"])
ev2 = dict(ev, tr=dict(ev["tr"], calls=ev["tr"]["calls"] + [{"id": "3", "name": "mcp__hub__ha_call", "input": {}, "sub": False}],
                       results={"3": "approval_required: switch.x needs --owner-ok"}))
res2, _ = E["grade"]({"id": "t2", "expect": [{"result_re": "approval_required", "on": "mcp__hub__.*"},
                                              {"no_result_re": "approval_required", "on": "Bash"}]}, ev2, H)
check("grader: result_re / no_result_re on tool results (on = tool name filter)", [g["passed"] for g in res2] == [True, True], res2)
hook_line = json.dumps({"type": "system", "subtype": "hook_response", "hook_event": "UserPromptSubmit", "hook_name": "UserPromptSubmit",
                        "output": json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "memory2: fact A"}})})
tr_h = E["parse_stream"](hook_line + "\n")
check("stream: a hook's added context is captured", tr_h["hooks"] and tr_h["hooks"][0]["text"] == "memory2: fact A", tr_h["hooks"])
dg = E["digest"]({"id": "t"}, dict(ev, tr=dict(ev["tr"], hooks=tr_h["hooks"])), "hi")
check("judge digest shows the hook pack", "CONTEXT HOOKS ADDED" in dg and "fact A" in dg, dg[:300])
# judge digest: what reached the owner goes in full, the rest shares the budget (synthetic evidence only)
big_calls = [{"id": f"b{k}", "name": "Bash", "input": {"command": f"step-{k:02d} " + "x" * 900}, "sub": False} for k in range(40)]
big_calls.append({"id": "snd", "name": "mcp__plugin_telegram-plus_telegram__reply", "input": {"chat_id": "42", "text": "MSG-BODY " + "m" * 2600}, "sub": False})
long_msg = {"kind": "reply", "text": "MSG-BODY " + "m" * 2600 + " MSG-END", "details": "DET " + "d" * 1200 + " DET-END",
            "buttons": [[{"text": f"opt {k} " + "b" * 40, "data": f"o:{k}"} for k in range(8)]]}
ev_big = dict(ev, tr=dict(ev["tr"], calls=big_calls, texts=["t" * 900] * 4, results={f"b{k}": "R" * 1000 for k in range(40)}),
              msgs=[long_msg], stubs=[{"cmd": "voice-reply", "line": "voice-reply v-9 " + "s" * 1800 + " SAY-END", "args": []}],
              mcp=[{"server": "telegram", "tool": "react", "input": {"emoji": "👍"}}])
dg = E["digest"]({"id": "t"}, ev_big, "hi")
check("digest: the sent message, its details and buttons are in full past 14k of tool calls",
      long_msg["text"] in dg and long_msg["details"] in dg and '"o:7"' in dg and "TELEGRAM MESSAGES SENT: 1" in dg, len(dg))
check("digest: sender stubs' command lines and reactions in full", "SAY-END" in dg and "REACTIONS SET: 👍" in dg)
check("digest: the reply call's text is not repeated in the tool lines", dg.count("MSG-BODY") == 1 and "<in full under TELEGRAM" in dg)
check("digest: stays within the budget, middle tool calls left out and marked, first and last kept",
      len(dg) <= 14000 and "tool call(s) left out" in dg and "step-00" in dg and "step-39" in dg and "step-20" not in dg, len(dg))
check("digest: tool results stay cut at 300", "R" * 300 in dg and "R" * 301 not in dg)
small = E["digest"]({"id": "t"}, dict(ev, msgs=[dict(st, text="short")]), "hi")
check("digest: a small run is unchanged in shape (700/300 caps, nothing left out)", "left out" not in small and "TOOL Bash: " in small
      and small.rstrip().endswith("- reply: short"), small[-200:])
huge = dict(ev_big, msgs=[dict(long_msg, text="H" * 30000, details="")], stubs=[], mcp=[])
dg = E["digest"]({"id": "t"}, huge, "hi")
check("digest: a huge message still leaves the floor for tool evidence", "H" * 30000 in dg and "step-00" in dg
      and 30000 + 5000 < len(dg) <= 30000 + 6000 + 2000, len(dg))
dg = E["digest"]({"id": "t"}, dict(huge, msgs=[dict(long_msg, text="H" * 50000, details="")]), "hi")
check("digest: the runaway guard cuts owner output at 40k", "cut at the runaway guard" in dg and "H" * 50000 not in dg)
dry = {"id": "t", "expect": [{"stub": r"^unraid-do\b(?!.*--dry-run).*restart.*emby"}]}
dline, rline = "unraid-do --dry-run docker-restart emby", "unraid-do docker-restart emby"
r_dry = E["grade"](dry, dict(ev, stubs=[{"line": dline}]), H)[0][0]["passed"]
r_both = E["grade"](dry, dict(ev, stubs=[{"line": dline}, {"line": rline}]), H)[0][0]
check("grader: a restart grader with the dry-run lookahead skips --dry-run, counts the real call",
      not r_dry and r_both["passed"] and r_both["detail"].startswith("1 hit") and not E["grade"](
          dry, dict(ev, stubs=[{"line": "unraid-do docker-restart emby --dry-run"}]), H)[0][0]["passed"], r_both)
check("grader: replies/reply_re/no_reply_re pass", got["replies"] and got["reply_re"] and got["no_reply_re"])
check("grader: judge criteria returned separately", len(judge) == 1 and judge[0]["w"] == 2.0)
g = E["gates"](ev, 0)
check("gates: clean run passes", all(x["passed"] for x in g), g)
ev["net"] = [f"123 1 curl connect {ip(10, 0, 0, 1)}:{80}"]
check("gates: blocked attempt fails a net_strict case", not [x for x in E["gates"](ev, 0, True) if x["name"] == "network-clean"][0]["passed"])
nc = [x for x in E["gates"](ev, 0) if x["name"] == "network-clean"][0]
check("gates: blocked attempt is information by default", nc["passed"] and nc.get("info") and f"{ip(10, 0, 0, 1)}:{80}" in nc["detail"], nc)
# --changed selection
E["git"] = lambda *a, check=True: (b"bin/remind\n" if a[:2] == ("diff", "--name-only") and len(a) == 4 else b"")
E["read_last_rev"] = lambda: "abc"


class A:
    case = tag = area = None
    changed, since, max_cases, rev = True, None, 60, None


sel, info = E["select"](cfg, cases, A())
check("select --changed: covered case only", [c["id"] for c in sel] == ["c1"], (sel, info))
E["git"] = lambda *a, check=True: (b"workspace/CLAUDE.md\n" if a[:2] == ("diff", "--name-only") and len(a) == 4 else b"")
sel, info = E["select"](cfg, cases, A())
check("select --changed: global path adds core cases", [c["id"] for c in sel] == ["c2"] and info["global"], (sel, info))
# summary / regression
os.makedirs(f"{T}/state", exist_ok=True)
import time as _t
rows = [{"run_id": "r1", "ts": _t.time() - 7200, "rev": "a", "ran": 3, "passed": 3, "score": 1.0, "scores": {"x": 1.0, "y": 1.0, "z": 1.0}},
        {"run_id": "r2", "ts": _t.time() - 60, "rev": "b", "ran": 3, "passed": 2, "score": 0.83, "scores": {"x": 1.0, "y": 0.5, "z": 1.0}}]
open(f"{T}/state/history.jsonl", "w").write("\n".join(json.dumps(r) for r in rows) + "\n")
s = E["summary"](26)
check("summary: regression with newly failing case", s["regression"] and s["new_fail"] == ["y"] and "Δ -0.17" in s["line"], s)
# sandbox settings rewrite
setup = os.path.join(T, "setup")
os.makedirs(os.path.join(setup, ".claude"))
json.dump({"model": "m", "effortLevel": "high", "permissions": {"defaultMode": "x", "additionalDirectories": ["/a"]},
           "env": {"A": "1", "SOME_TOKEN": "s"}, "enabledPlugins": {"p": True},
           "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "/R/.claude/hooks/risk-check.sh"}]}],
                     "Stop": [{"hooks": [{"type": "command", "command": "/R/bin/tg-status hook Stop"}]}]}},
          open(os.path.join(setup, ".claude/settings.json"), "w"))
ss = E["sandbox_settings"](setup, "/S", "/R")
pre = ss["hooks"]["PreToolUse"]
check("settings: guard hook first, paths rewritten", pre[0]["hooks"][0]["command"] == "/S/.eval/guard_hook.py"
      and pre[1]["hooks"][0]["command"] == "/S/.claude/hooks/risk-check.sh", pre)
check("settings: status hooks dropped, plugins off, no token env", "Stop" not in ss["hooks"] and ss["enabledPlugins"] == {}
      and "SOME_TOKEN" not in ss["env"] and ss["env"]["RISK_CHECK_LLM"] == "off" and ss["permissions"]["defaultMode"] == "bypassPermissions")

# --context (H16): the CLAUDE.md import switch, the FACTS core render; --memory-hook: store copy + query vectors
cm = "Intro. Always loaded: FACTS.md (setup + ledger), imported below.\n\n@FACTS.md\n\n## Rules\n- x\n"
core = E["claude_md_variant"](cm, "core")
check("context: core switches the import and adds the note", "\n@FACTS-core.md\n" + E["CORE_NOTE"] + "\n" in core
      and "\n@FACTS.md\n" not in core and "Always loaded: FACTS-core.md (setup" in core, core)
check("context: core idempotent, full restores byte for byte", E["claude_md_variant"](core, "core") == core
      and E["claude_md_variant"](core, "full") == cm and E["claude_md_variant"](cm, "full") == cm)
try:
    E["claude_md_variant"]("no import here\n", "core")
    _refused = False
except SystemExit:
    _refused = True
check("context: core without an import line refuses", _refused)
ctx_setup = os.path.join(T, "ctx-setup")
os.makedirs(os.path.join(ctx_setup, "workspace"))
open(os.path.join(ctx_setup, "workspace", "CLAUDE.md"), "w").write(cm)
stub_mem = os.path.join(T, "memory-stub")
open(stub_mem, "w").write('#!/bin/sh\n[ "$1 $2 $3" = "render --core --out" ] && printf "# Facts core\\n" > "$4"\n')
os.chmod(stub_mem, 0o755)
os.environ["EVALS_MEMORY_BIN"] = stub_mem
E["apply_context"](ctx_setup, "core")
check("context: apply core renders FACTS-core.md and switches CLAUDE.md",
      open(os.path.join(ctx_setup, "workspace", "FACTS-core.md")).read() == "# Facts core\n"
      and "@FACTS-core.md" in open(os.path.join(ctx_setup, "workspace", "CLAUDE.md")).read())
E["apply_context"](ctx_setup, "full")
check("context: apply full switches back", open(os.path.join(ctx_setup, "workspace", "CLAUDE.md")).read() == cm)
import sqlite3 as _sq
fh = os.path.join(T, "fakehome")
os.makedirs(os.path.join(fh, "bin", "lib"))
os.makedirs(os.path.join(fh, ".local", "state", "memory2"))
_m2 = os.path.expanduser("~/bin/lib/memory2_core.py")
if os.path.isfile(_m2):
    shutil.copy2(_m2, os.path.join(fh, "bin", "lib", "memory2_core.py"))
    with _sq.connect(os.path.join(fh, ".local", "state", "memory2", "mem.db")) as _c:
        for _t_ in ("facts", "meta", "cands", "records"):
            _c.execute(f"CREATE TABLE {_t_}(x)")
    os.environ["MEMORY2_EMBED_URL"] = "http://127.0.0.1:9"
    E["HOME"], _home = fh, E["HOME"]


    class MA:
        memory_hook, quiet = "on", True


    rd_mem = os.path.join(T, "rd-mem")
    add_ = E["prep_memory_hook"](MA(), cfg, cases, rd_mem)
    E["HOME"] = _home
    mh = add_.get("_mem") or {}
    with _sq.connect(mh.get("db", ":memory:")) as _c:
        _tabs = sorted(r[0] for r in _c.execute("SELECT name FROM sqlite_master WHERE type='table'"))
    check("memory hook: facts-only store copy, no vectors when the embedder is down",
          _tabs == ["facts", "meta"] and json.load(open(mh["qvec"])) == {} and mh["vectors"] == 0, (_tabs, mh))
    MA.memory_hook = "off"
    check("memory hook: off = no additions", E["prep_memory_hook"](MA(), cfg, cases, rd_mem) == {})

# the hub in the sandbox: --hub off adds nothing; a cfg with _hub puts the MCP server `hub` in the sandbox's mcp.json
class HA_:
    hub, quiet = "off", True


check("hub: off = no additions", E["prep_hub"](HA_(), os.path.join(T, "hubrun")) == {})
hl = os.path.join(T, "hubsnap", "lib")
os.makedirs(os.path.join(hl, "tools"))
open(os.path.join(hl, "tools", "cli.py"), "w").write("# snapshot\n")
hs = os.path.join(T, "hubsetup")
os.makedirs(hs, exist_ok=True)
hc = os.path.join(T, "hubcase")
shutil.rmtree(hc, ignore_errors=True)
os.makedirs(hc)
Hh = E["build_sandbox"](dict(cfg, _hub={"lib": hl, "python": "/venv/bin/python", "hash": "x"}), {"id": "h"}, hs, hc)
mc = json.load(open(os.path.join(Hh, ".eval", "mcp.json")))["mcpServers"].get("hub") or {}
check("hub: sandbox mcp.json serves the snapshot through the venv python", mc.get("command") == "/venv/bin/python"
      and mc.get("args", [])[-1] == "serve" and mc.get("env", {}).get("TOOLS_SYSPATH") == os.path.join(Hh, ".eval", "hub", "lib")
      and os.path.isfile(os.path.join(Hh, ".eval", "hub", "lib", "tools", "cli.py")), mc)
check("hub: the server's command line names the sandbox (per-case pkill -f)", Hh in " ".join(mc.get("args", [])), mc)
_calls = []
E["sudo"], _sudo = (lambda args, **kw: _calls.append(args) or subprocess.CompletedProcess(args, 0, "", "")), E["sudo"]
_run, E["subprocess"].run = E["subprocess"].run, (lambda args, **kw: subprocess.CompletedProcess(args, 0, f"11 bash {E['JOBS']}/runs/other/c/home/x\n", "")
                                                  if args[:1] == ["ps"] else _run(args, **kw))
E["kill_run_procs"](os.path.join(E["JOBS"], "runs", "mine"))
check("kill: a concurrent run's workers survive the end-of-run kill", ["pkill", "-KILL", "-u", E["RUN_USER"]] not in _calls
      and any(c[:3] == ["pkill", "-KILL", "-f"] for c in _calls), _calls)
E["subprocess"].run = _run
_calls.clear()
E["kill_run_procs"](os.path.join(E["JOBS"], "runs", "mine"))
check("kill: alone, the whole eval user is cleaned up", ["pkill", "-KILL", "-u", E["RUN_USER"]] in _calls, _calls)
E["sudo"] = _sudo

# judge sandbox cleanup + gc
J = E["JOBS"]
rd = os.path.join(J, "runs", "r-new")
for x in ("_judge/c1/home/.claude", "_setup/x", "c1/home/.eval", "c2"):
    os.makedirs(os.path.join(rd, x), exist_ok=True)
open(os.path.join(rd, "c1", "result.json"), "w").write("{}")
E["drop_judge"](rd, "c1")
check("judge: per-case sandbox and empty _judge/ removed", not os.path.exists(os.path.join(rd, "_judge")))
os.makedirs(os.path.join(rd, "_judge", "c9", "home"))
kr = os.path.join(J, "runs", "r-kept")
os.makedirs(os.path.join(kr, "c1", "home"))
open(os.path.join(kr, "c1", "stream.jsonl"), "w").write("")
old = os.path.join(J, "runs", "r-old")
os.makedirs(os.path.join(old, "c1"))
os.utime(old, (_t.time() - 9 * 86400, _t.time() - 9 * 86400))
n, m = E["gc_runs"](7, True)
check("gc: active run keeps leftovers, old run removed", n == 1 and m == 0 and not os.path.exists(old)
      and os.path.exists(os.path.join(rd, "_judge")), (n, m))
n, m = E["gc_runs"](7)
check("gc: leftovers removed (_judge, _setup, crashed case home), results kept",
      m == 3 and not os.path.exists(os.path.join(rd, "_judge")) and not os.path.exists(os.path.join(rd, "_setup"))
      and not os.path.exists(os.path.join(rd, "c1", "home")) and os.path.exists(os.path.join(rd, "c1", "result.json")), (n, m))
check("gc: --keep run keeps its homes", os.path.exists(os.path.join(kr, "c1", "home")))

# usage: a run that finds a regression is still a healthy run (outcome ok; the usage store counts any other
# exec outcome as a failed run + an error); the regression rides in attrs.state
_logged = []
_saved = {k: E[k] for k in ("load_corpus", "select", "run_lock", "do_run", "jdump", "print_report", "summary",
                            "usage_log", "inspect_ready")}
E.update(load_corpus=lambda: ({"model": "m", "effort": "high"}, [{"id": "c1"}]),
         select=lambda cfg, cases, a: (cases, {"mode": "all"}), run_lock=lambda: 1,
         do_run=lambda a, cfg, sel, rev=None: {"run_id": "r9", "totals": {"ran": 1, "score": 0.5}, "cases": []},
         jdump=lambda *a, **k: None, print_report=lambda *a, **k: None, inspect_ready=lambda: True,
         usage_log=lambda **kw: _logged.append(kw))
_argv = sys.argv
try:
    for _reg in (True, False):
        E["summary"] = lambda hours, _r=_reg: {"regression": _r, "line": ""}
        sys.argv = ["evals", "run", "--cron"]
        _rc = E["main"]()
finally:
    sys.argv = _argv
    E.update(_saved)
check("usage: a regression run logs outcome ok + attrs.state regress",
      len(_logged) == 2 and _logged[0].get("outcome") == "ok" and _logged[0].get("attrs") == {"cases": 1, "state": "regress"},
      _logged)
check("usage: a clean run logs outcome ok, no state", len(_logged) == 2 and _logged[1].get("outcome") == "ok"
      and _logged[1].get("attrs") == {"cases": 1}, _logged)

shutil.rmtree(T, ignore_errors=True)
print(f"{'all passed' if not FAILS else str(len(FAILS)) + ' failed'}")
sys.exit(1 if FAILS else 0)
