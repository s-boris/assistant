# claude-hub secrets @@SECKIT@@ (H10): copies @@COUNT@@ credential file(s) BY PATH from the brain's home into the hub's secrets folder (0600, the hub's uid; a userpass row becomes one user:password line) and restarts claude-hub so it loads them into memory (~10 s API gap). Unraid > Terminal as root, paste the whole block; not 23:30-02:00 on a cut-over night. Moves and deletes nothing; prints names, never values.
cat > /tmp/claude-hub-secrets.sh <<'HUB'
set -euo pipefail; trap 'echo "claude-hub secrets: STOPPED at line $LINENO; nothing after it ran" >&2' ERR
K='@@KIT_DIR@@'; H='@@HUB_ROOT@@'; B='@@BOT_HOME@@'
cd "$K"; [ "$(sha256sum < secrets.h10 | cut -c1-16)" = '@@SECSUM@@' ] || { echo "claude-hub secrets: the list changed since this paste was made: ask for a new paste" >&2; exit 1; }
[ -f "$H/compose.yaml" ] && [ -d "$H/secrets" ] || { echo "claude-hub secrets: the hub is not installed yet (hub install paste first)" >&2; exit 1; }
# kv FILE KEY: the KEY's value from "KEY: value" / "KEY=value" lines (CR, trailing blanks, outer quotes stripped); every match, the caller keeps the first. No early-exit reader, so pipefail never sees a SIGPIPE.
kv() { tr -d '\r' < "$1" | sed -n "s/^$2[[:space:]]*[:=][[:space:]]*//p" | sed -e 's/[[:space:]]*$//' -e "s/^[\"']//" -e "s/[\"']\$//"; }
n=0; while read -r d s m; do
  if [ ! -f "$B/$s" ] || [ -L "$B/$s" ]; then echo "claude-hub secrets: $d: no plain file at $s (skipped)" >&2; continue; fi
  t="$H/secrets/.$d.new"; rm -f "$t"
  if [ "$m" = userpass ]; then u=$(kv "$B/$s" user); u=${u%%$'\n'*}; p=$(kv "$B/$s" password); p=${p%%$'\n'*}
    if [ -z "$u" ] || [ -z "$p" ]; then u=; p=; echo "claude-hub secrets: $d: no user/password lines in $s (skipped)" >&2; continue; fi
    (umask 077; printf '%s:%s' "$u" "$p" > "$t"); u=; p=; chown 10100:10100 "$t"
  else install -m 600 -o 10100 -g 10100 "$B/$s" "$t"; cmp -s "$B/$s" "$t"; fi
  if [ -f "$H/secrets/$d" ] && cmp -s "$t" "$H/secrets/$d"; then rm -f "$t"; echo "$d: unchanged"; continue; fi
  mv -f "$t" "$H/secrets/$d"; echo "$d: copied${m:+ (one $m line)}"; n=$((n+1))
done < secrets.h10
chmod 700 "$H/secrets"; b=0; while read -r d s; do f="$H/secrets/$d"; [ -f "$f" ] || continue; [ "$(stat -c '%a %u' "$f")" = "600 10100" ] || { echo "claude-hub secrets: mode/owner wrong on $d" >&2; b=1; }; done < secrets.h10; [ "$b" = 0 ]
if [ "$n" -gt 0 ]; then docker restart claude-hub >/dev/null; echo "claude-hub restarted: it loads the secrets into memory"; fi
echo "claude-hub secrets @@SECKIT@@: OK ($n copied)"
HUB
bash /tmp/claude-hub-secrets.sh </dev/null; rm -f /tmp/claude-hub-secrets.sh
