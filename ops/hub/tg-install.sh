# claude-hub tg @@TGKIT@@: the Telegram adapter moves into the hub (H6). Unraid > Terminal as root, paste the whole block, only after the brain says its own adapter is stopped. Touches nothing in claude-bot or the vault.
cat > /tmp/claude-hub-tg.sh <<'HUB'
set -euo pipefail; trap 'echo "claude-hub tg: STOPPED at line $LINENO; nothing after it ran" >&2' ERR
K='@@KIT_DIR@@'; H='@@HUB_ROOT@@'; B='@@BOT_HOME@@'
cd "$K"; [ "$(cat compose.tg.yaml loader/tgloader.ts tg/live.json | sha256sum | cut -c1-16)" = '@@TGSUM@@' ] || { echo "claude-hub tg: the kit changed since this paste was made: ask for a new paste" >&2; exit 1; }
[ -f "$H/compose.yaml" ] && [ -s "$H/secrets/token_tg" ] || { echo "claude-hub tg: the hub is not installed yet (hub install paste first)" >&2; exit 1; }
[ -e "$B/.config/tg-adapter/live.off" ] || { echo "claude-hub tg: the brain's own Telegram adapter is still switched on: ask the brain to stop it first" >&2; exit 1; }
! docker top claude-bot -eo args 2>/dev/null | grep -q '[c]li.ts run --instance live' || { echo "claude-hub tg: the brain's own Telegram adapter is still running: ask the brain to stop it first" >&2; exit 1; }
[ -s "$B/@@TOKEN_SRC@@" ] && [ -s "$B/@@ACCESS_SRC@@" ] || { echo "claude-hub tg: the bot token or the allowlist file is missing in the brain's home" >&2; exit 1; }
install -d -m 755 "$H/tg"; install -d -m 750 -o 0 -g 10100 "$H/tg/config"; install -d -m 700 -o 10100 -g 10100 "$H/tg/state" "$H/tgfiles" "$H/tgfiles/in" "$H/tgfiles/out"
install -m 600 -o 10100 -g 10100 "$B/@@TOKEN_SRC@@" "$H/secrets/tg_live_token"
install -m 640 -o 0 -g 10100 "$B/@@ACCESS_SRC@@" "$H/tg/config/access.json"; install -m 640 -o 0 -g 10100 tg/live.json "$H/tg/config/live.json"
install -m 644 compose.tg.yaml "$H/compose.tg.yaml"; install -m 644 loader/tgloader.ts "$H/loader/tgloader.ts"
cd "$H"; set -- -f compose.yaml -f compose.tg.yaml; for o in compose.voice.yaml compose.phone.yaml; do [ ! -f "$o" ] || set -- "$@" -f "$o"; done; [ ! -f compose.browser.yaml ] || [ ! -f browser/browser.env ] || set -- --env-file .env --env-file browser/browser.env "$@" -f compose.browser.yaml; docker compose "$@" config -q; docker compose "$@" --profile tg pull -q tg
docker compose "$@" up -d --wait --wait-timeout 180 hub
docker compose "$@" --profile tg up -d --wait --wait-timeout 150 tg
docker compose "$@" --profile tg ps --format '{{.Name}}: {{.Status}}'; echo "claude-hub tg @@TGKIT@@: OK"
HUB
bash /tmp/claude-hub-tg.sh </dev/null; rm -f /tmp/claude-hub-tg.sh
