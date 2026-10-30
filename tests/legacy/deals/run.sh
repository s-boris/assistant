#!/usr/bin/env bash
# Tests for ~/bin/deals (and the `offer` watch type + robots matcher it added to ~/bin/watch). Exit 0 = all pass.
# Synthetic shop pages (fixtures/, see its README) are served by a local fixture server; temp state, config and inbox;
# a stub tg; a fake clock (WATCH_NOW). No real network, no owner messages. Real state is checked to be untouched.
set -uo pipefail
DEALS=${DEALS_BIN:-/home/node/bin/deals}
WATCH=$HOME/bin/watch  # CI: the suite HOME links the would-be ~/bin
HERE=$(cd "$(dirname "$0")" && pwd)
REAL_D=/home/node/.local/state/deals/hdd-latest.json
REAL_W=/home/node/.local/state/watch/state.json
REAL_C=/home/node/.config/deals/config.json
REAL_T=/home/node/.local/state/deals/tutti.json
sum() { [ -f "$1" ] && md5sum < "$1"; }
real_before="$(sum $REAL_D)$(sum $REAL_W)$(sum $REAL_C)$(sum $REAL_T)"
T=$(mktemp -d "${TMPDIR:-/tmp}/deals-tests.XXXXXX")
WWW=$T/www
mkdir -p "$WWW/alt" "$WWW/mm"
trap 'kill $SRV 2>/dev/null; rm -rf "$T"' EXIT
# deals loads watch (and tutti-fetch) from its own dir: a copy under test without them there (assistant ci's overlay
# stages deals alone) runs from $T/bin beside links to the live ones.
if [ ! -e "$(dirname "$DEALS")/watch" ]; then
  mkdir -p "$T/bin"
  cp "$DEALS" "$T/bin/deals" && chmod 0755 "$T/bin/deals"
  ln -s "$WATCH" "$T/bin/watch"
  ln -s "$HOME/bin/tutti-fetch" "$T/bin/tutti-fetch"
  DEALS=$T/bin/deals
fi
for f in "$HERE"/fixtures/alt/*.gz "$HERE"/fixtures/mm/*.gz; do
  sub=$(basename "$(dirname "$f")"); b=$(basename "$f" .gz); gunzip -c "$f" > "$WWW/$sub/$b"
done
cp "$HERE/fixtures/captcha.html" "$WWW/"
cat > "$WWW/robots.txt" <<'EOF'
User-agent: *
Disallow: /*searchParams=*
Disallow: */search.html?query=*
Disallow: /mm/private/
Allow: /mm/private/ok$
EOF
echo '<html><head><title>Plain</title></head><body><p>No prices here.</p></body></html>' > "$WWW/plain.html"

python3 -I "$HERE/fixture_server.py" "$WWW" "$T/port" "$T/server.log" 2>/dev/null &
SRV=$!
for _ in $(seq 50); do [ -s "$T/port" ] && break; sleep 0.1; done
B="http://127.0.0.1:$(cat "$T/port")"

export WATCH_STATE_DIR=$T/wstate WATCH_INBOX=$T/inbox WATCH_TG=$T/tg WATCH_HOST_GAP=0 WATCH_TIMEOUT=8
export DEALS_STATE_DIR=$T/dstate DEALS_CONFIG=$T/config.json DEALS_PAGE_DELAY=0
export DEALS_ALTERNATE_BASE=$B/alt DEALS_MEDIAMARKT_BASE=$B/mm
unset DEALS_SAVE_RAW
cat > "$T/tg" <<STUB
#!/bin/sh
printf '%s\n<<END>>\n' "\$*" >> "$T/tg.log"
STUB
chmod +x "$T/tg"

pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2" | head -15; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
# has/hasnt test the substring in the shell: `printf | grep -q` under pipefail reads as no match when grep exits
# on the match before printf (one write per line) is done, SIGPIPE (a false FAIL, or a false pass for hasnt)
has() { if [[ $2 == *"$3"* ]]; then ok "$1"; else no "$1" "$2"; fi; }
hasnt() { if [[ $2 == *"$3"* ]]; then no "$1" "$2"; else ok "$1"; fi; }
reqs() { local n; n=$(grep -c -- "$1" "$T/server.log" 2>/dev/null); echo "${n:-0}"; }
inboxn() { ls "$T/inbox" 2>/dev/null | wc -l | tr -d ' '; }
inboxlast() { f=$(ls -t "$T"/inbox/* 2>/dev/null | head -1); [ -n "$f" ] && cat "$f"; }
tgn() { local n; n=$(grep -c '^<<END>>$' "$T/tg.log" 2>/dev/null); echo "${n:-0}"; }
T0=$(date -d "2026-10-10 12:00" +%s)
at() { echo $((T0 + $1 * 60)); }                       # minutes after T0
D() { local m=$1; shift; WATCH_NOW=$(at "$m") timeout 60 "$DEALS" "$@" 2>&1; }
wrun() { WATCH_NOW=$(at "$1") timeout 60 "$WATCH" run --force >/dev/null 2>&1; }
# set the price of an Alternate card (all fixture pages it appears on): altprice "MG10 20 TB, Festplatte" "CHF 690,00"
altprice() { python3 -I - "$WWW/alt" "$1" "$2" <<'EOF'
import os, re, sys
d, name, new = sys.argv[1:4]
n = 0
for fn in os.listdir(d):
    p = os.path.join(d, fn)
    t = open(p, encoding='utf-8').read()
    out, pos = [], 0
    for m in re.finditer(r'class="product-name[^"]*"><span>[^<]*</span>\s*' + re.escape(name) + r'\s*</div>', t):
        pm = re.compile(r'(<span class="price\b[^"]*">)\s*[^<]+?\s*(</span>)').search(t, m.end())
        out.append(t[pos:pm.start()] + pm.group(1) + new + pm.group(2))
        pos = pm.end()
        n += 1
    if pos:
        open(p, 'w', encoding='utf-8').write(''.join(out) + t[pos:])
print(n)
EOF
}

echo "# A. help, sources, refusals without network"
out=$(D 0 --help); has "help lists hdd" "$out" "deals hdd [--min-tb 16]"
out=$(D 0 sources); has "sources: Galaxus refused" "$out" "refused  Galaxus.ch"; has "sources: Alternate used" "$out" "used     Alternate.ch"
out=$(D 0 watch "https://www.galaxus.ch/de/s1/product/x-123" --below 500); rc=$?
eq "galaxus URL refused (rc 2)" "$rc" "2"; has "  with the reason" "$out" "bot protection"
out=$(D 0 watch "https://www.digitec.ch/de/s1/product/y-1" --below 5); has "digitec refused" "$out" "refused: www.digitec.ch"
out=$(D 0 watch "https://www.ricardo.ch/de/s/rtx%205080" --below 700); has "ricardo URL refused" "$out" "CAPTCHA"
out=$(D 0 watch "https://www.toppreise.ch/preisvergleich/x" --below 1); has "toppreise refused" "$out" "blocks this network"
out=$(D 0 ricardo "RTX 5080" --max 700); rc=$?
eq "deals ricardo refuses (rc 3)" "$rc" "3"; has "  and suggests a saved search" "$out" "Suche speichern"
eq "no request was made so far" "$(reqs ' ')" "0"
out=$("$WATCH" add page "https://www.brack.ch/foo" 2>&1); has "watch itself refuses bot-protected hosts" "$out" "refused: www.brack.ch"

echo "# B. deals hdd: fetch, parse, rank (synthetic Alternate + MediaMarkt pages)"
out=$(D 0 hdd --top 5)
has "fetched now" "$out" "· fetched now"
has "#1 is the refurbished Exos 28 TB at 22.57 CHF/TB" "$out" " 1.  22.57 CHF/TB · CHF 632.00 · 28 TB · Seagate Exos 28 TB Generalüberholt, Festplatte [refurb] · Alternate"
has "  with its (absolute shop) link" "$out" "https://www.alternate.ch/Seagate/Exos-28-TB-Generalüberholt-Festplatte/html/product/1000008"
has "Alternate: 4 pages, 12 drives" "$out" "Alternate ok (4 pages, 12 drives)"
has "MediaMarkt: 3 pages, 5 drives" "$out" "MediaMarkt ok (3 pages, 5 drives)"
has "trend line: best new >=18 TB" "$out" "best 32.78 CHF/TB (Toshiba N300 18 TB, Festplatte @ Alternate), median 37.46, p10 35.70, 11 drives"
has "first fetch: no trend yet" "$out" "first fetch, no trend yet"
has "no threshold yet" "$out" "no threshold yet"
has "skipped shops named" "$out" "Skipped (bot protection or refused): Galaxus/Digitec"
eq "Alternate: exactly 4 page requests (stops after a page without a buyable big drive)" "$(reqs '^/alt/Festplatten')" "4"
eq "MediaMarkt: 3 page requests (max_page from the page state)" "$(reqs '^/mm/de/category')" "3"
eq "robots.txt read once" "$(reqs '^/robots.txt')" "1"
eq "every request used the honest UA" "$(grep -vc 'UA=personal-watch/1.0' "$T/server.log")" "0"
has "MediaMarkt filter: 3.5 inch, capacity >= 16000 GB, price asc" "$(cat "$T/server.log")" "hddSsdFormFactor=3.5%20Zoll&capacity=16000-100000&sort=currentprice+asc&page=2"

echo "# C. filters and output modes (cached: no new requests)"
n0=$(reqs ' ')
out=$(D 1 hdd --new --top 3); has "--new: top is the N300 18 TB" "$out" " 1.  32.78 CHF/TB · CHF 590.00 · 18 TB · Toshiba N300 18 TB, Festplatte · Alternate"
hasnt "--new: no refurb" "$out" "[refurb]"; has "uses the cache" "$out" "cached fetch from 2026-10-10 12:00"
out=$(D 1 hdd --any-stock --top 2); has "--any-stock: unbuyable HC560 shows, flagged" "$out" "18.45 CHF/TB · CHF 369.00 · 20 TB · WD Ultrastar DC HC560 20 TB, Festplatte [Artikel kann derzeit nicht gekauft werden]"
out=$(D 1 hdd --top 40); hasnt "default hides unbuyable listings" "$out" "HC560 20 TB"; hasnt "default hides desktop lines" "$out" "BarraCuda"
hasnt "default hides SAS" "$out" "0F00042"
has "MediaMarkt marketplace seller shown" "$out" "MediaMarkt · Muster Handels AG"
has "MG11ACA (no digit after the letters) is a Toshiba MG" "$out" "Toshiba MG11ACA 22 TB, Festplatte · Alternate"
has "MediaMarkt 'extern' Exos counted as internal" "$out" "SEAGATE Exos 28TB HDD 512E"
out=$(D 1 hdd --all --top 40); has "--all adds BarraCuda" "$out" "Seagate BarraCuda 24 TB, Festplatte"
out=$(D 1 hdd --sas --top 60); has "--sas adds SAS Ultrastar" "$out" "0F00042"
out=$(D 1 hdd --min-tb 20 --max-tb 22 --new --top 50); hasnt "--min/--max-tb: no 18 TB" "$out" " 18 TB ·"; hasnt "  no 24 TB" "$out" " 24 TB ·"
has "  22 TB MG10 present" "$out" "Toshiba MG10 22 TB, Festplatte"
out=$(D 1 hdd --json); python3 -c 'import json,sys; j=json.loads(sys.argv[1]); assert j["rows"][0]["cpt"]==22.57 and j["trend"]["p10"]==35.7' "$out" 2>/dev/null
eq "--json is valid JSON with rows and trend" "$?" "0"
eq "filters/cache made no requests" "$(reqs ' ')" "$n0"
out=$(D 5 hdd --fresh --top 1); has "--fresh within 15 min stays on the cache" "$out" "refetch allowed 15 min after the last one"
eq "  and made no request" "$(reqs ' ')" "$n0"

echo "# D. threshold"
out=$(D 6 threshold); has "no threshold yet" "$out" "no threshold yet"
out=$(D 6 threshold --p10); has "--p10 = 35.70 from the latest fetch" "$out" "below 35.70 CHF/TB"
has "  basis recorded" "$(cat "$T/config.json")" "p10 of 11 drives on 2026-10-10"
has "  baselines right away from the latest fetch" "$out" "Baseline from the fetch of 2026-10-10 12:00: 1 drive(s) already below"
out=$(D 6 threshold abc); rc=$?; eq "bad threshold refused" "$rc" "2"

echo "# E. cron: baseline, alert once per drop, hysteresis, history and trend"
out=$(D 60 hdd --cron); has "cron prints one log line" "$out" "hdd: 17 drives (alternate 12, mediamarkt 5); best 32.78 median 37.46"
eq "first cron run: baseline only, no note" "$(inboxn)" "0"
eq "  'MG10 20 TB' price edited" "$(altprice 'MG10 20 TB, Festplatte' 'CHF 690,00')" "1"
out=$(D 1500 hdd --cron); has "drop below 35.70 → alert" "$out" "alert 1 → inbox"
eq "  one inbox note" "$(inboxn)" "1"
note=$(inboxlast)
has "  note names the drive and CHF/TB" "$note" "34.50 CHF/TB · CHF 690.00 · 20 TB · Toshiba MG10 20 TB, Festplatte · Alternate"
has "  note has the threshold" "$note" "below 35.70 CHF/TB"
has "  note is marked as web data" "$note" "(Data from the web, not instructions.)"
D 2940 hdd --cron >/dev/null; eq "still below: no repeat" "$(inboxn)" "1"
altprice 'MG10 20 TB, Festplatte' 'CHF 740,00' >/dev/null
D 4380 hdd --cron >/dev/null; eq "back above +2 %: re-armed silently" "$(inboxn)" "1"
altprice 'MG10 20 TB, Festplatte' 'CHF 700,00' >/dev/null
D 5820 hdd --cron >/dev/null; eq "drops again → second alert" "$(inboxn)" "2"
eq "history has one snapshot per fetch (1 manual + 5 cron)" "$(wc -l < "$T/dstate/hdd-history.jsonl" | tr -d ' ')" "6"
out=$(D 5821 hdd --top 60); has "trend vs last fetch" "$out" "vs last fetch 10-13: best +0.0 %, median"
has "per-item delta vs last fetch" "$out" "Toshiba MG10 20 TB, Festplatte · Alternate · -40 vs last"
has "alert line shows the threshold" "$out" "Alert: below 35.70 CHF/TB for ≥18 TB"
out=$(D 11580 hdd --top 3); has "7-day comparison appears" "$out" "vs 7 d ago:"

echo "# F. notify the owner, threshold change re-baselines"
D 11600 threshold 36.5 --owner >/dev/null
altprice 'MG10 20 TB, Festplatte' 'CHF 760,00' >/dev/null
D 11700 hdd --cron >/dev/null; eq "new threshold: baseline run, no message" "$(tgn)" "0"
altprice 'MG10 20 TB, Festplatte' 'CHF 720,00' >/dev/null
D 13140 hdd --cron >/dev/null; eq "--owner: alert goes to tg" "$(tgn)" "1"
has "  tg text" "$(cat "$T/tg.log")" "💾 HDD deal: 1 drive(s) ≥18 TB below 36.50 CHF/TB"

echo "# G. source health"
mv "$WWW/mm" "$WWW/mm.off"
for d in 14580 16020; do D $d hdd --cron >/dev/null; done
n=$(inboxn); D 17460 hdd --cron >/dev/null
eq "MediaMarkt broken 3 runs → one note" "$(inboxn)" "$((n + 1))"
has "  the note" "$(inboxlast)" "source MediaMarkt failed 3 daily runs"
D 18900 hdd --cron >/dev/null; eq "  no repeat on run 4" "$(inboxn)" "$((n + 1))"
mv "$WWW/mm.off" "$WWW/mm"
D 20340 hdd --cron >/dev/null; has "recovery note" "$(inboxlast)" "works again"
out=$(D 20341 hdd); has "auto threshold not overwritten" "$out" "below 36.50"

echo "# H. auto threshold on the first cron run"
out=$(DEALS_CONFIG=$T/config2.json D 20400 hdd --cron); has "first cron sets p10" "$out" "threshold auto-set to"
has "  saved" "$(cat "$T/config2.json")" '"chf_per_tb"'

echo "# I. deals watch: Alternate search → offer watch"
before=$(reqs ' ')
out=$(D 20500 watch "IronWolf Pro 24 TB" --below 800 --until 2026-12-31)
has "probe lists matching products" "$out" "649.00 CHF Seagate IronWolf Pro NAS 24 TB CMR Generalüberholt, Festplatte"
has "  marks the one already below" "$out" "← already below"
has "added as an offer watch" "$out" "offer $B/alt/listing.xhtml?q=IronWolf+Pro+24+TB below 800 match \"IronWolf Pro 24 TB\""
eq "probe = 1 request" "$(( $(reqs ' ') - before ))" "1"
out=$(D 20500 list); has "deals list shows it" "$out" "[me] offer"
n=$(inboxn); wrun 20520
eq "watch run: one note for the product below 800" "$(inboxn)" "$((n + 1))"
has "  names the product and price" "$(inboxlast)" "Seagate IronWolf Pro NAS 24 TB CMR Generalüberholt, Festplatte now 649.00 CHF, below 800"
wrun 20540; eq "  no repeat" "$(inboxn)" "$((n + 1))"
altprice 'IronWolf Pro NAS 24 TB CMR Generalüberholt, Festplatte' 'CHF 820,00' >/dev/null
wrun 20560; eq "  back above: re-armed, silent" "$(inboxn)" "$((n + 1))"
altprice 'IronWolf Pro NAS 24 TB CMR Generalüberholt, Festplatte' 'CHF 700,00' >/dev/null
wrun 20580; eq "  drops again → note" "$(inboxn)" "$((n + 2))"
out=$(WATCH_NOW=$(at 20590) "$WATCH" check 1); has "watch check shows offers" "$out" "offer: 700.00 CHF Seagate IronWolf Pro NAS 24 TB CMR Generalüberholt"
out=$(D 20600 rm 1); has "deals rm" "$out" "removed #1"

echo "# J. deals watch URL: JSON-LD product page, --owner"
P="$B/alt/Toshiba/MG10-20-TB-Festplatte/html/product/1000006"
out=$(D 20700 watch "$P" --below 700 --owner); has "product page probe via JSON-LD" "$out" "(json-ld): 1 product(s)"
has "  current price" "$out" "723.00 CHF MG10 20 TB, Festplatte (seller: ALTERNATE GmbH) [in stock]"
id=$("$WATCH" list | grep -F "$P" | sed -E 's/^#([0-9]+).*/\1/')
t0=$(tgn); wrun 20720; eq "723 > 700: no message" "$(tgn)" "$t0"
sed -i 's/"price": "723.0"/"price": "650.0"/' "$WWW/alt/$(python3 -c 'import urllib.parse;print(urllib.parse.quote("/Toshiba/MG10-20-TB-Festplatte/html/product/1000006",safe=""))')"
wrun 20740; eq "650 < 700: owner texted once" "$(tgn)" "$((t0 + 1))"
has "  tg text" "$(tail -2 "$T/tg.log")" "🔔 MG10 20 TB, Festplatte now 650.00 CHF (seller: ALTERNATE GmbH), below 700 · https://www.alternate.ch/Toshiba/MG10-20-TB-Festplatte/html/product/1000006"
"$WATCH" rm "$id" >/dev/null

echo "# K. refusals and failures at add time"
out=$(D 20800 watch "$B/mm/de/search.html?query=hdd" --below 100); rc=$?
eq "robots wildcard rule refuses */search.html?query=* (rc 2)" "$rc" "2"; has "  says robots.txt" "$out" "robots.txt disallows"
out=$(D 20800 watch "$B/mm/de/category/x.html?searchParams=1" --below 100); has "robots /*searchParams=* refused" "$out" "robots.txt disallows"
out=$(D 20800 watch "$B/captcha/product/1" --below 100); rc=$?
eq "CAPTCHA page refused (rc 2)" "$rc" "2"; has "  named as a bot check" "$out" "CAPTCHA/bot check"
out=$(D 20800 watch "$B/plain.html" --below 100); rc=$?
eq "page without a price: nothing added (rc 1)" "$rc" "1"; has "  explains" "$out" "no machine-readable price"
out=$(D 20800 watch "$B/code/503" --below 100); rc=$?; eq "HTTP error: nothing added (rc 1)" "$rc" "1"
eq "no watches left behind" "$("$WATCH" list)" "(no watches)"

echo "# L. watch internals: robots matcher, prices"
out=$(python3 -I - <<'EOF'
import importlib.util, importlib.machinery
import os; ld = importlib.machinery.SourceFileLoader('w', os.path.expanduser('~/bin/watch'))
s = importlib.util.spec_from_loader('w', ld); W = importlib.util.module_from_spec(s); ld.exec_module(W)
ra = W.robots_allowed
r = "User-agent: *\nDisallow: /*?sort=\nDisallow: /p\nAllow: /p$\nDisallow: /*.pdf$\n"
checks = [ra(r, 'personal-watch', 'https://x/a?sort=1') is False, ra(r, 'personal-watch', 'https://x/a?q=1') is True,
          ra(r, 'personal-watch', 'https://x/p') is True, ra(r, 'personal-watch', 'https://x/pa') is False,
          ra(r, 'personal-watch', 'https://x/doc.pdf') is False, ra(r, 'personal-watch', 'https://x/doc.pdf?x') is True,
          ra("User-agent: personal-watch\nDisallow: /\n\nUser-agent: *\nAllow: /\n", 'personal-watch', 'https://x/') is False,
          ra("User-agent: other\nDisallow: /\n", 'personal-watch', 'https://x/') is True,
          ra("User-agent: *\nDisallow: /en/*\nAllow: /*/s/\n", 'personal-watch', 'https://x/en/s/q') is True,
          W.parse_price('CHF 1.522,00') == 1522.0, W.parse_price("1'522.–") == 1522.0, W.parse_price('723.0') == 723.0,
          W.parse_price('CHF 161,90') == 161.9, W.parse_price('n/a') is None,
          W.offer_matches({'name': 'WD Red Pro 22TB, Festplatte'}, 'red pro 22 TB'),
          not W.offer_matches({'name': 'WD Red Pro 24 TB'}, 'red pro 22tb')]
print(''.join('1' if c else '0' for c in checks))
EOF
)
eq "robots wildcards/\$/tie rules, price parsing, match normalisation" "$out" "1111111111111111"

echo "# N. deals tutti: used drives via the browser (stubbed tutti-fetch, synthetic pages)"
mkdir -p "$T/tutti"; cp "$HERE"/fixtures/tutti/*.html "$T/tutti/"
export DEALS_TUTTI_FETCH=$HERE/fake_tutti_fetch.py DEALS_TUTTI_DELAY=0 TUTTI_FIX_DIR=$T/tutti TUTTI_FETCH_LOG=$T/tutti-fetch.log
lt() { python3 -I - "$1" <<'EOF'   # epoch of a local wall-clock time in watch's time zone
import importlib.machinery, importlib.util, sys
from datetime import datetime
import os; ld = importlib.machinery.SourceFileLoader('w', os.path.expanduser('~/bin/watch'))
s = importlib.util.spec_from_loader('w', ld); W = importlib.util.module_from_spec(s); ld.exec_module(W)
print(int(datetime.strptime(sys.argv[1], '%Y-%m-%d %H:%M').replace(tzinfo=W.TZ).timestamp()))
EOF
}
DAY=$(lt "2026-10-12 10:00"); NIGHT=$(lt "2026-10-12 04:00")
TT() { local now=$1; shift; WATCH_NOW=$now timeout 60 "$DEALS" tutti "$@" 2>&1; }
fetchn() { local n; n=$(wc -l < "$T/tutti-fetch.log" 2>/dev/null); echo "${n:-0}"; }
tnotes() { ls "$T"/inbox/deals-hdd-tutti-* 2>/dev/null | wc -l | tr -d ' '; }
snotes() { ls "$T"/inbox/deals-source-tutti-* 2>/dev/null | wc -l | tr -d ' '; }
lastof() { local f; f=$(ls -t "$T"/inbox/"$1"-* 2>/dev/null | head -1); [ -n "$f" ] && cat "$f"; }
tadd() { python3 -I - "$@" <<'EOF'   # tadd PAGE ID TITLE PRICE: a new listing on top of a synthetic page
import json, re, sys
p, lid, title, price = sys.argv[1:5]
h = open(p, encoding='utf-8').read()
m = re.search(r'(<script id="__NEXT_DATA__"[^>]*>)(.*?)(</script>)', h, re.S)
d = json.loads(m.group(2))
L = d['props']['pageProps']['dehydratedState']['queries'][0]['state']['data']['listings']
L['edges'].insert(0, {'node': {'listingID': lid, 'localization': {'title': title, 'body': ''}, 'formattedPrice': price,
                               'postcodeInformation': {'locationName': 'Neudorf'}, 'timestamp': '2026-10-12T09:00:00+02:00'}})
L['totalCount'] += 1
open(p, 'w', encoding='utf-8').write(h[:m.start(2)] + json.dumps(d, ensure_ascii=False) + h[m.end(2):])
EOF
}
out=$(D 0 sources); has "sources: Tutti via the browser" "$out" "browser  Tutti.ch"
out=$(TT "$DAY" "16TB"); rc=$?
eq "tutti search rc 0" "$rc" "0"
has "  header: 2 matches of 8" "$out" '8 listing(s) on page 1 (8 total), 2 match (12–16 TB, ≤ CHF 250, over budget up to 300)'
has "  budget match: price, size, title, place, date, slug link" "$out" 'CHF 240 · 16 TB · WD Elements Desktop 16TB · Musterdorf (XX) · 09.10. · https://www.tutti.ch/de/vi/testkanton/'
has "  over budget marked negotiable" "$out" 'CHF 280 · over budget, negotiable? · 16 TB · Seagate Exos 16TB SATA'
has "  no slug → /de/vi/<id>" "$out" 'https://www.tutti.ch/de/vi/900002'
has "  body excerpt" "$out" 'keine defekten Sektoren'
hasnt "  SAS filtered" "$out" 'EXOS 16TB SAS'
hasnt "  over CHF 300 filtered" "$out" 'Toshiba MG08'
has "  filtered count" "$out" '(6 filtered out'
eq "  one page fetched" "$(fetchn)" "1"
has "  newest-first search URL" "$(cat "$T/tutti-fetch.log")" 'https://www.tutti.ch/de/q/suche?query=16TB&sorting=newest'
out=$(TT "$DAY" "16TB" --all)
has "--all: SAS with reason" "$out" '✗ SAS · CHF 150'
has "--all: SSD computer" "$out" '✗ SSD · CHF 2400'
has "--all: dock" "$out" '✗ enclosure/dock/accessory'
has "--all: NAS" "$out" '✗ NAS'
has "--all: defect" "$out" '✗ defect ("defekt")'
has "--all: above the stretch price" "$out" '✗ CHF 320 > 300'
out=$(TT "$DAY" "14TB" --max 200)
has "--max 200: 2x 14TB at CHF 220 each is over budget (stretch 240)" "$out" 'CHF 440 for 2 (CHF 220 each) · over budget, negotiable? · 14 TB'
has "  2.5\", 18 TB and > stretch filtered" "$out" '(3 filtered out'
out=$(TT "$DAY" "My Book" --json)
eq "--json: no-price listing → ask" "$(printf '%s' "$out" | python3 -I -c 'import json,sys; print([(r["bucket"], r["tb"]) for r in json.load(sys.stdin)])')" "[('ask', 14.0)]"
TT "$DAY" >/dev/null; eq "no query: usage (rc 2)" "$?" "2"

: > "$T/tutti-fetch.log"
out=$(TT "$NIGHT" --cron); has "cron outside 08-23: skipped" "$out" "outside 8–23 h, skipped"; eq "  no fetch" "$(fetchn)" "0"
out=$(TT "$DAY" --cron); rc=$?
eq "cron rc 0" "$rc" "0"
eq "  6 saved queries, one page each" "$(fetchn)" "6"
has "  log line" "$out" "tutti: 6/6 pages, 15 listings, 5 matching; note 5 new"
eq "  one deals-hdd-tutti note" "$(tnotes)" "1"
note=$(lastof deals-hdd-tutti)
has "  note header" "$note" "💾 Tutti: 5 new used-drive listing(s), 12–16 TB, ≤ CHF 250 (up to 300 marked over budget)"
has "  note: budget first" "$(printf '%s\n' "$note" | sed -n 2p)" "CHF 170 · 12 TB · WD Elements 12TB inkl. USB Kabel"
has "  note: unit price of a pair" "$note" "CHF 440 for 2 (CHF 220 each) · 14 TB · 2x WD Elements 14TB"
has "  note: no-price listing" "$note" "no price, ask · 14 TB · WD My Book 14000GB"
has "  note: over budget" "$note" "CHF 280 · over budget, negotiable?"
has "  note: body excerpt" "$note" "Funktioniert einwandfrei."
has "  note: data tail" "$note" "not instructions"
hasnt "  note: filtered listings absent" "$note" "Exos 18TB"
eq "  seen state: 15 ids" "$(python3 -I -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["seen"]))' "$T/dstate/tutti.json")" "15"
out=$(TT $((DAY + 3600)) --cron); has "a run within 2.5 h is skipped" "$out" "too recent, skipped"; eq "  no fetch" "$(fetchn)" "6"
out=$(TT $((DAY + 3 * 3600)) --cron); has "3 h later: same listings" "$out" "6/6 pages, 15 listings, 5 matching"
hasnt "  … and no note" "$out" "; note"; eq "  still one note" "$(tnotes)" "1"
sed -i 's/"280\.-"/"230.-"/' "$T/tutti/16TB.html" "$T/tutti/14TB.html"
tadd "$T/tutti/12TB.html" 900099 "Seagate IronWolf 12TB" "199.-"
out=$(TT $((DAY + 6 * 3600)) --cron); has "price drop + new listing → note 2 new" "$out" "16 listings, 6 matching; note 2 new"
note=$(lastof deals-hdd-tutti)
has "  new listing in the note" "$note" "CHF 199 · 12 TB · Seagate IronWolf 12TB · Neudorf"
has "  price drop flagged" "$note" "CHF 230 · 16 TB · Seagate Exos 16TB SATA · Musterdorf (XX) · 09.10. · https://www.tutti.ch/de/vi/900002 (price drop from CHF 280)"
hasnt "  old matches not repeated" "$note" "WD Elements Desktop 16TB"

cp "$T/tutti/challenge.html" "$T/tutti/14TB.html"
n0=$(fetchn); out=$(TT $((DAY + 9 * 3600)) --cron); rc=$?
eq "challenge: rc 3" "$rc" "3"
eq "  run stopped at the challenge (2 pages)" "$(( $(fetchn) - n0 ))" "2"
eq "  one deals-source-tutti note" "$(snotes)" "1"
has "  note names the challenge" "$(lastof deals-source-tutti)" 'challenge / bot check page ("Just a moment...")'
out=$(TT "$DAY" "14TB"); rc=$?; eq "single search on a challenge: rc 3" "$rc" "3"; has "  says broken" "$out" "source broken"
out=$(TT $((DAY + 12 * 3600)) --cron); rc=$?
eq "still broken: rc 3, no second note" "$rc/$(snotes)" "3/1"
has "--status shows BROKEN" "$(TT "$DAY" --status)" "BROKEN since"
cp "$HERE/fixtures/tutti/14TB.html" "$T/tutti/14TB.html"
out=$(TT $((DAY + 24 * 3600)) --cron); rc=$?
eq "recovered: rc 0 + a works-again note" "$rc/$(snotes)" "0/2"
has "  works-again text" "$(lastof deals-source-tutti)" "works again"
cp "$T/tutti/nodata.html" "$T/tutti/My%20Book.html"
out=$(TT $((DAY + 27 * 3600)) --cron); rc=$?
eq "no __NEXT_DATA__: rc 3 + broken note" "$rc/$(snotes)" "3/3"
has "  says layout changed" "$(lastof deals-source-tutti)" "layout changed"
cp "$HERE/fixtures/tutti/My%20Book.html" "$T/tutti/My%20Book.html"
touch "$T/tutti/DOWN"
out=$(TT $((DAY + 30 * 3600)) --cron); rc=$?
eq "browser unreachable: rc 1, no source note" "$rc/$(snotes)" "1/3"
has "  error on stderr" "$out" "browser not reachable"
rm -f "$T/tutti/DOWN"
n0=$(tnotes); out=$(DEALS_STATE_DIR=$T/dstate2 TT "$DAY" --cron --baseline); rc=$?
eq "--baseline on fresh state: rc 0, no note" "$rc/$(tnotes)" "0/$n0"
has "  prints the current matches" "$out" "(baseline)"
has "  … listed" "$out" "WD Elements Desktop 16TB"
TF=(${TUTTI_FETCH_BIN:-$HOME/bin/tutti-fetch})
command -v node >/dev/null || TF=(bun "${TF[0]}")  # no node here (the CI runner): bun runs the node script
timeout 10 "${TF[@]}" "https://www.example.com/de/q/x" >/dev/null 2>&1 </dev/null; eq "tutti-fetch refuses other hosts (rc 2)" "$?" "2"
timeout 10 "${TF[@]}" "https://www.tutti.ch/de/vi/123" >/dev/null 2>&1 </dev/null; eq "tutti-fetch refuses non-search paths (rc 2)" "$?" "2"
out=$(BROWSER_HOST_SWITCH=/dev/null/none BROWSER_CDP_PORT=9 timeout 15 "${TF[@]}" "https://www.tutti.ch/de/q/suche?query=x" 2>&1 </dev/null); rc=$?
eq "tutti-fetch without a browser: rc 4" "$rc" "4"; has "  JSON error line" "$out" '"ok":false'

echo "# M. isolation"
eq "real deals/watch state and config untouched" "$(sum $REAL_D)$(sum $REAL_W)$(sum $REAL_C)$(sum $REAL_T)" "$real_before"

echo "pass=$pass fail=$fail"
[ "$fail" = 0 ]
