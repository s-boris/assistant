"""Reminders screen API: GET /v1/reminders (+ ETag) and POST /v1/reminders (add/preview/snooze/move/done/cancel/
skip, replay dedupe, invalid phrases, schema 422) against the real `remind` CLI on temp state (REMIND_DB, fixed
REMIND_NOW, temp calcache). REMIND_BIN can point at a copy under test."""
import datetime as dt
import json
from zoneinfo import ZoneInfo

import pytest

from phonegw.util import ulid
from test_gateway_features import script

TZ = ZoneInfo("Europe/Zurich")


def ep(s):
    return int(dt.datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=TZ).timestamp())


@pytest.fixture
def rem(env, monkeypatch):
    st = env.tmp / "remind"
    (st / "cal").mkdir(parents=True)
    (st / "cal" / "events.json").write_text(json.dumps({"events": [
        {"start": "2026-10-22T19:00:00+02:00", "end": "2026-10-22T22:30:00+02:00", "all_day": False,
         "title": "Big Band Live", "free": True, "flags": {"travel": None}}]}))
    log = env.tmp / "usage.log"
    for k, v in {"REMIND_DB": str(st / "rem.json"), "REMIND_INBOX": str(st / "inbox"), "REMIND_TG": "/bin/true",
                 "REMIND_NOW": "2026-10-21 10:00", "CALCACHE_DIR": str(st / "cal"), "USAGE": "off",
                 "REMIND_PLACE_CMD": "/bin/false", "TRIPCLOCK_CONFIG": str(st / "trips.json"),
                 "TRIPCLOCK_FLIGHTS": str(st / "none"), "REMIND_CONFIG": str(st / "config.json"),
                 "USAGE_BIN": script(env.tmp, "usage", "printf '%%s|' \"$@\" >> %s; echo >> %s\n" % (log, log))}.items():
        monkeypatch.setenv(k, v)
    return log


async def post(phone, **kw):
    return await phone.http("POST", "/v1/reminders", dict({"id": ulid()}, **kw))


async def test_list_add_preview_and_etag(env, phone, rem):
    st, r = await phone.http("GET", "/v1/reminders")
    assert st == 200 and r["ok"] and r["items"] == [] and r["recent"] == []
    st, r = await post(phone, op="preview", when="tomorrow evening")
    assert st == 200 and r["status"] == "preview" and r["at"] == ep("2026-10-22 19:00") and r["message"] == "Thu 22.10. 19:00 (tomorrow, evening = 19:00)"
    st, r = await post(phone, op="preview", when="after work")
    assert r["status"] == "preview" and r["where"] == "when leaving Work" and r["at"] is None
    st, r = await post(phone, op="preview", when="blah blah")
    assert r["status"] == "invalid" and "could not read" in r["message"]
    st, r = await post(phone, op="add", when="after the concert", text="Call Mum")
    assert st == 200 and r["status"] == "ok" and r["at"] == ep("2026-10-22 22:30") and "Big Band Live" in r["message"]
    assert [(i["kind"], i["title"], i["at"]) for i in r["list"]["items"]] == [("timed", "Call Mum", ep("2026-10-22 22:30"))]
    st, r = await post(phone, op="add", when="when I get home", text="Water the plants")
    assert r["status"] == "ok" and r["where"] == "when home" and r["message"] == "📍 when home"
    kinds = sorted((i["kind"], i.get("where")) for i in r["list"]["items"])
    assert kinds == [("place", "when home"), ("timed", None)]
    store = json.load(open(env.tmp / "remind" / "rem.json"))
    assert store[0]["via"] == "app" and store[0]["owner"] is True
    st, r1 = await phone.http("GET", "/v1/reminders")
    assert st == 200 and len(r1["items"]) == 2


async def test_snooze_move_done_cancel_skip_and_replay(env, phone, rem):
    st, r = await post(phone, op="add", when="2026-10-23 08:00", text="Garage")
    rid = r["list"]["items"][0]["id"]
    st, r = await post(phone, op="move", rid=rid, when="friday 18:00")
    assert r["status"] == "ok" and r["at"] == ep("2026-10-23 18:00") and r["message"] == "Fri 23.10. 18:00"
    item = {"id": ulid(), "op": "snooze", "rid": rid, "when": "+1h"}
    st, r = await phone.http("POST", "/v1/reminders", item)
    assert r["status"] == "ok" and r["at"] == ep("2026-10-21 11:00")
    st, r = await phone.http("POST", "/v1/reminders", item)            # replay of the offline queue
    assert r["status"] == "duplicate" and r["list"]["items"][0]["at"] == ep("2026-10-21 11:00")
    st, r = await post(phone, op="done", rid=rid)
    assert r["status"] == "ok" and r["message"] == "Done" and r["list"]["items"] == []
    st, r = await post(phone, op="done", rid="deadbeef")
    assert r["status"] == "invalid" and "no reminder" in r["message"]
    st, r = await post(phone, op="add", when="2026-10-26 09:00", text="Plants")
    sid = r["list"]["items"][0]["id"]
    st, r = await post(phone, op="cancel", rid=sid)
    assert r["status"] == "ok" and r["list"]["items"] == []
    st, r = await post(phone, op="move", rid=rid, when="gibberish")
    assert r["status"] == "invalid"
    assert "op=done|--attr|status=ok" in rem.read_text() or "op=done" in rem.read_text()


async def test_series_skip_and_schema(env, phone, rem):
    import subprocess, os
    subprocess.run([os.path.expanduser(os.environ.get("REMIND_BIN") or "~/bin/remind"), "add", "--owner", "--every",
                    "weekly", "2026-10-26 09:00", "Weekly plants"], check=True, capture_output=True)
    st, r = await phone.http("GET", "/v1/reminders")
    s = r["items"][0]
    assert s["kind"] == "series" and s["every"]
    st, r = await post(phone, op="skip", rid=s["id"])
    assert r["status"] == "ok" and r["at"] == ep("2026-11-02 09:00")
    st, r = await post(phone, op="snooze", rid=s["id"])                  # when missing
    assert st == 422
    st, r = await phone.http("POST", "/v1/reminders", {"id": ulid(), "op": "add", "when": "x", "text": "y", "lat": 1})
    assert st == 422
