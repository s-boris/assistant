#!/usr/bin/env bash
# Offline tests for ~/bin/waiting-nudge (cue to ask the owner about the board's "Waiting on you" items). Exit 0 = all pass.
# Inputs: a board feed fixture (WAITING_NUDGE_FEED, shaped like ~/.local/state/board/board.json), a stub board
# (WAITING_NUDGE_BOARD) and WAITING_NUDGE_INBOX / _TODAY / _NOW; HOME is a temp dir too, so a broken seam can't reach the
# real feed, board or ~/inbox. The real inbox is checked at the end.
# Covers: Waiting rows only, list format, since = the item's created, asked from the feed, --cron (ONE note, 2-day rule,
# order, overwrite, board ids in the note), asked -> `board asked`, a missing / stale / broken feed (no cue, one stderr
# line, exit 0), no state file of its own, defaults under $HOME.
# WAITING_NUDGE_BIN=<path> tests another copy.
set -uo pipefail
W=${WAITING_NUDGE_BIN:-$(cd "$(dirname "$0")/../../../scripts" && pwd)/waiting-nudge}
REAL_INBOX=/home/node/inbox
T=$(mktemp -d "${TMPDIR:-/tmp}/waiting-nudge-tests.XXXXXX")
trap 'rm -rf "$T"' EXIT
export TZ=UTC HOME=$T/home WAITING_NUDGE_FEED=$T/feed/board.json WAITING_NUDGE_INBOX=$T/inbox WAITING_NUDGE_BOARD=$T/board
mkdir -p "$HOME" "$T/inbox" "$T/feed"
real_notes() { ls "$REAL_INBOX" 2>/dev/null | grep -c '^waiting-nudge-'; }
real_notes_before=$(real_notes)

pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2" | head -30; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
has() { if [[ "$2" == *"$3"* ]]; then ok "$1"; else no "$1" "$2"; fi; }
hasnt() { if [[ "$2" == *"$3"* ]]; then no "$1" "$2"; else ok "$1"; fi; }
NOW=$(date +%s)
run() { WAITING_NUDGE_TODAY=$1 WAITING_NUDGE_NOW=$NOW "$W" "${@:2}"; }            # run DATE ARGS...
notes() { ls -A "$T/inbox" | wc -l; }
LONG=$(printf 'L%.0s' $(seq 1 200))

cat > "$T/board" <<'EOF'
#!/bin/sh
printf '%s\n' "$*" >> "$(dirname "$0")/board.calls"
echo "marked 1"
EOF
chmod +x "$T/board"

# feed FILE FEED_TS ROW... ; ROW = "id|section|title|created YYYY-MM-DD|asked YYYY-MM-DD or -" (12:00 UTC)
feed() {
  python3 - "$@" <<'PY'
import json, sys, datetime as dt
def ts(d): return dt.datetime.fromisoformat(d + 'T12:00:00+00:00').timestamp()
p, now, *rows = sys.argv[1:]
items = []
for r in rows:
    i, sec, title, created, asked = r.split('|')
    items.append({'id': i, 'section': sec, 'title': title, 'due_ts': None, 'created': ts(created),
                  'asked': None if asked == '-' else ts(asked), 'flags': [], 'probes': []})
json.dump({'ts': float(now), 'mode': 'owner', 'coverage': {}, 'items': items}, open(p, 'w'), ensure_ascii=False)
PY
}
base() {   # base [ASKED_TAX]
  feed "$WAITING_NUDGE_FEED" "$NOW" \
    "d1|Due|Pay the dentist bill|2026-10-01|-" \
    "a1|Waiting on you|Tax extension OK?|2026-10-10|${1:--}" \
    "a2|Waiting on you|Seerr login|2026-10-10|-" \
    "a3|Waiting on you|Plain item without bold — still counts|2026-10-10|-" \
    "a4|Waiting on you|$LONG|2026-10-10|-" \
    "p1|In progress|Building the thing|2026-10-05|-" \
    "i1|Ideas|Idea not waiting|2026-10-01|-"
}

echo "# list"
base
out=$(run 2026-10-10 2>"$T/err"); rc=$?
eq "list: rc 0" "$rc" "0"
eq "list: no stderr" "$(cat "$T/err")" ""
eq "list: one line per waiting item" "$(printf '%s\n' "$out" | wc -l)" "4"
eq "list: first line format (since = created)" "$(printf '%s\n' "$out" | sed -n 1p)" "2026-10-10  asked never       Tax extension OK?"
has "list: title as in the feed" "$out" "  Plain item without bold — still counts"
hasnt "list: Due rows ignored" "$out" "dentist"
hasnt "list: In progress ignored" "$out" "Building"
hasnt "list: Ideas ignored" "$out" "Idea not waiting"
eq "list: long title cut to 160 chars" "$(printf '%s\n' "$out" | grep -o 'L*$' | awk '{print length}' | sort -n | tail -1)" "160"
eq "list: no inbox note" "$(notes)" "0"
eq "list: no state file of its own" "$(find "$T" -name 'waiting-nudge.json*' | wc -l)" "0"

echo "# --cron and the 2-day rule"
out=$(run 2026-10-10 --cron 2>"$T/err"); rc=$?
eq "cron day 0: rc 0, silent" "$rc:$out$(cat "$T/err")" "0:"
eq "cron day 0: no note" "$(notes)" "0"
run 2026-10-11 --cron
eq "cron day 1: no note (under 2 days)" "$(notes)" "0"
out=$(run 2026-10-12 --cron 2>&1); rc=$?
eq "cron day 2: rc 0, silent" "$rc:$out" "0:"
eq "cron day 2: exactly one note" "$(notes)" "1"
N=$T/inbox/waiting-nudge-2026-10-12.md
eq "cron: note named by date" "$([ -f "$N" ] && echo yes)" "yes"
has "cron: note explains the job" "$(cat "$N")" 'Then `board asked ID` for each item asked (ids below). Never in quiet hours.'
has "cron: note line format (board id)" "$(cat "$N")" "- Seerr login  (board a2, waiting since 2026-10-10, last asked never)"
eq "cron: all 4 due items listed" "$(grep -c '^- ' "$N")" "4"
hasnt "cron: other sections never in the note" "$(cat "$N")" "dentist"

echo "# asked dates from the feed"
rm -f "$T/inbox"/*
base 2026-10-12
run 2026-10-12 --cron
N=$T/inbox/waiting-nudge-2026-10-12.md
hasnt "cron: item asked today is not due" "$(cat "$N")" "Tax extension"
eq "cron: the other 3 are" "$(grep -c '^- ' "$N")" "3"
rm -f "$T/inbox"/*
run 2026-10-13 --cron
hasnt "cron: asked 1 day ago still not due" "$(cat "$T/inbox/waiting-nudge-2026-10-13.md")" "Tax extension"
run 2026-10-14 --cron
has "cron: asked 2 days ago is due again" "$(cat "$T/inbox/waiting-nudge-2026-10-14.md")" \
  "- Tax extension OK?  (board a1, waiting since 2026-10-10, last asked 2026-10-12)"
eq "cron: one note per day, older ones kept for me to handle" "$(notes)" "2"
first=$(grep '^- ' "$T/inbox/waiting-nudge-2026-10-14.md" | head -1)
has "cron: never-asked (oldest) items first" "$first" "last asked never"
eq "cron: last-asked item last" "$(grep '^- ' "$T/inbox/waiting-nudge-2026-10-14.md" | tail -1 | cut -c1-19)" "- Tax extension OK?"
run 2026-10-14 --cron
eq "cron: a second run the same day overwrites, still one note for the day" "$(ls "$T/inbox" | grep -c 2026-10-14)" "1"
rm -f "$T/inbox"/*
feed "$WAITING_NUDGE_FEED" "$NOW" "a1|Waiting on you|Tax extension OK?|2026-10-01|2026-10-20" \
  "a2|Waiting on you|Seerr login|2026-10-20|-"
run 2026-10-21 --cron
eq "cron: nothing due -> no note" "$(notes)" "0"

echo "# asked -> board asked"
base
out=$(run 2026-10-12 asked "SEERR"); rc=$?
eq "asked: passes board's output and rc" "$rc:$out" "0:marked 1"
eq "asked: runs board asked with the substring" "$(cat "$T/board.calls")" "asked SEERR"
out=$(run 2026-10-12 asked); rc=$?
eq "asked without substring lists, no board call" "$rc:$(wc -l < "$T/board.calls")" "0:1"
has "asked without substring: the list" "$out" "Seerr login"
out=$(WAITING_NUDGE_BOARD=$T/nope run 2026-10-12 asked x 2>"$T/err"); rc=$?
eq "asked: board missing -> rc 1, one stderr line" "$rc:$(wc -l < "$T/err")" "1:1"

echo "# missing, stale or broken feed: no cue, one stderr line, exit 0"
rm -f "$WAITING_NUDGE_FEED" "$T/inbox"/*
out=$(run 2026-10-30 --cron 2>"$T/err"); rc=$?
eq "missing: rc 0, no stdout" "$rc:$out" "0:"
eq "missing: one stderr line" "$(wc -l < "$T/err")" "1"
has "missing: says why" "$(cat "$T/err")" "no board feed"
eq "missing: no note" "$(notes)" "0"
feed "$WAITING_NUDGE_FEED" "$((NOW - 3 * 3600))" "a1|Waiting on you|Tax extension OK?|2026-10-01|-"
out=$(run 2026-10-30 --cron 2>"$T/err"); rc=$?
eq "stale (3 h): rc 0, no stdout, one stderr line" "$rc:$out:$(wc -l < "$T/err")" "0::1"
has "stale: says how old" "$(cat "$T/err")" "180 min old"
eq "stale: no note" "$(notes)" "0"
feed "$WAITING_NUDGE_FEED" "$((NOW - 3600))" "a1|Waiting on you|Tax extension OK?|2026-10-01|-"
run 2026-10-30 --cron 2>/dev/null
eq "1 h old feed still counts" "$(notes)" "1"
rm -f "$T/inbox"/*
echo '{broken' > "$WAITING_NUDGE_FEED"
out=$(run 2026-10-30 --cron 2>"$T/err"); rc=$?
eq "broken: rc 0, one stderr line, no traceback" "$rc:$(wc -l < "$T/err")" "0:1"
feed "$WAITING_NUDGE_FEED" "$NOW" "i1|Ideas|x|2026-10-01|-"
out=$(run 2026-10-30 --cron 2>&1); rc=$?
eq "empty section: rc 0, silent" "$rc:$out" "0:"
eq "empty section: no note" "$(notes)" "0"

echo "# defaults (no seams): everything under \$HOME"
H2=$T/home2; mkdir -p "$H2/.local/state/board" "$H2/inbox"
old=$(date -d '3 days ago' +%F); today=$(date +%F)
feed "$H2/.local/state/board/board.json" "$NOW" "a2|Waiting on you|Seerr login|$old|-"
out=$(env -u WAITING_NUDGE_FEED -u WAITING_NUDGE_INBOX -u WAITING_NUDGE_TODAY -u WAITING_NUDGE_NOW -u WAITING_NUDGE_BOARD HOME=$H2 "$W")
eq "default: reads \$HOME/.local/state/board/board.json" "$out" "$old  asked never       Seerr login"
env -u WAITING_NUDGE_FEED -u WAITING_NUDGE_INBOX -u WAITING_NUDGE_TODAY -u WAITING_NUDGE_NOW -u WAITING_NUDGE_BOARD HOME=$H2 "$W" --cron
eq "default: note in \$HOME/inbox, named by today" "$(ls "$H2/inbox")" "waiting-nudge-$today.md"

echo "# isolation"
eq "fake HOME never written while seams are set" "$(find "$HOME" -mindepth 1 | wc -l)" "0"
eq "no note in the real inbox" "$(real_notes)" "$real_notes_before"

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
