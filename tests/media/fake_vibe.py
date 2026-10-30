#!/usr/bin/env python3
"""Fake Emby library + fake embeddings for the `media vibe` tests: fake_vibe.py PORTFILE LOG EMBLOG SCENARIO.
- /emby/Items?Recursive=true&IncludeItemTypes=Movie&StartIndex&Limit: the fictional movie library below (paged)
- /emby/Users/{uid}/Items?Filters=IsPlayed|IsResumable: his played / partly watched ids (only the test user)
- POST /v1/embeddings {"input": [...]}: fake vectors = counts of the KEYS words in the text (+ a small per-text
  tie-breaker), unit length; each input is logged to EMBLOG as "doc <title>" or "query <text>"
Emby requests need X-Emby-Token EMBY-KEY-Vb42 and are logged as "METHOD path?query". SCENARIO file (per request):
  ok          the library below
  grown       + "The Lantern Keeper" (new), - "Dust Road" (removed), "Harbor Lights" overview changed
  emby-down   every Emby request answers 500
  embed-down  every embeddings request answers 500
  series      + the fictional series library (IncludeItemTypes=Series; empty in every other scenario)
  ep-down     like series, but episodes of "Murder Valley" (206) answer 500
  gaps        like series, but "Night Precinct" (201) has only skipped episodes left (all played on one day, like a bulk
              mark-as-played) and "Weak Show" (207) skipped S01E03 (in progress, played last) before E04 (furthest)
- /emby/Users/{uid}/Items?IncludeItemTypes=Series: his series with UserData (UnplayedItemCount, Played) +
  RecursiveItemCount; ?ParentId=<series>&IncludeItemTypes=Episode&IsPlayed=true|false: its episodes (dates relative
  to now, a special in season 0, one in-progress episode)
All titles, ids and texts are fictional."""

import hashlib
import json
import math
import sys
import urllib.parse
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORTFILE, LOG, EMBLOG, SCEN = sys.argv[1:5]
TOKEN = "EMBY-KEY-Vb42"
UID = "aaaa1111" + "0" * 24
T = 600000000
KEYS = [
    "rain",
    "cozy",
    "tea",
    "alien",
    "signal",
    "language",
    "heist",
    "vault",
    "ghost",
    "lake",
    "space",
    "laugh",
    "war",
    "love",
    "robot",
    "train",
    "detective",
    "murder",
    "office",
]


def mv(i, name, year, genres, rating, mins, overview, tagline=None, tmdb=None):
    d = {
        "Id": str(i),
        "Type": "Movie",
        "Name": name,
        "ProductionYear": year,
        "Genres": genres,
        "Overview": overview,
        "RunTimeTicks": mins * T if mins else None,
        "ProviderIds": {"Tmdb": str(tmdb or 9000 + i)},
    }
    if rating is not None:
        d["CommunityRating"] = rating
    if tagline:
        d["Taglines"] = [tagline]
    return d


LIB = [
    mv(
        101,
        "Autumn Tea House",
        2019,
        ["Comedy", "Romance"],
        7.4,
        95,
        "A cozy tea shop on a rain soaked street. Rain, tea and cozy friends.",
        "Every rainy day needs a cup.",
        tmdb=5550,
    ),
    mv(
        102,
        "Rainy Window",
        2015,
        ["Drama"],
        7.9,
        104,
        "Rain rain rain on a cozy window; a family waits out the storm.",
        None,
    ),
    mv(
        103,
        "Puddle Jump",
        2021,
        ["Comedy", "Family"],
        5.2,
        88,
        "Rain rain cozy rain rain cozy tea: a soggy, silly day.",
        "Splash.",
    ),
    mv(
        104,
        "First Signal",
        2017,
        ["Drama", "Science Fiction", "Mystery"],
        8.0,
        116,
        "A linguist decodes the language of an alien signal. Alien, language, signal.",
        "Why did they come?",
    ),
    mv(
        105,
        "Signal From Beyond",
        2011,
        ["Science Fiction", "Mystery"],
        7.1,
        101,
        "An alien signal reaches a radio telescope. Alien signal space.",
        None,
    ),
    mv(
        106,
        "Star Harbor",
        2008,
        ["Science Fiction", "Action"],
        6.9,
        128,
        "Alien ships over the harbor; war in space with alien fleets.",
        None,
    ),
    mv(
        107,
        "Glass Vault",
        2012,
        ["Crime", "Comedy"],
        7.2,
        99,
        "A clumsy crew plans a heist on a glass vault. Heist, vault, laugh.",
        "Crack it.",
    ),
    mv(108, "Ghost Lake", 2010, ["Horror"], 7.0, 92, "A ghost haunts the lake. Ghost lake ghost.", None),
    mv(109, "Ghost Lake Returns", 2013, ["Horror"], 6.8, 90, "A ghost haunts the lake. Ghost lake ghost.", None),
    mv(110, "Lake Phantom", 2016, ["Horror", "Mystery"], 6.7, 97, "A ghost by the lake at night. Ghost lake.", None),
    mv(111, "Dust Road", 2005, ["Western"], 6.6, 140, "Robot train robbers cross the desert. Train robot.", None),
    mv(112, "Harbor Lights", 2018, ["Romance", "Drama"], 7.6, 112, "Love on a harbor; love and laugh.", None),
    mv(
        113, "Long Rain", 2009, ["Drama", "War"], 7.3, 165, "Rain over the trenches of a long war. Rain war rain.", None
    ),
    mv(
        114,
        "Tiny Signal",
        2020,
        ["Animation", "Science Fiction"],
        None,
        70,
        "A small robot hears an alien signal. Robot alien signal.",
        None,
    ),
    mv(
        115,
        "Cozy Little Cafe",
        2022,
        ["Komödie"],
        6.9,
        84,
        "A cozy cafe, tea and rain, laugh with friends.",
        "Pull up a chair.",
    ),
]
GROWN = [dict(x) for x in LIB if x["Name"] != "Dust Road"] + [
    mv(
        116,
        "The Lantern Keeper",
        2024,
        ["Fantasy", "Drama"],
        7.5,
        108,
        "A ghost keeps a lantern by the lake; cozy and kind. Ghost cozy.",
        None,
    )
]
for x in GROWN:
    if x["Name"] == "Harbor Lights":
        x["Overview"] = "Love and laughter on a harbor at night; love love laugh."


def sr(i, name, year, genres, rating, overview):
    return {
        "Id": str(i),
        "Type": "Series",
        "Name": name,
        "ProductionYear": year,
        "Genres": genres,
        "Overview": overview,
        "CommunityRating": rating,
        "ProviderIds": {"Tmdb": str(7000 + i)},
    }


SERIES = [
    sr(
        201,
        "Night Precinct",
        2019,
        ["Crime", "Drama"],
        8.1,
        "A detective hunts a murder suspect through a city at night. Detective, murder.",
    ),
    sr(
        202,
        "Cold Case Lake",
        2016,
        ["Krimi", "Mystery"],
        7.6,
        "A detective reopens an old murder at the lake. Murder, lake, detective.",
    ),
    sr(203, "Harbor Detective", 2012, ["Crime"], 7.8, "Detective work on the harbor: murder after murder. Detective."),
    sr(204, "Office Laughs", 2014, ["Comedy"], 7.9, "An office full of laugh lines. Office laugh office laugh."),
    sr(
        205,
        "Star Frontier",
        2018,
        ["Sci-Fi & Fantasy", "Action & Adventure"],
        8.4,
        "Alien fleets and war in space. Space alien war.",
    ),
    sr(
        206,
        "Murder Valley",
        2020,
        ["Крими", "Драма"],
        6.9,
        "Murder in a quiet valley; a detective stays. Murder detective.",
    ),
    sr(207, "Weak Show", 2010, ["Krieg & Politik"], 5.0, "War and politics, badly. War war."),
    sr(208, "Specials Left", 2011, ["Drama"], 7.0, "A love story in ten parts. Love love."),
]
# his episodes per series: (season, episode, played, days since last play or None, resume minutes)
EPS = {
    "202": [(1, 1, True, 100, 0)] + [(1, e, False, None, 0) for e in range(2, 11)],
    "203": [(1, e, True, 400, 0) for e in range(1, 13)],
    "204": [(1, e, True, 5, 0) for e in range(1, 21)] + [(2, e, False, None, 0) for e in range(1, 11)],
    "205": [(1, e, True, 60 + (5 - e), 0) for e in range(1, 6)]
    + [(1, e, False, None, 0) for e in range(6, 9)]
    + [(0, 1, False, None, 0)],
    "206": [(1, e, True, 31, 0) for e in range(1, 5)]
    + [(1, 5, False, 30, 20)]
    + [(1, e, False, None, 0) for e in range(6, 11)],
    "208": [(1, e, True, 40, 0) for e in range(1, 11)] + [(0, 1, False, None, 0)],
}
for _sid in ("201", "207"):
    EPS[_sid] = [(1, e, False, None, 0) for e in range(1, 7)]
EPS_GAPS = {
    "201": [(1, e, True, 100, 0) for e in range(1, 5)]
    + [(2, 1, False, None, 0), (2, 2, False, None, 0)]
    + [(2, e, True, 100, 0) for e in range(3, 5)]
    + [(3, e, True, 100, 0) for e in range(1, 3)],
    "207": [
        (1, 1, True, 50, 0),
        (1, 2, True, 50, 0),
        (1, 3, False, 45, 10),
        (1, 4, True, 60, 0),
        (1, 5, False, None, 0),
        (1, 6, False, None, 0),
    ],
}


def episodes(sid, sc="series"):
    now = datetime.now(UTC)
    out = []
    for s, e, played, ago, res in EPS_GAPS[sid] if sc == "gaps" and sid in EPS_GAPS else EPS.get(sid, []):
        ud = {"Played": played, "PlaybackPositionTicks": res * T}
        if ago is not None:
            ud["LastPlayedDate"] = (now - timedelta(days=ago)).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")
        out.append(
            {
                "Id": f"{sid}{s:02d}{e:02d}",
                "Type": "Episode",
                "Name": f"Part {e}",
                "SeriesId": sid,
                "SeriesName": next(x["Name"] for x in SERIES if x["Id"] == sid),
                "ParentIndexNumber": s,
                "IndexNumber": e,
                "RunTimeTicks": 45 * T,
                "LocationType": "FileSystem",
                "UserData": ud,
            }
        )
    return out


def user_series(sc="series"):
    out = []
    for x in SERIES:
        eps = episodes(x["Id"], sc)
        un = sum(1 for e in eps if not e["UserData"]["Played"])
        out.append(dict(x, RecursiveItemCount=len(eps), UserData={"UnplayedItemCount": un, "Played": un == 0}))
    return out


PLAYED = {"102"}  # Rainy Window: he has seen it
RESUMABLE = {"105"}  # Signal From Beyond: started


def vecof(text):
    low = text.lower()
    v = [float(low.count(k)) for k in KEYS]
    h = hashlib.sha256(text.encode()).digest()
    v += [h[i] / 25500.0 for i in range(4)]
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


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

    def scen(self):
        try:
            return open(SCEN).read().strip()
        except OSError:
            return "ok"

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        if u.path != "/v1/embeddings":
            with open(LOG, "a") as f:
                f.write(f"POST {self.path}\n")
            return self.send(405, {"error": "GET only"})
        if self.scen() == "embed-down":
            return self.send(500, {"error": "down"})
        texts = json.loads(body)["input"]
        with open(EMBLOG, "a") as f:
            for t in texts:
                f.write(
                    (
                        "doc " + t.split("|", 1)[0].replace("title:", "").strip()
                        if t.startswith("title:")
                        else "query " + t.split("query:", 1)[-1].strip()
                    )
                    + "\n"
                )
        return self.send(200, {"data": [{"index": i, "embedding": vecof(t)} for i, t in enumerate(texts)]})

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        with open(LOG, "a") as f:
            f.write(f"GET {u.path}?{u.query}\n")
        if u.path == "/health":
            return self.send(200, {"status": "ok"})
        sc = self.scen()
        if sc == "emby-down":
            return self.send(500, {"error": "down"})
        if self.headers.get("X-Emby-Token") != TOKEN:
            return self.send(401, {"error": "bad token"})
        if u.path == "/emby/Items":
            if qs.get("IncludeItemTypes", [""])[0] == "Series":
                lib = SERIES if sc in ("series", "ep-down", "gaps") else []
            else:
                lib = GROWN if sc == "grown" else LIB
            start, lim = int(qs.get("StartIndex", ["0"])[0]), int(qs.get("Limit", ["1000"])[0])
            return self.send(200, {"Items": lib[start : start + lim], "TotalRecordCount": len(lib)})
        if u.path == f"/emby/Users/{UID}/Items" and qs.get("IncludeItemTypes", [""])[0] == "Series":
            return self.send(200, {"Items": user_series(sc), "TotalRecordCount": len(SERIES)})
        if u.path == f"/emby/Users/{UID}/Items" and qs.get("ParentId"):
            sid = qs["ParentId"][0]
            if sc == "ep-down" and sid == "206":
                return self.send(500, {"error": "down"})
            want = qs.get("IsPlayed", [""])[0]
            eps = [e for e in episodes(sid, sc) if not want or e["UserData"]["Played"] == (want == "true")]
            return self.send(200, {"Items": eps, "TotalRecordCount": len(eps)})
        if u.path == f"/emby/Users/{UID}/Items":
            f = qs.get("Filters", [""])[0]
            ids = PLAYED if f == "IsPlayed" else RESUMABLE if f == "IsResumable" else set()
            return self.send(
                200, {"Items": [{"Id": i, "Type": "Movie"} for i in sorted(ids)], "TotalRecordCount": len(ids)}
            )
        if u.path.startswith("/emby/Users/"):
            return self.send(200, {"Items": [], "TotalRecordCount": 0})
        return self.send(404, {"error": "not found"})

    def do_PUT(self):
        with open(LOG, "a") as f:
            f.write(f"PUT {self.path}\n")
        self.send(405, {"error": "GET only"})

    do_DELETE = do_PUT


srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
with open(PORTFILE, "w") as f:
    f.write(str(srv.server_address[1]))
srv.serve_forever()
