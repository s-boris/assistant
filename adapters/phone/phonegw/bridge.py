"""Routine bridge: his taps on push actions -> my tools, with no tokens and no notes.

  urgent push (sent by `push`)    -> `push event -` with {"t":"action","body":{push_id, nonce, verb, arg}} on stdin:
                                     push owns its verbs (seen/done/swipe stop the ladder, snooze, call)
  other levels, arg r:ID          -> `remind act VERB r:ID[:CODE]` (seen|done|snooze)
  other levels, call              -> `talk-button --json` (the relay rings him once; its own limits apply)
  crate with arg tid-…              -> `rave-radar releases crate tid-…` (Track ID tile)
ha_off and open only land in the push log. Fake tools for tests: PUSH_BIN, REMIND_BIN, TALK_BUTTON_BIN, RAVE_RADAR_BIN."""
import json
import asyncio
import re
import logging
import os

HOME = os.environ.get("HOME") or os.path.expanduser("~")
log = logging.getLogger("phonegw.bridge")


def tool(env, default):
    return os.environ.get(env) or os.path.join(HOME, "bin", default)


def plan(a):
    """action dict {push_id, nonce, tag, level, verb, arg} -> [(argv, stdin bytes or None), ...]"""
    verb, arg = a["verb"], a.get("arg") or ""
    if a.get("level") == "urgent":
        body = {"push_id": a["push_id"], "nonce": a.get("nonce"), "verb": "dismiss" if verb == "dismissed" else verb,
                "arg": arg or None}
        return [([tool("PUSH_BIN", "push"), "event", "-"], json.dumps({"t": "action", "body": body}).encode())]
    if arg.startswith("r:") and verb in ("seen", "done", "snooze"):
        return [([tool("REMIND_BIN", "remind"), "act", verb, arg], None)]
    if verb == "call":
        return [([tool("TALK_BUTTON_BIN", "talk-button"), "--json"], None)]
    if verb == "crate" and re.match(r"^tid-[A-Za-z0-9_-]{1,40}$", arg):     # Track ID tile's ➕ Crate
        return [([tool("RAVE_RADAR_BIN", "rave-radar"), "releases", "crate", arg], None)]
    return []


async def run(a, timeout=30):
    """-> [(tool name, exit code or None)]; never raises."""
    out = []
    for argv, data in plan(a):
        name = os.path.basename(argv[0])
        if not os.access(argv[0], os.X_OK):
            out.append((name, None))
            continue
        try:
            p = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.PIPE if data is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(p.communicate(data), timeout)
            out.append((name, p.returncode))
        except (OSError, asyncio.TimeoutError) as e:
            log.warning("bridge: %s failed (%s)", name, type(e).__name__)
            out.append((name, None))
    if out:
        log.info("bridge %s: %s", a["verb"], ", ".join("%s=%s" % x for x in out))
    return out


# ---------------------------------------------------------------- flag effects (FlagEngine 'tool' effects)
EFFECT_TOOLS = {
    "remind_place": lambda args: [tool("REMIND_BIN", "remind"), "place-due"],
    "sleepin_release": lambda args: [tool("SLEEPIN_BIN", "sleepin"), "release", "--reason", "phone app: %s" % (args[0] if args else "awake")],
    "routine_leaving": lambda args: [tool("PHONE_ROUTINE_BIN", "phone-routine"), "leaving"],
    "talk_button": lambda args: [tool("TALK_BUTTON_BIN", "talk-button"), "--json"],
    "sleepin_sleep_end": lambda args: [tool("SLEEPIN_BIN", "sleepin"), "release", "--reason", "phone app: sleep_end (Health Connect)"],
    "cat_mark": lambda args: [tool("CAT_SUPPLIES_BIN", "cat-supplies"), "mark", args[0] if args and args[0] in ("filter", "desiccant", "litter") else "none"],
    "crate_now": lambda args: [tool("CRATE_NOW_BIN", "crate-now"), "--json"],
    # Home autopilot 2 feed (routine home_ctx, off by default); home-ctx itself stays a dry run until its config allows writes
    "home_ctx": lambda args: [tool("HOME_CTX_BIN", "home-ctx"), "feed", "--apply", "--reason",
                              args[0] if args and str(args[0]).replace("_", "").isalnum() else "phone"],
}


async def run_tool(name, args, timeout=60):
    """One flag effect -> (tool, exit code or None); never raises."""
    if name not in EFFECT_TOOLS:
        return (name, None)
    argv = EFFECT_TOOLS[name](list(args))
    if not os.access(argv[0], os.X_OK):
        return (os.path.basename(argv[0]), None)
    try:
        p = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                                                 stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(p.communicate(), timeout)
        log.info("effect %s: exit %s", name, p.returncode)
        return (os.path.basename(argv[0]), p.returncode)
    except (OSError, asyncio.TimeoutError) as e:
        log.warning("effect %s failed (%s)", name, type(e).__name__)
        return (os.path.basename(argv[0]), None)
