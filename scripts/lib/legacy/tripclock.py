"""tripclock: the time zone the owner is in at an instant (his home zone, or a trip's zone while he is away).

One source for "his local time": quiet hours, the sleep-in guard, location-reminder holds and the time of the
morning report follow it. Server, Home Assistant and log timestamps stay on the home zone (assistant_profile.tz()).

    import tripclock
    tripclock.zone(t, home)        # ZoneInfo he is in at t (aware datetime, epoch seconds or None = now)
    tripclock.local(t, home)       # t on his wall clock (aware)
    tripclock.away(t, home)        # True when that zone is not the home zone
    tripclock.in_quiet(t, 0, 8)    # inside his quiet hours (whole hours, "22-6" style wraps)
    tripclock.quiet_end(t, 8)      # the next end of his quiet hours at or after t (aware, in his zone)
    tripclock.wall(d, time, home)  # the instant of wall time `time` on date d in the zone he is in that day
    tripclock.switches(home)       # [(instant, zone name, why)] every zone change from the config

`home` is the caller's home ZoneInfo (its own env override, e.g. a test zone); None = assistant_profile.tz().

Config (private, 0600, never in public paths): ~/.config/tripclock/trips.json ($TRIPCLOCK_CONFIG):
  {"trips": [{"name": "...", "tz": "Area/Zone", "from": "YYYY-MM-DD", "to": "YYYY-MM-DD",
              "start": ISO, "end": ISO,                         # optional: explicit switch instants
              "legs": [{"no", "from", "to", "dep", "arr", "arr_est"}]}]}   # `tripclock sync` keeps this
A trip's window: "from" 00:00 (home) to the day after "to" 00:00 (trip zone). Inside it, the zone at t is:
  1. "start"/"end" set: the trip zone from start to end, home outside.
  2. flight legs (the config's snapshot plus flight-watch's live flights departing inside the window; live wins,
     and flight-watch's estimated/actual landing time `arr_est` replaces the planned arrival): the zone of the
     airport where he last landed; before the first landing (and while in the air) the zone he took off from.
     An airport with no known zone: the trip zone when the arrival offset matches it, else Etc/GMT-N.
  3. neither: the trip zone for the whole window.
Outside every window: home. A missing or broken config, or any error: home (never raises).
Env: TRIPCLOCK_CONFIG, TRIPCLOCK_FLIGHTS (flight-watch's state dir, default ~/.local/state/flights),
TRIPCLOCK_OFF=1 (always home; tests of other scripts).
"""
import datetime as dt
import json
import os
import sys
import time
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.realpath(__file__))
UTC = dt.timezone.utc


def config_path():
    return os.environ.get('TRIPCLOCK_CONFIG') or os.path.expanduser('~/.config/tripclock/trips.json')


def flights_dir():
    return os.environ.get('TRIPCLOCK_FLIGHTS') or os.path.expanduser('~/.local/state/flights')


def home_zone(home=None):
    if home is not None:
        return home
    try:
        sys.path.insert(0, HERE)
        import assistant_profile
        return assistant_profile.tz()
    except Exception:
        return ZoneInfo('UTC')
    finally:
        if sys.path and sys.path[0] == HERE:
            sys.path.pop(0)


def instant(t=None):
    """None (now), epoch seconds or an aware datetime -> aware UTC datetime. A naive datetime is taken as UTC."""
    if t is None:
        return dt.datetime.now(UTC)
    if isinstance(t, (int, float)):
        return dt.datetime.fromtimestamp(float(t), UTC)
    return (t.replace(tzinfo=UTC) if t.tzinfo is None else t).astimezone(UTC)


def _iso(s):
    try:
        d = dt.datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    return d.astimezone(UTC) if d.tzinfo else None


def _load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def load_config():
    """The trips config as a dict ({} when missing); raises ValueError when present but unreadable or malformed."""
    p = config_path()
    if not os.path.exists(p):
        return {}
    d = _load(p)
    if not isinstance(d, dict) or not isinstance(d.get('trips', []), list):
        raise ValueError(f'{p}: not a {{"trips": [...]}} object')
    for i, tr in enumerate(d.get('trips', [])):
        if not isinstance(tr, dict):
            raise ValueError(f'trip {i}: not an object')
        try:
            ZoneInfo(str(tr.get('tz')))
            dt.date.fromisoformat(str(tr.get('from')))
            dt.date.fromisoformat(str(tr.get('to')))
        except Exception:
            raise ValueError(f'trip {i}: needs a valid "tz", "from" and "to"')
    return d


def _airport_zone(code):
    try:
        sys.path.insert(0, HERE)
        import flights
        name = flights.tz_name(code)
        return ZoneInfo(name) if name else None
    except Exception:
        return None
    finally:
        if sys.path and sys.path[0] == HERE:
            sys.path.pop(0)


def live_legs():
    """flight-watch's flights ({no, from, to, dep, arr, arr_est}); [] when none or unreadable."""
    d = _load(os.path.join(flights_dir(), 'flights.json'))
    st = _load(os.path.join(flights_dir(), 'state.json')) or {}
    st = st.get('flights', {}) if isinstance(st, dict) else {}
    out = []
    for f in (d or {}).get('flights', []) if isinstance(d, dict) else []:
        if not isinstance(f, dict) or not _iso(f.get('dep')) or not _iso(f.get('arr')):
            continue
        leg = {k: f.get(k) for k in ('no', 'from', 'to', 'dep', 'arr')}
        live = (st.get(f.get('id')) or {}).get('live') if isinstance(st.get(f.get('id')), dict) else None
        if isinstance(live, dict) and _iso(live.get('arr_est')):
            leg['arr_est'] = live['arr_est']
        out.append(leg)
    return out


def window(tr):
    """A trip's window (start, end) as UTC instants: "from" 00:00 home .. the day after "to" 00:00 trip zone."""
    home, tz = tr['_home'], ZoneInfo(tr['tz'])
    a = dt.datetime.combine(dt.date.fromisoformat(tr['from']), dt.time(0), tzinfo=home)
    b = dt.datetime.combine(dt.date.fromisoformat(tr['to']) + dt.timedelta(days=1), dt.time(0), tzinfo=tz)
    return a.astimezone(UTC), b.astimezone(UTC)


def legs_of(tr, live=None):
    """The trip's flight legs: the config snapshot, overlaid by live flights departing inside the window."""
    a, b = window(tr)

    def key(g):
        d = _iso(g.get('dep'))
        return (str(g.get('no') or '').replace(' ', '').upper(), d.isoformat() if d else '')
    merged = {}
    for g in tr.get('legs') or []:
        if isinstance(g, dict) and _iso(g.get('dep')) and _iso(g.get('arr')):
            merged[key(g)] = dict(g)
    for g in (live_legs() if live is None else live):
        d = _iso(g.get('dep'))
        if d and a - dt.timedelta(days=1) <= d < b + dt.timedelta(days=1):
            merged[key(g)] = dict(g)
    return sorted(merged.values(), key=lambda g: _iso(g['dep']))


def landed_at(g):
    return _iso(g.get('arr_est')) or _iso(g['arr'])


def leg_zone(g, tr):
    """The zone of the airport a leg lands at."""
    z = _airport_zone(g.get('to'))
    if z:
        return z
    arr = dt.datetime.fromisoformat(str(g['arr']))
    off = arr.utcoffset()
    tz = ZoneInfo(tr['tz'])
    if off == arr.astimezone(tz).utcoffset():
        return tz
    h = int(off.total_seconds() // 3600)
    if off.total_seconds() % 3600 == 0 and -12 <= h <= 14:
        return ZoneInfo('Etc/GMT%+d' % -h if h else 'Etc/UTC')
    return tz


def _trips(home):
    try:
        cfg = load_config()
    except ValueError:
        return []
    out = []
    for tr in cfg.get('trips', []):
        tr = dict(tr)
        tr['_home'] = home
        out.append(tr)
    return out


def _zone_in(tr, t):
    """(zone, why) for instant t inside trip tr's window."""
    tz = ZoneInfo(tr['tz'])
    s, e = _iso(tr.get('start')), _iso(tr.get('end'))
    if s or e:
        inside = (not s or s <= t) and (not e or t < e)
        return (tz, 'trip start/end') if inside else (tr['_home'], 'outside trip start/end')
    legs = legs_of(tr)
    if legs:
        last = None
        for g in legs:
            if landed_at(g) <= t:
                last = g
        if last is None:
            return tr['_home'], 'before the first landing'
        z = leg_zone(last, tr)
        if z.key == tr['_home'].key:
            return tr['_home'], f"landed at {last.get('to')} ({last.get('no')})"
        return z, f"landed at {last.get('to')} ({last.get('no')})"
    return tz, 'trip dates'


def zone_why(t=None, home=None):
    """(ZoneInfo, why, trip name or None) at instant t. Never raises."""
    home = home_zone(home)
    if os.environ.get('TRIPCLOCK_OFF') == '1':
        return home, 'tripclock off', None
    try:
        t = instant(t)
        for tr in _trips(home):
            a, b = window(tr)
            if a <= t < b:
                z, why = _zone_in(tr, t)
                return z, why, tr.get('name')
    except Exception:
        pass
    return home, 'home', None


def zone(t=None, home=None):
    return zone_why(t, home)[0]


def zone_name(t=None, home=None):
    return zone(t, home).key


def local(t=None, home=None):
    t = instant(t)
    return t.astimezone(zone(t, home))


def away(t=None, home=None):
    return zone(t, home).key != home_zone(home).key


def in_quiet(t=None, start=0, end=8, home=None):
    """t inside his quiet hours [start, end) (whole hours on his wall clock; start > end wraps midnight)."""
    h = local(t, home).hour
    return start <= h < end if start <= end else (h >= start or h < end)


def quiet_end(t=None, end=8, home=None, minute=0):
    """The next end of his quiet hours (end:minute on his wall clock) strictly after t, aware in his zone."""
    lt = local(t, home)
    z = lt.tzinfo
    cand = dt.datetime.combine(lt.date(), dt.time(end, minute), tzinfo=z)
    if cand <= lt:
        cand = dt.datetime.combine(lt.date() + dt.timedelta(days=1), dt.time(end, minute), tzinfo=z)
    z2 = zone(cand, home)                         # a switch overnight: end of quiet hours in the new zone
    if z2.key != z.key:
        cand = dt.datetime.combine(cand.date(), dt.time(end, minute), tzinfo=z2)
    return cand


def wall(d, tm, home=None):
    """The instant (aware, in his zone) of wall time `tm` on date `d` in the zone he is in at that moment."""
    z = zone(dt.datetime.combine(d, dt.time(12), tzinfo=home_zone(home)), home)
    cand = dt.datetime.combine(d, tm, tzinfo=z)
    z2 = zone(cand, home)
    return dt.datetime.combine(d, tm, tzinfo=z2) if z2.key != z.key else cand


def switches(home=None):
    """Every zone change implied by the config: [(instant UTC, zone name, why)], in time order."""
    home = home_zone(home)
    out = []
    for tr in _trips(home):
        try:
            a, b = window(tr)
            pts = {a, b}
            for k in ('start', 'end'):
                if _iso(tr.get(k)):
                    pts.add(_iso(tr[k]))
            if not (_iso(tr.get('start')) or _iso(tr.get('end'))):
                pts.update(landed_at(g) for g in legs_of(tr))
            prev = None
            for p in sorted(x for x in pts if a <= x <= b):
                z, why, _ = zone_why(p, home)
                before = zone(p - dt.timedelta(seconds=1), home)
                if z.key != before.key and (prev is None or prev != (p, z.key)):
                    out.append((p, z.key, why))
                    prev = (p, z.key)
        except Exception:
            continue
    return sorted(out)
