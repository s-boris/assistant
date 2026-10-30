"""hubkit.tg: `hub tg stop|start|status`: the brain's emergency stop for the hub's Telegram adapter. Stdlib only.

  hub tg status [--json]                 the hold flag on the hub and tgloader's last report (state, release, age)
  hub tg stop [--why TEXT] [--wait S]    hold: tgloader stops the adapter within a poll (5 s) and starts none until
                                         `hub tg start`, across container restarts too. Waits up to S s (default 30)
                                         for tgloader to report "held"; exit 1 when it did not (a loader without the
                                         hold, or the container down: then the owner's `hub kit paste tg-rollback`)
  hub tg start [--force] [--wait S]      release the hold; refused while the brain's own live adapter is switched on
                                         (no ~/.config/tg-adapter/live.off: two pollers on one bot = 409s) unless
                                         --force. --wait S: wait for tgloader to report "running" (default 0: don't)

The flag lives in the hub's own state (HUB_STATE/tg-hold.json, written by hubd: POST /tg/hold, brain caller only);
it needs no Postgres. Exit: 0 ok · 1 not confirmed in time · 2 usage or refused · 3 hub unreachable.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from core import hub, paths

POLL = 1.0
WHY_DEFAULT = "brain: emergency stop"


def _live_off() -> Path:
    return paths.guard(paths.home() / ".config" / "tg-adapter" / "live.off")


def _get() -> dict[str, Any]:
    return hub.call("GET", "/tg/hold", timeout=5.0)


def _age(s: Any) -> str:
    if not isinstance(s, (int, float)):
        return "?"
    s = int(s)
    return f"{s // 3600} h" if s >= 3600 else f"{s // 60} min" if s >= 60 else f"{s} s"


def _line(d: dict[str, Any]) -> str:
    held = "HELD" + (f" ({d['why']})" if d.get("why") else "") if d.get("held") else "not held"
    ld = d.get("loader")
    if isinstance(ld, dict):
        lo = f"tgloader {ld.get('state')}" + (f" {ld['release']}" if ld.get("release") else "")
        lo += f", seen {_age(ld.get('age_s'))} ago"
    else:
        lo = "tgloader not seen since the hub started"
    return f"hub tg: {held} · {lo}"


def _wait_for(state: str, since: float, secs: float) -> dict[str, Any] | None:
    """The hold answer once tgloader reported `state` after `since` (hub clock), else None after secs."""
    end = time.monotonic() + max(0.0, secs)
    while True:
        try:
            d = _get()
        except hub.HubError:
            d = {}
        ld = d.get("loader")
        if isinstance(ld, dict) and ld.get("state") == state and float(ld.get("ts") or 0) >= since:
            return d
        if time.monotonic() >= end:
            return None
        time.sleep(POLL)


def status(a: argparse.Namespace) -> int:
    d = _get()
    print(json.dumps(d, sort_keys=True) if a.json else _line(d))
    return 0


def stop(a: argparse.Namespace) -> int:
    d = hub.call("POST", "/tg/hold", {"held": True, "why": a.why}, timeout=5.0)
    print(f"hub tg: hold set{'' if d.get('changed') else ' (was set already)'}")
    if a.wait <= 0:
        return 0
    got = _wait_for("held", float(d.get("ts") or 0) - 1.0, a.wait)
    if got is None:
        print(
            f"hub tg: tgloader did not report 'held' within {a.wait:g} s ({_line(_get())}): the adapter may still poll."
            " Fallback: the owner's `hub kit paste tg-rollback`."
        )
        return 1
    print(_line(got))
    return 0


def start(a: argparse.Namespace) -> int:
    if not a.force and not _live_off().exists():
        print(
            "hub tg: refused: the brain's own live adapter is switched on (no ~/.config/tg-adapter/live.off); two "
            "pollers on one bot fight (409). Switch it off first (`touch ~/.config/tg-adapter/live.hub && tg-adapter "
            "ensure --instance live`), or --force."
        )
        return 2
    d = hub.call("POST", "/tg/hold", {"held": False, "why": a.why}, timeout=5.0)
    print(f"hub tg: hold released{'' if d.get('changed') else ' (was not set)'}")
    if a.wait <= 0:
        return 0
    got = _wait_for("running", float(d.get("ts") or 0) - 1.0, a.wait)
    if got is None:
        print(f"hub tg: tgloader did not report 'running' within {a.wait:g} s ({_line(_get())})")
        return 1
    print(_line(got))
    return 0


def add_parser(sub: Any) -> None:
    s = sub.add_parser("tg", help="the hub's Telegram adapter: emergency stop (hold), start, status")
    ts = s.add_subparsers(dest="tg_cmd", required=True)
    p = ts.add_parser("status")
    p.add_argument("--json", action="store_true")
    p = ts.add_parser("stop")
    p.add_argument("--why", default=WHY_DEFAULT)
    p.add_argument("--wait", type=float, default=30.0)
    p = ts.add_parser("start")
    p.add_argument("--why", default="brain: released")
    p.add_argument("--force", action="store_true")
    p.add_argument("--wait", type=float, default=0.0)


def main(a: argparse.Namespace) -> int:
    try:
        return {"status": status, "stop": stop, "start": start}[a.tg_cmd](a)
    except hub.HubDown as e:
        print(f"hub tg: hub unreachable ({e}); fallback: the owner's `hub kit paste tg-rollback`")
        return 3
    except hub.Refused as e:
        print(f"hub tg: the hub refused: {e}")
        return 2
    except hub.HubError as e:
        print(f"hub tg: {e}")
        return 1
