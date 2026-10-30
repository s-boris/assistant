"""Offline tests for ~/bin/scraper-canary (SCRAPER_CANARY_UNDER_TEST=/path/to/copy tests a copy). Every tool is fed
by its own fixture env (no network); synthetic data only. Real canary state and inbox are never touched."""

import datetime as dt
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest

HOME = os.path.expanduser("~")
CANARY = os.environ.get("SCRAPER_CANARY_UNDER_TEST") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "scripts", "scraper-canary"
)
TOOLS = os.path.join(HOME, "bin")


def _fix(suite):
    # a sibling suite's fixtures in tests/legacy/<suite>/, else this suite's copy (fixtures/<suite>: the files these
    # tests map), else ~/opt/<suite>-tests
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.join(here, "..", suite, "fixtures"), os.path.join(here, "fixtures", suite)):
        if os.path.isdir(p):
            return p
    return os.path.join(HOME, f"opt/{suite}-tests/fixtures")


TRIP_FIX = _fix("trip-conditions")
RR_FIX = _fix("rave-radar")
DAY = "2026-10-18"


def load(name):
    ld = importlib.machinery.SourceFileLoader("t_" + name.replace("-", "_"), os.path.join(TOOLS, name))
    spec = importlib.util.spec_from_loader(ld.name, ld)
    m = importlib.util.module_from_spec(spec)
    ld.exec_module(m)
    return m


class Env:
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="scraper-canary-test-")
        self.state = os.path.join(self.tmp, "state")
        self.inbox = os.path.join(self.tmp, "inbox")
        os.makedirs(self.inbox)
        self.base = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(
                ("SCRAPER_CANARY_", "TRIP_CONDITIONS_", "DEALS_", "RAVE_RADAR_", "AI_RELEASES_", "KK_", "DEADLINES_")
            )
        }
        self.base.update(
            SCRAPER_CANARY_STATE=self.state,
            SCRAPER_CANARY_INBOX=self.inbox,
            SCRAPER_CANARY_GAP="0",
            SCRAPER_CANARY_BIN=TOOLS,
            SCRAPER_CANARY_TODAY=DAY,
        )

    def path(self, *p):
        return os.path.join(self.tmp, *p)

    def write(self, rel, data):
        p = self.path(rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(data if isinstance(data, str) else json.dumps(data))
        return p

    def run(self, *args, **env):
        e = dict(self.base, **env)
        p = subprocess.run(
            [CANARY, *args], env=e, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL
        )
        return p.returncode, p.stdout, p.stderr

    def results(self, **env):
        code, out, err = self.run("run", "--json", **env)
        assert code == 0, (code, out, err)
        res = json.loads(out)
        return {f"{r['tool']}/{r['source']}": r["status"] for r in res["results"]}, res

    def notes(self):
        return sorted(os.listdir(self.inbox))

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


# ---------------------------------------------------------------------------------------------- per-tool setups
def deals_env(e, mm_error=None):
    snap = {
        "ts": time.time() - 3600,
        "min_tb": 16,
        "items": [],
        "sources": {
            "alternate": {"label": "Alternate", "pages": 2, "n": 12, "error": None, "note": None},
            "mediamarkt": {
                "label": "MediaMarkt",
                "pages": 1 if mm_error is None else 0,
                "n": 5 if mm_error is None else 0,
                "error": mm_error,
                "note": None,
            },
        },
    }
    e.write("deals/hdd-latest.json", snap)
    return {"SCRAPER_CANARY_TOOLS": "deals", "DEALS_STATE_DIR": e.path("deals")}


def trip_env(e, break_reef=False, drop_forecast=False):
    fix = e.path("trip-fix")
    shutil.copytree(TRIP_FIX, fix)
    with open(os.path.join(TRIP_FIX, "trips.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    t = next(iter(cfg["trips"].values()))
    t["bulletin"] = {
        "format": "sma-marine",
        "label": "SMA",
        "area": 1,
        "page_url": "https://bulletin.example/{yyyy}-{mm}-{dd}/",
    }
    e.write("trips.json", cfg)
    shutil.copy(os.path.join(TRIP_FIX, "sma-marine.txt"), os.path.join(fix, "bulletin.txt"))
    e.write("trip-fix/bulletin_page.html", '<html><a href="/docs/marine.pdf">today</a></html>')
    if break_reef:
        e.write("trip-fix/crw.txt", "<html>moved</html>\n")
    if drop_forecast:
        os.remove(os.path.join(fix, "forecast.json"))
    return {
        "SCRAPER_CANARY_TOOLS": "trip-conditions",
        "TRIP_CONDITIONS_CONFIG": e.path("trips.json"),
        "TRIP_CONDITIONS_FIXTURES": fix,
        "TRIP_CONDITIONS_CACHE": e.path("trip-cache"),
        "TRIP_CONDITIONS_NOW": "2030-03-12T09:00",
    }


def rave_env(e):
    fix = e.path("rr-fix")
    shutil.copytree(RR_FIX, fix)
    srcs = [
        {
            "id": "delta",
            "kind": "wp",
            "url": "https://club-delta.example/wp-json/wp/v2/events",
            "name": "Delta",
            "city": "Alphastadt",
            "lat": 47.01,
            "lon": 8.01,
        },
        {"id": "gamma", "kind": "jsonld", "url": "https://gamma.example/", "name": "Gamma"},
        {"id": "gone", "kind": "jsonld", "url": "https://gone.example/", "name": "Gone"},
        {"id": "broken", "kind": "html", "url": "https://broken.example/", "name": "Broken"},
        {"id": "off", "kind": "html", "url": "https://off.example/", "name": "Off", "disabled": True},
    ]
    e.write("rr-cfg/config.json", {"max_km": 150, "places": {"Alphastadt": [47.02, 8.02]}, "sources": srcs})
    e.write("rr-cfg/artists.json", {"version": 1, "artists": [{"name": "DJ Nova", "aliases": [], "source": "t"}]})
    e.write(
        "rr-fix/urls.json",
        {
            "https://club-delta.example/wp-json/wp/v2/events": "wp.json",
            "https://gamma.example/": "jsonld.html",
            "https://gone.example/": "lineup-empty.html",
            "https://broken.example/": {"status": 500},
        },
    )
    return {
        "SCRAPER_CANARY_TOOLS": "rave-radar",
        "RAVE_RADAR_CONFIG_DIR": e.path("rr-cfg"),
        "RAVE_RADAR_STATE_DIR": e.path("rr-state"),
        "RAVE_RADAR_CACHE_DIR": e.path("rr-cache"),
        "RAVE_RADAR_FIXTURES": fix,
        "RAVE_RADAR_NOW": "2026-10-01",
        "RAVE_RADAR_HOST_GAP": "0",
        "ASSISTANT_PROFILE": os.path.join(RR_FIX, "profile.json"),
    }


RSS = (
    '<?xml version="1.0"?><rss version="2.0"><channel><title>Lab</title>'
    "<item><title>Introducing Model Nine</title><link>https://lab.example/nine</link><guid>n9</guid>"
    "<pubDate>Mon, 12 Oct 2026 10:00:00 GMT</pubDate><description>x</description></item>"
    "<item><title>Research note</title><link>https://lab.example/r</link><guid>r1</guid>"
    "<pubDate>Sun, 11 Oct 2026 10:00:00 GMT</pubDate><description>y</description></item></channel></rss>"
)


def ai_env(e):
    e.write("ai-fix/openai.xml", RSS)
    e.write("ai-fix/mistral.html", '<html><body><div id="app"></div></body></html>')
    e.write("ai-fix/claude-code.json", [{"tag_name": "v2.1.0", "draft": False}, {"tag_name": "v3.0.0"}])
    return {
        "SCRAPER_CANARY_TOOLS": "ai-releases",
        "AI_RELEASES_FIXTURES": e.path("ai-fix"),
        "AI_RELEASES_STATE_DIR": e.path("ai-state"),
        "AI_RELEASES_SOURCES": "openai,mistral,gemini,claude-code",
    }


KK_HDR = (
    "Versicherer,Kanton,Hoheitsgebiet,Geschäftsjahr,Erhebungsjahr,Region,Altersklasse,Unfalleinschluss,Tarif,"
    "Tariftyp,Altersuntergruppe,Franchisestufe,Franchise,Prämie,isBaseP,isBaseF,Tarifbezeichnung"
)


def kk_env(e, year=2027, header=KK_HDR, size=25_000_000, xlsx=True):
    row = (
        f"1,ZZ,P_OKPCH,{year},{year - 1},PR_REG_1,AKA_03_ERW,MIT_UNF,T1,BASE,E1,FRASTU_01,FRA_01_E_0300,400.0,1,1,Test"
    )
    d = e.path("kk-fix")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "praemien_ch.csv"), "w", encoding="utf-8-sig") as f:
        f.write(f"{header}\n{row}\n{row}\n")
    e.write("kk-fix/praemien_ch.csv.size", str(size))
    if xlsx:
        with open(os.path.join(d, f"praemienregionen-{year}.xlsx"), "wb") as f:
            f.write(b"PK\x03\x04" + b"\0" * 60)
        e.write(f"kk-fix/praemienregionen-{year}.xlsx.size", "690000")
    return {"SCRAPER_CANARY_TOOLS": "kk-compare", "SCRAPER_CANARY_KK_FIXTURES": d, "KK_DATA_DIR": e.path("kk-data")}


def dl_env(e, pages, entries=None):
    """Synthetic deadline data (example.org/example.net only) + fixture map {url: {status, final, ctype, body}}."""
    entries = entries or [
        {
            "id": "max-amount",
            "title": "Pay in",
            "category": "tax",
            "date": "12-31",
            "amounts": {"2026": "CHF 7,258"},
            "sources": [{"title": "A", "url": "https://www.example.org/amount"}],
        },
        {
            "id": "plain",
            "title": "Plain",
            "category": "tax",
            "date": "03-31",
            "sources": [
                {"title": "B", "url": "https://www.example.org/plain"},
                {"title": "C", "url": "https://sub.example.org/doc.pdf"},
                {"title": "D", "url": "file:///nowhere.json"},
            ],
        },
        {
            "id": "phrase",
            "title": "Phrase",
            "category": "tax",
            "date": "03-31",
            "sources": [{"title": "E", "url": "https://www.example.net/frist", "expect": ["31. März"]}],
        },
    ]
    data = e.write("dl/ch-zz.json", {"deadlines": entries})
    fx = e.write("dl/fixtures.json", pages)
    return {"SCRAPER_CANARY_TOOLS": "deadlines", "DEADLINES_DATA": data, "SCRAPER_CANARY_DEADLINES_FIXTURES": fx}


DL_OK = {
    "https://www.example.org/amount": {"body": "<p>Maximal CHF 7&#39;258 pro Jahr</p>"},
    "https://www.example.org/plain": {"status": 200, "final": "https://www.example.org/plain-new"},
    "https://sub.example.org/doc.pdf": {"ctype": "application/pdf"},
    "https://www.example.net/frist": {"body": "<div>Frist: 31.\n März</div>"},
}


class Base(unittest.TestCase):
    def setUp(self):
        self.e = Env()

    def tearDown(self):
        self.e.close()


class Tools(Base):
    def test_deals_ok_silent(self):
        st, res = self.e.results(**deals_env(self.e))
        self.assertEqual(st, {"deals/Alternate": "OK", "deals/MediaMarkt": "OK"})
        self.assertIsNone(res["note"])
        self.assertEqual(self.e.notes(), [])

    def test_deals_format_and_down(self):
        st, _ = self.e.results(**deals_env(self.e, "no products parsed (page layout changed?)"))
        self.assertEqual(st["deals/MediaMarkt"], "FORMAT-CHANGED")
        self.assertEqual(self.e.notes(), [f"scraper-canary-{DAY}.md"])
        e2 = Env()
        try:
            st, _ = e2.results(**deals_env(e2, "HTTP 503"))
            self.assertEqual(st["deals/MediaMarkt"], "DOWN")
            self.assertEqual(e2.notes(), [])
        finally:
            e2.close()

    def test_trip_ok(self):
        st, _ = self.e.results(**trip_env(self.e))
        for s in ("forecast", "marine", "archive", "reef", "advice", "bulletin"):
            self.assertEqual(st.get(f"trip-conditions/{s}"), "OK", (s, st))
        self.assertTrue(os.path.isdir(os.path.join(self.e.state, "trip-state")))  # advisory state kept apart

    def test_trip_broken(self):
        st, _ = self.e.results(**trip_env(self.e, break_reef=True, drop_forecast=True))
        self.assertEqual(st["trip-conditions/reef"], "FORMAT-CHANGED")
        self.assertEqual(st["trip-conditions/forecast"], "DOWN")
        self.assertEqual(st["trip-conditions/marine"], "OK")

    def test_rave(self):
        st, _ = self.e.results(**rave_env(self.e))
        self.assertEqual(st["rave-radar/delta"], "OK")
        self.assertEqual(st["rave-radar/gamma"], "OK")
        self.assertEqual(st["rave-radar/gone"], "FORMAT-CHANGED")
        self.assertEqual(st["rave-radar/broken"], "DOWN")
        self.assertEqual(st["rave-radar/deezer"], "SKIP")
        self.assertNotIn("rave-radar/off", st)

    def test_ai(self):
        st, _ = self.e.results(**ai_env(self.e))
        self.assertEqual(
            st,
            {
                "ai-releases/openai": "OK",
                "ai-releases/mistral": "FORMAT-CHANGED",
                "ai-releases/gemini": "DOWN",
                "ai-releases/claude-code": "OK",
            },
        )

    def test_kk_ok(self):
        st, res = self.e.results(**kk_env(self.e))
        self.assertEqual(st, {"kk-compare/premiums": "OK", "kk-compare/regions": "OK"})
        self.assertIn("2027 premiums", res["results"][0]["detail"])
        self.assertEqual(self.e.notes(), [])

    def test_kk_season(self):
        env = kk_env(self.e, year=2026, xlsx=False)
        st, _ = self.e.results(SCRAPER_CANARY_TODAY="2026-08-20", **env)  # before publication: fine
        self.assertEqual(st, {"kk-compare/premiums": "OK", "kk-compare/regions": "SKIP"})
        st, _ = self.e.results(SCRAPER_CANARY_TODAY="2026-10-05", **env)  # October, still old year: moved?
        self.assertEqual(st["kk-compare/premiums"], "FORMAT-CHANGED")
        st, _ = self.e.results(SCRAPER_CANARY_TODAY="2026-11-02", **env)
        self.assertEqual(st, {"kk-compare/*": "SKIP"})

    def test_kk_broken(self):
        st, _ = self.e.results(**kk_env(self.e, xlsx=False))  # next year's xlsx not there
        self.assertEqual(st["kk-compare/regions"], "DOWN")
        st, _ = self.e.results(**kk_env(self.e, header="<!DOCTYPE html><html>"))
        self.assertEqual(st["kk-compare/premiums"], "FORMAT-CHANGED")
        e2 = Env()
        try:
            st, _ = e2.results(**kk_env(e2, size=1200))
            self.assertEqual(st["kk-compare/premiums"], "FORMAT-CHANGED")
            st, _ = e2.results(**dict(kk_env(e2), SCRAPER_CANARY_KK_FIXTURES=e2.path("nope")))
            self.assertEqual(st["kk-compare/premiums"], "DOWN")
        finally:
            e2.close()


class Deadlines(Base):
    def st(self, pages, **kw):
        st, res = self.e.results(**dl_env(self.e, pages, **kw))
        return {k.split("/", 1)[1]: v for k, v in st.items()}, res

    def test_all_ok_and_phrases_seen(self):
        st, res = self.st(DL_OK)
        self.assertEqual(set(st.values()), {"OK"}, st)
        self.assertEqual(len(st), 4)  # file:// skipped
        self.assertIsNone(res["note"])
        det = {r["source"]: r["detail"] for r in res["results"]}
        self.assertIn("7258", det["www.example.org/amount"])
        self.assertIn("31. März", det["www.example.net/frist"])
        self.assertIn("now www.example.org/plain-new", det["www.example.org/plain"])

    def test_down_on_4xx_5xx_dns(self):
        pages = dict(DL_OK)
        pages["https://www.example.org/plain"] = {"status": 404}
        pages["https://sub.example.org/doc.pdf"] = {"status": 502}
        del pages["https://www.example.net/frist"]
        st, res = self.st(pages)
        self.assertEqual(st["www.example.org/plain"], "DOWN")
        self.assertEqual(st["sub.example.org/doc.pdf"], "DOWN")
        self.assertEqual(st["www.example.net/frist"], "DOWN")
        self.assertIn("[plain]", {r["source"]: r["detail"] for r in res["results"]}["www.example.org/plain"])
        self.assertEqual(self.e.notes(), [])  # DOWN alerts only on the second weekly run

    def test_offsite_and_home_redirect_are_format(self):
        pages = dict(DL_OK)
        pages["https://www.example.org/plain"] = {"final": "https://parked.example.com/"}
        pages["https://sub.example.org/doc.pdf"] = {"final": "https://www.example.org/de"}
        st, _ = self.st(pages)
        self.assertEqual(st["www.example.org/plain"], "FORMAT-CHANGED")
        self.assertEqual(st["sub.example.org/doc.pdf"], "FORMAT-CHANGED")
        self.assertEqual(self.e.notes(), [f"scraper-canary-{DAY}.md"])

    def test_key_phrase_gone_only_after_it_was_seen(self):
        pages = dict(DL_OK)
        pages["https://www.example.org/amount"] = {"body": "<p>Neu gestaltete Seite</p>"}
        st, _ = self.st(pages)  # never seen before: no baseline, no alarm
        self.assertEqual(st["www.example.org/amount"], "OK")
        st, _ = self.st(DL_OK)  # baseline
        st, res = self.st(pages)
        self.assertEqual(st["www.example.org/amount"], "FORMAT-CHANGED")
        self.assertIn("7258", {r["source"]: r["detail"] for r in res["results"]}["www.example.org/amount"])

    def test_amount_spellings(self):
        for body in ("CHF 7’258.–", "CHF 7 258", "CHF 7258", "Fr. 7,258"):
            pages = dict(DL_OK, **{"https://www.example.org/amount": {"body": body}})
            st, res = self.st(pages)
            self.assertIn("7258", {r["source"]: r["detail"] for r in res["results"]}["www.example.org/amount"], body)
        pages = dict(DL_OK, **{"https://www.example.org/amount": {"body": "CHF 17258 or 7.2580"}})
        _, res = self.st(pages)  # no false match: the earlier baseline now reads as gone
        self.assertIn(
            "no longer on the page", {r["source"]: r["detail"] for r in res["results"]}["www.example.org/amount"]
        )

    def test_robots_disallow_is_skip_and_unfetched(self):
        pages = dict(DL_OK, **{"https://www.example.net/robots.txt": {"body": "User-agent: *\nDisallow: /frist"}})
        st, _ = self.st(pages)
        self.assertEqual(st["www.example.net/frist"], "SKIP")
        pages["https://www.example.net/robots.txt"] = {"status": 503}
        st, _ = self.st(pages)
        self.assertEqual(st["www.example.net/frist"], "DOWN")

    def test_head_refused_falls_back_to_get(self):
        # fixture status applies to both methods; a 405 page stays DOWN, a 200 page is OK either way
        st, _ = self.st(dict(DL_OK, **{"https://www.example.org/plain": {"status": 405}}))
        self.assertEqual(st["www.example.org/plain"], "DOWN")

    def test_no_data_skip(self):
        st, _ = self.e.results(SCRAPER_CANARY_TOOLS="deadlines", DEADLINES_DATA=self.e.path("missing.json"))
        self.assertEqual(st, {"deadlines/*": "SKIP"})


class StateAndNote(Base):
    def test_down_two_weeks(self):
        env = deals_env(self.e, "HTTP 503")
        self.e.results(**env)
        self.assertEqual(self.e.notes(), [])
        later = (dt.date.fromisoformat(DAY) + dt.timedelta(days=7)).isoformat()
        self.e.results(**dict(env, SCRAPER_CANARY_TODAY=later))
        self.assertEqual(self.e.notes(), [f"scraper-canary-{later}.md"])
        with open(os.path.join(self.e.inbox, self.e.notes()[0]), encoding="utf-8") as f:
            body = f.read()
        self.assertIn("MediaMarkt", body)
        self.assertIn("since " + DAY, body)
        self.assertNotIn("Alternate", body)

    def test_recovery_resets_down(self):
        self.e.results(**deals_env(self.e, "HTTP 503"))
        d3 = (dt.date.fromisoformat(DAY) + dt.timedelta(days=3)).isoformat()
        self.e.results(**dict(deals_env(self.e), SCRAPER_CANARY_TODAY=d3))
        d7 = (dt.date.fromisoformat(DAY) + dt.timedelta(days=7)).isoformat()
        self.e.results(**dict(deals_env(self.e, "HTTP 503"), SCRAPER_CANARY_TODAY=d7))
        self.assertEqual(self.e.notes(), [])

    def test_adapter_error_is_noted(self):
        fake = self.e.path("fakebin")
        os.makedirs(fake)
        with open(os.path.join(fake, "rave-radar"), "w") as f:
            f.write("#!/usr/bin/env python3\nX = 1\n")
        st, _ = self.e.results(SCRAPER_CANARY_TOOLS="rave-radar", SCRAPER_CANARY_BIN=fake)
        self.assertEqual(st, {"rave-radar/*": "ERROR"})
        self.assertEqual(len(self.e.notes()), 1)

    def test_status_and_usage(self):
        code, out, _ = self.e.run("status")
        self.assertEqual(code, 0)
        self.assertIn("no run yet", out)
        self.e.results(**deals_env(self.e))
        code, out, _ = self.e.run("status")
        self.assertEqual(code, 0)
        self.assertIn("MediaMarkt", out)
        self.assertIn("STATUS", out)
        code, out, _ = self.e.run("status", "--json")
        self.assertIn("deals/Alternate", json.loads(out)["results"])
        self.assertEqual(self.e.run("bogus")[0], 2)
        self.assertEqual(self.e.run("run", SCRAPER_CANARY_TOOLS="nope")[0], 2)
        self.assertEqual(self.e.run("help")[0], 0)


if __name__ == "__main__":
    unittest.main(verbosity=1)
