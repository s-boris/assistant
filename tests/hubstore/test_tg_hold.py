"""The brain's emergency stop for claude-the hub transport. hubd's /tg/hold (a flag in the hub's state folder, no store
needed; unreadable = held), tgloader honouring it (stops the adapter, starts none, keeps the hold across a restart
with the hub away, resumes on release) and `hub tg stop|start|status`. A throwaway Postgres + hubd (hubfix), the
repo's tgloader under bun with a fake adapter release. Never the real hub, never a real token."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import hubfix
import pytest
from hubkit import cli

REPO = Path(__file__).resolve().parents[2]
BUN = shutil.which("bun") or str(Path.home() / ".bun/bin/bun")
FAKE_CLI = """import { appendFileSync } from 'fs'
appendFileSync(process.env.TG_ADAPTER_STATE_DIR + '/starts.log', JSON.stringify({ rel: process.env.HUB_RELEASE,
  pid: process.pid }) + '\\n')
process.on('SIGTERM', () => process.exit(0))
setInterval(() => {}, 1000)
"""


@pytest.fixture(scope="module")
def hub(tmp_path_factory):
    why = hubfix.missing()
    if why:
        pytest.skip(why)
    root = tmp_path_factory.mktemp("h6hold")
    pg = hubfix.Pg(root).start()
    h = hubfix.Hubd(root, pg, hubfix.vendor_tree(root / "vendor")).start()
    yield h
    h.stop()
    pg.discard()


@pytest.fixture
def released(hub):
    """Every test starts and ends with no hold."""
    hub.call("POST", "/tg/hold", {"held": False, "why": "test reset"})
    yield
    hub.call("POST", "/tg/hold", {"held": False, "why": "test reset"})


@pytest.fixture
def brain_cli(hub, tmp_path, monkeypatch):
    """`hub …` in process as the brain: HOME, HUB_URL and the brain's token of the throwaway hubd."""
    home = tmp_path / "home"
    (home / ".config/tg-adapter").mkdir(parents=True)
    for k, v in {"HOME": str(home), "ASSISTANT_TEST": "1", "HUB_URL": hub.url,
                 "HUB_TOKEN_FILE": str(hub.secrets / "token_brain")}.items():  # fmt: skip
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("HUB_SCHEMA", raising=False)
    return home


def _ok(hub, method, path, body=None, who="brain"):
    st, got = hub.call(method, path, body, who=who)
    assert st == 200, (path, st, got)
    return got


def test_hold_api_roles_and_validation(hub, released):
    got = _ok(hub, "GET", "/tg/hold")
    assert got["held"] is False and "loader" in got
    assert hub.call("POST", "/tg/hold", {"held": True}, who="tg")[0] == 403, "the adapter can't hold or release"
    assert hub.call("POST", "/tg/hold/seen", {"state": "running"}, who="brain")[0] == 403
    assert hub.call("POST", "/tg/hold", {"held": "yes"})[0] == 400
    assert hub.call("POST", "/tg/hold", {"held": True}, schema="t_abc")[0] == 400, "live only"
    assert hub.call("POST", "/tg/hold/seen", {"state": "dancing"}, who="tg")[0] == 400
    assert hub.call("POST", "/tg/hold/seen", {"state": "running", "release": "../x"}, who="tg")[0] == 400
    assert hub.call("DELETE", "/tg/hold")[0] == 404

    got = _ok(hub, "POST", "/tg/hold", {"held": True, "why": "rollback\ndrill"})
    assert got["held"] is True and got["changed"] is True and got["by"] == "brain" and got["why"] == "rollback drill"
    assert _ok(hub, "POST", "/tg/hold", {"held": True})["changed"] is False
    seen = _ok(hub, "POST", "/tg/hold/seen", {"state": "running", "release": "abcdef1", "why": ""}, who="tg")
    assert seen["held"] is True
    got = _ok(hub, "GET", "/tg/hold", who="tg")
    assert got["loader"]["state"] == "running" and got["loader"]["release"] == "abcdef1" and got["loader"]["age_s"] < 5
    st, h = hub.call("GET", "/health")
    assert h["tg_hold"] == {"held": True, "loader": "running"}
    assert json.loads((hub.root / "state/tg-hold.json").read_text())["held"] is True


def test_a_broken_hold_file_holds(hub, released):
    f = hub.root / "state/tg-hold.json"
    f.write_text("{")
    got = _ok(hub, "GET", "/tg/hold")
    assert got["held"] is True and got["why"] == "hold file unreadable"
    f.write_text('{"held": "no"}')
    assert _ok(hub, "GET", "/tg/hold")["why"] == "hold file malformed"
    assert _ok(hub, "POST", "/tg/hold", {"held": False})["held"] is False


def _fake_release(root: Path, rid: str) -> None:
    d = root / rid
    files = {"tg/adapter.js": FAKE_CLI, "tg/tools.toml": "# t\n"}
    meta = {"format": 1, "rid": rid, "entry": {"hub": "app/hubd.py", "tg": "tg/adapter.js"}, "files": {}}
    for rel, text in files.items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(text)
        meta["files"][rel] = {"sha256": hashlib.sha256(text.encode()).hexdigest()}
    (d / "release.json").write_text(json.dumps(meta))


def _until(fn, secs: float = 20.0):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        got = fn()
        if got:
            return got
        time.sleep(0.1)
    raise AssertionError("timed out")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return Path(f"/proc/{pid}/stat").is_file() and ") Z " not in Path(f"/proc/{pid}/stat").read_text()


def test_stop_holds_the_loader_across_restarts_and_start_resumes(hub, released, brain_cli, tmp_path, capsys):
    if not Path(BUN).is_file():
        pytest.skip("no bun")
    rel, state, cfg = tmp_path / "releases", tmp_path / "state", tmp_path / "config/live.json"
    cfg.parent.mkdir(parents=True)
    live = {"hub": {"url": hub.url, "token_file": str(hub.secrets / "token_tg")}}
    cfg.write_text(json.dumps(live))
    _fake_release(rel, "aaaaaaa1")
    (rel / "CURRENT").write_text("aaaaaaa1 t1\n")
    procs: list[subprocess.Popen] = []

    def loader() -> subprocess.Popen:
        p = subprocess.Popen([BUN, str(REPO / "ops/hub/tgloader.ts"), "--releases", str(rel), "--state", str(state),
                              "--config", str(cfg), "--poll", "0.2", "--healthy-after", "0.6"],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)  # fmt: skip
        procs.append(p)
        return p

    def halt(p: subprocess.Popen) -> None:
        p.send_signal(signal.SIGTERM)
        try:
            p.wait(15)
        except subprocess.TimeoutExpired:
            p.kill()

    def status() -> dict:
        f = state / "tgloader-status.json"
        return json.loads(f.read_text()) if f.exists() else {}

    def starts() -> list:
        f = state / "adapter/starts.log"
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []

    try:
        p = loader()
        _until(lambda: status().get("state") == "running")
        _until(lambda: (_ok(hub, "GET", "/tg/hold").get("loader") or {}).get("state") == "running")
        assert cli.main(["tg", "status"]) == 0
        assert "not held" in capsys.readouterr().out
        pid = starts()[0]["pid"]
        assert _alive(pid)

        # the stop: hold set, the adapter gone, tgloader says "held" (the CLI waits for that)
        assert cli.main(["tg", "stop", "--why", "rollback drill", "--wait", "20"]) == 0
        out = capsys.readouterr().out
        assert "hold set" in out and "tgloader held" in out
        assert status()["state"] == "held" and "rollback drill" in status()["why"]
        _until(lambda: not _alive(pid), 10)
        time.sleep(1.0)
        assert len(starts()) == 1, "nothing starts while held (also not the last good release)"
        assert json.loads((state / "tgloader.json").read_text())["hold"]["why"] == "rollback drill"

        # a container restart while the hub is away: the kept hold still applies
        halt(p)
        away = {"url": f"http://127.0.0.1:{hubfix.free_port()}", "token_file": live["hub"]["token_file"]}
        cfg.write_text(json.dumps({"hub": away}))
        p = loader()
        _until(lambda: status().get("state") == "held" and status().get("pid") is None)
        time.sleep(1.5)
        assert len(starts()) == 1
        halt(p)

        # start: refused while the brain's own live adapter is switched on (no live.off), then released
        cfg.write_text(json.dumps(live))
        p = loader()
        _until(lambda: status().get("state") == "held")
        assert cli.main(["tg", "start"]) == 2
        assert "refused" in capsys.readouterr().out
        assert _ok(hub, "GET", "/tg/hold")["held"] is True
        (brain_cli / ".config/tg-adapter/live.off").touch()
        assert cli.main(["tg", "start", "--wait", "20"]) == 0
        out = capsys.readouterr().out
        assert "hold released" in out and "tgloader running" in out
        _until(lambda: status().get("state") == "running")
        assert len(starts()) == 2 and _alive(starts()[1]["pid"])
        assert json.loads((state / "tgloader.json").read_text())["hold"] is None
        halt(p)
    finally:
        for q in procs:
            if q.poll() is None:
                q.kill()
    logs = "".join(q.stdout.read() for q in procs if q.stdout)
    assert '"event":"hold_stop"' in logs and '"event":"hold_released"' in logs
    assert hub.tokens["tg"] not in logs and hub.tokens["brain"] not in logs


def test_cli_when_the_hub_is_unreachable(brain_cli, monkeypatch, capsys):
    monkeypatch.setenv("HUB_URL", f"http://127.0.0.1:{hubfix.free_port()}")
    assert cli.main(["tg", "stop", "--wait", "0"]) == 3
    assert "tg-rollback" in capsys.readouterr().out


def test_the_hold_answers_without_postgres(hub, released):
    """Last in this module: it stops the throwaway Postgres."""
    hub.pg.stop()
    assert hub.call("GET", "/admin/counts")[0] == 503, "the store is really down"
    got = _ok(hub, "POST", "/tg/hold", {"held": True, "why": "pg down"})
    assert got["held"] is True
    assert _ok(hub, "POST", "/tg/hold/seen", {"state": "held", "release": None}, who="tg")["held"] is True
    assert _ok(hub, "GET", "/tg/hold")["loader"]["state"] == "held"
