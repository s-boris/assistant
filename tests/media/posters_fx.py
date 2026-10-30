#!/usr/bin/env python3
"""Fake Emby + Radarr/Sonarr history for `media posters` tests (fictional titles only).

  posters_fx.py PORTFILE LOGFILE SCENARIOFILE
Scenario (file content, re-read per request): ok | radarr-down | many | empty.
Logs "METHOD PATH?QUERY token=yes|no" per request. Clock of the tests: Fri 2026-10-23 17:30 Zurich (KW 43).
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
VIEWS = [
    {"Name": "Movies", "Id": "14", "Guid": "g-movies", "CollectionType": "movies"},
    {"Name": "TV Shows", "Id": "16", "Guid": "g-tv", "CollectionType": "tvshows"},
    {"Name": "Kids", "Id": "20", "Guid": "g-kids", "CollectionType": "movies"},  # in his MyMediaExcludes
    {"Name": "Collections", "Id": "30", "Guid": "g-box", "CollectionType": "boxsets"},
    {"Name": "Live TV", "Id": "4", "Guid": "g-live", "CollectionType": "livetv"},
]
P = {"Primary": "tag"}


def mv(i, name, year, rating, created, tmdb, img=True, **kw):
    x = {
        "Id": str(i),
        "Name": name,
        "Type": "Movie",
        "ProductionYear": year,
        "DateCreated": created,
        "ProviderIds": {"Tmdb": str(tmdb)},
        "ImageTags": P if img else {},
    }
    if rating is not None:
        x["CommunityRating"] = rating
    x.update(kw)
    return x


def ep(i, sid, sname, s, e, created, **kw):
    x = {
        "Id": str(i),
        "Name": f"Episode {e}",
        "Type": "Episode",
        "SeriesId": sid,
        "SeriesName": sname,
        "ParentIndexNumber": s,
        "IndexNumber": e,
        "DateCreated": created,
        "SeriesPrimaryImageTag": "t",
    }
    x.update(kw)
    return x


MOVIES = [
    mv(101, "Star Voyage", 2026, 8.1, "2026-10-20T10:00:00.0000000Z", 9101),
    mv(102, "Old Upgrade", 2010, 7.9, "2026-10-21T10:00:00.0000000Z", 9102),  # upgrade: not new
    mv(103, "New Then Better", 2025, 6.5, "2026-10-18T10:00:00.0000000Z", 9103),  # new, then upgraded
    mv(104, "Renamed Film", 2001, 7.0, "2026-10-19T10:00:00.0000000Z", 9104),  # rename only: not new
    mv(105, "Too Old", 2020, 9.0, "2026-10-10T10:00:00.0000000Z", 9105),  # outside the window
    mv(106, "No Poster", 2024, None, "2026-10-17T10:00:00.0000000Z", 9106, img=False),
    mv(107, "Broken Poster", 2023, 6.9, "2026-10-17T10:00:00.0000000Z", 9107),  # image -> HTTP 500
    mv(108, "A Trailer", 2026, 5.0, "2026-10-20T10:00:00.0000000Z", 9108, ExtraType="Trailer"),
]
SERIES = {
    "201": {
        "Id": "201",
        "Name": "Nova Drift",
        "ProductionYear": 2026,
        "CommunityRating": 8.4,
        "DateCreated": "2026-10-20T09:00:00.0000000Z",
        "ProviderIds": {"Tvdb": "501"},
        "ImageTags": P,
    },
    "202": {
        "Id": "202",
        "Name": "Laugh Track",
        "ProductionYear": 2019,
        "CommunityRating": 7.2,
        "DateCreated": "2020-01-01T09:00:00.0000000Z",
        "ProviderIds": {"Tvdb": "502"},
        "ImageTags": P,
    },
    "203": {
        "Id": "203",
        "Name": "Night Shift",
        "ProductionYear": 2022,
        "CommunityRating": 7.8,
        "DateCreated": "2022-01-01T09:00:00.0000000Z",
        "ProviderIds": {"Tvdb": "503"},
        "ImageTags": P,
    },
}
EPS = [
    ep(301, "201", "Nova Drift", 1, 1, "2026-10-20T09:10:00.0000000Z"),
    ep(302, "201", "Nova Drift", 1, 2, "2026-10-20T09:10:00.0000000Z"),
    ep(303, "201", "Nova Drift", 1, 3, "2026-10-20T09:10:00.0000000Z"),
    ep(304, "201", "Nova Drift", 0, 1, "2026-10-20T09:10:00.0000000Z"),  # special
    ep(305, "201", "Nova Drift", 1, 4, "2026-10-20T09:10:00.0000000Z", LocationType="Virtual"),
    ep(311, "202", "Laugh Track", 4, 5, "2026-10-21T20:00:00.0000000Z"),
    ep(312, "202", "Laugh Track", 4, 1, "2026-10-21T20:00:00.0000000Z"),  # upgrade: not new
    ep(321, "203", "Night Shift", 2, 1, "2026-10-18T20:00:00.0000000Z"),
    ep(322, "203", "Night Shift", 3, 1, "2026-10-18T20:00:00.0000000Z"),
]
KIDS = [mv(401, "Hidden Kid Film", 2026, 9.9, "2026-10-20T10:00:00.0000000Z", 9401)]
MANY = [
    mv(500 + i, f"Bulk Film {i:02d}", 2026, round(5 + i / 10, 1), "2026-10-19T10:00:00.0000000Z", 9500 + i)
    for i in range(30)
]


def rad(t, ev, when="2026-10-20T10:00:00Z", **kw):
    return {"eventType": ev, "date": when, "movieId": t, "movie": {"tmdbId": t, "title": "x"}, "data": kw}


def son(tvdb, s, e, ev):
    return {
        "eventType": ev,
        "date": "2026-10-21T20:00:00Z",
        "series": {"tvdbId": tvdb},
        "episode": {"seasonNumber": s, "episodeNumber": e},
        "data": {},
    }


RADARR = [
    rad(9101, "grabbed"),
    rad(9101, "downloadFolderImported"),
    rad(9102, "movieFileDeleted", reason="Upgrade"),
    rad(9102, "downloadFolderImported"),
    rad(9103, "downloadFolderImported"),
    rad(9103, "movieFileDeleted", reason="Upgrade"),
    rad(9103, "downloadFolderImported"),
    rad(9104, "movieFileRenamed"),
]
SONARR = [
    son(501, 1, 1, "downloadFolderImported"),
    son(502, 4, 5, "downloadFolderImported"),
    son(502, 4, 1, "episodeFileDeleted"),
    son(502, 4, 1, "downloadFolderImported"),
]


def png(color):
    from PIL import Image

    b = io.BytesIO()
    Image.new("RGB", (200, 300), color).save(b, "PNG")
    return b.getvalue()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def handle_any(self):
        u = urllib.parse.urlsplit(self.path)
        qs = dict(urllib.parse.parse_qsl(u.query))
        tok = bool(self.headers.get("X-Emby-Token") or self.headers.get("X-Api-Key"))
        with open(sys.argv[2], "a") as f:
            f.write(f"{self.command} {self.path} token={'yes' if tok else 'no'}\n")
        sc = open(sys.argv[3]).read().strip()
        p = u.path
        if self.command != "GET":
            return self.send(405, {"error": "GET only"})
        if not tok:
            return self.send(401, {"error": "no key"})
        if p == f"/emby/Users/{UID}":
            return self.send(
                200, {"Id": UID, "Configuration": {"MyMediaExcludes": ["g-kids"], "LatestItemsExcludes": []}}
            )
        if p == f"/emby/Users/{UID}/Views":
            return self.send(200, {"Items": VIEWS})
        if p == f"/emby/Users/{UID}/Items":
            if "Ids" in qs:
                return self.send(200, {"Items": [SERIES[i] for i in qs["Ids"].split(",") if i in SERIES]})
            if sc == "empty":
                return self.send(200, {"Items": []})
            items = {"14": MOVIES + (MANY if sc == "many" else []), "16": EPS, "20": KIDS}.get(qs.get("ParentId"), [])
            return self.send(200, {"Items": items, "TotalRecordCount": len(items)})
        if p.startswith("/emby/Items/") and p.endswith("/Images/Primary"):
            iid = p.split("/")[3]
            if iid == "107":
                return self.send(500, {"error": "boom"})
            return self.send(200, png((40 + int(iid) % 200, 80, 160)), "image/png")
        if p == "/radarr/api/v3/history/since":
            return self.send(500, {"error": "down"}) if sc == "radarr-down" else self.send(200, RADARR)
        if p == "/sonarr/api/v3/history/since":
            return self.send(200, SONARR)
        return self.send(404, {"error": "not found"})

    do_GET = do_POST = do_PUT = do_DELETE = handle_any


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    with open(sys.argv[1] + ".tmp", "w") as f:
        f.write(str(srv.server_address[1]))
    import os

    os.replace(sys.argv[1] + ".tmp", sys.argv[1])
    srv.serve_forever()
