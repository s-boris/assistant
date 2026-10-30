"""`GET /internal/now` / `phone ask now --json`: the phone's cached C0/C1 state in EXACTLY the `vault ha now --json`
key set (protocol/fixtures/vault/ha-now.shape.json), plus the phone extras `source`, `vpn`, `screen`, `steps_today`.
No phone round trip; values are coarse like the vault's (battery in 5 % steps, local minutes, zone names, bands)."""
import datetime as dt
import json
from zoneinfo import ZoneInfo

from . import flags

PHONE_EXTRAS = ("source", "vpn", "screen", "steps_today")
VAULT_KEYS = ("device", "place", "drive", "activity", "bluetooth", "wifi", "sleep", "last_sleep", "alarm", "battery",
              "on_call", "dnd", "signals", "pending", "last_update", "age_min", "counts", "connection")
DEFAULT_ZONE_NAMES = {"home": "Home", "office": "Work", "gym": "Gym", "station": "Station", "airport": "Airport"}
STATE_KEYS = ("battery", "net", "dnd", "ringer", "screen", "alarm", "car", "bt", "place", "activity", "app")
ACTIVITY = {"still", "walking", "running", "vehicle", "bicycle"}       # tilting/unknown -> keep the last real one
BATTERY_LOW = 15                                                      # the vault's threshold
AWAKE_RECENT_S = 3 * 3600


def tz_of(cfg):
    try:
        return ZoneInfo(cfg.get("tz") or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def iso(ts, tz):
    return dt.datetime.fromtimestamp(ts, tz).isoformat(timespec="seconds") if ts else None


def minute(ts, tz):
    return dt.datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%dT%H:%M") if ts else None


def zone_name(zone, cfg):
    """App zone ('office', 'venue:bern-expo') -> the HA-style zone name the vault would print ('Work')."""
    names = dict(DEFAULT_ZONE_NAMES, **(cfg.get("zone_names") or {}))
    if zone in names:
        return names[zone]
    base, _, sub = zone.partition(":")
    if base in names and not sub:
        return names[base]
    if sub:
        return "%s %s" % (base.capitalize(), sub.replace("-", " ").replace("_", " ").title())
    return base.capitalize()


def battery_view(b):
    if not b or b.get("level") is None and b.get("charging") is None:
        return None
    lv = b.get("level")
    level = int(round(lv / 5.0) * 5) if isinstance(lv, (int, float)) else None
    charger = b.get("charger")
    return {"level": level,
            "band": None if level is None else "low" if level <= BATTERY_LOW else "mid" if level <= 50 else "high",
            "charging": b.get("charging"),
            "charger": charger if charger in ("ac", "usb", "wireless", "dock", "none") else
            ("none" if b.get("charging") is False else None)}


def place_view(p, p_at, now, cfg, tz):
    if not p or not p.get("zone"):
        return None
    zone = p["zone"]
    kind = flags.place_kind(zone)
    if kind not in ("home", "zone", "away"):
        return None
    band = p.get("band")
    band = None if kind == "home" or band in (None, "home", "") else band if band in ("near", "region", "far") else None
    fix = p.get("fix_age_min")
    fix = None if fix is None else int(fix + max(0, now - (p_at or now)) // 60)
    return {"kind": kind, "zone": None if kind == "away" else zone_name(zone, cfg), "band": band,
            "town": None if kind == "home" else p.get("town"),
            "flag": "home" if kind == "home" else "far" if band == "far" else "away",
            "since": iso(p.get("since"), tz), "fix_age_min": fix}


def wifi_view(net, place):
    if not net or "type" not in net:
        return None
    if net["type"] != "wifi":
        return "none"
    if place and place["kind"] == "home":
        return "home"
    if place and place.get("zone") == "Work":
        return "work"
    return "other"


def sleep_view(ctx, st, now):
    """awake | asleep | None: awake after an interaction/sleep_end/unlock in the last 3 h; asleep only from a
    health sleep window that has not ended."""
    aw, _ = ctx.state("flag.awake")
    last_unlock = (st.get("screen") or {}).get("last_unlock")
    recent = max((aw or {}).get("at") or 0, last_unlock or 0)
    if recent and now - recent <= AWAKE_RECENT_S:
        return "awake"
    if (st.get("screen") or {}).get("on"):
        return "awake"
    return None


def drive_view(stored, ctx, now, tz):
    """{active, since, minutes}: evaluated now (flags.drive_state: its timers end a drive without a frame), else the
    stored flag.drive. since is the stored real start; minutes count from it (never now - stored minutes)."""
    d = None
    if ctx is not None:
        try:
            d = flags.drive_state(ctx)
        except Exception:
            d = None
    if d is None:
        d = dict(stored or {})
        if not d.get("until") or now >= int(d["until"]):      # an old-format flag (minutes only) or one run out
            d = {}
    since = d.get("since") if d.get("active") else None
    return {"active": bool(d.get("active")), "since": iso(since, tz),
            "minutes": max(0, int(now - since) // 60) if since else None}


def now_view(db, dev, cfg, online=False, connected_since=None, now=None, ctx=None):
    """dev = devices row (or None). -> dict with VAULT_KEYS + PHONE_EXTRAS."""
    tz = tz_of(cfg)
    now = int(now or dt.datetime.now().timestamp())
    out = {k: None for k in VAULT_KEYS}
    out.update({"bluetooth": None, "signals": [], "pending": [], "counts": {"events": 0, "collapsed": 0, "observed": 0},
                "source": "phone", "vpn": None, "screen": None, "steps_today": None})
    if dev is None:
        out["connection"] = {"feed": "phone", "state": "no_device", "error": "no paired device", "connected_since": None,
                             "subscribed": 0, "reconnects": 0, "last_poll": None, "device": None}
        return out
    dev_id = dev["id"]
    st, at = {}, {}
    for r in db.all("SELECT k, v, at FROM state WHERE device_id=?", dev_id):
        st[r["k"]], at[r["k"]] = json.loads(r["v"]), r["at"]
    data_keys = [k for k in STATE_KEYS if k in st]
    last = max([at[k] for k in data_keys] or [0])
    place = place_view(st.get("place"), at.get("place"), now, cfg, tz)
    car_known = "car" in st or "activity" in st
    drive = None
    if car_known:
        drive = drive_view(st.get("flag.drive"), ctx, now, tz)
    act = (st.get("activity") or {}).get("kind")
    if act not in ACTIVITY:
        act = (st.get("flag.activity_last") or {}).get("kind")
    sl = db.one("SELECT v FROM daily WHERE device_id=? AND metric='sleep' ORDER BY day DESC LIMIT 1", dev_id)
    last_sleep = None
    if sl:
        s = json.loads(sl["v"])
        if s.get("min") is not None:
            last_sleep = {"hours": round(s["min"] / 60.0 * 2) / 2, "ended": minute(s.get("end"), tz)}
    steps = db.one("SELECT v FROM daily WHERE device_id=? AND metric='steps' AND day=?", dev_id,
                   dt.datetime.fromtimestamp(now, tz).strftime("%Y-%m-%d"))
    dnd = st.get("dnd")
    ev_n = db.one("SELECT COUNT(*) n FROM events WHERE device_id=?", dev_id)["n"]
    out.update({
        "device": dev["name"] or dev["model"],
        "place": place,
        "drive": drive,
        "activity": act,
        "bluetooth": list(st["bt"]) if "bt" in st else None,
        "wifi": wifi_view(st.get("net"), place),
        "sleep": sleep_view(ctx, st, now) if ctx else None,
        "last_sleep": last_sleep,
        "alarm": minute((st.get("alarm") or {}).get("next"), tz),
        "battery": battery_view(st.get("battery")),
        "on_call": None,                              # C3: the app never reads call state
        "dnd": None if dnd in (None, "unknown") else dnd != "off",
        "signals": sorted(data_keys),
        "pending": [],
        "last_update": iso(last, tz),
        "age_min": int((now - last) // 60) if last else None,
        "counts": {"events": ev_n, "collapsed": 0, "observed": len(data_keys)},
        "connection": {"feed": "phone", "state": "connected" if online else "offline", "error": None,
                       "connected_since": iso(connected_since, tz), "subscribed": len(data_keys),
                       "reconnects": int((st.get("app") or {}).get("reconnects_d") or 0),
                       "last_poll": iso(dev["last_seen"], tz), "device": dev["name"] or dev["model"]},
        "vpn": (st.get("net") or {}).get("vpn"),
        "screen": None if "screen" not in st else ("on" if st["screen"].get("on") else "off"),
        "steps_today": int(steps["v"]) if steps else None,
    })
    return out


def fresh(view, max_age_min=30):
    """The rule whereabouts uses: the phone answer counts when its state is <= max_age_min old and it has a place."""
    return bool(view and view.get("age_min") is not None and view["age_min"] <= max_age_min and view.get("place"))
