#!/bin/sh
# menu-usage tests: fixture journals (no PII), isolated state. MENU_USAGE_BIN=/copy to test a copy.
set -u
BIN="${MENU_USAGE_BIN:-$HOME/bin/menu-usage}"
F="$(cd "$(dirname "$0")" && pwd)/fixtures"
T="$(mktemp -d)"
trap 'find "${T:?}" -mindepth 1 -delete; rmdir "${T:?}"' EXIT
export MENU_USAGE_STATE="$T/state" MENU_USAGE_JOURNAL="$T/journal.jsonl" MENU_USAGE_INSTANT="$F/instant.json" \
  MENU_USAGE_COMMANDS="$F/commands.json" MENU_USAGE_TALKLOG="$F/talk.log" MENU_USAGE_MCP_DIR="$F/mcp" \
  MENU_USAGE_TZ=Europe/Zurich MENU_USAGE_NOW=2026-10-12T12:00:00Z MENU_USAGE_EVENTS_DB="$T/none.db"
fail=0; n=0
check() { n=$((n+1)); if [ "$2" = "$3" ]; then :; else echo "FAIL $1: got [$2] want [$3]"; fail=$((fail+1)); fi; }
q() { python3 -c "import json,sys; r=json.load(sys.stdin); print($1)"; }

cp "$F/journal1.jsonl" "$T/journal.jsonl"
"$BIN" collect; check "collect exit" $? 0
J=$("$BIN" --json --no-collect --days 30)
check "first run counts" "$(echo "$J" | q "sorted((c['command'],c['uses']) for c in r['commands'])")" \
  "[('bills', 1), ('continue', 2), ('gym', 1), ('space', 1), ('tonight', 1)]"
check "non-owner + plain text ignored" "$(echo "$J" | q "sum(c['uses'] for c in r['commands'])")" 6
check "session vs instant" "$(echo "$J" | q "[(c['instant'],c['session']) for c in r['commands'] if c['command']=='continue']")" "[(0, 2)]"
check "buttons" "$(echo "$J" | q "[(b['prefix'],b['presses'],b['sub']) for b in r['buttons']]")" \
  "[('call', 2, {'talk': 2}), ('mt', 1, {'play': 1}), ('rm', 1, {})]"
check "reactions" "$(echo "$J" | q "r['reactions']")" 1
check "counted since (backfill)" "$(echo "$J" | q "r['counted_since']")" 2026-10-05

"$BIN" collect
check "re-collect dedupes" "$("$BIN" --json --no-collect | q "sum(c['uses'] for c in r['commands'])")" 6

cp "$F/journal2.jsonl" "$T/journal.jsonl"
J=$("$BIN" --json --days 30)     # report collects first
check "rotated journal" "$(echo "$J" | q "sorted((c['command'],c['uses']) for c in r['commands'])")" \
  "[('bills', 2), ('continue', 2), ('gym', 1), ('home', 1), ('space', 1), ('tonight', 1)]"
check "never used" "$(echo "$J" | q "r['never_used']")" "['dive', 'rave']"
check "off menu" "$(echo "$J" | q "r['off_menu']")" "['home']"
check "talk tap = journal tap, recent tap waits" "$(echo "$J" | q "[b['presses'] for b in r['buttons'] if b['prefix']=='call']")" "[2]"
check "dl:score" "$(echo "$J" | q "[b['sub'] for b in r['buttons'] if b['prefix']=='dl']")" "[{'score': 1}]"

check "window 2 days (local dates)" "$("$BIN" --json --no-collect --days 2 | q "sorted((c['command'],c['uses']) for c in r['commands'])")" \
  "[('bills', 1), ('gym', 1), ('home', 1)]"
B=$("$BIN" --brief --no-collect --days 30)
check "brief line" "$(echo "$B" | grep -c '^Menu usage 30 d (since 05.10): 5/7 menu commands used; top /bills 2 · /continue 2.*never: /dive /rave$')" 1
"$BIN" --no-collect | grep -q '/home .*(not in menu)'; check "text report" $? 0

MENU_USAGE_NOW=2026-11-02T12:00:00Z "$BIN" collect
check "old ids pruned, not recounted" "$("$BIN" --json --no-collect --days 60 | MENU_USAGE_NOW=2026-11-02T12:00:00Z q "sum(c['uses'] for c in r['commands'])")" 8
check "late talk tap counted once settled" "$("$BIN" --json --no-collect --days 60 | q "[b['presses'] for b in r['buttons'] if b['prefix']=='call']")" "[3]"
check "seen pruned" "$(q "len(r['seen'])" < "$T/state/counts.json")" 0

check "no text stored" "$(grep -c -e SECRETPHRASE -e cozy -e hello "$T/state/counts.json")" 0
check "store 0600" "$(stat -c %a "$T/state/counts.json")" 600
"$BIN" --days 0 >/dev/null 2>&1; check "bad --days" $? 2

echo "# events ledger: from the cut-over on, tg-adapter rows; the menu from the bindings table"
export MENU_USAGE_STATE="$T/state2" MENU_USAGE_EVENTS_DB="$T/events.db" MENU_USAGE_NOW=2026-11-03T08:00:00Z MENU_USAGE_MCP_DIR="$T/nomcp"
cat > "$T/journal.jsonl" <<'J'
{"id": "e1", "at": "2026-11-03T07:30:00Z", "kind": "message", "owner": true, "ts": "2026-11-03T07:30:00Z", "content": "/gym", "meta": {}}
{"id": "e2", "at": "2026-11-03T09:00:00Z", "kind": "instant", "owner": true, "ts": "2026-11-03T09:00:00Z", "content": "/bills (answered instantly by the plugin; no reply needed).", "meta": {}}
J
python3 - "$T/events.db" <<'PY'
import json, sqlite3, sys, datetime as dt
con = sqlite3.connect(sys.argv[1])
con.executescript("""CREATE TABLE events(id INTEGER PRIMARY KEY, ts REAL, source TEXT, kind TEXT, payload TEXT, ack_by TEXT);
CREATE TABLE bindings(id INTEGER PRIMARY KEY, kind TEXT, key TEXT, tool TEXT, active INTEGER, expires REAL);
CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);""")
ts = lambda s: dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
rows = [("2026-11-03T07:00:00Z", "tg-adapter", "tg.message", {"text": "/space"}, "binding"),     # before the cut-over
        ("2026-11-03T09:00:00Z", "tg-adapter", "tg.message", {"text": "/bills"}, "binding"),
        ("2026-11-03T09:01:00Z", "tg-adapter", "tg.message", {"text": "/lights now"}, None),
        ("2026-11-03T09:02:00Z", "tg-adapter", "tg.message", {"text": "cozy hello"}, None),
        ("2026-11-03T09:03:00Z", "tg-adapter", "tg.button", {"callback_data": "rm:s1h:abc"}, "binding"),
        ("2026-11-03T09:04:00Z", "tg-adapter", "tg.reaction", {"reaction": "x"}, None),
        ("2026-11-03T09:05:00Z", "vault-events", "note.vault", {"text": "/bills"}, None)]
for t, src, kind, p, ack in rows:
    con.execute("INSERT INTO events(ts, source, kind, payload, ack_by) VALUES (?,?,?,?,?)", (ts(t), src, kind, json.dumps(p), ack))
con.execute("INSERT INTO bindings(kind, key, tool, active) VALUES ('command','bills','tg-cmd.bills',1),('command','space','x',1),('command','gone','x',0),('button','vf:fixed','x',1)")
con.execute("INSERT INTO meta VALUES ('commands:somebot', ?)", (json.dumps([{"command": "lights", "description": "L"}]),))
con.commit()
PY
"$BIN" collect; check "events: collect exit" $? 0
J=$("$BIN" --json --no-collect --days 30)
check "events: cut-over set, journal before it, ledger after it" "$(echo "$J" | q "sorted((c['command'],c['instant'],c['session']) for c in r['commands'] if c['uses'])")" \
  "[('bills', 1, 0), ('gym', 0, 1), ('lights', 0, 1)]"
check "events: menu = command rows + the set_commands list" "$(echo "$J" | q "r['never_used']")" "['space']"
check "events: button + reaction" "$(echo "$J" | q "([(b['prefix'],b['presses']) for b in r['buttons'] if b['prefix']=='rm'], r['reactions'])")" "([('rm', 1)], 1)"
"$BIN" collect
check "events: id watermark, no recount" "$("$BIN" --json --no-collect --days 30 | q "sum(c['uses'] for c in r['commands'])")" 3
check "events: off menu" "$(echo "$J" | q "r['off_menu']")" "['gym']"
check "events: no text stored" "$(grep -c -e cozy -e hello -e now "$T/state2/counts.json")" 0

echo "# hub-tg (v2 H6): the hub's rows (core.hub.tg_journal) after the local ledger's last row"
# A temp HOME (its bin/lib -> ~/bin/lib for core, MENU_USAGE_LIB another; owner 42 in its live.json) and a fake hub
# on 127.0.0.1.
H="$T/home"; mkdir -p "$H/bin" "$H/.config/tg-adapter" "$H/.secrets"; ln -s "${MENU_USAGE_LIB:-$HOME/bin/lib}" "$H/bin/lib"
printf '{"mode": "live", "allow_from": ["42"]}' > "$H/.config/tg-adapter/live.json"
printf 't%.0s' $(seq 40) > "$H/.secrets/hub-token"; chmod 600 "$H/.secrets/hub-token"
cat > "$T/fakehub.py" <<'PY'
import datetime as dt, json, os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
def row(i, t, **p):
    return {"id": i, "payload": json.dumps(dict(p, ts=t, user_id=p.pop("uid", 42), chat_id=42, message_id=i))}
rows = {"tg.message": [row(3, "2026-11-03T09:01:00.000Z", text="/lights now", event_type="message"),  # copied: local
                       row(11, "2026-11-03T09:10:00.000Z", text="/gym", event_type="message"),
                       row(12, "2026-11-03T09:11:00.000Z", text="/bills", handled="bills", event_type="message"),
                       row(16, "2026-11-03T09:15:00.000Z", text="/space", uid=99, event_type="message"),
                       row(17, "2026-11-03T09:16:00.000Z", text="cozy hello", event_type="message")],
        "tg.button": [row(13, "2026-11-03T09:12:00.000Z", callback_data="mt:play:1", event_type="button"),
                      row(14, "2026-11-03T09:13:00.000Z", callback_data="call:talk", event_type="button")],
        "tg.reaction": [row(15, "2026-11-03T09:14:00.000Z", reaction="x", event_type="reaction")]}
class Hd(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        b = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        with open(sys.argv[2], "a") as f:
            f.write(json.dumps(b) + "\n")
        out = json.dumps({"rows": rows.get((b.get("params") or {}).get("kind"), [])}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
srv = HTTPServer(("127.0.0.1", 0), Hd)
open(sys.argv[1], "w").write(str(srv.server_address[1]))
srv.serve_forever()
PY
python3 "$T/fakehub.py" "$T/hub.port" "$T/hub.calls" & HUBPID=$!
trap 'kill $HUBPID 2>/dev/null; find "${T:?}" -mindepth 1 -delete; rmdir "${T:?}"' EXIT
i=0; while [ ! -s "$T/hub.port" ] && [ $i -lt 50 ]; do sleep 0.1; i=$((i+1)); done
export MENU_USAGE_STATE="$T/state3" MENU_USAGE_NOW=2026-11-03T10:00:00Z
s3() { rm -rf "$T/state3"; cp -r "$T/state2" "$T/state3"; }   # the events-ledger store: cut-over 08:00, the ledger counted
mu() { HOME="$H" HUB_URL="http://127.0.0.1:$(cat "$T/hub.port")" "$BIN" "$@"; }
s3; mu collect; check "hub: test paths set, no MENU_USAGE_HUB: hub not asked" "$(cat "$T/hub.calls" 2>/dev/null | wc -l | tr -d ' ')" 0
s3; MENU_USAGE_HUB=on mu collect; check "hub: collect exit" $? 0
J=$(mu --json --no-collect --days 30)
check "hub: his rows after the ledger's last (09:04); the copied 09:01 row once; not his /space" \
  "$(echo "$J" | q "sorted((c['command'],c['instant'],c['session']) for c in r['commands'] if c['uses'])")" \
  "[('bills', 2, 0), ('gym', 0, 2), ('lights', 0, 1)]"
check "hub: buttons + reactions" "$(echo "$J" | q "([(b['prefix'],b['presses']) for b in r['buttons'] if b['prefix'] in ('mt','rm')], r['reactions'])")" \
  "([('mt', 1), ('rm', 1)], 2)"
check "hub: kinds asked" "$(python3 -c "import json,sys; print(sorted(json.loads(l)['params']['kind'] for l in open(sys.argv[1])))" "$T/hub.calls")" \
  "['tg.button', 'tg.message', 'tg.reaction']"
MENU_USAGE_HUB=on mu collect
check "hub: re-collect dedupes (hub ids)" "$(mu --json --no-collect --days 30 | q "sum(c['uses'] for c in r['commands'])")" 5
check "hub: no text stored" "$(grep -c -e cozy -e hello -e now "$T/state3/counts.json")" 0
python3 - "$T/events.db" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1]); con.execute("DELETE FROM events WHERE source = 'tg-adapter'"); con.commit()
PY
MENU_USAGE_HUB=on mu collect
check "hub: the ledger pruned its rows: the floor holds, no recount" "$(mu --json --no-collect --days 30 | q "sum(c['uses'] for c in r['commands'])")" 5
s3; MENU_USAGE_HUB=off mu collect
check "hub: MENU_USAGE_HUB=off" "$(mu --json --no-collect --days 30 | q "sum(c['uses'] for c in r['commands'])")" 3
s3; MENU_USAGE_HUB=on HOME="$H" HUB_URL=http://127.0.0.1:9 "$BIN" collect; check "hub down: collect exit 0" $? 0
check "hub down: the ledger alone" "$(mu --json --no-collect --days 30 | q "sum(c['uses'] for c in r['commands'])")" 3
kill $HUBPID 2>/dev/null

echo "menu-usage tests: $((n-fail))/$n passed"
[ "$fail" -eq 0 ]
