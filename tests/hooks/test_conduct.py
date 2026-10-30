"""hooks/conduct.py (deny-once conduct checks C1-C5; C1 shadow-first) and hooks/pitch-check.sh.

Unit tests load the hook as a module; the end-to-end tests run it as Claude Code does (hook JSON on stdin, a temp
HOME, stub `memory`, `agent-watch`, `tasks` and `board-lint`). Fixtures are shaped like the real payloads; every
name, id and ledger item is invented."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
HOOK = REPO / "hooks" / "conduct.py"
# the hook's interpreter (its shebang): here and in the hub CI runner (python:3.13 image, no /usr/bin/python3)
PY = "/usr/local/bin/python3.13" if os.path.exists("/usr/local/bin/python3.13") else sys.executable
PITCH = REPO / "hooks" / "pitch-check.sh"
OWNER = "100000001"
TP = "mcp__plugin_telegram-plus_telegram__"
LEDGER = [
    {
        "id": "r0000000001",
        "day": 21,
        "month": 10,
        "verb": "declined",
        "thing": "market ticker card",
        "silent": True,
        "negative": True,
    },
    {
        "id": "r0000000002",
        "day": 22,
        "month": 10,
        "verb": "removed",
        "thing": "link digests, gym coach, /radio explainer",
        "silent": False,
        "negative": True,
    },
    {
        "id": "r0000000003",
        "day": 20,
        "month": 10,
        "verb": "skipped",
        "thing": "GPU for the vault",
        "silent": True,
        "negative": True,
    },
    {
        "id": "r0000000004",
        "day": 19,
        "month": 10,
        "verb": "off",
        "thing": "away presence alerts",
        "silent": False,
        "negative": True,
    },
    {
        "id": "r0000000005",
        "day": 18,
        "month": 10,
        "verb": "scratched",
        "thing": "danceloop",
        "silent": True,
        "negative": True,
    },
]


def load() -> Any:
    spec = importlib.util.spec_from_file_location("conduct_hook", HOOK)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


C = load()


def items() -> list[dict[str, Any]]:
    out = []
    for r in LEDGER:
        for part in r["thing"].split(", "):
            out.append(
                {
                    "id": r["id"],
                    "verb": r["verb"],
                    "dm": f"{r['day']}.{r['month']}",
                    "thing": part,
                    "kw": C.thing_words(part),
                }
            )
    return out


# ------------------------------------------------------------------------------------------------ ledger matching
@pytest.mark.parametrize(
    "text,hit",
    [
        ("Want me to add a market ticker card to the board?", "market ticker card"),
        ("Left out: market ticker card (declined).", "market ticker card"),  # silent: no "(declined)" exemption
        ("Should I order a GPU for the vault?", "GPU for the vault"),
        ("New idea: danceloop v2", "danceloop"),
        ("The link digests are back?", "link digests"),
        ("Here is a digest of the link you sent.", None),  # 2-word item: order matters
        ("The market closed early; the ticker on the card was stale.", None),  # spread over the line, not named
        ("Link digests: removed on 22.10 as you said.", None),  # removed + says so on the line
        ("Away presence alerts are off since 19.10.", None),  # off + says so
        ("Turn the away presence alerts back on?", "away presence alerts"),
        ("GPU load is fine.\nThe vault is healthy.", None),  # words on different lines
    ],
)
def test_ledger_hits(text: str, hit: str | None) -> None:
    got = [h["thing"] for h in C.ledger_hits(text, items())]
    assert got == ([hit] if hit else [])


def test_ledger_owner_brought_it_up() -> None:
    kwh = set(C.text_hashes("what happened to the market ticker card?"))
    assert C.ledger_hits("The market ticker card was declined on 21.10.", items(), kwh) == []
    assert [h["thing"] for h in C.ledger_hits("Want a GPU for the vault?", items(), kwh)] == ["GPU for the vault"]


def test_ledger_fallback_from_facts(tmp_path: Path) -> None:
    facts = (
        "# Facts\n\n## Done / declined ledger\n- 21.10 declined: market ticker card (his poll)\n"
        "- 20.10 done: backups\n- 19.10 removed: link digests, gym coach\n\n## Other\n- 01.10 declined: nope\n"
    )
    rows = C.ledger_from_facts(facts)
    assert [(r["verb"], r["thing"]) for r in rows] == [
        ("declined", "market ticker card"),
        ("removed", "link digests, gym coach"),
    ]


# ------------------------------------------------------------------------------------------------ keyboard + length
def reply(text: str, buttons: Any = None, details: str | None = None) -> dict[str, Any]:
    inp: dict[str, Any] = {"chat_id": OWNER, "text": text, "format": "rich"}
    if buttons is not None:
        inp["buttons"] = buttons
    if details is not None:
        inp["details"] = details
    return inp


def rules(inp: dict[str, Any], tool: str = TP + "reply") -> list[str]:
    return [f["rule"] for f in C.sendlint(tool, inp, None, items())]


def test_keyboard_two_questions() -> None:
    assert rules(reply("Start the backup now?\nAnd move the old ones too?", [["Yes", "No"]])) == ["keyboard"]
    assert rules(reply("Start the backup now?\nAnd move the old ones too?")) == []  # no keyboard
    assert rules(reply("Start the backup now?", [["Yes", "No"], ["Later"]])) == []
    assert rules(reply("See https://x.example/a?b=1 and `x?`\n> old?\nGo?", [["Go"]])) == []  # url, code, quote
    assert rules(reply("Two? Questions?", [[{"text": "Docs", "url": "https://x.example"}]])) == []  # url button only


def test_keyboard_numbered_per_question() -> None:
    rows = [["1️⃣ Yes", "1️⃣ No"], ["2️⃣ Yes", "2️⃣ No"]]
    assert rules(reply("Two things:\n1️⃣ backup\n2️⃣ cleanup", rows)) == ["keyboard"]
    opts = [["1️⃣ Small"], ["2️⃣ Large"]]  # numbered options of one question
    assert rules(reply("Which size?", opts)) == []


def test_length() -> None:
    assert rules(reply("\n".join(f"line {i}" for i in range(13)))) == ["length"]
    assert rules(reply("\n".join(f"line {i}" for i in range(12)) + "\n\n\n")) == []
    table = "\n".join(["| a | b |", "|---|---|"] + [f"| {i} | x |" for i in range(12)])
    assert rules(reply(table)) == []  # a plan/schedule table
    assert rules({"chat_id": OWNER, "question": "Pick?", "options": ["market ticker card", "b"]}, TP + "send_poll") == [
        "ledger"
    ]


# ------------------------------------------------------------------------------------------------ inbox (C4)
@pytest.mark.parametrize(
    "cmd,hit",
    [
        ("rm ~/inbox/deals-hdd-2026-10-29.md", "rm"),
        ("rm -f $HOME/inbox/*.md", "rm"),
        ('rm -f "${HOME}/inbox/cue-x.md" 2>/dev/null', "rm"),
        ("cd ~/inbox && rm cue-a.md cue-b.md", "rm"),
        ("mv ~/inbox/cue-a.md /tmp/", "mv"),
        ("find ~/inbox -maxdepth 1 -name '*.md' -delete", "find -delete"),
        ("find ~/inbox -name 'x*.md' -exec rm {} \\;", "find -delete"),
        ("ls ~/inbox/*.md | xargs rm -f", "xargs rm"),
        ("python3 -c \"import os; os.remove('~/inbox/cue-a.md')\"", "script"),
        ("bash -c 'rm ~/inbox/cue-a.md'", "rm"),
        ("unlink ~/inbox/cue-a.md", "unlink"),
        ("rm ~/inbox/files/scan.pdf", ""),
        ("find ~/inbox/files -mtime +30 -delete", ""),
        ("mv /tmp/new.md ~/inbox/", ""),
        ("events ack 412 --note 'handled'", ""),
        ("grep -c rm ~/inbox/cue-a.md", ""),
        ("rm /tmp/test/inbox/a.md", ""),
        ("cat > /tmp/doc.md <<'EOF'\nnever rm ~/inbox/x.md by hand\nEOF", ""),
        ("ls ~/inbox | head; rm -f /tmp/x.md", ""),
        ("echo done # rm ~/inbox/x.md", ""),
    ],
)
def test_inbox(cmd: str, hit: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert C.inbox_hit(cmd) == hit
    assert C.inbox_hit(cmd.replace("~/inbox", f"{tmp_path}/inbox")) == hit


# ------------------------------------------------------------------------------------------------ ack-first (C2)
@pytest.mark.parametrize(
    "inp,slow",
    [
        ({"command": "cd ~/opt/assistant && .venv/bin/pytest -q tests/hooks"}, True),
        ({"command": "python3 -m pytest -q", "run_in_background": True}, False),
        ({"command": "assistant land my-branch"}, True),
        ({"command": "~/opt/x-tests/run.sh 2>&1 | tail -3"}, True),
        ({"command": "vault ask tg 'when is the dentist?'"}, True),
        ({"command": "sleep 30; ls"}, True),
        ({"command": "sleep 5"}, False),
        ({"command": "timeout 230 sh -c 'until test -e /tmp/x; do sleep 5; done'"}, True),
        ({"command": "grep -n pytest pyproject.toml"}, False),
        ({"command": "echo 'assistant land x' >> notes.txt"}, False),
        ({"command": "unraid-health --report", "timeout": 120000}, False),  # a big timeout alone: no signal
        ({"command": "ls", "timeout": 30000}, False),
        ({"command": "cat <<'EOF' > /tmp/a.sh\npytest -q\nEOF"}, False),
    ],
)
def test_slow_bash(inp: dict[str, Any], slow: bool) -> None:
    assert bool(C.slow_bash(inp)) is slow


def test_slow_hub_and_send_cmds() -> None:
    assert C.slow_hub("mcp__hub__vault_ask", {"source": "tg", "wait_s": 60})
    assert not C.slow_hub("mcp__hub__vault_ask", {"source": "tg"})
    assert C.slow_hub("mcp__hub__deploy_run", {"plan": False})
    assert not C.slow_hub("mcp__hub__deploy_run", {"plan": True})
    assert C.is_send_cmd("voice-reply 'on it, it comes on Telegram'")
    assert C.is_send_cmd("phone chat 'done'")
    assert not C.is_send_cmd("grep voice-reply ~/bin/x")


def test_classify(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    acc = tmp_path / "access.json"
    acc.write_text(json.dumps({"allowFrom": [OWNER]}))
    monkeypatch.setenv("CONDUCT_ACCESS", str(acc))
    tag = f'<channel source="plugin:telegram-plus:telegram" chat_id="{OWNER}" user_id="{OWNER}" ts="1"'
    assert C.classify(tag + ' event_type="message">hi</channel>')[0] == "owner"
    assert C.classify(tag + ' event_type="button" callback_data="x">y</channel>')[0] == "owner"
    assert C.classify(tag + ' event_type="reaction">👍</channel>')[0] == "other"
    assert C.classify(tag + ' event_type="message" handled="status"></channel>')[0] == "other"
    assert C.classify(tag.replace(f'user_id="{OWNER}"', 'user_id="999"') + ' event_type="message">x')[0] == "other"
    assert C.classify("[heartbeat] 2 new notes")[0] == "beat"
    assert C.classify("# Autonomous loop tick (dynamic pacing)\n...")[0] == "loop"
    assert C.classify("<task-notification><task-id>a1</task-id>")[0] == "other"


# ------------------------------------------------------------------------------------------------ end to end
@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    ws = home / "workspace"
    (home / "bin").mkdir(parents=True)
    ws.mkdir()
    (ws / "FACTS.md").write_text("# Facts\n")
    (ws / "TODO.md").write_text("# TODO\n\n## Due\n\n## In progress\n\n## Night backlog\n- **Prototype a**\n- **b**\n")
    (ws / "GOALS.md").write_text("# Goals\n- [ ] step one\n")
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (stubs / "ledger.json").write_text(json.dumps(LEDGER))
    (stubs / "agents.json").write_text(json.dumps({"agents": []}))

    def stub(name: str, body: str) -> str:
        p = stubs / name
        p.write_text("#!/bin/sh\n" + body + "\n")
        p.chmod(0o755)
        return str(p)

    acc = tmp_path / "access.json"
    acc.write_text(json.dumps({"allowFrom": [OWNER]}))
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "CONDUCT_ACCESS": str(acc),
        "CONDUCT_MEMORY_BIN": stub("memory", f'cat "{stubs}/ledger.json"'),
        "CONDUCT_AGENT_WATCH": stub("agent-watch", f'cat "{stubs}/agents.json"'),
        "CONDUCT_TASKS_BIN": stub("tasks", "echo '[]'"),
        "CONDUCT_WAKE_BIN": stub("wake", f'cat "{stubs}/wake.json" 2>/dev/null || echo "{{}}"'),
        "CONDUCT_BIN": str(HOOK),
        "BOARD_LINT_BIN": stub("board-lint", 'cat >/dev/null; echo \'{"hookSpecificOutput": {"x": 1}}\''),
        "PITCH_CHECK_LOG": str(tmp_path / "bl.log"),
        "CONDUCT_LIB": str(REPO),
        "CONDUCT_TODO": str(ws / "TODO.md"),
        "CONDUCT_GOALS": str(ws / "GOALS.md"),
        "STUBS": str(stubs),
        "CONDUCT_PROC_DIR": str(stubs / "proc"),  # no lands running unless a test writes one
    }


def ev(env: dict[str, str], name: str, **kw: Any) -> dict[str, Any]:
    ws = Path(env["HOME"]) / "workspace"
    base = {
        "session_id": "s-main",
        "transcript_path": f"{env['HOME']}/.claude/projects/x/s-main.jsonl",
        "cwd": str(ws),
        "hook_event_name": name,
        "permission_mode": "bypassPermissions",
    }
    base.update(kw)
    return base


def run(env: dict[str, str], cmd: str, payload: Any, script: Path = HOOK) -> str:
    argv = [PY, "-IS", str(HOOK), cmd] if script == HOOK else ["/bin/sh", str(script)]
    r = subprocess.run(argv, input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=20)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def owner_prompt(env: dict[str, str], text: str = "can you check the backups?") -> None:
    p = (
        f'<channel source="plugin:telegram-plus:telegram" chat_id="{OWNER}" message_id="7" user="x" '
        f'user_id="{OWNER}" ts="2026-10-29T08:00:00Z" event_id="1" event_type="message">\n{text}\n</channel>'
    )
    assert run(env, "prompt", ev(env, "UserPromptSubmit", prompt=p)) == ""


def holds(env: dict[str, str]) -> list[dict[str, Any]]:
    p = Path(env["HOME"]) / ".local/state/conduct/holds.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def test_ackfirst_deny_once_then_send_clears(env: dict[str, str]) -> None:
    owner_prompt(env)
    call = ev(env, "PreToolUse", tool_name="Bash", tool_input={"command": "pytest -q", "description": "tests"})
    out = json.loads(run(env, "pre", call))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "[conduct:ackfirst]" in out["hookSpecificOutput"]["permissionDecisionReason"]
    call["tool_input"]["description"] = "run the tests"  # same command, new description: still the same call
    assert run(env, "pre", call) == ""  # the identical retry goes through
    assert [(h["check"], h["act"]) for h in holds(env)] == [("ackfirst", "hold"), ("ackfirst", "override")]
    owner_prompt(env)
    react = ev(
        env, "PreToolUse", tool_name=TP + "react", tool_input={"chat_id": OWNER, "message_id": "7", "emoji": "✍"}
    )
    assert run(env, "pre", react) == ""
    assert run(env, "pre", call) == ""  # acked: slow work is fine now


def test_ackfirst_not_for_subagents_or_background(env: dict[str, str]) -> None:
    owner_prompt(env)
    sub = ev(
        env,
        "PreToolUse",
        tool_name="Bash",
        tool_input={"command": "pytest -q"},
        agent_id="a1",
        agent_type="general-purpose",
    )
    assert run(env, "pre", sub) == ""
    bg = ev(env, "PreToolUse", tool_name="Bash", tool_input={"command": "pytest -q", "run_in_background": True})
    assert run(env, "pre", bg) == ""
    beat = ev(env, "UserPromptSubmit", prompt="[heartbeat] tick")
    run(env, "prompt", beat)
    assert run(env, "pre", ev(env, "PreToolUse", tool_name="Bash", tool_input={"command": "pytest -q"})) == ""


def test_other_session_is_not_main(env: dict[str, str]) -> None:
    owner_prompt(env)
    other = ev(
        env,
        "PreToolUse",
        tool_name="Bash",
        tool_input={"command": "pytest -q"},
        cwd="/tmp",
        transcript_path="/tmp/p/-tmp/x.jsonl",
    )
    assert run(env, "pre", other) == ""


def test_inbox_any_caller_deny_once(env: dict[str, str]) -> None:
    call = ev(env, "PreToolUse", tool_name="Bash", tool_input={"command": "rm ~/inbox/cue-a.md"}, agent_id="a1")
    out = json.loads(run(env, "pre", call))
    assert "events ack" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert run(env, "pre", call) == ""


def test_inbox_monitor_command_like_bash(env: dict[str, str]) -> None:
    """A Monitor call's command is a shell script: the inbox check holds it once, like Bash."""
    cmd = "rm ~/inbox/cue-b.md; tail -f ~/.local/state/x.log"
    call = ev(env, "PreToolUse", tool_name="Monitor", tool_input={"command": cmd, "description": "x", "timeout_ms": 1})
    out = json.loads(run(env, "pre", call))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "[conduct:inbox]" in out["hookSpecificOutput"]["permissionDecisionReason"]
    call["tool_input"].update(description="y", timeout_ms=2)  # same command: still the same call
    assert run(env, "pre", call) == ""
    assert [(h["check"], h["act"]) for h in holds(env)] == [("inbox", "hold"), ("inbox", "override")]
    tail = {"command": "tail -f ~/inbox/files/x.log", "description": "x", "timeout_ms": 1}
    assert run(env, "pre", ev(env, "PreToolUse", tool_name="Monitor", tool_input=tail)) == ""
    ws = {"ws": {"url": "wss://example.org/s"}, "description": "x", "timeout_ms": 1}
    assert run(env, "pre", ev(env, "PreToolUse", tool_name="Monitor", tool_input=ws)) == ""


def test_monitor_is_not_held_by_ackfirst_or_ackslow(env: dict[str, str]) -> None:
    """Monitor starts a background watch and returns at once: never an ackfirst/ackslow hold, and not a send."""
    t0 = 1_800_000_000.0
    env["CONDUCT_NOW"] = str(t0)
    owner_prompt(env)
    env["CONDUCT_NOW"] = str(t0 + 30)
    for cmd in ("pytest -q", "assistant land x", "sleep 60; ls"):
        mon = ev(env, "PreToolUse", tool_name="Monitor", tool_input={"command": cmd, "description": "x"})
        assert run(env, "pre", mon) == "", cmd
    assert holds(env) == []
    bash = ev(env, "PreToolUse", tool_name="Bash", tool_input={"command": "pytest -q"})
    assert json.loads(run(env, "pre", bash))["hookSpecificOutput"]["permissionDecision"] == "deny"  # still waiting


def test_sendlint_via_pitch_check(env: dict[str, str]) -> None:
    owner_prompt(env, "how are the backups?")
    call = ev(
        env,
        "PreToolUse",
        tool_name=TP + "reply",
        tool_input=reply("Backups are fine.\nWant the market ticker card on the board too?"),
    )
    out = json.loads(run(env, "pre", call, PITCH))
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "market ticker card" in reason and "ledger 21.10" in reason
    assert json.loads(run(env, "pre", call, PITCH)) == {"hookSpecificOutput": {"x": 1}}  # resent: board-lint's turn
    ok = ev(env, "PreToolUse", tool_name=TP + "reply", tool_input=reply("Backups are fine."))
    assert json.loads(run(env, "pre", ok, PITCH)) == {"hookSpecificOutput": {"x": 1}}
    acts = [(h["rule"], h["act"], h.get("item")) for h in holds(env)]
    assert acts == [("ledger", "hold", "r0000000001"), ("ledger", "override", "r0000000001")]
    assert "market" not in (Path(env["HOME"]) / ".local/state/conduct/holds.jsonl").read_text()  # no text logged


def test_sendlint_owner_asked(env: dict[str, str]) -> None:
    owner_prompt(env, "why did we drop the market ticker card?")
    call = ev(
        env,
        "PreToolUse",
        tool_name=TP + "reply",
        tool_input=reply("The market ticker card was declined on 21.10 (your poll)."),
    )
    assert run(env, "pre", call) == ""


def test_shadow_and_switches(env: dict[str, str]) -> None:
    cfg = Path(env["HOME"]) / ".config/conduct"
    cfg.mkdir(parents=True)
    (cfg / "length-shadow").touch()
    call = ev(env, "PreToolUse", tool_name=TP + "reply", tool_input=reply("\n".join("x" * 20)))
    assert run(env, "pre", call) == ""
    assert [(h["rule"], h["act"]) for h in holds(env)] == [("length", "shadow")]
    (cfg / "off").touch()
    assert run(env, "pre", ev(env, "PreToolUse", tool_name="Bash", tool_input={"command": "rm ~/inbox/a.md"})) == ""


def test_idle_stop_once_per_beat(env: dict[str, str]) -> None:
    run(env, "prompt", ev(env, "UserPromptSubmit", prompt="[heartbeat] tick"))
    out = json.loads(run(env, "stop", ev(env, "Stop", stop_hook_active=False)))
    assert out["decision"] == "block" and "night 2" in out["reason"] and "goals 1" in out["reason"]
    assert run(env, "stop", ev(env, "Stop", stop_hook_active=True)) == ""
    assert run(env, "stop", ev(env, "Stop", stop_hook_active=False)) == ""  # once per beat
    run(env, "prompt", ev(env, "UserPromptSubmit", prompt="[heartbeat] tick"))
    Path(env["STUBS"], "agents.json").write_text(json.dumps({"agents": [{"id": "a1", "status": "running"}]}))
    assert run(env, "stop", ev(env, "Stop", stop_hook_active=False)) == ""  # an agent runs
    run(env, "prompt", ev(env, "UserPromptSubmit", prompt="[heartbeat] tick"))
    Path(env["STUBS"], "agents.json").write_text(json.dumps({"agents": []}))
    land = Path(env["STUBS"], "proc", "4242")
    land.mkdir(parents=True)
    (land / "cmdline").write_bytes(b"/usr/bin/python3\0-I\0/x/bin/assistant\0land\0my-branch\0")
    assert run(env, "stop", ev(env, "Stop", stop_hook_active=False)) == ""  # a land runs in the background
    (land / "cmdline").write_bytes(b"sleep\x0030\0")
    Path(env["STUBS"], "agents.json").write_text(json.dumps({"agents": []}))
    run(env, "prompt", ev(env, "UserPromptSubmit", prompt="# Autonomous loop tick (dynamic pacing)"))
    Path(env["STUBS"], "wake.json").write_text(json.dumps({"rules": [{"id": 3, "klass": "hold", "why": "x"}]}))
    assert run(env, "stop", ev(env, "Stop", stop_hook_active=False)) == ""  # parked on purpose
    Path(env["STUBS"], "wake.json").unlink()
    assert json.loads(run(env, "stop", ev(env, "Stop", stop_hook_active=False)))["decision"] == "block"  # loop tick
    owner_prompt(env)
    assert run(env, "stop", ev(env, "Stop", stop_hook_active=False)) == ""  # not a beat


def test_fail_open(env: dict[str, str]) -> None:
    r = subprocess.run(
        [PY, "-IS", str(HOOK), "pre"],
        input="not json",
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )
    assert r.returncode == 0 and r.stdout == ""
    env2 = dict(env, CONDUCT_STATE_DIR="/proc/nope")
    call = ev(env, "PreToolUse", tool_name="Bash", tool_input={"command": "rm ~/inbox/a.md"})
    assert run(env2, "pre", call) == ""  # state not writable: allow


def test_fast(env: dict[str, str]) -> None:
    owner_prompt(env)
    call = ev(env, "PreToolUse", tool_name="Bash", tool_input={"command": "git status --short"})
    t0 = time.monotonic()
    for _ in range(5):
        assert run(env, "pre", call) == ""
    assert (time.monotonic() - t0) / 5 < 1.0  # typical ~40 ms; generous for a loaded CI host


def test_holds_report(env: dict[str, str]) -> None:
    call = ev(env, "PreToolUse", tool_name="Bash", tool_input={"command": "rm ~/inbox/a.md"})
    run(env, "pre", call)
    run(env, "pre", call)
    r = subprocess.run([PY, "-IS", str(HOOK), "holds", "--json"], capture_output=True, text=True, env=env, timeout=20)
    assert json.loads(r.stdout)["counts"] == {"inbox": {"hold": 1, "override": 1}}
    assert os.access(HOOK, os.X_OK) and os.access(PITCH, os.X_OK)


def test_ackslow_holds_once_after_12s_without_a_send(env: dict[str, str]) -> None:
    t0 = 1_800_000_000.0
    env["CONDUCT_NOW"] = str(t0)
    owner_prompt(env)
    quick = ev(env, "PreToolUse", tool_name="Bash", tool_input={"command": "ls ~/reports"})
    env["CONDUCT_NOW"] = str(t0 + 5)
    assert run(env, "pre", quick) == ""  # 5 s in: fine
    env["CONDUCT_NOW"] = str(t0 + 14)
    out = json.loads(run(env, "pre", quick))
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny" and "waited 14 s" in reason
    other = ev(env, "PreToolUse", tool_name="Bash", tool_input={"command": "grep -n x ~/workspace/TODO.md"})
    assert run(env, "pre", other) == ""  # once per turn, not on every next call
    assert [(h["rule"], h["act"]) for h in holds(env)] == [("ackslow", "hold")]
    owner_prompt(env)  # a new turn starts the clock again
    env["CONDUCT_NOW"] = str(t0 + 30)
    react = ev(
        env, "PreToolUse", tool_name=TP + "react", tool_input={"chat_id": OWNER, "message_id": "7", "emoji": "✍"}
    )
    assert run(env, "pre", react) == ""
    env["CONDUCT_NOW"] = str(t0 + 60)
    assert run(env, "pre", other) == ""  # something was sent: not waiting


def test_ackslow_threshold_file_and_shadow(env: dict[str, str]) -> None:
    cfg = Path(env["HOME"]) / ".config/conduct"
    cfg.mkdir(parents=True)
    (cfg / "ack-after-s").write_text("30\n")
    t0 = 1_800_000_000.0
    env["CONDUCT_NOW"] = str(t0)
    owner_prompt(env)
    call = ev(env, "PreToolUse", tool_name="Bash", tool_input={"command": "ls"})
    env["CONDUCT_NOW"] = str(t0 + 20)
    assert run(env, "pre", call) == ""  # under the file's 30 s
    (cfg / "ackslow-shadow").write_text("")
    env["CONDUCT_NOW"] = str(t0 + 31)
    assert run(env, "pre", call) == ""  # shadow: logged, not held
    assert [(h["rule"], h["act"]) for h in holds(env)] == [("ackslow", "shadow")]


# ------------------------------------------------------------------------------------------------ status (C1)
@pytest.mark.parametrize(
    "line,want",
    [
        ("P3-2 is pending", ["notdone"]),
        ("P3-2 live since 28.10", ["done"]),
        ("The P3-2 jobs are not live yet", ["notdone"]),
        ("The P3-2 jobs aren’t deployed", ["notdone"]),
        ("alpha-sync landed 29.10 (a1b2c3d)", ["done"]),
        ("Waiting on your ✅ for the key deletes", ["notdone"]),
        ("Key deletes: needs your ✅", ["notdone"]),
        ("Is P3-2 live?", []),  # a question
        ("P3-2 will be live after the review", []),  # future
        ("Once C1 is live, C2 goes", []),  # conditional
        ("> P3-2 pending", []),  # a quoted line
        ("`pending` is a probe state", []),  # code span
        ("go-live on 2.11", []),
        ("the live call ended", []),
        ("fixed price, done-when probe", []),
        ("see https://x.example/pending", []),
        ("| P3-2 | live |", ["done"]),  # a table row is one clause
    ],
)
def test_status_claims(line: str, want: list[str]) -> None:
    got = [c[2] for _, _, cv in C.prose_lines(line) for s, e in C.clauses(cv) for c in C.claims(cv[s:e])]
    assert got == want


def test_status_claim_pairs_with_the_nearest_item() -> None:
    cv = "Rules audit: C2 live, C1 pending"
    cl = C.claims(cv)
    assert C.nearest(cl, cv.index("C2"), cv.index("C2") + 2, cv) == "done"
    assert C.nearest(cl, cv.index("C1"), cv.index("C1") + 2, cv) == "notdone"
    cv = "Rules review: done, H6 counters not live yet"  # nearer by characters, but behind a comma
    assert C.nearest(C.claims(cv), cv.index("H6"), cv.index("H6") + 2, cv) == "notdone"
    assert C.clauses("P3-2 live; key deletes pending") == [(0, 9), (11, 30)]


NOW = 1_793_350_800.0  # a fixed "now" (30.10.2026 around noon): the fixtures' log dates are relative to it
LONG_SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
HUB_SHA = "c0ffee1234567890abcdef1234567890abcdef12"


def c1_sources(root: Path, board_age: float = 60) -> dict[str, str]:
    """Synthetic reality sources (names invented): two lands, a hub release, three board items, logs."""
    root.mkdir(parents=True, exist_ok=True)
    deploys = root / "deploys.jsonl"
    rows = [
        {"op": "land", "branch": "alpha-sync", "ok": True, "sha": LONG_SHA, "ts": NOW - 86400},
        {"op": "land", "branch": "beta-fix-2", "ok": False, "why": "ci", "ts": NOW - 3600},
        {"op": "deploy", "target": "hub", "ok": True, "sha": HUB_SHA, "ts": NOW - 7200},
        {"op": "cron", "ok": True, "ts": NOW},
    ]
    deploys.write_text("".join(json.dumps(r) + "\n" for r in rows) + "not json\n")
    board = root / "board.json"
    feed = [
        {"id": "aa11bb", "section": "Due", "title": "Greeting voice swap", "probes": [
            {"spec": "file-exists /x/report.md", "state": "fail"}]},
        {"id": "cc22dd", "section": "In progress", "title": "🧭 Rules review (his ✅ msg 1, 29.10)", "probes": []},
        {"id": "ee33ff", "section": "Due", "title": "Backup restore drill", "probes": [
            {"spec": "file-exists /x/drill.md", "state": "pass"}]},
    ]  # fmt: skip
    board.write_text(json.dumps({"ts": NOW - board_age, "items": feed}))
    pro = root / "PROACTIVE.md"
    pro.write_text(
        "# Proactive log\n"
        "- 29.10 09:4x P3-2 critical jobs verified live (landed a1b2c3d + golive 28.10 19:58)\n"
        "- 28.10 22:10 Key deletes (§5 f): done (his ✅)\n"
        "- 01.09 10:00 Old alpha beta cleanup: done\n"
    )
    chg = root / "CHANGELOG.md"
    chg.write_text(
        "- 29.10 09:52 mistake: told him P3-2 + key deletes were pending from stale TODO lines (live since 28.10)\n"
        "- 28.10 18:00 H6 counters live\n"
        "- 29.10 11:00 H6 counters rolled back (broken)\n"
        "- 29.10 12:00 WP10 queued: not live yet\n"
    )
    return {
        "CONDUCT_DEPLOYS": str(deploys),
        "CONDUCT_BOARD_FEED": str(board),
        "CONDUCT_PROACTIVE": str(pro),
        "CONDUCT_CHANGELOG": str(chg),
        "CONDUCT_REPO": str(root / "no-repo"),
        "CONDUCT_NOW": str(NOW),
    }


@pytest.fixture
def c1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for k, v in c1_sources(tmp_path / "src").items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("CONDUCT_STATE_DIR", str(tmp_path / "state"))
    return tmp_path / "state"


def status(text: str, tool: str = TP + "reply", **inp: Any) -> list[tuple[str, str]]:
    found = C.status_findings(tool, dict(inp, text=text))
    return [(f["item"], f["msg"]) for f in found]


def rows(state: Path) -> list[dict[str, Any]]:
    p = state / "holds.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def test_status_reality_cache(c1: Path) -> None:
    by = {(it["kind"], it["key"]): it for it in C.reality()}
    assert by[("branch", "alpha-sync")]["state"] == "landed"
    assert by[("branch", "beta-fix-2")]["state"] == "failed"
    assert by[("release", LONG_SHA[:12])]["state"] == "landed"
    assert by[("release", HUB_SHA[:12])]["state"] == "live"
    assert by[("todo", "aa11bb")]["state"] == "pending" and by[("todo", "ee33ff")]["state"] == "done"
    assert by[("todo", "cc22dd")]["state"] == "unknown" and by[("todo", "cc22dd")]["aliases"] == ["🧭 Rules review"]
    closed = [it for it in by.values() if it["kind"] == "todo" and it["key"].startswith("p:")]
    assert [it["aliases"] for it in closed] == [["Key deletes"]]  # the 01.09 close is older than 14 days
    assert by[("plan", "P3-2")]["state"] == "live"  # the logged mistake's "pending" is narration, not a rollback
    assert by[("plan", "H6")]["state"] == "unknown"  # rolled back later
    assert ("plan", "WP10") not in by  # never logged live
    for it in by.values():
        assert set(it) >= {"key", "kind", "state"}
    cache = json.loads((c1 / "reality.json").read_text())
    assert cache["items"] and C.reality() == cache["items"]  # second call: the cache


def test_status_the_29_10_case(c1: Path) -> None:
    """Stale TODO lines said pending; P3-2 was logged live and the key deletes closed: both are named."""
    got = status("P3-2 and the key deletes are still pending your ✅")
    assert len(got) == 2 and got[0][0] == "P3-2" and got[1][0].startswith("p:")
    assert "“P3-2” is not done yet, but live" in got[0][1] and "PROACTIVE.md" in got[0][1]
    assert "“Key deletes” is not done yet, but done: closed 28.10 22:10" in got[1][1]
    assert status("P3-2 is live since 29.10, the key deletes are done") == []


def test_status_lands_board_and_releases(c1: Path) -> None:
    assert [i for i, _ in status("alpha-sync is still pending")] == ["alpha-sync"]
    assert status("alpha-sync landed") == []
    assert "last land failed" in status("beta-fix-2 landed 29.10")[0][1]
    assert status("beta-fix-2 is blocked on CI") == []
    assert "board probe fails: file-exists" in status("Greeting voice swap: done")[0][1]
    assert [i for i, _ in status("Backup restore drill still open")] == ["ee33ff"]
    assert [i for i, _ in status("Hub c0ffee1 is not deployed yet")] == [HUB_SHA[:12]]
    assert status("abcdef1 pending") == []  # unknown commit id
    assert status("Will alpha-sync land? Once it is live, beta-fix-2 goes.") == []
    got = status("All fine.", details="P3-2 pending")
    assert got and got[0][1].startswith("details line 1 says")


def test_status_unverified_and_no_text_logged(c1: Path) -> None:
    assert status("Rules review: done, H6 counters not live yet") == []
    r = rows(c1)
    assert [(x["act"], x["item"], x["claim"]) for x in r] == [
        ("unverified", "H6", "notdone"),  # plan ids, branches and commit ids first, then board titles
        ("unverified", "cc22dd", "done"),
    ]
    assert "review" not in (c1 / "holds.jsonl").read_text()


def test_status_stale_board_says_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in c1_sources(tmp_path / "src", board_age=7200).items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("CONDUCT_STATE_DIR", str(tmp_path / "state"))
    assert status("Greeting voice swap: done") == []
    assert rows(tmp_path / "state")[0]["act"] == "unverified"


def test_status_fails_open(c1: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any) -> list[Any]:
        raise RuntimeError("x")

    monkeypatch.setattr(C, "_status", boom)
    assert C.status_findings(TP + "reply", {"text": "P3-2 pending"}) == []
    assert [(x["check"], x["act"]) for x in rows(c1)] == [("status", "error")]
    monkeypatch.undo()
    monkeypatch.setenv("CONDUCT_STATE_DIR", str(c1))
    monkeypatch.setenv("CONDUCT_DEPLOYS", "/proc/nope/deploys.jsonl")  # every source missing: no evidence
    monkeypatch.setenv("CONDUCT_BOARD_FEED", "/proc/nope/board.json")
    monkeypatch.setenv("CONDUCT_PROACTIVE", "/proc/nope/P.md")
    monkeypatch.setenv("CONDUCT_CHANGELOG", "/proc/nope/C.md")
    monkeypatch.setenv("CONDUCT_GIT", "off")
    assert C.status_findings(TP + "reply", {"text": "P3-2 pending"}) == []


GIT = shutil.which("git")
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@x",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@x",
}


@pytest.mark.skipif(GIT is None, reason="no git")
def test_status_git_branch_check(c1: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    def g(*args: str, at: float = NOW - 10 * 86400) -> None:
        stamp = f"@{int(at)} +0000"
        env = dict(os.environ, **GIT_ENV, GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
        subprocess.run([str(GIT), "-C", str(repo), *args], check=True, capture_output=True, env=env)

    g("init", "-q", "-b", "main")
    g("commit", "-q", "--allow-empty", "-m", "base")
    g("branch", "eps-fresh")  # at main, no land: unknown
    g("checkout", "-q", "-b", "gamma-feature")
    g("commit", "-q", "--allow-empty", "-m", "work")  # not on main, no land: unlanded
    g("checkout", "-q", "-b", "alpha-sync", "main")
    g("commit", "-q", "--allow-empty", "-m", "pre-rebase", at=NOW - 2 * 86400)  # older than its land: landed
    g("checkout", "-q", "main")
    monkeypatch.setenv("CONDUCT_REPO", str(repo))
    monkeypatch.setenv("CONDUCT_GIT", str(GIT))
    assert "not on main and no land is logged" in status("gamma-feature landed")[0][1]
    assert status("gamma-feature is still pending") == []
    assert status("alpha-sync landed") == [] and [i for i, _ in status("alpha-sync pending")] == ["alpha-sync"]
    assert status("eps-fresh landed") == []  # on main without a land: unverified
    assert rows(c1)[-1]["item"] == "eps-fresh" and rows(c1)[-1]["act"] == "unverified"
    g("checkout", "-q", "alpha-sync")
    g("commit", "-q", "--allow-empty", "-m", "new work", at=NOW - 600)  # commits after the land
    assert "commits after its land" in status("alpha-sync is merged")[0][1]


def c1_env(env: dict[str, str], *switches: str) -> dict[str, str]:
    env = dict(env, **c1_sources(Path(env["STUBS"]) / "c1"))
    cfg = Path(env["HOME"]) / ".config/conduct"
    cfg.mkdir(parents=True, exist_ok=True)
    for s in switches:
        (cfg / s).touch()
    return env


def test_status_off_without_switch(env: dict[str, str]) -> None:
    env = dict(env, **c1_sources(Path(env["STUBS"]) / "c1"))
    call = ev(env, "PreToolUse", tool_name=TP + "reply", tool_input=reply("P3-2 is still pending."))
    assert json.loads(run(env, "pre", call, PITCH)) == {"hookSpecificOutput": {"x": 1}}
    assert holds(env) == []


def test_status_shadow_via_pitch_check(env: dict[str, str]) -> None:
    env = c1_env(env, "status-on")
    owner_prompt(env, "where are we with the jobs?")
    call = ev(env, "PreToolUse", tool_name=TP + "reply", tool_input=reply("P3-2 is still pending."))
    assert json.loads(run(env, "pre", call, PITCH)) == {"hookSpecificOutput": {"x": 1}}  # never held: shadow
    assert [(h["check"], h["act"], h["item"], h["claim"], h["state"]) for h in holds(env)] == [
        ("status", "shadow", "P3-2", "notdone", "live")
    ]
    assert "pending" not in (Path(env["HOME"]) / ".local/state/conduct/holds.jsonl").read_text()
    for tool, inp in (
        (TP + "edit_message", {"chat_id": OWNER, "message_id": "9", "text": "alpha-sync: still queued"}),
        ("mcp__hub__phone_chat", {"text": "Backup restore drill is not done yet"}),
    ):
        assert run(env, "pre", ev(env, "PreToolUse", tool_name=tool, tool_input=inp)) == ""
    assert [(h["tool"], h["item"]) for h in holds(env)][1:] == [
        ("tg:edit_message", "alpha-sync"),
        ("mcp__hub__phone_chat", "ee33ff"),
    ]


def test_status_enforce_holds_once(env: dict[str, str]) -> None:
    env = c1_env(env, "status-on", "status-enforce")
    call = ev(env, "PreToolUse", tool_name=TP + "reply", tool_input=reply("P3-2 is still pending."))
    out = json.loads(run(env, "pre", call, PITCH))
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert reason.startswith("[conduct:status] Held once: line 1 says “P3-2” is not done yet, but live")
    assert json.loads(run(env, "pre", call, PITCH)) == {"hookSpecificOutput": {"x": 1}}  # identical resend
    assert [h["act"] for h in holds(env)] == ["hold", "override"]
    (Path(env["HOME"]) / ".config/conduct/status-shadow").touch()  # shadow wins over enforce
    call["tool_input"] = reply("P3-2 is pending.")
    assert json.loads(run(env, "pre", call, PITCH)) == {"hookSpecificOutput": {"x": 1}}
    assert holds(env)[-1]["act"] == "shadow"


def test_status_fast(env: dict[str, str]) -> None:
    env = c1_env(env, "status-on")
    call = ev(env, "PreToolUse", tool_name=TP + "reply", tool_input=reply("alpha-sync landed, P3-2 live."))
    t0 = time.monotonic()
    for _ in range(5):
        assert run(env, "pre", call) == ""
    assert (time.monotonic() - t0) / 5 < 1.0  # typical ~50 ms with a cache rebuild; generous for a loaded CI host
