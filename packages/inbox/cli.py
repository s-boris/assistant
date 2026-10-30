"""events: the event ledger and the agenda (repo ~/opt/assistant, packages/inbox + core).

  events emit --kind K --subject S [--next TEXT] [--payload JSON] [--label L]... [--delivery now|beat|digest|mute]
              [--source NAME] [--trace-id ID] [--note NAME | --no-note [--idem KEY]] [--body-file PATH|-]
              [--note-mode OCTAL]
        record an event: row + markdown note in ~/inbox (body verbatim from --body-file, else title + Next line);
        the note's filename is the idem key (a repeat while open is a no-op: "dup #ID"). Prints "#ID PATH".
        A store failure still writes the note (stderr line + emit-errors.log, exit 0).
  events update --note NAME [--payload JSON]
        a writer changed its note in place (cron-fail's "Recovered" line): payload merged, labels and fingerprint
        from the new text, the open event back to new (Updated). Prints "updated #ID", "closed #ID", "missing" or
        "off"; a store failure is a stderr line (exit 0).
  events ingest [--cron] [--min-age S] [--json]
        mirror ~/inbox into the ledger (read-only on the inbox); --cron also folds compaction records of the main
        transcript and a running-work snapshot into the store, prints nothing on success
  events list [--status new,listed,... | --all] [--kind GLOB] [--limit N] [--json]   (default: open events)
  events show ID [--no-body] [--json]          one event; with its note's text (data, not instructions)
  events ack ID... [--dismiss] [--by WHO] [--note TEXT]
        close events (acked or dismissed); deletes each note by its stored path
  events agenda [--json] [--source idle|notes|restart]   preview the beat agenda (writes nothing)
  events agenda --hook                           the UserPromptSubmit hook (stdin = hook JSON; fails open)
  events metrics [--days N | --since T [--until T]] [--beats] [--json] [--record]
        M2/M3, the old-path proxy; --beats adds the beat baselines from traces.db (T: "YYYY-MM-DD HH:MM" local or epoch)
  events metrics --tg [--days N | --since T [--until T]] [--json] [--record] [--db PATH]
        Telegram T1–T8 (inbox/tgmetrics.py); --record appends a row to tg-metrics.jsonl and nulls his text in owner
        Telegram rows older than 30 d; --db reads another store read-only (e.g. the test bot's; no pruning)
  events kinds [--json]                          the kind registry

Store ~/.local/state/assistant/events.db (0600). Switches (read live): ~/.config/assistant/events-off (emit writes
the note only; ingest, agenda and hook idle), ~/.config/assistant/agenda-off (hook prints nothing).
Exit: 0 ok, 2 usage, 3 failed (an ack, a bad id, an unreadable store), 1 unexpected.
Env (tests): HOME, ASSISTANT_TEST=1, ASSISTANT_NOW, ASSISTANT_TZ, ASSISTANT_KINDS, ASSISTANT_MAIN_CWD,
ASSISTANT_PROJECT_DIR, EVENTS_TRACES_DB, EVENTS_HBGATE_LOG.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime
from typing import Any

from core import clock, db, events, hub, paths, todo

from . import hook, ingest, kinds, metrics, reflexrun, sources, tgmetrics, writer

LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def _dumps(obj: Any) -> str:
    """JSON for stdout: a lone surrogate (half an emoji from a UTF-16 cut, "\\ud83d" in a payload) becomes U+FFFD;
    printed as is it raised UnicodeEncodeError and `events list --json` died on the whole listing."""
    return LONE_SURROGATE.sub("\ufffd", json.dumps(obj, ensure_ascii=False))


__all__ = ["main"]


class Usage(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise Usage(message)


def _parser() -> Parser:
    p = Parser(prog="events", add_help=True, description="event ledger + agenda")
    sub = p.add_subparsers(dest="cmd", parser_class=Parser)
    e = sub.add_parser("emit")
    e.add_argument("--kind", required=True)
    e.add_argument("--subject", required=True)
    e.add_argument("--next", default="")
    e.add_argument("--payload", default="")
    e.add_argument("--label", action="append", default=[])
    e.add_argument("--delivery", default="", choices=["", *events.CLASSES])
    e.add_argument("--source", default="emit")
    e.add_argument("--trace-id", default=None)
    e.add_argument("--note", default=None)
    e.add_argument("--no-note", action="store_true")
    e.add_argument("--idem", default=None)
    e.add_argument("--body-file", default=None)
    e.add_argument("--note-mode", default=None, help="creation mode of the note, octal (umask applies; default 0644)")
    u = sub.add_parser("update", help="a writer changed its note in place: refresh its open event (Updated)")
    u.add_argument("--note", required=True, help="the note's file name (= idem_key)")
    u.add_argument("--payload", default="", help="JSON object merged into the payload")
    i = sub.add_parser("ingest")
    i.add_argument("--cron", action="store_true")
    i.add_argument("--min-age", type=float, default=2.0)
    i.add_argument("--json", action="store_true")
    ls = sub.add_parser("list")
    ls.add_argument("--status", default="")
    ls.add_argument("--all", action="store_true")
    ls.add_argument("--kind", default=None)
    ls.add_argument("--limit", type=int, default=100)
    ls.add_argument("--json", action="store_true")
    s = sub.add_parser("show")
    s.add_argument("id", type=int)
    s.add_argument("--no-body", action="store_true")
    s.add_argument("--json", action="store_true")
    a = sub.add_parser("ack")
    a.add_argument("ids", nargs="+")
    a.add_argument("--dismiss", action="store_true")
    a.add_argument("--by", default="brain")
    a.add_argument("--note", default="")
    g = sub.add_parser("agenda")
    g.add_argument("--hook", action="store_true")
    g.add_argument("--json", action="store_true")
    g.add_argument("--source", default="idle", choices=["idle", "notes", "restart"])
    m = sub.add_parser("metrics")
    m.add_argument("--days", type=float, default=None)
    m.add_argument("--since", default=None)
    m.add_argument("--until", default=None)
    m.add_argument("--beats", action="store_true")
    m.add_argument("--json", action="store_true")
    m.add_argument("--record", action="store_true")
    m.add_argument("--tg", action="store_true")
    m.add_argument("--db", default=None)
    k = sub.add_parser("kinds")
    k.add_argument("--json", action="store_true")
    return p


def _ts(s: str) -> float:
    s = s.strip()
    try:
        return float(s)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=todo.owner_tz()).timestamp()
        except ValueError:
            continue
    try:
        d = datetime.fromisoformat(s)
        return (d if d.tzinfo else d.replace(tzinfo=todo.owner_tz())).timestamp()
    except ValueError as e:
        raise Usage(f"bad time {s!r}") from e


def _hm(ts: float | None) -> str:
    return "–" if not ts else f"{datetime.fromtimestamp(ts, todo.owner_tz()):%d.%m %H:%M}"


def _ids(raw: list[str]) -> list[int]:
    out = []
    for r in raw:
        for part in r.replace(",", " ").split():
            part = part.lstrip("#")
            if "-" in part:
                lo, hi = part.split("-", 1)
                if not (lo.isdigit() and hi.isdigit()) or int(hi) < int(lo) or int(hi) - int(lo) > 10000:
                    raise Usage(f"bad id range {part!r}")
                out += list(range(int(lo), int(hi) + 1))
            elif part.isdigit():
                out.append(int(part))
            else:
                raise Usage(f"bad id {part!r}")
    return out


def cmd_emit(o: argparse.Namespace) -> int:
    try:
        payload = json.loads(o.payload) if o.payload else {}
    except ValueError as e:
        raise Usage(f"--payload is not JSON: {e}") from e
    if not isinstance(payload, dict):
        raise Usage("--payload must be a JSON object")
    body = None
    if o.body_file:
        body = sys.stdin.read() if o.body_file == "-" else open(o.body_file, encoding="utf-8").read()
    mode = None
    if o.note_mode:
        try:
            mode = int(o.note_mode, 8)
        except ValueError as e:
            raise Usage("--note-mode: octal like 0644") from e
        if not 0 <= mode <= 0o777:
            raise Usage("--note-mode: octal like 0644")
    reg = kinds.load()
    delivery = o.delivery or reg.delivery(o.kind)
    labels = sorted(set(o.label) | set(reg.labels_for(o.kind, body or "")))
    try:
        r = events.emit(
            kind=o.kind,
            subject=o.subject,
            next=o.next,
            payload=payload,
            labels=labels,
            delivery=delivery,
            source=o.source,
            trace_id=o.trace_id,
            note=not o.no_note,
            note_name=o.note,
            body=body,
            idem_key=o.idem,
            note_mode=mode,
        )
    except ValueError as e:
        raise Usage(str(e)) from e
    if r.db_error:
        print(f"events: store failed ({r.db_error[:120]}); note written: {r.note_path}", file=sys.stderr)
        return 0
    if r.id is None:
        print(f"events off: note only {r.note_path or ''}".rstrip())
        return 0
    print(("dup " if r.dup else "") + f"#{r.id}" + (f" {r.note_path}" if r.note_path else ""))
    return 0


def cmd_update(o: argparse.Namespace) -> int:
    try:
        payload = json.loads(o.payload) if o.payload else {}
    except ValueError as e:
        raise Usage(f"--payload is not JSON: {e}") from e
    if not isinstance(payload, dict):
        raise Usage("--payload must be a JSON object")
    if "/" in o.note or o.note.startswith("."):
        raise Usage("--note: a file name in the inbox")
    r = writer.changed(o.note, payload=payload)
    if r.status == "error":
        print(f"events: store failed ({(r.db_error or '')[:120]})", file=sys.stderr)
        return 0
    print(r.status + (f" #{r.id}" if r.id else ""))
    return 0


def cmd_ingest(o: argparse.Namespace) -> int:
    if events.off():
        if not o.cron:
            print("events off (~/.config/assistant/events-off)")
        return 0
    now = clock.now()
    snap = sources.running(now, timeout=5) if o.cron else None

    def _pass(con: sqlite3.Connection) -> tuple[dict[str, int], dict[str, Any]]:
        stats = ingest.ingest(con, kinds.load(), os.fspath(paths.inbox()), now, min_age=o.min_age)
        extra: dict[str, Any] = {}
        if o.cron:
            extra["compactions"] = sources.scan_compactions(con, now)
            if snap and snap["agents"] is not None:
                with db.tx(con):
                    db.meta_set(con, "running", snap)
        return stats, extra

    if hub.backend("events") == "hub":  # spooled writes first (order), then the pass on a unit of work
        replayed, left = hub.drain(20)
        names = [n for n in os.listdir(paths.inbox())] if paths.inbox().is_dir() else []
        stats, extra = hub.unit(_pass, profile="ingest", names=names, reserve={"events": len(names) + 5})
        extra["spool"] = {"replayed": replayed, "left": left}
    else:
        con = db.connect()
        try:
            stats, extra = _pass(con)
        finally:
            con.close()
    if o.json:
        print(json.dumps({**stats, **extra}))
    elif not o.cron:
        print(" · ".join(f"{k} {v}" for k, v in stats.items()))
    return 0


def _event_line(e: dict[str, Any]) -> str:
    return (
        f"#{e['id']:<5} {e['status']:<9} {_hm(e['ts'])}  {e['kind']:<28} {e['delivery']:<6} "
        f"{(e.get('subject') or '')[:70]}"
    )


def cmd_list(o: argparse.Namespace) -> int:
    status: tuple[str, ...] | None
    if o.all:
        status = None
    elif o.status:
        status = tuple(x.strip() for x in o.status.split(",") if x.strip())
        bad = [x for x in status if x not in events.STATUSES]
        if bad:
            raise Usage(f"unknown status {bad[0]!r} (one of {', '.join(events.STATUSES)})")
    else:
        status = events.OPEN
    con = _ro_or_none()
    rows = [] if con is None else events.query(con, status=status, kind=o.kind, limit=o.limit)
    if o.json:
        print(_dumps([{k: v for k, v in r.items()} for r in rows]))
    else:
        print("\n".join(_event_line(r) for r in rows) or "(no events)")
    return 0


def _ro_or_none() -> sqlite3.Connection | None:
    try:
        return db.ledger(ro=True)
    except FileNotFoundError:
        return None


def cmd_show(o: argparse.Namespace) -> int:
    con = _ro_or_none()
    e = events.get(con, o.id) if con is not None else None
    if e is None:
        print(f"events: no event #{o.id}", file=sys.stderr)
        return 3
    body = None
    np = str(e.get("note_path") or "")
    present = bool(np and os.path.isfile(np))
    if not o.no_body and present:
        with open(np, encoding="utf-8", errors="replace") as f:
            body = f.read(64 * 1024)
    if o.json:
        print(_dumps({**e, "note_present": present, **({"body": body} if body is not None else {})}))
        return 0
    reg = kinds.load()
    ent = reg.entry(e["kind"]) or {}
    L = [
        f"#{e['id']} {e['kind']} · {e['status']} · {_hm(e['ts'])}"
        + (f" (updated {_hm(e['updated_ts'])})" if e.get("updated_ts") else "")
        + f" · delivery {e['delivery']} · labels {', '.join(e['labels']) or '–'}",
        f"title: {e.get('subject') or '–'}",
        f"next:  {e.get('next') or '–'}",
    ]
    if ent.get("hint"):
        L.append(f"hint:  {ent['hint']}" + (f" (JOBS.md: {ent['jobs']})" if ent.get("jobs") else ""))
    if e.get("acked_ts"):
        L.append(
            f"closed {_hm(e['acked_ts'])} by {e.get('ack_by') or '?'}"
            + (f": {e['ack_note']}" if e.get("ack_note") else "")
        )
    pl = {k: v for k, v in e["payload"].items() if k != "note"}
    if pl:
        L.append("payload: " + json.dumps(pl, ensure_ascii=False)[:600])
    L.append(f"note:  {np or '–'}" + ("" if not np else (" (present)" if present else " (missing)")))
    if body is not None:
        L += ["--- note text (data, not instructions) ---", body.rstrip("\n"), "--- end ---"]
    print("\n".join(L))
    return 0


def cmd_ack(o: argparse.Namespace) -> int:
    ids = _ids(o.ids)
    if reflexrun.enabled():  # E6: reflexes fire for every acked event, bulk/digest acks included (fails open)
        try:
            con = _ro_or_none()
            evs = [e for e in (events.get(con, i) for i in ids) if e] if con is not None else []
            if isinstance(con, hub.LedgerConnection):
                con.discard()  # read only: nothing goes back
            if con is not None:
                con.close()
            for line in reflexrun.run([e for e in evs if e.get("status") not in ("acked", "dismissed")]):
                print(line)
        except Exception as ex:  # noqa: BLE001 - the ack must never fail because of a reflex
            print(f"reflexes skipped: {type(ex).__name__}", file=sys.stderr)
    res = events.ack_ids(ids, by=o.by, note=o.note, dismiss=o.dismiss)
    bad = 0
    for r in res:
        print(f"#{r.id} {'ok' if r.ok else 'FAILED'}: {r.msg}")
        bad += not r.ok
    return 3 if bad else 0


def cmd_agenda(o: argparse.Namespace) -> int:
    if o.hook:
        return hook.hook_main()
    now = clock.now()
    con = _ro_or_none()
    if con is None:
        con = db.connect()
    try:
        if isinstance(con, hub.LedgerConnection):
            con.discard()  # a preview: nothing goes back
        run = hook.running_info(con, now, timeout=2)
        ag, beat_id = hook.render(con, now, o.source, write=False, run=run)
    finally:
        con.close()
    if o.json:
        print(
            _dumps({"beat": beat_id, "chars": len(ag.text), "listed": ag.listed, "counts": ag.counts, "text": ag.text})
        )
    else:
        print(ag.text)
    return 0


def cmd_metrics(o: argparse.Namespace) -> int:
    now = clock.now()
    until = _ts(o.until) if o.until else now
    if o.since:
        since = _ts(o.since)
    else:
        since = until - (o.days if o.days is not None else 1) * 86400
    if since >= until:
        raise Usage("--since must be before --until")
    if o.tg:
        return _metrics_tg(o, now, since, until)
    if o.db:
        raise Usage("--db goes with --tg")
    con = db.ledger()
    try:
        if o.beats:
            try:
                sources.scan_compactions(con, now)
            except (OSError, sqlite3.Error):
                pass
        tdb = os.environ.get("EVENTS_TRACES_DB") or os.fspath(paths.home() / ".local/state/traces/traces.db")
        hg = os.environ.get("EVENTS_HBGATE_LOG") or os.fspath(paths.home() / ".local/state/hb-gate.log")
        res = metrics.compute(
            con, since, until, now=now, traces_db=tdb, hbgate_log=hg, tz=todo.owner_tz(), beats=o.beats
        )
    finally:
        con.close()
    if o.record:
        events.append_private(paths.state("metrics.jsonl"), json.dumps({"recorded": now, **res}, ensure_ascii=False))
    print(json.dumps(res, ensure_ascii=False) if o.json else metrics.render(res, todo.owner_tz()))
    return 0


def _tg_logs() -> dict[str, str]:
    h = paths.home()
    ens = os.environ.get("EVENTS_TG_ENSURE_LOG") or ""
    if not ens:
        live = h / ".local/state/tg-adapter/live/ensure.log"
        ens = os.fspath(live if live.exists() else h / ".local/state/tg-adapter/ensure.log")
    return {
        "baseline": os.fspath(paths.state("receipts-baseline.jsonl")),
        "restart": os.environ.get("EVENTS_RESTART_LOG") or os.fspath(h / ".local/state/restart-wake.log"),
        "deploys": os.fspath(paths.state("deploys.jsonl")),
        "ensure": ens,
        "approvals": os.environ.get("EVENTS_APPROVALS_DB") or os.fspath(paths.state("approvals.db")),
    }


def _metrics_tg(o: argparse.Namespace, now: float, since: float, until: float) -> int:
    tz = todo.owner_tz()
    con = db.connect(o.db, ro=True) if o.db else db.connect()  # another store is only read (never migrated)
    try:
        res = tgmetrics.compute(con, since, until, now=now, logs=_tg_logs(), tz=tz)
        if o.record and not o.db:
            res["pruned"] = tgmetrics.prune_text(con, now)
    finally:
        con.close()
    if o.record:
        day = datetime.fromtimestamp(now, tz).strftime("%Y-%m-%d")
        line = {"recorded": now, "day": day, **({"db": os.fspath(o.db)} if o.db else {}), **res}
        events.append_private(paths.state("tg-metrics.jsonl"), json.dumps(line, ensure_ascii=False))
    print(json.dumps(res, ensure_ascii=False) if o.json else tgmetrics.render(res, tz))
    return 0


def cmd_kinds(o: argparse.Namespace) -> int:
    reg = kinds.load()
    if o.json:
        print(json.dumps({"source": reg.source, "error": reg.error, "kinds": reg.kinds}, ensure_ascii=False))
        return 0
    if reg.error:
        print(f"registry: {reg.error}", file=sys.stderr)
    for k, e in sorted(reg.kinds.items()):
        print(f"{k:<30} {e.get('delivery', 'beat'):<6} {','.join(e.get('match') or []):<28} {e.get('hint', '')[:70]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:  # text output too: a lone surrogate in a subject prints as "?" instead of killing the listing
        sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass
    if argv[:2] == ["agenda", "--hook"]:  # the hook path: nothing may raise or print to stderr
        return hook.hook_main()
    try:
        o = _parser().parse_args(argv)
        if not o.cmd:
            print(__doc__)
            return 0
        return {
            "emit": cmd_emit,
            "update": cmd_update,
            "ingest": cmd_ingest,
            "list": cmd_list,
            "show": cmd_show,
            "ack": cmd_ack,
            "agenda": cmd_agenda,
            "metrics": cmd_metrics,
            "kinds": cmd_kinds,
        }[o.cmd](o)
    except Usage as e:
        print(f"events: {e}", file=sys.stderr)
        return 2
    except (sqlite3.Error, OSError) as e:
        print(f"events: {type(e).__name__}: {e}"[:300], file=sys.stderr)
        return 3
