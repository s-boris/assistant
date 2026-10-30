"""loop.cli: the `loop` and `reflex` commands (scripts/loop, scripts/reflex)."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from typing import Any

from core import notes

from . import common, corrections, reflex, review, signals

LOOP_DOC = """loop: the measured self-improvement loop. Drafts for the brain; nothing merges by itself.

  loop review [--dry-run] [--limit 3] [--json]   the daily pass (the self-review's step g runs it at 03:30): signals
        -> <= 3 new change proposals, metric first, each a PR-draft branch loop/<id>-<slug> on ~/opt/assistant main
        (no commits; the draft is the branch description) -> ONE ~/inbox/loop-proposals-DATE.md when new
  loop signals [--json]                           what the review reads: traces, beats, usage, evals, gateway
        denials, reflex firings, correction drafts, setup-health findings (counts and names only)
  loop health [--only a,b] [--json]               the setup-health pass alone (the self-review's step h): every log
        and trace source, 24 h vs 7 d -> anomalies (dry run: no history, no Phoenix write-back)
  loop coverage [--json] [--gaps] [--stack]       which loop step reads which part (packages/loop/coverage.toml vs
        the live registries): the matrix; --gaps only the problems (exit 1 when any); --stack the stack research
        rotation, the topic whose newest ~/reports/stack report is oldest first
  loop corrections [--since DATE] [--dry-run] [--json] [--cron]   owner corrections (memory2) -> fact-row + eval-case
        drafts; ONE ~/inbox/loop-corrections-DATE.md while drafts wait (cron every 2 h, after memory2 extract)
  loop ls [--all] [--json]                        open proposals and correction drafts (--all: decided ones too)
  loop show ID [--json]                           a proposal's PR draft, or a correction draft (fact + case)
  loop accept ID [--no-worktree]                  a proposal: worktree ~/jobs/loop-ID/wt on its branch + PR.md
        (the builder brief); land it with `assistant land BRANCH`
  loop accept ID [--msg M] [--judge J] [--reply-re R] [--area A] [--text T] [--section S] [--no-fact] [--no-case]
        a correction draft: the fact row via `memory` (skipped when a row covers it) + the case into the eval
        corpus (cases/60-corrections.yaml, checked with `evals list`, committed)
  loop reject ID --why TEXT                       a proposal (14 d cool-down; untouched branch deleted) or a draft

IDs: p... = proposal, c... = correction draft (a unique prefix works). State ~/.local/state/loop (0700).
Env: LOOP_STATE, LOOP_BIN (dir of traces/product/evals/tool/events/memory/memory2), MEMORY2_STATE, EVALS_CORPUS,
ASSISTANT_REPO, ASSISTANT_NOW. Exit: 0 ok · 1 error · 2 usage · 3 refused or not found.
"""

REFLEX_DOC = """reflex: the brain's reflex registry: trivial, always-obvious decisions it turned
into rules for itself (event kind + payload conditions -> one typed tool call). The registry starts empty.

  reflex add NAME --kind GLOB --tool TOOL --why TEXT --case EVAL_CASE [--when JSON] [--args JSON]
        refused unless TOOL exists, the gateway policy lets a reflex call it without an approval (`tool policy check
        TOOL --as reflex`), EVAL_CASE is in the eval corpus and WHY says why the decision is always the same
  reflex rm NAME --why TEXT          retire it (history kept; adding the name again = version + 1)
  reflex revise NAME --why TEXT [--kind GLOB] [--when JSON] [--tool TOOL] [--args JSON] [--case EVAL_CASE]
        a new version from its own data (WHY = what the numbers showed); the old definition stays in its history
  reflex history NAME [--json]       every version (with why it ended) and the add/revise/rm log
  reflex review [--days 30] [--all] [--json]   read-only: per reflex firings, hit / misfire rate, its eval case's
        last result and a keep | revise | retire suggestion with the numbers; the E6 line (eval case + firings for
        every reflex, >= 1 revised or retired)
  reflex ls [--all] [--json]         active reflexes with firings 24 h / 7 d / total, misfires, last firing
  reflex show NAME [--json]          one reflex
  reflex log [NAME] [--days 7] [--json]   the firing log
  reflex fire NAME --outcome ok|error|misfire|skipped [--event ID] [--ms N] [--note TEXT]
        record one firing (whoever runs the reflex; misfire = the brain overruled it). Notes: ids, never text
  reflex match KIND [--payload JSON] [--json]   which active reflexes an event would fire (dry run)
  reflex counts [--since EPOCH] [--json]        the agenda counter (firings since the last beat)
--when: {"field": value, "a.b": "glob*"} on the event payload (all must hold). Store ~/.local/state/loop/reflex.db.
Exit: 0 ok · 1 error · 2 usage · 3 refused or not found.
"""


def _out(obj: Any, as_json: bool, text: str) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=1, default=str) if as_json else text)


def _since(s: str | None) -> float | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s)
    except ValueError as e:
        raise SystemExit(f"loop: --since wants YYYY-MM-DD[THH:MM] ({e})") from e
    if d.tzinfo is None:
        d = d.replace(tzinfo=common.tz())
    return d.timestamp()


def _fmt_prop(p: dict[str, Any]) -> str:
    m = p["metric"]
    f = common.fmt_num
    return (
        f"{p['id']} [{p['status']}] {m.get('id')} {m.get('name')}: {f(m.get('now'), m.get('unit', ''))} (target "
        f"{f(m.get('target'), m.get('unit', ''))}) · {p['title']} · {p['branch']}"
    )


def _fmt_draft(d: dict[str, Any]) -> str:
    f = d["fact"]
    fact = (
        f"{f['op']}"
        + (f" {f.get('row')}" if f.get("row") else "")
        + (f" in {f['section']}" if f.get("section") else "")
    )
    return (
        f"{d['id']} [{d['status']}] {d['subject']} ({d['kind']}, {d['day']}) · fact {fact} · case {d['case']['id']}"
        f" ({d['case']['area']})"
    )


def _show_draft(d: dict[str, Any]) -> str:
    f = d["fact"]
    due = datetime.fromtimestamp(d["due_ts"], common.tz()).strftime("%d.%m %H:%M")
    L = [
        f"correction draft {d['id']} [{d['status']}] · memory2 {d['cand']} ({d['kind']}, {d['day']}) · due {due}",
        f"subject: {d['subject']}",
        f"normalized fact: {d['text']}",
        "",
        f"fact row: {f['op']}" + (f" (row {f['row']}: {f.get('row_text')})" if f.get("row") else ""),
    ]
    if f.get("argv"):
        L.append("  would run: " + " ".join(json.dumps(a, ensure_ascii=False) if " " in a else a for a in f["argv"]))
    if f.get("check"):
        L.append(f"  check {f['check']}")
    if f.get("related"):
        L.append("  related rows: " + ", ".join(f["related"]))
    c = d["case"]
    L += [
        "",
        "eval case draft (edit with --msg/--judge/--reply-re/--area on accept):",
        corrections.case_yaml(c, f"loop {d['id']}").rstrip(),
    ]
    return "\n".join(L)


def loop_main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="loop",
        description=LOOP_DOC.split("\n\n")[0],
        epilog=LOOP_DOC,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("review")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--limit", type=int, default=3)
    r.add_argument("--json", action="store_true")
    hl = sub.add_parser("health")
    hl.add_argument("--only", default="")
    hl.add_argument("--json", action="store_true")
    s = sub.add_parser("signals")
    s.add_argument("--json", action="store_true")
    cv = sub.add_parser("coverage")
    cv.add_argument("--json", action="store_true")
    cv.add_argument("--gaps", action="store_true")
    cv.add_argument("--stack", action="store_true")
    c = sub.add_parser("corrections")
    c.add_argument("--since")
    c.add_argument("--dry-run", action="store_true")
    c.add_argument("--json", action="store_true")
    c.add_argument("--cron", action="store_true")
    ls_ = sub.add_parser("ls")
    ls_.add_argument("--all", action="store_true")
    ls_.add_argument("--json", action="store_true")
    sh = sub.add_parser("show")
    sh.add_argument("id")
    sh.add_argument("--json", action="store_true")
    ac = sub.add_parser("accept")
    ac.add_argument("id")
    ac.add_argument("--no-worktree", action="store_true")
    for k in ("--msg", "--judge", "--reply-re", "--area", "--text", "--section"):
        ac.add_argument(k)
    ac.add_argument("--no-fact", action="store_true")
    ac.add_argument("--no-case", action="store_true")
    ac.add_argument("--json", action="store_true")
    rj = sub.add_parser("reject")
    rj.add_argument("id")
    rj.add_argument("--why", required=True)
    rj.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        return _loop(a)
    except (review.LoopError, corrections.CorrectionError) as e:
        print(f"loop: {e}", file=sys.stderr)
        return 3


def _loop(a: argparse.Namespace) -> int:
    if a.cmd == "health":
        from . import health

        res = health.run(write=False, only=[x for x in a.only.split(",") if x] or None)
        print(json.dumps(res, ensure_ascii=False, indent=1) if a.json else health.markdown(res))
        return 0
    if a.cmd == "coverage":
        from . import coverage

        rep = coverage.report()
        if a.gaps:
            lines = coverage.problem_lines(rep["problems"])
            print("\n".join(lines) if lines else "no coverage gaps")
            return 1 if lines else 0
        if a.stack:
            print("\n".join(f"{t}\t{d or 'never'}" for t, d in rep["stack"]))
            return 0
        print(json.dumps(rep, ensure_ascii=False, indent=1) if a.json else coverage.markdown(rep))
        return 0
    if a.cmd == "signals":
        sig = signals.collect()
        _out(sig, a.json, "\n".join(signals.summary_lines(sig)))
        return 0
    if a.cmd == "review":
        res = review.run(write=not a.dry_run, limit=a.limit)
        if a.json:
            _out({k: v for k, v in res.items() if k != "markdown"}, True, "")
        else:
            print(res["markdown"])
            if res["note"]:
                print(f"\nnote: {notes.home_rel(str(res['note']))}")
        return 0
    if a.cmd == "corrections":
        new = corrections.scan(since=_since(a.since), write=not a.dry_run)
        if a.json:
            _out(new, True, "")
        elif not a.cron:
            print(f"loop corrections: {len(new)} new draft(s)" + (" (dry run)" if a.dry_run else ""))
            for d in new:
                print("  " + _fmt_draft(d))
        return 0
    if a.cmd == "ls":
        props, drafts = review.listing(a.all), corrections.listing(a.all)
        if a.json:
            _out({"proposals": props, "corrections": drafts}, True, "")
            return 0
        print(f"proposals ({len(props)}):" if props else "proposals: none")
        for p in props:
            print("  " + _fmt_prop(p))
        print(f"correction drafts ({len(drafts)}):" if drafts else "correction drafts: none")
        for d in drafts:
            print("  " + _fmt_draft(d))
        return 0
    if a.cmd == "show":
        if a.id.startswith("c"):
            d = corrections.show(a.id)
            _out(d, a.json, _show_draft(d))
        else:
            p = review.show(a.id)
            _out(
                p,
                a.json,
                p["draft"] + f"\n\nstatus: {p['status']} · branch {p['branch']} (base {str(p.get('base'))[:12]})",
            )
        return 0
    if a.cmd == "accept":
        if a.id.startswith("c"):
            res = corrections.accept(
                a.id,
                msg=a.msg,
                judge=a.judge,
                reply_re=a.reply_re,
                text=a.text,
                section=a.section,
                area=a.area,
                no_fact=a.no_fact,
                no_case=a.no_case,
            )
            _out(
                res,
                a.json,
                f"loop: {res['id']} accepted · fact row {res.get('row') or '-'} · case "
                f"{res.get('case') or '-'}"
                + (f" in {res['case_file']}" if res.get("case_file") else "")
                + (" · past the 24 h target" if res.get("late") else ""),
            )
        else:
            res = review.accept(a.id, worktree=not a.no_worktree)
            _out(
                res,
                a.json,
                f"loop: {res['id']} accepted · branch {res['branch']}"
                + (
                    f" · worktree {notes.home_rel(res['worktree'])} · brief {notes.home_rel(res['brief'])}"
                    if res.get("worktree")
                    else ""
                )
                + f"\nnext: a builder works on the branch; land with `assistant land {res['branch']}`",
            )
        return 0
    if a.cmd == "reject":
        res = corrections.reject(a.id, a.why) if a.id.startswith("c") else review.reject(a.id, a.why)
        _out(res, a.json, f"loop: {res['id']} rejected" + (f" ({res['branch']})" if res.get("branch") else ""))
        return 0
    return 2


def reflex_main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="reflex",
        description=REFLEX_DOC.split("\n\n")[0],
        epilog=REFLEX_DOC,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    ad = sub.add_parser("add")
    ad.add_argument("name")
    for k in ("--kind", "--tool", "--why", "--case"):
        ad.add_argument(k, required=True)
    ad.add_argument("--when")
    ad.add_argument("--args")
    ad.add_argument("--json", action="store_true")
    rm = sub.add_parser("rm")
    rm.add_argument("name")
    rm.add_argument("--why", required=True)
    rm.add_argument("--json", action="store_true")
    rv = sub.add_parser("revise")
    rv.add_argument("name")
    rv.add_argument("--why", required=True)
    for k in ("--kind", "--when", "--tool", "--args", "--case"):
        rv.add_argument(k)
    rv.add_argument("--json", action="store_true")
    hi = sub.add_parser("history")
    hi.add_argument("name")
    hi.add_argument("--json", action="store_true")
    rw = sub.add_parser("review")
    rw.add_argument("--days", type=float, default=30)
    rw.add_argument("--all", action="store_true")
    rw.add_argument("--json", action="store_true")
    ls_ = sub.add_parser("ls")
    ls_.add_argument("--all", action="store_true")
    ls_.add_argument("--json", action="store_true")
    sh = sub.add_parser("show")
    sh.add_argument("name")
    sh.add_argument("--json", action="store_true")
    lg = sub.add_parser("log")
    lg.add_argument("name", nargs="?")
    lg.add_argument("--days", type=float, default=7)
    lg.add_argument("--json", action="store_true")
    fi = sub.add_parser("fire")
    fi.add_argument("name")
    fi.add_argument("--outcome", required=True, choices=reflex.OUTCOMES)
    fi.add_argument("--event")
    fi.add_argument("--ms", type=int)
    fi.add_argument("--note")
    fi.add_argument("--json", action="store_true")
    ma = sub.add_parser("match")
    ma.add_argument("kind")
    ma.add_argument("--payload")
    ma.add_argument("--json", action="store_true")
    co = sub.add_parser("counts")
    co.add_argument("--since", type=float)
    co.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        return _reflex(a)
    except reflex.ReflexError as e:
        print(f"reflex: {e}", file=sys.stderr)
        return 3


def _pass_word(p: Any) -> str:
    return "pass" if p else "FAIL" if p is False else "?"


def _fmt_reflex(r: dict[str, Any]) -> str:
    return (
        f"{r['name']} v{r['version']} [{r['status']}] {r['kind']} {json.dumps(r['when'], ensure_ascii=False)}"
        f" -> {r['tool']} · case {r['eval_case']} · fired 24 h {r['fired_24h']}, 7 d {r['fired_7d']}, total"
        f" {r['fired_total']} · misfires 7 d {r['misfires_7d']} · last {r['last_fired'] or '-'}\n    why: {r['why']}"
    )


def _reflex(a: argparse.Namespace) -> int:
    if a.cmd == "add":
        r = reflex.add(a.name, kind=a.kind, tool=a.tool, why=a.why, case=a.case, when=a.when, args=a.args)
        _out(r, a.json, "reflex: added " + _fmt_reflex(r))
    elif a.cmd == "rm":
        r = reflex.rm(a.name, a.why)
        _out(r, a.json, f"reflex: retired {r['name']} v{r['version']}")
    elif a.cmd == "revise":
        r = reflex.revise(a.name, a.why, kind=a.kind, when=a.when, tool=a.tool, args=a.args, case=a.case)
        _out(r, a.json, "reflex: revised " + _fmt_reflex(r))
    elif a.cmd == "history":
        h = reflex.history(a.name)
        lines = [
            f"v{v['version']} {v['kind']} {json.dumps(v['when'], ensure_ascii=False)} -> {v['tool']} · case"
            f" {v['eval_case']} · from {v['created'] or '-'}"
            + (f" · ended {v['ended']} by {v['end_op']}: {v['end_why']}" if v.get("ended") else " · current")
            for v in h["versions"]
        ]
        _out(h, a.json, f"{h['name']}:\n  " + "\n  ".join(lines))
    elif a.cmd == "review":
        rv = reflex.review(a.days, a.all)
        e6 = rv["e6"]
        lines = [
            f"{x['name']} v{x['version']}: {x['suggestion']} · {x['reason']} · n {x['n']} (ok {x['ok']}, misfire"
            f" {x['misfire']}, error {x['error']}) · eval {x['eval_case']} "
            + (f"{x['eval']['score']} ({_pass_word(x['eval']['passed'])})" if x["eval"] else "no result")
            for x in rv["reflexes"]
        ]
        lines.append(
            f"E6: {e6['reflexes']} reflexes · eval case {e6['with_eval_case']} · firings {e6['with_firings']} ·"
            f" revised {e6['revised']} · retired {e6['retired']} · {'done' if e6['done'] else 'not yet'}"
        )
        _out(rv, a.json, "\n".join(lines))
    elif a.cmd == "ls":
        rows = reflex.ls(a.all)
        _out(
            rows,
            a.json,
            "\n".join(_fmt_reflex(r) for r in rows) or "reflex: none (the brain adds them from its traces)",
        )
    elif a.cmd == "show":
        r = reflex.show(a.name)
        _out(r, a.json, _fmt_reflex(r))
    elif a.cmd == "log":
        rows = reflex.log(a.name, a.days)
        _out(
            rows,
            a.json,
            "\n".join(
                f"{x['ts']} {x['name']} v{x['version']} {x['outcome']}"
                + (f" event {x['event']}" if x["event"] else "")
                + (f" · {x['note']}" if x["note"] else "")
                for x in rows
            )
            or "reflex: no firings in the window",
        )
    elif a.cmd == "fire":
        r = reflex.fire(a.name, a.outcome, event=a.event, ms=a.ms, note=a.note)
        _out(r, a.json, f"reflex: firing #{r['id']} {r['name']} {r['outcome']}")
    elif a.cmd == "match":
        rows = reflex.match(a.kind, a.payload)
        _out(
            rows,
            a.json,
            "\n".join(f"{x['name']} -> {x['tool']} {json.dumps(x['args'], ensure_ascii=False)}" for x in rows)
            or "reflex: no match",
        )
    elif a.cmd == "counts":
        c = reflex.counts(a.since)
        _out(c, a.json, f"active {c['active']} · fired {c['fired']} · misfires {c['misfires']}")
    return 0
