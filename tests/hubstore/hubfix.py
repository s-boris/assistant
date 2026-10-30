"""Test fixtures for the hub store: a throwaway Postgres cluster (initdb, SCRAM, loopback only) and
hubd as a subprocess on Python 3.13 with the pinned wheels unpacked from the vendor cache. Never the real hub, never
a real token: tokens are random per run. Skips (pytest) when Postgres, Python 3.13 or the cached wheels are missing.
"""

from __future__ import annotations

import ctypes
import glob
import json
import os
import pwd
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:  # the manual smoke (__main__) runs without pytest's pythonpath
    sys.path.insert(0, str(REPO))

from core import http as web  # noqa: E402

REAL_HOME = Path(next((e.pw_dir for e in pwd.getpwall() if e.pw_uid == os.getuid()), "/nonexistent-home"))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _tc_pg() -> Path | None:
    """The CI toolchain's Postgres (packages/hubkit/citool.py: pg/usr/lib/postgresql/<major>, in the hub runner)."""
    tc = os.environ.get("ASSISTANT_CI_TOOLCHAIN", "")
    return Path(tc) / "pg" if tc and (Path(tc) / "pg").is_dir() else None


def pg_bin() -> Path | None:
    """The newest Postgres bin folder: the CI toolchain's when the runner has one, else /usr/lib/postgresql/*."""
    tcp = _tc_pg()
    base = str(tcp) + "/usr/lib/postgresql" if tcp else "/usr/lib/postgresql"
    got = sorted(glob.glob(base + "/*/bin/initdb"), key=lambda p: int(Path(p).parents[1].name))
    return Path(got[-1]).parent if got else None


def pg_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """env (default os.environ) for a Postgres process: C.UTF-8, and the toolchain's pg/lib on LD_LIBRARY_PATH (only
    here: the runner's own Python must keep its own libssl)."""
    out = {**(os.environ if env is None else env), "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8"}
    tcp = _tc_pg()
    if tcp:
        out["LD_LIBRARY_PATH"] = os.pathsep.join(x for x in (str(tcp / "lib"), out.get("LD_LIBRARY_PATH", "")) if x)
        out.update(nss_env(tcp / "lib" / "libnss_wrapper.so"))
    return out


def nss_env(lib: Path) -> dict[str, str]:
    """initdb refuses a uid without a passwd entry (the hub runner's): nss_wrapper with a one-line passwd and group
    file in the temp dir gives it one. {} when the uid has an entry or the library is missing."""
    try:
        pwd.getpwuid(os.getuid())
        return {}
    except KeyError:
        pass
    if not lib.is_file():
        return {}
    import tempfile

    d = Path(tempfile.gettempdir()) / f"nss-{os.getuid()}"
    d.mkdir(exist_ok=True)
    (d / "passwd").write_text(f"ci:x:{os.getuid()}:{os.getgid()}:ci:{d}:/bin/false\n")
    (d / "group").write_text(f"ci:x:{os.getgid()}:\n")
    return {"LD_PRELOAD": str(lib), "NSS_WRAPPER_PASSWD": str(d / "passwd"), "NSS_WRAPPER_GROUP": str(d / "group")}


def py313() -> str | None:
    for c in (os.environ.get("HUB_TEST_PYTHON", ""), shutil.which("python3.13") or "",
              str(REAL_HOME / ".local" / "bin" / "python3.13")):  # fmt: skip
        if c and os.access(c, os.X_OK):
            return c
    return None


def vendor_tree(dest: Path) -> Path | None:
    """The pinned wheels (ops/hub/vendor.toml) from the cache, unpacked into dest; None when one is not cached."""
    cache = Path(os.environ.get("HUB_VENDOR_CACHE") or REAL_HOME / ".cache" / "assistant" / "hub-vendor")
    pins = tomllib.loads((REPO / "ops/hub/vendor.toml").read_text()).get("wheel") or []
    for p in pins:
        whl = cache / p["file"]
        if not whl.is_file():
            return None
        with zipfile.ZipFile(whl) as z:
            z.extractall(dest)
    return dest


def missing() -> str | None:
    if pg_bin() is None:
        return "no Postgres binaries (/usr/lib/postgresql/*/bin, nor the CI toolchain's pg/)"
    if py313() is None:
        return "no python3.13"
    cache = Path(os.environ.get("HUB_VENDOR_CACHE") or REAL_HOME / ".cache" / "assistant" / "hub-vendor")
    pins = tomllib.loads((REPO / "ops/hub/vendor.toml").read_text()).get("wheel") or []
    if not all((cache / p["file"]).is_file() for p in pins):
        return "vendor wheels not cached (assistant deploy --target hub fetches them)"
    return None


# The cluster's parent-death guard: pg_ctl detaches the postmaster, so a test process killed outright (a CI timeout,
# SIGKILL) would leave it running. The guard holds the read end of a pipe whose write end only the test process has
# (close-on-exec: neither pg_ctl, postgres nor hubd inherits it); EOF = the test process is gone or stop() ran, and
# the guard stops the cluster (immediate: throwaway data) if it still runs.
_GUARD = """
import os, subprocess, sys
fd, ctl, data = int(sys.argv[1]), sys.argv[2], sys.argv[3]
try:
    while os.read(fd, 4096):
        pass
except OSError:
    pass
if os.path.exists(os.path.join(data, "postmaster.pid")):
    subprocess.run([ctl, "-D", data, "stop", "-m", "immediate", "-w", "-t", "30"], capture_output=True, timeout=60)
"""


class Pg:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.bin = pg_bin()
        self.data = root / "pgdata"
        self.sock = root / "pgsock"
        self.port = free_port()
        self.password = secrets.token_hex(16)
        self.pwfile = root / "pg_password"
        self.guard: subprocess.Popen | None = None
        self._guard_w: int | None = None

    def _start_guard(self) -> None:
        r, w = os.pipe()  # both ends close-on-exec; only the guard gets r (pass_fds)
        try:
            self.guard = subprocess.Popen(
                [sys.executable, "-I", "-c", _GUARD, str(r), str(self.bin / "pg_ctl"), str(self.data)],
                pass_fds=(r,), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=pg_env(),
                start_new_session=True,  # a ^C or a group kill of the test run must not take the guard first
            )  # fmt: skip
        finally:
            os.close(r)
        self._guard_w = w

    def start(self) -> Pg:
        self._start_guard()
        self.sock.mkdir(parents=True, exist_ok=True)
        self.pwfile.write_text(self.password)
        os.chmod(self.pwfile, 0o600)
        env = pg_env()
        subprocess.run(
            [str(self.bin / "initdb"), "-D", str(self.data), "-U", "hub", "--auth=scram-sha-256",
             f"--pwfile={self.pwfile}", "-E", "UTF8", "--no-instructions",
             "--no-sync"],  # a throwaway cluster: no fsync storm on a box running parallel suites
            check=True, capture_output=True, env=env, timeout=120,
        )  # fmt: skip
        self.ctl("start", "-w", "-t", "60", "-l", str(self.root / "pg.log"), "-o",
                 f"-p {self.port} -c unix_socket_directories='' -c listen_addresses=127.0.0.1 -c fsync=off "
                 "-c synchronous_commit=off -c max_connections=60")  # fmt: skip
        return self

    def ctl(self, *args: str) -> None:
        subprocess.run([str(self.bin / "pg_ctl"), "-D", str(self.data), *args], check=True, capture_output=True,
                       env=pg_env(), timeout=90)  # fmt: skip

    def stop(self) -> None:
        try:
            self.ctl("stop", "-m", "fast", "-w")
        except Exception:
            pass
        if self._guard_w is not None:  # EOF: the guard sees the cluster stopped and exits
            os.close(self._guard_w)
            self._guard_w = None
        if self.guard is not None:
            try:
                self.guard.wait(30)
            except subprocess.TimeoutExpired:
                self.guard.kill()
                self.guard.wait(5)

    def restart(self) -> None:
        self.ctl("restart", "-m", "fast", "-w", "-t", "60")

    def discard(self) -> None:
        """stop(), then remove the cluster's files (a fixture's teardown: ~40-160 MB each, and a CI run starts ~26
        clusters per interpreter, so they must not wait for the run dir to go at the end)."""
        self.stop()
        for d in (self.data, self.sock):
            shutil.rmtree(d, ignore_errors=True)


try:
    _PRCTL = ctypes.CDLL(None, use_errno=True).prctl
except (OSError, AttributeError):  # not Linux
    _PRCTL = None


def _die_with_parent() -> None:
    """preexec for hubd: SIGTERM when the test process dies (PR_SET_PDEATHSIG), so a killed run leaves no hubd."""
    if _PRCTL is not None:
        _PRCTL(1, signal.SIGTERM)


class Hubd:
    """hubd from the repo source on a loopback port; tokens for the brain and tg callers."""

    def __init__(self, root: Path, pg: Pg, vendor: Path, extra_env: dict[str, str] | None = None) -> None:
        self.root, self.pg, self.vendor = root, pg, vendor
        self.extra_env = dict(extra_env or {})
        self.secrets = root / "secrets"
        self.secrets.mkdir(parents=True, exist_ok=True)
        os.chmod(self.secrets, 0o700)
        self.tokens = {}
        for who in ("brain", "tg"):
            t = secrets.token_hex(24)
            (self.secrets / f"token_{who}").write_text(t)
            self.tokens[who] = t
        shutil.copy(pg.pwfile, self.secrets / "pg_password")
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.proc: subprocess.Popen | None = None
        self.log = root / "hubd.log"

    def env(self) -> dict[str, str]:
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.root),
            "HUB_RELEASE": "test",
            "HUB_PORT": str(self.port),
            "HUB_BIND": "127.0.0.1",
            "HUB_GW_PORT": "off",  # the hub gateway has its own tests (tests/tools/test_hubgw.py)
            "HUB_SECRETS": str(self.secrets),
            "HUB_VENDOR": str(self.vendor),
            "HUB_SQL_DIR": str(REPO / "schemas" / "pg"),
            "HUB_STATE": str(self.root / "state"),
            "HUB_SPOOL": str(self.root / "spool"),
            "PG_HOST": "127.0.0.1",
            "PG_PORT": str(self.pg.port),
            "PG_DB": "postgres",
            "PG_USER": "hub",
            "HUB_FEED_PORT": "off",  # the status feed listener: its own suite turns it on (test_status_feed.py)
            **self.extra_env,
        }

    def start(self, wait: float = 30.0) -> Hubd:
        f = open(self.log, "ab")
        self.proc = subprocess.Popen(
            [py313(), "-I", "-B", "-u", str(REPO / "packages" / "hub" / "hubd.py")],
            env=self.env(), stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            preexec_fn=_die_with_parent,
        )  # fmt: skip
        end = time.monotonic() + wait
        while time.monotonic() < end:
            try:
                with web.open(self.url + "/health", timeout=2) as r:
                    if json.load(r).get("store", {}).get("state") == "ok":
                        return self
            except (OSError, ValueError):
                pass
            if self.proc.poll() is not None:
                break
            time.sleep(0.1)
        raise RuntimeError("hubd did not come up: " + self.log.read_text()[-2000:])

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)

    def kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(5)

    def call(self, method: str, path: str, body: dict | None = None, who: str = "brain", schema: str | None = None,
             timeout: float = 10.0) -> tuple[int, dict]:  # fmt: skip
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.tokens[who]}")
        req.add_header("Content-Type", "application/json")
        if schema:
            req.add_header("X-Hub-Schema", schema)
        try:
            with web.open(req, timeout=timeout) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")


if __name__ == "__main__":  # manual smoke: python3 tests/hubstore/hubfix.py DIR
    root = Path(sys.argv[1])
    root.mkdir(parents=True, exist_ok=True)
    pg = Pg(root).start()
    hub = Hubd(root, pg, vendor_tree(root / "vendor")).start()
    print(json.dumps({"url": hub.url, "pg_port": pg.port, "pid": hub.proc.pid}))
