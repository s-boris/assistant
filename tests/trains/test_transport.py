"""core.transport: transport.opendata.ch first, timetable.search.ch when it fails, both read in opendata's shape.

Fixtures (fixtures/): search.ch route.json / completion.json answers and opendata's /locations recorded live (public
stations only); opendata's router-outage payload as it answers since the outage; its /connections built to its
documented shape (the router was down). HTTP goes through a stub of core.http's open: no network."""

from __future__ import annotations

import datetime as dt
import io
import json
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

from core import http, transport

FIX = Path(__file__).with_name("fixtures")
ZH = transport.SWISS


def fixture(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class Net:
    """core.http.open stand-in: answers by host and path from (kind, payload) entries; records every URL."""

    def __init__(self, monkeypatch, **answers):
        self.urls: list[str] = []
        self.answers = answers
        monkeypatch.setattr(http, "open", self.open)

    @staticmethod
    def key(url: str) -> str:
        u = urllib.parse.urlsplit(url)
        src = "opendata" if "opendata.test" in u.netloc else "searchch"
        return f"{src}_{u.path.rsplit('/', 1)[-1].split('.')[0]}"

    def open(self, url, headers=None, timeout=None):
        self.urls.append(url)
        assert headers and "User-Agent" in headers and timeout
        kind, payload = self.answers[self.key(url)]
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        if kind == "ok":
            return io.BytesIO(body)
        if kind == "http":
            raise urllib.error.HTTPError(url, 503, "Service Unavailable", {}, io.BytesIO(body))  # type: ignore[arg-type]
        if kind == "timeout":
            raise TimeoutError("timed out")
        raise AssertionError(kind)

    def keys(self) -> list[str]:
        return [self.key(u) for u in self.urls]

    def query(self, i: int) -> dict[str, str]:
        return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.urls[i]).query))


def client(**kw) -> transport.Client:
    return transport.Client(api="https://opendata.test/v1", fallback="https://searchch.test/api", **kw)


DOWN = ("ok", fixture("opendata-router-error.json"))


# ------------------------------------------------------------------------------------------------ mapping
def test_searchch_route_maps_to_opendata_shape():
    rows = transport.searchch_connections(
        fixture("searchch-route-walk-tram.json"), dt.datetime(2026, 10, 30, 17, tzinfo=ZH)
    )
    c = rows[0]
    assert (c["from"]["departure"], c["to"]["arrival"]) == ("2026-10-30T17:02:00+01:00", "2026-10-30T18:19:00+01:00")
    assert (c["duration"], c["transfers"], c["cancelled"]) == ("00d01:17:00", 1, False)
    kinds = [("ride" if s["journey"] else "walk") for s in c["sections"]]
    assert kinds == ["ride", "walk", "ride", "walk"]  # the zero-length walk at the start is dropped
    ic, walk, tram, last = c["sections"]
    assert ic["journey"] | {} == {
        "category": "IC",
        "number": "8",
        "to": "Romanshorn",
        "operator": "SBB",
        "name": "IC 8 Zug",
    }
    assert ic["departure"]["station"] == {"id": "8507000", "name": "Bern"} and ic["departure"]["platform"] == "2"
    assert (ic["departure"]["delay"], ic["arrival"]["platform"], ic["arrival"]["station"]["name"]) == (
        0,
        "34",
        "Zürich HB",
    )
    assert walk["walk"] == {"duration": 420} and walk["arrival"]["station"]["name"] == "Zürich, Sihlquai/HB"
    assert (tram["journey"]["category"], tram["journey"]["number"], tram["journey"]["operator"]) == (
        "T",
        "17",
        "VBZ    F",
    )
    assert last["walk"] == {"duration": 240} and last["arrival"]["location"] == {"name": "Zürich, Förrlibuckstr. 60"}


def test_searchch_track_change_and_cancellation():
    data = fixture("searchch-route.json")
    leg = next(x for x in data["connections"] if x["departure"].endswith("17:36:00"))["legs"][0]
    leg["track"], leg["dep_delay"] = "13!", "X"
    c = next(
        x
        for x in transport.searchch_connections(data, dt.datetime(2026, 10, 30, 17, 36, tzinfo=ZH))
        if x["from"]["departure"].startswith("2026-10-30T17:36")
    )
    d = c["sections"][0]["departure"]
    assert (d["platform"], d["prognosis"], d["delay"]) == ("", {"platform": "13"}, None)
    assert c["sections"][0]["journey"]["cancelled"] is True


def test_searchch_locations_by_name_and_by_point():
    stops = transport.searchch_locations(fixture("searchch-completion-term.json"), "station")
    assert stops and all(s["id"] for s in stops)
    assert stops[0] | {} == {
        "id": "8500010",
        "name": "Basel SBB",
        "score": None,
        "distance": None,
        "icon": "train",
        "coordinate": {"type": "WGS84", "x": 47.547403, "y": 7.589564},
    }
    every = transport.searchch_locations(fixture("searchch-completion-term.json"), "all")
    assert len(every) > len(stops)  # businesses and addresses too (no id)
    near = transport.searchch_locations(fixture("searchch-completion-latlon.json"), "station")
    assert near[0]["name"] == "Basel SBB" and near[0]["distance"] == 0
    assert [s["distance"] for s in near] == sorted(s["distance"] for s in near)
    od = fixture("opendata-locations.json")["stations"][0]
    assert set(od) == set(stops[0]) and od["id"] == stops[0]["id"]  # the same shape as opendata's own answer


# ------------------------------------------------------------------------------------------------ the switch
@pytest.mark.parametrize(
    "opendata",
    [DOWN, ("http", fixture("opendata-router-error.json")), ("timeout", b""), ("ok", b"<html>maintenance</html>")],
    ids=["error-payload", "non-2xx", "timeout", "junk"],
)
def test_departures_fall_back_to_searchch(monkeypatch, opendata):
    net = Net(
        monkeypatch, opendata_connections=opendata, searchch_route=("ok", fixture("searchch-route-night-back.json"))
    )
    said = []
    c = client(on_source=lambda s, why: said.append((s, why)))
    rows = c.connections("Basel SBB", "Zürich HB", dt.datetime(2026, 11, 7, 23, 0, tzinfo=ZH), limit=8)
    deps = [r["from"]["departure"][11:16] for r in rows]
    assert deps[:3] == ["23:07", "23:16", "00:04"]  # the two pre rows before 23:00 dropped
    c.connections("Basel SBB", "Zürich HB", dt.datetime(2026, 11, 8, 1, 0, tzinfo=ZH), limit=8)
    assert net.keys() == ["opendata_connections", "searchch_route", "searchch_route"]  # broken opendata asked once
    q = net.query(1)
    assert q | {} == {
        "from": "Basel SBB",
        "to": "Zürich HB",
        "date": "07.11.2026",
        "time": "23:00",
        "num": "8",
        "pre": "2",
        "show_delays": "1",
        "show_trackchanges": "1",
    }
    assert len(said) == 1 and said[0][0] == "search.ch" and said[0][1].startswith("opendata: transport API ")


def test_keep_before_keeps_rows_a_few_minutes_early(monkeypatch):
    Net(monkeypatch, opendata_connections=DOWN, searchch_route=("ok", fixture("searchch-route-night-back.json")))
    rows = client().connections("Basel SBB", "Zürich HB", dt.datetime(2026, 11, 7, 22, 50, tzinfo=ZH), keep_before=10)
    assert [r["from"]["departure"][11:16] for r in rows][:2] == ["22:43", "23:07"]


def test_arrivals_fall_back_to_searchch(monkeypatch):
    net = Net(monkeypatch, opendata_connections=DOWN, searchch_route=("ok", fixture("searchch-route-night-out.json")))
    rows = client().connections(
        "Zürich HB", "Basel SBB", dt.datetime(2026, 11, 7, 23, 45, tzinfo=ZH), limit=4, arrive=True
    )
    assert [r["to"]["arrival"][11:16] for r in rows] == ["22:52", "23:16", "23:23", "23:36"]  # by 23:45, the last 4
    q = net.query(1)
    assert (q["time_type"], q["pre"], q["num"], q["time"]) == ("arrival", "4", "1", "23:45")


def test_opendata_answering_is_used(monkeypatch):
    net = Net(monkeypatch, opendata_connections=("ok", fixture("opendata-connections.json")))
    said = []
    rows = client(on_source=lambda s, why: said.append((s, why))).connections(
        "Bern", "Zürich HB", dt.datetime(2026, 10, 30, 17, 36, tzinfo=ZH), arrive=True
    )
    assert len(rows) == 2 and net.keys() == ["opendata_connections"]
    assert net.query(0) | {} == {
        "from": "Bern",
        "to": "Zürich HB",
        "date": "2026-10-30",
        "time": "17:36",
        "limit": "4",
        "isArrivalTime": "1",
    }
    assert said == [("opendata", "")]


def test_a_day_without_a_time_asks_no_time(monkeypatch):
    net = Net(monkeypatch, opendata_connections=("ok", fixture("opendata-connections.json")))
    client().connections("Bern", "Zürich HB", dt.date(2026, 12, 24))
    assert "time" not in net.query(0) and net.query(0)["date"] == "2026-12-24"


def test_locations_fall_back_per_endpoint(monkeypatch):
    net = Net(
        monkeypatch,
        opendata_locations=("http", b"{}"),
        opendata_connections=("ok", fixture("opendata-connections.json")),
        searchch_completion=("ok", fixture("searchch-completion-term.json")),
    )
    c = client()
    assert c.locations(query="Basel SBB", type="station")[0]["name"] == "Basel SBB"
    assert net.query(1) | {} == {"term": "Basel SBB", "show_ids": "1", "show_coordinates": "1"}
    c.connections("Bern", "Zürich HB", dt.datetime(2026, 10, 30, 17, 36, tzinfo=ZH))  # opendata's router still asked
    assert net.keys() == ["opendata_locations", "searchch_completion", "opendata_connections"]


def test_locations_near_a_point(monkeypatch):
    net = Net(
        monkeypatch, opendata_locations=DOWN, searchch_completion=("ok", fixture("searchch-completion-latlon.json"))
    )
    near = client().locations(x=47.5474, y=7.5896, type="station")
    assert near[0]["id"] == "8500010" and net.query(1)["latlon"] == "47.5474,7.5896"


def test_opendata_locations_answering(monkeypatch):
    Net(monkeypatch, opendata_locations=("ok", fixture("opendata-locations.json")))
    assert client().locations(query="Basel SBB", type="station")[0]["coordinate"]["x"] == pytest.approx(
        47.5474, abs=1e-3
    )


def test_both_failing_is_one_error(monkeypatch):
    Net(monkeypatch, opendata_connections=DOWN, searchch_route=("ok", fixture("searchch-unknown-stop.json")))
    with pytest.raises(transport.TransportError) as e:
        client().connections("Bern", "Nowhere", dt.datetime(2026, 10, 30, 17, 36, tzinfo=ZH))
    msg = str(e.value)
    assert msg.startswith("transport API error: The router is not reachable")
    assert "; fallback search.ch: transport API error: " in msg


def test_fallback_off(monkeypatch):
    net = Net(monkeypatch, opendata_connections=DOWN)
    c = transport.Client(api="https://opendata.test/v1", fallback="off")
    with pytest.raises(transport.TransportError, match=r"^transport API error: The router is not reachable"):
        c.connections("Bern", "Zürich HB", dt.datetime(2026, 10, 30, 17, 36, tzinfo=ZH))
    assert net.keys() == ["opendata_connections"]


def test_repeated_autumn_hour_and_delays():
    first, second = (dt.datetime(2031, 10, 26, 2, 20, tzinfo=ZH, fold=f) for f in (0, 1))
    assert transport.sc_time("2031-10-26 02:20:00", first) == "2031-10-26T02:20:00+02:00"
    assert transport.sc_time("2031-10-26 02:20:00", second) == "2031-10-26T02:20:00+01:00"
    assert transport.sc_time("", first) is None
    assert [transport.sc_delay(x) for x in ("+3", "-1", "0", "X", "", None)] == [3, -1, 0, None, None, None]
