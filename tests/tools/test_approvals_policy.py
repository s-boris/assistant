"""The hub's approval API in the Cedar policy: the full principal x action matrix, the deny reasons, schema
validation, and no effect on the typed tools' decisions."""

from __future__ import annotations

import pytest

cedarpy = pytest.importorskip("cedarpy")

from tools.core import load  # noqa: E402
from tools.policy import API_OPS, PRINCIPALS, Engine, Principal, api_action, schema_text  # noqa: E402

ALLOWED = {
    "brain": {"request", "consume", "cancel", "read", "attach"},  # attach: webagent notes its card's message
    "subagent": {"request", "consume", "cancel", "read", "attach"},
    "binding": {"decide", "card", "expire", "attach", "read"},
    "reflex": set(),
    "ci": set(),
    "voice": set(),  # adapter principals: no approval route takes them (hubd: brain and tg only)
    "phone": set(),
}


@pytest.fixture(scope="module")
def engine():
    return Engine(load())


def test_policy_validates_with_the_api_actions(engine):
    assert engine.validate() == []
    sch = schema_text(engine.reg)
    for op in API_OPS:
        assert f'action "{api_action(op)}" appliesTo' in sch
    assert "entity Api;" in sch


@pytest.mark.parametrize("kind", PRINCIPALS)
@pytest.mark.parametrize("op", API_OPS)
def test_matrix(engine, kind, op):
    d = engine.decide_api(Principal(kind), op)
    assert d.allowed is (op in ALLOWED[kind]), (kind, op, d.as_dict())
    assert d.errors == 0


def test_deny_reasons(engine):
    d = engine.decide_api(Principal("binding"), "request")
    assert d.policies == ("approvals-tg-never-mints",) and "only records his tap" in d.reason
    for kind in ("brain", "subagent", "reflex", "ci"):
        d = engine.decide_api(Principal(kind), "decide")
        assert d.policies == ("approvals-only-his-tap-decides",)
    d = engine.decide_api(Principal("ci"), "request")
    assert not d.allowed and d.policies == () and "no policy lets a ci request approvals" in d.reason
    assert not engine.decide_api(Principal("binding"), "nonsense").allowed


def test_tool_decisions_unchanged(engine):
    """The approval policies name only Hub::Api: every typed tool decides as before (brain: up to send_external)."""
    for spec in engine.reg.typed.values():
        d = engine.decide(Principal("brain"), spec)
        assert d.allowed is (
            spec.effect in ("read", "home", "send_owner", "send_external") and spec.approval != "owner"
        )
