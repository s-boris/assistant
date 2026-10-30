#!/usr/bin/env python3
"""Offline tests for ~/bin/arr-mismatch on fixtures/radarr_library.json, a synthetic Radarr library (invented
movies, files and /api/v3/parse results shaped like the real API answers): true positives (Paper Lanterns 2017 <-
a 2001 file, Copper Ridge: Second Run 2010 <- the 1987 original, The Quiet Atoll 2026 <- a 1988 file matched by
its German alternate title, Steel Sentinel 1987 <- the 2014 remake, Cold Ledger 1998 <- a 2024 file), the remake
Steel Sentinel (2014) with its right file, Harbor Lights Live (TMDB re-dated 2020 -> 2025), and two long cuts
(The Crown of Embers Extended with a German name, Night Wardens Ultimate Cut). Also synthetic edge cases and the
cron flow (one note per new suspect, ack, replaced file, failure note, GET only).
Run: python3 ~/opt/arr-mismatch-tests/test_arr_mismatch.py"""

import atexit
import copy
import glob
import importlib.machinery
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = tempfile.mkdtemp(prefix="arr-mismatch-test-")
atexit.register(shutil.rmtree, TMP, True)
os.environ["ARR_MISMATCH_STATE"] = os.path.join(TMP, "state.json")
os.environ["ARR_MISMATCH_INBOX"] = os.path.join(TMP, "inbox")
os.environ["MEDIA_SECRETS"] = os.path.join(TMP, "no-secrets.json")  # a real call would fail, never reach the server
sys.dont_write_bytecode = True
_ldr = importlib.machinery.SourceFileLoader(
    "arr_mismatch", os.environ.get("ARR_MISMATCH_BIN") or "/home/node/bin/arr-mismatch"
)
AM = importlib.util.module_from_spec(importlib.util.spec_from_loader("arr_mismatch", _ldr))
_ldr.exec_module(AM)

FIX = json.loads((HERE / "fixtures/radarr_library.json").read_text())
P = F = 0


def ok(name, cond, got=""):
    global P, F
    if cond:
        P += 1
        print(f"ok   - {name}")
    else:
        F += 1
        print(f"FAIL - {name}\n       got: {str(got)[:700]}")


class Fake:
    """Serves the fixture; records every call; fails on demand."""

    def __init__(self, movies=None, parse=None, fail=False):
        self.movies = copy.deepcopy(FIX["movies"] if movies is None else movies)
        self.parse = dict(FIX["parse"], **(parse or {}))
        self.fail, self.calls = fail, []

    def __call__(self, svc, method, path, body=None, timeout=None):
        self.calls.append((svc, method, path))
        if self.fail:
            raise AM.ApiError("cannot reach radarr: test")
        if path == "/api/v3/movie":
            return self.movies
        name = urllib.parse.unquote(path.split("title=", 1)[1])
        return self.parse.get(name) or {"parsedMovieInfo": None, "movie": None}


def movie(title, year):
    return next(m for m in FIX["movies"] if m["title"] == title and m["year"] == year)


def verdict(m, parse=None):
    rel = AM.pick([(n, (parse or FIX["parse"]).get(n)) for n in AM.names(m["movieFile"])])
    return AM.check(m, rel)


def synth(title, year, runtime, name, p_title, p_year, file_rt, alts=(), edition="", f_edition=""):
    m = {
        "id": 99,
        "title": title,
        "originalTitle": title,
        "year": year,
        "secondaryYear": None,
        "runtime": runtime,
        "hasFile": True,
        "alternateTitles": [{"title": a} for a in alts],
        "movieFile": {
            "id": 999,
            "sceneName": name,
            "originalFilePath": None,
            "relativePath": f"{title} ({year}).mkv",
            "edition": f_edition,
            "size": 5 << 30,
            "dateAdded": "2026-10-01T00:00:00Z",
            "mediaInfo": {"runTime": file_rt},
        },
    }
    parse = {
        name: {
            "parsedMovieInfo": {"movieTitles": [p_title], "movieTitle": p_title, "year": p_year, "edition": edition},
            "movie": None,
        }
    }
    return m, parse


# ---------- pure helpers ----------
ok("minutes h:mm:ss", AM.minutes("1:57:03") == 117.05, AM.minutes("1:57:03"))
ok("minutes mm:ss", abs(AM.minutes("57:30") - 57.5) < 1e-9, AM.minutes("57:30"))
ok("minutes empty/zero -> None", AM.minutes("") is None and AM.minutes("0:00:00") is None and AM.minutes(None) is None)
ok("minutes garbage -> None", AM.minutes("abc") is None)
ok(
    "names order: scene, original folder, original file, relativePath",
    AM.names({"sceneName": "A.2001", "originalFilePath": "Dir.2001/a.2001.mkv", "relativePath": "A (2017).mkv"})
    == ["A.2001", "Dir.2001", "a.2001", "A (2017)"],
    AM.names({"sceneName": "A.2001", "originalFilePath": "Dir.2001/a.2001.mkv", "relativePath": "A (2017).mkv"}),
)
ok(
    "names dedupe + extension only stripped from video files",
    AM.names(
        {
            "sceneName": "X.2020.1080p-GRP",
            "originalFilePath": "X.2020.1080p-GRP/X.2020.1080p-GRP.mkv",
            "relativePath": "Mr. Smith.2020.H.264",
        }
    )
    == ["X.2020.1080p-GRP", "Mr. Smith.2020.H.264"],
)

# ---------- true positives (fixture data) ----------
for t, y, _why in [
    ("Paper Lanterns", 2017, "year 2001"),
    ("Copper Ridge: Second Run", 2010, "year 1987"),
    ("The Quiet Atoll", 2026, "year 1988"),
    ("Steel Sentinel", 1987, "year 2014"),
    ("Cold Ledger", 1998, "year 2024"),
]:
    r = verdict(movie(t, y))
    ok(f"TP {t} ({y}) flagged", r["suspect"], r)
ok(
    "The Quiet Atoll: alt title + close runtime cannot clear a 38-year gap",
    verdict(movie("The Quiet Atoll", 2026))["bad"] == ["year"],
    verdict(movie("The Quiet Atoll", 2026)),
)
ok("Copper Ridge: year + title disagree", verdict(movie("Copper Ridge: Second Run", 2010))["bad"] == ["year", "title"])

# ---------- remake with the same title, right file ----------
r = verdict(movie("Steel Sentinel", 2014))
ok(
    "remake Steel Sentinel (2014) with its own file: not flagged, all signals agree",
    not r["suspect"] and r["bad"] == [] and r["year_off"] == 0,
    r,
)
m, p = synth(
    "The Hollow Ice", 2011, 103, "The.Hollow.Ice.2011.1080p.BluRay.x264-GRP", "The Hollow Ice", 2011, "1:43:10"
)
ok("synthetic remake The Hollow Ice (2011) right file: not flagged", not verdict(m, p)["suspect"], verdict(m, p))
m, p = synth(
    "The Hollow Ice", 2011, 103, "The.Hollow.Ice.1982.1080p.BluRay.x264-GRP", "The Hollow Ice", 1982, "1:49:00"
)
ok("synthetic: the 1982 original in the 2011 remake's slot IS flagged", verdict(m, p)["suspect"], verdict(m, p))

# ---------- director's cut / extended ----------
for t, y in [("The Crown of Embers", 2003), ("Night Wardens", 2009)]:
    r = verdict(movie(t, y))
    ok(f"long cut {t}: not flagged", not r["suspect"], r)
m, p = synth(
    "Harrow Keep",
    2005,
    144,
    "Die.Feste.Harrow.2005.Directors.Cut.German.1080p",
    "Die Feste Harrow",
    2005,
    "3:10:00",
    edition="Directors Cut",
)
r = verdict(m, p)
ok(
    "director's cut, unknown German title, +46 min: runtime not counted -> not flagged",
    not r["suspect"] and r["runtime"] is False and r["title"] is True,
    r,
)
m, p = synth("Harrow Keep", 2005, 144, "Die.Feste.Harrow.2005.German.1080p", "Die Feste Harrow", 2005, "3:10:00")
ok("same file without an edition marker: title + runtime disagree -> flagged", verdict(m, p)["suspect"], verdict(m, p))
m, p = synth(
    "Harrow Keep", 2005, 144, "Harrow.Keep.2005.1080p", "Harrow Keep", 2005, "3:10:00", f_edition="Director's Cut"
)
ok("edition from Radarr's movieFile also counts", verdict(m, p)["runtime"] is False, verdict(m, p))

# ---------- false-positive guards ----------
r = verdict(movie("Harbor Lights Live", 2025))
ok(
    "Harbor Lights Live (TMDB re-dated 2020 -> 2025, main title, runtime 160 = 160): not flagged",
    not r["suspect"] and r["redated"],
    r,
)
m, p = synth("Harbor Lights Live", 2025, 160, "Harbor.Lights.Live.2020.1080p", "Harbor Lights Live", 2020, "2:20:00")
ok("re-dated exemption needs the runtime to match (140 vs 160 -> flagged)", verdict(m, p)["suspect"], verdict(m, p))
m, p = synth("Foo", 2026, 100, "Bar.1988.1080p", "Bar", 1988, "1:40:00", alts=["Bar"])
ok("re-dated exemption needs the MAIN title, an alternate one is not enough", verdict(m, p)["suspect"], verdict(m, p))
m, p = synth("Foo", 2020, 100, "Foo.2022.1080p", "Foo", 2022, "1:40:00")
ok("year off by 2 alone: not flagged", not verdict(m, p)["suspect"], verdict(m, p))
m, p = synth("Foo", 2020, 100, "Foo.2019.1080p", "Foo", 2019, "1:40:00")
ok("year off by 1: agrees", verdict(m, p)["year"] is False)
m, p = synth("Foo", 2020, 100, "Bar.2020.1080p", "Bar", 2020, "1:40:00")
ok("title alone (foreign title not in alts): not flagged", not verdict(m, p)["suspect"])
m, p = synth("Foo", 2020, 100, "Foo.2020.1080p", "Foo", 2020, "2:30:00")
ok("runtime alone: not flagged", not verdict(m, p)["suspect"])
m, p = synth("Foo", 2020, 100, "Foo.2020.1080p", "Foo", 2020, "1:54:00")
ok("runtime +14 min (> 20% of 60 would be, but <= 15 min): agrees", verdict(m, p)["runtime"] is False)
m, p = synth("Foo", 2020, 60, "Foo.2020.1080p", "Foo", 2020, "1:16:00")
ok("runtime +16 min on a 60-min film (27%): disagrees", verdict(m, p)["runtime"] is True)
m, p = synth("Foo", 2020, 0, "Bar.2020.1080p", "Bar", 2020, "")
ok(
    "missing runtime is unknown, not a disagreement (title alone -> not flagged)",
    verdict(m, p)["runtime"] is None and verdict(m, p)["title"] and not verdict(m, p)["suspect"],
    verdict(m, p),
)
m, p = synth("Foo", 2020, 100, "Bar.2022.1080p", "Bar", 2022, "")
ok("year off 2 + title, runtime unknown: 2 signals -> flagged", verdict(m, p)["suspect"], verdict(m, p))
m, p = synth("Foo", 2020, 100, "Bar.2022.1080p", "Bar", 2022, "2:30:00")
ok("year off 2 + title + runtime: flagged", verdict(m, p)["suspect"])
m, p = synth("Foo", 2020, 100, "obfuscated123", None, None, "1:40:00")
ok("no parsed year/title: nothing known, not flagged", not verdict(m, p)["suspect"], verdict(m, p))

# ---------- scan + cron flow ----------
fake = Fake()
AM.api = fake
sus, keys = AM.scan()
names = sorted(f"{s['title']} ({s['year']})" for s in sus)
ok(
    "scan finds exactly the 5 true positives",
    names
    == [
        "Cold Ledger (1998)",
        "Copper Ridge: Second Run (2010)",
        "Paper Lanterns (2017)",
        "Steel Sentinel (1987)",
        "The Quiet Atoll (2026)",
    ],
    names,
)
ok(
    "scan is read-only (GET only, movie + parse)",
    all(c[1] == "GET" and c[2].split("?")[0] in ("/api/v3/movie", "/api/v3/parse") for c in fake.calls),
    fake.calls,
)
ok("scan keys cover all 9 files", len(keys) == 9, keys)

inbox = os.environ["ARR_MISMATCH_INBOX"]


def notes():
    return sorted(glob.glob(os.path.join(inbox, "arr-mismatch-*.md")))


AM.ack(["radarr:501", "radarr:502", "radarr:503"])  # seed the 3 the owner is asked about
st = AM.load_state()
ok("ack seeds 3 known (acked)", len(st["known"]) == 3 and all(v["acked"] for v in st["known"].values()), st)
AM.cron()
n1 = notes()
body = Path(n1[0]).read_text() if n1 else ""
ok(
    "first cron: one note with only the 2 un-seeded suspects",
    len(n1) == 1 and "Steel Sentinel (1987)" in body and "Cold Ledger (1998)" in body and "Paper Lanterns" not in body,
    body,
)
ok("note says it is data, read-only", "not instructions" in body and "nothing was changed" in body)
AM.cron()
ok("second cron: nothing new, no note", len(notes()) == 1, notes())
# the Cold Ledger file is replaced by a new (still wrong) file -> new key, alerts again; old key pruned
mv = copy.deepcopy(FIX["movies"])
neg = next(m for m in mv if m["title"] == "Cold Ledger")
neg["movieFile"]["id"] = 77777
AM.api = Fake(movies=mv)
AM.cron()
st = AM.load_state()
ok(
    "replaced file: new key alerts, old key forgotten",
    len(notes()) == 2 and "radarr:77777" in st["known"] and "radarr:505" not in st["known"],
    st["known"].keys(),
)
# failures: one note after 2 consecutive failures, not on the first or third
AM.api = Fake(fail=True)
AM.cron()
a = len(notes())
AM.cron()
b = len(notes())
AM.cron()
c = len(notes())
ok("API failure: note only on the 2nd consecutive failure", (a, b, c) == (2, 3, 3), (a, b, c))
AM.api = Fake(movies=mv)
AM.cron()
ok("success resets the failure count", AM.load_state()["fails"] == 0)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
