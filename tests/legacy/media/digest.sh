#!/usr/bin/env bash
# Offline tests for `media digest` (daily/weekly summary cards for the friends' Seerr Telegram group). Exit 0 = all pass.
# digest_fx.py fakes Emby, Radarr/Sonarr, Seerr (incl. its Telegram settings), Tracearr and the Bot API; the clock is
# Sun 2026-10-25 19:00 Zurich. Checks: windows, first names only, requests grouped, plays >= 2 min, min_viewers,
# names (requesters, viewers) only on the Mini App pages: never drawn on the card, in the caption or on Telegraph,
# upgrades/downgrades/cleanups (+ snapshot removals), PNG + caption files, off switch, dry run never posts,
# sendPhoto fields, once per day, Telegram errors without the token, GET-only towards the media apps.
# Card styles: "compact" (default: a <= 1080x1350 teaser, two-line caption without a URL; the button carries the link)
# and "full" (the tall card, caption with "📖 All titles: URL").
set -uo pipefail
_pf="$(cd "$(dirname "$0")/.." && pwd)"; [ -f "$_pf/profile/fixtures/profile.json" ] && _pf=$_pf/profile || _pf=$_pf/profile-tests
export ASSISTANT_PROFILE="$_pf/fixtures/profile.json"
MEDIA=${MEDIA_BIN:-/home/node/bin/media}
HERE=$(cd "$(dirname "$0")" && pwd)
command -v rsync >/dev/null || PATH=$HERE/fakebin:$PATH  # no rsync here (the CI runner): its stand-in
T=$(mktemp -d "${TMPDIR:-/tmp}/media-digest-tests.XXXXXX")
trap 'kill $SRV 2>/dev/null; rm -rf "$T"' EXIT

echo ok > "$T/scenario"
python3 -I "$HERE/digest_fx.py" "$T/port" "$T/server.log" "$T/scenario" 2>"$T/server.err" &
SRV=$!
for _ in $(seq 50); do [ -s "$T/port" ] && break; sleep 0.1; done
B="http://127.0.0.1:$(cat "$T/port")"
printf '{"seerr": {"url": "%s/seerr", "key": "SEERR-KEY-Zq81"}, "radarr": {"url": "%s/radarr", "key": "RADARR-KEY-Xw42"}, "sonarr": {"url": "%s/sonarr", "key": "SONARR-KEY-Vy73"}, "emby": {"url": "%s", "key": "EMBY-KEY-Po66"}, "tracearr": {"url": "%s/ta", "key": "TA-KEY-Rt55"}}\n' \
  "$B" "$B" "$B" "$B" "$B" > "$T/cfg.json"
TOKEN="12345:FAKEfakeFAKEfakeFAKE0"
export MEDIA_SECRETS=$T/cfg.json MEDIA_NOW=2026-10-25T19:00:00+01:00 MEDIA_TIMEOUT=5 \
  MEDIA_CONTINUE_EMBY_USER=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa MEDIA_REPORT_DIR=$T/reports \
  MEDIA_DIGEST_CONF=$T/digest.json MEDIA_DIGEST_STATE=$T/state.json MEDIA_DIGEST_TG_API=$B/tg \
  MEDIA_POSTER_CACHE=$T/cache MEDIA_DIGEST_TELEGRAPH_API=$B/tph MEDIA_DIGEST_TELEGRAPH_KEY=$T/tph-key \
  MEDIA_DIGEST_WEB_DIR=$T/web MEDIA_DIGEST_WEB_DEST=$T/www
unset MEDIA_POSTERS_FONTS
# no DejaVu or Noto font here (the CI runner's image has none): Bitstream Vera, DejaVu's base with its metrics, which
# the CI toolchain's reportlab carries, under DejaVu's names
_vera=${ASSISTANT_CI_TOOLCHAIN:-/nonexistent}/site/reportlab/fonts
if [ ! -e /usr/share/fonts/truetype/dejavu/DejaVuSans.ttf ] && [ ! -e /usr/share/fonts/truetype/noto/NotoSans-Regular.ttf ] &&
  [ -f "$_vera/Vera.ttf" ]; then
  mkdir -p "$T/fonts/dejavu"
  ln -s "$_vera/Vera.ttf" "$T/fonts/dejavu/DejaVuSans.ttf" && ln -s "$_vera/VeraBd.ttf" "$T/fonts/dejavu/DejaVuSans-Bold.ttf"
  export MEDIA_POSTERS_FONTS=$T/fonts
fi

pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2" | head -15; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2"; fi; }
# has/hasnt test the substring in the shell: `printf | grep -q` under pipefail reads as no match when grep exits
# on the match before printf (one write per line) is done, SIGPIPE (a false FAIL, or a false pass for hasnt)
has() { if [[ $2 == *"$3"* ]]; then ok "$1"; else no "$1" "$2"; fi; }
hasnt() { if [[ $2 == *"$3"* ]]; then no "$1" "$2"; else ok "$1"; fi; }
js() { python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(eval(sys.argv[2]))" "$T/out.json" "$1"; }
run() { "$MEDIA" digest "$@" >"$T/out.json" 2>"$T/err"; echo $? > "$T/rc"; }

# 1. daily dry run: what is in it
run --daily --dry-run --json
eq "daily dry run exits 0" "$(cat "$T/rc")" 0
eq "daily: 1 new movie in 24 h (the weekly one is older)" "$(js 'd["new_movies"]')" 1
eq "daily: 1 show, 2 new episodes" "$(js '(d["new_shows"], d["new_episodes"])')" "(1, 2)"
eq "requests grouped per title, first names only, English title" "$(js 'd["requests"]')" "[{'title': 'Lost Reel EN', 'who': ['Robo']}, {'title': 'Star Voyage EN', 'who': ['Robo', 'Zed']}]"
eq "most watched: 2 viewers first, short plays out, weekly-only out, replaced file found by Tmdb id, no duplicate" "$(js 'd["top"]')" "['Moon Station EN', 'Star Voyage EN']"
eq "PNG written" "$(js 'd["size"][0]')" 1080
[ -s "$T/reports/digest-daily-2026-10-25.png" ] && ok "default PNG path" || no "default PNG path" "$(ls "$T/reports")"
CAP=$(cat "$T/reports/digest-daily-2026-10-25.txt")
eq "compact by default" "$(js 'd["style"]')" compact
eq "compact caption: two short lines (counts, top title)" "$CAP" $'🎬 New today · 1 movie · 1 show\n🔥 Most watched: Moon Station EN'
eq "compact card: 1080 wide, at most 1350 tall (4:5: Telegram shows it uncropped)" \
  "$(python3 -c "from PIL import Image; import sys; w, h = Image.open(sys.argv[1]).size; print(w, h <= 1350)" "$T/reports/digest-daily-2026-10-25.png")" "1080 True"
eq "compact: --json size = the PNG" "$(js 'd["size"][1] <= 1350')" True
hasnt "caption: English only" "$CAP" "Folge"
eq "new titles in English" "$(js 'd["new_titles"]')" "['Star Voyage EN', 'Moon Station EN']"
hasnt "no surname anywhere" "$(cat "$T/out.json" "$T/reports/digest-daily-2026-10-25.txt")" "Tester"
for n in Robo Zed Mira Kito Ulla; do hasnt "caption: no name ($n)" "$CAP" "$n"; done
eq "dry run: not posted" "$(js 'd["posted"]')" False
hasnt "dry run never reads the bot settings" "$(cat "$T/server.log")" "settings/notifications"
hasnt "dry run never calls Telegram" "$(cat "$T/server.log")" "POST"
# 1b. "style": "full" = the old tall card and caption (one config change to roll back)
echo '{"style": "full"}' > "$T/digest.json"
run --daily --dry-run --json
CAP=$(cat "$T/reports/digest-daily-2026-10-25.txt")
eq "full: style + exit 0" "$(js 'd["style"]') $(cat "$T/rc")" "full 0"
has "full caption: counts" "$CAP" "New on the server today: 1 movie, 1 show (2 episodes) · 2 requests fulfilled."
has "full caption: most watched, English titles" "$CAP" "Most watched: Moon Station EN, Star Voyage EN."
echo '{"style": "fancy"}' > "$T/digest.json"
run --daily --dry-run --json
eq "unknown style: compact + a warning (exit 3)" "$(js 'd["style"]') $(cat "$T/rc")" "compact 3"
has "unknown style: says so" "$(cat "$T/err")" "is not compact or full"
rm -f "$T/digest.json"

# 1c. the card draws no names: every string drawn on the PNG, compact and full, daily and weekly
DRAWN=$(python3 - "$MEDIA" "$T/digest.json" <<'PY'
import contextlib, io, json, runpy, sys
from PIL import ImageDraw
media, conf = sys.argv[1:3]
seen, orig = [], ImageDraw.ImageDraw.text
def text(self, xy, s, *a, **k):
    seen.append(str(s))
    return orig(self, xy, s, *a, **k)
ImageDraw.ImageDraw.text = text
for style in ("compact", "full"):
    for kind in ("--daily", "--weekly"):
        json.dump({"style": style}, open(conf, "w"))
        sys.argv = [media, "digest", kind, "--dry-run", "--json"]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.suppress(SystemExit):
            runpy.run_path(media, run_name="__main__")
print("\n".join(seen))
PY
)
rm -f "$T/digest.json"
has "card: titles drawn (the capture works)" "$DRAWN" "Moon Station EN"
for n in Robo Zed Mira Kito Ulla Pim; do hasnt "card: no name drawn ($n)" "$DRAWN" "$n"; done
has "card: a fulfilled request on a poster is badged without a name" "$DRAWN" "Requested"
has "card: most watched shows counts instead of names" "$DRAWN" "2 viewers · 2 plays"
hasnt "card: no 'for <name>' line" "$DRAWN" "for "
LN=$(python3 - "$MEDIA" <<'PY'
import importlib.machinery, importlib.util, sys
ld = importlib.machinery.SourceFileLoader("m", sys.argv[1]); m = importlib.util.module_from_spec(importlib.util.spec_from_loader("m", ld))
ld.exec_module(m)
print([m.dg_top_line(x) for x in ({"kind": "show", "viewers": 3, "plays": 7}, {"kind": "show", "viewers": 1, "plays": 4},
                                  {"kind": "movie", "viewers": 1, "plays": 1, "year": 2024}, {"kind": "show", "viewers": 1, "plays": 1})])
print(m.dg_names(["A"]), "|", m.dg_names(["A", "B"]), "|", m.dg_names(["A", "B", "C"]), "|", m.dg_names(list("ABCDEF")))
PY
)
eq "card line: counts, else kind (+ year)" "$LN" $'[\'3 viewers · 7 plays\', \'Show · 4 plays\', \'Movie · 2024\', \'Show\']\nA | A & B | A, B & C | A, B, C, D +2'

# 2. off switch: a live run without "enabled": true renders and posts nothing
: > "$T/server.log"
run --daily
eq "off: exit 0" "$(cat "$T/rc")" 0
has "off: says so" "$(cat "$T/out.json")" "off ("
eq "off: no request at all" "$(wc -l < "$T/server.log" | tr -d ' ')" 0

# 3. live post (enabled), once per day, the token stays out of every output
echo '{"enabled": true}' > "$T/digest.json"
: > "$T/server.log"
run --daily --json
eq "post: exit 0" "$(cat "$T/rc")" 0
eq "post: message id" "$(js 'd["message_id"]')" 777
SP=$(grep '^POST' "$T/server.log")
has "sendPhoto to Seerr's chat id" "$SP" '"chat_id": "-100777"'
has "photo attached" "$SP" '"photo": "<png'
has "caption sent" "$SP" "🎬 New today · 1 movie · 1 show"
hasnt "no empty thread id sent" "$SP" "message_thread_id"
hasnt "telegraph off: no button, no page" "$SP$(grep ^TPH "$T/server.log")" "reply_markup"
hasnt "token not in stdout/stderr" "$(cat "$T/out.json" "$T/err")" "$TOKEN"
hasnt "token not in the state file" "$(cat "$T/state.json")" "FAKEfake"
eq "state: daily posted today" "$(python3 -c "import json; d=json.load(open('$T/state.json')); print(d['daily']['day'], sorted(d['requests_posted']))")" "2026-10-25 ['51', '52', '54']"
if grep -q '^GET' "$T/server.log" && ! grep -qv -e '^GET' -e '/tg/bot' "$T/server.log"; then ok "GET-only towards the media apps"; else no "GET-only" "$(grep -v '^GET' "$T/server.log")"; fi
: > "$T/server.log"
run --daily
has "second run the same day: skipped" "$(cat "$T/out.json")" "posted today already"
eq "second run: no Telegram call" "$(grep -c '^POST' "$T/server.log")" 0
run --daily --dry-run --json
eq "same day: posted requests stay on that day's page" "$(js 'len(d["requests"])')" 2
cp "$T/state.json" "$T/state.keep"
python3 -c "import json,sys; d=json.load(open(sys.argv[1])); d['requests_posted']={k: '2026-10-24' for k in d['requests_posted']}; json.dump(d, open(sys.argv[1], 'w'))" "$T/state.json"
run --daily --dry-run --json
eq "requests posted on an earlier day are not repeated" "$(js 'd["requests"]')" "[]"
cp "$T/state.keep" "$T/state.json"

# 4. Telegram refuses: non-zero, message without the token
rm -f "$T/state.json"; echo tg-fail > "$T/scenario"
run --daily
[ "$(cat "$T/rc")" != 0 ] && ok "telegram error: non-zero exit" || no "telegram error: non-zero exit"
has "telegram error: reason shown" "$(cat "$T/err")" "telegram HTTP 400"
hasnt "telegram error: token masked" "$(cat "$T/out.json" "$T/err")" "FAKEfake"
echo ok > "$T/scenario"; echo '{"enabled": false}' > "$T/digest.json"

# 5. weekly: library counts, changes, removals from the snapshot a week ago; no storage stats
python3 - "$T/state.json" <<'EOF'
import json, sys
json.dump({"library": {"2026-10-18": {"movies": {"1": 5000000000, "2": 30000000000, "9": 7000000000},
                                      "shows": {"1": 20000000000, "5": 1000000000}, "episodes": 10, "bytes": 0}}},
          open(sys.argv[1], "w"))
EOF
run --weekly --dry-run --json
eq "weekly dry run exits 0" "$(cat "$T/rc")" 0
eq "weekly: 2 new movies in 7 days" "$(js 'd["new_movies"]')" 2
eq "weekly top: plays of the week (no English title: Emby's name)" "$(js 'd["top"]')" "['Moon Station EN', 'Star Voyage EN', 'Quiet Harbor']"
eq "weekly tiles: movies + shows only, no size / space tiles" "$(js '[t[:2] for t in d["tiles"]]')" "[['Movies', '2'], ['Shows', '1']]"
hasnt "weekly --json: no storage figures" "$(js 'd["tiles"]')" " GB"
eq "changes: upgrade, downgrade, cleanups incl. removed titles" "$(js 'd["chips"]')" \
  "['Upgraded: 1 movie', 'Downgraded: 1 movie', 'Removed: 2 movies, 1 show']"
eq "weekly compact caption" "$(cat "$T/reports/digest-weekly-2026-10-25.txt")" $'📊 New in week 43 · 2 movies · 1 show\n🔥 Most watched: Moon Station EN'
eq "weekly compact card: 1080 x <= 1350" "$(js 'd["size"][0] == 1080 and d["size"][1] <= 1350')" True
echo '{"style": "full"}' > "$T/digest.json"
run --weekly --dry-run --json
has "weekly full caption: week + counts" "$(cat "$T/reports/digest-weekly-2026-10-25.txt")" "Week 43 on the server: 2 movies, 1 show (2 episodes) new."
has "weekly full caption: library line" "$(cat "$T/reports/digest-weekly-2026-10-25.txt")" "Library: 2 movies, 1 shows."
hasnt "weekly full caption: no storage size" "$(cat "$T/reports/digest-weekly-2026-10-25.txt")" " GB"
eq "weekly full: tiles without storage" "$(js '[t[0] for t in d["tiles"]]')" "['Movies', 'Shows']"
MEDIA_NOW=2026-10-30T19:05:00+01:00 MEDIA_DIGEST_STATE=$T/state-fri.json run --weekly --dry-run --json
eq "weekly on a Friday (the cron day): rc 0, the Friday's week, a rolling 7 days" \
  "$(cat "$T/rc") $(head -1 "$T/reports/digest-weekly-2026-10-30.txt" 2>&1 | grep -o 'Week 44 on the server')" "0 Week 44 on the server"
rm -f "$T/digest.json"
eq "snapshot of today kept" "$(python3 -c "import json; print(sorted(json.load(open('$T/state.json'))['library']))")" "['2026-10-18', '2026-10-25']"

# 6. min_viewers: titles watched by one person only stay out
echo '{"min_viewers": 2}' > "$T/digest.json"
run --daily --dry-run --json
eq "min_viewers 2: only the shared title" "$(js 'd["top"]')" "['Moon Station EN']"

# 7. nothing new: no card, no post
echo empty > "$T/scenario"; echo '{"enabled": true}' > "$T/digest.json"; rm -f "$T/state.json"; : > "$T/server.log"
run --daily --json
eq "empty day: exit 0" "$(cat "$T/rc")" 0
eq "empty day: no card" "$(js 'd["path"]')" None
eq "empty day: nothing posted" "$(grep -c '^POST' "$T/server.log")" 0

# 8a. layout: a busy day still fits Telegram's 2560 px (most watched trimmed first, then new tiles)
FIT=$(python3 - "$MEDIA" <<'PY'
import importlib.machinery, importlib.util, sys
ld = importlib.machinery.SourceFileLoader("m", sys.argv[1]); m = importlib.util.module_from_spec(importlib.util.spec_from_loader("m", ld))
ld.exec_module(m)
print(m.dg_daily_fit(30, 9, 10)[3] <= m.DG_MAX_H, m.dg_daily_fit(11, 2, 8)[:3])
PY
)
eq "busy day fits 2560 px; a normal day keeps everything" "$FIT" "True (11, 2, 8)"
CFIT=$(python3 - "$MEDIA" <<'PY'
import importlib.machinery, importlib.util, sys
ld = importlib.machinery.SourceFileLoader("m", sys.argv[1]); m = importlib.util.module_from_spec(importlib.util.spec_from_loader("m", ld))
ld.exec_module(m)
v = {"new": [{"title": f"T{i}", "kind": "movie"} for i in range(30)], "reqs": [{"title": "R", "kind": "movie", "who": ["A"]}] * 9,
     "top": [{"title": "W", "kind": "show"}] * 10}
g, more, ts, top, b = m.dg_c_plan("daily", v)
w = {"new": v["new"][:10], "new_total": 39, "top": v["top"], "lib_line": ["2 movies", "1 show", "36 GB"]}
g2, more2, ts2, top2, b2 = m.dg_c_plan("weekly", w)
print(len(g), more, len(top), m.dg_c_height(len(g), ts, len(top), b) <= m.DG_C_H,
      len(g2), more2, m.dg_c_height(len(g2), ts2, len(top2), b2) <= m.DG_C_H)
PY
)
eq "compact busy day/week: 8 posters + '+N more', top 3, within 1350 px" "$CFIT" "8 22 3 True 8 31 True"

# 9. Telegraph page (Instant View): preview with --page, links into Emby, no requester names, token 0600 + never shown
echo ok > "$T/scenario"; rm -f "$T/state.json"; echo '{"enabled": true, "telegraph_author": "Test Media"}' > "$T/digest.json"
: > "$T/server.log"
run --daily --dry-run --page --json
eq "page: exit 0" "$(cat "$T/rc")" 0
eq "page: url" "$(js 'd["page_url"]')" "https://telegra.ph/New-today-10-25"
hasnt "page, compact: no URL in the caption" "$(cat "$T/reports/digest-daily-2026-10-25.txt")" "https://"
eq "page: one account created" "$(grep -c 'TPH /tph/createAccount' "$T/server.log")" 1
has "page: account name from the config" "$(grep 'createAccount' "$T/server.log")" '"short_name": "Test Media"'
eq "page: token file 0600" "$(stat -c %a "$T/tph-key")" 600
has "page: createPage with the token" "$(grep 'createPage' "$T/server.log")" "token=True"
PG=$(grep 'TPH /tph/createPage' "$T/server.log")
has "page: Emby item link with server id" "$PG" "/web/index.html#!/item?id=101&serverId=srv123abc"
has "page: TMDB poster" "$PG" "https://image.tmdb.org/t/p/w500/abc123.jpg"
has "page: sections" "$PG" "Requests fulfilled (2)"
has "page: most watched ranked" "$PG" "1. Moon Station EN"
has "page: overview trimmed" "$PG" "A crew flies far."
for n in Robo Zed Mira Kito Ulla; do hasnt "page: no names on Telegraph ($n)" "$PG" "$n"; done
hasnt "page: title not in Emby has no link" "$(printf '%s' "$PG" | python3 -c 'import sys,json; s=sys.stdin.read(); c=json.loads(json.loads(s[s.index("{"):])["content"]); print([n for n in c if n["tag"]=="h4" and "Lost Reel" in json.dumps(n)])')" "href"
hasnt "page: tokens never shown" "$(cat "$T/out.json" "$T/err")" "TPH-TOKEN"
eq "page dry run: nothing sent to the group" "$(grep -c '^POST' "$T/server.log")" 0
run --daily --dry-run --page --json
eq "page: the account is reused" "$(grep -c 'TPH /tph/createAccount' "$T/server.log")" 1
echo '{"enabled": true, "telegraph": true}' > "$T/digest.json"; : > "$T/server.log"
run --daily --json
SP=$(grep '^POST' "$T/server.log")
RM=$(printf '%s' "$SP" | python3 -c 'import sys,json; s=sys.stdin.read(); print(json.loads(s[s.index("{"):])["reply_markup"])')
eq "live + telegraph: Open list URL button" "$(printf '%s' "$RM" | python3 -c 'import sys,json; b=json.load(sys.stdin)["inline_keyboard"][0][0]; print(b["text"], b["url"])')" "📖 Open list https://telegra.ph/New-today-10-25"
CF=$(printf '%s' "$SP" | python3 -c 'import sys,json; s=sys.stdin.read(); print(json.loads(s[s.index("{"):])["caption"])')
eq "live + telegraph, compact: caption without the URL" "$CF" $'🎬 New today · 1 movie · 1 show\n🔥 Most watched: Moon Station EN'
echo '{"enabled": true, "telegraph": true, "style": "full"}' > "$T/digest.json"; rm -f "$T/state.json"; : > "$T/server.log"
run --daily --json
has "live + telegraph, full: link in the caption" "$(grep '^POST' "$T/server.log")" "All titles: https://telegra.ph/New-today-10-25"
echo tph-fail > "$T/scenario"; rm -f "$T/state.json"; : > "$T/server.log"
run --daily --json
eq "telegraph down: card still posted (exit 3 = warning)" "$(cat "$T/rc") $(js 'd["message_id"]')" "3 777"
hasnt "telegraph down: no button" "$(grep '^POST' "$T/server.log")" "reply_markup"
echo ok > "$T/scenario"; echo '{"enabled": false}' > "$T/digest.json"

# 10. Mini App pages: static HTML in the cards' look, Emby links, no requester names, index + start_param, prune
rm -rf "$T/state.json" "$T/web" "$T/www"; : > "$T/server.log"
mkdir -p "$T/web"; echo '<html>old</html>' > "$T/web/d-2026-01-01.html"
echo '{"d-2026-01-01": {"title": "old", "kind": "daily"}}' > "$T/web/pages.json"
echo '{"enabled": true, "web_base": "https://media.example.test/media"}' > "$T/digest.json"
run --daily --dry-run --web --json
eq "web: exit 0 + page id" "$(cat "$T/rc") $(js 'd["web_page"]')" "0 d-2026-10-25"
for f in d-2026-10-25.html today.html index.html pages.json style.css app.js; do
  [ -s "$T/www/$f" ] && ok "web: $f published" || no "web: $f published" "$(ls "$T/www")"; done
WP=$(cat "$T/www/d-2026-10-25.html")
has "web: Emby item link + server id" "$WP" '#!/item?id=101&amp;serverId=srv123abc'
has "web: TMDB poster" "$WP" 'https://image.tmdb.org/t/p/w342/abc123.jpg'
has "web: Telegram Mini App script" "$WP" 'https://telegram.org/js/telegram-web-app.js'
has "web: sections" "$WP" 'Requests fulfilled <span>2</span>'
has "web: English titles" "$WP" 'Moon Station EN'
has "web: requesters on the new poster" "$WP" '<span class="req">for Robo &amp; Zed</span>'
has "web: requester under a fulfilled request" "$WP" '<div class="m who">for Robo</div>'
has "web: viewers + plays under most watched" "$WP" '<div class="m who">watched by Mira &amp; Kito · 2 plays</div>'
has "web: a replaced file's viewers pooled into one title" "$WP" 'watched by Mira &amp; Ulla · 2 plays'
hasnt "web: first names only" "$WP" "Tester"
has "web: title not in Emby is a plain row" "$WP" '<li class="row"><div class="poster" style="--ph:'
has "web: today = the daily page" "$(cmp "$T/www/today.html" "$T/www/d-2026-10-25.html" && echo same)" "same"
hasnt "web: old page pruned" "$(ls "$T/www")" "d-2026-01-01"
hasnt "web, compact: no URL in the caption" "$(cat "$T/reports/digest-daily-2026-10-25.txt")" "https://"
eq "web, compact: page url still in --json" "$(js 'd["page_url"]')" "https://media.example.test/media/d-2026-10-25"
eq "web dry run: nothing sent to the group" "$(grep -c '^POST' "$T/server.log")" 0
run --weekly --dry-run --web --json
eq "web weekly: page id" "$(js 'd["web_page"]')" "w-2026-43"
[ -s "$T/www/week.html" ] && ok "web: week.html" || no "web: week.html"
hasnt "web weekly: no storage tiles" "$(cat "$T/www/week.html")" "on disk"
hasnt "web weekly: no size figures" "$(cat "$T/www/week.html")" " GB<"
has "web weekly: count tiles kept" "$(cat "$T/www/week.html")" '<div class="l">Movies</div>'
has "web weekly: viewers under a most-watched tile" "$(cat "$T/www/week.html")" '<div class="m who">watched by Pim</div>'
eq "web index: start_param list, newest first" "$(grep -o '<script type="application/json" id="pages">[^<]*' "$T/www/index.html" | sed 's/.*>//')" '["d-2026-10-25", "w-2026-43"]'
has "web index: router flag" "$(cat "$T/www/index.html")" "<body data-index>"
has "web app.js: start_param router" "$(cat "$T/www/app.js")" "start_param"
echo '{"enabled": true, "miniapp": true, "miniapp_link": "https://t.me/TestBot/media"}' > "$T/digest.json"
rm -f "$T/state.json"; : > "$T/server.log"
run --daily --json
SP=$(grep '^POST' "$T/server.log")
CF=$(printf '%s' "$SP" | python3 -c 'import sys,json; s=sys.stdin.read(); print(json.loads(s[s.index("{"):])["caption"])')
hasnt "miniapp, compact: no t.me link in the caption" "$CF" "t.me"
RM=$(printf '%s' "$SP" | python3 -c 'import sys,json; s=sys.stdin.read(); print(json.loads(s[s.index("{"):])["reply_markup"])')
eq "miniapp: button deep link" "$(printf '%s' "$RM" | python3 -c 'import sys,json; print(json.load(sys.stdin)["inline_keyboard"][0][0]["url"])')" "https://t.me/TestBot/media?startapp=d-2026-10-25"
echo '{"enabled": true, "miniapp": true, "miniapp_link": "https://t.me/TestBot/media", "style": "full"}' > "$T/digest.json"
rm -f "$T/state.json"; : > "$T/server.log"
run --daily --json
has "miniapp, full: caption deep link" "$(grep '^POST' "$T/server.log")" "All titles: https://t.me/TestBot/media?startapp=d-2026-10-25"
echo '{"enabled": true}' > "$T/digest.json"; rm -rf "$T/state.json" "$T/www"
run --daily --json
[ ! -e "$T/www" ] && ok "miniapp off: live run publishes no page" || no "miniapp off: live run publishes no page"
echo '{"enabled": false}' > "$T/digest.json"

# 8. keys only in headers, never in a URL
hasnt "no API key in any URL" "$(cat "$T/server.log")" "KEY-"

echo "digest: $pass passed, $fail failed"
[ "$fail" = 0 ]
