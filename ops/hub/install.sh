# claude-hub kit @@KIT@@ (rev @@REV@@): install or update. Unraid > Terminal as root, paste the whole block. Changes nothing in claude-bot or the vault.
cat > /tmp/claude-hub-install.sh <<'HUB'
set -euo pipefail; trap 'echo "claude-hub: STOPPED at line $LINENO; nothing after it ran" >&2' ERR
K='@@KIT_DIR@@'; H='@@HUB_ROOT@@'; B='@@BOT_HOME@@'; N='@@BRAIN_NETWORK@@'
cd "$K"; [ "$({ cat compose.yaml hub.env loader/hubloader.py secrets.copy; [ ! -d ci-image ] || find ci-image -type f | LC_ALL=C sort | xargs cat; } | sha256sum | cut -c1-16)" = '@@KITSUM@@' ] || { echo "claude-hub: the kit changed since this paste was made: ask for a new paste" >&2; exit 1; }
docker network inspect "$N" >/dev/null || { echo "claude-hub: docker network $N is missing (is claude-bot running?)" >&2; exit 1; }
[ -s "$B/.local/share/claude-hub/releases/CURRENT" ] || { echo "claude-hub: no hub release staged yet: ask for a new paste" >&2; exit 1; }
install -d -m 755 "$H" "$H/loader"; install -d -m 700 -o 999 -g 999 "$H/pg"; install -d -m 700 -o 65532 -g 65532 "$H/phoenix"
install -d -m 700 -o 10100 -g 10100 "$H/secrets" "$H/state"; install -d -m 2770 -o 10100 -g 10100 "$H/spool"; install -d -m 700 -o 10101 -g 10100 "$H/ci"
install -m 644 compose.yaml "$H/compose.yaml"; install -m 644 hub.env "$H/.env"; install -m 644 loader/hubloader.py "$H/loader/hubloader.py"; [ ! -d ci-image/build ] || { install -d -m 755 "$H/ci-image"; rm -rf "$H/ci-image/build"; cp -R ci-image/build "$H/ci-image/build"; }
for n in pg_password token_brain token_tg; do [ -s "$H/secrets/$n" ] || head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$H/secrets/$n"; done
while read -r d s; do if [ -f "$B/$s" ]; then install -m 600 "$B/$s" "$H/secrets/$d"; else echo "claude-hub: skipped $d (no such file)"; fi; done < secrets.copy
chown 10100:10100 "$H"/secrets/*; find "$H/secrets" -maxdepth 1 -type f -exec chmod 600 {} +; find "$H/secrets" -mindepth 1 -maxdepth 1 -type d -exec chmod 700 {} +; install -m 600 -o 1000 -g 1000 "$H/secrets/token_brain" "$B/.secrets/hub-token"
[ ! -f "$H/compose.browser.yaml" ] || { U=$(stat -c %u "$B"); G=$(stat -c %g "$B"); for n in browser_ctl_token browser_vnc_pass; do [ ! -f "$H/secrets/$n" ] || chown "$U:$G" "$H/secrets/$n"; done; [ ! -f "$H/secrets/token_browser" ] || { chown "10100:$G" "$H/secrets/token_browser"; chmod 640 "$H/secrets/token_browser"; }; }  # claude-hub-browser runs as the brain's uid: its own two stay the brain's, token_browser is shared (hub = owner, browser = group)
C=/boot/config/plugins/appdata.backup/config.json; if [ -f "$C" ]; then cp -p "$C" "$C.bak-claude-hub"; php -r '$f = $argv[1]; $c = json_decode(file_get_contents($f), true); if (!is_array($c)) exit(1); foreach (array_slice($argv, 2) as $n) $c["containerSettings"][$n]["skip"] = "yes"; exit(file_put_contents($f, json_encode($c, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES)) === false ? 1 : 0);' "$C" @@CONTAINERS@@; fi
cd "$H"; set -- -f compose.yaml; for o in compose.tg.yaml compose.voice.yaml compose.phone.yaml; do [ ! -f "$o" ] || set -- "$@" -f "$o"; done; [ ! -f compose.browser.yaml ] || [ ! -f browser/browser.env ] || set -- --env-file .env --env-file browser/browser.env "$@" -f compose.browser.yaml; docker compose "$@" config -q; docker compose "$@" pull -q; docker compose "$@" up -d --wait --wait-timeout 300
docker compose ps --format '{{.Name}}: {{.Status}}'; echo "claude-hub kit @@KIT@@: OK"
HUB
bash /tmp/claude-hub-install.sh </dev/null; rm -f /tmp/claude-hub-install.sh
