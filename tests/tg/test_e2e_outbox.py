"""Outbox end to end: the real bun adapter as the LIVE instance (temp HOME, the mock Bot API on a unix socket, no TCP
port)
and the senders on the one outbox: tg (urgent: sent at once; --soft: held in quiet hours; a repeat skipped), a voice
note through tg-outbox (caption + duration on sendVoice), and core.tg's Bot API calls as event-card / tg-status make
them (send with the Talk keyboard, edit, pin, unpin). Skipped when bun or the adapter's node_modules are missing."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
from pathlib import Path

import pytest
from outboxlib import rows
from tg_testlib import REPO, TG, run_cli, stub, wait_for

from core import tg

ADAPTER = REPO / "adapters" / "tg"
BUN = shutil.which("bun") or "/usr/local/bin/bun"
OUTBOX = REPO / "scripts" / "tg-outbox"
pytestmark = pytest.mark.skipif(
    not (os.access(BUN, os.X_OK) and (ADAPTER / "node_modules").is_dir()),
    reason="bun or adapters/tg/node_modules missing",
)


def test_live_adapter_sends_for_the_script_senders(home, tmp_path, monkeypatch):
    sock = tmp_path / "bot.sock"
    calls = tmp_path / "calls.json"
    mock = subprocess.Popen([BUN, "test/mockserve.ts", str(sock), str(calls), "ping"], cwd=ADAPTER)
    try:
        assert wait_for(sock.exists, 10)
        tok = home / "tok" / "live.env"
        tok.parent.mkdir()
        tok.write_text("TELEGRAM_BOT_TOKEN=4242:TEST-not-a-real-token\n")  # secretscan:allow (fake token)
        tok.chmod(0o600)
        cfg = home / ".config/tg-adapter/live.json"
        cfg.parent.mkdir(parents=True)
        cfg.write_text(
            json.dumps(
                {
                    "mode": "live",
                    "bot": "testadapter_bot",
                    "token_file": "~/tok/live.env",
                    "allow_from": ["111"],
                    "api_root": "http://mock.invalid",
                    "api_unix_socket": str(sock),
                    "send": True,
                    "poll_timeout_s": 1,
                    "outbox_interval_ms": 50,
                    "quiet_hours": "0-24",
                    "sleepin": False,
                    "transcribe": False,
                    "typing": False,
                }
            )
        )
        stub(home, "sender", f'echo direct >> "{home}/direct.txt"')
        stub(home, "usage", "true")
        monkeypatch.setenv("TG_ADAPTER_DIR", str(ADAPTER))
        monkeypatch.setenv("TG_ADAPTER_TEST", "1")
        r = run_cli("ensure", "--instance", "live")
        assert r.returncode == 0 and r.stdout.startswith(b"tg-adapter: started (pid "), r.stdout + r.stderr
        assert wait_for(lambda: tg.outbox_on("tg")[0], 15)

        def sent(method: str) -> list[dict]:
            try:
                return [c["params"] for c in json.loads(calls.read_text()) if c["method"] == method]
            except (OSError, ValueError):
                return []

        env = {**os.environ, "TG_SEND_BIN": str(home / "stub" / "sender"), "TG_OUTBOX_BIN": str(OUTBOX)}
        r = subprocess.run(["sh", str(TG), "disk 3 is hot"], env=env, capture_output=True, timeout=60)
        assert r.returncode == 0, r.stderr
        assert wait_for(lambda: [p["text"] for p in sent("sendMessage")] == ["disk 3 is hot"], 5)  # dumped every 100 ms
        r = subprocess.run(["sh", str(TG), "--soft", "weekly report"], env=env, capture_output=True, timeout=60)
        assert r.returncode == 0 and b"queued as outbox" in r.stderr  # quiet hours 0-24: held, not sent
        r = subprocess.run(["sh", str(TG), "disk 3 is hot"], env=env, capture_output=True, timeout=60)
        assert r.returncode == 0 and b"duplicate of" in r.stderr  # the same text within the dedupe window
        assert not (home / "direct.txt").exists()
        assert [p["text"] for p in sent("sendMessage")] == ["disk 3 is hot"]
        st = {json.loads(x["body"])["text"]: x["status"] for x in rows(home) if json.loads(x["body"]).get("text")}
        assert st == {"disk 3 is hot": "sent", "weekly report": "queued"} or st["weekly report"] == "queued"

        ogg = tmp_path / "note.ogg"
        ogg.write_bytes(b"OggS")
        r = subprocess.run(
            [
                "/usr/bin/python3",
                "-I",
                str(OUTBOX),
                "send",
                "tg-voice",
                "--file",
                str(ogg),
                "--caption",
                "on my way",
                "--duration",
                "4",
            ],
            capture_output=True,
            timeout=60,
        )
        assert r.returncode == 0 and r.stdout.strip().isdigit(), r.stderr
        assert wait_for(lambda: len(sent("sendVoice")) == 1, 5)
        v = sent("sendVoice")
        assert len(v) == 1 and v[0]["caption"] == "on my way" and v[0]["duration"] == 4

        talk = {"inline_keyboard": [[{"text": "📞 Talk", "callback_data": "call:talk"}]]}
        m = tg.bot_call(
            "sendMessage",
            {
                "text": "<b>board</b>",
                "parse_mode": "HTML",
                "disable_notification": True,
                "link_preview_options": {"is_disabled": True},
                "reply_markup": talk,
            },
            sender="tg-status",
            hold=False,
            dedupe=False,
            on_press="keep",
        )
        mid = m["message_id"]
        assert tg.bot_call("pinChatMessage", {"message_id": mid, "disable_notification": True}, sender="tg-status")
        assert tg.bot_call(
            "editMessageText",
            {"message_id": mid, "text": "<b>board 2</b>", "parse_mode": "HTML", "reply_markup": talk},
            sender="tg-status",
            on_press="keep",
        ) == {
            "message_id": mid
        }  # tg-status passes keep on every edit with the keyboard (an edit without it resets the mark)
        assert tg.bot_call("unpinChatMessage", {"message_id": mid}, sender="event-card") is True
        assert wait_for(lambda: len(sent("unpinChatMessage")) == 1, 5)
        board = [p for p in sent("sendMessage") if p["text"] == "<b>board</b>"]
        assert board and board[0]["parse_mode"] == "HTML" and board[0]["disable_notification"] is True
        assert board[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "call:talk"
        assert sent("pinChatMessage")[0]["message_id"] == mid and sent("pinChatMessage")[0]["disable_notification"]
        assert sent("editMessageText")[-1]["text"] == "<b>board 2</b>"
        assert sent("unpinChatMessage")[0]["message_id"] == mid
        keep = json.loads((home / ".local/state/tg-adapter/live/buttons.json").read_text())
        assert any(k.endswith(f":{mid}") and v.get("on_press") == "keep" for k, v in keep.items()), (mid, keep)

        r = run_cli("stop", "--instance", "live")
        assert r.returncode == 0
        assert wait_for(lambda: not tg.outbox_on("tg")[0], 10)
        r = subprocess.run(["sh", str(TG), "after stop"], env=env, capture_output=True, timeout=60)
        assert r.returncode == 0 and (home / "direct.txt").read_text() == "direct\n"  # direct path again
    finally:
        mock.send_signal(signal.SIGTERM)
        mock.wait(10)


def test_e2e_paths_stay_in_the_temp_home(home):
    assert Path(os.environ["HOME"]) == home
