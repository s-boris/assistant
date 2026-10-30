"""Regenerate the gateway block of ops/hub/vendor.toml from packages/tools/requirements.lock (v2 Phase 2 H7) and
packages/hub/requirements.lock (H15: DBOS and its deps; both come from the one uv.lock, so versions agree).

    uv run python ops/hub/vendor_gen.py [--check]

The hub image is a plain CPython 3.13 slim; the gateway (tools.hubgw: FastMCP, Pydantic, cedarpy and their deps)
ships in the release's vendor/ like psycopg. For every package the lock installs on the hub's interpreter
(markers evaluated for CPython 3.13 on Linux x86_64) this picks one wheel from PyPI's JSON API: cp313 first, then
abi3, then pure Python (manylinux x86_64, glibc ≤ 2.41 = trixie), and keeps it only when its sha256 is one of the
lock's hashes for that version, so the hub runs exactly what the lock pinned (two-week rule included). The lines
between the BEGIN/END markers are rewritten; everything else (psycopg) stays. --check: exit 1 when the file
would change. Dev only (packaging from the repo venv, network): tests/hub/test_vendor.py checks the result offline.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from packaging.markers import Marker
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from core import http  # noqa: E402  (the repo's own helper: UA, timeouts)

LOCK = ROOT / "packages/tools/requirements.lock"
LOCKS = (LOCK, ROOT / "packages/hub/requirements.lock")
PINS = ROOT / "ops/hub/vendor.toml"
BEGIN = "# BEGIN gateway wheels (ops/hub/vendor_gen.py; do not edit by hand)"
END = "# END gateway wheels"
HUB_ENV = {
    "python_version": "3.13",
    "python_full_version": "3.13.15",
    "platform_machine": "x86_64",
    "sys_platform": "linux",
    "platform_system": "Linux",
    "implementation_name": "cpython",
    "platform_python_implementation": "CPython",
    "os_name": "posix",
}
GLIBC_MAX = (2, 41)


def lock_entries(text: str) -> list[dict]:
    """[{name, version, hashes}] for every lock entry the hub's interpreter installs."""
    out = []
    for block in re.split(r"\n(?=[a-z0-9])", text):
        m = re.match(r"([A-Za-z0-9_.-]+)==([^\s;]+)\s*(?:;\s*([^\\\n]+))?", block)
        if not m:
            continue
        marker = (m[3] or "").strip()
        if marker and not Marker(marker).evaluate(HUB_ENV):
            continue
        out.append({"name": m[1], "version": m[2], "hashes": set(re.findall(r"--hash=sha256:([0-9a-f]{64})", block))})
    return out


def _plat_ok(plat: str) -> bool:
    if plat == "any":
        return True
    for p in plat.split("."):
        m = re.fullmatch(r"manylinux_(\d+)_(\d+)_x86_64", p)
        if m and (int(m[1]), int(m[2])) <= GLIBC_MAX:
            return True
        if p in ("manylinux2014_x86_64", "manylinux2010_x86_64", "manylinux1_x86_64"):
            return True
    return False


def rank(filename: str) -> int | None:
    """Lower is better; None = not installable on the hub."""
    if not filename.endswith(".whl"):
        return None
    parts = filename[:-4].split("-")
    py, abi, plat = parts[-3], parts[-2], parts[-1]
    if not _plat_ok(plat):
        return None
    pys = py.split(".")
    if "cp313" in pys and abi == "cp313":
        return 0
    if abi == "abi3" and any(re.fullmatch(r"cp3(\d+)", x) and int(x[3:]) <= 13 for x in pys):
        return 1
    if abi == "none" and plat == "any" and any(x in ("py3", "py313", "cp313") or x.startswith("py3") for x in pys):
        return 2
    return None


def pick(entry: dict) -> dict:
    files = http.get_json(f"https://pypi.org/pypi/{entry['name']}/{entry['version']}/json", timeout=30)["urls"]
    best = None
    for f in files:
        rk = rank(f["filename"])
        if rk is None or f["digests"]["sha256"] not in entry["hashes"]:
            continue
        if best is None or rk < best[0]:
            best = (rk, f)
    if best is None:
        raise SystemExit(f"{entry['name']}=={entry['version']}: no hub wheel whose sha256 the lock lists")
    f = best[1]
    return {"name": entry["name"], "version": entry["version"], "file": f["filename"], "url": f["url"],
            "sha256": f["digests"]["sha256"]}  # fmt: skip


def render(wheels: list[dict]) -> str:
    lines = [BEGIN]
    for w in wheels:
        lines += ["[[wheel]]", *(f'{k} = "{w[k]}"' for k in ("name", "version", "file", "url", "sha256")), ""]
    lines.append(END)
    return "\n".join(lines) + "\n"


def all_entries() -> list[dict]:
    """Every lock's entries, one per package (the locks come from one uv.lock: a version clash is a bug)."""
    by: dict[str, dict] = {}
    for lock in LOCKS:
        for e in lock_entries(lock.read_text(encoding="utf-8")):
            n = canonicalize_name(e["name"])
            if n in by and by[n]["version"] != e["version"]:
                raise SystemExit(f"{n}: {by[n]['version']} vs {e['version']} in {lock.name}")
            by.setdefault(n, {**e, "hashes": set()})["hashes"] |= e["hashes"]
    return list(by.values())


def main(argv: list[str]) -> int:
    entries = all_entries()
    old = PINS.read_text(encoding="utf-8")
    keep = {canonicalize_name(n) for n in re.findall(r'^name = "([^"]+)"', old.split(BEGIN)[0], re.M)}
    wheels = [pick(e) for e in entries if canonicalize_name(e["name"]) not in keep]
    block = render(sorted(wheels, key=lambda w: canonicalize_name(w["name"])))
    if BEGIN in old:
        head, rest = old.split(BEGIN, 1)
        new = head + block + rest.split(END, 1)[1].lstrip("\n")
    else:
        new = old.rstrip("\n") + "\n\n" + block
    if "--check" in argv:
        return 0 if new == old else 1
    PINS.write_text(new, encoding="utf-8")
    print(f"{len(wheels)} gateway wheels in {PINS.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
