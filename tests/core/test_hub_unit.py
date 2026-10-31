import sqlite3

import pytest

from core import hub


def _con():
    con = sqlite3.connect(":memory:", factory=hub.LedgerConnection, isolation_level=None)
    con._session = {"done": False}
    return con


@pytest.mark.parametrize("err", [hub.HubDown("URLError"), hub.Refused(429, "rate_limited", "")])
def test_unit_commit_hub_error_is_operational(monkeypatch, err):
    # the hub going away between snapshot and commit must read as "hub unreachable" (sqlite3.Error), not a crash
    monkeypatch.setattr(hub, "ledger", lambda **kw: _con())

    def boom(self):
        raise err

    monkeypatch.setattr(hub.LedgerConnection, "commit_hub", boom)
    with pytest.raises(sqlite3.OperationalError, match="hub: "):
        hub.unit(lambda c: 1, profile="ingest")


def test_unit_retries_a_conflict_then_gives_up(monkeypatch):
    n = []
    monkeypatch.setattr(hub, "ledger", lambda **kw: _con())

    def conflict(self):
        n.append(1)
        raise hub.Conflict("changed")

    monkeypatch.setattr(hub.LedgerConnection, "commit_hub", conflict)
    with pytest.raises(sqlite3.OperationalError, match="kept changing"):
        hub.unit(lambda c: 1, tries=2)
    assert len(n) == 2
