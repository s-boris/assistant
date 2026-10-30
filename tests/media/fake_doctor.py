"""Fake Seerr + Radarr + Sonarr + Emby for `media doctor` (test_doctor.py). Stateful: monitor PUTs and queue DELETEs
change what later GETs return. Logs every request as 'METHOD /path?query BODY' to argv[2].
Clock of the fixtures: 2026-10-20 12:00 Zurich (MEDIA_NOW in test_doctor.py)."""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

KEYS = {"seerr": "SEERR-KEY-Zq81", "radarr": "RADARR-KEY-Xw42", "sonarr": "SONARR-KEY-Vy73", "embysvc": "EMBY-KEY-Tt55"}
ANA, BEN, OWNER = (
    {"id": 2, "displayName": "Ana Muster"},
    {"id": 3, "displayName": "Ben"},
    {"id": 1, "displayName": "Owner"},
)


def req(rid, mtype, tmdb, st, ms, created, who=ANA, ext=None, seasons=(), tvdb=None, is4k=False):
    return {
        "id": rid,
        "status": st,
        "type": mtype,
        "is4k": is4k,
        "createdAt": created + "T10:00:00.000Z",
        "requestedBy": who,
        "seasons": [{"seasonNumber": s, "status": 2} for s in seasons],
        "media": {
            "id": 900 + rid,
            "mediaType": mtype,
            "tmdbId": tmdb,
            "tvdbId": tvdb,
            "status": ms,
            "externalServiceId": ext,
        },
    }


REQUESTS = [
    req(1, "movie", 101, 2, 3, "2026-09-01", ext=11),
    req(2, "movie", 102, 2, 3, "2026-09-05", who=BEN, ext=12),
    req(3, "movie", 103, 2, 3, "2026-09-06", ext=13),
    req(4, "movie", 104, 2, 3, "2026-10-01", who=OWNER, ext=14),
    req(5, "movie", 105, 2, 3, "2026-08-01", ext=15),
    req(6, "movie", 106, 2, 3, "2026-07-01", ext=16),
    req(7, "movie", 107, 2, 3, "2026-06-01", ext=17),
    req(8, "movie", 108, 2, 3, "2026-06-02", ext=18),
    req(9, "tv", 201, 4, 1, "2026-10-10", seasons=[1], tvdb=9201),
    req(10, "movie", 109, 2, 1, "2026-09-09", ext=None),  # removed from Radarr: skipped
    req(11, "movie", 110, 2, 3, "2025-01-01", ext=20),  # older than 180 d: skipped
    req(12, "tv", 202, 2, 4, "2026-09-20", ext=21, seasons=[1], tvdb=9202),
    req(13, "tv", 203, 2, 4, "2026-09-21", ext=22, seasons=[1], tvdb=9203),
    req(14, "movie", 111, 2, 3, "2026-09-25", ext=19),
    req(15, "movie", 101, 2, 3, "2026-09-02", ext=None, is4k=True),  # 4K: ignored
    req(16, "movie", 112, 1, 2, "2026-10-18", who=BEN),  # pending approval
    req(17, "movie", 113, 5, 5, "2026-05-01", ext=23),  # completed: not open
    req(18, "tv", 204, 4, 1, "2025-02-01", seasons=[1], tvdb=9204),  # failed long ago: skipped
]


def movie(mid, tmdb, title, yr, monitored=True, has_file=False, **kw):
    return {
        "id": mid,
        "tmdbId": tmdb,
        "title": title,
        "originalTitle": title,
        "year": yr,
        "monitored": monitored,
        "hasFile": has_file,
        "status": kw.pop("status", "released"),
        "qualityProfileId": 1,
        "alternateTitles": [],
        "lastSearchTime": kw.pop("last", None),
        **kw,
    }


MOVIES = {
    11: movie(11, 101, "Mismatch Movie", 2022, inCinemas="2022-09-27T00:00:00Z", digitalRelease="2021-09-26T00:00:00Z"),
    12: movie(12, 102, "Failed Download", 2024, digitalRelease="2025-01-01T00:00:00Z"),
    13: movie(13, 103, "Stalled Torrent", 2024, digitalRelease="2025-01-01T00:00:00Z"),
    14: movie(14, 104, "Downloading Movie", 2025, digitalRelease="2026-01-01T00:00:00Z"),
    15: movie(15, 105, "Cinema Movie", 2026, status="announced", inCinemas="2026-11-19T00:00:00Z"),
    16: movie(16, 106, "Unmonitored Gem", 2019, monitored=False, digitalRelease="2019-05-01T00:00:00Z"),
    17: movie(17, 107, "Nowhere Film", 2007, inCinemas="2007-06-19T00:00:00Z", last="2026-10-01T00:00:00Z"),
    18: movie(18, 108, "Emby Twin", 2019, digitalRelease="2019-05-01T00:00:00Z"),
    19: movie(19, 111, "Blocked Import", 2023, digitalRelease="2024-01-01T00:00:00Z"),
}
PROFILE = {
    "id": 1,
    "name": "Default QP",
    "cutoff": 7,
    "items": [
        {"quality": {"id": 1, "name": "SDTV"}, "items": [], "allowed": False},
        {"quality": {"id": 3, "name": "WEBDL-1080p"}, "items": [], "allowed": True},
        {"quality": {"id": 7, "name": "Bluray-1080p"}, "items": [], "allowed": True},
        {"quality": {"id": 31, "name": "Remux-2160p"}, "items": [], "allowed": True},
    ],
}
UNK = ["Unknown Movie. Unable to match to correct movie using release title."]


def rel(title, q, guid, approved=False, rej=UNK, **kw):
    return {
        "title": title,
        "guid": guid,
        "indexerId": 5,
        "protocol": "usenet",
        "approved": approved,
        "rejections": [] if approved else rej,
        "quality": {"quality": {"name": q}},
        "ageMinutes": 100,
        **kw,
    }


RELEASES = {
    11: [
        rel("Mismatch.Movie.2021.1080p.WEB-DL-GRP", "WEBDL-1080p", "g-web"),
        rel("Mismatch.Movie.2021.2160p.REMUX-GRP", "Remux-2160p", "g-remux"),
        rel("Mismatch.Movie.2021.1080p.BluRay-GRP2", "Bluray-1080p", "g-bluray"),
        rel("Other.Film.2021.1080p.BluRay-X", "Bluray-1080p", "g-other"),
        rel("Mismatch.Movie.2015.720p.HDTV-Y", "SDTV", "g-old"),
    ],
    16: [rel("Unmonitored.Gem.2019.1080p.WEB-DL-Z", "WEBDL-1080p", "g-gem", approved=True)],
    17: [],
    18: [],
}
QUEUE = {
    "radarr": [
        {
            "id": 501,
            "movieId": 12,
            "downloadId": "D1",
            "protocol": "usenet",
            "status": "completed",
            "trackedDownloadState": "importFailed",
            "trackedDownloadStatus": "warning",
            "size": 100,
            "sizeleft": 0,
            "statusMessages": [{"messages": ["No files found are eligible for import"]}],
            "added": "2026-10-19T00:00:00Z",
        },
        {
            "id": 502,
            "movieId": 13,
            "downloadId": "D2",
            "protocol": "torrent",
            "status": "warning",
            "trackedDownloadState": "downloading",
            "trackedDownloadStatus": "warning",
            "size": 100,
            "sizeleft": 100,
            "statusMessages": [{"messages": ["The download is stalled with no connections"]}],
            "added": "2026-10-15T00:00:00Z",
        },
        {
            "id": 503,
            "movieId": 14,
            "downloadId": "D3",
            "protocol": "usenet",
            "status": "downloading",
            "trackedDownloadState": "downloading",
            "trackedDownloadStatus": "ok",
            "size": 100,
            "sizeleft": 60,
            "estimatedCompletionTime": "2026-10-20T11:30:00Z",
            "added": "2026-10-20T09:00:00Z",
        },
        {
            "id": 504,
            "movieId": 19,
            "downloadId": "D4",
            "protocol": "usenet",
            "status": "completed",
            "trackedDownloadState": "importBlocked",
            "trackedDownloadStatus": "warning",
            "size": 100,
            "sizeleft": 0,
            "statusMessages": [{"messages": ["Manual import required"]}],
            "added": "2026-10-18T00:00:00Z",
        },
    ],
    "sonarr": [],
}
SERIES = {
    21: {
        "id": 21,
        "tvdbId": 9202,
        "title": "Airing Show",
        "year": 2026,
        "monitored": True,
        "alternateTitles": [],
        "seasons": [{"seasonNumber": 1, "monitored": True}],
    },
    22: {
        "id": 22,
        "tvdbId": 9203,
        "title": "Gappy Show",
        "year": 2025,
        "monitored": True,
        "alternateTitles": [],
        "seasons": [{"seasonNumber": 1, "monitored": True}],
    },
}


def ep(eid, sid, n, air, has, mon=True):
    return {
        "id": eid,
        "seriesId": sid,
        "seasonNumber": 1,
        "episodeNumber": n,
        "airDateUtc": air,
        "hasFile": has,
        "monitored": mon,
        "lastSearchTime": None,
        "episodeFileId": eid * 10 if has else 0,
    }


EPISODES = {
    21: [
        ep(2101, 21, 1, "2026-10-01T01:00:00Z", True),
        ep(2102, 21, 2, "2026-10-08T01:00:00Z", True),
        ep(2103, 21, 3, "2026-10-15T01:00:00Z", True),
        ep(2104, 21, 4, "2026-10-22T01:00:00Z", False),
    ],
    22: [
        ep(2201, 22, 1, "2025-01-01T01:00:00Z", True),
        ep(2202, 22, 2, "2025-01-08T01:00:00Z", True),
        ep(2203, 22, 3, "2025-01-15T01:00:00Z", False),
        ep(2204, 22, 4, "2025-01-22T01:00:00Z", False, mon=False),
    ],
}
SONARR_RELEASES = {
    22: [
        {
            "title": "Gappy.Show.S01E03.1080p.WEB-DL-Q",
            "guid": "s-3",
            "indexerId": 5,
            "approved": True,
            "rejections": [],
            "episodeNumbers": [3],
            "fullSeason": False,
            "quality": {"quality": {"name": "WEBDL-1080p"}},
        },
        {
            "title": "Gappy.Show.S01E01.1080p.WEB-DL-Q",
            "guid": "s-1",
            "indexerId": 5,
            "approved": False,
            "rejections": ["Existing file on disk has a equal or higher Custom Format score"],
            "episodeNumbers": [1],
            "fullSeason": False,
            "quality": {"quality": {"name": "WEBDL-1080p"}},
        },
    ]
}
# FAKE_SAME_PACK=same|bigger|other: Gappy Show's only releases are a season pack rejected as "existing file" that is
# the release on disk (same group + tail as the files' scene names) / the same name but far bigger than the files
# (more episodes) / another group's pack.
EPISODE_FILES = {
    22: [
        {
            "id": 22010,
            "seriesId": 22,
            "seasonNumber": 1,
            "size": 1000000000,
            "releaseGroup": "Q",
            "sceneName": "Gappy.Show.S01E01.Pilot.Night.1080p.WEB-DL.AAC2.0.H.264-Q",
            "quality": {"quality": {"name": "WEBDL-1080p"}},
        },
        {
            "id": 22020,
            "seriesId": 22,
            "seasonNumber": 1,
            "size": 1000000000,
            "releaseGroup": "Q",
            "sceneName": "Gappy.Show.S01E02.Web.of.Lies.1080p.WEB-DL.AAC2.0.H.264-Q",
            "quality": {"quality": {"name": "WEBDL-1080p"}},
        },
    ]
}
_SAME = os.environ.get("FAKE_SAME_PACK")
if _SAME:
    _grp = "OTHER" if _SAME == "other" else "Q"
    SONARR_RELEASES[22] = [
        {
            "title": f"Gappy.Show.S01.1080p.WEB-DL.AAC2.0.H.264-{_grp}",
            "guid": f"p-{i}",
            "indexerId": i,
            "approved": False,
            "rejections": ["Existing file on disk has a equal or higher Custom Format score: 10060"],
            "episodeNumbers": [],
            "fullSeason": True,
            "seasonNumber": 1,
            "releaseGroup": _grp,
            "size": 6000000000 if _SAME == "bigger" else 2150000000,
            "quality": {"quality": {"name": "WEBDL-1080p"}},
        }
        for i in (5, 6)
    ]
TITLES = {
    ("movie", 101): ("Mismatch Movie", "2022-09-27", 3, 11, [1]),
    ("movie", 999): ("Mismatch Movie", "2011-01-01", 1, None, []),
    ("movie", 112): ("Waiting Approval", "2026-01-01", 2, None, [16]),
    ("tv", 201): ("Failed Show", "", 1, None, [9]),
}


def seerr(path, qs):
    if path == "/api/v1/request":
        take, skip = int(qs.get("take", ["20"])[0]), int(qs.get("skip", ["0"])[0])
        return {"pageInfo": {"results": len(REQUESTS)}, "results": REQUESTS[skip : skip + take]}
    if path == "/api/v1/user":
        return {"pageInfo": {"results": 3}, "results": [OWNER, ANA, BEN]}
    if path == "/api/v1/search":
        qq = qs.get("query", [""])[0].lower()
        out = []
        for (t, tid), (name, d, ms, ext, rids) in TITLES.items():  # noqa: B007
            if qq in name.lower():
                out.append(
                    {
                        "id": tid,
                        "mediaType": t,
                        ("title" if t == "movie" else "name"): name,
                        ("releaseDate" if t == "movie" else "firstAirDate"): d,
                        "mediaInfo": {"status": ms},
                    }
                )
        return {"results": sorted(out, key=lambda x: x["id"], reverse=True)}
    for t in ("movie", "tv"):
        if path.startswith(f"/api/v1/{t}/"):
            tid = int(path.rsplit("/", 1)[1])
            name, d, ms, ext, rids = TITLES.get((t, tid), (f"Title {tid}", "2020-01-01", 1, None, []))
            rq = [{k: v for k, v in r.items() if k != "media"} for r in REQUESTS if r["id"] in rids]
            return {
                "id": tid,
                ("title" if t == "movie" else "name"): name,
                ("releaseDate" if t == "movie" else "firstAirDate"): d,
                "externalIds": {"tvdbId": 9201} if t == "tv" else {},
                "mediaInfo": {
                    "status": ms,
                    "externalServiceId": ext,
                    "requests": rq,
                    "tvdbId": 9201 if t == "tv" else None,
                },
            }
    return None


def radarr(method, path, qs, body):
    if method == "GET" and path.startswith("/api/v3/movie/") and path.rsplit("/", 1)[1].isdigit():
        return MOVIES.get(int(path.rsplit("/", 1)[1]), 404)
    if path == "/api/v3/movie" and "tmdbId" in qs:
        return [m for m in MOVIES.values() if m["tmdbId"] == int(qs["tmdbId"][0])]
    if path == "/api/v3/movie/lookup/tmdb":
        return 404
    if path == "/api/v3/queue" and method == "GET":
        return {"records": QUEUE["radarr"], "totalRecords": len(QUEUE["radarr"])}
    if path.startswith("/api/v3/queue/") and method == "DELETE":
        qid = int(path.rsplit("/", 1)[1])
        QUEUE["radarr"][:] = [r for r in QUEUE["radarr"] if r["id"] != qid]
        return {}
    if path == "/api/v3/history/movie":
        mid = int(qs["movieId"][0])
        return (
            [
                {"eventType": "grabbed", "date": "2026-09-10T00:00:00Z"},
                {"eventType": "downloadFailed", "date": "2026-09-11T00:00:00Z"},
            ]
            if mid == 17
            else []
        )
    if path == "/api/v3/release" and method == "GET":
        return RELEASES.get(int(qs["movieId"][0]), [])
    if path == "/api/v3/release" and method == "POST":
        return {}
    if path == "/api/v3/qualityprofile/1":
        return PROFILE
    if path == "/api/v3/movie/editor" and method == "PUT":
        for i in body.get("movieIds", []):
            MOVIES[i]["monitored"] = body.get("monitored")
        return {}
    if path == "/api/v3/command" and method == "POST":
        return {"id": 1, "name": body.get("name")}
    return None


def sonarr(method, path, qs, body):
    if method == "GET" and path.startswith("/api/v3/series/") and path.rsplit("/", 1)[1].isdigit():
        return SERIES.get(int(path.rsplit("/", 1)[1]), 404)
    if path == "/api/v3/series" and "tvdbId" in qs:
        return [s for s in SERIES.values() if s["tvdbId"] == int(qs["tvdbId"][0])]
    if path == "/api/v3/series/lookup":
        return [{"title": "Failed Show", "tvdbId": 9201}] if qs.get("term") == ["tvdb:9201"] else []
    if path == "/api/v3/episode" and method == "GET":
        return EPISODES.get(int(qs["seriesId"][0]), [])
    if path == "/api/v3/episode/monitor" and method == "PUT":
        for e in EPISODES[22]:
            if e["id"] in body.get("episodeIds", []):
                e["monitored"] = body.get("monitored")
        return {}
    if path == "/api/v3/episodefile" and method == "GET":
        return EPISODE_FILES.get(int(qs["seriesId"][0]), [])
    if path == "/api/v3/release" and method == "GET":
        return SONARR_RELEASES.get(int(qs["seriesId"][0]), [])
    if path == "/api/v3/queue":
        return {"records": QUEUE["sonarr"], "totalRecords": 0}
    if path == "/api/v3/command" and method == "POST":
        return {"id": 2, "name": body.get("name")}
    return None


def emby(path, qs):
    if path == "/emby/Items":
        if qs.get("SearchTerm") == ["Emby Twin"]:
            return {"Items": [{"Name": "Emby Twin", "ProductionYear": 2019, "ProviderIds": {"Tmdb": "5555"}}]}
        return {"Items": []}
    return None


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def handle_any(self):
        u = urlsplit(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode() if n else ""
        body = json.loads(raw) if raw else {}
        with open(LOG, "a") as f:
            f.write(f"{self.command} {self.path} {raw}\n")
        svc, _, rest = u.path.lstrip("/").partition("/")
        rest = "/" + rest
        key = self.headers.get("X-Emby-Token") if svc == "embysvc" else self.headers.get("X-Api-Key")
        if key != KEYS.get(svc):
            return self._send(401, {"message": "Unauthorized"})
        qs = parse_qs(u.query)
        if svc == "seerr":
            if self.command == "POST" and rest.endswith("/retry"):
                out = {"id": int(rest.split("/")[-2]), "status": 2}
            else:
                out = seerr(rest, qs)
        elif svc == "radarr":
            out = radarr(self.command, rest, qs, body)
        elif svc == "sonarr":
            out = sonarr(self.command, rest, qs, body)
        else:
            out = emby(rest, qs)
        if out == 404:
            return self._send(404, {"message": "NotFound"})
        if out is None:
            return self._send(404, {"message": f"fake: no route {self.command} {rest}"})
        self._send(200, out)

    do_GET = do_POST = do_PUT = do_DELETE = handle_any


if __name__ == "__main__":
    LOG = sys.argv[2]
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    with open(sys.argv[1], "w") as f:
        f.write(str(srv.server_address[1]))
    srv.serve_forever()
