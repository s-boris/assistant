"""rotate: the brain's nightly session rotation. Off unless ~/.config/assistant/rotate-on exists.

  rotate status [--json]          switch, window, armed flag, start-claude fresh-start block, last rotation and verify
  rotate check [--json]           the quiet-slot checks now (read-only; exit 0 all pass, 3 not now)
  rotate handoff                  print the hand-off a rotation would write now (read-only)
  rotate run [--dry-run] [--cron] a rotation when the switch is on and every check passes: snapshot the promises,
                                  write ~/inbox/rotation-<id>.md, arm the fresh-start flag, `restart "…"`.
                                  --dry-run: checks + hand-off + the steps, nothing written. --cron: quiet when off
                                  or outside the window, "not now" exits 0, and a pending verify runs first.
  rotate verify [--id ID] [--json]  after a rotation: each promise durable | passed | recreated | changed | lost
  rotate report [--json] [--n N]  the last N rotations (default 7): promises, lost, hand-off read (done-when)
  rotate rehearse [--model-check --yes] [--json]   the whole path without touching the session: checks, promises,
                                  hand-off and its completeness into ~/jobs/rotate-rehearsal/<id>/; --model-check asks
                                  a scratch `claude -p` to list the promises back from the hand-off alone
  rotate on --reason TEXT [--force] | off [--reason TEXT]   the switch (the owner's standing OK); off also disarms
The fresh session comes from start-claude: its fresh-start block skips `--continue` once when the flag
(~/.local/state/assistant/rotate/fresh-start) is under 10 minutes old, and removes it. Logs (runs.jsonl) hold
counts, check names and ids only.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from core import clock, jsonstate, notes, paths
from rotation import checks, handoff

EXIT_NOT_NOW = 3
VERIFY_AFTER_S = 15 * 60


def _rid(now: float) -> str:
    return time.strftime("%Y%m%d-%H%M", time.localtime(now))


def log(row: dict[str, Any]) -> None:
    f = checks.runs_file()
    f.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, separators=(",", ":")) + "\n")
    os.chmod(f, 0o600)


def rows() -> list[dict[str, Any]]:
    try:
        with open(checks.runs_file(), encoding="utf-8") as fh:
            return [json.loads(ln) for ln in fh if ln.strip()]
    except (OSError, ValueError):
        return []


def _out(obj: Any, as_json: bool, text: str) -> None:
    print(json.dumps(obj) if as_json else text)


def _check_lines(res: list[dict[str, Any]]) -> list[str]:
    mark = {True: "ok  ", False: "NO  ", None: "??  "}
    return [f"  {mark[r['ok']]}{r['check']:<13} {r['why']}" for r in res]


# ---------------------------------------------------------------- status / check / handoff


def cmd_status(a: argparse.Namespace) -> int:
    sw = checks.switch_file()
    flag = checks.fresh_flag()
    ok_sc, why_sc = checks.c_start_claude({})
    last = checks.last_run()
    ver = [r for r in rows() if r.get("op") == "verify"][-1:]
    d = {
        "switch": sw.exists(),
        "config": checks.config(),
        "armed": flag.exists(),
        "start_claude_fresh_start": ok_sc,
        "last_rotation": last,
        "last_verify": ver[0] if ver else None,
    }
    lines = [
        f"rotate: {'ON' if d['switch'] else 'off'} · window {d['config']['window']} his time · "
        f"fresh-start flag {'ARMED' if d['armed'] else 'not armed'} · start-claude: {why_sc}",
        f"last rotation: {last.get('id') if last else 'none'}"
        + (f" ({last.get('promises')} promises, previous session {last.get('old_session')})" if last else ""),
        f"last verify: {ver[0].get('id')}: lost {ver[0].get('lost')}, hand-off read {ver[0].get('handoff_read')}"
        if ver
        else "last verify: none",
    ]
    _out(d, a.json, "\n".join(lines))
    return 0


def cmd_check(a: argparse.Namespace) -> int:
    ctx = checks.context()
    res = checks.run_all(ctx)
    ok = checks.passed(res)
    _out(
        {"ok": ok, "checks": res},
        a.json,
        ("quiet slot: yes" if ok else "quiet slot: not now") + "\n" + "\n".join(_check_lines(res)),
    )
    return 0 if ok else EXIT_NOT_NOW


def cmd_handoff(a: argparse.Namespace) -> int:
    ctx = checks.context()
    rid = _rid(ctx["now"])
    ps = handoff.promises(ctx)
    title, body = handoff.text(ctx, rid, ps)
    print(notes.compose(title, body, handoff.NEXT, source="rotate"), end="")
    return 0


# ---------------------------------------------------------------- run


def _restart(reason: str) -> int:
    seam = os.environ.get("ROTATE_RESTART", "")
    argv = shlex.split(seam) if seam else [os.fspath(paths.bin_dir("restart"))]
    try:
        return subprocess.run([*argv, reason], timeout=60, check=False, stdin=subprocess.DEVNULL).returncode
    except (OSError, subprocess.SubprocessError):
        return -1


def _pending_verify(now: float) -> dict[str, Any] | None:
    last = checks.last_run()
    if not last or now - float(last.get("at") or 0) < VERIFY_AFTER_S:
        return None
    if any(r.get("op") == "verify" and r.get("id") == last.get("id") for r in rows()):
        return None
    return last


def cmd_run(a: argparse.Namespace) -> int:
    now = clock.now()
    if a.cron and not a.dry_run:
        pend = _pending_verify(now)
        if pend:
            do_verify(pend, now, quiet=True)
        if not checks.switch_file().exists():
            return 0
    ctx = checks.context()
    res = checks.run_all(ctx, skip=("switch",) if a.dry_run else ())
    ok = checks.passed(res)
    failed = [r["check"] for r in res if r["ok"] is not True]
    rid = _rid(ctx["now"])
    if a.cron and "window" in failed:
        return 0  # outside the window: nothing to log
    if a.dry_run:
        ps = handoff.promises(ctx)
        title, body = handoff.text(ctx, rid, ps)
        print(
            ("quiet slot: yes" if ok else f"quiet slot: not now ({', '.join(failed)})")
            + "\n"
            + "\n".join(_check_lines(res))
        )
        print(f"\nwould: snapshot {len(ps)} promises -> {checks.state_dir() / f'promises-{rid}.json'}")
        print(f"would: write ~/inbox/{handoff.note_name(rid)} (below)")
        print(f'would: arm {checks.fresh_flag()} and run `restart "nightly rotation…"`')
        print("\n" + notes.compose(title, body, handoff.NEXT, source="rotate"), end="")
        log({"op": "rotate", "dry": True, "at": int(ctx["now"]), "ok": ok, "failed": failed, "promises": len(ps)})
        return 0 if ok else EXIT_NOT_NOW
    if not ok:
        log({"op": "rotate", "at": int(ctx["now"]), "ok": False, "failed": failed})
        if not a.cron:
            print("not now: " + ", ".join(failed) + "\n" + "\n".join(_check_lines(res)))
        return 0 if a.cron else EXIT_NOT_NOW
    return rotate_now(ctx, rid)


def rotate_now(ctx: dict[str, Any], rid: str) -> int:
    ps = handoff.promises(ctx)
    snap = handoff.snapshot(ps)
    sd = checks.state_dir()
    sd.mkdir(parents=True, exist_ok=True, mode=0o700)
    jsonstate.save(sd / f"promises-{rid}.json", snap, mode=0o600)
    title, body = handoff.text(ctx, rid, ps)
    notes.write(
        paths.inbox(), handoff.note_name(rid), notes.compose(title, body, handoff.NEXT, source="rotate"), mode=0o600
    )
    old = str((ctx.get("session") or {}).get("sessionId") or "")
    jsonstate.save(checks.fresh_flag(), {"id": rid, "at": int(ctx["now"])}, mode=0o600)
    reason = f"nightly rotation: a fresh session; hand-off in ~/inbox/{handoff.note_name(rid)}"
    rc = _restart(reason)
    row = {
        "op": "rotate",
        "at": int(ctx["now"]),
        "id": rid,
        "old_session": old,
        "promises": len(snap),
        "crons": sum(1 for p in snap if p["kind"] == "cron"),
    }
    if rc != 0:
        checks.fresh_flag().unlink(missing_ok=True)
        log(dict(row, ok=False, failed=["restart"], rc=rc))
        print(f"restart failed (exit {rc}); flag removed, the session goes on", file=sys.stderr)
        return 1
    log(dict(row, ok=True))
    print(f"rotation {rid}: restart requested; hand-off ~/inbox/{handoff.note_name(rid)}")
    return 0


# ---------------------------------------------------------------- verify / report


def do_verify(rot: dict[str, Any], now: float, quiet: bool = False, as_json: bool = False) -> int:
    f = checks.state_dir() / f"promises-{rot.get('id')}.json"
    snap = jsonstate.load(f, None)
    if not isinstance(snap, list):
        if not quiet:
            print(f"no promise snapshot for {rot.get('id')}", file=sys.stderr)
        return 1
    res = handoff.verify(snap, rot, now)
    row = {
        "op": "verify",
        "at": int(now),
        "id": rot.get("id"),
        **{k: v for k, v in res.items() if k != "new_session"},
        "new_session": res.get("new_session"),
    }
    log(row)
    if not quiet:
        _out(
            row,
            as_json,
            f"verify {rot.get('id')}: {res['promises']} promises · durable {res['durable']} · passed "
            f"{res['passed']} · recreated {res['recreated']} · changed {res['changed']} · LOST {res['lost']}"
            f" · hand-off read {'yes' if res['handoff_read'] else 'no'} · rotated {'yes' if res['rotated'] else 'no'}",
        )
    return 0 if res["lost"] == 0 and res["rotated"] else 1


def cmd_verify(a: argparse.Namespace) -> int:
    rots = [r for r in rows() if r.get("op") == "rotate" and r.get("ok") and not r.get("dry")]
    rot = next((r for r in reversed(rots) if r.get("id") == a.id), None) if a.id else (rots[-1] if rots else None)
    if not rot:
        print("no rotation to verify", file=sys.stderr)
        return 1
    return do_verify(rot, clock.now(), as_json=a.json)


def cmd_report(a: argparse.Namespace) -> int:
    rs = rows()
    rots = [r for r in rs if r.get("op") == "rotate" and r.get("ok") and not r.get("dry")][-a.n :]
    ver = {r.get("id"): r for r in rs if r.get("op") == "verify"}
    tried = [r for r in rs if r.get("op") == "rotate" and not r.get("ok") and not r.get("dry")]
    out = []
    for r in rots:
        v = ver.get(r.get("id")) or {}
        out.append(
            {
                "id": r.get("id"),
                "promises": r.get("promises"),
                "crons": r.get("crons"),
                "verified": bool(v),
                "lost": v.get("lost"),
                "handoff_read": v.get("handoff_read"),
            }
        )
    lost = sum(int(x["lost"] or 0) for x in out if x["verified"])
    d = {
        "rotations": out,
        "verified": sum(1 for x in out if x["verified"]),
        "lost": lost,
        "skipped_nights": len(tried),
        "done_when": len(out) >= 7 and all(x["verified"] for x in out) and lost == 0,
    }
    lines = [
        f"last {len(out)} rotations: {d['verified']} verified, {lost} promises lost "
        f"(done when 7 verified with 0 lost: {'yes' if d['done_when'] else 'not yet'})"
    ]
    lines += [
        f"  {x['id']}: {x['promises']} promises ({x['crons']} check-ins) · "
        + (f"lost {x['lost']} · hand-off read {x['handoff_read']}" if x["verified"] else "not verified")
        for x in out
    ]
    _out(d, a.json, "\n".join(lines))
    return 0


# ---------------------------------------------------------------- rehearsal


def cmd_rehearse(a: argparse.Namespace) -> int:
    ctx = checks.context()
    rid = _rid(ctx["now"])
    res = checks.run_all(ctx)
    ps = handoff.promises(ctx)
    title, body = handoff.text(ctx, rid, ps)
    note = notes.compose(title, body, handoff.NEXT, source="rotate")
    root = Path(os.environ.get("ROTATE_REHEARSAL_DIR") or paths.home() / "jobs" / "rotate-rehearsal") / rid
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    jsonstate.write_text(root / "handoff.md", note, mode=0o600)
    jsonstate.save(root / "snapshot.json", handoff.snapshot(ps), mode=0o600)
    timed = [p for p in ps if p["kind"] in ("todo", "remind")]
    crons = [p for p in ps if p["kind"] == "cron"]
    labels = [f"[p{n}]" for n in range(1, len(timed) + 1)] + [f"[c{n}]" for n in range(1, len(crons) + 1)]
    carried = sum(1 for lb in labels if lb in note)
    d: dict[str, Any] = {
        "id": rid,
        "dir": os.fspath(root),
        "checks": res,
        "would_rotate": checks.passed(res),
        "promises": len(labels),
        "carried": carried,
        "note_chars": len(note),
    }
    if a.model_check:
        if not a.yes:
            print("--model-check runs one `claude -p` turn on the subscription; add --yes", file=sys.stderr)
            return 2
        d["model_check"] = model_check(root, note, labels)
    log(
        {
            "op": "rehearse",
            "at": int(ctx["now"]),
            "id": rid,
            "promises": len(labels),
            "carried": carried,
            "would_rotate": d["would_rotate"],
            **({"recalled": d["model_check"].get("recalled")} if a.model_check else {}),
        }
    )
    lines = [
        f"rehearsal {rid} (nothing live touched): {d['dir']}",
        f"would rotate now: {'yes' if d['would_rotate'] else 'no'}",
        *_check_lines(res),
        f"promises {len(labels)}, carried in the hand-off {carried} ({len(note)} chars)",
    ]
    if a.model_check:
        mc = d["model_check"]
        lines.append(
            f"model check: recalled {mc.get('recalled')}/{len(labels)} labels"
            + (f" ({mc['error']})" if mc.get("error") else "")
        )
    _out(d, a.json, "\n".join(lines))
    ok = carried == len(labels) and (not a.model_check or d["model_check"].get("recalled") == len(labels))
    return 0 if ok else 1


def model_check(root: Path, note: str, labels: list[str]) -> dict[str, Any]:
    """A scratch `claude -p` (from the rehearsal folder, project settings only, no MCP) reads the hand-off and lists
    the bracketed labels of every promise and check-in; recall = labels it names."""
    exe = os.environ.get("ROTATE_CLAUDE") or shutil.which("claude") or os.fspath(paths.home() / ".local/bin/claude")
    prompt = (
        note + "\n\n---\nYou just started as a fresh session and this hand-off is all you know. List the "
        "bracketed label of every promise and every check-in you must keep (like [p1] or [c1]), one per line, "
        "nothing else."
    )
    jsonstate.write_text(root / "CLAUDE.md", "", mode=0o600)
    try:
        r = subprocess.run(
            [
                exe,
                "-p",
                prompt,
                "--model",
                os.environ.get("ROTATE_MODEL", "claude-opus-5-5"),
                "--effort",
                os.environ.get("ROTATE_EFFORT", "low"),  # label recall: low = high on 6 cases (effort report 29.10)
                "--max-turns",
                "1",
                "--setting-sources",
                "project,local",
                "--strict-mcp-config",
                "--output-format",
                "json",
            ],
            cwd=root,
            env=dict(os.environ, ENABLE_CLAUDEAI_MCP_SERVERS="false"),
            capture_output=True,
            text=True,
            timeout=240,
            check=False,
            stdin=subprocess.DEVNULL,
        )
        out = json.loads(r.stdout).get("result", "") if r.returncode == 0 else ""
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        return {"recalled": 0, "error": type(e).__name__}
    return {"recalled": sum(1 for lb in labels if lb in str(out))}


# ---------------------------------------------------------------- switch


def cmd_on(a: argparse.Namespace) -> int:
    if len((a.reason or "").strip()) < 8:
        print("rotate on: --reason needed (8+ characters: whose OK, when)", file=sys.stderr)
        return 2
    ok, why = checks.c_start_claude({})
    if ok is not True and not a.force:
        print(f"rotate on: refused, {why} (or --force)", file=sys.stderr)
        return 1
    jsonstate.save(checks.switch_file(), {"at": int(clock.now()), "reason": a.reason.strip()[:200]}, mode=0o600)
    log({"op": "switch", "at": int(clock.now()), "on": True})
    print("rotate: ON (nightly, his window " + checks.config()["window"] + "); off: rotate off")
    return 0


def cmd_off(a: argparse.Namespace) -> int:
    checks.switch_file().unlink(missing_ok=True)
    checks.fresh_flag().unlink(missing_ok=True)
    log({"op": "switch", "at": int(clock.now()), "on": False})
    print("rotate: off (flag disarmed)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="rotate", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("status", "check", "verify", "report"):
        s = sub.add_parser(name)
        s.add_argument("--json", action="store_true")
        if name == "verify":
            s.add_argument("--id")
        if name == "report":
            s.add_argument("--n", type=int, default=7)
    sub.add_parser("handoff")
    s = sub.add_parser("run")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--cron", action="store_true")
    s = sub.add_parser("rehearse")
    s.add_argument("--model-check", action="store_true")
    s.add_argument("--yes", action="store_true")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("on")
    s.add_argument("--reason", default="")
    s.add_argument("--force", action="store_true")
    s = sub.add_parser("off")
    s.add_argument("--reason", default="")
    a = ap.parse_args(argv)
    fn = {
        "status": cmd_status,
        "check": cmd_check,
        "handoff": cmd_handoff,
        "run": cmd_run,
        "verify": cmd_verify,
        "report": cmd_report,
        "rehearse": cmd_rehearse,
        "on": cmd_on,
        "off": cmd_off,
    }[a.cmd]
    return fn(a)
