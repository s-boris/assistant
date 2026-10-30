"""hubkit.kit: render from the profile, the install paste (<= 25 lines) and a sandbox run of it with stub
docker / php / install / chown; secrets.copy validation."""

import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest
from hubkit import HubError, settings
from hubkit import kit as K
from hubkit import release as R

RID = "abcdef123456"
SHA = RID + "0" * 28
STUBS = {
    "docker": '#!/bin/sh\necho "docker $*" >> "$STUB_LOG"\n'
    '[ "$1 $2" = "compose ps" ] && echo "claude-hub: Up (healthy)"\nexit 0\n',
    "php": '#!/bin/sh\necho "php $*" >> "$STUB_LOG"\n',
    "chown": '#!/bin/sh\necho "chown $*" >> "$STUB_LOG"\n',
    # install without the root-only -o/-g owner flags
    "install": '#!/bin/bash\na=(); while [ $# -gt 0 ]; do case "$1" in -o|-g) shift 2;; *) a+=("$1"); shift;; esac;'
    ' done\nexec /usr/bin/install "${a[@]}"\n',
}


def render(src: Path, home: Path, creds: str = "") -> dict:
    if creds:
        f = home / ".config/claude-hub/secrets.copy"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(creds)
    R.build(src, RID, SHA)
    R.write_current(RID)
    return K.render(src, SHA)


def test_render_values_and_paste(hubhome, srctree):
    info = render(srctree, hubhome)
    assert info["profile"] == "profile" and info["defaulted"] == []
    kd = hubhome / ".local/share/claude-hub/kit"
    paste = (kd / "install.sh").read_text()
    assert len(paste.rstrip("\n").splitlines()) <= K.PASTE_MAX == 25
    assert info["paste_lines"] == len(paste.rstrip("\n").splitlines())
    assert "@@" not in paste + (kd / "rollback.sh").read_text() + (kd / "card.txt").read_text()
    env = (kd / "hub.env").read_text()
    s = settings()
    assert f"HUB_ROOT={s['hub_root']}\n" in env and "BRAIN_NETWORK=test_default\n" in env
    assert f"BOT_HOME={hubhome}\n" in env and "TZ=Europe/Zurich\n" in env and f"HUB_KIT={info['kit']}\n" in env
    for name in ("install.sh", "rollback.sh"):
        r = subprocess.run(["bash", "-n", str(kd / name)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
    # same inputs -> same kit id; a profile change -> a new id, the old kit kept as kit.prev
    assert K.render(srctree, SHA)["kit"] == info["kit"]
    prof = Path(os.environ["ASSISTANT_PROFILE"])
    p = json.loads(prof.read_text())
    p["server"]["bot_network"] = "other_net"
    prof.write_text(json.dumps(p))
    assert K.render(srctree, SHA)["kit"] != info["kit"]
    assert (kd.with_name("kit.prev") / "kit.json").is_file()


def test_defaults_without_profile(hubhome, srctree, tmp_path, monkeypatch):
    monkeypatch.setenv("ASSISTANT_PROFILE", str(tmp_path / "none.json"))
    s = settings()
    assert s["source"] == "defaults" and len(s["defaulted"]) == 4
    assert s["hub_root"] == "/mnt/user/appdata/claude-hub"


@pytest.mark.parametrize(
    "server, msg",
    [
        ({"appdata": "relative/x"}, "server.appdata"),
        ({"bot_home": "/a/../b"}, "server.bot_home"),
        ({"bot_network": "bad net"}, "bot_network"),
        ({"appdata": 5}, "not a string"),
    ],
)
def test_settings_refuses(hubhome, tmp_path, monkeypatch, server, msg):
    f = tmp_path / "p.json"
    f.write_text(json.dumps({"server": server}))
    monkeypatch.setenv("ASSISTANT_PROFILE", str(f))
    with pytest.raises(HubError, match=re.escape(msg)):
        settings()


@pytest.mark.parametrize(
    "line", ["one\n", "token_x .secrets/a\n", "pg_password .secrets/a\n", "a /etc/shadow\n", "a ../x\n", "a b\na c\n"]
)
def test_secrets_list_refuses(hubhome, line):
    f = hubhome / ".config/claude-hub/secrets.copy"
    f.parent.mkdir(parents=True)
    f.write_text(line)
    with pytest.raises(HubError):
        K.secrets_list()


def _inner(paste: str, cfg: Path) -> str:
    m = re.search(r"<<'HUB'\n(.*?)\nHUB\n", paste, re.S)
    assert m, "paste has no HUB heredoc"
    body = m.group(1)
    assert body.count("/boot/config/plugins/appdata.backup/config.json") == 1
    return body.replace("/boot/config/plugins/appdata.backup/config.json", str(cfg))


def test_install_paste_sandbox(hubhome, srctree, tmp_path):
    (hubhome / ".secrets").mkdir(mode=0o700)
    (hubhome / ".secrets" / "vault-token").write_text("v1\n")
    info = render(srctree, hubhome, "# names only\nvault_token .secrets/vault-token\nmissing_one .secrets/nope\n")
    assert info["credentials"] == 2
    kd = hubhome / ".local/share/claude-hub/kit"
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for n, body in STUBS.items():
        (stubs / n).write_text(body)
        (stubs / n).chmod(0o755)
    cfg = tmp_path / "backup-config.json"
    cfg.write_text("{}")
    script = tmp_path / "inner.sh"
    script.write_text(_inner((kd / "install.sh").read_text(), cfg))
    log = tmp_path / "stub.log"
    env = {"PATH": f"{stubs}:/usr/bin:/bin", "STUB_LOG": str(log), "HOME": str(hubhome)}

    def run():
        return subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, cwd=tmp_path)

    r = run()
    assert r.returncode == 0, r.stderr
    assert f"claude-hub kit {info['kit']}: OK" in r.stdout and "skipped missing_one" in r.stdout
    hub = Path(settings()["hub_root"])
    sec = hub / "secrets"
    toks = {n: (sec / n).read_text() for n in ("pg_password", "token_brain", "token_tg")}
    assert all(re.fullmatch(r"[0-9a-f]{64}", t) for t in toks.values())
    assert (sec / "vault_token").read_text() == "v1\n"
    assert stat.S_IMODE((sec / "vault_token").stat().st_mode) == 0o600
    assert (hubhome / ".secrets/hub-token").read_text() == toks["token_brain"]
    assert (hub / ".env").read_bytes() == (kd / "hub.env").read_bytes()
    assert (hub / "loader/hubloader.py").read_bytes() == (srctree / "packages/hub/loader.py").read_bytes()
    ci, cisrc = hub / "ci-image/build", srctree / "ops/hub/ci"  # compose's build context for claude-hub-ci
    assert (ci / "Dockerfile").read_bytes() == (cisrc / "Dockerfile").read_bytes()
    assert {p.name: p.read_bytes() for p in (ci / "requirements").iterdir()} == {
        p.name: p.read_bytes() for p in (cisrc / "requirements").iterdir()
    }
    calls = log.read_text()
    assert "docker network inspect test_default" in calls
    assert (
        "docker compose -f compose.yaml config -q" in calls and "docker compose -f compose.yaml up -d --wait" in calls
    )
    assert "compose.tg.yaml" not in calls, "the tg override joins only once the tg paste installed it"
    assert re.search(r"php -r .* claude-hub claude-hub-pg", calls)
    assert (tmp_path / "backup-config.json.bak-claude-hub").is_file()
    # again (= update): generated secrets stay
    assert run().returncode == 0
    assert all((sec / n).read_text() == t for n, t in toks.items())
    # with claude-hub-browser installed (it runs as the brain's uid): after the 10100 sweep its own two secrets go
    # back to the brain's uid, token_browser to 10100 + the brain's gid, 0640
    (hub / "compose.browser.yaml").write_text("x")
    for n in ("token_browser", "browser_ctl_token", "browser_vnc_pass"):
        (sec / n).write_text("t\n")
    log.unlink()
    assert run().returncode == 0
    calls = log.read_text()
    bh = hubhome.stat()
    for n in ("browser_ctl_token", "browser_vnc_pass"):
        assert f"chown {bh.st_uid}:{bh.st_gid} {sec / n}" in calls
    assert f"chown 10100:{bh.st_gid} {sec / 'token_browser'}" in calls
    assert stat.S_IMODE((sec / "token_browser").stat().st_mode) == 0o640
    assert all(stat.S_IMODE((sec / n).stat().st_mode) == 0o600 for n in ("browser_ctl_token", "browser_vnc_pass"))
    # after the sweep (the hub CI runner's gid is 10100 itself: look for the token_browser line)
    assert calls.index(f"chown 10100:10100 {sec}/") < calls.index(f"chown 10100:{bh.st_gid} {sec / 'token_browser'}")
    # a kit changed after the paste was made (the ci image's files count too): refused before anything else runs
    req = kd / "ci-image/build/requirements/system.txt"
    keep = req.read_bytes()
    req.write_bytes(keep + b"# changed\n")
    r = run()
    assert r.returncode == 1 and "the kit changed" in r.stderr
    req.write_bytes(keep)
    with (kd / "hub.env").open("a") as f:
        f.write("X=1\n")
    r = run()
    assert r.returncode == 1 and "the kit changed" in r.stderr


def test_release_carries_the_pinned_wheels(hubhome, srctree):
    d, _ = R.build(srctree, RID, SHA)
    files = json.loads((d / "release.json").read_text())["files"]
    assert "vendor/fakepkg/__init__.py" in files and (d / "vendor/fakepkg/__init__.py").is_file()
    assert "app/hubstore.py" in files and R.verify(RID) is None
