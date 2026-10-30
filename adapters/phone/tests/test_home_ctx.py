"""Home autopilot 2 glue: the home_ctx effect (flags) and its tool mapping (bridge). Pure, no gateway env."""
import os

from phonegw import bridge, flags


class _Ctx:
    def now(self):
        return 1_800_000_000

    def __getattr__(self, name):          # anything else the zone/buds paths may ask: harmless defaults
        return lambda *a, **k: None


def _names(effects):
    return [(e.name, e.args) for e in effects if e.kind == "tool" and e.name == "home_ctx"]


def test_change_detection_only_on_real_flips():
    f = flags.home_ctx_change
    assert f({"screen": {"on": False}}, {"screen": {"on": True}}) == "screen_on"
    assert f({"screen": {"on": True}}, {"screen": {"on": True}}) is None
    assert f({"dnd": "on"}, {"dnd": "off"}) == "dnd"
    assert f({"battery": {"level": 50, "charging": False}}, {"battery": {"level": 51, "charging": False}}) is None
    assert f({"battery": {"level": 50, "charging": True}}, {"battery": {"level": 50, "charging": False}}) == "battery_charging"
    assert f({"place": {"zone": "home", "band": "near"}}, {"place": {"zone": None, "band": "near"}}) == "place"
    assert f({"place": {"zone": None, "band": "region"}}, {"place": {"zone": None, "band": "near"}}) == "place"
    assert f({"activity": {"kind": "still"}}, {}) is None
    assert f({"screen": {"on": False}}, None) == "screen_on"


def test_engine_emits_effect_on_state_and_home_fence():
    eng = flags.FlagEngine()
    out = eng.on_state(_Ctx(), {"screen": {"on": False}}, {"screen": {"on": True}})
    assert _names(out) == [("home_ctx", ("screen_on",))]
    assert _names(eng.on_state(_Ctx(), {"battery": {"level": 3}}, {"battery": {"level": 4}})) == []
    assert "home_ctx" in flags.TOOLS


def test_tool_mapping_is_a_dry_feed_with_a_safe_reason():
    argv = bridge.EFFECT_TOOLS["home_ctx"](["zone_enter"])
    assert os.path.basename(argv[0]) == "home-ctx" and argv[1:] == ["feed", "--apply", "--reason", "zone_enter"]
    assert bridge.EFFECT_TOOLS["home_ctx"](["x; rm -rf"])[-1] == "phone"
    assert bridge.EFFECT_TOOLS["home_ctx"]([])[-1] == "phone"
