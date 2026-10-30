"""loop.reflex: registry v0 checks (tool, policy, eval case, why), retire + version, firing log, match, counts,
the agenda counter reading the same store, and the CLI."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from inbox import agenda, sources
from loop import reflex
from loop_testlib import PY

REPO_ROOT = Path(__file__).resolve().parents[2]
WHY = "the same answer every time: a watched deal under target gets the standard card"


def add(name: str = "deal-card", **kw: object) -> dict:
    a = dict(kind="note.deals-*", tool="reminders.add", why=WHY, case="case-a")
    a.update(kw)
    return reflex.add(name, **a)  # type: ignore[arg-type]


def test_starts_empty(env: Path) -> None:
    assert reflex.ls() == [] and reflex.counts(None)["active"] == 0
    assert sources.reflex_counts(None) is None


@pytest.mark.parametrize(
    "kw,err",
    [
        ({"name": "Bad Name"}, "name"),
        ({"why": "short"}, "--why"),
        ({"tool": "nope.tool"}, "unknown tool"),
        ({"tool": "vault.ask"}, "does not let a reflex"),
        ({"tool": "ha.set"}, "needs his approval"),
        ({"case": "no-such-case"}, "no eval case"),
        ({"when": "[1]"}, "JSON object"),
        ({"kind": " "}, "--kind"),
    ],
)
def test_add_refusals(env: Path, kw: dict, err: str) -> None:
    name = kw.pop("name", "deal-card")
    with pytest.raises(reflex.ReflexError, match=err):
        add(name, **kw)
    assert reflex.ls(include_retired=True) == []


def test_add_fire_rm_readd(env: Path) -> None:
    r = add(when='{"payload.shop": "galaxus*", "kind": "x"}', args='{"text": "card"}')
    assert r["version"] == 1 and r["status"] == "active" and r["eval_case"] == "case-a"
    with pytest.raises(reflex.ReflexError, match="exists"):
        add()
    reflex.fire("deal-card", "ok", event="123", ms=40, note="  event 123\nok  ")
    reflex.fire("deal-card", "misfire", event="124")
    with pytest.raises(reflex.ReflexError):
        reflex.fire("deal-card", "maybe")
    row = reflex.ls()[0]
    assert row["fired_24h"] == 2 and row["misfires_7d"] == 1 and row["last_fired"]
    log = reflex.log("deal-card")
    assert [x["outcome"] for x in log] == ["misfire", "ok"] and log[1]["note"] == "event 123 ok"
    sig = reflex.signal()
    assert sig["misfires_by_name"] == {"deal-card": 1} and sig["fired_24h"] == 2
    reflex.rm("deal-card", "stopped being obvious")
    assert reflex.ls() == [] and reflex.ls(include_retired=True)[0]["status"] == "retired"
    with pytest.raises(reflex.ReflexError, match="no active"):
        reflex.fire("deal-card", "ok")
    with pytest.raises(reflex.ReflexError, match="no active"):
        reflex.rm("deal-card", "again")
    assert add()["version"] == 2


def test_match(env: Path) -> None:
    add(when='{"payload.shop": "galaxus*"}')
    add("other", kind="note.other", when=None)
    assert [m["name"] for m in reflex.match("note.deals-hdd", '{"payload": {"shop": "galaxus.ch"}}')] == ["deal-card"]
    assert reflex.match("note.deals-hdd", '{"payload": {"shop": "brack"}}') == []
    assert reflex.match("note.deals-hdd", None) == []
    assert [m["name"] for m in reflex.match("note.other")] == ["other"]


def test_agenda_counter(env: Path) -> None:
    add()
    reflex.fire("deal-card", "ok")
    reflex.fire("deal-card", "misfire")
    c = sources.reflex_counts(0)
    assert c == {"active": 1, "fired": 2, "by_name": {"deal-card": 2}, "misfires": 1}
    assert reflex.counts(0)["fired"] == 2
    line = [x for x in agenda._footer(_now(), _tz(), None, None, None, [], 0, c) if x.startswith("Reflexes:")]
    assert line == ["Reflexes: 1 active · fired since last beat 2 (deal-card ×2) · misfires 1 (reflex ls · reflex log)"]
    assert not [x for x in agenda._footer(_now(), _tz(), None, None, None, [], 0, None) if x.startswith("Reflexes")]
    future = sources.reflex_counts(9_999_999_999)
    assert future == {"active": 1, "fired": 0, "by_name": {}, "misfires": 0}


def _tz():
    from zoneinfo import ZoneInfo

    return ZoneInfo("Europe/Zurich")


def _now():
    from datetime import datetime

    return datetime(2026, 10, 22, 3, 30, tzinfo=_tz())


def test_cli(env: Path) -> None:
    run = lambda *a: subprocess.run(  # noqa: E731
        [PY, "-I", str(REPO_ROOT / "scripts" / "reflex"), *a],
        capture_output=True,
        text=True,
        env=dict(os.environ),
    )
    r = run("ls")
    assert r.returncode == 0 and "none" in r.stdout
    r = run(
        "add",
        "deal-card",
        "--kind",
        "note.deals-*",
        "--tool",
        "reminders.add",
        "--why",
        WHY,
        "--case",
        "case-a",
        "--json",
    )
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["name"] == "deal-card"
    r = run("add", "x1", "--kind", "k", "--tool", "vault.ask", "--why", WHY, "--case", "case-a")
    assert r.returncode == 3 and "reflex:" in r.stderr
    assert run("fire", "deal-card", "--outcome", "ok", "--event", "5").returncode == 0
    r = run("match", "note.deals-hdd", "--json")
    assert json.loads(r.stdout)[0]["name"] == "deal-card"
    r = run("counts", "--since", "0", "--json")
    assert json.loads(r.stdout)["fired"] == 1
    assert run("rm", "deal-card", "--why", "test over").returncode == 0
