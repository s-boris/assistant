"""The brain's callers reach the relay in claude-hub-voice. voice-hub (scripts/voice-hub) against the
real hub gateway (tools.hubgw, the real registry's voice.* tools) with a stand-in for the relay's executor on the
voice adapter's claim queue; the voice-relay wrapper's routing (adapters/voice/bin/voice-relay). Scratch HOMEs only:
no real token, gateway, relay or call."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest
from tools import hubgw
from tools.core import load

from core import http as corehttp

REPO = Path(__file__).resolve().parents[2]
VH = REPO / "scripts" / "voice-hub"
WRAPPER = REPO / "adapters" / "voice" / "bin" / "voice-relay"
TOKENS = {"brain": b"B" * 40, "voice": b"V" * 40}


@pytest.fixture(autouse=True)
def _scratch_home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("ASSISTANT_TEST", "1")
    monkeypatch.setenv("ASSISTANT_OTLP", "off")
    for v in ("HUB_GW_URL", "HUB_URL", "HUB_TOKEN_FILE", "VOICE_HUB_BIN"):
        monkeypatch.delenv(v, raising=False)


class Relay:
    """Stands in for hubmode.Executor: claims voice.* with the voice token, answers from `answers`, records args."""

    def __init__(self, gw: str) -> None:
        self.gw, self.stop, self.seen = gw, threading.Event(), []
        self.answers: dict[str, dict[str, Any]] = {}
        self.th = threading.Thread(target=self.loop, daemon=True)

    def loop(self) -> None:
        tok = TOKENS["voice"].decode()
        while not self.stop.is_set():
            st, d = corehttp.call_json("POST", self.gw + "/gw/claim", {"wait": 1}, token=tok, timeout=5)
            job = d.get("job") if st == 200 and isinstance(d, dict) else None
            if not isinstance(job, dict) or not job.get("id"):
                time.sleep(0.05)
                continue
            self.seen.append((job["tool"], job.get("args") or {}, job.get("principal")))
            data = self.answers.get(job["tool"], {})
            res = {"ok": True, "data": data, "text": json.dumps(data), "ms": 1}
            corehttp.call_json("POST", self.gw + "/gw/result", {"id": job["id"], "result": res}, token=tok)


@pytest.fixture
def gw(tmp_path, monkeypatch):
    """The hub gateway on loopback (real registry, brain + voice tokens) and the relay stand-in (not started)."""
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    run = asyncio.run_coroutine_threadsafe(
        hubgw.start(TOKENS, port=0, bind="127.0.0.1", state=tmp_path / "gw", registry=load()), loop
    ).result(30)
    url = f"http://127.0.0.1:{run.port}"
    tok = tmp_path / "hub-token"
    tok.write_bytes(TOKENS["brain"] + b"\n")
    env = dict(os.environ, HUB_GW_URL=url, HUB_TOKEN_FILE=str(tok), ASSISTANT_LIB=str(REPO))
    relay = Relay(url)
    yield {"url": url, "run": run, "env": env, "relay": relay}
    relay.stop.set()
    asyncio.run_coroutine_threadsafe(run.stop(), loop).result(20)
    loop.call_soon_threadsafe(loop.stop)


def online(g: dict[str, Any]) -> Relay:
    r = g["relay"]
    r.th.start()
    for _ in range(100):
        if g["run"].gw.execs["voice"].online():
            break
        time.sleep(0.05)
    return r


def vh(env: dict[str, str], *args: str) -> tuple[int, Any, str]:
    p = subprocess.run([sys.executable, "-I", str(VH), *args], env=env, capture_output=True, text=True, timeout=60)
    try:
        out = json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        out = p.stdout
    return p.returncode, out, p.stderr


def test_call_answers_like_the_loopback_cli(gw):
    r = online(gw)
    ans = r.answers
    ans["voice.call"] = {"delivered": True, "status": "answered", "result": {"status": "answered", "ring_s": 3.1}}
    rc, out, _ = vh(gw["env"], "call", "--reason", "x" * 400, "--say", "your train is late")
    assert rc == 0 and out == {"status": "answered", "ring_s": 3.1}
    tool, args, principal = r.seen[-1]
    assert tool == "voice.call" and len(args["reason"]) == 300 and args["say"] == "your train is late"
    assert principal == "reflex" and "_caller" not in args  # the stamp is the gateway's, never the tool's input
    # the relay's own status rides along: talk-button tells the hourly limit from a call already on
    ans["voice.call"] = {"delivered": False, "status": "busy_or_limited",
                         "result": {"status": "rate_limited", "detail": "max 3 outbound calls per hour"}}  # fmt: skip
    rc, out, _ = vh(gw["env"], "call", "--reason=you tapped Talk")
    assert rc == 4 and out == {"status": "rate_limited", "detail": "max 3 outbound calls per hour"}
    for res, want in (({"status": "declined"}, 3), ({"status": "nothing"}, 6), ({"status": "unavailable"}, 5)):
        ans["voice.call"] = {"delivered": False, "status": "x", "result": res}
        assert vh(gw["env"], "call", "--reason", "decisions")[0] == want
    ans["voice.call"] = {"delivered": False, "status": "not_answered"}  # an executor without the relay's answer
    rc, out, _ = vh(gw["env"], "call", "--reason", "r")
    assert rc == 3 and out == {"status": "no_answer"}
    n = len(r.seen)
    rc, out, _ = vh(gw["env"], "call")
    assert rc == 5 and out == {"status": "failed", "detail": "voice-hub: --reason is needed (voice.call)"}
    assert len(r.seen) == n  # nothing sent
    rows = gw["run"].gw.store.db.execute("SELECT tool, principal, decision, attributed FROM calls").fetchall()
    assert rows and all(row[1:] == ("reflex", "allow", 1) for row in rows), rows


def test_reply_and_status(gw):
    r = online(gw)
    r.answers["voice.reply"] = {"delivered": True, "status": "spoken"}
    rc, out, _ = vh(gw["env"], "reply", "--more", "--", "v1", "It's", "14 degrees.")
    assert rc == 0 and out["delivered"] is True
    assert r.seen[-1][:2] == ("voice.reply", {"id": "v1", "text": "It's 14 degrees.", "more": True})
    r.answers["voice.reply"] = {"delivered": False, "status": "not_delivered", "note": "the call is over"}
    rc, _, err = vh(gw["env"], "reply", "v2", "late")
    assert rc == 3 and "send the answer on Telegram" in err
    n = len(r.seen)
    rc, _, err = vh(gw["env"], "reply", "v3", "y" * 601)
    assert rc == 3 and len(r.seen) == n and "Telegram" in err
    rc, _, err = vh(gw["env"], "reply", "not an id!", "x")  # the tool's id pattern: invalid_args = not delivered
    assert rc == 3 and "invalid_args" in err
    res = {"provider": "gemini", "minutes_today": 2.0, "call_failures": 0}
    r.answers["voice.status"] = {"up": True, "live": 0, "tg_calls": "on", "result": res}
    rc, out, _ = vh(gw["env"], "status")
    assert rc == 0 and out == {**res, "live": 0, "tg_calls": "on", "via": "hub"}
    assert r.seen[-1][:2] == ("voice.status", {"format": "detailed"})


def test_calls_reads_the_hub_relays_call_log(gw):
    """voice-hub calls -> the gateway's voice.calls (detailed) on the relay's own queue: the digests, the outgoing
    summary, the quality numbers and the live ids come back as JSON; --out / --last / --days go along as input."""
    r = online(gw)
    digest = {
        "id": "c1",
        "kind": "tg-out",
        "start": 1793222344.1,
        "outcome": "no_answer",
        "answered": False,
        "ring": ["no_answer", 45.7],
        "talked": False,
        "end": {"reason": "no answer"},
    }
    out_sum = {"calls": 1, "answered": 0, "not_answered": 1, "open": 0, "by": {"no_answer": 1}}
    r.answers["voice.calls"] = {
        "calls": [digest],
        "outgoing": out_sum,
        "stats": {"calls": 3, "out_calls": 1},
        "live": [],
    }
    rc, out, _ = vh(gw["env"], "calls", "--last", "7", "--out", "--days", "14")
    assert rc == 0 and out["calls"] == [digest] and out["outgoing"] == out_sum and out["via"] == "hub"
    assert out["stats"] == {"calls": 3, "out_calls": 1} and out["live"] == []
    assert r.seen[-1][:2] == ("voice.calls", {"last": 7, "days": 14.0, "outgoing": True, "format": "detailed"})
    rc, out, _ = vh(gw["env"], "calls")
    assert rc == 0 and r.seen[-1][1] == {"last": 5, "days": 7.0, "format": "detailed"}
    rc, _, err = vh(gw["env"], "calls", "--days", "30")  # over the log's 14 days: the tool's input refuses it
    assert rc == 2 and "invalid_args" in err


def test_relay_down_paths_exit_2(gw, tmp_path):
    rc, _, err = vh(gw["env"], "reply", "v1", "x")  # the relay in claude-hub-voice is not polling
    assert rc == 2 and "unavailable" in err
    assert vh(gw["env"], "status")[0] == 2 and vh(gw["env"], "call", "--reason", "r")[0] == 2
    assert vh(gw["env"], "calls")[0] == 2
    bad = tmp_path / "bad-token"
    bad.write_text("nope" * 10)
    assert vh(dict(gw["env"], HUB_TOKEN_FILE=str(bad)), "status")[0] == 2  # 401
    assert vh(dict(gw["env"], HUB_TOKEN_FILE=str(tmp_path / "none")), "status")[0] == 2
    assert vh(dict(gw["env"], HUB_GW_URL="http://127.0.0.1:9"), "status")[0] == 2
    env = {k: v for k, v in gw["env"].items() if k != "HUB_GW_URL"}
    rc, _, err = vh(env, "status")  # test mode never falls back to the real hub
    assert rc == 2 and "HUB_GW_URL" in err


def test_a_gateway_without_the_voice_queue_is_refused(tmp_path):
    """A hub without the voice queue runs voice.* on the brain's executor: the call would come back here and round."""
    posts: list[str] = []

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"ok": True, "executor": {"online": True}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            posts.append(self.path)
            self.send_response(500)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    tok = tmp_path / "t"
    tok.write_text("B" * 40)
    env = dict(os.environ, HUB_GW_URL=f"http://127.0.0.1:{srv.server_port}", HUB_TOKEN_FILE=str(tok),
               ASSISTANT_LIB=str(REPO))  # fmt: skip
    try:
        for args in (("reply", "v1", "x"), ("call", "--reason", "r"), ("status",)):
            rc, _, err = vh(env, *args)
            assert rc == 2 and "no voice adapter queue" in err
        assert posts == []
    finally:
        srv.shutdown()


# ---------------------------------------------------------------------------------------- the voice-relay wrapper


def rig(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """A copy of the wrapper with a recording python and a recording voice-hub (the repo copy has no venv)."""
    d = tmp_path / "vr" / "bin"
    d.mkdir(parents=True)
    (tmp_path / "vr" / "voice_relay").mkdir()  # a checkout: the wrapper runs the package next to it
    shutil.copy(WRAPPER, d / "voice-relay")
    log = tmp_path / "argv.log"
    for name, tag in (("py", "PY"), ("vh", "HUB")):
        f = tmp_path / name
        f.write_text(f'#!/bin/sh\nprintf "{tag}" >> "{log}"; for a in "$@"; do printf " [%s]" "$a" >> "{log}"; done\n'
                     f'echo >> "{log}"\n')  # fmt: skip
        f.chmod(0o755)
    env = dict(os.environ, VOICE_RELAY_PYTHON=str(tmp_path / "py"), VOICE_HUB_BIN=str(tmp_path / "vh"))
    return d / "voice-relay", env


def wrap(w: Path, env: dict[str, str], *args: str) -> tuple[int, str]:
    log = w.parents[2] / "argv.log"
    log.unlink(missing_ok=True)
    p = subprocess.run(["sh", str(w), *args], env=env, capture_output=True, text=True, timeout=30)
    return p.returncode, (log.read_text().strip() if log.exists() else p.stderr.strip())


def test_wrapper_sends_call_reply_status_to_voice_hub(tmp_path):
    w, env = rig(tmp_path)
    py = "PY [-E] [-s] [-m] [voice_relay.server]"
    assert wrap(w, env, "call", "--reason", "a b") == (0, "HUB [call] [--reason] [a b]")
    assert wrap(w, env, "reply", "--more", "v1", "it's 14") == (0, "HUB [reply] [--more] [v1] [it's 14]")
    assert wrap(w, env, "status") == (0, "HUB [status]")
    for other in ("serve", "health", "calls", "canary", "fastlane"):  # the rest stays local
        assert wrap(w, env, other)[1].startswith(f"{py} [{other}]")
    rc, err = wrap(w, dict(env, VOICE_HUB_BIN=str(tmp_path / "missing")), "reply", "v1", "x")
    assert rc == 2 and "missing" in err


def test_wrapper_deployed_runs_the_hub_release_copy(tmp_path):
    """~/bin/voice-relay (managed, no voice_relay/ next to it): the local subcommands run the hub's current release
    copy (voice/run.py); call | reply | status still go to voice-hub; no release with voice = exit 2."""
    w, env = rig(tmp_path)
    shutil.rmtree(tmp_path / "vr" / "voice_relay")
    rel = tmp_path / "releases"
    (rel / "abc123" / "voice").mkdir(parents=True)
    (rel / "abc123" / "voice" / "run.py").write_text("")
    (rel / "CURRENT").write_text("abc123 20261029T094012-1:now\n")
    env = dict(env, VOICE_RELAY_RELEASES=str(rel))
    run = str(rel / "abc123" / "voice" / "run.py")
    for cmd in ("fastlane", "stats", "calls", "greet", "canary"):
        assert wrap(w, env, cmd, "--json") == (0, f"PY [-I] [-B] [{run}] [{cmd}] [--json]")
    assert wrap(w, env, "status") == (0, "HUB [status]")
    (rel / "abc123" / "voice" / "run.py").unlink()
    rc, err = wrap(w, env, "stats")
    assert rc == 2 and "no relay code" in err
    rc, err = wrap(w, dict(env, VOICE_RELAY_RELEASES=str(tmp_path / "none")), "fastlane")
    assert rc == 2 and "no relay code" in err


def test_wrapper_to_the_gateway_end_to_end(gw, tmp_path):
    """voice-reply's path: voice-relay reply -> voice-hub -> gateway -> the relay."""
    r = online(gw)
    r.answers["voice.reply"] = {"delivered": True, "status": "spoken"}
    shim = tmp_path / "voice-hub"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" -I "{VH}" "$@"\n')
    shim.chmod(0o755)
    p = subprocess.run(["sh", str(WRAPPER), "reply", "v9", "on my way"], env=dict(gw["env"], VOICE_HUB_BIN=str(shim)),
                       capture_output=True, text=True, timeout=60)  # fmt: skip
    assert p.returncode == 0 and r.seen[-1][:2] == ("voice.reply", {"id": "v9", "text": "on my way"})
