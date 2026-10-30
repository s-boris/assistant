#!/usr/bin/env bash
# Offline tests for ~/bin/trackinfo (BPM/key/energy from Deezer previews + DJ set ordering).
# Fake Deezer (fake_deezer.py) serving synthetic tracks made by synth.py; fictional artists only.
# TRACKINFO_BIN=/copy tests a copy. Exit 0 = all pass.
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
TI=${TRACKINFO_BIN:-$(cd "$(dirname "$0")/../../../scripts" && pwd)/trackinfo}
VPY=${TRACKINFO_PY:-$HOME/opt/trackinfo-venv/bin/python3}  # as trackinfo finds it (a temp HOME links ~/opt)
T=$(mktemp -d); pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2" | head -20; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
has() { if [[ "$2" == *"$3"* ]]; then ok "$1"; else no "$1" "$2"; fi; }
hasnt() { if [[ "$2" == *"$3"* ]]; then no "$1" "$2"; else ok "$1"; fi; }

"$VPY" "$HERE/synth.py" "$T/am124.wav" 124 9 1 30
"$VPY" "$HERE/synth.py" "$T/c128.wav" 128 0 0 30
"$VPY" "$HERE/synth.py" "$T/em90.wav" 90 4 1 30
"$VPY" "$HERE/synth.py" "$T/local.wav" 120 7 0 90
cat > "$T/ctrl.json" <<'J'
{"tracks": [{"id": 11, "artist": "Test Artist", "title": "Night Drive", "bpm": 0, "file": "am124.wav"},
            {"id": 12, "artist": "Low Tide", "title": "Slow Burn", "bpm": 127.9, "file": "c128.wav"},
            {"id": 13, "artist": "Dub Unit", "title": "Half Step (Radio Edit)", "bpm": 180, "file": "em90.wav"}]}
J
python3 "$HERE/fake_deezer.py" "$T" & SRV=$!
trap 'kill $SRV 2>/dev/null; find "$T" -mindepth 1 -delete; rmdir "$T"' EXIT
for _ in $(seq 50); do [ -s "$T/port" ] && break; sleep 0.1; done
export TRACKINFO_API="http://127.0.0.1:$(cat "$T/port")" TRACKINFO_STATE="$T/state" TRACKINFO_RPS=5
J() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }

# --- list parsing
cat > "$T/list.txt" <<'L'
# a comment
## warmup
1. Alpha - One | 100 | 8a | 3
Bravo – Two | 102 | Am
## Notes for later
floor: Charlie - Three
peak: Delta feat. Echo - Four (Extended Mix)
L
out=$("$TI" parse --file "$T/list.txt")
eq "parse: 4 entries" "$(echo "$out" | J 'len(d)')" 4
eq "parse: numbering stripped, phase header" "$(echo "$out" | J 'd[0]["artist"]+"|"+d[0]["phase"]+"|"+d[0]["given_camelot"]')" "Alpha|warmup|8A"
eq "parse: en dash split, classic key -> Camelot" "$(echo "$out" | J 'd[1]["title"]+"|"+d[1]["given_camelot"]')" "Two|8A"
eq "parse: non-phase ## is a comment, prefix sets phase" "$(echo "$out" | J 'd[2]["phase"]+"|"+d[3]["phase"]')" "floor|peak"
printf '#\tTrack Title\tArtist\tBPM\tKey\n1\tNight Drive\tTest Artist\t124.00\t8A\n2\tSlow Burn\tLow Tide\t128.00\tC\n' \
  | iconv -f utf-8 -t utf-16 > "$T/rekordbox.txt"
out=$("$TI" parse --file "$T/rekordbox.txt")
eq "parse: UTF-16 Rekordbox TSV export with BPM/Key columns" "$(echo "$out" | J '[(e["artist"],e["given_bpm"],e["given_camelot"]) for e in d]')" "[('Test Artist', 124.0, '8A'), ('Low Tide', 128.0, '8B')]"
printf '#EXTM3U\n#EXTINF:90,Local Artist - Local Track\n%s\n#EXTINF:200,Remote - Song\nhttp://x/y.mp3\n' "$T/local.wav" > "$T/pl.m3u"
out=$("$TI" parse --file "$T/pl.m3u")
eq "parse: M3U local file + EXTINF name" "$(echo "$out" | J 'd[0]["artist"]+"|"+str("path" in d[0])+"|"+d[1]["title"]')" "Local Artist|True|Song"

# --- analysis via the fake Deezer
out=$("$TI" --json "Test Artist - Night Drive" "Low Tide - Slow Burn" "Dub Unit - Half Step" "Nobody - Nothing")
rc=$?
eq "analyse: exit 0 when some tracks are found" "$rc" 0
eq "analyse: 124 BPM within 1.5 %" "$(echo "$out" | J 'abs(d[0]["bpm"]-124)/124 < 0.015')" True
eq "analyse: A minor progression -> 8A" "$(echo "$out" | J 'd[0]["camelot"]+" "+d[0]["key"]')" "8A Am"
eq "analyse: C major progression -> 8B" "$(echo "$out" | J 'd[1]["camelot"]')" "8B"
eq "analyse: agrees with Deezer BPM" "$(echo "$out" | J 'abs(d[1]["bpm"]-127.9) < 1.5 and d[1]["bpm_agree"] < 0.02')" True
eq "analyse: Deezer 180 + local 90 -> display octave 90" "$(echo "$out" | J 'abs(d[2]["bpm"]-90) < 1.5 and d[2]["bpm_deezer"] == 180')" True
eq "analyse: E minor -> 9A" "$(echo "$out" | J 'd[2]["camelot"]')" "9A"
eq "analyse: energy 1-10" "$(echo "$out" | J 'all(1 <= x["energy"] <= 10 for x in d[:3])')" True
eq "analyse: unknown track reported" "$(echo "$out" | J 'd[3].get("error")')" "not found on Deezer"
"$TI" "Nobody - Nothing" >/dev/null 2>&1; eq "analyse: exit 1 when nothing is found" "$?" 1
n1=$(wc -l < "$T/server.log")
out=$("$TI" "Test Artist - Night Drive" "Low Tide - Slow Burn")
eq "cache: second run makes no requests" "$(wc -l < "$T/server.log")" "$n1"
has "text output: table row" "$out" "8A"
eq "rate limit: <= 5 requests in any 1 s window" "$(python3 -c "
import sys; ts=[float(l.split()[0]) for l in open('$T/server.log')]
print(max(sum(1 for u in ts if t <= u < t + 1) for t in ts) <= 5)")" True
"$TI" forget "Test Artist - Night Drive" >/dev/null
out=$("$TI" show --json "Test Artist - Night Drive"); eq "forget + show: offline, no analysis" "$(echo "$out" | J 'd[0]["error"]')" "no analysis (offline)"
out=$("$TI" --json --file "$T/pl.m3u" --offline); eq "offline: no requests for uncached" "$(wc -l < "$T/server.log")" "$n1"
out=$("$TI" --json "$T/local.wav")
eq "local file: analysed without Deezer (120 BPM, G major 9B)" "$(echo "$out" | J 'str(abs(d[0]["bpm"]-120)<2)+" "+d[0]["camelot"]+" "+d[0]["source"]')" "True 9B file"
eq "local file: no requests" "$(wc -l < "$T/server.log")" "$n1"

# --- set ordering (list values, offline)
cat > "$T/set.txt" <<'L'
Kilo - Eleven | 126 | 4B | 6.2
## warmup
Alpha - One | 100 | 8A | 3
Bravo - Two | 102 | 9A | 4
Charlie - Three | 101 | 8B | 2
## floor
Delta - Four | 124 | 3B | 6
Echo - Five | 125 | 4B | 7
Foxtrot - Six | 126 | 3A | 5
## peak
Golf - Seven | 128 | 4A | 8
Hotel <b> - Eight | 140 | 11B | 9
India - Nine | 129 | 5A | 8.5
L
out=$("$TI" set --offline --json --file "$T/set.txt")
eq "set: warmup by energy with neighbours" "$(echo "$out" | J '[t["artist"] for t in d["phases"][0]["tracks"]]')" "['Charlie', 'Alpha', 'Bravo']"
eq "set: unlabelled track goes to the nearest phase by energy" "$(echo "$out" | J '[t["artist"] for t in d["phases"][1]["tracks"]]')" "['Foxtrot', 'Delta', 'Kilo', 'Echo']"
eq "set: hard track last in peak" "$(echo "$out" | J '[t["artist"] for t in d["phases"][2]["tracks"]]')" "['Golf', 'India', 'Hotel <b>']"
eq "set: flags phase change and the hard one" "$(echo "$out" | J '[(t["from"].split(" ")[0], t["flags"]) for t in d["transitions"] if t["flags"]]')" "[('Bravo', ['key', 'bpm']), ('India', ['key', 'bpm'])]"
txt=$("$TI" set --offline --file "$T/set.txt")
has "set text: hard transition tip" "$txt" "!! key 5A"
has "set text: summary" "$txt" "10 tracks, 2 flagged transition(s), 2 hard (1 at phase changes)"
printf '## one\nA - a | 120 | 8A | 3\nB - b | 120 | 10A | 5\nC - c | 121 | 9A | 9\n' > "$T/bridge.txt"
out=$("$TI" set --offline --json --phases one --file "$T/bridge.txt")
eq "set: bridge suggested for a +2 Camelot jump" "$(echo "$out" | J '[t.get("bridge") for t in d["transitions"] if t["flags"]]')" "['C - c']"
printf 'U1 - x | 120 | 8A | 1\nU2 - x | 120 | 8A | 2\nU3 - x | 120 | 8A | 3\nU4 - x | 120 | 8A | 6\nU5 - x | 120 | 8A | 7\nU6 - x | 120 | 8A | 8\n' > "$T/auto.txt"
out=$("$TI" set --offline --json --phases low,high --file "$T/auto.txt")
eq "set: no labels -> split by energy" "$(echo "$out" | J '[t["artist"] for t in d["phases"][0]["tracks"]]')" "['U1', 'U2', 'U3']"
out=$("$TI" set --offline --json); eq "set: default = last list" "$(echo "$out" | J 'd["count"]')" 6
"$TI" set --offline --file "$T/nothing.txt" >/dev/null 2>&1; eq "set: missing file -> exit 1" "$?" 1

# --- phone card
cat > "$T/plan.json" <<'J'
{"title": "Test set", "eyebrow": "Card", "phases": {"warmup": {"label": "Warm-up", "time": "22:00", "brief": "easy"}},
 "checklist": ["USB stick with the backup folder", "USB-C laptop"], "notes": ["note one"]}
J
"$TI" set --offline --file "$T/set.txt" --html "$T/card.html" --plan "$T/plan.json" >/dev/null
c=$(cat "$T/card.html" 2>/dev/null)
has "card: phase label + time" "$c" "Warm-up</b>"
has "card: checklist" "$c" "USB stick with the backup folder"
has "card: flagged transition" "$c" "⚠ hard · key"
has "card: escaped names" "$c" "Hotel &lt;b&gt;"
hasnt "card: no raw tag from names" "$c" "Hotel <b>"
has "card: dark mode tokens" "$c" 'prefers-color-scheme: dark'

echo "passed $pass, failed $fail"
[ "$fail" -eq 0 ]
