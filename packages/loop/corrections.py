"""loop.corrections: an owner correction (memory2's extraction) -> a fact-row draft + an eval-case draft, both
waiting for the brain (accept or reject) within 24 h of the correction.

  scan(since=None, write=True) -> new drafts   (cron every 2 h after `memory2 extract`; ONE
        ~/inbox/loop-corrections-DATE.md per day while drafts wait, rewritten when new ones come)
  accept(ID, msg=, judge=, reply_re=, text=, section=, area=, no_fact=, no_case=)
        the fact row through `memory set|supersede|decide` (exit code checked; skipped when a row already covers
        it), the case appended to <corpus>/cases/60-corrections.yaml (checked with `evals list`, committed in the
        corpus repo), memory2's review item resolved
  reject(ID, why) / show(ID) / listing() / signal()
Source: memory2 state (candidates.jsonl: kind, subject, normalized text, cues, ts; mem.db read-only: review links,
facts, records). Never read: the candidates' raw message or its context. A correction = kind "correction", or a
strong correction cue on another kind.
Fact draft: a related active row (memory2's conflict links, else shared subject words) dated on or after the
correction day -> "covered" (the brain already wrote it: only the case is new); an older related row ->
supersede it (a ledger row -> a new decision superseding it); none -> a new fact in the guessed section.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any

from core import locks, notes, paths

from . import common

STATE = "corrections.json"
FIRST_WINDOW_H = 48
DUE_H = 24
CASE_FILE = "60-corrections.yaml"
CASE_HEADER = (
    "# loop: eval cases drafted from owner corrections (`loop accept`). Tags: facts + correction. Each case\n"
    "# carries its source as a comment (loop draft id, memory2 candidate, fact row). Private corpus: never copy\n"
    "# into a synced path.\ncases:\n"
)
STOP = set(
    "the and for with his her not now that this from into only owner uses use has have are was will should must "
    "never always about is to of in on at it no an or by be we he me my so up do as if am pm".split()
)
AREAS = [
    ("reminders", r"remind"),
    ("calendar", r"calendar|trip|flight|train|travel|event|wedding|concert|rave|booking|hotel"),
    ("media", r"emby|movie|film|media|radarr|sonarr|seerr|series|subtitle"),
    ("home", r"light|lamp|vacuum|deebot|cat|heating|home assistant|\bha\b|tv|wled|zigbee"),
    ("server", r"server|unraid|disk|parity|container|docker|nvme|gpu|cpu"),
    ("shopping", r"shop|amazon|galaxus|brack|order|basket"),
    ("vault", r"vault|mail|gmail|paperless|bill|chat|telegram archive"),
    ("voice", r"voice|call|phone|app\b|android"),
    ("approvals", r"approval|card|payment"),
]
SECTION_BY_AREA = {
    "home": "Home & HA",
    "server": "Server & hardware",
    "voice": "Phone app & voice",
    "calendar": "Travel",
    "approvals": "Accounts & money plumbing (no amounts)",
}
KEEP = ("id", "kind", "subject", "text", "cues", "strong", "conf", "ts", "status")


class CorrectionError(RuntimeError):
    pass


# ------------------------------------------------------------------------------------------------ sources
def _cand_ts(c: dict[str, Any]) -> float | None:
    try:
        return datetime.strptime(str(c.get("ts"))[:16], "%Y-%m-%d %H:%M").replace(tzinfo=common.tz()).timestamp()
    except ValueError:
        return None


def candidates() -> list[dict[str, Any]]:
    """memory2's staged candidates, only the fields named in KEEP (never raw text or context)."""
    p = common.memory2_state() / "candidates.jsonl"
    out = []
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for ln in lines:
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("id"):
            out.append({k: d.get(k) for k in KEEP})
    return out


def is_correction(c: dict[str, Any]) -> bool:
    if c.get("status") in ("not-durable",):
        return False
    return c.get("kind") == "correction" or ("correction" in (c.get("cues") or []) and bool(c.get("strong")))


def _db() -> sqlite3.Connection | None:
    p = common.memory2_state() / "mem.db"
    if not p.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        con.execute("SELECT 1 FROM records LIMIT 1")
        return con
    except sqlite3.Error:
        return None


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if len(w) >= 2 and w not in STOP and not w.isdigit()}


def _row_date(r: sqlite3.Row, ref_year: int) -> date | None:
    vf = r["valid_from"]
    if vf:
        try:
            return date.fromisoformat(str(vf)[:10])
        except ValueError:
            pass
    m = re.match(r"(\d{1,2})\.(\d{1,2})", str(r["day"] or ""))
    if m:
        try:
            return date(ref_year, int(m[2]), int(m[1]))
        except ValueError:
            return None
    return None


def _review(con: sqlite3.Connection, cand_id: str) -> list[sqlite3.Row]:
    try:
        return con.execute(
            "SELECT id, kind, fact_id, other_id, status FROM review WHERE cand_id = ? ORDER BY created", (cand_id,)
        ).fetchall()
    except sqlite3.Error:
        return []


def _linked_rows(con: sqlite3.Connection, revs: list[sqlite3.Row]) -> list[sqlite3.Row]:
    out = []
    for rv in revs:
        for fid in (rv["other_id"], rv["fact_id"]):
            if not fid:
                continue
            f = con.execute("SELECT text FROM facts WHERE id = ?", (fid,)).fetchone()
            if not f or not f["text"]:
                continue
            r = con.execute(
                "SELECT * FROM records WHERE status = 'active' AND (text = ? OR text = ?)",
                (f["text"], "- " + f["text"]),
            ).fetchone()
            if r and all(r["id"] != x["id"] for x in out):
                out.append(r)
    return out


def related(con: sqlite3.Connection, subject: str, revs: list[sqlite3.Row]) -> list[tuple[sqlite3.Row, int]]:
    """(row, shared subject words), best first: memory2's conflict links, then active rows sharing subject words."""
    sw = _words(subject)
    need = 1 if len(sw) <= 1 else 2
    scored: dict[str, tuple[sqlite3.Row, int]] = {}
    for r in _linked_rows(con, revs):
        scored[r["id"]] = (r, max(need, len(sw & _words(r["text"]))))
    if sw:
        for r in con.execute("SELECT * FROM records WHERE status = 'active'"):
            n = len(sw & _words(f"{r['text']} {r['thing'] or ''}"))
            if n >= need and r["id"] not in scored:
                scored[r["id"]] = (r, n)
    return sorted(scored.values(), key=lambda x: (-x[1], str(x[0]["created"] or "")))[:3]


# ------------------------------------------------------------------------------------------------ drafts
def guess_area(s: str) -> str:
    low = (s or "").lower()
    for area, rx in AREAS:
        if re.search(rx, low):
            return area
    return "memory"


def _sections(con: sqlite3.Connection | None) -> set[str]:
    if con is None:
        return set()
    try:
        return {r[0] for r in con.execute("SELECT DISTINCT section FROM records WHERE status = 'active'")}
    except sqlite3.Error:
        return set()


def draft(c: dict[str, Any], con: sqlite3.Connection | None) -> dict[str, Any]:
    subject = str(c.get("subject") or "").strip() or "a correction"
    text = " ".join(str(c.get("text") or "").split()).rstrip(".")
    ts = _cand_ts(c) or common.now()
    cday = datetime.fromtimestamp(ts, common.tz()).date()
    dm = f"{cday.day:02d}.{cday.month:02d}"
    revs = _review(con, str(c["id"])) if con is not None else []
    rel = related(con, subject, revs) if con is not None else []
    area = guess_area(f"{subject} {text}")
    fact: dict[str, Any]
    covered = next((r for r, _n in rel if (_row_date(r, cday.year) or date.min) >= cday), None)
    if covered is not None:
        fact = {
            "op": "covered",
            "row": covered["id"],
            "row_kind": covered["kind"],
            "row_text": covered["text"],
            "argv": None,
        }
    elif rel:
        r = rel[0][0]
        if r["kind"] == "decision":
            argv = [
                "memory",
                "decide",
                str(r["verb"] or "decided"),
                str(r["thing"] or subject),
                "--details",
                text,
                "--date",
                dm,
                "--supersedes",
                r["id"],
            ]
            fact = {
                "op": "decide",
                "row": r["id"],
                "row_kind": "decision",
                "row_text": r["text"],
                "argv": argv,
                "check": "the verb: declined | skipped | scratched | removed | off | done | decided | kept",
            }
        else:
            argv = ["memory", "supersede", r["id"], text, "--src", "owner", "--date", dm, "--source", "owner"]
            fact = {"op": "supersede", "row": r["id"], "row_kind": "fact", "row_text": r["text"], "argv": argv}
    else:
        secs = _sections(con)
        sec = SECTION_BY_AREA.get(area, "Apps & services")
        sec = sec if not secs or sec in secs else "Apps & services"
        argv = ["memory", "set", "--section", sec, text, "--src", "owner", "--date", dm, "--source", "owner"]
        fact = {"op": "set", "section": sec, "argv": argv}
    fact["related"] = [r["id"] for r, _n in rel]
    old = fact.get("row_text") if fact["op"] in ("supersede", "decide") else None
    if fact["op"] == "covered" and fact.get("row_kind") == "decision":
        judge = (
            f"Stays consistent with the settled decision on file ({_short(fact['row_text'], 160)}): does not"
            f" pitch, ask about or contradict it again. Short."
        )
    else:
        judge = (
            f"Uses the corrected fact: {_short(text, 220)}."
            + (f" Does not repeat the earlier claim ({_short(old, 140)})." if old else "")
            + " Short."
        )
    case = {
        "id": f"corr-{common.slug(subject, 36)}",
        "area": area,
        "tags": ["facts", "correction"],
        "msg": f"quick check: what do you have on file about {subject}, and is it current?",
        "expect": [{"judge": judge}],
    }
    did = common.short_id("c", str(c["id"]))
    return {
        "id": did,
        "cand": c["id"],
        "kind": c.get("kind"),
        "subject": subject,
        "text": text,
        "correction_ts": ts,
        "day": cday.isoformat(),
        "due_ts": ts + DUE_H * 3600,
        "review_ids": [rv["id"] for rv in revs if rv["status"] == "open"],
        "fact": fact,
        "case": case,
        "status": "draft",
        "created_ts": common.now(),
    }


def _short(s: str | None, n: int) -> str:
    s = " ".join(str(s or "").split())
    s = re.sub(r"^- ", "", s)
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


# ------------------------------------------------------------------------------------------------ state
def _load() -> dict[str, Any]:
    st = common.load(STATE, {})
    if not isinstance(st, dict) or not isinstance(st.get("drafts"), dict):
        st = {"drafts": {}, "since": None}
    return st


def _find(st: dict[str, Any], did: str) -> dict[str, Any]:
    d = st["drafts"].get(did)
    if d is None:
        hits = [v for k, v in st["drafts"].items() if k.startswith(did) or v.get("cand") == did]
        if len(hits) == 1:
            return hits[0]
        raise CorrectionError(f"no correction draft {did}")
    return d


def scan(*, since: float | None = None, write: bool = True) -> list[dict[str, Any]]:
    with locks.hold(common.lock_path().with_name(".corrections.lock"), wait=30, dir_mode=common.STATE_MODE) as fd:
        if fd is None:
            raise CorrectionError("another corrections scan holds the lock")
        st = _load()
        now = common.now()
        lo = since if since is not None else float(st.get("since") or (now - FIRST_WINDOW_H * 3600))
        known = {d["cand"] for d in st["drafts"].values()}
        con = _db()
        new = []
        try:
            for c in candidates():
                ts = _cand_ts(c)
                if ts is None or ts < lo or c["id"] in known or not is_correction(c):
                    continue
                d = draft(c, con)
                st["drafts"][d["id"]] = d
                new.append(d)
        finally:
            if con is not None:
                con.close()
        if write:
            if since is None:
                st["since"] = now - 6 * 3600  # re-read the last hours: extract may stage a turn late
            common.save(STATE, st)
            if new:
                _publish_note(st)
        return new


def _publish_note(st: dict[str, Any]) -> Path:
    waiting = sorted((d for d in st["drafts"].values() if d["status"] == "draft"), key=lambda d: d["correction_ts"])
    day = common.today().isoformat()
    lines = []
    for d in waiting:
        f = d["fact"]
        fact = {
            "covered": f"covered by row {f.get('row')}",
            "set": f"new row in {f.get('section')}",
            "supersede": f"supersede row {f.get('row')}",
            "decide": f"new decision superseding {f.get('row')}",
        }
        due = datetime.fromtimestamp(d["due_ts"], common.tz()).strftime("%d.%m %H:%M")
        lines.append(
            f"- {d['id']} · {d['subject']} ({d['kind']}, {d['day'][8:10]}.{d['day'][5:7]}) · fact:"
            f" {fact.get(f['op'], f['op'])} · case: {d['case']['id']} ({d['case']['area']}) · due {due}"
        )
    text = notes.compose(
        f"Loop: {len(waiting)} owner correction(s) -> fact + eval case drafts ({day})",
        [
            "From memory2's extraction; the drafts hold no message text (subject and the normalized fact only).",
            "",
            *lines,
        ],
        "per draft `loop show ID`, then `loop accept ID` (optionally --msg/--judge/--reply-re for the case, --text"
        ' or --section for the row) or `loop reject ID --why "..."`, within 24 h of the correction; then events ack.',
    )
    return notes.write(paths.inbox(), f"loop-corrections-{day}.md", text, mode=common.FILE_MODE)


def listing(include_closed: bool = False) -> list[dict[str, Any]]:
    st = _load()
    rows = [d for d in st["drafts"].values() if include_closed or d["status"] == "draft"]
    return sorted(rows, key=lambda d: d["correction_ts"])


def show(did: str) -> dict[str, Any]:
    return dict(_find(_load(), did))


def signal() -> dict[str, Any]:
    st = _load()
    now = common.now()
    ds = list(st["drafts"].values())
    return {
        "open": sum(1 for d in ds if d["status"] == "draft"),
        "overdue": sum(1 for d in ds if d["status"] == "draft" and now > d["due_ts"]),
        "accepted_7d": sum(1 for d in ds if d["status"] == "accepted" and now - d.get("decided_ts", 0) < 7 * 86400),
        "late_7d": sum(
            1
            for d in ds
            if d["status"] == "accepted"
            and now - d.get("decided_ts", 0) < 7 * 86400
            and d.get("decided_ts", 0) > d["due_ts"]
        ),
    }


# ------------------------------------------------------------------------------------------------ accept / reject
def case_yaml(case: dict[str, Any], comment: str) -> str:
    """One case as a YAML list item (JSON-quoted scalars: valid YAML, no dependency)."""
    q = lambda s: json.dumps(str(s), ensure_ascii=False)  # noqa: E731
    L = [
        f"  # {comment}",
        f"  - id: {case['id']}",
        f"    area: {case['area']}",
        "    tags: [" + ", ".join(case["tags"]) + "]",
        f"    msg: {q(case['msg'])}",
        "    expect:",
    ]
    for e in case["expect"]:
        ((k, v),) = e.items()
        L.append(f"      - {{{k}: {q(v)}}}")
    return "\n".join(L) + "\n"


def _corpus_ids() -> tuple[set[str] | None, str | None]:
    d, err = common.run_json([common.bin_path("evals"), "list", "--json"], timeout=60)
    if not isinstance(d, list):
        return None, err or "evals list: unexpected answer"
    return {str(x.get("id")) for x in d if isinstance(x, dict)}, None


def _add_case(case: dict[str, Any], comment: str) -> tuple[str, Path]:
    cdir = common.corpus_dir() / "cases"
    if not cdir.is_dir():
        raise CorrectionError(f"no eval corpus at {common.corpus_dir()}")
    ids, err = _corpus_ids()
    if ids is None:
        raise CorrectionError(f"cannot list the eval corpus: {err}")
    base, n = case["id"], 2
    while case["id"] in ids:
        case["id"], n = f"{base}-{n}", n + 1
    f = cdir / CASE_FILE
    before = f.read_text(encoding="utf-8") if f.exists() else None
    f.write_text((before if before is not None else CASE_HEADER) + case_yaml(case, comment), encoding="utf-8")
    got, err = _corpus_ids()
    if got is None or case["id"] not in got:
        if before is None:
            f.unlink()
        else:
            f.write_text(before, encoding="utf-8")
        raise CorrectionError(f"the case did not load ({err or 'missing after write'}); file restored")
    root = common.corpus_dir()
    if (root / ".git").exists():
        common.run(["git", "-C", str(root), "add", f"cases/{CASE_FILE}"])
        common.run(
            [
                "git",
                "-C",
                str(root),
                "-c",
                "user.name=loop",
                "-c",
                "user.email=loop@localhost.invalid",
                "commit",
                "-q",
                "-m",
                f"loop: case {case['id']} from an owner correction",
            ]
        )
    return case["id"], f


def accept(
    did: str,
    *,
    msg: str | None = None,
    judge: str | None = None,
    reply_re: str | None = None,
    text: str | None = None,
    section: str | None = None,
    area: str | None = None,
    no_fact: bool = False,
    no_case: bool = False,
) -> dict[str, Any]:
    with locks.hold(common.lock_path().with_name(".corrections.lock"), wait=30, dir_mode=common.STATE_MODE):
        st = _load()
        d = _find(st, did)
        if d["status"] != "draft":
            raise CorrectionError(f"{d['id']} is {d['status']}")
        out: dict[str, Any] = {"id": d["id"]}
        f = d["fact"]
        if f["op"] != "covered" and not no_fact:
            argv = list(f["argv"])
            if text:
                i = {"set": 4, "supersede": 3}.get(f["op"])
                if i is not None:
                    argv[i] = text
                elif f["op"] == "decide":
                    argv[argv.index("--details") + 1] = text
            if section and f["op"] == "set":
                argv[3] = section
            argv[0] = common.bin_path("memory")
            rc, so, se = common.run([*argv, "--json"], timeout=120)
            if rc != 0:
                raise CorrectionError(f"memory {f['op']} exit {rc}: {(se.strip().splitlines() or [''])[-1][:200]}")
            try:
                out["row"] = json.loads(so).get("id")
            except (ValueError, AttributeError):
                out["row"] = None
        else:
            out["row"] = f.get("row")
        if not no_case:
            case = json.loads(json.dumps(d["case"]))
            if msg:
                case["msg"] = msg
            if judge:
                case["expect"] = [e for e in case["expect"] if "judge" not in e] + [{"judge": judge}]
            if reply_re:
                case["expect"] = [{"reply_re": reply_re}, *case["expect"]]
            if area:
                case["area"] = area
            comment = (
                f"loop {d['id']}: memory2 {d['cand']} ({d['kind']} {d['day']}), fact row {out.get('row') or '-'}"
                f", accepted {common.today().isoformat()}"
            )
            out["case"], cf = _add_case(case, comment)
            out["case_file"] = notes.home_rel(str(cf))
        for rid in d.get("review_ids") or []:
            common.run([common.bin_path("memory2"), "accept", rid], timeout=60)
        d.update(status="accepted", decided_ts=common.now(), row=out.get("row"), case_id=out.get("case"))
        common.save(STATE, st)
        out["late"] = d["decided_ts"] > d["due_ts"]
        return out


def reject(did: str, why: str) -> dict[str, Any]:
    if not why.strip():
        raise CorrectionError("reject needs --why")
    with locks.hold(common.lock_path().with_name(".corrections.lock"), wait=30, dir_mode=common.STATE_MODE):
        st = _load()
        d = _find(st, did)
        if d["status"] != "draft":
            raise CorrectionError(f"{d['id']} is {d['status']}")
        for rid in d.get("review_ids") or []:
            common.run([common.bin_path("memory2"), "reject", rid], timeout=60)
        d.update(status="rejected", decided_ts=common.now(), why=why.strip()[:300])
        common.save(STATE, st)
        return {"id": d["id"], "status": "rejected"}
