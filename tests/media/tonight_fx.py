#!/usr/bin/env python3
"""Emby fixtures for the `media tonight` tests (stdlib only).

  tonight_fx.py build DIR           write DIR/library.db + DIR/playback_reporting.db (Emby library + Playback Reporting)
  tonight_fx.py sql DIR LOG [DB]    fake `sqlite3 -readonly -batch -ascii`: SQL on stdin (logged to LOG), rows out in
                                    ascii mode (0x1F / 0x1E). With DB (library|playback) it plays the SSH lockdown verb
                                    `emby-sql DB` and enforces its rules (one SELECT/WITH, ASCII only, no comments, no
                                    ';' chains, no write/attach/pragma keywords); without DB it plays the root command
                                    (playback_reporting.db, several statements, ATTACH allowed).

Fake clock: Sat 2026-10-10 20:00 Zurich (NOW below). The owner's Emby id is OWNER (Seerr has it dashed, uppercase);
his internal UserDatas id is 7, another user is 3.
"""

import os
import re
import sqlite3
import sys

NOW = 1791655200  # 2026-10-10 20:00 Zurich
DAY = 86400
OWNER, OTHER = "aaaa1111" + "0" * 24, "bbbb2222" + "0" * 24
TICKS = 600000000  # per minute
BULK_TS = NOW - 900 * DAY

GENRES = {
    901: "Science Fiction",
    902: "Comedy",
    903: "Drama",
    904: "Horror",
    905: "Family",
    906: "Animation",
    907: "Thriller",
    908: "Documentary",
    909: "Sci-Fi & Fantasy",
    911: "Adventure",
    912: "Western",
}
# (Id, type, Name, year, rating, minutes, ProviderIds, DateCreated, Genres column, ExtraType, Path, genre links)
MOVIES = [
    (101, "Arrival", 2016, 7.9, 116, "Tmdb=329865|Imdb=tt2543164", NOW - 400 * DAY, None, None, "/m/a.mkv", [901, 903]),
    (103, "Dune: Part Two", 2024, 8.6, 166, "Tmdb=693134", NOW - 100 * DAY, None, None, "/m/d.mkv", [901]),
    (104, "Short Film", 2025, 8.8, 25, "Tmdb=104", NOW - 50 * DAY, None, None, "/m/s.mkv", [901]),
    (105, "Low Rated", 2020, 4.0, 100, "Tmdb=105", NOW - 50 * DAY, None, None, "/m/l.mkv", [901]),
    (106, "Req_Film [Cut]", 2025, 6.5, 95, "Tmdb=5550", NOW - 3 * DAY, None, None, "/m/r.mkv", [901, 903]),
    (107, "Kids Toon", 2024, 8.9, 90, "Tmdb=107", NOW - 50 * DAY, None, None, "/m/k.mkv", [905, 906]),
    (108, "Scary Night", 2023, 7.5, 100, "Tmdb=108", NOW - 200 * DAY, None, None, "/m/sn.mkv", [904]),
    (109, "Funny One", 2022, 7.2, 92, "Tmdb=109", NOW - 200 * DAY, None, None, "/m/f.mkv", [902]),
    (110, "Krimi Film", 2019, 7.0, 100, "Tmdb=110", NOW - 300 * DAY, "Krimi|Drama", None, "/m/kr.mkv", []),
    (111, "Trailer Thing", 2024, 9.5, 2, "Tmdb=111", NOW - 10 * DAY, None, 1, "/m/t.mkv", [901]),
    (112, "Virtual Movie", 2024, 9.5, 120, "Tmdb=112", NOW - 10 * DAY, None, None, None, [901]),
    (113, "Half Seen", 2021, 8.0, 110, "Tmdb=113", NOW - 300 * DAY, None, None, "/m/h.mkv", [901]),
    (114, "Other User Saw", 2021, 6.0, 120, "Tmdb=114", NOW - 300 * DAY, None, None, "/m/o.mkv", [901]),
    # ratings: an Emby 10.0 (suspect), a 8.9 from 4 TMDB votes, a solid 8.0, his fresh request at 5.8, a 0.0 (no votes)
    (120, "Ten Western", 2024, 10.0, 100, "Tmdb=120", NOW - 300 * DAY, None, None, "/m/w0.mkv", [912]),
    (121, "Gem Western", 2023, 8.9, 100, "Tmdb=121", NOW - 300 * DAY, None, None, "/m/w1.mkv", [912]),
    (122, "Solid Western", 2022, 8.0, 100, "Tmdb=122", NOW - 300 * DAY, None, None, "/m/w2.mkv", [912]),
    (123, "Weak Western", 2025, 5.8, 100, "Tmdb=123", NOW - 5 * DAY, None, None, "/m/w3.mkv", [912]),
    (124, "Unrated Western", 2025, 0.0, 100, "Tmdb=124", NOW - 300 * DAY, None, None, "/m/w4.mkv", [912]),
] + [
    (150 + i, f"Bulk Movie {i}", 2001, 7.0, 100, f"Tmdb={150 + i}", NOW - 1000 * DAY, None, None, f"/m/b{i}.mkv", [902])
    for i in range(10)
]
# series: (Id, Name, year, rating, ProviderIds, genre links, [(ep id, season, episode, minutes, added)])
SERIES = [
    (
        201,
        "Star Voyage",
        2022,
        8.2,
        "Tmdb=2010|Tvdb=20",
        [909, 903],
        [
            (2011, 1, 1, 45, NOW - 90 * DAY),
            (2012, 1, 2, 45, NOW - 90 * DAY),
            (2013, 1, 3, 45, NOW - 90 * DAY),
            (2014, 1, 4, 45, NOW - 90 * DAY),
            (2015, 1, 5, 45, NOW - 90 * DAY),
            (2019, 0, 1, 20, NOW - 90 * DAY),
            (2016, 2, 1, 45, NOW - 2 * DAY),
        ],
    ),
    (
        202,
        "Old Show",
        2010,
        7.9,
        "Tmdb=2020",
        [902],
        [(2021, 1, 1, 22, NOW - 500 * DAY), (2022, 1, 2, 22, NOW - 500 * DAY)],
    ),
    (
        203,
        "Fresh Hit",
        2025,
        8.6,
        "Tmdb=2030",
        [907, 903],
        [(2031, 1, 1, 50, NOW - 20 * DAY), (2032, 1, 2, 50, NOW - 20 * DAY)],
    ),
    (204, "Kids Series", 2024, 9.0, "Tmdb=2040", [905, 906], [(2041, 1, 1, 12, NOW - 20 * DAY)]),
    (
        205,
        "Caught Up",
        2023,
        8.0,
        "Tmdb=2050",
        [902],
        [(2051, 1, 1, 25, NOW - 60 * DAY), (2052, 1, 2, 25, NOW - 60 * DAY)],
    ),
    (
        206,
        "Quick Laughs",
        2024,
        7.8,
        "Tmdb=2060",
        [902],
        [(2061, 1, 1, 22, NOW - 30 * DAY), (2062, 1, 2, 22, NOW - 30 * DAY)],
    ),
]
# UserDatas: (userId, item Id, played, LastPlayedDateInt, isFavorite, playbackPositionTicks)
USERDATA = [
    (7, 101, 1, NOW - 20 * DAY, 0, 0),  # finished Arrival 20 days ago
    (7, 113, 0, NOW - 5 * DAY, 0, 30 * TICKS),  # half seen: not "unwatched"
    (7, 2011, 1, NOW - 5 * DAY, 0, 0),
    (7, 2012, 1, NOW - 4 * DAY, 0, 0),
    (7, 2013, 1, NOW - 3 * DAY, 0, 0),
    (7, 2021, 1, NOW - 200 * DAY, 0, 0),  # Old Show: too long ago to be in progress
    (7, 2051, 1, NOW - 10 * DAY, 0, 0),
    (7, 2052, 1, NOW - 10 * DAY, 0, 0),  # Caught Up: nothing next
    (7, 109, 0, -62135596800, 0, 0),  # year-1 placeholder, not played: still unwatched
    (3, 114, 1, NOW - 2 * DAY, 0, 0),
    (3, 101, 1, NOW - 9 * DAY, 0, 0),
    (3, 2011, 1, NOW - 9 * DAY, 0, 0),
] + [(7, 150 + i, 1, BULK_TS, 0, 0) for i in range(10)]  # marked played in bulk
PLAYS = [  # Playback Reporting: (DateCreated, UserId, ItemId, ItemType, PlayDuration)
    ("2026-09-20 20:00:00.0000000", OWNER.upper()[:8] + "-0000-0000-0000-" + "0" * 12, "101", "Movie", 6000),
    ("2026-10-05 20:00:00.0000000", OWNER, "2011", "Episode", 2700),
    ("2026-10-06 20:00:00.0000000", OWNER, "2012", "Episode", 2700),
    ("2026-10-07 20:00:00.0000000", OWNER, "2013", "Episode", 2700),
    ("2026-10-07 21:00:00.0000000", OWNER, "2052", "Episode", 60),  # too short to count
    ("2026-10-01 20:00:00.0000000", OTHER, "114", "Movie", 7000),
]


def build(d):
    os.makedirs(d, exist_ok=True)
    for f in ("library.db", "playback_reporting.db"):
        if os.path.exists(os.path.join(d, f)):
            os.remove(os.path.join(d, f))
    lib = sqlite3.connect(os.path.join(d, "library.db"))
    lib.execute(
        "CREATE TABLE MediaItems (Id INTEGER PRIMARY KEY AUTOINCREMENT, type INT NOT NULL, Name Text,"
        " ProductionYear INT, CommunityRating Float, RunTimeTicks BIGINT, ProviderIds Text, DateCreated INT,"
        " Genres Text, ExtraType INT, Path TEXT NULL, SeriesId INT, ParentIndexNumber INT, IndexNumber INT,"
        " UserDataKeyId INT NULL)"
    )
    lib.execute(
        "CREATE TABLE ItemLinks2 (ItemId INT NOT NULL, Type INT NOT NULL, LinkedId INT NOT NULL,"
        " LinkOrder INT NOT NULL, PRIMARY KEY (ItemId, Type, LinkedId)) WITHOUT ROWID"
    )
    lib.execute(
        "CREATE TABLE UserDatas (userId INT not null, rating float null, played bit not null, playCount int"
        " not null, isFavorite bit not null, playbackPositionTicks bigint not null, LastPlayedDateInt INT null,"
        " UserDataKeyId INT NULL)"
    )
    ins = "INSERT INTO MediaItems VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    for gid, name in GENRES.items():
        lib.execute(ins, (gid, 21, name, None, None, None, None, None, None, None, None, None, None, None, None))
    for i, name, yr, rt, mins, prov, added, gcol, extra, path, links in MOVIES:
        lib.execute(
            ins, (i, 5, name, yr, rt, mins * TICKS, prov, added, gcol, extra, path, None, None, None, 10000 + i)
        )
        for n, g in enumerate(links):
            lib.execute("INSERT INTO ItemLinks2 VALUES (?,2,?,?)", (i, g, n))
    for i, name, yr, rt, prov, links, eps in SERIES:
        lib.execute(
            ins,
            (
                i,
                6,
                name,
                yr,
                rt,
                45 * TICKS,
                prov,
                NOW - 100 * DAY,
                None,
                None,
                f"/tv/{name}",
                None,
                None,
                None,
                10000 + i,
            ),
        )
        for n, g in enumerate(links):
            lib.execute("INSERT INTO ItemLinks2 VALUES (?,2,?,?)", (i, g, n))
        for eid, s, e, mins, added in eps:
            lib.execute(
                ins,
                (
                    eid,
                    8,
                    f"Episode {e}" if e != 4 else "The *Fourth*",
                    None,
                    None,
                    mins * TICKS,
                    "",
                    added,
                    None,
                    None,
                    f"/tv/{name}/{s}x{e}.mkv",
                    i,
                    s,
                    e,
                    10000 + eid,
                ),
            )
    lib.execute(ins, (2017, 8, "Missing One", None, None, 45 * TICKS, "", NOW, None, None, None, 201, 2, 2, 12017))
    for u, item, played, ts, fav, pos in USERDATA:
        lib.execute(
            "INSERT INTO UserDatas VALUES (?,NULL,?,?,?,?,?,?)", (u, played, played, fav, pos, ts, 10000 + item)
        )
    lib.commit()
    lib.close()
    pr = sqlite3.connect(os.path.join(d, "playback_reporting.db"))
    pr.execute(
        "CREATE TABLE PlaybackActivity (DateCreated DATETIME NOT NULL, UserId TEXT, ItemId TEXT, ItemType TEXT,"
        " ItemName TEXT, PlaybackMethod TEXT, ClientName TEXT, DeviceName TEXT, PlayDuration INT, PauseDuration INT)"
    )
    pr.executemany("INSERT INTO PlaybackActivity VALUES (?,?,?,?,'x','DirectPlay','Emby Web','Firefox',?,0)", PLAYS)
    pr.commit()
    pr.close()


FORBIDDEN = re.compile(
    r"(^|[^A-Za-z0-9_])(attach|detach|pragma|vacuum|insert|update|delete|upsert|create|drop|alter|"
    r"reindex|analyze|begin|commit|rollback|savepoint|release|transaction|readfile|writefile|edit|"
    r"load_extension|fsdir|shell)([^A-Za-z0-9_]|$)",
    re.I,
)


def reject(msg):
    print(f"claude-bot-cmd: {msg}", file=sys.stderr)
    sys.exit(2)


def run_sql(d, log, db=None):
    sql = sys.stdin.read()
    with open(log, "a") as f:
        f.write(f"[{db or 'root'}] " + sql + "\n-- end\n")
    if db:  # the lockdown verb's checks
        norm = sql.strip().rstrip(";").strip()
        if not sql.isascii():
            reject("emby-sql: control or non-ASCII characters")
        if ";" in norm:
            reject("emby-sql: one statement only (no ';' chains)")
        if "--" in norm or "/*" in norm or "*/" in norm:
            reject("emby-sql: no comments")
        if not re.match(r"(?i)(select|with)\s", norm):
            reject("emby-sql: must be one SELECT or WITH query")
        if FORBIDDEN.search(norm):
            reject("emby-sql: forbidden keyword or function (read-only SELECT only)")
        stmts = [norm]
        path = os.path.join(d, {"library": "library.db", "playback": "playback_reporting.db"}[db])
    else:
        stmts = [x for x in sql.split(";\n") if x.strip()]
        path = os.path.join(d, "playback_reporting.db")
    con = sqlite3.connect(f"file:{path}?immutable=1", uri=True)
    out = []
    try:
        for stmt in stmts:
            for row in con.execute(stmt):
                out.append("\x1f".join("" if v is None else str(v) for v in row))
    except sqlite3.Error as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    sys.stdout.write("\x1e".join(out) + ("\x1e" if out else ""))


if __name__ == "__main__":
    if sys.argv[1] == "build":
        build(sys.argv[2])
    elif sys.argv[1] == "sql":
        run_sql(sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else None)
