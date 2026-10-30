#!/usr/bin/env python3
"""Fixtures for the `media group` tests (stdlib only).

  group_fx.py plays DIR            add the friends' Playback Reporting plays to DIR/playback_reporting.db
                                   (DIR built by `tonight_fx.py build`)
  group_fx.py serve PORT_FILE LOG  fake Emby API under /emby: GET /Users and /Users/{id}/Items?Filters=IsPlayed|IsResumable
                                   (X-Emby-Token must be EMBY-KEY-Pq55, else 401; anything but GET gets 405; user Broken
                                   gets 500). Every request is logged as "METHOD /path?query".

Users: Robin (the owner, Seerr user 1's linked account), Friend, Ana, Ben, Zoë, Kim, Kimi, Kim2, Oldie, Broken,
Sleeper (disabled). Clock as tonight_fx: Sat 2026-10-10 20:00 Zurich.
"""  # noqa: E501

import json
import os
import sqlite3
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KEY = "EMBY-KEY-Pq55"


def g(prefix):
    return prefix + "0" * (32 - len(prefix))


OWNER, FRIEND, ANA, BEN, ZOE = g("aaaa1111"), g("bbbb2222"), g("cccc3333"), g("dddd4444"), g("eeee5555")
KIM, KIMI, KIM2, OLDIE, BROKEN, SLEEPER = (
    g("ffff6666"),
    g("abab7777"),
    g("cdcd8888"),
    g("efef9999"),
    g("12121212"),
    g("34343434"),
)
USERS = [
    ("Robin", OWNER),
    ("Friend", FRIEND),
    ("Ana", ANA),
    ("Ben", BEN),
    ("Zoë", ZOE),
    ("Kim", KIM),
    ("Kimi", KIMI),
    ("Kim2", KIM2),
    ("Oldie", OLDIE),
    ("Broken", BROKEN),
    ("Sleeper", SLEEPER),
]
# Emby's played / resumable movies per user: (item id, TMDB id). Ana's 999 is another version of Solid Western (TMDB 122).  # noqa: E501
PLAYED = {OWNER: [("101", 329865)], FRIEND: [("114", 114)], ANA: [("103", 693134), ("999", 122)]}
RESUMABLE = {OWNER: [("113", 113)], ZOE: [("108", 108)]}


def dashed(x):
    return f"{x[:8]}-{x[8:12]}-{x[12:16]}-{x[16:20]}-{x[20:]}".upper()


PLAYS = [  # Playback Reporting: (DateCreated, UserId, ItemId, ItemType, PlayDuration)
    ("2026-10-01 20:00:00.0000000", dashed(BEN), "109", "Movie", 2400),  # Ben saw Funny One (not marked played)
    ("2026-10-02 20:00:00.0000000", BEN, "110", "Movie", 300),  # 5 min of Krimi Film: not seen
    ("2026-09-01 20:00:00.0000000", ZOE, "2011", "Episode", 600),  # Zoë is active (an episode)
    ("2026-08-01 20:00:00.0000000", ANA, "103", "Movie", 6000),
    ("2025-01-01 20:00:00.0000000", OLDIE, "110", "Movie", 3000),  # Oldie: not active, but saw Krimi Film
    ("2026-10-03 20:00:00.0000000", SLEEPER, "106", "Movie", 6000),  # disabled: never in the default group
]


def plays(d):
    pr = sqlite3.connect(os.path.join(d, "playback_reporting.db"))
    pr.executemany("INSERT INTO PlaybackActivity VALUES (?,?,?,?,'x','DirectPlay','Emby Web','Firefox',?,0)", PLAYS)
    pr.commit()
    pr.close()


def serve(port_file, log):
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
            with open(log, "a") as f:
                f.write(f"{self.command} {self.path}\n")
            if self.command != "GET":
                return self.send(405, {"message": "read-only fake"})
            if self.headers.get("X-Emby-Token") != KEY:
                return self.send(401, {"message": "bad key"})
            u = urllib.parse.urlparse(self.path)
            parts, qs = u.path.strip("/").split("/"), urllib.parse.parse_qs(u.query)
            if parts == ["emby", "Users"]:
                return self.send(
                    200,
                    [
                        {
                            "Name": n,
                            "Id": i,
                            "Policy": {"IsDisabled": i == SLEEPER, "IsHidden": True},
                            "LastActivityDate": "2026-10-09T20:00:00.0000000Z"
                            if i != OLDIE
                            else "2025-01-01T20:00:00.0000000Z",
                        }
                        for n, i in USERS
                    ],
                )
            if len(parts) == 4 and parts[:2] == ["emby", "Users"] and parts[3] == "Items":
                uid = parts[2].lower().replace("-", "")
                if uid == BROKEN:
                    return self.send(500, {"message": "boom"})
                if uid not in dict((i, n) for n, i in USERS):
                    return self.send(404, {"message": "no such user"})
                if qs.get("IncludeItemTypes") != ["Movie"] or qs.get("Recursive") != ["true"]:
                    return self.send(400, {"message": "movies only"})
                src = {"IsPlayed": PLAYED, "IsResumable": RESUMABLE}.get((qs.get("Filters") or [""])[0])
                if src is None:
                    return self.send(400, {"message": "unknown filter"})
                items = [
                    {"Id": i, "Type": "Movie", "ProviderIds": {"Tmdb": str(t), "Imdb": "tt0"}}
                    for i, t in src.get(uid, [])
                ]
                return self.send(200, {"Items": items, "TotalRecordCount": len(items)})
            return self.send(404, {"message": "not found"})

        do_GET = do_POST = do_PUT = do_DELETE = handle_any

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    with open(port_file + ".tmp", "w") as f:
        f.write(str(srv.server_address[1]))
    os.replace(port_file + ".tmp", port_file)
    srv.serve_forever()


if __name__ == "__main__":
    if sys.argv[1] == "plays":
        plays(sys.argv[2])
    elif sys.argv[1] == "serve":
        serve(sys.argv[2], sys.argv[3])
