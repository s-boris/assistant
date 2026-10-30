"""media tests: a fresh Media world (tmp dir, temp HOME, staged repo copy, fake servers killed after) per test.
Helpers live in mediatest.py."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mediatest import Media  # noqa: E402


@pytest.fixture
def media(tmp_path):
    w = Media(tmp_path)
    yield w
    w.close()
