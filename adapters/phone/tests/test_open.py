"""Phone actions (`open`): the gateway sends it only on a live socket, waits for open.result, treats silence as a stale
socket (closes it, the next call is offline at once), applies the direct-host policy; `phone open` builds the nav /
SBB / app targets and falls back to a Telegram URL button (stub tg-buttons; PHONE_BIN overrides ~/bin/phone)."""
import asyncio
import json
import os

import pytest

from conftest import PHONE_BIN

CAPS = ["push", "chat", "link", "open"]
NAV = "google.navigation:q=Bern%20HB&mode=d"


async def answer(phone, result="opened", how="overlay", **kw):
    """The fake app: answer the next open frame."""
    f = await phone.recv("open")
    await phone.send("open.result", dict({"open_id": f["body"]["open_id"], "result": result, "how": how}, **kw))
    return f


async def gw_open(env, **kw):
    body = {"kind": "nav", "url": NAV, "label": "Navigation to Bern HB", "wait": 3}
    body.update(kw)
    return await env.call("POST", "/internal/open", body)


async def test_open_reaches_the_app_and_reports_its_result(env, phone):
    await phone.connect(caps=CAPS)
    task = asyncio.ensure_future(answer(phone, locked=True, car=True))
    st, j = await gw_open(env)
    f = await task
    assert st == 200 and j["ok"] and j["status"] == "opened" and j["how"] == "overlay"
    assert j["locked"] is True and j["car"] is True and j["direct"] is True
    b = f["body"]
    assert b["kind"] == "nav" and b["url"] == NAV and b["direct"] is True and f["ack"] is False
    assert b["until"] >= f["ts"] + 3
    assert env.gw.db.one("SELECT COUNT(*) AS n FROM outbox WHERE t='open'")["n"] == 0, "never queued"


async def test_silence_marks_the_socket_stale_and_the_next_call_is_offline(env, phone):
    await phone.connect(caps=CAPS)
    st, j = await gw_open(env, wait=1)
    assert st == 200 and j["status"] == "timeout" and not j["ok"]
    t0 = asyncio.get_running_loop().time()
    st, j = await gw_open(env, wait=5)
    assert j["status"] == "offline" and asyncio.get_running_loop().time() - t0 < 1.0


async def test_no_device(env):
    st, j = await gw_open(env)
    assert j["status"] == "no_device"


async def test_old_app(env, phone):
    await phone.connect(caps=["push", "link"])
    await asyncio.sleep(0.2)
    st, j = await gw_open(env)
    assert j["status"] == "unsupported"


async def test_offline_phone_gets_nothing_queued(env, phone):
    await phone.connect(caps=CAPS)
    await asyncio.sleep(0.2)
    await phone.close()
    await asyncio.sleep(0.2)
    st, j = await gw_open(env)
    assert j["status"] == "offline"
    assert env.gw.db.one("SELECT COUNT(*) AS n FROM outbox WHERE t='open'")["n"] == 0


@pytest.mark.parametrize("kind,url,direct", [
    ("url", "https://www.youtube.com/watch?v=x", True),
    ("url", "https://de.wikipedia.org/wiki/Bern", True),
    ("url", "https://example.com/pay?x=1", False),
    ("url", "https://youtube.com.evil.example/", False),
    ("sbb", "https://app.sbbmobile.ch/timetable?to=Bern&time=1792513200", True),
    ("nav", "https://www.google.com/maps/dir/?api=1&destination=Bern&travelmode=transit&dir_action=navigate", True),
])
async def test_direct_policy(env, phone, kind, url, direct):
    await phone.connect(caps=CAPS)
    task = asyncio.ensure_future(answer(phone, result="opened" if direct else "notified",
                                        how="overlay" if direct else "notification"))
    st, j = await gw_open(env, kind=kind, url=url, label="x")
    f = await task
    assert st == 200 and f["body"]["direct"] is direct and j["direct"] is direct


async def test_config_extends_the_direct_hosts(env, phone):
    env.gw.cfg["open"] = {"direct_hosts": ["example.com"]}
    await phone.connect(caps=CAPS)
    task = asyncio.ensure_future(answer(phone))
    st, j = await gw_open(env, kind="url", url="https://media.example.com/x", label="x")
    assert (await task)["body"]["direct"] is True


@pytest.mark.parametrize("body", [
    {"kind": "url", "url": "http://example.com/"},
    {"kind": "url", "url": "javascript:alert(1)"},
    {"kind": "url", "url": "geo:47.3,8.5"},                       # url = https only
    {"kind": "nav", "url": "https://example.com/maps/x"},          # nav = Google Maps only
    {"kind": "sbb", "url": "https://www.google.com/maps/"},       # sbb = SBB only
    {"kind": "app", "app": "Maps", "url": "https://www.google.com/maps/"},
    {"kind": "app", "app": "bad\nname"},
    {"kind": "shell", "url": NAV},
])
async def test_refused_targets(env, phone, body):
    await phone.connect(caps=CAPS)
    st, j = await env.call("POST", "/internal/open", dict(body, label="x"))
    assert st in (400, 403)


async def test_app_kind(env, phone):
    await phone.connect(caps=CAPS)
    task = asyncio.ensure_future(answer(phone, result="refused", how="none"))
    st, j = await gw_open(env, kind="app", app="Spotify", url=None, label="Spotify")
    f = await task
    assert f["body"]["app"] == "Spotify" and "url" not in f["body"]
    assert j["status"] == "refused" and not j["ok"]


# ------------------------------------------------------------------ CLI
pytestmark_cli = pytest.mark.skipif(not os.path.exists(PHONE_BIN), reason="phone CLI not found")


def stub(tmp_path, name, script):
    p = tmp_path / name
    p.write_text("#!/bin/sh\n" + script)
    p.chmod(0o755)
    return str(p)


async def run(env, *args, tg=None, extra=None):
    e = env.cli_env()
    e.pop("PHONE_TG_BUTTONS", None)
    if tg:
        e["PHONE_TG_BUTTONS"] = tg
    e.update(extra or {})
    p = await asyncio.create_subprocess_exec("python3", "-I", PHONE_BIN, *args, env=e, stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(p.communicate(), 60)
    return p.returncode, out.decode(), err.decode()


@pytestmark_cli
async def test_cli_nav_opens_on_the_phone(env, phone, tmp_path):
    await phone.connect(caps=CAPS)
    log = tmp_path / "tg.log"
    tg = stub(tmp_path, "tg-buttons", "echo \"$1\" >> %s\n" % log)
    task = asyncio.ensure_future(answer(phone))
    rc, out, err = await run(env, "open", "--nav", "Kunsthaus Zürich", "--json", tg=tg)
    f = await task
    assert rc == 0, err
    j = json.loads(out)
    assert j["route"] == "opened" and j["how"] == "overlay" and j["kind"] == "nav"
    assert f["body"]["url"] == "google.navigation:q=Kunsthaus%20Z%C3%BCrich&mode=d"
    assert f["body"]["label"] == "Navigation to Kunsthaus Zürich"
    assert not log.exists()


@pytestmark_cli
async def test_cli_transit_and_home(env, phone, tmp_path):
    await phone.connect(caps=CAPS)
    prof = stub(tmp_path, "profile-get", 'case "$1" in home.street) echo "Teststrasse 1";; home.postcode) echo 9999;;'
                                         ' home.town) echo Testdorf;; *) exit 2;; esac\n')
    task = asyncio.ensure_future(answer(phone))
    rc, out, err = await run(env, "open", "--nav", "home", "--mode", "r", "--json", extra={"PROFILE_GET": prof})
    f = await task
    assert rc == 0, err
    assert f["body"]["url"] == ("https://www.google.com/maps/dir/?api=1&destination=Teststrasse%201,%209999%20Testdorf"
                                "&travelmode=transit&dir_action=navigate")
    assert f["body"]["label"] == "Transit to home"
    assert "Teststrasse" not in out


@pytestmark_cli
async def test_cli_sbb_from_here(env, phone, tmp_path):
    await phone.connect(caps=CAPS)
    task = asyncio.ensure_future(answer(phone))
    rc, out, err = await run(env, "open", "--sbb", "here", "Bern", "--at", "2026-10-22 18:02", "--json")
    f = await task
    assert rc == 0, err
    assert f["body"]["kind"] == "sbb"
    # SBB Mobile's own current-location value: the SBB app fills the start field on the phone (no coordinates sent)
    assert f["body"]["url"].startswith("https://app.sbbmobile.ch/timetable?from=CURRENT_POSITION&to=Bern&time=")
    assert f["body"]["label"] == "SBB: to Bern"
    task = asyncio.ensure_future(answer(phone))
    rc, out, err = await run(env, "open", "--sbb", "Zürich HB", "Bern", "--json")
    f = await task
    assert rc == 0, err
    assert f["body"]["url"].startswith("https://app.sbbmobile.ch/timetable?from=Z%C3%BCrich%20HB&to=Bern&time=")
    assert f["body"]["label"] == "SBB: Zürich HB to Bern"


@pytestmark_cli
async def test_cli_sbb_arrive_by(env, phone, tmp_path):
    await phone.connect(caps=CAPS)
    task = asyncio.ensure_future(answer(phone))
    state = tmp_path / "usage"
    rc, out, err = await run(env, "open", "--sbb", "here", "Bern", "--arrive", "2026-10-22 18:30", "--json",
                             extra={"USAGE_TEST": "1", "USAGE_STATE": str(state)})
    f = await task
    assert rc == 0, err
    # usage: an opened exec is ok (not an error), the route rides in attrs
    recs = [json.loads(x) for p in state.rglob("*") if p.is_file() for x in p.read_text().splitlines() if x.strip()]
    rec = [r for r in recs if r.get("f") == "phone.open.sbb"][-1]
    assert rec["k"] == "exec" and rec["o"] == "ok" and rec["at"]["route"] == "opened"
    url = f["body"]["url"]
    # SBB Mobile's link parser: timemode=arrival makes `time` the latest arrival ("arrive by")
    assert url.startswith("https://app.sbbmobile.ch/timetable?from=CURRENT_POSITION&to=Bern&time=")
    assert url.endswith("&timemode=arrival")
    import datetime as dt
    t = int(url.split("time=")[1].split("&")[0])
    assert dt.datetime.fromtimestamp(t).astimezone().strftime("%Y-%m-%d %H:%M") == "2026-10-22 18:30"
    assert f["body"]["label"] == "SBB: to Bern, arrive by 18:30"
    rc, out, err = await run(env, "open", "--sbb", "here", "Bern", "--at", "18:00", "--arrive", "18:30")
    assert rc == 2 and "not both" in err
    rc, out, err = await run(env, "open", "--nav", "Bern", "--arrive", "18:30")
    assert rc == 2


@pytestmark_cli
async def test_cli_stale_socket_falls_back_to_telegram(env, phone, tmp_path):
    await phone.connect(caps=CAPS)                     # connected, but never answers (Android Auto dropped the tunnel)
    log = tmp_path / "tg.log"
    tg = stub(tmp_path, "tg-buttons", "printf '%%s\\n%%s\\n' \"$1\" \"$2\" >> %s\n" % log)
    rc, out, err = await run(env, "open", "--nav", "Bern HB", "--wait", "1", "--json", tg=tg)
    assert rc == 0, err
    j = json.loads(out)
    assert j["route"] == "telegram" and j["app"] == "timeout"
    text, rows = log.read_text().splitlines()[:2]
    assert text == "📍 Navigation to Bern HB"
    assert json.loads(rows) == [[{"text": "Navigate", "url": "https://www.google.com/maps/dir/?api=1&destination="
                                                             "Bern%20HB&travelmode=driving&dir_action=navigate"}]]
    rc, out, err = await run(env, "open", "--nav", "Bern HB", "--json", "--no-telegram")
    assert rc == 4 and json.loads(out)["app"] == "offline"


@pytestmark_cli
async def test_cli_app_never_goes_to_telegram_and_usage_errors(env, phone, tmp_path):
    rc, out, err = await run(env, "open", "--app", "Spotify", "--json")
    assert rc == 4 and json.loads(out)["route"] is None
    rc, out, err = await run(env, "open", "http://example.com/")
    assert rc == 6
    rc, out, err = await run(env, "open", "--nav", "x", "--app", "y")
    assert rc == 2
