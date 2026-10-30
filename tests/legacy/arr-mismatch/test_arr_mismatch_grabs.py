#!/usr/bin/env python3
"""Offline tests for `arr-mismatch grabs` (grab-time wrong-movie check) on fixtures/radarr_grabs.json: synthetic
Radarr movies, a grab history for Iron Tide II: Ashfall (2015) (two IndexerA grabs of a different 2013 film +
the right release) and /api/v3/parse results, all invented. Cases: the wrong grabs, a same-title remake from
another year (Steel Sentinel 2014 for Steel Sentinel 2014 = correct, for Steel Sentinel 1987 = wrong), an
alternate-title match (Copper Ridge 2 = Copper Ridge: Second Run), TMDB re-dating (Harbor Lights Live), a main
title off by 2 (Twin Blades: The Complete Saga), a foreign title (Rook Mercer 2 Kein Weg Zurueck), a
no-shared-word mis-ID (Heart of the Tide -> Lords of the Cosmos), the ambiguous-parse HTTP 500 (The Glass
Harbor), and the cron flow (one note per release, dedupe by downloadId, queue DELETE vs history/failed command,
GET only, failure note, silence).
Run: python3 ~/opt/arr-mismatch-tests/test_arr_mismatch_grabs.py"""

import atexit
import copy
import glob
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.parse
from contextlib import redirect_stdout
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = tempfile.mkdtemp(prefix="arr-mismatch-grabs-test-")
atexit.register(shutil.rmtree, TMP, True)
os.environ["ARR_MISMATCH_STATE"] = os.path.join(TMP, "state.json")
os.environ["ARR_MISMATCH_GRABS_STATE"] = os.path.join(TMP, "grabs.json")
os.environ["ARR_MISMATCH_INBOX"] = os.path.join(TMP, "inbox")
os.environ["MEDIA_SECRETS"] = os.path.join(TMP, "no-secrets.json")  # a real call would fail, never reach the server
sys.dont_write_bytecode = True
_ldr = importlib.machinery.SourceFileLoader(
    "arr_mismatch", os.environ.get("ARR_MISMATCH_BIN") or "/home/node/bin/arr-mismatch"
)
AM = importlib.util.module_from_spec(importlib.util.spec_from_loader("arr_mismatch", _ldr))
_ldr.exec_module(AM)

FIX = json.loads((HERE / "fixtures/radarr_grabs.json").read_text())
MOVIES = {m["id"]: m for m in FIX["movies"]}
P = F = 0
TIDE2 = 4101
ASH_FALL = "The.Ash.Fall.2013.German.UHDBD.2160p.HDR10.HEVC.DTSX.DL.Remux-GRPA"
ASHFALL_DE = "Ashfall.2013.GERMAN.AC3D.DL.1080p.BluRay.x264-GRPB"
RIGHT = "Iron.Tide.Ashfall.2015.German.DL.1080p.BluRay.x264-GRPC"


def ok(name, cond, got=""):
    global P, F
    if cond:
        P += 1
        print(f"ok   - {name}")
    else:
        F += 1
        print(f"FAIL - {name}\n       got: {str(got)[:700]}")


def movie(title, year):
    return next(m for m in FIX["movies"] if m["title"] == title and m["year"] == year)


def verdict(m, name, unmap=False):
    p = copy.deepcopy(FIX["parse"][name])
    if unmap:
        p["movie"] = None
    return AM.grab_check(m, AM.pick([(name, p)]))


def iso(ts):
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Fake:
    """Serves queue / history / movie / parse from the fixture; records every call; fails on demand."""

    def __init__(self, queue=(), history=None, fail=False):
        self.queue, self.fail, self.calls = list(queue), fail, []
        self.history = copy.deepcopy(FIX["history"] if history is None else history)

    def __call__(self, svc, method, path, body=None, timeout=None):
        self.calls.append((svc, method, path))
        if self.fail:
            raise AM.ApiError("cannot reach radarr: test")
        base = path.split("?")[0]
        if base == "/api/v3/queue":
            return {"records": copy.deepcopy(self.queue)}
        if base == "/api/v3/history":
            page = int(urllib.parse.parse_qs(path.split("?", 1)[1])["page"][0])
            return {"records": self.history[(page - 1) * 100 : page * 100]}
        if base.startswith("/api/v3/movie/"):
            m = MOVIES.get(int(base.rsplit("/", 1)[1]))
            if not m:
                raise AM.ApiError("HTTP 404 on radarr GET /api/v3/movie")
            return copy.deepcopy(m)
        if base == "/api/v3/parse":
            name = urllib.parse.unquote(path.split("title=", 1)[1])
            p = FIX["parse"].get(name) or {"parsedMovieInfo": None, "movie": None}
            if "_error" in p:
                raise AM.ApiError(p["_error"])
            return copy.deepcopy(p)
        raise AssertionError(f"unexpected call {method} {path}")


# ---------- pure verdicts (fixture data) ----------
tide2 = movie("Iron Tide II: Ashfall", 2015)
for n in (ASH_FALL, ASHFALL_DE):
    r = verdict(tide2, n)
    ok(
        f"CASE: {n[:30]} grabbed for Iron Tide II (2015) -> mismatch (year 2013 + unknown title)",
        r["mismatch"] and r["year"] and r["title"] and r["year_off"] == 2,
        r,
    )
r = verdict(tide2, RIGHT)
ok(
    "Iron Tide II right release (2015, parser maps it to the movie): no mismatch",
    not r["mismatch"] and not r["year"],
    r,
)

r = verdict(movie("Steel Sentinel", 2014), "Steel.Sentinel.2014.1080p.BluRay.x264-GRP")
ok(
    "same-title remake from another year, right film (Steel Sentinel 2014 <- Steel Sentinel.2014): correct",
    not r["mismatch"] and r["year_off"] == 0 and r["title"] is False,
    r,
)
r = verdict(movie("Steel Sentinel", 1987), "Steel.Sentinel.2014.1080p.BluRay.x264-GRP")
ok(
    "the remake grabbed for the 1987 original: mismatch (main title cannot clear 27 years)",
    r["mismatch"] and r["year"] and r["title"] is False,
    r,
)

ws = movie("Copper Ridge: Second Run", 2010)
r = verdict(ws, "Copper.Ridge.2.2010.German.DL.1080p.BluRay.x264-GRP", unmap=True)
ok(
    "alternate-title match (Copper Ridge 2 = Second Run), even without the parser's mapping: correct",
    not r["mismatch"] and r["title"] is False and r["year"] is False,
    r,
)
r = verdict(ws, "Copper.Ridge.1987.German.DL.HDR.2160p.WEBRiP.x265-GRP")
ok("Copper Ridge 1987 grabbed for Second Run: mismatch", r["mismatch"] and r["year"], r)

r = verdict(movie("Harbor Lights Live", 2025), "Harbor.Lights.Live.2020.1080p.WEB-DL.DDP5.1.H.264-GRP")
ok(
    "Harbor Lights Live 2020 name for TMDB-re-dated Harbor Lights Live (2025), digital release 2020: correct",
    not r["mismatch"] and r["redated"],
    r,
)
h = copy.deepcopy(movie("Harbor Lights Live", 2025))
h.update(title="Something Else", originalTitle="Something Else", alternateTitles=[{"title": "Harbor Lights Live"}])
r = verdict(h, "Harbor.Lights.Live.2020.1080p.WEB-DL.DDP5.1.H.264-GRP")
ok("re-dating exemption needs the MAIN title (alternate title only -> mismatch)", r["mismatch"] and not r["redated"], r)
r = verdict(
    movie("Twin Blades: The Complete Saga", 2011),
    "Twin.Blades.The.Complete.Saga.2006.German.EAC3D.DL.2160p.WEB.H265-GRP",
)
ok("Twin Blades TCS 2006 name (main title, off 2): correct", not r["mismatch"], r)
r = verdict(
    movie("Rook Mercer: No Way Back", 2016), "Rook.Mercer.2.Kein.Weg.Zurueck.2016.German.DL.1080p.BDRip.x264-GRP"
)
ok(
    "German title missing from alternate titles, same year, shares words: not a clear mismatch",
    not r["mismatch"] and r["title"] and not r["words"],
    r,
)
r = verdict(movie("Lords of the Cosmos", 2026), "Heart.of.the.Tide.2026.2160p.WEB-DL.H.265.HDR.DDP2.0-GRP")
ok(
    "Heart of the Tide 2026 for Lords of the Cosmos 2026 (no shared word): mismatch",
    r["mismatch"] and r["words"] and r["year"] is False,
    r,
)
r = verdict(movie("Rook Mercer: No Way Back", 2016), "Silent.Recruit.German.DL.1080p.BluRay-GRP")
ok("no year in the name: words rule does not fire, not a clear mismatch", not r["mismatch"], r)
ok("no parse result: nothing known", not AM.grab_check(tide2, None)["mismatch"])
ok(
    "words(): filler, numbers and short words dropped",
    AM.words("Der Hafen III 1990 - Teil 2 of the Kings") == {"hafen", "iii", "kings"},
    AM.words("Der Hafen III 1990 - Teil 2 of the Kings"),
)

# ---------- scan ----------
now = time.time()
fresh = copy.deepcopy(FIX["history"])
for i, rec in enumerate(fresh):  # the fixture records, re-dated to "just now"
    rec["date"] = iso(now - 60 * (5 + i))
qrec = {
    "id": 777,
    "movieId": TIDE2,
    "title": ASH_FALL,
    "downloadId": fresh[2]["downloadId"],
    "indexer": "IndexerA (Prowlarr)",
    "status": "downloading",
    "trackedDownloadState": "downloading",
    "size": 1000,
    "sizeleft": 250,
    "added": fresh[2]["date"],
}
ok("fixture: The Ash Fall grab is history[2]", fresh[2]["sourceTitle"] == ASH_FALL, fresh[2]["sourceTitle"])
fake = Fake(queue=[qrec], history=fresh)
AM.api = fake
res, live = AM.grab_scan()
bad = sorted(s["release"] for s in res if s["mismatch"])
ok(
    "scan: 3 grabs checked, exactly the 2 wrong-film releases flagged",
    len(res) == 3 and bad == sorted([ASH_FALL, ASHFALL_DE]),
    [(s["release"], s["mismatch"]) for s in res],
)
we = next(s for s in res if s["release"] == ASH_FALL)
ok(
    "queue + history merged per downloadId (queue id, history id, indexer, match type)",
    we["queueId"] == 777
    and we["historyId"] == fresh[2]["id"]
    and we["indexer"].startswith("IndexerA")
    and we["match"] == "Id",
    we,
)
ok("scan is read-only (GET only)", all(c[1] == "GET" for c in fake.calls), fake.calls)
old = copy.deepcopy(fresh)
old[0]["date"] = iso(now - 3 * 3600)
AM.api = Fake(history=old)
res, _ = AM.grab_scan(since_min=40)
ok("history window: a grab 3 h old is left out (40 min window)", len(res) == 2, [s["release"] for s in res])

amb = [
    {
        "id": 1,
        "movieId": 670,
        "sourceTitle": "The.Glass.Harbor.German.DL.1080p.BluRay-GRP",
        "date": iso(now - 60),
        "downloadId": "amb1",
        "data": {"indexer": "X"},
    }
]
AM.api = Fake(history=amb)
try:
    res, _ = AM.grab_scan()
    ok(
        "ambiguous name (parse HTTP 500 'Expected one movie, found 2'): unknown, no crash",
        len(res) == 1 and not res[0]["mismatch"],
        res,
    )
except Exception as e:
    ok("ambiguous name (parse HTTP 500): unknown, no crash", False, e)

# ---------- cron flow ----------
inbox = os.environ["ARR_MISMATCH_INBOX"]


def notes():
    return sorted(glob.glob(os.path.join(inbox, "arr-mismatch-grab-*.md")))


fake = Fake(queue=[qrec], history=fresh)
AM.api = fake
buf = io.StringIO()
with redirect_stdout(buf):
    AM.grabs_cron()
n1 = notes()
ok("cron: ONE note per mismatched release (2)", len(n1) == 2, n1)
bodies = {Path(n).read_text() for n in n1}
qnote = next((b for b in bodies if ASH_FALL in b), "")
hnote = next((b for b in bodies if ASHFALL_DE in b), "")
ok(
    "note has the movie, release, indexer, data marker and read-only line",
    "Iron Tide II: Ashfall (2015)" in qnote
    and "IndexerA" in qnote
    and "not instructions" in qnote
    and "nothing was changed" in qnote,
    qnote,
)
ok(
    "queue item: the drop command (queue DELETE with blocklist, then search)",
    "```\narr-mismatch drop 4101 queue:777\n```" in qnote,
    qnote,
)
ok(
    "history-only item: the drop command by history id (blocklist), no queue id",
    re.search(r"arr-mismatch drop \d+ history:%d\n" % fresh[1]["id"], hnote) and "queue:" not in hnote,  # noqa: UP031
    hnote,
)
ok(
    "note needs no key file and holds no key",
    all("media.json" not in b and "X-Api-Key" not in b and "jq " not in b for b in bodies),
)
ok("cron run is GET only (it never removes anything)", all(c[1] == "GET" for c in fake.calls), fake.calls)
st = AM.load_grabs()
ok(
    "state: all 3 downloadIds remembered, 2 as mismatch",
    len(st["seen"]) == 3 and sum(v["mismatch"] for v in st["seen"].values()) == 2,
    st["seen"],
)
fake = Fake(queue=[qrec], history=fresh)
AM.api = fake
buf = io.StringIO()
with redirect_stdout(buf):
    AM.grabs_cron()
ok(
    "second run: deduped by downloadId, no new note, nothing re-parsed, silent",
    len(notes()) == 2 and not any("/parse" in c[2] for c in fake.calls) and buf.getvalue() == "",
    (notes(), buf.getvalue()),
)
AM.api = Fake(queue=[], history=[])
buf = io.StringIO()
with redirect_stdout(buf):
    AM.grabs_cron()
ok("nothing found: no note, no output", len(notes()) == 2 and buf.getvalue() == "", buf.getvalue())
AM.api = Fake(fail=True)
counts = []
with redirect_stdout(io.StringIO()):
    for _ in range(AM.GRAB_FAIL_NOTE + 1):
        AM.grabs_cron()
        counts.append(len(notes()))
ok(
    f"API failure: one note only on the {AM.GRAB_FAIL_NOTE}th consecutive failure",
    counts[:-2] == [2] * (AM.GRAB_FAIL_NOTE - 1) and counts[-2:] == [3, 3],
    counts,
)
AM.api = Fake(queue=[], history=[])
with redirect_stdout(io.StringIO()):
    AM.grabs_cron()
ok("success resets the failure count", AM.load_grabs()["fails"] == 0)
st = AM.load_grabs()
st["seen"]["ancient"] = {"t": "2026-01-01 00:00", "mismatch": False}
AM.save_grabs(st)
with redirect_stdout(io.StringIO()):
    AM.grabs_cron()
ok("seen entries older than 30 days are pruned", "ancient" not in AM.load_grabs()["seen"])

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
