#!/usr/bin/python3 -I
"""docs-lint: TOOLS.md <-> ~/bin, skills/agents <-> ~/bin drift lint (read-only; never runs a script).

  python3 -I lint.py [--bin DIR] [--doc FILE] [--allow FILE] [--skills DIR] [--agents DIR] [--names FILE]... [--syspath P] [-v]

Checks, both directions:
  undocumented  every executable top-level entry of DIR (file or symlink; not lib/ or other folders, not copies
                named *.orig/*.new/*.bak/*.old/*.tmp/*.swp/*~) is named in DOC: as a backticked token (`name`,
                `name args`, `a`/`name`) or as ~/bin/name. A file reached through a documented symlink counts as
                documented (secretscan -> secretscan.py).
  missing       every ~/bin/PATH in DOC exists under DIR, and every script DOC presents (the first backtick span
                of a bullet, or a span joined to it with " + ", whose first word is a plain command name) is in DIR.
  allowlist     ALLOW lines: `undocumented<TAB>NAME<TAB>reason` (internal helper) or `notscript<TAB>NAME<TAB>reason`
                (a bullet-leading command that is not one of my scripts). An entry without a reason, or one that no
                longer matches anything (stale), is an error too.
With --skills (a skills dir: */SKILL.md), also:
  skill paths   every ~/bin/PATH a SKILL.md names exists under DIR.
  skill cmds    every command a SKILL.md tells me to run (first word of a backtick span or fenced-code line, and of
                each " | " / " && " / "; " segment, that is a plain name with arguments or a hyphenated name) is in
                DIR, on the system PATH (--syspath, default the standard dirs, not ~/bin), a token of a ~/bin script
                the same skill uses (then it is that script's subcommand), or allowlisted. Single bare words are
                prose (`status`, `get`) and are skipped.
  subcommands   `script word ...` with script in DIR: `word` must appear as a token in the script's source (its
                dispatcher or usage), unless the script forwards elsewhere (allowlist nosubcheck).
  skill names   every "skill X" / "skills X / Y" (or "X and Y"; commas end the list) in the --names docs (default: TOOLS.md, JOBS.md, CLAUDE.md next to
                --doc) and in the SKILL.md files names a skill dir with a SKILL.md (plugin skills a:b are skipped).
With --agents (a dir of subagent definitions *.md), also:
  agent cmds    the skill paths / cmds / subcommands checks above, on every agent file ("agent X" in findings).
  agent tools   frontmatter name: equals the file name; every tools: entry is a built-in tool or mcp__*.
  agent names   every agent type named in the --names docs, skills and agents ("`X` subagent", "subagent `X`",
                "agent type X", "x-y agent", subagent_type X) is a file X.md there or a built-in type.
  More allowlist kinds: skillcmd NAME (not a script: CLI outside the system PATH, MCP tool), nosubcheck SCRIPT
  (subcommands live elsewhere), notskill WORD (prose after "skill"), notagent WORD (prose matched as an agent type), agenttool NAME
  (a tool name the lint does not know). Stale ones fail when --skills (skill kinds) / --agents runs; skill kinds
  count uses by agents too, so run both together.
Exit 0 = no drift; 1 = drift (one line per finding); 2 = usage / unreadable input.
"""
import os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
COPY = re.compile(r"(\.(orig|new|bak|old|tmp|swp)(\.\w+)?|~)$|\.bak\.|\.orig\.")
CMD = re.compile(r"^[a-z][a-z0-9_.-]*$")


def die(msg):
    print("docs-lint: " + msg, file=sys.stderr)
    sys.exit(2)


def bin_entries(bindir):
    out = {}
    for n in sorted(os.listdir(bindir)):
        p = os.path.join(bindir, n)
        if n.startswith(".") or n == "__pycache__" or COPY.search(n):
            continue
        if os.path.islink(p) or (os.path.isfile(p) and os.access(p, os.X_OK)):
            if os.path.isdir(p):
                continue
            out[n] = p
    return out


def spans(text):
    return re.findall(r"`([^`\n]+)`", text)


def mentioned(name, doc_spans, doc):
    tok = re.compile(r"(?<![\w.~/-])" + re.escape(name) + r"(?![\w-])")
    if re.search(r"~/bin/" + re.escape(name) + r"(?![\w.-])", doc):
        return True
    return any(tok.search(s) for s in doc_spans)


def presented(doc):
    """(line number, name) of scripts the doc presents: a bullet's leading span plus the spans chained to it
    with ', ' or ' + ' (each optionally after a '(...)' note), e.g. `a`, `b` (skill x) + `c --flag`."""
    out = []
    for i, line in enumerate(doc.splitlines(), 1):
        m = re.match(r"\s*- `([^`]+)`", line)
        if not m:
            continue
        chain, pos = [m.group(1)], m.end()
        link = re.compile(r"\s*(?:\([^()]*\))?\s*(?:,|\+)\s*`([^`]+)`")
        while (n := link.match(line, pos)):
            chain.append(n.group(1))
            pos = n.end()
        for s in chain:
            w = s.split()[0] if s.split() else ""
            if w.startswith("~/bin/"):
                continue  # checked as a path
            if CMD.match(w):
                out.append((i, w))
    return out


SYSPATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
PROSE = {"a", "an", "the", "it", "is", "that", "this", "only", "named", "and", "or", "to", "for", "from", "with",
         "of", "in", "on", "which", "can", "will", "file", "files", "dir", "list", "first", "says", "loads", "uses",
         "runs", "names", "needs", "has", "does", "should", "must", "may", "would", "itself"}
SUB = re.compile(r"^[a-z][a-z0-9-]*$")


def token_in(word, text):
    return re.search(r"(?<![\w-])" + re.escape(word) + r"(?![\w-])", text) is not None


def read_src(path):
    try:
        with open(os.path.realpath(path), encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def skill_cmds(text):
    """(line number, words) for every command-shaped segment: inline backtick spans and fenced-code lines."""
    out, fenced = [], False
    for i, line in enumerate(text.splitlines(), 1):
        if re.match(r"\s*```", line):
            fenced = not fenced
            continue
        for s in ([line] if fenced else spans(line)):
            for seg in re.split(r" \| | && |; ", s):
                w = seg.split()
                if w:
                    out.append((i, w))
    return out


def skill_refs(text):
    """(line number, name) for "skill X" and "skills X / Y / Z" (plural only as a list of two or more)."""
    out = []
    pat = re.compile(r"\b(skills?) `?([a-z][a-z0-9:-]*)`?((?:\s*(?:/|and|or)\s*`?[a-z][a-z0-9:-]*`?)*)")
    for i, line in enumerate(text.splitlines(), 1):
        for m in pat.finditer(line):
            names = [m.group(2)] + re.findall(r"`?([a-z][a-z0-9:-]*)`?", m.group(3))
            names = [n for n in names if n not in ("and", "or")]
            if m.group(1) == "skills" and len(names) < 2:
                continue
            for n in names:
                n = n.rstrip(":-")
                if n and ":" not in n and n not in PROSE:
                    out.append((i, n))
    return out


TOOLS = {"Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "LS", "Bash", "BashOutput", "KillShell", "WebSearch",
         "WebFetch", "NotebookEdit", "NotebookRead", "TodoWrite", "Agent", "Task", "Skill", "ToolSearch", "Monitor",
         "SendMessage", "TaskStop", "AskUserQuestion", "ExitPlanMode", "EnterWorktree", "ExitWorktree", "Artifact",
         "ArtifactComments", "ArtifactData"}
BUILTIN_AGENTS = {"general-purpose", "Explore", "Plan", "claude-code-guide", "statusline-setup", "fork"}
AGENT_REF = [re.compile(p) for p in (
    r"`([a-z][a-z0-9-]*)` (?:sub)?agent\b",
    r"\b(?:sub)?agent(?: type)? `([A-Za-z][A-Za-z0-9-]*)`",
    r"\bagent type ([A-Za-z][A-Za-z0-9-]*)",
    r"\b([a-z][a-z0-9]*-[a-z0-9-]*[a-z0-9]) (?:sub)?agent\b",
    r"\bsubagent ([a-z][a-z0-9]*-[a-z0-9-]*[a-z0-9])\b",
    r"subagent_type[\"': =]+([A-Za-z][A-Za-z0-9-]*)")]


def agent_refs(text):
    """(line number, name) for "`X` subagent", "subagent `X`", "agent type X", "x-y agent", subagent_type X."""
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        for p in AGENT_REF:
            out += [(i, m.group(1)) for m in p.finditer(line) if m.group(1) not in PROSE]
    return list(dict.fromkeys(out))


def read_doc(f):
    try:
        return open(f, encoding="utf-8").read()
    except OSError as e:
        die(str(e))


def lint_cmds(docs, bindir, syspath, allow, used, problems):
    """~/bin paths, commands and subcommands in {label: file} docs ("skill X" / "agent X"); returns script calls."""
    import shutil
    isbin = lambda n: CMD.match(n) and os.path.lexists(os.path.join(bindir, n)) and not os.path.isdir(os.path.join(bindir, n))
    ncmds = 0
    for where, f in docs.items():
        text, kind = read_doc(f), where.split()[0]
        for path in sorted(set(re.findall(r"~/bin/([A-Za-z0-9_][A-Za-z0-9_./-]*[A-Za-z0-9_])", text))):
            if not os.path.lexists(os.path.join(bindir, path)):
                problems.append(f"missing: {where} names ~/bin/{path}, which does not exist")
        cmds = skill_cmds(text)
        mine = {w[0].removeprefix("~/bin/") for _, w in cmds} | set(re.findall(r"~/bin/([a-z][\w-]*)", text))
        srcs = "\n".join(read_src(os.path.join(bindir, n)) for n in sorted(mine) if isbin(n))
        for ln, w in cmds:
            c = w[0].removeprefix("~/bin/")
            if not CMD.match(c):
                continue
            if isbin(c):
                ncmds += 1
                if len(w) > 1 and SUB.match(w[1]):
                    if c in allow["nosubcheck"]:
                        used["nosubcheck"].add(c)
                    elif not token_in(w[1], read_src(os.path.join(bindir, c))):
                        problems.append(f"missing: {where}:{ln} runs `{c} {w[1]}`, but ~/bin/{c} has no '{w[1]}' "
                                        f"(fix the {kind}, or allowlist nosubcheck {c} if it forwards elsewhere)")
                continue
            if len(w) < 2 and "-" not in c and not w[0].startswith("~/bin/"):
                continue  # a bare word in prose
            if c in allow["skillcmd"]:
                used["skillcmd"].add(c)
            elif shutil.which(c, path=syspath) or token_in(c, srcs):
                continue
            else:
                problems.append(f"missing: {where}:{ln} tells me to run `{c}`, which is not in ~/bin, the system PATH "
                                f"or a script this {kind} uses (fix the {kind}, or allowlist skillcmd {c})")
    return ncmds


def lint_agents(agdir, problems, allow, used):
    """frontmatter of agents/*.md: name: matches the file, every tools: entry is a known tool. Returns {name: file}."""
    import glob
    agents = {os.path.basename(f)[:-3]: f for f in sorted(glob.glob(os.path.join(agdir, "*.md")))}
    if not agents:
        die(f"no *.md under {agdir}")
    for a, f in agents.items():
        fm = re.match(r"---\n(.*?)\n---\n", read_doc(f), re.S)
        if not fm:
            problems.append(f"missing: agent {a} has no --- frontmatter ---")
            continue
        name = re.search(r"^name:\s*(\S+)", fm.group(1), re.M)
        if not name or name.group(1) != a:
            problems.append(f"missing: agent {a}: frontmatter name: {name.group(1) if name else '(none)'} != file name")
        tools = re.search(r"^tools:\s*(.+)$", fm.group(1), re.M)
        for t in (re.split(r"\s*,\s*", tools.group(1).strip()) if tools else []):
            if t.startswith("mcp__") or t in TOOLS:
                continue
            if t in allow["agenttool"]:
                used["agenttool"].add(t)
            else:
                problems.append(f"missing: agent {a} lists tool {t}, which is not a known tool (fix the agent, or "
                                f"allowlist agenttool {t})")
    return agents


def lint_refs(textdocs, skills, agents, skdir, agdir, allow, used, problems):
    """skill names (when skills is not None) and agent type names (when agents is not None) in {label: file} docs."""
    for label, f in textdocs.items():
        text = read_doc(f)
        for kind, refs, known, d, tail in (("skill", skill_refs, skills, skdir, "{n}/SKILL.md"),
                                           ("agent", agent_refs, agents, agdir, "{n}.md")):
            if known is None:
                continue
            for ln, n in refs(text):
                if n in known or (kind == "agent" and n in BUILTIN_AGENTS):
                    continue
                if n in allow["not" + kind]:
                    used["not" + kind].add(n)
                else:
                    problems.append(f"missing: {label}:{ln} names {kind} {n}, but {d}/{tail.format(n=n)} does not exist")


def lint_skills(skdir, agdir, bindir, namedocs, syspath, allow, problems):
    import glob
    skills = agents = None
    used = {k: set() for k in ("skillcmd", "nosubcheck", "notskill", "notagent", "agenttool")}
    textdocs = {os.path.basename(f): f for f in namedocs}
    if skdir:
        skills = {os.path.basename(os.path.dirname(f)): f for f in sorted(glob.glob(os.path.join(skdir, "*", "SKILL.md")))}
        if not skills:
            die(f"no */SKILL.md under {skdir}")
        textdocs.update({"skill " + k: f for k, f in skills.items()})
    if agdir:
        agents = lint_agents(agdir, problems, allow, used)
        textdocs.update({"agent " + k: f for k, f in agents.items()})
    cmddocs = {k: f for k, f in textdocs.items() if k.startswith(("skill ", "agent "))}
    ncmds = lint_cmds(cmddocs, bindir, syspath, allow, used, problems)
    lint_refs(textdocs, skills, agents, skdir, agdir, allow, used, problems)
    kinds = (("skillcmd", "nosubcheck", "notskill") if skdir else ()) + (("notagent", "agenttool") if agdir else ())
    for kind in kinds:
        for n in allow[kind]:
            if n not in used[kind]:
                problems.append(f"stale allowlist: {kind} {n} is no longer needed by any skill or agent")
    return len(skills or ()), len(agents or ()), ncmds


def main(argv):
    bindir, docf, allowf, verbose = "/home/node/bin", "/home/node/workspace/TOOLS.md", os.path.join(HERE, "allowlist.tsv"), False
    skdir, agdir, namedocs, syspath = None, None, [], SYSPATH
    it = iter(argv)
    for a in it:
        if a == "--bin": bindir = next(it, "") or die("--bin needs DIR")
        elif a == "--doc": docf = next(it, "") or die("--doc needs FILE")
        elif a == "--allow": allowf = next(it, "") or die("--allow needs FILE")
        elif a == "--skills": skdir = next(it, "") or die("--skills needs DIR")
        elif a == "--agents": agdir = next(it, "") or die("--agents needs DIR")
        elif a == "--names": namedocs.append(next(it, "") or die("--names needs FILE"))
        elif a == "--syspath": syspath = next(it, "") or die("--syspath needs PATH")
        elif a == "-v": verbose = True
        elif a in ("-h", "--help"): print(__doc__); return 0
        else: die("unknown argument " + a)
    try:
        doc = open(docf, encoding="utf-8").read()
        entries = bin_entries(bindir)
    except OSError as e:
        die(str(e))
    allow = {k: {} for k in ("undocumented", "notscript", "skillcmd", "nosubcheck", "notskill", "notagent", "agenttool")}
    problems = []
    if os.path.exists(allowf):
        for ln, raw in enumerate(open(allowf, encoding="utf-8"), 1):
            if not raw.strip() or raw.startswith("#"):
                continue
            f = raw.rstrip("\n").split("\t")
            if len(f) < 3 or f[0] not in allow or not f[2].strip():
                problems.append(f"allowlist:{ln}: needs kind<TAB>NAME<TAB>reason (kind {'|'.join(allow)})")
                continue
            allow[f[0]][f[1]] = f[2]
    ds = spans(doc)
    docd = {n for n in entries if mentioned(n, ds, doc)}
    for n, p in entries.items():  # a target reached through a documented symlink counts as documented
        if os.path.islink(p) and n in docd:
            t = os.path.basename(os.path.realpath(p))
            if t in entries:
                docd.add(t)
    for n in entries:
        if n in docd:
            if n in allow["undocumented"]:
                problems.append(f"stale allowlist: {n} is documented now (drop its undocumented line)")
        elif n not in allow["undocumented"]:
            problems.append(f"undocumented: ~/bin/{n} has no TOOLS.md entry (add one line, or allowlist it with a reason)")
    for n in allow["undocumented"]:
        if n not in entries:
            problems.append(f"stale allowlist: {n} is not in {bindir} any more")
    for path in sorted(set(re.findall(r"~/bin/([A-Za-z0-9_][A-Za-z0-9_./-]*[A-Za-z0-9_])", doc))):
        if not os.path.lexists(os.path.join(bindir, path)):
            problems.append(f"missing: TOOLS.md names ~/bin/{path}, which does not exist")
    used_ns = set()
    for ln, w in presented(doc):
        if w in allow["notscript"]:
            used_ns.add(w)
        elif not os.path.lexists(os.path.join(bindir, w)):
            problems.append(f"missing: TOOLS.md:{ln} presents `{w}` as a script, but ~/bin/{w} does not exist")
    for n in allow["notscript"]:
        if n not in used_ns:
            problems.append(f"stale allowlist: notscript {n} is no longer presented in TOOLS.md")
    if skdir or agdir:
        if not namedocs:
            d = os.path.dirname(os.path.abspath(docf))
            namedocs = [os.path.join(d, n) for n in ("TOOLS.md", "JOBS.md", "CLAUDE.md") if os.path.exists(os.path.join(d, n))]
        nsk, nag, ncmd = lint_skills(skdir, agdir, bindir, namedocs, syspath, allow, problems)
    # skill/agent allowlist kinds are only checked (and only stale) when --skills / --agents runs
    problems = list(dict.fromkeys(problems))
    for p in problems:
        print(p)
    if verbose:
        print(f"docs-lint: {len(entries)} bin entries, {len(docd)} documented, {len(allow['undocumented'])} allowlisted, "
              f"{len(presented(doc))} presented scripts, {len(problems)} problem(s)")
        if skdir or agdir:
            print(f"docs-lint: {nsk} skills, {nag} agents, {ncmd} script calls checked")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
