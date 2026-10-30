#!/usr/bin/env python3
"""Fake Emby + Radarr/Sonarr + Seerr + Tracearr + Telegram Bot API for `media digest` tests (fictional data only).

  digest_fx.py PORTFILE LOGFILE SCENARIOFILE
Scenario (file content, re-read per request): ok | empty | tg-fail.
Logs "METHOD PATH?QUERY" per request (sendPhoto: also the multipart fields, never the photo bytes).
Clock of the tests: Sun 2026-10-25 19:00 Zurich (18:00Z), KW 43.
"""

import io
import json
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# the CI runner: Pillow is in the CI toolchain's site, on PYTHONPATH, which -I ignores
if os.environ.get("ASSISTANT_CI_TOOLCHAIN"):
    sys.path.append(os.path.join(os.environ["ASSISTANT_CI_TOOLCHAIN"], "site"))

UID = "a" * 32
TOKEN = "12345:FAKEfakeFAKEfakeFAKE0"
VIEWS = [
    {"Name": "Movies", "Id": "14", "Guid": "g-movies", "CollectionType": "movies"},
    {"Name": "TV Shows", "Id": "16", "Guid": "g-tv", "CollectionType": "tvshows"},
]
P = {"Primary": "tag"}
DAY = "2026-10-25T10:00:00.0000000Z"  # inside the daily window
WEEK = "2026-10-21T10:00:00.0000000Z"  # inside the weekly window only
MOVIES = [
    {
        "Id": "101",
        "Name": "Star Voyage",
        "Type": "Movie",
        "ProductionYear": 2026,
        "CommunityRating": 8.1,
        "DateCreated": DAY,
        "ProviderIds": {"Tmdb": "9101"},
        "ImageTags": P,
    },
    {
        "Id": "102",
        "Name": "Quiet Harbor",
        "Type": "Movie",
        "ProductionYear": 2024,
        "CommunityRating": 6.9,
        "DateCreated": WEEK,
        "ProviderIds": {"Tmdb": "9102"},
        "ImageTags": {},
    },
]
EPS = [
    {
        "Id": f"20{i}",
        "Name": f"Episode {i}",
        "Type": "Episode",
        "SeriesId": "301",
        "SeriesName": "Moon Station",
        "ParentIndexNumber": 2,
        "IndexNumber": i,
        "DateCreated": DAY,
        "SeriesPrimaryImageTag": "t",
    }
    for i in (1, 2)
]
SERIES = {
    "301": {
        "Id": "301",
        "Name": "Moon Station",
        "ProductionYear": 2023,
        "CommunityRating": 7.7,
        "DateCreated": "2025-01-01T00:00:00.0000000Z",
        "ProviderIds": {"Tvdb": "7301", "Tmdb": "8301"},
        "ImageTags": P,
    }
}
BY_ID = dict(SERIES, **{m["Id"]: m for m in MOVIES})


def q(r, quality, res, src, mod="none", **kw):
    r["quality"] = {"quality": {"name": quality, "resolution": res, "source": src, "modifier": mod}}
    r.update(kw)
    return r


RADARR_HIST = [
    q(
        {
            "eventType": "downloadFolderImported",
            "movieId": 1,
            "date": "2026-10-25T09:00:00Z",
            "data": {"size": "8000000000"},
        },
        "Bluray-2160p",
        2160,
        "bluray",
    ),
    q(
        {
            "eventType": "movieFileDeleted",
            "movieId": 1,
            "date": "2026-10-25T09:00:01Z",
            "data": {"size": "5000000000", "reason": "Upgrade"},
        },
        "Bluray-1080p",
        1080,
        "bluray",
    ),  # upgrade
    q(
        {
            "eventType": "downloadFolderImported",
            "movieId": 2,
            "date": "2026-10-22T09:00:00Z",
            "data": {"size": "4000000000"},
        },
        "Bluray-1080p",
        1080,
        "bluray",
    ),
    q(
        {
            "eventType": "movieFileDeleted",
            "movieId": 2,
            "date": "2026-10-22T09:00:01Z",
            "data": {"size": "30000000000", "reason": "Upgrade"},
        },
        "Remux-1080p",
        1080,
        "bluray",
        "remux",
    ),  # downgrade
    q(
        {
            "eventType": "movieFileDeleted",
            "movieId": 3,
            "date": "2026-10-23T09:00:00Z",
            "data": {"size": "2000000000", "reason": "Manual"},
        },
        "WEBDL-1080p",
        1080,
        "webdl",
    ),  # cleanup
]
SONARR_HIST = [
    q(
        {
            "eventType": "downloadFolderImported",
            "episodeId": 11,
            "date": "2026-10-25T08:00:00Z",
            "data": {"size": "1000000000"},
        },
        "WEBDL-1080p",
        1080,
        "web",
    ),
]
LIB_MOVIES = [
    {"id": 1, "hasFile": True, "sizeOnDisk": 8000000000},
    {"id": 2, "hasFile": True, "sizeOnDisk": 4000000000},
    {"id": 4, "hasFile": False, "sizeOnDisk": 0},
]
LIB_SERIES = [{"id": 1, "statistics": {"episodeFileCount": 12, "sizeOnDisk": 24000000000}}]
REQS = [
    {
        "id": 51,
        "type": "movie",
        "status": 5,
        "updatedAt": "2026-10-25T09:05:00.000Z",
        "media": {"tmdbId": 9101, "status": 5},
        "requestedBy": {"displayName": "Robo Tester"},
    },
    {
        "id": 52,
        "type": "movie",
        "status": 5,
        "updatedAt": "2026-10-25T09:06:00.000Z",
        "media": {"tmdbId": 9101, "status": 5},
        "requestedBy": {"displayName": "zed_unit"},
    },
    {
        "id": 53,
        "type": "tv",
        "status": 5,
        "updatedAt": "2026-10-20T09:00:00.000Z",  # before the window
        "media": {"tmdbId": 8301, "tvdbId": 7301, "status": 5},
        "requestedBy": {"displayName": "Old Asker"},
    },
    {
        "id": 54,
        "type": "movie",
        "status": 5,
        "updatedAt": "2026-10-25T09:07:00.000Z",  # not in Emby yet
        "media": {"tmdbId": 9555, "status": 5},
        "requestedBy": {"displayName": "Robo Tester"},
    },
]
TA = [
    {
        "media_type": "episode",
        "show_title": "Moon Station",
        "grandparent_rating_key": "301",
        "started_at": "2026-10-25T17:00:00Z",
        "duration_ms": 1500000,
        "user": {"id": "u1", "username": "mira_unit"},
    },
    {
        "media_type": "episode",
        "show_title": "Moon Station",
        "grandparent_rating_key": "301",
        "started_at": "2026-10-25T12:00:00Z",
        "duration_ms": 1500000,
        "user": {"id": "u2", "username": "Kito Tester"},
    },
    {
        "media_type": "movie",
        "media_title": "Star Voyage",
        "rating_key": "101",
        "year": 2026,
        "started_at": "2026-10-25T11:00:00Z",
        "duration_ms": 6000000,
        "user": {"id": "u1", "username": "mira_unit"},
    },
    {
        "media_type": "movie",
        "media_title": "Sternenreise",
        "rating_key": "999",
        "tmdb_id": 9101,  # old id of a
        "started_at": "2026-10-25T10:30:00Z",
        "duration_ms": 6000000,
        "user": {"id": "u4", "username": "ulla.unit"},
    },  # replaced file
    {
        "media_type": "movie",
        "media_title": "Blink Short",
        "rating_key": "109",
        "started_at": "2026-10-25T10:00:00Z",
        "duration_ms": 30000,
        "user": {"id": "u3", "username": "Pim"},
    },  # < 2 min: not a play
    {
        "media_type": "movie",
        "media_title": "Quiet Harbor",
        "rating_key": "102",
        "started_at": "2026-10-22T10:00:00Z",
        "duration_ms": 5000000,
        "user": {"id": "u3", "username": "Pim"},
    },
]  # weekly only


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, obj, ctype="application/json"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log(self, line):
        with open(LOG, "a") as f:
            f.write(line + "\n")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        u = urllib.parse.urlsplit(self.path)
        fields = {}
        for part in raw.split(b"\r\n--"):
            if b'name="' in part and b"\r\n\r\n" in part:
                head, _, val = part.partition(b"\r\n\r\n")
                name = head.split(b'name="')[1].split(b'"')[0].decode()
                fields[name] = "<png %d bytes>" % len(val) if name == "photo" else val.decode().rstrip("\r\n-")  # noqa: UP031
        if not u.path.startswith("/tph/"):
            self.log(f"POST {u.path} " + json.dumps(fields, ensure_ascii=False))
        if u.path.startswith("/tph/"):
            form = {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode()).items()}
            with open(LOG, "a") as f:
                f.write(
                    f"TPH {u.path} token={'access_token' in form} "
                    + json.dumps({k: v for k, v in form.items() if k != "access_token"}, ensure_ascii=False)
                    + "\n"
                )
            if open(SCEN).read().strip() == "tph-fail":
                return self.send(200, {"ok": False, "error": "CONTENT_TOO_BIG"})
            if u.path == "/tph/createAccount":
                return self.send(
                    200, {"ok": True, "result": {"short_name": form.get("short_name"), "access_token": "TPH-TOKEN-Qx9"}}
                )
            return self.send(
                200, {"ok": True, "result": {"url": "https://telegra.ph/New-today-10-25", "path": "New-today-10-25"}}
            )
        if not u.path.startswith(f"/tg/bot{TOKEN}/sendPhoto"):
            return self.send(404, {"ok": False})
        if open(SCEN).read().strip() == "tg-fail":
            return self.send(400, {"ok": False, "description": f"Bad Request: chat not found for bot{TOKEN}"})
        self.send(200, {"ok": True, "result": {"message_id": 777}})

    def do_GET(self):
        sc = open(SCEN).read().strip()
        u = urllib.parse.urlsplit(self.path)
        qs = urllib.parse.parse_qs(u.query)
        p = u.path
        self.log(f"GET {p}?{u.query}")
        empty = sc == "empty"
        if p.startswith("/emby/Items/") and p.endswith("/Images/Primary"):
            from PIL import Image

            b = io.BytesIO()
            Image.new("RGB", (200, 300), (90, 60, 150)).save(b, "JPEG")
            return self.send(200, b.getvalue(), "image/jpeg")
        if p == f"/emby/Users/{UID}":
            return self.send(200, {"Configuration": {"MyMediaExcludes": [], "LatestItemsExcludes": []}})
        if p == f"/emby/Users/{UID}/Views":
            return self.send(200, {"Items": VIEWS})
        if p == f"/emby/Users/{UID}/Items":
            if "AnyProviderIdEquals" in qs:
                hit = {"tmdb.9101": [MOVIES[0]]}.get(qs["AnyProviderIdEquals"][0], [])
                return self.send(200, {"Items": hit})
            if "Ids" in qs:
                return self.send(200, {"Items": [BY_ID[i] for i in qs["Ids"][0].split(",") if i in BY_ID]})
            floor = qs.get("MinDateCreated", [""])[0][:19]
            pool = [] if empty else (MOVIES if qs.get("ParentId") == ["14"] else EPS)
            return self.send(200, {"Items": [x for x in pool if x["DateCreated"][:19] >= floor]})
        if p in ("/radarr/api/v3/history/since", "/sonarr/api/v3/history/since"):
            since = qs.get("date", [""])[0][:19]
            h = [] if empty else (RADARR_HIST if p.startswith("/radarr") else SONARR_HIST)
            return self.send(200, [r for r in h if r["date"][:19] >= since])
        if p == "/radarr/api/v3/movie":
            return self.send(200, LIB_MOVIES)
        if p == "/sonarr/api/v3/series":
            return self.send(200, LIB_SERIES)
        if p == "/seerr/api/v1/request":
            res = [] if empty else REQS
            skip = int(qs.get("skip", ["0"])[0])
            return self.send(200, {"pageInfo": {"results": len(res)}, "results": res[skip : skip + 50]})
        if p == "/seerr/api/v1/movie/9101":
            return self.send(
                200,
                {
                    "title": "Star Voyage EN",
                    "releaseDate": "2026-03-01",
                    "posterPath": "/abc123.jpg",
                    "overview": "A crew flies far. " * 30,
                    "voteAverage": 7.9,
                },
            )
        if p == "/seerr/api/v1/movie/9555":
            return self.send(200, {"title": "Lost Reel EN", "releaseDate": "2025-05-01", "overview": "Short."})
        if p == "/seerr/api/v1/settings/jellyfin":
            return self.send(200, {"externalHostname": f"http://{self.headers['Host']}", "serverId": "srv123abc"})
        if p == "/emby/System/Info/Public":
            return self.send(200, {"Id": "srv123abc", "ServerName": "Fake"})
        if p == "/web/index.html":
            return self.send(200, b"<html>emby</html>", "text/html")
        if p == "/seerr/api/v1/tv/8301":  # English TMDB name; 9102 has none: Emby's name stays
            return self.send(200, {"name": "Moon Station EN", "firstAirDate": "2023-02-01"})
        if p == "/seerr/api/v1/settings/notifications/telegram":
            return self.send(
                200, {"enabled": True, "options": {"botAPI": TOKEN, "chatId": "-100777", "messageThreadId": ""}}
            )
        if p == "/ta/api/v2/public/history":
            return self.send(200, {"data": [] if empty else TA, "meta": {}})
        self.send(404, {"error": "not found"})


if __name__ == "__main__":
    PORTF, LOG, SCEN = sys.argv[1:4]
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    with open(PORTF + ".tmp", "w") as f:
        f.write(str(srv.server_address[1]))
    import os

    os.replace(PORTF + ".tmp", PORTF)
    srv.serve_forever()
