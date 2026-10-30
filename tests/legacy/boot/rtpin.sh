#!/bin/bash
# Sandbox test of the runtime-pin block of boot.sh.new: paths rewritten into a temp root, sudo = logged pass-through,
# dpkg-divert emulated (--rename moves the file). Uses the real pinned binaries in ~/opt/runtimes as sources.
set -u
NEW=${BOOT_SCRIPT:-$HOME/boot.sh}   # BOOT_SCRIPT=<path> tests another copy
T=$(mktemp -d /tmp/claude-1000/rtpin.XXXXXX 2>/dev/null || mktemp -d)
pass=0; fail=0
ck() { if eval "$2"; then pass=$((pass+1)); echo "ok   $1"; else fail=$((fail+1)); echo "FAIL $1"; fi; }
mkdir -p "$T/shim"
cat > "$T/shim/sudo" <<'S'
#!/bin/bash
echo "sudo $*" >> "$LOGF"
if [ "$1" = dpkg-divert ]; then
  args=("$@"); div=; f=; for ((i=1;i<${#args[@]};i++)); do case "${args[$i]}" in --divert) div=${args[$((i+1))]};; --add) f=${args[$((i+1))]};; esac; done
  mv "$f" "$div"; exit 0
fi
exec "$@"
S
chmod 755 "$T/shim/sudo"
block() {  # extract + rewrite the block for scenario dir $S
  awk '/^rt=(\/home\/node|\$HOME)\/opt\/runtimes/{p=1} p{print} /node.debian; fi$/{exit}' "$NEW" |
    sed -e "s#/home/node#$H#g" -e "s#/usr/local/bin/#$R/usr/local/bin/#g" -e "s#/usr/bin/node#$R/usr/bin/node#g" > "$S/block.sh"
}
scen() {  # scen NAME NODEVER BUNVER
  S=$T/$1; H=$S/home; R=$S/root; mkdir -p "$H/opt" "$R/usr/local/bin" "$R/usr/bin"
  ln -s /home/node/opt/runtimes "$H/opt/runtimes"
  printf '#!/bin/sh\necho v%s\n' "$2" > "$R/usr/local/bin/node"; printf '#!/bin/sh\necho %s\n' "$3" > "$R/usr/local/bin/bun"
  chmod 755 "$R/usr/local/bin/node" "$R/usr/local/bin/bun"; printf 'debian node 18 stub\n' > "$R/usr/bin/node"
  block; LOGF=$S/sudo.log; : > "$LOGF"
}
run() { HOME="$H" PATH="$T/shim:/usr/bin:/bin" LOGF="$LOGF" sh "$S/block.sh" > "$S/out" 2>&1; echo $?; }

scen older 24.21.0 1.4.2
ck "older: exit 0" '[ "$(run)" = 0 ]'
ck "older: no output" '[ ! -s "$S/out" ] || { cat "$S/out"; false; }'
ck "older: node is the pinned binary" 'cmp -s "$R/usr/local/bin/node" /home/node/opt/runtimes/node-v24.22.1-linux-x64/bin/node'
ck "older: bun is the pinned binary" 'cmp -s "$R/usr/local/bin/bun" /home/node/opt/runtimes/bun-1.4.4/bun-linux-x64/bun'
ck "older: image node kept" 'grep -q "echo v24.21.0" "$H/.local/state/image-orig/runtimes/node-24.21.0"'
ck "older: image bun kept" 'grep -q "echo 1.4.2" "$H/.local/state/image-orig/runtimes/bun-1.4.2"'
ck "older: /usr/bin/node -> pinned node" '[ "$(readlink "$R/usr/bin/node")" = "$R/usr/local/bin/node" ] && [ "$("$R/usr/bin/node" --version)" = v24.22.1 ]'
ck "older: diverted node 18 removed" '[ ! -e "$R/usr/bin/node.debian" ]'
ck "older: 5 sudo calls (2 installs, divert, ln, rm)" '[ "$(wc -l < "$LOGF")" = 5 ] || cat "$LOGF"'
: > "$LOGF"
ck "rerun: exit 0" '[ "$(run)" = 0 ]'
ck "rerun: no sudo calls" '[ ! -s "$LOGF" ] || { cat "$LOGF"; false; }'

scen newer 24.23.0 1.4.5
ck "newer image: exit 0" '[ "$(run)" = 0 ]'
ck "newer image: node kept" 'grep -q "echo v24.23.0" "$R/usr/local/bin/node"'
ck "newer image: bun kept" 'grep -q "echo 1.4.5" "$R/usr/local/bin/bun"'
ck "newer image: only the divert calls" '[ "$(grep -c "install" "$LOGF")" = 0 ]'

scen badsha 24.21.0 1.4.2
rm "$H/opt/runtimes"; mkdir -p "$H/opt/runtimes/node-v24.22.1-linux-x64/bin" "$H/opt/runtimes/bun-1.4.4/bun-linux-x64"
printf 'tampered\n' > "$H/opt/runtimes/node-v24.22.1-linux-x64/bin/node"; printf 'tampered\n' > "$H/opt/runtimes/bun-1.4.4/bun-linux-x64/bun"
ck "bad sha: exit 0" '[ "$(run)" = 0 ]'
ck "bad sha: nothing installed" '[ "$(grep -c "sudo install" "$LOGF")" = 0 ] && grep -q "echo v24.21.0" "$R/usr/local/bin/node"'

scen missing 24.21.0 1.4.2
rm "$H/opt/runtimes"
ck "no pinned files: exit 0, nothing installed" '[ "$(run)" = 0 ] && [ "$(grep -c "sudo install" "$LOGF")" = 0 ]'

rm -rf "${T:?}"
echo "blocktest: $pass passed, $fail failed"; [ "$fail" = 0 ]
