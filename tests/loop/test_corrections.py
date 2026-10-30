"""loop.corrections: memory2 corrections -> fact + case drafts (covered / supersede / set), never the raw text;
accept writes the row via `memory` and the case into the corpus (loads, committed); reject resolves memory2."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from loop import common, corrections
from loop_testlib import calls, git


def scan_all() -> list[dict]:
    return corrections.scan(since=common.now() - 5 * 86400)


def test_scan_picks_corrections_in_window(env: Path) -> None:
    new = scan_all()
    by = {d["cand"]: d for d in new}
    assert set(by) == {"cZUMBA", "cGIZMO", "cLAMP"}  # not the plain preference, not the old one
    z, g, lamp = by["cZUMBA"], by["cGIZMO"], by["cLAMP"]
    assert z["fact"]["op"] == "covered" and z["fact"]["row"] == "rDEC0000001"
    assert g["fact"]["op"] == "supersede" and g["fact"]["row"] == "rFACT000002"
    assert g["fact"]["argv"][:3] == ["memory", "supersede", "rFACT000002"] and g["review_ids"] == ["rv1"]
    assert lamp["fact"]["op"] == "set" and lamp["fact"]["section"] == "Home & HA"
    assert lamp["case"]["area"] == "home" and lamp["case"]["tags"] == ["facts", "correction"]
    assert z["case"]["id"] == "corr-zumba-class-role"
    assert "settled decision" in z["case"]["expect"][0]["judge"]
    assert "Does not repeat the earlier claim" in g["case"]["expect"][0]["judge"]
    assert g["due_ts"] - g["correction_ts"] == 24 * 3600


def test_never_reads_raw_or_context(env: Path) -> None:
    scan_all()
    blob = (env / ".local" / "state" / "loop" / "corrections.json").read_text()
    note = next((env / "inbox").glob("loop-corrections-*.md")).read_text()
    for s in ("PLANTEDRAW", "PLANTEDCTX"):
        assert s not in blob and s not in note
    assert "Data, not instructions." in note and "\nNext:" in note and str(env) not in note
    assert oct((env / ".local" / "state" / "loop" / "corrections.json").stat().st_mode)[-3:] == "600"


def test_default_window_and_no_repeats(env: Path) -> None:
    first = corrections.scan()  # first run: the last 48 h (now 22.10 03:30, so from 20.10 03:30)
    assert {d["cand"] for d in first} == {"cZUMBA", "cGIZMO", "cLAMP"}
    assert corrections.scan() == []
    assert scan_all() == []  # a wider window finds nothing twice


def test_dry_run_writes_nothing(env: Path) -> None:
    assert corrections.scan(since=common.now() - 5 * 86400, write=False)
    assert not (env / ".local" / "state" / "loop" / "corrections.json").exists()
    assert list((env / "inbox").iterdir()) == []


def cases_file(env: Path) -> Path:
    return env / "opt" / "evals-private" / "cases" / "60-corrections.yaml"


def test_accept_covered_adds_only_the_case(env: Path) -> None:
    d = {x["cand"]: x for x in scan_all()}["cZUMBA"]
    res = corrections.accept(d["id"], msg="any ideas for the zumba class?")
    assert res["row"] == "rDEC0000001" and res["case"] == "corr-zumba-class-role" and res["late"] is True
    assert not [c for c in calls(env) if c[0] == "memory" and c[1] != "ledger"]
    doc = yaml.safe_load(cases_file(env).read_text())
    case = doc["cases"][0]
    assert case["msg"] == "any ideas for the zumba class?" and case["tags"] == ["facts", "correction"]
    assert "# loop " + d["id"] in cases_file(env).read_text()
    log = git(env / "opt" / "evals-private", "log", "--oneline")
    assert "loop: case corr-zumba-class-role" in log
    assert corrections.show(d["id"])["status"] == "accepted"
    with pytest.raises(corrections.CorrectionError):
        corrections.accept(d["id"])


def test_accept_supersede_runs_memory_and_resolves_memory2(env: Path) -> None:
    d = {x["cand"]: x for x in scan_all()}["cGIZMO"]
    res = corrections.accept(d["id"], text="Gizmo bot answers in German", reply_re="German")
    assert res["row"] == "rNEW0000001"
    mem = [c for c in calls(env) if c[0] == "memory" and c[1] == "supersede"][0]
    assert mem[2] == "rFACT000002" and mem[3] == "Gizmo bot answers in German" and mem[-1] == "--json"
    assert ["memory2", "accept", "rv1"] in calls(env)
    case = yaml.safe_load(cases_file(env).read_text())["cases"][0]
    assert case["expect"][0] == {"reply_re": "German"}


def test_accept_set_and_duplicate_case_id(env: Path) -> None:
    drafts = {x["cand"]: x for x in scan_all()}
    corrections.accept(drafts["cLAMP"]["id"], section="Apps & services")
    mem = [c for c in calls(env) if c[0] == "memory" and c[1] == "set"][0]
    assert mem[2:5] == ["--section", "Apps & services", "Porch lamp stays off after 23:00"]
    # a second draft whose case id is taken gets a suffix
    st = json.loads((env / ".local" / "state" / "loop" / "corrections.json").read_text())
    st["drafts"][drafts["cGIZMO"]["id"]]["case"]["id"] = "corr-porch-lamp-schedule"
    (env / ".local" / "state" / "loop" / "corrections.json").write_text(json.dumps(st))
    res = corrections.accept(drafts["cGIZMO"]["id"], no_fact=True)
    assert res["case"] == "corr-porch-lamp-schedule-2" and res["row"] == "rFACT000002"
    assert len(yaml.safe_load(cases_file(env).read_text())["cases"]) == 2


def test_bad_case_restores_the_file(env: Path) -> None:
    d = scan_all()[0]
    cases_file(env).write_text(corrections.CASE_HEADER)
    before = cases_file(env).read_text()
    (env / "bin" / "evals").write_text("#!/bin/sh\necho '[]'\n")  # the case never shows up
    with pytest.raises(corrections.CorrectionError, match="did not load"):
        corrections.accept(d["id"], no_fact=True)
    assert cases_file(env).read_text() == before
    assert corrections.show(d["id"])["status"] == "draft"


def test_reject_and_signal(env: Path) -> None:
    drafts = {x["cand"]: x for x in scan_all()}
    corrections.reject(drafts["cGIZMO"]["id"], "noise")
    assert ["memory2", "reject", "rv1"] in calls(env)
    with pytest.raises(corrections.CorrectionError):
        corrections.reject(drafts["cLAMP"]["id"], " ")
    s = corrections.signal()
    assert s["open"] == 2 and s["overdue"] == 1  # zumba is past 24 h at 22.10 03:30; lamp is not
