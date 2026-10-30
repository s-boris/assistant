"""Retired share intents ('summary', 'dance'): the features behind them were removed. The app still offers them until
its next build, so the protocol keeps the values; the gateway hands the share over like a plain one and says the
option is gone (the hint goes into the session hand-off, the ~/inbox note and, in hub mode, the phone.share event)."""
import json
import os

from phonegw import m2

SCHEMA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "protocol", "schema",
                      "c2s.share.json")


def test_retired_intents_fall_back_to_a_plain_share():
    for intent, label in (("summary", "summarize"), ("dance", "practice loops")):
        hint = m2.NOTE_HINT[intent]
        assert hint.startswith("see what he wants") and f"'{label}' option is retired" in hint
        assert "skill" not in hint and "link-summary" not in hint and "danceloop" not in hint


def test_the_protocol_still_accepts_them():
    text = open(SCHEMA, encoding="utf-8").read()
    assert '"summary"' in text and '"dance"' in text, "an app built before the removal still sends them"
    json.loads(text)


def test_other_intents_unchanged():
    assert m2.NOTE_HINT["trackid"] == "name the track (skill trackid)"
    assert "vision-actions" in m2.NOTE_HINT["what"]
