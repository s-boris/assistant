"""packages/hubkit/citool.py: the CI toolchain's Postgres (Debian layout, libraries, nss_wrapper), bun and the pinned
hub wheels, against fake trees (no uv, no network, no real Postgres)."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from hubkit import citool


def _exe(p: Path, text: str = "#!/bin/sh\necho fake\n") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    os.chmod(p, 0o755)
    return p


def _fake_pg(tmp_path: Path, monkeypatch) -> Path:
    lib, share = tmp_path / "usrlib", tmp_path / "usrshare"
    for major in ("13", "15"):
        _exe(lib / major / "bin" / "initdb")
        _exe(lib / major / "bin" / "postgres", f"#!/bin/sh\necho 'postgres (PostgreSQL) {major}.1'\n")
        (lib / major / "lib").mkdir(parents=True)
        (lib / major / "lib" / "plpgsql.so").write_text("so")
        (lib / major / "lib" / "llvmjit.so").write_text("big")
        (lib / major / "lib" / "bitcode").mkdir()
        (share / major / "timezonesets").mkdir(parents=True)
        (share / major / "postgres.bki").write_text("bki")
    (lib / "15" / "bin" / "postmaster").symlink_to("postgres")
    (lib / "notaversion" / "bin").mkdir(parents=True)
    nss = tmp_path / "libnss_wrapper.so"
    nss.write_text("nss")
    monkeypatch.setenv("HUB_CI_PG_LIB", str(lib))
    monkeypatch.setenv("HUB_CI_PG_SHARE", str(share))
    monkeypatch.setenv("HUB_CI_NSS_WRAPPER", str(nss))
    monkeypatch.setattr(citool, "_libs", lambda p: [])  # fake binaries: no ldd output worth copying
    return lib


def test_pg_copy_keeps_the_debian_layout_without_jit(tmp_path, monkeypatch):
    lib = _fake_pg(tmp_path, monkeypatch)
    assert citool.pg_home() == lib / "15", "the newest major with an initdb"
    stage = tmp_path / "stage"
    assert citool._add_pg(stage) == "15"
    pg = stage / "pg"
    assert (pg / "usr/lib/postgresql/15/bin/initdb").is_file()
    assert (pg / "usr/lib/postgresql/15/lib/plpgsql.so").is_file()
    assert (pg / "usr/share/postgresql/15/postgres.bki").is_file(), "share beside lib: initdb finds it relocated"
    assert not (pg / "usr/lib/postgresql/15/lib/llvmjit.so").exists()
    assert not (pg / "usr/lib/postgresql/15/lib/bitcode").exists()
    assert (pg / "lib" / "libnss_wrapper.so").read_text() == "nss"
    assert not (pg / "usr/lib/postgresql/13").exists()


def test_no_postgres_no_pg_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_CI_PG_LIB", str(tmp_path / "none"))
    assert citool.pg_home() is None
    assert citool._add_pg(tmp_path / "stage") is None and not (tmp_path / "stage" / "pg").exists()


def test_libs_skip_glibc_parts_and_missing_ones(monkeypatch):
    out = (
        "\tlibc.so.6 => /lib/x86_64-linux-gnu/libc.so.6 (0x1)\n"
        "\tlibresolv.so.2 => /lib/x86_64-linux-gnu/libresolv.so.2 (0x1)\n"
        "\tlibicuuc.so.72 => /lib/x86_64-linux-gnu/libicuuc.so.72 (0x1)\n"
        "\tlibfoo.so.1 => not found\n"
    )

    class R:
        stdout = out

    monkeypatch.setattr(citool.subprocess, "run", lambda *a, **k: R())
    assert citool._libs("/x") == ["/lib/x86_64-linux-gnu/libicuuc.so.72"]


def test_wheels_copied_only_when_cached_and_matching(tmp_path, monkeypatch):
    src, cache = tmp_path / "src", tmp_path / "cache"
    (src / "ops/hub").mkdir(parents=True)
    cache.mkdir()
    (cache / "good-1-py3-none-any.whl").write_bytes(b"good")
    (cache / "bad-1-py3-none-any.whl").write_bytes(b"tampered")
    sha = hashlib.sha256(b"good").hexdigest()
    pins = "".join(
        f'[[wheel]]\nname = "{n}"\nfile = "{n}-1-py3-none-any.whl"\n'
        f'url = "https://example.invalid/{n}"\nsha256 = "{s}"\n'
        for n, s in (("good", sha), ("bad", sha), ("gone", sha))
    )
    (src / "ops/hub/vendor.toml").write_text(pins)
    monkeypatch.setenv("HUB_VENDOR_CACHE", str(cache))
    citool._add_wheels(src, tmp_path / "out")
    assert sorted(os.listdir(tmp_path / "out")) == ["good-1-py3-none-any.whl"]


def test_toolchain_id_follows_postgres_and_bun(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    (src / "uv.lock").write_text("lock")
    monkeypatch.setenv("HUB_CI_PG_LIB", str(tmp_path / "none"))
    monkeypatch.setenv("HUB_CI_BUN", str(_exe(tmp_path / "bun1", "#!/bin/sh\necho 1.4.4\n")))
    monkeypatch.setattr(citool.shutil, "which", lambda n: None)
    a = citool.tc_id(src)
    monkeypatch.setenv("HUB_CI_BUN", str(_exe(tmp_path / "bun2", "#!/bin/sh\necho 1.4.5\n")))
    b = citool.tc_id(src)
    _fake_pg(tmp_path, monkeypatch)
    c = citool.tc_id(src)
    (src / "ops/hub").mkdir(parents=True)
    (src / "ops/hub/vendor.toml").write_text("# pins\n")
    d = citool.tc_id(src)
    assert len({a, b, c, d}) == 4
