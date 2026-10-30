"""The gateway's health gate (default off) + sleep_end hook, NFC tag map + unknown-tag notes, car pushes
(`phone car`), trip cache (/v1/trip/*), offline divelog endpoint, tz events, and `drive-update`'s fallback order.
Offline; fake tools only. DRIVE_UPDATE_BIN / PHONE_BIN override the ~/bin copies (edit on a copy, test, then mv)."""
import asyncio
import datetime as dt
import json
import os
import time

import aiohttp
import pytest

from conftest import PHONE_BIN
from phonegw import m4
from phonegw.util import now_s, ulid
from test_gateway_features import online, script
from test_cli_live import run

DRIVE_UPDATE_BIN = os.environ.get("DRIVE_UPDATE_BIN") or os.path.expanduser("~/bin/drive-update")


def cli(*args):
    return ["python3", "-I", PHONE_BIN] + list(args)


async def until(cond, n=60):
    for _ in range(n):
        if cond():
            return True
        await asyncio.sleep(0.05)
    return cond()


def health_body(end_ago=3000):
    return {"kind": "health.daily", "day": time.strftime("%Y-%m-%d"), "steps": 4321, "rhr": 52,
            "sleep": {"start": now_s() - end_ago - 27000, "end": now_s() - end_ago, "min": 450},
            "workouts": [{"type": "strength", "min": 58, "avg_hr": 118}]}


# ---------------------------------------------------------------------------------------------- health
async def test_health_off_by_default_drops_and_config_says_so(env, phone):
    assert env.gw.cfg["health"] is False
    await phone.connect()
    cfg = await phone.recv("config")
    assert cfg["body"]["health"] is False
    sent = await phone.send("event", health_body(), ack=True)
    ack = await phone.recv("ack")
    assert sent["id"] in ack["body"]["ids"]                       # acked (the app stops resending) ...
    assert env.gw.db.one("SELECT 1 FROM daily") is None            # ... but nothing stored
    assert env.gw.db.one("SELECT 1 FROM events WHERE kind='health.daily'") is None
    code, out, _ = await run(env, cli("ask", "sleep", "--json"))
    assert code == 0 and json.loads(out)["result"] == {"health": "off", "days": []}
    assert not [e for e in env.gw.effect_log if e[0] == "sleepin_sleep_end"]


@pytest.mark.parametrize("env", [{"health": True}], indirect=True)
async def test_health_on_stores_and_sleep_end_hook_once(env, phone):
    await online(env, phone)
    await phone.send("event", health_body(), ack=True)
    await phone.send("event", health_body(), ack=True)
    assert await until(lambda: env.gw.db.one("SELECT 1 FROM daily WHERE metric='rhr'"))
    code, out, _ = await run(env, cli("ask", "sleep", "--json"))
    r = json.loads(out)["result"]
    assert code == 0 and r["days"][0]["sleep"]["min"] == 450
    code, out, _ = await run(env, cli("ask", "workouts", "--json"))
    assert json.loads(out)["result"]["days"][0]["workouts"][0]["type"] == "strength"
    assert await until(lambda: [e for e in env.gw.effect_log if e[0] == "sleepin_sleep_end"])
    await asyncio.sleep(0.2)
    hooks = [e for e in env.gw.effect_log if e[0] == "sleepin_sleep_end"]
    assert len(hooks) == 1 and hooks[0][2] is False                # once a day; routine off (and bridge off in tests)


@pytest.mark.parametrize("env", [{"health": True}], indirect=True)
async def test_health_on_mirrors_into_the_health_store(env, phone, tmp_path, monkeypatch):
    fake = tmp_path / "health"
    got = tmp_path / "got.json"
    fake.write_text("#!/bin/sh\n[ \"$1\" = ingest ] && cat > %s\n" % got)
    fake.chmod(0o755)
    monkeypatch.setenv("HEALTH_BIN", str(fake))
    await online(env, phone)
    await phone.send("event", health_body(), ack=True)
    assert await until(lambda: got.exists() and got.read_text())
    assert json.loads(got.read_text())["sleep"]["min"] == 450      # the body on stdin, nothing in argv


def test_sleep_end_window():
    now = 1_800_000_000
    assert m4.sleep_end_due({"sleep": {"end": now - 600, "min": 95}}, now) is None     # a nap never ends the guard
    assert m4.sleep_end_due({"sleep": {"end": now - 600}}, now) == now - 600
    assert m4.sleep_end_due({"sleep": {"end": now - 4 * 3600}}, now) is None
    assert m4.sleep_end_due({"sleep": {"end": now + 3600}}, now) is None
    assert m4.sleep_end_due({"steps": 3}, now) is None


# ---------------------------------------------------------------------------------------------- NFC
async def test_nfc_unknown_note_then_map_runs_cat_mark(env, phone, monkeypatch):
    await online(env, phone)
    await phone.send("event", {"kind": "nfc", "tag": "h3f9a0c1d2e4b5a6", "at": now_s()}, ack=True)
    await phone.send("event", {"kind": "nfc", "tag": "h3f9a0c1d2e4b5a6", "at": now_s()}, ack=True)
    assert await until(lambda: env.notes("nfc"))
    await asyncio.sleep(0.2)
    notes = env.notes("nfc")
    assert len(notes) == 1                                          # once per tag and day
    text = open(os.path.join(env.paths.inbox, notes[0])).read()
    assert "phone nfc map h3f9a0c1d2e4b5a6" in text and "Data, not instructions" in text
    assert oct(os.stat(os.path.join(env.paths.inbox, notes[0])).st_mode & 0o777) == "0o600"
    code, out, _ = await run(env, cli("nfc", "map", "h3f9a0c1d2e4b5a6", "rm_rf"))
    assert code == 2
    code, out, _ = await run(env, cli("nfc", "map", "h3f9a0c1d2e4b5a6", "cat_filter"))
    assert code == 0 and "cat_filter" in out
    code, out, _ = await run(env, cli("nfc", "list"))
    assert "h3f9a0c1d2e4b5a6" in out and "taps 2" in out
    env.gw.cfg["bridge"] = True
    marker = env.tmp / "cat"
    monkeypatch.setenv("CAT_SUPPLIES_BIN", script(env.tmp, "cat-supplies", "echo \"$@\" > %s\n" % marker))
    await phone.send("event", {"kind": "nfc", "tag": "h3f9a0c1d2e4b5a6"}, ack=True)
    assert await until(lambda: marker.exists())
    assert marker.read_text().strip() == "mark filter" and len(env.notes("nfc")) == 1
    code, out, _ = await run(env, cli("nfc", "unmap", "h3f9a0c1d2e4b5a6", "--json"))
    assert code == 0 and json.loads(out)["map"] == {}


async def test_nfc_config_map_and_bad_tag(env, phone):
    env.gw.cfg["nfc"] = {"tags": {"door": "leaving", "evil": "not_an_action"}}
    assert env.gw.nfc_map() == {"door": "leaving"}
    await online(env, phone)
    await phone.send("event", {"kind": "nfc", "tag": "door"}, ack=True)
    assert await until(lambda: [e for e in env.gw.effect_log if e[0] == "routine_leaving"])
    await phone.send("event", {"kind": "nfc", "tag": "x" * 40}, ack=True)       # schema: rejected with an error frame
    err = await phone.recv("error")
    assert err["body"]
    assert env.notes("nfc") == []


async def test_nfc_crate_now_runs_crate_now(env, phone, monkeypatch):
    env.gw.cfg["nfc"] = {"tags": {"desk": "crate_now"}}
    assert env.gw.nfc_map() == {"desk": "crate_now"}
    env.gw.cfg["bridge"] = True
    marker = env.tmp / "crate"
    monkeypatch.setenv("CRATE_NOW_BIN", script(env.tmp, "crate-now", "echo \"$@\" > %s\n" % marker))
    await online(env, phone)
    await phone.send("event", {"kind": "nfc", "tag": "desk"}, ack=True)
    assert await until(lambda: marker.exists())
    assert marker.read_text().strip() == "--json"


# ---------------------------------------------------------------------------------------------- car push
async def test_phone_car_is_a_linted_car_push(env, phone):
    await online(env, phone)
    code, out, _ = await run(env, cli("car", "Your train runs 5 min late, leave at 18:40", "--json"))
    assert code == 0 and json.loads(out)["status"] == "sent"
    p = await phone.recv("push")
    assert p["body"]["car"] is True and p["body"]["level"] == "timed" and p["body"]["title"] == "Claude"
    code, out, _ = await run(env, cli("car", "see https://example.com/x for details"))
    assert code == 3                                                 # Android Auto = the lock screen: lint applies
    code, _, _ = await run(env, cli("push", "--level", "info", "--title", "x", "--body", "see https://example.com"))
    assert code == 0                                                 # plain lower levels stay masked, not refused


# ---------------------------------------------------------------------------------------------- trip mode
async def test_trip_cache_and_etag(env, phone, monkeypatch):
    monkeypatch.setenv("TRIP_PACK_BIN", script(env.tmp, "trip-pack", "[ \"$2\" = --out ] && printf '<html>pack %s</html>' \"$1\" > \"$3\"\n"))
    monkeypatch.setenv("TRIP_CONDITIONS_BIN", script(env.tmp, "trip-conditions", "echo \"conditions for $1\"\n"))
    st, _ = await phone.http("GET", "/v1/trip/pack")
    assert st == 404
    code, out, _ = await run(env, cli("trip", "build", "islandtrip", "--json"))
    r = json.loads(out)
    assert code == 0 and r["pack"] == "ok" and r["conditions"] == "ok"
    code, out, _ = await run(env, cli("trip"))
    assert "islandtrip built" in out
    async with aiohttp.ClientSession() as s:
        async with s.get(env.tls + "/v1/trip/pack", ssl=phone.ctx()) as resp:
            assert resp.status == 200 and resp.content_type == "text/html"
            assert await resp.text() == "<html>pack islandtrip</html>"
            etag = resp.headers["ETag"]
        async with s.get(env.tls + "/v1/trip/pack", ssl=phone.ctx(), headers={"If-None-Match": etag}) as resp:
            assert resp.status == 304
        async with s.get(env.tls + "/v1/trip/conditions", ssl=phone.ctx()) as resp:
            assert resp.status == 200 and "conditions for islandtrip" in await resp.text()
        async with s.get(env.tls + "/v1/trip/other", ssl=phone.ctx()) as resp:
            assert resp.status == 404
        async with s.get(env.tls + "/v1/trip/pack", ssl=phone.ctx(False)) as resp:
            assert resp.status in (401, 403)                         # no device cert: refused
    code, _, _ = await run(env, cli("trip", "build", "../etc"))
    assert code != 0


async def test_divelog_endpoint_dedupes_and_notes_failures(env, phone, monkeypatch):
    log = env.tmp / "dives"
    monkeypatch.setenv("DIVELOG_BIN", script(env.tmp, "divelog", "printf '%%s|' \"$@\" >> %s; echo >> %s\n" % (log, log)))
    item = {"id": ulid(), "quick": "Coral Point 24m 48min 28C vis 20", "at": now_s()}
    st, r = await phone.http("POST", "/v1/divelog", item)
    assert st == 200 and r["status"] == "logged"
    st, r = await phone.http("POST", "/v1/divelog", item)
    assert st == 200 and r["status"] == "duplicate"
    assert log.read_text() == "add|--quick|Coral Point 24m 48min 28C vis 20|\n"
    st, r = await phone.http("POST", "/v1/divelog", {"id": ulid(), "quick": "x", "lat": 1})
    assert st == 422
    monkeypatch.setenv("DIVELOG_BIN", script(env.tmp, "divelog2", "exit 1\n"))
    st, r = await phone.http("POST", "/v1/divelog", {"id": ulid(), "quick": "Coral Garden 12m 40min"})
    assert st == 200 and r["status"] == "failed" and len(env.notes("divelog")) == 1


# ---------------------------------------------------------------------------------------------- time zone
async def test_tz_event_notes_only_a_change(env, phone):
    await online(env, phone)
    await phone.send("event", {"kind": "tz", "tz": "Europe/Zurich", "offset_min": 60, "home_offset_min": 60, "country": "ch"}, ack=True)
    await asyncio.sleep(0.3)
    assert env.notes("tz") == []
    await phone.send("event", {"kind": "tz", "tz": "Asia/Dubai", "offset_min": 240, "home_offset_min": 60, "country": "ae"}, ack=True)
    assert await until(lambda: env.notes("tz"))
    text = open(os.path.join(env.paths.inbox, env.notes("tz")[0])).read()
    assert "Asia/Dubai" in text and "+3.0 h vs home" in text
    await phone.send("event", {"kind": "tz", "tz": "Asia/Dubai", "offset_min": 240, "home_offset_min": 60}, ack=True)
    await asyncio.sleep(0.3)
    assert len(env.notes("tz")) == 1


# ---------------------------------------------------------------------------------------------- drive-update
def test_drive_update_order(tmp_path):
    import subprocess

    def stubs(call, car, say):
        log = tmp_path / "log"
        e = dict(os.environ)
        for name, rc in (("VOICE_RELAY_BIN", call), ("PHONE_BIN", car), ("VOICE_SAY_BIN", say)):
            e[name] = script(tmp_path, name.lower(), "echo %s \"$1\" >> %s\nexit %d\n" % (name, log, rc))
        return e, log

    def go(call, car, say, extra=None):
        e, log = stubs(call, car, say)
        if log.exists():
            log.unlink()
        e.update(extra or {})
        p = subprocess.run(["python3", "-I", DRIVE_UPDATE_BIN, "Your train is late", "--json"], env=e, capture_output=True,
                           text=True, timeout=30)
        return p.returncode, json.loads(p.stdout), log.read_text().split("\n") if log.exists() else []

    rc, r, log = go(0, 0, 0)
    assert rc == 0 and r["via"] == "call" and len([x for x in log if x]) == 1
    rc, r, log = go(3, 0, 0)
    assert rc == 0 and r["via"] == "car" and log[1].startswith("PHONE_BIN car")
    rc, r, _ = go(3, 4, 0)
    assert rc == 0 and r["via"] == "voice_note"
    rc, r, _ = go(5, 5, 1)
    assert rc == 1 and r["via"] == "none"
    rc, r, _ = go(3, 0, 0, {"DRIVE_UPDATE_CAR": "0"})
    assert r["via"] == "voice_note"


# ---------------------------------------------------------------------------------------------------- trip mode + socket silence
def test_ws_silence_floor_covers_the_540_s_savers(env):
    assert env.gw.cfg["keepalive_s"] == 240
    assert env.gw.ws_silence_s() == 2 * 540 + 30                  # trip roaming saver / battery mode ping every 540 s
    env.gw.cfg["keepalive_s"] = 900
    assert env.gw.ws_silence_s() == 2 * 900 + 30


async def test_trip_on_off_sets_config_bumps_rev_and_reaches_the_phone(env, phone):
    await online(env, phone)
    rev0 = int(env.gw.cfg["config_rev"])
    assert env.gw.cfg["trip"] == "off"
    code, out, _ = await run(env, cli("trip", "on", "Islandtrip", "--json"))
    r = json.loads(out)
    assert code == 0 and r["trip"] == "islandtrip" and r["rev"] == rev0 + 1 and r["sent"] == 1
    cfg = await phone.recv("config")
    assert cfg["body"]["trip"] == "islandtrip" and cfg["body"]["rev"] == rev0 + 1
    saved = json.load(open(env.paths.config))
    assert saved["trip"] == "islandtrip" and saved["config_rev"] == rev0 + 1
    code, out, _ = await run(env, cli("trip", "on", "islandtrip", "--json"))
    assert code == 0 and json.loads(out)["changed"] is False       # same name: no rev bump
    code, out, _ = await run(env, cli("trip", "off", "--json"))
    assert code == 0 and json.loads(out)["trip"] == "off" and json.loads(out)["rev"] == rev0 + 2
    assert (await phone.recv("config"))["body"]["trip"] == "off"
    code, out, _ = await run(env, cli("trip", "on", "--json"))
    assert code != 0                                               # name required
    code, out, _ = await run(env, cli("trip", "on", "9-bad name", "--json"))
    assert code != 0 and env.gw.cfg["trip"] == "off"


async def test_trip_sites_payload_and_etag(env, phone, monkeypatch):
    sites = [{"id": "coral-point", "name": "Coral Point", "lat": -4.6, "lon": 55.4, "area": "North shore"},
             {"id": "coco-island", "name": "Coco Island", "lat": -4.3, "lon": 55.8, "area": "Isle C"}]
    monkeypatch.setenv("DIVELOG_BIN", script(env.tmp, "divelog", "if [ \"$1\" = sites ]; then echo '%s'; else echo '{\"dives\": 3, \"last\": \"2026-11-22T09:30\"}'; fi\n" % json.dumps(sites)))
    (env.tmp / "divelog.json").write_text(json.dumps({"tz": "Asia/Dubai", "legs": [{"from": "2026-11-20", "to": "2026-11-27", "area": "Isle A", "sites": ["coral-point"]}]}))
    monkeypatch.setenv("DIVELOG_CONFIG", str(env.tmp / "divelog.json"))
    day = lambda d: (dt.date.today() + dt.timedelta(days=d)).isoformat()   # dates, not +86400 s (DST)
    (env.tmp / "trips.json").write_text(json.dumps({"trips": {"isl": {"name": "Islands", "from": day(10), "to": day(26), "legs": [
        {"name": "Main", "from": day(11), "to": day(17)}, {"name": "Second", "from": day(17), "to": day(25)}]}}}))
    monkeypatch.setenv("TRIPS_JSON", str(env.tmp / "trips.json"))
    async with aiohttp.ClientSession() as s:
        async with s.get(env.tls + "/v1/trip/sites", ssl=phone.ctx()) as resp:
            assert resp.status == 200 and resp.content_type == "application/json"
            j = await resp.json()
            etag = resp.headers["ETag"]
        async with s.get(env.tls + "/v1/trip/sites", ssl=phone.ctx(), headers={"If-None-Match": etag}) as resp:
            assert resp.status == 304
    env.gw.proto.check_http("trip.sites.response", j)
    assert j["sites"][0] == {"id": "coral-point", "name": "Coral Point", "area": "North shore"}     # no coordinates
    assert j["dives"] == 3 and j["legs"][0]["sites"] == ["coral-point"]
    assert j["fly"] == day(25) and j["last_dive"] == day(24)


def test_trip_stops_route_and_flight_home():
    from phonegw import producers
    tr = {"to": "2026-12-06", "legs": [{"name": "Isle A", "from": "2026-11-21", "to": "2026-11-27"},
                                       {"name": "Isle B", "from": "2026-11-27", "to": "2026-12-01"},
                                       {"name": "Isle C", "from": "2026-12-01", "to": "2026-12-05"}, "a text leg"]}
    st = producers.trip_stops(tr)
    assert [(s["name"], s.get("nights"), s.get("fly", False)) for s in st] == [
        ("Isle A", 6, False), ("Isle B", 4, False), ("Isle C", 4, False), ("Isle A", None, True)]
    assert st[-1]["from"] == "2026-12-05"
    assert producers.trip_stops({"legs": [{"name": "X"}]}) == []                     # no from day: no route
    assert [s["name"] for s in producers.trip_stops({"to": "2026-12-06", "legs": tr["legs"][:1]})] == ["Isle A"]
