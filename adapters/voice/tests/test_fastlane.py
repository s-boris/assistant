"""Fast lane (read-only quick tools) and the call recap note: fakes only, no real call, Gemini or network."""
import asyncio
import glob
import json
import os
import stat
import types
from datetime import datetime

import pytest

from util import RecFE, Rig, StubProvider, until
from voice_relay import fastlane, recap
from voice_relay.calllog import CallLog, calls_dir, format_call, load_calls
from voice_relay.config import Config
from voice_relay.fastlane import FastError, FastLane, clean
from voice_relay.providers import ASK_TOOL, GeminiLive, make_provider
from voice_relay.server import instructions
from voice_relay.session import VoiceSession

NOW = datetime.fromisoformat("2026-10-20T13:00:00+02:00")
READ_ONLY = {("vault", "ha", "now", "--json"), ("gohome", "--json"), ("calcache", "age", "--json"),
             ("calcache", "show", "--days", "8", "--json"), ("tg-cmd", "train"), ("tg-cmd", "space"),
             ("tg-cmd", "queue"), ("tg-cmd", "gym"), ("cat-corner", "status", "--json"), ("cat-supplies", "--json"),
             ("media", "finish", "-n", "3", "--json"), ("tg-cmd", "rave"), ("tg-cmd", "trip"),
             ("tg-cmd", "trip", "Islands"), ("bills", "list", "--open", "--json"), ("remind", "list"),
             ("parcels", "speak"), ("gohome", "Zürich HB", "--json"),
             ("gohome", "Bern, Bahnhof (Ost)", "--json")}

PLACE_WORK = {"place": {"kind": "zone", "zone": "Work", "band": "region", "town": None, "flag": "away"},
              "battery": 77, "signals": {"x": "private"}, "activity": "still"}
GOHOME = {"home": False, "origin": "Olten", "next": [
    {"dep": "2026-10-20T13:04:00+02:00", "arr": "2026-10-20T14:25:00+02:00", "changes": 1, "platform": "4",
     "lines": ["S12", "Bus 7"], "link": "https://app.example/x"},
    {"dep": "2026-10-20T13:19:00+02:00", "arr": "2026-10-20T14:29:00+02:00", "changes": 2, "platform": "4",
     "lines": ["S11", "IC"], "link": "https://app.example/y"}],
    "last": {"dep": "2026-10-20T23:49:00+02:00", "arr": "2026-10-21T01:14:00+02:00", "changes": 1,
             "lines": ["S11", "S9"]}}
EVENTS = [
    {"start": "2026-10-20", "end": "2026-10-21", "all_day": True, "title": "@Olten", "town": "Olten",
     "flags": {}},
    {"start": "2026-10-20T09:00:00+02:00", "end": "2026-10-20T10:00:00+02:00", "all_day": False,
     "title": "Standup", "town": None, "flags": {}},
    {"start": "2026-10-21T21:00:00+02:00", "end": "2026-10-21T22:00:00+02:00", "all_day": False,
     "title": "Shuffle", "town": "Zürich", "flags": {}},
    {"start": "2026-10-22T18:14:00+02:00", "end": "2026-10-22T19:23:00+02:00", "all_day": False,
     "title": "🚆 Home → Zürich HB", "town": "Hometown", "flags": {"travel": "train"}}]
CARDS = {
    "train": "🚆 Trains · home = door to door\nThu 22.10 18:14 home → Zürich HB 19:23\n   S9 · ✅ as planned\n"
             "Fri 30.10 16:55 home → Bern 19:17\n   Bus 21 › RE48 · ✅ as planned\nSat 31.10 05:14 Bern → home 07:30\n"
             "   IC1 · ✅ as planned\n",
    "space": "💾 Server space · 13:01\n\nArray 86% full\n▓▓▓▓▓▓▓▓▓░\n18.2 TB free of 132.0 TB\n\nFull in ~80 days "
             "(08 Jan)\nGrowing ~203 GB/day\n\nPools\n• cache 58% · 415 GB free\n",
    "queue": "📥 Nothing downloading.\n",
    "gym": "🏋️ Next: B · Deadlift + Military Press (~56 min)\nLast session 15.10. (A, 5 d ago) · streak 3 wk\n\n"
           "1  Barbell Deadlift  3×4–6 @ 77.5 kg\n5a Cable Seated Row  3×8–12 @ 65 kg\na/b = pair\n"
           "📅 Free evenings: today\n",
}

CAT_STATUS = {"view": {"now": "2026-10-20T11:00:00+00:00",
                       "robot": {"status": "rdy", "label": "ready", "drawer": 59.0, "litter": 80.0},
                       "pets": [{"name": "Mia", "visits_today": 4, "last_visit": "2026-10-20T08:26:58+00:00",
                                 "weight_cur": 3.937}],
                       "feeders": [{"food_g_today": 10.0, "next_feed": "2026-10-21T11:00:00+00:00", "low": False,
                                    "offline_since": None, "missed_meal": None, "schedule_off_since": None}],
                       "fountains": [{"low": False, "offline_since": None}]},
              "problems": [], "trip": None}
CAT_SUPPLIES = {"rates": {"filter_pct_now": 33.0, "filter_empty_on": "2026-10-30", "desiccant_days_left": -28.0,
                          "feeder_battery": "low"},
                "items": [{"line": "Secretbrand kibble"}], "questions": ["What is at home of: Secretbrand?"]}
FINISH = {"generated": "2026-10-20T11:00:00Z", "items": [
    {"title": "Show A", "episode": "S04E10", "left": 4, "total": 49, "state": "next", "pos_min": 0},
    {"title": "Show B", "episode": "S02E06", "left": 3, "total": 16, "state": "resume", "pos_min": 36},
    {"title": "Show C", "episode": "S01E06", "left": 2, "total": 7, "state": "next"},
    {"title": "Show D", "episode": "S01E02", "left": 9, "total": 10, "state": "next"}]}
CARDS.update({
    "rave": "🎧 Raves · next 4\nThu 22.10 · Artist One · Zürich · 🎟 going\nFri 30.10 · Artist Two · Bern · 🎟 going\n"
            "Sat 28.11 · Artist Three · Zürich · ✈️ clashes with Islands\ndate tba · Artist Four · Fest lineup, Town\n",
    "trip": "🌴 Islands · Fri 20.11–6.12 (16 nights) · in 31 days\n🏝 Isle A 21–27.11 › Isle B 27.11–1.12\n"
            "✈️ flight out Fri 20.11 21:35\n✈️ back Sun 6.12\nOpen:\n☐ stays: villas?\n"
            "☐ 07.11 apply for the permit (permit.example.org)…\n+ 3 more (ask me)\n🗺 https://example.org/map\n"
            "📦 Offline pack 9 KB (20.10) · “send the pack” → ~/reports/trips/x.html\n"
            "Then: 🇧🇪 City trip 5–6.11 · 📦 pack ready\n",
})
BILLS = [{"id": 1, "payee": "Helsana Versicherungen AG", "amount": 412.35, "currency": "CHF", "due": "2026-10-23",
          "iban": "CH9300762011623852957", "ref": "210000000003139471430009017"},
         {"id": 2, "payee": "Hans Muster", "amount": 80, "currency": "CHF", "due": "2026-10-25"},
         {"id": 3, "payee": "Steueramt Musterstadt", "amount": 1500, "currency": "CHF", "due": "2026-10-15"}]
REMIND = """ 1. 2026-10-20 12:00  [owner] Old one already past
 2. 2026-10-20 18:00  [owner] 🌴 Trip: book all nights by ~Sunday (the permit needs them). Friend's question is open
 3. 2026-10-20 18:58  Assistant follow-up SECRETFOLLOWUP check the vault
 4. 2026-10-26 18:30  [owner] 📅 Car: winter tyres before the cold (Oct to Easter)  (every Mon)
 5. 2026-11-07 09:00  [owner] Pay CHF 7'258 into the account https://bank.example/x
 6. 2026-11-08 09:00  [owner] Reply to [private: name] about the thing
 7. 2026-11-09 09:00  [owner] Fifth one
 8. 📍 when home: 🔌 Power-cycle the sensors
"""


def mkcfg(tmp, **over):
    prof = os.path.join(str(tmp), "profile.json")
    with open(prof, "w") as fh:
        json.dump({"home": {"town": "Hometown", "street": "Secret Street 1", "lat": 47.1, "lon": 8.1},
                   "work": {"town": "Worktown"}, "locale": {"tz": "Europe/Zurich", "country": "CH"}}, fh)
    kw = dict(state_dir=os.path.join(str(tmp), "state"), inbox_dir=os.path.join(str(tmp), "inbox"),
              profile_file=prof, recap_grace_s=0)
    kw.update(over)
    return Config(**kw)


class Fakes:
    def __init__(self, outputs=None, delay=0.0, fail=()):
        self.outputs = dict(outputs or {})
        self.delay, self.fail = delay, set(fail)
        self.ran, self.http, self.oks = [], [], {}

    async def run(self, argv, ok=None):
        self.ran.append(tuple(argv))
        if ok is not None:
            self.oks[tuple(argv)] = tuple(ok)
        await asyncio.sleep(self.delay)
        if argv[0] in self.fail:
            raise FastError("%s exit 1" % argv[0])
        key = " ".join(argv[:2]) if argv[0] in ("tg-cmd", "calcache") else argv[0]
        out = self.outputs.get(key)
        if out is None:
            raise FastError("no fake for %s" % key)
        return out if isinstance(out, str) else json.dumps(out)

    async def get(self, url, params):
        self.http.append((url, dict(params)))
        if "geocoding" in url:
            return {"results": [{"latitude": 40.0, "longitude": 9.0, "country_code": "DE"},
                                {"latitude": 46.95, "longitude": 7.45, "country_code": "CH"}]}
        return {"current": {"temperature_2m": 13.6, "weather_code": 3},
                "daily": {"temperature_2m_min": [9.6, 7.2], "temperature_2m_max": [16.4, 12.9],
                          "weather_code": [61, 2], "precipitation_probability_max": [60, 10]}}


def all_outputs(place=PLACE_WORK):
    return {"vault": place, "gohome": GOHOME, "calcache age": {"cached": True, "hours": 5.0},
            "calcache show": EVENTS, "tg-cmd train": CARDS["train"], "tg-cmd space": CARDS["space"],
            "tg-cmd queue": CARDS["queue"], "tg-cmd gym": CARDS["gym"], "cat-corner": CAT_STATUS,
            "cat-supplies": CAT_SUPPLIES, "media": FINISH, "tg-cmd rave": CARDS["rave"],
            "tg-cmd trip": CARDS["trip"], "bills": BILLS, "remind": REMIND,
            "parcels": "One parcel waits at the Townstation station, pick it up by Thursday 29.10. Shopco: 3 items, "
                       "arriving between Saturday and Friday 6.11.\n"}


def lane(tmp, fk, mono=None, **over):
    return FastLane(mkcfg(tmp, **over), run=fk.run, get_json=fk.get, now=lambda: NOW, mono=mono)


def call(ln, name, args=None):
    return asyncio.run(ln.call(name, args or {}))


# --- the quick tools ----------------------------------------------------------------------------------------

def test_weather_uses_town_only_and_caches_geocode(tmp_path):
    fk = Fakes(all_outputs())
    ln = lane(tmp_path, fk)
    out = call(ln, "weather")
    assert out.startswith("In Worktown: now 14 degrees, overcast. Today 10 to 16 degrees, light rain, rain chance 60")
    geo = [p for u, p in fk.http if "geocoding" in u][0]
    assert geo["name"] == "Worktown" and set(geo) == {"name", "count", "language", "format"}
    wx = [p for u, p in fk.http if "forecast" in u][0]
    assert (wx["latitude"], wx["longitude"]) == (46.95, 7.45)          # the CH match, the town's coordinates
    for _u, p in fk.http:                                             # never his address or home coordinates
        assert "Secret" not in json.dumps(p) and 47.1 not in p.values()
    assert "77" not in out and "private" not in out and "still" not in out   # no other vault flags spoken
    fk.http.clear()
    assert call(ln, "weather", {"when": "tomorrow"}) == "In Worktown: tomorrow 7 to 13 degrees, partly cloudy, rain chance 10 percent."
    assert not [u for u, _ in fk.http if "geocoding" in u]            # cached
    assert stat.S_IMODE(os.stat(os.path.join(str(tmp_path), "state")).st_mode) == 0o700


def test_weather_place_rules(tmp_path):
    for place, want in (({"place": {"flag": "home", "zone": "home"}}, "At home in Hometown"),
                        ({"place": {"flag": "away", "town": "Bern"}}, "In Bern"),
                        ("not json", "At home in Hometown")):
        fk = Fakes(all_outputs(place))
        assert call(lane(tmp_path, fk), "weather").startswith(want)
    fk = Fakes(all_outputs(), fail={"vault"})                         # vault down → home town
    assert call(lane(tmp_path, fk), "weather").startswith("At home in Hometown")


def test_way_home_calendar_and_cards(tmp_path):
    fk = Fakes(all_outputs())
    ln = lane(tmp_path, fk)
    wh = call(ln, "way_home")
    assert wh == ("From Olten: next 13:04 from platform 4, S12 then Bus 7, one change, home at 14:25. "
                  "Then 13:19, home at 14:29. Last one tonight 23:49, home at 01:14.")
    assert "http" not in wh
    assert call(ln, "calendar", {"day": "today"}) == "Today: all day Olten."    # 09:00 is over
    assert call(ln, "calendar", {"day": "tomorrow"}) == "Tomorrow: 21:00 Shuffle in Zürich."
    nxt = call(ln, "calendar")
    assert nxt == "Next: tomorrow 21:00 Shuffle in Zürich; Thursday 18:14 Home to Zürich HB."
    tr = call(ln, "trains")
    assert tr == "Thu 22.10 18:14 home to Zürich HB 19:23, S9, as planned. Fri 30.10 16:55 home to Bern 19:17, Bus 21 then RE48, as planned."
    sp = call(ln, "server_space")
    assert sp.startswith("Array 86% full. 18.2 TB free of 132.0 TB. Full in about 80 days (08 Jan)")
    assert "▓" not in sp and "13:01" not in sp
    assert call(ln, "downloads") == "Nothing downloading."
    gym = call(ln, "gym")
    assert gym.startswith("Next: B, Deadlift + Military Press (about 56 min). Last session 15.10")
    assert "Exercises: Barbell Deadlift, Cable Seated Row" in gym and "kg" not in gym
    fk.outputs["gohome"] = {"home": True}
    assert call(ln, "way_home") == "You're home."
    assert set(fk.ran) <= READ_ONLY                                   # only read commands, ever


def test_way_home_answers_when_origin_unknown(tmp_path):
    """Not knowing where he is is an answer ("which stop?"), not a failure: voicecheck 22.10 warned when the
    location read gave no origin (unknown place, a zone or away without a town, near the home town)."""
    def wh(out, args=None):
        fk = Fakes(dict(all_outputs(), gohome=out))
        return call(lane(tmp_path, fk), "way_home", args), fk

    base = {"home": False, "near": None, "origin": None, "unknown_stop": None, "next": [], "last": None}
    ask = "I don't know where you are right now. Which stop or town are you at?"
    assert wh(base)[0] == ask
    assert wh({"home": False, "origin": None, "next": []})[0] == ask              # the pre-unknown_stop schema too
    near, _ = wh(dict(base, near="Hometown"))
    assert near == "You're close to home. Tell me the stop if you want a bus or train."
    assert "Hometown" not in near                                                  # no town read out for "near"
    assert wh(dict(base, origin="Olten"))[0] == \
        "I found no connection home from Olten. Is there another stop near you?"
    assert wh(dict(base, unknown_stop="Nowhere Special"), {"from": "Nowhere Special"})[0] == \
        "I can't find a stop called Nowhere Special. Which station or town are you at?"
    assert wh({"home": True}, {"from": "home"})[0] == "You're home."


def test_way_home_from_argument(tmp_path):
    fk = Fakes(all_outputs())
    ln = lane(tmp_path, fk)
    assert call(ln, "way_home", {"from": "  Zürich   HB "}).startswith("From Olten: next 13:04")
    assert fk.ran[-1] == ("gohome", "Zürich HB", "--json")                        # one argv item, spaces collapsed
    call(ln, "way_home", {"from": "Bern, Bahnhof (Ost)"})
    assert fk.ran[-1] == ("gohome", "Bern, Bahnhof (Ost)", "--json")
    for bad in ("--help", "-x Basel", "x", "A" * 61, "Basel; rm -rf /", "Bern | cat", "$(id)", None, 7):
        call(ln, "way_home", {"from": bad})
        assert fk.ran[-1] == ("gohome", "--json"), bad                            # ignored: phone and calendar decide
    assert set(fk.ran) <= READ_ONLY
    assert fastlane.fallback_task("way_home", {"from": "Zürich HB"}) == \
        "How do I get home from Zürich HB? Next connections and the last one tonight."
    assert fastlane.fallback_task("way_home", {}) == \
        "How do I get home from where I am? Next connections and the last one tonight."
    decl = next(t for t in fastlane.TOOLS if t["name"] == "way_home")
    assert decl["parameters"]["properties"] == {"from": {"type": "string"}}
    assert "required" not in decl["parameters"]


def test_errors_raise_for_fallback(tmp_path):
    fk = Fakes(all_outputs(), fail={"tg-cmd"})
    with pytest.raises(FastError):
        call(lane(tmp_path, fk), "server_space")
    fk = Fakes(dict(all_outputs(), **{"calcache age": {"cached": True, "hours": 50}}))
    with pytest.raises(FastError, match="stale"):
        call(lane(tmp_path, fk), "calendar")
    with pytest.raises(FastError):                                    # gohome broken (exit 1: transport API down)
        call(lane(tmp_path, Fakes(all_outputs(), fail={"gohome"})), "way_home")
    with pytest.raises(FastError):                                    # gohome printing something that isn't JSON
        call(lane(tmp_path, Fakes(dict(all_outputs(), gohome="Where are you?\n"))), "way_home")
    with pytest.raises(FastError, match="unknown"):
        call(lane(tmp_path, Fakes()), "buy_things")
    slow = Fakes(all_outputs(), delay=5.0)
    with pytest.raises(FastError, match="timeout"):
        call(lane(tmp_path, slow, fast_timeout_s=0.2), "downloads")
    assert FastLane(mkcfg(tmp_path, fast_timeout_s=9)).timeout == 3.0    # capped


# --- round 2: cat, finish, rave, trip, bills_due, reminders --------------------------------------------------

def test_round2_tools_speak_short_answers(tmp_path):
    fk = Fakes(all_outputs())
    ln = lane(tmp_path, fk)
    cat = call(ln, "cat")
    assert cat == ("Mia: 4 litter visits today, last at 10:26, weighs 3.9 kilos. Feeder: 10 grams today, next meal "
                   "tomorrow at 13:00. Litter box ready, litter 80%, drawer 59% full. Fountain OK, filter at 33%, "
                   "change it on Friday 30.10. Also, the feeder's desiccant is overdue and the feeder's backup "
                   "battery is low.")
    assert "Secretbrand" not in cat
    assert fk.oks[("cat-corner", "status", "--json")] == (0, 1)        # HA partly down: still speak what we have
    assert call(ln, "finish") == ("Closest to the end: Show A, 4 of 49 episodes left, next season 4 episode 10; "
                                  "Show B, 3 of 16 episodes left, next season 2 episode 6 from minute 36; Show C, "
                                  "2 of 7 episodes left, next season 1 episode 6.")
    assert fk.oks[("media", "finish", "-n", "3", "--json")] == (0, 3)
    assert call(ln, "rave") == ("Next: Thu 22.10, Artist One, Zürich, going; Fri 30.10, Artist Two, Bern, going; "
                                "Sat 28.11, Artist Three, Zürich, clashes with Islands.")
    trip = call(ln, "trip")
    assert trip == ("Islands, Fri 20.11 to 6.12 (16 nights), in 31 days. Isle A 21 to 27.11 then Isle B 27.11 to "
                    "1.12. Flight out Fri 20.11 21:35. Back Sun 6.12. 5 open to-dos. After that: City trip 5 to 6.11.")
    assert "permit" not in trip and "pack" not in trip and "example" not in trip   # to-dos counted, never read out
    call(ln, "trip", {"name": "Islands"})
    call(ln, "trip", {"name": "x; rm -rf /"})                          # junk name → no argument at all
    assert fk.ran.count(("tg-cmd", "trip", "Islands")) == 1 and fk.ran.count(("tg-cmd", "trip")) == 1
    assert set(fk.ran) <= READ_ONLY                                   # only read commands, ever


def test_round2_empty_answers(tmp_path):
    fk = Fakes(dict(all_outputs(), media={"items": []}, bills=[], remind=" 1. 2026-10-21 09:00  mine only\n",
                    **{"tg-cmd rave": "🎧 No upcoming raves on the radar.\n", "tg-cmd trip": "🧳 No trip planned.\n"}))
    ln = lane(tmp_path, fk)
    assert call(ln, "finish") == "No paused shows to finish."
    assert call(ln, "rave") == "No upcoming raves on the radar."
    assert call(ln, "trip") == "No trip planned."
    assert call(ln, "bills_due") == "No open bills."
    assert call(ln, "reminders") == "No reminders set."                # the assistant's follow-ups don't count
    fk.outputs["tg-cmd trip"] = "🧳 No trip ahead called “Mars”.\n/trip list shows them.\n"
    assert call(lane(tmp_path, fk), "trip", {"name": "Mars"}) == "No trip ahead called “Mars”."


def test_bills_due_speaks_no_amounts_or_payees(tmp_path):
    fk = Fakes(all_outputs())
    out = call(lane(tmp_path, fk), "bills_due")
    assert out == ("3 open bills. One is overdue, since Thursday 15.10. Next due on Friday, health insurance. "
                   "2 are due within a week.")
    for bad in ("412", "80", "1500", "Helsana", "Hans", "Muster", "Steueramt", "CH93", "2100", "CHF"):
        assert bad not in out
    fk.outputs["bills"] = [BILLS[1]]                                   # a person as payee: no category, no name
    assert call(lane(tmp_path, fk), "bills_due") == "One open bill. Next due on Sunday."
    assert fastlane._bill_category("Swisscom (Schweiz) AG") == "phone and internet"
    assert fastlane._bill_category("SERAFE AG") == "the radio and TV fee"
    assert fastlane._bill_category("Someone") == ""


def test_reminders_his_own_only_no_private_text(tmp_path):
    fk = Fakes(all_outputs())
    out = call(lane(tmp_path, fk), "reminders")
    assert out.startswith("You have 5. Next: today 18:00, Trip: book all nights by about Sunday; Monday 18:30, Car: "
                          "winter tyres before the cold; Saturday 7.11 09:00, Pay ")
    assert out.endswith(" into the account. Plus 1 by place, like when you get home.")
    for bad in ("SECRETFOLLOWUP", "Old one", "Friend", "permit", "every", "7'258", "258", "http", "bank.example",
                "Power-cycle"):
        assert bad not in out
    assert fastlane._rem_title("Reply to [private: name] about the thing") == "a private one"
    assert fastlane._rem_title("🔌 One two three four five six seven eight nine ten") == \
        "One two three four five six seven eight nine"


def test_round2_cache_ttl_and_errors(tmp_path):
    t = [100.0]
    fk = Fakes(all_outputs())
    ln = lane(tmp_path, fk, mono=lambda: t[0])
    call(ln, "rave")
    call(ln, "rave")
    assert fk.ran.count(("tg-cmd", "rave")) == 1                      # repeat question: cached
    t[0] += fastlane.CACHE_TTL_S["rave"] + 1
    call(ln, "rave")
    assert fk.ran.count(("tg-cmd", "rave")) == 2                      # expired
    call(ln, "reminders")
    t[0] += fastlane.CACHE_TTL_S["reminders"] + 1
    call(ln, "reminders")
    assert fk.ran.count(("remind", "list")) == 2
    call(ln, "server_space")
    call(ln, "server_space")
    assert fk.ran.count(("tg-cmd", "space")) == 2                     # round-1 tools stay uncached
    fk.fail = {"bills"}
    with pytest.raises(FastError):
        call(ln, "bills_due")
    fk.fail = set()
    assert call(ln, "bills_due").startswith("3 open bills")           # failures are not cached
    fk2 = Fakes(all_outputs(), fail={"cat-supplies"})                  # supplies down: the rest still speaks
    cat = call(lane(tmp_path, fk2), "cat")
    assert cat.startswith("Mia: 4 litter visits") and cat.endswith("Fountain OK.")
    with pytest.raises(FastError):
        call(lane(tmp_path, Fakes(all_outputs(), fail={"cat-corner"})), "cat")
    with pytest.raises(FastError, match="no cat data"):
        call(lane(tmp_path, Fakes(dict(all_outputs(), **{"cat-corner": {"view": {}, "problems": []}}))), "cat")
    probs = dict(CAT_STATUS, problems=[{"key": "drawer", "text": "🐾 Drawer full (95 %)"}])
    assert call(lane(tmp_path, Fakes(dict(all_outputs(), **{"cat-corner": probs}))), "cat").startswith(
        "Heads-up: Drawer full (95 %). Mia:")
    slow = Fakes(all_outputs(), delay=5.0)
    with pytest.raises(FastError, match="timeout"):
        call(lane(tmp_path, slow, fast_timeout_s=0.2), "cat")


def test_run_cmd_ok_codes_and_text_cards(tmp_path, monkeypatch):
    """The real subprocess runner against fake scripts in fast_bin_dir (never the real tools)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("tg-cmd", 'echo "pictures=${TG_CMD_PICTURES:-unset} $*"'),
                       ("media", "echo '{\"items\": [{\"title\": \"Show A\", \"left\": 1, \"total\": 2}]}'; exit 3"),
                       ("bills", "echo '[]'; exit 3")):
        f = bindir / name
        f.write_text("#!/bin/sh\n" + body + "\n")
        f.chmod(0o755)
    monkeypatch.setenv("TG_CMD_PICTURES", "1")
    ln = FastLane(mkcfg(tmp_path, fast_bin_dir=str(bindir)), now=lambda: NOW)
    assert asyncio.run(ln._run_cmd(["tg-cmd", "rave"])).strip() == "pictures=unset rave"
    assert call(ln, "finish") == "Closest to the end: Show A, 1 of 2 episodes left."   # exit 3 = some didn't load
    with pytest.raises(FastError, match="exit 3"):
        call(ln, "bills_due")


def test_round2_session_answers_and_fallbacks(tmp_path):
    async def main():
        fk = Fakes(all_outputs(), fail={"bills"})
        cfg, prov, ho, s = await _run_session(tmp_path, [("rave", {}), ("bills_due", {})], fk=fk)
        assert prov.results[0][2]["answer"].startswith("Next: Thu 22.10, Artist One")
        assert ho.submitted == [("Which bills are due soon?", "now")]
    asyncio.run(main())
    assert fastlane.fallback_task("trip", {"name": "Islands"}) == "What's next for my trip: Islands?"
    assert fastlane.fallback_task("trip", {"name": "a/b"}) == "What's next for my next trip?"

def test_clean_and_fallback_tasks():
    assert clean("🚆 A → B · ✅ ok https://x.example/y ~5") == "A to B, ok about 5"
    for n in fastlane.NAMES:
        assert fastlane.fallback_task(n, {}).endswith("?") or "connections" in fastlane.fallback_task(n, {})
    assert "tomorrow" in fastlane.fallback_task("weather", {"when": "tomorrow"})


def test_declarations(tmp_path):
    cfg = mkcfg(tmp_path)
    p = make_provider(cfg, "x")
    decls = p.setup_message()["setup"]["tools"][0]["functionDeclarations"]
    assert [d["name"] for d in decls] == ["ask_assistant"] + [t["name"] for t in fastlane.TOOLS] + \
        ["identify_track", "decision_deck", "record_pick", "navigate", "open_on_phone", "end_call"]
    assert all(d["behavior"] == "NON_BLOCKING" and d["parameters"]["type"] == "OBJECT" for d in decls)
    assert ASK_TOOL["parameters"]["properties"]["action"]["type"] == "boolean"
    cfg.fast_lane = cfg.phone_open = False
    assert [t["name"] for t in make_provider(cfg, "x").tools] == ["ask_assistant", "identify_track", "decision_deck",
                                                                  "record_pick", "end_call"]
    cfg.track_id = False
    assert [t["name"] for t in make_provider(cfg, "x").tools] == ["ask_assistant", "decision_deck", "record_pick",
                                                                  "end_call"]
    cfg.decisions = False
    assert [t["name"] for t in make_provider(cfg, "x").tools] == ["ask_assistant", "end_call"]
    cfg.end_call = False
    assert [t["name"] for t in make_provider(cfg, "x").tools] == ["ask_assistant"]
    text = open(cfg.prompt_file).read()
    assert "quick tool" in text and "ask_assistant" in text


# --- session: dispatch, fallback, call log, recap -----------------------------------------------------------

class FakeHandoff:
    def __init__(self, route="channel", answer="Fake answer."):
        self.route, self.ans = route, answer
        self.pending, self.submitted = {}, []

    async def submit(self, task, urgency, deliver_late=None, owner=None):
        p = types.SimpleNamespace(id="v%04d" % len(self.submitted), route=self.route)
        self.submitted.append((task, urgency))
        if self.route != "failed":
            self.pending[p.id] = p
        return p

    async def wait(self, p, timeout):
        if self.ans is not None and timeout > 0:
            self.pending.pop(p.id, None)
            return self.ans
        return None

    def detach(self, owner):
        pass


async def _run_session(tmp, calls, fk=None, ho=None, **over):
    cfg = mkcfg(tmp, **over)
    prov, ho = StubProvider(), ho or FakeHandoff()
    log = CallLog(cfg, "tg-in", "owner")
    fast = FastLane(cfg, run=(fk or Fakes(all_outputs())).run, get_json=(fk or Fakes()).get, now=lambda: NOW)
    s = VoiceSession(cfg, prov, RecFE(), ho, log=log, fast=fast)
    await s.start()
    for i, (name, args) in enumerate(calls):
        await prov.q.put({"type": "tool_call", "id": "fc-%d" % i, "name": name, "args": args})
        assert await until(lambda: len(prov.results) > i)
    await s.close("hung up")
    return cfg, prov, ho, s


def recaps(cfg):
    return sorted(glob.glob(os.path.join(cfg.inbox_dir, "voice-recap-*.md")))


def test_fast_call_answers_locally_and_is_logged(tmp_path):
    async def main():
        cfg, prov, ho, s = await _run_session(tmp_path, [("downloads", {}), ("weather", {"when": "now"})])
        assert prov.results[0] == ("fc-0", "downloads", {"answer": "Nothing downloading."})
        assert prov.results[1][2]["answer"].startswith("In Worktown")
        assert ho.submitted == []                                      # main session not involved
        evs = load_calls(calls_dir(cfg))[0]
        tools = [e for e in evs if e["ev"] == "tool"]
        assert [t["lane"] for t in tools] == ["fast", "fast"] and all(isinstance(t["ms"], int) for t in tools)
        assert "(fast lane, " in format_call(evs)
        assert recaps(cfg) == []                                       # everything answered: no note
    asyncio.run(main())


def test_fast_failure_falls_back_to_ask_assistant(tmp_path):
    async def main():
        fk = Fakes(all_outputs(), fail={"tg-cmd"})
        cfg, prov, ho, s = await _run_session(tmp_path, [("server_space", {})], fk=fk)
        assert ho.submitted == [("How much space is left on my server?", "now")]
        assert prov.results[0] == ("fc-0", "server_space", {"answer": "Fake answer."})
        evs = load_calls(calls_dir(cfg))[0]
        tools = [e for e in evs if e["ev"] == "tool"]
        assert tools[0]["lane"] == "fast" and "exit" in tools[0]["error"]
        assert tools[1]["name"] == "ask_assistant" and tools[1]["fallback_from"] == "server_space"
        assert "(fallback for server_space)" in format_call(evs)
        assert recaps(cfg) == []
    asyncio.run(main())


def test_recap_written_for_open_failed_and_action_items(tmp_path):
    async def main():
        ho = FakeHandoff(answer=None)                                  # nothing answered in time
        cfg, prov, ho, s = await _run_session(tmp_path, [
            ("ask_assistant", {"task": "research flights to Milan", "urgency": "later"}),
            ("ask_assistant", {"task": "what is the capital of Peru", "urgency": "now", "action": False})], ho=ho)
        ho.pending.pop("v0001")                                        # second one answered after hang-up
        return cfg, s
    cfg, s = asyncio.run(main())
    notes = recaps(cfg)
    assert len(notes) == 1 and stat.S_IMODE(os.stat(notes[0]).st_mode) == 0o600
    body = open(notes[0]).read()
    assert "research flights to Milan" in body and "no answer yet" in body
    assert "capital of Peru" in body                                   # recap ran at close (grace 0): still pending
    assert "not a new request" in body and s.log.id in os.path.basename(notes[0])


def test_recap_grace_settles_late_answers_and_lists_actions(tmp_path):
    async def main():
        ho = FakeHandoff(answer=None)
        cfg, prov, ho, s = await _run_session(tmp_path, [("ask_assistant", {"task": "how tall is K2"})], ho=ho,
                                              recap_grace_s=0.2)
        ho.pending.clear()                                             # answered during the grace period
        await asyncio.sleep(0.4)
        assert recaps(cfg) == []
        ho2 = FakeHandoff(answer="Done, reminder set.")
        other = tmp_path / "b"
        other.mkdir()
        cfg2, *_ = await _run_session(other, [("ask_assistant", {"task": "remind me at six to call Mia", "action": True}),
            ("ask_assistant", {"task": "what's 2 plus 2", "action": False})], ho=ho2)
        body = open(recaps(cfg2)[0]).read()
        assert "action · answered in the call: \"Done, reminder set.\"" in body and "2 plus 2" not in body
    asyncio.run(main())


def test_recap_failed_route_and_rate_limit(tmp_path):
    async def main():
        cfg, prov, ho, s = await _run_session(tmp_path, [("ask_assistant", {"task": "check the cat feeder"}),
                                                         ("ask_assistant", {"task": "and the fountain"})],
                                              ho=FakeHandoff(route="failed"), max_asks_per_min=1)
        body = open(recaps(cfg)[0]).read()
        assert "failed: the assistant was unreachable" in body and "dropped: too many requests" in body
        assert len(recaps(cfg)) == 1
    asyncio.run(main())


def test_recap_helpers():
    assert recap.is_action("Kauf Milch") and recap.is_action("turn off the lights")
    assert not recap.is_action("when does the movie start") and recap.is_action("x", True)
    assert not recap.is_action("buy milk", False)


def test_gemini_quick_tool_roundtrip(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path))
        try:
            fk = Fakes(all_outputs())
            fast = FastLane(r.cfg, run=fk.run, get_json=fk.get, now=lambda: NOW)
            fe = RecFE()
            s = VoiceSession(r.cfg, make_provider(r.cfg, instructions(r.cfg)), fe, r.relay.handoff, fast=fast)
            await s.start()
            await s.text_in("quick downloads")
            assert await until(lambda: "Nothing downloading" in fe.said())
            fr = r.gem.log["tool_responses"][0]
            assert fr["name"] == "downloads" and fr["response"]["scheduling"] == "WHEN_IDLE"
            assert len(r.gem.log["setups"][0]["tools"][0]["functionDeclarations"]) == 1 + len(fastlane.TOOLS) + 6   # + identify_track + decision tools + phone open tools + end_call
            assert r.inbox.results == []                               # no hand-off to the session
            await s.close()
        finally:
            await r.close()
    asyncio.run(main())


def test_weather_geocode_falls_back_to_short_town_name(tmp_path):
    from voice_relay.fastlane import _town_names
    assert _town_names("Hometown am See") == ["Hometown am See", "Hometown"]
    assert _town_names("Biel/Bienne") == ["Biel/Bienne", "Biel"] and _town_names("Bern") == ["Bern"]
    fk = Fakes(all_outputs({"place": {"flag": "away", "town": "Longtown am See"}}))
    real = fk.get

    async def get(url, params):
        if "geocoding" in url and params["name"] == "Longtown am See":
            fk.http.append((url, dict(params)))
            return {"results": []}                      # the geocoder doesn't know the long official name
        return await real(url, params)
    ln = FastLane(mkcfg(tmp_path), run=fk.run, get_json=get, now=lambda: NOW)
    assert call(ln, "weather").startswith("In Longtown am See")
    assert [p["name"] for u, p in fk.http if "geocoding" in u] == ["Longtown am See", "Longtown"]
    wx = [p for u, p in fk.http if "forecast" in u][0]
    assert (wx["latitude"], wx["longitude"]) == (46.95, 7.45)


def test_parcels_speaks_cache_answer_and_falls_back(tmp_path):
    fk = Fakes(all_outputs())
    ln = lane(tmp_path, fk)
    out = call(ln, "parcels")
    assert out == ("One parcel waits at the Townstation station, pick it up by Thursday 29.10. Shopco: 3 items, "
                   "arriving between Saturday and Friday 6.11.")
    call(ln, "parcels")
    assert fk.ran.count(("parcels", "speak")) == 1                   # cached within the call
    assert set(fk.ran) <= READ_ONLY
    with pytest.raises(FastError):                                    # no cache yet (exit 4) → hand-off
        call(lane(tmp_path, Fakes(all_outputs(), fail=("parcels",))), "parcels")
    assert fastlane.fallback_task("parcels", {}) == "Where are my parcels? Anything waiting for pickup?"
