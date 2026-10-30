# claude-hub voice @@VOICEKIT@@: log the call account in again inside claude-hub-voice, then restart it (one relay). Unraid > Terminal as root, paste the whole block; it asks for the login code (and the 2FA password if the account has one) and never puts them on a command line. Touches nothing in claude-bot or the vault.
cat > /tmp/claude-hub-voice-relogin.sh <<'HUB'
set -euo pipefail; trap 'echo "claude-hub voice relogin: STOPPED at line $LINENO; nothing after it ran" >&2' ERR
K='@@KIT_DIR@@'; H='@@HUB_ROOT@@'; B='@@BOT_HOME@@'; C=claude-hub-voice; N='claude-hub voice relogin'
cd "$K"; [ "$(cat compose.voice.yaml loader/voiceloader.py voice/live.json | sha256sum | cut -c1-16)" = '@@VOICESUM@@' ] || { echo "$N: the kit changed since this paste was made: ask for a new paste" >&2; exit 1; }
[ -f "$H/compose.voice.yaml" ] && [ "$(docker inspect -f '{{.State.Running}}' $C 2>/dev/null)" = true ] || { echo "$N: $C is not installed or not running: ask the brain" >&2; exit 1; }
[ -e "$B/.voice-relay-off" ] || { echo "$N: the brain's own relay is switched on: ask the brain to hold it first" >&2; exit 1; }
! docker top claude-bot -eo args 2>/dev/null | grep -q '[v]oice_relay.server serve' || { echo "$N: the brain's own relay is running: ask the brain to stop it first" >&2; exit 1; }
R=$(docker exec $C python3 -I -c 'import json; s=json.load(open("/srv/voice/state/voiceloader-status.json")); print(s["release"] if s.get("state") == "running" else "")'); [[ $R =~ ^[0-9a-f]{7,40}$ ]] || { echo "$N: the hub's relay is not running (held or starting): ask the brain" >&2; exit 1; }
docker exec $C python3 -I -B /opt/hub-loader/voiceloader.py --releases /srv/releases --check-release "$R" >/dev/null || { echo "$N: release $R fails the loader's check: ask the brain" >&2; exit 1; }
V=(docker exec -i -e VOICE_TG_CONFIG_FILE=/run/secrets/voice_tg_json -e VOICE_TG_SESSION_DIR=/srv/voice/session -e VOICE_TG_ACCESS_FILE=/srv/voice/config/access.json $C python3 -I -B "/srv/releases/$R/voice/run.py" tg)
rc=0; S=$("${V[@]}" status --offline </dev/null 2>&1) || rc=$?; echo "$S"; [ $rc = 1 ] && [[ $S == *"session: none"* ]] || { echo "$N: refusing: a session is already there, or release $R has no relogin step (see above): ask the brain" >&2; exit 1; }
"${V[@]}" login cancel </dev/null >/dev/null; P=; [[ $S == *", phone)"* ]] || read -r -p 'Phone number of the call account (+country code): ' P </dev/tty
printf '%s\n' "$P" | "${V[@]}" login start --phone-stdin; P=; ok=; st=code
for i in 1 2 3 4 5; do read -r -s -p "$([ $st = code ] && echo 'Login code (Telegram/SMS)' || echo '2FA password of the call account'): " X </dev/tty; echo
  rc=0; printf '%s\n' "$X" | "${V[@]}" login finish --$st-stdin || rc=$?; X=; case $rc in 0) ok=1; break ;; 3) st=password ;; 4) [ $st = code ] && [[ $("${V[@]}" status --offline </dev/null 2>&1 || true) == *"waiter running"* ]] || break ;; *) break ;; esac; done
[ -n "$ok" ] || { "${V[@]}" login cancel </dev/null >/dev/null || true; echo "$N: not logged in (see above); nothing restarted" >&2; exit 1; }
"${V[@]}" harden </dev/null || echo "$N: harden incomplete (see above); the login holds"
docker restart -t 20 $C >/dev/null; for i in $(seq 60); do [ "$(docker inspect -f '{{.State.Health.Status}}' $C)" = healthy ] && break; sleep 3; done
T=; for i in $(seq 30); do T=$(docker exec -e VOICE_LOCAL_TOKEN_FILE=/srv/voice/state/relay/local-token $C python3 -I -B "/srv/releases/$R/voice/run.py" status 2>/dev/null | grep -o '"tg_calls": "[^"]*"' || true); [ -n "$T" ] && break; sleep 2; done
[ "$T" = '"tg_calls": "on"' ] || { echo "$N: logged in, but after the restart the call bridge says: ${T:-no answer}: ask the brain" >&2; exit 1; }
echo "claude-hub voice @@VOICEKIT@@: logged in again; $C restarted; call bridge on"
HUB
bash /tmp/claude-hub-voice-relogin.sh </dev/null; rm -f /tmp/claude-hub-voice-relogin.sh
