#!/usr/bin/env bash
# Offline tests for ~/bin/route (Swiss public transport connections from transport.opendata.ch; its search.ch fallback
# is off here). Exit 0 = all pass.
# The seam ROUTE_API points at a loopback mock API (python http.server) that records each request and serves a
# fixture shaped like the live API (captured 2026-10-15: walk.duration in seconds, nulls for platform/number/to/station).
# http(s)_proxy also point at the mock, so a request to the real default URL can only reach the mock (recorded as a
# CONNECT and refused): nothing leaves the box even if the seam breaks.
# Covers: output format (walks, journeys, platforms, transfers, null fields, skipped sections), query parameters
# (from/to/date/limit, --arrive/--at/--date/-n, umlauts and commas), no connections, HTTP errors with/without an API
# message, junk instead of JSON, dropped connection, unreachable API, bad arguments, the default URL.
# ROUTE_BIN=<path> tests another copy.
set -uo pipefail
RT=${ROUTE_BIN:-$(cd "$(dirname "$0")/../../../scripts" && pwd)/route}
T=$(mktemp -d "${TMPDIR:-/tmp}/route-tests.XXXXXX")
export FAKE=$T/fake; mkdir -p "$FAKE"
cat > "$T/mock.py" <<'EOF'
import http.server, os, sys
F = os.environ['FAKE']
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def rec(self, line):
        with open(F + '/requests.log', 'a') as f: f.write(line + '\n')
    def do_CONNECT(self):
        self.rec('CONNECT ' + self.path); self.send_response(403); self.end_headers()
    def do_GET(self):
        self.rec('GET ' + self.path)
        mode = open(F + '/mode').read().split()
        if mode[0] == 'close':
            self.close_connection = True; return
        body = open(F + '/answer', 'rb').read()
        self.send_response(int(mode[1]) if mode[0] == 'code' else 200)
        self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(body)))
        self.end_headers(); self.wfile.write(body)
s = http.server.ThreadingHTTPServer(('127.0.0.1', 0), H)
open(F + '/port.tmp', 'w').write(str(s.server_address[1])); os.rename(F + '/port.tmp', F + '/port')
s.serve_forever()
EOF
python3 "$T/mock.py" & MOCK=$!
trap 'kill $MOCK 2>/dev/null; rm -rf "$T"' EXIT
for _ in $(seq 1 50); do [ -s "$FAKE/port" ] && break; sleep 0.1; done
PORT=$(cat "$FAKE/port" 2>/dev/null) || { echo "mock API did not start"; exit 1; }
export ROUTE_API=http://127.0.0.1:$PORT/v1
export http_proxy=http://127.0.0.1:$PORT https_proxy=http://127.0.0.1:$PORT HTTP_PROXY=http://127.0.0.1:$PORT \
  HTTPS_PROXY=http://127.0.0.1:$PORT no_proxy=127.0.0.1 NO_PROXY=127.0.0.1

# home is door to door (lib/home_place): a fictional profile; geocoder off unless a test fills the cache
printf '%s' '{"home": {"street": "Probeweg 7", "postcode": "1234", "town": "Testdorf", "town_aliases": ["Testdorp"]}}' > "$T/profile.json"
export ASSISTANT_PROFILE=$T/profile.json HOME_PLACE_GEO=off HOME_PLACE_CACHE=$T/home-place.json
export TRANSPORT_FALLBACK_API=off   # opendata alone here; the search.ch fallback: tests/trains/test_transport_callers.py
pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2" | head -30; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
has() { if [[ "$2" == *"$3"* ]]; then ok "$1"; else no "$1" "$2"; fi; }
hasnt() { if [[ "$2" == *"$3"* ]]; then no "$1" "$2"; else ok "$1"; fi; }
mode() { echo "$1" > "$FAKE/mode"; rm -f "$FAKE/requests.log"; }
answer() { printf '%s' "$1" > "$FAKE/answer"; }
run() { "$RT" "$@" > "$T/out" 2> "$T/err"; rc=$?; out=$(cat "$T/out"); err=$(cat "$T/err"); }
req() { cat "$FAKE/requests.log" 2>/dev/null; }
# the query of the last GET as sorted key=value lines (decoded)
q() { python3 -c 'import sys, urllib.parse as u
p = sys.stdin.read().strip().splitlines()[-1].split(" ", 1)[1]
print(u.urlsplit(p).path); [print(f"{k}={v}") for k, v in sorted(u.parse_qsl(u.urlsplit(p).query))]' < "$FAKE/requests.log"; }

# ---- fixture (live shape): A = address start, walk 13 min, S-Bahn; B = transfer, nulls, walk to a location only
answer '{"connections": [
 {"from": {"departure": "2026-10-15T08:17:00+0200", "station": {"id": "8573692", "name": "Winterthur, Bachtelstrasse"}},
  "to": {"arrival": "2026-10-15T08:46:00+0200", "station": {"id": "8503016", "name": "Zürich Flughafen"}},
  "duration": "00d00:29:00", "transfers": 0, "sections": [
   {"walk": {"duration": 780}, "journey": null,
    "departure": {"departure": "2026-10-15T08:17:00+0200", "platform": null, "station": {"id": "8573692", "name": "Winterthur, Bachtelstrasse"}, "location": {"name": "Winterthur, Bachtelstrasse"}},
    "arrival": {"arrival": "2026-10-15T08:30:00+0200", "station": {"id": "8506000", "name": "Winterthur"}, "location": {"name": "Winterthur"}}},
   {"walk": null, "journey": {"name": "018012", "category": "S", "subcategory": null, "categoryCode": null, "number": "24", "operator": "SBB", "to": "Zürich Flughafen", "passList": []},
    "departure": {"departure": "2026-10-15T08:30:00+0200", "platform": "4", "station": {"id": "8506000", "name": "Winterthur"}},
    "arrival": {"arrival": "2026-10-15T08:46:00+0200", "platform": "2", "station": {"id": "8503016", "name": "Zürich Flughafen"}}}]},
 {"from": {"departure": "2026-10-15T09:05:00+0200", "station": {"name": "Exampletown"}},
  "to": {"arrival": "2026-10-15T10:12:00+0200", "station": {"name": "Zürich, Kunsthaus"}},
  "duration": "00d01:07:00", "transfers": 1, "sections": [
   {"walk": null, "journey": {"category": "IR", "number": "36", "to": "Basel SBB"},
    "departure": {"departure": "2026-10-15T09:05:00+0200", "platform": null, "station": {"name": "Exampletown"}},
    "arrival": {"arrival": "2026-10-15T09:50:00+0200", "station": {"name": "Zürich HB"}}},
   {"walk": {"duration": null}, "journey": null,
    "departure": {"departure": null, "station": {"name": "Zürich HB"}},
    "arrival": {"arrival": null, "station": null, "location": {"name": "Zürich, Löwenplatz"}}},
   {"walk": null, "journey": {"category": "T", "number": null, "to": null},
    "departure": {"departure": "2026-10-15T10:00:00+0200", "platform": "C", "station": {"name": "Zürich, Löwenplatz"}},
    "arrival": {"arrival": "2026-10-15T10:12:00+0200", "station": {"name": "Zürich, Kunsthaus"}}},
   {"walk": {"duration": 240}, "journey": null, "departure": {"departure": null}, "arrival": {"arrival": null, "station": null, "location": null}},
   {"walk": null, "journey": null, "departure": {"departure": null}, "arrival": {"arrival": null}}]}
], "from": {}, "to": {}, "stations": {}}'
FIX=$(cat "$FAKE/answer")
WANT="08:17 → 08:46  (00:29, 0 transfers)
   walk 13 min → Winterthur
   08:30 Winterthur [pl 4] → 08:46 Zürich Flughafen  S24 → Zürich Flughafen

09:05 → 10:12  (01:07, 1 transfers)
   09:05 Exampletown [pl -] → 09:50 Zürich HB  IR36 → Basel SBB
   walk 0 min → Zürich, Löwenplatz
   10:00 Zürich, Löwenplatz [pl C] → 10:12 Zürich, Kunsthaus  T → ?
   walk 4 min → "

echo "# output"
mode ok; run "Winterthur, Bachtelstrasse" "Zürich Flughafen"
eq "rc 0" "$rc" "0"
eq "stderr empty" "$err" ""
eq "exact output (walks, journeys, platforms, nulls, empty section skipped)" "$out" "$WANT"
eq "blank line after each connection" "$(grep -c '^$' "$T/out")" "2"
eq "one request" "$(req | wc -l)" "1"

echo "# query parameters"
eq "path + defaults (date = today, limit 4)" "$(q)" "/v1/connections
date=$(date +%F)
from=Winterthur, Bachtelstrasse
limit=4
to=Zürich Flughafen"
mode ok; run "Exampletown, Examplestrasse" "Otherstrasse 2, Wallisellen" --arrive 17:00
eq "--arrive -> time + isArrivalTime=1" "$(q | grep -E '^(time|isArrivalTime)=')" "isArrivalTime=1
time=17:00"
mode ok; run A B --at 15:50
eq "--at -> time, no isArrivalTime" "$(q | grep -E '^(time|isArrivalTime)=')" "time=15:50"
mode ok; run A B --at 15:50 --arrive 17:00
eq "--arrive wins over --at" "$(q | grep -E '^(time|isArrivalTime)=')" "isArrivalTime=1
time=17:00"
mode ok; run A B --date 2026-12-24 -n 2
eq "--date and -n" "$(q | grep -E '^(date|limit)=')" "date=2026-12-24
limit=2"
mode ok; run "Zürich, Löwenplatz" "Genève-Aéroport"
eq "umlauts/accents/commas round-trip" "$(q | grep -E '^(from|to)=')" "from=Zürich, Löwenplatz
to=Genève-Aéroport"
has "raw query is percent-encoded (no bare spaces)" "$(req)" "from=Z%C3%BCrich%2C+L%C3%B6wenplatz"

echo "# no connections"
answer '{"connections": [], "from": null, "to": null}'; mode ok; run Nowhere Elsewhere
eq "rc 1" "$rc" "1"
eq "message" "$err" "no connections found (check spelling; try a nearby stop name)"
eq "no stdout" "$out" ""
answer '{"from": null}'; mode ok; run Nowhere Elsewhere
eq "no 'connections' key: same message" "$rc|$err" "1|no connections found (check spelling; try a nearby stop name)"

echo "# API and network errors: one line, exit 1, no traceback"
answer '{"errors": [{"message": "Invalid date format"}]}'; mode "code 400"; run A B
eq "HTTP 400 with API message" "$rc|$err" "1|route: transport API error: Invalid date format"
answer '{"errors": [{"message": "The router is not reachable"}]}'; mode ok; run A B
eq "200 with an errors payload" "$rc|$err" "1|route: transport API error: The router is not reachable"
: > "$FAKE/requests.log"; run A B --date 15.10.2026
eq "bad --date: one line, no request" "$rc|$err|$(req)" "1|route: --date YYYY-MM-DD, --at/--arrive HH:MM|"
answer '<html><body>Bad Gateway</body></html>'; mode "code 502"; run A B
eq "HTTP 502 with HTML" "$rc|$err" "1|route: transport API error: HTTP 502"
answer '<html>maintenance</html>'; mode ok; run A B
eq "200 with junk: rc 1" "$rc" "1"
has "200 with junk: clean message" "$err" "route: transport API unreachable or broken:"
hasnt "200 with junk: no traceback" "$err" "Traceback"
mode close; run A B
eq "dropped connection: rc 1" "$rc" "1"
has "dropped connection: clean message" "$err" "route: transport API unreachable or broken:"
hasnt "dropped connection: no traceback" "$err" "Traceback"
ROUTE_API=http://127.0.0.1:1/v1 run A B
eq "unreachable: rc 1, one line" "$rc|$(printf '%s\n' "$err" | wc -l)" "1|1"
has "unreachable: says why" "$err" "route: transport API unreachable or broken: [Errno 111] Connection refused"
answer "$FIX"

echo "# arguments"
mode ok; run A B -n two
eq "-n must be a number: argparse rc 2" "$rc" "2"
eq "no request on bad arguments" "$(req)" ""
run A
eq "missing destination: rc 2" "$rc" "2"
has "usage shown" "$err" "usage:"

echo "# default URL (no ROUTE_API): only the mock proxy is reachable"
mode ok; env -u ROUTE_API "$RT" A B > "$T/out" 2> "$T/err"; rc=$?
eq "goes to transport.opendata.ch:443 (via the mock proxy)" "$(req)" "CONNECT transport.opendata.ch:443"
eq "refused tunnel: rc 1" "$rc" "1"
has "refused tunnel: clean message" "$(cat "$T/err")" "route: transport API unreachable or broken:"
has "path /v1/connections in the source" "$(cat "$RT")" "'https://transport.opendata.ch/v1'"

echo
# ---- home: "home", the town, an alias or its stop on the home street -> the profile address; --literal opts out
mode ok; run home "Zürich HB"
eq "home: rc" "$rc" 0
has "home: from = the address (geocoder off)" "$(q)" "from=1234 Testdorf, Probeweg 7"
answer '{"connections": [{"from": {"departure": "2026-10-15T23:07:00+0200", "station": {"name": "Zürich HB"}},
 "to": {"arrival": "2026-10-16T00:14:00+0200", "location": {"name": "Testdorf, Irgendwo 1"}},
 "duration": "00d01:07:00", "transfers": 0, "sections": [
  {"walk": null, "journey": {"category": "S", "number": "9", "to": "Testdorf"},
   "departure": {"departure": "2026-10-15T23:07:00+0200", "platform": "41/42", "station": {"name": "Zürich HB"}},
   "arrival": {"arrival": "2026-10-15T23:57:00+0200", "station": {"name": "Testdorf"}}},
  {"walk": {"duration": 1020}, "journey": null,
   "departure": {"departure": "2026-10-15T23:57:00+0200", "station": {"name": "Testdorf"}},
   "arrival": {"arrival": "2026-10-16T00:14:00+0200", "station": null, "location": {"name": "Testdorf, Irgendwo 1"}}}]}]}'
mode ok; run "Zürich HB" Testdorp
has "alias as destination: to = the address" "$(q)" "to=1234 Testdorf, Probeweg 7"
has "walk to the door shows as home" "$out" "walk 17 min → home"
mode ok; run "Zürich HB" "Zürich, Kunsthaus"
has "no home label for other places" "$out" "walk 17 min → Testdorf, Irgendwo 1"
mode ok; run --literal Testdorf "Zürich HB"
has "--literal: the town as typed" "$(q)" "from=Testdorf"
python3 -c 'import hashlib, json, sys
json.dump({"key": hashlib.sha256(b"Probeweg 7, 1234 Testdorf").hexdigest()[:16], "lat": 47.5, "lon": 8.5}, open(sys.argv[1], "w"))' "$T/home-place.json"
mode ok; run "Testdorf, Probeweg" "Zürich HB"
has "home stop -> cached coordinates" "$(q)" "from=47.500000,8.500000"
mode ok; run "Testdorf Süd" "Zürich HB"
has "a similar stop stays a stop" "$(q)" "from=Testdorf Süd"
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
