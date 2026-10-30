"""Offline tests for ha-apply autopilot / autopilot-undo / remote-swap: package validation, dry run writes nothing,
--apply needs the package sha, helpers + automations created and read back, idempotent, missing entities and
id collisions refused, undo deletes only what autopilot created, remote-swap swaps a removed device id for a
re-paired one of the same model and integration (and posts the before-config back on --undo). Fake HA only."""

import copy
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.dont_write_bytecode = True
BIN = os.environ.get("HA_APPLY_BIN") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "scripts", "ha-apply"
)
loader = importlib.machinery.SourceFileLoader("ha_apply_ap", BIN)
spec = importlib.util.spec_from_loader("ha_apply_ap", loader)
M = importlib.util.module_from_spec(spec)
loader.exec_module(M)

PKG = {
    "version": 1,
    "parts": {
        "away": {"helpers": ["away_mode", "autopilot_event"], "automations": ["autopilot_away_mode"]},
        "left-on": {"helpers": ["autopilot_event"], "automations": ["autopilot_left_on"]},
        "arrival": {"helpers": [], "automations": ["autopilot_arrival"]},
    },
    "helpers": {
        "away_mode": {"type": "input_boolean", "create": {"name": "Away mode", "icon": "mdi:airplane"}},
        "autopilot_event": {
            "type": "input_text",
            "create": {"name": "Autopilot event", "min": 0, "max": 255, "mode": "text"},
        },
    },
    "automations": {
        "autopilot_away_mode": {
            "alias": "Autopilot: away mode",
            "mode": "queued",
            "triggers": [{"trigger": "state", "entity_id": "input_boolean.away_mode", "to": "on"}],
            "actions": [
                {"action": "light.turn_off", "target": {"entity_id": "all"}},
                {
                    "action": "input_text.set_value",
                    "target": {"entity_id": "input_text.autopilot_event"},
                    "data": {"value": "away_on"},
                },
            ],
        },
        "autopilot_left_on": {
            "alias": "Autopilot: left on",
            "triggers": [{"trigger": "state", "entity_id": "zone.home", "to": "0"}],
            "conditions": [
                {"condition": "template", "value_template": "{{ not is_state('input_boolean.away_mode', 'on') }}"}
            ],
            "actions": [
                {
                    "action": "input_text.set_value",
                    "target": {"entity_id": "input_text.autopilot_event"},
                    "data": {"value": "left_on"},
                }
            ],
        },
        "autopilot_arrival": {
            "alias": "Autopilot: arrival",
            "triggers": [{"trigger": "state", "entity_id": "zone.home", "from": "0"}],
            "actions": [
                {"action": "light.turn_on", "target": {"entity_id": "light.room_a"}},
                {"action": "notify.send_message", "target": {"entity_id": "notify.owner_phone"}},
            ],
        },
    },
}
OLD_A, OLD_B = "a" * 32, "b" * 32
NEW_A, NEW_B, OTHER = "c" * 32, "d" * 32, "e" * 32
REMOTE = "automation.den_lights_remote"  # fixtures/plan.json remote_swap


def remote_cfg():
    trig = [
        {"trigger": "device", "domain": "zha", "device_id": d, "type": "remote_button_short_press", "subtype": s}
        for d in (OLD_A, OLD_B)
        for s in ("turn_on", "turn_off")
    ]
    return {
        "id": "42",
        "alias": "Hallway Lights Remote",
        "triggers": trig,
        "actions": [{"action": "light.turn_on", "target": {"area_id": ["hallway"]}}],
    }


class FakeHA:
    def __init__(self, guard):
        self.guard, self.calls, self.configs = guard, [], {}
        self.reg, self.states, self.attrs = {}, {}, {}
        self.helpers = {"input_boolean": {}, "input_text": {}, "input_datetime": {}}
        for e, st in (
            ("light.room_a", "off"),
            ("notify.owner_phone", "unknown"),
            ("zone.home", "1"),
            (REMOTE, "unavailable"),
        ):
            self.reg[e] = {"entity_id": e, "platform": "x"}
            self.states[e] = st
        self.attrs[REMOTE] = {"id": "42"}
        self.configs["42"] = remote_cfg()
        self.entries = {"z1": {"entry_id": "z1", "domain": "zha"}, "h1": {"entry_id": "h1", "domain": "hue"}}
        self.devs = {
            NEW_A: {"id": NEW_A, "name": "Hallway Door Dimmer", "model": "RODRET Dimmer", "config_entries": ["z1"]},
            NEW_B: {"id": NEW_B, "name": "Hallway Stairs Dimmer", "model": "RODRET Dimmer", "config_entries": ["z1"]},
            OTHER: {"id": OTHER, "name": "Hue Dimmer", "model": "RWL022", "config_entries": ["h1"]},
        }
        self.audit = {
            "dev_dead": [
                {"id": OLD_A, "name": "Old door dimmer", "model": "RODRET Dimmer", "integration": "zha"},
                {"id": OLD_B, "name": "Old stairs dimmer", "model": "RODRET Dimmer", "integration": "zha"},
            ]
        }
        self.repairs = [
            {
                "issue_id": "unknown_device_42",
                "domain": "automation",
                "ignored": False,
                "translation_placeholders": {"entity_id": REMOTE},
            }
        ]
        self.ws, self.rest = self, self

    def refresh(self):
        pass

    def entry_domain(self, eid):
        return (self.entries.get(eid) or {}).get("domain")

    def _reload(self, aid):
        cfg = self.configs.get(aid)
        for e in [
            e for e in self.states if e.startswith("automation.") and str(self.attrs.get(e, {}).get("id")) == aid
        ]:
            if cfg is None:
                self.states.pop(e)
                self.reg.pop(e, None)
        if cfg is not None:
            # like HA: an existing automation keeps its entity id when the alias changes
            had = [
                e for e in self.states if e.startswith("automation.") and str(self.attrs.get(e, {}).get("id")) == aid
            ]
            eid = had[0] if had else "automation." + M._slugify(cfg["alias"])
            if aid == "42":
                eid = REMOTE
                ok = all(t.get("device_id") in self.devs for t in cfg["triggers"])
                self.states[eid] = "on" if ok else "unavailable"
                if ok:
                    self.repairs = []
            else:
                self.states.setdefault(eid, "on")
            self.reg[eid] = {"entity_id": eid, "platform": "automation", "unique_id": aid}
            self.attrs[eid] = {"id": aid}

    def call(self, t, **p):
        self.guard.check_ws(t, p)
        self.calls.append(("ws", t, p))
        if t.endswith("/list") and t.split("/")[0] in self.helpers:
            return [dict(v, id=k) for k, v in self.helpers[t.split("/")[0]].items()]
        if t.endswith("/create"):
            typ = t.split("/")[0]
            hid = M._slugify(p["name"])
            self.helpers[typ][hid] = dict(p)
            self.states[f"{typ}.{hid}"] = "off" if typ == "input_boolean" else ""
            self.reg[f"{typ}.{hid}"] = {"entity_id": f"{typ}.{hid}", "platform": typ}
            return dict(p, id=hid)
        if t.endswith("/delete"):
            typ = t.split("/")[0]
            hid = p[f"{typ}_id"]
            self.helpers[typ].pop(hid, None)
            self.states.pop(f"{typ}.{hid}", None)
            self.reg.pop(f"{typ}.{hid}", None)
            return None
        if t == "repairs/list_issues":
            return {"issues": copy.deepcopy(self.repairs)}
        raise AssertionError("unexpected ws " + t)

    def request(self, method, path, body=None):
        self.guard.check_rest(method, path, body)
        self.calls.append(("rest", method, path))
        if path.startswith("/api/states/"):
            return {"state": self.states.get(path.rsplit("/", 1)[1]), "attributes": {}}
        aid = path.rsplit("/", 1)[1]
        if method == "GET":
            if aid not in self.configs:
                raise M.A.HAError(f"HTTP 404 on GET {path}")
            return copy.deepcopy(self.configs[aid])
        if method == "POST":
            self.configs[aid] = json.loads(json.dumps(dict(body, id=aid)))
            self._reload(aid)
            return {"result": "ok"}
        if method == "DELETE":
            self.configs.pop(aid, None)
            self._reload(aid)
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
        M.time.sleep = lambda s: None
        self.pkg_path = os.path.join(self.tmp.name, "pkg.json")
        self.write_pkg(PKG)

    def write_pkg(self, pkg):
        with open(self.pkg_path, "w") as f:
            json.dump(pkg, f)
        self.sha = hashlib.sha256(M.canon(pkg).encode()).hexdigest()

    def tearDown(self):
        self.tmp.cleanup()

    def load_undo(self):
        with open(M.UNDO) as f:
            return json.load(f)

    def writes(self, c):
        return [x for x in c.calls if (x[0] == "rest" and x[1] != "GET") or (x[0] == "ws" and x[1] not in M.WS_READ)]


class Package(Base):
    def test_validation(self):
        for bad in (
            {"automations": {"my_auto": {"alias": "My auto"}}},
            {"automations": {"autopilot_x": {"alias": "Something else"}}},
            {"helpers": {"away_mode": {"type": "input_number", "create": {"name": "Away mode"}}}},
            {"helpers": {"away_mode": {"type": "input_boolean", "create": {"name": "Away", "initial": True}}}},
            {"parts": {"x": {"automations": ["autopilot_nope"]}}},
        ):
            pkg = copy.deepcopy(PKG)
            for k, v in bad.items():
                pkg[k] = v if k == "parts" else dict(pkg[k], **v)
            self.write_pkg(pkg)
            with self.assertRaises(SystemExit, msg=str(bad)):
                M.ap_load(self.pkg_path)

    def test_refs_skip_service_names(self):
        refs = M.ap_refs(PKG["automations"]["autopilot_left_on"])
        self.assertEqual(refs, {"zone.home", "input_boolean.away_mode", "input_text.autopilot_event"})
        self.assertNotIn("light.turn_off", M.ap_refs(PKG["automations"]["autopilot_away_mode"]))

    def test_guard_helpers_exact(self):
        g = M.Guard(writes=True)
        msg = {"name": "Away mode", "icon": "mdi:airplane"}
        with self.assertRaises(M.NotAllowed):
            g.check_ws("input_boolean/create", msg)
        g.approve(M.target_key("input_boolean/create", msg))
        g.check_ws("input_boolean/create", msg)
        with self.assertRaises(M.NotAllowed):
            g.check_ws("input_boolean/create", {"name": "Other"})
        with self.assertRaises(M.NotAllowed):
            g.check_ws("input_boolean/create", dict(msg, initial=True))
        with self.assertRaises(M.NotAllowed):
            g.check_ws("input_number/create", {"name": "x"})


class Autopilot(Base):
    def test_dry_run_writes_nothing(self):
        c = FakeHA(M.Guard(writes=False))
        out = run(M.g_autopilot, c, c.guard, False, pkg_path=self.pkg_path)
        self.assertIn(self.sha[:12], out)
        self.assertIn("helper input_boolean.away_mode: create", out)
        self.assertEqual(self.writes(c), [])
        self.assertFalse(os.path.exists(M.UNDO))

    def test_apply_needs_matching_sha(self):
        for sha in (None, "1234567", "00000000"):
            c = FakeHA(M.Guard(writes=True))
            with self.assertRaises(SystemExit):
                run(M.g_autopilot, c, c.guard, True, sha=sha, pkg_path=self.pkg_path)
            self.assertEqual(self.writes(c), [])

    def test_apply_creates_reads_back_and_is_idempotent(self):
        c = FakeHA(M.Guard(writes=True))
        out = run(M.g_autopilot, c, c.guard, True, sha=self.sha[:10], pkg_path=self.pkg_path)
        self.assertIn("verify: OK", out)
        self.assertEqual(sorted(x for x in c.configs if x != "42"), sorted(PKG["automations"]))
        for aid in PKG["automations"]:
            self.assertEqual(c.states[f"automation.{aid}"], "on")
        self.assertIn("input_boolean.away_mode", c.states)
        self.assertIn("input_text.autopilot_event", c.states)
        rec = self.load_undo()["autopilot"]["groups"]["autopilot"]
        self.assertEqual(sorted(rec["created_helpers"]), ["input_boolean.away_mode", "input_text.autopilot_event"])
        self.assertTrue(rec["verify"]["ok"])
        self.assertIsNone(rec["automations"]["autopilot_arrival"]["before"])
        n = len(c.calls)
        out = run(M.g_autopilot, c, c.guard, True, sha=self.sha[:10], pkg_path=self.pkg_path)
        self.assertIn("nothing to do", out)
        self.assertEqual(self.writes(type("X", (), {"calls": c.calls[n:]})), [])

    def test_only_part(self):
        c = FakeHA(M.Guard(writes=True))
        run(M.g_autopilot, c, c.guard, True, only="left-on", sha=self.sha[:8], pkg_path=self.pkg_path)
        self.assertEqual([x for x in c.configs if x != "42"], ["autopilot_left_on"])
        self.assertNotIn("input_boolean.away_mode", c.states)
        self.assertIn("input_text.autopilot_event", c.states)
        with self.assertRaises(SystemExit):
            run(M.g_autopilot, c, c.guard, False, only="nope", pkg_path=self.pkg_path)

    def test_missing_entity_refused(self):
        c = FakeHA(M.Guard(writes=True))
        del c.reg["notify.owner_phone"], c.states["notify.owner_phone"]
        out = run(M.g_autopilot, c, c.guard, False, pkg_path=self.pkg_path)
        self.assertIn("MISSING ['notify.owner_phone']", out)
        with self.assertRaises(SystemExit):
            run(M.g_autopilot, c, c.guard, True, sha=self.sha[:8], pkg_path=self.pkg_path)
        self.assertEqual(self.writes(c), [])

    def test_collision_refused(self):
        c = FakeHA(M.Guard(writes=True))
        c.states["automation.autopilot_arrival"] = "on"
        c.attrs["automation.autopilot_arrival"] = {"id": "1700000000000"}
        with self.assertRaises(SystemExit):
            run(M.g_autopilot, c, c.guard, True, sha=self.sha[:8], pkg_path=self.pkg_path)
        self.assertEqual(self.writes(c), [])

    def test_existing_helper_reused_and_kept_on_undo(self):
        c = FakeHA(M.Guard(writes=True))
        c.helpers["input_boolean"]["away_mode"] = {"name": "Away mode"}
        c.states["input_boolean.away_mode"] = "off"
        c.reg["input_boolean.away_mode"] = {"entity_id": "input_boolean.away_mode", "platform": "input_boolean"}
        out = run(M.g_autopilot, c, c.guard, True, sha=self.sha[:8], pkg_path=self.pkg_path)
        self.assertIn("exists, reused", out)
        self.assertFalse(any(x[1] == "input_boolean/create" for x in c.calls))
        run(M.g_autopilot_undo, c, c.guard, True, pkg_path=self.pkg_path)
        self.assertIn("input_boolean.away_mode", c.states)
        self.assertNotIn("input_text.autopilot_event", c.states)
        self.assertEqual([x for x in c.configs if x != "42"], [])

    def test_undo_restores_replaced_and_keeps_used_helper(self):
        c = FakeHA(M.Guard(writes=True))
        old = {"id": "autopilot_arrival", "alias": "Autopilot: arrival", "triggers": [], "actions": []}
        c.configs["autopilot_arrival"] = copy.deepcopy(old)
        c._reload("autopilot_arrival")
        run(M.g_autopilot, c, c.guard, True, sha=self.sha[:8], pkg_path=self.pkg_path)
        # a hand-made automation starts using the mailbox helper
        c.configs["77"] = {
            "id": "77",
            "alias": "Mine",
            "triggers": [{"trigger": "state", "entity_id": "input_text.autopilot_event"}],
        }
        c._reload("77")
        dry = run(M.g_autopilot_undo, c, c.guard, False, pkg_path=self.pkg_path)
        self.assertIn("still used by", dry)
        out = run(M.g_autopilot_undo, c, c.guard, True, pkg_path=self.pkg_path)
        self.assertIn("verify: OK", out)
        self.assertEqual(c.configs["autopilot_arrival"], old)
        self.assertNotIn("autopilot_left_on", c.configs)
        self.assertNotIn("input_boolean.away_mode", c.states)
        self.assertIn("input_text.autopilot_event", c.states)
        self.assertFalse(any(x[1] == "DELETE" and not x[2].rsplit("/", 1)[1].startswith("autopilot_") for x in c.calls))


class DisplayAliasAndDatetime(Base):
    """display_aliases: existing automations may carry a free name (entity id kept); input_datetime helpers."""

    def pkg(self):
        pkg = copy.deepcopy(PKG)
        pkg["helpers"]["vac_last_mop"] = {
            "type": "input_datetime",
            "create": {"name": "Vac last mop", "has_date": True, "has_time": True},
        }
        pkg["parts"]["left-on"]["helpers"] = ["autopilot_event", "vac_last_mop"]
        a = pkg["automations"]["autopilot_left_on"]
        a["actions"].append(
            {
                "action": "input_datetime.set_datetime",
                "target": {"entity_id": "input_datetime.vac_last_mop"},
                "data": {"datetime": "{{ now().strftime('%Y-%m-%d %H:%M:%S') }}"},
            }
        )
        return pkg

    def test_validation(self):
        pkg = self.pkg()
        pkg["automations"]["autopilot_left_on"]["alias"] = "Lights: left on"
        self.write_pkg(pkg)
        with self.assertRaises(SystemExit):
            M.ap_load(self.pkg_path)  # free name without display_aliases
        pkg["display_aliases"] = ["autopilot_nope"]
        self.write_pkg(pkg)
        with self.assertRaises(SystemExit):
            M.ap_load(self.pkg_path)  # unknown id
        pkg["display_aliases"] = ["autopilot_left_on"]
        self.write_pkg(pkg)
        M.ap_load(self.pkg_path)
        pkg["helpers"]["vac_last_mop"]["create"]["initial"] = "2026-01-01 00:00:00"
        self.write_pkg(pkg)
        with self.assertRaises(SystemExit):
            M.ap_load(self.pkg_path)  # initial would override the restored value

    def test_rename_existing_keeps_entity_and_creates_datetime(self):
        c = FakeHA(M.Guard(writes=True))
        run(M.g_autopilot, c, c.guard, True, sha=self.sha[:8], pkg_path=self.pkg_path)
        self.assertIn("automation.autopilot_left_on", c.states)
        pkg = self.pkg()
        pkg["automations"]["autopilot_left_on"]["alias"] = "Lights: left on"
        pkg["display_aliases"] = ["autopilot_left_on"]
        self.write_pkg(pkg)
        out = run(M.g_autopilot, c, c.guard, True, sha=self.sha[:8], pkg_path=self.pkg_path)
        self.assertIn("verify: OK", out)
        self.assertIn("helper input_datetime.vac_last_mop: create", out)
        self.assertEqual(c.configs["autopilot_left_on"]["alias"], "Lights: left on")
        self.assertIn("automation.autopilot_left_on", c.states)
        self.assertNotIn("automation.lights_left_on", c.states)
        self.assertIn("input_datetime.vac_last_mop", c.states)
        rec = self.load_undo()["autopilot"]["groups"]["autopilot"]
        self.assertIn("input_datetime.vac_last_mop", rec["created_helpers"])
        out = run(M.g_autopilot_undo, c, c.guard, True, pkg_path=self.pkg_path)
        self.assertIn("verify: OK", out)
        self.assertNotIn("input_datetime.vac_last_mop", c.states)
        self.assertNotIn("autopilot_left_on", c.configs)

    def test_display_alias_never_creates(self):
        pkg = self.pkg()
        pkg["automations"]["autopilot_left_on"]["alias"] = "Lights: left on"
        pkg["display_aliases"] = ["autopilot_left_on"]
        self.write_pkg(pkg)
        c = FakeHA(M.Guard(writes=True))
        with self.assertRaises(SystemExit):
            run(M.g_autopilot, c, c.guard, True, sha=self.sha[:8], pkg_path=self.pkg_path)
        self.assertEqual(self.writes(c), [])

    def test_guard_datetime_exact(self):
        g = M.Guard(writes=True)
        msg = {"name": "Vac last mop", "has_date": True, "has_time": True}
        with self.assertRaises(M.NotAllowed):
            g.check_ws("input_datetime/create", msg)
        g.approve(M.target_key("input_datetime/create", msg))
        g.check_ws("input_datetime/create", msg)
        with self.assertRaises(M.NotAllowed):
            g.check_ws("input_datetime/create", dict(msg, initial="2026-01-01 00:00:00"))
        with self.assertRaises(M.NotAllowed):
            g.check_ws("input_datetime/delete", {"input_datetime_id": "vac_last_mop"})


def snap_pkg(scene_id="autopilot_snap_before", turn_on="scene.autopilot_snap_before"):
    return {
        "version": 1,
        "parts": {"snap": {"helpers": [], "automations": ["autopilot_snap"]}},
        "helpers": {},
        "automations": {
            "autopilot_snap": {
                "alias": "Autopilot: snap",
                "variables": {"snapshot": turn_on},
                "triggers": [{"trigger": "state", "entity_id": "light.room_a", "to": "on"}],
                "actions": [
                    {"action": "scene.create", "data": {"scene_id": scene_id, "snapshot_entities": ["light.room_a"]}},
                    {"action": "scene.turn_on", "target": {"entity_id": turn_on}},
                ],
            }
        },
    }


class RuntimeScene(Base):
    def test_snapshot_refs_are_own_and_apply_works(self):
        self.write_pkg(snap_pkg())
        c = FakeHA(M.Guard(writes=True))
        out = run(M.g_autopilot, c, c.guard, False, pkg_path=self.pkg_path)
        self.assertIn("runtime scene scene.autopilot_snap_before", out)
        self.assertNotIn("MISSING", out)
        self.assertEqual(self.writes(c), [])
        out = run(M.g_autopilot, c, c.guard, True, sha=self.sha[:8], pkg_path=self.pkg_path)
        self.assertIn("verify: OK", out)
        self.assertNotIn("scene.autopilot_snap_before", c.states)

    def test_scene_id_must_be_literal_autopilot(self):
        for sid in ("movie", "{{ x }}", None, "autopilot_Bad"):
            self.write_pkg(snap_pkg(scene_id=sid))
            with self.assertRaises(SystemExit, msg=str(sid)):
                M.ap_load(self.pkg_path)

    def test_saved_scene_collision_refused(self):
        self.write_pkg(snap_pkg())
        c = FakeHA(M.Guard(writes=True))
        c.reg["scene.autopilot_snap_before"] = {"entity_id": "scene.autopilot_snap_before", "platform": "homeassistant"}
        with self.assertRaises(SystemExit):
            run(M.g_autopilot, c, c.guard, False, pkg_path=self.pkg_path)
        self.assertEqual(self.writes(c), [])

    def test_other_scene_still_checked(self):
        self.write_pkg(snap_pkg(turn_on="scene.autopilot_other"))
        c = FakeHA(M.Guard(writes=True))
        out = run(M.g_autopilot, c, c.guard, False, pkg_path=self.pkg_path)
        self.assertIn("MISSING ['scene.autopilot_other']", out)
        with self.assertRaises(SystemExit):
            run(M.g_autopilot, c, c.guard, True, sha=self.sha[:8], pkg_path=self.pkg_path)
        self.assertEqual(self.writes(c), [])


class RemoteSwap(Base):
    def test_dry_run_lists_candidates(self):
        c = FakeHA(M.Guard(writes=False))
        out = run(M.g_remote_swap, c, c.guard, False)
        self.assertIn(f"dev#{M.h(OLD_A, 8)} (removed)", out)
        self.assertIn("Hallway Door Dimmer", out)
        self.assertNotIn("Hue Dimmer", out.split("candidates")[1])
        self.assertIn("repairs open: 1", out)
        self.assertEqual(self.writes(c), [])

    def test_swap_by_hash_and_name(self):
        c = FakeHA(M.Guard(writes=True))
        pairs = [f"{M.h(OLD_A, 8)}=Hallway Door Dimmer", f"dev#{M.h(OLD_B, 10)}={NEW_B}"]
        dry = run(M.g_remote_swap, c, M.Guard(writes=False), False, pairs=pairs)
        self.assertIn("then: POST the config (4 refs)", dry)
        self.assertEqual(self.writes(c), [])
        out = run(M.g_remote_swap, c, c.guard, True, pairs=pairs)
        self.assertIn("verify: OK", out)
        self.assertIn("repairs open: 0", out)
        s = json.dumps(c.configs["42"])
        self.assertEqual((s.count(OLD_A), s.count(OLD_B), s.count(NEW_A), s.count(NEW_B)), (0, 0, 2, 2))
        self.assertEqual(c.states[REMOTE], "on")
        rec = self.load_undo()["remote-swap"]["groups"][REMOTE]
        self.assertIn(OLD_A, json.dumps(rec["before"]["config"]))
        undo = run(M.g_remote_swap, c, c.guard, True, undo=True)
        self.assertIn("verify: OK", undo)
        self.assertEqual(c.configs["42"], remote_cfg())

    def test_refusals(self):
        bad = (
            [f"{M.h(OLD_A, 8)}=Hue Dimmer"],  # other model/integration
            [f"{M.h(OLD_A, 8)}=Hallway Door Dimmer", f"{M.h(OLD_B, 8)}=Hallway Door Dimmer"],  # same NEW twice
            ["abc=Hallway Door Dimmer"],  # OLD not a hash
            [f"{M.h(NEW_A, 8)}=Hallway Stairs Dimmer"],  # OLD not referenced
            [f"{M.h(OLD_A, 8)}=Nope"],
        )  # NEW unknown
        for pairs in bad:
            c = FakeHA(M.Guard(writes=True))
            with self.assertRaises(SystemExit, msg=str(pairs)):
                run(M.g_remote_swap, c, c.guard, True, pairs=pairs)
            self.assertEqual(self.writes(c), [], str(pairs))
        c = FakeHA(M.Guard(writes=True))
        c.devs[OLD_A] = {"id": OLD_A, "name": "still here", "model": "RODRET Dimmer", "config_entries": ["z1"]}
        with self.assertRaises(SystemExit):
            run(M.g_remote_swap, c, c.guard, True, pairs=[f"{M.h(OLD_A, 8)}=Hallway Door Dimmer"])
        with self.assertRaises(SystemExit):
            run(M.g_remote_swap, c, c.guard, False, automation="automation.living_room_remote")


if __name__ == "__main__":
    unittest.main()
