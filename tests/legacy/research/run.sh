#!/usr/bin/env bash
# Offline tests for ~/bin/research (new, render, summary, list). Made-up fixtures only. Exit 0 = all pass.
set -uo pipefail
R=${RESEARCH_BIN:-$(cd "$(dirname "$0")/../../../scripts" && pwd)/research}
FX=$(cd "$(dirname "$0")" && pwd)/fixtures
T=$(mktemp -d)
trap 'rm -rf "${T:?}"' EXIT
export RESEARCH_DIR="$T/root" USAGE_STATE="$T/usage"
mkdir -p "$RESEARCH_DIR"
pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2" | head -20; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
has() { if [[ "$2" == *"$3"* ]]; then ok "$1"; else no "$1" "$2"; fi; }
hasnt() { if [[ "$2" == *"$3"* ]]; then no "$1" "$(printf '%s' "$2" | grep -o ".\{0,40\}$3.\{0,40\}" | head -3)"; else ok "$1"; fi; }
J() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }

echo "# help"
out=$("$R" --help); eq "help rc" "$?" "0"; has "help names render" "$out" "research render MD"

echo "# new"
out=$("$R" new "What are the best travel eSIMs for Iceland?" --date 2026-01-02 --json); rc=$?
eq "new rc" "$rc" "0"
id=$(printf '%s' "$out" | J 'd["id"]')
eq "new id = date + slug without stopwords" "$id" "2026-01-02-travel-esims-iceland"
[ -d "$RESEARCH_DIR/work/$id/notes" ] && ok "notes dir made" || no "notes dir made"
[ -f "$RESEARCH_DIR/$id.md" ] && ok "skeleton md made" || no "skeleton md made"
id2=$("$R" new "What are the best travel eSIMs for Iceland?" --date 2026-01-02 --json | J 'd["id"]')
eq "second run gets a unique id" "$id2" "2026-01-02-travel-esims-iceland-2"
id3=$("$R" new "x" --slug "Ünïcode Slug, Test!" --date 2026-01-03 --json | J 'd["id"]')
eq "--slug ascii-folds" "$id3" "2026-01-03-unicode-slug-test"
"$R" new >/dev/null 2>&1; eq "new without question rc 2" "$?" "2"
"$R" render "$RESEARCH_DIR/$id.md" >/dev/null 2>&1; eq "untouched skeleton fails validation (rc 3)" "$?" "3"
[ ! -f "$RESEARCH_DIR/$id.html" ] && ok "failed render writes no html" || no "failed render writes no html"

echo "# render"
cp "$FX/report.md" "$RESEARCH_DIR/2026-01-02-widgets.md"
out=$("$R" render "$RESEARCH_DIR/2026-01-02-widgets.md" --json); rc=$?
eq "render rc" "$rc" "0"
H="$RESEARCH_DIR/2026-01-02-widgets.html"
[ -f "$H" ] && ok "html written next to md" || no "html written next to md"
page=$(cat "$H" 2>/dev/null)
eq "sources deduped by normalized URL (www, utm, slash, fragment)" "$(printf '%s' "$out" | J 'd["sources"]')" "3"
eq "citations counted" "$(printf '%s' "$out" | J 'd["citations"]')" "5"
eq "domains" "$(printf '%s' "$out" | J 'd["domains"]')" "3"
eq "confidence from front matter" "$(printf '%s' "$out" | J 'd["confidence"]')" "medium"
eq "pills counted" "$(printf '%s' "$out" | J 'd["pills"]["high"]')" "2"
eq "no warnings" "$(printf '%s' "$out" | J 'len(d["warnings"])')" "0"
has "title from front matter" "$page" "<h1>Example widgets compared</h1>"
hasnt "front-matter title wins over # heading" "$page" "Ignored heading"
has "bottom line box" "$page" 'class="bluf" id="bottom-line"'
has "group citation -> two numbers" "$page" '<sup class="cite"><a href="#s1">1</a>,<a href="#s3">3</a></sup>'
has "descriptive link keeps its text" "$page" 'Maker B<sup class="cite"><a href="#s2">2</a></sup>'
hasnt "generic 'Source' text dropped" "$page" '>Source<'
has "source list uses descriptive label" "$page" '>Shop review</a>'
has "source cited count" "$page" 'cited 2×'
has "raw html escaped" "$page" '&lt;script&gt;alert(1)&lt;/script&gt;'
hasnt "no live script tag" "$page" '<script'
has "code span untouched" "$page" '<code>[x](https://not.a.source.example)</code>'
has "table wrapped for phones" "$page" '<div class="tw"><table>'
has "high pill" "$page" '<span class="conf c-high">high</span>'
has "nav pill per section" "$page" 'href="#widget-cheapest-per-unit"'
has "method section has assumptions" "$page" 'home use only; prices as listed in January'
has "dark mode tokens" "$page" 'prefers-color-scheme: dark'
hasnt "no external stylesheet/script" "$page" '<link'
hasnt "no external src" "$page" ' src="http'

echo "# sources enrichment"
out=$("$R" render "$RESEARCH_DIR/2026-01-02-widgets.md" --sources "$FX/sources.json" --json)
page=$(cat "$H")
has "title from sources json" "$page" '>Maker B lifetime data</a>'
has "publisher + date + type line" "$page" 'Maker B · 2025-12-01 · vendor'
mkdir -p "$RESEARCH_DIR/work/2026-01-02-widgets" && cp "$FX/sources.json" "$RESEARCH_DIR/work/2026-01-02-widgets/sources.json"
"$R" render "$RESEARCH_DIR/2026-01-02-widgets.md" >/dev/null
has "work/<id>/sources.json picked up by default" "$(cat "$H")" '>Maker B lifetime data</a>'
"$R" render "$RESEARCH_DIR/2026-01-02-widgets.md" --sources "$T/missing.json" >/dev/null 2>&1
eq "missing --sources rc 2" "$?" "2"

echo "# check + validation"
rm -f "$H"
"$R" render "$RESEARCH_DIR/2026-01-02-widgets.md" --check >/dev/null; eq "--check rc 0" "$?" "0"
[ ! -f "$H" ] && ok "--check writes nothing" || no "--check writes nothing"
out=$("$R" render "$FX/nocite.md" --out "$T/x.html" 2>&1); rc=$?
eq "no citation -> rc 3" "$rc" "3"; has "names the error" "$out" "no cited source"
"$R" render "$T/nope.md" >/dev/null 2>&1; eq "missing md rc 2" "$?" "2"

echo "# summary"
s=$("$R" summary "$RESEARCH_DIR/2026-01-02-widgets.md")
eq "summary <= 8 lines" "$( [ "$(printf '%s\n' "$s" | wc -l)" -le 8 ] && echo yes)" "yes"
has "summary title line" "$s" "🔎 Example widgets compared"
has "summary bottom line, citations stripped" "$s" "Widget A wins on price by 30 % and Widget B on lifetime per Maker B."
has "summary bullets from front matter" "$s" "• Widget B lasts longest"
has "summary stats" "$s" "Confidence: medium · 3 sources · 12 min"
sed -i '/^summary:/,/^---/{/^  - /d}' "$RESEARCH_DIR/2026-01-02-widgets.md"
s=$("$R" summary "$RESEARCH_DIR/2026-01-02-widgets.md")
has "no summary list -> signpost headers" "$s" "• Widget B lasts twice as long"
hasnt "signposts skip 'How sure'" "$s" "How sure"

echo "# list"
"$R" render "$RESEARCH_DIR/2026-01-02-widgets.md" >/dev/null
echo "# not a report" > "$RESEARCH_DIR/notes-2026.md"
out=$("$R" list --json)
eq "list counts date-prefixed reports only" "$(printf '%s' "$out" | J 'len(d)')" "4"
has "list has the rendered report" "$(printf '%s' "$out" | J '[r for r in d if r["id"]=="2026-01-02-widgets"][0]["html"]')" "2026-01-02-widgets.html"
out=$("$R" list); has "list text shows title" "$out" "Example widgets compared  · medium · 3 src"

echo "# usage"
sp=$(cat "$USAGE_STATE"/spool/*.jsonl 2>/dev/null)
has "start logged" "$sp" '"k":"invoke"'
has "render logged with counts" "$sp" '"step":"render"'
USAGE=off RESEARCH_DIR="$T/root" "$R" new "usage off" --date 2026-01-04 >/dev/null

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
