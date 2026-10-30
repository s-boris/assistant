"""The assistant CLI's deploy lifecycle in one pass (the former shell suite): deploy, a second deploy that changes
bytes, mode and core, a live edit seen as drift and refused, rollback --force back to v1, deploy --force over it, a
smoke failure rolled back, a clean status after it, and a cron add + rm round trip through the stub installer. The
single steps (and the real-home refusal) have their own tests in test_deploy.py and test_cron.py; this one checks
they chain. The suite's nightly `assistant ci --offline` run is the CI run itself and is not repeated here."""

import hashlib
import os
import stat

HEADER = "# managed: ~/opt/assistant — edit there, deploy with assistant land"


def mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


def digest(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_deploy_lifecycle(env):
    hello = env.home / "bin/hello"
    link = env.home / "bin/lib/core"

    r = env.run("deploy")
    assert r.returncode == 0, r.stderr
    assert hello.read_text().splitlines()[1] == HEADER
    assert link.is_symlink() and (link / "clock.py").is_file()
    v1, l1 = digest(hello), os.readlink(link)

    env.write("tools/hello.py", "#!/bin/sh\necho v2\n")
    manifest = env.repo / "ops/deploy/manifest.toml"
    env.write(
        "ops/deploy/manifest.toml",
        manifest.read_text().replace('dest = "bin/hello"\nmode = "0755"', 'dest = "bin/hello"\nmode = "0700"'),
    )
    env.write("core/zz.py", "Z = 1\n")
    env.commit("v2")
    r = env.run("deploy")
    assert r.returncode == 0, r.stderr
    assert mode(hello) == 0o700 and os.readlink(link) != l1

    hello.write_text(hello.read_text() + "# live edit\n")
    d = env.run("drift")
    assert d.returncode == 3 and "bin/hello: modified" in d.stdout

    env.write("tools/hello.py", "#!/bin/sh\necho v3\n")
    env.commit("v3")
    assert env.run("deploy").returncode == 3
    assert "# live edit" in hello.read_text()

    r = env.run("rollback", "--force")
    assert r.returncode == 0, r.stderr
    assert digest(hello) == v1 and mode(hello) == 0o755 and os.readlink(link) == l1

    assert env.run("deploy", "--force").returncode == 0
    env.write("tools/hello.py", "#!/bin/sh\nexit 1\n")
    env.commit("broken")
    before = digest(hello)
    r = env.run("deploy")
    assert r.returncode == 4 and "rolled back" in r.stdout + r.stderr
    assert digest(hello) == before

    s = env.run("status")
    assert s.returncode == 0 and "drift: none" in s.stdout

    ct = env.home / "crontab"
    ct.write_text("SHELL=/bin/bash\n*/5 * * * * /bin/true\n")
    assert env.run("cron", "add", "demo", "0 4 * * *", "/bin/true").returncode == 0
    assert "assistant:demo" in ct.read_text()
    assert env.run("cron", "rm", "demo").returncode == 0
    assert ct.read_text() == "SHELL=/bin/bash\n*/5 * * * * /bin/true\n"
    assert len(env.cron_log.read_text().splitlines()) == 2
