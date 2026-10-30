import asyncio
import os
import time

from voice_relay.config import Config
from voice_relay.fakes import FakeGemini, FakePluginInbox, _serve
from voice_relay.server import Relay
from voice_relay.session import FrontEnd

TOKEN = "t0k3n-for-tests"


async def until(pred, timeout=5.0, step=0.01):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(step)
    return False


class RecFE(FrontEnd):
    def __init__(self, in_rate=16000, out_rate=24000):
        self.in_rate, self.out_rate = in_rate, out_rate
        self.audio, self.events, self.flushes, self.hung = bytearray(), [], 0, None

    async def play(self, pcm):
        self.audio += pcm

    async def flush(self):
        self.flushes += 1

    async def event(self, ev):
        self.events.append(ev)

    async def hangup(self, reason):
        self.hung = reason

    def said(self):
        return "".join(e["text"] for e in self.events if e["type"] == "transcript" and e["who"] == "assistant")


class Rig:
    """FakeGemini + Relay (internal API on a random port) + FakePluginInbox (the session hand-off).
    The inbound pick-up line (greet_inbound) is off here unless a test turns it on: other tests count the audio the
    model sends; test_inbound_greet.py covers the line and its interplay."""
    @classmethod
    async def make(cls, tmp, script=None, delay=0.05, slow_delay=1.0, key="fake-key", models=None, **over):
        over.setdefault("greet_inbound", False)
        over.setdefault("greet_tts_models", ())
        r = cls()
        r.gem = FakeGemini(script=script, models=models)
        gport = await r.gem.start()
        keyf, tokf = os.path.join(tmp, "gkey"), os.path.join(tmp, "tok")
        for path, val in ((keyf, key), (tokf, TOKEN)):
            with open(path, "w") as fh:
                fh.write(val + "\n")
            os.chmod(path, 0o600)
        r.cfg = Config(gemini_ws_url="ws://127.0.0.1:%d/ws" % gport, gemini_key_file=keyf, local_token_file=tokf,
                       state_dir=os.path.join(tmp, "state"), inbox_dir=os.path.join(tmp, "inbox"),
                       files_dir=os.path.join(tmp, "inbox", "files"),
                       secure_cookie=False, public_port=0, internal_port=0, **over)
        r.relay = Relay(r.cfg)
        r.irunner, r.iport = await _serve(r.relay.internal_app())
        r.inbox = FakePluginInbox(TOKEN, "http://127.0.0.1:%d" % r.iport, delay=delay, slow_delay=slow_delay)
        r.cfg.plugin_url = "http://127.0.0.1:%d/voice" % await r.inbox.start()
        return r

    async def close(self):
        await self.gem.stop()
        await self.inbox.stop()
        await self.irunner.cleanup()
        await self.relay.handoff.close()


class StubProvider:
    name, mode, input_rate, output_rate = "stub", "ptt", 16000, 24000

    def __init__(self):
        self.results, self.injects, self.q = [], [], asyncio.Queue()

    async def connect(self):
        pass

    async def events(self):
        while True:
            ev = await self.q.get()
            yield ev
            if ev["type"] == "closed":
                return

    async def tool_result(self, cid, name, res, scheduling=None):
        self.results.append((cid, name, res))
        self.scheduling = getattr(self, "scheduling", []) + [scheduling]

    async def prompt(self, text):
        self.prompts = getattr(self, "prompts", []) + [text]

    async def inject(self, text):
        self.injects.append(text)

    async def close(self):
        await self.q.put({"type": "closed"})
