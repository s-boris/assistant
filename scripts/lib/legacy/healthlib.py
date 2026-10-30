"""healthlib: the owner's sleep & energy store and insights (token-free, local only).

Data path: phone app (Health Connect, summarised on the phone) -> phonegw `health.daily` events (only when the gateway
config has health on) -> `health ingest` (stdin) -> STORE. Only daily aggregates ever arrive: sleep start/end, minutes
asleep/awake, stage shares, resting HR estimate, steps, workouts (type, start hour, minutes, avg HR). Raw samples stay
on the phone.

Privacy: the store is private health data (dir 0700, file 0600). Its values go only to his own surfaces (the app's
Board line, /sleep, the opt-in weekly recap). Never into notes, memory, logs, usage attrs or public paths; `health
status` prints counts only.

Env (tests): HEALTH_STATE (dir), HEALTH_CONFIG (json), HEALTH_NOW (epoch), HEALTH_TZ (IANA zone; default: tripclock tz,
else the system zone), GYM_DB, SLEEPIN_BIN, TRIPCLOCK_BIN, PHONE_BIN, TG_SOFT_BIN.
"""
import datetime as dt
import json
import os
import sqlite3
import statistics
import subprocess
import time
from zoneinfo import ZoneInfo

HOME = os.environ.get("HOME") or os.path.expanduser("~")
RETENTION_DAYS = 90
BASELINE_NIGHTS = 28
MIN_BASELINE = 7
BOARD_UNTIL_H = 14
DEFAULTS = {"weekly_recap": "off", "board_line": True, "board_until_h": BOARD_UNTIL_H, "retention_days": RETENTION_DAYS}
RECAP_MODES = ("off", "app", "telegram")

SCHEMA = """
CREATE TABLE IF NOT EXISTS days(day TEXT PRIMARY KEY, sleep_start INT, sleep_end INT, sleep_min INT, deep_pct INT,
  rem_pct INT, light_pct INT, awake_min INT, rhr INT, steps INT, workouts TEXT, first_rx INT, updated INT);
CREATE TABLE IF NOT EXISTS nights(day TEXT PRIMARY KEY, late INT, kinds TEXT, tentative INT, updated INT);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


# ---------------------------------------------------------------------------------------------------- plumbing
def state_dir():
    return os.environ.get("HEALTH_STATE") or os.path.join(HOME, ".local", "state", "health")


def db_path():
    return os.path.join(state_dir(), "health.db")


def now():
    v = os.environ.get("HEALTH_NOW")
    return int(v) if v else int(time.time())


def tool(env, name):
    return os.environ.get(env) or os.path.join(HOME, "bin", name)


def config():
    p = os.environ.get("HEALTH_CONFIG") or os.path.join(HOME, ".config", "health", "config.json")
    cfg = dict(DEFAULTS)
    try:
        with open(p) as fh:
            cfg.update(json.load(fh) or {})
    except (OSError, ValueError):
        pass
    if cfg.get("weekly_recap") not in RECAP_MODES:
        cfg["weekly_recap"] = "off"
    return cfg


def zone():
    z = os.environ.get("HEALTH_TZ")
    if not z:
        try:
            p = subprocess.run([tool("TRIPCLOCK_BIN", "tripclock"), "tz"], capture_output=True, text=True, timeout=5,
                               stdin=subprocess.DEVNULL)
            z = p.stdout.strip() if p.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            z = ""
    try:
        return ZoneInfo(z) if z else dt.datetime.now().astimezone().tzinfo
    except Exception:
        return dt.datetime.now().astimezone().tzinfo


def connect(ro=False):
    d = state_dir()
    if ro:
        if not os.path.exists(db_path()):
            return None
        return sqlite3.connect("file:%s?mode=ro" % db_path(), uri=True, timeout=10)
    os.makedirs(d, mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)
    old = os.umask(0o077)
    try:
        con = sqlite3.connect(db_path(), timeout=10)
    finally:
        os.umask(old)
    os.chmod(db_path(), 0o600)
    con.executescript(SCHEMA)
    return con


def _int(v, lo, hi):
    return v if isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi else None


# ---------------------------------------------------------------------------------------------------- ingest
def ingest(body, received=None, con=None):
    """One `health.daily` body (already schema-checked by the gateway; re-checked here) -> upsert. -> day or None."""
    if not isinstance(body, dict) or body.get("kind") != "health.daily":
        return None
    day = str(body.get("day") or "")
    try:
        dt.date.fromisoformat(day)
    except ValueError:
        return None
    rx = int(received or now())
    sl = body.get("sleep") if isinstance(body.get("sleep"), dict) else {}
    wos = []
    for w in body.get("workouts") or []:
        if isinstance(w, dict) and isinstance(w.get("type"), str):
            wos.append({k: w[k] for k in ("type", "min", "avg_hr", "hour") if k in w})
    row = {"sleep_start": _int(sl.get("start"), 0, 4102444800), "sleep_end": _int(sl.get("end"), 0, 4102444800),
           "sleep_min": _int(sl.get("min"), 0, 1440), "deep_pct": _int(sl.get("deep_pct"), 0, 100),
           "rem_pct": _int(sl.get("rem_pct"), 0, 100), "light_pct": _int(sl.get("light_pct"), 0, 100),
           "awake_min": _int(sl.get("awake_min"), 0, 1440), "rhr": _int(body.get("rhr"), 20, 220),
           "steps": _int(body.get("steps"), 0, 10 ** 6), "workouts": json.dumps(wos[:20]) if wos else None}
    own = con is None
    con = con or connect()
    try:
        old = con.execute("SELECT first_rx, sleep_end FROM days WHERE day=?", (day,)).fetchone()
        first_rx = old[0] if old and old[0] and old[1] == row["sleep_end"] else (rx if row["sleep_end"] else None)
        cols = list(row) + ["first_rx", "updated"]
        vals = [row[c] for c in row] + [first_rx, rx]
        con.execute("INSERT INTO days(day, %s) VALUES(?, %s) ON CONFLICT(day) DO UPDATE SET %s" % (
            ", ".join(cols), ", ".join("?" * len(cols)), ", ".join("%s=excluded.%s" % (c, c) for c in cols)), [day] + vals)
        keep = int(config().get("retention_days") or RETENTION_DAYS)
        cut = (dt.date.fromtimestamp(rx) - dt.timedelta(days=keep)).isoformat()
        con.execute("DELETE FROM days WHERE day<?", (cut,))
        con.execute("DELETE FROM nights WHERE day<?", (cut,))
        con.execute("INSERT OR REPLACE INTO meta(k, v) VALUES('last_ingest', ?)", (str(rx),))
        con.commit()
    finally:
        if own:
            con.close()
    return day


def tag_nights(status, con, t=None):
    """`sleepin status --json` -> late-night tags (kinds only, never titles): this morning's sources, tomorrow's
    guarded morning (tentative until that morning). -> number of nights tagged."""
    t = t or now()
    n = 0
    rows = []
    if isinstance(status, dict) and status.get("date"):
        rows.append((status["date"], status.get("sources") or [], 0))
        for a in status.get("ahead") or []:
            if isinstance(a, dict) and a.get("date") and not a.get("off"):
                rows.append((a["date"], a.get("sources") or [], 1))
    today = dt.date.fromtimestamp(t)
    for day, sources, tentative in rows:
        try:
            d = dt.date.fromisoformat(day)
        except ValueError:
            continue
        if d > today + dt.timedelta(days=1):
            continue
        kinds = sorted({str(s.get("kind"))[:12] for s in sources if isinstance(s, dict) and s.get("kind")})
        if not kinds:
            if not tentative:     # a past/current morning with no late night: record a 0 only if not tagged before
                con.execute("INSERT OR IGNORE INTO nights(day, late, kinds, tentative, updated) VALUES(?,?,?,?,?)",
                            (day, 0, "", 0, t))
            continue
        con.execute("INSERT INTO nights(day, late, kinds, tentative, updated) VALUES(?,?,?,?,?) ON CONFLICT(day) DO UPDATE "
                    "SET late=1, kinds=excluded.kinds, tentative=MIN(nights.tentative, excluded.tentative), updated=excluded.updated",
                    (day, 1, ",".join(kinds), tentative, t))
        n += 1
    con.commit()
    return n


def sleepin_status():
    try:
        p = subprocess.run([tool("SLEEPIN_BIN", "sleepin"), "status", "--json", "--days", "2"], capture_output=True,
                           text=True, timeout=15, stdin=subprocess.DEVNULL)
        return json.loads(p.stdout) if p.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def sleepin_holding():
    """True while the sleep-in guard holds (he may still be asleep): the board line waits."""
    try:
        p = subprocess.run([tool("SLEEPIN_BIN", "sleepin"), "active"], capture_output=True, timeout=10, stdin=subprocess.DEVNULL)
        return p.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def gym_days(since):
    """Dates with a logged gym session (gym.db, read-only); empty without the db."""
    p = os.environ.get("GYM_DB") or os.path.join(HOME, ".local", "state", "gym", "gym.db")
    if not os.path.exists(p):
        return set()
    try:
        con = sqlite3.connect("file:%s?mode=ro" % p, uri=True, timeout=5)
        try:
            return {r[0] for r in con.execute("SELECT DISTINCT date FROM sessions WHERE date>=?", (since,))}
        finally:
            con.close()
    except sqlite3.Error:
        return set()


# ---------------------------------------------------------------------------------------------------- load
def load(days=BASELINE_NIGHTS + 14, t=None):
    """-> (rows by day ascending, late-night tags {day: kinds}); empty without a store."""
    t = t or now()
    con = connect(ro=True)
    if con is None:
        return [], {}
    since = (dt.date.fromtimestamp(t) - dt.timedelta(days=days)).isoformat()
    try:
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute("SELECT * FROM days WHERE day>=? ORDER BY day", (since,))]
        tags = {r["day"]: r["kinds"] for r in con.execute("SELECT day, kinds FROM nights WHERE late=1 AND day>=?", (since,))}
    finally:
        con.close()
    for r in rows:
        r["workouts"] = json.loads(r["workouts"]) if r.get("workouts") else []
    return rows, tags


# ---------------------------------------------------------------------------------------------------- insights
def clock_min(epoch, tz):
    """Minutes after 18:00 local (bedtimes around midnight stay ordered: 23:30 -> 330, 00:40 -> 400)."""
    lt = dt.datetime.fromtimestamp(epoch, tz)
    return (lt.hour * 60 + lt.minute - 18 * 60) % 1440


def clock_txt(m18):
    m = (int(round(m18)) + 18 * 60) % 1440
    return "%02d:%02d" % (m // 60, m % 60)


def hm(minutes):
    m = int(round(abs(minutes)))
    return ("%d h %02d" % (m // 60, m % 60)) if m >= 60 else "%d min" % m


def _med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def insights(rows, tags, tz, t=None, gym=None):
    """Pure: rows (ascending days) + late tags -> insight dict (numbers only; texts are built by the surfaces)."""
    t = t or now()
    today = dt.datetime.fromtimestamp(t, tz).date()
    nights = [r for r in rows if r.get("sleep_min") is not None and r["day"] <= today.isoformat()]
    last28 = [r for r in nights if dt.date.fromisoformat(r["day"]) > today - dt.timedelta(days=BASELINE_NIGHTS)]
    last7 = [r for r in nights if dt.date.fromisoformat(r["day"]) > today - dt.timedelta(days=7)]
    prior = [r for r in last28 if r not in last7]
    out = {"nights": len(nights), "today": today.isoformat()}
    base = _med([r["sleep_min"] for r in last28]) if len(last28) >= MIN_BASELINE else None
    out["baseline_min"] = base
    out["baseline_missing"] = max(0, MIN_BASELINE - len(last28))
    last = nights[-1] if nights and nights[-1]["day"] >= (today - dt.timedelta(days=1)).isoformat() else None
    if last:
        out["last"] = {k: last.get(k) for k in ("day", "sleep_start", "sleep_end", "sleep_min", "deep_pct", "rem_pct",
                                                "light_pct", "awake_min", "rhr")}
    if last7:
        out["avg7_min"] = _mean([r["sleep_min"] for r in last7])
        out["nights7"] = len(last7)
        if base is not None:
            out["debt7_min"] = sum(base - r["sleep_min"] for r in last7)
    # bedtime drift: median bedtime this week vs the 3 weeks before (minutes; + = later)
    b7 = _med([clock_min(r["sleep_start"], tz) for r in last7 if r.get("sleep_start")])
    bp = _med([clock_min(r["sleep_start"], tz) for r in prior if r.get("sleep_start")])
    w7 = _med([clock_min(r["sleep_end"], tz) for r in last7 if r.get("sleep_end")])
    if b7 is not None:
        out["bed7"] = b7
        out["wake7"] = w7
    if b7 is not None and bp is not None and len(prior) >= 5:
        out["bed_prior"] = bp
        out["drift_min"] = b7 - bp
    # late nights (sleepin's calendar/place/hold tags): that night and the next one vs ordinary nights
    late_days = {d for d in tags}
    late = [r for r in last28 if r["day"] in late_days]
    after = [r for r in last28 if (dt.date.fromisoformat(r["day"]) - dt.timedelta(days=1)).isoformat() in late_days
             and r["day"] not in late_days]
    normal = [r for r in last28 if r["day"] not in late_days and r not in after]
    if late and len(normal) >= 3:
        out["late"] = {"n": len(late), "min": _mean([r["sleep_min"] for r in late]),
                       "wake": _med([clock_min(r["sleep_end"], tz) for r in late if r.get("sleep_end")]),
                       "normal_min": _mean([r["sleep_min"] for r in normal]),
                       "normal_wake": _med([clock_min(r["sleep_end"], tz) for r in normal if r.get("sleep_end")]),
                       "next_min": _mean([r["sleep_min"] for r in after]) if after else None, "next_n": len(after)}
    # gym days: the night after a strength workout (health) or a logged gym session (gym.db)
    gymd = set(gym or ())
    evening = set()
    for r in rows:
        for w in r.get("workouts") or []:
            if w.get("type") == "strength":
                gymd.add(r["day"])
                if isinstance(w.get("hour"), int) and w["hour"] >= 19:
                    evening.add(r["day"])
    prev = lambda r: (dt.date.fromisoformat(r["day"]) - dt.timedelta(days=1)).isoformat()   # noqa: E731
    g = [r for r in last28 if prev(r) in gymd and r["day"] not in late_days]
    ng = [r for r in last28 if prev(r) not in gymd and r["day"] not in late_days]
    if g and len(ng) >= 3:
        out["gym"] = {"n": len(g), "min": _mean([r["sleep_min"] for r in g]), "other_min": _mean([r["sleep_min"] for r in ng]),
                      "deep": _mean([r.get("deep_pct") for r in g]), "other_deep": _mean([r.get("deep_pct") for r in ng]),
                      "evening_n": sum(1 for r in g if prev(r) in evening)}
    # steps: 7 complete days vs the 21 before; today so far
    st = {r["day"]: r["steps"] for r in rows if r.get("steps") is not None}
    full = [d for d in sorted(st) if d < today.isoformat()]
    s7 = [st[d] for d in full if d >= (today - dt.timedelta(days=7)).isoformat()]
    sp = [st[d] for d in full if (today - dt.timedelta(days=28)).isoformat() <= d < (today - dt.timedelta(days=7)).isoformat()]
    if s7:
        out["steps7"] = _mean(s7)
        if len(sp) >= 5:
            out["steps_prior"] = _mean(sp)
            out["steps_change_pct"] = (out["steps7"] - out["steps_prior"]) * 100.0 / max(1.0, out["steps_prior"])
    if today.isoformat() in st:
        out["steps_today"] = st[today.isoformat()]
    rh = [r["rhr"] for r in last28 if r.get("rhr")]
    if last and last.get("rhr") and len(rh) >= MIN_BASELINE:
        out["rhr_base"] = _med(rh)
    out["tone"] = tone(out)
    return out


def tone(ins):
    base, last = ins.get("baseline_min"), (ins.get("last") or {}).get("sleep_min")
    debt = ins.get("debt7_min")
    if base is None or last is None:
        return "ok"
    if (debt is not None and debt >= 180) or last < base - 90:
        return "low"
    if (debt is None or debt <= 0) and last >= base:
        return "good"
    return "ok"


# ---------------------------------------------------------------------------------------------------- surfaces
def last_line(ins, tz):
    """'😴 6 h 52 · bed 00:41 · 7-day debt 2 h 10' (<= 80 chars) or None without last night."""
    last = ins.get("last")
    if not last or last.get("sleep_min") is None:
        return None
    parts = ["😴 " + hm(last["sleep_min"])]
    if last.get("sleep_start"):
        parts.append("bed " + clock_txt(clock_min(last["sleep_start"], tz)))
    if ins.get("debt7_min") is not None:
        d = ins["debt7_min"]
        parts.append(("7-day debt " + hm(d)) if d >= 15 else ("7-day surplus " + hm(-d)) if d <= -15 else "7 days on par")
    elif ins.get("baseline_missing"):
        parts.append("baseline in %d nights" % ins["baseline_missing"])
    return " · ".join(parts)[:80]


def board(t=None, cfg=None, holding=None):
    """The app Board's morning line: {line, night, tone} or {} (no data, still asleep / sleep-in holding, afternoon)."""
    t = t or now()
    cfg = cfg or config()
    if not cfg.get("board_line", True):
        return {}
    tz = zone()
    rows, tags = load(t=t)
    ins = insights(rows, tags, tz, t)
    last = ins.get("last")
    lt = dt.datetime.fromtimestamp(t, tz)
    if not last or last.get("day") != lt.date().isoformat() or not last.get("sleep_end") or last["sleep_end"] > t:
        return {}
    if lt.hour >= int(cfg.get("board_until_h") or BOARD_UNTIL_H):
        return {}
    if (sleepin_holding() if holding is None else holding):
        return {}
    line = last_line(ins, tz)
    return {"line": line, "night": last["day"], "tone": ins["tone"]} if line else {}


def card_lines(ins, tz):
    """/sleep text card (<= 10 lines)."""
    if not ins.get("nights"):
        return ["😴 No sleep data yet.",
                "Turn on “Health: sleep & energy” in the app's Setup; the first sync brings the last 30 days."]
    out = ["😴 Sleep & energy · last 7 nights"]
    last = ins.get("last")
    if last and last.get("sleep_min") is not None:
        s = "Last night " + hm(last["sleep_min"])
        if last.get("sleep_start") and last.get("sleep_end"):
            s += " (%s → %s)" % (clock_txt(clock_min(last["sleep_start"], tz)), clock_txt(clock_min(last["sleep_end"], tz)))
        stages = ["deep %d %%" % last["deep_pct"] if last.get("deep_pct") is not None else None,
                  "REM %d %%" % last["rem_pct"] if last.get("rem_pct") is not None else None]
        stages = [x for x in stages if x]
        out.append(s + (" · " + " · ".join(stages) if stages else ""))
    else:
        out.append("No sleep logged for last night yet.")
    if ins.get("avg7_min") is not None:
        s = "7-day avg " + hm(ins["avg7_min"])
        if ins.get("baseline_min") is not None:
            d = ins.get("debt7_min") or 0
            s += " · your usual " + hm(ins["baseline_min"]) + (" → debt " + hm(d) if d >= 15 else " → surplus " + hm(-d) if d <= -15 else " → on par")
        else:
            s += " · baseline after %d more nights" % ins["baseline_missing"]
        out.append(s)
    if ins.get("drift_min") is not None:
        d = ins["drift_min"]
        word = "later" if d > 0 else "earlier"
        out.append("Bedtime %s this week vs %s before (%s)" % (clock_txt(ins["bed7"]), clock_txt(ins["bed_prior"]),
                                                               ("%s %s" % (hm(d), word)) if abs(d) >= 15 else "steady"))
    elif ins.get("bed7") is not None:
        out.append("Bedtime %s · up %s (medians this week)" % (clock_txt(ins["bed7"]), clock_txt(ins["wake7"]) if ins.get("wake7") is not None else "–"))
    if ins.get("late"):
        L = ins["late"]
        s = "Late nights (%d): %s vs %s usually" % (L["n"], hm(L["min"]), hm(L["normal_min"]))
        if L.get("next_min") is not None:
            s += " · next night " + hm(L["next_min"])
        out.append(s)
    if ins.get("gym"):
        G = ins["gym"]
        s = "After gym (%d): %s vs %s" % (G["n"], hm(G["min"]), hm(G["other_min"]))
        if G.get("deep") is not None and G.get("other_deep") is not None:
            s += " · deep %.0f %% vs %.0f %%" % (G["deep"], G["other_deep"])
        out.append(s)
    if ins.get("steps7") is not None:
        s = "Steps %s/day this week" % "{:,}".format(int(round(ins["steps7"], -1))).replace(",", "'")
        if ins.get("steps_change_pct") is not None:
            s += " (%+.0f %% vs 3 weeks before)" % ins["steps_change_pct"]
        if ins.get("steps_today") is not None:
            s += " · today %s" % "{:,}".format(ins["steps_today"]).replace(",", "'")
        out.append(s)
    if ins.get("rhr_base") and last and last.get("rhr"):
        out.append("Resting pulse %d (usual %.0f)" % (last["rhr"], ins["rhr_base"]))
    if (ins.get("late") or {}).get("n", 9) < 3 or (ins.get("gym") or {}).get("n", 9) < 3:
        out.append("Few nights so far: comparisons firm up over the next weeks.")
    return out[:10]


def recap_lines(ins, tz):
    """Weekly recap (opt-in): the card without the last-night line, phone sized."""
    lines = card_lines(ins, tz)
    if len(lines) <= 2 and not ins.get("nights"):
        return []
    head = "😴 Your sleep week"
    return [head] + [x for x in lines[1:] if not x.startswith("Last night") and not x.startswith("No sleep logged")][:8]


def chart_rows(rows, tz, t=None, n=14):
    """Last n nights for the picture card: [(label, hours or None)]."""
    t = t or now()
    today = dt.datetime.fromtimestamp(t, tz).date()
    by = {r["day"]: r for r in rows}
    out = []
    for i in range(n - 1, -1, -1):
        d = today - dt.timedelta(days=i)
        r = by.get(d.isoformat())
        out.append((d.strftime("%d"), (r["sleep_min"] / 60.0) if r and r.get("sleep_min") is not None else None))
    return out


def status(t=None):
    """Counts only (safe for the session and logs): days stored, range, nights with sleep, last ingest, sync lag."""
    con = connect(ro=True)
    if con is None:
        return {"store": False}
    try:
        n, d0, d1 = con.execute("SELECT COUNT(*), MIN(day), MAX(day) FROM days").fetchone()
        ns = con.execute("SELECT COUNT(*) FROM days WHERE sleep_min IS NOT NULL").fetchone()[0]
        nl = con.execute("SELECT COUNT(*) FROM nights WHERE late=1").fetchone()[0]
        lags = [r[0] - r[1] for r in con.execute("SELECT first_rx, sleep_end FROM days WHERE first_rx IS NOT NULL AND "
                                                  "sleep_end IS NOT NULL AND first_rx-sleep_end BETWEEN 0 AND 43200")]
        li = con.execute("SELECT v FROM meta WHERE k='last_ingest'").fetchone()
    finally:
        con.close()
    return {"store": True, "days": n, "first": d0, "last": d1, "nights": ns, "late_nights": nl,
            "last_ingest": int(li[0]) if li else None,
            "sleep_end_lag_min": {"n": len(lags), "median": round(statistics.median(lags) / 60) if lags else None,
                                  "max": round(max(lags) / 60) if lags else None}}
