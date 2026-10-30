import json
import os
import subprocess
import sys
import tempfile

import cv2
import numpy as np

BIN = os.environ.get("UWFIX_BIN") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "scripts", "uwfix"
)
T = tempfile.mkdtemp(prefix="uwfix-tests-")
passed = failed = 0


def ok(name, cond, got=None):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok   - {name}")
    else:
        failed += 1
        print(f"FAIL - {name}" + (f"\n       got: {str(got)[:600]}" if got is not None else ""))


def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120)


def uw(*args, rc=0):
    r = sh([BIN, *args])
    if r.returncode != rc:
        ok(f"exit {rc}: uwfix {' '.join(args)[:70]}", False, r.stdout + r.stderr)
    return r


def mean_rgb(path):
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-vf", "scale=64:36", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True,
        timeout=60,
    )
    return np.frombuffer(r.stdout, np.uint8).reshape(-1, 3).astype(float).mean(0) / 255


def dur(path):
    return float(sh(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path]).stdout)


src = f"{T}/scene.mp4"
r = sh(
    [
        "ffmpeg",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=320x180:rate=10:duration=3",
        "-f",
        "lavfi",
        "-i",
        "smptebars=size=320x180:rate=10:duration=3",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=6",
        "-filter_complex",
        "[0:v]colorchannelmixer=rr=0.25:gg=0.85:bb=1[a];[1:v]colorchannelmixer=rr=0.3:bb=0.6[b];"
        "[a][b]concat=n=2:v=1:a=0,format=yuv420p[v]",
        "-map",
        "[v]",
        "-map",
        "2:a",
        "-c:v",
        "libx264",
        "-c:a",
        "aac",
        src,
    ]
)
ok("fixture clip built", r.returncode == 0 and os.path.exists(src), r.stderr)
photo = f"{T}/photo.png"
sh(["ffmpeg", "-v", "error", "-i", src, "-frames:v", "1", photo])

j = json.loads(uw(src, "--json", "--analyze-only").stdout)
sc = j["scenes"]
ok(
    "two scenes found, cut near 3 s",
    len(sc) == 2 and abs(sc[0]["end"] - 3.0) <= 0.6,
    [(s["start"], s["end"]) for s in sc],
)
f = sc[0]["fix"]
ok("red restored from green in the red-starved scene", f["red_from_green"] > 0.3, f)
for k, s in enumerate(sc):
    mi, mo = s["fix"]["mean_in"], s["fix"]["mean_out"]
    ok(f"scene {k + 1}: channel means closer together", max(mo) - min(mo) < max(mi) - min(mi), (mi, mo))
ok("analyze-only renders nothing", j["output"] is None and not os.path.exists(f"{T}/scene.uw.mp4"), j["output"])
j = json.loads(uw(src, "--json", "--analyze-only", "--mode", "clip").stdout)
ok("--mode clip = one correction", len(j["scenes"]) == 1, len(j["scenes"]))
j = json.loads(uw(src, "--json", "--analyze-only", "--strength", "0").stdout)
f = j["scenes"][0]["fix"]
ok(
    "strength 0 = identity",
    f["matrix"] == [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
    and f["levels"]["min"] == [0, 0, 0]
    and f["levels"]["max"] == [1, 1, 1],
    f,
)

r = uw(src, "--still", f"{T}/ba.jpg")
out = f"{T}/scene.uw.mp4"
ok("default output name + printed paths", r.stdout.split() == [out, f"{T}/ba.jpg"], r.stdout)
ok("same duration", abs(dur(out) - dur(src)) < 0.25, (dur(out), dur(src)))
ok(
    "audio kept",
    "audio" in sh(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", out]).stdout,
)
a, b = mean_rgb(src), mean_rgb(out)
ok(
    "rendered output is less colour-cast (ffmpeg chain does what numpy planned)",
    np.ptp(b) < np.ptp(a) and b[0] / b[1] > a[0] / a[1],
    (a.round(3), b.round(3)),
)
img = cv2.imread(f"{T}/ba.jpg")
ok(
    "before/after still: 2 panels at 540 px",
    img is not None and img.shape[0] == 540 and img.shape[1] == 2 * 960 + 8,
    None if img is None else img.shape,
)
uw(src, "--still-only", "--still", f"{T}/cmp.jpg", "--compare", "0.5,1", "-o", f"{T}/never.mp4")
img = cv2.imread(f"{T}/cmp.jpg")
ok(
    "--compare adds panels, --still-only renders no clip",
    img is not None and img.shape[1] == 4 * 960 + 3 * 8 and not os.path.exists(f"{T}/never.mp4"),
    None if img is None else img.shape,
)
uw(src, "--mode", "clip", "-o", f"{T}/clip.mp4", "--quiet")
ok("--mode clip render", os.path.exists(f"{T}/clip.mp4") and abs(dur(f"{T}/clip.mp4") - dur(src)) < 0.25)

r = uw(photo, "--still", f"{T}/pba.jpg")
pout = f"{T}/photo.uw.jpg"
pa, pb = mean_rgb(photo), mean_rgb(pout) if os.path.exists(pout) else np.zeros(3)
ok(
    "photo → .uw.jpg, redder and less cast",
    os.path.exists(pout) and pb[0] > pa[0] and np.ptp(pb) < np.ptp(pa),
    (pa, pb),
)
ok("photo still", os.path.exists(f"{T}/pba.jpg"))

ok("refuses to overwrite the input", "overwrite the input" in uw(src, "-o", src, rc=1).stderr)
ok("bad strength", "--strength" in uw(src, "--strength", "2", rc=1).stderr)
ok("missing input", "no such file" in uw(f"{T}/nope.mp4", rc=1).stderr)
ok("still-only needs --still", "needs --still" in uw(src, "--still-only", rc=1).stderr)
ok("missing output folder", "does not exist" in uw(src, "-o", f"{T}/no/x.mp4", rc=1).stderr)
ok("bad --compare", "--compare" in uw(src, "--compare", "x", "--analyze-only", rc=1).stderr)

subprocess.run(["rm", "-rf", "--", T])
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
