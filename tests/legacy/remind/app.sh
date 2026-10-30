#!/usr/bin/env bash
# Tests for remind's phone-app side: `actions` (the Telegram buttons as ≤ 3 app verbs), `act` (phonegw's bridge
# entry) and the opt-in app route in `due` (config route telegram|app|both, stub phone CLI).
# Temp state only: REMIND_DB/INBOX, stub REMIND_TG/REMIND_TG_BUTTONS/REMIND_PHONE, REMIND_CONFIG, REMIND_NOW.
# REMIND_BIN: a copy before installing. Run on its own or from run.sh. Exit 0 = all pass.
set -uo pipefail
R=${REMIND_BIN:-/home/node/bin/remind}
REAL_DIR=/home/node/.local/state/remind
T=$(mktemp -d "${TMPDIR:-/tmp}/remind-app-tests.XXXXXX")
trap 'rm -rf "${T:?}"' EXIT
export REMIND_DB=$T/rem.json REMIND_INBOX=$T/inbox REMIND_TG=$T/tg REMIND_TG_BUTTONS=$T/tgb
export REMIND_CONFIG=$T/config.json REMIND_NOW="2026-10-20 10:00"
unset REMIND_BUTTONS REMIND_PHONE
cat > "$T/tg" <<STUB
#!/bin/sh
printf '%s\n' "\$*" >> "$T/tg.log"
STUB
cat > "$T/tgb" <<STUB
#!/bin/sh
printf '%s\n' "\$1" >> "$T/tgb.log"
STUB
# phone stub: argv as one JSON line per call; exit code from $T/phone-rc (default 0)
cat > "$T/phone" <<STUB
#!/usr/bin/env python3
import json, os, sys
with open("$T/phone.log", "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
rc = int(open("$T/phone-rc").read()) if os.path.exists("$T/phone-rc") else 0
if sys.argv[1:2] == ["push"]:
    print(json.dumps({"id": "01JREMTEST", "nonce": "a1b2c3d4", "status": "delivered" if rc == 0 else "queued"}))
sys.exit(rc)
STUB
chmod +x "$T/tg" "$T/tgb" "$T/phone"
M=APPTEST   # marker in every test text: must never reach the live store

pass=0 fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2"; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
has() { if grep -qF -- "$3" <<<"$2"; then ok "$1"; else no "$1" "$2"; fi; }
reset() { find "$T" -mindepth 1 -maxdepth 1 ! -name tg ! -name tgb ! -name phone -exec rm -rf {} +; }
py() { python3 -I -c "$@"; }
idof() { py 'import json,sys; print(" ".join(r["id"] for r in json.load(open(sys.argv[1])) if sys.argv[2] in r["text"]))' "$REMIND_DB" "$1"; }
rstate() { py 'import json,sys; d=json.load(open(sys.argv[1])); o=d.get(sys.argv[2]); print("none" if o is None else o["state"] + (" " + o["to"] if o.get("to") else ""))' "$T/recent.json" "$1"; }
at() { REMIND_NOW="$1" timeout 30 "$R" "${@:2}"; }
lines() { [ -f "$1" ] && wc -l < "$1" | tr -d ' ' || echo 0; }
route() { printf '{"route": "%s"}\n' "$1" > "$REMIND_CONFIG"; }
pcalls() { [ -f "$T/phone.log" ] && py 'import json,sys
for l in open(sys.argv[1]): a = json.loads(l); print(a[0] + (" " + a[a.index("--tag") + 1] if "--tag" in a else "") + (" " + a[1] if a[0] == "cancel" else ""))' "$T/phone.log" || true; }

# ---------------------------------------------------------------- actions: the buttons as app verbs
reset
at "2026-10-20 09:00" add --owner "2026-10-20 10:00" "$M pills" >/dev/null
i=$(idof "$M pills")
at "2026-10-20 10:00" due
eq "fired once on Telegram (route telegram by default)" "$(lines "$T/tgb.log")" "1"
eq "default route: phone never runs" "$(lines "$T/phone.log")" "0"
eq "actions before 18:00: +1 h, Tonight, Done" "$(at "2026-10-20 10:01" actions "$i" | tr '\n' '|')" \
  "+1 h:snooze:r:$i:s1h|Tonight:snooze:r:$i:ston|Done:done:r:$i|"
eq "actions after 18:00: Tomorrow instead of Tonight" "$(at "2026-10-20 18:30" actions "$i" | sed -n 2p)" \
  "Tomorrow:snooze:r:$i:stom"
j=$(at "2026-10-20 10:01" actions "$i" --json)
eq "actions --json: 3 actions, no ':' in labels" "$(py 'import json,sys; a=json.loads(sys.argv[1]); print(len(a), all(":" not in x["label"] for x in a), sorted({x["verb"] for x in a}))' "$j")" \
  "3 True ['done', 'snooze']"
at "2026-10-20 10:01" actions ffffffff >/dev/null 2>&1; eq "actions: unknown ID exit 1" "$?" "1"
at "2026-10-20 10:01" actions >/dev/null 2>&1; eq "actions: no ID exit 2" "$?" "2"
at "2026-10-20 10:01" add --owner --every daily "2026-10-21 08:00" "$M daily" >/dev/null
at "2026-10-20 10:01" actions "$(idof "$M daily")" >/dev/null 2>&1; eq "actions: a series ID is refused (exit 1)" "$?" "1"

# ---------------------------------------------------------------- act: phonegw's bridge entry
out=$(at "2026-10-20 10:02" act snooze "r:$i:s1h"); eq "act snooze r:ID:s1h exit 0" "$?" "0"
eq "act snooze s1h → +1 h" "$(rstate "$i")" "snoozed 2026-10-20 11:02"
at "2026-10-20 10:03" act snooze "r:$i" >/dev/null
eq "act snooze plain r:ID → +15 min" "$(rstate "$i")" "snoozed 2026-10-20 10:18"
at "2026-10-20 10:04" act snooze "r:$i:stom" >/dev/null
eq "act snooze stom → tomorrow 09:00" "$(rstate "$i")" "snoozed 2026-10-21 09:00"
has "act seen: nothing to change" "$(at "2026-10-20 10:05" act seen "r:$i")" "nothing to change"
eq "act seen leaves the state" "$(rstate "$i")" "snoozed 2026-10-21 09:00"
at "2026-10-20 10:06" act done "r:$i" >/dev/null; eq "act done exit 0" "$?" "0"
eq "act done → done" "$(rstate "$i")" "done"
eq "done one-off left the store" "$(idof "$M pills")" ""
for bad in "approve r:$i" "done r:$i:s1h" "snooze rm:s1h:$i" "snooze r:$i:xyz" "snooze r:12" "seen" "done r:$i extra"; do
  # shellcheck disable=SC2086
  at "2026-10-20 10:07" act $bad >/dev/null 2>&1; eq "act $bad → exit 2" "$?" "2"
done
at "2026-10-20 10:07" act done r:0000aaaa >/dev/null 2>&1; eq "act done unknown ID → exit 1" "$?" "1"

# location reminder: 📍 Next time as the second action; act snooze snxt brings it back as a place reminder
at "2026-10-20 10:08" add --on home "$M plant" >/dev/null
p=$(py 'import json,sys; print(next(r["id"] for r in json.load(open(sys.argv[1])) if "plant" in r["text"]))' "$T/places.json")
eq "location actions: +1 h, 📍 Next time, Done" "$(at "2026-10-20 10:09" actions "$p" | cut -d: -f1 | tr '\n' '|')" \
  "+1 h|📍 Next time|Done|"

# ---------------------------------------------------------------- due: route app / both / telegram
reset; route app; export REMIND_PHONE=$T/phone
at "2026-10-20 09:00" add --owner "2026-10-20 10:00" "$M app one" >/dev/null
a=$(idof "$M app one")
at "2026-10-20 10:00" due
eq "route app, delivered: no Telegram" "$(lines "$T/tgb.log")$(lines "$T/tg.log")" "00"
eq "route app: one phone push, tag rem-ID" "$(pcalls)" "push rem-$a"
args=$(cat "$T/phone.log")
has "push level timed" "$args" '"--level", "timed"'
has "push carries the snooze verb" "$args" "\"+1 h:snooze:r:$a:s1h\""
has "push carries done" "$args" "\"Done:done:r:$a\""
has "push body = his text (no ⏰ prefix)" "$args" "\"--body\", \"$M app one\""
has "push waits for the device" "$args" '"--wait", "8"'
eq "fired occurrence kept for act" "$(rstate "$a")" "fired"

reset; route app; echo 4 > "$T/phone-rc"
at "2026-10-20 09:00" add --owner "2026-10-20 10:00" "$M offline" >/dev/null
a=$(idof "$M offline")
at "2026-10-20 10:00" due
eq "phone offline (exit 4): Telegram with buttons" "$(lines "$T/tgb.log")" "1"
eq "phone offline: queued copy cancelled" "$(pcalls | tr '\n' '|')" "push rem-$a|cancel rem-$a|"

reset; route app; echo 7 > "$T/phone-rc"
at "2026-10-20 09:00" add --owner "2026-10-20 10:00" "$M gwdown" >/dev/null
at "2026-10-20 10:00" due
eq "gateway down (exit 7): Telegram, no cancel" "$(lines "$T/tgb.log") $(lines "$T/phone.log")" "1 1"

reset; route both
at "2026-10-20 09:00" add --owner "2026-10-20 10:00" "$M both" >/dev/null
at "2026-10-20 10:00" due
eq "route both: app and Telegram" "$(lines "$T/phone.log") $(lines "$T/tgb.log")" "1 1"

reset; route telegram
at "2026-10-20 09:00" add --owner "2026-10-20 10:00" "$M tgonly" >/dev/null
at "2026-10-20 10:00" due
eq "route telegram: phone never runs" "$(lines "$T/phone.log") $(lines "$T/tgb.log")" "0 1"

reset; route app
at "2026-10-20 09:00" add "2026-10-20 10:00" "$M for me" >/dev/null
at "2026-10-20 10:00" due
eq "reminder for me: inbox note, no phone" "$(lines "$T/phone.log") $(ls "$T/inbox" | wc -l | tr -d ' ')" "0 1"

reset; route app; echo '{not json' > "$REMIND_CONFIG"
at "2026-10-20 09:00" add --owner "2026-10-20 10:00" "$M badcfg" >/dev/null
at "2026-10-20 10:00" due
eq "unreadable config = telegram" "$(lines "$T/phone.log") $(lines "$T/tgb.log")" "0 1"

unset REMIND_PHONE
reset; route app
at "2026-10-20 09:00" add --owner "2026-10-20 10:00" "$M nophone" >/dev/null
at "2026-10-20 10:00" due
eq "test state without REMIND_PHONE: no phone, Telegram" "$(lines "$T/phone.log") $(lines "$T/tgb.log")" "0 1"

# ---------------------------------------------------------------- live store
leak=$(cat "$REAL_DIR/reminders.json" "$REAL_DIR/recent.json" "$REAL_DIR/places.json" 2>/dev/null | grep -c "$M")
eq "live reminders.json / recent.json / places.json: no test text" "$leak" "0"
echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
