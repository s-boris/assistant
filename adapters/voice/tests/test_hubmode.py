"""Hub mode: the relay's ways out of claude-hub-voice, with a fake hub (no network)."""
import asyncio
import json
import os
import time

from aiohttp import web

from voice_relay import hubmode
from voice_relay.config import Config


class FakeHub:
    def __init__(self):
        self.events, self.calls = [], []
        self.ok = True

    async def event(self, kind, subject, payload, idem):
        self.events.append((kind, subject, payload, idem))
        return self.ok

    async def tool(self, name, args, timeout=10.0):
        self.calls.append((name, args))
        if name == "voice_lookup":
            return True, {"rc": 0, "out": "next train 13:04"}, ""
        return True, {"delivered": True, "result": {"ok": True, "route": "opened"}}, ""


def test_phone_args_map_every_form_the_relay_builds():
    tail = ["--label", "x", "--wait", "6", "--via", "voice", "--json"]
    assert hubmode.phone_args(["phone", "open", "--nav", "home", "--mode", "r"] + tail) == {
        "via": "voice", "nav": "home", "mode": "r"}
    assert hubmode.phone_args(["phone", "open", "--sbb", "here", "Luzern", "--arrive", "18:30"] + tail) == {
        "via": "voice", "sbb_from": "here", "sbb_to": "Luzern", "arrive": "18:30"}
    assert hubmode.phone_args(["phone", "open", "--app", "SBB Mobile"] + tail)["app"] == "SBB Mobile"
    assert hubmode.phone_args(["phone", "open", "https://x.example/a"] + tail)["url"] == "https://x.example/a"
    assert hubmode.phone_args(["phone", "push", "--level", "info"]) is None
    assert hubmode.phone_args(["phone", "open", "--unknown", "1"]) is None


def test_lookup_and_phone_open_go_through_the_gateway():
    hub = hubmode.Hub(Config())
    fake = FakeHub()
    hub.tool = fake.tool
    rc, out = asyncio.run(hub.lookup(["/home/x/bin/gohome", "--json"], 3.0))
    assert (rc, out) == (0, "next train 13:04") and fake.calls[0] == (
        "voice_lookup", {"argv": ["gohome", "--json"], "timeout_s": 3.0})
    rc, out = asyncio.run(hub.phone_open(["phone", "open", "--nav", "home", "--json"], 6.0))
    assert rc == 0 and json.loads(out)["route"] == "opened" and fake.calls[1][1]["format"] == "detailed"


def test_shipper_sends_notes_as_events_and_removes_them(tmp_path):
    cfg = Config(inbox_dir=str(tmp_path))
    for name in ("voice-recap-1.md", "voice-decisions-2.md", "voicerelay-call-failed-3.md", "voice-4-v1.md",
                 ".voice-5.md.tmp", "notes.txt"):
        (tmp_path / name).write_text("text of " + name)
    fake = FakeHub()
    sh = hubmode.Shipper(cfg, fake)
    assert asyncio.run(sh.once()) == 4
    kinds = {e[1]: e[0] for e in fake.events}
    assert kinds == {"voice-recap-1.md": "voice.recap", "voice-decisions-2.md": "voice.decisions",
                     "voicerelay-call-failed-3.md": "voice.alert", "voice-4-v1.md": "voice.handoff"}
    assert sorted(os.listdir(tmp_path)) == [".voice-5.md.tmp", "notes.txt"]
    (tmp_path / "voice-recap-9.md").write_text("x")
    fake.ok = False
    assert asyncio.run(sh.once()) == 0 and (tmp_path / "voice-recap-9.md").exists()  # kept until the hub has it


class FakeRelay:
    def __init__(self, state_dir):
        self.cfg = Config(hub_gw_url="http://gw", state_dir=str(state_dir))
        self.asked = []

    async def i_answer(self, req):
        b = await req.json()
        self.asked.append(("answer", b))
        return web.json_response({"delivered": "sync" if b["id"] == "v1" else "none"})

    async def i_call(self, req):
        b = await req.json()
        self.asked.append(("call", b))
        if "limit" in b["reason"]:
            return web.json_response({"status": "rate_limited", "detail": "max 3 outbound calls per hour"})
        return web.json_response({"status": "answered" if "train" in b["reason"] else "no_answer", "ring_s": 3.5,
                                  "call": "c1"})

    async def i_status(self, req):
        return web.json_response({"live": 1, "tg_calls": "on", "app_calls": 0, "minutes_today": 2.0})


def test_executor_runs_the_voice_tools_in_process(tmp_path):
    r = FakeRelay(tmp_path)
    ex = hubmode.Executor(r, FakeHub())
    run = lambda t, a: asyncio.run(ex.run_tool(t, a))  # noqa: E731
    assert run("voice.reply", {"id": "v1", "text": "Fourteen degrees."}) == {"delivered": True, "status": "spoken"}
    assert run("voice.reply", {"id": "v2", "text": "x"})["status"] == "not_delivered"
    c = run("voice.call", {"reason": "your train is late"})
    assert c["status"] == "answered" and c["result"] == {"status": "answered", "ring_s": 3.5, "call": "c1"}
    c = run("voice.call", {"reason": "over the limit"})  # the relay's own answer rides along for voice-hub
    assert c["status"] == "busy_or_limited" and c["result"] == {"status": "rate_limited",
                                                                "detail": "max 3 outbound calls per hour"}
    d = run("voice.drive_update", {"text": "jam ahead"})
    assert d["status"] == "not_answered" and "drive-update" in d["note"]
    assert run("voice.talk", {})["status"] == "not_answered" and r.asked[-1] == ("call", {"reason": "you tapped Talk"})
    s = run("voice.status", {})
    assert s["up"] and s["live"] == 1 and s["tg_calls"] == "on" and s["result"]["calls"] == []
    try:
        run("phone.push", {})
        raise AssertionError("not a voice tool")
    except KeyError:
        pass


def test_hub_mode_settings_come_from_the_environment():
    cfg = Config.from_env({"VOICE_HUB_MODE": "1", "VOICE_HUB_URL": "http://claude-hub:8700",
                           "VOICE_HANDOFF_ORDER": "hub,inbox", "VOICE_TG_ACCESS_FILE": "/srv/voice/config/access.json"})
    assert cfg.hub_mode and cfg.handoff_order == ("hub", "inbox") and cfg.tg_access_file.endswith("access.json")


def test_voice_status_carries_the_newest_calls_as_structured_fields_only(tmp_path):
    """The call log stays in claude-hub-voice: voice.status's result.calls is calllog.digest of the newest calls
    (kinds, end, closing stages, tool names and lanes, counts); never his words, the model's, tool args or texts."""
    from voice_relay.calllog import CallLog

    r = FakeRelay(tmp_path)
    log = CallLog(r.cfg, "app", "phone app (mTLS device via phonegw)", reason="SECRET-REASON")
    log.text("user", "SECRET-HIM how do I get home")
    log.tool("way_home", {"to": "SECRET-ARG"}, "fc-1", lane="fast", ms=120)
    log.result(None, {"answer": "SECRET-ANSWER"})
    log.text("model", "SECRET-GEMINI")
    log.event("idle", stage="ask", quiet_s=10)
    log.event("idle", stage="bye", why="idle_silence", quiet_s=5)
    log.event("hangup", by="assistant", why="silence")
    log.end("assistant_hangup", by="silence", why="idle_silence")
    s = asyncio.run(hubmode.Executor(r, FakeHub()).run_tool("voice.status", {}))
    (c,) = s["result"]["calls"]
    assert "SECRET" not in json.dumps(s)
    assert c["kind"] == "app" and c["end"]["reason"] == "assistant_hangup" and c["end"]["why"] == "idle_silence"
    assert c["idle"] == [["ask", None, 10], ["bye", "idle_silence", 5]] and c["hangup"] == ["assistant", "silence"]
    assert c["tools"] == [["way_home", "fast", None, False]] and c["results"] == ["answer"]
    assert c["events"]["him"] == 1 and c["events"]["gemini"] == 1
    assert c["closing"] == ["other"]                       # his line's closing class, never its words


def test_voice_status_carries_the_7_day_numbers_only(tmp_path):
    """result.stats_7d = calllog.week: counts, timing distributions and ring outcomes over 7 days, for the
    dashboard (the call log stays in claude-hub-voice); no code-word tallies, no text; an older call is left out."""
    from voice_relay.calllog import CallLog, week

    r = FakeRelay(tmp_path)
    log = CallLog(r.cfg, "tg", "owner", reason="SECRET-REASON")
    log.event("ring", outcome="answered", ring_s=3)
    log.text("user", "SECRET-HIM what's on today")
    log.event("reply", after_ms=850)
    log.text("model", "SECRET-GEMINI")
    log.end("hangup", by="him")
    old = CallLog(r.cfg, "app", "owner", clock=lambda: time.time() - 9 * 86400)
    old.event("reply", after_ms=9000)
    old.end("hangup", by="him")
    s = asyncio.run(hubmode.Executor(r, FakeHub()).run_tool("voice.status", {}))
    w = s["result"]["stats_7d"]
    assert "SECRET" not in json.dumps(s)
    assert w["days"] == 7 and w["calls"] == 1 and w["connected"] == 1 and w["ring"] == {"answered": 1}
    assert w["reply_ms"]["n"] == 1 and w["reply_ms"]["median"] == 850
    assert not {"kinds", "tools", "end_reason", "end_by", "end_why", "greet_in_via"} & set(w)
    assert all(isinstance(v, (int, dict)) and not isinstance(v, bool) for v in w.values())
    assert week(r.cfg, 30)["calls"] == 2


def _mixed_log(cfg, now):
    """Five outgoing calls (the rehearsal's shape: 2 answered, 3 not) with his own call and an app call between
    them, plus a call that never rang and one ringing right now; every text field carries SECRET."""
    from voice_relay.calllog import CallLog

    def call(kind, at, ring=None, end="hangup", talk=False, **kw):
        log = CallLog(cfg, kind, "owner", clock=lambda: now - at, reason="SECRET-REASON", say="SECRET-SAY", **kw)
        if ring:
            log.event("ring", outcome=ring, ring_s=45.7 if ring != "answered" else 6.2)
        if talk:
            log.text("user", "SECRET-HIM wake up")
            log.event("reply", after_ms=900)
            log.text("model", "SECRET-GEMINI")
            log.tool("ask_assistant", {"task": "SECRET-TASK"}, "fc-1", handoff="v1", route="hub")
        if end:
            log.end(end)
        return log.id

    ids = [call("tg-out", 9 * 3600, "answered", "assistant_hangup", talk=True),
           call("tg-out", 8 * 3600, "no_answer", "no answer"),
           call("tg-in", 7 * 3600, talk=True),
           call("tg-out", 6 * 3600, "answered", "hangup", talk=True),
           call("app", 5 * 3600, talk=True),
           call("tg-out", 4 * 3600, "no_answer", "no answer"),
           call("tg-out", 3 * 3600, "declined", "declined"),
           call("tg-out", 2 * 3600, None, "rate limited"),
           call("tg-out", 60, None, None)]                     # ringing now: no ring outcome, no end
    return ids


def test_voice_calls_gives_each_outgoing_calls_outcome_from_the_ring_never_text(tmp_path):
    """voice.calls: the newest calls as digests; each call that rang him carries outcome + answered from its ring
    (or never rang: rate_limited), incoming calls none; outgoing=true lists only calls that rang him, so his own
    calls between them never count; the summary counts them; stats = quality numbers over `days`; no text."""
    r = FakeRelay(tmp_path)
    now = time.time()
    ids = _mixed_log(r.cfg, now)
    ex = hubmode.Executor(r, FakeHub())
    d = asyncio.run(ex.run_tool("voice.calls", {"last": 5}))
    assert "SECRET" not in json.dumps(d)
    assert [c["id"] for c in d["calls"]] == ids[-5:]
    assert [(c["kind"], c["outcome"], c["answered"]) for c in d["calls"]] == [
        ("app", None, None), ("tg-out", "no_answer", False), ("tg-out", "declined", False),
        ("tg-out", "rate_limited", False), ("tg-out", None, None)]
    assert d["outgoing"] == {"calls": 4, "answered": 0, "not_answered": 3, "open": 1,
                             "by": {"no_answer": 1, "declined": 1, "rate_limited": 1, "open": 1}}
    o = asyncio.run(ex.run_tool("voice.calls", {"last": 5, "outgoing": True}))
    assert [c["kind"] for c in o["calls"]] == ["tg-out"] * 5 and [c["id"] for c in o["calls"]] == [
        ids[3], ids[5], ids[6], ids[7], ids[8]]
    first = asyncio.run(ex.run_tool("voice.calls", {"last": 50, "outgoing": True}))["calls"][0]
    assert first["ring"] == ["answered", 6.2] and first["talked"] and first["tools"] == [
        ["ask_assistant", "hub", None, True]]
    assert o["stats"]["out_calls"] == 7 and o["stats"]["out_answered"] == 2 and o["stats"]["out_not_answered"] == 4
    assert o["stats"]["days"] == 7 and o["stats"]["calls"] == 9 and o["live"] is None
    n = asyncio.run(ex.run_tool("voice.calls", {"last": 0, "days": 0.1}))     # numbers only, 2.4 h window
    assert n["calls"] == [] and n["stats"]["calls"] == 2 and n["stats"]["days"] == 0.1
    assert asyncio.run(ex.run_tool("voice.calls", {"days": 99}))["stats"]["days"] == 7   # out of range: the default


def test_voice_status_digests_carry_the_outcome_too(tmp_path):
    r = FakeRelay(tmp_path)
    _mixed_log(r.cfg, time.time())
    s = asyncio.run(hubmode.Executor(r, FakeHub()).run_tool("voice.status", {}))
    assert [c["answered"] for c in s["result"]["calls"]] == [None, False, False, False, None]
    assert s["result"]["stats_7d"]["out_calls"] == 7 and "SECRET" not in json.dumps(s)


def _fake_voice_hub(tmp_path, data, rc=0):
    vh = tmp_path / "voice-hub"
    out = tmp_path / "vh-out.json"
    out.write_text(json.dumps(data))
    vh.write_text('#!/bin/sh\necho "$@" >> "%s/vh-args"\ncat "%s"\nexit %d\n' % (tmp_path, out, rc))
    vh.chmod(0o755)
    return vh


def test_calls_and_stats_cli_read_the_hub_outside_the_container(tmp_path, monkeypatch, capsys):
    """Outside claude-hub-voice (no hub mode) `voice-relay calls|stats` read voice.calls through voice-hub and print
    metadata only: each call's kind, time, answered or not, talk turns, tools and end, and the outgoing summary."""
    from voice_relay import calllog, server

    src = FakeRelay(tmp_path / "hubstate")
    now = time.time()
    _mixed_log(src.cfg, now)
    hub = asyncio.run(hubmode.Executor(src, FakeHub()).run_tool("voice.calls", {"last": 50, "outgoing": True}))
    hub["live"] = [hub["calls"][-1]["id"]]
    vh = _fake_voice_hub(tmp_path, dict(hub, via="hub"))
    for k in ("VOICE_HUB_MODE", "VOICE_STATE_DIR"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("VOICE_HUB_BIN", str(vh))
    monkeypatch.setenv("VOICE_STATE_DIR", str(tmp_path / "brain-state"))     # the brain's own (stale) log: unread
    assert server.main(["calls", "--last", "9", "--out"]) == 0
    out = capsys.readouterr().out
    assert "SECRET" not in out and (tmp_path / "vh-args").read_text().split() == ["calls", "--last", "9", "--out"]
    assert out.count(" · tg-out · ") == 7 and "NOT ANSWERED (no answer) after 45.7s ringing" in out
    assert "answered after 6.2s ringing · talk: him 1, assistant 1 turn" in out
    assert "NOT ANSWERED (declined)" in out and "NOT RUNG (hourly limit)" in out
    assert "no outcome yet · still on (live now)" in out and "tools: ask_assistant (hub)" in out
    assert "outgoing (rang him): 2 of 7 answered, 4 not answered (no_answer 2, declined 1, rate_limited 1), " \
           "1 without an outcome yet" in out
    assert server.main(["calls", "--json"]) == 0 and json.loads(capsys.readouterr().out)["via"] == "hub"
    assert server.main(["stats", "--days", "0"]) == 0                    # 0 = everything the log keeps (14 days)
    out = capsys.readouterr().out
    assert "(last 14 days)" in out and "outgoing (rang him): 7, answered 2, not answered 4" in out
    assert (tmp_path / "vh-args").read_text().splitlines()[-1].split() == ["calls", "--last", "0", "--days", "14"]
    # down / missing: exit 2, said on stderr
    monkeypatch.setenv("VOICE_HUB_BIN", str(_fake_voice_hub(tmp_path, {}, rc=2)))
    assert server.main(["calls"]) == 2
    monkeypatch.setenv("VOICE_HUB_BIN", str(tmp_path / "missing"))
    assert server.main(["stats"]) == 2 and "missing" in capsys.readouterr().err
    # inside the container (hub mode) the local log in full, outgoing filter included
    monkeypatch.setenv("VOICE_HUB_MODE", "1")
    monkeypatch.setenv("VOICE_STATE_DIR", src.cfg.state_dir)
    monkeypatch.setenv("VOICE_LOCAL_TOKEN_FILE", str(tmp_path / "no-token"))
    monkeypatch.setenv("VOICE_INTERNAL_PORT", "9")
    assert server.main(["calls", "--last", "2", "--out", "--json"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert [c["outcome"] for c in d["calls"]] == ["rate_limited", None] and d["outgoing"]["open"] == 1
    assert server.main(["calls", "--last", "1"]) == 0 and "SECRET-REASON" in capsys.readouterr().out  # full, local
    assert calllog.KEEP_DAYS == 14
