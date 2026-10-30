import glob
import json
import os
import re
import subprocess
import sys
import tempfile
from xml.etree import ElementTree as ET

BIN = os.environ.get("DIVELOG_BIN") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "scripts", "divelog"
)
T = tempfile.mkdtemp(prefix="divelog-tests-")
ENV = dict(
    os.environ,
    DIVELOG_DIR=f"{T}/state",
    DIVELOG_CONFIG=f"{T}/cfg.json",
    DIVELOG_TZ="Asia/Dubai",
    DIVELOG_NOW="2026-11-24T17:00",
    HOME=f"{T}/home",
)
ENV.pop("DIVELOG_REEXEC", None)
ENV["DIVELOG_PY"] = os.path.expanduser("~/opt/dive-venv/bin/python")
os.makedirs(f"{T}/home")
if not os.path.exists(ENV["DIVELOG_PY"]) and os.environ.get("ASSISTANT_CI_TOOLCHAIN"):
    # the CI runner has no ~/opt: the PDF export re-execs (with -I) into a venv whose site reaches the CI
    # toolchain's, which carries reportlab
    subprocess.run([sys.executable, "-I", "-m", "venv", "--without-pip", f"{T}/dive-venv"], check=True)
    with open(glob.glob(f"{T}/dive-venv/lib/python3*/site-packages")[0] + "/ci-toolchain.pth", "w") as f:
        f.write(os.path.join(os.environ["ASSISTANT_CI_TOOLCHAIN"], "site") + "\n")
    ENV["DIVELOG_PY"] = f"{T}/dive-venv/bin/python"
passed = failed = 0


def ok(name, cond, got=None):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok   - {name}")
    else:
        failed += 1
        print(f"FAIL - {name}" + (f"\n       got: {str(got)[:600]}" if got is not None else ""))


def dl(*args, rc=0):
    r = subprocess.run([BIN, *args], env=ENV, capture_output=True, text=True, timeout=60)
    if r.returncode != rc:
        ok(f"exit {rc}: divelog {' '.join(args)[:60]}", False, r.stdout + r.stderr)
    return r


def tg(*args):
    return json.loads(dl("tg", *args).stdout)


json.dump(
    [
        {"name": "Coral Garden", "area": "North Coast", "lat": -4.5, "lon": 55.4, "depth_max": 30},
        {"name": "L'Ilot", "area": "North Coast", "lat": -4.56, "lon": 55.43, "aliases": ["Ilot"]},
        {"name": "Blue Rocks", "area": "East Isle", "lat": -4.28, "lon": 55.77},
        {"name": "Broken", "lat": "x"},
        {"name": "Far", "lat": 95, "lon": 0},
    ],
    open(f"{T}/sites.json", "w"),
)
r = dl("sites", "import", f"{T}/sites.json")
ok(
    "sites import skips bad coordinates",
    "3 sites (3 new)" in r.stdout
    and "skipped (no lat/lon): Broken" in r.stderr
    and "skipped (bad lat/lon): Far" in r.stderr,
    r.stdout + r.stderr,
)
ok("sites search by alias", "l-ilot" in dl("sites", "--search", "ilot").stdout)

r = dl(
    "add",
    "--json",
    "--quick",
    "Coral Garden 24m 48min 28.5C vis 20m 2 buddies yesterday 9:30 saw whale shark, eagle ray",
)
d = json.loads(r.stdout)
ok(
    "quick: all fields",
    (d["site"], d["depth"], d["time"], d["temp"], d["vis"], d["buddies"], d["when"])
    == ("Coral Garden", 24, 48, 28.5, 20, 2, "2026-11-23T09:30"),
    d,
)
ok("quick: site coordinates copied", d.get("lat") == -4.5 and d.get("site_id") == "coral-garden", d)
ok("quick: sightings capitalised", [s["species"] for s in d["sightings"]] == ["Whale shark", "Eagle ray"], d)
d = json.loads(dl("add", "--json", "--quick", "Ilot 18 m 52' 29°C sicht 15 solo 23.11 14:10 strong current").stdout)
ok(
    "quick: alias, apostrophe minutes, sicht, solo, dd.mm hh:mm, notes",
    (d["site"], d["depth"], d["time"], d["temp"], d["vis"], d["buddies"], d["when"], d.get("notes"))
    == ("L'Ilot", 18, 52, 29, 15, 0, "2026-11-23T14:10", "strong current"),
    d,
)
d = json.loads(dl("add", "--json", "--quick", "Secret Garden 12m 40min").stdout)
ok(
    "quick: unknown leading words = site",
    d["site"] == "Secret Garden" and d["site_id"] is None and d["when"] == "2026-11-24T17:00",
    d,
)
d = json.loads(dl("add", "--json", "--site", "Coral Garden", "--when", "30.12", "--depth", "10").stdout)
ok("date without year in the future = last year", d["when"] == "2025-12-30T09:00", d)
d = json.loads(dl("add", "--json", "--site", "Coral Garden", "--when", "18:30").stdout)
ok("bare time still ahead = yesterday", d["when"] == "2026-11-23T18:30", d)
r = dl("add", "--site", "X", "--when", "sometime soon", rc=1)
ok("unreadable date refused", "cannot read the date/time" in r.stderr, r.stderr)
r = dl("add", "--site", "X", "--depth", "500", rc=1)
ok("depth out of range refused", "outside" in r.stderr, r.stderr)
r = dl("add", "--depth", "12", rc=1)
ok("a dive needs a site", "needs a site" in r.stderr, r.stderr)

r = dl("sight", "auto", "--at", "23.11 16:00", "hawksbill turtle", "--count", "2")
ok(
    "sight auto picks the dive before the photo time",
    r.stdout.startswith("#2 ") and "Hawksbill turtle" in r.stdout,
    r.stdout,
)
dl("edit", "4", "--clear", "depth", "--notes", "test dive")
x = json.loads(dl("show", "4", "--json").stdout)
ok("edit: clear + notes", "depth" not in x and x["notes"] == "test dive", x)
dl("edit", "4", "--clear", "bogus", rc=1)
dl("delete", "5")
ok("delete moves to trash", "#5 " not in dl("list").stdout)
dl("undelete", "5")
ok("undelete restores", "#5 " in dl("list").stdout)
dl("delete", "5")
dl("delete", "4")
lst = dl("list").stdout.splitlines()
ok("list sorted by date", [l.split(" ")[0] for l in lst] == ["#1", "#2", "#3"], lst)  # noqa: E741

s = json.loads(dl("stats", "--json").stdout)
ok(
    "stats",
    s["dives"] == 3
    and s["bottom_min"] == 140
    and s["deepest"]["depth"] == 24
    and s["avg_vis"] == 17.5
    and s["species"] == {"Eagle ray": 1, "Hawksbill turtle": 1, "Whale shark": 1},
    s,
)

# Telegram form
json.dump(
    {"legs": [{"from": "2026-11-27", "to": "2026-12-01", "area": "East Isle"}], "title": "Cfg title"},
    open(f"{T}/cfg.json", "w"),
)
o = tg("cb", "dv:ok")
ok("tg without a draft", "No dive in progress" in o["text"], o)
o = tg("start")
flat = [b for row in o["buttons"] for b in row]
ok(
    "tg start asks the site, used sites first",
    o["step"] == "site"
    and {b["data"] for b in flat[:2]} == {"dv:s:coral-garden", "dv:s:l-ilot"}
    and flat[-1]["data"] == "dv:s:other",
    o,
)
ok("callback data fits 64 bytes", all(len(b["data"].encode()) <= 64 for b in flat))
o = tg("cb", "dv:s:nope")
ok("unknown site button", "unknown site" in o["text"] and o["step"] == "site", o)
o = tg("cb", "dv:s:coral-garden")
ok("site → depth", o["step"] == "depth" and "📍 Coral Garden" in o["text"], o)
o = tg("cb", "dv:d:22")
ok("depth button → time", o["step"] == "time" and "Max depth 22 m" in o["text"], o)
o = tg("text", "45")
ok("typed number → temp", o["step"] == "temp" and "Bottom time 45 min" in o["text"], o)
o = tg("cb", "dv:skip")
ok("skip → vis", o["step"] == "vis", o)
o = tg("text", "vis 25 1 buddy saw manta")
ok(
    "typed fields → confirm",
    o["step"] == "confirm"
    and "Visibility 25 m" in o["text"]
    and "Buddies 1" in o["text"]
    and "Manta" not in o["text"]
    and "manta" in o["text"],
    o,
)
o = tg("text", "nice drift")
ok("plain words on the card = notes", "📝 nice drift" in o["text"] and o["step"] == "confirm", o)
o = tg("cb", "dv:w")
ok("date step", o["step"] == "when" and len(o["buttons"]) == 2, o)
o = tg("text", "22.11 08:15")
ok("typed date → confirm", "Sun 22.11.2026 08:15" in o["text"] and o["step"] == "confirm", o)
o = tg("cb", "dv:w:y14")
ok("yesterday 14:00 button", "Mon 23.11.2026 14:00" in o["text"], o)
o = tg("cb", "dv:ok")
x = json.loads(dl("show", str(o.get("saved_id")), "--json").stdout)
ok(
    "saved from the form",
    (
        x["site"],
        x["depth"],
        x["time"],
        x.get("temp"),
        x["vis"],
        x["buddies"],
        x["when"],
        x["notes"],
        [s["species"] for s in x["sightings"]],
    )
    == ("Coral Garden", 22, 45, None, 25, 1, "2026-11-23T14:00", "nice drift", ["Manta"]),
    x,
)
ok("draft removed after save", not os.path.exists(f"{T}/state/draft.json"))
ENV["DIVELOG_NOW"] = "2026-11-29T10:00"
o = tg("start", "23.11 9:30 Blue Rocks 20m")
ok(
    "offline prefill: date, site, depth from one line",
    "Mon 23.11.2026 09:30" in o["text"] and "Blue Rocks" in o["text"] and o["step"] == "time",
    o,
)
o = tg("cb", "dv:no")
ok("discard", o["text"] == "Discarded." and not os.path.exists(f"{T}/state/draft.json"), o)
o = tg("start")
ok("trip leg picks the area's sites", [b["data"] for row in o["buttons"] for b in row][0] == "dv:s:blue-rocks", o)
json.dump(
    {
        "legs": [{"from": "2026-11-01", "to": "2026-12-31", "area": "Nowhere", "sites": ["l-ilot", "blue-rocks"]}],
        "title": "Cfg title",
    },
    open(f"{T}/cfg.json", "w"),
)
o = tg("show")
ok(
    "leg sites listed first (in order), even outside the area",
    [b["data"] for row in o["buttons"] for b in row][:3] == ["dv:s:l-ilot", "dv:s:blue-rocks", "dv:s:other"],
    o,
)
r = dl("sites")
ok("sites list depth: ≤max without a min", "≤30 m" in r.stdout, r.stdout)
o = tg("cb", "dv:s:other")
o = tg("text", "Mystery Pinnacle")
ok("typed unknown site", "📍 Mystery Pinnacle" in o["text"] and o["step"] == "depth", o)
o = tg("cb", "dv:d:999")
ok("button value out of range", "⚠️" in o["text"] and o["step"] == "depth", o)
tg("cb", "dv:no")

# exports
r = dl("export", "--subsurface", f"{T}/log.xml", "--pdf", f"{T}/log.pdf")
root = ET.parse(f"{T}/log.xml").getroot()
dives = root.findall("dives/dive")
site = {s.get("uuid"): s for s in root.findall("divesites/site")}
first = dives[0]
ok(
    "subsurface: dives + site gps",
    len(dives) == 4
    and root.get("program") == "subsurface"
    and site[first.get("divesiteid")].get("gps") == "-4.500000 55.400000",
    ET.tostring(root)[:400],
)
ok(
    "subsurface: dive fields",
    (
        first.get("date"),
        first.get("time"),
        first.get("duration"),
        first.get("visibility"),
        first.find("divecomputer/depth").get("max"),
        first.find("divetemperature").get("water"),
    )
    == ("2026-11-23", "09:30:00", "48:00 min", "4", "24.0 m", "28.5 C"),
    ET.tostring(first)[:400],
)
ok("subsurface: sightings in notes", "Whale shark" in first.find("notes").text, first.find("notes").text)
try:  # page count and title from the PDF's own objects (reportlab writes them uncompressed; no poppler needed)
    with open(f"{T}/log.pdf", "rb") as f:
        pdf = f.read()
except OSError:
    pdf = b""
pages = len(re.findall(rb"/Type\s*/Page(?![A-Za-z])", pdf))
title = re.search(rb"/Title\s*\(((?:[^()\\]|\\.)*)\)", pdf)
ok("pdf: one page (re-exec into the venv for reportlab)", pdf.startswith(b"%PDF-") and pages == 1, (pages, r.stderr))
ok("pdf: title from config", bool(title) and b"Cfg title" in title.group(1), title)
dl("export", "--pdf", f"{T}/nodir/x.pdf", rc=1)

subprocess.run(["rm", "-rf", "--", T])
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
