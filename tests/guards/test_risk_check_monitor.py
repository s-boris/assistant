"""risk-check rates a Monitor call's command exactly like a Bash command (stack research 30.10).

Monitor runs its tool_input.command as a shell script, so risk_check.py and the reduced risk_check_h11.py must treat
it like Bash: the same deny (HIGH, HANG) or allow (LOW, MEDIUM logged), the same owner-OK code flow, for the main
session and subagents alike. A Monitor call without a command (its `ws` form) and every non-shell tool still pass.
Both rule sets run in-process over the whole corpus (tests/guards/h11_corpus.json) and once through the wrapper.
Never touches the live hooks or their state: profile, state, log, lockdown files and the switch live in a temp dir.
The local LLM is off.
"""

from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GUARDS = REPO / "hooks" / "guards"
CORPUS = json.loads((Path(__file__).with_name("h11_corpus.json")).read_text(encoding="utf-8"))["cases"]
FIXTURE = {
    "owner": {"github": "example-user"},
    "network": {
        "server_name": "Testsrv",
        "server_host": "192.0.2.10",
        "lan_prefix": "192.0.2.",
        "domain": "example.org",
        "hosts": {"web_vm": "web@192.0.2.20"},
    },
}
AGENT = "a0123456789abcdef"
RISKY = [  # (command, what the deny reason starts with) for both rule sets
    ("crontab -r", "risk-check: HIGH"),
    ("git push --force origin main", "risk-check: HIGH"),
    ("rm -rf /home/node/.secrets/x", "risk-check: HIGH"),
    ("gh repo delete example-user/x --yes", "risk-check: HIGH"),
    ("rm -f $T/*", "risk-check: this rm would hang"),  # HANG: denied whatever the level
]
BENIGN = [
    "tail -f /home/node/.local/state/app.log | grep --line-buffered -E 'ERROR|Traceback'",
    "while true; do git -C /home/node/opt/assistant log --oneline -1; sleep 30; done",
    "ls -la /home/node/jobs",
    "rm -f /home/node/jobs/x/out.txt",
]
SETS = ["risk_check.py", "risk_check_h11.py"]


def _load(path: Path, name: str):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("rc-monitor")
    profile = tmp / "profile.json"
    profile.write_text(json.dumps(FIXTURE))
    probe = tmp / "probe"
    probe.write_text("verbs 1792688521\n")
    saved = dict(os.environ)
    os.environ.update(
        {
            "ASSISTANT_PROFILE": str(profile),
            "RISK_CHECK_LLM": "off",
            "RISK_CHECK_STATE": str(tmp / "state"),
            "RISK_CHECK_LOG": str(tmp / "risk-check.log"),
            "RISK_CHECK_H11_PROBE": str(probe),
            "RISK_CHECK_H11_MODE_FILE": str(tmp / "no-mode-file"),
        }
    )
    try:
        mods = {f: _load(GUARDS / f, "rc_monitor_" + f.split(".")[0]) for f in SETS}
        yield {"tmp": tmp, "mods": mods, "probe": probe, "env": dict(os.environ)}
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _payload(tool: str, cmd: str | None, agent: str | None = None, **ti) -> dict:
    tool_input = dict(ti)
    if cmd is not None:
        tool_input["command"] = cmd
    if tool == "Monitor":
        tool_input.setdefault("description", "errors in the app log")
        tool_input.setdefault("timeout_ms", 300000)
    else:
        tool_input.setdefault("description", "Show it")
    ev = {
        "session_id": "s-main",
        "hook_event_name": "PreToolUse",
        "cwd": "/home/node",
        "permission_mode": "bypassPermissions",
        "tool_name": tool,
        "tool_input": tool_input,
    }
    if agent:
        ev["agent_id"] = agent
    return ev


def _hook(mod, ev: dict, monkeypatch: pytest.MonkeyPatch) -> str:
    """In-process hook run: its stdout ('' = allowed)."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(ev)))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert mod.hook() == 0
    return out.getvalue().strip()


def _reason(out: str) -> str:
    d = json.loads(out)["hookSpecificOutput"]
    assert (d["hookEventName"], d["permissionDecision"]) == ("PreToolUse", "deny")
    return d["permissionDecisionReason"]


def _log(env) -> str:
    p = Path(env["env"]["RISK_CHECK_LOG"])
    return p.read_text() if p.exists() else ""


@pytest.mark.parametrize("rules", SETS)
def test_monitor_matches_bash_over_the_corpus(env, rules, monkeypatch):
    mod = env["mods"][rules]
    denied = 0
    for c in CORPUS:
        agent = AGENT if c["agent"] else None
        bash = _hook(mod, _payload("Bash", c["cmd"], agent), monkeypatch)
        mon = _hook(mod, _payload("Monitor", c["cmd"], agent), monkeypatch)
        assert mon == bash, (c["src"], c["cmd"])
        denied += bool(bash)
    assert denied > 50, "the corpus must exercise denies, not only allows"


@pytest.mark.parametrize("rules", SETS)
@pytest.mark.parametrize("agent", [None, AGENT])
@pytest.mark.parametrize("cmd,head", RISKY)
def test_monitor_risky_command_is_denied(env, rules, agent, cmd, head, monkeypatch):
    mod = env["mods"][rules]
    reason = _reason(_hook(mod, _payload("Monitor", cmd, agent), monkeypatch))
    assert reason.startswith(head), reason
    assert reason == _reason(_hook(mod, _payload("Bash", cmd, agent), monkeypatch))


@pytest.mark.parametrize("rules", SETS)
@pytest.mark.parametrize("cmd", BENIGN)
def test_monitor_benign_command_passes(env, rules, cmd, monkeypatch):
    mod = env["mods"][rules]
    before = _log(env)
    assert _hook(mod, _payload("Monitor", cmd), monkeypatch) == ""
    assert _log(env) == before, "a LOW command leaves no log line"


@pytest.mark.parametrize("rules", SETS)
def test_monitor_medium_is_allowed_and_logged_like_bash(env, rules, monkeypatch):
    mod = env["mods"][rules]
    cmd = "git push --force origin feature"
    n = _log(env).count(" allow ")
    assert _hook(mod, _payload("Monitor", cmd), monkeypatch) == ""
    assert _log(env).count(" allow ") == n + 1
    line = _log(env).splitlines()[-1]
    assert line.split(" ", 1)[1].startswith("MEDIUM allow"), line
    assert _hook(mod, _payload("Bash", cmd), monkeypatch) == ""
    bash_line = _log(env).splitlines()[-1]
    assert line.split(" ", 1)[1] == bash_line.split(" ", 1)[1], "same log line apart from the timestamp"


@pytest.mark.parametrize("rules", SETS)
def test_monitor_owner_ok_code_works_once_main_only(env, rules, monkeypatch, capsys):
    mod = env["mods"][rules]
    cmd = "crontab -r"
    reason = _reason(_hook(mod, _payload("Monitor", cmd), monkeypatch))
    h = reason.split("risk-ok ", 1)[1].split()[0]
    assert len(h) == 10, reason
    assert mod.cli([h, "his OK 10:00"]) == 0
    code = capsys.readouterr().out.split("RISK_OK=", 1)[1].split()[0]
    with_code = f"RISK_OK={code} {cmd}"
    assert _hook(mod, _payload("Monitor", with_code, AGENT), monkeypatch) != ""  # a subagent can't use a code
    assert _hook(mod, _payload("Monitor", with_code), monkeypatch) == ""  # the main session: passes once
    assert " OVERRIDE " in _log(env).splitlines()[-1]
    assert _hook(mod, _payload("Monitor", with_code), monkeypatch) != ""  # used up


@pytest.mark.parametrize("rules", SETS)
def test_no_command_or_other_tools_pass(env, rules, monkeypatch):
    mod = env["mods"][rules]
    ws = _payload("Monitor", None, ws={"url": "wss://example.org/stream"})
    assert _hook(mod, ws, monkeypatch) == ""  # the WebSocket form runs no shell
    for tool in ("Read", "monitor", "Grep", "Agent"):
        assert _hook(mod, _payload(tool, "crontab -r"), monkeypatch) == "", tool
    bad = _payload("Monitor", "crontab -r")
    bad["tool_input"] = "crontab -r"
    assert _hook(mod, bad, monkeypatch) == ""
    assert "ERROR" not in _log(env)


# ---------------------------------------------------------------------------------------------- through the wrapper
WRAP_TOOLS = shutil.which("timeout") and os.path.exists("/usr/bin/python3")


@pytest.mark.skipif(not WRAP_TOOLS, reason="needs /usr/bin/python3 and timeout")
@pytest.mark.parametrize("switch", [None, "reduced-v1\n"])
def test_wrapper_rates_a_monitor_payload(env, tmp_path, switch):
    d = tmp_path / "hooks"
    d.mkdir()
    for f in ("risk-check.sh", *SETS):
        shutil.copy2(GUARDS / f, d / f)
    if switch is not None:
        (d / "risk-check.h11").write_text(switch)
    log = tmp_path / "wrapper.log"
    e = dict(env["env"], RISK_CHECK_LOG=str(log), RISK_CHECK_H11_PROBE=str(env["probe"]))
    e.pop("RISK_CHECK_H11_SWITCH", None)

    def run(cmd: str) -> subprocess.CompletedProcess:
        payload = json.dumps(_payload("Monitor", cmd))
        return subprocess.run(
            ["/bin/sh", str(d / "risk-check.sh")], input=payload, capture_output=True, text=True, env=e, timeout=60
        )

    r = run("crontab -r")
    assert r.returncode == 0 and _reason(r.stdout).startswith("risk-check: HIGH")
    assert ("prof=h11" in log.read_text()) == (switch is not None)
    for cmd in ("tail -f /home/node/.local/state/app.log | grep --line-buffered ERROR", "git status --short"):
        r = run(cmd)
        assert (r.returncode, r.stdout) == (0, ""), cmd
