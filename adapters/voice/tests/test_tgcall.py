import asyncio
import json
import os

import pytest

from util import Rig, until
from voice_relay.audio import tone
from voice_relay.fakes import FakeCallClient
from voice_relay.providers import make_provider
from voice_relay.server import instructions
from voice_relay.tgcall import BridgeRefused, CallBridge, load_userbot_config

OWNER = 111222333


def run(coro):
    return asyncio.run(coro)


async def bridge(r, client=None, **kw):
    client = client or FakeCallClient()
    b = CallBridge(r.cfg, client, OWNER, r.relay.handoff, lambda: instructions(r.cfg), make_provider, **kw)
    await b.start()
    return b, client


def test_owner_call_round_trip_with_paced_48k_audio(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path), script=["what's the weather tomorrow"])
        try:
            b, cl = await bridge(r)
            await cl.ring("c1", OWNER)
            assert cl.accepted == ["c1"]
            assert await until(lambda: r.gem.log["setups"])
            assert r.gem.log["setups"][0]["realtimeInputConfig"]["automaticActivityDetection"]["disabled"] is False
            await cl.speak("c1", tone(48000, 1200))
            assert await until(lambda: r.gem.log["tool_responses"], 5)
            assert "Fake answer about: what's the weather tomorrow" in r.gem.log["tool_responses"][0]["response"]["answer"]
            assert await until(lambda: len(cl.sent["c1"]) >= 960 * 19, 5)
            ts = cl.frame_times["c1"]
            assert ts[-1] - ts[0] >= 0.15                       # paced in real time, not dumped
            assert r.gem.log["audio_bytes"] >= 32000            # 48k in → 16k to the provider
            await cl.end("c1")
            assert await until(lambda: not b.calls)
        finally:
            await r.close()
    run(main())


def test_strangers_are_declined_before_any_audio(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path))
        try:
            b, cl = await bridge(r)
            await cl.ring("x1", 999)
            await cl.speak("x1", tone(48000, 300))
            assert cl.declined == ["x1"] and cl.accepted == [] and b.rejected == 1
            assert r.gem.log["setups"] == [] and r.gem.log["audio_bytes"] == 0
        finally:
            await r.close()
    run(main())


def test_busy_and_hourly_call_cap(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path))
        try:
            b, cl = await bridge(r, max_calls_per_hour=2)
            await cl.ring("a", OWNER)
            await cl.ring("b", OWNER)                            # one call at a time
            assert cl.declined == ["b"]
            await cl.end("a")
            await cl.ring("c", OWNER)
            await cl.end("c")
            await cl.ring("d", OWNER)                            # third in the hour
            assert cl.accepted == ["a", "c"] and cl.declined == ["b", "d"]
        finally:
            await r.close()
    run(main())


def test_refuses_bot_sessions_own_account_and_missing_owner(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path))
        try:
            with pytest.raises(BridgeRefused):
                await bridge(r, FakeCallClient(is_bot=True))
            with pytest.raises(BridgeRefused):
                await bridge(r, FakeCallClient(me_id=OWNER))
            with pytest.raises(BridgeRefused):
                await CallBridge(r.cfg, FakeCallClient(), 0, r.relay.handoff, str, make_provider).start()
        finally:
            await r.close()
    run(main())


def test_hangup_with_open_task_reports_not_delivered(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path), script=["slow: check the backups"], slow_delay=0.5, sync_wait_s=0.1)
        try:
            b, cl = await bridge(r)
            await cl.ring("c1", OWNER)
            await until(lambda: r.gem.log["setups"])
            await cl.speak("c1", tone(48000, 1200))
            assert await until(lambda: r.gem.log["tool_responses"], 5)
            await cl.end("c1")
            assert await until(lambda: r.inbox.results, 3)
            assert r.inbox.results[0]["delivered"] == "none"
        finally:
            await r.close()
    run(main())


def test_userbot_config_rules(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    sec = tmp_path / ".secrets"
    sec.mkdir()
    conf = sec / "voice-tg.json"

    def write(d, mode=0o600):
        conf.write_text(json.dumps(d))
        os.chmod(conf, mode)

    good = {"api_id": 12345, "api_hash": "abc", "session_file": str(sec / "voice-tg.session")}
    write(good)
    assert load_userbot_config(str(conf))["session_file"].endswith("voice-tg.session")
    write(good, 0o644)
    with pytest.raises(BridgeRefused):
        load_userbot_config(str(conf))
    write(dict(good, bot_token="123:abc"))
    with pytest.raises(BridgeRefused):
        load_userbot_config(str(conf))
    write(dict(good, session_file=str(tmp_path / ".claude" / "x.session")))
    with pytest.raises(BridgeRefused):
        load_userbot_config(str(conf))
    write({"api_id": 1})
    with pytest.raises(BridgeRefused):
        load_userbot_config(str(conf))


def test_dead_provider_declines_the_owner_call_and_leaves_one_note(tmp_path):
    """Gemini down (billing, outage): the owner's call is declined before pickup, never accepted then dropped;
    one ~/inbox note per 30 min; the status counters say why."""
    import dataclasses
    from voice_relay.fakes import FakeCallClient as Fake
    async def main():
        r = await Rig.make(str(tmp_path))
        try:
            cfg = dataclasses.replace(r.cfg, gemini_ws_url="ws://127.0.0.1:9/ws")
            assert cfg.inbox_dir.startswith(str(tmp_path))
            cl = Fake()
            b = CallBridge(cfg, cl, OWNER, r.relay.handoff, lambda: instructions(cfg), make_provider)
            await b.start()
            await cl.ring("d1", OWNER)
            assert await until(lambda: cl.declined == ["d1"], 5)
            assert cl.accepted == [] and cl.hung == [] and not b.calls and b.provider_failures == 1
            assert "connect failed" in b.last_failure
            await cl.ring("d2", OWNER)
            assert await until(lambda: cl.declined == ["d1", "d2"], 5)
            notes = [f for f in os.listdir(cfg.inbox_dir) if f.startswith("voicerelay-call-failed-")]
            assert len(notes) == 1
        finally:
            await r.close()
    run(main())
