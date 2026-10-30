"""loop.reflex: the reflex registry v0. A reflex is a trivial, always-obvious decision the brain turned into a rule
for itself: event kind (glob) + conditions on the event payload -> one typed tool call with fixed arguments.

  add(name, kind=, tool=, why=, case=, when=None, args=None)   refused unless: the tool exists in the registry,
        the gateway's Cedar policy lets the reflex principal call it without an approval (`tool policy check
        NAME --as reflex`), the eval case exists in the corpus (`evals list`), and `why` says why it exists
  rm(name, why)        retire it (kept as history; a later add of the same name is version + 1)
  revise(name, why, kind=, when=, tool=, args=, case=)       a new version of an active reflex: the old
        definition goes to `versions` with why it changed; the tool and case checks run again
  history(name)         every version's definition (current one last) and the add/revise/rm log
  review(days)          read-only: per reflex its firings, hit and misfire rates, the linked eval case's last
        result and a keep | revise | retire suggestion with the numbers behind it, plus the E6 done-when counts
  ls() / show(name) / log(name, days)                         the registry with its firing counts, the firing log
  fire(name, outcome, event=, ms=, note=)                     one firing (ok | error | misfire | skipped): whoever
        runs a reflex records it here (v0 runs none by itself); a misfire = the brain overruled it afterwards
  match(kind, payload)  which active reflexes an event would fire (dry run; calls nothing)
  counts(since) / signal()                                    the agenda counter and the daily review's numbers
Store: ~/.local/state/loop/reflex.db (SQLite, 0600). Firing notes hold ids and outcomes, never message text.
The registry starts empty: the brain adds reflexes from its own traces.
"""

from __future__ import annotations

import fnmatch
import json
import re
import sqlite3
from contextlib import closing
from typing import Any

from . import common

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
OUTCOMES = ("ok", "error", "misfire", "skipped")
SCHEMA = """
CREATE TABLE IF NOT EXISTS reflexes (
  name TEXT PRIMARY KEY, version INTEGER NOT NULL DEFAULT 1, kind TEXT NOT NULL, cond TEXT NOT NULL DEFAULT '{}',
  tool TEXT NOT NULL, args TEXT NOT NULL DEFAULT '{}', why TEXT NOT NULL, eval_case TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retired')),
  created REAL NOT NULL, created_by TEXT, retired REAL, retired_why TEXT);
CREATE TABLE IF NOT EXISTS firings (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, name TEXT NOT NULL, version INTEGER, event TEXT,
  outcome TEXT NOT NULL CHECK (outcome IN ('ok', 'error', 'misfire', 'skipped')), ms INTEGER, note TEXT);
CREATE INDEX IF NOT EXISTS firings_ts ON firings (ts);
CREATE TABLE IF NOT EXISTS history (n INTEGER PRIMARY KEY, ts REAL, op TEXT, name TEXT, actor TEXT, why TEXT);
CREATE TABLE IF NOT EXISTS versions (
  name TEXT NOT NULL, version INTEGER NOT NULL, kind TEXT NOT NULL, cond TEXT NOT NULL, tool TEXT NOT NULL,
  args TEXT NOT NULL, why TEXT NOT NULL, eval_case TEXT NOT NULL, created REAL, created_by TEXT,
  ended REAL NOT NULL, end_op TEXT NOT NULL, end_why TEXT, PRIMARY KEY (name, version));
"""
# review thresholds: enough data = MIN_FIRINGS in the window; a misfire = the brain overruled the reflex
MIN_FIRINGS = 5
REVISE_MISFIRE = 0.15
RETIRE_BAD = 0.5
IDLE_DAYS = 30


class ReflexError(RuntimeError):
    pass


def connect() -> sqlite3.Connection:
    p = common.reflex_db()
    p.parent.mkdir(mode=common.STATE_MODE, parents=True, exist_ok=True)
    new = not p.exists()
    con = sqlite3.connect(p, timeout=5, isolation_level=None)
    con.row_factory = sqlite3.Row
    if new:
        p.chmod(common.FILE_MODE)
    con.executescript(SCHEMA)
    return con


def _obj(raw: str | dict[str, Any] | None, what: str) -> dict[str, Any]:
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        v = json.loads(raw)
    except ValueError as e:
        raise ReflexError(f"{what}: not JSON ({e})") from e
    if not isinstance(v, dict):
        raise ReflexError(f"{what}: must be a JSON object")
    return v


def check_tool(tool: str) -> dict[str, Any]:
    rc, _out, err = common.run([common.bin_path("tool"), "show", tool, "--json"], timeout=30)
    if rc != 0:
        raise ReflexError(
            f"unknown tool {tool} (tool show: {(err.strip().splitlines() or ['exit ' + str(rc)])[-1][:120]})"
        )
    d, err2 = common.run_json(
        [common.bin_path("tool"), "policy", "check", tool, "--as", "reflex", "--json"], timeout=30
    )
    if not isinstance(d, dict):
        raise ReflexError(f"policy check failed: {err2}")
    if not d.get("allowed") or d.get("approval_needed"):
        why = d.get("reason") or ("needs his approval" if d.get("approval_needed") else "denied")
        raise ReflexError(f"the gateway policy does not let a reflex call {tool}: {why}")
    return d


def check_case(case: str) -> None:
    d, err = common.run_json([common.bin_path("evals"), "list", "--json"], timeout=60)
    if not isinstance(d, list):
        raise ReflexError(f"cannot list the eval corpus: {err}")
    if case not in {str(x.get("id")) for x in d if isinstance(x, dict)}:
        raise ReflexError(f"no eval case {case} in the corpus (write the case first; a reflex needs one)")


def add(
    name: str, *, kind: str, tool: str, why: str, case: str, when: Any = None, args: Any = None, actor: str = "brain"
) -> dict[str, Any]:
    if not NAME_RE.match(name or ""):
        raise ReflexError("name: 2-41 chars of a-z, 0-9 and '-'")
    if len((why or "").strip()) < 10:
        raise ReflexError("--why: say why this decision is always the same (one sentence)")
    if not (kind or "").strip():
        raise ReflexError("--kind: the event kind (glob) it reacts to")
    cond, targs = _obj(when, "--when"), _obj(args, "--args")
    check_tool(tool)
    check_case(case)
    now = common.now()
    with closing(connect()) as con:
        cur = con.execute("SELECT status, version FROM reflexes WHERE name = ?", (name,)).fetchone()
        if cur and cur["status"] == "active":
            raise ReflexError(f"{name} exists (reflex rm {name} --why ... first, or pick another name)")
        ver = int(cur["version"]) + 1 if cur else 1
        con.execute(
            "INSERT OR REPLACE INTO reflexes (name, version, kind, cond, tool, args, why, eval_case, status, created,"
            " created_by, retired, retired_why) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, NULL, NULL)",
            (
                name,
                ver,
                kind.strip(),
                json.dumps(cond, sort_keys=True),
                tool,
                json.dumps(targs, sort_keys=True),
                why.strip()[:300],
                case,
                now,
                actor,
            ),
        )
        con.execute(
            "INSERT INTO history (ts, op, name, actor, why) VALUES (?, 'add', ?, ?, ?)",
            (now, name, actor, why.strip()[:300]),
        )
        return show(name, con=con)


def rm(name: str, why: str, actor: str = "brain") -> dict[str, Any]:
    if len((why or "").strip()) < 3:
        raise ReflexError("--why: why it goes")
    now = common.now()
    with closing(connect()) as con:
        _snapshot(con, name, now, "rm", why)
        n = con.execute(
            "UPDATE reflexes SET status = 'retired', retired = ?, retired_why = ? WHERE name = ? AND status = 'active'",
            (now, why.strip()[:300], name),
        ).rowcount
        if not n:
            raise ReflexError(f"no active reflex {name}")
        con.execute(
            "INSERT INTO history (ts, op, name, actor, why) VALUES (?, 'rm', ?, ?, ?)",
            (now, name, actor, why.strip()[:300]),
        )
        return show(name, con=con)


def _snapshot(con: sqlite3.Connection, name: str, now: float, op: str, why: str) -> None:
    """The active definition of `name` into `versions` (it ends now, by op) before a revise or rm changes it."""
    con.execute(
        "INSERT OR REPLACE INTO versions (name, version, kind, cond, tool, args, why, eval_case, created, created_by,"
        " ended, end_op, end_why) SELECT name, version, kind, cond, tool, args, why, eval_case, created, created_by,"
        " ?, ?, ? FROM reflexes WHERE name = ? AND status = 'active'",
        (now, op, why.strip()[:300], name),
    )


def revise(
    name: str,
    why: str,
    *,
    kind: str | None = None,
    when: Any = None,
    tool: str | None = None,
    args: Any = None,
    case: str | None = None,
    actor: str = "brain",
) -> dict[str, Any]:
    """A new version of an active reflex; unchanged fields carry over. `why` says what its data showed."""
    if len((why or "").strip()) < 10:
        raise ReflexError("--why: what its data showed (misfires, eval result; one sentence)")
    with closing(connect()) as con:
        cur = con.execute("SELECT * FROM reflexes WHERE name = ? AND status = 'active'", (name,)).fetchone()
        if cur is None:
            raise ReflexError(f"no active reflex {name}")
        new = {
            "kind": (kind or "").strip() or cur["kind"],
            "cond": json.dumps(_obj(when, "--when"), sort_keys=True) if when is not None else cur["cond"],
            "tool": tool or cur["tool"],
            "args": json.dumps(_obj(args, "--args"), sort_keys=True) if args is not None else cur["args"],
            "eval_case": case or cur["eval_case"],
        }
        if all(new[k] == cur[k] for k in new):
            raise ReflexError("nothing changes: give --kind, --when, --tool, --args or --case")
        check_tool(new["tool"])
        check_case(new["eval_case"])
        now = common.now()
        _snapshot(con, name, now, "revise", why)
        con.execute(
            "UPDATE reflexes SET version = version + 1, kind = ?, cond = ?, tool = ?, args = ?, eval_case = ?,"
            " created = ?, created_by = ? WHERE name = ?",
            (new["kind"], new["cond"], new["tool"], new["args"], new["eval_case"], now, actor, name),
        )
        con.execute(
            "INSERT INTO history (ts, op, name, actor, why) VALUES (?, 'revise', ?, ?, ?)",
            (now, name, actor, why.strip()[:300]),
        )
        return show(name, con=con)


def history(name: str) -> dict[str, Any]:
    """Every version of `name` (ended ones from `versions`, then the current row) and its add/revise/rm log."""
    with closing(connect()) as con:
        cur = con.execute("SELECT * FROM reflexes WHERE name = ?", (name,)).fetchone()
        old = con.execute("SELECT * FROM versions WHERE name = ? ORDER BY version", (name,)).fetchall()
        if cur is None and not old:
            raise ReflexError(f"no reflex {name}")

        def vrow(r: sqlite3.Row, ended: bool) -> dict[str, Any]:
            d = {
                "version": r["version"],
                "kind": r["kind"],
                "when": json.loads(r["cond"] or "{}"),
                "tool": r["tool"],
                "args": json.loads(r["args"] or "{}"),
                "eval_case": r["eval_case"],
                "created": common.iso(r["created"]) if r["created"] else None,
            }
            if ended:
                d.update(ended=common.iso(r["ended"]), end_op=r["end_op"], end_why=r["end_why"])
            return d

        vers = [vrow(r, True) for r in old]
        if cur is not None and (cur["status"] == "active" or not any(v["version"] == cur["version"] for v in vers)):
            vers.append(vrow(cur, False) | {"status": cur["status"]})
        log_ = [
            {"ts": common.iso(r["ts"]), "op": r["op"], "actor": r["actor"], "why": r["why"]}
            for r in con.execute("SELECT * FROM history WHERE name = ? ORDER BY n", (name,))
        ]
        return {"name": name, "versions": vers, "log": log_}


def _eval_results(cases: set[str]) -> dict[str, dict[str, Any]]:
    """The latest recorded evals result per case id: {case: {run, ts, score, passed}} from evals' history.jsonl and
    the run's result.json (passed as evals judged it). Missing state = {} (no-data, never a guess)."""
    d = common.evals_state()
    out: dict[str, dict[str, Any]] = {}
    try:
        lines = (d / "history.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in reversed(lines):
        if not cases - out.keys():
            break
        try:
            h = json.loads(line)
        except ValueError:
            continue
        hit = [c for c in cases - out.keys() if c in (h.get("scores") or {})]
        if not hit:
            continue
        passed: dict[str, Any] = {}
        try:
            run = json.loads((d / "runs" / str(h.get("run_id")) / "result.json").read_text(encoding="utf-8"))
            passed = {str(c.get("id")): c.get("passed") for c in run.get("cases") or [] if isinstance(c, dict)}
        except (OSError, ValueError):
            pass
        for c in hit:
            out[c] = {
                "run": h.get("run_id"),
                "ts": common.iso(h["ts"]) if isinstance(h.get("ts"), (int, float)) else None,
                "score": h["scores"][c],
                "passed": passed.get(c),
            }
    return out


def _suggest(n: int, ok: int, mis: int, err: int, ev: dict[str, Any] | None, age_d: float, idle_d: float | None):
    """keep | revise | retire with the reason in numbers. The thresholds are module constants."""
    bad = (mis + err) / n if n else 0.0
    ev_fail = bool(ev) and ev.get("passed") is False
    if n >= MIN_FIRINGS and bad >= RETIRE_BAD:
        return "retire", f"{mis + err}/{n} firings misfired or failed ({bad:.0%} >= {RETIRE_BAD:.0%})"
    if ev_fail and n >= MIN_FIRINGS and mis / n >= REVISE_MISFIRE:
        return "retire", f"eval case failing and {mis}/{n} misfires"
    if n == 0 and age_d >= IDLE_DAYS:
        return "retire", f"no firing in {age_d:.0f} d (idle; its event no longer comes)"
    if idle_d is not None and idle_d >= IDLE_DAYS:
        return "retire", f"last firing {idle_d:.0f} d ago (idle)"
    if ev_fail:
        e = ev or {}
        return "revise", f"eval case run {e.get('run')} failed (score {e.get('score')}): rule or case drifted"
    if n >= MIN_FIRINGS and mis / n >= REVISE_MISFIRE:
        return "revise", f"{mis}/{n} misfires ({mis / n:.0%} >= {REVISE_MISFIRE:.0%}): tighten --when"
    if err >= 2:
        return "revise", f"{err} errors in the window: check the tool call"
    if n < MIN_FIRINGS:
        return "keep", f"too little data ({n} firings < {MIN_FIRINGS})"
    return "keep", f"{ok}/{n} ok, {mis} misfires"


def review(days: float = 30, include_retired: bool = False) -> dict[str, Any]:
    """Read-only: each reflex with its numbers and a suggestion, plus the E6 done-when counts (every reflex has an
    eval case + firings; >= 1 revised or retired from its own data). Writes nothing (no store = all zeros)."""
    empty = {
        "window_days": days,
        "reflexes": [],
        "e6": {"reflexes": 0, "with_eval_case": 0, "with_firings": 0, "revised": 0, "retired": 0, "done": False},
    }
    if not common.reflex_db().exists():
        return empty
    now = common.now()
    lo = now - days * 86400
    with closing(connect()) as con:
        rows = con.execute(
            "SELECT * FROM reflexes" + ("" if include_retired else " WHERE status = 'active'") + " ORDER BY name"
        ).fetchall()
        allrows = con.execute("SELECT name, eval_case FROM reflexes").fetchall()
        ops = {
            op: int(n)
            for op, n in con.execute("SELECT op, COUNT(*) FROM history WHERE op IN ('revise', 'rm') GROUP BY op")
        }
        fired_names = {r[0] for r in con.execute("SELECT DISTINCT name FROM firings")}
        evs = _eval_results({r["eval_case"] for r in rows})
        out = []
        for r in rows:
            by: dict[str, dict[str, int]] = {}
            for v, outcome, c in con.execute(
                "SELECT version, outcome, COUNT(*) FROM firings WHERE name = ? AND ts >= ? GROUP BY version, outcome",
                (r["name"], lo),
            ):
                by.setdefault(f"v{v}", {})[outcome] = int(c)
            curv = by.get(f"v{r['version']}", {})
            ok, mis, err, skip = (curv.get(k, 0) for k in ("ok", "misfire", "error", "skipped"))
            n = ok + mis + err
            last = con.execute("SELECT MAX(ts) FROM firings WHERE name = ?", (r["name"],)).fetchone()[0]
            age_d = (now - float(r["created"])) / 86400
            idle_d = (now - float(last)) / 86400 if last else None
            ev = evs.get(r["eval_case"])
            sug, why = _suggest(n, ok, mis, err, ev, age_d, idle_d)
            out.append(
                {
                    "name": r["name"],
                    "version": r["version"],
                    "status": r["status"],
                    "eval_case": r["eval_case"],
                    "eval": ev,
                    "age_days": round(age_d, 1),
                    "n": n,
                    "ok": ok,
                    "misfire": mis,
                    "error": err,
                    "skipped": skip,
                    "hit_rate": round(ok / n, 3) if n else None,
                    "misfire_rate": round(mis / n, 3) if n else None,
                    "by_version": by,
                    "last_fired": common.iso(last) if last else None,
                    "suggestion": sug if r["status"] == "active" else "retired",
                    "reason": why if r["status"] == "active" else (r["retired_why"] or ""),
                }
            )
        e6 = {
            "reflexes": len(allrows),
            "with_eval_case": sum(1 for x in allrows if x["eval_case"]),
            "with_firings": sum(1 for x in allrows if x["name"] in fired_names),
            "revised": ops.get("revise", 0),
            "retired": ops.get("rm", 0),
        }
        e6["done"] = bool(
            e6["reflexes"]
            and e6["with_eval_case"] == e6["reflexes"]
            and e6["with_firings"] == e6["reflexes"]
            and e6["revised"] + e6["retired"] >= 1
        )
        return {"window_days": days, "reflexes": out, "e6": e6}


def _row(r: sqlite3.Row, con: sqlite3.Connection, now: float) -> dict[str, Any]:
    def cnt(since: float, outcome: str | None = None) -> int:
        q = "SELECT COUNT(*) FROM firings WHERE name = ? AND ts >= ?" + (" AND outcome = ?" if outcome else "")
        return int(con.execute(q, (r["name"], since, *([outcome] if outcome else []))).fetchone()[0])

    last = con.execute("SELECT MAX(ts) FROM firings WHERE name = ?", (r["name"],)).fetchone()[0]
    return {
        "name": r["name"],
        "version": r["version"],
        "status": r["status"],
        "kind": r["kind"],
        "when": json.loads(r["cond"] or "{}"),
        "tool": r["tool"],
        "args": json.loads(r["args"] or "{}"),
        "why": r["why"],
        "eval_case": r["eval_case"],
        "created": common.iso(r["created"]),
        "created_by": r["created_by"],
        "retired": common.iso(r["retired"]) if r["retired"] else None,
        "retired_why": r["retired_why"],
        "fired_24h": cnt(now - 86400),
        "fired_7d": cnt(now - 7 * 86400),
        "fired_total": cnt(0),
        "misfires_7d": cnt(now - 7 * 86400, "misfire"),
        "errors_7d": cnt(now - 7 * 86400, "error"),
        "last_fired": common.iso(last) if last else None,
    }


def show(name: str, con: sqlite3.Connection | None = None) -> dict[str, Any]:
    own = con is None
    c = con or connect()
    try:
        r = c.execute("SELECT * FROM reflexes WHERE name = ?", (name,)).fetchone()
        if r is None:
            raise ReflexError(f"no reflex {name}")
        return _row(r, c, common.now())
    finally:
        if own:
            c.close()


def ls(include_retired: bool = False) -> list[dict[str, Any]]:
    with closing(connect()) as con:
        q = "SELECT * FROM reflexes" + ("" if include_retired else " WHERE status = 'active'") + " ORDER BY name"
        now = common.now()
        return [_row(r, con, now) for r in con.execute(q).fetchall()]


def fire(
    name: str, outcome: str, *, event: str | None = None, ms: int | None = None, note: str | None = None
) -> dict[str, Any]:
    if outcome not in OUTCOMES:
        raise ReflexError(f"--outcome: one of {', '.join(OUTCOMES)}")
    with closing(connect()) as con:
        r = con.execute("SELECT status, version FROM reflexes WHERE name = ?", (name,)).fetchone()
        if r is None or r["status"] != "active":
            raise ReflexError(f"no active reflex {name}")
        cur = con.execute(
            "INSERT INTO firings (ts, name, version, event, outcome, ms, note) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                common.now(),
                name,
                r["version"],
                (event or None) and str(event)[:64],
                outcome,
                ms,
                (note or None) and " ".join(note.split())[:160],
            ),
        )
        return {"id": cur.lastrowid, "name": name, "outcome": outcome}


def log(name: str | None = None, days: float = 7) -> list[dict[str, Any]]:
    with closing(connect()) as con:
        since = common.now() - days * 86400
        q = "SELECT * FROM firings WHERE ts >= ?" + (" AND name = ?" if name else "") + " ORDER BY ts DESC LIMIT 500"
        rows = con.execute(q, (since, *([name] if name else []))).fetchall()
        return [
            dict(
                id=r["id"],
                ts=common.iso(r["ts"]),
                name=r["name"],
                version=r["version"],
                event=r["event"],
                outcome=r["outcome"],
                ms=r["ms"],
                note=r["note"],
            )
            for r in rows
        ]


def _cond_ok(cond: dict[str, Any], payload: dict[str, Any]) -> bool:
    for k, want in cond.items():
        cur: Any = payload
        for part in str(k).split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        if isinstance(want, str) and any(ch in want for ch in "*?["):
            if not isinstance(cur, (str, int, float)) or not fnmatch.fnmatchcase(str(cur), want):
                return False
        elif cur != want:
            return False
    return True


def match(kind: str, payload: Any = None) -> list[dict[str, Any]]:
    pl = _obj(payload, "--payload")
    with closing(connect()) as con:
        out = []
        for r in con.execute("SELECT * FROM reflexes WHERE status = 'active' ORDER BY name"):
            if fnmatch.fnmatchcase(kind, r["kind"]) and _cond_ok(json.loads(r["cond"] or "{}"), pl):
                out.append(
                    {
                        "name": r["name"],
                        "tool": r["tool"],
                        "args": json.loads(r["args"] or "{}"),
                        "eval_case": r["eval_case"],
                    }
                )
        return out


def counts(since: float | None) -> dict[str, Any]:
    """The agenda counter: active reflexes, firings since `since` per reflex, misfires among them."""
    if not common.reflex_db().exists():
        return {"active": 0, "fired": 0, "by_name": {}, "misfires": 0}
    with closing(connect()) as con:
        lo = float(since or common.now() - 86400)
        by = {r[0]: r[1] for r in con.execute("SELECT name, COUNT(*) FROM firings WHERE ts > ? GROUP BY name", (lo,))}
        mis = con.execute("SELECT COUNT(*) FROM firings WHERE ts > ? AND outcome = 'misfire'", (lo,)).fetchone()[0]
        act = con.execute("SELECT COUNT(*) FROM reflexes WHERE status = 'active'").fetchone()[0]
        return {"active": int(act), "fired": sum(by.values()), "by_name": by, "misfires": int(mis)}


def signal() -> dict[str, Any]:
    if not common.reflex_db().exists():
        return {"active": 0, "fired_24h": 0, "misfires_7d": 0, "misfires_by_name": {}}
    now = common.now()
    with closing(connect()) as con:
        act = con.execute("SELECT COUNT(*) FROM reflexes WHERE status = 'active'").fetchone()[0]
        f24 = con.execute("SELECT COUNT(*) FROM firings WHERE ts > ?", (now - 86400,)).fetchone()[0]
        mis = {
            r[0]: r[1]
            for r in con.execute(
                "SELECT name, COUNT(*) FROM firings WHERE ts > ? AND outcome IN ('misfire', 'error') GROUP BY name",
                (now - 7 * 86400,),
            )
        }
        return {"active": int(act), "fired_24h": int(f24), "misfires_7d": sum(mis.values()), "misfires_by_name": mis}
