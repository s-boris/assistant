#!/usr/bin/env python3
"""Fake Emby for the play tests: fake_emby.py PORTFILE LOG HA_STATE SCENARIO.
Every request is logged (method, path?query, token ok/bad). The TV's Emby session exists only while the fake HA
state says the TV's source is "Emby". SCENARIO file: "ok" (default), "noplay" (POST accepted, never plays). SCENARIO.fast ("lan" or "wan"): a
remote-controllable FASTBOX session from that kind of address (only while the file exists)."""  # noqa: E501

import json
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORTFILE, LOG, HA_STATE, SCEN = sys.argv[1:5]
TOKEN = "EMBY-KEY-Pt55"
PLAYING = {}
HD = [{"Type": "Video", "Width": 1920}]
UHD = [{"Type": "Video", "Width": 3840}]
ITEMS = {
    "555": {
        "Id": "555",
        "Type": "Movie",
        "Name": "Test Movie",
        "RunTimeTicks": 72000000000,
        "MediaSources": [{"Bitrate": 20000000, "Name": "Test Movie 1080p", "MediaStreams": HD}],
    },
    "558": {
        "Id": "558",
        "Type": "Movie",
        "Name": "Big Remux",
        "RunTimeTicks": 90000000000,
        "MediaSources": [
            {"Bitrate": 85300000, "Name": "Big Remux", "Path": "/m/Big Remux [Remux-2160p].mkv", "MediaStreams": UHD},
            {"Bitrate": 9000000, "MediaStreams": HD},
        ],
    },
    "556": {"Id": "556", "Type": "Episode", "Name": "Pilot", "RunTimeTicks": 30000000000},
    "777": {"Id": "777", "Type": "Series", "Name": "Some Show"},
}
USERDATA = {
    "555": {"PlaybackPositionTicks": 36000000000, "Played": False},
    "556": {"PlaybackPositionTicks": 0, "Played": False},
}


def tv_in_emby():
    with open(HA_STATE) as f:
        st = json.load(f)
    tv = st[st["_tv"]]
    return tv["state"] == "on" and (tv.get("attributes") or {}).get("source") == "Emby"


def sessions():
    out = [
        {
            "Id": "web1",
            "Client": "Emby Web",
            "DeviceName": "Firefox",
            "SupportsRemoteControl": True,
            "PlayableMediaTypes": ["Audio", "Video"],
            "UserId": "u2",
            "LastActivityDate": "2026-10-18T10:00:00Z",
        },
        {
            "Id": "old",
            "Client": "Emby for LG",
            "DeviceName": "LG TESTTV",
            "SupportsRemoteControl": False,
            "PlayableMediaTypes": ["Video"],
            "UserId": "u1",
            "LastActivityDate": "2026-10-17T10:00:00Z",
        },
    ]
    try:
        fast = open(SCEN + ".fast").read().strip()
    except OSError:
        fast = ""
    if fast:
        ep = "192.0.2.50:41000" if fast == "lan" else "203.0.113.9:41000"  # LAN = the profile fixture's prefix
        f = {
            "Id": "f1",
            "Client": "Emby for Android",
            "DeviceName": "Some FASTBOX",
            "SupportsRemoteControl": True,
            "PlayableMediaTypes": ["Audio", "Video"],
            "UserId": "u1",
            "RemoteEndPoint": ep,
            "LastActivityDate": "2026-10-18T19:00:00Z",
        }
        if "f1" in PLAYING:
            f["NowPlayingItem"] = {"Id": PLAYING["f1"]}
        out.append(f)
    else:
        PLAYING.pop("f1", None)
    if not tv_in_emby():
        PLAYING.pop("s1", None)  # the TV left Emby (or a test reset it): nothing plays there
    else:
        s = {
            "Id": "s1",
            "Client": "Emby for LG",
            "DeviceName": "LG TESTTV",
            "SupportsRemoteControl": True,
            "PlayableMediaTypes": ["Audio", "Video"],
            "UserId": "u1",
            "LastActivityDate": "2026-10-18T20:00:00Z",
        }
        if "s1" in PLAYING:
            s["NowPlayingItem"] = {"Id": PLAYING["s1"]}
        out.append(s)
    return out


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
        u = urllib.parse.urlsplit(self.path)
        qs = urllib.parse.parse_qs(u.query)
        parts = u.path.strip("/").split("/")
        if method == "GET" and u.path == "/emby/Items":
            ids = (qs.get("Ids") or [""])[0].split(",")
            return self.reply(200, {"Items": [ITEMS[i] for i in ids if i in ITEMS], "TotalRecordCount": 1})
        if method == "GET" and u.path == "/emby/Sessions":
            ss = sessions()
            if qs.get("Id"):
                ss = [s for s in ss if s["Id"] == qs["Id"][0]]
            return self.reply(200, ss)
        if method == "GET" and len(parts) == 5 and parts[1] == "Users" and parts[3] == "Items":
            it = dict(ITEMS.get(parts[4]) or {})
            it["UserData"] = USERDATA.get(parts[4], {})
            return self.reply(200, it)
        if method == "POST" and len(parts) == 4 and parts[1] == "Sessions" and parts[3] == "Playing":
            if open(SCEN).read().strip() != "noplay":
                PLAYING[parts[2]] = (qs.get("ItemIds") or [""])[0]
            else:
                PLAYING.pop(parts[2], None)
            return self.reply(204)
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
