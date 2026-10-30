#!/usr/bin/env bash
# Offline tests for ~/bin/trip-pack: fictional trips/places (fixtures/), a fake calcache, network answers from
# fixtures/net (TRIP_PACK_FIXTURES), a placeholder profile. Exit 0 = all pass. TRIP_PACK=/copy tests a copy.
set -uo pipefail
TP=${TRIP_PACK:-$(cd "$(dirname "$0")/../../../scripts" && pwd)/trip-pack}
FIX=$(cd "$(dirname "$0")" && pwd)/fixtures
pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2" | head -20; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
has() { if [[ "$2" == *"$3"* ]]; then ok "$1"; else no "$1" "$2"; fi; }
hasnt() { if [[ "$2" == *"$3"* ]]; then no "$1" "$2"; else ok "$1"; fi; }
T=$(mktemp -d)
trap 'find "$T" -mindepth 1 -delete; rmdir "$T"' EXIT
mkdir -p "$T/bin" "$T/net" "$T/out"
cp "$FIX"/net/*.json "$T/net/"
cat > "$T/bin/calcache" <<'EOF'
#!/usr/bin/env bash
[ -n "${FAKE_CAL_FAIL:-}" ] && { echo "calcache: broken" >&2; exit 1; }
[ -n "${FAKE_CAL_NONE:-}" ] && { echo "no cache" >&2; exit 3; }
case "$1" in
  age) printf '{"cached": true, "hours": 2, "window": ["2031-02-10", "%s"]}\n' "${FAKE_CAL_END:-2031-03-31}" ;;
  show) cat "$FAKE_CAL_EVENTS" ;;
  *) exit 2 ;;
esac
EOF
chmod +x "$T/bin/calcache"
export ASSISTANT_PROFILE="$FIX/profile.json" TRIP_PACK_TRIPS="$FIX/trips.json" TRIP_PACK_TEMPLATES="$FIX/templates.json" \
       TRIP_PACK_BIN="$T/bin" TRIP_PACK_FIXTURES="$T/net" TRIP_PACK_CITIES="$FIX/cities.json" TRIP_PACK_COUNTRIES="$FIX/countries.json" TRIP_PACK_CACHE="$T/cache" \
       TRIP_PACK_OUTDIR="$T/out" TRIP_PACK_EXTRAS_DIR="$T/no-extras" USAGE_STATE="$T/usage" FAKE_CAL_EVENTS="$FIX/events.json" TRIP_PACK_NOW="2031-02-20 09:00"
J() { python3 -c "import json,sys; m=json.load(sys.stdin); print(($1))"; }

echo "# arguments"
"$TP" --help >/dev/null; eq "--help rc 0" "$?" "0"
"$TP" >/dev/null 2>&1; eq "no args rc 1" "$?" "1"
err=$("$TP" atlantis 2>&1 >/dev/null); rc=$?
eq "unknown trip rc 1" "$rc" "1"; has "unknown trip says so" "$err" "no trip matches"
err=$("$TP" nordby 2>&1 >/dev/null); rc=$?
eq "dateless trip rc 1" "$rc" "1"; has "dateless trip says no dates" "$err" "has no dates"
"$TP" velmora --bogus >/dev/null 2>&1; eq "unknown option rc 1" "$?" "1"
list=$("$TP" --list)
has "--list: dated trip" "$list" "🎪 Velmora Fair · Wed 12.–Thu 13.3."
hasnt "--list: dateless trip left out" "$list" "Nordby"

echo "# far trip: forecast note, no legs yet (calendar has none in the window)"
out=$(FAKE_CAL_EVENTS=/dev/null "$TP" velmora); rc=$?
eq "far: rc 0" "$rc" "0"
has "far: summary line names the default out" "$out" "$T/out/velmora-fair-pack.html"
has "far: summary says legs not booked" "$out" "legs not booked yet"
[ -s "$T/out/velmora-fair-pack.html" ] && ok "far: page written to OUTDIR/<slug>-pack.html" || no "far: page written"
m=$(FAKE_CAL_EVENTS=/dev/null "$TP" velmora --json --out "$T/out/far.html" 2>&1)
eq "far: forecast notes per place" "$(printf '%s' "$m" | J '[(w["town"], w["note"], len(w["days"])) for w in m["weather"]]')" \
   "[('Velmora', 'Forecast from 26.02', 0), ('Castelluna', 'Forecast from 27.02', 0)]"
eq "far: legs note" "$(printf '%s' "$m" | J 'm["legs_note"], m["legs"]')" "('Legs not booked yet', [])"
eq "far: planned legs from the trip file" "$(printf '%s' "$m" | J 'm["planned"]')" "['✈️ Thu 13.3 Velmora → Castelluna (not booked yet)']"
eq "far: home town from the profile" "$(printf '%s' "$m" | J 'm["home_town"]')" "Testdorf"
eq "far: EUR with the fixture rate" "$(printf '%s' "$m" | J 'm["money"]')" \
   "[{'currency': 'EUR', 'rate': 1.0612, 'rate_date': '2031-03-04', 'base': 'CHF'}]"
has "far: roaming reminder (home plan outside the EU)" "$(printf '%s' "$m" | J 'm["roaming"]')" "Check your plan"
eq "far: emergency 112 + country line" "$(printf '%s' "$m" | J '[e["where"] for e in m["emergency"]]')" "['Europe', 'Belgium', 'Italy']"
eq "far: plugs per country" "$(printf '%s' "$m" | J '[(p["where"], p["type"]) for p in m["plugs"]]')" "[('Belgium', 'E'), ('Italy', 'L / F')]"
tr=$(printf '%s' "$m" | J '[x["label"].split(" ")[0] for x in m["transport"]]')
has "far: national rail (BE)" "$tr" "'SNCB/NMBS'"
has "far: national rail (IT)" "$tr" "'Trenitalia'"
has "far: city table from TRIP_PACK_CITIES" "$tr" "'Velmora'"
eq "far: packing = extras the template lacks + template" "$(printf '%s' "$m" | J 'm["packing"]["type"], m["packing"]["items"]')" \
   "('business', ['🛂 Passport or ID card', '💶 Some EUR cash', '🔌 Plug adapter (type E / L / F)', '🪥 Toothbrush', '👔 Smart shirt', '💻 Laptop + charger', '🪪 Badge'])"
eq "far: no problems" "$(printf '%s' "$m" | J 'm["problems"]')" "[]"
page=$(cat "$T/out/far.html")
has "page: doctype" "${page:0:15}" "<!doctype html>"
for id in where legs transport weather money sos pack; do has "page: section $id" "$page" "<section id=\"$id\">"; done
has "page: legs not booked note" "$page" "Legs not booked yet"
has "page: forecast note" "$page" "Forecast from 26.02"
has "page: dark mode tokens" "$page" 'prefers-color-scheme: dark'
hasnt "page: no external scripts" "$page" "<script src"
hasnt "page: no external stylesheets" "$page" "<link "
has "page: checklist keeps ticks in localStorage, guarded" "$page" "try{localStorage.setItem"
has "page: door to door from the profile town" "$page" "Door to door from and back to Testdorf"

echo "# near trip: forecast + calendar legs"
m=$(TRIP_PACK_NOW="2031-03-05 09:00" "$TP" velmora --json --out "$T/out/near.html"); rc=$?
eq "near: rc 0" "$rc" "0"
eq "near: forecast rows for Velmora" "$(printf '%s' "$m" | J '[(d["date"], d["icon"], d["text"], d["tmax"]) for d in m["weather"][0]["days"]]')" \
   "[('2031-03-12', '🌧', 'rain', 11.6), ('2031-03-13', '🌤', 'partly cloudy', 14.2)]"
eq "near: geocoded place gets a forecast too" "$(printf '%s' "$m" | J 'len(m["weather"][1]["days"]), m["weather"][1]["note"]')" "(2, None)"
eq "near: legs = edge-day leg naming the trip + flight; unrelated drive out" \
   "$(printf '%s' "$m" | J '[(g["when"], g["title"]) for g in m["legs"]]')" \
   "[('Tue 11.03 19:10–21:40', '🚆 Home → Velmora'), ('Thu 13.03 18:40–20:05', '✈️ Velmora → Castelluna')]"
eq "near: legs note gone" "$(printf '%s' "$m" | J 'm["legs_note"]')" "None"
eq "near: the trip's own calendar event, not dinner at home" "$(printf '%s' "$m" | J '[e["title"] for e in m["calendar"]]')" "['🎪 Fair visit (Velmora)']"
page=$(cat "$T/out/near.html")
has "page: forecast table" "$page" "<td>4–12</td>"
has "page: leg row" "$page" "✈️ Velmora → Castelluna"
hasnt "page: no not-booked note once legs exist" "$page" "Legs not booked yet"
eq "near: geocode not cached under fixtures" "$(ls "$T/cache" 2>/dev/null | wc -l)" "0"

echo "# failures and offline"
out=$(TRIP_PACK_NOW="2031-03-05 09:00" FAKE_CAL_FAIL=1 "$TP" velmora --out "$T/out/f1.html" 2>"$T/err"); rc=$?
eq "calcache broken: rc 2" "$rc" "2"
has "calcache broken: named on stderr" "$(cat "$T/err")" "calendar: calcache failed"
has "calcache broken: page still written + Not checked section" "$(cat "$T/out/f1.html")" "<section id=\"issues\">"
m=$(TRIP_PACK_NOW="2031-03-05 09:00" FAKE_CAL_NONE=1 "$TP" velmora --json --out "$T/out/f2.html"); rc=$?
eq "no calendar cache: rc 0, note" "$rc $(printf '%s' "$m" | J 'm["calendar_note"]')" "0 Calendar not cached"
m=$(FAKE_CAL_END=2031-03-12 "$TP" velmora --json --out "$T/out/f3.html")
eq "cache window ends inside the trip: note" "$(printf '%s' "$m" | J 'm["calendar_note"]')" "Calendar cached only up to 12.03"
rm -f "$T/net/forecast.json"
m=$(TRIP_PACK_NOW="2031-03-05 09:00" "$TP" velmora --json --out "$T/out/f4.html" 2>/dev/null); rc=$?
eq "forecast down: rc 2" "$rc" "2"
eq "forecast down: note per place" "$(printf '%s' "$m" | J '[w["note"] for w in m["weather"]]')" \
   "['Forecast unavailable when built', 'Forecast unavailable when built']"
rm -f "$T/net/fx.json" "$T/net/geocode.json"
m=$(TRIP_PACK_NOW="2031-03-05 09:00" "$TP" velmora --json --no-fetch --out "$T/out/f5.html"); rc=$?
eq "--no-fetch: rc 0 (nothing fetched)" "$rc" "0"
eq "--no-fetch: offline notes, no rate" "$(printf '%s' "$m" | J '[w["note"] for w in m["weather"]], m["money"][0]["rate"]')" \
   "(['No forecast (built offline)', 'No forecast (built offline)'], None)"
page=$("$TP" velmora --no-fetch --out -); rc=$?
eq "--out -: rc 0" "$rc" "0"
has "--out -: page on stdout" "${page:0:15}" "<!doctype html>"

echo "# domestic day trip, escaping, trip-file pack path"
m=$("$TP" "lake" --json --no-fetch --out "$T/out/lake.html")
eq "domestic: no roaming, money in home currency, no extras" \
   "$(printf '%s' "$m" | J 'm["roaming"], [x["currency"] for x in m["money"]], m["packing"]["items"]')" "(None, ['CHF'], [])"
has "domestic: dates of a one-day trip" "$(printf '%s' "$m" | J 'm["dates"]')" "Wed 2.4."
page=$(cat "$T/out/lake.html")
has "escaping: name escaped" "$page" "Lake &lt;Days&gt;"
hasnt "escaping: no raw tag" "$page" "<Days>"
python3 - "$FIX/trips.json" "$T/trips2.json" "$T/out/custom/v.html" <<'EOF'
import json, sys
d = json.load(open(sys.argv[1])); d["trips"][0]["pack"] = sys.argv[3]; json.dump(d, open(sys.argv[2], "w"))
EOF
TRIP_PACK_TRIPS="$T/trips2.json" "$TP" castelluna --no-fetch >/dev/null; rc=$?
eq "match word finds the trip; its pack path is the default out" "$rc $([ -s "$T/out/custom/v.html" ] && echo written)" "0 written"

echo "# trip outside Europe: private country table (TRIP_PACK_COUNTRIES), no reference rate, 112 line, roaming"
python3 - "$T/trips3.json" <<'PY'
import json, sys
json.dump({"trips": [{"name": "Corvina Dive", "from": "2031-05-10", "to": "2031-05-20", "abroad": True,
                      "places": [{"town": "Porto Ilha", "country": "xq", "lat": -4.6, "lon": 55.4}]}]}, open(sys.argv[1], "w"))
PY
m=$(TRIP_PACK_TRIPS="$T/trips3.json" "$TP" corvina --json --out "$T/out/corvina.html"); rc=$?
eq "non-Europe: rc 0 (no fx fetch for a currency without an ECB rate)" "$rc" "0"
eq "non-Europe: currency with its note, no rate" "$(printf '%s' "$m" | J '[(x["currency"], x["rate"], x.get("note")) for x in m["money"]]')" \
   "[('QRD', None, 'no reference rate')]"
eq "non-Europe: 112 line + private emergency numbers" "$(printf '%s' "$m" | J '[x["where"] for x in m["emergency"]]')" \
   "['Mobile', 'Corvina Isles']"
has "non-Europe: roaming note" "$(printf '%s' "$m" | J 'm["roaming"]')" "outside Europe"
has "non-Europe: private transport links" "$(printf '%s' "$m" | J '[x["label"] for x in m["transport"]]')" "Corvina inter-island ferries"
eq "non-Europe: plugs from the private table" "$(printf '%s' "$m" | J 'm["plugs"]')" \
   "[{'where': 'Corvina Isles', 'type': 'G'}]"
has "non-Europe: cash extra in the local currency" "$(printf '%s' "$m" | J 'm["packing"]["items"]')" "Some QRD cash"
has "non-Europe: page shows the rate note" "$(cat "$T/out/corvina.html")" "no reference rate"
echo "# reference rate for a non-Europe currency (Frankfurter v2 /rate/BASE/QUOTE)"
mkdir -p "$T/net2"; cp "$FIX"/net/*.json "$T/net2/"
printf '%s\n' '{"date": "2031-05-08", "base": "CHF", "quote": "QRD", "rate": 17.79843}' > "$T/net2/fx.json"
python3 - "$FIX/countries.json" "$T/countries-fx.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1])); del d["XQ"]["fx"]; json.dump(d, open(sys.argv[2], "w"))
PY
m=$(TRIP_PACK_FIXTURES="$T/net2" TRIP_PACK_COUNTRIES="$T/countries-fx.json" TRIP_PACK_TRIPS="$T/trips3.json" "$TP" corvina --json --out "$T/out/corvina-fx.html"); rc=$?
eq "fx on: rc 0" "$rc" "0"
eq "fx on: QRD rate from the v2 answer" "$(printf '%s' "$m" | J 'm["money"]')" \
   "[{'currency': 'QRD', 'rate': 17.7984, 'rate_date': '2031-05-08', 'base': 'CHF'}]"
has "fx on: page shows the rate" "$(cat "$T/out/corvina-fx.html")" "1 CHF ≈ 17.7984 QRD"
has "fx on: page names the reference rate date" "$(cat "$T/out/corvina-fx.html")" "reference rate 2031-05-08"
printf '%s\n' '{"date": "2031-05-08", "base": "CHF", "quote": "EUR", "rate": 1.08}' > "$T/net2/fx.json"
m=$(TRIP_PACK_FIXTURES="$T/net2" TRIP_PACK_COUNTRIES="$T/countries-fx.json" TRIP_PACK_TRIPS="$T/trips3.json" "$TP" corvina --json --out "$T/out/corvina-x.html" 2>/dev/null); rc=$?
eq "fx answer for another quote: rc 2, no rate" "$rc $(printf '%s' "$m" | J 'm["money"][0]["rate"]')" "2 None"
has "fx answer for another quote: problem named" "$(printf '%s' "$m" | J 'm.get("problems")')" "fx: no rate for QRD"
m=$("$TP" velmora --json --no-fetch --out "$T/out/v2.html")
eq "Europe: 112 line stays Europe, no private entries leak in" "$(printf '%s' "$m" | J '[x["where"] for x in m["emergency"]][0], m["roaming"] is not None')" "('Europe', True)"

echo "# extras: day by day, own sections, emergency lines first"
m=$(TRIP_PACK_EXTRAS_DIR="$FIX/extras" "$TP" velmora --json --no-fetch --out "$T/out/ex.html" 2>"$T/ex.err"); rc=$?
eq "extras: missing include -> rc 2" "$rc" "2"
has "extras: missing include named" "$(cat "$T/ex.err")" "extras: missing-file.json unreadable"
eq "extras: days sorted, bad ones dropped" "$(printf '%s' "$m" | J '[d["date"] for d in m["days"]]')" "['2031-03-12', '2031-03-13']"
eq "extras: empty day items dropped" "$(printf '%s' "$m" | J 'm["days"][0]["items"]')" "['Hall 3 at 09:00']"
eq "extras: section ids (builtin id prefixed, include read, empty dropped)" "$(printf '%s' "$m" | J '[x["id"] for x in m["extras"]]')" "['stays', 'x-legs', 'ferry']"
eq "extras: unsafe link dropped" "$(printf '%s' "$m" | J 'm["extras"][0]["groups"][0]["links"]')" "[['Hotel Ardent page', 'https://example.invalid/ardent']]"
eq "extras: emergency lines first, empty one dropped" "$(printf '%s' "$m" | J '[x["where"] for x in m["emergency"]][:2]')" "['Fair', 'Europe']"
page=$(cat "$T/out/ex.html")
has "extras: days section titled" "$page" '<section id="days"><h2>Fair days</h2>'
has "extras: day carries its date for the today marker" "$page" 'data-date="2031-03-12"'
has "extras: day title escaped" "$page" "Fair &lt;day&gt; one"
has "extras: tag shown" "$page" '<span class=tag>travel</span>'
has "extras: group table rendered" "$page" "<td>Pension Lys</td>"
has "extras: include rendered with note" "$page" '<p class="note">Book by 01.03</p>'
has "extras: tel link kept" "$page" 'href="tel:+0000000"'
hasnt "extras: no javascript link" "$page" "javascript:"
has "extras: item escaped" "$page" "Call &lt;office&gt;"
has "extras: nav lists the extra section" "$page" '<a href="#stays">Stays shortlist</a>'
has "extras: today marker script" "$page" "Jump to today"
printf '{"days_title": "Day by day (plan A)", "days": [{"date": "2031-03-12", "title": "x"}]}' > "$T/badx2.json"; mkdir -p "$T/nx2"; cp "$T/badx2.json" "$T/nx2/velmora-fair.json"
has "extras: nav label drops the bracket" "$(TRIP_PACK_EXTRAS_DIR="$T/nx2" "$TP" velmora --no-fetch --out - 2>/dev/null)" '<a href="#days">Day by day</a>'
order=$(python3 -c "import re,sys;print(' '.join(re.findall(r'<section id=\"([^\"]+)\"', open(sys.argv[1]).read())))" "$T/out/ex.html")
has "extras: days + sections sit after Legs, before Getting around" "$order" "legs days stays x-legs ferry transport"
mkdir -p "$T/badx"; printf '{oops' > "$T/badx/velmora-fair.json"
out=$(TRIP_PACK_EXTRAS_DIR="$T/badx" "$TP" velmora --no-fetch --out "$T/out/bx.html" 2>&1); rc=$?
eq "extras: broken file -> rc 2, page still written" "$rc $([ -s "$T/out/bx.html" ] && echo yes)" "2 yes"
has "extras: broken file named" "$out" "extras: velmora-fair.json unreadable"
m=$("$TP" velmora --json --no-fetch --out "$T/out/nx.html" 2>/dev/null)
eq "extras: none -> no days, no sections" "$(printf '%s' "$m" | J 'len(m["days"]), len(m["extras"])')" "(0, 0)"
has "usage: each build logs bin.trip-pack" "$(cat "$T"/usage/spool/*.jsonl 2>/dev/null)" '"bin.trip-pack"'
n=$(cat "$T"/usage/spool/*.jsonl 2>/dev/null | wc -l)
USAGE=off "$TP" velmora --no-fetch --out "$T/out/off.html" >/dev/null 2>&1
eq "usage: USAGE=off writes nothing" "$(cat "$T"/usage/spool/*.jsonl 2>/dev/null | wc -l)" "$n"

echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
