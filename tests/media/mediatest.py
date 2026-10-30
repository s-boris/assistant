"""Helpers for the `media` tests (conftest.py puts this dir on sys.path): the repo's scripts/media staged in a bin
dir whose lib/ holds the repo's botlib, assistant_profile and core (nothing from the live ~/bin), a temp HOME per
test, the synthetic profile, and the local fake servers (fake_*.py, *_fx.py next to this file) on 127.0.0.1 with a
free port each. No network."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
PROFILE = REPO / "tests" / "legacy" / "profile" / "fixtures" / "profile.json"
# the fakes run under the python3 on PATH, the tool's own interpreter (#!/usr/bin/env python3): some need Pillow
PYTHON3 = shutil.which("python3") or sys.executable
PY = shlex.quote(PYTHON3)
KEYS = ("SEERR-KEY-Zq81", "RADARR-KEY-Xw42", "SONARR-KEY-Vy73")
WRITE_SQL = re.compile(r"\b(insert|update|delete|drop|create|alter|replace into|pragma|vacuum)\b", re.I)


def stage(d: Path) -> str:
    """bin/media + lib/ (botlib, assistant_profile, core) from the repo, as deployed."""
    b = d / "bin"
    (b / "lib").mkdir(parents=True)
    shutil.copy2(REPO / "scripts" / "media", b / "media")
    (b / "lib" / "core").symlink_to(REPO / "core")
    (b / "lib" / "botlib.py").symlink_to(REPO / "scripts" / "lib" / "botlib.py")
    (b / "lib" / "assistant_profile.py").symlink_to(REPO / "scripts" / "lib" / "legacy" / "assistant_profile.py")
    return str(b / "media")


def dead_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def fx(name: str, *args) -> str:
    """Shell command line for a fixture script (as MEDIA_EMBY_SQL and friends take it)."""
    return " ".join([PY, "-I", shlex.quote(str(HERE / name)), *(shlex.quote(str(a)) for a in args)])


def line(text: str, n: int) -> str:
    """`sed -n Np`: line n (1-based) or ''."""
    lines = text.split("\n")
    return lines[n - 1] if 0 < n <= len(lines) else ""


class Media:
    """One test's world: tmp dir T, HOME, env, staged tool, fake servers; m() runs the tool like the shell's m()."""

    def __init__(self, t: Path):
        self.t = t
        self.tool = stage(t)
        home = t / "home"
        home.mkdir()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("MEDIA_", "ASSISTANT_"))}
        self.env.update(
            HOME=str(home),
            ASSISTANT_TEST="1",
            ASSISTANT_PROFILE=str(PROFILE),
            MEDIA_PLAY_CONFIG="/nonexistent/play.json",  # exact button rows: no play.json
        )
        self.procs: list[subprocess.Popen] = []
        self.all_out: list[str] = []
        self.out, self.err, self.rc = "", "", 0
        self.log_path = t / "server.log"
        self.err_path = t / "server.err"

    # ------------------------------------------------------------ servers and fixtures
    def serve(
        self,
        script: str,
        *args,
        pre: tuple = (),
        log: Path | None = None,
        err: Path | None = None,
        env: dict | None = None,
    ) -> str:
        """Start a fake server (`script [PRE...] PORT_FILE LOG ARGS...`); returns its base URL."""
        port = self.t / f"port-{len(self.procs)}"
        port.unlink(missing_ok=True)
        err_f = open(err or self.err_path, "a")  # noqa: SIM115 (closed with the process)
        p = subprocess.Popen(
            [PYTHON3, "-I", str(HERE / script), *map(str, pre), str(port), str(log or self.log_path), *map(str, args)],
            stderr=err_f,
            stdout=subprocess.DEVNULL,
            env=dict(self.env, **(env or {})),
        )
        err_f.close()
        self.procs.append(p)
        for _ in range(100):
            if port.exists() and port.read_text().strip():
                return f"http://127.0.0.1:{port.read_text().strip()}"
            time.sleep(0.05)
        raise AssertionError(f"{script} did not start")

    def py(self, script: str, *args, env: dict | None = None) -> str:
        r = subprocess.run(
            [PYTHON3, "-I", str(HERE / script), *map(str, args)],
            capture_output=True,
            text=True,
            timeout=60,
            env=dict(self.env, **(env or {})),
        )
        assert r.returncode == 0, r.stdout + r.stderr
        return r.stdout

    def stop(self):
        """Stop the servers started so far (a restart of a stateful fake)."""
        for p in self.procs:
            p.kill()
            p.wait()
        self.procs.clear()

    def close(self):
        for p in self.procs:
            p.kill()
            p.wait()

    # ------------------------------------------------------------ runs
    def m(self, *args, env: dict | None = None, timeout: int = 30, merge: bool = True) -> str:
        """Run the tool; out = stdout (+ stderr when merge), trailing newlines cut, like the shell's $(...)."""
        e = dict(self.env)
        for k, v in (env or {}).items():
            if v is None:
                e.pop(k, None)
            else:
                e[k] = str(v)
        try:
            r = subprocess.run(
                [self.tool, *map(str, args)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT if merge else subprocess.PIPE,
                text=True,
                env=e,
                timeout=timeout,
            )
            self.out, self.rc = r.stdout.rstrip("\n"), r.returncode
            self.err = "" if merge else r.stderr
            if not merge:
                self.all_out.append(r.stderr)
        except subprocess.TimeoutExpired as x:
            self.out, self.rc = (x.output or b"").decode(errors="replace").rstrip("\n"), 124
        self.all_out.append(self.out)
        return self.out

    @property
    def j(self):
        return json.loads(self.out)

    def line(self, n: int) -> str:
        return line(self.out, n)

    def lines(self, a: int, b: int | None = None) -> str:
        return "\n".join(self.out.split("\n")[a - 1 : b if b is not None else a])

    # ------------------------------------------------------------ logs
    def log(self, path: Path | None = None) -> str:
        p = path or self.log_path
        return p.read_text() if p.exists() else ""

    def logn(self, pat: str, path: Path | None = None) -> int:
        return sum(pat in ln for ln in self.log(path).splitlines())  # grep -c

    def reset_log(self):
        with open(self.t / "all.log", "a") as f:
            f.write(self.log())
        self.log_path.write_text("")

    def everything(self) -> str:
        return "\n".join(self.all_out)

    def safety(self, keys=KEYS, logs: tuple[Path, ...] | None = None, get_only: bool = True):
        """GET only, no key in any output, no traceback, the fake server's stderr empty."""
        for p in (logs or (self.t / "all.log", self.log_path)) if get_only else ():
            bad = [ln for ln in self.log(p).splitlines() if not ln.startswith("GET ")]
            assert bad == [], bad
        allout = self.everything()
        for k in keys:
            assert k not in allout
        assert "Traceback" not in allout
        assert self.log(self.err_path) == ""

    def sql_readonly(self, sql_log: Path):
        assert not WRITE_SQL.search(sql_log.read_text() if sql_log.exists() else "")


def mkcfg(path: Path, seerr: str, radarr: str, sonarr: str, **extra) -> Path:
    d = {
        "seerr": {"url": seerr, "key": KEYS[0]},
        "radarr": {"url": radarr, "key": KEYS[1]},
        "sonarr": {"url": sonarr, "key": KEYS[2]},
    }
    d.update(extra)
    path.write_text(json.dumps(d) + "\n")
    return path
