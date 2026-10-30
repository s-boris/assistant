"""approvals: the CLI over the approvals library. `approvals --help`.

  approvals new --action A --summary S [--ttl 900] [--ref REF] [--yes LABEL] [--no LABEL] [--json]
        a pending approval for exactly this action: prints the nonce (--json: nonce, expires, summary, ref and the
        card's ✅/❌ buttons with ap:<nonce>:y|n). --action - reads the action from stdin.
  approvals consume NONCE --action A [--by WHO] [--json]
        use his approval once: exit 0 (once) · 3 expired · 4 used already · 5 unknown, not decided, declined,
        cancelled or another action (not his approval for this)
  approvals status NONCE [--json]         state (pending|approved|denied|expired|cancelled|used), summary, ref, expiry
  approvals wait NONCE [--timeout S] [--json]
        until he decides: 0 approved · 3 expired · 5 declined / cancelled / unknown · 6 still pending (never consumes)
  approvals sent NONCE --msg MESSAGE_ID [--chat CHAT]   the card's message id (poll mode, after the reply tool)
  approvals cancel NONCE                  pending -> cancelled (a replaced card): exit 5 when it wasn't pending
  approvals list [--all] [--json]         open approvals (--all: every row of the last 30 days)
  approvals audit [--json]                T8: used without his yes, used or decided after expiry (exit 1 if any)

NONCE may also be the callback data ap:<nonce>:y|n. There is no verb that records a decision: only his tap does (the
adapter, or his entry in the telegram-plus inbound journal while the plugin polls).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

import approvals as A


def _out(obj: Any, as_json: bool, text: str) -> None:
    print(json.dumps(obj, ensure_ascii=False) if as_json else text)


def _action(v: str) -> str:
    return sys.stdin.read() if v == "-" else v


def _line(r: dict[str, Any], now: float) -> str:
    st = A.state(r, now)
    left = max(0, int(r["expires"] - now))
    when = f"expires {A.local_hm(r['expires'])} ({left // 60} min)" if st == "pending" else f"{st}"
    ref = f" · {r['ref']}" if r.get("ref") else ""
    return f"{r['nonce'][:8]}… {st:9} {when}{ref} · {r['summary']}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="approvals",
        description=(__doc__ or "").split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("new")
    s.add_argument("--action", required=True)
    s.add_argument("--summary", required=True)
    s.add_argument("--ttl", type=int, default=A.DEFAULT_TTL)
    s.add_argument("--ref")
    s.add_argument("--yes", default=A.YES)
    s.add_argument("--no", default=A.NO)
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("consume")
    s.add_argument("nonce")
    s.add_argument("--action", required=True)
    s.add_argument("--by", default="cli")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("status")
    s.add_argument("nonce")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("wait")
    s.add_argument("nonce")
    s.add_argument("--timeout", type=float, default=60.0)
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("sent")
    s.add_argument("nonce")
    s.add_argument("--msg", required=True, type=int)
    s.add_argument("--chat")
    s = sub.add_parser("cancel")
    s.add_argument("nonce")
    s = sub.add_parser("list")
    s.add_argument("--all", action="store_true")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("audit")
    s.add_argument("--json", action="store_true")
    o = ap.parse_args(argv)

    if o.cmd == "new":
        try:
            r = A.new(_action(o.action), o.summary, o.ttl, ref=o.ref, yes=o.yes, no=o.no)
        except ValueError as e:
            print(f"approvals: {e}", file=sys.stderr)
            return 2
        _out(r, o.json, r["nonce"])
        return 0
    if o.cmd == "consume":
        code, info = A.consume(o.nonce, _action(o.action), o.by)
        _out(dict(info, code=code), o.json, f"{info.get('state')}: {info.get('why')}")
        return code
    if o.cmd == "status":
        A.sync(o.nonce)
        r = A.get(o.nonce)
        if r is None:
            _out({"nonce": A.norm_nonce(o.nonce), "state": "unknown"}, o.json, "unknown")
            return A.EXIT_NOT_HIS
        now = time.time()
        pub = {
            k: r[k]
            for k in (
                "nonce",
                "summary",
                "ref",
                "chat",
                "message_id",
                "created",
                "expires",
                "decided_ts",
                "decided_via",
                "used_ts",
                "used_by",
                "refused",
            )
        }
        _out(dict(pub, state=A.state(r, now), expires_local=A.local_hm(r["expires"])), o.json, _line(r, now))
        return 0
    if o.cmd == "wait":
        code, info = A.wait(o.nonce, o.timeout)
        _out(dict(info, code=code), o.json, f"{info.get('state')}" + (f": {info['why']}" if info.get("why") else ""))
        return code
    if o.cmd == "sent":
        chat = o.chat or A.owner_id()
        if not chat:
            print("approvals: no --chat and no owner in the access file", file=sys.stderr)
            return 2
        if not A.attach(o.nonce, chat, o.msg):
            print("approvals: not a pending approval (or another chat)", file=sys.stderr)
            return A.EXIT_NOT_HIS
        print("ok")
        return 0
    if o.cmd == "cancel":
        if A.cancel(o.nonce):
            print("cancelled")
            return 0
        print("approvals: not a pending approval", file=sys.stderr)
        return A.EXIT_NOT_HIS
    if o.cmd == "list":
        now = time.time()
        rs = A.rows(every=o.all, now=now)
        if o.json:
            print(
                json.dumps(
                    [
                        dict(
                            {k: r[k] for k in ("nonce", "summary", "ref", "expires", "decided_via", "used_by")},
                            state=A.state(r, now),
                        )
                        for r in rs
                    ],
                    ensure_ascii=False,
                )
            )
        else:
            print("\n".join(_line(r, now) for r in rs) if rs else "no open approvals")
        return 0
    if o.cmd == "audit":
        r = A.audit()
        _out(
            r,
            o.json,
            f"T8 {'ok' if r['ok'] else 'VIOLATIONS'}: {r['violations']} violation(s), {r['approvals']} "
            f"approvals, {r['used']} used, {r['refused_attempts']} refused attempt(s)",
        )
        return 0 if r["ok"] else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
