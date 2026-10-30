#!/usr/local/bin/python3.13 -I
"""The board on the one outbox, for run.sh: tg-status with a temp HOME where the live tg-adapter
"runs" (a process named tg-adapter holding the live PID file, live.json with send on) and a fake consumer that ends
the live outbox rows like the adapter. No Bot API is ever called and no token file exists: in outbox mode tg-status
must not need one.

  test_outbox.py TG_STATUS     prints "ok   - ..." / "FAIL - ..." lines and "N passed, M failed"; exit 0 = all pass
"""

import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

TGS = sys.argv[1]
T = tempfile.mkdtemp(prefix="tg-status-outbox.")
HOME = os.path.join(T, "home")
OWNER = "4242"
passed = failed = 0


def ok(name, cond, got=""):
    global passed, failed
    if cond:
        passed += 1
        print("ok   - " + name)
    else:
        failed += 1
        print("FAIL - " + name)
        if got != "":
            print(f"       got: {got}")


class Consumer(threading.Thread):
    """Claims queued live rows (queued -> sending only while still queued) and ends them sent (ids from 700) or
    failed when fail_edits is set and the row is an edit."""

    def __init__(self, db):
        super().__init__(daemon=True)
        self.db, self.fail_edits, self.stop, self.next_id = db, False, threading.Event(), 700

    def run(self):
        while not self.stop.is_set():
            if os.path.exists(self.db):
                try:
                    con = sqlite3.connect(self.db, timeout=5, isolation_level=None)
                    for rid, op in con.execute(
                        "SELECT id, op FROM outbox WHERE status='queued' AND mode='live' ORDER BY id"
                    ).fetchall():
                        if not con.execute(
                            "UPDATE outbox SET status='sending', attempts=1 WHERE id=? AND status='queued'", (rid,)
                        ).rowcount:
                            continue
                        if op == "edit" and self.fail_edits:
                            con.execute(
                                "UPDATE outbox SET status='failed', last_error=? WHERE id=?",
                                (
                                    "Call to 'editMessageText' failed! (400: Bad Request: message to edit not found)",
                                    rid,
                                ),
                            )
                            continue
                        if op == "send":
                            self.next_id += 1
                        mid = self.next_id if op == "send" else None
                        body = json.loads(con.execute("SELECT body FROM outbox WHERE id=?", (rid,)).fetchone()[0])
                        mid = mid or body.get("message_id")
                        con.execute(
                            "UPDATE outbox SET status='sent', tg_message_id=?, result=? WHERE id=?",
                            (mid, json.dumps({"ids": [mid], "notes": []}), rid),
                        )
                    con.close()
                except sqlite3.OperationalError:
                    pass
            time.sleep(0.03)


def rows(db):
    if not os.path.exists(db):
        return []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute("SELECT * FROM outbox WHERE mode='live' ORDER BY id")]
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()


def main():
    os.makedirs(os.path.join(HOME, ".config/tg-adapter"))
    os.makedirs(os.path.join(HOME, ".local/state/tg-adapter/live"))
    with open(os.path.join(HOME, ".config/tg-adapter/live.json"), "w") as f:
        json.dump({"mode": "live", "bot": "livebot", "allow_from": [OWNER], "send": True}, f)
    fake = subprocess.Popen(
        ["tg-adapter", "-c", "import time; time.sleep(120)"],
        executable=sys.executable,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    with open(os.path.join(HOME, ".local/state/tg-adapter/live/adapter.pid"), "w") as f:
        f.write(f"{fake.pid}\ntg-adapter\n")
    with open(os.path.join(T, "TODO.md"), "w") as f:
        f.write("# TODO\n## Due\n- **Ship it** — whenever\n## In progress\n## Waiting on you\n")
    db = os.path.join(HOME, ".local/state/assistant/events.db")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TG_STATUS_", "TG_ADAPTER", "ASSISTANT_EVENTS"))}
    env.update(
        HOME=HOME,
        TG_STATUS_DIR=os.path.join(T, "state"),
        TG_STATUS_TODO=os.path.join(T, "TODO.md"),
        TG_STATUS_DEBOUNCE="0",
        TG_STATUS_NOW="2026-10-17 06:00",
    )
    st = lambda: json.load(open(os.path.join(T, "state/state.json")))  # noqa: E731

    def tgs(*args, extra=None):
        return subprocess.run([TGS, *args], env={**env, **(extra or {})}, capture_output=True, timeout=60)

    c = Consumer(db)
    c.start()
    try:
        r = tgs("note", "first")
        rs = rows(db)
        ok(
            "outbox: first board = send + pin rows, exit 0",
            r.returncode == 0 and [x["op"] for x in rs] == ["send", "pin"],
            (r.returncode, r.stderr[-300:], [x["op"] for x in rs]),
        )
        b = json.loads(rs[0]["body"]) if rs else {}
        ok(
            "outbox: board send is silent HTML, never held or deduped, Talk kept by the adapter",
            b.get("silent") is True
            and b.get("format") == "html"
            and b.get("hold") is False
            and b.get("dedupe") is False
            and b.get("on_press") == "keep"
            and "call:talk" in json.dumps(b.get("buttons"))
            and b.get("sender") == "tg-status"
            and rs[0]["urgent"] == 0
            and rs[0]["chat"] == OWNER,
            b,
        )
        mid = st().get("message_id")
        ok("outbox: the sent id is the board's message_id", mid == 701, mid)
        ok(
            "outbox: pin row names it, silently",
            len(rs) > 1 and json.loads(rs[1]["body"]) == {"message_id": 701, "notify": False, "sender": "tg-status"},
            rs[1:2],
        )
        ok(
            "outbox: no token file was needed (no Claude dir in HOME)",
            not os.path.exists(os.path.join(HOME, ".claude")),
        )

        tgs("note", "second")
        rs = rows(db)
        last = json.loads(rs[-1]["body"]) if rs else {}
        ok(
            "outbox: a change = one edit row of the board",
            [x["op"] for x in rs[2:]] == ["edit"]
            and last.get("message_id") == 701
            and "second" in last.get("text", "")
            and "call:talk" in json.dumps(last.get("buttons")),
            [x["op"] for x in rs],
        )
        tgs("refresh")
        ok("outbox: no change = no row", len(rows(db)) == 3, len(rows(db)))

        c.fail_edits = True
        tgs("note", "third")
        c.fail_edits = False
        rs = rows(db)
        ok(
            "outbox: edit 'not found' -> a new board is sent and pinned",
            [x["op"] for x in rs[3:]] == ["edit", "send", "pin"]
            and rs[3]["status"] == "failed"
            and st().get("message_id") == 702,
            ([x["op"] for x in rs[3:]], st().get("message_id")),
        )

        tgs("reset")
        rs = rows(db)
        ops = [(x["op"], json.loads(x["body"]).get("message_id"), json.loads(x["body"]).get("unpin")) for x in rs[6:]]
        ok(
            "outbox: reset = unpin + 'moved' edit of the old one, then send + pin a fresh one",
            ops[:2] == [("pin", 702, True), ("edit", 702, None)]
            and [o[0] for o in ops[2:]] == ["send", "pin"]
            and st().get("message_id") == 703,
            ops,
        )

        open(os.path.join(HOME, ".config/tg-adapter/direct-tg-status"), "w").close()
        n = len(rows(db))
        r = tgs("note", "switched")
        ok(
            "switch direct-tg-status: no outbox row (direct path; here it fails on the absent token file)",
            len(rows(db)) == n and r.returncode == 1,
            (len(rows(db)), r.returncode),
        )
        os.remove(os.path.join(HOME, ".config/tg-adapter/direct-tg-status"))

        fake.send_signal(signal.SIGKILL)
        fake.wait(5)
        n = len(rows(db))
        r = tgs("note", "adapter gone")
        ok(
            "adapter gone (stale PID file): the direct path, no outbox row",
            len(rows(db)) == n and r.returncode == 1,
            (len(rows(db)), r.returncode),
        )
    finally:
        c.stop.set()
        c.join(5)
        try:
            fake.send_signal(signal.SIGKILL)
        except OSError:
            pass
        shutil.rmtree(T, ignore_errors=True)
    print(f"{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
