#!/usr/bin/env python3
"""Tracearr key for media: the live default key file (~/.secrets/tracearr-key under a temp HOME) goes by handle
"tracearr" (core.egress). Hub mode works with the file absent and never sends its value; local/shadow mode with the
file absent is a clear error before anything is sent; a test's own key file (MEDIA_TRACEARR_KEYFILE) stays direct.
Offline: urlopen and core.egress's hub call are replaced in the child (nothing is sent); fake keys and profiles only.
Run: python3 -I ~/opt/media-tests/test_tracearr_key.py   (MEDIA_BIN=<copy> tests another copy)"""

import json
import os
import subprocess
import sys
import tempfile

BIN = os.environ.get("MEDIA_BIN") or os.path.expanduser("~/bin/media")
FAKE = "TA-FAKE-KEY-0001"
OWN = "TA-OWN-KEY-0002"

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

passed = failed = 0


def ok(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok   - {name}")
    else:
        failed += 1
        print(f"FAIL - {name}: {detail}")


def run(t, name, mode, keyfile=False, own=False):
    """One child run: HOME=<t>/<name>; egress switch tracearr=<mode>; the live key file only when keyfile."""
    home = os.path.join(t, name)
    os.makedirs(os.path.join(home, ".secrets"))
    os.makedirs(os.path.join(home, ".config", "assistant"))
    with open(os.path.join(home, ".config", "assistant", "egress.json"), "w") as f:
        json.dump({"handles": {"tracearr": mode}}, f)
    if keyfile:
        p = os.path.join(home, ".secrets", "tracearr-key")
        with open(p, "w") as f:
            f.write(FAKE + "\n")
        os.chmod(p, 0o600)
    prof = os.path.join(t, name + "-profile.json")
    with open(prof, "w") as f:
        json.dump({"network": {"server_host": "192.0.2.10"}, "services": {}}, f)
    env = {k: v for k, v in os.environ.items() if k not in ("MEDIA_SECRETS", "MEDIA_TRACEARR_KEYFILE")}
    env.update(HOME=home, ASSISTANT_TEST="1", ASSISTANT_PROFILE=prof)
    if own:
        p = os.path.join(t, name + "-own-key")
        with open(p, "w") as f:
            f.write(OWN + "\n")
        env["MEDIA_TRACEARR_KEYFILE"] = p
    r = subprocess.run([sys.executable, "-I", "-c", DRIVER, BIN], env=env, capture_output=True, text=True, timeout=120)
    try:
        o = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        o = {"error": f"child rc {r.returncode}: {r.stderr.strip()[-300:]}", "direct": [], "hub": []}
    o["raw"] = r.stdout + r.stderr
    return o


def hub_auth(o):
    return [dict((k.lower(), v) for k, v in h["body"].get("headers") or []).get("authorization") for h in o["hub"]]


def leaks(o):
    return FAKE in o["raw"] or FAKE in json.dumps(o["hub"])


with tempfile.TemporaryDirectory() as t:
    o = run(t, "hub-nofile", "hub")
    ok(
        "hub mode, no key file: the call goes through the hub, no error",
        "error" not in o
        and "exit" not in o
        and [h["route"] for h in o["hub"]] == ["/egress/fetch"]
        and not o["direct"],
        o,
    )
    ok(
        "hub mode, no key file: handle tracearr, bearer placeholder, Tracearr's LAN url",
        o["hub"]
        and o["hub"][0]["body"].get("handle") == "tracearr"
        and hub_auth(o) == ["Bearer hub:secret:tracearr"]
        and o["hub"][0]["body"].get("url", "").startswith("http://192.0.2.10:3000/api/v2/public/history"),
        o,
    )

    o = run(t, "hub-file", "hub", keyfile=True)
    ok(
        "hub mode, key file present: still the placeholder, the value is never sent or printed",
        "error" not in o and hub_auth(o) == ["Bearer hub:secret:tracearr"] and not o["direct"] and not leaks(o),
        o,
    )

    o = run(t, "local-file", "local", keyfile=True)
    ok(
        "local mode, key file present: direct, with the file's key (today's request)",
        "error" not in o
        and not o["hub"]
        and [d["auth"] for d in o["direct"]] == [f"Bearer {FAKE}"]
        and FAKE not in o["raw"].replace(json.dumps(o["direct"]), ""),
        o,
    )

    for mode in ("local", "shadow"):
        o = run(t, f"{mode}-nofile", mode)
        msg = o.get("error") or o.get("exit") or ""
        ok(
            f"{mode} mode, no key file: a clear error naming the handle and mode, nothing sent",
            "ApiError" in msg
            and "core.egress tracearr" in msg
            and f"{mode} mode" in msg
            and "FileNotFoundError" not in msg
            and not o["direct"]
            and not o["hub"],
            o,
        )

    o = run(t, "own-key", "hub", own=True)
    ok(
        "a test's own key file (MEDIA_TRACEARR_KEYFILE): the direct path with its own key, even in hub mode",
        "error" not in o and not o["hub"] and [d["auth"] for d in o["direct"]] == [f"Bearer {OWN}"],
        o,
    )

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
