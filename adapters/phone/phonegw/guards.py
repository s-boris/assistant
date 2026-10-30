"""Guards on the gateway: the Buds left-behind push (flags.buds_left -> Effect push) and the app's own battery budget
(spec §1.2: state.app counters per day -> ONE ~/inbox/phone-battery-*.md note when over budget two days running).

Both are no-ops until a phone is paired: they only react to frames from a paired device. Privacy: the push names the
zone (vault-style name) or the town and a time, never coordinates; the budget note carries counters only."""
import asyncio
import datetime as dt
import json
import logging
import secrets
import time

from . import flags
from . import now as N
from .schema import ProtocolError
from .util import now_s

log = logging.getLogger("phonegw")
GW = "_gw"

# Daily budget for the app's self-report (state.app). Per-metric "over" = value > budget for a completed day.
BUDGET_KEYS = ("reconnects_d", "wakeups_h", "loc_d", "cpu_s_d")
BUDGET_HINTS = {
    "reconnects_d": "socket drops: WireGuard/network flapping or a NAT timeout shorter than keepalive_s (`phone status`: rtt, probes)",
    "wakeups_h": "wakeups: push/widget churn or a worker loop (gateway widget_every_s, pushes per day)",
    "loc_d": "location deliveries: passive fixes from navigation apps, or event-night precise mode left on",
    "cpu_s_d": "CPU time: a busy loop, long in-app calls or Track ID recordings",
}
BUDS_PER_DAY = 3


class Guards:
    # ------------------------------------------------------------ Buds left behind (flags.buds_left)
    async def guard_push(self, dev_id, e):
        if e.name != "buds_left":
            return None
        if not self.cfg.get("buds_guard", True):
            self.effect_log = (self.effect_log + [("buds_left", ("off",), False)])[-50:]
            return None
        d = e.data or {}
        off_at = int(d.get("off_at") or 0)
        tz = N.tz_of(self.cfg)
        day = dt.datetime.now(tz).strftime("%Y-%m-%d")
        sent, _ = self.db.get_state(dev_id, "buds_alert", {})
        sent = dict(sent or {})
        if sent.get("off_at") == off_at or (sent.get("day") == day and int(sent.get("n") or 0) >= BUDS_PER_DAY):
            return None
        n = int(sent.get("n") or 0) + 1 if sent.get("day") == day else 1
        self.db.put_state(dev_id, "buds_alert", {"off_at": off_at, "day": day, "n": n, "state": "pending"})
        grace = int((self.cfg.get("flags") or {}).get("buds_grace_s", 90))
        if grace > 0:
            await asyncio.sleep(grace)
        b, _ = self.db.get_state(dev_id, "flag.buds", None)
        bt, _ = self.db.get_state(dev_id, "bt", None)
        if not isinstance(b, dict) or b.get("on") is not False or int(b.get("at") or 0) != off_at or \
                (isinstance(bt, list) and "buds" in bt):
            self.db.put_state(dev_id, "buds_alert", {"off_at": off_at, "day": day, "n": n - 1, "state": "back"})
            self.effect_log = (self.effect_log + [("buds_left", ("back",), False)])[-50:]
            return None
        hm = dt.datetime.fromtimestamp(off_at, tz).strftime("%H:%M")
        where = (" at %s" % N.zone_name(d["zone"], self.cfg)) if d.get("zone") else \
            (" in %s" % d["town"]) if d.get("town") else ""
        nonce = secrets.token_hex(4)
        acts = [{"verb": "seen", "label": "Seen"}, {"verb": "open", "label": "Find", "arg": "find-buds"}]
        pb = {"level": "timed", "channel": "timed", "tag": "buds-left", "title": "🎧 Buds left behind?",
              "body": ("Last connected %s%s." % (hm, where))[:200], "private": True, "sticky": False,
              "full_screen": False, "nonce": nonce, "actions": acts, "deep_link": "claudeapp://find-buds",
              "night_ok": True, "show_until": int(time.time()) + 6 * 3600}     # G5: matters most at 02:00 outside a club
        try:
            mid, _ = await self.queue(dev_id, "push", pb, 6 * 3600, key="push:buds-left")
        except ProtocolError as err:
            log.warning("buds push: %s", err.msg)
            return None
        self.db.x("INSERT INTO push_log(id, at, level, tag, device_id, nonce, verbs) VALUES(?,?,?,?,?,?,?)",
                  mid, int(time.time() * 1000), "timed", "buds-left", dev_id, nonce,
                  json.dumps([{"verb": a["verb"], "arg": a.get("arg")} for a in acts]))
        self.db.put_state(dev_id, "buds_alert", {"off_at": off_at, "day": day, "n": n, "state": "sent", "at": now_s()})
        self.effect_log = (self.effect_log + [("buds_left", (d.get("trigger") or "",), True)])[-50:]
        log.info("buds left-behind push (%s)", d.get("trigger"))
        return mid

    # ------------------------------------------------------------ battery budget (spec §1.2)
    def budget(self):
        b = dict(self.cfg.get("battery_budget") or {})
        return {k: b[k] for k in BUDGET_KEYS if isinstance(b.get(k), (int, float)) and b[k] > 0}

    def battery_ingest(self, dev_id, body):
        """state.app -> the day's row (metric 'app', latest value per key; the counters are per local day on the phone)."""
        app = body.get("app")
        if not isinstance(app, dict):
            return
        day = app.get("day") or dt.datetime.now(N.tz_of(self.cfg)).strftime("%Y-%m-%d")
        row = self.db.one("SELECT v FROM daily WHERE device_id=? AND day=? AND metric='app'", dev_id, day)
        cur = json.loads(row["v"]) if row else {}
        cur.update({k: v for k, v in app.items() if k != "day"})
        self.db.x("INSERT INTO daily(device_id, day, metric, v) VALUES(?,?,?,?) ON CONFLICT(device_id, day, metric) "
                  "DO UPDATE SET v=excluded.v", dev_id, day, "app", json.dumps(cur))

    def app_days(self, dev_id, days):
        out = {}
        for d in days:
            row = self.db.one("SELECT v FROM daily WHERE device_id=? AND day=? AND metric='app'", dev_id, d)
            out[d] = json.loads(row["v"]) if row else None
        return out

    def over_budget(self, vals):
        return sorted(k for k, cap in self.budget().items() if vals and isinstance(vals.get(k), (int, float)) and vals[k] > cap)

    def battery_check(self, dev_id, today=None):
        """Completed days only. -> note path when the same metric was over budget on each of the last `days` days and
        this streak has no note yet; else None. A day under budget (or without data) ends the streak."""
        tz = N.tz_of(self.cfg)
        today = today or dt.datetime.now(tz).date()
        n = max(2, int((self.cfg.get("battery_budget") or {}).get("days", 2)))
        days = [(today - dt.timedelta(days=i)).isoformat() for i in range(n, 0, -1)]     # oldest first
        vals = self.app_days(dev_id, days)
        common = None
        for d in days:
            o = set(self.over_budget(vals[d]))
            common = o if common is None else common & o
        st, _ = self.db.get_state(dev_id, "battery_note", {})
        st = dict(st or {})
        if not common:
            if st.get("through"):
                self.db.put_state(dev_id, "battery_note", {"through": None, "last": st.get("last")})
            return None
        if st.get("through") == days[-2]:          # the streak goes on: already noted, stay quiet
            self.db.put_state(dev_id, "battery_note", dict(st, through=days[-1]))
            return None
        dev = self.db.device(dev_id)
        hello, _ = self.db.get_state(dev_id, "hello", {})
        cap = self.budget()
        lines = ["# Phone battery: app over budget %d days" % n, "",
                 "Device %s (%s, app %s): the app's own counters (state.app) were over the daily budget (spec §1.2, "
                 "target <= 2 %%/day) on %s." % (dev_id, (dev["model"] if dev else None) or "?", (hello or {}).get("app") or "?",
                                                   " and ".join(days)), "",
                 "| metric | budget | " + " | ".join(d[5:] for d in days) + " |",
                 "|---|---|" + "---|" * len(days)]
        for k in sorted(cap):
            lines.append("| %s%s | %s | %s |" % (k, " **over**" if k in common else "", cap[k],
                                                 " | ".join(str((vals[d] or {}).get(k, "–")) for d in days)))
        lines += ["", "Likely causes:"] + ["- %s: %s" % (k, BUDGET_HINTS[k]) for k in sorted(common)]
        fg = [(vals[d] or {}).get("fgs_h") for d in days]
        lines += ["", "Foreground service hours: %s (≈24 = always connected)." % ", ".join(str(x) for x in fg),
                  "Budgets: config battery_budget. Handling: JOBS.md (phone-battery)."]
        path = self.note("battery", "\n".join(lines) + "\n")
        self.db.put_state(dev_id, "battery_note", {"through": days[-1], "last": days[-1], "metrics": sorted(common)})
        log.info("battery budget note: %s", ",".join(sorted(common)))
        return path

    def battery_tick(self):
        """Housekeeping (every minute): once per local day at/after battery_budget.check_at (07:30)."""
        bb = self.cfg.get("battery_budget") or {}
        if not bb.get("on", True):
            return
        local = dt.datetime.now(N.tz_of(self.cfg))
        hh, _, mm = str(bb.get("check_at", "07:30")).partition(":")
        if (local.hour, local.minute) < (int(hh), int(mm or 0)):
            return
        day = local.strftime("%Y-%m-%d")
        last, _ = self.db.get_state(GW, "battery_tick", {})
        if (last or {}).get("day") == day:
            return
        self.db.put_state(GW, "battery_tick", {"day": day})
        for d in self.db.active_devices():
            self.battery_check(d["id"], local.date())

    def battery_view(self, dev_id):
        """`phone status`: today's counters, what is over budget today (informational; notes need 2 full days)."""
        day = dt.datetime.now(N.tz_of(self.cfg)).strftime("%Y-%m-%d")
        vals = self.app_days(dev_id, [day])[day]
        st, _ = self.db.get_state(dev_id, "battery_note", {})
        return {"day": day, "app": vals, "over": self.over_budget(vals), "budget": self.budget(),
                "noted_through": (st or {}).get("through")}
