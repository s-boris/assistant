# ruff: noqa: E501  (fixture data and expected cards copied verbatim; long strings)
"""tg-cmd's picture cards (TG_CMD_PICTURES=1: /space, /gym, /finish, /vibe, /train board, /trip card, /week) and
cardimg.py (the staged bin/lib copy). They render with Pillow under the tools' python3 (PATH's), so the hub runner,
whose python3 has none, skips this file (tests/runner-skip.txt).

Isolated as test_tg_cmd.py: a temp HOME, the staged tg-cmd, fake sources in TG_CMD_BIN, cards in a temp
CARDIMG_DIR, the profile is the synthetic test fixture, the clock pinned. Made-up data only."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tgcmd_testlib import Box, Week, cat_history, nlines, script  # noqa: E402

NOW = "2031-03-05 12:00"
BROKEN = 'raise RuntimeError("no fonts")\n'

# --------------------------------------------------------------------------- /space, /gym, /cat, /finish, /vibe

FAKE_CAP = """#!/usr/bin/env bash
printf 'unraid-capacity %s\\n' "$*" >> "$FAKE_LOG"
[ -n "${FAKE_CAP_ERR:-}" ] && { echo "unraid-capacity: $FAKE_CAP_ERR" >&2; exit 1; }
cat <<'JSON'
{"ok": true, "host": "testhost", "as_of": "x", "as_of_local": "2031-03-05 11:00", "sample_age_h": 0.2,
 "array": {"size": 100000000000000, "used": 91000000000000, "free": 9000000000000, "usable": 8000000000000, "pct": 91.0,
           "eta": {"days": 40.0, "date": "2031-04-14"}},
 "model": {"rate": 200000000000}, "disks": {"disk1": {"size": 1, "status": "DISK_OK"}},
 "pools": {"cache": {"pct": 40.0, "free": 600000000000}}, "alerts": [{"key": "array", "msg": "array full in 40 days"}]}
JSON
"""
FAKE_GYM = """#!/usr/bin/env bash
printf 'gym %s\\n' "$*" >> "$FAKE_LOG"
case "$1" in
  plan) [ -n "${FAKE_GYM_ERR:-}" ] && { echo "gym: $FAKE_GYM_ERR" >&2; exit 1; }
        printf '🏋️ Next: A · Squat day (~45 min)\\nLast session 01.03. (B, 4 d ago) · streak 2 wk\\n\\n1  Squat  3×5 @ 60 kg\\n' ;;
  stats) printf '{"weekly": [{"start": "2031-02-24", "sessions": 2, "hard_sets": 20}, {"start": "2031-03-03", "sessions": 1, "hard_sets": 9}], "consistency": {"streak_weeks": 2, "days_since_last": 4}}\\n' ;;
  chart) [ -n "${FAKE_CHART_ERR:-}" ] && { echo "chart broke" >&2; exit 1; }
         mkdir -p "$5"; python3 -c 'from PIL import Image; import sys; Image.new("RGB", (1080, 620), (250, 250, 248)).save(sys.argv[1])' "$5/sessions.png"
         echo "$5/volume.png"; echo "$5/sessions.png" ;;
esac
"""  # noqa: E501
# fake media: finish/vibe JSON (made-up shows and films) and poster (small JPEGs in $FAKE_POSTERS; id 119 = no poster,
# id 173 = an unreadable file)
FAKE_MEDIA = """#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write("media " + " ".join(a) + "\\n")
env = os.environ.get
link = lambda i: f"https://emby.example.test/web/index.html#!/item?id={i}" + env("FAKE_LINK_PAD", "")
if a[0] == "finish":
    if env("FAKE_FIN_ERR"):
        sys.stderr.write("media: finish: Emby unreachable\\n")
        sys.exit(1)
    names = [] if env("FAKE_FIN_EMPTY") else ["Hijack", "Severed Ties", "Slow Wagons", "A Very Long Show Title That Wraps Twice Or More", "Quiet", "Andromeda"]
    items = [{"id": str(70 + k), "series_id": str(170 + k), "title": t, "episode": f"S01E0{k + 1}", "pos_min": 12 if k == 0 else 0,
              "left": k + 1, "total": 8, "rating": 7.0 + k / 10, "last_played": f"2031-02-0{k + 1}T20:00:00Z"} for k, t in enumerate(names)]
    text = "🏁 Finish next\\nStarted, paused 14+ days, episodes left\\n\\n" + "\\n".join(
        f"📺 {x['title']} · {x['left']} of 8 left\\n   ▶ {link(x['id'])}" for x in items)
    print(json.dumps({"items": items, "total": 9, "idle_days": 14, "text": text}))
elif a[0] == "vibe":
    series = bool(env("FAKE_VB_SERIES"))
    picks = [] if env("FAKE_VB_NONE") else [
        {"emby_id": i, "title": t, "year": 2020 + k, "rating": 7.5, "runtime_min": None if series else 90 + k,
         "why": f"Comedy, Drama · cozy · “A tagline {k}”" + (" · 👁 3 of 10 eps watched" if series else ""), "link": link(i),
         **({"kind": "series", "episodes": 10, "watched": 3 if k == 0 else 0} if series else {})}
        for k, (i, t) in enumerate([("115", "Cozy Little Cafe"), ("116", "Rainy Day"), ("117", "Teapot"), ("119", "Без постера")])]
    print(json.dumps({"kind": "series" if series else "movie", "label": "cozy rainy", "picks": picks,
                      "text": "🎬 Vibe · cozy rainy\\n\\n" + "\\n".join(f"{p['title']}\\n▶ {p['link']}" for p in picks)}))
elif a[0] == "poster":
    if env("FAKE_POSTER_ERR"):
        sys.stderr.write("media: poster: boom\\n")
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
"""  # noqa: E501


class Pics:
    """pictures.sh's world: fake unraid-capacity, gym and media, made-up capacity/cat history, cards in T/cards."""

    def __init__(self, tmp: Path):
        self.box = Box(tmp, profile=True)
        t = self.t = tmp / "pc"
        self.bin, self.cards, self.log = t / "bin", t / "cards", t / "log"
        for d in (self.bin, t / "cap", self.cards):
            d.mkdir(parents=True)
        script(self.bin / "unraid-capacity", FAKE_CAP)
        script(self.bin / "gym", FAKE_GYM)
        script(self.bin / "media", FAKE_MEDIA)
        (t / "cap" / "backfill.jsonl").write_text(
            '{"date": "2031-02-10", "used": 85000000000000, "src": "arr-ledger"}\n'
            '{"date": "2031-02-20", "used": 88000000000000, "src": "arr-ledger"}\n'
        )
        (t / "cap" / "samples.jsonl").write_text(
            '{"date": "2031-03-01", "array": {"size": 100000000000000, "used": 90000000000000}}\nnot json\n'
        )
        cat_history(t / "cat")
        self.log.write_text("")

    def run(self, *args, pic=False, **env):
        t = self.t
        e = dict(
            FAKE_LOG=self.log, TG_CMD_BIN=self.bin, TG_CMD_NOW=NOW, CARDIMG_DIR=self.cards, UNRAID_CAPACITY_STATE_DIR=t / "cap",
            CAT_CORNER_STATE_DIR=t / "cat", FAKE_POSTERS=t / "posters",
        )  # fmt: skip
        if pic:
            e["TG_CMD_PICTURES"] = "1"
        e.update(env)
        return self.box.run(*args, env=e)

    def pic(self, *args, **env):
        return self.run(*args, pic=True, **env)

    def calls(self) -> str:
        return self.log.read_text().rstrip("\n")


@pytest.fixture
def pc(tmp_path):
    return Pics(tmp_path)


def test_space(pc):
    t = pc.t
    text = pc.run("space")
    assert text.rc == 0
    assert "Array 91% full" in text.out  # flag off: the text card
    r = pc.pic("space")
    assert r.rc == 0
    d = r.j()
    assert d["photo"] == str(pc.cards / "space.png")
    assert pc.box.png_size(d["photo"]) == "1080x1350"
    assert oct(os.stat(d["photo"]).st_mode & 0o777) == "0o600"
    assert d["caption"] == "💾 Array 91% full · 9.0 TB free · full ~14.04.2031"
    assert d["text"] == text.out
    assert nlines(r.out) == 1  # one line of JSON
    r = pc.pic("space", FAKE_CAP_ERR="ssh down")
    assert r.rc == 3
    assert "⚠️ /space failed: capacity history: ssh down." in r.out  # the usual ⚠️ text, no JSON
    (t / "broken.py").write_text(BROKEN)
    r = pc.pic("space", TG_CMD_CARDIMG=t / "broken.py")
    assert r.rc == 0
    assert r.out == text.out  # render failure: the text card instead
    assert "/space picture failed (RuntimeError: no fonts)" in r.err  # logged on stderr
    (t / "cap").rename(t / "cap.off")
    r = pc.pic("space")
    assert (r.rc, "photo" in r.j()) == (0, True)  # no capacity history files: still a picture (one point)


def test_gym(pc):
    gtext = pc.run("gym").out
    assert "🏋️ Next: A · Squat day (~45 min)" in gtext
    pc.log.write_text("")
    r = pc.pic("gym")
    assert r.rc == 0
    d = r.j()
    assert pc.box.png_size(d["photo"]) == "1080x1350"
    assert d["caption"] == "🏋️ Next: A · Squat day (~45 min) · 1 this week"
    assert d["text"] == gtext
    assert f"gym chart --weeks 8 --out {pc.cards}/gym-src" in pc.calls()  # gym chart writes under CARDIMG_DIR/gym-src
    r = pc.pic("gym", FAKE_CHART_ERR="1")
    assert (r.rc, "photo" in r.j()) == (0, True)  # chart fails: still a picture (own bars)
    r = pc.pic("gym", FAKE_GYM_ERR="database is locked")
    assert r.rc == 3
    assert "⚠️ /gym failed: gym: database is locked." in r.out


def test_cat_picture(pc):
    r = pc.pic("cat")
    assert r.rc == 0
    d = r.j()
    assert pc.box.png_size(d["photo"]) == "1080x1350"
    assert d["caption"] == "🐾 Mittens · last 5 days · litter 3 today so far · 9 yesterday · avg 7.5/day"


def test_finish_picture(pc):
    pc.log.write_text("")
    ftext = pc.run("finish")
    assert (ftext.rc, ftext.out.splitlines()[3]) == (0, "📺 Hijack · 1 of 8 left")
    assert pc.calls() == "media finish --json"  # flag off: no posters
    pc.log.write_text("")
    r = pc.pic("finish")
    assert r.rc == 0
    d = r.j()
    assert (d["photo"], pc.box.png_size(pc.cards / "finish.png")) == (str(pc.cards / "finish.png"), "1080x1350")
    # 6 shows asked for, series posters fetched at 480 px
    assert pc.calls() == "media finish --json -n 6\nmedia poster --json --width 480 170 171 172 173 174 175"
    assert d["caption"].splitlines()[:5] == [
        "🏁 Finish next · 6 of 9 started",
        "1️⃣ Hijack · S01E01 · 1 left",
        "▶ https://emby.example.test/web/index.html#!/item?id=70",
        "2️⃣ Severed Ties · S01E02 · 2 left",
        "▶ https://emby.example.test/web/index.html#!/item?id=71",
    ]
    assert d["text"] == ftext.out
    assert nlines(r.out) == 1
    cap = pc.pic("finish", FAKE_LINK_PAD="&pad=" + "x" * 230).j()["caption"]
    # long links: caption ≤ 1024, all 6 shows, the first keeps its link, the last loses it
    assert (len(cap) <= 1024, cap.count("️⃣"), cap.splitlines()[2].startswith("▶ "), cap.splitlines()[-1].startswith("6️⃣")) == (
        True, 6, True, True,
    )  # fmt: skip
    r = pc.pic("finish", FAKE_POSTER_ERR="1")
    assert (r.rc, "photo" in r.j()) == (0, True)  # posters failing: placeholders
    r = pc.pic("finish", FAKE_FIN_EMPTY="1")
    assert (r.rc, r.out.splitlines()[0]) == (0, "🏁 Finish next")  # nothing to finish: the text card, no JSON
    r = pc.pic("finish", FAKE_FIN_ERR="1")
    assert (r.rc, r.out.splitlines()[0]) == (3, "⚠️ /finish failed: Emby finish: media: finish: Emby unreachable.")


def test_vibe_picture(pc):
    pc.log.write_text("")
    r = pc.pic("vibe")
    assert r.rc == 0
    assert "🎬 /vibe + a mood or a film" in r.out  # bare: the hint as text
    assert pc.calls() == ""  # nothing run
    vtext = pc.run("vibe", "cozy rainy").out
    pc.log.write_text("")
    r = pc.pic("vibe", "cozy rainy")
    assert (r.rc, pc.box.png_size(r.j()["photo"])) == (0, "1080x1350")
    assert pc.calls() == "media vibe --json -- cozy rainy\nmedia poster --json --width 480 115 116 117 119"
    cap = r.j()["caption"]
    assert cap.splitlines()[:3] == [
        "🎬 Vibe · cozy rainy",
        "1️⃣ Cozy Little Cafe (2020)",
        "▶ https://emby.example.test/web/index.html#!/item?id=115",
    ]
    assert "4️⃣ Без постера (2023)" in cap  # Cyrillic title in the caption
    assert r.j()["text"] == vtext
    assert pc.pic("vibe", "dark series", FAKE_VB_SERIES="1").j()["caption"].splitlines()[0] == "📺 Vibe · cozy rainy"
    r = pc.pic("vibe", "zzz", FAKE_VB_NONE="1")
    assert (r.rc, r.out.splitlines()[0]) == (0, "🎬 Vibe · cozy rainy")  # no picks: the text, no JSON
    # poster cards never write under HOME (the usage spool would be expected, but it lives outside HOME here)
    files = [p for p in pc.box.home.rglob("*") if p.is_file() and "/.local/state/product/" not in str(p)]
    assert files == []


CARDIMG_BASIC = """
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
"""  # noqa: E501
CARDIMG_POSTERS = """
import importlib.util, json, os, sys
from PIL import Image
spec = importlib.util.spec_from_file_location("cardimg", sys.argv[1]); ci = importlib.util.module_from_spec(spec); spec.loader.exec_module(ci)
os.makedirs(sys.argv[2], exist_ok=True)
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
"""  # noqa: E501


def test_cardimg(pc):
    lib = str(pc.box.bin / "lib" / "cardimg.py")
    out_dir = pc.t / "lib-out"
    d = json.loads(pc.box.py3(CARDIMG_BASIC, lib, env={"CARDIMG_DIR": out_dir}))
    assert d["path"] == str(out_dir / "t.png")
    assert [d["mode"], d["dir"], d["left"]] == ["0o600", "0o700", ["t.png"]]  # no temp file left
    assert d["fit"] == "…"  # long text cut with …
    assert d["json"] == json.dumps({"photo": str(out_dir / "t.png"), "caption": "cap"})
    assert d["sev"] == [True, True, True]
    assert d["paste_missing"] is False
    assert pc.box.png_size(out_dir / "t.png") == "1080x1350"  # odd input renders 1080x1350


def test_cardimg_posters(pc):
    lib = str(pc.box.bin / "lib" / "cardimg.py")
    d = json.loads(pc.box.py3(CARDIMG_POSTERS, lib, str(pc.t / "posters"), env={"CARDIMG_DIR": pc.t / "lib-out2"}))
    # poster_grid fits 1-6 tiles above the reserve (missing/unreadable → placeholder)
    assert d["grid"] == {
        "1": True,
        "2": True,
        "3": True,
        "4": True,
        "5": True,
        "6": True,
        "empty": True,
        "no_room": "ValueError",
    }
    assert [d["size"][0], d["size"][1] == d["h"], 600 <= d["h"] < 1350] == [1080, True, True]  # poster_rows + trim
    assert d["wrap"] == [2, "…"]
    assert [d["fit"], d["none"], d["ph"]] == [[400, 300], True, [100, 150]]


# --------------------------------------------------------------------------- /train board, /trip card (trip-cards.sh)

FAKE_TW = """#!/usr/bin/env bash
printf 'train-watch %s\\n' "$*" >> "$FAKE_LOG"
case "$1" in
  list) [ -n "${FAKE_TW_ERR:-}" ] && { echo "train-watch: $FAKE_TW_ERR" >&2; exit 1; }; cat "$FAKE_TW_LEGS" ;;
  status) [ -f "$FAKE_TW_DIR/$2.json" ] || { echo "train-watch: transport API unreachable" >&2; exit 1; }; cat "$FAKE_TW_DIR/$2.json" ;;
  *) exit 2 ;;
esac
"""
FAKE_FW = """#!/usr/bin/env bash
printf 'flight-watch %s\\n' "$*" >> "$FAKE_LOG"
[ -n "${FAKE_FW_ERR:-}" ] && { echo "flight-watch: state unreadable" >&2; exit 1; }
cat "$FAKE_FW_JSON"
"""
FAKE_TC = """#!/usr/bin/env bash
printf 'trip-conditions %s\\n' "$*" >> "$FAKE_LOG"
[ -n "${FAKE_TC_SLOW:-}" ] && sleep 15
[ -n "${FAKE_TC_ERR:-}" ] && { echo "trip-conditions: forecast down" >&2; exit 1; }
cat "$FAKE_TC_JSON"
"""
BOARD_LEGS = """[{"id": "b1", "from": "home", "to": "Basel SBB", "dep": "2031-03-05T13:10:00+01:00", "arr": "2031-03-05T14:05:00+01:00", "lines": ["IR36"], "title": "🚆 Home → Basel SBB"},
 {"id": "b2", "from": "home", "to": "Bern", "dep": "2031-03-05T14:00:00+01:00", "arr": "2031-03-05T15:00:00+01:00", "lines": ["IC1"], "title": "🚆 Home → Bern"},
 {"id": "b3", "from": "home", "to": "Lausanne", "dep": "2031-03-05T16:00:00+01:00", "arr": "2031-03-05T18:30:00+01:00", "lines": ["IC5", "IR15"], "title": "🚆 Home → Lausanne"},
 {"id": "b4", "from": "Basel SBB", "to": "home", "dep": "2031-03-06T01:40:00+01:00", "arr": "2031-03-06T02:50:00+01:00", "lines": ["SN3"], "title": "🚆 Basel SBB → Home (after Artist One)"},
 {"id": "b5", "from": "home", "to": "Olten", "dep": "2031-03-05T18:00:00+01:00", "arr": "2031-03-05T19:10:00+01:00", "lines": ["Bus 21", "IR16", "IC3", "Tram 9"], "title": "🚆 Home → Olten"},
 {"id": "b6", "from": "home", "to": "Chur", "dep": "2031-03-08T09:00:00+01:00", "arr": "2031-03-08T11:30:00+01:00", "lines": ["IC3"], "title": "🚆 Home → Chur"},
 {"id": "b7", "from": "home", "to": "Thun", "dep": "2031-03-20T10:00:00+01:00", "arr": "2031-03-20T11:00:00+01:00", "lines": [], "title": "🚆 Home → Thun"},
 {"id": "b0", "from": "home", "to": "Aarau", "dep": "2031-03-05T08:00:00+01:00", "arr": "2031-03-05T09:00:00+01:00", "lines": ["IR36"], "title": "🚆 past"}]
"""  # noqa: E501
BOARD_STATUS = {
    "b1": '{"fine": true, "live": {"dep": "13:10", "arr": "14:05", "delay": 0, "platform": "7", "lines": ["IR36"]}, "problems": [], "alt": null}',  # noqa: E501
    "b2": '{"fine": true, "live": {"dep": "14:03", "arr": "15:03", "delay": 3, "platform": "4"}, "problems": [], "alt": null}',
    "b3": '{"fine": false, "live": {"dep": "16:06", "arr": "18:36", "delay": 6, "platform": "3"}, "problems": [{"key": "delay5", "text": "late"}], "alt": null}',  # noqa: E501
    "b4": '{"fine": false, "live": null, "problems": [{"key": "cancelled", "text": "gone"}], "alt": {"dep": "01:55", "arr": "03:05", "lines": ["SN3"]}}',  # noqa: E501
    "b5": '{"fine": false, "live": {"dep": "18:00", "arr": "19:10", "delay": 0, "platform": "9"}, "problems": [{"key": "transfer:Olten Nord", "text": "x"}, {"key": "platform:Zürich HB:9", "text": "y"}, {"key": "bus:Aarau", "text": "z"}], "alt": null}',  # noqa: E501
    "b7": '{"fine": true, "live": {"dep": "10:00", "arr": "11:00", "delay": 0, "platform": "2"}, "problems": [], "alt": null}',
}
TC_CONDITIONS = """{"trips": {"island": {"name": "Island Trip", "emoji": "🌴", "tz": "UTC", "from": "2031-03-14", "to": "2031-03-22",
  "legs": [{"name": "North Isle", "spot": "Bay One", "from": "2031-03-15", "to": "2031-03-18", "dive_label": "Reef A"},
           {"name": "South Isle", "spot": "Bay Two", "from": "2031-03-18", "to": "2031-03-20", "dive_label": "Reef B", "crossing": "Fast ferry North Isle → South Isle"},
           {"name": "East Isle", "from": "2031-03-20", "to": "2031-03-21", "dive_label": "Reef C", "crossing": "Boat South Isle → East Isle"}]}}}
"""  # noqa: E501
TC_EXTRA = """{"trips": [{"name": "Island Trip", "link": "https://example.org/hub", "match": ["island"]},
           {"name": "Family Fest", "emoji": "💍", "from": "2031-03-07", "to": "2031-03-08", "legs": ["🚗 drive 15:00 → Fest Hall"], "match": ["fest"], "todo": ["Pack the gear"]}]}
"""  # noqa: E501
FLIGHTS = """[{"id": "f3", "no": "QZ18", "from": "CCC", "to": "BBB", "dep": "2031-03-21T20:30:00+04:00", "arr": "2031-03-22T01:05:00+04:00"},
 {"id": "f1", "no": "XY100", "from": "AAA", "to": "BBB", "dep": "2031-03-14T21:35:00+01:00", "arr": "2031-03-15T06:45:00+04:00"},
 {"id": "f2", "no": "QZ19", "from": "BBB", "to": "CCC", "dep": "2031-03-15T07:50:00+04:00", "arr": "2031-03-15T12:25:00+04:00"},
 {"id": "f4", "no": "XY101", "from": "BBB", "to": "AAA", "dep": "2031-03-22T02:35:00+04:00", "arr": "2031-03-22T06:45:00+01:00"},
 {"id": "f9", "no": "XY777", "from": "AAA", "to": "DDD", "dep": "2031-05-01T10:00:00+01:00", "arr": "2031-05-01T12:00:00+01:00"},
 {"id": "bad", "no": "XY1", "from": "AAA", "to": "BBB", "dep": "soon"}]
"""
COND = """{"trip": "island", "days": [{"date": "2031-03-01", "mode": "preview", "legs": [
  {"leg": "North Isle", "wind": {"kn": 8.4}, "sea": {"wave_m": 0.8, "temp_c": 28.6}, "dive": {"long_range": "go", "visibility": "good", "why": [], "estimate": true}},
  {"leg": "South Isle", "wind": {"kn": 14.2}, "sea": {"wave_m": 1.5, "temp_c": 28.1}, "dive": {"long_range": "marginal", "visibility": "fair", "why": [], "estimate": true}}]}]}
"""  # noqa: E501
PY_PRELUDE = """
import importlib.machinery, importlib.util, os, sys
sys.dont_write_bytecode = True
ld = importlib.machinery.SourceFileLoader("tgcmd", os.environ["TGCMD"])
m = importlib.util.module_from_spec(importlib.util.spec_from_loader("tgcmd", ld)); ld.exec_module(m)
ci = m.cardimg()
"""


class Trips:
    """trip-cards.sh's world: fake train-watch, flight-watch, trip-conditions and remind; made-up trip files."""

    def __init__(self, tmp: Path):
        self.box = Box(tmp, profile=True)
        t = self.t = tmp / "tc"
        self.bin, self.tw, self.cards, self.trips, self.log = t / "bin", t / "tw", t / "cards", t / "trips", t / "log"
        for d in (self.bin, self.tw, self.cards, self.trips):
            d.mkdir(parents=True)
        script(self.bin / "train-watch", FAKE_TW)
        script(self.bin / "flight-watch", FAKE_FW)
        script(self.bin / "trip-conditions", FAKE_TC)
        script(self.bin / "remind", '#!/usr/bin/env bash\nprintf "remind %s\\n" "$*" >> "$FAKE_LOG"\n')
        (t / "broken_cardimg.py").write_text('raise RuntimeError("no pillow today")\n')
        (t / "legs.json").write_text(BOARD_LEGS)
        (t / "legs-empty.json").write_text("[]\n")
        for k, v in BOARD_STATUS.items():
            (self.tw / f"{k}.json").write_text(v + "\n")
        (self.trips / "conditions.json").write_text(TC_CONDITIONS)
        (self.trips / "pretrip.json").write_text(
            '{"name": "Island Trip", "from": "2031-03-14", "until": "2031-03-22", '
            '"legs": ["flight out Fri 14.03 21:35", "back Sat 22.03"]}\n'
        )
        (self.trips / "extra.json").write_text(TC_EXTRA)
        (self.trips / "TODO.md").write_text(
            "# TODO\n\n## Waiting on you\n- **Island Trip stays** — pick one (internal note)\n\n## Ideas\n"
        )
        (t / "flights.json").write_text(FLIGHTS)
        (t / "cond.json").write_text(COND)
        self.base = dict(
            TG_CMD_TRIP_CONDITIONS=self.trips / "conditions.json", TG_CMD_PRETRIP=self.trips / "pretrip.json",
            TG_CMD_TRIPS=self.trips / "extra.json", TG_CMD_TODO=self.trips / "TODO.md", FAKE_LOG=self.log,
            TG_CMD_BIN=self.bin, FAKE_FW_JSON=t / "flights.json", FAKE_TC_JSON=t / "cond.json", FAKE_TW_LEGS=t / "legs.json",
            FAKE_TW_DIR=self.tw,
        )  # fmt: skip

    def run(self, *args, now=NOW, pic=False, **env):
        self.log.write_text("")
        e = dict(self.base, CARDIMG_DIR=self.cards, TG_CMD_NOW=now, **env)
        if pic:
            e["TG_CMD_PICTURES"] = "1"
        return self.box.run(*args, env=e)

    def pic(self, *args, **env):
        return self.run(*args, pic=True, **env)

    def py(self, code: str, now=NOW) -> str:
        return self.box.py3(PY_PRELUDE + code, env=dict(self.base, TGCMD=self.box.tgcmd, TG_CMD_NOW=now))

    def size(self, p) -> str:
        return self.box.py3(
            "from PIL import Image; import os, sys; i = Image.open(sys.argv[1]); "
            "print(i.width, i.height <= 1350, oct(os.stat(sys.argv[1]).st_mode & 0o777))",
            str(p),
        )


@pytest.fixture
def tc(tmp_path):
    return Trips(tmp_path)


BOARD_HEAD = "🚆 Trains · next 6 of 7 · home = door to door"


def test_train_board(tc):
    text = tc.run("train")
    assert (text.rc, text.out.splitlines()[0]) == (0, "🚆 Trains · next 4 of 7 · home = door to door")  # 4 without flag
    r = tc.pic("train")
    assert (r.rc, r.err) == (0, "")
    d = r.j()
    assert d["photo"] == str(tc.cards / "train.png")
    assert tc.size(tc.cards / "train.png") == "1080 True 0o600"
    assert d["caption"].splitlines()[0] == BOARD_HEAD == d["text"].splitlines()[0]  # caption = text = the 6 legs
    assert "❌ cancelled → alt 01:55 SN3 → 03:05" in d["caption"]  # the caption keeps the verdicts
    assert len(d["caption"]) <= 1024
    assert sorted(tc.log.read_text().splitlines()) == ["train-watch list --json"] + [
        f"train-watch status b{i} --json" for i in range(1, 7)
    ]  # one list + 6 status calls, no other source
    rows = tc.py("""
L = m.legs_ahead(); S = [m.leg_status(g) for g in L[:6]]
for g, s in zip(L, S):
    r = m.board_row(g, s, ci)
    col = {ci.GOOD: "good", ci.WARN: "warn", ci.BAD: "bad", ci.ACCENT: "accent", ci.TEXT2: "grey"}[r["color"]]
    print("|".join([r["time"], r["day"], r["dest"], r["origin"], "+".join(r["lines"]), r["arr"], r["platform"] + ("!" if r.get("platform_new") else ""), r["status"], col, r["note"]]))
""")  # noqa: E501
    assert rows == (
        "13:10|today|Basel SBB|Home|IR36|14:05|7|On time|good|\n"
        "14:00|today|Bern|Home|IC1|15:00|4|+3 min|warn|\n"
        "16:00|today|Lausanne|Home|IC5+IR15|18:36|3|+6 min|warn|\n"
        "18:00|today|Olten|Home|Bus 21+IR16+IC3+Tram 9|19:10|9!|Tight change|bad|Change at Olten Nord at risk · replacement bus from Aarau\n"  # noqa: E501
        "01:40|tomorrow|Home|Basel SBB|SN3|02:50||Cancelled|bad|Alt 01:55 SN3 → 03:05\n"
        "09:00|Sat 08.03|Chur|Home|IC3|11:30||No status|grey|"
    )
    far = tc.py(
        'g = m.legs_ahead()[0]; r = m.board_row(g, m.leg_status(g), ci); print(r["dest"], r["status"], repr(r["platform"]), r["color"] == ci.ACCENT)',  # noqa: E501
        now="2031-03-19 08:00",
    )
    assert far == "Thun As planned '' True"  # a far leg: as planned, no platform
    gone = tc.py(
        'g = m.legs_ahead()[0]; r = m.board_row(g, {"fine": False, "problems": [{"key": "cancelled"}]}, ci); print(r["status"], "|", r["note"])'  # noqa: E501
    )
    assert gone == "Cancelled | No alternative found, check SBB"
    r = tc.pic("train", FAKE_TW_DIR=tc.t / "none")
    assert (r.rc, r.j()["photo"]) == (0, str(tc.cards / "train.png"))  # every status fails: still a card
    assert r.j()["caption"].count("⚪ no live status right now") == 6
    r = tc.pic("train", FAKE_TW_ERR="legs file unreadable")
    assert (r.rc, r.out.splitlines()[0]) == (3, "⚠️ /train failed: train watch: legs file unreadable.")
    assert tc.pic("train", FAKE_TW_LEGS=tc.t / "legs-empty.json").out == "🚆 No train legs planned ahead."
    r = tc.pic("train", TG_CMD_CARDIMG=tc.t / "broken_cardimg.py")
    assert (r.rc, r.out.splitlines()[0]) == (0, BOARD_HEAD)  # card broken: the text card
    assert "/train picture failed (RuntimeError" in r.err


ISLAND = "🌴 Island Trip · Fri 14–22.3 (8 nights) · in 13 days"
FEST = "💍 Family Fest · Fri 7–8.3 · in 6 days"


def test_trip_card(tc):
    now = "2031-03-01 10:00"
    text = tc.run("trip", "island", now=now)
    assert (text.rc, text.out.splitlines()[0]) == (0, ISLAND)
    r = tc.pic("trip", "island", now=now)
    assert (r.rc, r.err) == (0, "")
    d = r.j()
    assert d["photo"] == str(tc.cards / "trip.png")
    assert tc.size(tc.cards / "trip.png") == "1080 True 0o600"
    assert (d["caption"] == d["text"], d["text"].splitlines()[0]) == (True, ISLAND)
    log = tc.log.read_text()
    assert "trip-conditions island --json --days 1 --no-advisory" in log  # today, without the advisory lookups
    assert "flight-watch list --json" in log
    pre = 't = m.trip_select("island")[1]\n'
    # out (2 legs) and back (2 legs); bad and far flights dropped
    assert (
        tc.py(pre + 'print([[f["no"] for f in j] for j in m.trip_flights(t)])', now)
        == "[['XY100', 'QZ19'], ['QZ18', 'XY101']]"
    )
    assert tc.py(pre + 'print(m.journey_route(m.trip_flights(t)[0], "Out"))', now) == (
        "(('Out', 'Fri 14.03'), [('AAA', 'Fri 21:35'), ('BBB', '06:45–07:50'), ('CCC', 'Sat 12:25')], ['XY 100', 'QZ 19'])"
    )  # stops in each airport's local time + flight numbers

    def cd(n):
        return tc.py(pre + 'print(" | ".join(m.trip_countdown(t, m.trip_flights(t), "North Isle")))', n)

    assert cd("2031-03-01 10:00") == "13 | days to go · XY 100 Fri 14.03 21:35"
    assert cd("2031-03-14 10:00") == "12 h | to go · XY 100 Fri 14.03 21:35"  # hours on the last day before
    assert cd("2031-03-13 22:00") == "24 h | to go · XY 100 Fri 14.03 21:35"  # one day before = hours too
    assert cd("2031-03-16 09:00") == "Day 3 | of 9 · North Isle"
    assert cd("2031-03-22 09:00") == "Last day | of 9 days"
    assert tc.py(pre + 'print(" | ".join(m.trip_countdown(t, [])))', now) == "13 | days to go · starts Fri 14.03"
    tiles = tc.py(
        pre
        + """k, conf = m.trip_conf(t)
from datetime import date
legs = [{**l, "from": date.fromisoformat(l["from"]), "to": date.fromisoformat(l["to"])} for l in conf["legs"]]
for it in m.trip_tiles(legs, m.trip_conditions(k), ci):
    print(it["title"], it.get("star", False), it["sub"], it["pill"] and it["pill"][0], [x if isinstance(x, str) else x[0] for x in it["lines"]])
""",  # noqa: E501
        now,
    )
    assert tiles == (
        "North Isle True ['15–18.3 · 3 nights', 'Bay One'] Long-range: go ['Vis good', '8 kn · waves 0.8 m', 'Sea 29°']\n"
        "South Isle False ['18–20.3 · 2 nights', 'Bay Two'] Long-range: maybe ['Vis fair', '14 kn · waves 1.5 m', 'Sea 28°']\n"
        "East Isle False ['20–21.3 · 1 night'] None ['Reef C']"
    )  # one per place, today's dive estimate, the clearly best one starred
    for n in ("2031-03-14 10:00", "2031-03-16 09:00", "2031-03-22 09:00"):
        r = tc.pic("trip", "island", now=n)
        assert (r.rc, r.j()["photo"], r.err) == (0, str(tc.cards / "trip.png"), ""), n
    r = tc.pic("trip", "island", now=now, FAKE_TC_ERR="1", FAKE_FW_ERR="1")
    assert (r.rc, r.j()["photo"], r.err) == (0, str(tc.cards / "trip.png"), "")  # conditions + flights down: a card
    start = time.monotonic()
    r = tc.pic("trip", "island", now=now, FAKE_TC_SLOW="1")
    took = time.monotonic() - start
    assert (r.rc, r.j()["photo"], took < 12) == (0, str(tc.cards / "trip.png"), True)  # slow conditions: killed in time
    r = tc.pic("trip", "fest", now=now)
    assert (r.rc, r.j()["photo"], tc.log.read_text().count("trip-conditions")) == (0, str(tc.cards / "trip.png"), 0)
    fest_caption = r.j()["caption"].splitlines()[0]
    assert tc.pic("trip", now=now).j()["caption"].splitlines()[0] == FEST  # no trip named: the soonest
    assert fest_caption == FEST  # a card without places: caption = its text card
    assert tc.pic("trip", "list", now=now).out.splitlines()[0] == "🧳 Trips ahead (2)"  # text only
    assert tc.pic("trip", "moon", now=now).out.splitlines()[0] == "🧳 No trip ahead called “moon”."
    assert tc.pic("trip", now="2031-06-01 10:00").out == "🧳 No trip planned."
    r = tc.pic("trip", "island", now=now, TG_CMD_CARDIMG=tc.t / "broken_cardimg.py")
    assert (r.rc, r.out.splitlines()[0]) == (0, ISLAND)  # card broken: the text card


def test_cardimg_trip_pieces(tc):
    out = tc.py("""
c = ci.Card("t"); f = ci.font(30)
w = c.rich(56, 300, "A → B ★4.5", f); print(round(w) == round(c.rich_w("A → B ★4.5", f)), c.rich(56, 400, "Alpha → Beta → Gamma → Delta", f, maxw=200) <= 200)
print([p for p in c._rich_parts("A → B ★4.5")])
""")  # noqa: E501
    assert out == "True True\n['A', '→', 'B ', '★', '4.5']"  # rich() draws → and ★ (no tofu), width = rich_w
    out = tc.py("""
c = ci.Card("t"); r = {"time": "10:00", "day": "today", "dest": "X", "lines": ["S1"], "status": "On time", "color": ci.GOOD, "note": "n"}
print(c.board([r] * 12), c.y <= c.h - ci.PAD)
""")  # noqa: E501
    assert out == "5 True"  # the board stops at the bottom, returns rows drawn
    out = tc.py("""
from datetime import date
c = ci.Card("t"); seen = []
orig = c.d.text
c.d.text = lambda xy, s, **k: (seen.append(s), orig(xy, s, **k))
c.trip_strip([{"name": "A", "from": date(2031, 3, 2), "to": date(2031, 3, 9)}, {"name": "B", "from": date(2031, 3, 9), "to": date(2031, 3, 16)}], date(2031, 3, 1), date(2031, 3, 17), today=date(2031, 3, 5))
print([s for s in seen if "." in s or s == "today"])
""")  # noqa: E501
    assert out == "['2.3', '9.3', '16.3', '1.3', 'today']"  # place cuts first, overlapping trip ends dropped
    out = tc.py("""
c = ci.Card("t"); y0 = c.y
c.route(("Out", "Fri"), [("AAA", "21:35"), ("BBB", "06:45–07:50"), ("CCC", "12:25")], ["XY 1", "QZ 2"])
c.tiles([{"title": "One", "star": True, "sub": ["a"], "pill": ("Long-range: go", (0, 0, 0), ci.GOOD), "lines": ["b", ("c", ci.BAD)]}, {"title": "Two"}])
print(c.y > y0 + 300, os.path.basename(c.save("t", os.environ["HOME"] + "/../tc/cards")))
""")  # noqa: E501
    assert out == "True t.png"  # route + tiles draw and advance y
    assert [p for p in tc.box.home.rglob("*") if p.is_file()] == []  # nothing written under HOME


# --------------------------------------------------------------------------- /week picture


def test_week_picture(tmp_path):
    w = Week(Box(tmp_path, profile=True), tmp_path / "wk")
    text = w().out
    r = w(pictures=True)
    assert r.rc == 0
    d = r.j()
    photo = d["photo"]
    assert photo == str(w.cards / "week.png")
    size = w.box.py3(
        "import sys; from PIL import Image; i = Image.open(sys.argv[1]); print(i.width, 700 <= i.height <= 1350)", photo
    )
    assert (size, oct(os.stat(photo).st_mode & 0o777)) == ("1080 True", "0o600")
    assert d["caption"] == text  # it fits
    assert d["text"] == text
    evs = json.loads((w.fx / "cal.json").read_text())
    for day in range(5, 12):
        for h in range(8, 22):
            evs.append({
                "id": f"b{day}{h}", "title": f"Busy slot {day}-{h} with a really long and winding title to cut",
                "start": f"2031-03-{day:02d}T{h:02d}:15:00+01:00", "end": f"2031-03-{day:02d}T{h:02d}:45:00+01:00",
                "all_day": False, "flags": {},
            })  # fmt: skip
    (w.fx / "busy.json").write_text(json.dumps(evs))
    r = w(pictures=True, FAKE_CAL=w.fx / "busy.json")
    assert (r.rc, len(r.j()["caption"]) <= 1024) == (0, True)
    size = w.box.py3(
        "import sys; from PIL import Image; i = Image.open(sys.argv[1]); print(i.width, i.height <= 1350)", photo
    )
    assert size == "1080 True"  # a busy week's card is still 1080x1350 at most
    assert "Busy slot 5-21" in r.j()["text"]  # the text keeps every event (cut at 3500)
    (w.t / "broken_cardimg.py").write_text('raise ImportError("no pillow here")\n')
    r = w(pictures=True, TG_CMD_CARDIMG=w.t / "broken_cardimg.py")
    assert (r.rc, r.out) == (0, text)  # render fails: the text card
    assert "/week picture failed" in r.err
    r = w(pictures=True, FAIL_CAL="1")
    assert r.rc == 3
    assert "⚠️ /week failed: calendar: calcache failed." in r.out
