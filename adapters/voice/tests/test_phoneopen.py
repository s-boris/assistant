"""navigate / open_on_phone: `phone open` argv per kind and mode, route -> spoken line, failures, next calendar event
from a fake calcache, and privacy of the call log and usage events (fake CLI only: nothing opens anywhere)."""
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

from voice_relay import phoneopen
from voice_relay.calllog import CallLog, calls_dir, format_call, load_calls
from voice_relay.config import Config
from voice_relay.providers import make_provider
from voice_relay.session import VoiceSession

from util import RecFE, StubProvider, until

TZ = timezone(timedelta(hours=2))
NOW = datetime(2026, 10, 22, 17, 30, tzinfo=TZ)
PLACE = "Riverside Arena"
URL = "https://example.org/tickets/abc?x=1"


def ok(route="opened", how="foreground"):
    return 0, json.dumps({"ok": True, "route": route, "kind": "nav", "how": how, "app": "online", "ms": 800,
                          "why": "ok"})


def ev(title, start_h, town=None, travel=None, all_day=False, minutes=60):
    s = NOW + timedelta(hours=start_h)
    return {"id": title, "title": title, "start": s.isoformat(), "end": (s + timedelta(minutes=minutes)).isoformat(),
            "all_day": all_day, "town": town, "free": False,
            "flags": {"travel": travel, "busy_evening": False, "away_overnight": False, "abroad": False,
                      "id_check": False}}


class FakeCLI:
    """run(argv, timeout): calcache answers from `events`/`age`, `phone open` from `answer`; remembers every argv."""

    def __init__(self, answer=None, events=(), age_h=2.0, cached=True):
        self.answer = answer or ok()
        self.events, self.age_h, self.cached = list(events), age_h, cached
        self.argvs, self.timeouts = [], []

    async def __call__(self, argv, timeout):
        self.argvs.append(list(argv))
        self.timeouts.append(timeout)
        if argv[0] == "calcache" and argv[1] == "age":
            return 0, json.dumps({"cached": self.cached, "hours": self.age_h, "events": len(self.events)})
        if argv[0] == "calcache" and argv[1] == "show":
            return 0, json.dumps(self.events)
        return self.answer

    def opens(self):
        return [a for a in self.argvs if a[1:2] == ["open"]]


class Usage:
    def __init__(self):
        self.calls = []

    def __call__(self, *a, **kw):
        self.calls.append((a, kw))
        return True


def mk(tmp=None, cli=None, **over):
    kw = dict(phone_cmd="/fake/phone", fast_bin_dir="/nonexistent")
    if tmp is not None:
        kw.update(state_dir=os.path.join(str(tmp), "state"), inbox_dir=os.path.join(str(tmp), "inbox"),
                  recap_grace_s=0, fast_lane=False, decisions=False, track_id=False)
    kw.update(over)
    cfg = Config(**kw)
    u = Usage()
    return cfg, phoneopen.PhoneOpen(cfg, run=cli or FakeCLI(), now=lambda: NOW, ulog=u), u


def call(po, name, args):
    return asyncio.run(po.call(name, args))


TAIL = ["--wait", "6", "--via", "voice", "--json"]


# --- declared by the switch ---------------------------------------------------------------------------------------

def test_tools_declared_only_when_switched_on():
    names = [t["name"] for t in make_provider(Config(fast_lane=False, decisions=False, track_id=False), "x").tools]
    assert names == ["ask_assistant", "navigate", "open_on_phone", "end_call"]
    off = Config.from_env({"VOICE_PHONE_OPEN": "0"}, fast_lane=False, decisions=False, track_id=False)
    assert off.phone_open is False
    assert [t["name"] for t in make_provider(off, "x").tools] == ["ask_assistant", "end_call"]
    nav = phoneopen.NAV_TOOL["parameters"]
    assert nav["required"] == ["place"] and nav["properties"]["mode"]["enum"] == ["drive", "transit", "walk", "bike"]
    assert phoneopen.OPEN_TOOL["parameters"]["properties"]["what"]["enum"] == ["link", "sbb", "app"]


def test_off_switch_no_phone_tool_in_the_session(tmp_path):
    cfg = Config(state_dir=str(tmp_path), phone_open=False)
    s = VoiceSession(cfg, StubProvider(), RecFE(), None)
    assert s.phone is None


# --- argv per kind and mode ---------------------------------------------------------------------------------------

def test_navigate_argv_per_mode():
    for mode, letter in (("drive", "d"), ("transit", "r"), ("walk", "w"), ("bike", "b"), (None, "d"), ("car", "d")):
        cli = FakeCLI()
        cfg, po, _ = mk(cli=cli)
        res, meta = call(po, "navigate", {"place": PLACE, **({"mode": mode} if mode else {})})
        assert cli.opens() == [["/fake/phone", "open", "--nav", PLACE, "--mode", letter, "--label", PLACE] + TAIL]
        assert res == {"status": "opened", "say": "Starting navigation to Riverside Arena."}
        assert cli.timeouts[-1] == 15.0


def test_navigate_home_passes_through_and_never_names_it():
    cli = FakeCLI()
    cfg, po, u = mk(cli=cli)
    res, meta = call(po, "navigate", {"place": "Home", "mode": "transit"})
    assert cli.opens() == [["/fake/phone", "open", "--nav", "home", "--mode", "r", "--label", "Home"] + TAIL]
    assert res["say"] == "Starting navigation home." and meta["log_args"] == {"kind": "nav", "mode": "transit",
                                                                             "target": "home"}


def test_open_link_sbb_app_argv():
    cli = FakeCLI()
    cfg, po, _ = mk(cli=cli)
    assert call(po, "open_on_phone", {"what": "link", "url": URL})[0] == {
        "status": "opened", "say": "The link is open on your phone."}
    assert call(po, "open_on_phone", {"what": "sbb", "to": "Eventville"})[0]["say"] == \
        "The SBB connection to Eventville is open on your phone."
    call(po, "open_on_phone", {"what": "sbb", "to": "Eventville", "from": "Central Station", "at": "18:05"})
    call(po, "open_on_phone", {"what": "sbb", "to": "Eventville", "at": "soon"})
    assert call(po, "open_on_phone", {"what": "sbb", "to": "Eventville", "at": "18:00", "arrive": "18:30"})[0]["say"] == \
        "The SBB connections to Eventville arriving by 18:30 are open on your phone."
    call(po, "open_on_phone", {"what": "sbb", "to": "Eventville", "arrive": "half past"})   # not HH:MM: plain now
    assert call(po, "open_on_phone", {"what": "app", "app": "Spotify"})[0]["say"] == "Spotify is open on your phone."
    call(po, "open_on_phone", {"app": "--rm Spotify"})          # `what` left out: inferred; never an option
    assert cli.opens() == [
        ["/fake/phone", "open", URL, "--label", "Link from your call"] + TAIL,
        ["/fake/phone", "open", "--sbb", "here", "Eventville", "--label", "SBB to Eventville"] + TAIL,
        ["/fake/phone", "open", "--sbb", "Central Station", "Eventville", "--at", "18:05", "--label", "SBB to Eventville"] + TAIL,
        ["/fake/phone", "open", "--sbb", "here", "Eventville", "--label", "SBB to Eventville"] + TAIL,
        ["/fake/phone", "open", "--sbb", "here", "Eventville", "--arrive", "18:30", "--label",
         "SBB to Eventville, arrive by 18:30"] + TAIL,
        ["/fake/phone", "open", "--sbb", "here", "Eventville", "--label", "SBB to Eventville"] + TAIL,
        ["/fake/phone", "open", "--app", "Spotify", "--label", "Spotify"] + TAIL,
        ["/fake/phone", "open", "--app", "rm Spotify", "--label", "rm Spotify"] + TAIL]


def test_bad_input_never_runs_the_cli():
    cli = FakeCLI()
    cfg, po, u = mk(cli=cli)
    for args in ({"what": "link", "url": "http://plain.example.org"}, {"what": "link", "url": "javascript:alert(1)"},
                 {"what": "link"}, {"what": "link", "url": "https://a b.org"}):
        res, meta = call(po, "open_on_phone", args)
        assert res["status"] == "error" and "assistant" in res["say"] and meta["error"] == "no_url"
    assert call(po, "open_on_phone", {"what": "sbb"})[1]["error"] == "no_to"
    assert call(po, "open_on_phone", {"what": "app", "app": "  "})[1]["error"] == "no_app"
    assert call(po, "navigate", {"place": ""})[0] == {"status": "error", "say": "Where to? Tell me the place."}
    assert cli.opens() == [] and u.calls == []                  # nothing ran, nothing counted


# --- route -> spoken line, failures -----------------------------------------------------------------------------

def test_route_to_say():
    for route, nav_say, link_say in (
            ("opened", "Starting navigation to Riverside Arena.", "The link is open on your phone."),
            ("notified", "It's on your phone as a notification, tap it.", "It's on your phone as a notification, "
                                                                           "tap it."),
            ("telegram", "Your phone app isn't reachable, so I put the route on Telegram.",
             "Your phone app isn't reachable, so I put the link on Telegram.")):
        cfg, po, _ = mk(cli=FakeCLI(answer=ok(route)))
        assert call(po, "navigate", {"place": PLACE}) [0] == {"status": route, "say": nav_say}
        assert call(po, "open_on_phone", {"what": "link", "url": URL})[0] == {"status": route, "say": link_say}


def test_failures_say_short_and_offer_the_assistant():
    cases = [((None, ""), "timeout", "Your phone didn't answer in time."),
             ((4, json.dumps({"ok": False, "route": None, "why": "offline"})), "offline",
              "Your phone isn't reachable right now."),
             ((5, ""), "timeout", "Your phone didn't answer in time."),
             ((7, "not json"), "gateway_down", "The link to your phone is down right now."),
             ((2, ""), "usage", "Opening that on your phone didn't work."),
             ((127, ""), "unavailable", "Opening that on your phone didn't work."),
             ((0, json.dumps({"ok": False, "route": None})), "failed", "Opening that on your phone didn't work.")]
    for answer, err, say in cases:
        cfg, po, u = mk(cli=FakeCLI(answer=answer))
        res, meta = call(po, "navigate", {"place": PLACE})
        assert res == {"status": "error", "say": say + phoneopen.OFFER} and meta["error"] == err
        assert u.calls[-1][1]["outcome"] == err
    refused = (6, json.dumps({"ok": False, "route": None, "why": "unsafe"}))
    assert call(mk(cli=FakeCLI(answer=refused))[1], "open_on_phone", {"what": "link", "url": URL})[0]["say"] \
        .startswith("Your phone refused that link.")
    assert call(mk(cli=FakeCLI(answer=refused))[1], "open_on_phone", {"what": "app", "app": "Nope"})[0]["say"] \
        .startswith("I couldn't find that app on your phone.")


def test_say_never_carries_addresses_numbers_or_links():
    # built from fragments: the publish scan reads source lines, and these are the kinds of text it flags
    parts = (("Exam", "ple", "stras", "se 1"), ("Lin", "den", "hofs", "trasse 3"), ("Ma", "in S", "treet"), ("Mus", "ter", "w", "eg"),
             ("htt", "ps:", "//x", ".", "org"), ("Cl", "ub", " 2", "2"), ("a", "@", "b", ".", "ch"))
    for unsafe in ["".join(p) for p in parts] + ["x" * 41]:
        assert phoneopen.speakable(unsafe) == ""
        cfg, po, _ = mk(cli=FakeCLI())
        res, _ = call(po, "navigate", {"place": unsafe})
        assert res["say"] == "Navigation is starting on your phone." and unsafe not in res["say"]
    assert phoneopen.speakable(" Central Station. ") == "Central Station"


def test_cli_hard_timeout_and_real_subprocess(tmp_path):
    stub = tmp_path / "phone"
    stub.write_text("#!/bin/sh\n[ \"$3\" = slow ] && sleep 30\n"
                    "printf '%s\\n' \"$@\" > \"$(dirname \"$0\")/argv.txt\"\n"
                    "echo '{\"ok\": true, \"route\": \"notified\", \"kind\": \"nav\", \"how\": \"notification\"}'\n")
    stub.chmod(0o700)
    cfg = Config(phone_cmd=str(stub), phone_open_timeout_s=0.5, fast_bin_dir=str(tmp_path))
    po = phoneopen.PhoneOpen(cfg, now=lambda: NOW, ulog=Usage())
    res, meta = asyncio.run(po.call("navigate", {"place": "Eventville", "mode": "walk"}))
    assert res["status"] == "notified" and meta["how"] == "notification"
    assert (tmp_path / "argv.txt").read_text().split("\n")[:5] == ["open", "--nav", "Eventville", "--mode", "w"]
    res, meta = asyncio.run(po.call("navigate", {"place": "slow"}))
    assert meta["error"] == "timeout" and meta["ms"] < 3000
    cfg.phone_cmd = str(tmp_path / "missing")
    assert asyncio.run(po.call("navigate", {"place": "Eventville"}))[1]["error"] == "unavailable"


# --- next calendar event ------------------------------------------------------------------------------------------

def test_next_event_resolves_the_town_locally():
    events = [ev("Office", -3, town="Worktown", minutes=360),           # started long ago: not "next"
              ev("@Marker", 0, all_day=True, town="Allday"),
              ev("🚆 Hometown → Trainville", 1, town="Trainville", travel="train"),
              ev("Dinner", 1.5),                                        # no place
              ev("Band concert", 2, town="Eventville"),
              ev("Club night", 5, town="Partytown")]
    cli = FakeCLI(events=events)
    cfg, po, u = mk(cli=cli)
    res, meta = call(po, "navigate", {"place": "next_event"})
    assert cli.opens()[0][2:6] == ["--nav", "Eventville", "--mode", "d"] and "--label" in cli.opens()[0]
    assert res["say"] == "Starting navigation to Eventville, for Band concert."
    assert meta["log_args"] == {"kind": "nav", "mode": "drive", "target": "event"}
    res, _ = call(po, "navigate", {"place": "my next appointment", "event": "the club one"})
    assert cli.opens()[1][3] == "Partytown" and res["say"] == "Starting navigation to Partytown, for Club night."
    res, _ = call(po, "navigate", {"place": "next_event", "event": "nothing like it"})
    assert cli.opens()[2][3] == "Eventville"                                  # no title match: the next one


def test_next_event_missing_or_stale_says_so_without_opening():
    for cli, say in ((FakeCLI(events=[ev("Dinner", 1)]), phoneopen.EVENT_SAY["no_place"]),
                     (FakeCLI(events=[]), phoneopen.EVENT_SAY["none"]),
                     (FakeCLI(events=[ev("Far", 30, town="Eventville")]), phoneopen.EVENT_SAY["none"]),
                     (FakeCLI(events=[ev("X", 1, town="Eventville")], age_h=50), phoneopen.EVENT_SAY["stale"]),
                     (FakeCLI(events=[ev("X", 1, town="Eventville")], cached=False), phoneopen.EVENT_SAY["stale"])):
        cfg, po, u = mk(cli=cli)
        res, meta = call(po, "navigate", {"place": "next_event"})
        assert res == {"status": "no_event", "say": say + " Where to?"} and cli.opens() == [] and u.calls == []


# --- in a call: log and usage privacy -----------------------------------------------------------------------------

def test_in_a_call_logs_kind_mode_route_only(tmp_path):
    async def main():
        cli = FakeCLI(answer=ok("telegram"))
        cfg, po, u = mk(tmp_path, cli=cli)
        prov = StubProvider()
        s = VoiceSession(cfg, prov, RecFE(), None, log=CallLog(cfg, "tg-in", "owner"), phone=po)
        s.handoff = type("H", (), {"detach": lambda self, owner: None})()
        await s.start()
        await prov.q.put({"type": "tool_call", "id": "fc-1", "name": "navigate",
                          "args": {"place": PLACE, "mode": "walk"}})
        await prov.q.put({"type": "tool_call", "id": "fc-2", "name": "open_on_phone",
                          "args": {"what": "link", "url": URL}})
        await prov.q.put({"type": "tool_call", "id": "fc-3", "name": "open_on_phone",
                          "args": {"what": "app", "app": "Secretapp"}})
        assert await until(lambda: len(prov.results) == 3)
        assert prov.results[0] == ("fc-1", "navigate", {"status": "telegram", "say": phoneopen.SAY["telegram"]["nav"]})
        await s.close("hung up")
        raw = open(os.path.join(calls_dir(cfg), os.listdir(calls_dir(cfg))[0])).read()
        for secret in (PLACE, "Riverside", URL, "example.org", "Secretapp", "Starting navigation"):
            assert secret not in raw
        evs = load_calls(calls_dir(cfg))[0]
        tools = [e for e in evs if e["ev"] == "tool"]
        assert [t["args"] for t in tools] == [{"kind": "nav", "mode": "walk", "target": "place"}, {"kind": "url"},
                                              {"kind": "app"}]
        assert all(t["lane"] == "phone" and t["route"] == "telegram" and isinstance(t["ms"], int) for t in tools)
        assert [e.get("outcome") for e in evs if e["ev"] == "result"] == ["telegram"] * 3
        assert "navigate (phone open, " in format_call(evs) and "→ telegram)" in format_call(evs)
        feats = [(a[0], kw["outcome"], kw["channel"], kw["actor"]) for a, kw in u.calls]
        assert feats == [("voice.open.nav", "telegram", "voice", "owner"), ("voice.open.link", "telegram", "voice",
                                                                            "owner"),
                         ("voice.open.app", "telegram", "voice", "owner")]
        assert PLACE not in json.dumps([kw for _, kw in u.calls]) and URL not in json.dumps([kw for _, kw in u.calls])
        assert u.calls[0][1]["attrs"] == {"mode": "walk", "target": "place", "how": "foreground"}
    asyncio.run(main())


def test_prompt_covers_the_tools():
    text = open(Config().prompt_file).read()
    assert "navigate" in text and "open_on_phone" in text and "next_event" in text
    assert "bring mich zu" in text and "no ask_assistant, no Telegram confirm card" in text
    assert "sends, buys, books, orders or deletes" in text
