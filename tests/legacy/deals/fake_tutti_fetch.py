#!/usr/local/bin/python3.13 -I
"""Stand-in for ~/bin/tutti-fetch in the deals tests: serves $TUTTI_FIX_DIR/<quoted query>.html for a
https://www.tutti.ch/de/q/suche?query=... URL, logs each URL to $TUTTI_FETCH_LOG, and mimics the real fetcher's
output (JSON line, `challenge` flag) and exit codes. $TUTTI_FIX_DIR/DOWN present = browser unreachable (exit 4)."""

import json
import os
import re
import sys
import urllib.parse

url = sys.argv[1] if len(sys.argv) > 1 else ""
d = os.environ["TUTTI_FIX_DIR"]
with open(os.environ["TUTTI_FETCH_LOG"], "a") as f:
    f.write(url + "\n")
if os.path.exists(os.path.join(d, "DOWN")):
    print(json.dumps({"ok": False, "error": "browser not reachable over CDP: ECONNREFUSED"}))
    sys.exit(4)
u = urllib.parse.urlsplit(url)
if u.netloc != "www.tutti.ch" or not re.match(r"/(de|fr|it)/q/", u.path):
    print("tutti-fetch: only https://www.tutti.ch/{de,fr,it}/q/... search pages are allowed", file=sys.stderr)
    sys.exit(2)
q = urllib.parse.parse_qs(u.query).get("query", [""])[0]
p = os.path.join(d, urllib.parse.quote(q, safe="") + ".html")
html = open(p, encoding="utf-8").read() if os.path.exists(p) else open(os.path.join(d, "nodata.html")).read()
title = (re.search(r"<title>(.*?)</title>", html) or [None, ""])[1]
chal = "__NEXT_DATA__" not in html and bool(re.search(r"cf[-_]chl|challenge-form|Just a moment", html))
print(
    json.dumps(
        {"ok": True, "status": 403 if chal else 200, "url": url, "title": title, "html": html, "challenge": chal},
        ensure_ascii=False,
    )
)  # raw UTF-8 like the real fetcher (U+2028 stays unescaped)
