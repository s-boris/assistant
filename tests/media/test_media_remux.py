"""media-remux's search budget: the memory guard before each Radarr release search (host MemAvailable from a fake
/proc/meminfo), the night's search cap in --cron and the per-run cap in --run / --plan. Offline: the legacy suite's
fake ~/bin/media (routes.json in FAKE_DIR, GETs logged to gets.log); no release is ever picked here, so arr-mismatch's
title check is never loaded. Picks, gates and follow stay in tests/legacy/media-remux/run.sh."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "media-remux"
FAKE_MEDIA = REPO / "tests" / "legacy" / "media-remux" / "fake_media.py"
G = 1e9
NIGHT = 1793232000  # 2026-10-29 00:00 UTC (the fake media module has no zone: UTC)
IN_WINDOW = NIGHT + 2400  # 00:40


def movie(mid, size):
    return {
        "id": mid,
        "title": f"Film {mid}",
        "year": 2001,
        "qualityProfileId": 1,
        "hasFile": True,
        "movieFile": {"id": 100 + mid, "size": size, "quality": {"quality": {"name": "Remux-2160p"}}},
    }


def meminfo(path, avail_kb):
    path.write_text(f"MemTotal:       65618092 kB\nMemAvailable:   {avail_kb} kB\n")
    return path


@pytest.fixture
def remux(tmp_path, monkeypatch):
    fake = tmp_path / "fake"
    fake.mkdir()
    movies = [movie(i, (60 - i) * G) for i in range(1, 13)]
    routes = {
        "/api/v3/qualityprofile/1": {"formatItems": []},
        "/api/v3/movie": movies,
        "/api/v3/queue": {"records": [], "totalRecords": 0},
    }
    for m in movies:
        routes[f"/api/v3/movie/{m['id']}"] = m
        routes[f"/api/v3/moviefile/{m['movieFile']['id']}"] = {"id": m["movieFile"]["id"], "customFormatScore": 0}
        routes[f"/api/v3/release?movieId={m['id']}"] = []  # nothing fits: every search is a no-release row
    (fake / "routes.json").write_text(json.dumps(routes))

    def make(now=IN_WINDOW, avail_kb=28600000, **env):
        base = {
            "FAKE_DIR": fake,
            "MEDIA_REMUX_MEDIA": FAKE_MEDIA,
            "MEDIA_REMUX_MISMATCH": tmp_path / "no-arr-mismatch",
            "MEDIA_REMUX_STATE_DIR": tmp_path / "state",
            "MEDIA_REMUX_INBOX": tmp_path / "inbox",
            "MEDIA_REMUX_OFF": tmp_path / "off",
            "MEDIA_REMUX_CACHE_FREE_GB": 500,
            "MEDIA_REMUX_CACHE_USED_PCT": 50,
            "MEDIA_REMUX_MEMINFO": meminfo(tmp_path / "meminfo", avail_kb),
            "MEDIA_REMUX_NOW": now,
        }
        base.update(env)
        for k in ("MEDIA_REMUX_NIGHT_SEARCHES", "MEDIA_REMUX_RUN_SEARCHES", "MEDIA_REMUX_MIN_MEM_GB",
                  "MEDIA_REMUX_SEARCHES"):  # fmt: skip
            monkeypatch.delenv(k, raising=False)
        for k, v in base.items():
            monkeypatch.setenv(k, str(v))
        ldr = importlib.machinery.SourceFileLoader("media_remux_under_test", str(SCRIPT))
        mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(ldr.name, ldr))
        ldr.exec_module(mod)
        return mod

    def run(mod, *argv):
        monkeypatch.setattr(sys, "argv", ["media-remux", *argv])
        return mod.main()

    def gets():
        log = fake / "gets.log"
        return log.read_text().splitlines() if log.exists() else []

    def state():
        return json.loads((tmp_path / "state" / "state.json").read_text())

    return make, run, gets, state


def searches(paths):
    return [p for p in paths if p.startswith("/api/v3/release")]


def test_cron_searches_and_counts_the_night(remux):
    make, run, gets, state = remux
    mod = make()
    assert run(mod, "--cron") == 0
    assert len(searches(gets())) == 4  # MEDIA_REMUX_SEARCHES per tick, unchanged
    assert state()["nights"]["2026-10-28"] == {"grabs": 0, "searches": 4}


def test_memory_guard_stops_cron_before_any_read(remux, capsys):
    make, run, gets, state = remux
    mod = make(avail_kb=7_900_000)
    assert run(mod, "--cron") == 0
    assert gets() == []  # no search, no movie list, no profile: nothing but the (empty) follow
    out = capsys.readouterr()
    assert out.out.strip() == "memory guard: host MemAvailable 7.9 GB < 8 GB: no more searches this run"
    assert out.err == ""
    assert not (Path(mod.INBOX)).exists() or not any(Path(mod.INBOX).iterdir())


def test_memory_guard_threshold_is_configurable(remux):
    make, run, gets, _state = remux
    mod = make(avail_kb=7_900_000, MEDIA_REMUX_MIN_MEM_GB=6)
    assert run(mod, "--cron") == 0
    assert len(searches(gets())) == 4
    mod = make(avail_kb=1_000_000, MEDIA_REMUX_MIN_MEM_GB=0)  # 0 = off
    assert run(mod, "--cron") == 0
    assert len(searches(gets())) == 8


def test_memory_guard_mid_run(remux, tmp_path, capsys):
    make, run, gets, state = remux
    mod = make()
    real = mod.M.get

    def get(svc, path, timeout=None):
        r = real(svc, path, timeout=timeout)
        if path.startswith("/api/v3/release"):  # the host runs low right after the first search
            meminfo(tmp_path / "meminfo", 3_000_000)
        return r

    mod.M.get = get
    assert run(mod, "--run", "--max", "5") == 0
    assert len(searches(gets())) == 1
    assert "memory guard: host MemAvailable 3.0 GB < 8 GB: no more searches this run" in capsys.readouterr().out
    assert [s["status"] for s in state()["movies"].values()] == ["no-release"]


def test_night_search_cap(remux, capsys):
    make, run, gets, state = remux
    mod = make(MEDIA_REMUX_NIGHT_SEARCHES=6)
    assert run(mod, "--cron") == 0
    assert len(searches(gets())) == 4
    capsys.readouterr()
    assert run(mod, "--cron") == 0  # 2 left of the night's 6 (the per-tick 4 would allow more)
    assert len(searches(gets())) == 6
    assert state()["nights"]["2026-10-28"]["searches"] == 6
    assert "stop: night search cap 6 reached" in capsys.readouterr().out
    assert run(mod, "--cron") == 0  # used up: no search, and no reads for one
    assert len(searches(gets())) == 6


def test_run_and_plan_search_cap(remux, capsys):
    make, run, gets, state = remux
    mod = make(now=NIGHT + 12 * 3600, MEDIA_REMUX_RUN_SEARCHES=2)
    assert run(mod, "--run", "--max", "5", "--dry-run") == 0
    assert len(searches(gets())) == 2
    assert "stop: per-run search cap 2 reached" in capsys.readouterr().out
    assert run(mod, "--plan", "--json") == 0
    tot = json.loads(capsys.readouterr().out)["totals"]
    assert len(searches(gets())) == 4
    assert tot["searched"] == 2 and tot["candidates"] == 12
    assert tot["stopped"].startswith("stop: per-run search cap 2 reached (searched 2 of 12 candidates")


def test_plan_memory_guard(remux, capsys):
    make, run, gets, _state = remux
    mod = make(avail_kb=5_000_000)
    assert run(mod, "--plan") == 0
    assert gets() == []
    assert "memory guard: host MemAvailable 5.0 GB < 8 GB" in capsys.readouterr().out
    assert not Path(mod.PLAN_F).exists()
