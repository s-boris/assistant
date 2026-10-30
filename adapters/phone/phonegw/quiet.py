"""Quiet hours and display limits for pushes (audit G5 + G8).

G5: a non-urgent push (info, decision, timed) made inside his quiet hours (cfg quiet, his local zone from `tripclock tz`,
a trip's zone while away) or on a sleep-in morning (`sleepin status --json`: active until HH:MM) waits in the `held` table
and goes out when both are over. Never held: urgent, night_ok (the Buds guard: leaving a club at 02:00 is when it
matters), live countdowns (event nights; the live channel is silent). A newer push with the same tag replaces a held
one; `phone cancel TAG` drops it. A push whose explicit show_until ends before the release is moot and dropped.

G8: every push carries show_until (epoch s), the display limit the app honours (the delivery ttl is not one): an explicit
show_until / show_s from the caller, else live.until + 5 min for a countdown, else the send moment (the release for a
held push) + max(ttl, cfg show_s[level]).
"""
import datetime as dt
import json
import logging
import os
import subprocess
import time
from zoneinfo import ZoneInfo

from . import now as N
from .util import now_s

log = logging.getLogger("phonegw.quiet")
HOME = os.path.expanduser("~")
SHOW_S = {"urgent": 6 * 3600, "timed": 12 * 3600, "decision": 2 * 86400, "info": 86400}
MAX_EPOCH = 4102444800


def _tool(env, default):
    return os.environ.get(env) or os.path.join(HOME, "bin", default)


def _hm(s):
    h, m = str(s).split(":")
    return int(h), int(m)


def quiet_release(now, tz, window):
    """-> epoch when the quiet window [start, end) (his wall clock, may wrap midnight) ends, or None outside it."""
    try:
        (sh, sm), (eh, em) = _hm(window[0]), _hm(window[1])
    except (TypeError, ValueError, IndexError):
        return None
    loc = dt.datetime.fromtimestamp(now, tz)
    t, start, end = loc.hour * 60 + loc.minute, sh * 60 + sm, eh * 60 + em
    if start == end:
        return None
    inside = start <= t < end if start < end else (t >= start or t < end)
    if not inside:
        return None
    day = loc.date() if t < end else loc.date() + dt.timedelta(days=1)
    return int(dt.datetime(day.year, day.month, day.day, eh, em, tzinfo=tz).timestamp())


class Quiet:
    """Mixin for the Gateway: the hold verdict, show_until, the held table and its release (housekeeping, 60 s)."""
    _tz_cache = (0, None)
    _sleepin_cache = (0, None)

    def quiet_now(self):
        return time.time()

    def his_tz(self):
        at, tz = self._tz_cache
        if tz is not None and time.time() - at < 300:
            return tz
        tz = None
        try:
            p = subprocess.run([_tool("TRIPCLOCK_BIN", "tripclock"), "tz"], capture_output=True, timeout=10,
                               stdin=subprocess.DEVNULL, text=True)
            if p.returncode == 0 and p.stdout.strip():
                tz = ZoneInfo(p.stdout.strip().split()[0])
        except Exception:
            tz = None
        tz = tz or N.tz_of(self.cfg)
        self._tz_cache = (time.time(), tz)
        return tz

    def sleepin_release(self, tz):
        """-> epoch the sleep-in guard ends (active now), else None. Unknown (tool missing/failing) = not active."""
        at, v = self._sleepin_cache
        if time.time() - at < 60:
            return v
        v = None
        try:
            p = subprocess.run([_tool("SLEEPIN_BIN", "sleepin"), "status", "--json", "--days", "1"], capture_output=True,
                               timeout=10, stdin=subprocess.DEVNULL, text=True)
            d = json.loads(p.stdout) if p.returncode == 0 else {}
            if d.get("active") and d.get("until") and d.get("date"):
                y, mo, da = (int(x) for x in str(d["date"]).split("-"))
                h, m = _hm(d["until"])
                v = int(dt.datetime(y, mo, da, h, m, tzinfo=tz).timestamp())
        except Exception:
            v = None
        self._sleepin_cache = (time.time(), v)
        return v

    def hold_verdict(self, pb, now=None):
        """-> ("send", None) | ("hold", release epoch) | ("drop", release epoch) for one push body."""
        if not self.cfg.get("quiet_hold", True) or pb.get("level") == "urgent" or pb.get("night_ok") or pb.get("live"):
            return "send", None
        now = int(now if now is not None else self.quiet_now())
        tz = self.his_tz()
        rel = quiet_release(now, tz, self.cfg.get("quiet") or ["00:00", "08:00"])
        s = self.sleepin_release(tz)
        if s and s > now:
            rel = max(rel or 0, s)
        if not rel:
            return "send", None
        if pb.get("show_until") and int(pb["show_until"]) <= rel:
            return "drop", rel
        return "hold", rel

    def show_until_for(self, level, ttl, start, req=None, live=None):
        """G8: the display limit for a push sent at `start` (epoch s)."""
        req = req or {}
        try:
            if req.get("show_until"):
                return min(int(req["show_until"]), MAX_EPOCH)
            if req.get("show_s"):
                return min(int(start) + int(req["show_s"]), MAX_EPOCH)
        except (TypeError, ValueError):
            pass
        if live and isinstance(live.get("until"), int):
            return min(int(live["until"]) + 300, MAX_EPOCH)
        lim = int((self.cfg.get("show_s") or {}).get(level) or SHOW_S.get(level, 86400))
        return min(int(start) + max(int(ttl or 0), lim), MAX_EPOCH)

    # ------------------------------------------------------------ held table
    def hold_push(self, dev_id, msg_id, body, ttl, key, release):
        if key:
            self.db.x("DELETE FROM held WHERE device_id=? AND key=?", dev_id, key)
        self.db.x("INSERT INTO held(id, device_id, key, body, ttl, created, release) VALUES(?,?,?,?,?,?,?)",
                  msg_id, dev_id, key, json.dumps(body, ensure_ascii=False), int(ttl), now_s(), int(release))

    def drop_held(self, key, dev_id=None):
        if dev_id:
            return self.db.x("DELETE FROM held WHERE key=? AND device_id=?", key, dev_id).rowcount
        return self.db.x("DELETE FROM held WHERE key=?", key).rowcount

    def held_count(self):
        r = self.db.one("SELECT COUNT(*) n, MIN(release) nxt FROM held")
        return {"n": int(r["n"] or 0), "next": r["nxt"]} if r else {"n": 0, "next": None}

    async def release_held(self, now=None):
        """Housekeeping: send held pushes whose release passed and that the gate no longer holds. -> number sent."""
        now = int(now if now is not None else self.quiet_now())
        rows = self.db.all("SELECT * FROM held WHERE release<=? ORDER BY created", now)
        sent = 0
        active = {d["id"] for d in self.db.active_devices()}
        for r in rows:
            body = json.loads(r["body"])
            if r["device_id"] not in active:
                self.db.x("DELETE FROM held WHERE id=?", r["id"])
                continue
            verdict, rel = self.hold_verdict(body, now)
            if verdict == "hold":
                self.db.x("UPDATE held SET release=? WHERE id=?", rel, r["id"])
                continue
            self.db.x("DELETE FROM held WHERE id=?", r["id"])
            if verdict == "drop":
                continue
            try:
                await self.queue(r["device_id"], "push", body, r["ttl"], key=r["key"], msg_id=r["id"], gate=False)
                sent += 1
            except Exception as e:
                log.warning("held push release: %s", type(e).__name__)
        return sent
