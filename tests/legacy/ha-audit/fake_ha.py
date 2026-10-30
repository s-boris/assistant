"""Fake Home Assistant (REST + websocket) with synthetic fixtures for ha-audit tests.

Records every request so tests can prove ha-audit only sends allowlisted reads. Fixtures carry
privacy canaries (coordinates, address, SSID, BSSID, IPs, BT names, notification text, media
title, email) that must never show up in ha-audit output.
"""

import base64
import datetime as dt
import hashlib
import json
import re
import struct
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = "fake-token-CANARY-0123456789abcdefXYZ"
NOW = dt.datetime(2026, 10, 15, 21, 0, tzinfo=dt.UTC)
CANARIES = [
    "12.34567",
    "65.43219",
    "Musterstrasse",
    "1234",
    "ExampleNet-5G",
    "aa:bb:cc:dd:ee:ff",
    "AA:BB:CC:DD:EE:01",
    "198.51.100.",
    "203.0.113.77",
    "Your package",
    "Secret Song",
    "alex@example.com",
    "AirPods",
    "Zone-Canary-Office",
    "Garage-Canary",
    TOKEN,
    "fe80::1:2:3:4",
]


def ago(days=0, hours=0):
    return (NOW - dt.timedelta(days=days, hours=hours)).isoformat()


HA_START = ago(days=2)


def st(eid, state, attrs=None, changed=None, updated=None):
    return {
        "entity_id": eid,
        "state": state,
        "attributes": attrs or {},
        "last_changed": changed or HA_START,
        "last_updated": updated or changed or HA_START,
    }


def reg(eid, platform, entry=None, device=None, disabled=None, rid=None, area=None):
    return {
        "entity_id": eid,
        "id": rid or hashlib.md5(eid.encode()).hexdigest(),
        "platform": platform,
        "config_entry_id": entry,
        "device_id": device,
        "area_id": area,
        "disabled_by": disabled,
        "hidden_by": None,
        "entity_category": None,
        "unique_id": "uid-aa:bb:cc:dd:ee:ff-" + eid,
        "original_name": "Name at Musterstrasse 12",
    }


ENTRIES = [
    {
        "entry_id": "E_S24",
        "domain": "mobile_app",
        "title": "Galaxy S24 Ultra",
        "state": "loaded",
        "source": "user",
        "disabled_by": None,
    },
    {
        "entry_id": "E_S20",
        "domain": "mobile_app",
        "title": "Galaxy S20 FE",
        "state": "loaded",
        "source": "user",
        "disabled_by": None,
    },
    {
        "entry_id": "E_PIX",
        "domain": "mobile_app",
        "title": "Pixel 10 Pro",
        "state": "loaded",
        "source": "user",
        "disabled_by": None,
    },
    {
        "entry_id": "E_HUE",
        "domain": "hue",
        "title": "Hue Bridge 198.51.100.40",
        "state": "loaded",
        "source": "zeroconf",
        "disabled_by": None,
    },
    {
        "entry_id": "E_TUYA",
        "domain": "tuya",
        "title": "tuya",
        "state": "setup_retry",
        "source": "user",
        "disabled_by": None,
        "reason": "Connection to 198.51.100.50 failed: 'Musterstrasse 12'",
    },
    {
        "entry_id": "E_IGN",
        "domain": "elgato",
        "title": "Key Light 198.51.100.61",
        "state": "not_loaded",
        "source": "ignore",
        "disabled_by": None,
    },
    {
        "entry_id": "E_WORK",
        "domain": "workday",
        "title": "Workday Sensor",
        "state": "loaded",
        "source": "user",
        "disabled_by": None,
    },
    {
        "entry_id": "E_RB",
        "domain": "radio_browser",
        "title": "Radio Browser",
        "state": "loaded",
        "source": "onboarding",
        "disabled_by": None,
    },
    {
        "entry_id": "E_GCAL",
        "domain": "google",
        "title": "alex@example.com",
        "state": "loaded",
        "source": "user",
        "disabled_by": None,
    },
    {
        "entry_id": "E_BLE",
        "domain": "private_ble_device",
        "title": "Alex AirPods Pro",
        "state": "loaded",
        "source": "user",
        "disabled_by": None,
    },
    {
        "entry_id": "E_SHELLY",
        "domain": "shelly",
        "title": "Shelly Plug",
        "state": "not_loaded",
        "source": "user",
        "disabled_by": "user",
    },
    {
        "entry_id": "E_DEAD",
        "domain": "netatmo",
        "title": "Netatmo",
        "state": "loaded",
        "source": "user",
        "disabled_by": None,
    },
]

DEVICES = [
    {
        "id": "D_S24",
        "name": "Galaxy S24 Ultra",
        "model": "SM-S928B",
        "manufacturer": "samsung",
        "config_entries": ["E_S24"],
    },
    {
        "id": "D_S20",
        "name": "Galaxy S20 FE",
        "model": "SM-G780G",
        "manufacturer": "samsung",
        "config_entries": ["E_S20"],
    },
    {
        "id": "D_PIX",
        "name": "Pixel 10 Pro",
        "model": "Pixel 10 Pro",
        "manufacturer": "Google",
        "config_entries": ["E_PIX"],
    },
    {
        "id": "D_BRIDGE",
        "name": "Hue Bridge",
        "model": "BSB002",
        "manufacturer": "Signify",
        "config_entries": ["E_HUE"],
        "connections": [["mac", "aa:bb:cc:dd:ee:ff"]],
    },
    {
        "id": "D_LAMP",
        "name": "Wohnzimmer Lampe",
        "model": "LCT015",
        "config_entries": ["E_HUE"],
        "via_device_id": "D_BRIDGE",
        "area_id": "living",
    },
    {"id": "D_EMPTY", "name": "Tuya Plug", "model": "TS011F", "config_entries": ["E_TUYA"]},
    {"id": "D_TSENS", "name": "Tuya Sensor", "model": "TS0201", "config_entries": ["E_TUYA"], "area_id": "kitchen"},
    {
        "id": "D_BT",
        "name": "Alex AirPods Pro",
        "model": "AirPods",
        "manufacturer": "Apple",
        "config_entries": ["E_BLE"],
        "connections": [["bluetooth", "AA:BB:CC:DD:EE:01"]],
    },
    {"id": "D_NET", "name": "Netatmo Station", "model": "NAMain", "config_entries": ["E_DEAD"]},
    {"id": "D_GONE", "name": "Old Thing", "model": "X", "config_entries": ["E_REMOVED"]},
    {"id": "D_BULB1", "name": "Hue bulb", "model": "LCA004", "config_entries": ["E_HUE"], "via_device_id": "D_BRIDGE"},
    {"id": "D_BULB2", "name": "Hue bulb", "model": "LCA004", "config_entries": ["E_HUE"], "via_device_id": "D_BRIDGE"},
]
for _d in DEVICES:
    for k in ("model", "manufacturer", "area_id", "via_device_id", "name_by_user", "disabled_by", "entry_type"):
        _d.setdefault(k, None)
    _d.setdefault("connections", [])
    _d.setdefault("identifiers", [["x", "serial-aa:bb:cc:dd:ee:ff"]])

AREAS = [{"area_id": "living", "name": "Wohnzimmer"}, {"area_id": "kitchen", "name": "Küche"}]

ENTITIES = [
    reg("device_tracker.galaxy_s24_ultra", "mobile_app", "E_S24", "D_S24"),
    reg("sensor.galaxy_s24_ultra_battery_level", "mobile_app", "E_S24", "D_S24"),
    reg("sensor.galaxy_s24_ultra_geocoded_location", "mobile_app", "E_S24", "D_S24"),
    reg("device_tracker.galaxy_s20_fe", "mobile_app", "E_S20", "D_S20"),
    reg("sensor.galaxy_s20_fe_battery_level", "mobile_app", "E_S20", "D_S20"),
    reg("device_tracker.pixel_10_pro", "mobile_app", "E_PIX", "D_PIX"),
    reg("sensor.pixel_10_pro_battery_level", "mobile_app", "E_PIX", "D_PIX"),
    reg("sensor.pixel_10_pro_wifi_connection", "mobile_app", "E_PIX", "D_PIX"),
    reg("sensor.pixel_10_pro_wifi_bssid", "mobile_app", "E_PIX", "D_PIX"),
    reg("sensor.pixel_10_pro_last_notification", "mobile_app", "E_PIX", "D_PIX"),
    reg("sensor.pixel_10_pro_public_ip", "mobile_app", "E_PIX", "D_PIX"),
    reg("light.wohnzimmer", "hue", "E_HUE", "D_LAMP", area="living"),
    reg("light.wohnzimmer_2", "hue", "E_HUE", "D_LAMP", rid="0123456789abcdef0123456789abcdef"),
    reg("sensor.tuya_temp", "tuya", "E_TUYA", "D_TSENS"),
    reg("sensor.tuya_humidity", "tuya", "E_TUYA", "D_TSENS"),
    reg("light.hue_bulb", "hue", "E_HUE", "D_BULB1"),
    reg("light.hue_bulb_2", "hue", "E_HUE", "D_BULB2"),
    reg("switch.old_plug", "old_integration", "E_REMOVED"),
    reg("sensor.orphan_device", "template", None, "D_VANISHED"),
    reg("sensor.shelly_power", "shelly", "E_SHELLY", None, disabled="config_entry"),
    reg("sensor.hue_diag", "hue", "E_HUE", "D_LAMP", disabled="integration"),
    reg("sensor.hue_user_off", "hue", "E_HUE", "D_LAMP", disabled="user"),
    reg("sensor.ble_aa_bb_cc_dd_ee_01_rssi", "private_ble_device", "E_BLE", "D_BT"),
    reg("calendar.alex_example_com", "google", "E_GCAL"),
    reg("sensor.netatmo_temp", "netatmo", "E_DEAD", "D_NET"),
    reg("media_player.tv", "cast", None),
    reg("binary_sensor.door", "zha", None),
]
ENTITIES += [reg(f"sensor.pad_{i:03d}", "template", None) for i in range(600)]  # > 64 KiB ws frame

PRIV = {"latitude": 12.34567, "longitude": 65.43219, "gps_accuracy": 12, "altitude": 410.5}
STATES = [
    st(
        "person.alex",
        "Zone-Canary-Office",
        dict(
            PRIV,
            device_trackers=[
                "device_tracker.galaxy_s24_ultra",
                "device_tracker.galaxy_s20_fe",
                "device_tracker.pixel_10_pro",
            ],
            source="device_tracker.pixel_10_pro",
            friendly_name="Alex",
        ),
    ),
    st("device_tracker.galaxy_s24_ultra", "Zone-Canary-Office", dict(PRIV, ip="198.51.100.23")),
    st("device_tracker.galaxy_s20_fe", "unavailable", dict(PRIV)),
    st("device_tracker.pixel_10_pro", "home", dict(PRIV, ssid="ExampleNet-5G")),
    st("zone.home", "1", dict(PRIV, friendly_name="Home Musterstrasse 12")),
    st("sensor.galaxy_s24_ultra_battery_level", "54", updated=HA_START),
    st(
        "sensor.galaxy_s24_ultra_geocoded_location",
        "Musterstrasse 12, 1234 Exampletown",
        {"Location": [12.34567, 65.43219], "Thoroughfare": "Musterstrasse", "Postal Code": "1234"},
    ),
    st("sensor.galaxy_s20_fe_battery_level", "12"),
    st("sensor.pixel_10_pro_battery_level", "80", changed=ago(hours=1)),
    st("sensor.pixel_10_pro_wifi_connection", "ExampleNet-5G", changed=ago(hours=3)),
    st("sensor.pixel_10_pro_wifi_bssid", "aa:bb:cc:dd:ee:ff", changed=ago(hours=3)),
    st("sensor.pixel_10_pro_last_notification", "Your package from Musterstrasse 12", {"android.text": "Your package"}),
    st("sensor.pixel_10_pro_public_ip", "203.0.113.77"),
    st("light.wohnzimmer", "unavailable", {"restored": True, "friendly_name": "Wohnzimmer"}),
    st("light.wohnzimmer_2", "on", {"friendly_name": "Wohnzimmer"}),
    st("sensor.tuya_temp", "unavailable"),
    st("sensor.tuya_humidity", "unavailable", {"restored": True}),
    st("light.hue_bulb", "on"),
    st("light.hue_bulb_2", "off"),
    st("switch.old_plug", "unavailable", {"restored": True}),
    st("sensor.orphan_device", "3"),
    st("sensor.legacy_yaml", "unavailable"),
    st("sensor.ble_aa_bb_cc_dd_ee_01_rssi", "unavailable", {"source": "AA:BB:CC:DD:EE:01"}, changed=ago(days=20)),
    st("calendar.alex_example_com", "off", {"message": "Dentist Musterstrasse 12"}),
    st("sensor.netatmo_temp", "unavailable"),
    st("media_player.tv", "playing", {"media_title": "Secret Song", "entity_picture": "/api/x"}),
    st("binary_sensor.door", "unknown"),
    st("button.restart", "unknown"),
    st("automation.morning_lights", "on", {"id": "1001", "last_triggered": ago(days=1)}),
    st("automation.never_ran", "on", {"id": "1002", "last_triggered": None}),
    st("automation.old_one", "off", {"id": "1003", "last_triggered": ago(days=200)}),
    st("automation.broken", "unavailable", {"id": "1004", "restored": True}),
    st("automation.erroring", "on", {"id": "1005", "last_triggered": ago(hours=2)}),
    st("automation.bp_motion", "on", {"id": "1006", "last_triggered": ago(days=120)}),
    st("automation.yaml_only", "on", {"last_triggered": ago(days=3)}),
    st("script.goodnight", "off", {"last_triggered": None}),
    st(
        "update.home_assistant_core_update",
        "on",
        {
            "installed_version": "2026.10.1",
            "latest_version": "2026.10.2",
            "title": "Home Assistant Core",
            "release_summary": "x",
        },
    ),
    st("update.home_assistant_operating_system_update", "off", {"installed_version": "17.0", "latest_version": "17.0"}),
    st(
        "update.esphome_update",
        "on",
        {"installed_version": "2026.9.0", "latest_version": "2026.10.0", "title": "ESPHome"},
    ),
]
STATES += [st(f"sensor.pad_{i:03d}", str(i)) for i in range(600)]

SERVICES = [
    {
        "domain": "notify",
        "services": {
            "mobile_app_galaxy_s24_ultra": {},
            "mobile_app_galaxy_s20_fe": {},
            "mobile_app_pixel_10_pro": {},
            "notify": {},
        },
    },
    {"domain": "light", "services": {"turn_on": {}, "turn_off": {}}},
    {"domain": "script", "services": {"turn_on": {}, "goodnight": {}}},
]

AUTOMATION_CFG = {
    "1001": {
        "id": "1001",
        "alias": "Morning",
        "triggers": [{"trigger": "time", "at": "07:00"}],
        "actions": [
            {"action": "light.turn_on", "target": {"entity_id": "light.wohnzimmer"}},
            {"action": "notify.mobile_app_galaxy_s20_fe", "data": {"message": "Musterstrasse 12"}},
        ],
    },
    "1002": {
        "id": "1002",
        "trigger": [{"platform": "state", "entity_id": "sensor.does_not_exist"}],
        "action": [
            {"service": "light.turn_off", "data": {"entity_id": ["light.wohnzimmer_2"]}},
            {
                "device_id": "DEVGONE2",
                "domain": "light",
                "type": "turn_on",
                "entity_id": "ffffffffffffffffffffffffffffffff",
            },
            {"action": "notify.mobile_app_old_tablet"},
        ],
    },
    "1003": {"id": "1003", "triggers": [{"trigger": "state", "entity_id": "sensor.tuya_temp"}], "actions": []},
    "1004": {"id": "1004", "triggers": "broken"},
    "1005": {
        "id": "1005",
        "triggers": [{"trigger": "state", "entity_id": "binary_sensor.door"}],
        "actions": [{"action": "script.goodnight"}],
    },
    "1006": {
        "id": "1006",
        "use_blueprint": {
            "path": "motion.yaml",
            "input": {
                "motion_entity": "binary_sensor.gone_motion",
                "light_target": {"entity_id": "0123456789abcdef0123456789abcdef"},
            },
        },
    },
}
SCRIPT_CFG = {
    "goodnight": {
        "sequence": [
            {"action": "light.turn_off", "target": {"entity_id": "light.wohnzimmer_2"}},
            {"action": "notify.mobile_app_galaxy_s24_ultra", "data": {"message": "x"}},
            {"action": "light.turn_off", "target": {"entity_id": "sensor.shelly_power"}},
        ]
    }
}

ISSUES = {
    "issues": [
        {
            "domain": "homeassistant",
            "issue_id": "deprecated_yaml_tuya",
            "severity": "warning",
            "translation_key": "deprecated_yaml",
            "is_fixable": False,
            "ignored": False,
            "created": ago(days=40),
            "breaks_in_ha_version": "2026.12.0",
            "translation_placeholders": {"host": "198.51.100.50"},
        },
        {
            "domain": "hue",
            "issue_id": "old",
            "severity": "error",
            "translation_key": "x",
            "is_fixable": True,
            "ignored": True,
            "created": ago(days=5),
        },
    ]
}
FLOWS = [
    {
        "flow_id": "f1",
        "handler": "esphome",
        "step_id": "discovery_confirm",
        "context": {"source": "zeroconf", "title_placeholders": {"name": "Garage-Canary 198.51.100.60"}},
    }
]
TRACES = {
    "automation": [
        {
            "domain": "automation",
            "item_id": "1005",
            "state": "stopped",
            "script_execution": "error",
            "error": "Error rendering 'Musterstrasse 12' for 198.51.100.9",
            "timestamp": {"start": ago(hours=2)},
            "trigger": "state of binary_sensor.door",
        },
        {
            "domain": "automation",
            "item_id": "1001",
            "state": "stopped",
            "script_execution": "finished",
            "timestamp": {"start": ago(days=1)},
        },
    ],
    "script": [],
}
SUPERVISOR = {
    "/core/info": {
        "version": "2026.10.1",
        "version_latest": "2026.10.2",
        "update_available": True,
        "ip_address": "203.0.113.1",
    },
    "/os/info": {"version": "17.0", "version_latest": "17.0", "update_available": False},
    "/supervisor/info": {"version": "2026.10.0", "version_latest": "2026.10.0", "update_available": False},
    "/resolution/info": {
        "unsupported": [],
        "unhealthy": [],
        "issues": [
            {"type": "free_space", "context": "system", "reference": None},
            {"type": "no_current_backup", "context": "system", "reference": None},
        ],
        "suggestions": [{"type": "clear_full_backup", "context": "system"}],
    },
    "/addons": {
        "addons": [
            {
                "name": "File editor",
                "slug": "core_configurator",
                "state": "stopped",
                "version": "5.8.0",
                "version_latest": "5.8.0",
                "update_available": False,
            },
            {
                "name": "Mosquitto broker",
                "slug": "core_mosquitto",
                "state": "started",
                "version": "6.4.0",
                "version_latest": "6.5.0",
                "update_available": True,
            },
            {
                "name": "Old Tool",
                "slug": "local_old",
                "state": "stopped",
                "version": "1.0",
                "version_latest": "1.0",
                "update_available": False,
            },
        ]
    },
    "/addons/core_configurator/info": {"boot": "manual", "state": "stopped", "network": {"8080/tcp": None}},
    "/addons/local_old/info": {"boot": "auto", "state": "stopped"},
}
HISTORY = {  # entity -> list of (state, days_ago)
    "light.wohnzimmer": [("on", 25), ("unavailable", 20)],
    "sensor.tuya_temp": [("unavailable", 30)],
    "binary_sensor.door": [("unknown", 30)],
    "switch.old_plug": [("off", 30), ("unavailable", 9)],
    "sensor.netatmo_temp": [("21.5", 30), ("unavailable", 12)],
    "sensor.galaxy_s20_fe_battery_level": [("12", 30)],
    "sensor.galaxy_s24_ultra_battery_level": [("60", 30), ("54", 45 / 24 * 0 + 14)],
    "sensor.pixel_10_pro_battery_level": [("90", 30), ("80", 0.04)],
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _write_methods(self):
        self.server.log.append(("REST", self.command, self.path))
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        self._json(405, {"message": "not allowed in fake"})

    do_POST = do_DELETE = do_PUT = do_PATCH = _write_methods

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(u.path)
        q = urllib.parse.parse_qs(u.query, keep_blank_values=True)
        if path == "/api/websocket":
            self.server.log.append(("REST", "GET", "/api/websocket"))
            return self._websocket()
        self.server.log.append(("REST", "GET", path))
        if self.headers.get("Authorization") != "Bearer " + TOKEN:
            return self._json(401, {"message": "unauthorized"})
        if path == "/api/":
            return self._json(200, {"message": "API running."})
        if path == "/api/config":
            return self._json(
                200,
                dict(
                    PRIV,
                    version="2026.10.1",
                    location_name="Musterstrasse 12",
                    components=["hue", "tuya"],
                    internal_url="http://198.51.100.7:8123",
                ),
            )
        if path == "/api/states":
            return self._json(200, STATES)
        if path == "/api/services":
            return self._json(200, SERVICES)
        if path == "/api/config/config_entries/entry":
            return self._json(200, ENTRIES)
        m = re.fullmatch(r"/api/config/automation/config/(.+)", path)
        if m:
            c = AUTOMATION_CFG.get(m.group(1))
            return self._json(200, c) if c else self._json(404, {"message": "Resource not found"})
        m = re.fullmatch(r"/api/config/script/config/(.+)", path)
        if m:
            c = SCRIPT_CFG.get(m.group(1))
            return self._json(200, c) if c else self._json(404, {"message": "Resource not found"})
        if path.startswith("/api/history/period/"):
            self.server.history_queries.append(path)
            ids = (q.get("filter_entity_id") or [""])[0].split(",")
            out = []
            for eid in ids:
                if eid in HISTORY:
                    ser = [{"state": s, "last_changed": ago(days=dd)} for s, dd in HISTORY[eid]]
                    ser[0] = dict(ser[0], entity_id=eid, attributes={})
                    out.append(ser)
            return self._json(200, out)
        return self._json(404, {"message": "not found"})

    # -- websocket
    def _ws_send(self, obj):
        data = json.dumps(obj).encode()
        n = len(data)
        hdr = bytes([0x81])
        if n < 126:
            hdr += bytes([n])
        elif n < 65536:
            hdr += bytes([126]) + struct.pack("!H", n)
        else:
            hdr += bytes([127]) + struct.pack("!Q", n)
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
        data = bytes(c ^ mask[i % 4] for i, c in enumerate(self.rfile.read(n)))
        return b1 & 0x0F, data

    def _websocket(self):
        key = self.headers.get("Sec-WebSocket-Key", "")
        acc = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", acc)
        self.end_headers()
        self.wfile.flush()
        self._ws_send({"type": "auth_required", "ha_version": "2026.10.1"})
        authed = False
        while True:
            try:
                op, data = self._ws_recv()
            except (ValueError, OSError):
                return
            if op == 0x8:
                return
            if op in (0x9, 0xA):
                self.server.log.append(("WSCTRL", op, None))
                continue
            msg = json.loads(data)
            self.server.log.append(
                ("WS", msg.get("type"), {k: v for k, v in msg.items() if k not in ("access_token",)})
            )
            if msg.get("type") == "auth":
                authed = msg.get("access_token") == TOKEN
                self._ws_send({"type": "auth_ok" if authed else "auth_invalid", "ha_version": "2026.10.1"})
                if not authed:
                    return
                if self.server.send_ping:
                    self.wfile.write(bytes([0x89, 4]) + b"ping")
                    self.wfile.flush()
                continue
            t, mid = msg.get("type"), msg.get("id")
            result = {
                "config/entity_registry/list": ENTITIES,
                "config/device_registry/list": DEVICES,
                "config/area_registry/list": AREAS,
                "config_entries/get": ENTRIES,
                "config_entries/flow/progress": FLOWS,
                "repairs/list_issues": ISSUES,
            }.get(t)
            if t == "trace/list":
                result = TRACES.get(msg.get("domain"), [])
            elif t == "supervisor/api":
                result = SUPERVISOR.get(msg.get("endpoint"))
            if result is None and t not in ("trace/list",):
                self._ws_send({"id": mid, "type": "result", "success": True, "result": None})  # fake accepts anything
            else:
                self._ws_send({"id": mid, "type": "result", "success": True, "result": result})


class FakeHA:
    def __init__(self, send_ping=True):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.srv.daemon_threads = True
        self.srv.log = []
        self.srv.history_queries = []
        self.srv.send_ping = send_ping
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True)

    @property
    def log(self):
        return self.srv.log

    def __enter__(self):
        self.t.start()
        return self

    def __exit__(self, *a):
        self.srv.shutdown()
        self.srv.server_close()
