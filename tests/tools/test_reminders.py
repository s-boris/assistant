"""Reminders tools against a temp remind store (the repo's scripts/remind, clock fixed at 2026-10-20 12:00)."""

from __future__ import annotations

import json
import re

from tools import cli, gateway


def call(name, **args):
    return gateway.call(name, args)


def test_when(remind_env):
    r = call("reminders.when", phrase="tomorrow 9")
    assert r.ok and r.data == {"kind": "at", "at": "2026-10-21 09:00", "why": "tomorrow"}
    assert call("reminders.when", phrase="tomorrow 9", format="detailed").data["form"] == "day"
    p = call("reminders.when", phrase="when I get home")
    assert p.data["kind"] == "on" and p.data["on"] == "home"
    bad = call("reminders.when", phrase="blorf")
    assert bad.code == "invalid_args" and "blorf" in bad.error["message"]


def test_add_list_snooze_move_cancel(remind_env):
    a = call("reminders.add", text="call the garage", owner=True, when="2026-10-21 09:00", tag="t:1", via="telegram")
    assert a.ok, a.error
    rid = a.data["id"]
    assert re.fullmatch(r"[0-9a-f]{8}", rid)
    assert {k: a.data[k] for k in ("kind", "owner", "at")} == {"kind": "timed", "owner": True, "at": "2026-10-21 09:00"}
    assert "message" not in a.data and rid in a.text  # concise
    s = call("reminders.add", text="water the plants", owner=False, when="2026-10-23 18:00", every="weekly")
    assert s.ok and s.data["kind"] == "series" and s.data["owner"] is False and s.data["every"]
    pl = call("reminders.add", text="take the slip", owner=True, when="when I get home")
    assert pl.ok and pl.data["kind"] == "place" and pl.data["on"] == "home"
    lv = call("reminders.add", text="bins out", owner=True, on="leave", every_time=True)
    assert lv.ok and lv.data["kind"] == "place"

    ls = call("reminders.list")
    assert ls.data["count"] == 4 and ls.data["now"] == "2026-10-20 12:00"
    assert [i["id"] for i in ls.data["items"]][0] == rid
    assert call("reminders.list", mine=True).data["count"] == 3  # his only: not my series
    q = call("reminders.list", query="GARAGE")
    assert q.data["count"] == 1 and q.data["items"][0]["text"] == "call the garage"
    assert call("reminders.list", limit=1).data["count"] == 4 and "more" in call("reminders.list", limit=1).text

    sn = call("reminders.snooze", id=rid, when="+1h")
    assert sn.ok and sn.data["at"] == "2026-10-20 13:00", sn
    mv = call("reminders.move", id=rid, when="2026-10-22 18:00")
    assert mv.ok and mv.data["at"] == "2026-10-22 18:00" and mv.data["message"].startswith("moved to")
    pr = call("reminders.press", data=f"rm:s1h:{rid}")
    assert pr.ok and pr.data["reply"].startswith("⏰") and "garage" not in pr.data["reply"]

    assert call("reminders.cancel", tag="t:1").data["cancelled"] == 1
    assert call("reminders.cancel", match="plants").data["cancelled"] == 1
    gone = call("reminders.cancel", id=rid)
    assert gone.code == "not_found"
    nf = call("reminders.snooze", id="0badc0de", when="+1h")
    assert nf.code == "not_found" and nf.exit_code == 3


def test_add_refusals(remind_env):
    both = call("reminders.add", text="x", owner=True, when="tomorrow 9", on="home")
    assert both.code == "invalid_args" and "exactly one" in both.error["message"]
    assert call("reminders.add", text="x", owner=False, on="home").code == "invalid_args"
    assert call("reminders.add", text="x", owner=False, when="tomorrow 9", call=True).code == "invalid_args"
    assert call("reminders.add", text="x", owner=True, when="tomorrow 9", until="2026-12-01").code == "invalid_args"
    assert call("reminders.add", text="x", when="tomorrow 9").code == "invalid_args"  # owner is required
    bad_time = call("reminders.add", text="x", owner=True, when="blorf o'clock")
    assert bad_time.code == "invalid_args" and "reminders_when" in bad_time.error["hint"]
    assert call("reminders.snooze", id="XYZ", when="+1h").code == "invalid_args"
    assert call("reminders.press", data="rm:zz:12345678").code == "invalid_args"
    assert call("reminders.cancel").code == "invalid_args"
    assert call("reminders.list").data["count"] == 0


def test_cli_matches_gateway(remind_env, capsys):
    rc = cli.main(["call", "reminders.when", '{"phrase": "tomorrow 9"}', "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["ok"] and out["data"] == call("reminders.when", phrase="tomorrow 9").data
    rc = cli.main(["call", "reminders_add", "text=pay rent", "owner=true", "when=2026-10-25 10:00"])
    assert rc == 0 and capsys.readouterr().out.strip().endswith("reminder for Sun 2026-10-25 10:00")
    rc = cli.main(["call", "reminders.cancel", "{}"])
    err = capsys.readouterr().err
    assert rc == 2 and err.startswith("invalid_args:")
    assert cli.main(["ls", "reminders"]) == 0 and "reminders.add" in capsys.readouterr().out
    assert cli.main(["show", "reminders.add", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["mcp_name"] == "reminders_add"
    assert cli.main(["ls", "--bindings"]) == 0 and "remind.press" in capsys.readouterr().out
    assert cli.main(["stats", "--json"]) == 0 and "reminders.add" in json.loads(capsys.readouterr().out)["tools"]
