"""traces tests on synthetic transcripts (no real data). TRACES_BIN = the script under test."""

import gzip
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from datetime import UTC, datetime

BIN = os.environ.get("TRACES_BIN") or os.path.expanduser("~/bin/traces")
LIB = os.environ.get("TRACES_ASSISTANT_LIB") or os.path.join(
    os.path.dirname(os.path.realpath(BIN)), "lib"
)  # holds core/
T = datetime(2026, 10, 20, 10, 0, tzinfo=UTC).timestamp()
SID = "11111111-2222-3333-4444-555555555555"
AID = "a0123456789abcdef"
AID2 = "afedcba9876543210"
SECRET = "SECRET-BODY-TEXT"


def iso(dt):
    return datetime.fromtimestamp(T + dt, UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def usage(out, cr=1000, cw1h=200, cw5=0, inp=5):
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_input_tokens": cr,
        "cache_creation_input_tokens": cw1h + cw5,
        "cache_creation": {"ephemeral_1h_input_tokens": cw1h, "ephemeral_5m_input_tokens": cw5},
        "speed": "standard",
        "inference_geo": "global",
    }


def asst(dt, mid, content, u, stop, ref=None, model="claude-opus-5-5", sid=SID):
    return {
        "type": "assistant",
        "sessionId": sid,
        "uuid": f"u-{mid}-{dt}",
        "timestamp": iso(dt),
        "requestId": "req_" + mid,
        "requestRef": ref,
        "effort": "high",
        "message": {
            "id": mid,
            "model": model,
            "role": "assistant",
            "content": content,
            "stop_reason": stop,
            "usage": u,
        },
    }


def tool_use(tid, name, inp=None):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp or {}}


def result(dt, tid, text, err=False, sid=SID):
    return {
        "type": "user",
        "sessionId": sid,
        "uuid": f"r-{tid}",
        "timestamp": iso(dt),
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tid, "content": text, "is_error": err}],
        },
    }


def prompt(dt, uuid, text, kind, idx, turn_origin, meta=False):
    return {
        "type": "user",
        "sessionId": SID,
        "uuid": uuid,
        "timestamp": iso(dt),
        "isMeta": meta,
        "origin": {"kind": kind},
        "turnOrigin": turn_origin,
        "turnPosition": {"promptIndex": idx, "turnIndex": idx},
        "promptSource": "system",
        "message": {"role": "user", "content": text},
    }


def req(dt, rid):
    return {
        "type": "api-request",
        "sessionId": SID,
        "id": rid,
        "timestamp": iso(dt),
        "querySource": "repl_main_thread",
        "params": {"model": "claude-opus-5-5"},
    }


def tdur(dt, ms=99999999):
    return {
        "type": "system",
        "subtype": "turn_duration",
        "sessionId": SID,
        "durationMs": ms,
        "timestamp": iso(dt),
        "uuid": f"td-{dt}",
    }


MAIN1 = [
    prompt(
        0,
        "p1",
        '<channel source="plugin:telegram-plus:telegram" chat_id="1" message_id="9" user="x" ts="1">'
        + SECRET
        + "</channel>",
        "channel",
        1,
        "peer",
        meta=True,
    ),
    req(1, "R1"),
    asst(3, "M1", [{"type": "thinking", "thinking": SECRET}], usage(3), None, "R1"),
    asst(4, "M1", [tool_use("tu1", "Bash", {"command": "ls /nonexistent-" + SECRET})], usage(100), "tool_use", "R1"),
    result(5, "tu1", "Exit code 2\nls: cannot access " + SECRET, err=True),
    req(6, "R2"),
    asst(
        8,
        "M2",
        [tool_use("tu2", "mcp__plugin_telegram-plus_telegram__reply", {"text": SECRET})],
        usage(50),
        "tool_use",
        "R2",
    ),
    result(9, "tu2", "sent"),
    asst(10, "M3", [{"type": "text", "text": SECRET}], usage(20), "end_turn"),
    tdur(11),
    {"type": "api-request-blob", "sessionId": SID, "hash": "h", "message": {"role": "user", "content": SECRET}},
    prompt(
        100,
        "p2",
        "The heartbeat plugin sent a message:\n[heartbeat] Idle 10+ min, 2 new notes in ~/inbox",
        "plugin",
        2,
        "system",
    ),
    asst(
        102,
        "M4",
        [tool_use("toolu_A1", "Agent", {"description": "Build thing X", "prompt": SECRET})],
        usage(40),
        "tool_use",
    ),
    result(103, "toolu_A1", "launched"),
    asst(104, "M5", [tool_use("tu5", "Bash", {"command": "cat ~/.secrets/x"})], usage(30), "tool_use"),
    result(105, "tu5", "PreToolUse:Bash hook error: vault-guard (secrets-path): blocked " + SECRET, err=True),
    tdur(110),
    prompt(
        200,
        "p3",
        f"<task-notification>\n<task-id>{AID}</task-id>\n<tool-use-id>toolu_A1</tool-use-id>\n<status>"
        f"completed</status>\n<summary>{SECRET}</summary>\n<usage><subagent_tokens>5000</subagent_tokens><tool_uses>3"
        "</tool_uses><duration_ms>60000</duration_ms></usage>\n</task-notification>",
        "task-notification",
        3,
        "task_notification",
    ),
    asst(205, "M6", [{"type": "text", "text": "ok"}], usage(10), "end_turn"),
]
MAIN2 = [
    tdur(206),
    prompt(
        300,
        "p4",
        '<channel source="plugin:telegram-plus:telegram" chat_id="1" message_id="10" user="x" ts="2" '
        'event_type="button" callback_data="mt:play:1">tap</channel>',
        "channel",
        4,
        "peer",
        meta=True,
    ),
    asst(302, "M7", [tool_use("tu7", "mcp__plugin_telegram-plus_telegram__react", {})], usage(5), "tool_use"),
    result(303, "tu7", "ok"),
    tdur(304),
]

HUBSEND = [  # a voice call answered through the hub's MCP voice_reply (no Telegram reply, no Bash sender)
    prompt(
        400,
        "p5",
        '<channel source="plugin:telegram-plus:telegram" chat_id="1" message_id="11" user="x" ts="3">'
        + SECRET
        + "</channel>",
        "channel",
        5,
        "peer",
        meta=True,
    ),
    asst(404, "M8", [tool_use("tu8", "mcp__hub__voice_reply", {"text": SECRET})], usage(5), "tool_use"),
    result(405, "tu8", "ok"),
    asst(407, "M9", [tool_use("tu9", "mcp__hub__phone_chat", {"text": SECRET})], usage(5), "tool_use"),
    result(408, "tu9", "ok"),
    asst(409, "M10", [{"type": "text", "text": "ok"}], usage(5), "end_turn"),
    tdur(410),
]


def agent_lines(sid=SID, model="claude-sonnet-5-5", base=103):
    return [
        {
            "type": "user",
            "sessionId": sid,
            "agentId": AID,
            "uuid": "a1",
            "timestamp": iso(base),
            "message": {"role": "user", "content": SECRET},
        },
        req(base + 1, "AR1"),
        asst(
            base + 3,
            "AM1",
            [{"type": "thinking", "thinking": SECRET}],
            usage(2, cr=5000, cw1h=0, cw5=300),
            None,
            "AR1",
            model,
        ),
        asst(
            base + 5,
            "AM1",
            [tool_use("atu1", "Read", {"file_path": "/x"})],
            usage(400, cr=5000, cw1h=0, cw5=300),
            "tool_use",
            "AR1",
            model,
        ),
        result(base + 6, "atu1", "File does not exist.", err=True),
        asst(
            base + 57,
            "AM2",
            [{"type": "text", "text": SECRET}],
            usage(600, cr=6000, cw1h=0, cw5=100),
            "end_turn",
            None,
            model,
        ),
    ]


def write_jsonl(path, rows, mode="w"):
    with open(path, mode) as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


class TracesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="traces-test-")
        self.proj = os.path.join(self.tmp, "proj")
        self.state = os.path.join(self.tmp, "state")
        self.sub = os.path.join(self.proj, SID, "subagents")
        os.makedirs(self.sub)
        self.main = os.path.join(self.proj, SID + ".jsonl")
        write_jsonl(self.main, MAIN1)
        write_jsonl(os.path.join(self.sub, f"agent-{AID}.jsonl"), agent_lines())
        with open(os.path.join(self.sub, f"agent-{AID}.meta.json"), "w") as f:
            json.dump(
                {
                    "agentType": "general-purpose",
                    "description": "Build thing X",
                    "toolUseId": "toolu_A1",
                    "spawnDepth": 1,
                    "effort": "xhigh",
                },
                f,
            )
        with gzip.open(os.path.join(self.sub, f"agent-{AID2}.jsonl.gz"), "wt") as f:
            for r in agent_lines(base=50):
                f.write(json.dumps(r) + "\n")
        for p in (self.main,):
            os.utime(p, (T + 210, T + 210))
        self.env = dict(
            os.environ, TRACES_PROJECT=self.proj, TRACES_STATE_DIR=self.state, TRACES_NOW=str(T + 3600), USAGE="off"
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_t(self, *args, ok=True):
        p = subprocess.run(
            [sys.executable, "-I", BIN] + list(args), env=self.env, capture_output=True, text=True, timeout=60
        )
        if ok:
            self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def db(self):
        c = sqlite3.connect(os.path.join(self.state, "traces.db"))
        c.row_factory = sqlite3.Row
        return c

    def turns(self):
        return [dict(r) for r in self.db().execute("SELECT * FROM turns ORDER BY start_ts")]

    def test_turns_triggers_costs(self):
        self.run_t("ingest", "--since-days", "100000", "--quiet")
        t = self.turns()
        self.assertEqual(len(t), 3)
        a, b, c = t
        self.assertEqual(
            (a["trigger"], a["channel"], a["start_kind"], a["outcome"]), ("owner", "telegram", "text", "replied")
        )
        self.assertEqual(a["dur_ms"], 11000)  # span, not the bogus durationMs
        self.assertEqual(a["first_reply_ms"], 8000)
        self.assertEqual(a["api_calls"], 3)
        self.assertEqual(a["output"], 170)  # final usage per message (100 + 50 + 20)
        self.assertEqual(json.loads(a["error_counts"]), {"Bash:exit_code": 1})
        self.assertEqual(json.loads(a["tool_counts"]), {"Bash": 1, "telegram.reply": 1})
        self.assertEqual(a["api_max_ms"], 3000)  # R1 at +1 s -> last block of M1 at +4 s
        exp = 3 * (5 * 4 + 1000 * 0.2 + 200 * 8) / 1e6 + 170 * 20 / 1e6
        self.assertAlmostEqual(a["cost"], exp, places=6)
        self.assertEqual(
            (b["trigger"], b["start_kind"], b["agents_launched"], b["hook_blocks"]), ("inbox", "notes", 1, 1)
        )
        self.assertIn("Bash:hook:vault-guard", json.loads(b["error_counts"]))
        self.assertEqual(
            (c["trigger"], c["channel"], c["start_kind"], c["is_open"]), ("agent", "agent", "agent_completed", 1)
        )

    def test_agents_and_inheritance(self):
        self.run_t("ingest", "--since-days", "100000", "--quiet")
        out = json.loads(self.run_t("agents", "--since", "2026-10-01 00:00", "--json"))
        by = {a["id"]: a for a in out}
        self.assertEqual(set(by), {AID, AID2})
        a = by[AID]
        self.assertEqual(
            (a["description"], a["effort"], a["status"], a["trigger"], a["notif_tokens"]),
            ("Build thing X", "xhigh", "done", "inbox", 5000),
        )
        self.assertIn("trigger_kind", a)  # agentfeed (the dashboard's Agents tab) shows trigger + kind
        exp = (5 * 2 + 400 * 10 + 5000 * 0.1 + 300 * 2 * 1.25 + 5 * 2 + 600 * 10 + 6000 * 0.1 + 100 * 2 * 1.25) / 1e6
        self.assertAlmostEqual(a["cost"], exp, places=6)
        self.assertEqual(a["errors"], 1)
        self.assertEqual(by[AID2]["status"], "done")  # gz, no meta: final answer seen
        # a bare date is 00:00 that day (it crashed with ValueError until 29.10)
        bare = json.loads(self.run_t("agents", "--since", "2026-10-01", "--json"))
        self.assertEqual({a["id"] for a in bare}, {AID, AID2})

    def test_hub_senders_count_as_the_first_reply(self):
        write_jsonl(self.main, MAIN2 + HUBSEND, "a")
        self.run_t("ingest", "--since-days", "100000", "--quiet")
        t = [x for x in self.turns() if x["start_ts"] == T + 400]
        self.assertEqual(len(t), 1)
        self.assertEqual(t[0]["first_reply_ms"], 4000)  # hub.voice_reply at +4 s, not phone_chat
        self.assertEqual(json.loads(t[0]["tool_counts"]), {"hub.voice_reply": 1, "hub.phone_chat": 1})

    def test_incremental_and_gz_move(self):
        self.run_t("ingest", "--since-days", "100000", "--quiet")
        before = {t["id"]: t["cost"] for t in self.turns()}
        acost = self.db().execute("SELECT cost FROM agents WHERE id=?", (AID,)).fetchone()[0]
        write_jsonl(self.main, MAIN2, "a")
        p = os.path.join(self.sub, f"agent-{AID}.jsonl")
        with open(p, "rb") as f, gzip.open(p + ".gz", "wb") as g:
            g.write(f.read())
        os.remove(p)
        self.run_t("ingest", "--quiet")
        t = self.turns()
        self.assertEqual(len(t), 4)
        self.assertEqual(t[2]["is_open"], 0)
        self.assertEqual(t[2]["dur_ms"], 6000)
        self.assertEqual((t[3]["start_kind"], t[3]["outcome"]), ("tap", "reacted"))
        for x in t[:2]:
            self.assertAlmostEqual(x["cost"], before[x["id"]], places=9)
        rows = self.db().execute("SELECT cost, path FROM agents WHERE id=?", (AID,)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0][0], acost, places=9)
        self.assertTrue(rows[0][1].endswith(".gz"))
        self.run_t("ingest", "--quiet")  # nothing changed: nothing double counted
        self.assertEqual(len(self.turns()), 4)

    def test_privacy_and_outputs(self):
        self.run_t("ingest", "--since-days", "100000", "--quiet")
        dump = "\n".join(self.db().iterdump())
        self.assertNotIn(SECRET, dump)
        self.assertNotIn("secrets-path", dump)
        brief = self.run_t("brief", "--hours", "48")
        self.assertLessEqual(len(brief.strip().splitlines()), 8)
        self.assertNotIn(SECRET, brief)
        rep = self.run_t("report", "--days", "2")
        self.assertIn("Cost by trigger", rep)
        self.assertNotIn(SECRET, rep)
        d = json.loads(self.run_t("dash-json", "--days", "2"))
        for k in ("tiles", "daily", "by_trigger", "agents", "slow", "errors", "latency"):
            self.assertIn(k, d)
        self.assertEqual((d["tiles"]["turns_24h"], d["tiles"]["agents_24h"]), (3, 2))
        self.assertGreater(d["tiles"]["cost_24h"], 0)
        self.assertNotIn(SECRET, json.dumps(d))
        # the dashboard's week trend: per day his reply time, tool errors, hook blocks and bad agent ends (numbers only)
        day = d["daily"][-1]
        self.assertEqual((day["replies"], day["reply_p50_ms"], day["reply_p95_ms"]), (1, 8000, 8000))
        self.assertEqual((day["hook_blocks"], day["agents_bad"]), (1, 0))
        self.assertGreaterEqual(day["errors"], 2)
        self.assertGreaterEqual(day["tools"], day["errors"])

    def test_otel_receiver(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        os.makedirs(self.state, exist_ok=True)
        proc = subprocess.Popen(
            [sys.executable, "-I", BIN, "otel", "serve", "--port", str(port)],
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            for _ in range(50):
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                    break
                except OSError:
                    time.sleep(0.1)

            def post(path, body):
                r = urllib.request.Request(
                    "http://127.0.0.1:%d%s" % (port, path),  # noqa: UP031
                    data=json.dumps(body).encode(),
                    headers={"Content-Type": "application/json"},
                )
                return urllib.request.urlopen(r, timeout=5).status

            def kv(k, v):
                return {
                    "key": k,
                    "value": {"doubleValue": v}
                    if isinstance(v, float)
                    else {"intValue": str(v)}
                    if isinstance(v, int)
                    else {"stringValue": v},
                }

            logs = {
                "resourceLogs": [
                    {
                        "resource": {"attributes": [kv("service.name", "claude-code"), kv("user.email", SECRET)]},
                        "scopeLogs": [
                            {
                                "logRecords": [
                                    {
                                        "timeUnixNano": str(int(T * 1e9)),
                                        "attributes": [
                                            kv("event.name", "api_request"),
                                            kv("session.id", SID),
                                            kv("model", "claude-opus-5-5"),
                                            kv("cost_usd", 0.5),
                                            kv("duration_ms", 1234),
                                            kv("input_tokens", 10),
                                            kv("prompt", SECRET),
                                        ],
                                    }
                                ]
                            }
                        ],
                    }
                ]
            }
            spans = {
                "resourceSpans": [
                    {
                        "resource": {"attributes": []},
                        "scopeSpans": [
                            {
                                "spans": [
                                    {
                                        "name": "claude_code.llm_request",
                                        "startTimeUnixNano": str(int(T * 1e9)),
                                        "endTimeUnixNano": str(int((T + 2) * 1e9)),
                                        "attributes": [
                                            kv("agent_id", AID),
                                            kv("ttft_ms", 800),
                                            kv("user_prompt", SECRET),
                                        ],
                                    }
                                ]
                            }
                        ],
                    }
                ]
            }
            self.assertEqual(post("/v1/logs", logs), 200)
            self.assertEqual(post("/v1/traces", spans), 200)
            self.assertEqual(post("/v1/metrics", {"resourceMetrics": []}), 200)
        finally:
            proc.terminate()
            proc.wait(5)
        rows = [dict(r) for r in self.db().execute("SELECT * FROM otel ORDER BY kind")]
        self.assertEqual(len(rows), 2)
        ev = [r for r in rows if r["kind"] == "event"][0]
        sp = [r for r in rows if r["kind"] == "span"][0]
        self.assertEqual((ev["name"], ev["cost"], ev["ms"]), ("api_request", 0.5, 1234))
        self.assertEqual((sp["agent_id"], sp["ttft_ms"], sp["ms"]), (AID, 800, 2000))
        self.assertNotIn(SECRET, "\n".join(self.db().iterdump()))

    def _fake_phoenix(self):
        """A fake Phoenix: OTLP protobuf in, the REST/GraphQL reads `traces phoenix` uses out."""
        import http.server
        import threading
        import urllib.parse

        got = self.got = []
        TIDX = "0af7651916cd43dd8448eb211c80319c"

        def iso_(dt):
            return datetime.fromtimestamp(T + dt, UTC).isoformat()

        def span(name, sid, parent, st, en, **a):
            return {
                "name": name,
                "context": {"trace_id": TIDX, "span_id": sid},
                "parent_id": parent,
                "span_kind": "LLM" if "llm" in name else "TOOL",
                "start_time": iso_(st),
                "end_time": iso_(en),
                "status_code": "OK",
                "attributes": a,
            }

        spans = [
            span("claude_code.interaction", "s1", None, 1, 20, **{"session.id": SID}),
            span(
                "claude_code.llm_request",
                "s2",
                "s1",
                2,
                5,
                **{"llm": {"model_name": "claude-opus-5-5", "token_count": {"total": 1200}}},
            ),
            span("claude_code.tool", "s3", "s1", 6, 9, **{"tool": {"name": "Bash"}}),
            span("gateway.reminders.list", "s4", "s3", 7, 8, **{"gateway.code": "ok", "tool.name": "reminders.list"}),
        ]

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, obj, ctype="application/json"):
                b = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.path == "/v1/traces":
                    got.append((self.headers.get("Content-Type"), body))
                    return self.reply(b"")
                if self.path == "/graphql":
                    return self.reply(
                        {
                            "data": {
                                "projectTraceRetentionPolicies": {
                                    "edges": [
                                        {
                                            "node": {
                                                "name": "Default",
                                                "cronExpression": "0 0 * * 0",
                                                "rule": {"maxDays": 30.0},
                                                "projects": {"edges": [{"node": {"name": "brain"}}]},
                                            }
                                        }
                                    ]
                                }
                            }
                        }
                    )
                self.send_response(404)
                self.end_headers()

            def do_GET(self):
                u = urllib.parse.urlparse(self.path)
                q = urllib.parse.parse_qs(u.query)
                if u.path == "/healthz":
                    return self.reply(b"OK", "text/plain")
                if u.path == "/v1/projects":
                    return self.reply({"data": [{"name": "brain", "id": "UHJv"}], "next_cursor": None})
                if u.path == "/v1/projects/brain/traces":
                    return self.reply(
                        {"data": [{"trace_id": TIDX, "start_time": iso_(1), "end_time": iso_(20)}], "next_cursor": None}
                    )
                if u.path == "/v1/projects/brain/spans":
                    if q.get("order") == ["asc"] and q.get("limit") == ["1"] and "name" not in q:  # the first span
                        return self.reply(
                            {"data": [span("claude_code.llm_request", "s0", None, -100, -99)], "next_cursor": None}
                        )
                    rows = [x for x in spans if "name" not in q or x["name"] in q["name"]]
                    return self.reply({"data": rows, "next_cursor": None})
                self.send_response(404)
                self.end_headers()

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        cfg = os.path.join(self.tmp, "config")
        os.makedirs(cfg)
        self.env.update(
            TRACES_PHOENIX_URL="http://127.0.0.1:%d" % srv.server_address[1],  # noqa: UP031
            TRACES_CONFIG_DIR=cfg,
            ASSISTANT_LIB=LIB,
            TRACES_GATEWAY_DB=os.path.join(self.tmp, "gateway.db"),
            NO_PROXY="*",
        )
        g = sqlite3.connect(self.env["TRACES_GATEWAY_DB"])
        g.execute(
            "CREATE TABLE calls (ts REAL, tool TEXT, surface TEXT, trace TEXT, trace_src TEXT, ms INTEGER, "
            "code TEXT, actor TEXT)"
        )
        g.executemany(
            "INSERT INTO calls VALUES (?,?,?,?,?,?,?,?)",
            [
                (T + 7, "reminders.list", "cli", TIDX, "env", 5, "ok", "brain"),
                (T + 30, "ha.state", "cli", "f" * 32, "new", 5, "ok", "brain"),
            ],
        )
        g.commit()
        g.close()
        return cfg, TIDX

    def test_phoenix_forwarding_privacy(self):
        cfg, _ = self._fake_phoenix()
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        os.makedirs(self.state, exist_ok=True)

        def kv(k, v):
            return {"key": k, "value": {"intValue": str(v)} if isinstance(v, int) else {"stringValue": v}}

        def body(name, sid):
            return {
                "resourceSpans": [
                    {
                        "resource": {
                            "attributes": [kv("service.name", "claude-code"), kv("user.email", "x@y.invalid")]
                        },
                        "scopeSpans": [
                            {
                                "spans": [
                                    {
                                        "traceId": "0af7651916cd43dd8448eb211c80319c",
                                        "spanId": sid,
                                        "name": name,
                                        "kind": 1,
                                        "startTimeUnixNano": str(int(T * 1e9)),
                                        "endTimeUnixNano": str(int((T + 2) * 1e9)),
                                        "status": {"code": 2, "message": SECRET},
                                        "attributes": [
                                            kv("model", "claude-opus-5-5"),
                                            kv("input_tokens", 10),
                                            kv("user_prompt", SECRET),
                                            kv("file_path", "/x/" + SECRET),
                                            kv("full_command", "echo " + SECRET),
                                        ],
                                    }
                                ]
                            }
                        ],
                    }
                ]
            }

        proc = subprocess.Popen(
            [sys.executable, "-I", BIN, "otel", "serve", "--port", str(port)],
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            for _ in range(50):
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                    break
                except OSError:
                    time.sleep(0.1)

            def post(b):
                r = urllib.request.Request(
                    "http://127.0.0.1:%d/v1/traces" % port,  # noqa: UP031
                    data=json.dumps(b).encode(),
                    headers={"Content-Type": "application/json"},
                )
                return urllib.request.urlopen(r, timeout=5).status

            self.assertEqual(post(body("claude_code.llm_request", "b7ad6b7169203331")), 200)
            for _ in range(60):
                if self.got:
                    break
                time.sleep(0.1)
            open(os.path.join(cfg, "phoenix-off"), "w").close()  # the rollback switch: nothing more goes out
            self.assertEqual(post(body("claude_code.tool", "00f067aa0ba902b7")), 200)
            time.sleep(1.5)
        finally:
            proc.terminate()
            proc.wait(5)
        self.assertEqual(len(self.got), 1)
        ctype, raw = self.got[0]
        self.assertEqual(ctype, "application/x-protobuf")
        self.assertIn(b"claude_code.llm_request", raw)
        self.assertIn(b"openinference.project.name", raw)
        self.assertIn(bytes.fromhex("0af7651916cd43dd8448eb211c80319c"), raw)
        for bad in (SECRET.encode(), b"x@y.invalid", b"/x/", b"user_prompt", b"echo "):
            self.assertNotIn(bad, raw)
        with open(os.path.join(self.state, "phoenix.json")) as f:
            st = json.load(f)
        self.assertEqual((st["received"], st["forwarded"], st["off"], st["lost"]), (2, 1, 1, 0))
        self.assertEqual(st["dropped_keys"]["claude_code.llm_request user_prompt"], 1)
        self.assertEqual(st["dropped_keys"]["resource user.email"], 2)  # counted while off too
        self.assertNotIn(SECRET, json.dumps(st))
        self.assertEqual(os.stat(os.path.join(self.state, "phoenix.json")).st_mode & 0o777, 0o600)
        self.assertNotIn(SECRET, "\n".join(self.db().iterdump()))

    def test_phoenix_views(self):
        _, tidx = self._fake_phoenix()
        self.run_t("ingest", "--quiet")
        st = json.loads(self.run_t("phoenix", "status", "--json"))
        self.assertEqual((st["health"], st["project_exists"], st["traces"]), ("OK", True, 1))
        self.assertEqual((st["retention"]["policy"], st["retention"]["max_days"]), ("Default", 30.0))
        j = st["joined"]
        self.assertEqual((j["owner_turns_traced"], j["interactions"]), (j["owner_turns"], 1))
        self.assertGreaterEqual(j["owner_turns"], 1)
        self.assertEqual((j["turns_traced"], j["since"]), (1, T - 100))  # one interaction in the fake
        self.assertGreater(j["turns"], j["turns_traced"])
        self.assertEqual((j["gateway_calls"], j["gateway_calls_in_cc_traces"]), (2, 1))
        self.assertIn("retention: Default policy, 30.0 days", self.run_t("phoenix", "status"))
        tr = self.run_t("phoenix", "trace", tidx)
        self.assertIn("4 spans (1 LLM, 1 tools, 1 gateway", tr)
        self.assertIn("    gateway.reminders.list", tr)  # nested under the Bash tool span
        d = json.loads(self.run_t("phoenix", "trace", tidx, "--json"))
        self.assertEqual((d["summary"]["tokens"], d["summary"]["slowest_tools"][0]["tool"]), (1200, "Bash"))
        slow = json.loads(self.run_t("phoenix", "slow", "--json"))
        self.assertEqual(slow[0]["root"], "claude_code.interaction")
        self.assertEqual(self.run_t("phoenix", "trace", "nothex", ok=False), "")
        self.env["TRACES_PHOENIX_URL"] = "http://127.0.0.1:9"
        p = subprocess.run(
            [sys.executable, "-I", BIN, "phoenix", "status"], env=self.env, capture_output=True, text=True, timeout=60
        )
        self.assertEqual(p.returncode, 4)
        self.assertIn("traces.db still has everything", p.stderr)
        self.assertIn("Daily", self.run_t("report", "--days", "2"))  # the fallback is untouched


if __name__ == "__main__":
    unittest.main()
