# claude-hub voice @@VOICEKIT@@: the voice relay moves into the hub (P3-7). Unraid > Terminal as root, paste the whole block, only after the brain says its own relay is held. Touches nothing in claude-bot or the vault.
cat > /tmp/claude-hub-voice.sh <<'HUB'
set -euo pipefail; trap 'echo "claude-hub voice: STOPPED at line $LINENO; nothing after it ran" >&2' ERR
K='@@KIT_DIR@@'; H='@@HUB_ROOT@@'; B='@@BOT_HOME@@'; O='-o 10100 -g 10100'
cd "$K"; [ "$(cat compose.voice.yaml loader/voiceloader.py voice/live.json | sha256sum | cut -c1-16)" = '@@VOICESUM@@' ] || { echo "claude-hub voice: the kit changed since this paste was made: ask for a new paste" >&2; exit 1; }
[ -f "$H/compose.yaml" ] && [ -s "$H/secrets/token_brain" ] || { echo "claude-hub voice: the hub is not installed yet" >&2; exit 1; }
[ -e "$B/.voice-relay-off" ] || { echo "claude-hub voice: the brain's own relay is still switched on: ask the brain to hold it first" >&2; exit 1; }
! docker top claude-bot -eo args 2>/dev/null | grep -q '[v]oice_relay.server serve' || { echo "claude-hub voice: the brain's own relay is still running: ask the brain to stop it first" >&2; exit 1; }
for f in '@@GEMINI_SRC@@' '@@TGJSON_SRC@@' '@@ENV_SRC@@' '@@SESSION_SRC@@' '@@ACCESS_SRC@@' '@@LINK_SRC@@'; do [ -s "$B/$f" ] || { echo "claude-hub voice: a key file is missing in the brain's home: $f" >&2; exit 1; }; done
install -d -m 755 "$H/voice"; install -d -m 750 -o 0 -g 10100 "$H/voice/config"; install -d -m 700 $O "$H/voice/state" "$H/secrets/voice"
[ -s "$H/secrets/token_voice" ] || head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$H/secrets/token_voice"; chown 10100:10100 "$H/secrets/token_voice"; chmod 600 "$H/secrets/token_voice"
install -m 600 $O "$B/@@GEMINI_SRC@@" "$H/secrets/voice_gemini_key"; install -m 600 $O "$B/@@TGJSON_SRC@@" "$H/secrets/voice_tg_json"; install -m 600 $O "$B/@@ENV_SRC@@" "$H/secrets/voice_relay_env"; install -m 600 $O "$B/@@LINK_SRC@@" "$H/secrets/voice_link_token"
install -m 600 $O "$B/@@SESSION_SRC@@" "$H/secrets/voice/voice-tg.session"; [ ! -s "$B/@@SESSION_SRC@@-journal" ] || install -m 600 $O "$B/@@SESSION_SRC@@-journal" "$H/secrets/voice/voice-tg.session-journal"
install -m 640 -o 0 -g 10100 voice/live.json "$H/voice/config/live.json"; install -m 640 -o 0 -g 10100 "$B/@@ACCESS_SRC@@" "$H/voice/config/access.json"; install -m 644 compose.voice.yaml "$H/compose.voice.yaml"; install -m 644 loader/voiceloader.py "$H/loader/voiceloader.py"
cd "$H"; set -- -f compose.yaml -f compose.voice.yaml; for o in compose.tg.yaml compose.phone.yaml; do [ ! -f "$o" ] || set -- "$@" -f "$o"; done; [ ! -f compose.browser.yaml ] || [ ! -f browser/browser.env ] || set -- --env-file .env --env-file browser/browser.env "$@" -f compose.browser.yaml; docker compose "$@" config -q; docker compose "$@" --profile voice pull -q voice
docker compose "$@" up -d --wait --wait-timeout 180 hub
docker compose "$@" --profile voice up -d --force-recreate --wait --wait-timeout 150 voice
docker compose "$@" --profile voice ps --format '{{.Name}}: {{.Status}}'; echo "claude-hub voice @@VOICEKIT@@: OK"
HUB
bash /tmp/claude-hub-voice.sh </dev/null; rm -f /tmp/claude-hub-voice.sh
