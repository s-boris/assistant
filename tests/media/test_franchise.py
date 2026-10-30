"""`media franchise` (missing parts of film series he owns, a request poll, ignore list). fake_franchise.py stands in
for Radarr (/api/v3/collection, /api/v3/movie) and Seerr (collections, movie details, POST /api/v1/request) with
fictional series; MEDIA_FRANCHISE_STATE points at a temp dir. Checks: gap list and counts, skip reasons (requested,
wanted, unmonitored, excluded, blocklisted, unreleased by Radarr and by Seerr's date, Emby-only copies), Seerr-only
parts, --min-owned, ranking, JSON, the poll (<= 10 options, <= 3 a series, prefix trimming, 100-char cap, saved for
--picked), ignore/unignore/ignored, request (dry run, --picked, already requested, blocklisted, partial), Seerr/Radarr
down, argument errors, GET-only list, no key leaks."""

from __future__ import annotations

import collections
import json

import pytest

KEYS = ("SEERR-KEY-Zq81", "RADARR-KEY-Xw42")


@pytest.fixture
def fr(media):
    t = media.t
    (t / "scenario").write_text("ok\n")
    b = media.serve("fake_franchise.py", t / "scenario")
    (t / "cfg.json").write_text(
        json.dumps({"seerr": {"url": f"{b}/seerr", "key": KEYS[0]}, "radarr": {"url": f"{b}/radarr", "key": KEYS[1]}})
    )
    media.env.update(
        MEDIA_SECRETS=str(t / "cfg.json"),
        MEDIA_NOW="2026-10-10T12:00:00+02:00",
        MEDIA_TIMEOUT="5",
        MEDIA_FRANCHISE_STATE=str(t / "state"),
    )
    yield media
    media.safety(keys=KEYS, get_only=False)  # requests POST: GET-only is checked on the list runs


def posts(m, prefix="POST"):
    return sum(x.startswith(prefix) for x in m.log().splitlines())


def test_list_and_json(fr):
    m = fr
    out = m.m("franchise")
    assert m.rc == 0
    assert m.line(1) == "Film series with missing parts (owned ≥ 2): 6 series, 15 titles"
    assert m.line(2) == "Starfall (2 owned, 2 missing):"
    assert m.line(3) == "  Starfall III (2008) ★7.2 (4000 votes)  tmdb:103"
    assert "Starfall: Echoes (2011)" in out  # Seerr-only part
    for skipped in (
        "Origins",  # unreleased (Radarr)
        "Tin Lantern 6",  # unreleased (Seerr date)
        "Copper Kettle",  # requested
        "Night Ferry",  # wanted + unmonitored
        "Harbor Lights",  # Emby-only copy = owned, excluded
        "Iron Choir",  # blocklisted
        "Lone Signal",  # one owned part < --min-owned
        "Complete Tide",  # complete series
    ):
        assert skipped not in out
    assert out.split("\n")[-1] == (
        "Skipped: 2 not released yet; 1 on Seerr's blocklist; 1 on Radarr's exclusion list; 1 requested in Seerr;"
        " 1 unmonitored in Radarr; 1 in Radarr (wanted or downloading)"
    )
    assert "Glass Orchard (3 owned, 2 missing):" in out
    assert "Saga of Ash (2 owned, 5 missing):" in out
    assert "… +4 more" in m.m("franchise", "-n", "2")
    assert "Lone Signal Returns (2018)" in m.m("franchise", "--min-owned", "1")
    m.m("franchise", "--min-owned", "0")
    assert m.rc == 1

    m.m("franchise", "--json")
    d = m.j
    assert m.rc == 0
    assert (d["series"], d["titles"], d["ignored_series"]) == (6, 15, 0)
    assert all(a["score"] >= b["score"] for a, b in zip(d["gaps"], d["gaps"][1:], strict=False))
    keys = ("collection", "collection_id", "owned", "missing", "title", "year", "tmdb", "rating", "votes")
    assert [d["gaps"][0][k] for k in keys] == ["Starfall", 9001, 2, 2, "Starfall III", 2008, 103, 7.2, 4000]
    assert sorted(d["skipped"].items()) == [
        ("blocklisted", 1),
        ("excluded", 1),
        ("requested", 1),
        ("unmonitored", 1),
        ("unreleased", 2),
        ("wanted", 1),
    ]
    m.reset_log()
    m.m("franchise")
    assert [x for x in m.log().splitlines() if not x.startswith("GET ")] == []  # the list is GET-only
    assert m.logn("GET /seerr/api/v1/collection/") == 9  # only series with candidates


def test_poll_ignore_request(fr):
    m, t = fr, fr.t
    m.m("franchise", "--poll-json")
    d = m.j
    assert m.rc == 0
    assert (d["poll"]["multiple"], d["poll"]["anonymous"], len(d["poll"]["options"])) == (True, False, 10)
    assert max(collections.Counter(p["collection_id"] for p in d["picks"]).values()) == 3
    assert d["ids"] == [p["tmdb"] for p in d["picks"]] and len(d["ids"]) == 10
    assert d["poll"]["options"][:2] == ["Starfall III (2008)", "Starfall: Echoes (2011)"]
    assert "Saga of Ash: Part 3 (2002)" in d["poll"]["options"]
    assert max(len(o) for o in d["poll"]["options"]) <= 100
    assert "6 film series you own have gaps; the best 10 missing parts are in the poll." in d["message"]
    saved = json.loads((t / "state" / "last-poll.json").read_text())
    assert (len(saved["options"]), len(saved["ids"])) == (10, 10)
    assert saved["options"] == d["poll"]["options"]
    assert oct((t / "state" / "last-poll.json").stat().st_mode & 0o777) == "0o600"
    poll = d

    # ignore
    assert "Ignoring Paper Moons Collection (9006)" in m.m("franchise", "ignore", "Paper Moons")
    out = m.m("franchise")
    assert "Paper Moons" not in out
    assert "; 1 ignored series" in out.split("\n")[-1]
    assert "Paper Moons Collection (9006), since 2026-10-10" in m.m("franchise", "ignored")
    assert "Ignoring Starfall Collection (9001)" in m.m("franchise", "ignore", "9001")
    out = m.m("franchise", "ignore", "Collection")
    assert m.rc == 1 and "matches 12 collections" in out
    m.m("franchise", "ignore", "Nope Saga")
    assert m.rc == 1
    assert "No longer ignoring Starfall Collection (9001)" in m.m("franchise", "unignore", "9001")
    assert "No longer ignoring Paper Moons Collection (9006)" in m.m("franchise", "unignore", "paper")
    assert m.m("franchise", "ignored") == "No ignored collections."

    # request
    m.reset_log()
    out = m.m("franchise", "request", "--dry-run", "103", "405")
    assert m.rc == 0
    assert (
        'DRY RUN, would POST /api/v1/request {"mediaType": "movie", "mediaId": 103, "is4k": false}'
        "  for Starfall III (2008)" in out
    )
    assert 'mediaId": 405' in out
    assert posts(m) == 0
    opt1, opt3 = poll["poll"]["options"][0], poll["poll"]["options"][2]
    out = m.m("franchise", "request", "--dry-run", "--picked", f"{opt1} | {opt3}", "103")
    assert sum("DRY RUN" in x for x in out.split("\n")) == 2  # picked + a duplicate id
    out = m.m("franchise", "request", "--dry-run", "--picked", "Nope: X (1999)")
    assert m.rc == 1 and "not in the last franchise poll: 'Nope: X (1999)'" in out
    assert "Not requested: Copper Kettle 3 (2005) is already requested (pending)." in m.m(
        "franchise", "request", "--dry-run", "203"
    )
    out = m.m("franchise", "request", "--dry-run", "803")
    assert m.rc == 1 and "on Seerr's blocklist" in out
    m.m("franchise", "request", "abc")
    assert m.rc == 1
    m.m("franchise", "request")
    assert m.rc == 1
    assert posts(m) == 0
    out = m.m("franchise", "request", "103", "803")
    assert m.rc == 3
    assert "Requested Starfall III (2008) (request #77, approved), Radarr will grab it" in out
    assert posts(m, "POST /seerr/api/v1/request") == 1
    assert [x for x in m.log().splitlines() if x.startswith("BODY")] == [
        'BODY {"is4k": false, "mediaId": 103, "mediaType": "movie"}'
    ]


def test_errors(fr):
    m, t = fr, fr.t
    for args, rc in (
        (("--json", "request", "103"), 1),
        (("--dry-run",), 1),
        (("list", "5"), 1),
        (("--bogus",), 2),
    ):
        m.m("franchise", *args)
        assert m.rc == rc, args
    (t / "scenario").write_text("seerr-down\n")
    m.reset_log()
    out = m.m("franchise")
    assert m.rc == 3
    assert "!! skipped Seerr: seerr HTTP 500" in out
    assert "Starfall III (2008)" in out  # Radarr-only gaps still listed
    assert m.logn("GET /seerr/") == 1  # one Seerr try only
    m.m("franchise", "--poll-json")
    assert m.rc == 3 and "Seerr: seerr HTTP 500" in str(m.j["warnings"])
    (t / "scenario").write_text("radarr-down\n")
    out = m.m("franchise")
    assert m.rc == 1 and "media: radarr HTTP 500" in out
