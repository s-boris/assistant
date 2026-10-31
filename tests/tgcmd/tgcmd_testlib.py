# ruff: noqa: E501  (fixture data and expected cards copied verbatim; long strings)
"""Shared helpers for the tg-cmd tests: a staged tg-cmd (+ pack) under a temp HOME, fake tools, shell-like runs.

Staging: tmp/stage/bin holds copies of the repo's scripts/tg-cmd and scripts/pack, secretscan.py, and lib/ with the
repo's legacy lib modules (cardimg, healthlib, ha_privacy, usage, assistant_profile, ...) and core, as deployed in
~/bin (nothing from the live ~/bin). tg-cmd runs by its shebang (python3 on PATH); exec records go to a scratch
USAGE_STATE. Every fake tool is a small script in a temp dir named by TG_CMD_BIN."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FIX = Path(__file__).with_name("fixtures")
PROFILE = REPO / "tests" / "legacy" / "profile" / "fixtures" / "profile.json"  # synthetic, never the real one
CLEAR = (
    "TG_CMD_",
    "TGCMD_",
    "FAKE_",
    "FAIL_",
    "CARDIMG_",
    "ASSISTANT_PROFILE",
    "USAGE_STATE",
    "TELEGRAM_INSTANT_KEYBOARD",
    "HA_HEALTH_DIR",
    "REMIND_",
    "PACK_",
    "UNRAID_CAPACITY_STATE_DIR",
    "CAT_CORNER_STATE_DIR",
    "KEEP_CARDS",
)


def stage(d: Path) -> Path:
    """bin/tg-cmd, bin/pack, bin/secretscan.py and bin/lib/ (legacy lib modules + core) from the repo."""
    b = d / "bin"
    lib = b / "lib"
    lib.mkdir(parents=True)
    for n in ("tg-cmd", "pack"):
        shutil.copy2(REPO / "scripts" / n, b / n)
    shutil.copy2(REPO / "scripts" / "secretscan_py", b / "secretscan.py")
    for p in (REPO / "scripts" / "lib" / "legacy").glob("*.py"):
        shutil.copy2(p, lib / p.name)
    (lib / "core").symlink_to(REPO / "core")
    return b


def script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


def nlines(s: str) -> int:
    """Lines as `printf '%s\\n' "$s" | wc -l` counts them."""
    return s.count("\n") + 1


@dataclass
class R:
    out: str
    err: str
    rc: int

    def j(self):
        return json.loads(self.out)


class Box:
    """One temp HOME, a scratch usage store and the staged tg-cmd; run() works like `out=$(cmd)` in the shell."""

    def __init__(self, tmp: Path, profile: bool = False):
        self.t = tmp
        self.bin = stage(tmp / "stage")
        self.tgcmd = str(self.bin / "tg-cmd")
        self.pack = str(self.bin / "pack")
        self.home = tmp / "home"
        self.home.mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith(CLEAR)}
        env.update(HOME=str(self.home), USAGE_STATE=str(tmp / "usage"))
        if profile:
            env["ASSISTANT_PROFILE"] = str(PROFILE)
        self.env = env

    def run(self, *args: str, env: dict | None = None, merge: bool = False, cmd: str | None = None, timeout=120) -> R:
        e = dict(self.env)
        for k, v in (env or {}).items():
            if v is None:
                e.pop(k, None)
            else:
                e[k] = str(v)
        p = subprocess.run(
            [cmd or self.tgcmd, *args],
            env=e,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if merge else subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
        return R(p.stdout.rstrip("\n"), (p.stderr or "").rstrip("\n"), p.returncode)

    def py3(self, code: str, *args: str, env: dict | None = None) -> str:
        """python3 (PATH's, the tools' interpreter) -c CODE ARGS; stdout without trailing newlines."""
        e = dict(self.env, **{k: str(v) for k, v in (env or {}).items()})
        p = subprocess.run(["python3", "-c", code, *args], env=e, capture_output=True, text=True, timeout=120)
        assert p.returncode == 0, p.stderr
        return p.stdout.rstrip("\n")

    def png_size(self, path) -> str:
        return self.py3(
            "from PIL import Image; import sys; i = Image.open(sys.argv[1]); print(f'{i.width}x{i.height}')", str(path)
        )

    def usage_lines(self) -> list[str]:
        spool = self.t / "usage" / "spool"
        return (
            [ln for p in sorted(spool.glob("*.jsonl")) for ln in p.read_text().splitlines()] if spool.is_dir() else []
        )


# --------------------------------------------------------------------------- shared worlds


def cat_history(d: Path) -> None:
    d.mkdir(parents=True, exist_ok=True)
    rows = [
        json.dumps({
            "date": f"2031-03-{n:02d}", "complete": True, "cycles": 5, "drawer_end": 40.0, "faults": 0,
            "feeders": {"feeder_x": {"feeds": 2, "food_g": 20.0}},
            "pets": {"sensor.mittens_visits_today": {"visits": 5 + n, "weight_kg": float(f"4.0{n}")}},
        })
        for n in range(1, 5)
    ]  # fmt: skip
    rows.append(
        '{"date": "2031-03-05", "complete": false, "cycles": 2, "drawer_end": 85.0, "faults": 1, '
        '"feeders": {"feeder_x": {"feeds": 1, "food_g": 10.0}}, "pets": {"sensor.mittens_visits_today": '
        '{"visits": 3, "weight_kg": 4.1}}}'
    )
    (d / "history.jsonl").write_text("\n".join(rows) + "\n")


NOW = "2031-03-05 12:00"
WEEK_CAL = """[{"id": "a1", "title": "@Testcity", "start": "2031-03-05", "end": "2031-03-06", "all_day": true, "flags": {}},
 {"id": "a2", "title": "@Otherburg", "start": "2031-03-06", "end": "2031-03-07", "all_day": true, "flags": {}},
 {"id": "a3", "title": "Past meeting", "start": "2031-03-05T09:00:00+01:00", "end": "2031-03-05T10:00:00+01:00", "all_day": false, "flags": {}},
 {"id": "a4", "title": "Long workshop", "start": "2031-03-04T20:00:00+01:00", "end": "2031-03-05T14:00:00+01:00", "all_day": false, "flags": {}},
 {"id": "a5", "title": "🚆 Home → Testburg (Concert)", "start": "2031-03-05T18:10:00+01:00", "end": "2031-03-05T19:00:00+01:00", "all_day": false, "flags": {"travel": "train"}},
 {"id": "a6", "title": "Concert night", "start": "2031-03-05T19:30:00+01:00", "end": "2031-03-05T23:00:00+01:00", "all_day": false, "town": "Testburg", "flags": {}},
 {"id": "a7", "title": "🚆 Testburg → Home (after Concert)", "start": "2031-03-05T23:40:00+01:00", "end": "2031-03-06T00:30:00+01:00", "all_day": false, "flags": {"travel": "train"}},
 {"id": "a8", "title": "Ski week", "start": "2031-03-08", "end": "2031-03-11", "all_day": true, "flags": {}},
 {"id": "a9", "title": "✈️ Testport → Skiville", "start": "2031-03-09T07:00:00+01:00", "end": "2031-03-09T08:30:00+01:00", "all_day": false, "flags": {"travel": "flight"}},
 {"id": "a10", "title": "Car to the garage", "start": "2031-03-10T08:00:00+01:00", "end": "2031-03-10T08:30:00+01:00", "all_day": false, "flags": {"travel": "drive"}},
 {"id": "a11", "title": "Too far ahead", "start": "2031-03-12T10:00:00+01:00", "end": "2031-03-12T11:00:00+01:00", "all_day": false, "flags": {}}]
"""  # noqa: E501
WEEK_LEGS = """[{"id": "l1", "from": "home", "to": "Testburg", "dep": "2031-03-05T18:10:00+01:00", "arr": "2031-03-05T19:00:00+01:00", "title": "🚆 Home → Testburg (Concert)"},
 {"id": "l2", "from": "home", "to": "Testcity", "dep": "2031-03-06T07:15:00+01:00", "arr": "2031-03-06T08:05:00+01:00", "title": "🚆 Home → Testcity"},
 {"id": "l3", "from": "home", "to": "Oldtown", "dep": "2031-03-04T07:15:00+01:00", "arr": "2031-03-04T08:05:00+01:00", "title": "🚆 Home → Oldtown"}]
"""  # noqa: E501
WEEK_SLEEP = """{"now": "2031-03-05 12:00", "active": false, "date": "2031-03-05", "why": "send", "until": null, "ended": null, "sources": [],
 "queue": {"count": 0, "tags": {}, "oldest": null},
 "ahead": [{"date": "2031-03-06", "off": false, "until": "12:30", "sources": [{"kind": "calendar", "what": "Testburg → Home", "end": "2031-03-06 00:30"}]},
           {"date": "2031-03-08", "off": true, "until": "12:30", "sources": [{"kind": "calendar", "what": "x", "end": "2031-03-08 01:00"}]},
           {"date": "2031-03-12", "off": false, "until": "11:00", "sources": [{"kind": "hold", "what": "by hand", "end": "2031-03-12 11:00"}]}]}
"""  # noqa: E501
WEEK_BILLS = """[{"id": 1, "payee": "Phone Co", "due": "2031-03-01", "amount": "12.00", "currency": "CHF"},
 {"id": 2, "payee": "Test Power AG", "due": "2031-03-07", "amount": "80.00", "currency": "CHF"},
 {"id": 3, "payee": "Water Works", "due": "2031-03-07", "amount": "45.50", "currency": "CHF"},
 {"id": 4, "payee": "Far Later Ltd", "due": "2031-04-01", "amount": "9.00", "currency": "CHF"}]
"""
WEEK_WX = """{"daily": {"time": ["2031-03-05", "2031-03-06", "2031-03-07", "2031-03-08", "2031-03-09", "2031-03-10", "2031-03-11", "2031-03-12"],
 "weather_code": [0, 2, 3, 61, 71, 95, 45, 1], "temperature_2m_max": [12.4, 10.0, 8.6, 6.0, -1.2, 9.5, 7.0, 11.0],
 "temperature_2m_min": [3.0, 2.2, 1.0, 0.4, -6.6, 4.0, 2.0, 3.0], "precipitation_probability_max": [0, 10, 20, 80, 60, 90, 5, 0]}}
"""  # noqa: E501
WEEK_TEXT = """🗓 Week · Wed 05.03 – Tue 11.03
Wed 05.03 ☀️ 12°/3°: 🏢 office · till 14:00 Long workshop · 🚆 18:10 Home → Testburg (Concert) · 19:30 Concert night · 🚆 23:40 Testburg → Home (after Concert) · 🌙 late night · 💳 1 bill overdue
Thu 06.03 ⛅ 10°/2°: 📍 Otherburg · 😴 sleep-in till 12:30 · 🚆 07:15 Home → Testcity
Fri 07.03 ☁️ 9°/1°: 🌙 late night · 🎂 Testa · 💳 2 bills due
Sat 08.03 🌧 6°/0° 💧80%: Ski week (to 10.03)
Sun 09.03 🌨 -1°/-7° 💧60%: ✈️ 07:00 Testport → Skiville
Mon 10.03 ⛈ 10°/4° 💧90%: 🚗 08:00 Car to the garage
Tue 11.03 🌫 7°/2°: 🌙 late night"""  # noqa: E501
WEEK_SRC = {"CAL": "calcache", "LEGS": "train-watch", "SLEEP": "sleepin", "BILLS": "bills", "GIFT": "gift-radar"}


class Week:
    """week.sh's world: logging fakes printing FAKE_<VAR>, fixture files, the weather cache in TG_CMD_STATE."""

    def __init__(self, box: Box, t: Path):
        self.box, self.t = box, t
        self.bin, self.state, self.fx, self.cards, self.calls = (
            t / "bin",
            t / "state",
            t / "fx",
            t / "cards",
            t / "calls",
        )
        for d in (self.bin, self.state, self.fx, self.cards):
            d.mkdir(parents=True)
        for var, name in WEEK_SRC.items():
            script(
                self.bin / name,
                f'#!/usr/bin/env bash\nprintf \'%s %s\\n\' "{name}" "$*" >> "{self.calls}"\n'
                f'[ -n "${{FAIL_{var}:-}}" ] && {{ echo "{name}: broken" >&2; exit ${{FAIL_{var}}}; }}\n'
                f'cat "${{FAKE_{var}}}"\n',
            )
        fx = self.fx
        (fx / "cal.json").write_text(WEEK_CAL)
        (fx / "legs.json").write_text(WEEK_LEGS)
        (fx / "sleep.json").write_text(WEEK_SLEEP)
        (fx / "bills.json").write_text(WEEK_BILLS)
        (fx / "gift.txt").write_text(
            "Fri 07.03.2031  in   2 d  Testa Person                 notified\nThu 13.03.2031  in   8 d  Bobby Far\n"
        )
        (fx / "wx.json").write_text(WEEK_WX)
        (fx / "wx-other.json").write_text(
            '{"daily": {"time": ["2031-03-05"], "weather_code": [61], "temperature_2m_max": [1.0], '
            '"temperature_2m_min": [0.0], "precipitation_probability_max": [99]}}\n'
        )
        (fx / "none.txt").write_text("")
        (fx / "none.json").write_text("[]\n")
        self.full()

    def full(self):
        fx = self.fx
        self.env = dict(
            FAKE_CAL=fx / "cal.json", FAKE_LEGS=fx / "legs.json", FAKE_SLEEP=fx / "sleep.json", FAKE_BILLS=fx / "bills.json",
            FAKE_GIFT=fx / "gift.txt",
        )  # fmt: skip
        for p in self.state.iterdir():
            p.unlink()

    def __call__(self, *args, now=NOW, wx=None, pictures=False, **over):
        self.calls.write_text("")
        e = dict(self.env, **over)
        e.update(
            TG_CMD_BIN=self.bin, TG_CMD_NOW=now, TG_CMD_STATE=self.state, TG_CMD_WEATHER=wx or self.fx / "wx.json",
            CARDIMG_DIR=self.cards,
        )  # fmt: skip
        if pictures:
            e["TG_CMD_PICTURES"] = "1"
        return self.box.run("week", *args, env=e)

    def cache_age(self, secs: float):
        p = self.state / "week-weather.json"
        d = json.loads(p.read_text())
        d["ts"] = time.time() - secs
        p.write_text(json.dumps(d))
