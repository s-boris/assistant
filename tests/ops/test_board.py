"""scripts/board: parse/render round trip, sync of hand edits, the probes (incl. the remote ones and his-answer), the
evaluator (close, move, notes), TODO.md writes (snapshots, the sanity refusal, stale copies), render / diff / status,
add --replace-title, asked marks (+ the hooks' asked.jsonl).
A JSON store (BOARD_STORE_FILE) stands in for the hub; tests/hubstore/test_board_store.py covers the hub routes."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "board"

TODO = """# TODO
Rules header line.

## Due
- **Alpha check** — due 16:00 29.10
  - sub one
- **Beta land** — branch feat-x waits
  - done-when: file-exists {ok}

## In progress
<!-- tasks:begin -->
- T1 running
<!-- tasks:end -->
- **Gamma** — LIVE: something
  - note

## Waiting on you
- **Install app 1.2.3** — on his phone

## Ideas
- **Idea one** — later
"""


@pytest.fixture
def b(tmp_path, monkeypatch):
    env = {
        "BOARD_STORE_FILE": "store.json", "BOARD_TODO": "TODO.md", "BOARD_STATE": "state",
        "BOARD_PROACTIVE": "PROACTIVE.md", "BOARD_INBOX": "inbox",
        "BOARD_DEPLOYS": "deploys.jsonl", "BOARD_REPO": "repo", "BOARD_BIN": "bin", "BOARD_CMD_ALLOW": "allow",
        "BOARD_PHONEGW_DB": "phonegw.db", "BOARD_TG_JOURNAL": "journal.jsonl",
    }  # fmt: skip
    for k, v in env.items():
        monkeypatch.setenv(k, str(tmp_path / v))
    monkeypatch.setenv("BOARD_TZ", "UTC")
    monkeypatch.setenv("BOARD_NOW", "2026-10-29 12:00")
    (tmp_path / "inbox").mkdir()
    (tmp_path / "bin").mkdir()
    (tmp_path / "ok").write_text("x")
    (tmp_path / "TODO.md").write_text(TODO.format(ok=tmp_path / "ok"))
    ld = importlib.machinery.SourceFileLoader("board_mod", str(SCRIPT))
    spec = importlib.util.spec_from_loader("board_mod", ld)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    ld.exec_module(mod)
    mod.T = tmp_path
    return mod


def run(b, *args) -> int:
    try:
        return b.main(list(args))
    except SystemExit as e:
        return int(e.code or 0)


def items(b, all_rows=True):
    return {it["title"]: it for it in b.get_store().load(all_rows=all_rows)[0] if it["kind"] == "item"}


def fill(b):
    """TODO.md -> store through the tick's sync; TODO.md and the probes untouched."""
    store = b.get_store()
    b.commit(store, *b.sync((b.T / "TODO.md").read_text(), *store.load(), b.now_ts()))


def tool(b, name, body):
    p = b.T / "bin" / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(0o755)


def test_sync_render_round_trip_and_flags(b, capsys):
    fill(b)
    text = (b.T / "TODO.md").read_text()
    out = b.render(*b.get_store().load(), b.now_ts())
    assert b.normalized(out) == b.normalized(text) and out != text
    assert "- **Alpha check** — due 16:00 29.10 [board: due today, unverified]\n" in out
    assert "<!-- tasks:begin -->\n- T1 running\n<!-- tasks:end -->" in out
    assert "[board:" not in out.split("## Ideas")[1]  # unverified only in Due / In progress / Waiting
    assert [e["line"] for e in b.parse(out)[2]] == [e["line"] for e in b.parse(text)[2]]  # flags stripped on sync
    it = items(b)
    assert it["Beta land"]["probes"][0]["src"] == "done-when"
    assert it["Install app 1.2.3"]["probes"][0]["spec"] == "app-installed 1.2.3"  # implicit rule
    capsys.readouterr()
    assert run(b, "render") == 0 and capsys.readouterr().out == out
    assert run(b, "diff") == 0
    (b.T / "TODO.md").write_text(text.replace("later", "later, edited"))
    assert run(b, "diff") == 1 and "later, edited" in capsys.readouterr().out


def test_due_parsing_and_flags(b):
    now = b.now_ts()
    assert b.hm(b.due_of("**A** — due 16:00 29.10", now)) == "16:00 29.10"
    assert b.hm(b.due_of("**A** — 10.11 10:00 — rest", now)) == "10:00 10.11"
    assert b.due_of("**A** — no date", now) is None
    assert b.hm(b.due_of("**A** — due 09:00 05.01", now)) == "09:00 05.01"
    row = {"kind": "item", "section": "Due", "probes": [], "due_ts": now - 60}
    assert b.flags(row, now) == ["overdue", "unverified"]
    assert b.set_due_text("**A** — x", b.parse_when("18:00 03.11", now)) == "**A** — due 18:00 03.11 — x"
    assert b.set_due_text("**A** — due 16:00 29.10 y", b.parse_when("03.11 18:00", now)) == "**A** — due 18:00 03.11 y"


def test_eval_closes_moves_and_reports(b):
    fill(b)
    assert (
        run(
            b,
            "add",
            "**Wait thing** — asked",
            "--section",
            "Waiting",
            "--when",
            f"file-exists {b.T / 'go'}",
            "--then",
            "move:Due",
        )
        == 0
    )
    (b.T / "go").write_text("")
    assert run(b, "eval") == 0
    it = items(b)
    assert it["Beta land"]["state"] == "done" and it["Beta land"]["closed_by"].startswith("probe: file-exists")
    assert it["Wait thing"]["section"] == "Due" and it["Wait thing"]["probes"][0]["state"] == "applied"
    assert "Beta land: done, closed by its probe" in (b.T / "PROACTIVE.md").read_text()
    note = next((b.T / "inbox").glob("board-*.md")).read_text()
    assert "closed: Beta land" in note and "moved: Wait thing: Waiting on you -> Due" in note
    todo = (b.T / "TODO.md").read_text()
    assert "Beta land" not in todo and "**Wait thing**" in todo.split("## In progress")[0]  # moved to Due


def test_todo_writes_hand_edits_stale_copies_and_snapshots(b, capsys):
    fill(b)
    assert run(b, "eval") == 0
    text = (b.T / "TODO.md").read_text()
    assert "Beta land" not in text and "[board: due today, unverified]" in text
    assert len(list((b.T / "state" / "snapshots").iterdir())) == 1
    capsys.readouterr()
    assert run(b, "status") == 0 and "1 snapshots" in capsys.readouterr().out
    edited = text.replace("- **Gamma** — LIVE: something", "- **Gamma** — LIVE: changed by hand")
    edited = edited.replace("- **Idea one** — later\n", "- **Idea two** — new\n")
    edited = edited.replace("## In progress\n", "## In progress\n- **Beta land** — branch feat-x waits\n")
    (b.T / "TODO.md").write_text(edited)
    assert run(b, "eval") == 0
    it = items(b)
    assert it["Gamma"]["line"].endswith("changed by hand") and it["Idea two"]["state"] == "open"
    assert it["Idea one"]["state"] == "done" and it["Idea one"]["closed_by"] == "todo.md"
    assert it["Beta land"]["state"] == "done"  # a stale copy never revives what a probe closed
    final = (b.T / "TODO.md").read_text()
    assert "Idea two" in final and "Beta land" not in final and "changed by hand" in final


def test_refuses_a_render_that_drops_half(b):
    big = "# TODO\n\n## Ideas\n" + "".join(f"- **Item {i}** — x\n" for i in range(12))
    (b.T / "TODO.md").write_text(big)

    class Empty:
        def load(self, all_rows=False):
            return [], {"sections": '["Ideas"]', "header": "# TODO\n"}

        def apply(self, ups, meta=None, log=None):
            return {"versions": {}}

    with pytest.raises(b.StoreConflict):
        b.tick(Empty())
    assert (b.T / "TODO.md").read_text() == big


def test_cli_add_done_drop_reopen(b):
    fill(b)
    assert run(b, "add", "**New thing** — status", "--due", "18:00 30.10", "--note", "detail") == 0
    it = items(b)["New thing"]
    assert it["section"] == "In progress" and it["notes"] == ["  - detail"] and b.hm(it["due_ts"]) == "18:00 30.10"
    assert run(b, "done", it["id"], "--why", "finished") == 0
    assert (
        items(b)["New thing"]["state"] == "done" and "New thing: done (finished)" in (b.T / "PROACTIVE.md").read_text()
    )
    assert run(b, "reopen", it["id"]) == 0 and items(b)["New thing"]["state"] == "open"
    assert run(b, "drop", it["id"]) == 2  # --why required
    assert run(b, "drop", it["id"], "--why", "declined") == 0 and items(b)["New thing"]["state"] == "dropped"
    assert run(b, "add", "**Bad**", "--when", "nosuch probe") == 2
    assert run(b, "probe", items(b)["Gamma"]["id"], "add", f"file-gone {b.T / 'none'}") == 0
    assert items(b)["Gamma"]["state"] == "done"  # a new probe is checked at once


def test_probes_land_release_files_cmd(b):
    rows = [{"op": "land", "ok": True, "branch": "feat-x", "sha": "abc1234def5678"},
            {"op": "deploy", "target": "hub", "ok": True, "sha": "fedcba9876543210"}]  # fmt: skip
    (b.T / "deploys.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert b.check_probe("landed feat-x")[0] == "pass" and b.check_probe("landed abc1234")[0] == "pass"
    assert b.check_probe("landed nope")[0] == "fail"
    assert b.check_probe("hub-release fedcba9")[0] == "pass"
    assert (
        b.check_probe(f"file-exists {b.T / 'ok'}")[0] == "pass"
        and b.check_probe(f"file-gone {b.T / 'ok'}")[0] == "fail"
    )
    assert b.check_probe(f"cmd-ok test -e {b.T / 'ok'}")[0] == "pass"
    assert b.check_probe(f"cmd-ok rm -f {b.T / 'ok'}")[0] == "unknown" and (b.T / "ok").exists()
    assert b.check_probe("cmd-ok tg hello")[0] == "unknown"
    tool(b, "mycheck", "exit 1\n")
    assert b.check_probe("cmd-ok mycheck")[0] == "unknown"
    (b.T / "allow").write_text("# vetted readers\nmycheck\n")
    assert b.check_probe("cmd-ok mycheck") == ("fail", "exit 1")
    assert b.check_probe("nosuch x")[0] == "unknown" and b.check_probe("landed")[0] == "unknown"
    b.main(["poke", "landed", "feat-y", "--hash", "1111111aaaa", "--also", "2222222bbbb"]) if False else None
    b.jsonstate.save(b.ALIASES, {"2222222bbbb": "1111111aaaa"})
    assert b.check_probe("landed 2222222bbbb")[0] == "pass"
    assert b.ref_hit("landed 2222222bbbb", ["2222222"]) and not b.ref_hit("landed feat-x", ["feat-y"])


def test_remote_probes(b):
    tool(b, "ha", 'echo \'{"state": "on"}\'\n')
    assert b.check_probe("ha-state light.x on")[0] == "pass" and b.check_probe("ha-state light.x off")[0] == "fail"
    tool(b, "paperless", "echo '{\"count\": 2}'\n")
    assert b.check_probe("paperless-has Lohnausweis") == ("pass", "2 document(s)")
    tool(b, "calcache", 'echo \'[{"start": "2026-10-31T10:00", "title": "Shooting course"}]\'\n')
    assert b.check_probe("calendar-has shooting 31.10")[0] == "pass"
    assert b.check_probe("calendar-has dentist 31.10")[0] == "fail"
    con = sqlite3.connect(b.T / "phonegw.db")
    con.execute("CREATE TABLE devices(id TEXT, app_code INT, status TEXT)")
    con.execute("CREATE TABLE state(device_id TEXT, k TEXT, v TEXT)")
    con.execute("INSERT INTO devices VALUES ('d1', 12, 'active')")
    con.execute("INSERT INTO state VALUES ('_gw', 'app_latest', '{\"name\": \"1.2.3\", \"code\": 12}')")
    con.commit()
    con.close()
    assert b.check_probe("app-installed 1.2.3")[0] == "pass" and b.check_probe("app-installed 13")[0] == "fail"


def test_his_answer_probe(b, monkeypatch):
    pend = ("unknown", "not asked yet (link: board probe ID add his-answer msg:N)")
    assert b.check_probe("his-answer pending") == pend and b.check_probe("his-answer pending:tax") == pend
    assert b.check_probe("his-answer msg:400") == ("unknown", "no Telegram inbound journal")  # missing journal
    # synthetic journal rows only (never real ones); content must never reach a result
    rows = [
        {"id": 1, "kind": "message", "owner": True, "message_id": "501", "ts": "1793271600", "content": "SECRETTEXT",
         "meta": {"message_id": "501", "reply_to_message_id": "400", "message_text": "SECRETTEXT"}},
        {"id": 2, "kind": "button", "owner": True, "message_id": "410", "ts": "1793271600",
         "meta": {"message_id": "410", "callback_data": "wn:yes:7", "button_text": "SECRETTEXT"}},
        {"id": 3, "kind": "poll_answer", "owner": True, "message_id": "420", "meta": {"message_id": "420"}},
        {"id": 4, "kind": "message", "owner": False, "meta": {"reply_to_message_id": "430"}},
        {"id": 5, "kind": "button", "owner": False, "meta": {"message_id": "440", "callback_data": "xx:1"}},
    ]  # fmt: skip
    (b.T / "journal.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows) + "not json\n")
    res = {r: b.check_probe("his-answer " + r) for r in
           ("msg:400", "msg:410", "msg:420", "msg:430", "msg:440", "msg:501", "button:wn:yes", "button:xx",
            "button:wn:no", "msg:abc", "nope:1")}  # fmt: skip
    assert [res[r][0] for r in ("msg:400", "msg:410", "msg:420", "button:wn:yes")] == ["pass"] * 4
    assert res["msg:400"][1].startswith("his reply") and res["msg:420"][1] == "his poll answer"
    assert [res[r][0] for r in ("msg:430", "msg:440", "msg:501", "button:xx", "button:wn:no")] == ["fail"] * 5
    assert res["msg:abc"][0] == res["nope:1"][0] == "unknown" and b.check_probe("his-answer")[0] == "unknown"
    assert "SECRETTEXT" not in repr(res) and "SECRETTEXT" not in repr(b._JOURNAL)
    monkeypatch.setattr(b, "_hub", lambda m, p: {"rows": [{"ref": "tax-ok", "status": "approved", "decided_ts": 0}]})
    assert b.check_probe("his-answer approval:tax-ok")[0] == "pass"
    assert b.check_probe("his-answer approval:other")[0] == "fail"
    # on an item: msg:400 closes it, pending keeps it open (verified, no "unverified" flag)
    fill(b)
    assert run(b, "add", "**Answered**", "--section", "Waiting", "--when", "his-answer msg:400") == 0
    assert run(b, "add", "**Not asked**", "--section", "Waiting", "--when", "his-answer pending:x") == 0
    assert run(b, "eval") == 0
    its = items(b)
    assert its["Answered"]["state"] == "done" and its["Not asked"]["state"] == "open"
    assert "unverified" not in b.flags(its["Not asked"], b.now_ts())


def test_add_replace_title(b, capsys):
    fill(b)
    rx = r"install (the )?app v?\d"
    assert run(b, "add", "**Install app 0.9** — elsewhere") == 0  # In progress: another section, kept
    assert run(b, "add", "**Bad rx**", "--replace-title", "(") == 2
    capsys.readouterr()
    assert run(b, "add", "**📱 Install app Claude** — tap Install", "--section", "Waiting on you",
               "--when", "app-installed 13", "--replace-title", rx) == 0  # fmt: skip
    its = items(b)
    new, old = its["📱 Install app Claude"], its["Install app 1.2.3"]
    assert new["state"] == "open" and new["probes"][0]["spec"] == "app-installed 13"
    assert old["state"] == "dropped" and old["closed_by"] == f"replaced by {new['id']}"
    assert its["Install app 0.9"]["state"] == "open" and its["Idea one"]["state"] == "open"
    assert f"(replaced {old['id']})" in capsys.readouterr().out
    log = json.loads((b.T / "store.json").read_text())["log"]
    assert {"item": old["id"], "op": "drop"}.items() <= log[-1].items() and log[-2]["op"] == "add"
    assert "Install app 1.2.3" not in (b.T / "TODO.md").read_text()


def asked_of(b):
    return json.loads(b.get_store().load()[1].get("asked") or "{}")


def test_asked_marks_queue_feed_and_prune(b, capsys):
    fill(b)
    wid, now = items(b)["Install app 1.2.3"]["id"], b.now_ts()
    capsys.readouterr()
    assert run(b, "asked", "INSTALL APP") == 0 and capsys.readouterr().out.strip() == "marked 1"
    assert asked_of(b) == {wid: now}
    assert run(b, "asked", "Gamma") == 0 and capsys.readouterr().out.strip() == "marked 0"  # not Waiting
    assert run(b, "asked", wid, "--at", "2026-10-20") == 0 and asked_of(b) == {wid: now}  # an older date: kept
    assert run(b, "asked", wid, "--at", "2026/10/20") == 2
    assert run(b, "add", "**Second wait**", "--section", "Waiting") == 0
    sid = items(b)["Second wait"]["id"]
    assert run(b, "asked", sid, "--at", "2026-10-20") == 0
    assert asked_of(b)[sid] == b.parse_when("2026-10-20 12:00", now)
    # the hooks' queue: drained on the next run, garbage lines skipped, newest ask wins
    q = b.T / "state" / "asked.jsonl"
    q.write_text(json.dumps({"ts": now + 60, "text": "install app 1.2"}) + "\nnot json\n"
                 + json.dumps({"ts": now - 30 * 86400, "text": "second WAIT"}) + "\n")  # fmt: skip
    assert run(b, "eval") == 0
    assert asked_of(b)[wid] == now + 60 and asked_of(b)[sid] == b.parse_when("2026-10-20 12:00", now)
    assert not q.exists() and not (b.T / "state" / "asked.jsonl.work").exists()
    feed = {r["id"]: r for r in json.loads((b.T / "state" / "board.json").read_text())["items"]}
    assert feed[wid]["asked"] == now + 60 and feed[wid]["created"] == now and feed[sid]["section"] == "Waiting on you"
    assert feed[items(b)["Gamma"]["id"]]["asked"] is None
    # closed items drop out of the map
    assert run(b, "done", wid) == 0 and run(b, "eval") == 0 and set(asked_of(b)) == {sid}
