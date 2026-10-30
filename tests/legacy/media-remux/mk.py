"""Fixture builder for the media-remux tests: python3 mk.py DIR SCENARIO [k=v ...] -> DIR/routes.json."""

import json
import sys
import urllib.parse

G = 10**9
D, SC = sys.argv[1], sys.argv[2]
kv = dict(a.split("=", 1) for a in sys.argv[3:])
SCORES = {
    "German DL": 550000,
    "English": 200000,
    "2160p": 48000,
    "1080p": 36000,
    "HDR": 500,
    "TrueHD": 2750,
    "DTS": 1250,
    "UHD Bluray Tier 01": 1800,
    "Remux Tier 01": 1450,
    "LQ": -260000,
    "DV (w/o HDR fallback)": -260000,
    "Language: Not Original, English or German": -1000000,
}


def cfs(*names):
    return [{"name": n} for n in names]


def score(names):
    return sum(SCORES.get(n, 0) for n in names)


def movie(mid, title, year, q, size, prof=1, file_id=None):
    mf = {"id": file_id or mid * 10, "size": size, "quality": {"quality": {"name": q}}}
    return {"id": mid, "title": title, "year": year, "qualityProfileId": prof, "hasFile": True, "movieFile": mf}


def rel(guid, q, size, names, rej=(), proto="usenet", mid=None, name=None):
    return {
        "guid": guid,
        "indexerId": 7,
        "indexer": "nzb",
        "title": name or f"Release.{guid}",
        "protocol": proto,
        "quality": {"quality": {"name": q}},
        "size": size,
        "customFormats": cfs(*names),
        "customFormatScore": score(names),
        "rejections": list(rej),
        "downloadAllowed": True,
        "mappedMovieId": mid,
    }


CUT = ["Existing file meets cutoff: Prioritize Custom Formats"]
movies = [
    movie(1, "Big Epic", 2001, "Remux-2160p", 80 * G),  # -> UHD encode (higher score: normal import)
    movie(2, "Old Drama", 1999, "Remux-1080p", 30 * G),  # only 1080p + SDR UHD releases -> keep (2160p HDR only)
    movie(3, "On Best", 2000, "Remux-2160p", 70 * G, prof=8),  # never touched
    movie(4, "Too New", 2023, "Remux-2160p", 60 * G),  # too new
    movie(5, "Already Encode", 2005, "Bluray-1080p", 10 * G),  # not a remux
    movie(6, "Nothing Fits", 1990, "Remux-1080p", 25 * G),
]  # only bad releases -> keep
files = {
    1: ["German DL", "English", "2160p", "HDR", "TrueHD", "Remux Tier 01"],
    2: ["German DL", "English", "1080p", "TrueHD", "Remux Tier 01"],
    6: ["German DL", "English", "1080p", "TrueHD"],
}
r = {
    "/api/v3/qualityprofile/1": {"formatItems": [{"name": k, "score": v} for k, v in SCORES.items()]},
    "/api/v3/movie": movies,
    "/api/v3/queue?page=1&pageSize=200&includeMovie=false": {
        "records": [{"movieId": 900 + i, "sizeleft": 0} for i in range(int(kv.get("queue", 2)))],
        "totalRecords": int(kv.get("queue", 2)),
    },
}
for m in movies:
    r[f"/api/v3/movie/{m['id']}"] = m
    n = files.get(m["id"], [])
    r[f"/api/v3/moviefile/{m['id'] * 10}"] = {
        "id": m["id"] * 10,
        "customFormats": cfs(*n),
        "customFormatScore": score(n),
    }
U = ["German DL", "English", "2160p", "HDR", "TrueHD", "UHD Bluray Tier 01"]
r["/api/v3/release?movieId=1"] = [
    rel("uhd-good", "Bluray-2160p", 25 * G, U, CUT),
    rel("uhd-big", "Bluray-2160p", 35 * G, U + ["DTS"], CUT),  # over 30 GB
    rel("uhd-dvonly", "WEBDL-2160p", 20 * G, U + ["DV (w/o HDR fallback)"], CUT),  # DV, no HDR
    rel("uhd-lq", "Bluray-2160p", 15 * G, U + ["LQ"], CUT),  # LQ
    rel("uhd-torrent", "Bluray-2160p", 22 * G, U, CUT, proto="torrent"),
    # HDR custom format but "SDR" in the name (would score best): never picked
    rel("uhd-sdrname", "WEBDL-2160p", 18 * G, U + ["DTS"], CUT, name="Release.uhd-sdrname.2160p.HDR.SDR"),
    # 1080p: smaller and German, but never a replacement (2160p HDR only)
    rel("hd", "Bluray-1080p", 12 * G, ["German DL", "English", "1080p", "DTS"], CUT),
]
r["/api/v3/release?movieId=2"] = [
    rel("hd-en", "Bluray-1080p", 9 * G, ["English", "1080p", "TrueHD"], CUT),  # loses German
    rel("hd-de", "Bluray-1080p", 11 * G, ["German DL", "English", "1080p", "DTS"], CUT),
    rel("hd-de-blk", "Bluray-1080p", 10 * G, ["German DL", "English", "1080p", "TrueHD"], ["Release is blocklisted"]),
    rel("sdr-uhd", "Bluray-2160p", 20 * G, ["German DL", "English", "2160p"], CUT),
]  # no HDR
r["/api/v3/release?movieId=6"] = [
    rel("wrong-lang", "Bluray-1080p", 8 * G, ["1080p", "Language: Not Original, English or German"], CUT),
    rel("too-close", "Bluray-1080p", 22 * G, ["German DL", "English", "1080p"], CUT),
]  # saves too little
# 29.10, real names and Radarr's real parse: the interactive search returned other films "matched by Id"
TF = {
    "id": 7,
    "title": "Transformers: Age of Extinction",
    "originalTitle": "Transformers: Age of Extinction",
    "year": 2014,
    "inCinemas": "2014-06-25T00:00:00Z",
    "digitalRelease": "2014-06-26T00:00:00Z",
    "physicalRelease": "2014-11-06T00:00:00Z",
    "alternateTitles": [
        {"title": t}
        for t in (
            "Transformers 4",
            "Transformers 4: \u00c4ra des Untergangs",
            "Transformers - \u00c4ra des Untergangs",
            "Transformers 4 - Age of Extinction",
        )
    ],
}
CM = {
    "id": 8,
    "title": "Catch Me If You Can",
    "originalTitle": "Catch Me If You Can",
    "year": 2002,
    "inCinemas": "2002-12-16T00:00:00Z",
    "digitalRelease": "2005-09-01T00:00:00Z",
    "physicalRelease": "2003-05-06T00:00:00Z",
    "alternateTitles": [{"title": "Catch Me If You Can - Mein Leben auf der Flucht"}],
}
EXT = "Extinction.2018.German.DL.EAC3.2160p.DV.HDR.NF.WEB.H265-ZeroTwo"
CAT = "Catch.Me.2018.GERMAN.DUBBED.DL.HDR.2160p.WEB.h265-TMSF"
FIRE = "Transformers.4.Aera.des.Untergangs.2014.German.OPUS.DL.2160p.UHDBDRiP.HDR.AV1-Firecooler"
PARSED = {  # GET /api/v3/parse, as Radarr answered on 29.10 (trimmed)
    EXT: {"parsedMovieInfo": {"movieTitles": ["Extinction"], "year": 2018}, "movie": None},
    CAT: {"parsedMovieInfo": {"movieTitles": ["Catch Me"], "year": 2018}, "movie": None},
    FIRE: {
        "parsedMovieInfo": {"movieTitles": ["Transformers 4 Aera des Untergangs"], "year": 2014},
        "movie": {"id": 7},
    },
}
REMUX = ["German DL", "English", "2160p", "HDR", "Remux Tier 01"]
WEB = ["German DL", "English", "2160p", "HDR", "TrueHD", "UHD Bluray Tier 01"]  # scores above the remux
if SC.startswith("mismatch"):
    mv = [
        dict(movie(7, TF["title"], 2014, "Remux-2160p", int(87.8 * G)), **TF),
        dict(movie(8, CM["title"], 2002, "Remux-2160p", int(92.3 * G)), **CM),
    ]
    r["/api/v3/movie"] = mv
    for m in mv:
        r[f"/api/v3/movie/{m['id']}"] = m
        r[f"/api/v3/moviefile/{m['id'] * 10}"] = {
            "id": m["id"] * 10,
            "customFormats": cfs(*REMUX),
            "customFormatScore": score(REMUX),
        }
        r[f"/api/v3/history/movie?movieId={m['id']}&eventType=4"] = []
        r[f"/api/v3/queue/details?movieId={m['id']}"] = []
    r["/api/v3/release?movieId=7"] = [
        rel("tf-extinction", "WEBDL-2160p", int(9.4 * G), WEB + ["DTS"], CUT, mid=7, name=EXT),  # best score, wrong
        rel(
            "tf-firecooler",
            "Bluray-2160p",
            int(24.6 * G),
            ["German DL", "English", "2160p", "HDR"],
            CUT,
            mid=7,
            name=FIRE,
        ),
    ]
    r["/api/v3/release?movieId=8"] = [rel("cm-2018", "WEBDL-2160p", int(12.6 * G), WEB, CUT, mid=8, name=CAT)]
    if kv.get("noparse") != "1":
        for n, p in PARSED.items():
            r["/api/v3/parse?title=" + urllib.parse.quote(n, safe="")] = p
if SC == "mismatch-imported":  # Radarr itself imported the wrong film over movie 7's remux (a CF upgrade)
    r["/api/v3/movie/7"] = dict(
        r["/api/v3/movie/7"],
        movieFile={
            "id": 71,
            "size": int(9.4 * G),
            "quality": {"quality": {"name": "WEBDL-2160p"}},
            "sceneName": EXT,
            "relativePath": "Transformers Age of Extinction (2014) - [WEBDL-2160p]-ZeroTwo.mkv",
        },
    )
if SC == "imported":  # movie 1's file replaced by the grabbed encode; movie 2 blocked as not an upgrade
    m1 = movie(1, "Big Epic", 2001, "Bluray-2160p", 25 * G, file_id=99)
    r["/api/v3/movie/1"] = m1
    r["/api/v3/movie"] = [m1] + movies[1:]
for mid in (1, 2, 6):
    r[f"/api/v3/history/movie?movieId={mid}&eventType=1"] = [
        {"sourceTitle": f"Release.{g}", "downloadId": f"DL{mid}", "date": "2026-10-29T01:00:00Z"}
        for g in ("uhd-good", "hd-de")
    ]
    r[f"/api/v3/history/movie?movieId={mid}&eventType=4"] = []
    r[f"/api/v3/queue/details?movieId={mid}"] = []
if SC == "imported":
    r["/api/v3/queue/details?movieId=2"] = [
        {
            "movieId": 2,
            "downloadId": "DL2",
            "status": "completed",
            "sizeleft": 0,
            "trackedDownloadState": "importBlocked",
            "statusMessages": [{"messages": ["Not a Custom Format upgrade for existing movie file(s)"]}],
        }
    ]
    r["/api/v3/manualimport"] = [
        {
            "path": "/dl/x.mkv",
            "size": 11 * G,
            "rejections": [{"reason": "Not a Custom Format upgrade for existing movie file(s)", "type": "permanent"}],
        }
    ]
if SC == "failed":
    r["/api/v3/history/movie?movieId=1&eventType=4"] = [{"downloadId": "DL1"}]
for k in [k for k in r if k.startswith("/api/v3/release?movieId=")]:
    m = r[f"/api/v3/movie/{k.split('=')[1]}"]
    for x in r[k]:
        key = "/api/v3/parse?title=" + urllib.parse.quote(x["title"], safe="")
        if x["title"].startswith("Release.") and key not in r:
            r[key] = {"parsedMovieInfo": {"movieTitles": [m["title"]], "year": m["year"]}, "movie": {"id": m["id"]}}
with open(f"{D}/routes.json", "w") as f:
    json.dump(r, f)
