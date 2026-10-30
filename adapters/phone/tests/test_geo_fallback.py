"""producers.stations: transport.opendata.ch /locations, else timetable.search.ch completion.json in opendata's shape
(an {"errors": ...} payload, a non-2xx status, a timeout or junk sends the lookup there). urlopen is stubbed: no
network. The search.ch answers are trimmed from live ones (public stations only)."""
import io
import json
import urllib.error
import urllib.parse

import pytest

from phonegw import producers as P

OD_ERR = {"errors": [{"message": "The router is not reachable: All connection attempts failed"}]}
OD_OK = {"stations": [{"id": "8500010", "name": "Basel SBB", "coordinate": {"type": "WGS84", "x": 47.547413,
                                                                           "y": 7.58956}, "distance": None}]}
SC_TERM = [
    {"label": "Basel SBB", "id": "8500010", "lon": 7.589564, "lat": 47.547403, "iconclass": "sl-icon-type-train"},
    {"label": "MediaMarkt Basel SBB, Basel, Güterstr. 115", "iconclass": "sl-icon-tel-business"},
]
SC_NEAR = [
    {"label": "Basel SBB (Haltestelle)", "dist": 2.97, "iconclass": "sl-icon-type-adr"},
    {"label": "Basel SBB", "id": "8500010", "lon": 7.589564, "lat": 47.547403, "dist": 0},
    {"label": "Basel, Bahnhof SBB", "id": "8578143", "lon": 7.590297, "lat": 47.548284, "dist": 110},
]


class Net:
    def __init__(self, monkeypatch, opendata, searchch):
        self.urls = []
        self.answers = {"opendata": opendata, "searchch": searchch}
        monkeypatch.setattr(P.urllib.request, "urlopen", self.open)

    def open(self, req, timeout=None):
        url = req.full_url
        self.urls.append(url)
        kind, payload = self.answers["opendata" if "opendata.ch" in url else "searchch"]
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        if kind == "http":
            raise urllib.error.HTTPError(url, 503, "Service Unavailable", {}, io.BytesIO(body))
        if kind == "timeout":
            raise TimeoutError("timed out")
        return io.BytesIO(body)


@pytest.mark.parametrize("opendata", [("ok", OD_ERR), ("http", OD_ERR), ("timeout", b""), ("ok", b"<html>")],
                         ids=["error-payload", "non-2xx", "timeout", "junk"])
def test_by_name_falls_back(monkeypatch, opendata):
    net = Net(monkeypatch, opendata, ("ok", SC_TERM))
    st = P.stations({"type": "station", "query": "Basel SBB"})
    assert st == [{"id": "8500010", "name": "Basel SBB", "distance": None,
                   "coordinate": {"type": "WGS84", "x": 47.547403, "y": 7.589564}}]
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(net.urls[1]).query))
    assert q == {"term": "Basel SBB", "show_ids": "1", "show_coordinates": "1"}


def test_near_a_point_keeps_the_address_row_for_the_caller(monkeypatch):
    net = Net(monkeypatch, ("http", OD_ERR), ("ok", SC_NEAR))
    st = P.stations({"x": "47.54740", "y": "7.58960", "type": "all"})
    assert [s["id"] for s in st] == [None, "8500010", "8578143"] and st[2]["distance"] == 110
    assert "latlon=47.54740%2C7.58960" in net.urls[1]


def test_opendata_answering_is_used(monkeypatch):
    net = Net(monkeypatch, ("ok", OD_OK), ("ok", []))
    assert P.stations({"type": "station", "query": "Basel SBB"})[0]["coordinate"]["x"] == 47.547413
    assert len(net.urls) == 1


def test_both_failing_raises(monkeypatch):
    Net(monkeypatch, ("http", OD_ERR), ("timeout", b""))
    with pytest.raises(OSError):
        P.stations({"type": "station", "query": "Basel SBB"})


def test_geocode_and_stations_near_through_the_fallback(monkeypatch, tmp_path):
    monkeypatch.delenv("GEO_FAKE", raising=False)
    monkeypatch.setenv("GEO_CACHE", str(tmp_path / "geo.json"))
    Net(monkeypatch, ("http", OD_ERR), ("ok", SC_NEAR))
    assert P.geocode(None, near=(47.5474, 7.5896)) == (47.547403, 7.589564)
    assert [n for n, _, _ in P.stations_near(47.5474, 7.5896)] == ["Basel SBB", "Basel, Bahnhof SBB"]
