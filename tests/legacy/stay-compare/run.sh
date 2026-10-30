#!/usr/bin/env bash
# Offline tests for ~/bin/stay-compare with a fictional trip (fixtures/data.json). SC=/copy tests a copy. Exit 0 = pass.
set -uo pipefail
SC=${SC:-$(cd "$(dirname "$0")/../../../scripts" && pwd)/stay-compare}
FIX=$(cd "$(dirname "$0")" && pwd)/fixtures
pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2" | head -20; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
has() { if [[ "$2" == *"$3"* ]]; then ok "$1"; else no "$1" "$2"; fi; }
hasnt() { if [[ "$2" == *"$3"* ]]; then no "$1" "$2"; else ok "$1"; fi; }
T=$(mktemp -d)
trap 'find "$T" -mindepth 1 -delete; rmdir "$T"' EXIT
export USAGE_STATE="$T/usage"
J() { python3 -c "import json,sys; m=json.load(sys.stdin); print(($1))"; }

echo "# arguments"
"$SC" --help >/dev/null; eq "--help rc 0" "$?" "0"
"$SC" >/dev/null 2>&1; eq "no args rc 1" "$?" "1"
"$SC" bogus "$FIX/data.json" >/dev/null 2>&1; eq "unknown verb rc 1" "$?" "1"
err=$("$SC" summary "$T/none.json" 2>&1); eq "missing data rc 1" "$?" "1"; has "missing data named" "$err" "cannot read"
printf '{"plans": []}' > "$T/thin.json"; "$SC" summary "$T/thin.json" >/dev/null 2>&1; eq "thin data rc 1" "$?" "1"

echo "# routes"
r=$("$SC" routes "$FIX/data.json")
eq "villa A: pick wins in S, cheapest free-cancel in N" "$(printf '%s' "$r" | J '[(x["options"], x["total"], x["pp"]) for x in m if x["plan"]=="a" and x["style"]=="villa"][0]')" "(['Casa <Uno>', 'Villa S'], 900, 450)"
eq "resort A: both stops, earliest free cancel" "$(printf '%s' "$r" | J '[(x["total"], x["free_cancel_until"], x["all_free"]) for x in m if x["plan"]=="a" and x["style"]=="resort"][0]')" "(1800, '2031-03-08', True)"
eq "guesthouse A: missing stop listed" "$(printf '%s' "$r" | J '[x["missing"] for x in m if x["plan"]=="a" and x["style"]=="guesthouse"][0]')" "['n']"
eq "mix A: villa N + resort S" "$(printf '%s' "$r" | J '[(x["options"], x["total"]) for x in m if x["plan"]=="a" and x["style"]=="mix"][0]')" "(['Casa <Uno>', 'Grand S'], 1200)"
eq "resort B: no B price in N -> missing" "$(printf '%s' "$r" | J '[x["missing"] for x in m if x["plan"]=="b" and x["style"]=="resort"][0]')" "['n']"
eq "pp per night (A villa: 900/2/4)" "$(printf '%s' "$r" | J '[x["pp_night"] for x in m if x["plan"]=="a" and x["style"]=="villa"][0]')" "112"

echo "# summary"
s=$("$SC" summary "$FIX/data.json")
eq "summary <= 8 lines" "$( [ "$(printf '%s\n' "$s" | wc -l)" -le 8 ] && echo yes)" "yes"
has "summary head: people + nights per stop" "$s" "🏝 Isla Test stays · 2 people · North 2 · South 2 nights"
has "summary villa line" "$s" "Villa / apartment: ~CHF 450 p.p. (Casa <Uno> + Villa S)"
hasnt "summary skips a route with a missing stop" "$s" "Guesthouse:"
has "summary mix line" "$s" "🔀 Mix: ~CHF 600 p.p."
has "summary book-by" "$s" "Book by 05.03 (permit)"
has "summary decision" "$s" "Your call: villa / resort / mix?"

echo "# page"
"$SC" render "$FIX/data.json" --out "$T/p.html" >/dev/null; eq "render rc 0" "$?" "0"
p=$(cat "$T/p.html")
has "page: plan chips" "$p" 'data-plan="b" aria-pressed="false">B 3/1</button>'
has "page: name escaped + linked" "$p" '<a href="https://example.invalid/uno">Casa &lt;Uno&gt;</a>'
hasnt "page: unsafe link dropped" "$p" "javascript:"
has "page: distance to jetty in metres" "$p" "⛴ North pier ~111 m"
has "page: distance to dive centre" "$p" "🤿 Reef Divers ~445 m"
has "page: non-refundable badge" "$p" "non-refundable"
has "page: deadline within 10 days is flagged" "$p" '<span class="b soon">free cancel to 08.03</span>'
has "page: later deadline is plain free" "$p" '<span class="b free">free cancel to 25.03</span>'
has "page: per person per night" "$p" "CHF 100 p.p./night"
has "page: no price for a plan" "$p" "no price for these dates"
has "page: unavailable list" "$p" "Sold Out Inn"
has "page: decision box" "$p" "<b>villa / resort / mix?</b>"
has "page: dark tokens" "$p" 'prefers-color-scheme: dark'
hasnt "page: no external resources" "$p" 'src="http'

echo "# combos"
python3 -c "import json,sys; d=json.load(open(sys.argv[1])); r=d['routes'][0]; r['why']={'n': 'near <the> pier', 's': 'beach'}; r['note']='best value'; r['recommended']=True; d['style_routes']=False; json.dump(d, open(sys.argv[2], 'w'))" "$FIX/data.json" "$T/combo.json"
r=$("$SC" routes "$T/combo.json")
eq "style_routes false: only the mix routes" "$(printf '%s' "$r" | J 'sorted(set(x["style"] for x in m))')" "['mix']"
eq "route legs: stop, nights, total" "$(printf '%s' "$r" | J '[(l["stop"], l["nights"], l["total"]) for l in m[0]["legs"]]')" "[('n', 2, 400), ('s', 2, 800)]"
"$SC" render "$T/combo.json" --out "$T/c.html" >/dev/null; eq "combo render rc 0" "$?" "0"
c=$(cat "$T/c.html")
has "combo card per plan" "$c" '<h2>Combos · '
has "combo reason escaped" "$c" 'near &lt;the&gt; pier'
has "combo recommended badge" "$c" '★ recommended'
has "combo note" "$c" 'best value'
has "combo cancel badge flags a near deadline" "$c" '<div class="badges"><span class="b soon">free cancel to 10.03</span>'

hasnt "no combo cards without why" "$p" '<h2>Combos'
hasnt "style_routes false: summary has no style line" "$("$SC" summary "$T/combo.json")" "Villa / apartment:"

echo "# trip-pack section"
x=$("$SC" section "$FIX/data.json")
eq "section id + groups" "$(printf '%s' "$x" | J 'm["id"], [g["title"] for g in m["groups"]]')" "('stays', ['North · Bay', 'South · Port'])"
eq "section: pick first with a star" "$(printf '%s' "$x" | J 'm["groups"][1]["table"]["rows"][0][0]')" "★ Villa S"
has "section: route rows" "$(printf '%s' "$x" | J 'm["rows"]')" "CHF 450 p.p."
has "usage: logged" "$(cat "$T"/usage/spool/*.jsonl 2>/dev/null)" '"bin.stay-compare"'

echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
