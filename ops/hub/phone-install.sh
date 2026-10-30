# claude-hub phone @@PHONEKIT@@: the phone gateway moves into the hub and takes over its host port @@FRONT_PORT@@ (the app's front) from claude-bot (P3-8). Unraid > Terminal as root, paste the whole block, only after the brain says its own gateway is held. claude-bot restarts once (~1-2 min brain gap); the vault is untouched.
cat > /tmp/claude-hub-phone.sh <<'HUB'
set -euo pipefail; trap 'echo "claude-hub phone: STOPPED at line $LINENO; see above (rollback: hub kit paste phone-rollback)" >&2' ERR
K='@@KIT_DIR@@'; H='@@HUB_ROOT@@'; B='@@BOT_HOME@@'; C="$(dirname '@@BOT_HOME@@')/src"; O='-o 10100 -g 10100'; PY='@@PY_IMAGE@@'
cd "$K"; [ "$(cat compose.phone.yaml loader/phoneloader.py phone/live.json phone/ports.py | sha256sum | cut -c1-16)" = '@@PHONESUM@@' ] || { echo "claude-hub phone: the kit changed since this paste was made: ask for a new paste" >&2; exit 1; }
[ -f "$H/compose.yaml" ] && [ -s "$H/secrets/token_brain" ] || { echo "claude-hub phone: the hub is not installed yet" >&2; exit 1; }
[ -e "$B/.phonegw-off" ] || { echo "claude-hub phone: the brain's own gateway is still switched on: ask the brain to hold it first" >&2; exit 1; }
! docker top claude-bot -eo args 2>/dev/null | grep -q '[o]pt/phonegw serve' || { echo "claude-hub phone: the brain's own gateway is still running: ask the brain to stop it first" >&2; exit 1; }
for f in @@PKI_FILES@@ '@@CLI_TOKEN_SRC@@' '@@LINK_SRC@@' '@@GWCONF_SRC@@' '@@GWSTATE_SRC@@/phonegw.db'; do [ -s "$B/$f" ] || { echo "claude-hub phone: missing in the brain's home: $f" >&2; exit 1; }; done
[ -f "$C/docker-compose.yml" ] && [ ! -e "$C/docker-compose.override.yml" ] || { echo "claude-hub phone: claude-bot's compose is not as expected ($C): ask the brain (forwarder option)" >&2; exit 1; }
N="$C/docker-compose.override.yml.new"; rm -f "$N"; docker compose --project-directory "$C" config --format json | docker run --rm -i --network none --entrypoint python3 -v "$K/phone/ports.py:/p.py:ro" "$PY" -I /p.py @@FRONT_PORT@@ > "$N" || { rm -f "$N"; echo "claude-hub phone: claude-bot's ports are not as expected (nothing changed): send the brain the lines above" >&2; exit 1; }
install -d -m 755 "$H/phone"; install -d -m 750 -o 0 -g 10100 "$H/phone/config"; install -d -m 700 $O "$H/phone/state" "$H/secrets/phone"
[ -s "$H/secrets/token_phone" ] || head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$H/secrets/token_phone"; chown 10100:10100 "$H/secrets/token_phone"; chmod 600 "$H/secrets/token_phone"
for f in @@PKI_FILES@@; do install -m 600 $O "$B/$f" "$H/secrets/phone/$(basename "$f")"; done; install -m 600 $O "$B/@@CLI_TOKEN_SRC@@" "$H/secrets/phone/internal.token"; install -m 600 $O "$B/@@LINK_SRC@@" "$H/secrets/phone_voice_link_token"
G="$H/phone/state/gw"; if [ -e "$G" ]; then rm -rf "$G.prev"; mv "$G" "$G.prev"; fi; install -d -m 700 $O "$G"; cp -a "$B/@@GWSTATE_SRC@@/." "$G/"; rm -f "$G/serve.pid"; chown -R 10100:10100 "$G"
install -m 640 -o 0 -g 10100 phone/live.json "$H/phone/config/live.json"; install -m 640 -o 0 -g 10100 "$B/@@GWCONF_SRC@@" "$H/phone/config/gw.json"; install -m 644 compose.phone.yaml "$H/compose.phone.yaml"; install -m 644 loader/phoneloader.py "$H/loader/phoneloader.py"
cd "$H"; set -- -f compose.yaml -f compose.phone.yaml; for o in compose.voice.yaml compose.tg.yaml; do [ ! -f "$o" ] || set -- "$@" -f "$o"; done; [ ! -f compose.browser.yaml ] || [ ! -f browser/browser.env ] || set -- --env-file .env --env-file browser/browser.env "$@" -f compose.browser.yaml; docker compose "$@" config -q; docker compose "$@" --profile phone pull -q phone
mv "$N" "$C/docker-compose.override.yml"; docker compose --project-directory "$C" up -d bot
docker compose "$@" up -d --wait --wait-timeout 180 hub
docker compose "$@" --profile phone up -d --wait --wait-timeout 150 phone
docker compose "$@" --profile phone ps --format '{{.Name}}: {{.Status}} {{.Ports}}'; echo "claude-hub phone @@PHONEKIT@@: OK"
HUB
bash /tmp/claude-hub-phone.sh </dev/null; rm -f /tmp/claude-hub-phone.sh
