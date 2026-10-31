"""The browser service: the CDP front and its commit gate, against a fake Chrome (an in-process CDP server) and,
when Chrome is installed, a throwaway headless Chrome on a free port with a temp profile (never the live :9222).
The web agent's tasks and the hub token are fakes here; HubTokens runs against a fake approvals library."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "packages"))
import browsersvc as B  # noqa: E402

from core import http  # noqa: E402

BTN = {10: ("button", "Buy now"), 20: ("button", "Search"), 30: ("button", "Submit order"), 40: ("button", "Send"),
       50: ("checkbox", "I agree to the terms"), 60: ("textbox", "Your message")}  # fmt: skip


def get_json(url: str) -> dict:
    return http.get_json(url, timeout=5)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeChrome:
    """A CDP endpoint: answers the service's DOM/Accessibility queries from BTN (x = backendNodeId), records the
    client commands it gets, answers each with an empty result (echoing sessionId)."""

    def __init__(self, focus: int = 60) -> None:
        self.got: list[dict] = []
        self.focus = focus
        self.port = free_port()

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", self.port, limit=2**20)
        return self

    async def handle(self, r, w):
        line, hdr = await B.read_head(r)
        if hdr.get("upgrade", "").lower() != "websocket":
            body = json.dumps({"Browser": "Fake/1", "webSocketDebuggerUrl":
                               f"ws://127.0.0.1:{self.port}/devtools/browser/fake"}).encode()  # fmt: skip
            w.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n" % len(body) + body
            )
            await w.drain()
            w.close()
            return
        w.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                 f"Sec-WebSocket-Accept: {B.accept_key(hdr['sec-websocket-key'])}\r\n\r\n").encode())  # fmt: skip
        while True:
            try:
                op, data = await B.read_message(r)
            except EOFError:
                return
            if op == 8:
                return
            m = json.loads(data)
            meth, p = m.get("method"), m.get("params") or {}
            res: dict = {}
            if meth == "DOM.getNodeForLocation":
                res = {"backendNodeId": int(p["x"])} if int(p["x"]) in BTN else {}
            elif meth == "Accessibility.getPartialAXTree":
                role, name = BTN[p["backendNodeId"]]
                res = {"nodes": [{"nodeId": "2", "backendDOMNodeId": p["backendNodeId"], "parentId": "1",
                                  "role": {"value": "StaticText"}, "name": {"value": name}},
                                 {"nodeId": "1", "role": {"value": role}, "name": {"value": name}}]}  # fmt: skip
            elif meth == "Runtime.evaluate":
                res = {"result": {"objectId": "o1"}}
            elif meth == "DOM.describeNode":
                res = {"node": {"backendNodeId": self.focus}}
            elif meth == "Runtime.releaseObject":
                pass
            elif meth == "Big.echo":
                res = {"blob": "x" * 3_000_000}
                self.got.append(m)
            else:
                self.got.append(m)
            out = {"id": m["id"], "result": res}
            if m.get("sessionId"):
                out["sessionId"] = m["sessionId"]
            w.write(B.frame(1, json.dumps(out).encode(), mask=False))
            await w.drain()


class FakeTasks:
    def __init__(self, active=(), approved=None):
        self._active, self.approved = list(active), approved

    def active(self):
        return self._active

    def classify(self, text):
        t = text.lower()
        if "search" in t:
            return {"commit": False, "tier": None, "category": None, "exempt": "search"}
        for w, tier, cat in (("buy", "hard", "pay"), ("order", "hard", "pay"), ("agree", "soft", "confirm"),
                             ("submit", "soft", "send"), ("send", "soft", "send")):  # fmt: skip
            if w in t:
                return {"commit": True, "tier": tier, "category": cat, "exempt": None}
        return {"commit": False, "tier": None, "category": None, "exempt": None}

    def search(self, text):
        return "search" in text.lower()

    def checkbox(self, text):
        return "checkbox" in text.lower()

    def auto_soft(self, c, active):
        return c.get("tier") == "soft" and bool(active) and all(a.get("kind") == "order" for a in active)

    def match(self, text, role=""):
        a = self.approved
        return a if a and a["label"].lower() == text.lower() and role not in ("checkbox", "radio") else None

    def action(self, t):
        return "action:" + t["id"]

    def dry_ok(self, t):
        return False


class FakeTokens:
    def __init__(self):
        self.used: set[str] = set()

    def take(self, t, tasks):
        if t["id"] in self.used:
            return False, "token used already"
        self.used.add(t["id"])
        return True, "ok"


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setenv("BROWSERSVC_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("BROWSERSVC_SWITCH", str(tmp_path / "switch"))


async def setup(gate_mode="enforce", tasks=None, tokens=None, notes=None):
    chrome = await FakeChrome().start()
    gate = B.Gate(tasks or FakeTasks(), tokens or FakeTokens(), mode_fn=lambda: gate_mode,
                  notify=(lambda t, op: notes.append((t["id"], op))) if notes is not None else None)  # fmt: skip
    svc = B.Service(gate, listen=free_port(), up_addr=("127.0.0.1", chrome.port))
    await svc.start()
    r, w = await B.ws_connect("127.0.0.1", svc.listen, "/devtools/browser/fake")
    return chrome, svc, r, w


async def call(r, w, msg) -> dict:
    w.write(B.frame(1, json.dumps(msg).encode(), mask=True))
    await w.drain()
    op, data = await B.read_message(r)
    return json.loads(data)


def click(i, x, sid="S1", typ="mousePressed"):
    return {"id": i, "method": "Input.dispatchMouseEvent", "sessionId": sid,
            "params": {"type": typ, "x": x, "y": 5, "button": "left", "clickCount": 1}}  # fmt: skip


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 30))


def test_switch_modes(tmp_path):
    assert B.mode() == "off"
    B.set_mode("shadow")
    assert B.mode() == "shadow"
    B.set_mode("enforce")
    assert B.mode() == "enforce"
    Path(os.environ["BROWSERSVC_SWITCH"]).write_text("bogus\n")
    assert B.mode() == "off"
    B.set_mode("off")
    assert not Path(os.environ["BROWSERSVC_SWITCH"]).exists()
    with pytest.raises(ValueError):
        B.set_mode("loud")


def test_http_rewrite_and_passthrough_of_big_messages():
    async def go():
        chrome, svc, r, w = await setup()
        v = await asyncio.to_thread(get_json, f"http://127.0.0.1:{svc.listen}/json/version")
        assert v["webSocketDebuggerUrl"] == f"ws://127.0.0.1:{svc.listen}/devtools/browser/fake"
        got = await call(r, w, {"id": 1, "method": "Big.echo", "params": {"pad": "y" * 2_000_000}})
        assert got["id"] == 1 and len(got["result"]["blob"]) == 3_000_000
        got = await call(r, w, {"id": 2, "method": "Page.navigate", "params": {"url": "about:blank"}})
        assert got == {"id": 2, "result": {}}
        assert [m["method"] for m in chrome.got] == ["Big.echo", "Page.navigate"]
        w.close()

    run(go())


def test_enforce_denies_a_hard_commit_without_task_and_its_release():
    async def go():
        chrome, svc, r, w = await setup()
        got = await call(r, w, click(1, 10))
        assert got["error"]["code"] == -32000 and "no approved web task" in got["error"]["message"]
        assert got["sessionId"] == "S1"
        got = await call(r, w, click(2, 10, typ="mouseReleased"))
        assert "error" in got
        assert not [m for m in chrome.got if m["method"] == "Input.dispatchMouseEvent"]  # nothing reached Chrome
        got = await call(r, w, click(3, 20))  # "Search": exempt
        assert got == {"id": 3, "sessionId": "S1", "result": {}}
        got = await call(r, w, click(4, 40))  # "Send": soft, no task -> allowed
        assert "result" in got
        got = await call(r, w, click(5, 99))  # nothing under the point: unresolved -> backstop's job
        assert "result" in got
        w.close()
        recs = B.records()
        assert [x["decision"] for x in recs] == ["deny", "allow", "allow", "allow"]
        assert recs[-1]["why"] == "unresolved" and all("label" not in x for x in recs)

    run(go())


def test_token_once_per_approval_and_workflow_notified():
    async def go():
        notes: list = []
        task = {"id": "wa0001", "label": "Submit order", "kind": "order"}
        tasks = FakeTasks(active=[task], approved=task)
        chrome, svc, r, w = await setup(tasks=tasks, notes=notes)
        got = await call(r, w, click(1, 30))
        assert "result" in got
        await call(r, w, click(2, 30, typ="mouseReleased"))
        got = await call(r, w, click(3, 30))  # the same approval again: the token is used
        assert "token used already" in got["error"]["message"]
        got = await call(r, w, click(4, 10))  # another hard action: no approved task matches
        assert "error" in got
        w.close()
        await asyncio.sleep(0.2)
        assert notes == [("wa0001", "commit")]
        a = B.audit()
        assert a["with_token"] == 1 and a["unguarded"] == 0 and a["denied"] == 2

    run(go())


def test_tasks_running_block_soft_steps_boxes_and_enter_in_text_fields():
    async def go():
        running = {"id": "wa0002", "label": "x", "kind": "form"}
        chrome, svc, r, w = await setup(tasks=FakeTasks(active=[running]))
        assert "error" in await call(r, w, click(1, 40))  # soft "Send" during a form task: needs a card
        assert "terms box" in (await call(r, w, click(2, 50)))["error"]["message"]
        enter = {"id": 3, "method": "Input.dispatchKeyEvent", "params": {"type": "keyDown", "key": "Enter"}}
        assert "enter-submit" in (await call(r, w, enter))["error"]["message"]
        char = {"id": 4, "method": "Input.dispatchKeyEvent", "params": {"type": "char", "text": "\r"}}
        assert "error" in await call(r, w, char)
        up = {"id": 5, "method": "Input.dispatchKeyEvent", "params": {"type": "keyUp", "key": "Enter"}}
        assert "error" in await call(r, w, up)
        typing = {"id": 6, "method": "Input.dispatchKeyEvent", "params": {"type": "keyDown", "key": "a", "text": "a"}}
        assert "result" in await call(r, w, typing)
        w.close()

    run(go())

    async def order():
        running = {"id": "wa0003", "label": "x", "kind": "order"}
        chrome, svc, r, w = await setup(tasks=FakeTasks(active=[running]))
        assert "result" in await call(r, w, click(1, 40))  # a booking/order task's intermediate soft step
        w.close()

    run(order())


def test_shadow_forwards_and_logs_would_deny():
    async def go():
        chrome, svc, r, w = await setup(gate_mode="shadow")
        got = await call(r, w, click(1, 10))
        assert "result" in got
        await call(r, w, click(2, 10, typ="mouseReleased"))
        assert len([m for m in chrome.got if m["method"] == "Input.dispatchMouseEvent"]) == 2
        w.close()
        assert B.records()[-1]["decision"] == "would-deny" and B.audit()["would_deny"] == 1

    run(go())


class FakeA:
    def __init__(self, row):
        self.row = row

    def get(self, n):
        if self.row == "down":
            raise RuntimeError("hub unreachable")
        return self.row

    @staticmethod
    def state(row):
        return "used" if row.get("used_ts") else row.get("status", "pending")

    @staticmethod
    def action_sha(a):
        import hashlib

        return hashlib.sha256(a.encode()).hexdigest()


class Res:
    def __init__(self, id_, dup=False):
        self.id, self.dup = id_, dup


def test_hub_tokens_verify_his_used_approval_and_claim_once():
    import hashlib

    now = 1_000_000.0
    tasks = FakeTasks()
    t = {"id": "wa0009", "mode": "live", "stops": 1, "approval": {"nonce": "ab" * 16}}
    good = {"used_ts": now - 30, "used_by": "webagent:wa0009",
            "action_sha": hashlib.sha256(b"action:wa0009").hexdigest()}  # fmt: skip
    seen: list = []

    def emit(**kw):
        seen.append(kw)
        return Res(7, dup=len(seen) > 1)

    tok = B.HubTokens(FakeA(good), emit, clock=lambda: now)
    assert tok.take(t, tasks) == (True, "ok")
    assert seen[0]["idem_key"] == "commit:" + "ab" * 16 and seen[0]["delivery"] == "mute" and not seen[0]["note"]
    assert tok.take(t, tasks) == (False, "token used already")
    cases = [
        ({**good, "used_by": "webagent:other"}, "not used for this task"),
        ({**good, "used_ts": None, "status": "approved"}, "not used for this task"),
        ({**good, "action_sha": "0" * 64}, "another action"),
        ({**good, "used_ts": now - 3600}, "too old"),
        ("down", "unverified"),
        (None, "no such approval"),
    ]
    for row, why in cases:
        ok, got = B.HubTokens(FakeA(row), lambda **k: Res(1), clock=lambda: now).take(t, tasks)
        assert not ok and why in got, (row, got)
    ok, got = B.HubTokens(FakeA(good), lambda **k: Res(None), clock=lambda: now).take(t, tasks)
    assert not ok and "spool" in got  # a receipt the hub did not record is no token
    ok, got = B.HubTokens(FakeA(good), emit, clock=lambda: now).take({"id": "wa1", "approval": {}}, tasks)
    assert not ok and "no hub approval token" in got
    ok, got = B.HubTokens(FakeA(good), emit, clock=lambda: now).take({"id": "wa1", "mode": "dry"}, tasks)
    assert not ok and "dry" in got


CHROME = shutil.which("google-chrome-stable") or shutil.which("chromium")
# no network beyond loopback: CI's netguard fails a run on any outside connection attempt
QUIET = ["--no-sandbox", "--disable-dev-shm-usage", "--no-first-run", "--no-default-browser-check",
         "--disable-background-networking", "--disable-component-update", "--disable-sync", "--no-pings",
         "--disable-domain-reliability", "--disable-breakpad", "--metrics-recording-only", "--disable-crash-reporter",
         "--disable-features=MediaRouter,DialMediaRouteProvider,OptimizationHints,Translate,AutofillServerCommunication",
         "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE localhost", "--proxy-server=127.0.0.1:9",
         "--proxy-bypass-list=<-loopback>"]  # fmt: skip
PAGE = (
    "data:text/html,<button id=buy onclick=\"window.hit='buy'\" style='position:absolute;left:20px;top:20px;"
    "width:200px;height:60px'>Buy now</button><button id=find onclick=\"window.hit='find'\" style='position:"
    "absolute;left:20px;top:120px;width:200px;height:60px'>Search</button>"
)


@pytest.mark.skipif(not CHROME, reason="no Chrome")
@pytest.mark.skipif(
    "netguard" in os.environ.get("LD_PRELOAD", ""),
    reason="Chrome's network service probes IPv6 reachability, which CI's netguard counts as a block",
)
def test_real_headless_chrome_resolves_and_gates(tmp_path):
    cdp = free_port()
    assert cdp != 9222
    prof = tmp_path / "profile"
    proc = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={cdp}", f"--user-data-dir={prof}",
                             *QUIET, "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # fmt: skip
    try:
        for _ in range(100):
            try:
                http.get_json(f"http://127.0.0.1:{cdp}/json/version", timeout=1)
                break
            except Exception:
                time.sleep(0.1)

        async def go():
            gate = B.Gate(FakeTasks(), FakeTokens(), mode_fn=lambda: "enforce")
            svc = B.Service(gate, listen=free_port(), up_addr=("127.0.0.1", cdp))
            await svc.start()
            wsu = (await asyncio.to_thread(get_json, f"http://127.0.0.1:{svc.listen}/json/version"))[
                "webSocketDebuggerUrl"
            ]
            assert f":{svc.listen}/" in wsu
            r, w = await B.ws_connect("127.0.0.1", svc.listen, "/" + wsu.split("/", 3)[3])
            n = iter(range(1, 1000))

            async def cmd(method, params=None, sid=None):
                i = next(n)
                m = {"id": i, "method": method, "params": params or {}}
                if sid:
                    m["sessionId"] = sid
                w.write(B.frame(1, json.dumps(m).encode(), mask=True))
                await w.drain()
                while True:
                    _, data = await B.read_message(r)
                    got = json.loads(data)
                    if got.get("id") == i:
                        return got

            tid = (await cmd("Target.createTarget", {"url": PAGE}))["result"]["targetId"]
            sid = (await cmd("Target.attachToTarget", {"targetId": tid, "flatten": True}))["result"]["sessionId"]
            for _ in range(50):
                st = await cmd("Runtime.evaluate", {"expression": "document.readyState", "returnByValue": True}, sid)
                if st["result"]["result"].get("value") == "complete":
                    break
                await asyncio.sleep(0.1)

            async def press(x, y):
                a = await cmd("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left",
                                                           "clickCount": 1}, sid)  # fmt: skip
                b = await cmd("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left",
                                                           "clickCount": 1}, sid)  # fmt: skip
                return a, b

            a, b = await press(120, 50)  # Buy now: a hard commit, no web task -> denied before Chrome
            assert "error" in a and "error" in b
            a, b = await press(120, 150)  # Search: exempt -> clicked
            assert "result" in a and "result" in b
            hit = await cmd("Runtime.evaluate", {"expression": "window.hit || ''", "returnByValue": True}, sid)
            assert hit["result"]["result"]["value"] == "find"
            w.close()
            recs = B.records()
            assert [x["decision"] for x in recs] == ["deny", "allow"] and recs[0]["tier"] == "hard"

        asyncio.run(asyncio.wait_for(go(), 60))
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
