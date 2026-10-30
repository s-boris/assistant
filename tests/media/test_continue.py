"""`media continue` (Emby Continue watching -> one Play-on-TV button each) and tonight's "▶ Continue" button.
fake_continue.py stands in for Emby (Resume, Next Up, a series' last played episode), fake_tonight.py for Seerr (his
user's linked Emby account), tonight_fx.py for Emby's DB (tonight's picks). Clock Sat 2026-10-10 20:00 Zurich.
Checks: Emby 4.9+ merged Resume and the legacy Next Up order, empty, series/season/folder/played entries never become
buttons, one entry per series, long titles, Markdown escaping, play off, partial and total failures, the user
override, tonight's button (recent only, never tonight's own pick, rows kept), GET-only, no key leaks."""

from __future__ import annotations

import json
import re

import pytest
from mediatest import KEYS, dead_port, fx

EMBY_KEY = "EMBY-KEY-Ct42"
MESSAGE = """▶ **Continue watching**

🍿 **Test Movie** (2024) · 45 of 110 min · today
📺 **The Extraordinarily Long Chronicles of Somew…** S02E03 · next up · 50 min · 2 days ago
📺 **Dark \\*Waters\\*** S01E05 · 10 of 55 min · 20.09.
🍿 **Old Movie** (2019) · 5 of 95 min · 01.12.2025"""


@pytest.fixture
def ct(media):
    t = media.t
    media.py("tonight_fx.py", "build", t / "emby")
    b = media.serve("fake_tonight.py")
    (t / "scenario").write_text("merged\n")
    e = media.serve("fake_continue.py", t / "scenario", log=t / "emby.log", err=t / "emby.err")
    dead = f"http://127.0.0.1:{dead_port()}"

    def cfg(name, seerr, emby=None):
        d = {
            "seerr": {"url": seerr, "key": KEYS[0]},
            "radarr": {"url": f"{b}/radarr", "key": KEYS[1]},
            "sonarr": {"url": f"{b}/sonarr", "key": KEYS[2]},
        }
        if emby:
            d["emby"] = {"url": emby, "key": EMBY_KEY}
        (t / name).write_text(json.dumps(d))

    cfg("cfg.json", f"{b}/seerr", e)
    cfg("noemby.json", f"{b}/seerr")
    cfg("seerr-down.json", dead, e)
    cfg("emby-down.json", f"{b}/seerr", dead)
    (t / "play-on.json").write_text('{"enabled": true, "tv": "media_player.test_tv", "device": "TESTTV"}\n')
    (t / "play-off.json").write_text('{"enabled": false, "tv": "media_player.test_tv", "device": "TESTTV"}\n')
    media.env.update(
        MEDIA_SECRETS=str(t / "cfg.json"),
        MEDIA_NOW="2026-10-10T20:00:00+02:00",
        MEDIA_PAGE_SIZE="2",
        MEDIA_TIMEOUT="5",
        MEDIA_EMBY_DIR=str(t / "emby"),
        MEDIA_EMBY_SQL=fx("tonight_fx.py", "sql", t / "emby", t / "sql.log") + " {db}",
        MEDIA_TONIGHT_STATE=str(t / "st" / "tonight.json"),
        MEDIA_PLAY_CONFIG=str(t / "play-on.json"),
    )
    yield media
    # safety: Emby GET-only with the key, Seerr GET-only, no keys, no tracebacks, the fakes clean
    assert [x for x in media.log(t / "emby.log").splitlines() if not re.fullmatch(r"GET .* token=ok", x)] == []
    media.safety(keys=(*KEYS, EMBY_KEY))
    assert media.log(t / "emby.err") == ""


def scen(m, s):
    (m.t / "scenario").write_text(s + "\n")


def mj(m, *a, **env):
    return m.m(*a, env=env, merge=False)


def ids(m):
    return " ".join(i["id"] for i in m.j["items"])


def btns(m):
    return " | ".join(r[0]["text"] + " " + r[0]["data"] for r in m.j["buttons"])


def test_continue(ct):
    m, t = ct, ct.t
    # merged Resume (Emby 4.9+: next episodes inside Resume)
    (t / "emby.log").write_text("")
    mj(m, "continue", "--json")
    d = m.j
    assert m.rc == 0
    assert ids(m) == "9001 9201 9301 9003"
    assert d["total"] == 5  # series/season/folder/played/duplicate series dropped, the Next Up extra kept
    assert [(i["state"], i["kind"], i["source"]) for i in d["items"]] == [
        ("resume", "movie", "resume"),
        ("next", "episode", "resume"),
        ("resume", "episode", "resume"),
        ("resume", "movie", "resume"),
    ]
    i0, i1 = d["items"][0], d["items"][1]
    assert (i0["title"], i0["year"], i0["pos_min"], i0["runtime_min"], i0["last_played"], i0["last_from"]) == (
        "Test Movie",
        2024,
        45,
        110,
        "2026-10-10T17:00:00Z",
        "item",
    )
    assert (
        i1["episode"],
        i1["episode_title"],
        i1["series_id"],
        i1["pos_min"],
        i1["last_played"],
        i1["last_from"],
    ) == ("S02E03", "Third", "9200", 0, "2026-10-08T19:00:00Z", "series")
    assert d["message"] == MESSAGE
    assert btns(m) == (
        "📺 Test Movie mt:play:9001 | 📺 The Extraordinarily L… S02E03 mt:play:9201 | "
        "📺 Dark *Waters* S01E05 mt:play:9301 | 📺 Old Movie mt:play:9003"
    )
    used = {r[0]["data"].split(":")[2] for r in d["buttons"]}
    assert not used & {"9100", "9200", "9300", "9400", "9500", "abc-def", "9002"}  # no series/season/folder id
    assert max(len(r[0]["text"]) for r in d["buttons"]) <= 32
    assert (d["play_enabled"], d["warnings"]) == (True, [])
    assert m.logn("ParentId=", t / "emby.log") == 1  # series lookup only for the shown undated next episode
    elog = m.log(t / "emby.log")
    assert "/emby/Users/aaaa1111000000000000000000000000/Items/Resume?" in elog
    assert "LegacyNextUp=true" in elog
    mj(m, "continue", "--json", "-n", "8")
    assert ids(m) == "9001 9201 9301 9003 9501"  # the Next Up extra comes last
    assert [i["last_played"] for i in m.j["items"]][-1] is None
    mj(m, "continue", "--json", "-n", "2")
    assert ids(m) == "9001 9201"
    out = m.m("continue", "-n", "9")
    assert m.rc == 1 and "-n is at most 8" in out
    m.m("continue", "-n", "0")
    assert m.rc == 1

    # text output
    out = m.m("continue")
    assert m.rc == 0
    assert "📺 Dark *Waters* S01E05 · 10 of 55 min · 20.09." in out
    assert "   button: mt:play:9201 = media play 9201" in out
    assert "**" not in out

    # legacy Emby (next episodes only under Next Up)
    scen(m, "legacy")
    mj(m, "continue", "--json")
    assert m.rc == 0 and ids(m) == "9001 9201 9301 9003"
    mj(m, "continue", "--json", "-n", "5")
    assert ids(m) == "9001 9201 9301 9003 9501"

    scen(m, "empty")
    mj(m, "continue", "--json")
    assert m.rc == 0 and (m.j["items"], m.j["buttons"], m.j["total"]) == ([], [], 0)
    assert "Nothing to continue: no partly watched movies or next episodes." in m.j["message"]

    scen(m, "merged")
    mj(m, "continue", "--json", MEDIA_PLAY_CONFIG=str(t / "play-off.json"))
    assert m.rc == 0 and (m.j["play_enabled"], m.j["buttons"], len(m.j["items"])) == (False, [], 4)
    assert "Play on TV is switched off, so no buttons." in m.j["message"]


def test_failures(ct):
    m, t = ct, ct.t
    scen(m, "nextup-down")
    mj(m, "continue", "--json")
    assert m.rc == 3 and ids(m) == "9001 9201 9301 9003"
    assert "Next Up: " in str(m.j["warnings"])
    assert "⚠️ Next Up didn't load" in m.j["message"]
    scen(m, "resume-down")
    out = m.m("continue", "--json")
    assert m.rc == 1 and "media: " in out
    scen(m, "merged")
    m.m("continue", env={"MEDIA_SECRETS": t / "emby-down.json"})
    assert m.rc == 1
    out = m.m("continue", env={"MEDIA_SECRETS": t / "noemby.json"})
    assert m.rc == 1 and "no emby url/key" in out
    m.m("continue", env={"MEDIA_SECRETS": t / "seerr-down.json"})
    assert m.rc == 1
    mj(
        m,
        "continue",
        "--json",
        MEDIA_SECRETS=str(t / "seerr-down.json"),
        MEDIA_CONTINUE_EMBY_USER="AAAA1111-0000-0000-0000-000000000000",
    )
    assert (m.rc, ids(m)) == (0, "9001 9201 9301 9003")  # the override skips Seerr
    out = m.m("continue", env={"MEDIA_CONTINUE_EMBY_USER": "nope"})
    assert m.rc == 1 and "MEDIA_CONTINUE_EMBY_USER must be an Emby user id" in out
    mj(m, "continue", "--json", MEDIA_TONIGHT_USER="Friend")
    assert m.rc == 1  # another Seerr user's Emby id gets nothing from Emby


def test_tonight_continue_button(ct):
    m, t = ct, ct.t

    def rows():
        return " | ".join(" + ".join(b["text"] for b in r) for r in m.j["buttons"])

    scen(m, "tonight")
    mj(m, "tonight", "--json", "--dry-run")
    assert m.rc == 0
    assert [p["emby_id"] for p in m.j["picks"] if p["slot"] == "B"][0] == "2014"
    assert m.j.get("continue") == "mt:play:9001"  # skips tonight's own pick, takes the movie watched today
    assert [[b.get("data") for b in r] for r in m.j["buttons"]][-2:] == [["mt:play:9001"], ["mt:more"]]
    assert m.j["buttons"][-2][0]["text"] == "▶ Continue Test Movie"
    with_btn = rows().replace(" | ▶ Continue Test Movie", "")
    scen(m, "old")
    mj(m, "tonight", "--json", "--dry-run")
    assert (m.rc, m.j.get("continue")) == (0, None)  # nothing recent (> 14 days)
    assert rows() == with_btn
    scen(m, "merged")
    mj(m, "tonight", "--json", "--dry-run")
    assert m.j.get("continue") == "mt:play:9001"
    scen(m, "resume-down")
    mj(m, "tonight", "--json", "--dry-run")
    assert (m.rc, m.j.get("continue")) == (0, None)
    assert "continue: " in str(m.j["warnings"])
    assert "continue" not in m.j["message"]
    scen(m, "tonight")
    (t / "emby.log").write_text("")
    mj(m, "tonight", "--json", "--dry-run", MEDIA_PLAY_CONFIG=str(t / "play-off.json"))
    assert (m.j.get("continue"), m.log(t / "emby.log")) == (None, "")  # play off: no button, no Emby call
    mj(m, "tonight", "--json", "--dry-run", MEDIA_SECRETS=str(t / "noemby.json"))
    assert (m.rc, m.j.get("continue"), [w for w in m.j["warnings"] if "continue" in w]) == (0, None, [])
    out = m.m("tonight", "--dry-run")
    assert "   continue: button mt:play:9001" in out
    assert not (t / "st" / "tonight.json").exists()  # dry runs left no tonight state
    assert "media continue [--json] [-n 4]" in m.m("-h")
