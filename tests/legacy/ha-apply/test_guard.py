"""Offline tests for ha-apply's Guard: nothing off the allowlist or outside the approved plan passes."""

import importlib.machinery
import importlib.util
import os
import sys
import unittest

sys.dont_write_bytecode = True
BIN = os.environ.get("HA_APPLY_BIN") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "scripts", "ha-apply"
)
loader = importlib.machinery.SourceFileLoader("ha_apply", BIN)
spec = importlib.util.spec_from_loader("ha_apply", loader)
M = importlib.util.module_from_spec(spec)
loader.exec_module(M)


class GuardTests(unittest.TestCase):
    def test_reads_pass_in_dry_run(self):
        g = M.Guard(writes=False)
        for t in M.WS_READ:
            g.check_ws(t, {})
        g.check_rest("GET", "/api/states/automation.x")

    def test_unknown_types_refused(self):
        g = M.Guard(writes=True)
        for t in (
            "config/entity_registry/update",
            "config/device_registry/remove",
            "auth/delete_refresh_token",
            "supervisor/api",
            "person/delete",
            "backup/delete",
            "execute_script",
            "zha/devices/permit",
        ):
            with self.assertRaises(M.NotAllowed):
                g.check_ws(t, {})

    def test_writes_refused_in_dry_run(self):
        g = M.Guard(writes=False)
        g.approve(("config/entity_registry/remove", "sensor.x"))
        with self.assertRaises(M.NotAllowed):
            g.check_ws("config/entity_registry/remove", {"entity_id": "sensor.x"})

    def test_writes_need_approved_target(self):
        g = M.Guard(writes=True)
        g.approve(("config/entity_registry/remove", "sensor.x"))
        g.check_ws("config/entity_registry/remove", {"entity_id": "sensor.x"})
        with self.assertRaises(M.NotAllowed):
            g.check_ws("config/entity_registry/remove", {"entity_id": "sensor.y"})
        with self.assertRaises(M.NotAllowed):
            g.check_ws("person/update", {"person_id": "jamie", "device_trackers": []})
        with self.assertRaises(M.NotAllowed):
            g.check_ws("config/entity_registry/remove", {"entity_id": "sensor.x", "extra": 1})

    def test_services(self):
        g = M.Guard(writes=True)
        with self.assertRaises(M.NotAllowed):
            g.check_ws("call_service", {"domain": "homeassistant", "service": "restart"})
        with self.assertRaises(M.NotAllowed):
            g.check_ws("call_service", {"domain": "zha", "service": "remove", "service_data": {"ieee": "a", "x": 1}})
        key = M.target_key("call_service", {"domain": "zha", "service": "remove", "service_data": {"ieee": "a"}})
        g.approve(key)
        g.check_ws("call_service", {"domain": "zha", "service": "remove", "service_data": {"ieee": "a"}})
        with self.assertRaises(M.NotAllowed):
            g.check_ws("call_service", {"domain": "zha", "service": "remove", "service_data": {"ieee": "b"}})
        with self.assertRaises(M.NotAllowed):  # target selector not allowed at all
            g.check_ws(
                "call_service",
                {"domain": "zha", "service": "remove", "service_data": {"ieee": "a"}, "target": {"entity_id": "all"}},
            )

    def test_rest(self):
        g = M.Guard(writes=True)
        for m, path in (
            ("DELETE", "/api/config/automation/config/1"),
            ("POST", "/api/services/x/y"),
            ("GET", "/api/states"),
            ("POST", "/api/states/automation.x"),
        ):
            with self.assertRaises(M.NotAllowed):
                g.check_rest(m, path)
        body = {"alias": "x", "triggers": []}
        with self.assertRaises(M.NotAllowed):
            g.check_rest("POST", "/api/config/automation/config/1", body)
        # automation POSTs are body-keyed: the path alone approves nothing
        g.approve(("rest", "POST", "/api/config/automation/config/1"))
        with self.assertRaises(M.NotAllowed):
            g.check_rest("POST", "/api/config/automation/config/1", body)
        g.approve(("rest", "POST", "/api/config/automation/config/1", M.canon(body)))
        g.check_rest("POST", "/api/config/automation/config/1", body)
        with self.assertRaises(M.NotAllowed):
            g.check_rest("POST", "/api/config/automation/config/1", dict(body, alias="y"))
        with self.assertRaises(M.NotAllowed):
            M.Guard(writes=False).check_rest("POST", "/api/config/automation/config/1", body)
        # DELETE of automations: only autopilot_* ids, and only when approved
        with self.assertRaises(M.NotAllowed):
            g.check_rest("DELETE", "/api/config/automation/config/autopilot_x")
        g.approve(("rest", "DELETE", "/api/config/automation/config/autopilot_x"))
        g.check_rest("DELETE", "/api/config/automation/config/autopilot_x")
        g.approve(("rest", "DELETE", "/api/config/automation/config/1704746170502"))
        with self.assertRaises(M.NotAllowed):
            g.check_rest("DELETE", "/api/config/automation/config/1704746170502")


if __name__ == "__main__":
    unittest.main()
