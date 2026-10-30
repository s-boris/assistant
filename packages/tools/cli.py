"""tool: the typed tool registry from the shell (same gateway as the MCP server `hub`).

  tool ls [DOMAIN] [--json] [--bindings]     typed tools: name, effect, approval, rate, summary (--bindings: bindings)
  tool show NAME [--json]                    input/output schema, effect, approval, rate, labels, examples
  tool call NAME [JSON | - | key=value ..] [--json] [--detailed] [--approval NONCE]
                                             run it through the gateway; prints the text answer (--json: the
                                             whole result {ok, tool, data|error, text, trace_id, ms})
  tool docs [DOMAIN] [--tools-md FILE [--write]]   generated docs; --tools-md: check (exit 1 if stale) or --write
                                             the domain's block in FILE (between its tool-docs markers)
  tool gen [--check] [--repo DIR]            write (or check) schemas/tools.toml, schemas/tools.typed.json and
                                             docs/tools/*.md in the repo checkout
  tool stats [--days N] [--json]             gateway calls per tool: count, errors by code, p50/p95 ms, trace source
  tool jobs [sweep] [--days N] [--json]      the gateway's job log (async vault jobs behind vault tool answers): per
                                             starting tool rows by state and finished-job p50/p95 s; sweep closes
                                             the open rows from `vault job list` (`slo --cache` runs it)
  tool direct [DOMAIN on|off] [--json]       the direct-mode switch per domain (or `all`): on = its typed tools refuse
                                             (unavailable, use the CLI) and the MCP server leaves them out at its
                                             next start; off = back through the gateway. No args: every domain's mode
  tool serve [--check]                       the MCP server `hub` on stdio (the MCP entry runs this); --check
                                             only builds it and prints the tool count
  tool worker run|ensure|status|stop [--threads N] [--json]
                                             the brain-side executor of the hub gateway: long-polls the hub,
                                             runs its calls through this gateway (tools.worker); cron runs ensure
  tool policy [--days N] [--json]            the hub gateway's Cedar policy per domain: calls, denies, unexpected
                                             denies, ready (≥ 100 calls, 0 unexpected), mode, recent denies
  tool policy mode DOMAIN|all log|enforce [--force] [--why TEXT]
                                             the log-only switch (no restart); enforce refuses a domain that is not
                                             ready unless --force
  tool policy ack ID.. --why TEXT            mark denies as expected (they leave the "unexpected" count)
  tool policy check NAME --as PRINCIPAL [--approved] [--tainted LABEL]..   one decision, locally (dry run)
  tool policy validate | schema              Cedar validation of policy.cedar against the registry's schema; the schema

NAME = registry name (reminders.add) or MCP name (reminders_add). key=value values are JSON when they parse
(true, 3, "x"), else strings. Exit: 0 ok · 1 internal · 2 usage, invalid_args or unknown tool · 3 tool error
(not_found, failed, timeout, unavailable, conflict) · 4 approval_required · 5 rate_limited · 6 policy_denied.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from tools import gateway, gen
from tools.core import Registry, ToolError, load


def _out(obj: Any) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=1) + "\n")


def _args(rest: list[str]) -> dict[str, Any]:
    if not rest:
        return {}
    if len(rest) == 1 and (rest[0] == "-" or rest[0].lstrip().startswith("{")):
        raw = sys.stdin.read() if rest[0] == "-" else rest[0]
        d = json.loads(raw)
        if not isinstance(d, dict):
            raise ValueError("arguments must be a JSON object")
        return d
    d = {}
    for kv in rest:
        k, sep, v = kv.partition("=")
        if not sep or not k:
            raise ValueError(f"want key=value, got {kv!r}")
        try:
            d[k] = json.loads(v)
        except ValueError:
            d[k] = v
    return d


def _repo() -> Path:
    here = Path(__file__).resolve().parent  # packages/tools in a checkout
    return here.parent.parent


def cmd_ls(reg: Registry, a: argparse.Namespace) -> int:
    if a.bindings:
        rows = [
            {"name": t.name, "effect": t.effect, "argv": list(t.argv), "description": t.description}
            for t in reg.argv.values()
        ]
        if a.json:
            _out(rows)
        else:
            for r in rows:
                print(f"{r['name']:28} {r['effect']:10} {r['description']}")
        return 0
    specs = [s for s in reg.typed.values() if not a.domain or s.domain == a.domain]
    if a.domain and a.domain not in reg.domains:
        print(f"tool: no domain {a.domain!r} ({', '.join(reg.domains)})", file=sys.stderr)
        return 2
    if a.json:
        _out(
            [
                {
                    "name": s.name,
                    "mcp_name": s.mcp_name,
                    "effect": s.effect,
                    "approval": s.approval,
                    "rate": s.rate,
                    "summary": s.summary,
                }
                for s in specs
            ]
        )
    else:
        for s in specs:
            print(f"{s.name:20} {s.effect:6} {s.rate or '-':8} {s.summary}")
    return 0


def cmd_show(reg: Registry, a: argparse.Namespace) -> int:
    s = reg.get(a.name)
    if s is None:
        print(f"tool: no tool {a.name!r} (`tool ls`)", file=sys.stderr)
        return 2
    d = gen.spec_schema(s)
    d.update(name=s.name, doc=s.doc, examples=list(s.examples))
    if a.json:
        _out(d)
    else:
        print(f"{s.name} (MCP {s.mcp_name}) · effect {s.effect} · approval {s.approval} · rate {s.rate or '-'}")
        print(s.doc)
        print("Input:\n" + "\n".join(gen.fields_md(d["input"]) or ["- (none)"]))
        print("Output: " + ", ".join(d["output"].get("properties", {})))
        for ex in s.examples:
            print(f"Example: tool call {s.name} '{json.dumps(ex, ensure_ascii=False)}'")
    return 0


def cmd_call(reg: Registry, a: argparse.Namespace) -> int:
    try:
        args = _args(a.args)
    except ValueError as e:
        print(f"tool call: {e}", file=sys.stderr)
        return 2
    if a.detailed:
        args.setdefault("format", "detailed")
    res = gateway.call(a.name, args, surface="cli", approval=a.approval, registry=reg)
    if a.json:
        _out(res.as_json())
    elif res.ok:
        print(res.text)
    else:
        print(res.error_text(), file=sys.stderr)
    return res.exit_code


def cmd_docs(reg: Registry, a: argparse.Namespace) -> int:
    doms = [a.domain] if a.domain else [d for d in reg.domains if reg.in_domain(d)]
    if any(d not in reg.domains or not reg.in_domain(d) for d in doms):
        print(f"tool docs: no typed tools in {a.domain!r}", file=sys.stderr)
        return 2
    if a.tools_md:
        p = Path(a.tools_md)
        text = p.read_text()
        new = text
        for d in doms:
            try:
                new = gen.patch_tools_md(new, reg, d)
            except ValueError as e:
                print(f"tool docs: {e}", file=sys.stderr)
                return 2
        if new == text:
            print("up to date")
            return 0
        if not a.write:
            print(f"stale: {p} (--write updates the block)")
            return 1
        tmp = p.with_name(p.name + ".tool-docs.tmp")
        tmp.write_text(new)
        tmp.replace(p)
        print(f"updated {p}")
        return 0
    for d in doms:
        print(gen.domain_md(reg, d))
    return 0


def cmd_gen(reg: Registry, a: argparse.Namespace) -> int:
    repo = Path(a.repo) if a.repo else _repo()
    if not (repo / "schemas").is_dir():
        print(f"tool gen: {repo} is not the repo checkout (--repo DIR)", file=sys.stderr)
        return 2
    if a.check:
        bad = gen.stale(repo, reg)
        print("stale: " + ", ".join(bad) if bad else "generated files up to date")
        return 1 if bad else 0
    for rel in gen.write(repo, reg) or ["(nothing changed)"]:
        print(rel)
    return 0


def cmd_stats(reg: Registry, a: argparse.Namespace) -> int:
    s = gateway.stats(a.days)
    if a.json:
        _out(s)
        return 0
    print(
        f"gateway calls, last {a.days:g} d · trace ids: "
        + (", ".join(f"{k} {v}" for k, v in s["trace_src"].items()) or "-")
    )
    for name, t in sorted(s["tools"].items()):
        errs = ", ".join(f"{k} {v}" for k, v in t["errors"].items()) or "no errors"
        print(f"{name:20} {t['calls']:5} calls · p50 {t['p50_ms']} ms · p95 {t['p95_ms']} ms · {errs}")
    return 0


def cmd_jobs(reg: Registry, a: argparse.Namespace) -> int:
    if a.action == "sweep":
        try:
            s = gateway.sweep_jobs()
        except ToolError as e:
            print(f"tool jobs sweep: {e.code}: {e.message}", file=sys.stderr)
            return 3
        except Exception as e:  # noqa: BLE001  (the type only: a sweep is bookkeeping)
            print(f"tool jobs sweep: {type(e).__name__}", file=sys.stderr)
            return 1
        if a.json:
            _out(s)
        else:
            print(", ".join(f"{k} {v}" for k, v in s.items()))
        return 0
    j = gateway.jobs(a.days)
    if a.json:
        _out(j)
        return 0
    print(f"job log, last {a.days:g} d (job start -> finished; timings only)")
    for name, t in sorted(j["tools"].items()):
        states = ", ".join(f"{k} {v}" for k, v in sorted(t["states"].items()))
        p = "-" if t["p95_s"] is None else f"p50 {t['p50_s']:g} s · p95 {t['p95_s']:g} s · max {t['max_s']:g} s"
        print(f"{name:20} {t['jobs']:5} jobs · {states} · finished {t['done_n']}: {p}")
    return 0


def cmd_direct(reg: Registry, a: argparse.Namespace) -> int:
    doms = [d for d in reg.domains if reg.in_domain(d)]
    if a.domain:
        if a.domain not in doms and a.domain != "all":
            print(f"tool direct: no typed domain {a.domain!r} ({', '.join(doms)}, all)", file=sys.stderr)
            return 2
        if a.state not in ("on", "off"):
            print("tool direct: DOMAIN on|off", file=sys.stderr)
            return 2
        p = gateway.set_direct(a.domain, a.state == "on")
        print(f"{a.domain}: {'direct (CLI)' if a.state == 'on' else 'gateway'} · {p}")
        return 0
    switched = gateway.direct_domains()
    rows = {d: ("direct" if gateway.is_direct(d, switched) else "gateway") for d in doms}
    if a.json:
        _out(rows)
    else:
        for d, m in rows.items():
            print(f"{d:12} {m}")
    return 0


def _gw(method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    """The hub gateway's admin API with the brain's token: (status, JSON)."""
    import urllib.error

    from core import http as corehttp
    from tools import worker

    try:
        with corehttp.open(
            worker.gw_url() + path,
            json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {worker.token()}", "Content-Type": "application/json"},
            method=method,
            timeout=10,
        ) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except (OSError, ValueError) as e:
        return 0, {"error": "unreachable", "hint": type(getattr(e, "reason", e)).__name__}


def cmd_policy(reg: Registry, a: argparse.Namespace) -> int:
    from tools import policy

    act = a.args[0] if a.args else "report"
    rest = a.args[1:]
    if act in ("validate", "schema"):
        eng = policy.Engine(reg)
        if act == "schema":
            print(eng.schema, end="")
            return 0
        errs = eng.validate()
        print(f"policy {eng.sha}: {len(reg.typed)} actions, {len(eng.reasons)} policies: "
              + ("valid" if not errs else f"{len(errs)} errors"))  # fmt: skip
        for e in errs:
            print(f"  {e}")
        return 1 if errs else 0
    if act == "check":
        spec = reg.get(rest[0]) if rest else None
        if spec is None or a.as_ not in policy.PRINCIPALS:
            print("tool policy check NAME --as brain|subagent|binding|reflex|ci", file=sys.stderr)
            return 2
        d = policy.Engine(reg).decide(
            policy.Principal(a.as_, agent_id="check"), spec, approved=a.approved, tainted=set(a.tainted or [])
        )
        out = dict(d.as_dict(), tool=spec.name, effect=spec.effect, principal=a.as_)
        if a.json:
            print(json.dumps(out, indent=1))
        else:
            print(f"{a.as_} → {spec.name} ({spec.effect}): " + ("allow" if d.allowed else f"deny: {d.reason}")
                  + (f" [{', '.join(d.policies)}]" if d.policies else ""))  # fmt: skip
        return 0 if d.allowed else 6
    if act == "mode":
        if len(rest) != 2 or rest[1] not in ("log", "enforce"):
            print("tool policy mode DOMAIN|all log|enforce [--force] [--why TEXT]", file=sys.stderr)
            return 2
        st, got = _gw("POST", "/gw/policy/mode", {"domain": rest[0], "mode": rest[1], "force": a.force,
                                                  "why": a.why or ""})  # fmt: skip
    elif act == "ack":
        ids = [int(x) for x in rest if x.isdigit()]
        if not ids or not a.why:
            print("tool policy ack ID.. --why TEXT", file=sys.stderr)
            return 2
        st, got = _gw("POST", "/gw/policy/ack", {"ids": ids, "why": a.why})
    elif act == "report":
        st, got = _gw("GET", f"/gw/policy?days={a.days}")
    else:
        print(f"tool policy: unknown {act!r}", file=sys.stderr)
        return 2
    if st != 200:
        print(f"hub gateway: {st or 'unreachable'} {got.get('error', '')}: {got.get('hint', '')}".rstrip(": "),
              file=sys.stderr)  # fmt: skip
        return 3
    if a.json or act != "report":
        print(json.dumps(got, indent=1, ensure_ascii=False))
        return 0
    print(f"policy {got.get('policy', {}).get('sha')} · default mode {got['modes']['default']} · last {got['days']:g} d"
          f" · gate: ≥ {got['gate']['calls']} calls, 0 unexpected denies")  # fmt: skip
    for name, d in got["domains"].items():
        print(f"  {name:<10} {d['mode']:<8} calls {d['calls']:>5} · denies {d['denies']:>3} (unexpected "
              f"{d['unexpected']}) · {'ready' if d['ready'] else 'not ready'}")  # fmt: skip
    print("  principals: " + ", ".join(f"{k} {v}" for k, v in got["principals"].items()))
    o = got["overhead_ms"]
    print(f"  hub overhead per call: p50 {o['p50']} ms, p95 {o['p95']} ms (n {o['n']})")
    for d in got["denies"][-8:]:
        print(f"  deny #{d['id']} {d['tool']} by {d['principal']}: {d['reason']}" + (" [acked]" if d["acked"] else ""))
    ready = [n for n, d in got["domains"].items() if d["ready"] and d["mode"] == "log"]
    if ready:
        print("  enforce when he agrees: " + " ; ".join(f"tool policy mode {n} enforce" for n in ready))
    return 0


def cmd_worker(reg: Registry, a: argparse.Namespace) -> int:
    from tools import worker

    return worker.main(a.action, threads=a.threads or worker.THREADS, as_json=a.json)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tool", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ls")
    p.add_argument("domain", nargs="?")
    p.add_argument("--json", action="store_true")
    p.add_argument("--bindings", action="store_true")
    p = sub.add_parser("show")
    p.add_argument("name")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("call")
    p.add_argument("name")
    p.add_argument("args", nargs="*")
    p.add_argument("--json", action="store_true")
    p.add_argument("--detailed", action="store_true")
    p.add_argument("--approval")
    p = sub.add_parser("docs")
    p.add_argument("domain", nargs="?")
    p.add_argument("--tools-md")
    p.add_argument("--write", action="store_true")
    p = sub.add_parser("gen")
    p.add_argument("--check", action="store_true")
    p.add_argument("--repo")
    p = sub.add_parser("stats")
    p.add_argument("--days", type=float, default=7.0)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("jobs")
    p.add_argument("action", nargs="?", choices=("sweep",))
    p.add_argument("--days", type=float, default=7.0)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("direct")
    p.add_argument("domain", nargs="?")
    p.add_argument("state", nargs="?")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("worker")
    p.add_argument("action", choices=("run", "ensure", "status", "stop"))
    p.add_argument("--threads", type=int, default=None, help="default: the worker's THREADS (12)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("policy")
    p.add_argument("args", nargs="*")
    p.add_argument("--days", type=float, default=30.0)
    p.add_argument("--json", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--why")
    p.add_argument("--as", dest="as_", default="brain")
    p.add_argument("--approved", action="store_true")
    p.add_argument("--tainted", action="append")
    p = sub.add_parser("serve")
    p.add_argument("--check", action="store_true", help="build the server, print its tool count, exit")
    a = ap.parse_args(argv)
    if a.cmd == "serve":
        from tools import mcp_server

        if a.check:
            import asyncio

            print(f"hub: {len(asyncio.run(mcp_server.build().list_tools()))} tools")
            return 0
        return mcp_server.serve()
    reg = load()
    try:
        return {
            "ls": cmd_ls,
            "show": cmd_show,
            "call": cmd_call,
            "docs": cmd_docs,
            "gen": cmd_gen,
            "stats": cmd_stats,
            "jobs": cmd_jobs,
            "direct": cmd_direct,
            "worker": cmd_worker,
            "policy": cmd_policy,
        }[a.cmd](reg, a)
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
