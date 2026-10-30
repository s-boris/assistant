"""Offline tests for usage-watch: fixture transcripts (weekly, monthly, cleared, quiet hours, dedupe), a fake sender.

python3 test_usage_watch.py        USAGE_WATCH_BIN points at a copy (default: the repo's scripts/ copy)
Nothing here reads a real transcript or sends a real message: every source and the sender are temp paths.
"""

import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")
REPO = os.path.normpath(os.path.join(HERE, "..", "..", ".."))
BIN = os.environ.get("USAGE_WATCH_BIN") or os.path.join(REPO, "scripts", "usage-watch")
OFF = "+01:00"  # the fixture profile's zone in March


def ts(iso):
    return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


class UsageWatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="usage-watch-t.")
        self.proj = os.path.join(self.tmp, "proj")
        self.runs = os.path.join(self.tmp, "runs")
        self.state = os.path.join(self.tmp, "state")
        self.sent_f = os.path.join(self.tmp, "sent.txt")
        os.makedirs(self.proj)
        os.makedirs(self.runs)
        self.tg = os.path.join(self.tmp, "fake-tg")
        with open(self.tg, "w") as f:
            f.write(
                '#!/bin/sh\n[ -n "$FAKE_TG_FAIL" ] && { echo "fake: down" >&2; exit 1; }\n'
                'printf "%s\\n" "$*" >> "$FAKE_TG_OUT"\n'
            )
        os.chmod(self.tg, 0o755)
        self.seen = 0

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def put(self, name, *fixtures, sub=None):
        """Append fixture transcripts to proj/NAME (or proj/SUB/subagents/NAME); mtime = its newest record."""
        d = os.path.join(self.proj, sub, "subagents") if sub else self.proj
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, name)
        newest = 0.0
        with open(p, "a") as out:
            for fx in fixtures:
                with open(os.path.join(FIX, fx)) as f:
                    for line in f:
                        out.write(line)
                        newest = max(newest, ts(json.loads(line)["timestamp"]))
        if newest:
            os.utime(p, (newest, newest))
        return p

    def run_at(self, local, *extra, fail=False, sender=True):
        env = dict(
            os.environ,
            USAGE_WATCH_DIR=self.state,
            USAGE_WATCH_PROJECTS=self.proj,
            USAGE_WATCH_EVALS=self.runs,
            ASSISTANT_PROFILE=os.path.join(FIX, "profile.json"),
            ASSISTANT_NOW=local + OFF,
            FAKE_TG_OUT=self.sent_f,
        )
        if sender:
            env["USAGE_WATCH_TG"] = self.tg
        else:
            env.pop("USAGE_WATCH_TG", None)
        if fail:
            env["FAKE_TG_FAIL"] = "1"
        p = subprocess.run([BIN, *(extra or ("run",))], env=env, capture_output=True, text=True, timeout=60)
        return p

    def new_sent(self):
        try:
            with open(self.sent_f) as f:
                lines = f.read().splitlines()
        except OSError:
            lines = []
        out, self.seen = lines[self.seen :], len(lines)
        return out

    def state_json(self):
        with open(os.path.join(self.state, "state.json")) as f:
            return json.load(f)

    # --- weekly limit, quiet hours, dedupe, cleared

    def test_weekly_held_in_quiet_hours_then_sent_at_8(self):
        self.put("s1.jsonl", "weekly.jsonl")
        p = self.run_at("2026-03-10T02:25:00")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.new_sent(), [])
        st = self.state_json()
        self.assertEqual(st["seen"]["state"], "limited")
        self.assertTrue(st["pending"])
        self.assertEqual(self.run_at("2026-03-10T07:59:00").returncode, 0)
        self.assertEqual(self.new_sent(), [])
        self.assertEqual(self.run_at("2026-03-10T08:03:00").returncode, 0)
        sent = self.new_sent()
        self.assertEqual(len(sent), 1, sent)
        self.assertIn("weekly limit hit at 02:20", sent[0])
        self.assertIn("resets Thu 12.03 14:00", sent[0])
        self.assertIn("paused until then", sent[0])
        self.assertIsNone(self.state_json()["pending"])

    def test_dedupe_no_repeat_while_limited(self):
        self.put("s1.jsonl", "weekly.jsonl")
        self.run_at("2026-03-10T02:25:00")
        self.put("s2.jsonl", "weekly.jsonl")  # a second transcript with the same limit
        for t in ("2026-03-10T08:03:00", "2026-03-10T08:13:00", "2026-03-10T08:23:00", "2026-03-10T09:33:00"):
            self.assertEqual(self.run_at(t).returncode, 0)
        self.assertEqual(len(self.new_sent()), 1)

    def test_cleared_sends_one_back_line(self):
        p = self.put("s1.jsonl", "weekly.jsonl")
        self.run_at("2026-03-10T08:03:00")
        self.assertEqual(len(self.new_sent()), 1)
        self.put("s1.jsonl", "cleared.jsonl")
        self.assertTrue(os.path.getmtime(p) >= ts("2026-03-10T13:05:00Z"))
        self.run_at("2026-03-10T14:13:00")
        sent = self.new_sent()
        self.assertEqual(len(sent), 1, sent)
        self.assertIn("works again", sent[0])
        self.assertIn("weekly limit cleared at 14:05", sent[0])
        self.assertIn("paused since 02:20", sent[0])
        self.run_at("2026-03-10T14:23:00")
        self.run_at("2026-03-10T15:23:00")
        self.assertEqual(self.new_sent(), [])
        st = self.state_json()
        self.assertEqual(st["told"]["state"], "ok")
        self.assertEqual(st["episodes"][-1]["kind"], "weekly")

    def test_back_line_waits_for_8_too(self):
        p = self.put("s1.jsonl", "weekly.jsonl")
        self.run_at("2026-03-10T08:03:00")
        self.assertEqual(len(self.new_sent()), 1)
        with open(os.path.join(FIX, "night-ok.jsonl")) as f:
            r = json.loads(f.readline())
        r["timestamp"] = "2026-03-11T00:30:00.000Z"  # 01:30 his time: a reply in quiet hours
        with open(p, "a") as f:
            f.write(json.dumps(r) + "\n")
        os.utime(p, (ts(r["timestamp"]), ts(r["timestamp"])))
        self.run_at("2026-03-11T01:33:00")
        self.assertEqual(self.new_sent(), [])
        self.assertEqual(self.state_json()["pending"]["state"], "ok")
        self.run_at("2026-03-11T08:00:00")  # the file is 6.5 h old by now: the kept state decides
        sent = self.new_sent()
        self.assertEqual(len(sent), 1, sent)
        self.assertIn("weekly limit cleared at 01:30 (paused since Tue 10.03 02:20)", sent[0])

    # --- monthly spend cap

    def test_monthly_update_while_paused(self):
        self.put("s1.jsonl", "weekly.jsonl")
        self.run_at("2026-03-10T08:03:00")
        self.assertEqual(len(self.new_sent()), 1)
        self.put("s1.jsonl", "monthly.jsonl")
        self.run_at("2026-03-10T10:13:00")
        sent = self.new_sent()
        self.assertEqual(len(sent), 1, sent)
        self.assertIn("update: now the monthly spend limit too", sent[0])
        self.assertIn("The weekly limit resets Thu 12.03 14:00", sent[0])
        self.run_at("2026-03-10T10:23:00")
        self.assertEqual(self.new_sent(), [])

    def test_monthly_no_reset_waits_for_8(self):
        self.put("s1.jsonl", "monthly-noreset.jsonl")
        self.run_at("2026-03-10T03:03:00")
        self.run_at("2026-03-10T07:53:00")
        self.assertEqual(self.new_sent(), [])
        self.run_at("2026-03-10T08:00:00")
        sent = self.new_sent()
        self.assertEqual(len(sent), 1, sent)
        self.assertIn("monthly spend limit hit at 03:00", sent[0])
        self.assertIn("No reset date given", sent[0])
        self.assertIn("raised at https://claude.ai/settings/usage ", sent[0])
        self.assertNotIn("from=", sent[0])

    def test_cleared_before_8_is_dropped(self):
        self.put("s1.jsonl", "monthly-noreset.jsonl")
        self.run_at("2026-03-10T03:03:00")
        self.put("s1.jsonl", "night-ok.jsonl")
        self.run_at("2026-03-10T05:03:00")
        self.run_at("2026-03-10T08:03:00")
        self.assertEqual(self.new_sent(), [])
        st = self.state_json()
        self.assertEqual(st["seen"]["state"], "ok")
        self.assertIsNone(st["pending"])
        with open(os.path.join(self.state, "log")) as f:
            self.assertIn("dropped the held message", f.read())

    # --- what counts

    def test_quoted_text_and_other_errors_never_count(self):
        self.put("s1.jsonl", "quoted.jsonl")
        self.run_at("2026-03-10T08:03:00")
        self.assertEqual(self.new_sent(), [])
        self.assertEqual(self.state_json()["seen"]["state"], "ok")

    def test_subagent_newer_than_session_wins(self):
        self.put("s1.jsonl", "night-ok.jsonl")
        self.put("agent-a1.jsonl", "monthly.jsonl", sub="s1")
        self.run_at("2026-03-10T10:13:00")
        sent = self.new_sent()
        self.assertEqual(len(sent), 1, sent)
        self.assertIn("monthly spend limit", sent[0])
        self.assertEqual(self.state_json()["seen"]["source"], "subagent")

    def evals_run(self, rid, started, detail, cost):
        d = os.path.join(self.runs, rid)
        os.makedirs(d)
        with open(os.path.join(d, "result.json"), "w") as f:
            json.dump(
                {
                    "run_id": rid,
                    "started": started,
                    "totals": {"duration_s": 10.0, "cost_usd": cost},
                    "cases": [{"graders": [{"detail": "ok"}, {"detail": detail}]}],
                },
                f,
            )

    def test_same_limit_from_evals_is_no_news(self):
        self.put("s1.jsonl", "weekly.jsonl")
        self.run_at("2026-03-10T02:25:00")
        detail = "no verdicts: You've hit your weekly limit · resets Mar 12, 2pm (Europe/Vienna)"
        self.evals_run("20260310-025003-live", ts("2026-03-10T01:50:03Z"), detail, 0)  # newer than the session
        self.run_at("2026-03-10T08:03:00")
        sent = self.new_sent()
        self.assertEqual(len(sent), 1, sent)
        self.assertIn("resets Thu 12.03 14:00", sent[0])  # the session's exact reset stays
        self.assertEqual(self.state_json()["seen"]["source"], "evals")
        self.run_at("2026-03-10T08:13:00")
        self.assertEqual(self.new_sent(), [])

    def test_evals_limit_then_ok(self):
        run = self.evals_run
        run(
            "20260310-025003-live",
            ts("2026-03-10T01:50:03Z"),
            "no verdicts: You've hit your org's monthly spend limit · ask your admin to raise it at "
            "https://claude.ai/admin-settings/usage · your",
            0,
        )
        self.run_at("2026-03-10T08:03:00")
        sent = self.new_sent()
        self.assertEqual(len(sent), 1, sent)
        self.assertIn("monthly spend limit hit at 02:50", sent[0])
        self.assertIn("https://claude.ai/admin-settings/usage", sent[0])
        run("20260311-025003-live", ts("2026-03-11T01:50:03Z"), "judge: fine", 1.5)
        self.run_at("2026-03-11T08:03:00")
        sent = self.new_sent()
        self.assertEqual(len(sent), 1, sent)
        self.assertIn("works again", sent[0])

    def test_evals_api_error_row(self):
        # evals no longer judges a run that ended in an API error: the limit text is in the row's infra detail
        d = os.path.join(self.runs, "20260310-025003-live")
        os.makedirs(d)
        with open(os.path.join(d, "result.json"), "w") as f:
            json.dump(
                {
                    "run_id": "20260310-025003-live",
                    "started": ts("2026-03-10T01:50:03Z"),
                    "totals": {"duration_s": 10.0, "cost_usd": 0, "errored": 1, "nodata": True},
                    "cases": [
                        {
                            "status": "error",
                            "score": None,
                            "graders": [{"detail": "0 hit(s)"}],
                            "infra": {
                                "kind": "usage_limit",
                                "status": 429,
                                "detail": "You've hit your weekly limit · resets Mar 12, 2pm (Europe/Vienna)",
                            },
                        }
                    ],
                },
                f,
            )
        self.run_at("2026-03-10T08:03:00")
        sent = self.new_sent()
        self.assertEqual(len(sent), 1, sent)
        self.assertIn("weekly limit hit at 02:50", sent[0])
        self.assertEqual(self.state_json()["seen"]["source"], "evals")

    # --- plumbing

    def test_send_failure_retries(self):
        self.put("s1.jsonl", "weekly.jsonl")
        p = self.run_at("2026-03-10T08:03:00", fail=True)
        self.assertEqual(p.returncode, 1)
        self.assertIn("send failed", p.stderr)
        self.assertEqual(self.state_json().get("told"), None)
        self.assertEqual(self.run_at("2026-03-10T08:13:00").returncode, 0)
        self.assertEqual(len(self.new_sent()), 1)
        self.run_at("2026-03-10T08:23:00")
        self.assertEqual(self.new_sent(), [])

    def test_small_chunks_find_the_last_record(self):
        self.put("s1.jsonl", "weekly.jsonl")
        os.environ["USAGE_WATCH_CHUNK"] = "37"
        try:
            self.run_at("2026-03-10T08:03:00")
        finally:
            os.environ.pop("USAGE_WATCH_CHUNK")
        sent = self.new_sent()
        self.assertEqual(len(sent), 1, sent)
        self.assertIn("resets Thu 12.03 14:00", sent[0])

    def test_old_transcripts_are_no_signal(self):
        self.put("s1.jsonl", "weekly.jsonl")
        self.run_at("2026-03-11T08:03:00")  # 30 h after the last record: outside the 6 h window
        self.assertEqual(self.new_sent(), [])
        self.assertEqual(self.state_json()["seen"]["state"], "ok")

    def test_dry_run_and_status_write_nothing(self):
        self.put("s1.jsonl", "weekly.jsonl")
        p = self.run_at("2026-03-10T02:25:00", "run", "--dry-run")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("(held for 08:00) Claude weekly limit hit", p.stdout)
        p = self.run_at("2026-03-10T08:03:00", "status", "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["now"]["state"], "limited")
        self.assertFalse(os.path.exists(os.path.join(self.state, "state.json")))
        self.assertEqual(self.new_sent(), [])

    def test_test_state_without_sender_sends_nothing(self):
        self.put("s1.jsonl", "weekly.jsonl")
        p = self.run_at("2026-03-10T08:03:00", sender=False)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.new_sent(), [])
        self.assertIsNone(self.state_json().get("told"))

    def test_help(self):
        p = subprocess.run([BIN, "--help"], capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0)
        self.assertIn("token-free", p.stdout)


if __name__ == "__main__":
    r = unittest.main(exit=False, verbosity=1).result
    print(f"usage-watch: {r.testsRun} tests, {len(r.failures) + len(r.errors)} FAIL")
    sys.exit(0 if r.wasSuccessful() else 1)
