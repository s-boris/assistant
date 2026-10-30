# claude-hub phone @@PHONEKIT@@: stop the hub's phone gateway and give the host port @@FRONT_PORT@@ back to claude-bot (rollback; claude-bot restarts once). Keeps the hub's state and key copies; the brain restarts its own gateway afterwards.
cat > /tmp/claude-hub-phone-off.sh <<'HUB'
set -uo pipefail
H='@@HUB_ROOT@@'; C="$(dirname '@@BOT_HOME@@')/src"
cd "$H" || exit 1
if [ -f compose.phone.yaml ]; then docker compose -f compose.yaml -f compose.phone.yaml --profile phone stop -t 20 phone; docker compose -f compose.yaml -f compose.phone.yaml --profile phone rm -f phone; fi
docker rm -f claude-hub-phone >/dev/null 2>&1 || true
if [ -f "$C/docker-compose.override.yml" ]; then mv "$C/docker-compose.override.yml" "$C/docker-compose.override.yml.p38-off"; docker compose --project-directory "$C" up -d bot; fi
if docker ps -a --format '{{.Names}}' | grep -qx claude-hub-phone; then echo "claude-hub phone: still there (docker ps -a)" >&2; else echo "claude-hub phone: stopped and removed; ports back on claude-bot: $(docker port claude-bot 2>/dev/null | tr '\n' ' ')"; fi
HUB
bash /tmp/claude-hub-phone-off.sh </dev/null; rm -f /tmp/claude-hub-phone-off.sh
