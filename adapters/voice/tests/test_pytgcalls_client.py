"""The real call client (PyTgCallsClient) against the installed py-tgcalls 3 / ntgcalls 3 API, with a fake
pyrogram Client and a fake PyTgCalls core; updates go through py-tgcalls' real filters and update types."""
import asyncio
import inspect
import json
import os

import pytest
from pytgcalls import PyTgCalls, filters as fl
from pytgcalls.types import ChatUpdate, Device, Direction, Frame, MediaStream, RecordStream, StreamFrames
from pytgcalls.types.raw import AudioParameters

from util import Rig, until
from voice_relay.audio import tone
from voice_relay.tgcall import BridgeRefused, CallBridge, PyTgCallsClient, load_userbot_config

OWNER, ME, STRANGER = 111222333, 4242, 999
API_HASH = "".join(['0123', '4567', '89ab', 'cdef', '0123', '4567', '89ab', 'cdef'])


def run(coro):
    return asyncio.run(coro)


class Me:
    def __init__(self, uid=ME, is_bot=False):
        self.id, self.is_bot = uid, is_bot


class FakeApp:
    """Stands in for pyrogram.Client: connect() returns whether the session is logged in."""
    made = []

    def __init__(self, name, api_id, api_hash, workdir, authorized=True, me=None):
        self.name, self.api_id, self.api_hash, self.workdir = name, api_id, api_hash, workdir
        self.authorized, self.me, self.is_connected, self.calls = authorized, me or Me(), False, []
        FakeApp.made.append(self)

    async def connect(self):
        self.calls.append("connect")
        return self.authorized

    async def disconnect(self):
        self.calls.append("disconnect")

    async def get_me(self):
        return self.me

    async def stop(self):
        self.calls.append("stop")
        self.is_connected = False


class FakeCalls:
    """Stands in for pytgcalls.PyTgCalls; dispatch() runs the real filters like py-tgcalls' _propagate."""

    def __init__(self, app):
        self.app, self.handlers, self.log = app, [], []
        self.leave_error = None

    def on_update(self, filters=None):
        def deco(func):
            self.handlers.append((filters, func))
            return func
        return deco

    async def start(self):
        self.log.append("start")
        self.app.is_connected = True

    async def dispatch(self, update):
        for flt, func in self.handlers:
            if flt is None or await flt(self, update):
                await func(self, update)

    async def play(self, chat_id, stream=None, config=None):
        self.log.append(("play", chat_id, stream))

    async def record(self, chat_id, stream=None, config=None):
        self.log.append(("record", chat_id, stream))

    async def leave_call(self, chat_id, close=False):
        self.log.append(("leave", chat_id))
        if self.leave_error:
            raise self.leave_error

    async def send_frame(self, chat_id, device, data, frame_data=None):
        self.log.append(("frame", chat_id, device, len(data)))


def app_factory(**kw):
    def make(*a, **k):
        return FakeApp(*a, **{**k, **kw})
    return make


@pytest.fixture
def conf(tmp_path):
    FakeApp.made.clear()
    sf = tmp_path / "voice-tg.session"
    sf.write_bytes(b"sqlite")
    sf.chmod(0o600)
    return {"api_id": 7654321, "api_hash": API_HASH, "session_file": str(sf)}


def client(conf, **kw):
    return PyTgCallsClient(conf, client_cls=app_factory(**kw), calls_cls=FakeCalls)


def frames_update(chat_id, direction=Direction.INCOMING, device=Device.MICROPHONE, n=2):
    return StreamFrames(chat_id, direction, device, [Frame(1, b"\x01\x00" * 480, Frame.Info()) for _ in range(n)])


def test_installed_api_matches_what_the_client_calls():
    import ntgcalls
    import pytgcalls
    assert pytgcalls.__version__.startswith("3.") and ntgcalls.__version__.startswith("3.")
    for m in ("play", "record", "leave_call", "send_frame", "on_update", "start"):
        assert callable(getattr(PyTgCalls, m)), m
    assert list(inspect.signature(PyTgCalls.send_frame).parameters)[1:4] == ["chat_id", "device", "data"]
    assert {"INCOMING_CALL", "DISCARDED_CALL", "BUSY_CALL"} <= set(ChatUpdate.Status.__members__)
    ms = MediaStream(__import__("pytgcalls.types", fromlist=["ExternalMedia"]).ExternalMedia.AUDIO,
                     AudioParameters(48000, 1), video_flags=MediaStream.Flags.IGNORE)
    assert (ms.microphone.parameters.bitrate, ms.microphone.parameters.channels) == (48000, 1) and ms.camera is None
    rs = RecordStream(True, AudioParameters(48000, 1))
    assert rs.microphone.parameters.channels == 1 and rs.speaker is None


def test_real_pytgcalls_wraps_a_kurigram_client_offline(tmp_path):
    from pyrogram import Client
    app = Client("probe", api_id=1, api_hash="x" * 32, workdir=str(tmp_path))
    PyTgCalls(app)  # constructs (environment/version checks pass) without touching the network
    assert not os.listdir(tmp_path)


def test_start_refuses_missing_open_or_logged_out_sessions(conf):
    os.chmod(conf["session_file"], 0o644)
    with pytest.raises(BridgeRefused, match="0600"):
        run(client(conf).start())
    os.remove(conf["session_file"])
    with pytest.raises(BridgeRefused, match="no session file"):
        run(client(conf).start())
    open(conf["session_file"], "w").close()
    os.chmod(conf["session_file"], 0o600)
    c = client(conf, authorized=False)
    with pytest.raises(BridgeRefused, match="not logged in"):
        run(c.start())
    assert "start" not in c.tg.log  # pyrogram's interactive login is never reached


def test_client_routes_calls_frames_and_hangups(conf):
    async def main():
        c = client(conf)
        seen = {"in": [], "fr": [], "end": []}

        async def on_in(cid, caller):
            seen["in"].append((cid, caller))

        async def on_fr(cid, pcm):
            seen["fr"].append((cid, len(pcm)))

        async def on_end(cid):
            seen["end"].append(cid)

        c.on_incoming(on_in), c.on_frames(on_fr), c.on_ended(on_end)
        me = await c.start()
        app = FakeApp.made[-1]
        assert me == {"id": ME, "is_bot": False}
        assert (app.name, app.workdir, app.api_hash) == ("voice-tg", os.path.dirname(conf["session_file"]), API_HASH)
        await c.tg.dispatch(ChatUpdate(OWNER, ChatUpdate.Status.INCOMING_CALL))
        await c.tg.dispatch(ChatUpdate(-100123, ChatUpdate.Status.INCOMING_CALL))      # group invite: ignored
        await c.tg.dispatch(frames_update(OWNER))
        await c.tg.dispatch(frames_update(OWNER, direction=Direction.OUTGOING))        # our own audio: ignored
        await c.tg.dispatch(ChatUpdate(OWNER, ChatUpdate.Status.DISCARDED_CALL))
        assert seen == {"in": [(OWNER, OWNER)], "fr": [(OWNER, 960), (OWNER, 960)], "end": [OWNER]}
        await c.accept(OWNER)
        (_, pc, ms), (_, rc, rs) = c.tg.log[-2:]
        assert pc == rc == OWNER
        assert (ms.microphone.parameters.bitrate, ms.microphone.parameters.channels) == (48000, 1)
        assert rs.microphone.parameters.channels == 1
        await c.send_frame(OWNER, b"\0" * 960)
        assert c.tg.log[-1] == ("frame", OWNER, Device.MICROPHONE, 960)
        c.tg.leave_error = RuntimeError("not in call")
        await c.hangup(OWNER)                                                            # swallowed
        await c.stop()
        assert "stop" in app.calls
    run(main())


def test_bridge_on_the_real_client_declines_strangers_and_serves_the_owner(tmp_path, conf):
    async def main():
        r = await Rig.make(str(tmp_path), script=["hello"])
        try:
            c = client(conf)
            b = CallBridge(r.cfg, c, OWNER, r.relay.handoff, lambda: "be brief", r.relay.provider_factory)
            await b.start()
            await c.tg.dispatch(ChatUpdate(STRANGER, ChatUpdate.Status.INCOMING_CALL))
            await c.tg.dispatch(frames_update(STRANGER))
            assert ("leave", STRANGER) in c.tg.log and not any(e[0] == "play" for e in c.tg.log if type(e) is tuple)
            assert b.rejected == 1 and r.gem.log["audio_bytes"] == 0
            await c.tg.dispatch(ChatUpdate(OWNER, ChatUpdate.Status.INCOMING_CALL))
            assert ("play", OWNER) == c.tg.log[-2][:2] and ("record", OWNER) == c.tg.log[-1][:2]
            assert await until(lambda: r.gem.log["setups"])
            await c.tg.dispatch(StreamFrames(OWNER, Direction.INCOMING, Device.MICROPHONE,
                                             [Frame(1, tone(48000, 300), Frame.Info())]))
            assert await until(lambda: r.gem.log["audio_bytes"] > 0)
            await c.tg.dispatch(ChatUpdate(OWNER, ChatUpdate.Status.DISCARDED_CALL))
            assert await until(lambda: not b.calls)
        finally:
            await r.close()
    run(main())


def test_bridge_refuses_bot_and_owner_sessions(conf):
    async def main(me):
        c = PyTgCallsClient(conf, client_cls=app_factory(me=me), calls_cls=FakeCalls)
        b = CallBridge(None, c, OWNER, None, None, None)
        with pytest.raises(BridgeRefused):
            await b.start()
    run(main(Me(ME, is_bot=True)))
    run(main(Me(OWNER)))


def test_config_defaults_session_file_to_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    sec = tmp_path / ".secrets"
    sec.mkdir()
    p = sec / "voice-tg.json"
    p.write_text(json.dumps({"api_id": 1, "api_hash": API_HASH, "phone": "+41000000000"}))
    p.chmod(0o600)
    assert load_userbot_config(str(p))["session_file"] == os.path.join(os.path.realpath(str(sec)), "voice-tg.session")


def test_serve_bridge_is_opt_in_and_refusals_keep_the_relay_up(tmp_path, conf, capsys):
    async def main():
        r = await Rig.make(str(tmp_path), tg_owner_id=OWNER, tg_config_file=str(tmp_path / "missing.json"))
        try:
            bridge, state = await r.relay.start_call_bridge()
            assert bridge is None and state == "failed: FileNotFoundError"
            bridge, state = await r.relay.start_call_bridge(client(conf, authorized=False))
            assert bridge is None and state.startswith("refused: the session is not logged in")
            bridge, state = await r.relay.start_call_bridge(client(conf))
            assert state == "on" and bridge.owner_id == OWNER
        finally:
            await r.close()
    run(main())
    out, err = capsys.readouterr()
    assert API_HASH not in out + err


class TelegramServerError(Exception):
    """Same name as ntgcalls' error: raised by py-tgcalls when the media never connects after the accept."""


class TimedOutAnswer(Exception):
    pass


class NotInCallError(Exception):
    pass


class FakeBridgeApp:
    """Stands in for py-tgcalls' mtproto bridge (PyTgCalls._app): confirm_call after the accept, discard_call."""

    def __init__(self):
        self.log = []

    async def confirm_call(self, chat_id, *a):
        self.log.append(("confirm", chat_id))
        return "protocol"

    async def discard_call(self, chat_id, is_missed):
        self.log.append(("discard", chat_id, is_missed))


class AcceptingCalls(FakeCalls):
    """play() on an outgoing call: he accepts (confirm_call), then the media connects or fails with `outcome`."""

    def __init__(self, app):
        super().__init__(app)
        self._app = FakeBridgeApp()
        self.outcome, self.accept = None, True

    async def play(self, chat_id, stream=None, config=None):
        self.log.append(("play", chat_id, stream))
        if self.accept:
            await self._app.confirm_call(chat_id, b"g_b", 1, None)
            await asyncio.sleep(0.05)   # "Connecting..."
        if self.outcome:
            raise self.outcome


def dial_client(conf, outcome=None, accept=True, leave_error=None):
    c = PyTgCallsClient(conf, client_cls=app_factory(), calls_cls=AcceptingCalls)
    c._make()
    c.callee = 4242
    c.tg.outcome, c.tg.accept, c.tg.leave_error = outcome, accept, leave_error
    return c


def test_dial_connect_failure_after_accept_hangs_up_and_discards(conf):
    c = dial_client(conf, outcome=TelegramServerError(), leave_error=NotInCallError())
    with pytest.raises(TelegramServerError):
        run(c.dial(4242, 45))
    assert ("leave", 4242) in c.tg.log                       # tried the normal hang-up first
    assert ("discard", 4242, False) in c.tg._app.log         # then phone.discardCall: no dead call on his phone
    assert c.last_connect["connected"] is False and c.last_connect["accept_to_connect_s"] >= 0.0


def test_dial_connected_records_accept_to_connected_time(conf):
    c = dial_client(conf)
    run(c.dial(4242, 45))
    assert c.last_connect["connected"] is True and 0.0 <= c.last_connect["accept_to_connect_s"] < 5
    assert ("confirm", 4242) in c.tg._app.log and not [e for e in c.tg._app.log if e[0] == "discard"]
    assert not [e for e in c.tg.log if e[0] == "leave"] and c.accepted_at == {}


def test_dial_no_answer_leaves_hangup_to_py_tgcalls(conf):
    c = dial_client(conf, outcome=TimedOutAnswer(), accept=False)
    with pytest.raises(TimedOutAnswer):
        run(c.dial(4242, 45))
    assert not [e for e in c.tg.log if e[0] == "leave"] and c.tg._app.log == []
    assert c.last_connect == {"connected": False, "accept_to_connect_s": None}


def test_leave_succeeds_without_extra_discard(conf):
    c = dial_client(conf)
    run(c.hangup(4242))
    assert ("leave", 4242) in c.tg.log and c.tg._app.log == []


def test_ring_log_carries_accept_to_connect_time():
    class Stub:
        last_connect = {"connected": False, "accept_to_connect_s": 10.2}
    b = CallBridge.__new__(CallBridge)
    b.client = Stub()
    assert b._connect_fields() == {"accept_to_connect_s": 10.2}
    Stub.last_connect = None
    assert b._connect_fields() == {}
