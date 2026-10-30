#!/usr/bin/env bash
# Offline tests for tg-cmd's picture cards (TG_CMD_PICTURES=1: /space, /gym, /cat, /finish, /vibe →
# {"photo","caption","text"}) and ~/bin/lib/cardimg.py. Fake unraid-capacity, gym and media (TG_CMD_BIN), made-up
# capacity/cat history files; HOME is a temp dir and the profile the test fixture.
# /week's picture: week.sh. TGCMD_BIN: a test copy (its lib/cardimg.py is used); CARDIMG_LIB: the cardimg.py to test. Exit 0 = all pass.
set -uo pipefail
_pf="$(cd "$(dirname "$0")/.." && pwd)"; [ -f "$_pf/profile/fixtures/profile.json" ] && _pf=$_pf/profile || _pf=$_pf/profile-tests
export ASSISTANT_PROFILE=${ASSISTANT_PROFILE:-$_pf/fixtures/profile.json}
TGCMD=${TGCMD_BIN:-/home/node/bin/tg-cmd}
LIB=${CARDIMG_LIB:-$(dirname "$(realpath "$TGCMD")")/lib/cardimg.py}
[ -f "$LIB" ] || LIB=$(getent passwd "$(id -u)" | cut -d: -f6)/bin/lib/cardimg.py   # a copy without lib/ (CI's overlay)
pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2" | head -30; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
has() { if [[ "$2" == *"$3"* ]]; then ok "$1"; else no "$1" "$2"; fi; }
hasnt() { if [[ "$2" == *"$3"* ]]; then no "$1" "$2"; else ok "$1"; fi; }
T=$(mktemp -d)
trap 'find "$T" -mindepth 1 -delete; rmdir "$T"' EXIT
export USAGE_STATE="$T/usage"   # tg-cmd's exec records go to a scratch usage store: this suite runs with the real HOME
mkdir -p "$T/bin" "$T/cap" "$T/cat" "$T/cards" "$T/home"
J() { python3 -c 'import json,sys; d=json.loads(sys.stdin.read()); print(eval(sys.argv[1]))' "$1"; }
png_size() { python3 -c 'from PIL import Image; import sys; i=Image.open(sys.argv[1]); print(f"{i.width}x{i.height}")' "$1" 2>&1; }

# fake unraid-capacity: forecast JSON (made-up numbers), FAKE_CAP_ERR = failure
cat > "$T/bin/unraid-capacity" <<'EOF'
#!/usr/bin/env bash
printf 'unraid-capacity %s\n' "$*" >> "$FAKE_LOG"
[ -n "${FAKE_CAP_ERR:-}" ] && { echo "unraid-capacity: $FAKE_CAP_ERR" >&2; exit 1; }
cat <<'JSON'
{"ok": true, "host": "testhost", "as_of": "x", "as_of_local": "2031-03-05 11:00", "sample_age_h": 0.2,
 "array": {"size": 100000000000000, "used": 91000000000000, "free": 9000000000000, "usable": 8000000000000, "pct": 91.0,
           "eta": {"days": 40.0, "date": "2031-04-14"}},
 "model": {"rate": 200000000000}, "disks": {"disk1": {"size": 1, "status": "DISK_OK"}},
 "pools": {"cache": {"pct": 40.0, "free": 600000000000}}, "alerts": [{"key": "array", "msg": "array full in 40 days"}]}
JSON
EOF
# fake gym: plan next / stats --json / chart (writes a small PNG and prints its path)
cat > "$T/bin/gym" <<'EOF'
#!/usr/bin/env bash
printf 'gym %s\n' "$*" >> "$FAKE_LOG"
case "$1" in
  plan) [ -n "${FAKE_GYM_ERR:-}" ] && { echo "gym: $FAKE_GYM_ERR" >&2; exit 1; }
        printf '🏋️ Next: A · Squat day (~45 min)\nLast session 01.03. (B, 4 d ago) · streak 2 wk\n\n1  Squat  3×5 @ 60 kg\n' ;;
  stats) printf '{"weekly": [{"start": "2031-02-24", "sessions": 2, "hard_sets": 20}, {"start": "2031-03-03", "sessions": 1, "hard_sets": 9}], "consistency": {"streak_weeks": 2, "days_since_last": 4}}\n' ;;
  chart) [ -n "${FAKE_CHART_ERR:-}" ] && { echo "chart broke" >&2; exit 1; }
         mkdir -p "$5"; python3 -c 'from PIL import Image; import sys; Image.new("RGB", (1080, 620), (250, 250, 248)).save(sys.argv[1])' "$5/sessions.png"
         echo "$5/volume.png"; echo "$5/sessions.png" ;;
esac
EOF
chmod +x "$T/bin/unraid-capacity" "$T/bin/gym"
printf '%s\n' '{"date": "2031-02-10", "used": 85000000000000, "src": "arr-ledger"}' '{"date": "2031-02-20", "used": 88000000000000, "src": "arr-ledger"}' > "$T/cap/backfill.jsonl"
printf '%s\n' '{"date": "2031-03-01", "array": {"size": 100000000000000, "used": 90000000000000}}' 'not json' > "$T/cap/samples.jsonl"
for d in 01 02 03 04; do
  printf '{"date": "2031-03-%s", "complete": true, "cycles": 5, "drawer_end": 40.0, "faults": 0, "feeders": {"feeder_x": {"feeds": 2, "food_g": 20.0}}, "pets": {"sensor.mittens_visits_today": {"visits": %s, "weight_kg": 4.%s}}}\n' "$d" "$((5 + 10#$d))" "$d"
done > "$T/cat/history.jsonl"
printf '%s\n' '{"date": "2031-03-05", "complete": false, "cycles": 2, "drawer_end": 85.0, "faults": 1, "feeders": {"feeder_x": {"feeds": 1, "food_g": 10.0}}, "pets": {"sensor.mittens_visits_today": {"visits": 3, "weight_kg": 4.1}}}' >> "$T/cat/history.jsonl"

run() { HOME="$T/home" FAKE_LOG="$T/log" TG_CMD_BIN="$T/bin" TG_CMD_NOW="2031-03-05 12:00" CARDIMG_DIR="$T/cards" UNRAID_CAPACITY_STATE_DIR="$T/cap" CAT_CORNER_STATE_DIR="$T/cat" "$TGCMD" "$@"; }
pic() { TG_CMD_PICTURES=1 run "$@"; }

echo "# command table (the picture env of each run line lives in the tools registry)"
eq "--list is the same with the flag on" "$(TG_CMD_PICTURES=1 "$TGCMD" --list)" "$("$TGCMD" --list)"
has "--list names /cat" "$("$TGCMD" --list)" "/cat        Cat week: litter visits, weight, feeder"

echo "# /space"
text=$(run space); rc=$?
eq "space, flag off: rc 0" "$rc" "0"
has "space, flag off: the text card" "$text" "Array 91% full"
out=$(pic space); rc=$?
eq "space picture: rc 0" "$rc" "0"
photo=$(printf '%s' "$out" | J 'd["photo"]')
eq "space picture: photo in CARDIMG_DIR" "$photo" "$T/cards/space.png"
eq "space picture: 1080x1350 PNG" "$(png_size "$photo")" "1080x1350"
eq "space picture: 0600" "$(stat -c %a "$photo")" "600"
eq "space picture: caption" "$(printf '%s' "$out" | J 'd["caption"]')" "💾 Array 91% full · 9.0 TB free · full ~14.04.2031"
eq "space picture: text = the plain card" "$(printf '%s' "$out" | J 'd["text"]')" "$text"
eq "space picture: one line of JSON" "$(printf '%s\n' "$out" | wc -l)" "1"
out=$(FAKE_CAP_ERR="ssh down" pic space); rc=$?
eq "space source failure: rc 3" "$rc" "3"
has "space source failure: the usual ⚠️ text, no JSON" "$out" "⚠️ /space failed: capacity history: ssh down."
printf 'raise RuntimeError("no fonts")\n' > "$T/broken.py"
err=$(TG_CMD_CARDIMG="$T/broken.py" pic space 2>&1 >/dev/null)
out=$(TG_CMD_CARDIMG="$T/broken.py" pic space 2>/dev/null); rc=$?
eq "render failure: rc 0" "$rc" "0"
eq "render failure: the text card instead" "$out" "$text"
has "render failure: logged on stderr" "$err" "/space picture failed (RuntimeError: no fonts)"
mv "$T/cap" "$T/cap.off"
out=$(pic space); rc=$?
eq "no capacity history files: still a picture (one point)" "$rc:$(printf '%s' "$out" | J '"photo" in d')" "0:True"
mv "$T/cap.off" "$T/cap"

echo "# /gym"
gtext=$(run gym)
has "gym, flag off: the plan text" "$gtext" "🏋️ Next: A · Squat day (~45 min)"
: > "$T/log"
out=$(pic gym); rc=$?
eq "gym picture: rc 0" "$rc" "0"
eq "gym picture: 1080x1350" "$(png_size "$(printf '%s' "$out" | J 'd["photo"]')")" "1080x1350"
eq "gym picture: caption = next line + sessions this week" "$(printf '%s' "$out" | J 'd["caption"]')" "🏋️ Next: A · Squat day (~45 min) · 1 this week"
eq "gym picture: text = the plain card" "$(printf '%s' "$out" | J 'd["text"]')" "$gtext"
has "gym picture: gym chart writes under CARDIMG_DIR/gym-src" "$(cat "$T/log")" "gym chart --weeks 8 --out $T/cards/gym-src"
out=$(FAKE_CHART_ERR=1 pic gym); rc=$?
eq "gym chart fails: still a picture (own bars)" "$rc:$(printf '%s' "$out" | J '"photo" in d')" "0:True"
out=$(FAKE_GYM_ERR="database is locked" pic gym); rc=$?
eq "gym plan fails: rc 3 + ⚠️ text" "$rc" "3"
has "gym plan fails: fallback" "$out" "⚠️ /gym failed: gym: database is locked."

echo "# /cat"
ctext=$(run cat); rc=$?
eq "cat, flag off: rc 0" "$rc" "0"
eq "cat text card" "$ctext" "🐾 Mittens · last 5 days
Litter visits: 3 today so far · 9 yesterday · avg 7.5/day
Weight 4.10 kg (+0.09 kg in 5 d)
Feeder: 10 g today so far · 20 g yesterday
Drawer 85% full"
out=$(pic cat); rc=$?
eq "cat picture: rc 0" "$rc" "0"
eq "cat picture: 1080x1350" "$(png_size "$(printf '%s' "$out" | J 'd["photo"]')")" "1080x1350"
eq "cat picture: caption" "$(printf '%s' "$out" | J 'd["caption"]')" "🐾 Mittens · last 5 days · litter 3 today so far · 9 yesterday · avg 7.5/day"
has "cat: stale data flagged" "$(FAKE_LOG="$T/log" TG_CMD_NOW="2031-03-09 12:00" CAT_CORNER_STATE_DIR="$T/cat" "$TGCMD" cat)" "⚠️ cat-corner's data is 4 days old"
out=$(FAKE_LOG="$T/log" CAT_CORNER_STATE_DIR="$T/nothing" "$TGCMD" cat); rc=$?
eq "cat without history: rc 3" "$rc" "3"
has "cat without history: fallback" "$out" "⚠️ /cat failed: no cat-corner history yet."
has "cat fallback hint" "$out" "how is the cat doing?"
eq "cat without history: its exec record lands in the scratch usage store, not the live one" \
  "$(cat "$T"/usage/spool/*.jsonl 2>/dev/null | grep -c '"f":"tg.cat".*"rc":3')" "1"

echo "# cardimg.py"
lib=$(CARDIMG_DIR="$T/lib-out" python3 - "$LIB" <<'EOF'
import importlib.util, json, os, stat, sys
spec = importlib.util.spec_from_file_location("cardimg", sys.argv[1]); ci = importlib.util.module_from_spec(spec); spec.loader.exec_module(ci)
c = ci.Card("A title that is far too long to fit on one phone-width line of the card, really", "sub")
c.big("42%", "label"); c.progress(150); c.progress(-5); c.stats([("1", "one"), ("2", "two", ci.GOOD)])
c.bar_chart([0, None, 3], ["a", "b", "c"]); c.line_chart([]); c.line_chart([(0, 1.0)], limit=(2, "max"))
c.lines(["x"] * 200); c.footer("foot")
p = c.save("t")
print(json.dumps({"path": p, "mode": oct(stat.S_IMODE(os.stat(p).st_mode)), "dir": oct(stat.S_IMODE(os.stat(os.path.dirname(p)).st_mode)),
                  "left": sorted(os.listdir(os.path.dirname(p))), "fit": c.fit("A title that is far too long to fit on one phone-width line of the card, really", ci.font(56, True), 968)[-1],
                  "json": ci.photo_json(p, "cap"), "sev": [ci.severity(80) == ci.ACCENT, ci.severity(92) == ci.WARN, ci.severity(97) == ci.BAD],
                  "paste_missing": c.paste("/nonexistent.png")}))
EOF
)
eq "cardimg: saved under CARDIMG_DIR" "$(printf '%s' "$lib" | J 'd["path"]')" "$T/lib-out/t.png"
eq "cardimg: file 0600, dir 0700, no temp file left" "$(printf '%s' "$lib" | J '[d["mode"], d["dir"], d["left"]]')" "['0o600', '0o700', ['t.png']]"
eq "cardimg: long text cut with …" "$(printf '%s' "$lib" | J 'd["fit"]')" "…"
eq "cardimg: photo_json" "$(printf '%s' "$lib" | J 'd["json"]')" "{\"photo\": \"$T/lib-out/t.png\", \"caption\": \"cap\"}"
eq "cardimg: severity colours" "$(printf '%s' "$lib" | J 'd["sev"]')" "[True, True, True]"
eq "cardimg: unreadable paste → False" "$(printf '%s' "$lib" | J 'd["paste_missing"]')" "False"
eq "cardimg: odd input renders 1080x1350" "$(png_size "$T/lib-out/t.png")" "1080x1350"

echo "# poster cards: fakes"
# fake media: finish/vibe JSON (made-up shows and films) and poster (small JPEGs in $FAKE_POSTERS; id 119 = no poster,
# id 173 = an unreadable file)
cat > "$T/bin/media" <<'EOF'
#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write("media " + " ".join(a) + "\n")
env = os.environ.get
link = lambda i: f"https://emby.example.test/web/index.html#!/item?id={i}" + env("FAKE_LINK_PAD", "")
if a[0] == "finish":
    if env("FAKE_FIN_ERR"):
        sys.stderr.write("media: finish: Emby unreachable\n")
        sys.exit(1)
    names = [] if env("FAKE_FIN_EMPTY") else ["Hijack", "Severed Ties", "Slow Wagons", "A Very Long Show Title That Wraps Twice Or More", "Quiet", "Andromeda"]
    items = [{"id": str(70 + k), "series_id": str(170 + k), "title": t, "episode": f"S01E0{k + 1}", "pos_min": 12 if k == 0 else 0,
              "left": k + 1, "total": 8, "rating": 7.0 + k / 10, "last_played": f"2031-02-0{k + 1}T20:00:00Z"} for k, t in enumerate(names)]
    text = "🏁 Finish next\nStarted, paused 14+ days, episodes left\n\n" + "\n".join(
        f"📺 {x['title']} · {x['left']} of 8 left\n   ▶ {link(x['id'])}" for x in items)
    print(json.dumps({"items": items, "total": 9, "idle_days": 14, "text": text}))
elif a[0] == "vibe":
    series = bool(env("FAKE_VB_SERIES"))
    picks = [] if env("FAKE_VB_NONE") else [
        {"emby_id": i, "title": t, "year": 2020 + k, "rating": 7.5, "runtime_min": None if series else 90 + k,
         "why": f"Comedy, Drama · cozy · “A tagline {k}”" + (" · 👁 3 of 10 eps watched" if series else ""), "link": link(i),
         **({"kind": "series", "episodes": 10, "watched": 3 if k == 0 else 0} if series else {})}
        for k, (i, t) in enumerate([("115", "Cozy Little Cafe"), ("116", "Rainy Day"), ("117", "Teapot"), ("119", "Без постера")])]
    print(json.dumps({"kind": "series" if series else "movie", "label": "cozy rainy", "picks": picks,
                      "text": "🎬 Vibe · cozy rainy\n\n" + "\n".join(f"{p['title']}\n▶ {p['link']}" for p in picks)}))
elif a[0] == "poster":
    if env("FAKE_POSTER_ERR"):
        sys.stderr.write("media: poster: boom\n")
        sys.exit(1)
    from PIL import Image
    ids, skip = [], False
    for x in a[1:]:
        if skip:
            skip = False
        elif x == "--width":
            skip = True
        elif not x.startswith("--"):
            ids.append(x)
    d = env("FAKE_POSTERS")
    os.makedirs(d, exist_ok=True)
    out = {}
    for i in ids:
        p = os.path.join(d, f"{i}.jpg")
        if i == "119":
            out[i] = None
            continue
        if i == "173":
            open(p, "w").write("not an image")
        else:
            Image.new("RGB", (400, 600 if int(i) % 2 else 560), (40, 90 + int(i) % 100, 160)).save(p)
        out[i] = p
    print(json.dumps({"posters": out, "fetched": len(ids), "cached": 0, "warnings": []}))
EOF
chmod +x "$T/bin/media"
export FAKE_POSTERS=$T/posters
caplines() { printf '%s' "$1" | J 'd["caption"]'; }

echo "# /finish"
: > "$T/log"
ftext=$(run finish); rc=$?
eq "finish, flag off: rc 0 + the plain text" "$rc $(printf '%s\n' "$ftext" | sed -n 4p)" "0 📺 Hijack · 1 of 8 left"
eq "finish, flag off: media finish --json only (no posters)" "$(cat "$T/log")" "media finish --json"
: > "$T/log"
out=$(pic finish); rc=$?
eq "finish picture: rc 0" "$rc" "0"
eq "finish picture: photo in CARDIMG_DIR, 1080x1350" "$(printf '%s' "$out" | J 'd["photo"]') $(png_size "$T/cards/finish.png")" "$T/cards/finish.png 1080x1350"
eq "finish picture: 6 shows asked for, series posters fetched at 480 px" "$(cat "$T/log")" "media finish --json -n 6
media poster --json --width 480 170 171 172 173 174 175"
eq "finish picture: caption = head + numbered shows with ▶ links" "$(caplines "$out" | head -5)" "🏁 Finish next · 6 of 9 started
1️⃣ Hijack · S01E01 · 1 left
▶ https://emby.example.test/web/index.html#!/item?id=70
2️⃣ Severed Ties · S01E02 · 2 left
▶ https://emby.example.test/web/index.html#!/item?id=71"
eq "finish picture: text = the plain card" "$(printf '%s' "$out" | J 'd["text"]')" "$ftext"
eq "finish picture: one line of JSON" "$(printf '%s\n' "$out" | wc -l)" "1"
out=$(FAKE_LINK_PAD="&pad=$(printf 'x%.0s' $(seq 230))" pic finish)
cap=$(caplines "$out")
eq "finish picture, long links: caption ≤ 1024, all 6 shows, the first keeps its link, the last loses it" \
  "$(python3 -c 'import sys; c=sys.argv[1]; print(len(c) <= 1024, c.count("️⃣"), c.splitlines()[2].startswith("▶ "), c.splitlines()[-1].startswith("6️⃣"))' "$cap")" "True 6 True True"
out=$(FAKE_POSTER_ERR=1 pic finish); rc=$?
eq "finish picture, posters failing: still a picture (placeholders)" "$rc $(printf '%s' "$out" | J '"photo" in d')" "0 True"
out=$(FAKE_FIN_EMPTY=1 pic finish); rc=$?
eq "finish picture, nothing to finish: the text card, no JSON" "$rc $(printf '%s' "$out" | head -1)" "0 🏁 Finish next"
out=$(FAKE_FIN_ERR=1 pic finish); rc=$?
eq "finish picture, media failing: rc 3 + ⚠️" "$rc $(printf '%s' "$out" | head -1)" "3 ⚠️ /finish failed: Emby finish: media: finish: Emby unreachable."

echo "# /vibe"
: > "$T/log"
out=$(pic vibe); rc=$?
has "vibe picture, bare: the hint as text" "$rc $out" "0 🎬 /vibe + a mood or a film"
eq "vibe picture, bare: nothing run" "$(cat "$T/log")" ""
vtext=$(run vibe "cozy rainy")
: > "$T/log"
out=$(pic vibe "cozy rainy"); rc=$?
eq "vibe picture: rc 0, 1080x1350" "$rc $(png_size "$(printf '%s' "$out" | J 'd["photo"]')")" "0 1080x1350"
eq "vibe picture: media vibe + posters of the picks" "$(cat "$T/log")" "media vibe --json -- cozy rainy
media poster --json --width 480 115 116 117 119"
eq "vibe picture: caption" "$(caplines "$out" | head -3)" "🎬 Vibe · cozy rainy
1️⃣ Cozy Little Cafe (2020)
▶ https://emby.example.test/web/index.html#!/item?id=115"
has "vibe picture: Cyrillic title in the caption" "$(caplines "$out")" "4️⃣ Без постера (2023)"
eq "vibe picture: text = the plain card" "$(printf '%s' "$out" | J 'd["text"]')" "$vtext"
out=$(FAKE_VB_SERIES=1 pic vibe "dark series")
eq "vibe picture, series: 📺 head" "$(caplines "$out" | head -1)" "📺 Vibe · cozy rainy"
out=$(FAKE_VB_NONE=1 pic vibe "zzz"); rc=$?
eq "vibe picture, no picks: the text, no JSON" "$rc $(printf '%s' "$out" | head -1)" "0 🎬 Vibe · cozy rainy"

eq "poster cards never write under HOME" "$(find "$T/home" -type f -not -path '*/.local/state/product/*' 2>/dev/null | wc -l)" "0"   # the usage spool (exec records) is expected

echo "# cardimg posters"
lib=$(CARDIMG_DIR="$T/lib-out2" python3 - "$LIB" "$T/posters" <<'EOF'
import importlib.util, json, os, sys
from PIL import Image
spec = importlib.util.spec_from_file_location("cardimg", sys.argv[1]); ci = importlib.util.module_from_spec(spec); spec.loader.exec_module(ci)
good = os.path.join(sys.argv[2], "x.png"); Image.new("RGB", (300, 450), (200, 40, 40)).save(good)
bad = os.path.join(sys.argv[2], "bad.jpg"); open(bad, "w").write("nope")
res = {}
for n in range(1, 7):
    c = ci.Card("t", "s")
    its = [{"image": [good, None, bad][k % 3], "title": "Title %d with a rather long name" % k, "lines": ["a", ("b", ci.GOOD)],
            "progress": 50 if k % 2 else None, "rating": 7.5 if k else None, "badge": str(k + 1)} for k in range(n)]
    y = c.poster_grid(its, reserve=36)
    res[n] = y <= c.h - ci.PAD
c = ci.Card("t")
res["empty"] = c.poster_grid([]) == c.y
c = ci.Card("rows")
c.poster_rows([{"image": good, "title": "One", "lines": ["x " * 60], "progress": 30}, {"image": None, "title": "Two"}], poster_w=200)
h = c.trim(min_height=600)
p = c.save("rows")
small = ci.Card("small", height=400)
try:
    small.poster_grid([{"image": good, "title": "x"}] * 6)
    res["no_room"] = "drew"
except ValueError:
    res["no_room"] = "ValueError"
w = c.wrap("one two three four five six seven eight nine ten", ci.font(30), 150, 2)
print(json.dumps({"grid": res, "h": h, "size": list(Image.open(p).size), "wrap": [len(w), w[-1][-1]],
                  "fit": ci.poster_image(good, 400, 300).size, "none": ci.poster_image(bad, 10, 10) is None,
                  "ph": ci.placeholder_image(100, 150, "the matrix").size}))
EOF
)
eq "cardimg: poster_grid fits 1-6 tiles above the reserve (missing/unreadable → placeholder)" "$(printf '%s' "$lib" | J 'd["grid"]')" "{'1': True, '2': True, '3': True, '4': True, '5': True, '6': True, 'empty': True, 'no_room': 'ValueError'}"
eq "cardimg: poster_rows + trim → a shorter card" "$(printf '%s' "$lib" | J '[d["size"][0], d["size"][1] == d["h"], 600 <= d["h"] < 1350]')" "[1080, True, True]"
eq "cardimg: wrap → 2 lines, the last cut with …" "$(printf '%s' "$lib" | J 'd["wrap"]')" "[2, '…']"
eq "cardimg: poster_image keeps the box size; unreadable → None; placeholder size" "$(printf '%s' "$lib" | J '[d["fit"], d["none"], d["ph"]]')" "[[400, 300], True, [100, 150]]"

echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
