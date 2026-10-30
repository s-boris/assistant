"""`media play` against localhost fakes only (fake_emby.py, fake_ha.py through an `ha` wrapper, a logging ha stub).
Part 1, dry runs: Emby GETs only, no POST, no ha call; env overrides, refusals, JSON, the tonight button gate.
Part 2, the real path: wake via the wake entity or the TV's own turn_on, select_source, session pick, resume ticks,
read-back, exit 3 when playback never starts. Part 3, the target by bitrate (devices.json): dry-run choices,
--target, a fast LAN device, the hint."""

from __future__ import annotations

import json
import re
import subprocess

import pytest
from mediatest import HERE, PYTHON3

from core import locks

STEP = re.compile(r"^ ?[0-9]+\. ", re.M)
HA = {
    "_tv": "media_player.test_tv",
    "_wake": "media_player.test_box",
    "media_player.test_tv": {"state": "unavailable", "attributes": {"supported_features": 24377}},
    "media_player.test_box": {"state": "off", "attributes": {"supported_features": 153529}},
}
I558 = "GET /emby/Items?Ids=558&Recursive=true&Fields=RunTimeTicks,MediaSources token=ok"
OVER = "85 Mbit is above the slow TV's 80 Mbit limit"
HINT = "4K remux 85 Mbit: may stutter on the slow TV — the fast box handles it"

# tn_buttons on three picks (A with a link, B without, C a request), in the staged copy
TB = """import importlib.machinery as m, importlib.util as u, json, sys
s = u.spec_from_loader("media", m.SourceFileLoader("media", sys.argv[1])); mod = u.module_from_spec(s); s.loader.exec_module(mod)
picks = [{"slot": "A", "title": "Test Movie", "link": "http://x/web/index.html#!/item?id=555", "emby_id": "555", "in_library": True},
         {"slot": "B", "title": "Show", "episode": "S01E02", "link": None, "emby_id": "556", "in_library": True},
         {"slot": "C", "title": "New", "in_library": False, "request": {"data": "mt:req:9"}}]
print(json.dumps(mod.tn_buttons(picks)))"""  # noqa: E501


class Play:
    def __init__(self, w):
        self.w, t = w, w.t
        self.t = t
        (t / "scenario").write_text("ok\n")
        (t / "ha.json").write_text(json.dumps(HA))
        self.b = w.serve("fake_emby.py", t / "ha.json", t / "scenario", log=t / "emby.log", err=t / "emby.err")
        b = self.b
        (t / "cfg.json").write_text(
            json.dumps({"seerr": {"url": b, "key": "SEERR-KEY-Zq81"}, "emby": {"url": b, "key": "EMBY-KEY-Pt55"}})
        )
        (t / "nokey.json").write_text(json.dumps({"seerr": {"url": b, "key": "SEERR-KEY-Zq81"}}))
        (t / "ha-stub").write_text(f'#!/bin/sh\necho "$*" >> "{t}/stub.log"\nexit 1\n')
        (t / "ha-fake").write_text(f'#!/bin/sh\nexec {PYTHON3} -I {HERE / "fake_ha.py"} "$@"\n')
        for n in ("ha-stub", "ha-fake"):
            (t / n).chmod(0o755)
        w.env.update(
            MEDIA_SECRETS=str(t / "cfg.json"),
            MEDIA_HA_BIN=str(t / "ha-stub"),
            MEDIA_PLAY_CONFIG=str(t / "absent.json"),
            MEDIA_PLAY_LOCK=str(t / "play.lock"),
            MEDIA_TIMEOUT="5",
            FAKE_HA_STATE=str(t / "ha.json"),
            FAKE_HA_LOG=str(t / "ha.log"),
            MEDIA_PLAY_DEVICES=str(t / "nodevices.json"),
        )
        for name, enabled, extra in (
            ("off", "false", ""),
            ("on", "true", ""),
            ("on-noresume", "true", ', "resume": false'),
        ):
            (t / f"{name}.json").write_text(
                f'{{"enabled": {enabled}, "tv": "media_player.test_tv", "wake": "media_player.test_box", '
                f'"client": "Emby for LG", "device": "TESTTV"{extra}}}\n'
            )
        (t / "strenabled.json").write_text('{"enabled": "yes", "tv": "media_player.test_tv", "device": "TESTTV"}\n')
        (t / "broken.json").write_text("not json")

    def read(self, n):
        p = self.t / n
        return p.read_text() if p.exists() else ""

    def run(self, *args, **env):
        out = self.w.m("play", *args, env=env)
        return out, self.w.rc

    def nosend(self):
        log = self.read("emby.log")
        assert not re.search(r"^POST", log, re.M) and "token=bad" not in log
        assert self.read("stub.log") == "" and self.read("ha.log") == ""

    def untouched(self):
        assert self.read("emby.log") == "" and self.read("stub.log") == "" and self.read("ha.log") == ""

    def ha_lines(self):
        return self.read("ha.log").splitlines()

    def emby_lines(self):
        return self.read("emby.log").splitlines()

    def posts(self):
        return [x for x in self.emby_lines() if x.startswith("POST")]

    def gets(self):
        return sum(x.startswith("GET") for x in self.emby_lines())

    def reset(self, scen="ok"):
        (self.t / "ha.json").write_text(json.dumps(HA))
        for n in ("ha.log", "emby.log", "scenario.fast"):
            (self.t / n).write_text("")
        (self.t / "scenario").write_text(scen + "\n")

    def fast(self, how):
        (self.t / "scenario.fast").write_text(how + "\n")

    def ha_edit(self, fn):
        p = self.t / "ha.json"
        d = json.loads(p.read_text())
        fn(d)
        p.write_text(json.dumps(d))

    def buttons(self, **env):
        e = dict(self.w.env, **env)
        r = subprocess.run([PYTHON3, "-c", TB, self.w.tool], capture_output=True, text=True, env=e, timeout=30)
        return r.stdout + r.stderr


@pytest.fixture
def play(media):
    return Play(media)


def noleak(out):
    assert not re.search(r"EMBY-KEY|SEERR-KEY", out)


def real_env(p):
    p.w.env.update(
        MEDIA_HA_BIN=str(p.t / "ha-fake"),
        MEDIA_PLAY_CONFIG=str(p.t / "on.json"),
        MEDIA_PLAY_POLL="0.1",
        MEDIA_PLAY_WAIT="3",
        MEDIA_PLAY_CONFIRM="2",
    )


def test_dry_run(play):
    p = play
    out, rc = p.run("555", "--dry-run")
    assert rc == 0
    assert "real mode refuses" in out and "is absent" in out
    assert "no tv set" in out and "no device set" in out
    p.nosend()
    noleak(out)

    out, rc = p.run(
        "555",
        "--dry-run",
        MEDIA_PLAY_TV="media_player.env_tv",
        MEDIA_PLAY_WAKE="remote.env_box",
        MEDIA_PLAY_DEVICE="ENVTV",
        MEDIA_PLAY_CLIENT="Emby for LG",
    )
    assert rc == 0
    assert "ha state media_player.env_tv --json" in out
    assert "ha call remote.turn_on remote.env_box --wait 3" in out
    assert """ha call media_player.select_source media_player.env_tv '{"source": "Emby"}' --wait 3""" in out
    assert "Client ~ 'Emby for LG' and DeviceName ~ 'ENVTV'" in out
    assert "real mode refuses" in out  # env can't enable
    assert f"GET  {p.b}/emby/Items?Ids=555&Recursive=true&Fields=RunTimeTicks" in out
    assert (
        f"POST {p.b}/emby/Sessions/{{session.Id}}/Playing?ItemIds=555&PlayCommand=PlayNow"
        "[&StartPositionTicks={resume}]" in out
    )
    assert f"GET  {p.b}/emby/Sessions?Id={{session.Id}}" in out
    p.nosend()

    on = str(p.t / "on.json")
    out, rc = p.run("555", "--dry-run", MEDIA_PLAY_CONFIG=on)
    assert "enabled, real mode runs" in out
    assert len(STEP.findall(out)) == 11
    p.nosend()
    out, _ = p.run("555", "--dry-run", MEDIA_PLAY_CONFIG=p.t / "off.json")
    assert 'is present; needs "enabled": true' in out
    out, _ = p.run("555", "--dry-run", MEDIA_PLAY_CONFIG=p.t / "strenabled.json")
    assert "enabled must be a bool" in out
    out, _ = p.run("555", "--dry-run", MEDIA_PLAY_CONFIG=p.t / "broken.json")
    assert "is unreadable" in out and "JSONDecodeError" in out

    out, _ = p.run("555", "--dry-run", "--from-start", MEDIA_PLAY_CONFIG=on)
    assert "StartPositionTicks" not in out
    out, _ = p.run("555", "--dry-run", MEDIA_PLAY_CONFIG=p.t / "on-noresume.json")
    assert "StartPositionTicks" not in out
    out, _ = p.run("555", "--dry-run", "--no-wake", MEDIA_PLAY_CONFIG=on)
    assert "turn_on" not in out and len(STEP.findall(out)) == 8
    out, _ = p.run("555", "--dry-run", MEDIA_PLAY_CONFIG=on, MEDIA_PLAY_WAKE="")
    assert "media_player.turn_on media_player.test_box" in out  # empty env leaves the config value

    out, rc = p.run("555", "--dry-run", "--json", MEDIA_PLAY_CONFIG=on)
    d = json.loads(out)
    assert d["dry_run"] and d["enabled"] and d["emby_key"] and d["item"] == "555"
    assert len(d["steps"]) == 11 and d["probe"] is None and d["tv"] == "media_player.test_tv"
    noleak(out)
    p.nosend()

    out, _ = p.run("555", "--dry-run", MEDIA_PLAY_TV="light.kitchen")
    assert "tv is not valid" in out
    out, _ = p.run("555", "--dry-run", MEDIA_PLAY_CONFIG=on, MEDIA_PLAY_WAKE="media_player.x;reboot")
    assert "wake is not valid" in out and "real mode refuses" in out
    p.nosend()
    (p.t / "emby.log").write_text("")
    out, rc = p.run("555", MEDIA_PLAY_CONFIG=on, MEDIA_PLAY_WAKE="light.x")
    assert rc == 2 and "wake is not valid" in out
    for item in ("12;id", "abc", "", "1234567890123"):
        out, rc = p.run(item, "--dry-run")
        assert rc == 2 and "ITEM_ID must be" in out, item
    p.untouched()

    # real-mode refusals: nothing sent
    out, rc = p.run("555")
    assert rc == 2 and "play is staged" in out
    out, rc = p.run("555", MEDIA_PLAY_CONFIG=p.t / "off.json")
    assert rc == 2 and "now: present" in out
    out, rc = p.run("555", MEDIA_PLAY_CONFIG=on, MEDIA_SECRETS=p.t / "nokey.json")
    assert rc == 2 and "no emby url/key" in out
    out, rc = p.run("555", "--probe", MEDIA_PLAY_CONFIG=on)
    assert rc == 2
    p.untouched()


def test_tonight_button_gate(play):
    p = play
    r = json.loads(p.buttons(MEDIA_PLAY_CONFIG=str(p.t / "on.json")))
    assert r[0] == [
        {"text": "▶ Test Movie", "url": "http://x/web/index.html#!/item?id=555"},
        {"text": "📺 Play on TV", "data": "mt:play:555"},
    ]
    assert r[1] == [{"text": "📺 Play on TV", "data": "mt:play:556"}]  # own row without a link
    assert json.dumps(r).count("mt:play:") == 2  # none for the request pick
    out = p.buttons(MEDIA_PLAY_CONFIG=str(p.t / "off.json"))
    assert "mt:play:" not in out and "mt:more" in out
    assert "mt:play:" not in p.buttons()


def test_real_path(play):
    p = play
    real_env(p)
    p.reset()
    out, rc = p.run("555")
    assert rc == 0
    assert "▶ Playing Test Movie on the TV (from 60 min)" in out
    ha = p.ha_lines()
    assert "call media_player.turn_on media_player.test_box --wait 3" in ha  # the TV has no turn_on
    assert not any("turn_on media_player.test_tv" in x for x in ha)
    sel = 'call media_player.select_source media_player.test_tv {"source": "Emby"} --wait 3'
    assert sel in ha
    assert min(i for i, x in enumerate(ha) if "turn_on" in x) < min(i for i, x in enumerate(ha) if "select_source" in x)
    assert (
        "POST /emby/Sessions/s1/Playing?ItemIds=555&PlayCommand=PlayNow&StartPositionTicks=36000000000 token=ok"
        in p.emby_lines()
    )
    assert len(p.posts()) == 1
    assert "token=bad" not in p.read("emby.log")
    assert p.emby_lines()[0].startswith("GET /emby/Items?Ids=555")  # item checked before any HA call
    noleak(out)

    p.reset()
    out, rc = p.run("555", "--from-start", "--json")
    d = json.loads(out)
    assert rc == 0 and d["ok"] and d["sent"] and d["resume_min"] == 0 and d["title"] == "Test Movie"
    assert "POST /emby/Sessions/s1/Playing?ItemIds=555&PlayCommand=PlayNow token=ok" in p.emby_lines()

    p.reset()
    p.ha_edit(
        lambda d: d.update(
            {"media_player.test_tv": {"state": "on", "attributes": {"supported_features": 24377, "source": "Emby"}}}
        )
    )
    out, rc = p.run("556")
    assert rc == 0 and not any(x.startswith("call") for x in p.ha_lines())  # TV already in Emby: no HA writes
    assert "POST /emby/Sessions/s1/Playing?ItemIds=556&PlayCommand=PlayNow token=ok" in p.emby_lines()

    p.reset()
    p.ha_edit(lambda d: d["media_player.test_tv"]["attributes"].update(supported_features=24377 | 128))
    out, rc = p.run("555")
    assert rc == 0 and "call media_player.turn_on media_player.test_tv --wait 3" in p.ha_lines()
    assert "test_box" not in p.read("ha.log")

    p.reset()
    out, rc = p.run("555", "--no-wake")
    assert rc == 1 and "--no-wake" in out
    assert not any(x.startswith("call") for x in p.ha_lines()) and not p.posts()

    p.reset()
    out, rc = p.run("777")
    assert rc == 2 and "is a Series" in out and p.read("ha.log") == ""
    p.reset()
    out, rc = p.run("999")
    assert rc == 2 and "no Emby item 999" in out and p.read("ha.log") == ""

    p.reset("noplay")
    out, rc = p.run("555")
    assert rc == 3 and "isn't playing yet" in out

    p.reset()
    p.ha_edit(lambda d: d.update(_wake_works=False))
    out, rc = p.run("555")
    assert rc == 1 and "waiting for the TV to turn on" in out and not p.posts()

    p.reset()
    out, rc = p.run("555", MEDIA_PLAY_DEVICE="OTHERTV")
    assert rc == 1 and "Emby session" in out and not p.posts()

    p.reset()
    lk = locks.acquire(p.t / "play.lock", wait=0)  # another play holds the lock
    assert lk is not None
    try:
        out, rc = p.run("555")
    finally:
        locks.release(lk)
    assert rc == 2 and "another play" in out and p.read("ha.log") == "" and p.read("emby.log") == ""


def test_target_by_bitrate(play):
    p, t = play, play.t
    dev = {
        "TESTTV": {"lan_mbit": 100, "label": "the slow TV"},
        "FASTBOX": {"lan_mbit": 1000, "client": "Emby for Android", "label": "the fast box"},
    }
    (t / "dev.json").write_text(json.dumps(dev))
    d = json.loads(json.dumps(dev))
    d["FASTBOX"]["source"] = "HDMI2"
    (t / "dev-src.json").write_text(json.dumps(d))
    d = json.loads(json.dumps(dev))
    d["TESTTV"]["prefer_over_mbit"] = 10
    (t / "dev-prefer.json").write_text(json.dumps(d))
    (t / "dev-one.json").write_text(json.dumps({"TESTTV": {"lan_mbit": 100, "label": "the slow TV"}}))
    (t / "dev-bad.json").write_text(
        json.dumps({"TESTTV": {"lan_mbit": "fast"}, "x;y": {}, "FASTBOX": {"lan_mbit": 1000, "source": "HDMI2;reboot"}})
    )
    (t / "dev-broken.json").write_text("not json")
    p.w.env["MEDIA_PLAY_DEVICES"] = str(t / "dev.json")
    real_env(p)  # part 3 runs with part 2's settings (enabled config, the fake ha)

    p.reset()
    out, rc = p.run("558", "--dry-run")
    assert rc == 0 and f"target: TESTTV ({OVER}; no faster device is online)" in out
    assert f"hint: {HINT}" in out
    p.nosend()
    assert p.gets() == 2 and I558 in p.emby_lines() and "GET /emby/Sessions token=ok" in p.emby_lines()

    p.reset()
    p.fast("lan")
    out, rc = p.run("558", "--dry-run")
    assert rc == 0 and f"target: FASTBOX ({OVER}; the fast box is online)" in out and "hint:" not in out
    assert "Client ~ 'Emby for Android' and DeviceName ~ 'FASTBOX' from the LAN shows up" in out
    assert "select_source" not in out
    p.nosend()

    p.reset()
    p.fast("wan")
    out, _ = p.run("558", "--dry-run")
    assert f"target: TESTTV ({OVER}; no faster device is online)" in out and f"hint: {HINT}" in out

    p.reset()
    p.fast("lan")
    out, _ = p.run("555", "--dry-run")
    assert "target: TESTTV (20 Mbit fits the slow TV (limit 80))" in out and p.gets() == 1
    out, _ = p.run("555", "--dry-run", MEDIA_PLAY_CONFIG=t / "on.json")
    assert len(STEP.findall(out)) == 12
    out, _ = p.run("555", "--dry-run", MEDIA_PLAY_DEVICES=t / "dev-prefer.json")
    assert "target: FASTBOX (20 Mbit is above the slow TV's 10 Mbit limit; the fast box is online)" in out

    p.reset()
    p.fast("lan")
    out, rc = p.run("555", "--dry-run", "--target", "The Fast Box")
    assert rc == 0 and "target: FASTBOX (--target)" in out and p.gets() == 1
    out, _ = p.run("558", "--dry-run", "--target", "testtv")
    assert "target: TESTTV (--target)" in out and f"hint: {HINT}" in out
    (t / "emby.log").write_text("")
    out, rc = p.run("558", "--dry-run", "--target", "nope")
    assert rc == 2 and "unknown --target 'nope' (known: TESTTV, FASTBOX)" in out and p.read("emby.log") == ""
    out, rc = p.run("558", "--target", "nope")
    assert rc == 2 and p.read("emby.log") == "" and p.read("ha.log") == ""

    p.reset()
    p.fast("lan")
    out, _ = p.run("558", "--dry-run", "--json")
    d = json.loads(out)
    assert d["target"] == "FASTBOX" and d["target_label"] == "the fast box" and d["primary"] is False
    assert d["mbit"] == 85.3 and d["hint"] is None and len(d["emby_reads"]) == 2
    assert d["devices"] == ["TESTTV", "FASTBOX"]
    noleak(out)
    out, _ = p.run("558", "--dry-run", MEDIA_PLAY_DEVICES=t / "dev-src.json", MEDIA_PLAY_CONFIG=t / "on.json")
    assert """ha call media_player.select_source media_player.test_tv '{"source": "HDMI2"}' --wait 3""" in out

    p.reset()
    out, _ = p.run("558", "--dry-run", MEDIA_PLAY_DEVICES=t / "dev-one.json")
    assert "hint: 4K remux 85 Mbit: may stutter on the slow TV" in out and "handles it" not in out and p.gets() == 1
    out, _ = p.run("558", "--dry-run", MEDIA_SECRETS=t / "nokey.json")
    assert "target: TESTTV (bitrate unknown: the default device)" in out and "(no network)" in out
    out, rc = p.run("558", "--dry-run", MEDIA_PLAY_DEVICES=t / "dev-bad.json")
    assert rc == 0 and "TESTTV: lan_mbit must be a positive number" in out and "FASTBOX: source is not valid" in out
    assert "an entry is not valid" in out and "target: TESTTV" in out
    out, rc = p.run("558", "--dry-run", MEDIA_PLAY_DEVICES=t / "dev-broken.json")
    assert rc == 0 and "dev-broken.json: JSONDecodeError" in out and "target: TESTTV" in out
    p.nosend()

    # real path with a fast device (fakes only)
    p.reset()
    p.fast("lan")
    out, rc = p.run("558", MEDIA_PLAY_DEVICES=t / "dev-src.json")
    assert rc == 0 and "▶ Playing Big Remux on the fast box" in out and "may stutter" not in out
    assert "call media_player.turn_on media_player.test_box --wait 3" in p.ha_lines()
    assert 'call media_player.select_source media_player.test_tv {"source": "HDMI2"} --wait 3' in p.ha_lines()
    assert p.posts() == ["POST /emby/Sessions/f1/Playing?ItemIds=558&PlayCommand=PlayNow token=ok"]

    p.reset()
    p.fast("lan")
    out, rc = p.run("558", "--json")
    d = json.loads(out)
    assert rc == 0 and d["ok"] and d["target"] == "FASTBOX" and d["primary"] is False and d["hint"] is None
    assert any("left as is" in x for x in d["log"]) and "select_source" not in p.read("ha.log")
    noleak(out)

    p.reset()
    out, rc = p.run("558")
    assert rc == 0 and "▶ Playing Big Remux on the TV" in out and HINT in out
    assert "POST /emby/Sessions/s1/Playing?ItemIds=558&PlayCommand=PlayNow token=ok" in p.emby_lines()
    p.reset()
    p.fast("wan")
    out, rc = p.run("558", "--json")
    d = json.loads(out)
    assert rc == 0 and d["primary"] and d["hint"] == HINT and "Sessions/f1/Playing" not in p.read("emby.log")

    p.reset()
    out, rc = p.run("555", "--target", "FASTBOX")
    assert rc == 1 and "the fast box's Emby session" in out and not p.posts()
