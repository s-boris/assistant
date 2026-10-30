"""Tests for ~/bin/ha-audit against a fake HA (no network beyond 127.0.0.1).

Run: tests/legacy/ha-audit/run.sh in ~/opt/assistant   (HA_AUDIT_BIN overrides the script under test)
"""

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fake_ha  # noqa: E402

BIN = os.environ.get("HA_AUDIT_BIN") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "scripts", "ha-audit"
)


def load():
    loader = importlib.machinery.SourceFileLoader("ha_audit_under_test", BIN)
    spec = importlib.util.spec_from_loader("ha_audit_under_test", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.dont_write_bytecode = True
    loader.exec_module(mod)
    return mod


H = load()
WRITE_WS_TYPES = [
    "call_service",
    "execute_script",
    "fire_event",
    "subscribe_events",
    "subscribe_trigger",
    "config/entity_registry/update",
    "config/entity_registry/remove",
    "config/device_registry/update",
    "config/device_registry/remove_config_entry",
    "config_entries/delete",
    "config_entries/disable",
    "config_entries/update",
    "config_entries/ignore_flow",
    "person/update",
    "repairs/ignore_issue",
    "automation/config",
    "backup/generate",
    "update/release_notes",
    "auth/long_lived_access_token",
    "config/auth/delete",
    "lovelace/config/save",
    "homeassistant/restart",
]


class GuardTests(unittest.TestCase):
    """The allowlist refuses writes before anything is sent."""

    def test_rest_non_get_methods_refused(self):
        for m in ("POST", "DELETE", "PUT", "PATCH", "HEAD", "get"):
            with self.assertRaises(H.ReadOnlyViolation, msg=m):
                H.check_rest(m, "/api/states")

    def test_rest_paths_outside_allowlist_refused(self):
        for p in (
            "/api/services/light/turn_on",
            "/api/error_log",
            "/api/states/person.alex",
            "/api/events",
            "/api/template",
            "/api/config/core/check_config",
            "/api/hassio/addons/x/uninstall",
            "/api/hassio/host/reboot",
            "/api/config/config_entries/entry/abc/reload",
            "/api/states/../services",
            "/api/history/period/2026-10-01T00:00:00+00:00/../../services",
            "/api/camera_proxy/camera.x",
        ):
            with self.assertRaises(H.ReadOnlyViolation, msg=p):
                H.check_rest("GET", p)
        with self.assertRaises(H.ReadOnlyViolation):
            H.check_rest("GET", "/api/history/period/2026-10-01T00:00:00+00:00", {"filter_entity_id": "x", "evil": "1"})

    def test_ws_write_types_refused(self):
        for t in WRITE_WS_TYPES:
            with self.assertRaises(H.ReadOnlyViolation, msg=t):
                H.check_ws(t, {})

    def test_ws_supervisor_only_get_on_allowlist(self):
        for params in (
            {"endpoint": "/addons", "method": "post"},
            {"endpoint": "/addons", "method": "GET"},
            {"endpoint": "/host/reboot", "method": "get"},
            {"endpoint": "/addons/x/uninstall", "method": "get"},
            {"endpoint": "/backups", "method": "get"},
            {"endpoint": "/addons", "method": "get", "data": {}},
            {"endpoint": "/core/update", "method": "get"},
        ):
            with self.assertRaises(H.ReadOnlyViolation, msg=str(params)):
                H.check_ws("supervisor/api", params)
        H.check_ws("supervisor/api", {"endpoint": "/addons", "method": "get"})

    def test_ws_extra_keys_refused(self):
        with self.assertRaises(H.ReadOnlyViolation):
            H.check_ws("config/entity_registry/list", {"entity_id": "x", "disabled_by": "user"})
        with self.assertRaises(H.ReadOnlyViolation):
            H.check_ws("trace/list", {"domain": "light"})

    def test_clients_never_send_refused_calls(self):
        with fake_ha.FakeHA() as srv:
            rest = H.RestClient(srv.url, fake_ha.TOKEN)
            ws = H.WSClient(srv.url, fake_ha.TOKEN)
            n_before = len(srv.log)
            for m, p in (
                ("POST", "/api/services/light/turn_on"),
                ("DELETE", "/api/config/config_entries/entry/E_HUE"),
                ("POST", "/api/states"),
                ("PUT", "/api/states"),
            ):
                with self.assertRaises(H.ReadOnlyViolation):
                    rest.request(m, p)
            for t in WRITE_WS_TYPES:
                with self.assertRaises(H.ReadOnlyViolation):
                    ws.call(t, domain="light", service="turn_on")
            with self.assertRaises(H.ReadOnlyViolation):
                ws.call("supervisor/api", endpoint="/addons/core_mosquitto/stop", method="post")
            # a legit read still works on the same connection afterwards
            self.assertEqual(len(ws.call("config/area_registry/list")), 2)
            ws.close()
            new = srv.log[n_before:]
            self.assertEqual([x[1] for x in new if x[0] == "WS"], ["config/area_registry/list"])
            self.assertFalse([x for x in new if x[0] == "REST"])

    def test_redirect_refused(self):
        class R(H._NoRedirect):
            pass

        with self.assertRaises(H.ReadOnlyViolation):
            R().redirect_request(None, None, 302, "Found", {}, "http://evil/")


class PrivacyTests(unittest.TestCase):
    def test_scrub(self):
        s = H.scrub(
            "Hub 198.51.100.40 mac aa:bb:cc:dd:ee:ff at 12.34567,65.43219 alex@example.com "
            "Musterstrasse 12 ble_aa_bb_cc_dd_ee_01_rssi fe80::1:2:3:4 aabbccddeeff",
            names=True,
        )
        for c in (
            "198.51.100",
            "aa:bb",
            "12.34567",
            "65.43219",
            "alex@",
            "Musterstrasse",
            "aa_bb_cc",
            "fe80",
            "aabbccddeeff",
        ):
            self.assertNotIn(c, s)
        self.assertEqual(H.scrub("2026.10.1 v17.0 Pixel 10 Pro"), "2026.10.1 v17.0 Pixel 10 Pro")

    def test_slim_state_drops_attributes(self):
        s = H.slim_state(fake_ha.STATES[0])
        self.assertNotIn("latitude", json.dumps(s))
        self.assertNotIn("Zone-Canary", json.dumps(s))
        self.assertEqual(s["device_trackers"][0], "device_tracker.galaxy_s24_ultra")
        g = H.slim_state([x for x in fake_ha.STATES if "geocoded" in x["entity_id"]][0])
        self.assertNotIn("Musterstrasse", json.dumps(g))
        self.assertEqual(g["cls"], "available")

    def test_bt_device_name_masked(self):
        d = H.slim_device(fake_ha.DEVICES[7], {"E_BLE": "private_ble_device"}, [0])
        self.assertNotIn("AirPods", json.dumps(d))
        self.assertTrue(d["is_bt"])


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        tok = os.path.join(cls.tmp, "tok")
        with open(tok, "w") as fh:
            fh.write(fake_ha.TOKEN + "\n")
        cls.md, cls.js = os.path.join(cls.tmp, "r.md"), os.path.join(cls.tmp, "r.json")
        with fake_ha.FakeHA() as srv:
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = H.main(
                    [
                        "--url",
                        srv.url,
                        "--token-file",
                        tok,
                        "--md",
                        cls.md,
                        "--json",
                        cls.js,
                        "--now",
                        fake_ha.NOW.isoformat(),
                        "--date",
                        "2026-10-15",
                    ]
                )
            cls.rc, cls.out, cls.err, cls.log = rc, out.getvalue(), err.getvalue(), list(srv.log)
            cls.hq = list(srv.srv.history_queries)
        with open(cls.md) as fh:
            cls.mdtext = fh.read()
        with open(cls.js) as fh:
            cls.jstext = fh.read()

    def test_rc_and_summary(self):
        self.assertEqual(self.rc, 0)
        lines = self.out.strip().splitlines()
        self.assertEqual(len(lines), 8, self.out)
        self.assertTrue(all(len(x) < 200 for x in lines))

    def test_only_reads_on_the_wire(self):
        self.assertTrue(self.log)
        self.assertEqual({x[1] for x in self.log if x[0] == "REST"}, {"GET"})
        ws_types = {x[1] for x in self.log if x[0] == "WS"}
        self.assertTrue(ws_types <= set(H.WS_ALLOW) | {"auth"}, ws_types)
        for x in self.log:
            if x[0] == "WS" and x[1] == "supervisor/api":
                self.assertEqual(x[2]["method"], "get")
        self.assertTrue(self.hq, "history was queried")

    def test_no_privacy_canaries(self):
        for blob, name in ((self.mdtext, "md"), (self.jstext, "json"), (self.out + self.err, "stdout")):
            for c in fake_ha.CANARIES:
                self.assertNotIn(c, blob, f"{c!r} leaked into {name}")
            for k in ("latitude", "longitude", "gps_accuracy", "altitude", "media_title", "ssid"):
                self.assertNotIn(f'"{k}"', blob, f"key {k} in {name}")
        self.assertNotIn("privacy gate redacted", self.err)
        for blob in (self.mdtext, self.jstext):
            self.assertNotIn("sensor.ble_", blob)
            self.assertNotIn("alex_example_com", blob)
        self.assertIn("[bt:private_ble_device:", self.jstext)

    def test_no_age_for_trackers(self):
        for line in self.mdtext.splitlines():
            if "device_tracker." in line or "geocoded" in line or "wifi" in line:
                for b in ("<1 d", "1–7 d", "7–30 d", "30–90 d", ">90 d"):
                    self.assertNotIn(b, line, line)
        j = json.loads(self.jstext)
        for b in j["bad"]:
            if b["domain"] in ("device_tracker", "person") or b["sensitive"]:
                self.assertIsNone(b["days"])

    def test_findings(self):
        md = self.mdtext
        j = json.loads(self.jstext)
        long_ids = {b["entity_id"] for b in j["bad_long"]}
        self.assertTrue(
            {"light.wohnzimmer", "sensor.tuya_temp", "binary_sensor.door", "switch.old_plug", "sensor.netatmo_temp"}
            <= long_ids,
            long_ids,
        )
        self.assertNotIn("button.restart", long_ids)
        orph = {o["entity_id"]: o["why"] for o in j["orphans"]}
        self.assertIn("switch.old_plug", orph)
        self.assertIn("light.wohnzimmer", orph)
        self.assertIn("sensor.orphan_device", orph)
        self.assertIn("sensor.legacy_yaml", orph)
        self.assertEqual([d["entity_id"] for d in j["duplicates"]], ["light.wohnzimmer_2"])
        self.assertEqual(j["same_name_devices"], {"hue": 1})
        self.assertNotIn("sensor.tuya_humidity", orph)
        self.assertEqual(j["restored_failing"], {"tuya": 1})
        self.assertIn("| your call | Backup first |", md)
        self.assertIn("NO current backup", self.out)
        self.assertRegex(md, r"\| Galaxy S20 FE \| SM-G780G \| ≥ 30 d \|")
        self.assertTrue(j["duplicates"][0]["stale_base"])
        self.assertIn("sensor.shelly_power", [x["entity_id"] for x in j["disabled_lingering"]])
        phones = {p["name"]: p for p in j["phones"]}
        self.assertEqual(phones["Galaxy S20 FE"]["linked_persons"], ["person.alex"])
        self.assertIn("automation.morning_lights", phones["Galaxy S20 FE"]["referenced_by"])
        self.assertIn("script.goodnight", phones["Galaxy S24 Ultra"]["referenced_by"])
        self.assertGreaterEqual(phones["Galaxy S20 FE"]["last_days"], 29)
        self.assertLess(phones["Pixel 10 Pro"]["last_days"], 1)
        self.assertEqual({p["name"] for p in H.old_phones(j)}, {"Galaxy S20 FE", "Galaxy S24 Ultra"})
        self.assertFalse([x for x in j["duplicates"] if x["kind"].startswith("separate")])
        self.assertEqual({x["name"] for x in j["dev_empty"]}, {"Tuya Plug", "Old Thing"})
        self.assertIn("Netatmo Station", [x["name"] for x in j["dev_dead"]])
        self.assertIn("Old Thing", [x["name"] for x in j["dev_orphan"]])
        self.assertEqual([e["domain"] for e in j["int_failing"]], ["tuya"])
        self.assertEqual([e["domain"] for e in j["int_ignored"]], ["elgato"])
        self.assertEqual([e["domain"] for e in j["int_unused"] if not e["exempt"]], ["workday"])
        self.assertIn("netatmo", [e["domain"] for e in j["int_dead"]])
        autos = {a["entity_id"]: a for a in j["automation"]}
        self.assertEqual(
            autos["automation.never_ran"]["refs"]["missing"],
            ["(registry id of a deleted entity)", "sensor.does_not_exist"],
        )
        self.assertIn("notify.mobile_app_old_tablet", autos["automation.never_ran"]["refs"]["missing_services"])
        self.assertIn("(deleted device)", autos["automation.never_ran"]["refs"]["missing_devices"])
        self.assertEqual(autos["automation.bp_motion"]["refs"]["missing"], ["binary_sensor.gone_motion"])
        self.assertEqual(autos["automation.broken"]["cls"], "unavailable")
        self.assertEqual(autos["automation.erroring"]["trace_errors"][0]["execution"], "error")
        self.assertTrue(autos["automation.yaml_only"]["cfg_error"])
        self.assertIn(
            "sensor.shelly_power", {a["entity_id"]: a for a in j["script"]}["script.goodnight"]["refs"]["disabled"]
        )
        self.assertIn("core_mosquitto", [a["slug"] for a in j["addons_update"]])
        self.assertEqual(
            {a["slug"]: a["boot"] for a in j["addons_stopped"]}, {"core_configurator": "manual", "local_old": "auto"}
        )
        self.assertEqual([i["translation_key"] for i in j["issues_active"]], ["deprecated_yaml"])
        self.assertIn("update.home_assistant_core_update", [u["entity_id"] for u in j["updates"]])
        for sec in (
            "## 1. Entities",
            "## 2. Devices",
            "## 3. Integrations",
            "## 4a. Automations",
            "## 4b. Scripts",
            "## 5. Repairs",
            "## 6. Plan",
        ):
            self.assertIn(sec, md)
        self.assertTrue(
            any(
                x["is_bt"]
                for x in H.slim_device(fake_ha.DEVICES[7], {"E_BLE": "private_ble_device"}, [0])
                and [H.slim_device(fake_ha.DEVICES[7], {"E_BLE": "private_ble_device"}, [0])]
            )
        )


if __name__ == "__main__":
    unittest.main(verbosity=1)
