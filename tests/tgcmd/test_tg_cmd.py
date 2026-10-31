# ruff: noqa: E501  (fixture data and expected cards copied verbatim; long strings)
"""tg-cmd's instant commands as text cards: the command table, /home, /list, /shop, /parcels, /gym, /train, /rave,
/trip, /warranty, /reminders, /back, /pack (+ `pack --todo`), /week, /gohome, /vibe, /finish, /doctor, /crate,
/party, /cat. Picture cards (TG_CMD_PICTURES=1) and cardimg: test_tg_cmd_pictures.py.

Isolated: HOME is a temp dir, the repo's tg-cmd and pack are staged with their lib modules (tgcmd_testlib.stage),
exec records go to a scratch usage store, every source is a fake script in TG_CMD_BIN, clocks are pinned
(TG_CMD_NOW). The /home fixtures in fixtures/ and fixtures/back are placeholders; all data is made up."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tgcmd_testlib import FIX, WEEK_TEXT, Box, Week, cat_history, nlines, script  # noqa: E402

NOW = "2031-03-05 12:00"


@pytest.fixture
def box(tmp_path: Path) -> Box:
    return Box(tmp_path)


@pytest.fixture
def fb(tmp_path: Path) -> Path:
    """The fake-tool dir (TG_CMD_BIN) and its call log."""
    d = tmp_path / "fakes"
    d.mkdir()
    return d


# --------------------------------------------------------------------------- command table


ORDER = (
    "/bills /queue /space /reminders /home /list /shop /parcels /gym /cat /train /gohome /rave /trip /warranty /back "
    "/week /pack /vibe /finish /doctor /party /crate /usage /sleep "
)


def test_command_table(box):
    r = box.run("--list")
    assert r.rc == 0
    assert "".join(ln.split()[0] + " " for ln in r.out.splitlines() if ln.strip()) == ORDER
    for c in ("gohome", "trip", "warranty", "back", "pack", "vibe", "party", "doctor", "gym", "parcels"):
        assert "take one argument at most" in box.run(c, "a", "b", merge=True).out, c
    assert "take no arguments" in box.run("bills", "a", merge=True).out
    # exec records land in the scratch usage store, not the live one
    assert sum('"f":"tg.gohome"' in ln for ln in box.usage_lines()) == 1
    lst = box.run("--list").out
    for want in (
        "/reminders  Your upcoming reminders",
        "/home       Home: lights, temperatures, media, HA health",
        "/shop       Shopping list: what to buy where",
        "/gym        Next gym session from your plan",
        "/train      Next trains with live status",
        "/gohome     Way home: next 3 connections + the last tonight (/gohome stop)",
        "/trip       Next trip: dates, legs, open to-dos",
        "/warranty   Still under warranty? (/warranty tv filters)",
        "/back       Catch-up after time away (/back 3 = 3 days)",
        "/pack       Packing list for the next event (/pack rave)",
        "/vibe       4 movies from your library for a mood",
        "/finish     Shows you started",
        "/doctor     Why a Seerr request",
        "/party      Party lights: WLEDs follow the music (/party off restores)",
        "/week       Your week: calendar, trains, weather, late nights",
        "/cat        Cat week: litter visits, weight, feeder",
    ):
        assert want in lst, want
    assert sum(ln.startswith("/") for ln in lst.splitlines()) == 25  # /cat listed, in the menu only with pictures on
    for gone in ("/emby ", "/dance", "/practice", "/memory", "/recall"):
        assert gone not in lst, gone
    assert "/back /week " in "".join(ln.split()[0] + " " for ln in lst.splitlines() if ln.strip())
    # the picture flag changes nothing in the table
    assert box.run("--list", env={"TG_CMD_PICTURES": "1"}).out == lst


def test_tg_bind_rows_name_tg_cmd_commands(box):
    tgbind = os.environ.get("TG_BIND_BIN")
    if not tgbind:
        pytest.skip("tg-bind rows match tg-cmd: the rows live on the hub (set TG_BIND_BIN to check one)")
    rows = subprocess.run([tgbind, "list", "--json"], capture_output=True, text=True, timeout=60).stdout
    known = {ln.split()[0] for ln in box.run("--list").out.splitlines() if ln.strip()}
    bad = [
        r["tool"]
        for r in json.loads(rows)
        if r["kind"] == "command" and r["tool"].startswith("tg-cmd.") and "/" + r["tool"][7:] not in known
    ]
    assert bad == []


def test_retired_and_bad_commands(box):
    assert box.run("memory").rc == 2
    for c in ("emby", "dance", "practice"):
        assert box.run(c).rc == 2, c
    assert box.run("recall", "paperless").rc == 2
    assert box.run("bogus").rc == 2
    r = box.run("bills", "extra")
    assert r.rc == 2
    assert "take no arguments" in r.err
    assert box.run("--help").rc == 0


# --------------------------------------------------------------------------- /home

FAKE_HA = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_HA_LOG"
if [ -n "${FAKE_HA_ERR:-}" ]; then echo "ha: $FAKE_HA_ERR" >&2; exit 1; fi
cat "$FAKE_HA_JSON"
"""

HOME_FULL = """🏠 Home · 12:00
💡 3 lights on · Kitchen 2, Study 1
🌡 Kitchen 20.4° · Lounge 21.5°→22°🔥 · Study 19.0° · Terrace 8.3°
🧹 Vacuum: docked · 5%
🎵 Playing: Speaker One (Radio App) · Tablet (mail me at <email>) · TV Box on (HDMI 1)
🩺 HA health: 4 open (1 new) · 2 known offline
↳ Study Desk Lamp 🆕, Study Old Sensor, select.extra_mode, +1 more
🔋 Low: Window low, Door Sensor 9%"""


@pytest.fixture
def home(box, fb):
    script(fb / "ha", FAKE_HA)
    calls = fb / "calls"

    def run(fixture, health, extra=None, args=()):
        calls.write_text("")
        env = dict(
            TG_CMD_BIN=fb,
            TG_CMD_NOW=NOW,
            FAKE_HA_LOG=calls,
            FAKE_HA_JSON=fixture,
            HA_HEALTH_DIR=health,
            **(extra or {}),
        )
        return box.run("home", *args, env=env)

    run.calls = calls
    return run


def test_home_full_and_damp_line(home, fb):
    r = home(FIX / "home-full.json", FIX / "health-new")
    assert r.rc == 0
    assert r.out == HOME_FULL
    assert nlines(r.out) <= 10
    assert home.calls.read_text().rstrip("\n") == "list --json --limit 5000"  # one ha read, nothing else
    script(
        fb / "damp-watch",
        '#!/usr/bin/env bash\n[ "$1" = line ] && echo "💧 ⚠️ Lounge humid 26 h (RH 68 %) · air 5–10 min"\n',
    )
    out2 = home(FIX / "home-full.json", FIX / "health-new").out
    assert "Terrace 8.3°\n💧 ⚠️ Lounge humid 26 h (RH 68 %) · air 5–10 min\n🧹" in out2
    assert nlines(out2) <= 10
    assert home.calls.read_text().rstrip("\n") == "list --json --limit 5000"  # damp-watch adds no ha read
    for bad in (
        "Test Person", "Workplace", "not_home", "Tracker", "Proximity", "@example.com", "Phone One", "3%",
        "Robo Vac Battery", "Fridge", "CPU", "55.0", "Loose", "25.0", "Speaker Two", "Idle Speaker", "String Lights",
        "Kitchen Lights",
    ):  # fmt: skip
        assert bad not in r.out, bad


def test_home_states(home, tmp_path):
    r = home(FIX / "home-empty.json", FIX / "health-clean")
    assert r.rc == 0
    assert (
        r.out
        == "🏠 Home · 12:00\n💡 All lights off\n🎵 Nothing playing\n🩺 HA health: all ok · 2 known offline\n🔋 Batteries ok"
    )
    out = home(FIX / "home-full.json", FIX / "health-base").out
    assert "🩺 HA health: 4 open · 2 known offline" in out
    assert "🆕" not in out
    out = home(FIX / "home-full.json", FIX / "health-old").out
    assert "🩺 HA health: 4 open · 2 known offline (snapshot 4 d old) · watch failing" in out
    out = home(FIX / "home-full.json", tmp_path / "no-such-dir").out
    assert "🩺 HA health: no ha-health-watch snapshot yet" in out
    assert "💡 3 lights on" in out
    r = home(FIX / "home-full.json", FIX / "health-new", {"FAKE_HA_ERR": "HA unreachable at http://198.51.100.7:8123"})
    assert r.rc == 3
    assert "⚠️ /home failed: Home Assistant" in r.out
    assert "what's on at home?" in r.out
    assert "198.51.100.7" not in r.out  # IP masked
    (tmp_path / "garbage.json").write_text("not json")
    r = home(tmp_path / "garbage.json", FIX / "health-new")
    assert r.rc == 3
    assert "unreadable answer" in r.out
    (tmp_path / "obj.json").write_text('{"a": 1}')
    assert home(tmp_path / "obj.json", FIX / "health-new").rc == 3
    assert home(FIX / "home-full.json", FIX / "health-new", args=("extra",)).rc == 2


def test_home_panel_env_keeps_the_plain_card(home, tmp_path):
    panel = script(tmp_path / "kp" / "home-panel", '#!/bin/sh\necho "{\\"text\\": \\"panel $*\\"}"\n')
    full, new = FIX / "home-full.json", FIX / "health-new"
    out = home(full, new, {"TELEGRAM_INSTANT_KEYBOARD": "1", "TG_CMD_HOME_PANEL": panel}).out
    assert "🏠 Home · 12:00" in out  # panel retired
    assert "🏠 Home · 12:00" in home(full, new, {"TG_CMD_HOME_PANEL": panel}).out
    out = home(full, new, {"TELEGRAM_INSTANT_KEYBOARD": "1", "TG_CMD_HOME_PANEL": tmp_path / "kp" / "missing"}).out
    assert "🏠 Home · 12:00" in out


# --------------------------------------------------------------------------- /list, /shop, /parcels

FAKE_LISTS = """#!/usr/bin/env bash
printf 'lists %s\\n' "$*" >> "$FAKE_LOG"
if [ -n "${FAKE_LISTS_ERR:-}" ]; then echo "lists: $FAKE_LISTS_ERR" >&2; exit 1; fi
printf '%s' "${FAKE_LISTS_OUT-(no lists)}"; echo
"""
FAKE_SHOP = """#!/usr/bin/env bash
printf 'shop %s\\n' "$*" >> "$FAKE_LOG"
case "$1" in
  list) [ -n "${FAKE_SHOP_STALE:-}" ] && echo "shop: HA not reachable, showing the last state (ha todo items: timeout)" >&2
        if [ -n "${FAKE_SHOP_ERR:-}" ]; then echo "shop: $FAKE_SHOP_ERR" >&2; exit 2; fi
        printf '%s\\n' "${FAKE_SHOP_JSON-[]}" ;;
  sync) if [ -n "${FAKE_SYNC_ERR:-}" ]; then echo "shop: ha todo items: $FAKE_SYNC_ERR" >&2; exit 1; fi
        echo '{"new": []}' ;;
  card) if [ -n "${FAKE_CARD_ERR:-}" ]; then echo "shop: $FAKE_CARD_ERR" >&2; exit 1; fi
        printf '%s' "${FAKE_CARD-}" ;;
  *) exit 2 ;;
esac
"""
FAKE_PARCELS = """#!/bin/sh
printf 'parcels %s\\n' "$*" >> "$FAKE_LOG"
case "$1" in
  line) [ -n "${FAKE_PARCELS_LINE:-}" ] && echo "$FAKE_PARCELS_LINE"; exit 0;;
  picked) echo "✅ Marked 1 parcel as picked up; pickup reminder cancelled.";;
  "") [ -n "${FAKE_PARCELS_ERR:-}" ] && { echo "boom" >&2; exit 1; }; printf '📦 Parcels · as of 09:00\\n🧾 Testshop: Fri 13.3 · Test Pot\\n';;
esac
"""
TWO = (
    '[{"id": 1, "text": "Oat milk", "qty": 2, "status": "pending"}, '
    '{"id": 4, "text": "Batteries AA", "qty": 1, "status": "researched"}]'
)
CARD = "🛒 Shopping list · 2 to buy\n\n📦 Online\nShopA · CHF 12.50\n  2× Oat milk, Batteries AA"


@pytest.fixture
def tcmd(box, fb):
    """`tcmd` of run.sh: TG_CMD_BIN = the fakes, a fresh call log per run."""
    log = fb / "log"

    def run(*args, **env):
        log.write_text("")
        return box.run(*args, env=dict(TG_CMD_BIN=fb, FAKE_LOG=log, **env))

    run.log = lambda: log.read_text().rstrip("\n")
    return run


def test_list(tcmd, fb):
    script(fb / "lists", FAKE_LISTS)
    script(fb / "shop", FAKE_SHOP)
    r = tcmd(
        "list",
        FAKE_SHOP_JSON=TWO,
        FAKE_LISTS_OUT="🛒 Shopping (1)\n1. Old milk\n\n☑️ Todo (1)\n1. Call the plumber\n✓ Pay rent",
    )
    assert r.rc == 0
    assert r.out == (
        "🛒 Shopping (2)\n1. 2× Oat milk\n2. Batteries AA\n→ /shop: where to buy\n"
        "🗒️ +1 on my old local shopping list (ask me to move them)\n\n☑️ Todo (1)\n1. Call the plumber\n✓ Pay rent"
    )
    assert tcmd.log() == "shop list --json\nlists show"  # one synced shop read + one lists read
    out = tcmd("list", FAKE_SHOP_JSON="[]", FAKE_LISTS_OUT="🛒 Shopping: empty\n\n🗒️ Packing (1)\n1. Charger").out
    assert out == "🛒 Shopping list is empty\n\n🗒️ Packing (1)\n1. Charger"
    assert tcmd("list", FAKE_SHOP_JSON="[]").out == "🛒 Shopping list is empty"
    r = tcmd("list", FAKE_SHOP_STALE="1", FAKE_SHOP_JSON=TWO)
    assert r.rc == 0
    assert "2. Batteries AA\n⚠️ HA not reachable: last synced list" in r.out
    r = tcmd("list", FAKE_SHOP_ERR="state file unreadable", FAKE_LISTS_OUT="☑️ Todo (1)\n1. Call the plumber")
    assert r.rc == 0
    assert r.out == "🛒 Shopping: ⚠️ couldn't read it (state file unreadable)\n\n☑️ Todo (1)\n1. Call the plumber"
    r = tcmd("list", FAKE_SHOP_JSON=TWO, FAKE_LISTS_ERR="lock busy")
    assert r.rc == 0
    assert "→ /shop: where to buy\n\n⚠️ Other lists: lists: lock busy" in r.out
    out = tcmd("list", FAKE_SHOP_JSON="not json", FAKE_LISTS_OUT="☑️ Todo (1)\n1. Call the plumber").out
    assert "🛒 Shopping: ⚠️ couldn't read it (unreadable answer)" in out
    r = tcmd("list", FAKE_SHOP_JSON="not json", FAKE_LISTS_ERR="lock busy")
    assert r.rc == 3
    assert "⚠️ /list failed: shopping list: unreadable answer; lists: lock busy." in r.out
    (fb / "shop").rename(fb / "shop.off")
    r = tcmd("list", FAKE_LISTS_ERR="lock busy")
    assert r.rc == 3
    assert "what's on my lists?" in r.out


def test_shop(tcmd, fb):
    script(fb / "shop", FAKE_SHOP)
    r = tcmd("shop", FAKE_CARD=CARD)
    assert (r.rc, r.out) == (0, CARD)
    assert tcmd.log() == "shop sync --json\nshop card"  # sync, then the text card (no --json)
    assert tcmd("shop", FAKE_CARD="🛒 Shopping list is empty").out == "🛒 Shopping list is empty"
    assert tcmd("shop", FAKE_CARD="").out == "🛒 Shopping list is empty"
    r = tcmd("shop", FAKE_SYNC_ERR="timeout", FAKE_CARD=CARD)
    assert r.rc == 0
    assert r.out == CARD + "\n\n⚠️ HA not reachable: showing the last synced list"
    r = tcmd("shop", FAKE_CARD_ERR="state file unreadable")
    assert r.rc == 3
    assert "⚠️ /shop failed: shopping list: state file unreadable." in r.out
    assert "what's on my shopping list?" in r.out


def test_parcels_and_its_shop_line(tcmd, fb):
    script(fb / "shop", FAKE_SHOP)
    script(fb / "parcels", FAKE_PARCELS)
    assert tcmd("shop", FAKE_CARD=CARD).out == CARD  # no parcels line when none are coming
    out = tcmd("shop", FAKE_CARD=CARD, FAKE_PARCELS_LINE="📦 2 on the way → /parcels").out
    assert out == CARD + "\n\n📦 2 on the way → /parcels"
    r = tcmd("parcels")
    assert r.rc == 0
    assert r.out == "📦 Parcels · as of 09:00\n🧾 Testshop: Fri 13.3 · Test Pot"
    assert tcmd("parcels", "picked").out == (
        "✅ Marked 1 parcel as picked up; pickup reminder cancelled.\n\n📦 Parcels · as of 09:00\n🧾 Testshop: Fri 13.3 · Test Pot"
    )
    assert tcmd.log().splitlines()[0] == "parcels picked station"
    assert "/parcels takes one word: picked" in tcmd("parcels", "whatever").out
    r = tcmd("parcels", FAKE_PARCELS_ERR="1")
    assert r.rc == 3
    assert "where are my parcels?" in r.out


# --------------------------------------------------------------------------- /gym

FAKE_GYM = """#!/usr/bin/env bash
printf 'gym %s\\n' "$*" >> "$FAKE_LOG"
if [ -n "${FAKE_GYM_ERR:-}" ]; then echo "gym: $FAKE_GYM_ERR" >&2; exit 1; fi
printf '%s\\n' "${FAKE_GYM_OUT-}"
"""


def test_gym(tcmd, fb):
    script(fb / "gym", FAKE_GYM)
    gcard = "🏋️ Next: B · Pull (~45 min)\nLast session 01.03. (A, 4 d ago) · streak 3 wk\n\n1  Row  3×8–12 @ 50 kg"
    r = tcmd("gym", FAKE_GYM_OUT=gcard)
    assert (r.rc, r.out) == (0, gcard)
    assert tcmd.log() == "gym plan next"  # exactly one local 'gym plan next' (no sync)
    out = tcmd(
        "gym", FAKE_GYM_OUT="🏋️ Gym · no plan yet\n6 sessions in 4 wk (1.5/wk) · streak 3 wk · last 01.03. (4 d ago)"
    ).out
    assert "streak 3 wk · last 01.03." in out
    assert tcmd("gym", FAKE_GYM_OUT="").out == "🏋️ Gym · nothing logged yet"
    r = tcmd("gym", FAKE_GYM_ERR="database is locked")
    assert r.rc == 3
    assert "⚠️ /gym failed: gym: database is locked." in r.out
    assert "what's my next gym session?" in r.out
    tcmd("gym", "recap", FAKE_GYM_OUT="🏋️ Recap · A · Squat day")
    assert tcmd.log() == "gym recap"
    out = tcmd("gym", "next").out
    assert "/gym recap = last session" in out  # the retired coach word: the hint
    assert tcmd.log() == ""


# --------------------------------------------------------------------------- /train

FAKE_TW = """#!/usr/bin/env bash
printf 'train-watch %s\\n' "$*" >> "$FAKE_LOG"
case "$1" in
  list) if [ -n "${FAKE_TW_ERR:-}" ]; then echo "train-watch: $FAKE_TW_ERR" >&2; exit 1; fi; cat "$FAKE_TW_LEGS" ;;
  status) [ "${FAKE_TW_SLOW:-}" = "$2" ] && sleep 15
          [ -f "$FAKE_TW_DIR/$2.json" ] || { echo "train-watch: transport API unreachable" >&2; exit 1; }
          cat "$FAKE_TW_DIR/$2.json" ;;
  *) exit 2 ;;
esac
"""
LEGS = """[{"id": "a5", "from": "home", "to": "Basel SBB", "dep": "2031-03-05T08:00:00+01:00", "arr": "2031-03-05T09:00:00+01:00", "lines": ["IR36"], "title": "🚆 Home → Basel SBB"},
 {"id": "a2", "from": "Basel SBB", "to": "home", "dep": "2031-03-06T01:40:00+01:00", "arr": "2031-03-06T02:50:00+01:00", "lines": ["SN3"], "title": "🚆 Basel SBB → Home (after Artist One)"},
 {"id": "a1", "from": "home", "to": "Basel SBB", "dep": "2031-03-05T13:10:00+01:00", "arr": "2031-03-05T14:05:00+01:00", "lines": ["IR36"], "title": "🚆 Home → Basel SBB (Artist One)"},
 {"id": "a3", "from": "home", "to": "Lausanne", "dep": "2031-03-05T16:00:00+01:00", "arr": "2031-03-05T18:30:00+01:00", "lines": ["IC5", "IR15"], "title": "🚆 Home → Lausanne"},
 {"id": "a4", "from": "Lausanne", "to": "home", "dep": "2031-03-08T09:00:00+01:00", "arr": "2031-03-08T11:30:00+01:00", "lines": ["IC5"], "title": "🚆 Lausanne → Home (after Club Night)"},
 {"id": "a6", "from": "home", "to": "Bern", "dep": "2031-03-20T10:00:00+01:00", "arr": "2031-03-20T11:00:00+01:00", "lines": [], "title": "🚆 Home → Bern"}]
"""  # noqa: E501
STATUS = {
    "a1": '{"fine": true, "live": {"dep": "13:10", "arr": "14:05", "delay": 0, "platform": "7"}, "problems": [], "alt": null}',
    "a3": '{"fine": false, "live": {"dep": "16:06", "arr": "18:36", "delay": 6, "platform": "3"}, "problems": [{"key": "delay5", "text": "delayed"}], "alt": null}',  # noqa: E501
    "a2": '{"fine": false, "live": null, "problems": [{"key": "cancelled", "text": "gone"}], "alt": {"dep": "01:55", "arr": "03:05", "lines": ["SN3"]}}',  # noqa: E501
    "a4": '{"fine": false, "live": {"dep": "09:00", "arr": "11:30", "delay": 0}, "problems": [{"key": "platform:Lausanne:9", "text": "pl"}, {"key": "bus:Lausanne", "text": "bus"}], "alt": null}',  # noqa: E501
    "a6": '{"fine": true, "live": {"dep": "10:00", "arr": "11:00", "delay": 0, "platform": "4"}, "problems": [], "alt": null}',
}


@pytest.fixture
def trains(tmp_path, fb):
    script(fb / "train-watch", FAKE_TW)
    tw = tmp_path / "tw"
    tw.mkdir()
    (tmp_path / "tw-none").mkdir()
    for k, v in STATUS.items():
        (tw / f"{k}.json").write_text(v + "\n")
    (tmp_path / "legs.json").write_text(LEGS)
    (tmp_path / "legs-empty.json").write_text("[]\n")
    return tmp_path


def test_train(tcmd, trains):
    t = trains

    def train(now=NOW, twd=t / "tw", **env):
        return tcmd("train", FAKE_TW_LEGS=t / "legs.json", FAKE_TW_DIR=twd, TG_CMD_NOW=now, **env)

    r = train()
    assert r.rc == 0
    assert r.out == (
        "🚆 Trains · next 4 of 5 · home = door to door\n"
        "today 13:10 home → Basel SBB 14:05\n   IR36 · ✅ on time · pl 7\n"
        "today 16:00 home → Lausanne 18:30\n   IC5 › IR15 · ⚠️ +6 min (arr 18:36)\n"
        "tomorrow 01:40 Basel SBB → home 02:50\n   SN3 · ❌ cancelled → alt 01:55 SN3 → 03:05\n"
        "Sat 08.03 09:00 Lausanne → home 11:30\n   IC5 · ⚠️ now pl 9 · 🚌 replacement bus"
    )  # past leg dropped, soonest first, 4 shown
    assert nlines(r.out) <= 10
    assert sorted(tcmd.log().splitlines()) == [
        "train-watch list --json",
        "train-watch status a1 --json",
        "train-watch status a2 --json",
        "train-watch status a3 --json",
        "train-watch status a4 --json",
    ]
    out = train(now="2031-03-01 12:00").out
    assert "home → Basel SBB 14:05\n   IR36 · ✅ as planned" in out  # far leg: as planned, no platform
    assert "on time" not in out
    r = train(twd=t / "tw-none")
    assert r.rc == 0  # every status fails: still rc 0, a grey line per leg
    assert r.out.count("⚪ no live status right now") == 4
    start = time.monotonic()
    r = train(FAKE_TW_SLOW="a3")
    took = time.monotonic() - start
    assert r.rc == 0
    assert "IC5 › IR15 · ⚪ no live status right now\ntomorrow 01:40" in r.out  # slow leg grey, others kept
    assert took < 15  # the slow leg is killed within the plugin's 20 s
    r = tcmd("train", FAKE_TW_LEGS=t / "legs-empty.json", FAKE_TW_DIR=t / "tw", TG_CMD_NOW=NOW)
    assert (r.rc, r.out) == (0, "🚆 No train legs planned ahead.")
    r = train(FAKE_TW_ERR="legs file unreadable")
    assert r.rc == 3
    assert "⚠️ /train failed: train watch: legs file unreadable." in r.out
    assert "are my trains on time?" in r.out


# --------------------------------------------------------------------------- /rave, /trip

FAKE_RR = """#!/usr/bin/env bash
printf 'rave-radar %s\\n' "$*" >> "$FAKE_LOG"
if [ -n "${FAKE_RR_ERR:-}" ]; then echo "rave-radar: $FAKE_RR_ERR" >&2; exit 1; fi
printf '%s\\n' "${FAKE_RR_JSON-[]}"
"""
FAKE_REMIND = """#!/usr/bin/env bash
printf 'remind %s\\n' "$*" >> "$FAKE_LOG"
if [ -n "${FAKE_REMIND_ERR:-}" ]; then echo "remind: $FAKE_REMIND_ERR" >&2; exit 1; fi
printf '%s\\n' "${FAKE_REMIND_OUT-}"
"""
CONDITIONS = """{"trips": {"island": {"name": "Island Trip", "emoji": "🌴", "tz": "UTC", "from": "2031-03-14", "to": "2031-03-22",
  "legs": [{"name": "North Isle", "from": "2031-03-14", "to": "2031-03-18"}, {"name": "South Isle", "from": "2031-03-18", "to": "2031-04-02"}]}}}
"""  # noqa: E501
EXTRA = """{"trips": [{"name": "Island Trip", "link": "https://example.org/hub"},
           {"name": "Unknown Trip", "link": "https://example.org/x"},
           {"name": "Family Fest", "emoji": "💍", "from": "2031-03-07", "to": "2031-03-08", "legs": ["🚗 drive 15:00"], "match": ["fest"], "todo": ["Pack the gear"]}]}
"""  # noqa: E501
TODO = """# TODO
## Due
- **Island prep call** — 06.03 18:00
## Waiting on you
- **Island Trip stays** — villas or resorts? (sent 11:10; internal note)
- **Other thing** — unrelated
## Ideas
- **Island Trip ideas** — not waiting
"""
RR = """[{"kind": "show", "artist": "Artist One", "date": "2031-03-05", "city": "Basel", "venue": "Hall A", "presale": null, "soldout": false},
 {"kind": "show", "artist": "Artist Two", "date": "2031-03-15", "city": "Bern", "venue": "Club B", "soldout": true},
 {"kind": "lineup", "artist": "Artist Three", "date": null, "city": "Somewhere (DE)", "venue": "Big Fest", "presale": "2031-04-01"},
 {"kind": "show", "artist": "Artist Four", "date": "2031-03-08", "city": "Zürich", "venue": "Club C", "presale": "2031-03-01"}]"""  # noqa: E501
REM = """ 1. 2031-03-06 18:00  [owner] 🌴 Island Trip: book all nights (the visa needs them)
 2. 2031-03-07 09:00  Follow up on the Island Trip shuttle
 3. 2031-03-09 09:00  [owner] Pay the bill before the island trip  (tag trip)
 4. 2031-04-01 09:00  [owner] Island Trip photos album
 5. 2031-03-06 10:00  [owner] Dentist"""


@pytest.fixture
def trips(trains, fb):
    t = trains
    script(fb / "rave-radar", FAKE_RR)
    script(fb / "remind", FAKE_REMIND)
    d = t / "trips"
    d.mkdir()
    (d / "conditions.json").write_text(CONDITIONS)
    (d / "pretrip.json").write_text(
        '{"name": "Island Trip", "from": "2031-03-14", "until": "2031-03-22", '
        '"legs": ["flight out Fri 14.03 21:35", "back Sat 22.03"]}\n'
    )
    (d / "extra.json").write_text(EXTRA)
    (d / "TODO.md").write_text(TODO)
    return dict(
        TG_CMD_TRIP_CONDITIONS=d / "conditions.json",
        TG_CMD_PRETRIP=d / "pretrip.json",
        TG_CMD_TRIPS=d / "extra.json",
        TG_CMD_TODO=d / "TODO.md",
    )


def test_rave(tcmd, trains, trips):
    t = trains

    def rave(**env):
        return tcmd("rave", FAKE_TW_LEGS=t / "legs.json", FAKE_TW_DIR=t / "tw", TG_CMD_NOW=NOW, **trips, **env)

    r = rave(FAKE_RR_JSON=RR)
    assert r.rc == 0
    assert r.out == (
        "🎧 Raves · next 5\n"
        "today · Artist One · Basel · 🎟 going\n"
        "Fri 07.03 · Club Night · Lausanne · 🎟 going\n"
        "Sat 08.03 · Artist Four · Zürich · ✈️ clashes with Family Fest\n"
        "Sat 15.03 · Artist Two · Bern · ✈️ clashes with Island Trip · sold out\n"
        "date tba · Artist Three · Big Fest lineup, Somewhere (DE) · presale 01.04"
    )  # going from 🚆 titles, return leg not doubled, clashes, past presale dropped
    assert tcmd.log() == "rave-radar list --days 180 --json\ntrain-watch list --json"  # no status calls
    many = json.dumps(
        [{"kind": "show", "artist": f"Act {i}", "date": f"2031-04-{i + 1:02d}", "city": "Bern"} for i in range(9)]
    )
    out = tcmd("rave", FAKE_RR_JSON=many, FAKE_TW_LEGS=t / "legs-empty.json", TG_CMD_NOW=NOW, **trips).out
    assert "🎧 Raves · next 8 of 9" in out
    assert nlines(out) == 9
    r = rave(FAKE_RR_JSON=RR, FAKE_TW_ERR="locked")
    assert r.rc == 0
    assert "🎟" not in r.out  # train-watch down: no going marks
    assert "today · Artist One · Basel" in r.out
    out = tcmd("rave", FAKE_RR_JSON="[]", FAKE_TW_LEGS=t / "legs-empty.json", TG_CMD_NOW=NOW, **trips).out
    assert out == "🎧 No upcoming raves on the radar."
    r = rave(FAKE_RR_ERR="state unreadable")
    assert r.rc == 3
    assert "what raves are coming up?" in r.out


def test_trip(tcmd, trains, trips):
    t = trains

    def trip(now, rem=REM, **env):
        e = dict(trips, FAKE_TW_LEGS=t / "legs.json", FAKE_TW_DIR=t / "tw", FAKE_REMIND_OUT=rem, TG_CMD_NOW=now)
        e.update(env)
        return tcmd("trip", **e)

    r = trip("2031-03-05 12:00")
    assert r.rc == 0
    assert r.out == (
        "💍 Family Fest · Fri 7–8.3 · in 2 days\n🚗 drive 15:00\n🚆 Sat 08.03 09:00 Lausanne → home 11:30\n"
        "Open:\n☐ Pack the gear\nThen: 🌴 Island Trip 14–22.3"
    )  # next = the short one, its own todo + train leg, then the next trip
    out = trip("2031-03-10 10:00").out
    assert out == (
        "🌴 Island Trip · Fri 14–22.3 (8 nights) · in 4 days\n"
        "🏝 North Isle 14–18.3 › South Isle 18.3–2.4\n"
        "✈️ flight out Fri 14.03 21:35\n✈️ back Sat 22.03\n"
        "🚆 Thu 20.03 10:00 home → Bern 11:00\n"
        "Open:\n☐ Island Trip stays: villas or resorts?\n"
        "☐ 06.03 book all nights (the visa needs them)\n"
        "☐ 09.03 Pay the bill before the island trip\n"
        "🗺 https://example.org/hub"
    )  # files merged by name, waiting item + owner reminders up to the trip end, link
    assert nlines(out) == 10
    assert "shuttle" not in out  # my own follow-ups stay out
    assert "internal note" not in out  # board internals stay out
    assert "example.org/x" not in out  # a dates-less extra trip is ignored
    assert "· day 3 of 9" in trip("2031-03-16 09:00").out.splitlines()[0]
    assert "· last day" in trip("2031-03-22 09:00").out.splitlines()[0]
    many_rem = "\n".join(f" {i}. 2031-03-0{i} 09:00  [owner] Island Trip task {i}" for i in range(1, 7))
    out = trip("2031-03-10 10:00", rem=many_rem).out
    assert nlines(out) == 10
    assert "+ 5 more (ask me)" in out
    r = tcmd(
        "trip",
        **trips,
        FAKE_REMIND_ERR="db locked",
        FAKE_TW_ERR="locked",
        FAKE_TW_LEGS=t / "legs.json",
        TG_CMD_NOW="2031-03-10 10:00",
    )
    assert r.rc == 0
    assert "☐ Island Trip stays: villas or resorts?" in r.out  # remind down: the TODO item still listed
    assert trip("2031-04-03 10:00").out == "🧳 No trip planned."
    d = t / "trips"
    (d / "broken.json").write_text("not json\n")
    r = trip("2031-03-10 10:00", TG_CMD_TRIP_CONDITIONS=d / "broken.json", TG_CMD_PRETRIP=d / "none.json")
    assert (r.rc, r.out) == (0, "🧳 No trip planned.")  # broken/missing trip files: only the remaining trip


def test_trip_words_list_and_pack_line(tcmd, trains, trips, box):
    t = trains
    d = t / "trips"
    (d / "velmora-pack.html").write_text("<!doctype html><p>pack</p>\n")
    x = json.loads((d / "extra.json").read_text())
    x["trips"] += [
        {
            "name": "Velmora Fair", "emoji": "🎪", "from": "2031-03-25", "to": "2031-03-26", "match": ["velmora", "castelluna"],
            "legs": ["✈️ Thu 26.3 Velmora → Castelluna (not booked yet)"], "pack": str(d / "velmora-pack.html"),
        },
        {"name": "Nordby Hike", "from": "2031-04-05", "to": "2031-04-05", "pack": str(d / "missing-pack.html")},
    ]  # fmt: skip
    (d / "extra2.json").write_text(json.dumps(x))

    def trip2(now, *args, rem=""):
        e = dict(trips, TG_CMD_TRIPS=d / "extra2.json", FAKE_TW_LEGS=t / "legs.json", FAKE_TW_DIR=t / "tw")
        return tcmd("trip", *args, FAKE_REMIND_OUT=rem, TG_CMD_NOW=now, **{k: v for k, v in e.items()})

    r = trip2("2031-03-10 10:00", "velmora")
    assert r.rc == 0
    assert r.out.splitlines()[0] == "🎪 Velmora Fair · Tue 25–26.3 · in 15 days"
    assert "✈️ Thu 26.3 Velmora → Castelluna (not booked yet)" in r.out
    assert "📦 Offline pack 1 KB (" in r.out  # pack line with size, date and path
    vp = str(d / "velmora-pack.html")
    if vp.startswith(str(box.home) + "/"):
        vp = "~" + vp[len(str(box.home)) :]  # tg-cmd shows home paths as ~/...
    assert f"“send the pack” → {vp}" in r.out
    assert "Then:" not in r.out  # no Then line for a picked trip
    assert "🎪 Velmora Fair" in trip2("2031-03-10 10:00", "Castelluna").out.splitlines()[0]  # by match word
    assert "🌴 Island Trip" in trip2("2031-03-10 10:00", "ISLAND").out.splitlines()[0]  # by name word, any case
    r = trip2("2031-03-10 10:00", "atlantis")
    assert (r.rc, r.out) == (0, "🧳 No trip ahead called “atlantis”.\n/trip list shows them.")
    out = trip2("2031-03-10 10:00", "list").out
    assert out == (
        "🧳 Trips ahead (3)\n🌴 Island Trip · 14–22.3 · in 4 d\n🎪 Velmora Fair · 25–26.3 · in 15 d 📦\n"
        "🧳 Nordby Hike · 5.4 · in 26 d\n/trip WORDS shows one"
    )
    assert "📦" in next(ln for ln in out.splitlines() if "Velmora" in ln)  # pack marker only where the file exists
    assert "📦" not in next(ln for ln in out.splitlines() if "Nordby" in ln)
    assert "Then: 🎪 Velmora Fair 25–26.3 · 📦 pack ready" in trip2("2031-03-20 12:00").out.splitlines()[-1]
    assert "📦" not in trip2("2031-03-30 10:00", "nordby").out  # missing pack file: no pack line
    rem2 = "\n".join(f" {i}. 2031-03-1{i} 09:00  [owner] Velmora task {i}" for i in range(1, 10))
    out = trip2("2031-03-10 10:00", "velmora", rem=rem2).out
    assert nlines(out) == 10  # 9 to-dos: still 10 lines
    assert "📦 Offline pack" in out.splitlines()[-1]  # pack line kept
    assert tcmd("trip", "a", "b").rc == 2


# --------------------------------------------------------------------------- /warranty, /reminders

FAKE_CONTRACTS = """#!/usr/bin/env bash
printf 'contracts %s\\n' "$*" >> "$FAKE_LOG"
if [ -n "${FAKE_CONTRACTS_ERR:-}" ]; then echo "contracts: $FAKE_CONTRACTS_ERR" >&2; exit 3; fi
printf '%s\\n' "${FAKE_CONTRACTS_OUT-🛡 Warranty · 1 still covered
• Beispiel-Fernseher · Galaxus · until 12.09.2028
24 mo = Swiss legal default (OR 210); shop/maker terms may differ.}"
"""
WL = """{"list": "warranty", "open": [
 {"n": 1, "text": "Beispiel-Kopfhörer · Digitec · bought 01.02.2030 · warranty to 01.02.2032", "added": "2030-02-01 10:00"},
 {"n": 2, "text": "Beispiel-Mixer · Brack · bought 01.06.2029 · warranty to 01.04.2031", "added": "2029-06-01 10:00"},
 {"n": 3, "text": "Alter Toaster · bought 01.01.2027 · warranty to 01.01.2029", "added": "2027-01-01 10:00"},
 {"n": 4, "text": "Velo-Lampe ohne Datum", "added": "2030-01-01 10:00"}], "done": [
 {"text": "Erledigt · warranty to 01.01.2035", "done": "2030-01-01 10:00"}]}"""


def test_warranty(tcmd, fb):
    script(fb / "lists", FAKE_LISTS)
    script(fb / "contracts", FAKE_CONTRACTS)
    empty = '{"list": "warranty", "open": [], "done": []}'

    def w(*args, lists=empty, **env):
        return tcmd("warranty", *args, FAKE_LISTS_OUT=lists, **env)

    def last():
        return tcmd.log().splitlines()[-1]

    r = w()
    assert r.rc == 0
    assert "• Beispiel-Fernseher · Galaxus · until 12.09.2028" in r.out
    assert tcmd.log() == "lists show warranty --json\ncontracts warranty --max 7 --budget 6"  # small card budget
    assert "Logged by you" not in r.out  # no logged entries: no block
    assert w("  Fernseher   55 ").rc == 0
    assert last() == "contracts warranty --max 7 --budget 6 -- Fernseher 55"  # words after --
    tcmd("/warranty", "--all", FAKE_LISTS_OUT=empty)
    assert last() == "contracts warranty --max 7 --budget 6 -- --all"  # a dash word stays a word
    w("   ")
    assert last() == "contracts warranty --max 7 --budget 6"  # blank arg = no filter
    r = w("a", "b")
    assert r.rc == 2
    assert "one argument at most" in r.err
    assert w(FAKE_CONTRACTS_OUT="").out == "🛡 Warranty · no answer"
    r = w(FAKE_CONTRACTS_ERR="paperless metadata unavailable: timeout")
    assert r.rc == 3
    assert "⚠️ /warranty failed: paperless: paperless metadata unavailable: timeout." in r.out
    assert "is my X still under warranty?" in r.out
    assert nlines(w().out) <= 10  # short card
    # his own entries (voice-plan's warranty action -> `lists add warranty "product · shop · bought D · warranty to D"`)
    r = w(lists=WL, TG_CMD_NOW=NOW)
    assert r.rc == 0
    assert "• Beispiel-Fernseher · Galaxus · until 12.09.2028\n📝 Logged by you (3)" in r.out  # after paperless rows
    assert (
        "📝 Logged by you (3)\n• Beispiel-Mixer · Brack · ⚠️ until 01.04.2031 (27 d)\n"
        "• Beispiel-Kopfhörer · Digitec · until 01.02.2032\n• Velo-Lampe ohne Datum"
    ) in r.out  # soonest first, ⚠️ within 90 d
    assert "Toaster" not in r.out and "Erledigt" not in r.out  # expired + checked ones hidden
    assert r.out.splitlines()[-1] == "24 mo = Swiss legal default (OR 210); shop/maker terms may differ."
    assert last() == "contracts warranty --max 4 --budget 6"  # paperless gets fewer rows (7 - 3 logged lines)
    assert nlines(r.out) <= 12
    out = w("toaster", lists=WL, TG_CMD_NOW=NOW).out
    assert "📝 Logged by you · 1 match\n• Alter Toaster · expired 01.01.2029" in out  # an expired match is shown
    assert last() == "contracts warranty --max 6 --budget 6 -- toaster"  # words still go to contracts
    out = w("kopfhorer", lists=WL, TG_CMD_NOW=NOW).out
    assert "• Beispiel-Kopfhörer · Digitec · until 01.02.2032" in out  # accent-folded match
    r = w(lists=WL, TG_CMD_NOW=NOW, FAKE_CONTRACTS_ERR="paperless metadata unavailable: timeout")
    assert r.rc == 0
    assert "🛡 Warranty · ⚠️ paperless not reachable (paperless metadata unavailable: timeout)" in r.out
    assert "• Beispiel-Mixer · Brack" in r.out
    out = w(lists=WL, TG_CMD_NOW=NOW, FAKE_CONTRACTS_OUT="").out
    assert out.splitlines()[:2] == ["🛡 Warranty", "📝 Logged by you (3)"]
    many = json.dumps(
        {
            "list": "warranty",
            "open": [{"text": f"Ding {i} · warranty to 0{i}.01.2033"} for i in range(1, 7)],
            "done": [],
        }
    )
    out = w(lists=many, TG_CMD_NOW=NOW).out
    assert "• Ding 4 · until 04.01.2033\n… +2 more" in out  # at most 4 rows + more
    assert last() == "contracts warranty --max 3 --budget 6"  # paperless keeps at least 3 rows
    r = w(lists="not json")
    assert r.rc == 0
    assert "⚠️ logged warranties: unreadable answer" in r.out
    assert "• Beispiel-Fernseher · Galaxus · until 12.09.2028" in r.out
    out = tcmd("warranty", "tv", FAKE_LISTS_ERR="lock busy").out
    assert "⚠️" not in out  # a search doesn't warn about the logged list


def test_reminders_by_place(tcmd, fb):
    script(fb / "remind", FAKE_REMIND)
    prem = (
        " 1. 2031-03-06 18:00  [owner] Dentist\n 2. 📍 when home: Power-cycle the sensors\n"
        " 3. 📍 when at Work: Bring the badge  (every time)  (tag x)"
    )
    out = tcmd("reminders", FAKE_REMIND_OUT=prem, TG_CMD_NOW=NOW).out
    assert "📍 By place" in out
    assert "When home: Power-cycle the sensors" in out
    assert "When at Work: Bring the badge  🔁" in out  # every time = 🔁, tag dropped
    assert "Dentist" in out  # timed ones still listed
    out = tcmd("reminders", FAKE_REMIND_OUT=" 1. 📍 when leaving home: Take the bins", TG_CMD_NOW=NOW).out
    assert "When leaving home: Take the bins" in out


# --------------------------------------------------------------------------- /gohome, /vibe, /finish, /doctor, /crate, /party

FAKE_GOHOME = """#!/usr/bin/env bash
printf '%s\\n' "$#:$*" >> "$FAKE_GH_LOG"
if [ -n "${FAKE_GH_ERR:-}" ]; then echo "gohome: transport API unreachable or broken: timed out" >&2; exit 1; fi
printf '🏠 Home from %s · 22:10\\n22:37 Alpha Hbf Pl. 3 · S7 · direct → 🏠 23:40 https://app.sbbmobile.ch/timetable?from=Alpha%%20Hbf&to=X&time=1\\n' "${1:-Alpha Hbf (phone)}"
"""  # noqa: E501


def test_gohome(box, fb):
    script(fb / "gohome", FAKE_GOHOME)
    log = fb / "log"
    env = dict(TG_CMD_BIN=fb, FAKE_GH_LOG=log)
    r = box.run("gohome", env=env)
    assert r.rc == 0
    assert r.out.splitlines()[0] == "🏠 Home from Alpha Hbf (phone) · 22:10"
    assert "https://app.sbbmobile.ch/timetable?from=Alpha%20Hbf" in r.out
    assert box.run("gohome", "Alpha Hbf Nord", env=env).rc == 0
    assert log.read_text() == "0:\n1:Alpha Hbf Nord\n"  # no arg = no argv, a stop with spaces = one argv
    r = box.run("gohome", env=dict(env, FAKE_GH_ERR="1"))
    assert r.rc == 3
    assert r.out == (
        "⚠️ /gohome failed: way home: transport API unreachable or broken: timed out.\n"
        'Ask me in a normal message ("how do I get home from here?") and I\'ll look.'
    )


FAKE_MEDIA_VIBE = """#!/usr/bin/env bash
printf '%s\\n' "$#:$*" >> "$FAKE_VB_LOG"
if [ -n "${FAKE_VB_ERR:-}" ]; then echo "media: vibe: no index yet and Emby is unreachable" >&2; exit 1; fi
if [ -n "${FAKE_VB_BAD:-}" ]; then echo "not json"; exit 0; fi
printf '{"text": "🎬 Vibe · cozy rainy\\\\n\\\\n1️⃣ Cozy Little Cafe (2022) · 84 min · ★6.9\\\\nComedy · cozy\\\\n▶ https://emby.example.test/web/index.html#!/item?id=115", "message": "**x**", "buttons": []}\\n'
"""  # noqa: E501
FAKE_MEDIA_FINISH = """#!/usr/bin/env bash
printf '%s\\n' "$#:$*" >> "$FAKE_VB_LOG"
if [ -n "${FAKE_FIN_ERR:-}" ]; then echo "media: Seerr user 'x' has no linked Emby account" >&2; exit 1; fi
if [ -n "${FAKE_FIN_BAD:-}" ]; then echo "not json"; exit 0; fi
printf '{"text": "🏁 Finish next\\\\nStarted, paused 14+ days, episodes left\\\\n\\\\n📺 Hijack · 3 of 8 left · ★7.4\\\\n   next S01E06 · last watched 21.08.\\\\n   ▶ https://emby.example.test/web/index.html#!/item?id=77", "message": "**x**", "buttons": []}\\n'
[ -n "${FAKE_FIN_PART:-}" ] && exit 3
exit 0
"""  # noqa: E501
FAKE_MEDIA_DOCTOR = """#!/usr/bin/env bash
printf '%s\\n' "$#:$*" >> "$FAKE_VB_LOG"
if [ -n "${FAKE_DR_ERR:-}" ]; then echo "media: no Seerr match for 'Nope'" >&2; exit 1; fi
if [ -n "${FAKE_DR_BAD:-}" ]; then echo "not json"; exit 0; fi
printf '{"text": "🩺 Request doctor · 2 open requests\\\\n🔤 Mismatch Movie: name mismatch", "message": "**x**", "items": []}\\n'
[ -n "${FAKE_DR_PART:-}" ] && exit 3
exit 0
"""


@pytest.fixture
def media(box, fb):
    log = fb / "log"

    def make(body):
        script(fb / "media", body)

        def run(*args, **env):
            return box.run(*args, env=dict(TG_CMD_BIN=fb, FAKE_VB_LOG=log, **env))

        run.log = lambda: log.read_text().rstrip("\n") if log.exists() else ""
        run.reset = lambda: log.write_text("")
        return run

    return make


def test_vibe(media):
    vb = media(FAKE_MEDIA_VIBE)
    r = vb("vibe")
    assert r.rc == 0
    assert "/vibe cozy rainy sunday" in r.out  # bare = hint
    assert vb.log() == ""  # bare runs nothing
    r = vb("vibe", "cozy  rainy")
    assert r.rc == 0
    assert r.out == (
        "🎬 Vibe · cozy rainy\n\n1️⃣ Cozy Little Cafe (2022) · 84 min · ★6.9\nComedy · cozy\n"
        "▶ https://emby.example.test/web/index.html#!/item?id=115"
    )
    assert vb.log() == "5:vibe --json -- cozy rainy"
    vb.reset()
    vb("vibe", "-n 9 like Arrival")
    assert vb.log() == "7:vibe --json -- -n 9 like Arrival"  # dashes stay words
    r = vb("vibe", "cozy", FAKE_VB_ERR="1")
    assert r.rc == 3
    assert "a movie from my library for a cozy evening" in r.out
    assert vb("vibe", "cozy", FAKE_VB_BAD="1").rc == 3  # unreadable answer
    assert vb("vibe", "cozy", "rainy").rc == 2  # two args = usage


def test_finish(media):
    vb = media(FAKE_MEDIA_FINISH)
    vb.reset()
    r = vb("finish")
    assert r.rc == 0
    assert r.out == (
        "🏁 Finish next\nStarted, paused 14+ days, episodes left\n\n📺 Hijack · 3 of 8 left · ★7.4\n"
        "   next S01E06 · last watched 21.08.\n   ▶ https://emby.example.test/web/index.html#!/item?id=77"
    )
    assert vb.log() == "2:finish --json"
    r = vb("finish", FAKE_FIN_PART="1")
    assert (r.rc, r.out.splitlines()[3]) == (
        0,
        "📺 Hijack · 3 of 8 left · ★7.4",
    )  # media exit 3 (a show failed) answers
    r = vb("finish", FAKE_FIN_ERR="1")
    assert r.rc == 3
    assert "no linked Emby account" in r.out
    assert "which shows should I finish" in r.out
    assert "Emby finish: unreadable answer" in vb("finish", FAKE_FIN_BAD="1").out
    assert vb("finish", "x").rc == 2  # takes no argument


def test_doctor(media):
    vb = media(FAKE_MEDIA_DOCTOR)
    vb.reset()
    r = vb("doctor")
    assert r.rc == 0
    assert r.out == "🩺 Request doctor · 2 open requests\n🔤 Mismatch Movie: name mismatch"
    assert vb.log() == "3:doctor --json --no-probe"
    vb.reset()
    vb("doctor", "The Last  Mountain")
    assert vb.log() == "7:doctor --json --no-probe -- The Last Mountain"  # one title after --
    r = vb("doctor", FAKE_DR_PART="1")
    assert (r.rc, r.out.splitlines()[0]) == (0, "🩺 Request doctor · 2 open requests")  # media exit 3 still answers
    r = vb("doctor", "Nope", FAKE_DR_ERR="1")
    assert r.rc == 3
    assert "no Seerr match for 'Nope'" in r.out
    assert "why isn't X on Emby yet" in r.out
    assert "Request doctor: unreadable answer" in vb("doctor", FAKE_DR_BAD="1").out
    assert vb("doctor", "a", "b").rc == 2


FAKE_CRATE = """#!/bin/sh
[ -n "$FAKE_CR_LOG" ] && echo "crate-now $*" >> "$FAKE_CR_LOG"
case "$FAKE_CR" in
  add) echo "➕ Crate: Artist – Title" ;;
  fail) echo "⚠️ Couldn't save the track (Home Assistant: down)."; exit 3 ;;
  crash) echo "boom" >&2; exit 1 ;;
esac
"""


def test_crate(box, fb):
    script(fb / "crate-now", FAKE_CRATE)
    log = fb / "log"
    r = box.run("crate", env=dict(TG_CMD_BIN=fb, FAKE_CR="add", FAKE_CR_LOG=log))
    assert (r.rc, r.out) == (0, "➕ Crate: Artist – Title")
    assert log.read_text() == "crate-now \n"  # called without args
    r = box.run("crate", env=dict(TG_CMD_BIN=fb, FAKE_CR="fail", FAKE_CR_LOG=""))
    assert r.out == "⚠️ Couldn't save the track (Home Assistant: down)."  # exit-3 fallback line shown
    r = box.run("crate", env=dict(TG_CMD_BIN=fb, FAKE_CR="crash", FAKE_CR_LOG=""))
    assert r.rc == 3
    assert "add the playing track to my crate" in r.out


FAKE_PARTY = """#!/usr/bin/env bash
echo "$#:$*" >> "$FAKE_PT_LOG"
if [ -n "${FAKE_PT_CRASH:-}" ]; then echo "Traceback: boom" >&2; exit 2; fi
case "$1" in
  on) echo "🎉 Party on · Kitchen + Shelf"; echo "♪ 128 BPM (Deezer) · Party palette" ;;
  off) if [ -n "${FAKE_PT_FAIL:-}" ]; then echo "⚠️ Party off: nothing restored yet"; exit 1; fi; echo "💡 Party off · Kitchen + Shelf restored" ;;
  status) echo "No party running." ;;
esac
"""  # noqa: E501


def test_party(box, fb):
    script(fb / "party", FAKE_PARTY)
    log = fb / "log"

    def pt(*args, **env):
        log.write_text("")
        return box.run("party", *args, env=dict(TG_CMD_BIN=fb, FAKE_PT_LOG=log, **env))

    r = pt()
    assert (r.rc, r.out) == (0, "🎉 Party on · Kitchen + Shelf\n♪ 128 BPM (Deezer) · Party palette")  # bare = on
    assert log.read_text() == "1:on\n"
    assert pt(" Off ").out == "💡 Party off · Kitchen + Shelf restored"
    assert log.read_text() == "1:off\n"
    log.write_text("")
    for a in ("stop", "status"):
        box.run("party", a, env=dict(TG_CMD_BIN=fb, FAKE_PT_LOG=log))
    assert log.read_text() == "1:off\n1:status\n"  # stop = off, status
    assert "/party off restores the lights" in pt("dance").out  # other words = hint
    assert log.read_text() == ""  # the hint runs nothing
    r = pt("off", FAKE_PT_FAIL="1")
    assert (r.rc, r.out) == (0, "⚠️ Party off: nothing restored yet")  # party's exit 1 keeps its own text
    r = pt(FAKE_PT_CRASH="1")
    assert r.rc == 3
    assert "party lights on (or off)" in r.out


# --------------------------------------------------------------------------- /cat (text)


def test_cat_text(tmp_path, fb):
    box = Box(tmp_path, profile=True)
    cat_history(tmp_path / "cat")
    log = fb / "log"
    r = box.run("cat", env=dict(FAKE_LOG=log, TG_CMD_BIN=fb, TG_CMD_NOW=NOW, CAT_CORNER_STATE_DIR=tmp_path / "cat"))
    assert r.rc == 0
    assert r.out == (
        "🐾 Mittens · last 5 days\nLitter visits: 3 today so far · 9 yesterday · avg 7.5/day\n"
        "Weight 4.10 kg (+0.09 kg in 5 d)\nFeeder: 10 g today so far · 20 g yesterday\nDrawer 85% full"
    )
    out = box.run(
        "cat", env=dict(FAKE_LOG=log, TG_CMD_NOW="2031-03-09 12:00", CAT_CORNER_STATE_DIR=tmp_path / "cat")
    ).out
    assert "⚠️ cat-corner's data is 4 days old" in out  # stale data flagged
    r = box.run("cat", env=dict(FAKE_LOG=log, CAT_CORNER_STATE_DIR=tmp_path / "nothing"))
    assert r.rc == 3
    assert "⚠️ /cat failed: no cat-corner history yet." in r.out
    assert "how is the cat doing?" in r.out
    # its exec record lands in the scratch usage store, not the live one
    assert sum(bool(re.search(r'"f":"tg.cat".*"rc":3', ln)) for ln in box.usage_lines()) == 1


# --------------------------------------------------------------------------- /back (fixtures/back)

BACK_FULL = """👋 Catch-up · last 2 days
⏰ 4 reminders fired · open: Water the plants, Pick up parcel
🔜 Next 48 h: today 18:00 Take out the bins, tomorrow 09:30 Dentist check-up (+1)
💳 2 bills due ≤ 7 d: Phone Co overdue, Test Power AG Sat 08.03
🎬 New on Emby: 2 · Test Movie One, Test Show
🖥 Server: 1 critical + 2 warnings · disk3 is disabled, CPU is hot: 90°C (+1)
🎂 Bobby today, Testa Fri 07.03
📅 Next: tomorrow 19:30 Concert night · Testburg"""
BFIX = FIX / "back"
SRC = {"REMIND": "remind", "BILLS": "bills", "MEDIA": "media", "GIFT": "gift-radar", "CAL": "calcache"}


class Back:
    """back.sh's world: fakes that log their args and print FAKE_<VAR>, a state dir with recent.json/place-state."""

    def __init__(self, box: Box, t: Path):
        self.box, self.t = box, t
        self.bin, self.state, self.calls = t / "bin", t / "state", t / "calls"
        for d in (self.bin, self.state, t / "empty"):
            d.mkdir()
        for var, name in SRC.items():
            script(
                self.bin / name,
                f'#!/usr/bin/env bash\nprintf \'%s %s\\n\' "{name}" "$*" >> "{self.calls}"\n'
                f'[ -n "${{FAIL_{var}:-}}" ] && {{ echo "{name}: broken" >&2; exit ${{FAIL_{var}}}; }}\n'
                f'cat "${{FAKE_{var}}}"\n',
            )
        (t / "none.txt").write_text("")
        (t / "none.json").write_text("[]\n")
        self.env: dict = {}

    def full(self):
        self.env = dict(
            FAKE_REMIND=BFIX / "remind-list.txt", FAKE_BILLS=BFIX / "bills.json", FAKE_MEDIA=BFIX / "media-recent.txt",
            FAKE_GIFT=BFIX / "gift-radar.txt", FAKE_CAL=BFIX / "calcache.json", REPORTS=BFIX / "reports",
        )  # fmt: skip
        shutil.copy(BFIX / "recent.json", self.state / "recent.json")
        (self.state / "place-state.json").unlink(missing_ok=True)

    def empty(self):
        t = self.t
        self.env = dict(
            FAKE_REMIND=t / "none.txt", FAKE_BILLS=t / "none.json", FAKE_MEDIA=t / "none.txt", FAKE_GIFT=t / "none.txt",
            FAKE_CAL=t / "none.json", REPORTS=t / "empty",
        )  # fmt: skip
        for p in self.state.iterdir():
            p.unlink()

    def place(self, s: str):
        (self.state / "place-state.json").write_text(s)

    def __call__(self, *args, now=NOW, **over):
        self.calls.write_text("")
        e = dict(self.env, **over)
        reports = e.pop("REPORTS")
        e.update(TG_CMD_BIN=e.get("TG_CMD_BIN", self.bin), TG_CMD_NOW=now, REMIND_DB=self.state / "reminders.json")
        e["TG_CMD_UNRAID_REPORTS"] = reports
        return self.box.run("back", *args, env=e)


@pytest.fixture
def back(box, tmp_path):
    t = tmp_path / "back"
    t.mkdir()
    b = Back(box, t)
    b.full()
    return b


def test_back_full_card(back):
    r = back("2")
    assert r.rc == 0
    assert r.out == BACK_FULL
    assert nlines(r.out) <= 10
    calls = back.calls.read_text()
    for want in (
        "remind list", "bills list --open --json", "media recent -n 40", "gift-radar list --days 7 --date 2031-03-05",
        "calcache show --days 14 --json",
    ):  # fmt: skip
        assert want in calls, want
    assert len(calls.splitlines()) == 5  # five sources, read verbs only
    for bad in (
        "Example", "Person", "80.00", "45.50", "CHF", "MYOWN", "of mine", "Too far away", "Ancient", "Call the garage",
        "Return library", "Far Later", "Older Movie", "Test Movie Two", "docker.img", "disk7", "disk9", "oldbox",
        "Stale old", "FCP", "Past meeting", "Today marker", "Place one", "before noon", "tag home", "every Thu",
    ):  # fmt: skip
        assert bad not in r.out, bad


def test_back_window(back):
    out = back("5").out
    assert "👋 Catch-up · last 5 days" in out
    assert "🎬 New on Emby: 3 · Test Movie One, Test Show (+1)" in out  # older media counted
    assert "⏰ 4 reminders fired · open: Water the plants, Pick up parcel" in out  # snoozed one counts as fired
    out = back("1").out
    assert "👋 Catch-up · last 1 day" in out
    assert "⏰ 2 reminders fired · open: Pick up parcel" in out
    assert "🖥 Server: 1 critical + 2 warnings" in out  # server flags by day (since 04.03 counts)
    assert "disk7" not in out
    out = back("30").out
    assert "last 30 days" in out
    assert "⏰ 5 reminders fired" in out  # the ancient fired one is in
    for bad in ("0", "31", "x", "-2", "2 3"):
        r = back(bad)
        assert (r.rc, r.out) == (0, "Use /back or /back N (N = days, 1–30)."), bad
    assert "last 3 days" in back("3d").out
    assert "last 2 days" in back(" 2 ").out


def test_back_default_window_from_place_state(back):
    assert "👋 Catch-up · last 2 days" in back().out  # no place-state: 2 days
    back.place(
        '{"zone": "home", "since": "2031-03-04 18:00", "trip": {"left": "2031-03-03 10:00", "back": "2031-03-04 18:00"}}'
    )
    out = back().out
    assert "👋 Catch-up · since Mon 03.03 10:00" in out  # trip back 18 h ago: since it began
    assert "🎬 New on Emby: 2 ·" in out  # media from 09:00 that day is out
    assert "⏰ 4 reminders fired" in out  # fired at 13:00 that day is in
    back.place('{"zone": "home", "trip": {"left": "2031-02-25 10:00", "back": "2031-03-01 18:00"}}')
    assert "👋 Catch-up · last 2 days" in back().out  # trip back 4 days ago: 2 days
    back.place(
        '{"zone": "away", "since": "2031-03-04 07:10", "left": "2031-03-04 07:00", '
        '"trip": {"left": "2031-03-01 10:00", "back": "2031-03-02 10:00"}}'
    )
    assert "👋 Catch-up · since Tue 04.03 07:00" in back().out  # away now: since he left
    back.place('{"zone": "zone:Work", "since": "2031-03-05 08:00", "left": "2031-02-01 07:00"}')
    assert "👋 Catch-up · last 2 days" in back().out  # away > 30 days: 2 days
    back.place("not json")
    r = back()
    assert (r.rc, r.out.splitlines()[0]) == (0, "👋 Catch-up · last 2 days")
    back.place('{"zone": "home", "trip": {"left": "2031-03-03 10:00", "back": "2031-03-04 18:00"}}')
    assert "last 1 day" in back("1").out  # N beats the trip


def test_back_empty_sources(back):
    t = back.t
    back.empty()
    r = back("2")
    assert (r.rc, r.out) == (0, "👋 Catch-up · last 2 days\nNothing new, nothing due soon.")
    for v in ("REMIND", "BILLS", "MEDIA", "GIFT", "CAL"):
        back.full()
        r = back("2", **{f"FAKE_{v}": t / ("none.json" if v in ("CAL", "BILLS") else "none.txt")})
        assert nlines(r.out) == 7, v  # only one source empty: 7 lines
    for var, f, mark in (
        ("FAKE_REMIND", "none.txt", "🔜"), ("FAKE_BILLS", "none.json", "💳"), ("FAKE_MEDIA", "none.txt", "🎬"),
        ("FAKE_GIFT", "none.txt", "🎂"), ("FAKE_CAL", "none.json", "📅"),
    ):  # fmt: skip
        back.full()
        assert mark not in back("2", **{var: t / f}).out, var
    back.full()
    assert "🖥" not in back("2", REPORTS=t / "empty").out  # no report: no 🖥
    back.full()
    (back.state / "recent.json").write_text("{}\n")
    assert "⏰" not in back("2").out  # nothing fired
    back.full()
    assert "🖥" not in back("2", now="2031-03-12 12:00").out  # old report only
    back.full()
    r = back("2", FAIL_CAL="3")
    assert (r.rc, sum(bool(re.search("📅|Not checked", ln)) for ln in r.out.splitlines())) == (0, 0)  # calcache exit 3
    back.full()
    p = back.state / "recent.json"
    d = json.loads(p.read_text())
    for k in d:
        d[k]["state"] = "done" if d[k].get("state") == "fired" else d[k]["state"]
    p.write_text(json.dumps(d))
    assert "⏰ 4 reminders fired · all handled" in back("2").out
    back.full()
    (t / "one.json").write_text('[{"payee": "Solo Bill", "due": "2031-03-05", "amount": "1", "currency": "CHF"}]')
    assert "💳 1 bill due ≤ 7 d: Solo Bill today" in back("2", FAKE_BILLS=t / "one.json").out


def test_back_calendar_picks_and_media_cap(back):
    t = back.t
    (t / "cal.json").write_text(
        '[{"start": "2031-03-06", "end": "2031-03-07", "all_day": true, "title": "Holiday", "town": null},\n'
        ' {"start": "2031-03-06T19:30:00+01:00", "all_day": false, "title": "Concert night", "town": "Testburg"}]'
    )
    assert "📅 Next: tomorrow Holiday" in back("2", FAKE_CAL=t / "cal.json").out  # all-day tomorrow first: no time
    (t / "cal.json").write_text('[{"start": "2031-03-05T12:30:00Z", "all_day": false, "title": "Lunch", "town": null}]')
    out = back("2", FAKE_CAL=t / "cal.json").out
    assert "📅 Next: today 13:30 Lunch" in out  # UTC start shown in local time, no town
    assert " · " not in next(ln for ln in out.splitlines() if "📅" in ln)
    (t / "many.txt").write_text(
        "".join(f"Wed 05.03 11:{i % 60:02d}  movie  Bulk Movie {i:02d} (2030)\n" for i in range(1, 41))
    )
    assert "🎬 New on Emby: 40+ · Bulk Movie 01, Bulk Movie 02 (+38)" in back("2", FAKE_MEDIA=t / "many.txt").out


def test_back_failures(back):
    t = back.t
    r = back("2", FAIL_BILLS="1")
    assert r.rc == 0
    assert "⚠️ Not checked: bills" in r.out
    assert "🎬 New on Emby: 2" in r.out  # the rest still there
    out = back("2", FAIL_REMIND="1", FAIL_MEDIA="1").out
    assert "⚠️ Not checked: reminders, Emby" in out
    assert "⏰ 4 reminders fired" in out  # remind down: fired ones still listed (recent.json)
    (t / "bad.json").write_text("not json")
    assert "⚠️ Not checked: bills" in back("2", FAKE_BILLS=t / "bad.json").out
    (t / "badrep" / "2031-03-05.md").mkdir(parents=True)
    r = back("2", FAIL_REMIND="1", FAIL_BILLS="1", FAIL_MEDIA="1", FAIL_GIFT="1", FAIL_CAL="1", REPORTS=t / "badrep")
    assert r.rc == 3
    assert "⚠️ /back failed: no data source answered." in r.out
    assert "what did I miss while I was away" in r.out
    r = back("2", TG_CMD_BIN=t / "nowhere")
    assert r.rc == 0  # missing binaries: a partial card
    assert "🖥 Server: 1 critical" in r.out


# --------------------------------------------------------------------------- /pack + pack --todo

PACK_TPL = """{"types": {
 "rave": {"label": "Rave / club night", "emoji": "🪩", "items": ["🎟 Ticket", "👂 Earplugs", "💳 Card"]},
 "wedding": {"label": "Wedding / gig", "emoji": "💍", "items": ["🎧 Headphones", "👔 Suit", "🎁 Gift"]},
 "business": {"label": "Business day abroad", "emoji": "💼", "items": ["🛂 Passport / ID", "💶 EUR", "🔌 Adapter", "🔋 Phone charger", "💻 Laptop"]},
 "beach": {"label": "Beach / long trip", "emoji": "🏝", "items": ["🩱 Swimwear", "🧴 Sunscreen", "s1", "s2", "s3", "s4", "s5"]}
}}
"""  # noqa: E501


def _fl(travel, away, abroad, idc):
    return {"travel": travel, "busy_evening": False, "away_overnight": away, "abroad": abroad, "id_check": idc}


PACK_CAL = [
    {
        "id": "e1",
        "start": "2026-03-03",
        "end": "2026-03-04",
        "all_day": True,
        "title": "@Werkstadt",
        "town": "Werkstadt",
        "free": True,
        "flags": _fl(None, False, False, False),
    },  # noqa: E501
    {
        "id": "e2",
        "start": "2026-03-03T19:00:00+01:00",
        "end": "2026-03-03T21:00:00+01:00",
        "all_day": False,
        "title": "Climbing",
        "town": None,
        "free": False,
        "flags": _fl(None, False, False, False),
    },  # noqa: E501
    {
        "id": "e3",
        "start": "2026-03-06T20:00:00+01:00",
        "end": "2026-03-06T21:10:00+01:00",
        "all_day": False,
        "title": "🚆 Home → Basel SBB (Kalia Dunes)",
        "town": "Bachwil",
        "free": False,
        "flags": _fl("train", False, False, False),
        "train": {"from": "home", "to": "Basel SBB", "lines": []},
    },  # noqa: E501
    {
        "id": "e4",
        "start": "2026-03-06T23:00:00+01:00",
        "end": "2026-03-07T06:00:00+01:00",
        "all_day": False,
        "title": "Kalia Dunez",
        "town": "Basel",
        "free": True,
        "flags": _fl(None, True, False, True),
    },  # noqa: E501
    {
        "id": "e5",
        "start": "2026-03-07T05:10:00+01:00",
        "end": "2026-03-07T06:30:00+01:00",
        "all_day": False,
        "title": "🚆 Basel SBB → Home",
        "town": "Basel",
        "free": False,
        "flags": _fl("train", False, False, False),
        "train": {"from": "Basel SBB", "to": "home", "lines": []},
    },  # noqa: E501
    {
        "id": "e6",
        "start": "2026-03-07T16:00:00+01:00",
        "end": "2026-03-08T01:00:00+01:00",
        "all_day": False,
        "title": "Mira Hochziit",
        "town": "Werkstadt",
        "free": True,
        "flags": _fl(None, False, False, False),
    },  # noqa: E501
    {
        "id": "e7",
        "start": "2026-03-07T23:20:00+01:00",
        "end": "2026-03-08T00:10:00+01:00",
        "all_day": False,
        "title": "🚆 Werkstadt → Home",
        "town": "Werkstadt",
        "free": False,
        "flags": _fl("train", False, False, False),
        "train": {"from": "Werkstadt", "to": "home", "lines": []},
    },  # noqa: E501
    {
        "id": "e8",
        "start": "2026-03-20",
        "end": "2026-03-21",
        "all_day": True,
        "title": "🇧🇪 Customer visit (Gent)",
        "town": "Gent",
        "free": False,
        "flags": _fl(None, True, True, False),
    },  # noqa: E501
]


@pytest.fixture
def pk(box, tmp_path):
    t = tmp_path / "pk"
    b = t / "bin"
    calls = t / "calls"
    script(
        b / "calcache",
        f'#!/usr/bin/env bash\n[ -n "${{FAIL_CAL:-}}" ] && {{ echo "calcache: broken" >&2; exit $FAIL_CAL; }}\ncat "{t}/cal.json"\n',
    )
    script(
        b / "rave-radar",
        '#!/usr/bin/env bash\n[ -n "${FAIL_RR:-}" ] && { echo "rave-radar: broken" >&2; exit 1; }\n'
        """echo '[{"name": "Kalia Dunes", "aliases": ["Dunes"], "source": "x", "added": "2026-01-01"}]'\n""",
    )
    script(
        b / "ha",
        f"""#!/usr/bin/env bash
printf '%s\\n' "$*" >> "{calls}"
case "$1 $2" in
  "list todo") cat "${{FAKE_HA_LIST}}" ;;
  "todo items") [ -n "${{FAKE_HA_DENY:-}}" ] && {{ echo "ha: refused: $4 is not an allowed to-do list" >&2; exit 1; }}
                echo '[{{"uid": "u1", "summary": "🎧 Headphones", "status": "needs_action"}}]' ;;
  "todo add") echo "added" ;;
  *) exit 2 ;;
esac
""",
    )
    (t / "cal.json").write_text(json.dumps(PACK_CAL, ensure_ascii=False))
    (t / "tpl.json").write_text(PACK_TPL)
    (t / "trips.json").write_text(
        '[{"name": "Mira\'s wedding", "from": "2026-03-07", "to": "2026-03-08", "match": ["Mira"]}]\n'
    )
    (t / "tc.json").write_text(
        '{"trips": {"isl": {"name": "Island trip", "from": "2026-03-25", "to": "2026-04-08", '
        '"legs": [{"name": "Isla", "from": "2026-03-26", "to": "2026-04-07"}]}}}\n'
    )
    (t / "ha-none.json").write_text('[{"entity_id": "todo.shopping_list", "name": "Shopping list", "state": "1"}]\n')
    (t / "ha-pack.json").write_text(
        '[{"entity_id": "todo.shopping_list", "name": "Shopping list"}, {"entity_id": "todo.packing", "name": "Packing"}]\n'
    )
    base = dict(
        TG_CMD_BIN=b, TG_CMD_NOW="2026-03-02 09:00", TG_CMD_PACK_TEMPLATES=t / "tpl.json", TG_CMD_TRIPS=t / "trips.json",
        TG_CMD_TRIP_CONDITIONS=t / "tc.json", TG_CMD_PRETRIP=t / "none.json", PACK_TG_CMD=box.tgcmd, TZ="Europe/Zurich",
    )  # fmt: skip

    def p(*args, **env):
        return box.run("pack", *args, env=dict(base, **env))

    def pack(*args, **env):
        return box.run(*args, env=dict(base, **env), cmd=box.pack)

    p.pack, p.t, p.calls = pack, t, calls
    return p


def head(s: str) -> str:
    return s.splitlines()[0] if s else ""


def test_pack_next_event(pk):
    r = pk()
    assert r.rc == 0
    assert "🎒 🪩 Kalia Dunez · Fri 06.03 (in 4 d)" in head(r.out)  # artist typo (fuzzy) -> rave, soonest typed event
    assert "🪪 Official photo ID" in r.out  # 18+/ID flag
    assert "🔋 Powerbank" in r.out  # train home 05:10
    assert r.out.count("Earplugs") == 1  # not doubled (the template has them)
    assert "Toothbrush" not in r.out  # a night train home is no night away
    assert "Passport" not in r.out  # not abroad
    assert "Climbing" not in r.out  # an untyped earlier event is skipped
    assert r.out.splitlines()[1] == "🪪 Official photo ID"  # extras first, then the template
    r = pk(FAIL_RR="1")
    assert r.rc == 0
    assert "Mira Hochziit" in head(r.out)  # no artist list -> the next typed event is the wedding
    assert "Nothing to pack for in the next 10 days" in pk(TG_CMD_NOW="2026-03-09 09:00").out
    assert "Customer visit (Gent) · Fri 20.03 (in 10 d)" in head(pk(TG_CMD_NOW="2026-03-10 09:00").out)  # day 10 counts


def test_pack_type_words_list(pk):
    t = pk.t
    out = pk("wedding").out
    assert "🎒 💍 Mira Hochziit · Sat 07.03 (in 5 d)" in head(out)  # the calendar event, not the trip file's copy
    assert "Powerbank" not in out  # train home 23:20 is no night train
    assert "Toothbrush" not in out  # ends 01:00
    out = pk("business").out
    assert "🎒 💼 🇧🇪 Customer visit (Gent) · Fri 20.03" in head(out)
    assert "🪥 Toothbrush" in out  # night away
    assert out.count("Passport") == 1
    assert out.count("EUR") == 1
    assert "🔌 Phone charger" not in out  # the template charger covers the extra
    out = pk("beach").out
    assert "🎒 🏝 Island trip · Wed 25.03 (in 23 d)" in head(out)  # the long trip from trip-conditions
    assert "🛂 Passport" in out
    assert "💶 EUR cash" in out
    assert nlines(out) == 7  # 11 items fit in 10 lines (paired)
    assert " · " in out
    assert "🪩 Kalia Dunez" in head(pk("club").out)  # alias club -> rave
    assert "Kalia Dunez" in head(pk("dunes").out)  # WORDS find an event by its leg (artist name)
    assert "Climbing: no list fits it" in pk("Climbing").out
    assert "No list or event called “zzz”" in pk("zzz").out
    out = pk("list").out
    assert "🪩 rave · Rave / club night (3)" in out
    assert "/pack TYPE shows one" in out
    assert nlines(out) == 6
    assert "🎒 💼 Business day abroad" in head(pk("business", FAIL_CAL="3").out)  # no cache, no such trip: template
    assert "🎒 💍 Mira's wedding" in head(pk("wedding", FAIL_CAL="3").out)  # no cache: the trip file's wedding
    assert "Island trip" in pk("beach", FAIL_CAL="3").out
    r = pk(FAIL_CAL="1")
    assert r.rc == 3
    assert "⚠️ /pack failed: calendar" in r.out
    r = pk(TG_CMD_PACK_TEMPLATES=t / "missing.json")
    assert r.rc == 3
    assert "no packing templates yet" in r.out
    for a in ((), ("wedding",), ("business",), ("beach",), ("list",)):
        assert nlines(pk(*a).out) <= 10, a


def test_pack_todo(pk):
    t, calls = pk.t, pk.calls

    def adds():
        return [ln for ln in calls.read_text().splitlines() if ln.startswith("todo add")]

    calls.write_text("")
    r = pk.pack("--todo", "wedding", FAKE_HA_LIST=t / "ha-none.json")
    assert r.rc == 4
    assert 'No HA to-do list "Packing" exists' in r.out
    assert adds() == []
    calls.write_text("")
    r = pk.pack("--todo", "wedding", "--dry-run", FAKE_HA_LIST=t / "ha-pack.json")
    assert r.rc == 0
    assert "todo.packing: would add 2, 1 already there." in r.out
    assert adds() == []
    calls.write_text("")
    r = pk.pack("--todo", "wedding", FAKE_HA_LIST=t / "ha-pack.json")
    assert r.rc == 0
    assert "todo.packing: added 2, 1 already there." in r.out
    assert sum(bool(re.search(r"^todo add .* --list todo.packing$", ln)) for ln in adds()) == 2
    assert "todo add 👔 Suit --list todo.packing" in calls.read_text()
    r = pk.pack("--todo", "wedding", FAKE_HA_LIST=t / "ha-pack.json", FAKE_HA_DENY="1")
    assert r.rc == 5
    assert "todo-lists.json" in r.out
    assert head(pk.pack("wedding").out) == "🎒 💍 Mira Hochziit · Sat 07.03 (in 5 d)"  # without --todo = the card
    assert pk.pack("--dry-run").rc == 2


# --------------------------------------------------------------------------- /week (text card)


@pytest.fixture
def week(tmp_path):
    box = Box(tmp_path, profile=True)
    t = tmp_path / "wk"
    t.mkdir()
    return Week(box, t)


def test_week_text_card(week):
    r = week()
    assert r.rc == 0
    assert r.out == WEEK_TEXT
    calls = week.calls.read_text()
    for want in (
        "calcache show --days 8 --json", "train-watch list --json", "sleepin status --days 7 --json --at 2031-03-05 12:00",
        "bills list --open --json", "gift-radar list --days 7 --date 2031-03-05",
    ):  # fmt: skip
        assert want in calls, want
    assert len(calls.splitlines()) == 5  # five sources, read verbs only
    for bad in (
        "Past meeting", "Too far ahead", "Oldtown", "Phone Co", "Test Power", "Water Works", "Far Later", "80.00", "Person",
        "Bobby", "Testcity (", "@",
    ):  # fmt: skip
        assert bad not in r.out, bad
    assert r.out.count("18:10") == 1  # the calendar leg isn't doubled by train-watch's copy
    assert oct((week.state / "week-weather.json").stat().st_mode & 0o777) == "0o600"  # weather cache 0600
    assert [p for p in week.box.home.rglob("*") if p.is_file()] == []  # nothing written under HOME


def test_week_weather(week):
    fx = week.fx
    week()
    assert "Wed 05.03 ☀️ 12°/3°:" in week(wx=fx / "wx-other.json").out  # fresh cache (< 3 h) reused, no fetch
    week.cache_age(36000)
    assert "Sun 09.03 🌨 -1°/-7° 💧60%:" in week(wx=fx / "missing.json").out  # source down, cache 10 h: still cached
    out = week(wx=fx / "wx-other.json").out
    assert "Wed 05.03 🌧 1°/0° 💧99%:" in out  # cache 10 h old + source up: refreshed
    assert "Thu 06.03: 📍 Otherburg" in out  # the refreshed forecast lacks the other days: no weather there
    week.cache_age(80 * 3600)
    r = week(wx=fx / "missing.json")
    assert r.rc == 0
    assert not re.search("°|⚠️", r.out)  # cache 80 h old + source down: no weather, no warning
    assert "Wed 05.03: 🏢 office · till 14:00 Long workshop" in r.out
    for p in week.state.iterdir():
        p.unlink()
    assert not re.search("°|⚠️", week(wx=fx / "missing.json").out)  # no cache: quiet
    out = week(wx="off").out
    assert ("°" in out, list(week.state.iterdir())) == (False, [])  # off: no weather, nothing cached


def test_week_failing_sources_and_clock_change(week):
    fx = week.fx
    r = week(FAIL_BILLS="1")
    assert (r.rc, r.out.splitlines()[-1]) == (0, "⚠️ Not checked: bills")
    assert "💳" not in r.out
    assert week(FAIL_SLEEP="1", FAIL_GIFT="2").out.splitlines()[-1] == "⚠️ Not checked: late nights, birthdays"
    r = week(FAIL_LEGS="1")
    assert r.rc == 0
    assert not re.search("Not checked|07:15", r.out)  # train-watch down: quiet (the calendar has the legs)
    r = week(FAIL_CAL="1")
    assert r.rc == 3
    assert "⚠️ /week failed: calendar: calcache failed." in r.out
    r = week(FAIL_CAL="3")
    assert (r.rc, r.out.splitlines()[-1]) == (0, "⚠️ Not checked: calendar (no cache yet)")
    assert "🚆 18:10 Home → Testburg (Concert)" in r.out  # legs from train-watch still shown
    r = week(
        wx="off", FAKE_CAL=fx / "none.json", FAKE_LEGS=fx / "none.json", FAKE_SLEEP=fx / "none.json",
        FAKE_BILLS=fx / "none.json", FAKE_GIFT=fx / "none.txt",
    )  # fmt: skip
    assert r.rc == 0
    assert sum(ln.endswith(": free") for ln in r.out.splitlines()) == 7  # every day free
    assert r.out.splitlines()[-1] == "⚠️ Not checked: late nights"  # sleepin's [] -> Not checked
    out = week(now="2031-03-27 12:00", wx="off").out
    assert "Sun 30.03: 🕒 clocks forward 1 h" in out  # DST start in the window
    assert out.count("🕒") == 1
    assert week.box.run("week", "extra").rc == 2  # an argument -> usage
