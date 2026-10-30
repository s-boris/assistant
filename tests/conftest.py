"""Root test hooks. In the hub's CI runner (ASSISTANT_CI_RUNNER=1) the host-bound tests listed in runner-skip.txt
are not collected; the brain's local CI runs every test.

Under pytest-xdist in `assistant ci` (ASSISTANT_CI_WORKER_DIRS=1, a temp HOME): each worker gets its own HOME and
TMPDIR inside the run's (HOME/gw0, TMPDIR/gw0, ...), set before any test module is imported, so no two workers share
a state file, a lock or a fixed temp name. A plain `pytest -n` on a real HOME is left alone."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "")
if _WORKER and os.environ.get("ASSISTANT_CI_WORKER_DIRS") == "1" and os.environ.get("ASSISTANT_TEST") == "1":
    for _k in ("HOME", "TMPDIR"):
        _base = os.environ.get(_k)
        if _base and os.path.isdir(_base):
            _d = os.path.join(_base, _WORKER)
            os.makedirs(_d, exist_ok=True)
            os.environ[_k] = _d
    tempfile.tempdir = None  # gettempdir() reads TMPDIR again

_SKIP = Path(__file__).with_name("runner-skip.txt")


def _noexec(d: str) -> bool:
    try:
        return bool(os.statvfs(d).f_flag & os.ST_NOEXEC)
    except OSError:
        return False


# A line's @tags: skipped only while one of them holds; an untagged line always (host-bound for good).
_WHEN = {
    # the checkout or the temp dir on a noexec mount (the ci container's /tmp tmpfs): no stub or script runs
    "noexec": lambda: _noexec(str(Path(__file__).resolve().parent)) or _noexec(tempfile.gettempdir()),
}


def _listed() -> set[str]:
    if os.environ.get("ASSISTANT_CI_RUNNER") != "1" or not _SKIP.is_file():
        return set()
    out = set()
    for ln in _SKIP.read_text(encoding="utf-8").splitlines():
        words = ln.split("#", 1)[0].split()
        if not words:
            continue
        tags = [w[1:] for w in words[1:] if w.startswith("@")]
        if not tags or any(_WHEN[t]() for t in tags):  # an unknown tag fails loudly (KeyError)
            out.add(words[0])
    return out


_RUNNER_SKIP = _listed()


def _arg_key(arg: str) -> tuple[str, ...]:
    return Path(arg.split("::", 1)[0]).parts


def pytest_configure(config):
    """The command line's test paths in path order (stable: a file's node ids keep theirs). pytest 9.1 drops a
    directory's conftest fixtures for a file named after a file of the parent directory that came after one of
    its own (`tests/hub/a.py tests/test_suites.py tests/hub/b.py`: b errors with "fixture 'hubhome' not found").
    `assistant ci` passes the impacted files sorted, then the smoke set: tests/test_suites.py in the first part,
    tests/hub/test_h17_hubkit_feed.py in the second, so its 8 tests errored on the first run and passed alone."""
    if len(config.args) > 1:
        config.args[:] = sorted(config.args, key=_arg_key)


def pytest_ignore_collect(collection_path, config):  # noqa: ARG001 (pytest hook signature)
    if not _RUNNER_SKIP:
        return None
    rel = os.path.relpath(str(collection_path), str(config.rootpath)).replace(os.sep, "/")
    return True if rel in _RUNNER_SKIP else None


def pytest_collection_modifyitems(config, items):
    """A node id line (file::test, every parametrization of it too) deselects those tests; so does a file line for
    a file named on the command line (an impacted selection): pytest collects such initial paths whatever
    pytest_ignore_collect (above) says. The tool suites (tests/test_suites.py, the longest file) go first, so an
    xdist worker starts them at once instead of after its other files."""
    if os.environ.get("ASSISTANT_CI_SUITES"):
        items.sort(key=lambda it: not it.nodeid.startswith("tests/test_suites.py::"))  # stable: the rest keep order
    if not _RUNNER_SKIP:
        return
    keep, drop = [], []
    for it in items:
        nid = it.nodeid.split("[", 1)[0]
        (
            drop if it.nodeid in _RUNNER_SKIP or nid in _RUNNER_SKIP or nid.split("::", 1)[0] in _RUNNER_SKIP else keep
        ).append(it)
    if drop:
        config.hook.pytest_deselected(items=drop)
        items[:] = keep
