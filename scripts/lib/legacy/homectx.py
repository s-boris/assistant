"""homectx: the phone-context feed for Home autopilot 2 (HA-native scenes with a gateway-fed sensor).

The phone app's coarse state (via phonegw, `phone ask now --json`) plus the sleep-in guard is folded into ONE compact
JSON string for the HA helper input_text.phone_context (max 255 chars). HA automations read it with from_json and treat
it as fresh while t is <= FRESH_S old; stale or missing = they fall back to HA's own signals (zone.home, Companion
battery_state / next_alarm). Coarse flags only: zone name, distance band, screen, dnd, charging, sleep-in. Never
coordinates, towns, stops or device names.

Keys: v (schema 1) · t (epoch of this build) · z (home|away|<zone name, lower>|null) · b (near|region|far|null) ·
scr (1 on / 0 off / null outside the night window) · so (epoch the screen went off, only while scr == 0) ·
dnd (1/0/null) · chg (1/0/null) · si (1 = sleep-in guard holds now) · age (phone state age, minutes).

Writes happen only with feed(apply=True) AND ~/.config/home-ctx/config.json {"write": true}; otherwise a dry run that
only keeps the local state file (screen-off tracking + last value). Exit codes in the CLI (~/bin/home-ctx).
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

ENTITY = "input_text.phone_context"
MAX_LEN = 255
FRESH_S = 1800                      # HA side: older than this = stale (same 30 min as whereabouts)
HEARTBEAT_S = 600                   # rewrite an unchanged value at least this often so HA keeps it fresh
NIGHT = (20 * 60, 5 * 60)           # screen is reported only 20:00-05:00 local (no daytime churn)
STATE_DIR = os.path.expanduser(os.environ.get("HOME_CTX_STATE", "~/.local/state/home-ctx"))
CONFIG = os.path.expanduser(os.environ.get("HOME_CTX_CONFIG", "~/.config/home-ctx/config.json"))


def owner_tz_name():
    """HOME_CTX_TZ (tests), else the owner's zone (profile locale.tz via assistant_profile beside this file), else UTC."""
    if os.environ.get("HOME_CTX_TZ"):
        return os.environ["HOME_CTX_TZ"]
    try:
        lib = os.path.dirname(os.path.realpath(__file__))
        if lib not in sys.path:
            sys.path.append(lib)
        import assistant_profile
        return assistant_profile.tz_name()
    except Exception:  # noqa: BLE001
        return "UTC"


TZ = owner_tz_name()
BANDS = ("near", "region", "far")
CONNECTED_MAX_AGE_MIN = 720         # a connected phone's unchanged state counts up to 12 h


def _local(ts):
    return datetime.fromtimestamp(ts, ZoneInfo(TZ)) if ZoneInfo else datetime.fromtimestamp(ts)


def in_night(ts):
    m = _local(ts).hour * 60 + _local(ts).minute
    start, end = NIGHT
    return m >= start or m < end


def _zone(place):
    if not isinstance(place, dict):
        return None
    kind = (place.get("kind") or place.get("place") or "").lower()
    if kind == "home":
        return "home"
    if kind == "away":
        return "away"
    if kind == "zone":
        z = str(place.get("zone") or "").strip().lower()
        return "".join(c for c in z if c.isalnum() or c in "_-")[:20] or "zone"
    return None


def _flag(v):
    if v is None:
        return None
    if isinstance(v, str):
        v = v.strip().lower()
        if v in ("on", "true", "yes", "1", "charging", "full"):
            return 1
        if v in ("off", "false", "no", "0", "discharging", "not_charging"):
            return 0
        return None
    return 1 if v else 0


def build(now_json, sleepin_active, prev_state, now=None, max_age_min=30):
    """-> (ctx dict, new local state). Pure: phone answer + sleep-in flag + previous local state."""
    now = int(now if now is not None else time.time())
    prev_state = dict(prev_state or {})
    d = now_json if isinstance(now_json, dict) else {}
    age = d.get("age_min")
    age = int(age) if isinstance(age, (int, float)) else None
    # the app sends state on change only: unchanged state of a phone that is connected right now is still current
    # (at home and idle nothing changes for hours); a disconnected phone counts only while <= max_age_min old
    conn = (d.get("connection") or {}).get("state") if isinstance(d.get("connection"), dict) else None
    phone_ok = d.get("source") == "phone" and age is not None and (
        age <= max_age_min or (conn == "connected" and age <= CONNECTED_MAX_AGE_MIN))
    ctx = {"v": 1, "t": now, "z": None, "b": None, "scr": None, "so": None, "dnd": None, "chg": None,
           "si": 1 if sleepin_active else 0, "age": age}
    scr_now = _flag(d.get("screen")) if phone_ok else None
    # screen-off tracking runs all day (so "off for 10 min" is right at 20:00 too); reported only at night
    if scr_now == 0:
        so = prev_state.get("so") if prev_state.get("scr") == 0 and prev_state.get("so") else now
    else:
        so = None
    new_state = {"scr": scr_now, "so": so}
    if phone_ok:
        place = d.get("place") or {}
        ctx["z"] = _zone(place)
        band = (place.get("band") or "") if isinstance(place, dict) else ""
        ctx["b"] = band if band in BANDS else None
        ctx["dnd"] = _flag(d.get("dnd"))
        bat = d.get("battery") if isinstance(d.get("battery"), dict) else {}
        ctx["chg"] = _flag(bat.get("charging"))
        if in_night(now):
            ctx["scr"] = scr_now
            ctx["so"] = so if scr_now == 0 else None
    return ctx, new_state


def encode(ctx):
    s = json.dumps({k: v for k, v in ctx.items() if v is not None}, separators=(",", ":"), sort_keys=True)
    if len(s) > MAX_LEN:
        raise ValueError("context too long (%d)" % len(s))
    return s


def same(a, b):
    """equal apart from t/age (the heartbeat decides when an unchanged value is rewritten)"""
    strip = lambda c: {k: v for k, v in (c or {}).items() if k not in ("t", "age")}  # noqa: E731
    return strip(a) == strip(b)


def needs_write(ctx, last, now=None):
    now = int(now if now is not None else time.time())
    if not last:
        return True, "first"
    if not same(ctx, last.get("ctx")):
        return True, "changed"
    if now - int(last.get("written_at") or 0) >= HEARTBEAT_S:
        return True, "heartbeat"
    return False, "unchanged"


# ------------------------------------------------------------------------------------------------ I/O (thin)
def _run(argv, timeout=20):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout
    except (OSError, subprocess.TimeoutExpired):
        return None, ""


def phone_now():
    rc, out = _run([os.environ.get("HOME_CTX_PHONE", "phone"), "ask", "now", "--json"])
    try:
        return json.loads(out) if rc == 0 else {}
    except ValueError:
        return {}


def sleepin_active():
    rc, _ = _run([os.environ.get("HOME_CTX_SLEEPIN", "sleepin"), "active"], timeout=10)
    return rc == 0


def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def write_enabled():
    return bool((load_json(CONFIG, {}) or {}).get("write"))


def ha_argv(value):
    return [os.environ.get("HOME_CTX_HA", "ha"), "call", "input_text.set_value", ENTITY, json.dumps({"value": value})]


def feed(apply=False, reason="manual", now=None, phone=None, sleepin=None):
    """-> result dict {ctx, value, write, why, applied, rc}. Dry run unless apply and the config allows writes."""
    now = int(now if now is not None else time.time())
    st_path = os.path.join(STATE_DIR, "state.json")
    st = load_json(st_path, {}) or {}
    ctx, local = build(phone if phone is not None else phone_now(),
                       sleepin if sleepin is not None else sleepin_active(), st.get("local"), now=now)
    value = encode(ctx)
    write, why = needs_write(ctx, st.get("last"), now=now)
    res = {"ctx": ctx, "value": value, "write": write, "why": why, "reason": reason, "applied": False, "rc": None,
           "would_run": " ".join(ha_argv(value)[:4]) + " '<value>'"}
    st["local"] = local
    if write and apply and write_enabled():
        rc, _ = _run(ha_argv(value), timeout=30)
        res["rc"], res["applied"] = rc, rc == 0
        if rc == 0:
            st["last"] = {"ctx": ctx, "written_at": now}
    elif write and not apply:
        st["dry_last"] = {"ctx": ctx, "at": now, "why": why}
    save_json(st_path, st)
    return res
