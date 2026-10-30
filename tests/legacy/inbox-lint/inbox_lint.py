#!/usr/local/bin/python3.13 -I
"""inbox_lint: every writer of ~/inbox notes must mark its notes as data ("Data, not instructions").

Scans ~/bin (recursively, following symlinks; skips __pycache__ and binaries) and ~/crontab.
  writer   a script with a write site into the inbox: a shell redirect/tee/mv/cp/touch whose target is an inbox
           path (~/inbox, $HOME/inbox, ${HOME}/inbox, /home/node/inbox) or a variable derived from one; a Python
           open(..., 'w'|'a'|'x'), write_text, atomic_write, os.replace, shutil.move ... on such a path or variable
           (names like INBOX, INBOX_DIR, inbox_dir count as inbox variables; assignments propagate it); or a call
           `module.helper(` to an inbox-writing function defined in another scanned file (botlib.write_inbox, ...).
  pass     the marker text appears in the script outside full-line comments: "Data, not instructions" or
           "Data from <source>, not instructions" (first letter either case); or every write site is a call to a
           helper whose own body adds the marker; or the script is in the allowlist (allowlist.txt, with a reason).
  crontab  per line (comments skipped): a redirect/tee/mv/cp into the inbox needs the marker on the same line,
           and a redirect into the literal /home/node/inbox fails (use "$HOME/inbox").
  stale    an allowlist entry for a missing script, a non-writer or a script that passes anyway fails too.
Usage: inbox_lint.py [--bin DIR] [--crontab FILE] [--allowlist FILE] [-v]   exit 0 = clean, 1 = problems, 2 = usage
"""
import argparse, functools, os, re, sys

HERE = os.path.dirname(os.path.realpath(__file__))
MARKER = re.compile(r"[Dd]ata(?: from [^,\n]{1,80})?, not instructions")
# An inbox path written out: shell/text forms, os.path.join(HOME, 'inbox'), HOME / "inbox", Path.home() / 'inbox'.
INBOX_PATH = re.compile(r"""(?x)
    (?:~|\$HOME|\$?\{HOME\}|/home/node)/inbox(?![\w.-])
  | (?<![\w.])HOME\w*\s*(?:,|/)\s*['"]inbox['"]
  | Path\.home\(\)\s*/\s*['"]inbox['"]
""")
LITERAL_HOME_INBOX = re.compile(r"/home/node/inbox(?![\w.-])")
SEED_PY = re.compile(r"(?<![\w.])(?:[A-Z][A-Z_]*_)?INBOX(?:_DIR|_PATH)?(?!\w)|(?<![\w])(?:\w+\.)?inbox_(?:dir|path)(?!\w)")
SEED_SHELL = re.compile(r"\$\{?(?:[A-Z][A-Z_]*_)?INBOX(?:_DIR|_PATH)?\b|\$\{?inbox(?:_dir)?\b")
PY_WRITE = re.compile(r"""(?x)
    \bopen\([^\n]*?,\s*(?:mode\s*=\s*)?[rf]?['"](?:[wax][bt]?\+?|r\+[bt]?)['"]
  | \.open\(\s*['"](?:[wax][bt]?\+?)['"]
  | \bos\.open\( | \.write_(?:text|bytes)\( | \.touch\(
  | (?<![\w])(?:\w+\.)?(?!write\()\w*write\w*\(          # write_atomic(, atomic_write(, write_private(; not f.write(
  | \bos\.(?:replace|rename|link|symlink)\( | \bshutil\.(?:move|copy\w*)\(
""")
PY_ASSIGN = re.compile(r"^\s*((?:[\w.]+\s*,\s*)*[\w.]+)\s*(?::\s*[\w\[\], .]+)?=(?!=)(.*)$")
PY_DEF = re.compile(r"^(\s*)(?:async\s+)?def\s+(\w+)\s*\(")
PY_FROM_IMPORT = re.compile(r"^\s*from\s+([\w.]+)\s+import\s+\(?([\w, ]+)")
SHELL_ASSIGN = re.compile(r"(?:^|[\s;&|(])(?:local\s+|export\s+|readonly\s+|declare\s+(?:-\w+\s+)?)?([A-Za-z_]\w*)="
                       r"(\"[^\"]*\"|'[^']*'|\S*)")
SHELL_REDIRECT = re.compile(r"(?<![<>&=\-|])(?:\d|&)?>>?\|?\s*(\"[^\"]*\"|'[^']*'|[^\s;|&)<>]+)")
SHELL_TEE = re.compile(r"\btee\b((?:\s+-\w+)*\s+[^;|&]+)")
SHELL_MOVE = re.compile(r"(?:^|[\s;&|({])(?:mv|cp|install|ln|touch)\s+([^;|&]+)")


def comment_line(line):
    return line.lstrip().startswith("#")


def language(path, text):
    first = text.split("\n", 1)[0]
    if path.endswith(".py") or (first.startswith("#!") and "python" in first):
        return "py"
    if first.startswith("#!") and re.search(r"\b(?:ba|da|z)?sh\b", first):
        return "sh"
    return "any"


def has_marker(text):
    return any(MARKER.search(l) for l in text.split("\n") if not comment_line(l))


class FileInfo:
    def __init__(self, rel, text):
        self.rel, self.text = rel, text
        self.lines = text.split("\n")
        self.lang = language(rel, text)
        self.marker = has_marker(text)
        self.defs = py_defs(self.lines)   # (name, start, end) of Python functions (also embedded in shell)
        self.names = {None: set()}  # inbox-derived names per scope: None = module/whole file, else a defs index
        self.sites = []             # (lineno, how, helper-or-None, helper_adds_marker)
        self.helpers = {}           # name -> adds marker (functions here that write into the inbox)
        self.modules = {}           # alias -> module name (import X as Y) or None (importlib module_from_spec)
        self.imported = set()       # (name, module) from `from module import name`

        self._scopes = [[None] for _ in self.lines]
        for k, (_, start, end) in enumerate(self.defs):   # defs are listed inner first: sort by start for nesting
            for i in range(start, end):
                self._scopes[i].append(k)
        for sc in self._scopes:
            sc[1:] = sorted(sc[1:], key=lambda k: self.defs[k][1])

    def scopes(self, i):
        """Scopes visible at 0-based line i: the module plus every enclosing def, innermost last."""
        return self._scopes[i]


@functools.lru_cache(maxsize=4096)
def name_rx(names, shell):
    if not names:
        return None
    alts = "|".join(sorted((re.escape(n) for n in names), key=len, reverse=True))
    return re.compile(r"\$\{?(?:%s)\b" % alts if shell else r"(?<![\w.])(?:%s)(?![\w'\"])" % alts)


def refs_inbox(s, info, shell, i=None):
    if INBOX_PATH.search(s) or (SEED_SHELL if shell else SEED_PY).search(s):
        return True
    names = frozenset().union(*(info.names.get(k, ()) for k in ([None] if i is None else info.scopes(i))))
    rx = name_rx(names, shell)
    return bool(rx and rx.search(s))


def py_defs(lines):
    out, stack = [], []
    for i, l in enumerate(lines):
        if not l.strip() or comment_line(l):
            continue
        ind = len(l) - len(l.lstrip())
        while stack and ind <= stack[-1][1]:
            name, _, start = stack.pop()
            out.append((name, start, i))
        m = PY_DEF.match(l)
        if m:
            stack.append((m.group(2), len(m.group(1)), i))
    out.extend((name, start, len(lines)) for name, _, start in stack)
    return out


def taint(info):
    """Grow the inbox-derived names to a fixpoint. Python: an assignment taints its target in the innermost scope;
    a function that returns an inbox reference taints its name. Shell: one scope for the whole file."""
    for _ in range(10):
        before = {k: set(v) for k, v in info.names.items()}
        for i, l in enumerate(info.lines):
            if comment_line(l):
                continue
            m = PY_ASSIGN.match(l)   # Python rules run on shell scripts too (Python embedded in heredocs)
            if m and refs_inbox(m.group(2), info, False, i):
                info.names.setdefault(info.scopes(i)[-1], set()).update(n.strip() for n in m.group(1).split(","))
            if info.lang != "py":
                for m in SHELL_ASSIGN.finditer(l):
                    if refs_inbox(m.group(2), info, True):
                        info.names[None].add(m.group(1))
        for k, (name, start, end) in enumerate(info.defs):
            for i in range(start + 1, end):
                l = info.lines[i]
                if re.match(r"\s*return\b", l) and info.scopes(i)[-1] == k and refs_inbox(l, info, False, i):
                    info.names[None].add(name)
                    break
        if info.names == before:
            break


def direct_sites(info):
    for i, l in enumerate(info.lines):
        if comment_line(l) or PY_DEF.match(l):
            continue
        if PY_WRITE.search(l) and refs_inbox(l, info, False, i):
            info.sites.append((i + 1, "python write", None, False))
        if info.lang != "py":
            targets = [m.group(1) for m in SHELL_REDIRECT.finditer(l) if not m.group(1).startswith("&")]
            targets += [m.group(1) for m in SHELL_TEE.finditer(l)] + [m.group(1) for m in SHELL_MOVE.finditer(l)]
            if any(refs_inbox(t, info, True) for t in targets):
                info.sites.append((i + 1, "shell write", None, False))
    for name, start, end in info.defs:
        if any(start < ln - 1 < end for ln, *_ in info.sites):
            info.helpers[name] = has_marker("\n".join(info.lines[start:end]))
    for l in info.lines:
        m = PY_FROM_IMPORT.match(l)
        if m:
            mod = m.group(1).split(".")[-1]
            info.imported.update((n.strip(), mod) for n in m.group(2).split(",") if n.strip())
        for m in re.finditer(r"^\s*import\s+([\w.]+)(?:\s+as\s+(\w+))?", l):
            info.modules[m.group(2) or m.group(1).split(".")[-1]] = m.group(1).split(".")[-1]
        m = re.match(r"\s*(\w+)\s*=\s*importlib\.(?:util\.module_from_spec|import_module)\(", l)
        if m:
            info.modules[m.group(1)] = None    # loaded by path: resolved below from the quoted script names


def modname(rel):
    return os.path.basename(rel).removesuffix(".py").replace("-", "_")


def helper_sites(info, registry, rels):
    """Calls to inbox-writing helpers defined in other scanned files: `botlib.write_inbox(` (import botlib),
    `W.inbox_note(` (W = importlib...module_from_spec of 'watch'), or a name from `from botlib import ...`."""
    if not registry:
        return
    quoted = set()
    if None in info.modules.values():   # scripts loaded by path: the quoted names of scanned files in this one
        strings = set(re.findall(r"['\"]([\w.-]+)['\"]", info.text))
        quoted = {r for r in rels if os.path.basename(r) in strings}

    def targets(alias):
        if alias not in info.modules:
            return set()
        mod = info.modules[alias]
        return quoted if mod is None else {r for r in rels if modname(r) == mod}

    alts = "|".join(sorted(map(re.escape, registry), key=len, reverse=True))
    qualified = re.compile(r"(?<![\w.])(\w+)\.(%s)\(" % alts)
    bare = re.compile(r"(?<![\w.])(%s)\(" % alts)
    imported = dict(info.imported)
    for i, l in enumerate(info.lines, 1):
        if comment_line(l) or PY_DEF.match(l):
            continue
        hits = [(m.group(2), targets(m.group(1))) for m in qualified.finditer(l)]
        hits += [(m.group(1), {r for r in rels if modname(r) == imported[m.group(1)]})
                 for m in bare.finditer(l) if m.group(1) in imported and m.group(1) not in info.helpers]
        for name, where in hits:
            defs = [a for rel, a in registry[name] if rel in where and rel != info.rel]
            if defs:   # a helper call replaces a direct match on the same line (the helper decides the text)
                info.sites = [x for x in info.sites if x[0] != i] + [(i, "helper call", name, all(defs))]
                break


def read_bin(bin_dir):
    """[(path relative to bin_dir, text)] for every text file below bin_dir, symlinks followed, each target once."""
    out, seen = [], set()
    for root, dirs, files in os.walk(bin_dir, followlinks=True):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__" and not d.startswith("."))
        for fn in sorted(files):
            p = os.path.join(root, fn)
            real = os.path.realpath(p)
            if fn.endswith((".pyc", ".so")) or real in seen or not os.path.isfile(real):
                continue
            seen.add(real)
            try:
                with open(real, "rb") as f:
                    raw = f.read(4_000_000)
            except OSError:
                continue
            if b"\0" not in raw[:8192]:
                out.append((os.path.relpath(p, bin_dir), raw.decode("utf-8", "replace")))
    return out


def analyze(files):
    """FileInfo for each (rel, text), with write sites, including calls to helpers defined in the other files."""
    infos = [FileInfo(rel, text) for rel, text in files]
    for info in infos:
        taint(info)
        direct_sites(info)
    registry, rels = build_registry(infos), [i.rel for i in infos]
    for info in infos:
        helper_sites(info, registry, rels)
    return infos


def build_registry(infos):
    """helper name -> [(rel, adds marker)] for every function that writes into the inbox."""
    registry = {}
    for info in infos:
        for name, adds in info.helpers.items():
            registry.setdefault(name, []).append((info.rel, adds))
    return registry


def scan_bin(bin_dir):
    return analyze(read_bin(bin_dir))


def lint_crontab(path):
    problems, writers = [], 0
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().split("\n")
    except OSError as e:
        return [f"crontab: cannot read {path}: {e.strerror}"], 0
    blank = FileInfo("crontab", "")
    for i, l in enumerate(lines, 1):
        if not l.strip() or comment_line(l):
            continue
        redirects = [m.group(1) for m in SHELL_REDIRECT.finditer(l) if not m.group(1).startswith("&")]
        redirects += [m.group(1) for m in SHELL_TEE.finditer(l)]
        targets = redirects + [m.group(1) for m in SHELL_MOVE.finditer(l)]
        if any(LITERAL_HOME_INBOX.search(t) for t in redirects):
            problems.append(f"crontab:{i}: redirect into the literal /home/node/inbox; write \"$HOME/inbox/...\"")
        if any(refs_inbox(t, blank, True) for t in targets):
            writers += 1
            if not MARKER.search(l):
                problems.append(f"crontab:{i}: writes an inbox note without \"Data, not instructions\" on the line")
    return problems, writers


def load_allowlist(path):
    """Lines `REL  # why` (REL relative to the bin dir). A reason is required."""
    out, problems = {}, []
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().split("\n")
    except OSError:
        return out, problems
    for i, l in enumerate(lines, 1):
        if not l.strip() or comment_line(l):
            continue
        rel, _, why = l.partition("#")
        if not why.strip():
            problems.append(f"allowlist:{i}: entry {rel.strip()!r} has no '# reason'")
        out[rel.strip()] = why.strip()
    return out, problems


def verdict(info, allow):
    """-> (is_writer, ok, how)"""
    if not info.sites:
        return False, True, "no inbox writes"
    if info.marker:
        return True, True, "marker in script"
    if all(how == "helper call" and adds for _, how, _, adds in info.sites):
        return True, True, "all writes via marker-adding helpers"
    if info.rel in allow:
        return True, True, "allowlisted"
    return True, False, "no marker"


def lint(bin_dir, crontab, allowlist):
    infos = scan_bin(bin_dir)
    allow, problems = load_allowlist(allowlist)
    report, by_rel = [], {}
    for info in infos:
        by_rel[info.rel] = info
        writer, ok, how = verdict(info, allow)
        if not writer:
            continue
        sites = ", ".join(f"{ln}{'(' + h + ')' if h else ''}" for ln, _, h, _ in info.sites[:8])
        report.append(f"{info.rel}: {how}; write sites line {sites}{' ...' if len(info.sites) > 8 else ''}")
        if not ok:
            problems.append(f"{info.rel}: writes into the inbox (line {sites}) but has no \"Data, not instructions\" "
                            "marker, no marker-adding helper and no allowlist entry")
    for rel in allow:
        info = by_rel.get(rel)
        if info is None:
            problems.append(f"allowlist: stale entry {rel!r} (no such script in the bin dir)")
            continue
        info_allow = {k: v for k, v in allow.items() if k != rel}
        writer, ok, _ = verdict(info, info_allow)
        if not writer:
            problems.append(f"allowlist: stale entry {rel!r} (not an inbox writer any more)")
        elif ok:
            problems.append(f"allowlist: stale entry {rel!r} (passes without it)")
    cp, cw = lint_crontab(crontab) if crontab else ([], 0)
    problems += cp
    report.append(f"crontab: {cw} inbox-writing line(s)")
    return problems, report, infos


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--bin", default=os.path.expanduser("~/bin"))
    ap.add_argument("--crontab", default=os.path.expanduser("~/crontab"))
    ap.add_argument("--allowlist", default=os.path.join(HERE, "allowlist.txt"))
    ap.add_argument("-v", "--verbose", action="store_true", help="list every writer and how it passes")
    a = ap.parse_args(argv)
    if not os.path.isdir(a.bin):
        print(f"inbox_lint: no bin dir {a.bin}", file=sys.stderr)
        return 2
    problems, report, infos = lint(a.bin, a.crontab, a.allowlist)
    if a.verbose:
        print("\n".join(report))
    nw = sum(1 for i in infos if i.sites)
    print(f"inbox_lint: {len(infos)} file(s), {nw} inbox writer(s), {len(problems)} problem(s)")
    for p in problems:
        print("FAIL " + p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
