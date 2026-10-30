"""core.transport: Swiss public transport lookups, transport.opendata.ch first, timetable.search.ch as the fallback.
Stdlib only (core.http underneath).

  Client(api=None, fallback=None, ua=None, timeout=20, on_source=None)
      api        opendata base (default env TRANSPORT_API, else https://transport.opendata.ch/v1)
      fallback   search.ch base (default env TRANSPORT_FALLBACK_API, else https://timetable.search.ch/api);
                 "off" = no fallback
      on_source  callback(source, why): once per source and client when it answers ("opendata" or "search.ch";
                 why = opendata's failure that sent the lookup to search.ch, else "")
  .connections(frm, to, when, limit=4, arrive=False, keep_before=0)
      -> opendata /connections rows. when = an aware datetime (or naive Swiss wall time), or a date alone (no time
         asked: from now on that day when it is today, else from its start). arrive: arriving by
         `when` (opendata isArrivalTime). From search.ch: route.json with show_delays and show_trackchanges;
         departures with pre=2 and the rows leaving over keep_before minutes before `when` dropped (search.ch
         leaves a slow train out when it would head the list; then `limit` rows from `when`, as opendata gives,
         search.ch may send more); arrivals with pre=limit, num=1 and the rows
         arriving after `when` dropped (the last `limit` kept).
  .locations(query=None, x=None, y=None, type="all")
      -> opendata /locations "stations" ({id, name, coordinate {x: lat, y: lon}, distance, icon}). From
         search.ch: completion.json by term, or by latlon (x, y) with the distance; type "station" keeps the
         stops (entries with an id).
  .get(base, path, params) -> the JSON object (TransportError on a failure, below)
  searchch_connections(data, anchor) / searchch_locations(data, type) / sc_time / sc_delay: the mapping alone

Failure (either source) = an {"errors": ...} payload (also with 200, as opendata answers while its router is
down), search.ch {"messages": [...]} without results, a non-2xx status, a timeout or no JSON object. A failed
opendata endpoint is not asked again by that client (one request per lookup). Both failing raise TransportError
"transport API error: <opendata's reason>; fallback search.ch: <its reason>".

The search.ch mapping (opendata's shape, so callers read both alike): times as ISO with the Swiss offset (the
repeated autumn hour resolved against the asked time), dep_delay/arr_delay -> delay minutes, a delay "X" or a
cancelled flag -> journey/connection "cancelled", a changed track ("13!", search.ch shows only the new one) ->
platform "" and prognosis.platform "13", *G/*L -> category/number (bus and tram types to B/T when *G is not one),
walks -> {"journey": None, "walk": {"duration": seconds}} (zero-length ones dropped), duration "00d01:17:00",
transfers = rides - 1.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import urllib.error
import urllib.parse
from collections.abc import Callable
from typing import Any
from zoneinfo import ZoneInfo

from core import http

__all__ = [
    "OPENDATA",
    "SEARCHCH",
    "Client",
    "TransportError",
    "sc_delay",
    "sc_time",
    "searchch_connections",
    "searchch_locations",
]

OPENDATA = "https://transport.opendata.ch/v1"
SEARCHCH = "https://timetable.search.ch/api"
SWISS = ZoneInfo("Europe/Zurich")  # the timetable's zone (search.ch gives wall times without an offset)
MIN = dt.timedelta(minutes=1)
BUS_CATS = {"B", "BN", "BUS", "NFB", "KB", "EXB", "BP", "EV"}
TRAM_CATS = {"T", "TN", "TRAM"}
ICONS = {
    "train": "train",
    "tram": "tram",
    "bus": "bus",
    "boat": "ship",
    "cablecar": "cableway",
    "funicular": "cableway",
    "metro": "train",
}


class TransportError(Exception):
    pass


# ------------------------------------------------------------------------------------------------ the mapping
def _fixed(t: dt.datetime) -> dt.datetime:
    """Swiss wall time with a fixed UTC offset: arithmetic on a ZoneInfo time drops the fold (the repeated autumn
    hour), a fixed offset keeps the real instant."""
    t = t.astimezone(SWISS)
    off = t.utcoffset()
    return t.replace(tzinfo=dt.timezone(off)) if off is not None else t


def sc_time(s: Any, anchor: dt.datetime) -> str | None:
    """search.ch 'YYYY-MM-DD HH:MM:SS' (Swiss wall time, no offset) -> ISO with offset. In the repeated autumn hour
    the wall time is two instants: the earliest one not before anchor (the asked time), else the latest."""
    try:
        naive = dt.datetime.fromisoformat(str(s or "").strip())
    except ValueError:
        return None
    if naive.tzinfo:
        return naive.isoformat()
    cands = sorted({_fixed(naive.replace(tzinfo=SWISS, fold=f)) for f in (0, 1)})
    later = [t for t in cands if t >= _fixed(anchor) - MIN]
    return (later[0] if later else cands[-1]).isoformat()


def sc_delay(v: Any) -> int | None:
    """search.ch delay '+3' / '-1' / '0' -> minutes; 'X' (cancelled), '' or none -> None."""
    s = str(v if v is not None else "").strip()
    return int(s) if re.fullmatch(r"[+-]?\d+", s) else None


def _cancelled(*ds: Any) -> bool:
    return any(
        isinstance(d, dict)
        and (
            bool(d.get("cancelled") or d.get("canceled") or d.get("isCancelled"))
            or any(str(d.get(k) or "").strip().upper() == "X" for k in ("dep_delay", "arr_delay"))
        )
        for d in ds
    )


def _where(name: Any, stopid: Any, ride: bool = True) -> dict[str, Any]:
    """A stop -> opendata's station; a walk's address end (no stop id) -> its location."""
    if stopid or ride:
        return {"station": {"id": str(stopid) if stopid else None, "name": name}}
    return {"location": {"name": name}}


def _track(v: Any) -> tuple[str, dict[str, Any]]:
    """search.ch track -> (platform, prognosis): a changed one ('13!') is only known as the new platform."""
    t = str(v or "").strip()
    if t.endswith("!"):
        return "", {"platform": t.rstrip("!")}
    return t, {}


def _section(leg: dict[str, Any], anchor: dt.datetime) -> dict[str, Any] | None:
    """One search.ch leg -> an opendata section (ride or walk); the end point and empty walks -> None."""
    x = leg.get("exit") or {}
    if not x or not leg.get("departure"):
        return None
    kind = str(leg.get("type") or "").lower()
    line = str(leg.get("line") or "").strip()
    ride = not (kind == "walk" or not (line or leg.get("*G") or leg.get("number")))
    dpl, dprog = _track(leg.get("track"))
    apl, aprog = _track(x.get("track"))
    dep = {
        "departure": sc_time(leg["departure"], anchor),
        "delay": sc_delay(leg.get("dep_delay")),
        "platform": dpl,
        "prognosis": dprog,
        **_where(leg.get("name"), leg.get("stopid"), ride),
    }
    arr = {
        "arrival": sc_time(x.get("arrival"), anchor),
        "delay": sc_delay(x.get("arr_delay")),
        "platform": apl,
        "prognosis": aprog,
        **_where(x.get("name"), x.get("stopid"), ride),
    }
    if not ride:
        secs = leg.get("runningtime") or leg.get("normal_time") or 0
        try:
            secs = int(secs)
        except (TypeError, ValueError):
            secs = 0
        return {"journey": None, "walk": {"duration": secs}, "departure": dep, "arrival": arr} if secs > 0 else None
    cat, num = str(leg.get("*G") or "").strip(), str(leg.get("*L") or "").strip()
    if not cat:  # no category field: 'IC 8', 'S13', a bare tram/bus '17'
        m = re.fullmatch(r"([A-Za-z]*)\s*(.*)", line or str(leg.get("number") or "").split(" ")[0])
        cat, num = (m.group(1), m.group(2).strip()) if m else ("", line)
    if kind in ("bus", "post", "night_bus") and cat.upper() not in BUS_CATS:
        cat = "B"
    elif kind == "tram" and cat.upper() not in TRAM_CATS:
        cat = "T"
    if not num and cat and line.upper().startswith(cat.upper()):
        num = line[len(cat) :].strip()
    journey: dict[str, Any] = {
        "category": cat,
        "number": num,
        "to": leg.get("terminal"),
        "operator": str(leg.get("operator") or "").strip() or None,
        "name": " ".join(str(leg.get(k) or "") for k in ("line", "type_name")).strip(),
    }
    if _cancelled(leg, x):
        journey["cancelled"] = True
    return {"journey": journey, "walk": None, "departure": dep, "arrival": arr}


def _duration(secs: Any) -> str | None:
    try:
        s = int(secs)
    except (TypeError, ValueError):
        return None
    return f"{s // 86400:02d}d{s % 86400 // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def searchch_connections(data: dict[str, Any], anchor: dt.datetime) -> list[dict[str, Any]]:
    """search.ch route.json -> opendata /connections rows (anchor: the asked time, for the repeated hour)."""
    out = []
    for c in data.get("connections") or []:
        if not isinstance(c, dict):
            continue
        legs = [x for x in (c.get("legs") or []) if isinstance(x, dict)]
        sections = [s for s in (_section(x, anchor) for x in legs) if s]
        rides = sum(1 for s in sections if s["journey"])
        out.append(
            {
                "from": {
                    "departure": sc_time(c.get("departure"), anchor),
                    "delay": sc_delay(c.get("dep_delay")),
                    "station": {"name": c.get("from")},
                    "prognosis": {},
                },
                "to": {
                    "arrival": sc_time(c.get("arrival"), anchor),
                    "delay": sc_delay(c.get("arr_delay")),
                    "station": {"name": c.get("to")},
                    "prognosis": {},
                },
                "duration": _duration(c.get("duration")),
                "transfers": max(rides - 1, 0),
                "cancelled": _cancelled(c),
                "sections": sections,
            }
        )
    return out


def searchch_locations(data: Any, type: str = "all") -> list[dict[str, Any]]:  # noqa: A002 (opendata's name)
    """search.ch completion.json -> opendata /locations stations; type "station": stops only (with an id)."""
    out = []
    for e in data if isinstance(data, list) else []:
        if not isinstance(e, dict) or not e.get("label"):
            continue
        if type == "station" and not e.get("id"):
            continue
        lat, lon = e.get("lat"), e.get("lon")
        coord = (
            {"type": "WGS84", "x": float(lat), "y": float(lon)}
            if isinstance(lat, (int, float)) and isinstance(lon, (int, float))
            else {"type": "WGS84", "x": None, "y": None}
        )
        icon = str(e.get("iconclass") or "").rsplit("-", 1)[-1]
        out.append(
            {
                "id": str(e["id"]) if e.get("id") else None,
                "name": e["label"],
                "score": None,
                "coordinate": coord,
                "distance": e.get("dist"),
                "icon": ICONS.get(icon),
            }
        )
    return out


# ------------------------------------------------------------------------------------------------ the client
def _why(data: Any) -> str:
    """The reason in an error payload: opendata {"errors": [{"message"}]}, search.ch {"messages": [...]}."""
    for k in ("errors", "messages"):
        for x in (data.get(k) or []) if isinstance(data, dict) else []:
            why = x.get("message") if isinstance(x, dict) else x
            if why:
                return str(why)
    return ""


class Client:
    def __init__(
        self,
        api: str | None = None,
        fallback: str | None = None,
        ua: str | None = None,
        timeout: float = 20,
        on_source: Callable[[str, str], None] | None = None,
    ) -> None:
        self.api = (api or os.environ.get("TRANSPORT_API") or OPENDATA).rstrip("/")
        self.fallback = (fallback or os.environ.get("TRANSPORT_FALLBACK_API") or SEARCHCH).rstrip("/")
        self.ua = ua or "assistant-transport/1 (personal trip planner)"
        self.timeout = timeout
        self.on_source = on_source
        self.down: dict[str, str] = {}  # opendata endpoint -> why it failed in this client
        self.seen: set[str] = set()

    def get(self, base: str, path: str, params: dict[str, Any]) -> Any:
        """GET base/path?params -> the JSON. TransportError on a non-2xx status, a timeout, no JSON or an error
        payload ({"errors": ...} even with 200; search.ch "messages" with nothing else)."""
        url = f"{base}/{path}?{urllib.parse.urlencode(params)}"
        try:
            with http.open(url, headers={"User-Agent": self.ua}, timeout=self.timeout) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            try:
                why = _why(json.load(e))
            except Exception:
                why = ""
            raise TransportError(f"transport API error: {why or f'HTTP {e.code}'}") from None
        except (OSError, ValueError) as e:
            raise TransportError(f"transport API unreachable or broken: {getattr(e, 'reason', e)}") from None
        if isinstance(data, dict):
            if data.get("errors") or (data.get("messages") and not data.get("connections")):
                raise TransportError(f"transport API error: {_why(data) or 'error payload'}")
            return data
        if isinstance(data, list) and path.startswith("completion"):
            return data
        raise TransportError("transport API broken: not a JSON object")

    def _said(self, src: str, why: str = "") -> None:
        if src not in self.seen:
            self.seen.add(src)
            if self.on_source:
                self.on_source(src, why)

    def _opendata(self, path: str, params: dict[str, Any]) -> Any:
        """opendata's answer, or None when it failed (now or earlier in this client)."""
        if path in self.down:
            return None
        try:
            data = self.get(self.api, path, params)
        except TransportError as e:
            self.down[path] = str(e)
            return None
        self._said("opendata")
        return data

    def _searchch(self, path: str, endpoint: str, params: dict[str, Any]) -> Any:
        why = self.down[path]
        if self.fallback == "off":
            raise TransportError(why)
        try:
            data = self.get(self.fallback, endpoint, params)
        except TransportError as e:
            raise TransportError(f"{why}; fallback search.ch: {e}") from None
        self._said("search.ch", f"opendata: {why}")
        return data

    def connections(
        self,
        frm: str,
        to: str,
        when: dt.datetime | dt.date,
        limit: int = 4,
        arrive: bool = False,
        keep_before: float = 0,
    ) -> list[dict[str, Any]]:
        if isinstance(when, dt.datetime):
            when = _fixed(when.replace(tzinfo=SWISS) if when.tzinfo is None else when)
            clock: str | None = when.strftime("%H:%M")
        else:  # a day without a time: from now (today) or its start
            now = dt.datetime.now(SWISS)
            clock = None
            when = _fixed(now if when == now.date() else dt.datetime.combine(when, dt.time(), SWISS))
        p: dict[str, Any] = {"from": frm, "to": to, "date": when.date().isoformat(), "limit": limit}
        if clock:
            p["time"] = clock
        if arrive:
            p["isArrivalTime"] = 1
        data = self._opendata("connections", p)
        if data is not None:
            return data.get("connections") or []
        q: dict[str, Any] = {"from": frm, "to": to, "date": when.strftime("%d.%m.%Y"), "time": when.strftime("%H:%M")}
        if arrive:
            q.update(time_type="arrival", num=1, pre=limit)
        else:
            # pre=2: search.ch leaves a slow connection out when it would head the list (an IR at the asked minute
            # behind a faster IC soon after); asking two earlier ones keeps it, and those before `when` are dropped
            q.update(num=limit, pre=2)
        q.update(show_delays=1, show_trackchanges=1)
        data = self._searchch("connections", "route.json", q)
        start = when.replace(second=0, microsecond=0)
        rows = searchch_connections(data, start)

        def at(c: dict[str, Any], end: str, key: str) -> dt.datetime | None:
            s = (c.get(end) or {}).get(key)
            return dt.datetime.fromisoformat(s) if s else None

        if arrive:
            return [c for c in rows if (at(c, "to", "arrival") or start) <= start][-limit:]
        keep = start - dt.timedelta(minutes=keep_before)
        early = [c for c in rows if keep <= (at(c, "from", "departure") or start) < start]
        return early + [c for c in rows if (at(c, "from", "departure") or start) >= start][:limit]

    def locations(
        self, query: str | None = None, x: float | str | None = None, y: float | str | None = None, type: str = "all"
    ) -> list[dict[str, Any]]:  # noqa: A002 (opendata's name)
        p: dict[str, Any] = {"type": type}
        if query:
            p["query"] = query
        else:
            p.update(x=x, y=y)
        data = self._opendata("locations", p)
        if data is not None:
            return data.get("stations") or []
        q: dict[str, Any] = {"show_ids": 1, "show_coordinates": 1}
        if query:
            q["term"] = query
        else:
            q["latlon"] = f"{x},{y}"
        return searchch_locations(self._searchch("locations", "completion.json", q), type)
