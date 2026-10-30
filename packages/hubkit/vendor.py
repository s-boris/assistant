"""hubkit.vendor: the wheels a hub release carries (ops/hub/vendor.toml). Stdlib only.

pins(src)            the [[wheel]] entries of <src>/ops/hub/vendor.toml ([] when the file is missing)
cache_dir()          HUB_VENDOR_CACHE, else ~/.cache/assistant/hub-vendor
fetch(pin)           the cached wheel, downloaded once (core.http) and kept only when its sha256 matches the pin
unpack(src, dest)    every pinned wheel unpacked into dest (the release's vendor/): regular files only, every path
                     one the loader accepts; -> {relative path: {"sha256", "size"}}
"""

from __future__ import annotations

import hashlib
import os
import re
import tomllib
import zipfile
from pathlib import Path
from typing import Any

from core import http, paths

from . import HubError

PINS = "ops/hub/vendor.toml"
# The loader's REL_PATH: what a release may hold.
REL_PATH = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_][A-Za-z0-9_.-]*)*$")
MAX_WHEEL = 64 << 20


def pins(src: Path, pins_file: str = PINS) -> list[dict[str, Any]]:
    p = src / pins_file
    if not p.is_file():
        return []
    got = tomllib.loads(p.read_text(encoding="utf-8")).get("wheel") or []
    for w in got:
        for k in ("name", "file", "url", "sha256"):
            if not isinstance(w.get(k), str) or not w[k]:
                raise HubError(f"{pins_file}: a wheel without {k}")
        if not re.fullmatch(r"[0-9a-f]{64}", w["sha256"]) or "/" in w["file"]:
            raise HubError(f"{pins_file}: {w['name']}: bad sha256 or file name")
    return got


def cache_dir() -> Path:
    v = os.environ.get("HUB_VENDOR_CACHE", "")
    return Path(v) if v else paths.home() / ".cache" / "assistant" / "hub-vendor"


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(pin: dict[str, Any]) -> Path:
    d = cache_dir()
    p = d / pin["file"]
    if p.is_file() and _sha(p) == pin["sha256"]:
        return p
    d.mkdir(parents=True, exist_ok=True)
    try:
        data = http.get_bytes(pin["url"], max_bytes=MAX_WHEEL, timeout=120, retries=2)
    except OSError as e:
        raise HubError(f"vendor: {pin['file']}: download failed ({type(e).__name__})", 3) from None
    if hashlib.sha256(data).hexdigest() != pin["sha256"]:
        raise HubError(f"vendor: {pin['file']}: sha256 differs from {PINS}", 3)
    tmp = d / f".{pin['file']}.{os.getpid()}.tmp"
    tmp.write_bytes(data)
    os.replace(tmp, p)
    return p


def unpack(src: Path, dest: Path, pins_file: str = PINS, prefix: str = "vendor") -> dict[str, dict[str, Any]]:
    files: dict[str, dict[str, Any]] = {}
    for pin in pins(src, pins_file):
        whl = fetch(pin)
        with zipfile.ZipFile(whl) as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                name = info.filename
                parts = name.split("/")
                if ".." not in parts and any(x.startswith(".") for x in parts):
                    continue  # hidden data files (numpy's f2py test fixtures): never imported, not shipped
                if not REL_PATH.match(name) or (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise HubError(f"vendor: {pin['file']}: member {name!r} is not a plain release path")
                rel = f"{prefix}/{name}"
                q = dest / name
                q.parent.mkdir(parents=True, exist_ok=True)
                data = z.read(info)
                q.write_bytes(data)
                os.chmod(q, 0o644)
                files[rel] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
    return files
