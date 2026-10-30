"""memrows: round trip byte for byte, hand-edit import, ledger fields, the write cycle."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from memrows import cli
from memrows import store as S

REPO = Path(__file__).resolve().parents[2]

DOC = (
    "# Facts: example\n"
    "Intro paragraph for the example file.\n"
    "\n"
    "## Server\n"
    "- Box runs 4 disks — owner, 01.10\n"
    "- Backups run nightly — live, 02.10\n"
    "\n"
    "## Notes & misc (no amounts)\n"
    "- Plain fact without a source\n"
    "\n"
    "- Second group fact — example agent, 03.10\n"
    "\n"
    "## Done / declined ledger\n"
    "Newest first, one line each: `- DD.MM verb: thing (details)`.\n"
    "- 03.10 declined: thing one (details here; more)\n"
    "- 02.10 done: thing two — [[memo]]\n"
    "- 01.10 decided: thing three\n"
)


@pytest.fixture
def home(home_dir: Path) -> Path:
    (home_dir / "workspace" / "FACTS.md").write_text(DOC, encoding="utf-8")
    return home_dir


EDGE = (
    "# Title\n"
    "- a dash line in the preamble stays layout\n"
    "\n"
    "\n"
    "## Section A   \n"
    "- trailing spaces stay   \n"
    "-  two spaces after the dash\n"
    "- unicode ✅ – “quotes” → arrows\n"
    "- duplicate line\n"
    "- duplicate line\n"
    "  - an indented sub-bullet\n"
    "### a subheading\n"
    "text line inside a section\n"
    "- carriage return\r\n"
    "\n"
    "## Empty section\n"
    "\n"
    "## Ledger of done / declined things\n"
    "- 01.10.2026 kept: year form\n"
    "- 02.10 his call: board-lint form\n"
    "- 03.10 paused: not a ledger verb\n"
    "- 04.10 off: thing four; with a semicolon\n"
    "- 05.10. done: dotted date\n"
)


def _store(home: Path) -> tuple[S.Paths, object]:
    p = S.resolve_paths(None, None)  # the defaults under the temp HOME
    assert home in p.db.parents
    return p, S.connect(p.db)


def _facts(home: Path) -> Path:
    return home / "workspace" / "FACTS.md"


def _import(con, data: bytes, **kw):
    with S.tx(con):
        return S.import_doc(con, data, **kw)


def _imported(home: Path):
    p, con = _store(home)
    S.refresh(con, p)
    return p, con


@pytest.mark.parametrize("variant", ["doc", "edge", "edge-no-final-newline", "edge-crlf", "empty-lines"])
def test_round_trip_byte_identical(home: Path, variant: str) -> None:
    text = {
        "doc": DOC,
        "edge": EDGE,
        "edge-no-final-newline": EDGE.rstrip("\n"),
        "edge-crlf": EDGE.replace("\n", "\r\n"),
        "empty-lines": "\n\n## S\n- x\n\n\n",
    }[variant]
    data = text.encode("utf-8")
    res = S.gate(data, S.connect(":memory:"))
    assert res["identical"], res
    assert res["counts"]["idempotent"]["changed"] == 0


def test_counts_kinds_and_ledger_fields(home: Path) -> None:
    con = S.connect(":memory:")
    _import(con, EDGE.encode())
    kinds = {r["text"]: r["kind"] for r in S.active(con)}
    assert kinds["01.10.2026 kept: year form"] == "decision"
    assert kinds["02.10 his call: board-lint form"] == "decision"
    assert kinds["03.10 paused: not a ledger verb"] == "fact"
    assert kinds["05.10. done: dotted date"] == "decision"
    assert "a dash line in the preamble stays layout" not in " ".join(kinds)
    led = {r["text"]: r for r in S.ledger(con)}
    assert led["02.10 his call: board-lint form"]["verb"] == "decided"
    four = led["04.10 off: thing four; with a semicolon"]
    assert (four["day"], four["month"], four["thing"], four["details"]) == (4, 10, "thing four", "with a semicolon")
    assert four["negative"] and not four["silent"]
    assert led["01.10.2026 kept: year form"]["date"] == "2026-10-01"


def test_fact_fields_and_source(home: Path) -> None:
    con = S.connect(":memory:")
    _import(con, DOC.encode())
    r = {x["text"]: x for x in S.active(con)}
    box = r["Box runs 4 disks — owner, 01.10"]
    assert (box["claim"], box["src"], box["source"], box["verified_at"]) == (
        "Box runs 4 disks",
        "owner, 01.10",
        "owner",
        "2026-10-01",
    )
    assert r["Backups run nightly — live, 02.10"]["source"] == "live"
    assert r["Second group fact — example agent, 03.10"]["source"] == "agent"
    assert r["Plain fact without a source"]["source"] is None
    one = r["03.10 declined: thing one (details here; more)"]
    assert (one["verb"], one["thing"], one["details"], one["day"]) == (
        "declined",
        "thing one",
        "details here; more",
        "03.10",
    )


def test_ids_stable_and_import_idempotent(home: Path) -> None:
    con = S.connect(":memory:")
    _import(con, DOC.encode())
    ids = [r["id"] for r in S.active(con)]
    c = _import(con, DOC.encode())
    assert c["changed"] == 0 and [r["id"] for r in S.active(con)] == ids


def test_hand_edits_become_rows_never_lost(home: Path) -> None:
    con = S.connect(":memory:")
    _import(con, DOC.encode())
    old = {r["text"]: r["id"] for r in S.active(con)}
    edited = (
        DOC.replace("Box runs 4 disks — owner, 01.10", "Box runs 5 disks — owner, 04.10")
        .replace("- Plain fact without a source\n", "")
        .replace("- Backups run nightly — live, 02.10\n", "- Backups run nightly — live, 02.10\n- Brand new fact\n")
        .replace("- 01.10 decided: thing three\n", "")
        .replace("- Second group fact — example agent, 03.10\n", "")
        .replace("## Server\n", "## Server\n- Second group fact — example agent, 03.10\n")
    )
    c = _import(con, edited.encode())
    assert (c["edited"], c["added"], c["removed"], c["moved"]) == (1, 1, 2, 1)
    assert S.render(con) == edited.encode()
    new = {r["text"]: r for r in S.active(con)}
    five = new["Box runs 5 disks — owner, 04.10"]
    assert five["supersedes"] == old["Box runs 4 disks — owner, 01.10"]
    chain = S.history(con, five["id"])
    assert [x["status"] for x in chain] == ["superseded", "active"]
    assert new["Second group fact — example agent, 03.10"]["id"] == old["Second group fact — example agent, 03.10"]
    assert new["Second group fact — example agent, 03.10"]["section"] == "Server"
    gone = con.execute(
        "SELECT status, valid_to FROM records WHERE id=?", (old["Plain fact without a source"],)
    ).fetchone()
    assert tuple(gone) == ("removed", "2026-10-05")
    c2 = _import(con, DOC.encode())  # the edits undone: removed rows come back with their ids
    assert c2["restored"] == 2 and S.render(con) == DOC.encode()
    back = {r["text"]: r["id"] for r in S.active(con)}
    assert back["Plain fact without a source"] == old["Plain fact without a source"]
    ops = {r[0] for r in con.execute("SELECT op FROM record_log")}
    assert {"edit", "add", "remove", "move", "restore", "import"} <= ops
    assert all("disks" not in (r[0] or "") for r in con.execute("SELECT note FROM record_log"))


def test_removal_guard(home: Path) -> None:
    con = S.connect(":memory:")
    big = DOC + "".join(f"- 0{i % 9 + 1}.09 done: filler {i}\n" for i in range(40))
    _import(con, big.encode())
    with pytest.raises(S.Refused, match="would lose"):
        _import(con, DOC.encode())
    assert S.render(con) == big.encode()
    _import(con, DOC.encode(), allow_removals=True)
    assert S.render(con) == DOC.encode()
    with pytest.raises(S.Refused, match="no bullets"):
        _import(con, b"# nothing\n")
    with pytest.raises(S.Refused, match="UTF-8"):
        _import(con, b"## S\n- \xff\n")


def test_gate_cli_and_status(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["gate", "--json"]) == 0
    res = json.loads(capsys.readouterr().out)
    assert res["identical"] and set(res["checks"]) == {"store", "fresh", "idempotent"}
    assert cli.main(["status", "--json"]) == 0
    st = json.loads(capsys.readouterr().out)
    assert (st["facts"], st["decisions"], st["file_matches_rows"]) == (4, 3, True)


def test_dry_run_previews_and_writes_nothing(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    before = _facts(home).read_bytes()
    assert cli.main(["decide", "done", "a thing", "--dry-run", "--json"]) == 0
    res = json.loads(capsys.readouterr().out)
    assert res["dry_run"] and res["diff"] == ["@@ -14,0 +15 @@", "+- 05.10 done: a thing"]
    assert _facts(home).read_bytes() == before
    p, con = _store(home)
    assert not con.execute("SELECT 1 FROM records WHERE origin='tool'").fetchone()


def test_writes_keep_the_round_trip(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p, con = _imported(home)
    capsys.readouterr()
    assert cli.main(["set", "--section", "serv", "Disk 5 added", "--src", "owner", "--json"]) == 0
    res = json.loads(capsys.readouterr().out)
    text = _facts(home).read_text()
    assert "- Backups run nightly — live, 02.10\n- Disk 5 added — owner, 05.10\n\n## Notes" in text
    assert cli.main(["decide", "declined", "new idea", "--details", "he said no", "--json"]) == 0
    dec = json.loads(capsys.readouterr().out)
    assert "(details)`.\n- 05.10 declined: new idea (he said no)\n- 03.10 declined" in _facts(home).read_text()
    assert cli.main(["supersede", res["id"], "Disk 6 added", "--src", "live", "--date", "04.10", "--json"]) == 0
    sup = json.loads(capsys.readouterr().out)
    assert "- Disk 6 added — live, 04.10\n" in _facts(home).read_text()
    assert "Disk 5 added" not in _facts(home).read_text()
    assert cli.main(["supersede", dec["id"], "--remove"]) == 0
    assert "new idea" not in _facts(home).read_text()
    assert cli.main(["gate"]) == 0
    st = S.stats(con, p)
    assert st["tool_writes"] == 4
    chain = S.history(con, sup["id"])
    assert [x["text"] for x in chain] == ["Disk 5 added — owner, 05.10", "Disk 6 added — live, 04.10"]


def test_hand_edit_before_a_write_is_imported_not_lost(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _imported(home)
    capsys.readouterr()
    f = _facts(home)
    f.write_text(f.read_text().replace("- Plain fact without a source\n", "- Plain fact, hand edited\n"))
    assert cli.main(["set", "--section", "Notes", "Tool fact", "--src", "agent x", "--json"]) == 0
    res = json.loads(capsys.readouterr().out)
    assert res["imported"]["edited"] == 1
    text = f.read_text()
    assert "- Plain fact, hand edited\n" in text and "- Tool fact — agent x, 05.10\n" in text


def test_compare_and_swap_retries_when_the_file_changes(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p, con = _imported(home)
    real = S.read_file
    calls = {"n": 0}

    def flaky(pp: S.Paths) -> bytes:
        calls["n"] += 1
        if calls["n"] == 2:  # the CAS read of the first try: a hand edit lands just before it
            pp.facts.write_text(pp.facts.read_text().replace("Box runs 4 disks", "Box runs 4 big disks"))
        return real(pp)

    monkeypatch.setattr(S, "read_file", flaky)
    res = S.write_cycle(con, p, S.op_set("Server", "Racing fact — live, 05.10"))
    text = p.facts.read_text()
    assert res["imported"]["edited"] == 1 and "Box runs 4 big disks" in text and "Racing fact" in text
    assert calls["n"] >= 4


def test_secrets_and_validation_refused(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _imported(home)
    before = _facts(home).read_bytes()
    assert cli.main(["set", "--section", "Server", "token PLANTEDSECRET", "--src", "live"]) == 3
    assert "secret" in capsys.readouterr().err
    assert cli.main(["decide", "maybe", "thing"]) == 3
    assert cli.main(["decide", "done", "thing (with details)"]) == 3
    assert cli.main(["set", "--section", "ledger", "x", "--src", "live"]) == 3
    assert cli.main(["set", "--section", "nope", "x", "--src", "live"]) == 6
    assert cli.main(["set", "--section", "Server", "no source"]) == 3
    assert cli.main(["set", "--section", "Server", "two\nlines", "--raw"]) == 3
    os.unlink(home / "bin" / "secretscan")
    assert cli.main(["set", "--section", "Server", "fine", "--src", "live"]) == 3
    assert "fail-closed" in capsys.readouterr().err
    assert _facts(home).read_bytes() == before


def test_scope_topic_leaves_the_always_render(home: Path) -> None:
    p, con = _imported(home)
    S.write_cycle(con, p, S.op_set("Server", "Only on topic — live, 05.10", scope="topic"))
    assert b"Only on topic" in S.render(con) and b"Only on topic" not in S.render(con, scope="always")


def test_restore_a_missing_file_from_rows(home: Path) -> None:
    p, con = _imported(home)
    assert cli.main(["render", "--write"]) == 3  # exists: nothing to restore
    p.facts.unlink()
    assert cli.main(["render", "--write"]) == 0
    assert p.facts.read_text() == DOC


def test_a_copy_needs_its_own_db(home: Path, tmp_path: Path) -> None:
    copy = tmp_path / "copy.md"
    copy.write_text(DOC)
    with pytest.raises(S.Refused, match="its own --db"):
        S.resolve_paths(None, str(copy))
    assert cli.main(["gate", "--facts", str(copy), "--db", str(tmp_path / "copy.db")]) == 0


def test_script_runs_from_the_repo(home: Path) -> None:
    r = subprocess.run(
        [sys.executable, "-I", str(REPO / "scripts" / "memory"), "--help"], capture_output=True, text=True, timeout=60
    )
    assert r.returncode == 0 and "memory supersede ID" in r.stdout


def test_decide_takes_one_ledger_shaped_string(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _imported(home)
    capsys.readouterr()
    assert cli.main(["decide", "04.10 declined: Ryanair flights (never again, any trip; owner)", "--json"]) == 0
    res = json.loads(capsys.readouterr().out)
    line = "- 04.10 declined: Ryanair flights (never again, any trip; owner)"
    assert res["line"] == line and res["file_line"] == 15
    text = _facts(home).read_text()
    assert f"(details)`.\n{line}\n- 03.10 declined" in text
    p, con = _store(home)
    row = S.get(con, res["id"])
    assert (row["verb"], row["thing"], row["details"]) == (
        "declined",
        "Ryanair flights",
        "never again, any trip; owner",
    )
    # no date: today; "- " prefix, "verb:" without details and the year form parse too
    assert cli.main(["decide", "- done: thing five", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["line"] == "- 05.10 done: thing five"
    assert cli.main(["decide", "01.10.2026 kept: thing six", "--details", "as is", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["line"] == "- 01.10 kept: thing six (as is)"
    assert cli.main(["decide", "02.10 declined: thing seven", "--date", "03.10"]) == 2  # two dates: give it once
    assert "give it once" in capsys.readouterr().err
    before = _facts(home).read_bytes()
    for bad in ("no Ryanair ever", "04.10 maybe: thing", "declined:", "04.10 declined Ryanair"):
        assert cli.main(["decide", bad]) == 2, bad
        err = capsys.readouterr().err
        assert 'memory decide "21.10 declined: Ryanair flights (never again)"' in err, err
    assert cli.main(["decide", "maybe", "thing"]) == 3  # the two-argument form keeps its checks
    assert "e.g. memory decide declined" in capsys.readouterr().err
    assert _facts(home).read_bytes() == before


def test_writes_say_what_changed_so_no_read_back_is_needed(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p, con = _imported(home)
    old = next(r["id"] for r in S.active(con) if r["text"].startswith("Box runs"))
    capsys.readouterr()
    assert cli.main(["supersede", old, "Box runs 5 disks", "--src", "owner"]) == 0
    out = capsys.readouterr().out
    new = next(r["id"] for r in S.active(con) if r["text"].startswith("Box runs"))
    assert f"memory supersede: written · new row {new} replaces {old}" in out
    assert "  FACTS.md line 5 (Server) now reads: - Box runs 5 disks — owner, 05.10\n" in out
    assert _facts(home).read_text().split("\n")[4] == "- Box runs 5 disks — owner, 05.10"
    assert "    -- Box runs 4 disks — owner, 01.10\n    +- Box runs 5 disks — owner, 05.10\n" in out
    assert "FACTS-core.md line" in out and "no read-back needed" in out
    core = (home / "workspace" / "FACTS-core.md").read_text()
    assert "- Box runs 5 disks — owner, 05.10" in core and "4 disks" not in core
    assert cli.main(["supersede", new, "--remove", "--json"]) == 0
    res = json.loads(capsys.readouterr().out)
    assert res["op"] == "remove" and res["file_line"] == 5 and res["core"]["line_no"] is None
    assert "Box runs" not in (home / "workspace" / "FACTS-core.md").read_text()
    assert cli.main(["decide", "done", "a thing", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "dry run, nothing written · would be: new ledger row" in out
    assert "FACTS.md line 14 (Done / declined ledger) would read: - 05.10 done: a thing" in out
    # show: --grep also matches an id; nothing found says so (stderr) instead of printing nothing
    assert cli.main(["show", "--grep", new, "--status", "any"]) == 0
    assert new in capsys.readouterr().out
    assert cli.main(["show", "--grep", "no such words"]) == 0
    got = capsys.readouterr()
    assert got.out == "" and "no active rows match" in got.err
