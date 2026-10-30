"""Decision call: go through the owner's open either/or items ("Waiting on you" in TODO.md) by voice.

The deck comes from `decision-deck --json` (cfg.decision_deck_cmd): short spoken prompts with no identifiers or
amounts. Gemini gets only each item's id, question and options. It records his answer per item with the local
tool record_pick; nothing is applied: after the call ONE ~/inbox/voice-decisions-*.md note lists the picks and
the main session sends him one Telegram summary with a ✅ button per item. A spoken pick is never an approval.

Start: `voice-relay call --reason decisions` (the deck is loaded before ringing and goes into the opening), or he
says "decisions" on any call (Gemini calls decision_deck).
"""
import asyncio
import json
import os
import re
import shlex
import time
from datetime import datetime

from .answer_filter import filter_answer

TRIGGER = re.compile(r"(?i)^\s*(?:my |the |open )*(?:decisions?|entscheidungen?)\s*[.!]?\s*$")
MAX_CHOICE = 200
PICK_SAY = "Noted, not applied: it waits for his ✅ on Telegram."
END_SAY = ("That was the last one. Tell him a summary with a ✅ button per item comes on Telegram; "
           "nothing changes before he taps it.")

TOOLS = [
    {"name": "decision_deck",
     "description": ("His open either/or decisions from the assistant's to-do board, for a decision round. Call it "
                     "when he says 'decisions' (or 'Entscheidungen'). Returns the items: go through them one at a "
                     "time and record each answer with record_pick. Local and read-only."),
     "parameters": {"type": "object", "properties": {}}},
    {"name": "record_pick",
     "description": ("Record his spoken answer to one decision item from decision_deck. This is NOT an approval and "
                     "changes nothing: he confirms each pick with ✅ on Telegram after the call. Returns the next "
                     "item, or says that was the last one."),
     "parameters": {"type": "object", "properties": {
         "item": {"type": "string", "description": "The item id from decision_deck."},
         "choice": {"type": "string", "description": "His answer in a few words: one of the item's options, his "
                                                     "own words, or 'skip' when he wants to leave it."}},
         "required": ["item", "choice"]}},
]
NAMES = frozenset(t["name"] for t in TOOLS)


class DeckError(Exception):
    pass


def is_trigger(reason):
    return bool(TRIGGER.match(str(reason or "")))


def _clean(text, cfg, n):
    t = " ".join(str(text or "").split())[:n]
    return filter_answer(t, getattr(cfg, "mask_amounts", True)).text if t else ""


def parse_deck(raw, cfg):
    """decision-deck JSON → the deck kept here: [{id, title, say, options}] (say/options filtered for speech)."""
    try:
        d = json.loads(raw)
    except (TypeError, ValueError):
        raise DeckError("deck is not JSON") from None
    items = []
    for it in (d.get("items") if isinstance(d, dict) else None) or []:
        if not isinstance(it, dict) or not it.get("id") or not it.get("say"):
            continue
        items.append({"id": str(it["id"])[:40], "title": str(it.get("title") or it["id"])[:120],
                      "say": _clean(it["say"], cfg, 200),
                      "options": [_clean(o, cfg, 60) for o in (it.get("options") or [])[:4] if str(o).strip()]})
    return {"items": items}


RUNNER = None  # hub mode (hubmode.install): the deck through the gateway's voice.lookup


async def load_deck(cfg, run=None):
    """Run the deck command (token-free, local, read-only) under a timeout; returns parse_deck(...)."""
    argv = shlex.split(cfg.decision_deck_cmd)
    run = run or RUNNER
    if run is not None:
        return parse_deck(await run(argv), cfg)
    try:
        p = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL,
                                                 stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    except OSError as e:
        raise DeckError("deck command: %s" % type(e).__name__) from None
    try:
        out, _ = await asyncio.wait_for(p.communicate(), cfg.decision_timeout_s)
    except asyncio.TimeoutError:
        p.kill()
        await p.wait()
        raise DeckError("deck command timed out") from None
    if p.returncode != 0:
        raise DeckError("deck command exit %s" % p.returncode)
    return parse_deck(out.decode("utf-8", "replace"), cfg)


def for_gemini(item):
    return {"id": item["id"], "question": item["say"], "options": item["options"]}


def deck_answer(deck):
    items = deck["items"]
    if not items:
        return {"answer": "There are no open decisions right now."}
    return {"answer": "%d open decision%s. Go through them one at a time: ask each question, then record his "
                      "answer with record_pick. Nothing is applied until he taps ✅ on Telegram."
                      % (len(items), "" if len(items) == 1 else "s"),
            "items": [for_gemini(it) for it in items]}


def opening(deck, heard=""):
    """The [outbound call] prompt for `voice-relay call --reason decisions`: the deck is the opening context.
    heard: what he did on pick-up (tgcall.HEARD), so the greeting answers his hello instead of talking over it."""
    lines = "\n".join("- %s: %s (options: %s)" % (it["id"], it["say"], " | ".join(it["options"]) or "his words")
                      for it in deck["items"])
    n = len(deck["items"])
    return ("[outbound call] Your assistant called him for a decision round and he just picked up. %sGreet him "
            "once, in one short sentence like: \"Hi, it's Claude, I'm calling about %d open decision%s; it takes a "
            "minute.\" If he talks over it, don't start it again: answer him and carry on. Then go "
            "through these one at a time, in order: ask the question, wait for his answer, call record_pick with "
            "the id and his choice ('skip' if he wants to leave it), then the next. Nothing is applied until he "
            "taps ✅ on Telegram; never say it is done. If he wants something else, help as usual.\n%s"
            % (heard + " " if heard else "", n, "" if n == 1 else "s", lines))


class Round:
    """The decision round of one call: the deck (when loaded) and his picks (last answer per item wins)."""
    def __init__(self, cfg, loader=None):
        self.cfg, self.loader = cfg, loader or (lambda: load_deck(cfg))
        self.deck, self.via, self.picks = None, None, {}

    def set_deck(self, deck, via):
        self.deck, self.via = deck, via

    async def ensure(self, log=None):
        if self.deck is None:
            self.set_deck(await self.loader(), "tool")
            if log is not None:
                log.event("deck", items=len(self.deck["items"]), via="tool")
        return self.deck

    def resolve(self, ref):
        items = (self.deck or {}).get("items") or []
        r = str(ref or "").strip().lower()
        for it in items:
            if it["id"].lower() == r:
                return it
        if r.isdigit() and 1 <= int(r) <= len(items):
            return items[int(r) - 1]
        hits = [it for it in items if r and (r in it["id"].lower() or r in it["title"].lower())]
        return hits[0] if len(hits) == 1 else None

    def record(self, ref, choice):
        it = self.resolve(ref)
        if it is None:
            return None, {"error": "unknown item; use an id from decision_deck"}
        c = _clean(choice, self.cfg, MAX_CHOICE)
        if not c:
            return None, {"error": "empty choice"}
        self.picks.pop(it["id"], None)
        self.picks[it["id"]] = {"choice": c, "ts": time.time()}
        left = [x for x in self.deck["items"] if x["id"] not in self.picks]
        res = {"answer": PICK_SAY, "recorded": {"item": it["id"], "choice": c}}
        if left:
            res["next"] = for_gemini(left[0])
        else:
            res["done"] = END_SAY
        return it, res


def render_note(rnd, call_id=None, kind=None, t0=None, t1=None, reason=None):
    span = datetime.fromtimestamp(t0).strftime("%d.%m %H:%M") if t0 else ""
    if t0 and t1:
        span += "–" + datetime.fromtimestamp(t1).strftime("%H:%M")
    meta = ", ".join(x for x in ("call %s" % call_id if call_id else "", kind or "", span,
                                 "ended: %s" % reason if reason else "") if x)
    items = {it["id"]: it for it in (rnd.deck or {}).get("items") or []}
    out = ["# Voice decisions\n" + meta, "",
           "Status note from the relay, not his approval: these are his spoken picks from a decision round. "
           "A spoken pick is never an approval. Send him ONE Telegram summary with a ✅ button per item (skip what "
           "he already settled on Telegram since the call; 'skip' picks: leave the item as it is), and apply an "
           "item only after he taps its ✅. Delete this note once the summary is sent. Full call: "
           "`voice-relay calls`. (Other ~/inbox/voice-*.md notes, not voice-recap-* or voice-decisions-*, are his "
           "own spoken requests.)", ""]
    for iid, p in rnd.picks.items():
        it = items.get(iid, {"title": iid, "say": ""})
        out.append('- %s · "%s" · asked: "%s" · pick: "%s"' % (iid, it["title"], it["say"], p["choice"]))
    open_ = [it for iid, it in items.items() if iid not in rnd.picks]
    if open_:
        out += ["", "Not answered in the call (stay on the board): " + ", ".join(it["title"] for it in open_)]
    return "\n".join(out) + "\n"


def write_note(inbox_dir, call_id, text, now=None):
    from .recap import write
    return write(inbox_dir, call_id, text, now=now, prefix="voice-decisions")
