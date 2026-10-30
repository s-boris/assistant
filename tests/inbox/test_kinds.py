"""Kind registry + every inbox filename shape."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest
from inbox import kinds
from inbox_testlib import KINDS, REPO

SHAPES = [
    # NAME-<epoch>.md and -<epoch>-<n>.md
    ("vault-events-1792602808.md", "note.vault-events", {}),
    ("vault-events-1792602808-2.md", "note.vault-events", {}),
    ("vault-events-ha-1792602808.md", "note.vault-events-ha", {}),
    ("vault-events-mail-1792602808.md", "note.vault-events-mail", {}),
    ("vault-events-failing-1792602808.md", "note.vault-events-failing", {}),
    ("vault-plans-1792602808.md", "note.vault-plans", {}),
    ("reminder-1792602808.md", "note.reminder", {}),
    ("reminder-place-1792602808.md", "note.reminder-place", {}),
    ("voice-1792602808.md", "note.voice", {}),
    ("voice-recap-1792602808.md", "note.voice-recap", {}),
    ("voice-decisions-1792602808.md", "note.voice-decisions", {}),
    ("phone-chat-20261020-224817-c2c4.md", "note.phone-chat", {}),
    ("phone-reject-1792602808.md", "note.phone-reject", {}),
    ("shop-new-items-1792602808.md", "note.shop-new-items", {}),
    ("daily-plan-1792602808.md", "cue.daily-plan", {}),
    # scheduled cues: their note names, typed cue.* kinds
    ("digest-1792602808.md", "cue.weekly-digest", {}),
    ("birthdays-1792602808.md", "cue.birthdays", {}),
    ("idea-scan-1792602808.md", "cue.idea-scan", {}),
    ("self-cleanup-1792602808.md", "cue.self-cleanup", {}),
    ("calendar-gap-pass-1792602808.md", "cue.calendar-gap-pass", {}),
    ("waiting-nudge-2031-06-15.md", "cue.waiting-nudge", {}),
    ("digest-failed-1792602808.md", "cron.fail", {"job": "digest"}),
    ("unraid-health-1792602808.md", "note.unraid-health", {}),
    ("security-watch-1792602808.md", "note.security-watch", {}),
    ("codebase-sync-1792602808.md", "note.codebase-sync", {}),
    ("ha-left-on-1021-1930.md", "note.ha-left-on", {}),
    ("rave-radar-1792602808.md", "note.rave-radar", {}),
    ("rave-radar-releases-1792602808.md", "note.rave-radar-releases", {}),
    ("gift-radar-example.md", "note.gift-radar", {}),
    ("artist-settime-1792602808.md", "note.settime", {}),
    # restart-cause-YYYYMMDD-HHMM.md, restart-*.md
    ("restart-cause-20261021-1930.md", "note.restart-cause", {}),
    ("restart-1792602808.md", "note.restart", {}),
    # NAME-failed-<epoch>.md (cron-fail), incl. a name that also matches a more specific prefix
    ("train-watch-failed-1792602808.md", "cron.fail", {"job": "train-watch"}),
    ("vault-events-failed-1792602808.md", "cron.fail", {"job": "vault-events"}),
    ("bills-week-failed-1792602808-1.md", "cron.fail", {"job": "bills-week"}),
    # bigfile-*
    ("bigfile-1792602808.md", "note.bigfile", {}),
    ("bigfile-20261021-193000-ab12.md", "note.bigfile", {}),
    # unregistered shapes: note.<prefix>
    ("model-drift-20261021.md", "note.model-drift", {}),
    ("deadlines-validate-2026-10-21.md", "note.deadlines-validate", {}),
    ("strange-thing-ab12cd34.md", "note.strange-thing", {}),
    ("Mixed-Case-1792602808.md", "note.mixed-case", {}),
    ("1792602808.md", "note.misc", {}),
    ("senders.json", "note.senders", {}),
]


@pytest.fixture(scope="module")
def reg() -> kinds.Registry:
    r = kinds.load(KINDS)
    assert r.error is None, r.error
    return r


@pytest.mark.parametrize("name,kind,extras", SHAPES)
def test_filename_shapes(reg: kinds.Registry, name: str, kind: str, extras: dict[str, str]) -> None:
    m = reg.classify(name)
    assert (m.kind, m.extras) == (kind, extras)


@pytest.mark.parametrize(
    "name,prefix",
    [
        ("vault-events-1792602808-2.md", "vault-events"),
        ("restart-cause-20261021-1930.md", "restart-cause"),
        ("phone-chat-20261020-224817-c2c4.md", "phone-chat"),
        ("x-21.10-y.md", "x"),
        ("dead-beef.md", "dead-beef"),
        ("a-face1.md", "a"),
        ("..md", "misc"),
        ("T12.md", "misc"),
    ],
)
def test_prefix_of(name: str, prefix: str) -> None:
    assert kinds.prefix_of(name) == prefix


def test_registry_has_the_busiest_kinds(reg: kinds.Registry) -> None:
    want = {
        "note.vault-events",
        "note.vault-events-ha",
        "note.reminder",
        "note.voice",
        "note.restart",
        "note.vault-plans",
        "note.rave-radar",
        "note.voice-recap",
        "note.gift-radar",
        "note.unraid-health",
        "note.shop-new-items",
        "note.phone-reject",
        "cue.daily-plan",
        "note.codebase-sync",
        "note.ha-left-on",
        "note.security-watch",
        "cron.fail",
        "note.bigfile",
    }
    assert want <= set(reg.kinds)
    assert len(reg.kinds) >= 25
    for k, e in reg.kinds.items():
        assert e.get("hint") and e.get("jobs") and e.get("match"), k


def test_registry_matches_its_schema() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((REPO / "schemas" / "kinds.schema.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    with open(KINDS, "rb") as f:
        jsonschema.validate(tomllib.load(f), schema)


def test_jobs_sections_exist_in_jobs_md() -> None:
    """The jobs anchors name JOBS.md section headings, or the start of one before " (" (live file when there)."""
    jobs = Path.home() / "workspace" / "JOBS.md"
    if not jobs.exists():
        pytest.skip("no JOBS.md in this HOME")
    heads = {line[3:].strip() for line in jobs.read_text(encoding="utf-8").splitlines() if line.startswith("## ")}
    heads.add("Recurring jobs (reference)")
    reg = kinds.load(KINDS)
    for a in {e["jobs"] for e in reg.kinds.values()}:
        assert any(h == a or h.startswith(a + " (") for h in heads), a


def test_label_lines_and_defaults(reg: kinds.Registry) -> None:
    assert reg.labels_for("cron.fail", "x\n\nRecovered 21.10 19:00 after 3 failed run(s).\n") == [
        "failure",
        "recovered",
    ]
    assert reg.labels_for("cron.fail", "no recovery here") == ["failure"]
    assert reg.delivery("note.voice") == "now"
    assert reg.delivery("note.unknown") == "beat"
    assert reg.hint("note.unknown") == ""


def test_broken_or_missing_registry_degrades(tmp_path: Path) -> None:
    bad = tmp_path / "kinds.toml"
    bad.write_text("this is = = not toml", encoding="utf-8")
    r = kinds.load(bad)
    assert r.error and r.kinds == {}
    assert r.classify("vault-events-1.md").kind == "note.vault-events"
    assert (r.classify("x-failed-1792602808.md").kind, r.classify("x-failed-1792602808.md").extras) == (
        "cron.fail",
        {"job": "x"},
    )
    r2 = kinds.load(tmp_path / "missing.toml")
    assert r2.kinds == {}
