#!/usr/bin/env python3
"""Fixture editor for the ready-watch tests: ready_fx.py FILE CMD ARGS...

  init                              users robin(1), Robin(9), PersonG(2) (robin = the fixture profile's seerr_user); requests; movie details; history
  movie TMDB STATUS [STATUS4K]      set the Seerr media status of a movie in every request
  addmovie USER TMDB TITLE DATE STATUS [REQSTATUS]   add a movie request (+ details)
  imp ID DATE SHOW S E              Sonarr import (downloadFolderImported) at DATE (ISO, Z)
  upg ID DATE SHOW S E              'Upgrade' file delete 1 s before DATE + import at DATE
  grab ID DATE SHOW S E             a grabbed event (must be ignored)
Shows: sh = Slow Horses (tvdb 710), pl = Pluribus (tmdb 7200 only), ls = PersonG Show (PersonG's request).
"""  # noqa: E501

import json
import sys
from datetime import datetime, timedelta

F, CMD, ARGS = sys.argv[1], sys.argv[2], sys.argv[3:]
SHOWS = {
    "sh": (31, "Slow Horses", 710, 7100),
    "pl": (32, "Pluribus", None, 7200),
    "ls": (33, "PersonG Show", 720, 7300),
}


def mreq(rid, tmdb, status, req_status=2, is4k=False, status4k=1):
    return {
        "id": rid,
        "type": "movie",
        "status": req_status,
        "is4k": is4k,
        "media": {"mediaType": "movie", "tmdbId": tmdb, "tvdbId": None, "status": status, "status4k": status4k},
    }


def treq(rid, tmdb, tvdb, status=5, req_status=2):
    return {
        "id": rid,
        "type": "tv",
        "status": req_status,
        "is4k": False,
        "media": {"mediaType": "tv", "tmdbId": tmdb, "tvdbId": tvdb, "status": status, "status4k": 1},
    }


def rec(rid, date, show, s, e, event, data=None):
    sid, title, tvdb, tmdb = SHOWS[show]
    return {
        "id": int(rid),
        "date": date,
        "eventType": event,
        "seriesId": sid,
        "episodeId": sid * 10000 + int(s) * 100 + int(e),
        "series": {"id": sid, "title": title, "tvdbId": tvdb, "tmdbId": tmdb},
        "episode": {"seasonNumber": int(s), "episodeNumber": int(e)},
        "data": data or {},
    }


if CMD == "init":
    d = {
        "users": [
            {"id": 1, "displayName": "robin"},
            {"id": 9, "displayName": "Robin"},
            {"id": 2, "displayName": "PersonG"},
        ],
        "requests": {
            "1": [
                mreq(1, 501, 5),
                mreq(2, 502, 3),
                mreq(3, 503, 3, req_status=3),
                mreq(4, 504, 5, is4k=True),
                treq(5, 7100, 710),
                treq(6, 7200, None, status=3),
            ],
            "9": [mreq(20, 901, 3)],
            "2": [mreq(10, 601, 3), treq(11, 7300, 720)],
        },
        "movies": {
            str(i): {"id": i, "title": t, "releaseDate": r}
            for i, t, r in [
                (501, "Already Here", "2024-03-01"),
                (502, "Dune: Part Three", "2026-12-18"),
                (503, "Declined Film", "2025-01-01"),
                (504, "Four K Film", "2023-05-05"),
                (601, "PersonG's Movie", "2026-02-02"),
                (901, "Capitalized Robin Film", "2020-01-01"),
            ]
        },
        "history": [rec(1, "2026-10-10T09:30:00Z", "sh", 6, 4, "downloadFolderImported")],
    }
else:
    d = json.load(open(F))
    if CMD == "movie":
        for reqs in d["requests"].values():
            for r in reqs:
                if r["media"]["tmdbId"] == int(ARGS[0]):
                    r["media"]["status"] = int(ARGS[1])
                    if len(ARGS) > 2:
                        r["media"]["status4k"] = int(ARGS[2])
    elif CMD == "addmovie":
        user, tmdb, title, date, status = ARGS[:5]
        reqs = d["requests"].setdefault(user, [])
        reqs.append(mreq(100 + len(reqs), int(tmdb), int(status), int(ARGS[5]) if len(ARGS) > 5 else 2))
        d["movies"][tmdb] = {"id": int(tmdb), "title": title, "releaseDate": date}
    elif CMD in ("imp", "upg", "grab"):
        rid, date, show, s, e = ARGS
        if CMD == "upg":
            t = datetime.fromisoformat(date.replace("Z", "+00:00")) - timedelta(seconds=1)
            d["history"].append(
                rec(
                    int(rid) + 100000,
                    t.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    show,
                    s,
                    e,
                    "episodeFileDeleted",
                    {"reason": "Upgrade"},
                )
            )
        d["history"].append(rec(rid, date, show, s, e, "grabbed" if CMD == "grab" else "downloadFolderImported"))
    else:
        sys.exit(f"unknown command {CMD}")
json.dump(d, open(F, "w"), indent=1)
