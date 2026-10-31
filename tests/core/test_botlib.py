"""botlib (scripts/lib/botlib.py): the watchers' helper shim that delegates to the harness core next to it.

Isolated: the repo's botlib is staged in a lib dir with the repo's core linked beside it (as deployed in ~/bin/lib;
nothing from the live ~/bin), imported in a subprocess per test so each test gets a fresh module. Fake tg and
unraid-ssh are tiny sh scripts in tmp_path."""

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "scripts" / "lib" / "botlib.py"


def sh(d: Path, name: str, body: str) -> str:
    p = d / name
    p.write_text("#!/bin/sh\n" + body + "\n")
    p.chmod(0o755)
    return str(p)


@pytest.fixture
def lib(tmp_path: Path) -> Path:
    """lib/botlib.py + lib/core (the repo's), as in a staged overlay."""
    d = tmp_path / "lib"
    d.mkdir()
    shutil.copy2(SRC, d / "botlib.py")
    (d / "core").symlink_to(REPO / "core")
    return d


def run(lib: Path, tmp: Path, body: str) -> str:
    """Run body with the staged botlib imported (D = tmp as a Path); assert exit 0, return stdout."""
    code = (
        "import os, stat, sys\nfrom pathlib import Path\n"
        f"sys.path.insert(0, {str(lib)!r})\nimport botlib\n"
        f"assert os.path.realpath(botlib.__file__) == {str((lib / 'botlib.py').resolve())!r}, botlib.__file__\n"
        f"D = Path({str(tmp)!r})\n" + textwrap.dedent(body) + "\nprint('ok')\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "TG_SOFT"}
    env["HOME"] = str(tmp / "home")
    r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.rstrip().endswith("ok"), r.stdout
    return r.stdout


def test_atomic_write_creates_dirs_and_leaves_no_tmp(lib, tmp_path):
    run(
        lib,
        tmp_path,
        """
        p = D / "a" / "b" / "x.txt"
        botlib.atomic_write(p, "hé\\n")
        botlib.atomic_write(str(p), "two\\n")
        assert p.read_text(encoding="utf-8") == "two\\n"
        assert os.listdir(p.parent) == ["x.txt"], os.listdir(p.parent)
    """,
    )


def test_json_roundtrip_and_defaults(lib, tmp_path):
    run(
        lib,
        tmp_path,
        """
        p = D / "s" / "state.json"
        assert botlib.load_json(p, {"d": 1}) == {"d": 1}
        botlib.save_json(p, {"b": 1, "a": [1]}, indent=1, sort_keys=True)
        assert p.read_text() == '{\\n "a": [\\n  1\\n ],\\n "b": 1\\n}', repr(p.read_text())
        assert botlib.load_json(p, None) == {"a": [1], "b": 1}
        p.write_text("{broken")
        assert botlib.load_json(p) is None
    """,
    )


def test_write_inbox_never_overwrites(lib, tmp_path):
    run(
        lib,
        tmp_path,
        """
        import re
        ib = D / "inbox"
        f1 = botlib.write_inbox(ib, "cw", "one")
        f2 = botlib.write_inbox(ib, "cw", "two")
        assert f1 != f2
        assert re.search(r"^cw-\\d+\\.md$", f1.name), f1.name
        assert re.search(r"^cw-\\d+(-\\d+)?\\.md$", f2.name), f2.name
        assert sorted(p.read_text() for p in ib.iterdir()) == ["one", "two"]
    """,
    )


def test_send_tg(lib, tmp_path):
    log = tmp_path / "sent"
    sh(tmp_path, "tg-ok", f'printf "%s" "$1" > {log}')
    sh(tmp_path, "tg-bad", 'echo "boom: no chat" >&2; exit 3')
    sh(tmp_path, "tg-env", f'printf "%s|%s" "${{TG_SOFT:-}}" "$1" > {log}')
    run(
        lib,
        tmp_path,
        """
        log = D / "sent"
        assert botlib.send_tg(str(D / "tg-ok"), "hi there") == (True, "")
        assert log.read_text() == "hi there"
        assert botlib.send_tg(str(D / "tg-bad"), "x") == (False, "boom: no chat")
        os.environ.pop("TG_SOFT", None)
        assert botlib.send_tg(str(D / "tg-env"), "alert")[0] is True
        assert log.read_text() == "|alert", log.read_text()            # default: urgent (no TG_SOFT), argv unchanged
        assert botlib.send_tg(str(D / "tg-env"), "report", soft=True)[0] is True
        assert log.read_text() == "1|report", log.read_text()          # soft: TG_SOFT=1, argv unchanged
        assert "TG_SOFT" not in os.environ                             # the caller's env is left alone
        r = botlib.send_tg(str(D / "missing"), "x")
        assert not r[0]
        assert len(r[1]) <= 200
    """,
    )


def test_lockdown_mode(lib, tmp_path):
    for out in ("verbs", "emulate", "root", "junk"):
        sh(tmp_path, "us-" + out, f"echo {out}")
    run(
        lib,
        tmp_path,
        """
        for out, want in (("verbs", "verbs"), ("emulate", "emulate"), ("root", "root"), ("junk", "root")):
            assert botlib.lockdown_mode(str(D / ("us-" + out))) == want, out
        assert botlib.lockdown_mode(str(D / "missing")) == "root"
    """,
    )


def test_delegates_to_core(lib, tmp_path):
    """The deployed shim finds the harness core next to it (~/bin/lib/core, or lib/core in a staged overlay)."""
    run(
        lib,
        tmp_path,
        """
        assert botlib.CORE
        assert botlib._js.__name__.endswith("jsonstate"), botlib._js.__name__
    """,
    )


def test_private_file_stays_private(lib, tmp_path):
    run(
        lib,
        tmp_path,
        """
        p = D / "state.json"
        botlib.save_json(p, {"a": 1})
        os.chmod(p, 0o600)
        botlib.save_json(p, {"a": 2})
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
        assert botlib.load_json(p) == {"a": 2}
    """,
    )


def test_fallback_without_core(tmp_path):
    """A copy with no core next to it still works (the pre-core bodies)."""
    d = tmp_path / "copy"
    d.mkdir()
    shutil.copy2(SRC, d / "botlib.py")
    run(
        d,
        d,
        """
        import pathlib
        assert not botlib.CORE
        p = D / "s" / "x.json"
        botlib.save_json(p, {"k": [1]}, indent=1)
        assert botlib.load_json(p) == {"k": [1]} and botlib.load_json(p.with_name("no"), 5) == 5
        a = botlib.write_inbox(D / "in", "fb", "one")
        b = botlib.write_inbox(D / "in", "fb", "two")
        assert a != b and a.read_text() == "one" and b.read_text() == "two"
    """,
    )
