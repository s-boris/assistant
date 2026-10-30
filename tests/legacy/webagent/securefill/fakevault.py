#!/usr/bin/env python3
"""A fake vault for securefill tests, with the real vault's personal.py (parse_request, preview, seal, FillSessions)
and synthetic values from $SF_TEST_DIR/values.json.

Fill sessions (/fill/session/start|poll|release|end, GET /fill/sessions): the real PD.FillSessions with a fake vault
bot that appends every message/edit to vaultbot.jsonl. $SF_TEST_DIR/mode = approve (his ✅ tapped on the 2nd poll) |
deny (❌ on the 2nd poll) | never. $SF_TEST_DIR/sessions = off: those endpoints answer 404 not_found like an older
vault, so securefill falls back to the per-fill path below.

Per fill (older path): /fill/fields|request|release|import; approval by the same mode file (released on the 2nd
poll); previews (what the owner card would show) -> previews.jsonl.
Usage: fakevault.py PORT"""
import json, os, secrets, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
sys.path.insert(0, os.path.expanduser("~/opt/claude-vault/app"))
import personal as PD  # noqa: E402
from vaultlib import VaultError  # noqa: E402
PORT = int(sys.argv[1])
DIR = os.environ["SF_TEST_DIR"]
REQS, POLLS, LOCK = {}, {}, threading.Lock()


def values():
    with open(os.path.join(DIR, "values.json")) as f:
        return json.load(f)


def mode():
    try:
        return open(os.path.join(DIR, "mode")).read().strip()
    except OSError:
        return "approve"


def sessions_on():
    try:
        return open(os.path.join(DIR, "sessions")).read().strip() != "off"
    except OSError:
        return True


def _append(name, rec):
    with LOCK:
        with open(os.path.join(DIR, name), "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


class Store:
    def status(self):
        return {"set": len(values())}

    def values(self, fields):
        v = values()
        missing = [f for f in fields if f not in v]
        if missing:
            raise VaultError("not stored yet: " + ", ".join(missing), 409, "fields_missing", missing=missing)
        return {f: v[f] for f in fields}


class Audit:
    def log(self, event, **kw):
        _append("audit.jsonl", dict(kw, ev=event))


class Bot:
    n = 0

    def chat_id(self):
        return "111"

    def bot_id(self):
        return 4242

    def send(self, text, buttons=None, **kw):
        Bot.n += 1
        _append("vaultbot.jsonl", {"op": "send", "mid": 500 + Bot.n, "text": text, "buttons": buttons})
        return 500 + Bot.n

    def edit(self, message_id, text, html=False, buttons=None):
        _append("vaultbot.jsonl", {"op": "edit", "mid": message_id, "text": text, "buttons": buttons})

    def answer(self, callback_id, text):
        _append("vaultbot.jsonl", {"op": "answer", "text": text})


class Approver:
    def healthy(self):
        return True


FS = PD.FillSessions(Store(), Bot(), Audit(), secrets.token_bytes(32), Path(DIR) / "fill-sessions.json",
                     approver=Approver(), log=lambda *a, **k: None)


def tap(sid):
    """His tap in the vault bot, by the mode file: ✅ or ❌ on the 2nd poll of a pending session."""
    with LOCK:
        POLLS[sid] = POLLS.get(sid, 0) + 1
        n = POLLS[sid]
    s = FS.sessions.get(sid)
    m = mode()
    if s and s.get("state") == "pending" and n >= 2 and m in ("approve", "deny") and s.get("message_id"):
        FS.on_callback({"id": "cb", "data": f"G:{'a' if m == 'approve' else 'd'}:{sid}:{s['nonce']}",
                        "from": {"id": 111}, "message": {"message_id": s["message_id"], "chat": {"id": 111}}})


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def nope(self):
        self.reply({"error": f"no such endpoint: {self.command} {self.path}", "code": "not_found"}, 404)

    def do_GET(self):
        if self.headers.get("Authorization") != "Bearer test-token":
            return self.reply({"error": "unauthorized"}, 401)
        if self.path == "/fill/fields":
            v = values()
            return self.reply({"fields": {f: f in v for f in PD.FIELDS}, "set": len(v)})
        if self.path == "/fill/sessions" and sessions_on():
            return self.reply({"sessions": FS.list()})
        self.nope()

    def do_POST(self):
        if self.headers.get("Authorization") != "Bearer test-token":
            return self.reply({"error": "unauthorized"}, 401)
        b = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        try:
            if self.path.startswith("/fill/session/"):
                if not sessions_on():
                    return self.nope()
                op = self.path.rsplit("/", 1)[1]
                if op == "start":
                    return self.reply(FS.start(b))
                if op == "poll":
                    tap(str(b.get("session_id") or ""))
                    return self.reply(FS.poll(b.get("session_id"), min(int(b.get("wait") or 0), 3), b.get("code"),
                                              b.get("task")))
                if op == "release":
                    return self.reply(FS.release(b))
                if op == "end":
                    return self.reply(FS.end(b.get("session_id"), b.get("why") or "agent", b.get("task")))
                return self.nope()
            if self.path == "/fill/request":
                req = PD.parse_request(b)
                v = values()
                missing = [f for f in req["fields"] if f not in v]
                if missing:
                    return self.reply({"error": "not stored", "code": "fields_missing"}, 409)
                rid = secrets.token_hex(6)
                with LOCK:
                    REQS[rid] = {"req": req, "polls": 0, "state": "pending"}
                with open(os.path.join(DIR, "previews.jsonl"), "a") as f:
                    f.write(json.dumps({"rid": rid, "lines": PD.preview(req), "fields": req["fields"]}) + "\n")
                return self.reply({"request_id": rid, "status": "pending", "approval": "buttons"})
            if self.path == "/fill/release":
                rid = b.get("request_id")
                with LOCK:
                    r = REQS.get(rid)
                    if not r:
                        return self.reply({"error": "unknown request id", "code": "unknown_request"}, 404)
                    if r["state"] == "released":
                        return self.reply({"error": "request is released", "code": "request_released"}, 410)
                    r["polls"] += 1
                    m = mode()
                    if m == "deny":
                        r["state"] = "denied"
                        return self.reply({"error": "request is denied", "code": "request_denied"}, 410)
                    if m == "never" or r["polls"] < 2:
                        return self.reply({"status": "pending", "approval": "buttons", "request_id": rid})
                    r["state"] = "released"
                    vals = {f: values()[f] for f in r["req"]["fields"]}
                box = PD.seal(json.dumps(vals).encode(), r["req"]["pub"], rid)
                return self.reply({"status": "released", "kind": "fill", "fields": sorted(vals), "sealed": box})
            if self.path == "/fill/import":
                clean, bad = PD.check_values(b.get("values") or {})
                return self.reply({"stored": sorted(clean), "already_set": [], "invalid": bad})
        except VaultError as e:
            return self.reply(dict(getattr(e, "extra", None) or {}, error=str(e), code=getattr(e, "code", None)),
                              getattr(e, "status", 400) or 400)
        self.nope()


ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
