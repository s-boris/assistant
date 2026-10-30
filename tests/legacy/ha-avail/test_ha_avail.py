"""Tests for ~/bin/ha-avail. Run: tests/legacy/ha-avail/run.sh in ~/opt/assistant
(HA_AVAIL_BIN overrides the script under test)."""

import contextlib
import datetime as dt
import importlib.machinery
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ha-audit"))
import fake_ha  # noqa: E402

BIN = os.environ.get("HA_AVAIL_BIN") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "scripts", "ha-avail"
)


def load():
    loader = importlib.machinery.SourceFileLoader("ha_avail_under_test", BIN)
    spec = importlib.util.spec_from_loader("ha_avail_under_test", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.dont_write_bytecode = True
    loader.exec_module(mod)
    return mod


V = load()
NOW = dt.datetime(2026, 10, 15, 21, 0, tzinfo=dt.UTC)


def t(h):  # hours before NOW
    return NOW - dt.timedelta(hours=h)


class Analysis(unittest.TestCase):
    def test_episodes_and_totals(self):
        hist = {
            "light.a": [("ok", t(10)), ("unavailable", t(8)), ("ok", t(7.5)), ("unavailable", t(1))],
            "light.b": [("ok", t(10))],
        }
        R = V.analyze(hist, NOW)
        a = R["entities"]["light.a"]
        self.assertEqual(len(a["episodes"]), 2)
        self.assertAlmostEqual(a["down_sec"], 1800 + 3600)
        self.assertEqual(a["longest_sec"], 3600)
        self.assertTrue(a["now_bad"])
        self.assertIsNone(a["episodes"][-1]["end"])
        self.assertEqual(R["entities"]["light.b"]["episodes"], [])
        self.assertEqual(R["n_episodes"], 2)
        self.assertEqual(sum(R["hours"]), 2)

    def test_min_sec_keeps_open_episode(self):
        hist = {
            "light.a": [
                ("ok", t(5)),
                ("unavailable", t(4)),
                ("ok", t(4) + dt.timedelta(seconds=20)),
                ("unavailable", NOW - dt.timedelta(seconds=5)),
            ]
        }
        R = V.analyze(hist, NOW, min_sec=60)
        eps = R["entities"]["light.a"]["episodes"]
        self.assertEqual(len(eps), 1)  # 20 s blip dropped, open 5 s episode kept
        self.assertIsNone(eps[0]["end"])

    def test_unknown_is_opt_in(self):
        hist = {"button.x": [("unknown", t(10))]}
        self.assertEqual(V.analyze(hist, NOW)["n_episodes"], 0)
        self.assertEqual(V.analyze(hist, NOW, bad_states=V.BAD)["n_episodes"], 1)

    def test_shared_drops(self):
        hist = {
            "light.a": [("ok", t(10)), ("unavailable", t(3)), ("ok", t(2))],
            "light.b": [("ok", t(10)), ("unavailable", t(3) + dt.timedelta(seconds=40)), ("ok", t(2))],
            "light.c": [("ok", t(10)), ("unavailable", t(6)), ("ok", t(5))],
        }
        R = V.analyze(hist, NOW)
        self.assertEqual(len(R["shared"]), 1)
        self.assertEqual(R["shared"][0]["entities"], ["light.a", "light.b"])
        txt = V.render(R)
        self.assertIn("1 drop(s) shared", txt)
        self.assertIn("light.c: since", txt)

    def test_fmt_dur(self):
        self.assertEqual(V.fmt_dur(42), "42 s")
        self.assertEqual(V.fmt_dur(125), "2 min 05 s")
        self.assertEqual(V.fmt_dur(3 * 3600 + 120), "3 h 02 min")


class AgainstFakeHA(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.tok = os.path.join(cls.tmp.name, "token")
        with open(cls.tok, "w") as fh:
            fh.write(fake_ha.TOKEN + "\n")
        cls._ents, cls._hist = list(fake_ha.ENTITIES), dict(fake_ha.HISTORY)
        fake_ha.ENTITIES += [
            fake_ha.reg("light.bulb_1", "hue_ble", "E_HUE1"),
            fake_ha.reg("light.bulb_2", "hue_ble", "E_HUE2"),
            fake_ha.reg("light.bulb_off", "hue_ble", "E_HUE3", disabled="user"),
        ]
        fake_ha.HISTORY.update(
            {
                "light.bulb_1": [("on", 5), ("unavailable", 2), ("off", 1.9), ("unavailable", 0.5)],
                "light.bulb_2": [("off", 5), ("unavailable", 2), ("on", 1.95)],
                "light.bulb_off": [("unavailable", 5)],
            }
        )

    @classmethod
    def tearDownClass(cls):
        fake_ha.ENTITIES[:] = cls._ents
        fake_ha.HISTORY.clear()
        fake_ha.HISTORY.update(cls._hist)
        cls.tmp.cleanup()

    def run_main(self, *args):
        with fake_ha.FakeHA() as srv:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = V.main(["--url", srv.url, "--token-file", self.tok, "--now", fake_ha.NOW.isoformat(), *args])
            return rc, buf.getvalue(), list(srv.log)

    def test_platform_run_reads_only(self):
        rc, out, log = self.run_main("--platform", "hue_ble", "--days", "7")
        self.assertEqual(rc, 0)
        self.assertIn("2 entities, 3 unavailable episode(s)", out)
        self.assertIn("light.bulb_1:", out)
        self.assertIn("DOWN NOW", out)
        self.assertNotIn("bulb_off", out)  # disabled entity skipped
        self.assertIn("shared by several entities", out)
        for kind, what, _ in log:
            if kind == "WS":
                self.assertIn(what, ("auth", "config/entity_registry/list"))
            elif kind == "REST":
                self.assertEqual(what, "GET")
        rest_paths = [p for k, m, p in log if k == "REST" and p != "/api/websocket"]
        self.assertTrue(rest_paths and all(p.startswith("/api/history/period/") for p in rest_paths))
        self.assertFalse([1 for k, m, p in log if k == "REST" and m != "GET"])  # query keys: ha-audit's check_rest

    def test_json_has_no_state_values_or_canaries(self):
        rc, out, _ = self.run_main("light.bulb_1", "light.bulb_2", "--json")
        data = json.loads(out)
        self.assertEqual(data["n_episodes"], 3)
        for c in fake_ha.CANARIES + ['"on"', '"off"']:
            self.assertNotIn(c, out)

    def test_bad_entity_id_refused_before_history(self):
        with fake_ha.FakeHA() as srv:
            with self.assertRaises(SystemExit):
                V.main(["--url", srv.url, "--token-file", self.tok, "light.x,switch.y"])
            self.assertFalse([p for k, m, p in srv.log if p and p.startswith("/api/history")])


if __name__ == "__main__":
    unittest.main()
