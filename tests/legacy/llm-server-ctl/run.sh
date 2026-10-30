#!/usr/bin/env bash
# Offline tests for `llm-server-ctl idle-check` (idle auto-stop of the local llama-server). Exit 0 = all pass.
# Never touches the real server: HOME is a temp dir (pid file, log and flag live under $HOME/.cache/llm-local),
# the "server" is a `sleep`, and LLM_LOCAL_PORT is an unused port; the busy case opens a real loopback TCP
# connection on that port. The real ~/.cache/llm-local/server.pid is checked untouched at the end.
# Default: the repo's scripts/ copy; LLM_SERVER_CTL_BIN=<path> tests another copy.
set -uo pipefail
C=${LLM_SERVER_CTL_BIN:-$(cd "$(dirname "$0")/../../../scripts" && pwd)/llm-server-ctl}
REAL_PIDF=/home/node/.cache/llm-local/server.pid
real_before=$(cat "$REAL_PIDF" 2>/dev/null; ls -l --time-style=+%s "$REAL_PIDF" 2>/dev/null)
T=$(mktemp -d "${TMPDIR:-/tmp}/llm-server-ctl-tests.XXXXXX")
# a free port per run: parallel CIs (lands overlap since 29.10) collided on a fixed 18179
LLM_LOCAL_PORT=$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
export HOME=$T/home LLM_LOCAL_PORT LLM_LOCAL_IDLE_MIN=30
unset LLM_LOCAL_CONF
DIR=$HOME/.cache/llm-local; PIDF=$DIR/server.pid; LOGF=$DIR/server.log; OFF=$DIR/disabled
pids=()
cleanup() { for p in "${pids[@]}"; do kill -9 "$p" 2>/dev/null; done; rm -rf "$T"; }
trap cleanup EXIT
mkdir -p "$DIR"
pass=0 fail=0
check() { local n=$1; shift; if "$@"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $n"; fi; }
alive() { kill -0 "$1" 2>/dev/null; }
dead() { ! kill -0 "$1" 2>/dev/null; }
fake_server() {   # $1 = minutes since the last log line, $2 = minutes since start
  sleep 300 & local p=$!; pids+=("$p"); echo "$p" > "$PIDF"; rm -f "$OFF"
  echo "slot print_timing ..." > "$LOGF"
  touch -d "-$1 min" "$LOGF"; touch -d "-$2 min" "$PIDF"; SP=$p
}

# 1. not running: no-op, exit 0
"$C" idle-check; check "no server: exit 0" [ $? = 0 ]
check "no server: no flag" [ ! -f "$OFF" ]

# 2. recent request (log 5 min old): keeps running
fake_server 5 120; "$C" idle-check
check "recent request keeps it" alive "$SP"; check "no flag when kept" [ ! -f "$OFF" ]

# 3. idle 45 min but started 10 min ago: keeps running (start counts as activity)
fake_server 45 10; "$C" idle-check
check "fresh start keeps it" alive "$SP"

# 4. idle 45 min, started 2 h ago: stopped, flag set, logged
fake_server 45 120; "$C" idle-check; sleep 0.3
check "idle 45 min: stopped" dead "$SP"; check "idle stop sets the flag" [ -f "$OFF" ]
check "idle stop logged" grep -q 'idle 45 min' "$DIR/idle.log"
check "pid file removed" [ ! -f "$PIDF" ]

# 5. LLM_LOCAL_IDLE_MIN honoured
fake_server 20 120; LLM_LOCAL_IDLE_MIN=15 "$C" idle-check; sleep 0.3
check "IDLE_MIN=15 stops a 20 min idle server" dead "$SP"

# 6. idle by the log, but a request is in flight (established connection on the port): keeps running
python3 -c '
import socket, sys, time
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", int(sys.argv[1]))); s.listen(1)
c = socket.create_connection(("127.0.0.1", int(sys.argv[1]))); a, _ = s.accept()
open(sys.argv[2], "w").close(); time.sleep(30)' "$LLM_LOCAL_PORT" "$T/conn.ready" & pids+=($!)
for _ in $(seq 300); do [ -f "$T/conn.ready" ] && break; sleep 0.1; done
fake_server 45 120; "$C" idle-check
check "open connection keeps it" alive "$SP"
kill "${pids[-2]}" 2>/dev/null

# 7. a listening-only socket (no connection) does not count as busy
python3 -c '
import socket, sys, time
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", int(sys.argv[1]))); s.listen(1); open(sys.argv[2], "w").close(); time.sleep(30)' \
  "$LLM_LOCAL_PORT" "$T/listen.ready" & pids+=($!)
for _ in $(seq 300); do [ -f "$T/listen.ready" ] && break; sleep 0.1; done
"$C" idle-check; sleep 0.3
check "listener only: stopped" dead "$SP"

# 8. usage line lists idle-check
u=$("$C" bogus 2>&1); check "usage mentions idle-check" grep -q idle-check <<<"$u"

real_after=$(cat "$REAL_PIDF" 2>/dev/null; ls -l --time-style=+%s "$REAL_PIDF" 2>/dev/null)
check "real pid file untouched" [ "$real_before" = "$real_after" ]
echo "llm-server-ctl tests: $pass passed, $fail failed"
[ "$fail" = 0 ]
