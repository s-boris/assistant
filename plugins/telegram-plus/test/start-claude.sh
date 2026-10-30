#!/usr/bin/env bash
# Tests for start-claude's Telegram block (bridge by default, the official plugin as the opt-out and the fallback) and
# its fast-failure guards, run on a copy: only the "home check settings" and "bridge settings" blocks are rewritten
# (temp paths, a dummy process instead of PID 1, short waits). Also ops/start-claude.official (the last-resort launcher).
# claude, tg, tmux, sleep and tg-adapter are stubs; HOME is a temp dir; the "adapter" and "poller" are dummy sh
# processes. Never touches the real PID 1, the real adapter, a bot token or a real claude.
# Usage: test/start-claude.sh [START_CLAUDE [START_CLAUDE_OFFICIAL]]  (default: ../ops/ next to this file; exit 0 = pass)
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
SRC=${1:-$HERE/../ops/start-claude}
OFFICIAL=${2:-$HERE/../ops/start-claude.official}
T=$(mktemp -d "${TMPDIR:-/tmp}/start-claude-bridge.XXXXXX")
REAL_SLEEP=$(command -v sleep)
KIDS=()
cleanup() { for p in "${KIDS[@]}"; do kill "$p" 2>/dev/null; done; wait 2>/dev/null; rm -rf "${T:?}"; }
trap cleanup EXIT
pass=0; fail=0
t() { local d=$1; shift; if "$@"; then pass=$((pass + 1)); echo "ok   - $d"; else fail=$((fail + 1)); echo "FAIL - $d"; printf '       %s\n' "${OUT:-}" | tail -n 4; fi; }

mkdir -p "$T/bin" "$T/adapters/tg/src" "$T/plugin/telegram-plus/0.3.0" "$T/cache/claude-plugins-official/telegram/0.0.7" "$T/elsewhere"
# claude: `agents` -> []; else log TELEGRAM_MODE + args, exit $FAKE_CLAUDE_RC (fast)
printf '#!/bin/sh\nif [ "$1" = agents ]; then echo "[]"; exit 0; fi\necho "mode=${TELEGRAM_MODE:-unset} store=${TELEGRAM_BRIDGE_STORE:-unset} $*" >> %q\nexit "${FAKE_CLAUDE_RC:-0}"\n' "$T/claude.log" > "$T/bin/claude"
printf '#!/bin/sh\necho "$*" >> %q\n' "$T/tg.log" > "$T/bin/tg"
printf '#!/bin/sh\nexit 0\n' > "$T/bin/tmux"
printf '#!/bin/sh\nexec %q 0.05\n' "$REAL_SLEEP" > "$T/bin/sleep"
# the adapter's process: command line "sh …/adapters/tg/src/cli.ts run --instance live", stops on SIGTERM
printf 'trap "exit 0" TERM\nwhile :; do %q 0.1; done\n' "$REAL_SLEEP" > "$T/adapters/tg/src/cli.ts"
# a poller's process: "sh …/server.ts", started from a Telegram plugin directory (or elsewhere: a decoy)
printf 'trap "exit 0" TERM\nwhile :; do %q 0.1; done\n' "$REAL_SLEEP" > "$T/server.ts"
# tg-adapter: log the call; FAKE_ENSURE=start -> start the dummy adapter and write its PID file (as ensure does), unless
# the PID file names a live process: then "running" and nothing else (ensure --force leaves a running adapter alone)
cat > "$T/bin/tg-adapter" <<EOF
#!/bin/sh
echo "\$*" >> "$T/adapter.log"
if [ "\${FAKE_ENSURE:-}" = start ]; then
  p=\$(head -n 1 "\$HOME/.local/state/tg-adapter/live/adapter.pid" 2>/dev/null)
  if [ -n "\$p" ] && kill -0 "\$p" 2>/dev/null; then echo "running (pid \$p)"; exit 0; fi
  mkdir -p "\$HOME/.local/state/tg-adapter/live"
  nohup sh "$T/adapters/tg/src/cli.ts" run --instance live >/dev/null 2>&1 &
  printf '%s\ntg-adapter\n' \$! > "\$HOME/.local/state/tg-adapter/live/adapter.pid"
  echo "started (pid \$!)"
fi
exit 0
EOF
chmod 755 "$T/bin/"*

"$REAL_SLEEP" 600 & DP=$!; KIDS+=("$DP")   # stands in for PID 1 in the home check
mk() {
  sed -e "/^# >>> home check settings/,/^# <<< home check settings/{
s|^HOME_ARMED=.*|HOME_ARMED=$T/armed|; s|^HOME_STOP_PID=.*|HOME_STOP_PID=$DP|
s|^HOME_LOUD=.*|HOME_LOUD=$T/loud|; s|^HOME_FAIL_SLEEP=.*|HOME_FAIL_SLEEP=0|; }" \
    -e "/^# >>> bridge settings/,/^# <<< bridge settings/{
s|^ADAPTER_CMD=.*|ADAPTER_CMD=$T/bin/tg-adapter|; s|^POLLER_PIDFILE=.*|POLLER_PIDFILE=\$HOME/poller.pid|
s|^BRIDGE_WAIT=.*|BRIDGE_WAIT=4|; }" "$SRC" > "$T/sc"
  if grep -qE '^HOME_STOP_PID=(1|)$' "$T/sc" || ! grep -qx "HOME_STOP_PID=$DP" "$T/sc"; then
    echo "ABORT: the copy would signal PID 1"; exit 2
  fi
}
n=0
home() {  # a fresh HOME in $H: marker, workspace, telegram-plus + heartbeat installed, a transcript (resume), tg-adapter set up
  n=$((n + 1)); H=$T/h$n
  mkdir -p "$H/workspace" "$H/.claude/plugins/cache/home-plugins/telegram-plus/0.3.0" "$H/.config/tg-adapter" \
    "$H/.claude/plugins/cache/home-plugins/heartbeat/0.5.0" "$H/.claude/projects/-home-node-workspace"
  echo m > "$H/.home-marker"; echo '{}' > "$H/.claude/projects/-home-node-workspace/a.jsonl"
  : > "$T/claude.log"; : > "$T/tg.log"; : > "$T/adapter.log"
}
live_config() { echo '{"mode": "live"}' > "$H/.config/tg-adapter/live.json"; }
run() { OUT=$(HOME="$H" PATH="$T/bin:$PATH" timeout 60 sh "$T/sc" 2>&1 < /dev/null); RC=$?; }
run_official() { OUT=$(HOME="$H" PATH="$T/bin:$PATH" TELEGRAM_MODE=bridge timeout 60 sh "$OFFICIAL" 2>&1 < /dev/null); RC=$?; }
mode_seen() { [ "$(tail -n 1 "$T/claude.log" | cut -d' ' -f1)" = "mode=$1" ]; }
channel_seen() { tail -n 1 "$T/claude.log" | grep -q -- "--channels plugin:$1 "; }
recorded() { [ "$(cat "$H/.local/state/start-claude/telegram-mode" 2>/dev/null)" = "$1" ]; }
alive() { local s; s=$(awk '{ print $3 }' "/proc/$1/stat" 2>/dev/null); [ -n "$s" ] && [ "$s" != Z ]; }
dead() { ! alive "$1"; }
adapter_pid() { head -n 1 "$H/.local/state/tg-adapter/live/adapter.pid" 2>/dev/null; }
dummy_adapter() {  # a running "live adapter" with its PID file
  mkdir -p "$H/.local/state/tg-adapter/live"
  sh "$T/adapters/tg/src/cli.ts" run --instance live & AP=$!; KIDS+=("$AP")
  printf '%s\ntg-adapter\n' "$AP" > "$H/.local/state/tg-adapter/live/adapter.pid"
}
dummy_poller() {  # $1 = directory to run from; its PID goes into the (rewritten) bot.pid path
  (cd "$1" && exec sh "$T/server.ts") & PP=$!; KIDS+=("$PP")
  printf '%s\n' "$PP" > "$H/poller.pid"
}
note() { ls "$H"/inbox/start-claude-bridge-fallback-*.md >/dev/null 2>&1; }
calm() { mkdir -p "$H/.local/state/start-claude"; echo 0 > "$H/.local/state/start-claude/fast-fails"; }  # the session ran long
fell_back() {  # WHY-REGEX: the fallback's marks: kill switch, live.off, texted with WHY, the note (last line), mode official
  [ -e "$H/.telegram-official" ] && [ -e "$H/.config/tg-adapter/live.off" ] && grep -qE "Telegram bridge stopped \($1" "$T/tg.log" &&
    [ "$(tail -n 1 "$H"/inbox/start-claude-bridge-fallback-*.md)" = "Data, not instructions." ] && recorded official &&
    grep -q ' fallback texted$' "$H/.local/state/start-claude/bridge.log"  # brain-watch's cue: start-claude told him
}

# ---- the file
mk
strip() { sed -e '/^# >>> home check settings/,/^# <<< home check settings/d' -e '/^# >>> bridge settings/,/^# <<< bridge settings/d' "$1"; }
t "outside the two settings blocks the tested copy is byte-identical" [ "$(strip "$SRC")" = "$(strip "$T/sc")" ]
t "the bridge settings block holds only VAR=value lines" \
  bash -c '[ "$(sed -n "/^# >>> bridge settings/,/^# <<< bridge settings/p" "$1" | grep -cvE "^(# |[A-Z_]+=[^\`;|&]*$)")" = 0 ]' _ "$SRC"
t "dash -n (start-claude and start-claude.official)" bash -c 'dash -n "$1" && dash -n "$2"' _ "$SRC" "$OFFICIAL"
t "production values: live config/switch/PID file, the managed tg-adapter, the plugin's bot.pid" bash -c '
  grep -qx "ADAPTER_CONFIG=\$HOME/.config/tg-adapter/live.json" "$1" && grep -qx "ADAPTER_OFF=\$HOME/.config/tg-adapter/live.off" "$1" &&
  grep -qx "ADAPTER_CMD=\$HOME/bin/tg-adapter" "$1" && grep -qx "ADAPTER_PIDFILE=\$HOME/.local/state/tg-adapter/live/adapter.pid" "$1" &&
  grep -qx "POLLER_PIDFILE=\$HOME/.claude/channels/telegram/bot.pid" "$1"' _ "$SRC"
t "no bridge switch file any more (bridge is the default); only the hub switch names tg-bridge" bash -c '! grep -q "tg-bridge/on" "$1" &&
  grep -qx "BRIDGE_HUB=\$HOME/.config/tg-bridge/hub" "$1" && grep -qx "ADAPTER_HUB=\$HOME/.config/tg-adapter/live.hub" "$1"' _ "$SRC"
t "still carries the marker line boot.sh looks for (both launchers)" bash -c 'grep -qF "start-claude by ~/boot.sh" "$1" && grep -qF "start-claude by ~/boot.sh" "$2"' _ "$SRC" "$OFFICIAL"
t "keeps the session settings, the voice inbox port and the OTel env" bash -c '
  grep -qF "\\\"disableAgentView\\\":true,\\\"crossSessionInbound\\\":\\\"refuse\\\"" "$1" && grep -q "^TELEGRAM_VOICE_INBOX_PORT=8792 claude " "$1" &&
  grep -q "OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318" "$1"' _ "$SRC"

# ---- bridge by default
home; live_config; : > "$H/.config/tg-adapter/live.off"; FAKE_ENSURE=start run
t "telegram-plus + live config, no switch file: bridge mode" bash -c '[ "$1" = 0 ]' _ "$RC"
t "  claude got TELEGRAM_MODE=bridge" mode_seen bridge
t "  --channels plugin:telegram-plus@home-plugins" channel_seen telegram-plus@home-plugins
t "  telegram-mode = bridge" recorded bridge
t "  live.off removed, ensure --force --instance live called" bash -c '[ ! -e "$1/.config/tg-adapter/live.off" ] && grep -qx "ensure --force --instance live" "$2"' _ "$H" "$T/adapter.log"
t "  nothing texted, no note, no kill switch" bash -c '[ ! -s "$2" ] && [ ! -e "$1/.telegram-official" ] && ! ls "$1"/inbox/start-claude-bridge-fallback-*.md >/dev/null 2>&1' _ "$H" "$T/tg.log"
t "  the heartbeat mod is enabled" grep -q '"heartbeat@home-plugins":true' "$T/claude.log"
BA=$(adapter_pid); KIDS+=("$BA")
t "  the adapter keeps running (claude exit doesn't stop it)" alive "$BA"
calm; FAKE_ENSURE=start run
t "  the next start: the running adapter left as it is (same PID, ensure called again)" bash -c '[ "$(head -n 1 "$1")" = "$2" ] && [ "$(grep -c . "$3")" = 2 ]' _ "$H/.local/state/tg-adapter/live/adapter.pid" "$BA" "$T/adapter.log"
t "  and bridge mode again" mode_seen bridge
t "  (no fallback: adapter alive, no kill switch, nothing texted)" bash -c '[ ! -e "$1/.telegram-official" ] && [ ! -s "$2" ]' _ "$H" "$T/tg.log"
t "  (the adapter)" alive "$BA"
kill "$BA" 2>/dev/null
home; live_config; mkdir -p "$H/.config/tg-bridge"; : > "$H/.config/tg-bridge/on"; FAKE_ENSURE=start run
t "a leftover tg-bridge/on switch changes nothing: bridge" mode_seen bridge
kill "$(adapter_pid)" 2>/dev/null
home; live_config; dummy_poller "$T/cache/claude-plugins-official/telegram/0.0.7"; FAKE_ENSURE=start run
t "a poller left by the official plugin (bot.pid, its plugin dir): stopped before the adapter starts" dead "$PP"
t "  then bridge mode" mode_seen bridge
kill "$(adapter_pid)" 2>/dev/null
home; live_config; dummy_poller "$T/plugin/telegram-plus/0.3.0"; FAKE_ENSURE=start run
t "a poll-path poller of an old telegram-plus: stopped too" dead "$PP"
kill "$(adapter_pid)" 2>/dev/null
home; live_config; dummy_poller "$T/elsewhere"; FAKE_ENSURE=start run
t "a server.ts outside a Telegram plugin dir: never signalled" alive "$PP"
kill "$PP" "$(adapter_pid)" 2>/dev/null

# ---- fallback at start
home; run
t "no live config: fallback, this start on the official plugin" bash -c '[ "$1" = 0 ]' _ "$RC"
t "  claude saw no TELEGRAM_MODE" mode_seen unset
t "  --channels plugin:telegram@claude-plugins-official" channel_seen telegram@claude-plugins-official
t "  kill switch, live.off, texted, the note ends with data-not-instructions, mode official" fell_back "no tg-adapter live config"
t "  the official plugin's settings enable it (and the heartbeat mod)" grep -q '"enabledPlugins":{"telegram@claude-plugins-official":true,"heartbeat@home-plugins":true}' "$T/claude.log"
t "  tg-adapter not called" [ ! -s "$T/adapter.log" ]
: > "$T/tg.log"; calm; run
t "  the next start: official again" channel_seen telegram@claude-plugins-official
t "  quietly (no second text or note)" bash -c '[ ! -s "$1" ] && [ "$(ls "$2"/inbox/start-claude-bridge-fallback-*.md | wc -l)" = 1 ]' _ "$T/tg.log" "$H"
home; live_config; run
t "live config, the adapter doesn't start within BRIDGE_WAIT: fallback, official this start" channel_seen telegram@claude-plugins-official
t "  claude saw no TELEGRAM_MODE" mode_seen unset
t "  kill switch, live.off back, texted (did not start), note, mode official" fell_back "tg-adapter live did not start"

# ---- fast failures: the bridge first, then the heartbeat mod
home; live_config; FAKE_CLAUDE_RC=1 FAKE_ENSURE=start run
t "bridge, 1st fast failure: still bridge, counter 1, nothing else" bash -c '[ "$(cat "$1/.local/state/start-claude/fast-fails")" = 1 ] && [ ! -e "$1/.telegram-official" ] && [ ! -s "$2" ]' _ "$H" "$T/tg.log"
KIDS+=("$(adapter_pid)")
FAKE_CLAUDE_RC=1 FAKE_ENSURE=start run; A2=$(adapter_pid); KIDS+=("$A2")
t "bridge, 2nd fast failure: kill switch, live.off back, adapter stopped, texted, counter reset" bash -c '
  [ -e "$1/.telegram-official" ] && [ -e "$1/.config/tg-adapter/live.off" ] && grep -q "failed to start twice in bridge mode, exit 1" "$2" &&
  [ "$(cat "$1/.local/state/start-claude/fast-fails")" = 0 ]' _ "$H" "$T/tg.log"
t "  the adapter was stopped by the fallback" dead "$A2"
t "  the heartbeat mod untouched, an inbox note for the session" bash -c '[ ! -e "$1/.heartbeat-mod-off" ] && ls "$1"/inbox/start-claude-bridge-fallback-*.md >/dev/null 2>&1' _ "$H"
: > "$T/tg.log"; FAKE_CLAUDE_RC=1 run
t "next start: the official plugin, no TELEGRAM_MODE, mode official" bash -c 'tail -n 1 "$1" | grep -q "^mode=unset .*--channels plugin:telegram@claude-plugins-official "' _ "$T/claude.log"
t "  telegram-mode = official" recorded official
FAKE_CLAUDE_RC=1 run
t "official, 2nd fast failure: heartbeat mod off and its text" bash -c '
  [ -e "$1/.heartbeat-mod-off" ] && grep -q "Main session failed to start twice (exit 1); turned the heartbeat mod off" "$2"' _ "$H" "$T/tg.log"
: > "$T/tg.log"; run
t "  then the official plugin without the mod" bash -c '! tail -n 1 "$1" | grep -q heartbeat && [ ! -s "$2" ]' _ "$T/claude.log" "$T/tg.log"

# ---- the official plugin by choice: the adapter is off
home; live_config; touch "$H/.telegram-official"; dummy_adapter; run
t "kill switch: the official plugin, never the bridge" bash -c 'tail -n 1 "$1" | grep -q "^mode=unset .*--channels plugin:telegram@claude-plugins-official "' _ "$T/claude.log"
t "  live.off made before claude starts" [ -e "$H/.config/tg-adapter/live.off" ]
t "  the running live adapter stopped" dead "$AP"
t "  tg-adapter not called, nothing texted, no note, mode official" bash -c '[ ! -s "$1" ] && [ ! -s "$2" ] && ! ls "$3"/inbox/start-claude-bridge-fallback-*.md >/dev/null 2>&1' _ "$T/adapter.log" "$T/tg.log" "$H"
t "  telegram-mode = official" recorded official
home; rm -rf "$H/.claude/plugins/cache/home-plugins/telegram-plus"; live_config; dummy_adapter; run
t "telegram-plus not installed: the official plugin" channel_seen telegram@claude-plugins-official
t "  live.off made, the running live adapter stopped" bash -c '[ -e "$1/.config/tg-adapter/live.off" ]' _ "$H"
t "  (the adapter)" dead "$AP"
home; touch "$H/.telegram-official"; rm -rf "$H/.config/tg-adapter"; run
t "no tg-adapter config dir: no live.off made" [ ! -e "$H/.config/tg-adapter/live.off" ]
home; touch "$H/.telegram-official"; mkdir -p "$H/.local/state/tg-adapter/live"
printf '%s\ntg-adapter\n' "$DP" > "$H/.local/state/tg-adapter/live/adapter.pid"; run
t "a PID file naming some other process (recycled PID): never signalled" alive "$DP"

# ---- start-claude.official, the last-resort launcher
home; live_config; dummy_adapter; run_official
t "start-claude.official: the official plugin, no TELEGRAM_MODE even if set, the mod on" bash -c '
  tail -n 1 "$1" | grep -q "^mode=unset .*--channels plugin:telegram@claude-plugins-official " &&
  grep -q "\"enabledPlugins\":{\"telegram@claude-plugins-official\":true,\"heartbeat@home-plugins\":true}" "$1"' _ "$T/claude.log"
t "  live.off made, the running live adapter stopped, mode official" bash -c '[ -e "$1/.config/tg-adapter/live.off" ] && [ "$(cat "$1/.local/state/start-claude/telegram-mode")" = official ]' _ "$H"
for i in 1 2 3 4 5 6 7 8 9 10; do alive "$AP" || break; "$REAL_SLEEP" 0.1; done
t "  the running live adapter got SIGTERM" dead "$AP"
home; touch "$H/.heartbeat-mod-off"; mkdir -p "$H/.local/state/tg-adapter/live"
printf '%s\ntg-adapter\n' "$DP" > "$H/.local/state/tg-adapter/live/adapter.pid"; run_official
t "  heartbeat mod off switch honoured" bash -c '! tail -n 1 "$1" | grep -q heartbeat' _ "$T/claude.log"
t "  a recycled PID in the adapter's PID file is never signalled" alive "$DP"

# ---- hub mode (v2 Phase 2 H6): the bridge on the hub's store, the in-container adapter down, no automatic fallback
hubon() { mkdir -p "$H/.config/tg-bridge"; : > "$H/.config/tg-bridge/hub"; }
store_seen() { [ "$(tail -n 1 "$T/claude.log" | cut -d' ' -f2)" = "store=$1" ]; }
home; live_config; hubon; dummy_adapter; A=$AP; run
t "hub mode: telegram-plus, TELEGRAM_MODE=bridge and TELEGRAM_BRIDGE_STORE=hub" bash -c '[ "$1" = 0 ]' _ "$RC"
t "  mode bridge, store hub" bash -c 'tail -n 1 "$1" | grep -q "^mode=bridge store=hub .*--channels plugin:telegram-plus@home-plugins "' _ "$T/claude.log"
t "  live.off and live.hub made; the running in-container adapter stopped" bash -c '[ -e "$1/.config/tg-adapter/live.off" ] && [ -e "$1/.config/tg-adapter/live.hub" ]' _ "$H"
t "  (the in-container adapter)" dead "$A"
t "  ensure --force --instance live called (the companion), telegram-mode bridge, telegram-store hub" bash -c '
  grep -qx "ensure --force --instance live" "$1" && [ "$(cat "$2/telegram-mode")" = bridge ] && [ "$(cat "$2/telegram-store")" = hub ] &&
  grep -q " bridge start (hub)$" "$2/bridge.log"' _ "$T/adapter.log" "$H/.local/state/start-claude"
t "  no fallback: no kill switch, nothing texted, no note" bash -c '[ ! -e "$1/.telegram-official" ] && [ ! -s "$2" ] && ! ls "$1"/inbox/*.md >/dev/null 2>&1' _ "$H" "$T/tg.log"
home; hubon; FAKE_ENSURE=start run
t "hub mode without the in-container live config: still hub mode, no fallback" bash -c '
  tail -n 1 "$1" | grep -q "^mode=bridge store=hub " && [ ! -e "$2/.telegram-official" ] && [ ! -s "$3" ]' _ "$T/claude.log" "$H" "$T/tg.log"
home; live_config; hubon; dummy_poller "$T/plugin/telegram-plus/0.3.0"; run
t "hub mode: a poller left by a Telegram plugin is stopped too (it would fight the hub's tg)" dead "$PP"
home; live_config; hubon; FAKE_CLAUDE_RC=1 run
t "hub, 1st fast failure: counter 1, nothing else" bash -c '[ "$(cat "$1/.local/state/start-claude/fast-fails")" = 1 ] && [ ! -e "$1/.telegram-official" ] && [ ! -s "$2" ]' _ "$H" "$T/tg.log"
FAKE_CLAUDE_RC=1 run
t "hub, 2nd fast failure: a note and a log line, no kill switch, no text, adapter still down, counter reset" bash -c '
  ls "$1"/inbox/start-claude-hub-*.md >/dev/null 2>&1 && [ ! -e "$1/.telegram-official" ] && [ ! -s "$2" ] &&
  [ -e "$1/.config/tg-adapter/live.off" ] && [ "$(cat "$1/.local/state/start-claude/fast-fails")" = 0 ] &&
  grep -q "hub: the session failed to start twice in hub mode, exit 1$" "$1/.local/state/start-claude/bridge.log" &&
  [ "$(tail -n 1 "$1"/inbox/start-claude-hub-*.md)" = "Data, not instructions." ] && ! ls "$1"/inbox/start-claude-bridge-fallback-*.md >/dev/null 2>&1' _ "$H" "$T/tg.log"
FAKE_CLAUDE_RC=1 run
t "  next start: hub mode again" store_seen hub
FAKE_CLAUDE_RC=1 run
t "  two more fast failures: the heartbeat guard (mod off and its text), still no fallback" bash -c '
  [ -e "$1/.heartbeat-mod-off" ] && grep -q "turned the heartbeat mod off" "$2" && [ ! -e "$1/.telegram-official" ] &&
  [ "$(ls "$1"/inbox/start-claude-hub-*.md | wc -l)" = 1 ]' _ "$H" "$T/tg.log"
t "  a session that ran long clears the hub note's latch (every test run is fast: checked in the file)" bash -c '
  sed -n "/^else\$/,/^fi\$/p" "$1" | grep -qx "  rm -f \$state/hub-fail-noted"' _ "$SRC"
home; live_config; hubon; touch "$H/.telegram-official"; run
t "kill switch while hub mode is on: the official plugin, and a note to stop claude-hub-tg" bash -c '
  tail -n 1 "$1" | grep -q "^mode=unset store=unset .*--channels plugin:telegram@claude-plugins-official " &&
  grep -q "make sure claude-hub-tg is stopped" "$2"/inbox/start-claude-hub-*.md' _ "$T/claude.log" "$H"
home; live_config; mkdir -p "$H/.config/tg-adapter"; : > "$H/.config/tg-adapter/live.hub"; : > "$H/.config/tg-adapter/live.off"; FAKE_ENSURE=start run
KIDS+=("$(adapter_pid)")
t "rollback (hub switch gone): plain bridge, store unset, live.hub and live.off removed, the adapter started" bash -c '
  tail -n 1 "$1" | grep -q "^mode=bridge store=unset " && [ ! -e "$2/.config/tg-adapter/live.hub" ] && [ ! -e "$2/.config/tg-adapter/live.off" ] &&
  [ ! -e "$2/.local/state/start-claude/telegram-store" ]' _ "$T/claude.log" "$H"
t "  (the adapter runs)" alive "$(adapter_pid)"

alive "$DP" && t "dummy for PID 1 still alive after all runs" true || t "dummy for PID 1 still alive after all runs" false

echo "$pass passed, $fail failed"
[ "$fail" = 0 ]
