"""hubkit.ci.wait waits out a hub restart (HubDown) within its timeout instead of failing the land at once."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "packages"), str(ROOT)]

from hubkit import ci as hci  # noqa: E402

from core import hub  # noqa: E402


def test_wait_survives_a_hub_restart(monkeypatch):
    answers = iter([hub.HubDown("ConnectionRefusedError"), hub.HubDown("ConnectionRefusedError"), {"state": "done"}])

    def job(jid, timeout=15.0):
        a = next(answers)
        if isinstance(a, Exception):
            raise a
        return a

    monkeypatch.setattr(hci, "job", job)
    monkeypatch.setattr(hci.time, "sleep", lambda s: None)
    assert hci.wait("j1", 60)["state"] == "done"


def test_wait_raises_hubdown_once_its_time_is_up(monkeypatch):
    def job(jid, timeout=15.0):
        raise hub.HubDown("ConnectionRefusedError")

    monkeypatch.setattr(hci, "job", job)
    monkeypatch.setattr(hci.time, "sleep", lambda s: None)
    with pytest.raises(hub.HubDown):
        hci.wait("j1", 0)
