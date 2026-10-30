#!/usr/local/bin/python3.13 -I
"""Tests for board-lint: each check flags its replayed case and stays silent on a clean board, the --beat line and its
dedupe/ack, the pitch hook (context only, never a block; marks Waiting items asked), the hand-off, fail-open, speed,
and that the real state files are untouched; the reality checks (landed / gone / live / logged against a temp git
repo, land log, schedules output and blame), the hourly --note and `mark`. Synthetic fixtures only."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

BIN = os.environ.get("BOARD_LINT_BIN") or "/home/node/bin/board-lint"
_TREE_HOOKS = Path(__file__).resolve().parents[3] / "hooks"


def hook(name):
    """A hook wrapper under test: BOARD_LINT_HOOKS/<name>, else this tree's hooks/<name> (what a deploy installs),
    else the live one; None where there is none (a wrapper the tree does not carry, on a CI runner)."""
    if os.environ.get("BOARD_LINT_HOOKS"):
        return Path(os.environ["BOARD_LINT_HOOKS"]) / name
    for d in (_TREE_HOOKS, Path("/home/node/.claude/hooks")):
        if (d / name).is_file():
            return d / name
    return None


REAL = ["/home/node/.local/state/board-lint/state.json"]
NOW = "2026-10-19 18:00"

FACTS = """# Facts
## Server
- Some fact — source, 19.10

## Done / declined ledger
- 19.10 declined: 1080p copies for remote streaming (fast uplink)
- 19.10 declined: doorbell → HA (also the app route) — [[doorbell-declined]]
- 19.10 skipped: money card (vault; the tracker is enough)
- 19.10 done: Disk 12 added (a free bay is left)
- 19.10 done: vault privacy fixes deployed (abc1234)
- 19.10 removed: Startfest Nights event + train (his request)
- 19.10 removed: old NetCtl 9.4.19 controller container (by him)
- 18.10 decided: parts watch on
- 13.10 declined: bill fields from mail via the vault
- 13.10 off: trip-guard cron
"""
CLEAN_TODO = """# TODO
Intro line with today in prose is not an item.

## Due
- **Conditions brief** — 13.11 18:00, one message 14.11

## In progress
- **Subtitle cleanup** — fewer languages (~1 h)

## Waiting on you
- **Shop order** — waiting for his login (asked 19.10 09:00)
- **Lamp remote** — re-pair or delete? (asked 18.10 08:50)

## Ideas
- **Wake light** — staged package, needs OK

## Night backlog
- **Stack research** — one item per night
"""
CLEAN_GOALS = """# Goals
**Next up (across goals)**
1. G3 Fix the warning: find what it flags (19.10).
Unblocked queue after that: G1 import sorting → G5 ideas refill.

## 1. Admin
In place: daily scan; bill fields from mail: declined.
- [ ] Import sorting: count docs that lack a type.
- [x] Trip-time reminders: shifted (19.10).

## 3. Server
- [ ] Spending import: the sync already runs in the vault bot; next = his first export **[waiting on owner]**.
"""
CLEAN_PROBLEMS = """# Problems
Last discovery question asked: 2026-10-18.
- **Array filling up** (solving): Disk 12 added 19.10.
- **Old controller** — solved 17.10: old 9.4.19 controller container removed by him 19.10.
"""
CLEAN_PROACTIVE = """# Proactive log
- 2026-10-19 06:00 dispatcher: verb staged (17 tests), not installed
- 2026-10-19 10:41 Lamp remote: asked him again
- 2026-10-01 10:00 Shop order placed and paid (too old to count)
"""
CLEAN_REMIND = [
    {
        "at": "2026-10-24 18:00",
        "owner": False,
        "text": "NetCtl cleanup ask: remove the stopped netctl-10 container; the old 9.4.19 one is already "
        "gone (he removed it 19.10).",
    },
    {"at": "2026-12-01 09:00", "owner": False, "text": "Next disk: check the parts watch results; pitch."},
]
CLEAN_WAITING = {
    "Shop order": {"since": "2026-10-18", "asked": None},
    "Lamp remote": {"since": "2026-10-10", "asked": "2026-10-18"},
}


def sha(p):
    try:
        return hashlib.sha256(Path(p).read_bytes()).hexdigest()
    except OSError:
        return None


def feed(waiting):
    """The board feed (~/.local/state/board/board.json) for {title: {'since', 'asked'}}: created / asked at 12:00."""

    def ts(d):
        return datetime.strptime(d + " 12:00", "%Y-%m-%d %H:%M").timestamp() if d else None

    return {
        "ts": 0,
        "mode": "owner",
        "items": [
            {"id": f"w{n}", "section": "Waiting on you", "title": t, "created": ts(v["since"]), "asked": ts(v["asked"])}
            for n, (t, v) in enumerate(waiting.items())
        ],
    }


class Base(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="board-lint-test-"))
        self.ws = self.d / "ws"
        self.ws.mkdir()
        self.files = {
            "TODO": self.ws / "TODO.md",
            "GOALS": self.ws / "GOALS.md",
            "PROBLEMS": self.ws / "PROBLEMS.md",
            "FACTS": self.ws / "FACTS.md",
            "PROACTIVE": self.ws / "PROACTIVE.md",
        }
        self.remind = self.d / "reminders.json"
        self.waiting = self.d / "board.json"
        self.queue = self.d / "board" / "asked.jsonl"
        self.state = self.d / "st" / "state.json"
        self.write()
        self.env = dict(
            os.environ,
            HOME=str(self.d / "home"),
            BOARD_LINT_WORKSPACE=str(self.ws),
            BOARD_LINT_REMIND_DB=str(self.remind),
            BOARD_LINT_BOARD_FEED=str(self.waiting),
            BOARD_LINT_ASKED_QUEUE=str(self.queue),
            BOARD_LINT_STATE=str(self.state),
            BOARD_LINT_LOG=str(self.d / "log"),
            BOARD_LINT_NOW=NOW,
        )
        for k, p in self.files.items():
            self.env[f"BOARD_LINT_{k}"] = str(p)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def write(
        self,
        todo=CLEAN_TODO,
        goals=CLEAN_GOALS,
        problems=CLEAN_PROBLEMS,
        facts=FACTS,
        proactive=CLEAN_PROACTIVE,
        remind=CLEAN_REMIND,
        waiting=CLEAN_WAITING,
    ):
        for k, v in (
            ("TODO", todo),
            ("GOALS", goals),
            ("PROBLEMS", problems),
            ("FACTS", facts),
            ("PROACTIVE", proactive),
        ):
            if v is None:
                self.files[k].unlink(missing_ok=True)
            else:
                self.files[k].write_text(v, encoding="utf-8")
        self.remind.write_text(json.dumps(remind), encoding="utf-8")
        self.waiting.write_text(json.dumps(feed(waiting)), encoding="utf-8")

    def run_bl(self, *args, stdin="", env=None, now=None):
        e = dict(env or self.env)
        if now:
            e["BOARD_LINT_NOW"] = now
        p = subprocess.run([BIN, *args], input=stdin.encode(), capture_output=True, timeout=20, env=e)
        return p.returncode, p.stdout.decode(), p.stderr.decode()

    def findings(self):
        rc, out, err = self.run_bl("--json")
        self.assertEqual(rc, 0, err)
        return json.loads(out)

    def checks(self):
        return [(f["check"], f["title"]) for f in self.findings()]


class Clean(Base):
    def test_clean_board_is_silent(self):
        self.assertEqual(self.findings(), [])
        rc, out, _ = self.run_bl()
        self.assertEqual((rc, out.strip()), (0, "board-lint: clean"))
        self.assertEqual(self.run_bl("--strict")[0], 0)
        self.assertEqual(self.run_bl("--beat")[1], "")

    def test_missing_files_are_fine(self):
        self.write(todo=None, goals=None, problems=None, facts=None, proactive=None, remind=[], waiting={})
        self.assertEqual(self.findings(), [])
        self.assertEqual(self.run_bl("--beat"), (0, "", ""))


class Replays(Base):
    """The audit's cases, renamed: each must be flagged."""

    def test_deploy_logged_but_still_waiting(self):
        todo = CLEAN_TODO.replace("## Ideas", "- **Vault privacy fixes** — ship it? (sent 18.10 09:00)\n\n## Ideas")
        proactive = CLEAN_PROACTIVE + "- 2026-10-19 10:40 vault abc1234 deployed (his ok): privacy nets + fixes\n"
        self.write(todo=todo, proactive=proactive, facts=FACTS.replace("- 19.10 done: vault privacy", "- 19.10 x: v"))
        self.assertIn(("log", "Vault privacy fixes"), self.checks())

    def test_done_in_ledger_but_still_waiting(self):
        todo = CLEAN_TODO.replace("## Ideas", "- **Disk day** — add the 10 TB as Disk 12 when home\n\n## Ideas")
        self.write(todo=todo)
        f = [x for x in self.findings() if x["title"] == "Disk day"]
        self.assertEqual([x["check"] for x in f], ["ledger"])
        self.assertIn("Disk 12 added", f[0]["ref"])

    def test_skipped_item_still_in_queue(self):
        goals = CLEAN_GOALS.replace("G5 ideas refill.", "G5 ideas refill → G2 money card proposal.")
        self.write(goals=goals)
        self.assertIn(("ledger", "G2 money card proposal"), self.checks())

    def test_reminder_about_removed_event(self):
        rem = CLEAN_REMIND + [
            {
                "at": "2026-10-30 10:00",
                "owner": False,
                "text": "Startfest Nights 06.11: check the program; fix the calendar event times and the train leg.",
            }
        ]
        self.write(remind=rem)
        self.assertIn("ledger", [f["check"] for f in self.findings() if f["src"] == "reminder"])

    def test_reminder_about_removed_container(self):
        rem = [
            {
                "at": "2026-10-24 18:00",
                "owner": False,
                "text": "NetCtl cleanup ask (one poll): remove stopped "
                "netctl-10 and old netctl-controller-reborn (9.4.19) + its appdata.",
            }
        ]
        self.write(remind=rem)
        self.assertEqual([f["check"] for f in self.findings() if f["src"] == "reminder"], ["ledger"])

    def test_declined_item_back_in_ideas(self):
        todo = CLEAN_TODO.replace(
            "## Night backlog", "- **Remote copies** — make 1080p copies for remote streaming\n\n## Night backlog"
        )
        self.write(todo=todo)
        self.assertIn(("ledger", "Remote copies"), self.checks())

    def test_in_place_line_names_removed_event(self):
        goals = CLEAN_GOALS.replace(
            "In place: daily scan;", "In place: daily scan; Startfest Nights 06.11 + train in the calendar;"
        )
        self.write(goals=goals)
        self.assertIn(("ledger", "In place"), self.checks())

    def test_waiting_too_long_never_asked(self):
        w = dict(CLEAN_WAITING, **{"Shop order": {"since": "2026-10-10", "asked": None}})
        self.write(waiting=w)
        f = [x for x in self.findings() if x["check"] == "age"]
        self.assertEqual([x["title"] for x in f], ["Shop order"])
        self.assertIn("9 d", f[0]["ref"])

    def test_relative_times(self):
        todo = CLEAN_TODO.replace("13.11 18:00, one message 14.11", "18:30 today").replace(
            "(asked 19.10 09:00)", "(asked 09:00)"
        )
        self.write(todo=todo)
        rel = sorted(t for c, t in self.checks() if c == "relative")
        self.assertEqual(rel, ["Conditions brief", "Shop order"])

    def test_duplicates(self):
        todo = (
            CLEAN_TODO.replace("## In progress", "- **Import round 1** — 18:30 19.10\n\n## In progress")
            .replace("## Night backlog", "- **Import round 1** — goes out 19.10\n\n## Night backlog")
            .replace("- **Wake light**", "- **Trip-time reminders** — shift them\n- **Wake light**")
        )
        goals = CLEAN_GOALS.replace(
            "Unblocked queue after that: G1", "Unblocked queue after that: G4 trip-time reminders → G1"
        )
        self.write(todo=todo, goals=goals)
        dups = sorted(t for c, t in self.checks() if c == "dup")
        self.assertEqual(dups, ["G4 trip-time reminders", "Import round 1", "Trip-time reminders"])


class Acks(Base):
    def test_item_with_ledger_date_or_word_or_already_is_fine(self):
        todo = CLEAN_TODO.replace(
            "## Ideas",
            "- **Disk day** — Disk 12 added 19.10, check parity\n- **Card** — money card: skipped, fine\n\n## Ideas",
        )
        self.write(todo=todo)
        self.assertEqual(self.findings(), [])

    def test_naming_word_required(self):
        rem = [
            {
                "at": "2026-10-28 18:00",
                "owner": False,
                "text": "Send the club plan (events folder): last train and night trains home.",
            }
        ]
        self.write(remind=rem)
        self.assertEqual(self.findings(), [])

    def test_negated_done_word_ignored(self):
        goals = CLEAN_GOALS + "- [ ] Dispatcher verb: staged, tests pass **[waiting on owner]**.\n"
        self.write(goals=goals)
        self.assertEqual(self.findings(), [])

    def test_positive_entries_only_for_waiting_items(self):
        todo = CLEAN_TODO.replace(
            "## Night backlog", "- **Parts watch** — tune the parts watch queries\n\n## Night backlog"
        )
        self.write(todo=todo)
        self.assertEqual(self.findings(), [])


class Beat(Base):
    def setUp(self):
        super().setUp()
        todo = CLEAN_TODO.replace("13.11 18:00, one message 14.11", "18:30 today").replace(
            "## Ideas", "- **Disk day** — add the 10 TB as Disk 12\n\n## Ideas"
        )
        self.write(todo=todo)

    def test_one_line_then_quiet_then_again_after_a_day(self):
        rc, out, _ = self.run_bl("--beat")
        self.assertEqual(rc, 0)
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.assertTrue(out.startswith("[board-lint] 2 stale board item(s): "), out)
        self.assertLessEqual(len(out.strip()), 420)
        self.assertNotRegex(out, r"\d+ new note\(s\)")  # never confuses the heartbeat mod's note count
        self.assertEqual(self.run_bl("--beat")[1], "", "same findings: silent")
        self.assertEqual(self.run_bl("--beat", now="2026-10-20 17:00")[1], "", "within 24 h: silent")
        self.assertIn("[board-lint]", self.run_bl("--beat", now="2026-10-20 18:30")[1])

    def test_new_finding_shows_alone(self):
        self.run_bl("--beat")
        todo = (
            self.files["TODO"]
            .read_text()
            .replace("## Night backlog", "- **Remote copies** — 1080p copies for remote streaming\n\n## Night backlog")
        )
        self.files["TODO"].write_text(todo)
        out = self.run_bl("--beat")[1]
        self.assertTrue(out.startswith("[board-lint] 1 stale"), out)
        self.assertIn("Remote copies", out)

    def test_ack_all_silences(self):
        self.assertIn("acked 2", self.run_bl("ack", "all")[1])
        self.assertEqual(self.run_bl("--beat", now="2026-10-21 18:00")[1], "")
        self.assertEqual(self.findings(), [], "acked findings leave the list")
        rc, out, _ = self.run_bl()
        self.assertIn("board-lint: clean", out)
        self.assertIn("2 acked finding(s) hidden", out)
        rc, out, _ = self.run_bl("--all")
        self.assertEqual(out.count("(acked)"), 2, out)
        self.assertEqual(self.run_bl("--strict")[0], 0, "acked findings do not fail --strict")

    def test_acked_keys_stay_hidden_across_runs_and_days(self):
        keys = [f["key"] for f in self.findings()]
        self.assertEqual(len(keys), 2)
        rc, out, _ = self.run_bl("ack", *keys)
        self.assertIn("acked 2", out)
        for now in ("2026-10-19 18:05", "2026-10-20 09:00", "2026-11-10 09:00"):
            rc, out, _ = self.run_bl("--json", now=now)
            self.assertFalse({f["key"] for f in json.loads(out)} & set(keys), now)
            beat = self.run_bl("--beat", now=now)[1]
            self.assertNotIn("Disk day", beat, now)
        self.assertEqual(len(self.findings()), 0)

    def test_ack_bare_hash_and_unknown_key(self):
        key = self.findings()[0]["key"]
        rc, out, err = self.run_bl("ack", key.split("-", 1)[1])
        self.assertEqual(rc, 0, err)
        self.assertIn(key, out, "a bare hash resolves to the full key")
        self.assertNotIn(key, [f["key"] for f in self.findings()])
        rc, out, err = self.run_bl("ack", "nonsense")
        self.assertEqual(rc, 1)
        self.assertIn("not a finding key: nonsense", err)
        self.assertIn("acked 0", out)

    def test_handoff_counts_only_open_findings(self):
        self.run_bl("ack", "all")
        out = self.run_bl("handoff", "--hook", stdin=json.dumps({"source": "compact", "cwd": str(self.ws)}))[1]
        self.assertNotIn("stale item", out)

    def test_ack_one_key(self):
        key = self.findings()[0]["key"]
        self.run_bl("ack", key)
        out = self.run_bl("--beat")[1]
        self.assertTrue(out.startswith("[board-lint] 1 stale"), out)

    def test_long_list_is_capped(self):
        extra = "".join(f"- **Thing {i} with a fairly long title here** — today\n" for i in range(12))
        self.files["TODO"].write_text(
            self.files["TODO"].read_text().replace("## Night backlog", extra + "\n## Night backlog")
        )
        out = self.run_bl("--beat")[1].strip()
        self.assertLessEqual(len(out), 420)
        self.assertRegex(out, r"\+\d+ more")

    def test_corrupt_state_fails_open(self):
        self.state.parent.mkdir(parents=True, exist_ok=True)
        self.state.write_text("{not json")
        rc, out, err = self.run_bl("--beat")
        self.assertEqual(rc, 0)
        self.assertIn("[board-lint]", out)

    def test_crash_fails_open(self):
        env = dict(self.env, BOARD_LINT_WAIT_DAYS="x")  # a bad setting falls back to the default
        rc, out, err = self.run_bl("--beat", env=env)
        self.assertEqual(rc, 0, err)
        self.assertIn("[board-lint]", out)
        rc, out, _ = self.run_bl("--beat", env=dict(self.env, BOARD_LINT_STATE="/proc/x/y/state.json"))
        self.assertEqual((rc, out), (0, ""), "unwritable state: no line, exit 0")


def reply(text, buttons=None, tool="mcp__plugin_telegram-plus_telegram__reply"):
    ti = {"chat_id": "1", "text": text}
    if buttons is not None:
        ti["buttons"] = buttons
    return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": ti, "cwd": "/x"}


class Pitch(Base):
    def ctx(self, payload, via_hook=False):
        stdin = json.dumps(payload) if isinstance(payload, dict) else payload
        if via_hook:
            p = subprocess.run(
                [str(hook("pitch-check.sh"))],
                input=stdin.encode(),
                capture_output=True,
                timeout=20,
                env=dict(self.env, BOARD_LINT_BIN=BIN),
            )
            rc, out = p.returncode, p.stdout.decode()
        else:
            rc, out, _ = self.run_bl("pitch", "--hook", stdin=stdin)
        self.assertEqual(rc, 0)
        if not out.strip():
            return None
        o = json.loads(out)
        h = o["hookSpecificOutput"]
        self.assertEqual(h["hookEventName"], "PreToolUse")
        self.assertNotIn("permissionDecision", h, "never allow/deny/ask: context only")
        self.assertNotIn("decision", o)
        return h["additionalContext"]

    def test_declined_item_in_buttons_gets_context(self):
        c = self.ctx(
            reply(
                "Remote streaming stutters. Make 1080p copies?",
                [[{"text": "Yes, 1080p copies", "data": "rc:yes"}], [{"text": "No", "data": "rc:no"}]],
            )
        )
        self.assertIn("19.10 declined: 1080p copies", c)

    def test_plain_reply_without_buttons_is_ignored(self):
        self.assertIsNone(self.ctx(reply("Make 1080p copies for remote streaming? Tell me.")))

    def test_poll_is_checked(self):
        p = {
            "tool_name": "mcp__plugin_telegram-plus_telegram__send_poll",
            "tool_input": {"question": "Which ones?", "options": ["Money card", "Something else"], "multiple": True},
        }
        self.assertIn("skipped: money card", self.ctx(p))

    def test_neutral_question_passes_silently(self):
        self.assertIsNone(self.ctx(reply("Dinner at 19:00?", [[{"text": "Yes", "data": "d:y"}]])))

    def test_marks_waiting_item_asked(self):
        self.ctx(
            reply(
                "Lamp remote: re-pair the dimmers or delete the automation?",
                [[{"text": "Re-pair", "data": "lr:a"}, {"text": "Delete", "data": "lr:b"}]],
            )
        )
        rows = [json.loads(x) for x in self.queue.read_text().splitlines()]
        now = datetime.strptime(NOW, "%Y-%m-%d %H:%M").timestamp()
        self.assertEqual(rows, [{"ts": now, "text": "Lamp remote"}])
        self.assertEqual(oct(self.queue.stat().st_mode & 0o777), "0o600")
        self.ctx(reply("Lamp remote: still re-pair them?", [[{"text": "Yes", "data": "lr:a"}]]))
        self.assertEqual(len(self.queue.read_text().splitlines()), 2)  # appended, never rewritten

    def test_malformed_input_fails_open(self):
        for bad in ("", "not json", "[]", json.dumps({"tool_name": "x"})):
            rc, out, _ = self.run_bl("pitch", "--hook", stdin=bad)
            self.assertEqual((rc, out), (0, ""), bad)

    def test_wrapper(self):
        c = self.ctx(reply("1080p copies for remote streaming?", [[{"text": "Yes", "data": "x"}]]), via_hook=True)
        self.assertIn("declined", c)

    def test_wrapper_fails_open_on_missing_or_hanging_script(self):
        for b, extra in (("/nonexistent/board-lint", {}), ("/bin/sleep", {"PITCH_CHECK_TIMEOUT": "1"})):
            t = time.time()
            p = subprocess.run(
                [str(hook("pitch-check.sh"))],
                input=json.dumps(reply("x", [[]])).encode(),
                capture_output=True,
                timeout=20,
                env=dict(self.env, BOARD_LINT_BIN=b, PITCH_CHECK_LOG=str(self.d / "pc.log"), **extra),
            )
            self.assertEqual((p.returncode, p.stdout), (0, b""))
            self.assertLess(time.time() - t, 5)


class Handoff(Base):
    def test_handoff_for_the_workspace(self):
        todo = CLEAN_TODO
        self.write(todo=todo)
        rc, out, _ = self.run_bl("handoff", "--hook", stdin=json.dumps({"source": "compact", "cwd": str(self.ws)}))
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("[handoff]"))
        self.assertIn("Declined / removed (don't re-pitch): 19.10 1080p copies", out)
        self.assertIn("Done or decided lately: 19.10 done Disk 12 added", out)
        self.assertIn("In progress: Subtitle cleanup", out)
        self.assertIn("Waiting on you: Shop order (1 d, asked never); Lamp remote (9 d, asked 18.10)", out)
        self.assertNotIn("13.10 off", out.split("Done or decided")[1])
        self.assertIn("19.10 doorbell → HA (also the app route);", out)
        self.assertNotIn("[[", out)
        self.assertLess(len(out), 3001)

    def test_other_cwd_prints_nothing(self):
        self.assertEqual(self.run_bl("handoff", "--hook", stdin=json.dumps({"cwd": "/home/x/jobs/y"}))[1], "")

    def project(self):  # Claude Code's transcript folder for the temp workspace, under the test HOME
        return self.d / "home" / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(self.ws))

    def test_workspace_transcript_counts_after_a_cd(self):
        # the session's cwd follows every `cd`: a compaction in ~/inbox is still the workspace session
        hook = {"source": "compact", "cwd": str(self.d / "inbox"), "transcript_path": str(self.project() / "s.jsonl")}
        self.assertTrue(self.run_bl("handoff", "--hook", stdin=json.dumps(hook))[1].startswith("[handoff]"))
        hook["transcript_path"] = str(self.d / "proj" / "s.jsonl")
        self.assertEqual(self.run_bl("handoff", "--hook", stdin=json.dumps(hook))[1], "")
        env = dict(self.env, BOARD_LINT_PROJECT_DIR=str(self.d / "proj"))
        self.assertTrue(self.run_bl("handoff", "--hook", stdin=json.dumps(hook), env=env)[1].startswith("[handoff]"))

    def test_other_transcripts_print_nothing(self):
        for tp in (
            str(self.d / "home/.claude/projects/-home-x-jobs-y/s.jsonl"),
            str(self.project() / "s" / "subagents" / "agent-1.jsonl"),
            "",
            None,
            5,
        ):
            hook = {"source": "compact", "cwd": "/home/x/jobs/y", "transcript_path": tp}
            self.assertEqual(self.run_bl("handoff", "--hook", stdin=json.dumps(hook))[:2], (0, ""), tp)
        self.assertEqual(self.run_bl("handoff", "--hook", stdin="[]")[:2], (0, ""))

    def test_wrapper_and_bad_input(self):
        if hook("handoff.sh") is None:
            self.skipTest("no handoff.sh here: the tree does not carry it and there is no live one")
        p = subprocess.run(
            [str(hook("handoff.sh"))],
            input=json.dumps({"cwd": str(self.ws)}).encode(),
            capture_output=True,
            timeout=20,
            env=dict(self.env, BOARD_LINT_BIN=BIN),
        )
        self.assertEqual(p.returncode, 0)
        self.assertTrue(p.stdout.decode().startswith("[handoff]"))
        self.assertEqual(self.run_bl("handoff", "--hook", stdin="nope")[:2], (0, ""))


class Speed(Base):
    def test_fast_on_a_big_log(self):
        big = CLEAN_PROACTIVE + "".join(
            f"- 2026-10-1{i % 10} 10:{i % 60:02d} item {i} deployed fine\n" for i in range(3000)
        )
        self.write(proactive=big)
        t = time.time()
        self.run_bl("--beat")
        self.assertLess(time.time() - t, 2.0)


# ------------------------------------------------------------------------------------------------ reality checks
# The two lines that went stale on 29.10 (renamed: no personal values): a branch still READY after its land, and two
# key files still "ready to delete" after they were deleted.
STALE_LAND = (
    "  - critical jobs (his msg 1234, no shadow): branch critical-live {old} READY (registry runs_on hub "
    "for alpha-watch, beta-ctx [24.10 hold over]; sentinel runs the critical ones while /health fails); its "
    "CI 28.10 17:5x all green except legacy canary, RED ON MAIN → helper fixes main, then run "
    "`~/jobs/critical-live/golive.sh` (land, then per job schedule live + `assistant cron rm`); hub schedules sit "
    "in shadow until then; back `~/jobs/critical-live/rollback.sh JOB…`\n"
)
STALE_DELETE = (
    '  - key deletes (§5 f): alpha-key + beta-token ready to delete (his ✅ "Delete both" msg 1235, 28.10 '
    "22:07). Readers no longer need the files in hub mode (tests green). Delete blocked by risk-check HIGH, "
    "files put back: main session runs `shred -u ~/.keys/alpha-key ~/.keys/beta-token` with risk-ok, then "
    "drops the two lines from ~/.config/hubx/secrets.list; the media file stays\n"
)
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@x",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@x",
}
SCHED = (
    "watch:alpha-watch: ACTIVE live '* * * * *' last 2026-10-19T17:59:00+02:00\n"
    "job:gamma-sync: ACTIVE live '*/5 * * * *' last 2026-10-19T17:55:00+02:00\n"
    "job:delta-feed: ACTIVE shadow '*/5 * * * *' last 2026-10-19T17:55:00+02:00\n"
    "job:watch: ACTIVE live '*/15 * * * *' last 2026-10-19T17:45:00+02:00\n"
)
REGISTRY = "".join(
    f'[jobs.{j}]\nline = "{j}"\n\n' for j in ("alpha-watch", "beta-ctx", "gamma-sync", "delta-feed", "watch")
)


if __name__ == "__main__":
    before = {p: sha(p) for p in REAL}
    prog = unittest.main(exit=False, verbosity=1)
    after = {p: sha(p) for p in REAL}
    ok = prog.result.wasSuccessful()
    if before != after:
        print("FAIL - a real state file changed during the tests")
        ok = False
    raise SystemExit(0 if ok else 1)
