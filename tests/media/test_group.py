"""`media group` (movie night with friends: library movies nobody in the group has seen + a poll). tonight_fx.py builds
Emby's DBs and plays the SQL verb, group_fx.py adds the friends' Playback Reporting plays and fakes Emby's /Users and
per-user played/resumable movies, fake_tonight.py stands in for Seerr. Clock Sat 2026-10-10 20:00 Zurich. Checks: who
is in the group (default and named, accents, prefixes, unknown and ambiguous names, disabled users), what counts as
seen (played, resumable, other versions by TMDB id, plays of 20+ min), ★7+ first and mixed genres, mood/time/args,
the poll spec and buttons, partial failures, errors, GET-only, read-only SQL, no key leaks, nothing remembered."""

from __future__ import annotations

import json

import pytest
from mediatest import dead_port, fx

KEYS = ("SEERR-KEY-Zq81", "EMBY-KEY-Pq55")


def cfg(path, seerr, emby):
    path.write_text(json.dumps({"seerr": {"url": seerr, "key": KEYS[0]}, "emby": {"url": emby, "key": KEYS[1]}}))


@pytest.fixture
def grp(media):
    t = media.t
    media.py("tonight_fx.py", "build", t / "emby")
    media.py("group_fx.py", "plays", t / "emby")
    media.log_path = t / "seerr.log"
    media.err_path = t / "seerr.err"
    b = media.serve("fake_tonight.py")
    e = media.serve("group_fx.py", pre=("serve",), log=t / "emby.log", err=t / "emby.err")
    dead = f"http://127.0.0.1:{dead_port()}"
    cfg(t / "cfg.json", f"{b}/seerr", e)
    cfg(t / "seerr-down.json", dead, e)
    cfg(t / "emby-down.json", f"{b}/seerr", dead)
    (t / "noemby.json").write_text(json.dumps({"seerr": {"url": f"{b}/seerr", "key": KEYS[0]}}))
    (t / "play.json").write_text('{"enabled": true, "tv": "media_player.living_room_tv", "device": "Living Room"}\n')
    media.root_sql = fx("tonight_fx.py", "sql", t / "emby", t / "sql.log")
    media.env.update(
        MEDIA_SECRETS=str(t / "cfg.json"),
        MEDIA_NOW="2026-10-10T20:00:00+02:00",
        MEDIA_PAGE_SIZE="2",
        MEDIA_TIMEOUT="5",
        MEDIA_EMBY_DIR=str(t / "emby"),
        MEDIA_EMBY_SQL=media.root_sql + " {db}",
        MEDIA_TONIGHT_STATE=str(t / "st" / "tonight.json"),
    )
    yield media
    # safety: GET only (Emby and Seerr), no keys, the SQL verb rejected nothing, no writes
    media.safety(keys=KEYS, logs=(t / "emby.log", t / "seerr.log"))
    assert media.log(t / "emby.err") == ""
    assert "claude-bot-cmd" not in media.everything()
    media.sql_readonly(t / "sql.log")


def titles(m):
    return " | ".join(sorted(p["title"] for p in m.j["picks"]))


def elogn(m, pat):
    return m.logn(pat, m.t / "emby.log")


def test_default_group(grp):
    m = grp
    m.m("group", "--json")
    d = m.j
    assert m.rc == 0
    assert ", ".join(d["group"]["names"]) == "Friend, Ana, Ben, Zoë"
    assert (d["group"]["size"], d["group"]["default"], d["group"]["active_days"]) == (5, True, 180)
    msg = d["message"].splitlines()
    assert msg[0] == "🎬 **Movie night** · you + Friend, Ana, Ben, Zoë"
    assert msg[1] == "Nobody in the group has seen these:"
    assert len(d["picks"]) == 4
    lead = " | ".join(p["title"] for p in d["picks"][:2])
    assert "Kids Toon" in lead and "Krimi Film" in lead  # the two unseen ★7+ films lead
    for seen in (
        "Arrival",
        "Half Seen",
        "Other User Saw",
        "Dune: Part Two",
        "Solid Western",
        "Funny One",
        "Scary Night",
        "Bulk Movie",
    ):
        assert seen not in titles(m)
    assert "⚠️ 2 of the 4 picks rated under ★7: not enough unseen ★7+ movies fit" in d["message"]
    assert [p["why"] for p in d["picks"] if p["title"] == "Kids Toon"][0] == "Animation, Family"
    assert (d["message"].count("1️⃣ "), d["message"].count("4️⃣ ")) == (1, 1)
    poll = d["poll"]
    assert (poll["question"], len(poll["options"]), poll["anonymous"], poll["multiple"]) == (
        "Movie night: which one?",
        4,
        False,
        False,
    )
    assert poll["options"] == [p["title"] + " (" + str(p["year"]) + ")" for p in d["picks"]]
    assert "🗳 Poll below: forward it to the group." in d["message"]
    assert ([len(r) for r in d["buttons"]], d["play_on_tv"], all("url" in r[0] for r in d["buttons"])) == (
        [1, 1, 1, 1],
        False,
        True,
    )
    assert (elogn(m, "Filters=IsPlayed"), elogn(m, "Filters=IsResumable")) == (5, 5)  # two Emby calls per person
    assert (elogn(m, "/Users/efef9999"), elogn(m, "/Users/34343434"), elogn(m, "/Users/ffff6666")) == (0, 0, 0)
    assert not (m.t / "st" / "tonight.json").exists()  # remembers nothing


def test_named_groups(grp):
    m = grp
    m.m("group", "--with", "Ana", "--json")
    d = m.j
    assert m.rc == 0
    assert titles(m) == "Funny One | Kids Toon | Krimi Film | Scary Night"  # four ★7+ films, one per genre
    assert all(p["rating"] >= 7 for p in d["picks"])
    assert len({p["lead"] for p in d["picks"]}) == 4
    genres = sum((p["genres"] for p in d["picks"]), [])
    assert len(genres) == len(set(genres))
    assert d["message"].splitlines()[0] == "🎬 **Movie night** · you + Ana"
    assert (d["group"]["default"], d["group"]["active_days"]) == (False, None)
    assert elogn(m, "Filters=IsPlayed") == 2  # only his and Ana's history
    m.m("group", "--with", "Ana", "-n", "2", "--json")
    assert (len(m.j["picks"]), len(m.j["poll"]["options"])) == (2, 2)
    m.m("group", "--with", "ben", "--json")
    assert len(m.j["picks"]) == 4
    assert "Funny One" not in titles(m)  # Ben's 40 min count
    assert "Krimi Film" in titles(m)  # 5 min don't
    m.m("group", "--with", "Oldie", "--json")
    assert "Krimi Film" not in titles(m)  # a named inactive user's old play counts
    m.m("group", "--with", "zoe", "--json")
    assert m.j["group"]["names"] == ["Zoë"]  # accents ignored
    assert "Scary Night" not in titles(m)  # resumable counts as seen
    m.m("group", "--with", "ki, Nobody, Robin", "--json")
    d = m.j
    assert (d["group"]["names"], d["group"]["not_found"]) == ([], ["ki", "Nobody"])
    assert "'ki' could be Kim, Kim2, Kimi: left out" in d["message"]
    assert "'Nobody': no such Emby user" in d["message"]
    assert d["message"].splitlines()[:2] == ["🎬 **Movie night** · just you", "You haven't seen these:"]
    m.m("group", "--with", "Sleeper", "--json")
    assert m.j["group"]["names"] == ["Sleeper"]  # disabled user by name: included
    m.m("group", "--with", "Broken", "--json")
    assert m.rc == 3
    assert "couldn't check what Broken saw" in m.j["message"]
    assert (len(m.j["picks"]), m.j["group"]["unchecked"]) == (4, ["Broken"])


def test_args_mood_time(grp):
    m = grp
    m.m("group", "--args", "ana comedy", "--json")
    d = m.j
    assert (d["group"]["names"], d["mood"], [p["title"] for p in d["picks"]]) == (["Ana"], "comedy", ["Funny One"])
    assert d["poll"] is None and "Poll below" not in d["message"]  # one pick: no poll
    m.m("group", "--args", "with Ana and Ben 95", "--json")
    d = m.j
    assert (d["group"]["names"], d["time"]) == (["Ana", "Ben"], 95)
    assert titles(m) == "Kids Toon | Req_Film [Cut]"
    assert "**Req\\_Film \\[Cut\\]** (2025) · 95 min · ★6.5" in d["message"]
    assert "you requested it" in [p["why"] for p in d["picks"] if p["title"].startswith("Req")][0]
    assert "Req_Film [Cut] (2025)" in d["poll"]["options"]
    assert "you + Ana, Ben · ≤ 95 min" in d["message"].splitlines()[0]
    m.m("group", "--with", "Ana", "--mood", "funny", "--time", "100", "--json")
    assert (m.j["mood"], m.j["time"], [p["title"] for p in m.j["picks"]]) == ("funny", 100, ["Funny One"])


def test_buttons_text_root_sql(grp):
    m = grp
    m.m("group", "--with", "Ana", "--json", env={"MEDIA_PLAY_CONFIG": m.t / "play.json"})
    d = m.j
    assert ([[b["text"][:2] for b in r] for r in d["buttons"]][0], d["play_on_tv"]) == (["▶ ", "📺 "], True)
    assert all(r[1]["data"] == "mt:play:" + p["emby_id"] for r, p in zip(d["buttons"], d["picks"], strict=False))
    assert f"item?id={d['picks'][0]['emby_id']}&" in d["buttons"][0][0]["url"]
    out = m.m("group", "--with", "Ana")
    assert m.rc == 0
    assert m.line(1) == "🎬 Movie night · you + Ana"
    assert "   poll: Movie night: which one? " in out
    m.m("group", "--with", "Ana", "--json", env={"MEDIA_EMBY_SQL": m.root_sql})
    assert titles(m) == "Funny One | Kids Toon | Krimi Film | Scary Night"


def test_errors(grp):
    m, t = grp, grp.t
    out = m.m("group", env={"MEDIA_SECRETS": t / "noemby.json"})
    assert m.rc == 1 and "can't tell who has seen what" in out
    out = m.m("group", env={"MEDIA_SECRETS": t / "emby-down.json"})
    assert m.rc == 1 and "Emby users:" in out
    m.m(
        "group",
        "--with",
        "Ana",
        "--json",
        env={
            "MEDIA_SECRETS": t / "seerr-down.json",
            "MEDIA_TONIGHT_EMBY_UID": 7,
            "MEDIA_CONTINUE_EMBY_USER": "aaaa1111000000000000000000000000",
        },
    )
    assert m.rc == 0
    assert (len(m.j["picks"]), m.j["buttons"]) == (4, [])  # no links: Seerr has the link template
    m.m("group", "-n", "9")
    assert m.rc == 1
    m.m("group", "--time", "0")
    assert m.rc == 1
