"""GET /internal/export: the allow-listed slice the brain mirrors (hubkit.phonemirror)."""
import json

from phonegw import export as X
from phonegw.util import now_s


def _seed(db, dev):
    now = now_s()
    db.put_state(dev, "place", {"zone": "home", "band": "near", "town": "Townsville", "stop": "Main St 4",
                                "since": now, "fix_age_min": 0, "lat": 47.1, "lon": 8.6})
    db.put_state(dev, "battery", {"band": "mid", "level": 55, "charging": 1, "saver": False, "charger": "usb"})
    db.put_state(dev, "bt", ["Galaxy Buds3 Pro", "Car Kit"])
    db.put_state(dev, "car", {"projection": False, "bt": True})
    db.put_state(dev, "dnd", "priority")
    db.put_state(dev, "net", {"type": "wifi", "vpn": False, "roaming": False, "country": "CH", "metered": False})
    db.put_state(dev, "hello", {"app": "0.4.9", "model": "SM-X", "perms": {}})          # not allow-listed
    db.put_state(dev, "app", {"cpu_s_d": 3})                                             # not allow-listed
    db.put_state("_gw", "app_latest", {"code": 49, "name": "0.4.9", "sha256": "ab" * 32, "size": 9, "path": "/x",
                                       "notes": ["secret note"]})
    db.put_state("_gw", "widget:home", {"digest": "d", "at": now, "data": {"lights_on": 3, "tv": "off", "away": False,
                                                                          "vacuum": "docked", "cat": None, "ha": True}})
    db.put_state("_gw", "widget:leg", {"digest": "d", "at": now, "data": {"from": "A", "to": "B"}})   # not allow-listed
    db.put_state("_gw", "geofences", {"fences": [{"lat": 1, "lon": 2}]})                            # not allow-listed
    db.x("INSERT INTO events(id, device_id, kind, at, body, received) VALUES(?,?,?,?,?,?)", "e1", dev, "tile", now,
         json.dumps({"kind": "tile", "name": "sleepin:on", "extra": "x"}), now)
    db.x("INSERT INTO events(id, device_id, kind, at, body, received) VALUES(?,?,?,?,?,?)", "e2", dev, "zone", now,
         json.dumps({"kind": "zone", "zone": "home", "lat": 1}), now)
    db.x("INSERT INTO events(id, device_id, kind, at, body, received) VALUES(?,?,?,?,?,?)", "e3", dev, "tile", now - 20 * 86400,
         json.dumps({"name": "old"}), now - 20 * 86400)
    db.x("INSERT INTO push_log(id, at, level, tag, delivered_ms, acked_ms, verb, device_id, nonce, verbs, arg, action_at) "
         "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", "p1", now * 1000, "timed", "rem-1", now * 1000, None, "done", dev, "NONCE",
         "[]", "ARG", now)
    db.x("INSERT INTO cards(id, kind, body, created, expires, answer, answered) VALUES(?,?,?,?,?,?,?)", "c1", "decision",
         json.dumps({"text": "card text"}), now, now + 60, "picked text", now)
    db.x("INSERT INTO chat(id, at, dir, text, reply_to) VALUES(?,?,?,?,?)", "m1", now, "in", "hello there", None)
    db.x("INSERT INTO inbound(id, device_id, t, at) VALUES(?,?,?,?)", "i1", dev, "crash", now)


async def test_export_is_allow_listed(env, phone):
    db = env.gw.db
    _seed(db, phone.device_id)
    st, j = await env.call("GET", "/internal/export")
    assert st == 200 and j["ok"] and j["schema"] == 1
    blob = json.dumps(j)
    for secret in ("Main St", "47.1", "NONCE", "ARG", "card text", "picked text", "hello there", "Buds3", "secret note",
                   "SM-X", "cpu_s_d", "fences", "S25", "sha256", "fix_age", "country"):
        assert secret not in blob, secret
    assert set(j["devices"][0]) == {"id", "status", "last_seen", "app_code"}
    st_rows = {(r["device_id"], r["k"]): json.loads(r["v"]) for r in j["state"]}
    dev = phone.device_id
    assert st_rows[(dev, "place")] == {"zone": "home", "band": "near", "town": "Townsville"}
    assert st_rows[(dev, "battery")] == {"band": "mid", "level": 55, "charging": True}
    assert st_rows[(dev, "bt")] == ["buds", "other"]
    assert st_rows[(dev, "car")] == {"projection": False, "bt": True}
    assert st_rows[(dev, "dnd")] == "priority"
    assert st_rows[("_gw", "app_latest")] == {"name": "0.4.9", "code": 49}
    assert st_rows[("_gw", "widget:home")]["data"] == {"lights_on": 3, "tv": "off", "away": False, "vacuum": "docked"}
    assert (dev, "hello") not in st_rows and ("_gw", "widget:leg") not in st_rows and ("_gw", "geofences") not in st_rows
    db.x("UPDATE state SET at=? WHERE device_id=? AND k='hello'", now_s() + 50, dev)     # newest row: not exported
    st, j = await env.call("GET", "/internal/export")
    seen = [r for r in j["state"] if r["k"] == "_seen"]
    assert len(seen) == 1 and seen[0]["device_id"] == dev and seen[0]["v"] == "null" and abs(seen[0]["at"] - now_s() - 50) <= 2
    assert [(e["id"], json.loads(e["body"])) for e in j["events"]] == [("e1", {"name": "sleepin:on"})]   # tiles, in window
    assert j["push_log"] == [{"id": "p1", "at": j["push_log"][0]["at"], "level": "timed", "tag": "rem-1",
                              "delivered_ms": j["push_log"][0]["delivered_ms"], "acked_ms": None, "verb": "done",
                              "action_at": j["push_log"][0]["action_at"]}]
    assert j["cards"][0].keys() == {"id", "kind", "created", "answered"}
    assert j["chat"] == [{"id": "m1", "at": j["chat"][0]["at"], "dir": "in"}]
    assert [r["id"] for r in j["inbound"] if r["t"] == "crash"] == ["i1"]


async def test_export_since_clamps_and_needs_the_token(env, phone):
    now = now_s()
    _seed(env.gw.db, phone.device_id)
    st, j = await env.call("GET", "/internal/export?since=%d" % ((now - 30 * 86400) * 1000))
    assert st == 200 and abs(j["since"] / 1000 - (now - X.DAYS_MAX * 86400)) <= 2
    assert [e["id"] for e in j["events"]] == ["e1"]            # 20 days back is past the 14-day cap
    st, j = await env.call("GET", "/internal/export?since=%d" % ((now + 3600) * 1000))
    assert st == 200 and abs(j["since"] / 1000 - now) <= 2         # a future since is clamped to now
    st, j = await env.call("GET", "/internal/export?since=junk")
    assert st == 200 and abs(j["since"] / 1000 - (now - X.DAYS_DEFAULT * 86400)) <= 2
    st, _ = await env.call("GET", "/internal/export", token="wrong")
    assert st == 401


def test_clean_value_drops_wrong_shapes():
    assert X.clean_value("place", "home", X.DEVICE_KEYS["place"]) is None
    assert X.clean_value("bt", {"a": 1}, "kinds") is None
    assert X.clean_value("dnd", 3, "str") is None
    assert X.clean_value("widget:home", {"at": 5, "data": "x"}, "widget") == {"at": 5, "data": {}}
    assert X.clean_value("place", {"zone": "x" * 99}, X.DEVICE_KEYS["place"]) == {"zone": "x" * X.TEXT_MAX}
