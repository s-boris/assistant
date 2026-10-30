"""Calendar helper tools against stub calcache / tripclock / plans-watch: argv, outputs, exit codes, path checks."""

from __future__ import annotations

from tools import gateway

EV = {
    "id": "e1",
    "start": "2026-10-24T18:00:00+02:00",
    "end": "2026-10-25T02:00:00+02:00",
    "all_day": False,
    "title": "Wedding",
    "town": "Exampletown",
    "free": True,
    "flags": {"travel": None, "busy_evening": True, "away_overnight": False, "abroad": False, "id_check": False},
}


def call(name, **args):
    return gateway.call(name, args)


def test_events_busy_towns_cache(stubs):
    stubs.on("calcache", ["show"], [EV], err="calcache: cache is 40 h old\n")
    day = {
        "date": "2026-10-24",
        "busy": [["18:00", "24:00"]],
        "evening_busy": True,
        "away_overnight": False,
        "all_day": 0,
        "travel": [],
        "covered": True,
    }
    stubs.on("calcache", ["busy"], [day, {"date": "2026-12-01", "busy": [], "covered": False}], rc=0)
    stubs.on("calcache", ["busy", "+40"], [{"date": "2026-12-01", "busy": [], "covered": False}], rc=4)
    stubs.on("calcache", ["towns"], [{"date": "2026-10-24", "towns": ["Exampletown"], "covered": True}])
    stubs.on("calcache", ["age"], {"cached": True, "fetched": "f", "hours": 2.5, "events": 16, "window": ["a", "b"]})

    e = call("calendar.events", days=3)
    assert e.ok and e.data["count"] == 1 and e.data["events"][0]["title"] == "Wedding"
    assert e.data["note"] == "calcache: cache is 40 h old" and "Wedding · Exampletown [free]" in e.text
    b = call("calendar.busy", date="2026-10-24", days=2)
    assert b.ok and b.data["days"][0]["busy"] == [["18:00", "24:00"]] and "not cached" in b.text
    out = call("calendar.busy", date="+40")
    assert out.ok and out.data["days"][0]["covered"] is False  # exit 4 = outside the window, not an error
    assert call("calendar.towns", date="today").data["days"][0]["towns"] == ["Exampletown"]
    assert call("calendar.cache").data["hours"] == 2.5
    calls = stubs.calls("calcache")
    assert ["show", "--days", "3", "--json"] in calls and ["busy", "2026-10-24", "--days", "2", "--json"] in calls
    assert ["towns", "today", "--days", "1", "--json"] in calls


def test_no_cache_and_missing_cache(stubs):
    stubs.on("calcache", ["show"], "", rc=3, err="calcache: no cache")
    stubs.on("calcache", ["age"], {"cached": False}, rc=1)
    r = call("calendar.events")
    assert r.code == "unavailable" and "calendar_import" in r.error["hint"]
    assert call("calendar.cache").data == {"cached": False}


def test_import_paths(stubs, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))  # the tool takes files under home or /tmp; CI's tmp_path is under neither
    f = tmp_path / "cal.json"
    f.write_text("{}")
    stubs.on("calcache", ["import"], "cached 16 events (3 travel, 1 cancelled/declined/skipped), a → b -> c\n")
    r = call("calendar.import", files=[str(f)])
    assert r.ok and r.data == {"events": 16, "travel": 3, "skipped": 1}
    assert stubs.calls("calcache")[-1] == ["import", str(f), "--days", "21", "--consume"]
    for bad in (
        "cal.json",
        str(tmp_path / "x.txt"),
        "/etc/passwd.json",
        "/tmp/../etc/x.json",
        str(tmp_path / ".secrets" / "x.json"),
    ):
        assert call("calendar.import", files=[bad]).code == "invalid_args", bad
    assert len(stubs.calls("calcache")) == 1


def test_clock_and_plans(stubs):
    clock = {
        "local": "2026-11-25 13:00",
        "zone": "Indian/Mauritius",
        "offset": "UTC+04:00",
        "home": "2026-11-25 10:00",
        "home_zone": "Europe/Zurich",
        "away": True,
    }
    stubs.on("tripclock", ["now"], clock)
    plan = {
        "key": "1a2b3c4d",
        "date": "2026-10-30",
        "start": "20:00",
        "state": "noted",
        "cat": "plan",
        "what": "Concert",
        "chat": "Alex",
        "extra": 1,
    }
    stubs.on("plans-watch", ["open"], [plan])
    stubs.on("plans-watch", ["done"], "plans-watch: 1 acked as added\n")
    stubs.on("plans-watch", ["done", "ffffffff"], "", rc=1, err="plans-watch: unknown key(s): ffffffff")
    c = call("calendar.clock", at="2026-11-25 10:00")
    assert c.ok and c.data["away"] is True and "home 2026-11-25 10:00" in c.text
    assert stubs.calls("tripclock")[-1] == ["now", "--json", "--at", "2026-11-25 10:00"]
    p = call("calendar.plans")
    assert p.data["count"] == 1 and p.data["plans"][0]["key"] == "1a2b3c4d" and "extra" not in p.data["plans"][0]
    d = call("calendar.plan_done", keys=["1a2b3c4d"], outcome="added", why="calendar")
    assert d.ok and d.data["acked"] == 1
    assert stubs.calls("plans-watch")[-1] == ["done", "1a2b3c4d", "--as", "added", "--why", "calendar"]
    assert call("calendar.plan_done", keys=["ffffffff"], outcome="skipped").code == "not_found"
    for args in (
        {"keys": ["XYZ"], "outcome": "added"},
        {"keys": ["1a2b3c4d"], "outcome": "deleted"},
        {"keys": ["1a2b3c4d"], "outcome": "added", "why": "two words"},
    ):
        assert call("calendar.plan_done", **args).code == "invalid_args"
    assert call("calendar.clock", at="--json").code == "invalid_args"
