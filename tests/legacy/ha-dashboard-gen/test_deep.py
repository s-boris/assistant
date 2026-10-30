"""Deep offline tests for ha-dashboard-gen: every card/section from fixture HA registries, empty and missing
areas, unavailable entities, the write guards, helper creation, main() apply/refuse/verify paths and HA errors.
A fake ha-audit module (in-memory websocket + stubbed urlopen) stands in for HA: no network, no real
dashboard, temp state dir. GEN_BIN=/path tests a copy (default: the repo's scripts/ha-dashboard-gen)."""

import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import types
import unittest
import urllib.error
from unittest import mock

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_dashboard_gen import g  # noqa: E402  (loads GEN_BIN or the repo's scripts/ha-dashboard-gen)

TOKEN = "SECRET-TOKEN-must-never-print"
BASE = "http://ha.test:8123"


def st(eid, state="on", **attrs):
    return {"entity_id": eid, "state": state, "attributes": attrs}


class HAError(Exception):
    pass


class FakeHA:
    """In-memory HA: registries, states, template config entries, dashboards and lovelace configs."""

    def __init__(self):
        self.areas = [
            {"area_id": "office", "name": "Office", "icon": None},
            {"area_id": "living", "name": "Wohnzimmer", "icon": None},
            {"area_id": "zz_attic", "name": "Attic", "icon": None},
            {"area_id": "kitchen", "name": "Kitchen", "icon": "mdi:chef-hat"},
            {"area_id": "bed", "name": "Bedroom", "icon": None},
            {"area_id": "empty", "name": "Bathroom", "icon": None},
        ]
        self.devices = [
            {"id": "fp2", "area_id": "living"},
            {"id": "fp2b", "area_id": "living"},
            {"id": "hue1", "area_id": "kitchen"},
            {"id": "pir", "area_id": "bed"},
        ]
        self.ents = [
            {"entity_id": "light.kitchen_ceiling", "device_id": "hue1"},
            {"entity_id": "light.wohnzimmer_lamp", "area_id": "living"},
            {"entity_id": "light.strip", "area_id": "living", "platform": "wled"},
            {"entity_id": "light.bulb_1", "area_id": "bed", "hidden_by": "user"},
            {"entity_id": "light.bulb_2", "area_id": "bed", "hidden_by": "user"},
            {"entity_id": "light.bed_lamp", "area_id": "bed", "platform": "group"},
            {"entity_id": "light.all_living", "area_id": "living", "platform": "group"},
            {"entity_id": "light.office_1", "area_id": "office"},
            {"entity_id": "light.office_2", "area_id": "office"},
            {"entity_id": "light.attic_old", "area_id": "zz_attic", "disabled_by": "user"},
            {"entity_id": "light.cfg_led", "area_id": "zz_attic", "entity_category": "config"},
            {
                "entity_id": "binary_sensor.fp2_presence_sensor_2",
                "device_id": "fp2",
                "original_device_class": "presence",
            },
            {
                "entity_id": "binary_sensor.fp2_presence_sensor_1",
                "device_id": "fp2",
                "original_device_class": "presence",
            },
            {"entity_id": "binary_sensor.fp2b_presence", "device_id": "fp2b", "device_class": "occupancy"},
            {"entity_id": "binary_sensor.pir_motion", "device_id": "pir", "original_device_class": "motion"},
            {"entity_id": "binary_sensor.no_area_presence", "original_device_class": "presence"},
            {"entity_id": "sensor.lights_on_count", "config_entry_id": "tmpl1"},
            {"entity_id": "scene.kitchen_cook", "area_id": "kitchen"},
            {"entity_id": "scene.bath_spa", "area_id": "empty"},
        ]
        self.states = [
            st(
                "light.kitchen_ceiling",
                friendly_name="Signify Netherlands B.V. Kitchen Ceiling",
                supported_color_modes=["color_temp"],
            ),
            st(
                "light.wohnzimmer_lamp", "unavailable", friendly_name="Wohnzimmer Lamp", supported_color_modes=["onoff"]
            ),
            st("light.strip", "off", friendly_name="Strip", supported_color_modes=["rgb"]),
            st("light.bulb_1", friendly_name="Bulb 1"),
            st("light.bulb_2", friendly_name="Bulb 2"),
            st(
                "light.bed_lamp",
                friendly_name="Bedroom Lamp",
                entity_id=["light.bulb_1", "light.bulb_2"],
                supported_color_modes=["brightness"],
            ),
            st("light.all_living", friendly_name="All living", entity_id=["light.wohnzimmer_lamp", "light.strip"]),
            st("light.office_1", friendly_name="Desk"),
            st("light.office_2", friendly_name="Desk"),
            st("light.attic_old", friendly_name="Old"),
            st("light.cfg_led", friendly_name="Status LED"),
            st("light.unregistered", friendly_name="Hall Bulb", supported_color_modes=["brightness"]),
            st("binary_sensor.fp2_presence_sensor_1", "off"),
            st("binary_sensor.fp2_presence_sensor_2", "on"),
            st("binary_sensor.fp2b_presence", "unknown"),
            st("binary_sensor.pir_motion", "off"),
            st("binary_sensor.no_area_presence", "on"),
            st("binary_sensor.attr_only", "on", device_class="occupancy"),
            st("sensor.lights_on_count", "2"),
            st("script.all_lights_off", "off", friendly_name="All lights off"),
            st("scene.kitchen_cook", friendly_name="Cook"),
            st("scene.bath_spa", friendly_name="Spa"),
        ]
        self.entries = [{"entry_id": "tmpl0", "title": "Other helper"}, {"entry_id": "tmpl1", "title": g.COUNT_NAME}]
        self.dashboards = []
        self.configs = {}
        self.calls = []
        self.fail = {}  # msg_type -> exception raised on call
        self.mangle_save = False  # saved config reads back different
        self.closed = 0

    def ctx(self):
        return g.Ctx(FakeWS(self, lambda t, p: None))


class FakeWS:
    def __init__(self, ha, guard):
        self.ha, self.guard = ha, guard

    def call(self, msg_type, **params):
        self.guard(msg_type, params)  # what ha-audit's WSClient.call does with check_ws
        ha = self.ha
        ha.calls.append((msg_type, params))
        if msg_type in ha.fail:
            raise ha.fail[msg_type]
        if msg_type == "config/area_registry/list":
            return copy.deepcopy(ha.areas)
        if msg_type == "config/device_registry/list":
            return copy.deepcopy(ha.devices)
        if msg_type == "config/entity_registry/list":
            return copy.deepcopy(ha.ents)
        if msg_type == "get_states":
            return copy.deepcopy(ha.states)
        if msg_type == "config_entries/get":
            return copy.deepcopy(ha.entries) if params.get("domain") == "template" else []
        if msg_type == "config/area_registry/update":
            next(a for a in ha.areas if a["area_id"] == params["area_id"])["icon"] = params["icon"]
            return None
        if msg_type == "lovelace/dashboards/list":
            return copy.deepcopy(ha.dashboards)
        if msg_type == "lovelace/dashboards/create":
            ha.dashboards.append({"url_path": params["url_path"], "title": params["title"]})
            return None
        if msg_type == "lovelace/config":
            if params["url_path"] not in ha.configs:
                raise HAError("config_not_found")
            return json.loads(json.dumps(ha.configs[params["url_path"]]))
        if msg_type == "lovelace/config/save":
            cfg = json.loads(json.dumps(params["config"]))
            if ha.mangle_save:
                cfg["title"] = "changed by HA"
            ha.configs[params["url_path"]] = cfg
            return None
        raise AssertionError("unexpected websocket call " + msg_type)

    def close(self):
        self.ha.closed += 1


def fake_audit(ha):
    m = types.SimpleNamespace(DEFAULT_URL=BASE, HAError=HAError, check_ws=None, tokens_read=[])

    def read_token(path):
        m.tokens_read.append(path)
        return TOKEN

    m.read_token = read_token
    m.WSClient = lambda base, token: FakeWS(ha, lambda t, p: m.check_ws(t, p))
    return m


def views(cfg):
    return cfg["views"][0]


def sec(cfg, area):
    return next(s for s in views(cfg)["sections"] if s["cards"][0].get("area") == area)


AVAIL = lambda e: {"condition": "state", "entity": e, "state_not": ["unavailable", "unknown"]}  # noqa: E731


# ----------------------------------------------------------------------------- build from fixtures
class BuildTests(unittest.TestCase):
    def setUp(self):
        self.ha = FakeHA()
        self.ctx = self.ha.ctx()
        self.cfg = g.build(self.ctx, "Home", "sensor.lights_on_count", "script.all_lights_off")

    def test_view_shape(self):
        self.assertEqual(set(self.cfg), {"title", "views"})
        v = views(self.cfg)
        self.assertEqual(
            (v["type"], v["path"], v["max_columns"], v["icon"], v["title"]), ("sections", "home", 3, "mdi:home", "Home")
        )
        json.dumps(self.cfg)  # serialisable

    def test_room_order_and_only_areas_with_lights(self):
        heads = [s["cards"][0].get("area") or s["cards"][0].get("heading") for s in views(self.cfg)["sections"]]
        # top row, living (wohn), kitchen, bed, office, then 'Other lights'; Bathroom (no lights) and the
        # attic (only disabled/config lights) get no section
        self.assertEqual(heads, [None, "living", "kitchen", "bed", "office", "Other lights"])

    def test_area_card_shape(self):
        card = sec(self.cfg, "kitchen")["cards"][0]
        self.assertEqual(
            card,
            {
                "type": "area",
                "area": "kitchen",
                "display_type": "compact",
                "alert_classes": ["occupancy", "presence"],
                "sensor_classes": ["temperature", "humidity"],
                "features": [{"type": "area-controls", "controls": ["light"]}],
                "features_position": "inline",
                "grid_options": {"columns": 12},
            },
        )

    def test_light_tile_shape_and_brightness_feature(self):
        k = sec(self.cfg, "kitchen")["cards"]
        tile = next(c for c in k if c.get("entity") == "light.kitchen_ceiling")
        self.assertEqual(tile["name"], "Hue Kitchen Ceiling")  # maker shortened; area prefix only at the start
        self.assertEqual(g.short_name("Kitchen Ceiling", "Kitchen"), "Ceiling")
        self.assertEqual(tile["features"], [{"type": "light-brightness"}])
        self.assertEqual(tile["features_position"], "bottom")
        self.assertEqual((tile["tap_action"], tile["hold_action"]), ({"action": "toggle"}, {"action": "more-info"}))
        self.assertEqual(tile["visibility"], [AVAIL("light.kitchen_ceiling")])
        lamp = next(c for c in sec(self.cfg, "living")["cards"] if c.get("entity") == "light.wohnzimmer_lamp")
        self.assertNotIn("features", lamp)  # onoff only
        self.assertEqual(lamp["name"], "Lamp")

    def test_wled_icon_only_without_own_icon(self):
        strip = next(c for c in sec(self.cfg, "living")["cards"] if c.get("entity") == "light.strip")
        self.assertEqual(strip["icon"], "mdi:led-strip-variant")
        self.ha.states[2]["attributes"]["icon"] = "mdi:custom"
        cfg = g.build(self.ha.ctx(), "Home", None, None)
        strip = next(c for c in sec(cfg, "living")["cards"] if c.get("entity") == "light.strip")
        self.assertNotIn("icon", strip)

    def test_light_groups(self):
        bed = [c.get("entity") for c in sec(self.cfg, "bed")["cards"][1:]]
        self.assertEqual(bed, ["light.bed_lamp"])  # group of hidden bulbs = one tile; bulbs hidden
        living = [c.get("entity") for c in sec(self.cfg, "living")["cards"][1:]]
        self.assertNotIn("light.all_living", living)  # room group: the area toggle covers it
        self.assertEqual(sorted(living), ["light.strip", "light.wohnzimmer_lamp"])

    def test_duplicate_names_numbered_and_natural_order(self):
        office = [(c["name"], c["entity"]) for c in sec(self.cfg, "office")["cards"][1:]]
        self.assertEqual(office, [("Desk 1", "light.office_1"), ("Desk 2", "light.office_2")])

    def test_disabled_hidden_and_config_lights_skipped(self):
        ents = g.refs(self.cfg, {"entities": set(), "areas": set()})["entities"]
        for e in ("light.attic_old", "light.cfg_led", "light.bulb_1", "light.bulb_2", "light.all_living"):
            self.assertNotIn(e, ents)

    def test_other_lights_section(self):
        s = views(self.cfg)["sections"][-1]
        self.assertEqual(
            s["cards"][0],
            {
                "type": "heading",
                "heading": "Other lights",
                "heading_style": "title",
                "icon": "mdi:lightbulb-question-outline",
            },
        )
        self.assertEqual([c["entity"] for c in s["cards"][1:]], ["light.unregistered"])
        self.assertEqual(s["cards"][1]["name"], "Hall Bulb")
        self.assertEqual(s["visibility"], [AVAIL("light.unregistered")])

    def test_section_visibility_single_and_or(self):
        self.assertEqual(sec(self.cfg, "kitchen")["visibility"], [AVAIL("light.kitchen_ceiling")])
        vis = sec(self.cfg, "living")["visibility"]
        self.assertEqual(len(vis), 1)
        self.assertEqual(vis[0]["condition"], "or")
        self.assertCountEqual(vis[0]["conditions"], [AVAIL("light.wohnzimmer_lamp"), AVAIL("light.strip")])

    def test_unavailable_entities_still_generated_but_hidden(self):
        # an unavailable light keeps its tile (shown again when back), guarded by a state_not condition
        lamp = next(c for c in sec(self.cfg, "living")["cards"] if c.get("entity") == "light.wohnzimmer_lamp")
        self.assertEqual(lamp["visibility"], [AVAIL("light.wohnzimmer_lamp")])

    def test_no_view_badges_presence_in_room_headers(self):
        v = views(self.cfg)
        self.assertNotIn("badges", v)  # presence lives in the room headers now
        for s in v["sections"][1:]:
            head = s["cards"][0]
            if head["type"] == "area":
                self.assertEqual(head["alert_classes"], ["occupancy", "presence"])
                self.assertEqual(head["sensor_classes"], ["temperature", "humidity"])
        # no presence entity is referenced anywhere: the area card finds them itself
        refs = g.refs(self.cfg, {"entities": set(), "areas": set()})["entities"]
        self.assertFalse([e for e in refs if e.startswith("binary_sensor.")])
        self.assertEqual(g.presence_rooms(self.ctx), ["Wohnzimmer"])  # motion/no-area/attr-only don't count

    def test_presence_area_without_lights_gets_header_only_section(self):
        self.ha.devices.append({"id": "fp2c", "area_id": "empty"})
        self.ha.ents.append(
            {
                "entity_id": "binary_sensor.bath_presence_sensor_2",
                "device_id": "fp2c",
                "original_device_class": "occupancy",
            }
        )
        self.ha.ents.append(
            {
                "entity_id": "binary_sensor.bath_presence_sensor_1",
                "device_id": "fp2c",
                "original_device_class": "occupancy",
            }
        )
        self.ha.states += [
            st("binary_sensor.bath_presence_sensor_2", "off"),
            st("binary_sensor.bath_presence_sensor_1", "unavailable"),
        ]
        ctx = self.ha.ctx()
        cfg = g.build(ctx, "Home", None, None)
        heads = [s["cards"][0].get("area") or s["cards"][0].get("heading") for s in views(cfg)["sections"]]
        self.assertEqual(heads, [None, "living", "kitchen", "bed", "office", "empty", "Other lights"])
        bath = sec(cfg, "empty")
        self.assertEqual(
            bath["cards"][0],
            {
                "type": "area",
                "area": "empty",
                "display_type": "compact",
                "alert_classes": ["occupancy", "presence"],
                "sensor_classes": ["temperature", "humidity"],
                "grid_options": {"columns": 12},
            },
        )  # no light toggle
        # its scene moves from the loose Scenes row into the room
        self.assertEqual([c.get("entity") for c in bath["cards"]], [None, "scene.bath_spa"])
        top = views(cfg)["sections"][0]["cards"]
        self.assertFalse(any(c.get("heading") == "Scenes" or c.get("entity", "").startswith("scene.") for c in top))
        # shown while a presence zone or the scene is reachable
        vis = bath["visibility"]
        self.assertEqual(vis[0]["condition"], "or")
        self.assertEqual(
            vis[0]["conditions"],
            [
                AVAIL("binary_sensor.bath_presence_sensor_1"),
                AVAIL("binary_sensor.bath_presence_sensor_2"),
                AVAIL("scene.bath_spa"),
            ],
        )
        self.assertEqual(g.presence_rooms(ctx), ["Wohnzimmer", "Bathroom"])
        self.assertIn("section empty: (header only); scenes: Spa", g.summarize(cfg))

    def test_header_only_section_single_presence_sensor(self):
        self.ha.ents.append(
            {"entity_id": "binary_sensor.attic_occ", "area_id": "zz_attic", "original_device_class": "presence"}
        )
        self.ha.states.append(st("binary_sensor.attic_occ", "on"))
        attic = sec(g.build(self.ha.ctx(), "Home", None, None), "zz_attic")
        self.assertEqual(attic["visibility"], [AVAIL("binary_sensor.attic_occ")])
        self.assertEqual(len(attic["cards"]), 1)

    def test_climate_sensor_area_without_lights_gets_header_only_section(self):
        # hallway-like room: lights moved away, presence offline, temperature/humidity still there
        self.ha.ents += [
            {"entity_id": "sensor.attic_temperature", "area_id": "zz_attic", "original_device_class": "temperature"},
            {"entity_id": "sensor.attic_humidity", "area_id": "zz_attic", "device_class": "humidity"},
            {"entity_id": "sensor.attic_battery", "area_id": "zz_attic", "original_device_class": "battery"},
            {
                "entity_id": "sensor.attic_rssi",
                "area_id": "zz_attic",
                "original_device_class": "temperature",
                "entity_category": "diagnostic",
            },
            {"entity_id": "binary_sensor.attic_occ", "area_id": "zz_attic", "original_device_class": "presence"},
            {"entity_id": "sensor.nowhere_temperature", "original_device_class": "temperature"},
        ]
        self.ha.states += [
            st("sensor.attic_temperature", "21.5"),
            st("sensor.attic_humidity", "50"),
            st("sensor.attic_battery", "90"),
            st("sensor.attic_rssi", "30"),
            st("binary_sensor.attic_occ", "unavailable"),
            st("sensor.nowhere_temperature", "5"),
        ]
        attic = sec(g.build(self.ha.ctx(), "Home", None, None), "zz_attic")
        self.assertEqual(
            attic["visibility"][0]["conditions"],
            [AVAIL("binary_sensor.attic_occ"), AVAIL("sensor.attic_humidity"), AVAIL("sensor.attic_temperature")],
        )  # battery, diagnostic and area-less ones ignored
        self.assertNotIn("features", attic["cards"][0])

    def test_climate_only_area_section_without_presence(self):
        self.ha.ents.append(
            {"entity_id": "sensor.attic_temperature", "area_id": "zz_attic", "original_device_class": "temperature"}
        )
        self.ha.states.append(st("sensor.attic_temperature", "21.5"))
        ctx = self.ha.ctx()
        attic = sec(g.build(ctx, "Home", None, None), "zz_attic")
        self.assertEqual(attic["visibility"], [AVAIL("sensor.attic_temperature")])
        self.assertEqual(g.presence_rooms(ctx), ["Wohnzimmer"])  # header without presence isn't listed

    def test_hidden_or_disabled_presence_sensor_gives_no_section(self):
        self.ha.ents.append(
            {
                "entity_id": "binary_sensor.attic_occ",
                "area_id": "zz_attic",
                "original_device_class": "presence",
                "hidden_by": "user",
            }
        )
        self.ha.states.append(st("binary_sensor.attic_occ", "on"))
        heads = [s["cards"][0].get("area") for s in views(g.build(self.ha.ctx(), "Home", None, None))["sections"]]
        self.assertNotIn("zz_attic", heads)

    def test_room_scene_and_scene_of_area_without_lights_is_loose(self):
        k = [c.get("entity") for c in sec(self.cfg, "kitchen")["cards"]]
        self.assertEqual(k, [None, "scene.kitchen_cook", "light.kitchen_ceiling"])
        top = views(self.cfg)["sections"][0]["cards"]
        self.assertEqual([c.get("entity") for c in top if c.get("entity", "").startswith("scene.")], ["scene.bath_spa"])

    def test_top_row_with_helpers(self):
        top = views(self.cfg)["sections"][0]["cards"]
        self.assertEqual(
            top[0],
            {
                "type": "tile",
                "entity": "sensor.lights_on_count",
                "name": "Lights on",
                "icon": "mdi:lightbulb-group",
                "color": "amber",
                "grid_options": {"columns": 6},
            },
        )
        act = {
            "action": "perform-action",
            "perform_action": "script.turn_on",
            "target": {"entity_id": "script.all_lights_off"},
        }
        self.assertEqual((top[1]["type"], top[1]["tap_action"], top[1]["icon_tap_action"]), ("tile", act, act))

    def test_top_row_without_helpers_falls_back_to_light_turn_off(self):
        top = views(g.build(self.ctx, "Home", None, None))["sections"][0]["cards"]
        self.assertEqual(top[0]["type"], "button")
        self.assertNotIn("entity", top[0])
        self.assertEqual(
            top[0]["tap_action"],
            {"action": "perform-action", "perform_action": "light.turn_off", "target": {"entity_id": "all"}},
        )
        self.assertFalse(any(c.get("name") == "Lights on" for c in top))

    def test_summary(self):
        lines = g.summarize(self.cfg)
        self.assertEqual(lines[0], "section top row: Lights on, All lights off; scenes: Spa")
        self.assertIn("section office: Desk 1, Desk 2", lines)
        self.assertIn("section Other lights: Hall Bulb", lines)

    def test_build_is_deterministic(self):
        again = g.build(self.ha.ctx(), "Home", "sensor.lights_on_count", "script.all_lights_off")
        self.assertEqual(g.digest(again), g.digest(self.cfg))
        self.assertEqual(g.digest({"b": 1, "a": [1, 2]}), g.digest({"a": [1, 2], "b": 1}))


class EmptyAndMissingTests(unittest.TestCase):
    def test_no_areas_no_entities(self):
        ha = FakeHA()
        ha.areas = ha.devices = ha.ents = ha.states = []
        cfg = g.build(ha.ctx(), "Home", None, None)
        v = views(cfg)
        self.assertNotIn("badges", v)
        self.assertEqual(len(v["sections"]), 1)
        self.assertEqual(g.summarize(cfg), ["section top row: All lights off"])
        self.assertEqual(g.presence_rooms(ha.ctx()), [])

    def test_lights_whose_area_or_device_vanished(self):
        ha = FakeHA()
        ha.areas = [a for a in ha.areas if a["area_id"] != "kitchen"]  # area deleted, device still points at it
        ha.devices = [d for d in ha.devices if d["id"] != "hue1"] + [{"id": "hue1", "area_id": "kitchen"}]
        cfg = g.build(ha.ctx(), "Home", None, None)
        self.assertNotIn("kitchen", [s["cards"][0].get("area") for s in views(cfg)["sections"]])
        ha.devices = [d for d in ha.devices if d["id"] != "hue1"]  # device gone too: light has no area
        ctx = ha.ctx()
        self.assertIsNone(ctx.area_of("light.kitchen_ceiling"))
        other = views(g.build(ctx, "Home", None, None))["sections"][-1]
        self.assertIn("light.kitchen_ceiling", [c.get("entity") for c in other["cards"]])

    def test_missing_attributes_and_names(self):
        ha = FakeHA()
        ha.states.append({"entity_id": "light.bare", "state": "on"})  # no attributes at all
        ha.ents.append({"entity_id": "light.bare", "area_id": "office"})
        cfg = g.build(ha.ctx(), "Home", None, None)
        t = next(c for c in sec(cfg, "office")["cards"] if c.get("entity") == "light.bare")
        self.assertEqual(t["name"], "light.bare")
        self.assertNotIn("features", t)

    def test_short_name_edge_cases(self):
        self.assertEqual(g.short_name("Office", "Office"), "Office")  # nothing left: keep the full name
        self.assertEqual(g.short_name("office - desk", "Office"), "Desk")
        self.assertEqual(g.short_name("IKEA of Sweden bulb", None), "IKEA bulb")
        self.assertEqual(g.short_name("", "Office"), "")

    def test_area_rank_and_icons(self):
        names = ["Zebra", "Bad", "Küche", "Büro", "Flur", "Alpha", "Living Room"]
        self.assertEqual(
            sorted(names, key=g.area_rank), ["Living Room", "Küche", "Büro", "Bad", "Flur", "Alpha", "Zebra"]
        )
        self.assertEqual(g.icon_for_area("Gästezimmer"), "mdi:bed-outline")  # guest before bed
        self.assertEqual(g.icon_for_area("Waschküche"), "mdi:countertop")  # first match wins
        self.assertIsNone(g.icon_for_area("Zebra"))

    def test_verify_reports_missing_entities_and_areas(self):
        ha = FakeHA()
        ctx = ha.ctx()
        cfg = g.build(ctx, "Home", "sensor.lights_on_count", "script.all_lights_off")
        r, missing = g.verify(ctx, cfg)
        self.assertEqual(missing, [])
        self.assertNotIn("all", r["entities"])
        del ctx.states["light.strip"]
        del ctx.areas["office"]
        self.assertEqual(g.verify(ctx, cfg)[1], ["light.strip", "area:office"])


# ----------------------------------------------------------------------------- guards (write allowlist)
class GuardTests(unittest.TestCase):
    def setUp(self):
        self.guard = g.make_ws_guard("home-mobile")

    def test_reads_allowed(self):
        for t in g.WS_READ:
            self.guard(t, {})

    def test_only_own_dashboard_writable(self):
        self.guard("lovelace/config/save", {"url_path": "home-mobile", "config": {}})
        self.guard("lovelace/dashboards/create", {"url_path": "home-mobile"})
        for p in ({"url_path": "lovelace"}, {"url_path": "other-dash"}, {}):
            with self.assertRaises(SystemExit):
                self.guard("lovelace/config/save", dict(p, config={}))
        for t in (
            "lovelace/config/delete",
            "lovelace/dashboards/delete",
            "lovelace/dashboards/update",
            "config/entity_registry/update",
            "config/device_registry/update",
            "auth/long_lived_access_token",
        ):
            with self.assertRaises(SystemExit):
                self.guard(t, {"url_path": "home-mobile"})

    def test_area_update_only_icon(self):
        self.guard("config/area_registry/update", {"area_id": "x", "icon": "mdi:sofa"})
        for p in ({"area_id": "x", "icon": "i", "name": "n"}, {"area_id": "x", "name": "n"}, {"icon": "i"}):
            with self.assertRaises(SystemExit):
                self.guard("config/area_registry/update", p)

    def test_rest_allowlist(self):
        for p in (
            "/api/config/script/config/other",
            "/api/config/script/config/all_lights_off/x",
            "/api/config/config_entries/flow/short",
            "/api/config/config_entries/flow/../../x",
            "/api/states/light.x",
            "/api/config/automation/config/a",
        ):
            with self.assertRaises(SystemExit):
                g.rest_post(BASE, TOKEN, p, {})


class RestTests(unittest.TestCase):
    def test_request_shape_and_token_only_in_header(self):
        seen = {}

        def urlopen(req, timeout):
            seen.update(
                url=req.full_url,
                method=req.get_method(),
                auth=req.get_header("Authorization"),
                body=json.loads(req.data),
                timeout=timeout,
            )
            return io.BytesIO(b'{"result": "ok"}')

        with mock.patch.object(g.urllib.request, "urlopen", urlopen):
            r = g.rest_post(BASE + "/", TOKEN, "/api/config/script/config/all_lights_off", g.SCRIPT_CFG)
        self.assertEqual(r, {"result": "ok"})
        self.assertEqual(seen["url"], BASE + "/api/config/script/config/all_lights_off")
        self.assertEqual((seen["method"], seen["auth"], seen["timeout"]), ("POST", "Bearer " + TOKEN, 60))
        self.assertEqual(seen["body"], g.SCRIPT_CFG)
        self.assertNotIn(TOKEN, seen["url"])

    def test_empty_body_is_none_and_http_error_propagates(self):
        with mock.patch.object(g.urllib.request, "urlopen", lambda req, timeout: io.BytesIO(b"")):
            self.assertIsNone(g.rest_post(BASE, TOKEN, "/api/config/config_entries/flow", {}))

        def boom(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b""))

        with mock.patch.object(g.urllib.request, "urlopen", boom):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                g.rest_post(BASE, TOKEN, "/api/config/config_entries/flow", {})
        self.assertNotIn(TOKEN, str(cm.exception))


# ----------------------------------------------------------------------------- helpers in HA
class FlowStub:
    """Stub urlopen for the template-helper flow and the script config endpoint."""

    def __init__(self, ha, steps):
        self.ha, self.steps, self.posts = ha, list(steps), []

    def __call__(self, req, timeout):
        path = req.full_url[len(BASE) :]
        body = json.loads(req.data)
        self.posts.append((path, body))
        if path.startswith("/api/config/script/config/"):
            self.ha.states.append(st("script.all_lights_off", "off"))
            return io.BytesIO(b'{"result": "ok"}')
        step = self.steps.pop(0)
        if step.get("type") == "create_entry":
            self.ha.ents.append({"entity_id": "sensor.lights_on", "config_entry_id": "new"})
            self.ha.states.append(st("sensor.lights_on", "0"))
            self.ha.entries.append({"entry_id": "new", "title": g.COUNT_NAME})
        return io.BytesIO(json.dumps(step).encode())


FID = "a" * 32


class HelperTests(unittest.TestCase):
    def setUp(self):
        self.ha = FakeHA()
        self.ha.states = [s for s in self.ha.states if s["entity_id"] != "script.all_lights_off"]
        self.ha.entries = [e for e in self.ha.entries if e["title"] != g.COUNT_NAME]
        self.ctx = self.ha.ctx()
        p = mock.patch.object(g.time, "sleep", lambda s: None)
        p.start()
        self.addCleanup(p.stop)

    def test_existing_helpers_found(self):
        ha = FakeHA()
        ctx = ha.ctx()
        self.assertEqual(g.ensure_script(ctx, BASE, TOKEN, False), ("script.all_lights_off", "exists"))
        self.assertEqual(g.ensure_count_sensor(ctx, BASE, TOKEN, False), ("sensor.lights_on_count", "exists"))

    def test_dry_run_creates_nothing(self):
        with mock.patch.object(g.urllib.request, "urlopen", side_effect=AssertionError("network")):
            self.assertEqual(g.ensure_script(self.ctx, BASE, TOKEN, True), ("script.all_lights_off", "would create"))
            self.assertEqual(g.ensure_count_sensor(self.ctx, BASE, TOKEN, True)[1], "would create (template helper)")

    def test_count_sensor_entry_without_sensor_entity_is_not_found(self):
        self.ha.entries.append({"entry_id": "tmpl9", "title": g.COUNT_NAME})
        self.ha.ents.append({"entity_id": "binary_sensor.x", "config_entry_id": "tmpl9"})
        self.assertIsNone(g.find_count_sensor(self.ha.ctx()))

    def test_script_created_and_polled(self):
        stub = FlowStub(self.ha, [])
        with mock.patch.object(g.urllib.request, "urlopen", stub):
            self.assertEqual(g.ensure_script(self.ctx, BASE, TOKEN, False), ("script.all_lights_off", "created"))
        self.assertEqual(stub.posts, [("/api/config/script/config/all_lights_off", g.SCRIPT_CFG)])

    def test_script_created_but_never_loaded(self):
        with mock.patch.object(g.urllib.request, "urlopen", lambda r, timeout: io.BytesIO(b"{}")):
            eid, note = g.ensure_script(self.ctx, BASE, TOKEN, False)
        self.assertIsNone(eid)
        self.assertIn("not loaded", note)
        self.assertEqual(sum(1 for t, _ in self.ha.calls if t == "get_states"), 1 + 20)

    def test_count_sensor_flow_menu_form_create(self):
        schema = [{"name": "name"}, {"name": "state"}, {"name": "state_class"}, {"name": "unit_of_measurement"}]
        stub = FlowStub(
            self.ha,
            [
                {"type": "menu", "flow_id": FID},
                {"type": "form", "flow_id": FID, "data_schema": schema},
                {"type": "create_entry"},
            ],
        )
        with mock.patch.object(g.urllib.request, "urlopen", stub):
            self.assertEqual(g.ensure_count_sensor(self.ctx, BASE, TOKEN, False), ("sensor.lights_on", "created"))
        paths = [p for p, _ in stub.posts]
        self.assertEqual(paths, ["/api/config/config_entries/flow"] + ["/api/config/config_entries/flow/" + FID] * 2)
        self.assertEqual(stub.posts[1][1], {"next_step_id": "sensor"})
        self.assertEqual(
            stub.posts[2][1], {"name": "Lights on", "state": g.COUNT_TEMPLATE, "state_class": "measurement"}
        )

    def test_count_sensor_flow_form_without_state_class(self):
        stub = FlowStub(
            self.ha,
            [
                {"type": "form", "flow_id": FID, "data_schema": [{"name": "name"}, {"name": "state"}]},
                {"type": "create_entry"},
            ],
        )
        with mock.patch.object(g.urllib.request, "urlopen", stub):
            g.ensure_count_sensor(self.ctx, BASE, TOKEN, False)
        self.assertEqual(stub.posts[-1][1], {"name": "Lights on", "state": g.COUNT_TEMPLATE})

    def test_count_sensor_flow_errors(self):
        for steps, msg in (
            ([{"type": "abort", "flow_id": FID, "step_id": "x"}], "unexpected step"),
            (
                [
                    {"type": "form", "flow_id": FID, "data_schema": []},
                    {"type": "form", "flow_id": FID, "errors": {"state": "bad"}},
                ],
                "did not create",
            ),
        ):
            stub = FlowStub(self.ha, steps)
            with mock.patch.object(g.urllib.request, "urlopen", stub), self.assertRaises(SystemExit) as cm:
                g.ensure_count_sensor(self.ctx, BASE, TOKEN, False)
            self.assertIn(msg, str(cm.exception))

    def test_count_sensor_created_but_entity_never_appears(self):
        stub = FlowStub(self.ha, [{"type": "form", "flow_id": FID, "data_schema": []}, {"type": "create_entry"}])
        stub.ha = FakeHA()  # entity lands somewhere this ctx never sees
        with mock.patch.object(g.urllib.request, "urlopen", stub):
            self.assertEqual(
                g.ensure_count_sensor(self.ctx, BASE, TOKEN, False), (None, "created but entity not found")
            )

    def test_area_icons(self):
        dry = g.ensure_area_icons(self.ctx, True)
        self.assertEqual(
            sorted(dry),
            ["Bathroom -> mdi:shower", "Bedroom -> mdi:bed", "Office -> mdi:desk", "Wohnzimmer -> mdi:sofa"],
        )
        self.assertFalse([c for c in self.ha.calls if c[0] == "config/area_registry/update"])
        guard = g.make_ws_guard("home-mobile")
        self.ctx.ws.guard = guard
        g.ensure_area_icons(self.ctx, False)
        ups = [p for t, p in self.ha.calls if t == "config/area_registry/update"]
        self.assertEqual(len(ups), 4)
        self.assertTrue(all(set(p) == {"area_id", "icon"} for p in ups))
        self.assertEqual(self.ctx.areas["office"]["icon"], "mdi:desk")
        self.assertEqual(self.ha.areas[3]["icon"], "mdi:chef-hat")  # own icon kept
        self.assertEqual(g.ensure_area_icons(self.ha.ctx(), False), [])  # idempotent


# ----------------------------------------------------------------------------- main()
class MainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state")
        self.ha = FakeHA()
        self.m = fake_audit(self.ha)
        patches = [
            mock.patch.object(g, "STATE_DIR", self.state),
            mock.patch.object(g, "load_ha_audit", lambda: self.m),
            mock.patch.object(g.time, "sleep", lambda s: None),
            mock.patch.object(g.urllib.request, "urlopen", side_effect=AssertionError("no REST expected")),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = g.main(list(argv))
        text = out.getvalue() + err.getvalue()
        self.assertNotIn(TOKEN, text)
        return rc, out.getvalue()

    def writes(self):
        return [t for t, _ in self.ha.calls if t in g.WS_WRITE]

    def state_file(self):
        return os.path.join(self.state, "home-mobile.json")

    def test_url_path_refused_before_connecting(self):
        for p in ("lovelace", "home", "energy", "map", "mobile"):
            with self.assertRaises(SystemExit):
                g.main(["--url-path", p])
        self.assertEqual(self.m.tokens_read, [])
        self.assertEqual(self.ha.calls, [])

    def test_guard_installed_on_ha_audit(self):
        self.run_main("--print")
        self.assertIsNotNone(self.m.check_ws)
        with self.assertRaises(SystemExit):
            self.m.check_ws("lovelace/config/save", {"url_path": "lovelace"})
        self.assertEqual(self.m.tokens_read, [g.TOKEN_FILE])

    def test_print_writes_nothing(self):
        rc, out = self.run_main("--print")
        self.assertEqual(rc, 0)
        cfg = json.loads(out)
        self.assertEqual(cfg["title"], "Home")
        self.assertEqual(self.writes(), [])
        self.assertFalse(os.path.exists(self.state))
        self.assertEqual(self.ha.closed, 1)

    def test_dry_run_writes_nothing(self):
        rc, out = self.run_main("--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn("dashboard /home-mobile (new): changes", out)
        self.assertIn("would set area icons: ", out)
        self.assertIn("script: script.all_lights_off (exists); count: sensor.lights_on_count (exists)", out)
        self.assertIn("  presence in room headers: Wohnzimmer\n", out)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.ha.dashboards, [])
        self.assertFalse(os.path.exists(self.state_file()))

    def test_apply_new_then_unchanged(self):
        rc, out = self.run_main("--no-area-icons")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.writes(), ["lovelace/dashboards/create", "lovelace/config/save"])
        create = next(p for t, p in self.ha.calls if t == "lovelace/dashboards/create")
        self.assertEqual(
            create,
            {
                "url_path": "home-mobile",
                "title": "Home",
                "icon": "mdi:home",
                "show_in_sidebar": True,
                "require_admin": False,
            },
        )
        self.assertIn("missing: none", out)
        self.assertIn("url: " + BASE + "/home-mobile/home", out)
        with open(self.state_file()) as fh:
            saved = json.load(fh)
        self.assertEqual(saved["digest"], g.digest(self.ha.configs["home-mobile"]))
        self.assertEqual(os.listdir(self.state), ["home-mobile.json"])  # no .tmp left behind
        self.ha.calls.clear()
        rc, out = self.run_main("--no-area-icons")
        self.assertEqual(rc, 0)
        self.assertIn("(exists): unchanged", out)
        self.assertEqual(self.writes(), [])

    def test_ui_edit_refused_then_forced(self):
        self.run_main("--no-area-icons")
        self.ha.configs["home-mobile"]["title"] = "edited in UI"
        self.ha.calls.clear()
        rc, out = self.run_main("--no-area-icons")
        self.assertEqual(rc, 2)
        self.assertIn("REFUSED", out)
        self.assertEqual(self.writes(), [])
        rc, out = self.run_main("--no-area-icons", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn("apply needs --force", out)
        rc, _ = self.run_main("--no-area-icons", "--force")
        self.assertEqual(rc, 0)
        self.assertEqual(self.ha.configs["home-mobile"]["title"], "Home")

    def test_existing_dashboard_without_state_counts_as_edited(self):
        self.ha.dashboards.append({"url_path": "home-mobile", "title": "Home"})
        self.ha.configs["home-mobile"] = {"title": "hand made", "views": []}
        rc, _ = self.run_main("--no-area-icons")
        self.assertEqual(rc, 2)
        self.assertNotIn("lovelace/dashboards/create", self.writes())

    def test_dashboard_listed_but_config_missing(self):
        self.ha.dashboards.append({"url_path": "home-mobile", "title": "Home"})  # config read raises HAError
        rc, out = self.run_main("--no-area-icons")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.writes(), ["lovelace/config/save"])

    def test_readback_mismatch_exit_3_no_state(self):
        self.ha.mangle_save = True
        rc, out = self.run_main("--no-area-icons")
        self.assertEqual(rc, 3)
        self.assertIn("VERIFY FAILED", out)
        self.assertFalse(os.path.exists(self.state_file()))

    def test_missing_entity_after_save_exit_3(self):
        orig = FakeWS.call

        def call(ws, t, **p):
            r = orig(ws, t, **p)
            if t == "lovelace/config/save":  # light removed between build and verify
                ws.ha.states = [s for s in ws.ha.states if s["entity_id"] != "light.strip"]
            return r

        with mock.patch.object(FakeWS, "call", call):
            rc, out = self.run_main("--no-area-icons")
        self.assertEqual(rc, 3)
        self.assertIn("missing: light.strip", out)
        self.assertTrue(os.path.exists(self.state_file()))

    def test_no_helpers_flag(self):
        self.ha.states = [s for s in self.ha.states if s["entity_id"] != "script.all_lights_off"]
        rc, out = self.run_main("--no-helpers", "--no-area-icons", "--dry-run")
        self.assertIn("script: - (skipped); count: sensor.lights_on_count (skipped)", out)
        self.assertEqual(rc, 0)

    def test_apply_creates_helpers_and_icons(self):
        self.ha.states = [s for s in self.ha.states if s["entity_id"] != "script.all_lights_off"]
        self.ha.entries = [e for e in self.ha.entries if e["title"] != g.COUNT_NAME]
        stub = FlowStub(
            self.ha,
            [
                {"type": "form", "flow_id": FID, "data_schema": [{"name": "name"}, {"name": "state"}]},
                {"type": "create_entry"},
            ],
        )
        with mock.patch.object(g.urllib.request, "urlopen", stub):
            rc, out = self.run_main()
        self.assertEqual(rc, 0, out)
        self.assertIn("script: script.all_lights_off (created); count: sensor.lights_on (created)", out)
        self.assertIn("set area icons: ", out)
        cfg = self.ha.configs["home-mobile"]
        self.assertEqual(views(cfg)["sections"][0]["cards"][0]["entity"], "sensor.lights_on")

    def test_ha_errors_propagate_and_close(self):
        for t in ("config/area_registry/list", "get_states", "lovelace/dashboards/list", "lovelace/config/save"):
            ha = FakeHA()
            ha.fail[t] = HAError("boom: " + t)
            m = fake_audit(ha)
            with mock.patch.object(g, "load_ha_audit", lambda: m), self.assertRaises(HAError):  # noqa: B023
                self.run_main("--no-area-icons")
            self.assertEqual(ha.closed, 1, t)
            self.assertFalse(os.path.exists(self.state_file()))

    def test_custom_url_path_and_title(self):
        rc, out = self.run_main("--url-path", "phone-home", "--title", "Phone", "--no-area-icons")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.ha.configs["phone-home"]["title"], "Phone")
        self.assertTrue(os.path.exists(os.path.join(self.state, "phone-home.json")))


if __name__ == "__main__":
    unittest.main()
