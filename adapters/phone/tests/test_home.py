"""Home-while-away widget producer (`home`): status only, privacy-gated ha reads, cat corner flags, hourly HA contact."""
import json
import os
import stat
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from phonegw import producers as P  # noqa: E402
from phonegw.schema import Protocol  # noqa: E402

PROTOCOL = os.environ.get("PHONEGW_PROTOCOL") or os.path.expanduser("~/opt/phone-app/claude/protocol")
NOW = 1792526112

LIGHTS = [
    {"entity_id": "light.porch", "state": "on", "attributes": {}},
    {"entity_id": "light.bulb_1", "state": "on", "attributes": {}},
    {"entity_id": "light.bulb_2", "state": "on", "attributes": {}},
    {"entity_id": "light.hanging", "state": "on", "attributes": {"entity_id": ["light.bulb_1", "light.bulb_2"]}},
    {"entity_id": "light.desk", "state": "off", "attributes": {}},
    {"entity_id": "light.dead", "state": "unavailable", "attributes": {}},
]
PLAYERS = [
    {"entity_id": "media_player.tv_a", "state": "unavailable", "attributes": {"device_class": "tv"}},
    {"entity_id": "media_player.tv_b", "state": "off", "attributes": {"device_class": "tv"}},
    {"entity_id": "media_player.speaker", "state": "playing", "attributes": {"device_class": "speaker"}},
]
VACS = [{"entity_id": "vacuum.litter_box", "name": "Litter box", "state": "error"},
        {"entity_id": "vacuum.robot", "name": "Robot", "state": "cleaning"}]
SENSORS = [
    {"entity_id": "sensor.living_t", "state": "22.5", "area": "Living Room", "attributes": {"device_class": "temperature", "unit_of_measurement": "°C"}},
    {"entity_id": "sensor.bed_t", "state": "20.4", "area": "Bedroom", "attributes": {"device_class": "temperature", "unit_of_measurement": "°C"}},
    {"entity_id": "sensor.balcony_t", "state": "3.0", "area": "Balcony", "attributes": {"device_class": "temperature", "unit_of_measurement": "°C"}},
    {"entity_id": "sensor.cpu_t", "state": "55", "area": None, "attributes": {"device_class": "temperature", "unit_of_measurement": "°C"}},
    {"entity_id": "sensor.phone_geocoded_location", "state": "Somewhere 1", "area": "Hall", "attributes": {}},
    {"entity_id": "sensor.hum", "state": "50", "area": "Bedroom", "attributes": {"device_class": "humidity"}},
]
CAT = {"view": {"robot": {"drawer": 60.4, "bad_since": None},
                 "feeders": [{"low": False, "offline_since": None, "missed_meal": None, "last_feed": "2026-10-20T11:00:08+00:00"}],
                 "fountains": [{"low": True, "offline_since": None}]}, "problems": [{"x": 1}], "trip": None}
SUPPLIES = {"rates": {"filter_pct_now": 33.0, "desiccant_days_left": -28.0, "feeder_battery": "low"}}


def script(d, name, body):
    p = d / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return str(p)


@pytest.fixture
def fake(tmp_path, monkeypatch):
    data = tmp_path / "hadata"
    data.mkdir()
    for dom, rows in (("light", LIGHTS), ("media_player", PLAYERS), ("vacuum", VACS), ("sensor", SENSORS)):
        (data / (dom + ".json")).write_text(json.dumps(rows))
    (data / "input_boolean.away_mode.json").write_text(json.dumps([{"entity_id": "input_boolean.away_mode", "state": "on"}]))
    log = tmp_path / "ha.log"
    ha = script(tmp_path, "ha", 'echo "$@" >> %s\n[ -f %s/down ] && exit 1\ncase "$1" in list) cat %s/"$2".json;; '
                'state) cat %s/"$2".json || exit 1;; *) exit 2;; esac\n' % (log, tmp_path, data, data))
    monkeypatch.setenv("HA_BIN", ha)
    monkeypatch.setenv("CAT_CORNER_BIN", script(tmp_path, "cc", "echo '%s'\n" % json.dumps(CAT)))
    monkeypatch.setenv("CAT_SUPPLIES_BIN", script(tmp_path, "cs", "echo '%s'\n" % json.dumps(SUPPLIES)))
    monkeypatch.setenv("PRODUCERS_NOW", str(NOW))
    monkeypatch.setenv("PHONEGW_STATE", str(tmp_path / "state"))
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"tz": "Europe/Zurich"}))
    monkeypatch.setenv("PHONEGW_CONFIG", str(cfg))
    return tmp_path, log, cfg


def valid(data):
    proto = Protocol(PROTOCOL)
    proto.check("s2c", {"v": 1, "id": "01JAB3K6Q2W8Z7M4D5N0P9RH09", "t": "widget", "ts": NOW, "ack": True,
                        "body": {"kind": "home", "data": data}})


def test_home_reads_status_only(fake):
    tmp, log, _ = fake
    d = P.home()
    assert d["lights_on"] == 2                        # porch + the hanging group (its bulbs are not counted twice)
    assert d["tv"] == "off" and d["vacuum"] == "cleaning"          # litter box vacuum skipped
    assert d["away"] is True and d["ha"] is True
    assert (d["temp_min"], d["temp_max"]) == (20, 23)              # balcony + area-less sensors skipped; 22.5 -> 23
    assert d["ha_at"] == NOW - NOW % 3600 and d["updated"] == NOW
    c = d["cat"]
    assert c["feeder"] == "ok" and c["fountain"] == "low" and c["litter"] == "ok" and c["drawer_pct"] == 60
    assert c["filter_pct"] == 33 and c["battery_low"] and c["desiccant_due"] and c["problems"] == 1
    assert c["fed_at"] == 1792494008
    valid(d)
    calls = log.read_text()
    assert "on " not in calls and "off " not in calls and "set " not in calls      # reads only, never controls
    assert all(line.split()[0] in ("list", "state") for line in calls.splitlines())
    assert "person" not in calls and "device_tracker" not in calls and "zone" not in calls
    assert "Somewhere" not in json.dumps(d)


def test_home_tv_on_and_overrides(fake, monkeypatch):
    tmp, _, cfg = fake
    PLAYERS[1]["state"] = "on"
    try:
        (tmp / "hadata" / "media_player.json").write_text(json.dumps(PLAYERS))
        assert P.home()["tv"] == "on"
    finally:
        PLAYERS[1]["state"] = "off"
    cfg.write_text(json.dumps({"tz": "Europe/Zurich", "home": {"tv": ["media_player.speaker"], "vacuum": "vacuum.litter_box",
                                                                "temps": ["sensor.balcony_t"]}}))
    d = P.home()
    assert d["tv"] == "on" and d["vacuum"] == "error" and (d["temp_min"], d["temp_max"]) == (3, 3)
    valid(d)


def test_home_ha_down_keeps_last_contact(fake):
    tmp, _, _ = fake
    first = P.home()
    (tmp / "down").write_text("1")
    d = P.home()
    assert d["ha"] is False and d["ha_at"] == first["ha_at"]
    assert d["lights_on"] is None and d["tv"] == "unknown" and d["vacuum"] == "unknown" and d["away"] is None
    valid(d)
    os.unlink(tmp / "state" / "home-ha.json")
    assert P.home()["ha_at"] is None


def test_home_without_cat_tools(fake, monkeypatch):
    monkeypatch.setenv("CAT_CORNER_BIN", "/bin/false")
    d = P.home()
    assert d["cat"] is None
    valid(d)
    monkeypatch.setenv("CAT_CORNER_BIN", script(fake[0], "cc2", "echo '%s'\n" % json.dumps(CAT)))
    monkeypatch.setenv("CAT_SUPPLIES_BIN", "/bin/false")
    c = P.home()["cat"]
    assert c["filter_pct"] is None and c["battery_low"] is False


def test_home_schema_refuses_location():
    with pytest.raises(Exception):
        valid({"ha": True, "zone": "home"})
