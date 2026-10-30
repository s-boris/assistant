#!/usr/local/bin/python3.13 -I
# managed: ~/opt/assistant — edit there, deploy with assistant land
"""hub-stamp: PreToolUse hook on mcp__hub__*: tells the hub gateway who calls.

Claude Code gives hooks the caller: a subagent's call carries agent_id (and agent_type); the main session's does
not. This hook adds one reserved argument, `_caller` = {p: brain|subagent, a: agent_id, t: agent_type, s:
session_id, ts, m}, where m is an HMAC-SHA256 (keyed with the brain's hub token, which the hub holds as
token_brain) over those fields and the MCP tool name; it replaces any `_caller` the model wrote. The hub
(tools.policy.verify_stamp) accepts a stamp for 5 min and for that tool only; a call without a valid stamp counts as
an unattributed subagent (the least the brain token may do). The stdio server drops `_caller` unread.

Output: the tool input with the stamp as hookSpecificOutput.updatedInput (permissionDecision allow: the session runs
in bypassPermissions anyway; another hook's deny still wins). Fails open without a stamp: any error, no token, a
tool outside mcp__hub__ = no output, exit 0. Never prints the token. Env: HUB_TOKEN_FILE (tests).
"""

import hashlib
import hmac
import json
import os
import sys
import time

PREFIX = "mcp__hub__"
VERSION = "hub-stamp/1"  # tools.policy.STAMP_VERSION


def _key() -> bytes:
    p = os.environ.get("HUB_TOKEN_FILE") or os.path.join(os.path.expanduser("~"), ".secrets", "hub-token")
    try:
        with open(p, "rb") as f:
            return f.read(4096).strip()
    except OSError:
        return b""


def stamp(ev: dict, key: bytes, now: float | None = None) -> dict | None:
    """The updated tool input for hook event EV, or None (not a hub tool, no input)."""
    tool = str(ev.get("tool_name") or "")
    inp = ev.get("tool_input")
    if not tool.startswith(PREFIX) or not isinstance(inp, dict) or not key:
        return None
    agent = str(ev.get("agent_id") or "")[:128]
    atype = str(ev.get("agent_type") or "")[:128]
    sess = str(ev.get("session_id") or "")[:128]
    p = "subagent" if agent else "brain"
    ts = int(now if now is not None else time.time())
    msg = "|".join((VERSION, p, agent, atype, sess, str(ts), tool[len(PREFIX) :])).encode()
    mac = hmac.new(key, msg, hashlib.sha256).hexdigest()[:32]
    out = {k: v for k, v in inp.items() if k != "_caller"}
    out["_caller"] = {"p": p, "a": agent, "t": atype, "s": sess, "ts": ts, "m": mac}
    return out


def main() -> int:
    try:
        ev = json.load(sys.stdin)
        new = stamp(ev, _key()) if isinstance(ev, dict) else None
        if new is None:
            return 0
        sys.stdout.write(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "allow",
                        "permissionDecisionReason": "hub stamp",
                        "updatedInput": new,
                    }
                }
            )
        )
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
