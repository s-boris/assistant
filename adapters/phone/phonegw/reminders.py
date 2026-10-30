"""Reminders screen API (app): GET /v1/reminders = his pending reminders + the ones fired in the last 36 h;
POST /v1/reminders = one change (add | snooze | move | done | cancel | skip | preview). Backed by `remind` (its
`list --json --mine`, `when --json` and verbs; REMIND_VIA=app tags the change for analytics). Mixed into Gateway."""
import asyncio
import datetime as dt
import hashlib
import json
import logging
import os
import re

from aiohttp import web

from . import bridge
from . import now as N
from .schema import ProtocolError

log = logging.getLogger("phonegw")
GW = "_gw"
REM_TIMEOUT = 25
REM_IDS = 500                     # replay dedupe window (ids of applied ops)
FMT = "%Y-%m-%d %H:%M"
STATES = ("fired", "snoozed", "done")


def where_of(on):
    if not on:
        return None
    if on == "home":
        return "when home"
    if on == "leave":
        return "when leaving home"
    if on.startswith("leave:zone:"):
        return "when leaving " + on[11:]
    return "when at " + on.split(":", 1)[-1]


class Reminders:
    def rem_epoch(self, s):
        """'YYYY-MM-DD HH:MM' owner wall time -> epoch seconds (None when unusable)."""
        try:
            return int(dt.datetime.strptime(str(s), FMT).replace(tzinfo=N.tz_of(self.cfg)).timestamp())
        except (TypeError, ValueError):
            return None

    async def rem_run(self, args, timeout=REM_TIMEOUT):
        """remind ARGS with REMIND_VIA=app -> (rc or None, stdout, stderr)."""
        env = dict(os.environ, REMIND_VIA="app")
        try:
            p = await asyncio.create_subprocess_exec(bridge.tool("REMIND_BIN", "remind"), *args,
                                                     stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                                                     stderr=asyncio.subprocess.PIPE, env=env)
            out, err = await asyncio.wait_for(p.communicate(), timeout)
            return p.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
        except (OSError, asyncio.TimeoutError) as e:
            log.warning("remind %s failed: %s", args[:1], type(e).__name__)
            return None, "", ""

    def rem_shape(self, d):
        """`remind list --json --mine` -> http.reminders.response."""
        items = []
        for x in d.get("items") or []:
            if not isinstance(x, dict) or not re.fullmatch(r"[0-9a-f]{8}", str(x.get("id"))):
                continue
            kind = x.get("kind") if x.get("kind") in ("timed", "series", "place") else "timed"
            it = {"id": x["id"], "kind": kind, "at": None if kind == "place" else self.rem_epoch(x.get("at")),
                  "title": str(x.get("text") or "")[:300], "call": bool(x.get("call"))}
            if kind == "place":
                it.update(where=str(x.get("where") or where_of(x.get("on")) or "")[:80] or None,
                          every_time=bool(x.get("every_time")), held_until=self.rem_epoch(x.get("held")))
            else:
                it.update(every=(str(x["every"])[:80] if x.get("every") else None), where=None)
                if x.get("local"):
                    it["local"] = str(x["local"])[:40]
            if kind != "place" and it["at"] is None:
                continue
            items.append(it)
        recent = []
        for x in d.get("recent") or []:
            if not isinstance(x, dict) or not re.fullmatch(r"[0-9a-f]{8}", str(x.get("id"))):
                continue
            fired = self.rem_epoch(x.get("fired"))
            if fired is None:
                continue
            recent.append({"id": x["id"], "title": str(x.get("text") or "")[:300],
                           "state": x.get("state") if x.get("state") in STATES else "fired", "fired": fired,
                           "to": self.rem_epoch(x.get("to")) if x.get("to") else None, "where": where_of(x.get("on"))})
        now = self.rem_epoch(d.get("now")) or int(dt.datetime.now().timestamp())
        return {"ok": True, "now": now, "items": items[:200], "recent": recent[:50]}

    async def rem_list(self):
        rc, out, _ = await self.rem_run(["list", "--json", "--mine"])
        if rc != 0:
            return None
        try:
            d = json.loads(out)
        except ValueError:
            return None
        return self.rem_shape(d) if isinstance(d, dict) else None

    async def x_reminders(self, request):
        body = await self.rem_list()
        if body is None:
            return web.json_response({"ok": False, "error": "unavailable", "message": "remind unavailable"}, status=503)
        self.touch(request["device"]["id"])
        stable = dict(body, now=0)                     # the ETag ignores the clock
        etag = '"%s"' % hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()[:20]
        if request.headers.get("If-None-Match") == etag:
            return web.Response(status=304, headers={"ETag": etag})
        return web.Response(body=json.dumps(body, ensure_ascii=False).encode(), content_type="application/json",
                            charset="utf-8", headers={"ETag": etag, "Cache-Control": "no-cache"})

    def rem_usage(self, op, status):
        """One analytics event per app change (op + status codes only)."""
        argv = [bridge.tool("USAGE_BIN", "usage"), "log", "reminders.app", "invoke", "--channel", "app", "--actor",
                "owner", "--attr", "op=" + op, "--attr", "status=" + status]
        try:
            asyncio.get_running_loop().create_task(self._rem_fire(argv))
        except RuntimeError:
            pass

    async def _rem_fire(self, argv):
        try:
            p = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL,
                                                     stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(p.wait(), 10)
        except (OSError, asyncio.TimeoutError):
            pass

    async def x_reminders_op(self, request):
        r = await self._json(request)
        if r is None:
            return web.json_response({"ok": False, "error": "usage"}, status=400)
        try:
            self.proto.check_http("reminders.request", r)
        except ProtocolError as e:
            return web.json_response({"ok": False, "error": "schema", "problems": (e.problems or [e.msg])[:5]}, status=422)
        self.touch(request["device"]["id"])
        op, rid, when = r["op"], r.get("rid"), r.get("when")
        if op == "preview":
            rc, out, _ = await self.rem_run(["when", "--json", when])
            try:
                d = json.loads(out) if rc in (0, 2) else None
            except ValueError:
                d = None
            if not isinstance(d, dict):
                return web.json_response({"ok": False, "id": r["id"], "status": "failed"})
            if not d.get("ok"):
                return web.json_response({"ok": False, "id": r["id"], "status": "invalid",
                                          "message": str(d.get("error") or "could not read that")[:200]})
            at = self.rem_epoch(d.get("at"))
            when_txt = dt.datetime.strptime(d["at"], FMT).strftime("%a %d.%m. %H:%M") if d.get("at") else ""
            why = str(d.get("why") or "")
            msg = "%s (%s)" % (when_txt, why) if when_txt and why else (when_txt or why)   # the app reads the (reason)
            return web.json_response({"ok": True, "id": r["id"], "status": "preview", "message": msg[:200],
                                      "at": at, "where": where_of(d.get("on"))})
        done, _ = self.db.get_state(GW, "reminders.ids", [])
        done = list(done or [])
        if r["id"] in done:
            body = {"ok": True, "id": r["id"], "status": "duplicate"}
            lst = await self.rem_list()
            if lst is not None:
                body["list"] = lst
            return web.json_response(body)
        args = {"add": ["add", "--owner", when or "", r.get("text") or ""], "snooze": ["snooze", rid, when or ""],
                "move": ["move", rid, when or ""], "done": ["done", rid], "cancel": ["cancel", rid],
                "skip": ["skip", rid]}[op]
        rc, out, err = await self.rem_run(args)
        if rc is None:
            self.rem_usage(op, "failed")
            return web.json_response({"ok": False, "id": r["id"], "status": "failed"})
        self.db.put_state(GW, "reminders.ids", (done + [r["id"]])[-REM_IDS:])
        line = (out.strip() or err.strip()).splitlines()[0] if (out.strip() or err.strip()) else ""
        if rc != 0 or line.startswith(("no such", "no matching")):
            self.rem_usage(op, "invalid")
            msg = re.sub(r"^remind(?: \w+)?: ", "", line) or "that did not work"
            return web.json_response({"ok": False, "id": r["id"], "status": "invalid", "message": msg[:200]})
        m = re.search(r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun) (\d{4}-\d\d-\d\d \d\d:\d\d)", line)
        at = self.rem_epoch(m[2]) if m and op in ("add", "snooze", "move", "skip") else None
        pm = re.search(r"📍 (when [^,]+)", line)
        if m and op in ("add", "snooze", "move", "skip"):
            msg = dt.datetime.strptime(m[2], FMT).strftime("%a %d.%m. %H:%M")
            rest = line[m.end():].strip()
            if op == "add" and rest.startswith("(") and rest.endswith(")"):
                msg += " (" + rest[1:-1] + ")"
        elif pm:
            msg = "📍 " + pm[1]
        else:
            msg = {"done": "Done", "cancel": "Cancelled"}.get(op, "OK")
        body = {"ok": True, "id": r["id"], "status": "ok", "message": msg[:200], "at": at,
                "where": pm[1][:80] if pm else None}
        lst = await self.rem_list()
        if lst is not None:
            body["list"] = lst
        self.rem_usage(op, "ok")
        try:
            await self.sync_alarms()                   # local alarms follow the change (no-op when unchanged/off)
        except Exception as e:                         # never fail the change over the alarm resync
            log.warning("alarm resync after reminder %s failed: %s", op, type(e).__name__)
        return web.json_response(body)
