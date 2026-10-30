#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime

LIB = os.environ.get("HOME_CTX_LIB", os.path.expanduser("~/bin/lib"))
BIN = os.environ.get("HOME_CTX_BIN", os.path.expanduser("~/bin/home-ctx"))
TMP = tempfile.mkdtemp(prefix="home-ctx-tests-")
ENT = os.path.join(TMP, "entities.json")
with open(ENT, "w") as f:
    json.dump(
        {
            "lr_lights": ["light.lr_a", "light.lr_b"],
            "other_lights": ["light.bed"],
            "tvs": ["media_player.tv"],
            "speakers": ["media_player.spk"],
            "battery": "sensor.p_batt",
            "next_alarm": "sensor.p_alarm",
            "lux": "sensor.lux",
            "presence": "binary_sensor.pres",
            "movie": "input_boolean.movie",
            "workday": "binary_sensor.work",
            "bedroom_lamp": "light.bed",
        },
        f,
    )
os.environ.update(
    HOME_AP2_ENTITIES=ENT,
    HOME_CTX_TZ="Europe/Zurich",
    HOME_CTX_STATE=os.path.join(TMP, "state"),
    HOME_CTX_CONFIG=os.path.join(TMP, "cfg.json"),
    USAGE="off",
)
sys.path.insert(0, LIB)
sys.dont_write_bytecode = True
import homectx as H  # noqa: E402
import homectx_sim as S  # noqa: E402

Z = "+02:00"


def T(s):
    return datetime.fromisoformat(s + Z).timestamp()


PHONE = {
    "source": "phone",
    "age_min": 3,
    "place": {"kind": "home", "zone": "Home", "band": None, "town": "X"},
    "screen": "off",
    "dnd": True,
    "battery": {"level": 80, "charging": True},
}


class Feed(unittest.TestCase):
    def test_night_full_context(self):
        ctx, st = H.build(PHONE, False, {}, now=T("2026-10-20T23:30:00"))
        self.assertEqual((ctx["z"], ctx["scr"], ctx["dnd"], ctx["chg"], ctx["si"]), ("home", 0, 1, 1, 0))
        self.assertEqual(ctx["so"], T("2026-10-20T23:30:00"))
        self.assertNotIn("X", H.encode(ctx))  # never the town
        self.assertLessEqual(len(H.encode(ctx)), 255)

    def test_screen_off_since_kept_and_day_hidden(self):
        t0 = T("2026-10-20T22:00:00")
        _, st = H.build(PHONE, False, {}, now=t0)
        ctx, st2 = H.build(PHONE, False, st, now=t0 + 900)
        self.assertEqual(ctx["so"], t0)
        day, _ = H.build(PHONE, False, st2, now=T("2026-10-21T14:00:00"))
        self.assertIsNone(day["scr"])
        self.assertIsNone(day["so"])
        on = dict(PHONE, screen="on")
        ctx, st3 = H.build(on, False, st2, now=t0 + 1000)
        self.assertEqual((ctx["scr"], ctx["so"], st3["so"]), (1, None, None))

    def test_stale_phone_gives_only_sleepin(self):
        ctx, _ = H.build(dict(PHONE, age_min=45), True, {}, now=T("2026-10-20T23:00:00"))
        self.assertEqual((ctx["z"], ctx["chg"], ctx["scr"], ctx["si"]), (None, None, None, 1))
        ctx, _ = H.build({}, False, {}, now=T("2026-10-20T23:00:00"))
        self.assertIsNone(ctx["z"])

    def test_connected_phone_unchanged_state_counts(self):
        conn = dict(PHONE, age_min=57, connection={"state": "connected"})
        ctx, _ = H.build(conn, False, {}, now=T("2026-10-20T23:00:00"))
        self.assertEqual((ctx["z"], ctx["chg"]), ("home", 1))
        gone = dict(PHONE, age_min=57, connection={"state": "disconnected"})
        self.assertIsNone(H.build(gone, False, {}, now=T("2026-10-20T23:00:00"))[0]["z"])
        ancient = dict(PHONE, age_min=800, connection={"state": "connected"})
        self.assertIsNone(H.build(ancient, False, {}, now=T("2026-10-20T23:00:00"))[0]["z"])

    def test_zones_and_bands(self):
        away = dict(PHONE, place={"kind": "away", "band": "region"})
        ctx, _ = H.build(away, False, {}, now=T("2026-10-20T12:00:00"))
        self.assertEqual((ctx["z"], ctx["b"]), ("away", "region"))
        work = dict(PHONE, place={"kind": "zone", "zone": "Work Place!", "band": "weird"})
        ctx, _ = H.build(work, False, {}, now=T("2026-10-20T12:00:00"))
        self.assertEqual((ctx["z"], ctx["b"]), ("workplace", None))

    def test_needs_write_heartbeat(self):
        now = T("2026-10-20T23:00:00")
        ctx, _ = H.build(PHONE, False, {}, now=now)
        self.assertEqual(H.needs_write(ctx, None, now), (True, "first"))
        last = {"ctx": dict(ctx, t=now - 60, age=1), "written_at": now - 60}
        self.assertEqual(H.needs_write(ctx, last, now), (False, "unchanged"))
        last["written_at"] = now - H.HEARTBEAT_S
        self.assertEqual(H.needs_write(ctx, last, now)[1], "heartbeat")
        self.assertEqual(H.needs_write(dict(ctx, chg=0), last, now)[1], "changed")

    def test_feed_dry_run_never_calls_ha(self):
        res = H.feed(apply=True, phone=PHONE, sleepin=False, now=T("2026-10-20T23:00:00"))
        self.assertFalse(res["applied"])  # config missing = writes disabled
        self.assertIsNone(res["rc"])
        self.assertIn("input_text.set_value", res["would_run"])

    def test_cli_apply_disabled_exit3(self):
        fake = os.path.join(TMP, "phone")
        with open(fake, "w") as f:
            f.write(f"#!/bin/sh\necho '{json.dumps(PHONE)}'\n")
        os.chmod(fake, 0o755)
        env = dict(os.environ, HOME_CTX_PHONE=fake, HOME_CTX_SLEEPIN="/bin/false", HOME_CTX_HA="/bin/false")
        p = subprocess.run([BIN, "feed", "--apply", "--json"], capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(p.returncode, 3, p.stderr)
        self.assertFalse(json.loads(p.stdout)["applied"])
        p = subprocess.run([BIN, "bogus"], capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(p.returncode, 2)

    def test_cli_usage_outcome_ok_with_cmd_in_attrs(self):
        # the usage store counts any exec outcome outside its OK set as a failed run + an error event:
        # a healthy run must log outcome "ok" (the subcommand rides in attrs), a failing one "rc<N>"
        fake = os.path.join(TMP, "phone2")
        with open(fake, "w") as f:
            f.write(f"#!/bin/sh\necho '{json.dumps(PHONE)}'\n")
        os.chmod(fake, 0o755)
        ust = os.path.join(TMP, "usage-state")
        env = {k: v for k, v in os.environ.items() if k != "USAGE"}
        env.update(HOME_CTX_PHONE=fake, HOME_CTX_SLEEPIN="/bin/false", HOME_CTX_HA="/bin/false", USAGE_STATE=ust)
        self.assertEqual(
            subprocess.run([BIN, "feed", "--json"], capture_output=True, env=env, timeout=30).returncode, 0
        )
        self.assertEqual(
            subprocess.run([BIN, "feed", "--apply", "--json"], capture_output=True, env=env, timeout=30).returncode, 3
        )
        recs = []
        for name in sorted(os.listdir(os.path.join(ust, "spool"))):
            with open(os.path.join(ust, "spool", name)) as f:
                recs += [json.loads(x) for x in f if x.strip()]
        recs = [r for r in recs if r.get("f") == "home.autopilot2"]
        self.assertEqual(
            [(r["k"], r.get("o"), (r.get("at") or {}).get("cmd")) for r in recs],
            [("exec", "ok", "feed"), ("exec", "rc3", "feed")],
        )


class Rules(unittest.TestCase):
    def test_arrival_v2(self):
        ev = T("2026-10-20T19:00:00")
        self.assertEqual(S.arrival_v2(True, False, False, False, ev)[0], "evening warm")
        self.assertEqual(S.arrival_v2(True, False, True, False, ev), (None, "TV/movie on"))
        self.assertEqual(S.arrival_v2(True, True, False, False, ev), (None, "lights already on"))
        self.assertEqual(S.arrival_v2(True, False, False, False, T("2026-10-20T01:30:00"))[0], "path 10%")
        self.assertEqual(S.arrival_v2(True, False, False, False, T("2026-10-20T07:59:00"))[0], None)
        self.assertEqual(S.arrival_v2(False, False, False, False, ev), (None, "daylight"))
        self.assertEqual(S.arrival_v2(True, False, False, False, ev, last_fire=ev - 600), (None, "already ran"))

    def test_bedtime(self):
        t = T("2026-10-20T23:30:00")
        self.assertEqual(S.bedtime_ok(t, True, t - 900, False, False), (True, "ok"))
        self.assertEqual(S.bedtime_ok(t, True, t - 300, False, False)[1], "screen not off 10 min")
        self.assertEqual(S.bedtime_ok(t, True, t - 900, True, False)[1], "TV playing")
        self.assertEqual(
            S.bedtime_ok(T("2026-10-20T22:30:00"), True, t - 9000, False, False)[1], "before 23:00 without DND"
        )
        self.assertTrue(S.bedtime_ok(T("2026-10-20T22:30:00"), True, t - 9000, False, False, dnd=1)[0])
        self.assertEqual(S.bedtime_ok(T("2026-10-20T12:00:00"), True, t, False, False)[1], "outside 21:30-04:00")
        self.assertEqual(S.bedtime_ok(t, False, t - 900, False, False)[1], "not home")

    def test_wake(self):
        a = datetime.fromisoformat("2026-10-20T07:20:00" + Z)
        self.assertEqual(S.wake_target(a, True, True, False)[0].strftime("%H:%M"), "07:10")
        self.assertEqual(S.wake_target(a, True, True, True)[1], "sleep-in morning")
        self.assertEqual(S.wake_target(a, False, True, False)[1], "not a workday")
        late = datetime.fromisoformat("2026-10-20T20:30:00" + Z)
        self.assertEqual(S.wake_target(late, True, True, False)[1], "alarm outside 05:00-10:30")

    def test_leaving(self):
        self.assertEqual(S.leave_fire_after(None), 20)
        self.assertEqual(S.leave_fire_after(2), 5)
        self.assertEqual(S.leave_fire_after(12), 12)
        self.assertEqual(S.leaving_v2(8, ["lr_a"], False), (None, "back within 20 min"))
        self.assertEqual(S.leaving_v2(60, ["lr_a"], True)[1], "TV playing (guest?)")
        self.assertEqual(S.leaving_v2(None, ["lr_a", "tv"], False), ("auto-off", "2 on"))
        self.assertEqual(S.leaving_v2(60, [], False), (None, "nothing on"))

    def test_timeline_stable_same_second(self):
        tl = S.Timeline([(10, "b"), (10, "a"), (5, "x")])
        self.assertEqual(tl.at(10), "a")  # last written wins, not the alphabetical one
        self.assertIsNone(tl.at(1))


class Replay(unittest.TestCase):
    def test_synthetic_week(self):
        hist = os.path.join(TMP, "hist")
        os.makedirs(hist, exist_ok=True)

        def h(eid, changes):
            with open(os.path.join(hist, eid + ".json"), "w") as f:
                json.dump({"changes": [{"time": t + Z, "state": s} for t, s in changes]}, f)

        h(
            "sun.sun",
            [
                ("2026-10-19T00:00:00", "below_horizon"),
                ("2026-10-19T08:00:00", "above_horizon"),
                ("2026-10-19T18:30:00", "below_horizon"),
            ],
        )
        h("light.lr_a", [("2026-10-19T00:00:00", "off"), ("2026-10-19T19:30:00", "on"), ("2026-10-20T00:40:00", "off")])
        h("light.bed", [("2026-10-19T00:00:00", "off")])
        h("media_player.tv", [("2026-10-19T00:00:00", "off")])
        h("sensor.p_batt", [("2026-10-19T00:00:00", "discharging"), ("2026-10-19T23:20:00", "charging")])
        h("sensor.p_alarm", [("2026-10-19T00:00:00", "2026-10-20T05:20:00+00:00")])
        h("binary_sensor.work", [("2026-10-19T00:00:00", "on")])
        ev = [
            {"label": "zone", "ts": "2026-10-19T00:00:00" + Z, "ha": {"place": "home"}},
            {"label": "zone", "ts": "2026-10-19T17:00:00" + Z, "ha": {"place": "away", "band": "near"}},
            {"label": "zone", "ts": "2026-10-19T19:00:00" + Z, "ha": {"place": "home", "zone": "Home"}},
        ]
        with open(os.path.join(hist, "vault_ha_events.json"), "w") as f:
            json.dump(ev, f)
        o = S.run(hist=hist, start=T("2026-10-19T09:00:00"), end=T("2026-10-20T09:00:00"))
        self.assertEqual([r["v2"] for r in o["arrival"]], ["evening warm"])
        self.assertEqual(o["bedtime"][0]["fire"], "23:30")
        self.assertEqual(o["bedtime"][0]["lights_on"], ["lr_a"])
        self.assertEqual(o["bedtime"][0]["earlier_by_min"], 70)
        self.assertEqual([r["fire"] for r in o["wakeup"]], ["07:10"])
        self.assertEqual(o["leaving"][0]["why"], "nothing on")


if __name__ == "__main__":
    unittest.main(verbosity=1)
