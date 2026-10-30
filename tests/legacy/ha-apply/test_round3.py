"""Offline tests for what ha-apply keeps from round 3: exact-message guard, dashboard/hidden_by restrictions,
body-keyed options flow, scene REST reads (no scene writes), the private plan loader, and the archived groups' write
types staying refused (addons/zigbee-rooms/scene/hue-log, hallway and scene-create live in ~/attic/ha-apply-oneoffs
with their own tests).
Nothing touches the network."""

import contextlib
import importlib.machinery
import importlib.util
import io
import os
import sys
import unittest

sys.dont_write_bytecode = True
BIN = os.environ.get("HA_APPLY_BIN") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "scripts", "ha-apply"
)
PLAN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "plan.json")
os.environ["HA_APPLY_PLAN"] = PLAN  # fictional plan; tests never read the private one
loader = importlib.machinery.SourceFileLoader("ha_apply_r3", BIN)
spec = importlib.util.spec_from_loader("ha_apply_r3", loader)
M = importlib.util.module_from_spec(spec)
loader.exec_module(M)


class GuardR3(unittest.TestCase):
    def test_exact_message_only(self):
        msg = {"entity_id": "media_player.study", "new_entity_id": "media_player.den_left"}
        with self.assertRaises(M.NotAllowed):
            M.Guard(writes=False).check_ws("config/entity_registry/update", msg)
        g = M.Guard(writes=True)
        with self.assertRaises(M.NotAllowed):
            g.check_ws("config/entity_registry/update", msg)
        g.approve(("config/entity_registry/update", M.canon(msg)))
        g.check_ws("config/entity_registry/update", dict(msg))
        with self.assertRaises(M.NotAllowed):
            g.check_ws("config/entity_registry/update", dict(msg, new_entity_id="media_player.x"))
        with self.assertRaises(M.NotAllowed):
            g.check_ws("config/entity_registry/update", dict(msg, disabled_by="user"))

    def test_restrictions(self):
        g = M.Guard(writes=True)
        bad = [
            ("lovelace/config/save", {"url_path": "home-mobile", "config": {}}),
            ("config/entity_registry/update", {"entity_id": "a.b", "hidden_by": "integration"}),
        ]
        for t, msg in bad:
            with self.assertRaises(M.NotAllowed, msg=str(msg)):
                g.approve((t, M.canon(msg)))
                g.check_ws(t, msg)

    def test_archived_write_types_refused(self):
        g = M.Guard(writes=True)
        for t, msg in (
            ("supervisor/api", {"endpoint": "/addons/x/options", "method": "post", "data": {"boot": "manual"}}),
            ("supervisor/api", {"endpoint": "/addons/x/stop", "method": "post"}),
            ("supervisor/api", {"endpoint": "/addons/x/info", "method": "get"}),
            ("logger/log_level", {"module": "x", "level": "ERROR", "persistence": "permanent"}),
            ("system_log/list", {}),
        ):
            g.approve((t, M.canon(msg)))
            with self.assertRaises(M.NotAllowed, msg=t):
                g.check_ws(t, msg)
        for grp in ("addons", "zigbee-rooms", "scene", "hue-log", "scene-create", "hallway"):
            self.assertNotIn(grp, M.GROUPS)
        no_token = os.path.join(os.path.dirname(PLAN), "no-such-token")  # never reaches HA, even if argparse let it by
        for argv in (["hallway"], ["scene-create"], ["show", "hallway"]):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                M.main(argv + ["--token-file", no_token])

    def test_archived_scene_and_hallway_writes_refused(self):
        g = M.Guard(writes=True)
        body = {"name": "Movie", "icon": "mdi:movie", "entities": {}}
        g.approve(("rest", "POST", "/api/config/scene/config/1", M.canon(body)))
        with self.assertRaises(M.NotAllowed):  # scene writes left with the archived scene-create group
            g.check_rest("POST", "/api/config/scene/config/1", body)
        for msg in (
            {"domain": "scene", "service": "reload", "service_data": {}},
            {"domain": "automation", "service": "turn_off", "service_data": {"entity_id": "automation.x"}},
        ):
            with self.assertRaises(M.NotAllowed, msg=str(msg)):  # off the allowlist: no key can be approved
                M.target_key("call_service", msg)
            with self.assertRaises(M.NotAllowed, msg=str(msg)):
                g.check_ws("call_service", msg)

    def test_options_flow_body_keyed(self):
        g = M.Guard(writes=True)
        path = "/api/config/config_entries/options/flow"
        with self.assertRaises(M.NotAllowed):
            g.check_rest("POST", path, {"handler": "abc"})
        g.approve(("rest", "POST", path, M.canon({"handler": "abc"})))
        g.check_rest("POST", path, {"handler": "abc"})
        with self.assertRaises(M.NotAllowed):
            g.check_rest("POST", path, {"handler": "other"})
        with self.assertRaises(M.NotAllowed):
            g.check_rest("POST", "/api/config/config_entries/flow", {"handler": "abc"})

    def test_scene_rest(self):
        M.Guard(writes=False).check_rest("GET", "/api/config/scene/config/1000000000002")
        g = M.Guard(writes=True)
        g.approve(("rest", "DELETE", "/api/config/scene/config/1"))
        with self.assertRaises(M.NotAllowed):  # scene deletes left with the archived scene group
            g.check_rest("DELETE", "/api/config/scene/config/1")


class Pure(unittest.TestCase):
    def test_plan_sections(self):
        pl = M.load_plan(PLAN)
        self.assertEqual(sorted(pl), ["REGISTRY_PLAN", "SWAP_AUTOMATIONS"])  # scenes/hallway sections ignored
        self.assertEqual(M.PLAN_FILE, PLAN)
        empty = M.load_plan(os.path.join(os.path.dirname(PLAN), "missing.json"))
        self.assertEqual(sorted(empty), sorted(M.PLAN_KEYS))
        self.assertFalse(any(empty.values()))

    def test_round4_plan_sections(self):
        pl = M.load_plan(PLAN)
        self.assertEqual(pl["SWAP_AUTOMATIONS"], ("automation.den_lights_remote",))


if __name__ == "__main__":
    unittest.main()
