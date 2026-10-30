"""hubkit.feed: the brain side of the hub's status feed. Stdlib only.

The hub serves its feed on HUB_FEED_URL (default http://claude-hub:8702; published nowhere). The dashboard reaches it
through an ssh reverse tunnel this module keeps: web VM 127.0.0.1:VM_PORT -> claude-hub:8702, and nginx serves that
as dash.<domain>/hub/ (-> /feed/) behind the dashboard's login. The brain also reports what only it can see: which
store is live per block (core.hub switches), its in-container Telegram transport (`events metrics --tg`) and the
adapter's state (`tg-adapter status`; with the in-container one switched off, claude-hub-tg's freshness from the hub
store's consumers) and its pending approvals (`approvals list --json`: a count of the rows in state pending, never their
summaries or actions).

  hub feed [--json]                     the hub's snapshot (parts, gateway, approvals, tg, traces)
  hub feed ensure                       cron, every minute: keep the tunnel up + push the brain report (silent when ok)
  hub feed push [--json]                push the brain report once
  hub feed watch ID HOST:PORT [--ttl S] [--label L] | unwatch ID      a TCP watch part in the feed (TTL <= 1 h)
  hub feed status | off | on            off = KILL SWITCH (~/.config/assistant/hub-feed-off): stops the tunnel, no
                                        pushes; the page then shows the 5-min snapshot (dash-data's hub block)
Env: HUB_FEED_URL, HUB_FEED_VM_PORT (8791), HUB_FEED_TARGET (claude-hub:8702, the tunnel's far end), HUB_FEED_VM
(user@host; default profile network.hosts.web_vm), HUB_FEED_SSH (tests: a fake ssh), HUB_FEED_EVENTS_BIN,
HUB_FEED_TGA_BIN, HUB_FEED_APPROVALS_BIN. Files: ~/.local/state/assistant/hub-feed-tunnel.{pid,log}.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from core import http, paths

from . import HubError, _profile_file

DEFAULT_URL = "http://claude-hub:8702"
VM_PORT = 8791
TARGET = "claude-hub:8702"
STORES = ("events", "outbox", "approvals")
TARGET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,62}:\d{1,5}$")
VM_RE = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+$|^[A-Za-z0-9.-]+$")


def feed_url() -> str:
    u = os.environ.get("HUB_FEED_URL", "").rstrip("/")
    if u:
        return u
    if paths.test_mode():
        raise HubError("ASSISTANT_TEST=1: set HUB_FEED_URL (tests never reach the real hub)", 2)
    return DEFAULT_URL


def off_file() -> Path:
    return paths.config("hub-feed-off")


def state_file(name: str) -> Path:
    return paths.state(name)


def snapshot(timeout: float = 5.0) -> dict[str, Any]:
    try:
        with http.open(feed_url() + "/feed", timeout=timeout) as r:
            d = json.load(r)
    except (OSError, ValueError) as e:
        raise HubError(f"feed unreachable ({type(e).__name__})", 3) from None
    if not isinstance(d, dict):
        raise HubError("feed: not a JSON object", 3)
    return d


# ------------------------------------------------------------------------------------------------ tunnel


def vm_port() -> int:
    v = os.environ.get("HUB_FEED_VM_PORT", str(VM_PORT))
    if not v.isdigit() or not 1024 <= int(v) < 65536:
        raise HubError("HUB_FEED_VM_PORT: 1024..65535", 2)
    return int(v)


def target() -> str:
    t = os.environ.get("HUB_FEED_TARGET", TARGET)
    if not TARGET_RE.match(t):
        raise HubError("HUB_FEED_TARGET: host:port", 2)
    return t


def web_vm() -> str:
    v = os.environ.get("HUB_FEED_VM", "")
    if not v:
        try:
            prof = json.loads(_profile_file().read_text(encoding="utf-8"))
            v = str(((prof.get("network") or {}).get("hosts") or {}).get("web_vm") or "")
        except (OSError, ValueError, AttributeError):
            v = ""
    if not v or not VM_RE.match(v):
        raise HubError("no web VM (profile network.hosts.web_vm)", 2)
    return v


def marker() -> str:
    return f"127.0.0.1:{vm_port()}:{target()}"


def tunnel_argv() -> list[str]:
    ssh = os.environ.get("HUB_FEED_SSH", "")
    if not ssh:
        if paths.test_mode():
            raise HubError("ASSISTANT_TEST=1: set HUB_FEED_SSH (tests never open a real tunnel)", 2)
        ssh = "ssh"
    return [ssh, "-N", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=20",
            "-o", "ServerAliveCountMax=3", "-o", "ConnectTimeout=10", "-R", marker(), web_vm()]  # fmt: skip


def tunnel_pid() -> int | None:
    """The running tunnel's pid (its command line carries the marker), else None."""
    try:
        pid = int(state_file("hub-feed-tunnel.pid").read_text().strip())
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except (OSError, ValueError):
        return None
    return pid if marker().encode() in cmd else None


def tunnel_start() -> int:
    log = state_file("hub-feed-tunnel.log")
    log.parent.mkdir(parents=True, exist_ok=True)
    if log.exists() and log.stat().st_size > 1 << 20:
        os.replace(log, log.with_suffix(".log.1"))
    with open(log, "ab") as fh:
        p = subprocess.Popen(tunnel_argv(), stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                             start_new_session=True, close_fds=True)  # fmt: skip
    pf = state_file("hub-feed-tunnel.pid")
    tmp = pf.with_suffix(".tmp")
    tmp.write_text(str(p.pid))
    os.replace(tmp, pf)
    return p.pid


def tunnel_stop() -> bool:
    pid = tunnel_pid()
    if pid is None:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    for _ in range(30):
        if tunnel_pid() is None or not Path(f"/proc/{pid}").exists():
            break
        time.sleep(0.1)
    return True


# ------------------------------------------------------------------------------------------------ brain report


def _bin(env: str, name: str) -> str:
    return os.environ.get(env) or str(paths.home() / "bin" / name)


def _json_of(argv: list[str], timeout: float = 30) -> dict[str, Any] | None:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        d = json.loads(p.stdout) if p.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    return d if isinstance(d, dict) else None


def _json_list(argv: list[str], timeout: float = 20) -> list[Any] | None:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        d = json.loads(p.stdout) if p.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    return d if isinstance(d, list) else None


def _dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _num(v: Any) -> int | float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v < 1e9:
        return None
    return v


def report() -> dict[str, Any]:
    """The brain's report: store switches, its Telegram transport (counts and timings, 24 h) and the adapter state.
    Counts, states and timings only."""
    from core import hub

    out: dict[str, Any] = {"stores": {s: hub.backend(s) for s in STORES}, "ttl": 180}
    m = _json_of([_bin("HUB_FEED_EVENTS_BIN", "events"), "metrics", "--tg", "--json", "--days", "1"])
    if m:
        t2, t3, t4 = (_dict(m.get(k)) for k in ("T2", "T3", "T4"))
        tg = {"sent_24h": _num(t4.get("sent")), "failed_24h": _num(t4.get("failed")),
              "dupes_24h": _num(t4.get("duplicates")), "send_p95_s": _num(t4.get("p95_s")),
              "receipts_24h": _num(t3.get("n")), "receipt_p95_s": _num(t3.get("p95_s")),
              "dropped_24h": _num(t2.get("dropped"))}  # fmt: skip
        out["tg"] = tg
    rows = _json_list([_bin("HUB_FEED_APPROVALS_BIN", "approvals"), "list", "--json"])
    if rows is not None:  # the open approvals of whichever store is live; only the count leaves this function
        out["approvals"] = {"pending": sum(1 for r in rows if isinstance(r, dict) and r.get("state") == "pending")}
    s = _json_of([_bin("HUB_FEED_TGA_BIN", "tg-adapter"), "status", "--instance", "live", "--json"], timeout=15)
    if s is not None:
        hubtg = _hub_tg(s) if s.get("switch_off") and not s.get("running") else None
        if hubtg:
            st = hubtg
        elif s.get("switch_off") or not s.get("configured"):
            st = ("off", "switched off" if s.get("switch_off") else "not configured")
        else:
            st = ("ok", "running") if s.get("running") else ("down", "stopped")
        out["parts"] = {"tg-adapter": {"state": st[0], "detail": st[1]}}
    else:
        out["parts"] = {"tg-adapter": {"state": "unknown", "detail": "status failed"}}
    tiles = _slo_tiles()
    if tiles is not None:
        out["slo"] = tiles
    return out


def _hub_tg(s: dict[str, Any]) -> tuple[str, str] | None:
    """With the in-container adapter switched off, the adapter runs in claude-hub-tg; `tg-adapter status` lists the
    hub store's consumers with their age in seconds. A tg consumer seen within 60 s = running there. None = no hub info
    (then the in-container switch state is reported)."""
    rows = _dict(s.get("hub")).get("consumers") or []
    tg = [_num(c.get("age")) for c in rows if isinstance(c, dict) and c.get("caller") == "tg"]
    ages = [a for a in tg if a is not None]
    if not ages:
        return None
    return ("ok", "in claude-hub-tg") if min(ages) < 60 else ("down", "claude-hub-tg not polling")


def _slo_tiles() -> list[dict[str, Any]] | None:
    """The SLO tiles from `slo --cache`'s file while the cache is under an hour old; never computed here (this runs
    every minute)."""
    try:
        from inbox import slo

        return slo.feed_tiles()
    except Exception:  # noqa: BLE001  (an SLO problem never stops the feed)
        return None


def push() -> dict[str, Any]:
    from core import hub

    body = report()
    try:
        try:
            hub.call("POST", "/feed/brain", body, timeout=5.0)
        except hub.Refused as e:
            # a hub release without the approvals count (or the slo tiles) refuses the key: report the rest
            late = [k for k in ("approvals", "slo") if k in body and k in str(e)]
            if not late or "unknown keys" not in str(e):
                raise
            body = {k: v for k, v in body.items() if k not in late}
            hub.call("POST", "/feed/brain", body, timeout=5.0)
    except hub.HubError as e:
        raise HubError(f"push refused or hub down ({e})", 3) from None
    return body


def watch(wid: str, tcp: str | None, ttl: int = 600, label: str | None = None, remove: bool = False) -> dict:
    from core import hub

    body: dict[str, Any] = {"id": wid, "remove": True} if remove else {"id": wid, "tcp": tcp, "ttl": ttl}
    if label and not remove:
        body["label"] = label
    try:
        return hub.call("POST", "/feed/watch", body, timeout=5.0)
    except hub.HubError as e:
        raise HubError(f"watch refused or hub down ({e})", 3) from None


def ensure() -> list[str]:
    """Tunnel up (unless switched off), then one brain report. Returns what it did (empty = nothing to do)."""
    did: list[str] = []
    if off_file().exists():
        if tunnel_stop():
            did.append("tunnel stopped (switched off)")
        return did
    if tunnel_pid() is None:
        did.append(f"tunnel started (pid {tunnel_start()})")
    push()
    return did


def status() -> dict[str, Any]:
    st: dict[str, Any] = {"switch": "off" if off_file().exists() else "on", "tunnel_pid": None}
    try:
        st["tunnel_pid"] = tunnel_pid()
        st["vm_port"], st["target"] = vm_port(), target()
    except HubError as e:
        st["error"] = str(e)
    try:
        s = snapshot(timeout=3)
        st.update(feed="ok", seq=s.get("seq"), at=s.get("at"), brain=s.get("brain"))
    except HubError as e:
        st["feed"] = str(e)
    return st


def summary(s: dict[str, Any]) -> str:
    parts = s.get("parts") or []
    bad = [f"{p['id']} {p['state']}" for p in parts if p.get("state") not in ("ok", "planned")]
    g, ap, tg, tr = (s.get(k) or {} for k in ("gateway", "approvals", "tg", "traces"))
    lines = [
        f"feed seq {s.get('seq')} · {len(parts)} parts" + (f" · not ok: {', '.join(bad)}" if bad else " · all ok"),
        f"gateway {g.get('state')} · calls 1 h {g.get('calls_1h')} (errors {g.get('errors_1h')}) · 24 h "
        f"{g.get('calls_24h')} (errors {g.get('errors_24h')}, denies {g.get('denies_24h')})",
        f"approvals pending {ap.get('pending')} (store {ap.get('store') or '?'}) · tg store {tg.get('store') or '?'}"
        f" · hub outbox queued {(tg.get('hub') or {}).get('queued')}",
        f"traces {tr.get('state')} · 1 h {tr.get('traces_1h')} · 24 h {tr.get('traces_24h')}"
        f" · error spans {tr.get('error_spans_24h')}",
    ]
    b = tg.get("brain") or {}
    if b:
        lines.append(f"brain tg 24 h: sent {b.get('sent_24h')} · failed {b.get('failed_24h')} · receipts "
                     f"{b.get('receipts_24h')} · {'fresh' if b.get('fresh') else 'stale'}")  # fmt: skip
    return "\n".join(lines)


def add_parser(sub: Any) -> None:
    p = sub.add_parser("feed", help="the hub's status feed")
    p.add_argument("action", nargs="?", default="show",
                   choices=("show", "ensure", "push", "watch", "unwatch", "status", "off", "on"))  # fmt: skip
    p.add_argument("args", nargs="*")
    p.add_argument("--json", action="store_true")
    p.add_argument("--ttl", type=int, default=600)
    p.add_argument("--label")


def main(a: Any) -> int:
    act = a.action
    if act == "show":
        s = snapshot()
        print(json.dumps(s, indent=1) if a.json else summary(s))
        return 0
    if act == "ensure":
        for line in ensure():
            print(f"hub feed: {line}")
        return 0
    if act == "push":
        body = push()
        print(json.dumps(body, indent=1) if a.json else "hub feed: report pushed")
        return 0
    if act in ("watch", "unwatch"):
        need = 2 if act == "watch" else 1
        if len(a.args) != need:
            raise HubError("usage: hub feed watch ID HOST:PORT [--ttl S] [--label L] | hub feed unwatch ID", 2)
        r = watch(a.args[0], a.args[1] if act == "watch" else None, a.ttl, a.label, remove=act == "unwatch")
        print(json.dumps(r))
        return 0
    if act == "status":
        st = status()
        print(json.dumps(st, indent=1) if a.json else " · ".join(f"{k} {v}" for k, v in st.items()))
        return 0
    f = off_file()
    if act == "off":
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("off\n")
        stopped = tunnel_stop()
        print("hub feed: off (tunnel " + ("stopped" if stopped else "was not running") + "); `hub feed on` to undo")
        return 0
    f.unlink(missing_ok=True)
    print("hub feed: on (the next `hub feed ensure` starts the tunnel)")
    return 0


__all__ = ["add_parser", "ensure", "main", "push", "report", "snapshot", "status", "watch"]
