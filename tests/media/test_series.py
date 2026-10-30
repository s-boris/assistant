"""`media vibe` on series (second index, series words, like <series>) and `media finish` (shows he started and
paused). fake_vibe.py (scenario "series") stands in for Emby: a fictional series library, his series user data and
episodes; fake_tonight.py for Seerr; MEDIA_VIBE_EMBED_URL points at the fake (keyword vectors). Checks: series
detection (words, --series, tv show, like <series>, moods stay movies), TMDB TV / German / Serbian genres, seen states,
rating floor, no play buttons for series, both indexes on --refresh; finish: idle filter, specials and finished shows
left out, ranking, resume point, next = after the furthest episode (skipped ones named, gaps-only shows last),
-n/--days, play buttons, a show failing (exit 3), GET-only, only his user, no key leaks."""

from __future__ import annotations

import json
import re

import pytest
from mediatest import KEYS

EMBY_KEY = "EMBY-KEY-Vb42"


@pytest.fixture
def sv(media):
    t = media.t
    b = media.serve("fake_tonight.py")
    (t / "scenario").write_text("series\n")
    e = media.serve("fake_vibe.py", t / "emb.log", t / "scenario", log=t / "emby.log", err=t / "emby.err")
    (t / "cfg.json").write_text(
        json.dumps({"seerr": {"url": f"{b}/seerr", "key": KEYS[0]}, "emby": {"url": e, "key": EMBY_KEY}})
    )
    (t / "noemby.json").write_text(json.dumps({"seerr": {"url": f"{b}/seerr", "key": KEYS[0]}}))
    (t / "play-on.json").write_text('{"enabled": true, "tv": "media_player.test_tv", "device": "TESTTV"}\n')
    media.env.update(
        MEDIA_SECRETS=str(t / "cfg.json"),
        MEDIA_TIMEOUT="5",
        MEDIA_PAGE_SIZE="4",
        MEDIA_VIBE_DIR=str(t / "vibe"),
        MEDIA_VIBE_EMBED_URL=e,
    )
    yield media
    elog = media.log(t / "emby.log").splitlines()
    assert [x for x in elog if not x.startswith("GET ")] == []
    assert [x for x in elog if "/Users/" in x and "/Users/aaaa1111" not in x] == []  # only his own user's data
    media.safety(keys=(*KEYS, EMBY_KEY))
    assert media.log(t / "emby.err") == ""


def test_series_vibe_and_finish(sv):
    m, t = sv, sv.t
    emb = t / "emb.log"

    def mj(*a, **env):
        return m.m(*a, env=env, merge=False)

    def titles():
        return " | ".join(p["title"] for p in m.j["picks"])

    def docs():
        return sum(x.startswith("doc ") for x in m.log(emb).splitlines())

    def pick(title, key):
        return [p[key] for p in m.j["picks"] if p["title"] == title][0]

    # movies first: a movie query builds no series index
    mj("vibe", "--json", "cozy", "rainy")
    assert m.rc == 0 and m.j["kind"] == "movie"
    assert not (t / "vibe" / "series-index.json").exists()

    # a series word switches to series
    emb.write_text("")
    mj("vibe", "--json", "a", "dark", "crime", "series")
    d = m.j
    assert m.rc == 0 and d["kind"] == "series"
    assert docs() == 8
    assert [x for x in m.log(emb).splitlines() if x.startswith("query ")] == ["query a dark crime TV series"]
    assert titles() == "Night Precinct | Cold Case Lake | Star Frontier | Office Laughs"
    assert "Weak Show" not in titles()  # rating floor (5.0)
    assert "Harbor Detective" not in titles()  # the finished one ranks below
    cc = [p for p in d["picks"] if p["title"] == "Cold Case Lake"][0]
    assert (cc["seen"], cc["why"].split(" · ")[-1], cc["episodes"], cc["watched"]) == (
        "barely",
        "👁 1 of 10 eps watched",
        10,
        1,
    )
    assert pick("Office Laughs", "seen") == "partly"
    assert pick("Star Frontier", "genres") == ["Fantasy", "Science Fiction", "Action", "Adventure"]
    assert "📺 **Vibe** · series · a dark crime" in d["message"]
    assert "**Night Precinct** (2019) · 6 eps · ★8.1" in d["message"]
    assert "▶ https://emby.example.test/web/index.html#!/item?id=201&" in d["text"]
    idx = json.loads((t / "vibe" / "series-index.json").read_text())
    assert [x["genres"] for x in idx["items"] if x["title"] == "Weak Show"][0] == ["War", "Politics"]
    assert all(x.get("kind") == "series" for x in idx["items"])
    names = sorted(
        re.sub(r"-[0-9a-f]*\.f32$", "", p.name) for p in (t / "vibe").iterdir() if not p.name.startswith(".")
    )
    assert names == ["index.json", "series-index.json", "series-vectors", "vectors"]
    mj("vibe", "--json", "detective", "series", MEDIA_PLAY_CONFIG=str(t / "play-on.json"))
    assert sum(1 for r in m.j["buttons"] for b in r if "data" in b) == 0  # a series is no play target

    # other ways in
    emb.write_text("")
    mj("vibe", "--json", "--series", "detective", "murder")
    assert m.j["kind"] == "series"
    assert m.log(emb).rstrip("\n") == "query detective murder TV series"
    assert m.j["picks"][0]["title"] == "Night Precinct"
    assert pick("Murder Valley", "seen") == "partly"
    assert pick("Murder Valley", "genres") == ["Crime", "Drama"]  # Serbian genres mapped
    emb.write_text("")
    mj("vibe", "--json", "funny", "tv", "show")
    assert f"{m.j['kind']} {m.log(emb).rstrip()}" == "series query funny TV series"
    mj("vibe", "--json", "eine", "lustige", "Serie")
    assert m.j["kind"] == "series"
    mj("vibe", "--json", "like", "Night Precinct")
    assert m.j["kind"] + " " + m.j["ref"]["title"] == "series Night Precinct"
    assert "Night Precinct" not in titles()
    mj("vibe", "--json", "Cold", "Case", "Lake")
    assert m.j["kind"] + " " + m.j["ref"]["title"] == "series Cold Case Lake"
    mj("vibe", "--json", "like", "First", "Signal")
    assert m.j["kind"] == "movie"
    mj("vibe", "--json", "a", "show", "like", "First", "Signal")
    assert m.j["kind"] == "series"  # a series word before like <movie>: words count
    mj("vibe", "--json", "heist", "with", "a", "laugh")
    assert m.j["kind"] == "movie"
    assert "📺 Vibe · series · a dark crime" in m.m("vibe", "a", "dark", "crime", "series")

    # refresh: both indexes
    out = m.m("vibe", "--refresh")
    assert m.rc == 0
    assert "vibe index: 15 movies (0 new, 0 changed, 0 removed; 0 embedded" in out
    assert "vibe index: 8 series (0 new, 0 changed, 0 removed; 0 embedded" in out
    emb.write_text("")
    assert m.m("vibe", "--refresh", "--full", "--quiet") == ""
    assert docs() == 23

    # finish
    out = m.m("finish")
    assert m.rc == 0
    ls = out.split("\n")
    assert "\n".join(ls[i - 1] for i in (1, 2, 3, 4, 6, 7)) == (
        "🏁 Finish next\nStarted, paused 14+ days, episodes left\n\n📺 Star Frontier · 3 of 8 left · ★8.4\n"
        "   ▶ https://emby.example.test/web/index.html#!/item?id=2050106&context=home&serverId=srv1\n"
        "📺 Murder Valley · 6 of 10 left · ★6.9"
    )
    assert "   next S01E06 · last watched " in out
    assert "   next S01E05 at 20 min · last watched " in out  # in-progress episode resumes
    assert "📺 Cold Case Lake · 9 of 10 left · ★7.6" in out
    for absent in ("Office Laughs", "Specials Left", "Harbor Detective", "Night Precinct"):
        assert absent not in out
    mj("finish", "--json")
    d = m.j
    assert " | ".join(f"{i['title']}:{i['left']}/{i['total']}:{i['id']}:{i['state']}" for i in d["items"]) == (
        "Star Frontier:3/8:2050106:next | Murder Valley:6/10:2060105:resume | Cold Case Lake:9/10:2020102:next"
    )
    assert (d["buttons"], "switched off" in d["message"]) == ([], True)
    assert "📺 **Star Frontier** · 3 of 8 left · ★8.4" in d["message"]
    assert (d["total"], d["started"], d["idle_days"]) == (3, 5, 14)
    mj("finish", "--json", "-n", "2", MEDIA_PLAY_CONFIG=str(t / "play-on.json"))
    assert [b["text"] + "=" + b["data"] for r in m.j["buttons"] for b in r] == [
        "📺 Star Frontier S01E06=mt:play:2050106",
        "📺 Murder Valley S01E05=mt:play:2060105",
    ]
    mj("finish", "--json", "--days", "3")
    assert (
        " | ".join(i["title"] for i in m.j["items"]) == "Star Frontier | Office Laughs | Murder Valley | Cold Case Lake"
    )
    mj("finish", "--json", "--days", "1000")
    assert m.j["text"].splitlines()[-1] == "Nothing to finish: no paused shows with episodes left."

    (t / "scenario").write_text("gaps\n")
    out = m.m("finish")
    assert m.rc == 0
    assert "📺 Weak Show · 3 of 6 left · ★5.0\n   next S01E05 · last watched " in out  # after the furthest episode
    assert " · skipped S01E03" in out
    assert "📺 Night Precinct · 2 of 10 left · ★8.1\n   watched to S03E02 · skipped S02E01–E02 · last watched " in out
    assert "next S02E01" not in out
    mj("finish", "--json")
    items = m.j["items"]
    assert " | ".join(f"{i['title']}:{i['id']}:{i['state']}:{i['gaps_only']}:{i['gaps']}" for i in items) == (
        "Star Frontier:2050106:next:False:None | Murder Valley:2060105:resume:False:None | "
        "Cold Case Lake:2020102:next:False:None | Weak Show:2070105:next:False:S01E03 | "
        "Night Precinct:2010201:next:True:S02E01–E02"
    )
    assert " ".join(i["furthest"] for i in items) == "S01E05 S01E05 S01E01 S01E04 S03E02"
    (t / "scenario").write_text("ep-down\n")
    mj("finish", "--json")
    assert m.rc == 3
    assert "1 show(s) didn't load: Murder Valley" in " ".join(m.j["warnings"])
    assert len(m.j["items"]) == 2
    (t / "scenario").write_text("series\n")
    assert "-n must be 1..10" in m.m("finish", "-n", "11")
    m.m("finish", env={"MEDIA_SECRETS": t / "noemby.json"})
    assert m.rc == 1
