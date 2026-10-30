"""Per-call log, local only (never sent anywhere): one JSON line per event in <state_dir>/calls/YYYY-MM-DD.jsonl
(dir 0700, files 0600; files older than KEEP_DAYS are pruned when a call starts). A call's lines all go to the file
of the day it started. Events (all carry ts, call):
  start{kind, caller, reason?, say?}  ring{outcome, ring_s}  pickup{heard, waited_ms}  opening{text}  him{text, closing}  gemini{text}  typed{text}  interrupted  reconnect
  tool{name, args, call_id, handoff?, route?, error?, lane?, ms?, fallback_from?}  (lane "fast": a quick read-only
  tool answered in the relay in ms; with error it fell back to an ask_assistant hand-off, logged as its own tool line)  result{handoff, outcome, text?}  tool_cancel{ids}
  late{handoff, delivered, text}  error{message}  end{reason, by, why?, duration_s, tools}
  idle{stage, why?, quiet_s?, pending?}: the closing flow (session.py + closing.py): ask = "anything else?" prompted,
  nudge = he thanked/said bye and the model stayed silent, asked = the model asked "anything else?" itself,
  bye = goodbye prompted, hangup = the relay hung up itself (no end_call from the model), closing_answer{cls} = his
  thanks / no / bye answered an "anything else?" and the model replied without a goodbye: goodbye prompted after
  closing_bye_s of quiet (why closing_silence)
  him{closing}: closing.line_class of that line (thanks | bye | no | other | none): a class for diagnosing endings
  without the transcript
  why (end): him, goodbye, nothing_else, thanks, closing_silence, asked_silence, idle_silence, pending_timeout,
  end_call, time_limit, error, relay, other (closing.end_why)
  reply{after_ms, tool?}: his end of speech (last loud inbound frame) → the first audio of the reply out
  (by: him, assistant (end_call), silence (fallback), time limit, error, relay)
  (lane "track": identify_track matched his call audio locally; error "retry: …" = it listened again)
  (lane "phone": navigate / open_on_phone ran `phone open`; args = kind/mode/target class only, route = opened |
  notified | telegram; never the place, URL or app name)
  track_retry{ms, status, hit, text}
  resumed: the call continues the last conversation (Gemini resumption handle); resume_fallback{trigger, waited_ms,
  ok, ms, replay_ms?}: the resumed session stayed mute (no audio/tool call after the opening or his words), reopened
  fresh in the same call; resume_handle{kept, why?}: at hang-up, kept for the next call or not (assistant_end |
  mute_resume)
him/gemini are Gemini's input/output audio transcriptions, buffered per speaker and written at turn boundaries.
`voice-relay calls [--last N]` prints the newest calls compactly."""
import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta

from . import closing

KEEP_DAYS = 14
MAX_TEXT = 2000
_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl$")


def calls_dir(cfg):
    return os.path.join(cfg.state_dir, "calls")


def prune(d, keep_days=KEEP_DAYS, today=None):
    cut = (today or datetime.now().date()) - timedelta(days=keep_days)
    try:
        names = os.listdir(d)
    except OSError:
        return
    for n in names:
        m = _NAME.match(n)
        if not m:
            continue
        try:
            if datetime.strptime(m.group(1), "%Y-%m-%d").date() < cut:
                os.remove(os.path.join(d, n))
        except (ValueError, OSError):
            pass


def _clip(s, n=MAX_TEXT):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n - 1] + "…"


ENDED_BY = {"hangup": "him", "assistant_hangup": "assistant", "time limit": "time limit", "provider ended": "error",
            "accept failed": "error", "page closed": "him", "app closed": "him", "app lost": "network",
            "relay stopped": "relay"}


def ended_by(reason):
    r = str(reason or "")
    return ENDED_BY.get(r) or ("error" if r.startswith("provider failed") else None)


class NullLog:
    id = None

    def text(self, who, t): pass
    def flush(self): pass
    def event(self, ev, **kw): pass
    def tool(self, name, args, call_id, **kw): pass
    def result(self, handoff, res): pass
    def end(self, reason, by=None, why=None): pass


class CallLog(NullLog):
    def __init__(self, cfg, kind, caller, clock=time.time, **extra):
        self.clock = clock
        self.dir = calls_dir(cfg)
        self.id = "c" + secrets.token_hex(4)
        self.t0 = clock()
        self.kind = kind
        self.path = None
        self.buf = {}            # who -> [first_ts, [fragments]]
        self.tools = 0
        self.ended = False
        try:
            os.makedirs(self.dir, mode=0o700, exist_ok=True)
            os.chmod(self.dir, 0o700)
            prune(self.dir)
            self.path = os.path.join(self.dir, datetime.fromtimestamp(self.t0).strftime("%Y-%m-%d") + ".jsonl")
        except OSError:
            self.path = None
        self.event("start", kind=kind, caller=caller, **extra)

    def _write(self, rec):
        if self.path is None:
            return
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.fchmod(fd, 0o600)
                os.write(fd, (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8"))
            finally:
                os.close(fd)
        except OSError:
            pass

    def event(self, ev, ts=None, **kw):
        if self.ended:
            return
        rec = {"ts": round(ts if ts is not None else self.clock(), 2), "call": self.id, "ev": ev}
        rec.update({k: v for k, v in kw.items() if v is not None})
        self._write(rec)

    def text(self, who, t):
        """Transcription fragments (Gemini streams them); joined per speaker until the next turn boundary."""
        if not t or self.ended:
            return
        b = self.buf.setdefault("him" if who == "user" else "gemini", [self.clock(), []])
        b[1].append(t)

    def flush(self):
        for who, (ts, parts) in sorted(self.buf.items(), key=lambda kv: kv[1][0]):
            text = _clip("".join(parts))
            if text:
                self.event(who, ts=ts, text=text, closing=closing.line_class(text) if who == "him" else None)
        self.buf = {}

    def tool(self, name, args, call_id, **kw):
        self.flush()
        self.tools += 1
        try:
            a = json.loads(json.dumps(args, ensure_ascii=False, default=str))
        except (TypeError, ValueError):
            a = {"_": _clip(args, 500)}
        self.event("tool", name=_clip(name, 80), args=a, call_id=_clip(call_id, 80), **kw)

    def result(self, handoff, res):
        res = res or {}
        outcome = "answer" if "answer" in res else res.get("status") or ("error" if "error" in res else "?")
        text = res.get("answer") or res.get("say") or res.get("error")
        self.event("result", handoff=handoff, outcome=outcome, text=_clip(text, 600) if text else None)

    def end(self, reason, by=None, why=None):
        """by: who ended it (him, assistant, silence, time limit, error, relay); derived from reason when not given.
        why: the end reason class (closing.end_why)."""
        if self.ended:
            return
        self.flush()
        self.event("end", reason=_clip(reason, 200), by=by or ended_by(reason), why=why,
                   duration_s=round(self.clock() - self.t0, 1), tools=self.tools)
        self.ended = True


# --- reading ---------------------------------------------------------------------------------------------

def load_calls(d, last=1):
    """The newest `last` calls as lists of events (oldest call first), reading day files newest first."""
    try:
        names = sorted((n for n in os.listdir(d) if _NAME.match(n)), reverse=True)
    except OSError:
        return []
    found = []                                  # newest first
    for n in names:
        order, calls = [], {}
        try:
            with open(os.path.join(d, n), encoding="utf-8") as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    cid = rec.get("call")
                    if cid not in calls:
                        calls[cid] = []
                        order.append(cid)
                    calls[cid].append(rec)
        except OSError:
            continue
        for cid in reversed(order):
            found.append(calls[cid])
            if len(found) >= last:
                return list(reversed(found))
    return list(reversed(found))


def _code(v, n=60):
    """A code word or class from the log (stage, why, lane, outcome, error class), clipped; None for anything else."""
    if isinstance(v, bool) or v is None:
        return None
    return v if isinstance(v, (int, float)) else _clip(v, n) if isinstance(v, str) else None


CLOSING_CLASSES = ("thanks", "bye", "no", "other", "none")
OUT_KINDS = ("tg-out", "app-out")       # the relay rang him (Telegram) / he answered an in-app ring
TALK_EVENTS = ("him", "gemini", "reply")  # any of them = there was talk (quality()'s "connected")


def call_outcome(kind, ring, end_reason, ended):
    """An outgoing call's outcome code and whether he answered, from the log's own facts (never a guess from words):
    tg-out = the ring event's outcome (answered | no_answer | declined | busy | failed); no ring but an end = it never
    rang (rate_limited | provider_failed | not_rung); no ring and no end = (None, None): ringing now or the relay
    stopped before the ring ended. app-out = answered (the relay logs an in-app ring only once he picked up; a missed
    in-app ring never reaches it). Incoming kinds = (None, None)."""
    if kind == "app-out":
        return "answered", True
    if kind != "tg-out":
        return None, None
    if ring is not None:
        oc = _code(ring, 20) or "?"
        return oc, oc == "answered"
    if not ended:
        return None, None
    r = str(end_reason or "")
    return ("rate_limited" if r == "rate limited" else "provider_failed" if r.startswith("provider failed")
            else "not_rung"), False


def digest(evs):
    """One call as structured fields only (no transcript, tool args, result text, reason or say): for readers outside
    the relay's container (hub mode: voice.status's result.calls, voice.calls). kind, start, end {reason, by, why,
    duration_s}, ring [outcome, ring_s] (outgoing Telegram calls), outcome + answered (call_outcome: outgoing calls
    only, else null), talked (him, the model or a reply logged), events per kind (him / gemini = transcript turns,
    counted), idle [[stage, why, quiet_s]], tools [[name, lane|route, error?, handoff?]], results [outcome], reply_ms
    [after_ms], closing [each him line's closing class in order: thanks | bye | no | other | none; null for a line
    logged before the class existed]."""
    out = {"id": None, "kind": None, "start": None, "end": None, "ring": None, "outcome": None, "answered": None,
           "talked": False, "events": {}, "idle": [], "tools": [], "results": [], "reply_ms": [], "closing": []}
    for e in evs:
        ev = str(e.get("ev") or "?")[:40]
        out["id"] = out["id"] or _code(e.get("call"), 20)
        out["events"][ev] = out["events"].get(ev, 0) + 1
        if ev == "start":
            out["kind"], out["start"] = _code(e.get("kind"), 20), e.get("ts")
        elif ev == "end":
            out["end"] = {k: _code(e.get(k), 40) for k in ("reason", "by", "why", "duration_s")}
        elif ev == "ring":
            out["ring"] = [_code(e.get("outcome"), 20) or "?", _code(e.get("ring_s"))]
        elif ev == "idle":
            out["idle"].append([_code(e.get("stage"), 20), _code(e.get("why"), 30), _code(e.get("quiet_s"))])
        elif ev == "tool":
            out["tools"].append([_code(e.get("name"), 40), _code(e.get("lane") or e.get("route"), 20),
                                 _code(e.get("error")), bool(e.get("handoff"))])
        elif ev == "result":
            out["results"].append(_code(e.get("outcome"), 20))
        elif ev == "reply" and isinstance(e.get("after_ms"), (int, float)):
            out["reply_ms"].append(round(e["after_ms"]))
        elif ev == "him":
            out["closing"].append(e.get("closing") if e.get("closing") in CLOSING_CLASSES else None)
        elif ev == "hangup":
            out["hangup"] = [_code(e.get("by"), 20), _code(e.get("why"), 30)]
    out["outcome"], out["answered"] = call_outcome(out["kind"], out["ring"][0] if out["ring"] else None,
                                                   (out["end"] or {}).get("reason"), out["end"] is not None)
    out["talked"] = any(out["events"].get(k) for k in TALK_EVENTS)
    return out


def recent(cfg, last=5, outgoing=False):
    """digest() of the newest `last` calls, newest last; outgoing = only the calls that rang him (OUT_KINDS)."""
    if not outgoing:
        return [digest(evs) for evs in load_calls(calls_dir(cfg), last=last)]
    ds = [d for d in (digest(evs) for evs in load_calls(calls_dir(cfg), last=100000)) if d["kind"] in OUT_KINDS]
    return ds[-last:] if last > 0 else []


def outgoing_summary(digests):
    """The outgoing calls among `digests`: {calls, answered, not_answered, open, by: {outcome: n}}; open = no outcome
    yet (ringing now, or the relay stopped before the ring ended)."""
    out = [d for d in digests if isinstance(d, dict) and d.get("kind") in OUT_KINDS]
    by = {}
    for d in out:
        k = d.get("outcome") or "open"
        by[k] = by.get(k, 0) + 1
    return {"calls": len(out), "answered": sum(d.get("answered") is True for d in out),
            "not_answered": sum(d.get("answered") is False for d in out),
            "open": sum(d.get("answered") is None for d in out), "by": by}


def stats(cfg, days=7, now=None):
    """quality() over the last `days` days (start time), for voice.calls: counts, timings and code-word tallies
    (kinds, ring outcomes, tool names, end reasons, closing classes); never any text."""
    now = time.time() if now is None else now
    q = quality(load_calls(calls_dir(cfg), 100000), since=now - days * 86400)
    q["days"] = days
    return q


def week(cfg, days=7, now=None):
    """quality() over the last `days` days as numbers only, for readers outside the relay's container (hub mode:
    voice.status's result.stats_7d, the dashboard's 7-day numbers): the counts, the timing distributions
    ({n, median, p90, max}) and the ring outcomes; the code-word tallies (kinds, tools, end reasons) stay here."""
    now = time.time() if now is None else now
    q = quality(load_calls(calls_dir(cfg), 100000), since=now - days * 86400)
    out = {k: v for k, v in q.items() if isinstance(v, int) and not isinstance(v, bool)}
    out.update({k: v for k, v in q.items() if isinstance(v, dict) and set(v) == {"n", "median", "p90", "max"}})
    out["ring"] = {str(k)[:20]: int(v) for k, v in q["ring"].items()}   # outcome codes: answered, declined, ...
    out["days"] = days
    return out


def _hms(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def _short(s, n=300):
    s = str(s)
    return s if len(s) <= n else s[:n - 1] + "…"


def _latency(evs):
    ms = sorted(int(e["after_ms"]) for e in evs if e["ev"] == "reply" and e.get("after_ms") is not None)
    if not ms:
        return None
    n_tool = sum(1 for e in evs if e["ev"] == "reply" and e.get("tool"))
    return ("  reply latency (his end of speech → first audio out): median %.1f s, max %.1f s, %d turn%s%s"
            % (ms[len(ms) // 2] / 1000.0, ms[-1] / 1000.0, len(ms), "" if len(ms) == 1 else "s",
               " (%d after a tool call)" % n_tool if n_tool else ""))


def format_call(evs, live=None):
    """live: the relay's live call ids (None = relay unreachable), to tell "still on" from "lost its end"."""
    start = next((e for e in evs if e["ev"] == "start"), evs[0])
    end = next((e for e in evs if e["ev"] == "end"), None)
    head = "%s · %s · %s" % (start.get("call"), start.get("kind", "?"),
                             datetime.fromtimestamp(start["ts"]).strftime("%a %d.%m %H:%M:%S"))
    if end:
        d = int(end.get("duration_s") or 0)
        head += " → %s (%d:%02d) · caller: %s · end: %s%s · tool calls: %s" % (
            _hms(end["ts"]), d // 60, d % 60, start.get("caller", "?"), end.get("reason", "?"),
            " (by %s%s)" % (end["by"], ", %s" % end["why"] if end.get("why") not in (None, end["by"]) else "")
            if end.get("by") else "",
            end.get("tools", 0))
    elif live is not None and start.get("call") in live:
        head += " · caller: %s · still on (live now)" % start.get("caller", "?")
    else:
        head += " · caller: %s · no end recorded (%s)" % (
            start.get("caller", "?"), "the relay is not running" if live is None else "the relay stopped mid-call")
    if start.get("reason"):
        head += "\n  reason: %s" % _short(start["reason"], 200)
    if start.get("say"):
        head += "\n  say: %s" % _short(start["say"], 200)
    out = [head]
    for e in evs:
        k, t = e["ev"], _hms(e["ts"])
        if k in ("him", "gemini", "typed"):
            out.append("  %s %s: %s%s" % (t, k, _short(e.get("text", "")),
                                          " [%s]" % e["closing"] if k == "him" and e.get("closing") else ""))
        elif k == "tool":
            a = e.get("args") or {}
            task = a.get("task") if isinstance(a, dict) else None
            extra = ", ".join("%s=%s" % (x, a[x]) for x in sorted(a) if x != "task") if isinstance(a, dict) else ""
            line = "  %s tool %s" % (t, e.get("name"))
            if e.get("lane") == "fast":
                line += " (fast lane, %s ms)" % e.get("ms", "?")
            elif e.get("lane") == "local":
                line += " (local)"
            elif e.get("lane") == "track":
                line += " (track id, %s ms)" % e.get("ms", "?")
            elif e.get("lane") == "phone":
                line += " (phone open, %s ms → %s)" % (e.get("ms", "?"), e.get("route") or "nothing")
            if e.get("fallback_from"):
                line += " (fallback for %s)" % e["fallback_from"]
            if e.get("handoff"):
                line += " → %s (%s)" % (e["handoff"], e.get("route", "?"))
            if extra:
                line += " [%s]" % extra
            if e.get("error"):
                line += " error: %s" % e["error"]
            if task:
                line += ': "%s"' % _short(task, 300)
            out.append(line)
        elif k == "result":
            out.append("  %s result %s: %s%s" % (t, e.get("handoff") or "-", e.get("outcome"),
                                                 (' "%s"' % _short(e["text"], 200)) if e.get("text") else ""))
        elif k == "late":
            out.append("  %s late answer %s (%s): \"%s\"" % (t, e.get("handoff"), e.get("delivered"),
                                                            _short(e.get("text", ""), 200)))
        elif k == "track_retry":
            out.append("  %s track id retry (%s ms): \"%s\"" % (t, e.get("ms", "?"), _short(e.get("text", ""), 200)))
        elif k == "tool_cancel":
            out.append("  %s tool call cancelled by Gemini: %s" % (t, ", ".join(e.get("ids") or [])))
        elif k == "error":
            out.append("  %s error: %s" % (t, e.get("message", "")))
        elif k == "ring":
            out.append("  %s ring: %s after %ss" % (t, e.get("outcome"), e.get("ring_s")))
        elif k == "deck":
            out.append("  %s decision deck loaded (%s items, via %s)" % (t, e.get("items"), e.get("via")))
        elif k == "opening":
            out.append("  %s opening prompt sent" % t)
        elif k == "answer":
            out.append("  %s answered: provider ready %.1f s after his ring, line up %.1f s later" % (
                t, (e.get("provider_ms") or 0) / 1000.0, (e.get("connect_ms") or 0) / 1000.0))
        elif k == "greet":
            if e.get("via") == "skipped":
                out.append("  %s pick-up line skipped: he spoke first" % t)
            elif e.get("inbound"):
                out.append("  %s pick-up line (%s, %s ms) %.1f s after the line was up" % (
                    t, e.get("via"), e.get("clip_ms"), (e.get("first_audio_ms") or 0) / 1000.0))
            else:
                out.append("  %s greeting: first audio %.1f s after pick-up" % (t, (e.get("first_audio_ms") or 0) / 1000.0))
        elif k == "greet_cut":
            out.append("  %s pick-up line cut: he talked over it" % t)
        elif k == "audio_in":
            out.append("  %s his audio: %.1f s in, %.1f s voiced%s, %.1f s sent to the model" % (
                t, (e.get("ms") or 0) / 1000.0, (e.get("voiced_ms") or 0) / 1000.0,
                " (first after %.1f s)" % (e["first_voice_ms"] / 1000.0) if e.get("first_voice_ms") is not None else "",
                (e.get("sent_ms") or 0) / 1000.0))
        elif k == "pickup":
            out.append("  %s picked up: %s (opening after %.1f s)" % (
                t, "he said hello" if e.get("heard") else "silent", (e.get("waited_ms") or 0) / 1000.0))
        elif k == "idle":
            out.append("  %s closing: %s%s%s" % (t, e.get("stage"), " (%s)" % e["why"] if e.get("why") else "",
                                                ", quiet %ss" % e["quiet_s"] if e.get("quiet_s") is not None else ""))
        elif k in ("interrupted", "reconnect", "resumed"):
            out.append("  %s (%s)" % (t, k))
        elif k == "resume_fallback":
            out.append("  %s resumed session stayed mute after %s: %s" % (
                t, "his words" if e.get("trigger") == "him" else "the opening", "reopened fresh in %.1f s" % ((e.get("ms") or 0) / 1000.0) if e.get("ok")
                else "reopen failed"))
        elif k == "link":
            out.append("  %s phone line %s%s" % (t, e.get("state"), " after %.1f s" % ((e.get("gap_ms") or 0) / 1000.0)
                                                 if e.get("state") == "back" else ""))
    lat = _latency(evs)
    if lat:
        out.append(lat)
    return "\n".join(out)


def print_calls(cfg, last=1, out=print, live=None, outgoing=False, as_json=False):
    """The local log in full (inside claude-hub-voice); outgoing = only the calls that rang him; as_json = the
    digests as voice.calls hands them out (calls, outgoing, live)."""
    last = max(1, int(last))
    if outgoing:
        calls = [c for c in load_calls(calls_dir(cfg), 100000) if c and c[0].get("kind") in OUT_KINDS][-last:]
    else:
        calls = load_calls(calls_dir(cfg), last)
    if as_json:
        ds = [digest(c) for c in calls]
        out(json.dumps({"calls": ds, "outgoing": outgoing_summary(ds), "live": live}))
        return 0
    if not calls:
        out("no calls logged (%s)" % calls_dir(cfg))
        return 1
    out("\n\n".join(format_call(c, live) for c in calls))
    return 0


ANSWER_WORDS = {"answered": "answered", "no_answer": "NOT ANSWERED (no answer)", "declined": "NOT ANSWERED (declined)",
                "busy": "NOT ANSWERED (busy)", "failed": "NOT ANSWERED (ring failed)",
                "rate_limited": "NOT RUNG (hourly limit)", "provider_failed": "NOT RUNG (voice provider did not start)",
                "not_rung": "NOT RUNG"}


def _mmss(s):
    s = int(s or 0)
    return "%d:%02d" % (s // 60, s % 60)


def format_digest(d, live=None):
    """One call from its digest (what the hub hands out: metadata only, never words, reasons or tool args).
    live = the relay's live call ids (None = unknown)."""
    st = d.get("start")
    when = datetime.fromtimestamp(st).strftime("%a %d.%m %H:%M:%S") if isinstance(st, (int, float)) else "?"
    head = ["%s · %s · %s" % (d.get("id") or "?", d.get("kind") or "?", when)]
    ring = d.get("ring") or [None, None]
    if d.get("kind") in OUT_KINDS:
        oc = d.get("outcome")
        word = ANSWER_WORDS.get(oc, "outcome %s" % oc) if oc else "no outcome yet"
        if ring[1] is not None:
            word += " after %ss ringing" % ring[1]
        head.append(word)
    end = d.get("end")
    ev = d.get("events") or {}
    if d.get("talked"):
        head.append("talk: him %d, assistant %d turn%s" % (ev.get("him", 0), ev.get("gemini", 0),
                                                         "" if ev.get("gemini", 0) == 1 else "s"))
    if end:
        by = end.get("by")
        why = end.get("why")
        head.append("%s · end: %s%s" % (_mmss(end.get("duration_s")), end.get("reason") or "?",
                                        " (by %s%s)" % (by, ", %s" % why if why not in (None, by) else "") if by else
                                        " (%s)" % why if why else ""))
    elif live is not None and d.get("id") in live:
        head.append("still on (live now)")
    else:
        head.append("no end recorded%s" % ("" if live is None else " (the relay stopped mid-call)"))
    lines = [" · ".join(head)]
    tools = d.get("tools") or []
    if tools:
        lines.append("  tools: " + ", ".join(
            "%s%s%s" % (t[0], " (%s)" % t[1] if len(t) > 1 and t[1] else "", " error" if len(t) > 2 and t[2] else "")
            for t in tools))
    if d.get("idle"):
        lines.append("  closing: " + ", ".join("%s%s" % (i[0], " (%s)" % i[1] if i[1] else "") for i in d["idle"]))
    ms = sorted(int(x) for x in d.get("reply_ms") or [] if isinstance(x, (int, float)))
    if ms:
        lines.append("  reply latency: median %.1f s, max %.1f s, %d turn%s" % (
            ms[len(ms) // 2] / 1000.0, ms[-1] / 1000.0, len(ms), "" if len(ms) == 1 else "s"))
    return "\n".join(lines)


def format_outgoing(s):
    """outgoing_summary() as one line."""
    if not s or not s.get("calls"):
        return "outgoing: none in this list"
    rest = ", ".join("%s %d" % (k, v) for k, v in sorted(s.get("by", {}).items(), key=lambda kv: -kv[1])
                     if k not in ("answered", "open"))
    return "outgoing (rang him): %d of %d answered, %d not answered%s%s" % (
        s.get("answered", 0), s["calls"], s.get("not_answered", 0), " (%s)" % rest if rest else "",
        ", %d without an outcome yet" % s["open"] if s.get("open") else "")


# --- quality numbers (voice-relay stats) -----------------------------------------------------------------------

def _pct(xs, p):
    xs = sorted(xs)
    if not xs:
        return None
    return xs[min(len(xs) - 1, int(round(p / 100.0 * (len(xs) - 1))))]


def _dist(xs):
    xs = [int(x) for x in xs if x is not None]
    return {"n": len(xs), "median": _pct(xs, 50), "p90": _pct(xs, 90), "max": max(xs) if xs else None}


def quality(calls, since=None):
    """Counts and timings over call logs (lists of events): never any text. since = epoch seconds (start ts)."""
    from collections import Counter
    calls = [c for c in calls if c and (since is None or c[0].get("ts", 0) >= since)]
    q = {"calls": len(calls), "kinds": Counter(), "ring": Counter(), "connected": 0, "duration_s": [],
         "pickup_waited_ms": [], "pickup_heard": 0, "greet_ms": [], "greet_prefetch": 0, "greet_in_ms": [],
         "greet_in_via": Counter(), "greet_cut": 0, "answer_provider_ms": [], "answer_connect_ms": [],
         "no_voice_in": 0, "not_sent": 0, "reply_ms": [],
         "reply_tool_ms": [], "tools": Counter(), "fast_hits": 0, "fast_fallbacks": 0, "tool_errors": 0,
         "handoffs": 0, "answered_in_call": 0, "slow": 0, "late_live": 0, "unanswered_in_call": 0, "handoff_ms": [],
         "interrupted": 0, "errors": 0, "model_fallbacks": 0, "reconnect_ok": 0, "reconnect_failed": 0,
         "go_away": 0, "link_lost": 0, "link_back": 0, "link_gap_ms": [], "resumed": 0, "resume_fallback_ok": 0,
         "resume_fallback_failed": 0, "end_reason": Counter(), "end_by": Counter(), "end_why": Counter(),
         "out_calls": 0, "out_answered": 0, "out_not_answered": 0}
    for evs in calls:
        start = evs[0]
        q["kinds"][start.get("kind", "?")] += 1
        if start.get("kind") in OUT_KINDS:          # the calls that rang him: answered or not (call_outcome)
            ring = next((e for e in evs if e.get("ev") == "ring"), None)
            end = next((e for e in evs if e.get("ev") == "end"), None)
            _oc, ans = call_outcome(start.get("kind"), (_code(ring.get("outcome"), 20) or "?") if ring else None,
                                    (end or {}).get("reason"), end is not None)
            q["out_calls"] += 1
            q["out_answered"] += ans is True
            q["out_not_answered"] += ans is False
        talked = False
        asked = {}                                  # handoff id -> tool ts
        done = set()
        for e in evs:
            k = e.get("ev")
            if k == "ring":
                q["ring"][e.get("outcome", "?")] += 1
            elif k in ("him", "gemini", "reply"):
                talked = True
                if k == "reply" and e.get("after_ms") is not None:
                    q["reply_tool_ms" if e.get("tool") else "reply_ms"].append(e["after_ms"])
            elif k == "pickup":
                q["pickup_waited_ms"].append(e.get("waited_ms"))
                q["pickup_heard"] += bool(e.get("heard"))
            elif k == "greet" and e.get("inbound"):
                q["greet_in_via"][e.get("via") or "?"] += 1
                if e.get("first_audio_ms") is not None:
                    q["greet_in_ms"].append(e["first_audio_ms"])
            elif k == "greet":
                q["greet_ms"].append(e.get("first_audio_ms"))
                q["greet_prefetch"] += bool(e.get("prefetch"))
            elif k == "greet_cut":
                q["greet_cut"] += 1
            elif k == "answer":
                q["answer_provider_ms"].append(e.get("provider_ms"))
                q["answer_connect_ms"].append(e.get("connect_ms"))
            elif k == "audio_in":
                q["no_voice_in"] += (e.get("ms") or 0) >= 2000 and not e.get("voiced_ms")
                q["not_sent"] += (e.get("ms") or 0) >= 2000 and not e.get("sent_ms")
            elif k == "tool":
                name = str(e.get("name") or "?")
                q["tools"][name] += 1
                if e.get("lane") == "fast":
                    q["fast_hits" if not e.get("error") else "fast_fallbacks"] += 1
                elif e.get("error"):
                    q["tool_errors"] += 1
                if e.get("handoff"):
                    q["handoffs"] += 1
                    asked[e["handoff"]] = e["ts"]
            elif k == "handoff_ms":
                if e.get("outcome") == "slow":
                    q["slow"] += 1
            elif k == "result" and e.get("handoff") in asked:
                if e.get("outcome") == "answer" and e["handoff"] not in done:
                    done.add(e["handoff"])
                    q["answered_in_call"] += 1
                    q["handoff_ms"].append(round((e["ts"] - asked[e["handoff"]]) * 1000))
            elif (k == "late" and e.get("handoff") in asked and not e.get("more")
                  and e.get("delivered") != "telegram"):
                if e["handoff"] not in done:
                    done.add(e["handoff"])
                    q["late_live"] += 1
                    q["handoff_ms"].append(e.get("ms") or round((e["ts"] - asked[e["handoff"]]) * 1000))
            elif k == "interrupted":
                q["interrupted"] += 1
            elif k == "error":
                q["errors"] += 1
            elif k == "model_fallback":
                q["model_fallbacks"] += 1
            elif k == "resumed":
                q["resumed"] += 1
            elif k == "resume_fallback":
                q["resume_fallback_ok" if e.get("ok") else "resume_fallback_failed"] += 1
            elif k == "link":
                if e.get("state") == "lost":
                    q["link_lost"] += 1
                elif e.get("state") == "back":
                    q["link_back"] += 1
                    q["link_gap_ms"].append(e.get("gap_ms"))
            elif k == "reconnect":
                if e.get("why") == "dropped":
                    q["reconnect_ok" if e.get("ok") else "reconnect_failed"] += 1
                else:
                    q["go_away"] += 1
            elif k == "end":
                q["end_reason"][str(e.get("reason") or "?")[:40]] += 1
                if e.get("by"):
                    q["end_by"][e["by"]] += 1
                if e.get("why"):
                    q["end_why"][e["why"]] += 1
                if talked:
                    q["duration_s"].append(e.get("duration_s"))
        q["connected"] += talked
        q["unanswered_in_call"] += len(set(asked) - done)
    for key in ("duration_s", "pickup_waited_ms", "greet_ms", "greet_in_ms", "answer_provider_ms", "answer_connect_ms",
                "reply_ms", "reply_tool_ms", "handoff_ms", "link_gap_ms"):
        q[key] = _dist(q[key])
    for key in ("kinds", "ring", "tools", "greet_in_via", "end_reason", "end_by", "end_why"):
        q[key] = dict(q[key].most_common())
    return q


def format_quality(q, days=None):
    d = lambda x: ("median %s, p90 %s, max %s (n=%d)" % (x["median"], x["p90"], x["max"], x["n"])) if x["n"] else "-"
    kv = lambda c: ", ".join("%s %s" % (k, v) for k, v in c.items()) or "-"
    return "\n".join([
        "voice call quality%s: %d calls (%s), %d with talk; ring: %s" % (
            " (last %s days)" % days if days else "", q["calls"], kv(q["kinds"]), q["connected"], kv(q["ring"])),
        "  outgoing (rang him): %d, answered %d, not answered %d" % (
            q.get("out_calls", 0), q.get("out_answered", 0), q.get("out_not_answered", 0)),
        "  talk time s: %s" % d(q["duration_s"]),
        "  pick-up hold ms: %s; he said hello in %d" % (d(q["pickup_waited_ms"]), q["pickup_heard"]),
        "  pick-up → first audio ms: %s; prefetched %d" % (d(q["greet_ms"]), q["greet_prefetch"]),
        "  his calls: line up → pick-up line ms %s (%s; cut by him %d); his ring → provider ready ms %s; "
        "accept → line up ms %s" % (d(q["greet_in_ms"]), kv(q["greet_in_via"]), q["greet_cut"],
                                     d(q["answer_provider_ms"]), d(q["answer_connect_ms"])),
        "  his audio: calls with no voice heard %d, with his audio not reaching the model %d" % (
            q["no_voice_in"], q["not_sent"]),
        "  reply ms (his end of speech → audio): %s" % d(q["reply_ms"]),
        "  reply ms after a tool call: %s" % d(q["reply_tool_ms"]),
        "  tools: %s" % kv(q["tools"]),
        "  fast lane: %d hits, %d fell back to a hand-off; tool errors %d" % (
            q["fast_hits"], q["fast_fallbacks"], q["tool_errors"]),
        "  hand-offs: %d; answered in the call %d, late but live %d, slow %d, never in the call %d" % (
            q["handoffs"], q["answered_in_call"], q["late_live"], q["slow"], q["unanswered_in_call"]),
        "  hand-off → answer ms: %s" % d(q["handoff_ms"]),
        "  barge-ins %d; errors %d; model fallbacks %d; reconnects ok %d / failed %d; goAway %d" % (
            q["interrupted"], q["errors"], q["model_fallbacks"], q["reconnect_ok"], q["reconnect_failed"],
            q["go_away"]),
        "  app line drops %d, resumed %d; gap ms %s" % (q["link_lost"], q["link_back"], d(q["link_gap_ms"])),
        "  calls continuing the last one %d; mute resumes reopened fresh %d / failed %d" % (
            q["resumed"], q["resume_fallback_ok"], q["resume_fallback_failed"]),
        "  ends: %s; by: %s; why: %s" % (kv(q["end_reason"]), kv(q["end_by"]), kv(q["end_why"])),
    ])
