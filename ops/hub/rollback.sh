# claude-hub kit @@KIT@@: remove. Stops and removes the hub containers and keeps their data folder (renamed). claude-bot, the vault and your other containers are not touched.
cat > /tmp/claude-hub-remove.sh <<'HUB'
set -uo pipefail
H='@@HUB_ROOT@@'; B='@@BOT_HOME@@'; T=$(date +%Y%m%d-%H%M%S)
if [ -f "$H/compose.yaml" ]; then (cd "$H" && docker compose --profile tg down --remove-orphans); else echo "claude-hub: no compose file in $H"; fi
if [ -d "$H" ]; then mv "$H" "$H.removed-$T" && echo "claude-hub: data kept in $H.removed-$T"; fi
rm -f "$B/.secrets/hub-token"
C=/boot/config/plugins/appdata.backup/config.json; if [ -f "$C" ]; then php -r '$f = $argv[1]; $c = json_decode(file_get_contents($f), true); if (!is_array($c)) exit(1); foreach (array_slice($argv, 2) as $n) unset($c["containerSettings"][$n]); exit(file_put_contents($f, json_encode($c, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES)) === false ? 1 : 0);' "$C" @@CONTAINERS@@ && echo "claude-hub: Appdata Backup entries removed"; fi
if docker ps -a --format '{{.Names}}' | grep -q '^claude-hub'; then echo "claude-hub: some hub containers remain (docker ps -a)" >&2; else echo "claude-hub: removed"; fi
HUB
bash /tmp/claude-hub-remove.sh </dev/null; rm -f /tmp/claude-hub-remove.sh
