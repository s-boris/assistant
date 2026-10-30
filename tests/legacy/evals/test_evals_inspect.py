#!/usr/bin/python3 -I
"""Offline tests for the Inspect runner of ~/bin/evals (no model calls, no sudo, no network): a fake `claude` plays
worker and judge; runs end to end on Inspect, plus --paths mapping. Skips the end-to-end part when the evals venv is
missing. (The legacy runner, `evals parity` and their checks were removed 29.10; the no-venv refusal is tested in
~/opt/assistant tests/ops/test_evals_api_errors.py.)
EVALS_BIN=/copy tests a copy; in `assistant ci` the tree's scripts/evals + scripts/lib/evals are used."""
import glob
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile

TREE = os.environ.get("ASSISTANT_CI_TREE")
BIN = os.environ.get("EVALS_BIN") or (os.path.join(TREE, "scripts", "evals") if TREE else os.path.expanduser("~/bin/evals"))
LIB = os.environ.get("EVALS_LIB") or os.path.join(os.path.dirname(os.path.realpath(BIN)), "lib", "evals")
if not os.path.isdir(LIB) and TREE:
    LIB = os.path.join(TREE, "scripts", "lib", "evals")
if not os.path.isdir(LIB):
    LIB = os.path.expanduser("~/bin/lib/evals")
REAL = pwd.getpwuid(os.getuid()).pw_dir
VENV = os.environ.get("EVALS_VENV") or os.path.join(REAL, ".local/share/evals/inspect-venv")
T = tempfile.mkdtemp(prefix="evals-inspect-tests-")
FAILS = []


def check(name, cond, info=""):
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else f"  [{str(info)[:400]}]"))
    if not cond:
        FAILS.append(name)


H = os.path.join(T, "home")
for d in ("bin", "corpus/cases", "state", "jobs", "reports", "share", "brain"):
    os.makedirs(os.path.join(T, d) if d != "bin" else os.path.join(H, "bin"), exist_ok=True)
with open(os.path.join(H, "bin", "remind"), "w") as f:
    f.write('#!/bin/sh\n# remind: durable reminders (test double; only its header is read)\nexit 0\n')
os.chmod(os.path.join(H, "bin", "remind"), 0o755)
open(os.path.join(T, "corpus/config.yaml"), "w").write(
    "owner_id: '100000001'\nowner_name: owner\ntz: UTC\nclock: '2030-01-02T03:04:00+00:00'\nmodel: fake-model\neffort: high\n")
open(os.path.join(T, "corpus/cases/a.yaml"), "w").write("""cases:
  - id: c-rem
    area: rem
    tags: [core]
    msg: remind me tomorrow at 9 to water the plants
    expect:
      - {stub: '^remind add'}
      - {replies: 1}
      - {reply_re: 'Done'}
      - {file: 'notes/*.md'}
      - {judge: 'Confirms the reminder in one short line.', w: 2}
  - id: c-thanks
    area: conduct
    msg: thanks
    expect:
      - {no_stub: '^ha '}
      - {replies: {min: 1}}
""")
FAKE = os.path.join(T, "fake-claude")
with open(FAKE, "w") as f:
    f.write(r'''#!/usr/bin/python3 -I
import json, os, subprocess, sys
args = sys.argv[1:]
sys.stdin.read()
if "--output-format" in args and args[args.index("--output-format") + 1] == "json":
    v = {"verdicts": [{"n": 1, "pass": True, "why": "fake judge"}]}
    print(json.dumps({"result": json.dumps(v), "total_cost_usd": 0.01, "usage": {"input_tokens": 10, "output_tokens": 5}}))
    sys.exit(0)
home = os.environ["HOME"]
subprocess.run(["remind", "add", "2030-01-03 09:00", "water the plants"], capture_output=True)
with open(os.path.join(home, ".eval", "mcp.jsonl"), "a") as f:
    f.write(json.dumps({"server": "telegram", "tool": "reply", "input": {"chat_id": "1", "text": "Done: 09:00 tomorrow"}}) + "\n")
os.makedirs(os.path.join(home, "notes"), exist_ok=True)
open(os.path.join(home, "notes", "x.md"), "w").write("x")
for m in [{"type": "system", "subtype": "init", "tools": ["Bash"], "mcp_servers": [], "model": "fake", "cwd": home},
          {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                                                          "input": {"command": "remind add '2030-01-03 09:00' x"}}]}},
          {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}},
          {"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}},
          {"type": "result", "subtype": "success", "is_error": False, "num_turns": 2, "total_cost_usd": 0.02, "result": "done"}]:
    print(json.dumps(m))
''')
os.chmod(FAKE, 0o755)
_built = os.path.join(REAL, ".local/share/evals/netguard-eval.so")
if not shutil.which("gcc") and os.path.exists(_built):  # no C compiler: reuse the installed guard, stamped for this source
    import hashlib
    shutil.copy2(_built, os.path.join(T, "share", "netguard-eval.so"))
    with open(os.path.join(LIB, "netguard_eval.c"), "rb") as f:
        open(os.path.join(T, "share", "netguard-eval.so.src-sha256"), "w").write(hashlib.sha256(f.read()).hexdigest() + "\n")
GUARD_OK = bool(shutil.which("gcc")) or os.path.exists(os.path.join(T, "share", "netguard-eval.so"))
G = ["git", "--git-dir", os.path.join(T, "brain"), "--work-tree", H]
subprocess.run(["git", "init", "-q", "--bare", os.path.join(T, "brain")], check=True)
subprocess.run(G + ["-c", "user.name=t", "-c", "user.email=t@t.invalid", "commit", "-q", "--allow-empty", "-m", "x"], check=True)
ENV = {"HOME": H, "PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "USAGE": "off", "EVALS_NO_SUDO": "1",
       "EVALS_TEST_TOKEN": "fake-token", "EVALS_CLAUDE": FAKE, "EVALS_CORPUS": os.path.join(T, "corpus"),
       "EVALS_STATE": os.path.join(T, "state"), "EVALS_JOBS": os.path.join(T, "jobs"), "EVALS_REPORTS": os.path.join(T, "reports"),
       "EVALS_SHARE": os.path.join(T, "share"), "EVALS_LIB": LIB, "EVALS_VENV": VENV, "BRAIN_GIT_DIR": os.path.join(T, "brain"),
       "EVALS_RUNNER_FILE": os.path.join(T, "runner")}


def ev(*args, env=None, timeout=200):
    return subprocess.run([BIN] + list(args), env=dict(ENV, **(env or {})), capture_output=True, text=True, timeout=timeout)


def runs(suffix):
    out = []
    for p in sorted(glob.glob(os.path.join(T, "state", "runs", "*", "result.json"))):
        r = json.load(open(p))
        if r["run_id"].endswith(suffix):
            out.append(r)
    return out


# ------------------------------------------------------------ pure helpers (engine imported, no runs)
os.environ.update({k: v for k, v in ENV.items() if k.startswith("EVALS_") or k == "USAGE"})
ns = {"__file__": BIN, "__name__": "evals_under_test"}
exec(compile(open(BIN).read(), BIN, "exec"), ns)
repo = os.path.join(T, "repo")
os.makedirs(os.path.join(repo, "ops", "deploy"))
open(os.path.join(repo, "ops/deploy/manifest.toml"), "w").write('[[file]]\nsrc = "scripts/remind"\ndest = "bin/remind"\n')
ns["ASSISTANT_REPO"] = repo
got = ns["map_paths"](["scripts/remind", "core/clock.py", os.path.join(ns["HOME"], ".claude/skills/x/SKILL.md")])
check("paths: manifest src -> dest, core -> bin/lib/core, absolute -> relative",
      {"scripts/remind", "bin/remind", "core/clock.py", "bin/lib/core/clock.py", ".claude/skills/x/SKILL.md"} == got, got)


# ------------------------------------------------------------ end to end on the fake worker
if not os.access(os.path.join(VENV, "bin", "python"), os.X_OK) or not GUARD_OK:
    print(f"skip end-to-end Inspect checks: {'no evals venv at ' + VENV if GUARD_OK else 'no gcc and no installed guard'}")
else:
    r = ev("setup", "--check")
    check("setup --check: pinned versions present", r.returncode == 0 and "inspect-ai" in r.stdout, r.stdout + r.stderr)
    r = ev("run", "--case", "c-*", "-j", "2")
    ri = runs("-inspect")
    check("inspect run: exit 0", r.returncode == 0, r.stdout[-600:] + r.stderr[-600:])
    ok = bool(ri) and ri[-1].get("runner") == "inspect" and ri[-1]["totals"]["ran"] == 2
    check("inspect run: result.json with runner inspect, 2 cases", ok, ri[-1:] or r.stdout[-300:])
    sc = {c["id"]: c["score"] for c in (ri[-1]["cases"] if ri else [])}
    check("inspect run: both cases 1.0 (stub, reply, file, judge graders)", sc == {"c-rem": 1.0, "c-thanks": 1.0}, sc)
    jg = [g for c in (ri[-1]["cases"] if ri else []) for g in c["graders"] if g["kind"] == "judge"]
    check("inspect run: judge through the claudecode provider", len(jg) == 1 and jg[0]["passed"] and "fake judge" in jg[0]["detail"], jg)
    lg = (ri[-1].get("inspect_log") or "") if ri else ""
    logdir = os.path.join(T, "state", "inspect-logs")
    check("inspect run: log written to the private log dir (0700)", lg.startswith(logdir) and os.path.exists(lg)
          and oct(os.stat(logdir).st_mode & 0o777) == "0o700", (lg, os.path.exists(logdir) and oct(os.stat(logdir).st_mode)))
    hist = [json.loads(x) for x in open(os.path.join(T, "state", "history.jsonl"))] if os.path.exists(os.path.join(T, "state", "history.jsonl")) else []
    check("inspect run: history row with runner", hist and hist[-1].get("runner") == "inspect" and hist[-1]["ran"] == 2, hist[-1:])
    check("inspect run: sandboxes removed", not glob.glob(os.path.join(T, "jobs", "runs", "*", "c-rem", "home")))

shutil.rmtree(T, ignore_errors=True)
print(f"{'all passed' if not FAILS else str(len(FAILS)) + ' failed'}")
sys.exit(1 if FAILS else 0)
