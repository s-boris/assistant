"""Determinism lint for DBOS workflows. Stdlib only (ast).

DBOS replays a workflow function after a restart: every step and DBOS call already recorded returns its recorded
result, so the body must take the same path again. A workflow body (a function decorated @DBOS.workflow) may
compute on its arguments and on what steps (@DBOS.step functions) and the allowed DBOS calls return; nothing else.
Flagged inside a workflow body, and inside the module's plain helper functions it calls (followed transitively):
  - calls into clocks, randomness, ids, the environment, files, processes, the network, threads or event loops
    (time, datetime.now/utcnow/today, random, secrets, uuid, os, subprocess, socket, urllib, http, asyncio,
    threading, open, print, input)
  - DBOS calls outside ALLOWED_DBOS (e.g. DBOS.list_workflows: a live read whose answer changes between runs)
  - reads of mutable module state (MUTABLE names: hooks, the loop) and `global` / `nonlocal` statements
  - iterating over a set literal or set() call (order is not stable across processes)

  python3 ops/lint/wf_determinism.py FILE...   prints file:line: message per finding; exit 1 when any
  check(source, filename) -> [str]
"""

from __future__ import annotations

import ast
import sys

BANNED_MODULES = ("time", "random", "secrets", "uuid", "os", "subprocess", "socket", "urllib", "http", "asyncio",
                  "threading", "sys", "shutil", "pathlib")  # fmt: skip
BANNED_CALLS = {"open", "print", "input", "id", "hash", "vars", "globals", "locals", "exec", "eval", "__import__"}
BANNED_ATTRS = {("datetime", "now"), ("datetime", "utcnow"), ("datetime", "today"), ("date", "today")}
ALLOWED_DBOS = {"recv", "send", "set_event", "get_event", "sleep", "start_workflow", "workflow_id", "logger", "step_id"}
MUTABLE = {"HOOKS", "LOOP", "STATE", "ENGINE"}


def _dotted(n: ast.AST) -> str:
    parts = []
    while isinstance(n, ast.Attribute):
        parts.append(n.attr)
        n = n.value
    if isinstance(n, ast.Name):
        parts.append(n.id)
        return ".".join(reversed(parts))
    return ""


def _deco_kind(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    for d in fn.decorator_list:
        target = d.func if isinstance(d, ast.Call) else d
        name = _dotted(target)
        if name in ("DBOS.workflow", "DBOS.scheduled"):
            return "workflow"
        if name in ("DBOS.step", "DBOS.transaction"):
            return "step"
    return "plain"


def check(source: str, filename: str = "<src>") -> list[str]:
    tree = ast.parse(source, filename)
    funcs: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    kinds: dict[str, str] = {}
    imported: dict[str, str] = {}  # local name -> module it came from (import x as y / from x import y)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs[node.name] = node
            kinds[node.name] = _deco_kind(node)
        elif isinstance(node, ast.Import):
            for a in node.names:
                imported[a.asname or a.name.split(".")[0]] = a.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for a in node.names:
                imported[a.asname or a.name] = f"{node.module}.{a.name}"
    out: list[str] = []
    seen: set[str] = set()

    def flag(n: ast.AST, why: str, where: str) -> None:
        out.append(f"{filename}:{getattr(n, 'lineno', 0)}: {why} (in {where})")

    def visit(fn: ast.FunctionDef | ast.AsyncFunctionDef, where: str) -> None:
        if fn.name in seen:
            return
        seen.add(fn.name)
        body = ast.Module(body=list(fn.body), type_ignores=[])  # the decorators are not the body
        for n in ast.walk(body):
            if isinstance(n, (ast.Global, ast.Nonlocal)):
                flag(n, "global/nonlocal state", where)
            elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in MUTABLE:
                flag(n, f"reads mutable module state {n.id}", where)
            elif isinstance(n, ast.For) and (
                isinstance(n.iter, ast.Set) or (isinstance(n.iter, ast.Call) and _dotted(n.iter.func) == "set")
            ):
                flag(n, "iterates a set (unstable order)", where)
            elif isinstance(n, ast.Call):
                name = _dotted(n.func)
                if not name:
                    continue
                head = name.split(".")[0]
                origin = imported.get(head, head)
                if name in BANNED_CALLS:
                    flag(n, f"calls {name}()", where)
                elif origin.split(".")[0] in BANNED_MODULES:
                    flag(n, f"calls {name} ({origin.split('.')[0]}: belongs in a step)", where)
                elif tuple(name.split(".")[-2:]) in BANNED_ATTRS:
                    flag(n, f"calls {name} (a clock: belongs in a step)", where)
                elif head == "DBOS" and name.count(".") == 1 and name.split(".")[1] not in ALLOWED_DBOS:
                    flag(n, f"calls {name} (not replay-safe in a workflow body)", where)
                elif name in funcs and kinds.get(name) == "plain" and n.func is not None:
                    visit(funcs[name], f"{where} → {name}")
            elif isinstance(n, ast.Attribute) and _dotted(n) in ("DBOS.workflow_id",):
                continue

    for name, fn in funcs.items():
        if kinds[name] == "workflow":
            visit(fn, name)
            seen.clear()
    return sorted(set(out))


def main(argv: list[str]) -> int:
    bad = 0
    for f in argv:
        with open(f, encoding="utf-8") as fh:
            found = check(fh.read(), f)
        for line in found:
            print(line)
        bad += len(found)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
