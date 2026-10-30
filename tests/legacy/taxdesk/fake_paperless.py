#!/usr/bin/env python3
"""Stand-in for ~/bin/paperless in the taxdesk tests: `search Q -n N --json` and `get ID --json` (a card: metadata +
amount, never content), answered from fixture_docs.json (fake documents only). Any other command exits
3. FAKE_PAPERLESS_FAIL=1 makes every call fail. Every call is logged to $FAKE_PAPERLESS_LOG (tests assert
read-only use)."""

import json
import os
import sys

here = os.path.dirname(os.path.abspath(__file__))
docs = json.load(open(os.path.join(here, "fixture_docs.json")))
log = os.environ.get("FAKE_PAPERLESS_LOG")
if log:
    with open(log, "a") as f:
        f.write(" ".join(sys.argv[1:]) + "\n")
if os.environ.get("FAKE_PAPERLESS_FAIL"):
    print("paperless: cannot reach http://fake: connection refused", file=sys.stderr)
    sys.exit(1)
cmd = sys.argv[1] if len(sys.argv) > 1 else ""


def meta(d):
    return {
        "id": d["id"],
        "created": d["created"],
        "title": d["title"],
        "correspondent": d.get("corr"),
        "document_type": d["type"],
        "tags": d.get("tags", []),
    }


if cmd == "search" and "--json" in sys.argv:
    terms = sys.argv[2].lower().split()
    hits = [meta(d) for d in docs if all(t in (d["title"] + " " + d["content"]).lower() for t in terms)]
    print(json.dumps({"count": len(hits), "results": hits}))
elif cmd == "get" and "--json" in sys.argv:
    d = next((d for d in docs if d["id"] == int(sys.argv[2])), None)
    if not d:
        print("paperless: HTTP 404", file=sys.stderr)
        sys.exit(1)
    print(
        json.dumps(
            dict(
                meta(d),
                amount=d.get("amount"),
                currency="CHF" if d.get("amount") else None,
                due_date=None,
                page_count=1,
            )
        )
    )
else:
    print(f"fake paperless: unsupported {sys.argv[1:]}", file=sys.stderr)
    sys.exit(3)
