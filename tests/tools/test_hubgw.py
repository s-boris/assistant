"""End to end on loopback: the hub gateway (FastMCP over streamable HTTP + Cedar, tools.hubgw.start as hubd runs
it) and the brain-side executor (tools.worker.Worker, real long poll) with a synthetic registry, driven by the
FastMCP client like Claude Code's HTTP MCP entry: tools/list, calls, the stamp, log-only vs enforce, auth, the
executor going offline, the admin API (report, gate, mode switch, ack) and the hub's release tree."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest
from tools import hubgw, worker
from tools.core import Domain, Registry, ToolInput, ToolOutput, tool
from tools.policy import stamp_mac

REPO = Path(__file__).resolve().parents[2]
TOKENS = {"brain": b"B" * 40, "tg": b"T" * 40, "voice": b"V" * 40}
RAN: list[str] = []


class In(ToolInput):
    x: int = 0


class Out(ToolOutput):
    n: int = 0

    def render(self, fmt: Any = "concise") -> str:
        return f"n={self.n}"


def reg() -> Registry:
    r = Registry()
    r.add_domain(Domain("e", "E2E", "synthetic", "test"))

    def look(a: In) -> Out:
        RAN.append("look")
        return Out(n=a.x * 2)

    def ping(a: In) -> Out:
        RAN.append("ping")
        return Out(n=1)

    def secret(a: In) -> Out:
        RAN.append("secret")
        return Out(n=7)

    def mail(a: In) -> Out:
        RAN.append("mail")
        return Out(n=3)

    tool("e.look", effect="read", registry=r)(look)
    tool("e.ping", effect="send_owner", registry=r)(ping)
    tool("e.secret", effect="read", labels_out=("private",), registry=r)(secret)
    tool("e.mail", effect="send_external", registry=r)(mail)
    return r


def stamp(tool_name: str, p: str = "brain", agent: str = "", session: str = "S") -> dict[str, Any]:
    ts = int(time.time())
    return {
        "p": p,
        "a": agent,
        "t": "",
        "s": session,
        "ts": ts,
        "m": stamp_mac(TOKENS["brain"], p, agent, "", session, ts, tool_name),
    }


@pytest.fixture
def hub(tmp_path, monkeypatch):
    """(loop runner, url, gateway): the gateway in a background event loop + one executor thread."""
    monkeypatch.setenv("ASSISTANT_OTLP", "off")
    monkeypatch.setenv("TOOLS_STATE", str(tmp_path / "brain-state"))
    tok = tmp_path / "hub-token"
    tok.write_bytes(TOKENS["brain"])
    monkeypatch.setenv("HUB_TOKEN_FILE", str(tok))
    r = reg()
    loop = asyncio.new_event_loop()
    th = threading.Thread(target=loop.run_forever, daemon=True)
    th.start()
    run = asyncio.run_coroutine_threadsafe(
        hubgw.start(TOKENS, port=0, bind="127.0.0.1", state=tmp_path / "gw", registry=r), loop
    ).result(30)
    url = f"http://127.0.0.1:{run.port}"
    monkeypatch.setenv("HUB_GW_URL", url)
    w = worker.Worker(threads=2, registry=r, claim_wait=1.0)
    wt = threading.Thread(target=w.run, daemon=True)
    wt.start()
    for _ in range(100):
        if run.gw.exec.online():
            break
        time.sleep(0.05)
    RAN.clear()
    yield {"loop": loop, "url": url, "run": run, "worker": w, "tmp": tmp_path}
    w.stop.set()
    wt.join(15)
    asyncio.run_coroutine_threadsafe(run.stop(), loop).result(20)
    loop.call_soon_threadsafe(loop.stop)


def mcp(url: str, token: bytes, calls: list[tuple[Any, ...]], headers: dict[str, str] | None = None):
    """(tool names, [CallToolResult]) through the FastMCP client over streamable HTTP; a call is (name, args) or
    (name, args, the request's _meta)."""
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    async def go():
        h = {"Authorization": f"Bearer {token.decode()}", **(headers or {})}
        async with Client(StreamableHttpTransport(url + "/mcp", headers=h)) as c:
            names = [t.name for t in await c.list_tools()]
            out = [await c.call_tool(n, a, raise_on_error=False, meta=(m or [None])[0]) for n, a, *m in calls]
        return names, out

    return asyncio.run(go())


def text(r: Any) -> str:
    return r.content[0].text if r.content else ""


def test_list_and_call_through_hub_and_executor(hub):
    names, (r1, r2) = mcp(
        hub["url"],
        TOKENS["brain"],
        [
            ("e_look", {"x": 21, "_caller": stamp("e_look")}),
            ("e_look", {"x": "nope", "_caller": stamp("e_look")}),
        ],
    )
    assert sorted(names) == ["e_look", "e_mail", "e_ping", "e_secret"]
    assert not r1.is_error and text(r1) == "n=42" and r1.structured_content == {"n": 42}
    assert r2.is_error and text(r2).startswith("invalid_args:")  # validated in the hub: never reached the executor
    assert RAN == ["look"]
    rep = hub["run"].gw.store.report()
    assert rep["domains"]["e"]["calls"] == 2 and rep["principals"] == {"brain": 2}
    assert rep["overhead_ms"]["n"] == 1


def test_log_only_runs_and_logs_the_would_deny(hub):
    _, (r,) = mcp(hub["url"], TOKENS["brain"], [("e_ping", {"x": 1})])  # no stamp: unattributed subagent
    assert not r.is_error and RAN == ["ping"]
    row = hub["run"].gw.store.db.execute("SELECT principal, decision, mode, enforced FROM calls").fetchone()
    assert row == ("subagent:unattributed", "deny", "log", 0)


def test_enforce_denies_with_a_typed_reason_and_never_runs(hub):
    sub = stamp("e_ping", "subagent", agent="a1")
    _, (r,) = mcp(
        hub["url"],
        TOKENS["brain"],
        [("e_ping", {"x": 1, "_caller": sub})],
        headers={"X-Hub-Test": "1", "X-Hub-Test-Mode": "enforce"},
    )
    assert r.is_error and text(r).startswith("policy_denied: subagent:agent → e.ping: no policy lets a subagent")
    assert "(hint: " in text(r) and RAN == []


def test_label_flow_over_http(hub):
    h = {"X-Hub-Test": "1", "X-Hub-Test-Mode": "enforce"}
    _, (a, b, c) = mcp(
        hub["url"],
        TOKENS["brain"],
        [
            ("e_mail", {"_caller": stamp("e_mail")}),
            ("e_secret", {"_caller": stamp("e_secret")}),
            ("e_mail", {"_caller": stamp("e_mail")}),
        ],
        headers=h,
    )
    assert not a.is_error and not b.is_error
    assert c.is_error and "private data was read" in text(c) and RAN == ["mail", "secret"]


def test_tg_token_is_a_binding(hub):
    _, (r,) = mcp(
        hub["url"],
        TOKENS["tg"],
        [("e_ping", {"_caller": stamp("e_ping")})],
        headers={"X-Hub-Test": "1", "X-Hub-Test-Mode": "enforce"},
    )
    assert not r.is_error
    row = hub["run"].gw.store.db.execute("SELECT caller, principal FROM calls").fetchone()
    assert row == ("tg", "binding")


def test_voice_token_is_always_enforced(hub):
    """The relay's token is principal voice. Its denies hold while the domain is log-only (every domain's start
    state on the live hub), and a test call's mode header can't relax them; a read still runs."""
    _, (look, ping) = mcp(hub["url"], TOKENS["voice"], [("e_look", {"x": 1}), ("e_ping", {"x": 1})])
    relax = {"X-Hub-Test": "1", "X-Hub-Test-Mode": "log"}
    _, (ping_log,) = mcp(hub["url"], TOKENS["voice"], [("e_ping", {"x": 1})], headers=relax)
    assert not look.is_error and RAN == ["look"]
    assert ping.is_error and text(ping).startswith("policy_denied: voice") and "(hint: " in text(ping)
    assert ping_log.is_error and "policy_denied" in text(ping_log)
    q = "SELECT principal, decision, mode, enforced FROM calls ORDER BY id"
    rows = hub["run"].gw.store.db.execute(q).fetchall()
    assert rows[:2] == [("voice", "allow", "enforce", 0), ("voice", "deny", "enforce", 1)]
    assert hub["run"].gw.store.mode("e") == "log"  # the domain itself stays log-only for everyone else
    with httpx.Client(timeout=10) as c:
        assert c.get(hub["url"] + "/gw/health").json()["mode"]["principals_enforced"] == [
            "phone",
            "voice",
        ]
    _, (r,) = mcp(hub["url"], TOKENS["brain"], [("e_ping", {"x": 1})])  # unattributed subagent: log-only, runs
    assert not r.is_error and RAN == ["look", "ping"]


def test_auth_and_routes(hub):
    u = hub["url"]
    with httpx.Client(timeout=10) as c:
        assert c.post(u + "/mcp", json={}).status_code == 401
        assert c.post(u + "/mcp", json={}, headers={"Authorization": "Bearer nope" + "x" * 40}).status_code == 401
        h = c.get(u + "/gw/health").json()
        assert h["ok"] and h["tools"] == 4 and h["executor"]["online"] and h["mode"]["default"] == "log"
        assert h["adapters"]["voice"]["online"] is False  # the voice queue is listed (voice-hub's check)
        tg = {"Authorization": "Bearer " + TOKENS["tg"].decode()}
        assert c.post(u + "/gw/claim", json={"wait": 0}, headers=tg).status_code == 403
        assert c.get(u + "/gw/policy", headers=tg).status_code == 403
        br = {"Authorization": "Bearer " + TOKENS["brain"].decode()}
        g = c.post(u + "/gw/policy/mode", json={"domain": "e", "mode": "enforce"}, headers=br)
        assert g.status_code == 409 and "not ready: e" in g.json()["hint"]
        assert c.post(u + "/gw/policy/mode", json={"domain": "nope", "mode": "log"}, headers=br).status_code == 400
        ok = c.post(
            u + "/gw/policy/mode", json={"domain": "e", "mode": "enforce", "force": True, "why": "t"}, headers=br
        )
        assert ok.status_code == 200 and ok.json()["modes"]["domains"] == {"e": "enforce"}
        chk = c.post(u + "/gw/policy/check", json={"tool": "e.ping", "principal": "subagent"}, headers=br).json()
        assert chk["allowed"] is False and chk["mode"] == "enforce"
    _, (r,) = mcp(u, TOKENS["brain"], [("e_ping", {"x": 1})])  # enforced now, no test header
    assert r.is_error and "policy_denied" in text(r)
    with httpx.Client(timeout=10) as c:  # the rollback: log-only again, no restart
        assert (
            c.post(
                u + "/gw/policy/mode", json={"domain": "all", "mode": "log", "why": "rollback"}, headers=br
            ).status_code
            == 200
        )
        rep = c.get(u + "/gw/policy", headers=br).json()
        ids = [d["id"] for d in rep["denies"]]
        assert rep["domains"]["e"]["unexpected"] == 1 and rep["domains"]["e"]["enforced"] == 1
        assert c.post(u + "/gw/policy/ack", json={"ids": ids, "why": "test"}, headers=br).json()["acked"] == 1
    _, (r,) = mcp(u, TOKENS["brain"], [("e_ping", {"x": 1})])
    assert not r.is_error


def test_keep_alive_responses_come_back_at_once(hub):
    """Nagle is off on accepted connections: a keep-alive response (headers and body in two writes) must not wait for
    the peer's delayed ACK (~40 ms on Linux), on every claim, result and MCP answer."""
    import http.client
    import statistics

    c = http.client.HTTPConnection("127.0.0.1", hub["run"].port, timeout=10)
    lat = []
    for _ in range(12):
        t0 = time.perf_counter()
        c.request("GET", "/gw/health")
        c.getresponse().read()
        lat.append(time.perf_counter() - t0)
    c.close()
    assert statistics.median(lat) < 0.02, [round(x * 1000, 1) for x in lat]


def test_worker_hands_over_to_new_code(hub, monkeypatch, tmp_path):
    """A deploy changes the code under a running worker: it stops claiming, drains and starts its replacement."""
    out, fake = tmp_path / "launched", tmp_path / "launcher"
    fake.write_text(f'#!/bin/sh\necho "$@" > "{out}"\n', encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("TOOLS_LAUNCHER", str(fake))
    monkeypatch.setattr(worker, "TICK", 0.05)
    stamps = iter([("old", 1, 1), ("old", 1, 1)])
    monkeypatch.setattr(worker, "_code_stamp", lambda: next(stamps, ("new", 2, 1)))
    w = worker.Worker(threads=1, registry=reg(), claim_wait=0.3)
    t0 = time.monotonic()
    assert w.run() == 0 and w.restart and time.monotonic() - t0 < 5
    assert '"code_changed"' in worker._paths()["log"].read_text(encoding="utf-8")
    assert worker.respawn(3) and out.read_text(encoding="utf-8").split() == ["worker", "ensure", "--threads", "3"]
    monkeypatch.setenv("TOOLS_LAUNCHER", str(tmp_path / "missing"))
    assert not worker.respawn(3)  # cron's next ensure is the fallback


def test_executor_calls_never_join_an_inherited_trace(hub, monkeypatch):
    """A worker started from a Bash call holds that call's TRACEPARENT: each call it serves still gets its own trace
    (the request's _meta.traceparent, else a new one), never the inherited one."""
    stale, mine = "00-" + "ab" * 16 + "-" + "cd" * 8 + "-01", "00-" + "12" * 16 + "-" + "34" * 8 + "-01"
    monkeypatch.setenv("TRACEPARENT", stale)
    calls: list[tuple[Any, ...]] = [("e_look", {"x": i, "_caller": stamp("e_look")}) for i in (1, 2)]
    calls.append(("e_look", {"x": 3, "_caller": stamp("e_look")}, {"traceparent": mine}))
    _, rs = mcp(hub["url"], TOKENS["brain"], calls)
    assert [text(r) for r in rs] == ["n=2", "n=4", "n=6"]
    with sqlite3.connect(hub["tmp"] / "brain-state" / "gateway.db") as c:
        rows = c.execute("SELECT trace, trace_src, surface FROM calls ORDER BY ts").fetchall()
    assert [r[1:] for r in rows] == [("new", "hub"), ("new", "hub"), ("meta", "hub")]
    assert len({rows[0][0], rows[1][0], "ab" * 16}) == 3 and rows[2][0] == "12" * 16


def test_worker_run_drops_an_inherited_traceparent(monkeypatch, tmp_path):
    """`tool worker run` (and ensure's child) clear the trace env first: a respawn through the launcher and the CLIs
    its tools start inherit os.environ, so neither carries the starting Bash call's trace on."""
    monkeypatch.setenv("TOOLS_STATE", str(tmp_path))
    monkeypatch.setenv("TRACEPARENT", "00-" + "ab" * 16 + "-" + "cd" * 8 + "-01")
    monkeypatch.setattr(worker, "_locked", lambda p: None)  # another worker holds the lock: run returns at once
    assert worker.cmd_run(1) == 3 and "TRACEPARENT" not in os.environ


def test_worker_drains_a_long_call_after_the_hand_over(hub, monkeypatch):
    """A deploy lands while a long call (a hub job) runs: the old worker stops claiming and returns, its replacement
    starts, and the call in hand still finishes and posts its result (drain) instead of dying with the process."""
    hub["worker"].stop.set()  # only the worker under test claims
    for t in hub["worker"].ts:
        t.join(10)
    monkeypatch.setattr(worker, "TICK", 0.05)
    monkeypatch.setattr(worker, "JOIN_WAIT", 0.1)
    w = worker.Worker(threads=1, registry=reg(), claim_wait=0.3)
    plain = w.execute
    started = threading.Event()

    def slow(job: dict[str, Any]) -> dict[str, Any]:
        started.set()
        time.sleep(1.5)
        return plain(job)

    monkeypatch.setattr(w, "execute", slow)
    stamps = iter([("old", 1, 1)])
    monkeypatch.setattr(worker, "_code_stamp", lambda: next(stamps, ("old", 1, 1)))
    wt = threading.Thread(target=w.run, daemon=True)
    wt.start()
    got: list[Any] = []
    ct = threading.Thread(target=lambda: got.append(mcp(hub["url"], TOKENS["brain"], [("e_look", {"x": 4})])),
                          daemon=True)  # fmt: skip
    ct.start()
    assert started.wait(20)
    monkeypatch.setattr(worker, "_code_stamp", lambda: ("new", 2, 1))  # the deploy
    wt.join(5)
    assert not wt.is_alive() and w.restart and w.ts[0].is_alive()  # run() returned with the call still in hand
    assert w.drain(10) == 0
    ct.join(20)
    (_, (r,)) = got[0]
    assert not r.is_error and "n=8" in text(r)
    log = worker._paths()["log"].read_text(encoding="utf-8")
    assert '"draining"' in log and '"drained"' in log


def test_worker_cli_defaults_to_threads(monkeypatch):
    """`tool worker ensure` (keepers, cron) starts THREADS claim threads: a hub job slot each + spares for his calls."""
    from tools import cli

    got: list[int] = []
    monkeypatch.setattr(worker, "main", lambda action, threads, as_json: got.append(threads) or 0)
    assert cli.main(["worker", "ensure"]) == 0 and cli.main(["worker", "ensure", "--threads", "3"]) == 0
    assert got == [worker.THREADS, 3] and worker.THREADS == 12


def test_code_stamp_sees_deploys_not_bytecode(monkeypatch, tmp_path):
    from tools import gateway

    pkg = tmp_path / "tools"
    (pkg / "domains").mkdir(parents=True)
    (pkg / "__pycache__").mkdir()
    for f in ("gateway.py", "policy.cedar", "requirements.lock", "domains/ha.py"):
        (pkg / f).write_text("x", encoding="utf-8")
        os.utime(pkg / f, ns=(10**18, 10**18))
    monkeypatch.setattr(gateway, "__file__", str(pkg / "gateway.py"))
    a = worker._code_stamp()
    (pkg / "__pycache__" / "gateway.cpython-313.pyc").write_bytes(b"x")
    assert worker._code_stamp() == a and a[2] == 4
    os.utime(pkg / "domains" / "ha.py", ns=(10**18 + 1, 10**18 + 1))
    assert worker._code_stamp() != a


def test_executor_offline_answers_unavailable_at_once(hub):
    hub["worker"].stop.set()
    gw = hub["run"].gw
    for _ in range(100):
        if gw.exec.claimers == 0:
            break
        time.sleep(0.05)
    gw.exec.last_poll = -1e9
    t0 = time.monotonic()
    _, (r,) = mcp(hub["url"], TOKENS["brain"], [("e_look", {"x": 1, "_caller": stamp("e_look")})])
    assert r.is_error and text(r).startswith("unavailable:") and time.monotonic() - t0 < 4


def test_cli_policy_report_against_the_gateway(hub):
    mcp(hub["url"], TOKENS["brain"], [("e_look", {"x": 1, "_caller": stamp("e_look")})])
    deps = [os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else []  # the hub runner's toolchain site
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(REPO), str(REPO / "packages"), *deps]))
    r = subprocess.run(
        [sys.executable, "-m", "tools.cli", "policy", "--json"], capture_output=True, text=True, env=env, timeout=60
    )
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["domains"]["e"]["calls"] == 1  # the hub's report, relayed
    r = subprocess.run(
        [sys.executable, "-m", "tools.cli", "worker", "status", "--json"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert json.loads(r.stdout)["hub"]["online"] is True


def test_release_tree_carries_the_gateway_and_imports_alone(tmp_path):
    from hubkit import release

    src = tmp_path / "src"
    for rel in ("packages/hub", "packages/tools", "schemas/pg"):
        if (REPO / rel).exists():
            __import__("shutil").copytree(
                REPO / rel, src / rel, ignore=__import__("shutil").ignore_patterns("__pycache__")
            )
    (src / "ops/hub").mkdir(parents=True)
    d, _ = release.build(src, "abc1234", "abc1234", root=tmp_path / "rel")
    names = {p.relative_to(d / "app").as_posix() for p in (d / "app").rglob("*") if p.is_file()}
    assert {"tools/hubgw.py", "tools/policy.cedar", "tools/policy.py", "tools/domains/reminders.py"} <= names
    assert not {"tools/cli.py", "tools/worker.py", "tools/gen.py"} & names
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); sys.path.append(sys.argv[3]); from tools import hubgw; "
        "g = hubgw.Gateway({'brain': b'x'*40},"
        " __import__('pathlib').Path(sys.argv[2])); print(len(g.reg.typed), g.invalid)"
    )
    import pydantic  # its site dir, not the repo: under -I the hub runner's toolchain site is off sys.path

    deps = str(Path(pydantic.__file__).resolve().parents[1])
    r = subprocess.run(
        [sys.executable, "-I", "-c", code, str(d / "app"), str(tmp_path / "gws"), deps],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=tmp_path,
    )
    assert r.returncode == 0, r.stderr[-2000:]
    n, errs = r.stdout.split(" ", 1)
    assert int(n) > 50 and errs.strip() == "[]"


def test_headers_helper(tmp_path):
    tok = tmp_path / "tok"
    tok.write_bytes(TOKENS["brain"] + b"\n")
    exe = [sys.executable, "-I", str(REPO / "scripts" / "hub-mcp-headers")]
    r = subprocess.run(exe, capture_output=True, text=True, env={"HUB_TOKEN_FILE": str(tok)}, timeout=20)
    assert r.returncode == 0 and json.loads(r.stdout) == {"Authorization": "Bearer " + TOKENS["brain"].decode()}
    r = subprocess.run(exe, capture_output=True, text=True, env={"HUB_TOKEN_FILE": str(tmp_path / "no")}, timeout=20)
    assert r.returncode == 1 and r.stdout == ""
