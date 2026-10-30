"""Early-exit pipe lint for shell test scripts that run under `set -o pipefail`. Stdlib only.

With pipefail a pipeline's status is its last failing stage. A reader that stops before its input ends (grep -q, -l,
-L or -m N; head) closes the pipe while the writer may still be writing: the writer's next write() gets SIGPIPE, it
exits 141, and the pipeline reads as failed although the reader found what it looked for. bash's printf and echo
write a pipe one line per write(), so `printf '%s' "$out" | grep -q X` (X before the last line) turns false when the
writer is descheduled mid-output on a loaded box, and `! printf '%s' "$out" | grep -q X` turns true.

Flagged in a file with a `set … pipefail` line (comment lines, heredoc bodies and `bash -c '…'` scripts skipped):
  - a pipe into grep with -q/--quiet/--silent, -l/-L or --files-with(out)-match(es): its status is all it gives
  - a pipe into head or grep -m/--max-count when the same line tests that pipeline's status (it follows if, while,
    until, elif or !, or && / || follow it), outside a $( … ): elsewhere only the printed value is used, and the
    reader prints that in full before it exits
Race-free forms: [[ $out == *"x"* ]] or [[ $out =~ re ]] (whole string), grep -q re <<<"$out" (per line, as with the
pipe), grep -q re FILE, out=$(cmd) && grep -q re <<<"$out" (keeps cmd's status as pipefail did).
A line that keeps such a pipe on purpose (pipefail off around it, a reader that drains its input) ends with
`# pipe-ok: <why>`.

  python3 ops/lint/pipe_early_exit.py FILE...   prints file:line: message per finding; exit 1 when any
  check(source, filename) -> [str]
  scan(root) -> [str]: the findings in every shell file under root/tests (tests/ops/test_pipe_early_exit.py)
"""

from __future__ import annotations

import os
import re
import sys

ALLOW = "# pipe-ok:"
PIPEFAIL = re.compile(r"^\s*set\s[^#\n]*\bpipefail\b", re.M)
HEREDOC = re.compile(r"<<(-?)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")
# a single | (not ||), or |&, then optional env assignments / `command`, then the reader's name
READER = re.compile(r"(?<!\|)\|&?(?!\|)\s*(?:\w+=\S*\s+)*(?:command\s+)?(grep|egrep|fgrep|head)\b([^|;&)`]*)")
STATUS_BEFORE = re.compile(r"(?:^|[;&|({]|\bthen|\bdo|\belse)\s*(?:if|while|until|elif|!)\s+[^;]*$")
STATUS_AFTER = re.compile(r"^[^;&]*?(?:&&|\|\|)")
GREP_ARG = set("efABCdD")  # short options that take an argument (m too, handled on its own)
GREP_QUIET_LONG = ("--quiet", "--silent", "--files-with-matches", "--files-without-match")


def _grep_mode(args: str) -> str:
    """'quiet' (only a status), 'max' (-m N: stops after N lines) or '' for grep's option words in `args`."""
    toks = args.split()
    mode = ""
    i = 0
    while i < len(toks):
        t = toks[i]
        i += 1
        if t == "--" or not t.startswith("-") or t == "-":
            break
        if t.startswith("--"):
            if t.startswith(GREP_QUIET_LONG):
                return "quiet"
            if t.startswith("--max-count"):
                mode = "max"
                i += 0 if "=" in t else 1
            elif "=" not in t and t in ("--regexp", "--file", "--context", "--after-context", "--before-context"):
                i += 1
            continue
        for k, ch in enumerate(t[1:]):
            if ch in "qlL":
                return "quiet"
            if ch == "m":
                mode = "max"
            if ch == "m" or ch in GREP_ARG:
                i += 0 if t[k + 2 :] else 1  # the argument is the rest of the word or the next word
                break
    return mode


def _head_drains(args: str) -> bool:
    """head -n -N / -c -N (all but the last N) reads its whole input."""
    return bool(re.match(r"\s*(?:-[nc]\s*-\d|--(?:lines|bytes)=-\d)", args))


def _in_subst(before: str) -> bool:
    """The text up to a pipe leaves a $( … ) or `…` open: the pipeline's status is the substitution's, its value the
    point (quotes are not tracked: a stray paren in a string can only make this miss a finding)."""
    stack: list[str] = []
    i = 0
    while i < len(before):
        c = before[i]
        if c == "$" and before[i + 1 : i + 2] == "(":
            stack.append("$")
            i += 2
            continue
        if c == "(":
            stack.append("(")
        elif c == ")" and stack:
            stack.pop()
        elif c == "`":
            if stack and stack[-1] == "`":
                stack.pop()
            else:
                stack.append("`")
        i += 1
    return "$" in stack or "`" in stack


NEW_SHELL = re.compile(r"\b(?:bash|sh)\s+(?:-[a-z]+\s+)*-[a-z]*c\s*$")  # bash -c, sh -ec, bash -o x -c


def _in_new_shell(before: str) -> bool:
    """The text up to a pipe is inside the quoted script of a `bash -c` / `sh -c`: a new shell, pipefail off there
    (set options are not inherited)."""
    quote, opened_new, i = "", False, 0
    while i < len(before):
        c = before[i]
        if quote == '"' and c == "\\":
            i += 2
            continue
        if c in "'\"" and (not quote or quote == c):
            if quote:
                quote, opened_new = "", False
            else:
                quote, opened_new = c, bool(NEW_SHELL.search(before[:i]))
        i += 1
    return bool(quote) and opened_new


def _logical_lines(src: str):
    """(first line number, text) per command line: comment lines and heredoc bodies dropped, lines ending in a
    backslash or a pipe joined with the next."""
    lines = src.split("\n")
    i, buf, start = 0, "", 0
    while i < len(lines):
        ln = lines[i]
        i += 1
        if not buf and ln.lstrip().startswith("#"):
            continue
        if not buf:
            start = i
        buf = buf + " " + ln if buf else ln
        for dash, _q, word in HEREDOC.findall(ln.replace("<<<", "   ")):
            while i < len(lines):
                body = lines[i]
                i += 1
                if (body.lstrip("\t") if dash else body) == word:
                    break
        s = buf.rstrip()
        if s.endswith("\\") or (s.endswith("|") and not s.endswith("||")):
            buf = s[:-1] if s.endswith("\\") else s
            continue
        yield start, buf
        buf = ""
    if buf:
        yield start, buf


def check(source: str, filename: str = "<src>") -> list[str]:
    if not PIPEFAIL.search(source):
        return []
    out = []
    for no, line in _logical_lines(source):
        if ALLOW in line:
            continue
        for m in READER.finditer(line):
            name, args = m.group(1), m.group(2)
            if _in_new_shell(line[: m.start()]):
                continue
            if name == "head":
                if _head_drains(args):
                    continue
                mode = "max"
            else:
                mode = _grep_mode(args)
            if not mode:
                continue
            if mode == "max":
                before, after = line[: m.start()], line[m.end() :]
                if _in_subst(before) or not (STATUS_BEFORE.search(before) or STATUS_AFTER.match(after)):
                    continue
                what = f"`| {name}` whose status the line tests"
            else:
                what = f"`| {name} -q`-style reader"
            out.append(
                f"{filename}:{no}: {what} under pipefail: the writer can die of SIGPIPE after the match "
                "(use [[ ]], grep … <<<\"$x\" or a file; or end the line with '# pipe-ok: why')"
            )
    return out


def shell_files(root: str) -> list[str]:
    """Shell scripts under root/tests (by .sh/.bash name or a sh/bash shebang), sorted, relative to root."""
    found = []
    for dp, dns, fns in os.walk(os.path.join(root, "tests")):
        dns[:] = sorted(d for d in dns if not d.startswith((".", "__pycache__", "node_modules")))
        for fn in sorted(fns):
            p = os.path.join(dp, fn)
            if fn.endswith((".sh", ".bash")):
                found.append(p)
                continue
            if "." in fn or os.path.islink(p):
                continue
            try:
                with open(p, "rb") as fh:
                    head = fh.readline(200)
            except OSError:
                continue
            if head.startswith(b"#!") and re.search(rb"\b(ba)?sh\b", head):
                found.append(p)
    return [os.path.relpath(p, root) for p in found]


def scan(root: str) -> list[str]:
    out = []
    for rel in shell_files(root):
        with open(os.path.join(root, rel), encoding="utf-8", errors="replace") as fh:
            out += check(fh.read(), rel)
    return out


def main(argv: list[str]) -> int:
    bad = 0
    for f in argv:
        with open(f, encoding="utf-8", errors="replace") as fh:
            found = check(fh.read(), f)
        for line in found:
            print(line)
        bad += len(found)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
