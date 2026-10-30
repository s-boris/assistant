"""Offline tests for ha-apply registry: device name/area, entity rename/disable (user only, skipped while used),
group helper member drops via the options flow, dashboard entity swap (never a generated dashboard), dry run
writes nothing, apply = exact approved messages + before-state + verify, second run is a no-op. Fake HA."""

import contextlib
import copy
import importlib.machinery
import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
BIN = os.environ.get("HA_APPLY_BIN") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "scripts", "ha-apply"
)
PLAN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "plan.json")
os.environ["HA_APPLY_PLAN"] = PLAN
loader = importlib.machinery.SourceFileLoader("ha_apply_reg", BIN)
spec = importlib.util.spec_from_loader("ha_apply_reg", loader)
M = importlib.util.module_from_spec(spec)
loader.exec_module(M)
DEV = "a" * 32
FID = "f" * 32
WRITES = {"config/entity_registry/update", "config/device_registry/update", "lovelace/config/save"}


def plan():
    return {
        "devices": [{"device_id": DEV, "name_by_user": "Den Strip", "area_id": "den"}],
        "entities": [
            {"entity_id": "switch.hall_strip_freeze", "new_entity_id": "switch.den_strip_freeze"},
            {"entity_id": "light.den_zgroup", "disabled_by": "user"},
            {"entity_id": "light.porch_tpl", "disabled_by": "user"},
        ],
        "group_members": {"light.den_lights": ["light.den_lamp"]},
        "dashboard_swap": [{"url_path": "den-board", "old": "light.porch_tpl", "new": "light.porch_plug"}],
    }


class FakeHA:
    def __init__(self, guard, used_by_auto=None):
        self.guard, self.calls = guard, []
        self.reg = {
            e: {"entity_id": e, "platform": pl, "disabled_by": None, "config_entry_id": ce, "device_id": dv}
            for e, pl, ce, dv in (
                ("light.den_strip", "wled", "W1", DEV),
                ("switch.hall_strip_freeze", "wled", "W1", DEV),
                ("light.den_zgroup", "zha", "Z1", None),
                ("light.porch_tpl", "template", None, None),
                ("light.porch_plug", "switch_as_x", "S1", None),
                ("light.den_lamp", "group", "G2", None),
                ("light.den_lights", "group", "G1", None),
            )
        }
        self.devs = {
            DEV: {"id": DEV, "name": "WLED Den", "name_by_user": "WLED Hall", "area_id": "hall", "model": "FOSS"}
        }
        self.entries = {
            "G1": {"entry_id": "G1", "domain": "group"},
            "G2": {"entry_id": "G2", "domain": "group"},
            "Z1": {"entry_id": "Z1", "domain": "zha"},
            "W1": {"entry_id": "W1", "domain": "wled"},
        }
        self.members = {"light.den_lights": ["light.den_zgroup", "light.den_lamp"]}
        self.autos = {
            "a1": {
                "id": "a1",
                "actions": [{"action": "light.turn_on", "target": {"entity_id": used_by_auto or "light.den_lamp"}}],
            }
        }
        self.dash = {
            "den-board": {"views": [{"cards": [{"type": "tile", "entity": "light.porch_tpl"}]}]},
            "home-mobile": {"views": [{"cards": [{"type": "tile", "entity": "light.den_zgroup"}]}]},
        }
        self.options = {"G1": {"hide_members": False, "all": False}}
        self._sync()
        self.ws, self.rest = self, self

    def _sync(self):
        self.states = {e: "on" for e, r in self.reg.items() if not r.get("disabled_by")}
        self.states["automation.a1"] = "on"
        self.attrs = {e: {} for e in self.states}
        self.attrs["automation.a1"] = {"id": "a1"}
        for g, m in self.members.items():
            if g in self.attrs:
                self.attrs[g] = {"entity_id": list(m)}

    def refresh(self):
        self._sync()

    def dev_entities(self, did):
        return [e for e in self.reg.values() if e.get("device_id") == did]

    def entry_domain(self, eid):
        return (self.entries.get(eid) or {}).get("domain")

    def call(self, t, **p):
        self.guard.check_ws(t, p)
        self.calls.append(("ws", t, copy.deepcopy(p)))
        if t == "config/area_registry/list":
            return [{"area_id": "den"}, {"area_id": "hall"}]
        if t == "lovelace/dashboards/list":
            return [{"url_path": u} for u in self.dash]
        if t == "lovelace/config":
            return copy.deepcopy(self.dash[p["url_path"]])
        if t == "lovelace/config/save":
            self.dash[p["url_path"]] = copy.deepcopy(p["config"])
            return None
        if t == "config/device_registry/update":
            self.devs[p["device_id"]].update({k: v for k, v in p.items() if k != "device_id"})
            return {}
        if t == "config/entity_registry/update":
            e = self.reg.pop(p["entity_id"])
            e.update({k: v for k, v in p.items() if k in ("disabled_by",)})
            e["entity_id"] = p.get("new_entity_id", p["entity_id"])
            self.reg[e["entity_id"]] = e
            return {"entity_entry": e}
        raise AssertionError("unexpected ws " + t)

    def request(self, method, path, body=None):
        self.guard.check_rest(method, path, body)
        self.calls.append(("rest", method, path, copy.deepcopy(body)))
        if path.startswith("/api/config/automation/config/"):
            return copy.deepcopy(self.autos[path.rsplit("/", 1)[1]])
        if path.startswith("/api/config/script/") or path.startswith("/api/config/scene/"):
            raise M.A.HAError("HTTP 404")
        if path == M.OPTIONS_FLOW:
            self._entry = body["handler"]
            return {
                "type": "form",
                "handler": body["handler"],
                "flow_id": FID,
                "data_schema": [
                    {"name": "entities", "default": []},
                    {"name": "hide_members", "default": False},
                    {"name": "all", "default": False},
                ],
            }
        if path == f"{M.OPTIONS_FLOW}/{FID}":
            g = next(k for k, v in self.reg.items() if v.get("config_entry_id") == self._entry)
            self.members[g] = list(body["entities"])
            return {"type": "create_entry"}
        raise AssertionError(path)


def run(fake, guard, apply):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        M.g_registry(fake, guard, apply)
    return out.getvalue()


class Registry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        M.UNDO = os.path.join(self.tmp.name, "undo.json")
        M.REG_BACKUP_DIR = self.tmp.name
        M.REGISTRY_PLAN = plan()
        self._sleep = M.time.sleep
        M.time.sleep = lambda s: None

    def tearDown(self):
        M.time.sleep = self._sleep
        self.tmp.cleanup()

    def test_guard_disabled_by_user_only(self):
        g = M.Guard(writes=True)
        bad = {"entity_id": "light.x", "disabled_by": "integration"}
        with self.assertRaises(M.NotAllowed):
            g.check_ws("config/entity_registry/update", bad)
        ok = {"entity_id": "light.x", "disabled_by": "user"}
        with self.assertRaises(M.NotAllowed):
            g.check_ws("config/entity_registry/update", ok)  # not approved
        g.approve(("config/entity_registry/update", M.canon(ok)))
        g.check_ws("config/entity_registry/update", ok)
        with self.assertRaises(M.NotAllowed):
            M.Guard(writes=True).check_ws("config/entity_registry/update", {"entity_id": "light.x", "name": "y"})

    def test_dry_run_writes_nothing(self):
        g = M.Guard(writes=False)
        f = FakeHA(g)
        out = run(f, g, False)
        self.assertFalse([c for c in f.calls if c[1] in WRITES or (c[0] == "rest" and c[1] != "GET")])
        self.assertIn("name_by_user 'WLED Hall' -> 'Den Strip'", out)
        self.assertIn("group light.den_lights", out)
        self.assertIn("light.den_zgroup still on dashboard", out)
        self.assertFalse(os.path.exists(M.UNDO))

    def test_apply_exact_writes_and_verify(self):
        g = M.Guard(writes=True)
        f = FakeHA(g)
        out = run(f, g, True)
        self.assertIn("verify: OK", out)
        self.assertEqual(f.members["light.den_lights"], ["light.den_lamp"])
        self.assertEqual(f.reg["light.den_zgroup"]["disabled_by"], "user")
        self.assertEqual(f.reg["light.porch_tpl"]["disabled_by"], "user")
        self.assertIn("switch.den_strip_freeze", f.reg)
        self.assertEqual((f.devs[DEV]["name_by_user"], f.devs[DEV]["area_id"]), ("Den Strip", "den"))
        self.assertEqual(f.dash["den-board"]["views"][0]["cards"][0]["entity"], "light.porch_plug")
        self.assertEqual(
            f.dash["home-mobile"]["views"][0]["cards"][0]["entity"], "light.den_zgroup"
        )  # generated: untouched
        order = [
            c[2] if c[0] == "rest" else c[1] for c in f.calls if c[0] == "rest" and c[1] == "POST" or c[1] in WRITES
        ]
        self.assertLess(order.index(M.OPTIONS_FLOW), order.index("config/entity_registry/update"))
        with open(M.UNDO) as fh:
            rec = json.load(fh)["registry"]
        run_rec = next(iter(rec["groups"].values()))
        self.assertTrue(run_rec["verify"]["ok"])
        self.assertEqual(run_rec["before"]["devices"][0]["area_id"], "hall")
        bf = run_rec["before"]["dashboards"][0]["before_file"]
        self.assertEqual(stat.S_IMODE(os.stat(bf).st_mode), 0o600)
        with open(bf) as fh:
            self.assertEqual(json.load(fh)["views"][0]["cards"][0]["entity"], "light.porch_tpl")
        g2 = M.Guard(writes=True)
        f.guard = g2
        self.assertIn("nothing to do", run(f, g2, True))

    def test_used_entity_not_disabled(self):
        g = M.Guard(writes=True)
        f = FakeHA(g, used_by_auto="light.porch_tpl")
        out = run(f, g, True)
        self.assertIn("light.porch_tpl still used by ['automation.a1']: not disabled", out)
        self.assertIsNone(f.reg["light.porch_tpl"]["disabled_by"])
        self.assertEqual(f.reg["light.den_zgroup"]["disabled_by"], "user")  # its group drops it first

    def test_group_never_grows_and_generated_dashboard_refused(self):
        M.REGISTRY_PLAN = {
            "group_members": {"light.den_lights": ["light.den_lamp", "light.porch_plug"]},
            "dashboard_swap": [{"url_path": "home-mobile", "old": "light.den_zgroup", "new": "light.den_lamp"}],
        }
        g = M.Guard(writes=True)
        f = FakeHA(g)
        out = run(f, g, True)
        self.assertIn("only drops allowed", out)
        self.assertIn("generated (ha-dashboard-gen)", out)
        self.assertIn("nothing to do", out)
        self.assertFalse([c for c in f.calls if c[1] in WRITES or (c[0] == "rest" and c[1] != "GET")])

    def test_plan_loads_registry_section(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as t:
            json.dump({"registry": plan()}, t)
        try:
            self.assertEqual(M.load_plan(t.name)["REGISTRY_PLAN"]["devices"][0]["area_id"], "den")
            self.assertEqual(M.load_plan(os.path.join(self.tmp.name, "none.json"))["REGISTRY_PLAN"], {})
        finally:
            os.unlink(t.name)


if __name__ == "__main__":
    unittest.main()
