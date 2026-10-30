"""`media digest` (daily/weekly summary cards for the friends' Seerr Telegram group). digest_fx.py fakes Emby,
Radarr/Sonarr, Seerr (incl. its Telegram settings), Tracearr, Telegraph and the Bot API; the clock is Sun 2026-10-25
19:00 Zurich. Checks: windows, first names only, requests grouped, plays >= 2 min, min_viewers, names (requesters,
viewers) only on the Mini App pages: never drawn on the card, in the caption or on Telegraph; upgrades/downgrades/
cleanups (+ snapshot removals), PNG + caption files, off switch, dry run never posts, sendPhoto fields, once per day,
Telegram errors without the token, GET-only towards the media apps, keys only in headers. Card styles: "compact" (the
default <= 1080x1350 teaser) and "full" (the tall card, caption with the link). Pillow runs in the tool's python3."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from mediatest import HERE, KEYS, PYTHON3

TOKEN = "12345:FAKEfakeFAKEfakeFAKE0"
NAMES = ("Robo", "Zed", "Mira", "Kito", "Ulla")
COMPACT_DAILY = "🎬 New today · 1 movie · 1 show\n🔥 Most watched: Moon Station EN"
LOAD = """import importlib.machinery, importlib.util, sys
ld = importlib.machinery.SourceFileLoader("m", sys.argv[1]); m = importlib.util.module_from_spec(importlib.util.spec_from_loader("m", ld))
ld.exec_module(m)
"""  # noqa: E501
DRAWN = r"""import contextlib, io, json, runpy, sys
from PIL import ImageDraw
media, conf = sys.argv[1:3]
seen, orig = [], ImageDraw.ImageDraw.text
def text(self, xy, s, *a, **k):
    seen.append(str(s))
    return orig(self, xy, s, *a, **k)
ImageDraw.ImageDraw.text = text
for style in ("compact", "full"):
    for kind in ("--daily", "--weekly"):
        json.dump({"style": style}, open(conf, "w"))
        sys.argv = [media, "digest", kind, "--dry-run", "--json"]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.suppress(SystemExit):
            runpy.run_path(media, run_name="__main__")
print("\n".join(seen))
"""
LN = """print([m.dg_top_line(x) for x in ({"kind": "show", "viewers": 3, "plays": 7}, {"kind": "show", "viewers": 1, "plays": 4},
                                  {"kind": "movie", "viewers": 1, "plays": 1, "year": 2024}, {"kind": "show", "viewers": 1, "plays": 1})])
print(m.dg_names(["A"]), "|", m.dg_names(["A", "B"]), "|", m.dg_names(["A", "B", "C"]), "|", m.dg_names(list("ABCDEF")))
"""  # noqa: E501
FIT = "print(m.dg_daily_fit(30, 9, 10)[3] <= m.DG_MAX_H, m.dg_daily_fit(11, 2, 8)[:3])\n"
CFIT = """v = {"new": [{"title": f"T{i}", "kind": "movie"} for i in range(30)], "reqs": [{"title": "R", "kind": "movie", "who": ["A"]}] * 9,
     "top": [{"title": "W", "kind": "show"}] * 10}
g, more, ts, top, b = m.dg_c_plan("daily", v)
w = {"new": v["new"][:10], "new_total": 39, "top": v["top"], "lib_line": ["2 movies", "1 show", "36 GB"]}
g2, more2, ts2, top2, b2 = m.dg_c_plan("weekly", w)
print(len(g), more, len(top), m.dg_c_height(len(g), ts, len(top), b) <= m.DG_C_H,
      len(g2), more2, m.dg_c_height(len(g2), ts2, len(top2), b2) <= m.DG_C_H)
"""  # noqa: E501


class Dg:
    def __init__(self, w):
        self.w, t = w, w.t
        self.t = t
        (t / "scenario").write_text("ok\n")
        b = w.serve("digest_fx.py", t / "scenario")
        (t / "cfg.json").write_text(
            json.dumps(
                {
                    "seerr": {"url": f"{b}/seerr", "key": KEYS[0]},
                    "radarr": {"url": f"{b}/radarr", "key": KEYS[1]},
                    "sonarr": {"url": f"{b}/sonarr", "key": KEYS[2]},
                    "emby": {"url": b, "key": "EMBY-KEY-Po66"},
                    "tracearr": {"url": f"{b}/ta", "key": "TA-KEY-Rt55"},
                }
            )
        )
        w.env.update(
            MEDIA_SECRETS=str(t / "cfg.json"),
            MEDIA_NOW="2026-10-25T19:00:00+01:00",
            MEDIA_TIMEOUT="5",
            MEDIA_CONTINUE_EMBY_USER="a" * 32,
            MEDIA_REPORT_DIR=str(t / "reports"),
            MEDIA_DIGEST_CONF=str(t / "digest.json"),
            MEDIA_DIGEST_STATE=str(t / "state.json"),
            MEDIA_DIGEST_TG_API=f"{b}/tg",
            MEDIA_POSTER_CACHE=str(t / "cache"),
            MEDIA_DIGEST_TELEGRAPH_API=f"{b}/tph",
            MEDIA_DIGEST_TELEGRAPH_KEY=str(t / "tph-key"),
            MEDIA_DIGEST_WEB_DIR=str(t / "web"),
            MEDIA_DIGEST_WEB_DEST=str(t / "www"),
        )
        if not shutil.which("rsync", path=w.env.get("PATH")):  # no rsync here (the CI runner): its stand-in
            w.env["PATH"] = f"{HERE / 'fakebin'}{os.pathsep}{w.env.get('PATH', '')}"
        vera = Path(os.environ.get("ASSISTANT_CI_TOOLCHAIN", "/nonexistent")) / "site" / "reportlab" / "fonts"
        if (
            not Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf").exists()
            and not Path("/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf").exists()
            and (vera / "Vera.ttf").is_file()
        ):  # no DejaVu or Noto font (the CI runner): Bitstream Vera under DejaVu's names
            (t / "fonts" / "dejavu").mkdir(parents=True)
            (t / "fonts" / "dejavu" / "DejaVuSans.ttf").symlink_to(vera / "Vera.ttf")
            (t / "fonts" / "dejavu" / "DejaVuSans-Bold.ttf").symlink_to(vera / "VeraBd.ttf")
            w.env["MEDIA_POSTERS_FONTS"] = str(t / "fonts")

    def run(self, *a, **env):
        self.w.m("digest", *a, env=env, merge=False)
        return self.w.rc

    def js(self, expr):
        return eval(expr, {}, {"d": json.loads(self.w.out)})  # noqa: S307 (the shell's js helper, test data only)

    def conf(self, d=None):
        p = self.t / "digest.json"
        if d is None:
            p.unlink(missing_ok=True)
        else:
            p.write_text(json.dumps(d))

    def scen(self, s):
        (self.t / "scenario").write_text(s + "\n")

    def clear(self):
        self.w.log_path.write_text("")

    def lines(self, prefix):
        return [x for x in self.w.log().splitlines() if x.startswith(prefix)]

    def post(self):
        """The sendPhoto line's JSON fields."""
        sp = "\n".join(self.lines("POST"))
        return sp, json.loads(sp[sp.index("{") :])

    def caption(self, kind="daily", day="2026-10-25"):
        return (self.t / "reports" / f"digest-{kind}-{day}.txt").read_text().rstrip("\n")

    def py(self, code, *args):
        r = subprocess.run(
            [PYTHON3, "-", self.w.tool, *map(str, args)],
            input=code,
            capture_output=True,
            text=True,
            env=self.w.env,
            timeout=120,
        )
        return (r.stdout + r.stderr).rstrip("\n")


@pytest.fixture
def dg(media):
    w = Dg(media)
    yield w
    assert "KEY-" not in media.log()  # keys only in headers, never in a URL
    media.safety(keys=(*KEYS, "EMBY-KEY-Po66", "TA-KEY-Rt55", TOKEN), get_only=False)


def test_daily_dry_run(dg):
    t = dg.t
    assert dg.run("--daily", "--dry-run", "--json") == 0
    assert dg.js('d["new_movies"]') == 1  # the weekly one is older
    assert dg.js('(d["new_shows"], d["new_episodes"])') == (1, 2)
    assert dg.js('d["requests"]') == [
        {"title": "Lost Reel EN", "who": ["Robo"]},
        {"title": "Star Voyage EN", "who": ["Robo", "Zed"]},
    ]
    # 2 viewers first, short plays out, weekly-only out, replaced file found by Tmdb id, no duplicate
    assert dg.js('d["top"]') == ["Moon Station EN", "Star Voyage EN"]
    assert dg.js('d["size"][0]') == 1080
    png = t / "reports" / "digest-daily-2026-10-25.png"
    assert png.stat().st_size > 0
    cap = dg.caption()
    assert dg.js('d["style"]') == "compact"
    assert cap == COMPACT_DAILY
    size = dg.py("import sys\nfrom PIL import Image\nw, h = Image.open(sys.argv[2]).size\nprint(w, h <= 1350)\n", png)
    assert size == "1080 True"  # 4:5: Telegram shows it uncropped
    assert dg.js('d["size"][1] <= 1350') is True
    assert "Folge" not in cap
    assert dg.js('d["new_titles"]') == ["Star Voyage EN", "Moon Station EN"]
    assert "Tester" not in dg.w.out + cap
    for n in NAMES:
        assert n not in cap
    assert dg.js('d["posted"]') is False
    assert "settings/notifications" not in dg.w.log()  # a dry run never reads the bot settings
    assert "POST" not in dg.w.log()

    # "style": "full" = the tall card and caption
    dg.conf({"style": "full"})
    rc = dg.run("--daily", "--dry-run", "--json")
    cap = dg.caption()
    assert (dg.js('d["style"]'), rc) == ("full", 0)
    assert "New on the server today: 1 movie, 1 show (2 episodes) · 2 requests fulfilled." in cap
    assert "Most watched: Moon Station EN, Star Voyage EN." in cap
    dg.conf({"style": "fancy"})
    rc = dg.run("--daily", "--dry-run", "--json")
    assert (dg.js('d["style"]'), rc) == ("compact", 3)
    assert "is not compact or full" in dg.w.err
    dg.conf()

    # the card draws no names: every string drawn on the PNG, compact and full, daily and weekly
    drawn = dg.py(DRAWN, t / "digest.json")
    dg.conf()
    assert "Moon Station EN" in drawn  # the capture works
    for n in (*NAMES, "Pim"):
        assert n not in drawn
    assert "Requested" in drawn  # a fulfilled request on a poster is badged without a name
    assert "2 viewers · 2 plays" in drawn
    assert "for " not in drawn
    assert dg.py(LOAD + LN) == (
        "['3 viewers · 7 plays', 'Show · 4 plays', 'Movie · 2024', 'Show']\nA | A & B | A, B & C | A, B, C, D +2"
    )
    # layout: a busy day still fits Telegram's 2560 px; compact: 8 posters + '+N more', top 3, within 1350 px
    assert dg.py(LOAD + FIT) == "True (11, 2, 8)"
    assert dg.py(LOAD + CFIT) == "8 22 3 True 8 31 True"


def test_off_post_once_and_errors(dg):
    t = dg.t
    # off: a live run without "enabled": true renders and posts nothing
    dg.clear()
    assert dg.run("--daily") == 0
    assert "off (" in dg.w.out
    assert dg.w.log() == ""

    # live post (enabled), once per day, the token stays out of every output
    dg.conf({"enabled": True})
    dg.clear()
    assert dg.run("--daily", "--json") == 0
    assert dg.js('d["message_id"]') == 777
    sp, _ = dg.post()
    assert '"chat_id": "-100777"' in sp
    assert '"photo": "<png' in sp
    assert "🎬 New today · 1 movie · 1 show" in sp
    assert "message_thread_id" not in sp
    assert "reply_markup" not in sp + "\n".join(dg.lines("TPH"))  # telegraph off: no button, no page
    assert TOKEN not in dg.w.out + dg.w.err
    assert "FAKEfake" not in (t / "state.json").read_text()
    st = json.loads((t / "state.json").read_text())
    assert (st["daily"]["day"], sorted(st["requests_posted"])) == ("2026-10-25", ["51", "52", "54"])
    log = dg.w.log().splitlines()
    assert any(x.startswith("GET") for x in log)
    assert [x for x in log if not x.startswith("GET") and "/tg/bot" not in x] == []  # GET-only to the media apps
    dg.clear()
    dg.run("--daily")
    assert "posted today already" in dg.w.out
    assert dg.lines("POST") == []
    dg.run("--daily", "--dry-run", "--json")
    assert len(dg.js('d["requests"]')) == 2  # posted requests stay on that day's page
    keep = (t / "state.json").read_text()
    st = json.loads(keep)
    st["requests_posted"] = {k: "2026-10-24" for k in st["requests_posted"]}
    (t / "state.json").write_text(json.dumps(st))
    dg.run("--daily", "--dry-run", "--json")
    assert dg.js('d["requests"]') == []  # requests posted on an earlier day are not repeated
    (t / "state.json").write_text(keep)

    # Telegram refuses: non-zero, the message without the token
    (t / "state.json").unlink()
    dg.scen("tg-fail")
    assert dg.run("--daily") != 0
    assert "telegram HTTP 400" in dg.w.err
    assert "FAKEfake" not in dg.w.out + dg.w.err

    # nothing new: no card, no post
    dg.scen("empty")
    dg.conf({"enabled": True})
    (t / "state.json").unlink(missing_ok=True)
    dg.clear()
    assert dg.run("--daily", "--json") == 0
    assert dg.js('d["path"]') is None
    assert dg.lines("POST") == []


def test_weekly_and_min_viewers(dg):
    t = dg.t
    dg.conf({"enabled": False})
    snap = {
        "library": {
            "2026-10-18": {
                "movies": {"1": 5000000000, "2": 30000000000, "9": 7000000000},
                "shows": {"1": 20000000000, "5": 1000000000},
                "episodes": 10,
                "bytes": 0,
            }
        }
    }
    (t / "state.json").write_text(json.dumps(snap))
    assert dg.run("--weekly", "--dry-run", "--json") == 0
    assert dg.js('d["new_movies"]') == 2
    assert dg.js('d["top"]') == ["Moon Station EN", "Star Voyage EN", "Quiet Harbor"]  # no English title: Emby's name
    assert dg.js('[t[:2] for t in d["tiles"]]') == [["Movies", "2"], ["Shows", "1"]]
    assert " GB" not in str(dg.js('d["tiles"]'))
    assert dg.js('d["chips"]') == ["Upgraded: 1 movie", "Downgraded: 1 movie", "Removed: 2 movies, 1 show"]
    assert dg.caption("weekly") == "📊 New in week 43 · 2 movies · 1 show\n🔥 Most watched: Moon Station EN"
    assert dg.js('d["size"][0] == 1080 and d["size"][1] <= 1350') is True
    dg.conf({"style": "full"})
    dg.run("--weekly", "--dry-run", "--json")
    cap = dg.caption("weekly")
    assert "Week 43 on the server: 2 movies, 1 show (2 episodes) new." in cap
    assert "Library: 2 movies, 1 shows." in cap
    assert " GB" not in cap
    assert dg.js('[t[0] for t in d["tiles"]]') == ["Movies", "Shows"]
    rc = dg.run(
        "--weekly",
        "--dry-run",
        "--json",
        MEDIA_NOW="2026-10-30T19:05:00+01:00",
        MEDIA_DIGEST_STATE=str(t / "state-fri.json"),
    )
    assert rc == 0  # on a Friday (the cron day): the Friday's week, a rolling 7 days
    assert "Week 44 on the server" in dg.caption("weekly", "2026-10-30").split("\n")[0]
    dg.conf()
    assert sorted(json.loads((t / "state.json").read_text())["library"]) == ["2026-10-18", "2026-10-25"]

    dg.conf({"min_viewers": 2})
    dg.run("--daily", "--dry-run", "--json")
    assert dg.js('d["top"]') == ["Moon Station EN"]  # titles watched by one person only stay out


def test_telegraph_page(dg):
    t = dg.t
    dg.conf({"enabled": True, "telegraph_author": "Test Media"})
    dg.clear()
    assert dg.run("--daily", "--dry-run", "--page", "--json") == 0
    assert dg.js('d["page_url"]') == "https://telegra.ph/New-today-10-25"
    assert "https://" not in dg.caption()  # compact: no URL in the caption
    assert len(dg.lines("TPH /tph/createAccount")) == 1
    assert '"short_name": "Test Media"' in "\n".join(x for x in dg.w.log().splitlines() if "createAccount" in x)
    assert oct((t / "tph-key").stat().st_mode & 0o777) == "0o600"
    assert "token=True" in "\n".join(x for x in dg.w.log().splitlines() if "createPage" in x)
    pg = "\n".join(dg.lines("TPH /tph/createPage"))
    assert "/web/index.html#!/item?id=101&serverId=srv123abc" in pg
    assert "https://image.tmdb.org/t/p/w500/abc123.jpg" in pg
    assert "Requests fulfilled (2)" in pg
    assert "1. Moon Station EN" in pg
    assert "A crew flies far." in pg
    for n in NAMES:
        assert n not in pg
    content = json.loads(json.loads(pg[pg.index("{") :])["content"])
    assert "href" not in str([n for n in content if n["tag"] == "h4" and "Lost Reel" in json.dumps(n)])
    assert "TPH-TOKEN" not in dg.w.out + dg.w.err
    assert dg.lines("POST") == []  # dry run: nothing sent to the group
    dg.run("--daily", "--dry-run", "--page", "--json")
    assert len(dg.lines("TPH /tph/createAccount")) == 1  # the account is reused

    dg.conf({"enabled": True, "telegraph": True})
    dg.clear()
    dg.run("--daily", "--json")
    _, f = dg.post()
    b = json.loads(f["reply_markup"])["inline_keyboard"][0][0]
    assert (b["text"], b["url"]) == ("📖 Open list", "https://telegra.ph/New-today-10-25")
    assert f["caption"] == COMPACT_DAILY  # compact: caption without the URL
    dg.conf({"enabled": True, "telegraph": True, "style": "full"})
    (t / "state.json").unlink()
    dg.clear()
    dg.run("--daily", "--json")
    assert "All titles: https://telegra.ph/New-today-10-25" in dg.post()[0]
    dg.scen("tph-fail")
    (t / "state.json").unlink()
    dg.clear()
    rc = dg.run("--daily", "--json")
    assert (rc, dg.js('d["message_id"]')) == (3, 777)  # telegraph down: the card still posted
    assert "reply_markup" not in dg.post()[0]


def test_mini_app_pages(dg):
    t = dg.t
    (t / "web").mkdir()
    (t / "web" / "d-2026-01-01.html").write_text("<html>old</html>\n")
    (t / "web" / "pages.json").write_text('{"d-2026-01-01": {"title": "old", "kind": "daily"}}\n')
    dg.conf({"enabled": True, "web_base": "https://media.example.test/media"})
    rc = dg.run("--daily", "--dry-run", "--web", "--json")
    assert (rc, dg.js('d["web_page"]')) == (0, "d-2026-10-25")
    www = t / "www"
    for f in ("d-2026-10-25.html", "today.html", "index.html", "pages.json", "style.css", "app.js"):
        assert (www / f).stat().st_size > 0, f
    wp = (www / "d-2026-10-25.html").read_text()
    for part in (
        "#!/item?id=101&amp;serverId=srv123abc",
        "https://image.tmdb.org/t/p/w342/abc123.jpg",
        "https://telegram.org/js/telegram-web-app.js",
        "Requests fulfilled <span>2</span>",
        "Moon Station EN",
        '<span class="req">for Robo &amp; Zed</span>',
        '<div class="m who">for Robo</div>',
        '<div class="m who">watched by Mira &amp; Kito · 2 plays</div>',
        "watched by Mira &amp; Ulla · 2 plays",  # a replaced file's viewers pooled into one title
        '<li class="row"><div class="poster" style="--ph:',  # a title not in Emby is a plain row
    ):
        assert part in wp, part
    assert "Tester" not in wp
    assert (www / "today.html").read_bytes() == (www / "d-2026-10-25.html").read_bytes()
    assert not any("d-2026-01-01" in p.name for p in www.iterdir())  # old page pruned
    assert "https://" not in dg.caption()
    assert dg.js('d["page_url"]') == "https://media.example.test/media/d-2026-10-25"
    assert dg.lines("POST") == []
    dg.run("--weekly", "--dry-run", "--web", "--json")
    assert dg.js('d["web_page"]') == "w-2026-43"
    week = (www / "week.html").read_text()
    assert "on disk" not in week and " GB<" not in week
    assert '<div class="l">Movies</div>' in week
    assert '<div class="m who">watched by Pim</div>' in week
    idx = (www / "index.html").read_text()
    assert (
        re.search(r'<script type="application/json" id="pages">([^<]*)', idx).group(1)
        == '["d-2026-10-25", "w-2026-43"]'
    )
    assert "<body data-index>" in idx
    assert "start_param" in (www / "app.js").read_text()

    dg.conf({"enabled": True, "miniapp": True, "miniapp_link": "https://t.me/TestBot/media"})
    (t / "state.json").unlink(missing_ok=True)
    dg.clear()
    dg.run("--daily", "--json")
    _, f = dg.post()
    assert "t.me" not in f["caption"]
    assert (
        json.loads(f["reply_markup"])["inline_keyboard"][0][0]["url"]
        == "https://t.me/TestBot/media?startapp=d-2026-10-25"
    )
    dg.conf({"enabled": True, "miniapp": True, "miniapp_link": "https://t.me/TestBot/media", "style": "full"})
    (t / "state.json").unlink(missing_ok=True)
    dg.clear()
    dg.run("--daily", "--json")
    assert "All titles: https://t.me/TestBot/media?startapp=d-2026-10-25" in dg.post()[0]
    dg.conf({"enabled": True})
    (t / "state.json").unlink(missing_ok=True)
    shutil.rmtree(www)
    dg.run("--daily", "--json")
    assert not www.exists()  # miniapp off: a live run publishes no page
