"""assistant uninstall: only the deployed files whose bytes are unchanged, the core link and the tagged cron lines."""

CRON = "MAILTO=\n# mine\n0 3 * * * /bin/true\n"


def test_uninstall_needs_dry_run_or_yes(env):
    assert env.run("deploy").returncode == 0
    r = env.run("uninstall")
    assert r.returncode == 2 and "--dry-run first" in r.stderr
    assert (env.home / "bin/hello").exists()


def test_uninstall_dry_run_then_yes(env):
    assert env.run("deploy").returncode == 0
    ct = env.home / "crontab"
    ct.write_text(CRON + "*/5 * * * * $HOME/bin/hello  # assistant:hello\n")
    (env.home / "bin/a.txt").write_text("edited by the user\n")  # changed since the deploy: kept
    (env.home / "bin/mine").write_text("the user's own file\n")  # never deployed: kept
    (env.home / "bin/lib/__pycache__").mkdir(parents=True)
    (env.home / "bin/lib/__pycache__/x.cpython-313.pyc").write_bytes(b"\0")  # bytecode beside removed modules
    r = env.run("uninstall", "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "  - ~/bin/hello" in r.stdout and "  - ~/bin/b.txt" in r.stdout
    assert "kept ~/bin/a.txt (changed since the deploy)" in r.stdout
    assert "1 crontab line(s) tagged" in r.stdout and "dry run, nothing changed" in r.stdout
    assert (env.home / "bin/hello").exists() and "# assistant:hello" in ct.read_text()

    r = env.run("uninstall", "--yes")
    assert r.returncode == 0, r.stderr
    assert not (env.home / "bin/hello").exists() and not (env.home / "bin/b.txt").exists()
    assert (env.home / "bin/a.txt").read_text() == "edited by the user\n"
    assert (env.home / "bin/mine").exists()
    assert not (env.home / "bin/lib/core").is_symlink() and not (env.home / "bin/lib").exists()  # left empty: gone
    assert ct.read_text() == CRON  # the user's own lines and settings stay
    assert env.cron_log.read_text().strip().endswith(str(ct))
    assert env.deployed() == {}
    assert env.run("uninstall", "--dry-run").stdout.startswith("uninstall: 0 deployed file(s)")
