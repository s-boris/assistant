"""The hub release's vendored wheels (ops/hub/vendor.toml) stay in step with packages/tools/requirements.lock,
offline: every package the lock installs on the hub's CPython 3.13 has exactly one pinned wheel of that version,
whose sha256 the lock lists and whose tags fit the hub (cp313 / abi3 / pure, manylinux x86_64). Regenerate with
`uv run python ops/hub/vendor_gen.py` after a lock bump."""

from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path

from packaging.utils import canonicalize_name

REPO = Path(__file__).resolve().parents[2]


def _gen():
    spec = importlib.util.spec_from_file_location("vendor_gen", REPO / "ops/hub/vendor_gen.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_vendor_pins_match_the_lock():
    gen = _gen()
    lock = {canonicalize_name(e["name"]): e for e in gen.all_entries()}  # the gateway's lock + the hub's
    pins_psycopg = {w["name"]: w["version"] for w in tomllib.loads((REPO / "ops/hub/vendor.toml").read_text())["wheel"]
                    if w["name"].startswith("psycopg")}  # fmt: skip
    for n in ("psycopg", "psycopg-binary"):  # hand-pinned at the top of vendor.toml; the hub lock must agree
        assert lock.pop(n)["version"] == pins_psycopg[n], n
    pins = tomllib.loads((REPO / "ops/hub/vendor.toml").read_text())["wheel"]
    by = {}
    for w in pins:
        by.setdefault(canonicalize_name(w["name"]), []).append(w)
    gateway = {n: ws for n, ws in by.items() if not n.startswith("psycopg")}
    assert set(gateway) == set(lock), sorted(set(gateway) ^ set(lock))
    for name, ws in gateway.items():
        assert len(ws) == 1, name
        w, e = ws[0], lock[name]
        assert w["version"] == e["version"], name
        assert w["sha256"] in e["hashes"], name
        assert gen.rank(w["file"]) is not None, w["file"]
        assert w["url"].startswith("https://files.pythonhosted.org/") and w["url"].endswith("/" + w["file"])
    assert {"fastmcp", "cedarpy", "pydantic-core", "uvicorn", "dbos", "sqlalchemy", "greenlet"} <= set(gateway)
