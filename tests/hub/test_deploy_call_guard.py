"""The call guard: `assistant deploy --target hub` flips CURRENT only while `voice-relay status` says live 0 (a fake
status: one line per read, the last one repeats; "down" = exit 2). A live call is waited out (before the lock and
again right before the flip), else exit 9 with CURRENT unchanged; unreadable = deploy anyway, said and logged; --force
and rollback skip it and write the ":now" attempt. voiceloader asks the running relay before a switch: live > 0 =
the switch waits; ":now", no answer or a call past DEFER_MAX = switch."""

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from hubkit import release as R

REPO = Path(__file__).resolve().parents[2]
TOOL = REPO / "ops" / "assistant.py"


def _mod(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


VL = _mod("voiceloader_guard", REPO / "ops/hub/voiceloader.py")


# ---------------------------------------------------------------- assistant deploy --target hub
def run(repo: Path, *args: str):
    env = dict(os.environ, ASSISTANT_REPO=str(repo), ASSISTANT_LOCK_WAIT="30")
    return subprocess.run(
        [sys.executable, "-I", str(TOOL), *args], env=env, capture_output=True, text=True, timeout=120
    )


@pytest.fixture
def relay(tmp_path, monkeypatch):
    """A fake `voice-relay`; set(*reads) queues the live counts its status reads answer; reads() = how many."""
    q, log = tmp_path / "relay-seq", tmp_path / "relay-log"
    p = tmp_path / "voice-relay"
    p.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"q, log = {str(q)!r}, {str(log)!r}\n"
        "rows = open(q).read().split()\n"
        "if len(rows) > 1:\n"
        "    open(q, 'w').write(chr(10).join(rows[1:]))\n"
        "open(log, 'a').write(' '.join(sys.argv[1:]) + chr(10))\n"
        "if rows[0] == 'down':\n"
        "    sys.stderr.write('voice-relay not reachable: the relay is not up' + chr(10))\n"
        "    sys.exit(2)\n"
        "print(json.dumps({'live': int(rows[0]), 'tg_calls': 'on', 'via': 'hub'}))\n"
    )
    p.chmod(0o755)
    monkeypatch.setenv("ASSISTANT_VOICE_RELAY_BIN", str(p))
    monkeypatch.setenv("ASSISTANT_CALL_POLL", "0.1")

    class Relay:
        def set(self, *reads: str) -> None:
            q.write_text("\n".join(reads))
            log.write_text("")

        def reads(self) -> int:
            return len(log.read_text().splitlines()) if log.exists() else 0

    return Relay()


def line(home: Path) -> str:
    return (home / ".local/share/claude-hub/releases/CURRENT").read_text().strip()


def last(home: Path) -> dict:
    return json.loads((home / ".local/state/assistant/deploys.jsonl").read_text().splitlines()[-1])


def test_a_live_call_is_waited_out_then_the_deploy_runs(hubhome, hubrepo, fakehub, relay):
    repo, commit = hubrepo
    relay.set("1", "1", "0")
    r = run(repo, "deploy", "--target", "hub", "--wait", "10")
    assert r.returncode == 0, r.stderr + r.stdout
    assert "1 live call(s): waiting up to 10 min" in r.stdout and "the call is over: deploying" in r.stdout
    assert "live" in r.stdout and not line(hubhome).endswith(R.NOW)  # a guarded flip: the voice loader waits too
    rec = last(hubhome)
    assert rec["ok"] is True and rec["voice"]["live"] == 0 and rec["voice"]["waited_s"] > 0
    first = line(hubhome).split()[0]
    # a call that starts while the release builds: the check right before the flip waits for it as well
    (repo / "packages/hub/hubd.py").write_text((repo / "packages/hub/hubd.py").read_text() + "# v2\n")
    v2 = commit("v2")[:12]
    relay.set("0", "2", "2", "0")
    r = run(repo, "deploy", "--target", "hub", "--wait", "10")
    assert r.returncode == 0 and line(hubhome).split()[0] == v2, r.stderr
    assert "2 live call(s)" in r.stdout and relay.reads() == 4
    # the plan says what a deploy would do and changes nothing
    relay.set("1")
    r = run(repo, "deploy", "--target", "hub", "--plan", "--rev", first)
    assert r.returncode == 0 and "voice: 1 live call(s): the deploy would wait up to 10 min" in r.stdout
    assert line(hubhome).split()[0] == v2


def test_a_call_past_the_wait_exits_9_with_nothing_changed(hubhome, hubrepo, fakehub, relay):
    repo, commit = hubrepo
    relay.set("0")
    assert run(repo, "deploy", "--target", "hub", "--wait", "10").returncode == 0
    before = line(hubhome)
    (repo / "packages/hub/hubd.py").write_text((repo / "packages/hub/hubd.py").read_text() + "# v2\n")
    v2 = commit("v2")[:12]
    relay.set("1")
    r = run(repo, "deploy", "--target", "hub", "--wait", "10", "--wait-calls", "0.01")
    assert r.returncode == 9, r.stderr + r.stdout
    assert "still live after 0.01 min; CURRENT unchanged" in r.stderr and "--force" in r.stderr
    assert line(hubhome) == before and not (hubhome / ".local/share/claude-hub/releases" / v2).exists()  # not built
    rec = last(hubhome)
    assert rec["ok"] is False and rec["why"] == "voice call live" and rec["voice"]["live"] == 1
    # --wait-calls 0 = refuse at once while a call is live
    r = run(repo, "deploy", "--target", "hub", "--wait-calls", "0")
    assert r.returncode == 9 and line(hubhome) == before
    # --force skips the guard: no status read, CURRENT ends in :now (the voice loader switches at once)
    relay.set("3")
    r = run(repo, "deploy", "--target", "hub", "--wait", "10", "--force")
    assert r.returncode == 0 and relay.reads() == 0, r.stderr
    assert line(hubhome).split()[0] == v2 and line(hubhome).endswith(R.NOW)
    assert last(hubhome)["voice"] == {"skipped": "force"}
    # rollback is never guarded
    r = run(repo, "rollback", "--target", "hub", "--plan")
    assert r.returncode == 0 and "voice: not checked (rollback)" in r.stdout
    r = run(repo, "rollback", "--target", "hub", "--wait", "10")
    assert r.returncode == 0 and relay.reads() == 0 and line(hubhome).split()[0] == before.split()[0], r.stderr
    assert line(hubhome).endswith(R.NOW) and last(hubhome)["voice"] == {"skipped": "rollback"}


def test_an_unreadable_status_deploys_and_says_so(hubhome, hubrepo, fakehub, relay):
    repo, _ = hubrepo
    relay.set("down")
    r = run(repo, "deploy", "--target", "hub", "--wait", "10")
    assert r.returncode == 0, r.stderr
    assert r.stdout.count("voice: status unreadable (voice-relay status: exit 2, voice-relay not reachable") == 1
    assert "deploying anyway" in r.stdout
    v = last(hubhome)["voice"]
    assert v["live"] is None and "exit 2" in v["unreadable"] and last(hubhome)["ok"] is True
    # garbage on stdout counts as unreadable too
    relay.set("x")
    r = run(repo, "deploy", "--target", "hub", "--wait", "10")
    assert r.returncode == 0 and "status unreadable" in r.stdout, r.stderr


def test_guard_flags_are_hub_only(hubhome, hubrepo, relay):
    repo, _ = hubrepo
    assert run(repo, "deploy", "--wait-calls", "5").returncode == 2
    assert run(repo, "deploy", "--wait-calls", "0").returncode == 2
    assert run(repo, "deploy", "--target", "hub", "--wait-calls", "-1").returncode == 2
    assert run(repo, "rollback", "--target", "hub", "--wait-calls", "5").returncode == 2  # argparse: no such flag


# ---------------------------------------------------------------- voiceloader
A, B = "aaaaaaa11111", "bbbbbbb22222"


def _release(root: Path, rid: str) -> None:
    body = "import time\ntime.sleep(60)\n"
    d = root / rid
    (d / "voice").mkdir(parents=True)
    (d / "voice" / "run.py").write_text(body)
    files = {"voice/run.py": {"sha256": hashlib.sha256(body.encode()).hexdigest()}}
    (d / "release.json").write_text(
        json.dumps({"format": 1, "rid": rid, "entry": {"voice": "voice/run.py"}, "files": files})
    )


class _Relay(BaseHTTPRequestHandler):
    """The running relay's loopback status: live calls for the loader's token only."""

    live = 0
    token_file = ""
    asked = 0

    def do_GET(self):  # noqa: N802
        _Relay.asked += 1
        tok = Path(_Relay.token_file).read_text().strip()
        ok = self.path == "/internal/status" and self.headers.get("Authorization") == f"Bearer {tok}"
        out = json.dumps({"live": _Relay.live} if ok else {"error": "unauthorized"}).encode()
        self.send_response(200 if ok else 401)
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):  # noqa: D401
        pass


def test_voiceloader_switches_only_between_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    rel, st = tmp_path / "rel", tmp_path / "st"
    _release(rel, A)
    _release(rel, B)
    srv = HTTPServer(("127.0.0.1", 0), _Relay)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cfg = tmp_path / "cfg" / "live.json"
    cfg.parent.mkdir()
    cfg.write_text(json.dumps({"env": {"VOICE_INTERNAL_PORT": str(srv.server_port)}}))
    _Relay.token_file = str(st / "relay" / "local-token")
    cur = rel / "CURRENT"
    lo = VL.Loader(str(rel), str(st), str(cfg), poll=0.1, healthy=0.0)
    try:
        cur.write_text(f"{A} 1\n")
        lo.step()
        lo.step()
        assert lo.running["rid"] == A and lo.running["healthy"]
        pid = lo.child.pid
        # a new release while a call is live: A keeps running, the status names the wait
        _Relay.live = 1
        cur.write_text(f"{B} 2\n")
        lo.step()
        lo.step()
        assert lo.running["rid"] == A and lo.child.pid == pid and _Relay.asked >= 2
        assert lo.status["state"] == "running" and f"switch to {B} waits for 1 live call" in lo.status["why"]
        # the call ends: the next poll switches
        _Relay.live = 0
        lo.step()
        assert lo.running["rid"] == B and lo.child.pid != pid
        lo.step()
        # ":now" (rollback, deploy --force): at once, the relay is not even asked
        _Relay.live, asked = 1, _Relay.asked
        line = R.write_current(A, rel, now=True)
        assert line.endswith(VL.NOW) and VL.parse_current(line) == (A, line) and R.read_current(rel) == (A, line)
        lo.step()
        assert lo.running["rid"] == A and _Relay.asked == asked
        lo.step()
        # a call still live after DEFER_MAX: switch anyway
        monkeypatch.setattr(VL, "DEFER_MAX", 0.0)
        cur.write_text(f"{B} 3\n")
        lo.step()
        assert lo.running["rid"] == B
        lo.step()
        # the relay does not answer: switch (as before the guard)
        monkeypatch.setattr(VL, "DEFER_MAX", 1800.0)
        srv.shutdown()
        srv.server_close()
        cur.write_text(f"{A} 4\n")
        lo.step()
        assert lo.running["rid"] == A
    finally:
        lo.stop_child()
        srv.server_close()
