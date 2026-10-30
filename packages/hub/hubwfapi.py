"""hubwfapi: hubd's side of the workflow engine. Stdlib only: hubwf (DBOS, SQLAlchemy) is imported
only while the engine is switched on, so a hub with the engine off runs exactly what it ran before.

Switch: STATE/workflows.json {"engine": "on"|"off", "why", "ts", "by"}; no file = HUB_WF_DEFAULT (default "off": a
release deploys dark). HUB_WF=off in the environment wins (never launched). "on" launches DBOS now (a failed launch,
e.g. Postgres down, is retried every RETRY s); "off" stops it: pending workflows stay in Postgres and resume at the
next launch (DBOS recovery). Env for tests: HUB_WF_SCHEMA (default "dbos"), HUB_WF_SCHED_POLL, HUB_WF_NOTIFY_POLL.

API (brain caller only; live schema only; bodies and answers JSON; ids, states and small outputs, never secrets):
  GET  /wf/engine                       {state: off|starting|on|down, why, since, version, schema, job_timeout_max,
                                        counts?, catch_up?: the last launch's catch-up {at, run: [names],
                                        late: [names], errors: [names]} (hubwf.catch_up)}
  POST /wf/engine {on, why}             switch it (persisted)
  GET  /wf[?name=&status=&prefix=&limit=]   workflows, newest first (limit ≤ 100, default 20)
  POST /wf/start {name, args, id?}      start one of hubwf.NAMES; the same id again = the same workflow (no second run)
  GET  /wf/<id>                         status + its events (webtask.approval: waiting.until, outcome); an id may
                                        be percent-encoded (a schedule's run ids carry "+hh:mm")
  POST /wf/<id>/send {topic, message, key?}   a message (key = idempotency key: a resend with it is delivered once)
  POST /wf/<id>/cancel                  cancel (DBOS keeps it; resumable from the DBOS side)
  GET  /wf/schedules[?runs=N]           the watch schedules (+ their last N runs)
  POST /wf/schedules {name, job, cron, tz?, mode, queue?, timeout?, catch_up?}   create or replace one (mode
                                        shadow | live; timeout = the job's limit, 1-JOB_TIMEOUT_MAX s: the hub
                                        waits for it; catch_up true|false = the registry's, none = by cadence)
  POST /wf/schedules/<name>/pause|resume|delete|trigger
Errors: 400 bad request · 403 not the brain · 404 no such workflow/schedule · 503 engine off or down (hint says why).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import time
import urllib.parse
from collections.abc import Callable
from typing import Any

STATE_FILE = "workflows.json"
RETRY = 60.0
OP_TIMEOUT = 25.0
JOB_TIMEOUT_MAX = 3600  # = hubwf.JOB_TIMEOUT_MAX (here too: /wf/engine says it while the engine is off)
LIMIT_MAX = 100
# workflow ids: a schedule's runs are sched-<name>-<ISO slot> (sched-<name>-trigger-<ISO now>), offsets with "+"
WID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._+-]{0,127}$")
TOPIC_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,39}$")
SCHED_RE = r"((?:watch|job):[a-z0-9][a-z0-9-]{0,39})"
STATUSES = ("PENDING", "ENQUEUED", "SUCCESS", "ERROR", "CANCELLED", "MAX_RECOVERY_ATTEMPTS_EXCEEDED")
MAX_MESSAGE = 4096


class Bad(Exception):
    pass


def _text(v: Any, n: int = 200) -> str:
    return re.sub(r"[\x00-\x1f\x7f]", " ", v if isinstance(v, str) else "")[:n]


class Engine:
    """The engine's lifecycle in hubd: wanted (the switch), launch/destroy on a worker thread, the /wf API."""

    def __init__(self, state_dir: str, conninfo: Callable[[], dict[str, Any]], hooks: dict[str, Any]) -> None:
        self.state_dir = state_dir
        self.conninfo = conninfo
        self.hooks = hooks
        self.mod: Any = None  # hubwf once imported
        self.state = "off"
        self.why = ""
        self.since: float | None = None
        self.launched = False  # DBOS was launched in this process (hubd then ends with os._exit: no stray threads)
        self.lock = asyncio.Lock()
        self.schema = os.environ.get("HUB_WF_SCHEMA") or "dbos"
        self.caught: dict[str, Any] | None = None  # the last launch's catch-up summary (brief: catch_up)

    # -- the switch
    def switch(self) -> dict[str, Any]:
        try:
            with open(os.path.join(self.state_dir, STATE_FILE), encoding="utf-8") as f:
                d = json.load(f)
            if not isinstance(d, dict) or d.get("engine") not in ("on", "off"):
                return {"engine": "off", "why": "switch file malformed"}
            return d
        except FileNotFoundError:
            dflt = os.environ.get("HUB_WF_DEFAULT", "off")
            return {"engine": "on" if dflt == "on" else "off", "why": "default"}
        except (OSError, ValueError):
            return {"engine": "off", "why": "switch file unreadable"}

    def wanted(self) -> bool:
        return os.environ.get("HUB_WF", "") != "off" and self.switch().get("engine") == "on"

    def write_switch(self, on: bool, why: str, who: str) -> None:
        os.makedirs(self.state_dir, exist_ok=True)
        p = os.path.join(self.state_dir, STATE_FILE)
        tmp = f"{p}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"engine": "on" if on else "off", "why": why, "ts": round(time.time(), 3), "by": who}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)

    def _log(self, event: str, **kw: Any) -> None:
        print(json.dumps({"event": event, **kw}), flush=True)

    # -- lifecycle
    def _launch_sync(self) -> None:
        if self.mod is None:
            import hubwf  # noqa: PLC0415  (DBOS and SQLAlchemy load only here)

            self.mod = hubwf
        self.mod.HOOKS.clear()
        self.mod.HOOKS.update(self.hooks)
        self.mod.launch(
            self.conninfo(),
            self.schema,
            sched_poll=float(os.environ.get("HUB_WF_SCHED_POLL") or 30),
            notify_poll=float(os.environ.get("HUB_WF_NOTIFY_POLL") or 1.0),
        )

    async def ensure(self) -> None:
        async with self.lock:
            if self.state == "on" or not self.wanted():
                return
            self.state, self.why = "starting", ""
            loop = asyncio.get_running_loop()
            try:
                if self.mod is not None:
                    self.mod.LOOP = loop
                await loop.run_in_executor(None, self._prepare_and_launch, loop)
                self.launched = True
                self.state, self.since = "on", time.time()
                self._log("workflows_on", schema=self.schema, version=getattr(self.mod, "WF_VERSION", None))
            except Exception as e:  # Postgres down, a missing wheel: hubd keeps serving; retried
                self.state, self.why = "down", type(e).__name__
                self._log("workflows_failed", error=self.why)
                await loop.run_in_executor(None, self._destroy_quiet)
                return
            await self._catch_up(loop)

    async def _catch_up(self, loop: asyncio.AbstractEventLoop) -> None:
        """The ticks missed while the engine was down: each schedule's latest, once (hubwf.catch_up). Never fails the
        launch: an error is logged and the schedules run on as before."""
        fn = getattr(self.mod, "catch_up", None)
        if fn is None:
            return
        try:
            rows = await asyncio.wait_for(loop.run_in_executor(None, fn), OP_TIMEOUT * 4)
        except Exception as e:
            self.caught = {"at": round(time.time(), 3), "error": type(e).__name__}
            self._log("schedules_catch_up_failed", error=type(e).__name__)
            return
        pick = lambda v: [str(r.get("name"))[:60] for r in rows if r.get("verdict") == v]  # noqa: E731
        self.caught = {"at": round(time.time(), 3), "run": pick("run"), "late": pick("late"), "errors": pick("error")}
        for r in rows:
            if r.get("verdict") in ("run", "late", "error"):
                self._log("schedule_catch_up", **{k: r.get(k) for k in ("name", "verdict", "tick", "late_s", "why")})

    def _prepare_and_launch(self, loop: asyncio.AbstractEventLoop) -> None:
        if self.mod is None:
            import hubwf  # noqa: PLC0415

            self.mod = hubwf
        self.mod.LOOP = loop
        self._launch_sync()

    def _destroy_quiet(self) -> None:
        if self.mod is None:
            return
        try:
            self.mod.destroy()
        except Exception:
            pass

    async def stop(self, why: str = "stopped") -> None:
        async with self.lock:
            if self.mod is not None and self.state in ("on", "down", "starting"):
                await asyncio.get_running_loop().run_in_executor(None, self._destroy_quiet)
                self._log("workflows_off", why=why)
            self.state, self.why, self.since = "off", why, None

    async def run(self, stopping: asyncio.Event) -> None:
        """Launch when wanted; retry a failed launch every RETRY s."""
        while not stopping.is_set():
            if self.wanted() and self.state != "on":
                await self.ensure()
            try:
                await asyncio.wait_for(stopping.wait(), RETRY)
            except TimeoutError:
                pass
        await self.stop("hubd stopping")

    def brief(self) -> dict[str, Any]:
        sw = self.switch()
        return {
            "state": self.state,
            "why": self.why or ("HUB_WF=off" if os.environ.get("HUB_WF") == "off" else ""),
            "switch": sw.get("engine"),
            "since": round(self.since, 3) if self.since else None,
            "version": self.mod.WF_VERSION if self.mod is not None else None,
            "schema": self.schema,
            "job_timeout_max": JOB_TIMEOUT_MAX,
            **({"catch_up": self.caught} if self.caught is not None else {}),
        }

    # -- the API
    async def _call(self, fn: Callable[..., Any], *args: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await asyncio.wait_for(loop.run_in_executor(None, fn, *args), OP_TIMEOUT)

    async def api(self, method: str, path: str, who: str, schema: str, query: dict[str, str], body: bytes
                  ) -> tuple[int, Any]:  # fmt: skip
        if who != "brain":
            return 403, {"error": "forbidden", "hint": f"{path} is not the {who} caller's"}
        if schema:
            return 400, {"error": "bad_request", "hint": "workflows are live only (no X-Hub-Schema)"}
        try:
            a = json.loads(body or b"{}") if body else {}
        except ValueError:
            return 400, {"error": "bad_json"}
        if not isinstance(a, dict):
            return 400, {"error": "bad_json", "hint": "a JSON object"}
        try:
            return await self._route(method, path, who, query, a)
        except Bad as e:
            return 400, {"error": "bad_request", "hint": str(e)[:300]}
        except ValueError as e:
            return 400, {"error": "bad_request", "hint": str(e)[:300]}
        except LookupError as e:
            return 404, {"error": "not_found", "hint": str(e)[:300]}
        except TimeoutError:
            return 503, {"error": "workflows_busy", "hint": "the engine did not answer in time: retry"}
        except Exception as e:
            name = type(e).__name__
            if name.startswith("DBOS"):  # DBOS refuses: an invalid cron, an unregistered function, a duplicate ...
                return 400, {"error": "bad_request", "hint": f"{name}: {str(e)[:240]}"}
            self._log("workflows_error", path=re.sub(r"/wf/[^/]+", "/wf/<id>", path)[:60], error=name)
            return 500, {"error": "internal", "hint": name}

    async def _route(self, method: str, path: str, who: str, q: dict[str, str], a: dict[str, Any]
                     ) -> tuple[int, Any]:  # fmt: skip
        if path == "/wf/engine":
            if method == "GET":
                out = self.brief()
                if self.state == "on":
                    out["counts"] = await self._call(self.mod.counts)
                return 200, out
            if method == "POST":
                on = a.get("on")
                if not isinstance(on, bool):
                    raise Bad("on: true or false")
                why = _text(a.get("why")) or ("brain: on" if on else "brain: off")
                self.write_switch(on, why, who)
                self._log("workflows_switch", on=on)
                if on:
                    await self.ensure()
                else:
                    await self.stop(why)
                return 200, self.brief()
            return 404, {"error": "not_found"}
        if self.state != "on":
            hint = "the engine is off: `hub wf engine on`" if self.state == "off" else f"the engine is {self.state}"
            return 503, {"error": "workflows_off", "hint": hint + (f" ({self.why})" if self.why else "")}
        m = self.mod
        if path == "/wf" and method == "GET":
            name = q.get("name") or None
            if name is not None and name not in (*m.NAMES, "job.tick"):
                raise Bad(f"name: one of {', '.join((*m.NAMES, 'job.tick'))}")
            st = q.get("status") or None
            if st is not None and st not in STATUSES:
                raise Bad(f"status: one of {', '.join(STATUSES)}")
            prefix = q.get("prefix") or None
            if prefix is not None and not WID_RE.match(prefix):
                raise Bad("prefix: a workflow id prefix")
            try:
                limit = max(1, min(LIMIT_MAX, int(q.get("limit") or 20)))
            except ValueError:
                raise Bad("limit: a number") from None
            return 200, {"rows": await self._call(m.listing, name, st, limit, prefix)}
        if path == "/wf/start" and method == "POST":
            name = a.get("name")
            if name not in m.NAMES:
                raise Bad(f"name: one of {', '.join(m.NAMES)}")
            wid = a.get("id") or f"{str(name).replace('.', '-')}-{secrets.token_hex(6)}"
            if not isinstance(wid, str) or not WID_RE.match(wid):
                raise Bad("id: letters, digits and : . _ + - (≤ 128)")
            args = a.get("args") if a.get("args") is not None else {}
            if len(json.dumps(args)) > MAX_MESSAGE:
                raise Bad(f"args: at most {MAX_MESSAGE} bytes")
            r = await self._call(m.start, name, args, wid)
            self._log("workflow_start", name=name, id=wid[:64])
            return 200, r
        if path == "/wf/schedules":
            if method == "GET":
                try:
                    runs = max(0, min(20, int(q.get("runs") or 0)))
                except ValueError:
                    raise Bad("runs: a number") from None
                return 200, {"rows": await self._call(m.schedules, runs)}
            if method == "POST":
                name, job, cron, tz, mode = (a.get(k) for k in ("name", "job", "cron", "tz", "mode"))
                if not all(isinstance(x, str) and x for x in (name, job, cron, mode)):
                    raise Bad("name, job, cron and mode: strings")
                if tz is not None and (not isinstance(tz, str) or not re.fullmatch(r"[A-Za-z0-9_+/-]{1,64}", tz)):
                    raise Bad("tz: an IANA zone name")
                queue = a.get("queue")
                if queue is not None and (not isinstance(queue, str) or queue not in m.QUEUES):
                    raise Bad(f"queue: {' | '.join(m.QUEUES)}")
                timeout = a.get("timeout")
                if timeout is not None and (
                    isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= JOB_TIMEOUT_MAX
                ):
                    raise Bad(f"timeout: whole seconds 1-{JOB_TIMEOUT_MAX}")
                catch_up = a.get("catch_up")
                if catch_up is not None and not isinstance(catch_up, bool):
                    raise Bad("catch_up: true or false (none = by cadence)")
                r = await self._call(m.schedule_set, name, job, cron, tz, mode, queue, timeout, catch_up)
                self._log("schedule_set", name=name, mode=mode, queue=queue or m.JOB_QUEUE, timeout=timeout,
                          catch_up=catch_up)  # fmt: skip
                return 200, r
            return 404, {"error": "not_found"}
        g = re.fullmatch(rf"/wf/schedules/{SCHED_RE}/(pause|resume|delete|trigger)", path)
        if g and method == "POST":
            r = await self._call(m.schedule_op, g[1], g[2])
            self._log("schedule_op", name=g[1], op=g[2])
            return 200, r
        g = re.fullmatch(r"/wf/([^/]{1,400})(?:/(send|cancel))?", path)
        wid = urllib.parse.unquote(g[1]) if g else ""
        if g is None or not WID_RE.match(wid) or wid in ("engine", "start", "schedules"):
            return 404, {"error": "not_found"}
        op = g[2]
        if op is None and method == "GET":
            r = await self._call(m.status, wid)
            if r is None:
                raise LookupError(f"no workflow {wid}")
            return 200, r
        if op == "send" and method == "POST":
            topic, key = a.get("topic"), a.get("key")
            if not isinstance(topic, str) or not TOPIC_RE.match(topic):
                raise Bad("topic: [a-z0-9_.-] (≤ 40)")
            if key is not None and (not isinstance(key, str) or not WID_RE.match(key)):
                raise Bad("key: letters, digits and : . _ + - (≤ 128)")
            msg = a.get("message")
            if len(json.dumps(msg)) > MAX_MESSAGE:
                raise Bad(f"message: at most {MAX_MESSAGE} bytes")
            await self._call(m.send, wid, topic, msg, key)
            return 200, {"sent": True, "id": wid, "topic": topic}
        if op == "cancel" and method == "POST":
            r = await self._call(m.cancel, wid)
            self._log("workflow_cancel", id=wid[:64])
            return 200, r
        return 404, {"error": "not_found"}
