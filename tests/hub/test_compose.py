"""ops/hub/compose.yaml: valid against the compose spec, images = ops/hub/pins.toml (two-week rule), policy."""

import datetime as dt
import json
import re
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

yaml = pytest.importorskip("yaml")
jsonschema = pytest.importorskip("jsonschema")

COMPOSE = yaml.safe_load((REPO / "ops/hub/compose.yaml").read_text())
PINS = tomllib.loads((REPO / "ops/hub/pins.toml").read_text())["image"]
SERVICES = COMPOSE["services"]


def test_schema_valid():
    spec = json.loads((REPO / "ops/hub/compose-spec.json").read_text())
    v = jsonschema.Draft7Validator(spec)
    errors = sorted(v.iter_errors(COMPOSE), key=lambda e: list(e.path))
    assert not errors, [f"{list(e.path)}: {e.message}" for e in errors[:5]]
    broken = yaml.safe_load((REPO / "ops/hub/compose.yaml").read_text())
    broken["services"]["hub"]["imgae"] = "typo"
    assert list(v.iter_errors(broken)), "the schema must reject unknown service keys"


def test_pins_match_compose_refs():
    by_service = {s: p for p in PINS for s in p["services"]}
    assert set(by_service) == set(SERVICES), "every service pinned once, no stale pins"
    for name, svc in SERVICES.items():
        p = by_service[name]
        assert p["digest"].startswith("sha256:") and len(p["digest"]) == 71
        # a service built on the server (ci: the pinned base + the heavy tools) pins its base as the build arg
        got = (svc.get("build") or {}).get("args", {}).get("BASE") or svc["image"]
        assert got == f"{p['ref']}@{p['digest']}", name


def test_pins_respect_exclude_newer():
    cutoff = tomllib.loads((REPO / "pyproject.toml").read_text())["tool"]["uv"]["exclude-newer"]
    limit = dt.datetime.fromisoformat(cutoff.replace("Z", "+00:00")).date()
    for p in PINS:
        if p.get("exception"):  # a deliberate early bump (security fix) names its reason
            assert len(p["exception"].strip()) >= 10, p["ref"]
        else:
            assert dt.date.fromisoformat(p["pushed"]) <= limit, f"{p['ref']} pushed {p['pushed']} after {limit}"
        assert ":latest" not in p["ref"] and ":" in p["ref"]


def test_ci_image_is_built_here_from_locked_inputs():
    """claude-hub-ci = the pinned python base + the heavy tools (ops/hub/ci), built on the server by the install paste
    (kit: ci-image/build): every wheel hash-locked, binary only; 8 GB and 1024 pids for 4 jobs at once."""
    svc = SERVICES["ci"]
    assert svc["build"]["context"] == "${HUB_ROOT:?HUB_ROOT missing}/ci-image/build" and svc["pull_policy"] == "build"
    lim = svc["deploy"]["resources"]["limits"]
    assert lim["memory"] == "8g" and lim["pids"] >= 1024 and lim["cpus"] == "8"
    d = REPO / "ops/hub/ci"
    df = (d / "Dockerfile").read_text()
    assert df.count("--require-hashes --only-binary :all:") == 2
    for f in sorted((d / "requirements").glob("*.txt")):
        pins = [ln for ln in f.read_text().splitlines() if ln and not ln.startswith(("#", " "))]
        assert pins and all(re.fullmatch(r"[a-z0-9-]+==[0-9][0-9a-z.]* \\", ln) for ln in pins), f.name
        assert f.read_text().count("--hash=sha256:") >= len(pins), f.name
    for ln in (d / "requirements/voices.sha256").read_text().splitlines():
        assert re.fullmatch(r"[0-9a-f]{64} [a-z]{2}/[a-z]{2}_[A-Z]{2}/\w+/\w+/[\w.-]+\.onnx(\.json)?", ln), ln


def test_policy():
    for name, svc in SERVICES.items():
        assert "ports" not in svc, f"{name}: nothing published on the host"
        assert svc.get("restart") == "unless-stopped", name
        assert svc.get("healthcheck"), name
        lim = svc["deploy"]["resources"]["limits"]
        assert lim.get("cpus") and lim.get("memory"), name
        assert "no-new-privileges:true" in svc.get("security_opt", []), name
        for v in svc.get("volumes", []):
            src = v["source"] if isinstance(v, dict) else str(v).split(":")[0]
            assert "docker.sock" not in src and not src.startswith(("/etc", "/var/run", "/boot")), (name, src)
            assert src.startswith("${HUB_ROOT") or src.startswith("${BOT_HOME"), (name, src)
            if isinstance(v, dict) and "releases" in v["source"]:
                assert v.get("read_only") is True, f"{name}: releases bind must be read-only"
    assert SERVICES["ci"].get("network_mode") == "none"
    assert "secrets" not in SERVICES["ci"]
    assert SERVICES["pg"]["networks"] == ["hub"]
    assert SERVICES["tg"].get("profiles") == ["tg"]
    assert COMPOSE["networks"]["hub"]["internal"] is True
    assert COMPOSE["networks"]["brain"]["external"] is True


def test_pids_cap_only_in_deploy_limits():
    # compose 2.40 refuses a project that sets pids_limit next to deploy.resources.limits ("can't set distinct
    # values on 'pids_limit' and 'deploy.resources.limits.pids'"), so each service caps processes there only.
    for name, svc in SERVICES.items():
        assert "pids_limit" not in svc, name
        assert svc["deploy"]["resources"]["limits"].get("pids"), name


def test_real_compose_accepts_project(tmp_path):
    # The schema misses compose's own cross-field checks; run the same compose version the server has, when present.
    import os
    import shutil
    import subprocess

    exe = (
        os.environ.get("HUB_COMPOSE_BIN")
        or shutil.which("docker-compose")
        or str(Path.home() / "opt/tools/compose/docker-compose")
    )
    if not Path(exe).is_file():
        pytest.skip("no docker-compose binary")
    env_file = tmp_path / "hub.env"
    env_file.write_text("HUB_ROOT=/tmp/hub\nBOT_HOME=/tmp/bot\nBRAIN_NETWORK=net\nTZ=UTC\n")
    cmd = [exe, "-f", str(REPO / "ops/hub/compose.yaml"), "--env-file", str(env_file), "config", "-q"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-500:]


def test_ci_cpuset_is_a_comma_list_off_the_host_threads():
    # The kit paste recreates claude-hub-ci; a live `docker update --cpuset-cpus` is lost then, so compose carries it.
    # A comma list (the server's CPU pinning page shows nothing for a range), threads 0-3 left to the host.
    cs = SERVICES["ci"]["cpuset"]
    assert isinstance(cs, str) and "-" not in cs
    cpus = [int(c) for c in cs.split(",")]
    assert cpus == sorted(set(cpus)) and min(cpus) >= 4
    assert len(cpus) >= int(float(SERVICES["ci"]["deploy"]["resources"]["limits"]["cpus"]))
