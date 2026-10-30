# claude-hub voice @@VOICEKIT@@: stop the hub's voice relay (rollback: the brain's own relay takes the call account back after this). Keeps its state and the key copies.
cat > /tmp/claude-hub-voice-off.sh <<'HUB'
set -uo pipefail
H='@@HUB_ROOT@@'
cd "$H" || exit 1
if [ -f compose.voice.yaml ]; then docker compose -f compose.yaml -f compose.voice.yaml --profile voice stop -t 20 voice; docker compose -f compose.yaml -f compose.voice.yaml --profile voice rm -f voice; fi
docker rm -f claude-hub-voice >/dev/null 2>&1 || true
if docker ps -a --format '{{.Names}}' | grep -qx claude-hub-voice; then echo "claude-hub voice: still there (docker ps -a)" >&2; else echo "claude-hub voice: stopped and removed (state and key copies kept in $H)"; fi
HUB
bash /tmp/claude-hub-voice-off.sh </dev/null; rm -f /tmp/claude-hub-voice-off.sh
