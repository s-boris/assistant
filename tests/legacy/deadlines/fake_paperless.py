#!/usr/bin/env python3
"""Stand-in for ~/bin/paperless in the deadlines tests: `search Q -n N`, `get ID` (a card, no content: plan B) and
`fields ID --fields a,b` (tier 1 like the vault: period_years = years of the first two dd.mm.yyyy dates; tier-2
names come back null + _withheld) answered from fixture.json (fake documents only). FAKE_PAPERLESS_FAIL=1 makes
every call fail, FAKE_PAPERLESS_HANG=SECS sleeps before answering (timeouts), FAKE_PAPERLESS_GARBAGE=1 answers
rc 0 with junk, FAKE_PAPERLESS_FIXTURE=PATH reads another fixture file (tests that plant fake identifiers in a
temp dir). Every call is logged to $FAKE_PAPERLESS_LOG so tests can assert it was read-only."""

import json
import os
import re
import sys

here = os.path.dirname(os.path.abspath(__file__))
docs = json.load(open(os.environ.get("FAKE_PAPERLESS_FIXTURE") or os.path.join(here, "fixture.json")))
log = os.environ.get("FAKE_PAPERLESS_LOG")
if log:
    with open(log, "a") as f:
        f.write(" ".join(sys.argv[1:]) + "\n")
if os.environ.get("FAKE_PAPERLESS_FAIL"):
    print("paperless: cannot reach http://fake: connection refused", file=sys.stderr)
    sys.exit(1)
cmd = sys.argv[1] if len(sys.argv) > 1 else ""
if os.environ.get("FAKE_PAPERLESS_HANG"):
    import time

    time.sleep(float(os.environ["FAKE_PAPERLESS_HANG"]))
if os.environ.get("FAKE_PAPERLESS_GARBAGE"):
    print("<html>502 Bad Gateway</html>\n#x not a doc line")
    sys.exit(0)


def line(d):
    return f"#{d['id']}  {d['created']}  {d['title']}  | corr: - | type: {d['type']} | tags: {d.get('tags', '-')}"


if cmd == "search":
    q = sys.argv[2].lower()
    terms = q.split()
    hits = [d for d in docs if all(t in (d["title"] + " " + d["content"]).lower() for t in terms)]
    if not hits:
        print("no matches")
    else:
        print(f"{len(hits)} match(es), showing {len(hits)}:")
        for d in hits:
            print(line(d))
elif cmd == "get":
    d = next((d for d in docs if d["id"] == int(sys.argv[2])), None)
    if not d:
        print("paperless: HTTP 404", file=sys.stderr)
        sys.exit(1)
    print(line(d))
    print(f"added: {d['created']}T10:00:00  pages: 1  amount: -  due: -")
    print(f"url: http://fake/documents/{d['id']}/details")
    print("privacy: a card, no content (plan B)")
elif cmd == "fields":
    d = next((d for d in docs if d["id"] == int(sys.argv[2])), None)
    if not d:
        print("paperless: HTTP 404", file=sys.stderr)
        sys.exit(1)
    want = sys.argv[sys.argv.index("--fields") + 1].split(",")
    tier1 = {
        "title": d["title"],
        "document_type": None if d["type"] == "-" else d["type"],
        "correspondent": None,
        "document_date": d["created"],
        "period_years": [int(y) for y in re.findall(r"\b[0-3]\d\.[01]\d\.((?:19|20)\d\d)\b", d["content"][:6000])][:2],
    }
    out = {k: tier1.get(k) for k in want}
    withheld = [k for k in want if k not in tier1]
    if withheld:
        out["_withheld"] = withheld
    print(json.dumps(out, indent=1))
else:
    print(f"fake paperless: unsupported command {cmd}", file=sys.stderr)
    sys.exit(2)
