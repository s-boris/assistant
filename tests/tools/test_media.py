"""Media tools against a stub `media`: argv per tool, parsing and text, the request results, typed errors, and the
boundaries (no delete/blocklist tools, doctor always a dry run, values never read as options)."""

from __future__ import annotations

import pytest
from tools import gateway
from tools.core import load

DUNE = {"title": "Dune", "year": "2021", "type": "movie", "tmdb": 438631, "status": "available"}


def call(name, **args):
    return gateway.call(name, args)


def test_search_status_queue_recent(stubs):
    stubs.on("media", ["search"], [DUNE, dict(DUNE, title="Dune: Part Two", tmdb=693134, status="not requested")])
    status_row = dict(
        DUNE,
        type="tv",
        tmdb=1399,
        status="requested (processing)",
        requests=[{"id": 7, "status": "approved", "seasons": [2], "by": "Friend A", "created": "2026-10-01"}],
        seasons={"1": "available", "2": "requested"},
        downloading=[
            {
                "series": "Show",
                "season": 2,
                "episodes": [1, 2, 3],
                "progress": "40%",
                "status": "downloading",
                "eta": "1h00m (~12:00)",
                "issue": None,
            }
        ],
    )
    stubs.on("media", ["status"], [status_row])
    stubs.on(
        "media",
        ["queue"],
        {
            "movies": [
                {
                    "title": "Film",
                    "year": 2020,
                    "progress": "100%",
                    "status": "completed",
                    "eta": "done, importing",
                    "issue": "importPending: not found",
                }
            ],
            "tv": [],
        },
    )
    stubs.on(
        "media",
        ["recent"],
        [
            {"type": "tv", "title": "Show", "season": 2, "episodes": [5, 6], "at": "2026-10-22T02:33+02:00"},
            {"type": "movie", "title": "Film", "year": 2026, "at": "2026-10-22T02:17+02:00"},
        ],
    )

    s = call("media.search", query="dune", type="movie", n=2)
    assert s.ok and s.data["count"] == 2 and s.data["results"][0]["tmdb"] == 438631
    assert s.text.splitlines()[0] == "Dune (2021) [movie] tmdb:438631 — available"
    st = call("media.status", title="Severance")
    assert st.ok and st.data["titles"][0]["requests"][0]["by"] == "Friend A"
    assert "request #7: approved S2 by Friend A" in st.text and "downloading: Show S02E01-E03 (3 eps) — 40%" in st.text
    assert call("media.status", tmdb=1399, type="tv").ok
    q = call("media.queue")
    assert q.data["movies"][0]["issue"].startswith("importPending") and q.text.startswith("Movies (1):")
    r = call("media.recent", n=2)
    assert r.text.splitlines()[0] == "2026-10-22T02:33+02:00  tv     Show S02 E05,E06"
    calls = stubs.calls("media")
    assert ["search", "--json", "-n", "2", "--type", "movie", "dune"] in calls
    assert ["status", "--json", "-n", "1", "Severance"] in calls
    assert ["status", "--json", "-n", "1", "--tmdb", "1399", "--type", "tv"] in calls
    assert ["queue", "--json"] in calls and ["recent", "--json", "-n", "2"] in calls


def test_extras(stubs):
    stubs.on(
        "media",
        ["airing"],
        {
            "start": "2026-10-22T00:00:00+02:00",
            "end": "2026-10-23T00:00:00+02:00",
            "episodes": [
                {"series": "S", "season": 1, "episode": 2, "air_local": "2026-10-22T21:00:00+02:00", "has_file": True}
            ],
            "movies": [],
            "warnings": [],
        },
    )
    stubs.on(
        "media",
        ["missing"],
        {
            "episodes_total": 9,
            "shows": [{"series": "A", "episodes": 5, "seasons": [1]}, {"series": "B", "episodes": 4, "seasons": [2]}],
            "movies_released": [],
            "movies_not_out": [{"title": "X"}],
            "warnings": [],
        },
    )
    stubs.on(
        "media",
        ["health"],
        {
            "ok": False,
            "problems": [{"service": "Sonarr", "type": "warning", "message": "Indexers unavailable"}],
            "notes": [],
            "checked": [],
        },
        rc=2,
    )
    stubs.on("media", ["trending"], [dict(DUNE, rating=8.1)])
    stubs.on("media", ["similar"], {"like": DUNE, "results": [dict(DUNE, title="Arrival", tmdb=329865)]})
    stubs.on(
        "media",
        ["vibe"],
        {
            "label": "cozy",
            "kind": "movie",
            "picks": [{"title": "Klaus", "year": 2019}],
            "text": "🎬 Vibe · cozy",
            "message": "**m**",
            "buttons": [[{"text": "x", "data": "y"}]],
        },
    )
    stubs.on(
        "media",
        ["continue"],
        {"items": [{"title": "Show", "episode": "S01E02", "state": "next"}], "total": 3, "message": "m", "buttons": []},
    )
    stubs.on(
        "media",
        ["doctor"],
        {"items": [{"title": "Film", "verdict": "in cinemas"}], "dry_run": True, "text": "🩺 Film", "message": "m"},
    )

    a = call("media.airing", days=1)
    assert a.ok and "S01E02 ✓" in a.text
    m = call("media.missing", n=1)
    assert m.data["totals"] == {"shows": 2, "movies_released": 0, "movies_not_out": 1} and len(m.data["shows"]) == 1
    h = call("media.health")
    assert h.ok and h.data["ok"] is False and h.text == "Sonarr warning: Indexers unavailable"  # exit 2 = problems
    assert call("media.trending", type="tv", new=True, n=3).data["results"][0]["rating"] == 8.1
    si = call("media.similar", title="Dune")
    assert si.data["like"]["tmdb"] == 438631 and si.text.startswith("like Dune (2021)")
    v = call("media.vibe", text="cozy rainy sunday", n=2)
    assert v.text == "🎬 Vibe · cozy" and "buttons" not in v.data  # concise drops the Telegram payload
    assert "buttons" in call("media.vibe", text="cozy", format="detailed").data
    assert call("media.continue").text == "Show S01E02 (next)"
    assert call("media.doctor", title="Film").text == "🩺 Film"
    call("media.doctor", mine=True, days=30, probe=True)
    calls = stubs.calls("media")
    assert ["airing", "--json", "--days", "1"] in calls
    assert ["trending", "--json", "-n", "3", "--type", "tv", "--new"] in calls
    assert ["vibe", "--json", "-n", "2", "cozy rainy sunday"] in calls
    assert ["doctor", "--json", "--dry-run", "-n", "15", "--no-probe", "Film"] in calls
    assert ["doctor", "--json", "--dry-run", "-n", "15", "--all", "--days", "30", "--mine"] in calls
    assert all("--dry-run" in c for c in calls if c[0] == "doctor")


def test_request(stubs):
    stubs.on(
        "media",
        ["request", "438631"],
        {
            "title": "Dune (2021)",
            "type": "movie",
            "tmdb": 438631,
            "result": "not_requested",
            "reason": "already available",
        },
    )
    stubs.on(
        "media",
        ["request", "1399"],
        {
            "title": "Show (2011)",
            "type": "tv",
            "tmdb": 1399,
            "result": "requested",
            "request_id": 12,
            "request_status": "approved",
            "seasons": [3],
            "skipped": {"1": "available"},
        },
    )
    stubs.on("media", ["request", "5"], "", rc=1, err="media: Film (2020) is on Seerr's blocklist; not requesting.\n")
    stubs.on("media", ["request", "6"], "", rc=1, err="media: seerr HTTP 503 on POST /api/v1/request\n")

    r = call("media.request", tmdb=438631, type="movie")
    assert r.ok and r.data["result"] == "not_requested" and r.text == "Not requested: Dune (2021) — already available"
    t = call("media.request", tmdb=1399, type="tv", seasons=[3])
    assert t.ok and t.text == "Requested Show (2011) seasons 3 (request #12, approved)"
    assert call("media.request", tmdb=5, type="movie").error["code"] == "conflict"
    assert call("media.request", tmdb=6, type="movie").error["code"] == "unavailable"
    assert call("media.request", tmdb=1, type="movie", seasons=[1]).error["code"] == "invalid_args"
    assert ["request", "1399", "--type", "tv", "--json", "--seasons", "3"] in stubs.calls("media")
    reg = load()
    assert reg.get("media.request").effect == "home"
    assert {s.effect for s in reg.in_domain("media")} == {"read", "home"}


def test_boundaries(stubs):
    reg = load()
    names = {s.name for s in reg.in_domain("media")}
    assert not {n for n in names if any(w in n for w in ("delete", "blocklist", "unwatched", "hogs", "play", "remove"))}
    for bad in ({"query": "-x"}, {"query": "--type=tv"}):
        assert call("media.search", **bad).error["code"] == "invalid_args"
    assert call("media.status").error["code"] == "invalid_args"  # title or tmdb
    assert call("media.status", tmdb=1).error["code"] == "invalid_args"  # tmdb needs type
    assert call("media.vibe", text="--refresh").error["code"] == "invalid_args"
    assert stubs.calls("media") == []


@pytest.mark.parametrize(
    ("rc", "err", "code"),
    [
        (2, "media: --days must be between 1 and 60\n", "invalid_args"),
        (1, "media: radarr timed out\n", "unavailable"),
        (1, "media: something odd\n", "failed"),
    ],
)
def test_errors(stubs, rc, err, code):
    stubs.on("media", ["queue"], "", rc=rc, err=err)
    assert call("media.queue").error["code"] == code
