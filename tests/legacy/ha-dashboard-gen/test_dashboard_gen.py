"""Offline tests for ha-dashboard-gen's build step (no HA, no network).
GEN_BIN=/path tests a copy (default: the repo's scripts/ha-dashboard-gen)."""

import importlib.machinery
import importlib.util
import os
import sys
import unittest

sys.dont_write_bytecode = True
GEN = os.environ.get("GEN_BIN") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "scripts", "ha-dashboard-gen"
)


def load():
    ld = importlib.machinery.SourceFileLoader("ha_dashboard_gen", GEN)
    spec = importlib.util.spec_from_loader("ha_dashboard_gen", ld)
    mod = importlib.util.module_from_spec(spec)
    ld.exec_module(mod)
    return mod


g = load()


def st(eid, state="on", **attrs):
    return {"entity_id": eid, "state": state, "attributes": attrs}


class FakeCtx(g.Ctx):
    def __init__(self):
        self.areas = {
            "living_room": {"area_id": "living_room", "name": "Living Room", "icon": "mdi:sofa"},
            "office": {"area_id": "office", "name": "Office", "icon": None},
        }
        self.devices = {"d1": {"id": "d1", "area_id": "living_room"}}
        self.ents = {
            "light.lamp": {"entity_id": "light.lamp", "device_id": "d1"},
            "scene.movie": {"entity_id": "scene.movie", "area_id": "living_room"},
            "scene.full_light": {"entity_id": "scene.full_light", "area_id": "living_room"},
            "scene.ambient": {"entity_id": "scene.ambient", "area_id": "living_room"},
            "scene.dim_light": {"entity_id": "scene.dim_light", "area_id": "living_room"},
            "scene.focus": {"entity_id": "scene.focus", "area_id": "office"},
            "scene.night": {"entity_id": "scene.night"},
            "scene.old": {"entity_id": "scene.old", "area_id": "living_room", "disabled_by": "user"},
        }
        self.states = {
            e: st(e, friendly_name=n)
            for e, n in [
                ("light.lamp", "Lamp"),
                ("scene.movie", "Movie"),
                ("scene.full_light", "Full Light"),
                ("scene.dim_light", "Dim Light"),
                ("scene.focus", "Focus"),
                ("scene.night", "Night"),
            ]
        }
        self.states["scene.ambient"] = st("scene.ambient", friendly_name="Ambient", icon="mdi:weather-night")
        self.states["light.lamp"]["attributes"]["supported_color_modes"] = ["brightness"]


def section(cfg, area):
    return next(s for s in cfg["views"][0]["sections"] if s["cards"][0].get("area") == area)


class SceneTests(unittest.TestCase):
    def setUp(self):
        self.ctx = FakeCtx()
        self.cfg = g.build(self.ctx, "Home", "sensor.lights_on", "script.all_lights_off")

    def test_room_scenes_sorted_in_room_section(self):
        cards = section(self.cfg, "living_room")["cards"]
        scn = [c for c in cards if c.get("entity", "").startswith("scene.")]
        self.assertEqual([c["name"] for c in scn], ["Ambient", "Dim Light", "Full Light", "Movie"])
        self.assertEqual(cards[0]["type"], "area")
        self.assertTrue(all(cards.index(c) < cards.index(cards[-1]) for c in scn))  # before the light tiles
        self.assertEqual(cards[-1]["entity"], "light.lamp")

    def test_scene_button_shape(self):
        b = next(c for c in section(self.cfg, "living_room")["cards"] if c.get("entity") == "scene.movie")
        self.assertEqual(b["type"], "button")
        self.assertEqual(
            b["tap_action"],
            {"action": "perform-action", "perform_action": "scene.turn_on", "target": {"entity_id": "scene.movie"}},
        )
        self.assertEqual(b["hold_action"], {"action": "more-info"})
        self.assertFalse(b["show_state"])
        self.assertEqual(b["grid_options"], {"columns": 3, "rows": 2})
        self.assertEqual(b["icon"], "mdi:palette")
        amb = next(c for c in section(self.cfg, "living_room")["cards"] if c.get("entity") == "scene.ambient")
        self.assertEqual(amb["icon"], "mdi:weather-night")

    def test_loose_scenes_in_top_row_and_disabled_skipped(self):
        top = self.cfg["views"][0]["sections"][0]["cards"]
        self.assertEqual([c["name"] for c in top if c.get("entity", "").startswith("scene.")], ["Focus", "Night"])
        self.assertTrue(any(c.get("type") == "heading" and c.get("heading") == "Scenes" for c in top))
        allrefs = g.refs(self.cfg, {"entities": set(), "areas": set()})["entities"]
        self.assertNotIn("scene.old", allrefs)
        self.assertEqual(
            {e for e in allrefs if e.startswith("scene.")},
            {"scene.movie", "scene.full_light", "scene.ambient", "scene.dim_light", "scene.focus", "scene.night"},
        )

    def test_no_loose_scenes_no_heading(self):
        del self.ctx.states["scene.focus"], self.ctx.states["scene.night"]
        cfg = g.build(self.ctx, "Home", "sensor.lights_on", "script.all_lights_off")
        top = cfg["views"][0]["sections"][0]["cards"]
        self.assertFalse(any(c.get("type") == "heading" for c in top))
        self.assertEqual(len(top), 2)

    def test_summary_lists_scenes(self):
        lines = g.summarize(self.cfg)
        self.assertIn("section living_room: Lamp; scenes: Ambient, Dim Light, Full Light, Movie", lines)

    def test_warm_dim_tile_only_when_script_exists(self):
        top = self.cfg["views"][0]["sections"][0]["cards"]
        self.assertFalse(any(c.get("entity") == g.WARM_SCRIPT for c in top))
        self.ctx.states[g.WARM_SCRIPT] = st(g.WARM_SCRIPT, "off", friendly_name="Warm & dim")
        top = g.build(self.ctx, "Home", "sensor.lights_on", "script.all_lights_off")["views"][0]["sections"][0]["cards"]
        names = [c.get("name") for c in top]
        self.assertEqual(names[:3], ["Lights on", "All lights off", "Warm & dim"])
        warm = top[2]
        act = {"action": "perform-action", "perform_action": "script.turn_on", "target": {"entity_id": g.WARM_SCRIPT}}
        self.assertEqual((warm["type"], warm["tap_action"], warm["icon_tap_action"]), ("tile", act, act))
        self.assertTrue(warm["hide_state"])

    def test_guard_never_allows_services(self):
        guard = g.make_ws_guard("home-mobile")
        for t in ("call_service", "execute_script"):
            with self.assertRaises(SystemExit):
                guard(t, {"domain": "scene", "service": "turn_on"})
        with self.assertRaises(SystemExit):
            g.rest_post("http://127.0.0.1:9", "x", "/api/services/scene/turn_on", {})


if __name__ == "__main__":
    unittest.main()
