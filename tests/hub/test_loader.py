"""packages/hub/loader.py + hubd.py + ciworker.py end to end on loopback: run CURRENT, switch, refuse a tampered
release, mark a release that does not start bad and fall back to the last good one; the ci role's heartbeat."""

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
from pathlib import Path

import pytest
from hubkit import release as R
from hubkit.cli import judge

from core import http as web

REPO = Path(__file__).resolve().parents[2]
LOADER = REPO / "packages/hub/loader.py"
A, B, C, D = "a" * 12, "b" * 12, "c" * 12, "d" * 12


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(url: str, token: str | None = None) -> tuple[int, dict]:
    try:
        with web.open(url, headers={"Authorization": f"Bearer {token}"} if token else {}, timeout=3) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")
    except OSError:
        return 0, {}


def until(fn, secs: float = 25.0):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.2)
    return fn()


@pytest.fixture
def loader(tmp_path, srctree):
    rel, procs = tmp_path / "rel", []

    def start(role: str, **env):
        port = _free_port()
        args = [sys.executable, "-I", "-B", "-u", str(LOADER), "--role", role, "--releases", str(rel)]
        args += ["--state", str(tmp_path / f"state-{role}"), "--port", str(port), "--poll", "0.3"]
        args += ["--start-timeout", "8", "--check-every", "1", "--heartbeat", str(tmp_path / "hb")]
        e = dict(os.environ, HUB_SPOOL=str(tmp_path / "spool"), **env)
        (tmp_path / "spool").mkdir(exist_ok=True)
        log = (tmp_path / f"{role}.log").open("w")
        p = subprocess.Popen(args, env=e, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        procs.append(p)
        return f"http://127.0.0.1:{port}"

    yield rel, srctree, start
    for p in procs:
        p.terminate()
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()


def test_hub_role(loader, tmp_path):
    rel, src, start = loader
    sec = tmp_path / "secrets"
    sec.mkdir()
    (sec / "token_brain").write_text("t" * 64)
    R.build(src, A, "a" * 40, rel)
    R.write_current(A, rel)
    url = start(
        "hub",
        HUB_SECRETS=str(sec),
        PG_HOST="127.0.0.1",
        PG_PORT="1",
        PHOENIX_URL="http://127.0.0.1:1",
        HUB_FEED_BROWSER="off",  # the feed's claude-hub-browser probe: no connect out of a test
        HUB_KIT="kit123",
        HUB_LOADER="ldr",
    )
    running = lambda rid: get(url + "/health")[1].get("release") == rid  # noqa: E731
    assert until(lambda: running(A)), (tmp_path / "hub.log").read_text()[-800:]
    code, h = get(url + "/health")
    assert code == 200 and h["kit"] == "kit123" and set(h["checks"]) >= {"pg", "phoenix"}
    assert judge(code, h, R.read_current(rel))[0] == 1  # pg + phoenix unreachable here: degraded, not down
    assert get(url + "/whoami", "t" * 64) == (200, {"caller": "brain", "release": A})
    assert get(url + "/whoami", "x" * 64)[0] in (401, 403)
    # switch
    R.build(src, B, "b" * 40, rel)
    R.write_current(B, rel)
    assert until(lambda: running(B))
    # tampered: refused, B keeps running
    d, _ = R.build(src, C, "c" * 40, rel)
    f = d / "app/hubd.py"
    f.write_text(f.read_text() + "#x\n")
    R.write_current(C, rel)
    time.sleep(2)
    assert running(B)
    # a release that does not start: marked bad, back to the last good one
    bad = tmp_path / "badsrc"
    shutil.copytree(src, bad)
    (bad / "packages/hub/hubd.py").write_text("raise SystemExit(3)\n")
    R.build(bad, D, "d" * 40, rel)
    R.write_current(D, rel)
    cur = R.read_current(rel)
    assert until(lambda: cur and cur[1] in (get(url + "/health")[1].get("bad") or []), 30)
    assert running(B)
    rc, probs = judge(*get(url + "/health"), cur)
    assert rc == 1 and any("marked bad" in p for p in probs)
    st = json.loads((tmp_path / "state-hub/loader.json").read_text())
    assert st["last_good"] == B and any(k.startswith(D) for k in st["bad"])


def test_ci_role(loader, tmp_path):
    rel, src, start = loader
    R.build(src, A, "a" * 40, rel)
    R.write_current(A, rel)
    start("ci")
    hb = tmp_path / "spool/ci-heartbeat.json"
    assert until(lambda: hb.is_file() and hb.stat().st_size > 0), (tmp_path / "ci.log").read_text()[-800:]
    assert json.loads(hb.read_text()).get("release") == A


def test_judge_store_state():
    from hubkit.cli import checks_line

    h = {"release": A, "checks": {"pg": "ok"}, "store": {"state": "ok", "schema": "hub", "migrations": ["001"]}}
    assert judge(200, h, (A, "x"))[0] == 0
    assert checks_line(h) == "pg ok · store ok (hub, 1 migration)"
    h["store"] = {"state": "down", "why": "OperationalError"}
    rc, probs = judge(200, h, (A, "x"))
    assert rc == 1 and probs == ["store down (OperationalError)"]
    assert checks_line({"checks": {"pg": "ok"}}) == "pg ok"  # a placeholder release reports no store
