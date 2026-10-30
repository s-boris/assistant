"""Offline tests for ~/bin/trip-recap. Synthetic photos (Pillow + numpy) with EXIF time/GPS, one tagged video,
a HEIC stub and a broken file, all in a temp dir; fixed config + dives file, so live state is never read.
`test.py --fixtures DIR` only writes the fixture set (for rehearsals)."""

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

BIN = os.environ.get("TRIP_RECAP_BIN") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "scripts", "trip-recap"
)
passed = failed = 0


def ok(name, cond, got=None):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok   - {name}")
    else:
        failed += 1
        print(f"FAIL - {name}" + (f"\n       got: {str(got)[:800]}" if got is not None else ""))


# ---------- fixtures ----------
def scene(kind, seed, w=960, h=720):
    import numpy as np
    from PIL import Image, ImageDraw

    rng = np.random.default_rng(seed)
    y = np.linspace(0, 1, h)[:, None, None]
    if kind == "uw":
        top, bot = np.array([25, 120, 150]), np.array([8, 70, 95])
        a = top * (1 - y) + bot * y + np.zeros((h, w, 3))
    else:
        a = np.zeros((h, w, 3))
        sky = np.array([120, 180, 230]) * (1 - y) + np.array([200, 225, 245]) * y
        a[:] = sky
        a[int(h * 0.42) : int(h * 0.55)] = [40, 150, 170]
        a[int(h * 0.55) :] = [228, 212, 175]
    im = Image.fromarray(np.clip(a + rng.normal(0, 9, (h, w, 3)), 0, 255).astype("uint8"))
    d = ImageDraw.Draw(im)
    for _ in range(14):
        x0, y0 = int(rng.integers(0, w - 80)), int(rng.integers(int(h * 0.45), h - 40))
        r = int(rng.integers(20, 90))
        col = (60, 115, 105) if kind == "uw" else (150, 118, 98)
        if kind == "uw" and rng.random() < 0.4:
            col = (95, 165, 175)
        d.ellipse([x0, y0, x0 + r * 2, y0 + r], fill=col, outline=(20, 30, 30), width=3)
    for _ in range(40):
        x0, y0 = int(rng.integers(0, w)), int(rng.integers(0, h))
        d.line([x0, y0, x0 + int(rng.integers(-60, 60)), y0 + int(rng.integers(-60, 60))], fill=(30, 40, 35), width=2)
    return im


def exif_bytes(t, off=None, gps=None):
    from PIL import Image, TiffImagePlugin

    R = TiffImagePlugin.IFDRational
    ex = Image.Exif()
    ex[0x0110] = "FixtureCam"
    ifd = ex.get_ifd(0x8769)
    ifd[0x9003] = t.strftime("%Y:%m:%d %H:%M:%S")
    if off:
        ifd[0x9011] = off
    if gps:

        def dms(x):
            x = abs(x)
            d = int(x)
            m = int((x - d) * 60)
            s = ((x - d) * 60 - m) * 60
            return (R(d, 1), R(m, 1), R(int(round(s * 1000)), 1000))

        g = ex.get_ifd(0x8825)
        g.update({1: "S" if gps[0] < 0 else "N", 2: dms(gps[0]), 3: "W" if gps[1] < 0 else "E", 4: dms(gps[1])})
    return ex.tobytes()


ISL_W, ISL_W2, ISL_N, ISL_E, FAR = (
    (-20.62, 100.45),
    (-20.70, 100.50),
    (-20.32, 100.71),
    (-20.36, 100.835),
    (51.47, -0.45),
)
T = dt.datetime


def make_fixtures(d):
    from PIL import ImageEnhance, ImageFilter

    os.makedirs(d, exist_ok=True)

    def save(name, im, t=None, off=None, gps=None):
        p = os.path.join(d, name)
        im.save(p, "JPEG", quality=90, **({"exif": exif_bytes(t, off, gps)} if t else {}))
        return p

    save("far_away.jpg", scene("land", 1), T(2030, 3, 9, 7, 0), "+01:00", FAR)
    save("m1_beach.jpg", scene("land", 2), T(2030, 3, 10, 9, 0), None, ISL_W)
    save("m1_beach_blur.jpg", scene("land", 2).filter(ImageFilter.GaussianBlur(7)), T(2030, 3, 10, 9, 20), None, ISL_W)
    save("m2_burst_a.jpg", scene("land", 3), T(2030, 3, 10, 11, 0, 0), None, ISL_W2)
    save("m2_burst_b.jpg", scene("land", 3), T(2030, 3, 10, 11, 0, 4), None, ISL_W2)
    save("m3_dark.jpg", ImageEnhance.Brightness(scene("land", 4)).enhance(0.18), T(2030, 3, 10, 18, 30), None, ISL_W)
    save("m4_hike.jpg", scene("land", 5), T(2030, 3, 10, 14, 0), None, ISL_W2)
    for i in range(3):
        save(
            f"u{i + 1}_dive.jpg", scene("uw", 10 + i), T(2030, 3, 11, 9, 40) + dt.timedelta(minutes=10 * i)
        )  # no GPS: underwater camera
    save("m5_lunch.jpg", scene("land", 6), T(2030, 3, 11, 12, 0), None, ISL_W)
    for i in range(3):
        save(f"n{i + 1}_north.jpg", scene("land", 20 + i), T(2030, 3, 12, 10 + i, 0), None, ISL_N)
    save("e1_east.jpg", scene("land", 30), T(2030, 3, 13, 9, 0), "+04:00", ISL_E)
    save("e2_east_offset.jpg", scene("land", 31), T(2030, 3, 13, 6, 0), "+01:00", ISL_E)
    save("e3_bright.jpg", ImageEnhance.Brightness(scene("land", 32)).enhance(2.6), T(2030, 3, 13, 13, 0), None, ISL_E)
    save("u4_snorkel.jpg", scene("uw", 40), T(2030, 3, 14, 11, 0))  # no GPS, no dive, alone that day -> leg
    p = save("noexif.jpg", scene("land", 50))
    ts = T(2030, 3, 12, 8, 0, tzinfo=dt.timezone(dt.timedelta(hours=4))).timestamp()
    os.utime(p, (ts, ts))
    with open(os.path.join(d, "IMG_0001.HEIC"), "wb") as f:
        f.write(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 64)
    with open(os.path.join(d, "broken.jpg"), "wb") as f:
        f.write(b"not a jpeg at all")
    os.makedirs(os.path.join(d, "videos"), exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x240:rate=10",
            "-t",
            "2",
            "-metadata",
            "creation_time=2030-03-13T06:00:00Z",
            "-metadata",
            "location=-20.3600+100.8350/",
            "-pix_fmt",
            "yuv420p",
            os.path.join(d, "videos", "v1_east.mp4"),
        ],
        check=True,
        stdin=subprocess.DEVNULL,
    )


CFG = {
    "title": "Fixture trip",
    "tz": "Asia/Dubai",
    "coast": "coast.json",
    "map_bbox": [-20.83, 100.18, -20.24, 100.98],
    "max_km": 40,
    "regions": [
        {"name": "West Isle", "anchors": [[-20.60, 100.45], [-20.76, 100.51]]},
        {"name": "North Isle", "anchors": [[-20.31, 100.70], [-20.34, 100.76]]},
        {"name": "East Isle", "anchors": [[-20.36, 100.84]]},
    ],
    "legs": [
        {"from": "2030-03-09", "to": "2030-03-11", "area": "West Isle"},
        {"from": "2030-03-12", "to": "2030-03-12", "area": "North Isle"},
        {"from": "2030-03-13", "to": "2030-03-15", "area": "East Isle"},
    ],
}
DIVES = [
    {
        "id": 1,
        "when": "2030-03-11T09:30",
        "time": 45,
        "site": "Fixture Reef",
        "lat": -20.69,
        "lon": 100.39,
        "depth": 24,
        "area": "West Isle",
    }
]
SQUARE = {
    "elements": [  # ~2 km island split into two ways + a 30 m rock
        {
            "type": "way",
            "geometry": [
                {"lat": -20.50, "lon": 100.50},
                {"lat": -20.50, "lon": 100.51},
                {"lat": -20.505, "lon": 100.515},
                {"lat": -20.51, "lon": 100.52},
            ],
        },
        {
            "type": "way",
            "geometry": [
                {"lat": -20.51, "lon": 100.52},
                {"lat": -20.52, "lon": 100.51},
                {"lat": -20.50, "lon": 100.50},
            ],
        },
        {
            "type": "way",
            "geometry": [
                {"lat": -20.6, "lon": 100.6},
                {"lat": -20.6, "lon": 100.6002},
                {"lat": -20.6002, "lon": 100.6002},
                {"lat": -20.6, "lon": 100.6},
            ],
        },
    ]
}


def run(args, env, rc=0, timeout=600):
    r = subprocess.run([BIN, *args], env=env, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    if r.returncode != rc:
        ok(f"exit {rc}: trip-recap {' '.join(args)[:70]}", False, r.stdout + r.stderr)
    return r


def main():
    if sys.argv[1:2] == ["--fixtures"]:
        make_fixtures(sys.argv[2])
        print(f"fixtures in {sys.argv[2]}")
        return
    tmp = tempfile.mkdtemp(prefix="trip-recap-tests-")
    try:
        tests(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


def tests(tmp):
    photos, out = f"{tmp}/photos", f"{tmp}/out"
    make_fixtures(photos)
    with open(f"{tmp}/osm.json", "w") as f:
        json.dump(SQUARE, f)
    with open(f"{tmp}/dives.json", "w") as f:
        json.dump(DIVES, f)
    env = dict(
        os.environ,
        HOME=f"{tmp}/home",
        TRIP_RECAP_CONFIG=f"{tmp}/cfg.json",
        DIVELOG_CONFIG=f"{tmp}/none.json",
        TRIP_RECAP_DIVELOG=f"{tmp}/no-divelog",
    )
    env.pop("TRIP_RECAP_REEXEC", None)
    env["TRIP_RECAP_FACE_MODEL"] = os.environ.get("TRIP_RECAP_FACE_MODEL") or os.path.expanduser(
        "~/opt/trip-recap-models/face_detection_yunet_2023mar.onnx"
    )
    env["TRIP_RECAP_PY"] = env["UWFIX_PY"] = os.path.expanduser("~/opt/dive-venv/bin/python")
    # the real uwfix and vidmontage from ~/bin (a temp HOME links it; the CI runner's is the would-be ~/bin of the
    # tree), named before HOME moves to the empty temp one, where neither PATH nor ~/bin would find them
    for name in ("uwfix", "vidmontage"):
        var = f"TRIP_RECAP_{name.upper()}"
        found = os.environ.get(var) or shutil.which(name) or os.path.expanduser(f"~/bin/{name}")
        if os.access(found, os.X_OK):
            env[var] = found
    os.makedirs(f"{tmp}/home")

    # coast: two ways stitched into one ring, the rock dropped, a southern box parsed as a box
    r = run(["coast", "-20.9,100.1,-20.1,56.0", "-o", f"{tmp}/coast.json", "--from", f"{tmp}/osm.json"], env)
    coast = json.load(open(f"{tmp}/coast.json"))
    ok("coast: one stitched ring, rock dropped", len(coast["rings"]) == 1 and not coast["lines"], coast)
    ok(
        "coast: ring closed + simplified",
        coast["rings"][0][0] == coast["rings"][0][-1] and len(coast["rings"][0]) <= 6,
        coast["rings"],
    )
    with open(f"{tmp}/cfg.json", "w") as f:
        json.dump(CFG, f)

    # stub vidmontage records its args; uwfix is the real one
    stub = f"{tmp}/vidmontage-stub"
    with open(stub, "w") as f:
        f.write(
            '#!/bin/sh\nprintf \'%s\\n\' "$@" > "$(dirname "$0")/vm-args.txt"\n'
            'while [ $# -gt 0 ]; do [ "$1" = -o ] && : > "$2"; shift; done\n'
        )
    os.chmod(stub, 0o755)
    music = f"{tmp}/song.mp3"
    open(music, "wb").close()
    env1 = dict(env, TRIP_RECAP_VIDMONTAGE=stub)
    r = run(
        [
            photos,
            "-o",
            out,
            "--count",
            "8",
            "--dives",
            f"{tmp}/dives.json",
            "--json",
            "--music",
            music,
            "--title",
            "Fixture trip",
        ],
        env1,
    )
    try:
        s = json.loads(r.stdout)
    except ValueError:
        ok("json summary", False, r.stdout + r.stderr)
        return
    rec = json.load(open(f"{out}/recap.json"))
    by = {os.path.basename(it["path"]): it for it in rec["items"]}
    ok(
        "counts: 19 photos, 1 video, 1 HEIC skipped, 1 unreadable",
        (s["photos"], s["videos"], s["skipped"], s["unreadable"]) == (19, 1, 1, 1),
        s,
    )
    ok(
        "islands: West Isle, North Isle, East Isle, Elsewhere",
        set(s["islands"]) == {"West Isle", "North Isle", "East Isle", "Elsewhere"},
        s["islands"],
    )
    ok(
        "GPS read from EXIF (S/E signs)",
        abs(by["n1_north.jpg"]["lat"] + 20.32) < 1e-3 and abs(by["n1_north.jpg"]["lon"] - 100.71) < 1e-3,
        by["n1_north.jpg"],
    )
    ok(
        "offset +01:00 converted to trip time",
        by["e2_east_offset.jpg"]["t"] == "2030-03-13T09:00:00",
        by["e2_east_offset.jpg"]["t"],
    )
    ok("naive EXIF time = trip time", by["m1_beach.jpg"]["t"] == "2030-03-10T09:00:00", by["m1_beach.jpg"]["t"])
    ok(
        "no EXIF -> file time, island by same-day GPS",
        by["noexif.jpg"]["time_src"] == "file"
        and by["noexif.jpg"]["island"] == "North Isle"
        and by["noexif.jpg"]["island_src"] == "time",
        by["noexif.jpg"],
    )
    ok(
        "video: UTC creation time + ISO 6709 location",
        by["v1_east.mp4"]["t"] == "2030-03-13T10:00:00" and by["v1_east.mp4"]["island"] == "East Isle",
        by["v1_east.mp4"],
    )
    ok(
        "dive photos: island by time, underwater, linked to dive",
        all(
            by[f"u{i}_dive.jpg"]["island"] == "West Isle"
            and by[f"u{i}_dive.jpg"]["underwater"]
            and by[f"u{i}_dive.jpg"].get("dive") == 1
            for i in (1, 2, 3)
        ),
        [by[f"u{i}_dive.jpg"] for i in (1, 2, 3)],
    )
    ok("underwater shots get a pick on their dive day", any(by[f"u{i}_dive.jpg"].get("pick") for i in (1, 2, 3)), None)
    ok(
        "lone no-GPS day -> island from the leg",
        by["u4_snorkel.jpg"]["island"] == "East Isle" and by["u4_snorkel.jpg"]["island_src"] == "leg",
        by["u4_snorkel.jpg"],
    )
    ok(
        "colour cast: snorkel shot underwater, beach shots not",
        by["u4_snorkel.jpg"]["underwater"] and not any(v["underwater"] for k, v in by.items() if k[0] in "mne"),
        {k: v.get("uw_cast") for k, v in by.items()},
    )
    ok(
        "blurred copy scores below the sharp one",
        by["m1_beach_blur.jpg"]["score"] < by["m1_beach.jpg"]["score"]
        and by["m1_beach_blur.jpg"]["sharp"] < by["m1_beach.jpg"]["sharp"] / 3,
        (by["m1_beach_blur.jpg"], by["m1_beach.jpg"]),
    )
    ok(
        "dark shot has low exposure",
        by["m3_dark.jpg"]["exposure"] < 0.3 and by["m4_hike.jpg"]["exposure"] > 0.5,
        (by["m3_dark.jpg"]["exposure"], by["m4_hike.jpg"]["exposure"]),
    )
    burst = [by["m2_burst_a.jpg"], by["m2_burst_b.jpg"]]
    ok(
        "burst: one of two dropped as duplicate",
        sum("dup_of" in b for b in burst) == 1 and sum(bool(b.get("pick")) for b in burst) <= 1,
        burst,
    )
    picks = [k for k, v in by.items() if v.get("pick")]
    days = {by[k]["day"] for k in picks}
    ok("8 picks spread over ≥ 5 days", len(picks) == 8 and len(days) >= 5, sorted(picks))
    ok(
        "picks file in time order",
        [os.path.basename(ln.split("\t")[0]) for ln in open(f"{out}/picks.txt").read().splitlines()]
        == sorted(picks, key=lambda k: by[k]["t"]),
        open(f"{out}/picks.txt").read(),
    )
    page = open(f"{out}/index.html").read()
    bare = re.sub(r"(data:image/jpeg;base64,)[A-Za-z0-9+/=]+", r"\1", page)
    ok(
        "page: CSP blocks the network, no external refs",
        "Content-Security-Policy" in page
        and "default-src 'none'" in page
        and not re.search(r"https?:|//[a-z]|url\(|src=\"(?!data:)", bare),
        re.findall(r".{0,30}(?:https?:|//[a-z]|url\(|src=\"(?!data:)).{0,30}", bare)[:3],
    )
    ok(
        "page: map with coast, dots, dive marker, day chips",
        '<path class="land"' in page
        and page.count('<circle class="dot"') == 14
        and "Fixture Reef" in page
        and page.count('class="day"') == 6,
        page.count('<circle class="dot"'),
    )
    ok(
        "page: thumbnails inline + previews for picks",
        page.count("data:image/jpeg;base64,") >= 20 + 8,
        page.count("data:image/jpeg;base64,"),
    )
    args = open(f"{tmp}/vm-args.txt").read().splitlines()
    files = [a for a in args if a.endswith(".jpg")]
    uw_picks = sum(1 for k in picks if by[k]["uwfix"])
    ok(
        "montage: picks in order, music + title passed",
        len(files) == 8 and "--music" in args and music in args and "Fixture trip" in args,
        args,
    )
    ok(
        "montage: underwater picks went through uwfix",
        uw_picks >= 1 and sum(f.endswith("-uw.jpg") for f in files) == uw_picks == s["montage_uw_corrected"],
        (files, uw_picks),
    )
    from PIL import Image

    ok("montage inputs carry no GPS", all(not Image.open(f).getexif().get_ifd(0x8825) for f in files), None)
    ok("summary has no coordinates", not re.search(r"-20\.\d|100\.\d|51\.4", r.stdout), r.stdout)

    # human output, real vidmontage on a tiny montage, --since filter, default-free dives
    out2 = f"{tmp}/out2"
    r = run(
        [
            photos,
            "-o",
            out2,
            "--count",
            "2",
            "--size",
            "320x180",
            "--photo-secs",
            "1",
            "--dives",
            "none",
            "--since",
            "2030-03-12",
        ],
        env,
        timeout=300,
    )
    ok(
        "human summary lines",
        r.stdout.startswith("trip-recap: ") and "picks: 2" in r.stdout and "montage: " in r.stdout,
        r.stdout + r.stderr,
    )
    ok("human summary has no coordinates", not re.search(r"-20\.\d|100\.\d", r.stdout), r.stdout)
    pr = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", f"{out2}/montage.mp4"],
        capture_output=True,
        text=True,
    )
    ok(
        "real vidmontage made a playable mp4",
        pr.returncode == 0 and float(pr.stdout.strip() or 0) > 1,
        pr.stdout + pr.stderr,
    )
    rec2 = json.load(open(f"{out2}/recap.json"))
    ok("--since drops earlier days", min(it["day"] for it in rec2["items"]) >= "2030-03-12", None)

    # errors
    r = run([f"{tmp}/home", "-o", f"{tmp}/o3"], env, rc=1)
    ok("empty dir: clear error", "no photos or videos" in r.stderr, r.stderr)
    r = run([photos, "--music", f"{tmp}/missing.mp3"], env, rc=1)
    ok("missing music: clear error", "music file not found" in r.stderr, r.stderr)


if __name__ == "__main__":
    main()
