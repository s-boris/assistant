# claude-hub tg @@TGKIT@@: stop the hub's Telegram adapter (rollback: the brain's own adapter takes the bot back after this). Keeps its data and the token copy.
cat > /tmp/claude-hub-tg-off.sh <<'HUB'
set -uo pipefail
H='@@HUB_ROOT@@'
cd "$H" || exit 1
if [ -f compose.tg.yaml ]; then docker compose -f compose.yaml -f compose.tg.yaml --profile tg stop -t 20 tg; docker compose -f compose.yaml -f compose.tg.yaml --profile tg rm -f tg; fi
docker rm -f claude-hub-tg >/dev/null 2>&1 || true
if docker ps -a --format '{{.Names}}' | grep -qx claude-hub-tg; then echo "claude-hub tg: still there (docker ps -a)" >&2; else echo "claude-hub tg: stopped and removed (data and token copy kept in $H)"; fi
HUB
bash /tmp/claude-hub-tg-off.sh </dev/null; rm -f /tmp/claude-hub-tg-off.sh
