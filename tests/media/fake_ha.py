#!/usr/bin/env python3
"""Stand-in for ~/bin/ha in the play tests: state lives in $FAKE_HA_STATE (JSON {entity: {state, attributes}}),
every invocation is appended to $FAKE_HA_LOG. call media_player.turn_on/remote.turn_on on the wake entity (or the
TV) turns the TV on with an HDMI source; select_source sets the TV's source. Nothing else is supported."""

import json
import os
import sys

state_f, log_f = os.environ["FAKE_HA_STATE"], os.environ["FAKE_HA_LOG"]
with open(log_f, "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\n")
with open(state_f) as f:
    st = json.load(f)
tv = st["_tv"]
args = sys.argv[1:]
if args[:1] == ["state"]:
    e = st.get(args[1])
    if e is None:
        print(f"{args[1]}: no such entity", file=sys.stderr)
        sys.exit(2)
    print(json.dumps({"entity_id": args[1], "state": e["state"], "attributes": e.get("attributes", {})}))
    sys.exit(0)
if args[:1] == ["call"]:
    svc, eid = args[1], args[2]
    data = json.loads(args[3]) if len(args) > 3 and args[3].startswith("{") else {}
    if svc.endswith(".turn_on") and eid in (tv, st.get("_wake")):
        if st.get("_wake_works", True):
            st[tv]["state"] = "on"
            st[tv].setdefault("attributes", {})["source"] = "HDMI 2"
    elif svc == "media_player.select_source" and eid == tv:
        st[tv]["attributes"]["source"] = data.get("source")
    else:
        print(f"refused: {svc} {eid}", file=sys.stderr)
        sys.exit(2)
    with open(state_f, "w") as f:
        json.dump(st, f)
    print(f"{svc} -> {eid}: sent")
    sys.exit(0)
sys.exit(2)
