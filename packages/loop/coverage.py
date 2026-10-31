"""loop.coverage: which loop step reads which part of the system. The map is packages/loop/coverage.toml.

  load(path=None)                 the map
  registries(root, home=None)     the live members: features, jobs (core/schedules.toml), containers (the hub kit's
                                  CONTAINERS + compose container_name), tools (typed tool domains), egress (bindings);
                                  skills and agents from `home` (~/.claude/...) when that dir exists, else None
  check(cov, regs, ...)           problems: unmapped members, parts with no step, stale or doubled claims, unknown step
                                  ids, agent-file markers that are gone, eval areas that don't exist
  matrix(cov, regs)               one row per part: its members and steps, the steps derived from schedules.toml merged
  stack_due(cov, reports_dir)     the stack research rotation: topics, oldest newest-report first
  report(root=None, home=None)    the above for the CLI (`loop coverage`) and the self-review collector `coverage`
Derived steps (from schedules.toml, never listed by hand): h:joberr for every job, h:cronlogs for a job with a `>>`
log, h:hubjobs for keeper and hub-schedule jobs, scorecard cron_fail for a job run through cron-fail.
Names and ids only: the map and the registries hold no personal values.
"""

from __future__ import annotations

import ast
import os
import re
import tomllib
from pathlib import Path
from typing import Any

KINDS = ("review", "scorecard", "ideas", "cleanup", "stack", "evals")
MEMBERS = ("containers", "features", "jobs", "tools", "egress", "skills", "agents", "plugins")
CHECKED = ("features", "tools", "egress", "skills", "agents", "containers")  # claims must exist in these registries
TOOL_RE = re.compile(r'@tool\(\s*"([a-z_]+)\.[a-z_]+"')
CONTAINER_RE = re.compile(r"^\s*container_name:\s*\"?([\w.-]+)", re.M)
AREA_RE = re.compile(r"^  ([\w-]+):\s*\{\s*covers:", re.M)
DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
AGENT_FILES = {"ideas": "idea-scan.md", "cleanup": "self-cleanup.md"}


def default_path() -> Path:
    return Path(__file__).with_name("coverage.toml")


def load(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    return tomllib.loads(Path(path or default_path()).read_text(encoding="utf-8"))


def repo_root() -> Path:
    """The repo checkout: this file's own repo when run from one (tests), else core.paths.repo() (~/opt/assistant)."""
    here = Path(__file__).resolve().parents[2]
    if (here / "core" / "schedules.toml").is_file():
        return here
    from core import paths

    return paths.repo()


# ------------------------------------------------------------------------------------------------ registries
def schedule(root: Path) -> dict[str, dict[str, Any]]:
    return tomllib.loads((root / "core" / "schedules.toml").read_text(encoding="utf-8")).get("jobs") or {}


def containers(root: Path) -> set[str]:
    out: set[str] = set()
    tree = ast.parse((root / "packages" / "hubkit" / "__init__.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "CONTAINERS" for t in node.targets):
            out |= {str(e.value) for e in getattr(node.value, "elts", []) if isinstance(e, ast.Constant)}
    for f in sorted((root / "ops").glob("*/compose*.y*ml")):
        out |= set(CONTAINER_RE.findall(f.read_text(encoding="utf-8")))
    return out


def tool_domains(root: Path) -> set[str]:
    out: set[str] = set()
    for f in sorted((root / "packages" / "tools" / "domains").glob("*.py")):
        out |= set(TOOL_RE.findall(f.read_text(encoding="utf-8")))
    return out


def egress(root: Path) -> set[str]:
    p = root / "packages" / "hub" / "policy" / "egress.toml"
    return set(tomllib.loads(p.read_text(encoding="utf-8")).get("bindings") or {}) if p.is_file() else set()


def registries(root: Path | None = None, home: Path | None = None) -> dict[str, Any]:
    root = root or repo_root()
    sched = schedule(root)
    regs: dict[str, Any] = {
        "features": {str(j.get("feature")) for j in sched.values() if j.get("feature")},
        "jobs": set(sched),
        "containers": containers(root),
        "tools": tool_domains(root),
        "egress": egress(root),
        "skills": None,
        "agents": None,
        "_sched": sched,
    }
    if home is not None:
        sk, ag = home / ".claude" / "skills", home / ".claude" / "agents"
        if sk.is_dir():
            regs["skills"] = {p.parent.name for p in sk.glob("*/SKILL.md")}
        if ag.is_dir():
            regs["agents"] = {p.stem for p in ag.glob("*.md")}
    return regs


# ------------------------------------------------------------------------------------------------ the map
def part_jobs(part: dict[str, Any], sched: dict[str, dict[str, Any]]) -> list[str]:
    feats, names = set(part.get("features") or ()), set(part.get("jobs") or ())
    return sorted(n for n, j in sched.items() if n in names or j.get("feature") in feats)


def derived(part: dict[str, Any], sched: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {"review": [], "scorecard": []}
    jobs = [sched[n] for n in part_jobs(part, sched)]
    if jobs:
        out["review"].append("h:joberr")
    if any(">>" in str(j.get("cron_line") or "") for j in jobs):
        out["review"].append("h:cronlogs")
    if any(j.get("runs_on") in ("keepers", "hub") for j in jobs):
        out["review"].append("h:hubjobs")
    if any("cron-fail" in str(j.get("cron_line") or "") for j in jobs):
        out["scorecard"].append("cron_fail")
    return out


def steps(part: dict[str, Any], sched: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    d = derived(part, sched)
    out = {}
    for k in KINDS:
        own = list(part.get(k) or [])
        out[k] = own + [s for s in d.get(k, []) if s not in own]
    return out


def matrix(cov: dict[str, Any], regs: dict[str, Any]) -> list[dict[str, Any]]:
    sched = regs.get("_sched") or {}
    rows = []
    for p in cov.get("part") or []:
        rows.append(
            {
                "id": p["id"],
                "title": p.get("title", ""),
                "status": p.get("status", ""),
                "members": {k: list(p.get(k) or []) for k in MEMBERS if p.get(k)},
                "jobs": len(part_jobs(p, sched)),
                **steps(p, sched),
            }
        )
    return rows


def _eval_areas(home: Path | None) -> set[str] | None:
    p = home / "opt" / "evals-private" / "config.yaml" if home else None
    return set(AREA_RE.findall(p.read_text(encoding="utf-8"))) if p and p.is_file() else None


def check(
    cov: dict[str, Any],
    regs: dict[str, Any],
    collectors: set[str] | None = None,
    metrics: set[str] | None = None,
    home: Path | None = None,
) -> dict[str, Any]:
    """Every problem, empty lists when the map holds. Registries that are None (not on this box) are skipped."""
    parts = cov.get("part") or []
    vocab = {k: set((cov.get("steps") or {}).get(k) or {}) for k in KINDS if k != "evals"}
    areas = _eval_areas(home)
    if areas is not None:
        vocab["evals"] = areas
    sched = regs.get("_sched") or {}
    claims: dict[str, dict[str, list[str]]] = {k: {} for k in MEMBERS}
    for p in parts:
        for k in MEMBERS:
            for m in p.get(k) or ():
                claims[k].setdefault(str(m), []).append(p["id"])
    known = dict(regs)
    if known.get("containers") is not None:
        known["containers"] = set(known["containers"]) | set(cov.get("external_containers") or ())
    unmapped: dict[str, list[str]] = {}
    for k in ("features", "containers", "tools", "egress", "skills", "agents"):
        if known.get(k) is not None:
            miss = sorted(m for m in known[k] if m not in claims[k])
            if miss:
                unmapped[k] = miss
    loose = sorted(
        n for n, j in sched.items() if n not in claims["jobs"] and j.get("feature") not in claims["features"]
    )
    if loose:
        unmapped["jobs"] = loose
    stale: dict[str, list[str]] = {}
    for k in (*CHECKED, "jobs"):
        reg = set(sched) if k == "jobs" else known.get(k)
        if reg is not None:
            gone = sorted(m for m in claims[k] if m not in reg)
            if gone:
                stale[k] = gone
    doubled = sorted(f"{k}:{m}" for k in MEMBERS for m, who in claims[k].items() if len(who) > 1)
    unknown, uncovered = [], []
    for p in parts:
        st = steps(p, sched)
        if not any(st.values()) and p.get("status") != "in-progress":
            uncovered.append(p["id"])
        for k in KINDS:
            for s in p.get(k) or ():
                if k in vocab and s not in vocab[k]:
                    unknown.append(f"{p['id']}: {k} {s}")
                elif k == "review" and s.startswith("h:") and collectors is not None and s[2:] not in collectors:
                    unknown.append(f"{p['id']}: no collector {s}")
                elif k == "scorecard" and metrics is not None and s not in metrics:
                    unknown.append(f"{p['id']}: no scorecard metric {s}")
    ids = [p["id"] for p in parts]
    unknown += [f"part id twice: {i}" for i in sorted({i for i in ids if ids.count(i) > 1})]
    markers = []
    for kind, fname in AGENT_FILES.items():
        f = home / ".claude" / "agents" / fname if home else None
        if f and f.is_file():
            text = f.read_text(encoding="utf-8")
            for sid, spec in ((cov.get("steps") or {}).get(kind) or {}).items():
                mk = spec.get("marker") if isinstance(spec, dict) else None
                if mk and mk not in text:
                    markers.append(f"{fname}: {kind} {sid} ({mk!r} is gone)")
    return {
        "unmapped": unmapped,
        "uncovered": uncovered,
        "stale": stale,
        "doubled": doubled,
        "unknown": unknown,
        "markers": markers,
    }


def problem_count(pr: dict[str, Any]) -> int:
    n = 0
    for v in pr.values():
        n += sum(len(x) for x in v.values()) if isinstance(v, dict) else len(v)
    return n


def problem_lines(pr: dict[str, Any]) -> list[str]:
    out = []
    for k, v in pr.get("unmapped", {}).items():
        out.append(f"no part claims {k}: {', '.join(v)}")
    if pr.get("uncovered"):
        out.append(f"parts with no loop step: {', '.join(pr['uncovered'])}")
    for k, v in pr.get("stale", {}).items():
        out.append(f"claimed but gone ({k}): {', '.join(v)}")
    if pr.get("doubled"):
        out.append(f"claimed twice: {', '.join(pr['doubled'])}")
    out += pr.get("unknown", []) + pr.get("markers", [])
    return out


# ------------------------------------------------------------------------------------------------ stack rotation
def stack_due(cov: dict[str, Any], reports: Path) -> list[tuple[str, str]]:
    """(topic, newest report date or "") for every stack topic, never-covered and oldest first."""
    names = [f.name for f in reports.glob("*.md")] if reports.is_dir() else []
    out = []
    for tid, spec in ((cov.get("steps") or {}).get("stack") or {}).items():
        own = rf"-{re.escape(tid)}(?:[-.])"  # the topic's own report name, 2026-10-30-<tid>.md
        extra = spec.get("match") if isinstance(spec, dict) else None
        rx = re.compile(f"{own}|{extra}" if extra else own, re.I)
        dates = [m.group(1) for n in names if rx.search(n) for m in [DATE_RE.search(n)] if m]
        out.append((tid, max(dates) if dates else ""))
    return sorted(out, key=lambda t: (t[1], t[0]))


# ------------------------------------------------------------------------------------------------ CLI + collector
def _validators() -> tuple[set[str] | None, set[str] | None]:
    try:
        from . import health

        collectors: set[str] | None = set(health.COLLECTORS)
    except Exception:  # pragma: no cover - a partial deploy
        collectors = None
    try:
        from . import scorecard

        metrics: set[str] | None = set(scorecard.WHERE)
    except Exception:  # pragma: no cover
        metrics = None
    return collectors, metrics


def report(root: Path | None = None, home: Path | None = None, cov: dict[str, Any] | None = None) -> dict[str, Any]:
    cov = cov or load()
    home = home if home is not None else Path.home()
    regs = registries(root, home)
    collectors, metrics = _validators()
    pr = check(cov, regs, collectors, metrics, home)
    return {
        "rows": matrix(cov, regs),
        "problems": pr,
        "count": problem_count(pr),
        "registries": {k: (sorted(v) if v is not None else None) for k, v in regs.items() if not k.startswith("_")},
        "stack": stack_due(cov, home / "reports" / "stack"),
    }


def markdown(rep: dict[str, Any]) -> str:
    L = [
        "| part | self-review | scorecard | idea scan | cleanup | stack | evals |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rep["rows"]:
        name = r["id"] + (" (in progress)" if r.get("status") == "in-progress" else "")
        L.append(f"| {name} | " + " | ".join(", ".join(r[k]) or "-" for k in KINDS) + " |")
    lines = problem_lines(rep["problems"])
    L += ["", f"{len(rep['rows'])} parts; problems: {rep['count']}"] + [f"- {x}" for x in lines]
    return "\n".join(L)
