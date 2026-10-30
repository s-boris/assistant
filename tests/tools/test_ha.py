"""HA tools against a stub `ha`: argv, read-back parsing (ok / already / unconfirmed / dry run), typed refusals,
and that no tool can pass --owner-ok or reach a subcommand outside the allowlist."""

from __future__ import annotations

import pytest
from tools import gateway
from tools.core import ToolError, load
from tools.domains import ha as H

LAMP = {"entity_id": "light.living_room", "name": "Ceiling", "state": "on", "area": "Living Room", "attributes": {}}


def call(name, **args):
    return gateway.call(name, args)


def test_reads(stubs):
    stubs.on("ha", ["state", "light.living_room"], dict(LAMP, attributes={"brightness": 128, "icon": "x"}, hidden=None))
    stubs.on("ha", ["state", "lamp"], [LAMP, dict(LAMP, entity_id="light.desk")])
    stubs.on("ha", ["list"], [LAMP])
    stubs.on("ha", ["find"], [LAMP])
    stubs.on("ha", ["find", "zzz"], "", rc=2, err="")
    stubs.on(
        "ha",
        ["areas"],
        "Living Room (living_room): 4 light, 2 sensor\nBedroom (bedroom): no entities\n(no area): 7 entities\n",
    )
    hist = {
        "entity_id": "vacuum.robot",
        "name": "Robot",
        "area": None,
        "unit": None,
        "since": "s",
        "until": "u",
        "changes": [{"time": "t", "state": "docked", "seconds": 60}],
        "earlier_not_shown": 0,
        "attrs_not_seen": [],
    }
    stubs.on("ha", ["history"], hist)
    stubs.on(
        "ha",
        ["logbook"],
        {"entity_id": None, "since": "s", "until": "u", "entries": [], "earlier_not_shown": 0, "hidden_entries": 2},
    )
    stubs.on("ha", ["trace"], {"entity_id": "automation.arrival", "name": "Arrival", "stored_runs": 5, "runs": [{}]})

    s = call("ha.state", entity="light.living_room")
    assert s.ok and s.data["count"] == 1 and s.data["entities"][0]["attributes"]["brightness"] == 128
    assert "brightness" in s.text and "icon" not in s.text
    m = call("ha.state", entity="lamp")
    assert m.data["count"] == 2 and m.text.startswith("2 entities match")
    ls = call("ha.list", domain="light", area="Living Room", limit=5)
    assert ls.data["count"] == 1 and "attributes" not in ls.data["entities"][0]  # concise
    assert "attributes" in call("ha.list", domain="light", format="detailed").data["entities"][0]
    assert call("ha.find", words="ceiling", domain="light").data["count"] == 1
    assert call("ha.find", words="zzz").data == {"count": 0, "entities": []}
    ar = call("ha.areas")
    assert ar.data["areas"][0] == {"name": "Living Room", "id": "living_room", "counts": {"light": 4, "sensor": 2}}
    assert ar.data["unassigned"] == 7 and ar.data["areas"][1]["counts"] == {}
    h = call("ha.history", entity="vacuum.robot", since="7d", attrs=["status"])
    assert h.ok and h.data["changes"][0]["state"] == "docked"
    assert call("ha.logbook", since="2h").data["hidden_entries"] == 2
    assert call("ha.trace", item="automation.arrival", last=2).data["stored_runs"] == 5
    calls = stubs.calls("ha")
    assert ["list", "light", "--area", "Living Room", "--limit", "5", "--json"] in calls
    assert ["history", "vacuum.robot", "--since", "7d", "--limit", "50", "--json", "--attrs", "status"] in calls
    assert ["logbook", "--since", "2h", "--limit", "40", "--json"] in calls
    assert ["trace", "automation.arrival", "--last", "2", "--json"] in calls


def test_writes_and_read_back(stubs):
    stubs.on("ha", ["on", "light.living_room"], "light.living_room: off -> on 100% ok\n")
    stubs.on("ha", ["off", "light.living_room"], "light.living_room: off -> off ok (already)\n")
    stubs.on("ha", ["toggle"], "light.a: on -> on (not confirmed after 8s)\nlight.b: on -> off ok\n", rc=3)
    stubs.on("ha", ["off", "--area"], "light.a: on -> off ok\n", err="note: 2 lights in Living Room\n")
    stubs.on("ha", ["set"], 'dry run: would call light.turn_on {"brightness_pct": 30} on:\n  light.a: on 80%\n')
    stubs.on("ha", ["media"], "media_player.tv: playing -> paused ok\n")
    stubs.on("ha", ["scene"], "scene.movie: 2026-10-21 -> 2026-10-22\n")
    stubs.on("ha", ["call"], "vacuum.robot: cleaning -> returning ok\n")

    on = call("ha.switch", action="on", targets=["light.living_room"])
    assert on.ok and on.data["confirmed"] is True
    assert on.data["changes"] == [{"entity_id": "light.living_room", "old": "off", "new": "on 100%", "status": "ok"}]
    already = call("ha.switch", action="off", targets=["light.living_room"])
    assert already.data["changes"][0]["status"] == "already"
    t = call("ha.switch", action="toggle", targets=["light.a", "light.b"], wait=8)
    assert t.ok and t.data["confirmed"] is False and "NOT confirmed" in t.text
    assert [c["status"] for c in t.data["changes"]] == ["unconfirmed", "ok"] and t.data["changes"][0]["new"] == "on"
    area = call("ha.switch", action="off", area="Living Room", domain="light")
    assert area.data["notes"] == ["2 lights in Living Room"]
    dry = call("ha.set", target="light.a", brightness=30, dry_run=True)
    assert dry.data["dry_run"] is True and dry.data["changes"] == [
        {"entity_id": "light.a", "old": "on 80%", "status": "dry_run"}
    ]
    assert call("ha.media", player="media_player.tv", action="pause").data["confirmed"] is True
    sc = call("ha.run", kind="scene", target="Movie")
    assert sc.data["changes"][0]["status"] == "sent"
    v = call("ha.call", service="vacuum.return_to_base", entity="vacuum.robot", data={"x": 1})
    assert v.ok
    calls = stubs.calls("ha")
    assert ["on", "light.living_room", "--wait", "8"] in calls
    assert ["toggle", "light.a", "light.b", "--wait", "8"] in calls
    assert ["off", "--area", "Living Room", "--domain", "light", "--wait", "8"] in calls
    assert ["set", "light.a", "--brightness", "30", "--dry-run", "--wait", "8"] in calls
    assert ["media", "media_player.tv", "pause", "--wait", "8"] in calls
    assert ["scene", "run", "Movie", "--wait", "8"] in calls
    assert ["call", "vacuum.return_to_base", "vacuum.robot", '{"x": 1}', "--wait", "8"] in calls
    call("ha.media", player="media_player.tv", action="volume", volume=20, wait=2.5)
    assert stubs.calls("ha")[-1] == ["media", "media_player.tv", "volume", "20", "--wait", "2.5"]


@pytest.mark.parametrize(
    "rc,err,code",
    [
        (2, 'ha: refused: cover.garage needs --owner-ok "his words"', "approval_required"),
        (2, "ha: refused: 'lamp' is ambiguous: light.a, light.b", "invalid_args"),
        (2, "ha: refused: lock.door: lock is never controllable", "invalid_args"),
        (1, "ha: error: cannot connect to HA", "unavailable"),
        (1, "ha: error: HTTP 400 bad data", "failed"),
    ],
)
def test_write_errors(stubs, rc, err, code):
    stubs.on("ha", ["call"], "", rc=rc, err=err)
    r = call("ha.call", service="cover.open_cover", entity="cover.garage")
    assert r.code == code, r.error


def test_refused_inputs(stubs):
    bad = [
        ("ha.switch", {"action": "on"}),
        ("ha.switch", {"action": "on", "targets": ["a"], "area": "b"}),
        ("ha.switch", {"action": "on", "targets": ["ceiling lamp", "desk lamp"]}),  # several: ids only
        ("ha.switch", {"action": "on", "targets": ["light.a"], "domain": "light"}),
        ("ha.switch", {"action": "on", "targets": ["--owner-ok"]}),
        ("ha.switch", {"action": "on", "targets": ["light.a"], "owner_ok": "yes"}),
        ("ha.set", {"target": "light.a"}),
        ("ha.set", {"target": "light.a", "brightness": 130}),
        ("ha.media", {"player": "tv", "action": "volume"}),
        ("ha.media", {"player": "tv", "action": "play", "volume": 3}),
        ("ha.call", {"service": "homeassistant.restart; x", "entity": "a"}),
        ("ha.call", {"service": "light.turn_on", "entity": "-n"}),
        ("ha.history", {"entity": "x", "since": "--until"}),
        ("ha.state", {"entity": "-x"}),
        ("ha.run", {"kind": "automation", "target": "x"}),
    ]
    for name, args in bad:
        r = gateway.call(name, args)
        assert r.code == "invalid_args", (name, args, r.error)
    assert stubs.calls() == []


def test_guard_and_registry():
    for argv in (["on", "light.a", "--wait", "8"], ["state", "x", "--json"], ["call", "a.b", "c", "{}", "--dry-run"]):
        assert H.guard(argv) == argv
    for argv in (
        ["on", "cover.garage", "--owner-ok", "yes"],
        ["todo", "add", "milk"],
        ["device-url", "x"],
        ["state", "x", "--url", "http://x"],
        [],
    ):
        with pytest.raises(ToolError):
            H.guard(argv)
    reg = load()
    specs = reg.in_domain("ha")
    assert {s.name for s in specs if s.effect == "home"} == {"ha.switch", "ha.set", "ha.media", "ha.run", "ha.call"}
    assert all(
        s.effect == "read" for s in specs if s.name not in {"ha.switch", "ha.set", "ha.media", "ha.run", "ha.call"}
    )
    for s in specs:
        assert "owner_ok" not in s.input.model_json_schema()["properties"], s.name


def test_mcp_round_trip_for_domain_tools(stubs):
    """Structured results of the new domains pass FastMCP's output-schema check (dicts, Any fields, lists)."""
    import asyncio

    from fastmcp import Client
    from tools import mcp_server

    stubs.on("ha", ["state"], LAMP).on("ha", ["on"], "light.living_room: off -> on ok\n")
    stubs.on("vault", ["ha", "now"], {"place": {"zone": "Home"}, "wifi": None, "bluetooth": ["audio"], "age_min": 1})
    stubs.on("vault", ["--async"], "0123456789abcdef")
    stubs.on("vault", ["job", "wait"], {"id": "0123456789abcdef", "state": "done", "result": {"answer": [1, "x"]}})
    stubs.on("calcache", ["busy"], [{"date": "2026-10-24", "busy": [], "covered": True}])

    async def go():
        async with Client(mcp_server.build()) as c:
            return [
                await c.call_tool("ha_state", {"entity": "light.living_room"}),
                await c.call_tool("ha_switch", {"action": "on", "targets": ["light.living_room"]}),
                await c.call_tool("vault_ha_now", {"format": "detailed"}),
                await c.call_tool("vault_ask", {"source": "tg", "chat": "Alex", "question": "q"}),
                await c.call_tool("calendar_busy", {"date": "today"}),
                await c.call_tool("ha_switch", {"action": "on"}, raise_on_error=False),
            ]

    st, sw, now, ask, busy, bad = asyncio.run(go())
    assert st.structured_content["entities"][0]["entity_id"] == "light.living_room"
    assert sw.structured_content["confirmed"] is True
    assert now.structured_content["bluetooth"] == ["audio"]
    assert ask.structured_content["result"] == {"answer": [1, "x"]}
    assert busy.structured_content["days"][0]["covered"] is True
    assert bad.is_error and bad.content[0].text.startswith("invalid_args:")
