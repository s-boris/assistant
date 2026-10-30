"""memrows.store: FACTS.md as rows in mem.db, rendered back byte for byte.

Schema 2 (additive: next to memory2's facts/review/cands tables in the same mem.db, which it leaves alone):
  records     one row per bullet ("- ...") under a "## " heading. text = the exact line after "- " (the render
              source); derived fields: kind fact|decision, section, claim + src (a fact's words before / after its
              last " — "), verb, thing, details, day (a ledger line), source owner|live|agent, verified_at,
              valid_from / valid_to, scope always|topic, status active|superseded|removed, supersedes /
              superseded_by, origin import|tool. Rows are never deleted.
  doc_lines   every other line (title, intro text, headings, blank lines, anything unusual): exact text + pos.
  record_log  one line per change: ts, op, id, other id, actor (hand = imported from the file, tool = a memory
              write), note. Ids and counts only, never fact text.
  meta        records_schema, records_import_sha / _render_sha (sha256 of the file the rows last matched),
              records_import_ts.

render(): active records ("- " + text) and doc_lines merged by pos, joined with "\\n" (the file's last newline is
the empty last line). import_doc(): parse the file and reconcile it with the active rows: the same text keeps its
row (a move keeps the id), an edited line becomes a new row that supersedes the old one, a new line a new row, a
deleted line status removed, a line that comes back restores its removed row.

The rows are the source: every write imports hand edits first, checks that the rows render the file byte for byte,
applies the change, renders FACTS.md and replaces it atomically (compare-and-swap on its bytes).
"""

from __future__ import annotations

import dataclasses
import difflib
import hashlib
import os
import re
import secrets
import sqlite3
import subprocess
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from core import clock, jsonstate, locks, paths

DOC = "FACTS.md"
SCHEMA = "2"
STEP = 1024.0
LEDGER_VERBS = ("declined", "skipped", "scratched", "removed", "off", "done", "decided", "kept")
NEG_VERBS = ("declined", "skipped", "scratched", "removed", "off")
SILENT_VERBS = ("declined", "skipped", "scratched")
SOURCES = ("owner", "live", "agent")
SCOPES = ("always", "topic")
REMOVE_LIMIT = 10  # an import may remove at most max(REMOVE_LIMIT, 20 % of the active rows) without allow_removals

# The two ledger matchers in use, on the text after "- ": board-lint's (pitch-check, handoff) and memory2's (it also
# takes a year). A ledger-section bullet is a decision when either one accepts it with a ledger verb.
LEDGER_BL = re.compile(r"^(\d{1,2})\.(\d{1,2})\.?\s+([a-z]+(?: call)?):\s*(.+)$")
LEDGER_M2 = re.compile(r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))? (" + "|".join(LEDGER_VERBS) + r"): (.+)$")
THING_SPLIT = re.compile(r" \(| — |; ")  # board-lint: a thing is the words before "(", " — " or "; "
DM = re.compile(r"(?<![\d.])(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?(?!\d)")
OWNER_RE = re.compile(r"\b(owner|by him|his|he|him)\b", re.I)
LIVE_RE = re.compile(
    r"\b(live|api|ha (state|list|history)|list_events|config|journal|vault|verified|sample|report|log)\b", re.I
)
AGENT_RE = re.compile(r"\b(agents?|research|web|planner|survey|rehearsal)\b", re.I)

DDL = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS records(
    id TEXT PRIMARY KEY,
    doc TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('fact', 'decision')),
    section TEXT NOT NULL,
    pos REAL NOT NULL,
    text TEXT NOT NULL,
    claim TEXT, src TEXT,
    verb TEXT, thing TEXT, details TEXT, day TEXT,
    source TEXT CHECK (source IS NULL OR source IN ('owner', 'live', 'agent')),
    verified_at TEXT, valid_from TEXT, valid_to TEXT,
    scope TEXT NOT NULL DEFAULT 'always' CHECK (scope IN ('always', 'topic')),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'superseded', 'removed')),
    supersedes TEXT, superseded_by TEXT, origin TEXT, created TEXT, updated TEXT);
CREATE INDEX IF NOT EXISTS records_live ON records(doc, status, pos);
CREATE INDEX IF NOT EXISTS records_text ON records(doc, text);
CREATE TABLE IF NOT EXISTS doc_lines(
    doc TEXT NOT NULL, pos REAL NOT NULL, section TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL,
    PRIMARY KEY (doc, pos));
CREATE TABLE IF NOT EXISTS record_log(
    n INTEGER PRIMARY KEY, ts TEXT, op TEXT, id TEXT, other TEXT, actor TEXT, note TEXT);
"""

COLS = (
    "id doc kind section pos text claim src verb thing details day source verified_at valid_from valid_to scope "
    "status supersedes superseded_by origin created updated"
).split()


class MemError(Exception):
    code = 1


class Usage(MemError):
    code = 2


class Refused(MemError):
    code = 3


class Mismatch(MemError):
    code = 4


class Busy(MemError):
    code = 5


class NotFound(MemError):
    code = 6


# ---------------------------------------------------------------- paths


@dataclasses.dataclass(frozen=True)
class Paths:
    db: Path
    facts: Path
    conf: Path

    @property
    def lock(self) -> Path:
        return self.db.parent / "records.lock"


def live_paths() -> Paths:
    """The defaults: ~/.local/state/memory2/mem.db, ~/workspace/FACTS.md, ~/.config/memory2 (memory2's env names
    MEMORY2_STATE / MEMORY2_FACTS / MEMORY2_CONF are honoured, then MEMORY_DB / MEMORY_FACTS)."""
    h = paths.home()
    st = os.environ.get("MEMORY2_STATE")
    db = os.environ.get("MEMORY_DB") or (os.path.join(st, "mem.db") if st else h / ".local/state/memory2/mem.db")
    facts = os.environ.get("MEMORY_FACTS") or os.environ.get("MEMORY2_FACTS") or h / "workspace" / DOC
    conf = os.environ.get("MEMORY2_CONF") or h / ".config/memory2"
    return Paths(paths.guard(db), paths.guard(facts), paths.guard(conf))


def resolve_paths(db: str | None = None, facts: str | None = None) -> Paths:
    """Explicit --db/--facts win. A FACTS.md copy never goes into the live store: it needs its own --db."""
    live = live_paths()
    p = Paths(paths.guard(db) if db else live.db, paths.guard(facts) if facts else live.facts, live.conf)
    if p.db.resolve() == live.db.resolve() and p.facts.resolve() != live.facts.resolve():
        raise Refused(f"{p.facts} is not the live {DOC}: give it its own --db (the live store holds only {live.facts})")
    return p


# ---------------------------------------------------------------- small helpers


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def today() -> date:
    return clock.today()


def ts() -> str:
    return clock.now_dt().strftime("%Y-%m-%dT%H:%M:%S")


def new_id(con: sqlite3.Connection) -> str:
    while True:
        rid = "r" + secrets.token_hex(5)
        if not con.execute("SELECT 1 FROM records WHERE id=?", (rid,)).fetchone():
            return rid


def iso_dm(day: str | int, month: str | int, year: str | int | None, ref: date) -> str | None:
    """DD.MM[.YYYY] -> YYYY-MM-DD; without a year: ref's year, unless that lies > 60 days after ref."""
    try:
        d = date(int(year) if year else ref.year, int(month), int(day))
    except ValueError:
        return None
    if not year and d > ref + timedelta(days=60):
        try:
            d = date(d.year - 1, d.month, d.day)
        except ValueError:
            return None
    return d.isoformat()


def last_dm(text: str | None, ref: date) -> str | None:
    best = None
    for m in DM.finditer(text or ""):
        v = iso_dm(m.group(1), m.group(2), m.group(3), ref)
        if v:
            best = v
    return best


def dm_text(d: date) -> str:
    return f"{d.day:02d}.{d.month:02d}"


def parse_dm(s: str, ref: date) -> date:
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.?(\d{4})?", s.strip())
    v = iso_dm(m.group(1), m.group(2), m.group(3), ref) if m else None
    if not v:
        raise Refused(f"not a DD.MM date: {s!r}")
    return date.fromisoformat(v)


# ---------------------------------------------------------------- parsing


def decision_fields(body: str) -> dict[str, Any] | None:
    """The ledger fields of one bullet's text (after "- "), or None when it is no ledger line."""
    s = body.strip()
    year = None
    m = LEDGER_BL.match(s)
    if m and (m.group(3).lower() if m.group(3).lower() != "his call" else "decided") in LEDGER_VERBS:
        day, month, verb, rest = m.group(1), m.group(2), m.group(3), m.group(4)
    else:
        m2 = LEDGER_M2.match(s)
        if not m2:
            return None
        day, month, year, verb, rest = m2.group(1), m2.group(2), m2.group(3), m2.group(4), m2.group(5)
    head = THING_SPLIT.split(rest, maxsplit=1)[0]
    details = re.sub(r"^(?:;|—)\s*", "", rest[len(head) :].strip())
    if details.startswith("(") and details.endswith(")"):
        details = details[1:-1]
    return {
        "day": day,
        "month": month,
        "year": year,
        "verb": verb,
        "thing": head.strip(),
        "details": details or None,
        "rest": rest,
        "day_txt": s.split(" ", 1)[0],
    }


def fact_fields(body: str) -> tuple[str, str | None]:
    """A fact's claim and its attribution (the words after its last " — "; None without one)."""
    if " — " in body:
        claim, src = body.rsplit(" — ", 1)
        return claim.strip(), src.strip() or None
    return body.strip(), None


def classify_source(src: str | None) -> str | None:
    """owner | live | agent from a fact's attribution (a heuristic for imported lines; tool writes say it)."""
    if not src:
        return None
    if OWNER_RE.search(src):
        return "owner"
    if LIVE_RE.search(src):
        return "live"
    if AGENT_RE.search(src):
        return "agent"
    return "owner" if "[[" in src else "agent"


@dataclasses.dataclass
class Line:
    pos: float
    section: str
    kind: str  # fact | decision (records) · title | heading | blank | text (layout)
    text: str  # a record: the text after "- " · layout: the whole line

    @property
    def is_record(self) -> bool:
        return self.kind in ("fact", "decision")


def is_ledger_heading(line: str) -> bool:
    return line.startswith("## ") and "ledger" in line.lower()


def parse(text: str) -> list[Line]:
    out: list[Line] = []
    section, ledger = "", False
    for i, line in enumerate(text.split("\n")):
        pos = (i + 1) * STEP
        if line.startswith("## "):
            section, ledger = line[3:].strip() or line, is_ledger_heading(line)
            out.append(Line(pos, section, "heading", line))
        elif line.startswith("- ") and section:
            body = line[2:]
            out.append(Line(pos, section, "decision" if ledger and decision_fields(body) else "fact", body))
        else:
            kind = "blank" if not line.strip() else ("title" if line.startswith("# ") and not section else "text")
            out.append(Line(pos, section, kind, line))
    return out


def derived(kind: str, text: str, ref: date) -> dict[str, Any]:
    """The fields a record's text implies (the text stays the render source)."""
    f: dict[str, Any] = dict.fromkeys(("claim", "src", "verb", "thing", "details", "day", "source", "verified_at"))
    if kind == "decision":
        d = decision_fields(text)
        if d is None:
            raise Refused("not a ledger line: want 'DD.MM verb: thing (details)' with verb " + "|".join(LEDGER_VERBS))
        f.update(
            verb=d["verb"],
            thing=d["thing"],
            details=d["details"],
            day=d["day_txt"],
            source="owner",
            verified_at=iso_dm(d["day"], d["month"], d["year"], ref),
        )
    else:
        claim, src = fact_fields(text)
        f.update(claim=claim, src=src, source=classify_source(src), verified_at=last_dm(src, ref))
    return f


# ---------------------------------------------------------------- store


def connect(db: Path | str) -> sqlite3.Connection:
    """Open (and create) the store; isolation_level None: transactions are explicit (BEGIN IMMEDIATE)."""
    if str(db) == ":memory:":
        con = sqlite3.connect(":memory:", isolation_level=None)
    else:
        p = Path(db)
        p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        con = sqlite3.connect(str(p), timeout=30, isolation_level=None)
        os.chmod(p, 0o600)
        con.execute("PRAGMA journal_mode=WAL")
    con.row_factory = sqlite3.Row
    con.executescript(DDL)
    con.execute("INSERT OR IGNORE INTO meta(k, v) VALUES('records_schema', ?)", (SCHEMA,))
    return con


def meta(con: sqlite3.Connection, k: str, default: str | None = None) -> str | None:
    r = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r[0] if r else default


def set_meta(con: sqlite3.Connection, k: str, v: str) -> None:
    con.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)", (k, v))


@contextmanager
def tx(con: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
    except BaseException:
        con.execute("ROLLBACK")
        raise
    con.execute("COMMIT")


def log(con: sqlite3.Connection, op: str, rid: str | None, other: str | None, actor: str, note: str = "") -> None:
    con.execute(
        "INSERT INTO record_log(ts, op, id, other, actor, note) VALUES(?,?,?,?,?,?)",
        (ts(), op, rid, other, actor, note),
    )


def active(con: sqlite3.Connection, doc: str = DOC) -> list[sqlite3.Row]:
    return con.execute("SELECT * FROM records WHERE doc=? AND status='active' ORDER BY pos", (doc,)).fetchall()


def get(con: sqlite3.Connection, rid: str) -> sqlite3.Row:
    r = con.execute("SELECT * FROM records WHERE id=?", (rid,)).fetchone()
    if r is None:
        rows = con.execute("SELECT * FROM records WHERE id LIKE ? LIMIT 2", (rid + "%",)).fetchall()
        if len(rows) != 1:
            raise NotFound(f"no record {rid!r}" + (" (ambiguous prefix)" if rows else ""))
        r = rows[0]
    return r


def insert(
    con: sqlite3.Connection,
    *,
    kind: str,
    section: str,
    pos: float,
    text: str,
    origin: str,
    ref: date,
    scope: str = "always",
    source: str | None = None,
    supersedes: str | None = None,
    doc: str = DOC,
) -> str:
    f = derived(kind, text, ref)
    if source:
        f["source"] = source
    rid, now = new_id(con), ts()
    con.execute(
        "INSERT INTO records(id, doc, kind, section, pos, text, claim, src, verb, thing, details, day, source, "
        "verified_at, valid_from, valid_to, scope, status, supersedes, superseded_by, origin, created, updated) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,'active',?,NULL,?,?,?)",
        (
            rid,
            doc,
            kind,
            section,
            pos,
            text,
            f["claim"],
            f["src"],
            f["verb"],
            f["thing"],
            f["details"],
            f["day"],
            f["source"],
            f["verified_at"],
            f["verified_at"] if origin == "import" and f["verified_at"] else ref.isoformat(),
            scope,
            supersedes,
            origin,
            now,
            now,
        ),
    )
    return rid


def retire(con: sqlite3.Connection, rid: str, status: str, ref: date, by: str | None = None) -> None:
    con.execute(
        "UPDATE records SET status=?, valid_to=?, superseded_by=COALESCE(?, superseded_by), updated=? WHERE id=?",
        (status, ref.isoformat(), by, ts(), rid),
    )


# ---------------------------------------------------------------- render


def render(con: sqlite3.Connection, doc: str = DOC, scope: str | None = None) -> bytes:
    """The document from the rows: active records and layout lines by pos (scope='always' leaves out topic rows)."""
    q = (
        "SELECT pos, '- ' || text AS t FROM records WHERE doc=? AND status='active'"
        + (" AND scope=?" if scope else "")
        + " UNION ALL SELECT pos, text AS t FROM doc_lines WHERE doc=? ORDER BY pos"
    )
    args: tuple[Any, ...] = (doc, scope, doc) if scope else (doc, doc)
    return "\n".join(r[1] for r in con.execute(q, args)).encode("utf-8")


def renumber(con: sqlite3.Connection, doc: str = DOC) -> None:
    """Spread every rendered item to (i + 1) * STEP in its current order (keeps midpoint inserts exact)."""
    items = con.execute(
        "SELECT 'r' AS t, id AS k, pos FROM records WHERE doc=? AND status='active' "
        "UNION ALL SELECT 'l', CAST(pos AS TEXT), pos FROM doc_lines WHERE doc=? ORDER BY pos",
        (doc, doc),
    ).fetchall()
    layout = [(r[2], (i + 1) * STEP) for i, r in enumerate(items) if r[0] == "l"]
    recs = [(r[1], (i + 1) * STEP) for i, r in enumerate(items) if r[0] == "r"]
    # layout positions are a primary key: park them out of the way first, then set the final values
    con.execute("UPDATE doc_lines SET pos = -pos - 1 WHERE doc=?", (doc,))
    for old, new in layout:
        con.execute("UPDATE doc_lines SET pos=? WHERE doc=? AND pos=?", (new, doc, -old - 1))
    for rid, new in recs:
        con.execute("UPDATE records SET pos=? WHERE id=?", (new, rid))


# ---------------------------------------------------------------- import (file -> rows)


def _score(old: sqlite3.Row, new: Line) -> float:
    """How likely `new` is a hand edit of `old` (0..1.1)."""
    if old["kind"] != new.kind:
        return 0.0
    a, b = old["text"], new.text
    bonus = 0.05 if old["section"] == new.section else 0.0
    if new.kind == "decision":
        d = decision_fields(b)
        if d and old["thing"] and d["thing"].lower() == str(old["thing"]).lower():
            return 1.0 + bonus
    n = 0
    for x, y in zip(a, b, strict=False):
        if x != y:
            break
        n += 1
    sm = difflib.SequenceMatcher(None, a, b)
    if n < 25 and (sm.real_quick_ratio() < 0.5 or sm.quick_ratio() < 0.5):
        return 0.0
    r = sm.ratio()
    if n >= 25:
        r = max(r, 0.85)
    return r + bonus


def import_doc(
    con: sqlite3.Connection,
    data: bytes,
    *,
    actor: str = "hand",
    allow_removals: bool = False,
    doc: str = DOC,
    ref: date | None = None,
) -> dict[str, Any]:
    """Reconcile the rows with the file's bytes (call inside a transaction). Returns the counts."""
    ref = ref or today()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise Refused(f"{doc} is not valid UTF-8 (byte {e.start}): nothing imported") from e
    lines = parse(text)
    recs = [ln for ln in lines if ln.is_record]
    old = active(con, doc)
    if not recs and old:
        raise Refused(f"{doc} has no bullets left: nothing imported (restore it with `memory render --write`)")
    by_text: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in old:
        by_text[r["text"]].append(r)
    match: dict[int, sqlite3.Row] = {}
    used: set[str] = set()
    for same_section in (True, False):
        for j, ln in enumerate(recs):
            if j in match:
                continue
            for r in by_text.get(ln.text, []):
                if r["id"] in used or (same_section and r["section"] != ln.section) or r["kind"] != ln.kind:
                    continue
                match[j] = r
                used.add(r["id"])
                break
    rest_old = [r for r in old if r["id"] not in used]
    rest_new = [j for j in range(len(recs)) if j not in match]
    pairs = []
    for j in rest_new:
        for r in rest_old:
            s = _score(r, recs[j])
            if s >= 0.6:
                pairs.append((s, -abs(r["pos"] - recs[j].pos), j, r["id"]))
    pairs.sort(reverse=True)
    edit: dict[int, sqlite3.Row] = {}
    old_by_id = {r["id"]: r for r in rest_old}
    for _s, _d, j, rid in pairs:
        if j in edit or rid in used:
            continue
        edit[j] = old_by_id[rid]
        used.add(rid)
    gone = [r for r in old if r["id"] not in used]
    limit = max(REMOVE_LIMIT, len(old) // 5)
    if len(gone) > limit and not allow_removals:
        raise Refused(
            f"{doc} would lose {len(gone)} of {len(old)} rows (limit {limit}): nothing imported; "
            "check the file, or import with --allow-removals"
        )
    c: dict[str, Any] = dict(kept=0, moved=0, edited=0, added=0, restored=0, removed=0)
    now = ts()
    for j, ln in enumerate(recs):
        if j in match:
            r = match[j]
            moved = r["section"] != ln.section
            con.execute("UPDATE records SET pos=?, section=? WHERE id=?", (ln.pos, ln.section, r["id"]))
            if moved:
                con.execute("UPDATE records SET updated=? WHERE id=?", (now, r["id"]))
                log(con, "move", r["id"], None, actor)
            c["moved" if moved else "kept"] += 1
        elif j in edit:
            r = edit[j]
            nid = insert(
                con,
                kind=ln.kind,
                section=ln.section,
                pos=ln.pos,
                text=ln.text,
                origin="import",
                ref=ref,
                scope=r["scope"],
                supersedes=r["id"],
                doc=doc,
            )
            retire(con, r["id"], "superseded", ref, by=nid)
            log(con, "edit", nid, r["id"], actor)
            c["edited"] += 1
        else:
            back = con.execute(
                "SELECT id FROM records WHERE doc=? AND status='removed' AND text=? AND kind=? "
                "ORDER BY updated DESC LIMIT 1",
                (doc, ln.text, ln.kind),
            ).fetchone()
            if back:
                con.execute(
                    "UPDATE records SET status='active', valid_to=NULL, pos=?, section=?, updated=? WHERE id=?",
                    (ln.pos, ln.section, now, back[0]),
                )
                log(con, "restore", back[0], None, actor)
                c["restored"] += 1
            else:
                nid = insert(con, kind=ln.kind, section=ln.section, pos=ln.pos, text=ln.text, origin="import", ref=ref)
                log(con, "add", nid, None, actor)
                c["added"] += 1
    for r in gone:
        retire(con, r["id"], "removed", ref)
        log(con, "remove", r["id"], None, actor)
        c["removed"] += 1
    con.execute("DELETE FROM doc_lines WHERE doc=?", (doc,))
    con.executemany(
        "INSERT INTO doc_lines(doc, pos, section, kind, text) VALUES(?,?,?,?,?)",
        [(doc, ln.pos, ln.section, ln.kind, ln.text) for ln in lines if not ln.is_record],
    )
    digest = sha256(data)
    set_meta(con, "records_import_sha", digest)
    set_meta(con, "records_import_ts", now)
    changed = c["moved"] + c["edited"] + c["added"] + c["restored"] + c["removed"]
    if changed or not old:
        log(con, "import", None, None, actor, " ".join(f"{k}={v}" for k, v in c.items()))
    c.update(layout=sum(1 for ln in lines if not ln.is_record), changed=changed, sha=digest)
    return c


# ---------------------------------------------------------------- the write cycle


def read_file(p: Paths) -> bytes:
    try:
        return p.facts.read_bytes()
    except FileNotFoundError as e:
        raise NotFound(f"{p.facts} is missing (`memory render --write` restores it)") from e


def refresh(con: sqlite3.Connection, p: Paths, *, allow_removals: bool = False) -> dict[str, Any] | None:
    """Import the file when its bytes differ from what the rows last matched (cheap no-op otherwise)."""
    if sha256(read_file(p)) in (meta(con, "records_import_sha"), meta(con, "records_render_sha")) and active(con):
        return None
    with locks.hold(p.lock, wait=30, dir_mode=0o700) as fd:
        if fd is None:
            raise Busy("another memory write holds the lock")
        with tx(con):
            return import_doc(con, read_file(p), allow_removals=allow_removals)


def secret_check(text: str) -> None:
    """Fail closed: a tool write goes in only when secretscan says the text is clean."""
    binp = os.environ.get("MEMORY_SECRETSCAN_BIN") or str(paths.bin_dir("secretscan"))
    try:
        r = subprocess.run([binp, "--stdin", "-q"], input=text.encode("utf-8"), capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Refused(f"secretscan unavailable ({e.__class__.__name__}): nothing written (fail-closed)") from e
    if r.returncode == 1:
        raise Refused("secretscan: the text holds a secret: nothing written")
    if r.returncode != 0:
        raise Refused(f"secretscan exit {r.returncode}: nothing written (fail-closed)")


def diff_lines(before: bytes, after: bytes) -> list[str]:
    a = before.decode("utf-8").split("\n")
    b = after.decode("utf-8").split("\n")
    return [
        ln
        for ln in difflib.unified_diff(a, b, "before", "after", n=0, lineterm="")
        if not ln.startswith(("---", "+++"))
    ]


def line_no(data: bytes, line: str | None) -> int | None:
    """The 1-based number of the first line of data that reads exactly `line` (None when absent)."""
    if not line:
        return None
    for i, ln in enumerate(data.decode("utf-8").split("\n"), 1):
        if ln == line:
            return i
    return None


def write_cycle(
    con: sqlite3.Connection,
    p: Paths,
    change: Callable[[sqlite3.Connection, date], dict[str, Any]],
    *,
    dry_run: bool = False,
    tries: int = 3,
) -> dict[str, Any]:
    """lock · import hand edits · rows must render the file · change · render · compare-and-swap the file."""
    with locks.hold(p.lock, wait=30, dir_mode=0o700) as fd:
        if fd is None:
            raise Busy("another memory write holds the lock")
        for _ in range(tries):
            data = read_file(p)
            ref = today()
            con.execute("BEGIN IMMEDIATE")
            try:
                imported = None
                if sha256(data) not in (meta(con, "records_import_sha"), meta(con, "records_render_sha")) or not active(
                    con
                ):
                    imported = import_doc(con, data, ref=ref)
                before = render(con)
                if before != data:
                    raise Mismatch(f"the rows do not render {DOC} byte for byte: nothing written (run `memory gate`)")
                res = change(con, ref)
                renumber(con)
                after = render(con)
                res.update(diff=diff_lines(before, after), imported=imported, file=str(p.facts))
                res["file_line"] = line_no(before if res["op"] == "remove" else after, res.get("line"))
                if dry_run:
                    con.execute("ROLLBACK")
                    res["dry_run"] = True
                    return res
                if read_file(p) != data:  # a hand edit landed meanwhile: start over
                    con.execute("ROLLBACK")
                    continue
                jsonstate.write_bytes(p.facts, after)  # first the file, then the commit (a lost commit = re-import)
                digest = sha256(after)
                set_meta(con, "records_render_sha", digest)
                set_meta(con, "records_import_sha", digest)
                con.execute("COMMIT")
            except BaseException:
                if con.in_transaction:
                    con.execute("ROLLBACK")
                raise
            res["dry_run"] = False
            return res
    raise Busy(f"{DOC} kept changing during the write ({tries} tries): nothing written")


# ---------------------------------------------------------------- the writes (run inside write_cycle)


def sections(con: sqlite3.Connection, doc: str = DOC) -> list[str]:
    return [
        r[0] for r in con.execute("SELECT section FROM doc_lines WHERE doc=? AND kind='heading' ORDER BY pos", (doc,))
    ]


def find_section(con: sqlite3.Connection, name: str, doc: str = DOC) -> str:
    secs = sections(con, doc)
    low = name.strip().lower()
    for test in (lambda s: s.lower() == low, lambda s: s.lower().startswith(low), lambda s: low in s.lower()):
        hits = [s for s in secs if test(s)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise Refused(f"section {name!r} is ambiguous: " + " | ".join(hits))
    raise NotFound(f"no section {name!r}; sections: " + " | ".join(secs))


def ledger_section(con: sqlite3.Connection, doc: str = DOC) -> str:
    for r in con.execute("SELECT section, text FROM doc_lines WHERE doc=? AND kind='heading' ORDER BY pos", (doc,)):
        if is_ledger_heading(r[1]):
            return r[0]
    raise NotFound("no ledger section (a '## ' heading with 'ledger' in it)")


def _next_pos(con: sqlite3.Connection, after: float, doc: str = DOC) -> float:
    r = con.execute(
        "SELECT MIN(pos) FROM (SELECT pos FROM records WHERE doc=? AND status='active' AND pos>? "
        "UNION ALL SELECT pos FROM doc_lines WHERE doc=? AND pos>?)",
        (doc, after, doc, after),
    ).fetchone()
    return (after + r[0]) / 2 if r and r[0] is not None else after + STEP


def _end_of_section(con: sqlite3.Connection, section: str, doc: str = DOC) -> float:
    """After the section's last record, or after its heading and intro text when it has none."""
    r = con.execute(
        "SELECT MAX(pos) FROM records WHERE doc=? AND status='active' AND section=?", (doc, section)
    ).fetchone()
    if r and r[0] is not None:
        return r[0]
    lay = con.execute(
        "SELECT pos, kind FROM doc_lines WHERE doc=? AND section=? ORDER BY pos", (doc, section)
    ).fetchall()
    last = lay[0][0]
    for pos, kind in lay[1:]:
        if kind == "blank":
            break
        last = pos
    return last


def check_text(text: str) -> str:
    t = text.strip()
    if not t or "\n" in t or "\r" in t:
        raise Refused("a record is one non-empty line")
    if t.startswith("- "):
        t = t[2:].strip()
    secret_check(t)
    return t


def compose_fact(claim: str, src: str | None, when: date | None) -> str:
    claim = claim.strip()
    if not src:
        return claim
    src = src.strip()
    if when is not None and not DM.search(src):
        src = f"{src}, {dm_text(when)}"
    return f"{claim} — {src}"


DECIDE_EXAMPLE = (
    'e.g. memory decide declined "Ryanair flights" --details "never again"  or one ledger line: '
    'memory decide "21.10 declined: Ryanair flights (never again)"'
)
LEDGER_ARG = re.compile(r"^(?:-\s+)?(?:(\d{1,2}\.\d{1,2}(?:\.\d{4})?)\.?\s+)?([A-Za-z]+):\s*(.*)$")


def parse_ledger_arg(s: str) -> dict[str, Any]:
    """One ledger-shaped string "[- ][DD.MM[.YYYY] ]verb: thing (details)" -> {date, verb, thing, details}; thing and
    details split the way the importer reads a ledger line (decision_fields). Usage error when it is no such line."""
    m = LEDGER_ARG.match(" ".join(s.split()))
    verb = m.group(2).lower() if m else ""
    if not m or verb not in LEDGER_VERBS or not m.group(3).strip():
        raise Usage(
            f'not a ledger line: {s!r}. Give VERB "THING" or one string "[DD.MM ]verb: thing (details)" with verb '
            + "|".join(LEDGER_VERBS)
            + "; "
            + DECIDE_EXAMPLE
        )
    d = decision_fields(f"01.01 {verb}: {m.group(3).strip()}")
    if not d or not d["thing"]:
        raise Usage(f"no thing in {s!r}; " + DECIDE_EXAMPLE)
    return {"date": m.group(1), "verb": verb, "thing": d["thing"], "details": d["details"]}


def compose_decision(verb: str, thing: str, details: str | None, when: date) -> str:
    verb, thing = verb.strip().lower().rstrip(":"), " ".join(thing.split())
    if verb not in LEDGER_VERBS:
        raise Refused(f"verb {verb!r}: one of " + "|".join(LEDGER_VERBS) + "; " + DECIDE_EXAMPLE)
    if THING_SPLIT.search(thing) or not thing:
        raise Refused(
            "the thing is the 2–4 words naming it: no '(', ' — ' or '; ' (details go in --details, or pass the whole "
            "line as one string); " + DECIDE_EXAMPLE
        )
    text = f"{dm_text(when)} {verb}: {thing}" + (f" ({details.strip()})" if details and details.strip() else "")
    d = decision_fields(text)
    if not d or d["thing"] != thing or d["verb"] != verb:
        raise Refused("the line would not parse back as this ledger entry; simplify the thing")
    return text


def op_set(
    section: str,
    text: str,
    *,
    source: str | None = None,
    scope: str = "always",
    after: str | None = None,
) -> Callable[[sqlite3.Connection, date], dict[str, Any]]:
    def change(con: sqlite3.Connection, ref: date) -> dict[str, Any]:
        t = check_text(text)
        if after:
            a = get(con, after)
            if a["status"] != "active":
                raise Refused(f"{a['id']} is {a['status']}")
            sec, anchor = a["section"], a["pos"]
        else:
            sec = find_section(con, section)
            anchor = _end_of_section(con, sec)
        if sec == ledger_section(con):
            raise Refused("the ledger takes decisions: use `memory decide`")
        if con.execute("SELECT 1 FROM records WHERE status='active' AND text=?", (t,)).fetchone():
            raise Refused("that exact line exists already")
        rid = insert(
            con,
            kind="fact",
            section=sec,
            pos=_next_pos(con, anchor),
            text=t,
            origin="tool",
            ref=ref,
            scope=scope,
            source=source,
        )
        log(con, "set", rid, None, "tool")
        return {"op": "set", "id": rid, "section": sec, "line": "- " + t}

    return change


def op_decide(
    verb: str,
    thing: str,
    details: str | None = None,
    *,
    when: str | None = None,
    supersedes: str | None = None,
) -> Callable[[sqlite3.Connection, date], dict[str, Any]]:
    def change(con: sqlite3.Connection, ref: date) -> dict[str, Any]:
        d = parse_dm(when, ref) if when else ref
        t = check_text(compose_decision(verb, thing, details, d))
        sec = ledger_section(con)
        first = con.execute(
            "SELECT MIN(pos) FROM records WHERE doc=? AND status='active' AND kind='decision'", (DOC,)
        ).fetchone()[0]
        if first is not None:
            prev = con.execute(
                "SELECT MAX(pos) FROM (SELECT pos FROM records WHERE doc=? AND status='active' AND pos<? "
                "UNION ALL SELECT pos FROM doc_lines WHERE doc=? AND pos<?)",
                (DOC, first, DOC, first),
            ).fetchone()[0]
            pos = (prev + first) / 2 if prev is not None else first - STEP
        else:
            pos = _next_pos(con, _end_of_section(con, sec))
        old = None
        if supersedes:
            old = get(con, supersedes)
            if old["status"] != "active" or old["kind"] != "decision":
                raise Refused(f"{old['id']} is no active ledger row")
        rid = insert(
            con,
            kind="decision",
            section=sec,
            pos=pos,
            text=t,
            origin="tool",
            ref=ref,
            source="owner",
            supersedes=old["id"] if old else None,
        )
        if old:
            retire(con, old["id"], "superseded", ref, by=rid)
        same = [
            r["id"]
            for r in con.execute(
                "SELECT id, thing FROM records WHERE status='active' AND kind='decision' AND id<>?", (rid,)
            )
            if (r["thing"] or "").lower() == " ".join(thing.split()).lower()
        ]
        log(con, "decide", rid, old["id"] if old else None, "tool")
        return {
            "op": "decide",
            "id": rid,
            "old": old["id"] if old else None,
            "section": sec,
            "line": "- " + t,
            "same_thing": same,
        }

    return change


def op_supersede(
    rid: str,
    text: str | None = None,
    *,
    remove: bool = False,
    source: str | None = None,
) -> Callable[[sqlite3.Connection, date], dict[str, Any]]:
    def change(con: sqlite3.Connection, ref: date) -> dict[str, Any]:
        old = get(con, rid)
        if old["status"] != "active":
            raise Refused(f"{old['id']} is {old['status']} (superseded by {old['superseded_by'] or '-'})")
        if remove:
            retire(con, old["id"], "removed", ref)
            log(con, "remove", old["id"], None, "tool")
            return {"op": "remove", "id": old["id"], "section": old["section"], "line": "- " + old["text"]}
        if not text:
            raise Refused("supersede needs the new text (or --remove)")
        t = check_text(text)
        kind = old["kind"]
        if kind == "decision" and not decision_fields(t):
            raise Refused("a ledger row needs 'DD.MM verb: thing (details)'")
        nid = insert(
            con,
            kind=kind,
            section=old["section"],
            pos=old["pos"],
            text=t,
            origin="tool",
            ref=ref,
            scope=old["scope"],
            source=source,
            supersedes=old["id"],
        )
        retire(con, old["id"], "superseded", ref, by=nid)
        log(con, "supersede", nid, old["id"], "tool")
        return {"op": "supersede", "id": nid, "old": old["id"], "section": old["section"], "line": "- " + t}

    return change


# ---------------------------------------------------------------- reads


def as_dict(r: sqlite3.Row) -> dict[str, Any]:
    return {k: r[k] for k in COLS}


def history(con: sqlite3.Connection, rid: str) -> list[dict[str, Any]]:
    """The supersedes chain through rid, oldest first."""
    r = get(con, rid)
    chain = [r]
    seen = {r["id"]}
    while chain[0]["supersedes"] and chain[0]["supersedes"] not in seen:
        prev = con.execute("SELECT * FROM records WHERE id=?", (chain[0]["supersedes"],)).fetchone()
        if prev is None:
            break
        chain.insert(0, prev)
        seen.add(prev["id"])
    while chain[-1]["superseded_by"] and chain[-1]["superseded_by"] not in seen:
        nxt = con.execute("SELECT * FROM records WHERE id=?", (chain[-1]["superseded_by"],)).fetchone()
        if nxt is None:
            break
        chain.append(nxt)
        seen.add(nxt["id"])
    return [as_dict(x) for x in chain]


def ledger(con: sqlite3.Connection, doc: str = DOC) -> list[dict[str, Any]]:
    """The active ledger rows in file order, with the fields board-lint matches on."""
    out = []
    for r in con.execute(
        "SELECT * FROM records WHERE doc=? AND status='active' AND kind='decision' ORDER BY pos", (doc,)
    ):
        d = decision_fields(r["text"]) or {}
        verb = str(r["verb"]).lower()
        verb = "decided" if verb == "his call" else verb
        out.append(
            {
                "id": r["id"],
                "day": int(d.get("day") or 0),
                "month": int(d.get("month") or 0),
                "verb": verb,
                "thing": r["thing"],
                "details": r["details"],
                "silent": verb in SILENT_VERBS,
                "negative": verb in NEG_VERBS,
                "date": r["verified_at"],
                "text": r["text"],
            }
        )
    return out


def stats(con: sqlite3.Connection, p: Paths | None = None, doc: str = DOC) -> dict[str, Any]:
    by = {
        f"{k}:{s}": n
        for k, s, n in con.execute("SELECT kind, status, COUNT(*) FROM records WHERE doc=? GROUP BY 1, 2", (doc,))
    }
    writes = con.execute(
        "SELECT COUNT(*) FROM record_log WHERE actor='tool' AND op IN ('set','decide','supersede','remove')"
    ).fetchone()[0]
    hand = con.execute("SELECT COUNT(*) FROM record_log WHERE actor='hand' AND op='import'").fetchone()[0]
    out: dict[str, Any] = {
        "facts": by.get("fact:active", 0),
        "decisions": by.get("decision:active", 0),
        "superseded": by.get("fact:superseded", 0) + by.get("decision:superseded", 0),
        "removed": by.get("fact:removed", 0) + by.get("decision:removed", 0),
        "layout_lines": con.execute("SELECT COUNT(*) FROM doc_lines WHERE doc=?", (doc,)).fetchone()[0],
        "last_import": meta(con, "records_import_ts"),
        "tool_writes": writes,
        "hand_edit_imports": hand,
    }
    if p is not None:
        try:
            digest = sha256(p.facts.read_bytes())
            out["file_matches_rows"] = digest in (meta(con, "records_import_sha"), meta(con, "records_render_sha"))
        except OSError:
            out["file_matches_rows"] = False
    return out


# ---------------------------------------------------------------- round-trip check


def gate(data: bytes, con: sqlite3.Connection | None = None) -> dict[str, Any]:
    """The round-trip check on these bytes: (1) into the given store (when there is one: hand edits imported) and
    render; (2) a fresh in-memory store, import, render; (3) re-importing the render changes nothing."""
    res: dict[str, Any] = {"bytes": len(data), "sha": sha256(data), "lines": data.count(b"\n") + 1}
    checks: list[tuple[str, bool, dict[str, Any], bytes]] = []
    if con is not None:
        with tx(con):
            c = import_doc(con, data)
        out = render(con)
        checks.append(("store", out == data, c, out))
    scratch = connect(":memory:")
    with tx(scratch):
        c0 = import_doc(scratch, data)
    out0 = render(scratch)
    checks.append(("fresh", out0 == data, c0, out0))
    with tx(scratch):
        c1 = import_doc(scratch, out0)
    out1 = render(scratch)
    checks.append(("idempotent", c1["changed"] == 0 and out1 == data, c1, out1))
    res["checks"] = {name: ok for name, ok, _c, _o in checks}
    res["counts"] = {name: {k: v for k, v in c.items() if k != "sha"} for name, _ok, c, _o in checks}
    res["identical"] = all(ok for _n, ok, _c, _o in checks)
    for _name, ok, _c, out in checks:
        if not ok:
            a, b = data.split(b"\n"), out.split(b"\n")
            res["first_diff_line"] = next(
                (i + 1 for i, (x, y) in enumerate(zip(a, b, strict=False)) if x != y), min(len(a), len(b)) + 1
            )
            break
    return res
