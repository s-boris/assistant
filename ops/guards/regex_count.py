#!/usr/local/bin/python3.13 -I
"""regex_count: count the regular expressions in the guard hooks, one stated method.

Method (static, reproducible, no code is run):
  Python  one regex = one call site of re.compile / search / match / fullmatch / findall / finditer / sub / subn /
          split whose pattern argument is not a name bound at module level to an re.compile() result (that compile
          is the site). Methods on compiled patterns (RX.search) add nothing. Globs (fnmatch) are not counted.
          FILE:FUNC limits a library file to the code reachable from FUNC (calls by plain name, transitively) plus
          the module-level compiles those functions use.
  Shell   one regex = one grep -E / egrep / grep -P invocation outside comments; case arms (globs) are not counted.
Usage: regex_count.py [--json] PATH[:ENTRY]...
"""

import ast
import json
import re
import sys

FUNCS = {"compile", "search", "match", "fullmatch", "findall", "finditer", "sub", "subn", "split"}
SHELL_RX = re.compile(r"(?<![\w-])(?:egrep|grep(?:\s+-[A-Za-z]+)*\s+-[A-Za-z]*[EP][A-Za-z]*)(?![\w-])")


def _is_re_call(node):
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in FUNCS
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "re"
    )


def _pattern_arg(call):
    if call.args:
        return call.args[0]
    return next((k.value for k in call.keywords if k.arg == "pattern"), None)


def count_python(src, entry=None):
    tree = ast.parse(src)
    compiled = set()  # module-level names bound to re.compile(...)
    for node in tree.body:
        if isinstance(node, ast.Assign) and _is_re_call(node.value) and node.value.func.attr == "compile":
            compiled.update(t.id for t in node.targets if isinstance(t, ast.Name))

    def sites(nodes):
        out = []
        for root in nodes:
            for n in ast.walk(root):
                if _is_re_call(n):
                    p = _pattern_arg(n)
                    if n.func.attr != "compile" and isinstance(p, ast.Name) and p.id in compiled:
                        continue
                    out.append((n.lineno, n.col_offset))  # two sites on one line are two
        return out

    if entry is None:
        return len(set(sites([tree])))
    funcs = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    if entry not in funcs:
        raise SystemExit(f"regex_count: no function {entry}")
    seen, todo, names = set(), [entry], set()
    while todo:
        f = todo.pop()
        if f in seen:
            continue
        seen.add(f)
        for n in ast.walk(funcs[f]):
            if isinstance(n, ast.Name):
                names.add(n.id)
                if n.id in funcs and n.id not in seen:
                    todo.append(n.id)
    module_sites = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in node.targets):
            module_sites += sites([node])
    return len(set(sites([funcs[f] for f in seen]) + module_sites))


def count_shell(src):
    n = 0
    for line in src.splitlines():
        code = line.split("#", 1)[0] if not line.lstrip().startswith("#") else ""
        n += len(SHELL_RX.findall(code))
    return n


def count(spec):
    path, _, entry = spec.partition(":")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    if path.endswith(".py") or src.startswith("#!/usr/bin/python") or src.startswith("#!/usr/bin/env python"):
        return count_python(src, entry or None)
    return count_shell(src)


def main(argv):
    as_json = "--json" in argv
    specs = [a for a in argv if a != "--json"]
    if not specs:
        print(__doc__.strip())
        return 2
    res = {s: count(s) for s in specs}
    if as_json:
        print(json.dumps({"files": res, "total": sum(res.values())}, indent=1))
    else:
        for s, n in res.items():
            print(f"{n:5d}  {s}")
        print(f"{sum(res.values()):5d}  total")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
