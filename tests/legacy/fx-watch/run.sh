#!/usr/bin/env bash
# fx-watch tests: offline, fake rate source (FX_WATCH_SOURCE_CMD), temp dirs. FX_WATCH_BIN=/path/to/copy tests a copy.
set -u
BIN=${FX_WATCH_BIN:-$(cd "$(dirname "$0")/../../../scripts" && pwd)/fx-watch}
T=$(mktemp -d)
trap 'rm -rf "${T:?}"' EXIT
export FX_WATCH_CONFIG="$T/config.json" FX_WATCH_STATE="$T/state/state.json" FX_WATCH_INBOX="$T/inbox"
mkdir -p "$T/inbox"
echo '{"until": "2026-11-18", "threshold_pct": 3}' > "$FX_WATCH_CONFIG"
pass=0 fail=0
ok() { if [ "$1" = 0 ]; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $2"; fi; }
rate() { export FX_WATCH_SOURCE_CMD="echo '{\"SCR\": $1, \"EUR\": $2}'"; }
notes() { find "$T/inbox" -name 'fx-watch-*.md' | wc -l; }

# 1 first run sets the baseline, silent
rate 17.80 1.08; FX_WATCH_TODAY=2026-10-21 "$BIN" run >"$T/out" 2>&1; ok $? "first run exit"
[ ! -s "$T/out" ]; ok $? "first run silent"
grep -q '"baseline_date": "2026-10-21"' "$FX_WATCH_STATE"; ok $? "baseline stored"
[ "$(notes)" = 0 ]; ok $? "no note on baseline"

# 2 small move (+2.9 %) stays silent
rate 18.316 1.09; FX_WATCH_TODAY=2026-10-22 "$BIN" run >"$T/out" 2>&1; ok $? "small move exit"
[ "$(notes)" = 0 ] && [ ! -s "$T/out" ]; ok $? "small move silent"

# 3 dry run of a big move prints, writes nothing
rate 18.40 1.08; FX_WATCH_TODAY=2026-10-23 "$BIN" run --dry-run >"$T/out" 2>&1; ok $? "dry run exit"
grep -q 'CHF→SCR moved +3.4 %' "$T/out" && [ "$(notes)" = 0 ]; ok $? "dry run prints only"

# 4 big move down (-3.5 %) writes ONE note
rate 17.177 1.08; FX_WATCH_TODAY=2026-10-24 "$BIN" run >"$T/out" 2>&1; ok $? "big move exit"
[ "$(notes)" = 1 ]; ok $? "one note"
grep -q 'moved -3.5 %' "$T"/inbox/fx-watch-*.md && grep -q 'fewer SCR per CHF' "$T"/inbox/fx-watch-*.md; ok $? "note content"
grep -q 'CHF→EUR: 1.0800' "$T"/inbox/fx-watch-*.md; ok $? "note has EUR line"

# 5 a further move: still only one note
rate 16.5 1.05; FX_WATCH_TODAY=2026-10-25 "$BIN" run >/dev/null 2>&1; ok $? "after alert exit"
[ "$(notes)" = 1 ]; ok $? "no second note"

# 6 history keeps one entry per day
rate 16.6 1.05; FX_WATCH_TODAY=2026-10-25 "$BIN" run >/dev/null 2>&1
[ "$(grep -c '"date": "2026-10-25"' "$FX_WATCH_STATE")" = 1 ]; ok $? "one history row per day"

# 7 after the window: no fetch, exit 0 (a failing source proves no fetch)
export FX_WATCH_SOURCE_CMD="exit 1"; FX_WATCH_TODAY=2026-11-19 "$BIN" run >"$T/out" 2>&1; ok $? "after window exit 0"
[ ! -s "$T/out" ]; ok $? "after window silent"

# 8 source failure inside the window: exit 2, stderr, no note
FX_WATCH_TODAY=2026-11-01 "$BIN" run >"$T/out" 2>&1; [ $? = 2 ]; ok $? "source failure exit 2"
grep -q 'all sources failed' "$T/out" && [ "$(notes)" = 1 ]; ok $? "failure message, no note"

# 9 missing currency counts as failure
export FX_WATCH_SOURCE_CMD="echo '{\"EUR\": 1.08}'"; FX_WATCH_TODAY=2026-11-01 "$BIN" run >"$T/out" 2>&1; [ $? = 2 ]; ok $? "missing SCR exit 2"
grep -q 'missing SCR' "$T/out"; ok $? "missing SCR named"

# 10 status shows change; reset clears baseline; next run rebaselines silently
rate 18.0 1.1; FX_WATCH_TODAY=2026-11-02 "$BIN" status >"$T/out" 2>&1; ok $? "status exit"
grep -q 'CHF→SCR: 18.0000  (+1.12 % vs 17.8000' "$T/out" && grep -q 'alerted: yes 2026-10-24' "$T/out"; ok $? "status text"
FX_WATCH_TODAY=2026-11-02 "$BIN" status --json | python3 -c 'import sys,json; d=json.load(sys.stdin); assert d["change_pct"]["SCR"]==1.12' ; ok $? "status json"
"$BIN" reset >/dev/null; ok $? "reset exit"
FX_WATCH_TODAY=2026-11-03 "$BIN" run >/dev/null 2>&1
grep -q '"baseline_date": "2026-11-03"' "$FX_WATCH_STATE" && grep -q '"alerted": null' "$FX_WATCH_STATE"; ok $? "rebaselined"

# 11 no config: defaults, open window, still works
rm -f "$FX_WATCH_CONFIG"; FX_WATCH_TODAY=2027-06-01 "$BIN" run >/dev/null 2>&1; ok $? "no config run"
"$BIN" --help | grep -q 'fx-watch run'; ok $? "help"
"$BIN" bogus >/dev/null 2>&1; [ $? = 1 ]; ok $? "unknown verb exit 1"

echo "fx-watch tests: $pass passed, $fail failed"
[ "$fail" = 0 ]
