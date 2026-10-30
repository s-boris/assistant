# claude-hub browser @@BROWSERKIT@@: his Chrome in claude-hub-browser; its profile is COPIED there once from the brain (claude-bot keeps its copy). Unraid > Terminal as root, paste the whole block, only after the brain says no Chrome of its own runs on the profile. Touches nothing in claude-bot or the vault.
cat > /tmp/claude-hub-browser.sh <<'HUB'
set -euo pipefail; trap 'echo "claude-hub browser: STOPPED at line $LINENO; nothing after it ran" >&2' ERR
K='@@KIT_DIR@@'; H='@@HUB_ROOT@@'; B='@@BOT_HOME@@'; P='@@PROFILE_SRC@@'; U=$(stat -c %u "$B"); G=$(stat -c %g "$B"); O="-o $U -g $G"
cd "$K"; [ "$(cat compose.browser.yaml loader/browserloader.py browser/live.json browser/build/Dockerfile browser/browser.env | sha256sum | cut -c1-16)" = '@@BROWSERSUM@@' ] || { echo "claude-hub browser: the kit changed since this paste was made: ask for a new paste" >&2; exit 1; }
[ -f "$H/compose.yaml" ] && [ -s "$H/secrets/token_brain" ] || { echo "claude-hub browser: the hub is not installed yet" >&2; exit 1; }
! docker top claude-bot -eo args 2>/dev/null | grep -q "[u]ser-data-dir=.*$P" || { echo "claude-hub browser: the brain's Chrome still runs on the profile: ask the brain to stop it first" >&2; exit 1; }
for f in "$P/Default/Preferences" '@@CTL_SRC@@' '@@VNC_SRC@@'; do [ -s "$B/$f" ] || { echo "claude-hub browser: missing in the brain's home: $f" >&2; exit 1; }; done
install -d -m 755 "$H/browser" "$H/browser/build"; install -d -m 750 -o 0 -g "$G" "$H/browser/config"; install -d -m 700 $O "$H/browser/state" "$H/browser/profile"
[ -s "$H/secrets/token_browser" ] || { head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$H/secrets/token_browser"; NEW=1; }; chown "10100:$G" "$H/secrets/token_browser"; chmod 640 "$H/secrets/token_browser"
install -m 600 $O "$B/@@CTL_SRC@@" "$H/secrets/browser_ctl_token"; install -m 600 $O "$B/@@VNC_SRC@@" "$H/secrets/browser_vnc_pass"
if [ -z "$(ls -A "$H/browser/profile")" ]; then tar -C "$B/$P" --exclude='./Singleton*' --exclude='*/Cache' --exclude='*/Code Cache' --exclude='*/GPUCache' --exclude='*/Service Worker/CacheStorage' -cf - . | tar -C "$H/browser/profile" -xpf -; chown -R "$U:$G" "$H/browser/profile"; echo "claude-hub browser: profile copied ($(du -sh "$H/browser/profile" | cut -f1)); claude-bot keeps its copy"; else echo "claude-hub browser: profile already there: kept (re-copy: hub kit paste browser-recopy)"; fi
install -m 640 -o 0 -g "$G" browser/live.json "$H/browser/config/live.json"; install -m 644 browser/build/Dockerfile "$H/browser/build/Dockerfile"; install -m 644 compose.browser.yaml "$H/compose.browser.yaml"; install -m 644 loader/browserloader.py "$H/loader/browserloader.py"
{ cat browser/browser.env; echo "BROWSER_UID=$U"; echo "BROWSER_GID=$G"; } > "$H/browser/browser.env"; chmod 644 "$H/browser/browser.env"
cd "$H"; set -- --env-file .env --env-file browser/browser.env -f compose.yaml; for o in compose.tg.yaml compose.voice.yaml compose.phone.yaml; do [ ! -f "$o" ] || set -- "$@" -f "$o"; done; set -- "$@" -f compose.browser.yaml; docker compose "$@" config -q; docker compose "$@" --profile browser build -q browser
[ -z "${NEW:-}" ] || { docker restart claude-hub >/dev/null; docker compose "$@" up -d --wait --wait-timeout 180 hub; }
docker compose "$@" --profile browser up -d --wait --wait-timeout 240 browser
docker compose "$@" --profile browser ps --format '{{.Name}}: {{.Status}}' browser; echo "claude-hub browser @@BROWSERKIT@@: OK"
HUB
bash /tmp/claude-hub-browser.sh </dev/null; rm -f /tmp/claude-hub-browser.sh
