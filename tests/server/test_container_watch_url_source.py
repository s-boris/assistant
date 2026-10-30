"""URL source for container-watch: the live default key file (~/.secrets/media.json under a temp HOME) takes each
service's base URL from the profile (services.<svc>_url), else the file's url; the key goes by handle (core.egress,
local mode); a test's own config keeps its own url/key; a missing live file is not fatal. Offline: urlopen is
replaced in the child (nothing is sent), fake profiles (ASSISTANT_PROFILE) and fake configs only; the repo's script
staged with its lib/ (as in test_container_watch.py)."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

DRIVER = r"""
import importlib.machinery, importlib.util, io, json, os, sys, urllib.request
from pathlib import Path
path = sys.argv[1]
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
    L = m.Local(False)
    if own:
        L.media_path = Path(own); L.media = m.load_json(L.media_path, {})
    L.enabled = True
    out["version"] = L.version("radarr", {"state": "running", "name": "radarr"})
    L.version("seerr", {"state": "running", "name": "seerr"})
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
PROF = {f"{s}_url": u for s, u in P_URL.items()}
PLACEHOLDER = {f"{s}_url": f"http://198.51.100.8:{u.rsplit(':', 1)[1]}" for s, u in P_URL.items()}


def cfg(urls, key):
    return {s: {"url": u, "key": f"{key}-{s}"} for s, u in urls.items()}


@pytest.fixture(scope="module")
def tool(tmp_path_factory):
    """bin/container-watch + lib/ (core, botlib, assistant_profile) from the repo: nothing from the live ~/bin."""
    b = tmp_path_factory.mktemp("cw-url") / "bin"
    (b / "lib").mkdir(parents=True)
    shutil.copy2(REPO / "scripts" / "container-watch", b / "container-watch")
    (b / "lib" / "core").symlink_to(REPO / "core")
    (b / "lib" / "botlib.py").symlink_to(REPO / "scripts" / "lib" / "botlib.py")
    (b / "lib" / "assistant_profile.py").symlink_to(REPO / "scripts" / "lib" / "legacy" / "assistant_profile.py")
    return str(b / "container-watch")


@pytest.fixture
def run(tmp_path, tool):
    def go(name, media=None, profile=None, own=None):
        """One child run: HOME=<tmp>/<name> holding .secrets/media.json (media), ASSISTANT_PROFILE (services)."""
        home = tmp_path / name
        (home / ".secrets").mkdir(parents=True)
        if media is not None:
            (home / ".secrets" / "media.json").write_text(json.dumps(media))
        prof = tmp_path / (name + "-profile.json")
        prof.write_text(json.dumps({"services": profile or {}}))
        drop = ("MEDIA_SECRETS", "EMBY_SECRETS", "EMBY_URL", "EMBY_KEY", "OWN_CFG")
        env = {k: v for k, v in os.environ.items() if k not in drop}
        env.update(HOME=str(home), ASSISTANT_TEST="1", ASSISTANT_PROFILE=str(prof))
        if own is not None:
            p = tmp_path / (name + "-own.json")
            p.write_text(json.dumps(own))
            env.update(OWN_CFG=str(p), MEDIA_SECRETS=str(p), EMBY_SECRETS=str(p))
        r = subprocess.run(
            [sys.executable, "-I", "-c", DRIVER, tool], env=env, capture_output=True, text=True, timeout=120
        )
        try:
            return json.loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {"error": f"child rc {r.returncode}: {r.stderr.strip()[-300:]}", "seen": []}

    return go


def first(o, svc="radarr"):
    return next((x for x in o["seen"] if x["url"].startswith((P_URL[svc], F_URL[svc], O_URL[svc]))), None)


def key_of(x):
    return dict(x["h"]).get("x-api-key") if x else None


def test_live_file_url_from_profile_key_by_handle(run):
    o = run("profile-first", media=cfg(F_URL, "k"), profile=PROF)
    x = first(o)
    assert x and x["url"].startswith(P_URL["radarr"]), o
    assert key_of(x) == "k-radarr", o  # the file's key, local mode
    assert any(s["url"].startswith(P_URL["seerr"]) for s in o["seen"]), o


@pytest.mark.parametrize("profile", [{}, PLACEHOLDER], ids=["unset", "placeholder"])
def test_live_file_profile_unset_or_placeholder_uses_media_json_url(run, profile):
    o = run("fallback", media=cfg(F_URL, "k"), profile=profile)
    x = first(o)
    assert x and x["url"].startswith(F_URL["radarr"]) and key_of(x) == "k-radarr", o


def test_own_config_keeps_its_url_and_key(run):
    o = run("own", media=cfg(F_URL, "k"), profile=PROF, own=cfg(O_URL, "own"))
    x = first(o)
    assert x and x["url"].startswith(O_URL["radarr"]) and key_of(x) == "own-radarr", o


def test_local_mode_same_request_as_direct_path(run):
    same = cfg(P_URL, "k")
    a = run("wire-live", media=same, profile=PROF)
    b = run("wire-own", media=None, profile=PROF, own=same)
    assert a["seen"] and a["seen"] == b["seen"], (a, b)


def test_missing_live_file_no_crash_url_from_profile(run):
    o = run("no-file", media=None, profile=PROF)
    assert "error" not in o and "exit" not in o, o
    assert any(s["url"].startswith(P_URL["seerr"]) for s in o["seen"]), o
