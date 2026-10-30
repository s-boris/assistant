import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_resume_slot():
    """The resumption slot is process-wide (one owner): no test continues another test's conversation."""
    from voice_relay import session
    session.RESUME.handle, session.RESUME.t = None, 0.0
    yield
    session.RESUME.handle, session.RESUME.t = None, 0.0


@pytest.fixture(autouse=True)
def _no_usage_writes(monkeypatch):
    """Sessions log a voice.call_end usage event at hang-up: never into the real usage spool from a test."""
    monkeypatch.setenv("USAGE", "off")
    yield
