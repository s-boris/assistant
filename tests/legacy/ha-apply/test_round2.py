"""Offline tests for ha-apply's config entry delete guard (REST DELETE only for approved entries).
Nothing touches the network."""

import importlib.machinery
import importlib.util
import os
import sys
import unittest

sys.dont_write_bytecode = True
BIN = os.environ.get("HA_APPLY_BIN") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "scripts", "ha-apply"
)
loader = importlib.machinery.SourceFileLoader("ha_apply_r2", BIN)
spec = importlib.util.spec_from_loader("ha_apply_r2", loader)
M = importlib.util.module_from_spec(spec)
loader.exec_module(M)

E = "/api/config/config_entries/entry/"


class GuardRound2(unittest.TestCase):
    def test_entry_delete_needs_apply_and_plan(self):
        path = E + "OFFENT0000000000000000000A"
        with self.assertRaises(M.NotAllowed):
            M.Guard(writes=False).check_rest("DELETE", path)
        g = M.Guard(writes=True)
        with self.assertRaises(M.NotAllowed):
            g.check_rest("DELETE", path)
        g.approve(("rest", "DELETE", path))
        g.check_rest("DELETE", path)
        with self.assertRaises(M.NotAllowed):
            g.check_rest("DELETE", E + "ONENT00000000000000000000B")

    def test_other_deletes_refused(self):
        g = M.Guard(writes=True)
        for m, path in (
            ("DELETE", E + "abc"),
            ("DELETE", E + "OFFENT0000000000000000000A/x"),
            ("DELETE", E + "../../auth/x0000000000000000000000"),
            ("POST", E + "OFFENT0000000000000000000A"),
            ("DELETE", "/api/config/automation/config/1"),
            ("DELETE", "/api/config/config_entries/entry"),
            ("GET", E + "OFFENT0000000000000000000A"),
        ):
            g.approve(("rest", m, path))
            with self.assertRaises(M.NotAllowed, msg=f"{m} {path}"):
                g.check_rest(m, path)

    def test_no_new_ws_types(self):
        g = M.Guard(writes=True)
        for t in (
            "config_entries/remove",
            "config_entries/delete",
            "config_entries/disable",
            "config/device_registry/remove",
            "mobile_app/delete",
        ):
            with self.assertRaises(M.NotAllowed):
                g.check_ws(t, {})


if __name__ == "__main__":
    unittest.main()
