#!/usr/bin/env python3
"""Fake Seerr + Radarr + Sonarr for ~/bin/media tests (offline, stdlib).

One HTTP server; the first path segment picks the service: /seerr/api/v1/..., /radarr/api/v3/..., /sonarr/api/v3/...
Usage: fake_arr.py PORT_FILE LOG_FILE SCENARIO_FILE [READY_FILE]
- every request is logged as "METHOD /path?query" (the tests assert GET-only)
- a wrong/missing X-Api-Key gives 401
- SCENARIO_FILE is re-read per request: ok | unhealthy | notes | slow | http500 | badjson
Fixture data is built around the fake clock 2026-10-10 12:00 Zurich (Sat).
READY_FILE (optional, re-read per request; written by ready_fx.py) backs the ready-watch endpoints:
Seerr /api/v1/user, /api/v1/user/ID/requests, /api/v1/movie/ID and Sonarr /api/v3/history/since.
"""

import json
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT_FILE, LOG, SCEN = sys.argv[1:4]
READY = sys.argv[4] if len(sys.argv) > 4 else None
KEYS = {"seerr": "SEERR-KEY-Zq81", "radarr": "RADARR-KEY-Xw42", "sonarr": "SONARR-KEY-Vy73"}


def ep(sid, title, s, e, utc, name="", has=False):
    return {
        "seriesId": sid,
        "seasonNumber": s,
        "episodeNumber": e,
        "title": name,
        "airDateUtc": utc,
        "airDate": utc[:10],
        "hasFile": has,
        "monitored": True,
        "id": sid * 1000 + s * 100 + e,
        "series": {"id": sid, "title": title, "qualityProfileId": 6},
    }


CAL = [
    ep(1, "Night Show", 52, 3, "2026-10-11T03:30:00Z", "Host / Band"),  # Sun 05:30 CEST
    ep(2, "Avatar: Seven Havens", 1, 4, "2026-10-16T07:00:00Z", "The Faceless"),  # Fri 09:00, collapsed
    ep(2, "Avatar: Seven Havens", 1, 5, "2026-10-16T07:23:00Z", "Left Behind"),
    ep(2, "Avatar: Seven Havens", 1, 6, "2026-10-16T07:46:00Z", "Night"),
    ep(3, "Slow Horses", 6, 5, "2026-10-10T04:00:00Z", "Sayonara", True),  # today 06:00, on disk
    ep(4, "Daily Shop", 2026, 2911, "2026-10-12T13:00:00Z", "TBA"),  # Mon 15:00, TBA hidden
    ep(5, "Late Show", 1, 1, "2026-10-24T19:00:00Z", "Before DST"),  # Sat 24.10 21:00 CEST
    ep(5, "Late Show", 1, 2, "2026-10-26T19:00:00Z", "After DST"),  # Mon 26.10 20:00 CET
]
RCAL = [
    {
        "title": "Tuner",
        "year": 2026,
        "tmdbId": 11,
        "hasFile": True,
        "inCinemas": "2026-05-21T00:00:00Z",
        "digitalRelease": "2026-06-01T00:00:00Z",
        "physicalRelease": "2026-10-16T00:00:00Z",
    },
    {
        "title": "Hope",
        "year": 2026,
        "tmdbId": 12,
        "hasFile": False,
        "inCinemas": "2026-07-15T00:00:00Z",
        "digitalRelease": "2026-10-13T00:00:00Z",
    },
    {"title": "Clayface", "year": 2026, "tmdbId": 13, "hasFile": False, "inCinemas": "2026-10-12T00:00:00Z"},
    {
        "title": "Old News",
        "year": 2025,
        "tmdbId": 14,
        "hasFile": True,
        "digitalRelease": "2026-01-01T00:00:00Z",
        "physicalRelease": "2026-12-01T00:00:00Z",
    },
]
S_MISSING = [ep(4, "Daily Shop", 2026, n, f"2026-09-{n - 2880:02d}T13:00:00Z") for n in range(2901, 2906)] + [
    ep(6, "Top Gear", 3, 1, "1980-01-01T20:00:00Z"),
    ep(6, "Top Gear", 4, 2, "1981-01-01T20:00:00Z"),
]
R_MISSING = [
    {"title": "Stuck Doc", "year": 2023, "tmdbId": 21, "status": "released", "added": "2024-02-01T10:00:00Z"},
    {"title": "Recent Doc", "year": 2025, "tmdbId": 22, "status": "released", "added": "2025-11-01T10:00:00Z"},
    {
        "title": "Verity",
        "year": 2026,
        "tmdbId": 23,
        "status": "inCinemas",
        "added": "2026-05-03T10:00:00Z",
        "inCinemas": "2026-09-30T00:00:00Z",
        "digitalRelease": "2026-10-27T00:00:00Z",
    },
    {
        "title": "Wildwood",
        "year": 2026,
        "tmdbId": 24,
        "status": "announced",
        "added": "2026-05-18T10:00:00Z",
        "inCinemas": "2026-10-22T00:00:00Z",
    },
    {"title": "Ghost Film", "year": 2027, "tmdbId": 25, "status": "announced", "added": "2026-06-01T10:00:00Z"},
]
S_CUTOFF = [ep(7, "", 1, n, "2015-01-01T20:00:00Z", has=True) for n in range(1, 4)] + [
    ep(8, "", 2, 1, "2016-01-01T20:00:00Z", has=True)
]
for r in S_CUTOFF:
    r.pop("series")  # cutoff is fetched without includeSeries: titles come from /series
R_CUTOFF = [
    {
        "title": "Startup.com",
        "year": 2001,
        "tmdbId": 31,
        "qualityProfileId": 1,
        "sizeOnDisk": 734662656,
        "statistics": {"sizeOnDisk": 734662656, "movieFileQualities": [{"id": 2, "name": "DVD"}]},
    },
    {
        "title": "Another Film",
        "year": 2004,
        "tmdbId": 32,
        "qualityProfileId": 1,
        "movieFile": {"quality": {"quality": {"id": 23, "name": "DVD-R"}}},
    },
]
S_PROFILES = [
    {
        "id": 6,
        "name": "Up to 1080p",
        "cutoff": 1003,
        "items": [
            {"quality": {"id": 1, "name": "SDTV"}, "items": []},
            {"id": 1003, "name": "WEB 1080p", "items": [{"quality": {"id": 3, "name": "WEBDL-1080p"}}]},
        ],
    },
    {
        "id": 9,
        "name": "Up to 4K",
        "cutoff": 19,
        "items": [{"id": 1004, "name": "WEB 2160p", "items": [{"quality": {"id": 19, "name": "Bluray-2160p"}}]}],
    },
]
R_PROFILES = [{"id": 1, "name": "Default QP", "cutoff": 7, "items": [{"quality": {"id": 7, "name": "Bluray-1080p"}}]}]
GB = 10**9
SERIES = [
    {
        "id": 7,
        "title": "Big Old Show",
        "year": 2004,
        "tvdbId": 700,
        "tmdbId": 7000,
        "qualityProfileId": 6,
        "status": "ended",
        "added": "2019-01-15T10:00:00Z",
        "lastAired": "2012-05-01T00:00:00Z",
        "statistics": {"sizeOnDisk": 1200 * GB, "episodeFileCount": 160},
        "seasons": [{"seasonNumber": 0, "statistics": {"episodeFileCount": 0}}]
        + [{"seasonNumber": n, "statistics": {"episodeFileCount": 20}} for n in range(1, 9)],
    },
    {
        "id": 8,
        "title": "Fresh Show",
        "year": 2024,
        "tvdbId": 800,
        "tmdbId": 8000,
        "qualityProfileId": 9,
        "status": "continuing",
        "added": "2024-03-01T10:00:00Z",
        "previousAiring": "2026-10-09T20:00:00Z",
        "statistics": {"sizeOnDisk": 50 * GB, "episodeFileCount": 20},
        "seasons": [
            {"seasonNumber": 1, "statistics": {"episodeFileCount": 10}},
            {"seasonNumber": 2, "statistics": {"episodeFileCount": 10}},
        ],
    },
    {"id": 9, "title": "Empty Show", "tvdbId": 900, "statistics": {"sizeOnDisk": 0}, "seasons": []},
]
MOVIES = [
    {
        "title": "Huge Remux",
        "year": 2002,
        "tmdbId": 41,
        "sizeOnDisk": 80 * GB,
        "added": "2018-05-01T10:00:00Z",
        "movieFile": {"dateAdded": "2019-06-01T10:00:00Z", "quality": {"quality": {"name": "Remux-2160p"}}},
    },
    {
        "title": "New Big",
        "year": 2026,
        "tmdbId": 42,
        "sizeOnDisk": 30 * GB,
        "added": "2026-09-01T10:00:00Z",
        "movieFile": {"dateAdded": "2026-09-02T10:00:00Z", "quality": {"quality": {"name": "Bluray-2160p"}}},
    },
    {"title": "No File", "year": 2027, "tmdbId": 43, "sizeOnDisk": 0, "added": "2026-01-01T10:00:00Z"},
    {
        "title": "Small Old",
        "year": 1999,
        "tmdbId": 44,
        "added": "2015-01-01T10:00:00Z",
        "statistics": {"sizeOnDisk": 2 * GB, "movieFileQualities": [{"name": "DVD"}]},
    },
]
REQUESTS = [
    {
        "type": "movie",
        "createdAt": "2020-01-01T00:00:00Z",
        "requestedBy": {"displayName": "Timon"},
        "media": {"tmdbId": 41},
    },
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
        "type": "tv",
        "createdAt": "2024-03-01T00:00:00Z",
        "requestedBy": {"displayName": "Noah"},
        "media": {"tmdbId": 8000},
    },
    {
        "type": "movie",
        "createdAt": "2026-01-01T00:00:00Z",
        "requestedBy": {"displayName": "Beni"},
        "media": {"tmdbId": 999},
    },
]
TB = 10**12
DISK = [
    {"path": "/", "freeSpace": 28 * GB, "totalSpace": 53 * GB},
    {"path": "/data", "freeSpace": int(9.7 * TB), "totalSpace": 123 * TB},
]


def disc(i, mtype, title, dt, vote, status=None, reqs=None):
    x = {
        "id": i,
        "mediaType": mtype,
        ("title" if mtype == "movie" else "name"): title,
        ("releaseDate" if mtype == "movie" else "firstAirDate"): dt,
        "voteAverage": vote,
    }
    if status:
        x["mediaInfo"] = {"status": status, "requests": reqs or []}
    return x


TREND = {
    1: {
        "page": 1,
        "totalPages": 2,
        "results": [
            disc(101, "movie", "Spider-Man: Brand New Day", "2026-07-29", 8.357, 5),
            disc(102, "tv", "Carrie", "2026-10-07", 8.416),
            {"id": 103, "mediaType": "person", "name": "Some Actor"},
            disc(104, "movie", "Other Mommy", "2026-10-07", 6.375, 3),
            disc(105, "tv", "Pending Show", "2026-01-01", 7.0, 1, [{"status": 1, "is4k": False}]),
        ],
    },
    2: {
        "page": 2,
        "totalPages": 2,
        "results": [
            disc(106, "tv", "Avatar: Seven Havens", "2026-10-09", 9.7, 4),
            disc(107, "movie", "Matchbox the Movie", "2026-10-05", 0),
        ],
    },
}
SEARCH = {
    "inception": [
        disc(27205, "movie", "Inception", "2010-07-15", 8.4, 5),
        disc(5000, "tv", "Inception: Behind", "2011-01-01", 6.0),
    ],
    "breaking bad": [disc(1396, "tv", "Breaking Bad", "2008-01-20", 8.9, 5)],
}
RECS = {
    "/api/v1/movie/27205/recommendations": [
        disc(9620, "movie", "Paycheck", "2003-12-25", 6.2),
        disc(353491, "movie", "The Dark Tower", "2017-08-03", 5.8, 5),
        disc(157336, "movie", "Interstellar", "2014-11-05", 8.4),
    ],
    "/api/v1/tv/1396/recommendations": [],
    "/api/v1/tv/1396/similar": [
        disc(80968, "tv", "Narcos: Mexico", "2018-11-16", 7.9),
        disc(1438, "tv", "The Wire", "2002-06-02", 8.6, 5),
    ],
}


def paged(recs, qs):
    page, size = int(qs.get("page", ["1"])[0]), int(qs.get("pageSize", ["10"])[0])
    return {"page": page, "pageSize": size, "totalRecords": len(recs), "records": recs[(page - 1) * size : page * size]}


def seerr_page(items, qs):
    take, skip = int(qs.get("take", ["10"])[0]), int(qs.get("skip", ["0"])[0])
    return {"pageInfo": {"results": len(items), "pageSize": take}, "results": items[skip : skip + take]}


def ready_route(svc, path, qs):
    try:
        with open(READY) as f:
            d = json.load(f)
    except (TypeError, OSError, ValueError):
        return None
    if svc == "seerr":
        if path == "/api/v1/user":
            return seerr_page(d["users"], qs)
        parts = path.split("/")
        if len(parts) == 6 and parts[3] == "user" and parts[5] == "requests":
            return seerr_page(d["requests"].get(parts[4], []), qs)
        if len(parts) == 5 and parts[3] == "movie" and parts[4] in d["movies"]:
            return d["movies"][parts[4]]
    if svc == "sonarr" and path == "/api/v3/history/since":
        since = qs.get("date", [""])[0]
        return [r for r in d["history"] if r["date"] >= since]
    return None


def route(svc, path, qs, scen):
    r = ready_route(svc, path, qs) if READY else None
    if r is not None:
        return r
    if svc == "sonarr":
        if path == "/api/v3/calendar":
            return CAL
        if path == "/api/v3/wanted/missing":
            return paged(S_MISSING, qs)
        if path == "/api/v3/wanted/cutoff":
            return paged(S_CUTOFF, qs)
        if path == "/api/v3/series":
            return SERIES
        if path == "/api/v3/qualityprofile":
            return S_PROFILES
        if path == "/api/v3/health":
            if scen == "unhealthy":
                return [
                    {
                        "source": "IndexerStatusCheck",
                        "type": "warning",
                        "message": "Indexers unavailable due to failures: NZBgeek",
                        "wikiUrl": {"fullUri": "https://wiki.servarr.com/sonarr/system#indexers"},
                    }
                ]
            return []
        if path == "/api/v3/queue":
            return {"records": []}
        if path == "/api/v3/diskspace":
            return DISK
    if svc == "radarr":
        if path == "/api/v3/calendar":
            return RCAL
        if path == "/api/v3/wanted/missing":
            return paged(R_MISSING, qs)
        if path == "/api/v3/wanted/cutoff":
            return paged(R_CUTOFF, qs)
        if path == "/api/v3/qualityprofile":
            return R_PROFILES
        if path == "/api/v3/movie":
            return MOVIES
        if path == "/api/v3/diskspace":
            return DISK
        if path == "/api/v3/health":
            if scen == "unhealthy":
                return [{"source": "RootFolderCheck", "type": "error", "message": "Missing root folder: /data/Movies"}]
            if scen == "notes":
                return [{"source": "UpdateCheck", "type": "notice", "message": "New update available"}]
            return []
        if path == "/api/v3/queue":
            return {"records": []}
    if svc == "seerr":
        if path == "/api/v1/status":
            return {
                "version": "3.5.0",
                "updateAvailable": scen in ("unhealthy", "notes"),
                "commitsBehind": 3 if scen == "notes" else 0,
                "restartRequired": scen == "unhealthy",
            }
        if path == "/api/v1/request":
            take, skip = int(qs.get("take", ["10"])[0]), int(qs.get("skip", ["0"])[0])
            se = {"radarr": [{"id": 0, "name": "Radarr 4K"}] if scen == "unhealthy" else [], "sonarr": []}
            return {
                "pageInfo": {"results": len(REQUESTS), "pageSize": take},
                "results": REQUESTS[skip : skip + take],
                "serviceErrors": se,
            }
        if path == "/api/v1/discover/trending":
            return TREND.get(int(qs.get("page", ["1"])[0]), {"page": 3, "totalPages": 2, "results": []})
        if path == "/api/v1/search":
            res = SEARCH.get(qs.get("query", [""])[0].lower(), [])
            return {"page": 1, "totalPages": 1, "results": res}
        if path in RECS:
            return {"page": 1, "totalPages": 1, "results": RECS[path]}
        if path.startswith("/api/v1/movie/") or path.startswith("/api/v1/tv/"):
            tid = int(path.split("/")[4])
            for res in SEARCH.values():
                for x in res:
                    if x["id"] == tid:
                        return x
    return None


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the client already gave up (the "slow" timeout test): not a server error

    def handle_any(self):
        with open(LOG, "a") as f:
            f.write(f"{self.command} {self.path}\n")
        try:
            scen = open(SCEN).read().strip()
        except OSError:
            scen = "ok"
        u = urllib.parse.urlsplit(self.path)
        parts = u.path.split("/", 2)
        svc, path = parts[1], "/" + (parts[2] if len(parts) > 2 else "")
        if self.headers.get("X-Api-Key") != KEYS.get(svc):
            return self._send(401, {"error": "Unauthorized"})
        if self.command != "GET":
            return self._send(405, {"error": "fake server is read-only"})
        if scen == "slow":
            time.sleep(3)
        if scen == "http500" and svc == "sonarr":
            return self._send(500, {"message": "database is locked"})
        if scen == "badjson" and svc == "radarr":
            return self._send(200, b"<html>login</html>", "text/html")
        body = route(svc, path, urllib.parse.parse_qs(u.query), scen)
        if body is None:
            return self._send(404, {"message": "NotFound"})
        self._send(200, body)

    do_GET = do_POST = do_PUT = do_DELETE = handle_any


srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
with open(PORT_FILE, "w") as f:
    f.write(str(srv.server_address[1]))
srv.serve_forever()
