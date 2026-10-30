#!/usr/bin/env bash
# Offline tests for ~/bin/browser, the CLI of his headed Chrome in claude-hub-browser. Exit 0 = all pass.
# browser hands every command (all but --help) to browser-hub beside its own file. Part 1 runs a copy of browser beside
# a fake browser-hub (arguments, exit codes, a symlinked browser, help, a missing browser-hub). Part 2 runs it beside a
# copy of the real browser-hub against a fake claude-hub-browser on loopback: one HTTP server playing both the control
# API (BROWSER_HUB_CTL: status, restart, screen, view, audit; bearer token from BROWSER_HUB_CTL_TOKEN) and the CDP gate
# (BROWSER_HUB_CDP: /json/*), plus the noVNC page that `browser link` checks. Never touches the real browser: HOME,
# the token, the state dir (BROWSER_HUB_STATE) and the MCP config (CLAUDE_JSON) are temp files, and usage and
# profile-get beside the copy are fakes. Every request the fake gets is logged, so a command that went elsewhere
# fails its check; the real state and screenshot dirs are checked untouched at the end.
# BROWSER_BIN=<path> tests another copy; BROWSER_HUB_BIN=<path> another browser-hub (default: the one beside
# BROWSER_BIN, else this tree's scripts/browser-hub; under CI only the copy under test is staged).
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
TREE=${ASSISTANT_CI_TREE:-$(cd "$HERE/../../.." && pwd)}
B=${BROWSER_BIN:-/home/node/bin/browser}
BDIR=$(dirname "$(readlink -f "$B")")
HUB=${BROWSER_HUB_BIN:-}
[ -z "$HUB" ] && { [ -f "$BDIR/browser-hub" ] && HUB=$BDIR/browser-hub || HUB=$TREE/scripts/browser-hub; }
PY=/usr/local/bin/python3.13
OUTER_HOME=$HOME
T=$(mktemp -d "${TMPDIR:-/tmp}/browser-tests.XXXXXX")
export FAKE=$T/fake
SRV=
cleanup() { [ -n "$SRV" ] && kill "$SRV" 2>/dev/null; rm -rf "$T"; }
trap cleanup EXIT
mkdir -p "$FAKE" "$T/home"

# the real dirs browser-hub writes by default (its state, the default screenshot dir) under the HOME this suite was
# started with and the host's: path, mtime and size of every file
REAL_HOMES=("$OUTER_HOME"); [ "$OUTER_HOME" != /home/node ] && REAL_HOMES+=(/home/node)
real_snap() {
  local h
  for h in "${REAL_HOMES[@]}"; do
    find "$h/.local/state/browser" "$h/reports/browser" -maxdepth 1 -type f -printf '%p %T@ %s\n' 2>/dev/null
  done | sort
}
real_before=$(real_snap)

pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2" | head -30; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
has() { if [[ "$2" == *"$3"* ]]; then ok "$1"; else no "$1" "$2"; fi; }
hasnt() { if [[ "$2" == *"$3"* ]]; then no "$1" "$2"; else ok "$1"; fi; }
lines() { [ -f "$1" ] && wc -l < "$1" | tr -d ' ' || echo 0; }

[ -f "$B" ] || { echo "FAIL - no browser at $B"; exit 1; }
[ -f "$HUB" ] || { echo "FAIL - no browser-hub at $HUB"; exit 1; }
# the copies under test: without the managed header line (as in the repo) and with it (as deployed)
grep -v '^# managed: ' "$B" > "$T/browser.plain"
{ head -n 1 "$T/browser.plain"; echo '# managed: ~/opt/assistant — edit there, deploy with assistant land'; tail -n +2 "$T/browser.plain"; } > "$T/browser.managed"

echo "# browser: hands every command to browser-hub beside it"
mkdir -p "$T/wrap" "$T/link" "$T/lone"
cp "$T/browser.managed" "$T/wrap/browser"; chmod 0755 "$T/wrap/browser"
printf '%s\n' '#!/bin/bash' 'printf "%s\n" "$@" > "$FAKE/hub.args"; echo "hub out"; echo "hub err" >&2; exit "${FAKE_RC:-0}"' \
  > "$T/wrap/browser-hub"
chmod 0755 "$T/wrap/browser-hub"
out=$("$T/wrap/browser" open "https://example.org/x?a=1&b=two words" 2>"$T/err"); rc=$?
eq "open: rc of browser-hub" "$rc" "0"
eq "open: arguments verbatim, one each" "$(cat "$FAKE/hub.args")" "open
https://example.org/x?a=1&b=two words"
eq "browser-hub's stdout and stderr pass through" "$out|$(cat "$T/err")" "hub out|hub err"
for c in status ensure start restart stop screenshot url "view on 5" link update audit prep "mcp status" bogus; do
  rm -f "$FAKE/hub.args"
  # shellcheck disable=SC2086  # $c is the command and its words
  "$T/wrap/browser" $c >/dev/null 2>&1
  eq "browser $c: handed to browser-hub as is" "$(tr '\n' ' ' 2>/dev/null < "$FAKE/hub.args")" "$c "
done
for want in 1 2 3; do
  FAKE_RC=$want "$T/wrap/browser" status >/dev/null 2>&1; rc=$?
  eq "browser-hub's exit $want is browser's" "$rc" "$want"
done
ln -s "$T/wrap/browser" "$T/link/browser"
rm -f "$FAKE/hub.args"
"$T/link/browser" url >/dev/null 2>&1; rc=$?
eq "called through a symlink: browser-hub beside the real file" "$rc $(cat "$FAKE/hub.args" 2>/dev/null)" "0 url"
cp "$T/browser.plain" "$T/lone/browser"; chmod 0755 "$T/lone/browser"
err=$("$T/lone/browser" status 2>&1); rc=$?
eq "no browser-hub beside it: rc 1" "$rc" "1"
has "no browser-hub beside it: says where it looked" "$err" "browser: browser-hub is missing ($(readlink -f "$T/lone")/browser-hub)"

echo "# browser --help"
rm -f "$FAKE/hub.args"
help=$("$T/wrap/browser" --help); rc=$?
eq "--help: rc 0" "$rc" "0"
[ -e "$FAKE/hub.args" ] && no "--help: browser-hub not run" "$(cat "$FAKE/hub.args")" || ok "--help: browser-hub not run"
eq "-h and help: the same text" "$("$T/wrap/browser" -h)|$("$T/wrap/browser" help)" "$help|$help"
has "--help: what it is" "$(head -n 1 <<<"$help")" "browser: his real, headed Google Chrome"
hasnt "--help: no managed header line" "$help" "managed:"
has "--help: the whole block, to its last line" "$(tail -n 1 <<<"$help")" "browser-hub --help"
eq "--help: the same with or without the managed header" "$(bash "$T/browser.plain" --help)" "$help"
hasnt "--help: no comment marks left" "$help" "# "
has "--help lists the view" "$help" "browser view on [MIN]|off|status"
# the commands its help names, as browser-hub must know them (part 2 runs each)
mapfile -t HELP_CMDS < <(sed -n 's/^  browser \([a-z|]*\).*/\1/p' <<<"$help" | tr '|' '\n')
eq "--help names 11 commands" "${#HELP_CMDS[@]}" "11"

echo "# the fake claude-hub-browser"
cat > "$T/fakehub.py" <<'PY'
import json, os, sys, time, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
FAKE, TOK = sys.argv[1], sys.argv[2]
view = {"on": False, "until": None, "conns": 4242}

def flag(name):
    return os.path.exists(os.path.join(FAKE, name))

class H(BaseHTTPRequestHandler):
    def send(self, body, ctype="application/json", code=200):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def handle_one(self, method):
        auth = self.headers.get("Authorization")
        who = "token" if auth == f"Bearer {TOK}" else "none" if auth is None else "bad"
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode() if n else ""
        with open(os.path.join(FAKE, "req.log"), "a") as f:
            f.write(f"{method} {self.path} {who}" + (f" {body}" if body else "") + "\n")
        path, _, query = self.path.partition("?")
        if path == "/vnc.html" and method == "GET":
            return self.send(b"<html>noVNC</html>", "text/html", 404 if flag("novnc-broken") else 200)
        if path.startswith("/json/"):  # the CDP gate (no token: the gate checks commits itself)
            if path == "/json/version":
                return self.send({"Browser": "Chrome/999.0.0.0", "webSocketDebuggerUrl": "ws://127.0.0.1/x"})
            if path == "/json/list":
                return self.send([{"type": "page", "title": "Example Domain", "url": "https://example.org/"},
                                  {"type": "service_worker", "title": "SW", "url": "https://example.org/sw.js"},
                                  {"type": "page", "title": "", "url": "about:blank"}])
            if path == "/json/new" and method == "PUT":
                return self.send({"id": "T1", "url": urllib.parse.unquote(query)})
            return self.send({"error": "no"}, code=404)
        if who != "token":
            return self.send({"error": "unauthorized"}, code=401)
        chrome = "down" if flag("chrome-down") else "up"
        if path == "/status" and method == "GET":
            view["conns"] = 4242 + (1 if flag("view-opened") else 0)
            return self.send({"chrome": chrome, "version": "Chrome/999.0.0.0", "svc": {"up": True, "mode": "enforce"},
                              "view": view})
        if path == "/chrome/restart" and method == "POST":
            return self.send({"chrome": chrome, "version": "Chrome/999.0.0.0"})
        if path == "/screen" and method == "GET":
            return self.send(b"\x89PNG fake\n", "image/png")
        if path == "/view" and method == "POST":
            a = json.loads(body or "{}")
            view.update(on=bool(a.get("on")), until=time.time() + 60 * a.get("minutes", 0) if a.get("on") else None)
            return self.send(view)
        if path == "/audit" and method == "GET":
            return self.send({"hours": urllib.parse.parse_qs(query).get("hours", ["?"])[0], "commits_token": 3,
                              "denied": 1, "unguarded": 2 if flag("unguarded") else 0})
        return self.send({"error": "no route"}, code=404)

    def do_GET(self):  # noqa: N802
        self.handle_one("GET")

    def do_POST(self):  # noqa: N802
        self.handle_one("POST")

    def do_PUT(self):  # noqa: N802
        self.handle_one("PUT")

    def log_message(self, *a):
        pass

srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
with open(os.path.join(FAKE, "port.tmp"), "w") as f:
    f.write(str(srv.server_address[1]))
os.replace(os.path.join(FAKE, "port.tmp"), os.path.join(FAKE, "port"))
srv.serve_forever()
PY
TOKEN=t0k-$RANDOM$RANDOM
"$PY" -I "$T/fakehub.py" "$FAKE" "$TOKEN" & SRV=$!
for _ in $(seq 100); do [ -s "$FAKE/port" ] && break; sleep 0.1; done
P=$(cat "$FAKE/port" 2>/dev/null)
[ -n "$P" ] || { echo "FAIL - the fake claude-hub-browser did not start"; exit 1; }
CLOSED=$("$PY" -I -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')

# the copies under test: browser beside a copy of browser-hub, its lib (core, browsersvc), fake usage and profile-get
mkdir -p "$T/bin/lib"
cp "$T/browser.managed" "$T/bin/browser"; cp "$HUB" "$T/bin/browser-hub"; chmod 0755 "$T/bin/browser" "$T/bin/browser-hub"
HUBDIR=$(dirname "$(readlink -f "$HUB")")
for pkg in core browsersvc; do
  for d in "$BDIR/lib/$pkg" "$HUBDIR/lib/$pkg" "$TREE/$pkg" "$TREE/packages/$pkg"; do
    [ -f "$d/__init__.py" ] && { ln -s "$(readlink -f "$d")" "$T/bin/lib/$pkg"; break; }
  done
done
printf '%s\n' '#!/bin/bash' 'printf "%s\n" "$*" >> "$FAKE/usage.log"' > "$T/bin/usage"
printf '%s\n' '#!/bin/bash' '[ "$1" = --feature ] && { echo "${FAKE_FEATURE:-on}"; [ "${FAKE_FEATURE:-on}" = on ]; exit; }' \
  'echo "$*" >> "$FAKE/profile.args"; [ "$1" = network.server_host ] && cat "$FAKE/host" 2>/dev/null' \
  > "$T/bin/profile-get"
chmod 0755 "$T/bin/usage" "$T/bin/profile-get"
export HOME=$T/home BROWSER_HUB_CTL=http://127.0.0.1:$P BROWSER_HUB_CDP=http://127.0.0.1:$P \
       BROWSER_HUB_CTL_TOKEN=$T/home/.secrets/browser-ctl-token BROWSER_HUB_STATE=$T/state BROWSER_VIEW_PORT=$P \
       CLAUDE_JSON=$T/home/.claude.json no_proxy=127.0.0.1,localhost NO_PROXY=127.0.0.1,localhost
unset BROWSER_LINK_HOST ASSISTANT_LIB
ST=$BROWSER_HUB_STATE
br() { timeout 60 "$T/bin/browser" "$@"; }
reqs() { cat "$FAKE/req.log" 2>/dev/null; }
since() { tail -n +"$(( $1 + 1 ))" "$FAKE/req.log" 2>/dev/null; }   # requests after the first N

echo "# no control token yet"
out=$(br status); rc=$?
eq "status without a token: rc 3" "$rc" "3"
has "status without a token: says so" "$out" "claude-hub-browser: no control token (browser-hub prep)"
has "status: clients' CDP route on the gate" "$out" "clients -> http://127.0.0.1:$P (hub)"
err=$(br ensure 2>&1); rc=$?
eq "ensure without a token: rc 3, says so" "$rc $err" "3 browser-hub: no control token (browser-hub prep)"
eq "nothing sent to the control API without a token" "$(grep -vc '^GET /json/version ' <<<"$(reqs)")" "0"
out=$(br prep); rc=$?
eq "prep: rc 0" "$rc" "0"
has "prep: made the token" "$out" "control token made"
eq "prep: token mode 600, 64 hex chars" "$(stat -c %a "$BROWSER_HUB_CTL_TOKEN") $(tr -d '\n' < "$BROWSER_HUB_CTL_TOKEN" | wc -c)" "600 64"
tok_made=$(cat "$BROWSER_HUB_CTL_TOKEN")
has "prep again: kept" "$(br prep)" "control token kept"
eq "prep again: the same token" "$(cat "$BROWSER_HUB_CTL_TOKEN")" "$tok_made"
printf '%s\n' "$TOKEN" > "$BROWSER_HUB_CTL_TOKEN"

echo "# status, ensure, start, stop"
n=$(lines "$FAKE/req.log")
out=$(br status); rc=$?
eq "status up: rc 0" "$rc" "0"
eq "status up: route and container" "$out" "clients -> http://127.0.0.1:$P (hub)
claude-hub-browser: chrome up (Chrome/999.0.0.0) · service up (enforce) · view off"
eq "status: the control API's /status with the token, then the gate's /json/version" "$(since "$n")" "GET /status token
GET /json/version none"
out=$(br status --json); rc=$?
eq "status --json: rc 0, route and container" "$rc $("$PY" -I -c 'import json, sys; d = json.loads(sys.argv[1]); print(d["route"]["cdp"], d["route"]["ok"], d["container"]["chrome"], d["container"]["svc"]["mode"])' "$out")" \
  "0 http://127.0.0.1:$P True up enforce"
out=$(BROWSER_HUB_CDP=http://127.0.0.1:$CLOSED br status)
has "status, gate down: the route says so" "$out" "clients -> http://127.0.0.1:$CLOSED (hub (down))"
out=$(BROWSER_HUB_CTL=http://127.0.0.1:$CLOSED br status); rc=$?
eq "status, control API down: rc 3" "$rc" "3"
has "status, control API down: unreachable" "$out" "claude-hub-browser: claude-hub-browser unreachable"
br ensure; eq "ensure, Chrome up: rc 0" "$?" "0"
touch "$FAKE/chrome-down"
br ensure; eq "ensure, Chrome down: rc 1" "$?" "1"
out=$(br status); rc=$?
eq "status, Chrome down: rc 1" "$rc" "1"
has "status, Chrome down: says so" "$out" "chrome down (Chrome/999.0.0.0)"
out=$(br start); rc=$?
eq "start, Chrome stays down: rc 1" "$rc" "1"
rm -f "$FAKE/chrome-down"
BROWSER_HUB_CTL=http://127.0.0.1:$CLOSED br ensure 2>/dev/null; eq "ensure, control API down: rc 3" "$?" "3"
n=$(lines "$FAKE/req.log")
out=$(br start); rc=$?
eq "start: rc 0, Chrome restarted in the container" "$rc $out" "0 chrome up (Chrome/999.0.0.0) in claude-hub-browser"
out=$(br restart); rc=$?
eq "restart: rc 0" "$rc" "0"
eq "start and restart: POST /chrome/restart with the token" "$(since "$n")" "POST /chrome/restart token {}
POST /chrome/restart token {}"
n=$(lines "$FAKE/req.log")
out=$(br stop); rc=$?
eq "stop: refused, rc 2" "$rc" "2"
has "stop: says why" "$out" "refused: Chrome runs in claude-hub-browser and stays up there"
eq "stop: nothing sent" "$(lines "$FAKE/req.log")" "$n"

echo "# url, open, screenshot"
n=$(lines "$FAKE/req.log")
out=$(br url); rc=$?
eq "url: rc 0, pages only (title | URL)" "$rc|$out" "0|Example Domain | https://example.org/
 | about:blank"
eq "url: GET /json/list on the gate" "$(since "$n")" "GET /json/list none"
eq "tabs: the same as url" "$(br tabs)" "$out"
out=$(BROWSER_HUB_CDP=http://127.0.0.1:$CLOSED br url); rc=$?
eq "url, gate down: rc 3" "$rc" "3"
has "url, gate down: says so" "$out" "the service does not answer"
n=$(lines "$FAKE/req.log")
out=$(br open "https://example.org/x?a=1&b=2"); rc=$?
eq "open: rc 0, says so" "$rc $out" "0 opened https://example.org/x?a=1&b=2"
eq "open: PUT /json/new on the gate, the URL as the query" "$(since "$n")" "PUT /json/new?https://example.org/x?a=1&b=2 none"
n=$(lines "$FAKE/req.log")
err=$(br open 2>&1); rc=$?
eq "open without URL: rc 2" "$rc" "2"
has "open without URL: usage" "$err" "usage: browser-hub open URL"
eq "open without URL: nothing sent" "$(lines "$FAKE/req.log")" "$n"
out=$(BROWSER_HUB_CDP=http://127.0.0.1:$CLOSED br open https://example.org/); rc=$?
eq "open, gate down: rc 1" "$rc" "1"
out=$(br screenshot "$T/shots/a.png"); rc=$?
eq "screenshot to a file: rc 0, prints the path" "$rc $out" "0 $T/shots/a.png"
eq "screenshot: the container's PNG" "$(head -c 9 "$T/shots/a.png" | tail -c 8)" "PNG fake"
eq "screenshot: GET /screen with the token" "$(tail -n 1 "$FAKE/req.log")" "GET /screen token"
out=$(br screenshot)
has "screenshot default path" "$out" "$HOME/reports/browser/screen-"
[ -s "$out" ] && ok "screenshot default file written" || no "screenshot default file written" "$out"
out=$(br shot "$T/shots/b.png"); eq "shot: the same as screenshot" "$out" "$T/shots/b.png"
out=$(BROWSER_HUB_CTL=http://127.0.0.1:$CLOSED br screenshot "$T/shots/c.png"); rc=$?
eq "screenshot, control API down: rc 3, no file" "$rc $([ -e "$T/shots/c.png" ] && echo file || echo none)" "3 none"

echo "# owner view"
n=$(lines "$FAKE/req.log")
out=$(br view on 2); rc=$?
eq "view on 2: rc 0" "$rc" "0"
has "view on: until when, where, the password asked by noVNC" "$out" "(claude-hub-browser; noVNC asks for the VNC password)"
eq "view on 2: POST /view on for 2 min" "$(since "$n")" 'POST /view token {"on": true, "minutes": 2}'
has "view status: on" "$(br view status)" "view: on until "
has "view (no subcommand): status" "$(br view)" "view: on until "
out=$(br view off); rc=$?
eq "view off: rc 0, says so" "$rc $out" "0 view: off"
eq "view off: POST /view off" "$(grep '^POST /view' "$FAKE/req.log" | tail -n 1)" 'POST /view token {"on": false}'
eq "view status: off" "$(br view status)" "view: off"
br view on >/dev/null
eq "view on: 60 min by default" "$(grep '^POST /view' "$FAKE/req.log" | tail -n 1)" 'POST /view token {"on": true, "minutes": 60}'
br view off >/dev/null
n=$(lines "$FAKE/req.log")
err=$(br view on abc 2>&1); rc=$?
eq "view on abc: rc 2" "$rc" "2"
has "view on abc: usage" "$err" "usage: browser-hub view on [MIN]|off|status"
err=$(br view bogus 2>&1); rc=$?
eq "view bogus: rc 2" "$rc" "2"
has "view bogus: usage" "$err" "usage: browser-hub view on [MIN]|off|status"
eq "view usage errors: nothing sent" "$(lines "$FAKE/req.log")" "$n"

echo "# feature switch"
out=$(FAKE_FEATURE=off br status 2>&1); rc=$?
eq "features.browser off: exit 3, says so" "$rc $out" "3 browser: feature browser is off (features.browser in the profile)"
echo "# link"
echo 127.0.0.1 > "$FAKE/host"
out=$(br link --json); rc=$?
eq "link --json: rc 0" "$rc" "0"
eq "link --json: URL on the profile's host, checked to load, the container" \
  "$("$PY" -I -c 'import json, sys; d = json.loads(sys.argv[1]); print(d["url"], d["verified"], d["http"], d["view_minutes_left"], d["where"])' "$out" 2>&1)" \
  "http://127.0.0.1:$P/vnc.html?autoconnect=1&resize=scale&reconnect=1 True 200 30 claude-hub-browser"
has "link: the password is never in the URL" "$out" '"password": "noVNC asks for the VNC password (never in the URL)"'
eq "link: host from profile-get network.server_host" "$(cat "$FAKE/profile.args")" "network.server_host"
eq "link: view on for 30 min" "$(grep '^POST /view' "$FAKE/req.log" | tail -n 1)" 'POST /view token {"on": true, "minutes": 30}'
eq "link: the noVNC page checked" "$(grep -c '^GET /vnc.html none' "$FAKE/req.log")" "1"
eq "link: the view's connections marked" "$("$PY" -I -c 'import json, sys; print(json.load(open(sys.argv[1]))["conns"])' "$ST/hub-link.json" 2>&1)" "4242"
has "link: usage logged" "$(cat "$FAKE/usage.log")" "log tg.browser.open exec --channel telegram --ms "
has "link: usage outcome ok, http 200" "$(cat "$FAKE/usage.log")" "--outcome ok --attr http=200"
n=$(lines "$FAKE/req.log")
out=$(br link); rc=$?
eq "link with 30 min left: rc 0, the URL" "$rc $out" "0 http://127.0.0.1:$P/vnc.html?autoconnect=1&resize=scale&reconnect=1"
hasnt "link with >= 10 min left: the view not turned on again" "$(since "$n")" "POST /view"
touch "$FAKE/view-opened"
br view off >/dev/null
has "view off after a link he opened: usage notify_opened" "$(tail -n 1 "$FAKE/usage.log")" "log tg.browser.open notify_opened --channel telegram --outcome opened --attr conns=1"
[ -e "$ST/hub-link.json" ] && no "view off: the link mark removed" || ok "view off: the link mark removed"
rm -f "$FAKE/view-opened"
br link >/dev/null; br view off >/dev/null
has "view off after a link he did not open: usage ignored" "$(tail -n 1 "$FAKE/usage.log")" "log tg.browser.open ignored --channel telegram --outcome unopened --attr conns=0"
n=$(lines "$FAKE/profile.args")
out=$(BROWSER_LINK_HOST=127.0.0.1 br link --min 5); rc=$?
eq "link --min 5: rc 0" "$rc" "0"
eq "link --min 5: view on for 5 min" "$(grep '^POST /view' "$FAKE/req.log" | tail -n 1)" 'POST /view token {"on": true, "minutes": 5}'
eq "BROWSER_LINK_HOST: profile-get not asked" "$(lines "$FAKE/profile.args")" "$n"
br view off >/dev/null
n=$(lines "$FAKE/req.log")
for bad in 0 241 abc; do
  err=$(br link --min "$bad" 2>&1); rc=$?
  eq "link --min $bad: rc 2, says the range" "$rc $err" "2 browser-hub: --min must be 1-240"
done
eq "link with a bad --min: nothing sent" "$(lines "$FAKE/req.log")" "$n"
: > "$FAKE/host"
err=$(br link 2>&1); rc=$?
eq "link without a host: rc 1" "$rc" "1"
has "link without a host: says so" "$err" "no host (profile network.server_host)"
br view off >/dev/null
echo 127.0.0.1 > "$FAKE/host"
touch "$FAKE/novnc-broken"
out=$(br link --json 2>/dev/null); rc=$?
eq "link, noVNC page not loading: rc 1, verified false" "$rc $("$PY" -I -c 'import json, sys; d = json.loads(sys.argv[1]); print(d["verified"], d["http"])' "$out" 2>&1)" "1 False 404"
rm -f "$FAKE/novnc-broken"
br view off >/dev/null

echo "# audit, update, mcp, unknown"
out=$(br audit); rc=$?
eq "audit: rc 0, the counts" "$rc $out" "0 hours 24 · commits_token 3 · denied 1 · unguarded 0"
eq "audit: GET /audit for 24 h" "$(tail -n 1 "$FAKE/req.log")" "GET /audit?hours=24 token"
br audit --hours 2 --json >/dev/null
eq "audit --hours 2" "$(tail -n 1 "$FAKE/req.log")" "GET /audit?hours=2 token"
touch "$FAKE/unguarded"
br audit >/dev/null; eq "audit with unguarded commits: rc 1" "$?" "1"
rm -f "$FAKE/unguarded"
err=$(br audit --hours x 2>&1); eq "audit --hours x: rc 2" "$?" "2"
n=$(lines "$FAKE/req.log")
out=$(br update); rc=$?
eq "update: rc 0" "$rc" "0"
has "update: Chrome is pinned in the image" "$out" "Chrome is pinned in claude-hub-browser's image (ops/hub/browser-pins.toml"
eq "update: nothing sent" "$(lines "$FAKE/req.log")" "$n"
printf '%s\n' '{"mcpServers": {"browser": {"args": ["cli.js", "--cdp-endpoint", "http://127.0.0.1:9222"]}}, "other": 1}' > "$CLAUDE_JSON"
chmod 600 "$CLAUDE_JSON"
eq "mcp status: the MCP's endpoint" "$(br mcp status)" "browser MCP --cdp-endpoint http://127.0.0.1:9222"
out=$(br mcp hub); rc=$?
eq "mcp hub: rc 0" "$rc" "0"
eq "mcp hub: the endpoint is the gate, the rest kept, mode kept" "$("$PY" -I -c 'import json, sys; d = json.load(open(sys.argv[1])); print(d["mcpServers"]["browser"]["args"][2], d["other"])' "$CLAUDE_JSON") $(stat -c %a "$CLAUDE_JSON")" \
  "http://127.0.0.1:$P 1 600"
has "mcp hub again: unchanged" "$(br mcp hub)" "(unchanged)"
br mcp local >/dev/null 2>&1; eq "mcp local: rc 2" "$?" "2"
err=$(br bogus 2>&1); rc=$?
eq "unknown command: rc 2" "$rc" "2"
has "unknown command: says so" "$err" "browser-hub: unknown command bogus"
for c in "${HELP_CMDS[@]}"; do
  case $c in open) a=(https://example.org/) ;; view) a=(status) ;; screenshot) a=("$T/shots/h.png") ;; *) a=() ;; esac
  err=$(br "$c" "${a[@]}" 2>&1 >/dev/null)
  hasnt "browser $c (from --help): known to browser-hub" "$err" "unknown command"
done
br view off >/dev/null

echo "# isolation"
eq "every request carried this run's token or none" "$(grep -vcE ' (token|none)( |$)' <<<"$(reqs)")" "0"
all_out=$(cat "$FAKE"/*.log "$FAKE"/*.args "$ST"/* 2>/dev/null)
hasnt "the token never in what browser-hub logged or passed on" "$all_out" "$TOKEN"
real_after=$(real_snap)
if [ "$real_after" = "$real_before" ]; then
  ok "real browser state and screenshot dirs untouched ($(grep -c . <<<"$real_before") files)"
else  # each file added, changed or gone: the host's live writers (the daily update cron, a real `browser link`) may
  # write its own dirs meanwhile, never with test data
  leak=
  while read -r f; do
    [ -n "$f" ] || continue
    case $f in
      /home/node/.local/state/browser/*|/home/node/reports/browser/*)
        grep -qaE "Chrome/999\.0\.0\.0|PNG fake|\"conns\": 424[23]|$TOKEN" "$f" 2>/dev/null && leak+="$f " ;;
      *) leak+="$f (no live writer there) " ;;
    esac
  done < <(comm -3 <(printf '%s\n' "$real_before") <(printf '%s\n' "$real_after") | sed 's/^\t//' | cut -d' ' -f1 | sort -u)
  if [ -n "$leak" ]; then no "real browser state and screenshot dirs untouched" "written by this run: $leak"
  else ok "real browser state and screenshot dirs untouched (changed by a live writer, no test data in it)"; fi
fi

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
