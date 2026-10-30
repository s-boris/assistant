#!/usr/bin/env python3
"""Offline tests for `arr-mismatch grabs --cron` AUTO-DROP on fixtures/radarr_grabs.json (synthetic
Iron Tide II grabs: a different 2013 film x2 from IndexerA) + fixtures/radarr_releases.json (synthetic interactive
search answers). Cases: queue drop + history mark-failed + ONE correct regrab, regrab with no match (left missing),
an already-imported item (queue state and history import event: never dropped), the per-movie cap (3 in 7 days, older
ones do not count), the per-run cap, the kill switch (file, --no-drop, CLI default = drop), the dry run, the drops log,
and that library files are never touched. Never reaches a server (MEDIA_SECRETS points to a missing file).
Run: python3 ~/opt/arr-mismatch-tests/test_arr_mismatch_drops.py"""

import atexit
import copy
import glob
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from contextlib import redirect_stdout
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = tempfile.mkdtemp(prefix="arr-mismatch-drops-test-")
atexit.register(shutil.rmtree, TMP, True)
os.environ["ARR_MISMATCH_STATE"] = os.path.join(TMP, "state.json")
os.environ["ARR_MISMATCH_GRABS_STATE"] = os.path.join(TMP, "grabs.json")
os.environ["ARR_MISMATCH_INBOX"] = os.path.join(TMP, "inbox")
os.environ["ARR_MISMATCH_NO_DROP"] = os.path.join(TMP, "no-drop")
os.environ["ARR_MISMATCH_DROPS_LOG"] = os.path.join(TMP, "drops.log")
os.environ["MEDIA_SECRETS"] = os.path.join(TMP, "no-secrets.json")  # a real call would fail, never reach the server
sys.dont_write_bytecode = True
_ldr = importlib.machinery.SourceFileLoader(
    "arr_mismatch", os.environ.get("ARR_MISMATCH_BIN") or "/home/node/bin/arr-mismatch"
)
AM = importlib.util.module_from_spec(importlib.util.spec_from_loader("arr_mismatch", _ldr))
_ldr.exec_module(AM)

FIX = json.loads((HERE / "fixtures/radarr_grabs.json").read_text())
RELS = json.loads((HERE / "fixtures/radarr_releases.json").read_text())
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
        print(f"FAIL - {name}\n       got: {str(got)[:900]}")


def iso(ts):
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Fake:
    """Radarr from the fixtures: queue, history (paged + by downloadId), movie, parse, release search/grab, queue
    DELETE, history/failed. Records (method, path, body). Anything touching movie files fails the test."""

    def __init__(self, queue=(), history=(), releases=(), events=()):
        self.queue, self.calls = copy.deepcopy(list(queue)), []
        self.history, self.events = copy.deepcopy(list(history)), copy.deepcopy(list(events))
        self.releases = copy.deepcopy(list(releases))

    def __call__(self, svc, method, path, body=None, timeout=None):
        self.calls.append((method, path, body))
        base, qs = path.split("?")[0], urllib.parse.parse_qs(path.split("?", 1)[1] if "?" in path else "")
        if "moviefile" in base.lower():
            raise AssertionError(f"library file call {method} {path}")
        if base == "/api/v3/queue" and method == "GET":
            return {"records": copy.deepcopy(self.queue)}
        if base.startswith("/api/v3/queue/") and method == "DELETE":
            qid = int(base.rsplit("/", 1)[1])
            self.queue = [r for r in self.queue if r["id"] != qid]
            return None
        if base == "/api/v3/history" and method == "GET":
            if "downloadId" in qs:
                did = qs["downloadId"][0]
                return {"records": [r for r in self.history + self.events if r["downloadId"] == did]}
            page = int(qs["page"][0])
            return {"records": self.history[(page - 1) * 100 : page * 100]}
        if base.startswith("/api/v3/history/failed/") and method == "POST":
            return None
        if base.startswith("/api/v3/movie/") and method == "GET":
            return copy.deepcopy(MOVIES[int(base.rsplit("/", 1)[1])])
        if base == "/api/v3/parse":
            name = urllib.parse.unquote(path.split("title=", 1)[1])
            return copy.deepcopy(FIX["parse"].get(name) or {"parsedMovieInfo": None, "movie": None})
        if base == "/api/v3/release" and method == "GET":
            return copy.deepcopy(self.releases)
        if base == "/api/v3/release" and method == "POST":
            return dict(body or {})
        raise AssertionError(f"unexpected call {method} {path}")

    def writes(self):
        return [(m, p, b) for m, p, b in self.calls if m != "GET"]


now = time.time()
hist = copy.deepcopy(FIX["history"])
for i, rec in enumerate(hist):  # the fixture records, re-dated to "just now"
    rec["date"] = iso(now - 60 * (5 + i))
H_RIGHT, H_EW, H_WE = hist  # fixture order: right grab, Ashfall, The Ash Fall
ok("fixture order", (H_RIGHT["sourceTitle"], H_EW["sourceTitle"], H_WE["sourceTitle"]) == (RIGHT, ASHFALL_DE, ASH_FALL))
QREC = {
    "id": 777,
    "movieId": TIDE2,
    "title": ASH_FALL,
    "downloadId": H_WE["downloadId"],
    "indexer": "IndexerA (Prowlarr)",
    "status": "downloading",
    "trackedDownloadState": "downloading",
    "size": 1000,
    "sizeleft": 250,
    "added": H_WE["date"],
}
inbox = os.environ["ARR_MISMATCH_INBOX"]


def grab_notes():
    return sorted(glob.glob(os.path.join(inbox, "arr-mismatch-grab-*.md")))


def drop_notes():
    return sorted(glob.glob(os.path.join(inbox, "arr-mismatch-drop-*.md")))


def logtext():
    return Path(AM.DROPS_LOG).read_text() if os.path.exists(AM.DROPS_LOG) else ""


def reset():
    for p in (AM.GRABS_STATE, AM.DROPS_LOG, AM.NO_DROP_FILE):
        if os.path.exists(p):
            os.remove(p)
    shutil.rmtree(inbox, True)
    AM.DROP_RUN_MAX = 10


def run(fake, **kw):
    AM.api = fake
    buf = io.StringIO()
    with redirect_stdout(buf):
        AM.grabs_cron(**kw)
    return buf.getvalue()


# ---------- 1. queue drop + history mark-failed + ONE correct regrab ----------
reset()
fake = Fake(queue=[QREC], history=[H_EW, H_WE], releases=RELS["match"])
out = run(fake, drop=True)
w = fake.writes()
ok(
    "queue item: exact DELETE removeFromClient + blocklist + skipRedownload",
    ("DELETE", "/api/v3/queue/777?removeFromClient=true&blocklist=true&skipRedownload=true", None) in w,
    w,
)
ok(
    "history-only item (left the queue, not imported): POST history/failed/<grab id>",
    ("POST", f"/api/v3/history/failed/{H_EW['id']}", None) in w,
    w,
)
posts = [b for m, p, b in w if p == "/api/v3/release"]
ok(
    "ONE regrab for the movie (after both drops), the right GRPC release by guid + indexerId",
    posts == [{"guid": "guid-right", "indexerId": 23}],
    w,
)
ok(
    "interactive search by movieId, after the drops",
    [p for m, p, b in fake.calls if p.startswith("/api/v3/release?")] == [f"/api/v3/release?movieId={TIDE2}"]
    and fake.calls.index(w[0]) < next(i for i, c in enumerate(fake.calls) if c[1].startswith("/api/v3/release?")),
    fake.calls,
)
parsed = [urllib.parse.unquote(p.split("title=", 1)[1]) for m, p, b in fake.calls if p.startswith("/api/v3/parse")]
ok(
    "rejected release never considered, dropped release names skipped",
    "Iron.Tide.Ashfall.2015.2160p.UHD.BluRay.x265-GRP" not in parsed
    and parsed.count(ASH_FALL) == 1
    and parsed.count(ASHFALL_DE) == 1,
    parsed,
)
ok("only 3 writes: DELETE, history/failed, release POST (nothing else)", len(w) == 3, w)
ok(
    "no per-release note (it was handled), ONE drop note",
    grab_notes() == [] and len(drop_notes()) == 1,
    (grab_notes(), drop_notes()),
)
body = Path(drop_notes()[0]).read_text() if drop_notes() else ""
ok(
    "drop note: data marker, movie, both releases, regrab result, kill switch hint, no key",
    "not instructions" in body
    and "Iron Tide II: Ashfall (2015)" in body
    and ASH_FALL in body
    and ASHFALL_DE in body
    and f"regrabbed `{RIGHT}`" in body
    and "no-drop" in body
    and "X-Api-Key" not in body,
    body,
)
ok("drop note is short (<= 10 lines)", len(body.strip().splitlines()) <= 10, body)
lg = logtext()
ok(
    "log: DROP queue, DROP failed, REGRAB grabbed",
    "DROP queue 777 movie 4101" in lg and f"DROP failed {H_EW['id']} movie 4101" in lg and "REGRAB grabbed" in lg,
    lg,
)
st = AM.load_grabs()
ok(
    "state: 2 drops for the movie, downloadIds marked dropped",
    len(st["drops"].get(str(TIDE2), [])) == 2
    and st["seen"][H_WE["downloadId"]].get("action") == "dropped-queue"
    and st["seen"][H_EW["downloadId"]].get("action") == "dropped-failed",
    st,
)
ok("summary line counts drops + regrab", "2 dropped, 1 regrabbed" in out, out)
out2 = run(Fake(queue=[], history=[H_EW, H_WE], releases=RELS["match"]), drop=True)
ok("second run: deduped, nothing dropped again, silent", out2 == "" and len(drop_notes()) == 1, out2)

# ---------- 2. regrab with no match: left missing ----------
reset()
fake = Fake(queue=[QREC], history=[H_WE], releases=RELS["nomatch"])
run(fake, drop=True)
w = fake.writes()
ok(
    "no match: dropped, but no release grabbed",
    [p for m, p, b in w] == ["/api/v3/queue/777?removeFromClient=true&blocklist=true&skipRedownload=true"],
    w,
)
body = Path(drop_notes()[0]).read_text() if drop_notes() else ""
ok("no match: the note says it stays missing", "left missing" in body and "no approved release" in body, body)
ok("no match: logged", "REGRAB none movie 4101" in logtext(), logtext())

# Radarr already queued another download for the movie (e.g. its own re-search after mark-failed): no second grab
reset()
other = dict(QREC, id=778, title=RIGHT, downloadId="other-dl")
fake = Fake(queue=[QREC, other], history=[H_WE, dict(H_RIGHT, downloadId="other-dl")], releases=RELS["match"])
run(fake, drop=True)
ok(
    "another download already queued for the movie: dropped, not regrabbed",
    not any(p == "/api/v3/release" for m, p, b in fake.writes()) and "REGRAB queued" in logtext(),
    fake.writes(),
)

# ---------- 3. already imported: never dropped, note only ----------
reset()
imp = {
    "id": 99001,
    "movieId": TIDE2,
    "sourceTitle": ASHFALL_DE,
    "date": H_EW["date"],
    "downloadId": H_EW["downloadId"],
    "eventType": "downloadFolderImported",
    "data": {},
}
fake = Fake(
    queue=[dict(QREC, trackedDownloadState="imported")], history=[H_EW, H_WE], events=[imp], releases=RELS["match"]
)
run(fake, drop=True)
ok("imported (queue state imported / history import event): no write at all", fake.writes() == [], fake.writes())
ok("imported: no interactive search", not any(p.startswith("/api/v3/release") for m, p, b in fake.calls))
bodies = [Path(n).read_text() for n in grab_notes()]
ok(
    "imported: one per-release note each, as before, saying it was not dropped",
    len(bodies) == 2
    and all("already imported" in b and "nothing was changed" in b for b in bodies)
    and drop_notes() == [],
    bodies,
)
ok("imported: logged", logtext().count("IMPORTED note only") == 2, logtext())

# ---------- 4. caps ----------
reset()
recent = [datetime.fromtimestamp(now - d * 86400).strftime("%Y-%m-%d %H:%M") for d in (1, 2, 3)]
st = AM.load_grabs()
st["drops"] = {str(TIDE2): recent}
AM.save_grabs(st)
fake = Fake(queue=[QREC], history=[H_WE], releases=RELS["match"])
run(fake, drop=True)
body = Path(grab_notes()[0]).read_text() if grab_notes() else ""
ok(
    "cap reached (3 drops in 7 days): no write, a note 'indexer keeps mis-IDing'",
    fake.writes() == []
    and len(grab_notes()) == 1
    and "indexer keeps mis-IDing Iron Tide II" in body
    and drop_notes() == [],
    (fake.writes(), body),
)
ok("cap note still has the manual drop command", "arr-mismatch drop 4101 queue:777" in body, body)
ok("cap: logged", "CAPPED note only" in logtext(), logtext())

reset()
old = [datetime.fromtimestamp(now - d * 86400).strftime("%Y-%m-%d %H:%M") for d in (8, 9, 10)]
st = AM.load_grabs()
st["drops"] = {str(TIDE2): old + recent[:2]}
AM.save_grabs(st)
fake = Fake(queue=[QREC], history=[H_EW, H_WE], releases=RELS["match"])
run(fake, drop=True)
w = fake.writes()
ok(
    "drops older than 7 days do not count; the cap stops at the 3rd in the window (2 old + 1 now, then a note)",
    len([x for x in w if x[1].startswith(("/api/v3/queue/", "/api/v3/history/failed/"))]) == 1
    and len(grab_notes()) == 1
    and len(drop_notes()) == 1,
    (w, grab_notes()),
)
ok(
    "old drops pruned from the state",
    AM.load_grabs()["drops"][str(TIDE2)] and all(t not in AM.load_grabs()["drops"][str(TIDE2)] for t in old),
    AM.load_grabs()["drops"],
)

reset()
AM.DROP_RUN_MAX = 1
fake = Fake(queue=[QREC], history=[H_EW, H_WE], releases=RELS["match"])
run(fake, drop=True)
gb = [Path(n).read_text() for n in grab_notes()]
ok(
    "per-run cap (patched to 1): one dropped, the other noted with the per-run cap",
    len([x for x in fake.writes() if not x[1].startswith("/api/v3/release")]) == 1
    and len(gb) == 1
    and "per-run cap" in gb[0],
    (fake.writes(), gb),
)
AM.DROP_RUN_MAX = 10

# ---------- 5. kill switch ----------
reset()
Path(AM.NO_DROP_FILE).write_text("")
fake = Fake(queue=[QREC], history=[H_EW, H_WE], releases=RELS["match"])
run(fake, drop=True)
ok(
    "kill switch file: GET only, notes as before (one per release, with the commands), no log",
    fake.writes() == []
    and len(grab_notes()) == 2
    and drop_notes() == []
    and logtext() == ""
    and all("Auto-drop" not in Path(n).read_text() for n in grab_notes()),
    (fake.writes(), grab_notes()),
)
buf = io.StringIO()
with redirect_stdout(buf):
    AM.grabs_status()
ok("kill switch: --status says auto-drop OFF", "auto-drop: OFF" in buf.getvalue(), buf.getvalue())

reset()


def cli(*args, fake):
    AM.api = fake
    argv, sys.argv = sys.argv, ["arr-mismatch", *args]
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            AM.main()
    finally:
        sys.argv = argv
    return buf.getvalue()


fake = Fake(queue=[QREC], history=[H_WE], releases=RELS["match"])
cli("grabs", "--cron", "--no-drop", fake=fake)
ok("--no-drop: GET only, one note with the commands", fake.writes() == [] and len(grab_notes()) == 1, fake.writes())
reset()
fake = Fake(queue=[QREC], history=[H_WE], releases=RELS["match"])
cli("grabs", "--cron", fake=fake)
ok(
    "CLI `grabs --cron` (no kill switch) drops + regrabs by default",
    len(fake.writes()) == 2 and len(drop_notes()) == 1,
    fake.writes(),
)

# ---------- 6. dry run ----------
reset()
fake = Fake(queue=[QREC], history=[H_EW, H_WE], releases=RELS["match"])
out = cli("grabs", "--cron", "--dry-run", fake=fake)
ok(
    "--cron --dry-run: GET only, no notes, no state, no log; says what it would do",
    fake.writes() == []
    and grab_notes() == []
    and drop_notes() == []
    and not os.path.exists(AM.GRABS_STATE)
    and logtext() == ""
    and "DROP queue 777" in out
    and f"would regrab `{RIGHT}`" in out
    and "2 would be dropped" in out,
    (fake.writes(), out),
)

# ---------- regrab_ok: the year rule is +-1 (stricter than the mismatch check) ----------
tide2 = MOVIES[TIDE2]
ok("regrab_ok: right release", AM.regrab_ok(tide2, AM.pick([(RIGHT, FIX["parse"][RIGHT])])))
ok("regrab_ok: wrong movie refused", not AM.regrab_ok(tide2, AM.pick([(ASH_FALL, FIX["parse"][ASH_FALL])])))
p2 = copy.deepcopy(FIX["parse"][RIGHT])
p2["parsedMovieInfo"]["year"] = 2017
ok("regrab_ok: main title but year off by 2 refused (+-1 only)", not AM.regrab_ok(tide2, AM.pick([("x", p2)])))
p3 = copy.deepcopy(FIX["parse"][RIGHT])
p3["parsedMovieInfo"]["year"] = 0
ok("regrab_ok: no year refused", not AM.regrab_ok(tide2, AM.pick([("x", p3)])))

# ---------- drop MOVIEID queue:ID|history:ID (the note's command; owner OK first)
reset()
calls = []


def rec(svc, method, path, body=None, timeout=None):
    return calls.append((svc, method, path, body))


with redirect_stdout(io.StringIO()):
    AM.drop_one("4101", "queue:777", get=rec)
ok(
    "drop queue: DELETE with removeFromClient + blocklist + skipRedownload, then MoviesSearch, Radarr only",
    calls
    == [
        ("radarr", "DELETE", "/api/v3/queue/777?removeFromClient=true&blocklist=true&skipRedownload=true", None),
        ("radarr", "POST", "/api/v3/command", {"name": "MoviesSearch", "movieIds": [4101]}),
    ],
    calls,
)
ok("drop is logged", "drop movie 4101 queue:777" in logtext(), logtext())
calls.clear()
with redirect_stdout(io.StringIO()):
    AM.drop_one(4101, "history:55", get=rec)
ok(
    "drop history: history/failed (blocklist), then MoviesSearch",
    calls
    == [
        ("radarr", "POST", "/api/v3/history/failed/55", None),
        ("radarr", "POST", "/api/v3/command", {"name": "MoviesSearch", "movieIds": [4101]}),
    ],
    calls,
)
calls.clear()
with redirect_stdout(io.StringIO()) as buf:
    AM.drop_one(4101, "queue:777", get=rec, dry=True)
ok("drop --dry-run: no call", not calls and "would drop movie 4101 queue:777" in buf.getvalue(), buf.getvalue())
for bad in (("4101", "777"), ("4101", "file:777"), ("x", "queue:1"), ("4101", "queue:7a")):
    try:
        AM.drop_one(*bad, get=rec)
        ok(f"drop {bad}: refused", False)
    except ValueError:
        ok(f"drop {bad}: refused", not calls, calls)
ok(
    "drop_cmd: queue item -> the drop verb",
    AM.drop_cmd({"movieId": 4101, "queueId": 777}) == "arr-mismatch drop 4101 queue:777\n",
)
ok(
    "drop_cmd: history only -> the drop verb by history id",
    AM.drop_cmd({"movieId": 4101, "historyId": 55}) == "arr-mismatch drop 4101 history:55\n",
)
r = subprocess.run([sys.executable, _ldr.path, "drop", "4101", "bogus"], capture_output=True, text=True, timeout=60)
ok(
    "CLI drop with a bad ref: usage error (rc 2), nothing sent",
    r.returncode == 2 and "queue:ID" in r.stderr,
    (r.returncode, r.stderr[-200:]),
)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
