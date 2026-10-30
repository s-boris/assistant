#!/usr/bin/env bash
# Offline tests for ~/bin/restart-cause (boot-time note explaining a container start). Exit 0 = all pass.
# Seams: RESTART_CAUSE_PROC (a fake /proc: stat btime + 1/stat starttime), RESTART_CAUSE_NOW, RESTART_CAUSE_SSH (a
# fake unraid-ssh printing fixture output per verb, or failing), RESTART_CAUSE_DMESG (fake dmesg). HOME is a temp dir.
# Covers: auto-update window note (marker, next step, server facts, one file), idempotence per start, deliberate
# restart via restart-wake.log / ~/inbox/restart-<epoch>.md (silent, state kept), session-only (silent), host reboot,
# appdata backup run, OOM in dmesg, unknown + server unreachable, weekly window on the wrong day, recreated self,
# --dry-run writes nothing, --force, usage, interrupted agents from agent-watch's saved state (listed / skipped
# when ended, out of window or written after the start / owner title / none / corrupt state), the boot.sh line (timeout + background), the real inbox untouched.
# RESTART_CAUSE_BIN=<path> tests another copy.
set -uo pipefail
B=${RESTART_CAUSE_BIN:-$(cd "$(dirname "$0")/../../../scripts" && pwd)/restart-cause}
REAL_INBOX=/home/node/inbox
startfile=${RESTART_CAUSE_STARTFILE:-$HOME/boot.sh}
T=$(mktemp -d "${TMPDIR:-/tmp}/restart-cause-tests.XXXXXX")
trap 'rm -rf "$T"' EXIT
export HOME=$T/home TZ=Europe/Berlin RESTART_CAUSE_SSH_WAIT=0
export RESTART_CAUSE_PROC=$T/proc RESTART_CAUSE_SSH=$T/fake-ssh RESTART_CAUSE_DMESG=$T/fake-dmesg
mkdir -p "$HOME/inbox" "$HOME/.local/state" "$T/proc/1" "$T/fx"
before=$(ls "$REAL_INBOX" 2>/dev/null | grep -c '^restart-cause-')
pass=0; fail=0
ok() { pass=$((pass + 1)); }
bad() { fail=$((fail + 1)); echo "FAIL: $*"; }
check() { if eval "$2"; then ok; else bad "$1"; fi; }

BOOT=1792288869          # 2026-10-18 04:01:09 local (CEST)
HOSTB=1791964589         # host up since 2026-10-14
TICK=$(getconf CLK_TCK)
setproc() {  # setproc HOST_BOOT PID1_START
  printf 'cpu  1 2 3\nbtime %s\nprocesses 9\n' "$1" > "$T/proc/stat"
  f=""; for i in $(seq 1 18); do f="$f 0"; done   # fields 4..21; starttime = field 22
  printf '1 (docker-init) S%s %s 0 0\n' "$f" $(( ($2 - $1) * TICK )) > "$T/proc/1/stat"
}
cat > "$T/fake-ssh" <<'EOF'
#!/bin/sh
[ "$1" = run ] || exit 9
[ -f "$FX/fail" ] && exit 255
cat "$FX/$2.txt" 2>/dev/null || exit 1
EOF
cat > "$T/fake-dmesg" <<'EOF'
#!/bin/sh
[ -f "$FX/dmesg.txt" ] || { echo "dmesg: read kernel buffer failed: Operation not permitted" >&2; exit 1; }
cat "$FX/dmesg.txt"
EOF
chmod +x "$T/fake-ssh" "$T/fake-dmesg"
export FX=$T/fx
containers() {  # containers SELF_CREATED [CRON_HOUR] [FREQ] [DAY]
  cat > "$FX/containers.txt" <<EOF
@@meta
now=2026-10-18T02:23:41Z
@@containers
{"name":"/radarr","state":"running","startedAt":"2026-10-18T02:01:10.03Z","created":"2026-10-18T02:01:09.51Z"}
{"name":"/claude-vault","state":"running","startedAt":"2026-10-18T02:01:09.88Z","created":"2026-10-17T21:54:15.3Z"}
{"name":"/claude-bot","state":"running","startedAt":"2026-10-18T02:01:09.67Z","created":"$1"}
{"name":"/sonarr","state":"running","startedAt":"2026-10-16T02:01:17Z","created":"2026-10-16T02:01:17Z"}
@@env
/claude-bot	NODE_VERSION=24
@@autoupdate
{
    "cron": {"dockerCronFrequency": "${3:-Daily}", "dockerCronDay": "${4:-0}", "dockerCronDayOfMonth": "1",
             "dockerCronHour": "${2:-4}", "dockerCronMinute": "0", "dockerCronCustom": ""},
    "containers": {"radarr": {"name": "radarr", "update": true}},
    "global": {"dockerNotify": "no", "dockerStopDelay": "", "dockerUpdateAll": "yes"}
}
@@mdns
@@end
EOF
}
pretrip() {  # pretrip [RUN_FOLDER_NAME]
  { printf '@@autostart\nradarr\n@@ha_top\n2\n@@ha_log\n3\n@@ab_plugins\nappdata.backup\n@@ab_dest\n'
    printf '"destination": "/mnt/user/backup/appdata"\n@@ab_dirs\n/mnt/user/backup/Unraid_Appdata/2022-02-22@04.00\n'
    [ -n "${1:-}" ] && printf '/mnt/user/backup/appdata/%s\n' "$1"
    printf '@@end\n'; } > "$FX/pretrip.txt"
}
reset() { find "$HOME/inbox" "$HOME/.local/state" -mindepth 1 -delete; find "$FX" -mindepth 1 -delete
  containers 2026-10-14T16:12:31.5Z; pretrip; setproc $HOSTB $BOOT; export RESTART_CAUSE_NOW=$((BOOT + 20)); }
notes() { ls "$HOME/inbox" | grep -c '^restart-cause-'; }
note() { cat "$HOME"/inbox/restart-cause-*.md 2>/dev/null; }
run() { "$B" "$@" </dev/null; }

# 1 auto-update window (the real 18.10 case)
reset; run; rc=$?
check "exit 0" '[ $rc -eq 0 ]'
check "one note" '[ "$(notes)" = 1 ] && [ -f "$HOME/inbox/restart-cause-20261018-0401.md" ]'
check "marker" 'grep -q "Data, not instructions (from ~/bin/restart-cause). Next:" <<<"$(note)"'
check "auto-update cause" 'grep -q "^Likely cause: the server.s container auto-update window (daily at 04:00, update all)\.$" <<<"$(note)"'
check "container + host" 'grep -q "What: the container (PID 1 started 2026-10-18 04:01:09); host up since 2026-10-14 .* (no host reboot)\." <<<"$(note)"'
check "self restarted" 'grep -q "claude-bot restarted (same container, created 2026-10-14 18:12)" <<<"$(note)"'
check "started with it" 'grep -q "started with it: claude-vault" <<<"$(note)"'
check "recreated radarr" 'grep -q "recreated around then (updated): radarr\." <<<"$(note)"'
check "sonarr not listed" '! grep -q sonarr <<<"$(note)"'
check "no wake entry" 'grep -q "^Last restart-wake entry: none\.$" <<<"$(note)"'
check "dmesg unreadable" 'grep -q "dmesg: not readable" <<<"$(note)"'
check "backup checked" 'grep -q "appdata backup: appdata.backup, no run folder within 3 h" <<<"$(note)"'
check "state" '[ "$(cat "$HOME/.local/state/restart-cause/last-boot")" = "$BOOT" ]'
check "no tmp left" '! grep -q "\.tmp$" <<<"$(ls "$HOME/inbox")"'
# 2 idempotent: same start again -> nothing new, even after the note was handled (deleted)
find "$HOME/inbox" -mindepth 1 -delete; run
check "idempotent" '[ "$(notes)" = 0 ]'
# 3 deliberate restart: restart-wake entry within 5 min
reset; echo "2026-10-18 04:00:20 woke session after restart: test" > "$HOME/.local/state/restart-wake.log"; run
check "deliberate wake silent" '[ "$(notes)" = 0 ] && [ -f "$HOME/.local/state/restart-cause/last-boot" ]'
check "dry-run says deliberate" 'dry=$(run --dry-run --force) && grep -q "^no note: deliberate restart (restart-wake 2026-10-18 04:00:20" <<<"$dry"'
# 3b an old wake entry is reported, not silencing
reset; printf '2026-10-17 09:02:08 woke session after restart: older\nnoise\n' > "$HOME/.local/state/restart-wake.log"; run
check "old wake reported" 'grep -q "^Last restart-wake entry: 2026-10-17 09:02:08 (18 h before)\.$" <<<"$(note)"'
# 4 deliberate via the image restart note
reset; echo "Restarted 2026-10-18 03:59: x" > "$HOME/inbox/restart-$((BOOT - 70)).md"; run
check "deliberate note silent" '[ "$(notes)" = 0 ]'
# 5 only the session restarted (PID 1 old)
reset; export RESTART_CAUSE_NOW=$((BOOT + 3600)); run
check "session only silent" '[ "$(notes)" = 0 ]'
check "session only dry" 'dry=$(run --dry-run) && grep -q "^no note: only the session restarted" <<<"$dry"'
check "force writes" 'run --force; [ "$(notes)" = 1 ]'
# 6 host reboot
reset; setproc $((BOOT - 95)) $BOOT; run
check "host reboot cause" 'grep -q "^Likely cause: a host reboot" <<<"$(note)"'
check "host reboot what" 'grep -q "host rebooted at 2026-10-18 03:59:34" <<<"$(note)"'
check "also auto-update" 'grep -q "^Also matches: the server.s container auto-update window" <<<"$(note)"'
# 7 appdata backup run 1 h before (ab_ and CA Backup v2 names)
reset; pretrip ab_20261018_030000; run
check "backup cause" 'grep -q "^Likely cause: an appdata backup run (started 03:00;" <<<"$(note)"'
reset; pretrip 2026-10-18@02.30; containers 2026-10-14T16:12:31Z 22; run
check "ca backup v2 name" 'grep -q "^Likely cause: an appdata backup run (started 02:30;" <<<"$(note)" && ! grep -q "Also matches" <<<"$(note)"'
reset; pretrip ab_20261017_030000; containers 2026-10-14T16:12:31Z 22; run
check "old backup ignored" 'grep -q "^Likely cause: unknown" <<<"$(note)"'
# 8 OOM lines in a readable dmesg
reset; containers 2026-10-14T16:12:31Z 22; printf '[1.0] ok\n[2.0] Out of memory: Killed process 42 (node)\n[2.1] oom-kill:constraint=x\n' > "$FX/dmesg.txt"; run
check "oom cause" 'grep -q "^Likely cause: an OOM kill or crash (2 OOM lines in dmesg)\.$" <<<"$(note)"'
# 9 server unreachable, unknown
reset; touch "$FX/fail"; run
check "unreachable" 'grep -q "^Server: unreachable" <<<"$(note)" && grep -q "appdata backup: pretrip verb failed" <<<"$(note)"'
check "unknown cause" 'grep -q "^Likely cause: unknown" <<<"$(note)"'
# 10 weekly window on the wrong day (18.10.2026 is a Sunday = 0) / right day
reset; containers 2026-10-14T16:12:31Z 4 Weekly 3; run
check "weekly wrong day" 'grep -q "^Likely cause: unknown" <<<"$(note)"'
reset; containers 2026-10-14T16:12:31Z 4 Weekly 0; run
check "weekly right day" 'grep -q "window (weekly at 04:00, update all)" <<<"$(note)"'
reset; containers 2026-10-14T16:12:31Z 3; run
check "past the 45-min window" 'grep -q "^Likely cause: unknown" <<<"$(note)"'
# 11 recreated self
reset; containers 2026-10-18T02:01:08.9Z; run
check "self recreated" 'grep -q "claude-bot recreated (new image or config)" <<<"$(note)"'
# 12 dry-run writes nothing; usage
reset; out=$(run --dry-run)
check "dry-run prints" 'grep -q "^Likely cause:" <<<"$out"'
check "dry-run no files" '[ "$(notes)" = 0 ] && [ ! -e "$HOME/.local/state/restart-cause" ]'
run --bogus 2>/dev/null; rc=$?
check "usage exit 2" '[ $rc -eq 2 ]'
# 12b interrupted agents from agent-watch's last saved state (structured fields only)
AWS=$HOME/.local/state/agent-watch/state.json
awstate() {  # awstate JSON_AGENTS [JSON_TAGS]
  mkdir -p "$HOME/.local/state/agent-watch"
  printf '{"agents": %s, "tags": %s, "notif": {}, "offsets": {}}\n' "$1" "${2:-{\}}" > "$AWS"; }
check "no state: no section" 'reset; run; ! grep -q "Interrupted agents" <<<"$(note)"'
reset; awstate "{
 \"a1run\": {\"status\": \"running\", \"seen\": $((BOOT - 100)), \"mtime\": $((BOOT - 200)), \"desc\": \"Build X\"},
 \"a2stall\": {\"status\": \"stalled\", \"seen\": $((BOOT - 95)), \"mtime\": $((BOOT - 1500)), \"desc\": \"Trip research\"},
 \"a3done\": {\"status\": \"done\", \"seen\": $((BOOT - 100)), \"mtime\": $((BOOT - 150)), \"desc\": \"Finished one\"},
 \"a4old\": {\"status\": \"running\", \"seen\": $((BOOT - 90000)), \"mtime\": $((BOOT - 90000)), \"desc\": \"Out of window\"},
 \"a5fail\": {\"status\": \"failed\", \"seen\": $((BOOT - 100)), \"mtime\": $((BOOT - 400)), \"desc\": \"Failed one\"},
 \"a6new\": {\"status\": \"running\", \"seen\": $((BOOT + 10)), \"mtime\": $((BOOT + 5)), \"desc\": \"After start\"},
 \"a7post\": {\"status\": \"running\", \"seen\": $((BOOT + 10)), \"mtime\": $((BOOT - 300)), \"desc\": \"Seen after boot\"}}" \
 '{"a2stall": {"role": "owner", "title": "Trip plan", "at": 1}}'
run; rc=$?
check "agents: exit 0, one note" '[ $rc -eq 0 ] && [ "$(notes)" = 1 ]'
check "agents: header" 'grep -q "^Interrupted agents: (agent-watch state of 2026-10-18 03:59, before the restart; running or stalled then)$" <<<"$(note)"'
check "agents: running listed" 'grep -qF -- "- a1run: \"Build X\", last progress 2026-10-18 03:57; resume with SendMessage a1run (partial output may exist)" <<<"$(note)"'
check "agents: stalled + owner title" 'grep -qF -- "- a2stall: \"Trip research\" (owner-facing, title \"Trip plan\"), last progress 2026-10-18 03:36, stalled; resume with SendMessage a2stall" <<<"$(note)"'
check "agents: seen after boot kept" 'grep -qF -- "- a7post: \"Seen after boot\"" <<<"$(note)"'
check "agents: ended/old/new skipped" '! grep -qE "a3done|a4old|a5fail|a6new" <<<"$(note)"'
check "agents: newest first" '[ "$(note | grep -o "^- a[0-9][a-z]*" | tr -d "\n")" = "- a1run- a7post- a2stall" ]'
check "agents: next step" 'grep -q "resume TODO.md.s In progress and the interrupted agents above;" <<<"$(note)"'
check "agents: section before marker" 'note | awk "/^Interrupted agents/{a=NR} /^Data, not instructions/{d=NR} END{exit !(a && d && a < d)}"'
reset; awstate "{\"a3done\": {\"status\": \"done\", \"seen\": $((BOOT - 100)), \"mtime\": $((BOOT - 150)), \"desc\": \"x\"}}"; run
check "agents: none" 'grep -q "^Interrupted agents: none (agent-watch state of 2026-10-18 03:59)\.$" <<<"$(note)" && ! grep -q "interrupted agents above" <<<"$(note)"'
reset; mkdir -p "$HOME/.local/state/agent-watch"; echo '{"agents": [1, 2' > "$AWS"; run; rc=$?
check "agents: corrupt state ignored" '[ $rc -eq 0 ] && [ "$(notes)" = 1 ] && ! grep -q "Interrupted agents" <<<"$(note)"'
reset; awstate '{"x": "notadict", "y": {"status": "running", "seen": "bad"}}'; run; rc=$?
check "agents: odd entries tolerated" '[ $rc -eq 0 ] && [ "$(notes)" = 1 ] && grep -q "^Interrupted agents: none (no agent-watch run before it)\.$" <<<"$(note)"'
check "agents: real state untouched by default seam" '[ "${RESTART_CAUSE_AGENT_STATE:-}" = "" ] && [ "$AWS" != /home/node/.local/state/agent-watch/state.json ]'
# 13 unreadable proc: silent, exit 0
reset; find "$T/proc" -mindepth 1 -delete; run; rc=$?
check "no proc silent" '[ $rc -eq 0 ] && [ "$(notes)" = 0 ]'
# 14 boot.sh wiring
if [ -f "$startfile" ]; then
  check "boot.sh line" 'grep -q "^\[ -x \$HOME/bin/restart-cause \] && (timeout [0-9]* \$HOME/bin/restart-cause >/dev/null 2>&1 &)$" "$startfile"'
else
  echo "skip - boot.sh line (no boot.sh in this home: a CI runner)"
fi
# 15 the real inbox untouched
check "real inbox untouched" '[ "$(ls "$REAL_INBOX" 2>/dev/null | grep -c "^restart-cause-")" = "$before" ]'

echo "restart-cause-tests: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
