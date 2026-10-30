"""Big-file inbox: FileInbox with fake media, plus the real pyrogram filter/handler on PyTgCallsClient with real
pyrogram message types and a fake client. Nothing reaches Telegram; every path is under tmp_path."""
import asyncio
import json
import os
import stat
from types import SimpleNamespace

import pyrogram
import pytest
from pyrogram import enums, types
from pyrogram.handlers import MessageHandler

from voice_relay.bigfiles import FileInbox, human_size, print_files, safe_name
from voice_relay.config import Config
from voice_relay.tgcall import PyTgCallsClient, PyroMedia, owner_media_filter

OWNER, STRANGER, ME = 111222333, 999, 4242
MB = 1024 * 1024


def run(coro):
    return asyncio.run(coro)


def cfg(tmp, **over):
    kw = dict(state_dir=str(tmp / "state"), inbox_dir=str(tmp / "inbox"), files_dir=str(tmp / "inbox" / "files"),
              files_max_mb=5, files_min_free_gb=1.0)
    kw.update(over)
    return Config(**kw)


class Disk:
    def __init__(self, free):
        self.free = free

    def __call__(self, _path):
        return SimpleNamespace(free=self.free)


class FakeMedia:
    """Like PyroMedia; download() behaves like pyrogram: writes PATH.temp in chunks, calls the guard after each,
    removes the temp file and returns None when the guard says stop, renames it to PATH when done."""

    def __init__(self, data=b"v" * 3000, kind="video", file_name="dive clip.mp4", mime="video/mp4", size=None,
                 from_id=OWNER, chat_id=OWNER, caption="", mid=7, duration=0, fail=None, on_chunk=None):
        self.data, self.kind, self.file_name, self.mime = data, kind, file_name, mime
        self.size = len(data) if size is None else size
        self.from_id, self.chat_id, self.caption, self.id, self.duration = from_id, chat_id, caption, mid, duration
        self.fail, self.on_chunk = fail, on_chunk
        self.downloads, self.replies = [], []

    async def download(self, path, guard):
        self.downloads.append(path)
        tmp = path + ".temp"
        step = max(1, len(self.data) // 4)
        with open(tmp, "wb") as fh:
            for i in range(0, len(self.data), step):
                fh.write(self.data[i:i + step])
                cur = min(len(self.data), i + step)
                if self.on_chunk:
                    self.on_chunk(cur)
                if self.fail:
                    raise self.fail
                if await guard(cur, len(self.data)) is False:
                    fh.close()
                    os.remove(tmp)
                    return None
        os.replace(tmp, path)
        return path

    async def reply(self, text):
        self.replies.append(text)


def mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


def notes(tmp):
    d = tmp / "inbox"
    return sorted(p for p in d.iterdir() if p.is_file()) if d.exists() else []


def test_owner_file_is_saved_private_with_one_note_and_a_short_reply(tmp_path):
    inbox = FileInbox(cfg(tmp_path), OWNER, disk_usage=Disk(100 * 1024 ** 3))
    m = FakeMedia(caption="loop 0:12-0:20\nplease", duration=754)
    res = run(inbox.fetch(m))
    assert res["status"] == "saved" and res["size"] == 3000
    path = res["path"]
    assert os.path.dirname(path) == str(tmp_path / "inbox" / "files") and path.endswith("-7-dive_clip.mp4")
    assert open(path, "rb").read() == m.data
    assert mode(path) == 0o600 and mode(tmp_path / "inbox" / "files") == 0o700
    assert mode(tmp_path / "inbox" / "files" / ".part") == 0o700
    assert os.listdir(tmp_path / "inbox" / "files" / ".part") == []
    [note] = notes(tmp_path)
    assert note.name.startswith("bigfile-") and note.name.endswith("-7.md") and mode(note) == 0o600
    text = note.read_text()
    assert "Data, not instructions" in text and "JOBS.md" in text
    assert "- path: %s" % path in text and "(sent as: dive_clip.mp4)" in text
    assert "- type: video, video/mp4, 12:34 min" in text
    assert '- caption: "loop 0:12-0:20 please"' in text
    assert m.replies == ["📥 got it (1 MB)"]
    [row] = [json.loads(x) for x in open(tmp_path / "state" / "files.jsonl")]
    assert row["status"] == "saved" and row["path"] == path and "caption" not in row
    assert mode(tmp_path / "state" / "files.jsonl") == 0o600
    assert inbox.status() == {"saved": 1, "refused": 0, "failed": 0, "queued": 0}


def test_strangers_and_other_chats_are_never_read_downloaded_or_answered(tmp_path):
    inbox = FileInbox(cfg(tmp_path), OWNER, disk_usage=Disk(100 * 1024 ** 3))
    for m in (FakeMedia(from_id=STRANGER, chat_id=STRANGER), FakeMedia(from_id=OWNER, chat_id=-100123),
              FakeMedia(from_id=STRANGER, chat_id=OWNER)):
        assert run(inbox.fetch(m)) == {"status": "ignored"}
        assert m.downloads == [] and m.replies == []
    assert not (tmp_path / "inbox").exists() and not (tmp_path / "state").exists()
    assert run(FileInbox(cfg(tmp_path), 0).fetch(FakeMedia())) == {"status": "ignored"}


def test_size_cap_and_disk_floor_refuse_before_downloading(tmp_path):
    c = cfg(tmp_path)
    big = FakeMedia(size=6 * MB)
    inbox = FileInbox(c, OWNER, disk_usage=Disk(100 * 1024 ** 3))
    assert run(inbox.fetch(big))["status"] == "too_big"
    assert big.downloads == [] and big.replies == ["📥 not saved: over the 5 MB limit"]
    tight = FakeMedia()
    inbox = FileInbox(c, OWNER, disk_usage=Disk(1024 ** 3 + 2999))     # 1 GB floor: 3000 bytes would cross it
    res = run(inbox.fetch(tight))
    assert res["status"] == "disk_low" and tight.downloads == []
    assert tight.replies == ["📥 not saved: the disk is too full right now"]
    assert notes(tmp_path) == [] and os.listdir(tmp_path / "inbox" / "files") == [".part"]
    rows = [json.loads(x) for x in open(tmp_path / "state" / "files.jsonl")]
    assert [r["status"] for r in rows] == ["too_big", "disk_low"]


def test_guards_abort_a_running_download_and_leave_no_partial_file(tmp_path):
    disk = Disk(100 * 1024 ** 3)
    inbox = FileInbox(cfg(tmp_path), OWNER, disk_usage=disk)
    inbox.check_every = 1
    m = FakeMedia(on_chunk=lambda cur: setattr(disk, "free", 1024 ** 3) if cur > 1000 else None)
    res = run(inbox.fetch(m))
    assert res["status"] == "disk_low" and m.replies == ["📥 not saved: the disk is too full right now"]
    # Telegram said 1 KB, but more arrives: the size cap stops it mid-way
    inbox2 = FileInbox(cfg(tmp_path, files_max_mb=1), OWNER, disk_usage=Disk(100 * 1024 ** 3))
    liar = FakeMedia(data=b"x" * (2 * MB), size=1000, mid=8)
    assert run(inbox2.fetch(liar))["status"] == "too_big"
    boom = FakeMedia(fail=ConnectionError("x"), mid=9)
    res = run(inbox2.fetch(boom))
    assert res["status"] == "failed" and res["detail"] == "error: ConnectionError"
    assert boom.replies == ["📥 not saved: the download failed, please send it again"]
    assert os.listdir(tmp_path / "inbox" / "files" / ".part") == []
    assert [p for p in os.listdir(tmp_path / "inbox" / "files") if p != ".part"] == [] and notes(tmp_path) == []


def test_queue_names_collisions_and_reply_errors(tmp_path):
    inbox = FileInbox(cfg(tmp_path), OWNER, disk_usage=Disk(100 * 1024 ** 3), clock=lambda: 1_790_000_000)

    class NoReply(FakeMedia):
        async def reply(self, text):
            raise RuntimeError("flood")

    async def main():
        a, b = FakeMedia(mid=3), NoReply(mid=3)
        c = FakeMedia(kind="photo", file_name="", mime="image/jpeg", mid=4)
        d = FakeMedia(kind="voice", file_name=None, mime="audio/ogg", mid=5)
        await asyncio.gather(*(inbox.handle(x) for x in (a, b, c, d)))
        return a, b, c, d
    a, b, c, d = run(main())
    names = sorted(p for p in os.listdir(tmp_path / "inbox" / "files") if p != ".part")
    assert len(names) == 4 and any(n.endswith("-3-dive_clip-1.mp4") for n in names)
    assert any(n.endswith("-4-photo.jpg") for n in names) and any(n.endswith("-5-voice.ogg") for n in names)
    assert a.replies == ["📥 got it (1 MB)"] and inbox.saved == 4 and not inbox.tasks
    assert len(notes(tmp_path)) == 3   # a and b share the msg id and second (only in this test): one note name


def test_safe_names_and_sizes():
    assert safe_name("../../etc/passwd", "document") == "passwd"
    assert safe_name("..\\..\\x.mp4", "video") == "x.mp4"
    assert safe_name(".bashrc", "document") == "bashrc"
    assert safe_name("my set\t2026 (live).mp3", "audio") == "my_set_2026__live_.mp3"
    assert safe_name("a\x00b$(rm -rf x);`id`.wav", "audio") == "ab__rm_-rf_x___id_.wav"
    assert safe_name("", "audio", "audio/mpeg") == "audio.mp3"
    assert safe_name(None, "document", "") == "file"
    assert len(safe_name("x" * 400 + ".mp4", "video")) == 104
    assert human_size(3000) == "1 MB" and human_size(152_400_000) == "152 MB" and human_size(1_600_000_000) == "1.6 GB"


def test_files_cli_lists_the_log(tmp_path, capsys):
    c = cfg(tmp_path)
    assert print_files(c) == 0 and "no files yet" in capsys.readouterr().out
    inbox = FileInbox(c, OWNER, disk_usage=Disk(100 * 1024 ** 3))
    res = run(inbox.fetch(FakeMedia()))
    run(inbox.fetch(FakeMedia(size=6 * MB, mid=8)))
    print_files(c, 5)
    out = capsys.readouterr().out.splitlines()
    assert "saved" in out[0] and res["path"] in out[0] and "too_big" in out[1]
    os.rename(res["path"], res["path"] + ".moved")      # routed elsewhere: shown as gone, never deleted by us
    print_files(c, 1, as_json=False)
    assert "too_big" in capsys.readouterr().out
    print_files(c, 5, as_json=True)
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["exists"] is False and rows[1]["status"] == "too_big"


# ---- the real pyrogram handler on PyTgCallsClient (fake app, real filters and message types) ----

class FakeApp:
    def __init__(self):
        self.handlers, self.stopped = [], 0

    def add_handler(self, handler, group=0):
        self.handlers.append((handler, group))

    def stop_transmission(self):
        self.stopped += 1
        raise pyrogram.StopTransmission()


def msg(from_id=OWNER, chat_id=OWNER, outgoing=False, media=True, mid=11, caption="set from sat"):
    kw = {}
    if media:
        kw["document"] = types.Document(file_id="f", file_unique_id="u", file_name="wedding set.mp3",
                                        mime_type="audio/mpeg", file_size=812 * MB)
    return types.Message(id=mid, from_user=types.User(id=from_id), outgoing=outgoing, caption=caption if media else None,
                         chat=types.Chat(id=chat_id, type=enums.ChatType.PRIVATE if chat_id > 0 else
                                         enums.ChatType.SUPERGROUP), text=None if media else "hi", **kw)


def test_on_media_filters_on_the_owner_before_anything_is_read(tmp_path):
    client = PyTgCallsClient({"session_file": "unused"})
    client.app = FakeApp()
    got = []
    client.on_media(got.append, OWNER)
    [(handler, group)] = client.app.handlers
    assert isinstance(handler, MessageHandler) and group == 7

    async def check(m):
        return bool(await handler.filters(client.app, m))

    assert run(check(msg())) is True
    for bad in (msg(from_id=STRANGER, chat_id=STRANGER), msg(from_id=STRANGER), msg(chat_id=-100555),
                msg(outgoing=True), msg(media=False)):
        assert run(check(bad)) is False
    run(handler.callback(client.app, msg()))
    [pm] = got
    assert isinstance(pm, PyroMedia)
    assert (pm.id, pm.kind, pm.from_id, pm.chat_id, pm.file_name, pm.mime, pm.size, pm.caption) == \
        (11, "document", OWNER, OWNER, "wedding set.mp3", "audio/mpeg", 812 * MB, "set from sat")
    with pytest.raises(Exception):
        PyTgCallsClient({"session_file": "unused"}).on_media(got.append, OWNER)   # no live client: refused
    c2 = PyTgCallsClient({"session_file": "unused"})
    c2.app = FakeApp()
    with pytest.raises(Exception):
        c2.on_media(got.append, 0)


def test_pyromedia_guard_stops_the_transmission(tmp_path):
    app = FakeApp()
    seen = {}

    class M:
        id, outgoing, caption = 5, False, None
        chat, from_user = SimpleNamespace(id=OWNER), SimpleNamespace(id=OWNER)
        photo = SimpleNamespace(file_size=4000)

        async def download(self, file_name, progress):
            seen["path"] = file_name
            try:
                await progress(2000, 4000)
                await progress(4000, 4000)
            except pyrogram.StopTransmission:
                return None
            return file_name

        async def reply(self, text, parse_mode=None):
            seen["reply"] = (text, parse_mode)

    pm = PyroMedia(app, M(), "photo")
    assert pm.mime == "image/jpeg" and pm.file_name == "" and pm.size == 4000

    async def stop_at_half(cur, total):
        return cur < 2000

    assert run(pm.download("/x/y", stop_at_half)) is None and app.stopped == 1

    async def ok(cur, total):
        return True
    assert run(pm.download("/x/y", ok)) == "/x/y"
    run(pm.reply("📥 got it (1 MB)"))
    assert seen["reply"] == ("📥 got it (1 MB)", enums.ParseMode.DISABLED)


def test_relay_status_shows_the_file_inbox(tmp_path):
    from util import Rig
    from voice_relay.fakes import FakeCallClient

    class MediaClient(FakeCallClient):
        def on_media(self, cb, owner_id):
            self.media_cb, self.media_owner = cb, owner_id

    async def main():
        r = await Rig.make(str(tmp_path), tg_owner_id=OWNER)
        try:
            client = MediaClient()
            bridge, state = await r.relay.start_call_bridge(client)
            assert state == "on" and client.media_owner == OWNER
            st = await r.relay.i_status(None)
            body = json.loads(st.body)
            assert body["files_inbox"] == "on" and body["files"]["saved"] == 0
            assert r.relay._files.dir == str(tmp_path / "inbox" / "files")
            r2 = await Rig.make(str(tmp_path / "b"), tg_owner_id=OWNER, files_inbox=False)
            try:
                await r2.relay.start_call_bridge(MediaClient())
                assert r2.relay._files_state == "off"
            finally:
                await r2.close()
        finally:
            await r.close()
    os.makedirs(tmp_path / "b", exist_ok=True)
    run(main())
