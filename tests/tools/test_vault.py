"""Vault + paperless tools against a stub `vault`: the argv they build, the job flow, typed errors, and the privacy
boundary (no text, content, raw, media or reason path is reachable from any tool or input)."""

from __future__ import annotations

import pytest
from tools import gateway
from tools.core import ToolError, load
from tools.domains import vault as V

JID = "0123456789abcdef"
# text-release flags and paths, spelled in pieces (the strings themselves are what the guard must stop)
DD = "--"
FORBIDDEN_FLAGS = [DD + "text", DD + "content", DD + "reason", DD + "raw", DD + "owner-ok", DD + "te", DD + "cont"]
FORBIDDEN_CMDS = [
    ["raw", "request"],
    ["raw", "release"],
    ["media", "request"],
    ["media", "release"],
    ["llm-run"],
    ["tg", "search"],
    ["tg", "recent"],
    ["tg", "download"],
    ["paperless", "download"],
    ["paperless", "upload"],
    ["paperless", "set"],
    ["secret", "list"],
    ["admin", "deploy"],
    ["deploy", "request"],
    ["find", "x"],
    ["people"],
]


def call(name, **args):
    return gateway.call(name, args)


def done(result):
    return {"id": JID, "op": "answer", "state": "done", "elapsed_s": 4, "result": result}


def test_ask_tg_runs_as_a_job(stubs):
    stubs.on("vault", ["--async", "ask"], JID + "\n", err="vault: job started")
    stubs.on("vault", ["job", "wait", JID], done({"kind": "when", "answer": {"date": "2026-10-24"}, "messages": 3}))
    r = call("vault.ask", source="tg", chat="Alex", question="when do we meet?", kind="when")
    assert r.ok, r.error
    assert r.data["state"] == "done" and r.data["result"]["answer"] == {"date": "2026-10-24"}
    assert "elapsed_s" not in r.data and "2026-10-24" in r.text  # concise
    sub, wait = stubs.calls("vault")
    assert sub == ["--async", "ask", "tg", "--chat", "Alex", "--kind", "when", "--json", "when do we meet?"]
    assert wait == ["job", "wait", JID, "--max", "20", "--json"]


def test_ask_gmail_and_search(stubs):
    stubs.on("vault", ["--async"], JID).on("vault", ["job", "wait"], done({"kind": "deadline", "answer": None}))
    assert call("vault.ask", source="gmail", ref="main:1234", kind="deadline").ok
    assert call("vault.ask", source="tg", search="wedding", since="30d", retrieval="hybrid").ok
    assert call("vault.ask", source="tg", kind="digest").ok  # digest needs no chat
    asks = [c for c in stubs.calls("vault") if c[0] == "--async"]
    assert asks[0] == ["--async", "ask", "gmail", "main:1234", "--kind", "deadline", "--json"]
    assert asks[1] == [
        "--async",
        "ask",
        "tg",
        "--search",
        "wedding",
        "--since",
        "30d",
        "--retrieval",
        "hybrid",
        "--json",
    ]


def test_job_running_queued_and_pickup(stubs):
    stubs.on("vault", ["--async"], JID)
    running = {"id": JID, "op": "answer", "state": "running", "position": 2, "eta_s": 35.0, "elapsed_s": 21}
    stubs.on("vault", ["job", "wait"], running, rc=12, err="still running")
    r = call("vault.ask", source="tg", chat="Alex", question="where?", wait_s=5)
    assert r.ok and r.data["state"] == "running" and r.data["job"] == JID and "vault_job" in r.text
    q = call("vault.facts", source="tg", chat="Alex", wait_s=0)
    assert q.ok and q.data == {"state": "queued", "job": JID, "status": "pending", "hint": f"vault_job {JID}"}
    assert not [c for c in stubs.calls("vault") if c[:2] == ["job", "wait"] and "0" == c[4]]  # wait 0: no wait call
    stubs.on("vault", ["job", "wait", JID], done({"kind": "who", "answer": "Sam"}))
    j = call("vault.job", id=JID, wait_s=60)
    assert j.ok and j.data["result"]["answer"] == "Sam" and stubs.calls("vault")[-1][4] == "60"
    assert "status" not in j.data and "hint" not in j.data  # done: no pending handle


@pytest.mark.parametrize(
    "job,rc,code",
    [
        ({"state": "failed", "error": {"error": "llm busy", "code": "llm_busy"}}, 1, "rate_limited"),
        ({"state": "failed", "error": {"error": "chat not found"}}, 1, "failed"),
        ({"state": "expired", "error": {"error": "dropped", "code": "result_dropped"}}, 1, "not_found"),
    ],
)
def test_job_failures(stubs, job, rc, code):
    stubs.on("vault", ["job", "wait"], dict(job, id=JID), rc=rc)
    r = call("vault.job", id=JID)
    assert not r.ok and r.code == code


def test_release_shaped_answer_is_refused(stubs):
    stubs.on("vault", ["job", "wait"], done({"text_release": True, "request_id": "r1"}))
    r = call("vault.job", id=JID)
    assert r.code == "failed" and "release" in r.error["message"]


@pytest.mark.parametrize(
    "rc,code", [(2, "invalid_args"), (3, "unavailable"), (75, "rate_limited"), (77, "unavailable"), (1, "failed")]
)
def test_exit_codes(stubs, rc, code):
    stubs.on("vault", ["plans"], "", rc=rc, err="vault: something")
    assert call("vault.plans").code == code


def test_sync_reads(stubs):
    stubs.on("vault", ["plans"], {"from": "2026-10-22", "to": "2026-11-05", "plans": [{"date": "2026-10-24"}]})
    stubs.on("vault", ["events"], {"events": [{"cursor": 5, "source": "gmail"}], "next_cursor": 6, "more": True})
    stubs.on("vault", ["ha", "now"], {"place": {"zone": "Home"}, "drive": {"active": False}, "age_min": 3, "new": 1})
    stubs.on("vault", ["status"], {"llm": "ok"}).on("vault", ["inbox"], {"needs_you": 2})
    stubs.on("vault", ["gmail", "search"], {"count": 1, "hits": [{"ref": "main:7"}]})
    p = call("vault.plans", days=7)
    assert p.ok and p.data["plans"] == [{"date": "2026-10-24"}] and p.data["start"] == "2026-10-22"
    e = call("vault.events", since="5", max=10, source="gmail")
    assert e.data["next_cursor"] == 6 and e.data["more"] is True
    h = call("vault.ha_now")
    assert h.ok and h.data["place"] == {"zone": "Home"} and "new" not in h.data and "place Home" in h.text
    assert call("vault.status").data == {"counts": {"llm": "ok"}}
    assert call("vault.inbox").data == {"counts": {"needs_you": 2}}
    assert call("vault.gmail_search", query="from:example.com newer_than:7d", n=5).data["result"]["count"] == 1
    calls = stubs.calls("vault")
    assert ["plans", "--days", "7", "--json"] in calls
    assert ["events", "--since", "5", "--max", "10", "--json", "--source", "gmail"] in calls
    assert ["gmail", "search", "from:example.com newer_than:7d", "-n", "5", "--no-text", "--json"] in calls


@pytest.mark.parametrize(
    ("drive", "age", "text"),
    [
        ({"active": True, "minutes": 12}, 2, "driving 12 min"),
        ({"active": True, "minutes": 12}, 60, "driving 12 min"),  # the rule's edge still counts
        ({"active": True, "minutes": 12}, 61, "drive unknown"),  # a stuck or stale flag never reads as a drive
        ({"active": True}, 5, "· driving (age 5 min)"),  # no minutes: no made-up 0
        ({"active": False}, 3, "not driving"),
        ({"active": False}, 90, "drive unknown"),  # too old to say either way
        (None, 3, "drive unknown"),
    ],
)
def test_ha_now_drive_uses_the_shared_age_rule(stubs, monkeypatch, drive, age, text):
    monkeypatch.delenv("DRIVING_MAX_AGE_MIN", raising=False)
    stubs.on("vault", ["ha", "now"], {"place": {"zone": "Home"}, "drive": drive, "age_min": age})
    h = call("vault.ha_now")
    assert h.ok and text in h.text
    if text != "driving 12 min":
        assert "driving 12" not in h.text and "driving 0" not in h.text


PARITY = [
    None,
    "x",
    {},
    {"drive": None},
    {"drive": "on"},
    {"drive": True},
    {"drive": False, "age_min": 61},
    {"drive": {"active": None}},
    {"drive": {"active": "yes", "minutes": 3}},
    {"drive": {"active": True, "minutes": 7.9}, "age_min": 0},
    {"drive": {"active": True, "minutes": True}},
    {"drive": {"active": True, "minutes": 5}, "age_min": True},
    {"drive": {"active": True, "minutes": 5}, "age_min": "90"},
    {"drive": {"active": True, "minutes": 5}, "age_min": 60},
    {"drive": {"active": True, "minutes": 5}, "age_min": 60.5},
    {"drive": {"active": False}, "age_min": 30},
    {"drive": {"active": False}},
]


@pytest.mark.parametrize("env", [None, "", "15", "junk", "120"])
def test_drive_verdict_matches_core_drive(monkeypatch, env):
    """vault.drive_verdict is core.drive.verdict inlined (the hub release has no core/): same answer for every case."""
    from core import drive as coredrive

    if env is None:
        monkeypatch.delenv("DRIVING_MAX_AGE_MIN", raising=False)
    else:
        monkeypatch.setenv("DRIVING_MAX_AGE_MIN", env)
    assert V.DRIVE_MAX_AGE_MIN == coredrive.MAX_AGE_MIN
    for age in (None, 10, 20, 59, 61, 100, 130):
        for a in PARITY:
            a = dict(a, age_min=age) if isinstance(a, dict) and "age_min" not in a and age is not None else a
            assert V.drive_verdict(a) == coredrive.verdict(a), a


def test_vault_tool_imports_no_core():
    """The hub release tree carries app/tools but no core/: a module-level core import breaks the gateway there."""
    import ast
    from pathlib import Path

    src = Path(V.__file__).read_text(encoding="utf-8")
    top = [n for n in ast.parse(src).body if isinstance(n, (ast.Import, ast.ImportFrom))]
    names = [n.module or "" for n in top if isinstance(n, ast.ImportFrom)] + [
        a.name for n in top if isinstance(n, ast.Import) for a in n.names
    ]
    assert not [m for m in names if m == "core" or m.startswith("core.")]


def test_summarize_and_gmail_get_force_no_text(stubs):
    stubs.on("vault", ["--async"], JID)
    stubs.on("vault", ["job", "wait"], done({"kind": "digest", "window": {"messages": 4, "chats": 2, "hours": 24}}))
    s = call("vault.tg_summarize", hours=24, unread=True)
    assert s.ok and s.text.startswith("digest: 4 message(s)")
    assert call("vault.gmail_get", ref="main:1234", deep=True).ok
    subs = [c for c in stubs.calls("vault") if c[0] == "--async"]
    assert subs[0] == ["--async", "tg", "summarize", "--no-text", "--json", "--hours", "24", "--unread"]
    assert subs[1] == ["--async", "gmail", "get", "main:1234", "--no-text", "--json", "--deep"]


def test_paperless(stubs):
    stubs.on("vault", ["paperless", "get"], {"id": 42, "title": "Invoice", "amount": 12.5, "currency": "CHF"})
    withheld = {"amount": 12.5, "currency": "CHF", "iban": None, "_withheld": ["iban"]}
    stubs.on("vault", ["paperless", "fields"], withheld)
    stubs.on("vault", ["paperless", "search"], {"count": 3, "results": [{"id": 1, "title": "A"}]})
    stubs.on("vault", ["paperless", "recent"], {"results": [{"id": 2}, {"id": 3}]})
    g = call("paperless.get", id=42)
    assert g.ok and g.data["card"]["title"] == "Invoice" and "12.5 CHF" in g.text
    f = call("paperless.fields", id=42, fields=["amount", "currency"])
    assert f.data["withheld"] == ["iban"] and "_withheld" not in f.data["fields"]
    assert call("paperless.search", query="invoice", n=3).data["count"] == 3
    assert call("paperless.recent").data["count"] == 2
    calls = stubs.calls("vault")
    assert ["paperless", "get", "42", "--json"] in calls
    assert ["paperless", "fields", "42", "--fields", "amount,currency"] in calls
    assert ["paperless", "search", "invoice", "-n", "3", "--json"] in calls
    tier2 = call("paperless.fields", id=42, fields=["iban"])
    assert tier2.code == "invalid_args"


def test_inputs_never_become_options(stubs):
    bad = [
        ("vault.ask", {"source": "tg", "chat": "Alex", "question": DD + "text"}),
        ("vault.ask", {"source": "tg", "chat": "-x", "question": "q"}),
        ("vault.ask", {"source": "tg", "search": " " + DD + "content"}),
        ("vault.ask", {"source": "gmail", "ref": "main:1 " + DD + "text"}),
        ("vault.ask", {"source": "tg", "chat": "Alex", "since": "30d " + DD + "reason"}),
        ("vault.facts", {"source": "tg", "chat": DD + "raw"}),
        ("vault.gmail_search", {"query": "-in:inbox"}),
        ("vault.tg_summarize", {"chat": DD + "text"}),
        ("vault.events", {"since": "1 " + DD + "text"}),
        ("vault.job", {"id": "../../x"}),
        ("paperless.search", {"query": DD + "content"}),
        ("vault.ask", {"source": "tg", "chat": "Alex", "reason": "x"}),  # unknown fields are refused
        ("vault.ask", {"source": "tg", "chat": "Alex", "text": True}),
        ("paperless.get", {"id": 1, "content": True}),
        ("vault.ask", {"source": "tg"}),  # tg needs chat or search
        ("vault.ask", {"source": "tg", "chat": "a", "search": "b"}),
        ("vault.ask", {"source": "gmail", "chat": "a"}),
        ("vault.facts", {"source": "gmail", "chat": "a"}),
    ]
    for name, args in bad:
        r = gateway.call(name, args)
        assert r.code == "invalid_args", (name, args, r.error)
    assert stubs.calls() == []  # nothing ran


def test_guard():
    for argv in (["ask", "tg", "--chat", "x", "--json", "q"], ["--async", "facts", "gmail", "main:1"]):
        assert V.guard(argv) == argv
    for flag in FORBIDDEN_FLAGS:
        with pytest.raises(ToolError):
            V.guard(["ask", "tg", "--chat", "x", flag])
        with pytest.raises(ToolError):
            V.guard(["paperless", "get", "1", flag])
    for cmd in FORBIDDEN_CMDS:
        with pytest.raises(ToolError):
            V.guard(cmd)
        with pytest.raises(ToolError):
            V.guard(["--async", *cmd])
    with pytest.raises(ToolError):
        V.guard(["ask", "tg", "--", "--chat"])


def test_every_vault_tool_argv_is_on_the_allowlist(stubs):
    """Run every vault and paperless tool with its examples: each argv passes the guard and carries no forbidden
    flag; the text-capable commands always say --no-text."""
    reg = load()
    stubs.on("vault", ["--async"], JID).on("vault", ["job", "wait"], done({"kind": "short", "answer": "ok"}))
    stubs.on("vault", [], "{}")
    specs = reg.in_domain("vault") + reg.in_domain("paperless")
    assert len(specs) == 15
    for s in specs:
        assert s.effect == "read" and s.approval == "none", s.name
        names = set(s.input.model_json_schema()["properties"])
        assert not names & {"reason", "text", "content", "raw", "release", "owner_ok", "download"}, s.name
        for ex in s.examples or ({},):
            r = gateway.call(s.name, dict(ex))
            assert r.ok, (s.name, ex, r.error)
    calls = stubs.calls("vault")
    assert len(calls) >= len(specs)
    for argv in calls:
        V.guard(argv)
        assert not set(argv) & set(FORBIDDEN_FLAGS), argv
        a = argv[1:] if argv[0] == "--async" else argv
        if a[:2] in (["tg", "summarize"], ["gmail", "get"], ["gmail", "search"]):
            assert "--no-text" in argv, argv


# ------------------------------------------------------------------ pending code + the job log (SLO)
def _gw(stubs, sql, args=()):
    import sqlite3

    with sqlite3.connect(stubs.dir.parent / "state" / "gateway.db") as c:
        return c.execute(sql, args).fetchall()


def test_wait_is_capped_at_the_call_budget(stubs):
    """wait_s over the budget (230 is valid input) still asks `vault job wait` for at most CALL_BUDGET_S seconds."""
    stubs.on("vault", ["--async"], JID).on("vault", ["job", "wait"], done({"answer": 1}))
    assert call("vault.ask", source="tg", chat="Alex", question="where?", wait_s=230).ok
    assert call("vault.job", id=JID, wait_s=230).ok
    waits = [int(c[4]) for c in stubs.calls("vault") if c[:2] == ["job", "wait"]]
    assert len(waits) == 2 and all(90 <= w <= V.CALL_BUDGET_S for w in waits)


def _slow_budget(monkeypatch):
    monkeypatch.setattr(V, "CALL_BUDGET_S", 2.0)
    monkeypatch.setattr(V, "KILL_GRACE_S", 1.0)


@pytest.mark.parametrize("tool,args", [
    ("vault.ask", {"source": "tg", "chat": "Alex", "question": "where?"}),
    ("vault.gmail_get", {"ref": "main:1234", "deep": True}),
    ("vault.facts", {"source": "gmail", "ref": "main:1234"}),
    ("vault.tg_summarize", {"hours": 24}),
])  # fmt: skip
def test_slow_vault_answers_pending_before_the_cap(stubs, monkeypatch, tool, args):
    """A fake vault whose `job wait` hangs past the budget: the call is cut at budget + grace and answers status
    pending with the job id and the vault_job hint, never a timeout (the job lives on in the vault)."""
    import time

    _slow_budget(monkeypatch)
    stubs.on("vault", ["--async"], JID).on("vault", ["job", "wait"], done({"answer": 1}), sleep=30)
    t = time.monotonic()
    r = call(tool, **args, wait_s=230)
    took = time.monotonic() - t
    assert r.ok, r.error
    assert {k: r.data[k] for k in ("status", "job", "hint")} == {
        "status": "pending",
        "job": JID,
        "hint": f"vault_job {JID}",
    }
    assert r.data["state"] == "running" and "vault_job" in r.text
    assert took < V.CALL_BUDGET_S + V.KILL_GRACE_S + 2  # cut, not the fake's 30 s
    assert _gw(stubs, "SELECT tool, code FROM calls") == [(tool, "pending")]
    assert _gw(stubs, "SELECT job, tool, state FROM jobs") == [(JID, tool, "running")]  # the job log keeps it


def test_slow_submit_leaves_no_wait(stubs, monkeypatch):
    """The submit alone used the budget: pending at once, no `vault job wait` call."""
    _slow_budget(monkeypatch)
    stubs.on("vault", ["--async"], JID, sleep=2.2)
    r = call("vault.ask", source="tg", chat="Alex", question="where?")
    assert r.ok and r.data["status"] == "pending" and r.data["job"] == JID
    assert not [c for c in stubs.calls("vault") if c[:2] == ["job", "wait"]]


def test_vault_job_pickup_is_cut_at_the_budget_too(stubs, monkeypatch):
    _slow_budget(monkeypatch)
    stubs.on("vault", ["job", "wait"], done({"answer": 1}), sleep=30)
    r = call("vault.job", id=JID, wait_s=230)
    assert r.ok and r.data["status"] == "pending" and r.data["hint"] == f"vault_job {JID}"
    assert _gw(stubs, "SELECT tool, code FROM calls") == [("vault.job", "pending")]


def test_vault_running_answer_carries_the_hint(stubs):
    """The vault's own exit 12 (still running at --max) gets the same pending handle."""
    stubs.on("vault", ["--async"], JID)
    stubs.on("vault", ["job", "wait"], {"id": JID, "state": "running", "elapsed_s": 20}, rc=12)
    r = call("vault.gmail_get", ref="main:1234")
    assert r.ok and r.data["status"] == "pending" and r.data["hint"] == f"vault_job {JID}"


def test_pending_code_and_job_log(stubs):
    """A wait that ends with the job still running logs code pending (not ok); the job gets a row from the call that
    started it; the vault.job pickup fills in the end; the duration is job start -> end, not the caller's wait."""
    t0 = "2026-10-29T10:00:00+01:00"
    stubs.on("vault", ["--async"], JID)
    running = {"id": JID, "op": "answer", "state": "running", "created_at": t0, "elapsed_s": 19}
    stubs.on("vault", ["job", "wait"], running, rc=12)
    r = call("vault.ask", source="tg", chat="Alex", question="where?", wait_s=20)
    assert r.ok and r.code == "ok" and r.data["state"] == "running"  # the caller's result is unchanged
    assert _gw(stubs, "SELECT tool, code FROM calls") == [("vault.ask", "pending")]
    rows = _gw(stubs, "SELECT job, tool, state, created, ended, s FROM jobs")
    assert rows == [(JID, "vault.ask", "running", V._epoch(t0), None, None)]
    fin = dict(running, state="done", ended_at="2026-10-29T10:00:47+01:00", elapsed_s=46, result={"answer": 1})
    stubs.on("vault", ["job", "wait", JID], fin, rc=0)
    assert call("vault.job", id=JID).ok
    assert _gw(stubs, "SELECT tool, code FROM calls")[-1] == ("vault.job", "ok")
    assert _gw(stubs, "SELECT tool, state, s FROM jobs") == [("vault.ask", "done", 47.0)]  # stamps: 47 s
    st = gateway.stats(1)["tools"]["vault.ask"]
    assert st["pending"] == 1 and st["errors"] == {}  # pending is no error
    j = gateway.jobs(1)["tools"]["vault.ask"]
    assert (j["jobs"], j["done_n"], j["p95_s"], j["states"]) == (1, 1, 47.0, {"done": 1})


def test_job_log_done_queued_and_unknown_pickup(stubs):
    q, other = "1" * 16, "fedcba9876543210"
    stubs.on("vault", ["--async", "ask"], JID).on("vault", ["--async", "facts"], q)
    stubs.on("vault", ["job", "wait", JID], dict(done({"answer": "x"}), elapsed_s=6))  # no stamps: elapsed_s
    assert call("vault.ask", source="tg", chat="Alex", question="who?").ok
    assert call("vault.facts", source="tg", chat="Alex", wait_s=0).ok  # queued: started, never waited
    stubs.on("vault", ["job", "wait", other], dict(done({"answer": "y"}), id=other))
    assert call("vault.job", id=other).ok  # a job no tool call started (Bash `vault --async`): no row
    assert _gw(stubs, "SELECT tool, code FROM calls") == [
        ("vault.ask", "ok"),
        ("vault.facts", "pending"),
        ("vault.job", "ok"),
    ]
    assert sorted(_gw(stubs, "SELECT job, tool, state, s FROM jobs")) == [
        (JID, "vault.ask", "done", 6.0),
        (q, "vault.facts", "queued", None),
    ]


def test_sweep_closes_open_jobs_from_the_job_list(stubs):
    a, b, c = "a" * 16, "b" * 16, "c" * 16
    for jid in (a, b, c):
        stubs.on("vault", ["--async"], jid)
        assert call("vault.facts", source="tg", chat="Alex", wait_s=0).ok
        stubs.rules["vault"] = [r for r in stubs.rules["vault"] if r["argv"] != ["--async"]]
    assert gateway.open_jobs() == 3
    _gw(stubs, "UPDATE jobs SET ts = ts - 7200 WHERE job = ?", (c,))  # c: 2 h old and gone from the list: lost
    stubs.on("vault", ["job", "list"], [
        {"id": a, "op": "answer", "state": "done", "created_at": "2026-10-29T10:00:00+01:00",
         "ended_at": "2026-10-29T10:01:05+01:00", "elapsed_s": 64, "bytes": 70},
        {"id": b, "op": "answer", "state": "running", "created_at": "2026-10-29T10:00:00+01:00", "elapsed_s": 9},
        {"id": "d" * 16, "op": "llm.run", "state": "done"},
    ])  # fmt: skip
    assert gateway.sweep_jobs() == {"open": 3, "closed": 1, "lost": 1, "still_open": 1}
    assert dict((r[0], r[1:]) for r in _gw(stubs, "SELECT job, state, s FROM jobs")) == {
        a: ("done", 65.0),
        b: ("running", None),
        c: ("lost", None),
    }
    assert ["job", "list", "--json"] in stubs.calls("vault")
    n = len(stubs.calls("vault"))
    _gw(stubs, "UPDATE jobs SET state = 'done' WHERE job = ?", (b,))
    assert gateway.sweep_jobs() == {"open": 0, "closed": 0, "lost": 0, "still_open": 0}
    assert len(stubs.calls("vault")) == n  # nothing open: no vault call
    assert V.guard(["job", "list", "--json"]) == ["job", "list", "--json"]
