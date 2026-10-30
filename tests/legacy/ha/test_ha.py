"""Tests for ~/bin/ha against a fake HA on 127.0.0.1 (no other network).

Run: ~/opt/ha-tests/run.sh      HA_BIN=/path/to/ha tests another copy (default: ./stage/ha if present,
                                 else ~/bin/ha); its lib/ha_privacy.py is the one next to it.
"""

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import stat
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fake_ha  # noqa: E402

BIN = os.environ.get("HA_BIN") or (
    os.path.join(HERE, "stage", "ha")
    if os.path.exists(os.path.join(HERE, "stage", "ha"))
    else os.path.expanduser("~/bin/ha")
)
sys.dont_write_bytecode = True


def load(path, name):
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


H = load(BIN, "ha_under_test")
P = H.P


class Base(unittest.TestCase):
    effects = True

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tok = os.path.join(self.tmp.name, "tok")
        with open(tok, "w") as fh:
            fh.write(fake_ha.TOKEN + "\n")
        self.fake = fake_ha.FakeHA(effects=self.effects).__enter__()
        self.state_dir = os.path.join(self.tmp.name, "state")
        self.env = {"HA_URL": self.fake.url, "HA_TOKEN_FILE": tok, "HA_STATE_DIR": self.state_dir}
        self._old = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)

    def tearDown(self):
        self.fake.__exit__()
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def ha(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = H.main(list(argv))
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def logged(self):
        p = os.path.join(self.state_dir, "actions.log")
        if not os.path.exists(p):
            return []
        with open(p) as fh:
            return [json.loads(x) for x in fh if x.strip()]

    def assertNoCanary(self, text):
        for c in fake_ha.CANARIES:
            self.assertNotIn(c, text, f"canary {c!r} leaked")

    def assertNothingSent(self):
        self.assertEqual(self.fake.posts(), [])
        self.assertEqual(self.fake.calls, [])


# ----------------------------------------------------------------------------- privacy
class PrivacyTests(Base):
    def test_no_canary_in_any_read(self):
        chunks = []
        for argv in (
            ["list"],
            ["list", "--json"],
            ["areas"],
            ["find", "galaxy"],
            ["find", "alex"],
            ["find", "musterstrasse"],
            ["state", "galaxy"],
            ["state", "tv", "--json"],
            ["list", "--area", "Living Room"],
        ):
            rc, o, e = self.ha(*argv)
            chunks += [o, e]
        for eid in self.fake.srv.states:
            for extra in ([], ["--json"]):
                rc, o, e = self.ha("state", eid, *extra)
                self.assertEqual(rc, 0, (eid, e))
                chunks += [o, e]
        self.assertNoCanary("\n".join(chunks))

    def test_geocoded_sensor_hidden(self):
        rc, o, e = self.ha("state", "sensor.phone_geocoded_location", "--json")
        g = json.loads(o)
        self.assertEqual(g["state"], "<hidden>")
        self.assertEqual(g["attributes"], {})
        self.assertNoCanary(o)

    def test_trackers_show_zone_or_home_only(self):
        want = {
            "device_tracker.phone": "home",
            "person.alex": "Work",
            "person.jamie": "not_home",
            "zone.home": "<hidden>",
        }
        for eid, st in want.items():
            g = json.loads(self.ha("state", eid, "--json")[1])
            self.assertEqual(g["state"], st, eid)
            self.assertEqual(g["attributes"], {}, eid)

    def test_wifi_ip_notification_sensors_hidden(self):
        for eid in (
            "sensor.phone_wifi_connection",
            "sensor.phone_wifi_bssid",
            "sensor.phone_last_notification",
            "sensor.phone_public_ip",
        ):
            g = json.loads(self.ha("state", eid, "--json")[1])
            self.assertEqual(g["state"], "<hidden>", eid)
        g = json.loads(self.ha("state", "sensor.phone_battery_level", "--json")[1])
        self.assertEqual(g["state"], "54")

    def test_nested_attributes(self):
        rc, o, e = self.ha("state", "sensor.car_status", "--json")
        self.assertNoCanary(o)
        a = json.loads(o)["attributes"]
        keys = []

        def walk(x):
            if isinstance(x, dict):
                for k, v in x.items():
                    keys.append(k)
                    walk(v)
            elif isinstance(x, list):
                for v in x:
                    walk(v)

        walk(a)
        for bad in ("lat", "lon", "address"):
            self.assertNotIn(bad, keys)
        d = a["details"]
        self.assertEqual(d["pos"], {})
        self.assertEqual(d["coords"], ["<coord>", "<coord>"])
        self.assertEqual(d["wifi_ssid"], "<ssid>")
        self.assertEqual(d["items"][1]["contact"], "<email>")
        self.assertEqual(d["items"][2]["router"], "<ip>")
        self.assertEqual(d["items"][2]["ap_mac"], "<mac>")
        self.assertEqual(d["items"][2]["v6"], "<ip>")
        self.assertIn("<address>", d["note"])
        self.assertEqual(a["x"], "<coord>")
        self.assertEqual(a["battery_level"], 0.877)
        self.assertEqual(a["last_seen"], "2026-10-15T20:59:59+00:00")

    def test_gate_value_unit(self):
        v = P.gate_value(
            {
                "a": [
                    {
                        "b": {
                            "Latitude": 1.0,
                            "GPS": [1, 2],
                            "street": "x",
                            "ok": "y",
                            "BSSID": "zz",
                            "Access_Token": "t",
                            "url": "http://h/x?apikey=SECRET1&a=1",
                        }
                    }
                ]
            }
        )
        self.assertEqual(v, {"a": [{"b": {"ok": "y", "BSSID": "<mac>", "url": "http://h/x?apikey=<token>&a=1"}}]})

    def test_media_player_token_and_picture(self):
        rc, o, e = self.ha("state", "media_player.lg_webos_tv_oled")
        self.assertNoCanary(o)
        self.assertNotIn("entity_picture", o)
        self.assertIn("source: HDMI 1", o)
        self.assertIn("volume_level: 0.12", o)
        rc, o, e = self.ha("list", "media_player")
        self.assertIn("vol 12%", o)

    def test_bluetooth_names_masked(self):
        rc, o, e = self.ha("state", "device_tracker.alex_airpods", "--json")
        g = json.loads(o)
        self.assertTrue(g["entity_id"].startswith("device_tracker.[bt:"))
        self.assertEqual(g["name"], "Bluetooth device")
        rc, o, e = self.ha("find", "airpods")
        self.assertNotIn("AirPods", o + e)

    def test_calendar_and_camera_content_hidden(self):
        for eid in ("calendar.alex_example_com", "camera.door"):
            rc, o, e = self.ha("state", eid, "--json")
            self.assertEqual(json.loads(o)["attributes"], {}, eid)
            self.assertNoCanary(o)

    def test_measured_value_rounded_not_coord(self):
        rc, o, e = self.ha("state", "sensor.living_room_temperature")
        self.assertIn("state: 21.53", o)

    def test_text_gate_masks_token(self):
        H._SECRETS[:] = ["SECRETTOKEN123456789012"]
        self.assertEqual(P.text_gate("x SECRETTOKEN123456789012 y", H._SECRETS), "x <token> y")

    def test_regexes_equal_ha_audit(self):
        audit = os.path.expanduser("~/bin/ha-audit")
        if not os.path.exists(audit):
            self.skipTest("no ha-audit")
        A = load(audit, "ha_audit_for_drift")
        for n in (
            "EMAIL_RE",
            "EMAIL_SLUG_RE",
            "MAC_RE",
            "MAC12_RE",
            "IP4_RE",
            "IP6_RE",
            "IP6C_RE",
            "COORD_RE",
            "STREET_RE",
            "PLZ_RE",
            "SENSITIVE_OBJ_RE",
        ):
            self.assertEqual(getattr(A, n).pattern, getattr(P, n).pattern, n)
            self.assertEqual(getattr(A, n).flags, getattr(P, n).flags, n)


# ----------------------------------------------------------------------------- resolution
class ResolutionTests(Base):
    def test_ambiguous_name_refused_ids_only(self):
        rc, o, e = self.ha("off", "lamp")
        self.assertEqual(rc, 2)
        self.assertIn("ambiguous", e)
        self.assertIn("light.kitchen", e)
        self.assertIn("light.living_room_lamp", e)
        self.assertNotIn("Kitchen Lamp", e)
        self.assertNotIn("Floor Lamp", e)
        self.assertNothingSent()

    def test_tv_ambiguous_for_off(self):
        rc, o, e = self.ha("off", "tv")
        self.assertEqual(rc, 2)
        self.assertIn("media_player.lg_webos_tv_oled", e)
        self.assertIn("switch.tv_plug", e)
        self.assertNothingSent()

    def test_exact_name_resolves(self):
        rc, o, e = self.ha("on", "Floor Lamp")
        self.assertEqual(rc, 0, e)
        self.assertEqual(self.fake.calls[0][:2], ("light", "turn_on"))
        self.assertEqual(self.fake.calls[0][2]["entity_id"], "light.living_room_lamp")

    def test_words_resolve_when_unique(self):
        rc, o, e = self.ha("off", "ceiling")
        self.assertEqual(rc, 0, e)
        self.assertEqual(self.fake.calls[0][2]["entity_id"], "light.living_room_ceiling")

    def test_ambiguous_exact_names_refused(self):
        self.fake.srv.states["light.kitchen"]["attributes"]["friendly_name"] = "Floor Lamp"
        rc, o, e = self.ha("on", "Floor Lamp")
        self.assertEqual(rc, 2)
        self.assertNothingSent()

    def test_unquoted_multiword_names(self):
        rc, o, e = self.ha("on", "Floor", "Lamp")
        self.assertEqual(rc, 0, e)
        self.assertEqual(self.fake.calls[-1][2]["entity_id"], "light.living_room_lamp")
        rc, o, e = self.ha("media", "LG", "webOS", "TV", "volume", "30")
        self.assertEqual(rc, 0, e)
        self.assertEqual(self.fake.calls[-1][2], {"volume_level": 0.3, "entity_id": "media_player.lg_webos_tv_oled"})
        rc, o, e = self.ha("set", "Floor", "Lamp", "--brightness", "40")
        self.assertEqual(rc, 0, e)
        rc, o, e = self.ha("scene", "run", "Movie", "night")
        self.assertEqual(rc, 0, e)
        rc, o, e = self.ha("state", "Ceiling", "Light")
        self.assertIn("state: on", o)
        rc, o, e = self.ha("off", "light.kitchen", "light.living_room_ceiling")
        self.assertEqual(rc, 0, e)
        self.assertEqual(self.fake.calls[-1][2]["entity_id"], ["light.kitchen", "light.living_room_ceiling"])
        rc, o, e = self.ha("media", "LG", "TV", "dance")
        self.assertEqual(rc, 2)

    def test_unknown_refused(self):
        rc, o, e = self.ha("off", "spaceship")
        self.assertEqual(rc, 2)
        rc, o, e = self.ha("off", "light.nope")
        self.assertEqual(rc, 2)
        self.assertNothingSent()

    def test_state_by_name_lists_matches(self):
        rc, o, e = self.ha("state", "tv")
        self.assertEqual(rc, 0)
        self.assertIn("media_player.lg_webos_tv_oled", o)
        self.assertIn("switch.tv_plug", o)

    def test_find_and_area_list(self):
        rc, o, e = self.ha("find", "living room light")
        self.assertIn("light.living_room_ceiling", o)
        rc, o, e = self.ha("list", "light", "--area", "living room")
        self.assertEqual(
            sorted(x.split()[0] for x in o.splitlines()), ["light.living_room_ceiling", "light.living_room_lamp"]
        )
        rc, o, e = self.ha("list", "--area", "kuche")
        self.assertIn("light.kitchen", o)
        rc, o, e = self.ha("list", "--area", "nowhere")
        self.assertEqual(rc, 2)
        rc, o, e = self.ha("areas")
        self.assertIn("Living Room (living_room)", o)


# ----------------------------------------------------------------------------- guard
class GuardTests(Base):
    REFUSED = [
        ["call", "homeassistant.restart", "light.kitchen"],
        ["call", "lock.unlock", "lock.front_door"],
        ["call", "alarm_control_panel.alarm_disarm", "alarm_control_panel.home_alarm"],
        ["off", "lock.front_door"],
        ["on", "alarm_control_panel.home_alarm"],
        ["call", "scene.apply", "scene.movie_night", '{"entities": {"lock.front_door": "unlocked"}}'],
        ["call", "scene.create", "scene.movie_night"],
        ["call", "light.reload", "light.kitchen"],
        ["call", "script.reload"],
        ["call", "remote.learn_command", "remote.x"],
        ["call", "light.turn_on", "light.kitchen", '{"entity_id": "lock.front_door"}'],
        ["call", "light.turn_on", "light.kitchen", '{"area_id": "all"}'],
        ["call", "light.turn_on", "light.kitchen", '{"target": {"entity_id": "all"}}'],
        ["call", "light.turn_on", "switch.tv_plug"],
        ["call", "light.turn_on"],
        ["call", "notify.notify", "light.kitchen", '{"message": "hi"}'],
        ["call", "shell_command.x", "light.kitchen"],
        ["call", "hassio.host_reboot", "light.kitchen"],
        ["call", "button.press", "light.kitchen"],
        ["on", "button.router_restart"],
        ["on", "sensor.car_status"],
        ["off", "cover.living_room_blinds"],
        ["off", "cover.garage"],
        ["toggle", "cover.garage"],
        ["on", "switch.garage_door_opener"],
        ["call", "cover.open_cover", "cover.garage"],
        ["on", "scene.bad_scene"],
        ["scene", "run", "bad_scene"],
        ["script", "run", "open_up"],
        ["call", "script.open_up"],
        ["call", "script.turn_on", "script.open_up"],
        ["set", "switch.tv_plug", "--brightness", "20"],
        ["set", "light.kitchen", "--brightness", "200"],
        ["media", "light.kitchen", "play"],
        ["on", "--area", "Living Room", "--domain", "sensor"],
    ]

    def test_refused_without_sending(self):
        for argv in self.REFUSED:
            rc, o, e = self.ha(*argv)
            self.assertEqual(rc, 2, (argv, o, e))
            self.assertIn("refused", e, argv)
        self.assertNothingSent()
        self.assertEqual(self.logged(), [])

    def test_check_request_allowlist(self):
        bad = [
            ("DELETE", "/api/states/light.kitchen", None),
            ("PUT", "/api/states", None),
            ("POST", "/api/states/light.kitchen", {"state": "on"}),
            ("GET", "/api/config", None),
            ("GET", "/api/websocket", None),
            ("GET", "/api/", None),
            ("GET", "/api/states?x=1", None),
            ("POST", "/api/template", {"template": "{{ states.device_tracker | list }}"}),
            ("POST", "/api/config/script/config/x", {}),
            ("POST", "/api/events/x", {}),
            ("POST", "/api/services/homeassistant/restart", {"entity_id": "light.x"}),
            ("POST", "/api/services/lock/unlock", {"entity_id": "lock.x"}),
            ("POST", "/api/services/light/turn_on", {"entity_id": "lock.x"}),
            ("POST", "/api/services/light/turn_on", {"entity_id": "light.x", "device_id": "abc"}),
            ("POST", "/api/services/light/turn_on", {}),
            ("POST", "/api/services/light/turn_on", {"entity_id": "all"}),
            ("POST", "/api/services/script/reload", {"entity_id": "script.x"}),
            ("GET", "/api/config/automation/config/1", None),
        ]
        for m, p, b in bad:
            with self.assertRaises(H.Refused, msg=(m, p, b)):
                H.check_request(m, p, b)
        H.check_request("GET", "/api/states")
        H.check_request("GET", "/api/states/light.kitchen")
        H.check_request("POST", "/api/template", {"template": H.AREA_TEMPLATE})
        H.check_request("POST", "/api/services/light/turn_off", {"entity_id": ["light.a", "light.b"]})

    def test_never_websocket_or_other_methods(self):
        self.ha("list")
        self.ha("off", "light.living_room_ceiling")
        self.ha("state", "tv")
        for _kind, method, path, body in self.fake.log:
            self.assertNotEqual(path, "/api/websocket")
            self.assertIn(method, ("GET", "POST"))
            if method == "POST" and path == "/api/template":
                self.assertEqual(body, {"template": H.AREA_TEMPLATE})

    def test_device_url_template_guard(self):
        H.check_request("POST", "/api/template", {"template": H.DEVICE_URL_TEMPLATE % "light.kitchen"})
        for t in [
            H.DEVICE_URL_TEMPLATE % "light.kitchen') }}{{ states.person | list }}{{ ('x",
            H.DEVICE_URL_TEMPLATE % "Light.Kitchen",
            H.DEVICE_URL_TEMPLATE % "kitchen",
            H.DEVICE_URL_TEMPLATE.replace("configuration_url", "name") % "light.kitchen",
        ]:
            with self.assertRaises(H.Refused, msg=t):
                H.check_request("POST", "/api/template", {"template": t})
        with self.assertRaises(H.Refused):
            H.check_request("POST", "/api/template", {"template": H.DEVICE_URL_TEMPLATE % "light.kitchen", "x": 1})

    def test_lan_networks(self):
        # exactly the RFC 1918 blocks: 10/8, 172.16/12, 192.168/16
        self.assertEqual(
            [(n.network_address.packed[:2], n.prefixlen) for n in H.LAN_NETS],
            [(bytes([10, 0]), 8), (bytes([172, 16]), 12), (bytes([192, 168]), 16)],
        )

    def test_lan_url(self):
        # one host in each RFC 1918 block, derived from H.LAN_NETS (no private-range literal in the public tree)
        ten, mid, home = (str(n.network_address + k) for n, k in zip(H.LAN_NETS, (5, 300, 23), strict=False))
        ok = {
            "http://" + home: "http://" + home,
            f"http://{home}/": "http://" + home,
            f"http://{ten}:8080": f"http://{ten}:8080",
            "https://wled-shelf.local/": "https://wled-shelf.local",
            "http://wled-tv": "http://wled-tv",
            "http://" + mid: "http://" + mid,
        }
        for u, want in ok.items():
            self.assertEqual(H.lan_url(u), want, u)
        for u in [
            "http://198.51.100.9",
            "http://8.8.8.8",
            "http://example.com",
            "http://172.32.0.1",
            f"http://{home}/admin",
            "http://u:p@" + home,
            "ftp://" + home,
            f"http://{home}?x=1",
            f"http://{home}:99999",
            "http://999.1.1.1",
            None,
            42,
            "",
        ]:
            self.assertIsNone(H.lan_url(u), u)

    def test_device_url_command(self):
        rc, o, e = self.ha("device-url", "Ceiling", "Light")
        self.assertEqual((rc, o.strip()), (0, "http://ceiling-light.local"), e)
        rc, o, e = self.ha("device-url", "light.living_room_ceiling", "--json")
        self.assertEqual(json.loads(o), {"entity_id": "light.living_room_ceiling", "url": "http://ceiling-light.local"})
        rc, o, e = self.ha("device-url", "cover.living_room_blinds")
        self.assertEqual((rc, o.strip()), (0, "https://blinds.lan:8443"))
        for target, why in [
            ("light.kitchen", "not a LAN"),
            ("switch.tv_plug", "not a LAN"),
            ("light.bedroom_dead", "no configuration_url"),
            ("light.living_room_lamp", "no configuration_url"),
            ("light.nope", "no such entity"),
        ]:
            rc, o, e = self.ha("device-url", target)
            self.assertEqual(rc, 2, target)
            self.assertEqual(o, "", target)
            self.assertIn(why, e, target)
            self.assertNoCanary(o + e)
        self.assertNothingSent()
        self.assertEqual(self.logged(), [])

    def test_owner_ok_allows_garage_and_is_logged(self):
        rc, o, e = self.ha("call", "cover.open_cover", "cover.garage", "--owner-ok", "yes open the garage")
        self.assertEqual(rc, 0, e)
        lg = self.logged()
        self.assertEqual(lg[-1]["owner_ok"], "yes open the garage")
        self.assertEqual(lg[-1]["service"], "cover.open_cover")

    def test_update_install_needs_owner_ok(self):
        rc, o, e = self.ha("call", "update.install", "update.core_update", '{"backup": true}')
        self.assertEqual(rc, 2, o + e)
        self.assertIn("owner", e)
        self.assertEqual(self.logged(), [])

    def test_update_install_with_owner_ok_is_sent_and_logged(self):
        rc, o, e = self.ha(
            "call", "update.install", "update.core_update", '{"backup": true}', "--owner-ok", "update it"
        )
        self.assertIn(rc, (0, 3), o + e)
        lg = self.logged()
        self.assertEqual(lg[-1]["service"], "update.install")
        self.assertEqual(lg[-1]["owner_ok"], "update it")

    def test_yaml_script_runs_with_note(self):
        rc, o, e = self.ha("script", "run", "yaml_only", "--wait", "1")
        self.assertIn("lock/garage check skipped", e)
        self.assertEqual(self.fake.calls[0][:2], ("script", "turn_on"))

    def test_bad_token_no_leak(self):
        with open(os.environ["HA_TOKEN_FILE"], "w") as fh:
            fh.write("wrong-token-0123456789-abcdef\n")
        rc, o, e = self.ha("list")
        self.assertEqual(rc, 1)
        self.assertNotIn("wrong-token", o + e)


# ----------------------------------------------------------------------------- writes
class WriteTests(Base):
    def test_off_logged_and_read_back(self):
        rc, o, e = self.ha("off", "light.living_room_ceiling")
        self.assertEqual(rc, 0, e)
        self.assertIn("light.living_room_ceiling: on 50% 2700K -> off ok", o)
        lg = self.logged()
        self.assertEqual(len(lg), 1)
        self.assertEqual(lg[0]["service"], "light.turn_off")
        self.assertEqual(lg[0]["entity_ids"], ["light.living_room_ceiling"])
        self.assertEqual(lg[0]["result"], "ok")
        self.assertTrue(re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", lg[0]["ts"]))
        self.assertEqual(lg[0]["old"], {"light.living_room_ceiling": "on 50% 2700K"})
        self.assertEqual(lg[0]["new"], {"light.living_room_ceiling": "off"})
        mode = stat.S_IMODE(os.stat(os.path.join(self.state_dir, "actions.log")).st_mode)
        self.assertEqual(mode, 0o600)
        with open(os.path.join(self.state_dir, "actions.log")) as fh:
            self.assertNotIn(fake_ha.TOKEN, fh.read())

    def test_already_off(self):
        rc, o, e = self.ha("off", "light.kitchen")
        self.assertEqual(rc, 0)
        self.assertIn("off -> off ok (already)", o)

    def test_set_light(self):
        rc, o, e = self.ha("set", "Floor Lamp", "--brightness", "30", "--color-temp", "3000")
        self.assertEqual(rc, 0, e)
        self.assertEqual(
            self.fake.calls[0][2],
            {"brightness_pct": 30, "color_temp_kelvin": 3000, "entity_id": "light.living_room_lamp"},
        )
        self.assertIn("off -> on 30% 3000K ok", o)

    def test_media(self):
        rc, o, e = self.ha("media", "media_player.lg_webos_tv_oled", "volume", "25")
        self.assertEqual(rc, 0, e)
        self.assertIn("vol 12%", o)
        self.assertIn("-> on vol 25% src HDMI 1 ok", o)
        rc, o, e = self.ha("media", "LG webOS TV OLED55C7V", "pause")
        self.assertEqual(rc, 0, e)
        self.assertIn("-> paused", o)
        self.assertNoCanary(o + e)
        rc, o, e = self.ha(
            "call", "media_player.select_source", "media_player.lg_webos_tv_oled", '{"source": "Netflix"}'
        )
        self.assertEqual(rc, 0, e)
        self.assertEqual(self.fake.calls[-1][2], {"source": "Netflix", "entity_id": "media_player.lg_webos_tv_oled"})

    def test_area_off_one_call(self):
        self.fake.srv.states["light.living_room_lamp"]["state"] = "on"
        rc, o, e = self.ha("off", "--area", "Living Room")
        self.assertEqual(rc, 0, e)
        self.assertEqual(len(self.fake.calls), 1)
        self.assertEqual(self.fake.calls[0][2]["entity_id"], ["light.living_room_ceiling", "light.living_room_lamp"])
        self.assertEqual(self.logged()[0]["entity_ids"], ["light.living_room_ceiling", "light.living_room_lamp"])

    def test_scene_and_script(self):
        rc, o, e = self.ha("scene", "run", "movie_night")
        self.assertEqual(rc, 0, e)
        self.assertEqual(self.fake.calls[-1][:2], ("scene", "turn_on"))
        self.assertNoCanary(o)
        self.assertNotIn("<coord>", o)
        rc, o, e = self.ha("script", "run", "Goodnight")
        self.assertEqual(rc, 0, e)
        self.assertEqual(self.fake.calls[-1][:2], ("script", "turn_on"))
        rc, o, e = self.ha("call", "script.goodnight", '{"x": 1}')
        self.assertEqual(rc, 0, e)
        self.assertEqual(
            self.fake.calls[-1], ("script", "turn_on", {"variables": {"x": 1}, "entity_id": "script.goodnight"})
        )

    def test_dry_run_sends_nothing(self):
        rc, o, e = self.ha("off", "light.living_room_ceiling", "-n")
        self.assertEqual(rc, 0)
        self.assertIn("dry run: would call light.turn_off", o)
        self.assertNothingSent()
        self.assertEqual(self.logged(), [])

    def test_ha_error_logged(self):
        self.fake.srv.fail.add(("media_player", "select_source"))
        rc, o, e = self.ha("call", "media_player.select_source", "media_player.lg_webos_tv_oled", '{"source": "Nope"}')
        self.assertEqual(rc, 1)
        self.assertIn("HTTP 400", e)
        self.assertNoCanary(e)
        self.assertTrue(self.logged()[0]["result"].startswith("error"))


class MaskedIdTests(Base):
    def test_email_masked_id_usable(self):
        rc, o, e = self.ha("list", "vacuum")
        self.assertIn("vacuum.<email>", o)
        rc, o, e = self.ha("state", "vacuum.<email>")
        self.assertEqual(rc, 0, e)
        self.assertIn("state: docked", o)
        rc, o, e = self.ha("call", "vacuum.start", "vacuum.<email>")
        self.assertEqual(rc, 0, e)
        self.assertEqual(self.fake.calls[-1], ("vacuum", "start", {"entity_id": "vacuum.alex_gmail_com"}))
        self.assertIn("vacuum.<email>: docked -> cleaning", o)
        self.assertNoCanary(o + e + json.dumps(self.logged()))

    def test_unavailable_target_refused(self):
        rc, o, e = self.ha("on", "light.bedroom_dead")
        self.assertEqual(rc, 2)
        self.assertIn("unavailable", e)
        self.assertNothingSent()


class UnconfirmedTests(Base):
    effects = False

    def test_not_confirmed_exit_3(self):
        rc, o, e = self.ha("off", "light.living_room_ceiling", "--wait", "0.5")
        self.assertEqual(rc, 3)
        self.assertIn("not confirmed after 0.5s", o)
        self.assertEqual(self.logged()[0]["result"], "unconfirmed")
        self.assertEqual(len(self.fake.calls), 1)


class InputTextGuardTests(unittest.TestCase):
    """input_text.set_value: only the phone-context feed helper, one string value <= 255 chars."""

    def test_feed_helper_allowed(self):
        H.check_service("input_text", "set_value", {"entity_id": "input_text.phone_context", "value": '{"v":1}'})

    def test_everything_else_refused(self):
        bad = [
            {"entity_id": "input_text.autopilot_event", "value": "x"},
            {"entity_id": "input_text.phone_context", "value": "x" * 256},
            {"entity_id": "input_text.phone_context", "value": 5},
            {"entity_id": "input_text.phone_context"},
            {"entity_id": "input_text.phone_context", "value": "x", "extra": 1},
            {"entity_id": ["input_text.phone_context", "input_text.other"], "value": "x"},
        ]
        for body in bad:
            with self.assertRaises(H.Refused, msg=str(body)[:80]):
                H.check_service("input_text", "set_value", body)
        for svc in ("set_max", "reload", "set_min"):
            with self.assertRaises(H.Refused):
                H.check_service("input_text", svc, {"entity_id": "input_text.phone_context", "value": "x"})


class SelectDatetimeGuardTests(unittest.TestCase):
    """select.select_option / input_datetime.set_datetime: one listed entity each, listed values only."""

    def test_work_mode_allowed(self):
        for opt in ("vacuum", "vacuum_and_mop", "mop_after_vacuum"):
            H.check_service("select", "select_option", {"entity_id": "select.deebot_work_mode", "option": opt})

    def test_select_everything_else_refused(self):
        bad = [
            {"entity_id": "select.deebot_active_map", "option": "Home"},
            {"entity_id": "select.deebot_work_mode", "option": "mop"},
            {"entity_id": "select.deebot_work_mode"},
            {"entity_id": "select.deebot_work_mode", "option": "vacuum", "cycle": True},
            {"entity_id": ["select.deebot_work_mode", "select.other"], "option": "vacuum"},
        ]
        for body in bad:
            with self.assertRaises(H.Refused, msg=str(body)[:80]):
                H.check_service("select", "select_option", body)
        for svc in ("select_next", "select_first", "reload"):
            with self.assertRaises(H.Refused):
                H.check_service("select", svc, {"entity_id": "select.deebot_work_mode", "option": "vacuum"})

    def test_last_mop_allowed(self):
        H.check_service(
            "input_datetime",
            "set_datetime",
            {"entity_id": "input_datetime.deebot_last_mop", "datetime": "2026-10-20 12:10:00"},
        )
        H.check_service(
            "input_datetime", "set_datetime", {"entity_id": "input_datetime.deebot_last_mop", "date": "2026-10-20"}
        )

    def test_datetime_everything_else_refused(self):
        bad = [
            {"entity_id": "input_datetime.other", "datetime": "2026-10-20 12:10:00"},
            {"entity_id": "input_datetime.deebot_last_mop", "datetime": "tomorrow"},
            {"entity_id": "input_datetime.deebot_last_mop", "timestamp": 1},
            {"entity_id": "input_datetime.deebot_last_mop"},
            {"entity_id": "input_datetime.deebot_last_mop", "date": "2026-10-20", "time": "10:00:00"},
        ]
        for body in bad:
            with self.assertRaises(H.Refused, msg=str(body)[:80]):
                H.check_service("input_datetime", "set_datetime", body)
        with self.assertRaises(H.Refused):
            H.check_service("input_datetime", "reload", {"entity_id": "input_datetime.deebot_last_mop"})


if __name__ == "__main__":
    unittest.main()


# ----------------------------------------------------------------------------- todo (shopping list)
class TodoTests(Base):
    def setUp(self):
        super().setUp()
        os.environ.pop("HA_TODO_LISTS", None)
        os.environ["HA_TODO_CONFIG"] = os.path.join(self.tmp.name, "no-todo-config.json")

    def tearDown(self):
        os.environ.pop("HA_TODO_CONFIG", None)
        super().tearDown()

    def writes(self):
        return [c for c in self.fake.calls if c[0] == "todo" and c[1] != "get_items"]

    def test_items_open_and_all(self):
        rc, o, e = self.ha("todo", "items", "--json")
        self.assertEqual(rc, 0, e)
        self.assertEqual([i["summary"] for i in json.loads(o)], ["Milk"])
        rc, o, e = self.ha("todo", "items", "--all")
        self.assertEqual(rc, 0, e)
        self.assertIn("u2\tcompleted\tBatteries AA", o)
        self.assertEqual(self.writes(), [])

    def test_get_items_uses_return_response(self):
        self.ha("todo", "items")
        H.check_request(
            "POST",
            "/api/services/todo/get_items?return_response",
            {"entity_id": "todo.shopping_list", "status": ["needs_action"]},
        )
        for m, p, b in [
            ("POST", "/api/services/todo/get_items", {"entity_id": "todo.shopping_list"}),
            ("POST", "/api/services/light/turn_on?return_response", {"entity_id": "light.x"}),
            ("POST", "/api/services/todo/add_item?return_response", {"entity_id": "todo.shopping_list", "item": "x"}),
            ("POST", "/api/services/todo/get_items?x=1", {"entity_id": "todo.shopping_list"}),
            ("GET", "/api/services/todo/get_items?return_response", None),
        ]:
            with self.assertRaises(H.Refused, msg=(m, p)):
                H.check_request(m, p, b)

    def test_other_lists_and_fields_refused(self):
        for argv in (
            ["todo", "items", "--list", "todo.private_notes"],
            ["todo", "add", "x", "--list", "todo.private_notes"],
            ["call", "todo.add_item", "todo.private_notes", '{"item": "x"}'],
            ["call", "todo.add_item", "todo.shopping_list", '{"item": "x", "area_id": "all"}'],
            ["call", "todo.add_item", "todo.shopping_list", '{"item": "x", "foo": "y"}'],
            ["call", "todo.update_item", "todo.shopping_list", '{"item": "u1", "status": "gone"}'],
            ["call", "todo.remove_completed_items", "todo.shopping_list"],
            ["todo", "add", "x" * 300],
            ["todo", "add"],
        ):
            rc, o, e = self.ha(*argv)
            self.assertEqual(rc, 2, (argv, o, e))
            self.assertIn("refused", e, argv)
        self.assertEqual(self.writes(), [])
        self.assertNotIn("Dentist", "".join(str(x) for x in self.fake.log if x[2] != "/api/states"))
        for b in (
            {"entity_id": ["todo.shopping_list", "todo.private_notes"], "item": "x"},
            {"entity_id": "todo.shopping_list"},
        ):
            with self.assertRaises(H.Refused):
                H.check_request("POST", "/api/services/todo/add_item", b)

    def test_config_allows_more_lists(self):
        cfg = os.environ["HA_TODO_CONFIG"]
        with open(cfg, "w") as fh:
            json.dump({"lists": ["todo.private_notes", "light.kitchen"]}, fh)
        self.assertEqual(H.todo_lists(), ["todo.shopping_list", "todo.private_notes"])
        rc, o, e = self.ha("todo", "items", "--list", "todo.private_notes", "--json")
        self.assertEqual(rc, 0, e)

    def test_add_done_undo_rm_read_back(self):
        rc, o, e = self.ha("todo", "add", "Desiccant", "packs", "--description", "for the feeder", "--json")
        self.assertEqual(rc, 0, e)
        new = json.loads(o)
        self.assertEqual(
            (new["summary"], new["status"], new["description"]), ("Desiccant packs", "needs_action", "for the feeder")
        )
        self.assertEqual(self.fake.srv.states["todo.shopping_list"]["state"], "2")
        rc, o, e = self.ha("todo", "done", new["uid"])
        self.assertEqual(rc, 0, e)
        self.assertIn("completed", o)
        rc, o, e = self.ha("todo", "undo", "desiccant packs", "--json")
        self.assertEqual((rc, json.loads(o)["status"]), (0, "needs_action"), e)
        rc, o, e = self.ha("todo", "rm", new["uid"], "--json")
        self.assertEqual((rc, json.loads(o)["status"]), (0, "removed"), e)
        self.assertEqual([i["uid"] for i in self.fake.srv.todo["todo.shopping_list"]], ["u1", "u2"])
        svcs = [(c[1], c[2].get("status")) for c in self.writes()]
        self.assertEqual(
            svcs,
            [("add_item", None), ("update_item", "completed"), ("update_item", "needs_action"), ("remove_item", None)],
        )
        log = self.logged()
        self.assertEqual(
            [x["service"] for x in log], ["todo.add_item", "todo.update_item", "todo.update_item", "todo.remove_item"]
        )

    def test_ambiguous_and_missing(self):
        self.fake.srv.todo["todo.shopping_list"].append({"uid": "u3", "summary": "Milk", "status": "needs_action"})
        rc, o, e = self.ha("todo", "done", "milk")
        self.assertEqual(rc, 2)
        self.assertIn("u1", e)
        rc, o, e = self.ha("todo", "rm", "Bread")
        self.assertEqual(rc, 2)
        self.assertEqual(self.writes(), [])

    def test_dry_run_sends_no_write(self):
        rc, o, e = self.ha("todo", "add", "Eggs", "-n")
        self.assertEqual(rc, 0, e)
        self.assertIn("dry run", o)
        self.assertEqual(self.writes(), [])

    def test_add_ha_error_exit_1(self):
        self.fake.srv.fail.add(("todo", "add_item"))
        rc, o, e = self.ha("todo", "add", "Eggs")
        self.assertEqual(rc, 1)
        self.fake.srv.fail.clear()


# ----------------------------------------------------------------------------- history, logbook, traces (read-only)
class HistoryTests(Base):
    def setUp(self):
        super().setUp()
        for s in fake_ha.EXTRA_STATES:
            self.fake.srv.states[s["entity_id"]] = s

    def assertReadOnly(self):
        self.assertNothingSent()
        self.assertFalse([x for x in self.fake.log if x[1] not in ("GET", "POST")])
        self.assertFalse([x for x in self.fake.log if x[1] == "POST" and x[2] != "/api/template"])

    def run_ok(self, *argv):
        rc, out, err = self.ha(*argv)
        self.assertEqual(rc, 0, err)
        self.assertNoCanary(out + err)
        self.assertReadOnly()
        return out

    # -- history
    def test_history_minimal_response(self):
        out = self.run_ok("history", "vacuum.robi")
        kind, start, q = self.fake.srv.queries[-1]
        self.assertEqual(kind, "history")
        self.assertRegex(start, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")
        self.assertEqual(
            q,
            {"filter_entity_id": "vacuum.robi", "end_time": q["end_time"], "minimal_response": "", "no_attributes": ""},
        )
        states = [ln.split()[2] for ln in out.splitlines()[1:]]
        self.assertEqual(states, ["docked", "cleaning", "returning", "docked"])
        self.assertIn("3 changes", out.splitlines()[0])
        self.assertIn("(at start)", out)
        self.assertRegex(out, r"cleaning\s+50m")
        self.assertEqual(self.fake.srv.ws_log, [])

    def test_history_measurements_rounded_not_masked(self):
        out = self.run_ok("history", "living", "room", "temperature", "--since", "3h")
        self.assertIn("21.53 °C", out)
        self.assertIn("22.06 °C", out)
        self.assertNotIn("<coord>", out)
        self.assertIn("unavailable", out)

    def test_history_attrs_are_gated(self):
        out = self.run_ok("history", "light.living_room_ceiling", "--attrs", "brightness,entity_picture")
        q = self.fake.srv.queries[-1][2]
        self.assertEqual(q.get("significant_changes_only"), "0")
        self.assertNotIn("minimal_response", q)
        self.assertIn("brightness=128", out)
        self.assertIn("brightness=255", out)
        self.assertEqual(out.count("brightness=255"), 1)  # attribute-only noise collapsed
        self.assertIn("not shown: entity_picture", out)
        out = self.run_ok("history", "media_player.lg_webos_tv_oled", "--attrs", "source,media_content_id", "--json")
        d = json.loads(out)
        self.assertEqual(d["changes"][-1]["attrs"]["source"], "HDMI 1")
        self.assertIn("<token>", d["changes"][-1]["attrs"]["media_content_id"])

    def test_history_hidden_entities_stay_hidden(self):
        for eid in (
            "person.alex",
            "sensor.phone_geocoded_location",
            "device_tracker.phone",
            "device_tracker.alex_airpods",
            "sensor.phone_wifi_connection",
            "zone.home",
        ):
            rc, out, err = self.ha("history", eid)
            self.assertEqual(rc, 2, eid)
            self.assertIn("privacy gate", err)
            self.assertNoCanary(out + err)
        self.assertEqual(self.fake.srv.queries, [])

    def test_history_json_and_limit(self):
        d = json.loads(self.run_ok("history", "vacuum.robi", "--json", "--limit", "2"))
        self.assertEqual([c["state"] for c in d["changes"]], ["returning", "docked"])
        self.assertEqual(d["earlier_not_shown"], 2)
        self.assertTrue(all(isinstance(c["seconds"], int) for c in d["changes"]))

    def test_history_ambiguous_and_unknown(self):
        rc, out, err = self.ha("history", "light")
        self.assertEqual(rc, 2)
        rc, out, err = self.ha("history", "light.nope")
        self.assertEqual(rc, 2)
        self.assertEqual(self.fake.srv.queries, [])

    def test_time_parsing(self):
        now = H.dt.datetime(2026, 10, 20, 12, 0, tzinfo=H.dt.timezone.utc)
        self.assertEqual(H.parse_when("24h", now, "x"), now - H.dt.timedelta(hours=24))
        self.assertEqual(H.parse_when("1d12h", now, "x"), now - H.dt.timedelta(hours=36))
        self.assertEqual(H.parse_when("90m", now, "x"), now - H.dt.timedelta(minutes=90))
        self.assertEqual(
            H.parse_when("2026-10-19T08:00:00Z", now, "x"), H.dt.datetime(2026, 10, 19, 8, tzinfo=H.dt.timezone.utc)
        )
        self.assertIsNotNone(H.parse_when("2026-10-19", now, "x").tzinfo)
        t = H.parse_when("23:59", now, "x")
        self.assertLessEqual(t, now)
        self.assertGreater(t, now - H.dt.timedelta(days=1))
        for bad in ("yesterday", "5x", "25:00", ""):
            with self.assertRaises(H.Refused):
                H.parse_when(bad, now, "x")
        rc, _, err = self.ha("history", "vacuum.robi", "--since", "1h", "--until", "2h")
        self.assertEqual(rc, 2)
        rc, _, err = self.ha("history", "vacuum.robi", "--since", "40d")
        self.assertEqual(rc, 2)
        self.assertEqual(self.fake.srv.queries, [])

    # -- logbook
    def test_logbook_gated(self):
        out = self.run_ok("logbook")
        self.assertEqual(self.fake.srv.queries[-1][2], {"end_time": self.fake.srv.queries[-1][2]["end_time"]})
        self.assertIn('Robi: changed to returning  <- automation.vacuum_dock "Vacuum dock"', out)
        self.assertIn("Ceiling Light: changed to on  <- light.turn_on (a user)", out)
        self.assertIn("<- <hidden entity>", out)
        self.assertIn("3 entries of location/network/Bluetooth entities left out", out)
        self.assertNotIn("alex_airpods", out)
        self.assertIn("[bt:", out)
        self.assertNotIn("Alex:", out)
        d = json.loads(self.run_ok("logbook", "--json", "--limit", "2"))
        self.assertEqual(len(d["entries"]), 2)
        self.assertEqual(d["hidden_entries"], 3)

    def test_logbook_one_entity(self):
        out = self.run_ok("logbook", "vacuum.robi", "--since", "12h")
        self.assertEqual(self.fake.srv.queries[-1][2]["entity"], "vacuum.robi")
        self.assertIn("changed to returning", out)
        rc, out, err = self.ha("logbook", "person.alex")
        self.assertEqual(rc, 2)
        self.assertEqual(len(self.fake.srv.queries), 1)

    # -- traces
    def test_trace_summary(self):
        out = self.run_ok("trace", "vacuum_dock")
        types = [t for t, _ in self.fake.srv.ws_log if t != "close"]  # the close frame may land after main() returns
        self.assertEqual(types, ["auth", "trace/list", "trace/get", "trace/get", "trace/get"])
        self.assertEqual(
            self.fake.srv.ws_log[1][1],
            {"id": 1, "type": "trace/list", "domain": "automation", "item_id": "1700000000001"},
        )
        self.assertIn("3 of 3 stored runs", out)
        self.assertIn("trigger: state of vacuum.robi (cleaning -> returning)", out)
        self.assertIn("conditions: ok condition/0  ok condition/1", out)
        self.assertIn("action/0  vacuum.return_to_base -> vacuum.robi", out)
        self.assertIn("action/1  notify.mobile_app_galaxy", out)
        self.assertIn("action/2  delay 5m done", out)
        self.assertIn("FAILED condition/0 (person.jamie: state not_home, wanted not_home)", out)
        self.assertIn("error: Error rendering data template at <ip>", out)
        self.assertNotIn("alex_airpods", out)
        first = out.splitlines()[1]
        self.assertIn("error", first)  # newest first

    def test_trace_last_json(self):
        d = json.loads(self.run_ok("trace", "automation.vacuum_dock", "--last", "1", "--json"))
        self.assertEqual(d["stored_runs"], 3)
        self.assertEqual(len(d["runs"]), 1)
        self.assertEqual(d["runs"][0]["result"], "error")
        self.assertEqual([t for t, _ in self.fake.srv.ws_log].count("trace/get"), 1)
        for r in d["runs"]:
            self.assertNotIn("config", r)
            self.assertNotIn("changed_variables", json.dumps(r))

    def test_trace_refusals(self):
        for argv in (("trace", "yaml_noid"), ("trace", "vacuum.robi"), ("trace", "nothing_like_this")):
            rc, out, err = self.ha(*argv)
            self.assertEqual(rc, 2, argv)
        self.assertEqual(self.fake.srv.ws_log, [])

    def test_script_trace_empty(self):
        out = self.run_ok("trace", "script.goodnight")
        self.assertIn("(no stored traces)", out)
        self.assertEqual(self.fake.srv.ws_log[1][1]["domain"], "script")
        self.assertEqual(self.fake.srv.ws_log[1][1]["item_id"], "goodnight")

    # -- guards
    def test_read_guards(self):
        ts = "2026-10-20T10:00:00+00:00"
        tq = ts.replace("+", "%2B")  # the query as urlencode() writes it
        ok = [
            f"/api/history/period/{ts}?filter_entity_id=vacuum.robi&end_time={tq}&minimal_response=&no_attributes=",
            f"/api/history/period/{ts}?filter_entity_id=light.x&end_time={tq}&significant_changes_only=0",
            f"/api/logbook/{ts}?end_time={tq}",
            f"/api/logbook/{ts}?end_time={tq}&entity=vacuum.robi",
        ]
        for p in ok:
            H.check_request("GET", p)
        bad = [
            f"/api/history/period/{ts}?end_time={tq}",  # no entity: no dumps
            f"/api/history/period/{ts}?filter_entity_id=a.b,c.d&end_time={tq}",
            f"/api/history/period/{ts}?filter_entity_id=a.b&end_time={tq}&significant_changes_only=1",
            f"/api/history/period/{ts}?filter_entity_id=a.b&filter_entity_id=c.d&end_time={tq}",
            f"/api/history/period/{ts}?filter_entity_id=a.b%0A&end_time={tq}",
            f"/api/history/period/{ts}?filter_entity_id=a.b&end_time={tq}&minimal_response=1",
            f"/api/history/period/2026-10-20?filter_entity_id=a.b&end_time={tq}",
            f"/api/history/period?filter_entity_id=a.b&end_time={tq}",
            f"/api/logbook/{ts}?end_time={tq}&context_id=x",
            f"/api/logbook/{ts}?end_time=tomorrow",
            f"/api/logbook/{ts}?entity=a.b",
            f"/api/logbook/{ts}/x?end_time={tq}",
        ]
        for p in bad:
            with self.assertRaises(H.Refused, msg=p):
                H.check_request("GET", p)
        with self.assertRaises(H.Refused):
            H.check_request("POST", f"/api/logbook/{ts}?end_time={ts}", {})

    def test_ws_guards(self):
        H.check_ws("trace/list", {"domain": "automation", "item_id": "1700000000001"})
        H.check_ws("trace/get", {"domain": "script", "item_id": "goodnight", "run_id": "01JAAAAAAAAAAAAAAAAAAAAAA1"})
        for t, p in (
            ("config/entity_registry/list", {}),
            ("call_service", {"domain": "light", "service": "turn_on"}),
            ("subscribe_events", {}),
            ("trace/list", {"domain": "light", "item_id": "x"}),
            ("trace/list", {"domain": "automation"}),
            ("trace/list", {"domain": "automation", "item_id": "x", "y": 1}),
            ("trace/get", {"domain": "automation", "item_id": "x", "run_id": "../x"}),
            ("trace/get", {"domain": "automation", "item_id": "a b", "run_id": "r"}),
        ):
            with self.assertRaises(H.Refused, msg=t):
                H.check_ws(t, p)
        ws = H.Client(self.fake.url, fake_ha.TOKEN).ws()
        try:
            with self.assertRaises(H.Refused):
                ws.call("call_service", domain="light", service="turn_on")
            self.assertEqual(ws.call("trace/get", domain="automation", item_id="1700000000001", run_id="nope"), None)
        finally:
            ws.close()
        self.assertNotIn("call_service", [t for t, _ in self.fake.srv.ws_log])
        self.assertEqual(self.fake.calls, [])

    def test_ws_bad_token(self):
        with self.assertRaises(H.HAError):
            H.Client(self.fake.url, "wrong-token-0123456789abcdef").ws()


class EgressTests(Base):
    """REST by handle "ha" through core.egress; a GET over the hub's cap is read again direct; ws stays direct."""

    class Refused(Exception):
        def __init__(self, code, error):
            super().__init__(error)
            self.code, self.error = code, error

    def fake_egress(self, mode, refuse=None, fail=None):
        calls = []
        T = self

        class FakeEgress:
            Refused = T.Refused

            @staticmethod
            def mode(handle):
                return mode

            @staticmethod
            def secret(handle):
                return "hub:secret:" + handle

            @staticmethod
            def open(req, handle, timeout=20):
                calls.append((req.get_method(), req.full_url, req.get_header("Authorization"), handle))
                if refuse:
                    raise T.Refused(502, refuse)
                if fail:
                    raise fail
                return io.BytesIO(b'[{"entity_id": "light.hub_path", "state": "on", "attributes": {}}]')

        return FakeEgress, calls

    def client(self, mode, refuse=None, token=fake_ha.TOKEN, fail=None):
        eg, calls = self.fake_egress(mode, refuse, fail)
        old = (H._egress, H._REFUSED)
        H._egress, H._REFUSED = eg, (self.Refused,)
        self.addCleanup(lambda: (setattr(H, "_egress", old[0]), setattr(H, "_REFUSED", old[1])))
        return H.Client(self.fake.url, token, handle="ha"), calls

    def test_hub_mode_sends_the_placeholder(self):
        c, calls = self.client("hub")
        self.assertEqual(c.states()[0]["entity_id"], "light.hub_path")
        self.assertEqual(calls[0][0], "GET")
        self.assertEqual(calls[0][2], "Bearer hub:secret:ha")
        self.assertTrue(calls[0][1].endswith("/api/states"))

    def test_hub_mode_get_over_the_cap_goes_direct(self):
        c, calls = self.client("hub", refuse="response_too_large")
        states = c.states()
        self.assertEqual(len(calls), 1)
        self.assertNotIn("light.hub_path", [s.get("entity_id") for s in states])
        self.assertTrue(states)

    def test_hub_mode_post_over_the_cap_is_not_resent(self):
        c, calls = self.client("hub", refuse="response_too_large")
        with self.assertRaises(H.HAError):
            c.call("light", "turn_on", {"entity_id": "light.living_room_lamp"})
        self.assertEqual(self.fake.calls, [])

    def test_hub_mode_other_refusal_is_an_error(self):
        c, calls = self.client("hub", refuse="credential_endpoint_mismatch")
        with self.assertRaises(H.HAError) as cm:
            c.states()
        self.assertIn("credential_endpoint_mismatch", str(cm.exception))

    def test_hub_mode_all_entity_logbook_goes_direct(self):
        c, calls = self.client("hub")
        now = H.dt.datetime.now(H.dt.timezone.utc)
        c.logbook(now - H.dt.timedelta(days=1), now)
        self.assertEqual(calls, [])
        c.logbook(now - H.dt.timedelta(days=1), now, eid="light.living_room_lamp")
        self.assertEqual(len(calls), 1)
        self.assertIn("/api/logbook/", calls[0][1])

    def test_hub_stalled_read_goes_direct(self):
        c, calls = self.client("hub", fail=H.urllib.error.URLError(TimeoutError("timed out")))
        self.assertTrue(c.states())
        self.assertEqual(len(calls), 1)

    def test_hub_stalled_write_is_not_resent(self):
        c, calls = self.client("hub", fail=H.urllib.error.URLError(TimeoutError("timed out")))
        with self.assertRaises(H.HAError):
            c.call("light", "turn_on", {"entity_id": "light.living_room_lamp"})
        self.assertEqual(self.fake.calls, [])

    def test_hub_refused_connection_write_goes_direct(self):
        c, calls = self.client("hub", fail=H.urllib.error.URLError(ConnectionRefusedError(111, "refused")))
        c.call("light", "turn_on", {"entity_id": "light.living_room_lamp"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(self.fake.calls), 1)

    def test_local_mode_stays_direct(self):
        c, calls = self.client("local")
        self.assertTrue(c.states())
        self.assertEqual(calls, [])

    def test_no_local_token_in_hub_mode(self):
        c, calls = self.client("hub", refuse="response_too_large", token=None)
        with self.assertRaises(H.HAError):
            c.states()
        with self.assertRaises(H.HAError):
            c.ws()

    def test_handle_only_for_the_live_default_file(self):
        if H._egress is None:
            H._egress, restore = object(), True
        else:
            restore = False
        try:
            self.assertEqual(H.egress_handle(H.DEFAULT_TOKEN_FILE, None), "ha")
            self.assertIsNone(H.egress_handle(H.DEFAULT_TOKEN_FILE, self.fake.url))
            self.assertIsNone(H.egress_handle(os.environ["HA_TOKEN_FILE"], None))
        finally:
            if restore:
                H._egress = None
