#!/usr/bin/env python3
"""Fake Emby for the `media continue` tests: fake_continue.py PORTFILE LOG SCENARIO.
Serves /emby/Users/{uid}/Items/Resume, /emby/Shows/NextUp and /emby/Users/{uid}/Items?ParentId=SERIES (the series'
last played episode). Every request is logged as "METHOD path?query token=ok|bad". Only the test user's id (32 hex,
no dashes) gets data. SCENARIO file (re-read per request):
  merged       Emby 4.9+: Resume holds next episodes too (plus a series, a season, a folder, a played movie, a
               duplicate episode of a listed series); Next Up repeats one and adds one more show
  legacy       Resume = partly watched only; next episodes only under Next Up (ordered by last watched)
  empty        nothing in progress
  old          only items last watched over 14 days ago
  tonight      the first entry is tonight's B pick (episode 2014), then a movie watched today
  nextup-down  Next Up answers 500 (Resume as merged)
  resume-down  Resume answers 500
All dates are relative to the tests' clock, Sat 2026-10-10 20:00 Zurich."""

import json
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORTFILE, LOG, SCEN = sys.argv[1:4]
TOKEN = "EMBY-KEY-Ct42"
UID = "aaaa1111" + "0" * 24
T = 600000000  # ticks per minute
LONG = "The Extraordinarily Long Chronicles of Somewhere Far Away"


def mv(i, name, year, pos, run, last=None, played=False):
    ud = {"PlaybackPositionTicks": pos * T, "Played": played, "PlayCount": 0, "IsFavorite": False}
    if last:
        ud["LastPlayedDate"] = last
    return {"Id": i, "Type": "Movie", "Name": name, "ProductionYear": year, "RunTimeTicks": run * T, "UserData": ud}


def ep(i, series, sid, s, e, name, pos, run, last=None):
    ud = {"PlaybackPositionTicks": pos * T, "Played": False}
    if last:
        ud["LastPlayedDate"] = last
    return {
        "Id": i,
        "Type": "Episode",
        "Name": name,
        "SeriesName": series,
        "SeriesId": sid,
        "ParentIndexNumber": s,
        "IndexNumber": e,
        "RunTimeTicks": run * T,
        "UserData": ud,
    }


MOVIE = mv("9001", "Test Movie", 2024, 45, 110, "2026-10-10T17:00:00.0000000Z")
NEXT_LONG = ep("9201", LONG, "9200", 2, 3, "Third", 0, 50)
NEXT_LONG2 = ep("9202", LONG, "9200", 2, 4, "Fourth", 0, 50)
DARK = ep("9301", "Dark *Waters*", "9300", 1, 5, "Five", 10, 55, "2026-09-20T19:00:00Z")
OLD = mv("9003", "Old Movie", 2019, 5, 95, "2025-12-01T20:00:00Z")
EXTRA = ep("9501", "Extra Show", "9500", 3, 1, "Opener", 0, 42)
JUNK = [
    {"Id": "9100", "Type": "Series", "Name": "Some Show", "UserData": {"PlaybackPositionTicks": 0}},
    {"Id": "9400", "Type": "Season", "Name": "Season 1", "SeriesName": "Some Show", "SeriesId": "9100"},
    {"Id": "abc-def", "Type": "Movie", "Name": "Bad Id"},
    "not an item",
    mv("9002", "Watched Movie", 2020, 0, 100, "2026-10-09T20:00:00Z", played=True),
]
LAST_EP = {"9200": "2026-10-08T19:00:00.0000000Z", "9300": "2026-09-20T19:00:00Z"}  # 9500: never played

RESUME = {
    "merged": [MOVIE, JUNK[0], NEXT_LONG, JUNK[1], DARK, NEXT_LONG2, JUNK[2], JUNK[3], JUNK[4], OLD],
    "legacy": [MOVIE, DARK, OLD],
    "empty": [],
    "old": [DARK, OLD],
    "tonight": [ep("2014", "Star Voyage", "201", 1, 4, "The Fourth", 5, 45, "2026-10-10T18:00:00Z"), MOVIE, DARK],
}
NEXTUP = {"merged": [NEXT_LONG, EXTRA], "legacy": [EXTRA, NEXT_LONG], "empty": [], "old": [], "tonight": []}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, obj=None):
        body = b"" if obj is None else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_any(self, method):
        ok = self.headers.get("X-Emby-Token") == TOKEN
        with open(LOG, "a") as f:
            f.write(f"{method} {self.path} token={'ok' if ok else 'bad'}\n")
        if not ok:
            return self.reply(401, {"error": "Access token is invalid or expired."})
        if method != "GET":
            return self.reply(405, {"error": "read-only fake"})
        scen = open(SCEN).read().strip()
        base = "merged" if scen in ("nextup-down", "resume-down") else scen
        u = urllib.parse.urlsplit(self.path)
        qs = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        parts = u.path.strip("/").split("/")
        if len(parts) == 5 and parts[:2] == ["emby", "Users"] and parts[3:] == ["Items", "Resume"]:
            if parts[2] != UID:
                return self.reply(404, {"error": "user not found"})
            if scen == "resume-down":
                return self.reply(500, {"error": "boom"})
            items = RESUME[base][: int(qs.get("Limit") or 100)]
            return self.reply(200, {"Items": items, "TotalRecordCount": len(RESUME[base])})
        if u.path == "/emby/Shows/NextUp":
            if qs.get("UserId") != UID or qs.get("LegacyNextUp") != "true":
                return self.reply(200, {"Items": [], "TotalRecordCount": 0})
            if scen == "nextup-down":
                return self.reply(500, {"error": "boom"})
            return self.reply(200, {"Items": NEXTUP[base][: int(qs.get("Limit") or 100)], "TotalRecordCount": 2})
        if len(parts) == 4 and parts[:2] == ["emby", "Users"] and parts[3] == "Items" and qs.get("ParentId"):
            if parts[2] != UID:
                return self.reply(404, {"error": "user not found"})
            d = LAST_EP.get(qs["ParentId"])
            items = (
                [{"Id": "x" + qs["ParentId"], "Type": "Episode", "UserData": {"Played": True, "LastPlayedDate": d}}]
                if d
                else []
            )
            return self.reply(200, {"Items": items, "TotalRecordCount": len(items)})
        return self.reply(404, {"error": "not found"})

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")


srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
with open(PORTFILE + ".tmp", "w") as f:
    f.write(str(srv.server_address[1]))
import os  # noqa: E402

os.replace(PORTFILE + ".tmp", PORTFILE)
srv.serve_forever()
