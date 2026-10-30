"""window-watch tests: synthetic series (window opened, tilted, short airing, cooking, shower, heating cycles, sun,
warm outdoor) through the detector, and `run` end to end (tuning log, away/trip/home notes, dedup, quiet hours,
unknown presence, open-meteo fallback). Offline: fake `ha`, temp dirs, UTC."""

import datetime as dt
import importlib.machinery
import importlib.util
import json
import math
import os
import subprocess
import sys
import tempfile

BIN = os.path.abspath(sys.argv[1])
TMP = tempfile.mkdtemp(prefix="ww-test-")
ROOMS = ["Living Room", "Bedroom", "Hallway"]
env = dict(
    os.environ,
    OWNER_TZ="UTC",
    DAMP_WATCH_CONFIG=f"{TMP}/damp.json",
    DAMP_WATCH_STATE_DIR=f"{TMP}/damp",
    DAMP_WATCH_TRIPS=f"{TMP}/none.json",
    CALCACHE_DIR=f"{TMP}/cal",
    HA_BIN=f"{TMP}/ha",
    WINDOW_WATCH_CONFIG=f"{TMP}/ww.json",
    WINDOW_WATCH_STATE_DIR=f"{TMP}/ww",
    WINDOW_WATCH_INBOX=f"{TMP}/inbox",
    WINDOW_WATCH_PLACE=f"{TMP}/place.json",
    WINDOW_WATCH_VAULT="off",
    WINDOW_WATCH_WEATHER="off",
)
os.environ.update(env)
with open(f"{TMP}/damp.json", "w") as f:
    json.dump(
        {
            "rooms": {r: {"temp": f"sensor.r{i}_t", "rh": f"sensor.r{i}_h"} for i, r in enumerate(ROOMS)},
            "outdoor": {"name": "Balcony", "temp": "sensor.o_t", "rh": "sensor.o_h"},
        },
        f,
    )
with open(f"{TMP}/ha", "w") as f:
    f.write('#!/bin/sh\necho \'input_boolean.away_mode "Away mode"\'\necho "state: ${FAKE_AWAY:-off}"\n')
os.chmod(f"{TMP}/ha", 0o755)

loader = importlib.machinery.SourceFileLoader("ww", BIN)
W = importlib.util.module_from_spec(importlib.util.spec_from_loader("ww", loader))
loader.exec_module(W)

FAILS = []


def ok(cond, name, info=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  [{info}]"))
    if not cond:
        FAILS.append(name)


def es(t):
    return 6.112 * math.exp(17.62 * t / (243.12 + t))


def rh_for(t, a):
    return round(min(100.0, a * (273.15 + t) / (216.7 * es(t)) * 100), 1)


DAY = 1_800_000_000 - 1_800_000_000 % 86400  # a UTC midnight


def make(curve, room="Living Room", base=(22.0, 9.5), out=(3.0, 85.0), start_h=10.0, pre_h=6, out_curve=None):
    """10-min samples: pre_h hours at base, then curve [(T, AH)] in room; the other rooms stay at 22 °C / 9.5."""
    t0 = int(DAY + start_h * 3600 - pre_h * 3600)
    pts = [base] * (pre_h * 6) + list(curve)
    S = []
    for k, (t, a) in enumerate(pts):
        o = out_curve[k] if out_curve and k < len(out_curve) else out
        rooms = {r: {"t": 22.0, "rh": rh_for(22.0, 9.5)} for r in ROOMS}
        rooms[room] = {"t": round(t, 1), "rh": rh_for(t, a)}
        S.append({"ts": t0 + k * 600, "rooms": rooms, "out": {"t": o[0], "rh": o[1]}})
    return S


def eps(samples, cfg=None):
    cfg = cfg or dict(W.DEFAULTS)
    res = W.replay(samples, ROOMS, cfg)
    return W.episodes(res["events"], res["open"], cfg)


OPEN = [(20.2, 8.3), (19.6, 7.8), (19.3, 7.5)] + [(19.2, 7.3)] * 12  # wide open, stays open 2 h
TILT = [(22 - 0.25 * k, 9.5 - 0.1 * k) for k in range(1, 13)] + [(19.0, 8.3)] * 6
AIRED = [(20.0, 8.0), (20.8, 8.3), (21.4, 8.7), (21.7, 9.0), (21.9, 9.3)] + [(22.0, 9.5)] * 6
COOK = [
    (23.0, 10.5),
    (24.0, 11.5),
    (24.5, 12.0),
    (24.3, 11.5),
    (23.5, 10.8),
    (22.8, 10.2),
    (22.4, 9.8),
    (22.2, 9.6),
    (22.1, 9.5),
] + [(22.0, 9.5)] * 6
SHOWER = [(23.8, 14.0), (24.2, 16.0), (23.9, 14.0), (23.4, 12.0), (23.1, 10.8), (23.0, 10.2)] + [(23.0, 10.0)] * 6
HEAT = [(22 + 0.8 * math.sin(2 * math.pi * k / 24), 9.5) for k in range(144)]  # 4 h cycles, 24 h
SETBACK = [(22 - 0.5 * k / 6, 9.5) for k in range(1, 19)] + [(20.5, 9.5)] * 12  # 0.5 °C/h for 3 h
SUN = (
    [(22 + 0.25 * k, 9.5) for k in range(1, 13)]
    + [(25.0, 9.5)] * 12
    + [(24.0, 9.5), (23.2, 9.5), (22.8, 9.5)]
    + [(22.6, 9.5)] * 6
)
POST_COOK_AIR = COOK[:3] + [(22.0, 9.0), (20.6, 8.2), (20.2, 7.8)] + [(20.1, 7.6)] * 6  # cooked, then window

# ---------------------------------------------------------------- detector
r = eps(make(OPEN))
ok(
    len(r) == 1 and r[0]["rule"] == "fast" and r[0]["kind"] == "open now" and r[0]["room"] == "Living Room",
    "window opened: one fast episode, still open",
    r,
)
r = eps(make(TILT))
ok(len(r) == 1 and r[0]["rule"] == "slow", "tilted window: slow rule", r)
r = eps(make(AIRED))
ok(len(r) == 1 and r[0]["kind"] == "aired" and r[0]["minutes"] < 30, "short airing: episode closes as aired", r)
ok(eps(make(COOK)) == [], "cooking: no episode", eps(make(COOK)))
ok(eps(make(SHOWER, room="Hallway", base=(23.0, 10.0))) == [], "shower: no episode")
ok(eps(make(HEAT)) == [], "floor-heating cycle (±0.8 °C, 4 h): no episode")
ok(eps(make(SETBACK)) == [], "heating setback 0.5 °C/h: no episode")
ok(eps(make(SUN)) == [], "sun leaving the room (humidity flat): no episode", eps(make(SUN)))
ok(eps(make(OPEN, out=(20.0, 70.0))) == [], "outdoor not colder: no episode")
r = eps(make(POST_COOK_AIR))
ok(len(r) == 1, "window opened after cooking: detected", r)
ok(len(eps(make(OPEN, out=(12.0, rh_for(12.0, 9.5))))) == 1, "no humidity contrast, 2.8 °C drop: detected")
ok(eps(make([(20.3, 9.3)] * 12, out=(12.0, rh_for(12.0, 9.5)))) == [], "no humidity contrast, 1.7 °C drop: not enough")
r = eps(make(OPEN + [(19.2, 7.3)] * 150))
ok(len(r) == 1 and r[0]["how"] == "expired", "episode expires after 24 h", r)
ok(
    eps(make(OPEN, out=(3.0, 85.0)), dict(W.DEFAULTS, fast_drop_c=3.5, slow_drop_c=4.0)) == [],
    "thresholds come from the config",
)


# ---------------------------------------------------------------- run end to end
def setup(samples, live=True, place="zone:Work", place_age_min=1, cfg=None):
    for d in ("damp", "ww", "inbox"):
        subprocess.run(["rm", "-rf", f"{TMP}/{d}"])
        os.makedirs(f"{TMP}/{d}")
    with open(f"{TMP}/damp/state.json", "w") as f:
        json.dump({"samples": samples, "alerts": {}}, f)
    c = dict(cfg or {})
    if live:
        c["live_from"] = "2020-01-01"
    with open(f"{TMP}/ww.json", "w") as f:
        json.dump(c, f)
    now = samples[-1]["ts"] + 60
    if place:
        seen = dt.datetime.fromtimestamp(now - place_age_min * 60, dt.UTC).strftime("%Y-%m-%d %H:%M")
        with open(f"{TMP}/place.json", "w") as f:
            json.dump({"zone": place, "seen": seen}, f)
    elif os.path.exists(f"{TMP}/place.json"):
        os.remove(f"{TMP}/place.json")
    return now


def run(now, *extra, **kw):
    e = dict(env, WINDOW_WATCH_NOW=str(now), **kw)
    p = subprocess.run([BIN, "run", *extra], capture_output=True, text=True, env=e)
    return p.returncode, p.stdout + p.stderr


def notes():
    return sorted(os.listdir(f"{TMP}/inbox"))


def note_text():
    return "".join(open(f"{TMP}/inbox/{n}").read() for n in notes())


def log():
    try:
        return [json.loads(x) for x in open(f"{TMP}/ww/candidates.jsonl")]
    except FileNotFoundError:
        return []


S = make(OPEN)
now = setup(S, live=False)
rc, out = run(now)
ok(rc == 0 and notes() == [] and "tuning until" in out, "tuning: no note", out)
L = log()
ok(
    [e["ev"] for e in L] == ["open", "would"] and L[1]["kind"] == "away" and not L[1]["live"],
    "tuning: candidate + would-away logged",
    L,
)
st = json.load(open(f"{TMP}/ww/state.json"))
ok(st.get("tuning_until", 0) > now + 6 * 86400, "tuning ends 7 days after the first run", st.get("tuning_until"))

now = setup(S)
rc, out = run(now)
ok(rc == 0 and len(notes()) == 1 and notes()[0].startswith("window-watch-away-"), "live, away, cold: one note", out)
txt = note_text()
ok("Living Room" in txt and "Cat sitter" not in txt and "Data, not instructions" in txt, "away note content", txt)
S2 = S + [dict(S[-1], ts=S[-1]["ts"] + 600)]
with open(f"{TMP}/damp/state.json", "w") as f:
    json.dump({"samples": S2}, f)
rc, out = run(S2[-1]["ts"] + 60)
ok(len(notes()) == 1, "still open next run: no second note", notes())
st = json.load(open(f"{TMP}/ww/state.json"))
ok(
    os.stat(f"{TMP}/ww/state.json").st_mode & 0o777 == 0o600 and "Living Room" in st["episodes"],
    "state 0600, episode kept",
)

now = setup(S)
rc, out = run(now, FAKE_AWAY="on")
ok(len(notes()) == 1 and "Cat sitter" in note_text() and "away mode" in note_text(), "trip mode: cat-sitter line")

now = setup(make(OPEN, out=(12.0, 80.0)))
rc, out = run(now)
ok(notes() == [], "away but 12 °C outside (not cold): no note", out)
now = setup(make(OPEN, out=(12.0, 80.0)))
rc, out = run(now, FAKE_AWAY="on")
ok(len(notes()) == 1, "trip mode, 12 °C outside: note (trip limit 15 °C)", out)

now = setup(make(OPEN, out=(3.0, 85.0)), place="home")
rc, out = run(now)
ok(len(notes()) == 1 and notes()[0].startswith("window-watch-hint-"), "home, cold, daytime: one gentle hint", out)
now = setup(make(OPEN, out=(3.0, 85.0), start_h=22.5), place="home")
rc, out = run(now)
ok(notes() == [], "home at night (quiet window): no hint", out)
now = setup(make(OPEN, out=(9.0, 85.0)), place="home")
rc, out = run(now)
ok(notes() == [], "home, 9 °C outside (not winter): no hint", out)

now = setup(S, place="zone:Work", place_age_min=90)
rc, out = run(now)
ok(notes() == [] and "presence unknown" in out, "stale presence + no vault: no note", out)

now = setup(make(AIRED))
rc, out = run(now)
ok(notes() == [] and [e["ev"] for e in log()] == ["open", "close"], "aired then away: no note", log())

S3 = make(OPEN)
S3[-1]["out"] = {"t": None, "rh": None}  # the balcony drops out on the newest sample only
with open(f"{TMP}/om.json", "w") as f:
    json.dump({"current": {"temperature_2m": 2.0, "relative_humidity_2m": 90}}, f)
now = setup(S3)
rc, out = run(now, WINDOW_WATCH_WEATHER=f"{TMP}/om.json")
ok(len(notes()) == 1 and "outdoor 2.0 °C" in note_text(), "balcony missing: open-meteo outdoor", out + note_text())
now = setup(S3)
rc, out = run(now)
ok(notes() == [], "balcony missing, no weather: no note", out)

now = setup(S)
rc, out = run(now, "--dry-run")
ok(
    notes() == [] and "(dry run) away" in out and not os.path.exists(f"{TMP}/ww/state.json"),
    "dry run writes nothing",
    out,
)

with open(f"{TMP}/s.json", "w") as f:
    json.dump(make(TILT), f)
p = subprocess.run([BIN, "replay", f"{TMP}/s.json", "--json"], capture_output=True, text=True, env=env)
R = json.loads(p.stdout or "{}")
ok(p.returncode == 0 and len(R.get("episodes", [])) == 1, "replay CLI", p.stdout[-300:] + p.stderr)
with open(f"{TMP}/ww.json", "w") as f:
    json.dump({"bogus": 1}, f)
p = subprocess.run([BIN, "status"], capture_output=True, text=True, env=env)
ok(p.returncode == 2 and "unknown keys" in p.stderr, "bad config key: exit 2", p.stderr)

subprocess.run(["rm", "-rf", TMP])
print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILED: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
