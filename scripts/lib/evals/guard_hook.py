#!/usr/local/bin/python3.13 -I
# ruff: noqa: E501
"""Eval-sandbox PreToolUse hook (`evals`): keeps a worker hermetic. The setup under test names the real home in
places (docs, notes, absolute paths in scripts), so file tools and Bash commands that point into the real home are
rewritten to the same path inside the sandbox home: the worker finds its own copy there, as it would in production,
and nothing real is read. Each rewrite is logged to $HOME/.eval/guard.log for scoring. Fails open: the eval user's
permissions and the network guard are the hard boundary; this hook is about hermeticity."""

import json
import os
import re
import sys
import time

HOME = os.environ.get("HOME", "").rstrip("/")
REAL = os.environ.get("EVALS_REAL_HOME", "/home/node").rstrip("/")
ALLOW = (HOME, REAL + "/.local/share/claude", REAL + "/.local/bin/claude")
PATH_RE = re.compile(re.escape(REAL) + r"(?![\w.-])(/[^\s'\"`;|&)<>]*)?")
PATH_KEYS = ("file_path", "path", "notebook_path")


def resolve(path):
    return os.path.normpath(os.path.join(os.getcwd(), os.path.expanduser(path)))


def outside(path):
    """True for a path in the real home that is not the sandbox itself (or the claude binary)."""
    if not path:
        return False
    p = resolve(path)
    if not (p == REAL or p.startswith(REAL + "/")):
        return False
    return not any(p == a or p.startswith(a.rstrip("/") + "/") for a in ALLOW if a)


def twin(path, keep_spelling=False):
    """The sandbox twin of a real-home path."""
    p = path if keep_spelling else resolve(path)
    return HOME + p[len(REAL) :]


def log(tool, what):
    try:
        with open(os.path.join(HOME, ".eval", "guard.log"), "a") as f:
            f.write(json.dumps({"ts": time.time(), "tool": tool, "what": what[:300], "action": "remap"}) + "\n")
    except Exception:
        pass


def rewrite(tool, new, hits):
    log(tool, ", ".join(hits))
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "permissionDecisionReason": "eval sandbox: real-home path mapped to the same path in the sandbox home",
                    "updatedInput": new,
                }
            }
        )
    )
    return 0


def main():
    try:
        ev = json.load(sys.stdin)
    except Exception:
        return 0
    if not HOME or HOME == REAL:
        return 0
    tool = ev.get("tool_name", "")
    inp = ev.get("tool_input") or {}
    if not isinstance(inp, dict):
        return 0
    hits = []
    if tool in ("Bash", "Monitor"):

        def sub(m):
            if outside(m.group(0)):
                hits.append(m.group(0))
                return twin(m.group(0), keep_spelling=True)
            return m.group(0)

        new = PATH_RE.sub(sub, str(inp.get("command", "")))
        return rewrite(tool, dict(inp, command=new), hits) if hits else 0
    new = dict(inp)
    keys = PATH_KEYS + (("pattern",) if tool == "Glob" else ())
    for key in keys:
        v = inp.get(key)
        if isinstance(v, str) and v.startswith(("/", "~")) and outside(v):
            new[key] = twin(v)
            hits.append(v)
    return rewrite(tool, new, hits) if hits else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
