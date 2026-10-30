"""Phone tools against a stub `phone`: status and push log, and the four ways to reach him (push, link, open, chat):
free text as --opt=value or after `--` (a leading '-' is text, never an option), the gateway's exit codes as typed
results (offline = queued with a note, lint = invalid_args, down = unavailable), and input validation."""

from __future__ import annotations

import json

import pytest
from tools import gateway
from tools.core import load

STATUS = {
    "ok": True,
    "uptime_s": 3600,
    "held": {"n": 2, "next": None},
    "devices": [
        {
            "id": "d_1",
            "name": "Phone",
            "model": "SM-X",
            "status": "active",
            "online": True,
            "route": "lan",
            "app": "0.5.0",
            "battery_band": "ok",
            "perms": {"overlay": True},
        }
    ],
}


def call(name, **args):
    return gateway.call(name, args)


def test_status_and_pushes(stubs):
    stubs.on("phone", ["status", "--json"], STATUS)
    stubs.on(
        "phone",
        ["pushes"],
        {"pushes": [{"id": "p1", "level": "info", "tag": "t", "delivered_ms": 5, "acked_ms": 900, "verb": "done"}]},
    )
    s = call("phone.status")
    assert s.ok and s.data["held"] == 2 and "raw" not in s.data and "perms" not in s.data["devices"][0]
    assert s.text == "gateway up · 1 device(s) · 2 held\n  Phone (SM-X): online via lan · app 0.5.0 · battery ok"
    assert call("phone.status", format="detailed").data["raw"]["devices"][0]["perms"] == {"overlay": True}
    assert "tapped done" in call("phone.pushes", tag="t", limit=5).text
    assert ["pushes", "--json", "--limit", "5", "--tag", "t"] in stubs.calls("phone")


def test_push(stubs):
    stubs.on("phone", ["push"], json.dumps({"id": "p2", "status": "held", "tag": "x", "hold_until": 1}) + "\n")
    p = call("phone.push", level="info", title="-dash title", body="--body text", tag="x", ttl="6h", private=True)
    assert p.ok and p.data["delivered"] is False and p.data["status"] == "held" and "quiet hours" in p.text
    assert stubs.calls("phone")[-1] == [
        "push",
        "--json",
        "--level=info",
        "--title=-dash title",
        "--body=--body text",
        "--tag=x",
        "--ttl=6h",
        "--private",
    ]


@pytest.mark.parametrize(
    ("rc", "out", "code", "ok"),
    [
        (4, {"id": "p3", "status": "queued"}, None, True),  # offline: queued until ttl, still an answer
        (3, {"ok": False, "exit": 3, "error": "lint"}, "invalid_args", False),
        (7, {"ok": False, "exit": 7, "error": "gateway down"}, "unavailable", False),
        (6, {"ok": False, "exit": 6, "error": "refused"}, "invalid_args", False),
    ],
)
def test_push_exits(stubs, rc, out, code, ok):
    stubs.on("phone", ["push"], json.dumps(out) + "\n", rc=rc, err="phone: " + str(out.get("error", "")) + "\n")
    r = call("phone.push", level="urgent", title="Train", body="leaves in 10 min")
    assert r.ok is ok
    if ok:
        assert r.data["status"] == "queued" and "offline" in r.data["note"]
    else:
        assert r.error["code"] == code


def test_link_open_chat(stubs):
    stubs.on(
        "phone",
        ["link"],
        {
            "ok": True,
            "route": "telegram",
            "url": "https://maps.example/",
            "tag": "link-1",
            "app": "queued",
            "why": "phone offline",
        },
    )
    stubs.on("phone", ["open"], {"ok": True, "route": "opened", "kind": "nav", "app": "opened", "how": "overlay"})
    stubs.on("phone", ["chat"], {"msg_id": 41, "status": "queued"}, rc=4)
    li = call("phone.link", maps="Luzern", nav="transit")
    assert li.ok and li.data["route"] == "telegram" and li.data["note"] == "via Telegram (phone offline)"
    assert call("phone.link", url="https://www.example.org/x?y=1", title="T").ok
    o = call("phone.open", nav="home", mode="r")
    assert o.ok and o.data["delivered"] is True and o.data["route"] == "opened"
    assert call("phone.open", sbb_from="here", sbb_to="Luzern", arrive="18:30").ok
    assert call("phone.open", app="SBB Mobile", tap=True).ok
    assert call("phone.open", url="https://www.example.org/").ok
    c = call(
        "phone.chat",
        text="- done, see list",
        reply_to="m_1",
        buttons=[[{"text": "Yes", "data": "y:1"}, {"text": "No", "data": "n:1"}]],
    )
    assert c.ok and c.data["status"] == "queued" and c.data["note"].startswith("phone offline: send it on Telegram")
    calls = stubs.calls("phone")
    assert ["link", "--json", "--level=timed", "--body=", "--maps=Luzern", "--nav=transit"] in calls
    assert ["link", "--json", "--level=timed", "--body=", "--title=T", "--", "https://www.example.org/x?y=1"] in calls
    assert ["open", "--json", "--via", "session", "--nav=home", "--mode=r"] in calls
    assert ["open", "--json", "--via", "session", "--sbb", "here", "Luzern", "--arrive=18:30"] in calls
    assert ["open", "--json", "--via", "session", "--tap", "--app=SBB Mobile"] in calls
    assert ["open", "--json", "--via", "session", "--", "https://www.example.org/"] in calls
    chat = [c for c in calls if c[0] == "chat"][0]
    assert chat[:3] == ["chat", "--json", "--reply-to=m_1"] and chat[-2:] == ["--", "- done, see list"]
    assert json.loads(chat[3].removeprefix("--buttons=")) == [
        [{"text": "Yes", "data": "y:1"}, {"text": "No", "data": "n:1"}]
    ]


def test_refused_inputs(stubs):
    for name, args in (
        ("phone.link", {}),
        ("phone.link", {"url": "https://a.example/", "maps": "Bern"}),
        ("phone.link", {"url": "http://a.example/"}),
        ("phone.link", {"url": "https://user@a.example/"}),
        ("phone.link", {"url": "https://a.example/", "nav": "driving"}),
        ("phone.open", {}),
        ("phone.open", {"nav": "home", "app": "Maps"}),
        ("phone.open", {"sbb_from": "here"}),
        ("phone.open", {"sbb_from": "-x", "sbb_to": "Bern"}),
        ("phone.open", {"nav": "home", "at": "18:00"}),
        ("phone.open", {"url": "geo:0,0"}),
        ("phone.open", {"app": "x; rm -rf"}),
        ("phone.push", {"level": "loud", "title": "x"}),
        ("phone.push", {"level": "info", "title": "x", "ttl": "forever"}),
        ("phone.chat", {"text": "x", "buttons": [[{"text": "a", "data": "bad data!"}]]}),
    ):
        assert call(name, **args).error["code"] == "invalid_args", (name, args)
    assert stubs.calls("phone") == []
    reg = load()
    effects = {s.name: s.effect for s in reg.in_domain("phone")}
    assert effects == {
        "phone.status": "read",
        "phone.pushes": "read",
        "phone.push": "send_owner",
        "phone.link": "send_owner",
        "phone.open": "send_owner",
        "phone.chat": "send_owner",
        "phone.lookup": "read",  # the gateway's data lookups for principal phone
        "phone.act": "home",  # the gateway's allow-listed writes for principal phone
    }
