"""memory: FACTS.md as rows in mem.db, rendered back byte for byte. `memory --help`.

  memory status [--json]                  row counts, whether FACTS.md matches the rows, tool writes, hand-edit
                                          imports
  memory import [--allow-removals] [--json]
        reconcile the rows with FACTS.md (hand edits become rows: edit = new row superseding the old one, deleted
        line = removed, never lost). Refuses when the file would drop > max(10, 20 %) of the rows.
  memory gate [--json]                    the round-trip check: import, render, byte compare (store, fresh,
                                          idempotent)
  memory render [--out FILE] [--scope always] [--write] [--core [--core-config FILE]]
        the document from the rows (stdout by default); --write restores a missing FACTS.md; --core = the
        facts core instead (FACTS-core.md: basics, short facts, the whole ledger thing list; see `memory core`)
  memory core [show|explain [--section S] [--out-only]|write|set ID in|out|auto] [--json]
        the facts core the main session imports: show = counts, estimated tokens, whether FACTS-core.md is
        current; explain = each row in or out and the rule that placed it; write = re-render FACTS-core.md (the
        import cron does this); set = pin a row in or out (follows its edits), auto = back to the rules.
        Rules and parameters: packages/memrows/corepack.py, <conf>/core.json; <conf>/core-off stops the file writes
  memory show [ID] [--section S] [--kind fact|decision] [--grep TEXT] [--status S|any] [--json]
        rows in file order (--grep matches the text or a row id); with ID: the row and its supersedes chain
  memory ledger [--negative] [--json]     the active ledger rows with board-lint's fields (verb, thing, day, month)
  memory set --section S "CLAIM" --src "owner|live|<who>" [--date DD.MM] [--source owner|live|agent]
             [--scope always|topic] [--after ID] [--raw] [--dry-run] [--json]
        a new fact at the end of section S (or after row ID): "CLAIM — SRC, DD.MM" (date: today unless SRC has one;
        --raw = the text as given, no attribution needed)
  memory decide VERB "THING" [--details D] [--date DD.MM] [--supersedes ID] [--dry-run] [--json]
  memory decide "[DD.MM ]verb: thing (details)" [--supersedes ID] ...     (the same, as one ledger-shaped string)
        a ledger line at the top: "DD.MM verb: thing (details)"; verb declined|skipped|scratched|removed|off|done|
        decided|kept; --supersedes retires an older ledger row on the same thing
  memory supersede ID ["NEW TEXT" [--src ..] [--date ..] [--raw] | --remove] [--dry-run] [--json]
        replace a row in place (the old one stays as superseded history) or retire it (removed)

The rows are the source of FACTS.md; hand edits of the file are imported as rows on the next memory call. Every
write: lock, import hand edits, check the rows render FACTS.md byte for byte, change, render, compare-and-swap
FACTS.md, re-render FACTS-core.md (--dry-run: the diff, nothing written). It prints the new row id, the FACTS.md
line number and text, where the row sits in FACTS-core.md and the diff: both files are current when it returns, so
no read-back is needed.
Global: --db PATH (default ~/.local/state/memory2/mem.db) --facts PATH (default ~/workspace/FACTS.md; a copy needs its
own --db). Env MEMORY_DB, MEMORY_FACTS, MEMORY2_STATE/_FACTS/_CONF, MEMORY_SECRETSCAN_BIN (tests).

Exit: 0 ok · 1 error or gate not identical · 2 usage · 3 refused (removal limit, validation, secret)
· 4 rows and file disagree · 5 busy (lock, file kept changing) · 6 not found.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from core import jsonstate, locks
from memrows import corepack
from memrows import store as S


def _out(obj: Any, as_json: bool, text: str) -> None:
    print(json.dumps(obj, ensure_ascii=False) if as_json else text)


def _line(r: dict[str, Any], width: int = 150) -> str:
    st = "" if r["status"] == "active" else f"[{r['status']}] "
    text = r["text"] if len(r["text"]) <= width else r["text"][: width - 1] + "…"
    return (
        f"{r['id']}  {r['kind'][0]}  {r['section'][:16]:16}  {r['verified_at'] or '-':10}  {r['source'] or '-':5}  "
        f"{st}{text}"
    )


def _write(a: argparse.Namespace, p: S.Paths, con: Any, change: Any) -> int:
    """Run one write and say exactly what changed: the row ids, the FACTS.md line (number and text), where the row sits
    in the re-rendered FACTS-core.md, and the diff, so no read-back is needed."""
    res = S.write_cycle(con, p, change, dry_run=a.dry_run)
    core = None
    if not res["dry_run"]:
        core = _core_refresh(p, con, quiet=True, want=None if res["op"] == "remove" else res.get("id"))
        res["core"] = _core_report(p, core, res)
    if a.json:
        _out(res, True, "")
        return 0
    print(_write_head(res))
    if res.get("imported") and res["imported"]["changed"]:
        c = res["imported"]
        print(f"  hand edits imported first: {c['edited']} edited, {c['added']} added, {c['removed']} removed")
    fname = p.facts.name
    sec = f" ({res['section']})" if res.get("section") else ""
    if res["op"] == "remove":
        print(f"  {fname}: line {res.get('file_line') or '?'}{sec} {'would be ' if res['dry_run'] else ''}deleted")
    else:
        verb = "would read" if res["dry_run"] else "now reads"
        print(f"  {fname} line {res.get('file_line') or '?'}{sec} {verb}: {res.get('line', '')}")
    if res.get("core"):
        print("  " + res["core"]["say"])
    for d in res["diff"]:
        print("    " + d)
    if res.get("same_thing"):
        print("  note: active ledger rows on the same thing: " + ", ".join(res["same_thing"]) + " (--supersedes?)")
    if not res["dry_run"]:
        files = fname + (f" and {corepack.CORE_DOC}" if res["core"].get("written") else "")
        print(
            f"  {files} already re-rendered: no read-back needed (`memory show {res['id']}` = the row and its history)"
        )
    return 0


def _write_head(res: dict[str, Any]) -> str:
    op, rid, old = res["op"], res.get("id", ""), res.get("old")
    what = {
        "set": f"new row {rid}",
        "decide": f"new ledger row {rid}" + (f", supersedes {old} (kept as history)" if old else ""),
        "supersede": f"new row {rid} replaces {old} (the old row stays as superseded history)",
        "remove": f"row {rid} retired (status removed, kept as history)",
    }.get(op, rid)
    if res["dry_run"]:
        return f"memory {op}: dry run, nothing written · would be: {what}"
    return f"memory {op}: written · {what}"


def _core_report(p: S.Paths, core: dict[str, Any] | None, res: dict[str, Any]) -> dict[str, Any]:
    """Where the written row ended up in FACTS-core.md, as data plus one sentence ("say")."""
    name = corepack.CORE_DOC
    path = str(corepack.core_path(p))
    if core is None:
        return {"path": path, "written": False, "say": f"{name} NOT re-rendered (see the error above)"}
    if core.get("skipped"):
        return {"path": path, "written": False, "say": f"{name} not written (core-off)"}
    out = {"path": path, "written": True, "changed": core.get("changed")}
    if res["op"] == "remove":
        try:
            still = S.line_no(corepack.core_path(p).read_bytes(), res.get("line"))
        except OSError:
            still = None
        out["line_no"] = still
        out["say"] = (
            f"{name} re-rendered: the line is gone from it too"
            if still is None
            else (f"{name} line {still} still reads the same text (another active row holds it)")
        )
        return out
    row = core.get("row") or {}
    out.update({k: row.get(k) for k in ("inc", "rule", "form", "line", "line_no")})
    if row.get("inc"):
        same = row.get("line") == res.get("line")
        out["say"] = f"{name} line {row.get('line_no') or '?'} (re-rendered): " + (
            "the same line" if same else f"{row.get('line')} (shortened to the thing)"
        )
    else:
        out["say"] = (
            f"{name} re-rendered: this row stays on file only (rule {row.get('rule', '?')}; "
            "`memory core explain` lists why)"
        )
    return out


def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db")
    common.add_argument("--facts")
    common.add_argument("--json", action="store_true")
    ap = argparse.ArgumentParser(
        prog="memory", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter, parents=[common]
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status", parents=[common])
    s.add_argument("--no-refresh", action="store_true")
    s = sub.add_parser("import", parents=[common])
    s.add_argument("--allow-removals", action="store_true")
    s.add_argument("--quiet", action="store_true")
    s = sub.add_parser("gate", parents=[common])
    s = sub.add_parser("render", parents=[common])
    s.add_argument("--out")
    s.add_argument("--scope", choices=S.SCOPES)
    s.add_argument("--write", action="store_true")
    s.add_argument("--core", action="store_true")
    s.add_argument("--core-config", help="with --core: a candidate core.json (render only; the live file stays)")
    s = sub.add_parser("core", parents=[common])
    s.add_argument("action", nargs="?", default="show", choices=["show", "explain", "write", "set"])
    s.add_argument("args", nargs="*")
    s.add_argument("--section")
    s.add_argument("--out-only", action="store_true")
    s = sub.add_parser("show", parents=[common])
    s.add_argument("id", nargs="?")
    s.add_argument("--section")
    s.add_argument("--kind", choices=("fact", "decision"))
    s.add_argument("--grep")
    s.add_argument("--status", default="active")
    s.add_argument("--no-refresh", action="store_true")
    s = sub.add_parser("ledger", parents=[common])
    s.add_argument("--negative", action="store_true")
    s.add_argument("--no-refresh", action="store_true")
    s = sub.add_parser(
        "set",
        parents=[common],
        description='A new fact at the end of section S: "CLAIM — SRC, DD.MM". Prints the FACTS.md / FACTS-core.md '
        "lines it wrote.",
        epilog='e.g. memory set --section "Network" "Switch behind the TV is 1 Gbit" --src live',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    s.add_argument("text")
    s.add_argument("--section")
    s.add_argument("--src")
    s.add_argument("--date")
    s.add_argument("--source", choices=S.SOURCES)
    s.add_argument("--scope", choices=S.SCOPES, default="always")
    s.add_argument("--after")
    s.add_argument("--raw", action="store_true")
    s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser(
        "decide",
        parents=[common],
        description='A ledger line at the top of the ledger: VERB "THING" [--details D], or the whole line as one '
        "string. Prints the FACTS.md / FACTS-core.md lines it wrote.",
        epilog='e.g. memory decide declined "Ryanair flights" --details "never again"\n'
        '     memory decide "21.10 declined: Ryanair flights (never again)"',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    s.add_argument("verb", help="|".join(S.LEDGER_VERBS) + ', or one ledger line "[DD.MM ]verb: thing (details)"')
    s.add_argument("thing", nargs="?", help="the 2-4 words naming it (no '(', ' — ', '; ')")
    s.add_argument("--details")
    s.add_argument("--date")
    s.add_argument("--supersedes")
    s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser(
        "supersede",
        parents=[common],
        description="Replace row ID with NEW TEXT (the old row stays as superseded history) or retire it (--remove). "
        "Prints the FACTS.md / FACTS-core.md lines it wrote.",
        epilog='e.g. memory supersede r0123456789 "Living room: LG C9 + Google TV Streamer" --src owner\n'
        "     (find the ID with memory show --grep WORD)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    s.add_argument("id")
    s.add_argument("text", nargs="?")
    s.add_argument("--src")
    s.add_argument("--date")
    s.add_argument("--source", choices=S.SOURCES)
    s.add_argument("--raw", action="store_true")
    s.add_argument("--remove", action="store_true")
    s.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    try:
        p = S.resolve_paths(a.db, a.facts)
        con = S.connect(p.db)
        return _run(a, p, con)
    except S.MemError as e:
        print(f"memory {a.cmd}: {e}", file=sys.stderr)
        return e.code


def _run(a: argparse.Namespace, p: S.Paths, con: Any) -> int:
    if a.cmd == "status":
        if not a.no_refresh:
            try:
                S.refresh(con, p)
            except (S.Refused, S.NotFound) as e:
                print(f"memory status: rows not refreshed: {e}", file=sys.stderr)
        st = S.stats(con, p)
        _out(
            st,
            a.json,
            f"memory: {st['facts']} facts + "
            f"{st['decisions']} decisions active · {st['superseded']} superseded · {st['removed']} removed · "
            f"file matches rows: {'yes' if st.get('file_matches_rows') else 'NO'} · last import {st['last_import']} · "
            f"tool writes {st['tool_writes']} · hand-edit imports {st['hand_edit_imports']}",
        )
        return 0

    if a.cmd == "import":
        with locks.hold(p.lock, wait=30, dir_mode=0o700) as fd:
            if fd is None:
                raise S.Busy("another memory write holds the lock")
            with S.tx(con):
                c = S.import_doc(con, S.read_file(p), allow_removals=a.allow_removals)
        if not a.quiet or a.json:
            _out(
                c,
                a.json,
                "memory import: {kept} kept, {moved} moved, {edited} edited, {added} added, {restored} restored, "
                "{removed} removed, {layout} layout lines".format(**c),
            )
        _core_refresh(p, con, quiet=a.quiet and not a.json)
        return 0

    if a.cmd == "gate":
        with locks.hold(p.lock, wait=30, dir_mode=0o700) as fd:
            if fd is None:
                raise S.Busy("another memory write holds the lock")
            res = S.gate(S.read_file(p), con)
        ok = res["identical"]
        _out(
            res,
            a.json,
            f"memory gate: {'byte-identical' if ok else 'NOT identical'} · {res['bytes']} bytes, {res['lines']} lines, "
            f"sha256 {res['sha'][:12]} · checks "
            + " ".join(f"{k}={'ok' if v else 'FAIL'}" for k, v in res["checks"].items())
            + (f" · first differing line {res['first_diff_line']}" if not ok else ""),
        )
        return 0 if ok else 1

    if a.cmd == "render":
        if a.write:
            return _restore(p, con)
        if a.core_config and not a.core:
            raise S.Refused("--core-config needs --core")
        cfg_file = Path(a.core_config) if a.core_config else None
        out = corepack.render(con, p, cfg_file=cfg_file) if a.core else S.render(con, scope=a.scope)
        if a.out and a.out != "-":
            jsonstate.write_bytes(a.out, out, mode=0o600)
        else:
            sys.stdout.buffer.write(out)
            sys.stdout.flush()
        return 0

    if a.cmd == "show":
        if not a.no_refresh:
            S.refresh(con, p)
        if a.id:
            chain = S.history(con, a.id)
            if a.json:
                _out(chain, True, "")
            else:
                for r in chain:
                    print(_line(r, 10_000))
                    print(
                        f"    valid {r['valid_from'] or '?'}..{r['valid_to'] or ''} · scope {r['scope']} · origin "
                        f"{r['origin']} · updated {r['updated']}"
                        + (f" · thing '{r['thing']}'" if r["thing"] else "")
                        + (f" · src '{r['src']}'" if r["src"] else "")
                    )
            return 0
        q, args = "SELECT * FROM records WHERE doc=?", [S.DOC]
        if a.status != "any":
            q += " AND status=?"
            args.append(a.status)
        if a.kind:
            q += " AND kind=?"
            args.append(a.kind)
        if a.section:
            q += " AND section=?"
            args.append(S.find_section(con, a.section))
        if a.grep:
            q += " AND (text LIKE ? OR id = ?)"
            args += [f"%{a.grep}%", a.grep.strip()]
        rows = [S.as_dict(r) for r in con.execute(q + " ORDER BY pos", args)]
        if a.json:
            _out(rows, True, "")
        else:
            for r in rows:
                print(_line(r))
            if not rows:
                hint = "" if a.status == "any" else " (--status any adds superseded and removed ones)"
                print(f"memory show: no {'' if a.status == 'any' else a.status + ' '}rows match{hint}", file=sys.stderr)
        return 0

    if a.cmd == "ledger":
        if not a.no_refresh:
            S.refresh(con, p)
        rows = [r for r in S.ledger(con) if r["negative"] or not a.negative]
        if a.json:
            _out(rows, True, "")
        else:
            for r in rows:
                print(f"{r['id']}  {r['day']:02d}.{r['month']:02d} {r['verb']}: {r['thing']}")
        return 0

    if a.cmd == "set":
        if not a.section and not a.after:
            raise S.Refused("set needs --section S (or --after ID)")
        if a.raw:
            text = a.text
        else:
            if not a.src:
                raise S.Refused("a fact names its source: --src 'owner' / 'live' / '<agent>, DD.MM' (or --raw)")
            text = S.compose_fact(a.text, a.src, S.parse_dm(a.date, S.today()) if a.date else S.today())
        return _write(a, p, con, S.op_set(a.section or "", text, source=a.source, scope=a.scope, after=a.after))

    if a.cmd == "decide":
        verb, thing, details, when = a.verb, a.thing, a.details, a.date
        if thing is None:  # one ledger-shaped string: "[DD.MM ]verb: thing (details)"
            d = S.parse_ledger_arg(a.verb)
            for flag, mine, theirs in (("--date", when, d["date"]), ("--details", details, d["details"])):
                if mine and theirs and mine.strip() != theirs.strip():
                    raise S.Usage(f"{flag} {mine!r} and the line's own {theirs!r}: give it once")
            verb, thing, details, when = d["verb"], d["thing"], details or d["details"], when or d["date"]
        return _write(a, p, con, S.op_decide(verb, thing, details, when=when, supersedes=a.supersedes))

    if a.cmd == "supersede":
        text = a.text
        if text and not a.raw and a.src:
            text = S.compose_fact(text, a.src, S.parse_dm(a.date, S.today()) if a.date else S.today())
        return _write(a, p, con, S.op_supersede(a.id, text, remove=a.remove, source=a.source))

    if a.cmd == "core":
        return _core(a, p, con)
    return 2


def _core_refresh(p: S.Paths, con: Any, quiet: bool, want: str | None = None) -> dict[str, Any] | None:
    """Keep FACTS-core.md current after an import or a write; a failure here never fails the import or write.
    Returns write_file's report (None on a failure)."""
    try:
        r = corepack.write_file(con, p, want=want)
    except (S.MemError, OSError, ValueError) as e:
        print(f"memory: FACTS-core.md not refreshed: {e}", file=sys.stderr)
        return None
    if r.get("over_budget"):
        print(f"memory: FACTS-core.md over budget (~{r['tokens_est']} > {r['budget_tokens']} tokens)", file=sys.stderr)
    if not quiet and r.get("changed"):
        print(f"memory: FACTS-core.md re-rendered (~{r['tokens_est']} tokens est.)")
    return r


def _core(a: argparse.Namespace, p: S.Paths, con: Any) -> int:
    if a.action == "set":
        if len(a.args) != 2:
            raise S.Refused("memory core set ID in|out|auto")
        res = corepack.set_flag(con, a.args[0], a.args[1])
        r = corepack.write_file(con, p)
        res.update(core_changed=r.get("changed"), tokens_est=r.get("tokens_est"))
        _out(res, a.json, f"memory core: {res['id']} {res['flag']} · FACTS-core.md ~{res['tokens_est']} tokens est.")
        return 0
    if a.args:
        raise S.Refused(f"memory core {a.action} takes no arguments")
    if a.action == "write":
        r = corepack.write_file(con, p)
        _out(
            r,
            a.json,
            f"memory core: {r['path']} "
            + ("skipped (core-off)" if r.get("skipped") else ("re-rendered" if r["changed"] else "current"))
            + (f" · ~{r['tokens_est']} tokens est." if "tokens_est" in r else ""),
        )
        return 0
    if a.action == "explain":
        rows = corepack.explain(con, p)
        if a.section:
            sec = S.find_section(con, a.section)
            rows = [r for r in rows if r["section"] == sec]
        if a.out_only:
            rows = [r for r in rows if not r["inc"]]
        if a.json:
            _out(rows, True, "")
        else:
            for r in rows:
                print(
                    f"{r['id']}  {'IN ' if r['inc'] else 'out'}  {r['rule']:17}  {r['chars']:5d}  "
                    f"{r['section'][:16]:16}  {r['subject']}" + ("  [thing only]" if r["form"] == "thing" else "")
                )
        return 0
    data, rep = corepack.build(con, p)
    target = corepack.core_path(p)
    try:
        current = target.read_bytes() == data
    except FileNotFoundError:
        current = None
    rep = {k: v for k, v in rep.items() if k != "decisions"}
    rep.update(path=str(target), file_current=current, core_off=corepack.off_file(p).exists())
    _out(
        rep,
        a.json,
        f"memory core: {rep['facts_in']} facts in, {rep['facts_out']} on file only · {rep['ledger']} ledger lines "
        f"({rep['ledger_shortened']} shortened to the thing) · {rep['chars']} chars ≈ {rep['tokens_est']} tokens "
        f"(est. at {rep['chars_per_token']} chars/token; budget {rep['budget_tokens']}"
        + (", OVER" if rep["over_budget"] else "")
        + ") · rules "
        + " ".join(f"{k}={v['in']}/{v['out']}" for k, v in rep["rules"].items())
        + f" (in/out) · {target.name} "
        + {True: "current", False: "STALE", None: "missing"}[current]
        + (" · core-off set" if rep["core_off"] else ""),
    )
    return 0


def _restore(p: S.Paths, con: Any) -> int:
    """render --write: only when FACTS.md is missing (edits go through set/supersede/decide)."""
    with locks.hold(p.lock, wait=30, dir_mode=0o700) as fd:
        if fd is None:
            raise S.Busy("another memory write holds the lock")
        if p.facts.exists():
            raise S.Refused(f"{p.facts} exists: nothing to restore (writes go through set/supersede/decide)")
        out = S.render(con)
        if not out:
            raise S.Refused("the store holds no rows: nothing to restore")
        jsonstate.write_bytes(p.facts, out, mode=0o644)
        with S.tx(con):
            S.set_meta(con, "records_render_sha", S.sha256(out))
            S.log(con, "restore-file", None, None, "tool", f"{len(out)} bytes")
    print(f"memory render: restored {p.facts} from the rows ({len(out)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
