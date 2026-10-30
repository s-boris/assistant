"""wake: the brain's own wake rules. Data the brain writes, read by the heartbeat gate and the agenda;
no code here decides what an event means.

  wake next <time|+dur> "why"      hold idle-only beats until then; inbox events, Due items, finished agents, the
                                   first beat after a restart, timers and the 3 h safety valve still wake. One hold at a
                                   time (a new one replaces it); at most 24 h ahead.
  wake on <kind-glob> now|beat|digest|mute [--until T] "why"
                                   delivery class for matching event kinds (newest matching rule wins): now = wake at
                                   once, even while background agents run; beat = wake for it (the default); digest =
                                   no wake of its own, grouped in the next agenda; mute = no wake, still listed as a
                                   count + ids until acked. Default until: +24 h; at most 30 days (lasting defaults
                                   belong in the kind registry). A wildcard never matches a kind outside the registry:
                                   unknown kinds stay beat unless a rule names them exactly.
  wake at <time> "why"             a timer: `remind add --tag wake` (a reminder note for me at that time; it wakes the
                                   brain even during a hold)
  wake list [--all] [--json]       active rules (the hold first) and pending timers; --all adds ended rules
  wake rm ID... | hold | all | t:TIMER_ID   end rules (kept with active=0) or cancel a timer (`remind cancel`)
  wake check [--json] [--inbox DIR]
                                   what the heartbeat gate sees now: the hold and every ~/inbox note by delivery class
                                   (kind registry + rules). Read-only; exit 3 when the store can't be read.

Time: +30m, +2h, +1d, +1h30m, HH:MM (next one), "tomorrow HH:MM", "DD.MM[.YYYY] HH:MM", "YYYY-MM-DD HH:MM", epoch
seconds, else whatever `remind when` reads ("tomorrow evening", "in 2 hours"); owner zone.
Store: wake_rules in ~/.local/state/assistant/events.db (core.db). The gate ignores all rules while
~/.config/hb-gate/wake-rules-off exists. Exit: 0 ok, 2 usage, 3 failed (store, remind), 1 unexpected.
Env (tests): HOME, ASSISTANT_TEST=1, ASSISTANT_NOW, ASSISTANT_TZ, ASSISTANT_KINDS, WAKE_REMIND (the remind binary).
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import sqlite3
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta, tzinfo
from typing import Any

from core import clock, db, events, paths, todo

from . import kinds

__all__ = ["HOLD", "check", "main", "parse_when"]

HOLD = "hold"
RULE_DEFAULT_H = 24
RULE_MAX_D = 30
HOLD_MAX_H = 24
WHY_MAX = 200
NAMES_MAX = 50
TIMER_TAG = "wake"
_GLOB_OK = re.compile(r"^[a-z0-9.*?\[\]_-]{1,80}$")
_HM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_DUR = re.compile(r"^\+?(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m(?:in)?)?$")


class Usage(Exception):
    pass


class Failed(Exception):
    pass


# ------------------------------------------------------------------------------------------------ time


def _remind_bin() -> str:
    return os.environ.get("WAKE_REMIND") or os.fspath(paths.bin_dir("remind"))


def _remind_when(phrase: str, tz: tzinfo) -> float | None:
    """`remind when PHRASE --json` -> its "at" (home wall time) as epoch seconds; None when it can't read it."""
    try:
        r = subprocess.run(
            [_remind_bin(), "when", phrase, "--json"],
            capture_output=True,
            text=True,
            timeout=15,
            stdin=subprocess.DEVNULL,
        )
        d = json.loads(r.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if not isinstance(d, dict) or not d.get("ok") or d.get("kind") != "at" or not d.get("at"):
        return None
    try:
        return datetime.strptime(str(d["at"]), "%Y-%m-%d %H:%M").replace(tzinfo=tz).timestamp()
    except ValueError:
        return None


def _at(day: datetime, hm: re.Match[str], tz: tzinfo) -> datetime:
    return datetime(day.year, day.month, day.day, int(hm.group(1)), int(hm.group(2)), tzinfo=tz)


def parse_when(raw: str, now: float, tz: tzinfo, *, phrases: bool = True) -> float:
    """An instant (epoch s) from the forms in the module doc; Usage when unreadable."""
    s = " ".join(raw.strip().split())
    low = s.lower()
    if not s:
        raise Usage("empty time")
    m = _DUR.match(low)
    if m and any(m.groups()) and (low.startswith("+") or not low.isdigit()):
        d, h, mi = (int(x or 0) for x in m.groups())
        secs = ((d * 24 + h) * 60 + mi) * 60
        if secs <= 0:
            raise Usage(f"duration must be positive: {raw!r}")
        return now + secs
    if re.fullmatch(r"\d{9,11}(?:\.\d+)?", s):
        return float(s)
    today = datetime.fromtimestamp(now, tz)
    hm = _HM.match(low)
    if hm:
        t = _at(today, hm, tz)
        if t.timestamp() <= now < t.replace(fold=1).timestamp():
            t = t.replace(fold=1)  # the repeated autumn hour: its second pass is still ahead (not tomorrow's)
        return (t if t.timestamp() > now else _at(today + timedelta(days=1), hm, tz)).timestamp()
    parts = low.split(" ")
    day_hm = _HM.match(parts[1]) if len(parts) == 2 else None
    if day_hm and parts[0] in ("tomorrow", "morgen", "today", "heute"):
        day = today + timedelta(days=1) if parts[0] in ("tomorrow", "morgen") else today
        return _at(day, day_hm, tz).timestamp()
    dm = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.?(\d{4})?[ t](\d{1,2}:\d{2})", low)
    if dm:
        hm2 = _HM.match(dm.group(4))
        if not hm2:
            raise Usage(f"bad time {raw!r}")
        year = int(dm.group(3)) if dm.group(3) else today.year
        try:
            t = _at(datetime(year, int(dm.group(2)), int(dm.group(1))), hm2, tz)
            if not dm.group(3) and t.timestamp() < now - 86400 * 2:  # no year: the next such day
                t = _at(datetime(year + 1, int(dm.group(2)), int(dm.group(1))), hm2, tz)
        except ValueError as e:
            raise Usage(f"bad date {raw!r}") from e
        return t.timestamp()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dt%H:%M"):
        try:
            return datetime.strptime(low, fmt).replace(tzinfo=tz).timestamp()
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(s)
        return (dt if dt.tzinfo else dt.replace(tzinfo=tz)).timestamp()
    except ValueError:
        pass
    if phrases:
        t2 = _remind_when(s, tz)
        if t2 is not None:
            return t2
    raise Usage(f"can't read the time {raw!r} (+30m, HH:MM, 'tomorrow 09:00', 'DD.MM HH:MM', 'YYYY-MM-DD HH:MM')")


def _hm(ts: float | None, tz: tzinfo, now: float) -> str:
    if not ts:
        return "–"
    d = datetime.fromtimestamp(ts, tz)
    return d.strftime("%H:%M") if d.date() == datetime.fromtimestamp(now, tz).date() else d.strftime("%d.%m %H:%M")


def _split_when(args: list[str]) -> tuple[str, list[str]]:
    """'tomorrow 09:00 why…' or '22.10 09:00 why…' unquoted: the time takes the HH:MM word after it."""
    if len(args) >= 2 and _HM.match(args[1]) and not _HM.match(args[0]) and not args[0].startswith("+"):
        return f"{args[0]} {args[1]}", args[2:]
    return (args[0], args[1:]) if args else ("", [])


def _why(words: list[str]) -> str:
    why = " ".join(" ".join(words).split())
    if not why:
        raise Usage('every rule needs a why: wake … "why"')
    return why[:WHY_MAX]


# ------------------------------------------------------------------------------------------------ store


def _rw() -> sqlite3.Connection:
    try:
        return db.ledger(reserve={"wake_rules": 4}) if _hub() else db.connect()
    except (sqlite3.Error, OSError) as e:
        raise Failed(f"store: {type(e).__name__}: {e}"[:200]) from e


def _hub() -> bool:
    from core import hub

    return hub.backend("events") == "hub"


def _ro() -> sqlite3.Connection | None:
    try:
        return db.ledger(ro=True)
    except FileNotFoundError:
        return None
    except (sqlite3.Error, OSError) as e:
        raise Failed(f"store: {type(e).__name__}: {e}"[:200]) from e


def _rules(con: sqlite3.Connection | None, now: float) -> list[dict[str, Any]]:
    if con is None:
        return []
    try:
        return events.wake_rules(con, now)
    except sqlite3.Error as e:
        raise Failed(f"store: {type(e).__name__}: {e}"[:200]) from e


def _hold(rules: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((r for r in rules if r.get("klass") == HOLD), None)


# ------------------------------------------------------------------------------------------------ check (the gate)


def inbox_notes(inbox_dir: str) -> list[str]:
    """Regular, non-hidden files directly in the inbox (the gate's own definition of a note)."""
    try:
        with os.scandir(inbox_dir) as it:
            return sorted(e.name for e in it if not e.name.startswith(".") and e.is_file(follow_symlinks=False))
    except FileNotFoundError:
        return []


def check(inbox_dir: str, now: float) -> dict[str, Any]:
    reg = kinds.load()
    con = _ro()
    try:
        rules = _rules(con, now)
    finally:
        if con is not None:
            con.close()
    known = set(reg.kinds)
    classes: dict[str, list[str]] = {c: [] for c in events.CLASSES}
    per_kind: dict[str, Counter[str]] = {c: Counter() for c in events.CLASSES}
    for name in inbox_notes(inbox_dir):
        kind = reg.classify(name).kind
        cls = events.effective_delivery({"kind": kind, "delivery": reg.delivery(kind)}, rules, known)
        classes[cls].append(name)
        per_kind[cls][kind] += 1
    hold = _hold(rules)
    return {
        "ok": True,
        "ts": now,
        "db": "ok" if con is not None else "missing",
        "registry_error": reg.error,
        "rules": sum(1 for r in rules if r.get("klass") != HOLD),
        "hold": None
        if hold is None
        else {"id": hold["id"], "until": hold["until"], "why": hold.get("why") or "", "created": hold["created"]},
        "counts": {c: len(v) for c, v in classes.items()},
        "kinds": {c: dict(sorted(v.items())) for c, v in per_kind.items()},
        "classes": {c: v[:NAMES_MAX] for c, v in classes.items()},
    }


# ------------------------------------------------------------------------------------------------ commands


def cmd_next(args: list[str], now: float, tz: tzinfo) -> int:
    raw, rest = _split_when(args)
    if not raw:
        raise Usage('wake next <time|+dur> "why"')
    until = parse_when(raw, now, tz)
    why = _why(rest)
    if until <= now + 30:
        raise Usage(f"{raw!r} is not in the future")
    if until > now + HOLD_MAX_H * 3600:
        raise Usage(f"a hold reaches at most {HOLD_MAX_H} h ahead (the 3 h safety valve wakes anyway)")
    con = _rw()
    try:
        with db.tx(con):
            ended = con.execute("UPDATE wake_rules SET active=0 WHERE active=1 AND klass=?", (HOLD,)).rowcount
            rid = con.execute(
                "INSERT INTO wake_rules(created, kind_glob, klass, until, why) VALUES(?, NULL, ?, ?, ?)",
                (now, HOLD, until, why),
            ).lastrowid
    except sqlite3.Error as e:
        raise Failed(f"store: {type(e).__name__}: {e}"[:200]) from e
    finally:
        con.close()
    print(
        f"hold #{rid}: idle beats held until {_hm(until, tz, now)} ({why})"
        + (f"; replaced {ended} older hold(s)" if ended else "")
        + ". Inbox events, Due items, finished agents, timers and the 3 h valve still wake. End early: wake rm hold"
    )
    return 0


def cmd_on(args: list[str], now: float, tz: tzinfo) -> int:
    until_raw = None
    rest: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--until":
            if i + 1 >= len(args):
                raise Usage("--until needs a time")
            until_raw = args[i + 1]
            i += 2
            continue
        if a.startswith("--until="):
            until_raw = a.split("=", 1)[1]
        else:
            rest.append(a)
        i += 1
    if len(rest) < 2:
        raise Usage('wake on <kind-glob> now|beat|digest|mute [--until T] "why"')
    glob, klass = rest[0].strip().lower(), rest[1].strip().lower()
    why = _why(rest[2:])
    if klass not in events.CLASSES:
        raise Usage(f"class must be one of {', '.join(events.CLASSES)}")
    if not _GLOB_OK.match(glob):
        raise Usage("kind-glob: lowercase kind names with * ? [] (e.g. note.rave-*), at most 80 characters")
    until = parse_when(until_raw, now, tz) if until_raw else now + RULE_DEFAULT_H * 3600
    if until <= now + 30:
        raise Usage("--until is not in the future")
    if until > now + RULE_MAX_D * 86400:
        raise Usage(f"a rule lasts at most {RULE_MAX_D} days; a lasting default belongs in the kind registry")
    reg = kinds.load()
    matched = sorted(k for k in reg.kinds if fnmatch.fnmatchcase(k, glob))
    now_kinds = [k for k in matched if reg.delivery(k) == "now"] if klass != "now" else []
    con = _rw()
    try:
        with db.tx(con):
            rid = con.execute(
                "INSERT INTO wake_rules(created, kind_glob, klass, until, why) VALUES(?, ?, ?, ?, ?)",
                (now, glob, klass, until, why),
            ).lastrowid
    except sqlite3.Error as e:
        raise Failed(f"store: {type(e).__name__}: {e}"[:200]) from e
    finally:
        con.close()
    exact = not events.WILDCARD.search(glob)
    if matched:
        reach = f"registry kinds: {', '.join(matched[:8])}" + (f" +{len(matched) - 8}" if len(matched) > 8 else "")
    elif exact:
        reach = "no registry kind of that name (an unknown kind, matched by its exact name)"
    else:
        reach = "matches no registry kind (a wildcard never reaches unknown kinds: name them exactly)"
    if now_kinds:  # his live inputs (calls, app chat, shares) default to now: say so when a rule slows them down
        reach += f"; includes now-kinds: {', '.join(now_kinds)}"
    print(f"rule #{rid}: {glob} → {klass} until {_hm(until, tz, now)} ({why}); {reach}")
    return 0


def _timers() -> tuple[list[dict[str, Any]], str | None]:
    try:
        r = subprocess.run(
            [_remind_bin(), "list", "--json"], capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL
        )
        if r.returncode != 0:
            return [], f"remind list exit {r.returncode}"
        d = json.loads(r.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        return [], f"remind list: {type(e).__name__}"
    items = d.get("items") if isinstance(d, dict) else None
    out = [
        {"id": str(i.get("id")), "at": i.get("at"), "text": str(i.get("text") or "")[:120]}
        for i in items or []
        if isinstance(i, dict) and i.get("tag") == TIMER_TAG and not i.get("owner")
    ]
    return out, None


def cmd_at(args: list[str], now: float, tz: tzinfo) -> int:
    raw, rest = _split_when(args)
    if not raw:
        raise Usage('wake at <time> "why"')
    why = _why(rest)
    try:
        at = parse_when(raw, now, tz, phrases=False)
    except Usage:
        at = None  # a phrase: remind reads it itself
    if at is not None and at <= now + 30:
        raise Usage(f"{raw!r} is not in the future")
    when = datetime.fromtimestamp(at, tz).strftime("%Y-%m-%d %H:%M") if at is not None else raw
    try:
        r = subprocess.run(
            [_remind_bin(), "add", "--tag", TIMER_TAG, when, f"wake timer: {why}"],
            capture_output=True,
            text=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise Failed(f"remind add: {type(e).__name__}: {e}"[:200]) from e
    if r.returncode == 2:
        raise Usage((r.stderr or r.stdout).strip()[:300] or "remind add refused the time")
    if r.returncode != 0:
        raise Failed(f"remind add exit {r.returncode}: {(r.stderr or r.stdout).strip()[:200]}")
    print(f"timer: {r.stdout.strip()[:200]} (tag {TIMER_TAG}; wake list shows it)")
    return 0


def _rule_line(r: dict[str, Any], tz: tzinfo, now: float) -> str:
    state = "" if r.get("active") and (not r.get("until") or r["until"] > now) else " [ended]"
    why = f" · {r.get('why')}" if r.get("why") else ""
    if r.get("klass") == HOLD:
        return f"#{r['id']:<4} hold: idle beats held until {_hm(r.get('until'), tz, now)}{why}{state}"
    return f"#{r['id']:<4} {r.get('kind_glob') or '*'} → {r['klass']} until {_hm(r.get('until'), tz, now)}{why}{state}"


def cmd_list(args: list[str], now: float, tz: tzinfo) -> int:
    allr = "--all" in args
    as_json = "--json" in args
    bad = [a for a in args if a not in ("--all", "--json")]
    if bad:
        raise Usage(f"unknown argument {bad[0]!r}")
    con = _ro()
    try:
        active = _rules(con, now)
        ended: list[dict[str, Any]] = []
        if allr and con is not None:
            ids = {r["id"] for r in active}
            rows = con.execute("SELECT * FROM wake_rules ORDER BY id DESC LIMIT 60").fetchall()
            ended = [dict(r) for r in rows if r["id"] not in ids][:30]
    except sqlite3.Error as e:
        raise Failed(f"store: {type(e).__name__}: {e}"[:200]) from e
    finally:
        if con is not None:
            con.close()
    active.sort(key=lambda r: (r.get("klass") != HOLD, -(r.get("created") or 0), -r["id"]))
    timers, terr = _timers()
    if as_json:
        print(json.dumps({"rules": active, "ended": ended, "timers": timers, "timers_error": terr}, ensure_ascii=False))
        return 0
    L = [_rule_line(r, tz, now) for r in active] or ["(no active rules)"]
    if timers:
        L += [f"t:{t['id']} {t.get('at') or '?'} · {t['text']}" for t in timers]
    elif terr:
        L.append(f"(timers unreadable: {terr})")
    if ended:
        L += ["ended:"] + ["  " + _rule_line(r, tz, now) for r in ended]
    print("\n".join(L))
    return 0


def cmd_rm(args: list[str], now: float, tz: tzinfo) -> int:
    if not args:
        raise Usage("wake rm ID... | hold | all | t:TIMER_ID")
    ids: list[int] = []
    timers: list[str] = []
    what: set[str] = set()
    for a in args:
        for part in a.replace(",", " ").split():
            p = part.lstrip("#")
            if p in ("hold", "all"):
                what.add(p)
            elif p.startswith("t:") and re.fullmatch(r"[A-Za-z0-9_-]{1,40}", p[2:]):
                timers.append(p[2:])
            elif p.isdigit():
                ids.append(int(p))
            else:
                raise Usage(f"bad rule id {part!r}")
    n = 0
    if ids or what:
        con = _rw()
        try:
            with db.tx(con):
                if "all" in what:
                    n += con.execute("UPDATE wake_rules SET active=0 WHERE active=1").rowcount
                elif "hold" in what:
                    n += con.execute("UPDATE wake_rules SET active=0 WHERE active=1 AND klass=?", (HOLD,)).rowcount
                for rid in ids:
                    n += con.execute("UPDATE wake_rules SET active=0 WHERE id=? AND active=1", (rid,)).rowcount
        except sqlite3.Error as e:
            raise Failed(f"store: {type(e).__name__}: {e}"[:200]) from e
        finally:
            con.close()
        print(f"ended {n} rule(s)")
    bad = 0
    for t in timers:
        try:
            r = subprocess.run(
                [_remind_bin(), "cancel", t], capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL
            )
            ok = r.returncode == 0
            msg = (r.stdout or r.stderr).strip()[:160]
        except (OSError, subprocess.SubprocessError) as e:
            ok, msg = False, type(e).__name__
        print(f"t:{t} {'cancelled' if ok else 'FAILED'}: {msg}")
        bad += not ok
    if bad:
        return 3
    if ids and not what and n < len(ids):
        print("(some ids were not active)", file=sys.stderr)
    return 0


def cmd_check(args: list[str], now: float, tz: tzinfo) -> int:
    as_json = False
    inbox_dir = os.fspath(paths.inbox())
    i = 0
    while i < len(args):
        if args[i] == "--json":
            as_json = True
        elif args[i] == "--inbox" and i + 1 < len(args):
            inbox_dir = args[i + 1]
            i += 1
        else:
            raise Usage(f"unknown argument {args[i]!r}")
        i += 1
    v = check(inbox_dir, now)
    if as_json:
        print(json.dumps(v, ensure_ascii=False))
        return 0
    h = v["hold"]
    L = [
        "hold: "
        + ("none" if not h else f"#{h['id']} until {_hm(h['until'], tz, now)} ({h['why']})")
        + f" · delivery rules: {v['rules']}"
        + ("" if v["db"] == "ok" else " (no store yet)")
    ]
    for c in events.CLASSES:
        ks = v["kinds"][c]
        L.append(f"{c}: {v['counts'][c]}" + (" (" + ", ".join(f"{k} ×{n}" for k, n in ks.items()) + ")" if ks else ""))
    if v["registry_error"]:
        L.append(f"kind registry: {v['registry_error']} (every note counts as beat)")
    print("\n".join(L))
    return 0


COMMANDS = {
    "next": cmd_next,
    "on": cmd_on,
    "at": cmd_at,
    "list": cmd_list,
    "ls": cmd_list,
    "rm": cmd_rm,
    "check": cmd_check,
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0 if argv else 2
    cmd = COMMANDS.get(argv[0])
    try:
        if cmd is None:
            raise Usage(f"unknown command {argv[0]!r} (next, on, at, list, rm, check)")
        return cmd(argv[1:], clock.now(), todo.owner_tz())
    except Usage as e:
        print(f"wake: {e}", file=sys.stderr)
        return 2
    except Failed as e:
        print(f"wake: {e}", file=sys.stderr)
        return 3
    except (sqlite3.Error, OSError) as e:
        print(f"wake: {type(e).__name__}: {e}"[:300], file=sys.stderr)
        return 3
