"""Deeper offline tests for ~/bin/ha-audit: per-section findings from fixture HA responses (stubbed
REST/websocket layer, no sockets), the privacy gate, and error handling (HA down, 401, timeouts,
malformed data). Never talks to a real HA; the only sockets are 127.0.0.1 fakes."""

import contextlib
import copy
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fake_ha  # noqa: E402
from test_ha_audit import BIN, H  # noqa: E402

NOW = fake_ha.NOW
ago = fake_ha.ago
SECRET_STATE = "SECRETSTATE4242"


class StubRest:
    """In-process stand-in for RestClient: enforces the same read-only guard, serves fake_ha fixtures."""

    def __init__(self, over=None, fail=None, history=None):
        self.over, self.fail = over or {}, fail or {}
        self.history = fake_ha.HISTORY if history is None else history
        self.calls = []

    def get(self, path, query=None):
        H.check_rest("GET", path, query)
        self.calls.append((path, dict(query or {})))
        for prefix, exc in self.fail.items():
            if path.startswith(prefix):
                raise exc
        if path in self.over:
            return copy.deepcopy(self.over[path])
        if path == "/api/config":
            return {"version": "2026.10.1", "components": ["hue", "tuya"], "latitude": 12.34567}
        if path == "/api/states":
            return copy.deepcopy(fake_ha.STATES)
        if path == "/api/services":
            return copy.deepcopy(fake_ha.SERVICES)
        if path == "/api/config/config_entries/entry":
            return copy.deepcopy(fake_ha.ENTRIES)
        for pre, src in (
            ("/api/config/automation/config/", fake_ha.AUTOMATION_CFG),
            ("/api/config/script/config/", fake_ha.SCRIPT_CFG),
        ):
            if path.startswith(pre):
                c = src.get(path[len(pre) :])
                if c is None:
                    raise H.HAError("HTTP 404")
                return copy.deepcopy(c)
        if path.startswith("/api/history/period/"):
            out = []
            for eid in query["filter_entity_id"].split(","):
                if eid in self.history:
                    ser = [{"state": s, "last_changed": ago(days=dd)} for s, dd in self.history[eid]]
                    ser[0] = dict(ser[0], entity_id=eid)
                    out.append(ser)
            return out
        raise H.HAError("HTTP 404")


class StubWS:
    ha_version = "2026.10.1"

    def __init__(self, over=None, fail=None):
        self.over, self.fail = over or {}, fail or {}
        self.calls = []

    def call(self, t, **p):
        H.check_ws(t, p)
        self.calls.append((t, p))
        key = p.get("endpoint") if t == "supervisor/api" else t
        if key in self.fail or t in self.fail:
            raise self.fail.get(key) or self.fail[t]
        if key in self.over:
            return copy.deepcopy(self.over[key])
        if t == "trace/list":
            return copy.deepcopy(fake_ha.TRACES.get(p.get("domain"), []))
        if t == "supervisor/api":
            return copy.deepcopy(fake_ha.SUPERVISOR.get(p["endpoint"]))
        return copy.deepcopy(
            {
                "config/entity_registry/list": fake_ha.ENTITIES,
                "config/device_registry/list": fake_ha.DEVICES,
                "config/area_registry/list": fake_ha.AREAS,
                "config_entries/get": fake_ha.ENTRIES,
                "config_entries/flow/progress": fake_ha.FLOWS,
                "repairs/list_issues": fake_ha.ISSUES,
            }.get(t)
        )

    def close(self):
        pass


def run(rest=None, ws=None, history=True):
    d = H.collect(rest or StubRest(), ws or StubWS(), NOW, history=history)
    return d, H.analyze(d, NOW)


def outputs(F):
    md, _ = H.privacy_gate(H.render_md(F, "2026-10-15"))
    js, _ = H.privacy_gate(json.dumps(H._jsonable(F), ensure_ascii=False))
    sm, _ = H.privacy_gate("\n".join(H.summary_lines(F)))
    return md, js, sm


def plus(lst, *items):
    return copy.deepcopy(lst) + list(items)


class Sections(unittest.TestCase):
    def test_stub_baseline(self):
        d, F = run()
        self.assertEqual(d["ha_version"], "2026.10.1")
        self.assertTrue(d["history_ok"])
        self.assertEqual([x["entity_id"] for x in F["duplicates"]], ["light.wohnzimmer_2"])
        self.assertEqual(len(H.summary_lines(F)), 8)
        self.assertFalse([e for e in d["errors"] if not e.endswith("not readable via the UI API")], d["errors"])

    # 1. entities
    def test_dead_entity_age_from_state_without_history(self):
        states = plus(
            fake_ha.STATES,
            fake_ha.st("sensor.long_dead", "unavailable", changed=ago(days=40)),
            fake_ha.st("sensor.fresh_dead", "unavailable", changed=ago(days=3)),
        )
        _, F = run(StubRest({"/api/states": states}), history=False)
        bad = {b["entity_id"]: b for b in F["bad"]}
        # oldest last_changed = HA start (real HA: nothing predates the restart); younger ones age from their state
        self.assertEqual(bad["sensor.long_dead"]["basis"], "since HA start")
        self.assertGreaterEqual(bad["sensor.long_dead"]["days"], 39)
        self.assertEqual(bad["sensor.fresh_dead"]["basis"], "state")
        self.assertEqual(F["uptime_days"], bad["sensor.long_dead"]["days"])
        long_ids = {b["entity_id"] for b in F["bad_long"]}
        self.assertIn("sensor.long_dead", long_ids)
        self.assertNotIn("sensor.fresh_dead", long_ids)
        self.assertFalse(F["history_ok"])

    def test_history_overrides_state_age(self):
        states = plus(fake_ha.STATES, fake_ha.st("sensor.flappy", "unavailable", changed=ago(days=40)))
        hist = dict(fake_ha.HISTORY, **{"sensor.flappy": [("unavailable", 30), ("5", 10), ("unavailable", 2)]})
        _, F = run(StubRest({"/api/states": states}, history=hist))
        b = {x["entity_id"]: x for x in F["bad"]}["sensor.flappy"]
        self.assertEqual(b["basis"], "history")
        self.assertLess(b["days"], 7)
        self.assertNotIn("sensor.flappy", {x["entity_id"] for x in F["bad_long"]})

    def test_unknown_ok_domains_not_flagged(self):
        states = plus(
            fake_ha.STATES,
            fake_ha.st("button.press_me", "unknown"),
            fake_ha.st("scene.movie", "unknown"),
            fake_ha.st("sensor.mystery", "unknown", changed=ago(days=20)),
        )
        _, F = run(StubRest({"/api/states": states}), history=False)
        bad = {b["entity_id"]: b for b in F["bad"]}
        self.assertNotIn("button.press_me", bad)
        self.assertNotIn("scene.movie", bad)
        self.assertEqual(bad["sensor.mystery"]["cls"], "unknown")

    def test_bad_since_start(self):
        start = min(s["last_changed"] for s in fake_ha.STATES if s.get("last_changed"))
        states = plus(fake_ha.STATES, fake_ha.st("sensor.boot_dead", "unavailable", changed=start))
        _, F = run(StubRest({"/api/states": states}), history=False)
        b = {x["entity_id"]: x for x in F["bad"]}["sensor.boot_dead"]
        self.assertEqual(b["basis"], "since HA start")
        self.assertEqual("sensor.boot_dead" in [x["entity_id"] for x in F["bad_since_start"]], b["days"] < 7)

    def test_orphans_missing_entry_and_device(self):
        ents = plus(
            fake_ha.ENTITIES,
            fake_ha.reg("sensor.lost_entry", "zha", entry="E_NOPE"),
            fake_ha.reg("sensor.lost_device", "hue", entry="E_HUE", device="D_NOPE"),
        )
        _, F = run(ws=StubWS({"config/entity_registry/list": ents}))
        orph = {o["entity_id"]: o["why"] for o in F["orphans"]}
        self.assertIn("its integration entry is gone", orph["sensor.lost_entry"])
        self.assertIn("its device is gone", orph["sensor.lost_device"])

    # 2. devices
    def test_disabled_device_listed(self):
        devs = plus(fake_ha.DEVICES, dict(fake_ha.DEVICES[5], id="D_OFF", name="Spare Plug", disabled_by="user"))
        _, F = run(ws=StubWS({"config/device_registry/list": devs}))
        self.assertIn("Spare Plug", [x["name"] for x in F["dev_disabled"]])

    # 3. integrations
    def test_integration_states(self):
        entries = plus(
            fake_ha.ENTRIES,
            {
                "entry_id": "E_ZW",
                "domain": "zwave_js",
                "title": "Z-Wave",
                "state": "setup_error",
                "source": "user",
                "disabled_by": None,
                "reason": "cannot reach 198.51.100.99",
            },
            {
                "entry_id": "E_DIS",
                "domain": "sonos",
                "title": "Sonos",
                "state": "not_loaded",
                "source": "user",
                "disabled_by": "user",
            },
        )
        _, F = run(StubRest({"/api/config/config_entries/entry": entries}))
        failing = {e["domain"]: e for e in F["int_failing"]}
        self.assertIn("zwave_js", failing)
        self.assertIn("<ip>", failing["zwave_js"]["reason"])
        self.assertIn("sonos", [e["domain"] for e in F["int_disabled"]])
        self.assertNotIn("sonos", failing)

    def test_entries_fall_back_to_websocket(self):
        rest = StubRest(fail={"/api/config/config_entries/entry": H.HAError("HTTP 404")})
        ws = StubWS()
        d, F = run(rest, ws)
        self.assertIn("config_entries/get", [c[0] for c in ws.calls])
        self.assertEqual(len(d["entries"]), len(fake_ha.ENTRIES))
        self.assertEqual([e["domain"] for e in F["int_failing"]], ["tuya"])

    def test_discovered_flows_only_non_user(self):
        flows = [
            {"flow_id": "a", "handler": "esphome", "context": {"source": "zeroconf"}},
            {"flow_id": "b", "handler": "hue", "context": {"source": "user"}},
            {"flow_id": "c", "handler": "tuya", "context": {"source": "reauth"}},
        ]
        _, F = run(ws=StubWS({"config_entries/flow/progress": flows}))
        self.assertEqual([f["handler"] for f in F["int_discovered"]], ["esphome"])

    # 4. automations / scripts
    def test_automation_ids_guarded(self):
        states = plus(
            fake_ha.STATES,
            fake_ha.st("automation.weird", "on", {"id": "../../etc"}),
            fake_ha.st("automation.noid", "on", {}),
        )
        rest = StubRest({"/api/states": states})
        d, _ = run(rest)
        self.assertEqual(d["automation_cfg"]["automation.weird"], {"error": "unusual id, not fetched"})
        self.assertIn("no id", d["automation_cfg"]["automation.noid"]["error"])
        self.assertFalse([p for p, _ in rest.calls if "etc" in p])

    def test_trace_errors_scrubbed(self):
        traces = {
            "automation": [
                {
                    "item_id": "x",
                    "script_execution": "error",
                    "timestamp": {"start": ago(1)},
                    "error": "Timeout talking to 198.51.100.7 for 'Secret Song'",
                }
            ]
        }
        d, _ = run(ws=StubWS({"trace/list": traces["automation"]}))
        err = d["traces"]["automation"][0]["error"]
        self.assertNotIn("198.51.100", err)
        self.assertNotIn("Secret Song", err)

    def test_config_404s_collapse_into_one_error(self):
        d, _ = run()
        cfg = [e for e in d["errors"] if "automation/script configs" in e]
        self.assertLessEqual(len(cfg), 1)
        self.assertFalse([e for e in d["errors"] if e.startswith(("automation_cfg", "script_cfg"))])

    # 5. repairs, updates, supervisor
    def test_repairs_ignored_vs_active(self):
        iss = {
            "issues": [
                {
                    "domain": "hue",
                    "issue_id": "x_alex@example.com",
                    "severity": "warning",
                    "translation_key": "a",
                    "ignored": True,
                },
                {"domain": "tuya", "issue_id": "y", "severity": "error", "translation_key": "b", "ignored": False},
            ]
        }
        d, F = run(ws=StubWS({"repairs/list_issues": iss}))
        self.assertEqual([i["translation_key"] for i in F["issues_active"]], ["b"])
        self.assertEqual([i["translation_key"] for i in F["issues_ignored"]], ["a"])
        self.assertNotIn("alex@example.com", json.dumps(d["issues"]))

    def test_update_skipped(self):
        states = plus(
            fake_ha.STATES,
            fake_ha.st(
                "update.foo_firmware",
                "off",
                {"installed_version": "1.0", "latest_version": "1.1", "skipped_version": "1.1", "title": "Foo"},
            ),
        )
        _, F = run(StubRest({"/api/states": states}))
        self.assertIn("update.foo_firmware", F["updates_skipped"])

    def test_supervisor_rest_fallback(self):
        ws = StubWS(fail={"supervisor/api": H.HAError("supervisor/api: unknown_command")})
        rest = StubRest(
            {
                "/api/hassio/core/info": {
                    "result": "ok",
                    "data": {"version": "2026.10.1", "version_latest": "2026.10.2", "update_available": True},
                }
            }
        )
        d, F = run(rest, ws)
        self.assertEqual(d["supervisor"]["core"]["version_latest"], "2026.10.2")
        self.assertTrue([p for p, _ in rest.calls if p.startswith("/api/hassio/")])
        self.assertIn("core 2026.10.1→2026.10.2", H.summary_lines(F)[5])

    def test_addon_bad_slug_not_queried(self):
        sup = copy.deepcopy(fake_ha.SUPERVISOR)
        sup["/addons"] = {"addons": [{"name": "Evil", "slug": "../x", "state": "stopped"}]}
        ws = StubWS({"/addons": sup["/addons"]})
        d, _ = run(ws=ws)
        self.assertFalse([c for c in ws.calls if "../x" in str(c[1].get("endpoint"))])
        self.assertIsNone(d["supervisor"]["addons"][0]["boot"])


class History(unittest.TestCase):
    def test_no_history_makes_no_history_calls(self):
        rest = StubRest()
        d, _ = run(rest, history=False)
        self.assertFalse([p for p, _ in rest.calls if p.startswith("/api/history")])
        self.assertEqual(d["history"], {})

    def test_chunking_and_window(self):
        rest = StubRest()
        ids = [f"sensor.s{i:02d}" for i in range(60)]
        H.fetch_history(rest, ids, NOW)
        hq = [(p, q) for p, q in rest.calls if p.startswith("/api/history")]
        self.assertEqual(len(hq), 3)
        self.assertTrue(all(len(q["filter_entity_id"].split(",")) <= 25 for _, q in hq))
        self.assertIn("2026-09-15T21:00:00", hq[0][0])
        self.assertTrue(hq[0][1]["end_time"].startswith("2026-10-15T21:00:00"))

    def test_series_semantics(self):
        hist = {
            "sensor.a": [("1", 30), ("unavailable", 9)],
            "sensor.b": [("unknown", 30)],
            "sensor.c": [("unavailable", 30), ("2", 5)],
        }
        h = H.fetch_history(StubRest(history=hist), list(hist), NOW)
        self.assertEqual(h["sensor.a"]["bad_since"], ago(days=9))
        self.assertTrue(h["sensor.a"]["good_in_window"])
        self.assertFalse(h["sensor.b"]["good_in_window"])
        self.assertEqual(h["sensor.b"]["bad_since"], ago(days=30))
        self.assertIsNone(h["sensor.c"]["bad_since"])
        self.assertEqual(h["sensor.a"]["n_changes"], 1)

    def test_logbook_and_raw_history_paths_refused(self):
        for p in (
            "/api/logbook/2026-10-01T00:00:00",
            "/api/history/period",
            "/api/states/sensor.x",
            "/api/camera_proxy/camera.door",
            "/api/template",
        ):
            with self.assertRaises(H.ReadOnlyViolation, msg=p):
                H.check_rest("GET", p)
        with self.assertRaises(H.ReadOnlyViolation):
            H.check_rest("GET", "/api/history/period/2026-10-01T00:00:00", {"significant_changes_only": "1"})


class Privacy(unittest.TestCase):
    def test_gate_counts_late_redactions(self):
        raw = "a alex@example.com b 198.51.100.7 c aa:bb:cc:dd:ee:ff d 12.34567 e Musterstrasse 12"
        out, n = H.privacy_gate(raw)
        self.assertEqual(n, 5)
        for c in ("alex@example.com", "198.51.100.7", "aa:bb:cc:dd:ee:ff", "12.34567", "Musterstrasse 12"):
            self.assertNotIn(c, out)
        self.assertEqual(H.privacy_gate("already <ip> and <email>"), ("already <ip> and <email>", 0))

    def test_debug_gate_prints_only_scrubbed_lines(self):
        err = io.StringIO()
        os.environ["HA_AUDIT_DEBUG_GATE"] = "1"
        try:
            with contextlib.redirect_stderr(err):
                H.privacy_gate("ok line\nleak 203.0.113.77 here")
        finally:
            del os.environ["HA_AUDIT_DEBUG_GATE"]
        self.assertIn("<ip>", err.getvalue())
        self.assertNotIn("203.0.113.77", err.getvalue())

    def test_state_values_and_attributes_never_kept(self):
        s = H.slim_state(fake_ha.st("sensor.power", SECRET_STATE, {"media_title": "Secret Song", "latitude": 1.0}))
        self.assertNotIn(SECRET_STATE, json.dumps(s))
        self.assertNotIn("Secret Song", json.dumps(s))
        p = H.slim_state(fake_ha.STATES[0])
        self.assertEqual(
            set(p) - {"entity_id", "domain", "cls", "last_changed", "last_updated", "restored"}, {"device_trackers"}
        )

    def test_injected_canaries_never_reach_any_output(self):
        leak = [
            "alex@example.com",
            "198.51.100.",
            "aa:bb:cc:dd:ee:ff",
            "Bahnhofstrasse 5",
            "23.45678",
            SECRET_STATE,
            "Secret Song",
            fake_ha.TOKEN,
            "AirPods",
        ]
        states = plus(
            fake_ha.STATES,
            fake_ha.st("sensor.living_power", SECRET_STATE, {"friendly_name": "Power alex@example.com"}),
            fake_ha.st("media_player.tv", "unavailable", {"media_title": "Secret Song", "latitude": 23.45678}),
            fake_ha.st(
                "update.router", "on", {"title": "Router 198.51.100.1", "installed_version": "1", "latest_version": "2"}
            ),
        )
        areas = plus(fake_ha.AREAS, {"area_id": "flat", "name": "Bahnhofstrasse 5"})
        devs = plus(
            fake_ha.DEVICES, dict(fake_ha.DEVICES[5], id="D_R", name="Router aa:bb:cc:dd:ee:ff", area_id="flat")
        )
        sup = copy.deepcopy(fake_ha.SUPERVISOR)
        sup.setdefault("/addons", {"addons": []})["addons"].append(
            {"name": "Proxy alex@example.com", "slug": "proxy", "state": "started", "update_available": True}
        )
        ws = StubWS(
            {"config/area_registry/list": areas, "config/device_registry/list": devs, "/addons": sup["/addons"]}
        )
        _, F = run(StubRest({"/api/states": states}), ws)
        for blob, name in zip(outputs(F), ("md", "json", "summary")):  # noqa: B905
            for c in leak:
                self.assertNotIn(c, blob, f"{c!r} leaked into {name}")
            for k in ("latitude", "media_title", "friendly_name"):
                self.assertNotIn(f'"{k}"', blob)


class _Silent:
    """127.0.0.1 server that accepts connections and never answers (timeout fixture)."""

    def __enter__(self):
        self.s = socket.socket()
        self.s.bind(("127.0.0.1", 0))
        self.s.listen(8)
        self.conns, self.stop = [], False
        self.url = f"http://127.0.0.1:{self.s.getsockname()[1]}"
        self.s.settimeout(0.2)
        self.t = threading.Thread(target=self._loop, daemon=True)
        self.t.start()
        return self

    def _loop(self):
        while not self.stop:
            try:
                self.conns.append(self.s.accept()[0])
            except OSError:
                pass

    def __exit__(self, *a):
        self.stop = True
        self.t.join(2)
        for c in self.conns:
            c.close()
        self.s.close()


def closed_port_url():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


class Errors(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="ha-audit-deep-")
        cls.tok = os.path.join(cls.tmp, "tok")
        cls.bad = os.path.join(cls.tmp, "badtok")
        with open(cls.tok, "w") as fh:
            fh.write(fake_ha.TOKEN + "\n")
        cls.wrong = "wrong-token-CANARY-9876543210zyx"
        with open(cls.bad, "w") as fh:
            fh.write(cls.wrong + "\n")

    def cli(self, *args):
        env = dict(os.environ, HOME=self.tmp, PYTHONDONTWRITEBYTECODE="1")
        env.pop("HA_AUDIT_DEBUG_GATE", None)
        t = time.monotonic()
        p = subprocess.run([sys.executable, BIN, *args], capture_output=True, text=True, timeout=60, env=env)
        return p, time.monotonic() - t

    def assertNoTokens(self, text):
        self.assertNotIn(fake_ha.TOKEN, text)
        self.assertNotIn(self.wrong, text)

    def test_ha_down_cli(self):
        p, secs = self.cli("--url", closed_port_url(), "--token-file", self.tok)
        self.assertNotEqual(p.returncode, 0)
        self.assertLess(secs, 30)
        self.assertNoTokens(p.stdout + p.stderr)
        self.assertEqual(p.stdout, "")

    def test_ha_down_rest_raises(self):
        with self.assertRaises(OSError):
            H.RestClient(closed_port_url(), fake_ha.TOKEN, timeout=2).get("/api/config")

    def test_401_cli(self):
        with fake_ha.FakeHA() as srv:
            p, _ = self.cli("--url", srv.url, "--token-file", self.bad, "--check")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("auth failed", p.stderr)
        self.assertNoTokens(p.stdout + p.stderr)

    def test_401_rest(self):
        with fake_ha.FakeHA() as srv:
            with self.assertRaises(H.HAError) as cm:
                H.RestClient(srv.url, self.wrong).get("/api/states")
        self.assertEqual(str(cm.exception), "HTTP 401")

    def test_401_rest_partial_collect(self):
        rest = StubRest(fail={"/api/": H.HAError("HTTP 401")})
        d, F = run(rest)
        self.assertTrue(any(e.startswith("states: HAError: HTTP 401") for e in d["errors"]), d["errors"])
        self.assertEqual(d["states"], [])
        self.assertEqual(len(d["entries"]), len(fake_ha.ENTRIES))  # websocket fallback
        self.assertEqual(len(H.summary_lines(F)), 8)

    def test_rest_timeout(self):
        with _Silent() as srv:
            t = time.monotonic()
            with self.assertRaises(OSError):
                H.RestClient(srv.url, fake_ha.TOKEN, timeout=0.5).get("/api/config")
            self.assertLess(time.monotonic() - t, 5)

    def test_ws_timeout(self):
        with _Silent() as srv:
            t = time.monotonic()
            with self.assertRaises(OSError):
                H.WSClient(srv.url, fake_ha.TOKEN, timeout=0.5)
            self.assertLess(time.monotonic() - t, 5)

    def test_timeouts_in_collect_are_partial(self):
        rest = StubRest(fail={"/api/history": TimeoutError("timed out"), "/api/services": TimeoutError("timed out")})
        ws = StubWS(fail={"config/device_registry/list": TimeoutError("timed out")})
        d, F = run(rest, ws)
        errs = "\n".join(d["errors"])
        for name in ("history: TimeoutError", "services: TimeoutError", "device_registry: TimeoutError"):
            self.assertIn(name, errs)
        self.assertFalse(d["history_ok"])
        self.assertIsNone(d["services"])
        self.assertEqual(d["devices"], [])
        md, js, sm = outputs(F)
        self.assertIn("## 1. Entities", md)
        self.assertEqual(len(sm.splitlines()), 8)

    def test_readonly_violation_not_swallowed(self):
        ws = StubWS(fail={"config/entity_registry/list": H.ReadOnlyViolation("nope")})
        with self.assertRaises(H.ReadOnlyViolation):
            run(ws=ws)

    def test_malformed_payloads(self):
        rest = StubRest(
            {
                "/api/states": {"not": "a list"},
                "/api/services": [],
                "/api/config": None,
                "/api/config/config_entries/entry": ["junk", None],
            }
        )
        ws = StubWS(
            {
                "config/entity_registry/list": None,
                "config/device_registry/list": [1, "x"],
                "repairs/list_issues": ["junk"],
                "trace/list": None,
                "/addons": {"addons": ["x"]},
            }
        )
        d, F = run(rest, ws)
        self.assertEqual(d["states"], [])
        self.assertEqual(d["devices"], [])
        self.assertEqual(len(H.summary_lines(F)), 8)
        H.render_md(F, "2026-10-15")

    def test_token_file_errors(self):
        empty = os.path.join(self.tmp, "empty")
        with open(empty, "w") as fh:
            fh.write("short\n")
        p, _ = self.cli("--url", closed_port_url(), "--token-file", empty)
        self.assertEqual(p.returncode, 1)
        self.assertIn("token file empty or invalid", p.stderr)
        self.assertNotIn("short", p.stderr.replace("token file empty", ""))
        p, _ = self.cli("--url", closed_port_url(), "--token-file", os.path.join(self.tmp, "missing"))
        self.assertNotEqual(p.returncode, 0)

    def test_check_mode(self):
        with fake_ha.FakeHA() as srv:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = H.main(["--url", srv.url, "--token-file", self.tok, "--check"])
            log = list(srv.log)
        self.assertEqual(rc, 0)
        self.assertRegex(out.getvalue(), r"^HA 2026\.10\.1: \d+ states, \d+ registry entities\n$")
        self.assertNoTokens(out.getvalue())
        self.assertNotIn("/api/history", str(log))


if __name__ == "__main__":
    unittest.main(verbosity=1)
