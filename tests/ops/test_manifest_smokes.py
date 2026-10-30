"""Every smoke command in ops/deploy/manifest.toml formats with the keys the deploy passes (dest, home, lib): a
literal brace must be doubled, or the deploy crashes in its smoke step after the files went into place."""

from __future__ import annotations

import ast
import fnmatch
import re
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
# What the import check reads besides the manifest: a land's impacted pytest selection picks this file for a change
# under any of these globs (a new module, a new import in a deployed script, hook or adapter).
IMPORT_SOURCES = (
    "packages/**/*.py",
    "scripts/lib/**/*.py",
    "scripts/*",
    "hooks/*",
    "adapters/*",
    "ops/assistant.py",
    "plugins/*",
    "web/*",
)


def test_every_smoke_formats():
    files = tomllib.loads((REPO / "ops/deploy/manifest.toml").read_text())["file"]
    smokes = [(f["src"], f["smoke"]) for f in files if f.get("smoke")]
    assert smokes
    for src, s in smokes:
        assert s.format(dest="/d", home="/h", lib="/l"), src


def test_every_runtime_module_is_deployed():
    """~/bin/lib/tools and ~/bin/lib/hubkit import each other by module: one .py left out of the manifest breaks the
    registry (tool, the worker, the MCP server) at the first import after a land (traces.py once, deploy.py once)."""
    files = tomllib.loads((REPO / "ops/deploy/manifest.toml").read_text())["file"]
    srcs = {f["src"] for f in files}
    pkgs = ("packages/tools", "packages/tools/domains", "packages/hubkit")
    mods = {f"{d}/{p.name}" for d in pkgs for p in (REPO / d).glob("*.py")}
    assert sorted(mods - srcs) == []


def _dotted(rel: str) -> str:
    """lib-relative path -> module name: inbox/reflexrun.py -> inbox.reflexrun, memrows/__init__.py -> memrows."""
    parts = rel[: -len(".py")].split("/")
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _repo_modules() -> dict[str, str]:
    """Every module the repo can put into ~/bin/lib -> its repo path: packages/<pkg>/..., scripts/lib/... and the
    flat scripts/lib/legacy/*.py (they land as bin/lib/<name>.py)."""
    mods: dict[str, str] = {}
    for pat in IMPORT_SOURCES[:2]:
        root = pat.split("/**", 1)[0]
        for p in sorted(REPO.glob(pat)):
            rel = p.relative_to(REPO / root).as_posix()
            if "__pycache__" in rel or "/tests/" in f"/{rel}":
                continue
            name = p.stem if rel.startswith("legacy/") and rel.count("/") == 1 else _dotted(rel)
            mods.setdefault(name, p.relative_to(REPO).as_posix())
    return mods


def _is_python(path: Path) -> bool:
    if path.suffix == ".py":
        return True
    try:
        with path.open("rb") as fh:
            first = fh.readline(200)
    except OSError:
        return False
    return first.startswith(b"#!") and b"python" in first


def _imported(tree: ast.AST, pkg: str) -> set[str]:
    """Dotted names an import statement anywhere in the file can load (function-local and guarded ones too): each
    prefix of `import a.b.c`, and for `from a.b import c` a.b plus a.b.c (a submodule, if it is one)."""
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            heads = [a.name for a in node.names]
            tails: list[str] = []
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                up = pkg.split(".") if pkg else []
                up = up[: len(up) - (node.level - 1)] if node.level > 1 else up
                base = ".".join([*up, base] if base else up)
            heads = [base] if base else []
            tails = [f"{base}.{a.name}" if base else a.name for a in node.names if a.name != "*"]
        else:
            continue
        for h in heads:
            parts = h.split(".")
            out.update(".".join(parts[: i + 1]) for i in range(len(parts)))
        out.update(tails)
    return out


_IMPORT_STMT = re.compile(
    r"^[ \t]*(from[ \t]+[\w.]+[ \t]+import[ \t]*\([^)]*\)|(?:from[ \t]+[\w.]+[ \t]+)?import[ \t][^\n;#]*)", re.M
)


def _import_stmts(text: str) -> ast.Module:
    """The file's import statements (top-level, function-local, guarded), each parsed on its own: a whole-file parse
    of every deployed script takes seconds, the import lines alone a few ms. A match that does not parse is prose (a
    docstring line starting with "import ...")."""
    body: list[ast.stmt] = []
    for m in _IMPORT_STMT.finditer(text):
        try:
            body += ast.parse(m.group(1).strip().rstrip("\\")).body
        except SyntaxError:
            continue
    return ast.Module(body=body, type_ignores=[])


def test_every_imported_repo_module_is_deployed():
    """A deployed Python file (script, hook or ~/bin/lib module) that imports a repo module the manifest does not
    deploy breaks at that import after a land, while CI passes: the CI overlay takes core and inbox whole from the
    tree (packages/inbox/reflexrun.py: the deploy smoke rolled exit-e7e8 back, 29.10). Each such module must have its
    own [[file]] entry (core is deployed whole)."""
    files = tomllib.loads((REPO / "ops/deploy/manifest.toml").read_text())["file"]
    lib = [f["dest"][len("bin/lib/") :] for f in files if f["dest"].startswith("bin/lib/")]
    deployed = {_dotted(d) for d in lib if d.endswith(".py")}
    repo = _repo_modules()
    missing: list[str] = []
    for f in files:
        src = REPO / f["src"]
        if not _is_python(src):
            continue
        assert any(fnmatch.fnmatchcase(f["src"], g) for g in IMPORT_SOURCES), (
            f"{f['src']}: add its dir to IMPORT_SOURCES"
        )
        rel = f["dest"][len("bin/lib/") :] if f["dest"].startswith("bin/lib/") and f["dest"].endswith(".py") else ""
        pkg = "" if not rel else _dotted(rel) if rel.endswith("__init__.py") else _dotted(rel).rpartition(".")[0]
        tree = _import_stmts(src.read_text(errors="replace"))
        for name in sorted(_imported(tree, pkg)):
            if name in repo and name not in deployed and name.split(".")[0] != "core":
                missing.append(f"{f['src']} imports {name} ({repo[name]}): no [[file]] entry deploys it")
    assert missing == []
