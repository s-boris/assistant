#!/usr/bin/env bash
# Offline tests for ~/bin/dayline ("Today I did" summary for the dashboard). Exit 0 = all pass.
# Inputs are fixtures via the script's own seams (DAYLINE_WORKSPACE = PROACTIVE.md + CHANGELOG.md,
# DAYLINE_AW_DIR = agent-watch.log + state.json, DAYLINE_OUT); HOME is a temp dir too, so a broken seam can't
# reach the real files. The real ~/.local/state/dash/dayline.json is checked untouched at the end.
# dash-tests covers dayline only through dash-data's live view; this suite runs it directly.
# DAYLINE_BIN=<path> tests another copy.
set -uo pipefail
DL=${DAYLINE_BIN:-$(cd "$(dirname "$0")/../../../scripts" && pwd)/dayline}
REAL_OUT=/home/node/.local/state/dash/dayline.json
T=$(mktemp -d "${TMPDIR:-/tmp}/dayline-tests.XXXXXX")
trap 'rm -rf "$T"' EXIT
real_state() { [ -f "$REAL_OUT" ] && stat -c '%Y %s' "$REAL_OUT"; }
real_before=$(real_state)
WS=$T/ws AW=$T/aw OUT=$T/state/dash/dayline.json
export HOME=$T/home DAYLINE_WORKSPACE=$WS DAYLINE_AW_DIR=$AW DAYLINE_OUT=$OUT
mkdir -p "$WS" "$AW" "$HOME"

LONG="alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron pi rho sigma tau upsilon phi chi psi omega alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron pi rho sigma tau upsilon phi chi psi omega end"
cat > "$WS/PROACTIVE.md" <<EOF
# Proactive log
- 2026-10-14 23:59 done: yesterday thing
- 2026-10-15 01:10 Tests: tg-status 67 passed
- 2026-10-15 02:00 started: building **hddheat** with \`pytest\`
- 2026-10-15 02:30 started building the second   thing
- 2026-10-15 03:00 done: finished the skill audit
- 2026-10-15 04:00 Finished: migrated X
- 2026-10-15 05:00 failed: proxy push rolled back
- 2026-10-15T06:00 restart: reloaded after settings change
- 2026-10-15 — no time entry
- 2026-10-15 07:00 done: $LONG
  - 2026-10-15 08:00 indented sub-bullet
* 2026-10-15 08:10 star bullet
not a bullet 2026-10-15
- 2026-10-16 09:00 done: tomorrow
EOF
cat > "$WS/CHANGELOG.md" <<'EOF'
# Changelog
- 2026-10-15 — Added **X** to `bin`
- 2026-10-15 01:50 Heartbeat mod 0.3.1: fixed
- 2026-10-14 — old change
- 2026-10-15: colon style
- 2026-10-16 – en dash style
EOF
cat > "$AW/agent-watch.log" <<'EOF'
2026-10-14 23:59:01 a1781e2e795701ce0 running -> done
2026-10-15 00:40:01 a0585a18e364c7cf2 running -> done
2026-10-15 01:15:01 ac4e24db1ec9bcdbc running -> done
2026-10-15 02:35:01 stalled abe416ebaf2310998 silent 18m -> inbox/agent-watch-stalled-abe416ebaf2310998-1.md
2026-10-15 02:35:01 abe416ebaf2310998 running -> stalled
2026-10-15 02:50:01 abe416ebaf2310998 stalled -> killed
2026-10-15 02:55:01 resumed abe416ebaf2310998 (was stalled)
2026-10-15 03:00:01 progress a6b3f290c960c79f8 sent (15 min)
2026-10-15 03:15:01 progress a6b3f290c960c79f8 sent (30 min)
2026-10-15 03:20:01 progress a6b3f290c960c79f8 tg FAILED
2026-10-15T04:00:01 a000000000000000x running -> done
2026-10-16 00:00:01 a1781e2e795701ce1 running -> done
EOF
e() { date -d "$1" +%s; }
cat > "$AW/state.json" <<EOF
{"agents": {
 "a1": {"run_start": $(e 2026-10-14T23:30:00Z), "status": "done"},
 "a2": {"run_start": $(e 2026-10-14T22:30:00Z), "status": "done"},
 "a3": {"run_start": $(e 2026-10-14T21:30:00Z), "status": "done"},
 "a4": {"status": "running"},
 "a5": "not a dict",
 "a6": {"run_start": "1791851701"},
 "a7": {"run_start": $(e 2026-10-16T08:00:00Z).5}
}, "tags": {}}
EOF

pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2" | head -30; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
has() { if [[ "$2" == *"$3"* ]]; then ok "$1"; else no "$1" "$2"; fi; }
J() { jq -c "$1" "$T/out.json"; }      # compact JSON of a path
R() { jq -r "$1" "$T/out.json"; }      # raw value

echo "# help"
out=$("$DL" --help 2>&1); rc=$?
eq "--help: rc 0" "$rc" "0"
has "--help: names --stdout" "$out" "--stdout"

echo "# --stdout --date: a full day"
"$DL" --stdout --date 2026-10-15 > "$T/out.json" 2>"$T/err"; rc=$?
eq "rc 0" "$rc" "0"
eq "no stderr" "$(cat "$T/err")" ""
jq -e . "$T/out.json" >/dev/null 2>&1 && ok "valid JSON" || no "valid JSON" "$(head -c 300 "$T/out.json")"
eq "schema and date" "$(R '"\(.schema) \(.date)"')" "1 2026-10-15"
[[ "$(R .generated_at)" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]{8}\+0[12]:00$ ]] && ok "generated_at in Zurich time" \
  || no "generated_at in Zurich time" "$(R .generated_at)"
eq "keys" "$(J 'keys')" '["agents","changes","counts","date","generated_at","proactive","schema","summary"]'
eq "only that day's top-level '- ' entries" "$(J '[.proactive[].time]')" '["01:10","02:00","02:30","03:00","04:00","05:00","06:00",null,"07:00"]'
eq "entry: no kind keeps the text" "$(J '.proactive[0]')" '{"time":"01:10","kind":null,"text":"Tests: tg-status 67 passed"}'
eq "entry: started: + markdown stripped" "$(J '.proactive[1]')" '{"time":"02:00","kind":"started","text":"building hddheat with pytest"}'
eq "entry: started without colon, spaces squeezed" "$(J '.proactive[2]')" '{"time":"02:30","kind":"started","text":"building the second thing"}'
eq "entry: done:" "$(J '.proactive[3]')" '{"time":"03:00","kind":"done","text":"finished the skill audit"}'
eq "entry: Finished (any case) -> done" "$(J '.proactive[4]')" '{"time":"04:00","kind":"done","text":"migrated X"}'
eq "entry: failed:" "$(J '.proactive[5]')" '{"time":"05:00","kind":"failed","text":"proxy push rolled back"}'
eq "entry: T separator; restart is not a kind" "$(J '.proactive[6]')" '{"time":"06:00","kind":null,"text":"restart: reloaded after settings change"}'
eq "entry: date only, em dash" "$(J '.proactive[7]')" '{"time":null,"kind":null,"text":"no time entry"}'
t=$(R '.proactive[8].text')
[ "${#t}" -le 160 ] && ok "long text shortened to <= 160 chars (${#t})" || no "long text shortened to <= 160 chars" "${#t}"
[[ "$t" == *… ]] && ok "long text ends with an ellipsis" || no "long text ends with an ellipsis" "$t"
w=${t%…}; w=${w##* }
[[ " $LONG " == *" $w "* ]] && ok "long text cut at a word boundary ($w)" || no "long text cut at a word boundary" "$w"
eq "changes: that day, markdown stripped, all dash styles" "$(J '.changes')" '["Added X to bin","Heartbeat mod 0.3.1: fixed","colon style"]'
eq "agents: log transitions + run starts in Zurich time" "$(J '.agents')" '{"started":2,"finished":2,"stalled":1,"progress_pings":2}'
eq "counts" "$(J '.counts')" '{"proactive":9,"done":3,"started":2,"changes":3}'
eq "summary (done = done + unlabelled)" "$(R .summary)" "6 things done, 3 setup changes, 2 agent runs finished"

echo "# kind words glued to the next word are not kinds"
printf -- '- 2026-10-17 09:00 Failed-login alerts added\n- 2026-10-17 09:10 Doneness check for steaks\n- 2026-10-17 09:20 Done tidying\n' > "$WS/PROACTIVE.md.new"
cp "$WS/PROACTIVE.md" "$T/proactive.bak"; cat "$WS/PROACTIVE.md.new" >> "$WS/PROACTIVE.md"
"$DL" --stdout --date 2026-10-17 > "$T/out.json"
eq "Failed-login: kind null, text intact" "$(J '.proactive[0]')" '{"time":"09:00","kind":null,"text":"Failed-login alerts added"}'
eq "Doneness: kind null, text intact" "$(J '.proactive[1]')" '{"time":"09:10","kind":null,"text":"Doneness check for steaks"}'
eq "Done tidying: still kind done" "$(J '.proactive[2]')" '{"time":"09:20","kind":"done","text":"tidying"}'
cp "$T/proactive.bak" "$WS/PROACTIVE.md"

echo "# singular wording"
"$DL" --stdout --date 2026-10-16 > "$T/out.json"; rc=$?
eq "next day: rc 0" "$rc" "0"
eq "next day: one of each" "$(R .summary)" "1 thing done, 1 setup change, 1 agent run finished"
eq "next day: fractional run_start counted" "$(R .agents.started)" "1"

echo "# empty day"
"$DL" --stdout --date 2026-01-01 > "$T/out.json"; rc=$?
eq "empty day: rc 0" "$rc" "0"
eq "empty day: no entries" "$(J '[.proactive, .changes]')" '[[],[]]'
eq "empty day: zero counts" "$(J '[.counts, .agents]')" '[{"proactive":0,"done":0,"started":0,"changes":0},{"started":0,"finished":0,"stalled":0,"progress_pings":0}]'
eq "empty day: summary" "$(R .summary)" "0 things done, 0 setup changes, 0 agent runs finished"

echo "# missing and broken inputs"
DAYLINE_WORKSPACE=$T/nope DAYLINE_AW_DIR=$T/nope2 "$DL" --stdout --date 2026-10-15 > "$T/out.json" 2>"$T/err"; rc=$?
eq "missing inputs: rc 0" "$rc" "0"
eq "missing inputs: empty, no traceback" "$(R .summary) | $(cat "$T/err")" "0 things done, 0 setup changes, 0 agent runs finished | "
cp "$AW/state.json" "$T/state.bak"
echo '{not json' > "$AW/state.json"
"$DL" --stdout --date 2026-10-15 > "$T/out.json" 2>"$T/err"; rc=$?
eq "broken state.json: rc 0, no stderr" "$rc $(cat "$T/err")" "0 "
eq "broken state.json: log counts kept, no starts" "$(J '.agents')" '{"started":0,"finished":2,"stalled":1,"progress_pings":2}'
echo '{"agents": null}' > "$AW/state.json"
"$DL" --stdout --date 2026-10-15 > "$T/out.json" 2>"$T/err"; rc=$?
eq "agents: null: rc 0" "$rc $(cat "$T/err")" "0 "
cp "$T/state.bak" "$AW/state.json"

echo "# default date = today in Zurich"
"$DL" --stdout > "$T/out.json"; rc=$?
eq "no --date: rc 0" "$rc" "0"
eq "no --date: today (Europe/Zurich)" "$(R .date)" "$(TZ=Europe/Zurich date +%F)"

echo "# bad --date"
err=$("$DL" --date 2026-1-5 2>&1); rc=$?
eq "bad date: rc 1" "$rc" "1"
eq "bad date: message" "$err" "dayline: --date must be YYYY-MM-DD"
[ -e "$OUT" ] && no "bad date: nothing written" || ok "bad date: nothing written"
err=$("$DL" --date '2026-10-15; rm -rf /' 2>&1); rc=$?
eq "junk date: rc 1" "$rc" "1"

echo "# file output (cron mode)"
out=$("$DL" --date 2026-10-15 2>"$T/err"); rc=$?
eq "file: rc 0" "$rc" "0"
eq "file: no stderr" "$(cat "$T/err")" ""
[[ "$out" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}\ [0-9:]{8}\ dayline\ 2026-10-15:\ 6\ things\ done,\ 3\ setup\ changes,\ 2\ agent\ runs\ finished\ -\>\ $OUT$ ]] \
  && ok "file: one log line with summary and path" || no "file: one log line with summary and path" "$out"
[ -f "$OUT" ] && ok "file: written (parent dirs created)" || no "file: written (parent dirs created)"
eq "file: mode 644" "$(stat -c %a "$OUT")" "644"
eq "file: ends with a newline" "$(tail -c1 "$OUT" | od -An -c | tr -d ' ')" '\n'
"$DL" --stdout --date 2026-10-15 > "$T/out.json"
eq "file: same JSON as --stdout (minus generated_at)" "$(jq -cS 'del(.generated_at)' "$OUT")" "$(jq -cS 'del(.generated_at)' "$T/out.json")"
"$DL" --date 2026-10-16 >/dev/null; rc=$?
eq "file: overwrite rc 0" "$rc" "0"
eq "file: overwritten with the new day" "$(jq -r .date "$OUT")" "2026-10-16"
eq "file: no temp files left" "$(ls -A "$(dirname "$OUT")" | tr '\n' ' ')" "dayline.json "
"$DL" --stdout --date 2026-10-15 >/dev/null
eq "--stdout leaves the file alone" "$(jq -r .date "$OUT")" "2026-10-16"

echo "# isolation"
[ -e "$HOME/.local" ] && no "nothing written under HOME" "$(find "$HOME" | head)" || ok "nothing written under HOME"
eq "real dayline.json unchanged" "$(real_state)" "$real_before"

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
