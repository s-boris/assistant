"""Widget, fence and routine producers: read my tools' local state and print one JSON body. The gateway runs them as a
subprocess (`python -m phonegw.producers KIND`, timeout) so a broken tool never hurts the server.

  board     tg-status's board (read_todo/split_due + its state.json): working/idle, agents, due/next/progress/waiting;
            + `sleep` = `health board` (his morning sleep line) while config health is on; a "next" item from
            `parcels board` while a parcel waits at the station
  leg       the next 🚆/🚌 leg (train-watch list, else calcache) + its live status (train-watch status, 6 s budget)
  trip      the next or current trip (trip-conditions trips.json): phase, days left, next leg
  fences    geofences: home + office (profile-get), gym/station/airport (~/.config/phonegw places), tonight's venue
            (calcache + night-plan venues, geocoded via transport.opendata.ch, else timetable.search.ch; cached 0600)
  leaving   the Leaving card: next leg, battery, alarm, leave reminders, due items (gateway /internal/now passed in)
  depart    the first departure before the next 10:00 (train-watch legs + calcache travel events): leave-by, label
  geo       the offline place pack for the phone (/v1/geo/ch): {"towns":[[name,lat,lon]...],"stops":[...]}, stops
            near home/office/venues/legs/config extras from transport.opendata.ch (else timetable.search.ch);
            towns = their stops' centroid

Personal values come only from profile-get and ~/.config at run time; nothing personal lives in this file.
Test hooks (env): PRODUCERS_NOW (epoch), TG_STATUS_BIN, TRAIN_WATCH_BIN, CALCACHE_BIN, PROFILE_GET_BIN, TRIPS_JSON,
NIGHT_VENUES, REMIND_DB, HEALTH_BIN, GEO_FAKE (JSON {query: [lat, lon]}; "off" = no geocoding), GEO_CACHE, PHONEGW_CONFIG."""
import datetime as dt
import importlib.machinery
import importlib.util
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

from . import privacy

HOME = os.environ.get("HOME") or os.path.expanduser("~")
BIN = os.path.join(HOME, "bin")
TIME_HEAD = re.compile(r"^((?:\d{2}\.\d{2}\s+)?\d{1,2}:\d{2}|\d{2}\.\d{2})\s+(.*)$")


def now():
    return int(os.environ.get("PRODUCERS_NOW") or time.time())


def cfg():
    from . import config as C
    return C.load(C.Paths())


def tz():
    try:
        return ZoneInfo(cfg().get("tz") or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def tool(env, name):
    return os.environ.get(env) or os.path.join(BIN, name)


def run_json(argv, timeout):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        if p.returncode != 0:
            return None
        return json.loads(p.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def read_json(path, default=None):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def epoch(s, zone):
    """Local ISO ('2026-10-22T23:37[:00][+02:00]' or '2026-10-22 23:37') -> epoch s, or None."""
    if not s:
        return None
    try:
        t = dt.datetime.fromisoformat(str(s).strip().replace(" ", "T").replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=zone)
    return int(t.timestamp())


def clip(s, n):
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


# ---------------------------------------------------------------------------------------------------- board
def load_tg_status():
    path = tool("TG_STATUS_BIN", "tg-status")
    loader = importlib.machinery.SourceFileLoader("tg_status_for_phonegw", path)
    spec = importlib.util.spec_from_loader("tg_status_for_phonegw", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def when_at(when, t):
    """Board time text (HH:MM today, DD.MM HH:MM, DD.MM; container = home wall time) -> (epoch, all_day) or
    (None, False). The phone formats the epoch in its own zone, so trips abroad show the right local time."""
    m = re.match(r"^(?:(\d{2})\.(\d{2})\s+)?(\d{1,2}):(\d{2})$|^(\d{2})\.(\d{2})$", (when or "").strip())
    if not m:
        return None, False
    lt = time.localtime(t)
    day, mon = (m.group(1), m.group(2)) if m.group(1) else (m.group(5), m.group(6))
    hh, mm = (int(m.group(3)), int(m.group(4))) if m.group(3) else (0, 0)
    d, mo = (int(day), int(mon)) if day else (lt.tm_mday, lt.tm_mon)
    best = None
    for y in (lt.tm_year - 1, lt.tm_year, lt.tm_year + 1):        # the year that puts it nearest to now
        try:
            e = int(time.mktime((y, mo, d, hh, mm, 0, 0, 0, -1)))
        except (OverflowError, ValueError):
            continue
        if time.localtime(e)[:3] != (y, mo, d):
            continue                                                 # 31.02 and friends
        if best is None or abs(e - t) < abs(best - t):
            best = e
    return best, bool(best is not None and not m.group(3))


def board():
    ts = load_tg_status()
    t = now()
    todo = ts.read_todo()
    due, nxt = ts.split_due(todo.get("due") or [], t)
    try:
        st = ts.load()
    except Exception:
        st = {}
    agents = [a for a in (st.get("agents") or {}).values() if isinstance(a, dict)]
    items = []

    def add(sec, text):
        m = TIME_HEAD.match(text)
        when, title = (m.group(1), m.group(2)) if m else (None, text)
        it = {"sec": sec, "title": clip(title, 80), "when": when}
        at, all_day = when_at(when, t)
        if at is not None:
            it["at"] = at
            if all_day:
                it["all_day"] = True
        items.append(it)

    for x in due[:4]:
        add("due", x)
    pl = parcels_line()
    if pl:
        items.append({"sec": "next", "title": pl, "when": None})
    for x in nxt[:2]:
        add("next", x)
    for title, _, _ in (todo.get("in progress") or [])[:3]:
        items.append({"sec": "progress", "title": clip(title, 80), "when": None})
    for title, _, _ in (todo.get("waiting on you") or [])[:3]:
        items.append({"sec": "waiting", "title": clip(title, 80), "when": None})
    mode = st.get("mode")
    out = {"state": mode if mode in ("working", "idle") else "unknown", "agents": len(agents), "due": len(due),
           "next": len(nxt), "progress": len(todo.get("in progress") or []),
           "waiting": len(todo.get("waiting on you") or []), "items": items[:12], "updated": t}
    sl = sleep_line()
    if sl:
        out["sleep"] = sl
    return out


def parcels_line():
    """A Board line while a parcel waits at the station (`parcels board --json`, cache only), else None."""
    got = run_json([tool("PARCELS_BIN", "parcels"), "board", "--json"], 10)
    t = got.get("title") if isinstance(got, dict) else None
    return clip(str(t), 80) if t else None


def sleep_line():
    """board.sleep: his morning sleep line (`health board`: he is up, the sleep-in guard is not holding, before early
    afternoon), only while the gateway's health is on. App only (C1): never the Telegram board."""
    try:
        on = bool(cfg().get("health"))
    except Exception:
        on = False
    if not on:
        return None
    got = run_json([tool("HEALTH_BIN", "health"), "board"], 20)
    if not isinstance(got, dict) or not isinstance(got.get("line"), str) or not got["line"]:
        return None
    sl = {"line": clip(got["line"], 80)}
    if re.match(r"^\d{4}-\d{2}-\d{2}$", str(got.get("night") or "")):
        sl["night"] = got["night"]
    if got.get("tone") in ("good", "ok", "low"):
        sl["tone"] = got["tone"]
    return sl


# ---------------------------------------------------------------------------------------------------- leg
def next_leg(zone, t, live_timeout=6):
    """-> leg dict (internal, with epoch times) or None."""
    legs = run_json([tool("TRAIN_WATCH_BIN", "train-watch"), "list", "--json"], 15) or []
    cands = []
    for l in legs if isinstance(legs, list) else []:
        dep = epoch(l.get("dep"), zone)
        if dep and dep >= t - 300:
            cands.append((dep, l))
    src = "train-watch"
    if not cands:
        src = "calcache"
        for e in run_json([tool("CALCACHE_BIN", "calcache"), "show", "--days", "2", "--json"], 15) or []:
            tr = e.get("train") if isinstance(e, dict) else None
            dep = epoch(e.get("start"), zone) if tr and not e.get("all_day") else None
            if dep and dep >= t - 300:
                cands.append((dep, {"id": None, "from": tr.get("from"), "to": tr.get("to"), "dep": e.get("start"),
                                    "arr": e.get("end"), "lines": tr.get("lines") or [], "title": e.get("title")}))
    if not cands:
        return None
    dep, l = sorted(cands, key=lambda x: x[0])[0]
    leg = {"icon": "🚆", "title": clip(re.sub(r"^\W+\s*", "", l.get("title") or ""), 64) or None,
           "line": clip((l.get("lines") or [None])[0], 16) if l.get("lines") else None, "dep": dep,
           "from": clip(l.get("from"), 64) or None, "to": clip(l.get("to"), 64) or None, "platform": None,
           "status": "unknown", "delay_min": 0, "arrive": epoch(l.get("arr"), zone), "source": src}
    if (l.get("title") or "").startswith("🚌"):
        leg["icon"] = "🚌"
    if l.get("id") and live_timeout:
        s = run_json([tool("TRAIN_WATCH_BIN", "train-watch"), "status", l["id"], "--json"], live_timeout)
        live = (s or {}).get("live") or {}
        if live:
            leg["platform"] = clip(live.get("platform"), 8) or None
            leg["delay_min"] = max(0, int(live.get("delay") or 0))
            leg["status"] = "cancelled" if live.get("cancelled") else "delayed" if leg["delay_min"] >= 3 else "on_time"
            if live.get("lines"):
                leg["line"] = clip(live["lines"][0], 16)
    return leg


def leg():
    t, zone = now(), tz()
    lg = next_leg(zone, t)
    if not lg:
        return {"empty": True, "updated": t}
    walk = int((cfg().get("places") or {}).get("walk_to_stop_min", 0) or 0)
    town = (profile("home.town") or "").strip().lower() if walk else ""
    frm = (lg.get("from") or "").lower()
    from_home = frm in ("home", "") or bool(town and town.split()[0] in frm)
    out = {k: lg[k] for k in ("icon", "title", "line", "dep", "from", "to", "platform", "status", "delay_min", "arrive")}
    out["leave_by"] = lg["dep"] - walk * 60 if walk and from_home else None
    out["home_eta"] = lg["arrive"] if (lg.get("to") or "").lower() == "home" else None
    out["updated"] = t
    if out["arrive"] is None:
        out.pop("arrive")
    return out


# ---------------------------------------------------------------------------------------------------- trip
def trip():
    t, zone = now(), tz()
    trips = (read_json(os.environ.get("TRIPS_JSON") or os.path.join(HOME, ".config/trip-conditions/trips.json"), {})
             or {}).get("trips") or {}
    best = None
    for key, tr in trips.items():
        start = epoch(tr.get("from"), zone)
        end = epoch(tr.get("to"), zone)
        if not start:
            continue
        end_day = (end or start) + 86400
        if end_day < t:
            continue
        if best is None or start < best[0]:
            best = (start, end, key, tr)
    if not best:
        return {"empty": True, "updated": t}
    start, end, key, tr = best
    phase = "before" if t < start else "during"
    today = dt.datetime.fromtimestamp(t, zone).date()
    days_left = (dt.datetime.fromtimestamp(start, zone).date() - today).days if phase == "before" else \
        ((dt.datetime.fromtimestamp(end, zone).date() - today).days if end else 0)
    nxt = None
    for lg in tr.get("legs") or []:
        lf = epoch(lg.get("from"), zone)
        if lf and lf + 86400 > t and (phase == "during" or lf >= start):
            nxt = lg.get("name")
            if phase == "before" or lf > t:
                break
    name = clip("%s %s" % (tr.get("emoji") or "", tr.get("name") or key), 40)
    out = {"name": name, "phase": phase, "starts": start, "ends": end, "days_left": int(days_left),
           "next": clip(nxt, 80) if nxt else None, "updated": t}
    stops = trip_stops(tr)
    if stops:
        out["stops"] = stops
    return out


DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def trip_stops(tr):
    """The planned route from trips.json legs ({name, from, to}): one stop per leg with its nights, plus the flight
    home from the first stop when the route ends elsewhere (island hops: fly in and out of the same airport).
    Legs without a name or a from day are skipped; nothing here is a booking."""
    out = []
    for lg in tr.get("legs") or []:
        if not isinstance(lg, dict) or not lg.get("name") or not DAY_RE.match(str(lg.get("from") or "")):
            continue
        to = str(lg.get("to") or "")
        to = to if DAY_RE.match(to) else None
        nights = None
        if to:
            try:
                nights = (dt.date.fromisoformat(to) - dt.date.fromisoformat(lg["from"])).days
            except ValueError:
                to = None
        out.append({"name": clip(lg["name"], 24), "from": lg["from"], "to": to,
                    "nights": nights if nights is not None and nights >= 0 else None})
        if len(out) == 7:
            break
    end = str(tr.get("to") or "")
    if len(out) >= 2 and out[-1]["to"] and out[0]["name"] != out[-1]["name"] and DAY_RE.match(end) and end >= out[-1]["to"]:
        out.append({"name": out[0]["name"], "from": out[-1]["to"], "fly": True})
    return out


def trip_sites():
    """GET /v1/trip/sites: divelog's site names + areas (never coordinates), its planned legs by area, the dive
    count, and the last dive day = one day before the flight home (DAN 24 h) when the trip route has one."""
    sites = run_json([tool("DIVELOG_BIN", "divelog"), "sites", "--json"], 20) or []
    stats = run_json([tool("DIVELOG_BIN", "divelog"), "stats", "--json"], 20) or {}
    cfg = read_json(os.environ.get("DIVELOG_CONFIG") or os.path.join(HOME, ".config/divelog/config.json"), {}) or {}
    out = {"sites": [{"id": clip(s.get("id"), 64), "name": clip(s.get("name"), 60), "area": clip(s.get("area"), 60) or None}
                     for s in sites if isinstance(s, dict) and s.get("id") and s.get("name")][:200],
           "legs": [], "dives": int(stats.get("dives") or 0) if isinstance(stats, dict) else 0,
           "last": clip(stats.get("last"), 32) or None if isinstance(stats, dict) else None, "last_dive": None, "fly": None}
    for lg in (cfg.get("legs") or [])[:12]:
        if isinstance(lg, dict) and lg.get("area") and DAY_RE.match(str(lg.get("from") or "")):
            to = str(lg.get("to") or "")
            out["legs"].append({"area": clip(lg["area"], 40), "from": lg["from"], "to": to if DAY_RE.match(to) else None,
                                "sites": [clip(x, 64) for x in (lg.get("sites") or []) if isinstance(x, str)][:40]})
    t = trip()
    fly = next((s["from"] for s in t.get("stops") or [] if s.get("fly")), None) if not t.get("empty") else None
    if fly:
        out["fly"] = fly
        out["last_dive"] = (dt.date.fromisoformat(fly) - dt.timedelta(days=1)).isoformat()
    return out


# ---------------------------------------------------------------------------------------------------- fences
def profile(key):
    try:
        p = subprocess.run([tool("PROFILE_GET_BIN", "profile-get"), key], capture_output=True, text=True, timeout=10,
                           stdin=subprocess.DEVNULL)
        return p.stdout.strip() if p.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def geo_cache_path():
    from . import config as C
    return os.environ.get("GEO_CACHE") or os.path.join(C.Paths().state, "geo-cache.json")


OPENDATA_LOCATIONS = "https://transport.opendata.ch/v1/locations"
SEARCHCH_COMPLETION = "https://timetable.search.ch/api/completion.json"


def _get_json(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "phonegw"}), timeout=8) as r:
        return json.load(r)


def stations(qs):
    """opendata /locations stations for qs ({type, query} or {type, x, y}: x = lat, y = lon). When opendata fails (an
    {"errors": ...} payload, a non-2xx status, a timeout, no JSON) the same lookup goes to timetable.search.ch
    completion.json (keyless; term or latlon), mapped to opendata's shape: core/transport's mapping, kept here as the
    gateway runs from a hub release without the core library. Raises when both fail."""
    try:
        data = _get_json(OPENDATA_LOCATIONS + "?" + urllib.parse.urlencode(qs))
        if isinstance(data, dict) and not data.get("errors") and isinstance(data.get("stations") or [], list):
            return data.get("stations") or []
    except Exception:
        pass
    sq = {"show_ids": 1, "show_coordinates": 1}
    if qs.get("query"):
        sq["term"] = qs["query"]
    else:
        sq["latlon"] = "%s,%s" % (qs["x"], qs["y"])
    data = _get_json(SEARCHCH_COMPLETION + "?" + urllib.parse.urlencode(sq))
    if not isinstance(data, list):
        raise ValueError("search.ch: not a list")
    out = []
    for e in data:
        if not isinstance(e, dict) or not e.get("label") or (qs.get("type") == "station" and not e.get("id")):
            continue
        lat, lon = e.get("lat"), e.get("lon")
        ok = isinstance(lat, (int, float)) and isinstance(lon, (int, float))
        out.append({"id": str(e["id"]) if e.get("id") else None, "name": e["label"], "distance": e.get("dist"),
                    "coordinate": {"type": "WGS84", "x": float(lat) if ok else None, "y": float(lon) if ok else None}})
    return out


def geocode(query, near=None):
    """Stop / station name (or near=(lat, lon): the nearest station) -> (lat, lon) or None. Cached 0600, 60 d."""
    fake = os.environ.get("GEO_FAKE")
    key = query or "near:%.3f,%.3f" % near
    if fake:
        if fake == "off":
            return None
        v = (read_json(fake, {}) or {}).get(key)
        return tuple(v) if v else None
    path = geo_cache_path()
    cache = read_json(path, {}) or {}
    hit = cache.get(key)
    if hit and hit.get("at", 0) > time.time() - 60 * 86400:
        return tuple(hit["ll"]) if hit.get("ll") else None
    qs = {"type": "station"}
    if query:
        qs["query"] = query
    else:
        qs["x"], qs["y"] = "%.5f" % near[0], "%.5f" % near[1]
    ll = None
    try:
        for s in stations(qs):
            c = s.get("coordinate") or {}
            if isinstance(c.get("x"), (int, float)) and isinstance(c.get("y"), (int, float)):
                ll = (float(c["x"]), float(c["y"]))
                break
    except Exception:
        return None
    cache[key] = {"ll": list(ll) if ll else None, "at": int(time.time())}
    from .util import write_private
    write_private(path, json.dumps(cache))
    return ll


def stations_near(lat, lon, limit=10):
    """Up to `limit` stations around a point -> [(name, lat, lon)] (opendata, cached like geocode)."""
    fake = os.environ.get("GEO_FAKE")
    key = "nearlist:%.3f,%.3f" % (lat, lon)
    if fake:
        return [tuple(x) for x in ((read_json(fake, {}) or {}).get(key) or [])] if fake != "off" else []
    path = geo_cache_path()
    cache = read_json(path, {}) or {}
    hit = cache.get(key)
    if hit and hit.get("at", 0) > time.time() - 60 * 86400:
        return [tuple(x) for x in hit.get("list") or []]
    out = []
    try:
        for st in stations({"x": "%.5f" % lat, "y": "%.5f" % lon, "type": "station"})[:limit + 1]:
            c = st.get("coordinate") or {}
            # Without an id it's the street address at the query point (opendata adds one even for type=station).
            if not st.get("id") or privacy.address_like(st.get("name") or ""):
                continue
            if st.get("name") and isinstance(c.get("x"), (int, float)) and isinstance(c.get("y"), (int, float)):
                out.append((st["name"], float(c["x"]), float(c["y"])))
    except Exception:
        return []
    cache[key] = {"list": [list(x) for x in out], "at": int(time.time())}
    from .util import write_private
    write_private(path, json.dumps(cache))
    return out


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")[:32] or "x"


def fences():
    t, zone = now(), tz()
    places = cfg().get("places") or {}
    out, missing = [], []

    def add(name, kind, ll, r_m, until=None, dwell_s=None):
        if not ll:
            missing.append(name)
            return
        f = {"name": name, "kind": kind, "lat": round(float(ll[0]), 5), "lon": round(float(ll[1]), 5),
             "r_m": int(r_m)}
        if until:
            f["until"] = int(until)
        if dwell_s:
            f["dwell_s"] = int(dwell_s)
        out.append(f)

    def ll_of(prefix):
        try:
            return float(profile(prefix + ".lat")), float(profile(prefix + ".lon"))
        except (TypeError, ValueError):
            return None

    home = ll_of("home")
    add("home", "home", home, (places.get("home") or {}).get("r_m", 150))
    add("office", "office", ll_of("work"), (places.get("office") or {}).get("r_m", 250), dwell_s=120)
    for name, kind, r_def in (("gym", "gym", 120), ("station", "station", 250), ("airport", "airport", 1500)):
        p = places.get(name) or {}
        ll = (p["lat"], p["lon"]) if isinstance(p.get("lat"), (int, float)) and isinstance(p.get("lon"), (int, float)) \
            else geocode(p["query"]) if p.get("query") else \
            (geocode(None, near=home) if name == "station" and home and p.get("nearest", True) else None)
        add(name, kind, ll, p.get("r_m", r_def), dwell_s=120 if name == "gym" else None)
    # tonight's venue: a timed event today matching a night-plan venue (title/alias), else today's train leg target
    venues = read_json(os.environ.get("NIGHT_VENUES") or os.path.join(HOME, ".config/night-plan/venues.json"), {}) or {}
    today = dt.datetime.fromtimestamp(t, zone).date()
    events = run_json([tool("CALCACHE_BIN", "calcache"), "show", "--days", "2", "--json"], 15) or []
    venue_done = False
    for e in events:
        if not isinstance(e, dict) or e.get("all_day"):
            continue
        st, en = epoch(e.get("start"), zone), epoch(e.get("end"), zone)
        if not st or not en or en < t or dt.datetime.fromtimestamp(st, zone).date() != today:
            continue
        title = (e.get("title") or "").lower()
        for vkey, v in venues.items():
            names = [vkey] + list(v.get("aliases") or [])
            if any(n and n.lower() in title for n in names) and v.get("stop"):
                add("venue:" + slug(vkey), "venue", geocode(v["stop"]), places.get("venue_r_m", 400),
                    until=en + 3 * 3600, dwell_s=300)
                venue_done = True
                break
        if venue_done:
            break
    if not venue_done:
        for e in events:
            tr = e.get("train") if isinstance(e, dict) else None
            st = epoch(e.get("start"), zone) if tr else None
            if tr and st and st >= t - 3600 and dt.datetime.fromtimestamp(st, zone).date() == today \
                    and (tr.get("to") or "").lower() not in ("", "home"):
                add("venue:" + slug(tr["to"]), "venue", geocode(tr["to"]), places.get("venue_r_m", 400),
                    until=st + 18 * 3600, dwell_s=300)
                break
    return {"fences": out, "missing": missing}


# ---------------------------------------------------------------------------------------------------- leaving
def leave_reminders():
    items = read_json(os.environ.get("REMIND_DB") or os.path.join(HOME, ".local/state/remind/reminders.json"), []) or []
    return sum(1 for r in items if isinstance(r, dict) and r.get("on") == "leave")


def leaving(view):
    """view = /internal/now (or {}). -> card body for /internal/card (info)."""
    t, zone = now(), tz()
    lines = []
    lg = next_leg(zone, t, live_timeout=3)
    if lg and lg["dep"] - t <= 6 * 3600:
        hm = dt.datetime.fromtimestamp(lg["dep"], zone).strftime("%H:%M")
        bits = ["%s %s %s" % (lg["icon"], lg.get("line") or "", hm)]
        if lg.get("platform"):
            bits.append("pl. %s" % lg["platform"])
        if lg.get("delay_min"):
            bits.append("+%d min" % lg["delay_min"])
        if lg.get("status") == "cancelled":
            bits.append("cancelled")
        if lg.get("to"):
            bits.append("→ %s" % lg["to"])
        lines.append(" · ".join(b.replace("  ", " ").strip() for b in bits))
    b = (view or {}).get("battery") or {}
    if b.get("level") is not None and b.get("level") <= 30 and not b.get("charging"):
        lines.append("🔋 %d %% – take the charger" % b["level"])
    n = leave_reminders()
    if n:
        lines.append("📝 %d reminder%s when you leave" % (n, "" if n == 1 else "s"))
    try:
        due = board()
        if due["due"]:
            lines.append("📋 %d due on the board" % due["due"])
    except Exception:
        pass
    if not lines:
        lines.append("Nothing pending. Have a good one.")
    day = dt.datetime.fromtimestamp(t, zone).strftime("%Y%m%d")
    return {"card_id": "c_leaving_" + day, "kind": "info", "title": "🚪 Leaving", "text": clip("\n".join(lines), 400),
            "expires": t + 2 * 3600, "group": "routine", "notify": True}


# ---------------------------------------------------------------------------------------------------- depart
TRAVEL = {"train": "🚆", "bus": "🚌", "drive": "🚗", "flight": "✈️"}


def depart():
    """The first departure from now until the next 10:00 (spec #8: tomorrow's first leg before 10:00)."""
    t, zone = now(), tz()
    local = dt.datetime.fromtimestamp(t, zone)
    cut = local.replace(hour=10, minute=0, second=0, microsecond=0)
    if local >= cut:
        cut += dt.timedelta(days=1)
    cut = int(cut.timestamp())
    walk = int((cfg().get("places") or {}).get("walk_to_stop_min", 0) or 0)
    town = (profile("home.town") or "").strip().lower().split(" ")[0] if walk else ""
    cands = []
    for l in run_json([tool("TRAIN_WATCH_BIN", "train-watch"), "list", "--json"], 15) or []:
        dep = epoch(l.get("dep"), zone) if isinstance(l, dict) else None
        if dep and t < dep <= cut:
            frm = (l.get("from") or "").lower()
            home = frm in ("", "home") or bool(town and town in frm)
            cands.append({"dep": dep, "at": dep - walk * 60 if home else dep, "kind": "train",
                          "what": clip(re.sub(r"^\W+\s*", "", l.get("title") or "") or (l.get("lines") or ["train"])[0], 40)})
    for e in run_json([tool("CALCACHE_BIN", "calcache"), "show", "--days", "2", "--json"], 15) or []:
        if not isinstance(e, dict) or e.get("all_day"):
            continue
        kind = (e.get("flags") or {}).get("travel")
        st = epoch(e.get("start"), zone)
        if kind in TRAVEL and st and t < st <= cut:
            cands.append({"dep": st, "at": st - (walk * 60 if kind in ("train", "bus") else 0), "kind": kind,
                          "what": clip(re.sub(r"^\W+\s*", "", e.get("title") or "") or kind, 40)})
    if not cands:
        return {"empty": True, "cut": cut}
    first = sorted(cands, key=lambda c: c["at"])[0]
    return dict(first, icon=TRAVEL.get(first["kind"], ""), cut=cut)


# ---------------------------------------------------------------------------------------------------- geo pack
TOWN_SPLIT = re.compile(r"\s*,\s*")
TOWN_TAIL = re.compile(r"\s+(?:HB|SBB|Bahnhof|Bf|Hbf|Flughafen|Airport|Station|Gare|Bahnhofplatz)$", re.I)


def town_of(stop):
    name = stop.split(",")[0].strip() if "," in stop else TOWN_TAIL.sub("", stop.strip())
    return name.strip() or None


def geo():
    """Offline pack for the phone's place.town / place.stop (S's GeoPack.parse format). Coarse: stops at 4 decimals
    (public stop positions), towns at 3 (centroid of their stops). Home/office are only used as search seeds."""
    zone = tz()
    conf = cfg()
    gcfg = conf.get("geo") or {}
    seeds, queries = [], list(gcfg.get("extra") or [])
    for prefix in ("home", "work"):
        try:
            seeds.append((float(profile(prefix + ".lat")), float(profile(prefix + ".lon"))))
        except (TypeError, ValueError):
            pass
    venues = read_json(os.environ.get("NIGHT_VENUES") or os.path.join(HOME, ".config/night-plan/venues.json"), {}) or {}
    queries += [v["stop"] for v in venues.values() if isinstance(v, dict) and v.get("stop")]
    for l in run_json([tool("TRAIN_WATCH_BIN", "train-watch"), "list", "--json"], 15) or []:
        if isinstance(l, dict):
            queries += [x for x in (l.get("from"), l.get("to")) if x and x.lower() != "home"]
    for e in run_json([tool("CALCACHE_BIN", "calcache"), "show", "--days", "21", "--json"], 15) or []:
        tr = e.get("train") if isinstance(e, dict) else None
        if tr:
            queries += [x for x in (tr.get("from"), tr.get("to")) if x and x.lower() != "home"]
    for q in dict.fromkeys(queries):            # de-duplicated, order kept
        ll = geocode(q)
        if ll:
            seeds.append(ll)
    stops = {}
    for lat, lon in seeds[: int(gcfg.get("max_seeds", 40))]:
        for name, la, lo in stations_near(lat, lon, int(gcfg.get("per_seed", 10))):
            if not privacy.address_like(name):          # cached lists may still hold street-address entries
                stops.setdefault(name, (round(la, 4), round(lo, 4)))
    towns = {}
    for name, (la, lo) in stops.items():
        tn = town_of(name)
        if tn and not privacy.street_like(tn):
            towns.setdefault(tn, []).append((la, lo))
    return {"pack": "ch", "built": now(), "tz": str(zone),
            "towns": sorted([[n, round(sum(p[0] for p in v) / len(v), 3), round(sum(p[1] for p in v) / len(v), 3)]
                             for n, v in towns.items()]),
            "stops": sorted([[n, la, lo] for n, (la, lo) in stops.items()])}


def tiles():
    """Tile states the app mirrors: sleepin = `sleepin active` (exit 0 yes, 1 no, else unknown)."""
    try:
        p = subprocess.run([tool("SLEEPIN_BIN", "sleepin"), "active"], capture_output=True, timeout=10, stdin=subprocess.DEVNULL)
        v = True if p.returncode == 0 else False if p.returncode == 1 else None
    except (OSError, subprocess.SubprocessError):
        v = None
    return {"sleepin": v, "updated": now()}


# ---------------------------------------------------------------------------------------------------- home
# Home-while-away widget (status only, never controls): `ha` reads (privacy-gated CLI) + cat-corner/cat-supplies.
# Never reads person/device_tracker/zone or location-like entities; temperatures only from indoor areas.
OUTDOOR = re.compile(r"(?i)balcon|balkon|outdoor|outside|aussen|garden|garten|terrace|terrasse|roof|dach")
NOT_HOME = re.compile(r"(?i)^(person|device_tracker|zone)\.|location|geocod|gps|latitude|longitude")
TV_OFF = {"off", "unavailable", "unknown", "standby", "none", ""}
VACUUM = {"docked", "cleaning", "returning", "paused", "idle", "error"}


def ha_list(domain):
    d = run_json([tool("HA_BIN", "ha"), "list", domain, "--json"], 20)
    return [x for x in d if isinstance(x, dict) and not NOT_HOME.search(str(x.get("entity_id", "")))] \
        if isinstance(d, list) else None


def ha_state(eid):
    if NOT_HOME.search(eid):
        return None
    d = run_json([tool("HA_BIN", "ha"), "state", eid, "--json"], 20)
    d = (d[0] if d else None) if isinstance(d, list) else d
    return d if isinstance(d, dict) else None


def lights_on(lights):
    """On lamps, a group counted once (members of any light group are skipped)."""
    members = set()
    for x in lights:
        m = (x.get("attributes") or {}).get("entity_id")
        if isinstance(m, list):
            members.update(m)
    return sum(1 for x in lights if x.get("state") == "on" and x.get("entity_id") not in members)


def tv_state(players, pick):
    tvs = [x for x in players if (x.get("entity_id") in pick if pick else (x.get("attributes") or {}).get("device_class") == "tv")]
    if not tvs:
        return "unknown"
    return "on" if any(str(x.get("state", "")).lower() not in TV_OFF for x in tvs) else "off"


def vacuum_state(vacs, pick):
    vacs = [x for x in vacs if (x.get("entity_id") == pick if pick else
                                not re.search(r"(?i)litter", "%s %s" % (x.get("entity_id"), x.get("name"))))]
    st = str(vacs[0].get("state", "")).lower() if vacs else ""
    return st if st in VACUUM else "unknown"


def temps(sensors, pick):
    vals = []
    for x in sensors:
        a = x.get("attributes") or {}
        if pick:
            if x.get("entity_id") not in pick:
                continue
        elif a.get("device_class") != "temperature" or a.get("unit_of_measurement") != "°C" or not x.get("area") \
                or OUTDOOR.search("%s %s" % (x.get("area"), x.get("entity_id"))):
            continue
        try:
            v = float(x.get("state"))
        except (TypeError, ValueError):
            continue
        if -40 <= v <= 60:
            vals.append(int(math.floor(v + 0.5)))
    return (min(vals), max(vals)) if vals else (None, None)


def cat_corner(zone):
    cc = run_json([tool("CAT_CORNER_BIN", "cat-corner"), "status", "--json"], 30)
    if not isinstance(cc, dict) or not isinstance(cc.get("view"), dict):
        return None
    v = cc["view"]
    fd = (v.get("feeders") or [{}])[0] or {}
    fo = (v.get("fountains") or [{}])[0] or {}
    rb = v.get("robot") or {}
    feeder = "unknown" if not v.get("feeders") else "offline" if fd.get("offline_since") else \
        "missed" if fd.get("missed_meal") else "low" if fd.get("low") else "ok"
    fountain = "unknown" if not v.get("fountains") else "offline" if fo.get("offline_since") else "low" if fo.get("low") else "ok"
    litter = "unknown" if not rb else "fault" if rb.get("bad_since") else "ok"
    pct = lambda x: int(max(0, min(100, round(x)))) if isinstance(x, (int, float)) else None   # noqa: E731
    out = {"feeder": feeder, "fed_at": epoch(fd.get("last_feed"), zone), "fountain": fountain, "filter_pct": None,
           "litter": litter, "drawer_pct": pct(rb.get("drawer")), "battery_low": False, "desiccant_due": False,
           "problems": len(cc.get("problems") or [])}
    cs = run_json([tool("CAT_SUPPLIES_BIN", "cat-supplies"), "--json"], 30)
    r = (cs or {}).get("rates") if isinstance(cs, dict) else None
    if isinstance(r, dict):
        out["filter_pct"] = pct(r.get("filter_pct_now"))
        out["battery_low"] = r.get("feeder_battery") == "low"
        d = r.get("desiccant_days_left")
        out["desiccant_due"] = isinstance(d, (int, float)) and d <= 0
    return out


def home():
    t, zone = now(), tz()
    hc = cfg().get("home") or {}
    lights = ha_list("light")
    ok = lights is not None
    players = ha_list("media_player") if ok else None
    vacs = ha_list("vacuum") if ok else None
    sensors = ha_list("sensor") if ok else None
    away = ha_state(hc.get("away") or "input_boolean.away_mode") if ok else None
    lo, hi = temps(sensors or [], hc.get("temps"))
    state_dir = os.environ.get("PHONEGW_STATE") or os.path.join(HOME, ".local/state/phonegw")
    mark = os.path.join(state_dir, "home-ha.json")
    if ok:
        last = t - t % 3600                     # hourly: a stable flat still reports in once an hour, not every 10 min
        try:
            os.makedirs(state_dir, mode=0o700, exist_ok=True)
            with open(mark + ".tmp", "w") as fh:
                json.dump({"at": t}, fh)
            os.replace(mark + ".tmp", mark)
        except OSError:
            pass
    else:
        at = (read_json(mark, {}) or {}).get("at")
        last = at - at % 3600 if isinstance(at, int) and at > 0 else None
    return {"lights_on": lights_on(lights) if ok else None,
            "tv": tv_state(players or [], hc.get("tv")) if ok else "unknown",
            "vacuum": vacuum_state(vacs or [], hc.get("vacuum")) if ok else "unknown",
            "away": {"on": True, "off": False}.get(str((away or {}).get("state", "")).lower()),
            "temp_min": lo, "temp_max": hi, "cat": cat_corner(zone), "ha": ok, "ha_at": last, "updated": t}


PRODUCERS = {"tiles": tiles, "board": board, "leg": leg, "trip": trip, "fences": fences, "depart": depart, "geo": geo,
             "home": home, "trip_sites": trip_sites}


def main(argv):
    if not argv or argv[0] not in list(PRODUCERS) + ["leaving"]:
        print("usage: producers tiles|board|leg|trip|trip_sites|home|fences|depart|geo|leaving [VIEW_JSON]", file=sys.stderr)
        return 2
    if argv[0] == "leaving":
        view = json.loads(argv[1]) if len(argv) > 1 else {}
        print(json.dumps(leaving(view), ensure_ascii=False))
    else:
        print(json.dumps(PRODUCERS[argv[0]](), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
