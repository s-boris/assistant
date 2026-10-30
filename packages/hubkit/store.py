"""hubkit.store: `hub store …`: the stores on the hub, from the brain's side. Stdlib only (core.hub client).

hub store status [--json]           each store's backend, spool depth, the hub's row counts and outbox consumers
hub store spool [--drain]           spooled ledger writes (hub was down) and their ordered replay
"""

from __future__ import annotations

import json
from typing import Any

from core import hub

from . import HubError


def status() -> dict[str, Any]:
    st: dict[str, Any] = {"stores": {s: hub.backend(s) for s in hub.STORES}, "spool": hub.spool_depth()}
    try:
        st["counts"] = hub.call("GET", "/admin/counts", timeout=5.0)
        st["consumers"] = hub.call("GET", "/outbox/consumers", timeout=5.0).get("consumers")
    except hub.HubError as e:
        st["hub"] = f"unreachable: {e}"
    return st


def main(a: Any) -> int:
    try:
        if a.store_cmd == "status":
            st = status()
            if a.json:
                print(json.dumps(st, sort_keys=True))
            else:
                print("stores: " + " · ".join(f"{k} {v}" for k, v in st["stores"].items()) + f" · spool {st['spool']}")
                print("hub: " + (st.get("hub") or " · ".join(f"{k} {v}" for k, v in (st.get("counts") or {}).items())))
            return 0
        if a.store_cmd == "spool":
            if a.drain:
                done, left = hub.drain(60)
                print(f"spool: replayed {done}, left {left}")
            else:
                print(f"spool: {hub.spool_depth()} row(s)")
            return 0
    except hub.HubError as e:
        raise HubError(f"store: {e}", 3) from None
    raise HubError("unknown store command", 2)


def add_parser(sub: Any) -> None:
    s = sub.add_parser("store", help="the stores on the hub (status, spool)")
    ss = s.add_subparsers(dest="store_cmd", required=True)
    p = ss.add_parser("status")
    p.add_argument("--json", action="store_true")
    p = ss.add_parser("spool")
    p.add_argument("--drain", action="store_true")
