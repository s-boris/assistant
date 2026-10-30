"""The phone paste's port move (P3-8, D-x): reads `docker compose config --format json` of claude-bot's project on
stdin and prints a docker-compose.override.yml for service bot that keeps every published port except the ones the
phone gateway takes (argv: the host ports to move; today the front alone). Runs in a throwaway python container
(--network none); stdlib only. Exit 3 (nothing printed) when bot does not publish every one of them: the paste stops
before touching claude-bot."""

import json
import sys


def main(argv: list[str]) -> int:
    try:
        move = {str(int(x)) for x in argv}
    except ValueError:
        move = set()
    svc = (json.load(sys.stdin).get("services") or {}).get("bot") or {}
    ports = svc.get("ports") or []
    have = {str(p.get("published", "")) for p in ports}
    if not move or not move <= have:
        missing = " ".join(sorted(move - have)) or "(none asked)"
        print(f"phone-ports: bot does not publish the port(s) to move: {missing}", file=sys.stderr)
        return 3
    keep = [p for p in ports if str(p.get("published", "")) not in move]
    out = [
        "# P3-8 (claude-hub phone paste): the phone gateway's host port(s) moved to claude-hub-phone.",
        "# Rollback: hub kit paste phone-rollback (renames this file and recreates bot).",
        "services:",
        "  bot:",
    ]
    if not keep:
        out.append("    ports: !reset []")
    else:
        out.append("    ports: !override")
        for p in keep:
            spec = {
                k: p[k] for k in ("target", "published", "host_ip", "protocol", "mode") if p.get(k) not in (None, "")
            }
            out.append("      - " + json.dumps(spec, sort_keys=True))
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
