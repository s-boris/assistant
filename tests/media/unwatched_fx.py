#!/usr/bin/env python3
"""Emby fixtures for the `media unwatched` tests (stdlib only).

  unwatched_fx.py build DIR          write DIR/playback_reporting.db + DIR/library.db (Playback Reporting + Emby library)
  unwatched_fx.py sql DIR LOG        fake `sqlite3 -readonly -batch -ascii`: SQL on stdin (logged to LOG), run against
                                     file:DIR/playback_reporting.db?immutable=1, rows out in ascii mode (0x1F / 0x1E)

Fake clock: Sat 2026-10-10 12:00 Zurich. Users are 32-hex Emby ids; U1 is stored uppercase with dashes once to check
the normalization. Tracearr's first record is 2026-08-25, so Emby rows from that day on must be ignored.
"""  # noqa: E501

import os
import sqlite3
import sys

U1, U2, U3, U5 = "aaaa1111" + "0" * 24, "bbbb2222" + "0" * 24, "cccc3333" + "0" * 24, "eeee5555" + "0" * 24
U1_DASHED = "AAAA1111-0000-0000-0000-000000000000"
COLS = "DateCreated, UserId, ItemId, ItemType, ItemName, PlaybackMethod, ClientName, DeviceName, PlayDuration, PauseDuration"  # noqa: E501

CURRENT = [  # (DateCreated, UserId, ItemId, ItemType, ItemName, PlayDuration seconds)
    ("2024-03-01 20:00:00.1234567", U1_DASHED, "9001", "Episode", "Big Old Show - s03e04 - Foo", 1500),
    ("2024-03-01 21:00:00.0000000", U1, "9001", "Episode", "Big Old Show - s03e04 - Foo", 30),  # same day: one play
    ("2026-03-01 19:00:00.0000000", U2, "9002", "Episode", "Убице мог оца - s01e02 - Bar", 1200),  # Emby's name
    ("2025-01-10 18:00:00.0000000", U1, "7701", "Episode", "Watched Big Show - s01e01 - A", 2400),  # Emby id known
    ("1970-01-01 01:07:13.9790607", U1, "9003", "Movie", "Huge Remux", 1000),  # bogus date
    ("2026-08-01 20:00:00.0000000", U3, "9004", "Movie", "Dune", 5000),  # two Dunes
    ("2023-06-01 20:00:00.0000000", U1, "5001", "Movie", "Ид Филм", 5000),  # by Emby id
    ("2026-09-15 20:00:00.0000000", U2, "9005", "Movie", "Small Old", 3000),  # after Tracearr's start: ignored
    ("2025-05-05 20:00:00.0000000", U1, "9006", "Movie", "Short One", 60),  # < 2 min: not a play
    ("2026-02-02 20:00:00.0000000", U1, "9007", "Episode", "Gone Show - s01e01 - X", 900),  # not in Sonarr
    ("2026-02-03 20:00:00.0000000", U1, "9008", "TvChannel", "SRF 1", 5000),
]
BACKUP = [  # the plugin's dated backup table
    ("2021-02-02 20:00:00.0000000", U5, "8101", "Episode", "Big Old Show - s01e01 - Pilot", 2000),
    ("2020-02-25 21:53:30.4292029", U5, "8102", "Episode", "Gone Show - s01e02 - Y", 1300),  # history start
]
LIBRARY = [  # (Id, type, Name, OriginalTitle, ProductionYear, ProviderIds, Path, SeriesId, ParentIndexNumber)
    (7319, 6, "Убице мог оца", "Ubice mog oca", 2017, "Tvdb=319183|IMDB=tt5907796", "/tv/Ubice mog oca", None, None),
    (7700, 6, "Watched Big Show", None, 2021, "Tvdb=770", "/tv/Watched Big Show", None, None),
    (7701, 8, "A", None, 2021, "Tvdb=1", "/tv/Watched Big Show/Season 1/e1.mkv", 7700, 1),
    (8000, 6, "Big Old Show", None, 2004, "Tvdb=700|Tmdb=7000", "/tv/Big Old Show", None, None),
    (5001, 5, "Ид Филм", None, 2010, "Tmdb=51", "/movies/Id Movie (2010)/Id Movie (2010).mkv", None, None),
    (5002, 5, "Emby Id Film", None, 2015, "", "/movies/Emby Id Film (2015)/film.mkv", None, None),
]


def build(d):
    os.makedirs(d, exist_ok=True)
    for f in ("playback_reporting.db", "library.db"):
        if os.path.exists(os.path.join(d, f)):
            os.remove(os.path.join(d, f))
    pr = sqlite3.connect(os.path.join(d, "playback_reporting.db"))
    for t, extra in (
        ("PlaybackActivity", ", RemoteAddress TEXT, TranscodeReasons TEXT"),
        ("PlaybackActivity_20230425_040144", ""),
    ):
        pr.execute(
            f"CREATE TABLE {t} (DateCreated DATETIME NOT NULL, UserId TEXT, ItemId TEXT, ItemType TEXT, ItemName TEXT,"
            f" PlaybackMethod TEXT, ClientName TEXT, DeviceName TEXT, PlayDuration INT, PauseDuration INT{extra})"
        )
    pr.execute("CREATE TABLE UserList (UserId TEXT)")
    for t, rows in (("PlaybackActivity", CURRENT), ("PlaybackActivity_20230425_040144", BACKUP)):
        pr.executemany(f"INSERT INTO {t} ({COLS}) VALUES (?,?,?,?,?,'DirectPlay','Emby Web','Firefox',?,0)", rows)
    pr.commit()
    pr.close()
    lib = sqlite3.connect(os.path.join(d, "library.db"))
    lib.execute(
        "CREATE TABLE MediaItems (Id INTEGER PRIMARY KEY AUTOINCREMENT, type INT NOT NULL, Name Text, OriginalTitle Text,"  # noqa: E501
        " ProductionYear INT, ProviderIds Text, Path TEXT NULL, SeriesId INT, ParentIndexNumber INT)"
    )
    lib.executemany("INSERT INTO MediaItems VALUES (?,?,?,?,?,?,?,?,?)", LIBRARY)
    lib.commit()
    lib.close()


def run_sql(d, log):
    sql = sys.stdin.read()
    with open(log, "a") as f:
        f.write(sql + "\n-- end\n")
    con = sqlite3.connect(f"file:{d}/playback_reporting.db?immutable=1", uri=True)
    out = []
    try:
        for stmt in sql.split(";\n"):
            if stmt.strip():
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
        run_sql(sys.argv[2], sys.argv[3])
