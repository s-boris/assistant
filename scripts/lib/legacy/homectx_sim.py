"""homectx_sim: replay Home autopilot 2's scene rules over recorded history (read-only, no HA writes).

Inputs (a private dir, default ~/.local/state/home-autopilot2/hist, filled by `home-ctx pull`): one `ha history --json`
file per entity (<entity_id>.json) plus vault_ha_events.json (the vault's coarse HA feed: zone / drive / sleep events).
Phone signals with no 7-day history yet (screen, dnd, sleep-in) use stated proxies: screen off = phone charging for
SCREEN_PROXY_MIN, DND unknown (so bedtime needs >= 23:00), sleep-in morning = home between 01:00 and 06:00.
The rule functions are pure (tests feed them synthetic timelines); load() + run() do the replay.
"""
import json
import os
from bisect import bisect_right
from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

from homectx import TZ  # noqa: E402  (the owner's zone: HOME_CTX_TZ, else the profile's locale.tz)
HIST = os.path.expanduser(os.environ.get("HOME_AP2_HIST", "~/.local/state/home-autopilot2/hist"))
ENTITIES = os.path.expanduser(os.environ.get("HOME_AP2_ENTITIES", "~/.config/home-ctx/entities.json"))


def entities(path=None):
    """his entity ids live in a private config (never in this public file)."""
    try:
        with open(path or ENTITIES) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


E = entities()
LR_LIGHTS = tuple(E.get("lr_lights") or ())
OTHER_LIGHTS = tuple(E.get("other_lights") or ())
TVS = tuple(E.get("tvs") or ())
SPEAKERS = tuple(E.get("speakers") or ())     # home speakers only: a music-account entity plays on his phone/car too
ON_STATES = ("on", "playing", "paused", "idle", "buffering")
SCREEN_PROXY_MIN = 10
LEAVE_NEAR_MIN = 20                 # still near home (or no phone band): wait 20 min (errands, gym)
LEAVE_FAR_MIN = 5                   # phone band region/far: off 5 min after leaving
STEP = 300


def tz():
    return ZoneInfo(TZ) if ZoneInfo else None


def ts(iso):
    return datetime.fromisoformat(iso).timestamp()


def loc(t):
    return datetime.fromtimestamp(t, tz())


def hm(t):
    d = loc(t)
    return d.hour * 60 + d.minute


class Timeline:
    """step function from (t, state) changes; before the first change = None (unknown)."""

    def __init__(self, changes):
        pts = sorted(((float(t), s) for t, s in changes), key=lambda p: p[0])   # stable: same-second order kept
        self.t = [p[0] for p in pts]
        self.s = [p[1] for p in pts]

    @classmethod
    def from_history(cls, d):
        return cls([(ts(c["time"]), c["state"]) for c in (d or {}).get("changes") or []])

    def at(self, t):
        i = bisect_right(self.t, t) - 1
        return self.s[i] if i >= 0 else None

    def changes_between(self, a, b):
        i = bisect_right(self.t, a)
        out = []
        while i < len(self.t) and self.t[i] <= b:
            out.append((self.t[i], self.s[i]))
            i += 1
        return out

    def first(self):
        return self.t[0] if self.t else None


def is_on(state):
    return state in ON_STATES


# --------------------------------------------------------------------------------------------- pure scene rules
def arrival_v1(dark, lr_on, t):
    if not dark or lr_on:
        return None
    return "path 15%" if hm(t) < 8 * 60 else "evening 70%"


def arrival_v2(dark, lr_on, tv_on, movie, t, last_fire=None):
    """-> (action or None, why)"""
    if last_fire is not None and t - last_fire < 45 * 60:
        return None, "already ran"
    if not dark:
        return None, "daylight"
    m = hm(t)
    if movie or tv_on:
        return None, "TV/movie on"
    if lr_on:
        return None, "lights already on"
    if 16 * 60 <= m < 23 * 60 + 30:
        return "evening warm", "dark evening"
    if m >= 23 * 60 + 30 or m < 5 * 60:
        return "path 10%", "late night"
    return None, "morning/day (no scene)"


def bedtime_ok(t, home, charging_since, tv_playing, movie, dnd=None, screen_off_since=None, now_proxy=True):
    """bedtime conditions at t. screen_off_since None + now_proxy: screen off = charging >= SCREEN_PROXY_MIN."""
    m = hm(t)
    if not (m >= 21 * 60 + 30 or m < 4 * 60):
        return False, "outside 21:30-04:00"
    if not home:
        return False, "not home"
    if charging_since is None:
        return False, "not charging"
    off_since = screen_off_since if screen_off_since is not None else (charging_since if now_proxy else None)
    if off_since is None or t - off_since < SCREEN_PROXY_MIN * 60:
        return False, "screen not off 10 min"
    if not (dnd == 1 or m >= 23 * 60 or m < 4 * 60):
        return False, "before 23:00 without DND"
    if tv_playing or movie:
        return False, "TV playing"
    return True, "ok"


def wake_target(alarm_local, workday, home, sleepin):
    """alarm_local: datetime of the next clock alarm. -> (fire datetime or None, why)"""
    if alarm_local is None:
        return None, "no alarm"
    m = alarm_local.hour * 60 + alarm_local.minute
    if not (5 * 60 <= m < 10 * 60 + 30):
        return None, "alarm outside 05:00-10:30"
    if not workday:
        return None, "not a workday"
    if not home:
        return None, "not home"
    if sleepin:
        return None, "sleep-in morning"
    return alarm_local - timedelta(minutes=10), "fade-in"


def leave_fire_after(far_after_min):
    """minutes after leaving the leaving scene fires: 5 once the phone band is region/far, else 20."""
    if far_after_min is not None and far_after_min <= LEAVE_NEAR_MIN:
        return max(LEAVE_FAR_MIN, far_after_min)
    return LEAVE_NEAR_MIN


def leaving_v2(away_min, on_list, tv_playing, fire_after=LEAVE_NEAR_MIN):
    if away_min is not None and away_min < fire_after:
        return None, "back within %d min" % fire_after
    if tv_playing:
        return None, "TV playing (guest?)"
    if not on_list:
        return None, "nothing on"
    return "auto-off", "%d on" % len(on_list)


# --------------------------------------------------------------------------------------------- replay
def load(hist=HIST):
    tl = {}
    for f in os.listdir(hist):
        if f.endswith(".json") and f.count(".") >= 2 and not f.startswith(("lb_", "vault_")):
            try:
                with open(os.path.join(hist, f)) as fh:
                    tl[f[:-5]] = Timeline.from_history(json.load(fh))
            except (OSError, ValueError, KeyError):
                pass
    try:
        with open(os.path.join(hist, "vault_ha_events.json")) as fh:
            ev = json.load(fh)
    except (OSError, ValueError):
        ev = []
    return tl, ev


def presence(ev):
    """vault zone events -> Timeline of 'home'/'away'/'zone' + list of (t, band) + drive stops."""
    pts, drives = [], []
    for e in ev:
        h = e.get("ha") or {}
        if e.get("label") == "zone" and h.get("place"):
            pts.append((ts(e["ts"]), h["place"]))
        elif e.get("label") == "drive" and str(h.get("state")) in ("stop", "end", "ended", "stopped"):
            drives.append(ts(e["ts"]))
    return Timeline(pts), sorted(drives)


def band_timeline(ev):
    pts = [(ts(e["ts"]), (e.get("ha") or {}).get("band") or "home" if (e.get("ha") or {}).get("place") == "home"
            else (e.get("ha") or {}).get("band")) for e in ev if e.get("label") == "zone"]
    return Timeline([(t, b) for t, b in pts if b])


def ha_arrivals(hist):
    """zone.home 'from 0' runs of autopilot_arrival from its logbook (lb_autopilot_arrival.json), if pulled."""
    try:
        with open(os.path.join(hist, "lb_autopilot_arrival.json")) as fh:
            rows = json.load(fh)
    except (OSError, ValueError):
        return []
    rows = rows.get("entries") or [] if isinstance(rows, dict) else rows
    return sorted(ts(r["time"]) for r in rows if "zone.home" in str(r.get("what")))


def run(hist=HIST, start=None, end=None):
    tl, ev = load(hist)
    pres, drive_ends = presence(ev)
    end = end or (datetime.now(tz()).replace(minute=0, second=0, microsecond=0).timestamp())
    start = start or end - 7 * 86400
    loc_from = pres.first()
    get = lambda e, t: tl[e].at(t) if e in tl else None  # noqa: E731

    def home_at(t):
        p = pres.at(t)
        if p is None:                        # before the vault feed: the first event says where he came from
            return True if loc_from and t < loc_from else None
        return p == "home"

    def dark(t):
        lux = get(E.get("lux"), t)
        try:
            dim = float(lux) < 30
        except (TypeError, ValueError):
            dim = False
        return get("sun.sun", t) == "below_horizon" or dim

    def lights_on(t, ents=LR_LIGHTS + OTHER_LIGHTS):
        return [e.split(".", 1)[1] for e in ents if get(e, t) == "on"]

    def tv_on(t):
        return any(is_on(get(e, t)) for e in TVS)

    def tv_playing(t):
        return any(get(e, t) == "playing" for e in TVS)

    out = {"window": [loc(start).isoformat(timespec="minutes"), loc(end).isoformat(timespec="minutes")],
           "location_known_from": loc(loc_from).isoformat(timespec="minutes") if loc_from else None}

    # arrivals / departures from the vault zone feed (+ HA zone.home arrivals the vault feed smoothed away)
    arr, dep, src = [], [], {}
    prev = None
    for t, p in zip(pres.t, pres.s):
        if start <= t <= end:
            if p == "home" and prev != "home":
                arr.append(t)
            if prev == "home" and p != "home":
                dep.append(t)
        prev = p
    for t in ha_arrivals(hist):
        if start <= t <= end and not any(abs(t - a) < 300 for a in arr):
            arr.append(t)
            src[t] = "HA zone flap"
    arr.sort()
    bands = band_timeline(ev)

    rows, offs = [], []
    for t in dep:
        nxt = [a for a in arr if a > t and t not in src and src.get(a) != "HA zone flap"]
        back = nxt[0] if nxt else None
        away = round((back - t) / 60) if back else None
        far = [ct for ct, cv in bands.changes_between(t, back or t + 6 * 3600) if cv in ("region", "far")]
        fire_after = leave_fire_after(round((far[0] - t) / 60) if far else None)
        probe = t + fire_after * 60
        on = lights_on(probe) + [e.split(".", 1)[1] for e in TVS if is_on(get(e, probe))] + \
            [e.split(".", 1)[1] for e in SPEAKERS if get(e, probe) == "playing"]
        v2, why = leaving_v2(away, on, tv_playing(probe), fire_after)
        saved = 0
        if v2:
            offs.append((probe, back))
            for name in on:
                e = next((x for x in LR_LIGHTS + OTHER_LIGHTS + TVS + SPEAKERS if x.endswith("." + name)), None)
                offs_at = [ct for ct, cv in (tl[e].changes_between(probe, back or end) if e in tl else [])
                           if not is_on(cv)]
                saved += ((offs_at[0] if offs_at else (back or end)) - probe) / 60
        t3 = t + 180
        on3 = lights_on(t3) + [e.split(".", 1)[1] for e in TVS if is_on(get(e, t3))]
        pres_on = get(E.get("presence"), t3) == "on"
        v1 = "prompt" if (on3 and not pres_on and (back is None or back > t3)) else None
        rows.append({"at": loc(t).strftime("%a %d.%m %H:%M"), "away_min": away, "fire_after_min": fire_after,
                     "on_at_fire": on, "v2": v2, "why": why, "v1_prompt": v1,
                     "on_minutes_saved": round(saved),
                     "v1_blocked_by_presence": bool(on3) and pres_on})
    out["leaving"] = rows

    rows, last2 = [], None
    for t in arr:
        lr = [e for e in LR_LIGHTS[:2] if get(e, t - 5) == "on"]        # just before: v1's own switch-on lands at t
        v1 = arrival_v1(dark(t), bool(lr), t)
        auto_off = any(back == t for _, back in offs)          # v2 switched them off while he was out
        v2, why = arrival_v2(dark(t), bool(lr) and not auto_off, tv_on(t - 5),
                             get(E.get("movie"), t - 5) == "on", t, last2)
        if src.get(t) == "HA zone flap":
            v2, why = None, "GPS flap: phone fence needs a dwell; " + why
        if v2:
            last2 = t
        gap = None
        prior = [d for d in drive_ends if t - 45 * 60 <= d <= t]
        if prior:
            gap = round((t - prior[-1]) / 60)
        rows.append({"at": loc(t).strftime("%a %d.%m %H:%M"), "source": src.get(t, "vault zone"), "dark": dark(t),
                     "v1": v1, "v2": v2, "why": why + (" (after v2 auto-off)" if auto_off and v2 else ""),
                     "drive_end_gap_min": gap})
    out["arrival"] = rows

    # bedtime: one row per night
    rows = []
    day0 = loc(start).replace(hour=21, minute=30, second=0, microsecond=0)
    if day0.timestamp() < start:
        day0 += timedelta(days=1)
    batt = tl.get(E.get("battery"))
    asleep = [ts(e["ts"]) for e in ev if e.get("label") == "sleep" and (e.get("ha") or {}).get("asleep") is True]
    n = day0
    while n.timestamp() + 6.5 * 3600 <= end + 1:
        a, b = n.timestamp(), n.timestamp() + 6.5 * 3600
        fire, charge_since, why_last = None, None, "no data"
        t = a
        while t <= b:
            st = batt.at(t) if batt else None
            charging = st in ("charging", "full")
            if charging and charge_since is None:
                # walk back to the real charge start
                cs = t
                for ct, cv in reversed(batt.changes_between(a - 6 * 3600, t)):
                    if cv in ("charging", "full"):
                        cs = ct
                    else:
                        break
                charge_since = cs
            elif not charging:
                charge_since = None
            ok, why_last = bedtime_ok(t, home_at(t), charge_since, tv_playing(t),
                                      get(E.get("movie"), t) == "on")
            if ok:
                fire = t
                break
            t += STEP
        row = {"night": n.strftime("%a %d.%m"), "fire": loc(fire).strftime("%H:%M") if fire else None,
               "why_not": None if fire else why_last}
        if fire:
            on = lights_on(fire)
            row["lights_on"] = on
            row["tv_on_idle"] = tv_on(fire) and not tv_playing(fire)
            last_off = None
            relit = False
            for e in LR_LIGHTS + OTHER_LIGHTS:
                for ct, cv in (tl[e].changes_between(fire, fire + 8 * 3600) if e in tl else []):
                    if cv == "off" and e.split(".", 1)[1] in on:
                        last_off = max(last_off or 0, ct)
                        break
                for ct, cv in (tl[e].changes_between(fire, fire + 30 * 60) if e in tl else []):
                    if cv == "on":
                        relit = True
            row["earlier_by_min"] = round((last_off - fire) / 60) if last_off else None
            row["still_up_risk"] = relit
            sl = [s for s in asleep if fire - 3 * 3600 <= s <= fire + 4 * 3600]
            row["asleep_detected"] = loc(sl[0]).strftime("%H:%M") if sl else None
        rows.append(row)
        n += timedelta(days=1)
    out["bedtime"] = rows

    # wake-up: one row per morning
    rows = []
    alarm = tl.get(E.get("next_alarm"))
    d = loc(start).replace(hour=4, minute=0, second=0, microsecond=0)
    if d.timestamp() < start:
        d += timedelta(days=1)
    while d.timestamp() <= end:
        raw = alarm.at(d.timestamp()) if alarm else None
        al = None
        try:
            al = datetime.fromisoformat(raw).astimezone(tz()) if raw and raw not in ("unavailable", "unknown") else None
        except ValueError:
            al = None
        if al and al.date() != d.date():
            al = None
        work = get(E.get("workday"), d.timestamp()) == "on"
        probe = (al - timedelta(minutes=10)).timestamp() if al else d.timestamp() + 3 * 3600
        home = home_at(probe)
        late = [x for x in arr if d.timestamp() - 3 * 3600 <= x <= d.timestamp() + 2 * 3600]
        fire, why = wake_target(al, work, home, bool(late))
        row = {"morning": d.strftime("%a %d.%m"), "alarm": al.strftime("%H:%M") if al else None, "workday": work,
               "fire": fire.strftime("%H:%M") if fire else None, "why": why}
        if fire:
            f = fire.timestamp()
            own = [ct for ct, cv in (tl[E.get("bedroom_lamp")].changes_between(f - 900, f + 3600)
                                     if E.get("bedroom_lamp") in tl else []) if cv == "on"]
            row["lamp_on_by_him"] = loc(own[0]).strftime("%H:%M") if own else None
        rows.append(row)
        d += timedelta(days=1)
    out["wakeup"] = rows

    # still-on nudges: v1 prompts vs v2 (only when auto-off is off or failed -> 0 expected with it on)
    out["still_on"] = {"v1_prompts": sum(1 for r in out["leaving"] if r["v1_prompt"]),
                       "v1_missed_by_cat_presence": sum(1 for r in out["leaving"] if r["v1_blocked_by_presence"]),
                       "v2_auto_off": sum(1 for r in out["leaving"] if r["v2"]),
                       "v2_device_minutes_saved": sum(r["on_minutes_saved"] for r in out["leaving"]),
                       "v2_prompts_expected": 0}
    return out
