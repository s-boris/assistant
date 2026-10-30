"""Every tool suite as one pytest test, id = the suite's name: tests/legacy/<suite>/run.sh run through the CI
driver's suite runner (ops/assistant.py _ci_legacy: the script staged into a bin overlay with the tree's lib/
modules and put in its *_BIN seam, a temp HOME and TMPDIR each, the network guard, 240 s each, side by side with
tests/legacy/slow.txt first, real-home suites one at a time). The first test of a session runs every suite the
session selected, at once; each test then reports its own suite: red with the suite's FAIL lines and stderr tail.

Which suites run: ASSISTANT_CI_SUITES, a comma list or "all", set by `assistant ci` from its impact selection
(legacy_select) and its side (host-bound suites here, the rest on the hub runner); "" = none (no test). Unset: the
suite tests skip (a plain `pytest` run, or an older CI driver that runs the suites in a step of its own).
ASSISTANT_CI_SUITES_OUT: the results merged into this JSON file ({"suites": {suite: {ok, detail, src, secs, ...}}})
for the driver's report and main's record. ASSISTANT_CI_SUITES_DIR: where the run dirs go (default TMPDIR).
By hand: `ASSISTANT_CI_SUITES=kk,recall .venv-3.13/bin/python -m pytest tests/test_suites.py`.

All suites become native pytest; the shell form is temporary (tests/legacy/README.md)."""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import shutil
import tempfile
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENV, OUT, DIR = "ASSISTANT_CI_SUITES", "ASSISTANT_CI_SUITES_OUT", "ASSISTANT_CI_SUITES_DIR"


def _tool():
    spec = importlib.util.spec_from_file_location("assistant_ci_suites", ROOT / "ops" / "assistant.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


with (ROOT / "ops" / "deploy" / "manifest.toml").open("rb") as _f:
    SUITES = [e["suite"] for e in tomllib.load(_f).get("file", []) if e.get("suite")]
_RAW = os.environ.get(ENV)
WANTED = None if _RAW is None else {s.strip() for s in _RAW.split(",") if s.strip()}
if WANTED is not None and "all" in WANTED:
    WANTED = set(SUITES)
# a name no manifest entry has is a test too: it fails ("no such suite") instead of vanishing
PARAMS = SUITES if WANTED is None else [s for s in SUITES if s in WANTED] + sorted(WANTED - set(SUITES))


def _merge(p: Path, res: dict) -> None:
    """res into p's {"suites": {...}} (a retry's results replace the first run's)."""
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    d.setdefault("suites", {}).update(res)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(d), encoding="utf-8")
    os.replace(tmp, p)


@pytest.fixture(scope="module")
def results(request) -> dict:
    """Every suite this session selected (after -k, deselection, a retry's ids), run once, side by side."""
    names = {
        it.callspec.params["suite"]
        for it in request.session.items
        if getattr(it, "module", None) is request.module and hasattr(it, "callspec")
    }
    tool = _tool()
    # In CI: the run dir itself, as the driver's own step used it (<run dir>/legacy-<suite>): a deeper path breaks
    # suites whose unix sockets live there (Chrome's, 108 bytes at most); only what this run made goes afterwards.
    base = os.environ.get(DIR)
    work = Path(base) if base else Path(tempfile.mkdtemp(prefix="s"))
    mine = [work / "overlay", *(work / f"legacy-{n}" for n in names)]
    res: dict = {}
    try:
        with pytest.MonkeyPatch.context() as mp:
            # the suites start from the driver's environment, not pytest's
            for k in [k for k in os.environ if k.startswith("PYTEST_")] + ["ASSISTANT_CI_WORKER_DIRS", ENV, OUT, DIR]:
                mp.delenv(k, raising=False)
            tool._ci_legacy(ROOT, None, work, only=names, results=res)
    finally:
        for d in mine if base else [work]:
            with contextlib.suppress(Exception):
                tool._ci_reap(d)
            shutil.rmtree(d, ignore_errors=True)
    if os.environ.get(OUT):
        _merge(Path(os.environ[OUT]), res)
    return res


if PARAMS:

    @pytest.mark.skipif(WANTED is None, reason=f"tool suites run when {ENV} names them (`assistant ci` sets it)")
    @pytest.mark.parametrize("suite", PARAMS)
    def test_suite(suite: str, results: dict) -> None:
        r = results.get(suite)
        if r is None:
            pytest.fail(f"{suite}: no result (no manifest entry has suite = {suite!r})", pytrace=False)
        if not r.get("ok"):
            lines = [str(r.get("detail") or "red"), *(r.get("fails") or []), str(r.get("tail") or "")]
            pytest.fail("\n".join(x for x in lines if x), pytrace=False)


def test_shell_form_suites_are_counted() -> None:
    """The count `assistant ci` prints ("N still in shell form") and land rows carry (shell_suites): every manifest
    suite and every tests/legacy/<suite>/ with a run.sh or run.py, until it is native pytest. Not a gate."""
    names = _tool().shell_suites(ROOT)
    assert set(SUITES) <= set(names)
    legacy = ROOT / "tests" / "legacy"
    assert all(n in SUITES or (legacy / n / "run.sh").is_file() or (legacy / n / "run.py").is_file() for n in names)
