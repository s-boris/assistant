"""Offline tests for ha-apply automation-delete: dry run writes nothing, --apply backs the config up (0600) before the
DELETE, the read-back checks config + entity + repair issue, --undo POSTs the backup back; YAML-only, autopilot_*,
foreign-platform and unknown automations are refused; the guard arms exactly one DELETE path. Fake HA only."""

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
from contextlib import redirect_stdout

sys.dont_write_bytecode = True
BIN = os.environ.get("HA_APPLY_BIN") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "scripts", "ha-apply"
)
loader = importlib.machinery.SourceFileLoader("ha_apply_ad", BIN)
spec = importlib.util.spec_from_loader("ha_apply_ad", loader)
M = importlib.util.module_from_spec(spec)
loader.exec_module(M)

DEAD = "automation.den_lights_remote"
KEEP = "automation.den_lights_on_presence"


def dead_cfg():
    return {
        "id": "1700000000001",
        "alias": "Den Lights Remote",
        "description": "",
        "mode": "single",
        "triggers": [
            {
                "trigger": "device",
                "domain": "zha",
                "device_id": "a" * 32,
                "type": "remote_button_short_press",
                "subtype": "turn_on",
            }
        ],
        "conditions": [],
        "actions": [{"action": "light.turn_on", "target": {"area_id": "den"}}],
    }


def keep_cfg():
    return {"id": "1700000000002", "alias": "Den Lights Presence", "triggers": [], "actions": []}


class FakeHA:
    def __init__(self, guard, clear_repairs=True):
        self.guard, self.calls, self.clear_repairs = guard, [], clear_repairs
        self.configs = {"1700000000001": dead_cfg(), "1700000000002": keep_cfg()}
        self.states = {
            DEAD: "unavailable",
            KEEP: "off",
            "automation.yaml_only": "on",
            "automation.autopilot_x": "on",
            "light.den": "off",
        }
        self.attrs = {
            DEAD: {"id": "1700000000001"},
            KEEP: {"id": "1700000000002"},
            "automation.autopilot_x": {"id": "autopilot_x"},
        }
        self.reg = {
            e: {"entity_id": e, "platform": "automation", "unique_id": (self.attrs.get(e) or {}).get("id")}
            for e in self.states
            if e.startswith("automation.")
        }
        self.reg["light.den"] = {"entity_id": "light.den", "platform": "hue"}
        self.configs["autopilot_x"] = {"id": "autopilot_x", "alias": "Autopilot x", "triggers": [], "actions": []}
        self.repairs = [{"issue_id": DEAD + "_validation_failed_triggers", "domain": "automation", "ignored": False}]
        self.ws, self.rest = self, self

    def refresh(self):
        pass

    def call(self, t, **p):
        self.guard.check_ws(t, p)
        self.calls.append(("ws", t, p))
        if t == "repairs/list_issues":
            return {"issues": copy.deepcopy(self.repairs)}
        raise AssertionError("unexpected ws " + t)

    def request(self, method, path, body=None):
        self.guard.check_rest(method, path, body)
        self.calls.append(("rest", method, path))
        aid = path.rsplit("/", 1)[1]
        if method == "GET":
            if aid not in self.configs:
                raise M.A.HAError(f"HTTP 404 on GET {path}")
            return copy.deepcopy(self.configs[aid])
        if method == "DELETE":
            self.configs.pop(aid)
            for e in [e for e, a in self.attrs.items() if str(a.get("id")) == aid]:
                self.states.pop(e, None)
                self.reg.pop(e, None)
                self.attrs.pop(e, None)
                if self.clear_repairs:
                    self.repairs = [r for r in self.repairs if e not in r["issue_id"]]
            return {"result": "ok"}
        if method == "POST":
            self.configs[aid] = json.loads(json.dumps(dict(body, id=aid)))
            eid = "automation." + M._slugify(body["alias"])
            self.states[eid] = "unavailable"
            self.attrs[eid] = {"id": aid}
            self.reg[eid] = {"entity_id": eid, "platform": "automation", "unique_id": aid}
            return {"result": "ok"}
        raise AssertionError(method)


def run(fn, *a, **k):
    out = io.StringIO()
    with redirect_stdout(out):
        fn(*a, **k)
    return out.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        M.UNDO = os.path.join(self.tmp.name, "undo.json")
        M.AUTO_BACKUP_DIR = os.path.join(self.tmp.name, "backups")
        M.time.sleep = lambda s: None

    def tearDown(self):
        self.tmp.cleanup()

    def writes(self, c):
        return [x for x in c.calls if (x[0] == "rest" and x[1] != "GET") or (x[0] == "ws" and x[1] not in M.WS_READ)]


class AutomationDelete(Base):
    def test_dry_run_writes_nothing(self):
        c = FakeHA(M.Guard(writes=False))
        out = run(M.g_automation_delete, c, c.guard, False, automation=DEAD)
        self.assertIn("config id 1700000000001; alias 'Den Lights Remote'; repairs open: 1", out)
        self.assertEqual(self.writes(c), [])
        self.assertFalse(os.path.exists(M.AUTO_BACKUP_DIR))

    def test_dry_run_guard_blocks_delete(self):
        c = FakeHA(M.Guard(writes=False))
        with self.assertRaises(M.NotAllowed):
            run(M.g_automation_delete, c, c.guard, True, automation=DEAD)
        self.assertIn("1700000000001", c.configs)

    def test_apply_backs_up_deletes_verifies_and_undoes(self):
        c = FakeHA(M.Guard(writes=True))
        out = run(M.g_automation_delete, c, c.guard, True, automation=DEAD)
        self.assertIn("verify: OK", out)
        self.assertIn("repairs open: 0", out.split("read-back:")[1])
        self.assertNotIn("1700000000001", c.configs)
        self.assertNotIn(DEAD, c.states)
        self.assertEqual(c.configs["1700000000002"], keep_cfg())
        self.assertIn(KEEP, c.states)
        files = os.listdir(M.AUTO_BACKUP_DIR)
        self.assertEqual(len(files), 1)
        bpath = os.path.join(M.AUTO_BACKUP_DIR, files[0])
        self.assertEqual(stat.S_IMODE(os.stat(bpath).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(M.AUTO_BACKUP_DIR).st_mode), 0o700)
        with open(bpath) as f:
            self.assertEqual(json.load(f)["config"], dead_cfg())
        # backup written before the DELETE
        kinds = [x[1] for x in self.writes(c)]
        self.assertEqual(kinds, ["DELETE"])
        with open(M.UNDO) as f:
            rec = json.load(f)["automation-delete"]["groups"][DEAD]
        self.assertTrue(rec["verify"]["ok"])
        self.assertEqual(rec["before"]["backup"], bpath)
        # undo: dry run first, then POST back
        dry = run(M.g_automation_delete, c, M.Guard(writes=False), False, automation=DEAD, undo=True)
        self.assertIn("POST the config", dry)
        self.assertNotIn("1700000000001", c.configs)
        out = run(M.g_automation_delete, c, c.guard, True, automation=DEAD, undo=True)
        self.assertIn("verify: OK", out)
        self.assertEqual(c.configs["1700000000001"], dead_cfg())
        self.assertIn(DEAD, c.states)
        # a second undo refuses (id exists)
        with self.assertRaises(SystemExit):
            run(M.g_automation_delete, c, c.guard, True, automation=DEAD, undo=True)

    def test_repair_left_open_is_mismatch(self):
        c = FakeHA(M.Guard(writes=True), clear_repairs=False)
        out = run(M.g_automation_delete, c, c.guard, True, automation=DEAD)
        self.assertIn("verify: MISMATCH", out)
        self.assertIn("repairs open: 1", out.split("read-back:")[1])

    def test_refusals(self):
        for eid in ("automation.yaml_only", "automation.autopilot_x", "automation.missing", "light.den", "", None):
            c = FakeHA(M.Guard(writes=True))
            with self.assertRaises(SystemExit, msg=str(eid)):
                run(M.g_automation_delete, c, c.guard, True, automation=eid)
            self.assertEqual(self.writes(c), [], str(eid))
        c = FakeHA(M.Guard(writes=True))  # entity says it has a config id, but the store does not know it
        c.configs.pop("1700000000001")
        with self.assertRaises(SystemExit):
            run(M.g_automation_delete, c, c.guard, True, automation=DEAD)
        c = FakeHA(M.Guard(writes=True))  # foreign platform
        c.reg[DEAD]["platform"] = "template"
        with self.assertRaises(SystemExit):
            run(M.g_automation_delete, c, c.guard, True, automation=DEAD)
        c = FakeHA(M.Guard(writes=True))  # undo without a backup
        with self.assertRaises(SystemExit):
            run(M.g_automation_delete, c, c.guard, True, automation=DEAD, undo=True)
        self.assertEqual(self.writes(c), [])


class GuardArm(unittest.TestCase):
    def test_arm_delete_one_path(self):
        g = M.Guard(writes=True)
        p1, p2 = "/api/config/automation/config/1", "/api/config/automation/config/2"
        g.approve(("rest", "DELETE", p1))
        with self.assertRaises(M.NotAllowed):  # approved but not armed
            g.check_rest("DELETE", p1)
        g.arm_delete(p1)
        g.check_rest("DELETE", p1)
        with self.assertRaises(M.NotAllowed):  # one per run
            g.arm_delete(p2)
        g.approve(("rest", "DELETE", p2))
        with self.assertRaises(M.NotAllowed):
            g.check_rest("DELETE", p2)
        with self.assertRaises(M.NotAllowed):
            M.Guard(writes=True).arm_delete("/api/config/script/config/x")
        d = M.Guard(writes=False)
        d.arm_delete(p1)
        d.approve(("rest", "DELETE", p1))
        with self.assertRaises(M.NotAllowed):  # dry run
            d.check_rest("DELETE", p1)
        a = M.Guard(writes=True)
        a.arm_delete(p1)
        with self.assertRaises(M.NotAllowed):  # armed but not approved
            a.check_rest("DELETE", p1)


if __name__ == "__main__":
    unittest.main()
