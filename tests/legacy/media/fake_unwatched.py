#!/usr/bin/env python3
"""Fake Sonarr + Radarr + Seerr + Tracearr (v2 public API) for the `media unwatched` tests (offline, stdlib).

Usage: fake_unwatched.py PORT_FILE LOG_FILE
- the first path segment picks the service: /sonarr/api/v3/..., /radarr/..., /seerr/..., /tracearr/api/v2/public/...
- every request is logged as "METHOD /path?query"; anything but GET gets 405
- arr/Seerr want X-Api-Key, Tracearr wants "Authorization: Bearer TRACEARR-KEY-Tk55" (else 401)
- Tracearr lists are cursor-paginated with at most 2 rows per page (cursor = offset), to exercise the walk
Fake clock: Sat 2026-10-10 12:00 Zurich.
"""

import json
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT_FILE, LOG = sys.argv[1:3]
KEYS = {"seerr": "SEERR-KEY-Zq81", "radarr": "RADARR-KEY-Xw42", "sonarr": "SONARR-KEY-Vy73"}
TA_AUTH = "Bearer TRACEARR-KEY-Tk55"
GB = 10**9


def season(n, size, aired=None):
    st = {"episodeFileCount": 10, "sizeOnDisk": size}
    if aired:
        st["previousAiring"] = aired
    return {"seasonNumber": n, "statistics": st}


SERIES = [
    {
        "title": "Big Old Show",
        "year": 2004,
        "tvdbId": 700,
        "tmdbId": 7000,
        "status": "ended",
        "added": "2019-01-15T10:00:00Z",
        "path": "/data/tv/Big Old Show",
        "statistics": {"sizeOnDisk": 1200 * GB, "episodeFileCount": 80},
        "seasons": [season(n, 150 * GB) for n in range(1, 9)],
    },
    {
        "title": "Fresh Show",
        "year": 2024,
        "tvdbId": 800,
        "tmdbId": 8000,
        "status": "continuing",
        "added": "2024-03-01T10:00:00Z",
        "path": "/data/tv/Fresh Show",
        "statistics": {"sizeOnDisk": 50 * GB, "episodeFileCount": 20},
        "seasons": [season(1, 25 * GB), season(2, 25 * GB)],
    },
    {
        "title": "Ubice mog oca",
        "year": 2017,
        "tvdbId": 319183,
        "status": "ended",
        "added": "2022-01-01T10:00:00Z",
        "path": "/data/tv/Ubice mog oca (2017)",
        "alternateTitles": [{"title": "My Father's Killers", "seasonNumber": -1}],
        "statistics": {"sizeOnDisk": 300 * GB, "episodeFileCount": 30},
        "seasons": [season(1, 300 * GB)],
    },
    {
        "title": "Never Show",
        "year": 2020,
        "tvdbId": 900,
        "status": "ended",
        "added": "2026-01-05T10:00:00Z",
        "path": "/data/tv/Never Show",
        "statistics": {"sizeOnDisk": 500 * GB, "episodeFileCount": 50},
        "seasons": [season(1, 500 * GB)],
    },
    {
        "title": "Watched Big Show",
        "year": 2021,
        "tvdbId": 770,
        "status": "continuing",
        "added": "2021-01-01T10:00:00Z",
        "path": "/data/tv/Watched Big Show",
        "statistics": {"sizeOnDisk": 400 * GB, "episodeFileCount": 40},
        "seasons": [
            season(0, 10 * GB),
            season(1, 150 * GB),
            season(2, 150 * GB),
            season(3, 90 * GB, "2026-09-01T20:00:00Z"),
        ],
    },
    {"title": "Empty Show", "tvdbId": 901, "statistics": {"sizeOnDisk": 0}, "seasons": []},
]


def movie(title, year, tmdb, gb, filed, path=None, **kw):
    m = {
        "title": title,
        "year": year,
        "tmdbId": tmdb,
        "sizeOnDisk": gb * GB,
        "added": "2015-01-01T10:00:00Z",
        "movieFile": {"dateAdded": filed},
        "path": path or f"/data/movies/{title} ({year})",
    }
    m.update(kw)
    return m


MOVIES = [
    movie("Huge Remux", 2002, 41, 80, "2019-06-01T10:00:00Z"),
    movie("New Big", 2026, 42, 30, "2026-09-02T10:00:00Z"),
    movie("Small Old", 1999, 44, 2, "2015-01-01T10:00:00Z"),
    movie("Dune", 1984, 841, 20, "2020-01-01T10:00:00Z"),
    movie("Dune", 2021, 438631, 40, "2022-01-01T10:00:00Z", alternateTitles=[{"title": "Dune: Part One"}]),
    movie("Id Movie", 2010, 51, 10, "2020-01-01T10:00:00Z"),
    movie("Emby Id Film", 2015, 52, 8, "2020-01-01T10:00:00Z"),
    movie("Short One", 2018, 61, 5, "2020-01-01T10:00:00Z", imdbId="tt0000061"),
    movie("No File", 2027, 43, 0, None),
]
REQUESTS = [
    {
        "type": "tv",
        "createdAt": "2021-01-01T00:00:00Z",
        "requestedBy": {"displayName": "PersonH"},
        "media": {"tmdbId": 7000, "tvdbId": 700},
    },
    {
        "type": "tv",
        "createdAt": "2019-01-01T00:00:00Z",
        "requestedBy": {"displayName": "PersonG"},
        "media": {"tmdbId": 7000, "tvdbId": 700},
    },
    {
        "type": "movie",
        "createdAt": "2020-01-01T00:00:00Z",
        "requestedBy": {"displayName": "Timon Muster"},
        "media": {"tmdbId": 41},
    },
]
U1X, U2X = "aaaa1111-0000-0000-0000-000000000000", "bbbb2222-0000-0000-0000-000000000000"
TA_USERS = [
    {"id": "id-1", "username": "u1", "accounts": [{"server_user_id": "su-1", "external_user_id": U1X}]},
    {"id": "id-2", "username": "u2", "accounts": [{"server_user_id": "su-2", "external_user_id": U2X}]},
    {"id": "id-3", "username": "u3", "accounts": [{"server_user_id": "su-3", "external_user_id": None}]},
]
TA_SHOWS = [
    {"media_id": "sm-800", "media_type": "show", "title": "Fresh Show", "year": 2024, "tvdb_id": 800, "tmdb_id": 8000},
    {"media_id": "sm-770", "media_type": "show", "title": "Watched Big Show", "year": 2021, "tvdb_id": 770},
]


def play(pid, mt, start, su, **kw):
    p = {
        "id": pid,
        "media_type": mt,
        "started_at": start,
        "user": {"id": "id-" + su[-1], "server_user_id": su},
        "media_id": "m-" + pid,
        "tmdb_id": None,
        "tvdb_id": None,
        "imdb_id": None,
        "rating_key": None,
    }
    p.update(kw)
    return p


TA_HISTORY = [  # newest first, like the real API
    play(
        "p1",
        "episode",
        "2026-10-01T19:00:00Z",
        "su-1",
        show_media_id="sm-800",
        show_title="Fresh Show",
        season_number=2,
        episode_number=1,
        tvdb_id=99999,
    ),  # episode tvdb id: must not be used for the show
    play(
        "p2",
        "episode",
        "2026-09-20T19:00:00Z",
        "su-1",
        show_media_id="sm-770",
        show_title="Watched Big Show",
        season_number=2,
        episode_number=3,
        grandparent_rating_key="7700",
    ),
    play("p3", "movie", "2026-09-15T19:00:00Z", "su-2", media_title="Small Old", year=1999, tmdb_id=44),
    play(
        "p4", "movie", "2026-09-10T19:00:00Z", "su-3", media_title="Some Other Name", rating_key="5002"
    ),  # Emby id only
    play("p5", "track", "2026-09-05T19:00:00Z", "su-1", media_title="A Song"),
    play(
        "p6",
        "episode",
        "2026-08-25T10:54:45Z",
        "su-1",
        show_media_id="sm-800",
        show_title="Fresh Show",
        season_number=1,
        episode_number=1,
    ),  # Tracearr's first record
]


def ta_page(rows, qs):
    size = min(int(qs.get("pageSize", ["25"])[0]), 2)
    off = int(qs.get("cursor", ["0"])[0])
    nxt = off + size if off + size < len(rows) else None
    return {
        "data": rows[off : off + size],
        "meta": {"nextCursor": str(nxt) if nxt is not None else None, "pageSize": size},
    }


def route(svc, path, qs):
    if svc == "sonarr" and path == "/api/v3/series":
        return SERIES
    if svc == "radarr" and path == "/api/v3/movie":
        return MOVIES
    if svc == "seerr" and path == "/api/v1/request":
        take, skip = int(qs.get("take", ["10"])[0]), int(qs.get("skip", ["0"])[0])
        return {"pageInfo": {"results": len(REQUESTS), "pageSize": take}, "results": REQUESTS[skip : skip + take]}
    if svc == "tracearr":
        if path == "/api/v2/public/history":
            return ta_page(TA_HISTORY, qs)
        if path == "/api/v2/public/watched-media" and qs.get("media_type") == ["show"]:
            return ta_page(TA_SHOWS, qs)
        if path == "/api/v2/public/users":
            return ta_page(TA_USERS, qs)
    return None


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def handle_any(self):
        with open(LOG, "a") as f:
            f.write(f"{self.command} {self.path}\n")
        u = urllib.parse.urlsplit(self.path)
        parts = u.path.split("/", 2)
        svc, path = parts[1], "/" + (parts[2] if len(parts) > 2 else "")
        ok = (
            (self.headers.get("Authorization") == TA_AUTH)
            if svc == "tracearr"
            else (self.headers.get("X-Api-Key") == KEYS.get(svc))
        )
        if not ok:
            return self._send(401, {"error": "Unauthorized"})
        if self.command != "GET":
            return self._send(405, {"error": "fake server is read-only"})
        body = route(svc, path, urllib.parse.parse_qs(u.query))
        if body is None:
            return self._send(404, {"message": "NotFound"})
        self._send(200, body)

    do_GET = do_POST = do_PUT = do_DELETE = handle_any


srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
with open(PORT_FILE, "w") as f:
    f.write(str(srv.server_address[1]))
srv.serve_forever()
