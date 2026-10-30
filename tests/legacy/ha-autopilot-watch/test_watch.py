"""Offline tests for ha-autopilot-watch with stub `ha` / `tg` (and `tg --buttons`; synthetic entities; no network)."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.environ.get("AUTOPILOT_BIN", os.path.expanduser("~/bin/ha-autopilot-watch"))

HA_STUB = r"""#!/usr/bin/env python3
import json, os, sys
db = os.environ['STUB_DB']
d = json.load(open(db))
if d.get('down'):
    sys.exit(1)
a = [x for x in sys.argv[1:] if x != '--json']
with open(db + '.log', 'a') as f:
    f.write(' '.join(a) + '\n')
ents = d['entities']
if a[0] == 'state':
    e = ents.get(a[1])
    if not e:
        sys.exit(2)
    print(json.dumps(dict(e, entity_id=a[1])))
elif a[0] == 'list':
    print(json.dumps([dict(v, entity_id=k) for k, v in ents.items() if k.startswith(a[1] + '.')]))
elif a[0] == 'off':
    ents[a[1]]['state'] = 'off'
    json.dump(d, open(db, 'w'))
"""
TG_STUB = (
    '#!/bin/sh\nprintf "%s\\n" "${TG_SOFT:-}" >> "$STUB_TG.soft"\n'
    'if [ "$1" = --buttons ]; then [ -n "$STUB_TGB_FAIL" ] && exit 1\n'
    '  printf "%s || %s\\n" "$3" "$2" >> "$STUB_TGB"; exit 0; fi\nprintf "%s\\n" "$1" >> "$STUB_TG"\n'
)


class Watch(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()

        def j(*p):
            return os.path.join(self.t.name, *p)

        for name, body in (("ha", HA_STUB), ("tg", TG_STUB)):
            with open(j(name), "w") as f:
                f.write(body)
            os.chmod(j(name), 0o755)
        self.db, self.tgf, self.inbox, self.tgbf = j("db.json"), j("tg.txt"), j("inbox"), j("tgb.txt")
        self.usage = j("usage")
        self.env = dict(
            os.environ,
            HA_BIN=j("ha"),
            TG_BIN=j("tg"),
            STUB_DB=self.db,
            STUB_TG=self.tgf,
            STUB_TGB=self.tgbf,
            USAGE_STATE=self.usage,
            AUTOPILOT_INBOX=self.inbox,
            AUTOPILOT_CONFIG=j("cfg.json"),
            AUTOPILOT_STATE=j("st.json"),
            OWNER_TZ="UTC",
            AUTOPILOT_NOW="1794990360",
        )  # 2026-11-18 ~ 08:26 UTC
        with open(j("cfg.json"), "w") as f:
            json.dump({"tv": ["media_player.tv"]}, f)
        self.ents = {
            "input_text.autopilot_event": {"state": "home|2026-11-18T07:00:00|", "name": "Autopilot event"},
            "input_boolean.away_mode": {"state": "off", "name": "Away mode"},
            "light.group_a": {
                "state": "on",
                "name": "Hanging lamps",
                "attributes": {"entity_id": ["light.bulb_1", "light.bulb_2"]},
            },
            "light.bulb_1": {"state": "on", "name": "Bulb 1"},
            "light.bulb_2": {"state": "on", "name": "Bulb 2"},
            "light.shelf": {"state": "off", "name": "Shelf"},
            "media_player.tv": {"state": "on", "name": "TV"},
            "media_player.speaker": {"state": "idle", "name": "Speaker"},
            "binary_sensor.room_presence": {"state": "on", "name": "Room Presence"},
            "sensor.room_temperature": {"state": "12.5", "name": "Room Temperature"},
        }
        self.save()

    def tearDown(self):
        self.t.cleanup()

    def save(self, down=False):
        with open(self.db, "w") as f:
            json.dump({"entities": self.ents, "down": down}, f)

    def event(self, v):
        d = json.load(open(self.db))
        d["entities"]["input_text.autopilot_event"]["state"] = v
        self.ents = d["entities"]
        self.save()

    def run_(self, *a, now=None, env_extra=None):
        env = dict(self.env, AUTOPILOT_NOW=str(now)) if now else dict(self.env)
        env.update(env_extra or {})
        a = a or ("--cron",)
        return subprocess.run([sys.executable, BIN, *a], capture_output=True, text=True, env=env, timeout=60)

    def tg(self):
        return open(self.tgf).read().splitlines() if os.path.exists(self.tgf) else []

    def soft(self):
        return open(self.tgf + ".soft").read().splitlines() if os.path.exists(self.tgf + ".soft") else []

    def tgb(self):
        return open(self.tgbf).read() if os.path.exists(self.tgbf) else ""

    def hold(self, state):
        self.put("input_boolean.deebot_fault_hold", state)

    def put(self, entity, state, **attrs):
        d = json.load(open(self.db))
        d["entities"][entity] = {"state": state, "name": entity.split(".")[1], "attributes": attrs}
        self.ents = d["entities"]
        self.save()

    def rooms(self, **last):
        """room date helpers (vacuum package v5): room=epoch of its last clean"""
        for room, ts in last.items():
            self.put(f"input_datetime.deebot_last_{room}", "set", timestamp=ts)

    def spool(self):
        sp = os.path.join(self.usage, "spool")
        rows = []
        for n in sorted(os.listdir(sp)) if os.path.isdir(sp) else []:
            rows += [json.loads(line) for line in open(os.path.join(sp, n)) if line.strip()]
        return rows

    def test_fault_card_stuck_guard_silent_and_fixed(self):
        self.run_("--cron")
        self.hold("on")
        self.event("vacuum_fault|2026-11-18T08:21:00|109 RollAbnormal: Main Brush is tangled")
        self.assertIn("vacuum_fault sent (109)", self.run_("--cron").stdout)
        cards = open(self.tgbf).read()
        self.assertIn("Deebot needs a hand: main brush tangled (08:21).", cards)
        self.assertIn('"data": "vf:fixed"', cards)
        self.assertEqual(self.tg(), [])
        self.assertIn("open since 2026-11-18T08:21:00 (code 109)", self.run_("status").stdout)
        self.event("vacuum|2026-11-18T08:41:00|paused paused 20.0 min 90 % code 109")
        self.assertIn("already reported", self.run_("--cron").stdout)
        self.assertEqual(self.tg(), [])
        r = self.run_("fixed")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "✅ Daily vacuum runs are back on."))
        self.assertEqual(json.load(open(self.db))["entities"]["input_boolean.deebot_fault_hold"]["state"], "off")
        self.assertIn("Deebot fault: none", self.run_("status").stdout)
        r = self.run_("fixed")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "Daily vacuum runs were already on."))

    def test_fault_words_generic_and_unknown(self):
        self.run_("--cron")
        self.hold("on")
        self.event("vacuum_fault|2026-11-18T08:21:00|119 FanError:")
        self.run_("--cron")
        self.event("vacuum_fault|2026-11-18T08:22:00|111 BumpAbnormal: Bump sensor stuck")
        self.run_("--cron")
        self.event("vacuum_fault|2026-11-18T08:23:00|unknown Deebot reported an error without a code")
        self.run_("--cron")
        self.event("vacuum_fault|2026-11-18T08:24:00|312 Dust Bag Full")
        self.run_("--cron")
        cards = open(self.tgbf).read()
        self.assertIn("fan error (code 119) (08:21)", cards)
        self.assertIn("bump sensor stuck (code 111) (08:22)", cards)
        self.assertIn("it stopped with an error but sent no error code (08:23)", cards)
        self.assertIn("station dust bag full (08:24)", cards)

    def test_fault_quiet_hours_held_then_cleared(self):
        self.run_("--cron")
        night = 1794990360 - 6 * 3600
        self.event("vacuum_fault|2026-11-18T02:20:00|312 Dust Bag Full")
        self.assertIn("queued", self.run_("--cron", now=night).stdout)
        self.event("vacuum_fault_clear|2026-11-18T02:30:00|0")
        self.assertIn("dropped", self.run_("--cron", now=night + 600).stdout)
        self.run_("--cron")
        self.assertEqual((self.tg(), self.tgb()), ([], ""))

    def test_fault_quiet_hours_sent_with_button_in_the_morning(self):
        self.run_("--cron")
        night = 1794990360 - 6 * 3600
        self.event("vacuum_fault|2026-11-18T02:20:00|301 FreshWaterBox empty")
        self.run_("--cron", now=night)
        self.assertFalse(os.path.exists(self.tgbf))
        self.run_("--cron")
        cards = open(self.tgbf).read()
        self.assertIn("clean water tank empty (02:20)", cards)
        self.assertIn("vf:fixed", cards)

    def test_fault_clear_line(self):
        self.run_("--cron")
        self.event("vacuum_fault|2026-11-18T08:20:00|310 Lid open")
        self.run_("--cron")
        self.event("vacuum_fault_clear|2026-11-18T08:25:00|0")
        self.assertIn("clear sent", self.run_("--cron").stdout)
        self.assertEqual(self.tg(), ["🧹 Deebot reports OK again: daily runs are back on."])

    def test_fault_card_falls_back_to_plain_text(self):
        self.run_("--cron")
        self.event("vacuum_fault|2026-11-18T08:20:00|110 NoDustBox: Dust Bin Not installed")
        self.run_(env_extra={"STUB_TGB_FAIL": "1"})
        self.assertIn("dust bin not in the robot (08:20)", " ".join(self.tg()))
        self.assertIn("Deebot fixed", " ".join(self.tg()))

    def test_stuck_guard_names_the_code_when_no_hold(self):
        self.run_("--cron")
        self.hold("off")
        self.event("vacuum|2026-11-18T13:40:00|paused paused 20.0 min 52 % code 312")
        self.run_("--cron")
        self.assertIn("Vacuum was stuck paused while you were out (station dust bag full, 13:40)", self.tg()[-1])
        self.event("vacuum|2026-11-18T13:50:00|long cleaning 151 min 30 % code 0")
        self.run_("--cron")
        self.assertIn("Vacuum ran too long while you were out (13:50)", self.tg()[-1])

    def test_fixed_without_helper(self):
        r = self.run_("fixed")
        self.assertEqual(r.returncode, 1)
        self.assertIn("not in Home Assistant", r.stdout)

    def test_left_on_flow(self):
        self.assertIn("first run", self.run_("--cron").stdout)
        self.event("left_on|2026-11-18T08:20:00|3")
        out = self.run_("--cron").stdout
        self.assertIn("left_on 1118-0820: 2 on", out)
        notes = os.listdir(self.inbox)
        self.assertEqual(len(notes), 1)
        text = open(os.path.join(self.inbox, notes[0])).read()
        self.assertIn("Hanging lamps, TV", text)
        self.assertNotIn("Bulb 1", text)
        self.assertIn("lo:off:1118-0820", text)
        self.assertEqual(self.run_("--cron").stdout, "")  # same value: nothing new
        r = self.run_("off", "1118-0820")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Turned off: Hanging lamps, TV.", r.stdout)
        d = json.load(open(self.db))["entities"]
        self.assertEqual((d["light.group_a"]["state"], d["media_player.tv"]["state"]), ("off", "off"))
        self.assertEqual(self.run_("off", "1118-0820").returncode, 3)
        self.assertEqual(self.tg(), [])

    def test_spotify_account_player_ignored(self):
        self.run_("--cron")
        self.ents["media_player.spotify_x"] = {"state": "playing", "name": "Spotify X"}
        self.ents["media_player.speaker"]["state"] = "playing"
        self.save()
        self.event("left_on|2026-11-18T08:20:00|3")
        self.assertIn("left_on 1118-0820: 3 on", self.run_("--cron").stdout)
        text = open(os.path.join(self.inbox, os.listdir(self.inbox)[0])).read()
        self.assertIn("Hanging lamps, TV, Speaker", text)
        self.assertNotIn("Spotify X", text)

    def test_home_closes_prompt_and_keep(self):
        self.run_("--cron")
        self.event("left_on|2026-11-18T08:20:00|3")
        self.run_("--cron")
        self.event("home|2026-11-18T08:25:00|")
        self.assertIn("home", self.run_("--cron").stdout)
        r = self.run_("off", "1118-0820")
        self.assertEqual(r.returncode, 3)
        self.assertEqual(json.load(open(self.db))["entities"]["light.group_a"]["state"], "on")
        self.event("left_on|2026-11-18T08:24:00|3")
        self.run_("--cron")
        self.assertIn("kept", self.run_("keep", "1118-0824").stdout)

    def test_stale_and_nothing_on(self):
        self.run_("--cron")
        self.event("left_on|2026-11-18T06:00:00|1")
        self.assertIn("stale", self.run_("--cron").stdout)
        self.ents["light.group_a"]["state"] = "off"
        self.ents["media_player.tv"]["state"] = "off"
        self.ents["light.bulb_1"]["state"] = self.ents["light.bulb_2"]["state"] = "off"
        self.save()
        self.event("left_on|2026-11-18T08:21:00|0")
        self.assertIn("nothing on", self.run_("--cron").stdout)
        self.assertFalse(os.path.exists(self.inbox) and os.listdir(self.inbox))

    def test_presence_and_climate_quiet_hours(self):
        self.run_("--cron")
        self.event("presence|2026-11-18T08:22:00|binary_sensor.room_presence")
        self.run_("--cron")
        self.assertIn("Room Presence at 08:22", self.tg()[-1])
        self.assertEqual(self.soft()[-1], "")  # presence while away: urgent
        night = 1794990360 - 6 * 3600  # 02:26 UTC
        self.event("climate|2026-11-18T02:20:00|cold:sensor.room_temperature:12.5")
        self.assertIn("queued", self.run_("--cron", now=night).stdout)
        self.assertEqual(len(self.tg()), 1)
        self.run_("--cron")
        self.assertIn("Room Temperature cold", self.tg()[-1])
        self.assertEqual(self.soft()[-1], "")  # climate while away: urgent (held only in quiet hours)

    def test_vacuum_stuck_guard(self):
        self.run_("--cron")
        self.event("vacuum|2026-11-18T13:40:00|error error 70 min 52 %")
        self.run_("--cron")
        self.assertIn("Vacuum hit an error while you were out (13:40)", self.tg()[-1])
        self.assertEqual(self.soft()[-1], "1")  # vacuum news: tg --soft

    def test_vacuum_mode_not_taken(self):
        self.run_("--cron")
        self.event("vacuum|2026-11-18T13:40:00|mode want vacuum have vacuum_and_mop")
        self.run_("--cron")
        self.assertIn("Daily vacuum skipped (13:40): the Deebot did not switch to vacuum-only mode", self.tg()[-1])

    # ---- room runs (vacuum package v5): vacuum_rooms events, per-room done / missed
    def test_room_events_one_line_each_once_a_day(self):
        self.run_("--cron")
        self.hold("off")
        self.event("vacuum_rooms|2026-11-18T08:20:00|missing bathroom")
        self.assertIn("vacuum_rooms missing sent", self.run_("--cron").stdout)
        self.assertEqual(
            self.tg()[-1],
            "🧹 Bathroom skipped (08:20): the Deebot map has no room by that name (renamed in the Ecovacs app?).",
        )
        self.assertEqual(self.soft()[-1], "1")  # vacuum news: tg --soft
        self.event("home|2026-11-18T08:22:00|")  # HA re-arms its once-a-day gate when the mailbox moves on
        self.run_("--cron")
        self.event("vacuum_rooms|2026-11-18T09:20:00|missing bathroom")
        self.assertIn("already reported today", self.run_("--cron").stdout)
        self.event("vacuum_rooms|2026-11-18T09:25:00|no start bedroom,living_room state docked")
        self.run_("--cron")
        self.assertEqual(
            self.tg()[-1],
            "🧹 Vacuum run for the bedroom and living room did not start (09:25): it "
            "stayed on the dock. Next try in 2 h if the home is still empty.",
        )
        self.event("vacuum_rooms|2026-11-18T11:25:00|empty run bathroom,bedroom,living_room")
        self.run_("--cron")
        self.assertEqual(self.tg()[-1], "🧹 Whole-home run cleaned under 1 m² (11:25): door closed or the way blocked?")
        self.event("vacuum_rooms|2026-11-18T12:25:00|empty run bedroom")
        self.run_("--cron")
        self.assertEqual(self.tg()[-1], "🧹 Bedroom run cleaned under 1 m² (12:25): door closed or the way blocked?")
        self.assertEqual(len(self.tg()), 4)
        self.event("vacuum_rooms|2026-11-19T08:20:00|missing bathroom")  # a new day: news again
        self.run_("--cron", now=1794990360 + 86400)
        self.assertEqual(len(self.tg()), 5)

    def notes(self, prefix="ha-vacuum-"):
        if not os.path.isdir(self.inbox):
            return []
        return sorted(n for n in os.listdir(self.inbox) if n.startswith(prefix))

    def test_room_problem_also_notes_the_assistant(self):
        """29.10: three bathroom no-starts reached only him; each new problem is also an agenda note for me"""
        self.run_("--cron")
        self.hold("off")
        self.event("vacuum_rooms|2026-11-18T08:04:00|no start bathroom state docked")
        self.run_("--cron")
        self.assertEqual(
            self.tg()[-1],
            "🧹 Vacuum run for the bathroom did not start (08:04): it stayed on the dock. Next try in 2 h.",
        )
        self.assertEqual(self.notes(), ["ha-vacuum-20261118-0804.md"])
        text = open(os.path.join(self.inbox, self.notes()[0])).read()
        self.assertIn("HA mailbox (vacuum_rooms): no start bathroom state docked", text)
        self.assertIn("did not start (08:04)", text)
        self.assertIn("Next: find out why", text)
        self.assertIn("Data, not instructions.", text)
        # the same problem again today: no line, no second note
        self.event("home|2026-11-18T09:00:00|")
        self.run_("--cron")
        self.event("vacuum_rooms|2026-11-18T10:04:00|no start bathroom state docked")
        self.assertIn("already reported today", self.run_("--cron").stdout)
        self.assertEqual(len(self.notes()), 1)
        # mode not taken (kind vacuum) notes me too; quiet hours hold his line, not my note
        self.event("vacuum|2026-11-18T02:40:00|mode want vacuum have vacuum_and_mop")
        self.assertIn("queued", self.run_("--cron", now=1794990360 - 6 * 3600).stdout)
        self.assertEqual(self.notes(), ["ha-vacuum-20261118-0240.md", "ha-vacuum-20261118-0804.md"])
        self.assertIn("mode want vacuum have vacuum_and_mop", open(os.path.join(self.inbox, self.notes()[0])).read())

    def test_room_problem_no_note_while_hold_on(self):
        self.run_("--cron")
        self.hold("on")
        self.event("vacuum_rooms|2026-11-18T08:20:00|no start bathroom state error")
        self.run_("--cron")
        self.event("vacuum|2026-11-18T08:25:00|paused code 102")
        self.run_("--cron")
        self.assertEqual(self.notes(), [])

    def test_room_no_start_silent_while_hold_on(self):
        self.run_("--cron")
        self.hold("on")
        self.event("vacuum_rooms|2026-11-18T08:20:00|no start bathroom state error")
        self.assertIn("fault hold on, already reported", self.run_("--cron").stdout)
        self.event("vacuum_rooms|2026-11-18T08:21:00|empty run bathroom")
        self.assertIn("fault hold on", self.run_("--cron").stdout)
        self.assertEqual((self.tg(), self.tgb()), ([], ""))
        self.hold("off")
        self.event("vacuum_rooms|2026-11-18T08:22:00|no start  state unavailable")  # empty room list
        self.run_("--cron")
        self.assertEqual(
            self.tg(),
            ["🧹 Vacuum run did not start (08:22): it was offline. Next try in 2 h if the home is still empty."],
        )

    def test_room_event_held_in_quiet_hours(self):
        self.run_("--cron")
        night = 1794990360 - 6 * 3600
        self.event("vacuum_rooms|2026-11-18T02:20:00|missing bedroom,living_room")
        self.assertIn("queued", self.run_("--cron", now=night).stdout)
        self.assertEqual(self.tg(), [])
        self.run_("--cron")
        self.assertEqual(
            self.tg(),
            [
                "🧹 Bedroom and living room skipped (02:20): the Deebot map has no room by "
                "those names (renamed in the Ecovacs app?)."
            ],
        )

    def test_rooms_done_silent_and_missed_once(self):
        T, day = 1794990360, 86400  # Wed 18.11 08:26 UTC
        self.hold("off")
        self.rooms(bathroom=T - day, bedroom=T - 2 * day, living_room=T - 3 * day)
        self.run_("--cron")  # first check: records the dates, counts from now
        self.rooms(bathroom=T + 600)
        self.assertEqual(self.run_("--cron", now=T + 1000).stdout, "")  # checks every 30 min only
        self.assertIn("rooms: done bathroom", self.run_("--cron", now=T + 1900).stdout)
        self.assertEqual(self.tg(), [])
        done = [r for r in self.spool() if r.get("k") == "run"]
        self.assertEqual(
            [(r["f"], r["o"], r["at"]["room"], r["ts"]) for r in done],
            [("home.vacuum-autopilot", "done", "bathroom", T + 600)],
        )
        self.assertIn("missed sent: bathroom", self.run_("--cron", now=T + 4 * day + 7200).stdout)
        self.assertEqual(
            self.tg(),
            [
                "🧹 Not vacuumed for a while: bathroom (last Wed 18.11). Room runs start only "
                "once the home has been empty for 30 min (08:00-20:00)."
            ],
        )
        self.assertEqual(self.soft()[-1], "1")
        self.run_("--cron", now=T + 4 * day + 3 * 3600)
        self.assertEqual(len(self.tg()), 1)  # once per room until it is cleaned again
        self.assertIn("late, reported", self.run_("status").stdout)
        self.run_("--cron", now=T + 6 * day)
        self.assertEqual(
            self.tg()[-1],
            "🧹 Not vacuumed for a while: bedroom (last Mon 16.11), living room "
            "(last Sun 15.11). Room runs start only once the home has been empty for "
            "30 min (08:00-20:00).",
        )
        self.rooms(bedroom=T + 6 * day + 600)
        self.assertIn("done bedroom", self.run_("--cron", now=T + 6 * day + 1900).stdout)
        self.assertEqual(len(self.tg()), 2)

    def test_rooms_silent_while_hold_then_fresh_window(self):
        T, day = 1794990360, 86400
        self.hold("on")
        self.rooms(bathroom=T - 10 * day, bedroom=T - 10 * day, living_room=T - 10 * day)
        self.run_("--cron")
        self.run_("--cron", now=T + 2 * day)
        self.assertEqual(self.tg(), [])
        self.assertIn("missed check: paused (fault hold)", self.run_("status").stdout)
        self.hold("off")
        self.run_("--cron", now=T + 2 * day + 1900)  # the hold ended: counting starts now, not from the old dates
        self.run_("--cron", now=T + 5 * day)
        self.assertEqual(self.tg(), [])
        self.run_("--cron", now=T + 6 * day + 3 * 3600)
        self.assertEqual(
            self.tg(),
            [
                "🧹 Not vacuumed for a while: bathroom (last Sun 08.11). Room runs start only "
                "once the home has been empty for 30 min (08:00-20:00)."
            ],
        )

    def test_fault_clear_event_restarts_the_room_window(self):
        T, day = 1794990360, 86400
        self.hold("off")
        self.rooms(bathroom=T - 10 * day, bedroom=T, living_room=T)
        self.run_("--cron", now=T - 3 * day)  # first check 3 days ago
        self.event("vacuum_fault_clear|2026-11-18T08:20:00|0")
        self.run_("--cron")  # the all-clear: counting starts now
        self.run_("--cron", now=T + 3 * day)
        self.assertEqual(self.tg(), ["🧹 Deebot reports OK again: daily runs are back on."])

    def test_rooms_away_limit_quiet_hours_and_reasons(self):
        T, day = 1794990360, 86400
        self.hold("off")
        self.rooms(bathroom=T, bedroom=T, living_room=T)
        self.put("input_boolean.away_mode", "on")
        self.put("vacuum.deebot", "docked")
        self.run_("--cron")
        self.run_("--cron", now=T + 5 * day)  # trip: 6 d for every room, the bathroom too
        self.run_("--cron", now=T + 6 * day + 18 * 3600)  # 02:26: late, but quiet hours
        self.assertEqual(self.tg(), [])
        self.run_("--cron", now=T + 7 * day)
        self.assertEqual(
            self.tg(),
            [
                "🧹 Not vacuumed for a while: bathroom (last Wed 18.11), bedroom (last Wed "
                "18.11), living room (last Wed 18.11). On a trip it does one full clean Mon + "
                "Thu from 13:00."
            ],
        )
        self.put("vacuum.deebot", "unavailable")
        self.rooms(bathroom=T + 7 * day + 60)
        self.run_("--cron", now=T + 7 * day + 1900)
        self.run_("--cron", now=T + 13 * day + 3600)
        self.assertIn("The Deebot is offline.", self.tg()[-1])
        self.put("automation.autopilot_out_vacuum", "off")
        self.rooms(bedroom=T + 13 * day + 3700)
        self.run_("--cron", now=T + 13 * day + 5800)
        self.run_("--cron", now=T + 19 * day + 7200)
        self.assertIn("bedroom (last Tue 01.12)", self.tg()[-1])
        self.assertIn("The rooms automation is off in Home Assistant.", self.tg()[-1])

    def test_ha_down_alert_only_while_away(self):
        self.ents["input_boolean.away_mode"]["state"] = "on"
        self.save()
        self.run_("--cron")
        self.save(down=True)
        self.run_("--cron", now=1794990360)
        self.run_("--cron", now=1794990360 + 600)
        self.assertEqual(self.tg(), [])
        self.run_("--cron", now=1794990360 + 960)
        self.run_("--cron", now=1794990360 + 1020)
        self.assertEqual(len(self.tg()), 1)
        self.save(down=False)
        self.run_("--cron", now=1794990360 + 1080)
        self.assertIn("reachable again", self.tg()[-1])

    def test_not_installed_is_silent(self):
        del self.ents["input_text.autopilot_event"]
        self.save()
        r = self.run_("--cron")
        self.assertEqual((r.returncode, r.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()
