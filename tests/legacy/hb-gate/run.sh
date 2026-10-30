#!/usr/bin/env bash
# Offline tests for ~/bin/hb-gate (-> ~/plugins/heartbeat/ops/hb-gate): the token-free wake/skip gate the heartbeat
# mod asks before every beat. Covers the CLI (text/--json/--dry-run, log, stats, help), every wake/skip rule
# ((a) inbox, (b) Due dates/times incl. the year boundary, (c) startable work, (d) finished agents, (e) first beat,
# (f) safety valve), the agent-watch fallback, fail-open on every error, and log trimming. Then the plugin's own
# fixtures (bun test test/hb-gate.test.mjs) when bun is there; the plugin's other test files run in heartbeat-tests.
# Hermetic: temp HOME, every path through HB_GATE_* env, a fake clock in 2031/2032 (so a stray write to the real log
# would be visible), a fake agent-watch; tg/curl/ssh/docker/vault/unraid-ssh shims refuse and record.
# HB_GATE_BIN=<path> tests another copy.
set -uo pipefail
B=${HB_GATE_BIN:-/home/node/bin/hb-gate}
PLUGIN=/home/node/plugins/heartbeat
REAL_LOG=/home/node/.local/state/hb-gate.log
real_fake_lines() { grep -c '^203[12]-' "$REAL_LOG" 2>/dev/null || true; }
real_before=$(real_fake_lines)
T=$(mktemp -d "${TMPDIR:-/tmp}/hb-gate-tests.XXXXXX")
trap 'chmod -R u+w "${T:?}" 2>/dev/null; rm -rf "${T:?}"' EXIT
export HOME=$T/home FAKE=$T/fake
mkdir -p "$T/shims" "$FAKE" "$HOME/inbox" "$HOME/workspace"
mk() { printf '#!/bin/bash\n%s\n' "$2" > "$T/shims/$1"; chmod +x "$T/shims/$1"; }
for c in tg curl ssh docker vault unraid-ssh; do mk "$c" "echo \"$c \$*\" >> \"\$FAKE/refused.log\"; echo \"$c shim: refusing\" >&2; exit 99"; done
# Fake agent-watch: `status --hours 6` prints $FAKE/aw.out and exits with $FAKE/aw.rc (default 0); records calls.
mk agent-watch 'echo "agent-watch $*" >> "$FAKE/aw.calls"; cat "$FAKE/aw.out" 2>/dev/null; exit "$(cat "$FAKE/aw.rc" 2>/dev/null || echo 0)"'
export PATH=$T/shims:$PATH
# Fake wake: `check --json --inbox DIR` prints $FAKE/wake.out, sleeps $FAKE/wake.sleep s, exits $FAKE/wake.rc; records calls.
mk wake 'echo "wake $*" >> "$FAKE/wake.calls"; [ -s "$FAKE/wake.sleep" ] && sleep "$(cat "$FAKE/wake.sleep")"; cat "$FAKE/wake.out" 2>/dev/null; exit "$(cat "$FAKE/wake.rc" 2>/dev/null || echo 0)"'
export HB_GATE_INBOX=$HOME/inbox HB_GATE_TODO=$HOME/workspace/TODO.md HB_GATE_GOALS=$HOME/workspace/GOALS.md \
  HB_GATE_MARKER=$T/heartbeat HB_GATE_LOG=$HOME/.local/state/hb-gate.log HB_GATE_OFF=$HOME/.config/hb-gate/off \
  HB_GATE_AGENT_WATCH=$T/shims/agent-watch HB_GATE_WAKE=$T/shims/wake \
  HB_GATE_RULES_OFF=$HOME/.config/hb-gate/wake-rules-off

pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2" | head -20; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
has() { if [[ "$2" == *"$3"* ]]; then ok "$1"; else no "$1" "$2"; fi; }
hasnt() { if [[ "$2" == *"$3"* ]]; then no "$1" "$2"; else ok "$1"; fi; }

ep() { TZ=Europe/Zurich date -d "$1" +%s; }        # "YYYY-MM-DD HH:MM" Zurich -> epoch s
NOW="2031-06-15 14:00"
setnow() { NOW=$1; NOW_S=$(ep "$1"); }
setnow "2031-06-15 14:00"
# todo DUE IDEAS NIGHT: each a "|"-separated list of bullet texts ("" = none). TODO.md mtime = NOW - 1 h.
todo() {
  { echo "# TODO"; echo; echo "## Due"; [ -n "$1" ] && tr '|' '\n' <<<"$1" | sed 's/^/- /'
    echo; echo "## In progress"; echo "- **Something** — running"
    echo; echo "## Ideas"; [ -n "${2:-}" ] && tr '|' '\n' <<<"$2" | sed 's/^/- /'
    echo; echo "## Night backlog"; [ -n "${3:-}" ] && tr '|' '\n' <<<"${3:-}" | sed 's/^/- /'
  } > "$HB_GATE_TODO"
  touch -d "@$((NOW_S - 3600))" "$HB_GATE_TODO"
}
goals() { printf '# Goals\n\n## Now\n'; printf '%s\n' "$@"; }
beat_ago() { touch -d "@$((NOW_S - $1))" "$HB_GATE_MARKER"; }     # the marker = the mod's last beat
reset() {   # wake rules OFF (the switch file present): every section before "# wake rules" is today's gate
  find "$HOME/inbox" -mindepth 1 -delete; rm -f "$HB_GATE_OFF" "$HB_GATE_LOG" "$HB_GATE_MARKER" "$FAKE/aw.out" \
    "$FAKE/aw.rc" "$FAKE/aw.calls" "$FAKE/wake.out" "$FAKE/wake.rc" "$FAKE/wake.sleep" "$FAKE/wake.calls"
  mkdir -p "${HB_GATE_RULES_OFF%/*}"; touch "$HB_GATE_RULES_OFF"
  todo "" "" ""; goals > "$HB_GATE_GOALS"; beat_ago 600
}
# view NOW BEAT DIGEST MUTE [HOLD_UNTIL_EPOCH [WHY]]: the fake `wake check --json` answer (counts + kinds)
view() {
  local hold=null
  [ -n "${5:-}" ] && hold="{\"id\": 7, \"until\": $5, \"why\": \"${6:-}\", \"created\": 0}"
  printf '{"ok": true, "ts": 0, "db": "ok", "registry_error": null, "rules": 2, "hold": %s, "counts": {"now": %s, "beat": %s, "digest": %s, "mute": %s}, "kinds": {"now": {"note.voice": %s}, "beat": {"cron.fail": %s}, "digest": {"note.rave-radar": %s}, "mute": {}}, "classes": {}}\n' \
    "$hold" "$1" "$2" "$3" "$4" "$1" "$2" "$3" > "$FAKE/wake.out"
}
rules_on() { rm -f "$HB_GATE_RULES_OFF"; }
# gate ARGS...: JSON run at the fake clock; sets out, rc, dec, why
gate() { out=$(HB_GATE_NOW="$NOW" timeout 20 "$B" --json "$@" 2>&1 </dev/null); rc=$?
         dec=$(jq -r .decision <<<"$out" 2>/dev/null); why=$(jq -r .reason <<<"$out" 2>/dev/null); }
A0=(--running 0 --finished 0)
A1=(--running 1 --finished 0 --ids abcdef1234567890,bbbbbbbbcccc)

echo "# CLI"
reset
out=$("$B" --help 2>&1); rc=$?
eq "--help exits 0" "$rc" 0; has "--help prints the usage" "$out" "hb-gate [--json]"
out=$(HB_GATE_NOW="$NOW" "$B" --notes 1 "${A0[@]}" 2>&1); rc=$?
eq "text mode: one 'decision: reason' line" "$out" "wake: inbox: 1 note(s)"; eq "text mode exits 0" "$rc" 0
rm -f "$HB_GATE_LOG"
gate --notes 1 "${A0[@]}"
eq "--json: one JSON line" "$(wc -l <<<"$out")" 1
eq "--json: decision/reason/checks" "$(jq -c '[.decision, .reason, .checks.notes]' <<<"$out")" '["wake","inbox: 1 note(s)",1]'
eq "a decision is logged with the clock" "$(cat "$HB_GATE_LOG")" "2031-06-15 14:00:00 wake inbox: 1 note(s)"
gate --notes 1 --dry-run "${A0[@]}"
eq "--dry-run decides but does not log" "$(wc -l < "$HB_GATE_LOG")" 1
rm -f "$HB_GATE_LOG"
eq "log with no log yet" "$("$B" log)" "(no log yet)"
printf '2031-06-14 23:59:00 wake x\n2031-06-15 00:01:00 skip y\n2031-06-15 09:00:00 wake z\n2031-06-15 10:00:00 skip w\n2031-06-15 11:00:00 skip v\n' > "$HB_GATE_LOG"
eq "log N: the last N lines" "$("$B" log 2)" "$(printf '2031-06-15 10:00:00 skip w\n2031-06-15 11:00:00 skip v')"
eq "log: default 20 lines" "$("$B" log | wc -l)" 5
eq "log 0 prints nothing (not the whole log)" "$("$B" log 0)" ""
eq "stats: today's wakes and skips only" "$(HB_GATE_NOW="$NOW" "$B" stats)" "2031-06-15: 1 wake, 3 skip"
rm -f "$HB_GATE_LOG"
eq "stats with no log" "$(HB_GATE_NOW="$NOW" "$B" stats)" "2031-06-15: 0 wake, 0 skip"

echo "# (a) inbox"
reset
touch "$HOME/inbox/.hidden"; mkdir "$HOME/inbox/sub"; ln -s /etc/hostname "$HOME/inbox/link.md"
gate "${A0[@]}"
eq "hidden files, folders and symlinks are not notes" "$(jq .checks.notes <<<"$out")" 0
echo x > "$HOME/inbox/a.md"; echo y > "$HOME/inbox/b.md"
gate "${A1[@]}"
eq "two notes wake even with an agent running" "$dec|$why" "wake|inbox: 2 note(s)"
gate --notes 5 "${A0[@]}"
eq "--notes counts when higher than the folder" "$why" "inbox: 5 note(s)"
reset; rmdir "$HOME/inbox"
gate "${A1[@]}"
eq "no inbox folder: 0 notes, no error" "$dec|$(jq .checks.notes <<<"$out")" "skip|0"
mkdir -p "$HOME/inbox"

echo "# (b) Due"
reset
due() { todo "$1" "" ""; gate "${A1[@]}"; }   # an agent runs, so only a due item can wake
due '**Pay the placeholder bill** — no date'
eq "no date, no time: due now" "$dec|$why" "wake|due: Pay the placeholder bill"
due '**Call** — 18:00';            eq "a time later today: not yet" "$dec" skip
due '**Call** — 13:30';            eq "a time that passed today: due" "$dec" wake
due '**Call** — 14:00';            eq "the exact minute: due" "$dec" wake
due '**Trip** — 20.06';            eq "DD.MM later this year: not yet" "$dec" skip
due '**Trip** — 15.06';            eq "DD.MM today, no time: due" "$dec" wake
due '**Trip** — 16.06 09:00';      eq "DD.MM HH:MM tomorrow: not yet" "$dec" skip
due '**Trip** — 15.06 15:30';      eq "DD.MM HH:MM later today: not yet" "$dec" skip
due '**Trip** — 10.06.';           eq "DD.MM. in the past: due" "$dec" wake
due '**Renewal** — 01.03.2032';    eq "DD.MM.YYYY next year: not yet" "$dec" skip
due '**Renewal** — 2031-06-14';    eq "ISO date in the past: due" "$dec" wake
due '**Renewal** — 2031-07-01 08:00'; eq "ISO date in the future: not yet" "$dec" skip
due '**Odd** — 31.02';             eq "an impossible date is ignored (no date left: due now)" "$dec" wake
due '**Two dates** — 20.06 or 14.06'; eq "the earliest date counts" "$dec" wake
due '**Brief** — tomorrow 09:00';  eq "tomorrow, anchored on TODO.md's day: not yet" "$dec" skip
# the board's due phrase wins over history times and dates in file names (agenda/hb-gate due fix, 30.10)
due '**Re-measure** — due 18:00 20.06 — write ~/reports/r/remeasure-2031-06-20.md; see ~/reports/r/review-2031-06-14.md'
eq "board phrase 'due 18:00 20.06' beats an ISO date in a file name: not yet" "$dec" skip
due '**Verify fix** — due 15:00 15.06 — live since 22:04 14.06 (~/jobs/fix/apply.log)'
eq "board phrase 'due 15:00 15.06' beats a history time and date: not yet" "$dec" skip
due '**Verify fix** — due 13:00 15.06 — live since 22:04 14.06'
eq "board phrase 'due 13:00 15.06' passed: due" "$dec" wake
due '**Swap** — 16.06 10:00 — rest: notes from 10.06'
eq "board form '— DD.MM HH:MM' after the title: not yet" "$dec" skip
due '**Report** — read ~/reports/r-2031-06-01.md'
eq "a date only inside a path is no date: due now" "$dec" wake
due '**Report** — 20.06, notes in ~/reports/r-2031-06-01.md'
eq "no phrase: rules (b) without the path's date: not yet" "$dec" skip
todo '**Brief** — tomorrow 09:00' "" ""; touch -d "@$((NOW_S - 2 * 86400))" "$HB_GATE_TODO"; gate "${A1[@]}"
eq "tomorrow, TODO.md written two days ago: due" "$dec" wake
due '**A very long due item title that goes on and on and on** — now|**Second** — now'
eq "title cut at 40 chars, + count of the others" "$why" "due: A very long due item title that goes on… (+1)"
printf '# TODO\n\n## Due today\n- **Prefix heading**\n  - sub-bullet 01.01\n\n## Ideas\n' > "$HB_GATE_TODO"; gate "${A1[@]}"
eq "heading matched by prefix; sub-bullets ignored" "$why" "due: Prefix heading"
printf '# TODO\n\n## In progress\n- **Not due** — x\n' > "$HB_GATE_TODO"; gate "${A1[@]}"
eq "no Due section at all: nothing due" "$dec|$(jq .checks.due <<<"$out")" "skip|0"
echo "# (b) Due across the year boundary (DD.MM without a year)"
setnow "2031-12-20 14:00"; beat_ago 600
due '**Renew the vignette** — 05.01'
eq "20.12: '05.01' is next January, not overdue" "$dec" skip
due '**Renew the vignette** — 05.01 09:00'
eq "20.12: '05.01 09:00' is next January, not overdue" "$dec" skip
due '**Pay** — 18.12';             eq "20.12: '18.12' is two days overdue" "$dec" wake
setnow "2032-01-03 14:00"; beat_ago 600
due '**Pay the December bill** — 28.12'
eq "03.01: '28.12' (last week) is overdue" "$dec" wake
due '**Pay** — 10.01';             eq "03.01: '10.01' is next week" "$dec" skip
due '**Pay** — 01.01.2033';        eq "an explicit year is never moved" "$dec" skip
setnow "2031-06-15 14:00"

echo "# (d) (e) (f) agents, first beat, safety valve"
reset
gate --running 1 --finished 0 --ids abcdef1234567890,bbbbbbbbcccc,cccccccc1,dddddddd2
eq "an agent running: skip, first 3 ids cut to 8 chars" "$dec|$why" "skip|agent running: 1 (abcdef12, bbbbbbbb, cccccccc)"
gate --running 2 --finished 0
eq "running without ids: no tag" "$why" "agent running: 2"
gate --running 1 --finished 1
eq "an agent finished: wake" "$dec|$why" "wake|1 agent(s) finished since the last beat"
gate --running 1 --finished 0 --first
eq "--first: wake" "$dec|$why" "wake|first beat after a restart"
rm -f "$HB_GATE_MARKER"; gate "${A1[@]}"
eq "no beat on record: safety valve" "$dec|$why" "wake|safety valve: no beat on record"
gate "${A1[@]}" --last-beat "$(( (NOW_S - 4 * 3600) * 1000 ))"
eq "--last-beat in ms, 4 h ago: safety valve" "$why" "safety valve: last beat 4.0 h ago"
gate "${A1[@]}" --last-beat "$(( NOW_S - 3 * 3600 + 60 ))"
eq "--last-beat in s, just under 3 h: no valve" "$dec" skip
beat_ago 300; gate "${A1[@]}" --last-beat "$(( NOW_S - 5 * 3600 ))"
eq "the newer of marker and --last-beat counts" "$dec|$(jq .checks.last_beat_min <<<"$out")" "skip|5"

# DST nights (Zurich): a Due time is reached at the first instant the wall clock shows it
utc() { date -u -d "$1 UTC" +%s; }
NOW=$(utc "2026-10-25 00:10"); NOW_S=$NOW    # 02:10 CEST, first pass of the repeated hour
due '**Call** — 2026-10-25 02:30';  eq "autumn 02:10 CEST, due 02:30: not yet" "$dec" skip
NOW=$(utc "2026-10-25 01:10"); NOW_S=$NOW    # 02:10 CET, second pass: 02:30 CEST passed 40 min ago
due '**Call** — 2026-10-25 02:30';  eq "autumn 02:10 CET (second pass), due 02:30: due" "$dec" wake
due '**Call** — 2026-10-25 02:50';  eq "autumn second pass, due 02:50 (passed at 02:50 CEST): due" "$dec" wake
due '**Call** — 2026-10-25 03:30';  eq "autumn second pass, due 03:30: not yet" "$dec" skip
NOW=$(utc "2027-03-28 00:59"); NOW_S=$NOW    # 01:59 CET, just before the skipped hour
due '**Call** — 2027-03-28 02:30';  eq "spring 01:59 CET, due 02:30 (skipped): not yet" "$dec" skip
NOW=$(utc "2027-03-28 01:00"); NOW_S=$NOW    # 03:00 CEST: the skipped 02:30 is due at once
due '**Call** — 2027-03-28 02:30';  eq "spring 03:00 CEST, due 02:30 (skipped): due" "$dec" wake
due '**Call** — 2027-03-28 03:30';  eq "spring 03:00 CEST, due 03:30: not yet" "$dec" skip
setnow "2031-06-15 14:00"

echo "# (c) startable work"
reset
gate "${A0[@]}"
eq "nothing startable: skip" "$dec|$why" "skip|no agent running, nothing startable"
goals '- [ ] Build the placeholder feature' '- [x] Done step' '- [ ] Ask him about the thing' \
  '- [ ] Waiting for the parts' '- [ ] Something (on hold)' > "$HB_GATE_GOALS"; gate "${A0[@]}"
eq "an open, unblocked goal step: wake" "$dec|$why" "wake|no agent running; startable: goals 1, ideas 0, night 0"
goals '- [ ] Ask him about the thing' '- [ ] after his reply' > "$HB_GATE_GOALS"
todo "" '**Idea one** — buildable|**Idea two** — waiting on parts|**Idea three** (owner: later)' '**Night A**|**Night B**'
gate "${A0[@]}"
eq "blocked goals/ideas don't count; night backlog does" "$why" "no agent running; startable: goals 0, ideas 1, night 2"
rm -f "$HB_GATE_GOALS"; todo "" "" "**Night A**"; gate "${A0[@]}"
eq "no GOALS.md: not an error" "$dec|$why" "wake|no agent running; startable: goals 0, ideas 0, night 1"
todo "" "" ""; gate "${A1[@]}"
eq "an agent running beats startable work: skip" "$dec" skip

echo "# agent-watch fallback (no --running from the mod)"
reset
aw() { printf 'Background agents (6 h)\nID STATE AGE SILENT TITLE\n'; printf '%s\n' "$@"; }
aw 'a1b2c3d4e5 running 12m 1m build' > "$FAKE/aw.out"; gate
eq "fallback: asks agent-watch status --hours 6" "$(cat "$FAKE/aw.calls")" "agent-watch status --hours 6"
eq "fallback: a running agent: skip" "$dec|$why" "skip|agent running: 1 (a1b2c3d4)"
aw 'a1 stalled 60m 30m x' > "$FAKE/aw.out"; gate
eq "stalled <= 45 min counts as running" "$dec" skip
aw 'a1 stalled 90m 50m x' > "$FAKE/aw.out"; goals '- [ ] Step' > "$HB_GATE_GOALS"; gate
eq "stalled > 45 min no longer blocks" "$dec|$(jq .checks.running <<<"$out")" "wake|0"
aw 'a1 done 30m 2m x' > "$FAKE/aw.out"; gate
eq "ended after the last beat (10 min ago): finished -> wake" "$why" "1 agent(s) finished since the last beat"
aw 'a1 done 30m 20m x' > "$FAKE/aw.out"; gate
eq "ended before the last beat: not new" "$(jq .checks.finished <<<"$out")" 0
printf 'Background agents (6 h)\n(none)\n' > "$FAKE/aw.out"; gate
eq "(none): no agents, no error" "$dec|$why" "wake|no agent running; startable: goals 1, ideas 0, night 0"
aw 'garbage line' 'x y z' > "$FAKE/aw.out"; gate
eq "short/odd table rows are skipped" "$(jq -c '[.checks.running, .checks.finished]' <<<"$out")" "[0,0]"
echo "something else" > "$FAKE/aw.out"; gate
eq "no table: fail open" "$dec|$why" "wake|gate error: RuntimeError: agent-watch status: no table"
aw 'a1 running 1m 1m x' > "$FAKE/aw.out"; echo 3 > "$FAKE/aw.rc"; gate
eq "agent-watch exit 3: fail open" "$dec|$why" "wake|gate error: RuntimeError: agent-watch status exit 3"
rm -f "$FAKE/aw.rc"
out=$(HB_GATE_NOW="$NOW" HB_GATE_AGENT_WATCH=$T/missing timeout 20 "$B" --json 2>&1); rc=$?
eq "agent-watch missing: fail open, exit 0" "$(jq -r .decision <<<"$out")|$rc" "wake|0"
has "agent-watch missing: the reason names it" "$(jq -r .reason <<<"$out")" "gate error: FileNotFoundError"

echo "# fail open"
reset
mkdir -p "$(dirname "$HB_GATE_OFF")"; touch "$HB_GATE_OFF"; gate "${A1[@]}"
eq "off switch: always wake" "$dec|$why" "wake|gate off (~/.config/hb-gate/off)"
rm -f "$HB_GATE_OFF"
gate --bogus; eq "unknown argument: wake" "$dec|$why|$rc" "wake|gate error: ValueError: unknown argument --bogus|0"
gate --notes; eq "a flag without its value: wake" "$why" "gate error: ValueError: --notes needs a value"
gate --running x; has "a non-number: wake" "$dec|$why" "wake|gate error: ValueError"
rm -f "$HB_GATE_TODO"; gate "${A1[@]}"
has "TODO.md missing: wake" "$dec|$why" "wake|gate error: FileNotFoundError"
todo "" "" ""; printf '\xff\xfe broken utf-8\n' >> "$HB_GATE_TODO"; gate "${A1[@]}"
eq "undecodable TODO.md: still an answer, exit 0" "$(jq -r .decision <<<"$out" | grep -cE '^(wake|skip)$')|$rc" "1|0"
out=$(HB_GATE_NOW="not a time" timeout 20 "$B" --json "${A1[@]}" 2>&1); rc=$?
eq "a broken clock: still a JSON wake, exit 0" "$(jq -r .decision <<<"$out")|$rc" "wake|0"
todo "" "" ""
mkdir -p "$T/ro"; chmod 555 "$T/ro"
out=$(HB_GATE_NOW="$NOW" HB_GATE_LOG=$T/ro/sub/hb.log timeout 20 "$B" "${A1[@]}" 2>&1); rc=$?
eq "unwritable log: the decision still prints, exit 0" "$out|$rc" "skip: agent running: 1 (abcdef12, bbbbbbbb)|0"

echo "# wake rules: switch present = today's gate"
reset
gate --notes 1 "${A0[@]}"
eq "rules off: notes wake as before" "$dec|$why" "wake|inbox: 1 note(s)"
eq "rules off: wake is never asked" "$(cat "$FAKE/wake.calls" 2>/dev/null)" ""
rm -f "$HB_GATE_LOG"; gate --now-only "${A1[@]}"
eq "rules off: the now lane always skips" "$dec|$why" "skip|now lane: wake rules off"
eq "a now-lane skip is not logged" "$(cat "$HB_GATE_LOG" 2>/dev/null)" ""

echo "# wake rules: on"
reset; rules_on
view 1 0 0 0; gate "${A1[@]}"
eq "a now-class note wakes even while an agent runs" "$dec|$why" "wake|now: 1 note(s) (note.voice)"
eq "the gate asks wake check with its inbox" "$(cat "$FAKE/wake.calls")" "wake check --json --inbox $HOME/inbox"
view 0 0 2 1; gate --notes 3 "${A1[@]}"
eq "digest/mute notes don't count (the mod's --notes is ignored): agent running" "$dec|$why" "skip|agent running: 1 (abcdef12, bbbbbbbb)"
eq "the checks name the classes" "$(jq -c '[.checks.notes, .checks.now_notes, .checks.digest, .checks.mute]' <<<"$out")" "[0,0,2,1]"
gate --notes 3 "${A0[@]}"
eq "digest/mute only, nothing startable: skip" "$dec|$why" "skip|no agent running, nothing startable"
view 0 2 1 0; gate "${A1[@]}"
eq "beat-class notes wake as before (digest/mute counted aside)" "$dec|$why" "wake|inbox: 2 note(s) (+1 digest/mute)"
H=$((NOW_S + 7200))
view 0 0 0 0 "$H" "quiet night"
todo "" "**Idea A** — buildable now" ""
gate "${A0[@]}"
eq "a hold skips the idle work beat (c)" "$dec|$why" "skip|held until 15.06 16:00: quiet night"
eq "the hold is in the checks" "$(jq -r .checks.hold_until <<<"$out")" "$H"
todo "**Report** — 13:00" "**Idea A** — buildable now" ""
gate "${A0[@]}"; eq "a hold never holds a Due item" "$dec|$why" "wake|due: Report"
todo "" "**Idea A** — buildable now" ""
gate --running 0 --finished 1; eq "a hold never holds a finished agent" "$dec" "wake"
gate --first "${A0[@]}"; eq "a hold never holds the first beat" "$dec|$why" "wake|first beat after a restart"
beat_ago $((4 * 3600)); gate "${A0[@]}"; has "the 3 h safety valve stays during a hold" "$dec|$why" "wake|safety valve: last beat 4.0 h ago"
beat_ago 600
view 0 0 0 0 "$((NOW_S - 60))" "over"; gate "${A0[@]}"
has "an expired hold holds nothing" "$dec|$why" "wake|no agent running; startable"
view 1 0 0 0 "$H" "quiet"; gate "${A1[@]}"
eq "now-class notes wake through a hold" "$dec|$why" "wake|now: 1 note(s) (note.voice)"

echo "# wake rules: the now lane (--now-only)"
rm -f "$HB_GATE_LOG"
view 2 1 0 0; gate --now-only "${A1[@]}"
eq "now lane: wakes for a now-class note" "$dec|$why" "wake|now: 2 note(s) (note.voice ×2) [now lane]"
eq "now lane: its wake is logged" "$(cat "$HB_GATE_LOG")" "2031-06-15 14:00:00 wake now: 2 note(s) (note.voice ×2) [now lane]"
view 0 3 0 0; gate --now-only "${A1[@]}"
eq "now lane: beat-class notes wait for the regular path" "$dec|$why" "skip|now lane: no now-class note"
eq "now lane: misses are not logged" "$(wc -l < "$HB_GATE_LOG")" 1

echo "# wake rules: unreadable = today's gate (fail open)"
view 0 0 1 0; echo 3 > "$FAKE/wake.rc"; gate --notes 1 "${A1[@]}"
eq "wake check exit 3: notes wake as before, marked" "$dec|$why" "wake|inbox: 1 note(s) [wake rules unreadable: wake check exit 3]"
gate "${A1[@]}"
eq "...and skips as before" "$dec|$why" "skip|agent running: 1 (abcdef12, bbbbbbbb) [wake rules unreadable: wake check exit 3]"
eq "...checks say so" "$(jq -r .checks.rules <<<"$out")" "unreadable"
rm -f "$HB_GATE_LOG"; gate --now-only "${A1[@]}"
eq "...the now lane skips (no extra beat)" "$dec|$why" "skip|now lane: wake rules unreadable (wake check exit 3)"
eq "...not logged" "$(cat "$HB_GATE_LOG" 2>/dev/null)" ""
rm -f "$FAKE/wake.rc"; echo 'not json' > "$FAKE/wake.out"; gate --notes 1 "${A0[@]}"
eq "garbage from wake: as before" "$dec|$why" "wake|inbox: 1 note(s) [wake rules unreadable: wake check: bad output]"
echo '{"ok": false}' > "$FAKE/wake.out"; gate --notes 1 "${A0[@]}"
eq "ok false from wake: as before" "$dec|$why" "wake|inbox: 1 note(s) [wake rules unreadable: wake check: bad output]"
todo "" "" ""; view 0 0 0 0; echo 8 > "$FAKE/wake.sleep"; t0=$(date +%s); gate "${A0[@]}"; t1=$(date +%s)
eq "a hanging wake times out after 5 s: as before" "$dec|$why" "skip|no agent running, nothing startable [wake rules unreadable: wake check: TimeoutExpired]"
[ $((t1 - t0)) -le 8 ] && ok "...within the timeout ($((t1 - t0)) s)" || no "the wake timeout" "$((t1 - t0)) s"
rm -f "$FAKE/wake.sleep"
out=$(HB_GATE_WAKE=$T/nonexistent HB_GATE_NOW="$NOW" timeout 20 "$B" --json --notes 1 "${A0[@]}" 2>&1)
eq "no wake binary: as before" "$(jq -r .reason <<<"$out")" "inbox: 1 note(s) [wake rules unreadable: wake check: FileNotFoundError]"
reset; rules_on; view 0 0 0 0; touch "$HB_GATE_OFF"; gate "${A1[@]}"
eq "the gate's off switch still wins" "$dec|$why" "wake|gate off (~/.config/hb-gate/off)"

WB=${WAKE_BIN:-/home/node/bin/wake}
echo "# wake rules: the real wake CLI ($WB, temp store)"
if [ -x "$WB" ] && [ -z "${SKIP_REAL_WAKE:-}" ]; then
  reset; rules_on
  wk() { ASSISTANT_TEST=1 ASSISTANT_TZ=Europe/Zurich ASSISTANT_NOW="$(TZ=Europe/Zurich date -d "$NOW" --iso-8601=seconds)" \
         WAKE_REMIND=$T/shims/remind timeout 30 "$WB" "$@"; }
  touch "$HOME/inbox/voice-1792600000.md" "$HOME/inbox/rave-radar-1792600000.md"
  wr() { out=$(HB_GATE_WAKE=$WB ASSISTANT_TEST=1 ASSISTANT_TZ=Europe/Zurich \
         ASSISTANT_NOW="$(TZ=Europe/Zurich date -d "$NOW" --iso-8601=seconds)" HB_GATE_NOW="$NOW" timeout 30 "$B" --json "$@" 2>&1)
         dec=$(jq -r .decision <<<"$out" 2>/dev/null); why=$(jq -r .reason <<<"$out" 2>/dev/null); }
  wr "${A1[@]}"; eq "real wake: a voice note is now-class" "$dec|$why" "wake|now: 1 note(s) (note.voice)"
  rm -f "$HOME/inbox/voice-1792600000.md"
  wr "${A1[@]}"; eq "real wake: a rave-radar note is beat by default" "$dec|$why" "wake|inbox: 1 note(s)"
  wk on 'note.rave-*' mute "test" >/dev/null; wr "${A1[@]}"
  eq "real wake: muted by a rule, agent running" "$dec|$why" "skip|agent running: 1 (abcdef12, bbbbbbbb)"
  touch "$HOME/inbox/odd-new-kind-1792600000.md"; wk on '*' mute "everything" >/dev/null; wr "${A1[@]}"
  eq "real wake: a wildcard never mutes an unknown kind" "$dec|$why" "wake|inbox: 1 note(s) (+1 digest/mute)"
  rm -f "$HOME/inbox/odd-new-kind-1792600000.md"; todo "" "**Idea A** — buildable now" ""
  wk next +2h "quiet" >/dev/null; wr "${A0[@]}"
  eq "real wake: the hold skips the idle work beat" "$dec|$why" "skip|held until 15.06 16:00: quiet"
  wk rm hold >/dev/null; wr "${A0[@]}"
  has "real wake: hold ended, (c) wakes" "$dec|$why" "wake|no agent running; startable"
  eq "real wake: its store stayed in the temp home" "$(ls "$HOME/.local/state/assistant/events.db" 2>/dev/null | wc -l)" 1
else echo "skip - real wake CLI (not installed or SKIP_REAL_WAKE)"; fi

echo "# log trimming"
reset
python3 -c 'import sys
with open(sys.argv[1], "w") as f:
    for i in range(6000): f.write("2031-06-14 10:00:00 skip padding line %05d %s\n" % (i, "x" * 60))' "$HB_GATE_LOG"
gate "${A1[@]}"
eq "over 512 KB: trimmed to the last 2000 lines" "$(wc -l < "$HB_GATE_LOG")" 2000
eq "the new decision is the last line" "$(tail -1 "$HB_GATE_LOG")" "2031-06-15 14:00:00 skip agent running: 1 (abcdef12, bbbbbbbb)"
eq "no leftover .tmp" "$(ls "$HB_GATE_LOG".tmp 2>/dev/null | wc -l)" 0

echo "# hermetic"
eq "no tg/curl/ssh/docker/vault/unraid-ssh calls" "$(cat "$FAKE/refused.log" 2>/dev/null)" ""
eq "the real hb-gate log got no fake-clock lines" "$(real_fake_lines)" "$real_before"

echo "# plugin fixtures (bun test test/hb-gate.test.mjs)"
if command -v bun >/dev/null && [ -z "${HB_GATE_BIN:-}" ]; then
  if r=$(cd "$PLUGIN" && timeout 120 bun test test/hb-gate.test.mjs 2>&1 </dev/null); then ok "bun fixtures pass ($(grep -oE '[0-9]+ pass' <<<"$r"))"
  else no "bun fixtures" "$(tail -15 <<<"$r")"; fi
else echo "skip - bun fixtures (no bun, or HB_GATE_BIN set)"; fi

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
