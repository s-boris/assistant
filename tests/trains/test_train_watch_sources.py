"""train-watch's timetable sources: transport.opendata.ch and the timetable.search.ch fallback read into one internal
shape, and the switch (an opendata error payload, a non-2xx status, a timeout or junk -> search.ch, opendata asked
once per run, the answering source logged).

Fixtures (fixtures/): search.ch route.json answers and opendata's router-outage payload recorded live; opendata's
/connections built to its documented shape for the same two trips (the API was down when the others were recorded).
scripts/train-watch is loaded from the repo; HTTP goes through a stub of core.http's open, so no network."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "train-watch"
FIX = Path(__file__).with_name("fixtures")
ZH_OFFSET = "+01:00"


def fixture(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


@pytest.fixture
def tw(tmp_path, monkeypatch):
    prof = tmp_path / "profile.json"
    prof.write_text(json.dumps({"locale": {"tz": "Europe/Zurich"}}), encoding="utf-8")
    for k, v in {
        "ASSISTANT_PROFILE": str(prof),
        "TRAIN_WATCH_API": "https://opendata.test/v1",
        "TRAIN_WATCH_FALLBACK_API": "https://searchch.test/api",
        "TRAIN_WATCH_DIR": str(tmp_path / "state"),
        "TRAIN_WATCH_INBOX": str(tmp_path / "inbox"),
        "HOME_PLACE_GEO": "off",
        "HOME_PLACE_CACHE": str(tmp_path / "home-place.json"),
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(sys, "path", [str(REPO / "scripts" / "lib" / "legacy"), *sys.path])  # restored after
    loader = importlib.machinery.SourceFileLoader(f"train_watch_{tmp_path.name}", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class Net:
    """core.http.open stand-in: answers by host from a script of (kind, payload); records every URL."""

    def __init__(self, tw, **answers):
        self.urls: list[str] = []
        self.answers = answers
        tw.web.open = self.open

    def open(self, url, headers=None, timeout=None):
        self.urls.append(url)
        assert timeout == 25 and headers and "User-Agent" in headers
        kind, payload = self.answers["opendata" if "opendata.test" in url else "searchch"]
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        if kind == "ok":
            return io.BytesIO(body)
        if kind == "http":
            raise urllib.error.HTTPError(url, 503, "Service Unavailable", {}, io.BytesIO(body))  # type: ignore[arg-type]
        if kind == "timeout":
            raise TimeoutError("timed out")
        raise AssertionError(kind)

    def hosts(self):
        return ["opendata" if "opendata.test" in u else "searchch" for u in self.urls]


def at(tw, hhmm, day="2026-10-30"):
    return tw.iso(f"{day}T{hhmm}:00{ZH_OFFSET}")


def conns(tw, rows):
    return [c for c in map(tw.connection, rows) if c]


def view(c):
    """The fields the checks read, comparable across sources."""
    return {
        "sdep": c["sdep"].isoformat(),
        "sarr": c["sarr"].isoformat(),
        "dep": c["dep"].isoformat(),
        "arr": c["arr"].isoformat(),
        "ddelay": c["ddelay"],
        "adelay": c["adelay"],
        "lines": c["lines"],
        "cancelled": c["cancelled"],
        "rides": [
            (
                r["line"],
                r["from"],
                r["at"],
                r["platform"],
                r["new_platform"],
                r["ddelay"],
                r["adelay"],
                r["bus"],
                r["ev"],
                r["cancelled"],
            )
            for r in c["rides"]
        ],
    }


# ------------------------------------------------------------------------------------------------ mapping
def test_searchch_recorded_route_maps_to_the_internal_shape(tw):
    got = {
        c["sdep"].strftime("%H:%M"): c
        for c in conns(tw, tw.sc_connections(fixture("searchch-route.json"), at(tw, "17:36")))
    }
    ir = got["17:36"]
    assert view(ir) == {
        "sdep": "2026-10-30T17:36:00+01:00",
        "sarr": "2026-10-30T18:54:00+01:00",
        "dep": "2026-10-30T17:37:00+01:00",
        "arr": "2026-10-30T18:54:00+01:00",
        "ddelay": 1,
        "adelay": 0,
        "lines": ["IR16"],
        "cancelled": False,
        "rides": [("IR16", "Bern", "Zürich HB", "12", "", 1, 0, False, False, False)],
    }
    ec = got["18:33"]  # a late first train, a change at Olten
    assert (ec["ddelay"], ec["adelay"], ec["lines"]) == (12, 0, ["EC", "IC5"])
    assert [(r["from"], r["at"], r["platform"], r["ddelay"], r["adelay"]) for r in ec["rides"]] == [
        ("Bern", "Olten", "5", 12, 11),
        ("Olten", "Zürich HB", "7", 0, 0),
    ]
    assert tw.conn_text(ec) == "18:45 EC › IC5 → 19:56 (+12 min), pl 5, 1 change"


def test_both_sources_give_the_same_shape_for_the_same_trips(tw):
    od = {c["sdep"].strftime("%H:%M"): view(c) for c in conns(tw, fixture("opendata-connections.json")["connections"])}
    sc = {
        c["sdep"].strftime("%H:%M"): view(c)
        for c in conns(tw, tw.sc_connections(fixture("searchch-route.json"), at(tw, "17:36")))
    }
    assert set(od) == {"17:36", "18:33"}
    for k in od:
        assert sc[k] == od[k], k


def test_searchch_walks_and_trams(tw):
    rows = conns(tw, tw.sc_connections(fixture("searchch-route-walk-tram.json"), at(tw, "17:00")))
    c = rows[0]
    assert c["lines"] == ["IC8", "Tram 17"]  # the walks are no rides
    assert c["sdep"].strftime("%H:%M") == "17:02" and c["sarr"].strftime("%H:%M") == "18:19"
    assert [r["bus"] for r in c["rides"]] == [False, False]
    assert c["rides"][1]["platform"] == ""


def test_searchch_track_change_cancellation_and_replacement_bus(tw):
    data = fixture("searchch-route.json")
    legs = [x for x in data["connections"] if x["departure"].endswith("17:36:00")][0]["legs"]
    legs[0]["track"] = "13!"  # show_trackchanges: the new track, marked
    c = conns(tw, tw.sc_connections(data, at(tw, "17:36")))
    r = next(x for x in c if x["sdep"].strftime("%H:%M") == "17:36")["rides"][0]
    assert (r["platform"], r["new_platform"]) == ("", "13")

    data = fixture("searchch-route.json")
    legs = [x for x in data["connections"] if x["departure"].endswith("17:36:00")][0]["legs"]
    legs[0]["dep_delay"] = "X"
    c = next(x for x in conns(tw, tw.sc_connections(data, at(tw, "17:36"))) if x["sdep"].strftime("%H:%M") == "17:36")
    assert c["cancelled"] and not tw.plain(c)

    data = fixture("searchch-route.json")
    leg = [x for x in data["connections"] if x["departure"].endswith("17:36:00")][0]["legs"][0]
    leg.update({"type": "bus", "*G": "EV", "*L": "", "line": "EV", "type_name": "Ersatzbus"})
    c = next(x for x in conns(tw, tw.sc_connections(data, at(tw, "17:36"))) if x["sdep"].strftime("%H:%M") == "17:36")
    assert (c["lines"], c["rides"][0]["bus"], c["rides"][0]["ev"]) == (["EV"], True, True)


def test_searchch_wall_times_in_the_repeated_hour(tw):
    first = tw.iso("2031-10-26T02:20:00+02:00")  # 02:20 CEST, the first pass
    second = tw.iso("2031-10-26T02:20:00+01:00")  # 02:20 CET, an hour later
    assert tw.sc_time("2031-10-26 02:20:00", first) == "2031-10-26T02:20:00+02:00"
    assert tw.sc_time("2031-10-26 02:20:00", second) == "2031-10-26T02:20:00+01:00"
    assert tw.sc_time("2031-10-26 02:50:00", first) == "2031-10-26T02:50:00+02:00"
    assert tw.sc_time("2026-10-30 17:36:00", at(tw, "17:00")) == "2026-10-30T17:36:00+01:00"
    assert tw.sc_time("", first) is None


@pytest.mark.parametrize("raw,want", [("+3", 3), ("-1", -1), ("0", 0), ("X", None), ("", None), (None, None)])
def test_searchch_delay(tw, raw, want):
    assert tw.sc_delay(raw) == want


# ------------------------------------------------------------------------------------------------ the switch
@pytest.mark.parametrize(
    "opendata",
    [
        ("ok", fixture("opendata-router-error.json")),  # what it answers since the outage, also with 200
        ("http", fixture("opendata-router-error.json")),  # 503 with the same payload
        ("timeout", b""),
        ("ok", b"<html>maintenance</html>"),
    ],
    ids=["error-payload", "non-2xx", "timeout", "junk"],
)
def test_opendata_failing_falls_back_to_searchch(tw, opendata):
    net = Net(tw, opendata=opendata, searchch=("ok", fixture("searchch-route.json")))
    g = {"from": "Bern", "to": "Zürich HB"}
    rows = tw.connections(g, at(tw, "17:36"), 4)
    assert [c["sdep"].strftime("%H:%M") for c in rows][:3] == ["17:36", "18:02", "18:31"]  # earlier pre rows dropped
    tw.connections(g, at(tw, "17:36"), 6)  # the alternative's lookup in the same run
    assert net.hosts() == ["opendata", "searchch", "searchch"]  # the broken source once per run
    q = net.urls[1].split("?", 1)[1]
    for part in (
        "from=Bern",
        "to=Z%C3%BCrich+HB",
        "date=30.10.2026",
        "time=17%3A36",
        "num=4",
        "pre=2",
        "show_delays=1",
        "show_trackchanges=1",
    ):
        assert part in q.split("&"), (part, q)
    log = (Path(tw.DIR) / "sources.log").read_text(encoding="utf-8").splitlines()
    assert len(log) == 1 and " search.ch (opendata: transport API " in log[0]


def test_opendata_answering_is_used_and_logged(tw):
    net = Net(tw, opendata=("ok", fixture("opendata-connections.json")), searchch=("ok", {}))
    rows = tw.connections({"from": "Bern", "to": "Zürich HB"}, at(tw, "17:36"), 4)
    assert [c["lines"] for c in rows] == [["IR16"], ["EC", "IC5"]]
    assert net.hosts() == ["opendata"]
    assert "date=2026-10-30" in net.urls[0] and "limit=4" in net.urls[0]
    assert (Path(tw.DIR) / "sources.log").read_text(encoding="utf-8").rstrip().endswith(" opendata")


def test_both_failing_is_one_transport_error(tw):
    Net(tw, opendata=("ok", fixture("opendata-router-error.json")), searchch=("timeout", b""))
    with pytest.raises(tw.Fail) as e:
        tw.connections({"from": "Bern", "to": "Zürich HB"}, at(tw, "17:36"), 4)
    msg = str(e.value)
    assert msg.startswith("transport API error: The router is not reachable")
    assert "; fallback search.ch: transport API unreachable or broken: timed out" in msg


def test_searchch_message_without_connections_fails(tw):
    Net(
        tw,
        opendata=("http", fixture("opendata-router-error.json")),
        searchch=("ok", fixture("searchch-unknown-stop.json")),
    )
    with pytest.raises(tw.Fail, match="fallback search.ch: transport API error: Haltestelle .* nicht gefunden"):
        tw.connections({"from": "Xqzzyplonk", "to": "Bern"}, at(tw, "17:00"), 4)


def test_fallback_keeps_the_planned_slow_train(tw):
    """search.ch drops a slow train heading the list (IR 16 at the asked minute); pre=2 keeps it for match()."""
    Net(tw, opendata=("ok", fixture("opendata-router-error.json")), searchch=("ok", fixture("searchch-route.json")))
    g = {
        "from": "Bern",
        "to": "Zürich HB",
        "dep": "2026-10-30T17:36:00+01:00",
        "arr": "2026-10-30T18:54:00+01:00",
        "lines": ["IR16"],
    }
    c = tw.match(g, tw.connections(g, tw.iso(g["dep"]), 4))
    assert c and c["lines"] == ["IR16"] and c["ddelay"] == 1
