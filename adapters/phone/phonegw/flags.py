"""Derived C1 flags from the phone's `state`/`event` frames: the interface between builder G (gateway) and builder S
(sensors logic).

Ownership (no two builders edit the same function bodies):
  G owns  Ctx, Fence, Effect, FlagEngine.on_state/on_event dispatch, now_view() assembly (vault `ha now` key set),
          the call sites in server.py and the effect runner (bridge tools, derived state, inbox notes).
  S owns  the bodies marked `# S:` below: place_kind, drive_state, awake_from_event, alarm_check, zone_effects,
          activity_effects. S may add private helpers named `_s_*` in this file. G-review before changing signatures.

Contract for every `# S:` function:
  - pure: inputs are plain dicts/ints, I/O only through the Ctx methods given; no network, no files, no clock other
    than ctx.now();
  - never return or log raw coordinates, SSIDs, BT addresses or free text (C2/C3); zone names, kinds, bands, ints only;
  - return values are JSON-serialisable.
Effects are *requests*; the gateway decides (rate limits, quiet hours, test mode) and executes them.
"""
from dataclasses import dataclass, field
from typing import Optional, Protocol as _Proto

# Zone-name prefixes the app may send (schema pattern ^[a-z][a-z0-9_-]{0,23}(:[a-z0-9_-]{1,32})?$)
ZONES = ("home", "office", "gym", "venue", "station", "airport", "away")
# Vault `ha now` place.kind values this module may produce (vault: home | zone | away; "unknown" -> place null)
PLACE_KINDS = ("home", "zone", "away", "unknown")
# Bridge tools an Effect may name (G maps them to binaries; anything else is dropped)
TOOLS = ("remind_place", "sleepin_release", "routine_leaving", "talk_button", "sleepin_sleep_end", "cat_mark", "crate_now",
         "card_deck", "home_ctx")
# Pushes an Effect may request (kind 'push'; G builds the text from data, rate-limits and sends; data = flags only)
PUSHES = ("buds_left",)
# Activity classes that count as "moving on" (vault/app activity kinds)
MOVING = ("walking", "running", "bicycle", "vehicle")
# Buds left-behind thresholds (override in config flags.*); see buds_left() for the rules
BUDS_DEFAULTS = {"buds_window_min": 10, "buds_home_window_min": 4, "buds_move_window_min": 5, "buds_still_min": 10}
# Drive flag timers (override in config flags.*); see drive_state() for the rules
DRIVE_DEFAULTS = {"drive_still_off_min": 15, "drive_walk_off_min": 5, "drive_stale_min": 20, "drive_max_h": 6}
# Real activity kinds (tilting/unknown keep the last real one)
ACTIVITY_KINDS = ("still", "walking", "running", "vehicle", "bicycle")


@dataclass(frozen=True)
class Fence:
    """A geofence definition as sent to the phone (G builds them from profile-get / ~/.config; S consumes names)."""
    name: str            # zone name, e.g. "home", "office", "venue:club"
    kind: str            # one of ZONES
    radius_m: int
    until: Optional[int] = None   # epoch s; None = permanent


@dataclass
class Effect:
    """What should happen because of a frame. kind: 'tool' | 'derived' | 'note' | 'push'.
    tool    -> name in TOOLS, args = short tokens (zone names, verbs), never payload text
    derived -> name = state key stored as 'flag.<name>' (latest only), data = JSON value
    note    -> name = inbox note stem (e.g. 'phone-offline'), data = small dict of flags (no text)
    push    -> name in PUSHES, data = flags only (zone names, epoch s); G writes the words"""
    kind: str
    name: str
    args: tuple = ()
    data: dict = field(default_factory=dict)


class Ctx(_Proto):
    """What flags logic may read (G implements it over the gateway DB)."""
    def now(self) -> int: ...                                   # epoch s
    def state(self, key: str): ...                              # -> (value, at_epoch_s) or (None, None)
    def events(self, kind: str, since_s: int) -> list: ...      # bodies (dicts) of this device, oldest first
    def fences(self) -> list: ...                               # list[Fence] currently active
    def cfg(self) -> dict: ...                                  # private flags config (thresholds), may be {}


# ---------------------------------------------------------------------------------------------------- S: logic
def place_kind(zone: Optional[str]) -> str:
    # S: map an app zone name to the vault's place.kind
    if not zone:
        return "unknown"
    base = zone.split(":", 1)[0]
    if base == "home":
        return "home"
    if base == "away":
        return "away"
    return "zone"


# Drive flag (flag.drive; `driving`, drive-brief and `whereabouts` read it through now_view). Inputs: the car state,
# the car on/off events, the bt class list and the activity class; flags and epoch seconds only. Rules:
#  1. On: the car connection (Android Auto projection or the car's Bluetooth) or activity "vehicle". The car
#     connection is the NEWEST report of: the car state, the car on/off events, a bt list without "car". The app's
#     queued "off" event can arrive without its state frame (22.10: the state stayed {projection: true}).
#  2. since = the real start (epoch s), stored: the newest car "on" (event at, else when the car state arrived) while
#     the car is on, else the vehicle activity's since. A drive still running at that moment keeps its stored since
#     (vehicle -> car handover). Consumers count minutes from it; never now - minutes.
#  3. Off although a signal still says on (until = the earliest of these; evaluated again on every read, so they need
#     no frame):
#     - activity still since after the start, for drive_still_off_min (15, drive-brief's chain gap);
#     - activity walking/running/bicycle since after the start, for drive_walk_off_min (5);
#     - not in a car (vehicle only) and the newest car/activity report older than drive_stale_min (20);
#     - on for drive_max_h (6, the car-event lookback): a lost "off" with no later report.
def _s_drive_cfg(ctx: Ctx) -> dict:
    cfg = ctx.cfg() or {}
    return dict(DRIVE_DEFAULTS, **{k: v for k, v in cfg.items() if k in DRIVE_DEFAULTS and isinstance(v, (int, float))})


def _s_car(ctx: Ctx, now: int, look_s: int) -> tuple:
    # S: (on, start, from_event, report): the car connection by its newest report; start = when it turned on (on
    # only), from_event = that start is a car "on" event (a real off -> on transition)
    car, car_at = ctx.state("car")
    on = bool(car and (car.get("projection") or car.get("bt")))
    report = int(car_at or 0)
    evs = [e for e in ctx.events("car", now - look_s) if e.get("state") in ("on", "off") and e.get("at")]
    last = evs[-1] if evs else None
    if last and int(last["at"]) >= report:          # a tie: the event (the transition itself) wins
        on, report = last["state"] == "on", int(last["at"])
    bt, bt_at = ctx.state("bt")
    if on and isinstance(bt, list) and "car" not in bt and int(bt_at or 0) > report:
        on, report = False, int(bt_at)
    if not on:
        return False, None, False, report or None
    if last and last["state"] == "on":
        return True, int(last["at"]), True, report
    return True, int(car_at or report), False, report


def drive_state(ctx: Ctx) -> dict:
    # S: {active, since (epoch s|None), minutes (int|None), until (epoch s|None), why, car_on (the car "on" event
    # at it started from)} — see the rules above
    now = int(ctx.now())
    cfg = _s_drive_cfg(ctx)
    car_on, car_start, car_event, car_report = _s_car(ctx, now, int(cfg["drive_max_h"] * 3600))
    raw, act_at = ctx.state("activity")
    act = raw if (raw or {}).get("kind") in ACTIVITY_KINDS else (ctx.state("flag.activity_last")[0] or {})
    kind = act.get("kind")
    act_since = int(act.get("since") or act_at or 0) or None
    off = {"active": False, "since": None, "minutes": None, "until": None, "why": "off"}
    if not (car_on or kind == "vehicle"):
        return off
    start = car_start if car_on else (act_since or now)
    prev, _ = ctx.state("flag.drive")
    # The stored drive was still running when this signal began: the same drive (vehicle <-> car handover, or the car
    # state re-sent). Not after a car "on" event the stored car drive had not seen: the car went off in between (its
    # off was lost).
    fresh_on = car_event and isinstance(prev, dict) and prev.get("why") == "car" and prev.get("car_on") != car_start
    if isinstance(prev, dict) and prev.get("active") and prev.get("since") and prev.get("until") \
            and int(prev["since"]) < start <= int(prev["until"]) and not fresh_on:
        start = int(prev["since"])
    start = min(start, now)
    ends = {"max": start + int(cfg["drive_max_h"] * 3600)}
    if act_since and act_since >= start and kind == "still":
        ends["still"] = act_since + int(cfg["drive_still_off_min"] * 60)
    elif act_since and act_since >= start and kind in ("walking", "running", "bicycle"):
        ends["walking"] = act_since + int(cfg["drive_walk_off_min"] * 60)
    if not car_on:
        ends["stale"] = max(int(act_at or 0), int(car_report or 0)) + int(cfg["drive_stale_min"] * 60)
    why, until = min(ends.items(), key=lambda kv: kv[1])
    if now >= until:
        return dict(off, why=why)
    return {"active": True, "since": start, "minutes": (now - start) // 60, "until": until,
            "why": "car" if car_on else "vehicle", "car_on": car_start if car_event else None}


def awake_from_event(ev: dict) -> Optional[str]:
    # S: 'awake' | None for an awake event (unlocks alone is only a context flag, never a release)
    if ev.get("kind") == "awake" and ev.get("how") in ("interaction", "sleep_end"):
        return "awake"
    return None


def alarm_check(alarm_next: Optional[int], depart_at: Optional[int], cfg: dict) -> dict:
    # S: compare his next phone alarm with the next leave-by (spec #8) -> {ok, reason, slack_min}.
    # Problem when there is no alarm, or the alarm is later than leave-by - ready_min (default 30 min).
    if not depart_at:
        return {"ok": True, "reason": "no_departure", "slack_min": None}
    if not alarm_next:
        return {"ok": False, "reason": "no_alarm", "slack_min": None}
    need = int(cfg.get("ready_min", 30)) * 60
    slack = (depart_at - need - alarm_next) // 60
    return {"ok": slack >= 0, "reason": "ok" if slack >= 0 else "alarm_late", "slack_min": int(slack)}


def zone_effects(ev: dict, ctx: Ctx) -> list:
    # S: effects of a zone event (place reminders on enter/exit; leaving home -> routine card)
    out = []
    if ev.get("dir") in ("enter", "exit"):
        out.append(Effect("tool", "remind_place", (ev["zone"].split(":", 1)[0], ev["dir"])))
    return out


def activity_effects(prev: Optional[dict], cur: dict, ctx: Ctx) -> list:
    # S: effects of an activity change: still -> moving may mean he left a place without a fence (Buds guard)
    if (prev or {}).get("kind") == "still" and (cur or {}).get("kind") in MOVING:
        return buds_left(ctx, "move", at=cur.get("since"))
    return []


# ---------------------------------------------------------------------------------------------------- S: Buds guard
# Buds left behind (idea #2; he lost his Buds3 Pro once). Inputs are C1 flags only: the bt "buds" on/off event, the
# zone name, the activity class and the town; never coordinates. Rules:
#  1. A buds "off" event records flag.buds = {on: false, at, zone (fence he is in, or null), town, still_since}.
#     A buds "on" event clears it. The app's state.bt list must not show buds either (a missed "on" event).
#  2. Leaving a fence (zone exit) he disconnected in, within buds_window_min (10) of the disconnect -> alert.
#  3. Home is special: Buds charging in the case at home is normal and leaving them at home is not a loss. Only the
#     "walked out of range" signature counts: the disconnect within buds_home_window_min (4) before the home exit
#     (BT range ~10 m vs the fence radius plus ~120 s fence responsiveness, so 2 min would miss most real cases).
#  4. No fence (a cafe, a friend's place): he had been still >= buds_still_min (10) when they disconnected, and
#     activity turns still -> walking/running/bicycle/vehicle within buds_move_window_min (5) -> alert.
#  5. G then waits a grace period (buds_grace_s, 90 s) and sends only if the buds are still off; one alert per
#     disconnect, at most 3 a day; config buds_guard false turns it off. A window of 0 disables that rule.
# Accepted miss: Buds put into the closed case and the case left on a table look exactly like Buds in his pocket.
def buds_effects(ev: dict, ctx: Ctx) -> list:
    # S: bookkeeping for a bt event of class "buds" -> derived flag.buds
    if ev.get("dev") != "buds":
        return []
    at = int(ev.get("at") or ctx.now())
    if ev.get("state") == "on":
        return [Effect("derived", "buds", data={"on": True, "at": at})]
    place, _ = ctx.state("place")
    zone = (place or {}).get("zone")
    act, _ = ctx.state("activity")
    still = (act or {}).get("since") if (act or {}).get("kind") == "still" else None
    return [Effect("derived", "buds", data={"on": False, "at": at, "zone": zone if zone and zone != "away" else None,
                                            "town": (place or {}).get("town"), "still_since": still})]


def buds_left(ctx: Ctx, trigger: str, zone: Optional[str] = None, at: Optional[int] = None) -> list:
    # S: trigger "zone" (exit of `zone`) or "move" (still -> moving) -> [Effect push buds_left] or []
    cfg = dict(BUDS_DEFAULTS, **{k: v for k, v in ctx.cfg().items() if k in BUDS_DEFAULTS})
    b, _ = ctx.state("flag.buds")
    if not isinstance(b, dict) or b.get("on") is not False or not b.get("at"):
        return []
    bt, _ = ctx.state("bt")
    if isinstance(bt, list) and "buds" in bt:
        return []
    at = int(at or ctx.now())
    gap = at - int(b["at"])
    if gap < -60:                                  # the trigger happened before the disconnect
        return []
    if trigger == "zone":
        if not zone or b.get("zone") != zone:
            return []
        win = cfg["buds_home_window_min"] if place_kind(zone) == "home" else cfg["buds_window_min"]
    elif trigger == "move":
        if b.get("zone"):                          # inside a fence: its exit decides
            return []
        still = b.get("still_since")
        if not still or int(b["at"]) - int(still) < int(cfg["buds_still_min"]) * 60:
            return []
        win = cfg["buds_move_window_min"]
    else:
        return []
    if not win or gap > int(win) * 60:
        return []
    return [Effect("push", "buds_left", data={"zone": b.get("zone"), "town": b.get("town"), "off_at": int(b["at"]),
                                              "trigger": trigger})]


# ---------------------------------------------------------------------------------------------------- G: home context
def home_ctx_change(partial: dict, prev: dict) -> Optional[str]:
    # Home autopilot 2: the state bits HA's scenes read (input_text.phone_context via `home-ctx feed`). Only real flips
    # count (battery level ticks and same-value frames do not): screen on/off, dnd, charging, place zone/band.
    prev = prev or {}

    def pick(d, k, sub=None):
        v = (d or {}).get(k)
        return v.get(sub) if sub and isinstance(v, dict) else v
    for key, sub in (("screen", "on"), ("dnd", None), ("battery", "charging"), ("place", "zone"), ("place", "band")):
        if key in partial and pick(partial, key, sub) != pick(prev, key, sub):
            return key if not sub or key == "place" else "%s_%s" % (key, sub)
    return None


# ---------------------------------------------------------------------------------------------------- G: dispatch
class FlagEngine:
    """G: called by the server for every accepted state/event frame; returns effects to run."""

    def on_state(self, ctx: Ctx, partial: dict, prev: dict) -> list:
        out = []
        if "activity" in partial:
            out += activity_effects(prev.get("activity"), partial["activity"], ctx)
        if "car" in partial or "activity" in partial or "bt" in partial:
            out.append(Effect("derived", "drive", data=drive_state(ctx)))
        why = home_ctx_change(partial, prev)
        if why:
            out.append(Effect("tool", "home_ctx", (why,)))
        return out

    def on_event(self, ctx: Ctx, ev: dict) -> list:
        k = ev.get("kind")
        if k == "zone":
            out = zone_effects(ev, ctx)
            if str(ev.get("zone") or "").split(":", 1)[0] == "home" and ev.get("dir") in ("enter", "exit"):
                out.append(Effect("tool", "home_ctx", ("zone_" + ev["dir"],)))
            if ev.get("dir") == "exit":
                out += buds_left(ctx, "zone", zone=ev.get("zone"), at=ev.get("at"))
            return out
        if k == "bt":
            return buds_effects(ev, ctx)
        if k == "awake" and awake_from_event(ev) == "awake":
            return [Effect("tool", "sleepin_release", (ev.get("how", ""),)), Effect("derived", "awake", data={"at": ev.get("at") or ctx.now()})]
        if k == "car":
            return [Effect("derived", "drive", data=drive_state(ctx))]
        if k == "tile" and ev.get("name") == "leaving":
            return [Effect("tool", "routine_leaving", ())]
        if k == "tile" and ev.get("name") == "talk":
            return [Effect("tool", "talk_button", ())]
        if k == "tile" and ev.get("name") == "deck":                 # Board "Decide now": Waiting-on-you as cards
            return [Effect("tool", "card_deck", ())]
        return []
