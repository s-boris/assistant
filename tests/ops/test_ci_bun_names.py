"""`assistant ci`'s bun step names the failing tests in its one-line detail (the nightly log keeps only that line;
29.10 a single unnamed bun failure could not be traced)."""

from __future__ import annotations


def test_names_failing_tests(tool):
    out = ["(pass) a > ok [1.00ms]", "(fail) cards > renders compact [12.50ms]", "(fail) x > y", " 288 pass", " 2 fail"]
    assert tool._bun_failed(out) == ": cards > renders compact; x > y"


def test_caps_at_three(tool):
    out = [f"(fail) t{i} [2ms]" for i in range(5)]
    assert tool._bun_failed(out) == ": t0; t1; t2 +2"


def test_nothing_when_green(tool):
    assert tool._bun_failed(["(pass) a [1ms]", " 1 pass", " 0 fail"]) == ""
