"""Phone app line drops (phonegw holds the call for its grace window and sends link lost/back): silence timers pause,
late answers wait for the line (or go to Telegram), the model says it lost him for a moment (and repeats what he
missed), resume count + gap in the call log, usage voice.link_resume and voice.call_end; phonegw's grace running out ends
the call as "app lost" (why network). StubProvider / FakeGemini only."""
import asyncio
import json

import aiohttp
from aiohttp.test_utils import TestClient, TestServer

from util import TOKEN, Rig, until
from voice_relay import calllog, closing
from voice_relay.calllog import load_calls, calls_dir, quality
from voice_relay.session import IDLE_ASK, link_back_text

from test_closing import ULog, end_row, him, mk, run, says
from test_endcall import ends


def link_events(cfg):
    return [r for r in ends(cfg) if r.get("ev") == "link"]


def test_line_down_pauses_the_silence_timer_and_back_says_it_lost_him(tmp_path):
    async def main():
        ul = ULog()
        cfg, p, s = await mk(tmp_path, ulog=ul, kind="app", closing_bye_s=5)
        await him(p, "what's on my calendar")
        await says(p, "Nothing until the gym at six.")
        assert await until(lambda: s.answered and not s.turn_open, timeout=2)
        await s.link_lost()
        await asyncio.sleep(0.9)                               # idle_prompt_s is 0.4: no "anything else?" into the gap
        assert not getattr(p, "prompts", None) and not s.closed
        await s.link_back(3400)
        assert p.prompts == [link_back_text(3400, False)]
        assert "about 3 seconds" in p.prompts[0] and "lost him for a moment" in p.prompts[0]
        assert await until(lambda: len(p.prompts) == 2, timeout=2)   # the silence clock started over at back
        assert p.prompts[1] == IDLE_ASK
        await s.close("hangup")
        lk = link_events(cfg)
        assert [e["state"] for e in lk] == ["lost", "back"] and lk[1]["gap_ms"] == 3400 and lk[1]["n"] == 1
        res = [c for c in ul.calls if c[0][0] == "voice.link_resume"]
        assert len(res) == 1 and res[0][1]["ms"] == 3400 and res[0][1]["outcome"] == "ok"
        assert res[0][1]["channel"] == "app" and res[0][1]["attrs"]["n"] == 1 and res[0][1]["actor"] == "system"
        end = [c for c in ul.calls if c[0][0] == "voice.call_end"][0][1]
        assert end["attrs"]["resumes"] == 1 and end["attrs"]["gap_ms"] == 3400
    run(main())


def test_words_said_into_the_gap_are_repeated(tmp_path):
    async def main():
        cfg, p, s = await mk(tmp_path, kind="app")
        await s.link_lost()
        await says(p, "The train leaves at 18:04 from platform 7.")
        assert await until(lambda: s.link_spoke, timeout=2)
        await s.link_back(None)                                # no gap given: measured here
        assert "say it again briefly" in p.prompts[0]
        assert link_events(cfg)[1]["missed"] is True
        await s.close("hangup")
    run(main())


def test_back_during_the_goodbye_adds_no_line_and_a_second_lost_is_one_event(tmp_path):
    async def main():
        cfg, p, s = await mk(tmp_path, kind="app")
        await s.link_lost()
        await s.link_lost()
        s.idle_stage = 2
        await s.link_back(800)
        assert not getattr(p, "prompts", None)
        await s.link_back(800)                                 # not down any more: ignored
        assert [e["state"] for e in link_events(cfg)] == ["lost", "back"]
        await s.close("hangup")
    run(main())


def test_late_answer_waits_for_the_line_then_goes_live(tmp_path):
    async def main():
        cfg, p, s = await mk(tmp_path, kind="app", link_answer_wait_s=3)
        await s.link_lost()
        t = asyncio.ensure_future(s._deliver_late("h1", "[assistant result] It's 21 degrees."))
        await asyncio.sleep(0.3)
        assert not t.done() and not p.injects
        await s.link_back(1200)
        assert await t is True
        assert p.injects == ["[assistant result] It's 21 degrees."]
        await s.close("hangup")
    run(main())


def test_late_answer_goes_to_telegram_when_the_line_stays_down(tmp_path):
    async def main():
        cfg, p, s = await mk(tmp_path, kind="app", link_answer_wait_s=0.3)
        s.working.add("h1")
        await s.link_lost()
        assert await s._deliver_late("h1", "[assistant result] It's 21 degrees.") is False
        assert not p.injects and "h1" not in s.working and s.link_tg
        await s.link_back(5000)
        assert "went to Telegram" in p.prompts[0]
        late = [r for r in ends(cfg) if r.get("ev") == "late"][0]
        assert late["delivered"] == "telegram"
        await s.close("hangup")
        q = quality(load_calls(calls_dir(cfg), last=5))
        assert q["late_live"] == 0 and q["link_lost"] == 1 and q["link_back"] == 1
        assert q["link_gap_ms"]["n"] == 1 and "app line drops 1, resumed 1" in calllog.format_quality(q)
    run(main())


def test_link_resume_off_keeps_the_old_behaviour(tmp_path):
    async def main():
        cfg, p, s = await mk(tmp_path, kind="app", link_resume=False)
        await s.link_lost()
        assert not s.link_down
        await s.link_back(100)
        assert not getattr(p, "prompts", None) and link_events(cfg) == []
        await s.close("hangup")
    run(main())


def test_end_why_network_for_app_lost():
    assert closing.end_why("app lost", calllog.ended_by("app lost")) == "network"
    assert calllog.ended_by("app lost") == "network"
    assert closing.end_why("app closed", calllog.ended_by("app closed")) == "him"


async def _ticket(r):
    async with aiohttp.ClientSession() as s:
        async with s.post("http://127.0.0.1:%d/internal/ticket" % r.iport, json={"via": "app"},
                          headers={"Authorization": "Bearer " + TOKEN}) as resp:
            return (await resp.json())["ticket"]


def test_app_ws_link_frames_and_grace_end(tmp_path):
    """Over /ws as phonegw speaks it: link lost/back reach the session; hangup why=app_lost ends as "app lost"."""
    async def main():
        r = await Rig.make(str(tmp_path))
        c = TestClient(TestServer(r.relay.public_app()))
        await c.start_server()
        try:
            ws = await c.ws_connect("/ws?ticket=" + await _ticket(r))
            await ws.send_str(json.dumps({"type": "start", "mode": "open"}))

            async def drain():                                 # read like phonegw does (close handshakes finish)
                async for _ in ws:
                    pass
            reader = asyncio.ensure_future(drain())
            assert await until(lambda: r.relay.app_live, 5)
            s = next(iter(r.relay.app_live))
            await ws.send_str(json.dumps({"type": "link", "state": "lost"}))
            assert await until(lambda: s.link_down, 2)
            await ws.send_str(json.dumps({"type": "link", "state": "back", "gap_ms": 2500}))
            assert await until(lambda: not s.link_down, 2)
            assert await until(lambda: any("lost him for a moment" in i for i in r.gem.log["injects"]), 5)
            await ws.send_str(json.dumps({"type": "link", "state": "lost"}))
            await ws.send_str(json.dumps({"type": "hangup", "why": "app_lost"}))
            assert await until(lambda: s.closed and not r.relay.app_live, 8)
            row = end_row(r.cfg)
            assert row["reason"] == "app lost" and row["why"] == "network"
            await ws.close()
            reader.cancel()
        finally:
            await c.close()
            await r.close()
    run(main())
