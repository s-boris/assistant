"""evals' run lock with --lock-wait (29.10): a land's eval step waits for another land's subset instead of failing
with "another run is active"; without it (the default 0) a held lock still fails at once."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _evals(monkeypatch, tmp_path):
    monkeypatch.setenv("EVALS_STATE", str(tmp_path / "state"))
    loader = importlib.machinery.SourceFileLoader("evals_lockwait", str(REPO / "scripts" / "evals"))
    spec = importlib.util.spec_from_loader("evals_lockwait", loader)
    assert spec
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def test_lock_wait_queues_behind_a_running_eval(monkeypatch, tmp_path):
    ev = _evals(monkeypatch, tmp_path)
    held = ev.run_lock()
    assert held is not None
    assert ev.run_lock() is None  # no wait: busy at once, as before
    t0 = time.monotonic()
    assert ev.run_lock(0.3) is None and time.monotonic() - t0 >= 0.25  # waited, still busy
    threading.Timer(0.5, os.close, (held,)).start()  # the other land's eval run ends
    t0 = time.monotonic()
    fd = ev.run_lock(10.0)
    assert fd is not None and 0.4 <= time.monotonic() - t0 < 5
    os.close(fd)
    p = ev.argparse.ArgumentParser()
    ev.add_run_args(p)
    assert p.parse_args(["--lock-wait", "1800"]).lock_wait == 1800 and p.parse_args([]).lock_wait == 0
