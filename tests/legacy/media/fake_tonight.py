#!/usr/bin/env python3
"""Fake Seerr for the `media tonight` tests (offline, stdlib).

Usage: fake_tonight.py PORT_FILE LOG_FILE
- paths start with /seerr (the config's Seerr url); every request is logged as "METHOD /path?query"
- X-Api-Key must be SEERR-KEY-Zq81 (else 401); anything but GET gets 405
- users and requests are paged by take/skip like Seerr; discover/movies filters only by its genre and page parameters (the
  runtime limit is left to the client's details check on purpose)
"""  # noqa: E501

import json
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT_FILE, LOG = sys.argv[1:3]
KEY = "SEERR-KEY-Zq81"
URL = "https://emby.example.test/web/index.html#!/item?id=STALE9&context=home&serverId=srv1"
USERS = [
    {"id": 1, "displayName": "Robin", "jellyfinUserId": "AAAA1111-0000-0000-0000-000000000000"},
    {"id": 2, "displayName": "Friend", "jellyfinUserId": "bbbb2222" + "0" * 24},
]
REQUESTS = [
    {"type": "movie", "status": 2, "media": {"tmdbId": 5550, "status": 5, "mediaType": "movie"}},
    {"type": "movie", "status": 2, "media": {"tmdbId": 9999, "status": 3, "mediaType": "movie"}},
    {"type": "movie", "status": 2, "media": {"tmdbId": 123, "status": 5, "mediaType": "movie"}},
]
GENRES = [
    {"id": 878, "name": "Science Fiction"},
    {"id": 35, "name": "Comedy"},
    {"id": 18, "name": "Drama"},
    {"id": 27, "name": "Horror"},
    {"id": 10751, "name": "Family"},
    {"id": 16, "name": "Animation"},
    {"id": 53, "name": "Thriller"},
    {"id": 99, "name": "Documentary"},
    {"id": 12, "name": "Adventure"},
    {"id": 80, "name": "Crime"},
]


def mv(i, title, rel, genres, vote, count, runtime, status=None):
    d = {
        "id": i,
        "mediaType": "movie",
        "title": title,
        "releaseDate": rel,
        "genreIds": genres,
        "voteAverage": vote,
        "voteCount": count,
        "runtime": runtime,
    }
    if status:
        d["mediaInfo"] = {"status": status, "requests": []}
    return d


DISCOVER = [
    mv(7001, "Nova Drift", "2025-03-01", [878, 18], 8.4, 900, 118),
    mv(7002, "Tight Thriller", "2024-05-01", [878, 53], 7.0, 400, 95),
    mv(7003, "Already Here", "2024-01-01", [878], 8.5, 900, 100, status=5),
    mv(7004, "Asked For", "2024-01-01", [878], 8.4, 900, 100, status=3),
    mv(7005, "Kiddo Space", "2024-01-01", [878, 10751], 8.3, 900, 90),
    mv(7006, "Future Film", "2027-01-01", [878], 8.0, 900, 100),
    mv(7007, "Few Votes", "2024-01-01", [878], 9.0, 10, 100),
    mv(7008, "Laugh Riot", "2025-06-01", [35], 7.6, 700, 101),
    mv(7009, "Fright Fest", "2024-02-02", [27], 7.0, 300, 92),
    mv(329865, "Arrival", "2016-11-10", [878, 18], 7.9, 9000, 116),  # in the library; Seerr doesn't know
    mv(7011, "Tiny Short", "2025-01-01", [878], 8.1, 500, 15),
    mv(7013, "Docs Here", "2024-03-01", [99], 7.5, 400, 90, status=5),
    mv(7012, "Deep Docs", "2025-04-01", [99], 7.8, 400, 95),  # discover page 2 (page 1 is all on the server)
]
PAGE = {7012: 2}
VOTES = {120: (10.0, 2), 121: (9.1, 4), 122: (7.9, 800), 123: (5.9, 300), 124: (0.0, 0)}  # TMDB (average, count)
TRENDING = [
    {
        "id": 7101,
        "mediaType": "tv",
        "name": "Trend Show",
        "firstAirDate": "2026-01-01",
        "genreIds": [18],
        "voteAverage": 8.0,
        "voteCount": 500,
    },
    mv(7102, "Trend Wave", "2026-09-01", [18], 6.6, 200, 110),
    mv(329865, "Arrival", "2016-11-10", [878, 18], 7.9, 9000, 116, status=5),
]
LIB_TMDB = {5550, 693134, 329865, 104, 105, 107, 108, 109, 110, 113, 114, 120, 121, 122, 123, 124}
TV_TMDB = {2010, 2020, 2030, 2040, 2050, 2060}


def paged(items, qs):
    take, skip = int(qs.get("take", ["20"])[0]), int(qs.get("skip", ["0"])[0])
    return {"pageInfo": {"results": len(items)}, "results": items[skip : skip + take]}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_any(self):
        with open(LOG, "a") as f:
            f.write(f"{self.command} {self.path}\n")
        if self.command != "GET":
            return self.send(405, {"message": "read-only fake"})
        if self.headers.get("X-Api-Key") != KEY:
            return self.send(401, {"message": "bad key"})
        u = urllib.parse.urlparse(self.path)
        p, qs = u.path, urllib.parse.parse_qs(u.query)
        if not p.startswith("/seerr/api/v1/"):
            return self.send(404, {"message": "not found"})
        p = p[len("/seerr/api/v1") :]
        if p == "/user":
            return self.send(200, paged(USERS, qs))
        if p == "/user/1/requests":
            return self.send(200, paged(REQUESTS, qs))
        if p == "/user/2/requests":
            return self.send(200, paged([], qs))
        if p == "/genres/movie":
            return self.send(200, GENRES)
        if p == "/discover/movies":
            g, page = int(qs.get("genre", ["0"])[0]), int(qs.get("page", ["1"])[0])
            hits = [d for d in DISCOVER if g in d["genreIds"]]
            return self.send(
                200,
                {
                    "page": page,
                    "totalPages": max([PAGE.get(d["id"], 1) for d in hits] or [1]),
                    "results": [d for d in hits if PAGE.get(d["id"], 1) == page],
                },
            )
        if p == "/discover/trending":
            return self.send(200, {"page": 1, "totalPages": 1, "results": TRENDING})
        if p.startswith("/movie/"):
            i = int(p.split("/")[2])
            if i in LIB_TMDB or i == 5550:
                d = {
                    "id": i,
                    "title": f"Library {i}",
                    "releaseDate": "2020-01-01",
                    "runtime": 100,
                    "mediaInfo": {"status": 5, "mediaUrl": URL, "requests": []},
                }
                if i in VOTES:
                    d["voteAverage"], d["voteCount"] = VOTES[i]
                return self.send(200, d)
            for d in DISCOVER + TRENDING:
                if d["id"] == i and d["mediaType"] == "movie":
                    return self.send(200, {k: v for k, v in d.items() if k != "genreIds"})
            return self.send(404, {"message": "no such movie"})
        if p.startswith("/tv/"):
            i = int(p.split("/")[2])
            if i in TV_TMDB:
                return self.send(200, {"id": i, "name": f"Show {i}", "mediaInfo": {"status": 5, "mediaUrl": URL}})
            return self.send(404, {"message": "no such show"})
        return self.send(404, {"message": "not found"})

    do_GET = do_POST = do_PUT = do_DELETE = handle_any


srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
with open(PORT_FILE + ".tmp", "w") as f:
    f.write(str(srv.server_address[1]))
import os  # noqa: E402

os.replace(PORT_FILE + ".tmp", PORT_FILE)
srv.serve_forever()
