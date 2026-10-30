"""context-size: how much context the main session carries on every turn.

  context-size [--json] [--budget N] [--sections] [--record]
               [--claude-md FILE] [--facts-core FILE] [--memory-index FILE]   measure (candidates: replace a part)
  context-size calibrate [--yes] [--json]   exact counts by two `claude -p` runs; saves the ratio
  context-size history [--json]             the recorded rows (--record), newest last

Always loaded (static: every turn, every subagent of the session, every compaction):
  managed    /etc/claude-code/CLAUDE.md (the owner's rules; fixed, never ours to shrink)
  user       ~/.claude/CLAUDE.md, when present
  project    ~/workspace/CLAUDE.md; each @import it names is its own row (FACTS-core.md), resolved like Claude Code
             does (relative to the importing file, ~/ = home, up to 5 levels, imports inside code spans ignored)
  memory     the auto-memory index ~/.claude/projects/<workspace>/memory/MEMORY.md (its first 200 lines)
Hook output (reported apart, not part of the always-on total):
  handoff    what the SessionStart hook adds after a compaction or resume (`board-lint handoff`, read-only preview)
  pack       the memory2 per-turn pack: chars per turn from ~/.local/state/memory2/hook.log (p50, p95, max)
  agenda     the beat agenda (`events agenda`, a preview that writes nothing)
Tokens = characters / chars_per_token. The ratio comes from ~/.config/assistant/context-size.json (written by
`calibrate`), else 2.15 (the Opus 5.5 tokenizer on FACTS-core.md, measured). `calibrate` runs `claude -p`
twice from ~/jobs/context-size (never from the workspace: a restart would resume that conversation): once with an
empty CLAUDE.md, once with the always-on text inlined; the input-token difference is the exact size. Each run costs
one short turn; --yes is required.
--sections splits every part into its "## " sections (largest first): the input for the slimming plan.
Exit: 0 total within --budget (default 5000), 1 over it, 2 usage or a part missing.
--record appends one row (counts only) to ~/.local/state/assistant/context-size.jsonl.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core import clock, jsonstate, paths

BUDGET = 5000
DEFAULT_CPT = 2.15  # the Opus 5.5 tokenizer measured ~2.15 chars/token on FACTS-core.md, ~2.2 on FACTS.md
MEMORY_LINES = 200  # Claude Code loads the first 200 lines of MEMORY.md
IMPORT_DEPTH = 5
MANAGED = "/etc/claude-code/CLAUDE.md"
IMPORT_RE = re.compile(r"(?<![\w`])@((?:~/|\.{1,2}/|/)?[\w.\-/]+\.md)\b")
CALIB_PROMPT = "Reply with just OK."


@dataclass
class Part:
    name: str
    path: str
    text: str
    kind: str = "always"  # always | hook
    note: str = ""
    sections: list[tuple[str, int]] = field(default_factory=list)

    @property
    def chars(self) -> int:
        return len(self.text)


# ---------------------------------------------------------------- reading the parts


def home() -> Path:
    return paths.home()


def workspace() -> Path:
    return home() / "workspace"


def project_dir() -> Path:
    env = os.environ.get("ASSISTANT_PROJECT_DIR", "")
    if env:
        return paths.guard(env)
    return home() / ".claude" / "projects" / os.fspath(workspace()).replace("/", "-")


def managed_path() -> Path:
    return Path(os.environ.get("CONTEXT_SIZE_MANAGED") or MANAGED)


def _read(p: Path) -> str | None:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _strip_code(text: str) -> str:
    """The text without fenced blocks and inline code spans (imports there are not followed)."""
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    return re.sub(r"`[^`\n]*`", "", text)


def imports_of(text: str, base: Path) -> list[Path]:
    out: list[Path] = []
    for m in IMPORT_RE.finditer(_strip_code(text)):
        ref = m.group(1)
        p = home() / ref[2:] if ref.startswith("~/") else (Path(ref) if ref.startswith("/") else base / ref)
        p = Path(os.path.normpath(p))
        if p not in out:
            out.append(p)
    return out


def project_parts(claude_md: Path, overrides: dict[str, Path], candidate: Path | None = None) -> list[Part]:
    """CLAUDE.md and its imports (depth-first, each file once). candidate: read this file in CLAUDE.md's place
    (imports still resolve against the workspace); overrides: an import's file name -> a candidate file."""
    parts: list[Part] = []
    seen: set[Path] = set()

    def walk(p: Path, depth: int, name: str) -> None:
        if p in seen or depth > IMPORT_DEPTH:
            return
        seen.add(p)
        src = (candidate or p) if depth == 0 else overrides.get(p.name, p)
        text = _read(src)
        note = "candidate" if src != p else ""
        if text is None:
            parts.append(Part(name, os.fspath(src), "", note="missing"))
            return
        parts.append(Part(name, os.fspath(src), text, note=note))
        for q in imports_of(text, p.parent):
            walk(q, depth + 1, q.name)

    walk(claude_md, 0, "CLAUDE.md")
    return parts


def memory_part(path: Path | None) -> Part:
    p = path or (project_dir() / "memory" / "MEMORY.md")
    text = _read(p)
    if text is None:
        return Part("MEMORY.md", os.fspath(p), "", note="missing")
    lines = text.splitlines(keepends=True)
    note = f"first {MEMORY_LINES} of {len(lines)} lines" if len(lines) > MEMORY_LINES else ""
    return Part("MEMORY.md", os.fspath(p), "".join(lines[:MEMORY_LINES]), note=note)


def static_parts(a: argparse.Namespace) -> list[Part]:
    parts: list[Part] = []
    mt = _read(managed_path())
    if mt is not None:
        parts.append(Part("managed CLAUDE.md", os.fspath(managed_path()), mt, note="fixed"))
    ut = _read(home() / ".claude" / "CLAUDE.md")
    if ut is not None:
        parts.append(Part("user CLAUDE.md", "~/.claude/CLAUDE.md", ut))
    overrides = {}
    if a.facts_core:
        overrides["FACTS-core.md"] = Path(a.facts_core)
    cand = Path(a.claude_md) if a.claude_md else None
    parts += project_parts(workspace() / "CLAUDE.md", overrides, cand)
    mem = memory_part(Path(a.memory_index) if a.memory_index else None)
    if a.memory_index:
        mem.note = (mem.note + ", candidate").lstrip(", ") if mem.note != "missing" else mem.note
    parts.append(mem)
    return parts


# ---------------------------------------------------------------- hook outputs (reported apart)


def _run(argv: list[str], timeout: float, stdin: str | None = None) -> str | None:
    exe = shutil.which(argv[0]) or os.fspath(paths.bin_dir(argv[0]))
    if not os.access(exe, os.X_OK):
        return None
    try:
        r = subprocess.run([exe, *argv[1:]], input=stdin, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def pack_stats(n: int = 400) -> dict[str, Any] | None:
    """Per-turn memory2 pack sizes (chars) from the hook log's last n rows: p50, p95, max, rows."""
    log = Path(os.environ.get("CONTEXT_SIZE_PACK_LOG") or home() / ".local" / "state" / "memory2" / "hook.log")
    try:
        with open(log, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 200_000))
            rows = f.read().decode("utf-8", "replace").splitlines()[-n:]
    except OSError:
        return None
    sizes = []
    for ln in rows:
        c = ln.split()
        if len(c) >= 7 and c[4] == "ok" and c[6].isdigit():
            sizes.append(int(c[6]))
    if not sizes:
        return {"rows": 0}
    sizes.sort()
    return {
        "rows": len(sizes),
        "p50": int(statistics.median(sizes)),
        "p95": sizes[min(len(sizes) - 1, math.ceil(0.95 * len(sizes)) - 1)],
        "max": sizes[-1],
    }


def hook_parts() -> tuple[list[Part], dict[str, Any] | None]:
    parts = []
    h = _run(["board-lint", "handoff"], 10)
    parts.append(Part("handoff (SessionStart)", "board-lint handoff", h or "", kind="hook", note="" if h else "n/a"))
    ag = _run(["events", "agenda"], 15)
    parts.append(Part("agenda (beats)", "events agenda", ag or "", kind="hook", note="" if ag else "n/a"))
    return parts, pack_stats()


# ---------------------------------------------------------------- numbers


def conf_file() -> Path:
    return paths.config("context-size.json")


def ratio() -> tuple[float, str]:
    c = jsonstate.load(conf_file(), {}) or {}
    try:
        v = float(c.get("chars_per_token") or 0)
    except (TypeError, ValueError):
        v = 0.0
    if v > 0.5:
        return v, f"calibrated {c.get('calibrated_at', '?')}"
    return DEFAULT_CPT, "default (measured)"


def tok(chars: int, cpt: float) -> int:
    return math.ceil(chars / cpt) if chars else 0


def split_sections(text: str) -> list[tuple[str, int]]:
    """[(heading, chars)] for the '## ' sections (the text before the first one as '(top)'), largest first."""
    out: list[tuple[str, int]] = []
    cur, buf = "(top)", []
    for ln in text.splitlines(keepends=True):
        if ln.startswith("## "):
            out.append((cur, len("".join(buf))))
            cur, buf = ln[3:].strip()[:60], [ln]
        else:
            buf.append(ln)
    out.append((cur, len("".join(buf))))
    return sorted([s for s in out if s[1]], key=lambda s: -s[1])


def ledger_verbs(text: str) -> dict[str, int]:
    """FACTS-core ledger lines per verb (`- DD.MM verb: thing`)."""
    c: dict[str, int] = {}
    for m in re.finditer(r"^- \d{1,2}\.\d{1,2} (\w+):", text, flags=re.M):
        c[m.group(1)] = c.get(m.group(1), 0) + 1
    return dict(sorted(c.items(), key=lambda kv: -kv[1]))


def measure(a: argparse.Namespace) -> dict[str, Any]:
    cpt, how = ratio()
    st = static_parts(a)
    rows = []
    for p in st:
        r = {"name": p.name, "path": paths_rel(p.path), "chars": p.chars, "tokens": tok(p.chars, cpt)}
        if p.note:
            r["note"] = p.note
        if a.sections:
            r["sections"] = [{"name": s, "chars": n, "tokens": tok(n, cpt)} for s, n in split_sections(p.text)]
            if p.name == "FACTS-core.md":
                r["ledger_verbs"] = ledger_verbs(p.text)
        rows.append(r)
    total = sum(r["tokens"] for r in rows)
    fixed = sum(r["tokens"] for r in rows if r.get("note") == "fixed")
    out: dict[str, Any] = {
        "at": int(clock.now()),
        "chars_per_token": cpt,
        "ratio": how,
        "parts": rows,
        "total_tokens": total,
        "fixed_tokens": fixed,
        "budget": a.budget,
        "over_by": max(0, total - a.budget),
        "missing": [r["name"] for r in rows if r.get("note") == "missing"],
    }
    if a.hooks:
        hp, pack = hook_parts()
        out["hooks"] = [
            {"name": p.name, "chars": p.chars, "tokens": tok(p.chars, cpt), **({"note": p.note} if p.note else {})}
            for p in hp
        ]
        if pack is not None:
            out["pack"] = {k: v for k, v in pack.items()} | (
                {"p95_tokens": tok(int(pack["p95"]), cpt)} if pack.get("rows") else {}
            )
    return out


def paths_rel(p: str) -> str:
    h = os.fspath(home())
    return "~" + p[len(h) :] if p.startswith(h + "/") else p


def render(d: dict[str, Any]) -> str:
    w = max(len(r["name"]) for r in d["parts"]) if d["parts"] else 10
    lines = [
        f"Always-on context: {d['total_tokens']:,} tokens (budget {d['budget']:,}; ratio {d['chars_per_token']} "
        f"chars/token, {d['ratio']})"
    ]
    for r in d["parts"]:
        note = f"  ({r['note']})" if r.get("note") else ""
        lines.append(f"  {r['name']:<{w}}  {r['chars']:>7,} chars  {r['tokens']:>6,} tok{note}")
        for s in r.get("sections", [])[:14]:
            lines.append(f"      {s['tokens']:>6,} tok  {s['name']}")
        if r.get("ledger_verbs"):
            lines.append("      ledger lines: " + ", ".join(f"{k} {v}" for k, v in r["ledger_verbs"].items()))
    lines.append(
        f"  {'over budget by' if d['over_by'] else 'within budget'}"
        f"{' ' + format(d['over_by'], ',') + ' tokens' if d['over_by'] else ''}; fixed part {d['fixed_tokens']:,}"
    )
    if d.get("hooks"):
        lines.append("Hook output (not always-on):")
        for r in d["hooks"]:
            lines.append(
                f"  {r['name']:<{w}}  {r['chars']:>7,} chars  {r['tokens']:>6,} tok"
                + (f"  ({r['note']})" if r.get("note") else "")
            )
    pk = d.get("pack")
    if pk and pk.get("rows"):
        lines.append(
            f"  memory2 pack per turn: p50 {pk['p50']:,} · p95 {pk['p95']:,} · max {pk['max']:,} chars "
            f"(~{pk['p95_tokens']:,} tok at p95; {pk['rows']} turns)"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------- calibrate (claude -p, opt-in)


def _usage(out: str) -> int | None:
    try:
        d = json.loads(out)
    except ValueError:
        return None
    u = d.get("usage") if isinstance(d, dict) else None
    if not isinstance(u, dict):
        return None
    return sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))


def _claude_p(folder: Path, timeout: float) -> int | None:
    exe = os.environ.get("CONTEXT_SIZE_CLAUDE") or shutil.which("claude") or os.fspath(home() / ".local/bin/claude")
    env = dict(os.environ, ENABLE_CLAUDEAI_MCP_SERVERS="false")
    try:
        r = subprocess.run(
            [
                exe,
                "-p",
                CALIB_PROMPT,
                "--model",
                os.environ.get("CONTEXT_SIZE_MODEL", "claude-opus-5-5"),
                "--max-turns",
                "1",
                "--setting-sources",
                "project,local",
                "--strict-mcp-config",
                "--output-format",
                "json",
            ],
            cwd=folder,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return _usage(r.stdout) if r.returncode == 0 else None


def calibrate(a: argparse.Namespace) -> int:
    """Two `claude -p` runs: empty CLAUDE.md vs the always-on text minus the managed file (loaded in both runs)."""
    if not a.yes:
        print("calibrate runs `claude -p` twice (two short turns on the subscription); add --yes", file=sys.stderr)
        return 2
    a.sections = False
    parts = [p for p in static_parts(a) if p.note != "fixed" and p.text]
    text = "\n\n".join(p.text for p in parts)
    root = Path(os.environ.get("CONTEXT_SIZE_JOBS") or home() / "jobs" / "context-size")
    base, full = root / "base", root / "full"
    for d, body in ((base, ""), (full, text)):
        d.mkdir(parents=True, exist_ok=True)
        jsonstate.write_text(d / "CLAUDE.md", body, mode=0o600)
    t_base = _claude_p(base, 240)
    t_full = _claude_p(full, 240)
    if t_base is None or t_full is None or t_full <= t_base:
        print(f"calibrate: no usable usage numbers (base {t_base}, full {t_full})", file=sys.stderr)
        return 1
    tokens = t_full - t_base
    cpt = round(len(text) / tokens, 3)
    rec = {
        "chars_per_token": cpt,
        "calibrated_at": time.strftime("%Y-%m-%d %H:%M"),
        "chars": len(text),
        "tokens": tokens,
    }
    jsonstate.save(conf_file(), rec, mode=0o600)
    if a.json:
        print(json.dumps(rec))
    else:
        print(
            f"calibrated: {tokens:,} tokens for {len(text):,} chars of always-on text = {cpt} chars/token "
            f"(saved to {paths_rel(os.fspath(conf_file()))})"
        )
    return 0


# ---------------------------------------------------------------- history


def history_file() -> Path:
    return paths.state("context-size.jsonl")


def record(d: dict[str, Any]) -> None:
    row = {
        "at": d["at"],
        "total": d["total_tokens"],
        "cpt": d["chars_per_token"],
        "parts": {r["name"]: r["tokens"] for r in d["parts"]},
    }
    f = history_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, separators=(",", ":")) + "\n")


def history(as_json: bool) -> int:
    rows = []
    try:
        with open(history_file(), encoding="utf-8") as fh:
            rows = [json.loads(ln) for ln in fh if ln.strip()]
    except (OSError, ValueError):
        pass
    if as_json:
        print(json.dumps(rows))
    else:
        for r in rows[-20:]:
            print(
                time.strftime("%Y-%m-%d %H:%M", time.localtime(r["at"])),
                f"{r['total']:>6,} tok",
                " ".join(f"{k}={v}" for k, v in r["parts"].items()),
            )
        if not rows:
            print("no rows yet (context-size --record)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="context-size", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("cmd", nargs="?", default="measure", choices=("measure", "calibrate", "history"))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--budget", type=int, default=BUDGET)
    ap.add_argument("--sections", action="store_true")
    ap.add_argument("--hooks", action="store_true", help="also the hook outputs (handoff, agenda, memory2 pack)")
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--claude-md")
    ap.add_argument("--facts-core")
    ap.add_argument("--memory-index")
    ap.add_argument("--yes", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "calibrate":
        return calibrate(a)
    if a.cmd == "history":
        return history(a.json)
    d = measure(a)
    if a.record:
        record(d)
    print(json.dumps(d) if a.json else render(d))
    if d["missing"] and not a.json:
        print("missing: " + ", ".join(d["missing"]), file=sys.stderr)
    return 2 if d["missing"] else (1 if d["over_by"] else 0)
