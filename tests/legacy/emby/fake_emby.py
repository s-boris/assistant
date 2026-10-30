#!/usr/bin/env python3
"""Fake Emby for ~/opt/emby-tests: synthetic sessions/library, scenario file switches the answers.
argv: PORTFILE LOGFILE SCENARIOFILE. Log line per request: METHOD PATH?QUERY token=ok|bad|none."""

import json
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORTFILE, LOG, SCEN = sys.argv[1:4]
KEY = "EMBY-KEY-Tq93"


def vid(w, h, codec="hevc", rng="SDR", ext="None", br=None):
    return {
        "Type": "Video",
        "Index": 0,
        "Codec": codec,
        "Width": w,
        "Height": h,
        "VideoRange": rng,
        "ExtendedVideoType": ext,
        "BitRate": br,
        "IsTextSubtitleStream": False,
    }


def aud(i, codec, profile=None, ch=6):
    return {
        "Type": "Audio",
        "Index": i,
        "Codec": codec,
        "Profile": profile,
        "Channels": ch,
        "IsTextSubtitleStream": False,
    }


def sub(i, codec, text):
    return {"Type": "Subtitle", "Index": i, "Codec": codec, "IsTextSubtitleStream": text, "IsExternal": False}


HW = {
    "VideoDecoderIsHardware": True,
    "VideoEncoderIsHardware": True,
    "VideoDecoderHwAccel": "QuickSync",
    "VideoEncoderHwAccel": "QuickSync",
}
STEP = lambda t, n: {"StepType": t, "Name": n}  # noqa: E731

BIG_STREAMS = [
    vid(3840, 2160, rng="DolbyVision", ext="DolbyVision", br=85000000),
    aud(1, "truehd", "TrueHD"),
    sub(2, "subrip", True),
    sub(3, "PGSSUB", False),
]
ITEMS = {
    "i1": {
        "Id": "i1",
        "Type": "Movie",
        "Name": "Big Movie",
        "ProductionYear": 2026,
        "Width": 1920,
        "Height": 1080,
        "Bitrate": 9000000,
        "MediaStreams": [vid(1920, 1080)],
        "_sources": [
            {"Id": "ms-big-hd", "Bitrate": 9000000, "MediaStreams": [vid(1920, 1080, br=9000000)]},
            {"Id": "ms-big-4k", "Bitrate": 85000000, "MediaStreams": BIG_STREAMS},
        ],
    },
    "i2": {
        "Id": "i2",
        "Type": "Movie",
        "Name": "Wide Film",
        "ProductionYear": 2024,
        "Width": 3840,
        "Height": 1600,
        "Bitrate": 60000000,
        "MediaStreams": [vid(3840, 1600, rng="HDR10", br=60000000), aud(1, "eac3")],
    },
    "i3": {
        "Id": "i3",
        "Type": "Episode",
        "Name": "Pilot",
        "SeriesName": "Space Show",
        "SeriesId": "sr3",
        "RunTimeTicks": 36000000000,
        "ParentIndexNumber": 1,
        "IndexNumber": 2,
        "Width": 3840,
        "Height": 2160,
        "Bitrate": 30000000,
        "MediaStreams": [vid(3840, 2160, codec="av1", br=30000000), aud(1, "aac", ch=2)],
    },
    "i4": {
        "Id": "i4",
        "Type": "Episode",
        "Name": "Cold Open",
        "SeriesName": "Quiet Show",
        "ParentIndexNumber": 3,
        "IndexNumber": 10,
        "Width": 1920,
        "Height": 1080,
        "Bitrate": 5400000,
        "MediaStreams": [vid(1920, 1080, br=5400000), aud(1, "aac", ch=2), sub(2, "subrip", True)],
    },
    "i5": {
        "Id": "i5",
        "Type": "Movie",
        "Name": "Loud Film",
        "ProductionYear": 2019,
        "Width": 1920,
        "Height": 1080,
        "Bitrate": 12000000,
        "MediaStreams": [vid(1920, 1080, codec="h264", br=12000000), aud(1, "dts", "DTS-HD MA")],
    },
    "i6": {
        "Id": "i6",
        "Type": "Movie",
        "Name": "Third Film",
        "ProductionYear": 2025,
        "Width": 3840,
        "Height": 2160,
        "Bitrate": 70000000,
        "MediaStreams": [vid(3840, 2160, br=70000000), aud(1, "eac3")],
    },
}


def sess(sid, dev, user, ep, item, method, sub_i=-1, aud_i=1, src=None, paused=False, ti=None, client="Emby Web"):
    s = {
        "Id": sid,
        "DeviceId": "dev-" + sid,
        "DeviceName": dev,
        "Client": client,
        "RemoteEndPoint": ep,
        "PlayState": {
            "PlayMethod": method,
            "SubtitleStreamIndex": sub_i,
            "AudioStreamIndex": aud_i,
            "MediaSourceId": src or ("mediasource_" + (item or "")),
            "IsPaused": paused,
        },
    }
    if user:
        s["UserName"] = user
    if item:
        s["NowPlayingItem"] = {k: v for k, v in ITEMS[item].items() if not k.startswith("_")}
    if ti:
        s["TranscodingInfo"] = ti
    return s


LAN_TV = lambda reasons, steps: sess(  # noqa: E731
    "s1",
    "Living Room TV",
    "Robin",
    "192.0.2.50",
    "i1",
    "Transcode",
    sub_i=3,
    src="ms-big-4k",
    client="Emby for TV",
    ti={
        **HW,
        "IsVideoDirect": False,
        "IsAudioDirect": False,
        "VideoCodec": "h264",
        "AudioCodec": "ac3",
        "Width": 3840,
        "Height": 2160,
        "Bitrate": 40000000,
        "TranscodeReasons": reasons,
        "VideoPipelineInfo": steps,
    },
)
BURN = [
    STEP("Decoder", "HW Decode H.265 (HEVC)"),
    STEP("ToneMapping", "Tone Map"),
    STEP("SubtitleOverlay", "Burn In Subtitles"),
    STEP("Encoder", "HW Encode H.264 (AVC)"),
]
REMOTE_SW = sess(
    "s2",
    "Firefox Windows",
    "Kim Example",
    "203.0.113.7:51000",
    "i2",
    "Transcode",
    ti={
        "VideoDecoderIsHardware": True,
        "VideoEncoderIsHardware": False,
        "IsVideoDirect": False,
        "IsAudioDirect": True,
        "VideoCodec": "h264",
        "Width": 1920,
        "Height": 800,
        "Bitrate": 8000000,
        "TranscodeReasons": ["ContainerBitrateExceedsLimit"],
    },
)
REMOTE_AV1 = sess(
    "s3",
    "Old Stick",
    "Lee Sample",
    "[2001:db8::1]:8096",
    "i3",
    "Transcode",
    paused=True,
    client="AndroidTv",
    ti={
        **HW,
        "IsVideoDirect": False,
        "IsAudioDirect": True,
        "VideoCodec": "h264",
        "Width": 1920,
        "Height": 1080,
        "Bitrate": 10000000,
        "TranscodeReasons": ["VideoCodecNotSupported"],
    },
)
DIRECT = sess("s4", "Phone", "Max Tester", "198.51.100.20", "i4", "DirectStream", sub_i=2, client="Emby for iOS")
AUDIO_ONLY = sess(
    "s5",
    "Laptop",
    "Kim Example",
    "203.0.113.9",
    "i5",
    "Transcode",
    ti={
        "IsVideoDirect": True,
        "IsAudioDirect": False,
        "AudioCodec": "ac3",
        "TranscodeReasons": ["AudioCodecNotSupported"],
    },
)
REMOTE_3 = sess(
    "s6",
    "Tablet",
    "Sam Third",
    "203.0.113.30",
    "i6",
    "Transcode",
    ti={
        **HW,
        "IsVideoDirect": False,
        "IsAudioDirect": True,
        "VideoCodec": "h264",
        "Width": 1920,
        "Height": 1080,
        "Bitrate": 12000000,
        "TranscodeReasons": ["ContainerBitrateExceedsLimit"],
    },
)
DIRECT_A = sess("s9", "TV Box", "Ana Placeholder", "203.0.113.40", "i5", "DirectPlay", client="AndroidTv")
DIRECT_B = sess("s10", "Tablet", "Bo Placeholder", "203.0.113.41", "i3", "DirectStream", paused=True)
OWNER_DIRECT = sess("s11", "Living Room TV", "Robin", "192.0.2.50", "i4", "DirectPlay", client="Emby for TV")
REMOTE_GPU = sess(
    "s12",
    "Firefox Windows",
    "Kim Example",
    "203.0.113.7:51000",
    "i2",
    "Transcode",
    ti={
        **HW,
        "IsVideoDirect": False,
        "IsAudioDirect": True,
        "VideoCodec": "h264",
        "Width": 1920,
        "Height": 800,
        "Bitrate": 8000000,
        "TranscodeReasons": ["ContainerBitrateExceedsLimit"],
    },
)
MANY_DIRECT = [
    sess(f"d{n}", "Phone", f"Viewer{n} Placeholder", f"203.0.113.{60 + n}", "i4", "DirectStream") for n in range(1, 9)
]
SERVICE = sess("s7", "Stats Server", None, "172.17.0.5", None, None, client="Stats")
IDLE = sess("s8", "Old Browser", "Max Tester", "198.51.100.20", None, None)
for s in (SERVICE, IDLE):
    s["PlayState"] = {}


def tweak(s, dev=None, paused=None, pos_s=None, ep=None, **ti):
    """A copy of session s with another device name, playback position (s) and TranscodingInfo fields."""
    s = json.loads(json.dumps(s))
    if dev:
        s["DeviceName"] = dev
    if ep:
        s["RemoteEndPoint"] = ep
    if paused is not None:
        s["PlayState"]["IsPaused"] = paused
    if pos_s is not None:
        s["PlayState"]["PositionTicks"] = int(pos_s * 10_000_000)
    for k, v in ti.items():
        s["TranscodingInfo"][k] = int(v * 10_000_000) if k.endswith("Ticks") else v
    return s


BITRATE_TV = LAN_TV(["ContainerBitrateExceedsLimit"], BURN[:2] + BURN[3:])
LAN_4K4K = tweak(BITRATE_TV, VideoCodec="hevc")
DP_ERR = tweak(REMOTE_GPU, TranscodeReasons=["DirectPlayError"])

SESSIONS = {
    "busy": [
        SERVICE,
        DIRECT,
        AUDIO_ONLY,
        REMOTE_SW,
        IDLE,
        REMOTE_AV1,
        LAN_TV(["SubtitleCodecNotSupported", "ContainerBitrateExceedsLimit"], BURN),
    ],
    "lan4k": [LAN_TV(["SubtitleCodecNotSupported", "ContainerBitrateExceedsLimit"], BURN), DIRECT],
    "lanreason2": [LAN_TV(["ContainerBitrateExceedsLimit"], BURN[:2] + BURN[3:]), DIRECT],
    "load": [REMOTE_SW, REMOTE_AV1, REMOTE_3, DIRECT],
    "calm": [DIRECT, IDLE, SERVICE],
    "tonight": [
        DIRECT,
        LAN_TV(["ContainerBitrateExceedsLimit"], BURN[:2] + BURN[3:]),
        REMOTE_GPU,
        DIRECT_A,
        REMOTE_3,
        DIRECT_B,
    ],
    "ownerdirect": [OWNER_DIRECT, DIRECT],
    "manydirect": [REMOTE_3] + MANY_DIRECT,
    "empty": [],
}
SESSIONS["noitems"] = SESSIONS["busy"]
# The owner's own stream ("Robin" = the test profile's owner) on the 100 Mbit TV: shown like anyone else's
OWN_4K = sess("o1", "Living Room TV", "Robin", "192.0.2.50", "i1", "DirectPlay", src="ms-big-4k", client="Emby for TV")
OWN_CPU = sess(
    "o3",
    "Living Room TV",
    "Robin",
    "192.0.2.50",
    "i2",
    "Transcode",
    client="Emby for TV",
    ti={
        "VideoDecoderIsHardware": True,
        "VideoEncoderIsHardware": False,
        "VideoDecoderHwAccel": "NVDEC",
        "IsVideoDirect": False,
        "IsAudioDirect": True,
        "VideoCodec": "h264",
        "Width": 1920,
        "Height": 800,
        "Bitrate": 8000000,
        "TranscodeReasons": ["VideoRangeNotSupported"],
    },
)
SESSIONS.update(
    {
        "own4k": [OWN_4K, DIRECT],
        "owncpu": [OWN_CPU, REMOTE_SW],
    }
)
SESSIONS.update(
    {
        "lanpath": [tweak(BITRATE_TV, dev="Bedroom Streamer")],  # LAN, port speed unknown
        "lanslow": [tweak(REMOTE_GPU, dev="Bedroom Streamer", ep="192.0.2.60")],  # LAN, 60 Mbps source
        "dperr": [DP_ERR],
        "stall": [tweak(LAN_4K4K, pos_s=600, TranscodingPositionTicks=590, TranscodingStartPositionTicks=0)],
        "stallstart": [tweak(LAN_4K4K, pos_s=320, TranscodingPositionTicks=300, TranscodingStartPositionTicks=300)],
        "freshseek": [tweak(LAN_4K4K, pos_s=300, TranscodingPositionTicks=300, TranscodingStartPositionTicks=300)],
        "ahead": [
            tweak(
                LAN_4K4K,
                pos_s=600,
                TranscodingPositionTicks=900,
                TranscodingStartPositionTicks=0,
                CompletionPercentage=40.5,
            )
        ],
        "pctstuck": [tweak(LAN_4K4K, pos_s=600, CompletionPercentage=40.5)],
        "pctmoving": [tweak(LAN_4K4K, pos_s=720, CompletionPercentage=44.0)],
        "pctpaused": [tweak(LAN_4K4K, paused=True, pos_s=600, CompletionPercentage=40.5)],
    }
)


def lib(i, kind, name, w, h, subs, tmdb=None, series=None, s=None, e=None, versions=None):
    streams = [vid(w, h)] + [sub(j + 2, c, c.lower() in ("subrip", "srt", "ass")) for j, c in enumerate(subs)]
    it = {
        "Id": i,
        "Type": kind,
        "Name": name,
        "Width": w,
        "Height": h,
        "_4k": w >= 3200,
        "MediaSources": versions or [{"Id": "ms-" + i, "MediaStreams": streams}],
    }
    if tmdb:
        it["ProviderIds"] = {"Tmdb": str(tmdb), "Imdb": f"tt{tmdb:07d}"}
    if series:
        it.update(SeriesName=series, ParentIndexNumber=s, IndexNumber=e)
    return it


LIBRARY = [
    lib("m1", "Movie", "Alpha", 3840, 2160, ["PGSSUB"], tmdb=1),
    lib("m2", "Movie", "Beta", 3840, 1600, ["PGSSUB", "subrip"], tmdb=2),
    lib("m2b", "Movie", "Beta", 1920, 800, ["subrip"], tmdb=2),
    lib(
        "m3",
        "Movie",
        "Gamma",
        3840,
        2160,
        [],
        tmdb=3,
        versions=[
            {"Id": "ms-m3-4k", "MediaStreams": [vid(3840, 2160)]},
            {"Id": "ms-m3-hd", "MediaStreams": [vid(1920, 1080)]},
        ],
    ),
    lib("m4", "Movie", "Delta", 1920, 1080, ["subrip"], tmdb=4),
    lib("m5", "Movie", "Epsilon", 3840, 2160, ["subrip"], tmdb=5),
    lib("e1", "Episode", "One", 3840, 2160, ["PGSSUB"], series="Space Show", s=1, e=1),
    lib("e2", "Episode", "Two", 3840, 2160, ["subrip"], series="Space Show", s=1, e=2),
    lib("e2b", "Episode", "Two", 1920, 1080, ["subrip"], series="Space Show", s=1, e=2),
    lib("e3", "Episode", "Three", 1920, 1080, [], series="Other Show", s=1, e=1),
]
USERS = [
    {"Name": "Robin", "Policy": {"RemoteClientBitrateLimit": 0}},
    {"Name": "Kim Example", "Policy": {"RemoteClientBitrateLimit": 20000000}},
    {"Name": "Lee Sample", "Policy": {}},
]


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, obj=None, raw=None):
        body = raw if raw is not None else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_any(self):
        tok = self.headers.get("X-Emby-Token")
        with open(LOG, "a") as f:
            f.write(f"{self.command} {self.path} token={'ok' if tok == KEY else 'bad' if tok else 'none'}\n")
        scen = open(SCEN).read().strip()
        if self.command != "GET":
            return self.reply(405, {"error": "read-only fake"})
        if tok != KEY:
            return self.reply(401, raw=b"Access token is invalid or expired.")
        u = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        if scen == "500":
            return self.reply(500, raw=b"boom")
        if scen == "badjson":
            return self.reply(200, raw=b"<html>login</html>")
        if scen == "slow":
            time.sleep(3)
        if u.path == "/Sessions":
            return self.reply(200, SESSIONS.get(scen, []))
        if u.path == "/Items" and "Ids" in q:
            if scen == "noitems":
                return self.reply(500, raw=b"items broken")
            items = [
                {
                    "Id": i,
                    "MediaSources": ITEMS[i].get("_sources")
                    or [
                        {
                            "Id": "mediasource_" + i,
                            "Bitrate": ITEMS[i]["Bitrate"],
                            "MediaStreams": ITEMS[i]["MediaStreams"],
                        }
                    ],
                }
                for i in q["Ids"].split(",")
                if i in ITEMS
            ]
            return self.reply(200, {"Items": items, "TotalRecordCount": len(items)})
        if u.path == "/Items":
            rows = [x for x in LIBRARY if x["Type"] == q.get("IncludeItemTypes")]
            if q.get("Is4K") == "true":
                rows = [x for x in rows if x["_4k"]]
            fields = q.get("Fields", "")
            out = []
            for x in rows:
                y = {k: v for k, v in x.items() if not k.startswith("_")}
                if "MediaSources" not in fields:
                    y.pop("MediaSources")
                out.append(y)
            st, lim = int(q.get("StartIndex", 0)), int(q.get("Limit", 1000))
            return self.reply(200, {"Items": out[st : st + lim], "TotalRecordCount": len(out)})
        if u.path == "/Users":
            return self.reply(200, USERS)
        if u.path == "/System/Configuration":
            return self.reply(200, {"RemoteClientBitrateLimit": 0})
        return self.reply(404, raw=b"not found")

    do_GET = do_POST = do_DELETE = do_PUT = handle_any


srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
with open(PORTFILE + ".tmp", "w") as f:
    f.write(str(srv.server_address[1]))
import os  # noqa: E402

os.replace(PORTFILE + ".tmp", PORTFILE)
srv.serve_forever()
