#!/usr/bin/env -S python3 -I
"""Offline tests for ~/bin/trip-conditions (fixture responses of a fictional trip; no network).
Run: ~/opt/assistant/tests/legacy/trip-conditions/run.sh   (default: the repo's scripts/ copy;
TRIP_CONDITIONS_BIN overrides the script path)"""

import datetime as dt
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
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")
BIN = os.environ.get("TRIP_CONDITIONS_BIN") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "scripts", "trip-conditions"
)
NOW = "2030-03-10T09:00"


def dl(iso):
    d = dt.date.fromisoformat(iso)
    return f"{'Mon Tue Wed Thu Fri Sat Sun'.split()[d.weekday()]} {d.day:02d}.{d.month:02d}"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tc-test-")
        self.log = os.path.join(self.tmp, "urls.log")
        self.fix = FIX

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fixture_copy(self, drop=(), patch=None):
        d = os.path.join(self.tmp, "fx")
        shutil.copytree(FIX, d, dirs_exist_ok=True)
        for name in drop:
            os.unlink(os.path.join(d, name))
        for name, fn in (patch or {}).items():
            p = os.path.join(d, name)
            with open(p, encoding="utf-8") as f:
                body = f.read()
            with open(p, "w", encoding="utf-8") as f:
                f.write(fn(body))
        self.fix = d
        return d

    def run_tc(self, *args, config=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith("TRIP_CONDITIONS_")}
        env.update(
            {
                "TRIP_CONDITIONS_CONFIG": config or os.path.join(FIX, "trips.json"),
                "TRIP_CONDITIONS_CACHE": os.path.join(self.tmp, "cache"),
                "TRIP_CONDITIONS_STATE": os.path.join(self.tmp, "state"),
                "TRIP_CONDITIONS_NOW": NOW,
                "TRIP_CONDITIONS_FIXTURES": self.fix,
                "TRIP_CONDITIONS_URL_LOG": self.log,
            }
        )
        p = subprocess.run(
            [sys.executable, "-I", BIN, *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
        )
        return p.returncode, p.stdout, p.stderr

    def urls(self):
        try:
            with open(self.log, encoding="utf-8") as f:
                return f.read().splitlines()
        except OSError:
            return []

    def js(self, *args):
        rc, out, err = self.run_tc(*args, "--json")
        self.assertEqual(rc, 0, err)
        return json.loads(out)


class TestBrief(Base):
    def test_preview_text(self):
        rc, out, err = self.run_tc("testisles")
        self.assertEqual(rc, 0, err + out)
        self.assertIn("🏝 Test Isles conditions · Sun 10.03", out)
        self.assertIn("Preview: the trip starts in 2 days", out)
        for t in ("**North Isle · Harbour Beach**", "**South Isle · Palm Cove**", "**East Isle**"):
            self.assertIn(t, out)
        self.assertIn("⛈ Thunderstorms, 30 mm (80 %) · 25–30 °C", out)
        self.assertIn("💨 ESE 8 kn, gusts 16 · Harbour Beach sheltered → next day SSW 26 kn", out)
        self.assertIn("🌊 Waves 0.8 m, swell 0.6 m SSE 8 s · sea 28 °C", out)
        self.assertIn("🤿 Outer reef: go · visibility good · estimate", out)
        self.assertIn("☀️ UV 9 · 06:10–18:20 · tide ~high 03:00/15:00, low 09:00/21:00", out)
        self.assertIn("⛴ Fast ferry: calm enough", out)
        self.assertIn("🗓 Season (Mar): fictional dry season, light winds", out)
        self.assertIn(f"🧭 NW regime: signal from {dl('2030-03-12')}", out)
        self.assertIn("🪸 Reef (05.03, NOAA CRW): no heat stress · SST 29.1 °C (+1.3) · DHW 0.0", out)
        self.assertIn("🛂 GOV.UK advice (last update 15.01.2030): first check · “Fictional update", out)
        self.assertIn("🔗 Other advisory: https://example.org/advice", out)
        self.assertIn("📝 Fictional note line", out)
        self.assertNotIn("⚠️", out)
        block = out.split("**North Isle · Harbour Beach**")[1].split("\n\n")[0]
        self.assertLessEqual(len(block.strip().splitlines()), 8)

    def test_json_values(self):
        j = self.js("testisles")
        self.assertEqual(j["forecast_until"], "2030-03-24")  # the empty 16th day is not forecast
        north = j["days"][0]["legs"][0]
        self.assertEqual(north["leg"], "North Isle")
        self.assertEqual(north["dive"], {"long_range": "go", "visibility": "good", "why": [], "estimate": True})
        self.assertEqual(north["sea"]["temp_c"], 28.5)
        self.assertEqual(north["rain_48h_mm"], 2.0)
        self.assertEqual(north["tides"]["high"], ["03:00", "15:00"])
        self.assertEqual(north["tides"]["low"], ["09:00", "21:00"])
        self.assertEqual(north["exposure"], "sheltered")
        self.assertEqual(j["reef"]["alert"], 0)
        self.assertEqual(j["advisory"]["govuk"]["status"], "first check")
        self.assertEqual(j["sources"]["forecast"], {"ok": True, "note": None})
        self.assertFalse(any(k.startswith("_") for k in j))

    def test_rough_day_and_window(self):
        rc, out, err = self.run_tc("testisles", "--date", "2030-03-11")
        self.assertEqual(rc, 0, err)
        self.assertIn("🤿 Outer reef: no · visibility reduced (runoff, rough sea) · estimate", out)
        self.assertIn(f"→ next long-range window: {dl('2030-03-13')}", out)
        self.assertIn("⛴ Fast ferry: likely rough: delays or cancellations possible", out)
        j = self.js("testisles", "--date", "2030-03-12")
        north = j["days"][0]["legs"]
        self.assertEqual(len(north), 1)  # trip mode: only the leg of the day
        self.assertEqual(north[0]["dive"]["long_range"], "marginal")
        self.assertEqual(north[0]["dive"]["why"], ["runoff"])
        self.assertEqual(north[0]["exposure"], "onshore")

    def test_trip_mode_text(self):
        rc, out, err = self.run_tc("testisles", "--date", "2030-03-12")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("Preview", out)
        self.assertIn("· onshore at Harbour Beach", out)
        self.assertNotIn("South Isle", out)

    def test_compact_beyond_and_travel_day(self):
        j = self.js("testisles", "--date", "2030-03-17", "--days", "12")
        legs = {d["date"]: [r["leg"] for r in d["legs"]] for d in j["days"]}
        self.assertEqual(legs["2030-03-17"], ["North Isle"])
        self.assertEqual(legs["2030-03-18"], ["South Isle"])  # overlap day: the later leg
        self.assertEqual(legs["2030-03-23"], [])
        self.assertEqual(legs["2030-03-24"], ["East Isle"])
        self.assertNotIn("2030-03-25", legs)
        self.assertEqual(len(j["beyond"]), 1)
        b = j["beyond"][0]
        self.assertEqual((b["leg"], b["from"], b["to"]), ("East Isle", "2030-03-25", "2030-03-28"))
        self.assertEqual(b["climate"]["rain_mm_day"], 2.0)
        self.assertEqual(b["climate"]["wet_days_pct"], 50)
        self.assertEqual(b["climate"]["tmax_c"], 30.0)
        self.assertEqual(j["climate_years"], "2025–2029")
        arch = [u for u in self.urls() if "archive-api" in u]
        self.assertEqual(len(arch), 5)
        self.assertTrue(any("start_date=2029-03-25" in u and "end_date=2029-03-28" in u for u in arch))
        self.assertTrue(any("start_date=2025-03-25" in u for u in arch))
        rc, out, err = self.run_tc("testisles", "--date", "2030-03-17", "--days", "12")
        self.assertEqual(rc, 0, err)
        self.assertIn("· Sun 17.03–Thu 28.03**", out)
        self.assertIn(f"{dl('2030-03-17')} ⛅ 25–30° 0 mm · E 9 kn · 🌊 0.9 m · 🤿 go, vis good", out)
        self.assertIn(f"{dl('2030-03-19')} 🌤 25–30° 0.2 mm · E 9 kn · 🌊 –", out)
        self.assertIn(f"{dl('2030-03-23')}: no leg (travel day)", out)
        self.assertIn("**East Isle** · 25.03–28.03, beyond the forecast", out)
        self.assertLess(out.index("no leg (travel day)"), out.index("🤿 = long-range"))
        self.assertIn(
            "📊 Typical for these dates (2025–2029): 25–30 °C · rain 2.0 mm/day, 50 % wet days · wind 10 kn", out
        )
        self.assertLess(len(out.splitlines()), 40)

    def test_all_days_beyond(self):
        rc, out, err = self.run_tc("testisles", "--date", "2030-03-26", "--days", "2")
        self.assertEqual(rc, 0, err)
        self.assertIn("· Tue 26.03–Wed 27.03**", out)
        self.assertIn("**East Isle** · 26.03–27.03, beyond the forecast", out)
        self.assertFalse(any("api.open-meteo.com/v1/forecast" in u for u in self.urls()))

    def test_full_flag(self):
        rc, out, err = self.run_tc("testisles", "--date", "2030-03-12", "--days", "3", "--full")
        self.assertEqual(rc, 0, err)
        self.assertIn(f"**{dl('2030-03-13')} · North Isle · Harbour Beach**", out)


class TestSources(Base):
    def test_urls_and_politeness(self):
        self.run_tc("testisles")
        u = self.urls()
        fc = [x for x in u if "api.open-meteo.com/v1/forecast" in x]
        mar = [x for x in u if "marine-api" in x]
        self.assertEqual((len(fc), len(mar)), (1, 1))  # one multi-location call each
        for k in (
            "past_days=2",
            "forecast_days=16",
            "wind_speed_unit=kn",
            "timezone=Etc%2FGMT-4",
            "latitude=10.000,9.000,8.000",
        ):
            self.assertIn(k, fc[0])
        self.assertIn("latitude=10.100,9.100,8.100", mar[0])
        self.assertIn("sea_level_height_msl", mar[0])
        self.assertTrue(any(x.endswith("/vs/data/testland.txt") for x in u))
        self.assertTrue(any(x.endswith("/foreign-travel-advice/testland") for x in u))

    def test_no_advisory(self):
        rc, out, err = self.run_tc("testisles", "--no-advisory")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("🛂", out)
        self.assertNotIn("🪸", out)
        self.assertFalse(any("gov.uk" in x or "coralreefwatch" in x for x in self.urls()))

    def test_source_failure_still_briefs(self):
        self.fixture_copy(drop=("marine.json",))
        rc, out, err = self.run_tc("testisles")
        self.assertEqual(rc, 2)
        self.assertIn("**North Isle · Harbour Beach**", out)
        self.assertIn("🌊 sea forecast not out yet", out)
        self.assertIn("⚠️ marine: no response (fixture missing)", out)
        self.assertNotIn("🤿", out.split("**South Isle")[0].split("Harbour Beach**")[1])

    def test_bad_json_source(self):
        self.fixture_copy(patch={"govuk.json": lambda b: "<html>blocked</html>"})
        rc, out, err = self.run_tc("testisles")
        self.assertEqual(rc, 2)
        self.assertIn("⚠️ advice: unreadable response", out)

    def test_advisory_change_tracking(self):
        self.assertIn("first check", self.run_tc("testisles")[1])
        self.assertIn("no change since 10.03", self.run_tc("testisles")[1])
        self.fixture_copy(
            patch={
                "govuk.json": lambda b: b.replace("2030-01-15T10:00:00+00:00", "2030-03-09T08:00:00+00:00").replace(
                    '"alert_status": []', '"alert_status": ["avoid_all_but_essential_travel_to_parts"]'
                )
            }
        )
        out = self.run_tc("testisles")[1]
        self.assertIn("🛂 GOV.UK advice (last update 09.03.2030): changed since the last check (10.03)", out)
        self.assertIn("⚠️ GOV.UK alert: avoid all but essential travel to parts", out)
        self.assertIn("changed today", self.run_tc("testisles")[1])

    def test_reef_alert(self):
        self.fixture_copy(
            patch={
                "crw.txt": lambda b: (
                    b + "2030 03 06 27.0000 30.5000 30.2000      2.1000       1.1000    4.3000            2\n"
                )
            }
        )
        out = self.run_tc("testisles")[1]
        self.assertIn("🪸 Reef (06.03, NOAA CRW): bleaching warning · SST 30.2 °C (+2.1) · DHW 4.3", out)


class TestUsage(Base):
    def test_errors(self):
        rc, out, err = self.run_tc("nowhere")
        self.assertEqual(rc, 1)
        self.assertIn("have: testisles", err)
        self.assertEqual(self.run_tc("testisles", "--days", "0")[0], 1)
        self.assertEqual(self.run_tc("testisles", "--date", "2030-03-01")[0], 1)
        self.assertEqual(self.run_tc("testisles", "--date", "10.03.2030")[0], 1)
        self.assertEqual(self.run_tc("testisles", "--bogus")[0], 1)
        self.assertEqual(self.run_tc()[0], 1)
        rc, out, err = self.run_tc("--list")
        self.assertEqual((rc, out.strip()), (0, "testisles\tTest Isles"))
        self.assertEqual(self.run_tc("--help")[0], 0)
        missing = os.path.join(self.tmp, "none.json")
        self.assertEqual(self.run_tc("testisles", config=missing)[0], 1)
        bad = os.path.join(self.tmp, "bad.json")
        with open(bad, "w") as f:
            json.dump({"trips": {"x": {"tz": "Etc/GMT-4", "legs": [{"name": "A", "from": "2030-03-12"}]}}}, f)
        rc, out, err = self.run_tc("x", config=bad)
        self.assertEqual(rc, 1)
        self.assertIn("lacks 'to'", err)


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tc-cache-")
        loader = importlib.machinery.SourceFileLoader("tripcond", BIN)
        spec = importlib.util.spec_from_loader("tripcond", loader)
        self.m = importlib.util.module_from_spec(spec)
        loader.exec_module(self.m)
        self.m.FIXTURES = None
        self.m.CACHE = self.tmp
        self.calls = []
        self.fail = False

        def fake(req, timeout=None):
            self.calls.append((req.full_url, req.get_header("User-agent"), timeout))
            if self.fail:
                raise OSError("down")
            return FakeResp(b'{"n": %d}' % len(self.calls))

        self.m.web.open = fake  # the script's one fetch call (core.http.open)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def age_all(self, seconds):
        for n in os.listdir(self.tmp):
            p = os.path.join(self.tmp, n)
            t = time.time() - seconds
            os.utime(p, (t, t))

    def test_ttl_stale_and_headers(self):
        url = "https://example.invalid/x?a=1"
        self.assertEqual(self.m.fetch(url, "forecast"), ('{"n": 1}', None))
        self.assertEqual(self.m.fetch(url, "forecast"), ('{"n": 1}', None))  # cached
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.calls[0][1].startswith("trip-conditions/"))
        self.assertEqual(self.calls[0][2], 25)
        self.m.fetch(url, "forecast", refresh=True)
        self.assertEqual(len(self.calls), 2)
        self.age_all(4 * 3600)  # past the 3 h TTL
        self.assertEqual(self.m.fetch(url, "forecast")[0], '{"n": 3}')
        self.assertEqual(self.m.fetch(url, "archive")[0], '{"n": 3}')  # 30-day TTL: still fresh
        self.fail = True
        self.age_all(5 * 3600)
        body, note = self.m.fetch(url, "forecast")
        self.assertEqual(body, '{"n": 3}')
        self.assertIn("stale copy, 5 h old", note)
        self.age_all(8 * 86400)
        with self.assertRaises(self.m.SourceError):
            self.m.fetch(url, "forecast")
        self.assertFalse([n for n in os.listdir(self.tmp) if n.endswith(".tmp")])

    def test_binary_fetch_cached(self):
        url = "https://example.invalid/b.pdf"
        self.assertEqual(self.m.fetch(url, "bulletin_pdf", binary=True), (b'{"n": 1}', None))
        self.assertEqual(self.m.fetch(url, "bulletin_pdf", binary=True), (b'{"n": 1}', None))
        self.assertEqual(len(self.calls), 1)
        self.age_all(5 * 3600)  # past the 4 h TTL
        self.assertEqual(self.m.fetch(url, "bulletin_pdf", binary=True)[0], b'{"n": 2}')


SMA = os.path.join(FIX, "sma-marine.txt")  # a real bulletin's pdftotext -layout table; header, notes and footer removed
PAGE = '<html><a href="/docs/1/Marine_Forecast_10th_March_2030.pdf">today</a></html>'


class TestBulletin(Base):
    def setup_bulletin(self, text=None, page=PAGE, drop=()):
        d = self.fixture_copy()
        with open(SMA, encoding="utf-8") as f:
            body = f.read().replace("Mon 19 Oct 2026", "Sun 10 Mar 2030")
        files = {"bulletin_page.html": page, "bulletin.txt": body if text is None else text}
        for name, body in files.items():
            if name not in drop:
                with open(os.path.join(d, name), "w", encoding="utf-8") as f:
                    f.write(body)
        with open(os.path.join(FIX, "trips.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        cfg["trips"]["testisles"]["bulletin"] = {
            "format": "sma-marine",
            "label": "SMA",
            "area": 1,
            "page_url": "https://example.invalid/mf/marine-forecast-{d}{th}-{month}-{yyyy}/",
            "pdf_regex": 'href="(/docs/[^"]+\\.pdf)"',
        }
        self.cfg = os.path.join(self.tmp, "trips.json")
        with open(self.cfg, "w", encoding="utf-8") as f:
            json.dump(cfg, f)

    def test_official_line_and_tides(self):
        self.setup_bulletin()
        rc, out, err = self.run_tc("testisles", config=self.cfg)
        self.assertEqual(rc, 0, err + out)
        self.assertIn("🏛 Official (SMA, area 1): Moderate/Rough · waves 1.5–2.5 m · wind E–SE 11–24 kn, max 32", out)
        self.assertIn("tide high 09:20/00:06, low 17:21/04:14", out)
        self.assertNotIn("tide ~", out)
        self.assertNotIn("bulletin unavailable", out)
        u = self.urls()
        self.assertIn("https://example.invalid/mf/marine-forecast-10th-march-2030/", u)
        self.assertIn("https://example.invalid/docs/1/Marine_Forecast_10th_March_2030.pdf", u)

    def test_json(self):
        self.setup_bulletin()
        rc, out, err = self.run_tc("testisles", "--json", config=self.cfg)
        self.assertEqual(rc, 0, err)
        rec = json.loads(out)["days"][0]["legs"][0]
        self.assertEqual(rec["tides"]["source"], "SMA bulletin")
        self.assertEqual(rec["official"]["wave_m"], [1.5, 2.5])
        self.assertEqual(rec["official"]["wind"]["max_kmh"], 60)
        self.assertIn("tides_model", rec)

    def assert_unavailable(self, out, rc, why):
        self.assertEqual(rc, 2)
        notes = [ln for ln in out.splitlines() if "bulletin unavailable" in ln]
        self.assertEqual(notes, [f"⚠️ SMA: bulletin unavailable ({why})"])
        self.assertIn("🌊 Waves", out)  # the rest of the brief still prints
        self.assertIn("tide ~", out)  # model tides stay
        self.assertNotIn("🏛", out)

    def test_missing_pdf(self):
        self.setup_bulletin(drop=("bulletin.txt",))
        rc, out, err = self.run_tc("testisles", config=self.cfg)
        self.assert_unavailable(out, rc, "no response (fixture missing)")

    def test_changed_pdf(self):
        self.setup_bulletin(text="Some other document\nwith no forecast table\n")
        rc, out, err = self.run_tc("testisles", config=self.cfg)
        self.assert_unavailable(out, rc, "format not recognised")

    def test_no_pdf_link(self):
        self.setup_bulletin(page="<html>maintenance</html>")
        rc, out, err = self.run_tc("testisles", config=self.cfg)
        self.assert_unavailable(out, rc, "no PDF link on the page")

    def test_old_bulletin(self):
        with open(SMA, encoding="utf-8") as f:
            self.setup_bulletin(text=f.read().replace("Mon 19 Oct 2026", "Sat 09 Mar 2030"))
        rc, out, err = self.run_tc("testisles", config=self.cfg)
        self.assert_unavailable(out, rc, "latest is from 09.03")

    def test_not_fetched_for_other_days(self):
        self.setup_bulletin()
        rc, out, err = self.run_tc("testisles", "--date", "2030-03-11", config=self.cfg)
        self.assertEqual(rc, 0, err)
        self.assertFalse([u for u in self.urls() if "example.invalid" in u])
        self.assertNotIn("🏛", out)


class TestBulletinParse(unittest.TestCase):
    def setUp(self):
        loader = importlib.machinery.SourceFileLoader("tripcond_b", BIN)
        spec = importlib.util.spec_from_loader("tripcond_b", loader)
        self.m = importlib.util.module_from_spec(spec)
        loader.exec_module(self.m)
        with open(SMA, encoding="utf-8") as f:
            self.text = f.read()

    def test_area2_and_dirs(self):
        r = self.m.parse_sma(self.text, 2)
        self.assertEqual(
            (r["sea_state"], r["wave_m"], r["wind"]["dir"], r["wind"]["kmh"]),
            ("Moderate/Rough", [2.0, 3.5], "SE", [20, 35]),
        )
        self.assertEqual(r["date"], "2026-10-19")
        self.assertEqual(self.m.short_dir("North-west to West,"), "NW–W")
        self.assertEqual(self.m.short_dir("Light and variable"), "Light and variable")
        self.assertEqual(
            [self.m.ordinal(n) for n in (1, 2, 3, 4, 11, 12, 21, 22, 23)],
            ["st", "nd", "rd", "th", "th", "th", "st", "nd", "rd"],
        )

    def test_pdf_text_passthrough_and_missing_tool(self):
        self.assertEqual(self.m.pdf_text(b"plain text"), "plain text")
        self.m.shutil.which = lambda name: None
        with self.assertRaises(self.m.SourceError):
            self.m.pdf_text(b"%PDF-1.4 ...")


if __name__ == "__main__":
    unittest.main(verbosity=1)
