"""`media posters` (the weekly "New on the server" collage) and `media poster` (cached Primary images for picture
cards). posters_fx.py serves a fake Emby (views, items, series, poster images) and Radarr/Sonarr history; the clock is
Fri 2026-10-23 17:30 Zurich (week 43). Checks: what counts as new (window, upgrades/renames, specials, virtual
episodes, extras, excluded library), series grouping, ranking + cap, PNG, English texts, cron note + group send rule,
failures, GET-only, the key only in a header, no key leaks. PNG/JPEG checks run Pillow in the tool's python3."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

import pytest
from mediatest import KEYS

EMBY_KEY = "EMBY-KEY-Po66"
PIL_SIZE = (
    "import sys; from PIL import Image; im=Image.open(sys.argv[1]); print(im.format, im.size[0], im.size[1] > 900)"
)
PIL_FMT = "import sys; from PIL import Image; print(Image.open(sys.argv[1]).format)"


def pil(code, path) -> str:
    r = subprocess.run(["python3", "-c", code, str(path)], capture_output=True, text=True, timeout=60)
    return (r.stdout + r.stderr).strip()


def ago(path: Path, days: int):
    t = time.time() - days * 86400
    path.touch()
    os.utime(path, (t, t))


@pytest.fixture
def po(media):
    t = media.t
    (t / "scenario").write_text("ok\n")
    b = media.serve("posters_fx.py", t / "scenario")
    (t / "cfg.json").write_text(
        json.dumps(
            {
                "seerr": {"url": f"{b}/seerr", "key": KEYS[0]},
                "radarr": {"url": f"{b}/radarr", "key": KEYS[1]},
                "sonarr": {"url": f"{b}/sonarr", "key": KEYS[2]},
                "emby": {"url": b, "key": EMBY_KEY},
            }
        )
    )
    (t / "inbox").mkdir()
    (t / "reports").mkdir()
    media.env.update(
        MEDIA_SECRETS=str(t / "cfg.json"),
        MEDIA_NOW="2026-10-23T17:30:00+02:00",
        MEDIA_TIMEOUT="5",
        MEDIA_CONTINUE_EMBY_USER="a" * 32,
        MEDIA_POSTERS_INBOX=str(t / "inbox"),
        MEDIA_REPORT_DIR=str(t / "reports"),
        MEDIA_POSTERS_CONFIG=str(t / "posters.json"),
    )
    # no DejaVu or Noto font (the CI runner's image has none): Bitstream Vera from the CI toolchain's reportlab, under
    # DejaVu's names
    vera = Path(os.environ.get("ASSISTANT_CI_TOOLCHAIN", "/nonexistent")) / "site" / "reportlab" / "fonts"
    if (
        not Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf").exists()
        and not Path("/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf").exists()
        and (vera / "Vera.ttf").is_file()
    ):
        (t / "fonts" / "dejavu").mkdir(parents=True)
        (t / "fonts" / "dejavu" / "DejaVuSans.ttf").symlink_to(vera / "Vera.ttf")
        (t / "fonts" / "dejavu" / "DejaVuSans-Bold.ttf").symlink_to(vera / "VeraBd.ttf")
        media.env["MEDIA_POSTERS_FONTS"] = str(t / "fonts")
    yield media
    notes = "\n".join(p.read_text() for p in (t / "inbox").iterdir())
    for k in (*KEYS, EMBY_KEY):
        assert k not in media.everything() and k not in notes


def scen(m, s):
    (m.t / "scenario").write_text(s + "\n")


def p(m, *a, **env):
    return m.m("posters", *a, env=env, merge=False)


def test_what_counts_as_new(po):
    m, t = po, po.t
    p(m, "--json", "--out", t / "a.png")
    d = m.j
    assert m.rc == 3  # one poster failed to load
    assert (d["header"], d["subtitle"]) == ("New on the server · Week 43", "Added this week · 16–23 Oct")
    # new in the window, best rated first; upgrade, rename, old, extra, excluded library out
    assert [x["title"] for x in d["items"] if x["kind"] == "movie"] == [
        "Star Voyage",
        "Broken Poster",
        "New Then Better",
        "No Poster",
    ]
    assert [
        (x["title"], x["episodes"], x["seasons"], x["new_series"], x["rating"])
        for x in d["items"]
        if x["kind"] == "show"
    ] == [
        ("Nova Drift", 3, [1], True, 8.4),
        ("Night Shift", 2, [2, 3], False, 7.8),
        ("Laugh Track", 1, [4], False, 7.2),
    ]
    assert sorted(d["skipped"].items()) == [("missing", 1), ("specials", 1), ("upgrades", 3)]
    assert (d["counts"], d["total"], d["more"], d["send"]) == ("4 movies · 3 shows (6 new episodes)", 7, 0, True)
    assert d["caption"] == "🎬 New on the server (week 43): 4 movies · 3 shows (6 new episodes)."
    assert "Neu" not in d["caption"]
    assert d["post_chat"] is None
    assert "poster HTTP 500" in str(d["warnings"])
    assert pil(PIL_SIZE, t / "a.png") == "PNG 1080 True"
    log = m.log()
    assert "ParentId=20" not in log  # excluded library never queried
    assert not re.search(r"ParentId=(30|4)&", log)  # boxsets/live TV views never queried
    assert [x for x in log.splitlines() if not x.startswith("GET ")] == []
    assert "KEY" not in log  # the key only in a header
    assert "token=no" not in log  # every request carried it

    # text output, cap
    p(m, "--out", t / "b.png", "--days", "7")
    assert f"Collage: {t}/b.png (1080x" in m.out
    assert "left out: 1 missing, 1 specials, 3 upgrades" in m.out
    scen(m, "many")
    p(m, "--json", "--max", "10", "--out", t / "c.png")
    d = m.j
    assert (d["total"], d["shown"], d["more"], sum(x["kind"] == "movie" for x in d["items"])) == (37, 10, 27, 7)
    assert [x["title"] for x in d["items"]][:3] == ["Star Voyage", "Bulk Film 29", "Bulk Film 28"]
    assert pil(PIL_SIZE, t / "c.png") == "PNG 1080 True"
    p(m, "--json", "--max", "30", "--out", t / "c2.png")
    assert (m.j["shown"], sum(x["kind"] == "movie" for x in m.j["items"])) == (30, 27)

    # failures
    scen(m, "radarr-down")
    p(m, "--json", "--out", t / "d.png")
    assert m.rc == 3
    assert "radarr history failed, upgrades not filtered" in str(m.j["warnings"])
    assert "Old Upgrade" in [x["title"] for x in m.j["items"]]
    scen(m, "ok")
    p(m, "--days", "0")
    assert m.rc == 1
    p(m, "--out", t / "e.png", MEDIA_POSTERS_FONTS=str(t / "nofonts"))
    assert pil(PIL_SIZE, t / "e.png") == "PNG 1080 True"  # Pillow finds the system fonts by name
    p(m, MEDIA_SECRETS=str(t / "nocfg.json"))
    assert m.rc == 1


def test_cron(po):
    m, t = po, po.t
    note = t / "inbox" / "media-posters-2026-10-23.md"
    png = t / "reports" / "new-on-server-2026-10-23.png"
    p(m, "--cron")
    assert m.rc == 3
    assert pil(PIL_SIZE, png) == "PNG 1080 True"
    n = note.read_text()
    assert f"- Image: {png} (1080x" in n
    assert "- Send: group (post_chat not set)" in n  # 7 items, no posters.json
    assert "Send: yes" not in n  # never the owner
    assert "- Caption: 🎬 New on the server (week 43): 4 movies" in n
    note.unlink()
    (t / "posters.json").write_text('{"post_chat": ""}\n')
    p(m, "--cron")
    assert "- Send: group (post_chat not set)" in note.read_text()
    (t / "posters.json").write_text('{"post_chat": "-100555"}\n')
    p(m, "--cron")
    assert "- Send: group -100555" in note.read_text()
    note.unlink()
    (t / "posters.json").unlink()
    p(m, "--cron", "--days", "4")
    n = note.read_text()
    assert "- Send: no (fewer than 5 new items)" in n
    assert "1 movie · 2 shows (4 new episodes) = 3 items" in n
    note.unlink()
    png.unlink()
    p(m, "--cron", "--days", "1")
    assert m.rc == 0 and "Nothing new in the last 1 days" in m.out
    assert [x for d in ("inbox", "reports") for x in (t / d).rglob("*") if x.is_file()] == []
    scen(m, "empty")
    p(m, "--json")
    assert (m.j["path"], m.j["total"]) == (None, 0)


def test_poster_cache(po):
    m, t = po, po.t
    pc = t / "pcache"
    m.log_path.write_text("")

    def pm(*a, **env):
        return m.m("poster", *a, env=dict(env, HOME=str(t / "home"), MEDIA_POSTER_CACHE=str(pc)), merge=False)

    pm("--json", "101", "102", "101")
    assert m.rc == 0
    assert sorted(m.j["posters"].items()) == [("101", f"{pc}/101-w400.jpg"), ("102", f"{pc}/102-w400.jpg")]
    assert [m.j["fetched"], m.j["cached"], m.j["warnings"]] == [2, 0, []]
    assert pil(PIL_FMT, pc / "101-w400.jpg") == "JPEG"
    assert (oct(pc.stat().st_mode & 0o777), oct((pc / "101-w400.jpg").stat().st_mode & 0o777)) == ("0o700", "0o600")
    assert m.logn("Images/Primary?maxWidth=400") == 2
    m.log_path.write_text("")
    pm("--json", "101", "102")
    assert ([m.j["fetched"], m.j["cached"]], m.log()) == ([0, 2], "")  # from the cache, no request
    ago(pc / "101-w400.jpg", 40)
    pm("--json", "101")
    assert m.j["fetched"] == 1  # older than 30 days: fetched again
    pm("--json", "--width", "200", "103", "107")
    assert (m.rc, m.j["posters"]["107"], m.j["warnings"]) == (3, None, ["poster HTTP 500"])
    assert m.j["posters"]["103"] == f"{pc}/103-w200.jpg"
    assert pm("104") == f"104 {pc}/104-w400.jpg"
    for args in (("../x",), tuple(str(i) for i in range(1, 14)), ("--width", "5000", "101")):
        pm("--json", *args)
        assert m.rc == 2, args
    pm("--json", "101", MEDIA_SECRETS=str(t / "nocfg.json"))
    assert m.rc == 1
    log = m.log().splitlines()
    assert [x for x in log if not re.fullmatch(r"GET .* token=yes", x)] == [] and not any("KEY" in x for x in log)
    ago(pc / "102-w400.jpg", 70)
    ago(pc / ".pruned", 2)
    pm("--json", "101")
    assert not (pc / "102-w400.jpg").exists()  # once a day, entries not refreshed in 60 days are dropped
    assert [x for x in (t / "home").rglob("*") if x.is_file()] == []  # never writes under HOME
