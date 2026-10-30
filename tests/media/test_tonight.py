"""`media tonight` (movie night picks): fake_tonight.py stands in for Seerr; tonight_fx.py builds Emby's library.db +
playback_reporting.db and plays the SSH lockdown verb `emby-sql library|playback` (enforcing its rules) or the root
sqlite3 command, logging the SQL. Clock Sat 2026-10-10 20:00 Zurich. Checks: the three picks and their reasons, taste,
mood/time, args parsing, repeats and --more, Emby user mapping + cache, links, buttons, Markdown escaping, partial
failures, dry runs, GET-only, read-only SQL that passes the verb's checks, no key leaks."""

from __future__ import annotations

import json
import sqlite3
import subprocess

import pytest
from mediatest import PYTHON3, dead_port, fx, mkcfg

RATING = r"""
import importlib.machinery, importlib.util, sys, types
ld = importlib.machinery.SourceFileLoader("media_under_test", sys.argv[1])
m = importlib.util.module_from_spec(importlib.util.spec_from_loader(ld.name, ld))
ld.exec_module(m)
t = m.Tonight(types.SimpleNamespace(), {}, [])
tmdb = {"1": (10.0, 2), "2": (8.833, 108), "3": (8.7, 31613), "5": (7.0, 0)}
t.seerr_item = lambda kind, i: ({"voteAverage": tmdb[str(i)][0], "voteCount": tmdb[str(i)][1]} if str(i) in tmdb else {})
def it(r, i=None):
    return {"rating": r, "tmdb": i, "kind": "tv"}
def show(x):
    return (round(t.rating(x), 2) if t.rating(x) is not None else None, x.get("shown"), x.get("votes"))
a, b, c, d, e, f = it(10.0, "1"), it(10.0, "2"), it(9.3, "3"), it(10.0), it(7.4, "4"), it(None, "5")
print("provisional", [t.rating(x) for x in (a, b, c, d, e, f)])
for x in (a, b, c, d, e, f):
    t.verify(x)
print("verified", [show(x) for x in (a, b, c, d, e, f)])
print("again", t.verify(a))
print("mood", m.tn_mood(["Good", "scifi"])[::4], m.tn_mood(["gute", "Komödie"])[1], m.tn_mood(["scifi"])[4])
print("line", m.tn_line({"slot": "A", "title": "X", "rating": 10.0, "votes": 2, "in_library": True}, False),
      "|", m.tn_line({"slot": "A", "title": "Y", "rating": 8.0, "votes": 800, "in_library": True}, False))
"""  # noqa: E501

MESSAGE = """🎬 **Movie night**

🍿 **Req\\_Film \\[Cut\\]** (2025) · 95 min · ★6.5
you requested it, on Emby since 07.10 · Science Fiction, Drama

📺 **Star Voyage** S01E04 “The \\*Fourth\\*” · 45 min · ★8.2
next up: you watched S01E03 on 07.10 · 2 more after this

✨ **Nova Drift** (2025) · 118 min · ★8.4 · not on Emby yet
popular Science Fiction from 2025 · because you finished Arrival"""

TEXT = """🎬 Movie night

🍿 Req_Film [Cut] (2025) · 95 min · ★6.5
you requested it, on Emby since 07.10 · Science Fiction, Drama

📺 Star Voyage S01E04 “The *Fourth*” · 45 min · ★8.2
next up: you watched S01E03 on 07.10 · 2 more after this

✨ Nova Drift (2025) · 118 min · ★8.4 · not on Emby yet
popular Science Fiction from 2025 · because you finished Arrival
   A: https://emby.example.test/web/index.html#!/item?id=106&context=home&serverId=srv1
   B: https://emby.example.test/web/index.html#!/item?id=2014&context=home&serverId=srv1
   C: button mt:req:7001 = media request 7001 --type movie"""
FIRST = "A Req_Film [Cut] | B Star Voyage S01E04 | C Nova Drift"


class Tn:
    def __init__(self, w):
        self.w, t = w, w.t
        self.t = t
        w.py("tonight_fx.py", "build", t / "emby")
        b = w.serve("fake_tonight.py")
        mkcfg(t / "cfg.json", f"{b}/seerr", f"{b}/radarr", f"{b}/sonarr")
        mkcfg(t / "seerr-down.json", f"http://127.0.0.1:{dead_port()}", f"{b}/radarr", f"{b}/sonarr")
        self.sql_log = t / "sql.log"
        self.root_sql = fx("tonight_fx.py", "sql", t / "emby", self.sql_log)
        w.env.update(
            MEDIA_SECRETS=str(t / "cfg.json"),
            MEDIA_NOW="2026-10-10T20:00:00+02:00",
            MEDIA_PAGE_SIZE="2",
            MEDIA_TIMEOUT="5",
            MEDIA_EMBY_DIR=str(t / "emby"),
            MEDIA_EMBY_SQL=self.root_sql + " {db}",
            MEDIA_TONIGHT_STATE=str(t / "st" / "tonight.json"),
        )

    def picks(self):
        return " | ".join(
            p["slot"] + " " + p["title"] + (" " + p["episode"] if p.get("episode") else "") for p in self.w.j["picks"]
        )

    def slot(self, s, key):
        return [p[key] for p in self.w.j["picks"] if p["slot"] == s][0]

    def why(self, s):
        return self.slot(s, "why")

    def apick(self):
        return self.slot("A", "title")

    def fresh(self):
        (self.t / "st" / "tonight.json").unlink(missing_ok=True)

    def sql(self):
        return self.sql_log.read_text() if self.sql_log.exists() else ""

    def sqlcount(self, pat):
        return sum(pat in x for x in self.sql().splitlines())

    def bep(self):
        """B's play id in the fixture DB = an episode of the pick's show with the episode number the card shows."""
        p = [p for p in self.w.j["picks"] if p["slot"] == "B"][0]
        db = sqlite3.connect(f"file:{self.t}/emby/library.db?mode=ro", uri=True)
        r = db.execute(
            "SELECT type, SeriesId, ParentIndexNumber, IndexNumber FROM MediaItems WHERE Id = ?", (int(p["emby_id"]),)
        ).fetchone()
        db.close()
        return (
            "ok" if r and r[0] == 8 and str(r[1]) == p["series_id"] and f"S{r[2]:02d}E{r[3]:02d}" == p["episode"] else r
        )


@pytest.fixture
def tn(media):
    w = Tn(media)
    yield w
    # every run: the emby-sql verb accepted each query (no rejections), read-only SQL, GET only, no key, no traceback
    assert "claude-bot-cmd" not in media.everything()
    media.sql_readonly(w.sql_log)
    media.safety(keys=("SEERR-KEY-Zq81",))


def test_default_picks(tn, media):
    m = media
    m.m("tonight", "--json", "--dry-run")
    assert m.rc == 0
    assert tn.picks() == FIRST
    assert tn.why("A") == "you requested it, on Emby since 07.10 · Science Fiction, Drama"
    assert tn.why("B") == "next up: you watched S01E03 on 07.10 · 2 more after this"  # specials, missing files skipped
    assert tn.why("C") == "popular Science Fiction from 2025 · because you finished Arrival"
    assert m.j["taste"] == {"top_genres": ["Science Fiction", "Drama", "Comedy"], "movies_seen": 12, "shows_seen": 3}
    a = [p for p in m.j["picks"] if p["slot"] == "A"][0]
    assert (a["year"], a["runtime_min"], a["rating"], a["tmdb"], a["in_library"]) == (2025, 95, 6.5, 5550, True)
    b = [p for p in m.j["picks"] if p["slot"] == "B"][0]
    assert (b["kind"], b["episode_title"], b["runtime_min"], b["rating"], b["emby_id"]) == (
        "episode",
        "The *Fourth*",
        45,
        8.2,
        "2014",
    )
    assert tn.bep() == "ok" and (b["series_id"], b["link"].split("id=")[1].split("&")[0]) == ("201", "2014")
    c = [p for p in m.j["picks"] if p["slot"] == "C"][0]
    assert (c["year"], c["runtime_min"], c["rating"], c["in_library"], c["request"]) == (
        2025,
        118,
        8.4,
        False,
        {"data": "mt:req:7001", "cmd": "media request 7001 --type movie"},
    )
    assert [p["link"] for p in m.j["picks"]] == [
        "https://emby.example.test/web/index.html#!/item?id=106&context=home&serverId=srv1",
        "https://emby.example.test/web/index.html#!/item?id=2014&context=home&serverId=srv1",
        None,
    ]
    assert [[(x["text"], x.get("data") or x["url"].split("?")[1]) for x in r] for r in m.j["buttons"]] == [
        [("▶ Req_Film [Cut]", "id=106&context=home&serverId=srv1")],
        [("▶ Star Voyage S01E04", "id=2014&context=home&serverId=srv1")],
        [("➕ Request Nova Drift", "mt:req:7001")],
        [("🎲 3 more", "mt:more")],
    ]
    assert m.j["message"] == MESSAGE
    assert m.j["warnings"] == []
    assert not (tn.t / "st").exists() or not any((tn.t / "st").iterdir())  # dry run writes no state
    assert tn.sqlcount("FROM PlaybackActivity") == 1  # user mapped by play overlap, read once
    assert "= 'aaaa1111000000000000000000000000'" in tn.sql()  # his Emby id normalized (dashes, case)
    assert sum(("/api/v1/movie/5550" in x or "/api/v1/tv/" in x) for x in m.log().splitlines()) == 1
    assert (
        "/seerr/api/v1/discover/movies?page=1&genre=878&sortBy=popularity.desc&voteAverageGte=6.8&voteCountGte=150"
        "&primaryReleaseDateGte=2022-10-11&primaryReleaseDateLte=2026-10-10" in m.log()
    )
    assert "withRuntimeLte" not in m.log()

    assert m.m("tonight", "--dry-run") == TEXT and m.rc == 0


def test_remembered_picks(tn, media):
    m, t = media, media.t
    tn.fresh()
    m.m("tonight", "--json", "--args", "scifi")
    assert m.rc == 0 and tn.picks() == FIRST
    d = json.loads((t / "st" / "tonight.json").read_text())
    assert (
        sorted(d["shown"].items()),
        d["last"]["words"],
        d["last"]["time"],
        d["last"]["picks"],
        d["emby"]["uid"],
        d["emby"]["at"],
    ) == (
        [("e:2014", "2026-10-10"), ("m:106", "2026-10-10"), ("t:7001", "2026-10-10")],
        ["scifi"],
        None,
        ["m:106", "e:2014", "t:7001"],
        7,
        "2026-10-10",
    )
    m.m("tonight", "--json", "--more")
    assert tn.picks() == "A Dune: Part Two | B Fresh Hit S01E01 | C Tight Thriller"
    assert m.j["mood"] == "scifi"
    assert tn.why("A") == "because you finished Arrival · Science Fiction"
    assert tn.why("B") == "new to you · because you watched Star Voyage · Drama, Thriller · 2 episodes on Emby"
    assert tn.slot("B", "link").split("?")[1] == "id=203&context=home&serverId=srv1"
    b = [p for p in m.j["picks"] if p["slot"] == "B"][0]
    assert (b["kind"], b["episode"], b["emby_id"], b["series_id"]) == ("series", "S01E01", "2031", "203")
    assert tn.bep() == "ok"
    assert [x["text"] for r in m.j["buttons"] for x in r if "id=203&" in x.get("url", "")] == ["▶ Fresh Hit"]
    (t / "play.json").write_text('{"enabled": true, "tv": "media_player.test_tv", "device": "Test TV"}\n')
    m.m("tonight", "--json", "--more", "--dry-run", env={"MEDIA_PLAY_CONFIG": t / "play.json"})
    rows = [[(x["text"], x.get("data") or x["url"].split("?")[1].split("&")[0]) for x in r] for r in m.j["buttons"][:2]]
    assert rows == [
        [("▶ Other User Saw", "id=114"), ("📺 Play on TV", "mt:play:114")],
        [("▶ Quick Laughs", "id=206"), ("📺 Play on TV", "mt:play:2061")],
    ]
    assert (tn.bep(), tn.slot("B", "episode")) == ("ok", "S01E01")
    assert tn.sqlcount("FROM PlaybackActivity") == 1  # mapping cached
    m.m("tonight", "--json", env={"MEDIA_NOW": "2026-10-12T20:00:00+02:00"})
    assert tn.picks() == "A Other User Saw | B Star Voyage S01E04 | C Trend Wave"  # next episode may come back
    m.m("tonight", "--json", "--dry-run", env={"MEDIA_NOW": "2026-10-25T20:00:00+02:00"})
    assert tn.picks() == FIRST  # 15 days later the first picks are back


def test_mood_and_time(tn, media):
    m = media
    tn.fresh()
    m.m("tonight", "--json", "--dry-run", "--time", "100")
    assert tn.picks() == "A Req_Film [Cut] | B Star Voyage S01E04 | C Tight Thriller"
    assert "&withRuntimeLte=100" in m.log()
    m.m("tonight", "--json", "--dry-run", "--time", "30")
    assert tn.picks() == "A Short Film | B Quick Laughs S01E01 | C Tiny Short"
    assert m.j["message"].splitlines()[0] == "🎬 **Movie night** · ≤ 30 min"
    m.m("tonight", "--json", "--dry-run", "--mood", "funny")
    assert tn.picks() == "A Funny One | B Quick Laughs S01E01 | C Laugh Riot"
    assert tn.why("B") == "new to you · Comedy · 2 episodes on Emby"  # a fitting new show beats one in progress
    m.m("tonight", "--json", "--dry-run", "--mood", "dark")
    assert tn.picks() == "A Krimi Film | B Fresh Hit S01E01 | C Tight Thriller"  # German genre alias Krimi = Crime
    m.m("tonight", "--json", "--dry-run", "--mood", "horror")
    assert tn.apick() == "Scary Night"
    m.m("tonight", "--json", "--dry-run", "--mood", "kids")
    assert tn.apick() == "Kids Toon"
    m.m("tonight", "--json", "--dry-run", "--args", "blah 1h30")
    assert (m.j["time"], m.j["mood"], m.j["message"].splitlines()[-1]) == (
        90,
        None,
        "⚠️ unknown mood 'blah' (try: action, chill, cozy, dark, date, deep, doc, docu, epic, funny, kids, learn,"
        " light, mind, romantic, sad, scary, smart, tense or a genre)",
    )
    for args, wmood, wtime in (
        ("light 90", "light", 90),
        ("2", "None", 120),
        ("45m doc", "doc", 45),
        ("1.5h", "None", 90),
        ("Sci-Fi 2h", "sci-fi", 120),
    ):
        m.m("tonight", "--json", "--dry-run", "--args", args)
        assert (str(m.j["mood"]), m.j["time"]) == (wmood, wtime), args
    m.m("tonight", "--json", "--dry-run", "--args", "light", "--time", "75")
    assert (m.j["mood"], m.j["time"]) == ("light", 75)
    m.m("tonight", "--time", "0")
    assert m.rc == 1


def test_quality_words(tn, media):
    m = media
    tn.fresh()
    m.m("tonight", "--json", "--dry-run", "--args", "western")
    assert tn.apick() == "Solid Western"  # his fresh ★5.8 request doesn't beat a ★8.0
    assert (m.j["quality"], m.j["mood"]) == (False, "western")
    m.log_path.write_text("")
    m.m("tonight", "--json", "--dry-run", "--args", "good western")
    assert (m.j["quality"], m.j["mood"], m.j["message"].splitlines()[0]) == (
        True,
        "good western",
        "🎬 **Movie night** · good western",
    )
    assert tn.apick() == "Solid Western"  # the ★8.9 from 4 votes drops under ★7 once checked
    a = [p for p in m.j["picks"] if p["slot"] == "A"][0]
    assert (a["rating"], a["votes"]) == (8.0, 800)
    assert "GET /seerr/api/v1/movie/121" in m.log()
    assert "/api/v1/movie/120" not in m.log()  # the suspect ★10.0 never led, so never checked
    m.m("tonight", "--json", "--dry-run", "--args", "good scifi")
    assert tn.picks() == "A Dune: Part Two | B Star Voyage S01E04 | C Nova Drift"
    assert "&genre=878&sortBy=popularity.desc&voteAverageGte=7.2&" in m.log()
    m.m("tonight", "--json", "--dry-run", "--args", "scifi")
    assert tn.apick() == "Req_Film [Cut]"
    m.m("tonight", "--json", "--args", "gute western")
    assert (tn.apick(), m.j["quality"]) == ("Solid Western", True)
    m.m("tonight", "--json", "--more", "--dry-run")
    assert tn.apick() == "Gem Western"  # --more keeps quality; nothing else ★7+ fits: the best rated
    assert "★8.9 (4 votes)" in m.j["message"]
    assert "⚠️ no unwatched movie rated ★7+ fits; picked the best rated" in m.j["message"]
    tn.fresh()
    m.m("tonight", "--json", "--dry-run", "--mood", "doc")
    assert [p["title"] for p in m.j["picks"] if p["slot"] == "C"] == ["Deep Docs"]  # discover's next page
    assert "/discover/movies?page=2&genre=99&" in m.log()
    assert "page=3&genre=99" not in m.log()


def test_rating_checks_unit(tn, media):
    r = subprocess.run(
        [PYTHON3, "-", media.tool], input=RATING, capture_output=True, text=True, env=media.env, timeout=60
    )
    got = {x.split(" ", 1)[0]: x for x in (r.stdout + r.stderr).splitlines()}
    assert got["provisional"] == "provisional [None, None, 9.3, None, 7.4, None]"
    # damped by TMDB votes; TMDB's average for a suspect 10.0; no TMDB id = unknown
    assert got["verified"] == (
        "verified [(6.47, 10.0, 2), (8.49, 8.833, 108), (9.3, 9.3, 31613), (None, None, None), (7.4, 7.4, None),"
        " (None, None, 0)]"
    )
    assert got["again"] == "again True"
    assert got["mood"] == "mood ('good scifi', True) {'Comedy'} False"
    assert got["line"] == "line 🍿 X · ★10.0 (2 votes) | 🍿 Y · ★8.0"


def test_emby_modes_and_mapping(tn, media):
    m, t = media, media.t
    tn.sql_log.write_text("")
    m.m("tonight", "--json", "--dry-run", env={"MEDIA_EMBY_SQL": tn.root_sql})
    assert tn.picks() == FIRST  # root sqlite3 command (ATTACH library)
    assert f"ATTACH 'file:{t}/emby/library.db?immutable=1' AS lib;" in tn.sql()
    tn.sql_log.write_text("")
    m.m("tonight", "--json", "--dry-run", env={"MEDIA_TONIGHT_EMBY_UID": 7})
    assert (tn.picks(), tn.sqlcount("FROM PlaybackActivity")) == (FIRST, 0)
    m.m("tonight", "--json", "--dry-run", env={"MEDIA_TONIGHT_EMBY_UID": 3})
    assert tn.picks() == "A Req_Film [Cut] | B Star Voyage S01E02 | C Nova Drift"  # his own progress
    assert tn.why("B") == "next up: you watched S01E01 on 01.10 · 4 more after this"
    m.m("tonight", "--json", "--dry-run", env={"MEDIA_TONIGHT_USER": "Friend"})
    assert (m.rc, " / ".join(m.j["warnings"]), [p["slot"] for p in m.j["picks"]]) == (
        3,
        "Emby: can't tell your Emby user from the others (set MEDIA_TONIGHT_EMBY_UID)",
        ["C"],
    )
    m.m("tonight", "--json", "--dry-run", env={"MEDIA_TONIGHT_USER": "Nobody"})
    assert (m.rc, m.j["warnings"][0]) == (3, "Seerr: Seerr user 'Nobody': 0 matches (set MEDIA_TONIGHT_USER)")


def test_partial_failures(tn, media):
    m, t = media, media.t
    down = {"MEDIA_SECRETS": t / "seerr-down.json"}
    tn.fresh()
    m.m("tonight", "--json", env={"MEDIA_EMBY_SQL": "exit 1"})
    assert (m.rc, tn.picks()) == (3, "C Trend Wave")
    assert "⚠️ Emby's data didn't load: no library picks" in m.j["message"]
    out = m.m("tonight", "--json", "--dry-run", env=down)
    assert m.rc == 1 and "media: no picks: Seerr: cannot reach seerr" in out  # no cached mapping
    tn.fresh()
    m.m("tonight", "--json", "--args", "scifi")
    m.m("tonight", "--json", "--dry-run", env=down)
    assert (m.rc, [(p["slot"], p["link"]) for p in m.j["picks"]]) == (3, [("A", None), ("B", None)])
    assert "⚠️ the new pick failed" in m.j["message"]
    tn.fresh()
    out = m.m("tonight", "--json", env=dict(down, MEDIA_EMBY_SQL="exit 1"))
    assert (m.rc, out.encode()[:60].decode(errors="ignore")) == (
        1,
        "media: no picks: Seerr: cannot reach seerr: [Errno 111] Conn",
    )
