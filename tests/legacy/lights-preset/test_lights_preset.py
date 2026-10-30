#!/usr/bin/env python3
"""Tests for ~/bin/lights-preset against fake_ha.py. LIGHTS_PRESET_BIN=/copy to test a copy."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.environ.get("LIGHTS_PRESET_BIN") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "scripts", "lights-preset"
)
FAKE = os.path.join(HERE, "fake_ha.py")

CONFIG = {
    "presets": {
        "warm": {
            "run": ["script", "warm_dim"],
            "when": "before",
            "fallback": [["set", "light.lamp_a", "--brightness", "30"]],
        },
        "dim": {"run": ["scene", "scene.soft"]},
        "movie": {
            "run": ["scene", "scene.film"],
            "when": "after",
            "fallback": [
                ["set", "light.lamp_a", "--brightness", "30"],
                ["call", "light.turn_on", "light.strip_b", '{"brightness_pct": 30}'],
            ],
        },
        "film2": {
            "run": ["scene", "scene.film2"],
            "when": "after",
            "fallback": [["set", "light.lamp_a", "--brightness", "30"]],
        },
    }
}

SCENES = {
    "scene.film": {
        "members": ["light.lamp_a", "light.strip_b", "light.strip_tv"],
        "sets": {"light.lamp_a": "off", "light.strip_b": "off"},
    },
    "scene.film2": {
        "members": ["light.lamp_a", "light.strip_b", "light.strip_tv"],
        "sets": {"light.lamp_a": "on", "light.strip_b": "off"},
    },
    "scene.soft": {"members": ["light.lamp_a"], "sets": {"light.lamp_a": "on"}},
}


class T(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="lp-")
        self.state, self.log, self.cfg = (os.path.join(self.d, n) for n in ("state.json", "calls.log", "cfg.json"))
        self.ha = os.path.join(self.d, "ha")
        with open(self.ha, "w") as f:
            f.write(f'#!/bin/sh\nexec {sys.executable} {FAKE} "$@"\n')
        os.chmod(self.ha, 0o755)
        self.write_cfg(CONFIG)
        open(self.log, "w").close()

    def write_cfg(self, cfg):
        with open(self.cfg, "w") as f:
            json.dump(cfg, f)

    def world(self, lights, **extra):
        with open(self.state, "w") as f:
            json.dump({"lights": lights, "scenes": SCENES, **extra}, f)

    def run_lp(self, *args, settle="2"):
        env = dict(
            os.environ,
            LIGHTS_HA_BIN=self.ha,
            LIGHTS_PRESETS=self.cfg,
            LIGHTS_PRESET_SETTLE=settle,
            FAKE_STATE=self.state,
            FAKE_LOG=self.log,
        )
        return subprocess.run([BIN, *args], capture_output=True, text=True, env=env, timeout=60)

    def calls(self, verbs=("scene", "script", "set", "call", "on")):
        with open(self.log) as f:
            rows = [json.loads(x) for x in f if x.strip()]
        return [r for r in rows if r[0] in verbs]

    def lights(self):
        with open(self.state) as f:
            return json.load(f)["lights"]

    # movie (scene + after-check)
    def test_movie_scene_turns_all_off_falls_back(self):
        self.world(
            {"light.lamp_a": "on", "light.strip_b": "on", "light.strip_tv": "unavailable", "light.kitchen": "off"},
            lag=2,
        )
        r = self.run_lp("movie")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("fallback", r.stdout)
        c = self.calls()
        self.assertEqual([x[0] for x in c], ["scene", "set", "call"])
        self.assertEqual(self.lights()["light.lamp_a"], "on")
        self.assertEqual(self.lights()["light.strip_b"], "on")

    def test_movie_dark_already_falls_back_at_once(self):
        self.world({"light.lamp_a": "off", "light.strip_b": "off", "light.strip_tv": "unavailable"})
        r = self.run_lp("lt:movie", settle="30")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([x[0] for x in self.calls()], ["scene", "set", "call"])

    def test_movie_scene_that_keeps_a_light_on_no_fallback(self):
        self.world({"light.lamp_a": "off", "light.strip_b": "on", "light.strip_tv": "unavailable"})
        r = self.run_lp("film2", settle="1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("fallback", r.stdout)
        self.assertEqual([x[0] for x in self.calls()], ["scene"])

    def test_light_outside_the_scene_does_not_count(self):
        self.world({"light.lamp_a": "on", "light.strip_b": "off", "light.strip_tv": "off", "light.kitchen": "on"})
        r = self.run_lp("movie")
        self.assertEqual([x[0] for x in self.calls()], ["scene", "set", "call"], r.stdout)

    def test_movie_dry_run_sends_nothing(self):
        self.world({"light.lamp_a": "on", "light.strip_b": "on", "light.strip_tv": "unavailable"})
        r = self.run_lp("movie", "-n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        c = self.calls()
        self.assertEqual([x[0] for x in c], ["scene", "set", "call"])
        self.assertTrue(all("-n" in x for x in c))
        self.assertIn("read dark afterwards", r.stdout)
        self.assertEqual(self.lights()["light.lamp_a"], "on")
        with open(self.state) as f:
            self.assertNotIn("pending", json.load(f))

    def test_scene_failure_no_fallback(self):
        self.world({"light.lamp_a": "off", "light.strip_b": "off"}, fail={"scene": 1})
        r = self.run_lp("movie")
        self.assertEqual(r.returncode, 1)
        self.assertEqual([x[0] for x in self.calls()], ["scene"])

    def test_scene_unconfirmed_still_checks(self):
        self.world({"light.lamp_a": "off", "light.strip_b": "off"}, fail={"scene": 3})
        r = self.run_lp("movie")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual([x[0] for x in self.calls()], ["scene", "set", "call"])

    def test_unreadable_lights_no_fallback(self):
        self.world({"light.lamp_a": "off"}, list_fail=True)
        r = self.run_lp("movie", settle="0.5")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("could not read", r.stdout)
        self.assertEqual([x[0] for x in self.calls()], ["scene"])

    def test_fallback_step_failure_exits_1(self):
        self.world({"light.lamp_a": "off", "light.strip_b": "off"}, fail={"call": 1})
        r = self.run_lp("movie")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertEqual([x[0] for x in self.calls()], ["scene", "set", "call"])

    # warm (script + before-check)
    def test_warm_with_nothing_on_lights_first(self):
        self.world({"light.lamp_a": "off", "light.strip_tv": "unavailable"})
        r = self.run_lp("warm")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([x[0] for x in self.calls()], ["set", "script"])

    def test_warm_with_a_light_on_runs_script_only(self):
        self.world({"light.lamp_a": "off", "light.bedroom": "on"})
        self.run_lp("warm")
        self.assertEqual([x[0] for x in self.calls()], ["script"])

    # plain presets, usage, config
    def test_plain_scene_no_check(self):
        self.world({"light.lamp_a": "off"})
        r = self.run_lp("dim")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.calls(), [["scene", "run", "scene.soft"]])
        self.assertEqual(self.calls(("list",)), [])

    def test_unknown_preset_and_usage(self):
        self.world({})
        self.assertEqual(self.run_lp("disco").returncode, 2)
        self.assertEqual(self.run_lp().returncode, 2)
        self.assertEqual(self.run_lp("movie", "dim").returncode, 2)
        self.assertEqual(self.calls(), [])

    def test_bad_fallback_verb_refused(self):
        self.world({})
        self.write_cfg(
            {
                "presets": {
                    "x": {
                        "run": ["scene", "scene.soft"],
                        "when": "after",
                        "fallback": [["scene", "run", "scene.other"]],
                    }
                }
            }
        )
        r = self.run_lp("x")
        self.assertEqual(r.returncode, 2)
        self.assertIn("fallback step", r.stderr)
        self.assertEqual(self.calls(), [])

    def test_missing_when_refused(self):
        self.write_cfg({"presets": {"x": {"run": ["scene", "s"], "fallback": [["on", "light.a"]]}}})
        self.assertEqual(self.run_lp("x").returncode, 2)

    def test_list(self):
        r = self.run_lp("--list")
        self.assertEqual(r.returncode, 0)
        self.assertIn("movie", r.stdout)
        self.assertIn("fallback after", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=1)
