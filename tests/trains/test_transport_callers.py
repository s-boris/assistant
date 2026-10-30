"""The transport tools with transport.opendata.ch down: night-plan and route answer through the
timetable.search.ch fallback (core.transport). Each tool runs from the repo as a subprocess against a loopback fake:
/v1/* answers opendata's router-outage payload (503), /api/route.json and /api/completion.json the recorded
search.ch fixtures (public stations only). Synthetic profile; no network."""

from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading
import urllib.parse
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
FIX = Path(__file__).with_name("fixtures")
ROUTES = {"Zürich HB": "searchch-route-night-out.json", "Basel SBB": "searchch-route-night-back.json"}


class Fake(http.server.BaseHTTPRequestHandler):
    log: list[str] = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        Fake.log.append(self.path)
        u = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        if u.path.startswith("/v1/"):
            return self.reply(503, (FIX / "opendata-router-error.json").read_bytes())
        if u.path == "/api/route.json":
            name = ROUTES.get(q.get("from", ""))
            body = (FIX / name).read_bytes() if name else (FIX / "searchch-unknown-stop.json").read_bytes()
            return self.reply(200, body)
        if u.path == "/api/completion.json":
            return self.reply(200, (FIX / "searchch-completion-term.json").read_bytes())
        self.reply(404, b"{}")

    def reply(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def fake():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def run(fake, tmp_path):
    prof = tmp_path / "profile.json"
    prof.write_text(
        json.dumps(
            {
                "home": {"town": "Testdorf", "street": "Probeweg 7", "postcode": "1234"},
                "locale": {"tz": "Europe/Zurich"},
            }
        ),
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "ASSISTANT_PROFILE": str(prof),
        "HOME_PLACE_GEO": "off",
        "HOME_PLACE_CACHE": str(tmp_path / "home-place.json"),
        "PYTHONPATH": os.pathsep.join([str(REPO / "scripts" / "lib" / "legacy"), str(REPO)]),
        "ROUTE_API": f"{fake}/v1",
        "NIGHT_PLAN_API": f"{fake}/v1",
        "GOHOME_API": f"{fake}/v1",
        "TRANSPORT_FALLBACK_API": f"{fake}/api",
        "NIGHT_PLAN_CONFIG": str(tmp_path / "none.json"),
        "NIGHT_PLAN_VENUES": str(tmp_path / "none.json"),
        "RAVE_RADAR_STATE_DIR": str(tmp_path / "rr"),
        "no_proxy": "127.0.0.1",
        "NO_PROXY": "127.0.0.1",
    }
    Fake.log.clear()

    def go(tool, *args, extra=None):
        p = subprocess.run(
            [sys.executable, str(REPO / "scripts" / tool), *args],
            env={**env, **(extra or {})},
            capture_output=True,
            text=True,
            timeout=60,
        )
        return p.returncode, p.stdout, p.stderr

    return go


def test_route_through_searchch(run):
    code, out, err = run("route", "Basel SBB", "Zürich HB", "--date", "2026-11-07", "--at", "23:00", "--literal")
    assert code == 0, err
    first = out.split("\n\n")[0].splitlines()
    assert first[0] == "23:07 → 00:00  (00:53, 0 transfers)"
    assert first[1] == "   23:07 Basel SBB [pl 6] → 00:00 Zürich HB  IC3 → Zürich HB"
    assert [c.splitlines()[0][:13] for c in out.strip().split("\n\n")] == [
        "23:07 → 00:00",
        "23:16 → 00:40",
        "00:04 → 01:06",
        "00:07 → 01:24",
    ]
    hits = [p for p in Fake.log if p.startswith("/api/route.json")]
    assert Fake.log[0].startswith("/v1/connections") and len(hits) == 1
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(hits[0]).query))
    assert (q["date"], q["time"], q["num"], q["pre"]) == ("07.11.2026", "23:00", "4", "2")


def test_route_arrive_through_searchch(run):
    code, out, err = run(
        "route", "Zürich HB", "Basel SBB", "--date", "2026-11-07", "--arrive", "23:45", "--literal", "-n", "2"
    )
    assert code == 0, err
    assert [c.splitlines()[0][:13] for c in out.strip().split("\n\n")] == ["22:10 → 23:23", "22:32 → 23:36"]


def test_route_both_down_is_one_line(run, fake):
    code, out, err = run("route", "Nowhere", "Zürich HB", "--date", "2026-11-07", "--at", "23:00", "--literal")
    assert code == 1 and out == ""
    assert err.strip().startswith("route: transport API error: The router is not reachable")
    assert "; fallback search.ch: transport API error: Haltestelle" in err and "Traceback" not in err


def test_night_plan_through_searchch(run):
    code, out, err = run(
        "night-plan",
        "--venue",
        "Testclub, Basel",
        "--date",
        "2026-11-07",
        "--start",
        "22:30",
        "--stop",
        "Basel SBB",
        "--home",
        "Zürich HB",
        "--json",
    )
    assert code == 0, err
    plan = json.loads(out)
    assert "Traceback" not in err
    text = json.dumps(plan, ensure_ascii=False)
    assert "IR 37" in text or "IR37" in text or "IC 3" in text or "IC3" in text
    assert plan.get("calendar")
    paths = [urllib.parse.urlsplit(p).path for p in Fake.log]
    assert "/api/route.json" in paths and "/api/completion.json" in paths
    assert paths.count("/v1/connections") == 1 and paths.count("/v1/locations") == 1  # opendata asked once each
