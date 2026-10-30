"""Hand-off of ask_assistant tasks to the main Claude Code session, and the answers coming back.

Routes, tried in cfg.handoff_order:
  channel  POST to the telegram-plus voice inbox (staged patch): a channel event in the live session, ms away
  inbox    an ~/inbox note: picked up at the next heartbeat (2-3 min); also the fallback when the channel is down
  worker   optional claude -p lane for quick read-only lookups; replies HANDOFF to pass to the next route
Answers arrive via the internal API (voice-reply) and pass the answer filter before reaching the provider.
"""
import asyncio
import os
import secrets
import shlex
import time

import aiohttp

from .answer_filter import filter_answer
from .config import read_secret

INBOX_NOTE = """# Voice task ({urgency})
id: {id}
The owner asked this in a live voice session (relay login = the owner): {task}
Answer with: voice-reply {id} "short spoken answer" (2 sentences max, no identifiers; details go to Telegram).
If voice-reply says "not delivered", send the answer on Telegram instead.
"""

WORKER_PROMPT = """You answer one quick question from the owner's live voice call. Read-only: change nothing,
message nobody. Answer in at most two short spoken sentences, no lists, links or identifiers. If it needs more
than a quick read-only lookup, or you are unsure, reply exactly HANDOFF.

Question: {task}
"""


class Pending:
    __slots__ = ("id", "task", "urgency", "created", "route", "future", "waiting", "deliver_late", "owner",
                 "t_sent", "t_answer", "parts")

    def __init__(self, task, urgency, deliver_late, owner):
        self.id = "v" + secrets.token_hex(4)
        self.task, self.urgency = task, urgency
        self.created = time.time()
        self.route = "failed"
        self.future = asyncio.get_running_loop().create_future()
        self.waiting = False
        self.deliver_late, self.owner = deliver_late, owner
        self.t_sent = self.t_answer = None
        self.parts = 0                   # partial answers spoken so far (voice-reply --more)


class Handoff:
    def __init__(self, cfg, token=None):
        self.cfg = cfg
        self._token = token
        self.pending = {}
        self._http = None

    @property
    def token(self):
        return self._token or read_secret(self.cfg.local_token_file)

    async def http(self):
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession()
        return self._http

    async def close(self):
        if self._http is not None:
            await self._http.close()

    async def submit(self, task, urgency="now", deliver_late=None, owner=None):
        p = Pending(task, urgency, deliver_late, owner)
        self.pending[p.id] = p
        self._gc()
        await self._route(p, list(self.cfg.handoff_order))
        return p

    async def _route(self, p, order):
        for i, name in enumerate(order):
            if await getattr(self, "_via_" + name)(p, order[i + 1:]):
                p.route = name
                p.t_sent = time.monotonic()
                return True
        p.route = "failed"
        return False

    async def _via_channel(self, p, rest):
        token = self.token
        if not token:
            return False
        try:
            s = await self.http()
            async with s.post(self.cfg.plugin_url, json={"id": p.id, "task": p.task, "urgency": p.urgency},
                              headers={"Authorization": "Bearer " + token},
                              timeout=aiohttp.ClientTimeout(total=3)) as r:
                return r.status == 202
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            return False

    async def _via_hub(self, p, rest):
        """Hub mode: a voice.handoff event carrying the bridge's request and the inbox note (`hub voice inbox`
        in the brain container hands it to the live session, else writes the note)."""
        hub = getattr(self, "hub", None)
        if hub is None:
            return False
        name = "voice-%s-%s.md" % (time.strftime("%Y%m%d-%H%M%S"), p.id)
        payload = {"channel": {"id": p.id, "task": p.task, "urgency": p.urgency},
                   "note": {"name": name, "text": INBOX_NOTE.format(id=p.id, task=p.task, urgency=p.urgency)}}
        try:
            return await hub.event("voice.handoff", "hand-off " + p.id, payload, "handoff:" + p.id)
        except Exception:  # noqa: BLE001 (hub down: the next route takes it)
            return False

    async def _via_inbox(self, p, rest):
        try:
            os.makedirs(self.cfg.inbox_dir, exist_ok=True)
            name = "voice-%s-%s.md" % (time.strftime("%Y%m%d-%H%M%S"), p.id)
            tmp = os.path.join(self.cfg.inbox_dir, "." + name + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(INBOX_NOTE.format(id=p.id, task=p.task, urgency=p.urgency))
            os.replace(tmp, os.path.join(self.cfg.inbox_dir, name))
            return True
        except OSError:
            return False

    async def _via_worker(self, p, rest):
        if not self.cfg.worker_cmd or p.urgency != "now":
            return False
        asyncio.ensure_future(self._run_worker(p, rest))
        return True

    async def _run_worker(self, p, rest):
        out = ""
        try:
            os.makedirs(self.cfg.worker_cwd, exist_ok=True)
            proc = await asyncio.create_subprocess_exec(
                *shlex.split(self.cfg.worker_cmd), cwd=self.cfg.worker_cwd, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            try:
                so, _ = await asyncio.wait_for(proc.communicate(WORKER_PROMPT.format(task=p.task).encode()),
                                               self.cfg.worker_timeout_s)
                out = so.decode("utf-8", "replace").strip() if proc.returncode == 0 else ""
            except asyncio.TimeoutError:
                proc.kill()
        except OSError:
            out = ""
        if out and out.strip() != "HANDOFF" and p.id in self.pending:
            await self.answer(p.id, out)
        elif p.id in self.pending:
            await self._route(p, rest)   # pass on to the main session

    async def wait(self, p, timeout):
        """The answer if it lands within timeout (spoken as the tool result), else None."""
        if timeout <= 0:
            return None
        p.waiting = True
        try:
            return await asyncio.wait_for(asyncio.shield(p.future), timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            p.waiting = False

    async def answer(self, vid, text, more=False):
        """From voice-reply. delivered: sync (tool result) | live (pushed into the open call) | none (use Telegram).
        more=True (voice-reply --more): a first part spoken now, the hand-off stays open for the rest (streaming
        a quick first sentence while the real work runs). A part that can't be spoken leaves it open too."""
        p = self.pending.get(vid) if more else self.pending.pop(vid, None)
        if p is None:
            return {"delivered": "unknown"}
        f = filter_answer(text, self.cfg.mask_amounts)
        p.t_answer = time.monotonic()
        delivered = "none"
        if p.waiting and not p.future.done():
            if more:
                p.parts += 1
            p.future.set_result(f.text)
            delivered = "sync"
        elif p.deliver_late is not None:
            try:
                if await (p.deliver_late(p.id, f.text, more=True) if more else p.deliver_late(p.id, f.text)):
                    delivered = "live"
                    p.parts += 1 if more else 0
            except Exception:
                delivered = "none"
        res = {"delivered": delivered, "spoken": f.text, "masked": f.masked, "truncated": f.truncated}
        if more:
            res["open"] = True
        if p.t_sent is not None:
            res["handoff_to_answer_ms"] = round((p.t_answer - p.t_sent) * 1000, 1)
        return res

    def detach(self, owner):
        """A call/page ended: its open tasks stay pending, their answers will report 'none' (→ Telegram)."""
        for p in self.pending.values():
            if p.owner is owner:
                p.deliver_late = None
                p.owner = None

    def summary(self):
        now = time.time()
        return [{"id": p.id, "age_s": int(now - p.created), "urgency": p.urgency, "route": p.route,
                 "live": p.deliver_late is not None, "parts": p.parts} for p in self.pending.values()]

    def _gc(self, max_age=86400):
        cut = time.time() - max_age
        for k in [k for k, p in self.pending.items() if p.created < cut]:
            self.pending.pop(k, None)
