"""tgadapter.cli: the `tg-adapter` command. Stdlib only.

  Every command takes --instance test|live (default test): the test bot, or the live bot.
  tg-adapter ensure [--force] [--cron]     keep the adapter up (cron); nothing at all while the instance's config
                                           (~/.config/tg-adapter/<instance>.json; test: or config.json) is absent
  tg-adapter status [--json]               configured? token present (yes/no)? running?
  tg-adapter stop [--keep-down]            stop it now (--keep-down: also write ~/.config/tg-adapter/<instance>.off)
  tg-adapter bindings [--json] | bindings resolve TEXT | --data DATA [--chat ID --message ID] [--json]
                                           what a command or tap runs (tg-bind rows, card binds, the 0.1.22
                                           compat tables) or that it goes to the brain; edit rows with tg-bind
  tg-adapter check | run | enqueue | bindings …  handed to the bun CLI (adapters/tg/src/cli.ts); `bun install
                                           --frozen-lockfile` first when the adapter's node_modules is missing
Exit: 0 ok · 1 error · 2 usage · 3 held down (an exit that won't heal by itself; --cron: 0) · 4 crash-loop
backoff (--cron: 0).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

from . import ctl

PASS = ("check", "run", "enqueue", "bindings")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] and argv[0] in PASS:
        err = ctl.install()
        if err:
            print(f"tg-adapter: {err}", file=sys.stderr)
            return 1
        env = {k: v for k, v in os.environ.items() if k != "TELEGRAM_BOT_TOKEN"}
        cli = ctl.adapter_dir() / "src" / "cli.ts"
        try:
            return subprocess.run([ctl.bun(), str(cli), *argv], cwd=str(ctl.adapter_dir()), env=env).returncode
        except OSError as e:
            print(f"tg-adapter: cannot run the bun CLI: {e}", file=sys.stderr)
            return 1
    ap = argparse.ArgumentParser(
        prog="tg-adapter", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--version", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("ensure")
    p.add_argument("--force", action="store_true")
    p.add_argument("--cron", action="store_true")
    p.add_argument("--instance", choices=ctl.INSTANCES, default="test")
    p = sub.add_parser("status")
    p.add_argument("--json", action="store_true")
    p.add_argument("--instance", choices=ctl.INSTANCES, default="test")
    p = sub.add_parser("stop")
    p.add_argument("--keep-down", action="store_true")
    p.add_argument("--instance", choices=ctl.INSTANCES, default="test")
    a = ap.parse_args(argv)
    if a.version:
        print("tg-adapter (instances test|live; ensure/status/stop)")
        return 0
    if a.cmd == "ensure":
        code, msg = ctl.ensure(force=a.force, instance=a.instance)
        if not (
            a.cron
            and code == 0
            and msg.startswith(("running", "not configured", "switch off", "hub mode: companion running"))
        ):
            print(f"tg-adapter: {msg}")
        return 0 if a.cron and code in (3, 4) else code
    if a.cmd == "status":
        s = ctl.status(a.instance)
        print(json.dumps(s, sort_keys=True) if a.json else ctl.render_status(s))
        return 0
    if a.cmd == "stop":
        code, msg = ctl.stop(keep_down=a.keep_down, instance=a.instance)
        print(f"tg-adapter: {msg}")
        return code
    ap.print_help()
    return 2
