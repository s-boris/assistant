"""Open on his phone from inside a call: navigation, a link, an SBB connection or an app (`phone open`).

Opening something on his own phone is approved as a class, so there is no Telegram confirmation: these tools only
OPEN things. Anything that sends, buys, books or deletes stays an ask_assistant [action].

  navigate {place, mode?, event?}  -> phone open --nav PLACE --mode d|r|w|b --label L --wait W --via voice --json
      place "home" passes through (the phone CLI resolves it from the private profile); place "next_event" = the
      next located event on his calendar (calcache: its town; `event` = title words that pick among them), so the
      voice model never needs an address.
  open_on_phone {what: link|sbb|app, url?, to?, from?, at?, arrive?, app?}
      link -> phone open URL (https only) · sbb -> phone open --sbb FROM TO [--at HH:MM | --arrive HH:MM]
      (FROM default "here"; arrive = be there by, wins over at)
      app  -> phone open --app NAME
The CLI runs as a subprocess under a hard timeout (cfg.phone_open_timeout_s), never through the main session. The
tool result is {"status": opened | notified | telegram | error | no_event, "say": one short spoken line} (no URLs,
long numbers or addresses in "say"). Logging: the call log gets kind/mode/target class/route/ms only (log_args), the
usage store voice.open.<nav|link|sbb|app> with outcome = the route (or the error class). Off: VOICE_PHONE_OPEN=0.
"""
import asyncio
import importlib.machinery
import importlib.util
import json
import os
import re
import signal
import time
from datetime import datetime, timedelta

MODES = {"drive": "d", "transit": "r", "walk": "w", "bike": "b"}
WHATS = ("link", "sbb", "app")
CLI_KIND = {"navigate": "nav", "link": "url", "sbb": "sbb", "app": "app"}
USAGE_KIND = {"nav": "nav", "url": "link", "sbb": "sbb", "app": "app"}
ROUTES = ("opened", "notified", "telegram")
EXIT_ERR = {2: "usage", 4: "offline", 5: "timeout", 6: "refused", 7: "gateway_down"}
NEXT_EVENT = re.compile(r"(?i)^\s*(?:the |my )?(?:next[ _]?event|next[ _]appointment|next[ _]meeting|"
                        r"my next appointment|next termin|nächste[rn]? termin)\s*$")
HHMM = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")
URL_OK = re.compile(r"^https://[^\s<>\"'`]{3,2000}$")
EVENT_LOOKAHEAD_H = 24.0                 # "next event": the next located one starting within this
EVENT_LATE_MIN = 20                      # ...or that started at most this long ago (he is on his way, late)

NAV_TOOL = {
    "name": "navigate",
    "description": ("Start navigation on his phone (Google Maps opens with the route): 'navigate me to X', 'take me "
                    "to X', 'route to X', 'bring mich zu X', 'navigier mich nach X'. Opening on his phone is approved: "
                    "no Telegram confirmation. place: the destination as he named it (a venue, shop, town or place "
                    "name), 'home' for home, or 'next_event' for his next calendar appointment (the place comes from "
                    "his calendar: never ask him for an address). mode: drive (default), transit, walk or bike. "
                    "Returns status and a short line to say."),
    "parameters": {"type": "object", "properties": {
        "place": {"type": "string", "description": "Where to: his words, 'home', or 'next_event'."},
        "mode": {"type": "string", "enum": list(MODES), "description": "How he travels; default drive."},
        "event": {"type": "string", "description": "Only with place next_event: a few title words of the "
                                                   "appointment when one was named in this call."}},
        "required": ["place"]},
}
OPEN_TOOL = {
    "name": "open_on_phone",
    "description": ("Open something on his phone right away (approved, no Telegram confirmation; it only opens, "
                    "nothing is sent, bought or booked). what=link: an https link that came up in this conversation "
                    "(url). what=sbb: an SBB connection in the SBB app (to, optional from = where he is by default, "
                    "optional at = departure HH:MM, or arrive = arrive-by HH:MM when he must be there by a time). "
                    "what=app: launch an installed app by its name (app, e.g. "
                    "Spotify). Returns status and a short line to say."),
    "parameters": {"type": "object", "properties": {
        "what": {"type": "string", "enum": list(WHATS)},
        "url": {"type": "string", "description": "what=link: the https URL exactly as it came up in this call."},
        "to": {"type": "string", "description": "what=sbb: destination station or town."},
        "from": {"type": "string", "description": "what=sbb: start station; leave out for where he is now."},
        "at": {"type": "string", "description": "what=sbb: departure time HH:MM; leave out for now."},
        "arrive": {"type": "string", "description": "what=sbb: arrive-by time HH:MM (he must be there by then); "
                                                    "use instead of at."},
        "app": {"type": "string", "description": "what=app: the app's name."}},
        "required": ["what"]},
}
TOOLS = [NAV_TOOL, OPEN_TOOL]
NAMES = frozenset(t["name"] for t in TOOLS)

OFFER = " Want me to hand it to the assistant?"
SAY = {
    "opened": {"nav": "Starting navigation to {dest}.", "nav_home": "Starting navigation home.",
               "nav_plain": "Navigation is starting on your phone.", "url": "The link is open on your phone.",
               "sbb": "The SBB connection to {dest} is open on your phone.",
               "sbb_plain": "The SBB connection is open on your phone.", "app": "{dest} is open on your phone.",
               "sbb_arrive": "The SBB connections to {dest} arriving by {at} are open on your phone.",
               "app_plain": "The app is open on your phone."},
    "notified": "It's on your phone as a notification, tap it.",
    "telegram": {"nav": "Your phone app isn't reachable, so I put the route on Telegram.",
                 "other": "Your phone app isn't reachable, so I put the link on Telegram."},
    "error": {"refused_url": "Your phone refused that link.", "refused_app": "I couldn't find that app on your phone.",
              "refused": "Your phone refused that.", "offline": "Your phone isn't reachable right now.",
              "timeout": "Your phone didn't answer in time.", "gateway_down": "The link to your phone is down right now.",
              "other": "Opening that on your phone didn't work."},
    "bad": {"no_place": "Where to? Tell me the place.", "no_url": "I don't have a link for that from this call.",
            "no_to": "Where to? Tell me the station or town.", "no_app": "Which app?"},
}
EVENT_SAY = {"stale": "I can't read your calendar right now.",
             "none": "There's no appointment with a place on your calendar in the next day.",
             "no_place": "Your next appointment has no place in the calendar."}
_UNSAFE = re.compile(r"(?i)(\d|https?|www\.|[/@:#<>\\]|\w*(strasse|straße|gasse)\b|\b\w*(weg|platz|allee)\b|"
                     r"\b(street|road|avenue|rue|chemin|str)\b\.?)")


def speakable(text, n=40):
    """A destination/app name fit to be said: short, no digits, links, '@' or street words; else ''."""
    t = " ".join(str(text or "").split()).strip(" .,;")
    if not t or len(t) > n or _UNSAFE.search(t):
        return ""
    return t


def _arg(text, n):
    """A free-text CLI argument: one line, bounded, never read as an option."""
    return " ".join(str(text or "").split())[:n].lstrip("-").strip()


def _target(place):
    p = str(place or "").strip().lower()
    return "home" if p == "home" else "event" if NEXT_EVENT.match(p) else "place"


def _load_usage_log():
    """~/bin/lib/usage.py's log() under a private module name (no sys.path change); None if unavailable."""
    path = os.path.join(os.path.expanduser("~"), "bin", "lib", "usage.py")
    try:
        loader = importlib.machinery.SourceFileLoader("voice_usage_lib", path)
        spec = importlib.util.spec_from_loader("voice_usage_lib", loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        return mod.log
    except Exception:
        return None


CMD_RUNNER = None  # hub mode (hubmode.install): `phone open` argv -> the gateway's phone.open

class PhoneOpen:
    """One per call. run(argv, timeout) -> (exit code | None on timeout, stdout text); now() -> aware datetime."""

    def __init__(self, cfg, run=None, now=None, ulog=None):
        self.cfg = cfg
        self.run = run or CMD_RUNNER or self._run
        self.now = now or (lambda: datetime.now().astimezone())
        self._ulog = ulog

    # -- plumbing ------------------------------------------------------------------------------------------------
    async def _run(self, argv, timeout):
        env = dict(os.environ)
        env["PATH"] = os.pathsep.join([self.cfg.fast_bin_dir, "/usr/local/bin", "/usr/bin", "/bin",
                                       env.get("PATH", "")])
        try:
            proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL,
                                                        stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.DEVNULL, env=env,
                                                        start_new_session=True)
        except OSError:
            return 127, ""
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), float(timeout))
        except asyncio.TimeoutError:
            return None, ""
        finally:
            if proc.returncode is None:
                # its own process group: a child still holding stdout (e.g. a fallback sender) dies with it, so
                # the wait below cannot hang on the pipe
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    proc.kill()
                await proc.wait()
        return proc.returncode, out.decode("utf-8", "replace")

    def ulog(self, *a, **kw):
        if self._ulog is None:
            self._ulog = _load_usage_log() or (lambda *a, **kw: False)
        try:
            self._ulog(*a, **kw)
        except Exception:
            pass

    def _base(self):
        return [self.cfg.phone_cmd, "open"]

    def _tail(self, label):
        t = (["--label", label] if label else []) + ["--wait", str(int(self.cfg.phone_open_wait_s)),
                                                     "--via", "voice", "--json"]
        return t

    # -- next calendar event ---------------------------------------------------------------------------------------
    async def next_event(self, hint=None):
        """(town, title) of the next located calendar event, or raises LookupError(reason: stale|none|no_place)."""
        rc, age_raw = await self.run(["calcache", "age", "--json"], 5)
        try:
            age = json.loads(age_raw)
        except (TypeError, ValueError):
            age = {}
        if rc != 0 or not age.get("cached") or float(age.get("hours") or 1e9) > self.cfg.fast_cal_max_age_h:
            raise LookupError("stale")
        rc, ev_raw = await self.run(["calcache", "show", "--days", "2", "--json"], 5)
        try:
            events = json.loads(ev_raw) if rc == 0 else None
        except (TypeError, ValueError):
            events = None
        if not isinstance(events, list):
            raise LookupError("stale")
        now = self.now()
        lo, hi = now - timedelta(minutes=EVENT_LATE_MIN), now + timedelta(hours=EVENT_LOOKAHEAD_H)
        timed, located = [], []
        for e in events:
            if not isinstance(e, dict) or e.get("all_day") or (e.get("flags") or {}).get("travel"):
                continue
            try:
                s = datetime.fromisoformat(str(e.get("start")))
            except ValueError:
                continue
            if s.tzinfo is None:
                s = s.replace(tzinfo=now.tzinfo)
            if not lo <= s <= hi:
                continue
            timed.append(e)
            town = str(e.get("town") or "").strip()
            if town and town.lower() != "online":
                located.append((s, town, str(e.get("title") or "").lstrip("@").strip()))
        if not located:
            raise LookupError("no_place" if timed else "none")
        located.sort(key=lambda x: x[0])
        words = {w for w in re.findall(r"\w{3,}", str(hint or "").casefold())}
        if words:
            for s, town, title in located:
                if words & set(re.findall(r"\w{3,}", title.casefold())):
                    return town, title
        return located[0][1], located[0][2]

    # -- the tools ---------------------------------------------------------------------------------------------------
    async def call(self, name, args):
        """-> (result for the model, meta {kind, log_args, route, error}). Never raises for bad input."""
        args = args if isinstance(args, dict) else {}
        t0 = time.monotonic()
        if name == "navigate":
            res, meta = await self._navigate(args)
        else:
            res, meta = await self._open(args)
        meta["ms"] = round((time.monotonic() - t0) * 1000)
        if meta.get("ran"):
            self.ulog("voice.open." + USAGE_KIND[meta["kind"]], "invoke", channel="voice", actor="owner",
                      ms=meta["ms"], outcome=meta.get("route") or meta.get("error") or "failed",
                      attrs={k: v for k, v in (("mode", meta["log_args"].get("mode")),
                                               ("target", meta["log_args"].get("target")),
                                               ("how", meta.get("how"))) if v})
        return res, meta

    async def _navigate(self, args):
        place = _arg(args.get("place"), 200)
        mode = args.get("mode") if args.get("mode") in MODES else "drive"
        target = _target(place)
        log_args = {"kind": "nav", "mode": mode, "target": target}
        meta = {"kind": "nav", "log_args": log_args}
        if not place:
            return {"status": "error", "say": SAY["bad"]["no_place"]}, dict(meta, error="no_place")
        title = None
        if target == "event":
            try:
                place, title = await self.next_event(args.get("event"))
            except LookupError as e:
                why = str(e.args[0]) if e.args else "none"
                return ({"status": "no_event", "say": EVENT_SAY.get(why, EVENT_SAY["none"]) + " Where to?"},
                        dict(meta, error="event_" + why))
            place = _arg(place, 200)
        elif target == "home":
            place = "home"
        if target == "event":
            t, p = speakable(title), speakable(place)
            dest = "%s, for %s" % (p, t) if t and p else (t or p)
        else:
            dest = speakable(place)
        label = "Home" if target == "home" else (speakable(title) or speakable(place) or "Navigation")
        argv = self._base() + ["--nav", place, "--mode", MODES[mode]] + self._tail(label)
        say_ok = SAY["opened"]["nav_home"] if target == "home" else (
            SAY["opened"]["nav"].format(dest=dest) if dest else SAY["opened"]["nav_plain"])
        return await self._exec(argv, meta, say_ok)

    async def _open(self, args):
        what = args.get("what") if args.get("what") in WHATS else None
        if what is None:
            # infer from the fields given (models sometimes leave `what` out)
            what = "link" if args.get("url") else "app" if args.get("app") else "sbb" if args.get("to") else "link"
        kind = CLI_KIND[what]
        log_args = {"kind": kind}
        meta = {"kind": kind, "log_args": log_args}
        if what == "link":
            url = str(args.get("url") or "").strip()
            if not URL_OK.match(url):
                return {"status": "error", "say": SAY["bad"]["no_url"] + OFFER}, dict(meta, error="no_url")
            argv = self._base() + [url] + self._tail("Link from your call")
            return await self._exec(argv, meta, SAY["opened"]["url"])
        if what == "sbb":
            to, frm = _arg(args.get("to"), 80), _arg(args.get("from"), 80) or "here"
            if not to:
                return {"status": "error", "say": SAY["bad"]["no_to"]}, dict(meta, error="no_to")
            at = str(args.get("at") or "").strip()
            arrive = str(args.get("arrive") or "").strip()
            arrive = arrive if HHMM.match(arrive) else ""
            log_args["at"] = bool(HHMM.match(at)) and not arrive
            log_args["arrive"] = bool(arrive)
            argv = self._base() + ["--sbb", frm, to] + (["--arrive", arrive] if arrive else
                                                         ["--at", at] if HHMM.match(at) else [])
            dest = speakable(to)
            label = ("SBB to " + dest) if dest else "SBB connection"
            argv += self._tail(label + (", arrive by " + arrive if arrive else ""))
            if arrive and dest:
                return await self._exec(argv, meta, SAY["opened"]["sbb_arrive"].format(dest=dest, at=arrive))
            return await self._exec(argv, meta, SAY["opened"]["sbb"].format(dest=dest) if dest
                                    else SAY["opened"]["sbb_plain"])
        app = _arg(args.get("app"), 60)
        if not app:
            return {"status": "error", "say": SAY["bad"]["no_app"]}, dict(meta, error="no_app")
        dest = speakable(app, 30)
        argv = self._base() + ["--app", app] + self._tail(dest or "App")
        return await self._exec(argv, meta, SAY["opened"]["app"].format(dest=dest) if dest
                                else SAY["opened"]["app_plain"])

    async def _exec(self, argv, meta, say_ok):
        meta["ran"] = True
        rc, out = await self.run(argv, float(self.cfg.phone_open_timeout_s))
        try:
            d = json.loads(out) if out and out.strip() else {}
        except ValueError:
            d = {}
        d = d if isinstance(d, dict) else {}
        route = d.get("route") if d.get("route") in ROUTES else None
        meta["how"] = d.get("how") if isinstance(d.get("how"), str) else None
        if route is not None and rc == 0:
            meta["route"] = route
            if route == "opened":
                say = say_ok
            elif route == "notified":
                say = SAY["notified"]
            else:
                say = SAY["telegram"]["nav" if meta["kind"] == "nav" else "other"]
            return {"status": route, "say": say}, meta
        err = "timeout" if rc is None else "unavailable" if rc == 127 else EXIT_ERR.get(rc, "failed")
        meta["error"] = err
        e = SAY["error"]
        if err == "refused":
            say = e["refused_url"] if meta["kind"] == "url" else e["refused_app"] if meta["kind"] == "app" \
                else e["refused"]
        else:
            say = e.get(err, e["other"])
        return {"status": "error", "say": say + OFFER}, meta
