#!/usr/bin/env bash
# Offline tests for ~/bin/tg-status (the pinned status board). Exit 0 = all pass.
# Everything runs against a temp state dir (TG_STATUS_DIR), a fixture TODO (TG_STATUS_TODO) and a loopback mock
# Bot API (mock_tg.py, TG_STATUS_API + a fake TG_STATUS_TEST_TOKEN), so no call ever reaches Telegram and the real
# board/state in ~/.local/state/tg-status is never read or written. TG_STATUS_DEBOUNCE=0 renders synchronously,
# so no trailing render process outlives the test. Hooks run under a fake `claude` parent process (with or
# without --channels) to exercise the main-session detection.
# TG_STATUS_BIN=<path> tests another copy (e.g. an edited one before installing it).
# Synthetic profile (tests never read the real one): repo layout tests/profile, live layout ~/opt/profile-tests.
_pf="$(cd "$(dirname "$0")/.." && pwd)"; [ -f "$_pf/profile/fixtures/profile.json" ] && _pf=$_pf/profile || _pf=$_pf/profile-tests
export ASSISTANT_PROFILE="$_pf/fixtures/profile.json"
set -uo pipefail
TGS=${TG_STATUS_BIN:-/home/node/bin/tg-status}
HERE=$(cd "$(dirname "$0")" && pwd)
T=$(mktemp -d "${TMPDIR:-/tmp}/tg-status-tests.XXXXXX")
MOCK_PID=
cleanup() { [ -n "$MOCK_PID" ] && kill "$MOCK_PID" 2>/dev/null; rm -rf "$T"; }
trap cleanup EXIT
mkdir -p "$T/mock" "$T/p" "$T/fakebin"
python3 -I "$HERE/mock_tg.py" "$T/mock" & MOCK_PID=$!
for _ in $(seq 50); do [ -s "$T/mock/port" ] && break; sleep 0.1; done
PORT=$(cat "$T/mock/port" 2>/dev/null) || { echo "mock server did not start"; exit 1; }

TOKEN="123456789:TEST_ONLY_not_a_real""_token_abcdefgh"   # split so secretscan does not flag the fake
export TG_STATUS_DIR=$T/state TG_STATUS_TODO=$T/TODO.md TG_STATUS_API=http://127.0.0.1:$PORT \
       TG_STATUS_TEST_TOKEN=$TOKEN TG_STATUS_TEST_CHAT=4242 TG_STATUS_DEBOUNCE=0 TGS T
export TG_STATUS_NOW='2026-10-17 06:00'   # the Due/Next split only (hb-gate's parser, Zurich time)
unset TG_STATUS_TRACE CLAUDE_JOB_DIR TG_STATUS_HB_GATE
# tg-status loads hb-gate from its own dir; a copy under test without it there (assistant ci's overlay stages
# tg-status alone) reads the live one
[ -e "$(dirname "$TGS")/hb-gate" ] || export TG_STATUS_HB_GATE=/home/node/bin/hb-gate
export AGENT_TITLES_FILE=$T/titles.json   # renamed agents (lib/agent_titles.py); never the real file
REAL_TITLES=/home/node/.local/state/agent-titles/titles.json
titles_before="$( [ -f "$REAL_TITLES" ] && md5sum < "$REAL_TITLES")"

pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2" | head -30; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
has() { if [[ "$2" == *"$3"* ]]; then ok "$1"; else no "$1" "$2"; fi; }
hasnt() { if [[ "$2" == *"$3"* ]]; then no "$1" "$2"; else ok "$1"; fi; }
CALLS=$T/mock/calls.jsonl
ncalls() { [ -f "$CALLS" ] && wc -l < "$CALLS" | tr -d ' ' || echo 0; }
# calls since line N: "method method ..."
methods_since() { tail -n +"$(( $1 + 1 ))" "$CALLS" 2>/dev/null | python3 -I -c 'import json,sys; print(" ".join(json.loads(l)["method"] for l in sys.stdin if l.strip()))'; }
# field of the last call: last_call 'expr on c (the call dict)'
last_call() { tail -n 1 "$CALLS" | python3 -I -c 'import json,sys; c = json.loads(sys.stdin.read()); print('"$1"')'; }
st() { python3 -I -c 'import json,sys; s = json.load(open(sys.argv[1])); print('"$1"')' "$T/state/state.json"; }
first_line() { "$TGS" show --plain | head -n 1; }

cat > "$T/TODO.md" <<'EOF'
# TODO
## Due
- **Pay rent** — 18:00 / 15.10
- **Ship it** — whenever
- **Call bank** — 17.10 09:00
- **Morning run** — 05:30
- **Book trip** — 19.10
## In progress
- **Build tests** — tg-status suite running
  - internal sub-bullet, never shown
- Plain item — with status
- **Leak test** — @LEAK@
## Waiting on you
- **Pick a drive** — 2 options
- **Approve X**
- **R&D <x>** — check
## Ideas
- **Not shown**
EOF
sed -i "s/@LEAK@/987654321:ABCDEFGHIJKLMNOPQRSTUVWXYZ""abcdef1234/" "$T/TODO.md"   # bot-token-shaped fake, split for secretscan

echo "# help and usage"
out=$("$TGS" --help); rc=$?
eq "--help rc 0" "$rc" "0"
has "--help prints the usage" "$out" "tg-status refresh | render"
[ -e "$T/state" ] && no "--help creates no state dir" || ok "--help creates no state dir"
"$TGS" -h >/dev/null; eq "-h rc 0" "$?" "0"
err=$("$TGS" bogus 2>&1 >/dev/null); rc=$?
eq "unknown command rc 2" "$rc" "2"
has "unknown command prints usage on stderr" "$err" "tg-status hook [event]"

echo "# show (render only, no Telegram)"
out=$("$TGS" show --plain); rc=$?
eq "show rc 0" "$rc" "0"
eq "header: idle, counts, first in-progress title" "$(printf '%s\n' "$out" | head -n 1)" "💤 ❗3 ❓3 · Idle · Build tests"
has "due: undated first, then by time; past days as DD.MM HH:MM, today as HH:MM" "$out" "❗ Due:
• Ship it
• 15.10 18:00 Pay rent
• 05:30 Morning run"
has "next: not yet due, by time, without the ❗ count" "$out" "🕒 Next:
• 09:00 Call bank
• 19.10 Book trip"
eq "next is the last block" "$(printf '%s\n' "$out" | grep -n -e '🕒 Next:' -e '❓ Waiting on you:' | cut -d: -f2- | tr '\n' ' ')" "❓ Waiting on you: 🕒 Next: "
eq "its time came: counted as due" "$(TG_STATUS_NOW='2026-10-17 09:00' "$TGS" show --plain | head -n 1)" "💤 ❗4 ❓3 · Idle · Build tests"
has "its time came: listed under Due" "$(TG_STATUS_NOW='2026-10-17 09:00' "$TGS" show --plain)" "• 05:30 Morning run
• 09:00 Call bank"
printf -- '# TODO\n## Due\n- **NY call** — 02.01 10:00\n- **Old bill** — 28.12\n' > "$T/TODO-year.md"
out2=$(TG_STATUS_TODO=$T/TODO-year.md TG_STATUS_NOW='2026-12-30 08:00' "$TGS" show --plain)
has "yearless 02.01 in late December = next January" "$out2" "🕒 Next:
• 02.01 10:00 NY call"
has "yearless 28.12 two days ago: due, date only" "$out2" "❗ Due:
• 28.12 Old bill"
has "yearless 28.12 in early January = last December: due" "$(TG_STATUS_TODO=$T/TODO-year.md TG_STATUS_NOW='2027-01-03 08:00' "$TGS" show --plain)" "❗ Due:
• 28.12 Old bill"
printf -- '# TODO\n## Due\n- **Night call** — 25.10 02:30\n' > "$T/TODO-dst.md"
has "autumn DST: 02:10 CEST (first pass), due 02:30: next" "$(TG_STATUS_TODO=$T/TODO-dst.md TG_STATUS_NOW=$(date -u -d '2026-10-25 00:10 UTC' +%s) "$TGS" show --plain)" "🕒 Next:
• 02:30 Night call"
has "autumn DST: 02:10 CET (second pass), due 02:30: due (hb-gate's rule)" "$(TG_STATUS_TODO=$T/TODO-dst.md TG_STATUS_NOW=$(date -u -d '2026-10-25 01:10 UTC' +%s) "$TGS" show --plain)" "❗ Due:
• 02:30 Night call"
out2=$(TG_STATUS_HB_GATE=$T/no-hb-gate "$TGS" show --plain)
eq "hb-gate missing: every Due item counts" "$(printf '%s\n' "$out2" | head -n 1)" "💤 ❗5 ❓3 · Idle · Build tests"
has "hb-gate missing: plain titles, TODO order" "$out2" "❗ Due:
• Pay rent
• Ship it
• Call bank"
hasnt "hb-gate missing: no Next block" "$out2" "🕒 Next:"
has "hb-gate missing: logged" "$(cat "$T/state/log")" "hb-gate parser unavailable"
has "in progress with status" "$out" "• Build tests — tg-status suite running"
has "plain bullet split on the dash" "$out" "• Plain item — with status"
has "waiting on you" "$out" "❓ Waiting on you:
• Pick a drive
• Approve X"
hasnt "sub-bullets skipped" "$out" "internal sub-bullet"
hasnt "other sections skipped" "$out" "Not shown"
has "bot-token-like text is scrubbed" "$out" "Leak test — <token>"
hasnt "token digits never shown" "$out" "987654321:ABC"
has "links line" "$out" "📊 Dashboard https://dash.example.org · 🖥 Browser https://browser.example.org/"
has "footer clock" "$out" "updated "
html=$("$TGS" show)
has "HTML: bold section labels" "$html" "<b>❗ Due:</b>"
has "HTML: bold Next label" "$html" "<b>🕒 Next:</b>"
has "HTML: text escaped" "$html" "R&amp;D &lt;x&gt;"
has "HTML: link anchors" "$html" '<a href="https://dash.example.org">📊 Dashboard</a>'
eq "show made no Telegram call" "$(ncalls)" "0"
eq "missing TODO: plain idle header" "$(TG_STATUS_TODO=$T/nope.md "$TGS" show --plain | head -n 1)" "💤 · Idle"

echo "# note: first render sends and pins"
"$TGS" note "Night build running"; rc=$?
eq "note rc 0" "$rc" "0"
eq "sendMessage then pinChatMessage" "$(methods_since 0)" "sendMessage pinChatMessage"
eq "only the test token is used" "$(python3 -I -c 'import json,sys; print(sorted({json.loads(l)["token"] for l in open(sys.argv[1])}))' "$CALLS")" "['$TOKEN']"
eq "sent to the test chat, silently" "$(head -n 1 "$CALLS" | python3 -I -c 'import json,sys; p = json.loads(sys.stdin.read())["params"]; print(p["chat_id"], p["parse_mode"], p["disable_notification"])')" "4242 HTML True"
has "note shown under the header" "$(head -n 1 "$CALLS" | python3 -I -c 'import json,sys; print(json.loads(sys.stdin.read())["params"]["text"])')" "📌 Night build running"
eq "pinned the new message" "$(last_call 'c["params"]["message_id"]')" "101"
eq "message id remembered" "$(st 's["message_id"]')" "101"

echo "# refresh: edit only when something changed"
n=$(ncalls); "$TGS" refresh; eq "unchanged: no call" "$(ncalls)" "$n"
printf -- '- **Renew domain** — 20.10\n' > "$T/due.add"
python3 -I - "$T/TODO.md" "$T/due.add" <<'EOF'
import sys
p, add = sys.argv[1], open(sys.argv[2]).read()
s = open(p).read().replace('## In progress\n', add + '## In progress\n', 1)
open(p, 'w').write(s)
EOF
n=$(ncalls); "$TGS" refresh
eq "changed TODO: one edit" "$(methods_since "$n")" "editMessageText"
eq "edit targets the pinned message" "$(last_call 'c["params"]["message_id"]')" "101"
has "edit carries the new item under Next" "$(last_call 'c["params"]["text"]')" "• 20.10 Renew domain"
has "a not-yet-due item leaves the ❗ count alone" "$(last_call 'c["params"]["text"]')" "❗3 "
n=$(ncalls); "$TGS" note --clear
hasnt "note --clear removes the note" "$(last_call 'c["params"]["text"]')" "📌"
eq "note --clear: one edit" "$(methods_since "$n")" "editMessageText"

echo "# Telegram failures"
touch "$T/mock/fail-html"; "$TGS" note "html fallback"; rm -f "$T/mock/fail-html"
eq "HTML rejected: retried as plain text" "$(last_call '(c["method"], "parse_mode" in c["params"], "<b>" in c["params"]["text"])')" "('editMessageText', False, False)"
touch "$T/mock/fail-edit-notfound"; n=$(ncalls); "$TGS" note "message gone"; rm -f "$T/mock/fail-edit-notfound"
eq "pinned message gone: send a new one and pin it" "$(methods_since "$n")" "editMessageText sendMessage pinChatMessage"
eq "new message id remembered" "$(st 's["message_id"]')" "102"
touch "$T/mock/fail-429-once"; n=$(ncalls); "$TGS" note "rate limited"
eq "429 with retry_after: retried once" "$(methods_since "$n")" "editMessageText editMessageText"
has "429: the retry is logged" "$(cat "$T/state/log")" "429 on editMessageText, retrying in 1s"
out=$(TG_STATUS_API=http://127.0.0.1:1 "$TGS" note "api down" 2>&1); rc=$?
eq "API unreachable: rc 0 (logged, not fatal)" "$rc" "0"
has "API unreachable: logged" "$(cat "$T/state/log")" "telegram: "
hasnt "the token never reaches the log" "$(cat "$T/state/log")" "TEST_ONLY_not_a_real_token"
n=$(ncalls); err=$(env -u TG_STATUS_TEST_TOKEN "$TGS" note "no token" 2>&1); rc=$?
eq "test API without a test token: rc 1" "$rc" "1"
has "test API without a test token: says it failed" "$err" "tg-status note failed"
eq "test API without a test token: no call at all" "$(ncalls)" "$n"

echo "# reset"
n=$(ncalls); "$TGS" reset; rc=$?
eq "reset rc 0" "$rc" "0"
eq "reset: unpin + mark the old one, send + pin a new one" "$(methods_since "$n")" "unpinChatMessage editMessageText sendMessage pinChatMessage"
has "reset: old message says it moved" "$(sed -n "$(( n + 2 ))p" "$CALLS" | python3 -I -c 'import json,sys; print(json.loads(sys.stdin.read())["params"]["text"])')" "Status board moved to a newer message."
eq "reset: new message id" "$(st 's["message_id"]')" "103"

echo "# background ages: 10-min steps, an age-only change edits at most every 10 min"
setst() { python3 -I - "$T/state/state.json" "$@" <<'EOF'
import json, sys, time
p = sys.argv[1]; s = json.load(open(p)); now = time.time()
for kv in sys.argv[2:]:
    k, _, v = kv.partition('=')
    if k == 'age':
        s['agents'] = {'x1': {'desc': 'Long job', 'started': now - float(v), 'added': now - float(v)}}
    elif k == 'edited':
        s['updated_at'] = now - float(v)
    elif k == 'noagents':
        s['agents'] = {}
s['main'] = {}
json.dump(s, open(p, 'w'))
EOF
}
setst age=60; n=$(ncalls); "$TGS" refresh
eq "new background agent: one edit" "$(methods_since "$n")" "editMessageText"
has "age in 10-min steps" "$(last_call 'c["params"]["text"]')" "• Long job (&lt;10m)"
ls1=$(st 's.get("last_send")'); n=$(ncalls); "$TGS" refresh
eq "unchanged board: no call" "$(ncalls)" "$n"
eq "unchanged board: the send window is not claimed" "$(st 's.get("last_send")')" "$ls1"
setst age=700; n=$(ncalls); "$TGS" refresh
eq "age-only change 0 min after the last edit: no call" "$(ncalls)" "$n"
setst age=700 edited=650; n=$(ncalls); "$TGS" refresh
eq "age-only change 10+ min after the last edit: one edit" "$(methods_since "$n")" "editMessageText"
has "the age moved a step" "$(last_call 'c["params"]["text"]')" "• Long job (10m)"
setst age=1300; n=$(ncalls); "$TGS" note "real change"
eq "a real change right after an edit: edits at once" "$(methods_since "$n")" "editMessageText"
has "a real change carries the current age" "$(last_call 'c["params"]["text"]')" "• Long job (20m)"
eq "ages: 1h" "$(setst age=3700; "$TGS" show --plain | grep 'Long job')" "• Long job (1h)"
eq "ages: 1h50m" "$(setst age=6900; "$TGS" show --plain | grep 'Long job')" "• Long job (1h50m)"
eq "ages: 1d2h" "$(setst age=94000; "$TGS" show --plain | grep 'Long job')" "• Long job (1d2h)"
setst noagents; "$TGS" note --clear

echo "# hooks: only the main session (claude with --channels) drives the board"
: > "$T/TODO.md"
"$TGS" note --clear
cat > "$T/fakebin/claude" <<'EOF'
#!/bin/bash
# fake claude: argv[1] is its flag (--channels=... or --print), the rest is a command it runs as its child
shift
"$@"; rc=$?
exit $rc
EOF
chmod +x "$T/fakebin/claude"
cat > "$T/p/start.json" <<'EOF'
{"hook_event_name": "SessionStart", "session_id": "s1", "source": "startup"}
EOF
cat > "$T/p/prompt.json" <<'EOF'
{"hook_event_name": "UserPromptSubmit", "session_id": "s1", "prompt": "<channel source=\"plugin:telegram-plus:telegram\" chat_id=\"1\">hi</channel>"}
EOF
cat > "$T/p/bash.json" <<'EOF'
{"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "ls", "description": "Run the test suite"}}
EOF
cat > "$T/p/grep.json" <<'EOF'
{"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "Grep", "tool_input": {"pattern": "needle"}}
EOF
cat > "$T/p/agent-pre.json" <<'EOF'
{"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "Agent", "tool_use_id": "tu1", "tool_input": {"description": "Write tests", "subagent_type": "general-purpose"}}
EOF
cat > "$T/p/agent-post.json" <<'EOF'
{"hook_event_name": "PostToolUse", "session_id": "s1", "tool_name": "Agent", "tool_use_id": "tu1", "tool_input": {"description": "Write tests"}, "tool_response": {"agentId": "a1", "isAsync": true, "status": "async_launched", "description": "Write tests"}}
EOF
cat > "$T/p/sub-step.json" <<'EOF'
{"hook_event_name": "PreToolUse", "session_id": "s1", "agent_id": "a1", "agent_type": "general-purpose", "tool_name": "Bash", "tool_input": {"description": "SUBAGENT STEP never shown"}}
EOF
cat > "$T/p/stop.json" <<'EOF'
{"hook_event_name": "Stop", "session_id": "s1", "background_tasks": [{"id": "a1", "type": "subagent", "status": "running", "description": "Write tests"}]}
EOF
cat > "$T/p/helper.json" <<'EOF'
{"hook_event_name": "SubagentStart", "session_id": "s1", "agent_id": "h1", "agent_type": ""}
EOF
cat > "$T/age.py" <<'EOF'
import json, sys
p = sys.argv[1]; s = json.load(open(p))
for a in s['agents'].values():
    a['started'] -= float(sys.argv[2])
json.dump(s, open(p, 'w'))
EOF
cat > "$T/p/stopfail.json" <<'EOF'
{"hook_event_name": "StopFailure", "session_id": "s1", "error": "rate limit reached"}
EOF
cat > "$T/seq-main.sh" <<'EOF'
h() { "$TGS" hook < "$T/p/$1.json"; }
h start; h prompt; "$TGS" show --plain > "$T/s-prompt.txt"
h bash; "$TGS" show --plain > "$T/s-bash.txt"
h grep; "$TGS" show --plain > "$T/s-grep.txt"
h agent-pre; h agent-post; h sub-step; "$TGS" show --plain > "$T/s-agent.txt"
h stop; "$TGS" show --plain > "$T/s-stop.txt"
python3 -I "$T/age.py" "$T/state/state.json" 700; wc -l < "$T/mock/calls.jsonl" > "$T/s-helper.n0"
h helper; wc -l < "$T/mock/calls.jsonl" > "$T/s-helper.n1"; "$TGS" show --plain > "$T/s-helper.txt"
echo broken-json | "$TGS" hook; echo "$?" > "$T/s-badjson.rc"
EOF
n=$(ncalls)
"$T/fakebin/claude" --channels=plugin:telegram-plus@home-plugins bash "$T/seq-main.sh"
eq "prompt from Telegram: working" "$(head -n 1 "$T/s-prompt.txt")" "🟢 · Reading your Telegram message"
eq "tool with a description: its description" "$(head -n 1 "$T/s-bash.txt")" "🟢 · Run the test suite"
eq "Grep: search label" "$(head -n 1 "$T/s-grep.txt")" "🟢 · Searching for “needle”"
eq "async agent: delegating, one in the background" "$(head -n 1 "$T/s-agent.txt")" "🟢 ⚙1 · Delegating: Write tests"
has "background agent listed with its age" "$(cat "$T/s-agent.txt")" "⚙ Background:
• Write tests (<10m)"
hasnt "a subagent's own steps never show" "$(cat "$T/s-agent.txt")" "SUBAGENT STEP"
eq "Stop: idle, the running agent stays" "$(head -n 1 "$T/s-stop.txt")" "💤 ⚙1 · Idle"
eq "helper-agent hook while only an age moved: no edit" "$(cat "$T/s-helper.n1")" "$(cat "$T/s-helper.n0")"
has "helper-agent hook: the board itself shows the new age" "$(cat "$T/s-helper.txt")" "• Write tests (10m)"
eq "broken hook JSON: rc 0 (hooks fail silently)" "$(cat "$T/s-badjson.rc")" "0"
has "hooks rendered to Telegram as they went" "$(methods_since "$n")" "editMessageText"
eq "main session remembered" "$(st 's["main"]["session_id"], s["mode"]')" "s1 idle"

before=$(md5sum < "$T/state/state.json"); n=$(ncalls)
"$T/fakebin/claude" --print bash -c '"$TGS" hook < "$T/p/prompt.json"; "$TGS" hook < "$T/p/bash.json"'
eq "a claude without --channels (worker) is ignored: state untouched" "$(md5sum < "$T/state/state.json")" "$before"
eq "a worker's hooks make no Telegram call" "$(ncalls)" "$n"

echo "# refresh: stale main session and stale working"
"$TGS" refresh
eq "main process gone: background agents cleared" "$(first_line)" "💤 · Idle"
"$T/fakebin/claude" --channels=plugin:telegram-plus@home-plugins bash -c '"$TGS" hook < "$T/p/prompt.json"; "$TGS" hook < "$T/p/stopfail.json"'
eq "StopFailure: error shown in the header" "$(first_line)" "⚠️ · error: rate limit reached"
python3 -I - "$T/state/state.json" <<'EOF'
import json, sys, time
p = sys.argv[1]; s = json.load(open(p))
s.update(mode='working', now='Old work', last_activity=time.time() - 700, main={}, agents={})
s.pop('error', None)
json.dump(s, open(p, 'w'))
EOF
eq "fixture: working" "$(first_line)" "🟢 · Old work"
"$TGS" refresh
eq "working with no activity for 10+ min: idle" "$(first_line)" "💤 · Idle"

# Board times follow the profile's zone (Europe/Zurich in the fixture), never the process TZ: TZ=UTC, both DST nights.
out=$(TZ=UTC python3 -I - "$TGS" <<'PY' 2>&1
import datetime as dt, importlib.machinery, importlib.util, sys
from zoneinfo import ZoneInfo
loader = importlib.machinery.SourceFileLoader('tg_status', sys.argv[1])
m = importlib.util.module_from_spec(importlib.util.spec_from_loader('tg_status', loader)); loader.exec_module(m)
Z, U = ZoneInfo('Europe/Zurich'), dt.timezone.utc
z = lambda *t, fold=0: dt.datetime(*t, fold=fold, tzinfo=Z).timestamp()
u = lambda *t: dt.datetime(*t, tzinfo=U).timestamp()
print('zone', m.ZONE.key)
for label, ts, now in [
    ('autumn 02:30 CEST', z(2026, 10, 25, 2, 30), z(2026, 10, 25, 9, 0)),
    ('autumn 02:30 CET', z(2026, 10, 25, 2, 30, fold=1), z(2026, 10, 25, 9, 0)),
    ('autumn 07:30 CET', u(2026, 10, 25, 6, 30), u(2026, 10, 25, 7, 0)),
    ('autumn 00:30 CEST next to Sat 23:59', z(2026, 10, 25, 0, 30), z(2026, 10, 24, 23, 59)),
    ('spring 01:59 CET', z(2027, 3, 28, 1, 59), z(2027, 3, 28, 9, 0)),
    ('spring 03:00 CEST', u(2027, 3, 28, 1, 0), u(2027, 3, 28, 7, 0)),
    ('spring 08:00 CEST', u(2027, 3, 28, 6, 0), u(2027, 3, 28, 6, 0)),
    ('spring 00:30 CET (23:30Z Sat)', u(2027, 3, 27, 23, 30), u(2027, 3, 28, 10, 0)),
]:
    print(label, '=', m.hm(ts, now))
PY
)
has "board zone: profile locale.tz under TZ=UTC" "$out" "zone Europe/Zurich"
has "TZ=UTC: autumn first 02:30 (CEST)" "$out" "autumn 02:30 CEST = 02:30"
has "TZ=UTC: autumn second 02:30 (CET)" "$out" "autumn 02:30 CET = 02:30"
has "TZ=UTC: autumn 06:30Z = 07:30 CET" "$out" "autumn 07:30 CET = 07:30"
has "TZ=UTC: autumn 00:30 vs Sat 23:59 = another day" "$out" "autumn 00:30 CEST next to Sat 23:59 = Sun 00:30"
has "TZ=UTC: spring 01:59 CET" "$out" "spring 01:59 CET = 01:59"
has "TZ=UTC: spring 01:00Z = 03:00 CEST" "$out" "spring 03:00 CEST = 03:00"
has "TZ=UTC: spring 06:00Z = 08:00 CEST" "$out" "spring 08:00 CEST = 08:00"
has "TZ=UTC: spring 23:30Z Sat = Sun 00:30, same day as 10:00Z" "$out" "spring 00:30 CET (23:30Z Sat) = 00:30"

echo "# Talk button (call:talk) and the buttons.json keep mark"
eq "the first send carried the 📞 Talk button" "$(head -n 1 "$CALLS" | python3 -I -c 'import json,sys; print(json.loads(sys.stdin.read())["params"].get("reply_markup"))')" "{'inline_keyboard': [[{'text': '📞 Talk', 'callback_data': 'call:talk'}]]}"
B=$T/buttons.json
n=$(ncalls); TG_STATUS_BUTTONS=$B "$TGS" reset >/dev/null
mid=$(st 's["message_id"]')
eq "reset: the new board keeps the button" "$(last_call 'c["method"]'):$(tail -n 2 "$CALLS" | head -n 1 | python3 -I -c 'import json,sys; print(json.loads(sys.stdin.read())["params"]["reply_markup"]["inline_keyboard"][0][0]["callback_data"])')" "pinChatMessage:call:talk"
eq "buttons.json: the new board is marked keep (0600)" "$(python3 -I -c 'import json,sys; d = json.load(open(sys.argv[1])); print(d[sys.argv[2]]["on_press"])' "$B" "4242:$mid"):$(stat -c %a "$B")" "keep:600"
python3 -I -c 'import json,sys; json.dump({"4242:7": {"on_press": "remove", "ts": 1}, "4242:" + sys.argv[2]: {"on_press": "keep", "ts": 5}}, open(sys.argv[1], "w"))' "$B" "$mid"
TG_STATUS_BUTTONS=$B "$TGS" note "keep refresh"
eq "an edit refreshes a mark older than a week and leaves other entries" "$(python3 -I -c 'import json,sys; d = json.load(open(sys.argv[1])); print(d["4242:7"]["on_press"], d["4242:" + sys.argv[2]]["ts"] > 5)' "$B" "$mid")" "remove True"
eq "the edit carries the button" "$(last_call 'c["method"] + ":" + str("call:talk" in str(c["params"].get("reply_markup")))')" "editMessageText:True"
before=$(cat "$B"); TG_STATUS_BUTTONS=$B "$TGS" note "keep fresh"
eq "a fresh mark is not rewritten" "$(cat "$B")" "$before"
TG_STATUS_BUTTONS=off "$TGS" note "keep off"; eq "TG_STATUS_BUTTONS=off: buttons.json untouched" "$(cat "$B")" "$before"
n=$(ncalls); TG_STATUS_TALK=0 "$TGS" refresh
eq "TG_STATUS_TALK=0: one edit drops the button" "$(methods_since "$n"):$(last_call 'c["params"].get("reply_markup")')" "editMessageText:None"
n=$(ncalls); TG_STATUS_TALK=0 "$TGS" refresh; eq "TG_STATUS_TALK=0: no further edit" "$(methods_since "$n")" ""
n=$(ncalls); "$TGS" refresh; eq "button back on: one edit adds it" "$(methods_since "$n"):$(last_call '"call:talk" in str(c["params"].get("reply_markup"))')" "editMessageText:True"
eq "a mock API without TG_STATUS_BUTTONS never writes the real buttons.json" "$(grep -s "4242:" "$HOME/.claude/channels/telegram/buttons.json" | wc -l | tr -d ' ')" "0"

echo "# title: renamed background agents (lib/agent_titles.py)"
(
export TG_STATUS_DIR=$T/state-titles
mkdir -p "$TG_STATUS_DIR"
python3 -I - "$TG_STATUS_DIR/state.json" <<'EOF'
import json, sys, time
now = time.time()
ag = {'a0123456789abcdef': 'Write tests', 'a0129999999999999': 'Second agent', 'b1': 'Short id'}
json.dump({'mode': 'idle', 'idle_at': now, 'last_activity': now,
           'agents': {k: {'desc': d, 'started': now - 60, 'added': now} for k, d in ag.items()},
           'known': {k: {'desc': d, 't': now} for k, d in ag.items()}}, open(sys.argv[1], 'w'))
EOF
has "before: the spawn description" "$("$TGS" show --plain)" "• Write tests ("
n=$(ncalls)
out=$("$TGS" title a01234 "Disk check" 2>&1); rc=$?
eq "title by prefix: rc 0" "$rc" "0"
eq "title by prefix: confirms the full id" "$out" 'a0123456789abcdef: "Disk check"'
eq "stored in the titles file" "$(python3 -I -c 'import json,sys; d = json.load(open(sys.argv[1])); print(sorted(d), d["a0123456789abcdef"]["title"])' "$T/titles.json")" "['a0123456789abcdef'] Disk check"
eq "titles file is private (0600)" "$(stat -c %a "$T/titles.json")" "600"
board=$("$TGS" show --plain)
has "board shows the title" "$board" "• Disk check ("
hasnt "board no longer shows the spawn description" "$board" "Write tests"
has "the other agents keep their descriptions" "$board" "• Second agent ("
has "the rename re-renders the board" "$(methods_since "$n")" "Message"
eq "the rendered board carries the title" "$(tail -n +"$((n + 1))" "$CALLS" | grep -c 'Disk check')" "1"
eq "list: id, title, spawn description" "$("$TGS" title)" 'a0123456789abcdef  "Disk check"  (was: Write tests)'
out=$("$TGS" title a0123456789abcdef "$(printf 'A very long title\nthat goes on\tand on and on and on and on')" 2>&1)
eq "long multi-line title: one line, cut to 40 with …" "$out" 'a0123456789abcdef: "A very long title that goes on and on a…" (cut to 40 chars)'
eq "the cut title is what is stored" "$(python3 -I -c 'import json,sys; t = json.load(open(sys.argv[1]))["a0123456789abcdef"]["title"]; print(len(t), "\n" in t)' "$T/titles.json")" "40 False"
out=$("$TGS" title b1 "Short one" 2>&1); eq "an exact short id" "$out" 'b1: "Short one"'
out=$("$TGS" title a01 "x" 2>&1); rc=$?
eq "ambiguous prefix: rc 1" "$rc" "1"; has "ambiguous prefix: says so" "$out" "ambiguous prefix 'a01'"
out=$("$TGS" title nope "x" 2>&1); rc=$?
eq "unknown agent: rc 1" "$rc" "1"; has "unknown agent: says so" "$out" "no agent matches 'nope'"
out=$("$TGS" title ../etc "x" 2>&1); eq "not an id: rc 1" "$?" "1"
out=$("$TGS" title afedcba9876543210 "Not seen yet" 2>&1)
eq "a full id the board has not seen yet: accepted" "$out" 'afedcba9876543210: "Not seen yet"'
out=$("$TGS" title a01234 2>&1)
eq "no title: cleared" "$out" "a0123456789abcdef: title cleared (spawn description again)"
has "cleared: the spawn description is back" "$("$TGS" show --plain)" "• Write tests ("
"$TGS" title b1 - >/dev/null; hasnt "'-' clears too" "$("$TGS" title)" "b1"
echo '{broken' > "$T/titles.json"
has "broken titles file: the board still renders descriptions" "$("$TGS" show --plain)" "• Write tests ("
eq "broken titles file: list says none" "$("$TGS" title)" "(no titles)"
out=$("$TGS" title a01234 "After the break" 2>&1); eq "broken titles file: a new title replaces it" "$out" 'a0123456789abcdef: "After the break"'
) | tee "$T/titles.out"
pass=$((pass + $(grep -c '^ok' "$T/titles.out"))); fail=$((fail + $(grep -c '^FAIL' "$T/titles.out")))
eq "real titles file untouched" "$( [ -f "$REAL_TITLES" ] && md5sum < "$REAL_TITLES")" "$titles_before"

# One sender: the board through the live outbox (temp HOME, a fake live adapter + consumer; no Bot API)
ob=$(python3 -I "$HERE/test_outbox.py" "$TGS" 2>&1)
printf '%s\n' "$ob" | grep -v '^[0-9]* passed, [0-9]* failed$'
obsum=$(printf '%s\n' "$ob" | grep '^[0-9]* passed, [0-9]* failed$' | tail -n 1)
if [ -n "$obsum" ]; then
  pass=$((pass + ${obsum%% *})); obf=${obsum#*, }; fail=$((fail + ${obf%% *}))
else
  no "outbox scenario ran" "$ob"
fi

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
