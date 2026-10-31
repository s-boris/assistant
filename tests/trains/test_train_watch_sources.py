"""train-watch's timetable sources: transport.opendata.ch and the timetable.search.ch fallback read into one internal
shape, and the switch (an opendata error payload, a non-2xx status, a timeout or junk -> search.ch, opendata asked
once per run, the answering source logged); Belgian legs and stations opendata resolves elsewhere go to iRail, and a
station no source resolves right is "can't watch", never "cancelled".

Fixtures (fixtures/): search.ch route.json answers and opendata's router-outage payload recorded live; opendata's
/connections built to its documented shape for the same two trips (the API was down when the others were recorded).
iRail: /connections Antwerpen-Centraal -> Brussels Airport recorded live (intermediate stops trimmed) and its
unknown-stop answer; opendata's answer for the same trip (it resolves "Brussels Airport" to Schiphol Airport).
scripts/train-watch is loaded from the repo (its sources: core.transport); HTTP goes through a stub of core.http's
open, so no network."""

from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import io
import json
import re
import sys
import urllib.error
import urllib.parse
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
        "TRAIN_WATCH_IRAIL_API": "https://irail.test/v1",
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
    monkeypatch.setattr(mod.transport.http, "open", mod.transport.http.open)  # Net replaces it: restored after
    return mod


class Net:
    """core.http.open stand-in: answers by host from a script of (kind, payload); records every URL."""

    def __init__(self, tw, **answers):
        self.urls: list[str] = []
        self.answers = answers
        tw.transport.http.open = self.open

    @staticmethod
    def host(url):
        return next((h for h in ("opendata", "irail") if f"{h}.test" in url), "searchch")

    def open(self, url, headers=None, timeout=None):
        self.urls.append(url)
        assert timeout == 25 and headers and "User-Agent" in headers
        kind, payload = self.answers[self.host(url)]
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        if kind == "ok":
            return io.BytesIO(body)
        if kind.startswith("http"):  # "http" = 503, "http400" = that status
            code = int(kind[4:] or 503)
            raise urllib.error.HTTPError(url, code, "Error", {}, io.BytesIO(body))  # type: ignore[arg-type]
        if kind == "timeout":
            raise TimeoutError("timed out")
        raise AssertionError(kind)

    def hosts(self):
        return [self.host(u) for u in self.urls]


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
        for c in conns(tw, tw.transport.searchch_connections(fixture("searchch-route.json"), at(tw, "17:36")))
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
        for c in conns(tw, tw.transport.searchch_connections(fixture("searchch-route.json"), at(tw, "17:36")))
    }
    assert set(od) == {"17:36", "18:33"}
    for k in od:
        assert sc[k] == od[k], k


def test_searchch_walks_and_trams(tw):
    rows = conns(tw, tw.transport.searchch_connections(fixture("searchch-route-walk-tram.json"), at(tw, "17:00")))
    c = rows[0]
    assert c["lines"] == ["IC8", "Tram 17"]  # the walks are no rides
    assert c["sdep"].strftime("%H:%M") == "17:02" and c["sarr"].strftime("%H:%M") == "18:19"
    assert [r["bus"] for r in c["rides"]] == [False, False]
    assert c["rides"][1]["platform"] == ""


def test_searchch_track_change_cancellation_and_replacement_bus(tw):
    data = fixture("searchch-route.json")
    legs = [x for x in data["connections"] if x["departure"].endswith("17:36:00")][0]["legs"]
    legs[0]["track"] = "13!"  # show_trackchanges: the new track, marked
    c = conns(tw, tw.transport.searchch_connections(data, at(tw, "17:36")))
    r = next(x for x in c if x["sdep"].strftime("%H:%M") == "17:36")["rides"][0]
    assert (r["platform"], r["new_platform"]) == ("", "13")

    data = fixture("searchch-route.json")
    legs = [x for x in data["connections"] if x["departure"].endswith("17:36:00")][0]["legs"]
    legs[0]["dep_delay"] = "X"
    c = next(
        x
        for x in conns(tw, tw.transport.searchch_connections(data, at(tw, "17:36")))
        if x["sdep"].strftime("%H:%M") == "17:36"
    )
    assert c["cancelled"] and not tw.plain(c)

    data = fixture("searchch-route.json")
    leg = [x for x in data["connections"] if x["departure"].endswith("17:36:00")][0]["legs"][0]
    leg.update({"type": "bus", "*G": "EV", "*L": "", "line": "EV", "type_name": "Ersatzbus"})
    c = next(
        x
        for x in conns(tw, tw.transport.searchch_connections(data, at(tw, "17:36")))
        if x["sdep"].strftime("%H:%M") == "17:36"
    )
    assert (c["lines"], c["rides"][0]["bus"], c["rides"][0]["ev"]) == (["EV"], True, True)


@pytest.mark.parametrize(
    "dep,arr,lines,found",
    [
        ("17:04", "18:22", ["Tram 4", "IC8", "Tram 17"], True),  # another tram/walk to the planned IC 8: the same trip
        ("17:04", "18:22", ["Tram 4", "IC5", "Tram 17"], False),  # no planned train in it
        ("17:04", "18:26", ["Tram 4", "IC8", "Tram 17"], False),  # arrives too far off
        ("17:04", "18:22", ["Tram 4", "Tram 17"], False),  # trams only: no train to hold on to
    ],
)
def test_match_same_train_another_way(tw, dep, arr, lines, found):
    rows = conns(tw, tw.transport.searchch_connections(fixture("searchch-route-walk-tram.json"), at(tw, "17:00")))
    g = {"dep": f"2026-10-30T{dep}:00+01:00", "arr": f"2026-10-30T{arr}:00+01:00", "lines": lines}
    c = tw.match(g, rows)
    assert (c is not None) == found
    if found:
        assert c["sdep"].strftime("%H:%M") == "17:02" and c["lines"] == ["IC8", "Tram 17"]


def test_searchch_wall_times_in_the_repeated_hour(tw):
    first = tw.iso("2031-10-26T02:20:00+02:00")  # 02:20 CEST, the first pass
    second = tw.iso("2031-10-26T02:20:00+01:00")  # 02:20 CET, an hour later
    assert tw.transport.sc_time("2031-10-26 02:20:00", first) == "2031-10-26T02:20:00+02:00"
    assert tw.transport.sc_time("2031-10-26 02:20:00", second) == "2031-10-26T02:20:00+01:00"
    assert tw.transport.sc_time("2031-10-26 02:50:00", first) == "2031-10-26T02:50:00+02:00"
    assert tw.transport.sc_time("2026-10-30 17:36:00", at(tw, "17:00")) == "2026-10-30T17:36:00+01:00"
    assert tw.transport.sc_time("", first) is None


@pytest.mark.parametrize("raw,want", [("+3", 3), ("-1", -1), ("0", 0), ("X", None), ("", None), (None, None)])
def test_searchch_delay(tw, raw, want):
    assert tw.transport.sc_delay(raw) == want


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
    assert [c["sdep"].strftime("%H:%M") for c in rows][:3] == [
        "17:31",
        "17:36",
        "18:02",
    ]  # pre rows over 5 min early dropped
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


# ------------------------------------------------------------------------------------------------ iRail (Belgium)
BE_LEG = {
    "id": "be000001",
    "from": "Antwerpen-Centraal",
    "to": "Brussels Airport",
    "dep": "2026-11-12T08:49:00+01:00",
    "arr": "2026-11-12T09:19:00+01:00",
    "lines": [],
    "title": "Antwerpen-Centraal → Brussels Airport",
    "source": "calendar",
}


def irail(**changes):
    """The recorded iRail answer; changes = {"<connection index>.<departure|arrival>.<field>": value}."""
    data = fixture("irail-connections.json")
    for path, v in changes.items():
        i, end, field = path.split(".")
        if field == "platform":
            data["connection"][int(i)][end]["platforminfo"] = {"name": v, "normal": "0"}
        else:
            data["connection"][int(i)][end][field] = v
    return data


def belgian_net(tw, irail_answer=None, opendata=None):
    return Net(
        tw,
        opendata=opendata or ("ok", fixture("opendata-schiphol.json")),
        irail=irail_answer or ("ok", irail()),
        searchch=("ok", {}),
    )


def test_irail_maps_to_the_internal_shape(tw):
    rows = conns(tw, tw.transport.irail_connections(irail()))
    got = [view(c) for c in rows]
    assert [(c["sdep"][11:16], c["sarr"][11:16], c["lines"]) for c in got] == [
        ("08:19", "08:49", ["EC"]),
        ("08:49", "09:19", ["IC"]),
        ("08:54", "09:47", ["IC", "IC"]),
    ]
    assert got[0]["sdep"].endswith("+01:00")
    assert got[2]["rides"] == [
        ("IC", "Antwerp-Central", "Brussels-North", "24", "", 0, 0, False, False, False),
        ("IC", "Brussels-North", "Brussels Airport - Zaventem", "6", "", 0, 0, False, False, False),
    ]
    assert not any(c["cancelled"] for c in got)


def test_schiphol_mismatch_goes_to_irail_and_the_leg_is_on_time(tw):
    """opendata resolves "Brussels Airport" to Schiphol Airport and finds nothing: iRail has the planned train."""
    net = belgian_net(tw)
    st: dict = {"noted": []}
    found, c, missing = tw.assess(BE_LEG, at(tw, "07:00", "2026-11-12"), st, live=False)
    assert (found, missing) == ([], False)
    assert c["sdep"] == at(tw, "08:49", "2026-11-12") and c["lines"] == ["IC"] and c["rides"][0]["platform"] == "23"
    assert net.hosts() == ["opendata", "irail"]
    u = urllib.parse.urlsplit(net.urls[1])
    assert u.path == "/v1/connections/"
    q = dict(urllib.parse.parse_qsl(u.query))
    assert q == {"from": "Antwerpen-Centraal", "to": "Brussels Airport", "date": "121126", "time": "0849",
                 "timesel": "departure", "results": "4", "format": "json", "lang": "en"}  # fmt: skip
    tw.connections(BE_LEG, at(tw, "08:09", "2026-11-12"), 6)  # the alternative's lookup: iRail straight away
    assert net.hosts() == ["opendata", "irail", "irail"]
    assert (Path(tw.DIR) / "sources.log").read_text(encoding="utf-8").count(" irail") == 1


def test_irail_delay_and_platform_change(tw):
    late = {"1.departure.delay": "420", "1.arrival.delay": "300", "1.departure.platform": "21"}  # seconds
    belgian_net(tw, ("ok", irail(**late)))
    found, c, _ = tw.assess(BE_LEG, at(tw, "08:20", "2026-11-12"), {"noted": []})
    keys = [k for k, _ in found]
    assert keys == ["delay5", "platform:Antwerp-Central:21"]
    assert (c["ddelay"], c["adelay"], c["dep"]) == (7, 5, at(tw, "08:56", "2026-11-12"))
    assert "leaves 08:56 (+7 min), arrives 09:24 (+5 min)" in found[0][1]


def test_irail_cancelled(tw):
    belgian_net(tw, ("ok", irail(**{"1.departure.canceled": "1"})))
    found, c, missing = tw.assess(BE_LEG, at(tw, "08:20", "2026-11-12"), {"noted": []})
    assert c["cancelled"] and not missing
    assert found[0] == ("cancelled", "cancelled: IC 08:49 from Antwerp-Central")


def test_belgian_stops_on_opendata_go_to_irail(tw):
    od = fixture("opendata-schiphol.json")
    od["to"] = {"id": "8819406", "name": "Brussels Airport"}  # resolved right, but Belgian: iRail is live there
    net = belgian_net(tw, opendata=("ok", od))
    found, c, _ = tw.assess(BE_LEG, at(tw, "07:00", "2026-11-12"), {"noted": []}, live=False)
    assert not found and c["lines"] == ["IC"] and net.hosts() == ["opendata", "irail"]


def test_belgian_stop_unknown_on_irail_stays_on_opendata(tw):
    od = fixture("opendata-connections.json")
    od["from"], od["to"] = {"id": "8821006", "name": "Bern"}, {"id": "8503000", "name": "Zürich HB"}
    net = belgian_net(tw, ("http400", fixture("irail-unknown-stop.json")), ("ok", od))
    rows = tw.connections({"from": "Bern", "to": "Zürich HB"}, at(tw, "17:36"), 4)
    assert [c["lines"] for c in rows] == [["IR16"], ["EC", "IC5"]]
    assert net.hosts() == ["opendata", "irail"]


@pytest.mark.parametrize(
    "irail_answer, why",
    [
        (("http400", fixture("irail-unknown-stop.json")), "no source knows the stations (Brussels Airport → Schiphol"),
        (("ok", irail()), "stations resolve elsewhere (Liège-Guillemins → Brussels Airport - Zaventem)"),
    ],
    ids=["unknown-on-irail", "elsewhere-on-irail"],
)
def test_station_mismatch_is_cant_watch_never_cancelled(tw, capsys, irail_answer, why):
    leg = dict(BE_LEG, to="Brussels Airport" if irail_answer[0] != "ok" else "Liège-Guillemins")
    belgian_net(tw, irail_answer)  # opendata: Schiphol Airport either way
    with pytest.raises(tw.Unwatchable, match=re.escape(why)):
        tw.assess(leg, at(tw, "08:40", "2026-11-12"), {"noted": []})
    # check: silent, no note, no miss counted; status: "can't watch", no problems
    now = at(tw, "08:40", "2026-11-12")
    tw.legs_save([leg], now)
    tw._ROUTE.clear()
    assert tw.cmd_check(argparse.Namespace(cron=True, dry_run=False, now=None), now) == 0
    assert capsys.readouterr().out == ""
    assert not list(Path(tw.INBOX).glob("*"))
    st = json.loads((Path(tw.DIR) / "state.json").read_text(encoding="utf-8"))["legs"][leg["id"]]
    assert st.get("miss", 0) == 0 and st["noted"] == []
    assert tw.cmd_status(argparse.Namespace(id=leg["id"], json=True, now=None), now) == 0
    out = json.loads(capsys.readouterr().out)
    assert (out["watch"], out["fine"], out["problems"], out["live"]) == (False, None, [], None)
    assert why in out["why"]


@pytest.mark.parametrize(
    "asked, got, same",
    [
        ("Brussels Airport", {"name": "Schiphol Airport"}, False),
        ("Leuven", {"name": "Leuk"}, False),
        ("Brussels Airport", {"name": "Brussels Airport - Zaventem"}, True),
        ("Antwerpen-Centraal", {"name": "Antwerp-Central", "standardname": "Antwerpen-Centraal"}, True),
        ("Geneva", {"name": "Genève"}, True),
        ("Zuerich HB", {"name": "Zürich HB"}, True),
        ("Airport", {"name": "Schiphol Airport"}, True),  # noise words alone: can't tell
    ],
)
def test_same_place(tw, asked, got, same):
    assert tw.same_place(asked, got) is same


# ------------------------------------------------------------------------------------------------ feeder buses
# opendata /connections recorded live (passList trimmed): Uster, Gschwader -> Zürich Flughafen starts with the
# regular Bus 725 (category B) to Uster station; the way back ends with it. A plan that lists only the trains, or the
# bus in the compact form `route` writes ("B725"), is fine; only rail-replacement markers or a bus in a planned
# train's place are a "replacement bus".
FEED_OUT = {
    "id": "fb000001",
    "from": "Uster, Gschwader",
    "to": "Zürich Flughafen",
    "dep": "2026-11-20T18:04:00+01:00",
    "arr": "2026-11-20T18:49:00+01:00",
    "lines": ["S9", "IC1"],
    "title": "Uster, Gschwader → Zürich Flughafen",
    "source": "calendar",
}
FEED_BACK = dict(
    FEED_OUT,
    id="fb000002",
    **{
        "from": "Zürich Flughafen",
        "to": "Uster, Gschwader",
        "dep": "2026-11-20T18:09:00+01:00",
        "arr": "2026-11-20T18:45:00+01:00",
        "lines": ["IC1", "S14"],
    },  # fmt: skip
)


def feeder_out(change=None):
    """The recorded way out; change(sections of the 18:04 connection) edits it."""
    data = fixture("opendata-feeder-bus-out.json")
    if change:
        change(data["connections"][0]["sections"])
    return data


def assess_feed(tw, leg, data):
    Net(tw, opendata=("ok", data), searchch=("ok", {}))
    return tw.assess(leg, at(tw, "17:30", "2026-11-20"), {"noted": []}, live=False)


@pytest.mark.parametrize(
    "leg, name, lines",
    [
        (FEED_OUT, "opendata-feeder-bus-out.json", ["Bus 725", "S9", "IC1"]),
        (FEED_BACK, "opendata-feeder-bus-back.json", ["IC1", "S14", "Bus 725"]),
    ],
    ids=["bus-first", "bus-last"],
)
def test_regular_feeder_bus_around_the_planned_trains_is_fine(tw, capsys, leg, name, lines):
    found, c, missing = assess_feed(tw, leg, fixture(name))
    assert (found, missing) == ([], False)
    assert c["lines"] == lines and [r["bus"] for r in c["rides"]] == [x.startswith("Bus") for x in lines]
    assert not any(r["ev"] for r in c["rides"]) and tw.plain(c)
    now = at(tw, "17:30", "2026-11-20")
    tw.legs_save([leg], now)
    tw._ROUTE.clear()
    assert tw.cmd_status(argparse.Namespace(id=leg["id"], json=True, now=None), now) == 0
    out = json.loads(capsys.readouterr().out)
    assert (out["fine"], out["problems"]) == (True, [])


def test_calendar_leg_with_the_compact_bus_name_lists_the_bus(tw):
    """`route` and the calendar write rides as category+number ("B725", "T10"): they are the planned bus and tram."""
    e = {
        "id": "ev-feeder",
        "summary": "🚆 Uster, Gschwader → Zürich Flughafen",
        "start": {"dateTime": FEED_OUT["dep"]},
        "end": {"dateTime": FEED_OUT["arr"]},
        "description": "18:00 walk 4 min › 18:04 B725 Uster, Loren › 18:10 S9 Uster pl 2 › 18:37 IC1 Zürich HB pl 33 › "
        "18:49 Zürich Flughafen · 2 changes\nSBB: https://app.sbbmobile.ch/timetable?from=Uster%2C%20Gschwader&"
        "to=Z%C3%BCrich%20Flughafen&time=1795194240",
    }
    leg = tw.from_event(e)
    assert leg["lines"] == ["Bus 725", "S9", "IC1"]
    found, c, _ = assess_feed(tw, leg, fixture("opendata-feeder-bus-out.json"))
    assert found == [] and c["lines"] == ["Bus 725", "S9", "IC1"]


@pytest.mark.parametrize(
    "text, lines",
    [
        ("18:10 B7 Alpha, Post › 18:18 S24 Alpha pl 3 › 19:02 Beta", ["Bus 7", "S24"]),
        ("07:57 S24 Beta pl 2 › 08:42 Alpha › 08:47 B7 Alpha, Bahnhof › walk 8 min › 09:00 home", ["S24", "Bus 7"]),
        (
            "16:58 Bus 21 › RE48 X › T10 Y › EV 5 Z › EXB12 W › BN3 V",
            ["Bus 21", "RE48", "Tram 10", "EV 5", "Bus 12", "Bus 3"],
        ),
        ("10:00 IC8 Bern pl 3 › 11:00 Zürich HB", ["IC8"]),
    ],
)
def test_compact_line_names_parse_alike_in_calcache(tw, text, lines):
    assert tw.parse_lines(text) == lines
    loader = importlib.machinery.SourceFileLoader("calcache_lines", str(REPO / "scripts" / "calcache"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec
    cc = importlib.util.module_from_spec(spec)
    loader.exec_module(cc)
    assert cc.parse_lines(text) == lines  # calcache's train fields: the same lines as train-watch's sync


def _set(i, **journey):
    def change(sections):
        sections[i]["journey"].update(journey)

    return change


@pytest.mark.parametrize(
    "change, key",
    [
        (_set(2, category="EV", number="9"), "bus:Uster"),  # Ersatzverkehr in place of the S9
        (_set(2, category="B", number=""), "bus:Uster"),  # an unmarked bus where the planned S9 was
        (_set(0, category="EXB", number="725"), "bus:Uster, Loren"),  # Extrabus: replacement even as the feeder
        (_set(0, name="Bahnersatz"), "bus:Uster, Loren"),  # a regular category named as rail replacement
    ],
    ids=["ev", "bus-for-planned-train", "exb-feeder", "ersatz-named"],
)
def test_rail_replacement_bus_is_still_a_problem(tw, change, key):
    found, c, _ = assess_feed(tw, FEED_OUT, feeder_out(change))
    assert key in [k for k, _ in found]
    assert next(t for k, t in found if k == key).startswith("replacement bus: ")
