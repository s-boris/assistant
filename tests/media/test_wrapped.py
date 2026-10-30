"""`media wrapped` (his Emby year in review). wrapped_fx.py builds fictional Emby DBs (his plays, a friend's plays, a
backup table, replaced item ids, a stuck session) and serves fake Seerr users + Emby covers; tonight_fx.py plays the
SSH lockdown verb (or the root sqlite3 command) and logs the SQL. Clock Sat 2026-10-10 20:00 Zurich. Checks: numbers,
card, JSON, viewing-day rule, rewatch vs partial, name fallback, caps, HTML (offline, covers, size), privacy (his rows
only), GET-only, no key leaks."""

from __future__ import annotations

import json
import re

import pytest
from mediatest import dead_port, fx

KEYS = ("SEERR-KEY-Zq81", "EMBY-KEY-Wr55")


@pytest.fixture
def wr(media):
    t = media.t
    media.py("wrapped_fx.py", "build", t / "emby")
    b = media.serve("wrapped_fx.py", pre=("serve",))
    (t / "cfg.json").write_text(
        json.dumps({"seerr": {"url": f"{b}/seerr", "key": KEYS[0]}, "emby": {"url": b, "key": KEYS[1]}})
    )
    (t / "seerr-down.json").write_text(
        json.dumps(
            {"seerr": {"url": f"http://127.0.0.1:{dead_port()}", "key": KEYS[0]}, "emby": {"url": b, "key": KEYS[1]}}
        )
    )
    media.root_sql = fx("tonight_fx.py", "sql", t / "emby", t / "sql.log")
    media.env.update(
        MEDIA_SECRETS=str(t / "cfg.json"),
        MEDIA_NOW="2026-10-10T20:00:00+02:00",
        MEDIA_TIMEOUT="5",
        MEDIA_EMBY_DIR=str(t / "emby"),
        MEDIA_EMBY_SQL=media.root_sql + " {db}",
    )
    return media


def test_wrapped(wr):
    m, t = wr, wr.t

    def w(*a, **env):
        return m.m("wrapped", *a, env=env, merge=False)

    # numbers (lockdown verb SQL)
    w("--json")
    d = m.j
    assert m.rc == 0
    assert (d["hours"], d["plays"], d["days"]) == (17.5, 16, 10)
    assert (d["films"], d["film_plays"], d["episodes"], d["episode_plays"], d["shows"]) == (5, 7, 9, 9, 2)
    # genres by time watched (show genres for episodes, Genres column fallback)
    assert [(g["name"], g["pct"]) for g in d["genres"]] == [
        ("Science Fiction", 38),
        ("Drama", 34),
        ("Comedy", 16),
        ("Western", 13),
    ]
    assert [(s["name"], s["episodes"], s["hours"]) for s in d["top_shows"]] == [
        ("Nova Drift", 6, 4.5),
        ("Laugh Track", 3, 1.1),
    ]
    bg = d["binge"]
    assert (bg["day"], bg["episodes"], bg["show"], bg["show_episodes"]) == ("2026-03-07", 7, "Nova Drift", 6)
    assert (d["fav_weekday"], d["fav_hour"]) == ("Sat", 20)
    assert (d["rewatched"]["title"], d["rewatched"]["times"]) == ("Star Voyage", 2)  # a 10-min replay is not one
    assert (d["first"]["title"], d["first"]["at"], d["latest"]["title"]) == (
        "Star Voyage",
        "2026-01-03T20:00",
        "Vanished Film",
    )
    assert ([(x["decade"], x["films"]) for x in d["decades"]], d["top_decade"]) == (
        [("1960s", 1), ("2010s", 2), ("2020s", 1)],
        "2010s",
    )
    assert (d["uhd_pct"], d["uhd_films"]) == (51, 2)
    assert d["fun_fact"] == "🏔 Longest film: Long Epic (3 h 20 min)."
    assert [(x["name"], x["pct"]) for x in d["screens"]] == [("Emby for LG", 98), ("Emby for Android", 2)]
    assert sorted(x["title"] for x in d["film_wall"]) == [  # replaced ids matched by name
        "Long Epic",
        "Old Western",
        "Renamed Copy",
        "Star Voyage",
        "Vanished Film",
    ]
    assert "Friend Only Film" not in m.out and "Some Channel" not in m.out

    # card
    card = w()
    assert m.rc == 0
    assert len(card.split("\n")) <= 8
    for part in (
        "🎬 Emby Wrapped 2026 so far (to 10.10)",
        "⏱ 17.5 h · 5 films · 9 episodes of 2 shows",
        "🍿 Binge record: 7 episodes on Sat 07.03 (Nova Drift)",
        "📅 Saturdays around 20:00 · 4K 51 % · films: the 2010s",
        "🔁 Star Voyage 2× · first Star Voyage 03.01",
    ):
        assert part in card
    assert w(MEDIA_EMBY_SQL=m.root_sql) == card  # root sqlite3 command (ATTACH library)

    # years
    w("--year", "2025", "--json")
    assert (m.j["plays"], m.j["films"], m.j["partial"], m.j["latest"]["title"]) == (1, 1, False, "Old Western")
    w("--year", "2024")
    assert m.line(2) == "No plays of yours in 2024."
    w("--year", "2031")
    assert m.rc == 1

    # html
    m.log_path.write_text("")
    h = t / "html" / "w.html"
    w("--html", h, "--json")
    assert m.rc == 0
    assert (m.j["html"]["path"].endswith("w.html"), m.j["html"]["covers"]) == (True, 5)  # 404 + non-image skipped
    html = h.read_text()
    assert h.stat().st_size < 3_000_000
    assert "Binge record" in html
    assert 'src="data:image/png;base64,' in html
    assert not re.search(r'(src|href)="https?:|url\(https?:|@import', html)  # offline
    assert "Friend Only Film" not in html
    assert "<title>Emby Wrapped 2026</title>" in html
    log = m.log().splitlines()
    assert (sum(bool(re.search(r"Images/Primary.*token=yes", x)) for x in log), sum("api_key" in x for x in log)) == (
        7,
        0,
    )
    w("--html", t / "nc.html", "--no-covers", "--json")
    assert (m.j["html"]["covers"], m.logn("Images")) == (0, 7)
    w("--html", "--no-covers", HOME=str(t / "home"))
    assert "reports/media/emby-wrapped-2026.html" in m.out.split("\n")[-1]
    assert [p.name for p in (t / "home" / "reports" / "media").iterdir()] == ["emby-wrapped-2026.html"]
    w("--html", "/proc/nope/w.html")
    assert m.rc == 1 and "can't write" in m.err

    # user mapping and failures
    m.log_path.write_text("")
    assert w(MEDIA_WRAPPED_GUID="aaaa1111-0000-0000-0000-000000000000") == card
    assert m.logn("seerr") == 0
    w(MEDIA_SECRETS=str(t / "seerr-down.json"))
    assert m.rc == 1 and "can't tell which Emby user is yours" in m.err
    w(MEDIA_EMBY_SQL="exit 1")
    assert m.rc == 1

    # privacy + safety
    sql = (t / "sql.log").read_text()
    reads = [x for x in sql.splitlines() if "FROM (SELECT DateCreated" in x]
    assert reads and all("= 'aaaa1111000000000000000000000000'" in x for x in reads)  # his user id only
    assert "bbbb2222" not in sql
    assert "claude-bot-cmd:" not in m.everything()
    assert "EMBY-KEY-Wr55" not in html
    m.safety(keys=KEYS)
