"""`media unwatched` (cleanup candidates nobody watches): fake_unwatched.py stands in for Sonarr/Radarr/Seerr/Tracearr,
unwatched_fx.py builds Emby's playback_reporting.db + library.db and plays sqlite3 (MEDIA_EMBY_SQL), logging the SQL.
Clock Sat 2026-10-10 12:00 Zurich. Checks: ranking, thresholds, Tracearr/Emby cutoff, id/folder/localized-title/
ambiguous matching, viewers across sources, bogus 1970 dates, short plays, stale seasons, the report, partial
failures, GET-only, read-only SQL, no key leaks."""

from __future__ import annotations

import re

import pytest
from mediatest import KEYS, dead_port, fx, mkcfg


@pytest.fixture
def uw(media):
    t = media.t
    media.py("unwatched_fx.py", "build", t / "emby")
    b = media.serve("fake_unwatched.py")
    mkcfg(t / "cfg.json", f"{b}/seerr", f"{b}/radarr", f"{b}/sonarr", tracearr={"url": f"{b}/tracearr"})
    mkcfg(
        t / "ta-down.json",
        f"{b}/seerr",
        f"{b}/radarr",
        f"{b}/sonarr",
        tracearr={"url": f"http://127.0.0.1:{dead_port()}"},
    )
    (t / "ta-key").write_text("TRACEARR-KEY-Tk55\n")
    (t / "bad-key").write_text("WRONG\n")
    media.env.update(
        MEDIA_SECRETS=str(t / "cfg.json"),
        MEDIA_NOW="2026-10-10T12:00:00+02:00",
        MEDIA_PAGE_SIZE="2",
        MEDIA_TIMEOUT="5",
        MEDIA_TRACEARR_KEYFILE=str(t / "ta-key"),
        MEDIA_EMBY_DIR=str(t / "emby"),
        MEDIA_EMBY_SQL=fx("unwatched_fx.py", "sql", t / "emby", t / "sql.log"),
        MEDIA_REPORT_DIR=str(t / "reports"),
    )
    return media


def cand(m, title, year):
    return [
        (c["plays"], c["viewers"], c["last_played"])
        for c in m.j["candidates"]
        if c["title"] == title and str(c["year"]) == year
    ][0]


SUMMARY = """Unwatched: play history since 25.02.2020 (Emby Playback Reporting, Tracearr from 25.08.2026)
Reclaimable, on disk >90 d:
  no plays 180 d: 2.1 TB (3 shows 2.0 TB, 3 movies 95.0 GB)
  no plays 365 d: 1.8 TB (2 shows 1.7 TB, 3 movies 95.0 GB)
  never played: 585 GB (1 show 500 GB, 2 movies 85.0 GB)
Shows, top 3 of 3 (size × idle time):
 1. Big Old Show · 1.2 TB · last 03.2024 · req. PersonG
 2. Never Show · 500 GB · never played
 3. Ubice mog oca · 300 GB · last 03.2026
Movies, top 3 of 3 (size × idle time):
 1. Huge Remux (2002) · 80.0 GB · never played · req. Timon
 2. Id Movie (2010) · 10.0 GB · last 06.2023
 3. Short One (2018) · 5.0 GB · never played
Stale seasons of watched big shows (365 d): 150 GB in 1 season
88% of last year's plays map to Sonarr/Radarr titles (the rest: 1 title deleted or added by hand)"""


def test_unwatched(uw):
    m, t = uw, uw.t
    assert m.m("unwatched") == SUMMARY and m.rc == 0
    assert len(re.findall(r"^ [0-9]\. ", m.m("unwatched", "--top", "2"), re.M)) == 2  # 1 show + 1 movie
    out = m.m("unwatched", "--type", "tv", "--top", "2")
    assert "Shows, top 2 of 3" in out and "Movies" not in out
    assert "  no plays 180 d: 2.0 TB (3 shows 2.0 TB)" in out
    assert "150 GB in 1 season" in out

    # matching and counting (--days 1 makes every played title a candidate)
    out = m.m("unwatched", "--days", "1", "--json")
    assert m.rc == 0
    assert len(m.j["candidates"]) == 12
    assert cand(m, "Big Old Show", "2004") == (2, 2, "2024-03-01")  # same-day segments merged, backup table too
    assert cand(m, "Ubice mog oca", "2017") == (1, 1, "2026-03-01")  # localized Emby title -> Sonarr item
    assert cand(m, "Watched Big Show", "2021") == (2, 1, "2026-09-20")  # one viewer in Emby + Tracearr
    assert cand(m, "Fresh Show", "2024") == (2, 1, "2026-10-01")  # Tracearr's show rollup
    assert cand(m, "Small Old", "1999") == (1, 1, "2026-09-15")  # Emby row after Tracearr's start ignored
    assert cand(m, "Emby Id Film", "2015") == (1, 1, "2026-09-10")  # rating key -> Emby id -> folder
    assert cand(m, "Id Movie", "2010") == (1, 1, "2023-06-01")  # Emby id with another name -> tmdb
    assert cand(m, "Dune", "1984") == cand(m, "Dune", "2021") == (1, 1, "2026-08-01")  # ambiguous: both
    assert cand(m, "Huge Remux", "2002") == (0, 0, None)  # 1970 date ignored
    assert cand(m, "Short One", "2018") == (0, 0, None)  # play under 2 min ignored
    assert '"New Big"' not in out and '"No File"' not in out
    keys = ("emby_plays", "ta_plays", "by_id", "by_name", "ambiguous", "unmatched", "unmatched_titles", "cutoff")
    assert [m.j["matching"][k] for k in keys] == [8, 5, 7, 4, 1, 2, 1, "2026-08-25"]
    assert m.j["history_start"] == "2020-02-25"
    assert [c["requested_by"] for c in m.j["candidates"] if c["title"] == "Big Old Show"][0] == ["PersonG", "PersonH"]
    m.m("unwatched", "--json")
    assert [(c["title"], c["score"]) for c in m.j["candidates"]] == [
        ("Big Old Show", 3131),
        ("Huge Remux", 530),
        ("Never Show", 381),
        ("Ubice mog oca", 183),
        ("Id Movie", 34),
        ("Short One", 33),
    ]
    # stale seasons: S1 only (S0 specials, S2 watched, S3 just aired)
    assert [(s["title"], s["season"], s["last"]) for s in m.j["stale_seasons"]] == [
        ("Watched Big Show", 1, "2025-01-10")
    ]
    assert m.j["totals"]["no plays 365 d"] == {
        "tv": {"titles": 2, "bytes": 1700000000000},
        "movie": {"titles": 3, "bytes": 95000000000},
    }
    m.m("unwatched", "--min-age", "400", "--json")  # drops Never Show (added 278 d ago)
    assert [c["title"] for c in m.j["candidates"] if c["type"] == "tv"] == ["Big Old Show", "Ubice mog oca"]
    m.m("unwatched", "--days", "0")
    assert m.rc == 1

    # report
    out = m.m("unwatched", "--report")
    r = t / "reports" / "unwatched-2026-10-10.md"
    assert f"Full table: {r}" in out
    rep = r.read_text()
    assert "Play history since **25.02.2020** (Emby Playback Reporting, Tracearr from 25.08.2026)" in rep
    assert "| no plays 180 d | 3 · 2.0 TB | 3 · 95.0 GB | 6 · 2.1 TB |" in rep
    assert (
        "| 1 | Big Old Show (2004) | 1.2 TB | 8 / 80 | 15.01.2019 | 01.03.2024 | 0/0 (2) | 2 | PersonG, PersonH"
        " | ended |" in rep
    )
    assert "| 1 | Huge Remux (2002) | 80.0 GB | 01.06.2019 | never | 0/0 (0) | 0 | Timon Muster |" in rep
    assert "| Watched Big Show | 1 | 150 GB | 10 | 10.01.2025 | 20.09.2026 |" in rep
    assert "  - Gone Show [tv]: 2 plays, last 02.02.2026" in rep
    m.m("unwatched", "--report", t / "custom.md", "--type", "movie")
    assert (t / "custom.md").stat().st_size > 0
    assert "## Shows" not in (t / "custom.md").read_text()

    # sources and failures
    def sqln():
        return sum("-- end" in x for x in (t / "sql.log").read_text().splitlines())

    before = sqln()
    m.m("unwatched", "--no-emby", "--json")
    assert (m.j["sources"], m.j["history_start"]) == ("Tracearr from 25.08.2026", "2026-08-25")
    assert sqln() == before
    out = m.m(
        "unwatched",
        env={"MEDIA_EMBY_SQL": "echo 'ssh: connect to host unraid port 22: Connection refused' >&2; exit 255"},
    )
    assert m.rc == 3
    assert "!! skipped Emby playback DB: ssh: connect to host unraid port 22: Connection refused" in out
    assert "play history since 25.08.2026 (Tracearr from 25.08.2026)" in out
    out = m.m("unwatched", "--json", env={"MEDIA_TRACEARR_KEYFILE": t / "bad-key"})
    assert m.rc == 3 and "Tracearr: tracearr HTTP 401" in out
    # Emby only: its 15.09 row counts, the Tracearr-only play is gone
    assert (m.j["sources"], sorted(c["title"] for c in m.j["candidates"] if c["type"] == "movie")) == (
        "Emby Playback Reporting",
        ["Emby Id Film", "Huge Remux", "Id Movie", "Short One"],
    )
    m.m("unwatched", env={"MEDIA_SECRETS": t / "ta-down.json"})
    assert m.rc == 3
    out = m.m("unwatched", env={"MEDIA_TRACEARR_KEYFILE": t / "nope", "MEDIA_EMBY_SQL": "exit 1"})
    assert m.rc == 1 and "media: no play history: Tracearr: no Tracearr key" in out

    # safety
    assert any("history?pageSize=100&cursor=" in x for x in m.log().splitlines())  # Tracearr pages walked
    m.sql_readonly(t / "sql.log")
    assert f"ATTACH 'file:{t}/emby/library.db?immutable=1' AS lib;" in (t / "sql.log").read_text()
    m.safety(keys=(*KEYS, "TRACEARR-KEY-Tk55"))
