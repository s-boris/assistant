#!/usr/bin/env python3
"""Offline tests for ~/bin/skillforge (fake transcripts, fake usage store, fake skills/bin dirs; hashed vectors).
SKILLFORGE_BIN=/path/to/copy tests a copy. Exit 0 = all pass."""

import glob
import json
import os
import pwd
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest

BIN = os.environ.get("SKILLFORGE_BIN") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "scripts", "skillforge"
)
LIB = os.environ.get("SKILLFORGE_LIB") or os.path.expanduser("~/bin/lib")
# skill-creator's validator: under HOME, else the account's own home (CI runs this suite with a temp HOME)
_VGLOB = ".claude/skills/synced/*/skill-creator/scripts/quick_validate.py"
VALIDATOR = next(
    iter(
        sorted(glob.glob(os.path.join(os.path.expanduser("~"), _VGLOB)))
        or sorted(glob.glob(os.path.join(pwd.getpwuid(os.getuid()).pw_dir, _VGLOB)))
    ),
    "",
)
FAKE_TG_BIND = r"""#!/usr/bin/env python3
# fake tg-bind: menu --json, list --json, import FILE [--dry-run] over the fixture's tgbind/ files; logs each call
import json, os, sys
D = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "tgbind")
open(os.path.join(D, "calls.log"), "a").write(" ".join(sys.argv[1:]) + "\n")
a = sys.argv[1:]
if a[:2] == ["menu", "--json"]:
    print(open(os.path.join(D, "menu.json")).read())
elif a[:2] == ["list", "--json"]:
    print(open(os.path.join(D, "rows.json")).read())
elif a[:1] == ["import"]:
    rows = json.load(open(a[1]))["rows"]
    bad = [r["tool"] for r in rows if r["tool"] not in ("disk-card", "tg-cmd.space")]
    if bad:
        sys.exit(print("tg-bind: nothing imported:\n  row 1: tool %r is not in the tools registry" % bad[0],
                       file=sys.stderr) or 2)
    if "--dry-run" not in a:
        have = json.load(open(os.path.join(D, "rows.json")))
        json.dump(have + [dict(r, active=1) for r in rows], open(os.path.join(D, "rows.json"), "w"))
    print("imported %d rows" % len(rows))
else:
    sys.exit(2)
"""
DAY0 = 1791590400.0  # a Monday 00:00 UTC; fixture days are DAY0 + n days


def iso(ts):
    import datetime

    return datetime.datetime.fromtimestamp(ts, datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class Fixture:
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="sf-test-")
        d = lambda *p: os.path.join(self.tmp, *p)  # noqa: E731
        for sub in (
            "tx",
            "bin",
            "skills/disk-help",
            "skills/home",
            "staging",
            "state",
            "reports",
            "inbox",
            "tg",
            "usage",
            "installbin",
            "conf",
        ):
            os.makedirs(d(sub), exist_ok=True)
        for s in ("disk-check", "remind"):
            with open(d("bin", s), "w") as f:
                f.write("#!/bin/sh\n")
            os.chmod(d("bin", s), 0o755)
            os.utime(d("bin", s), (DAY0, DAY0))
        with open(d("skills/home/SKILL.md"), "w") as f:
            f.write("---\nname: home\ndescription: Turn lights on and off in the flat.\n---\nUses lights.\n")
        with open(d("skills/disk-help/SKILL.md"), "w") as f:
            f.write("---\nname: disk-help\ndescription: Unrelated helper.\n---\nbody\n")
        os.makedirs(d("tgbind"), exist_ok=True)
        os.makedirs(d("fakebin"), exist_ok=True)
        with open(d("fakebin/tg-bind"), "w") as f:
            f.write(FAKE_TG_BIND)
        os.chmod(d("fakebin/tg-bind"), 0o755)
        with open(d("tgbind/menu.json"), "w") as f:
            json.dump(
                {
                    "bound": [{"command": "space", "description": "Free space"}],
                    "custom": [],
                    "custom_from": "none",
                    "shown": [{"command": "space", "description": "Free space"}],
                },
                f,
            )
        with open(d("tgbind/rows.json"), "w") as f:
            json.dump([{"id": 1, "kind": "command", "key": "space", "tool": "tg-cmd.space", "active": 1}], f)
        with open(d("tg/commands.json"), "w") as f:
            json.dump([{"command": "tonight", "description": "Movie pick"}], f)
        self.lines, self.n = [], 0
        con = sqlite3.connect(d("usage.db"))
        con.execute(
            "CREATE TABLE turns(turn TEXT PRIMARY KEY, ts REAL, day TEXT, ch TEXT, start TEXT, prefix TEXT, "
            "dev TEXT, mix TEXT, label TEXT, primary_f TEXT)"
        )
        con.execute(
            "CREATE TABLE events(id INTEGER PRIMARY KEY, ts REAL, turn TEXT, kind TEXT, attrs TEXT, feature TEXT)"
        )
        con.commit()
        self.con = con
        self.env = dict(
            os.environ,
            SKILLFORGE_TRANSCRIPTS=d("tx"),
            SKILLFORGE_DB=d("usage.db"),
            SKILLFORGE_STAGING=d("staging"),
            SKILLFORGE_STATE=d("state"),
            SKILLFORGE_CONFIG=d("conf/config.json"),
            SKILLFORGE_REPORTS=d("reports"),
            SKILLFORGE_INBOX=d("inbox"),
            SKILLFORGE_SKILLS=d("skills"),
            SKILLFORGE_BIN_DIR=d("installbin"),
            SKILLFORGE_TG_DIR=d("tg"),
            SKILLFORGE_EMBED="hash",
            SKILLFORGE_TG_BIND=d("fakebin/tg-bind"),
            SKILLFORGE_NOW=str(DAY0 + 6 * 86400),
            SKILLFORGE_LIB=LIB,
            SKILLFORGE_VALIDATOR=VALIDATOR,
            USAGE_BIN=d("bin"),
            USAGE_STATE=d("usage"),
            USAGE_TEST="1",
        )
        self.d = d

    def turn(self, day, text, tools=(), label="use", error=False, fb=None):
        """One owner turn: a channel message, then tool calls (name, input) and a reply."""
        import hashlib

        self.n += 1
        ts = DAY0 + day * 86400 + 36000 + self.n * 60
        uuid = f"u{self.n}"
        tid = hashlib.sha1(uuid.encode()).hexdigest()[:8]
        self.lines.append(
            {
                "type": "user",
                "uuid": uuid,
                "timestamp": iso(ts),
                "origin": {"kind": "channel"},
                "message": {
                    "role": "user",
                    "content": f'<channel source="telegram" chat_id="1" '
                    f'message_id="{self.n}" user="x">{text}</channel>',
                },
            }
        )
        blocks = [
            {"type": "tool_use", "id": f"t{self.n}-{i}", "name": n, "input": inp}
            for i, (n, inp) in enumerate(list(tools) + [("mcp__plugin_telegram-plus_telegram__reply", {"text": "ok"})])
        ]
        self.lines.append({"type": "assistant", "timestamp": iso(ts + 5), "message": {"content": blocks}})
        self.lines.append(
            {
                "type": "user",
                "timestamp": iso(ts + 6),
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": f"t{self.n}-0", "is_error": error, "content": "x"}
                    ]
                },
            }
        )
        self.con.execute(
            "INSERT INTO turns(turn, ts, day, ch, start, label) VALUES (?,?,?,?,?,?)",
            (tid, ts, "d", "telegram", "text", label),
        )
        if fb:
            self.con.execute(
                "INSERT INTO events(ts, turn, kind, attrs) VALUES (?,?,?,?)", (ts, tid, "msg", json.dumps({"fb": fb}))
            )
        self.con.commit()
        return tid

    def heartbeat(self, day, tools):
        self.n += 1
        ts = DAY0 + day * 86400 + 72000 + self.n * 60
        self.lines.append(
            {"type": "user", "uuid": f"h{self.n}", "timestamp": iso(ts), "message": {"content": "[heartbeat] idle"}}
        )
        self.lines.append(
            {
                "type": "assistant",
                "timestamp": iso(ts + 5),
                "message": {
                    "content": [
                        {"type": "tool_use", "id": f"h{self.n}-{i}", "name": n, "input": inp}
                        for i, (n, inp) in enumerate(tools)
                    ]
                },
            }
        )

    def write(self):
        with open(self.d("tx", "s.jsonl"), "w") as f:
            for e in self.lines:
                f.write(json.dumps(e) + "\n")

    def run(self, *args, env=None):
        return subprocess.run(
            [sys.executable, BIN, *args], capture_output=True, text=True, timeout=120, env=dict(self.env, **(env or {}))
        )


SEQ = [("Bash", {"command": "disk-check run --all"}), ("Bash", {"command": "remind add 'tomorrow 9:00' check"})]
ASKS = ["how full is the disk array right now", "is the disk array almost full", "how full is my disk array today"]


def build():
    fx = Fixture()
    for day, text in enumerate(ASKS):
        fx.turn(day, text, SEQ)
    fx.turn(4, "how full is the disk array, building the forecast", SEQ, label="build")  # build turn: no intent
    for day in (1, 3, 5):
        fx.turn(day, "please switch the kitchen lights on", [("Skill", {"skill": "home"})])
    fx.turn(2, "deploy the thing", [("Bash", {"command": "remind list"}), ("Bash", {"command": "disk-check x"})])
    fx.turn(3, "deploy the thing again", [("Bash", {"command": "remind list"}), ("Bash", {"command": "disk-check x"})])
    fx.heartbeat(5, [("Bash", {"command": "remind list"})])
    fx.write()
    return fx


class T(unittest.TestCase):
    def test_mine_sequences_and_intents(self):
        fx = build()
        r = fx.run("mine", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        seqs = [c for c in data["candidates"] if c["type"] == "sequence"]
        self.assertTrue(any(c["steps"] == ["disk-check run", "remind add"] and c["turns"] == 4 for c in seqs), seqs)
        self.assertFalse(any(c["steps"] == ["remind list", "disk-check x"] for c in seqs))  # 2 turns only
        ints = [c for c in data["candidates"] if c["type"] == "intent"]
        disk = [c for c in ints if "disk" in c["keywords"]]
        self.assertEqual(len(disk), 1, ints)
        self.assertEqual(disk[0]["turns"], 3)  # the build turn is not an ask
        self.assertEqual(disk[0]["days"], 3)
        lights = [c for c in ints if "lights" in c["keywords"]][0]
        self.assertEqual(lights["covered_by"], "home")
        self.assertEqual(lights["kind"], "covered")

    def test_no_text_in_state(self):
        fx = build()
        fx.run("mine")
        raw = open(fx.d("state", "candidates.json")).read()
        for t in ASKS + ["please switch the kitchen lights on"]:
            self.assertNotIn(t, raw)

    def test_errors_and_corrections(self):
        fx = Fixture()
        for day in range(3):
            fx.turn(day, "rename the backup folder on the server", SEQ, error=(day == 0))
            fx.turn(day, "no, that is wrong, the other folder", fb="correction")
        fx.write()
        data = json.loads(fx.run("mine", "--json").stdout)
        c = [c for c in data["candidates"] if c["type"] == "intent" and "folder" in c["keywords"]][0]
        self.assertEqual(c["turns_with_errors"], 1)
        self.assertEqual(c["corrections"], 3)

    def test_draft_reject_and_no_repropose(self):
        fx = build()
        fx.run("mine")
        r = fx.run("draft", "--top", "5")
        self.assertEqual(r.returncode, 0, r.stderr)
        metas = [json.load(open(p)) for p in glob.glob(fx.d("staging", "*", "meta.json"))]
        self.assertTrue(metas)
        self.assertFalse(any(m["kind"] == "covered" for m in metas))
        seq = [m for m in metas if m["type"] == "sequence"][0]
        sd = fx.d("staging", seq["name"])
        self.assertTrue(os.stat(os.path.join(sd, "tests", "run.sh")).st_mode & stat.S_IXUSR)
        sk = open(os.path.join(sd, "SKILL.md")).read()
        self.assertTrue(sk.startswith(f"---\nname: {seq['name']}\n"))
        ev = json.load(open(os.path.join(sd, "evidence.json")))
        self.assertNotIn("centroid", ev)
        self.assertEqual(json.load(open(os.path.join(sd, "evals", "evals.json")))["skill_name"], seq["name"])
        chk = fx.run("check", seq["name"])
        self.assertEqual(chk.returncode, 1)
        self.assertIn("skeleton", chk.stdout)
        self.assertEqual(fx.run("reject", seq["name"], "--why", "not useful").returncode, 0)
        dec = json.load(open(fx.d("state", "decisions.json")))
        self.assertEqual(dec[seq["name"]]["status"], "rejected")
        r = fx.run("draft", "--top", "5")
        self.assertIn("nothing new", r.stdout)

    def _fleshed(self, fx, name="disk-card", risk="readonly"):
        sd = fx.d("staging", name)
        os.makedirs(os.path.join(sd, "scripts"))
        with open(os.path.join(sd, "SKILL.md"), "w") as f:
            f.write(f"---\nname: {name}\ndescription: Answer how full the disk array is.\n---\n\n# {name}\n")
        with open(os.path.join(sd, "scripts", name), "w") as f:
            f.write("#!/bin/sh\necho ok\n")
        json.dump(
            {
                "name": name,
                "type": "intent",
                "kind": "instant-command",
                "status": "skeleton",
                "risk": risk,
                "cand": "i:x",
                "score": 1,
                "turns": 3,
                "days": 3,
            },
            open(os.path.join(sd, "meta.json"), "w"),
        )
        return name

    def test_propose_accept_install(self):
        fx = build()
        name = self._fleshed(fx)
        r = fx.run("propose", name)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = fx.run("install", name)
        self.assertEqual(r.returncode, 2)  # proposed is not accepted
        self.assertFalse(os.path.exists(fx.d("skills", name)))
        fx.run("accept", name)
        r = fx.run("install", name, "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(fx.d("skills", name)))
        r = fx.run("install", name)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(fx.d("skills", name, "SKILL.md")))
        self.assertTrue(os.access(fx.d("installbin", name), os.X_OK))
        self.assertEqual(json.load(open(fx.d("staging", name, "meta.json")))["status"], "installed")
        meta = json.load(open(fx.d("staging", name, "meta.json")))
        meta["status"] = "accepted"
        json.dump(meta, open(fx.d("staging", name, "meta.json"), "w"))
        r = fx.run("install", name)
        self.assertEqual(r.returncode, 2)
        self.assertIn("refusing to overwrite", r.stderr)

    def test_weekly_note_and_auto_install_flag(self):
        fx = build()
        name = self._fleshed(fx)
        fx.run("propose", name)
        r = fx.run("weekly", "--cron")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "")
        notes = glob.glob(fx.d("inbox", "skillforge-*.md"))
        self.assertEqual(len(notes), 1)
        body = open(notes[0]).read()
        self.assertIn(f"proposed `{name}`", body)
        for t in ASKS:
            self.assertNotIn(t, body)
        self.assertTrue(glob.glob(fx.d("reports", "skillforge-*.md")))
        self.assertFalse(os.path.exists(fx.d("skills", name)))  # default: proposals only
        with open(fx.d("conf/config.json"), "w") as f:
            json.dump({"auto_install_readonly": True}, f)
        fx.run("weekly", "--cron")
        self.assertTrue(os.path.exists(fx.d("skills", name, "SKILL.md")))

    def test_auto_install_skips_writes(self):
        fx = build()
        name = self._fleshed(fx, "lamp-writer", risk="writes")
        fx.run("propose", name)
        with open(fx.d("conf/config.json"), "w") as f:
            json.dump({"auto_install_readonly": True}, f)
        fx.run("weekly", "--cron")
        self.assertFalse(os.path.exists(fx.d("skills", name)))

    def test_usage_logged_and_stats(self):
        fx = build()
        fx.run("mine")
        name = self._fleshed(fx)
        fx.run("propose", name)
        fx.run("reject", name)
        spool = "".join(open(p).read() for p in glob.glob(fx.d("usage", "spool", "*.jsonl")))
        self.assertIn('"f":"bin.skillforge"', spool)
        self.assertIn('"verb":"reject"', spool)
        s = json.loads(fx.run("stats", "--json").stdout)
        self.assertEqual(s["decided"], 1)
        self.assertEqual(s["acceptance_rate"], 0.0)

    def test_install_bindings_and_drop(self):
        fx = build()
        name = self._fleshed(fx)
        json.dump(
            {"rows": [{"kind": "command", "key": "/DiskCard", "tool": "disk-card", "why": "Disk card"}]},
            open(fx.d("staging", name, "bindings.json"), "w"),
        )
        fx.run("propose", name)
        fx.run("accept", name)
        self.assertIn("tg-bind +command diskcard", fx.run("install", name, "--dry-run").stdout)
        r = fx.run("install", name)
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = [(x["kind"], x["key"]) for x in json.load(open(fx.d("tgbind", "rows.json")))]
        self.assertEqual(rows, [("command", "space"), ("command", "/DiskCard")])
        bj = fx.d("staging", name, "bindings.json")
        self.assertEqual(
            [ln for ln in open(fx.d("tgbind", "calls.log")).read().splitlines() if ln.startswith(("list", "import"))],
            ["list --json", f"import {bj} --dry-run", "list --json", f"import {bj} --dry-run", f"import {bj}"],
        )
        fx.run("mine")
        fx.run("draft", "--top", "1")
        m = [json.load(open(p)) for p in glob.glob(fx.d("staging", "*", "meta.json")) if "disk-card" not in p][0]
        self.assertNotIn("--", m["name"])
        self.assertEqual(fx.run("drop", m["name"], "--why", "covered").returncode, 0)
        self.assertEqual(json.load(open(fx.d("state", "decisions.json")))[m["name"]]["status"], "dropped")
        self.assertEqual(json.loads(fx.run("stats", "--json").stdout)["acceptance_rate"], 1.0)  # drops do not count

    def test_install_refuses_instant_json_taken_rows_and_unknown_tools(self):
        for snippet, err in [
            ("instant.json", "convert it to bindings.json"),
            (
                {"rows": [{"kind": "command", "key": "Space", "tool": "disk-card", "why": "x"}]},
                "refusing to overwrite tg-bind row(s) command space",
            ),
            ({"rows": [{"kind": "command", "key": "dc", "tool": "nope", "why": "x"}]}, "not in the tools registry"),
            ({"rows": []}, "want"),
        ]:
            fx = build()
            name = self._fleshed(fx)
            if snippet == "instant.json":
                json.dump(
                    [{"command": "dc", "run": "~/bin/disk-card"}], open(fx.d("staging", name, "instant.json"), "w")
                )
            else:
                json.dump(snippet, open(fx.d("staging", name, "bindings.json"), "w"))
            fx.run("propose", name)
            fx.run("accept", name)
            r = fx.run("install", name)
            self.assertEqual(r.returncode, 2, (snippet, r.stdout, r.stderr))  # refused
            self.assertIn(err, r.stderr, snippet)
            self.assertFalse(os.path.exists(fx.d("skills", name)), snippet)  # nothing installed
            self.assertEqual(len(json.load(open(fx.d("tgbind", "rows.json")))), 1)

    def test_coverage_sources_bound_rows_and_menu(self):
        fx = build()
        code = (
            "import json, sys; from importlib.machinery import SourceFileLoader as L; "
            "m = L('sf', sys.argv[1]).load_module(); "
            "print(json.dumps([c[:2] for c in m.coverage_sources() if c[0] != 'skill']))"
        )
        cov = lambda: json.loads(  # noqa: E731
            subprocess.run(
                [sys.executable, "-c", code, BIN], capture_output=True, text=True, timeout=60, env=fx.env
            ).stdout
        )
        self.assertEqual(cov(), [["instant", "/space"], ["command", "/tonight"]])  # custom empty: commands.json
        with open(fx.d("tgbind/menu.json"), "w") as f:
            json.dump(
                {
                    "bound": [{"command": "space", "description": "Free space"}],
                    "custom": [{"command": "lights", "description": "Remote"}],
                    "custom_from": "x",
                },
                f,
            )
        self.assertEqual(cov(), [["instant", "/space"], ["command", "/lights"]])  # commands.json not read
        os.remove(fx.d("fakebin/tg-bind"))
        self.assertEqual(cov(), [["command", "/tonight"]])  # no tg-bind: commands.json

    def test_usage_errors(self):
        fx = build()
        self.assertEqual(fx.run("accept").returncode, 2)
        self.assertEqual(fx.run("install", "nope").returncode, 2)


if __name__ == "__main__":
    unittest.main(verbosity=1)
