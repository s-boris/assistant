"""tgadapter.bind: the `tg-bind` command. Stdlib only.

The brain's bindings: a Telegram command or button bound in advance to a tool of the tools registry
(schemas/tools.toml), which tg-adapter then runs without a brain turn (adapters/tg/src/bind.ts). Rows live in the
events store's `bindings` table (DDL v2); `rm` retires a row (active 0), history stays.

  tg-bind list [--all] [--json]                 tg-bind rows (--all: also retired, expired and card binds)
  tg-bind add command|button|prefix KEY TOOL --why "…" [--arg A]... [--expires 7d|12h|30m|YYYY-MM-DD|EPOCH]
              [--note | --no-note] [--forward]
        command: KEY = the name without "/" (the owner's "/KEY" runs TOOL; "/KEY <text>" too when the tool takes
        text) · button: KEY = the exact callback data · prefix: every callback data starting with KEY (≥ 2 bytes,
        the longest prefix wins). An active row with the same kind and key is replaced. --note: the brain also
        gets a short "answered" event (default: the tool's note) · --forward: a failed command goes to the brain as
        the message instead of a ⚠️ line. Tools above send_owner are refused (approvals have their own ap: cards).
  tg-bind rm ID | KIND KEY                      retire a row
  tg-bind import FILE [--dry-run]               add or update many rows at once, in file order (the seed:
        ops/rollout/tg-bind-seed.json): {"rows": [{kind, key, tool, why, note?, forward?, args?, expires?}]}; every row
        is checked first (as add), then all are written in one transaction; rows not in the file are left as they are
  tg-bind export                                the active command, button and prefix rows (no card binds, nothing
        retired or expired) in the import format on stdout, in row order: `tg-bind import FILE` restores them
  tg-bind menu [--custom FILE] [--diff] [--json]
        the /commands menu set_commands would publish now: command rows with a menu line (the tool's `menu` in the
        registry), in row order, then the brain's own commands (its last set_commands, or FILE); --diff compares it
        with 0.1.22's (/status + instant.json + the same list) and exits 1 when they differ
  tg-bind compat [--json]                       how often the running adapter still used the 0.1.22 compat tables
        (instant.json, callbacks.json: an input resolved from one, menu entries taken from one) and when last: they
        can go once they have gone unread for 48 h (store meta compat_reads:<bot>; the adapter also logs each use)
  tg-bind tools [--json]                        the registry: name, effect, timeout, output, argv
  tg-bind off | on | status                     the switch ~/.config/tg-adapter/bindings-off: off = every command and
                                                tap goes to the brain (no restart); status also counts rows
  tg-bind table … | resolve …                   tg-adapter's own view (`tg-adapter bindings …`: tg-bind rows, card
                                                binds and the 0.1.22 compat tables, with what runs)
Every command takes --instance live|test (default live: the main events DB; test: the test bot's DB) or --db PATH.
Registry: the instance config's tools_registry, else $TG_ADAPTER_TOOLS, else schemas/tools.toml of the checkout
tg-adapter runs from (the same file the adapter reads).
Exit: 0 ok · 1 error · 2 usage or refused.
Instance live without --db: the rows live on the hub. Every command runs on an in-memory copy of the
hub's bindings (and its compat_reads meta); what a command changed goes back as one upsert (new or changed active
rows) plus retires (rows it set inactive). Card binds stay in the hub adapter's own journal (not listed here).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sqlite3
import sys
import time
import tomllib
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from core import db, paths

from . import ctl

__all__ = ["EFFECTS", "TAP_MAX_EFFECT", "load_registry", "main", "tap_allowed"]

EFFECTS = ("read", "home", "send_owner", "send_external", "spend", "irreversible")
TAP_MAX_EFFECT = "send_owner"
KINDS = ("command", "button", "prefix")
NAME = re.compile(r"^[a-z0-9_]{1,32}$")


def tap_allowed(effect: str) -> bool:
    return effect in EFFECTS and EFFECTS.index(effect) <= EFFECTS.index(TAP_MAX_EFFECT)


def registry_path(instance: str = "live") -> Path:
    """The registry tg-adapter reads (bind.ts): the instance config's "tools_registry", else $TG_ADAPTER_TOOLS, else
    schemas/tools.toml of the checkout the adapter runs from ($TG_ADAPTER_DIR/../..)."""
    cfg = ctl.read_config(instance) or {}
    if isinstance(cfg.get("tools_registry"), str) and cfg["tools_registry"].strip():
        return ctl._home_path(cfg["tools_registry"])
    v = os.environ.get("TG_ADAPTER_TOOLS", "")
    return ctl._home_path(v) if v else ctl.adapter_dir().parent.parent / "schemas" / "tools.toml"


def load_registry(path: Path) -> dict[str, dict[str, Any]]:
    """name -> {argv, effect, timeout_s, output, note, text_arg, description}; invalid entries are left out."""
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    out: dict[str, dict[str, Any]] = {}
    for name, t in (raw.get("tools") or {}).items():
        argv = t.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
            continue
        if t.get("effect") not in EFFECTS:
            continue
        out[name] = {
            "argv": argv,
            "effect": t["effect"],
            "timeout_s": min(float(t.get("timeout_s", 20)), 300.0),
            "output": "picture" if t.get("output") == "picture" else "text",
            "note": t.get("note") is True,
            "text_arg": t.get("text_arg") is True,
            "description": str(t.get("description", "")),
            "menu": str(t.get("menu", "")).strip(),
            "toast": str(t.get("toast", "")),
        }
    return out


def _db_path(a: argparse.Namespace) -> Path:
    """The DB tg-adapter's instance uses (its paths.ts): --db, else the config's "db", else test: the test DB under
    TG_ADAPTER_STATE_DIR, live: $ASSISTANT_EVENTS_DB or the main events DB."""
    if a.db:
        return paths.guard(ctl._home_path(a.db))
    cfg = ctl.read_config(a.instance) or {}
    if isinstance(cfg.get("db"), str) and cfg["db"].strip():
        return paths.guard(ctl._home_path(cfg["db"]))
    if a.instance == "test":
        return ctl.state_root() / "test.db"
    v = os.environ.get("ASSISTANT_EVENTS_DB", "")
    return paths.guard(ctl._home_path(v)) if v else db.default_path()


def _hub_mode(a: argparse.Namespace) -> bool:
    if getattr(a, "db", None) or getattr(a, "instance", "live") != "live":
        return False
    from core import hub

    return hub.tg_hub()


BIND_COLS = ("kind", "key", "tool", "args", "note", "on_error", "why", "created", "expires")


def _hub_copy() -> tuple[sqlite3.Connection, dict[int, dict[str, Any]]]:
    """An in-memory store holding the hub's bindings (ids kept) and compat_reads meta; plus the rows as read."""
    from core import hub

    rows = hub.tg_q("bindings_all", timeout=10.0)
    con = sqlite3.connect(":memory:", isolation_level=None)
    con.row_factory = sqlite3.Row
    for st in hub._memory_ddl():
        con.execute(st)
    before: dict[int, dict[str, Any]] = {}
    for r in rows:
        cols = ["id", *BIND_COLS, "active"]
        vals = [r.get(c) for c in cols]
        con.execute(f"INSERT INTO bindings({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)
        before[int(r["id"])] = dict(zip(cols, vals, strict=True))
    for k, v in hub.tg_meta("compat_reads:").items():
        con.execute("INSERT OR REPLACE INTO meta(k, v) VALUES (?, ?)", (k, v))
    return con, before


def _hub_push(con: sqlite3.Connection, before: dict[int, dict[str, Any]]) -> None:
    """The command's changes to the hub: new or changed active rows upserted (by kind + key), rows it retired."""
    from core import hub

    after = {int(r["id"]): dict(r) for r in con.execute("SELECT * FROM bindings")}
    cols = (*BIND_COLS, "active")
    up = [
        r
        for i, r in after.items()
        if r["active"] and (i not in before or any(before[i].get(c) != r.get(c) for c in cols))
    ]
    gone = [i for i, r in after.items() if not r["active"] and i in before and before[i].get("active")]
    if up:
        got = hub.call("POST", "/tg/bindings/upsert", {"rows": [{c: r.get(c) for c in BIND_COLS} for r in up],
                                                       "rid": hub.new_rid()}, timeout=10.0)  # fmt: skip
        moved = [f"#{r['id']} → #{h}" for r, h in zip(up, got.get("ids") or [], strict=False) if h != r["id"]]
        if moved:
            print("tg-bind: hub ids " + ", ".join(moved), file=sys.stderr)
    for i in gone:
        hub.call("POST", "/tg/bindings/retire", {"id": i, "rid": hub.new_rid()}, timeout=10.0)


@contextlib.contextmanager
def _open(a: argparse.Namespace, ro: bool = False) -> Iterator[sqlite3.Connection]:
    if _hub_mode(a):
        hcon, before = _hub_copy()
        try:
            yield hcon
            if not ro:
                _hub_push(hcon, before)
        finally:
            hcon.close()
        return
    con = db.connect(_db_path(a), ro=ro)
    try:
        yield con
    finally:
        con.close()


def switch_file() -> Path:
    """tg-adapter's bindings switch (bind.ts): $TG_ADAPTER_BINDINGS_OFF, else ~/.config/tg-adapter/bindings-off."""
    v = os.environ.get("TG_ADAPTER_BINDINGS_OFF", "")
    return paths.guard(ctl._home_path(v) if v else paths.home() / ".config" / "tg-adapter" / "bindings-off")


def parse_expiry(v: str | None, now: float) -> float | None:
    if not v:
        return None
    m = re.fullmatch(r"(\d+)([dhm])", v.strip())
    if m:
        return now + int(m.group(1)) * {"d": 86400, "h": 3600, "m": 60}[m.group(2)]
    if re.fullmatch(r"\d{9,11}(\.\d+)?", v.strip()):
        return float(v)
    try:
        return datetime.strptime(v.strip(), "%Y-%m-%d").timestamp()
    except ValueError:
        raise SystemExit(_usage(f"--expires: {v!r} is not 7d / 12h / 30m / YYYY-MM-DD / epoch seconds")) from None


def _usage(msg: str) -> int:
    print(f"tg-bind: {msg}", file=sys.stderr)
    return 2


def _check_key(kind: str, key: str) -> str | None:
    if kind == "command":
        return None if NAME.fullmatch(key.lstrip("/").lower()) else "a command name is 1-32 of a-z 0-9 _"
    n = len(key.encode())
    if key.startswith("perm:") or (kind == "prefix" and "perm:".startswith(key)):
        return '"perm:" data is the permission relay\'s'
    if key.startswith("ap:") or (kind == "prefix" and "ap:".startswith(key)):
        return '"ap:" data is the approvals\': a tap on it is decided by tg-adapter, never a binding'
    if kind == "button" and not 1 <= n <= 64:
        return "button data is 1-64 bytes"
    if kind == "prefix" and not 2 <= n <= 63:
        return "a prefix is 2-63 bytes"
    return None


def _fmt_ts(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m %H:%M") if ts else "-"


def _rows(con: sqlite3.Connection, everything: bool) -> list[dict[str, Any]]:
    q = "SELECT * FROM bindings"
    if not everything:
        q += " WHERE active = 1 AND kind != 'card' AND (expires IS NULL OR expires > ?)"
        return [dict(r) for r in con.execute(q + " ORDER BY kind, key", (time.time(),))]
    return [dict(r) for r in con.execute(q + " ORDER BY id")]


def cmd_list(a: argparse.Namespace) -> int:
    with _open(a) as con:
        rows = _rows(con, a.all)
    if a.json:
        print(json.dumps(rows))
        return 0
    if not rows:
        print("no tg-bind rows" + ("" if a.all else " (tg-bind table: the compat tables too)"))
    for r in rows:
        state = "" if r["active"] else " (retired)"
        exp = f" · expires {_fmt_ts(r['expires'])}" if r["expires"] else ""
        args = " " + " ".join(json.loads(r["args"] or "[]")) if r["args"] not in (None, "[]") else ""
        flags = ("note " if r["note"] else "") + ("forward " if r["on_error"] == "forward" else "")
        print(f"#{r['id']} {r['kind']} {r['key']} → {r['tool']}{args} · {flags}why: {r['why']}{exp}{state}")
    return 0


def cmd_add(a: argparse.Namespace) -> int:
    key = a.key.lstrip("/").lower() if a.kind == "command" else a.key
    bad = _check_key(a.kind, key)
    if bad:
        return _usage(bad)
    if not a.why or not a.why.strip():
        return _usage("--why is required (one line: why this binding exists)")
    try:
        reg = load_registry(registry_path(a.instance))
    except (OSError, tomllib.TOMLDecodeError) as e:
        print(f"tg-bind: tools registry unreadable: {e}", file=sys.stderr)
        return 1
    tool = reg.get(a.tool)
    if tool is None:
        return _usage(
            f"tool {a.tool!r} is not in the tools registry ({registry_path(a.instance)}); `tg-bind tools` lists them"
        )
    if not tap_allowed(tool["effect"]):
        return _usage(
            f"{a.tool} has effect {tool['effect']}: above send_owner, a tap may not run it without an approval"
        )
    now = time.time()
    expires = parse_expiry(a.expires, now)
    if expires is not None and expires <= now:
        return _usage("--expires is in the past")
    note = tool["note"] if a.note is None else a.note
    with _open(a) as con, db.tx(con):
        con.execute(
            "INSERT INTO bindings(kind, key, tool, args, note, on_error, why, created, expires, active)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1) ON CONFLICT(kind, key) DO UPDATE SET tool = excluded.tool,"
            " args = excluded.args, note = excluded.note, on_error = excluded.on_error, why = excluded.why,"
            " created = excluded.created, expires = excluded.expires, active = 1",
            (
                a.kind,
                key,
                a.tool,
                json.dumps(a.arg or []),
                1 if note else 0,
                "forward" if a.forward else "reply",
                a.why.strip(),
                now,
                expires,
            ),
        )
        rid = con.execute("SELECT id FROM bindings WHERE kind = ? AND key = ?", (a.kind, key)).fetchone()[0]
    what = f"/{key}" if a.kind == "command" else f"{a.kind} {key}"
    print(f"bound #{rid}: {what} → {a.tool} ({tool['effect']}){' until ' + _fmt_ts(expires) if expires else ''}")
    if switch_file().exists():
        print("note: bindings are off (tg-bind on turns them on)")
    return 0


UPSERT = (
    "INSERT INTO bindings(kind, key, tool, args, note, on_error, why, created, expires, active)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1) ON CONFLICT(kind, key) DO UPDATE SET tool = excluded.tool,"
    " args = excluded.args, note = excluded.note, on_error = excluded.on_error, why = excluded.why,"
    " created = excluded.created, expires = excluded.expires, active = 1"
)


def _import_row(r: Any, reg: dict[str, dict[str, Any]], now: float) -> tuple[tuple[Any, ...] | None, str]:
    """One file row -> (the UPSERT values, "") or (None, why it is refused)."""
    if not isinstance(r, dict):
        return None, "not an object"
    kind, key, tool, why = r.get("kind"), r.get("key"), r.get("tool"), r.get("why")
    if kind not in KINDS:
        return None, f"kind must be one of {', '.join(KINDS)}"
    if not isinstance(key, str) or not isinstance(tool, str):
        return None, "key and tool must be strings"
    key = key.lstrip("/").lower() if kind == "command" else key
    bad = _check_key(kind, key)
    if bad:
        return None, bad
    if not isinstance(why, str) or not why.strip():
        return None, "why is required"
    t = reg.get(tool)
    if t is None:
        return None, f"tool {tool!r} is not in the tools registry"
    if not tap_allowed(t["effect"]):
        return None, f"{tool} has effect {t['effect']}: above send_owner"
    args = r.get("args", [])
    if not isinstance(args, list) or not all(isinstance(x, str) for x in args):
        return None, "args must be a list of strings"
    note = r.get("note", t["note"])
    if not isinstance(note, bool) or not isinstance(r.get("forward", False), bool):
        return None, "note and forward must be true or false"
    exp = r.get("expires")
    expires = parse_expiry(str(exp), now) if exp else None
    if expires is not None and expires <= now:
        return None, "expires is in the past"
    fwd = "forward" if r.get("forward") else "reply"
    return (kind, key, tool, json.dumps(args), 1 if note else 0, fwd, why.strip(), now, expires), ""


def cmd_import(a: argparse.Namespace) -> int:
    try:
        with open(a.file, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as e:
        print(f"tg-bind: {a.file}: {e}", file=sys.stderr)
        return 1
    rows = doc.get("rows") if isinstance(doc, dict) else doc
    if not isinstance(rows, list) or not rows:
        return _usage(f'{a.file}: want {{"rows": [...]}} or a list of rows')
    try:
        reg = load_registry(registry_path(a.instance))
    except (OSError, tomllib.TOMLDecodeError) as e:
        print(f"tg-bind: tools registry unreadable: {e}", file=sys.stderr)
        return 1
    now = time.time()
    vals, bad, seen = [], [], set()
    for i, r in enumerate(rows, 1):
        v, why = _import_row(r, reg, now)
        if v is not None and (v[0], v[1]) in seen:
            v, why = None, "duplicate kind and key in the file"
        if v is None:
            bad.append(f"row {i}: {why}")
            continue
        seen.add((v[0], v[1]))
        vals.append(v)
    if bad:
        print("tg-bind: nothing imported:\n  " + "\n  ".join(bad), file=sys.stderr)
        return 2
    with _open(a) as con:
        have = {(r["kind"], r["key"]): dict(r) for r in con.execute("SELECT * FROM bindings WHERE kind != 'card'")}
        new = [v for v in vals if (v[0], v[1]) not in have]
        same = [
            v
            for v in vals
            if (h := have.get((v[0], v[1])))
            and h["active"]
            and (h["tool"], h["args"], h["note"], h["on_error"], h["why"], h["expires"])
            == (v[2], v[3], v[4], v[5], v[6], v[8])
        ]
        changed = len(vals) - len(new) - len(same)
        if a.dry_run:
            print(f"dry run: {len(vals)} rows ok ({len(new)} new, {changed} to update, {len(same)} unchanged)")
            return 0
        with db.tx(con):
            for v in vals:
                if v not in same:
                    con.execute(UPSERT, v)
    print(f"imported {len(vals)} rows: {len(new)} new, {changed} updated, {len(same)} unchanged")
    if switch_file().exists():
        print("note: bindings are off (tg-bind on turns them on)")
    return 0


def export_rows(con: sqlite3.Connection, now: float) -> list[dict[str, Any]]:
    """The active command, button and prefix rows in the import format, in row order (= menu order on a restore)."""
    out = []
    q = (
        "SELECT * FROM bindings WHERE active = 1 AND kind IN ('command', 'button', 'prefix')"
        " AND (expires IS NULL OR expires > ?) ORDER BY id"
    )
    for r in con.execute(q, (now,)):
        row = {
            "kind": r["kind"],
            "key": r["key"],
            "tool": r["tool"],
            "why": r["why"],
            "note": bool(r["note"]),
            "forward": r["on_error"] == "forward",
            "args": json.loads(r["args"] or "[]"),
        }
        if r["expires"]:
            row["expires"] = r["expires"]
        out.append(row)
    return out


def cmd_export(a: argparse.Namespace) -> int:
    try:
        with _open(a, ro=True) as con:
            rows = export_rows(con, time.time())
    except FileNotFoundError as e:
        print(f"tg-bind: events store: no DB at {e}", file=sys.stderr)
        return 1
    print(json.dumps({"rows": rows}, indent=1, ensure_ascii=False))
    return 0


def cmd_compat(a: argparse.Namespace) -> int:
    """The running adapter's compat-table uses (bind.ts compatUse), read from the store's meta."""
    cfg = ctl.read_config(a.instance) or {}
    bot = str(cfg.get("bot") or "")
    if not bot:
        print(f"tg-bind: no bot in the {a.instance} config", file=sys.stderr)
        return 1
    try:
        with _open(a, ro=True) as con:
            raw = db.meta_get(con, f"compat_reads:{bot}")
    except (FileNotFoundError, sqlite3.Error) as e:
        print(f"tg-bind: events store: {e}", file=sys.stderr)
        return 1
    c = raw if isinstance(raw, dict) else None
    if a.json:
        print(json.dumps({"bot": bot, "compat_reads": c}))
        return 0
    if not c:
        print("compat reads: not counted yet (the running adapter starts the counter)")
        return 0
    uses = [k for k in ("instant", "callbacks", "menu") if isinstance(c.get(k), dict)]
    last = max([float(c.get("since") or 0)] + [float(c[k].get("last") or 0) for k in uses])
    print(
        f"compat reads since {_fmt_ts(c.get('since'))}: "
        + (", ".join(f"{k} {c[k].get('n', 0)}" for k in uses) if uses else "none")
    )
    for k in uses:
        print(f"  {k}: last {_fmt_ts(c[k].get('last'))} ({c[k].get('key', '')})")
    hours = (time.time() - last) / 3600
    print(
        f"unread for {hours:.1f} h{'' if uses else ' (since the counter started)'}"
        + (" · 48 h reached" if hours >= 48 else "")
    )
    return 0


def cmd_rm(a: argparse.Namespace) -> int:
    with _open(a) as con, db.tx(con):
        if len(a.target) == 1 and a.target[0].lstrip("#").isdigit():
            cur = con.execute(
                "UPDATE bindings SET active = 0 WHERE id = ? AND active = 1", (int(a.target[0].lstrip("#")),)
            )
        elif len(a.target) == 2 and a.target[0] in KINDS + ("card",):
            key = a.target[1].lstrip("/").lower() if a.target[0] == "command" else a.target[1]
            cur = con.execute(
                "UPDATE bindings SET active = 0 WHERE kind = ? AND key = ? AND active = 1", (a.target[0], key)
            )
        else:
            return _usage("rm takes ID or KIND KEY")
    if not cur.rowcount:
        print("tg-bind: no active binding matched", file=sys.stderr)
        return 1
    print("retired")
    return 0


def cmd_tools(a: argparse.Namespace) -> int:
    try:
        reg = load_registry(registry_path(a.instance))
    except (OSError, tomllib.TOMLDecodeError) as e:
        print(f"tg-bind: tools registry unreadable: {e}", file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(reg))
        return 0
    for name, t in reg.items():
        gate = "" if tap_allowed(t["effect"]) else " (not from a tap)"
        flags = " ".join(
            f
            for f, on in (("picture", t["output"] == "picture"), ("text_arg", t["text_arg"]), ("note", t["note"]))
            if on
        )
        print(f"{name:28} {t['effect']:13} {t['timeout_s']:>5.0f}s {flags:22} {' '.join(t['argv'])}{gate}")
    return 0


def cmd_switch(a: argparse.Namespace) -> int:
    f = switch_file()
    if a.cmd == "off":
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("bindings off: every Telegram command and tap goes to the brain (tg-bind on)\n")
        print("bindings off: commands and taps go to the brain")
        return 0
    if a.cmd == "on":
        try:
            f.unlink()
        except FileNotFoundError:
            pass
        print("bindings on (tg-adapter answers bound commands and taps while its send is on)")
        return 0
    n = 0
    try:
        with _open(a, ro=True) as con:
            n = len(_rows(con, False))
    except (FileNotFoundError, sqlite3.Error):
        pass
    print(f"bindings {'off (switch file)' if f.exists() else 'on'} · {n} active tg-bind row(s)")
    return 0


def cmd_adapter(a: argparse.Namespace, rest: list[str]) -> int:
    """`tg-bind table|resolve|menu …` = `tg-adapter bindings [resolve|menu …] --instance I` (the bun side's view)."""
    from . import cli

    sub = [] if a.cmd == "table" else [a.cmd]
    # the bun CLI runs in the adapter's dir: a relative --custom FILE is the caller's
    rest = [os.path.abspath(x) if i and rest[i - 1] == "--custom" else x for i, x in enumerate(rest)]
    return cli.main(["bindings", *sub, *rest, "--instance", a.instance])


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tg-bind", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--instance", choices=("live", "test"), default="live")
    common.add_argument("--db", default="")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("list", parents=[common])
    s.add_argument("--all", action="store_true")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("add", parents=[common])
    s.add_argument("kind", choices=KINDS)
    s.add_argument("key")
    s.add_argument("tool")
    s.add_argument("--why", required=True)
    s.add_argument("--arg", action="append")
    s.add_argument("--expires")
    s.add_argument("--note", dest="note", action="store_true", default=None)
    s.add_argument("--no-note", dest="note", action="store_false")
    s.add_argument("--forward", action="store_true")
    s = sub.add_parser("rm", parents=[common])
    s.add_argument("target", nargs="+")
    s = sub.add_parser("tools", parents=[common])
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("import", parents=[common])
    s.add_argument("file")
    s.add_argument("--dry-run", action="store_true")
    sub.add_parser("export", parents=[common])
    s = sub.add_parser("compat", parents=[common])
    s.add_argument("--json", action="store_true")
    for name in ("off", "on", "status"):
        sub.add_parser(name, parents=[common])
    for name in ("table", "resolve", "menu"):
        sub.add_parser(name, parents=[common])
    return p


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    p = parser()
    if argv[:1] in (["table"], ["resolve"], ["menu"]):
        a, rest = p.parse_known_args(argv)
        return cmd_adapter(a, rest)
    a = p.parse_args(argv)
    try:
        if a.cmd == "list":
            return cmd_list(a)
        if a.cmd == "add":
            return cmd_add(a)
        if a.cmd == "rm":
            return cmd_rm(a)
        if a.cmd == "tools":
            return cmd_tools(a)
        if a.cmd == "import":
            return cmd_import(a)
        if a.cmd == "export":
            return cmd_export(a)
        if a.cmd == "compat":
            return cmd_compat(a)
        return cmd_switch(a)
    except paths.RealHomeRefused as e:
        print(f"tg-bind: {e}", file=sys.stderr)
        return 1
    except sqlite3.Error as e:
        print(f"tg-bind: events store: {e}", file=sys.stderr)
        return 1
