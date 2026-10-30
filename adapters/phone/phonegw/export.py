"""`GET /internal/export?since=MS` (internal API, bearer token): the read-only slice of the gateway's database that the
brain's readers use. The brain keeps a
mirror of it (hubkit.phonemirror, refreshed by `hub phone inbox`), so usage.py and board-done run their
SQL against the same table and column names.

Allow-listed: columns per table, state keys per device, and inside each state value only the fields those readers
use. Never exported: coordinates, stops, chat or card text, push args and nonces, device names and certificates, the
app's other state. Rows: since MS (default 8 days back, at most 14) for events (tile taps only, body = the tile name),
push_log, cards, chat (direction only) and inbound; every active or revoked device; the allow-listed state rows plus
one `_seen` row per device (value null, at = its newest state row of any key: the readers' live/stale test).
"""
import json

from .util import now_s

SCHEMA = 1
DAYS_DEFAULT, DAYS_MAX = 8, 14
ROWS_MAX = 5000
GW = "_gw"
SEEN = "_seen"
TEXT_MAX = 40

# device state key -> the value's allowed fields (a dict value) or its kind (str / list / bool flags)
DEVICE_KEYS = {
    "place": ("zone", "band", "town"),          # coarse names only: no stop, no fix age, no coordinates
    "activity": ("kind",),
    "battery": ("band", "level", "charging"),
    "screen": ("on",),
    "net": ("type", "vpn", "roaming"),
    "car": ("projection", "bt"),
    "alarm": ("next",),
    "flag.drive": ("active",),
    "dnd": "str",
    "bt": "kinds",                              # connected device names -> "buds" / "other" only
}
GW_KEYS = {
    "app_latest": ("name", "code"),
    "widget:home": "widget",
}
WIDGET_HOME = ("lights_on", "tv", "away", "vacuum")
BOOL_FIELDS = {("car", "bt"), ("car", "projection"), ("battery", "charging"), ("screen", "on"), ("net", "vpn"),
               ("net", "roaming"), ("flag.drive", "active")}
BUDS = ("bud", "head", "ear")

DEVICE_COLS = ("id", "status", "last_seen", "app_code")
TABLES = {  # table -> (columns, the time column, its unit factor against seconds)
    "push_log": (("id", "at", "level", "tag", "delivered_ms", "acked_ms", "verb", "action_at"), "at", 1000),
    "cards": (("id", "kind", "created", "answered"), "created", 1),
    "chat": (("id", "at", "dir"), "at", 1),
    "inbound": (("id", "t", "at"), "at", 1),
}


def _scalar(v):
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        return v[:TEXT_MAX]
    return None


def _fields(key, v, allowed):
    if not isinstance(v, dict):
        return None
    out = {}
    for f in allowed:
        if f in v:
            out[f] = bool(v[f]) if (key, f) in BOOL_FIELDS else _scalar(v[f])
    return out


def clean_value(key, v, spec):
    """One state value cut to its allow-list; None = drop the row."""
    if spec == "str":
        return v[:TEXT_MAX] if isinstance(v, str) else None
    if spec == "kinds":
        if not isinstance(v, list):
            return None
        return ["buds" if any(b in str(x).lower() for b in BUDS) else "other" for x in v[:10]]
    if spec == "widget":
        if not isinstance(v, dict):
            return None
        data = v.get("data") if isinstance(v.get("data"), dict) else {}
        return {"at": _scalar(v.get("at")), "data": {f: _scalar(data[f]) for f in WIDGET_HOME if f in data}}
    return _fields(key, v, spec)


def _since(raw, now):
    lo, default = now - DAYS_MAX * 86400, now - DAYS_DEFAULT * 86400
    try:
        s = int(raw) // 1000
    except (TypeError, ValueError):
        return default
    return min(max(s, lo), now)


def _tile_name(body):
    try:
        b = json.loads(body)
    except (TypeError, ValueError):
        return None
    n = b.get("name") if isinstance(b, dict) else None
    return n[:TEXT_MAX] if isinstance(n, str) else None


def export(db, since_ms=None, now=None):
    """-> the export dict (rows as dicts, the gateway's own units)."""
    now = now_s() if now is None else now
    since = _since(since_ms, now)
    out = {"ok": True, "schema": SCHEMA, "at": now, "since": since * 1000}
    out["devices"] = [{c: r[c] for c in DEVICE_COLS} for r in db.all("SELECT * FROM devices")]
    state, seen = [], {}
    for r in db.all("SELECT device_id, k, v, at FROM state"):
        dev, k = r["device_id"], r["k"]
        if dev != GW and isinstance(r["at"], (int, float)):
            seen[dev] = max(seen.get(dev, 0), r["at"])
        spec = GW_KEYS.get(k) if dev == GW else DEVICE_KEYS.get(k)
        if spec is None:
            continue
        try:
            v = clean_value(k, json.loads(r["v"]), spec)
        except (TypeError, ValueError):
            v = None
        if v is not None:
            state.append({"device_id": dev, "k": k, "v": json.dumps(v, ensure_ascii=False), "at": r["at"]})
    # the phone's liveness: the newest of ALL its state rows (usage's phone live/stale), with no value of its own
    state += [{"device_id": dev, "k": SEEN, "v": "null", "at": at} for dev, at in sorted(seen.items())]
    out["state"] = state
    ev = []
    for r in db.all("SELECT id, device_id, at, body FROM events WHERE kind='tile' AND received>=? ORDER BY received DESC "
                    "LIMIT ?", since, ROWS_MAX):
        name = _tile_name(r["body"])
        if name is not None:
            ev.append({"id": r["id"], "device_id": r["device_id"], "kind": "tile", "at": r["at"],
                       "body": json.dumps({"name": name}, ensure_ascii=False)})
    out["events"] = ev
    for t, (cols, tcol, unit) in TABLES.items():
        rows = db.all("SELECT %s FROM %s WHERE %s>=? ORDER BY %s DESC LIMIT ?" % (", ".join(cols), t, tcol, tcol),
                      since * unit, ROWS_MAX)
        out[t] = [{c: (_scalar(r[c]) if c in ("tag", "level", "verb", "kind", "t", "dir") else r[c]) for c in cols}
                  for r in rows]
    return out
