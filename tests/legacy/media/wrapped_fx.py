"""Fixture for `media wrapped` (fictional data): `build DIR` writes library.db + playback_reporting.db (his plays,
a friend's plays, a backup table, replaced item ids, an anomaly row); `serve PORTFILE LOG` = fake Seerr users +
Emby cover endpoint (logs "METHOD path token=yes|no"). SQL runs through tonight_fx.py's verb emulation."""

import http.server
import json
import os
import re
import sqlite3
import struct
import sys
import zlib

TICKS = 600000000
ME = "AAAA1111000000000000000000000000"
FRIEND = "bbbb2222" + "0" * 24
GENRES = {901: "Science Fiction", 902: "Comedy", 903: "Drama"}
# (Id, type, Name, year, minutes, Width, Height, SeriesId, season, episode, Genres column, genre links)
ITEMS = (
    [
        (101, 5, "Star Voyage", 2016, 120, 3840, 1600, None, None, None, None, [901]),
        (102, 5, "Old Western", 1968, 100, 1920, 1080, None, None, None, "Western", []),
        (103, 5, "Long Epic", 2024, 200, 3840, 2160, None, None, None, None, [903]),
        (104, 5, "Friend Only Film", 2020, 100, 1920, 1080, None, None, None, None, [902]),
        (105, 5, "Renamed Copy", 2019, 90, 1920, 1080, None, None, None, None, [902]),
        (201, 6, "Nova Drift", 2022, None, None, None, None, None, None, None, [901, 903]),
        (202, 6, "Laugh Track", 2010, None, None, None, None, None, None, None, [902]),
    ]
    + [(2010 + e, 8, f"Drift {e}", None, 45, 1920, 1080, 201, 1, e, None, []) for e in range(1, 7)]
    + [
        (2021, 8, "Pilot", None, 22, 3840, 2160, 202, 1, 1, None, []),
        (2022, 8, "Second", None, 22, 3840, 2160, 202, 1, 2, None, []),
        (2023, 8, "Pilot Again", None, 22, 3840, 2160, 202, 1, 3, None, []),
    ]
)
# (DateCreated local, user, ItemId, ItemType, ItemName, seconds, client)
PLAYS = (
    [
        ("2026-01-01 02:00:00.0000000", ME, "102", "Movie", "Old Western", 6000, "Emby Web"),  # viewing day 31.12.2025
        ("2026-01-03 20:00:00.1234567", ME, "101", "Movie", "Star Voyage", 7200, "Emby for LG"),
        ("2026-02-14 21:00:00.0000000", ME, "101", "Movie", "Star Voyage", 7000, "Emby for LG"),
        ("2026-03-01 20:00:00.0000000", ME, "101", "Movie", "Star Voyage", 600, "Emby for LG"),  # partial: no rewatch
    ]
    + [
        (
            f"2026-03-07 {t}:00.0000000",
            ME.lower(),
            str(2010 + e),
            "Episode",
            f"Nova Drift - s01e0{e} - Drift {e}",
            2700,
            "Emby for LG",
        )
        for e, t in zip(range(1, 7), ("19:00", "19:45", "20:30", "21:15", "22:00", "22:45"), strict=False)
    ]
    + [
        (
            "2026-03-08 00:10:00.0000000",
            ME,
            "2021",
            "Episode",
            "Laugh Track - s01e01 - Pilot",
            1320,
            "Emby for Android",
        ),
        ("2026-04-05 20:00:00.0000000", ME, "103", "Movie", "Long Epic", 6000, "Emby for LG"),
        ("2026-04-05 21:50:00.0000000", ME, "103", "Movie", "Long Epic", 6000, "Emby for LG"),
        ("2026-05-10 20:30:00.0000000", ME, "2022", "Episode", "Laugh Track - s01e02 - Second", 1300, "Emby for LG"),
        ("2026-06-01 20:00:00.0000000", ME, "999", "Movie", "Renamed Copy", 5400, "Emby for LG"),  # replaced id
        (
            "2026-06-02 20:00:00.0000000",
            ME,
            "998",
            "Episode",
            "Laugh Track - s01e03 - Pilot Again",
            1300,
            "Emby for LG",
        ),
        ("2026-07-04 20:00:00.0000000", ME, "102", "Movie", "Old Western", 30, "Emby for LG"),  # < 2 min
        ("2026-08-01 20:00:00.0000000", ME, "102", "Movie", "Old Western", 100000, "Emby for LG"),  # stuck session
        ("2026-09-20 20:00:00.0000000", ME, "997", "Movie", "Vanished Film", 3060, "Emby for LG"),  # gone, no match
        ("2026-09-21 20:00:00.0000000", ME, "500", "TvChannel", "Some Channel", 9000, "Emby for LG"),
        ("2026-03-07 20:00:00.0000000", FRIEND, "104", "Movie", "Friend Only Film", 7200, "Emby Web"),
        ("2026-03-08 20:00:00.0000000", FRIEND, "2021", "Episode", "Laugh Track - s01e01 - Pilot", 1320, "Emby Web"),
    ]
)


def build(d):
    os.makedirs(d, exist_ok=True)
    for f in ("library.db", "playback_reporting.db"):
        if os.path.exists(os.path.join(d, f)):
            os.remove(os.path.join(d, f))
    lib = sqlite3.connect(os.path.join(d, "library.db"))
    lib.execute(
        "CREATE TABLE MediaItems (Id INTEGER PRIMARY KEY, type INT NOT NULL, Name Text, ProductionYear INT,"
        " RunTimeTicks BIGINT, Width INT, Height INT, SeriesId INT, ParentIndexNumber INT, IndexNumber INT,"
        " Genres Text, ExtraType INT, Path TEXT NULL)"
    )
    lib.execute(
        "CREATE TABLE ItemLinks2 (ItemId INT NOT NULL, Type INT NOT NULL, LinkedId INT NOT NULL,"
        " LinkOrder INT NOT NULL, PRIMARY KEY (ItemId, Type, LinkedId)) WITHOUT ROWID"
    )
    for gid, name in GENRES.items():
        lib.execute("INSERT INTO MediaItems (Id, type, Name) VALUES (?,21,?)", (gid, name))
    for i, t, name, yr, mins, w, h, sid, s, e, gcol, links in ITEMS:
        lib.execute(
            "INSERT INTO MediaItems VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,?)",
            (i, t, name, yr, mins * TICKS if mins else None, w, h, sid, s, e, gcol, f"/m/{i}"),
        )
        for n, g in enumerate(links):
            lib.execute("INSERT INTO ItemLinks2 VALUES (?,2,?,?)", (i, g, n))
    lib.commit()
    lib.close()
    pr = sqlite3.connect(os.path.join(d, "playback_reporting.db"))
    for t in ("PlaybackActivity", "PlaybackActivity_20260101_040000"):
        pr.execute(
            f"CREATE TABLE {t} (DateCreated DATETIME NOT NULL, UserId TEXT, ItemId TEXT, ItemType TEXT,"
            " ItemName TEXT, PlaybackMethod TEXT, ClientName TEXT, DeviceName TEXT, PlayDuration INT,"
            " PauseDuration INT, RemoteAddress TEXT, TranscodeReasons TEXT)"
        )
    rows = [(dt, u, i, ty, n, c, s) for (dt, u, i, ty, n, s, c) in PLAYS]
    pr.executemany("INSERT INTO PlaybackActivity VALUES (?,?,?,?,?,'DirectPlay',?,'Dev',?,0,'','')", rows)
    pr.executemany(
        "INSERT INTO PlaybackActivity_20260101_040000 VALUES (?,?,?,?,?,'DirectPlay',?,'Dev',?,0,'','')", rows[:2]
    )  # backup copy: duplicates must not count twice
    pr.commit()
    pr.close()


def png(rgb):
    raw = b"\x00" + bytes(rgb) * 2
    raw = raw + b"\x00" + bytes(rgb) * 2

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        with open(self.server.log, "a") as f:
            f.write(f"GET {self.path} token={'yes' if self.headers.get('X-Emby-Token') == 'EMBY-KEY-Wr55' else 'no'}\n")
        p = self.path.split("?")[0]
        if p == "/seerr/api/v1/user":
            users = [
                {"id": 1, "displayName": "Robin", "jellyfinUserId": "aaaa1111-0000-0000-0000-000000000000"},
                {"id": 2, "displayName": "Friend", "jellyfinUserId": FRIEND},
            ]
            return self.send(200, json.dumps({"pageInfo": {"results": 2}, "results": users}).encode())
        m = re.fullmatch(r"/emby/Items/(\d+)/Images/Primary", p)
        if m:
            if self.headers.get("X-Emby-Token") != "EMBY-KEY-Wr55":
                return self.send(401, b"{}")
            if m.group(1) == "202":
                return self.send(404, b"not found", "text/plain")
            if m.group(1) == "103":
                return self.send(200, b"<html>login</html>", "text/html")
            return self.send(200, png((int(m.group(1)) % 256, 80, 160)), "image/png")
        self.send(404, b"{}")

    def do_POST(self):
        with open(self.server.log, "a") as f:
            f.write(f"POST {self.path}\n")
        self.send(405, b"{}")


def serve(portfile, log):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.log = log
    with open(portfile + ".tmp", "w") as f:
        f.write(str(srv.server_address[1]))
    os.replace(portfile + ".tmp", portfile)
    srv.serve_forever()


if __name__ == "__main__":
    if sys.argv[1] == "build":
        build(sys.argv[2])
    elif sys.argv[1] == "serve":
        serve(sys.argv[2], sys.argv[3])
