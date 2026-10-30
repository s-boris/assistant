"""scripts/tg's flags: --soft goes through the sleep-in gate,
--buttons ROWS and --voice FILE take the live outbox while it runs and the Bot API directly otherwise. Every seam is
a recording stub or a local mock Bot API; no token file outside the temp HOME, nothing reaches Telegram."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from tg_testlib import TG, stub, wait_for

ROWS = '[[{"text":"Yes","data":"x:yes:1"},{"text":"Map","url":"https://maps.example/x"}]]'
TOKEN = "123456:TESTtoken_abc-DEF"
OWNER = "4242"
needs_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="jq missing")
needs_curl = pytest.mark.skipif(shutil.which("curl") is None, reason="curl missing")
needs_ff = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="ffmpeg/ffprobe missing"
)


class Bot:
    """A local Bot API: records each call (method, content type, raw body) and answers from `answers`."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.answers: dict[str, tuple[int, dict]] = {}
        bot = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                method = self.path.rsplit("/", 1)[-1]
                bot.calls.append(
                    {"path": self.path, "method": method, "ctype": self.headers.get("Content-Type", ""), "body": body}
                )
                code, ans = bot.answers.get(method, (200, {"ok": True, "result": {"message_id": 777}}))
                out = json.dumps(ans).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a: object) -> None:
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def bot():
    b = Bot()
    yield b
    b.close()


def env_for(home: Path, *, live: bool = True, gate: str = "pass", bot: Bot | None = None) -> dict:
    """Recording stubs for every seam. gate: pass = the sleep-in gate runs the command now; hold = it records the
    command and its TG_SOFT and queues it (exit 0)."""
    for name in ("sent", "adapter", "usage"):
        stub(
            home, name, f'printf "%s\\n" "$*" >> "{home}/{name}.txt"; cat >> "{home}/{name}.stdin" 2>/dev/null; exit 0'
        )
    stub(
        home,
        "outbox",
        f'printf "%s\\n" "$*" >> "{home}/outbox.txt"; cat >> "{home}/outbox.stdin" 2>/dev/null; '
        f'[ "$2" = tg-voice ] && echo 901; exit "${{OUTBOX_RC:-0}}"',
    )
    if gate == "pass":
        stub(home, "sleepin", '[ "$1" = gate ] && [ "$2" = -- ] || exit 9; shift 2; exec "$@"')
    else:
        stub(
            home,
            "sleepin",
            f'shift 2; for a in "$@"; do printf "%s\\n" "$a"; done > "{home}/queued.txt"; '
            f'echo "${{TG_SOFT:-}}" > "{home}/queued.soft"; exit 0',
        )
    state = home / ".local/state/tg-adapter"
    (state / "live").mkdir(parents=True, exist_ok=True)
    pid = state / "live" / "adapter.pid"
    if live:
        pid.write_text("1\ntg-adapter\n")
    elif pid.exists():
        pid.unlink()
    e = {
        **os.environ,
        "TG_SEND_BIN": str(home / "stub" / "sent"),
        "TG_OUTBOX_BIN": str(home / "stub" / "outbox"),
        "TG_ADAPTER_BIN": str(home / "stub" / "adapter"),
        "TG_ADAPTER_STATE_DIR": str(state),
        "TG_SLEEPIN_BIN": str(home / "stub" / "sleepin"),
    }
    if bot is not None:
        cdir = home / "tgcfg"
        cdir.mkdir(exist_ok=True)
        (cdir / "tok").write_text(f"TELEGRAM_BOT_TOKEN={TOKEN}\n")
        (cdir / "access.json").write_text(json.dumps({"allowFrom": [OWNER]}))
        e.update(TG_ENV=str(cdir / "tok"), TG_ACCESS=str(cdir / "access.json"), TG_API=bot.url)
    return e


def run_tg(home: Path, *args: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(TG), *args], capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL, timeout=30
    )


def read(home: Path, name: str) -> str:
    p = home / name
    return p.read_text() if p.exists() else ""


@pytest.fixture
def ogg(tmp_path: Path) -> Path:
    f = tmp_path / "note.ogg"
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-c:a",
            "libopus",
            str(f),
        ],
        check=True,
        timeout=60,
    )
    return f


# ------------------------------------------------------------------------------------------------ --soft


def test_soft_text_runs_the_gate_then_the_outbox_as_soft(home):
    r = run_tg(home, "--soft", "weekly", "report", env=env_for(home))
    assert r.returncode == 0, r.stderr
    assert read(home, "outbox.txt") == "send tg --soft\n" and read(home, "outbox.stdin") == "weekly report"


def test_soft_env_is_soft_too(home):
    r = run_tg(home, "weekly report", env={**env_for(home), "TG_SOFT": "1"})
    assert r.returncode == 0 and read(home, "outbox.txt") == "send tg --soft\n"


def test_soft_on_a_sleep_in_morning_queues_the_plain_call(home):
    """As tg-soft did: the queued command is plain tg TEXT (urgent once the guard is over), nothing is sent now."""
    r = run_tg(home, "--soft", "weekly report", env=env_for(home, gate="hold"))
    assert r.returncode == 0, r.stderr
    assert read(home, "queued.txt").splitlines() == [str(TG), "weekly report"]
    assert read(home, "queued.soft").strip() == "1"
    assert not (home / "outbox.txt").exists() and not (home / "sent.txt").exists()


def test_urgent_never_meets_the_gate(home):
    r = run_tg(home, "disk 3 hot", env=env_for(home, gate="hold"))
    assert r.returncode == 0 and not (home / "queued.txt").exists()
    assert read(home, "outbox.txt") == "send tg\n"


def test_soft_without_a_sleepin_binary_still_sends(home):
    e = {**env_for(home), "TG_SLEEPIN_BIN": str(home / "nope")}
    assert run_tg(home, "--soft", "hi", env=e).returncode == 0
    assert read(home, "outbox.txt") == "send tg --soft\n"


# ------------------------------------------------------------------------------------------------ --buttons


@needs_jq
def test_buttons_go_to_the_outbox_as_tg_buttons(home):
    r = run_tg(home, "--buttons", ROWS, "Leave now?", env=env_for(home))
    assert r.returncode == 0, r.stderr
    assert read(home, "outbox.txt") == f"send tg-buttons --buttons {ROWS}\n"
    assert read(home, "outbox.stdin") == "Leave now?"
    assert wait_for(lambda: "kind=buttons" in read(home, "usage.txt")), read(home, "usage.txt")


@needs_jq
def test_soft_buttons_queue_with_their_rows(home):
    r = run_tg(home, "Later?", "--soft", "--buttons", ROWS, env=env_for(home, gate="hold"))
    assert r.returncode == 0, r.stderr
    assert read(home, "queued.txt").splitlines() == [str(TG), "--buttons", ROWS, "Later?"]
    r = run_tg(home, "Later?", "--soft", "--buttons", ROWS, env=env_for(home))
    assert read(home, "outbox.txt") == f"send tg-buttons --soft --buttons {ROWS}\n"


@needs_jq
@pytest.mark.parametrize(
    "rows",
    [
        "",
        "not json",
        '[[{"text":"Ok","data":"' + "z" * 70 + '"}]]',
        '[[{"text":"","data":"x"}]]',
        '[[{"text":"Map","url":"http://plain.example"}]]',
    ],
)
def test_bad_rows_exit_2_before_anything(home, rows):
    r = run_tg(home, "--soft", "--buttons", rows, "x", env=env_for(home, gate="hold"))
    assert r.returncode == 2 and "bad ROWS_JSON" in r.stderr
    assert not any((home / f).exists() for f in ("outbox.txt", "sent.txt", "queued.txt", "usage.txt"))


@needs_jq
def test_outbox_exit_75_takes_the_direct_path(home):
    """The outbox is asked first; on 75 the direct path runs (here: no token file in the temp HOME, so exit 1)."""
    r = run_tg(home, "--buttons", ROWS, "Pick", env={**env_for(home), "OUTBOX_RC": "75"})
    assert read(home, "outbox.txt") == f"send tg-buttons --buttons {ROWS}\n"
    assert r.returncode == 1 and "no bot token" in r.stderr
    assert wait_for(lambda: "path=direct" in read(home, "usage.txt"))


@needs_jq
@needs_curl
def test_buttons_direct_path_sends_the_keyboard(home, bot):
    r = run_tg(home, "--buttons", ROWS, "Leave now?", env=env_for(home, bot=bot))
    assert r.returncode == 0, r.stderr
    assert not (home / "outbox.txt").exists()  # a seam set: never the outbox, even with the pid file
    (call,) = bot.calls
    assert call["path"] == f"/bot{TOKEN}/sendMessage"
    form = urllib.parse.parse_qs(call["body"].decode())
    assert form["chat_id"] == [OWNER] and form["text"] == ["Leave now?"]
    assert json.loads(form["reply_markup"][0]) == {
        "inline_keyboard": [
            [{"text": "Yes", "callback_data": "x:yes:1"}, {"text": "Map", "url": "https://maps.example/x"}]
        ]
    }
    assert TOKEN not in r.stdout + r.stderr
    assert wait_for(lambda: "path=direct" in read(home, "usage.txt"))


@needs_jq
@needs_curl
def test_buttons_direct_failure_is_not_exit_0(home, bot):
    bot.answers["sendMessage"] = (400, {"ok": False, "description": "Bad Request: chat not found"})
    r = run_tg(home, "--buttons", ROWS, "x", env=env_for(home, bot=bot))
    assert r.returncode not in (0, 2)
    assert TOKEN not in r.stdout + r.stderr


# ------------------------------------------------------------------------------------------------ --voice


@needs_ff
@needs_jq
def test_voice_goes_to_the_outbox_with_its_fields(home, ogg):
    r = run_tg(home, "--voice", str(ogg), "--silent", "--reply-to", "99", "Train at 14:30", env=env_for(home))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "901"
    assert read(home, "outbox.txt") == (
        f"send tg-voice --file {ogg} --caption Train at 14:30 --duration 2 --silent --reply-to 99\n"
    )
    assert wait_for(lambda: "kind=voice" in read(home, "usage.txt"))


@needs_ff
@needs_jq
@needs_curl
def test_voice_direct_path_and_the_audio_fallback(home, bot, ogg):
    bot.answers["sendVoice"] = (400, {"ok": False, "description": "Bad Request: VOICE_MESSAGES_FORBIDDEN"})
    bot.answers["sendAudio"] = (200, {"ok": True, "result": {"message_id": 555}})
    r = run_tg(home, "--voice", str(ogg), "--reply-to", "7", "Hi", env=env_for(home, bot=bot))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "555" and "audio file" in r.stderr
    assert [c["method"] for c in bot.calls] == ["sendVoice", "sendAudio"]
    body = bot.calls[0]["body"]
    assert b'name="voice"' in body and b"\r\n\r\nHi\r\n" in body and b'"message_id":7' in body
    assert b'name="duration"' in body and b'name="disable_notification"' not in body
    assert not (home / "outbox.txt").exists()
    time.sleep(0.3)
    assert not (home / "adapter.txt").exists()  # no shadow row for a voice note


@needs_ff
@needs_jq
@needs_curl
def test_voice_direct_failure_redacts_the_token(home, bot, ogg):
    bot.answers["sendVoice"] = (401, {"ok": False, "description": f"Unauthorized bot{TOKEN}"})
    r = run_tg(home, "--voice", str(ogg), env=env_for(home, bot=bot))
    assert r.returncode == 1 and "HTTP 401" in r.stderr
    assert TOKEN not in r.stderr and "bot<token>" in r.stderr


@needs_ff
def test_voice_refuses_bad_files(home, tmp_path):
    txt = tmp_path / "x.ogg"
    txt.write_text("not audio")
    for args in (["--voice", str(tmp_path / "missing.ogg")], ["--voice", str(txt)]):
        r = run_tg(home, *args, env=env_for(home))
        assert r.returncode == 2, (args, r.stderr)
    assert not (home / "outbox.txt").exists()


# ------------------------------------------------------------------------------------------------ usage errors


@pytest.mark.parametrize(
    "args",
    [
        ("--silent", "hi"),
        ("--reply-to", "3", "hi"),
        ("--buttons",),
        ("hi", "--voice"),
        ("--buttons", ROWS, "--voice", "/dev/null", "hi"),
    ],
)
def test_flag_misuse_exits_2_and_sends_nothing(home, args):
    r = run_tg(home, *args, env=env_for(home))
    assert r.returncode == 2 and "nothing sent" in r.stderr
    assert not (home / "outbox.txt").exists() and not (home / "sent.txt").exists()


@needs_jq
def test_buttons_need_a_text(home):
    r = run_tg(home, "--buttons", ROWS, " ", env=env_for(home))
    assert r.returncode == 2 and "no text" in r.stderr
