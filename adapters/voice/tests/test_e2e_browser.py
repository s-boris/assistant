"""End to end: headless Chrome (fake mic) → phone page → relay → Gemini fake → ask_assistant → session fake →
answer filter → spoken back to the page."""
import json
import os
import select
import shutil
import socket
import subprocess
import sys
import time

import pytest

sync_api = pytest.importorskip("playwright.sync_api")
CHROME = "/usr/bin/google-chrome"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHOT = os.environ.get("VOICE_E2E_SHOT", "")
pytestmark = pytest.mark.skipif(not os.path.exists(CHROME), reason="no Chrome")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def relay():
    port, iport = free_port(), free_port()
    p = subprocess.Popen([os.path.join(ROOT, "bin", "voice-relay"), "serve", "--fake", "--port", str(port),
                          "--internal-port", str(iport), "--sync-wait", "1.5", "--fake-slow-delay", "2.5"],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         env=dict(os.environ, VOICE_PUBLIC_HOST="127.0.0.1"))
    ready, _, _ = select.select([p.stdout], [], [], 20)
    assert ready, "relay did not start"
    info = json.loads(p.stdout.readline().decode())
    yield port, info
    p.terminate()
    p.wait(5)
    shutil.rmtree(info["tmp"], ignore_errors=True)


def test_phone_page_push_to_talk_and_typed_slow_task(relay):
    """Runs with the page's real CSP: only locator APIs (isolated world), and no CSP violations allowed."""
    port, info = relay
    base = "http://127.0.0.1:%d" % port
    with sync_api.sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=CHROME, headless=True, args=[
            "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
            "--autoplay-policy=no-user-gesture-required"])
        try:
            ctx = b.new_context(permissions=["microphone"], viewport={"width": 390, "height": 844})
            page = ctx.new_page()
            problems = []
            page.on("console", lambda m: problems.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: problems.append(str(e)))
            page.goto(base + info["login_path"])
            assert page.url.rstrip("/") == base
            page.click("#conn")
            sync_api.expect(page.locator("#state")).to_have_text("Live", timeout=10000)
            box = page.locator("#talk").bounding_box()
            page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            page.mouse.down()
            page.wait_for_timeout(1200)
            t0 = time.monotonic()
            page.mouse.up()
            page.locator(".msg.bot", has_text="Fake answer").first.wait_for(timeout=10000)
            release_to_text = (time.monotonic() - t0) * 1000
            bots = page.locator(".msg.bot").all_text_contents()
            assert any("(phone withheld)" in t for t in bots) and not any("079" in t for t in bots)
            page.fill("#typed", "slow: check the backups")
            page.press("#typed", "Enter")
            page.wait_for_selector("#working:not([hidden])", timeout=5000)
            page.locator(".msg.bot", has_text="Fake answer about: slow").first.wait_for(timeout=10000)
            page.wait_for_selector("#working", state="hidden", timeout=5000)
            assert page.locator(".msg.you").count() >= 2
            if SHOT:
                page.screenshot(path=SHOT)
            page.click("#conn")
            sync_api.expect(page.locator("#state")).to_have_text("Not connected", timeout=5000)
            assert not [x for x in problems if "Content Security Policy" in x or "Error" in x], problems
            print("LATENCY browser PTT release -> answer text on page (Playwright-observed): %.0f ms"
                  % release_to_text, file=sys.stderr)
        finally:
            b.close()
