#!/usr/local/bin/python3.13 -I
"""tg-buttons and tg-voice on the one outbox. See run.sh.

test_outbox.py BIN_DIR     "ok   - ..." / "FAIL - ..." lines, then "N passed, M failed"; exit 0 = all pass
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BIN = os.path.abspath(sys.argv[1])
T = tempfile.mkdtemp(prefix="tg-senders-tests.")
FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
HOME = os.path.join(T, "home")
DB = os.path.join(HOME, ".local/state/assistant/events.db")
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
    """Ends queued live rows like the adapter (claim while queued, then sent with ids from 900, or failed when the
    text/caption contains FAIL)."""

    def __init__(self):
        super().__init__(daemon=True)
        self.stop, self.next_id = threading.Event(), 900

    def run(self):
        while not self.stop.is_set():
            if os.path.exists(DB):
                try:
                    con = sqlite3.connect(DB, timeout=5, isolation_level=None)
                    for rid, body in con.execute(
                        "SELECT id, body FROM outbox WHERE status='queued' AND mode='live' ORDER BY id"
                    ).fetchall():
                        if not con.execute(
                            "UPDATE outbox SET status='sending', attempts=1 WHERE id=? AND status='queued'", (rid,)
                        ).rowcount:
                            continue
                        if "FAIL" in body:
                            con.execute(
                                "UPDATE outbox SET status='failed', last_error=? WHERE id=?",
                                ("Call to 'sendMessage' failed! (400: Bad Request: chat not found)", rid),
                            )
                            continue
                        self.next_id += 1
                        con.execute(
                            "UPDATE outbox SET status='sent', tg_message_id=?, result=? WHERE id=?",
                            (self.next_id, json.dumps({"ids": [self.next_id], "notes": []}), rid),
                        )
                    con.close()
                except sqlite3.OperationalError:
                    pass
            time.sleep(0.03)


class FakeHub:
    """The hub API as tg-outbox uses it (consumers, outbox rows, file uploads); records every call."""

    def __init__(self):
        self.calls, self.age, self.next_id = [], 0.5, 10000100
        hub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _go(self, method):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                is_json = self.headers.get("Content-Type") == "application/json" and raw
                hub.calls.append((method, self.path, json.loads(raw) if is_json else len(raw)))
                if self.path == "/outbox/consumers":
                    out = {"consumers": [{"bot": "live", "caller": "tg", "age": hub.age}]}
                elif self.path == "/tg/files":
                    out = {"ref": "hubfile:out/" + (self.headers.get("X-File-Name") or "f")}
                elif method == "POST" and self.path == "/outbox":
                    hub.next_id += 1
                    out = {"id": hub.next_id}
                elif method == "GET" and self.path.startswith("/outbox/"):
                    rid = int(self.path.rsplit("/", 1)[1])
                    out = {"id": rid, "status": "sent", "result": json.dumps({"ids": [rid - 10000000]})}
                else:
                    out = {"error": "not_found"}
                data = json.dumps(out).encode()
                self.send_response(404 if "error" in out else 200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._go("GET")

            def do_POST(self):
                self._go("POST")

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]  # noqa: UP031
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def posted(self):
        return [b for m, p, b in self.calls if m == "POST" and p == "/outbox"]


def rows():
    if not os.path.exists(DB):
        return []
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
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
        f.write("%d\ntg-adapter\n" % fake.pid)  # noqa: UP031
    ogg = os.path.join(T, "note.ogg")
    shutil.copyfile(os.path.join(FIX, "note.ogg"), ogg)  # 2 s of 440 Hz, OGG/Opus mono (made with ffmpeg)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TG_", "ASSISTANT_EVENTS", "TELEGRAM_", "HUB_"))}
    env.update(HOME=HOME)
    if not shutil.which("ffprobe"):  # no ffmpeg here (the CI runner): its stand-in, which knows only that fixture
        env["PATH"] = FIX + os.pathsep + env.get("PATH", "")

    def run(*argv, extra=None, stdin=b""):
        return subprocess.run(list(argv), env={**env, **(extra or {})}, input=stdin, capture_output=True, timeout=60)

    c = Consumer()
    c.start()
    try:
        tgb = os.path.join(BIN, "tg-buttons")
        rows_json = '[[{"text":"Yes","data":"x:yes:1"},{"text":"Map","url":"https://maps.example/x"}]]'
        r = run(tgb, "Leave now?", rows_json)
        rs = rows()
        b = json.loads(rs[-1]["body"]) if rs else {}
        ok(
            "tg-buttons: one urgent live row with text and buttons, exit 0",
            r.returncode == 0
            and len(rs) == 1
            and rs[0]["urgent"] == 1
            and b.get("text") == "Leave now?"
            and b.get("sender") == "tg-buttons"
            and b.get("buttons") == json.loads(rows_json)
            and rs[0]["chat"] == OWNER
            and rs[0]["status"] == "sent",
            (r.returncode, r.stderr[-300:], rs),
        )
        r = run(tgb, "Later?", '[[{"text":"Ok","data":"y"}]]', extra={"TG_SOFT": "1"})
        rs = rows()
        ok("tg-buttons: TG_SOFT=1 = non-urgent row", r.returncode == 0 and rs[-1]["urgent"] == 0, rs[-1:])
        r = run(tgb, "FAIL me", '[[{"text":"Ok","data":"y"}]]')
        ok(
            "tg-buttons: a failed row = exit 1, Telegram's reason on stderr",
            r.returncode == 1 and b"chat not found" in r.stderr,
            (r.returncode, r.stderr[-200:]),
        )
        n = len(rows())
        r = run(tgb, "x", '[[{"text":"Ok","data":"' + "z" * 70 + '"}]]')
        ok(
            "tg-buttons: bad ROWS_JSON still exit 2 before anything is queued",
            r.returncode == 2 and len(rows()) == n,
            r.returncode,
        )
        r = run(tgb, "seam", '[[{"text":"Ok","data":"y"}]]', extra={"TG_BUTTONS_API": "http://127.0.0.1:9"})
        ok("tg-buttons: a test seam set = never the outbox", len(rows()) == n and r.returncode != 0, len(rows()))

        tgv = os.path.join(BIN, "tg-voice")
        r = run(tgv, ogg, "Train at 14:30", "--silent", "--reply-to", "99")
        rs = rows()
        b = json.loads(rs[-1]["body"]) if rs else {}
        ok(
            "tg-voice: the note as a file row (caption, duration, silent, reply), the id printed",
            r.returncode == 0
            and r.stdout.strip().isdigit()
            and json.loads(rs[-1]["files"] or "[]") == [ogg]
            and b.get("caption") == "Train at 14:30"
            and b.get("duration") == 2
            and b.get("silent") is True
            and b.get("reply_to") == 99
            and rs[-1]["urgent"] == 1
            and b.get("sender") == "tg-voice",
            (r.returncode, r.stdout, r.stderr[-300:], b),
        )
        r = run(tgv, ogg, "FAIL")
        ok("tg-voice: a failed row = exit 1", r.returncode == 1, (r.returncode, r.stderr[-200:]))
        n = len(rows())
        r = run(tgv, os.path.join(T, "missing.ogg"))
        ok("tg-voice: bad input still exit 2, nothing queued", r.returncode == 2 and len(rows()) == n, r.returncode)

        open(os.path.join(HOME, ".config/tg-adapter/direct-tg-voice"), "w").close()
        r = run(tgv, ogg)
        ok(
            "tg-voice: switch direct-tg-voice = direct path (fails here: no token file), no row",
            len(rows()) == n and r.returncode != 0,
            (len(rows()), r.returncode),
        )
        os.remove(os.path.join(HOME, ".config/tg-adapter/direct-tg-voice"))

        fake.send_signal(signal.SIGKILL)
        fake.wait(5)
        n = len(rows())
        r = run(tgb, "gone", '[[{"text":"Ok","data":"y"}]]')
        ok(
            "adapter gone (stale PID file): tg-buttons takes the direct path, no row",
            len(rows()) == n and r.returncode != 0,
            (len(rows()), r.returncode),
        )

        # the hub: the in-container adapter is off (no PID file); the hub's adapter drains the hub outbox
        os.remove(os.path.join(HOME, ".local/state/tg-adapter/live/adapter.pid"))
        open(os.path.join(HOME, ".config/tg-adapter/live.off"), "w").close()
        hub = FakeHub()
        tok = os.path.join(T, "hub-token")
        with open(tok, "w") as f:
            f.write("t" * 40)
        os.chmod(tok, 0o600)
        hx = {"HUB_URL": hub.url, "HUB_TOKEN_FILE": tok}
        n = len(rows())
        r = run(tgb, "Hub card", '[[{"text":"Ok","data":"h:1"}]]', extra=hx)
        p = hub.posted()
        ok(
            "hub: tg-buttons = one urgent hub outbox row (text, buttons, sender), exit 0",
            r.returncode == 0
            and len(p) == 1
            and p[0]["body"].get("text") == "Hub card"
            and p[0]["body"].get("sender") == "tg-buttons"
            and p[0]["body"].get("buttons") == [[{"text": "Ok", "data": "h:1"}]]
            and p[0]["urgent"] is True
            and len(rows()) == n,
            (r.returncode, r.stderr[-300:], p),
        )
        r = run(tgv, ogg, "Hub note", extra=hx)
        p = hub.posted()
        up = [b for m, q, b in hub.calls if q == "/tg/files"]
        ok(
            "hub: tg-voice = the file uploaded, then a hub row (caption, sender), the id printed",
            r.returncode == 0
            and r.stdout.strip() == str(hub.next_id - 10000000).encode()
            and len(up) == 1
            and len(p) == 2
            and p[1]["body"].get("caption") == "Hub note"
            and p[1]["body"].get("sender") == "tg-voice"
            and p[1]["files"] == ["hubfile:out/note.ogg"],
            (r.returncode, r.stdout, r.stderr[-300:], p[1:]),
        )
        hub.age = 95.0
        r = run(tgb, "stale", '[[{"text":"Ok","data":"y"}]]', extra=hx)
        ok(
            "the hub's adapter gone (stale consumer): the direct path (fails here: no token file), no row",
            r.returncode != 0 and len(hub.posted()) == 2,
            (r.returncode, len(hub.posted())),
        )
        hub.srv.shutdown()
        ok(
            "no Claude dir (token, access file) was ever needed in HOME",
            not os.path.exists(os.path.join(HOME, ".claude")),
        )
    finally:
        c.stop.set()
        c.join(5)
        try:
            fake.send_signal(signal.SIGKILL)
        except OSError:
            pass
        shutil.rmtree(T, ignore_errors=True)
    print("%d passed, %d failed" % (passed, failed))  # noqa: UP031
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
