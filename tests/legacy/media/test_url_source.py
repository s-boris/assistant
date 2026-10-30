#!/usr/bin/env python3
"""URL source for media: the live default key file (~/.secrets/media.json under a temp HOME) takes each
service's base URL from the profile (services.<svc>_url), else the file's url; the key goes by handle (core.egress,
local mode); a test's own config keeps its own url/key; a missing live file is not fatal. Offline: urlopen is
replaced in the child (nothing is sent), fake profiles (ASSISTANT_PROFILE) and fake configs only.
Run: python3 -I ~/opt/media-tests/test_url_source.py   (MEDIA_BIN=<copy> tests another copy)"""

import json
import os
import subprocess
import sys
import tempfile

KIND = "media"
BIN = os.environ.get("MEDIA_BIN") or os.path.expanduser("~/bin/media")
SVC = {"emby": "emby"}.get(KIND, "radarr")

DRIVER = r"""
import importlib.machinery, importlib.util, io, json, os, sys, urllib.request
from pathlib import Path
kind, path = sys.argv[1], sys.argv[2]
seen = []
class R(io.BytesIO):
    status, reason = 200, "OK"
    def __init__(s, b):
        super().__init__(b); s.headers = {}
    def getcode(s): return 200
    def getheader(s, k, d=None): return d
def fake(req, *a, **k):
    seen.append({"m": req.get_method(), "url": req.full_url, "d": (req.data or b"").decode(),
                 "h": sorted([k2.lower(), v] for k2, v in req.header_items() if k2.lower() != "user-agent")})
    return R(b"[]" if "history/since" in req.full_url else b'{"version": "1.0"}')
urllib.request.urlopen = fake
out = {}
try:
    ldr = importlib.machinery.SourceFileLoader("t_mod", path)
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("t_mod", ldr))
    ldr.exec_module(m)
    own = os.environ.get("OWN_CFG")
    if kind in ("media", "arr-unblock"):
        m.api("radarr", "GET", "/api/v3/system/status")
    elif kind == "emby":
        m.get("/System/Info")
    elif kind == "container-watch":
        L = m.Local(False)
        if own:
            L.media_path = Path(own); L.media = m.load_json(L.media_path, {})
        L.enabled = True
        out["version"] = L.version("radarr", {"state": "running", "name": "radarr"})
        L.version("seerr", {"state": "running", "name": "seerr"})
    elif kind == "unraid-capacity":
        if own:
            m.MEDIA_SECRETS = Path(own)
        m.ledger(0, 1)
except SystemExit as e:
    out["exit"] = str(e.code)
except Exception as e:
    out["error"] = f"{type(e).__name__}: {e}"
out["seen"] = seen
print(json.dumps(out))
"""

P_URL = {
    s: f"http://192.0.2.10:{p}"
    for s, p in (
        ("radarr", 7878),
        ("sonarr", 8989),
        ("seerr", 5055),
        ("emby", 8096),
        ("bazarr", 6767),
        ("sabnzbd", 8080),
    )
}
F_URL = {s: u.replace("192.0.2.10", "192.0.2.20") for s, u in P_URL.items()}
O_URL = {s: u.replace("192.0.2.10", "192.0.2.30") for s, u in P_URL.items()}
passed = failed = 0


def ok(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok   - {name}")
    else:
        failed += 1
        print(f"FAIL - {name}: {detail}")


def cfg(urls, key):
    return {s: {"url": u, "key": f"{key}-{s}"} for s, u in urls.items()}


def run(t, name, media=None, profile=None, own=None):
    """One child run: HOME=<t>/<name> holding .secrets/media.json (media), ASSISTANT_PROFILE (profile services)."""
    home = os.path.join(t, name)
    os.makedirs(os.path.join(home, ".secrets"))
    if media is not None:
        with open(os.path.join(home, ".secrets/media.json"), "w") as f:
            json.dump(media, f)
    prof = os.path.join(t, name + "-profile.json")
    with open(prof, "w") as f:
        json.dump({"services": profile or {}}, f)
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("MEDIA_SECRETS", "EMBY_SECRETS", "EMBY_URL", "EMBY_KEY", "OWN_CFG")
    }
    env.update(HOME=home, ASSISTANT_TEST="1", ASSISTANT_PROFILE=prof)
    if own is not None:
        p = os.path.join(t, name + "-own.json")
        with open(p, "w") as f:
            json.dump(own, f)
        env.update(OWN_CFG=p, MEDIA_SECRETS=p, EMBY_SECRETS=p)
    r = subprocess.run(
        [sys.executable, "-I", "-c", DRIVER, KIND, BIN], env=env, capture_output=True, text=True, timeout=120
    )
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": f"child rc {r.returncode}: {r.stderr.strip()[-300:]}", "seen": []}


def first(o, svc=SVC):
    return next((x for x in o["seen"] if x["url"].startswith((P_URL[svc], F_URL[svc], O_URL[svc]))), None)


def key_of(x):
    return dict(x["h"]).get("x-emby-token" if SVC == "emby" else "x-api-key") if x else None


with tempfile.TemporaryDirectory() as t:
    prof = {f"{s}_url": u for s, u in P_URL.items()}
    o = run(t, "profile-first", media=cfg(F_URL, "k"), profile=prof)
    x = first(o)
    ok("live file: URL from the profile", x and x["url"].startswith(P_URL[SVC]), o)
    ok("live file: key by handle (the file's key, local mode)", key_of(x) == f"k-{SVC}", o)
    if KIND == "container-watch":
        ok("live file: seerr URL from the profile", any(s["url"].startswith(P_URL["seerr"]) for s in o["seen"]), o)
    if KIND == "unraid-capacity":
        ok(
            "live file: sonarr URL from the profile",
            first(o, "sonarr") and first(o, "sonarr")["url"].startswith(P_URL["sonarr"]),
            o,
        )

    placeholder = {f"{s}_url": f"http://198.51.100.8:{u.rsplit(':', 1)[1]}" for s, u in P_URL.items()}
    for nm, pf in (("fallback", {}), ("placeholder", placeholder)):
        o = run(t, nm, media=cfg(F_URL, "k"), profile=pf)
        x = first(o)
        ok(
            f"live file, profile {'unset' if not pf else 'at its placeholder'}: media.json's url",
            x and x["url"].startswith(F_URL[SVC]) and key_of(x) == f"k-{SVC}",
            o,
        )

    o = run(t, "own", media=cfg(F_URL, "k"), profile=prof, own=cfg(O_URL, "own"))
    x = first(o)
    ok(
        "test's own config: its own url and key (profile ignored)",
        x and x["url"].startswith(O_URL[SVC]) and key_of(x) == f"own-{SVC}",
        o,
    )

    same = cfg(P_URL, "k")
    a = run(t, "wire-live", media=same, profile=prof)
    b = run(t, "wire-own", media=None, profile=prof, own=same)
    ok("local mode: the same request as the direct path", a["seen"] and a["seen"] == b["seen"], (a, b))

    o = run(t, "no-file", media=None, profile=prof)
    if KIND == "container-watch":
        ok(
            "missing live file: no crash, URL from the profile",
            "error" not in o and "exit" not in o and any(s["url"].startswith(P_URL["seerr"]) for s in o["seen"]),
            o,
        )
    else:
        msg = o.get("error") or o.get("exit") or ""
        ok(
            "missing live file: not fatal at start, a clear key error at the call, nothing sent",
            not o["seen"]
            and ("egress" in msg or "handle" in msg)
            and "media.json" not in msg
            and "FileNotFoundError" not in msg,
            o,
        )
    o = run(t, "no-file-no-url", media=None, profile={})
    msg = o.get("error") or o.get("exit") or ""
    if KIND != "container-watch":
        ok(
            "missing live file + no profile URL: names the profile key, nothing sent",
            not o["seen"] and f"services.{SVC}_url" in msg,
            o,
        )

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
