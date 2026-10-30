"""`media`'s read-only extras (airing, missing, cutoff, health, trending, similar, hogs), error paths, the ready-watch
pinger (tg stubbed, fixture edited by ready_fx.py) and the existing commands, against fake_arr.py (one local fake
Seerr/Radarr/Sonarr). The clock is fixed at Sat 2026-10-10 12:00 Zurich, MEDIA_PAGE_SIZE=2 forces paging. Checks:
output, Zurich times across the DST switch, partial failures, timeouts, errors, GET-only, no key leaks."""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

import pytest
from mediatest import dead_port, mkcfg


class Arr:
    def __init__(self, w):
        self.w, t = w, w.t
        self.t = t
        (t / "scenario").write_text("ok\n")
        b = self.b = w.serve("fake_arr.py", t / "scenario", t / "ready.json")
        dead = f"http://127.0.0.1:{dead_port()}"
        self.cfg = mkcfg(t / "cfg.json", f"{b}/seerr", f"{b}/radarr", f"{b}/sonarr/")
        mkcfg(t / "sonarr-down.json", f"{b}/seerr", f"{b}/radarr", dead)
        mkcfg(t / "seerr-down.json", dead, f"{b}/radarr", f"{b}/sonarr")
        mkcfg(t / "all-down.json", dead, dead, dead)
        (t / "badkey.json").write_text(self.cfg.read_text().replace("SEERR-KEY-Zq81", "WRONG"))
        d = json.loads(self.cfg.read_text())
        d.pop("seerr")
        (t / "noseerr.json").write_text(json.dumps(d))
        w.env.update(
            MEDIA_SECRETS=str(self.cfg), MEDIA_NOW="2026-10-10T12:00:00+02:00", MEDIA_PAGE_SIZE="2", MEDIA_TIMEOUT="5"
        )

    def scen(self, s):
        (self.t / "scenario").write_text(s + "\n")


@pytest.fixture
def arr(media):
    return Arr(media)


def after(text: str, pat: str) -> str:
    """`grep -A1 pat`: the matching line and the next."""
    ls = text.split("\n")
    i = next(i for i, x in enumerate(ls) if re.search(pat, x))
    return "\n".join(ls[i : i + 2])


def numbered(text: str) -> list[str]:
    """Lines after the header with the trailing ' (...' cut (sed 1d; sed -E 's/ \\(.*//')."""
    return [re.sub(r" \(.*", "", x) for x in text.split("\n")[1:]]


def test_airing(arr, media):
    m = media
    out = m.m("airing")
    assert m.rc == 0
    assert m.line(1) == "Airing Sat 10.10 – Fri 16.10 (Zurich time, ✓ = on disk): 6 episodes, 2 movie releases"
    assert m.line(2) == "Sat 10.10 (today)"
    assert m.line(3) == '  06:00 Slow Horses S06E05 "Sayonara" ✓'
    assert '  05:30 Night Show S52E03 "Host / Band"' in out  # UTC 03:30 -> 05:30 CEST next day
    assert "  15:00 Daily Shop S2026E2911" in out  # TBA title hidden
    assert "TBA" not in out
    assert "  09:00 Avatar: Seven Havens S01E04-E06 (3 eps)" in out
    assert "  🎬 Hope (2026) digital" in out
    assert "  🎬 Tuner (2026) physical ✓" in out
    assert "Clayface" not in out  # cinema hidden by default
    assert "Late Show" not in out and "Old News" not in out  # outside the window
    assert after(out, "^Tue 13.10") == "Tue 13.10\n  🎬 Hope (2026) digital"
    log = m.log()
    q = "start=2026-10-09T22%3A00%3A00Z&end=2026-10-16T22%3A00%3A00Z&unmonitored=false"
    assert f"/sonarr/api/v3/calendar?{q}&includeSeries=true" in log
    assert f"/radarr/api/v3/calendar?{q}" in log
    assert "  🎬 Clayface (2026) cinema" in m.m("airing", "--cinema")
    out = m.m("airing", "--days", "17")
    assert "end=2026-10-26T23%3A00%3A00Z" in "\n".join(m.log().splitlines()[-2:])
    assert 'Sat 24.10\n  21:00 Late Show S01E01 "Before DST"' in out
    assert 'Mon 26.10\n  20:00 Late Show S01E02 "After DST"' in out
    m.m("airing", "--days", "1")
    assert m.line(1) == "Airing Sat 10.10 (Zurich time, ✓ = on disk): 1 episode, 0 movie releases"
    assert m.m("airing", env={"MEDIA_NOW": "2026-12-20T10:00:00+01:00"}) == "Nothing airing Sun 20.12 – Sat 26.12."
    m.m("airing", "--json")
    d = m.j
    assert len(d["episodes"]) == 6
    assert [(x["title"], x["kind"], x["date"]) for x in d["movies"]] == [
        ("Hope", "digital", "2026-10-13"),
        ("Tuner", "physical", "2026-10-16"),
    ]
    assert [x["air_local"] for x in d["episodes"] if x["series"] == "Night Show"][0] == "2026-10-11T05:30:00+02:00"
    m.m("airing", "--days", "0")
    assert m.rc == 1
    m.safety()


def test_missing_cutoff(arr, media):
    m = media
    out = m.m("missing")
    assert m.rc == 0
    assert m.line(1) == "Missing (monitored, no file): 7 episodes in 2 shows, 2 released movies; 3 more not out yet"
    assert "  Daily Shop: 5 eps (S2026), last aired 25.09.2026" in m.line(3)
    assert "  Top Gear: 2 eps (S3,S4), last aired 01.01.1981" in out
    assert "  Stuck Doc (2023), wanted since 02.2024" in after(out, "released but not")
    assert "  Verity (2026): digital 27.10.2026" in out
    assert "  Wildwood (2026): cinema 22.10.2026" in out
    assert "  Ghost Film (2027): no date yet" in out
    assert m.logn("sonarr/api/v3/wanted/missing") == 4  # 7 records, page size 2
    assert m.logn("radarr/api/v3/wanted/missing") == 3
    assert "wanted/missing?monitored=true&includeSeries=true&page=1&pageSize=2" in m.log()
    assert "  … +1 more" in m.m("missing", "-n", "1")
    m.m("missing", "--json")
    assert [(s["series"], s["episodes"], s["seasons"]) for s in m.j["shows"]] == [
        ("Daily Shop", 5, [2026]),
        ("Top Gear", 2, [3, 4]),
    ]
    assert [x["title"] for x in m.j["movies_not_out"]] == ["Wildwood", "Verity", "Ghost Film"]

    out = m.m("cutoff")
    assert m.rc == 0
    assert m.line(1) == (
        "Below quality cutoff (upgrade candidates; upgrades usually need more space): 4 episodes in 2 shows, 2 movies"
    )
    assert "  Big Old Show: 3 eps below WEB 1080p (Up to 1080p)" in out
    assert "  Fresh Show: 1 ep below Bluray-2160p (Up to 4K)" in out
    assert "  Startup.com (2001): has DVD, cutoff Bluray-1080p" in out
    assert "  Another Film (2004): has DVD-R, cutoff Bluray-1080p" in out
    m.m("cutoff", "--json")
    assert (m.j["episodes_total"], [x["quality"] for x in m.j["movies"]]) == (4, ["DVD-R", "DVD"])
    m.safety()


def test_health(arr, media):
    m = media
    assert m.m("health") == "all fine: Sonarr, Radarr, Seerr" and m.rc == 0
    assert (m.m("health", "--quiet"), m.rc) == ("", 0)
    arr.scen("notes")
    assert (m.m("health"), m.rc) == (
        "all fine: Sonarr, Radarr, Seerr · notes: Radarr: New update available;"
        " Seerr: update available (3 commits behind)",
        0,
    )
    arr.scen("unhealthy")
    out = m.m("health")
    assert m.rc == 2
    assert m.line(1) == "media health: 4 problems"
    assert "  Sonarr warning: Indexers unavailable due to failures: NZBgeek" in out
    assert "  Radarr error: Missing root folder: /data/Movies" in out
    assert "  Seerr warning: restart required" in out
    assert "  Seerr error: cannot reach radarr: Radarr 4K" in out
    assert "  (Seerr note: update available)" in out
    m.m("health", "--quiet")
    assert m.rc == 2
    m.m("health", "--json")
    d = m.j
    assert (d["ok"], len(d["problems"]), d["problems"][0]["wiki"]) == (
        False,
        4,
        "https://wiki.servarr.com/sonarr/system#indexers",
    )
    arr.scen("ok")
    out = m.m("health", env={"MEDIA_SECRETS": m.t / "sonarr-down.json"})
    assert m.rc == 2 and "  Sonarr error: unreachable (cannot reach sonarr:" in out
    m.safety()


def test_trending_similar(arr, media):
    m = media
    assert m.m("trending", "-n", "2") == (
        "Trending titles this week (TMDB via Seerr):\n"
        " 1. Spider-Man: Brand New Day (2026) [movie] ★8.4 tmdb:101 -- available\n"
        " 2. Carrie (2026) [tv] ★8.4 tmdb:102 -- not requested"
    )
    assert m.logn("discover/trending") == 1
    out = m.m("trending")
    assert len(re.findall(r"^ *[0-9]*\. ", out, re.M)) == 6
    assert "Some Actor" not in out
    assert "Pending Show (2026) [tv] ★7.0 tmdb:105 -- requested (pending approval)" in out
    assert " 6. Matchbox the Movie (2026) [movie] tmdb:107 -- not requested" in out
    assert numbered(m.m("trending", "--new")) == [" 1. Carrie", " 2. Matchbox the Movie"]
    out = m.m("trending", "--type", "tv", "-n", "2")
    assert numbered(out) == [" 1. Carrie", " 2. Pending Show"]
    assert "Trending shows this week" in out
    m.m("trending", "--json", "-n", "1")
    assert m.j == [
        {
            "title": "Spider-Man: Brand New Day",
            "year": "2026",
            "type": "movie",
            "tmdb": 101,
            "rating": 8.4,
            "status": "available",
        }
    ]

    out = m.m("similar", "Inception")
    assert m.line(1) == "Like Inception (2010) [movie]:"
    assert " 2. The Dark Tower (2017) [movie] ★5.8 tmdb:353491 -- available" in out
    assert numbered(m.m("similar", "Inception", "--new")) == [" 1. Paycheck", " 2. Interstellar"]
    assert m.m("similar", "breaking bad") == (
        "Like Breaking Bad (2008) [tv]:\n"
        " 1. Narcos: Mexico (2018) [tv] ★7.9 tmdb:80968 -- not requested\n"
        " 2. The Wire (2002) [tv] ★8.6 tmdb:1438 -- available"
    )
    out = m.m("similar", "--tmdb", "27205", "--type", "movie", "-n", "1")
    assert (m.line(1), len(out.split("\n"))) == ("Like Inception (2010) [movie]:", 2)
    m.m("similar", "--tmdb", "27205")
    assert m.rc == 1
    assert (m.m("similar", "zzzunknown"), m.rc) == ('no match for "zzzunknown"', 0)
    m.m("similar", "Inception", "--json", "-n", "1")
    assert (m.j["like"]["tmdb"], m.j["results"][0]["title"]) == (27205, "Paycheck")
    m.safety()


def test_hogs(arr, media):
    m = media
    out = m.m("hogs")
    assert m.rc == 0
    assert m.line(1) == "/data: 113 TB used of 123 TB (92%), 9.7 TB free"
    assert "Library: movies 3 = 112 GB, shows 2 =" in m.line(2)
    assert m.line(3) == "Movies by quality: Remux-2160p 1 = 80.0 GB, Bluray-2160p 1 = 30.0 GB, DVD 1 = 2.0 GB"
    assert m.line(4) == "Top 5 by size = 1.4 TB, 100% of the library:"
    assert m.line(5) == (
        " 1. Big Old Show [tv] 1.2 TB · 8 seasons/160 eps (7.5 GB/ep) · ended · last aired 05.2012 · added 01.2019"
        " · req. PersonG+PersonH"
    )
    assert m.line(6) == " 2. Huge Remux (2002) 80.0 GB · Remux-2160p · file from 06.2019 · req. Timon"
    assert m.line(7) == (
        " 3. Fresh Show [tv] 50.0 GB · 2 seasons/20 eps (2.5 GB/ep) · continuing · last aired 10.2026 · added 03.2024"
        " · req. Noah"
    )
    assert m.line(9) == " 5. Small Old (1999) 2.0 GB · DVD · file from 01.2015"
    assert "No File" not in out
    assert m.logn("seerr/api/v1/request") == 3  # 5 requests, page size 2
    out = m.m("hogs", "--sort", "stale")
    stale = [re.sub(r" [\[(0-9].*", "", re.sub(r"^ *[0-9]+\. ", "", x)) for x in out.split("\n")[4:9]]
    assert stale == ["Big Old Show", "Huge Remux", "Small Old", "New Big", "Fresh Show"]
    assert "nothing new for 7.7 y" in out
    m.m("hogs", "--top", "2", "--type", "movie")
    assert "\n".join([m.line(2), m.lines(4, 6)]) == (
        "Library: movies 3 = 112 GB\n"
        "Top 2 by size = 110 GB, 98% of the library:\n"
        " 1. Huge Remux (2002) 80.0 GB · Remux-2160p · file from 06.2019 · req. Timon\n"
        " 2. New Big (2026) 30.0 GB · Bluray-2160p · file from 09.2026"
    )
    m.m("hogs", "--type", "tv", "--top", "1")
    assert m.lines(1, 3) == (
        "/data: 113 TB used of 123 TB (92%), 9.7 TB free\n"
        "Library: shows 2 = 1.2 TB\n"
        "Top 1 by size = 1.2 TB, 96% of the library:"
    )
    m.m("hogs", "--json", "--top", "1")
    d = m.j
    top = d["top"][0]
    assert (top["title"], top["requested_by"], top["score"], d["library"]["movie"]["titles"], top["added"]) == (
        "Big Old Show",
        ["PersonG", "PersonH"],
        9240,
        3,
        "2019-01-15T10:00:00+00:00",
    )
    out = m.m("hogs", "--top", "1", env={"MEDIA_SECRETS": m.t / "seerr-down.json"})
    assert m.rc == 0 and "(no requester names: Seerr requesters: cannot reach seerr:" in out
    arr.scen("badjson")
    out = m.m("hogs", "--top", "1")
    assert m.rc == 3
    assert " 1. Big Old Show [tv]" in out
    assert "!! skipped Radarr: radarr sent non-JSON on GET /api/v3/movie (wrong URL, or a login page?)" in out
    m.safety()


def test_errors_and_partial_results(arr, media):
    m, t = media, media.t
    arr.scen("http500")
    out = m.m("missing")
    assert m.rc == 3
    assert "  Stuck Doc (2023)" in out
    assert '!! skipped Sonarr: sonarr HTTP 500 on GET /api/v3/wanted/missing: {"message": "database is locked"}' in out
    m.m("airing", "--json")
    assert (m.rc, len(m.j["warnings"])) == (3, 1)
    out = m.m("queue")
    assert m.rc == 1 and "media: sonarr HTTP 500 on GET /api/v3/queue" in out
    arr.scen("ok")
    out = m.m("missing", env={"MEDIA_SECRETS": t / "all-down.json"})
    assert m.rc == 1
    assert "media: Sonarr: cannot reach sonarr:" in out and "; Radarr: cannot reach radarr:" in out
    arr.scen("slow")
    s0 = time.monotonic()
    out = m.m("trending", env={"MEDIA_TIMEOUT": 1})
    took = time.monotonic() - s0
    assert m.rc == 1
    assert out == "media: seerr timed out after 1s on GET /api/v1/discover/trending"
    assert took < 2.5  # the server sleeps 3 s
    arr.scen("ok")
    out = m.m("trending", env={"MEDIA_SECRETS": t / "badkey.json"})
    assert (m.rc, out) == (1, 'media: seerr HTTP 401 on GET /api/v1/discover/trending: {"error": "Unauthorized"}')
    out = m.m("trending", env={"MEDIA_SECRETS": t / "noseerr.json"})
    assert f"media: no seerr url/key in {t}/noseerr.json" in out
    out = m.m("health", env={"MEDIA_SECRETS": t / "nope.json"})
    assert (m.rc, out) == (1, f"media: cannot read {t}/nope.json: FileNotFoundError")
    m.m("missing", "-n", "0")
    assert m.rc == 1
    m.safety()


def test_ready_watch(arr, media):
    m, t = media, media.t
    m.py("ready_fx.py", t / "ready.json", "init")
    (t / "tg").write_text('#!/bin/sh\n[ -e "$TG_FAIL" ] && exit 1\nprintf \'%s\\n--\\n\' "$1" >> "$TG_LOG"\n')
    (t / "tg").chmod(0o755)
    tg_log, tg_fail, inbox = t / "tg.log", t / "tg.fail", t / "inbox"
    m.env.update(
        MEDIA_READY_STATE=str(t / "rw"),
        MEDIA_READY_TG=str(t / "tg"),
        MEDIA_READY_INBOX=str(inbox),
        TG_LOG=str(tg_log),
        TG_FAIL=str(tg_fail),
        MEDIA_READY_QUIET="0-8",
        MEDIA_READY_FAIL_NOTE="2",
    )
    tg_log.write_text("")

    def rfx(*a):
        m.py("ready_fx.py", t / "ready.json", *a)

    def tgn():
        return sum(x == "--" for x in tg_log.read_text().splitlines())

    def sent():
        s = tg_log.read_text().rstrip("\n")
        tg_log.write_text("")
        return s

    def rw(at, *a, **env):
        return m.m("ready-watch", *a, env=dict(env, MEDIA_NOW=at))

    def st():
        return json.loads((t / "rw" / "state.json").read_text())

    def notes():
        return sorted(inbox.iterdir()) if inbox.is_dir() else []

    out = rw("2026-10-10T10:00:00Z")
    assert m.rc == 0
    assert "baseline: 1 of robin's requested movies already available; no pings" in out
    assert "baseline: Sonarr history up to 2026-10-10T10:00:00Z" in out
    assert tgn() == 0
    # robin = the fixture profile's services.seerr_user, resolved exactly (not 'Robin')
    assert st()["user"] == {"query": "robin", "id": 1, "name": "robin"}
    assert st()["movies_done"] == ["501"]  # the 4K request needs status4k
    assert st()["sonarr"]["seen"] == [1]  # pre-baseline import marked seen
    assert "/seerr/api/v1/user/1/requests?take=2&skip=2" in m.log()
    assert (
        "/sonarr/api/v3/history/since?date=2026-10-10T09%3A00%3A00Z&includeSeries=true&includeEpisode=true" in m.log()
    )

    out = rw("2026-10-10T10:10:00Z")
    assert (out, m.rc, tgn()) == ("", 0, 0)
    assert "date=2026-10-10T09%3A00%3A00Z" in m.log()
    assert m.logn("seerr/api/v1/user?") == 2  # user cached: no user list per run

    for a in (("movie", 502, 5), ("movie", 601, 5), ("movie", 503, 5), ("movie", 901, 5)):
        rfx(*a)
    rfx("imp", 2, "2026-10-10T10:15:00Z", "pl", 1, 3)
    rfx("imp", 3, "2026-10-10T10:15:00Z", "pl", 1, 4)
    rfx("imp", 4, "2026-10-10T10:15:01Z", "pl", 1, 5)
    rfx("imp", 5, "2026-10-10T10:15:00Z", "ls", 1, 1)
    rfx("upg", 6, "2026-10-10T10:16:00Z", "sh", 6, 4)
    rfx("grab", 7, "2026-10-10T10:17:00Z", "sh", 6, 9)
    out = rw("2026-10-10T10:20:00Z")
    assert m.rc == 0
    assert sent() == "🎬 Dune: Part Three (2026) is ready on Emby\n--\n📺 Pluribus S01E03–E05 ready\n--"
    assert "2026-10-10 12:20 sent: 🎬 Dune: Part Three (2026) is ready on Emby" in out
    out = rw("2026-10-10T10:30:00Z")
    assert (out, tgn()) == ("", 0)

    for i, e in ((8, 8), (9, 10), (10, 11)):
        rfx("imp", i, "2026-10-10T10:35:00Z", "sh", 6, e)
    rfx("imp", 11, "2026-10-10T10:35:00Z", "sh", 5, 2)
    rw("2026-10-10T10:40:00Z")
    assert sent() == "📺 Slow Horses S05E02, S06E08, E10–E11 ready\n--"

    # quiet hours, retries, batching
    rfx("addmovie", 1, 505, "Night Movie", "2025-11-11", 5)
    rfx("imp", 12, "2026-10-11T00:50:00Z", "pl", 1, 6)
    out = rw("2026-10-11T01:00:00Z")
    assert (m.rc, tgn()) == (0, 0)  # 03:00 Zurich: held
    assert "quiet hours: holding 2 ping(s): Night Movie (2025); Pluribus S01E06" in out
    rfx("imp", 13, "2026-10-11T02:50:00Z", "pl", 1, 7)
    rw("2026-10-11T03:00:00Z")
    assert tgn() == 0
    assert [p.get("eps") for p in st()["pending"]] == [None, {"1": [6, 7]}]  # same show merged in the queue
    rw("2026-10-11T06:10:00Z")
    assert sent() == "🎬 Night Movie (2025) is ready on Emby\n--\n📺 Pluribus S01E06–E07 ready\n--"
    assert st()["pending"] == []

    tg_fail.touch()
    rfx("addmovie", 1, 506, "Retry Film", "2026-01-01", 5)
    out = rw("2026-10-11T06:20:00Z")
    assert (m.rc, len(st()["pending"])) == (3, 1)
    assert "tg failed; 1 ping(s) kept for the next run" in out
    out = m.m("ready-watch", "--status", env={"MEDIA_NOW": "2026-10-11T06:25:00Z"})
    assert "pending: 1\n  🎬 Retry Film (2026) is ready on Emby" in out
    assert "failing: 1 run(s) in a row; last error: tg: delivery failed" in out
    tg_fail.unlink()
    rw("2026-10-11T06:30:00Z")
    assert (m.rc, sent()) == (0, "🎬 Retry Film (2026) is ready on Emby\n--")

    for i in range(1, 6):
        rfx("addmovie", 1, 510 + i, f"Batch {i}", f"2026-0{i}-01", 5)
    rw("2026-10-11T06:40:00Z")
    assert sent() == "🍿 Ready on Emby (5):\n" + "".join(f"🎬 Batch {i} (2026)\n" for i in range(1, 6)) + "--"

    # failures
    arr.scen("http500")
    rfx("addmovie", 1, 520, "During Outage", "2026-02-02", 5)
    rfx("imp", 14, "2026-10-11T06:45:00Z", "sh", 6, 12)
    out = rw("2026-10-11T06:50:00Z")
    assert (m.rc, sent()) == (3, "🎬 During Outage (2026) is ready on Emby\n--")  # Sonarr down: movie still pinged
    assert "!! Sonarr: sonarr HTTP 500 on GET /api/v3/history/since" in out
    arr.scen("ok")
    rw("2026-10-11T07:00:00Z")
    assert (m.rc, sent()) == (0, "📺 Slow Horses S06E12 ready\n--")  # missed episode caught up
    assert st()["failures"] == 0
    down = {"MEDIA_SECRETS": str(t / "seerr-down.json")}
    out = rw("2026-10-11T07:10:00Z", **down)
    assert (m.rc, tgn()) == (1, 0)
    assert "media: ready-watch: cannot reach seerr:" in out
    assert len(notes()) == 0  # no inbox note after 1 failure
    rw("2026-10-11T07:20:00Z", **down)
    assert len(notes()) == 1
    assert "media ready-watch has failed 2 runs in a row" in notes()[0].read_text()
    rw("2026-10-11T07:30:00Z", **down)
    assert len(notes()) == 1  # one note per streak

    rfx("addmovie", 1, 530, "Dry Film", "2026-03-03", 5)
    before = (t / "rw" / "state.json").read_bytes()
    out = rw("2026-10-11T07:40:00Z", "--dry-run")
    assert (m.rc, tgn()) == (0, 0)
    assert "(dry run) would send: 🎬 Dry Film (2026) is ready on Emby" in out
    assert (t / "rw" / "state.json").read_bytes() == before
    rw("2026-10-11T07:50:00Z")
    assert sent() == "🎬 Dry Film (2026) is ready on Emby\n--"

    (t / "rw" / "state.json").write_text("{broken\n")
    for n in notes():
        n.unlink()
    rfx("addmovie", 1, 540, "After Corruption", "2026-04-04", 5)
    out = rw("2026-10-11T08:00:00Z")
    assert (m.rc, tgn()) == (0, 0)  # corrupt state: fresh baseline, no flood
    assert "state unreadable (JSONDecodeError), moved to" in out
    assert len(notes()) == 1

    shutil.rmtree(t / "rw")
    arr.scen("http500")
    rw("2026-10-11T08:10:00Z")
    assert (m.rc, tgn()) == (3, 0)  # first run with Sonarr down: no pings
    assert st().get("sonarr") is None and len(st()["movies_done"]) > 5  # movie baseline kept, Sonarr pending
    arr.scen("ok")
    out = rw("2026-10-11T08:20:00Z")
    assert "baseline: Sonarr history up to" in out
    assert tgn() == 0
    allout = m.everything()
    # never pinged: declined / PersonG's / capitalized Robin / 4K-only / an upgrade
    for never in ("Declined Film is ready", "PersonG's Movie", "PersonG Show", "Capitalized Robin", "Four K Film"):
        assert never not in allout
    assert "S06E04" not in allout
    m.safety()


def test_existing_commands(arr, media):
    m = media
    m.m("search", "inception")
    assert m.line(1) == "Inception (2010)  [movie]  tmdb:27205  -- available"
    assert m.m("queue") == "nothing downloading"
    # --json on search/status/queue/request (the typed tools' shape)
    m.m("search", "inception", "--json")
    x = m.j[0]
    assert (x["title"], x["year"], x["type"], x["tmdb"], x["status"]) == (
        "Inception",
        "2010",
        "movie",
        27205,
        "available",
    )
    m.m("status", "inception", "--json")
    x = m.j[0]
    assert (x["title"], x["status"], x["requests"], x["downloading"]) == ("Inception", "available", [], [])
    assert m.m("queue", "--json") == '{\n "movies": [],\n "tv": []\n}'
    m.m("request", "27205", "--type", "movie", "--json")
    assert (m.j["result"], m.j["reason"]) == ("not_requested", "already available")
    out = m.m("-h")
    for usage in (
        "media hogs [--top 20]",
        "media unwatched [--top 15]",
        "media tonight [--json] [--mood X] [--time MIN]",
    ):
        assert usage in out
    m.safety()


def test_default_emby_sql_is_read_only():
    """The default Emby command (no MEDIA_EMBY_SQL) opens the playback DB read-only and immutable."""
    src = (Path(__file__).resolve().parents[2] / "scripts" / "media").read_text()
    lines = [x for x in src.splitlines() if "sqlite3 -readonly -batch -ascii" in x]
    assert any("'file:{dir}/playback_reporting.db?immutable=1'" in x for x in lines)
