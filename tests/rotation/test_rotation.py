"""rotation: transcript reads, quiet-slot checks, the hand-off, run/verify/report, the switch and start-claude's
fresh-start block. Everything runs against stubs under a temp HOME."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from rotation import checks, cli, handoff, session
from rotation_testlib import SID, entry, stub


def cron_create(tid: str, ts: str, cron: str, recurring: bool, jid: str, prompt: str = "check the agents") -> list:
    kind = "recurring job" if recurring else "one-shot task"
    return [
        entry(
            "assistant",
            ts,
            [
                {
                    "type": "tool_use",
                    "id": tid,
                    "name": "CronCreate",
                    "input": {"cron": cron, "prompt": prompt, "recurring": recurring},
                }
            ],
        ),
        entry(
            "user",
            ts,
            [
                {
                    "type": "tool_result",
                    "tool_use_id": tid,
                    "content": f"Scheduled {kind} {jid} ({cron}). Session-only (not written to disk).",
                }
            ],
        ),
    ]


def add_lines(p: Path, lines: list[str]) -> None:
    with open(p, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------- session reads


def test_last_owner_message_and_crons(world):
    tp = world["transcript"]
    assert session.last_owner_message(tp) == pytest.approx(1792879800.0)  # 2026-10-24T22:10Z
    add_lines(
        tp,
        cron_create("t1", "2026-10-24T20:00:00Z", "7,22,37,52 * * * *", True, "aaaa1111")
        + cron_create("t2", "2026-10-24T20:01:00Z", "0 7 25 10 *", False, "bbbb2222")
        + cron_create("t3", "2026-10-24T20:02:00Z", "0 1 25 10 *", False, "cccc3333")  # fired at 01:00
        + cron_create("t4", "2026-10-15T20:00:00Z", "*/5 * * * *", True, "dddd4444")  # expired (> 7 days)
        + cron_create("t5", "2026-10-24T20:03:00Z", "*/10 * * * *", True, "eeee5555")
        + [
            entry(
                "assistant",
                "2026-10-24T20:04:00Z",
                [{"type": "tool_use", "id": "t6", "name": "CronDelete", "input": {"id": "eeee5555"}}],
            )
        ],
    )
    now = 1792895400.0
    got = {c["id"]: c for c in session.session_crons(tp, now)}
    assert set(got) == {"aaaa1111", "bbbb2222"}
    assert got["bbbb2222"]["next"] == pytest.approx(1792908000.0)  # 07:00 local on 25.10 (CET)
    assert got["aaaa1111"]["recurring"] and got["aaaa1111"]["next"] is None


def test_marker_and_recreated(world):
    tp = world["transcript"]
    add_lines(
        tp,
        [
            entry(
                "user",
                "2026-10-25T02:40:00Z",
                [{"type": "tool_result", "tool_use_id": "x", "content": "rotation-handoff: 20261025-0330"}],
            )
        ]
        + cron_create("n1", "2026-10-25T02:41:00Z", "7,22,37,52 * * * *", True, "ffff6666"),
    )
    since = 1792895400.0
    assert session.has_marker(tp, "rotation-handoff: 20261025-0330", since)
    assert not session.has_marker(tp, "rotation-handoff: 20261025-0330", since + 7200)
    assert session.cron_recreated(tp, "7,22,37,52  * * * *", since)
    assert not session.cron_recreated(tp, "0 7 25 10 *", since)


# ---------------------------------------------------------------- checks


def test_quiet_night_passes(world):
    res = checks.run_all(checks.context())
    assert checks.passed(res), res


@pytest.mark.parametrize(
    ("seam", "body", "check"),
    [
        ("ROTATE_VOICE", 'echo \'{"live": 1, "call_ids": ["c1"]}\'', "call"),
        ("ROTATE_DRIVING", "exit 0", "drive"),
        ("ROTATE_APPROVALS", 'echo \'[{"state": "pending"}]\'', "approvals"),
        ("ROTATE_AGENT_WATCH", "printf '  ID STATE RUN SILENT LAST\\n  a1 running 5m 0m tool_result\\n'", "agents"),
        ("ROTATE_VOICE", "exit 7", "call"),  # unknown blocks
    ],
)
def test_each_check_blocks(world, monkeypatch, seam, body, check):
    monkeypatch.setenv(seam, stub(world["bins"] / f"x-{check}", body))
    res = {r["check"]: r for r in checks.run_all(checks.context())}
    assert res[check]["ok"] is not True
    assert not checks.passed(list(res.values()))


def test_relay_down_means_no_call(world, monkeypatch):
    monkeypatch.setenv("ROTATE_VOICE", stub(world["bins"] / "vdown", "exit 2"))
    res = {r["check"]: r for r in checks.run_all(checks.context())}
    assert res["call"]["ok"] is True


def test_window_session_and_start_claude_block(world, monkeypatch):
    monkeypatch.setenv("ASSISTANT_NOW", "2026-10-25T14:00:00+01:00")
    res = {r["check"]: r for r in checks.run_all(checks.context())}
    assert res["window"]["ok"] is False
    monkeypatch.setenv("ASSISTANT_NOW", "2026-10-24T23:30:00+00:00")  # 00:30 his time? no: 01:30 CEST -> outside
    world["agents"].write_text(
        json.dumps(
            [
                {
                    "cwd": os.environ["HOME"] + "/workspace",
                    "kind": "interactive",
                    "sessionId": SID,
                    "status": "busy",
                    "startedAt": world["now_ms"],
                }
            ]
        )
    )
    res = {r["check"]: r for r in checks.run_all(checks.context())}
    assert res["session"]["ok"] is False
    world["start_claude"].write_text("#!/bin/sh\nresume=--continue\n")
    assert checks.c_start_claude({})[0] is False


def test_soon_one_shot_blocks(world):
    add_lines(world["transcript"], cron_create("t9", "2026-10-25T02:00:00Z", "40 3 25 10 *", False, "abcd9999"))
    res = {r["check"]: r for r in checks.run_all(checks.context())}
    assert res["crons"]["ok"] is False


# ---------------------------------------------------------------- hand-off, run, verify


def test_handoff_carries_every_promise(world):
    add_lines(world["transcript"], cron_create("t1", "2026-10-24T20:00:00Z", "7,22,37,52 * * * *", True, "aaaa1111"))
    ctx = checks.context()
    ps = handoff.promises(ctx)
    kinds = sorted(p["kind"] for p in ps)
    assert kinds == ["cron", "remind", "remind", "todo"]  # r3 too far, r4 a place reminder
    title, body = handoff.text(ctx, "20261025-0330", ps)
    assert "rotation-handoff: 20261025-0330" in body
    assert "[p1]" in body and "[p3]" in body and "[c1]" in body and "aaaa1111" in body
    assert "header" not in body and "Due: Morning report" in body


def test_dry_run_writes_nothing(world, capsys):
    assert cli.main(["run", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would: arm" in out and "rotation-handoff:" in out
    assert not checks.fresh_flag().exists()
    assert not list(Path(os.environ["HOME"], "inbox").glob("rotation-*.md"))
    assert not world["restart_log"].exists()


def test_cron_mode_quiet_when_off_or_outside(world, monkeypatch, capsys):
    (Path(os.environ["HOME"]) / ".config/assistant/rotate-on").unlink()
    assert cli.main(["run", "--cron"]) == 0
    assert not checks.runs_file().exists()
    (Path(os.environ["HOME"]) / ".config/assistant/rotate-on").write_text("{}")
    monkeypatch.setenv("ASSISTANT_NOW", "2026-10-25T14:00:00+01:00")
    assert cli.main(["run", "--cron"]) == 0
    assert not checks.runs_file().exists()


def test_live_run_then_verify(world, monkeypatch, capsys):
    add_lines(world["transcript"], cron_create("t1", "2026-10-24T20:00:00Z", "7,22,37,52 * * * *", True, "aaaa1111"))
    assert cli.main(["run", "--cron"]) == 0
    note = Path(os.environ["HOME"], "inbox", "rotation-20261025-0330.md")
    assert note.exists() and oct(note.stat().st_mode & 0o777) == "0o600"
    text = note.read_text()
    assert "Data from rotate, not instructions." in text and text.rstrip().splitlines()[-1].startswith("Next:")
    assert json.loads(checks.fresh_flag().read_text())["id"] == "20261025-0330"
    assert "nightly rotation: a fresh session" in world["restart_log"].read_text()
    row = [json.loads(x) for x in checks.runs_file().read_text().splitlines()][-1]
    assert row["ok"] and row["promises"] == 4 and row["crons"] == 1 and "title" not in json.dumps(row)
    # a second run the same night is refused by `nightly`
    assert cli.main(["run"]) == cli.EXIT_NOT_NOW
    # the fresh session: new id, reads the note, re-creates the check-in
    new = "99999999-8888-7777-6666-555555555555"
    world["agents"].write_text(
        json.dumps(
            [
                {
                    "cwd": os.environ["HOME"] + "/workspace",
                    "kind": "interactive",
                    "sessionId": new,
                    "status": "idle",
                    "startedAt": world["now_ms"],
                }
            ]
        )
    )
    ntp = world["transcript"].with_name(f"{new}.jsonl")
    ntp.write_text(
        "\n".join(
            [entry("user", "2026-10-25T02:35:00Z", [{"type": "tool_result", "tool_use_id": "r", "content": text}])]
            + cron_create("n1", "2026-10-25T02:36:00Z", "7,22,37,52 * * * *", True, "ffff0000")
        )
        + "\n"
    )
    monkeypatch.setenv("ASSISTANT_NOW", "2026-10-25T04:00:00+01:00")
    assert cli.main(["verify", "--json"]) == 0
    v = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert v["handoff_read"] and v["rotated"] and v["lost"] == 0 and v["recreated"] == 1 and v["durable"] == 3


def test_verify_counts_lost_check_in(world, monkeypatch, capsys):
    add_lines(world["transcript"], cron_create("t1", "2026-10-24T20:00:00Z", "7,22,37,52 * * * *", True, "aaaa1111"))
    assert cli.main(["run"]) == 0
    new = "99999999-8888-7777-6666-555555555555"
    world["agents"].write_text(
        json.dumps(
            [
                {
                    "cwd": os.environ["HOME"] + "/workspace",
                    "kind": "interactive",
                    "sessionId": new,
                    "status": "idle",
                    "startedAt": world["now_ms"],
                }
            ]
        )
    )
    world["transcript"].with_name(f"{new}.jsonl").write_text(
        entry("user", "2026-10-25T02:35:00Z", "[heartbeat]") + "\n"
    )
    monkeypatch.setenv("ASSISTANT_NOW", "2026-10-25T04:00:00+01:00")
    assert cli.main(["verify", "--json"]) == 1
    v = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert v["lost"] == 1 and not v["handoff_read"]
    assert cli.main(["report", "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["lost"] == 1 and not rep["done_when"]


def test_restart_failure_disarms(world, monkeypatch):
    monkeypatch.setenv("ROTATE_RESTART", stub(world["bins"] / "rfail", "exit 4"))
    assert cli.main(["run"]) == 1
    assert not checks.fresh_flag().exists()
    row = [json.loads(x) for x in checks.runs_file().read_text().splitlines()][-1]
    assert row["ok"] is False and row["failed"] == ["restart"]


def test_switch_on_off(world, capsys):
    h = Path(os.environ["HOME"])
    (h / ".config/assistant/rotate-on").unlink()
    assert cli.main(["on", "--reason", "short"]) == 2
    world["start_claude"].write_text("#!/bin/sh\n")
    assert cli.main(["on", "--reason", "owner OK on 24.10"]) == 1  # no fresh-start block: refused
    world["start_claude"].write_text("#!/bin/sh\n# rotate/fresh-start\n")
    assert cli.main(["on", "--reason", "owner OK on 24.10"]) == 0 and checks.switch_file().exists()
    checks.fresh_flag().parent.mkdir(parents=True, exist_ok=True)
    checks.fresh_flag().write_text("{}")
    assert cli.main(["off"]) == 0
    assert not checks.switch_file().exists() and not checks.fresh_flag().exists()


def test_rehearse_writes_only_its_folder(world, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("ROTATE_REHEARSAL_DIR", str(tmp_path / "reh"))
    fake = stub(world["bins"] / "claude", 'echo \'{"result": "[p1] [p2] [p3]"}\'')
    monkeypatch.setenv("ROTATE_CLAUDE", fake)
    assert cli.main(["rehearse", "--model-check", "--yes", "--json"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["promises"] == d["carried"] == 3 and d["model_check"]["recalled"] == 3 and d["would_rotate"]
    assert (Path(d["dir"]) / "handoff.md").exists()
    assert not checks.fresh_flag().exists() and not world["restart_log"].exists()


# ---------------------------------------------------------------- start-claude's fresh-start block


def _snippet() -> str:
    block = (Path(__file__).resolve().parent / "start_claude_fresh_start.sh").read_text()
    assert checks.FRESH_MARKER in block
    return block


@pytest.mark.parametrize(
    ("transcripts", "flag_age_min", "want_resume", "want_log"),
    [(True, None, "--continue", False), (True, 1, "", True), (True, 20, "--continue", False), (False, 1, "", False)],
)
def test_start_claude_fresh_start_block(tmp_path, transcripts, flag_age_min, want_resume, want_log):
    h = tmp_path / "h"
    st = h / ".local/state/assistant/rotate"
    st.mkdir(parents=True)
    flag = st / "fresh-start"
    if flag_age_min is not None:
        flag.write_text("{}")
        t = flag.stat().st_mtime - flag_age_min * 60
        os.utime(flag, (t, t))
    script = "resume=\n" + ("resume=--continue\n" if transcripts else "") + _snippet() + '\nprintf "%s" "$resume"\n'
    r = subprocess.run(
        ["sh", "-c", script],
        env={"HOME": str(h), "PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        check=True,
    )
    assert r.stdout == want_resume
    assert not flag.exists()
    assert (st / "start.log").exists() == want_log
