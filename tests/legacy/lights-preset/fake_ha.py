#!/usr/bin/env python3
"""Fake `ha` for lights-preset tests. State: FAKE_STATE (json), call log: FAKE_LOG (one JSON argv per line).

State file: {"lights": {eid: state}, "scenes": {sid: {"members": [...], "sets": {eid: state}}},
             "lag": N (list reads that still show the old states after a scene run), "pending": {...},
             "fail": {"verb": rc}, "list_fail": bool}
"""

import json
import os
import sys

SF, LOG = os.environ["FAKE_STATE"], os.environ["FAKE_LOG"]
argv = sys.argv[1:]
with open(LOG, "a") as f:
    f.write(json.dumps(argv) + "\n")
dry = "-n" in argv
args = [a for a in argv if a != "-n"]
with open(SF) as f:
    S = json.load(f)


def save():
    with open(SF, "w") as f:
        json.dump(S, f)


verb = args[0]
if verb in S.get("fail", {}):
    print(f"fake: {verb} failed")
    sys.exit(S["fail"][verb])

if verb == "list":
    if S.get("list_fail"):
        print("HA unreachable", file=sys.stderr)
        sys.exit(1)
    lights = dict(S["lights"])
    if S.get("pending"):
        if S.get("lag", 0) > 0:
            S["lag"] -= 1
        else:
            S["lights"].update(S.pop("pending"))
            lights = dict(S["lights"])
        save()
    print(json.dumps([{"entity_id": e, "state": s, "attributes": {}} for e, s in lights.items()]))
elif verb == "state":
    sc = S["scenes"].get(args[1])
    if sc is None:
        sys.exit(2)
    print(json.dumps({"entity_id": args[1], "attributes": {"entity_id": sc["members"]}}))
elif verb == "scene":
    sc = S["scenes"][args[2]]
    if dry:
        print(f"dry run: would call scene.turn_on on: {args[2]}")
    else:
        S["pending"] = dict(sc["sets"])
        save()
        print(f"{args[2]}: old -> new ok")
elif verb == "script":
    print(("dry run: would call " if dry else "") + f"script.{args[2]} ok")
elif verb in ("set", "call", "on"):
    eid = args[1] if verb != "call" else args[2]
    if dry:
        print(f"dry run: would call light.turn_on on: {eid}")
    else:
        S["lights"][eid] = "on"
        save()
        print(f"{eid}: off -> on ok")
else:
    sys.exit(2)
