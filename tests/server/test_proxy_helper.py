"""`proxy helper`: the root-owned cert helper (claude-cert) the web VM runs for `proxy cert`, printed by scripts/proxy.

Offline: the script is rendered with the synthetic profile (tests/legacy/profile/fixtures, domain example.org), its
certbot paths (/snap/bin/certbot, /usr/bin/certbot) are rewritten to fakes in tmp_path, and it runs under sh. The
fakes answer `plugins` (with or without "* nginx") and log every other call. No ssh, no server, no real certbot:
`helper` exits before the script reads the server or the mirror (PROXY_HOST/PROXY_MIRROR point nowhere).
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PROXY = REPO / "scripts" / "proxy"
PROFILE = REPO / "tests" / "legacy" / "profile" / "fixtures" / "profile.json"
SNAP, APT = "/snap/bin/certbot", "/usr/bin/certbot"

pytestmark = pytest.mark.skipif(not shutil.which("sh"), reason="needs sh")


def _env(tmp_path, profile=PROFILE):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PROXY_", "ASSISTANT_PROFILE"))}
    env.update(
        ASSISTANT_PROFILE=str(profile),
        HOME=str(tmp_path),
        PROXY_HOST="user@proxy-test.invalid",
        PROXY_MIRROR=str(tmp_path / "no-mirror"),
    )
    return env


def _proxy(tmp_path, *args, profile=PROFILE):
    return subprocess.run(
        ["sh", str(PROXY), *args], capture_output=True, text=True, timeout=30, env=_env(tmp_path, profile), check=False
    )


def _fake(path: Path, nginx: bool, log: Path):
    plug = "* nginx\\nDescription: Nginx Web Server plugin\\n" if nginx else ""
    path.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = plugins ]; then printf "* dns-cloudflare\\nDescription: x\\n{plug}* standalone\\n"; exit 0; fi\n'
        f'echo "{path.parent.name} $*" >> {log}\n'
    )
    path.chmod(0o755)


@pytest.fixture
def helper(tmp_path):
    """Render the helper with certbot paths pointing at tmp_path/snap and tmp_path/apt; return a runner."""
    r = _proxy(tmp_path, "helper", "--script")
    assert r.returncode == 0, r.stderr
    script = r.stdout
    assert SNAP in script and APT in script
    for d in ("snap", "apt"):
        (tmp_path / d).mkdir()
    log = tmp_path / "calls.log"
    body = script.replace(SNAP, str(tmp_path / "snap" / "certbot")).replace(APT, str(tmp_path / "apt" / "certbot"))
    sp = tmp_path / "claude-cert"
    sp.write_text(body)

    def run(name, snap=None, apt=None):
        for d, nginx in (("snap", snap), ("apt", apt)):
            p = tmp_path / d / "certbot"
            p.unlink(missing_ok=True)
            if nginx is not None:
                _fake(p, nginx, log)
        log.unlink(missing_ok=True)
        r = subprocess.run(["sh", str(sp), name], capture_output=True, text=True, timeout=30, check=False)
        return r, (log.read_text().splitlines() if log.exists() else [])

    return run


NGINX_ARGS = "--nginx --non-interactive --agree-tos --redirect -d"


def test_snap_certbot_with_nginx_wins(helper):
    r, calls = helper("seerr.example.org", snap=True, apt=False)
    assert r.returncode == 0, r.stderr
    assert calls == [f"snap {NGINX_ARGS} seerr.example.org"]


def test_snap_preferred_even_when_apt_has_nginx(helper):
    _, calls = helper("example.org", snap=True, apt=True)
    assert calls == [f"snap {NGINX_ARGS} example.org"]


def test_apt_certbot_used_when_it_is_the_only_one_with_nginx(helper):
    _, calls = helper("x.example.org", snap=None, apt=True)
    assert calls == [f"apt {NGINX_ARGS} x.example.org"]
    _, calls = helper("x.example.org", snap=False, apt=True)
    assert calls == [f"apt {NGINX_ARGS} x.example.org"]


def test_no_certbot_with_nginx_fails_without_running_certbot(helper):
    """Today's VM before the fix: only apt's certbot is called, and it has no nginx plugin."""
    r, calls = helper("seerr.example.org", snap=None, apt=False)
    assert r.returncode == 1 and calls == []
    assert "no certbot with the nginx plugin" in r.stderr


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "-x.example.org",
        ".example.org",
        "a..example.org",
        "A.example.org",
        "a b.example.org",
        "a;id.example.org",
        "other.org",
        "example.org.evil.net",
        "xexample.org",
    ],
)
def test_bad_or_foreign_names_refused_before_certbot(helper, bad):
    r, calls = helper(bad, snap=True, apt=True)
    assert r.returncode == 1 and calls == []


def test_paste_installs_atomically_and_names_the_domain(tmp_path):
    r = _proxy(tmp_path, "helper")
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "sudo tee /usr/local/sbin/claude-cert.new >/dev/null <<'CLAUDE_CERT'" in out
    assert out.rstrip().endswith("sudo mv /usr/local/sbin/claude-cert.new /usr/local/sbin/claude-cert")
    assert "D=example.org" in out and "@@DOMAIN@@" not in out
    assert not (tmp_path / "no-mirror").exists()


def test_bad_domain_in_profile_refused(tmp_path):
    p = tmp_path / "profile.json"
    p.write_text(PROFILE.read_text().replace('"example.org"', '"exa/mple.org"'))
    assert '"exa/mple.org"' in p.read_text()
    r = _proxy(tmp_path, "helper", profile=p)
    assert r.returncode != 0 and "CLAUDE_CERT" not in r.stdout
