"""Decision call: decision-deck (TODO.md → spoken either/or prompts) + the relay's decision round.
Fakes only: FakeGemini, FakeCallClient, a fixture TODO.md. Nobody is rung, Gemini is never reached."""
import asyncio
import glob
import json
import os
import stat
import subprocess
import sys

from util import RecFE, Rig, until
from voice_relay import decisions
from voice_relay.audio import tone
from voice_relay.calllog import CallLog, format_call, load_calls, calls_dir
from voice_relay.fakes import FakeCallClient
from voice_relay.providers import make_provider
from voice_relay.server import CALL_EXIT, instructions
from voice_relay.session import VoiceSession
from voice_relay.tgcall import CallBridge

OWNER = 111222333
DECK_BIN = os.environ.get("DECISION_DECK_BIN") or os.path.expanduser("~/bin/decision-deck")

TODO = """# TODO
## Due
- **Something due** — 19:35 20.10 — not part of the deck?

## Waiting on you
- **📞 Voice test call** — first call worked; live test 19:00 20.10 (his pick)
- **👆 Reaction shortcuts** — live; your first try (😴 ✍ on any of my messages)
- **🎬 Franchise poll** — sent 10:10 20.10; answer → `media franchise request --picked "A | B"`
- **📞 Call account** — next: his 2nd-SIM number → `voice-tg login start` → he forwards code
- **✈️ Book AZ 153** — he chose tonight: reminder 19:30 20.10 (then ask for times → legs)
- **🛒 Amazon basket** — packs in the basket (€18.79 to home); he orders when more items pile up
- **📉 Market watchlist** — markets built; asked him for symbols to watch (+ target prices)
- **💰 Bank exports** — send a bank export to the vault bot → dry run → Import

- **Backup tweaks** — UI steps sent 18.10 09:57 → his Done (ab:tweaks-done)
- **Hallway remote** — re-pair dimmers or delete the automation? (re-asked 19.10 20:37 msg 1570, hwr:)
- **Island stays** — villas / resorts / mix? (re-asked msg 1568, sey:; free-cancel deadline 22.10)
  - internal sub-note, never spoken
- **Old idea** — parked since last week

## Ideas
- **Not waiting** — or this? no
"""


def run(coro):
    return asyncio.run(coro)


def deck_cmd(path):
    return "%s %s --json --todo %s" % (sys.executable, DECK_BIN, path)


def fixture(tmp_path, text=TODO):
    p = tmp_path / "TODO.md"
    p.write_text(text, encoding="utf-8")
    return str(p)


def deck_json(path, *extra):
    out = subprocess.run([sys.executable, DECK_BIN, "--todo", path] + list(extra), capture_output=True, text=True,
                         timeout=20, stdin=subprocess.DEVNULL)
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_deck_turns_waiting_items_into_short_spoken_prompts(tmp_path):
    d = json.loads(deck_json(fixture(tmp_path)))
    ids = [it["id"] for it in d["items"]]
    assert ids[0] == "island-stays"                          # a deadline goes first
    assert set(ids) == {"island-stays", "hallway-remote", "amazon-basket", "book-the-flight", "backup-tweaks",
                        "franchise-poll", "old-idea"}
    by = {it["id"]: it for it in d["items"]}
    assert by["island-stays"]["options"] == ["villas", "resorts", "mix"] and by["island-stays"]["due"] == "22.10"
    assert by["hallway-remote"]["say"] == "Hallway remote: re-pair dimmers, or delete the automation?"
    assert by["amazon-basket"]["kind"] == "order" and by["book-the-flight"]["kind"] == "done"
    assert by["franchise-poll"]["kind"] == "poll" and by["old-idea"]["kind"] == "keep"
    for it in d["items"]:                                         # one sentence, no identifiers or amounts
        s = it["say"]
        assert s.endswith("?") and len(s) <= 160 and ". " not in s, s
        assert not any(ch.isdigit() for ch in s + " ".join(it["options"])), s
        assert "€" not in s and "AZ" not in s and "hwr" not in s and "`" not in s, s
    why = {s["title"]: s["why"] for s in d["skipped"]}
    assert why["📞 Voice test call"] == "needs a live test call"
    assert why["📞 Call account"] == "needs a login, code or number"
    assert why["👆 Reaction shortcuts"] == "needs him to try it"
    assert "send" in why["📉 Market watchlist"] and "send" in why["💰 Bank exports"]
    assert "Something due" not in json.dumps(d) and "Not waiting" not in json.dumps(d)
    lst = subprocess.run([sys.executable, DECK_BIN, "--list", "--todo", fixture(tmp_path)], capture_output=True,
                         text=True, timeout=20, stdin=subprocess.DEVNULL).stdout
    assert "1. [island-stays]" in lst and "skipped: 📞 Call account" in lst
    assert len(json.loads(deck_json(fixture(tmp_path), "--max", "2"))["items"]) == 2


def test_relay_deck_keeps_only_id_question_options_and_filters_them(tmp_path):
    cfg = type("C", (), {"mask_amounts": True})()
    raw = json.dumps({"items": [{"id": "x", "title": "Book AZ 153", "status": "secret status",
                                 "say": "Pay the CHF 120 bill or wait?", "options": ["pay", "wait"]}, {"id": ""}]})
    deck = decisions.parse_deck(raw, cfg)
    assert len(deck["items"]) == 1
    g = decisions.deck_answer(deck)["items"][0]
    assert set(g) == {"id", "question", "options"} and "120" not in g["question"]
    assert decisions.is_trigger("decisions") and decisions.is_trigger(" Entscheidungen ")
    assert not decisions.is_trigger("the decisions about the parcel")
    assert CALL_EXIT["nothing"] == 6


def test_said_decisions_on_a_call_round_trip_and_note(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path), decision_deck_cmd=deck_cmd(fixture(tmp_path)))
        try:
            fe, log = RecFE(), CallLog(r.cfg, "tg-in", "owner")
            s = VoiceSession(r.cfg, make_provider(r.cfg, instructions(r.cfg), "ptt"), fe, r.relay.handoff, log=log)
            await s.start()
            decl = [d["name"] for d in r.gem.log["setups"][0]["tools"][0]["functionDeclarations"]]
            assert decl[0] == "ask_assistant" and "decision_deck" in decl and "record_pick" in decl
            assert "record_pick" in r.gem.log["setups"][0]["systemInstruction"]["parts"][0]["text"]
            await s.text_in("quick decision_deck")
            assert await until(lambda: r.gem.log["tool_responses"])
            resp = r.gem.log["tool_responses"][0]["response"]
            assert len(resp["items"]) == 7 and all(set(i) == {"id", "question", "options"} for i in resp["items"])
            assert "153" not in json.dumps(resp) and "€" not in json.dumps(resp)
            await s.text_in("pick hallway-remote delete the automation")
            await s.text_in("pick nonsense-item yes")
            await s.text_in("pick 1 a mix of both")
            assert await until(lambda: len(r.gem.log["tool_responses"]) >= 4)
            rs = [x["response"] for x in r.gem.log["tool_responses"][1:4]]
            assert rs[0]["recorded"] == {"item": "hallway-remote", "choice": "delete the automation"}
            assert rs[0]["next"]["id"] == "island-stays" and "✅" in rs[0]["answer"]
            assert "error" in rs[1]
            assert rs[2]["recorded"]["item"] == "island-stays"
            assert s.items == []                                  # picks are never hand-offs
            await s.close("hangup")
            notes = glob.glob(os.path.join(r.cfg.inbox_dir, "voice-decisions-*.md"))
            assert len(notes) == 1 and stat.S_IMODE(os.stat(notes[0]).st_mode) == 0o600
            text = open(notes[0], encoding="utf-8").read()
            assert 'hallway-remote · "Hallway remote"' in text and 'pick: "delete the automation"' in text
            assert 'pick: "a mix of both"' in text and "never an approval" in text and "✅ button per item" in text
            assert "not voice-recap-* or voice-decisions-*" in text and "Not answered in the call" in text
            assert not glob.glob(os.path.join(r.cfg.inbox_dir, "voice-recap-*.md"))
            out = format_call(load_calls(calls_dir(r.cfg), 1)[0])
            assert "decision deck loaded (7 items, via tool)" in out
            assert "tool record_pick (local) [choice=delete the automation, item=hallway-remote]" in out
        finally:
            await r.close()
    run(main())


def test_no_picks_no_note_and_a_broken_deck_says_so(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path), decision_deck_cmd="/nonexistent/decision-deck --json")
        try:
            s = VoiceSession(r.cfg, make_provider(r.cfg, instructions(r.cfg), "ptt"), RecFE(), r.relay.handoff)
            await s.start()
            await s.text_in("quick decision_deck")
            assert await until(lambda: r.gem.log["tool_responses"])
            assert "isn't available" in r.gem.log["tool_responses"][0]["response"]["error"]
            await s.close("hangup")
            assert not glob.glob(os.path.join(r.cfg.inbox_dir, "voice-decisions-*.md"))
        finally:
            await r.close()
    run(main())


def test_call_reason_decisions_opens_with_the_deck(tmp_path):
    async def main():
        r = await Rig.make(str(tmp_path), script=["pick island-stays villas"],
                           decision_deck_cmd=deck_cmd(fixture(tmp_path)))
        try:
            cl = FakeCallClient()
            b = CallBridge(r.cfg, cl, OWNER, r.relay.handoff, lambda: instructions(r.cfg), make_provider)
            await b.start()
            res = await b.call_owner("decisions")
            assert res["status"] == "answered" and res["decisions"] == 7 and cl.dialed == [OWNER]
            assert await until(lambda: r.gem.log["injects"])
            op = r.gem.log["injects"][0]
            assert op.startswith("[outbound call]") and "7 open decisions" in op and "record_pick" in op
            assert "- island-stays: Island stays: villas, resorts, or mix?" in op
            assert "153" not in op and "€" not in op and "Book AZ" not in op
            assert "you won't hear it" in op                       # prefetched during the pick-up hold
            assert await until(lambda: len(cl.sent[OWNER]) > 0, 4)   # the hold ended: the greeting plays
            await cl.speak(OWNER, tone(48000, 1200))
            assert await until(lambda: any((x["response"].get("recorded") or {}).get("item") == "island-stays"
                                           for x in r.gem.log["tool_responses"]), 5)
            await cl.end(OWNER)
            assert await until(lambda: glob.glob(os.path.join(r.cfg.inbox_dir, "voice-decisions-*.md")))
            out = format_call(load_calls(calls_dir(r.cfg), 1)[0])
            assert "reason: decisions" in out and "decision deck loaded (7 items, via opening)" in out
        finally:
            await r.close()
    run(main())


def test_call_reason_decisions_with_nothing_to_decide_never_rings(tmp_path):
    async def main():
        empty = fixture(tmp_path, "## Waiting on you\n- **📞 Call account** — his number → login code\n")
        r = await Rig.make(str(tmp_path), decision_deck_cmd=deck_cmd(empty))
        try:
            cl = FakeCallClient()
            b = CallBridge(r.cfg, cl, OWNER, r.relay.handoff, lambda: instructions(r.cfg), make_provider)
            await b.start()
            assert (await b.call_owner("decisions"))["status"] == "nothing"
            r.cfg.decision_deck_cmd = "/nonexistent/decision-deck --json"
            res = await b.call_owner("decisions")
            assert res["status"] == "failed" and "decision deck" in res["detail"]
            assert cl.dialed == [] and not r.gem.log["setups"]
        finally:
            await r.close()
    run(main())
