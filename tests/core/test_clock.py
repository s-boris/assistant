import datetime as dt
import time

import pytest

from core import clock


def test_real_time(monkeypatch):
    monkeypatch.delenv("ASSISTANT_NOW", raising=False)
    assert abs(clock.now() - time.time()) < 2


def test_epoch_override(monkeypatch):
    epoch = int(dt.datetime(2026, 10, 25, tzinfo=dt.UTC).timestamp())
    monkeypatch.setenv("ASSISTANT_NOW", str(epoch))
    assert clock.now() == float(epoch)
    assert clock.now_dt(dt.UTC) == dt.datetime(2026, 10, 25, 0, 0, tzinfo=dt.UTC)


def test_iso_override(monkeypatch):
    monkeypatch.setenv("ASSISTANT_NOW", "2026-10-21T19:00:00+02:00")
    assert clock.now() == dt.datetime(2026, 10, 21, 17, 0, tzinfo=dt.UTC).timestamp()
    assert clock.today(dt.UTC) == dt.date(2026, 10, 21)
    monkeypatch.setenv("ASSISTANT_NOW", "2026-10-21T17:00:00Z")
    assert clock.now_dt(dt.UTC).hour == 17


def test_naive_is_local(monkeypatch):
    monkeypatch.setenv("ASSISTANT_NOW", "2026-10-21T19:00:00")
    assert clock.now() == dt.datetime(2026, 10, 21, 19, 0).astimezone().timestamp()
    assert clock.now_dt().tzinfo is not None


def test_bad_override_raises(monkeypatch):
    monkeypatch.setenv("ASSISTANT_NOW", "tomorrow-ish")
    with pytest.raises(ValueError):
        clock.now()
