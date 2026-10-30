"""hubkit.wf: `hub wf …`, the brain's handle on the hub's workflow engine (DBOS in hubd). Stdlib only.

  hub wf status [--json]               the engine (state, switch, version, counts) and the watch schedules
  hub wf engine on|off [--why TEXT]    the engine's switch (persisted in the hub's state; off by default). off keeps
                                       pending workflows in Postgres; they resume at the next `on`
  hub wf probe [--ttl S]               smoke test: start hub.probe, send it a ping, wait for SUCCESS (exit 0)
  hub wf ls [--name N] [--status S] [--limit N] [--json] · hub wf show ID [--json]
  hub wf start NAME [--args JSON] [--id ID] · hub wf send ID TOPIC [JSON] [--key K] · hub wf cancel ID
  hub wf watches shadow|live|paused|off [--jobs a,b] [--force] [--dry-run]
                                       the watch jobs (core.watchjobs) as hub schedules: shadow = they fire
                                       and the brain's executor only says what it would run; live = they run (refused
                                       while a job's cron line is still in ~/crontab, unless --force: it would run
                                       twice); paused; off = removed
  hub wf cron [--json]                 the ~/crontab lines the watch schedules replace (none left = live is safe), and
                                       the registry check (core/schedules.toml): lines it does not know
                                       (untracked), registry lines missing from ~/crontab, lines that differ

Exit: 0 ok · 1 failed or not confirmed · 2 usage or refused · 3 hub unreachable · 4 engine off.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any
from urllib.parse import quote, urlencode

from core import hub, paths, schedules, watchjobs

POLL = 0.5


def add_parser(sub: Any) -> None:
    p = sub.add_parser("wf", help="the hub's workflow engine (DBOS)")
    w = p.add_subparsers(dest="wf_cmd", required=True)
    s = w.add_parser("status")
    s.add_argument("--json", action="store_true")
    s = w.add_parser("engine")
    s.add_argument("on_off", choices=("on", "off"))
    s.add_argument("--why", default="")
    s = w.add_parser("probe")
    s.add_argument("--ttl", type=int, default=30)
    s = w.add_parser("ls")
    s.add_argument("--name")
    s.add_argument("--status")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--json", action="store_true")
    s = w.add_parser("show")
    s.add_argument("id")
    s.add_argument("--json", action="store_true")
    s = w.add_parser("start")
    s.add_argument("name")
    s.add_argument("--args", default="{}")
    s.add_argument("--id")
    s = w.add_parser("send")
    s.add_argument("id")
    s.add_argument("topic")
    s.add_argument("message", nargs="?", default="null")
    s.add_argument("--key")
    s = w.add_parser("cancel")
    s.add_argument("id")
    s = w.add_parser("watches")
    s.add_argument("mode", choices=("shadow", "live", "paused", "off"))
    s.add_argument("--jobs", default="")
    s.add_argument("--force", action="store_true")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--why", default="")
    s = w.add_parser("cron")
    s.add_argument("--json", action="store_true")


def _wid(w: str) -> str:
    return quote(w, safe=":._+-")  # a schedule's run ids carry "+hh:mm" (a path keeps "+" literally)


def _crontab() -> str:
    try:
        return paths.guard(paths.home() / "crontab").read_text(encoding="utf-8")
    except OSError:
        return ""


def cron_left(jobs: list[str]) -> dict[str, list[tuple[int, str]]]:
    lines = watchjobs.cron_lines(_crontab())
    return {j: lines.get(j) or [] for j in jobs if lines.get(j)}


def registry_check() -> dict[str, Any]:
    """The crontab against the schedule registry: counts + the untracked / missing / changed lines."""
    try:
        p = schedules.parity(_crontab(), schedules.load())
    except schedules.RegistryError as e:
        return {"error": str(e)}
    return {"active": p.active, "tagged": p.tagged, "untracked": [{"line": n, "text": t[:160]} for n, t in p.untracked],
            "missing": p.missing, "changed": [{"line": n, "tag": t, "why": w} for n, t, w in p.changed]}  # fmt: skip


def _changed(jobs: list[str], mode: str, why: str) -> None:
    """Every schedule change shows in the agenda (schedule.changed, digest); never fails the change."""
    import os
    import subprocess

    ev = os.environ.get("JOB_EVENTS") or str(paths.home() / "bin" / "events")
    for j in jobs:
        body = {"type": "schedule.changed", "v": 1, "job": j, "state": mode, "why": why[:200]}
        try:
            subprocess.run([ev, "emit", "--kind", "schedule.changed", "--subject", f"schedule {j}: {mode} ({why[:60]})",
                            "--source", "hub wf", "--delivery", "digest", "--no-note", "--payload", json.dumps(body)],
                           capture_output=True, timeout=20, check=False)  # fmt: skip
        except (OSError, subprocess.SubprocessError):
            pass


def _jobs(arg: str) -> list[str]:
    if not arg:
        return list(watchjobs.JOBS)
    js = [j.strip() for j in arg.split(",") if j.strip()]
    bad = [j for j in js if j not in watchjobs.JOBS]
    if bad:
        raise SystemExit(f"hub wf: unknown job(s) {', '.join(bad)} (known: {', '.join(watchjobs.JOBS)})")
    return js


def _line(r: dict[str, Any]) -> str:
    out = f"{r.get('id')}  {r.get('name')}  {r.get('status')}"
    if r.get("output") is not None:
        out += f"  → {json.dumps(r['output'], ensure_ascii=False)[:160]}"
    if r.get("error"):
        out += f"  ({r['error']})"
    return out


def _status(as_json: bool) -> int:
    eng = hub.call("GET", "/wf/engine", timeout=30.0)
    sch = hub.call("GET", "/wf/schedules?runs=1", timeout=30.0).get("rows") if eng.get("state") == "on" else None
    if as_json:
        print(json.dumps({"engine": eng, "schedules": sch}, sort_keys=True))
        return 0 if eng.get("state") in ("on", "off") else 1
    c = eng.get("counts") or {}
    print(
        f"engine: {eng.get('state')} (switch {eng.get('switch')}{', ' + eng['why'] if eng.get('why') else ''})"
        f" · version {eng.get('version') or '-'} · schema {eng.get('schema')}"
        + (f" · pending {c.get('pending')}, queued {c.get('enqueued')}, errors {c.get('error')}" if c else "")
    )
    cu = eng.get("catch_up") or {}
    if cu.get("run") or cu.get("late") or cu.get("errors") or cu.get("error"):
        names = lambda k: ", ".join(cu.get(k) or []) or "-"  # noqa: E731
        err = cu.get("error") or (names("errors") if cu.get("errors") else "")
        print(f"  catch-up at the last start: ran {names('run')} · too late {names('late')}"
              + (f" · errors {err}" if err else ""))  # fmt: skip
    for s in sch or []:
        last = (s.get("runs") or [{}])[0]
        print(
            f"  {s.get('name')}: {s.get('status')} {s.get('mode')} '{s.get('cron')}' {s.get('tz') or ''}"
            f" · last {last.get('status') or '-'}"
        )
    if sch == []:
        print("  no watch schedules")
    return 0 if eng.get("state") in ("on", "off") else 1


def _probe(ttl: int) -> int:
    r = hub.call("POST", "/wf/start", {"name": "hub.probe", "args": {"ttl": ttl}}, timeout=30.0)
    wid = r["id"]
    hub.call("POST", f"/wf/{_wid(wid)}/send", {"topic": "ping", "message": "ping"}, timeout=30.0)
    end = time.monotonic() + ttl + 10
    while time.monotonic() < end:
        st = hub.call("GET", f"/wf/{_wid(wid)}", timeout=30.0)
        if st.get("status") not in ("PENDING", "ENQUEUED"):
            ok = st.get("status") == "SUCCESS" and (st.get("output") or {}).get("got") is True
            print(("probe ok: " if ok else "probe failed: ") + _line(st))
            return 0 if ok else 1
        time.sleep(POLL)
    print(f"probe: {wid} still pending after {ttl + 10} s")
    return 1


def _watches(mode: str, jobs: list[str], force: bool, dry: bool, why: str = "") -> int:
    if mode == "live":
        left = cron_left(jobs)
        if left and not force:
            print("hub wf watches live: refused, these cron lines still run the same jobs (they would run twice):")
            for j, ls in left.items():
                for n, line in ls:
                    print(f"  {j}: ~/crontab:{n}: {line[:160]}")
            print("remove them first (the main session: `assistant cron` / ~/crontab edit), or --force")
            return 2
    tz = watchjobs.local_tz()
    rc = 0
    for j in jobs:
        name = f"watch:{j}"
        if dry:
            print(f"{name}: would be {mode} ('{watchjobs.JOBS[j].cron}' {tz})")
            continue
        try:
            if mode in ("shadow", "live"):
                body: dict[str, Any] = {"name": name, "job": j, "cron": watchjobs.JOBS[j].cron, "tz": tz, "mode": mode}
                queue = watchjobs.JOBS[j].queue
                if queue:
                    body["queue"] = queue
                e = schedules.load().entry(j)
                if e is not None and e.catch_up is not None:
                    body["catch_up"] = e.catch_up  # the registry's; none = the hub's default by cadence
                r = hub.call("POST", "/wf/schedules", body, timeout=30.0)
            else:
                r = hub.call("POST", f"/wf/schedules/{name}/{'pause' if mode == 'paused' else 'delete'}", {},
                             timeout=30.0)  # fmt: skip
            print(f"{name}: {r.get('status')} {r.get('mode') or ''}".rstrip())
        except hub.Refused as e:
            if mode == "off" and getattr(e, "code", 0) == 404:
                print(f"{name}: absent")
                continue
            print(f"{name}: refused ({e})")
            rc = 1
    if not dry:
        _changed(jobs, mode, why or f"hub wf watches {mode}")
    return rc


def main(a: argparse.Namespace) -> int:
    c = a.wf_cmd
    try:
        if c == "status":
            return _status(a.json)
        if c == "engine":
            r = hub.call("POST", "/wf/engine", {"on": a.on_off == "on", "why": a.why or f"brain: {a.on_off}"},
                         timeout=60.0)  # fmt: skip
            print(f"engine: {r.get('state')} (switch {r.get('switch')}{', ' + r['why'] if r.get('why') else ''})")
            return 0 if r.get("state") == ("on" if a.on_off == "on" else "off") else 1
        if c == "cron":
            left = cron_left(list(watchjobs.JOBS))
            reg = registry_check()
            if a.json:
                out: dict[str, Any] = {j: [{"line": n, "text": t} for n, t in ls] for j, ls in left.items()}
                out["_registry"] = reg
                print(json.dumps(out))
                return 0
            for j in watchjobs.JOBS:
                for n, t in left.get(j, []):
                    print(f"{j}: ~/crontab:{n}: {t[:160]}")
                if not left.get(j):
                    print(f"{j}: no cron line")
            if "error" in reg:
                print(f"registry: unreadable ({reg['error']})")
                return 1
            print(f"registry: {reg['active']} active lines · {reg['tagged']} tagged · {len(reg['untracked'])} untracked"
                  f" · {len(reg['missing'])} missing · {len(reg['changed'])} changed (core/schedules.toml)")  # fmt: skip  # noqa: E501
            for u in reg["untracked"]:
                print(f"  untracked ~/crontab:{u['line']}: {u['text'][:100]}")
            for m in reg["missing"]:
                print(f"  missing: {m} (registry runs_on cron, no tagged line)")
            for c in reg["changed"]:
                print(f"  changed ~/crontab:{c['line']} ({c['tag']}): {c['why']}")
            return 0
        if c == "watches":
            return _watches(a.mode, _jobs(a.jobs), a.force, a.dry_run, a.why)
        if c == "probe":
            return _probe(max(1, min(600, a.ttl)))
        if c == "ls":
            q = {k: v for k, v in (("name", a.name), ("status", a.status), ("limit", str(a.limit))) if v}
            rows = hub.call("GET", "/wf?" + urlencode(q), timeout=30.0).get("rows") or []
            print(json.dumps(rows) if a.json else ("\n".join(_line(r) for r in rows) or "no workflows"))
            return 0
        if c == "show":
            r = hub.call("GET", f"/wf/{_wid(a.id)}", timeout=30.0)
            print(
                json.dumps(r, sort_keys=True)
                if a.json
                else _line(r)
                + "".join(
                    f"\n  {k}: {json.dumps(v, ensure_ascii=False)[:200]}" for k, v in (r.get("events") or {}).items()
                )
            )
            return 0
        if c == "start":
            body: dict[str, Any] = {"name": a.name, "args": json.loads(a.args)}
            if a.id:
                body["id"] = a.id
            print(_line(hub.call("POST", "/wf/start", body, timeout=30.0)))
            return 0
        if c == "send":
            body = {"topic": a.topic, "message": json.loads(a.message)}
            if a.key:
                body["key"] = a.key
            hub.call("POST", f"/wf/{_wid(a.id)}/send", body, timeout=30.0)
            print(f"sent to {a.id} on {a.topic}")
            return 0
        if c == "cancel":
            print(_line(hub.call("POST", f"/wf/{_wid(a.id)}/cancel", {}, timeout=30.0)))
            return 0
    except hub.HubDown as e:
        print(f"hub wf: hub unreachable ({e})", file=sys.stderr)
        return 3
    except hub.Refused as e:
        print(f"hub wf: {e}", file=sys.stderr)
        return 4 if getattr(e, "code", 0) == 503 else 2
    except ValueError as e:
        print(f"hub wf: {e}", file=sys.stderr)
        return 2
    return 2
