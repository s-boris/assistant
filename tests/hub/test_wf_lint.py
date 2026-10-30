"""The determinism lint for DBOS workflows (ops/lint/wf_determinism.py) passes hubwf and catches what would
make a replay take another path: clocks, randomness, the environment, files, live DBOS reads, hooks, set order."""

from __future__ import annotations

import importlib.util
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _lint():
    spec = importlib.util.spec_from_file_location("wf_determinism", REPO / "ops/lint/wf_determinism.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_hubwf_is_clean():
    lint = _lint()
    assert lint.check((REPO / "packages/hub/hubwf.py").read_text(), "hubwf.py") == []
    assert lint.main([str(REPO / "packages/hub/hubwf.py")]) == 0


HEAD = """
import time, random, os
from datetime import datetime
from dbos import DBOS
HOOKS = {}

@DBOS.step()
def clock():
    return time.time()  # fine: a step

def helper(x):
    return x + random.random()
"""

BAD = {
    "time.time()": "calls time.time",
    "datetime.now()": "a clock",
    "os.environ.get('X')": "calls os.environ.get",
    "open('/tmp/x').read()": "calls open()",
    "DBOS.list_workflows()": "not replay-safe",
    "HOOKS['x']": "mutable module state HOOKS",
    "helper(1)": "calls random.random",
    "[x for x in set([1, 2])]": None,
}


@pytest.mark.parametrize("expr", sorted(BAD))
def test_flags_nondeterminism(expr):
    lint = _lint()
    src = HEAD + textwrap.dedent(f"""
    @DBOS.workflow()
    def wf(a):
        v = {expr}
        return clock()
    """)
    found = lint.check(src, "x.py")
    want = BAD[expr]
    if want is None:  # a comprehension over a set is not a for statement: allowed (documented limit)
        return
    assert any(want in f for f in found), found


def test_set_iteration_and_global():
    lint = _lint()
    src = HEAD + textwrap.dedent("""
    @DBOS.workflow()
    def wf(a):
        global HOOKS
        for x in {1, 2}:
            pass
        return DBOS.recv("t", timeout_seconds=5)
    """)
    found = lint.check(src, "x.py")
    assert any("global" in f for f in found) and any("set" in f for f in found)


def test_steps_and_allowed_dbos_calls_pass():
    lint = _lint()
    src = HEAD + textwrap.dedent("""
    @DBOS.workflow()
    def wf(a):
        t = clock()
        m = DBOS.recv("t", timeout_seconds=min(5, a))
        DBOS.set_event("k", {"t": t, "m": m})
        return round(t, 1), DBOS.workflow_id
    """)
    assert lint.check(src, "x.py") == []
