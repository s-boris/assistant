"""Tracearr key for media: the live default key file (~/.secrets/tracearr-key under a temp HOME) goes by handle
"tracearr" (core.egress). Hub mode works with the file absent and never sends its value; local/shadow mode with the
file absent is a clear error before anything is sent; a test's own key file (MEDIA_TRACEARR_KEYFILE) stays direct.
Offline: urlopen and core.egress's hub call are replaced in the child (nothing is sent); fake keys and profiles only;
the repo's media staged with its lib."""

from __future__ import annotations

import json
import os
import subprocess

import pytest
from mediatest import PYTHON3, stage

FAKE = "TA-FAKE-KEY-0001"
OWN = "TA-OWN-KEY-0002"
PLACEHOLDER = ["Bearer hub:secret:tracearr"]

DRIVER = r"""
import base64, importlib.machinery, importlib.util, io, json, os, sys, urllib.request
direct, hub = [], []
class R(io.BytesIO):
    status, reason = 200, "OK"
    def __init__(s, b):
        super().__init__(b); s.headers = {}
    def getcode(s): return 200
    def getheader(s, k, d=None): return d
def fake(req, *a, **k):
    direct.append({"url": req.full_url, "auth": dict((k2.lower(), v) for k2, v in req.header_items()).get("authorization")})
    return R(b'{"data": [], "meta": {}}')
urllib.request.urlopen = fake
def fake_hub(path, body, timeout):
    hub.append({"route": path, "body": body})
    return 200, {"status": 200, "reason": "OK", "headers": [["Content-Type", "application/json"]],
                 "body_b64": base64.b64encode(b'{"data": [], "meta": {}}').decode()}
out = {}
try:
    ldr = importlib.machinery.SourceFileLoader("t_mod", sys.argv[1])
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("t_mod", ldr))
    ldr.exec_module(m)
    if m._egress is not None:
        m._egress._hub = fake_hub
    m.tracearr_cfg()
    out["data"] = m.get("tracearr", "/api/v2/public/history?pageSize=1")
except SystemExit as e:
    out["exit"] = str(e.code)
except Exception as e:
    out["error"] = f"{type(e).__name__}: {e}"
out["direct"], out["hub"] = direct, hub
print(json.dumps(out))
"""  # noqa: E501


@pytest.fixture
def run(tmp_path):
    tool = stage(tmp_path)

    def run_(name, mode, keyfile=False, own=False):
        """One child run: HOME=<tmp>/<name>; egress switch tracearr=<mode>; the live key file only when keyfile."""
        home = tmp_path / name
        (home / ".secrets").mkdir(parents=True)
        (home / ".config" / "assistant").mkdir(parents=True)
        (home / ".config" / "assistant" / "egress.json").write_text(json.dumps({"handles": {"tracearr": mode}}))
        if keyfile:
            p = home / ".secrets" / "tracearr-key"
            p.write_text(FAKE + "\n")
            p.chmod(0o600)
        prof = tmp_path / f"{name}-profile.json"
        prof.write_text(json.dumps({"network": {"server_host": "192.0.2.10"}, "services": {}}))
        env = {k: v for k, v in os.environ.items() if not k.startswith("MEDIA_")}
        env.update(HOME=str(home), ASSISTANT_TEST="1", ASSISTANT_PROFILE=str(prof))
        if own:
            p = tmp_path / f"{name}-own-key"
            p.write_text(OWN + "\n")
            env["MEDIA_TRACEARR_KEYFILE"] = str(p)
        r = subprocess.run([PYTHON3, "-I", "-c", DRIVER, tool], env=env, capture_output=True, text=True, timeout=120)
        try:
            o = json.loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            o = {"error": f"child rc {r.returncode}: {r.stderr.strip()[-300:]}", "direct": [], "hub": []}
        o["raw"] = r.stdout + r.stderr
        return o

    return run_


def hub_auth(o):
    return [dict((k.lower(), v) for k, v in h["body"].get("headers") or []).get("authorization") for h in o["hub"]]


def leaks(o):
    return FAKE in o["raw"] or FAKE in json.dumps(o["hub"])


def test_hub_mode_without_key_file(run):
    o = run("hub-nofile", "hub")
    assert "error" not in o and "exit" not in o, o
    assert [h["route"] for h in o["hub"]] == ["/egress/fetch"] and not o["direct"], o
    # handle tracearr, bearer placeholder, Tracearr's LAN url
    assert o["hub"][0]["body"].get("handle") == "tracearr" and hub_auth(o) == PLACEHOLDER, o
    assert o["hub"][0]["body"].get("url", "").startswith("http://192.0.2.10:3000/api/v2/public/history"), o


def test_hub_mode_never_sends_the_value(run):
    o = run("hub-file", "hub", keyfile=True)
    assert "error" not in o and hub_auth(o) == PLACEHOLDER and not o["direct"] and not leaks(o), o


def test_local_mode_with_key_file(run):
    o = run("local-file", "local", keyfile=True)
    assert "error" not in o and not o["hub"], o
    assert [d["auth"] for d in o["direct"]] == [f"Bearer {FAKE}"], o
    assert FAKE not in o["raw"].replace(json.dumps(o["direct"]), ""), o


@pytest.mark.parametrize("mode", ["local", "shadow"])
def test_no_key_file_is_a_clear_error(run, mode):
    o = run(f"{mode}-nofile", mode)
    msg = o.get("error") or o.get("exit") or ""
    assert "ApiError" in msg and "core.egress tracearr" in msg and f"{mode} mode" in msg, o
    assert "FileNotFoundError" not in msg and not o["direct"] and not o["hub"], o


def test_own_key_file_stays_direct(run):
    o = run("own-key", "hub", own=True)
    assert "error" not in o and not o["hub"] and [d["auth"] for d in o["direct"]] == [f"Bearer {OWN}"], o
