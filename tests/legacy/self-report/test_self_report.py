"""self-report tests: stub traces (fixed JSON), fixture logs, evals file, cron files; no real data."""

import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

BIN = os.environ.get("SELF_REPORT_BIN") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "scripts", "self-report"
)
REPORT = {
    "total_cost": 120.5,
    "agent_cost": 100.0,
    "main_cost": 20.5,
    "by_trigger": {"owner": {"agents": 3}},
    "top_agents": [{"description": "Build widget", "cost": 12.3, "effort": "xhigh", "status": "done"}],
    "owner_first_p50": 9000,
    "error_rate": 0.02,
    "hook_blocks": 2,
    "agents_bad": 1,
    "error_counts": {"Bash:exit_code": 5},
    "daily": {"2026-10-20": {"turns": 4, "agents": 3, "main_cost": 20.5, "agent_cost": 100.0}},
}


class SelfReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="selfreport-test-")
        ws = os.path.join(self.tmp, "ws")
        os.makedirs(ws)
        with open(os.path.join(ws, "PROACTIVE.md"), "w") as f:
            f.write(
                "# log\n- 20.10 widget: built it\n- 19.10 cleanup: removed X after his correction\n- 01.01 old: x\n"
            )
        with open(os.path.join(ws, "CHANGELOG.md"), "w") as f:
            f.write("- 20.10 hooks: tuned\n")
        stub = os.path.join(self.tmp, "traces")
        with open(stub, "w") as f:
            f.write(f"import json\nprint(json.dumps({REPORT!r}))\n")
        ev = os.path.join(self.tmp, "evals.json")
        with open(ev, "w") as f:
            json.dump({"score": 0.82, "previous": 0.78}, f)
        self.env = dict(
            os.environ,
            SELF_REPORT_NOW="2026-10-25 19:30",
            SELF_REPORT_WORKSPACE=ws,
            USAGE="off",
            SELF_REPORT_TRACES=stub,
            SELF_REPORT_USAGE_DB=os.path.join(self.tmp, "none.db"),
            SELF_REPORT_INBOX=os.path.join(self.tmp, "inbox"),
            SELF_REPORT_OUT=os.path.join(self.tmp, "out"),
            SELF_REPORT_EVALS=ev,
            SELF_REPORT_AGENT_WATCH_LOG=os.path.join(self.tmp, "none.log"),
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_s(self, *a):
        p = subprocess.run(
            [sys.executable, "-I", BIN] + list(a), env=self.env, capture_output=True, text=True, timeout=60
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def test_short_text(self):
        out = self.run_s().strip().splitlines()
        self.assertLessEqual(len(out), 10)
        text = "\n".join(out)
        self.assertIn("Did: 2 logged jobs, 1 setup changes", text)
        self.assertIn("$120", text)
        self.assertIn("Build widget $12", text)
        self.assertIn("evals 0.82 (was 0.78)", text)
        self.assertIn("1 corrections logged", text)

    def test_cron_writes_note_and_html(self):
        self.assertEqual(self.run_s("--cron"), "")
        notes = glob.glob(os.path.join(self.tmp, "inbox", "self-report-2026-10-25.md"))
        self.assertEqual(len(notes), 1)
        body = open(notes[0]).read()
        self.assertIn("📊 My week", body)
        html = glob.glob(os.path.join(self.tmp, "out", "week-2026-W43.html"))
        self.assertEqual(len(html), 1)
        self.assertIn("Build widget", open(html[0]).read())


if __name__ == "__main__":
    unittest.main()
