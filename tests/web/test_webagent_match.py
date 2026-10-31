"""The web agent's commit gate (webagent._matches and its callers): once a step is approved, only the element whose
whole name equals the approved label passes (case, whitespace, surrounding quotes and punctuation aside); a label
inside a longer name, or a toggle / text field with that name, is refused. Callers: the PreToolUse guard (MCP element
description), abx (real role + name parts, via guard) and browsersvc (accessible name + role)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def W(tmp_path_factory):
    state = tmp_path_factory.mktemp("webagent")
    mp = pytest.MonkeyPatch()
    for k, v in {"WEBAGENT_STATE": state / "state", "WEBAGENT_JOURNAL": state / "journal.jsonl",
                 "WEBAGENT_ACCESS": state / "access.json", "USAGE_STATE": state / "usage"}.items():  # fmt: skip
        mp.setenv(k, str(v))
    spec = importlib.util.spec_from_file_location("webagent_match_t", ROOT / "scripts" / "lib" / "legacy" /
                                                  "webagent.py")  # fmt: skip
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    yield mod
    mp.undo()


def task(label="Senden", target=None, tid="wa0001"):
    return {"id": tid, "state": "approved", "pending": {"label": label, "target": target}}


@pytest.mark.parametrize("element", ["Senden", "Senden button", "button Senden", "“Senden” button",
                                     'button "Senden"', "the Senden button", "Senden-Button"])  # fmt: skip
def test_exact_name_passes(W, element):
    assert W._matches(task(), element, None)


@pytest.mark.parametrize("element", ["SENDEN", "  senden ", "Senden\n", "„Senden“", "'Senden'", "Senden!", "sEnDeN."])
def test_case_whitespace_quote_variants_pass(W, element):
    assert W._matches(task(), element, None)


def test_multiword_label_whitespace_and_case(W):
    assert W._matches(task("Jetzt kaufen"), "  jetzt   KAUFEN  button", None)
    assert W._matches(task("“Submit order”"), "Submit order", None)


@pytest.mark.parametrize("element", ["Kopie an x senden", "Kopie an x senden checkbox", "Nachricht senden",
                                     "Senden und Kopie an mich", "Senden checkbox", "checkbox Senden",
                                     "Senden radio button", "Senden switch", "Senden text field"])  # fmt: skip
def test_label_inside_a_longer_name_or_a_toggle_is_refused(W, element):
    assert not W._matches(task(), element, None)


def test_target_selector_still_matches(W):
    t = task(target="button.send")
    assert W._matches(t, "anything", "button.send")
    assert not W._matches(t, "anything", "button.other")


def test_empty_label_never_matches(W):
    assert not W._matches(task(""), "", None)
    assert not W._matches({"pending": None}, "Senden", None)


def test_real_role_and_names(W):
    """abx / browsersvc: the element's real role and name parts; one part must equal the label."""
    t = task()
    assert W._matches(t, "button Senden", None, "button", ["Senden", " senden "])
    assert W._matches(t, "button Nachricht senden Senden", None, "button", ["Nachricht senden", "Senden"])
    assert not W._matches(t, "checkbox Kopie an x senden", None, "checkbox", ["Kopie an x senden"])
    assert not W._matches(t, "checkbox Senden", None, "checkbox", ["Senden"])  # a box is never the button
    assert not W._matches(t, "button Kopie an x senden", None, "button", ["Kopie an x senden"])
    assert not W._matches(t, "button", None, "button", [])


# ---------------------------------------------------------------- the guard (PreToolUse hook + abx path)
def click(element, target="e12", agent=None, **extra):
    ev = {"tool_name": "mcp__browser__browser_click", "tool_input": {"element": element, "target": target, **extra}}
    if agent:
        ev["agent_id"] = agent
    return ev


@pytest.fixture
def approved(W, monkeypatch):
    """One approved task for label "Senden"; _consume's file round trip replaced by a recorder."""
    t = dict(task(), kind="form")
    used = []
    monkeypatch.setattr(W, "all_tasks", lambda: [t])
    monkeypatch.setattr(W, "effective", lambda x: x.get("state"))
    monkeypatch.setattr(W, "ACTIVE", {"approved", "running", "executing"})

    def consume(text, target, tool, role=None, names=None):
        return t if W._matches(t, text, target, role, names) and not used.append(text) else None

    monkeypatch.setattr(W, "_consume", consume)
    return used


def denied(r):
    return bool(r) and r["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_guard_passes_the_real_button(W, approved):
    assert W.guard(click("Senden button")) is None
    assert approved == ["Senden button"]


def test_guard_refuses_the_copy_checkbox(W, approved):
    assert denied(W.guard(click("Kopie an x senden")))
    assert approved == []


def test_guard_compares_the_element_not_its_selector(W, approved):
    assert W.guard(click("Senden", target="form#contact button[type=submit]")) is None


def test_guard_abx_names(W, approved):
    abx = "abx:s1"
    assert denied(W.guard(click("button Kopie an x senden", agent=abx, role="button", names=["Kopie an x senden"])))
    assert W.guard(click("button Senden", agent=abx, role="button", names=["Senden"])) is None


def test_guard_ignores_names_from_an_mcp_call(W, approved):
    """Only abx's own events carry role/names; an MCP tool input with them is read by its element alone."""
    assert denied(W.guard(click("Kopie an x senden", names=["Senden"], role="button")))
    assert denied(W.guard(click("Kopie an x senden", agent="a1b2c3", names=["Senden"], role="button")))


# ---------------------------------------------------------------- browsersvc's WebagentTasks.match
def test_browsersvc_match(W, monkeypatch):
    sys.path.insert(0, str(ROOT / "packages"))
    import browsersvc as B

    t = dict(task(), approval={"via": "tap"})
    monkeypatch.setattr(W, "all_tasks", lambda: [t])
    monkeypatch.setattr(W, "effective", lambda x: x.get("state"))
    wt = B.WebagentTasks(W)
    assert wt.match("Senden", "button") is t
    assert wt.match(" senden ", "button") is t
    assert wt.match("Kopie an x senden", "checkbox") is None
    assert wt.match("Senden", "checkbox") is None
    assert wt.match("Nachricht senden", "button") is None
