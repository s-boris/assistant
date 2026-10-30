"""Offline tests for ha-apply package scripts ("scripts" in an autopilot-style package): validation, dry run writes
nothing, --apply needs the sha, the script is POSTed and read back (config + state), idempotent, missing entities
and unreadable (YAML) scripts refused, the Guard keys script writes by exact body, autopilot-undo deletes only a
script it created and posts a replaced script's before-config back. Fake HA only."""

import copy
import json
import os
import sys
import unittest

sys.dont_write_bytecode = True
import test_autopilot as T  # noqa: E402

M = T.M
SCRIPT = {
    "alias": "Warm test",
    "description": "test script",
    "icon": "mdi:lightbulb",
    "mode": "single",
    "fields": {"all": {"name": "All", "selector": {"boolean": {}}, "default": False}},
    "variables": {
        "own": {"light.room_a": [1, 2, 3, 4]},
        "targets": "{{ states.light | map(attribute='entity_id') | list }}",
    },
    "sequence": [
        {
            "repeat": {
                "for_each": "{{ targets }}",
                "sequence": [
                    {
                        "action": "light.turn_on",
                        "target": {"entity_id": "{{ repeat.item }}"},
                        "data": {"brightness_pct": 30},
                    }
                ],
            }
        }
    ],
}
PKG = {
    "version": 1,
    "about": "test package with one script",
    "helpers": {},
    "automations": {},
    "scripts": {"warm_test": SCRIPT},
    "parts": {"warm": {"scripts": ["warm_test"]}},
}
SPATH = "/api/config/script/config/"


class FakeScriptHA(T.FakeHA):
    def __init__(self, guard):
        super().__init__(guard)
        self.scripts = {}

    def request(self, method, path, body=None):
        if not path.startswith(SPATH):
            return super().request(method, path, body)
        self.guard.check_rest(method, path, body)
        self.calls.append(("rest", method, path))
        sid = path[len(SPATH) :]
        if method == "GET":
            if sid not in self.scripts:
                raise M.A.HAError(f"HTTP 404 on GET {path}")
            return copy.deepcopy(self.scripts[sid])
        if method == "POST":
            self.scripts[sid] = json.loads(json.dumps(body))
            self.states["script." + sid] = "off"
            self.reg["script." + sid] = {"entity_id": "script." + sid, "platform": "script"}
            return {"result": "ok"}
        if method == "DELETE":
            self.scripts.pop(sid, None)
            self.states.pop("script." + sid, None)
            self.reg.pop("script." + sid, None)
            return {"result": "ok"}
        raise AssertionError(method)


class Base(T.Base):
    def setUp(self):
        super().setUp()
        self.write_pkg(PKG)

    def ha(self, writes):
        return FakeScriptHA(M.Guard(writes=writes))


class Validation(Base):
    def test_bad_scripts_refused(self):
        for bad in (
            {"scripts": {"Bad-Id": SCRIPT}, "parts": {"warm": {"scripts": ["Bad-Id"]}}},
            {"scripts": {"warm_test": dict(SCRIPT, sequence=[])}},
            {"scripts": {"warm_test": {k: v for k, v in SCRIPT.items() if k != "alias"}}},
            {"scripts": {"warm_test": dict(SCRIPT, trigger=[{"platform": "state"}])}},
            {
                "scripts": {
                    "warm_test": dict(
                        SCRIPT, sequence=[{"action": "scene.create", "data": {"scene_id": "autopilot_x"}}]
                    )
                }
            },
            {"parts": {"warm": {"scripts": ["nope"]}}},
        ):
            pkg = dict(copy.deepcopy(PKG), **bad)
            self.write_pkg(pkg)
            with self.assertRaises(SystemExit, msg=str(bad)):
                M.ap_load(self.pkg_path)

    def test_refs_ignore_dict_keys_and_states_attr(self):
        self.assertEqual(M.ap_refs(SCRIPT), set())

    def test_guard_script_writes_exact_body(self):
        g = M.Guard(writes=True)
        path = SPATH + "warm_test"
        with self.assertRaises(M.NotAllowed):
            g.check_rest("POST", path, SCRIPT)
        g.approve(("rest", "POST", path, M.canon(SCRIPT)))
        g.check_rest("POST", path, SCRIPT)
        with self.assertRaises(M.NotAllowed):
            g.check_rest("POST", path, dict(SCRIPT, alias="Other"))
        with self.assertRaises(M.NotAllowed):
            g.check_rest("DELETE", path)
        with self.assertRaises(M.NotAllowed):
            g.check_rest("POST", SPATH + "../automation/config/x", SCRIPT)
        with self.assertRaises(M.NotAllowed):
            M.Guard(writes=False).check_rest("POST", path, SCRIPT)


class Apply(Base):
    def test_dry_run_writes_nothing(self):
        c = self.ha(False)
        out = T.run(M.g_autopilot, c, c.guard, False, pkg_path=self.pkg_path)
        self.assertIn("script.warm_test: create", out)
        self.assertIn(self.sha[:12], out)
        self.assertEqual(self.writes(c), [])
        self.assertFalse(os.path.exists(M.UNDO))

    def test_apply_needs_sha(self):
        c = self.ha(True)
        with self.assertRaises(SystemExit):
            T.run(M.g_autopilot, c, c.guard, True, sha="00000000", pkg_path=self.pkg_path)
        self.assertEqual(self.writes(c), [])

    def test_apply_reads_back_and_is_idempotent(self):
        c = self.ha(True)
        out = T.run(M.g_autopilot, c, c.guard, True, sha=self.sha[:8], pkg_path=self.pkg_path)
        self.assertIn("verify: OK", out)
        self.assertIn("1 scripts written", out)
        self.assertEqual(c.scripts["warm_test"], SCRIPT)
        self.assertEqual(c.states["script.warm_test"], "off")
        rec = self.load_undo()["autopilot"]["groups"]["autopilot"]
        self.assertIsNone(rec["scripts"]["warm_test"]["before"])
        self.assertTrue(rec["verify"]["config_match"]["script.warm_test"])
        self.assertFalse([x for x in c.calls if x[1] == "call_service"])  # never runs the script
        n = len(c.calls)
        out = T.run(M.g_autopilot, c, c.guard, True, sha=self.sha[:8], pkg_path=self.pkg_path)
        self.assertIn("nothing to do", out)
        self.assertEqual(self.writes(type("X", (), {"calls": c.calls[n:]})), [])

    def test_missing_entity_refused(self):
        pkg = copy.deepcopy(PKG)
        pkg["scripts"]["warm_test"]["sequence"].append(
            {"action": "light.turn_on", "target": {"entity_id": "light.nope"}}
        )
        self.write_pkg(pkg)
        c = self.ha(True)
        with self.assertRaises(SystemExit):
            T.run(M.g_autopilot, c, c.guard, True, sha=self.sha[:8], pkg_path=self.pkg_path)
        self.assertEqual(self.writes(c), [])

    def test_yaml_script_collision_refused(self):
        c = self.ha(True)
        c.states["script.warm_test"] = "off"
        with self.assertRaises(SystemExit):
            T.run(M.g_autopilot, c, c.guard, True, sha=self.sha[:8], pkg_path=self.pkg_path)
        self.assertEqual(self.writes(c), [])


class Undo(Base):
    def test_undo_deletes_created_script(self):
        c = self.ha(True)
        T.run(M.g_autopilot, c, c.guard, True, sha=self.sha[:8], pkg_path=self.pkg_path)
        dry = self.ha(False)
        dry.scripts, dry.states = c.scripts, c.states
        out = T.run(M.g_autopilot_undo, dry, dry.guard, False, pkg_path=self.pkg_path)
        self.assertIn("script.warm_test: delete", out)
        self.assertEqual(self.writes(dry), [])
        out = T.run(M.g_autopilot_undo, c, c.guard, True, pkg_path=self.pkg_path)
        self.assertIn("verify: OK", out)
        self.assertNotIn("warm_test", c.scripts)
        self.assertNotIn("script.warm_test", c.states)
        self.assertEqual(self.load_undo()["autopilot"]["groups"]["autopilot"]["scripts"], {})

    def test_undo_restores_replaced_script(self):
        c = self.ha(True)
        old = {"alias": "Old one", "sequence": [{"delay": 1}]}
        c.scripts["warm_test"] = old
        c.states["script.warm_test"] = "off"
        out = T.run(M.g_autopilot, c, c.guard, True, sha=self.sha[:8], pkg_path=self.pkg_path)
        self.assertIn("replace existing config", out)
        self.assertEqual(c.scripts["warm_test"], SCRIPT)
        out = T.run(M.g_autopilot_undo, c, c.guard, True, pkg_path=self.pkg_path)
        self.assertIn("post the before-config back", out)
        self.assertEqual(c.scripts["warm_test"], old)

    def test_script_without_record_kept(self):
        c = self.ha(True)
        c.scripts["warm_test"] = {"alias": "His own", "sequence": [{"delay": 1}]}
        out = T.run(M.g_autopilot_undo, c, c.guard, True, pkg_path=self.pkg_path)
        self.assertIn("kept (no undo record", out)
        self.assertIn("warm_test", c.scripts)
        self.assertEqual(self.writes(c), [])


if __name__ == "__main__":
    unittest.main()
