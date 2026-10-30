"""Fast lane: a few read-only quick answers inside a call, without the main session (~1 s instead of ~5 s).

Each tool runs token-free local commands (or open-meteo) under one hard timeout (cfg.fast_timeout_s, at most 3 s)
and returns short spoken text that passes the answer filter. On any error, timeout or empty answer the session
hands the question to the main session instead (ask_assistant), so he still gets an answer.
No side effects, ever: only read commands. No vault text: `vault ha now --json` is read for the town only
(home / work / the phone's town), and only the town name goes to the weather API.
"""
import asyncio
import json
import os
import re
import time
import unicodedata
from datetime import date, datetime, timedelta

import aiohttp

from .answer_filter import filter_answer

MAX_TIMEOUT_S = 3.0
# Seconds a read command's output is reused within one call (repeat questions answer at once). Reminders stay
# fresh: he may add one through the assistant during the call.
CACHE_TTL_S = {"cat": 60.0, "finish": 300.0, "rave": 600.0, "trip": 120.0, "bills_due": 120.0, "reminders": 15.0, "parcels": 60.0}


CMD_RUNNER = None  # hub mode (hubmode.install): (argv, timeout) -> (rc, stdout) through the gateway

class FastError(Exception):
    pass


def _tool(name, description, props=None):
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": props or {}}}


_QUICK = " Quick, read-only, answers in about a second. If it can't answer, it hands the question to the assistant itself."
TOOLS = [
    _tool("weather", "The weather where he is now (his town, else home): now, today or tomorrow." + _QUICK,
          {"when": {"type": "string", "enum": ["now", "today", "tomorrow"]}}),
    _tool("way_home", "His next public-transport connections home from where he is, door to door, plus the last "
                      "one tonight. from: only when he names where he is (a stop, station or town); without it, his "
                      "phone and calendar decide. When it answers by asking where he is, ask him and call it again "
                      "with from." + _QUICK,
          {"from": {"type": "string"}}),
    _tool("calendar", "His calendar events: today, tomorrow, or the next few (next)." + _QUICK,
          {"day": {"type": "string", "enum": ["today", "tomorrow", "next"]}}),
    _tool("trains", "His planned train trips (from his calendar) and whether they run on time." + _QUICK),
    _tool("server_space", "How full his home server's storage is and when it will be full." + _QUICK),
    _tool("downloads", "What his media server is downloading right now (movies and shows)." + _QUICK),
    _tool("gym", "His next gym session: which workout, when he last trained." + _QUICK),
    _tool("cat", "His cat right now: litter box visits and state, the feeder's meals, the water fountain and its "
                 "filter." + _QUICK),
    _tool("finish", "TV shows he started and paused, closest to the end first (what to finish)." + _QUICK),
    _tool("rave", "Upcoming raves: shows and festival lineups of his favourite artists, and the ones he's going "
                  "to." + _QUICK),
    _tool("trip", "His next trip: countdown, the next leg, and what comes after. name: only for a specific trip "
                  "he names (a place or event)." + _QUICK,
          {"name": {"type": "string"}}),
    _tool("bills_due", "His open bills: how many, which one is due next and when (no amounts)." + _QUICK),
    _tool("reminders", "His next reminders (his own), with day and time." + _QUICK),
    _tool("parcels", "His parcels and online orders: what waits at the parcel station for pickup and by when, what's on "
                     "the way and when it arrives (shops and item counts only)." + _QUICK),
]
NAMES = frozenset(t["name"] for t in TOOLS)


def fallback_task(name, args):
    """The question in words, for ask_assistant when the quick tool fails."""
    args = args or {}
    return {
        "weather": "What's the weather %s where I am?" % {"today": "today", "tomorrow": "tomorrow"}.get(
            args.get("when"), "right now"),
        "way_home": "How do I get home from %s? Next connections and the last one tonight." % (
            _stop_arg(args) or "where I am"),
        "calendar": "What's on my calendar %s?" % {"today": "today", "tomorrow": "tomorrow"}.get(
            args.get("day"), "next"),
        "trains": "Are my planned trains on time?",
        "server_space": "How much space is left on my server?",
        "downloads": "What's downloading on my media server?",
        "gym": "What's my next gym session?",
        "cat": "How is my cat: litter box, feeder and water fountain?",
        "finish": "Which shows should I finish?",
        "rave": "Which raves and shows of my artists are coming up?",
        "trip": "What's next for my %s?" % ("trip: %s" % _trip_name(args) if _trip_name(args) else "next trip"),
        "bills_due": "Which bills are due soon?",
        "reminders": "What are my next reminders?",
        "parcels": "Where are my parcels? Anything waiting for pickup?",
    }.get(name, "Quick question: %s" % name)


def _trip_name(args):
    """The optional trip name: letters, digits, spaces and a few marks, at most 30 characters, else ''."""
    n = re.sub(r"\s+", " ", str((args or {}).get("name") or "")).strip()
    return n if re.fullmatch(r"[\w .'+&-]{2,30}", n) else ""


def _stop_arg(args):
    """way_home's optional "from" (a stop, station or town he named): letters, digits, spaces and the marks station
    names use, 2 to 60 characters, never starting with "-" (one argv item for gohome, no shell), else ''."""
    n = re.sub(r"\s+", " ", str((args or {}).get("from") or "")).strip()
    return n if re.fullmatch(r"[\w][\w .,'()/+&-]{1,59}", n) else ""


# --- speech shaping ----------------------------------------------------------------------------------------

_URL = re.compile(r"(?i)\bhttps?://\S+")
_SUBS = (("→", " to "), ("›", " then "), ("·", ","), ("•", ""), ("~", "about "), ("×", " times "),
         ("▓", ""), ("░", ""), ("—", ", "), ("–", " to "))
_SPACES = re.compile(r"[ \t]+")


def clean(text):
    """Telegram card text → plain speakable text: no links, emoji, bars or arrows."""
    text = _URL.sub("", text)
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat in ("So", "Sk", "Cs", "Co") or ch in "️‍":
            continue
        out.append(ch)
    text = "".join(out)
    for a, b in _SUBS:
        text = text.replace(a, b)
    lines = [re.sub(r"\s+,", ",", _SPACES.sub(" ", ln)).strip(" ,") for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def speak(text, mask_amounts=True):
    joined = ". ".join(ln.rstrip(".") for ln in clean(text).splitlines())
    joined = re.sub(r"\s+,", ",", joined).strip()
    if joined and joined[-1] not in ".!?":
        joined += "."
    return filter_answer(joined, mask_amounts).text if joined else ""


WMO = {0: "clear sky", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
       51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
       61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
       71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "light showers", 81: "showers",
       82: "heavy showers", 85: "snow showers", 86: "heavy snow showers", 95: "thunderstorms",
       96: "thunderstorms with hail", 99: "thunderstorms with hail"}


def _deg(v):
    return "%d" % round(float(v))


def _hm(iso):
    return iso[11:16]


def _day(d, today, on=False):
    """'today', 'tomorrow', 'Friday' (within 6 days) or 'Friday 6.11'; on=True adds 'on ' before a weekday."""
    n = (d - today).days
    if n == 0:
        return "today"
    if n == 1:
        return "tomorrow"
    if n == -1:
        return "yesterday"
    s = d.strftime("%A") if 1 < n <= 6 else "%s %d.%d" % (d.strftime("%A"), d.day, d.month)
    return ("on " + s) if on else s


def _local(iso, tz):
    """An ISO time (any offset) as an aware datetime in tz, or None."""
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(tz) if dt.tzinfo else dt.replace(tzinfo=tz)


def _pct(v):
    return "%d%%" % round(float(v))


_EPISODE = re.compile(r"^S(\d+)E(\d+)$", re.I)


def _episode(code):
    m = _EPISODE.match(str(code or ""))
    return "season %d episode %d" % (int(m.group(1)), int(m.group(2))) if m else ""


# Payee → spoken category (bills_due names no payee and no amount; unknown payees stay unnamed).
BILL_CATEGORIES = (
    (r"krankenkass|helsana|\bcss\b|swica|sanitas|visana|concordia|assura|\bkpt\b|atupri|sympany|groupe mutuel|"
     r"\bökk\b|egk|health", "health insurance"),
    (r"steuer|\btax|gemeinde|kanton|\bestv\b|finanzverwaltung", "taxes"),
    (r"serafe", "the radio and TV fee"),
    (r"swisscom|\bsalt\b|sunrise|wingo|yallo|init7|quickline|\bupc\b|mobile|internet|telecom", "phone and internet"),
    (r"strom|elektrizit|energie|\bekz\b|\beks\b|\bewz\b|power|wasser|\bgas\b|werke", "utilities"),
    (r"miete|\brent\b|verwaltung|immobilien|liegenschaft", "rent"),
    (r"leasing|\blease\b|credit|kredit|finance", "a lease or credit"),
    (r"strassenverkehr|verkehrsamt|motorfahrzeug|vignette", "the vehicle office"),
    (r"versicherung|insurance|\baxa\b|mobiliar|allianz|generali|baloise|helvetia|smile|\btcs\b", "insurance"),
    (r"arzt|praxis|spital|klinik|zahn|dental|apothek|medical|labor", "a medical bill"),
)


def _bill_category(payee):
    p = str(payee or "").lower()
    return next((cat for rx, cat in BILL_CATEGORIES if re.search(rx, p)), "")


# `remind list` lines (the same format tg-cmd's /reminders reads): his own carry [owner]; place ones are his too.
_REM = re.compile(r"^\s*\d+\.\s+(\d{4}-\d\d-\d\d \d\d:\d\d)\s+(\[owner\] )?(.*)$")
_PLACE_REM = re.compile(r"^\s*\d+\.\s+\S+ (when [^:]{1,60}): (.*)$")
_PRIVATE = re.compile(r"(?i)\[(?:private|redacted|masked)|withheld")


def _rem_title(text):
    """A reminder's first clause, at most 9 words, no emoji, links or tags; vault placeholders → 'a private one'."""
    t = re.sub(r"\s{2}\((?:tag [^)]*|every [^)]*|urgent|held until [^)]*)\)", "", text)
    if _PRIVATE.search(t):
        return "a private one"
    t = re.sub(r"^\(owner\)\s*", "", clean(t)).strip()
    for sep in (". ", " (", "; ", " , "):
        i = t.find(sep)
        if i > 10:
            t = t[:i]
    words = t.split()
    return " ".join(words[:9]).rstrip(",:;") or "a reminder"


# --- the lane ----------------------------------------------------------------------------------------------

def _town_names(town):
    """The town as given, then shorter forms the geocoder knows: before a comma or bracket, before
    " am "/" an der "/" bei "/" im "/" ob ", then the first word."""
    out = [town]
    base = re.split(r"[,(/]", town)[0].strip()
    for b in (base, re.split(r"\s+(?:am|an der|an|bei|im|ob|in der|auf der)\s+", base)[0].strip(), base.split(" ")[0]):
        if b and b not in out:
            out.append(b)
    return out


class FastLane:
    def __init__(self, cfg, run=None, get_json=None, now=None, mono=None):
        self.cfg = cfg
        self.timeout = min(float(cfg.fast_timeout_s), MAX_TIMEOUT_S)
        self._run = run or self._run_cmd
        self._get = get_json or self._get_json
        self._now = now or (lambda: datetime.now().astimezone())
        self._mono = mono or time.monotonic
        self._http = None
        self._cache = {}

    async def close(self):
        if self._http is not None:
            await self._http.close()

    async def call(self, name, args):
        """Spoken answer text, or FastError. The session also wraps this in the timeout."""
        if name not in NAMES:
            raise FastError("unknown quick tool")
        fn = getattr(self, "t_" + name)
        try:
            text = await asyncio.wait_for(fn(args or {}), self.timeout)
        except asyncio.TimeoutError:
            raise FastError("timeout") from None
        except FastError:
            raise
        except (ValueError, KeyError, TypeError, IndexError, OSError, aiohttp.ClientError) as e:
            raise FastError(type(e).__name__) from None
        text = speak(text, self.cfg.mask_amounts) if text else ""
        if not text:
            raise FastError("empty answer")
        return text

    # plumbing
    async def _run_cmd(self, argv, ok=(0,)):
        if CMD_RUNNER is not None:                  # hub mode: the CLI runs in the brain container (voice.lookup)
            rc, out = await CMD_RUNNER(argv, self.cfg.fast_timeout_s)
            if rc not in ok:
                raise FastError("%s exit %s" % (os.path.basename(argv[0]), rc))
            return out
        env = dict(os.environ)
        env.pop("TG_CMD_PICTURES", None)                 # instant commands as text, never a picture card
        env["PATH"] = os.pathsep.join([self.cfg.fast_bin_dir, "/usr/local/bin", "/usr/bin", "/bin",
                                       env.get("PATH", "")])
        proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.DEVNULL, env=env)
        try:
            out, _ = await proc.communicate()
        finally:
            if proc.returncode is None:
                proc.kill()
        if proc.returncode not in ok:
            raise FastError("%s exit %s" % (argv[0], proc.returncode))
        return out.decode("utf-8", "replace")

    async def _cached(self, tool, argv, ok=None):
        """A read command's output, reused for CACHE_TTL_S[tool] seconds (per call; failures are not kept)."""
        key = tuple(argv)
        hit = self._cache.get(key)
        if hit is not None and self._mono() - hit[0] < CACHE_TTL_S.get(tool, 0):
            return hit[1]
        out = await (self._run(argv, ok=ok) if ok else self._run(argv))
        self._cache[key] = (self._mono(), out)
        return out

    async def _get_json(self, url, params):
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession()
        async with self._http.get(url, params=params, timeout=aiohttp.ClientTimeout(total=self.timeout)) as r:
            if r.status != 200:
                raise FastError("%s http %s" % (url.split("/")[2], r.status))
            return await r.json(content_type=None)

    def _profile(self):
        try:
            with open(self.cfg.profile_file, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    # weather
    async def _place(self):
        """(town, at_home). Only the town ever leaves this function."""
        prof = self._profile()
        home = (prof.get("home") or {}).get("town") or ""
        work = (prof.get("work") or {}).get("town") or ""
        try:
            pl = json.loads(await self._run(["vault", "ha", "now", "--json"])).get("place") or {}
        except (FastError, ValueError, OSError, AttributeError):
            pl = {}
        zone = str(pl.get("zone") or "").lower()
        if pl.get("flag") == "home" or zone == "home":
            return home, True
        if pl.get("town"):
            return str(pl["town"]), False
        if zone == "work" and work:
            return work, False
        return home, True

    def _geo_cache(self):
        return os.path.join(self.cfg.state_dir, "fastlane-geo.json")

    async def _geocode(self, town):
        path = self._geo_cache()
        try:
            with open(path, encoding="utf-8") as fh:
                cache = json.load(fh)
        except (OSError, ValueError):
            cache = {}
        if town in cache:
            return cache[town]
        country = (self._profile().get("locale") or {}).get("country", "CH")
        res = []
        for name in _town_names(town):   # the geocoder misses long official names ("X am Y" -> "X")
            d = await self._get(self.cfg.geocode_url, {"name": name, "count": 5, "language": "en", "format": "json"})
            got = d.get("results") or []
            res = res or got
            if any(r.get("country_code") == country for r in got):
                res = got
                break
        if not res:
            raise FastError("town not found")
        best = next((r for r in res if r.get("country_code") == country), res[0])
        cache[town] = [round(best["latitude"], 3), round(best["longitude"], 3)]
        try:
            os.makedirs(self.cfg.state_dir, mode=0o700, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(cache, fh)
            os.replace(tmp, path)
        except OSError:
            pass
        return cache[town]

    async def t_weather(self, args):
        town, at_home = await self._place()
        if not town:
            raise FastError("no town")
        lat, lon = await self._geocode(town)
        tz = (self._profile().get("locale") or {}).get("tz") or "Europe/Zurich"
        d = await self._get(self.cfg.weather_url, {
            "latitude": lat, "longitude": lon, "timezone": tz, "forecast_days": 2,
            "current": "temperature_2m,weather_code,wind_speed_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"})
        where = ("At home in %s" if at_home else "In %s") % town
        day = d["daily"]

        def day_line(i, label):
            s = "%s %s to %s degrees, %s" % (label, _deg(day["temperature_2m_min"][i]),
                                            _deg(day["temperature_2m_max"][i]),
                                            WMO.get(int(day["weather_code"][i]), "mixed"))
            p = (day.get("precipitation_probability_max") or [None, None])[i]
            if p is not None:
                s += ", rain chance %d percent" % round(float(p))
            return s + "."

        when = args.get("when") or "now"
        if when == "tomorrow":
            return "%s: %s" % (where, day_line(1, "tomorrow"))
        out = [where + ":"]
        cur = d.get("current") or {}
        if when == "now" and cur.get("temperature_2m") is not None:
            out.append("now %s degrees, %s." % (_deg(cur["temperature_2m"]),
                                                WMO.get(int(cur.get("weather_code", -1)), "mixed")))
        out.append(day_line(0, "Today"))
        return " ".join(out)

    # way home
    async def t_way_home(self, args):
        """gohome's answer in words. Where he is comes from gohome only (his phone's coarse town or zone, the
        calendar), never coordinates. Not knowing where he is is an answer (ask "from where?"), not a failure; only a
        broken gohome or transport API hands the question on."""
        stop = _stop_arg(args)
        d = json.loads(await self._run(["gohome"] + ([stop] if stop else []) + ["--json"]))
        if not isinstance(d, dict):
            raise FastError("gohome answer")
        if d.get("home"):
            return "You're home."
        if d.get("unknown_stop"):
            return "I can't find a stop called %s. Which station or town are you at?" % d["unknown_stop"]
        if d.get("near"):
            return "You're close to home. Tell me the stop if you want a bus or train."
        if not d.get("origin"):
            return "I don't know where you are right now. Which stop or town are you at?"
        nxt, last = d.get("next") or [], d.get("last")
        if not (nxt or last):
            return "I found no connection home from %s. Is there another stop near you?" % d["origin"]

        def conn(c, first=False):
            s = _hm(c["dep"])
            if first:
                if c.get("platform"):
                    s += " from platform %s" % c["platform"]
                if c.get("lines"):
                    s += ", " + " then ".join(c["lines"])
                ch = int(c.get("changes") or 0)
                s += ", %s" % ("direct" if ch == 0 else "one change" if ch == 1 else "%d changes" % ch)
            return s + ", home at %s" % _hm(c["arr"])

        parts = ["From %s: next %s." % (d["origin"], conn(nxt[0], True))] if nxt else ["From %s:" % d["origin"]]
        if len(nxt) > 1:
            parts.append("Then %s." % conn(nxt[1]))
        if last and not any(c.get("dep") == last.get("dep") for c in nxt[:2]):
            parts.append("Last one tonight %s." % conn(last))
        elif last:
            parts.append("That's the last one tonight.")
        return " ".join(parts)

    # calendar
    async def t_calendar(self, args):
        age_raw, ev_raw = await asyncio.gather(self._run(["calcache", "age", "--json"]),
                                               self._run(["calcache", "show", "--days", "8", "--json"]))
        age = json.loads(age_raw)
        if not age.get("cached") or float(age.get("hours") or 1e9) > self.cfg.fast_cal_max_age_h:
            raise FastError("calendar cache stale")
        events = json.loads(ev_raw)
        now = self._now()
        today = now.date()
        want = args.get("day") or "next"

        def start_date(e):
            return date.fromisoformat(e["start"][:10])

        def upcoming(e):
            if e.get("all_day"):
                return date.fromisoformat(e["end"][:10]) > today
            return datetime.fromisoformat(e["end"]) > now

        def line(e, with_day):
            title = str(e.get("title") or "event").lstrip("@").strip()
            town = e.get("town")
            if town and town.lower() not in title.lower() and not (e.get("flags") or {}).get("travel"):
                title += " in " + town
            sd = start_date(e)
            day = ""
            if with_day:
                day = "today " if sd <= today else "tomorrow " if sd == today + timedelta(days=1) else \
                    sd.strftime("%A ")
            return ("%sall day %s" % (day, title)) if e.get("all_day") else "%s%s %s" % (day, e["start"][11:16], title)

        evs = [e for e in events if upcoming(e)]
        if want in ("today", "tomorrow"):
            d0 = today if want == "today" else today + timedelta(days=1)
            evs = [e for e in evs if (start_date(e) <= d0 < date.fromisoformat(e["end"][:10]))
                   if e.get("all_day")] + [e for e in evs if not e.get("all_day") and start_date(e) == d0]
            if not evs:
                return "Nothing %s on your calendar." % ("else today" if want == "today" else "tomorrow")
            return "%s: %s." % (want.capitalize(), "; ".join(line(e, False) for e in evs[:5]))
        timed = [e for e in evs if not e.get("all_day")][:4]
        if not timed:
            return "Nothing timed on your calendar in the next week."
        return "Next: %s." % "; ".join(line(e, True) for e in timed)

    # server and media cards (tg-cmd: the same text as his instant commands)
    async def t_trains(self, args):
        lines = clean(await self._run(["tg-cmd", "train"])).splitlines()
        body = [ln for ln in lines[1:] if not ln.lower().startswith("trains")]
        if not body:
            return clean(lines[0]) if lines else ""
        pairs, cur = [], None
        for ln in body:
            if re.match(r"^[A-Z][a-z]{2} \d{1,2}\.\d{1,2}\b", ln):        # "Thu 22.10 18:14 …" starts a leg
                cur = [ln]
                pairs.append(cur)
            elif cur is not None:
                cur.append(ln)
            else:
                pairs.append([ln])
        return "\n".join(", ".join(p) for p in pairs[:2])

    async def t_server_space(self, args):
        lines = clean(await self._run(["tg-cmd", "space"])).splitlines()
        keep = [ln for ln in lines[1:] if ln and ln.lower() != "pools"]
        return "\n".join(keep[:7])

    async def t_downloads(self, args):
        lines = clean(await self._run(["tg-cmd", "queue"])).splitlines()
        return "\n".join(lines[:6])

    async def t_gym(self, args):
        lines = clean(await self._run(["tg-cmd", "gym"])).splitlines()
        if not lines:
            return ""
        out = [lines[0]]
        out += [ln for ln in lines if ln.lower().startswith(("last session", "free evenings"))]
        names = [re.sub(r"\s+\d.*$", "", re.sub(r"^\d+[ab]?\s+", "", ln)) for ln in lines
                 if re.match(r"^\d+[ab]?\s+\S", ln)]
        if names:
            out.append("Exercises: " + ", ".join(n for n in names[:6] if n))
        return "\n".join(out)

    # round 2: cat, finish, rave, trip, bills_due, reminders, parcels (read-only; outputs cached per call, CACHE_TTL_S)
    async def t_cat(self, args):
        st, sup = await asyncio.gather(self._cached("cat", ["cat-corner", "status", "--json"], ok=(0, 1)),
                                       self._cached("cat", ["cat-supplies", "--json"]), return_exceptions=True)
        if isinstance(st, BaseException):
            raise st
        st = json.loads(st)
        v = st.get("view") or {}
        try:
            rates = (json.loads(sup).get("rates") or {}) if isinstance(sup, str) else {}
        except ValueError:
            rates = {}
        now = self._now()
        tz, today = now.tzinfo, now.date()
        out = []
        probs = [clean(str(p.get("text") or "")) for p in st.get("problems") or [] if isinstance(p, dict)]
        probs = [p.replace("\n", ", ") for p in probs if p]
        if probs:
            out.append("Heads-up: %s." % "; ".join(probs[:2]).rstrip("."))
        pets = [p for p in v.get("pets") or [] if isinstance(p, dict)]
        for p in pets[:2]:
            n = int(p.get("visits_today") or 0)
            line = "%s: %d litter visit%s today" % (p.get("name") or "Your cat", n, "" if n == 1 else "s")
            last = _local(p["last_visit"], tz) if p.get("last_visit") else None
            if last:
                line += ", last " + ("" if last.date() == today else _day(last.date(), today) + " ") + \
                    "at " + last.strftime("%H:%M")
            if p.get("weight_cur"):
                line += ", weighs %.1f kilos" % float(p["weight_cur"])
            out.append(line + ".")
        feeders = [f for f in v.get("feeders") or [] if isinstance(f, dict)]
        for f in feeders[:1]:
            if f.get("offline_since"):
                out.append("The feeder is offline.")
                continue
            line = "Feeder: %d grams today" % round(float(f.get("food_g_today") or 0))
            nxt = _local(f["next_feed"], tz) if f.get("next_feed") else None
            if nxt:
                line += ", next meal %s at %s" % (_day(nxt.date(), today), nxt.strftime("%H:%M"))
            if f.get("low"):
                line += ", food is low"
            if f.get("missed_meal"):
                line += ", a meal was missed"
            if f.get("schedule_off_since"):
                line += ", its schedule is off"
            out.append(line + ".")
        r = v.get("robot") if isinstance(v.get("robot"), dict) else None
        if r:
            line = "Litter box %s" % (r.get("label") or "status unknown")
            if r.get("litter") is not None:
                line += ", litter %s" % _pct(r["litter"])
            if r.get("drawer") is not None:
                line += ", drawer %s full" % _pct(r["drawer"])
            out.append(line + ".")
        fountains = [f for f in v.get("fountains") or [] if isinstance(f, dict)]
        for fo in fountains[:1]:
            line = "Fountain " + ("offline" if fo.get("offline_since") else "low on water" if fo.get("low")
                                  else "OK")
            if rates.get("filter_pct_now") is not None:
                line += ", filter at %s" % _pct(rates["filter_pct_now"])
            try:
                empty = date.fromisoformat(str(rates.get("filter_empty_on"))[:10])
                line += ", change it %s" % _day(empty, today, on=True)
            except ValueError:
                pass
            out.append(line + ".")
        extra = []
        if isinstance(rates.get("desiccant_days_left"), (int, float)) and rates["desiccant_days_left"] < 0:
            extra.append("the feeder's desiccant is overdue")
        if rates.get("feeder_battery") == "low":
            extra.append("the feeder's backup battery is low")
        if extra:
            out.append("Also, %s." % " and ".join(extra))
        if not (pets or feeders or r or fountains):
            raise FastError("no cat data")
        return " ".join(out)

    async def t_finish(self, args):
        d = json.loads(await self._cached("finish", ["media", "finish", "-n", "3", "--json"], ok=(0, 3)))
        items = [i for i in d.get("items") or [] if isinstance(i, dict) and i.get("title")]
        if not items:
            return "No paused shows to finish."
        parts = []
        for i in items[:3]:
            line = str(i["title"])
            if i.get("left") is not None and i.get("total"):
                line += ", %d of %d episodes left" % (int(i["left"]), int(i["total"]))
            ep = _episode(i.get("episode"))
            if ep:
                line += ", next %s" % ep
                if i.get("state") == "resume" and i.get("pos_min"):
                    line += " from minute %d" % int(i["pos_min"])
            parts.append(line)
        return "Closest to the end: %s." % "; ".join(parts)

    async def t_rave(self, args):
        lines = clean(await self._cached("rave", ["tg-cmd", "rave"])).splitlines()
        rows = [ln for ln in lines[1:] if ln]
        if not rows:
            return lines[0] if lines else ""
        return "Next: %s." % "; ".join(rows[:3])

    async def t_trip(self, args):
        name = _trip_name(args)
        lines = clean(await self._cached("trip", ["tg-cmd", "trip"] + ([name] if name else []))).splitlines()
        if not lines:
            return ""
        head, legs, n_open, then, in_open = lines[0], [], 0, "", False
        if head.lower().startswith("no trip"):
            return head.split(". ")[0].rstrip(".") + "."
        for ln in lines[1:]:
            if ln.startswith("Then:"):
                then = ln[5:].strip()
            elif ln.startswith("Offline pack") or ln.startswith("Nothing open"):
                continue
            elif ln == "Open:":
                in_open = True
            elif in_open:
                m = re.match(r"^\+ (\d+) more", ln)
                n_open += int(m.group(1)) if m else 1
            else:
                legs.append(ln)
        out = [head + "."] + [ln[:1].upper() + ln[1:] + "." for ln in legs[:3]]
        if n_open:
            out.append("%d open to-do%s." % (n_open, "" if n_open == 1 else "s"))
        if then:
            out.append("After that: %s." % re.sub(r",?\s*pack ready$", "", then))
        return " ".join(out)

    async def t_bills_due(self, args):
        bills = json.loads(await self._cached("bills_due", ["bills", "list", "--open", "--json"]) or "[]")
        bills = [b for b in bills if isinstance(b, dict)]
        if not bills:
            return "No open bills."
        today = self._now().date()
        dated = []
        for b in bills:
            try:
                dated.append((date.fromisoformat(str(b.get("due"))[:10]), _bill_category(b.get("payee"))))
            except ValueError:
                continue
        dated.sort(key=lambda x: x[0])
        n = len(bills)
        out = ["One open bill." if n == 1 else "%d open bills." % n]
        over = [d for d, _ in dated if d < today]
        if over:
            out.append("%s overdue, since %s." % ("One is" if len(over) == 1 else "%d are" % len(over),
                                                 _day(over[0], today)))
        ahead = [(d, c) for d, c in dated if d >= today]
        if ahead:
            d, cat = ahead[0]
            out.append("Next due %s%s." % (_day(d, today, on=True), (", " + cat) if cat else ""))
            week = sum(1 for d, _ in ahead if (d - today).days <= 7)
            if week > 1:
                out.append("%d are due within a week." % week)
        return " ".join(out)

    async def t_reminders(self, args):
        raw = await self._cached("reminders", ["remind", "list"])
        now = self._now()
        mine, places = [], 0
        for ln in raw.splitlines():
            if _PLACE_REM.match(ln):
                places += 1
                continue
            m = _REM.match(ln)
            if not m or not m.group(2):
                continue                                  # the assistant's own follow-ups: never spoken
            at = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M")
            if at >= now.replace(tzinfo=None) - timedelta(minutes=1):
                mine.append((at, m.group(3)))
        if not mine and not places:
            return "No reminders set."
        today = now.date()
        out = []
        if mine:
            parts = ["%s %s, %s" % (_day(at.date(), today), at.strftime("%H:%M"), _rem_title(t)) for at, t in mine[:3]]
            out.append(("You have %d. Next: " % len(mine) if len(mine) > 3 else "Next: ") + "; ".join(parts) + ".")
        if places:
            out.append(("Plus %d" if mine else "%d") % places + " by place, like when you get home.")
        return " ".join(out)

    async def t_parcels(self, args):
        # `parcels speak` reads its cache only (the cron refresh reads the shops and the vault's mail flags); exit 4 =
        # never checked → hand-off to the assistant
        return (await self._cached("parcels", ["parcels", "speak"])).strip()


def measure(cfg, names=None):
    """Live read-only latency of each quick tool (ms, ok/error) — for `voice-relay fastlane`."""
    async def main():
        lane = FastLane(cfg)
        res = []
        try:
            for n in names or [t["name"] for t in TOOLS]:
                t0 = time.monotonic()
                try:
                    text, ok = await lane.call(n, {}), True
                except FastError as e:
                    text, ok = str(e), False
                res.append({"tool": n, "ms": round((time.monotonic() - t0) * 1000), "ok": ok, "text": text})
        finally:
            await lane.close()
        return res
    return asyncio.run(main())
