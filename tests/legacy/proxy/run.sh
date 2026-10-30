#!/usr/bin/env bash
# Offline tests for ~/bin/proxy (mirror of the owner's nginx config on the Webserver VM, profile network.hosts.web_vm: pull, push with
# nginx -t + reload and rollback, test, cert, log). Exit 0 = all pass. PROXY_BIN=<path> tests another copy.
# The VM is NEVER reached: the seams PROXY_HOST (a fake host, user@proxy-test.invalid) and PROXY_MIRROR (a temp git repo)
# are set; PATH shims for ssh and rsync emulate the VM on a temp tree ($T/server/etc/nginx) and refuse (exit 99, logged)
# any other host (also the fixture profile's web@192.0.2.20), anything naming 192.0.2.x or 192.168.x and any
# unknown remote command; the real ssh is never executed (scp/sftp
# shims refuse too; RSYNC_RSH points at a refusing shim). The suite refuses a copy without both seams, with an absolute
# ssh/rsync path, or with an IP address in its code (the VM comes from the profile). HOME and TMPDIR are temp dirs.
# The fake nginx -t fails (on stderr, like nginx) when any file on the fake VM contains BROKEN; reload/cert/copy failures
# are switched on per test.
# Covers: help, guards, pull (initial, no drift, drift + --delete, dash-www excluded, refuses tracked/untracked/staged
# local edits, unreachable VM), push (nothing, happy path, new files, never deletes what it did not add, missing
# message), the drift check before a push (server changes committed first and kept; same file edited on both sides
# refused; server unreadable), the rollback on a failed nginx -t (restored, files and new directories the push added
# removed, VM-only files kept, revert commit, not reloaded), a rename, rollback incomplete (copy back fails, a name
# that is no exact filter rule), a failed reload (rolled back), a failed or partial copy (server restored, commit
# undone, retry works), nginx warnings,
# test, cert (suffix, injection refused, failure -> no pull), log, missing mirror, defaults, isolation.
set -uo pipefail
# Synthetic profile (tests never read the real one): tests/legacy/profile once that suite moves here, else ~/opt/profile-tests.
_pf="$(cd "$(dirname "$0")/.." && pwd)"; [ -f "$_pf/profile/fixtures/profile.json" ] && _pf=$_pf/profile || _pf=$HOME/opt/profile-tests
export ASSISTANT_PROFILE="$_pf/fixtures/profile.json"
P=${PROXY_BIN:-$(cd "$(dirname "$0")/../../../scripts" && pwd)/proxy}
grep -q 'PROXY_HOST:-' "$P" && grep -q 'PROXY_MIRROR:-' "$P" \
  || { echo "refusing: $P has no PROXY_HOST/PROXY_MIRROR seams (it would reach the owner's nginx VM)"; exit 2; }
grep -q . <<<"$(grep -nE '(/usr)?/bin/(ssh|rsync|scp)\b' "$P" | grep -v '^[0-9]*:#')" \
  && { echo "refusing: $P calls ssh/rsync/scp by absolute path (the PATH shims could not stop it)"; exit 2; }
grep -q . <<<"$(grep -nE '([0-9]{1,3}\.){3}[0-9]{1,3}' "$P" | grep -v '^[0-9]*:#')" \
  && { echo "refusing: $P names an IP address in its code (the VM must come from the profile)"; exit 2; }
REAL_MIRROR=/home/node/opt/nginx-mirror
export REAL_GIT=$(command -v git) REAL_RSYNC=$(command -v rsync)
T=$(mktemp -d "${TMPDIR:-/tmp}/proxy-tests.XXXXXX")
trap 'rm -rf "$T"' EXIT
_pg=$HOME/bin/profile-get   # the VM and the domain from the synthetic profile; a copy under test may have none beside it
export T FAKE=$T/fake HOME=$T/home TMPDIR=$T/tmp GIT_CONFIG_NOSYSTEM=1 SV=$T/server/etc/nginx
export PROXY_HOST=user@proxy-test.invalid PROXY_MIRROR=$T/mirror FAKE_HOST=user@proxy-test.invalid
export RSYNC_RSH=$T/shims/scp SSH_AUTH_SOCK=
mkdir -p "$T/shims" "$FAKE" "$HOME" "$TMPDIR" "$SV" "$T/cwd"
[ -x "$_pg" ] && ln -s "$_pg" "$T/shims/profile-get"
rgit() { "$REAL_GIT" --no-optional-locks -C "$REAL_MIRROR" "$@" 2>/dev/null; }
real_state() { echo "$(rgit rev-parse HEAD) $(rgit status --porcelain | sha256sum | cut -c1-16)"; }
real_before=$(real_state)

# ---- shims (the real ssh is never run)
mk() { printf '#!/bin/bash\n%s\n' "$2" > "$T/shims/$1"; chmod +x "$T/shims/$1"; }
REFUSE='refuse() { echo "$(basename "$0") shim: refusing: $*" | tee -a "$T/refused.log" >&2; exit 99; }'
mk ssh "$REFUSE"'
echo "ssh $*" >> "$FAKE/ssh.log"
case "$*" in *192.0.2.*|*192.168.*) refuse "$*" ;; esac
[ "$#" = 2 ] && [ "$1" = "$FAKE_HOST" ] || refuse "$*"
[ -e "$FAKE/ssh-down" ] && { echo "ssh: connect to host proxy-test.invalid port 22: Connection refused" >&2; exit 255; }
case $2 in
  "sudo -n /usr/sbin/nginx -t")
    [ -e "$FAKE/nginx-warn" ] && echo "nginx: [warn] the \"listen ... http2\" directive is deprecated" >&2
    if grep -rqs BROKEN "$SV"; then
      echo "nginx: [emerg] unknown directive \"BROKEN\" in /etc/nginx/$(grep -rls BROKEN "$SV" | head -1 | sed "s|^$SV/||"):1" >&2
      echo "nginx: configuration file /etc/nginx/nginx.conf test failed" >&2; exit 1
    fi
    echo "nginx: the configuration file /etc/nginx/nginx.conf syntax is ok" >&2
    echo "nginx: configuration file /etc/nginx/nginx.conf test is successful" >&2 ;;
  "sudo -n /usr/bin/systemctl reload nginx")
    [ -e "$FAKE/reload-fail" ] && { echo "Job for nginx.service failed because the control process exited with error code." >&2; exit 1; }
    (cd "$SV" && find . -type f | sort | xargs cat | sha256sum | cut -c1-16) >> "$FAKE/reloads.log" ;;
  "sudo -n /usr/local/sbin/claude-cert "*)
    n=${2#sudo -n /usr/local/sbin/claude-cert }; echo "$n" >> "$FAKE/certs.log"
    rc=$(cat "$FAKE/cert-rc" 2>/dev/null || echo 0); [ "$rc" = 0 ] || { echo "certbot failed" >&2; exit "$rc"; }
    printf "server_name %s;\nssl_certificate /etc/letsencrypt/live/%s/fullchain.pem;\n" "$n" "$n" > "$SV/sites-available/$n.conf" ;;
  *) refuse "$*" ;;
esac'
mk rsync "$REFUSE"'
echo "rsync $*" >> "$FAKE/rsync.log"
args=(); dir=""
for a in "$@"; do
  case $a in
    *192.0.2.*|*192.168.*) refuse "$*" ;;
    "$FAKE_HOST:/etc/nginx/") [ "$a" = "${!#}" ] && dir=to || dir=from; args+=("$SV/") ;;
    -*) args+=("$a") ;;
    *:*) refuse "unknown remote in: $*" ;;
    *) args+=("$a") ;;
  esac
done
if [ "$dir" = to ]; then
  n=$(( $(cat "$FAKE/to.count" 2>/dev/null || echo 0) + 1 )); echo $n > "$FAKE/to.count"
  grep -qx "$n" "$FAKE/fail-to" 2>/dev/null && { echo "rsync: connection unexpectedly closed (0 bytes received so far) [sender]" >&2; exit 12; }
  grep -qx "$n" "$FAKE/partial-to" 2>/dev/null && { "$REAL_RSYNC" "${args[@]}"; echo "rsync: connection unexpectedly closed (4096 bytes received so far) [sender]" >&2; exit 12; }
elif [ "$dir" = from ] && [ -e "$FAKE/fail-from" ]; then
  echo "ssh: connect to host proxy-test.invalid port 22: Connection refused" >&2; exit 255
fi
exec "$REAL_RSYNC" "${args[@]}"'
mk scp "$REFUSE"'
refuse "$*"'
cp "$T/shims/scp" "$T/shims/sftp"
export PATH=$T/shims:$PATH
[ "$(command -v ssh)" = "$T/shims/ssh" ] && [ "$(command -v rsync)" = "$T/shims/rsync" ] \
  || { echo "refusing: ssh/rsync do not resolve to the shims"; exit 2; }

pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2" | head -30; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
has() { if [[ "$2" == *"$3"* ]]; then ok "$1"; else no "$1" "$2"; fi; }
hasnt() { if [[ "$2" == *"$3"* ]]; then no "$1" "$2"; else ok "$1"; fi; }
M=$T/mirror; E=$M/etc-nginx
g() { "$REAL_GIT" -C "$M" "$@"; }
run() { (cd "$T/cwd" && "$P" "$@") > "$T/out" 2> "$T/err"; rc=$?; out=$(cat "$T/out"); err=$(cat "$T/err"); }
f() { cat "$FAKE/$1" 2>/dev/null; }
cnt() { local n; n=$(grep -c -e "${2:-.}" "$FAKE/$1" 2>/dev/null); echo "${n:-0}"; }
clean() { rm -f "$FAKE"/*.log "$FAKE"/to.count "$FAKE"/fail-* "$FAKE"/partial-to "$FAKE"/ssh-down "$FAKE"/nginx-warn "$FAKE"/reload-fail "$FAKE"/cert-rc; }
tmpleft() { ls -A "$TMPDIR" | wc -l; }
sv() { cat "$SV/$1" 2>/dev/null || echo "<absent>"; }

# ---- fixtures: the fake VM and an empty mirror repo
mkdir -p "$SV/sites-available" "$SV/sites-enabled" "$SV/snippets" "$SV/dash-www"
echo 'user www-data; include /etc/nginx/sites-enabled/*;' > "$SV/nginx.conf"
echo 'server { server_name emby.example.org; }' > "$SV/sites-available/emby.conf"
echo 'server { server_name old.example.org; }' > "$SV/sites-available/old.conf"
echo 'add_header X-Frame-Options DENY;' > "$SV/snippets/headers.conf"
ln -s ../sites-available/emby.conf "$SV/sites-enabled/emby.conf"
echo '<html>dash</html>' > "$SV/dash-www/index.html"
printf '[user]\n\tname = Test Bot\n\temail = test@example.invalid\n' > "$HOME/.gitconfig"
mkdir -p "$E"; g init -q -b main; echo mirror > "$M/README"; g add README; g commit -q -m init

echo "# guards: the shims refuse the real VM and anything unknown"
ssh web@192.0.2.20 'sudo -n /usr/sbin/nginx -t' 2>/dev/null; eq "ssh to 192.0.2.20 refused" "$?" "99"
rsync -a "$T/home/" web@192.0.2.20:/etc/nginx/ 2>/dev/null; eq "rsync to 192.0.2.20 refused" "$?" "99"
rsync -a "$T/home/" other@host:/tmp/ 2>/dev/null; eq "rsync to another remote refused" "$?" "99"
ssh "$FAKE_HOST" 'sudo rm -rf /etc/nginx' 2>/dev/null; eq "unknown remote command refused" "$?" "99"
scp x web@192.0.2.20:/tmp 2>/dev/null; eq "scp refused" "$?" "99"
eq "5 refusals logged so far" "$(wc -l < "$T/refused.log")" "5"; rm -f "$T/refused.log"
env -u PROXY_HOST "$P" test >/dev/null 2>&1; eq "proxy test with the DEFAULT host: refused by the shim" "$?" "99"
env -u PROXY_HOST "$P" pull >/dev/null 2>&1; eq "proxy pull with the DEFAULT host: fails" "$?" "1"
eq "both default-host runs refused at the shim (only these 2 refusals)" "$(grep -c 'web@192.0.2.20' "$T/refused.log") $(wc -l < "$T/refused.log")" "2 2"
rm -f "$T/refused.log"; clean

echo "# help"
run; eq "no args: rc 0" "$rc" "0"
has "lists push" "$out" 'proxy push "message"'
has "lists cert" "$out" "proxy cert NAME"
hasnt "no code lines in the help" "$out" "host="
eq "help = the comment block, ends with the workflow line" "$(echo "$out" | tail -1 | cut -c1-20)" "Workflow: proxy pull"
eq "no ssh/rsync" "$(f ssh.log)$(f rsync.log)" ""
run bogus; eq "unknown command: rc 2 + help" "$rc|${out:0:24}" "2|Owner's nginx reverse pr"
PROXY_MIRROR=$T/nope run help; eq "help works without a mirror" "$rc" "0"

echo "# pull: initial mirror"
clean; run pull
eq "rc 0" "$rc" "0"
eq "files mirrored" "$(cat "$E/sites-available/emby.conf" 2>/dev/null)" 'server { server_name emby.example.org; }'
eq "symlink kept as a link" "$(readlink "$E/sites-enabled/emby.conf")" "../sites-available/emby.conf"
eq "dash-www excluded" "$([ -e "$E/dash-www" ] && echo there || echo absent)" "absent"
msg=$(g log -1 --format=%s)
[[ $msg =~ ^live\ state\ [0-9]{4}-[0-9]{2}-[0-9]{2}\ [0-9]{2}:[0-9]{2}:[0-9]{2}$ ]] && ok "commit 'live state DATE TIME'" || no "commit message" "$msg"
has "prints the commit" "$out" "live state"
eq "work tree clean" "$(g status --porcelain)" ""
eq "stash dir removed" "$(tmpleft)" "0"
eq "no ssh command (rsync only)" "$(f ssh.log)" ""
has "rsync from the VM with --delete --exclude=/dash-www" "$(f rsync.log)" "rsync -a --delete --exclude=/dash-www $FAKE_HOST:/etc/nginx/ "

echo "# pull: no drift"
clean; h0=$(g rev-parse HEAD); run pull
eq "rc 0, no new commit" "$rc|$(g rev-parse HEAD)" "0|$h0"

echo "# pull: drift on the VM (edit + delete) is committed"
clean; echo 'server { server_name emby.example.org; listen 443; }' > "$SV/sites-available/emby.conf"; rm "$SV/sites-available/old.conf"
run pull
eq "rc 0" "$rc" "0"
eq "edit mirrored" "$(cat "$E/sites-available/emby.conf")" 'server { server_name emby.example.org; listen 443; }'
eq "--delete: removed file gone from the mirror" "$([ -e "$E/sites-available/old.conf" ] && echo there || echo gone)" "gone"
eq "one commit with both" "$(g show --name-status --format= HEAD | sort | tr '\t\n' ' ')" "D etc-nginx/sites-available/old.conf M etc-nginx/sites-available/emby.conf "

echo "# pull refuses while local edits are pending"
clean; h0=$(g rev-parse HEAD); echo 'server { server_name drift.example.org; }' > "$SV/sites-available/drift.conf"
echo '# my edit' >> "$E/snippets/headers.conf"; run pull
eq "tracked edit: rc 1" "$rc" "1"
has "says why" "$err" "proxy: local edits pending"
eq "edit kept" "$(tail -1 "$E/snippets/headers.conf")" "# my edit"
eq "no commit" "$(g rev-parse HEAD)" "$h0"
eq "stash dir removed" "$(tmpleft)" "0"
g checkout -q -- etc-nginx
clean; echo 'server { server_name new.example.org; }' > "$E/sites-available/new.conf"; run pull
eq "NEW untracked file: rc 1 (was deleted by rsync --delete)" "$rc" "1"
eq "new file survives" "$(cat "$E/sites-available/new.conf" 2>/dev/null)" 'server { server_name new.example.org; }'
eq "no commit" "$(g rev-parse HEAD)" "$h0"
rm "$E/sites-available/new.conf"
clean; echo x > "$M/NOTES"; g add NOTES; run pull
eq "staged change outside etc-nginx: rc 1" "$rc" "1"
eq "not committed by pull" "$(g rev-parse HEAD)" "$h0"
g rm -q --cached NOTES; rm "$M/NOTES"
clean; touch "$FAKE/fail-from"; run pull
eq "VM unreachable: rc 1" "$rc" "1"
eq "no commit" "$(g rev-parse HEAD)" "$h0"
eq "stash dir removed" "$(tmpleft)" "0"
clean; run pull; eq "clean again: drift committed" "$rc|$(g show --name-only --format= HEAD)" "0|etc-nginx/sites-available/drift.conf"

echo "# push: nothing to push"
clean; run push "noop"
eq "rc 0 + message" "$rc|$out" "0|nothing to push"
eq "no rsync/ssh" "$(f rsync.log)$(f ssh.log)" ""
echo "# push: message required"
clean; echo '# x' >> "$E/nginx.conf"; h0=$(g rev-parse HEAD); run push
eq "rc != 0" "$([ "$rc" != 0 ] && echo yes)" "yes"
has "says message" "$err" "message"
eq "no commit, no copy" "$(g rev-parse HEAD)|$(f rsync.log)" "$h0|"
g checkout -q -- etc-nginx

echo "# push: happy path (edit + new file)"
clean; h0=$(g rev-parse HEAD)
echo 'server { server_name emby.example.org; listen 443 ssl; }' > "$E/sites-available/emby.conf"
echo 'server { server_name seerr.example.org; }' > "$E/sites-available/seerr.conf"
run push "add seerr"
eq "rc 0" "$rc" "0"
eq "stdout" "$out" "pushed + reloaded: add seerr"
eq "edit on the VM" "$(sv sites-available/emby.conf)" 'server { server_name emby.example.org; listen 443 ssl; }'
eq "new file on the VM" "$(sv sites-available/seerr.conf)" 'server { server_name seerr.example.org; }'
eq "dash-www on the VM untouched" "$(sv dash-www/index.html)" "<html>dash</html>"
eq "commit = message" "$(g log -1 --format=%s)" "add seerr"
eq "one new commit" "$(g rev-list --count "$h0"..HEAD)" "1"
eq "nginx -t then one reload" "$(cut -d' ' -f3- "$FAKE/ssh.log" | tr '\n' '|')" "sudo -n /usr/sbin/nginx -t|sudo -n /usr/bin/systemctl reload nginx|"
has "copy flags (no --delete)" "$(f rsync.log)" "rsync -rl --checksum --omit-dir-times --no-perms --no-times etc-nginx/ $FAKE_HOST:/etc/nginx/"
eq "toward the VM: one copy, no --delete" "$(grep -c " $FAKE_HOST:/etc/nginx/\$" "$FAKE/rsync.log") $(grep " $FAKE_HOST:/etc/nginx/\$" "$FAKE/rsync.log" | grep -c -e --delete)" "1 0"
has "drift check first: reads the VM (rsync from, dash-www excluded)" "$(head -1 "$FAKE/rsync.log")" "rsync -a --delete --exclude=/dash-www $FAKE_HOST:/etc/nginx/ "
eq "drift check: no ssh before nginx -t" "$(head -1 "$FAKE/ssh.log" | cut -d' ' -f3-)" "sudo -n /usr/sbin/nginx -t"
eq "temp dirs removed" "$(tmpleft)" "0"

echo "# push: a file deleted in the mirror stays on the VM (push deletes only what it added)"
clean; g rm -q "$E/sites-available/seerr.conf"; run push "drop seerr"
eq "rc 0" "$rc|$out" "0|pushed + reloaded: drop seerr"
eq "still on the VM" "$(sv sites-available/seerr.conf)" 'server { server_name seerr.example.org; }'
echo 'server { server_name seerr.example.org; }' > "$E/sites-available/seerr.conf"; g add -A; g commit -qm "seerr back (matches the VM)"

echo "# push: drift on the VM (certbot) is committed first and kept, then the push"
clean; h0=$(g rev-parse HEAD)
printf 'server { server_name emby.example.org; listen 443 ssl; }\nssl_certificate /etc/letsencrypt/live/emby/fullchain.pem;\n' > "$SV/sites-available/emby.conf"
echo 'only on the VM' > "$SV/snippets/vm-only.conf"
echo '# my edit' >> "$E/snippets/headers.conf"; echo 'server { server_name new.example.org; }' > "$E/sites-available/new.conf"
run push "edit headers"
eq "rc 0 + pushed" "$rc|$out" "0|pushed + reloaded: edit headers"
has "stderr: drift committed first" "$err" "proxy: the server had changed since the last pull; committed as drift first:"
has "names the drifted file" "$err" "etc-nginx/sites-available/emby.conf"
eq "two commits: drift, then the push" "$(g log -2 --format=%s | tr '\n' '|')" "edit headers|drift: live state before push|"
eq "drift commit = the VM's changes only" "$(g show --name-only --format= HEAD~1 | sort | tr '\n' ' ')" "etc-nginx/sites-available/emby.conf etc-nginx/snippets/vm-only.conf "
eq "push commit = my edits only" "$(g show --name-only --format= HEAD | sort | tr '\n' ' ')" "etc-nginx/sites-available/new.conf etc-nginx/snippets/headers.conf "
has "certbot's edit kept on the VM" "$(sv sites-available/emby.conf)" "ssl_certificate /etc/letsencrypt/live/emby/fullchain.pem;"
eq "VM-only file kept" "$(sv snippets/vm-only.conf)" "only on the VM"
eq "my edit on the VM" "$(tail -1 "$SV/snippets/headers.conf")" "# my edit"
eq "work tree clean, no stash left" "$(g status --porcelain)|$(g stash list)" "|"
eq "temp dirs removed" "$(tmpleft)" "0"

echo "# push: the VM and I changed the same file -> nothing pushed"
clean; h0=$(g rev-parse HEAD); echo '# vm edit' >> "$SV/snippets/headers.conf"; echo '# my second edit' >> "$E/snippets/headers.conf"
echo 'server { server_name other.example.org; }' > "$E/sites-available/other.conf"
run push "conflict"
eq "rc 1" "$rc" "1"
has "says why" "$err" "changed on the server since the last pull AND edited by you: nothing pushed"
has "names the file" "$err" "  etc-nginx/snippets/headers.conf"
eq "no commit" "$(g rev-parse HEAD)" "$h0"
eq "nothing copied to the VM, no ssh" "$(grep -c " $FAKE_HOST:/etc/nginx/\$" "$FAKE/rsync.log")|$(f ssh.log)" "0|"
eq "VM keeps its edit" "$(tail -1 "$SV/snippets/headers.conf")" "# vm edit"
eq "my edits still staged" "$(g status --porcelain | tr '\n' ' ')" "A  etc-nginx/sites-available/other.conf M  etc-nginx/snippets/headers.conf "
g reset -q; g checkout -q -- etc-nginx; rm "$E/sites-available/other.conf"; clean; run pull; eq "pull takes the VM edit" "$rc" "0"

echo "# push: the VM cannot be read -> nothing pushed"
clean; touch "$FAKE/fail-from"; h0=$(g rev-parse HEAD); echo '# x' >> "$E/nginx.conf"; run push "unreadable"
eq "rc 1 + says why" "$rc|$(echo "$err" | tail -1)" "1|proxy: cannot read the server's /etc/nginx: nothing pushed (edits kept)"
eq "no commit, no copy, no ssh" "$(g rev-parse HEAD)|$(grep -c " $FAKE_HOST:/etc/nginx/\$" "$FAKE/rsync.log")|$(f ssh.log)" "$h0|0|"
g reset -q; g checkout -q -- etc-nginx

echo "# push: nginx warnings still count as success"
clean; touch "$FAKE/nginx-warn"; echo '# w' >> "$E/nginx.conf"; run push "with warn"
eq "rc 0 + reloaded" "$rc|$out" "0|pushed + reloaded: with warn"

echo "# push: nginx -t fails -> rollback restores the VM, no reload"
clean; good=$(sv sites-available/emby.conf); h0=$(g rev-parse HEAD)
echo 'server { BROKEN; }' > "$E/sites-available/emby.conf"; run push "break emby"
eq "rc 1" "$rc" "1"
has "stderr: rolling back" "$err" "nginx -t FAILED; rolling back"
has "stderr shows nginx's error" "$err" 'unknown directive "BROKEN" in /etc/nginx/sites-available/emby.conf'
has "stderr: rolled back" "$err" "rolled back (config restored, not reloaded)"
hasnt "not reported incomplete" "$err" "ROLLBACK INCOMPLETE"
eq "VM file restored" "$(sv sites-available/emby.conf)" "$good"
eq "no reload" "$(cnt reloads.log)" "0"
eq "mirror: commit + revert" "$(g log -2 --format=%s | tr '\n' '|')" 'Revert "break emby"|break emby|'
eq "mirror file restored" "$(cat "$E/sites-available/emby.conf")" "$good"
eq "work tree clean" "$(g status --porcelain)" ""
eq "copy back = same flags (push + restore)" "$(grep -c "rsync -rl --checksum --omit-dir-times --no-perms --no-times .*etc-nginx/ $FAKE_HOST:/etc/nginx/\$" "$FAKE/rsync.log")" "2"
eq "nothing to remove: no --delete toward the VM" "$(grep " $FAKE_HOST:/etc/nginx/\$" "$FAKE/rsync.log" | grep -c -e --delete)" "0"
eq "nginx -t: check, show, verify" "$(cnt ssh.log 'nginx -t')" "3"
hasnt "stdout has no 'pushed'" "$out" "pushed"

echo "# push: nginx -t fails on NEW files -> the rollback removes exactly them (and a new directory)"
mkdir -p "$SV/conf.d" "$E/conf.d"; echo c > "$SV/conf.d/c.conf"; echo c > "$E/conf.d/c.conf"; g add -A; g commit -qm "conf.d (on both)"
clean; echo 'keep' > "$SV/snippets/vm-only.conf"   # appears on the VM: drift, committed before the push
echo 'BROKEN' > "$E/snippets/bad.conf"; mkdir -p "$E/conf.d/new/deep"; echo x > "$E/conf.d/new/deep/a.conf"
ln -s ../../snippets/headers.conf "$E/conf.d/new/link.conf"; run push "add bad snippet"
eq "rc 1" "$rc" "1"
has "stderr: rolled back" "$err" "rolled back (config restored, not reloaded)"
hasnt "not reported incomplete" "$err" "ROLLBACK INCOMPLETE"
eq "added file removed from the VM" "$(sv snippets/bad.conf)" "<absent>"
eq "new directory removed from the VM" "$([ -e "$SV/conf.d/new" ] && echo there || echo gone)" "gone"
eq "existing directory and its file kept" "$(sv conf.d/c.conf)" "c"
eq "VM-only file in the same directory kept" "$(sv snippets/vm-only.conf)" "keep"
eq "other files kept" "$(sv snippets/headers.conf | head -1)" "add_header X-Frame-Options DENY;"
eq "mirror reverted" "$([ -e "$E/snippets/bad.conf" ] && echo there || echo gone)" "gone"
eq "no reload" "$(cnt reloads.log)" "0"
has "removal = rsync --delete limited by a filter" "$(f rsync.log)" "rsync -rl --delete --omit-dir-times --no-perms --no-times --filter=merge "
eq "temp dirs removed" "$(tmpleft)" "0"
rm -f "$SV/snippets/vm-only.conf"; clean; run pull; eq "in sync again" "$rc" "0"

echo "# push: a renamed file whose new name breaks nginx -t -> the new name is removed"
clean; g mv etc-nginx/snippets/headers.conf etc-nginx/snippets/hdr.conf; echo BROKEN >> "$E/snippets/hdr.conf"; run push "rename"
eq "rc 1, rolled back" "$rc|$(echo "$err" | tail -1)" "1|rolled back (config restored, not reloaded)"
eq "new name gone, old name there" "$(sv snippets/hdr.conf)|$(sv snippets/headers.conf | head -1)" "<absent>|add_header X-Frame-Options DENY;"

echo "# push: an added name that no filter rule can name exactly -> not removed, reported"
clean; echo BROKEN > "$E/snippets/we[i]rd*.conf"; run push "odd name"
eq "rc 1" "$rc" "1"
has "stderr: ROLLBACK INCOMPLETE" "$err" "proxy: ROLLBACK INCOMPLETE"
has "names the leftover file" "$err" "etc-nginx/snippets/we[i]rd*.conf"
eq "left on the VM, nothing else deleted" "$(sv 'snippets/we[i]rd*.conf')|$(sv snippets/headers.conf | head -1)" "BROKEN|add_header X-Frame-Options DENY;"
rm -f "$SV/snippets/we[i]rd*.conf"

echo "# push: nginx -t fails and the copy back fails -> rollback incomplete"
clean; echo 2 > "$FAKE/fail-to"; echo 'server { BROKEN 2; }' > "$E/sites-available/emby.conf"; run push "break again"
eq "rc 1" "$rc" "1"
has "stderr: ROLLBACK INCOMPLETE" "$err" "proxy: ROLLBACK INCOMPLETE"
eq "VM still broken (copy back failed)" "$(sv sites-available/emby.conf)" "server { BROKEN 2; }"
eq "no reload" "$(cnt reloads.log)" "0"
echo "$good" > "$SV/sites-available/emby.conf"

echo "# push: reload fails -> rolled back to the running config, not reloaded again"
clean; before=$(sv sites-available/emby.conf); touch "$FAKE/reload-fail"; echo 'server { server_name emby.example.org; listen 8443; }' > "$E/sites-available/emby.conf"
echo 'server { server_name added.example.org; }' > "$E/sites-available/added.conf"; run push "port 8443"
eq "rc 1" "$rc" "1"
has "stderr: reload FAILED, rolling back" "$err" "proxy: nginx -t passed but the reload FAILED; rolling back"
has "stderr: rolled back" "$err" "rolled back (config restored, not reloaded)"
hasnt "no 'pushed + reloaded'" "$out" "pushed + reloaded"
eq "old config back on the VM" "$(sv sites-available/emby.conf)" "$before"
eq "added file removed" "$(sv sites-available/added.conf)" "<absent>"
eq "mirror: commit + revert" "$(g log -2 --format=%s | tr '\n' '|')" 'Revert "port 8443"|port 8443|'
eq "nginx -t twice (check, verify), one reload attempt; copy, restore, removal" "$(cnt ssh.log 'nginx -t') $(cnt ssh.log reload) $(grep -c " $FAKE_HOST:/etc/nginx/\$" "$FAKE/rsync.log")" "2 1 3"

echo "# push: the copy to the VM fails -> commit undone, retry works"
clean; echo 1 > "$FAKE/fail-to"; h0=$(g rev-parse HEAD); echo '# retry me' >> "$E/nginx.conf"; run push "retry"
eq "rc 1" "$rc" "1"
has "stderr: copy failed, server restored, commit undone" "$err" "proxy: copy to the server FAILED; the server has its old files again (nginx -t ok, not reloaded). Local commit undone"
eq "HEAD unchanged" "$(g rev-parse HEAD)" "$h0"
eq "edit still pending" "$(g status --porcelain)" "M  etc-nginx/nginx.conf"
eq "restore verified with nginx -t, no reload" "$(cut -d' ' -f3- "$FAKE/ssh.log" | tr '\n' '|')" "sudo -n /usr/sbin/nginx -t|"
clean; run push "retry"
eq "retry: rc 0 + pushed (not 'nothing to push')" "$rc|$out" "0|pushed + reloaded: retry"
eq "retry: on the VM" "$(tail -1 "$SV/nginx.conf")" "# retry me"

echo "# push: the copy arrives but fails at the end -> server restored, added file removed"
clean; echo 1 > "$FAKE/partial-to"; h0=$(g rev-parse HEAD); before=$(sv nginx.conf)
echo '# half' >> "$E/nginx.conf"; echo 'server { server_name half.example.org; }' > "$E/sites-available/half.conf"; run push "half"
eq "rc 1" "$rc" "1"
has "stderr: server has its old files again" "$err" "the server has its old files again"
eq "VM: old nginx.conf, no half.conf" "$(sv nginx.conf)|$(sv sites-available/half.conf)" "$before|<absent>"
eq "HEAD unchanged, edits staged" "$(g rev-parse HEAD)|$(g status --porcelain | tr '\n' ' ')" "$h0|M  etc-nginx/nginx.conf A  etc-nginx/sites-available/half.conf "
clean; echo 1 > "$FAKE/fail-to"; echo 2 >> "$FAKE/fail-to"; run push "half"
eq "copy and restore both fail: rc 1, restore did not finish + leftovers listed" "$rc|$(echo "$err" | grep -c 'restore did not finish')|$(echo "$err" | grep -c 'etc-nginx/sites-available/half.conf')" "1|1|1"
g reset -q; g checkout -q -- etc-nginx; rm -f "$E/sites-available/half.conf"

echo "# test"
clean; run test; eq "passes nginx -t through (rc 0, stderr)" "$rc|$(echo "$err" | tail -1)" "0|nginx: configuration file /etc/nginx/nginx.conf test is successful"
clean; touch "$FAKE/ssh-down"; run test; eq "ssh down: rc 255" "$rc" "255"

echo "# cert"
clean; run cert seerr
eq "rc 0" "$rc" "0"
eq "NAME gets .example.org" "$(f certs.log)" "seerr.example.org"
eq "then pulls with 'certbot: NAME'" "$(g log -1 --format=%s)" "certbot: seerr.example.org"
has "cert config mirrored" "$(cat "$E/sites-available/seerr.example.org.conf" 2>/dev/null)" "ssl_certificate /etc/letsencrypt/live/seerr.example.org/"
clean; run cert example.org; eq "apex kept" "$(f certs.log)" "example.org"
clean; run cert x.example.org; eq "FQDN kept" "$(f certs.log)" "x.example.org"
for bad in 'x; touch /tmp/pwned' '$(id)' 'a b' '-rf' '../etc' 'x`id`' 'x|y' '.example.org' 'a..b'; do
  clean; run cert "$bad"; eq "refused: '$bad' (rc 2, no ssh)" "$rc|$(f ssh.log)" "2|"
done
clean; echo 1 > "$FAKE/cert-rc"; h0=$(g rev-parse HEAD); run cert fail
eq "cert fails: rc 1, no pull" "$rc|$(f rsync.log)|$(g rev-parse HEAD)" "1||$h0"

echo "# log"
clean; run log 2; eq "two lines" "$(echo "$out" | wc -l)" "2"
has "newest first" "$(echo "$out" | head -1)" "certbot: x.example.org"

echo "# missing mirror"
clean; PROXY_MIRROR=$T/nope run pull
eq "rc 1, no ssh/rsync" "$rc|$(f ssh.log)$(f rsync.log)" "1|"
eq "nothing written to the cwd" "$(ls -A "$T/cwd")" ""

echo "# defaults (static: never run against the VM)"
src=$(cat "$P")
has "default host: the profile's network.hosts.web_vm" "$src" 'host=${PROXY_HOST:-$("$pg" network.hosts.web_vm)}'
has "default mirror" "$src" 'm=${PROXY_MIRROR:-$HOME/opt/nginx-mirror}'
has "nginx -t via sudo -n" "$src" "'sudo -n /usr/sbin/nginx -t'"
has "reload via sudo -n" "$src" "'sudo -n /usr/bin/systemctl reload nginx'"
has "cert via sudo -n" "$src" '"sudo -n /usr/local/sbin/claude-cert $n"'

echo "# profile unreadable"
echo '{' > "$T/broken.json"
clean; ASSISTANT_PROFILE=$T/broken.json run cert seerr; eq "cert: rc 1, no ssh" "$rc|$(f ssh.log)" "1|"
clean; ASSISTANT_PROFILE=$T/broken.json env -u PROXY_HOST "$P" test >/dev/null 2>&1; rc=$?
eq "default host: rc 1, no ssh" "$rc|$(f ssh.log)" "1|"

echo "# isolation"
eq "no shim refused a call" "$(cat "$T/refused.log" 2>/dev/null)" ""
eq "real mirror untouched (HEAD + status)" "$(real_state)" "$real_before"

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
