"""Approvals are minted, read and consumed on the hub (the hub's adapter records his tap there and never mints); a
test run without a hub (no HUB_URL) uses the local store."""

from __future__ import annotations

from pathlib import Path

import approvals
import pytest


@pytest.fixture
def hubmode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HUB_URL", "http://127.0.0.1:9")


def test_local_without_a_hub(home):
    assert approvals._hub() is False


def test_hub_when_a_hub_is_set(home, hubmode):
    assert approvals._hub() is True


def test_new_mints_on_the_hub(home, hubmode, monkeypatch: pytest.MonkeyPatch):
    calls = []

    def fake(method, path, body=None):
        calls.append((method, path, sorted(body or {})))
        return {"nonce": "a" * 32, "expires": 2_000_000_000.0, "summary": body["summary"], "ref": None}

    def no_local(*a, **k):
        raise AssertionError("the local store must not be opened in hub mode")

    monkeypatch.setattr(approvals, "_hcall", fake)
    monkeypatch.setattr(approvals, "_open", no_local)
    got = approvals.new("web:book table 4 at 19:00", "Book the table", 600)
    assert got["nonce"] == "a" * 32
    assert calls == [("POST", "/approvals", ["action_sha", "chat", "ref", "rid", "summary", "ttl"])]


def test_own_connection_to_the_default_store_is_refused_in_hub_mode(home, hubmode, tmp_path: Path):
    """A caller's own connection would skip the hub, so his tap (decided on the hub) could never reach the card's
    nonce. Minting through a connection on the default store raises (the caller falls back); a scratch store
    stays free."""
    con = approvals.connect()
    try:
        with pytest.raises(RuntimeError, match="hub approvals are on"):
            approvals.new("web:pay EUR 1", "Pay", 600, con=con)
        assert con.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0
    finally:
        con.close()
    other = approvals.connect(tmp_path / "scratch.db")
    try:
        assert approvals._hub() is True
        assert approvals.new("web:pay EUR 1", "Pay", 600, con=other)["nonce"]
    finally:
        other.close()
