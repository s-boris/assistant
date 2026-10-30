"""Offline tests for ha-apply group-helper: set members via the options flow (backup 0600 first, other options kept,
read-back, undo), delete a UI group helper only while unreferenced (backup, entry + entity gone, undo recreates it via
the group config flow under the same entity id), refusals, dry runs write nothing, the guard only starts group config
flows. Fake HA only."""

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
loader = importlib.machinery.SourceFileLoader("ha_apply_gh", BIN)
spec = importlib.util.spec_from_loader("ha_apply_gh", loader)
M = importlib.util.module_from_spec(spec)
loader.exec_module(M)

HOME = "binary_sensor.den_presence"
UP = "binary_sensor.up_presence"
DOWN = "binary_sensor.down_presence"
S = ["binary_sensor.room_%s" % x for x in ("a", "b", "c")]  # noqa: UP031
FID = "f" * 32
NEW_ENTRY = "N" * 26


def slug(t):
    return M._slugify(t)


class FakeHA:
    def __init__(self, guard):
        self.guard, self.calls = guard, []
        self.entries = {
            "E" * 26: {"entry_id": "E" * 26, "domain": "group", "title": "Den Presence"},
            "U" * 26: {"entry_id": "U" * 26, "domain": "group", "title": "Up Presence"},
            "D" * 26: {"entry_id": "D" * 26, "domain": "group", "title": "Down Presence"},
            "Z" * 26: {"entry_id": "Z" * 26, "domain": "aqara", "title": "FP2"},
        }
        self.opts = {
            "E" * 26: {"entities": [UP, DOWN], "hide_members": False, "all": False},
            "U" * 26: {"entities": [S[0]], "hide_members": False, "all": True},
            "D" * 26: {"entities": [S[1], S[2]], "hide_members": True, "all": False},
        }
        self.reg = {
            HOME: {"entity_id": HOME, "platform": "group", "config_entry_id": "E" * 26, "unique_id": "u1"},
            UP: {"entity_id": UP, "platform": "group", "config_entry_id": "U" * 26, "unique_id": "u2"},
            DOWN: {"entity_id": DOWN, "platform": "group", "config_entry_id": "D" * 26, "unique_id": "u3"},
            "binary_sensor.yaml_group": {
                "entity_id": "binary_sensor.yaml_group",
                "platform": "group",
                "config_entry_id": None,
            },
        }
        for x in S:
            self.reg[x] = {"entity_id": x, "platform": "aqara", "config_entry_id": "Z" * 26}
        self.autos = {"a1": {"id": "a1", "triggers": [{"trigger": "state", "entity_id": HOME}], "actions": []}}
        self.dash = {"board": {"views": [{"cards": [{"type": "tile", "entity": "light.x"}]}]}}
        self.flow = None
        self.rename_to = None  # recreate: HA picks this id instead of the old one
        self._sync()
        self.ws, self.rest = self, self

    def _sync(self):
        self.states = {e: "on" for e in self.reg}
        self.states["light.x"] = "off"
        self.states["automation.a1"] = "on"
        self.attrs = {e: {} for e in self.states}
        self.attrs["automation.a1"] = {"id": "a1"}
        for e, r in self.reg.items():
            ce = r.get("config_entry_id")
            if ce in self.opts:
                self.attrs[e] = {"entity_id": list(self.opts[ce]["entities"])}

    def refresh(self):
        self._sync()

    def entry_domain(self, eid):
        return (self.entries.get(eid) or {}).get("domain")

    def call(self, t, **p):
        self.guard.check_ws(t, p)
        self.calls.append(("ws", t, copy.deepcopy(p)))
        if t == "lovelace/dashboards/list":
            return [{"url_path": u} for u in self.dash]
        if t == "lovelace/config":
            return copy.deepcopy(self.dash[p["url_path"]])
        if t == "config/entity_registry/update":
            e = self.reg.pop(p["entity_id"])
            e["entity_id"] = p["new_entity_id"]
            self.reg[e["entity_id"]] = e
            return {"entity_entry": e}
        raise AssertionError("unexpected ws " + t)

    def _form(self, ce):
        o = self.opts[ce]
        return [
            {"name": "entities", "required": True, "description": {"suggested_value": list(o["entities"])}},
            {
                "name": "hide_members",
                "required": True,
                "default": False,
                "description": {"suggested_value": o["hide_members"]},
            },
            {"name": "all", "required": True, "default": False, "description": {"suggested_value": o["all"]}},
        ]

    def request(self, method, path, body=None):
        self.guard.check_rest(method, path, body)
        self.calls.append(("rest", method, path, copy.deepcopy(body)))
        if path.startswith("/api/config/automation/config/"):
            return copy.deepcopy(self.autos[path.rsplit("/", 1)[1]])
        if path.startswith("/api/config/script/") or path.startswith("/api/config/scene/"):
            raise M.A.HAError("HTTP 404")
        if path == M.OPTIONS_FLOW:
            self.flow = ("options", body["handler"])
            return {
                "type": "form",
                "handler": body["handler"],
                "flow_id": FID,
                "data_schema": self._form(body["handler"]),
            }
        if path == f"{M.OPTIONS_FLOW}/{FID}":
            if method == "DELETE":
                self.flow = None
                return None
            self.opts[self.flow[1]] = {k: copy.deepcopy(body[k]) for k in ("entities", "hide_members", "all")}
            self.flow = None
            return {"type": "create_entry"}
        if path.startswith("/api/config/config_entries/entry/"):
            ce = path.rsplit("/", 1)[1]
            self.entries.pop(ce)
            self.opts.pop(ce)
            for e in [e for e, r in self.reg.items() if r.get("config_entry_id") == ce]:
                self.reg.pop(e)
            return {"require_restart": False}
        if path == M.CONFIG_FLOW:
            self.flow = ("config", "user")
            return {
                "type": "menu",
                "flow_id": FID,
                "handler": "group",
                "step_id": "user",
                "menu_options": ["binary_sensor", "light", "switch"],
            }
        if path == f"{M.CONFIG_FLOW}/{FID}":
            if method == "DELETE":
                self.flow = None
                return None
            if "next_step_id" in body:
                self.flow = ("config", body["next_step_id"])
                return {
                    "type": "form",
                    "flow_id": FID,
                    "handler": "group",
                    "step_id": body["next_step_id"],
                    "data_schema": [
                        {"name": "name", "required": True},
                        {"name": "entities", "required": True},
                        {"name": "hide_members", "required": True, "default": False},
                        {"name": "all", "required": True, "default": False},
                    ],
                }
            self.entries[NEW_ENTRY] = {"entry_id": NEW_ENTRY, "domain": "group", "title": body["name"]}
            self.opts[NEW_ENTRY] = {k: copy.deepcopy(body[k]) for k in ("entities", "hide_members", "all")}
            eid = self.rename_to or "binary_sensor." + slug(body["name"])
            self.reg[eid] = {"entity_id": eid, "platform": "group", "config_entry_id": NEW_ENTRY}
            self.flow = None
            return {"type": "create_entry", "result": {"entry_id": NEW_ENTRY, "domain": "group"}}
        raise AssertionError(path)


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
        M.PKG_DIR = os.path.join(self.tmp.name, "pkgs")
        os.makedirs(M.PKG_DIR)
        self._sleep = M.time.sleep
        M.time.sleep = lambda s: None

    def tearDown(self):
        M.time.sleep = self._sleep
        self.tmp.cleanup()

    def writes(self, c):
        return [x for x in c.calls if (x[0] == "rest" and x[1] != "GET") or (x[0] == "ws" and x[1] not in M.WS_READ)]

    def backups(self):
        d = M.AUTO_BACKUP_DIR
        return sorted(os.listdir(d)) if os.path.isdir(d) else []


class SetMembers(Base):
    def test_dry_run_writes_nothing(self):
        c = FakeHA(M.Guard(writes=False))
        out = run(M.g_group_helper, c, c.guard, False, group=HOME, members=",".join(S))
        self.assertIn("adds ['binary_sensor.room_a'", out)
        self.assertIn("drops", out)
        self.assertEqual(self.writes(c), [])
        self.assertEqual(self.backups(), [])

    def test_dry_run_guard_blocks(self):
        c = FakeHA(M.Guard(writes=False))
        with self.assertRaises(M.NotAllowed):
            run(M.g_group_helper, c, c.guard, True, group=HOME, members=",".join(S))
        self.assertEqual(c.opts["E" * 26]["entities"], [UP, DOWN])

    def test_apply_backup_keep_options_verify_undo(self):
        c = FakeHA(M.Guard(writes=True))
        c.opts["E" * 26]["all"] = True
        out = run(M.g_group_helper, c, c.guard, True, group=HOME, members=",".join(S))
        self.assertIn("verify: OK", out)
        self.assertEqual(c.opts["E" * 26], {"entities": S, "hide_members": False, "all": True})
        files = self.backups()
        self.assertEqual(len(files), 1)
        b = os.path.join(M.AUTO_BACKUP_DIR, files[0])
        self.assertEqual(stat.S_IMODE(os.stat(b).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(M.AUTO_BACKUP_DIR).st_mode), 0o700)
        with open(b) as f:
            rec = json.load(f)
        self.assertEqual(rec["kind"], "members")
        self.assertEqual(rec["options"]["entities"], [UP, DOWN])
        self.assertEqual(rec["title"], "Den Presence")
        with open(M.UNDO) as f:
            self.assertTrue(json.load(f)["group-helper"]["groups"][HOME]["verify"]["ok"])
        self.assertIn(f"undo: ha-apply group-helper --group {HOME} --undo --apply", out)
        # nothing to do on a second run
        again = run(M.g_group_helper, c, c.guard, True, group=HOME, members=",".join(S))
        self.assertIn("nothing to do", again)
        # undo puts the old members back, options still kept
        out = run(M.g_group_helper, c, c.guard, True, group=HOME, undo=True)
        self.assertIn("verify: OK", out)
        self.assertEqual(c.opts["E" * 26], {"entities": [UP, DOWN], "hide_members": False, "all": True})
        # the undo's own backup is skipped: a second undo is a no-op
        self.assertEqual(len(self.backups()), 2)
        again = run(M.g_group_helper, c, c.guard, True, group=HOME, undo=True)
        self.assertIn("nothing to do", again)

    def test_refusals(self):
        cases = [
            dict(group="binary_sensor.yaml_group", members=S[0]),
            dict(group=S[0], members=S[1]),
            dict(group="binary_sensor.missing", members=S[0]),
            dict(group=HOME, members="light.x"),
            dict(group=HOME, members="binary_sensor.gone"),
            dict(group=HOME, members=f"{S[0]},{S[0]}"),
            dict(group=HOME, members=HOME),
            dict(group=HOME, members=" , "),
            dict(group="", members=S[0]),
            dict(group=HOME, members=S[0], delete=True),
            dict(group=HOME),
        ]
        for k in cases:
            c = FakeHA(M.Guard(writes=True))
            with self.assertRaises(SystemExit, msg=str(k)):
                run(M.g_group_helper, c, c.guard, True, **k)
            self.assertEqual(self.writes(c), [], str(k))
        c = FakeHA(M.Guard(writes=True))  # entry owns two entities
        c.reg["binary_sensor.twin"] = dict(c.reg[HOME], entity_id="binary_sensor.twin")
        with self.assertRaises(SystemExit):
            run(M.g_group_helper, c, c.guard, True, group=HOME, members=S[0])


class Delete(Base):
    def test_refused_while_referenced(self):
        c = FakeHA(M.Guard(writes=True))  # UP is a member of HOME
        with self.assertRaises(SystemExit):
            run(M.g_group_helper, c, c.guard, True, group=UP, delete=True)
        c = FakeHA(M.Guard(writes=True))  # HOME is used by an automation
        with self.assertRaises(SystemExit):
            run(M.g_group_helper, c, c.guard, True, group=HOME, delete=True)
        c = FakeHA(M.Guard(writes=True))  # dashboard
        c.opts["E" * 26]["entities"] = [S[0]]
        c._sync()
        c.dash["board"]["views"][0]["cards"].append({"type": "tile", "entity": UP})
        with self.assertRaises(SystemExit):
            out = run(M.g_group_helper, c, c.guard, True, group=UP, delete=True)  # noqa: F841
        c = FakeHA(M.Guard(writes=True))  # a local package
        c.opts["E" * 26]["entities"] = [S[0]]
        c._sync()
        os.makedirs(os.path.join(M.PKG_DIR, "staged"))
        with open(os.path.join(M.PKG_DIR, "staged", "x.json"), "w") as f:
            json.dump({"cond": UP}, f)
        with self.assertRaises(SystemExit):
            run(M.g_group_helper, c, c.guard, True, group=UP, delete=True)
        self.assertEqual(self.writes(c), [])
        self.assertIn(UP, c.reg)

    def test_backups_dir_is_not_a_reference(self):
        c = FakeHA(M.Guard(writes=False))
        c.opts["E" * 26]["entities"] = [S[0]]
        c._sync()
        os.makedirs(os.path.join(M.PKG_DIR, "backups"))
        with open(os.path.join(M.PKG_DIR, "backups", "old.json"), "w") as f:
            json.dump({"x": UP}, f)
        out = run(M.g_group_helper, c, c.guard, False, group=UP, delete=True)
        self.assertIn("then: back up", out)
        self.assertEqual(self.writes(c), [])

    def test_delete_and_undo_recreates(self):
        c = FakeHA(M.Guard(writes=True))
        run(M.g_group_helper, c, c.guard, True, group=HOME, members=",".join(S))
        out = run(M.g_group_helper, c, c.guard, True, group=DOWN, delete=True)
        self.assertIn("verify: OK", out)
        self.assertNotIn(DOWN, c.reg)
        self.assertNotIn("D" * 26, c.entries)
        deletes = [x for x in self.writes(c) if x[1] == "DELETE" and "/entry/" in x[2]]
        self.assertEqual([x[2] for x in deletes], ["/api/config/config_entries/entry/" + "D" * 26])
        b = [f for f in self.backups() if "down" in f]
        with open(os.path.join(M.AUTO_BACKUP_DIR, b[0])) as f:
            rec = json.load(f)
        self.assertEqual((rec["kind"], rec["title"], rec["options"]["hide_members"]), ("delete", "Down Presence", True))
        # undo: dry run writes nothing, then the config flow recreates it
        n = len(self.writes(c))
        dry = run(M.g_group_helper, c, M.Guard(writes=False), False, group=DOWN, undo=True)
        self.assertIn("recreate binary_sensor group 'Down Presence'", dry)
        self.assertEqual(len(self.writes(c)), n)
        out = run(M.g_group_helper, c, c.guard, True, group=DOWN, undo=True)
        self.assertIn("verify: OK", out)
        self.assertEqual(c.opts[NEW_ENTRY], {"entities": [S[1], S[2]], "hide_members": True, "all": False})
        self.assertIn(DOWN, c.reg)
        with self.assertRaises(SystemExit):  # exists now
            run(M.g_group_helper, c, c.guard, True, group=DOWN, undo=True)

    def test_recreate_renames_to_old_id(self):
        c = FakeHA(M.Guard(writes=True))
        c.opts["E" * 26]["entities"] = [S[0]]
        c._sync()
        run(M.g_group_helper, c, c.guard, True, group=UP, delete=True)
        c.rename_to = "binary_sensor.up_presence_2"
        out = run(M.g_group_helper, c, c.guard, True, group=UP, undo=True)
        self.assertIn("verify: OK", out)
        self.assertIn(UP, c.reg)
        self.assertNotIn("binary_sensor.up_presence_2", c.reg)

    def test_undo_without_backup(self):
        c = FakeHA(M.Guard(writes=True))
        with self.assertRaises(SystemExit):
            run(M.g_group_helper, c, c.guard, True, group=HOME, undo=True)


class GuardFlows(unittest.TestCase):
    def test_only_group_config_flow(self):
        g = M.Guard(writes=True)
        for body in ({"handler": "zha"}, {"handler": "group", "show_advanced_options": True}, None):
            g.approve(("rest", "POST", M.CONFIG_FLOW, M.canon(body)))
            with self.assertRaises(M.NotAllowed):
                g.check_rest("POST", M.CONFIG_FLOW, body)
        body = {"handler": "group"}
        with self.assertRaises(M.NotAllowed):  # not approved
            g.check_rest("POST", M.CONFIG_FLOW, body)
        g.approve(("rest", "POST", M.CONFIG_FLOW, M.canon(body)))
        g.check_rest("POST", M.CONFIG_FLOW, body)
        with self.assertRaises(M.NotAllowed):  # body-keyed step
            g.check_rest("POST", f"{M.CONFIG_FLOW}/{FID}", {"name": "x"})
        with self.assertRaises(M.NotAllowed):
            M.Guard(writes=False).check_rest("POST", M.CONFIG_FLOW, body)


if __name__ == "__main__":
    unittest.main()
