#!/usr/bin/env bash
# Offline tests for ~/bin/whisper-server-ctl (warm whisper.cpp server for ~/bin/transcribe). Exit 0 = all pass.
# Never touches the real server: HOME is a temp dir (the binary, models, pid file and log all live under $HOME),
# TRANSCRIBE_SERVER_PORT is an unused port, the whisper-server binary is a fake (a `sleep` that records its args),
# and curl (answers /health for the fake server only), sleep (fast) and nproc are PATH shims. The real
# ~/.cache/whisper-server/server.pid and its process are checked untouched at the end.
# Default: the repo's scripts/ copy; WHISPER_SERVER_CTL_BIN=<path> tests another copy.
set -uo pipefail
W=${WHISPER_SERVER_CTL_BIN:-$(cd "$(dirname "$0")/../../../scripts" && pwd)/whisper-server-ctl}
REAL_PIDF=/home/node/.cache/whisper-server/server.pid
T=$(mktemp -d "${TMPDIR:-/tmp}/whisper-server-ctl-tests.XXXXXX")
REAL_SLEEP=$(command -v sleep)
# a free port per run: parallel CIs and parallel legacy suites must not share a fixed one
TRANSCRIBE_SERVER_PORT=$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
export FAKE=$T/fake HOME=$T/home TRANSCRIBE_SERVER_PORT REAL_SLEEP
unset TRANSCRIBE_MODEL TRANSCRIBE_THREADS
DIR=$HOME/.cache/whisper-server; PIDF=$DIR/server.pid
SRV=$HOME/opt/whisper.cpp/build/bin/whisper-server
M=$HOME/opt/whisper-models
MODEL=$M/ggml-large-v3-turbo-q8_0.bin
VAD=$M/ggml-silero-v6.2.0.bin
cleanup() {
  local p
  [ -f "$FAKE/all.pids" ] && while read -r p; do kill -9 "$p" 2>/dev/null; done < "$FAKE/all.pids"
  rm -rf "$T"
}
trap cleanup EXIT
mkdir -p "$T/shims" "$FAKE" "$HOME"
real_state() { [ -f "$REAL_PIDF" ] && p=$(cat "$REAL_PIDF") && echo "$p $(kill -0 "$p" 2>/dev/null && echo up || echo down)"; }
real_before=$(real_state)

# ---- shims
ALIVE='alive_pid() { case "$1" in ""|*[!0-9]*) return 1 ;; esac; s=$(cut -d" " -f3 /proc/"$1"/stat 2>/dev/null) && [ -n "$s" ] && [ "$s" != Z ]; }'
mk() { printf '#!/bin/bash\n%s\n%s\n' "$ALIVE" "$2" > "$T/shims/$1"; chmod +x "$T/shims/$1"; }
# curl: only the test port's /health. Healthy = the fake server's pid is alive and has no sick-<pid> file
# (sick-<pid> holds "forever" or a count of failing checks still to go, like a model that is still loading).
mk curl 'url=; for a in "$@"; do case "$a" in http://*) url=$a ;; esac; done
echo "$url" >> "$FAKE/curl.log"
[ "$url" = "http://127.0.0.1:$TRANSCRIBE_SERVER_PORT/health" ] || { echo "curl shim: unexpected URL $url" >&2; exit 99; }
p=$(cat "$FAKE/server.pid" 2>/dev/null) && alive_pid "$p" || { echo "${p:-none} dead" >> "$FAKE/health.log"; exit 7; }
if [ -f "$FAKE/sick-$p" ]; then
  n=$(cat "$FAKE/sick-$p")
  [ "$n" = forever ] && { echo "$p sick" >> "$FAKE/health.log"; exit 22; }
  [ "$n" -gt 0 ] && { echo $((n - 1)) > "$FAKE/sick-$p"; echo "$p loading" >> "$FAKE/health.log"; exit 22; }
fi
echo "$p ok sick=[$(cat "$FAKE/sick-$p" 2>&1)]" >> "$FAKE/health.log"
echo "{\"status\":\"ok\"}"'
mk sleep 'echo "$*" >> "$FAKE/sleep.log"; exec "$REAL_SLEEP" 0.02'
mk nproc 'cat "$FAKE/nproc" 2>/dev/null || echo 32'
export PATH=$T/shims:$PATH

mk_server() {   # fake whisper-server: records args/pid, optionally starts sick or ignores SIGTERM, then sleeps
  mkdir -p "$(dirname "$SRV")"
  cat > "$SRV" <<'EOF'
#!/bin/bash
printf '%s\n' "$*" >> "$FAKE/server.args"
echo $$ >> "$FAKE/all.pids"
[ -f "$FAKE/next-sick" ] && cp "$FAKE/next-sick" "$FAKE/sick-$$"
[ -e "$FAKE/ignore-term" ] && trap '' TERM
echo $$ > "$FAKE/server.pid.tmp$$" && mv "$FAKE/server.pid.tmp$$" "$FAKE/server.pid"   # atomic: curl reads it concurrently
exec "$REAL_SLEEP" 600
EOF
  chmod +x "$SRV"
}

pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2" | head -30; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
has() { if [[ "$2" == *"$3"* ]]; then ok "$1"; else no "$1" "$2"; fi; }
hasnt() { if [[ "$2" == *"$3"* ]]; then no "$1" "$2"; else ok "$1"; fi; }
lines() { [ -f "$1" ] && wc -l < "$1" | tr -d ' ' || echo 0; }
alive() {   # alive PID -> up|down; waits up to ~30 s for a killed process to go (a loaded CI host is slow)
  local s i
  case "$1" in ""|*[!0-9]*) echo down; return ;; esac
  for i in $(seq 1500); do
    s=$(cut -d' ' -f3 "/proc/$1/stat" 2>/dev/null)
    [ -n "$s" ] && [ "$s" != Z ] || { echo down; return; }
    [ "${2:-}" = wait ] || { echo up; return; }
    "$REAL_SLEEP" 0.02
  done
  echo up
}
srv_pid() { cat "$FAKE/server.pid" 2>/dev/null; }

echo "# usage"
err=$("$W" 2>&1); rc=$?
eq "no args: rc 2" "$rc" "2"
has "no args: usage" "$err" "usage: whisper-server-ctl start|stop|restart|status|ensure"
err=$("$W" bogus 2>&1); rc=$?
eq "unknown command: rc 2" "$rc" "2"

echo "# status while down"
out=$("$W" status 2>&1); rc=$?
eq "status down: rc 1" "$rc" "1"
eq "status down: says down" "$out" "down"
[ -d "$DIR" ] && ok "state dir created under HOME" || no "state dir created under HOME"

echo "# missing files: rc 1, nothing started"
err=$("$W" ensure 2>&1); rc=$?
eq "no binary: rc 1" "$rc" "1"
eq "no binary: names it" "$err" "whisper-server-ctl: missing $SRV"
mk_server
err=$("$W" ensure 2>&1); rc=$?
eq "no model: rc 1" "$rc" "1"
eq "no model: names it" "$err" "whisper-server-ctl: missing $MODEL"
mkdir -p "$M"; : > "$MODEL"
err=$("$W" start 2>&1); rc=$?
eq "no VAD model: rc 1" "$rc" "1"
eq "no VAD model: names it" "$err" "whisper-server-ctl: missing $VAD"
eq "missing files: server never launched" "$(lines "$FAKE/server.args")" "0"
[ -f "$PIDF" ] && no "missing files: no pid file" || ok "missing files: no pid file"
: > "$VAD"

echo "# ensure starts the server"
out=$("$W" ensure 2>&1); rc=$?
eq "ensure: rc 0" "$rc" "0"
eq "ensure: quiet" "$out" ""
p1=$(srv_pid)
eq "ensure: pid file = server pid" "$(cat "$PIDF" 2>/dev/null)" "$p1"
eq "ensure: server up" "$(alive "$p1")" "up"
args=$(cat "$FAKE/server.args")
has "binds 127.0.0.1 on the test port" "$args" "--host 127.0.0.1 --port $TRANSCRIBE_SERVER_PORT "
has "preloads the default model" "$args" "-m $MODEL "
has "threads capped at 24 (nproc 32)" "$args" " -t 24 "
has "beam/best-of defaults" "$args" "-bs 5 -bo 5"
has "VAD with the silero model" "$args" "--vad -vm $VAD -vp 200 -vsd 500"
has "tmp dir under the state dir" "$args" "--tmp-dir $DIR"
[ -f "$DIR/server.log" ] && ok "server log written" || no "server log written"
eq "every health check went to the test port" "$(grep -vcxF "http://127.0.0.1:$TRANSCRIBE_SERVER_PORT/health" "$FAKE/curl.log")" "0"

echo "# status and idempotent ensure"
out=$("$W" status); rc=$?
eq "status up: rc 0" "$rc" "0"
eq "status up: pid and address" "$out" "up (pid $p1, 127.0.0.1:$TRANSCRIBE_SERVER_PORT)"
: > "$FAKE/sleep.log"
"$W" ensure; rc=$?
eq "ensure while healthy: rc 0" "$rc" "0"
eq "ensure while healthy: no second server" "$(lines "$FAKE/server.args")" "1"
eq "ensure while healthy: no waiting" "$(lines "$FAKE/sleep.log")" "0"
mv "$PIDF" "$T/pidf.bak"
out=$("$W" status); rc=$?
eq "status healthy without pid file: rc 0" "$rc" "0"
eq "status healthy without pid file: pid ?" "$out" "up (pid ?, 127.0.0.1:$TRANSCRIBE_SERVER_PORT)"
mv "$T/pidf.bak" "$PIDF"

echo "# still loading: waits, keeps the server"
echo 1 > "$FAKE/sick-$p1"; : > "$FAKE/sleep.log"
"$W" ensure; rc=$?
eq "loading: rc 0" "$rc" "0"
eq "loading: same server kept" "$(srv_pid) $(lines "$FAKE/server.args")" "$p1 1"
eq "loading: waited 5 s once" "$(grep -cx 5 "$FAKE/sleep.log")" "1"

echo "# wedged: replaced"
echo forever > "$FAKE/sick-$p1"; : > "$FAKE/sleep.log"
"$W" ensure; rc=$?
p2=$(srv_pid)
eq "wedged: rc 0" "$rc" "0"
eq "wedged: old server killed" "$(alive "$p1" wait)" "down"
[ "$p2" != "$p1" ] && ok "wedged: new server started" || no "wedged: new server started" "$p2"
eq "wedged: pid file = new server" "$(cat "$PIDF")" "$p2"
eq "wedged: new server up" "$(alive "$p2")" "up"
eq "wedged: waited 5 s before replacing" "$(head -1 "$FAKE/sleep.log")" "5"

echo "# stop"
out=$("$W" stop 2>&1); rc=$?
eq "stop: rc 0" "$rc" "0"
eq "stop: server gone" "$(alive "$p2" wait)" "down"
[ -f "$PIDF" ] && no "stop: pid file removed" || ok "stop: pid file removed"
"$W" status >/dev/null; eq "status after stop: rc 1" "$?" "1"
"$W" stop; eq "stop with nothing running: rc 0" "$?" "0"
touch "$FAKE/ignore-term"
"$W" start; p3=$(srv_pid); rm -f "$FAKE/ignore-term"
"$W" stop
eq "stop: SIGTERM-ignoring server killed with -9" "$(alive "$p3" wait)" "down"

echo "# stale pid file"
"$REAL_SLEEP" 0 & dead=$!; wait "$dead"
echo "$dead" > "$PIDF"; : > "$FAKE/sleep.log"
"$W" ensure; rc=$?
p4=$(srv_pid)
eq "stale pid: rc 0" "$rc" "0"
eq "stale pid: replaced by the new server" "$(cat "$PIDF")" "$p4"
eq "stale pid: no 5 s wait" "$(grep -cx 5 "$FAKE/sleep.log")" "0"

echo "# restart"
"$W" restart; rc=$?
p5=$(srv_pid)
eq "restart: rc 0" "$rc" "0"
eq "restart: old server gone" "$(alive "$p4" wait)" "down"
[ "$p5" != "$p4" ] && [ "$(alive "$p5")" = up ] && ok "restart: new server up" || no "restart: new server up" "$p5"
"$W" stop

echo "# env overrides"
echo 8 > "$FAKE/nproc"; : > "$T/other.bin"
TRANSCRIBE_MODEL=$T/other.bin "$W" start; rc=$?
eq "custom model: rc 0" "$rc" "0"
args=$(tail -1 "$FAKE/server.args")
has "TRANSCRIBE_MODEL used" "$args" "-m $T/other.bin "
has "threads = nproc when below 24" "$args" " -t 8 "
"$W" stop
TRANSCRIBE_THREADS=3 "$W" start
has "TRANSCRIBE_THREADS used" "$(tail -1 "$FAKE/server.args")" " -t 3 "
"$W" stop
err=$(TRANSCRIBE_MODEL=$T/nope.bin "$W" start 2>&1); rc=$?
eq "missing custom model: rc 1" "$rc" "1"
has "missing custom model: names it" "$err" "missing $T/nope.bin"

echo "# never healthy"
echo forever > "$FAKE/next-sick"; : > "$FAKE/sleep.log"
: > "$FAKE/health.log"
err=$("$W" start 2>&1); rc=$?
p6=$(srv_pid)
eq "never healthy: rc 1" "$rc" "1"
[ "$rc" = 1 ] || { echo "       p6=$p6 all=$(tr '\n' ' ' < "$FAKE/all.pids")"; grep -v " sick$" "$FAKE/health.log" | head -5; }
eq "never healthy: says so" "$err" "whisper-server-ctl: server did not become healthy (see $DIR/server.log)"
eq "never healthy: polled 60 times at 0.5 s" "$(grep -cx 0.5 "$FAKE/sleep.log")" "60"
eq "never healthy: process left for the next ensure" "$(cat "$PIDF") $(alive "$p6")" "$p6 up"
rm -f "$FAKE/next-sick"
"$W" ensure; rc=$?
eq "next ensure recovers: rc 0" "$rc" "0"
eq "next ensure recovers: stuck server killed" "$(alive "$p6" wait)" "down"
"$W" stop

echo "# isolation"
eq "every curl went to the test port" "$(grep -vcxF "http://127.0.0.1:$TRANSCRIBE_SERVER_PORT/health" "$FAKE/curl.log")" "0"
eq "real pid file and server unchanged" "$(real_state)" "$real_before"

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
