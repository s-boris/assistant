#!/usr/bin/env python3
"""usage: the local product-analytics store (writer, context snapshot, collectors). Token-free; nothing leaves the box.

Writer, from any script (never raises, never waits on the database; False = not written; USAGE=off disables it):
    sys.path.insert(0, os.path.expanduser("~/bin/lib")); import usage
    usage.log("tg.week", "invoke", channel="telegram", ms=420, attrs={"picture": True})
  It appends one sanitized JSON line to the spool (STATE/spool/*.jsonl, O_APPEND). Kept: feature ids, kinds, numbers
  and short tokens without spaces (so never message text, names or addresses); anything else is dropped.
`usage tick` (cron, the only writer, under a lock) refreshes the context snapshot and folds the spool and the
collectors into STATE/usage.db (STATE = $USAGE_STATE or ~/.local/state/product; SQLite, WAL):
  events   owner-relevant events: ts, day, feature, kind, channel, actor, src, hour, dow, zone, ms, outcome,
           ref (tg:<msg id> | push:<id> | call:<id>: links a notification to his tap, reaction or reply), turn,
           ext_id (dedupe), ctx JSON (context dimensions), attrs JSON (event fields). New dimensions need no
           migration: slice with json_extract(ctx, '$.<dim>').
  runs     day x feature x actor x channel aggregates (n, fails, ms_sum, ms_max, hist = latency buckets) for cron
           runs, script execs (tg-cmd), every Bash/Skill call (hook, also subagents) and voice reply latency.
  ctx_log  context snapshots (change-only, t_from..t_to), joined to events by time.
  turns    one row per owner turn: how it started (start: tap | command | voice_call | voice_note | text | terminal …,
           prefix = button prefix or command name, dev = test/build words in his message, a flag only), mix = counts
           of my tool calls by category (use, edit, test, ops, maint, agent, read, reply, …; fx = features whose files
           I edited), and, set at derive time, label (use | build | test | maint) and primary_f (the one feature the
           turn is mainly about). Ad-hoc question-button taps in a use turn get attrs.credit = that feature.
  meta     collector offsets.
Collectors: spool (lib, hook, tg/tg-buttons, cron-fail, hubjob + keepers = core.jobfail.usage_run for jobs the
harness runs itself, app = the phone app's `usage` frames via phonegw), the events
ledger's tg-adapter rows (tg.message, tg.button, tg.reaction, tg.poll_answer, tg.stop; a message a binding
answered = an instant command; id watermark in meta events_id), while hub-tg is on the same rows on the hub
(core.hub.tg_journal from meta hub_ts minus 15 min, first run 3 days; USAGE_HUB=on|off, default on only while
hub-tg is on and neither USAGE_JOURNAL nor USAGE_EVENTS_DB is set), the telegram-plus inbound journal (0.1.22
shape, until bridge-only 0.3.0; the same ext_ids, so they dedupe), main-session transcripts
(turn trigger, owner channel events incl. ones queued mid-turn, tool calls; text is read only to derive flags and never
stored), the voice call log (claude-hub-voice, `voice-hub calls`), phonegw DB (read-only), talk-button log, remind's fired.log (one notify_sent per fired
owner reminder, the reminder's only send row; its app push rows give opens and swipes); menu-usage day counts fill gaps of settled
days (every tick: up to 2 days ago; `usage backfill`: up to yesterday).
Context (coarse only: no coordinates, stops, titles or text): time (hour band, weekday, quiet, sleep-in, zone, trip),
place (zone, band, town), motion (activity, driving), phone (screen, battery, net, vpn, roaming, dnd, car, buds,
next alarm), calendar (office day, event before/during/after, travel leg, busy band, event night), home (TV,
lights band, away mode, vacuum). Tests: the repo's tests/legacy/usage (run.sh).
"""
import datetime as dt
import html
import json
import os
import re
import sqlite3
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
HERE = os.path.dirname(os.path.realpath(__file__))


def _env(name, *rel):
    return os.environ.get(name) or os.path.join(HOME, *rel)


STATE = _env("USAGE_STATE", ".local/state/product")
DB = os.path.join(STATE, "usage.db")
SPOOL = os.path.join(STATE, "spool")
CTX_FILE = os.path.join(STATE, "ctx.json")
SLOW_FILE = os.path.join(STATE, "ctx-slow.json")
BIN = _env("USAGE_BIN", "bin")
CATALOGUE = _env("USAGE_CATALOGUE", ".config/product/features.json")
JOURNAL = _env("USAGE_JOURNAL", ".claude/channels/telegram/inbound-journal.jsonl")
HUB = os.environ.get("USAGE_HUB", "")
HUB_OVERLAP_S = 900
EVENTS_DB = os.environ.get("USAGE_EVENTS_DB") or os.environ.get("ASSISTANT_EVENTS_DB") or os.path.join(
    HOME, ".local/state/assistant/events.db")
TRANSCRIPTS = os.environ.get("USAGE_TRANSCRIPTS") or os.path.join(
    HOME, ".claude/projects", os.path.join(HOME, "workspace").replace("/", "-"))
# the relay's call log stays in claude-hub-voice (14 days); `voice-hub calls` reads it as voice.calls digests (metadata
# only). USAGE_CALLS_CMD = the command, off = not asked (unset in a test env = off).
CALLS_CMD = os.environ.get("USAGE_CALLS_CMD", "")
# the gateway runs in claude-hub-phone; `hub phone inbox` keeps an allow-listed mirror of its database (same tables
# and columns, hubkit.phonemirror).
PHONEGW = _env("USAGE_PHONEGW_DB", ".local/state/claude-hub/phonegw-mirror.db")
TALK_LOG = _env("USAGE_TALK_LOG", ".local/state/talk-button/log")
MENU_COUNTS = _env("USAGE_MENU_COUNTS", ".local/state/menu-usage/counts.json")
CALCACHE = _env("USAGE_CALCACHE", ".local/state/calcache/events.json")
PLACE_STATE = _env("USAGE_PLACE_STATE", ".local/state/remind/place-state.json")
REMIND_FIRED = _env("USAGE_REMIND_FIRED", ".local/state/remind/fired.log")
PROFILE = os.environ.get("ASSISTANT_PROFILE") or os.path.join(HOME, ".config/assistant/profile.json")

INTENT = ("invoke", "tap", "msg", "reply", "react", "answer", "call")
OK_OUT = ("ok", "0", "answered", "sent", "done", "usage", "empty")   # "usage" = bad arguments, a usage hint printed
KINDS = set(INTENT) | {"exec", "run", "notify_sent", "notify_opened", "dismissed", "ignored", "error", "abandoned"}
CHANNELS = {"telegram", "app", "voice", "cron", "terminal", "session", "script"}
TOKEN = re.compile(r"[A-Za-z0-9_.:/+@#=-]{1,48}")
FEAT = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,63}")
KEY = re.compile(r"[a-z][a-z0-9_]{0,31}")
CTXVAL = re.compile(r"[\w .'/+-]{1,40}")
EMOJI = {"👍": "up", "👎": "down", "🎉": "party", "😴": "snooze", "✍": "write", "✍️": "write", "💩": "delete",
         "❤": "heart", "❤️": "heart", "🔥": "fire", "👀": "eyes", "🙏": "thanks", "😂": "laugh", "👌": "ok"}
DISMISS = re.compile(r"(^|:)(no|skip|dismiss|mute|later|cancel|off|ignore|npno|drop)(:|$)")
NEG_EMOJI = {"down", "delete"}
HIST = (250, 500, 1000, 2000, 4000, 8000)          # runs.hist buckets: <250 ms … >= 8 s
DOW = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
SKIP_ANCESTORS = {"tg", "tg-buttons", "cron-fail", "usage", "timeout", "env", "sh", "bash", "dash", "python3",
                  "python", "flock", "nice", "xargs"}


# ---------------------------------------------------------------- sanitizing

def tok(v, n=48):
    """bool/number as is; a str only when it is one short token (no spaces); else None."""
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v if v == v and abs(v) < 1e15 else None
    s = str(v)
    return s if len(s) <= n and TOKEN.fullmatch(s) else None


def cv(v):
    """A coarse context value: bool/number, or a short label (letters, digits, spaces); else None."""
    if v is None or isinstance(v, bool) or isinstance(v, (int, float)):
        return v
    s = str(v).strip()
    return s if CTXVAL.fullmatch(s) and not re.search(r"\d{4,}", s) else None


def clean_attrs(a):
    out = {}
    for k, v in (a or {}).items():
        if len(out) >= 16 or not isinstance(k, str) or not KEY.fullmatch(k):
            continue
        if isinstance(v, (list, tuple)):
            v = [x for x in (tok(x) for x in list(v)[:8]) if x is not None] or None
        else:
            v = tok(v)
        if v is not None:
            out[k] = v
    return out


def norm_feature(f):
    if not isinstance(f, str):
        return None
    f = f.strip().lower().replace(" ", "-")
    return f if FEAT.fullmatch(f) else None


# ---------------------------------------------------------------- writer

def off():
    return os.environ.get("USAGE", "").lower() in ("off", "0", "no", "false")


def _spool(rec, src=None):
    os.makedirs(SPOOL, mode=0o700, exist_ok=True)
    line = json.dumps(rec, separators=(",", ":"), ensure_ascii=True) + "\n"
    if len(line) > 3500:
        return False
    path = os.path.join(SPOOL, f"{src or rec.get('src') or 'lib'}-{time.strftime('%Y%m%d')}.jsonl")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line.encode())
    finally:
        os.close(fd)
    return True


def record(feature, kind="invoke", channel=None, actor=None, ms=None, outcome=None, ref=None, attrs=None,
           ts=None, src="lib", ext_id=None):
    f = norm_feature(feature)
    if not f or kind not in KINDS:
        return None
    rec = {"ts": round(float(time.time() if ts is None else ts), 3), "f": f, "k": kind, "src": tok(src) or "lib"}
    ch = channel if channel is not None else caller_channel()
    if ch in CHANNELS:
        rec["ch"] = ch
    for key, val in (("a", tok(actor)), ("o", tok(outcome, 32)), ("r", tok(ref, 64)), ("x", tok(ext_id, 96))):
        if val is not None:
            rec[key] = val
    if isinstance(ms, (int, float)) and 0 <= ms < 1e9:
        rec["ms"] = int(ms)
    at = clean_attrs(attrs)
    if at:
        rec["at"] = at
    return rec


def log(feature, kind="invoke", channel=None, actor=None, ms=None, outcome=None, ref=None, attrs=None, ts=None,
        src="lib", ext_id=None):
    """Record one event. Never raises; returns False when nothing was written."""
    try:
        if off():
            return False
        rec = record(feature, kind, channel, actor, ms, outcome, ref, attrs, ts, src, ext_id)
        return _spool(rec) if rec else False
    except Exception:
        return False


# ---------------------------------------------------------------- process ancestry (who called me)

def _proc(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            argv = [a.decode(errors="replace") for a in f.read().split(b"\0") if a]
        with open(f"/proc/{pid}/stat") as f:
            ppid = int(f.read().rsplit(")", 1)[1].split()[1])
        return argv, ppid
    except Exception:
        return None, 0


def _bin_name(path):
    """~/bin script name of an argv entry, else None."""
    if not path:
        return None
    b = os.path.basename(path)
    if path.startswith(BIN + "/") or path.startswith("~/bin/") or ("/" not in path and is_bin(b)):
        return b if is_bin(b) else None
    return None


def ancestors(pid=None, depth=8):
    """[(argv, ~/bin script names in argv[0:2])] from pid (default: my parent) upwards."""
    out, pid = [], pid or os.getppid()
    for _ in range(depth):
        if pid <= 1:
            break
        argv, ppid = _proc(pid)
        if argv is None:
            break
        names = [n for n in (_bin_name(a) for a in argv[:2]) if n]
        out.append((argv, names))
        pid = ppid
    return out


def caller_channel(pid=None):
    for argv, _ in ancestors(pid):
        line = " ".join(argv[:3])
        base = os.path.basename(argv[0]) if argv else ""
        if "telegram-plus" in line or line.endswith("server.ts") or " server.ts" in line:
            return "telegram"
        if "adapters/tg/src/cli.ts" in line and "run" in argv:  # tg-adapter (v2) running a bound command or tap
            return "telegram"
        if "voice_relay" in line or "voice-relay" in line:
            return "voice"
        if "phonegw" in line:
            return "app"
        if base == "cron" or base == "crond":
            return "cron"
        if base == "claude":
            return "session"
    return "script"


OWNER_CHANNELS = ("telegram", "voice", "app")   # a run started by the plugin, the voice relay or the phone gateway
NON_OWNER = ("test", "agent", "subagent", "session")  # actors that never count as owner use or owner-facing failures
TEST_ENV = ("USAGE_TEST", "TG_CMD_TEST", "PYTEST_CURRENT_TEST")


def caller_actor(channel=None, pid=None):
    """Who a script run is for: "test" (a test env var set), "owner" (started by the plugin / voice relay / phone
    gateway), "agent" (from a Claude session or subagent: dev and test runs), else "cron" / "script"."""
    if any(os.environ.get(k) for k in TEST_ENV):
        return "test"
    ch = channel or caller_channel(pid)
    if ch in OWNER_CHANNELS:
        return "owner"
    return {"session": "agent", "cron": "cron"}.get(ch, "script")


def caller_script(pid=None):
    for _, names in ancestors(pid):
        for n in names:
            if n not in SKIP_ANCESTORS:
                return n
    return None


_BINS = {}


def is_bin(name):
    if name in _BINS:
        return _BINS[name]
    ok = bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,40}", name or "")) and \
        not re.search(r"\.(orig|bak|new|old|tmp)$|\.bak-", name) and \
        os.path.isfile(os.path.join(BIN, name)) and os.access(os.path.join(BIN, name), os.X_OK)
    _BINS[name] = ok
    return ok


SEG = re.compile(r"(?:^|[;&|`(\n{]|\$\()\s*(?:[A-Za-z_][A-Za-z0-9_]*=(?![$`]\()[^\s$`]*\s+)*"
                 r"(?:(?:env|nice|time|exec|command|nohup|xargs)(?:\s+-\S+)*\s+|timeout(?:\s+-\S+)*\s+\S+\s+)*"
                 r"([^\s;&|`()<>'\"]+)(?:[ \t]+([a-z][a-z0-9_-]{1,24})(?=[\s;&|)]|$))?")


def scripts(cmd):
    """[[name, verb|None]] of the ~/bin scripts a shell command runs (validated against ~/bin; no other text)."""
    out = []
    for m in SEG.finditer(cmd or ""):
        word, verb = m.group(1), m.group(2)
        w = word
        for pre in ("~/bin/", "$HOME/bin/", "${HOME}/bin/", BIN + "/"):
            if w.startswith(pre):
                w = w[len(pre):]
        if "/" in w or not is_bin(w):
            continue
        pair = [w, verb]
        if pair not in out:
            out.append(pair)
        if len(out) >= 8:
            break
    return out


# ---------------------------------------------------------------- time and context

_ZC, _TC = {}, {"mod": None, "failed": False}


def _tripclock():
    if _TC["mod"] is None and not _TC["failed"]:
        try:
            sys.path.insert(0, HERE)
            import tripclock
            _TC["mod"] = tripclock
        except Exception:
            _TC["failed"] = True
        finally:
            if sys.path and sys.path[0] == HERE:
                sys.path.pop(0)
    return _TC["mod"]


def local(ts):
    """(aware local datetime on his clock, trip name or None)."""
    k = int(ts // 3600)
    if k not in _ZC:
        z = trip = None
        tc = _tripclock()
        if tc:
            try:
                z, _, trip = tc.zone_why(ts)
            except Exception:
                z = None
        _ZC[k] = (z, trip)
    z, trip = _ZC[k]
    d = dt.datetime.fromtimestamp(ts, z) if z else dt.datetime.fromtimestamp(ts).astimezone()
    return d, trip


def hour_band(h):
    return ("night" if h < 6 else "early" if h < 9 else "morning" if h < 12 else "midday" if h < 14 else
            "afternoon" if h < 18 else "evening" if h < 22 else "late")


def time_ctx(ts):
    d, trip = local(ts)
    c = {"hour_band": hour_band(d.hour), "dow": DOW[d.weekday()], "weekend": d.weekday() >= 5,
         "quiet": d.hour < 8, "tz": getattr(d.tzinfo, "key", None)}
    if trip and cv(trip):
        c["trip"] = cv(trip)
    return d, {k: v for k, v in c.items() if v is not None}


def _json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def iso(s):
    try:
        d = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.astimezone()
        return d.timestamp()
    except Exception:
        return None


def _band(n, cuts, labels):
    for c, lab in zip(cuts, labels):
        if n < c:
            return lab
    return labels[-1]


def _drive_flag(row, now):
    """The gateway's stored flag.drive (value, at) under core.drive's rule, the one `driving` uses: on, its until
    (when the row carries one) not passed, the row at most DRIVING_MAX_AGE_MIN (60) min old. No raw car/activity
    guess beside it: the gateway's verdict already covers both. core missing -> False (never a stuck "driving")."""
    if not row:
        return False
    try:
        from core import drive
    except Exception:
        return False
    return drive.stored(row[0], row[1], now)


def _phone(now):
    c = {}
    try:
        con = sqlite3.connect(f"file:{PHONEGW}?mode=ro", uri=True, timeout=2)
    except Exception:
        return c
    try:
        dev = con.execute("SELECT id FROM devices WHERE status='active' ORDER BY last_seen DESC LIMIT 1").fetchone()
        rows = con.execute("SELECT device_id, k, v, at FROM state").fetchall()
    except Exception:
        return c
    finally:
        con.close()
    st, newest = {}, 0
    for d, k, v, at in rows:
        try:
            val = json.loads(v)
        except Exception:
            val = v
        at = (at or 0) / (1000 if (at or 0) > 1e11 else 1)
        if dev and d == dev[0]:
            st[k] = (val, at)
            newest = max(newest, at)
        elif k.startswith("widget:") and (k not in st or at > st[k][1]):
            st[k] = (val, at)
    c["phone"] = "none" if not dev or not newest else "live" if now - newest <= 1800 else "stale"

    def g(k):
        v = st.get(k)
        return v[0] if v else None
    if c["phone"] == "live":
        pl = g("place") or {}
        if isinstance(pl, dict):
            if cv(pl.get("zone")):
                c["zone"] = cv(pl.get("zone")).lower()
                c["place_src"] = "phone"
            c["band"], c["town"] = cv(pl.get("band")), coarse_town(pl.get("town"))
        act = g("activity") or {}
        c["activity"] = cv(act.get("kind")) if isinstance(act, dict) else None
        b = g("battery") or {}
        if isinstance(b, dict):
            c["batt"] = cv(b.get("band")) or (_band(b.get("level") or 0, (16, 31, 61), ("critical", "low", "mid", "high"))
                                                if isinstance(b.get("level"), (int, float)) else None)
            c["charging"] = bool(b.get("charging"))
        s = g("screen") or {}
        c["screen"] = ("on" if s.get("on") else "off") if isinstance(s, dict) and "on" in s else None
        n = g("net") or {}
        if isinstance(n, dict):
            c["net"], c["vpn"], c["roaming"] = cv(n.get("type")), bool(n.get("vpn")), bool(n.get("roaming"))
        dnd = g("dnd")
        c["dnd"] = cv(dnd) if isinstance(dnd, str) else None
        car = g("car") or {}
        if isinstance(car, dict):
            c["car"] = "auto" if car.get("projection") else "bt" if car.get("bt") else "none"
        bt = g("bt")
        if isinstance(bt, list):
            types = [str(x).lower() for x in bt]
            c["buds"] = any("bud" in x or "head" in x or "ear" in x for x in types)
        al = g("alarm") or {}
        nxt = al.get("next") if isinstance(al, dict) else None
        if isinstance(nxt, (int, float)) and nxt > 0:
            mins = ((nxt / 1000 if nxt > 1e11 else nxt) - now) / 60
            c["alarm_in"] = "none" if mins < 0 else _band(mins, (60, 180, 480, 1440), ("<1h", "1-3h", "3-8h", "8-24h", ">24h"))
        c["driving"] = _drive_flag(st.get("flag.drive"), now)
    hw = st.get("widget:home")
    if hw and isinstance(hw[0], dict):
        data = hw[0].get("data") or {}
        at = hw[0].get("at") or hw[1]
        at = at / 1000 if at > 1e11 else at
        if isinstance(data, dict) and now - at <= 1800:
            lo = data.get("lights_on")
            c["tv"] = cv(data.get("tv"))
            c["lights"] = _band(lo, (1, 4), ("0", "1-3", "4+")) if isinstance(lo, (int, float)) else None
            c["away_mode"] = bool(data.get("away"))
            c["vacuum"] = cv(data.get("vacuum"))
    return {k: v for k, v in c.items() if v is not None}


def _calendar(now, zone_now=None):
    j = _json(CALCACHE)
    if not isinstance(j, dict):
        return {}
    if now - (iso(j.get("fetched")) or 0) > 2 * 86400:
        return {"cal": "stale"}
    d0 = local(now)[0]
    today = d0.date().isoformat()
    day_start = d0.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    day_end = day_start + 86400
    work = ((_json(PROFILE) or {}).get("work") or {}).get("town") or ""
    marker = ("@" + work).lower() if work else None
    state, travel, busy, office, night = None, None, 0, False, False
    rank = {"during": 3, "before": 2, "after": 1}
    for e in j.get("events") or []:
        if not isinstance(e, dict):
            continue
        flags = e.get("flags") or {}
        if e.get("all_day"):
            sd, ed = str(e.get("start", ""))[:10], str(e.get("end", ""))[:10]
            if sd <= today < (ed if ed > sd else sd + "~") and marker and marker in str(e.get("title", "")).lower():
                office = True
            continue
        s, en = iso(e.get("start")), iso(e.get("end"))
        if s is None or en is None:
            continue
        if flags.get("travel"):
            if s <= now < en:
                travel = cv(flags["travel"]) if isinstance(flags["travel"], str) else "yes"
            continue
        st = "during" if s <= now < en else "before" if 0 < s - now <= 7200 else "after" if 0 <= now - en <= 10800 else None
        if st and rank[st] > rank.get(state, 0):
            state = st
        if s < day_end and en > day_start:
            busy += 0 if e.get("free") else 1
            night = night or bool(flags.get("busy_evening"))
    c = {"event": state or "none", "busy": "free" if busy == 0 else "some" if busy <= 2 else "busy",
         "office_day": office or zone_now == "work", "event_night": night}
    if travel:
        c["travel"] = travel
    return c


def _slow(now):
    """Slower sources (subprocess), refreshed at most every 10 min."""
    s = _json(SLOW_FILE, {}) or {}
    if now - (s.get("at") or 0) < 600 or os.environ.get("USAGE_NO_SUBPROC") == "1":
        return s.get("ctx") or {}
    c = {}
    try:
        r = subprocess.run(["sleepin", "status", "--json"], capture_output=True, text=True, timeout=6)
        if r.returncode == 0:
            c["sleepin"] = bool(json.loads(r.stdout).get("active"))
    except Exception:
        pass
    _atomic(SLOW_FILE, {"at": now, "ctx": c})
    return c


def build_ctx(now=None):
    now = time.time() if now is None else now
    c = {}
    c.update(_phone(now))
    if "zone" not in c:
        ps = _json(PLACE_STATE) or {}
        if isinstance(ps, dict) and cv(ps.get("zone")):
            c["zone"], c["place_src"] = cv(ps["zone"]).lower(), "remind"
    c.update(_calendar(now, c.get("zone")))
    c.update(_slow(now))
    return {k: v for k, v in c.items() if v is not None}


def _atomic(path, obj):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, separators=(",", ":"), sort_keys=True)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def refresh_ctx(con, now=None):
    now = time.time() if now is None else now
    c = build_ctx(now)
    _atomic(CTX_FILE, {"at": now, "ctx": c})
    js = json.dumps(c, sort_keys=True, separators=(",", ":"))
    last = con.execute("SELECT t_from, ctx FROM ctx_log ORDER BY t_from DESC LIMIT 1").fetchone()
    if last and last[1] == js:
        con.execute("UPDATE ctx_log SET t_to=? WHERE t_from=?", (now, last[0]))
    else:
        con.execute("INSERT OR REPLACE INTO ctx_log(t_from, t_to, ctx) VALUES(?,?,?)", (now, now, js))
    return c


_SNAP = {"key": None, "val": {}}


def snapshot_at(con, ts):
    """The context snapshot valid at ts (taken at most 10 min before and confirmed until >= ts - 10 min)."""
    row = con.execute("SELECT t_from, t_to, ctx FROM ctx_log WHERE t_from <= ? ORDER BY t_from DESC LIMIT 1",
                      (ts + 60,)).fetchone()
    if not row or ts - row[1] > 600:
        return {}
    if _SNAP["key"] != row[0]:
        _SNAP["key"], _SNAP["val"] = row[0], json.loads(row[2])
    return dict(_SNAP["val"])


# ---------------------------------------------------------------- store

SCHEMA = """
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts REAL NOT NULL, day TEXT NOT NULL, feature TEXT NOT NULL,
  kind TEXT NOT NULL, channel TEXT, actor TEXT, src TEXT NOT NULL, hour INTEGER, dow INTEGER, zone TEXT, ms INTEGER,
  outcome TEXT, ref TEXT, turn TEXT, ext_id TEXT UNIQUE, ctx TEXT NOT NULL DEFAULT '{}', attrs TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS ev_ts ON events(ts);
CREATE INDEX IF NOT EXISTS ev_feature ON events(feature, ts);
CREATE INDEX IF NOT EXISTS ev_kind ON events(kind, ts);
CREATE INDEX IF NOT EXISTS ev_ref ON events(ref);
CREATE TABLE IF NOT EXISTS runs(day TEXT, feature TEXT, actor TEXT, channel TEXT, n INTEGER DEFAULT 0,
  fails INTEGER DEFAULT 0, ms_sum INTEGER DEFAULT 0, ms_max INTEGER DEFAULT 0, ms_n INTEGER DEFAULT 0,
  hist TEXT DEFAULT '[0,0,0,0,0,0,0]', PRIMARY KEY(day, feature, actor, channel));
CREATE TABLE IF NOT EXISTS ctx_log(t_from REAL PRIMARY KEY, t_to REAL NOT NULL, ctx TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE INDEX IF NOT EXISTS ev_turn ON events(turn);
CREATE TABLE IF NOT EXISTS turns(turn TEXT PRIMARY KEY, ts REAL NOT NULL, day TEXT NOT NULL, ch TEXT, start TEXT,
  prefix TEXT, dev TEXT, mix TEXT NOT NULL DEFAULT '{}', label TEXT, primary_f TEXT);
CREATE INDEX IF NOT EXISTS turns_ts ON turns(ts);
"""
TURNS_V = 1          # bump to rebuild the turns table (rescans the transcripts from the start once)
# verbs that maintain a feature rather than use it (an exec with one of them in his turn is not his usage)
MAINT_VERBS = {"admin", "deploy", "doctor", "test", "tests", "selftest", "health", "init", "install", "setup", "gc",
               "index", "lint", "version", "logs", "rotate", "backup", "restore", "ensure", "restart", "start", "stop",
               "tick", "collect", "backfill", "build", "release", "help", "canary", "audit"}


def connect(ro=False):
    if ro:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
        con.row_factory = sqlite3.Row
        return con
    os.makedirs(STATE, mode=0o700, exist_ok=True)
    con = sqlite3.connect(DB, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    os.chmod(DB, 0o600)
    con.row_factory = sqlite3.Row
    return con


def mget(con, k, default=None):
    r = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return json.loads(r[0]) if r else default


def mset(con, k, v):
    con.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?,?)", (k, json.dumps(v, separators=(",", ":"))))


def put(con, ev, idx=None):
    """Insert (or merge into the row with the same ext_id) one event dict."""
    ts = float(ev["ts"])
    feat = resolve(ev["feature"], idx, (ev.get("attrs") or {}).get("verb"))
    if not feat or ev.get("kind") not in KINDS:
        return False
    d, ctx = time_ctx(ts)
    ctx.update(snapshot_at(con, ts))
    ctx.update({k: v for k, v in (ev.get("ctx") or {}).items() if cv(v) is not None or isinstance(v, bool)})
    attrs = clean_attrs(ev.get("attrs"))
    ms = ev.get("ms")
    ms = int(ms) if isinstance(ms, (int, float)) and 0 <= ms < 1e9 else None
    con.execute("""INSERT INTO events(ts, day, feature, kind, channel, actor, src, hour, dow, zone, ms, outcome, ref,
        turn, ext_id, ctx, attrs) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(ext_id) DO UPDATE SET ctx=json_patch(events.ctx, excluded.ctx),
          attrs=json_patch(events.attrs, excluded.attrs), turn=coalesce(events.turn, excluded.turn),
          outcome=coalesce(excluded.outcome, events.outcome), ms=coalesce(events.ms, excluded.ms)""",
                (ts, d.date().isoformat(), feat, ev["kind"], ev.get("channel"), ev.get("actor"), ev.get("src") or "?",
                 d.hour, d.weekday(), ctx.get("zone"), ms, tok(ev.get("outcome"), 32), tok(ev.get("ref"), 64),
                 tok(ev.get("turn")), _ext(ev.get("ext_id")), json.dumps(ctx, separators=(",", ":")),
                 json.dumps(attrs, separators=(",", ":"))))
    return True


def _ext(x):
    """A dedupe key that always survives sanitizing (an unusual one is hashed, never dropped)."""
    if x is None:
        return None
    return tok(str(x), 96) or "h:" + _h(str(x))


class Runs:
    """Accumulates runs-table increments during one sync, written by flush()."""

    def __init__(self):
        self.acc = {}

    def add(self, ts, feature, actor, channel, ok=True, ms=None, idx=None, verb=None):
        f = resolve(feature, idx, verb)
        if not f:
            return
        day = local(ts)[0].date().isoformat()
        a = self.acc.setdefault((day, f, actor or "?", channel or "?"), [0, 0, 0, 0, 0, [0] * 7])
        a[0] += 1
        a[1] += 0 if ok else 1
        if isinstance(ms, (int, float)) and ms >= 0:
            a[2] += int(ms)
            a[3] = max(a[3], int(ms))
            a[4] += 1
            a[5][sum(1 for c in HIST if ms >= c)] += 1

    def flush(self, con):
        for (day, f, actor, ch), (n, fails, s, mx, mn, h) in self.acc.items():
            row = con.execute("SELECT n, fails, ms_sum, ms_max, ms_n, hist FROM runs WHERE day=? AND feature=? AND "
                              "actor=? AND channel=?", (day, f, actor, ch)).fetchone()
            if row:
                oh = json.loads(row["hist"])
                h = [x + y for x, y in zip(oh, h)]
                n, fails, s, mx, mn = n + row["n"], fails + row["fails"], s + row["ms_sum"], max(mx, row["ms_max"]), \
                    mn + row["ms_n"]
            con.execute("INSERT OR REPLACE INTO runs(day, feature, actor, channel, n, fails, ms_sum, ms_max, ms_n, hist) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)", (day, f, actor, ch, n, fails, s, mx, mn, json.dumps(h)))
        self.acc = {}


# ---------------------------------------------------------------- catalogue index (written by `product catalogue`)

def load_index():
    j = _json(CATALOGUE, {}) or {}
    idx = j.get("index") or {}
    for k in ("scripts", "verbs", "buttons", "skills", "jobs", "voice", "app"):
        idx.setdefault(k, {})
    fs = [f for f in j.get("features") or [] if isinstance(f, dict)]
    idx["features"] = {f.get("id") for f in fs}
    # features a turn can be "about": owner-facing, not a bare script or notifier
    idx["facing"] = {f.get("id") for f in fs if f.get("owner_facing") and f.get("kind") not in ("script", "notifier")}
    return idx


def facing(f, idx=None):
    if idx and idx.get("facing"):
        return f in idx["facing"]
    return bool(f) and f.startswith(("skill.", "tg.", "voice.", "app."))


def resolve(f, idx=None, verb=None):
    f = norm_feature(f)
    if not f or not idx:
        return f
    head, _, rest = f.partition(".")
    if head == "bin":
        return idx["verbs"].get(f"{rest} {verb}") or idx["scripts"].get(rest) or f
    if head == "btn":                       # a known button family, else one of my ad-hoc question buttons
        parts = rest.split(":")
        return idx["buttons"].get(":".join(parts[:2])) or idx["buttons"].get(parts[0]) or "session.buttons"
    if head == "job":
        return idx["jobs"].get(rest) or f
    if head == "skill":
        return idx["skills"].get(rest) or f
    return f


def remap(con, idx):
    """Re-resolve rows stored before the catalogue knew their feature (bin.* scripts, question-button prefixes)."""
    n = 0
    for (f,) in con.execute("SELECT DISTINCT feature FROM events WHERE feature LIKE 'bin.%'").fetchall():
        to = resolve(f, idx)
        if to != f:
            n += con.execute("UPDATE events SET feature=? WHERE feature=? AND json_extract(attrs,'$.verb') IS NULL",
                             (to, f)).rowcount
    for p, in con.execute("SELECT DISTINCT json_extract(attrs,'$.prefix') FROM events WHERE feature='session.buttons'"):
        to = resolve("btn." + str(p), idx) if p else None
        if to and to != "session.buttons":
            n += con.execute("UPDATE events SET feature=? WHERE feature='session.buttons' AND "
                             "json_extract(attrs,'$.prefix')=?", (to, p)).rowcount
    return n


STREETISH = re.compile(r"\d|(strasse|straße|str\.|gasse|weg|platz|allee|rain|ring|halde|steig|quai)\b|"
                       r"\b(road|street|avenue|rue|via|chemin|lane|drive|boulevard)\b", re.I)


def coarse_town(v):
    """A town name or nothing: anything that looks like a street or has a number is dropped (never store places)."""
    v = cv(v)
    return None if v is None or len(str(v)) > 40 or STREETISH.search(str(v)) else v


# ---------------------------------------------------------------- telegram owner events (journal + transcripts)

CMD_RE = re.compile(r"^/([A-Za-z0-9_]{1,32})(?:@\w+)?(?:\s|$)")
FB = (("correction", re.compile(r"^\s*(no|nope|nein|nö|wrong|falsch|not that|nicht das|stimmt nicht)\b|\b(i said|i meant|"
                                r"ich (hab|habe) (doch )?gesagt|ich meinte|that'?s not what|das ist nicht)\b", re.I)),
      ("complaint", re.compile(r"\b(doesn'?t work|not working|broken|geht nicht|funktioniert nicht|klappt nicht|still "
                               r"(not|no|doesn'?t|broken)|immer noch( nicht)?|schon wieder|why (did|didn'?t|is|isn'?t|are) "
                               r"(you|it|they)|warum (hast|ist|geht|kommt)|annoying|nervt|too many|zu viele|stop "
                               r"(sending|pinging|calling)|hör auf|spam)\b", re.I)),
      ("praise", re.compile(r"^\W*(thanks|thank you|danke|merci|perfect|perfekt|great|super|nice|cool|awesome|genial|"
                            r"love it|works|it worked|funktioniert)\b", re.I)))


def feedback(text):
    t = (text or "")[:600]
    for name, rx in FB:
        if rx.search(t):
            return name
    return None


def button_prefix(data):
    parts = (data or "").split(":")
    if not parts[0]:
        return None
    p = parts[0][:24]
    if len(parts) > 1 and re.fullmatch(r"[a-z_]{1,12}", parts[1]):
        p += ":" + parts[1]
    return p


def _len_band(t):
    n = len(t or "")
    return "s" if n < 40 else "m" if n < 200 else "l"


def tg_events(kind, content, meta, src):
    """Owner Telegram events from one journal row or channel tag. Only structure and derived flags are kept."""
    meta = meta or {}
    ts = iso(meta.get("ts"))
    if ts is None:
        return []
    via = meta.get("via")
    if via in ("voice", "app"):
        return []                                   # relay hand-offs / app chat: the call log and phonegw have them
    mid = tok(meta.get("message_id"), 24) or "-"
    ch = "app" if via == "app" else "telegram"
    base = {"ts": ts, "channel": ch, "actor": "owner", "src": src}
    et = meta.get("event_type") or kind
    if et == "button":
        data = meta.get("callback_data") or ""
        p = button_prefix(data)
        if not p or p == "call:talk":
            return []                               # 📞 Talk: talk-button's own log is the source
        return [dict(base, feature="btn." + p, kind="tap", ref=f"tg:{mid}",
                     ext_id=f"tg:tap:{mid}:{int(ts)}:{_h(data)}",
                     attrs={"prefix": tok(p), "dismiss": bool(DISMISS.search(data))})]
    if et == "reaction":
        target = tok(meta.get("reacted_to_message_id"), 24) or "-"
        emo = EMOJI.get(meta.get("reaction"), "other")
        return [dict(base, feature="tg.reaction", kind="react", ref=f"tg:{target}",
                     ext_id=f"tg:react:{target}:{int(ts)}:{emo}", attrs={"emoji": emo})]
    if et == "poll_answer":
        return [dict(base, feature="tg.poll", kind="answer", ref=f"tg:{mid}", ext_id=f"tg:poll:{mid}:{int(ts)}")]
    if et == "stop":
        return [dict(base, feature="session.draft", kind="abandoned", ref=f"tg:{mid}", ext_id=f"tg:stop:{mid}:{int(ts)}")]
    if kind in ("instant", "status") or meta.get("handled"):
        cmd = tok(str(meta.get("handled") or "status").lstrip("/"), 32) or "unknown"
        return [dict(base, feature=f"tg.{cmd}".lower(), kind="invoke", ext_id=f"tg:cmd:{mid}:{int(ts)}",
                     attrs={"instant": True})]
    text = content or ""
    m = CMD_RE.match(text)
    if meta.get("transcript"):
        modality = "voice_note"
    elif meta.get("image_path"):
        modality = "photo"
    elif meta.get("attachment_kind"):
        modality = tok(meta.get("attachment_kind"), 16) or "file"
    else:
        modality = "app_chat" if via == "app" else "text"
    attrs = {"modality": modality, "len": _len_band(text)}
    fb = feedback(text)
    if fb:
        attrs["fb"] = fb
    if meta.get("instant_error") or meta.get("status_error"):
        attrs["instant_failed"] = True
    ref = f"tg:{tok(meta.get('reply_to_message_id'), 24)}" if meta.get("reply_to_message_id") else None
    if m:
        feat, k = f"tg.{m.group(1).lower()}", "invoke"
    else:
        feat, k = f"chat.{ch}", ("reply" if ref else "msg")
    return [dict(base, feature=feat, kind=k, ref=ref, ext_id=f"tg:msg:{mid}:{int(ts)}", attrs=attrs)]


def _h(s):
    import hashlib
    return hashlib.sha1((s or "").encode()).hexdigest()[:8]


def collect_journal(con, idx):
    n = 0
    try:
        with open(JOURNAL, errors="replace") as f:
            lines = f.readlines()
    except Exception:
        return 0
    for line in lines:
        try:
            r = json.loads(line)
        except Exception:
            continue
        meta = r.get("meta") or {}
        if not r.get("owner") and meta.get("via") != "app":
            continue
        for ev in tg_events(r.get("kind"), r.get("content"), meta, "journal"):
            n += put(con, ev, idx)
    return n


EV_KINDS = {"tg.message": "message", "tg.button": "button", "tg.reaction": "reaction", "tg.poll_answer": "poll_answer",
            "tg.stop": "stop"}


def collect_events(con, idx):
    """Owner Telegram events from the events ledger (tg-adapter's rows; only allowlisted senders get one). The
    payload is the 0.1.22 channel meta + text, so tg_events reads it as it reads a journal row."""
    if not os.path.exists(EVENTS_DB):
        return 0
    last = int(mget(con, "events_id", 0) or 0)
    ev = sqlite3.connect(f"file:{EVENTS_DB}?mode=ro", uri=True, timeout=5)
    try:
        rows = ev.execute("SELECT id, kind, payload, ack_by FROM events WHERE id > ? AND source = 'tg-adapter' AND kind IN"
                          f" ({', '.join('?' * len(EV_KINDS))}) ORDER BY id", (last, *EV_KINDS)).fetchall()
    finally:
        ev.close()
    n = 0
    for rid, kind, payload, ack_by in rows:
        last = max(last, rid)
        try:
            meta = json.loads(payload or "{}")
        except ValueError:
            continue
        k = EV_KINDS[kind]
        text = meta.get("text") or ""
        if k == "message" and ack_by == "binding":     # answered by a binding: an instant command
            m = CMD_RE.match(text)
            if m:
                k, meta = "instant", dict(meta, handled=m.group(1).lower())
        for e in tg_events(k, text, meta, "events"):
            n += put(con, e, idx)
    mset(con, "events_id", last)
    return n


def _hub():
    """core.hub while the Telegram transport runs on the hub (hub-tg; USAGE_HUB=on|off overrides), else None."""
    if HUB == "off" or (HUB != "on" and (os.environ.get("USAGE_JOURNAL") or os.environ.get("USAGE_EVENTS_DB"))):
        return None
    try:
        lib = os.path.join(HOME, "bin", "lib")
        if lib not in sys.path:
            sys.path.insert(0, lib)
        from core import hub
        return hub if HUB == "on" or hub.tg_hub() else None
    except Exception:
        return None


def collect_hub(con, idx):
    """hub-tg (v2 H6): tg-adapter's rows on the hub (core.hub.tg_journal: journal entries of the EV_KINDS rows), read
    like collect_events reads the ledger; the same ext_ids as the ledger's and the journal's, so the three dedupe (the
    cut-over copied the ledger's recent rows to the hub). A hub error fails this step; hub_ts stays, the next tick
    reads the window again."""
    hub = _hub()
    if hub is None:
        return 0
    now = time.time()
    since = max(float(mget(con, "hub_ts", 0) or 0) - HUB_OVERLAP_S, now - 3 * 86400)
    n = 0
    for e in hub.tg_journal(since, now + 60, kinds=tuple(EV_KINDS), timeout=10.0):
        meta, k, text = e.get("meta") or {}, e.get("kind"), e.get("content") or ""
        m = CMD_RE.match(text) if k == "instant" else None
        if m:                                           # answered by a binding: the command, as collect_events names it
            meta = dict(meta, handled=m.group(1).lower())
        for ev in tg_events(k, text, meta, "hub"):
            n += put(con, ev, idx)
    mset(con, "hub_ts", now)
    return n


# ---------------------------------------------------------------- transcripts (main session only)

TAG = re.compile(r"<channel\s+([^>]*)>(.*?)</channel>", re.S)
ATTR = re.compile(r'(\w+)="([^"]*)"')
MEMFILES = {"FACTS.md": "mem.facts", "TODO.md": "mem.todo", "CLAUDE.md": "mem.claude_md", "GOALS.md": "mem.goals",
            "PROBLEMS.md": "mem.problems", "TOOLS.md": "mem.docs", "JOBS.md": "mem.docs", "MEMORY.md": "mem.memory",
            "PROACTIVE.md": "mem.log", "CHANGELOG.md": "mem.log"}
TG_SEND = {"reply": "session.reply", "send_poll": "session.poll"}


def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _trigger(entry, text):
    """(trigger, channel) when this user entry starts a turn, else None (a continuation: keep the current one)."""
    o = entry.get("origin")
    k = o.get("kind") if isinstance(o, dict) else None
    to = entry.get("turnOrigin")
    head = text.lstrip()[:120]
    if head.startswith("[heartbeat]"):
        return "heartbeat", None                  # also the restart waker, which types into the terminal
    if head.startswith("This session is being continued from a previous conversation"):
        return "system", None                     # compaction summary
    if k == "channel" or to == "peer" or "<channel source=" in text[:300]:
        m = TAG.search(text)
        via = dict(ATTR.findall(m.group(1))).get("via") if m else None
        return "owner", ("voice" if via == "voice" else "app" if via == "app" else "telegram")
    if k == "human" or to == "human" or entry.get("promptSource") == "typed":
        return "owner", "terminal"
    if k == "task-notification" or to == "task_notification" or "<task-notification>" in text[:200]:
        return "agent", None
    if text.lstrip().startswith("[heartbeat]") or k == "plugin":
        return "heartbeat", None
    return None


def _tool_events(b, ts, st, turn):
    """Events for one assistant tool_use block (main session)."""
    name, inp, tu = b.get("name") or "", b.get("input") or {}, b.get("id")
    base = {"ts": ts, "kind": "exec", "actor": "assistant", "channel": "session", "src": "transcript", "turn": turn,
            "ctx": {"trigger": st["trigger"]} | ({"trigger_ch": st["tch"]} if st.get("tch") else {})}
    out = []
    if name == "Bash":
        for i, (s, verb) in enumerate(scripts(inp.get("command") or "")):
            out.append(dict(base, feature="bin." + s, ext_id=f"tu:{tu}:{i}", attrs={"verb": verb} if verb else {}))
    elif name == "Skill":
        out.append(dict(base, feature="skill." + str(tok(inp.get("skill")) or "unknown"), ext_id=f"tu:{tu}"))
    elif name == "Agent":
        out.append(dict(base, feature="arch.subagent", ext_id=f"tu:{tu}",
                        attrs={"type": tok(inp.get("subagent_type")) or "general", "effort": tok(inp.get("effort")),
                               "bg": bool(inp.get("run_in_background"))}))
    elif name in ("Read", "Edit", "Write", "MultiEdit"):
        p = str(inp.get("file_path") or "")
        f = MEMFILES.get(os.path.basename(p)) or ("mem.memory" if "/memory/" in p else
                                                  "mem.skills" if "/.claude/skills/" in p else None)
        if f:
            out.append(dict(base, feature=f, ext_id=f"tu:{tu}", attrs={"op": name.lower()}))
    elif name in ("WebSearch", "WebFetch"):
        out.append(dict(base, feature="arch.web", ext_id=f"tu:{tu}", attrs={"op": name.lower()}))
    elif name.startswith("mcp__browser__"):
        out.append(dict(base, feature="skill.browser", ext_id=f"tu:{tu}"))
    elif name.startswith("mcp__claude_ai_Google_Calendar__"):
        out.append(dict(base, feature="skill.calendar", ext_id=f"tu:{tu}", attrs={"verb": tok(name.rsplit("__", 1)[1])}))
    elif "_telegram__" in name:
        verb = name.rsplit("__", 1)[1]
        if verb in TG_SEND:
            st["pending"][tu] = {"ts": ts, "turn": turn, "trigger": st["trigger"], "f": TG_SEND[verb],
                                 "buttons": bool(inp.get("buttons")), "files": bool(inp.get("files"))}
        elif verb == "react":
            out.append(dict(base, feature="session.react", ext_id=f"tu:{tu}"))
    return out


# ---- owner turns: how a turn started + what my tool calls in it were (counts only; the label comes at derive time)
TEST_RE = re.compile(r"(?:^|[\s/;&|(])(?:run\.sh|pytest|unittest|test_[\w-]*\.py|tests?\.sh|test-all|selftest|"
                     r"--selftest|gradlew?\s+[^;&|\n]*\btest\w*|npm\s+(?:run\s+)?test)\b")
OPS_RE = re.compile(r"(?:^|[\s;&|(])(?:brain-commit|codebase-sync|crontab\s|restart\s|sudo\s|chmod\s|pip3?\s+install|"
                    r"npm\s+(?:i|install|ci|run\s+build)\b|apt(?:-get)?\s+install|git\s+(?:commit|push|add|tag|merge)\b|"
                    r"gradlew?\s|bin/release|docker\s|systemctl\s|mv\s[^;&|\n]*(?:/bin/|/bin\s|/plugins/|/hooks/))")
ACK = re.compile(r"^\W*(ok(ay)?[\s,.!]+)?(i\s+)?(did it|done|started( it)?|accepted|locked|finished|fixed|ready|"
                 r"installed|restarted|erledigt|fertig|gemacht|gestartet|ok(ay)?|kk?|yes|ja|go|should be up( again)?|"
                 r"✅|👍)\W*$", re.I)
DEV = (("test", re.compile(r"\b(tests?|testing|teste[nt]?|selftest|dry[ -]?run|so i can (watch|see)|probier\w*|"
                           r"ausprobier\w*)\b", re.I)),
       ("build", re.compile(r"\b(build|bau(e|en|st)?|implement\w*|deploy\w*|install\w*|refactor\w*|debug\w*|bugs?|"
                            r"scripts?|commit\w*|apk|plugins?|hooks?|cron\w*|restart\w*|neustart\w*|repo|subagents?|"
                            r"prototype|set ?up|setting up|einricht\w*|readme|docs|policy|codebase|deployment|ssh|"
                            r"tokens?|rotat\w*|refactor\w*|log ?in|pairing)\b", re.I)))
CODE_PATH = re.compile(r"/(bin|opt|plugins|\.claude/(skills|agents|hooks|settings))(/|\.json|$)")


def dev_flag(text):
    """'ack' (a bare "done" / "ok" / "started") | 'test' | 'build' | None from the words of his message (the text
    itself is never kept)."""
    t = (text or "")[:1500]
    if len(t) < 40 and ACK.match(t):
        return "ack"
    for name, rx in DEV:
        if rx.search(t):
            return name
    return None


def turn_start(text, ch):
    """(start, prefix, dev) of the message that starts an owner turn: structure + derived flags, no text."""
    attrs, body = {}, text or ""
    if ch != "terminal":
        m = TAG.search(body)
        if m:
            attrs = {k: html.unescape(v) for k, v in ATTR.findall(m.group(1))}
            body = html.unescape(m.group(2).strip())
    et, prefix, start = attrs.get("event_type"), None, None
    cm = CMD_RE.match(body) if ch != "terminal" else None
    if et == "button":
        start, prefix = "tap", button_prefix(attrs.get("callback_data"))
    elif et in ("reaction", "poll_answer", "stop"):
        start = {"reaction": "reaction", "poll_answer": "poll", "stop": "stop"}[et]
    elif attrs.get("handled"):
        start, prefix = "command", tok(str(attrs["handled"]).lstrip("/").lower(), 32)
    elif cm:
        start, prefix = "command", cm.group(1).lower()
    elif ch == "voice":
        start = "voice_call"
    elif ch == "app":
        start = "app_chat"
    elif attrs.get("transcript"):
        start = "voice_note"
    elif attrs.get("image_path"):
        start = "photo"
    elif attrs.get("attachment_kind") or attrs.get("attachment_file_id"):
        start = "file"
    else:
        start = "terminal" if ch == "terminal" else "text"
    dev = dev_flag(body) if start in ("voice_call", "app_chat", "voice_note", "photo", "file", "text", "terminal") else None
    return start, tok(prefix, 32) if prefix else None, dev


def tool_cats(b, idx=None):
    """[(category, feature or None)] for one assistant tool_use block: what kind of work it is."""
    name, inp = b.get("name") or "", b.get("input") or {}
    if name == "Bash":
        cmd = str(inp.get("command") or "")
        out = [("test", None)] if TEST_RE.search(cmd) else [("ops", None)] if OPS_RE.search(cmd) else []
        for s, verb in scripts(cmd):
            f = resolve("bin." + s, idx, verb)
            out.append(("maint" if verb in MAINT_VERBS else "use" if facing(f, idx) else "tool", None))
        return out or [("shell", None)]
    if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        p = str(inp.get("file_path") or inp.get("notebook_path") or "")
        if MEMFILES.get(os.path.basename(p)) or "/memory/" in p:
            return [("mem", None)]
        if p.startswith(("/tmp/", "/var/tmp/")) and not CODE_PATH.search(p):
            return [("tool", None)]
        m = re.search(r"/\.claude/skills/([^/]+)/", p) or re.search(r"/bin/([A-Za-z0-9._-]+)$", p)
        f = None
        if m:
            f = ("skill." + m.group(1)) if "/skills/" in m.group(0) else resolve("bin." + m.group(1), idx)
        return [("edit", f if f and facing(f, idx) else None)]
    if name in ("Read", "Grep", "Glob"):
        return [("read", None)]
    if name == "Skill" or name.startswith(("mcp__browser__", "mcp__claude_ai_Google_Calendar__")):
        return [("use", None)]
    if name in ("Agent", "SendMessage", "TaskStop", "Task"):
        return [("agent", None)]
    if name in ("WebSearch", "WebFetch"):
        return [("web", None)]
    if "_telegram__" in name:
        return [("reply", None)]
    return [("tool", None)]


def classify_turn(start, known_tap, dev, mix):
    """use | build | test | maint for one owner turn. A tap on a feature's own button, a / command or a live-voice
    ask is a use unless the work is mostly building; test/build words in his message make it work on the feature;
    otherwise the turn is building when my edits, tests, deploys and maintenance runs outnumber feature uses."""
    m = mix or {}
    t, mt = m.get("test", 0), m.get("maint", 0)
    e = m.get("edit", 0) + m.get("ops", 0) + m.get("agent", 0)
    B, U = t + mt + e, m.get("use", 0)

    def sub():
        if dev == "test" or (t and t >= e and t >= mt):
            return "test"
        return "maint" if mt > e + t else "build"
    if known_tap or start in ("command", "voice_call"):
        return sub() if B >= 3 and B > 2 * U else "use"
    if dev == "test":
        return "test"
    if dev == "build":
        return sub()
    return sub() if B >= 2 and B >= U else "use"


def _turn_row(con, turn, ts, ch, text):
    start, prefix, dev = turn_start(text, ch)
    con.execute("INSERT OR IGNORE INTO turns(turn, ts, day, ch, start, prefix, dev) VALUES(?,?,?,?,?,?,?)",
                (turn, ts, local(ts)[0].date().isoformat(), ch, start, prefix, dev))


def _flush_mix(con, mixd):
    for turn, cnt in mixd.items():
        r = con.execute("SELECT mix FROM turns WHERE turn=?", (turn,)).fetchone()
        if not r:
            continue
        m = json.loads(r[0] or "{}")
        fx = m.pop("fx", {})
        for k, v in cnt.items():
            if k.startswith("fx:"):
                fx[k[3:]] = fx.get(k[3:], 0) + v
            else:
                m[k] = m.get(k, 0) + v
        if fx:
            m["fx"] = dict(sorted(fx.items(), key=lambda kv: -kv[1])[:6])
        con.execute("UPDATE turns SET mix=?, label=NULL WHERE turn=?", (json.dumps(m, separators=(",", ":")), turn))
    mixd.clear()


SENT = re.compile(r"sent(?: \d+ parts)? \(ids?: ([\d, ]+)\)")


def _results(content, st, idx, con):
    n = 0
    for b in content if isinstance(content, list) else []:
        if not isinstance(b, dict) or b.get("type") != "tool_result":
            continue
        tu = b.get("tool_use_id")
        p = st["pending"].pop(tu, None)
        if p:
            m = SENT.search(_text_of(b.get("content")) if not isinstance(b.get("content"), str) else b["content"])
            if m:
                ids = [x.strip() for x in m.group(1).split(",") if x.strip()]
                kind = "exec" if p["trigger"] == "owner" else "notify_sent"   # an answer in his turn is no notification
                n += put(con, {"ts": p["ts"], "feature": p["f"], "kind": kind, "channel": "telegram",
                               "actor": "assistant", "src": "transcript", "turn": p["turn"], "ref": f"tg:{ids[0]}",
                               "ext_id": f"tgsent:{ids[0]}", "ctx": {"trigger": p["trigger"]},
                               "attrs": {"parts": len(ids), "buttons": p["buttons"], "files": p["files"],
                                         "proactive": p["trigger"] != "owner"}}, idx)
        elif b.get("is_error"):
            con.execute("UPDATE events SET outcome='error' WHERE ext_id=? OR ext_id LIKE ?", (f"tu:{tu}", f"tu:{tu}:%"))
    if len(st["pending"]) > 200:
        st["pending"] = dict(list(st["pending"].items())[-100:])
    return n


def _entry_text(e):
    """(entry, text, content) of a user entry or of an owner message queued mid-turn (attachment queued_command)."""
    if e.get("type") == "attachment":
        a = e.get("attachment") or {}
        if not isinstance(a, dict) or a.get("type") != "queued_command":
            return None
        pr = a.get("prompt")
        return {"origin": a.get("origin"), "uuid": e.get("uuid")}, (pr if isinstance(pr, str) else _text_of(pr)), None
    msg = e.get("message") or {}
    content = msg.get("content") if isinstance(msg, dict) else None
    return e, _text_of(content), content


def collect_transcripts(con, idx, max_bytes=None):
    n = 0
    mixd = {}
    for path in sorted(p for p in (os.path.join(TRANSCRIPTS, x) for x in _ls(TRANSCRIPTS)) if p.endswith(".jsonl")):
        key = "tx:" + os.path.basename(path)
        st = mget(con, key, {"off": 0, "trigger": "system", "tch": None, "turn": None, "pending": {}})
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if size < st["off"]:
            st = {"off": 0, "trigger": "system", "tch": None, "turn": None, "pending": {}}
        if size == st["off"]:
            continue
        for data, end in _chunks(path, st["off"], size, max_bytes):
          for raw in data[:end].split(b"\n"):
            if not raw or not (b'"tool_use"' in raw or b'"tool_result"' in raw or b'"origin"' in raw
                               or b"[heartbeat]" in raw or b"<channel" in raw):
                continue
            try:
                e = json.loads(raw)
            except Exception:
                continue
            if e.get("isSidechain") or e.get("type") not in ("user", "assistant", "attachment"):
                continue
            ts = iso(e.get("timestamp"))
            if ts is None:
                continue
            msg = e.get("message") or {}
            content = msg.get("content") if isinstance(msg, dict) else None
            if e["type"] == "assistant":
                for b in content if isinstance(content, list) else []:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        for ev in _tool_events(b, ts, st, st.get("turn")):
                            n += put(con, ev, idx)
                        if st.get("trigger") == "owner" and st.get("turn"):
                            c = mixd.setdefault(st["turn"], {})
                            for cat, f in tool_cats(b, idx):
                                c[cat] = c.get(cat, 0) + 1
                                if f:
                                    c["fx:" + f] = c.get("fx:" + f, 0) + 1
                continue
            if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                n += _results(content, st, idx, con)
                continue
            et = _entry_text(e)
            if et is None:
                continue
            entry, text, _ = et
            trig = _trigger(entry, text)
            if trig:
                st["trigger"], st["tch"] = trig
                st["turn"] = _h(str(entry.get("uuid") or ts))
                if trig[0] == "owner":
                    _turn_row(con, st["turn"], ts, trig[1], text)
            for m in TAG.finditer(text):
                attrs = {k: html.unescape(v) for k, v in ATTR.findall(m.group(1))}
                kind = "instant" if attrs.get("handled") else attrs.get("event_type") or "message"
                for ev in tg_events(kind, html.unescape(m.group(2).strip()), attrs, "transcript"):
                    if trig and trig[0] == "owner":
                        ev["turn"] = st["turn"]
                    n += put(con, ev, idx)
          st["off"] += end
          _flush_mix(con, mixd)
          mset(con, key, st)
    return n


def _chunks(path, off, size, max_bytes=None, chunk=16 << 20):
    """(bytes, end) pieces from off to size; end = after the last newline (a partial last line waits)."""
    with open(path, "rb") as f:
        f.seek(off)
        left = min(size - off, max_bytes or size)
        while left > 0:
            data = f.read(min(chunk, left))
            end = data.rfind(b"\n") + 1
            if not data:
                return
            if end == 0:
                if len(data) == left:
                    return
                chunk *= 2
                f.seek(off)
                continue
            yield data, end
            off += end
            left -= end
            f.seek(off)


def _ls(d):
    try:
        return os.listdir(d)
    except OSError:
        return []


# ---------------------------------------------------------------- spool

def fold_spool(con, idx, runs, now=None):
    n = 0
    now = time.time() if now is None else now
    for name in sorted(_ls(SPOOL)):
        if not name.endswith(".jsonl"):
            continue
        path, key = os.path.join(SPOOL, name), "spool:" + name
        off = mget(con, key, 0)
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if size > off:
            with open(path, "rb") as f:
                f.seek(off)
                data = f.read(size - off)
            end = data.rfind(b"\n") + 1
            for raw in data[:end].split(b"\n"):
                try:
                    n += ingest(con, json.loads(raw), idx, runs) if raw else 0
                except Exception:
                    continue
            mset(con, key, off + end)
        elif now - os.path.getmtime(path) > 14 * 86400:
            os.remove(path)
            con.execute("DELETE FROM meta WHERE k=?", (key,))
    return n


def ingest(con, r, idx, runs):
    src, ts = r.get("src"), float(r.get("ts") or 0)
    if not ts:
        return 0
    if src == "hook":                       # every Bash/Skill call, also subagents: aggregate only
        actor = "subagent" if r.get("agent") else "session"
        if r.get("skill"):
            runs.add(ts, "skill." + str(r["skill"]), actor, "session", idx=idx)
        for s, verb in r.get("c") or []:
            runs.add(ts, "bin." + str(s), actor, "session", idx=idx, verb=verb)
        return 0
    k, f = r.get("k"), r.get("f")
    ok = (r.get("o") or "ok") in OK_OUT
    if k in ("run", "exec"):
        actor = r.get("a") or ("cron" if k == "run" else r.get("ch") or "script")
        runs.add(ts, f, actor, r.get("ch") or "script", ok, r.get("ms"), idx, (r.get("at") or {}).get("verb"))
        if ok:
            return 0
        k = "error"
    ev = {"ts": ts, "feature": f, "kind": k, "channel": r.get("ch"), "actor": r.get("a") or
          ("owner" if k in INTENT else "assistant" if k == "notify_sent" else None), "src": src or "lib",
          "ms": r.get("ms"), "outcome": r.get("o"), "ref": r.get("r"), "ext_id": r.get("x"), "attrs": r.get("at")}
    if k == "error" and not ev["ext_id"]:
        ev["ext_id"] = f"err:{f}:{int(ts)}"
    return put(con, ev, idx)


# ---------------------------------------------------------------- the voice call log (claude-hub-voice)

CALL_EVENTS = (("resumed", "voice.resume", "invoke", "owner"), ("model_fallback", "voice.model_fallback", "error",
               "assistant"), ("partial", "voice.partial", "invoke", "assistant"),
               ("deck", "voice.decision_deck", "invoke", "owner"))   # voice-first: continued call, fallback, --more


def _hub_calls():
    """The newest 50 calls as voice.calls digests, newest last (`voice-hub calls --last 50 --days 14`: kind, start,
    end, ring, tools [[name, lane, error, handoff]], reply_ms, events {ev: count}; no text, reasons or callers).
    None when the hub does not answer or the source is off."""
    if CALLS_CMD in ("off", "0") or (not CALLS_CMD and (os.environ.get("USAGE_JOURNAL") or
                                                         os.environ.get("USAGE_EVENTS_DB"))):
        return None
    argv = CALLS_CMD.split() if CALLS_CMD else [os.path.join(BIN, "voice-hub"), "calls", "--last", "50", "--days",
                                                "14"]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        got = json.loads(r.stdout).get("calls") if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return None
    return [d for d in got if isinstance(d, dict)] if isinstance(got, list) else None


def collect_calls(con, idx, runs):
    """Each call once, when it has ended (or is a day old without an end): meta calls:done keeps the ids taken."""
    digests = _hub_calls()
    if digests is None:
        return 0
    done = mget(con, "calls:done", [])
    seen, n = set(done), 0
    for d in digests:
        start, cid = d.get("start"), d.get("id")
        if not cid or not isinstance(start, (int, float)) or cid in seen:
            continue
        if d.get("end") is None and time.time() - start < 86400:
            continue                                   # still live: taken once it ends
        n += _call_digest(con, idx, runs, d)
        seen.add(cid)
        done.append(cid)
    mset(con, "calls:done", done[-300:])
    return n


def _call_digest(con, idx, runs, d):
    cid, ts, kind = _h(str(d["id"])), float(d["start"]), str(d.get("kind") or "")
    way = "out" if kind.endswith("-out") else "app" if kind == "app" else "in"
    ch, actor = ("app" if kind == "app" else "voice"), ("assistant" if way == "out" else "owner")
    evs = d.get("events") if isinstance(d.get("events"), dict) else {}
    end = d.get("end") if isinstance(d.get("end"), dict) else {}
    ring = d.get("ring") if isinstance(d.get("ring"), list) and d.get("ring") else None
    tools = [t for t in d.get("tools") or [] if isinstance(t, list) and t]
    attrs = {"dir": way}
    if ring:
        attrs["ring"] = tok(ring[0], 24) or "unknown"
    if end:
        dur = end.get("duration_s")
        attrs.update(dur_s=int(dur) if isinstance(dur, (int, float)) else 0, tools=len(tools),
                     end=tok(end.get("reason"), 24))
    if isinstance(d.get("hangup"), list) and d["hangup"]:
        attrs["by"] = tok(d["hangup"][0], 16)
    k = put(con, {"ts": ts, "feature": "voice.call", "kind": "call", "channel": ch, "actor": actor, "src": "calls",
                  "ext_id": f"call:{cid}", "attrs": attrs}, idx)
    if way == "out":                                   # the digest names no caller: voice.call_out
        k += put(con, {"ts": ts, "feature": "voice.call_out", "kind": "notify_sent", "channel": "voice",
                       "actor": "assistant", "src": "calls", "ref": f"call:{cid}", "ext_id": f"callout:{cid}",
                       "attrs": {"call": True}}, idx)
        if ring:
            out = attrs["ring"]
            ring_s = ring[1] if len(ring) > 1 and isinstance(ring[1], (int, float)) else 0
            how = "notify_opened" if out == "answered" else "dismissed" if out in ("declined", "busy") else "ignored"
            k += put(con, {"ts": ts + ring_s, "feature": "voice.call_out", "kind": how, "channel": "voice",
                           "actor": "owner", "src": "calls", "ref": f"call:{cid}", "ext_id": f"resp:callout:{cid}",
                           "ms": ring_s * 1000, "attrs": {"how": "call", "ring": out}}, idx)
    for ms in d.get("reply_ms") or []:
        if isinstance(ms, (int, float)):
            runs.add(ts, "voice.reply", "assistant", ch, True, ms, idx)
    for i, t in enumerate(tools):
        name = tok(t[0], 32) or "unknown"
        feat = "voice.ask_assistant" if name == "ask_assistant" else f"voice.tool.{name}".lower()
        k += put(con, {"ts": ts, "feature": feat, "kind": "invoke", "channel": ch, "actor": "owner", "src": "calls",
                       "outcome": "error" if len(t) > 2 and t[2] else None, "ext_id": f"calltool:{cid}:{i}",
                       "ctx": {"modality": "live_call"}, "attrs": {"lane": tok(t[1], 16) if len(t) > 1 else None,
                                                                   "handoff": bool(len(t) > 3 and t[3])}}, idx)
    for ev, feat, how, who in CALL_EVENTS:
        for i in range(min(int(evs.get(ev) or 0), 20)):
            k += put(con, {"ts": ts, "feature": feat, "kind": how, "channel": ch, "actor": who, "src": "calls",
                           "ext_id": f"call{ev}:{cid}:{i}"}, idx)
    return k


# ---------------------------------------------------------------- phonegw (read-only)

PUSH_TAG = (("rem-", "bin.remind"), ("r:", "bin.remind"), ("alarm:", "bin.remind"), ("link-", "bin.phone"),
            ("live:", "app.live"), ("buds", "app.buds_guard"), ("batt", "app.battery_watch"))
PUSH_SKIP = ("probe", "apptest", "demo", "selftest", "test")
# remind's push rows send nothing here: a reminder's send is its fired.log line (collect_remind; remind takes an
# unconfirmed push back and Telegram sends it), and alarm: rows are the app's local-alarm schedule (synced up to 48 h
# ahead), not notifications. Their Done / Snooze / swipe still count as opened / dismissed.
PUSH_NO_SEND = ("rem-", "r:", "alarm:")


def collect_phonegw(con, idx, runs, now=None):
    now = time.time() if now is None else now
    try:
        pg = sqlite3.connect(f"file:{PHONEGW}?mode=ro", uri=True, timeout=2)
    except Exception:
        return 0
    n, since = 0, now - 7 * 86400
    try:
        for rid, kind, at, body in pg.execute("SELECT id, kind, at, body FROM events WHERE at >= ?", (since,)):
            if kind != "tile":
                continue
            try:
                name = tok(json.loads(body).get("name"), 24) or "unknown"
            except Exception:
                name = "unknown"
            name, _, state = name.lower().partition(":")       # sleepin:on / private:off -> one feature + state
            n += put(con, {"ts": at, "feature": f"app.tile.{name}", "kind": "invoke", "channel": "app",
                           "actor": "owner", "src": "phonegw", "ext_id": f"pg:ev:{rid}",
                           "attrs": {"state": state} if state else None}, idx)
        for rid, at, level, tag, dl, ack, verb, action_at in pg.execute(
                "SELECT id, at, level, tag, delivered_ms, acked_ms, verb, action_at FROM push_log WHERE at >= ?",
                (since * 1000,)):
            tag = str(tag or "")
            if any(tag.startswith(s) for s in PUSH_SKIP) or level == "probe":
                continue
            feat = next((f for p, f in PUSH_TAG if tag.startswith(p)), "app.push")
            ts = at / 1000
            if not tag.startswith(PUSH_NO_SEND):
                n += put(con, {"ts": ts, "feature": feat, "kind": "notify_sent", "channel": "app", "actor": "assistant",
                               "src": "phonegw", "ref": f"push:{_h(str(rid))}", "ext_id": f"pg:push:{rid}",
                               "attrs": {"level": tok(level, 16), "delivered": dl is not None}}, idx)
            if verb or action_at:
                act = (action_at / 1000 if (action_at or 0) > 1e11 else action_at) if action_at else None
                n += put(con, {"ts": act or ts, "feature": feat,          # a swipe-away is no open
                               "kind": "dismissed" if verb == "dismissed" else "notify_opened", "channel": "app",
                               "actor": "owner", "src": "phonegw", "ref": f"push:{_h(str(rid))}",
                               "ext_id": f"resp:pg:push:{rid}", "ms": ((act - ts) * 1000) if act else None,
                               "attrs": {"how": tok(verb, 16) or "tap"}}, idx)
        for rid, kind, created, answered in pg.execute(
                "SELECT id, kind, created, answered FROM cards WHERE created >= ?", (since,)):
            feat = f"app.card.{tok(kind, 16) or 'card'}".lower()
            n += put(con, {"ts": created, "feature": feat, "kind": "notify_sent", "channel": "app", "actor": "assistant",
                           "src": "phonegw", "ref": f"card:{_h(str(rid))}", "ext_id": f"pg:card:{rid}"}, idx)
            if answered:
                n += put(con, {"ts": answered, "feature": feat, "kind": "notify_opened", "channel": "app",
                               "actor": "owner", "src": "phonegw", "ref": f"card:{_h(str(rid))}",
                               "ext_id": f"resp:pg:card:{rid}", "ms": (answered - created) * 1000,
                               "attrs": {"how": "answer"}}, idx)
        for rid, at, d in pg.execute("SELECT id, at, dir FROM chat WHERE at >= ?", (since,)):
            if d == "in":
                ts = at / 1000 if at > 1e11 else at
                n += put(con, {"ts": ts, "feature": "chat.app", "kind": "msg", "channel": "app", "actor": "owner",
                               "src": "phonegw", "ext_id": f"pg:chat:{rid}", "ctx": {"modality": "app_chat"}}, idx)
        mark = mget(con, "pg:inbound", 0)
        top = mark
        for rid, t, at in pg.execute("SELECT id, t, at FROM inbound WHERE at > ? ORDER BY at", (mark,)):
            top = max(top, at)
            ts = at / 1000 if at > 1e11 else at
            if t == "crash":
                n += put(con, {"ts": ts, "feature": "app", "kind": "error", "channel": "app", "actor": "system",
                               "src": "phonegw", "outcome": "crash", "ext_id": f"pg:in:{rid}"}, idx)
            elif t in ("hello", "selftest"):
                runs.add(ts, f"app.{t}", "app", "app", True, None, idx)
        mset(con, "pg:inbound", top)
    except Exception:
        pass
    finally:
        pg.close()
    return n


# ---------------------------------------------------------------- talk-button log

TALK_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) tap -> (\w+)")


FIRED_LINE = re.compile(r"(\d{4}-\d\d-\d\d \d\d:\d\d) (owner|me)")


def _home_tz():
    try:
        sys.path.insert(0, HERE)
        import assistant_profile
        return assistant_profile.tz()
    except Exception:
        return None
    finally:
        if sys.path and sys.path[0] == HERE:
            sys.path.pop(0)


def collect_remind(con, idx, now=None, full=False):
    """remind's fired.log ("YYYY-MM-DD HH:MM owner|me" on his home zone's wall clock, no text): one notify_sent
    (bin.remind) per fired owner reminder, the last 7 days each tick (all with full), lines older than 10 min only
    (the phone mirror catches up first). Channel app when a remind push (tag rem-) that the phone showed (delivered,
    acked or acted on) starts within that minute, else telegram (remind takes an unconfirmed push back)."""
    now = time.time() if now is None else now
    since = 0 if full else now - 7 * 86400
    try:
        with open(REMIND_FIRED, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return 0
    shown = []
    try:
        pg = sqlite3.connect(f"file:{PHONEGW}?mode=ro", uri=True, timeout=2)
        try:
            shown = [r[0] / 1000 for r in pg.execute(
                "SELECT at FROM push_log WHERE tag LIKE 'rem-%' AND at >= ? AND (delivered_ms IS NOT NULL OR "
                "acked_ms IS NOT NULL OR verb IS NOT NULL OR action_at IS NOT NULL)", ((since - 600) * 1000,))]
        finally:
            pg.close()
    except Exception:
        shown = []
    tz, n, k = _home_tz(), 0, {}
    for line in lines:
        m = FIRED_LINE.fullmatch(line.strip())
        if not m or m[2] != "owner":
            continue
        i = k[m[1]] = k.get(m[1], -1) + 1                # the i-th owner reminder fired in that minute
        try:
            d = dt.datetime.strptime(m[1], "%Y-%m-%d %H:%M")
            ts = (d.replace(tzinfo=tz) if tz else d).timestamp()
        except ValueError:
            continue
        if ts < since or ts > now - 600:
            continue
        ch = "app" if i < sum(1 for t in shown if ts - 60 <= t < ts + 180) else "telegram"
        n += put(con, {"ts": ts, "feature": "bin.remind", "kind": "notify_sent", "channel": ch, "actor": "assistant",
                       "src": "remind", "ext_id": "rem:fired:" + re.sub(r"\D", "", m[1]) + f":{i}",
                       "attrs": {"via": ch}}, idx)
    return n


def collect_talk(con, idx):
    n = 0
    try:
        lines = open(TALK_LOG, errors="replace").read().splitlines()
    except Exception:
        return 0
    for line in lines:
        m = TALK_RE.match(line)
        if not m:
            continue
        ts = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
        n += put(con, {"ts": ts, "feature": "voice.talk_button", "kind": "tap", "channel": "telegram", "actor": "owner",
                       "src": "talk-button", "outcome": tok(m.group(2), 24),
                       "ext_id": f"talk:{m.group(1).replace(' ', 'T')}"}, idx)
    return n


# ---------------------------------------------------------------- menu-usage history (backfill only)

def backfill_menu(con, idx, until=None):
    """Adds day counts from menu-usage's store where the exact sources (journal, transcripts) saw fewer, for days
    before `until` (settled days, so a late exact row can't double a gap row). Buttons compare per prefix."""
    j = _json(MENU_COUNTS, {}) or {}
    n = 0
    for day, rec in (j.get("days") or {}).items():
        if until and day >= until:
            continue
        for cmd, kinds in ((rec or {}).get("cmd") or {}).items():
            for kind, cnt in (kinds or {}).items():
                feat = resolve(f"tg.{cmd}".lower(), idx)
                have = con.execute("SELECT count(*) FROM events WHERE feature=? AND day=? AND actor='owner' AND "
                                   "kind='invoke'", (feat, day)).fetchone()[0]
                for i in range(max(0, int(cnt or 0) - have)):
                    ts = iso(f"{day}T12:00:00") or 0
                    n += put(con, {"ts": ts, "feature": feat, "kind": "invoke", "channel": "telegram", "actor": "owner",
                                   "src": "menu-usage", "ext_id": f"mu:{day}:{cmd}:{kind}:{i}",
                                   "attrs": {"approx": True, "instant": kind == "instant"}}, idx)
        for key in ("btn", "buttons"):
            for p, cnt in ((rec or {}).get(key) or {}).items():
                cnt = cnt if isinstance(cnt, int) else sum(v for v in (cnt or {}).values() if isinstance(v, int))
                feat = resolve("btn." + p, idx)
                have = con.execute("SELECT count(*) FROM events WHERE day=? AND kind='tap' AND (json_extract(attrs,"
                                   "'$.prefix')=? OR (?='call:talk' AND feature='voice.talk_button'))",
                                   (day, tok(p), p)).fetchone()[0]
                for i in range(max(0, cnt - have)):
                    n += put(con, {"ts": iso(f"{day}T12:00:00") or 0, "feature": feat, "kind": "tap",
                                   "channel": "telegram", "actor": "owner", "src": "menu-usage",
                                   "ext_id": f"mu:{day}:btn:{p}:{i}", "attrs": {"approx": True, "prefix": tok(p)}},
                                idx)
    return n


# ---------------------------------------------------------------- derived engagement

OPEN_WINDOW = 3600


def derive(con, now=None, since=None):
    """notify_sent (Telegram) -> notify_opened / dismissed (his tap, reaction or reply on that message) or ignored
    (none within an hour of his waking time); session replies get attrs.for = the feature the turn used."""
    now = time.time() if now is None else now
    since = now - 4 * 86400 if since is None else since
    n = 0
    sent = con.execute("SELECT id, ts, feature, ref, ext_id, channel FROM events WHERE kind='notify_sent' AND ts>=? AND "
                       "ref LIKE 'tg:%'", (since,)).fetchall()
    for s in sent:
        resp = "resp:" + (s["ext_id"] or str(s["id"]))
        have = con.execute("SELECT kind FROM events WHERE ext_id=?", (resp,)).fetchone()
        if have and have["kind"] != "ignored":
            continue
        o = con.execute("SELECT id, ts, kind, attrs FROM events WHERE ref=? AND actor='owner' AND ts>=? AND ts<=? "
                        "AND kind IN ('tap','react','reply','answer','invoke') ORDER BY ts LIMIT 1",
                        (s["ref"], s["ts"] - 5, s["ts"] + 86400)).fetchone()
        if o:
            a = json.loads(o["attrs"] or "{}")
            neg = a.get("dismiss") or a.get("emoji") in NEG_EMOJI
            con.execute("DELETE FROM events WHERE ext_id=?", (resp,))
            n += put(con, {"ts": o["ts"], "feature": s["feature"], "kind": "dismissed" if neg else "notify_opened",
                           "channel": s["channel"], "actor": "owner", "src": "derive", "ref": s["ref"], "ext_id": resp,
                           "ms": max(0, (o["ts"] - s["ts"]) * 1000), "attrs": {"how": o["kind"], "emoji": a.get("emoji")}})
            con.execute("UPDATE events SET attrs=json_set(attrs, '$.target', ?) WHERE id=?", (s["feature"], o["id"]))
            continue
        if have:
            continue
        due = s["ts"] + OPEN_WINDOW
        d, _ = local(s["ts"])
        if d.hour < 8:                               # sent in quiet hours: count from 08:00 + 2 h
            due = max(due, d.replace(hour=10, minute=0, second=0).timestamp())
        if now < due:
            continue
        active = con.execute("SELECT count(*) FROM events WHERE actor='owner' AND ts>? AND ts<=?",
                             (s["ts"], due)).fetchone()[0]
        n += put(con, {"ts": due, "feature": s["feature"], "kind": "ignored", "channel": s["channel"], "actor": "owner",
                       "src": "derive", "ref": s["ref"], "ext_id": resp, "attrs": {"active": active > 0}})
    for r in con.execute("SELECT id, turn FROM events WHERE feature IN ('session.reply','session.poll') AND ts>=? AND "
                         "turn IS NOT NULL AND json_extract(attrs, '$.for') IS NULL", (since,)).fetchall():
        f = con.execute("SELECT feature FROM events WHERE turn=? AND kind='exec' AND feature NOT LIKE 'session.%' AND "
                        "feature NOT LIKE 'arch.%' AND feature NOT LIKE 'mem.%' AND feature NOT LIKE 'bin.%' "
                        "ORDER BY ts DESC LIMIT 1", (r["turn"],)).fetchone()
        con.execute("UPDATE events SET attrs=json_set(attrs, '$.for', ?) WHERE id=?", (f[0] if f else "-", r["id"]))
    return n


def _primary(con, turn, mix, idx, skip_maint=True):
    """The one owner-facing feature a turn is mainly about: Skill loads and MCP calls weigh 2, script runs and edits
    of the feature's own files 1; ties go to the earliest."""
    score, first = {}, {}
    for r in con.execute("SELECT feature, ext_id, ts, attrs FROM events WHERE turn=? AND kind='exec' ORDER BY ts",
                         (turn,)):
        f = r["feature"]
        if not facing(f, idx) or (skip_maint and json.loads(r["attrs"] or "{}").get("verb") in MAINT_VERBS):
            continue
        score[f] = score.get(f, 0) + (1 if re.search(r":\d+$", r["ext_id"] or "") else 2)
        first.setdefault(f, r["ts"])
    for f, k in ((mix or {}).get("fx") or {}).items():
        if facing(f, idx):
            score[f] = score.get(f, 0) + k
            first.setdefault(f, float("inf"))
    return max(score, key=lambda f: (score[f], -first[f])) if score else None


def _source_turn(con, t):
    """The owner turn his tap / poll answer / reaction answers (the turn that sent that message), or for a bare "done"
    / "ok" the owner turn before it (within 3 h)."""
    if t["start"] in ("tap", "poll", "reaction"):
        r = con.execute("SELECT ref FROM events WHERE turn=? AND actor='owner' AND kind IN ('tap','answer','react') "
                        "AND ref IS NOT NULL LIMIT 1", (t["turn"],)).fetchone()
        s = r and con.execute("SELECT turn FROM events WHERE ref=? AND feature IN ('session.reply','session.poll') "
                              "AND turn IS NOT NULL LIMIT 1", (r[0],)).fetchone()
        return s and con.execute("SELECT * FROM turns WHERE turn=?", (s[0],)).fetchone()
    if t["dev"] == "ack":
        return con.execute("SELECT * FROM turns WHERE ts < ? AND ts > ? ORDER BY ts DESC LIMIT 1",
                           (t["ts"], t["ts"] - 3 * 3600)).fetchone()
    return None


def classify_turns(con, idx):
    """Label owner turns that are new or changed since the last tick; credit ad-hoc question-button taps that start
    a use turn to the turn's primary feature (attrs.credit)."""
    n = 0
    for t in con.execute("SELECT * FROM turns WHERE label IS NULL ORDER BY ts").fetchall():
        mix = json.loads(t["mix"] or "{}")
        known = None
        if t["start"] == "tap" and t["prefix"]:
            known = resolve("btn." + t["prefix"], idx)
            known = None if known == "session.buttons" else known
        label = classify_turn(t["start"], bool(known), t["dev"], mix)
        cmd = resolve(f"tg.{t['prefix']}", idx) if t["start"] == "command" and t["prefix"] else None
        prim = known or cmd or _primary(con, t["turn"], mix, idx, skip_maint=label == "use")
        src = _source_turn(con, t) if not known and label == "use" else None
        if src and src["label"] and src["label"] != "use":
            label, prim = src["label"], prim or src["primary_f"]   # his answer to my build / setup question
        con.execute("UPDATE turns SET label=?, primary_f=? WHERE turn=?", (label, prim, t["turn"]))
        if t["start"] in ("tap", "poll") and not known:
            if label == "use" and prim:
                con.execute("UPDATE events SET attrs=json_set(attrs, '$.credit', ?) WHERE turn=? AND actor='owner' "
                            "AND kind IN ('tap','answer') AND feature IN ('session.buttons','tg.poll')", (prim, t["turn"]))
            else:
                con.execute("UPDATE events SET attrs=json_remove(attrs, '$.credit') WHERE turn=? AND actor='owner' "
                            "AND kind IN ('tap','answer')", (t["turn"],))
        n += 1
    return n


# ---------------------------------------------------------------- tick / backfill

def tick(backfill=False, now=None, quiet=True):
    """Refresh the context, fold every source, derive engagement. Returns per-source counts."""
    import fcntl
    os.makedirs(STATE, mode=0o700, exist_ok=True)
    lock = open(os.path.join(STATE, "tick.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | (0 if backfill else fcntl.LOCK_NB))
    except BlockingIOError:
        return {"skipped": "locked"}
    now = time.time() if now is None else now
    con = connect()
    idx = load_index()
    runs = Runs()
    out = {}
    try:
        with con:
            refresh_ctx(con, now)
            if mget(con, "turns_v") != TURNS_V:          # new turn model: rescan the transcripts once
                con.execute("DELETE FROM turns")
                con.execute("DELETE FROM meta WHERE k LIKE 'tx:%'")
                mset(con, "turns_v", TURNS_V)
        today = local(now)[0].date()
        settled = (today - dt.timedelta(days=0 if backfill else 1)).isoformat()   # menu gap-fill: days before this
        steps = [("spool", lambda: fold_spool(con, idx, runs, now)), ("events", lambda: collect_events(con, idx)),
                 ("hub", lambda: collect_hub(con, idx)), ("journal", lambda: collect_journal(con, idx)),
                 ("transcripts", lambda: collect_transcripts(con, idx)), ("calls", lambda: collect_calls(con, idx, runs)),
                 ("phonegw", lambda: collect_phonegw(con, idx, runs, now)), ("talk", lambda: collect_talk(con, idx)),
                 ("remind", lambda: collect_remind(con, idx, now, backfill)),
                 ("menu-usage", lambda: backfill_menu(con, idx, settled)), ("turns", lambda: classify_turns(con, idx))]
        for name, fn in steps:
            try:
                with con:
                    out[name] = fn()
                    runs.flush(con)
            except Exception as e:
                out[name] = f"error: {type(e).__name__}: {str(e)[:120]}"
        with con:
            out["remapped"] = remap(con, idx)
            out["derived"] = derive(con, now, 0 if backfill else None)
            mset(con, "last_tick", {"at": now, "out": out})
    finally:
        con.close()
        lock.close()
    return out


def hook_main(stream=None):
    """PostToolUse hook body (Skill|Bash): one spool line, never fails, never prints."""
    try:
        if off():
            return 0
        j = json.load(stream or sys.stdin)
        tool, inp = j.get("tool_name"), j.get("tool_input") or {}
        rec = {"ts": round(time.time(), 3), "src": "hook", "agent": bool(j.get("agent_id"))}
        if tool == "Skill":
            rec["skill"] = tok(inp.get("skill"))
        elif tool == "Bash":
            rec["c"] = scripts(inp.get("command") or "")
        if rec.get("skill") or rec.get("c"):
            _spool(rec)
    except Exception:
        pass
    return 0


def notify_main(src, pid, resp_text, buttons=0):
    """tg / tg-buttons: record notify_sent with the sent message id; feature = cron job or calling ~/bin script."""
    try:
        if off():
            return 0
        mid = ((json.loads(resp_text or "{}") or {}).get("result") or {}).get("message_id")
        if not mid:
            return 0
        job = os.environ.get("USAGE_JOB")
        caller = caller_script(int(pid)) if str(pid).isdigit() else None
        feat = ("job." + job) if job and FEAT.fullmatch(job.lower()) else ("bin." + caller) if caller else "session.tg_shell"
        rec = record(feat, "notify_sent", "telegram", "assistant", ref=f"tg:{mid}", src=src, ext_id=f"tgsent:{mid}",
                     attrs={"buttons": int(buttons or 0), "caller": caller, "job": job})
        if rec:
            _spool(rec)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(hook_main() if sys.argv[1:2] == ["--hook"] else 2)
