"""The post-workout recap (`gym recap`, gymcoach.py) and tg-cmd's /gym WORD. Synthetic log only (made-up values,
gym_testlib.make_db); tg-cmd runs against a fake gym (TG_CMD_BIN)."""

import datetime as dt
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gym_testlib as base  # noqa: E402


class TestCoach:
    @classmethod
    def setup_class(cls):
        cls.m = base.load_module()

    def setup_method(self):
        self.e = base.Env(now=base.NOW)
        self.e.env.update(
            GYM_CALCACHE="off", GYM_RAVE_RADAR="off", USAGE="off", GYM_INBOX=os.path.join(self.e.dir, "inbox")
        )
        self.conn, self.xid = base.make_db(self.e.env["GYM_DB"], self.m)
        self.state = os.path.dirname(self.e.env["GYM_DB"])

    def teardown_method(self):
        self.conn.close()
        self.e.close()

    def plan(self):
        self.e.run("plan", "draft", "--minutes", "60", "--json")
        with open(os.path.join(self.state, "plan.json")) as fh:
            return json.load(fh)

    def add(self, date, items, sets=3):
        base.add_session(self.conn, dt.date.fromisoformat(date), items, self.xid, sets=sets)
        self.conn.commit()

    def j(self, *args, now=None, code=0):
        return json.loads(self.e.run(*args, "--json", now=now, code=code).stdout)

    # ------------------------------------------------------------------ recap
    def test_recap_card(self):
        p = self.plan()
        a = p["days"][0]
        items = [
            (x["name"], (x["weight_kg"] or 0.0) + (10 if x["role"] == "main" else 0), x["hi"])
            for x in a["exercises"][:3]
        ]
        self.add("2026-10-18", items)
        d = self.j("recap")
        assert d["date"] == "2026-10-18"
        assert d["day"] == a["key"]
        assert d["prs"]
        assert d["skipped"]
        assert "💪 Session 18.10. · " + a["key"] in d["text"]
        assert "🏆 PR:" in d["text"]
        assert self.e.run("recap", "--date", "2026-10-17", code=3).stdout == ""

    def test_recap_cron_two_polls_then_one_note(self):
        inbox = self.e.env["GYM_INBOX"]

        def notes():
            return sorted(os.listdir(inbox)) if os.path.isdir(inbox) else []

        self.e.run("recap", "--cron")  # first run: silent baseline
        assert notes() == []
        self.add("2026-10-18", [("Barbell Squat", 100.0, 5)], sets=2)
        self.e.run("recap", "--cron")  # seen once: wait
        assert notes() == []
        self.add_set_to_last()  # still logging: changed
        self.e.run("recap", "--cron")
        assert notes() == []
        self.e.run("recap", "--cron")  # same twice: settled
        assert len(notes()) == 1
        with open(os.path.join(inbox, notes()[0])) as fh:
            body = fh.read()
        assert "gym recap" in body
        assert "kg" not in body
        assert "100" not in body
        self.e.run("recap", "--cron")
        self.e.run("recap", "--cron")
        assert len(notes()) == 1  # once per session
        self.add("2026-10-01", [("Barbell Squat", 100.0, 5)])  # late backfill of an old day
        self.e.run("recap", "--cron")
        self.e.run("recap", "--cron")
        assert len(notes()) == 1

    def test_recap_cron_usage_runs_are_not_errors(self):
        """Every --cron poll is a healthy run: the usage store folds an exec outcome outside usage.OK_OUT into a
        failed run + an error event, so the poll state (init/pending/note/none/old) goes into attrs, not outcome."""
        loader = importlib.machinery.SourceFileLoader("usage_t", str(base.staged() / "lib" / "usage.py"))
        spec = importlib.util.spec_from_loader("usage_t", loader)
        usage = importlib.util.module_from_spec(spec)
        loader.exec_module(usage)
        ustate = os.path.join(self.e.dir, "usage")
        self.e.env.update(USAGE="", USAGE_STATE=ustate)
        self.e.run("recap", "--cron")  # init
        self.add("2026-10-18", [("Barbell Squat", 100.0, 5)], sets=2)
        self.e.run("recap", "--cron")  # pending
        self.e.run("recap", "--cron")  # note
        self.e.run("recap", "--cron")  # none
        self.add("2026-10-20", [("Barbell Squat", 100.0, 5)])
        self.e.run("recap", "--cron", now="2026-10-30")  # late backfill: old
        self.e.run("recap", "--cron", now="2026-10-30")  # none
        rows = []
        for name in sorted(os.listdir(os.path.join(ustate, "spool"))):
            with open(os.path.join(ustate, "spool", name)) as fh:
                rows += [json.loads(x) for x in fh if x.strip()]
        rows = [r for r in rows if r.get("f") == "gym.recap" and r.get("k") == "exec"]
        assert len(rows) == 6
        for r in rows:
            assert (r.get("o") or "ok") in usage.OK_OUT, r
        assert [(r.get("at") or {}).get("state") for r in rows] == ["init", "pending", "note", "none", "old", "none"]

    def add_set_to_last(self):
        lid = self.conn.execute("SELECT MAX(id) FROM exercise_logs").fetchone()[0]
        self.conn.execute(
            "INSERT INTO sets (log_id, idx, weight_kg, reps, e1rm_kg) VALUES (?,?,?,?,?)", (lid, 9, 100.0, 5, 116.67)
        )
        self.conn.commit()


def _png(path, w=10, h=10):
    """A black RGB PNG (stdlib only: the fake card needs a real image file, not Pillow)."""
    import struct
    import zlib

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + b"\x00\x00\x00" * w for _ in range(h))
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)))
        f.write(chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class TestTgCmdGym:
    """tg-cmd /gym WORD with a fake gym (prints its argv; --card writes a PNG)."""

    def setup_method(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.log = os.path.join(d, "log")
        png = os.path.join(d, "card.png")
        _png(png)
        with open(os.path.join(d, "gym"), "w") as fh:
            fh.write(
                f"#!/bin/sh\necho \"gym $*\" >> '{self.log}'\n"
                f'case "$*" in *--card*) echo \'{png}\';; *) echo "📈 fake $*"; echo line2;; esac\n'
            )
        os.chmod(os.path.join(d, "gym"), 0o755)
        self.env = dict(os.environ, TG_CMD_BIN=d, TG_CMD_TEST="1", USAGE="off")
        self.env.pop("TG_CMD_PICTURES", None)

    def teardown_method(self):
        self.tmp.cleanup()

    def run_tg(self, *args, pictures=False):
        env = dict(self.env, **({"TG_CMD_PICTURES": "1"} if pictures else {}))
        return subprocess.run(
            [sys.executable, str(base.staged() / "tg-cmd"), *args], env=env, capture_output=True, text=True, timeout=60
        )

    def test_words(self):
        assert "📈 fake plan next" in self.run_tg("gym").stdout
        assert "📈 fake recap" in self.run_tg("gym", "last").stdout
        assert "📈 fake recap" in self.run_tg("gym", "recap").stdout
        hint = self.run_tg("gym", "zumba")
        assert hint.returncode == 0
        assert "/gym recap" in hint.stdout
        if os.path.exists(self.log):  # the fake gym never ran for it
            with open(self.log) as fh:
                assert "zumba" not in fh.read()
        assert self.run_tg("gym", "a", "b").returncode == 2
        assert "📈 fake recap" in self.run_tg("gym", "last", pictures=True).stdout  # text, no photo JSON
        assert "/gym " in self.run_tg("--list").stdout  # its text argument lives in the tools registry
