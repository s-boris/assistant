"""App `usage` frames -> the analytics spool (usagefwd), and on into a scratch usage.db with the real lib (end to end)."""
import asyncio
import glob
import json
import os
import subprocess
import sys

from conftest import PROTOCOL
from phonegw.usagefwd import UsageForwarder, store_kind
from phonegw.util import now_s, ulid
from test_gateway_features import online

FIXTURE = json.load(open(os.path.join(PROTOCOL, "fixtures/valid/c2s.usage.json")))


def spooled(env):
    out = []
    for f in sorted(glob.glob(os.path.join(env.tmp, "product", "spool", "app-*.jsonl"))):
        out += [json.loads(line) for line in open(f)]
    return out


async def until(cond, n=60):
    for _ in range(n):
        if cond():
            return True
        await asyncio.sleep(0.05)
    return cond()


def test_kind_mapping():
    assert store_kind("view", "notify") == "notify_sent"
    assert store_kind("view", "screen") == "invoke"
    assert store_kind("open", "notify") == "notify_opened"
    assert store_kind("open", "widget") == "tap"
    assert store_kind("dismiss", "notify") == "dismissed"
    assert store_kind("tap", "tile") == "tap"
    assert store_kind("bogus", "tile") is None


def test_records_sanitized_and_capped(tmp_path):
    fw = UsageForwarder(state=str(tmp_path))
    body = dict(FIXTURE["body"], dropped=7)
    recs = fw.records("01JAB3K6Q2W8Z7M4D5N0P9R1V1", body)
    assert [r["f"] for r in recs] == ["app.tile.capture", "app.screen.board", "app.notify.timed", "app.trackid", "app"]
    assert [r["k"] for r in recs] == ["tap", "invoke", "notify_opened", "invoke", "error"]
    assert all(r["ch"] == "app" and r["src"] == "app" for r in recs)
    assert recs[1]["ms"] == 8400 and recs[1]["at"]["view"] is True and recs[1]["at"]["surface"] == "screen"
    assert recs[2]["at"]["level"] == "timed" and recs[2]["a"] == "owner"
    assert recs[3]["o"] == "queued"
    assert recs[4]["o"] == "usage_capped" and recs[4]["at"]["n"] == 7 and recs[4]["a"] == "system"
    assert len({r["x"] for r in recs}) == len(recs)


def test_route_switches_are_system_events(tmp_path):
    fw = UsageForwarder(state=str(tmp_path))
    body = {"app": "0.4.9", "items": [
        {"feature": "app.route.back", "kind": "invoke", "surface": "app", "at": 1792595000, "ms": 850, "outcome": "home"},
        {"feature": "app.route.network", "kind": "invoke", "surface": "app", "at": 1792595100, "ms": 1200,
         "outcome": "internet"},
        {"feature": "app.call.link", "kind": "invoke", "surface": "app", "at": 1792595150, "ms": 3400,
         "outcome": "resumed"},
        {"feature": "app.screen.board", "kind": "view", "surface": "screen", "at": 1792595200, "ms": 4000}]}
    recs = fw.records("01JAB3K6Q2W8Z7M4D5N0P9R1V2", body)
    assert [(r["f"], r["a"], r.get("o")) for r in recs] == [
        ("app.route.back", "system", "home"), ("app.route.network", "system", "internet"),
        ("app.call.link", "system", "resumed"), ("app.screen.board", "owner", None)]


async def test_usage_frame_acked_and_spooled_once(env, phone):
    await online(env, phone)
    sent = await phone.send("usage", FIXTURE["body"], ack=True)
    ack = await phone.recv("ack")
    assert sent["id"] in ack["body"]["ids"]
    assert await until(lambda: len(spooled(env)) == 4)
    # the outbox re-sends the same frame after a reconnect: deduped, nothing spooled twice
    await phone.send("usage", FIXTURE["body"], ack=True, msg_id=sent["id"])
    await phone.recv("ack")
    await asyncio.sleep(0.3)
    recs = spooled(env)
    assert len(recs) == 4
    assert {r["x"] for r in recs} == {"pg:u:%s:%d" % (sent["id"], i) for i in range(4)}
    assert all("text" not in json.dumps(r) for r in recs)


async def test_usage_with_text_is_refused(env, phone):
    await online(env, phone)
    bad = json.load(open(os.path.join(PROTOCOL, "fixtures/invalid/c2s.usage.text.json")))
    await phone.send("usage", bad["body"], ack=True)
    err = await phone.recv("error")
    assert err["body"]["code"] == "schema"
    await asyncio.sleep(0.2)
    assert spooled(env) == []


async def test_usage_over_https_sync(env, phone):
    await online(env, phone)
    st, j = await phone.http("POST", "/v1/events", {"items": [
        {"v": 1, "id": ulid(), "t": "usage", "ts": now_s(), "ack": True, "body": FIXTURE["body"]}]})
    assert st == 200 and j["accepted"] == 1
    assert await until(lambda: len(spooled(env)) == 4)


def test_spool_folds_into_usage_db(tmp_path):
    """The real lib (~/bin/lib/usage.py) folds the gateway's spool lines into a scratch usage.db."""
    state = tmp_path / "product"
    fw = UsageForwarder(state=str(state))
    assert fw.forward("01JAB3K6Q2W8Z7M4D5N0P9R1V1", FIXTURE["body"]) == 4
    lib = os.path.dirname(os.environ.get("PHONEGW_USAGE_LIB") or os.path.expanduser("~/bin/lib/usage.py"))
    code = ("import sys; sys.path.insert(0, %r); import usage, json; con = usage.connect(); idx = usage.load_index(); "
            "runs = usage.Runs(); usage.fold_spool(con, idx, runs); runs.flush(con); con.commit(); "
            "print(json.dumps([list(r) for r in con.execute('SELECT feature, kind, channel, actor, ms, outcome FROM events "
            "ORDER BY ts')]))" % lib)
    e = dict(os.environ, USAGE_STATE=str(state), USAGE="on")
    out = subprocess.run([sys.executable, "-c", code], env=e, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-800:]
    rows = json.loads(out.stdout.strip().splitlines()[-1])
    assert [r[0] for r in rows] == ["app.tile.capture", "app.screen.board", "app.notify.timed", "app.trackid"]
    assert [r[1] for r in rows] == ["tap", "invoke", "notify_opened", "invoke"]
    assert all(r[2] == "app" for r in rows) and rows[1][4] == 8400 and rows[3][5] == "queued"
