"""H12 against a throwaway Postgres + hubd (hubfix): CI jobs through the API, the runner's result signed by the hub,
verification (a changed record fails), records by tree, caller rules, and the protected-path gate at hubd start."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from hubfix import REPO, Hubd, Pg, missing, vendor_tree

pytestmark = pytest.mark.skipif(bool(missing()), reason=missing() or "")


@pytest.fixture(scope="module")
def hub(tmp_path_factory):
    root = tmp_path_factory.mktemp("h12")
    pg = Pg(root).start()
    h = Hubd(root, pg, vendor_tree(root / "vendor")).start()
    yield h
    h.stop()
    pg.discard()


def bundle(tmp: Path) -> tuple[str, str, str]:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    d = tmp / "r"
    subprocess.run(["git", "init", "-q", "-b", "main", str(d)], check=True, env=env)
    (d / "a").write_text("a\n")
    subprocess.run(["git", "-C", str(d), "add", "."], check=True, env=env)
    subprocess.run(["git", "-C", str(d), "commit", "-qm", "a"], check=True, env=env)
    subprocess.run(["git", "-C", str(d), "bundle", "create", "-q", str(tmp / "b"), "HEAD"], check=True, env=env)
    head = subprocess.run(["git", "-C", str(d), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    tree = subprocess.run(["git", "-C", str(d), "rev-parse", "HEAD^{tree}"], capture_output=True, text=True).stdout
    return head, tree.strip(), base64.b64encode((tmp / "b").read_bytes()).decode()


def test_job_signed_verified_and_tamper_evident(hub, tmp_path):
    head, tree, b64 = bundle(tmp_path)
    s = "t_h12a"
    code, j = hub.call(
        "POST",
        "/ci/jobs",
        {"rid": "h12-job-0001", "commit": head, "tree": tree, "base": None, "branch": "h12", "bundle": b64},
        schema=s,
    )
    assert code == 200 and j["state"] == "queued", j
    again = hub.call(
        "POST", "/ci/jobs", {"rid": "h12-job-0001", "commit": head, "tree": tree, "bundle": b64}, schema=s
    )[1]
    assert again["id"] == j["id"]  # same rid: the first answer, no second job
    spool = hub.root / "spool" / "ci"
    job = json.loads((spool / "jobs" / f"{j['id']}.json").read_text())
    assert job["schema"] == s and (spool / "jobs" / f"{j['id']}.bundle").stat().st_size > 0
    assert hub.call("GET", f"/ci/jobs/{j['id']}", schema=s)[1]["state"] == "queued"
    steps = [
        {"name": "ruff", "ok": True, "detail": "clean", "secs": 1.0},
        {"name": "pytest-3.13", "ok": True, "detail": "9 passed", "secs": 30.0},
        {"name": "legacy", "ok": True, "detail": "ran a", "secs": 9.0, "suites": {"a": [1, 8.5]}},
    ]
    res = {
        "id": j["id"],
        "schema": s,
        "commit": head,
        "tree": tree,
        "ok": True,
        "steps": steps,
        "secs": 31.5,
        "runner": {"release": "x", "toolchain": "y"},
    }
    (spool / "results" / f"{j['id']}.json").write_text(json.dumps(res))
    code, got = hub.call("GET", f"/ci/jobs/{j['id']}", schema=s)
    assert code == 200 and got["state"] == "done" and got["ok"] is True and got["valid"] is True, got
    rec, sig = got["record"], got["sig"]
    assert rec["tree"] == tree and rec["commit"] == head and rec["signer"] == "claude-hub" and len(sig) == 64
    assert rec["steps"][-1]["suites"] == {"a": [1, 8.5]}  # the legacy step's slow/red suites are signed too
    assert hub.call("POST", "/ci/verify", {"record": rec, "sig": sig}, schema=s)[1]["valid"] is True
    forged = {**rec, "ok": True, "tree": "1" * 40}
    assert hub.call("POST", "/ci/verify", {"record": forged, "sig": sig}, schema=s)[1]["valid"] is False
    code, by_tree = hub.call("GET", f"/ci/records?tree={tree}&ok=1", schema=s)
    assert code == 200 and by_tree["id"] == j["id"]
    assert hub.call("GET", f"/ci/records?commit={'2' * 40}", schema=s)[0] == 404
    key = hub.root / "state" / "ci-sign.key"
    assert key.exists() and oct(key.stat().st_mode & 0o777) == "0o600"
    assert (spool / "done" / f"{j['id']}.json").exists()


def test_result_for_another_tree_is_signed_red(hub, tmp_path):
    head, tree, b64 = bundle(tmp_path)
    s = "t_h12b"
    j = hub.call("POST", "/ci/jobs", {"rid": "h12-job-0002", "commit": head, "tree": tree, "bundle": b64}, schema=s)[1]
    res = {
        "id": j["id"],
        "schema": s,
        "commit": head,
        "tree": "3" * 40,
        "ok": True,
        "steps": [{"name": "ruff", "ok": True}],
    }
    (hub.root / "spool" / "ci" / "results" / f"{j['id']}.json").write_text(json.dumps(res))
    got = hub.call("GET", f"/ci/jobs/{j['id']}", schema=s)[1]
    assert got["state"] == "done" and got["ok"] is False and got["valid"] is True
    assert any(st["name"] == "identity" for st in got["record"]["steps"])


def test_bad_submissions_and_callers(hub, tmp_path):
    head, tree, b64 = bundle(tmp_path)
    s = "t_h12c"
    bad = base64.b64encode(b"not a bundle").decode()
    assert hub.call("POST", "/ci/jobs", {"commit": head, "tree": tree, "bundle": bad}, schema=s)[0] == 400
    assert hub.call("POST", "/ci/jobs", {"commit": "xyz", "tree": tree, "bundle": b64}, schema=s)[0] == 400
    assert hub.call("POST", "/ci/jobs", {"commit": head, "tree": tree, "bundle": b64}, who="tg", schema=s)[0] == 403
    assert hub.call("GET", "/ci/records?tree=nothex", schema=s)[0] == 400


def test_runner_slots_set_read_and_default(hub):
    """POST /ci/slots writes the spool's ci/slots.json (the runner reads it before each claim), null removes it (the
    release default); out of 1..4 or another caller is refused; GET shows the set value and the runner's heartbeat."""
    f = hub.root / "spool" / "ci" / "slots.json"
    (hub.root / "spool" / "ci-heartbeat.json").write_text(json.dumps({"slots": 2, "jobs": ["a" * 16], "ts": 1.0}))
    code, ans = hub.call("POST", "/ci/slots", {"slots": 1})
    assert code == 200 and ans["set"] == 1 and ans["runner"]["jobs"] == ["a" * 16], ans
    assert json.loads(f.read_text())["slots"] == 1
    assert hub.call("GET", "/ci/slots")[1]["set"] == 1
    for bad in (0, 5, "2", True):
        assert hub.call("POST", "/ci/slots", {"slots": bad})[0] == 400, bad
    assert hub.call("POST", "/ci/slots", {"slots": 3}, who="tg")[0] == 403
    code, ans = hub.call("POST", "/ci/slots", {"slots": None})
    assert code == 200 and ans["set"] is None and not f.exists()
    assert hub.call("POST", "/ci/slots", {"slots": None})[0] == 200  # already default: still fine


def test_protected_route_and_health(hub):
    code, h = hub.call("GET", "/health")
    assert code == 200 and h["protected"]["how"] == "baseline" and "queued" in h["ci_runner"]
    dg = "a" * 64
    code, p = hub.call("GET", f"/protected/{dg}")
    assert code == 200 and p["accepted"] is False and p["how"] == "refused" and len(p["action_sha"]) == 64


def test_gate_stops_a_release_with_unaccepted_protected_code(hub, tmp_path):
    rel = tmp_path / "rel"
    (rel / "app").mkdir(parents=True)
    (rel / "app" / "hubsecrets.py").write_text("# changed secret loader\n")
    h2 = Hubd(tmp_path / "h2", hub.pg, hub.vendor)
    state = hub.root / "state"  # shares the first hub's state: its baseline is already accepted
    env = {**h2.env(), "HUB_RELEASE_DIR": str(rel), "HUB_STATE": str(state)}
    p = subprocess.run(
        [shutil.which("python3.13") or h2_python(), "-I", "-B", "-u", str(REPO / "packages" / "hub" / "hubd.py")],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert p.returncode == 3 and "protected_refused" in p.stdout, p.stdout[-800:]
    # his tap (the tg caller) on a hub approval for that digest lets it load
    import importlib.util

    spec = importlib.util.spec_from_file_location("hv", REPO / "packages" / "hub" / "hubverify.py")
    hv = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(hv)  # type: ignore[union-attr]
    dg = hv.digest(hv.scan(str(rel)))
    code, ap = hub.call(
        "POST",
        "/approvals",
        {"action_sha": hv.action_sha(dg), "summary": "load protected", "ref": "hubrel:test", "ttl": 600},
    )
    assert code == 200
    assert hub.call("POST", f"/approvals/{ap['nonce']}/decide", {"decision": "y", "by": "42"}, who="tg")[1]["ok"]
    assert hub.call("GET", f"/protected/{dg}")[1]["accepted"] is True
    h2.port = h2.port  # a fresh port for the second hubd
    proc = subprocess.Popen(
        [shutil.which("python3.13") or h2_python(), "-I", "-B", "-u", str(REPO / "packages" / "hub" / "hubd.py")],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        ok = False
        end = time.monotonic() + 30
        while time.monotonic() < end and not ok:
            try:
                code, hh = h2.call("GET", "/health")
                ok = code == 200 and hh["protected"]["how"] == "approval"
            except OSError:
                time.sleep(0.2)
        assert ok
    finally:
        proc.terminate()
        proc.wait(10)
    row = hub.call("GET", f"/approvals/{ap['nonce']}")[1]
    assert row["used_by"] == "hub-verifier"


def h2_python() -> str:
    from hubfix import py313

    return py313() or "python3"
