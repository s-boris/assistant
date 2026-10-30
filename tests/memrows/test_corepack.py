"""memrows.corepack: the facts core: rules, per-row pins that follow edits, the complete ledger thing list, the
budget steps, and FACTS-core.md kept fresh on import and on writes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from memrows import cli, corepack
from memrows import store as S

DOC = (
    "# Facts: example\n"
    "Intro paragraph for the example file.\n"
    "\n"
    "## Base & box\n"
    "- Box runs 4 disks — owner, 01.10\n"
    "- Backups run nightly to the second box over the local network, checked every morning by a job — live, 02.10\n"
    "\n"
    "## Home\n"
    "- Lamp A is the hall strip — owner, 01.10\n"
    "- Robot: cleans the kitchen daily while out, docks on arrival, mops weekly when the last mop is 7 days old, "
    "and a stuck guard pauses it after five minutes of errors — robot agent, 03.10\n"
    "\n"
    "## Build notes\n"
    "- Widget v2 live (sha abc): the widget renders cards — builder, 04.10\n"
    "\n"
    "## Done / declined ledger\n"
    "Newest first, one line each: `- DD.MM verb: thing (details)`.\n"
    "- 05.10 declined: thing one (details here; more and more words so this line is long enough to shrink)\n"
    "- 04.10 done: thing two — [[memo]]\n"
    "- 03.10 decided: thing three\n"
)

CONF = {"basics_sections": ["Base"], "reference_sections": ["Build notes"], "short_max": 60, "ledger_whole_max": 40}


@pytest.fixture
def home(home_dir: Path) -> Path:
    (home_dir / "workspace" / "FACTS.md").write_text(DOC, encoding="utf-8")
    conf = home_dir / ".config" / "memory2"
    conf.mkdir(parents=True)
    (conf / "core.json").write_text(json.dumps(CONF), encoding="utf-8")
    return home_dir


def _store(home: Path):
    p = S.resolve_paths(None, None)
    con = S.connect(p.db)
    with S.tx(con):
        S.import_doc(con, S.read_file(p))
    return p, con


def _core(home: Path) -> str:
    return (home / "workspace" / "FACTS-core.md").read_text(encoding="utf-8")


def test_rules_place_rows_and_the_ledger_thing_list_is_complete(home: Path) -> None:
    p, con = _store(home)
    text = corepack.render(con, p).decode("utf-8")
    assert "- Box runs 4 disks — owner, 01.10" in text  # basics: whole, also when long
    assert "- Backups run nightly to the second box" in text
    assert "- Lamp A is the hall strip — owner, 01.10" in text  # short
    assert "- Robot:" not in text and "- Widget" not in text  # long, reference
    assert "More on file: Robot" in text and "More on file: Widget v2 live" in text
    assert "- 05.10 declined: thing one\n" in text  # long ledger line: the thing only
    assert "- 04.10 done: thing two — [[memo]]\n" in text and "- 03.10 decided: thing three\n" in text
    things = {(r["verb"], r["thing"]) for r in S.ledger(con)}
    core_led = {
        (d["verb"], d["thing"]) for ln in text.splitlines() if ln.startswith("- ") and (d := S.decision_fields(ln[2:]))
    }
    assert core_led == things  # every ledger thing, parsed by the same matcher as FACTS.md
    assert "Newest first, one line each" in text  # the ledger's own intro stays
    assert text.startswith("# Facts core\n") and "Intro paragraph" not in text
    why = {e["subject"]: e["rule"] for e in corepack.explain(con, p)}
    assert why["Robot"] == "long" and why["Widget v2 live"] == "reference" and why["Box runs 4 disks"] == "basics"


def test_pins_follow_edits_and_auto_returns_to_the_rules(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p, con = _store(home)
    rid = next(r["id"] for r in S.active(con) if r["text"].startswith("Robot:"))
    assert cli.main(["core", "set", rid, "in"]) == 0
    assert "- Robot: cleans the kitchen" in _core(home)  # set re-renders the file
    data = S.read_file(p).replace(b"mops weekly", b"mops every week")  # a hand edit supersedes the row
    p.facts.write_bytes(data)
    assert cli.main(["import", "--quiet"]) == 0
    assert "- Robot: cleans the kitchen daily while out, docks on arrival, mops every week" in _core(home)
    new = next(r["id"] for r in S.active(con) if r["text"].startswith("Robot:"))
    assert new != rid
    assert cli.main(["core", "set", new, "auto"]) == 0
    assert "- Robot:" not in _core(home)
    short = next(r["id"] for r in S.active(con) if r["text"].startswith("Lamp A"))
    assert cli.main(["core", "set", short, "out"]) == 0
    assert "- Lamp A" not in _core(home) and "More on file: Lamp A is the · Robot" in _core(home)  # index only
    assert cli.main(["core", "set", short, "maybe"]) == 3
    old = S.get(con, rid)
    assert old["status"] == "superseded" and cli.main(["core", "set", rid, "in"]) == 3


def test_topic_scope_is_out(home: Path) -> None:
    p, con = _store(home)
    con.execute("UPDATE records SET scope='topic' WHERE text LIKE 'Lamp A%'")
    assert "- Lamp A" not in corepack.render(con, p).decode("utf-8")


def test_budget_steps_keep_pins_basics_and_every_thing(home: Path) -> None:
    conf = home / ".config" / "memory2" / "core.json"
    p, con = _store(home)
    full = corepack.build(con, p)[1]
    conf.write_text(json.dumps(dict(CONF, budget_tokens=full["tokens_est"] - 5, ledger_whole_max=500)))
    data, rep = corepack.build(con, p)
    text = data.decode("utf-8")
    assert rep["budget_steps"] and rep["budget_steps"][0].startswith("drop ")
    assert "- Lamp A" not in text  # the short fact leaves first
    assert "- Box runs 4 disks" in text
    conf.write_text(json.dumps(dict(CONF, budget_tokens=10, ledger_whole_max=500)))
    data, rep = corepack.build(con, p)
    text = data.decode("utf-8")
    assert rep["over_budget"] and "index off" in rep["budget_steps"]
    assert "- 05.10 declined: thing one\n" in text and "- 03.10 decided: thing three" in text
    assert "- Box runs" not in text  # basics leave last, the ledger things never


def test_import_and_writes_keep_the_file_fresh(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p, _con = _store(home)
    target = home / "workspace" / "FACTS-core.md"
    assert cli.main(["import", "--quiet"]) == 0
    first = target.read_bytes()
    mtime = target.stat().st_mtime_ns
    assert cli.main(["import", "--quiet"]) == 0
    assert target.stat().st_mtime_ns == mtime  # unchanged render: not rewritten
    assert cli.main(["decide", "declined", "thing four", "--details", "why not", "--date", "05.10"]) == 0
    assert target.read_bytes() != first and "- 05.10 declined: thing four" in _core(home)
    capsys.readouterr()
    assert cli.main(["core", "--json"]) == 0
    st = json.loads(capsys.readouterr().out)
    assert st["file_current"] is True and st["ledger"] == 4 and st["tokens_est"] > 0
    (home / ".config" / "memory2" / "core-off").write_text("")
    target.unlink()
    assert cli.main(["import", "--quiet"]) == 0
    assert not target.exists()  # core-off: no file writes
    assert cli.main(["render", "--core"]) == 0
    assert capsys.readouterr().out.startswith("# Facts core\n")


def test_bad_config_is_refused(home: Path) -> None:
    (home / ".config" / "memory2" / "core.json").write_text(json.dumps({"short_maxx": 3}))
    p, con = _store(home)
    with pytest.raises(S.Refused):
        corepack.render(con, p)
    assert cli.main(["core"]) == 3
    assert cli.main(["import", "--quiet"]) == 0  # the import itself never fails on the core


def test_a_write_reports_its_place_in_the_core(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _store(home)
    capsys.readouterr()
    assert cli.main(["decide", "05.10 declined: thing four (a long reason, long enough to shrink it here)"]) == 0
    out = capsys.readouterr().out
    core = _core(home).split("\n")
    n = core.index("- 05.10 declined: thing four") + 1
    assert f"  FACTS-core.md line {n} (re-rendered): - 05.10 declined: thing four (shortened to the thing)\n" in out
    assert cli.main(["set", "--section", "Home", "Lamp B is the porch light", "--src", "owner", "--json"]) == 0
    res = json.loads(capsys.readouterr().out)
    c = res["core"]
    assert (c["inc"], c["rule"], c["written"]) == (True, "short", True)
    assert _core(home).split("\n")[c["line_no"] - 1] == res["line"] == "- Lamp B is the porch light — owner, 05.10"
    long_claim = "Robot two: " + "mops and vacuums the hall " * 4
    assert cli.main(["set", "--section", "Home", long_claim.strip(), "--src", "owner"]) == 0
    assert "FACTS-core.md re-rendered: this row stays on file only (rule long;" in capsys.readouterr().out
    (home / ".config" / "memory2" / "core-off").write_text("")
    assert cli.main(["set", "--section", "Home", "Lamp C", "--src", "owner"]) == 0
    out = capsys.readouterr().out
    assert "FACTS-core.md not written (core-off)" in out and "  FACTS.md already re-rendered" in out


def test_sections_without_index_drop_only_the_index_line(home: Path) -> None:
    conf = home / ".config" / "memory2" / "core.json"
    p, con = _store(home)
    before = corepack.render(con, p).decode("utf-8")
    conf.write_text(json.dumps(dict(CONF, sections_without_index=["build"])))  # a prefix, any case
    text = corepack.render(con, p).decode("utf-8")
    assert "More on file: Widget v2 live" in before and "More on file: Widget" not in text
    assert "## Build notes" not in text  # nothing left in it: no empty heading either
    assert "More on file: Robot" in text  # other sections keep theirs
    assert before.replace("\n\n## Build notes\nMore on file: Widget v2 live", "") == text
    why = {e["subject"]: e["rule"] for e in corepack.explain(con, p)}
    assert why["Widget v2 live"] == "reference"  # still out by its rule, still listed by explain


def test_aged_done_lines_leave_only_when_the_knob_is_on(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conf = home / ".config" / "memory2" / "core.json"
    p, con = _store(home)
    facts = S.render(con)
    off = corepack.render(con, p)
    conf.write_text(json.dumps(dict(CONF, done_core_days=0)))
    assert corepack.render(con, p) == off  # 0 = off: byte-identical
    conf.write_text(json.dumps(dict(CONF, done_core_days=14)))
    monkeypatch.setenv("ASSISTANT_NOW", "2026-10-17T12:00:00")  # the done line is 13 days old: stays
    assert "- 04.10 done: thing two" in corepack.render(con, p).decode("utf-8")
    monkeypatch.setenv("ASSISTANT_NOW", "2026-10-20T12:00:00")  # 16 days: leaves; older non-done lines stay
    text = corepack.render(con, p).decode("utf-8")
    assert "thing two" not in text
    assert "- 05.10 declined: thing one\n" in text and "- 03.10 decided: thing three\n" in text
    assert "every ledger thing except `done` lines older than 14 days (" in text
    assert "every ledger thing (" in off.decode("utf-8")
    assert S.render(con) == facts  # FACTS.md keeps every line
    why = {e["subject"]: e["rule"] for e in corepack.explain(con, p)}
    assert why["thing two"] == "ledger-aged" and why["thing three"] == "ledger"
    rid = next(r["id"] for r in S.active(con) if r["text"].startswith("04.10 done"))
    assert cli.main(["core", "set", rid, "in"]) == 0  # a pin wins over the age rule
    assert "- 04.10 done: thing two" in _core(home)


def test_ledger_date_reads_last_year_and_explicit_years() -> None:
    import datetime as dt

    today = dt.date(2026, 10, 20)
    assert corepack.ledger_date("20.10 done: x", today) == today
    assert corepack.ledger_date("25.12 done: x", today) == dt.date(2025, 12, 25)  # not in the future
    assert corepack.ledger_date("03.10.2024 done: x", today) == dt.date(2024, 10, 3)
    assert corepack.ledger_date("31.02 done: x", today) is None and corepack.ledger_date("no date", today) is None


def test_slim_knobs_grouped_ledger_and_aged_verbs(home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Slim knobs: grouped thing list, per-verb aging (never-re-pitch verbs refused), header override, and a candidate
    config rendered from the CLI without touching the live file."""
    conf = home / ".config" / "memory2" / "core.json"
    p, con = _store(home)
    live = corepack.render(con, p)
    conf.write_text(json.dumps(dict(CONF, aged_verbs={})))
    assert corepack.render(con, p) == live  # new keys at their defaults: byte-identical
    slim = dict(
        CONF,
        ledger_style="grouped",
        ledger_intro="Settled things.",
        header="# Core\nShort header.",
        aged_verbs={"decided": 5},
    )
    monkeypatch.setenv("ASSISTANT_NOW", "2026-10-07T12:00:00")
    conf.write_text(json.dumps(slim))
    text = corepack.render(con, p).decode("utf-8")
    assert text.startswith("# Core\nShort header.\n")
    assert (
        "## Done / declined ledger\nSettled things.\n- declined: thing one\n- decided: thing three\n- done: thing two"
        in text
    )
    monkeypatch.setenv("ASSISTANT_NOW", "2026-10-10T12:00:00")  # decided is 7 days old: leaves; declined stays
    text = corepack.render(con, p).decode("utf-8")
    assert "thing three" not in text and "- declined: thing one" in text
    conf.write_text(json.dumps(dict(CONF, aged_verbs={"declined": 3})))
    with pytest.raises(S.Refused, match="never-re-pitch"):
        corepack.render(con, p)
    conf.write_text(json.dumps(dict(CONF, ledger_style="bullets")))
    with pytest.raises(S.Refused, match="ledger_style"):
        corepack.render(con, p)
    conf.write_text(json.dumps(CONF))
    cand = tmp_path / "cand.json"
    cand.write_text(json.dumps(slim))
    out = tmp_path / "core.md"
    before = _core(home) if (home / "workspace" / "FACTS-core.md").exists() else None
    assert cli.main(["render", "--core", "--core-config", str(cand), "--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8").startswith("# Core\n")
    after = _core(home) if (home / "workspace" / "FACTS-core.md").exists() else None
    assert before == after  # the live core file is untouched
    assert cli.main(["render", "--core-config", str(cand)]) != 0  # needs --core
