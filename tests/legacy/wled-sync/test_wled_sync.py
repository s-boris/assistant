"""Tests for ~/bin/wled-sync: fake WLED JSON API servers + fake ha, all on 127.0.0.1.

Run: tests/legacy/wled-sync/run.sh      WLED_SYNC_BIN=/path tests another copy.
"""

import copy
import datetime
import glob
import json
import os
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.environ.get("WLED_SYNC_BIN") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "scripts", "wled-sync"
)
SECRET = "CANARY-WIFI-PSK-123"
RGBW = [255, 145, 22, 160]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        w = self.server.wled
        w.log.append(("GET", self.path, None))
        with w.lock:
            if self.path == "/json/info":
                lc = 3 if w.white else 1
                return self._send(
                    {
                        "ver": w.ver,
                        "mac": "aabbccddeeff",
                        "leds": {"count": 60, "rgbw": w.white, "lc": lc, "seglc": [lc]},
                    }
                )
            if self.path == "/json/state":
                return self._send(w.state)
            if self.path == "/presets.json":
                return self._send(w.presets)
            if self.path == "/cfg.json":
                return self._send(w.cfg)
        self._send({"error": "not found"}, 404)

    def do_POST(self):
        w = self.server.wled
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        w.log.append(("POST", self.path, body))
        if w.post_error:
            return self._send({"error": "boom"}, 500)
        with w.lock:
            if self.path == "/json/state":
                w.apply_state(body)
            elif self.path == "/json/cfg":
                if "def" in body:
                    w.cfg["def"].update(body["def"])
            else:
                return self._send({"error": "not found"}, 404)
        self._send({"success": True})


class FakeWLED:
    """Mimics WLED 0.14+/16: seg colour/fx, async psave (current state; on/bri only with ib, bounds only
    with sb), bootps -> cfg def.ps (unless old firmware), /json/cfg def merge."""

    def __init__(
        self,
        white=True,
        on=False,
        bri=40,
        fx=9,
        col=None,
        presets=None,
        def_ps=0,
        bootps=True,
        save_delay=0.0,
        saves=True,
        ver="16.0.1",
    ):
        self.white, self.ver, self.bootps, self.save_delay, self.saves = white, ver, bootps, save_delay, saves
        self.post_error = False
        c = col or ([0, 0, 255, 0] if white else [0, 0, 255])
        self.state = {
            "on": on,
            "bri": bri,
            "ps": -1,
            "seg": [
                {
                    "id": 0,
                    "start": 0,
                    "stop": 60,
                    "col": [c, [0] * len(c), [0] * len(c)],
                    "fx": fx,
                    "on": True,
                    "bri": 255,
                }
            ],
        }
        self.presets = presets if presets is not None else {"0": {}}
        self.cfg = {"def": {"ps": def_ps, "on": True, "bri": 128}, "nw": {"ins": [{"ssid": "x", "psk": SECRET}]}}
        self.log, self.lock = [], threading.RLock()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.srv.daemon_threads = True
        self.srv.wled = self
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()

    def posts(self):
        return [x for x in self.log if x[0] == "POST"]

    def apply_state(self, body):
        if "on" in body:
            self.state["on"] = bool(body["on"])
        if "bri" in body:
            self.state["bri"] = body["bri"]
        for s in body.get("seg") or []:
            seg = self.state["seg"][s.get("id", 0)]
            if "col" in s:
                for i, c in enumerate(s["col"]):
                    c = list(c) + [0] * (4 - len(c))
                    seg["col"][i] = c[:4] if self.white else c[:3]
            if "fx" in s:
                seg["fx"] = s["fx"]
        ps = body.get("psave")
        if ps:
            if "o" in body:
                raise AssertionError("wled-sync must not save API-call presets")
            snap = copy.deepcopy(self.state)
            preset = {"n": body.get("n", f"Preset {ps}"), "mainseg": 0, "seg": []}
            if body.get("ib"):
                preset.update(on=snap["on"], bri=snap["bri"])
            for seg in snap["seg"]:
                if not body.get("sb"):
                    seg = {k: v for k, v in seg.items() if k not in ("start", "stop")}
                preset["seg"].append(seg)
            if "bootps" in body and self.bootps:
                self.cfg["def"]["ps"] = body["bootps"]

            def do_save():
                with self.lock:
                    self.presets[str(ps)] = preset

            if self.saves:
                threading.Timer(self.save_delay, do_save).start() if self.save_delay else do_save()


def good_presets(white=True):
    c = RGBW if white else RGBW[:3]
    return {
        "0": {},
        "1": {
            "n": "Filament warm",
            "on": True,
            "bri": 128,
            "seg": [{"id": 0, "col": [c, [0] * len(c), [0] * len(c)], "fx": 0}, {"stop": 0}],
        },
    }


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.wleds = {}
        self.lights = []
        self.urls = {}
        self.inbox = os.path.join(self.d, "inbox")
        self.state = os.path.join(self.d, "state", "wled-sync.json")
        self.logf = os.path.join(self.d, "state", "wled-sync.log")
        self.ha_fail = False

    def tearDown(self):
        for w in self.wleds.values():
            w.close()
        self.tmp.cleanup()

    def add(self, eid, name, state="off", wled=None, url=None):
        self.lights.append({"entity_id": eid, "name": name, "state": state, "area": None})
        if wled is not None:
            self.wleds[eid] = wled
            self.urls[eid] = wled.url
        if url:
            self.urls[eid] = url
        return wled

    def run_sync(self, *args, extra_env=None):
        with open(os.path.join(self.d, "ha.json"), "w") as fh:
            json.dump(
                {
                    "lights": self.lights + [{"entity_id": "light.kitchen", "name": "Kitchen", "state": "on"}],
                    "urls": self.urls,
                    "fail": self.ha_fail,
                },
                fh,
            )
        env = dict(
            os.environ,
            FAKE_HA_DIR=self.d,
            WLED_SYNC_HA=os.path.join(HERE, "fake_ha"),
            WLED_SYNC_STATE=self.state,
            WLED_SYNC_LOG=self.logf,
            WLED_SYNC_INBOX=self.inbox,
            WLED_SYNC_SETTLE="3",
            WLED_SYNC_TIMEOUT="3",
            **{"WLED_SYNC_OVERRIDES": os.path.join(self.d, "none.json"), **(extra_env or {})},
        )
        r = subprocess.run([BIN, *args], capture_output=True, text=True, env=env, timeout=120)
        self.assertNotIn(SECRET, r.stdout + r.stderr + self.read(self.logf) + self.notes_text())
        return r

    def read(self, p):
        try:
            with open(p) as fh:
                return fh.read()
        except OSError:
            return ""

    def notes(self):
        return sorted(glob.glob(os.path.join(self.inbox, "*.md")))

    def notes_text(self):
        return "".join(self.read(p) for p in self.notes())

    def st(self):
        return json.loads(self.read(self.state) or "{}")

    def ha_calls(self):
        return [json.loads(x) for x in self.read(os.path.join(self.d, "calls.log")).splitlines() if x.strip()]


class SyncTests(Base):
    def test_rgbw_off_at_night_gets_colour_preset_boot_and_stays_off(self):
        w = self.add("light.wled_5", "WLED Bed", wled=FakeWLED(white=True, on=False, bri=40))
        r = self.run_sync("--quiet")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertFalse(w.state["on"])
        self.assertEqual(w.state["bri"], 40)
        self.assertEqual(w.state["seg"][0]["col"][0], RGBW)
        self.assertEqual(w.state["seg"][0]["fx"], 0)
        p = w.presets["1"]
        self.assertEqual(p["n"], "Filament warm")
        self.assertNotIn("on", p)
        self.assertNotIn("bri", p)
        self.assertNotIn("start", p["seg"][0])
        self.assertEqual(w.cfg["def"]["ps"], 1)
        for m, path, body in w.posts():  # noqa: B007
            self.assertEqual(path, "/json/state")
            self.assertFalse({"on", "bri", "o", "ib", "sb"} & set(body), body)
        self.assertEqual(self.st()["synced"]["light.wled_5"]["how"], "written")
        n = self.notes()
        self.assertEqual(len(n), 1)
        txt = self.read(n[0])
        self.assertIn("# WLED Bed synced", txt)
        self.assertIn("255,145,22,160", txt)
        self.assertNotIn("RGB-only", txt)
        self.assertIn("verified", self.read(self.logf))
        self.assertTrue(all(c[0] in ("list", "device-url") for c in self.ha_calls()))

    def test_on_light_keeps_on_and_brightness(self):
        w = self.add("light.wled", "WLED TV", state="on", wled=FakeWLED(on=True, bri=77))
        self.assertEqual(self.run_sync("--quiet").returncode, 0)
        self.assertEqual((w.state["on"], w.state["bri"]), (True, 77))
        self.assertEqual(w.presets["1"]["seg"][0]["col"][0], RGBW)

    def test_colour_override_by_mac(self):
        ov = os.path.join(self.d, "colours.json")
        with open(ov, "w") as fh:
            json.dump({"AA:BB:CC:DD:EE:FF": [255, 65, 4, 0]}, fh)
        w = self.add("light.wled_6", "WLED Six", wled=FakeWLED(white=True))
        r = self.run_sync("--quiet", extra_env={"WLED_SYNC_OVERRIDES": ov})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(w.posts()[0][2], {"seg": [{"id": 0, "col": [[255, 65, 4, 0]], "fx": 0}]})
        self.assertEqual(w.presets["1"]["seg"][0]["col"][0], [255, 65, 4, 0])

    def test_rgb_only_strip_gets_rgb_and_is_flagged(self):
        w = self.add("light.wled_3", "WLED Terrace", wled=FakeWLED(white=False))
        r = self.run_sync("--quiet")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(w.posts()[0][2], {"seg": [{"id": 0, "col": [[255, 145, 22]], "fx": 0}]})
        self.assertEqual(w.presets["1"]["seg"][0]["col"][0], [255, 145, 22])
        self.assertTrue(self.st()["synced"]["light.wled_3"]["rgb_only"])
        txt = self.notes_text()
        self.assertIn("RGB-only", txt)
        self.assertIn("rgb 255,145,22,", txt)

    def test_already_in_sync_is_marked_without_writes_or_note(self):
        w = self.add("light.wled_4", "WLED Shelf", wled=FakeWLED(presets=good_presets(), def_ps=1))
        self.assertEqual(self.run_sync("--quiet").returncode, 0)
        self.assertEqual(w.posts(), [])
        self.assertIn("already in sync", self.st()["synced"]["light.wled_4"]["how"])
        self.assertEqual(self.notes(), [])

    def test_synced_entity_is_never_contacted_again(self):
        w = self.add("light.wled_5", "WLED Bed", wled=FakeWLED())
        self.run_sync("--quiet")
        n_req, n_ha = len(w.log), len(self.ha_calls())
        w.presets["1"]["n"] = "changed by hand"
        self.assertEqual(self.run_sync("--quiet").returncode, 0)
        self.assertEqual(len(w.log), n_req)
        self.assertEqual(self.ha_calls()[n_ha:], [["list", "light", "--json"]])
        self.assertEqual(len(self.notes()), 1)

    def test_unavailable_is_skipped(self):
        self.add("light.wled_2", "WLED Stairs", state="unavailable", url="http://127.0.0.1:9")
        self.assertEqual(self.run_sync("--quiet").returncode, 0)
        self.assertEqual(self.ha_calls(), [["list", "light", "--json"]])
        self.assertEqual(self.st()["synced"], {})
        self.assertEqual(self.notes(), [])

    def test_differing_preset_is_rewritten(self):
        bad = good_presets()
        bad["1"]["seg"][0]["col"][0] = [255, 0, 0, 0]
        w = self.add("light.wled", "WLED TV", wled=FakeWLED(presets=bad, def_ps=1))
        self.assertEqual(self.run_sync("--quiet").returncode, 0)
        self.assertIn("colour [255, 0, 0, 0]", self.read(self.logf))
        self.assertEqual(w.presets["1"]["seg"][0]["col"][0], RGBW)
        bad2 = good_presets()
        bad2["1"]["seg"][0]["fx"] = 2
        w2 = self.add("light.wled_2", "WLED Stairs", wled=FakeWLED(presets=bad2, def_ps=3))
        self.assertEqual(self.run_sync("--quiet").returncode, 0)
        self.assertEqual((w2.presets["1"]["seg"][0]["fx"], w2.cfg["def"]["ps"]), (0, 1))

    def test_old_firmware_without_bootps_falls_back_to_cfg(self):
        w = self.add("light.wled", "WLED TV", wled=FakeWLED(bootps=False))
        r = self.run_sync("--quiet")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(w.cfg["def"]["ps"], 1)
        self.assertIn(("POST", "/json/cfg", {"def": {"ps": 1}}), w.log)

    def test_async_preset_save_is_waited_for(self):
        w = self.add("light.wled", "WLED TV", wled=FakeWLED(save_delay=1.5))
        self.assertEqual(self.run_sync("--quiet").returncode, 0)
        self.assertEqual(w.presets["1"]["n"], "Filament warm")
        self.assertIn("light.wled", self.st()["synced"])

    def test_failed_save_retries_and_notes_once_after_three(self):
        self.add("light.wled", "WLED TV", wled=FakeWLED(saves=False))
        for i in range(4):
            r = self.run_sync("--quiet")
            self.assertEqual(r.returncode, 1)
            self.assertEqual(self.st()["failures"]["light.wled"]["count"], i + 1)
            self.assertEqual(len(self.notes()), 0 if i < 2 else 1)
        self.assertEqual(self.st()["synced"], {})
        self.assertIn("read-back differs", self.notes_text())
        self.assertIn("sync failing", self.notes_text())
        self.wleds["light.wled"].saves = True
        self.assertEqual(self.run_sync("--quiet").returncode, 0)
        self.assertNotIn("light.wled", self.st()["failures"])
        self.assertIn("light.wled", self.st()["synced"])

    def test_unreachable_wled_does_not_block_others(self):
        self.add("light.wled_2", "WLED Stairs", url="http://127.0.0.1:9")
        w = self.add("light.wled_5", "WLED Bed", wled=FakeWLED())
        r = self.run_sync("--quiet")
        self.assertEqual(r.returncode, 1)
        self.assertIn("unreachable", self.read(self.logf))
        self.assertIn("light.wled_5", self.st()["synced"])
        self.assertNotIn("light.wled_2", self.st()["synced"])
        self.assertEqual(w.presets["1"]["n"], "Filament warm")
        self.assertIn("# WLED Bed synced", self.notes_text())

    def test_post_error_is_a_failure(self):
        w = self.add("light.wled", "WLED TV", wled=FakeWLED())
        w.post_error = True
        self.assertEqual(self.run_sync("--quiet").returncode, 1)
        self.assertIn("HTTP 500", self.read(self.logf))
        self.assertEqual(self.st()["synced"], {})

    def test_same_device_twice_is_synced_once(self):
        w = self.add("light.wled", "WLED TV", wled=FakeWLED())
        self.lights.append({"entity_id": "light.wled_segment_1", "name": "WLED TV Segment 1", "state": "off"})
        self.urls["light.wled_segment_1"] = w.url
        self.assertEqual(self.run_sync("--quiet").returncode, 0)
        self.assertEqual(len([p for p in w.posts() if "psave" in p[2]]), 1)
        self.assertIn("same device", self.st()["synced"]["light.wled_segment_1"]["how"])

    def test_dry_run_and_verify_only_never_write(self):
        w = self.add("light.wled", "WLED TV", wled=FakeWLED())
        w2 = self.add("light.wled_4", "WLED Shelf", wled=FakeWLED(presets=good_presets(), def_ps=1))
        r = self.run_sync("--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("would write", r.stdout)
        self.assertIn("WLED Shelf (light.wled_4): in sync", r.stdout)
        self.assertEqual(self.read(self.state), "")
        r = self.run_sync("--verify-only")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(w.posts() + w2.posts(), [])
        self.assertEqual(list(self.st()["synced"]), ["light.wled_4"])
        self.assertEqual(self.notes(), [])

    def test_only_status_reset(self):
        self.add("light.wled", "WLED TV", wled=FakeWLED())
        w = self.add("light.wled_5", "WLED Bed", wled=FakeWLED())
        self.assertEqual(self.run_sync("--quiet", "--only", "light.wled_5").returncode, 0)
        self.assertEqual(list(self.st()["synced"]), ["light.wled_5"])
        self.assertIn("synced  light.wled_5", self.run_sync("status").stdout)
        self.assertEqual(self.run_sync("reset", "light.wled_5").returncode, 0)
        self.assertEqual(self.st()["synced"], {})
        self.assertEqual(self.run_sync("reset", "light.wled_5").returncode, 2)
        self.assertEqual(self.run_sync("--only", "light.kitchen").returncode, 2)
        self.assertEqual(self.run_sync("--only", "light.wled_9").returncode, 2)
        self.assertEqual(len([p for p in w.posts() if "psave" in p[2]]), 1)

    def test_ha_failure_exits_1_without_note(self):
        self.ha_fail = True
        r = self.run_sync("--quiet")
        self.assertEqual(r.returncode, 1)
        self.assertIn("cannot list lights", self.read(self.logf))
        self.assertEqual(self.notes(), [])

    def test_quiet_run_with_nothing_to_do_logs_nothing(self):
        self.add("light.wled_2", "WLED Stairs", state="unavailable")
        r = self.run_sync("--quiet")
        self.assertEqual((r.returncode, r.stdout, self.read(self.logf)), (0, "", ""))


class ClockTests(Base):
    """WLED_SYNC_NOW pins the clock: log line, state 'at' and note name carry Europe/Zurich wall time at DST
    switches, month end, year end and a leap day (the only clock use: no weekday/month logic in wled-sync)."""

    INSTANTS = [  # UTC instant -> Zurich wall time
        ("2027-03-28T00:59:59", "2027-03-28 01:59:59"),  # last second before the spring-forward jump
        ("2027-03-28T01:00:00", "2027-03-28 03:00:00"),  # 02:00 CET -> 03:00 CEST
        ("2026-10-25T00:30:00", "2026-10-25 02:30:00"),  # first 02:30 (CEST)
        ("2026-10-25T01:30:00", "2026-10-25 02:30:00"),  # second 02:30 (CET)
        ("2026-10-31T22:59:59", "2026-10-31 23:59:59"),  # month end
        ("2026-12-31T23:00:00", "2027-01-01 00:00:00"),  # year end
        ("2028-02-28T23:30:00", "2028-02-29 00:30:00"),  # leap day
    ]

    def test_pinned_clock_at_tricky_dates(self):
        utc = datetime.UTC
        for i, (u, want) in enumerate(self.INSTANTS):
            with self.subTest(utc=u):
                for w in self.wleds.values():
                    w.close()
                self.wleds, self.lights, self.urls = {}, [], {}
                for p in [self.state, self.logf] + self.notes():
                    if os.path.exists(p):
                        os.remove(p)
                self.add(f"light.wled_c{i}", "WLED Clock", wled=FakeWLED(white=True, on=False, bri=40))
                epoch = datetime.datetime.fromisoformat(u).replace(tzinfo=utc).timestamp()
                r = self.run_sync(extra_env={"WLED_SYNC_NOW": str(int(epoch)), "TZ": "Europe/Zurich"})
                self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
                self.assertTrue(self.read(self.logf).startswith(want + " "), self.read(self.logf)[:40])
                self.assertEqual(self.st()["synced"][f"light.wled_c{i}"]["at"], want[:16])
                n = self.notes()
                self.assertEqual(len(n), 1)
                stamp = want.replace("-", "").replace(":", "").replace(" ", "-")
                self.assertTrue(os.path.basename(n[0]).endswith(f"-{stamp}.md"), n[0])

    def test_bad_pin_falls_back_to_real_time(self):
        self.add("light.wled_x", "WLED X", wled=FakeWLED(white=True, on=False, bri=40))
        r = self.run_sync(extra_env={"WLED_SYNC_NOW": "soon"})
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertTrue(self.read(self.logf).startswith(datetime.datetime.now().strftime("%Y-%m-%d")))


if __name__ == "__main__":
    unittest.main()
