"""Offline tests for ha-apply lint (weekly package drift check): applied packages stay silent while HA matches,
report edited/missing automations, scripts and helpers and gone entities/devices; staged packages run the dry
run (missing entities, refusals); undone packages count as staged; --cron writes one inbox note only on
findings; lint never writes to HA. Fake HA only."""

import copy
import json
import os
import sys
import unittest

sys.dont_write_bytecode = True
import test_autopilot as T  # noqa: E402
import test_package_scripts as S  # noqa: E402

M = T.M
STAGED = {
    "version": 1,
    "helpers": {"movie_switch": {"type": "input_boolean", "create": {"name": "Movie switch"}}},
    "parts": {"movie": {"helpers": ["movie_switch"], "automations": ["autopilot_movie"]}},
    "automations": {
        "autopilot_movie": {
            "alias": "Autopilot: movie",
            "triggers": [{"trigger": "state", "entity_id": "media_player.tv_a", "to": "on"}],
            "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.room_a"}}],
        }
    },
}


class Lint(S.Base):
    def setUp(self):
        super().setUp()
        self.dir = os.path.join(self.tmp.name, "pkgs")
        os.makedirs(self.dir)
        M.LINT_INBOX = os.path.join(self.tmp.name, "inbox")
        self.c = self.ha(True)
        both = copy.deepcopy(T.PKG)
        self.put("home.json", both)
        self.put("warm.json", S.PKG)
        for name in ("home.json", "warm.json"):
            path = os.path.join(self.dir, name)
            self.write_pkg(self.load(path))
            out = T.run(M.g_autopilot, self.c, self.c.guard, True, sha=self.sha[:8], pkg_path=path)
            self.assertIn("verify: OK", out)
        self.c.guard = M.Guard(writes=False)  # lint must get by with reads only
        self.n = len(self.c.calls)

    @staticmethod
    def load(path):
        with open(path) as f:
            return json.load(f)

    @staticmethod
    def load_text(path):
        with open(path) as f:
            return f.read()

    def put(self, name, pkg):
        with open(os.path.join(self.dir, name), "w") as f:
            json.dump(pkg, f)

    def lint(self, cron=False):
        rc = [None]
        out = T.run(lambda: rc.__setitem__(0, M.g_lint(self.c, cron=cron, pkg_dir=self.dir)))
        new = self.c.calls[self.n :]
        self.assertEqual(
            [x for x in new if (x[0] == "rest" and x[1] != "GET") or (x[0] == "ws" and x[1] not in M.WS_READ)], []
        )
        return rc[0], out

    def notes(self):
        return sorted(os.listdir(M.LINT_INBOX)) if os.path.isdir(M.LINT_INBOX) else []

    def test_all_fine_is_silent(self):
        rc, out = self.lint(cron=True)
        self.assertEqual((rc, out, self.notes()), (0, "", []))
        rc, out = self.lint()
        self.assertEqual(rc, 0)
        self.assertIn("home.json applied", out)
        self.assertIn("warm.json applied", out)

    def test_edited_automation_and_script(self):
        self.c.configs["autopilot_arrival"]["actions"] = []
        self.c.scripts["warm_test"]["alias"] = "Renamed by hand"
        rc, out = self.lint()
        self.assertEqual(rc, 1)
        self.assertIn("automation.autopilot_arrival: edited in HA (actions differ)", out)
        self.assertIn("script.warm_test: edited in HA (alias differ)", out)
        self.assertIn("Fix:", out)

    def test_missing_automation_script_helper(self):
        self.c.configs.pop("autopilot_left_on")
        self.c.scripts.pop("warm_test")
        for k in ("states", "reg"):
            getattr(self.c, k).pop("input_boolean.away_mode")
        rc, out = self.lint()
        self.assertEqual(rc, 1)
        for s in (
            "automation.autopilot_left_on: missing in HA",
            "script.warm_test: missing in HA",
            "input_boolean.away_mode: helper missing in HA",
        ):
            self.assertIn(s, out)

    def test_helper_edited(self):
        self.c.helpers["input_boolean"]["away_mode"]["icon"] = "mdi:home"
        rc, out = self.lint()
        self.assertIn("input_boolean.away_mode: helper edited in HA (icon differ)", out)

    def test_entity_renamed(self):
        for k in ("states", "reg"):
            getattr(self.c, k).pop("light.room_a")
        rc, out = self.lint()
        self.assertEqual(rc, 1)
        self.assertIn("home.json (applied): light.room_a: referenced, not in HA", out)

    def test_staged_dry_run(self):
        self.put("movie.json", STAGED)
        rc, out = self.lint()
        self.assertEqual(rc, 1)
        self.assertIn("movie.json (staged): media_player.tv_a: referenced, not in HA", out)
        self.c.states["media_player.tv_a"] = "off"
        rc, out = self.lint()
        self.assertEqual(rc, 0)
        self.assertIn("movie.json staged", out)
        self.assertNotIn("autopilot_movie", self.c.configs)

    def test_staged_refusal(self):
        self.put("movie.json", STAGED)
        self.c.states["media_player.tv_a"] = "off"
        self.c.reg["automation.autopilot_movie"] = {"entity_id": "automation.autopilot_movie", "platform": "x"}
        rc, out = self.lint()
        self.assertIn("movie.json (staged): dry run refused: refusing: automation.autopilot_movie already exists", out)

    def test_invalid_package(self):
        self.put("bad.json", {"parts": {}})
        rc, out = self.lint()
        self.assertIn("bad.json (invalid): does not load", out)

    def test_undone_counts_as_staged_but_reapply_wins(self):
        rec = self.load_undo()["autopilot"]["groups"]["autopilot"]
        pkg = self.load(os.path.join(self.dir, "home.json"))
        for a in pkg["automations"]:
            self.c.configs.pop(a)
        rec["undone"] = {"at": "9999", "actions": [["delete", a] for a in pkg["automations"]]}
        M.undo_save(M.AUTOPILOT, rec, section=M.AUTOPILOT)
        rc, out = self.lint()
        self.assertEqual(rc, 0, out)
        self.assertIn("home.json staged", out)
        rec["undone"]["at"] = "0000"  # an undo older than the last apply is stale
        M.undo_save(M.AUTOPILOT, rec, section=M.AUTOPILOT)
        rc, out = self.lint()
        self.assertIn("automation.autopilot_arrival: missing in HA", out)

    def test_gone_device(self):
        pkg = copy.deepcopy(STAGED)
        pkg["automations"]["autopilot_movie"]["triggers"] = [{"trigger": "device", "device_id": "f" * 32}]
        self.put("movie.json", pkg)
        self.c.states["media_player.tv_a"] = "off"
        rc, out = self.lint()
        self.assertIn(f"device {M.h('f' * 32, 8)}: referenced, not in HA", out)
        self.assertNotIn("f" * 32, out)

    def test_cron_writes_one_note(self):
        self.c.configs["autopilot_arrival"]["actions"] = []
        rc, out = self.lint(cron=True)
        self.assertEqual((rc, out), (0, ""))
        notes = self.notes()
        self.assertEqual(len(notes), 1)
        self.assertTrue(notes[0].startswith("ha-package-lint-"))
        text = self.load_text(os.path.join(M.LINT_INBOX, notes[0]))
        self.assertIn("# ha-package-lint", text)
        self.assertIn("autopilot_arrival", text)


if __name__ == "__main__":
    unittest.main()
