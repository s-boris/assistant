#!/usr/bin/env bash
# Tests for ~/bin/preclear-watch. Exit 0 = all pass.
# Server-independent: runs a copy of the script with PATH pointing at a fake `unraid-ssh` that prints
# a fixture health output (built below), and HOME pointing at a fresh temp dir per case (state, inbox,
# config). Placeholder serials only. PW_SCRIPT=/path tests another copy (default ~/bin/preclear-watch).
set -uo pipefail
SRC=${PW_SCRIPT:-$(cd "$(dirname "$0")/../../../scripts" && pwd)/preclear-watch}
T=$(mktemp -d "${TMPDIR:-/tmp}/preclear-watch-tests.XXXXXX")
trap 'find "${T:?}" -mindepth 1 -delete; rmdir "${T:?}"' EXIT
pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && echo "       $2"; }
SUF=TEST1234

cp "$SRC" "$T/preclear-watch"
mkdir -p "$T/fakebin"
cat > "$T/fakebin/unraid-ssh" <<'EOF'
#!/bin/sh
# fake: only `run health`; prints $PW_FIXTURE, or fails with $PW_FAKE_RC
[ "$1 $2" = "run health" ] || { echo "fake unraid-ssh: unexpected args: $*" >&2; exit 9; }
[ -n "${PW_FAKE_RC:-}" ] && { echo "boom" >&2; exit "$PW_FAKE_RC"; }
cat "$PW_FIXTURE"
EOF
chmod +x "$T/fakebin/unraid-ssh"

# fx FILE key=val...  keys: run (0/1) realloc pending unc crc health gone (1 = no dev block)
#                      nosmart (1 = no smartctl section for the disk) slot (dev1 or disk12) other_pc (1 = another disk in preclear)
fx() {
  local f=$1; shift
  local run=0 realloc=0 pending=0 unc=0 crc=0 health=PASSED gone=0 nosmart=0 slot=dev1 other_pc=0 kv
  for kv in "$@"; do local "$kv"; done
  local pc=""
  [ "$run" = 1 ] && pc="/dev/sdx "
  [ "$other_pc" = 1 ] && pc="${pc}/dev/sdy "
  {
    echo "=====UH meta"
    echo "host=fakehost"
    echo "preclear=$pc"
    echo "=====UH disks"
    printf '["disk1"]\nidx="1"\nname="disk1"\ndevice="sda"\nid="FAKE_MODEL_TEST9999"\nstatus="DISK_OK"\n'
    printf '["disk2"]\nidx="2"\nname="disk2"\ndevice="sdb"\nid="FAKE_MODEL_TEST12345"\nstatus="DISK_OK"\n'
    if [ "$gone" != 1 ] && [ "$slot" != dev1 ]; then
      printf '["%s"]\nidx="12"\nname="%s"\ndevice="sdx"\nid="FAKE_MODEL_%s"\nstatus="DISK_OK"\n' "$slot" "$slot" "$SUF"
    fi
    echo "=====UH devs"
    if [ "$gone" != 1 ] && [ "$slot" = dev1 ]; then
      printf '["dev1"]\nname="dev1"\nid="FAKE_MODEL_%s"\ndevice="sdx"\ntransport="ata"\ntemp="40"\n' "$SUF"
    fi
    echo "=====UH monitor"
    echo "nothing=1"
    # another disk's smartctl section first, with bad values that must not leak into ours
    echo "=====UH smartctl disk1 sda"
    smart 99 98 97 96 FAILED
    echo "=====UH_RC 0"
    if [ "$gone" != 1 ] && [ "$nosmart" != 1 ]; then
      echo "=====UH smartctl $slot sdx"
      smart "$realloc" "$pending" "$unc" "$crc" "$health"
      echo "=====UH_RC 0"
    fi
    echo "=====UH df"
    echo "Filesystem 1K-blocks Used"
  } > "$f"
}
smart() {
  cat <<EOF
smartctl 7.5 2025-04-30 r5714 [x86_64-linux] (local build)

=== START OF READ SMART DATA SECTION ===
SMART overall-health self-assessment test result: $5

ID# ATTRIBUTE_NAME          FLAG     VALUE WORST THRESH TYPE      UPDATED  WHEN_FAILED RAW_VALUE
  1 Raw_Read_Error_Rate     0x000b   100   100   016    Pre-fail  Always       -       0
  5 Reallocated_Sector_Ct   0x0033   100   100   010    Pre-fail  Always       -       $1
  9 Power_On_Hours          0x0012   100   100   000    Old_age   Always       -       1234
194 Temperature_Celsius     0x0002   150   150   000    Old_age   Always       -       40 (Min/Max 20/45)
197 Current_Pending_Sector  0x0012   100   100   000    Old_age   Always       -       $2
198 Offline_Uncorrectable   0x0010   100   100   000    Old_age   Offline      -       $3
199 UDMA_CRC_Error_Count    0x003e   200   200   000    Old_age   Always       -       $4
EOF
}

# per-case environment
H=""; RC=0; OUT=""; ERR=""
newhome() { H=$(mktemp -d "$T/home.XXXXXX"); mkdir -p "$H/inbox"; }
# pw [fixture args...] -- [script args...]
pw() {
  local fargs=() a
  while [ $# -gt 0 ] && [ "$1" != -- ]; do fargs+=("$1"); shift; done
  [ "${1:-}" = -- ] && shift
  fx "$T/fixture.txt" "${fargs[@]}"
  OUT=$(HOME="$H" PATH="$T/fakebin:/usr/local/bin:/usr/bin:/bin" PW_FIXTURE="$T/fixture.txt" \
        bash "$T/preclear-watch" "$@" 2>"$T/err"); RC=$?
  ERR=$(cat "$T/err")
}
notes() { find "$H/inbox" -maxdepth 1 -name 'preclear-watch-*.md' 2>/dev/null | wc -l; }
lastnote() { find "$H/inbox" -maxdepth 1 -name 'preclear-watch-*.md' -print0 2>/dev/null | xargs -0 -r cat 2>/dev/null; }
clearnotes() { find "$H/inbox" -maxdepth 1 -name 'preclear-watch-*.md' -delete 2>/dev/null; }
state() { echo "$H/.local/state/preclear-watch/$SUF.json"; }
jget() { python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(eval(sys.argv[2], {}, {"d": d}))' "$(state)" "$1"; }
expect_silent() { if [ "$RC" = 0 ] && [ "$(notes)" = 0 ]; then ok "$1"; else no "$1" "rc=$RC notes=$(notes) err=$ERR"; fi; clearnotes; }
expect_note() { # desc pattern
  if [ "$RC" = 0 ] && [ "$(notes)" = 1 ] && grep -qF -- "$2" <<<"$(lastnote)"; then ok "$1"; else no "$1" "rc=$RC notes=$(notes) err=$ERR note=$(lastnote | head -5 | tr '\n' '|')"; fi
  clearnotes
}

# ---------------------------------------------------------------- first run, start, stop
newhome; pw run=0 -- "$SUF"
expect_silent "first run, not running: silent"
[ -f "$(state)" ] && [ "$(jget "d['running']")" = False ] && [ "$(jget "d['slot'], d['dev']")" = "('dev1', 'sdx')" ] \
  && ok "first run writes state (running False, dev1/sdx)" || no "first run writes state"
[ "$(jget "d['smart']")" = "{'realloc': 0, 'pending': 0, 'unc': 0, 'crc': 0, 'temp': 40, 'health': 'PASSED'}" ] \
  && ok "SMART parsed from the disk's own smartctl section only" || no "SMART parse" "$(jget "d['smart']")"
pw run=0 -- "$SUF"; expect_silent "second run, still not running: silent"
pw run=1 -- "$SUF"; expect_note "start detected" "preclear started"
[ "$(jget "d['running']")" = True ] && ok "state running True after start" || no "state running True after start"
pw run=1 -- "$SUF"; expect_silent "still running: silent"
pw run=0 -- "$SUF"; expect_note "stop detected (finished or aborted)" "finished or aborted"
pw run=0 -- "$SUF"; expect_silent "stopped, stays stopped: silent"

newhome; pw run=1 -- "$SUF"; expect_note "first run while running: note" "preclear running (first check)"
newhome; pw run=0 other_pc=1 -- "$SUF"
[ "$RC" = 0 ] && [ "$(jget "d['running']")" = False ] && [ "$(notes)" = 0 ] \
  && ok "another disk in preclear does not count as ours" || no "another disk in preclear does not count as ours"

# ---------------------------------------------------------------- SMART worsening
newhome; pw run=1 -- "$SUF"; clearnotes
pw run=1 realloc=8 -- "$SUF"; expect_note "realloc > 0: note" "SMART realloc = 8 (was 0)"
pw run=1 realloc=8 -- "$SUF"; expect_silent "realloc unchanged: silent"
pw run=1 realloc=9 -- "$SUF"; expect_note "realloc grows: note" "SMART realloc = 9 (was 8)"
pw run=1 realloc=9 pending=2 -- "$SUF"; expect_note "pending > 0: note" "SMART pending = 2"
pw run=1 realloc=9 pending=2 unc=1 -- "$SUF"; expect_note "uncorrectable > 0: note" "SMART unc = 1"
newhome; pw run=1 realloc=3 -- "$SUF"
if [ "$(notes)" = 1 ] && grep -qF "preclear running (first check)" <<<"$(lastnote)" && grep -qF "SMART realloc = 3 (was 0)" <<<"$(lastnote)"; then
  ok "first run with realloc > 0: one note with both lines"; else no "first run with realloc > 0"; fi

newhome; pw run=1 crc=2 -- "$SUF"; clearnotes
pw run=1 crc=5 -- "$SUF"; expect_note "CRC grows: note" "CRC errors grew 2 → 5"
pw run=1 crc=5 -- "$SUF"; expect_silent "CRC same: silent"
pw run=1 crc=4 -- "$SUF"; expect_silent "CRC lower (counter reset): silent"

newhome; pw run=1 -- "$SUF"; clearnotes
pw run=1 health=FAILED! -- "$SUF"; expect_note "health not PASSED: note" "SMART health FAILED!"
pw run=1 health=FAILED! -- "$SUF"; expect_silent "health still FAILED: silent"

# ---------------------------------------------------------------- disk gone / moved
newhome; pw run=1 -- "$SUF"; clearnotes
pw gone=1 -- "$SUF"; expect_note "disk gone: note" "not visible on the server"
[ "$(jget "d['dev']")" = None ] && ok "state dev None when gone" || no "state dev None when gone"
pw gone=1 -- "$SUF"; expect_silent "disk still gone: no repeated note"
pw run=0 slot=disk12 -- "$SUF"
if [ "$RC" = 0 ] && [ "$(jget "d['slot']")" = disk12 ] && [ "$(notes)" = 0 ]; then ok "disk back as array disk12: found, silent"; else no "disk back as array disk12" "rc=$RC notes=$(notes) err=$ERR"; fi
newhome; pw gone=1 -- "$SUF"; expect_note "first run, disk not visible: note" "not visible"
newhome; pw run=0 -- TEST123; expect_note "suffix must match the end of the id (TEST123 vs …TEST1234)" "not visible"

# ---------------------------------------------------------------- SMART section missing for one run
newhome; pw run=1 realloc=4 crc=1 health=FAILED -- "$SUF"; clearnotes
pw run=1 nosmart=1 -- "$SUF"; expect_silent "SMART section missing: silent"
pw run=1 realloc=4 crc=1 health=FAILED -- "$SUF"; expect_silent "SMART back, unchanged: no repeated notes"
pw run=1 nosmart=1 -- "$SUF"; pw run=1 realloc=4 crc=3 health=FAILED -- "$SUF"
expect_note "CRC grows across a run without SMART: note" "CRC errors grew 1 → 3"

# ---------------------------------------------------------------- --status
newhome; pw run=1 -- --status "$SUF"
if [ "$RC" = 0 ] && [ ! -e "$(state)" ] && [ "$(notes)" = 0 ] && \
   echo "$OUT" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["now"]["running"] is True and d["now"]["slot"]=="dev1" and d["prev"] is None'; then
  ok "--status prints JSON, writes no state, no note"; else no "--status prints JSON, writes no state" "rc=$RC out=$OUT err=$ERR"; fi
pw run=0 -- "$SUF"; before=$(md5sum < "$(state)")
pw run=1 realloc=5 -- --status "$SUF"
if [ "$RC" = 0 ] && [ "$(md5sum < "$(state)")" = "$before" ] && [ "$(notes)" = 0 ] && \
   echo "$OUT" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["prev"]["running"] is False and d["now"]["smart"]["realloc"]==5'; then
  ok "--status with prior state: shows prev, state unchanged"; else no "--status with prior state" "rc=$RC err=$ERR"; fi

# ---------------------------------------------------------------- serial default
newhome; mkdir -p "$H/.config/preclear-watch"; echo "$SUF" > "$H/.config/preclear-watch/serial"
pw run=1 -- ; expect_note "serial from ~/.config/preclear-watch/serial" "preclear running (first check)"
[ -f "$(state)" ] && ok "state named after the configured serial" || no "state named after the configured serial"
pw run=1 -- --status
echo "$OUT" | python3 -c 'import json,sys; assert json.load(sys.stdin)["now"]["running"] is True' 2>/dev/null \
  && ok "--status without serial uses the config" || no "--status without serial uses the config" "rc=$RC err=$ERR"
newhome; pw run=1 -- 
[ "$RC" = 2 ] && grep -q usage <<<"$ERR" && ok "no serial, no config: usage, exit 2" || no "no serial, no config: exit 2" "rc=$RC"

# ---------------------------------------------------------------- health verb failure
newhome; pw run=0 -- "$SUF"; before=$(md5sum < "$(state)")
fx "$T/fixture.txt" run=1 realloc=7
OUT=$(HOME="$H" PATH="$T/fakebin:/usr/local/bin:/usr/bin:/bin" PW_FIXTURE="$T/fixture.txt" PW_FAKE_RC=255 \
      bash "$T/preclear-watch" "$SUF" 2>"$T/err"); RC=$?
if [ "$RC" = 1 ] && grep -q "health verb failed" "$T/err" && [ "$(md5sum < "$(state)")" = "$before" ] && [ "$(notes)" = 0 ]; then
  ok "health verb failure: exit 1, state unchanged, no note"; else no "health verb failure" "rc=$RC err=$(cat "$T/err")"; fi

# ---------------------------------------------------------------- misc
newhome; rmdir "$H/inbox"; pw run=1 -- "$SUF"
[ "$RC" = 0 ] && [ "$(notes)" = 1 ] && ok "missing ~/inbox is created" || no "missing ~/inbox is created" "rc=$RC err=$ERR"
grep -q "unraid-ssh" "$T/preclear-watch" && ! grep -q "/unraid-ssh" "$T/preclear-watch" \
  && ok "script calls unraid-ssh via PATH (fake is used)" || no "script calls unraid-ssh via PATH"

# ---------------------------------------------------------------- temperature limit
newhome; pw run=1 -- "$SUF"; clearnotes
PW_HOT=40 pw run=1 -- "$SUF"; expect_note "temperature at the limit: note" "temperature 40°C (limit 40)"
PW_HOT=40 pw run=1 -- "$SUF"; [ "$(notes)" = 0 ] && ok "still hot: no repeat note" || no "hot repeat" "notes=$(notes)"
PW_HOT=50 pw run=1 -- "$SUF"; expect_note "cooled down: note" "temperature back to 40°C"
echo "# $pass passed, $fail failed"
[ "$fail" = 0 ]
