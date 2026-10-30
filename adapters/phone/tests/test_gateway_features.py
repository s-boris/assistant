"""Privacy-class enforcement, now view (= the vault's key set), flags ingestion/effects, ask round trip, widgets,
cards + decisions notes, geofences, alarms, the Leaving routine, phone-offline notes. Offline; fake tools only."""
import asyncio
import datetime as dt
import json
import os
import stat
import time

import pytest

from conftest import PROTOCOL
from phonegw import now as N
from phonegw.util import now_s, ulid

SHAPE = json.load(open(os.path.join(PROTOCOL, "fixtures/vault/ha-now.shape.json")))


def script(tmp, name, body):
    p = tmp / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return str(p)


def nested_keys(shape):
    return {k: (set(v) if isinstance(v, dict) else None) for k, v in shape.items()}


async def online(env, phone):
    await phone.connect()
    await phone.recv("config")
    return phone


STATE = {"battery": {"level": 63, "band": "ok", "charging": False, "charger": "none"},
         "net": {"type": "wifi", "vpn": True, "roaming": False, "country": "ch"}, "dnd": "off",
         "screen": {"on": False, "last_unlock": None}, "alarm": {"next": None}, "car": {"projection": True, "bt": True},
         "bt": ["car", "watch"], "place": {"zone": "office", "band": "region", "town": "Testtown", "since": 1792500000,
                                           "fix_age_min": 3},
         "activity": {"kind": "vehicle", "since": 1792505000}}


# ---------------------------------------------------------------------------------------------- privacy
@pytest.mark.parametrize("body", [
    {"place": {"zone": "home", "town": "46.9480, 7.4474"}},
    {"place": {"zone": "home", "stop": "aa:bb:cc:dd:ee:ff"}},
    {"place": {"zone": "home", "town": "https://maps.example/x"}},
    {"place": {"zone": "home", "town": "Call 0791234567"}},
])
async def test_state_smuggling_raw_values_is_rejected(env, phone, body):
    st, j = await phone.http("POST", "/v1/events", {"items": [{"v": 1, "id": ulid(), "t": "state", "ts": now_s(), "body": body}]})
    assert st == 422 and j["error"] == "schema"
    assert "46.9480" not in json.dumps(j) and "0791234567" not in json.dumps(j)


@pytest.mark.parametrize("t,body", [
    ("event", {"kind": "zone", "zone": "gym", "dir": "enter", "lat": 46.1}),
    ("event", {"kind": "health.daily", "day": "2026-10-20", "samples": [60, 61]}),
    ("event", {"kind": "health.daily", "day": "2026-10-20", "sleep": {"min": 400, "text": "x"}}),
    ("answer", {"q_id": "q_01ABC", "ok": True, "result": {"inside": True, "lat": 46.1, "lon": 7.1}}),
    ("answer", {"q_id": "q_01ABC", "ok": True, "result": {"zone": "home", "text": "near the station"}}),
    ("state", {"notification": {"app": "x"}}),
])
async def test_raw_keys_rejected_everywhere(env, phone, t, body):
    st, j = await phone.http("POST", "/v1/events", {"items": [{"v": 1, "id": ulid(), "t": t, "ts": now_s(), "body": body}]})
    assert st == 422, (t, body)


async def test_ws_rejects_raw_state_with_error_frame(env, phone):
    await online(env, phone)
    sent = await phone.send("state", {"place": {"zone": "home", "town": "46.9480,7.4474"}})
    err = await phone.recv("error")
    assert err["body"]["ref"] == sent["id"] and "46.9480" not in json.dumps(err)
    assert env.gw.db.get_state(phone.device_id, "place")[0] is None


@pytest.mark.parametrize("town,stop", [("Hauptstrasse 5", "Hauptstrasse 5, Neustadt"), ("Hauptstrasse", None),
                                       ("8000 Bigcity", None), ("Rue du Lac 3", "8000 Bigcity, Post")])
async def test_street_like_place_names_are_blanked_not_stored(env, phone, town, stop, caplog):
    body = {"place": {"zone": "home", "band": "near", "town": town, "stop": stop, "since": now_s(), "fix_age_min": 0},
            "battery": {"level": 80, "charging": False}}
    st, j = await phone.http("POST", "/v1/events", {"items": [{"v": 1, "id": ulid(), "t": "state", "ts": now_s(), "body": body}]})
    assert st == 200, j
    place = env.gw.db.get_state(phone.device_id, "place")[0]
    assert place["zone"] == "home" and place["town"] is None and place["stop"] is None
    assert env.gw.db.get_state(phone.device_id, "battery")[0]["level"] == 80        # the rest of the frame lands
    assert "Hauptstrasse" not in caplog.text and "Rue du Lac" not in caplog.text and "8000" not in caplog.text


async def test_locality_and_public_stop_names_pass(env, phone):
    body = {"place": {"zone": "away", "band": "far", "town": "St. Gallen", "stop": "Bern, Guisanplatz Expo", "since": now_s()}}
    st, j = await phone.http("POST", "/v1/events", {"items": [{"v": 1, "id": ulid(), "t": "state", "ts": now_s(), "body": body}]})
    assert st == 200, j
    place = env.gw.db.get_state(phone.device_id, "place")[0]
    assert place["town"] == "St. Gallen" and place["stop"] == "Bern, Guisanplatz Expo"


def test_privacy_place_rules():
    from phonegw import privacy
    for ok in ("Zürich", "Bern", "St. Gallen", "Rapperswil-Jona", "Wettswil am Albis", "Genève", "Biel/Bienne"):
        assert not privacy.street_like(ok), ok
    for bad in ("Bahnhofstrasse 1", "Bahnhofstrasse", "Seestr. 12", "Marktgasse", "Schulweg", "Bundesplatz", "Rue du Rhône 8",
                "Via Nassa", "Chemin des Vignes", "8001 Zürich", "Bahnhofstraße", "Zürich, Bahnhofstrasse"):
        assert privacy.street_like(bad), bad
    assert privacy.address_like("Bahnhofplatz 1, Zürich") and not privacy.address_like("Zürich, Bahnhofstrasse/HB")
    b = {"place": {"town": "Hauptstrasse 5", "stop": "Hauptstrasse 5, X"}, "x": {"stop": "Line 5"}}
    assert privacy.scrub_places(b) == 2 and b["place"] == {"town": None, "stop": None} and b["x"]["stop"] == "Line 5"


# ---------------------------------------------------------------------------------------------- now view
async def test_now_has_exactly_the_vault_keys(env, phone):
    st, j = await phone.http("POST", "/v1/events", {"items": [{"v": 1, "id": ulid(), "t": "state", "ts": now_s(), "body": STATE}]})
    assert st == 200, j
    st, v = await env.call("GET", "/internal/now")
    assert st == 200
    assert set(v) - set(SHAPE["phone_extras"]) == set(SHAPE["shape"])
    assert set(SHAPE["phone_extras"]) <= set(v)
    for k, sub in nested_keys(SHAPE["shape"]).items():
        if sub and isinstance(v[k], dict):
            assert set(v[k]) == sub, k
    assert v["source"] == "phone" and v["place"]["kind"] == "zone" and v["place"]["zone"] == "Work"
    assert v["place"]["flag"] == "away" and v["place"]["band"] == "region" and v["place"]["town"] == "Testtown"
    assert v["battery"] == {"level": 65, "band": "high", "charging": False, "charger": "none"}
    assert v["drive"]["active"] is True and v["activity"] == "vehicle" and v["wifi"] == "work"
    assert v["dnd"] is False and v["vpn"] is True and v["screen"] == "off" and v["age_min"] == 0
    assert v["connection"]["feed"] == "phone"


async def test_now_without_device_keeps_the_key_set(env):
    st, v = await env.call("GET", "/internal/now")
    assert st == 404 and set(v) - set(SHAPE["phone_extras"]) == set(SHAPE["shape"])
    assert v["connection"]["state"] == "no_device" and v["place"] is None


def test_now_home_has_no_band_or_town():
    p = N.place_view({"zone": "home", "band": "home", "town": "X", "since": 1, "fix_age_min": 2}, 100, 100 + 600, {}, N.tz_of({}))
    assert p["kind"] == "home" and p["band"] is None and p["town"] is None and p["flag"] == "home" and p["fix_age_min"] == 12
    far = N.place_view({"zone": "away", "band": "far", "town": "Y"}, 0, 0, {}, N.tz_of({}))
    assert far["kind"] == "away" and far["zone"] is None and far["flag"] == "far"
    assert N.zone_name("venue:bern-expo", {}) == "Venue Bern Expo"
    assert N.zone_name("office", {"zone_names": {"office": "Büro"}}) == "Büro"


def test_fresh_rule():
    assert N.fresh({"age_min": 5, "place": {"kind": "home"}})
    assert not N.fresh({"age_min": 45, "place": {"kind": "home"}})
    assert not N.fresh({"age_min": 1, "place": None})


# ---------------------------------------------------------------------------------------------- flags + effects
@pytest.mark.parametrize("env", [{"health": True}], indirect=True)     # health summaries are opt-in
async def test_events_drive_flags_and_effects(env, phone):
    await online(env, phone)
    await phone.send("event", {"kind": "zone", "zone": "gym", "dir": "enter", "at": now_s()}, ack=True)
    await phone.send("event", {"kind": "tile", "name": "leaving"}, ack=True)
    await phone.send("event", {"kind": "awake", "how": "interaction", "at": now_s()}, ack=True)
    await phone.send("event", {"kind": "awake", "how": "unlocks", "at": now_s()}, ack=True)
    await phone.send("event", {"kind": "health.daily", "day": time.strftime("%Y-%m-%d"), "steps": 4321,
                               "sleep": {"start": now_s() - 30000, "end": now_s() - 3000, "min": 415}}, ack=True)
    await phone.recv("ack")
    for _ in range(50):
        if len(env.gw.effect_log) >= 3 and env.gw.db.one("SELECT 1 FROM daily WHERE metric='steps'"):
            break
        await asyncio.sleep(0.05)
    names = [e[0] for e in env.gw.effect_log]
    assert names.count("remind_place") == 1 and "routine_leaving" in names and names.count("sleepin_release") == 1
    assert all(ran is False for _, _, ran in env.gw.effect_log)        # bridge off in tests
    st, v = await env.call("GET", "/internal/now")
    assert v["place"]["zone"] == "Gym" and v["sleep"] == "awake" and v["steps_today"] == 4321
    assert v["last_sleep"]["hours"] == 7.0


async def test_routines_gate_tools(env, phone, monkeypatch):
    env.gw.cfg["bridge"] = True
    env.gw.cfg["routines"] = {"remind_place": False, "routine_leaving": True}
    marker = env.tmp / "ran"
    monkeypatch.setenv("PHONE_ROUTINE_BIN", script(env.tmp, "routine", "echo \"$@\" > %s\n" % marker))
    monkeypatch.setenv("REMIND_BIN", script(env.tmp, "remind", "echo bad > %s.remind\n" % marker))
    await online(env, phone)
    await phone.send("event", {"kind": "zone", "zone": "home", "dir": "enter"}, ack=True)
    await phone.send("event", {"kind": "tile", "name": "leaving"}, ack=True)
    for _ in range(60):
        if marker.exists():
            break
        await asyncio.sleep(0.05)
    assert marker.read_text().strip() == "leaving" and not os.path.exists(str(marker) + ".remind")


# ---------------------------------------------------------------------------------------------- ask
async def test_ask_fresh_round_trip_and_cache(env, phone):
    await online(env, phone)
    await phone.send("state", STATE)

    async def answer():
        a = await phone.recv("ask")
        assert a["body"]["q"] == "battery" and a["ack"] is False
        await phone.send("state", {"battery": {"level": 22, "charging": False}})
        await phone.send("answer", {"q_id": a["body"]["q_id"], "ok": True, "result": {"fresh": True}})

    t = asyncio.ensure_future(answer())
    st, j = await env.call("POST", "/internal/ask", {"q": "battery", "fresh": True, "timeout_s": 5})
    await t
    assert j["source"] == "phone" and j["result"]["level"] == 20 and j["result"]["band"] == "mid"
    st, j = await env.call("POST", "/internal/ask", {"q": "car"})
    assert j["source"] == "cache" and j["result"]["drive"]["active"] is True


async def test_ask_near_and_timeout_and_offline(env, phone):
    st, j = await env.call("POST", "/internal/ask", {"q": "near", "args": {"place": "venue:expo"}})
    assert j["status"] == "offline"
    await online(env, phone)

    async def answer():
        a = await phone.recv("ask")
        assert a["body"]["args"] == {"place": "venue:expo"}
        await phone.send("answer", {"q_id": a["body"]["q_id"], "ok": True, "result": {"inside": True, "band": "near"}})

    t = asyncio.ensure_future(answer())
    st, j = await env.call("POST", "/internal/ask", {"q": "near", "args": {"place": "venue:expo"}, "timeout_s": 5})
    await t
    assert j["ok"] and j["result"] == {"inside": True, "band": "near"}
    st, j = await env.call("POST", "/internal/ask", {"q": "near", "args": {"place": "gym"}, "timeout_s": 1})
    assert j["status"] == "timeout"
    st, j = await env.call("POST", "/internal/ask", {"q": "where", "fresh": True, "timeout_s": 1})
    assert j["ok"] and j["fresh_failed"] == "timeout"
    st, j = await env.call("POST", "/internal/ask", {"q": "bogus"})
    assert st == 400


# ---------------------------------------------------------------------------------------------- widgets
BOARD = {"state": "idle", "agents": 0, "due": 1, "waiting": 2, "items": [{"sec": "due", "title": "Test item", "when": "19:35"}]}


async def test_widget_push_coalesce_and_schema(env, phone):
    st, j = await env.call("POST", "/internal/widget", {"kind": "board", "data": BOARD})
    assert j["results"][0]["status"] == "queued"          # paired, not connected: the outbox keeps the latest
    await online(env, phone)
    w = await phone.recv("widget")                        # the queued one, replayed on connect
    assert w["body"]["data"]["due"] == 1
    st, j = await env.call("POST", "/internal/widget", {"kind": "board", "data": dict(BOARD, due=2)})
    w = await phone.recv("widget")
    assert w["key"] == "widget:board" and w["body"]["data"]["due"] == 2 and j["results"][0]["status"] == "sent"
    st, j = await env.call("POST", "/internal/widget", {"kind": "board", "data": dict(BOARD, due=2, updated=5), "changed_only": True})
    assert j["results"][0]["status"] == "unchanged"
    st, j = await env.call("POST", "/internal/widget", {"kind": "board", "data": dict(BOARD, secret=1)})
    assert j["results"][0]["status"] == "schema"


async def test_widgets_from_tools(env, phone, monkeypatch):
    todo = env.tmp / "TODO.md"
    todo.write_text("# TODO\n\n## In progress\n- **Build the widget** — running\n\n## Waiting on you\n"
                    "- **Pick a ferry** — A or B?\n- **Second thing** — x\n\n## Due\n- **Call the vet** — 10:00 01.01\n")
    monkeypatch.setenv("TG_STATUS_TODO", str(todo))
    monkeypatch.setenv("TG_STATUS_DIR", str(env.tmp / "tgs"))
    dep = time.strftime("%Y-%m-%dT%H:%M:00", time.localtime(now_s() + 3600))
    arr = time.strftime("%Y-%m-%dT%H:%M:00", time.localtime(now_s() + 7200))
    legs = [{"id": "abc123", "from": "Neustadt", "to": "home", "dep": dep, "arr": arr, "lines": ["S9"], "title": "🚆 Home"}]
    monkeypatch.setenv("TRAIN_WATCH_BIN", script(env.tmp, "tw", "if [ \"$1\" = list ]; then echo '%s'; else echo "
                       "'{\"live\": {\"platform\": \"41\", \"delay\": 4, \"cancelled\": false, \"lines\": [\"S9\"]}}'; fi\n"
                       % json.dumps(legs)))
    trips = {"trips": {"isl": {"name": "Islands", "emoji": "🏝", "from": (dt.date.today() + dt.timedelta(days=10)).isoformat(),
                               "to": (dt.date.today() + dt.timedelta(days=20)).isoformat(),
                               "legs": [{"name": "Main island", "from": (dt.date.today() + dt.timedelta(days=10)).isoformat()}]}}}
    (env.tmp / "trips.json").write_text(json.dumps(trips))
    monkeypatch.setenv("TRIPS_JSON", str(env.tmp / "trips.json"))
    await online(env, phone)
    st, j = await env.call("POST", "/internal/widget", {"kind": "all"})
    assert j["ok"], j
    got = {}
    for _ in range(3):
        w = await phone.recv("widget")
        got[w["body"]["kind"]] = w["body"]["data"]
    assert got["board"]["waiting"] == 2 and got["board"]["progress"] == 1
    assert got["leg"]["line"] == "S9" and got["leg"]["platform"] == "41" and got["leg"]["status"] == "delayed"
    assert got["leg"]["home_eta"] and got["leg"]["delay_min"] == 4
    assert got["trip"]["phase"] == "before" and got["trip"]["days_left"] == 10 and got["trip"]["next"] == "Main island"


# ---------------------------------------------------------------------------------------------- cards + decisions
async def test_decision_card_answer_writes_one_note(env, phone, monkeypatch):
    await online(env, phone)
    deck = {"items": [{"id": "ab12", "title": "Ferry or plane", "say": "Ferry or plane?", "options": ["ferry", "plane"]},
                      {"id": "cd34", "title": "Keep the watch", "say": "Keep or drop?", "options": ["keep", "drop"]},
                      {"id": "ef56", "title": "One option only", "say": "x", "options": ["only"]}]}
    monkeypatch.setenv("DECISION_DECK_BIN", script(env.tmp, "deck", "echo '%s'\n" % json.dumps(deck)))
    st, j = await env.call("POST", "/internal/card", {"deck": True})
    assert j["cards"] == ["c_deck_ab12", "c_deck_cd34"]
    c1 = await phone.recv("card")
    c2 = await phone.recv("card")
    assert c1["body"]["options"][1] == {"id": "o2", "label": "plane"} and c2["body"]["group"] == "deck"
    await phone.send("card.answer", {"card_id": "c_deck_ab12", "option": "o2", "at": now_s()}, ack=True)
    await phone.send("card.answer", {"card_id": "c_deck_cd34", "option": "o1", "at": now_s()}, ack=True)
    await phone.send("card.answer", {"card_id": "c_deck_cd34", "option": "o2", "at": now_s()}, ack=True)  # only the first counts
    for _ in range(40):
        if env.gw.db.one("SELECT 1 FROM cards WHERE id='c_deck_cd34' AND answer IS NOT NULL"):
            break
        await asyncio.sleep(0.05)
    notes = env.notes("decisions")
    assert len(notes) == 1
    text = open(os.path.join(env.paths.inbox, notes[0])).read()
    assert "Ferry or plane → **plane**" in text and "Keep the watch → **keep**" in text and "drop" not in text
    st, j = await env.call("GET", "/internal/cards")
    assert {c["card_id"]: c["answer_label"] for c in j["cards"]}["c_deck_ab12"] == "plane"
    st, j = await env.call("POST", "/internal/card/remove", {"card_id": "c_deck_ab12"})
    rm = await phone.recv("card.remove")
    assert rm["body"]["card_id"] == "c_deck_ab12"


async def test_deck_ids_with_hyphens_are_answerable_and_late_picks_say_so(env, phone, monkeypatch):
    """decision-deck ids are slugs ("island-stays"): the pick must pass c2s.card.answer (it was refused and stuck
    in the app's outbox), and a pick that arrives > 10 min after his tap says when he made it."""
    await online(env, phone)
    deck = {"items": [{"id": "island-stays", "title": "Island nights", "say": "6/4/4 or 7/4/3?", "options": ["6/4/4", "7/4/3"]}]}
    monkeypatch.setenv("DECISION_DECK_BIN", script(env.tmp, "deck", "echo '%s'\n" % json.dumps(deck)))
    st, j = await env.call("POST", "/internal/card", {"deck": True})
    assert j["cards"] == ["c_deck_island-stays"]
    await phone.recv("card")
    picked = now_s() - 9 * 3600
    await phone.send("card.answer", {"card_id": "c_deck_island-stays", "option": "o2", "at": picked}, ack=True)
    for _ in range(40):
        if env.notes("decisions"):
            break
        await asyncio.sleep(0.05)
    text = open(os.path.join(env.paths.inbox, env.notes("decisions")[0])).read()
    assert "Island nights → **7/4/3** (picked %s, arrived late)" % time.strftime("%d.%m %H:%M", time.localtime(picked)) in text
    assert not env.notes("reject"), "the pick was accepted"


def test_late_pick_text():
    from phonegw.m2 import late_pick
    assert late_pick(1000, now=1000 + 600) == "" and late_pick(None) == "" and late_pick("x") == ""
    assert late_pick(1000, now=1000 + 601).startswith(" (picked ") and late_pick(1000, now=1601).endswith(", arrived late)")


async def test_info_card_and_bad_card(env, phone):
    await online(env, phone)
    st, j = await env.call("POST", "/internal/card", {"kind": "info", "title": "Hello", "text": "Line"})
    assert j["ok"] and (await phone.recv("card"))["body"]["kind"] == "info"
    st, j = await env.call("POST", "/internal/card", {"kind": "decision", "title": "x", "options": [{"id": "BAD ID", "label": "y"}]})
    assert st == 400


# ---------------------------------------------------------------------------------------------- geofences
def geo_env(env, monkeypatch, venue_today=True):
    prof = {"home.lat": "46.5", "home.lon": "7.5", "work.lat": "46.6", "work.lon": "7.6"}
    monkeypatch.setenv("PROFILE_GET_BIN", script(env.tmp, "pg", "case \"$1\" in %s *) exit 2;; esac\n" % " ".join(
        "%s) echo %s;;" % (k, v) for k, v in prof.items())))
    geo = {"near:46.500,7.500": [46.51, 7.51], "Expo Stop": [46.9, 7.4], "Big Airport": [47.4, 8.5]}
    (env.tmp / "geo.json").write_text(json.dumps(geo))
    monkeypatch.setenv("GEO_FAKE", str(env.tmp / "geo.json"))
    venues = {"expo hall": {"stop": "Expo Stop", "aliases": ["mega rave"]}}
    (env.tmp / "venues.json").write_text(json.dumps(venues))
    monkeypatch.setenv("NIGHT_VENUES", str(env.tmp / "venues.json"))
    start = time.strftime("%Y-%m-%dT%H:%M:00", time.localtime(now_s()))     # today, also just before midnight
    end = time.strftime("%Y-%m-%dT%H:%M:00", time.localtime(now_s() + 4 * 3600))
    ev = [{"title": "Mega Rave night", "start": start, "end": end, "all_day": False}] if venue_today else []
    monkeypatch.setenv("CALCACHE_BIN", script(env.tmp, "cc", "echo '%s'\n" % json.dumps(ev)))
    env.gw.cfg["places"] = {"airport": {"query": "Big Airport"}}
    import phonegw.config as C
    cfg = C.load(env.paths)
    cfg["places"] = env.gw.cfg["places"]
    C.save(env.paths, cfg)


async def test_geofences_auto_manual_and_no_coords_out(env, phone, monkeypatch):
    geo_env(env, monkeypatch)
    await online(env, phone)
    st, j = await env.call("POST", "/internal/geofences", {"source": "auto"})
    assert j["ok"] and j["missing"] == ["gym"], j
    g = await phone.recv("geofences")
    names = {f["name"]: f for f in g["body"]["fences"]}
    assert set(names) == {"home", "office", "station", "airport", "venue:expo-hall"}
    assert names["venue:expo-hall"]["until"] > now_s() and names["station"]["lat"] == 46.51
    st, j = await env.call("POST", "/internal/geofences", {"source": "auto"})
    assert j["status"] == "unchanged"
    st, j = await env.call("POST", "/internal/geofences", {"add": {"name": "gym", "kind": "gym", "lat": 46.0, "lon": 7.0, "r_m": 100}})
    assert j["count"] == 6
    await phone.recv("geofences")
    st, j = await env.call("POST", "/internal/geofences", {"source": "auto"})     # manual fences survive a rebuild
    assert j["status"] == "unchanged" and j["count"] == 6
    st, j = await env.call("GET", "/internal/geofences")
    assert "lat" not in json.dumps(j) and "46.5" not in json.dumps(j) and len(j["fences"]) == 6
    st, j = await env.call("POST", "/internal/geofences", {"remove": "gym"})
    assert j["count"] == 5


# ---------------------------------------------------------------------------------------------- alarms
async def test_alarms_from_remind_store(env, phone, monkeypatch):
    def at(sec):
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(now_s() + sec))
    items = [{"id": "1a2b3c4d", "at": at(3600), "owner": True, "text": "⏰ Gym bag"},
             {"id": "2b3c4d5e", "at": at(7200), "owner": True, "text": "Pay 1234567 now"},
             {"id": "3c4d5e6f", "at": at(3 * 86400), "owner": True, "text": "Too far"},
             {"id": "4d5e6f70", "at": at(600), "text": "Mine, not his"},
             {"id": "5e6f7081", "at": at(600), "owner": True, "on": "leave", "text": "Place one"}]
    (env.tmp / "rem.json").write_text(json.dumps(items))
    monkeypatch.setenv("REMIND_DB", str(env.tmp / "rem.json"))
    env.gw.cfg["alarms"] = "on"
    st, j = await env.call("POST", "/internal/alarms", {})
    assert j["status"] == "queued" and j["count"] == 2 and j["mode"] == "on"
    await online(env, phone)
    a = await phone.recv("alarms")
    its = a["body"]["items"]
    assert [i["rid"] for i in its] == ["1a2b3c4d", "2b3c4d5e"]
    assert its[0]["title"] == "Gym bag" and its[0]["private"] is False and "text" not in its[0]
    assert its[1]["title"] == "Reminder" and its[1]["private"] is True and its[1]["text"] == "Pay 1234567 now"
    assert {i["exact"] for i in its} == {"exact"} and not any("backup" in i for i in its)
    st, j = await env.call("POST", "/internal/alarms", {})
    assert j["status"] == "unchanged"


def remind_store(env, monkeypatch, route=None):
    def at(sec):
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(now_s() + sec))
    items = [{"id": "1a2b3c4d", "at": at(3600), "owner": True, "text": "Gym bag"},
             {"id": "2b3c4d5e", "at": at(7200), "owner": True, "call": True, "text": "Leave for the airport"}]
    (env.tmp / "rem.json").write_text(json.dumps(items))
    monkeypatch.setenv("REMIND_DB", str(env.tmp / "rem.json"))
    cfg = env.tmp / "remind-config.json"
    if route:
        cfg.write_text(json.dumps({"route": route}))
    monkeypatch.setenv("REMIND_CONFIG", str(cfg))


async def test_alarms_auto_clears_the_phone_while_remind_routes_to_telegram(env, phone, monkeypatch):
    remind_store(env, monkeypatch)                          # no remind config = route telegram
    await online(env, phone)
    env.gw.cfg["alarms"] = "on"                             # the old behaviour put both on the phone
    st, j = await env.call("POST", "/internal/alarms", {})
    assert j["status"] == "sent" and j["count"] == 2
    assert len((await phone.recv("alarms"))["body"]["items"]) == 2
    env.gw.cfg.pop("alarms")                                # default auto
    assert env.gw.alarms_mode() == "off"
    st, j = await env.call("POST", "/internal/alarms", {})
    assert j["status"] == "sent" and j["count"] == 0 and j["mode"] == "off"
    a = await phone.recv("alarms")
    assert a["body"]["items"] == [] and a["body"]["rev"] == j["rev"]
    st, j = await env.call("POST", "/internal/alarms", {})
    assert j["status"] == "unchanged" and j["count"] == 0
    state, _ = env.gw.db.get_state("_gw", "alarms", {})
    assert state["items"] == [] and state["mode"] == "off"
    (env.tmp / "remind-config.json").write_text("{not json")    # unreadable = telegram, like remind itself
    assert env.gw.alarms_mode() == "off"
    env.gw.cfg["alarms"] = "loud"                           # unknown mode = off
    assert env.gw.alarms_mode() == "off"


async def test_alarms_backup_only_for_apps_with_the_cap(env, phone, monkeypatch):
    remind_store(env, monkeypatch, route="app")
    await online(env, phone)
    assert env.gw.alarms_mode() == "backup"
    st, j = await env.call("POST", "/internal/alarms", {})
    assert j["mode"] == "backup" and j["count"] == 2
    assert (await phone.recv("alarms"))["body"]["items"] == []      # an old app would ring at the due minute
    hello, _ = env.gw.db.get_state(phone.device_id, "hello", {})
    env.gw.db.put_state(phone.device_id, "hello", dict(hello, caps=list(hello.get("caps") or []) + ["alarm_backup"]))
    st, j = await env.call("POST", "/internal/alarms", {"force": True})
    its = (await phone.recv("alarms"))["body"]["items"]
    assert [i["rid"] for i in its] == ["1a2b3c4d", "2b3c4d5e"] and all(i["backup"] is True for i in its)
    assert [i["exact"] for i in its] == ["exact", "alarm_clock"]        # alarm_clock only for remind --call
    (env.tmp / "remind-config.json").write_text(json.dumps({"route": "both"}))
    assert env.gw.alarms_mode() == "backup"


def local_epoch(days, hour):
    import datetime as dt
    from zoneinfo import ZoneInfo
    d = dt.datetime.now(ZoneInfo("Europe/Zurich")).replace(hour=hour, minute=0, second=0, microsecond=0) + dt.timedelta(days=days)
    return int(d.timestamp())


async def test_alarm_vs_departure_problem_only_once(env, phone, monkeypatch):
    now = local_epoch(1, 22)                                   # "tomorrow 22:00"; the leg leaves the next morning
    dep = local_epoch(2, 6)
    monkeypatch.setenv("PRODUCERS_NOW", str(now))
    legs = [{"id": "b1", "from": "Neustadt", "to": "Bigcity", "lines": ["IC 1"], "title": "🚆 Belgium train",
             "dep": time.strftime("%Y-%m-%dT%H:%M:00", time.localtime(dep)), "arr": None}]
    ev = [{"title": "🚗 Office", "start": time.strftime("%Y-%m-%dT%H:%M:00", time.localtime(dep + 3600)), "end": None,
           "all_day": False, "flags": {"travel": "drive"}}]
    monkeypatch.setenv("TRAIN_WATCH_BIN", script(env.tmp, "tw", "echo '%s'\n" % json.dumps(legs)))
    monkeypatch.setenv("CALCACHE_BIN", script(env.tmp, "cc", "echo '%s'\n" % json.dumps(ev)))
    await online(env, phone)
    await phone.send("state", {"alarm": {"next": dep - 10 * 60}})          # later than leave-by - 30 min
    p = await phone.recv("push", timeout=10)
    assert p["body"]["tag"] == "alarm-check" and p["body"]["private"] is True and p["body"]["level"] == "timed"
    assert "you leave 06:00" in p["body"]["title"] and "Belgium train" in p["body"]["body"]
    assert env.gw.db.one("SELECT verbs FROM push_log WHERE id=?", p["id"])          # [Seen] works like any push
    flag, _ = env.gw.db.get_state(phone.device_id, "flag.alarm_check")
    assert flag["ok"] is False and flag["reason"] == "alarm_late" and flag["slack_min"] == -20
    await phone.send("state", {"alarm": {"next": None}})                    # no alarm: still the same departure
    await asyncio.sleep(0.5)
    assert [e["t"] for e in phone.received].count("push") == 1
    await phone.send("state", {"alarm": {"next": dep - 60 * 60}})           # fine: 30 min to spare
    for _ in range(40):
        flag, _ = env.gw.db.get_state(phone.device_id, "flag.alarm_check")
        if flag["ok"]:
            break
        await asyncio.sleep(0.1)
    assert flag["ok"] and flag["slack_min"] == 30


async def test_no_alarm_is_a_problem_and_unknown_alarm_is_not(env, phone, monkeypatch):
    dep = local_epoch(2, 5)
    monkeypatch.setenv("PRODUCERS_NOW", str(local_epoch(1, 21)))
    monkeypatch.setenv("TRAIN_WATCH_BIN", "/bin/false")
    ev = [{"title": "✈️ Flight", "start": time.strftime("%Y-%m-%dT%H:%M:00", time.localtime(dep)), "all_day": False,
           "flags": {"travel": "flight"}}]
    monkeypatch.setenv("CALCACHE_BIN", script(env.tmp, "cc", "echo '%s'\n" % json.dumps(ev)))
    await online(env, phone)
    assert await env.gw.check_alarm(phone.device_id) is None                # never reported: no comparison
    await phone.send("state", {"alarm": {"next": None}})
    p = await phone.recv("push", timeout=10)
    assert p["body"]["title"].startswith("⏰ No alarm, you leave 05:00")


async def test_geo_pack_build_serve_etag(env, phone, monkeypatch):
    monkeypatch.setenv("PROFILE_GET_BIN", script(env.tmp, "pg", "case \"$1\" in home.lat) echo 46.5;; home.lon) echo 7.5;; "
                                                 "*) exit 2;; esac\n"))
    geo = {"nearlist:46.500,7.500": [["Hauptstrasse 5, Neustadt", 46.5, 7.5], ["Neustadt, Post", 46.50012, 7.50034],
                                     ["Neustadt, Mitte", 46.502, 7.498], ["Neustadt Bahnhof", 46.504, 7.51]],
           "Expo Stop": [46.9, 7.4], "nearlist:46.900,7.400": [["Bigcity, Expo", 46.9001, 7.4002]]}
    (env.tmp / "geo.json").write_text(json.dumps(geo))
    monkeypatch.setenv("GEO_FAKE", str(env.tmp / "geo.json"))
    (env.tmp / "venues.json").write_text(json.dumps({"expo": {"stop": "Expo Stop"}}))
    monkeypatch.setenv("NIGHT_VENUES", str(env.tmp / "venues.json"))
    monkeypatch.setenv("TRAIN_WATCH_BIN", "/bin/false")
    monkeypatch.setenv("CALCACHE_BIN", "/bin/false")
    st, j = await phone.http("GET", "/v1/geo/ch")
    assert st == 404
    st, j = await env.call("POST", "/internal/geo", {"build": True})
    assert j["ok"] and j["towns"] == 2 and j["stops"] == 4, j
    st, g = await phone.http("GET", "/v1/geo/ch")
    assert st == 200 and ["Neustadt", 46.502, 7.503] in g["towns"] and ["Neustadt, Post", 46.5001, 7.5003] in g["stops"]
    env.gw.proto.check_http("geo.response", g)
    assert not any("Hauptstrasse" in x[0] for x in g["towns"] + g["stops"])    # street-address entries never ship
    assert oct(os.stat(env.gw.geo_path("ch")).st_mode & 0o777) == "0o600"
    import aiohttp
    async with aiohttp.ClientSession() as s:
        async with s.get(env.tls + "/v1/geo/ch", ssl=phone.ctx()) as r:
            etag = r.headers["ETag"]
        async with s.get(env.tls + "/v1/geo/ch", ssl=phone.ctx(), headers={"If-None-Match": etag}) as r:
            assert r.status == 304
    st, _ = await phone.http("GET", "/v1/geo/ch", cert=False)
    assert st in (401, 403)
    st, j = await env.call("POST", "/internal/geo", {})
    assert j["status"] == "present" and j["stops"] == 4


# ---------------------------------------------------------------------------------------------- leaving + offline
async def test_leaving_routine_card(env, phone, monkeypatch):
    dep = time.strftime("%Y-%m-%dT%H:%M:00", time.localtime(now_s() + 1800))
    legs = [{"id": "x1", "from": "Neustadt", "to": "Bigcity", "dep": dep, "arr": dep, "lines": ["IR 36"], "title": "🚆 Bigcity"}]
    monkeypatch.setenv("TRAIN_WATCH_BIN", script(env.tmp, "tw", "if [ \"$1\" = list ]; then echo '%s'; else exit 1; fi\n" % json.dumps(legs)))
    monkeypatch.setenv("TG_STATUS_TODO", str(env.tmp / "none.md"))
    monkeypatch.setenv("TG_STATUS_DIR", str(env.tmp / "tgs"))
    (env.tmp / "rem.json").write_text(json.dumps([{"id": "5e6f7081", "at": "2026-01-01 00:00", "owner": True, "on": "leave", "text": "x"}]))
    monkeypatch.setenv("REMIND_DB", str(env.tmp / "rem.json"))
    await online(env, phone)
    await phone.send("state", {"battery": {"level": 18, "charging": False}})
    await asyncio.sleep(0.1)
    t0 = time.time()
    st, j = await env.call("POST", "/internal/routine", {"name": "leaving"})
    c = await phone.recv("card")
    assert time.time() - t0 < 5 and j["ok"]
    assert c["body"]["title"] == "🚪 Leaving" and "IR 36" in c["body"]["text"] and "→ Bigcity" in c["body"]["text"]
    assert "🔋 20 %" in c["body"]["text"] and "1 reminder when you leave" in c["body"]["text"]


async def test_offline_note_once_a_day_and_not_at_home(env, phone):
    await online(env, phone)
    await phone.send("state", {"place": {"zone": "away", "band": "far"}})
    await asyncio.sleep(0.1)
    await phone.close()
    env.gw.db.x("UPDATE devices SET last_seen=? WHERE id=?", now_s() - 3 * 3600, phone.device_id)
    assert len(env.gw.check_offline()) == 1
    assert env.gw.check_offline() == [] and len(env.notes("offline")) == 1
    env.gw.db.put_state(phone.device_id, "offline_noted", {})
    env.gw.db.put_state(phone.device_id, "place", {"zone": "home"})
    assert env.gw.check_offline() == []


async def test_hello_catches_up_fences_alarms_widgets(env, monkeypatch):
    from conftest import FakePhone
    geo_env(env, monkeypatch)
    rem = [{"id": "1a2b3c4d", "owner": True, "text": "Gym bag", "at": time.strftime("%Y-%m-%d %H:%M", time.localtime(now_s() + 3600))}]
    (env.tmp / "rem.json").write_text(json.dumps(rem))
    monkeypatch.setenv("REMIND_DB", str(env.tmp / "rem.json"))
    env.gw.cfg["alarms"] = "on"
    st, j = await env.call("POST", "/internal/geofences", {"source": "auto"})
    assert j["status"] == "stored"
    st, j = await env.call("POST", "/internal/alarms", {})
    assert j["status"] == "stored"
    st, j = await env.call("POST", "/internal/widget", {"kind": "board", "data": BOARD})
    assert j["results"][0]["status"] == "no_device"            # nothing stored for widgets without a device
    p = FakePhone(env)
    await p.pair(env.code())
    await online(env, p)
    g = await p.recv("geofences")
    a = await p.recv("alarms")
    assert len(g["body"]["fences"]) == 5 and a["body"]["items"][0]["rid"] == "1a2b3c4d"
    st, j = await env.call("POST", "/internal/widget", {"kind": "board", "data": BOARD})
    await p.recv("widget")
    await p.close()
    p2 = FakePhone(env, name="Tab")                            # a second device gets all three on its first hello
    await p2.pair(env.code())
    await online(env, p2)
    kinds = {(await p2.recv())["t"] for _ in range(3)}
    assert kinds == {"geofences", "alarms", "widget"}
    await p2.close()
    p.frames, p.received = asyncio.Queue(), []
    await online(env, p)                                       # the first phone, reconnecting, gets nothing new
    await asyncio.sleep(0.3)
    assert not [e for e in p.received if e["t"] in ("geofences", "alarms", "widget")]
    await p.close()


# ---------------------------------------------------------------------------------------------- shares, updater, alarms actions
async def test_share_frame_and_multipart(env, phone):
    import aiohttp
    await online(env, phone)
    await phone.send("share", {"share_id": "s_link1", "kind": "youtube", "intent": "summary", "url": "https://youtu.be/x",
                               "text": "watch this"}, ack=True)
    await phone.send("share", {"share_id": "s_link1", "kind": "youtube", "intent": "summary"}, ack=True)   # dup id
    await phone.recv("ack")
    for _ in range(40):
        if env.notes("share"):
            break
        await asyncio.sleep(0.05)
    notes = env.notes("share")
    assert len(notes) == 1 and "https://youtu.be/x" in open(os.path.join(env.paths.inbox, notes[0])).read()

    async def post(meta, files):
        fd = aiohttp.FormData()
        fd.add_field("meta", json.dumps(meta), content_type="application/json")
        for name, data, mime in files:
            fd.add_field("file", data, filename=name, content_type=mime)
        async with aiohttp.ClientSession() as s:
            async with s.post(env.tls + "/v1/share", data=fd, ssl=phone.ctx()) as r:
                return r.status, await r.json()

    meta = {"share_id": "s_img1", "kind": "image", "intent": "fish",
            "files": [{"name": "IMG 1.jpg", "mime": "image/jpeg", "bytes": 5}]}
    st, j = await post(meta, [("../IMG 1.jpg", b"\xff\xd8abc", "image/jpeg")])
    assert st == 200 and j == {"ok": True, "accepted": 1, "dup": 0}
    st, j = await post(meta, [("IMG 1.jpg", b"x", "image/jpeg")])
    assert j["dup"] == 1
    env.gw.cfg["share_min_free"] = 1 << 62                     # less free disk than the floor -> 507, nothing stored
    full = {"share_id": "s_full", "kind": "image", "intent": "what", "files": [{"name": "a.jpg", "mime": "image/jpeg"}]}
    st, j = await post(full, [("a.jpg", b"\xff\xd8abc", "image/jpeg")])
    assert st == 507 and j["error"] == "disk_full"
    assert not os.path.exists(os.path.join(env.paths.state, "shares", "s_full"))
    env.gw.cfg["share_min_free"] = 0
    d = os.path.join(env.paths.state, "shares", "s_img1")
    files = os.listdir(d)
    assert files == ["01-IMG_1.jpg"] and oct(os.stat(os.path.join(d, files[0])).st_mode & 0o777) == "0o600"
    texts = [open(os.path.join(env.paths.inbox, n)).read() for n in env.notes("share")]
    text = next(t for t in texts if "s_img1" in t)
    assert len(texts) == 2
    assert "skill divelog" in text and "01-IMG_1.jpg" in text
    bill = {"share_id": "s_bill", "kind": "pdf", "intent": "save", "files": [{"name": "Rechnung_Okt.pdf", "mime": "application/pdf"}]}
    st, j = await post(bill, [("Rechnung_Okt.pdf", b"%PDF", "application/pdf")])
    assert st == 422 and j["error"] == "paperwork" and not os.path.exists(os.path.join(env.paths.state, "shares", "s_bill"))
    st, j = await post({"share_id": "s_bad", "kind": "contacts", "intent": "save"}, [])
    assert st == 422
    st, _ = await phone.http("POST", "/v1/share", {"x": 1}, cert=False)
    assert st in (401, 403)


async def test_release_update_and_apk(env, phone, tmp_path):
    import aiohttp
    import hashlib
    apk = tmp_path / "claude-0.2.0-2.apk"
    apk.write_bytes(b"PK\x03\x04" + os.urandom(5000))
    st, j = await env.call("POST", "/internal/update", {"apk": str(apk), "notes": ["Widgets"]})
    assert j["ok"] and j["code"] == 2 and j["name"] == "0.2.0" and j["status"] == "queued" and j["devices"] == 1
    assert j["sha256"] == hashlib.sha256(apk.read_bytes()).hexdigest()
    await online(env, phone)                                    # paired with app_code 1: the queued update replays
    u = await phone.recv("app.update")
    assert u["body"]["path"] == "/v1/app/apk/2" and u["body"]["size"] == 5004
    async with aiohttp.ClientSession() as s:
        async with s.get(env.tls + u["body"]["path"], ssl=phone.ctx()) as r:
            assert r.status == 200 and hashlib.sha256(await r.read()).hexdigest() == u["body"]["sha256"]
    st, j = await phone.http("GET", "/v1/app/latest")
    assert st == 200 and j["code"] == 2
    st, _ = await phone.http("GET", "/v1/app/apk/2", cert=False)
    assert st in (401, 403)
    st, _ = await phone.http("GET", "/v1/app/apk/3")
    assert st == 404
    st, j = await env.call("POST", "/internal/update", {"apk": str(tmp_path / "nope.apk")})
    assert st == 400


async def test_alarm_items_carry_push_id_and_actions_sync(env, phone, monkeypatch):
    at = time.strftime("%Y-%m-%d %H:%M", time.localtime(now_s() + 3600))
    (env.tmp / "rem.json").write_text(json.dumps([{"id": "1a2b3c4d", "at": at, "owner": True, "text": "Gym bag"}]))
    monkeypatch.setenv("REMIND_DB", str(env.tmp / "rem.json"))
    env.gw.cfg["alarms"] = "on"
    await online(env, phone)
    st, j = await env.call("POST", "/internal/alarms", {})
    it = (await phone.recv("alarms"))["body"]["items"][0]
    assert it["push_id"] and len(it["nonce"]) == 8
    await phone.send("action", {"push_id": it["push_id"], "nonce": it["nonce"], "verb": "snooze", "arg": "r:1a2b3c4d",
                                "at": now_s()}, ack=True)
    for _ in range(40):
        row = env.gw.db.one("SELECT verb, arg, level FROM push_log WHERE id=?", it["push_id"])
        if row["verb"]:
            break
        await asyncio.sleep(0.05)
    assert row["verb"] == "snooze" and row["arg"] == "r:1a2b3c4d" and row["level"] == "alarm"
    from phonegw import bridge
    plan = bridge.plan({"push_id": it["push_id"], "level": "alarm", "verb": "snooze", "arg": "r:1a2b3c4d"})
    assert plan and plan[0][0][1:] == ["act", "snooze", "r:1a2b3c4d"]


async def test_tiles_widget_and_night_config(env, phone, monkeypatch):
    monkeypatch.setenv("SLEEPIN_BIN", script(env.tmp, "sleepin", "exit 0\n"))
    env.gw.cfg["night"] = "hold"
    await phone.connect()
    cfg = await phone.recv("config")
    assert cfg["body"]["night"] == "hold"
    st, j = await env.call("POST", "/internal/widget", {"kind": "tiles"})
    w = await phone.recv("widget")
    assert w["body"] == {"kind": "tiles", "data": {"sleepin": True, "updated": w["body"]["data"]["updated"]}}


# ---------------------------------------------------------------------------------------------- deck tile, board times, leaving notify
async def test_deck_tile_sends_waiting_items_as_cards_once_a_minute(env, phone, monkeypatch):
    deck = {"items": [{"id": "ab12", "title": "Ferry or plane", "say": "Ferry or plane?", "options": ["ferry", "plane"]}]}
    runs = env.tmp / "deck-runs"
    monkeypatch.setenv("DECISION_DECK_BIN", script(env.tmp, "deck", "echo x >> '%s'\necho '%s'\n" % (runs, json.dumps(deck))))
    await online(env, phone)
    env.gw.cfg["bridge"] = True
    await phone.send("event", {"kind": "tile", "name": "deck", "at": now_s()}, ack=True)
    c = await phone.recv("card")
    assert c["body"]["card_id"] == "c_deck_ab12" and c["body"]["group"] == "deck"
    await phone.send("event", {"kind": "tile", "name": "deck", "at": now_s()}, ack=True)
    await asyncio.sleep(0.4)
    assert runs.read_text().count("x") == 1, "a second tap within a minute doesn't re-run the deck"
    env.gw.deck_at = -1e9
    env.gw.cfg["routines"] = {"card_deck": False}
    await phone.send("event", {"kind": "tile", "name": "deck", "at": now_s()}, ack=True)
    await asyncio.sleep(0.4)
    assert runs.read_text().count("x") == 1, "routines.card_deck false = off"


def test_board_times_carry_an_epoch():
    from phonegw.producers import when_at
    t = int(time.mktime((2026, 10, 20, 23, 0, 0, 0, 0, -1)))
    assert when_at("09:00", t) == (int(time.mktime((2026, 10, 20, 9, 0, 0, 0, 0, -1))), False)
    assert when_at("21.10 09:00", t) == (int(time.mktime((2026, 10, 21, 9, 0, 0, 0, 0, -1))), False)
    assert when_at("05.12", t) == (int(time.mktime((2026, 12, 5, 0, 0, 0, 0, 0, -1))), True)
    assert when_at("02.01 08:00", t)[0] == int(time.mktime((2027, 1, 2, 8, 0, 0, 0, 0, -1)))   # nearest year
    assert when_at("31.02", t) == (None, False) and when_at(None, t) == (None, False) and when_at("soon", t) == (None, False)


def test_board_sleep_line_only_with_health_on(monkeypatch, tmp_path):
    from phonegw import producers
    fake = tmp_path / "health"
    fake.write_text("#!/bin/sh\necho '{\"line\": \"x 7 h 05\", \"night\": \"2026-10-21\", \"tone\": \"low\", \"extra\": 1}'\n")
    fake.chmod(0o755)
    monkeypatch.setenv("HEALTH_BIN", str(fake))
    monkeypatch.setattr(producers, "cfg", lambda: {"health": False})
    assert producers.sleep_line() is None
    monkeypatch.setattr(producers, "cfg", lambda: {"health": True})
    assert producers.sleep_line() == {"line": "x 7 h 05", "night": "2026-10-21", "tone": "low"}
    monkeypatch.setenv("HEALTH_BIN", "/bin/false")
    assert producers.sleep_line() is None


def test_board_parcels_line(monkeypatch, tmp_path):
    from phonegw import producers
    fake = tmp_path / "parcels"
    fake.write_text("#!/bin/sh\necho '{\"title\": \"Pick up 1 parcel in Town by Thu 29.10\", \"n\": 1}'\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PARCELS_BIN", str(fake))
    assert producers.parcels_line() == "Pick up 1 parcel in Town by Thu 29.10"
    fake.write_text("#!/bin/sh\necho '{}'\n")
    assert producers.parcels_line() is None
    monkeypatch.setenv("PARCELS_BIN", "/bin/false")
    assert producers.parcels_line() is None


def test_leaving_card_notifies(monkeypatch):
    from phonegw import producers
    card = producers.leaving({"battery": {"level": 80}})
    assert card["notify"] is True and card["group"] == "routine"
