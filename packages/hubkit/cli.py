"""hub: the claude-hub stack as the brain sees it. `hub --help`.

  hub status [--json]       stack + local view: reachable?, running release vs CURRENT, last good, bad attempts, checks
                            (pg, phoenix, ci), kit installed vs rendered, loader hash, the brain's token (whoami)
  hub health [--json] [--wait S]
                            /health only: 0 ok · 1 degraded (a check not ok, a release other than CURRENT, a bad
                            attempt) · 3 unreachable or not installed
  hub kit show [--json]     the rendered kit: id, loader hash, rev, profile source, paste lines, credential names
  hub kit render [--rev REV]   render the kit from a git archive of the repo's REV (default main); no release
  hub kit paste [install|rollback|tg-install|tg-rollback|voice-install|voice-rollback|voice-relogin|phone-install|
                 phone-rollback|browser-install|browser-rollback|browser-recopy]
                                         print a console paste (install also updates; tg-*: the
                            Telegram adapter into the hub and back; voice-relogin: the call account's login
                            inside claude-hub-voice, docs/runbooks/voice-relogin.md); never a secret
  hub kit card              print the owner's step card
  hub kit apply-request PART [--no-approval] [--json]   one-tap kit apply (dispatcher 3.3; docs/runbooks/kit-apply.md):
                            the server reviews PART (tg voice phone phoenix ci tg-off voice-off) and decides the tier:
                            0 = no tap, 1 = his card (an approval for exactly the server's facts is minted; the payload
                            has the row to send), paste = the reasons + which paste; exit 3 for paste
  hub kit apply PART --facts SHA [--nonce N] [--json]   the server re-reviews, checks his tap itself and applies
  hub kit apply-log         the server's audit lines (reviews, applies, refusals)
  hub feed [show|ensure|push|watch|unwatch|status|off|on]   the hub's status feed (hubkit.feed has the details):
                            the snapshot, the tunnel to the web VM + the brain's report (cron), test watch parts,
                            the kill switch
  hub voice status|stop|start|inbox   the hub's voice relay: hold, release, the hand-off helper
  hub phone status|stop|start|token|inbox   the hub's phone gateway: hold, release, the CLI token, the inbox
  hub tg status|stop|start  the hub's Telegram adapter: stop = a hold tgloader honours (adapter down, stays down
                            across restarts) until start; hubkit.tg has the details
  hub secrets status|pull|paste   the hub's secret store and redaction counts, the fingerprint export for
                            secretscan, the owner's copy-by-path paste (rendered, never run); hubkit.secrets has the
                            details
  hub egress status|check|use|endpoints   the hub's egress injector (placeholder handles, audit then
                            enforce), the brain-side mode per handle, the owner-pasted endpoints file; hubkit.egress
  hub ci slots [N|default] [--json]   the CI runner's jobs at once (ciworker slots): show, set N (1..4, from its
                            next claim on, no restart) or back to the release's default; rollback = `hub ci slots 1`;
                            also the runner's memory gate ("waiting: host MemAvailable 3.9 GB < 6 GB")
  hub wf status|engine|probe|ls|show|start|send|cancel|watches|cron   the hub's workflow engine (DBOS) and the
                            watch schedules; hubkit.wf has the details

Deploys: `assistant deploy --target hub` (builds the release, renders the kit, flips CURRENT, waits for /health).
Files: ~/.local/share/claude-hub/{releases,kit}; token ~/.secrets/hub-token (written by the install paste).
Env: HUB_URL (default http://claude-hub:8700), HUB_TOKEN_FILE, ASSISTANT_REPO, ASSISTANT_PROFILE.
Exit: 0 ok · 1 degraded or error · 2 usage · 3 unreachable / not installed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from core import paths

from . import HubError, kit_dir
from . import client as C
from . import kit as K
from . import release as R


def _out(obj: Any, as_json: bool, text: str) -> None:
    print(json.dumps(obj, sort_keys=True) if as_json else text)


def _age(ts: float | None) -> str:
    if not ts:
        return "?"
    s = max(0, int(time.time() - ts))
    return f"{s // 3600} h" if s >= 3600 else f"{s // 60} min" if s >= 60 else f"{s} s"


def _ld(h: dict | None) -> dict:
    """The loader's own block: when its fallback answers /health (no release running), the status nests here."""
    v = (h or {}).get("loader")
    return v if isinstance(v, dict) else {}


def judge(code: int | None, h: dict | None, cur: tuple[str, str] | None) -> tuple[int, list[str]]:
    """-> (exit code, problems) for a /health answer (the hub's own, or the loader's fallback)."""
    if code is None or h is None:
        return 3, ["unreachable"]
    probs: list[str] = []
    if code != 200 or not h.get("release"):
        probs.append(f"no release running (loader {_ld(h).get('phase', '?')})")
    for k, v in sorted((h.get("checks") or {}).items()):
        if v != "ok":
            probs.append(f"{k} {v}")
    store = h.get("store")  # hubd's Postgres store (a release without it reports none)
    if isinstance(store, dict) and store.get("state") != "ok":
        probs.append(f"store {store.get('state') or '?'}" + (f" ({store['why']})" if store.get("why") else ""))
    if cur and h.get("release") and h["release"] != cur[0]:
        probs.append(f"runs {h['release']}, CURRENT names {cur[0]}")
    if cur and cur[1] in (h.get("bad") or _ld(h).get("bad") or []):
        probs.append("CURRENT's attempt was marked bad")
    exp, got = h.get("loader_expected"), h.get("loader")
    if isinstance(got, str) and got and exp and exp != "unknown" and exp != got:
        probs.append("loader differs from the installed kit (restart pending)")
    return (1 if probs else 0), probs


def checks_line(h: dict | None) -> str:
    """The checks plus the store when the hub reports one: "pg ok · … · store ok (hub, 1 migration)"."""
    h = h or {}
    parts = [f"{k} {v}" for k, v in sorted((h.get("checks") or {}).items())]
    st = h.get("store")
    if isinstance(st, dict):
        n = len(st.get("migrations") or [])
        parts.append(
            f"store {st.get('state') or '?'}"
            + (f" ({st.get('schema')}, {n} migration{'' if n == 1 else 's'})" if st.get("state") == "ok" else "")
        )
    return " · ".join(parts)


def cmd_health(a: argparse.Namespace) -> int:
    end = time.monotonic() + max(0.0, a.wait)
    while True:
        code, h, err = C.health()
        rc, probs = judge(code, h, R.read_current())
        if rc == 0 or time.monotonic() >= end:
            break
        time.sleep(2)
    if a.json:
        _out({"exit": rc, "status": code, "health": h, "error": err, "problems": probs}, True, "")
    elif rc == 3:
        print(f"hub: unreachable at {C.url()}: {err}")
    else:
        h = h or {}
        line = f"hub: release {h.get('release') or '-'} · " + checks_line(h)
        print(line + ("" if rc == 0 else " · PROBLEMS: " + "; ".join(probs)))
    return rc


def status(a: argparse.Namespace) -> int:
    cur = R.read_current()
    code, h, err = C.health()
    rc, probs = judge(code, h, cur)
    kinfo = K.info()
    tok = C.token_state()
    who = None
    if code is not None and tok == "ok":
        wc, wb, _ = C.whoami()
        who = (wb or {}).get("caller") if wc == 200 else f"refused ({wc})"
        if wc != 200:
            probs.append("the hub refuses the brain's token")
            rc = max(rc, 1)
    kept = R.kept()
    kit_state = None
    inst = (h or {}).get("kit") or _ld(h).get("kit")
    if kinfo and inst and inst != "unknown":
        kit_state = "installed = rendered" if inst == kinfo.get("kit") else "rendered kit not installed yet (paste)"
        if inst != kinfo.get("kit"):
            probs.append("kit pending: send `hub kit paste install`")
            rc = max(rc, 1)
    info = {
        "exit": rc,
        "url": C.url(),
        "reachable": code is not None,
        "error": err,
        "current": cur[0] if cur else None,
        "attempt": cur[1] if cur else None,
        "running": (h or {}).get("release"),
        "last_good": (h or {}).get("last_good") or _ld(h).get("last_good"),
        "checks": (h or {}).get("checks"),
        "store": (h or {}).get("store"),
        "kit_rendered": (kinfo or {}).get("kit"),
        "kit_installed": inst,
        "kit_state": kit_state,
        "kit_profile": (kinfo or {}).get("profile"),
        "token": tok,
        "whoami": who,
        "kept": kept,
        "problems": probs,
    }
    if a.json:
        _out(info, True, "")
        return rc
    if code is None:
        print(f"hub: not reachable ({err})")
    else:
        print(
            f"hub: {'ok' if rc == 0 else 'DEGRADED'} · running {info['running'] or '-'} · last good "
            f"{info['last_good'] or '-'} · " + checks_line(h)
        )
    print(
        f"release: CURRENT {info['current'] or '-'} · kept {len(kept)}"
        + (" (staged; loads when the stack runs)" if code is None and cur else "")
    )
    if kinfo:
        print(
            f"kit: {kinfo.get('kit')} from {str(kinfo.get('rev'))[:12]}, rendered {_age(kinfo.get('rendered'))} ago · "
            f"profile {kinfo.get('profile')} · paste {kinfo.get('paste_lines')} lines · "
            f"{kinfo.get('credentials')} credential name(s)" + (f" · {kit_state}" if kit_state else "")
        )
    else:
        print("kit: not rendered (assistant deploy --target hub, or hub kit render)")
    print(f"token: {tok}" + (f" · whoami {who}" if who else ""))
    for p in probs:
        if p != "unreachable":
            print(f"  problem: {p}")
    return rc


def cmd_kit_apply(a: argparse.Namespace) -> int:
    from . import kitapply as KA

    if a.kit_cmd == "apply-log":
        rc, text = KA.log()
        sys.stdout.write(text)
        return rc
    if a.kit_cmd == "apply-request":
        rc, out = KA.request(a.part, mint=not a.no_approval)
        text = [f"tier {out['tier']} · {out['summary']}", out["card"]]
        text += [f"needs the paste: {w}" for w in out["why"]]
        text.append(f"next: {out['next']}")
        _out(out, a.json, "\n".join(text))
        return rc
    rc, res = KA.apply(a.part, a.facts, KA.nonce_of(a.nonce))
    why = "; ".join(res.get("why") or [])
    _out(res, a.json, f"{res.get('result', 'error')}: {res.get('summary') or why or f'exit {rc}'}")
    return rc


def cmd_kit(a: argparse.Namespace) -> int:
    if a.kit_cmd in ("apply-request", "apply", "apply-log"):
        return cmd_kit_apply(a)
    if a.kit_cmd == "render":
        inf = K.render_rev(paths.repo(), a.rev)
        print(f"kit {inf['kit']} rendered from {inf['rev'][:12]} ({inf['paste_lines']}-line paste) in {kit_dir()}")
        if inf["profile"] != "profile":
            print(f"  warning: profile values defaulted: {', '.join(inf['defaulted'])}")
        return 0
    inf = K.info()
    if inf is None:
        print("hub: no kit rendered yet (assistant deploy --target hub, or hub kit render)", file=sys.stderr)
        return 3
    if a.kit_cmd == "show":
        _out(inf, a.json, "\n".join(f"{k}: {v}" for k, v in sorted(inf.items())))
        return 0
    name = f"{a.which}.sh" if a.kit_cmd == "paste" else "card.txt"
    if not kit_dir(name).is_file():
        part = next((p for p in ("tg", "voice", "phone", "browser") if name.startswith(p + "-")), "")
        why = ((inf.get(part) or {}).get("why") if part else None) or "not in the rendered kit"
        print(f"hub: no {name} ({why})", file=sys.stderr)
        return 3
    sys.stdout.write(kit_dir(name).read_text(encoding="utf-8"))
    return 0


def cmd_ci_slots(a: argparse.Namespace) -> int:
    from . import ci

    raw = str(a.n).strip().lower()
    if raw not in ("get", "default") and not (raw.isdigit() and 1 <= int(raw) <= 4):
        print("hub: ci slots: N (1..4) or default", file=sys.stderr)
        return 2
    ans = ci.slots("get" if raw == "get" else None if raw == "default" else int(raw))
    run = ans.get("runner") or {}
    line = (
        f"ci slots: {run.get('slots') or '?'} (runner)"
        + (f", set {ans['set']}" if ans.get("set") else ", release default")
        + f"; running {len(run.get('jobs') or [])}"
        + (f"; waiting: {run['mem_hold']}" if run.get("mem_hold") else "")
        + ("; the runner reads a change before its next claim" if raw != "get" else "")
    )
    _out(ans, a.json, line)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="hub",
        description=(__doc__ or "").split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("health")
    s.add_argument("--json", action="store_true")
    s.add_argument("--wait", type=float, default=0.0)
    s = sub.add_parser("kit")
    ks = s.add_subparsers(dest="kit_cmd", required=True)
    k = ks.add_parser("show")
    k.add_argument("--json", action="store_true")
    k = ks.add_parser("render")
    k.add_argument("--rev", default="main")
    k = ks.add_parser("paste")
    k.add_argument(
        "which",
        nargs="?",
        default="install",
        choices=(
            "install",
            "rollback",
            "tg-install",
            "tg-rollback",
            "browser-install",
            "browser-rollback",
            "browser-recopy",
            "voice-install",
            "voice-rollback",
            "voice-relogin",
            "phone-install",
            "phone-rollback",
        ),
    )
    ks.add_parser("card")
    k = ks.add_parser("apply-request")
    k.add_argument("part")
    k.add_argument("--no-approval", action="store_true")
    k.add_argument("--json", action="store_true")
    k = ks.add_parser("apply")
    k.add_argument("part")
    k.add_argument("--facts", required=True)
    k.add_argument("--nonce")
    k.add_argument("--json", action="store_true")
    ks.add_parser("apply-log")
    from . import egress, feed, phone, secrets, store, tg, voice, wf

    store.add_parser(sub)
    feed.add_parser(sub)
    tg.add_parser(sub)
    voice.add_parser(sub)
    phone.add_parser(sub)
    secrets.add_parser(sub)
    egress.add_parser(sub)
    wf.add_parser(sub)
    s = sub.add_parser("ci")
    cs = s.add_subparsers(dest="ci_cmd", required=True)
    k = cs.add_parser("slots")
    k.add_argument("n", nargs="?", default="get", help="N (1..4) or default; none: show")
    k.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "status":
            return status(a)
        if a.cmd == "health":
            return cmd_health(a)
        if a.cmd == "store":
            from . import store

            return store.main(a)
        if a.cmd == "feed":
            from . import feed

            return feed.main(a)
        if a.cmd == "phone":
            from . import phone

            return phone.main(a)
        if a.cmd == "voice":
            from . import voice

            return voice.main(a)
        if a.cmd == "tg":
            from . import tg

            return tg.main(a)
        if a.cmd == "secrets":
            from . import secrets

            return secrets.main(a)
        if a.cmd == "egress":
            from . import egress

            return egress.main(a)
        if a.cmd == "ci":
            return cmd_ci_slots(a)
        if a.cmd == "wf":
            from . import wf

            return wf.main(a)
        return cmd_kit(a)
    except HubError as e:
        print(f"hub: {e}", file=sys.stderr)
        return e.code
    except paths.RealHomeRefused as e:
        print(f"hub: {e}", file=sys.stderr)
        return 2
