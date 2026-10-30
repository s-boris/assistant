#!/usr/bin/env python3
"""lint_deps.py: static tool-dependency lint for ~/bin (part of the boot test suite; nothing is executed).

Every external command a ~/bin script depends on must be a shell builtin, a ~/bin script, provided by the
container image, installed by ~/boot.sh, or on the allowlist below (one-line reason each). References found:
  shell    `command -v X`, `type -P X`, `which X` in command position; comments, quoted text and heredoc
           bodies (often scripts run on another host) are skipped
  python   shutil.which("X"); subprocess run/call/check_call/check_output/Popen with an argv list, also through
           local helpers that pass their first parameter on; argv[0] is resolved through module constants such
           as NAME = os.environ.get("VAR") or "tool" and literal ~/bin, ~/opt paths
  shebang  `#!/usr/bin/env X` and absolute interpreters (a venv under ~/opt must be built by boot.sh)
Names that can't be resolved statically (variables, shell strings, other paths) are counted as skipped.
Sets: image = image-baseline.txt (commands on the system PATH that came with the image; regenerate after an
image change with `lint_deps.py --gen-baseline > image-baseline.txt`), boot.sh = BOOT_TOOLS/BOOT_FILES (credited
only while boot.sh still names the package or path), ~/bin = the files in the scanned directory.
Output: one "ok: ..." / "FAIL: ..." line per check, details indented below, info lines; exit 1 on any FAIL.
Usage: lint_deps.py [--bin DIR] [--boot FILE] [--baseline FILE] [--home DIR] [--only provided] [--verbose]
       lint_deps.py --gen-baseline
"""
import ast
import glob
import gzip
import os
import pwd
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# Packages boot.sh installs -> the commands scripts use from them. A package's commands count as provided only
# while boot.sh still names the package; every package here must stay in boot.sh and be used somewhere.
BOOT_TOOLS = {
    "openssh-client": "ssh scp sftp ssh-keygen ssh-keyscan",
    "img2pdf": "img2pdf",
    "age": "age age-keygen",
    "faketime": "faketime",
    "nmap": "nmap",
    "ffmpeg": "ffmpeg ffprobe",
    "rsync": "rsync",
    "poppler-utils": "pdftotext pdfinfo pdftoppm pdfimages",
    "tesseract-ocr": "tesseract",
    "google-chrome-stable": "google-chrome google-chrome-stable",
    "gh": "gh",
    "gcc": "gcc",
}
# Commands boot.sh copies into place: name -> path text boot.sh must contain.
BOOT_FILES = {
    "start-claude": "/usr/local/bin/start-claude",
    "heartbeat": ".local/bin/heartbeat",
    "python3.13": "/usr/local/bin/python3.13",   # link to uv's ~/.local/bin/python3.13: the scripts' shebang, as on the CI runner
}
# Used but neither in the image nor installed by boot.sh. Each entry must still be referenced by a script.
ALLOW = {
    "uv": "standalone binary in the persistent home (~/.local/bin/uv, from ~/opt/assistant/ops/ci/install-uv.sh); "
          "survives a container recreate, and every caller falls back to that path when it isn't on PATH",
    "zbarimg": "deliberately not installed: local QR payload decoding stays off for privacy; bills falls back without it",
}
# Not listed: build-essential and cmake. Nothing looks them up; llm-server-ctl installs them itself before a rebuild.
# boot.sh packages no script uses (kept on purpose).
UNUSED_OK = {
    "xdotool": "installed with the browser stack, no script uses it (drop candidate)",
}
BUILTINS = set("""alias bg bind break builtin caller cd command compgen complete compopt continue declare dirs disown
echo enable eval exec exit export false fc fg getopts hash help history jobs kill let local logout mapfile popd printf
pushd pwd read readarray readonly return set shift shopt source suspend test times trap true type typeset ulimit umask
unalias unset wait . : [ [[ case do done elif else esac fi for function if in select then time until while""".split())
SYS_PATH = ("/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin")
SUBPROCESS_FUNCS = {"run", "call", "check_call", "check_output", "Popen"}
NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.+-]*$")
# Usage scan (is each boot.sh package still used?): these trees under the home, text files only.
USAGE_ROOTS = ("bin", ".claude/skills", ".claude/agents", "opt/*-tests", "opt/assistant/tests/legacy", "opt/claude-vault/tests",
               "plugins", "crontab")   # opt/assistant/tests/legacy: the suites moved from ~/opt/*-tests (30.10)
USAGE_SKIP_SUITE = os.sep + os.path.join("tests", "legacy", "boot")   # this lint's own lists, in whichever checkout
USAGE_SKIP_DIRS = {"node_modules", ".git", "__pycache__", "fixtures"}
SHELLS = {"sh", "bash", "dash"}


def real_home():
    try:
        return pwd.getpwuid(os.getuid()).pw_dir
    except KeyError:
        return os.path.expanduser("~")


# ---------- shell ----------

def shell_code(text):
    """text with comments, quoted literal text and heredoc bodies blanked (newlines kept); command
    substitutions inside double quotes stay code. -> (code, line numbers of blanked spans holding a lookup)"""
    out, i, n, hidden = [], 0, len(text), []
    stack = ["code"]             # code | dq | bt ; code entries track $( ) depth in depth[]
    depth = [0]
    pending = []                 # heredoc terminators waiting for the next newline
    look = re.compile(r"\b(command\s+-[vV]|type\s+-[pP]|which)\b")

    def blank(s, fill=" "):
        return "".join("\n" if c == "\n" else fill for c in s)

    def hide(seg, at):
        if look.search(seg):
            hidden.append(text.count("\n", 0, at) + 1)

    while i < n:
        c, top = text[i], stack[-1]
        if top == "dq":
            if c == "\\" and i + 1 < n:
                out.append("  " if text[i + 1] != "\n" else " \n"); i += 2; continue
            if c == '"':
                stack.pop(); depth.pop(); out.append(c); i += 1; continue
            if text.startswith("$(", i):
                stack.append("code"); depth.append(0); out.append("$("); i += 2; continue
            if c == "`":
                stack.append("bt"); depth.append(0); out.append(c); i += 1; continue
            j = i
            while j < n and text[j] not in '\\"`$':
                j += 1
            if j == i:
                j = i + 1
            seg = text[i:j]
            hide(seg, i)
            out.append(blank(seg, "_")); i = j; continue
        # code or backtick context
        if c == "\n" and pending:
            out.append(c); i += 1
            while pending:
                word, strip_tabs = pending.pop(0)
                start = i
                while i < n:
                    e = text.find("\n", i)
                    e = n if e < 0 else e
                    line = text[i:e]
                    i = min(e + 1, n)
                    if (line.lstrip("\t") if strip_tabs else line) == word:
                        break
                body = text[start:i]
                hide(body, start)
                out.append(blank(body))
            continue
        if c == "\\" and i + 1 < n:
            out.append(text[i:i + 2]); i += 2; continue
        if c == "#" and (i == 0 or text[i - 1] in " \t\n;&|()"):
            e = text.find("\n", i)
            e = n if e < 0 else e
            out.append(blank(text[i:e])); i = e; continue
        if c == "'":
            e = text.find("'", i + 1)
            e = n - 1 if e < 0 else e
            seg = text[i:e + 1]
            hide(seg, i)
            out.append("'" + blank(seg[1:-1], "_") + "'"); i = e + 1; continue
        if c == '"':
            stack.append("dq"); depth.append(0); out.append(c); i += 1; continue
        if c == "`":
            if top == "bt":
                stack.pop(); depth.pop()
            else:
                stack.append("bt"); depth.append(0)
            out.append(c); i += 1; continue
        if text.startswith("<<", i) and not text.startswith("<<<", i):
            m = re.match(r"<<(-?)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2", text[i:])
            if m:
                pending.append((m.group(3), m.group(1) == "-"))
                out.append(m.group(0)); i += len(m.group(0)); continue
        if c == "(":
            depth[-1] += 1
        elif c == ")":
            depth[-1] -= 1
            if depth[-1] < 0 and len(stack) > 1 and top == "code":
                stack.pop(); depth.pop()
        out.append(c); i += 1
    return "".join(out), hidden


SHELL_LOOKUP = re.compile(r"(?:^|[;&|(!{`]|\b(?:then|do|else|if|elif|while|until)\b)[ \t]*"
                       r"(command[ \t]+-[vV]|type[ \t]+-[pP]a?|which)[ \t]+([^;&|<>()`\n]*)", re.M)


def scan_shell(text, add, skip):
    code, hidden = shell_code(text)
    for line in hidden:
        skip.hit("quoted or heredoc", line, "lookup inside quotes or a heredoc")
    for m in SHELL_LOOKUP.finditer(code):
        line = code.count("\n", 0, m.start(1)) + 1
        how = re.sub(r"\s+", " ", m.group(1))
        for w in m.group(2).split():
            if w.startswith("-") or w.isdigit():
                continue
            if NAME_RE.match(w):
                add(w, line, how)
            else:
                skip.hit("dynamic", line, f"{how} {w}")


# ---------- python ----------

class PyScan:
    """Collect tool references from one Python module."""

    def __init__(self, tree, add, skip):
        self.add, self.skip = add, skip
        self.consts = {}        # module-level NAME -> value node (single assignment only)
        seen = set()
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                k = node.targets[0].id
                if k in seen:
                    self.consts.pop(k, None)
                else:
                    self.consts[k] = node.value
                seen.add(k)
        self.sp_mods, self.sp_funcs, self.which_funcs, self.sh_mods = {"subprocess"}, {}, set(), {"shutil"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name == "subprocess":
                        self.sp_mods.add(a.asname or a.name)
                    if a.name == "shutil":
                        self.sh_mods.add(a.asname or a.name)
            elif isinstance(node, ast.ImportFrom) and node.module in ("subprocess", "shutil"):
                for a in node.names:
                    if node.module == "subprocess" and a.name in SUBPROCESS_FUNCS:
                        self.sp_funcs[a.asname or a.name] = a.name
                    if node.module == "shutil" and a.name == "which":
                        self.which_funcs.add(a.asname or a.name)
        # local helpers that hand their first parameter to subprocess as argv
        self.wrappers = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.args.args:
                p = node.args.args[0].arg
                if p == "self" and len(node.args.args) > 1:
                    continue
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call) and self.is_subprocess(sub) and sub.args:
                        a = sub.args[0]
                        if (isinstance(a, ast.Name) and a.id == p) or (
                                isinstance(a, (ast.List, ast.Tuple)) and a.elts and isinstance(a.elts[0], ast.Starred)
                                and isinstance(a.elts[0].value, ast.Name) and a.elts[0].value.id == p):
                            self.wrappers.add(node.name)
                            break
        self.tree = tree

    def is_subprocess(self, call):
        f = call.func
        if isinstance(f, ast.Attribute) and f.attr in SUBPROCESS_FUNCS and isinstance(f.value, ast.Name):
            return f.value.id in self.sp_mods
        return isinstance(f, ast.Name) and f.id in self.sp_funcs

    def is_which(self, call):
        f = call.func
        if isinstance(f, ast.Attribute) and f.attr == "which" and isinstance(f.value, ast.Name):
            return f.value.id in self.sh_mods
        return isinstance(f, ast.Name) and f.id in self.which_funcs

    def lit(self, node, d=0):
        """Static string value of node ('~' stands for the home), or None."""
        if d > 8 or node is None:
            return None
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in ("HOME", "home"):
                return "~"
            if node.id in self.consts:
                return self.lit(self.consts[node.id], d + 1)
            return None
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            return self.lit(node.values[-1], d + 1)       # the fallback default
        if isinstance(node, ast.IfExp):                   # "path if exists(path) else name": the fallback
            v = self.lit(node.orelse, d + 1)
            return v if v is not None else self.lit(node.body, d + 1)
        if isinstance(node, ast.JoinedStr):
            parts = []
            for v in node.values:
                if isinstance(v, ast.Constant):
                    parts.append(str(v.value))
                elif isinstance(v, ast.FormattedValue) and self.lit(v.value, d + 1) == "~":
                    parts.append("~")
                else:
                    return None
            return "".join(parts)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):   # Path / "x"
            a, b = self.lit(node.left, d + 1), self.lit(node.right, d + 1)
            return a + "/" + b if a is not None and b is not None else None
        if isinstance(node, ast.Call):
            f = node.func
            fn = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""
            if fn == "home" and not node.args:                              # Path.home()
                return "~"
            lam = self.consts.get(fn) if isinstance(f, ast.Name) else None   # P = lambda *a: os.path.join(X, *a)
            if isinstance(lam, ast.Lambda) and lam.args.vararg and isinstance(lam.body, ast.Call) \
                    and lam.body.args and isinstance(lam.body.args[-1], ast.Starred):
                inner = ast.Call(func=lam.body.func, args=lam.body.args[:-1] + node.args, keywords=[])
                return self.lit(inner, d + 1)
            if fn in ("expanduser", "str", "Path", "abspath", "realpath") and len(node.args) == 1:
                return self.lit(node.args[0], d + 1)
            if fn == "join" and isinstance(f, ast.Attribute) and node.args:  # os.path.join
                parts = [self.lit(a, d + 1) for a in node.args]
                return "/".join(p.rstrip("/") for p in parts) if None not in parts else None
            if fn == "get" and len(node.args) == 2:                          # os.environ.get(K, default)
                return self.lit(node.args[1], d + 1)
        return None

    def argv0(self, node, d=0):
        """First element of an argv expression, or None."""
        if d > 8:
            return None
        if isinstance(node, (ast.List, ast.Tuple)):
            if not node.elts:
                return None
            e = node.elts[0]
            return self.argv0(e.value, d + 1) if isinstance(e, ast.Starred) else e
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return self.argv0(node.left, d + 1)
        if isinstance(node, ast.Name) and node.id in self.consts and isinstance(
                self.consts[node.id], (ast.List, ast.Tuple, ast.BinOp)):
            return self.argv0(self.consts[node.id], d + 1)
        return None

    def run(self):
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Call):
                continue
            if self.is_which(node):
                v = self.lit(node.args[0]) if node.args else None
                self.ref(v, node.lineno, "shutil.which")
                continue
            sp = self.is_subprocess(node)
            if not sp and not (isinstance(node.func, ast.Name) and node.func.id in self.wrappers):
                continue
            how = "subprocess" if sp else f"{node.func.id}() helper"
            arg = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg == "args"), None)
            if arg is None:
                continue
            if any(k.arg == "shell" for k in node.keywords) or isinstance(arg, (ast.Constant, ast.JoinedStr)):
                self.skip.hit("shell string", node.lineno, how)
                continue
            first = self.argv0(arg)
            if first is None:
                self.skip.hit("dynamic", node.lineno, f"{how} {ast.unparse(arg)[:60]}")
                continue
            self.ref(self.lit(first), node.lineno, how)

    def ref(self, v, line, how):
        if v is None:
            self.skip.hit("dynamic", line, how)
        else:
            self.add(v, line, how)


# ---------- files ----------

def classify(path):
    """-> ('python' | 'shell' | None, shebang interpreter or None)"""
    try:
        with open(path, "rb") as f:
            head = f.read(256)
    except OSError:
        return None, None
    first = head.split(b"\n", 1)[0].decode("utf-8", "replace")
    if first.startswith("#!"):
        words = first[2:].split()
        if not words:
            return None, None
        interp = words[0]
        if os.path.basename(interp) == "env":
            rest = [w for w in words[1:] if not w.startswith("-") and "=" not in w]
            interp = rest[0] if rest else "env"
        base = os.path.basename(interp)
        if base.startswith("python"):
            return "python", interp
        if base in SHELLS:
            return "shell", interp
        return "other", interp
    if path.endswith(".py"):
        return "python", None
    return None, None


def scripts(bin_dir):
    out = []
    for d in (bin_dir, os.path.join(bin_dir, "lib")):
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            p = os.path.join(d, name)
            if name.startswith(".") or name.endswith((".bak", ".orig", ".new", ".tmp", ".wip", ".pyc")) \
                    or not os.path.isfile(p):
                continue
            out.append(p)
    return out


def boot_text(path):
    """boot.sh without comment lines/tails."""
    with open(path, errors="replace") as f:
        return "\n".join(re.sub(r"(^|\s)#.*", "", line) for line in f)


def load_baseline(path):
    with open(path) as f:
        return {w for line in f for w in [line.split("#")[0].strip()] if w}


# ---------- baseline generation ----------

def gen_baseline():
    """Commands on the system PATH that came with the container image: everything executable there minus what
    apt installed later (history runs with a Requested-By line: boot.sh and manual installs) and minus the
    files boot.sh copies into place."""
    owner = {}
    for lst in glob.glob("/var/lib/dpkg/info/*.list"):
        pkg = os.path.basename(lst)[:-5].split(":")[0]
        with open(lst, errors="replace") as f:
            for line in f:
                owner.setdefault(line.rstrip("\n"), pkg)
    later = set()
    for h in sorted(glob.glob("/var/log/apt/history.log*")):
        op = gzip.open if h.endswith(".gz") else open
        with op(h, "rt", errors="replace") as f:
            for block in f.read().split("\n\n"):
                if "Requested-By:" not in block:
                    continue
                for line in block.splitlines():
                    if line.startswith("Install: "):
                        for item in line[9:].split("), "):
                            later.add(item.split(" (")[0].split(":")[0].strip())

    def pkgs(p):
        cands = {p, os.path.realpath(p)}
        for c in list(cands):
            if c.startswith("/usr/"):
                cands.add(c[4:])
            else:
                cands.add("/usr" + c)
        return {owner[c] for c in cands if c in owner}

    names = set()
    for d in SYS_PATH:
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if os.path.isdir(p) or not os.access(p, os.X_OK) or p in BOOT_FILES.values():
                continue
            if pkgs(p) & later:
                continue
            names.add(name)
    print("# Commands on the system PATH that come with the container image (one per line), used by lint_deps.py.")
    print("# Generated by `lint_deps.py --gen-baseline`: excludes everything apt installed after the image build")
    print("# (boot.sh and manual installs) and the files boot.sh copies into place. Regenerate after an image change.")
    for n in sorted(names):
        print(n)
    return 0


# ---------- main ----------

class Skips(dict):
    """Counts of references that can't be checked statically, plus their sites (for --verbose)."""

    def __init__(self, kinds):
        super().__init__((k, 0) for k in kinds)
        self.sites, self.cur = [], ""

    def hit(self, kind, line, what):
        self[kind] += 1
        self.sites.append((kind, f"{self.cur}:{line}", what))


def main(argv):
    home = real_home()
    opts = {"--bin": None, "--boot": None, "--baseline": os.path.join(HERE, "image-baseline.txt"),
            "--home": None, "--only": None, "--verbose": False}
    it = iter(argv)
    for a in it:
        if a == "--gen-baseline":
            return gen_baseline()
        if a == "--verbose":
            opts[a] = True
        elif a in opts:
            opts[a] = next(it, None)
            if opts[a] is None:
                print(f"lint_deps: {a} needs a value", file=sys.stderr)
                return 2
        else:
            print(__doc__.strip(), file=sys.stderr)
            return 2
    home = opts["--home"] or home
    bin_dir = opts["--bin"] or os.path.join(home, "bin")
    boot = opts["--boot"] or os.path.join(home, "boot.sh")
    t0 = time.monotonic()
    results = []                      # (ok, desc, [detail lines])

    def check(ok, desc, details=()):
        results.append((bool(ok), desc, list(details)))

    def tilde(p):
        return "~" + p[len(home):] if p.startswith(home + "/") else p

    btext = boot_text(boot)
    baseline = load_baseline(opts["--baseline"])
    bin_names = {n for n in os.listdir(bin_dir) if not n.startswith(".")} if os.path.isdir(bin_dir) else set()
    boot_pkgs = {p for p in BOOT_TOOLS if re.search(r"(?<![\w.-])" + re.escape(p) + r"(?![\w.-])", btext)}
    boot_cmds = {t: p for p in boot_pkgs for t in BOOT_TOOLS[p].split()}
    boot_cmds.update({n: "boot.sh copy" for n, path in BOOT_FILES.items() if path in btext})
    boot_venvs = set(re.findall(r"/opt/([A-Za-z0-9._-]+)", btext))

    refs = {}                          # key -> [(file, line, how)]
    skip = Skips(("dynamic", "shell string", "quoted or heredoc", "other path", "not a script", "other language"))
    parse_fail, nfiles = [], {"python": 0, "shell": 0}

    for path in scripts(bin_dir):
        kind, interp = classify(path)
        rel = skip.cur = os.path.relpath(path, bin_dir)
        if kind is None:
            skip.hit("not a script", 0, "")
            continue
        if kind == "other":
            skip.hit("other language", 0, interp)
            continue
        nfiles[kind] += 1

        def add(v, line, how, rel=rel):
            key = norm(v)
            if key is None:
                skip.hit("other path", line, f"{how} {v}")
            else:
                refs.setdefault(key, []).append((rel, line, how))

        def norm(v):
            """'name' | '~/bin/X' | '~/opt/DIR' key for a referenced command, or None (skipped)."""
            if v.startswith(home + "/"):
                v = "~" + v[len(home):]
            if "/" not in v:
                return v if NAME_RE.match(v) else None
            m = re.match(r"^~/bin/([^/]+)$", v)
            if m:
                return "~/bin/" + m.group(1)
            m = re.match(r"^~/opt/([^/]+)/", v)
            if m:
                return "~/opt/" + m.group(1)
            if os.path.dirname(v) in SYS_PATH:
                return os.path.basename(v)
            return None

        if interp:
            add(interp, 1, "shebang")
        try:
            with open(path, errors="replace") as f:
                text = f.read()
        except OSError as e:
            parse_fail.append(f"{rel}: {e}")
            continue
        if kind == "shell":
            scan_shell(text, add, skip)
        else:
            try:
                tree = ast.parse(text, filename=rel)
            except SyntaxError as e:
                parse_fail.append(f"{rel}:{e.lineno}: {e.msg}")
                continue
            PyScan(tree, add, skip).run()

    def provided(key):
        if key.startswith("~/bin/"):
            return "~/bin" if key[6:] in bin_names else None
        if key.startswith("~/opt/"):
            if key[6:] in boot_venvs:
                return "boot.sh venv"
            d = os.path.join(home, "opt", key[6:])     # persistent home content; a venv must come from boot.sh
            return "~/opt" if os.path.isdir(d) and not os.path.exists(os.path.join(d, "pyvenv.cfg")) else None
        if key in BUILTINS:
            return "builtin"
        if key in bin_names:
            return "~/bin"
        if key in boot_cmds:
            return "boot.sh"
        if key in baseline:
            return "image"
        return None

    missing, by_src, srcs = [], {}, {}
    for key in sorted(refs):
        src = srcs[key] = provided(key) or ("allowlist" if key in ALLOW else None)
        if src is None:
            locs = ", ".join(f"{f}:{ln} ({how})" for f, ln, how in refs[key][:3])
            more = f" +{len(refs[key]) - 3} more" if len(refs[key]) > 3 else ""
            missing.append(f"  {key}: {locs}{more}")
        else:
            by_src[src] = by_src.get(src, 0) + 1

    nref = sum(len(v) for v in refs.values())
    check(not parse_fail, f"all {sum(nfiles.values())} scripts parse", ["  " + p for p in parse_fail])
    check(not missing, f"every tool the scripts look up or run is provided ({len(refs)} tools, {nref} references)",
          missing + (["  -> add it to boot.sh (and BOOT_TOOLS), or to ALLOW with a reason"] if missing else []))

    if opts["--only"] != "provided":
        gone = [p for p in BOOT_TOOLS if p not in boot_pkgs] + \
               [n for n, path in BOOT_FILES.items() if path not in btext]
        check(not gone, "every BOOT_TOOLS/BOOT_FILES entry is still in boot.sh",
              [f"  {g}: not in {tilde(boot)} (drop it from the map)" for g in gone])
        masked = sorted(({t for p in boot_pkgs for t in BOOT_TOOLS[p].split()}
                         | {n for n, path in BOOT_FILES.items() if os.path.dirname(path) in SYS_PATH}) & baseline)
        check(len(baseline) > 100 and {"sh", "python3"} <= baseline and not masked,
              f"image baseline holds {len(baseline)} commands, none of them installed by boot.sh",
              [f"  {m}: in {os.path.basename(opts['--baseline'])} but installed by boot.sh (regenerate it)"
               for m in masked])
        stale = [k for k in ALLOW if k not in refs or provided(k)]
        check(not stale, f"every allowlist entry is still needed ({len(ALLOW)} entries)",
              [f"  {k}: " + (f"now provided by {provided(k)}" if k in refs else "no longer referenced")
               + " (drop it from ALLOW)" for k in stale])
        gone_img = [k for k, v in srcs.items() if v == "image"
                    and not any(os.access(os.path.join(d, k), os.X_OK) for d in SYS_PATH)]
        check(not gone_img, "every image command the scripts use exists on this system's PATH",
              [f"  {k}: in the baseline but not installed here (image changed? regenerate the baseline, then "
               "provide it via boot.sh)" for k in gone_img])
        used = usage_words(home)
        unused = [p for p in sorted(boot_pkgs) if p not in UNUSED_OK
                  and not any(t in used for t in BOOT_TOOLS[p].split())]
        stale_u = [p for p in UNUSED_OK if p in boot_pkgs and any(t in used for t in BOOT_TOOLS[p].split())]
        check(not unused and not stale_u,
              f"every boot.sh package in the map is used somewhere ({len(UNUSED_OK)} known unused)",
              [f"  {p}: none of {BOOT_TOOLS[p]} appears in scripts, skills, tests or plugins" for p in unused]
              + [f"  {p}: in UNUSED_OK but used now (drop it there)" for p in stale_u])

    nf = sum(1 for r in results if not r[0])
    secs = time.monotonic() - t0
    print(f"lint_deps: {nfiles['python']} python + {nfiles['shell']} shell scripts in {tilde(bin_dir)}, "
          f"{len(refs)} tools ({nref} references), {secs:.2f}s")
    print("  provided by: " + ", ".join(f"{k} {v}" for k, v in sorted(by_src.items(), key=lambda kv: -kv[1])))
    print("  skipped: " + ", ".join(f"{k} {v}" for k, v in skip.items()))
    if opts["--verbose"]:
        for key in sorted(refs):
            print(f"    {key}: {srcs[key] or 'MISSING'} ({len(refs[key])}x, {refs[key][0][0]}:{refs[key][0][1]})")
        for kind, where, what in skip.sites:
            print(f"    skip {kind}: {where} {what}")
    for ok, desc, details in results:
        print(("ok: " if ok else "FAIL: ") + desc)
        if not ok:
            for d in details:
                print(d)
    return 1 if nf else 0


def usage_words(home):
    """Every word-like token in the usage-scan trees (text files under 512 KB)."""
    words = set()
    tok = re.compile(r"[A-Za-z0-9_+-]+(?:\.[A-Za-z0-9_+-]+)*")
    for pat in USAGE_ROOTS:
        for root in glob.glob(os.path.join(home, pat)):
            if os.path.realpath(root) == HERE:            # not this lint's own lists
                continue
            files = [root] if os.path.isfile(root) else []
            for dp, dns, fns in os.walk(root):
                dns[:] = [d for d in dns if d not in USAGE_SKIP_DIRS and not d.endswith("venv")
                          and not os.path.join(dp, d).endswith(USAGE_SKIP_SUITE)]
                files += [os.path.join(dp, f) for f in fns]
            for p in files:
                try:
                    if os.path.getsize(p) > 512 * 1024:
                        continue
                    with open(p, "rb") as f:
                        data = f.read()
                except OSError:
                    continue
                if b"\0" in data[:1024]:
                    continue
                words.update(tok.findall(data.decode("utf-8", "replace")))
    return words


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
