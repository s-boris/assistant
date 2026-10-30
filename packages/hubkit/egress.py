"""hubkit.egress: `hub egress status|check|use|endpoints` — the brain's side of the hub's egress injector. Stdlib only.

hub egress status [--json]     the hub's injector (GET /egress/status, brain caller): policy, per binding the mode
                               (audit | enforce), ready or why not, slots, rule count and counters, the last refusals
                               and would_deny calls, and the would_deny shapes (method + path with ids folded) that
                               the audit collects; plus each handle's local mode (core.egress). Exit 1 when the
                               running release has no injector (egress_off), 3 when the hub is unreachable.
hub egress check HANDLE METHOD URL [--header 'Name: value']...
                               the hub's decision for one request (POST /egress/check: nothing is sent, no value
                               used). The URL and headers carry the placeholder hub:secret:HANDLE in the slot.
hub egress use [HANDLE|default MODE]
                               show or set the brain-side mode per handle: local (the tool reads its key
                               file), shadow (local + the hub's decision logged), hub (placeholder in, the hub sends)
hub egress endpoints [--set NAME=URL]... [--write]
                               the endpoints file the owner's secrets paste copies into the hub's secrets folder
                               ("NAME URL" lines: the hub binds @endpoints:NAME to it). ha and dash come from the
                               profile (services.ha_url, network.domain), the rest from --set. Prints names and
                               set/missing only; --write puts it at ~/.config/claude-hub/egress.endpoints (0600).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
from pathlib import Path
from typing import Any

from core import egress as E
from core import paths

from . import HubError, _profile_file
from . import client as C

ENDPOINTS = ("claude-hub", "egress.endpoints")
NAMES = ("ha", "tracearr", "gym", "dash")  # the policy's @endpoints:NAME references


def endpoints_file() -> Path:
    return paths.guard(paths.home() / ".config" / ENDPOINTS[0] / ENDPOINTS[1])


def _profile() -> dict[str, Any]:
    pf = _profile_file()
    try:
        d = json.loads(pf.read_text(encoding="utf-8")) if pf.is_file() else {}
    except (OSError, ValueError):
        raise HubError("profile unreadable: fix it first") from None
    return d if isinstance(d, dict) else {}


def _get(d: dict[str, Any], dotted: str) -> str:
    o: Any = d
    for k in dotted.split("."):
        o = o.get(k) if isinstance(o, dict) else None
    return o.strip() if isinstance(o, str) else ""


def _url_ok(u: str) -> bool:
    p = urllib.parse.urlsplit(u)
    return p.scheme in ("http", "https") and bool(p.hostname) and not (p.username or p.query or p.fragment)


def render_endpoints(sets: list[str]) -> dict[str, str]:
    prof = _profile()
    out: dict[str, str] = {}
    if _get(prof, "services.ha_url"):
        out["ha"] = _get(prof, "services.ha_url").rstrip("/")
    if _get(prof, "network.domain"):
        out["dash"] = "https://dash." + _get(prof, "network.domain")
    for s in sets:
        name, eq, url = s.partition("=")
        if not eq or name not in NAMES:
            raise HubError(f"--set NAME=URL with NAME one of {', '.join(NAMES)}", 2)
        out[name] = url.strip().rstrip("/")
    bad = [n for n, u in out.items() if not _url_ok(u)]
    if bad:
        raise HubError(f"not an http(s) URL without user, query or fragment: {', '.join(bad)}", 2)
    return out


def endpoints(a: argparse.Namespace) -> int:
    got = render_endpoints(a.set or [])
    for n in NAMES:
        print(f"{n:9} {'set' if n in got else 'missing'}")
    if a.write:
        f = endpoints_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f".{f.name}.{os.getpid()}.tmp")
        body = "# hub egress endpoints: NAME URL, copied into the hub's secrets folder by the owner's paste\n"
        body += "".join(f"{n} {got[n]}\n" for n in NAMES if n in got)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.replace(tmp, f)
        print(f"wrote {f} ({len(got)} endpoint(s)); its secrets.h10 row: egress.endpoints "
              f".config/{ENDPOINTS[0]}/{ENDPOINTS[1]} egress")  # fmt: skip
    return 0 if all(n in got for n in NAMES) else 1


def status(a: argparse.Namespace) -> int:
    code, d, why = C.get("/egress/status", auth=True, timeout=6.0)
    local = {h: E.mode(h) for h in sorted(E.HANDLES)}
    if a.json:
        print(json.dumps({"hub": d, "status": code, "why": why, "local": local}, indent=1))
    elif code is None:
        print(f"hub: unreachable ({why})")
    elif code != 200 or not isinstance(d, dict):
        print(f"hub: {code} {(d or {}).get('error', '')} {(d or {}).get('hint', '')}".rstrip())
    else:
        pol = d.get("policy") or {}
        print(f"policy {pol.get('sha') or '-'} · {pol.get('bindings', 0)} binding(s)" +
              (f" · ERROR {pol['error']}" if pol.get("error") else ""))  # fmt: skip
        for n, b in sorted((d.get("bindings") or {}).items()):
            c = b.get("count") or {}
            sent = c.get("fetch:allow", 0) + c.get("fetch:would_deny", 0)
            wd = c.get("fetch:would_deny", 0) + c.get("check:would_deny", 0)
            dn = c.get("fetch:deny", 0) + c.get("check:deny", 0)
            ready = "ready" if b.get("ready") else f"off ({b.get('why')})"
            print(f"  {n:9} {b.get('mode'):7} {ready:24} local={local.get(n, '-'):6} sent {sent} · would_deny {wd} · "
                  f"denied {dn} · refused {c.get('fetch:refused', 0) + c.get('check:refused', 0)}")  # fmt: skip
        for w in (d.get("would_deny") or [])[:20]:
            print(f"  would_deny {w.get('handle')} {w.get('method')} {w.get('path')} ×{w.get('n')}")
        aud = d.get("audit") or {}
        if aud.get("error"):
            print(f"  audit log: {aud['error']}")
    if code is None:
        return 3
    return 0 if code == 200 else 1


def check(a: argparse.Namespace) -> int:
    heads = []
    for h in a.header or []:
        k, sep, v = h.partition(":")
        if not sep:
            raise HubError("--header 'Name: value'", 2)
        heads.append([k.strip(), v.strip()])
    try:
        code, d = E._hub("/egress/check", {"handle": a.handle, "method": a.method, "url": a.url, "headers": heads}, 8.0)
    except OSError as e:
        print(f"hub: unreachable ({type(e).__name__})", file=sys.stderr)
        return 3
    print(json.dumps({"status": code, **d}))
    return 0 if code == 200 else 1


def use(a: argparse.Namespace) -> int:
    if a.handle is None:
        for h in sorted(E.HANDLES):
            print(f"{h:9} {E.mode(h)}")
        return 0
    if a.mode is None:
        raise HubError("hub egress use HANDLE|default local|shadow|hub", 2)
    try:
        E.set_mode(a.handle, a.mode)
    except ValueError as e:
        raise HubError(str(e), 2) from None
    print(f"{a.handle}: {a.mode}")
    return 0


def add_parser(sub: Any) -> None:
    p = sub.add_parser("egress", help="the hub's egress injector (status, check, local modes, endpoints)")
    ps = p.add_subparsers(dest="egress_cmd", required=True)
    s = ps.add_parser("status")
    s.add_argument("--json", action="store_true")
    s = ps.add_parser("check")
    s.add_argument("handle")
    s.add_argument("method")
    s.add_argument("url")
    s.add_argument("--header", action="append")
    s = ps.add_parser("use")
    s.add_argument("handle", nargs="?")
    s.add_argument("mode", nargs="?", choices=E.MODES)
    s = ps.add_parser("endpoints")
    s.add_argument("--set", action="append", metavar="NAME=URL")
    s.add_argument("--write", action="store_true")


def main(a: argparse.Namespace) -> int:
    return {"status": status, "check": check, "use": use, "endpoints": endpoints}[a.egress_cmd](a)
