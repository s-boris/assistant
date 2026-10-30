"""One-tap links (push.link): gateway validation + the 'link' caps gate, and `phone link` (Maps URLs, Telegram
URL-button fallback through a stub tg-buttons; PHONE_BIN overrides ~/bin/phone)."""
import asyncio
import json
import os

import pytest

from conftest import PHONE_BIN

LINK_CAPS = ["push", "chat", "link"]


async def push(env, **kw):
    body = {"level": "timed", "title": "Kunsthaus", "body": "", "tag": "link-t"}
    body.update(kw)
    return await env.call("POST", "/internal/push", body)


async def test_link_reaches_a_link_capable_app(env, phone):
    await phone.connect(caps=LINK_CAPS)
    st, j = await push(env, wait=5, link={"url": "https://www.google.com/maps/search/?api=1&query=Bern",
                                          "label": "Open in Maps but this label is far too long"})
    assert st == 200 and j["status"] == "acked"
    b = (await phone.recv("push"))["body"]
    assert b["link"] == {"url": "https://www.google.com/maps/search/?api=1&query=Bern", "label": "Open in Maps but this la"}
    assert b["private"] is True


@pytest.mark.parametrize("url", ["javascript:alert(1)", "file:///sdcard/a.html", "content://media/x", "http://x.ch/",
                                 "intent://x#Intent;end", "https://google.com@evil.example/", "https://x.ch/a b",
                                 "https://x.ch/\n", "HTTPS://x.ch/", "", None])
async def test_unsafe_links_refused(env, phone, url):
    await phone.connect(caps=LINK_CAPS)
    st, j = await push(env, link={"url": url})
    assert st == 403 and j["error"] == "link"
    assert env.gw.db.one("SELECT COUNT(*) AS n FROM outbox WHERE t='push'")["n"] == 0


async def test_older_app_gets_no_link_push(env, phone):
    await phone.connect()                      # hello caps without "link" (app <= 0.3.3)
    st, j = await push(env, link={"url": "geo:47.37,8.54"})
    assert st == 200 and j["status"] == "unsupported"
    assert env.gw.db.one("SELECT COUNT(*) AS n FROM outbox WHERE t='push'")["n"] == 0


# ------------------------------------------------------------------ CLI
pytestmark_cli = pytest.mark.skipif(not os.path.exists(PHONE_BIN), reason="phone CLI not found")


def tg_stub(tmp_path, rc=0):
    log = tmp_path / "tg.log"
    stub = tmp_path / "tg-buttons"
    stub.write_text("#!/bin/sh\nprintf '%%s\\n%%s\\n' \"$1\" \"$2\" >> %s\nexit %d\n" % (log, rc))
    stub.chmod(0o755)
    return str(stub), log


async def run(env, *args, tg=None):
    e = env.cli_env()
    e.pop("PHONE_TG_BUTTONS", None)
    if tg:
        e["PHONE_TG_BUTTONS"] = tg
    p = await asyncio.create_subprocess_exec("python3", "-I", PHONE_BIN, *args, env=e, stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(p.communicate(), 60)
    return p.returncode, out.decode(), err.decode()


@pytestmark_cli
async def test_cli_maps_nav_goes_to_the_app(env, phone, tmp_path):
    await phone.connect(caps=LINK_CAPS)
    tg, log = tg_stub(tmp_path)
    rc, out, err = await run(env, "link", "--maps", "Kunsthaus Zürich", "--nav", "transit", "--wait", "5", "--json", tg=tg)
    assert rc == 0, err
    j = json.loads(out)
    assert j["route"] == "app"
    b = (await phone.recv("push"))["body"]
    assert b["link"]["url"] == ("https://www.google.com/maps/dir/?api=1&destination=Kunsthaus%20Z%C3%BCrich"
                                "&travelmode=transit")
    assert b["link"]["label"] == "Directions" and b["title"] == "📍 Kunsthaus Zürich" and b["level"] == "timed"
    assert not log.exists(), "delivered to the app: no Telegram copy"


@pytestmark_cli
async def test_cli_falls_back_to_a_telegram_url_button(env, phone, tmp_path):
    await phone.connect()                      # old app: no link cap
    tg, log = tg_stub(tmp_path)
    rc, out, err = await run(env, "link", "google.navigation:q=Bern+HB&mode=w", "--title", "Walk to the station",
                             "--json", tg=tg)
    assert rc == 0, err
    j = json.loads(out)
    assert j["route"] == "telegram" and j["app"] == "unsupported"
    text, rows = log.read_text().splitlines()[:2]
    assert text == "Walk to the station"
    assert json.loads(rows) == [[{"text": "Open in Maps", "url": "https://www.google.com/maps/dir/?api=1&destination="
                                                                 "Bern%20HB&travelmode=walking&dir_action=navigate"}]]


@pytestmark_cli
async def test_cli_offline_cancels_the_queued_copy(env, phone, tmp_path):
    await phone.connect(caps=LINK_CAPS)
    await asyncio.sleep(0.3)                  # the hello (caps) is stored
    await phone.close()
    tg, log = tg_stub(tmp_path)
    rc, out, err = await run(env, "link", "https://app.sbbmobile.ch/timetable?from=A&to=B&time=1792513200",
                             "--title", "Train home", "--wait", "2", "--json", tg=tg)
    assert rc == 0, err
    j = json.loads(out)
    assert j["route"] == "telegram" and j["app"] in ("queued", "timeout")
    assert json.loads(log.read_text().splitlines()[1])[0][0]["text"] == "Open"
    assert env.gw.db.one("SELECT COUNT(*) AS n FROM outbox WHERE t='push.cancel'")["n"] >= 1


@pytestmark_cli
async def test_cli_refuses_unsafe_and_never_hits_real_telegram_in_tests(env, phone, tmp_path):
    tg, log = tg_stub(tmp_path)
    rc, _, err = await run(env, "link", "javascript:alert(1)", "--title", "x", tg=tg)
    assert rc == 6 and "unsafe" in err and not log.exists()
    rc, _, err = await run(env, "link", "https://x.ch/", "--maps", "Bern")
    assert rc == 2
    rc, out, err = await run(env, "link", "--maps", "Bern", "--wait", "1")   # no phone connected, no stub
    assert rc == 4 and "skipped (test gateway" in err
    rc, out, err = await run(env, "link", "--maps", "Bern", "--no-telegram", "--wait", "1", tg=tg)
    assert rc == 4 and not log.exists()
