"""Fixtures for the hub kit: a temp HOME with a profile, a git repo holding the hub sources, a fake
hub on loopback whose /health follows releases/CURRENT."""

import hashlib
import http.server
import json
import os
import shutil
import socket
import subprocess
import threading
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HUB_SRC = ("ops/hub", "packages/hub")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def hubhome(tmp_path, monkeypatch):
    """HOME + profile (bot_home = that HOME, so the kit dir the paste reads is the one render writes) + HUB_URL to a
    closed loopback port. Returns the HOME."""
    home = tmp_path / "home"
    home.mkdir()
    prof = tmp_path / "profile.json"
    prof.write_text(
        json.dumps(
            {
                "server": {"appdata": str(tmp_path / "appdata"), "bot_home": str(home), "bot_network": "test_default"},
                "locale": {"tz": "Europe/Zurich"},
            }
        )
    )
    for k, v in {
        "HOME": str(home),
        "ASSISTANT_TEST": "1",
        "ASSISTANT_PROFILE": str(prof),
        "HUB_URL": f"http://127.0.0.1:{free_port()}",
        "HUB_TOKEN_FILE": str(tmp_path / "brain-token"),
    }.items():
        monkeypatch.setenv(k, v)
    return home


FAKE_WHEEL = "fakepkg-1.0-py3-none-any.whl"


def vendor_cache(tmp_path: Path) -> Path:
    return tmp_path / "vendor-cache"


@pytest.fixture(autouse=True)
def _offline_vendor(tmp_path, monkeypatch):
    """Releases built here carry a tiny local wheel instead of the pinned ones: tests never download."""
    monkeypatch.setenv("HUB_VENDOR_CACHE", str(vendor_cache(tmp_path)))


def fake_vendor(src: Path, cache: Path) -> None:
    """ops/hub/vendor.toml in the copied tree pins FAKE_WHEEL, which waits in the cache (HUB_VENDOR_CACHE)."""
    cache.mkdir(parents=True, exist_ok=True)
    whl = cache / FAKE_WHEEL
    if not whl.is_file():
        with zipfile.ZipFile(whl, "w") as z:
            z.writestr("fakepkg/__init__.py", "X = 1\n")
    sha = hashlib.sha256(whl.read_bytes()).hexdigest()
    (src / "ops/hub/vendor.toml").write_text(
        f'[[wheel]]\nname = "fakepkg"\nversion = "1.0"\nfile = "{FAKE_WHEEL}"\n'
        f'url = "https://example.invalid/{FAKE_WHEEL}"\nsha256 = "{sha}"\n'
    )


def make_src(dest: Path) -> Path:
    """A source tree with just what the hub release and kit need (its vendor pin: the fake wheel)."""
    for rel in HUB_SRC:
        shutil.copytree(REPO / rel, dest / rel, ignore=shutil.ignore_patterns("__pycache__"))
    fake_vendor(dest, vendor_cache(dest.parent))
    return dest


class FakeHub:
    """GET /health: release = CURRENT's id (mode "follow"), a fixed id (mode "stuck"), or CURRENT's attempt in
    `bad` (mode "bad"); checks all ok unless `checks` is set."""

    def __init__(self, releases: Path):
        self.releases = releases
        self.mode = "follow"
        self.fixed: str | None = None
        self.checks = {"pg": "ok", "phoenix": "ok", "ci": "ok"}
        self.kit = "unknown"
        hub = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                try:
                    line = (hub.releases / "CURRENT").read_text().split()
                except OSError:
                    line = []
                body: dict = {"ok": True, "checks": hub.checks, "kit": hub.kit, "last_good": hub.fixed}
                if hub.mode == "follow":
                    body["release"] = line[0] if line else None
                elif hub.mode == "stuck":
                    body["release"] = hub.fixed
                else:
                    body["release"] = hub.fixed
                    body["bad"] = [" ".join(line)]
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def fakehub(hubhome, monkeypatch):
    h = FakeHub(hubhome / ".local/share/claude-hub/releases")
    monkeypatch.setenv("HUB_URL", h.url)
    yield h
    h.close()


@pytest.fixture
def srctree(tmp_path) -> Path:
    return make_src(tmp_path / "src")


@pytest.fixture
def hubrepo(tmp_path):
    """A git repo (main) with the hub sources; returns (repo, commit fn(msg) -> sha)."""
    repo = make_src(tmp_path / "repo")
    git(repo, "init", "-q", "-b", "main")

    def commit(msg: str) -> str:
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", msg)
        return git(repo, "rev-parse", "HEAD")

    commit("v1")
    return repo, commit


def git(repo: Path, *args: str) -> str:
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "PATH": os.environ.get("PATH") or "/usr/bin:/bin",
    }
    for k in ("GIT_EXEC_PATH", "GIT_TEMPLATE_DIR", "LD_LIBRARY_PATH"):  # the hub runner's git is its toolchain's
        if os.environ.get(k):
            env[k] = os.environ[k]
    r = subprocess.run(["git", "-C", str(repo), *args], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()
