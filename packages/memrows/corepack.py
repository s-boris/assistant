"""memrows.corepack: the facts core, a short always-loaded view of the memory rows.

FACTS-core.md (next to FACTS.md) is what the main session's CLAUDE.md imports instead of the whole FACTS.md.
FACTS.md stays the full rendered file; the facts left out of the core reach the session per
message (the memory2 hook pack), through `kb ask` / `recall`, or by reading FACTS.md.

What goes in: a small rule set, first match wins, and a per-row flag (`memory core set ID in|out|auto`):
  pin        the row's own flag (table core_flags; it follows the row through edits along its supersedes chain)
  topic      scope 'topic' (`memory set --scope topic`): out
  ledger-aged  a `done` ledger row dated more than done_core_days days ago (0 = off, the default): out; every other
             verb (declined, skipped, scratched, removed, off, decided, kept) always stays
  ledger     every other active ledger row: in, whole when it is at most ledger_whole_max characters, else shortened
             to "DD.MM verb: thing" (with done_core_days off the thing list stays complete, so check-before-ask still
             works; FACTS.md keeps every line either way)
  basics     a fact in one of basics_sections (at most basics_max characters when that is set): in, whole
  reference  a fact in one of reference_sections: out
  short      a fact of at most short_max characters: in, whole
  long       any other fact: out
Facts left out are named, a few words each, on one "More on file:" line per section (index), so the session knows
what to look up; sections in sections_without_index (name prefixes, like the other section lists) get no such line,
and a section with nothing in the core and no index line is left out whole. Budget: when the render passes
budget_tokens (estimated as characters / chars_per_token), facts placed by the rule 'short' leave first (longest
first), then the longest ledger lines shrink to their thing, then the "More on file" lines go, then basics leave
(longest first); pinned rows and the ledger thing list (less the aged `done` lines) never leave. The
default chars_per_token 1.75 is conservative: the Opus 5.5 tokenizer measured ~2.15 on FACTS-core.md and ~2.2 on
FACTS.md (`claude -p` usage with and without the import). Parameters: <conf>/core.json (keys as DEFAULTS; private),
else DEFAULTS. The file <conf>/core-off stops writing FACTS-core.md (render still works).
Slim knobs (defaults = the output above): aged_verbs {verb: days} ages decided/kept/done lines out like
done_core_days (the never-re-pitch verbs declined/skipped/scratched/removed/off can't be aged: refused);
ledger_style "grouped" renders the ledger as one line per verb ("- declined: a · b · c", newest first) under
ledger_intro (empty = the section's own text); header replaces the header template (same {fields}).
`memory render --core --core-config FILE` renders a candidate config without touching the live file.
"""

from __future__ import annotations

import datetime as _dt
import json
import math
import re
import sqlite3
from pathlib import Path
from typing import Any

from core import clock, jsonstate
from memrows import store as S

CORE_DOC = "FACTS-core.md"
FLAGS = ("in", "out")
DEFAULTS: dict[str, Any] = {
    "budget_tokens": 6000,
    "chars_per_token": 1.75,
    "basics_sections": [],
    "basics_max": 0,
    "reference_sections": [],
    "short_max": 180,
    "ledger_whole_max": 100,
    "index": True,
    "index_words": 4,
    "sections_without_index": [],
    "done_core_days": 0,
    "aged_verbs": {},
    "ledger_style": "lines",
    "ledger_intro": "",
    "header": "",
}
RULES = ("pin", "topic", "ledger-aged", "ledger", "basics", "reference", "short", "long")
AGED_VERBS = ("done",)
# The never-re-pitch verbs stay in the core whatever the config says (check-before-ask reads them every turn).
KEEP_VERBS = ("declined", "skipped", "scratched", "removed", "off")
VERB_ORDER = KEEP_VERBS + ("decided", "kept", "done")
LEDGER_STYLES = ("lines", "grouped")
DDL = "CREATE TABLE IF NOT EXISTS core_flags(id TEXT PRIMARY KEY, flag TEXT NOT NULL, ts TEXT, actor TEXT)"
SUBJECT_CUT = re.compile(r": | \(|; | — | = |, ")

HEADER = (
    "# Facts core\n"
    "Generated from the memory rows (`memory render --core`; don't edit): basics, short facts, every ledger thing"
    '{ledger_except} ({facts_in} of {facts} facts). The other {facts_out} ("More on file") and the ledger details '
    'come per message in the `memory2:` block, else `kb ask "…"`, `recall`, `memory show --grep WORD` or '
    "~/workspace/FACTS.md. Writes: `memory set|supersede|decide`."
)


# ---------------------------------------------------------------- config, flags


def core_path(p: S.Paths) -> Path:
    return p.facts.with_name(CORE_DOC)


def off_file(p: S.Paths) -> Path:
    return p.conf / "core-off"


def load_config(p: S.Paths | None, override: Path | None = None) -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    if p is not None or override is not None:
        f = override if override is not None else p.conf / "core.json"  # type: ignore[union-attr]
        try:
            got = json.loads(f.read_text(encoding="utf-8"))
        except FileNotFoundError:
            got = {}
        except (OSError, ValueError) as e:
            raise S.Refused(f"{f}: not readable JSON ({e})") from e
        unknown = sorted(set(got) - set(DEFAULTS))
        if unknown:
            raise S.Refused(f"{f}: unknown keys {', '.join(unknown)} (known: {', '.join(DEFAULTS)})")
        cfg.update(got)
    bad = sorted(set(cfg["aged_verbs"] or {}) & set(KEEP_VERBS))
    if bad:
        raise S.Refused(f"core config: aged_verbs can't age {', '.join(bad)} (never-re-pitch verbs stay in the core)")
    if cfg["ledger_style"] not in LEDGER_STYLES:
        raise S.Refused(f"core config: ledger_style must be one of {', '.join(LEDGER_STYLES)}")
    return cfg


def _age_limits(cfg: dict[str, Any]) -> dict[str, int]:
    """verb -> days after which its ledger lines leave the core (done_core_days counts for done)."""
    lim = {str(k): int(v) for k, v in (cfg.get("aged_verbs") or {}).items() if int(v) > 0}
    if int(cfg["done_core_days"]) > 0:
        lim["done"] = min(lim.get("done", 10**6), int(cfg["done_core_days"]))
    return lim


def ensure(con: sqlite3.Connection) -> None:
    con.execute(DDL)


def flags(con: sqlite3.Connection) -> dict[str, str]:
    ensure(con)
    return {r[0]: r[1] for r in con.execute("SELECT id, flag FROM core_flags")}


def set_flag(con: sqlite3.Connection, rid: str, flag: str, actor: str = "tool") -> dict[str, Any]:
    """in | out pins the row (and its later edits); auto hands it back to the rules."""
    if flag not in (*FLAGS, "auto"):
        raise S.Refused("core flag: in | out | auto")
    ensure(con)
    with S.tx(con):
        r = S.get(con, rid)
        if r["status"] != "active":
            raise S.Refused(f"{r['id']} is {r['status']}: flag the active row ({r['superseded_by'] or '-'})")
        chain = _chain(con, r["id"])
        con.execute(f"DELETE FROM core_flags WHERE id IN ({','.join('?' * len(chain))})", chain)
        if flag != "auto":
            con.execute("INSERT INTO core_flags(id, flag, ts, actor) VALUES(?,?,?,?)", (r["id"], flag, S.ts(), actor))
        S.log(con, "core", r["id"], None, actor, flag)
    return {"id": r["id"], "flag": flag}


def _chain(con: sqlite3.Connection, rid: str) -> list[str]:
    """The row and every row it superseded, newest first."""
    out, seen = [], set()
    cur: str | None = rid
    while cur and cur not in seen and len(out) < 50:
        seen.add(cur)
        out.append(cur)
        row = con.execute("SELECT supersedes FROM records WHERE id=?", (cur,)).fetchone()
        cur = row[0] if row else None
    return out


# ---------------------------------------------------------------- the rules


def subject(text: str, words: int = 6) -> str:
    """A fact's few naming words: its claim up to the first ': ', ' (', '; ', ' — ', ' = ' or ', '."""
    claim, _src = S.fact_fields(text)
    head = SUBJECT_CUT.split(claim, maxsplit=1)[0].strip()
    ws = head.split()
    if len(ws) > words:
        head = " ".join(ws[:words])
    return head.strip(" .,:;") or claim[:40]


def short_ledger(r: sqlite3.Row) -> str:
    d = S.decision_fields(r["text"]) or {}
    if not d:
        return r["text"]
    return f"{d['day_txt']} {d['verb']}: {d['thing']}"


def _in_sections(section: str, names: list[str]) -> bool:
    s = section.lower()
    return any(s.startswith(str(n).lower()) for n in names)


def ledger_date(text: str, today: _dt.date) -> _dt.date | None:
    """A ledger line's date: its own year, else the latest DD.MM not after today (a date that would land in the
    future is last year's); None when the line has no readable date."""
    d = S.decision_fields(text) or {}
    if not d:
        return None
    try:
        day, month = int(d["day"]), int(d["month"])
        if d.get("year"):
            return _dt.date(int(d["year"]), month, day)
        got = _dt.date(today.year, month, day)
        return got if got <= today else _dt.date(today.year - 1, month, day)
    except ValueError:
        return None


def _aged(r: sqlite3.Row, cfg: dict[str, Any], today: _dt.date) -> bool:
    lim = _age_limits(cfg)
    if not lim:
        return False
    d = S.decision_fields(r["text"]) or {}
    days = lim.get(str(d.get("verb")))
    if not days or d.get("verb") in KEEP_VERBS:
        return False
    when = ledger_date(r["text"], today)
    return when is not None and (today - when).days > days


def decide(
    rows: list[sqlite3.Row], cfg: dict[str, Any], pins: dict[str, str], today: _dt.date | None = None
) -> list[dict[str, Any]]:
    """One decision per active row: {row, inc, rule, form (whole|thing)}."""
    today = today or clock.today()
    out = []
    for r in rows:
        pin = pins.get(r["id"])
        if pin:
            inc, rule = pin == "in", "pin"
        elif r["scope"] == "topic":
            inc, rule = False, "topic"
        elif r["kind"] == "decision" and _aged(r, cfg, today):
            inc, rule = False, "ledger-aged"
        elif r["kind"] == "decision":
            inc, rule = True, "ledger"
        elif _in_sections(r["section"], cfg["basics_sections"]) and (
            not int(cfg["basics_max"]) or len(r["text"]) <= int(cfg["basics_max"])
        ):
            inc, rule = True, "basics"
        elif _in_sections(r["section"], cfg["reference_sections"]):
            inc, rule = False, "reference"
        elif len(r["text"]) <= int(cfg["short_max"]):
            inc, rule = True, "short"
        else:
            inc, rule = False, "long"
        form = "whole"
        if inc and r["kind"] == "decision" and len(r["text"]) > int(cfg["ledger_whole_max"]):
            form = "thing"
        out.append({"row": r, "inc": inc, "rule": rule, "form": form})
    return out


def _pins(con: sqlite3.Connection, rows: list[sqlite3.Row]) -> dict[str, str]:
    """Each active row's effective flag: its own, else the nearest one up its supersedes chain."""
    fl = flags(con)
    if not fl:
        return {}
    sup = {r[0]: r[1] for r in con.execute("SELECT id, supersedes FROM records")}
    got = {}
    for r in rows:
        cur, n = r["id"], 0
        while cur and n < 50:
            if cur in fl:
                got[r["id"]] = fl[cur]
                break
            cur, n = sup.get(cur), n + 1
    return got


# ---------------------------------------------------------------- render


def est_tokens(text: str, cfg: dict[str, Any]) -> int:
    return math.ceil(len(text) / float(cfg["chars_per_token"]))


def _compose(con: sqlite3.Connection, dec: list[dict[str, Any]], cfg: dict[str, Any], doc: str) -> str:
    layout = con.execute("SELECT pos, section, kind, text FROM doc_lines WHERE doc=? ORDER BY pos", (doc,)).fetchall()
    order: list[str] = []
    texts: dict[str, list[str]] = {}
    for _pos, section, kind, text in layout:
        if kind == "heading" and section not in texts:
            order.append(section)
            texts[section] = []
        elif kind == "text" and section in texts:
            texts[section].append(text)
    per: dict[str, list[dict[str, Any]]] = {}
    for d in dec:
        sec = d["row"]["section"]
        if sec not in texts:
            order.append(sec)
            texts[sec] = []
        per.setdefault(sec, []).append(d)
    n_facts = sum(1 for d in dec if d["row"]["kind"] == "fact")
    n_in = sum(1 for d in dec if d["row"]["kind"] == "fact" and d["inc"])
    n_led = sum(1 for d in dec if d["row"]["kind"] == "decision" and d["inc"])
    lim = _age_limits(cfg)
    except_ = " except " + ", ".join(f"`{v}` lines older than {n} days" for v, n in sorted(lim.items())) if lim else ""
    head = (cfg["header"] or HEADER).format(
        facts_in=n_in, facts=n_facts, facts_out=n_facts - n_in, ledger=n_led, ledger_except=except_
    )
    parts = [head]
    for sec in order:
        ds = per.get(sec, [])
        ins = [d for d in ds if d["inc"]]
        outs = [d for d in ds if not d["inc"]]
        if not ds:
            continue
        lines = [f"## {sec}"] + texts.get(sec, [])
        led = [d for d in ins if d["row"]["kind"] == "decision"] if cfg["ledger_style"] == "grouped" else []
        if led:
            if cfg["ledger_intro"]:
                lines = [f"## {sec}", cfg["ledger_intro"]]
            lines += _grouped(led)
            ins = [d for d in ins if d["row"]["kind"] != "decision"]
        for d in ins:
            r = d["row"]
            lines.append("- " + (short_ledger(r) if d["form"] == "thing" else r["text"]))
        outs = [d for d in outs if d["rule"] != "ledger-aged"]  # FACTS.md, the hook pack and kb find those
        index = bool(outs) and cfg["index"] and not _in_sections(sec, cfg["sections_without_index"])
        if not ins and not index and not led:
            continue  # nothing of this section in the core: no bare heading
        if index:
            names: list[str] = []
            for d in outs:
                nm = subject(d["row"]["text"], int(cfg["index_words"]))
                if nm not in names:
                    names.append(nm)
            lines.append("More on file: " + " · ".join(names))
        parts.append("\n".join(lines))
    return "\n\n".join(parts) + "\n"


def _grouped(led: list[dict[str, Any]]) -> list[str]:
    """Ledger rows as one line per verb, VERB_ORDER first, rows in document order (newest first): '- verb: a · b'."""
    per: dict[str, list[str]] = {}
    for d in led:
        f = S.decision_fields(d["row"]["text"]) or {}
        verb, thing = str(f.get("verb") or "other"), str(f.get("thing") or d["row"]["text"])
        if thing not in per.setdefault(verb, []):
            per[verb].append(thing)
    order = [v for v in VERB_ORDER if v in per] + sorted(v for v in per if v not in VERB_ORDER)
    return [f"- {v}: " + " · ".join(per[v]) for v in order]


def build(
    con: sqlite3.Connection, p: S.Paths | None = None, doc: str = S.DOC, cfg_file: Path | None = None
) -> tuple[bytes, dict[str, Any]]:
    """(the core document, a report: counts, estimated tokens, budget steps, per-rule counts). cfg_file: a candidate
    config in place of <conf>/core.json (render only)."""
    cfg = load_config(p, cfg_file)
    rows = S.active(con, doc)
    dec = decide(rows, cfg, _pins(con, rows))
    budget = int(cfg["budget_tokens"])
    text = _compose(con, dec, cfg, doc)
    steps: list[str] = []
    if est_tokens(text, cfg) > budget:
        shorts = sorted((d for d in dec if d["inc"] and d["rule"] == "short"), key=lambda d: -len(d["row"]["text"]))
        for d in shorts:
            d["inc"], d["rule"] = False, "short-over-budget"
            steps.append(f"drop {d['row']['id']}")
            text = _compose(con, dec, cfg, doc)
            if est_tokens(text, cfg) <= budget:
                break
    if est_tokens(text, cfg) > budget:
        led = sorted(
            (d for d in dec if d["inc"] and d["row"]["kind"] == "decision" and d["form"] == "whole"),
            key=lambda d: -len(d["row"]["text"]),
        )
        for d in led:
            d["form"] = "thing"
            steps.append(f"shrink {d['row']['id']}")
            text = _compose(con, dec, cfg, doc)
            if est_tokens(text, cfg) <= budget:
                break
    if est_tokens(text, cfg) > budget and cfg["index"]:
        cfg = dict(cfg, index=False)
        steps.append("index off")
        text = _compose(con, dec, cfg, doc)
    if est_tokens(text, cfg) > budget:
        basics = sorted((d for d in dec if d["inc"] and d["rule"] == "basics"), key=lambda d: -len(d["row"]["text"]))
        for d in basics:
            d["inc"], d["rule"] = False, "basics-over-budget"
            steps.append(f"drop {d['row']['id']}")
            text = _compose(con, dec, cfg, doc)
            if est_tokens(text, cfg) <= budget:
                break
    rules: dict[str, dict[str, int]] = {}
    for d in dec:
        k = rules.setdefault(d["rule"], {"in": 0, "out": 0})
        k["in" if d["inc"] else "out"] += 1
    rep = {
        "bytes": len(text.encode("utf-8")),
        "chars": len(text),
        "tokens_est": est_tokens(text, cfg),
        "budget_tokens": budget,
        "over_budget": est_tokens(text, cfg) > budget,
        "chars_per_token": cfg["chars_per_token"],
        "facts_in": sum(1 for d in dec if d["row"]["kind"] == "fact" and d["inc"]),
        "facts_out": sum(1 for d in dec if d["row"]["kind"] == "fact" and not d["inc"]),
        "ledger": sum(1 for d in dec if d["row"]["kind"] == "decision" and d["inc"]),
        "ledger_out": sum(1 for d in dec if d["row"]["kind"] == "decision" and not d["inc"]),
        "ledger_shortened": sum(1 for d in dec if d["inc"] and d["form"] == "thing"),
        "rules": rules,
        "budget_steps": steps,
        "decisions": dec,
    }
    return text.encode("utf-8"), rep


def render(con: sqlite3.Connection, p: S.Paths | None = None, doc: str = S.DOC, cfg_file: Path | None = None) -> bytes:
    return build(con, p, doc, cfg_file)[0]


def placement(data: bytes, dec: list[dict[str, Any]], rid: str) -> dict[str, Any] | None:
    """Where row rid sits in a rendered core: in (its line and line number, whole or the thing only) or out (the
    rule); None when rid is no active row."""
    for d in dec:
        r = d["row"]
        if r["id"] != rid:
            continue
        line = ("- " + (short_ledger(r) if d["form"] == "thing" else r["text"])) if d["inc"] else None
        return {
            "id": rid,
            "inc": d["inc"],
            "rule": d["rule"],
            "form": d["form"] if d["inc"] else None,
            "line": line,
            "line_no": S.line_no(data, line),
        }
    return None


def write_file(con: sqlite3.Connection, p: S.Paths, want: str | None = None) -> dict[str, Any]:
    """Render the core and replace FACTS-core.md when its bytes differ (atomic). The import cron path calls this.
    want = a row id: the report's "row" says where that row landed (placement)."""
    target = core_path(p)
    if off_file(p).exists():
        return {"path": str(target), "skipped": "core-off", "changed": False}
    data, rep = build(con, p)
    if want:
        rep["row"] = placement(data, rep["decisions"], want)
    try:
        old = target.read_bytes()
    except FileNotFoundError:
        old = None
    changed = old != data
    if changed:
        jsonstate.write_bytes(target, data, mode=0o644)
    rep = {k: v for k, v in rep.items() if k != "decisions"}
    rep.update(path=str(target), changed=changed, sha=S.sha256(data))
    return rep


def explain(con: sqlite3.Connection, p: S.Paths | None = None, doc: str = S.DOC) -> list[dict[str, Any]]:
    _data, rep = build(con, p, doc)
    out = []
    for d in rep["decisions"]:
        r = d["row"]
        out.append(
            {
                "id": r["id"],
                "kind": r["kind"],
                "section": r["section"],
                "inc": d["inc"],
                "rule": d["rule"],
                "form": d["form"] if d["inc"] else None,
                "chars": len(r["text"]),
                "subject": subject(r["text"]) if r["kind"] == "fact" else r["thing"],
            }
        )
    return out
