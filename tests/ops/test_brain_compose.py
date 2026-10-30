"""ops/brain/compose.yaml (the brain container's reference definition): valid compose, in-place names, policy."""

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
FILE = REPO / "ops/brain/compose.yaml"

yaml = pytest.importorskip("yaml")
jsonschema = pytest.importorskip("jsonschema")

TEXT = FILE.read_text()
COMPOSE = yaml.safe_load(TEXT)
BOT = COMPOSE["services"]["bot"]

# NEVER set these in the brain (the owner's rules): Telegram inbound drops, or the effort level gets pinned.
FORBIDDEN_ENV = {
    "DISABLE_TELEMETRY",
    "DO_NOT_TRACK",
    "DISABLE_GROWTHBOOK",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    "CLAUDE_CODE_DISABLE_AGENT_VIEW",
    "CLAUDE_CODE_EFFORT_LEVEL",
}


def _env_keys(svc):
    env = svc.get("environment") or {}
    if isinstance(env, list):
        return {str(e).split("=", 1)[0] for e in env}
    return set(env)


def test_schema_valid():
    spec = json.loads((REPO / "ops/hub/compose-spec.json").read_text())
    errors = list(jsonschema.Draft7Validator(spec).iter_errors(COMPOSE))
    assert not errors, [f"{list(e.path)}: {e.message}" for e in errors[:5]]


def test_names_replace_the_live_container_in_place():
    # live: compose project src, service bot, container claude-bot, its default network (the hub's BRAIN_NETWORK)
    assert COMPOSE["name"] == "src"
    assert list(COMPOSE["services"]) == ["bot"]
    assert BOT["container_name"] == "claude-bot" and BOT["hostname"] == "claude-bot"
    assert "image" not in BOT and BOT["build"]["context"] == ".", "compose's own image name src-bot, as live"
    assert BOT["networks"] == ["default"] and list(COMPOSE["networks"]) == ["default"]
    assert COMPOSE["networks"]["default"]["name"].startswith("${BRAIN_NETWORK")
    assert BOT["init"] is True and BOT["user"] == "node"


def test_never_set_env_vars_stay_out():
    keys = _env_keys(BOT)
    assert not keys & FORBIDDEN_ENV, keys & FORBIDDEN_ENV
    assert "TELEGRAM_BOT_TOKEN" not in keys, "the token lives in the home's channel file, never in the env"
    assert "env_file" not in BOT, "only the named keys may reach the container"
    for name in FORBIDDEN_ENV:
        assert not re.search(rf"^\s*-?\s*{name}\s*[:=]", TEXT, re.M), name


def test_no_secret_or_personal_values():
    # every env value is an interpolation from the owner's env file, never a literal
    for k, v in (BOT.get("environment") or {}).items():
        assert isinstance(v, str) and re.fullmatch(r"\$\{[A-Z_]+(:?[-?][^}]*)?\}", v), (k, v)
    assert "secrets" not in BOT and "secrets" not in COMPOSE, "no secret files: the brain's keys live in its home"
    assert not re.search(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b", TEXT), "bot-token-shaped string"
    assert not re.search(r"[A-Za-z0-9+/_-]{40,}", TEXT), "long token-like string"
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", TEXT), "IP address"
    assert not re.search(r"/mnt/|/boot/|/home/(?!node\b)", TEXT), "host path: use BOT_HOME"


def test_policy():
    assert "ports" not in BOT and "expose" not in BOT, "nothing published"
    assert not BOT.get("privileged") and "cap_add" not in BOT and "devices" not in BOT
    assert BOT.get("network_mode") is None, "own network, never host"
    vols = BOT["volumes"]
    assert len(vols) == 1, "only /home/node survives a rebuild"
    v = vols[0]
    assert v["type"] == "bind" and v["target"] == "/home/node"
    assert v["source"].startswith("${BOT_HOME:?") and v["bind"]["create_host_path"] is False
    assert "docker.sock" not in TEXT


def test_cpuset_is_a_comma_list_off_the_host_threads():
    cs = BOT["cpuset"]
    assert isinstance(cs, str) and "-" not in cs
    cpus = [int(c) for c in cs.split(",")]
    assert cpus == sorted(set(cpus)) and min(cpus) >= 4
    assert cpus == list(range(4, 32)), "the live pin (28.10): threads 4-31"


def test_real_compose_accepts_project(tmp_path):
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
    env_file = tmp_path / "brain.env"
    env_file.write_text("BOT_HOME=/tmp/bot\nTELEGRAM_CHAT_ID=1\nTZ=UTC\n")
    base = [exe, "-f", str(FILE), "--env-file", str(env_file)]
    # a clean env: the process env wins over --env-file, and the brain's own env has BOT_HOME etc.
    clean = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path)}

    def run(*args):
        return subprocess.run([*base, *args], capture_output=True, text=True, timeout=60, env=clean)

    r = run("config", "-q")
    assert r.returncode == 0, r.stderr[-500:]
    r = run("config", "--format", "json")
    assert r.returncode == 0, r.stderr[-500:]
    cfg = json.loads(r.stdout)
    assert cfg["networks"]["default"]["name"] == "src_default", "the hub and the vault join this network"
    assert not _env_keys(cfg["services"]["bot"]) & FORBIDDEN_ENV
    # a missing BOT_HOME must fail, never fall back to an empty path
    env_file.write_text("TELEGRAM_CHAT_ID=1\n")
    r = run("config", "-q")
    assert r.returncode != 0 and "BOT_HOME" in r.stderr
