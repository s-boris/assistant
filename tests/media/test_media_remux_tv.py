"""media-remux-tv: plan picks (WEB 1080p by score, 2160p HDR per-episode cap, >= 50 % saved, language parity, season
packs first, per-episode fallback over the gateway cap), the dry run's GET-only contract, the night window and caps,
the off switch, the run lock (core.locks) and --follow's import override. Offline: an in-process fake Sonarr stands
in for ~/bin/media, a fake arr-unblock for the override; the clock is pinned with ASSISTANT_NOW."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import urllib.parse
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from core import locks

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "media-remux-tv"
ZRH = ZoneInfo("Europe/Zurich")
G = 1e9
NIGHT = "2026-10-31T01:00:00+01:00"  # inside the 00:30-06:00 window
DAY = "2026-10-31T14:00:00+01:00"
REJ_PREF = "Existing file on disk is of equal or higher preference: Bluray-1080p Remux v1"


class ApiError(Exception):
    pass


class FakeSonarr:
    """~/bin/media's get/api/ApiError/q/ZRH over route tables; every call is logged as (svc, METHOD, path)."""

    ApiError = ApiError
    ZRH = ZRH

    def __init__(self):
        self.routes = {"sonarr": {}, "radarr": {}}
        self.calls = []
        self.posts = []
        for svc in ("sonarr", "radarr"):
            self.routes[svc]["/api/v3/queue"] = {"records": [], "totalRecords": 0}

    @staticmethod
    def q(s):
        return urllib.parse.quote(str(s), safe="")

    def get(self, svc, path, timeout=None):
        return self.api(svc, "GET", path, timeout=timeout)

    def api(self, svc, method, path, body=None, timeout=None):
        self.calls.append((svc, method, path))
        if method != "GET":
            self.posts.append((svc, method, path, body))
            return {}
        for key in (path, path.split("?")[0]):
            if key in self.routes[svc]:
                v = self.routes[svc][key]
                if isinstance(v, Exception):
                    raise v
                return json.loads(json.dumps(v))
        raise ApiError(f"{svc}: no route for GET {path}")


class FakeUnblock:
    def __init__(self):
        self.verified, self.imported = [], []

    def verify_sonarr(self, recs, series, preview, parse):
        self.verified.append((recs[0].get("downloadId"), series.get("id")))
        return type("Dec", (), {"problems": [], "files": preview})()

    def import_now(self, dec):
        self.imported.append(dec.files)
        return "ok", "imported"


# ----------------------------------------------------------------------------- fixture data
def profile(pid, name, uhd=False, scores=None):
    names = ["WEBDL-1080p", "WEBRip-1080p", "Bluray-1080p"] + (["WEBDL-2160p", "Bluray-2160p"] if uhd else [])
    items = [{"quality": {"name": n}, "allowed": True} for n in names]
    items.append({"quality": {"name": "Bluray-1080p Remux"}, "allowed": uhd})
    sc = {
        "WEB Tier 01": 1700,
        "WEB Tier 02": 1650,
        "AMZN": 75,
        "x265 (HD)": -10000,
        "Obfuscated": -1000,
        "DV": 1500,
        "DV HDR10": 1500,
        "DV (w/o HDR fallback)": -10000,
        "HDR": 500,
    }
    sc.update(scores or {})
    return {
        "id": pid,
        "name": name,
        "items": items,
        "minFormatScore": 0,
        "formatItems": [{"name": k, "score": v} for k, v in sc.items()],
    }


def series(sid, title, pid, runtime=45, lang="English", alt=()):
    return {
        "id": sid,
        "title": title,
        "sortTitle": title.lower(),
        "year": 2010,
        "qualityProfileId": pid,
        "runtime": runtime,
        "originalLanguage": {"name": lang},
        "statistics": {"episodeFileCount": 9},
        "alternateTitles": [{"title": a} for a in alt],
    }


def efile(fid, season, size, quality="Bluray-1080p Remux", audio="eng"):
    return {
        "id": fid,
        "seasonNumber": season,
        "size": size,
        "quality": {"quality": {"name": quality}},
        "mediaInfo": {"audioLanguages": audio},
        "languages": [{"name": "English"}],
    }


def ep(eid, season, num, fid):
    return {"id": eid, "seasonNumber": season, "episodeNumber": num, "episodeFileId": fid, "hasFile": True}


def rel(
    title,
    size,
    quality="WEBDL-1080p",
    score=1700,
    cfs=("WEB Tier 01",),
    langs=("English",),
    pack=True,
    season=1,
    eps=(),
    proto="usenet",
    series_title="Old Show",
    sid=1,
    rej=(REJ_PREF,),
):
    return {
        "title": title,
        "guid": f"guid-{title}",
        "indexerId": 7,
        "indexer": "NZB",
        "protocol": proto,
        "size": size,
        "quality": {"quality": {"name": quality}},
        "customFormatScore": score,
        "customFormats": [{"name": c} for c in cfs],
        "languages": [{"name": x} for x in langs],
        "fullSeason": pack,
        "seasonNumber": season,
        "mappedSeasonNumber": season,
        "episodeNumbers": list(eps),
        "mappedEpisodeNumbers": list(eps),
        "seriesTitle": series_title,
        "mappedSeriesId": sid,
        "rejections": list(rej),
        "downloadAllowed": True,
    }


def world():
    f = FakeSonarr()
    s = f.routes["sonarr"]
    s["/api/v3/qualityprofile"] = [profile(6, "Up to 1080p"), profile(9, "Up to 4K", uhd=True)]
    s["/api/v3/series"] = [
        series(1, "Old Show", 6),
        series(2, "Krimi", 6, lang="German"),
        series(3, "Hochhaus", 6, lang="German"),
        series(4, "Space 4K", 9, runtime=60),
        series(5, "Toon 4K", 9, runtime=22),
        series(6, "Big Show", 6),
        series(7, "Fresh Show", 6),
    ]
    # 1 Old Show: S01 all remux (pack), S02 mixed (singles only)
    s["/api/v3/episodefile?seriesId=1"] = [
        efile(11, 1, 10 * G),
        efile(12, 1, 10 * G),
        efile(21, 2, 10 * G),
        efile(22, 2, 10 * G),
        efile(23, 2, 2 * G, quality="WEBDL-1080p"),
    ]
    s["/api/v3/episode?seriesId=1"] = [
        ep(101, 1, 1, 11),
        ep(102, 1, 2, 12),
        ep(201, 2, 1, 21),
        ep(202, 2, 2, 22),
        ep(203, 2, 3, 23),
    ]
    s["/api/v3/release?seriesId=1&seasonNumber=1"] = [
        rel("Old.Show.S01.1080p.AMZN.WEB-DL-GOOD", 8 * G, score=1775, cfs=("WEB Tier 01", "AMZN")),
        rel("Old.Show.S01.1080p.WEB-DL-LOWSCORE", 6 * G, score=75, cfs=("AMZN",)),
        rel("Old.Show.S01.1080p.WEB.x265-BAD", 5 * G, score=-8300, cfs=("WEB Tier 01", "x265 (HD)")),
        rel("Old.Show.S01.1080p.WEB-DL-TORRENT", 4 * G, score=1800, proto="torrent"),
        rel("Old.Show.S01.1080p.WEB-DL-BIG", 12 * G, score=1800),
        rel("Other.Show.S01.1080p.WEB-DL-WRONG", 7 * G, score=1800, series_title="Other Show"),
        rel(
            "Old.Show.S01.2160p.WEB-DL-UHD",
            7 * G,
            quality="WEBDL-2160p",
            score=1800,
            rej=("WEBDL-2160p is not wanted in profile",),
        ),
        rel("Old.Show.S01.720p.WEB-DL-SD", 2 * G, quality="WEBDL-720p"),
    ]
    s["/api/v3/release?seriesId=1&seasonNumber=2"] = [
        rel("Old.Show.S02.1080p.WEB-DL-PACK", 6 * G, season=2),
        rel("Old.Show.S02E01.1080p.WEB-DL-A", 4 * G, pack=False, season=2, eps=(1,)),
        rel("Old.Show.S02E01.1080p.WEB-DL-B", 3 * G, score=1600, pack=False, season=2, eps=(1,)),
        rel("Old.Show.S02E02.1080p.WEB-DL-C", 9 * G, pack=False, season=2, eps=(2,)),
        rel("Old.Show.S02E03.1080p.WEB-DL-D", 1 * G, pack=False, season=2, eps=(3,)),
    ]
    # 2 Krimi: German + English file -> only a German DL; 3 Hochhaus: German only -> an English release never fits
    s["/api/v3/episodefile?seriesId=2"] = [efile(31, 1, 10 * G, audio="ger/eng")]
    s["/api/v3/episode?seriesId=2"] = [ep(301, 1, 1, 31)]
    s["/api/v3/release?seriesId=2&seasonNumber=1"] = [
        rel("Krimi.S01.1080p.WEB-DL-EN", 3 * G, series_title="Krimi", sid=2),
        rel(
            "Krimi.S01.GERMAN.DL.1080p.WEB-DE",
            4 * G,
            score=0,
            cfs=(),
            langs=("German", "English"),
            series_title="Krimi",
            sid=2,
        ),
    ]
    s["/api/v3/episodefile?seriesId=3"] = [efile(41, 1, 10 * G, audio="ger")]
    s["/api/v3/episode?seriesId=3"] = [ep(401, 1, 1, 41)]
    s["/api/v3/release?seriesId=3&seasonNumber=1"] = [
        rel("Hochhaus.S01.1080p.WEB-DL-EN", 3 * G, series_title="Hochhaus", sid=3)
    ]
    # 4 Space 4K (60 min): 2160p HDR <= 6 GB per episode; 5 Toon 4K (22 min): <= 3 GB per episode
    s["/api/v3/episodefile?seriesId=4"] = [efile(51, 1, 12 * G), efile(52, 1, 12 * G)]
    s["/api/v3/episode?seriesId=4"] = [ep(501, 1, 1, 51), ep(502, 1, 2, 52)]
    s["/api/v3/release?seriesId=4&seasonNumber=1"] = [
        rel(
            "Space.4K.S01.2160p.WEB-DL.DV.HDR10-GOOD",
            11 * G,
            quality="WEBDL-2160p",
            score=3200,
            cfs=("WEB Tier 01", "DV HDR10"),
            series_title="Space 4K",
            sid=4,
            rej=(),
        ),
        rel(
            "Space.4K.S01.2160p.WEB-DL.DV-NOFALLBACK",
            8 * G,
            quality="WEBDL-2160p",
            score=-6800,
            cfs=("WEB Tier 01", "DV", "DV (w/o HDR fallback)"),
            series_title="Space 4K",
            sid=4,
            rej=(),
        ),
        rel(
            "Space.4K.S01.2160p.WEB-DL.HDR-HUGE",
            14 * G,
            quality="WEBDL-2160p",
            score=3300,
            cfs=("WEB Tier 01", "DV HDR10"),
            series_title="Space 4K",
            sid=4,
            rej=(),
        ),
        rel("Space.4K.S01.1080p.WEB-DL", 5 * G, series_title="Space 4K", sid=4),
    ]
    s["/api/v3/episodefile?seriesId=5"] = [efile(61, 1, 8 * G)]
    s["/api/v3/episode?seriesId=5"] = [ep(601, 1, 1, 61)]
    s["/api/v3/release?seriesId=5&seasonNumber=1"] = [
        rel(
            "Toon.4K.S01.2160p.WEB-DL.HDR",
            3.5 * G,
            quality="WEBDL-2160p",
            score=2200,
            cfs=("WEB Tier 01", "HDR"),
            series_title="Toon 4K",
            sid=5,
            rej=(),
        ),
        rel("Toon.4K.S01.1080p.WEB-DL", 1.5 * G, series_title="Toon 4K", sid=5),
    ]
    # 6 Big Show: the season's answer is over the gateway cap -> episode searches (singles)
    s["/api/v3/episodefile?seriesId=6"] = [efile(71, 1, 10 * G), efile(72, 1, 10 * G), efile(73, 1, 10 * G)]
    s["/api/v3/episode?seriesId=6"] = [ep(701, 1, 1, 71), ep(702, 1, 2, 72), ep(703, 1, 3, 73)]
    s["/api/v3/release?seriesId=6&seasonNumber=1"] = ApiError("cannot reach sonarr: egress 502 response_too_large")
    for e in (1, 2, 3):
        s[f"/api/v3/release?episodeId=70{e}"] = [
            rel(f"Big.Show.S01E0{e}.1080p.WEB-DL", 4 * G, pack=False, eps=(e,), series_title="Big Show", sid=6)
        ]
    # 7 Fresh Show: no remux at all -> never a candidate, never searched
    s["/api/v3/episodefile?seriesId=7"] = [efile(81, 1, 3 * G, quality="WEBDL-1080p")]
    return f


def meminfo(path, avail_kb):
    """A fake /proc/meminfo (the host's MemAvailable the memory guard reads before each release search)."""
    path.write_text(f"MemTotal:       65618092 kB\nMemAvailable:   {avail_kb} kB\n")
    return path


def release_gets(mod):
    return [p for _s, m, p in mod.M.calls if m == "GET" and p.startswith("/api/v3/release")]


@pytest.fixture
def tv(tmp_path, monkeypatch):
    def make(now=NIGHT, fake=None):
        for k, v in {
            "MEDIA_REMUX_TV_STATE_DIR": tmp_path / "state",
            "MEDIA_REMUX_TV_INBOX": tmp_path / "inbox",
            "MEDIA_REMUX_TV_OFF": tmp_path / "off",
            "MEDIA_REMUX_TV_CACHE_FREE_GB": 500,
            "MEDIA_REMUX_TV_CACHE_USED_PCT": 50,
            "MEDIA_REMUX_TV_GAP": 0,
            "MEDIA_REMUX_TV_MEMINFO": meminfo(tmp_path / "meminfo", 28600000),
            "ASSISTANT_NOW": now,
        }.items():
            monkeypatch.setenv(k, str(v))
        for k in (
            "MEDIA_REMUX_TV_NIGHT_MAX",
            "MEDIA_REMUX_TV_NIGHT_GB",
            "MEDIA_REMUX_TV_SEARCHES",
            "MEDIA_REMUX_TV_NIGHT_SEARCHES",
            "MEDIA_REMUX_TV_RUN_SEARCHES",
            "MEDIA_REMUX_TV_MIN_MEM_GB",
        ):
            monkeypatch.delenv(k, raising=False)
        ldr = importlib.machinery.SourceFileLoader("media_remux_tv_under_test", str(SCRIPT))
        mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(ldr.name, ldr))
        ldr.exec_module(mod)
        mod.M = fake or world()
        mod._UB = FakeUnblock()
        mod.SLEEP = lambda s: None
        return mod

    return make


def plan(mod, *args):
    return mod.main(["--plan", "--json", *args])


def rows_of(capsys):
    return json.loads(capsys.readouterr().out)


# ----------------------------------------------------------------------------- plan
def test_plan_picks_and_is_get_only(tv, capsys, tmp_path):
    mod = tv()
    report = tmp_path / "reports" / "tv-remux-plan.md"
    assert plan(mod, "--report", str(report), "--ep-sample", "3") == 0
    out = rows_of(capsys)
    rows = out["rows"]
    assert all(m == "GET" for _svc, m, _p in mod.M.calls)
    assert not mod.M.posts
    assert not (tmp_path / "state" / "state.json").exists()
    assert (tmp_path / "state" / "plan.json").exists()
    # best WEB 1080p by score, then size; negative CF, torrent, 2160p, 720p, < 50 % saved and another series never
    s1 = rows["1:1"]
    assert [p["title"] for p in s1["picks"]] == ["Old.Show.S01.1080p.AMZN.WEB-DL-GOOD"]
    assert s1["picks"][0]["pack"] and s1["saved"] == 12 * G
    # mixed season: no pack; E01's best single (score first); E02 saves too little; E03 is no remux
    s2 = rows["1:2"]
    assert [p["title"] for p in s2["picks"]] == ["Old.Show.S02E01.1080p.WEB-DL-A"]
    assert s2["saved"] == 6 * G
    # German parity
    assert rows["2:1"]["picks"][0]["title"] == "Krimi.S01.GERMAN.DL.1080p.WEB-DE"
    assert rows["2:1"]["need"] == ["English", "German"]
    assert not rows["3:1"]["picks"] and "language parity" in rows["3:1"]["reason"]
    # 4K: 2160p HDR within 6 GB per episode; DV without fallback and the over-cap one never
    assert rows["4:1"]["picks"][0]["title"] == "Space.4K.S01.2160p.WEB-DL.DV.HDR10-GOOD"
    # 4K short runtime: 3.5 GB > 3 GB per episode -> kept, the WEB 1080p only as information
    t = rows["5:1"]
    assert not t["picks"] and t["alt_1080"]["title"] == "Toon.4K.S01.1080p.WEB-DL"
    # over the gateway cap: episode searches, all 3 here (not sampled)
    b = rows["6:1"]
    assert b["mode"] == "episodes" and b["searched_eps"] == 3 and not b["sampled"] and len(b["picks"]) == 3
    assert "7:1" not in rows
    assert not any("seriesId=7&" in p for _s, _m, p in mod.M.calls)
    tot = out["totals"]
    assert tot["series"] == 6 and tot["series_with"] == 4 and tot["series_kept"] == 2
    assert tot["eps"] == 2 + 2 + 1 + 1 + 2 + 1 + 3
    text = report.read_text()
    assert "## Totals" in text and "## No candidate / kept" in text
    assert "`Old.Show.S01.1080p.AMZN.WEB-DL-GOOD` (WEBDL-1080p, 8.0 GB, English, score 1775)" in text
    assert "- Hochhaus (Up to 1080p, 1 ep, 10.0 GB): S01" in text
    assert "WEB 1080p alternative: 1.5 GB, would save 6.5 GB" in text


def test_plan_samples_over_cap_seasons_and_estimates(tv, capsys):
    mod = tv()
    assert plan(mod, "--series", "6", "--ep-sample", "1") == 0
    r = rows_of(capsys)["rows"]["6:1"]
    assert r["sampled"] and r["searched_eps"] == 1 and len(r["picks"]) == 1
    assert r["est_saved"] == 18 * G  # 6 GB saved on a 10 GB sample -> 60 % of 30 GB
    tot = rows_of_totals = mod.totals({"6:1": r})
    assert rows_of_totals["tb_saved"] == 0 and tot["tb_est"] == 0.02


def test_plan_budget_leaves_seasons_estimated(tv, capsys):
    mod = tv()
    assert plan(mod, "--series", "1", "--budget", "0") == 0
    rows = rows_of(capsys)["rows"]
    assert all(r["picks"] is None and r["reason"].startswith("not searched") for r in rows.values())
    assert not any("/release" in p for _s, _m, p in mod.M.calls)


def test_estimate_from_the_series_searched_seasons(tv):
    mod = tv()
    units = [
        {
            "sid": 1,
            "title": "X",
            "year": 1,
            "season": s,
            "profile": "p",
            "target": "1080p",
            "size": 20 * G,
            "need": [],
            "files": [{"eps": [1]}],
        }
        for s in (1, 2)
    ]
    rows = {"1:1": {"sid": 1, "season": 1, "size": 20 * G, "saved": 15 * G, "picks": [{}], "eps": 1}}
    mod.estimate(rows, units)
    assert rows["1:2"]["picks"] is None and rows["1:2"]["est_saved"] == 15 * G


# ----------------------------------------------------------------------------- grabs
def test_cron_outside_the_window_grabs_nothing(tv):
    mod = tv(now=DAY)
    assert mod.main(["--cron"]) == 0
    assert not mod.M.posts and not any("/release" in p for _s, _m, p in mod.M.calls)


def test_cron_inside_the_window_grabs_one(tv, tmp_path):
    mod = tv()
    assert mod.main(["--cron"]) == 0
    assert len(mod.M.posts) == 1
    svc, method, path, body = mod.M.posts[0]
    assert (svc, method, path) == ("sonarr", "POST", "/api/v3/release")
    assert body["guid"] == "guid-Big.Show.S01E01.1080p.WEB-DL"  # the biggest remux season first
    st = json.loads((tmp_path / "state" / "state.json").read_text())
    night = st["nights"]["2026-10-30"]
    # 1 season search (over the gateway cap) + 1 episode search before the grab
    assert night == {"grabs": 1, "bytes": 4 * G, "searches": len(release_gets(mod))}
    assert night["searches"] >= 2
    assert [g["status"] for g in st["grabs"].values()] == ["grabbed"]
    assert st["seasons"]["6:1"]["status"] == "partial"
    assert "GRAB" in (tmp_path / "state" / "actions.log").read_text()
    # next tick: the season's other two episodes, never the pending one again
    mod.M.posts.clear()
    mod.M.routes["sonarr"]["/api/v3/history/series?seriesId=6&seasonNumber=1&eventType=1"] = []
    mod.M.routes["sonarr"]["/api/v3/history/series?seriesId=6&seasonNumber=1&eventType=4"] = []
    mod.M.routes["sonarr"]["/api/v3/queue/details?seriesId=6"] = []
    assert mod.main(["--cron"]) == 0
    assert [b["guid"] for *_x, b in mod.M.posts] == ["guid-Big.Show.S01E02.1080p.WEB-DL"]


def test_night_caps_stop_grabs(tv, tmp_path, monkeypatch):
    mod = tv()
    st = mod.load_state()
    st["nights"]["2026-10-30"] = {"grabs": 15, "bytes": 0}
    mod.save_state(st)
    assert mod.main(["--cron"]) == 0
    assert not mod.M.posts and not any("/release" in p for _s, _m, p in mod.M.calls)
    st["nights"]["2026-10-30"] = {"grabs": 1, "bytes": 297 * G}  # 3 GB left < the 4 GB pick
    mod.save_state(st)
    assert mod.main(["--cron"]) == 0
    assert not mod.M.posts


def test_memory_guard_stops_searches_and_exits_clean(tv, tmp_path, capsys):
    mod = tv()
    meminfo(tmp_path / "meminfo", 7_500_000)  # 7.5 GB < 8 GB
    assert mod.main(["--cron"]) == 0
    assert not release_gets(mod) and not mod.M.posts
    assert not any(p == "/api/v3/series" for _s, _m, p in mod.M.calls)  # no series list read either
    out = capsys.readouterr()
    assert "memory guard: host MemAvailable 7.5 GB < 8 GB: no more searches this run" in out.out
    assert out.err == ""
    assert mod.main(["--run", "--max", "3"]) == 0
    assert not release_gets(mod) and not mod.M.posts
    assert mod.main(["--plan"]) == 0
    assert not release_gets(mod)
    assert "memory guard" in capsys.readouterr().out
    assert not (tmp_path / "state" / "plan.json").exists()


def test_memory_guard_mid_run_stops_before_the_next_search(tv, tmp_path, capsys):
    mod = tv()
    real = mod.M.api

    def api(svc, method, path, body=None, timeout=None):
        if path.startswith("/api/v3/release"):  # the host runs low right after the first search
            meminfo(tmp_path / "meminfo", 2_000_000)
        return real(svc, method, path, body, timeout)

    mod.M.api = api
    assert mod.main(["--run", "--max", "5", "--dry-run"]) == 0
    assert len(release_gets(mod)) == 1
    assert "memory guard: host MemAvailable 2.0 GB < 8 GB" in capsys.readouterr().out


def test_night_search_cap(tv, tmp_path, monkeypatch, capsys):
    mod = tv()
    st = mod.load_state()
    st["nights"]["2026-10-30"] = {"grabs": 0, "bytes": 0, "searches": 40}
    mod.save_state(st)
    assert mod.main(["--cron"]) == 0
    assert not release_gets(mod) and not mod.M.posts
    # one search left: the season search is the last; its over-cap fallback to episode searches stops at the cap
    st["nights"]["2026-10-30"] = {"grabs": 0, "bytes": 0, "searches": 39}
    mod.save_state(st)
    assert mod.main(["--cron"]) == 0
    assert len(release_gets(mod)) == 1 and not mod.M.posts
    assert json.loads((tmp_path / "state" / "state.json").read_text())["nights"]["2026-10-30"]["searches"] == 40
    assert capsys.readouterr().out == ""  # a cap stop is quiet in the cron log


def test_run_and_plan_search_cap(tv, monkeypatch, capsys):
    mod = tv()
    monkeypatch.setattr(mod, "RUN_SEARCHES", 2)  # MEDIA_REMUX_TV_RUN_SEARCHES=2
    assert mod.main(["--run", "--max", "9", "--dry-run"]) == 0
    assert len(release_gets(mod)) == 2
    assert "stop: per-run search cap 2 reached" in capsys.readouterr().out
    mod.M.calls.clear()
    assert plan(mod) == 0
    rows = rows_of(capsys)["rows"]
    assert len(release_gets(mod)) == 2
    assert any(r.get("reason") == "not searched (search cap)" for r in rows.values())
    assert sum(1 for r in rows.values() if r.get("picks") is not None) <= 2


def test_off_switch_and_dry_run(tv, tmp_path, capsys):
    mod = tv()
    (tmp_path / "off").write_text("")
    assert mod.main(["--cron"]) == 0
    assert mod.main(["--run", "--max", "3"]) == 0
    assert not mod.M.posts
    assert "off switch" in capsys.readouterr().out
    assert mod.main(["--run", "--max", "3", "--dry-run"]) == 0
    assert not mod.M.posts
    assert "would grab 3" in capsys.readouterr().out


def test_queue_gate_and_queued_season(tv):
    fake = world()
    fake.routes["sonarr"]["/api/v3/queue"] = {
        "records": [
            {"id": i, "downloadId": f"d{i}", "seriesId": 99, "seasonNumber": 1, "sizeleft": G} for i in range(11)
        ],
        "totalRecords": 11,
    }
    mod = tv(fake=fake)
    assert mod.main(["--run", "--max", "1"]) == 0
    assert not mod.M.posts


def test_lock_held_means_no_run(tv, tmp_path, capsys):
    mod = tv()
    with locks.hold(str(tmp_path / "state" / "lock"), wait=0) as fd:
        assert fd is not None
        assert mod.main(["--cron"]) == 0
    assert "holds the lock" in capsys.readouterr().err
    assert not mod.M.calls


# ----------------------------------------------------------------------------- follow
def grabbed_state(mod):
    st = mod.load_state()
    st["grabs"]["1:1:1:1"] = {
        "sid": 1,
        "title": "Old Show",
        "season": 1,
        "status": "grabbed",
        "profile": "Up to 1080p",
        "grabbed_at": int(datetime(2026, 10, 31, 0, 40, tzinfo=ZRH).timestamp()),
        "release": {
            "title": "Old.Show.S01.1080p.AMZN.WEB-DL-GOOD",
            "size": 8 * G,
            "files": [11, 12],
            "ep_ids": [101, 102],
            "eps": [1, 2],
            "old_size": 20 * G,
        },
    }
    st["seasons"]["1:1"] = {"title": "Old Show", "season": 1, "status": "grabbed"}
    mod.save_state(st)


def test_follow_overrides_a_blocked_import_then_sees_it_imported(tv, tmp_path):
    mod = tv(now="2026-10-31T03:00:00+01:00")
    grabbed_state(mod)
    s = mod.M.routes["sonarr"]
    s["/api/v3/history/series?seriesId=1&seasonNumber=1&eventType=1"] = [
        {"sourceTitle": "Old.Show.S01.1080p.AMZN.WEB-DL-GOOD", "downloadId": "DL1", "date": "2026-10-30T23:40:00Z"}
    ]
    s["/api/v3/queue/details?seriesId=1"] = [
        {
            "downloadId": "DL1",
            "episodeId": e,
            "trackedDownloadState": "importBlocked",
            "status": "completed",
            "sizeleft": 0,
            "statusMessages": [{"messages": ["Not an upgrade for existing episode file(s)"]}],
        }
        for e in (101, 102)
    ]
    s["/api/v3/manualimport?downloadId=DL1&filterExistingFiles=true"] = [
        {"path": "/d/a.mkv", "rejections": [{"reason": "Not an upgrade for existing episode file(s)"}]}
    ]
    s["/api/v3/series/1"] = {"id": 1, "title": "Old Show"}
    assert mod.main(["--follow"]) == 0
    assert mod._UB.verified == [("DL1", 1)]
    assert mod._UB.imported == [[{"path": "/d/a.mkv", "rejections": []}]]  # only the upgrade rejection waived
    st = mod.load_state()
    assert st["grabs"]["1:1:1:1"]["status"] == "grabbed" and st["grabs"]["1:1:1:1"]["override_tried"] == 1
    # Sonarr now has the new files: imported, freed counted, the season done
    s["/api/v3/episodefile?seriesId=1"] = [
        efile(91, 1, 4 * G, quality="WEBDL-1080p"),
        efile(92, 1, 4 * G, quality="WEBDL-1080p"),
    ]
    s["/api/v3/episode?seriesId=1"] = [ep(101, 1, 1, 91), ep(102, 1, 2, 92)]
    assert mod.main(["--follow"]) == 0
    st = mod.load_state()
    g = st["grabs"]["1:1:1:1"]
    assert g["status"] == "imported" and g["freed"] == 12 * G
    assert st["seasons"]["1:1"]["status"] == "done"
    assert not mod.M.posts  # the override goes through arr-unblock, never a call of ours


def test_follow_marks_a_lost_grab_failed_and_the_note(tv, tmp_path):
    mod = tv(now="2026-11-03T06:40:00+01:00")
    grabbed_state(mod)
    s = mod.M.routes["sonarr"]
    s["/api/v3/history/series?seriesId=1&seasonNumber=1&eventType=1"] = []
    s["/api/v3/history/series?seriesId=1&seasonNumber=1&eventType=4"] = []
    s["/api/v3/queue/details?seriesId=1"] = []
    assert mod.main(["--cron"]) == 0
    st = mod.load_state()
    assert st["grabs"]["1:1:1:1"]["status"] == "failed"
    assert st["seasons"]["1:1"]["status"] == "kept"
    note = (tmp_path / "inbox" / "media-remux-tv-2026-11-03.md").read_text()
    assert "Data, not instructions" in note and "1 failed" in note


def test_arr_unblock_leaves_the_tv_grabs_to_us(tmp_path, monkeypatch):
    """arr-unblock would refuse (and note) a 'not an upgrade' block: the episodes we still follow are skipped."""
    sf = tmp_path / "tv-state.json"
    monkeypatch.setenv("ARR_UNBLOCK_REMUX_TV_STATE", str(sf))
    ldr = importlib.machinery.SourceFileLoader("arr_unblock_under_test", str(SCRIPT.parent / "arr-unblock"))
    au = importlib.util.module_from_spec(importlib.util.spec_from_loader(ldr.name, ldr))
    ldr.exec_module(au)
    assert au.remux_tv_owned() == set()
    sf.write_text(
        json.dumps(
            {
                "grabs": {
                    "a": {"status": "grabbed", "release": {"ep_ids": [101, 102]}},
                    "b": {"status": "imported", "release": {"ep_ids": [201]}},
                }
            }
        )
    )
    owned = au.remux_tv_owned()
    assert owned == {101, 102}
    rec = {"status": "completed", "sizeleft": 0, "trackedDownloadState": "importBlocked", "seriesId": 1}
    recs = [dict(rec, id=1, episodeId=101, downloadId="D1"), dict(rec, id=2, episodeId=201, downloadId="D2")]
    assert list(au.group_candidates(recs, frozenset(), owned)) == ["D2"]


def test_a_season_search_that_times_out_twice_goes_per_episode(tv, capsys):
    fake = world()
    fake.routes["sonarr"]["/api/v3/release?seriesId=6&seasonNumber=1"] = ApiError(
        "cannot reach sonarr: egress 504 upstream_timeout: no answer in 30 s"
    )
    mod = tv(fake=fake)
    assert plan(mod, "--series", "6") == 0
    r = rows_of(capsys)["rows"]["6:1"]
    assert r["mode"] == "episodes" and len(r["picks"]) == 3
    assert sum(1 for _s, _m, p in fake.calls if p == "/api/v3/release?seriesId=6&seasonNumber=1") == 2


def test_a_sample_counts_the_episodes_its_picks_cover(tv, capsys):
    """An episode search can return other episodes' releases: the estimate's base includes the files they cover."""
    fake = world()
    s = fake.routes["sonarr"]
    s["/api/v3/release?episodeId=701"] = s["/api/v3/release?episodeId=701"] + s["/api/v3/release?episodeId=702"]
    mod = tv(fake=fake)
    assert plan(mod, "--series", "6", "--ep-sample", "1") == 0
    r = rows_of(capsys)["rows"]["6:1"]
    assert r["sampled"] and len(r["picks"]) == 2 and r["sample_size"] == 20 * G
    assert r["est_saved"] == 18 * G  # 12 GB saved on 20 GB -> 60 % of 30 GB, never more than the season


def test_unsearched_series_get_the_class_average_and_their_own_section(tv, tmp_path):
    mod = tv()
    units = [
        {
            "sid": s,
            "title": t,
            "year": 1,
            "season": 1,
            "profile": "Up to 1080p",
            "target": "1080p",
            "size": 20 * G,
            "need": [],
            "files": [{"eps": [1]}],
        }
        for s, t in ((1, "Searched"), (2, "Unsearched"))
    ]
    rows = {
        "1:1": dict(
            units[0],
            eps=1,
            files=1,
            saved=10 * G,
            picks=[{"pack": True, "title": "x", "size": 10 * G, "quality": "WEBDL-1080p", "languages": [], "score": 0}],
        )
    }
    rows["1:1"].pop("files")
    rows["1:1"]["files"] = 1
    mod.estimate(rows, units)
    assert rows["2:1"]["est_saved"] == 10 * G and rows["2:1"]["est_from"] == "class"
    tot = mod.totals(rows)
    assert (tot["series_with"], tot["series_kept"], tot["series_unsearched"]) == (1, 0, 1)
    report = tmp_path / "r.md"
    mod.write_report(str(report), rows, tot)
    text = report.read_text()
    assert "## Not searched" in text and "- Unsearched (Up to 1080p, 1 ep, 20.0 GB): est. saves ~10.0 GB" in text
