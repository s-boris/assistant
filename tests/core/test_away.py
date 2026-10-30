import datetime as dt
import json

from core import away

D = dt.date


def allday(a, b, on=True, **flags):
    return {"id": "x", "start": a, "end": b, "all_day": True, "title": "t", "flags": {"away_overnight": on, **flags}}


def timed(a, b, on=True):
    return {"id": "y", "start": a, "end": b, "all_day": False, "title": "t", "flags": {"away_overnight": on}}


def test_allday_event_covers_its_nights_end_exclusive():
    assert away.nights([allday("2030-03-05", "2030-03-07")]) == [D(2030, 3, 5), D(2030, 3, 6)]
    assert away.nights([allday("2030-03-05", "2030-03-05")]) == [D(2030, 3, 5)]  # bad end: one day, as calcache


def test_timed_event_counts_the_midnights_it_crosses():
    # 20:00 -> 06:00 next day: the night it starts on
    assert away.nights([timed("2030-03-05T20:00:00+01:00", "2030-03-06T06:00:00+01:00")]) == [D(2030, 3, 5)]
    # an evening flight that lands before midnight: no night (calcache's per-day rule)
    assert away.nights([timed("2030-03-05T20:15:00+01:00", "2030-03-05T21:45:00+01:00")]) == []
    # ends exactly at midnight: not across it
    assert away.nights([timed("2030-03-05T20:00:00+01:00", "2030-03-06T00:00:00+01:00")]) == []
    # a hotel stay 15:00 -> 11:00 two days later: two nights
    assert away.nights([timed("2030-03-05T15:00:00+01:00", "2030-03-07T11:00:00+01:00")]) == [
        D(2030, 3, 5),
        D(2030, 3, 6),
    ]
    # offsets differ across a DST change: the local wall clock decides
    assert away.nights([timed("2030-10-26T22:00:00+02:00", "2030-10-27T09:00:00+01:00")]) == [D(2030, 10, 26)]


def test_unflagged_and_malformed_events_are_ignored():
    evs = [
        allday("2030-03-05", "2030-03-09", on=False, abroad=True),
        allday("bad", "2030-03-09"),
        {"start": "2030-03-01", "all_day": True, "flags": {"away_overnight": True}},  # no end
        {"start": "2030-03-01", "end": "2030-03-04", "all_day": True, "flags": {"away_overnight": 1}},
        "garbage",
        None,
        {"flags": None},
    ]
    assert away.nights(evs) == []


def test_trips_chain_consecutive_nights_across_events():
    evs = [
        allday("2030-03-05", "2030-03-07"),  # nights 5, 6
        timed("2030-03-07T19:00:00+01:00", "2030-03-08T10:00:00+01:00"),  # night 7
        timed("2030-03-20T20:00:00+01:00", "2030-03-21T06:00:00+01:00"),  # a lone night out
    ]
    assert away.trips(evs, 2) == [
        {"from": D(2030, 3, 5), "last_night": D(2030, 3, 7), "back": D(2030, 3, 8), "nights": 3}
    ]
    assert [t["nights"] for t in away.trips(evs, 1)] == [3, 1]
    assert away.trips(evs, 4) == []
    assert away.trips(evs, 0) == away.trips(evs, 1)  # at least one night


def test_overlapping_events_count_each_night_once():
    evs = [allday("2030-03-05", "2030-03-08"), allday("2030-03-06", "2030-03-07"), allday("2030-03-07", "2030-03-09")]
    assert away.trips(evs, 2) == [
        {"from": D(2030, 3, 5), "last_night": D(2030, 3, 8), "back": D(2030, 3, 9), "nights": 4}
    ]


def test_runaway_span_is_capped():
    assert len(away.nights([allday("2030-01-01", "2090-01-01")])) == away.MAX_SPAN_DAYS


def test_events_and_load(tmp_path):
    assert away.events({"events": [1]}) == [1]
    assert away.events({"events": "x"}) == []
    assert away.events(None) == []
    p = tmp_path / "events.json"
    assert away.load(str(p)) == []  # missing
    p.write_text("{not json")
    assert away.load(str(p)) == []
    p.write_text(json.dumps({"events": [allday("2030-03-05", "2030-03-07")]}))
    assert [t["nights"] for t in away.trips(away.load(str(p)))] == [2]
