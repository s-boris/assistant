"""hubkit.citool: the CI toolchain of the hub's ci container. Stdlib only; runs on the brain side.

The ci container is a plain python:3.13 slim image with no network: what `assistant ci` needs comes from here, built
once per lock and shared by releases (content-addressed, under releases/toolchains/<id>, which the containers mount
read-only; the loader ignores the folder, the release names it and its manifest sha256 in release.json):
  site/   the repo's dev group for CPython 3.13 (uv export --locked + uv pip install --target): pytest, ruff,
          basedpyright (+ its node wheel), jsonschema, pyyaml, FastMCP, Pydantic ...; plus EXTRA (pinned wheels the
          legacy suites import: Pillow for _admindesk, reportlab for divelog's PDF export)
  bin/    ruff, basedpyright (#!/usr/bin/env python3), git and jq (this container's binaries) · lib/ their libraries
          beyond glibc (libpcre2, libjq, libonig: none the runner's image has)
  share/git-core/templates, libexec/git-core (empty: the builtins need nothing else)
  bin/bun         this container's bun (static apart from glibc), when it has one
  bun-deps/<sha>/node_modules   adapters/tg's packages (bun install --frozen-lockfile here, the runner has no
          network) keyed by the first 16 hex of bun.lock's sha256; the CI driver's bun step copies them in (_ci_bun)
  pg/usr/lib/postgresql/<major>/{bin,lib}, pg/usr/share/postgresql/<major>   this container's Postgres in the
          Debian layout, so the binaries find their share and lib folders relative to themselves (the runner's image
          has none); pg/lib = the shared libraries they need beyond glibc, for LD_LIBRARY_PATH of the Postgres
          processes only (tests/hubstore/hubfix.py pg_env: never the runner's own Python, whose libssl differs),
          and libnss_wrapper.so (initdb refuses a uid without a passwd entry, like the runner's)
  hub-vendor/     the pinned hub wheels (ops/hub/vendor.toml) for the hub-store tests (HUB_VENDOR_CACHE)
  manifest.json   {id, files: {path: sha256}, pg, bun}: the runner checks every file before its first job

build(src) -> {"toolchain": id, "manifest_sha256": sha} (reused when that id is complete); off with
HUB_CI_TOOLCHAIN=off (and under ASSISTANT_TEST=1 unless HUB_CI_TOOLCHAIN=on). id = sha256 of the lock inputs,
git's, Postgres' and bun's versions. Postgres and bun are optional (missing here: the runner skips their tests).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from core import paths

from . import HubError, releases_dir, vendor

VERSION = "5"
KEEP = 2
INPUTS = ("uv.lock", "pyproject.toml", "packages/tools/pyproject.toml", "ops/hub/vendor.toml", "adapters/tg/bun.lock")
BUN_DIR = ("adapters", "tg")  # the bun package whose node_modules the toolchain vendors (bun-deps/)
GIT = "/usr/bin/git"
TOOLS = ("/usr/bin/jq",)  # more binaries the legacy suites call (optional: skipped when missing here)
# wheels beyond the dev group, pinned with their dependencies (installed --no-deps like the dev group): what the
# legacy suites' scripts import in-process (the brain has them in its system Python or a venv under ~/opt)
EXTRA = ("pillow==12.3.0", "reportlab==5.0.1", "charset-normalizer==3.5.2")
TEMPLATES = "/usr/share/git-core/templates"
GLIBC = re.compile(r"/(libc|libm|libpthread|libdl|librt|libresolv|libutil|libanl|libnsl|libnss_\w+|ld-linux|libz)\.")
PG_LIB = "/usr/lib/postgresql"
PG_SHARE = "/usr/share/postgresql"
NSS_WRAPPER = "/usr/lib/x86_64-linux-gnu/libnss_wrapper.so"  # libnss-wrapper (~/boot.sh): initdb needs a passwd entry


def pg_home() -> Path | None:
    """The newest /usr/lib/postgresql/<major> with an initdb (HUB_CI_PG_LIB overrides the parent, for tests)."""
    base = Path(os.environ.get("HUB_CI_PG_LIB") or PG_LIB)
    got = sorted(
        (d for d in base.glob("*") if d.name.isdigit() and (d / "bin" / "initdb").is_file()), key=lambda d: int(d.name)
    )
    return got[-1] if got else None


def bun_bin() -> str | None:
    for c in (os.environ.get("HUB_CI_BUN", ""), shutil.which("bun") or "", str(paths.real_home() / ".bun/bin/bun")):
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return os.path.realpath(c)
    return None


def _version(cmd: list[str]) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return "-"
    return r.stdout.strip() or "-"


def wanted() -> bool:
    v = os.environ.get("HUB_CI_TOOLCHAIN", "")
    if v in ("off", "0"):
        return False
    return v in ("on", "1") or not paths.test_mode()


def tc_id(src: Path) -> str:
    h = hashlib.sha256(f"citool {VERSION}\n".encode())
    h.update(" ".join(EXTRA).encode() + b"\0")
    for rel in INPUTS:
        p = src / rel
        h.update(rel.encode() + b"\0" + (p.read_bytes() if p.is_file() else b"-") + b"\0")
    h.update(_git_version().encode())
    pg, bun = pg_home(), bun_bin()
    h.update(b"\0pg " + (_version([str(pg / "bin" / "postgres"), "--version"]) if pg else "-").encode())
    h.update(b"\0bun " + (_version([bun, "--version"]) if bun else "-").encode())
    return h.hexdigest()[:12]


def _git_version() -> str:
    return _version([GIT, "--version"])


def root() -> Path:
    return releases_dir("toolchains")


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _uv() -> str:
    for c in (shutil.which("uv"), str(paths.real_home() / ".local/bin/uv")):
        if c and os.access(c, os.X_OK):
            return c
    raise HubError("uv missing (ops/ci/install-uv.sh): the CI toolchain can't be built")


def _run(cmd: list[str], cwd: Path | None = None, timeout: float = 900) -> str:
    env = dict(os.environ, UV_NO_PROGRESS="1")
    env.pop("VIRTUAL_ENV", None)
    r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise HubError(f"{Path(cmd[0]).name} {cmd[1]}: {(r.stderr.strip().splitlines() or ['?'])[-1][:200]}")
    return r.stdout


def _libs(binary: str) -> list[str]:
    """Shared libraries of binary outside the libc family (python:slim has zlib and glibc, not pcre2; glibc's own
    parts must come from the runner's glibc, never this container's older one)."""
    out = subprocess.run(["ldd", binary], capture_output=True, text=True).stdout
    keep = []
    for ln in out.splitlines():
        if "not found" in ln:  # an optional dependency of a library nobody loads here (ldd lists it anyway)
            continue
        m = re.search(r"=>\s+(/\S+)", ln)
        if m and not GLIBC.search(m.group(1)):
            keep.append(m.group(1))
    return keep


def _verify(d: Path) -> str | None:
    try:
        man = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        for rel, want in man["files"].items():
            p = d / rel
            if p.is_symlink() or not p.is_file() or _sha(p) != want:
                return f"{rel}: missing or changed"
    except (OSError, ValueError, KeyError) as e:
        return f"manifest: {type(e).__name__}"
    return None


def build(src: Path) -> dict | None:
    if not wanted() or not (src / "uv.lock").is_file():
        return None
    tid = tc_id(src)
    base = root()
    d = base / tid
    if d.is_dir() and _verify(d) is None:
        return {"toolchain": tid, "manifest_sha256": _sha(d / "manifest.json")}
    base.mkdir(parents=True, exist_ok=True)
    stage = base / f".stage-{tid}-{os.getpid()}"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir()
    try:
        uv = _uv()
        req = stage / "requirements.txt"
        _run(
            [
                uv,
                "export",
                "--locked",
                "--only-group",
                "dev",
                "--no-hashes",
                "--no-emit-workspace",
                "--no-header",
                "--format",
                "requirements-txt",
                "-o",
                str(req),
            ],
            cwd=src,
        )
        _run(
            [
                uv,
                "pip",
                "install",
                "--quiet",
                "--target",
                str(stage / "site"),
                "--python-version",
                "3.13",
                "--python-platform",
                "x86_64-manylinux_2_28",
                "--only-binary",
                ":all:",
                "--no-deps",
                "-r",
                str(req),
            ],
            cwd=src,
        )
        req.unlink()
        _run(
            [uv, "pip", "install", "--quiet", "--target", str(stage / "site"), "--python-version", "3.13",
             "--python-platform", "x86_64-manylinux_2_28", "--only-binary", ":all:", "--no-deps", *EXTRA],
            cwd=src,
        )  # fmt: skip
        sbin, binp = stage / "site" / "bin", stage / "bin"
        binp.mkdir()
        if sbin.is_dir():
            for f in sorted(sbin.iterdir()):
                if not f.is_file():
                    continue
                data = f.read_bytes()
                if data.startswith(b"#!") and b"python" in data.split(b"\n", 1)[0]:
                    data = b"#!/usr/bin/env python3\n" + data.split(b"\n", 1)[1]
                (binp / f.name).write_bytes(data)
                os.chmod(binp / f.name, 0o755)
            shutil.rmtree(sbin)
        for need in ("ruff", "basedpyright"):
            if not (binp / need).is_file():
                raise HubError(f"toolchain: no {need} after the install")
        shutil.copy2(GIT, binp / "git")
        (stage / "lib").mkdir()
        for lib in _libs(GIT):
            shutil.copy2(os.path.realpath(lib), stage / "lib" / os.path.basename(lib))
        for tool in (t for t in TOOLS if os.path.isfile(t)):
            shutil.copy2(tool, binp / os.path.basename(tool))
            for lib in _libs(tool):
                shutil.copy2(os.path.realpath(lib), stage / "lib" / os.path.basename(lib))
        (stage / "libexec" / "git-core").mkdir(parents=True)
        (stage / "libexec" / "git-core" / "README").write_text("git builtins only (H12 CI toolchain)\n")
        if os.path.isdir(TEMPLATES):
            shutil.copytree(TEMPLATES, stage / "share" / "git-core" / "templates", symlinks=False)
        bun = bun_bin()
        if bun:
            shutil.copy2(bun, binp / "bun")
            _add_bun_deps(src, stage, bun)
        pg = _add_pg(stage)
        _add_wheels(src, stage / "hub-vendor")
        files = {}
        for dp, _dns, fns in os.walk(stage):
            os.chmod(dp, 0o755)
            for fn in fns:
                p = Path(dp) / fn
                if p.is_symlink():
                    p.unlink()
                    continue
                os.chmod(p, 0o755 if os.access(p, os.X_OK) else 0o644)
                files[str(p.relative_to(stage))] = _sha(p)
        man = {"id": tid, "version": VERSION, "python": "3.13", "built": round(time.time(), 3), "files": files,
               "pg": pg, "bun": _version([bun, "--version"]) if bun else None}  # fmt: skip
        (stage / "manifest.json").write_text(json.dumps(man, sort_keys=True, indent=0) + "\n", encoding="utf-8")
        os.chmod(stage / "manifest.json", 0o644)
        if d.exists():
            shutil.rmtree(d)
        os.replace(stage, d)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    prune(keep={tid})
    return {"toolchain": tid, "manifest_sha256": _sha(d / "manifest.json")}


def bun_key(lock: Path) -> str:
    return hashlib.sha256(lock.read_bytes()).hexdigest()[:16]


def _add_bun_deps(src: Path, stage: Path, bun: str) -> None:
    """adapters/tg's node_modules from its frozen lock -> stage/bun-deps/<bun_key>/node_modules (links dropped by the
    manifest walk: the tests import packages, never .bin)."""
    pkg = src.joinpath(*BUN_DIR)
    lock = pkg / "bun.lock"
    if not (lock.is_file() and (pkg / "package.json").is_file()):
        return
    d = stage / "bun-deps" / bun_key(lock)
    d.mkdir(parents=True)
    for n in ("package.json", "bun.lock"):
        shutil.copy2(pkg / n, d / n)
    _run([bun, "install", "--frozen-lockfile", "--ignore-scripts"], cwd=d)
    for n in ("package.json", "bun.lock"):
        (d / n).unlink()
    if not (d / "node_modules").is_dir():
        raise HubError("toolchain: bun install left no node_modules")


def _add_pg(stage: Path) -> str | None:
    """Postgres' bin + lib (pkglib) + share in their Debian layout under stage/pg, the libraries they load beyond
    glibc in stage/pg/lib; -> the major version, None when this container has no Postgres. Links are left out (the
    manifest holds regular files only): the tests run initdb, pg_ctl and postgres, never a link name. No LLVM JIT
    (it would add libLLVM's 100 MB; Postgres runs without JIT when llvmjit.so is missing)."""
    home = pg_home()
    if home is None:
        return None
    major = home.name
    root = stage / "pg"
    for sub in ("bin", "lib"):
        if (home / sub).is_dir():
            shutil.copytree(
                home / sub,
                root / "usr/lib/postgresql" / major / sub,
                symlinks=True,
                ignore=shutil.ignore_patterns("llvmjit*", "bitcode"),
            )  # JIT: off without it (+libLLVM)
    share = Path(os.environ.get("HUB_CI_PG_SHARE") or PG_SHARE) / major
    if share.is_dir():
        shutil.copytree(share, root / "usr/share/postgresql" / major, symlinks=True)
    libs = root / "lib"
    libs.mkdir(parents=True, exist_ok=True)
    for dp, _dns, fns in os.walk(root / "usr/lib/postgresql" / major):
        for fn in fns:
            p = os.path.join(dp, fn)
            if os.path.islink(p) or not (os.access(p, os.X_OK) or fn.endswith(".so")):
                continue
            for lib in _libs(p):
                dst = libs / os.path.basename(lib)
                if not dst.exists():
                    shutil.copy2(os.path.realpath(lib), dst)
    nss = os.environ.get("HUB_CI_NSS_WRAPPER") or NSS_WRAPPER
    if os.path.isfile(nss):  # the runner's uid has no passwd entry: the tests preload it for initdb (hubfix.pg_env)
        shutil.copy2(nss, libs / "libnss_wrapper.so")
    return major


def _add_wheels(src: Path, dest: Path) -> None:
    """The pinned hub wheels from the cache the release build just filled (none cached: none here)."""
    for pin in vendor.pins(src):
        whl = vendor.cache_dir() / pin["file"]
        if whl.is_file() and _sha(whl) == pin["sha256"]:
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(whl, dest / pin["file"])


def prune(keep: set[str], n: int = KEEP) -> list[str]:
    base = root()
    if not base.is_dir():
        return []
    have = sorted(
        ((d.stat().st_mtime, d.name) for d in base.iterdir() if d.is_dir() and re.fullmatch(r"[0-9a-f]{12}", d.name)),
        reverse=True,
    )
    keep = set(keep) | {name for _, name in have[:n]}
    gone = []
    for _, name in have:
        if name not in keep:
            shutil.rmtree(base / name, ignore_errors=True)
            gone.append(name)
    return gone
