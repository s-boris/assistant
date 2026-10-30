"""loop.review: the daily pass (the self-review's step g; `loop review` runs it alone).

  run(write=True, limit=3) -> {"new", "skipped", "markdown", "note", "signals"}
      signals -> candidates -> <= `limit` new proposals (open ones are not repeated, rejected ones cool down
      14 d) -> each one a PR draft: branch loop/<id>-<slug> on the harness repo's main (no commits; the draft is
      the branch description and ~/.local/state/loop/proposals.json) -> ONE ~/inbox/loop-proposals-DATE.md note
      when there is something new. Accepted proposals are followed: landed (the branch got commits and is in
      main) -> the metric now vs at proposal time. Open proposals expire after 14 d (untouched branch deleted).
      With Phoenix writes on (loop.phoenix), each new proposal also gets a "before" experiment run on the dataset
      loop-proposal-metrics, and a landed, re-measured one an "after" run with target-met / moved evaluations.
  accept(ID, worktree=True) / reject(ID, why) / show(ID) / listing()
Nothing merges here: a proposal lands only through `assistant land` after the brain built and tested it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core import locks, notes, paths

from . import common, propose, signals

EXPIRE_DAYS = 14
STATE = "proposals.json"


class LoopError(RuntimeError):
    pass


# ------------------------------------------------------------------------------------------------ git
def _git(*args: str, timeout: float = 30) -> tuple[int, str]:
    rc, out, err = common.run(["git", "-C", str(paths.repo()), *args], timeout=timeout)
    return rc, (out if rc == 0 else err).strip()


def _branch_exists(b: str) -> bool:
    return _git("rev-parse", "--verify", "--quiet", f"refs/heads/{b}")[0] == 0


def _sha(ref: str) -> str | None:
    rc, out = _git("rev-parse", "--verify", "--quiet", ref)
    return out if rc == 0 and out else None


def _in_worktree(b: str) -> bool:
    rc, out = _git("worktree", "list", "--porcelain")
    return rc == 0 and f"branch refs/heads/{b}" in out.splitlines()


def _untouched(p: dict[str, Any]) -> bool:
    b = p.get("branch") or ""
    return _branch_exists(b) and _sha(f"refs/heads/{b}") == p.get("base") and not _in_worktree(b)


def _drop_branch(p: dict[str, Any]) -> str:
    b = p.get("branch") or ""
    if b and _untouched(p):
        rc, out = _git("branch", "-D", b)
        _git("config", "--unset", f"branch.{b}.description")
        return "branch deleted" if rc == 0 else f"branch kept ({out[:80]})"
    return "branch kept (has work or is checked out)" if b and _branch_exists(b) else ""


def _make_branch(p: dict[str, Any]) -> None:
    base = _sha("refs/heads/main")
    if not base:
        raise LoopError(f"no main branch in {paths.repo()}")
    b = p["branch"]
    if not _branch_exists(b):
        rc, out = _git("branch", b, base)
        if rc != 0:
            raise LoopError(f"git branch {b}: {out[:160]}")
    _git("config", f"branch.{b}.description", draft_text(p))
    p["base"] = base


# ------------------------------------------------------------------------------------------------ drafts
def draft_text(p: dict[str, Any]) -> str:
    m = p["metric"]
    fmt = common.fmt_num
    cmp = "<=" if m.get("dir", "down") == "down" else ">="
    L = [
        f"loop {p['id']}: {p['title']}",
        "",
        f"Metric ({m.get('id')}): {m.get('name')}: now {fmt(m.get('now'), m.get('unit', ''))}, target {cmp} "
        f"{fmt(m.get('target'), m.get('unit', ''))} (source: {m.get('source')})",
        "Done when: the next review shows the metric at its target, or moved >= 30 % toward it; name the number in"
        " the land message.",
        "",
        "Evidence (counts only):",
        *[f"- {e}" for e in p.get("evidence") or []],
        "",
        f"Where to look first: {p.get('change')}",
    ]
    if p.get("feature"):
        L.append(f"Feature: {p['feature']} (product catalogue): keep its usage instrumentation and catalogue metric.")
    L += [
        "",
        "Rules: one change on this branch, tests first, `assistant ci`, land with `assistant land "
        f"{p['branch']}` (never auto-merged). Public paths: no personal values.",
        f"Proposed by the daily self-review {p['day']} (loop review). Accept: loop accept {p['id']} · reject: "
        f'loop reject {p["id"]} --why "..."',
    ]
    return "\n".join(L)


def _proposal(c: dict[str, Any], now: float) -> dict[str, Any]:
    day = common.today().isoformat()
    pid = common.short_id("p", c["key"], day)
    return {
        "id": pid,
        "key": c["key"],
        "day": day,
        "created_ts": now,
        "status": "open",
        "title": c["title"],
        "metric": c["metric"],
        "evidence": c.get("evidence") or [],
        "change": c.get("change") or "",
        "feature": c.get("feature"),
        "score": round(float(c.get("score") or 0), 2),
        "branch": f"loop/{pid}-{common.slug(c['title'], 28)}",
    }


# ------------------------------------------------------------------------------------------------ state
def _load() -> dict[str, Any]:
    st = common.load(STATE, {})
    if not isinstance(st, dict) or not isinstance(st.get("proposals"), dict):
        st = {"proposals": {}}
    return st


def _find(st: dict[str, Any], pid: str) -> dict[str, Any]:
    p = st["proposals"].get(pid)
    if p is None:
        hits = [v for k, v in st["proposals"].items() if k.startswith(pid)]
        if len(hits) == 1:
            return hits[0]
        raise LoopError(f"no proposal {pid}")
    return p


def _follow(st: dict[str, Any], cands: list[dict[str, Any]], now: float, write: bool) -> list[str]:
    """Expire stale open proposals; mark landed ones and measure them. Report lines."""
    L = []
    by_key = {c["key"]: c for c in cands}
    for p in sorted(st["proposals"].values(), key=lambda x: x.get("created_ts", 0)):
        age_d = (now - float(p.get("created_ts") or now)) / 86400
        if p["status"] == "open" and age_d >= EXPIRE_DAYS:
            p["status"], p["decided_ts"] = "expired", now
            what = _drop_branch(p) if write else "dry run"
            L.append(f"{p['id']} expired after {EXPIRE_DAYS} d ({what})")
            continue
        b = p.get("branch") or ""
        if p["status"] == "accepted" and b and _branch_exists(b):
            head = _sha(f"refs/heads/{b}")
            if head and head != p.get("base") and _git("merge-base", "--is-ancestor", head, "refs/heads/main")[0] == 0:
                p["status"], p["landed_ts"] = "landed", now
        if p["status"] == "landed" and (now - float(p.get("landed_ts") or now)) / 86400 <= EXPIRE_DAYS:
            m = p["metric"]
            c = by_key.get(p["key"])
            now_v = c["metric"]["now"] if c else None
            p["metric_after"] = now_v
            fmt = common.fmt_num
            if c is None:
                L.append(
                    f"{p['id']} landed: {m.get('name')} no longer over its target"
                    f" (was {fmt(m.get('now'), m.get('unit', ''))})"
                )
            else:
                L.append(
                    f"{p['id']} landed: {m.get('name')} {fmt(m.get('now'), m.get('unit', ''))} ->"
                    f" {fmt(now_v, m.get('unit', ''))} (target {fmt(m.get('target'), m.get('unit', ''))})"
                )
    return L


def run(*, write: bool = True, limit: int = 3, sig: dict[str, Any] | None = None) -> dict[str, Any]:
    with locks.hold(common.lock_path(), wait=30, dir_mode=common.STATE_MODE) as fd:
        if fd is None:
            raise LoopError("another loop run holds the lock")
        now = common.now()
        sig = sig if sig is not None else signals.collect()
        cands = propose.candidates(sig)
        st = _load()
        follow = _follow(st, cands, now, write)
        day = common.today().isoformat()
        today_n = sum(1 for p in st["proposals"].values() if p.get("day") == day)
        new, skipped = propose.pick(
            cands, st["proposals"], propose.negative_ledger(), now=now, limit=limit, today_n=today_n
        )
        made = []
        for c in new:
            p = _proposal(c, now)
            if write:
                try:
                    _make_branch(p)
                except LoopError as e:
                    p["branch_error"] = str(e)
            st["proposals"][p["id"]] = p
            made.append(p)
        note = None
        if write:
            follow += _phoenix(st, made)
            common.save(STATE, st)
            if made:
                note = _publish_note(made)
        md = markdown(sig, made, skipped, follow, st)
        return {"new": made, "skipped": skipped, "follow": follow, "markdown": md, "note": note, "signals": sig}


def _phoenix(st: dict[str, Any], made: list[dict[str, Any]]) -> list[str]:
    """Before/after checks of proposals as Phoenix experiments (loop.phoenix); never stops the review."""
    try:
        from . import phoenix

        if not phoenix.enabled_writes():
            return []
        return phoenix.track_proposals(st["proposals"], made)
    except Exception as e:
        return [f"Phoenix experiments skipped ({type(e).__name__})"]


def markdown(
    sig: dict[str, Any], made: list[dict[str, Any]], skipped: list[str], follow: list[str], st: dict[str, Any]
) -> str:
    fmt = common.fmt_num
    L = ["## (g) Loop: signals and change proposals", "", "Signals (counts only):"]
    L += [f"- {x}" for x in signals.summary_lines(sig)]
    L += ["", f"New proposals ({len(made)}):" if made else "New proposals: none"]
    for p in made:
        m = p["metric"]
        L.append(
            f"- {p['id']} {p['title']}: {m.get('name')} {fmt(m.get('now'), m.get('unit', ''))} (target "
            f"{fmt(m.get('target'), m.get('unit', ''))}) · branch {p['branch']}"
            + (f" · {p['branch_error']}" if p.get("branch_error") else "")
        )
    still = [p for p in st["proposals"].values() if p["status"] in ("open", "accepted") and p not in made]
    if still:
        L.append("Open from earlier: " + ", ".join(f"{p['id']} ({p['status']}, {p['title']})" for p in still))
    if follow:
        L += ["", "Followed:"] + [f"- {x}" for x in follow]
    if skipped:
        L += ["", "Not proposed again:"] + [f"- {x}" for x in skipped[:12]]
    return "\n".join(L)


def _publish_note(made: list[dict[str, Any]]) -> Path:
    fmt = common.fmt_num
    day = common.today().isoformat()
    body = []
    for i, p in enumerate(made, 1):
        m = p["metric"]
        body += [
            f"{i}. {p['id']} · {m.get('id')} {m.get('name')}: {fmt(m.get('now'), m.get('unit', ''))} (target "
            f"{fmt(m.get('target'), m.get('unit', ''))}) · {p['title']}",
            f"   Branch {p['branch']} (PR draft: loop show {p['id']})",
        ]
    text = notes.compose(
        f"Loop: {len(made)} change proposal(s), metric first ({day})",
        [
            "From the daily self-review's step g (traces, usage, evals, gateway denials, reflex firings).",
            "Drafts only: nothing is built or merged until I accept one.",
            "",
            *body,
        ],
        "per proposal `loop show ID`, then `loop accept ID` (worktree + builder brief; land with `assistant land`)"
        ' or `loop reject ID --why "..."`; then events ack.',
    )
    return notes.write(paths.inbox(), f"loop-proposals-{day}.md", text, mode=common.FILE_MODE)


# ------------------------------------------------------------------------------------------------ verbs
def show(pid: str) -> dict[str, Any]:
    st = _load()
    p = _find(st, pid)
    return dict(p, draft=draft_text(p))


def listing(include_closed: bool = False) -> list[dict[str, Any]]:
    st = _load()
    rows = [p for p in st["proposals"].values() if include_closed or p["status"] in ("open", "accepted")]
    return sorted(rows, key=lambda p: -float(p.get("created_ts") or 0))


def accept(pid: str, *, worktree: bool = True) -> dict[str, Any]:
    with locks.hold(common.lock_path(), wait=30, dir_mode=common.STATE_MODE):
        st = _load()
        p = _find(st, pid)
        if p["status"] not in ("open", "accepted"):
            raise LoopError(f"{p['id']} is {p['status']}")
        if not p.get("base") or not _branch_exists(p["branch"]):
            _make_branch(p)
        out: dict[str, Any] = {"id": p["id"], "branch": p["branch"]}
        if worktree:
            job = common.jobs_dir() / f"loop-{p['id']}"
            wt = job / "wt"
            if not wt.exists() and not _in_worktree(p["branch"]):
                job.mkdir(parents=True, exist_ok=True)
                rc, msg = _git("worktree", "add", "--quiet", str(wt), p["branch"], timeout=60)
                if rc != 0:
                    raise LoopError(f"git worktree add: {msg[:160]}")
            brief = job / "PR.md"
            brief.parent.mkdir(parents=True, exist_ok=True)
            brief.write_text(draft_text(p) + "\n", encoding="utf-8")
            out.update(worktree=str(wt), brief=str(brief))
        p["status"], p["decided_ts"] = "accepted", common.now()
        common.save(STATE, st)
        return out


def reject(pid: str, why: str) -> dict[str, Any]:
    if not why.strip():
        raise LoopError("reject needs --why")
    with locks.hold(common.lock_path(), wait=30, dir_mode=common.STATE_MODE):
        st = _load()
        p = _find(st, pid)
        if p["status"] not in ("open", "accepted"):
            raise LoopError(f"{p['id']} is {p['status']}")
        p["status"], p["decided_ts"], p["why"] = "rejected", common.now(), why.strip()[:300]
        what = _drop_branch(p)
        common.save(STATE, st)
        return {"id": p["id"], "status": "rejected", "branch": what}
