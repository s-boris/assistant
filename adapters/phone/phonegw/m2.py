"""Gateway mixin: flags ingestion (state/event -> FlagEngine effects), the ask round trip, and the internal API for
now / ask / widget / card / geofences / alarms, plus phone-offline and phone-decisions notes. Mixed into Gateway."""
import asyncio
import datetime as dt
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import sys
import time
import urllib.parse

from aiohttp import ClientSession, ClientTimeout, web

from . import bridge, flags
from . import now as N
from .lint import lint
from .schema import ProtocolError
from .util import now_s, ulid

log = logging.getLogger("phonegw")
GW = "_gw"                                   # pseudo device id for gateway-wide state rows


def late_pick(at, now=None, after_s=600):
    """' (picked 20.10 23:59, arrived late)' when a card pick reaches the gateway > 10 min after his tap (it sat in the
    app's offline outbox or was refused until a fix), so a stale answer is read as one; '' otherwise."""
    try:
        at = int(at)
    except (TypeError, ValueError):
        return ""
    now = now_s() if now is None else now
    if at <= 0 or now - at <= after_s:
        return ""
    return " (picked %s, arrived late)" % time.strftime("%d.%m %H:%M", time.localtime(at))


ALARM_MODES = ("auto", "off", "backup", "on")


def remind_route():
    """remind's owner-reminder route (~/.config/remind/config.json "route", $REMIND_CONFIG): telegram | app | both;
    missing or unreadable = telegram, same rule as remind's app_route()."""
    path = os.environ.get("REMIND_CONFIG") or os.path.expanduser("~/.config/remind/config.json")
    try:
        with open(path, encoding="utf-8") as fh:
            route = str(json.load(fh).get("route") or "telegram").strip().lower()
    except (OSError, ValueError, AttributeError):
        return "telegram"
    return route if route in ("telegram", "app", "both") else "telegram"
ASK_LOCAL = ("now", "where", "car", "battery", "awake", "alarm", "dnd", "net", "activity", "steps", "sleep", "workouts")
ASK_PHONE = ("now", "where", "car", "battery", "awake", "alarm", "dnd", "net", "activity", "near")
WIDGETS = ("board", "leg", "trip", "tiles", "home")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def jerr(status, error, msg="", **kw):
    body = {"error": error}
    if msg:
        body["msg"] = msg[:200]
    body.update(kw)
    return web.json_response(body, status=status)


def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


class FlagCtx:
    """flags.Ctx over the gateway DB for one device."""

    def __init__(self, gw, dev_id, at=None):
        self.gw, self.dev, self.at = gw, dev_id, at

    def now(self):
        return self.at or now_s()

    def state(self, key):
        return self.gw.db.get_state(self.dev, key, None)

    def events(self, kind, since_s):
        return [json.loads(r["body"]) for r in self.gw.db.all(
            "SELECT body FROM events WHERE device_id=? AND kind=? AND at>=? ORDER BY at, received", self.dev, kind, since_s)]

    def fences(self):
        cur, _ = self.gw.db.get_state(GW, "geofences", {})
        return [flags.Fence(f["name"], f["kind"], f["r_m"], f.get("until")) for f in (cur or {}).get("fences", [])]

    def cfg(self):
        return dict(self.gw.cfg.get("flags") or {})


class M2:
    # ------------------------------------------------------------ hello: bring a (re)connected phone up to date
    async def m2_hello(self, dev_id):
        """Fences, alarms and widgets made while no phone was paired (or before this one paired) reach it on hello.
        Keys coalesce in the outbox, so a repeat only replaces the queued copy."""
        seen, _ = self.db.get_state(dev_id, "m2_revs", {})
        seen = dict(seen or {})
        now = now_s()
        try:
            g, _ = self.db.get_state(GW, "geofences", None)
            if g and g.get("fences") is not None and seen.get("geofences") != g["rev"]:
                fences = [{k: v for k, v in f.items() if k != "manual"} for f in g["fences"] if not f.get("until") or f["until"] > now]
                await self.queue(dev_id, "geofences", {"rev": g["rev"], "fences": fences}, self.ttl("geofences", 7 * 86400),
                                 key="geofences")
                seen["geofences"] = g["rev"]
            a, _ = self.db.get_state(GW, "alarms", None)
            if a and a.get("items") is not None and seen.get("alarms") != a["rev"]:
                items = [i for i in a["items"] if i["at"] > now]
                await self.queue(dev_id, "alarms", self.alarm_body(dev_id, a["rev"], items), self.ttl("alarms", 2 * 86400),
                                 key="alarms")
                seen["alarms"] = a["rev"]
            for k in WIDGETS:
                w, _ = self.db.get_state(GW, "widget:" + k, None)
                if w and w.get("data") and seen.get("widget:" + k) != w["digest"]:
                    await self.queue(dev_id, "widget", {"kind": k, "data": w["data"]}, self.ttl("widget", 7 * 86400),
                                     key="widget:" + k)
                    seen["widget:" + k] = w["digest"]
            up, _ = self.db.get_state(GW, "app_latest", None)
            dev = self.db.device(dev_id)
            if up and dev and int(dev["app_code"] or 0) < up["code"]:
                await self.queue(dev_id, "app.update", up, 7 * 86400, key="app.update")
        except ProtocolError as e:
            log.warning("hello catch-up: %s", e.msg)
        self.db.put_state(dev_id, "m2_revs", seen)

    # ------------------------------------------------------------ ingestion (called from handle_c2s)
    def m2_init(self):
        self.engine = flags.FlagEngine()
        self.asks = {}
        self.effect_log = []          # (name, args, ran) for tests and `phone status`
        self.widget_task = None

    async def m2_state(self, dev_id, body):
        prev = {k: self.db.get_state(dev_id, k, None)[0] for k in body}
        for k, v in body.items():
            self.db.put_state(dev_id, k, v)
        act = body.get("activity") or {}
        if act.get("kind") in N.ACTIVITY:
            self.db.put_state(dev_id, "flag.activity_last", act)
        try:
            effects = self.engine.on_state(FlagCtx(self, dev_id), body, prev)
        except Exception as e:
            log.warning("flags.on_state: %s", type(e).__name__)
            effects = []
        await self.run_effects(dev_id, effects)
        if "alarm" in body and prev.get("alarm") != body["alarm"]:
            self.spawn(self.check_alarm(dev_id, refresh=True))     # the departure producer may take seconds

    async def m2_event(self, dev_id, env):
        b = env["body"]
        if b["kind"] == "health.daily":
            for metric in ("sleep", "rhr", "steps", "workouts"):
                if metric in b:
                    self.db.x("INSERT INTO daily(device_id, day, metric, v) VALUES(?,?,?,?) ON CONFLICT(device_id, day, metric) "
                              "DO UPDATE SET v=excluded.v", dev_id, b["day"], metric, json.dumps(b[metric]))
        if b["kind"] == "zone":       # keep the place state in step with fence events (latest only)
            cur, _ = self.db.get_state(dev_id, "place", None)
            if b["dir"] in ("enter", "dwell") and (not cur or cur.get("zone") != b["zone"]):
                self.db.put_state(dev_id, "place", {"zone": b["zone"], "since": b.get("at") or env["ts"], "fix_age_min": 0})
            elif b["dir"] == "exit" and cur and cur.get("zone") == b["zone"]:
                self.db.put_state(dev_id, "place", {"zone": "away", "since": b.get("at") or env["ts"], "fix_age_min": 0})
        if b["kind"] == "alarm":
            self.db.put_state(dev_id, "alarm", {"next": b.get("next")})
        try:
            effects = self.engine.on_event(FlagCtx(self, dev_id), b)
        except Exception as e:
            log.warning("flags.on_event: %s", type(e).__name__)
            effects = []
        await self.run_effects(dev_id, effects)
        if b["kind"] == "alarm":
            self.spawn(self.check_alarm(dev_id, refresh=True))

    def m2_answer(self, dev_id, b):
        f = self.asks.get(b["q_id"])
        if f and not f.done():
            f.set_result(b)

    async def run_effects(self, dev_id, effects):
        routines = dict({"remind_place": False, "sleepin_release": False, "routine_leaving": True, "talk_button": True,
                         "sleepin_sleep_end": False, "cat_mark": True, "card_deck": True},
                        **(self.cfg.get("routines") or {}))
        for e in effects or []:
            if not isinstance(e, flags.Effect):
                continue
            if e.kind == "derived" and re.match(r"^[a-z][a-z0-9_]{0,31}$", e.name or ""):
                self.db.put_state(dev_id, "flag." + e.name, e.data)
            elif e.kind == "note" and re.match(r"^[a-z][a-z0-9-]{0,31}$", e.name or ""):
                self.note(e.name.replace("phone-", "", 1), "# Phone flag: %s\n\n%s\n" % (
                    e.name, "\n".join("- %s: %s" % (k, v) for k, v in sorted((e.data or {}).items())[:12])))
            elif e.kind == "push" and e.name in flags.PUSHES:
                self.spawn(self.guard_push(dev_id, e))          # guards.py: grace wait, limits, text
            elif e.kind == "tool" and e.name in flags.TOOLS:
                ran = bool(self.cfg.get("bridge")) and bool(routines.get(e.name))
                self.effect_log = (self.effect_log + [(e.name, tuple(e.args), ran)])[-50:]
                if ran and e.name == "card_deck":
                    if time.monotonic() - getattr(self, "deck_at", -1e9) >= 60:      # one deck a minute, however often tapped
                        self.deck_at = time.monotonic()
                        self.spawn(self.card_deck({}))
                elif ran:
                    self.spawn(bridge.run_tool(e.name, [str(a)[:40] for a in e.args]))

    # ------------------------------------------------------------ helpers
    def active(self):
        return self.db.active_devices()

    def first_device(self, dev_id=None):
        devs = self.active()
        if dev_id:
            devs = [d for d in devs if d["id"] == dev_id]
        return devs[0] if devs else None

    def view(self, dev):
        conn = self.conns.get(dev["id"]) if dev else None
        return N.now_view(self.db, dev, self.cfg, online=bool(conn), connected_since=int(conn.since) if conn else None,
                          ctx=FlagCtx(self, dev["id"]) if dev else None)

    def mark_rev(self, name, rev):
        for d in self.active():
            seen, _ = self.db.get_state(d["id"], "m2_revs", {})
            self.db.put_state(d["id"], "m2_revs", dict(seen or {}, **{name: rev}))

    async def queue_all(self, t, body, ttl, key=None, ack=True):
        """-> (ids, online) over every active device; raises ProtocolError on a schema problem."""
        ids, online = [], False
        for d in self.active():
            mid, on = await self.queue(d["id"], t, body, ttl, key=key, ack=ack)
            ids.append(mid)
            online = online or on
        return ids, online

    def ttl(self, name, default):
        return int((self.cfg.get("ttl_s") or {}).get(name, default))

    async def produce(self, kind, *args, timeout=40):
        """Run a producer (phonegw.producers) in a subprocess -> parsed JSON or None."""
        argv = [sys.executable, "-I", "-c", "import sys; sys.path.insert(0, sys.argv.pop(1)); from phonegw.producers "
                "import main; sys.exit(main(sys.argv[1:]))", ROOT, kind] + list(args)
        env = dict(os.environ)
        env["PHONEGW_CONFIG"] = self.paths.config
        env["PHONEGW_STATE"] = self.paths.state
        try:
            p = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                                                     stderr=asyncio.subprocess.DEVNULL, env=env)
            out, _ = await asyncio.wait_for(p.communicate(), timeout)
            return json.loads(out) if p.returncode == 0 else None
        except (OSError, ValueError, asyncio.TimeoutError) as e:
            log.warning("producer %s failed: %s", kind, type(e).__name__)
            return None

    # ------------------------------------------------------------ /internal/now, /internal/ask
    async def i_now(self, request):
        dev = self.first_device(request.query.get("device"))
        v = self.view(dev)
        return web.json_response(v, status=200 if dev else 404)

    async def ask_phone(self, dev_id, q, args, timeout):
        q_id = "q_" + ulid()
        fut = asyncio.get_running_loop().create_future()
        self.asks[q_id] = fut
        try:
            await self.queue(dev_id, "ask", {"q_id": q_id, "q": q, "args": args, "timeout_s": int(timeout)},
                             max(1, int(timeout)), ack=False)
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self.asks.pop(q_id, None)

    def daily(self, dev_id, metric, days):
        rows = self.db.all("SELECT day, v FROM daily WHERE device_id=? AND metric=? ORDER BY day DESC LIMIT ?", dev_id, metric,
                           int(days))
        return [{"day": r["day"], metric: json.loads(r["v"])} for r in rows]

    def answer_from(self, q, v, dev_id, args):
        if q == "now":
            return v
        if q == "where":
            return {"place": v["place"], "wifi": v["wifi"], "age_min": v["age_min"]}
        if q == "car":
            return {"drive": v["drive"], "bluetooth": v["bluetooth"], "activity": v["activity"]}
        if q == "battery":
            return v["battery"]
        if q == "awake":
            return {"sleep": v["sleep"], "screen": v["screen"]}
        if q == "alarm":
            return {"alarm": v["alarm"]}
        if q == "dnd":
            return {"dnd": v["dnd"]}
        if q == "net":
            net, _ = self.db.get_state(dev_id, "net", None)
            return net
        if q == "activity":
            return {"activity": v["activity"]}
        if q in ("steps", "sleep", "workouts") and not self.cfg.get("health"):
            return {"health": "off", "days": []}
        if q == "steps":
            return {"steps_today": v["steps_today"], "days": self.daily(dev_id, "steps", args.get("days", 7))}
        if q == "sleep":
            return {"last_sleep": v["last_sleep"], "days": self.daily(dev_id, "sleep", args.get("days", 1))}
        if q == "workouts":
            return {"days": self.daily(dev_id, "workouts", args.get("days", 7))}
        return None

    async def i_ask(self, request):
        r = await self._json(request) or {}
        q = str(r.get("q") or "")
        args = r.get("args") if isinstance(r.get("args"), dict) else {}
        if q not in ASK_LOCAL + ("near",):
            return jerr(400, "usage", "unknown question")
        dev = self.first_device(r.get("device"))
        if not dev:
            return web.json_response({"ok": False, "status": "no_device", "q": q})
        timeout = min(max(float(r.get("timeout_s") or 10), 1.0), 60.0)
        online = dev["id"] in self.conns
        source, phone = "cache", None
        if q == "near":
            place = str(args.get("place") or "")
            if not re.match(r"^[a-z][a-z0-9_-]{0,23}(:[a-z0-9_-]{1,32})?$", place):
                return jerr(400, "usage", "near needs a fence name")
            if not online:
                return web.json_response({"ok": False, "status": "offline", "q": q})
            ans = await self.ask_phone(dev["id"], "near", {"place": place}, timeout)
            if ans is None:
                return web.json_response({"ok": False, "status": "timeout", "q": q})
            return web.json_response({"ok": bool(ans.get("ok")), "status": "answered", "q": q, "source": "phone",
                                      "result": ans.get("result")})
        if r.get("fresh") and q in ASK_PHONE:
            if online:
                phone = await self.ask_phone(dev["id"], q, {}, timeout)
                source = "phone" if phone and phone.get("ok") else "cache"
        v = self.view(dev)
        res = {"ok": True, "status": "answered", "q": q, "source": source, "online": online, "age_min": v["age_min"],
               "result": self.answer_from(q, v, dev["id"], args)}
        if r.get("fresh") and source == "cache":
            res["fresh_failed"] = "offline" if not online else "timeout"
        return web.json_response(res)

    # ------------------------------------------------------------ widgets
    async def push_widget(self, kind, data, changed_only=False):
        body = {"kind": kind, "data": data}
        self.proto.check("s2c", {"v": 1, "id": ulid(), "t": "widget", "ts": now_s(), "ack": True, "body": body})
        d = digest({k: v for k, v in data.items() if k != "updated"})
        last, _ = self.db.get_state(GW, "widget:" + kind, {})
        if changed_only and (last or {}).get("digest") == d:
            return {"ok": True, "status": "unchanged", "kind": kind}
        if not self.active():
            return {"ok": False, "status": "no_device", "kind": kind}
        ids, online = await self.queue_all("widget", body, self.ttl("widget", 7 * 86400), key="widget:" + kind)
        self.db.put_state(GW, "widget:" + kind, {"digest": d, "at": now_s(), "data": data})
        self.mark_rev("widget:" + kind, d)
        return {"ok": True, "status": "sent" if online else "queued", "online": online, "kind": kind, "id": ids[0]}

    async def i_widget(self, request):
        r = await self._json(request) or {}
        kinds = WIDGETS if r.get("kind") == "all" else (r.get("kind"),)
        if any(k not in WIDGETS for k in kinds):
            return jerr(400, "usage", "kind board|leg|trip|tiles|home|all")
        out = []
        for k in kinds:
            data = r.get("data") if r.get("kind") != "all" else None
            if data is None and r.get("from_tools", True):
                data = await self.produce(k)
                if data is None:
                    out.append({"ok": False, "status": "producer_failed", "kind": k})
                    continue
            try:
                out.append(await self.push_widget(k, data, changed_only=bool(r.get("changed_only"))))
            except ProtocolError as e:
                out.append({"ok": False, "status": "schema", "kind": k, "msg": e.msg, "problems": e.problems})
        ok = all(x["ok"] for x in out)
        return web.json_response({"ok": ok, "results": out}, status=200 if ok or any(x["status"] != "schema" for x in out) else 400)

    async def widget_loop(self):
        """Refresh widgets every widget_every_s while a device is paired (pushes only on change)."""
        await asyncio.sleep(20)
        n = 0
        while True:
            try:
                if self.cfg.get("widgets_auto", True) and self.active():
                    await self.sync_alarms()
                    if n % 12 == 0:
                        await self.sync_fences()
                    if n % 6 == 0:
                        await self.alarm_tick()
                    p = self.geo_path("ch")
                    if not os.path.exists(p) or time.time() - os.path.getmtime(p) > 7 * 86400:
                        await self.build_geo("ch")
                    n += 1
                    for k in WIDGETS:
                        if k == "home":                   # HA reads: every home_every_s (10 min), not every pass
                            if time.time() - getattr(self, "_home_at", 0) < int(self.cfg.get("home_every_s", 600)):
                                continue
                            self._home_at = time.time()
                        data = await self.produce(k)
                        if data is not None:
                            await self.push_widget(k, data, changed_only=True)
            except Exception as e:
                log.warning("widget loop: %s %s", type(e).__name__, str(getattr(e, "msg", "") or "")[:160])
            await asyncio.sleep(int(self.cfg.get("widget_every_s", 300)))

    # ------------------------------------------------------------ alarm vs departure (spec #8; S: flags.alarm_check)
    async def departure(self, max_age_s=600):
        """The first departure until the next 10:00 (producer `depart`), cached in GW state for max_age_s."""
        cur, at = self.db.get_state(GW, "depart", None)
        if cur is not None and at and now_s() - at < max_age_s and (cur.get("empty") or cur.get("at", 0) > now_s()):
            return cur
        got = await self.produce("depart", timeout=40)
        if got is not None:
            self.db.put_state(GW, "depart", got)
            return got
        return cur

    async def check_alarm(self, dev_id, refresh=False):
        """Problem-only: no alarm, or an alarm later than leave-by - ready_min -> one private `timed` push per
        departure. Runs when the alarm changes, at 21:00 and when the departure changes (widget loop)."""
        alarm, alarm_at = self.db.get_state(dev_id, "alarm", None)
        if alarm_at is None:
            return None                           # the phone never reported its alarm: nothing to compare
        dep = await self.departure(0 if refresh else 600)
        if not dep or dep.get("empty"):
            return None
        try:
            res = flags.alarm_check(alarm.get("next"), dep["at"], dict(self.cfg.get("flags") or {}))
        except Exception as e:
            log.warning("flags.alarm_check: %s", type(e).__name__)
            return None
        self.db.put_state(dev_id, "flag.alarm_check", dict(res, depart=dep["at"]))
        sent, _ = self.db.get_state(dev_id, "alarm_check_sent", {})
        if res.get("ok") or (sent or {}).get("depart") == dep["at"]:
            return res
        tz = N.tz_of(self.cfg)
        hm = lambda ts: dt.datetime.fromtimestamp(ts, tz).strftime("%H:%M")   # noqa: E731
        title = ("⏰ Alarm %s, you leave %s" % (hm(alarm["next"]), hm(dep["at"]))) if alarm.get("next") else \
            "⏰ No alarm, you leave %s" % hm(dep["at"])
        body = "For %s %s." % (dep.get("icon") or "", dep.get("what") or dep.get("kind") or "the trip")
        nonce = secrets.token_hex(4)
        pb = {"level": "timed", "channel": "timed", "tag": "alarm-check", "title": title[:80],
              "body": re.sub(r"\s+", " ", body).strip()[:200], "private": True, "sticky": False, "full_screen": False,
              "nonce": nonce, "actions": [{"verb": "seen", "label": "Seen"}]}
        if int(dep["at"]) > now_s():
            pb["show_until"] = int(dep["at"])           # moot once he leaves; held at night, dropped if he leaves before 08:00
        try:
            mid, on = await self.queue(dev_id, "push", pb, 6 * 3600, key="push:alarm-check")
            if on == "dropped":
                return res
        except ProtocolError as e:
            log.warning("alarm check push: %s", e.msg)
            return res
        self.db.x("INSERT INTO push_log(id, at, level, tag, device_id, nonce, verbs) VALUES(?,?,?,?,?,?,?)",
                  mid, int(time.time() * 1000), "timed", "alarm-check", dev_id, nonce, json.dumps([{"verb": "seen", "arg": None}]))
        self.db.put_state(dev_id, "alarm_check_sent", {"depart": dep["at"], "at": now_s()})
        log.info("alarm check: %s", res.get("reason"))
        return res

    async def alarm_tick(self):
        """Widget loop: once a day at/after 21:00 (owner time), and whenever the departure changed."""
        local = dt.datetime.now(N.tz_of(self.cfg))
        day = local.strftime("%Y-%m-%d")
        last, _ = self.db.get_state(GW, "alarm_tick", {})
        before, _ = self.db.get_state(GW, "depart", None)
        dep = await self.departure()
        evening = local.hour >= 21 and (last or {}).get("day") != day
        if evening or (dep or {}).get("at") != (before or {}).get("at"):
            for d in self.active():
                await self.check_alarm(d["id"])
            if evening:
                self.db.put_state(GW, "alarm_tick", {"day": day})

    # ------------------------------------------------------------ geo packs (/v1/geo/<pack>, mTLS)
    def geo_path(self, pack):
        return os.path.join(self.paths.state, "geo-%s.json" % pack)

    async def build_geo(self, pack="ch"):
        got = await self.produce("geo", timeout=180)
        if got is None:
            return {"ok": False, "status": "producer_failed"}
        try:
            self.proto.check_http("geo.response", got)
        except ProtocolError as e:
            return {"ok": False, "status": "schema", "msg": e.msg}
        from .util import write_private
        write_private(self.geo_path(pack), json.dumps(got, ensure_ascii=False, separators=(",", ":")))
        return {"ok": True, "status": "built", "towns": len(got["towns"]), "stops": len(got["stops"]),
                "bytes": os.path.getsize(self.geo_path(pack))}

    async def i_geo(self, request):
        r = await self._json(request) or {}
        if r.get("pack", "ch") != "ch":
            return jerr(400, "usage", "pack ch")
        if r.get("build"):
            return web.json_response(await self.build_geo("ch"))
        p = self.geo_path("ch")
        if not os.path.exists(p):
            return web.json_response({"ok": False, "status": "missing"})
        with open(p) as fh:
            g = json.load(fh)
        return web.json_response({"ok": True, "status": "present", "towns": len(g["towns"]), "stops": len(g["stops"]),
                                  "built": g.get("built"), "bytes": os.path.getsize(p)})

    async def x_geo(self, request):
        pack = request.match_info["pack"]
        if pack != "ch":
            return jerr(404, "not_found", "unknown pack")
        p = self.geo_path(pack)
        if not os.path.exists(p):
            return jerr(404, "not_found", "pack not built yet")
        with open(p, "rb") as fh:
            data = fh.read()
        etag = '"%s"' % hashlib.sha256(data).hexdigest()[:20]
        self.touch(request["device"]["id"])
        if request.headers.get("If-None-Match") == etag:
            return web.Response(status=304, headers={"ETag": etag})
        return web.Response(body=data, content_type="application/json", headers={"ETag": etag, "Cache-Control": "no-cache"})

    # ------------------------------------------------------------ cards
    async def i_card(self, request):
        r = await self._json(request) or {}
        if r.get("deck"):
            return await self.card_deck(r)
        cid = r.get("card_id") or "c_" + ulid().lower()
        body = {"card_id": cid, "kind": r.get("kind") or "decision", "title": str(r.get("title") or "")[:80]}
        for k in ("text", "options", "confirm", "expires", "group"):
            if r.get(k) is not None:
                body[k] = r[k]
        if isinstance(body.get("text"), str):
            body["text"] = body["text"][:400]
        if not self.active():
            return web.json_response({"ok": False, "status": "no_device"})
        ttl = max(60, int(body["expires"]) - now_s()) if body.get("expires") else self.ttl("card", 2 * 86400)
        try:
            ids, online = await self.queue_all("card", body, ttl, key="card:" + cid)
        except ProtocolError as e:
            return jerr(400, "usage", e.msg, problems=e.problems)
        self.db.x("INSERT INTO cards(id, kind, body, created, expires, answer, answered) VALUES(?,?,?,?,?,NULL,NULL) "
                  "ON CONFLICT(id) DO UPDATE SET body=excluded.body, expires=excluded.expires, answer=NULL, answered=NULL",
                  cid, body["kind"], json.dumps(body, ensure_ascii=False), now_s(), now_s() + ttl)
        return web.json_response({"ok": True, "card_id": cid, "online": online, "status": "sent" if online else "queued"})

    async def deck_items(self):
        """`decision-deck --json` -> its items, or None (hub mode asks the brain: hubmode.py)."""
        argv = [bridge.tool("DECISION_DECK_BIN", "decision-deck"), "--json"]
        try:
            p = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                                                     stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(p.communicate(), 30)
            return json.loads(out).get("items") if p.returncode == 0 else None
        except (OSError, ValueError, AttributeError, asyncio.TimeoutError):
            return None

    async def card_deck(self, r):
        """Decision cards from `decision-deck --json` (TODO.md's Waiting on you as either/or prompts)."""
        deck = r.get("items")
        if deck is None:
            deck = await self.deck_items()
        if deck is None:
            return web.json_response({"ok": False, "status": "deck_failed"})
        if not self.active():
            return web.json_response({"ok": False, "status": "no_device", "items": len(deck)})
        sent = []
        for it in deck[: int(r.get("max") or 8)]:
            opts = [o if isinstance(o, str) else (o or {}).get("label") or "" for o in (it.get("options") or [])][:4]
            if len(opts) < 2:
                continue
            cid = "c_deck_" + re.sub(r"[^A-Za-z0-9_-]", "", str(it.get("id") or ulid()))[:32]
            body = {"card_id": cid, "kind": "decision", "title": str(it.get("title") or "")[:80] or "Decision",
                    "text": str(it.get("say") or "")[:400] or None,
                    "options": [{"id": "o%d" % (i + 1), "label": str(o)[:40]} for i, o in enumerate(opts)],
                    "confirm": "none", "expires": now_s() + 2 * 86400, "group": "deck"}
            try:
                await self.queue_all("card", body, 2 * 86400, key="card:" + cid)
            except ProtocolError as e:
                log.warning("deck card %s: %s", cid, e.msg)
                continue
            self.db.x("INSERT INTO cards(id, kind, body, created, expires, answer, answered) VALUES(?,?,?,?,?,NULL,NULL) "
                      "ON CONFLICT(id) DO UPDATE SET body=excluded.body, expires=excluded.expires, answer=NULL, answered=NULL",
                      cid, "decision", json.dumps(body, ensure_ascii=False), now_s(), body["expires"])
            sent.append(cid)
        return web.json_response({"ok": True, "status": "sent", "cards": sent})

    async def i_card_rm(self, request):
        r = await self._json(request) or {}
        cid = str(r.get("card_id") or "")
        if not re.match(r"^c_[A-Za-z0-9_-]{1,40}$", cid):
            return jerr(400, "usage", "card_id required")
        if not self.active():
            return web.json_response({"ok": False, "status": "no_device"})
        ids, online = await self.queue_all("card.remove", {"card_id": cid}, self.ttl("card", 2 * 86400), key="card:" + cid)
        self.db.x("UPDATE cards SET expires=? WHERE id=?", now_s(), cid)
        return web.json_response({"ok": True, "online": online})

    async def i_cards(self, request):
        rows = self.db.all("SELECT id, kind, body, created, expires, answer, answered FROM cards WHERE created>? "
                           "ORDER BY created DESC LIMIT 50", now_s() - 7 * 86400)
        out = []
        for r in rows:
            b = json.loads(r["body"] or "{}")
            label = next((o["label"] for o in b.get("options") or [] if o["id"] == r["answer"]), None)
            out.append({"card_id": r["id"], "kind": r["kind"], "title": b.get("title"), "group": b.get("group"),
                        "answer": r["answer"], "answer_label": label, "answered": r["answered"], "expires": r["expires"]})
        return web.json_response({"ok": True, "cards": out})

    # ------------------------------------------------------------ hand-offs to my session (telegram-plus inbox)
    def plugin_token(self):
        """First line of the inbox token file (telegram-plus reads the same file: TELEGRAM_VOICE_INBOX_TOKEN_FILE,
        default ~/.secrets/voice-local-token). Never logged."""
        path = os.path.expanduser(self.cfg.get("chat_plugin_token_file") or self.cfg.get("voice_token_file")
                                  or "~/.secrets/voice-local-token")
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.readline().strip()
        except OSError:
            return ""

    async def plugin_handoff(self, what, task, **extra):
        """POST one app input (chat message, card pick) to telegram-plus's inbox (body source "app" -> a channel event
        via=app). -> True when the inbox took it (2xx); False = the caller writes an ~/inbox note instead."""
        if not self.cfg.get("chat_plugin"):
            return False
        body = {"id": "a" + secrets.token_hex(4), "task": task, "urgency": "later", "source": "app"}
        body.update({k: v for k, v in extra.items() if v})
        try:
            async with ClientSession(timeout=ClientTimeout(total=5)) as s:
                async with s.post(self.cfg["chat_plugin_url"], json=body,
                                  headers={"Authorization": "Bearer " + self.plugin_token()}) as r:
                    if r.status < 300:
                        return True
                    log.warning("%s: plugin inbox answered %s; writing a note", what, r.status)
        except Exception as e:
            log.warning("%s: plugin inbox unreachable (%s); writing a note", what, type(e).__name__)
        return False

    def on_card_answer(self, dev_id, b):
        cur = self.db.x("UPDATE cards SET answer=?, answered=? WHERE id=? AND answer IS NULL", b["option"], now_s(), b["card_id"])
        if not cur.rowcount:
            return
        row = self.db.one("SELECT kind, body FROM cards WHERE id=?", b["card_id"])
        body = json.loads(row["body"] or "{}") if row else {}
        if body.get("kind") != "decision":
            return
        label = next((o["label"] for o in body.get("options") or [] if o["id"] == b["option"]), b["option"])
        late = late_pick(b.get("at"))
        if self.cfg.get("chat_plugin"):
            self.spawn(self.card_answer_out(dev_id, b["card_id"], body, label, late))
            return
        self.decisions_note(b["card_id"], body, label, late)

    async def card_answer_out(self, dev_id, card_id, body, label, late=""):
        task = ("[card %s] %s → %s%s (his pick on a phone decision card: apply it like his Telegram answer; "
                "a pick is never an approval for a risky step)" % (card_id, body.get("title") or "?", label, late))
        if not await self.plugin_handoff("card", task, device_id=dev_id):
            self.decisions_note(card_id, body, label, late)

    def decisions_note(self, card_id, body, label, late=""):
        line = "- %s → **%s**%s (card %s, %s)\n" % (body.get("title") or "?", label, late, card_id, time.strftime("%d.%m %H:%M"))
        last, _ = self.db.get_state(GW, "decisions_note", {})
        path = (last or {}).get("path")
        if path and os.path.exists(path) and now_s() - (last or {}).get("at", 0) < 600:
            with open(path, "a") as fh:
                fh.write(line)
        else:
            path = self.note("decisions", "# Phone decisions (his picks on decision cards; nothing applied yet)\n\n"
                                          "Apply each pick like his Telegram answer, then delete this note.\n\n" + line)
        self.db.put_state(GW, "decisions_note", {"path": path, "at": now_s()})

    # ------------------------------------------------------------ geofences
    async def i_geofences_get(self, request):
        cur, at = self.db.get_state(GW, "geofences", {"rev": 0, "fences": []})
        return web.json_response({"ok": True, "rev": cur.get("rev", 0), "at": at, "missing": cur.get("missing", []),
                                  "fences": [{k: f.get(k) for k in ("name", "kind", "r_m", "until", "dwell_s")}
                                             for f in cur.get("fences", [])]})   # never the coordinates

    async def i_geofences(self, request):
        r = await self._json(request) or {}
        res = await self.fences_from(r)
        return web.json_response(res, status=res.pop("_status", 200))

    async def fences_from(self, r):
        cur, _ = self.db.get_state(GW, "geofences", {"rev": 0, "fences": []})
        missing = []
        if r.get("source") == "auto":
            got = await self.produce("fences", timeout=90)
            if got is None:
                return {"ok": False, "status": "producer_failed"}
            fences, missing = got["fences"], got.get("missing", [])
            extra = [f for f in cur.get("fences", []) if f.get("manual") and f["name"] not in {x["name"] for x in fences}]
            fences += [{k: v for k, v in f.items() if k != "manual"} for f in extra]
            manual = {f["name"] for f in extra}
        else:
            fences = cur.get("fences", [])
            manual = {f["name"] for f in fences if f.get("manual")}
            fences = [{k: v for k, v in f.items() if k != "manual"} for f in fences]
            if r.get("remove"):
                fences = [f for f in fences if f["name"] != r["remove"]]
                manual.discard(r["remove"])
            if isinstance(r.get("add"), dict):
                f = r["add"]
                fences = [x for x in fences if x["name"] != f.get("name")] + [f]
                manual.add(f.get("name"))
        now = now_s()
        fences = [f for f in fences if not f.get("until") or f["until"] > now]
        body = {"rev": int(cur.get("rev", 0)) + 1, "fences": fences}
        try:
            self.proto.check("s2c", {"v": 1, "id": ulid(), "t": "geofences", "ts": now, "ack": True, "body": body})
        except ProtocolError as e:
            return {"ok": False, "status": "schema", "msg": e.msg, "problems": e.problems, "_status": 400}
        stored = {"rev": body["rev"], "missing": missing,
                  "fences": [dict(f, manual=True) if f["name"] in manual else f for f in fences]}
        changed = digest(cur.get("fences", [])) != digest(stored["fences"])
        if not changed and not r.get("force"):
            return {"ok": True, "status": "unchanged", "rev": cur.get("rev", 0), "count": len(fences), "missing": missing}
        self.db.put_state(GW, "geofences", stored)
        if not self.active():
            return {"ok": True, "status": "stored", "rev": body["rev"], "count": len(fences), "missing": missing}
        ids, online = await self.queue_all("geofences", body, self.ttl("geofences", 7 * 86400), key="geofences")
        self.mark_rev("geofences", body["rev"])
        return {"ok": True, "status": "sent" if online else "queued", "rev": body["rev"], "count": len(fences),
                "missing": missing}

    # ------------------------------------------------------------ alarms (remind -> local alarms, next 48 h)
    def remind_items(self, hours=48):
        path = os.environ.get("REMIND_DB") or self.cfg.get("remind_db") or os.path.expanduser("~/.local/state/remind/reminders.json")
        try:
            with open(path) as fh:
                items = json.load(fh)
        except (OSError, ValueError):
            return None
        tz = N.tz_of(self.cfg)
        now = now_s()
        out = []
        for r in items if isinstance(items, list) else []:
            if not isinstance(r, dict) or not r.get("owner") or r.get("on") or not re.match(r"^[0-9a-f]{8}$", str(r.get("id"))):
                continue
            try:
                at = int(dt.datetime.strptime(r["at"], "%Y-%m-%d %H:%M").replace(tzinfo=tz).timestamp())
            except (KeyError, ValueError, TypeError):
                continue
            if now - 60 <= at <= now + hours * 3600:
                out.append({"rid": r["id"], "at": at, "title": str(r.get("text") or ""), "call": bool(r.get("call"))})
        return sorted(out, key=lambda x: x["at"])[:50]

    def alarm_body(self, dev_id, rev, items):
        """Per device: each alarm gets a push id + nonce in push_log (verbs snooze/done/seen, arg r:<rid>), so an
        offline Snooze/Done tap syncs back as `action` and the bridge runs `remind act`."""
        if any(it.get("backup") for it in items) and not self.has_cap(dev_id, "alarm_backup"):
            items = []                          # an app without backup semantics would ring at the due minute
        out = []
        for it in items:
            pid, nonce = ulid(), secrets.token_hex(4)
            arg = "r:" + it["rid"]
            self.db.x("INSERT INTO push_log(id, at, level, tag, device_id, nonce, verbs) VALUES(?,?,?,?,?,?,?)",
                      pid, int(time.time() * 1000), "alarm", "alarm:" + it["rid"], dev_id, nonce,
                      json.dumps([{"verb": v, "arg": arg} for v in ("snooze", "done", "seen")]))
            out.append(dict(it, push_id=pid, nonce=nonce))
        return {"rev": rev, "items": out}

    async def sync_alarms(self):
        """Loop helper: the remind store -> alarms (no-op when unchanged)."""
        return await self.alarms_from(dict(hours=48))

    async def sync_fences(self):
        return await self.fences_from({"source": "auto"})

    async def i_alarms(self, request):
        r = await self._json(request) or {}
        res = await self.alarms_from(r)
        return web.json_response(res, status=res.pop("_status", 200))

    def has_cap(self, dev_id, cap):
        hello, _ = self.db.get_state(dev_id, "hello", {})
        return cap in ((hello or {}).get("caps") or [])

    def alarms_mode(self):
        """Config `alarms`: auto (default) | off | backup | on.
        off: no local alarms (an empty set, so the phone clears what it has). on: every owner reminder of the next
        48 h rings locally at its minute. backup: items carry backup=true (the app fires them only when no push for
        that reminder arrived by due + 2 min; apps without the `alarm_backup` cap get an empty set).
        auto: off while remind's route is telegram (Telegram already brings each reminder: a local alarm would
        double it), backup when the route is app or both."""
        mode = str(self.cfg.get("alarms") or "auto").strip().lower()
        if mode not in ALARM_MODES:
            mode = "off"
        if mode == "auto":
            mode = "backup" if remind_route() in ("app", "both") else "off"
        return mode

    async def alarms_from(self, r):
        mode = self.alarms_mode()
        if isinstance(r.get("items"), list):
            items = r["items"]
        elif mode == "off":
            items = []
        else:
            items = self.remind_items(int(r.get("hours") or 48))
        if items is None:
            return {"ok": False, "status": "no_remind_store"}
        out = []
        for it in items:
            text = re.sub(r"\s+", " ", str(it.get("title") or "")).replace("⏰", "").strip()
            _, _, problems, _ = lint(text[:40], "", True)
            private = bool(problems) or len(text) > 40 or not text
            a = {"rid": it["rid"], "at": int(it["at"]), "title": "Reminder" if private else text[:60],
                 "private": private, "exact": "alarm_clock" if it.get("call") else "exact", "snooze": True}
            if private and text:
                a["text"] = text[:120]          # shown only when unlocked; the lock screen keeps the safe title
            if mode == "backup":
                a["backup"] = True
            out.append(a)
        cur, _ = self.db.get_state(GW, "alarms", {"rev": 0})
        body = {"rev": int(cur.get("rev", 0)) + 1, "items": out}
        try:
            self.proto.check("s2c", {"v": 1, "id": ulid(), "t": "alarms", "ts": now_s(), "ack": True, "body": body})
        except ProtocolError as e:
            return {"ok": False, "status": "schema", "msg": e.msg, "problems": e.problems, "_status": 400}
        d = digest(out)
        if cur.get("digest") == d and not r.get("force"):
            return {"ok": True, "status": "unchanged", "rev": cur.get("rev", 0), "count": len(out), "mode": mode}
        self.db.put_state(GW, "alarms", {"rev": body["rev"], "digest": d, "count": len(out), "mode": mode,
                                         "next": out[0]["at"] if out else None, "items": out})
        if not self.active():
            return {"ok": True, "status": "stored", "rev": body["rev"], "count": len(out), "mode": mode}
        online = False
        for dv in self.active():
            _, on = await self.queue(dv["id"], "alarms", self.alarm_body(dv["id"], body["rev"], out),
                                     self.ttl("alarms", 2 * 86400), key="alarms")
            online = online or on
        self.mark_rev("alarms", body["rev"])
        return {"ok": True, "status": "sent" if online else "queued", "rev": body["rev"], "mode": mode,
                "count": len(out), "private": sum(1 for x in out if x["private"])}

    # ------------------------------------------------------------ routines
    async def i_routine(self, request):
        r = await self._json(request) or {}
        if r.get("name") != "leaving":
            return jerr(400, "usage", "routine leaving")
        dev = self.first_device()
        v = self.view(dev) if dev else {}
        card = await self.produce("leaving", json.dumps({"battery": v.get("battery")}), timeout=8)
        if card is None:
            return web.json_response({"ok": False, "status": "producer_failed"})
        if not dev:
            return web.json_response({"ok": False, "status": "no_device", "card": card})
        ttl = max(60, card["expires"] - now_s())
        try:
            ids, online = await self.queue_all("card", card, ttl, key="card:" + card["card_id"])
        except ProtocolError as e:
            return jerr(400, "usage", e.msg, problems=e.problems)
        self.db.x("INSERT INTO cards(id, kind, body, created, expires, answer, answered) VALUES(?,?,?,?,?,NULL,NULL) "
                  "ON CONFLICT(id) DO UPDATE SET body=excluded.body, expires=excluded.expires",
                  card["card_id"], "info", json.dumps(card, ensure_ascii=False), now_s(), card["expires"])
        return web.json_response({"ok": True, "status": "sent" if online else "queued", "card_id": card["card_id"],
                                  "lines": card["text"].count("\n") + 1})

    # ------------------------------------------------------------ phone-offline notes (housekeeping, every minute)
    def check_offline(self, now=None):
        now = now or now_s()
        limit = int(float(self.cfg.get("offline_note_h", 2)) * 3600)
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        written = []
        for d in self.active():
            if d["id"] in self.conns or not d["last_seen"] or now - d["last_seen"] < limit:
                continue
            place, _ = self.db.get_state(d["id"], "place", None)
            if place and flags.place_kind(place.get("zone")) == "home":
                continue
            noted, _ = self.db.get_state(d["id"], "offline_noted", {})
            if (noted or {}).get("day") == day:
                continue
            hours = (now - d["last_seen"]) // 3600
            written.append(self.note("offline", "# Phone offline\n\nDevice %s (%s): no socket for %d h while he is away "
                                                "(last zone not home). Pushes queue until their ttl; Telegram stays the "
                                                "fallback. Check `phone status`; once a day.\n" % (d["id"], d["name"], hours)))
            self.db.put_state(d["id"], "offline_noted", {"day": day})
        return written


PAPERWORK = re.compile(r"(?i)rechnung|invoice|\bbill|quittung|receipt|mahnung|vertrag|contract|lohnausweis|lohnabrechnung|"
                       r"payslip|steuer|\btax|police|policy|kontoauszug|statement|k(?:ü|ue)ndigung|versicherung|insurance")
SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
FILE_ID = re.compile(r"^([A-Za-z0-9_-][A-Za-z0-9._-]{0,63})/([A-Za-z0-9_-][A-Za-z0-9._-]{0,127})$")
# Retired share intents (the features were removed; the app still offers them until its next build): the share is
# handled like a plain one, and the hint says the option is gone so the session neither looks for the removed skill
# nor goes silent. The protocol keeps the enum values (an older app sends them; c2s.share.json is the app's schema).
RETIRED_HINT = "see what he wants: a plain share; the app's '%s' option is retired (that feature was removed), " \
               "so don't run it, and say so in one line if that is what he asked for"
NOTE_HINT = {"summary": RETIRED_HINT % "summarize", "trackid": "name the track (skill trackid)",
             "dance": RETIRED_HINT % "practice loops", "fish": "which fish? run the divelog fish-ID flow (skill divelog: identify, `divelog sight auto --at <photo time>`)",
             "what": "a photo: skill vision-actions (`vision gate FILE` first; paperwork -> private filing, else one "
                     "confirm card sent with `phone chat --buttons`)", "save": "keep it and say where", "file": "keep the file; ask what he wants"}


class M2b:
    """Shares, the APK updater. Mixed into Gateway next to M2."""

    def shares_dir(self):
        d = os.path.join(self.paths.state, "shares")
        os.makedirs(d, mode=0o700, exist_ok=True)
        return d

    async def i_file(self, request):
        """GET /internal/file?id=SHARE/NAME: one stored file of a share (or a Track ID clip in flight) for the brain;
        in hub mode the files stay here and `phone file get` fetches them. Only files under the shares dir."""
        m = FILE_ID.match(request.query.get("id") or "")
        if not m:
            return jerr(400, "usage", "id=SHARE/NAME")
        base = os.path.realpath(self.shares_dir())
        path = os.path.realpath(os.path.join(base, m.group(1), m.group(2)))
        if not path.startswith(base + os.sep) or not os.path.isfile(path):
            return jerr(404, "not_found")
        return web.FileResponse(path, headers={"Content-Type": "application/octet-stream", "Cache-Control": "no-store"})

    def paperwork(self, meta):
        for f in meta.get("files") or []:
            if (f.get("mime", "").startswith(("application/pdf", "image/")) or f.get("name", "").lower().endswith(".pdf")) \
                    and PAPERWORK.search(f.get("name", "")):
                return True
        return False

    async def on_share(self, dev_id, meta, stored=None):
        """-> 'new' | 'dup'. Writes ~/inbox/phone-share-*.md (his content: handle like his Telegram message)."""
        sid = meta["share_id"]
        if self.db.one("SELECT 1 FROM shares WHERE id=?", sid):
            return "dup"
        files = stored or []
        self.db.x("INSERT INTO shares(id, at, kind, path, bytes, mime) VALUES(?,?,?,?,?,?)", sid, now_s(), meta["kind"],
                  os.path.dirname(files[0][0]) if files else None, sum(f[2] for f in files),
                  files[0][1] if files else None)
        lines = ["# Phone share (his app)", "",
                 "From his phone app (device %s), %s, share %s: %s, intent %s." % (
                     dev_id, time.strftime("%d.%m %H:%M"), sid, meta["kind"], meta["intent"]),
                 "Handle it like his Telegram message: %s. Answer with `phone chat \"…\"` (or Telegram)." % NOTE_HINT.get(
                     meta["intent"], "see what he wants"),
                 "Paperwork (bills, contracts, letters) never belongs here: if one slipped through, don't open it; file it "
                 "privately (skill paperless) and point him to the vault bot.", ""]
        if meta.get("url"):
            lines.append("URL: " + meta["url"])
        if meta.get("text"):
            lines += ["Text:", meta["text"][:20000]]
        if files:
            lines.append("Files:")
            lines += ["- %s (%s, %d bytes)" % f for f in files]
        if await self.share_handoff(dev_id, sid, meta, files):
            log.info("device %s: share %s (%s, %d file(s)) -> session", dev_id, meta["kind"], meta["intent"], len(files))
            return "new"
        self.note("share", "\n".join(lines) + "\n")
        log.info("device %s: share %s (%s, %d file(s))", dev_id, meta["kind"], meta["intent"], len(files))
        return "new"

    async def share_handoff(self, dev_id, sid, meta, files):
        """chat_plugin on: the share reaches my session at once as an app event (no heartbeat wait). False -> note."""
        if not self.cfg.get("chat_plugin"):
            return False
        task = ("[share %s] %s share from his phone app (%s, intent %s): handle it like his Telegram message: %s. "
                "Answer with `phone chat \"…\"` (no --reply-to: a share has no chat message). Paperwork (bills, "
                "contracts, letters) never belongs here: if one slipped through, don't open it; file it privately "
                "(skill paperless) and point him to the vault bot."
                % (sid, meta["kind"], time.strftime("%d.%m %H:%M"), meta["intent"],
                   NOTE_HINT.get(meta["intent"], "see what he wants")))
        if meta.get("url"):
            task += "\nURL: " + meta["url"]
        if files:
            task += "\nFiles: " + "; ".join("%s (%s, %d bytes)" % f for f in files)
        if meta.get("text"):
            task += "\nText: " + meta["text"]
        if len(task) > 3600:                     # the plugin takes <= 4000 chars; long texts stay in the note
            return False
        return await self.plugin_handoff("share", task, device_id=dev_id)

    async def x_share(self, request):
        dev = request["device"]
        try:
            reader = await request.multipart()
        except Exception:
            return jerr(400, "bad_multipart")
        meta, stored, total = None, [], 0
        limit = int(self.cfg.get("share_max_bytes", 2 << 30))
        tmpdir = None
        try:
            while True:
                part = await reader.next()
                if part is None:
                    break
                if part.name == "meta":
                    try:
                        meta = json.loads(await part.read(decode=True))
                        self.proto.check("c2s", {"v": 1, "id": ulid(), "t": "share", "ts": now_s(), "body": meta})
                    except ProtocolError as e:
                        return jerr(422, "schema", e.msg, problems=e.problems)
                    except ValueError:
                        return jerr(400, "bad_json")
                    if self.paperwork(meta):
                        return jerr(422, "paperwork", "bills, contracts and letters go to the vault bot")
                    if self.db.one("SELECT 1 FROM shares WHERE id=?", meta["share_id"]):
                        return web.json_response({"ok": True, "accepted": 0, "dup": 1})
                    if shutil.disk_usage(self.paths.state).free < int(self.cfg.get("share_min_free", 20 << 30)):
                        return jerr(507, "disk_full")
                    tmpdir = os.path.join(self.shares_dir(), SAFE_NAME.sub("_", meta["share_id"])[:64])
                    os.makedirs(tmpdir, mode=0o700, exist_ok=True)
                elif part.name == "file":
                    if meta is None:
                        return jerr(400, "usage", "the meta part comes first")
                    if len(stored) >= 20:
                        return jerr(413, "too_many_files")
                    raw = urllib.parse.unquote(part.filename or "file").replace("\\", "/")
                    name = SAFE_NAME.sub("_", os.path.basename(raw))[-100:].lstrip(".") or "file"
                    path = os.path.join(tmpdir, "%02d-%s" % (len(stored) + 1, name))
                    n = 0
                    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
                    with os.fdopen(fd, "wb") as fh:
                        while True:
                            chunk = await part.read_chunk(1 << 20)
                            if not chunk:
                                break
                            n += len(chunk)
                            total += len(chunk)
                            if total > limit:
                                raise OverflowError
                            fh.write(chunk)
                    stored.append((path, part.headers.get("Content-Type", "application/octet-stream")[:100], n))
        except OverflowError:
            if tmpdir and os.path.isdir(tmpdir):
                shutil.rmtree(tmpdir, ignore_errors=True)
            return jerr(413, "too_large")
        if meta is None:
            return jerr(400, "usage", "meta part missing")
        res = await self.on_share(dev["id"], meta, stored)
        self.touch(dev["id"])
        return web.json_response({"ok": True, "accepted": 1 if res == "new" else 0, "dup": 1 if res == "dup" else 0})

    # ------------------------------------------------------------ updater
    def apk_dir(self):
        d = os.path.join(self.paths.state, "apk")
        os.makedirs(d, mode=0o700, exist_ok=True)
        return d

    async def i_update(self, request):
        """POST {apk: /abs/path.apk, code?, name?, notes?}: copy, hash, announce `app.update` to older devices."""
        r = await self._json(request) or {}
        src = str(r.get("apk") or "")
        m = re.search(r"-([0-9][0-9A-Za-z._+-]{0,31})-([0-9]{1,9})\.apk$", os.path.basename(src))
        code = int(r.get("code") or (m.group(2) if m else 0))
        name = str(r.get("name") or (m.group(1) if m else ""))
        if not src.endswith(".apk") or not os.path.isfile(src) or not code or not name:
            return jerr(400, "usage", "apk path claude-<name>-<code>.apk (or code + name)")
        with open(src, "rb") as fh:
            if fh.read(2) != b"PK":
                return jerr(400, "usage", "not an APK (zip)")
        dst = os.path.join(self.apk_dir(), "claude-%d.apk" % code)
        h = hashlib.sha256()
        tmp = dst + ".tmp"
        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with open(src, "rb") as fi, os.fdopen(fd, "wb") as fo:
            for chunk in iter(lambda: fi.read(1 << 20), b""):
                h.update(chunk)
                fo.write(chunk)
        os.replace(tmp, dst)
        body = {"code": code, "name": name, "sha256": h.hexdigest(), "size": os.path.getsize(dst),
                "path": "/v1/app/apk/%d" % code, "notes": [str(n)[:80] for n in (r.get("notes") or [])][:10]}
        try:
            self.proto.check("s2c", {"v": 1, "id": ulid(), "t": "app.update", "ts": now_s(), "ack": True, "body": body})
        except ProtocolError as e:
            return jerr(400, "usage", e.msg, problems=e.problems)
        self.db.put_state(GW, "app_latest", body)
        sent, online = 0, False
        for d in self.active():
            if int(d["app_code"] or 0) < code:
                _, on = await self.queue(d["id"], "app.update", body, 7 * 86400, key="app.update")
                sent += 1
                online = online or on
        return web.json_response(dict(body, ok=True, devices=sent, online=online,
                                      status="sent" if online else "queued" if sent else "stored"))

    async def x_app_latest(self, request):
        up, _ = self.db.get_state(GW, "app_latest", None)
        return web.json_response(up) if up else jerr(404, "not_found", "no release yet")

    async def x_app_apk(self, request):
        code = request.match_info["code"]
        if not code.isdigit():
            return jerr(404, "not_found")
        path = os.path.join(self.apk_dir(), "claude-%d.apk" % int(code))
        if not os.path.isfile(path):
            return jerr(404, "not_found")
        self.touch(request["device"]["id"])
        return web.FileResponse(path, headers={"Content-Type": "application/vnd.android.package-archive",
                                               "Cache-Control": "no-store"})
