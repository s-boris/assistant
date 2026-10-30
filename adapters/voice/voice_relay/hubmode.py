"""Hub mode: the relay inside claude-hub-voice (cfg.hub_mode, set by ops/hub/voiceloader.py).
The relay keeps its own call and model code; only the ways out of the container change:
  hand-offs  handoff route "hub": POST {hub_url}/voice/event kind voice.handoff with the bridge's request
             (channel) and the inbox note; the brain's `hub voice inbox` hands it to the live session (else a note)
  notes      recap / decisions / alert notes the relay writes into cfg.inbox_dir (a container dir) are shipped as
             voice.* events and removed once the hub has them (Shipper)
  voice.*    tools the brain calls through the gateway run here: the Executor long-polls {hub_gw_url}/gw/claim
             with this container's token and answers through the relay's own internal handlers (in process);
             voice.status also carries the newest calls of the call log (which stays here) as structured fields
             only (calllog.digest: kinds, end, ring outcome and answered, closing stages, tool names and lanes,
             counts; no words or args); voice.calls carries more of them (last N, outgoing only), each outgoing
             call's outcome and the quality numbers over N days (`voice-relay calls|stats` outside this container)
  lookups    the fast lane, the decision deck and phone open call the hub gateway as principal voice
             (voice.lookup runs the read-only CLI in the brain container; phone.open opens on his phone)
Nothing here sees a key: the hub token file is read at use, never logged; payloads carry only what a hand-off
or note carries today.
"""
import asyncio
import json
import os
import re
import secrets
import time

import aiohttp

from . import calllog
from .config import read_secret

NOTE_KINDS = (("voice-recap-", "voice.recap"), ("voice-decisions-", "voice.decisions"), ("bigfile-", "voice.file"),
              ("voicerelay-", "voice.alert"), ("voice-", "voice.handoff"))
NOTE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.md$")
CLAIM_WAIT = 20.0


class HubError(Exception):
    pass


class Hub:
    def __init__(self, cfg):
        self.cfg = cfg
        self._http = None
        self._rpc = 0

    @property
    def token(self):
        return read_secret(self.cfg.hub_token_file)

    async def http(self):
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession()
        return self._http

    async def close(self):
        if self._http is not None:
            await self._http.close()

    async def post(self, url, body, timeout=10.0, headers=None):
        tok = self.token
        if not tok:
            raise HubError("no hub token")
        s = await self.http()
        h = {"Authorization": "Bearer " + tok, **(headers or {})}
        try:
            async with s.post(url, json=body, headers=h, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                text = await r.text()
                try:
                    data = json.loads(text) if text else {}
                except ValueError:
                    data = {}
                return r.status, data
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            raise HubError(type(e).__name__) from None

    async def event(self, kind, subject, payload, idem):
        st, d = await self.post(self.cfg.hub_url.rstrip("/") + "/voice/event",
                                {"kind": kind, "subject": subject[:200], "payload": payload, "idem_key": idem,
                                 "rid": "ve-" + secrets.token_hex(8)}, timeout=5.0)
        return st == 200 and isinstance(d, dict)

    async def tool(self, name, args, timeout=10.0):
        """One gateway call (MCP tools/call, stateless JSON) -> (ok, structured data, text)."""
        self._rpc += 1
        st, d = await self.post(self.cfg.hub_gw_url.rstrip("/") + "/mcp",
                                {"jsonrpc": "2.0", "id": self._rpc, "method": "tools/call",
                                 "params": {"name": name, "arguments": args}},
                                timeout=timeout,
                                headers={"Accept": "application/json, text/event-stream"})  # fmt: skip
        r = d.get("result") if isinstance(d, dict) else None
        if st != 200 or not isinstance(r, dict):
            raise HubError("gateway http %s" % st)
        text = "".join(c.get("text", "") for c in r.get("content") or [] if isinstance(c, dict))
        return not r.get("isError"), r.get("structuredContent") or {}, text

    async def lookup(self, argv, timeout):
        """A fast-lane / deck CLI through the gateway (voice.lookup, run in the brain container) -> (rc, stdout)."""
        try:
            ok, data, _text = await self.tool("voice_lookup", {"argv": [os.path.basename(argv[0])] + list(argv[1:]),
                                                               "timeout_s": float(timeout)}, timeout=timeout + 5)  # fmt: skip
        except HubError:
            return None, ""
        if not ok:
            return 127, ""
        return int(data.get("rc", 1)), str(data.get("out") or "")

    async def phone_open(self, argv, timeout):
        """`phone open ...` argv (phoneopen.py) as the gateway's phone.open -> (rc, the CLI's JSON)."""
        args = phone_args(argv)
        if args is None:
            return 2, ""
        try:
            ok, data, _text = await self.tool("phone_open", {**args, "format": "detailed"}, timeout=timeout + 5)
        except HubError:
            return None, ""
        if not ok:
            return 7, ""
        return 0, json.dumps(data.get("result") or data)


def phone_args(argv):
    """The typed phone.open arguments for the relay's `phone open` argv; None when it is not one we know."""
    a = list(argv[2:]) if len(argv) >= 2 and argv[1] == "open" else None
    if a is None:
        return None
    out = {"via": "voice"}
    i = 0
    while i < len(a):
        k = a[i]
        if k == "--nav" and i + 1 < len(a):
            out["nav"], i = a[i + 1], i + 2
        elif k == "--mode" and i + 1 < len(a):
            out["mode"], i = a[i + 1][:1], i + 2
        elif k == "--sbb" and i + 2 < len(a):
            out["sbb_from"], out["sbb_to"], i = a[i + 1], a[i + 2], i + 3
        elif k in ("--arrive", "--at") and i + 1 < len(a):
            out[k[2:]], i = a[i + 1], i + 2
        elif k == "--app" and i + 1 < len(a):
            out["app"], i = a[i + 1], i + 2
        elif k in ("--label", "--wait", "--via") and i + 1 < len(a):
            i += 2
        elif k == "--json":
            i += 1
        elif k.startswith("https://") and "url" not in out:
            out["url"], i = k, i + 1
        else:
            return None
    return out if len(out) > 1 else None


class _Req:
    """Just enough of an aiohttp request for the relay's internal handlers."""

    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def _body(resp):
    try:
        return json.loads(resp.body or b"{}")
    except (ValueError, AttributeError, TypeError):
        return {}


CALL_OK = {"answered"}


class Executor:
    """voice.* tools from the hub gateway, run by this relay (hubgw ADAPTER_RUN)."""

    def __init__(self, relay, hub):
        self.relay, self.hub = relay, hub
        self.talk_lock = asyncio.Lock()
        self.served = 0

    async def run_tool(self, tool, args):
        r = self.relay
        if tool == "voice.reply":
            res = _body(await r.i_answer(_Req({"id": args.get("id"), "text": args.get("text"),
                                               "more": args.get("more") is True})))  # fmt: skip
            d = res.get("delivered")
            if d in ("sync", "live"):
                return {"delivered": True, "status": "spoken"}
            return {"delivered": False, "status": "not_delivered", "note": "the call is over: send it on Telegram"}
        if tool == "voice.status":
            d = _body(await r.i_status(_Req({})))
            tg = d.get("tg_calls")
            res = {k: d.get(k) for k in ("provider", "live", "app_calls", "minutes_today", "minutes_cap", "call_on",
                                         "call_failures")}
            try:   # the call log stays in this container: its newest calls as structured fields (calllog.digest)
                res["calls"] = calllog.recent(r.cfg, 5)
            except Exception:  # noqa: BLE001 (a broken log never fails the status)
                res["calls"] = None
            try:   # its 7-day quality numbers (calllog.week: counts and timings only) for the dashboard
                res["stats_7d"] = calllog.week(r.cfg, 7)
            except Exception:  # noqa: BLE001 (a broken log never fails the status)
                res["stats_7d"] = None
            return {"up": True, "live": d.get("live"), "tg_calls": tg if isinstance(tg, str) else None, "result": res}
        if tool == "voice.calls":   # the call log stays here: digests + outgoing outcomes + quality numbers, no text
            last = max(0, min(50, int(args.get("last", 5))))
            days = float(args.get("days") or 7)
            days = days if 0 < days <= calllog.KEEP_DAYS else 7.0
            calls = calllog.recent(r.cfg, last, outgoing=args.get("outgoing") is True) if last else []
            ids = _body(await r.i_status(_Req({}))).get("call_ids")
            return {"calls": calls, "outgoing": calllog.outgoing_summary(calls), "stats": calllog.stats(r.cfg, days),
                    "live": [str(i)[:20] for i in ids] if isinstance(ids, list) else None}
        if tool in ("voice.call", "voice.drive_update", "voice.talk"):
            if tool == "voice.talk":
                if self.talk_lock.locked():
                    return {"delivered": False, "status": "ringing_already"}
                async with self.talk_lock:
                    return await self._call({"reason": "you tapped Talk"})
            body = {"reason": args.get("reason") or args.get("text") or "", "say": args.get("say") or ""}
            out = await self._call(body)
            if tool == "voice.drive_update" and not out["delivered"]:
                out["note"] = "missed: the car message and the voice note go from the brain (drive-update)"
            return out
        raise KeyError(tool)

    async def _call(self, body):
        d = _body(await self.relay.i_call(_Req(body)))
        raw = str(d.get("status") or "failed")
        st = {"busy": "not_answered", "declined": "not_answered", "no_answer": "not_answered",
              "in_call": "busy_or_limited", "rate_limited": "busy_or_limited", "nothing": "nothing_to_decide"}.get(raw, raw)
        # result = the relay's own answer, as `voice-relay call` prints it: the brain's voice-hub hands it to the
        # callers (talk-button tells rate-limited from in-call by it); status, detail, ring time, call-log id only
        res = {k: d[k] for k in ("ring_s", "call") if isinstance(d.get(k), (int, float, str))}
        res.update(status=raw, **({"detail": str(d["detail"])[:200]} if d.get("detail") else {}))
        return {"delivered": st in CALL_OK, "status": st, "note": None if st in CALL_OK else "send it on Telegram",
                "result": res}

    async def once(self):
        st, d = await self.hub.post(self.relay.cfg.hub_gw_url.rstrip("/") + "/gw/claim", {"wait": CLAIM_WAIT},
                                    timeout=CLAIM_WAIT + 10)  # fmt: skip
        job = d.get("job") if st == 200 and isinstance(d, dict) else None
        if not isinstance(job, dict) or not isinstance(job.get("id"), str):
            return False
        t0 = time.monotonic()
        try:
            data = await self.run_tool(str(job.get("tool")), job.get("args") or {})
            data = {k: v for k, v in data.items() if v is not None}
            res = {"ok": True, "data": data, "text": json.dumps(data, ensure_ascii=False)}
        except KeyError:
            res = {"ok": False, "code": "not_found", "error": {"code": "not_found", "message": "not a voice tool",
                                                                 "hint": "voice.* only"}}  # fmt: skip
        except Exception as e:  # never leave the gateway waiting for its timeout
            res = {"ok": False, "code": "internal", "error": {"code": "internal",
                                                              "message": "relay: %s" % type(e).__name__, "hint": ""}}  # fmt: skip
        res["ms"] = int((time.monotonic() - t0) * 1000)
        await self.hub.post(self.relay.cfg.hub_gw_url.rstrip("/") + "/gw/result", {"id": job["id"], "result": res})
        self.served += 1
        return True

    async def loop(self):
        backoff = 1.0
        while True:
            try:
                await self.once()
                backoff = 1.0
            except HubError:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)


class Shipper:
    """Notes in cfg.inbox_dir (recap, decisions, alerts, hand-off fallbacks) -> voice.* events, then removed."""

    def __init__(self, cfg, hub, every=2.0):
        self.cfg, self.hub, self.every = cfg, hub, every
        self.shipped = 0

    @staticmethod
    def kind(name):
        for prefix, kind in NOTE_KINDS:
            if name.startswith(prefix):
                return kind
        return "voice.alert"

    async def once(self):
        d = self.cfg.inbox_dir
        try:
            names = sorted(n for n in os.listdir(d) if NOTE_NAME.match(n))
        except OSError:
            return 0
        n = 0
        for name in names:
            p = os.path.join(d, name)
            try:
                with open(p, encoding="utf-8") as fh:
                    text = fh.read(16000)
            except OSError:
                continue
            try:
                ok = await self.hub.event(self.kind(name), name, {"note": {"name": name, "text": text}}, "note:" + name)
            except HubError:
                return n
            if ok:
                os.unlink(p)
                n += 1
                self.shipped += 1
        return n

    async def loop(self):
        while True:
            await self.once()
            await asyncio.sleep(self.every)


def install(relay):
    """Hub mode on: lookups through the gateway, the executor and the note shipper as tasks. -> the tasks."""
    from . import decisions, fastlane, phoneopen
    cfg = relay.cfg
    hub = Hub(cfg)
    relay.hub = hub

    async def deck_run(argv):
        rc, out = await hub.lookup(argv, cfg.decision_timeout_s)
        if rc != 0:
            raise decisions.DeckError("deck lookup %s" % rc)
        return out

    fastlane.CMD_RUNNER = hub.lookup
    decisions.RUNNER = deck_run
    phoneopen.CMD_RUNNER = hub.phone_open
    relay.executor, relay.shipper = Executor(relay, hub), Shipper(cfg, hub)
    return [asyncio.ensure_future(relay.executor.loop()), asyncio.ensure_future(relay.shipper.loop())]
