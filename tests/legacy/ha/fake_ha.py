"""Fake Home Assistant (REST + read-only websocket traces) for ~/bin/ha tests; pattern of ~/opt/assistant/tests/legacy/ha-audit/fake_ha.py.

Holds mutable states, applies the effect of the service calls ha may send (so the read-back can be
tested), records every request, and carries privacy canaries that must never appear in ha output/logs.
"""  # noqa: E501

import base64
import copy
import datetime as dt
import hashlib
import json
import re
import struct
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = "fake-token-CANARY-0123456789abcdefXYZ"
CANARIES = [
    "12.345",
    "65.432",
    "Musterstrasse",
    "1234",
    "Exampletown",
    "ExampleNet-5G",
    "aa:bb:cc:dd:ee:ff",
    "AA:BB:CC:DD:EE:01",
    "198.51.100.",
    "203.0.113.77",
    "Your package",
    "alex@example.com",
    "AirPods",
    "CANARYPICTOKEN",
    "CANARYAPIKEY",
    "CANARYCAMTOKEN",
    "Dentist",
    "fe80::1:2:3:4",
    "alex_gmail_com",
    TOKEN,
]
T0 = "2026-10-15T21:00:00.123456+00:00"
PRIV = {"latitude": 12.34567, "longitude": 65.43219, "gps_accuracy": 12, "altitude": 410.5}


def st(eid, state, attrs=None):
    return {
        "entity_id": eid,
        "state": state,
        "attributes": attrs or {},
        "last_changed": T0,
        "last_updated": T0,
        "context": {"id": "x"},
    }


def fixtures():
    return [
        st("person.alex", "Work", dict(PRIV, friendly_name="Alex", source="device_tracker.phone")),
        st("person.jamie", "Musterstrasse 12, 1234 Exampletown", dict(PRIV, friendly_name="Jamie")),
        st(
            "device_tracker.phone",
            "home",
            dict(PRIV, ip="198.51.100.23", ssid="ExampleNet-5G", friendly_name="Galaxy A55", source_type="gps"),
        ),
        st("zone.home", "1", dict(PRIV, radius=100, friendly_name="Home")),
        st("zone.work", "1", dict(PRIV, radius=100, friendly_name="Work")),
        st(
            "sensor.phone_geocoded_location",
            "Musterstrasse 12, 1234 Exampletown",
            {
                "Location": [12.34567, 65.43219],
                "Thoroughfare": "Musterstrasse",
                "Postal Code": "1234",
                "friendly_name": "Galaxy A55 Geocoded location",
            },
        ),
        st("sensor.phone_wifi_connection", "ExampleNet-5G", {"friendly_name": "Galaxy A55 Wi-Fi connection"}),
        st("sensor.phone_wifi_bssid", "aa:bb:cc:dd:ee:ff", {"friendly_name": "Galaxy A55 Wi-Fi BSSID"}),
        st(
            "sensor.phone_last_notification",
            "Your package from Musterstrasse 12",
            {"android.text": "Your package", "friendly_name": "Galaxy A55 Last notification"},
        ),
        st("sensor.phone_public_ip", "203.0.113.77", {"friendly_name": "Galaxy A55 Public IP"}),
        st("sensor.phone_battery_level", "54", {"unit_of_measurement": "%", "friendly_name": "Galaxy A55 Battery"}),
        st(
            "sensor.car_status",
            "parked",
            {
                "friendly_name": "Car status",
                "battery_level": 0.87654,
                "x": 12.34567,
                "details": {
                    "pos": {"lat": 12.34567, "lon": 65.43219},
                    "coords": [12.34567, 65.43219],
                    "items": [
                        {"address": "Musterstrasse 12"},
                        {"contact": "alex@example.com"},
                        {"router": "198.51.100.1", "ap_mac": "AA:BB:CC:DD:EE:01", "v6": "fe80::1:2:3:4"},
                    ],
                    "wifi_ssid": "ExampleNet-5G",
                    "note": "parked at Musterstrasse 12, 1234 Exampletown",
                },
                "last_seen": "2026-10-15T20:59:59.987654+00:00",
            },
        ),
        st(
            "sensor.living_room_temperature",
            "21.53125",
            {"unit_of_measurement": "°C", "device_class": "temperature", "friendly_name": "Living room temperature"},
        ),
        st(
            "media_player.lg_webos_tv_oled",
            "on",
            {
                "friendly_name": "LG webOS TV OLED55C7V",
                "source": "HDMI 1",
                "source_list": ["HDMI 1", "Netflix"],
                "volume_level": 0.12000000476837158,
                "is_volume_muted": False,
                "entity_picture": "/api/media_player_proxy/media_player.lg?token=CANARYPICTOKEN&cache=1",
                "media_content_id": "http://198.51.100.5:8096/Videos/1/stream?api_key=CANARYAPIKEY",
            },
        ),
        st("switch.tv_plug", "on", {"friendly_name": "TV Plug"}),
        st(
            "light.living_room_ceiling",
            "on",
            {
                "friendly_name": "Ceiling Light",
                "brightness": 128,
                "color_temp_kelvin": 2700,
                "supported_color_modes": ["color_temp"],
            },
        ),
        st("light.living_room_lamp", "off", {"friendly_name": "Floor Lamp"}),
        st("light.kitchen", "off", {"friendly_name": "Kitchen Lamp"}),
        st("light.bedroom_dead", "unavailable", {"friendly_name": "Bedroom", "restored": True}),
        st("switch.garage_door_opener", "off", {"friendly_name": "Garage door opener"}),
        st("cover.garage", "closed", {"friendly_name": "Garage", "device_class": "garage", "current_position": 0}),
        st(
            "cover.living_room_blinds",
            "open",
            {"friendly_name": "Blinds", "device_class": "shade", "current_position": 100},
        ),
        st("lock.front_door", "locked", {"friendly_name": "Front door"}),
        st("alarm_control_panel.home_alarm", "armed_away", {"friendly_name": "Alarm"}),
        st(
            "scene.movie_night",
            T0,
            {
                "friendly_name": "Movie night",
                "entity_id": ["light.living_room_ceiling", "media_player.lg_webos_tv_oled"],
            },
        ),
        st("scene.bad_scene", T0, {"friendly_name": "Leave", "entity_id": ["lock.front_door", "light.kitchen"]}),
        st("script.goodnight", "off", {"friendly_name": "Goodnight", "last_triggered": None}),
        st("script.open_up", "off", {"friendly_name": "Open up", "last_triggered": None}),
        st("script.yaml_only", "off", {"friendly_name": "Yaml only", "last_triggered": None}),
        st("device_tracker.alex_airpods", "home", {"friendly_name": "Alex AirPods Pro", "source": "AA:BB:CC:DD:EE:01"}),
        st("sensor.ble_aa_bb_cc_dd_ee_01_rssi", "-70", {"source": "AA:BB:CC:DD:EE:01"}),
        st(
            "calendar.alex_example_com",
            "on",
            {
                "friendly_name": "alex@example.com",
                "message": "Dentist",
                "location": "Musterstrasse 12",
                "description": "Dentist",
            },
        ),
        st(
            "camera.door",
            "idle",
            {
                "friendly_name": "Door cam",
                "access_token": "CANARYCAMTOKEN",
                "entity_picture": "/api/camera_proxy/camera.door?token=CANARYCAMTOKEN",
            },
        ),
        st("button.router_restart", "unknown", {"friendly_name": "Router restart"}),
        st(
            "update.core_update",
            "on",
            {"friendly_name": "Core Update", "installed_version": "1.0.0", "latest_version": "1.0.1"},
        ),
        st("vacuum.robi", "docked", {"friendly_name": "Robi"}),
        st("vacuum.alex_gmail_com", "docked", {"friendly_name": "alex_gmail_com"}),
        st("input_boolean.guest_mode", "off", {"friendly_name": "Guest mode"}),
        st("todo.shopping_list", "1", {"friendly_name": "Shopping list", "supported_features": 15}),
        st("todo.private_notes", "1", {"friendly_name": "Private notes", "supported_features": 15}),
    ]


def todo_fixtures():
    return {
        "todo.shopping_list": [
            {"uid": "u1", "summary": "Milk", "status": "needs_action"},
            {"uid": "u2", "summary": "Batteries AA", "status": "completed"},
        ],
        "todo.private_notes": [{"uid": "p1", "summary": "Dentist", "status": "needs_action"}],
    }


def todo_effect(srv, service, body):
    """HA's todo services on the fake store; returns (code, answer)."""
    eid = body.get("entity_id")
    items = srv.todo.get(eid)
    if items is None:
        return 400, {"message": "Entity not found"}
    if service == "get_items":
        want = body.get("status") or ["needs_action", "completed"]
        want = [want] if isinstance(want, str) else want
        return 200, {
            "changed_states": [],
            "service_response": {eid: {"items": [dict(i) for i in items if i["status"] in want]}},
        }
    ref = body.get("item")
    hit = [i for i in items if i["uid"] == ref] or [i for i in items if i["summary"] == ref]
    if service == "add_item":
        srv.uid += 1
        it = {"uid": f"n{srv.uid}", "summary": ref, "status": "needs_action"}
        if body.get("description"):
            it["description"] = body["description"]
        items.append(it)
    elif not hit:
        return 400, {"message": f"Unable to find to-do list item: {ref}"}
    elif service == "update_item":
        if "status" in body:
            hit[0]["status"] = body["status"]
        if "rename" in body:
            hit[0]["summary"] = body["rename"]
    elif service == "remove_item":
        items.remove(hit[0])
    s = srv.states[eid]
    s["state"] = str(sum(1 for i in items if i["status"] == "needs_action"))
    s["last_updated"] = _now()
    return 200, [copy.deepcopy(s)]


AREAS = {
    "areas": [
        [
            "living_room",
            "Living Room",
            [
                "light.living_room_ceiling",
                "light.living_room_lamp",
                "media_player.lg_webos_tv_oled",
                "switch.tv_plug",
                "cover.living_room_blinds",
                "sensor.living_room_temperature",
            ],
        ],
        ["kitchen", "Küche", ["light.kitchen"]],
        ["bedroom", "Bedroom", ["light.bedroom_dead"]],
        ["garage", "Garage", ["cover.garage", "switch.garage_door_opener"]],
    ],
    "bt": ["device_tracker.alex_airpods"],
}
DEVICE_URLS = {
    "light.living_room_ceiling": "http://ceiling-light.local/",
    "light.kitchen": "http://198.51.100.9",
    "switch.tv_plug": "http://tv-plug.local/admin?token=CANARYAPIKEY",
    "light.bedroom_dead": None,
    "cover.living_room_blinds": "https://blinds.lan:8443",
}
SCRIPT_CFG = {
    "goodnight": {"alias": "Goodnight", "sequence": [{"action": "light.turn_off", "target": {"entity_id": "all"}}]},
    "open_up": {
        "alias": "Open up",
        "sequence": [{"action": "lock.unlock", "target": {"entity_id": "lock.front_door"}}],
    },
}


def _now():
    return dt.datetime.now(dt.UTC).isoformat()


# ----------------------------------------------------------------------------- history / logbook / traces
def ago(**kw):
    return (dt.datetime.now(dt.UTC) - dt.timedelta(**kw)).isoformat()


# entities added by the history tests (not in fixtures(), so the older tests see the same world)
EXTRA_STATES = [
    st(
        "automation.vacuum_dock",
        "on",
        {"friendly_name": "Vacuum dock", "id": "1700000000001", "mode": "single", "current": 0, "last_triggered": T0},
    ),
    st("automation.yaml_noid", "on", {"friendly_name": "Yaml no id", "mode": "single", "current": 0}),
]


def history_rows(eid, start, attrs):
    """Recorder rows as HA's REST API returns them (first row full, then minimal unless attrs)."""
    big = {
        "vacuum.robi": [
            ("docked", {}, 30),
            ("cleaning", {}, 5 * 60),
            ("returning", {}, 4 * 60 + 10),
            ("docked", {}, 4 * 60),
        ],
        "sensor.living_room_temperature": [("21.53125", {}, 600), ("22.0625", {}, 120), ("unavailable", {}, 60)],
        "light.living_room_ceiling": [
            ("off", {}, 600),
            ("on", {"brightness": 128, "color_temp_kelvin": 2700, "entity_picture": "/x?token=CANARYPICTOKEN"}, 300),
            ("on", {"brightness": 255, "color_temp_kelvin": 2700, "entity_picture": "/x?token=CANARYPICTOKEN"}, 200),
            ("on", {"brightness": 255, "color_temp_kelvin": 2700, "x": 1}, 150),
        ],
        "media_player.lg_webos_tv_oled": [
            ("off", {}, 600),
            ("on", {"source": "HDMI 1", "media_content_id": "http://198.51.100.5:8096/v?api_key=CANARYAPIKEY"}, 90),
        ],
        "person.alex": [("home", PRIV, 600), ("Musterstrasse 12, 1234 Exampletown", PRIV, 60)],
        "sensor.phone_geocoded_location": [("Musterstrasse 12, 1234 Exampletown", {}, 60)],
    }
    out = []
    for i, (state, a, minutes) in enumerate(big.get(eid, [])):
        t = start if i == 0 else ago(minutes=minutes)
        if attrs:
            out.append({"entity_id": eid, "state": state, "attributes": dict(a), "last_changed": t, "last_updated": t})
        elif i == 0:
            out.append({"entity_id": eid, "state": state, "attributes": {}, "last_changed": t, "last_updated": t})
        else:
            out.append({"state": state, "last_changed": t})
    return out


def logbook_rows():
    return [
        {
            "when": ago(minutes=300),
            "name": "Vacuum dock",
            "message": "triggered by state of vacuum.robi",
            "source": "state of vacuum.robi",
            "entity_id": "automation.vacuum_dock",
            "domain": "automation",
            "context_id": "c1",
        },
        {
            "when": ago(minutes=299),
            "state": "returning",
            "entity_id": "vacuum.robi",
            "name": "Robi",
            "context_entity_id": "automation.vacuum_dock",
            "context_entity_id_name": "Vacuum dock",
            "context_event_type": "automation_triggered",
            "context_message": "triggered by state of person.alex",
            "context_state": "Musterstrasse 12",
        },
        {
            "when": ago(minutes=200),
            "name": "Alex",
            "message": "is at Musterstrasse 12, 1234 Exampletown",
            "entity_id": "person.alex",
            "state": "Exampletown",
        },
        {
            "when": ago(minutes=150),
            "state": "Musterstrasse 12, 1234 Exampletown",
            "name": "Galaxy A55 Geocoded",
            "entity_id": "sensor.phone_geocoded_location",
        },
        {
            "when": ago(minutes=120),
            "state": "home",
            "name": "Alex AirPods Pro",
            "entity_id": "device_tracker.alex_airpods",
        },
        {
            "when": ago(minutes=100),
            "state": "on",
            "entity_id": "light.living_room_ceiling",
            "name": "Ceiling Light",
            "context_user_id": "u123",
            "context_domain": "light",
            "context_service": "turn_on",
        },
        {
            "when": ago(minutes=90),
            "name": "Arrival",
            "message": "triggered by state of device_tracker.alex_airpods",
            "entity_id": "automation.arrival",
            "domain": "automation",
        },
        {
            "when": ago(minutes=80),
            "state": "on",
            "entity_id": "light.kitchen",
            "name": "Kitchen Lamp",
            "context_entity_id": "person.alex",
            "context_state": "Exampletown",
        },
        {"when": ago(minutes=10), "name": "Home Assistant", "message": "started", "domain": "homeassistant"},
    ]


def _step(path, minutes, **kw):
    return [dict({"path": path, "timestamp": ago(minutes=minutes)}, **kw)]


def trace_runs():
    trig = {
        "id": "0",
        "idx": "0",
        "alias": None,
        "platform": "state",
        "entity_id": "vacuum.robi",
        "from_state": {"entity_id": "vacuum.robi", "state": "cleaning", "attributes": PRIV},
        "to_state": {"entity_id": "vacuum.robi", "state": "returning", "attributes": PRIV},
        "description": "state of vacuum.robi",
    }
    this = {"entity_id": "automation.vacuum_dock", "state": "on", "attributes": dict(PRIV, note="Musterstrasse 12")}
    cfg = {
        "id": "1700000000001",
        "alias": "Vacuum dock",
        "description": "Musterstrasse 12, 1234 Exampletown",
        "triggers": [{"trigger": "state", "entity_id": "vacuum.robi"}],
        "conditions": [
            {"condition": "state", "entity_id": ["person.jamie"], "state": "not_home"},
            {"condition": "template", "value_template": "{{ true }}"},
        ],
        "actions": [
            {"action": "vacuum.return_to_base", "target": {"entity_id": "vacuum.robi"}},
            {"action": "notify.mobile_app_galaxy", "data": {"message": "Your package"}},
            {"delay": 300},
        ],
    }
    ok = {
        "last_step": "action/2",
        "run_id": "01JAAAAAAAAAAAAAAAAAAAAAA1",
        "state": "stopped",
        "script_execution": "finished",
        "timestamp": {"start": ago(minutes=300), "finish": ago(minutes=295)},
        "domain": "automation",
        "item_id": "1700000000001",
        "trigger": "state of vacuum.robi",
        "trace": {
            "trigger/0": _step("trigger/0", 300, changed_variables={"this": this, "trigger": trig}),
            "condition/0": _step("condition/0", 300, result={"result": True}),
            "condition/0/entity_id/0": _step(
                "condition/0/entity_id/0", 300, result={"result": True, "state": "not_home", "wanted_state": "not_home"}
            ),
            "condition/1": _step("condition/1", 300, result={"result": True, "entities": []}),
            "action/0": _step(
                "action/0",
                300,
                result={
                    "params": {
                        "domain": "vacuum",
                        "service": "return_to_base",
                        "service_data": {},
                        "target": {"entity_id": ["vacuum.robi"]},
                    },
                    "running_script": False,
                },
            ),
            "action/1": _step(
                "action/1",
                300,
                result={
                    "params": {
                        "domain": "notify",
                        "service": "mobile_app_galaxy",
                        "service_data": {"message": "Your package from Musterstrasse 12", "title": "alex@example.com"},
                        "target": {},
                    },
                    "running_script": False,
                },
            ),
            "action/2": _step("action/2", 300, result={"delay": 300.0, "done": True}),
        },
        "config": cfg,
        "blueprint_inputs": None,
        "context": {"id": "c1", "parent_id": None, "user_id": "u1"},
    }
    failed = {
        "last_step": "condition/0",
        "run_id": "01JAAAAAAAAAAAAAAAAAAAAAA2",
        "state": "stopped",
        "script_execution": "failed_conditions",
        "timestamp": {"start": ago(minutes=100), "finish": ago(minutes=100)},
        "domain": "automation",
        "item_id": "1700000000001",
        "trigger": "state of vacuum.robi",
        "trace": {
            "trigger/0": _step("trigger/0", 100, changed_variables={"this": this, "trigger": trig}),
            "condition/0": _step("condition/0", 100, result={"result": False}),
            "condition/0/entity_id/0": _step(
                "condition/0/entity_id/0",
                100,
                result={"result": False, "state": "Musterstrasse 12, 1234 Exampletown", "wanted_state": "not_home"},
            ),
        },
        "config": cfg,
        "blueprint_inputs": None,
        "context": {"id": "c2"},
    }
    error = {
        "last_step": "action/0",
        "run_id": "01JAAAAAAAAAAAAAAAAAAAAAA3",
        "state": "stopped",
        "script_execution": "error",
        "error": "Error rendering data template at 198.51.100.7",
        "timestamp": {"start": ago(minutes=20), "finish": ago(minutes=20)},
        "domain": "automation",
        "item_id": "1700000000001",
        "trigger": "state of device_tracker.alex_airpods",
        "trace": {
            "trigger/0": _step(
                "trigger/0",
                20,
                changed_variables={
                    "trigger": {
                        "platform": "state",
                        "entity_id": "device_tracker.alex_airpods",
                        "from_state": {"state": "not_home"},
                        "to_state": {"state": "home"},
                    }
                },
            ),
            "action/0": _step("action/0", 20, error="Error rendering data template at 198.51.100.7"),
        },
        "config": cfg,
        "blueprint_inputs": None,
        "context": {"id": "c3"},
    }
    return {("automation", "1700000000001"): [ok, failed, error]}


def short_trace(t):
    keep = ("last_step", "run_id", "state", "script_execution", "timestamp", "domain", "item_id", "trigger", "error")
    return {k: t[k] for k in keep if k in t}


def apply_effect(states, domain, service, body):
    ids = body.get("entity_id")
    ids = [ids] if isinstance(ids, str) else ids or []
    changed = []
    for eid in ids:
        s = states.get(eid)
        if not s:
            continue
        a = s["attributes"]
        old = (s["state"], json.dumps(a, sort_keys=True))
        if service == "turn_off":
            s["state"] = "off"
        elif service == "turn_on":
            if domain == "scene":
                s["state"] = _now()
            elif domain == "script":
                a["last_triggered"] = _now()
            else:
                s["state"] = "on"
                if "brightness_pct" in body:
                    if body["brightness_pct"] == 0:
                        s["state"] = "off"
                    else:
                        a["brightness"] = round(body["brightness_pct"] * 255 / 100)
                if "color_temp_kelvin" in body:
                    a["color_temp_kelvin"] = body["color_temp_kelvin"]
                if "color_name" in body:
                    a["rgb_color"] = [255, 0, 0]
        elif service == "toggle":
            s["state"] = "off" if s["state"] == "on" else "on"
        elif service == "media_play":
            s["state"] = "playing"
        elif service == "media_pause":
            s["state"] = "paused"
        elif service == "media_stop":
            s["state"] = "idle"
        elif service == "volume_set":
            a["volume_level"] = body["volume_level"]
        elif service == "volume_mute":
            a["is_volume_muted"] = body["is_volume_muted"]
        elif service == "start":
            s["state"] = "cleaning"
        elif service == "open_cover":
            s["state"], a["current_position"] = "open", 100
        if (s["state"], json.dumps(a, sort_keys=True)) != old or domain in ("scene", "script"):
            s["last_updated"] = _now()
            changed.append(copy.deepcopy(s))
    return changed


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, code, obj, text=False):
        body = (obj if text else json.dumps(obj)).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain" if text else "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            return raw.decode("latin-1")

    def _authed(self):
        return self.headers.get("Authorization") == "Bearer " + TOKEN

    def _other(self):
        self.server.log.append(("REST", self.command, self.path, self._body()))
        self._json(405, {"message": "not allowed in fake"})

    do_DELETE = do_PUT = do_PATCH = _other

    def do_GET(self):
        path = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)
        self.server.log.append(("REST", "GET", path, None))
        if path == "/api/websocket":
            return self._websocket()
        if not self._authed():
            return self._json(401, {"message": "unauthorized"})
        with self.server.lock:
            if path == "/api/states":
                return self._json(200, list(self.server.states.values()))
            m = re.fullmatch(r"/api/states/(.+)", path)
            if m:
                s = self.server.states.get(m.group(1))
                return self._json(200, s) if s else self._json(404, {"message": "Entity not found."})
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query, keep_blank_values=True))
        m = re.fullmatch(r"/api/history/period/(.+)", path)
        if m:
            self.server.queries.append(("history", m.group(1), q))
            eid = q.get("filter_entity_id")
            rows = history_rows(eid, m.group(1), "minimal_response" not in q) if eid else []
            return self._json(200, [rows] if rows else [])
        m = re.fullmatch(r"/api/logbook/(.+)", path)
        if m:
            self.server.queries.append(("logbook", m.group(1), q))
            rows = logbook_rows()
            if q.get("entity"):
                rows = [r for r in rows if r.get("entity_id") == q["entity"]]
            return self._json(200, rows)
        m = re.fullmatch(r"/api/config/script/config/(.+)", path)
        if m:
            c = SCRIPT_CFG.get(m.group(1))
            return self._json(200, c) if c else self._json(404, {"message": "Resource not found"})
        return self._json(404, {"message": "not found"})

    def do_POST(self):
        path = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)
        body = self._body()
        self.server.log.append(("REST", "POST", path, body))
        if not self._authed():
            return self._json(401, {"message": "unauthorized"})
        if path == "/api/template":
            m = re.fullmatch(
                r"\{\{ \{'url': device_attr\(device_id\('([a-z0-9_.]+)'\), 'configuration_url'\)\} \| tojson \}\}",
                str((body or {}).get("template", "")),
            )
            if m:
                return self._json(200, json.dumps({"url": DEVICE_URLS.get(m.group(1))}), text=True)
            return self._json(200, json.dumps(AREAS), text=True)
        m = re.fullmatch(r"/api/services/([a-z_]+)/([a-z0-9_]+)", path)
        if m:
            d, s = m.groups()
            if (d, s) in self.server.fail:
                return self._json(400, {"message": f"Invalid data for {d}.{s} at 198.51.100.7"})
            self.server.calls.append((d, s, body))
            if d == "todo":
                rr = "return_response" in urllib.parse.urlparse(self.path).query
                if (s == "get_items") != rr:
                    return self._json(400, {"message": "return_response mismatch"})
                with self.server.lock:
                    code, ans = todo_effect(self.server, s, body or {})
                return self._json(code, ans)
            with self.server.lock:
                changed = apply_effect(self.server.states, d, s, body) if self.server.effects else []
            return self._json(200, changed)
        return self._json(404, {"message": "not found"})

    # -- websocket (auth, trace/list, trace/get; anything else is logged and answered unknown_command)
    def _ws_send(self, obj):
        data = json.dumps(obj).encode()
        n = len(data)
        hdr = bytes([0x81]) + (
            bytes([n])
            if n < 126
            else bytes([126]) + struct.pack("!H", n)
            if n < 65536
            else bytes([127]) + struct.pack("!Q", n)
        )
        self.wfile.write(hdr + data)
        self.wfile.flush()

    def _ws_recv(self):
        b1, b2 = self.rfile.read(2)
        n = b2 & 0x7F
        if n == 126:
            n = struct.unpack("!H", self.rfile.read(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", self.rfile.read(8))[0]
        mask = self.rfile.read(4) if b2 & 0x80 else b"\0\0\0\0"
        return b1 & 0x0F, bytes(c ^ mask[i % 4] for i, c in enumerate(self.rfile.read(n)))

    def _websocket(self):
        self.close_connection = True
        key = self.headers.get("Sec-WebSocket-Key", "")
        acc = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", acc)
        self.end_headers()
        self.wfile.flush()
        self._ws_send({"type": "auth_required", "ha_version": "2026.10.2"})
        while True:
            try:
                op, data = self._ws_recv()
            except (ValueError, OSError):
                return
            if op == 0x8:
                self.server.ws_log.append(("close", None))
                self.close_connection = True
                return
            if op in (0x9, 0xA):
                continue
            msg = json.loads(data)
            self.server.ws_log.append((msg.get("type"), {k: v for k, v in msg.items() if k != "access_token"}))
            if msg.get("type") == "auth":
                ok = msg.get("access_token") == TOKEN
                self._ws_send({"type": "auth_ok" if ok else "auth_invalid", "ha_version": "2026.10.2"})
                if not ok:
                    return
                self.wfile.write(bytes([0x89, 4]) + b"ping")  # HA may ping: the client must pong, not choke
                self.wfile.flush()
                continue
            t, mid = msg.get("type"), msg.get("id")
            runs = self.server.traces.get((msg.get("domain"), msg.get("item_id")), [])
            if t == "trace/list":
                self._ws_send({"id": mid, "type": "event", "event": {"noise": True}})
                self._ws_send({"id": mid, "type": "result", "success": True, "result": [short_trace(r) for r in runs]})
            elif t == "trace/get":
                hit = [r for r in runs if r["run_id"] == msg.get("run_id")]
                self._ws_send(
                    {
                        "id": mid,
                        "type": "result",
                        "success": bool(hit),
                        "result": copy.deepcopy(hit[0]) if hit else None,
                        **({} if hit else {"error": {"code": "not_found", "message": "no trace"}}),
                    }
                )
            else:
                self._ws_send(
                    {
                        "id": mid,
                        "type": "result",
                        "success": False,
                        "error": {"code": "unknown_command", "message": "Unknown command."},
                    }
                )


class FakeHA:
    def __init__(self, effects=True):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.srv.daemon_threads = True
        self.srv.log, self.srv.calls, self.srv.fail = [], [], set()
        self.srv.effects = effects
        self.srv.lock = threading.Lock()
        self.srv.states = {s["entity_id"]: s for s in fixtures()}
        self.srv.todo, self.srv.uid = todo_fixtures(), 0
        self.srv.queries, self.srv.ws_log, self.srv.traces = [], [], trace_runs()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True)

    @property
    def log(self):
        return self.srv.log

    @property
    def calls(self):
        return self.srv.calls

    def posts(self):
        return [x for x in self.srv.log if x[1] == "POST" and x[2].startswith("/api/services/")]

    def __enter__(self):
        self.t.start()
        return self

    def __exit__(self, *a):
        self.srv.shutdown()
        self.srv.server_close()
