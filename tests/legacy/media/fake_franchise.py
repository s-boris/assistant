#!/usr/bin/env python3
"""Fake Radarr + Seerr for `media franchise` tests (fictional film series).
Usage: fake_franchise.py PORTFILE LOGFILE SCENARIOFILE. Logs "METHOD path" per request.
Scenarios: ok | seerr-down (Seerr 500) | radarr-down (Radarr 500)."""

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORTFILE, LOG, SCEN = sys.argv[1:4]
KEYS = {"radarr": "RADARR-KEY-Xw42", "seerr": "SEERR-KEY-Zq81"}
LONG = "The Extraordinarily Long and Winding Chronicle of the Velvet Static Broadcast That Never Ended Part Two"
# (collection id, name, [(tmdb, title, year, radarr: file|wanted|unmon|-, radarr status, excluded, rating, votes,
#                        seerr status, seerr release date)])
S = [
    (
        9001,
        "Starfall Collection",
        [
            (101, "Starfall", 2001, "file", "released", 0, 7.5, 9000, 5, "2001-05-01"),
            (102, "Starfall II", 2004, "file", "released", 0, 7.1, 6000, 5, "2004-05-01"),
            (103, "Starfall III", 2008, "-", "released", 0, 7.2, 4000, 1, "2008-05-01"),
            (104, "Starfall: Origins", 2027, "-", "announced", 0, 0, 0, 1, "2027-03-01"),
        ],
    ),
    (
        9002,
        "Copper Kettle Collection",
        [
            (201, "Copper Kettle", 1999, "file", "released", 0, 6.5, 800, 5, "1999-01-01"),
            (202, "Copper Kettle 2", 2002, "file", "released", 0, 6.2, 500, 5, "2002-01-01"),
            (203, "Copper Kettle 3", 2005, "-", "released", 0, 6.0, 400, 2, "2005-01-01"),
        ],
    ),
    (
        9003,
        "Night Ferry Collection",
        [
            (301, "Night Ferry", 2010, "file", "released", 0, 6.9, 700, 5, "2010-01-01"),
            (302, "Night Ferry 2", 2012, "file", "released", 0, 6.7, 600, 5, "2012-01-01"),
            (303, "Night Ferry 3", 2014, "wanted", "released", 0, 6.5, 500, 3, "2014-01-01"),
            (304, "Night Ferry 4", 2016, "unmon", "released", 0, 5.0, 300, 1, "2016-01-01"),
        ],
    ),
    (
        9004,
        "Glass Orchard Collection",
        [
            (401, "Glass Orchard", 1990, "file", "released", 0, 7.0, 2000, 5, "1990-01-01"),
            (402, "Glass Orchard: Spring", 1992, "file", "released", 0, 6.8, 1500, 5, "1992-01-01"),
            (403, "Glass Orchard: Summer", 1994, "file", "released", 0, 6.6, 1200, 5, "1994-01-01"),
            (404, "Glass Orchard: Autumn", 1996, "-", "released", 0, 6.1, 120, 1, "1996-01-01"),
            (405, "Glass Orchard: Winter", 1998, "-", "released", 0, 6.8, 900, 1, "1998-01-01"),
        ],
    ),
    (
        9005,
        "Lone Signal Collection",
        [
            (501, "Lone Signal", 2015, "file", "released", 0, 6.4, 300, 5, "2015-01-01"),
            (502, "Lone Signal Returns", 2018, "-", "released", 0, 6.3, 250, 1, "2018-01-01"),
        ],
    ),
    (
        9006,
        "Paper Moons Collection",
        [
            (601, "Paper Moons", 2003, "file", "released", 0, 6.6, 1000, 5, "2003-01-01"),
            (602, "Paper Moons Rising", 2006, "file", "released", 0, 6.4, 800, 5, "2006-01-01"),
            (603, "Paper Moons Forever", 2009, "-", "released", 0, 6.2, 700, 1, "2009-01-01"),
        ],
    ),
    (
        9007,
        "Harbor Lights Collection",
        [
            (701, "Harbor Lights", 1985, "file", "released", 0, 7.0, 3000, 5, "1985-01-01"),
            (702, "Harbor Lights II", 1987, "file", "released", 0, 6.5, 2000, 5, "1987-01-01"),
            (703, "Harbor Lights III", 1989, "-", "released", 0, 6.4, 1800, 5, "1989-01-01"),
            (704, "Harbor Lights IV", 1991, "-", "released", 1, 5.1, 900, 1, "1991-01-01"),
        ],
    ),
    (
        9008,
        "Iron Choir Collection",
        [
            (801, "Iron Choir", 2011, "file", "released", 0, 6.0, 400, 5, "2011-01-01"),
            (802, "Iron Choir II", 2013, "file", "released", 0, 5.9, 300, 5, "2013-01-01"),
            (803, "Iron Choir III", 2015, "-", "released", 0, 5.8, 200, 6, "2015-01-01"),
        ],
    ),
    (
        9009,
        "Saga of Ash Collection",
        [
            (901, "Saga of Ash", 2000, "file", "released", 0, 6.9, 1500, 5, "2000-01-01"),
            (902, "Saga of Ash: Embers", 2001, "file", "released", 0, 6.8, 1400, 5, "2001-01-01"),
        ]
        + [
            (
                902 + i,
                f"Saga of Ash: Part {i + 2}",
                2001 + i,
                "-",
                "released",
                0,
                6.5 - i / 10,
                1000,
                1,
                f"{2001 + i}-01-01",
            )
            for i in range(1, 6)
        ],
    ),
    (
        9010,
        "Tin Lantern Collection",
        [
            (1001, "Tin Lantern", 1995, "file", "released", 0, 6.3, 600, 5, "1995-01-01"),
            (1002, "Tin Lantern 2", 1997, "file", "released", 0, 6.2, 500, 5, "1997-01-01"),
            (1003, "Tin Lantern 3", 1999, "-", "released", 0, 6.4, 450, 1, "1999-01-01"),
            (1004, "Tin Lantern 4", 2001, "-", "released", 0, 6.1, 400, 1, "2001-01-01"),
            (1005, "Tin Lantern 5", 2003, "-", "released", 0, 6.0, 350, 1, "2003-01-01"),
            (1006, "Tin Lantern 6", 2026, "-", "released", 0, 0, 0, 1, "2026-12-01"),
        ],
    ),
    (
        9011,
        "Velvet Static Collection",
        [
            (1101, "Velvet Static", 2007, "file", "released", 0, 6.0, 200, 5, "2007-01-01"),
            (1102, "Velvet Static 2", 2009, "file", "released", 0, 5.9, 150, 5, "2009-01-01"),
            (1103, LONG, 2011, "-", "released", 0, 5.8, 100, 1, "2011-01-01"),
            (1104, "Velvet Static 4", 2013, "-", "released", 0, 5.7, 90, 1, "2013-01-01"),
        ],
    ),
    (
        9012,
        "Complete Tide Collection",
        [
            (1201, "Complete Tide", 2004, "file", "released", 0, 7.0, 900, 5, "2004-01-01"),
            (1202, "Complete Tide 2", 2006, "file", "released", 0, 6.9, 800, 5, "2006-01-01"),
        ],
    ),
]
# Seerr knows an extra Starfall part that Radarr's collection cache lacks
EXTRA = {9001: [(105, "Starfall: Echoes", 2011, 6.9, 2500, 1, "2011-07-01")]}
PARTS = {p[0]: p for c in S for p in c[2]}


def radarr_collections():
    return [
        {
            "id": i,
            "tmdbId": cid,
            "title": name,
            "monitored": False,
            "missingMovies": 0,
            "movies": [
                {
                    "tmdbId": t,
                    "title": ti,
                    "year": y,
                    "status": st,
                    "isExisting": r != "-",
                    "isExcluded": bool(ex),
                    "ratings": {"tmdb": {"value": rt, "votes": v, "type": "user"}},
                }
                for t, ti, y, r, st, ex, rt, v, _s, _d in parts
            ],
        }
        for i, (cid, name, parts) in enumerate(S, 1)
    ]


def radarr_movies():
    return [
        {
            "tmdbId": t,
            "title": ti,
            "hasFile": r == "file",
            "monitored": r in ("file", "wanted"),
            "collection": {"tmdbId": cid},
        }
        for cid, _n, parts in S
        for t, ti, y, r, *_ in parts
        if r != "-"
    ]


def seerr_part(t, ti, rt, v, ss, d):
    return {
        "id": t,
        "mediaType": "movie",
        "title": ti,
        "releaseDate": d,
        "voteAverage": rt,
        "voteCount": v,
        "popularity": v / 100,
        "mediaInfo": {"status": ss} if ss != 1 else None,
    }


def seerr_collection(cid):
    for c, name, parts in S:
        if c == cid:
            ps = [seerr_part(t, ti, rt, v, ss, d) for t, ti, y, r, st, ex, rt, v, ss, d in parts]
            ps += [seerr_part(t, ti, rt, v, ss, d) for t, ti, y, rt, v, ss, d in EXTRA.get(cid, [])]
            return {"id": cid, "name": name, "parts": ps}


def seerr_movie(t):
    p = PARTS.get(t)
    if p:
        return {"id": t, "title": p[1], "releaseDate": p[9], "mediaInfo": {"status": p[8], "requests": []}}
    for ex in EXTRA.values():
        for e in ex:
            if e[0] == t:
                return {"id": t, "title": e[1], "releaseDate": e[6], "mediaInfo": None}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def route(self, method):
        with open(LOG, "a") as f:
            f.write(f"{method} {self.path}\n")
        scen = open(SCEN).read().strip()
        svc, _, rest = self.path.lstrip("/").partition("/")
        path = "/" + rest.split("?")[0]
        if self.headers.get("X-Api-Key") != KEYS.get(svc):
            return self.send(401, {"error": "bad key"})
        if scen == f"{svc}-down":
            return self.send(500, {"error": "boom"})
        if svc == "radarr" and method == "GET" and path == "/api/v3/collection":
            return self.send(200, radarr_collections())
        if svc == "radarr" and method == "GET" and path == "/api/v3/movie":
            return self.send(200, radarr_movies())
        if svc == "seerr" and method == "GET" and path.startswith("/api/v1/collection/"):
            d = seerr_collection(int(path.rsplit("/", 1)[1]))
            return self.send(200, d) if d else self.send(404, {"message": "not found"})
        if svc == "seerr" and method == "GET" and path.startswith("/api/v1/movie/"):
            d = seerr_movie(int(path.rsplit("/", 1)[1]))
            return self.send(200, d) if d else self.send(404, {"message": "not found"})
        if svc == "seerr" and method == "POST" and path == "/api/v1/request":
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            with open(LOG, "a") as f:
                f.write(f"BODY {json.dumps(body, sort_keys=True)}\n")
            return self.send(201, {"id": 77, "status": 2})
        return self.send(404, {"message": "no route"})

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")


srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
with open(PORTFILE + ".tmp", "w") as f:
    f.write(str(srv.server_address[1]))
import os  # noqa: E402

os.replace(PORTFILE + ".tmp", PORTFILE)
srv.serve_forever()
