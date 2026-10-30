"""`phone` CLI exit codes and output against a live test gateway (PHONE_BIN overrides ~/bin/phone)."""
import asyncio
import json
import os

import pytest

from conftest import PHONE_BIN


async def run(env, *args, url=None):
    e = env.cli_env()
    if url:
        e["PHONEGW_URL"] = url
    p = await asyncio.create_subprocess_exec("python3", "-I", PHONE_BIN, *args, env=e, stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(p.communicate(), 60)
    return p.returncode, out.decode(), err.decode()


pytestmark = pytest.mark.skipif(not os.path.exists(PHONE_BIN), reason="phone CLI not found")


async def test_no_device_is_exit_4(env):
    code, out, _ = await run(env, "push", "--level", "info", "--title", "x", "--body", "y")
    assert code == 4 and "no paired device" in out


async def test_lint_exit_3_and_verb_exit_6_and_usage_2(env, phone):
    code, _, err = await run(env, "push", "--level", "urgent", "--title", "Bill", "--body", "CHF 120.50 due")
    assert code == 3 and "lint" in err
    code, _, err = await run(env, "push", "--level", "urgent", "--title", "x", "--action", "Pay:pay")
    assert code == 6
    code, _, _ = await run(env, "push", "--level", "loud", "--title", "x")
    assert code == 2
    code, _, _ = await run(env, "push", "--level", "info", "--title", "x", "--ttl", "soon")
    assert code == 2


async def test_gateway_down_exit_7(env):
    code, _, err = await run(env, "status", url="http://127.0.0.1:1")
    assert code == 7 and "gateway down" in err


async def test_pair_status_devices(env):
    code, out, _ = await run(env, "pair", "--name", "S25", "--json")
    j = json.loads(out)
    assert code == 0 and len(j["code"]) == 9 and j["code"][4] == "-"
    from conftest import FakePhone
    p = FakePhone(env)
    st, _ = await p.pair(j["code"])
    assert st == 200
    code, out, _ = await run(env, "devices")
    assert code == 0 and p.device_id in out and "S25" in out
    await p.connect()
    await p.recv("hello")
    code, out, _ = await run(env, "status")
    assert code == 0 and "online" in out
    code, out, _ = await run(env, "status", "--json")
    assert json.loads(out)["devices"][0]["online"] is True
    await p.close()


async def test_push_wait_ack_exit_0_and_timeout_5(env, phone):
    await phone.connect()
    await phone.recv("hello")
    code, out, _ = await run(env, "push", "--level", "urgent", "--title", "Last S9 in 25 min", "--body", "pl. 41",
                             "--action", "Seen:seen", "--action", "+15 min:snooze:r:1a2b3c4d", "--tag", "lasttrain",
                             "--ttl", "30m", "--live", "countdown:1792513680", "--wait", "5", "--json")
    j = json.loads(out)
    assert code == 0 and j["status"] == "acked"
    p = await phone.recv("push")
    assert p["body"]["actions"][1] == {"verb": "snooze", "label": "+15 min", "arg": "r:1a2b3c4d", "unlock": True}
    assert p["body"]["live"]["until"] == 1792513680 and p["ttl"] <= 1800
    phone.auto_ack = False
    code, out, _ = await run(env, "push", "--level", "timed", "--title", "x", "--wait", "0.5")
    assert code == 5
    code, out, _ = await run(env, "cancel", "lasttrain")
    assert code == 0 and "cancel lasttrain" in out
    code, out, _ = await run(env, "pushes", "--tag", "lasttrain", "--json")
    assert json.loads(out)["pushes"][0]["acked_ms"] is not None


async def test_chat_and_probe(env, phone):
    await phone.connect()
    await phone.recv("hello")
    code, out, _ = await run(env, "chat", "Done.", "--buttons", '[[{"text":"OK","data":"ok"}]]', "--wait", "5")
    assert code == 0 and "acked" in out
    c = await phone.recv("chat")
    assert c["body"]["buttons"][0][0]["data"] == "ok"
    code, out, _ = await run(env, "chat", "x", "--buttons", "not json")
    assert code == 2
    code, out, _ = await run(env, "probe", "--n", "3")
    assert code == 0 and "p95" in out
    code, out, _ = await run(env, "probe", "--idle", "1,5")
    assert code == 0 and "scheduled" in out


async def test_probe_offline_exit_4(env, phone):
    code, out, _ = await run(env, "probe")
    assert code == 4
