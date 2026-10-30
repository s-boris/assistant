#!/usr/bin/env bash
# Tests for ~/bin/letter and ~/bin/pdfform (admin desk). Exit 0 = all pass.
# Everything runs against temp state: a stub paperless (logs instead of uploading), a temp REMIND_DB with a
# stub sender, a temp config dir (no signature), and a fake clock. The real reminders DB and
# ~/.config/letter are checked to be untouched. Sample outputs go to $LETTER_SAMPLES (default: the temp dir, removed
# at exit; never into the repo tree).
# The sender (name, address, date-line town) comes from the synthetic profile fixture; KK_PROFILE points to a
# temp file that exists only in section A2.
set -uo pipefail
# Synthetic profile (tests never read the real one): repo layout tests/profile, live layout ~/opt/profile-tests.
_pf="$(cd "$(dirname "$0")/.." && pwd)"; [ -f "$_pf/profile/fixtures/profile.json" ] && _pf=$_pf/profile || _pf=$_pf/profile-tests
export ASSISTANT_PROFILE="$_pf/fixtures/profile.json"
HERE=$(cd "$(dirname "$0")" && pwd)
# the real home (the CI runner's nss_wrapper names its job HOME there); HOME itself may be a temp one with ~/opt linked
RH=$(getent passwd "$(id -u)" | cut -d: -f6)
PY=${LETTER_PY:-$HOME/opt/letter-venv/bin/python}
L=${LETTER_BIN:-$RH/bin/letter}      # LETTER_BIN / PDFFORM_BIN: test another copy
P=${PDFFORM_BIN:-$RH/bin/pdfform}
REAL_REM=$RH/.local/state/remind/reminders.json
REAL_SIG=$RH/.config/letter/signature.png
T=$(mktemp -d "${TMPDIR:-/tmp}/letter-tests.XXXXXX")
trap 'rm -rf "$T"' EXIT
SAMPLES=${LETTER_SAMPLES:-$T/samples}
mkdir -p "$SAMPLES" "$T/cfg" "$T/out"
# assistant ci stages only letter (LETTER_BIN) in its overlay, but letter and pdfform import _admindesk.py from their
# own dir (and it imports assistant_profile from lib/ there): under CI (ASSISTANT_CI_TREE) the staged letter runs from
# $T/bin beside the tree's _admindesk.py and pdfform, with the overlay's lib/ (else ~/bin/lib).
if [ -n "${ASSISTANT_CI_TREE:-}" ] && [ -f "$ASSISTANT_CI_TREE/scripts/_admindesk" ]; then
  mkdir -p "$T/bin"
  _ov=$(cd "$(dirname "$L")" && pwd)
  if [ -d "$_ov/lib" ]; then ln -s "$_ov/lib" "$T/bin/lib"; else ln -s "$RH/bin/lib" "$T/bin/lib"; fi
  cp "$L" "$T/bin/letter" && chmod 0755 "$T/bin/letter"
  if [ -f "$_ov/_admindesk.py" ]; then cp "$_ov/_admindesk.py" "$T/bin/_admindesk.py"
  else cp "$ASSISTANT_CI_TREE/scripts/_admindesk" "$T/bin/_admindesk.py"; fi
  L=$T/bin/letter
  if [ -z "${PDFFORM_BIN:-}" ]; then
    cp "$ASSISTANT_CI_TREE/scripts/pdfform" "$T/bin/pdfform" && chmod 0755 "$T/bin/pdfform"
    P=$T/bin/pdfform
  fi
  # their shebang names the host's letter venv by its absolute path: the copies run with this home's venv ($PY)
  for _f in "$T/bin/letter" "$T/bin/pdfform"; do
    [ -f "$_f" ] && sed -i "1s|^#!.*/opt/letter-venv/bin/python[0-9.]*\$|#!$PY|" "$_f"
  done
fi

export ADMIN_DESK_NOW="2026-10-10 11:00" LETTER_CONFIG_DIR=$T/cfg
export REMIND_DB=$T/rem.json REMIND_INBOX=$T/inbox REMIND_TG=$T/tg
export ADMIN_DESK_PAPERLESS=$T/paperless
export RL_invariant=1   # reportlab: fixed dates and ids, so the sample PDFs are byte-stable across runs
unset LETTER_SIGNATURE
export KK_PROFILE=$T/kk-profile.json   # absent except in A2: no real kk profile
printf '#!/bin/sh\nprintf "%%s\\n" "$*" >> "%s/tg.log"\n' "$T" > "$T/tg"
cat > "$T/paperless" <<STUB
#!/bin/sh
printf '%s\n' "\$*" >> "$T/paperless.log"
case "\$1" in
  meta)
    echo "tags (2):"
    [ -e "$T/has-tag" ] && echo "  Korrespondenz  [id 70, 0 docs]"
    echo "  Lohnausweis  [id 55, 5 docs]"
    echo "correspondents (0):"
    echo "document types (1):"
    echo "  Kündigung  [id 1, 1 docs]" ;;
  upload) echo "uploaded, task abc" >&2; echo "document 999: http://198.51.100.5:8000/documents/999/details" ;;
  *) exit 1 ;;
esac
STUB
chmod +x "$T/tg" "$T/paperless"

rem_before=$({ md5sum < "$REAL_REM"; } 2>/dev/null)
sig_before=$([ -e "$REAL_SIG" ] && echo present || echo absent)

pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$(printf '%s' "$2" | head -c 600)"; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
# has/hasnt test the substring in the shell: `printf | grep -q` under pipefail reads as no match when grep exits
# on the match before printf (one write per line) is done, SIGPIPE (a false FAIL, or a false pass for hasnt)
has() { if [[ $2 == *"$3"* ]]; then ok "$1"; else no "$1" "$2"; fi; }
hasnt() { if [[ $2 == *"$3"* ]]; then no "$1" "$2"; else ok "$1"; fi; }
# PDF text with all whitespace (incl. NBSP and line wraps) collapsed to single spaces
txt() { pdftotext "$@" - 2>/dev/null | sed 's/\xc2\xa0/ /g' | tr '\n\f' '  ' | tr -s ' '; }
nimg() { pdfimages "$@" -list 2>/dev/null | awk 'NR > 2 && $3 == "image"' | wc -l | tr -d ' '; }
pages() { pdfinfo "$1" 2>/dev/null | awk '/^Pages:/ {print $2}'; }
# x (pt) and top y (pt) of the first word matching $2 in PDF $1 (pdftotext -bbox, origin top-left)
pos() { pdftotext -bbox "$1" - 2>/dev/null | grep -F ">$2<" | head -1 | sed -E 's/.*xMin="([0-9.]+)" yMin="([0-9.]+)".*/\1 \2/'; }
in_range() { awk -v v="$2" -v lo="$3" -v hi="$4" 'BEGIN { exit !(v >= lo && v <= hi) }' && ok "$1" || no "$1" "$2 not in [$3, $4]"; }

TO_SALT='Salt Mobile SA\nKundendienst\nRue du Caudray 4\n1020 Renens VD'
TO_STV='Kantonale Steuerverwaltung Examplecity\nJ. J. Wepfer-Strasse 6\n8200 Examplecity'

echo "# A. letter: Kündigung (template)"
out=$("$L" new --type kuendigung --to "$TO_SALT" --contract "Mobile-Abo Salt Swiss Unlimited" --effective 2026-12-31 \
      --notice-months 2 --ref "Kundennummer: 1234567" --out "$T/out/k.pdf" 2>&1); rc=$?
eq "kuendigung rc 0" "$rc" "0"
eq "kuendigung 1 page" "$(pages "$T/out/k.pdf")" "1"
t=$(txt "$T/out/k.pdf")
for s in "Robin Tester" "Teststrasse 9" "4321 Testhausen" "Testhausen, 10. Oktober 2026" \
         "Kündigung Mobile-Abo Salt Swiss Unlimited" "Kundennummer: 1234567" "Sehr geehrte Damen und Herren" \
         "fristgerecht per 31. Dezember 2026" "nächstmöglichen Termin" "Bitte bestätigen Sie mir die Kündigung schriftlich" \
         "Freundliche Grüsse" "Einschreiben" "Rue du Caudray 4" "1020 Renens VD"; do
  has "kuendigung PDF text: $s" "$t" "$s"
done
k=$(cat "$T/out/k.txt" 2>/dev/null)
has "kuendigung .txt copy written" "$k" "Bitte bestätigen Sie mir die Kündigung schriftlich"
has "txt has EINSCHREIBEN + recipient" "$k" "EINSCHREIBEN
Salt Mobile SA"
has "deadline: must arrive by 31 Oct" "$out" "Must ARRIVE at the recipient by Saturday, 31 October 2026"
has "suggested send-by" "$out" "suggested: --send-by 2026-10-28"
read -r x y <<< "$(pos "$T/out/k.pdf" Salt)"
in_range "right window: recipient x ≈ 118 mm" "$x" 330 340
in_range "right window: recipient y in 60-88 mm" "$y" 170 250
read -r x y <<< "$(pos "$T/out/k.pdf" Kündigung)"
in_range "subject at left margin 25 mm" "$x" 68 74
cp "$T/out/k.pdf" "$SAMPLES/kuendigung-de.pdf"; cp "$T/out/k.txt" "$SAMPLES/kuendigung-de.txt"

out=$("$L" new --type kuendigung --to "$TO_SALT" --contract X --effective 2026-12-31 --notice-months 3 --out "$T/out/k3.pdf" 2>&1)
has "deadline passed detected" "$out" "DEADLINE PASSED"

printf 'Hiermit kündige ich mein Abonnement per 31. Januar 2027.\n' > "$T/body.txt"
out=$("$L" new --type kuendigung --to "$TO_SALT" --subject "Kündigung Fitness-Abo" --body-file "$T/body.txt" --out "$T/out/kb.pdf" 2>&1)
has "body-file Kündigung gets confirmation sentence" "$(txt "$T/out/kb.pdf")" "Bitte bestätigen Sie mir die Kündigung schriftlich"
has "and says so" "$out" "added the written-confirmation sentence"
printf 'Ich kündige.\n\nBitte bestätigen Sie den Eingang.\n' > "$T/body2.txt"
"$L" new --type kuendigung --to "$TO_SALT" --subject "Kündigung" --body-file "$T/body2.txt" --out "$T/out/kb2.pdf" >/dev/null 2>&1
eq "no duplicate confirmation when present" "$(grep -c 'bestätig' "$T/out/kb2.txt")" "1"

echo "# A2. letter: sender sources (assistant profile < kk profile < sender.json)"
printf '{"name": "Kim Kk", "street": "Kkweg 2", "plz_city": "5555 Kkdorf", "city": "Kkdorf"}\n' > "$KK_PROFILE"
"$L" new --to "$TO_SALT" --subject "Sender" --body "Text." --out "$T/out/s1.pdf" >/dev/null 2>&1
t=$(txt "$T/out/s1.pdf")
has "kk profile overrides the profile: name" "$t" "Kim Kk"
has "kk profile: date line uses its city" "$t" "Kkdorf, 10. Oktober 2026"
hasnt "kk profile: profile name not used" "$t" "Robin Tester"
mkdir -p "$T/cfg2"; printf '{"name": "Sam Sender", "extra": "Tel. 000 111"}\n' > "$T/cfg2/sender.json"
LETTER_CONFIG_DIR=$T/cfg2 "$L" new --to "$TO_SALT" --subject "Sender" --body "Text." --out "$T/out/s2.pdf" >/dev/null 2>&1
t=$(txt "$T/out/s2.pdf")
has "sender.json overrides the name" "$t" "Sam Sender"
has "sender.json keeps the kk street" "$t" "Kkweg 2"
has "sender.json extra line" "$t" "Tel. 000 111"
rm -f "$KK_PROFILE"
"$L" new --to "$TO_SALT" --subject "Sender" --body "Text." --out "$T/out/s3.pdf" >/dev/null 2>&1
has "no kk profile: back to the profile fixture" "$(txt "$T/out/s3.pdf")" "Robin Tester Teststrasse 9 4321 Testhausen"

echo "# B. letter: Einsprache, Widerruf, generic, English, left window"
printf 'Die Berufskosten von CHF 3200 wurden nicht berücksichtigt.\n\n- Streckenabo: CHF 2890\n- Halbtax: CHF 190\n' > "$T/reasons.txt"
out=$("$L" new --type einsprache --to "$TO_STV" --decision "Veranlagungsverfügung Staats- und Gemeindesteuern 2025" \
      --decision-date 2026-09-28 --received 2026-10-02 --antrag "Die Berufskosten seien mit CHF 3200 zum Abzug zuzulassen." \
      --reasons-file "$T/reasons.txt" --encl "Kopie Veranlagungsverfügung" --encl "Belege" --out "$T/out/e.pdf" 2>&1); rc=$?
eq "einsprache rc 0" "$rc" "0"
t=$(txt "$T/out/e.pdf")
for s in "Einsprache gegen die Veranlagungsverfügung Staats- und Gemeindesteuern 2025 vom 28. September 2026" \
         "zugestellt am 2. Oktober 2026" "Antrag" "Begründung" "Streckenabo: CHF 2890" "Beilagen" "Kopie Veranlagungsverfügung" \
         "Die entsprechenden Belege liegen bei."; do
  has "einsprache PDF text: $s" "$t" "$s"
done
has "einsprache 30-day deadline (Sat 1 Nov -> Mon 2 Nov)" "$out" "last day Monday, 2 November 2026"
eq "einsprache fits on 1 page" "$(pages "$T/out/e.pdf")" "1"
cp "$T/out/e.pdf" "$SAMPLES/einsprache-de.pdf"
out=$("$L" new --type einsprache --to "$TO_STV" --out "$T/out/e2.pdf" 2>&1); rc=$?
eq "einsprache without Antrag/Begründung refused (placeholders, rc 3)" "$rc" "3"
has "lists the placeholders" "$out" "[Antrag:"

out=$("$L" new --type widerruf --lang en --window left --to 'Example Vertriebs GmbH\nMusterweg 1\n8000 Zürich' \
      --contract "solar panel purchase" --contract-date 2026-10-05 --basis "Art. 40a ff. OR" --out "$T/out/w.pdf" 2>&1)
t=$(txt "$T/out/w.pdf")
for s in "Withdrawal from solar panel purchase of 5 October 2026" "Dear Sir or Madam," "Kind regards" \
         "Testhausen, 10 October 2026" "Registered mail" "pursuant to Art. 40a ff. OR"; do
  has "widerruf EN text: $s" "$t" "$s"
done
has "widerruf 14-day note" "$out" "Monday, 19 October 2026"
read -r x y <<< "$(pos "$T/out/w.pdf" Example)"
in_range "left window: recipient x ≈ 25 mm" "$x" 68 74
in_range "left window: recipient y in 60-88 mm" "$y" 170 250
read -r x y <<< "$(pos "$T/out/w.pdf" Teststrasse)"
in_range "left window: sender block moves right" "$x" 330 340
cp "$T/out/w.pdf" "$SAMPLES/widerruf-en-leftwindow.pdf"

"$L" new --to 'A\n8000 Zürich' --body "x" --out "$T/out/g0.pdf" >/dev/null 2>&1
eq "generic without --subject: rc 2" "$?" "2"
"$L" new --to 'Herr PersonB Petrović\nBeispielgasse 2\n1234 Exampletown' --subject "Rückgabe der Bohrmaschine" \
  --body 'Danke fürs Ausleihen.\n\nBis bald – Ćevapi am Samstag?' --salutation "Hallo PersonB" --closing "Liebe Grüsse" \
  --out "$T/out/g.pdf" >/dev/null 2>&1
t=$(txt "$T/out/g.pdf")
has "unicode name renders" "$t" "Petrović"
has "custom salutation" "$t" "Hallo PersonB"
has "custom closing" "$t" "Liebe Grüsse"
hasnt "generic: no Einschreiben by default" "$t" "Einschreiben"
has "non-cp1252 text embeds a Unicode font" "$(pdffonts "$T/out/g.pdf" 2>/dev/null)" "DejaVuSans"
cp "$T/out/g.pdf" "$SAMPLES/generic-unicode.pdf"

echo "# C. letter: placeholders, draft, preview, template, multi-page, signature"
out=$("$L" new --type kuendigung --to 'Fitnesspark AG\nPostfach\n8000 Zürich' --out "$T/out/p.pdf" 2>&1); rc=$?
eq "placeholder [Vertrag] blocks final PDF (rc 3)" "$rc" "3"
[ -e "$T/out/p.pdf" ] && no "no PDF written when blocked" || ok "no PDF written when blocked"
"$L" new --type kuendigung --to 'Fitnesspark AG\nPostfach\n8000 Zürich' --draft --out "$T/out/p.pdf" >/dev/null 2>&1
eq "draft rc 0" "$?" "0"
has "draft watermark" "$(pdftotext -raw "$T/out/p.pdf" - 2>/dev/null | tr -d ' \n')" "ENTWURF"
mkdir -p "$T/prev"
out=$(cd "$T/prev" && "$L" new --type kuendigung --to "$TO_SALT" --contract "Mobile-Abo" --preview 2>&1)
has "preview prints the letter" "$out" "Hiermit kündige ich meinen Vertrag «Mobile-Abo»"
eq "preview writes nothing" "$(ls "$T/prev" | wc -l | tr -d ' ')" "0"
out=$("$L" template kuendigung --contract "Netflix" --effective 2026-11-30 2>/dev/null)
has "template prints body" "$out" "Hiermit kündige ich meinen Vertrag «Netflix» fristgerecht per 30. November 2026."
hasnt "template stdout has no notes" "$out" "#"
$PY -c 'import sys; print("\n\n".join(f"Absatz {i}: " + "Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 4 for i in range(40)))' > "$T/long.txt"
"$L" new --to 'Muster AG\nBahnhofstrasse 1\n8001 Zürich' --subject "Langer Brief" --body-file "$T/long.txt" --out "$T/out/long.pdf" >/dev/null 2>&1
n=$(pages "$T/out/long.pdf"); in_range "long letter runs onto more pages" "$n" 2 9
has "page 2 header" "$(txt -f 2 -l 2 "$T/out/long.pdf")" "Seite 2"
out=$("$L" new --type kuendigung --to "$TO_SALT" --contract X --effective next --sign --out "$T/out/s0.pdf" 2>&1)
has "--sign without image: hint to send a photo" "$out" "send a photo of his"
eq "--sign without image: no image in PDF" "$(nimg "$T/out/s0.pdf")" "0"
# synthetic "photo" of a signature -> import (to the temp dir only) -> letter with image
$PY - "$T/sigphoto.jpg" <<'EOF'
import math, sys
from PIL import Image, ImageDraw, ImageFilter
im = Image.new("RGB", (900, 400), (236, 232, 225)); d = ImageDraw.Draw(im)
d.line([(80 + t * 7, 220 + 70 * math.sin(t / 9) - t * 0.6) for t in range(110)], fill=(25, 25, 60), width=7)
im.filter(ImageFilter.GaussianBlur(1.2)).save(sys.argv[1], quality=85)
EOF
out=$("$L" signature import "$T/sigphoto.jpg" --out "$T/sig.png" 2>&1)
has "signature import" "$out" "stored"
eq "signature stored 0600" "$(stat -c %a "$T/sig.png" 2>/dev/null)" "600"
eq "signature is transparent RGBA (corner alpha 0)" "$($PY -c 'import sys; from PIL import Image; im=Image.open(sys.argv[1]); print(im.mode, im.getpixel((0,0))[3])' "$T/sig.png")" "RGBA 0"
out=$(LETTER_SIGNATURE=$T/sig.png "$L" new --type kuendigung --to "$TO_SALT" --contract X --effective next --sign --out "$T/out/s1.pdf" 2>&1)
eq "--sign with image: one image in PDF" "$(nimg "$T/out/s1.pdf")" "1"
has "--sign on a Kündigung: handwritten-signature note" "$out" "Art. 14 OR"
"$L" signature status >/dev/null 2>&1
eq "signature status without image: rc 1" "$?" "1"

echo "# D. letter: --file (paperless stub) and --send-by (temp reminders)"
rm -f "$T/paperless.log" "$T/has-tag"
out=$("$L" new --type kuendigung --to "$TO_SALT" --contract "Mobile-Abo" --out "$T/out/f.pdf" --file 2>&1)
log=$(cat "$T/paperless.log")
has "file: meta read first" "$log" "meta"
has "file: uploads the PDF" "$log" "upload $T/out/f.pdf --title Kündigung Mobile-Abo (Brief 10.10.2026) --created 2026-10-10"
hasnt "file: no tag when Korrespondenz is missing" "$log" "--tag"
grep -qE -- '--create( |$)' <<<"$log" && no "file: never creates without --create-tag" "$log" || ok "file: never creates without --create-tag"
has "file: Kündigung type used when it exists" "$log" "--type Kündigung"
has "file: reports the missing tag" "$out" "has no tag 'Korrespondenz'"
has "file: prints the document link" "$out" "paperless: document 999"
rm -f "$T/paperless.log"; touch "$T/has-tag"
"$L" new --to 'Muster AG\n8001 Zürich' --subject "Anfrage" --body "Text" --out "$T/out/f2.pdf" --file >/dev/null 2>&1
log=$(cat "$T/paperless.log")
has "file: tags Korrespondenz when it exists" "$log" "--tag Korrespondenz"
hasnt "file: generic letter has no type" "$log" "--type"
rm -f "$T/paperless.log"
"$L" new --type kuendigung --to "$TO_SALT" --out "$T/out/f3.pdf" --draft --file >/dev/null 2>&1
[ -e "$T/paperless.log" ] && no "draft is never filed" "$(cat "$T/paperless.log")" || ok "draft is never filed"

rm -f "$REMIND_DB"
out=$("$L" new --type kuendigung --to "$TO_SALT" --contract "Mobile-Abo" --out "$T/out/r.pdf" --send-by 2026-10-28 2>&1)
has "send-by: reminder added" "$out" "reminder: added owner reminder for Mon 2026-10-26 09:00"
r=$(cat "$REMIND_DB")
has "send-by: owner reminder text" "$r" "Post letter «Kündigung Mobile-Abo» by Wed 28.10.2026 (send it Einschreiben / registered mail)"
has "send-by: tagged for cancelling" "$r" '"tag": "letter:kuendigung-mobile-abo"'
has "send-by: owner flag" "$r" '"owner": true'
rm -f "$REMIND_DB"
out=$("$L" new --type kuendigung --to "$TO_SALT" --contract "Mobile-Abo" --out "$T/out/r2.pdf" --send-by 2026-10-11 2>&1)
has "send-by tomorrow: reminder later today" "$out" "added owner reminder for Sat 2026-10-10 11:10"
"$L" new --type kuendigung --to "$TO_SALT" --contract "Mobile-Abo" --out "$T/out/r3.pdf" --send-by 2026-10-01 >/dev/null 2>&1
eq "send-by in the past: rc 2" "$?" "2"
[ -e "$T/tg.log" ] && no "nothing texted during tests" "$(cat "$T/tg.log")" || ok "nothing texted during tests"

echo "# E. pdfform: fields, fill roundtrip, flatten"
$PY "$HERE/make_form.py" "$T/form.pdf" "$T/plain.pdf"
out=$("$P" fields "$T/form.pdf")
has "fields: count" "$out" "8 fields, 1 pages"
has "fields: radio states" "$out" "[radio] Zahlung = '/Monatlich'"
has "fields: checkbox states" "$out" "states Yes"
has "fields: position in mm" "$out" "Name = ''  @ 60.0,38.0 mm"
eq "fields --json count" "$("$P" fields "$T/form.pdf" --json | $PY -c 'import json,sys; print(len(json.load(sys.stdin)))')" "8"
"$P" fields "$T/form.pdf" --template > "$T/tpl.json"
eq "fields --template keys" "$($PY -c 'import json,sys; print(",".join(json.load(open(sys.argv[1]))))' "$T/tpl.json")" \
   "Name,Vorname,Strasse,PLZOrt,Einverstanden,Zahlung,Kanton,Sprache"
cat > "$T/data.json" <<'EOF'
{"Name": "Doe", "Vorname": "Alex", "Strasse": "Examplestrasse 1", "PLZOrt": "1234 Exampletown",
 "Einverstanden": true, "Zahlung": "jaehrlich", "Kanton": "XX", "Sprache": "English",
 "_overlay": [{"page": 1, "x": 25, "y": 250, "text": "Exampletown, 10.10.2026", "size": 9}]}
EOF
out=$("$P" fill "$T/form.pdf" --data "$T/data.json" --out "$T/filled.pdf" 2>&1); rc=$?
eq "fill rc 0" "$rc" "0"
has "fill summary" "$out" "filled 8 field(s), 1 overlay(s)"
hasnt "fill: values read back as written" "$out" "not read back"
back=$("$P" fields "$T/filled.pdf" --json | $PY -c '
import json, sys
print(";".join("%s=%s" % (f["name"], f["value"]) for f in json.load(sys.stdin)))')
eq "fill roundtrip values" "$back" "Name=Doe;Vorname=Alex;Strasse=Examplestrasse 1;PLZOrt=1234 Exampletown;Einverstanden=/Yes;Zahlung=/Jaehrlich;Kanton=XX;Sprache=English"
has "overlay text present" "$(txt "$T/filled.pdf")" "Exampletown, 10.10.2026"
cp "$T/filled.pdf" "$SAMPLES/form-filled.pdf"
"$P" fill "$T/form.pdf" --data "$T/data.json" --out "$T/flat.pdf" --flatten >/dev/null 2>&1
has "flatten: no fields left" "$("$P" fields "$T/flat.pdf")" "no form fields"
t=$(txt "$T/flat.pdf")
for s in "Doe" "Examplestrasse 1" "1234 Exampletown" "XX"; do has "flatten keeps value: $s" "$t" "$s"; done
cp "$T/flat.pdf" "$SAMPLES/form-flattened.pdf"
echo '{"Nmae": "x"}' > "$T/bad.json"
out=$("$P" fill "$T/form.pdf" --data "$T/bad.json" --out "$T/x.pdf" 2>&1); rc=$?
eq "unknown field: rc 2" "$rc" "2"
has "unknown field: suggestion" "$out" "did you mean 'Name'"
"$P" fill "$T/form.pdf" --data "$T/bad.json" --out "$T/x.pdf" --ignore-unknown >/dev/null 2>&1
eq "--ignore-unknown: rc 0" "$?" "0"
echo '{"Zahlung": "woechentlich"}' > "$T/bad2.json"
out=$("$P" fill "$T/form.pdf" --data "$T/bad2.json" --out "$T/x.pdf" 2>&1); rc=$?
eq "bad radio value: rc 2" "$rc" "2"
has "bad radio value: lists states" "$out" "Monatlich, Jaehrlich"
echo '{"Einverstanden": "nein"}' > "$T/cb.json"
"$P" fill "$T/form.pdf" --data "$T/cb.json" --out "$T/cb.pdf" >/dev/null 2>&1
has "checkbox 'nein' -> Off" "$("$P" fields "$T/cb.pdf")" "Einverstanden = '/Off'"
"$P" fill "$T/form.pdf" --data "$T/data.json" --out "$T/form.pdf" >/dev/null 2>&1
eq "--out = input refused (rc 2)" "$?" "2"

echo "# F. pdfform: non-form PDFs (overlay), sign, text, grid"
out=$("$P" fill "$T/plain.pdf" --data "$T/data.json" --out "$T/x.pdf" 2>&1); rc=$?
eq "field data on a non-form PDF: rc 2" "$rc" "2"
has "hint to use _overlay" "$out" "_overlay"
cat > "$T/ov.json" <<'EOF'
[{"page": 1, "x": 30, "y": 52, "text": "Alex Doe", "size": 10},
 {"page": 1, "x": 26.5, "y": 61, "text": "X", "bold": true},
 {"page": 2, "x": 25, "y": 40, "text": "Zweite Seite ausgefüllt"}]
EOF
"$P" fill "$T/plain.pdf" --data "$T/ov.json" --out "$T/ov.pdf" >/dev/null 2>&1
eq "overlay rc 0" "$?" "0"
has "overlay page 1 text" "$(txt -f 1 -l 1 "$T/ov.pdf")" "Alex Doe"
has "overlay page 2 text" "$(txt -f 2 -l 2 "$T/ov.pdf")" "Zweite Seite ausgefüllt"
read -r x y <<< "$(pos "$T/ov.pdf" Alex)"
in_range "overlay x = 30 mm (85 pt)" "$x" 84 86.5
in_range "overlay baseline 52 mm (top of glyphs ≈ 140 pt)" "$y" 135 147
echo '[{"page": 3, "x": 1, "y": 1, "text": "x"}]' > "$T/ov3.json"
"$P" fill "$T/plain.pdf" --data "$T/ov3.json" --out "$T/x.pdf" >/dev/null 2>&1
eq "overlay page out of range: rc 2" "$?" "2"
out=$("$P" sign "$T/ov.pdf" --page 1 --pos 100,40 --out "$T/signed.pdf" 2>&1); rc=$?
eq "sign without signature image: rc 1" "$rc" "1"
has "sign without image: hint" "$out" "send a photo"
# relative image path: reportlab names the image after its path, so a temp path would change the sample's bytes
(cd "$T" && "$P" sign ov.pdf --page 1 --pos 100,40 --width 50 --image sig.png --out signed.pdf) >/dev/null 2>&1
eq "sign rc 0" "$?" "0"
eq "sign: image on page 1" "$(nimg -f 1 -l 1 "$T/signed.pdf")" "1"
LETTER_SIGNATURE=$T/sig.png "$P" sign "$T/filled.pdf" --page 1 --pos 110,240 --out "$T/filled-signed.pdf" >/dev/null 2>&1
eq "sign uses LETTER_SIGNATURE default" "$?" "0"
has "sign keeps form values" "$("$P" fields "$T/filled-signed.pdf")" "Name = 'Doe'"
cp "$T/signed.pdf" "$SAMPLES/plain-overlay-signed.pdf"
"$P" sign "$T/ov.pdf" --page 5 --pos 1,1 --image "$T/sig.png" --out "$T/x.pdf" >/dev/null 2>&1
eq "sign page out of range: rc 2" "$?" "2"
"$P" text "$T/plain.pdf" --page 2 --pos 25,60 --text "Ort, Datum: Exampletown" --out "$T/text.pdf" >/dev/null 2>&1
has "text command" "$(txt -f 2 -l 2 "$T/text.pdf")" "Ort, Datum: Exampletown"
"$P" grid "$T/plain.pdf" --page 1 --out "$T/grid.png" >/dev/null 2>&1
has "grid PNG" "$(head -c 4 "$T/grid.png" | tail -c 3)" "PNG"  # the PNG signature (no `file` in the CI image)
cp "$T/grid.png" "$SAMPLES/grid-page1.png"
"$P" grid "$T/plain.pdf" --out "$T/grid.pdf" >/dev/null 2>&1
eq "grid PDF keeps pages" "$(pages "$T/grid.pdf")" "2"

echo "# G. pdfform: --file and --send-by"
rm -f "$T/paperless.log" "$REMIND_DB"; touch "$T/has-tag"
out=$("$P" fill "$T/form.pdf" --data "$T/data.json" --out "$T/out/Antrag.pdf" --file --title "Antrag Test" --send-by 2026-10-20 2>&1)
has "pdfform --file uploads" "$(cat "$T/paperless.log")" "upload $T/out/Antrag.pdf --title Antrag Test --created 2026-10-10 --tag Korrespondenz"
has "pdfform --send-by reminder" "$(cat "$REMIND_DB")" "Send «Antrag Test» by post by Tue 20.10.2026"
has "pdfform reminder 2 days before" "$out" "for Sun 2026-10-18 09:00"

echo "# H. safety"
eq "real reminders DB untouched" "$({ md5sum < "$REAL_REM"; } 2>/dev/null)" "$rem_before"
eq "real signature state unchanged" "$([ -e "$REAL_SIG" ] && echo present || echo absent)" "$sig_before"
hasnt "letter never mails" "$(grep -E 'smtplib|sendmail|pingen|requests\.post' "$L" "$P" "$(dirname "$L")/_admindesk.py")" "smtp"

echo
echo "samples: $SAMPLES"
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
