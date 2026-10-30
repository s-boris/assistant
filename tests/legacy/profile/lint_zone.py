"""Owner-zone lint: no hardcoded owner time zone in code under ROOT's bin, plugins and hooks.

  python3 -I lint_zone.py ROOT     exit 0 = clean, 1 = hits (printed as path:line, never the line itself)

The zone comes from the profile (assistant_profile.tz() / tz_name(); shell: `profile-get locale.tz`).
Allowed: the constants in ALLOW (the helper's fallback, the two plugin fallbacks that can't import Python, a protocol
constant that is not the owner's zone), the reference-data tables in DATA (public data, not personal values),
comments and docstrings, docs (*.md, README*), and tests. Release snapshots (bin/lib/core.releases/<rel>/: generated
copies of the repo's core, kept for rollback) are skipped; the active core is scanned once through its link
bin/lib/core. ROOT: the live home (bin, plugins, .claude/hooks) or the public repo (bin, plugins, claude/hooks).
Every entry of ALLOW and DATA names one constant in one file and says why; never allow a whole file or directory.
"""

import ast
import io
import os
import re
import sys
import tokenize

ZONE = re.compile(r"\b(?:Africa|America|Antarctica|Asia|Atlantic|Australia|Europe|Indian|Pacific)/[A-Z][A-Za-z_]+")
SCAN = ("bin", "plugins", ".claude/hooks", "claude/hooks", "bin/lib/core")  # bin/lib/core: the active core's link
SKIP_DIRS = {"node_modules", ".git", "test", "tests", "__pycache__", "fixtures"}
SKIP_EXT = (".md", ".json", ".lock", ".tsv", ".txt", ".log", ".png", ".jpg", ".gz", ".pdf")
ALLOW = {  # path relative to ROOT -> (the one constant there that may hold a zone, that exact zone or None = any, why)
    "bin/lib/assistant_profile.py": ("FALLBACK_TZ", None, "the helper's fallback when the profile names no zone"),
    "plugins/telegram-plus/quiet.ts": ("DEFAULT_QUIET_TZ", None, "plugin fallback; the plugin can't import Python"),
    "plugins/heartbeat/hooks/register.js": ("DEFAULT_TZ", None, "plugin fallback; the hook can't import Python"),
    "bin/lib/trackid_shazam.py": (
        "TZ",
        "Europe/Berlin",
        "protocol constant: the generic CET zone the Shazam apps send; sending the owner's zone would leak it",
    ),
}
DATA = {  # path relative to ROOT -> (the one top-level literal whose zone strings are reference data, why)
    "bin/lib/flights.py": ("AIRPORTS", "public IATA airport -> zone table (generic data, not a personal value)"),
}


def skip_file(name):
    low = name.lower()
    return (
        name.startswith(".")
        or low.startswith("readme")
        or low.endswith(SKIP_EXT)
        or "test" in low
        or ".bak" in low
        or low.endswith((".orig", ".new", "~"))
    )


def files(root):
    seen = set()
    for top in SCAN:
        base = os.path.join(root, top)
        if not os.path.isdir(base):
            continue
        link_top = os.path.islink(base)  # report the active core as bin/lib/core/..., not its release path
        for d, dirs, names in os.walk(base):
            dirs[:] = sorted(
                x
                for x in dirs
                if x not in SKIP_DIRS
                and not x.endswith(("-tests", ".releases"))
                and not (top == "plugins" and d == base and x in ("cache", "marketplaces", "data"))
            )
            for n in sorted(names):
                p = os.path.join(d, n)
                real = os.path.realpath(p)
                if skip_file(n) or real in seen or not os.path.isfile(real):
                    continue
                seen.add(real)
                under = real.startswith(root + os.sep) and not link_top
                yield os.path.relpath(real, root) if under else os.path.relpath(p, root), real


def is_python(path, text):
    return path.endswith(".py") or (text.startswith("#!") and "python" in text.split("\n", 1)[0])


def doc_lines_python(text):
    """(lines inside docstrings, {line: column where its comment starts}); a docstring = a string token that
    starts a logical line."""
    out, comments = set(), {}
    prev = tokenize.NEWLINE
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.type == tokenize.COMMENT:
            comments[tok.start[0]] = tok.start[1]
        elif tok.type == tokenize.STRING and prev in (tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.NL):
            out.update(range(tok.start[0], tok.end[0] + 1))
        if tok.type not in (tokenize.COMMENT, tokenize.NL):
            prev = tok.type
    return out, comments


def data_lines(text, const):
    """Lines of the one top-level `const = <dict/list/tuple literal>` in Python text; empty when there is none."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return set()
    for n in tree.body:
        targets = n.targets if isinstance(n, ast.Assign) else [n.target] if isinstance(n, ast.AnnAssign) else []
        if any(isinstance(t, ast.Name) and t.id == const for t in targets) and isinstance(
            n.value, (ast.Dict, ast.List, ast.Tuple)
        ):
            return set(range(n.lineno, (n.end_lineno or n.lineno) + 1))
    return set()


def is_comment_line(line, start):
    s = line.lstrip()
    if s.startswith(("#", "//", "*", "/*")):
        return True
    return bool(re.search(r"(^|\s)(#|//)\s", line[:start]))


def hits(rel, path):
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except (UnicodeDecodeError, OSError):
        return []
    if not ZONE.search(text):
        return []
    docs = None
    py = is_python(rel, text)
    if py:
        try:
            docs = doc_lines_python(text)
        except (tokenize.TokenError, SyntaxError, IndentationError):
            docs = None
    const, pinned, _why = ALLOW.get(rel, (None, None, None))
    table = data_lines(text, DATA[rel][0]) if py and rel in DATA else set()
    out = []
    for i, line in enumerate(text.split("\n"), 1):
        for m in ZONE.finditer(line):
            if docs is not None and (i in docs[0] or m.start() > docs[1].get(i, len(line))):
                continue
            if docs is None and is_comment_line(line, m.start()):
                continue
            if i in table:
                continue
            if (
                const
                and pinned in (None, m.group())
                and re.search(r"\b%s\s*=\s*['\"]%s['\"]" % (const, re.escape(m.group())), line)  # noqa: UP031
            ):
                continue
            out.append("%s:%d" % (rel, i))  # noqa: UP031
    return out


def main(argv):
    if len(argv) != 1 or not os.path.isdir(argv[0]):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    root = os.path.realpath(argv[0])
    bad = [h for rel, p in files(root) for h in hits(rel, p)]
    for h in bad:
        print(
            "owner-zone lint: hardcoded zone at %s (use assistant_profile.tz()/tz_name() or profile-get locale.tz)" % h  # noqa: UP031
        )
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
