#!/usr/bin/env bash
# Offline tests for ~/bin/unraid-do (the owner-OK-gated client for the Unraid dispatcher). Never contacts the
# server: UNRAID_DO_SSH is a recording stub, `ssh`/`unraid-ssh` on PATH are refusing shims, `date` is pinned.
# Covers: every gated verb refuses without --owner-ok (and with too-short words); the owner-ok log (0600, dir 0700,
# one JSON line per sent gated call); deterministic references; --dry-run sends and logs nothing; usage errors and
# their exit codes; VM names with spaces; local inputs (--file, a terminal stdin) checked before an OK is logged; the stub's exit code passes through. The dispatcher side and the end-to-end
# path are in ~/opt/unraid-lockdown-tests (rights_test.sh). UNRAID_DO_BIN=<path> tests another copy.
set -u
UD=${UNRAID_DO_BIN:-/home/node/bin/unraid-do}
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
mkdir -p "$T/stubs" "$T/shims"
TS=2026-01-02T03:04:05+0100
cat > "$T/stubs/rec-ssh" <<'S'
#!/bin/bash
# records argv (one per line, then ---) and stdin; exit code from $T/ssh-rc. A server older than 3.5: no
# owner-ok-review verb, so a gated call gets the words' reference (the path these cases cover; the 3.5 tap is not)
if [ "${1:-} ${2:-}" = "run owner-ok-review" ]; then echo "unraid-ssh: unknown verb 'owner-ok-review'" >&2; exit 2; fi
{ printf '%s\n' "$@"; echo ---; } >> "$UDT/calls"; cat > "$UDT/stdin"
exit "$(cat "$UDT/ssh-rc" 2>/dev/null || echo 0)"
S
printf '#!/bin/bash\ncase ${1:-} in +%%Y*) echo %s ;; *) exec /bin/date "$@" ;; esac\n' "$TS" > "$T/shims/date"
for s in ssh unraid-ssh; do printf '#!/bin/bash\necho "shim %s: refusing" >> "%s/refused"; exit 99\n' "$s" "$T" > "$T/shims/$s"; done
chmod +x "$T/stubs/rec-ssh" "$T/shims/"*
export UDT=$T PATH=$T/shims:$PATH UNRAID_DO_SSH=$T/stubs/rec-ssh UNRAID_DO_STATE=$T/state
LOG=$T/state/owner-ok.jsonl

pass=0; fail=0
t() { local d=$1; shift; if "$@" >/dev/null 2>&1; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $d"; fi; }
udo() {  # udo ARG... -> OUT ERR RC; calls in $T/calls
  : > "$T/calls"; : > "$T/stdin"
  OUT=$(bash "$UD" "$@" < "${UDO_IN:-/dev/null}" 2> "$T/err"); RC=$?; ERR=$(cat "$T/err")
}
sent() { tr '\n' ' ' < "$T/calls"; }
nolog() { [ ! -s "$LOG" ]; }
expref() {  # expref "REQUEST WORDS" "his words" -> ok-<12 hex> as unraid-do computes it
  python3 -I -c 'import hashlib,sys; print("ok-" + hashlib.sha256(("%s\n%s\n%s\n" % tuple(sys.argv[1:4])).encode()).hexdigest()[:12])' "$TS" "$1" "$2"
}

# ---- 1. every gated verb refuses without --owner-ok: exit 2, nothing sent, nothing logged
GATED=("docker-update sonarr" "vm-create t9 2 4 16 none" "userscript-run hello-world" "appdata-put sonarr config.xml"
       "appdata-restore sonarr 20260101-000000-1" "plugin-update community.applications")
for g in "${GATED[@]}"; do
  read -r -a ga <<< "$g"
  udo "${ga[@]}"
  t "$g without --owner-ok: exit 2, the OK message, nothing sent or logged" \
    bash -c '[ "$1" = 2 ] && [[ $2 == *"needs the owner'"'"'s OK"* ]] && [ ! -s "$3" ] && [ ! -s "$4" ]' _ "$RC" "$ERR" "$T/calls" "$LOG"
  for w in "" "x" "   " "$(printf ' \t ')"; do
    udo "${ga[@]}" --owner-ok "$w"
    t "$g with words '${w//[[:space:]]/_}' (under 2 chars after trimming): refused" bash -c '[ "$1" = 2 ] && [ ! -s "$2" ]' _ "$RC" "$T/calls"
  done
done
for v in "Home Assistant" "homeassistant" "HASS" "haos-vm" "Pi-Hole" "pihole" "PI HOLE"; do
  udo vm-stop "$v"
  t "vm-stop '$v' without OK: refused as critical" bash -c '[ "$1" = 2 ] && [[ $2 == *critical* ]] && [ ! -s "$3" ]' _ "$RC" "$ERR" "$T/calls"
done
udo vm-stop testvm --force
t "vm-stop --force without OK: refused (hard power-off)" bash -c '[ "$1" = 2 ] && [[ $2 == *"hard power-off"* ]] && [ ! -s "$3" ]' _ "$RC" "$ERR" "$T/calls"
t "nothing logged by any refusal" nolog
for v in "docker-restart sonarr" "vm-start testvm" "vm-stop testvm" "docker-list" "vm-list" "rights-log 5" "iso-list"; do
  read -r -a va <<< "$v"; udo "${va[@]}"
  t "$v: ungated, sent without a reference" bash -c '[ "$1" = 0 ] && [ "$2" = "run $3 --- " ]' _ "$RC" "$(sent)" "$v"
done
t "ungated calls log nothing" nolog

# ---- 2. with his OK: sent with the reference; the log is 0600 in a 0700 dir, one JSON line per call
n=0
for g in "${GATED[@]}"; do
  read -r -a ga <<< "$g"
  req=$g; [ "${ga[0]}" = appdata-put ] && req="appdata-put sonarr config.xml"
  UDO_IN=/dev/null udo "${ga[@]}" --owner-ok "ja, mach das"; n=$((n+1))
  r=$(expref "$req" "ja, mach das")
  t "$g with OK: exit 0, sent as 'run $req $r'" bash -c '[ "$1" = 0 ] && [ "$2" = "run $3 $4 --- " ]' _ "$RC" "$(sent)" "$req" "$r"
done
t "owner-ok log: mode 600" [ "$(stat -c %a "$LOG")" = 600 ]
t "state dir: mode 700" [ "$(stat -c %a "$T/state")" = 700 ]
t "owner-ok log: one line per gated call ($n)" [ "$(wc -l < "$LOG")" = "$n" ]
t "owner-ok log: every line is JSON with ts, ref, request, owner_ok" python3 -I -c '
import json, sys
for ln in open(sys.argv[1]):
    d = json.loads(ln); assert set(d) == {"ts", "ref", "request", "owner_ok"} and d["ts"] == sys.argv[2] and d["owner_ok"] == "ja, mach das"' "$LOG" "$TS"
chmod 644 "$LOG"; (umask 000; udo docker-update radarr --owner-ok "ok, go")
t "a log that drifted to 644 is put back to 600 (umask 000)" [ "$(stat -c %a "$LOG")" = 600 ]
rm -rf "$T/state"; (umask 000; udo plugin-update x.y --owner-ok "ok, go")
t "a fresh log under umask 000 is 600, its dir 700" [ "$(stat -c %a "$LOG")/$(stat -c %a "$T/state")" = 600/700 ]
udo --owner-ok "ok" docker-list
t "read verbs never get a reference, even with --owner-ok" bash -c '[ "$1" = "run docker-list --- " ] && [ "$(wc -l < "$2")" = 1 ]' _ "$(sent)" "$LOG"
udo docker-restart sonarr --owner-ok "ja bitte"
t "a logged ungated verb with OK gets a reference too" [ "$(sent)" = "run docker-restart sonarr $(expref "docker-restart sonarr" "ja bitte") --- " ]

# ---- 3. references are deterministic: same time + request + words -> same ref; any change -> another
udo docker-update sonarr --owner-ok "ja, mach das"; r1=$(sed -n 4p "$T/calls")
udo docker-update sonarr --owner-ok "ja, mach das"; r2=$(sed -n 4p "$T/calls")
udo docker-update sonarr --owner-ok "  ja,   mach$(printf '\t')das  "; r3=$(sed -n 4p "$T/calls")
udo docker-update radarr --owner-ok "ja, mach das"; r4=$(sed -n 4p "$T/calls")
udo docker-update sonarr --owner-ok "ja, mach es"; r5=$(sed -n 4p "$T/calls")
t "reference format ok-<12 hex>" bash -c '[[ $1 =~ ^ok-[0-9a-f]{12}$ ]]' _ "$r1"
t "same input twice: same reference" [ "$r1" = "$r2" ]
t "reference = sha256(ts, request, words) (independent recomputation)" [ "$r1" = "$(expref "docker-update sonarr" "ja, mach das")" ]
t "his words are whitespace-normalized before hashing and logging" bash -c '[ "$1" = "$2" ] && [ "$(tail -3 "$3" | head -1 | python3 -I -c "import json,sys; print(json.load(sys.stdin)[\"owner_ok\"])" 2>/dev/null)" = "ja, mach das" ]' _ "$r1" "$r3" "$LOG"
t "another request: another reference" [ "$r4" != "$r1" ]
t "other words: another reference" [ "$r5" != "$r1" ]
t "the logged reference is the sent one" bash -c 'tail -1 "$1" | grep -qF "\"ref\": \"$2\""' _ "$LOG" "$r5"
r6=$(PATH=$T/shims2:$PATH bash -c 'mkdir -p "$1/shims2"; printf "#!/bin/bash\necho 2026-01-02T03:04:06+0100\n" > "$1/shims2/date"; chmod +x "$1/shims2/date"; UNRAID_DO_STATE=$1/state2 bash "$2" --dry-run docker-update sonarr --owner-ok "ja, mach das"' _ "$T" "$UD" | sed -n 's/^would send: .* //p')
t "another second: another reference" bash -c '[[ $1 =~ ^ok-[0-9a-f]{12}$ ]] && [ "$1" != "$2" ]' _ "$r6" "$r1"

# ---- 4. --dry-run: prints the request, sends nothing, logs nothing
n0=$(wc -l < "$LOG")
udo --dry-run --owner-ok "ja" plugin-update community.applications
t "--dry-run gated: exit 0, prints the request with its reference and his OK" bash -c '[ "$1" = 0 ] && [ "$2" = "would send: plugin-update community.applications $3
owner'"'"'s OK (yes): ja" ]' _ "$RC" "$OUT" "$(expref "plugin-update community.applications" "ja")"
t "--dry-run: nothing sent, nothing logged" bash -c '[ ! -s "$1" ] && [ "$(wc -l < "$2")" = "$3" ]' _ "$T/calls" "$LOG" "$n0"
udo vm-list --dry-run
t "--dry-run read verb: 'would send: vm-list', nothing sent" bash -c '[ "$1" = 0 ] && [ "$2" = "would send: vm-list" ] && [ ! -s "$3" ]' _ "$RC" "$OUT" "$T/calls"
udo --dry-run docker-update sonarr
t "--dry-run does not bypass the gate (exit 2)" bash -c '[ "$1" = 2 ] && [ ! -s "$2" ]' _ "$RC" "$T/calls"
rm -rf "$T/state3"; UNRAID_DO_STATE=$T/state3 udo --dry-run vm-stop "Home Assistant" --owner-ok "yes, stop HA"
t "--dry-run creates no state dir" bash -c '[ "$1" = 0 ] && [ ! -e "$2" ] && [[ $3 == "would send: vm-stop Home Assistant --confirm ok-"* ]]' _ "$RC" "$T/state3" "$OUT"

# ---- 5. usage errors and exit codes
udo; t "no args: exit 2, usage on stderr" bash -c '[ "$1" = 2 ] && [[ $2 == *"Usage: unraid-do"* ]] && [ -z "$3" ]' _ "$RC" "$ERR" "$OUT"
for h in --help -h help; do udo $h; t "$h: exit 0, usage on stdout" bash -c '[ "$1" = 0 ] && [[ $2 == *"Usage: unraid-do"* ]]' _ "$RC" "$OUT"; done
U=("--bogus docker-list|unknown option" "frobnicate|unknown verb" "docker-update sonarr --owner-ok|needs his words"
   "appdata-put sonarr a.conf --file|needs a path" "docker-list --file /dev/null|only for appdata-put"
   "docker-restart sonarr --force|only for vm-stop" "vm-start testvm --force|takes no --force" "vm-start|needs the VM's name"
   "vm-stop|needs the VM's name" "appdata-get sonarr|takes CONTAINER PATH" "appdata-get sonarr /etc/passwd|relative"
   "appdata-get sonarr a//b|relative" "appdata-get sonarr dir/|relative" "docker-restart sonarr;id|characters the server refuses"
   "vm-start ../x|"
   "docker-restart --confirm|--confirm is added" "iso-fetch http://x/y.iso 00|URL must be" "iso-fetch|takes URL SHA256")
for u in "${U[@]}"; do
  args=${u%%|*} want=${u#*|}; read -r -a aa <<< "$args"
  udo "${aa[@]}"
  t "'$args': exit 2${want:+, \"$want\"}, nothing sent" bash -c '[ "$1" = 2 ] && [[ $2 == *"$3"* ]] && [ ! -s "$4" ]' _ "$RC" "$ERR" "$want" "$T/calls"
done
udo vm-start "$(printf 'w%.0s' $(seq 1 200))"
t "request over 200 characters: exit 2" bash -c '[ "$1" = 2 ] && [[ $2 == *"longer than 200"* ]] && [ ! -s "$3" ]' _ "$RC" "$ERR" "$T/calls"
udo appdata-put sonarr a.conf --owner-ok "ja" < /dev/null
t "appdata-put from /dev/null (not a terminal): sent" bash -c '[ "$1" = 0 ] && [[ $2 == "run appdata-put sonarr a.conf ok-"* ]]' _ "$RC" "$(sent)"
echo 7 > "$T/ssh-rc"; udo docker-list
t "unraid-ssh's exit code passes through (7)" [ "$RC" = 7 ]
rm -f "$T/ssh-rc"

# ---- 6. VM names with spaces
udo vm-start "Windows 11"; t "vm-start 'Windows 11' (one argument): sent as words" [ "$(sent)" = "run vm-start Windows 11 --- " ]
udo vm-start Windows 11;   t "vm-start Windows 11 (two words): same request" [ "$(sent)" = "run vm-start Windows 11 --- " ]
udo vm-start "  Windows   11 "; t "extra spaces collapse to single words" [ "$(sent)" = "run vm-start Windows 11 --- " ]
udo vm-stop "Ubuntu Server 24"; t "vm-stop with spaces, not critical: ungated" bash -c '[ "$1" = 0 ] && [ "$2" = "run vm-stop Ubuntu Server 24 --- " ]' _ "$RC" "$(sent)"
udo vm-stop "my test vm" --force --owner-ok "ok, force it"
t "vm-stop 'my test vm' --force with OK: words + --force + reference" [ "$(sent)" = "run vm-stop my test vm --force $(expref "vm-stop my test vm --force" "ok, force it") --- " ]
udo vm-stop Home Assistant --owner-ok "yes, stop HA"
t "vm-stop Home Assistant (two words) with OK: --confirm added" [ "$(sent)" = "run vm-stop Home Assistant --confirm $(expref "vm-stop Home Assistant --confirm" "yes, stop HA") --- " ]
t "the log keeps the VM name with its space" bash -c 'tail -1 "$1" | grep -qF "\"request\": \"vm-stop Home Assistant --confirm\""' _ "$LOG"
udo vm-start "Win 11;reboot"; t "a VM name word with a refused character: exit 2, nothing sent" bash -c '[ "$1" = 2 ] && [ ! -s "$2" ]' _ "$RC" "$T/calls"

# ---- 7. local inputs are checked before his OK is logged: a refused or failed call leaves no OK entry, sends nothing
lc() { if [ -f "$LOG" ]; then wc -l < "$LOG"; else echo 0; fi; }
n0=$(lc); udo appdata-put sonarr a.conf --owner-ok "ja, mach das" --file "$T/missing.conf"
t "appdata-put --file MISSING with OK: exit 2, 'no file', nothing sent, no OK entry" \
  bash -c '[ "$1" = 2 ] && [[ $2 == *"no file"* ]] && [ ! -s "$3" ] && [ "$4" = "$5" ]' _ "$RC" "$ERR" "$T/calls" "$n0" "$(lc)"
echo secret-ish > "$T/unreadable.conf"; chmod 000 "$T/unreadable.conf"
n0=$(lc); udo appdata-put sonarr a.conf --owner-ok "ja, mach das" --file "$T/unreadable.conf"
t "appdata-put --file UNREADABLE with OK: exit 2, 'cannot read', nothing sent, no OK entry" \
  bash -c '[ "$1" = 2 ] && [[ $2 == *"cannot read"* ]] && [ ! -s "$3" ] && [ "$4" = "$5" ]' _ "$RC" "$ERR" "$T/calls" "$n0" "$(lc)"
chmod 600 "$T/unreadable.conf"
mkdir -p "$T/adir"; n0=$(lc); udo appdata-put sonarr a.conf --owner-ok "ja, mach das" --file "$T/adir"
t "appdata-put --file DIRECTORY with OK: exit 2, nothing sent, no OK entry" \
  bash -c '[ "$1" = 2 ] && [ ! -s "$2" ] && [ "$3" = "$4" ]' _ "$RC" "$T/calls" "$n0" "$(lc)"
n0=$(lc); udo --dry-run appdata-put sonarr a.conf --owner-ok "ja, mach das" --file "$T/missing.conf"
t "--dry-run appdata-put --file MISSING: exit 2 (a dry run reports it too), nothing logged" \
  bash -c '[ "$1" = 2 ] && [ ! -s "$2" ] && [ "$3" = "$4" ]' _ "$RC" "$T/calls" "$n0" "$(lc)"
n0=$(lc); : > "$T/calls"
timeout 30 script -qec "bash '$UD' appdata-put sonarr a.conf --owner-ok 'ja, mach das'" /dev/null < /dev/null > "$T/tty-out" 2>&1; RC=$?
t "appdata-put with a terminal on stdin (no content) and OK: exit 2, nothing sent, no OK entry" \
  bash -c '[ "$1" = 2 ] && grep -q "reads the new content" "$2" && [ ! -s "$3" ] && [ "$4" = "$5" ]' _ "$RC" "$T/tty-out" "$T/calls" "$n0" "$(lc)"
printf 'a=1\n' > "$T/good.conf"; n0=$(lc); udo appdata-put sonarr a.conf --owner-ok "ja, mach das" --file "$T/good.conf"
t "appdata-put --file GOOD with OK: sent, the file's content on stdin, one OK entry" \
  bash -c '[ "$1" = 0 ] && [[ $2 == "run appdata-put sonarr a.conf ok-"* ]] && [ "$(cat "$3")" = "a=1" ] && [ "$5" = $(($4 + 1)) ]' _ "$RC" "$(sent)" "$T/stdin" "$n0" "$(lc)"
n0=$(lc); udo appdata-put sonarr a.conf --owner-ok "ja, mach das" < /dev/null
t "appdata-put with an empty stdin (not a terminal) is not a refusal: sent, one OK entry (the server backs up the old file)" \
  bash -c '[ "$1" = 0 ] && [ ! -s "$2" ] && [ "$4" = $(($3 + 1)) ]' _ "$RC" "$T/stdin" "$n0" "$(lc)"

# ---- isolation + drift
t "no refusing shim was hit (never ssh / unraid-ssh from PATH)" [ ! -e "$T/refused" ]
P=/home/node/opt/unraid-lockdown/client/unraid-do
if [ -z "${UNRAID_DO_BIN:-}" ] && [ -f "$P" ]; then t "~/bin/unraid-do is the lockdown package's client copy" cmp -s "$UD" "$P"; fi

echo "unraid-do tests: $pass passed, $fail failed"
[ "$fail" = 0 ]
