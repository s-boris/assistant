"""The call account's relogin inside claude-hub-voice (docs/runbooks/voice-relogin.md): the kit's voice-relogin paste
renders like its siblings (kit id, sum check, <= PASTE_MAX lines, nothing copied from the brain, no secret on a
command line), `hub kit paste voice-relogin` exists, and the simulation (tests/hub/voice_relogin_sim.py: the
paste under a pseudo-terminal, a docker stand-in, the release's run.py tg with a fake Telegram) passes."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from hubkit import cli
from hubkit import kit as K

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / ".config/tg-adapter").mkdir(parents=True)
    (h / ".config/tg-adapter/live.json").write_text(json.dumps({
        "bot": "test_bot", "token_file": "~/.secrets/tg-live-token",
        "allow_from_file": "~/.claude/channels/telegram/access.json"}))  # fmt: skip
    (h / "opt/voice-relay").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("ASSISTANT_TEST", "1")
    return h


def _render():
    return K.build_voice(REPO, {"KIT_DIR": "/k", "HUB_ROOT": "/mnt/hub", "BOT_HOME": "/home/bot"})


def test_relogin_paste_renders_like_its_siblings(home):
    files, info = _render()
    paste = files["voice-relogin.sh"].decode()
    assert info["state"] == "rendered" and info["relogin_lines"] <= K.PASTE_MAX
    assert "@@" not in paste and paste.startswith(f"# claude-hub voice {info['kit']}: ")
    blob = files["compose.voice.yaml"] + files["loader/voiceloader.py"] + files["voice/live.json"]
    assert f"= '{hashlib.sha256(blob).hexdigest()[:16]}'" in paste  # the kit-changed check, as in voice-install
    assert "<<'HUB'" in paste and paste.rstrip().endswith("rm -f /tmp/claude-hub-voice-relogin.sh")
    # the same one-runner guards as the install paste
    assert '"$B/.voice-relay-off"' in paste and "[v]oice_relay.server serve" in paste
    # nothing from the brain's home is copied or installed; the hub's own key copies stay where they are
    assert '"$B/.secrets' not in paste and "install " not in paste and "voice-tg.json" not in paste


def test_relogin_paste_keeps_codes_off_command_lines(home):
    paste = _render()[0]["voice-relogin.sh"].decode()
    body = K.fill(paste, {})  # no markers left
    # phone, code and 2FA password each go in through stdin (st = code | password)
    assert "login start --phone-stdin" in body and "login finish --$st-stdin" in body
    assert "printf '%s\\n' \"$X\" |" in body and "printf '%s\\n' \"$P\" |" in body
    assert "read -r -s -p" in body and "</dev/tty" in body  # typed at a hidden prompt on his terminal
    assert "docker exec -i" in body and '"$X"' not in body.replace("printf '%s\\n' \"$X\"", "")
    assert body.count("docker restart") == 1  # one restart, after the login; never a second relay
    assert body.index("login finish") < body.index("docker restart")
    assert "--check-release" in body  # the loader's own release check before anything of the release runs


def test_cli_offers_the_relogin_paste(home, monkeypatch, capsys, tmp_path):
    kd = tmp_path / "kit"
    kd.mkdir()
    (kd / "voice-relogin.sh").write_text("# claude-hub voice x: relogin\n")
    monkeypatch.setattr(cli, "kit_dir", lambda *p: kd.joinpath(*p))
    monkeypatch.setattr(cli.K, "info", lambda: {"kit": "x", "voice": {"state": "rendered"}})
    assert cli.main(["kit", "paste", "voice-relogin"]) == 0
    assert capsys.readouterr().out == "# claude-hub voice x: relogin\n"


@pytest.mark.skipif(not os.path.exists("/dev/ptmx") or not os.path.exists("/bin/bash"), reason="needs a pty, bash")
def test_simulation_passes(tmp_path):
    r = subprocess.run(
        [sys.executable, "-I", str(REPO / "tests/hub/voice_relogin_sim.py"), str(REPO), str(tmp_path / "r")],
        capture_output=True,
        text=True,
        timeout=240,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LANG": "C.UTF-8"},
    )
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]
    assert "all steps passed" in r.stdout and "FAIL" not in r.stdout
