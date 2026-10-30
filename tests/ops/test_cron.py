import pytest

CRON = "SHELL=/bin/bash\n# jobs\n*/5 * * * * /bin/true\n@reboot /bin/true\n"


def setup(env):
    ct = env.home / "crontab"
    ct.write_text(CRON)
    return ct


def test_add_list_rm(env):
    ct = setup(env)
    r = env.run("cron", "add", "events-ingest", "* * * * *", "$HOME/bin/events ingest --cron")
    assert r.returncode == 0, r.stderr
    assert ct.read_text() == CRON + "* * * * * $HOME/bin/events ingest --cron  # assistant:events-ingest\n"
    assert env.cron_log.read_text().strip() == str(ct)
    assert "assistant:events-ingest" in env.run("cron", "list").stdout
    assert env.run("cron", "add", "events-ingest", "* * * * *", "x").returncode == 3
    assert env.run("cron", "rm", "events-ingest").returncode == 0
    assert ct.read_text() == CRON
    assert env.run("cron", "rm", "events-ingest").returncode == 3
    assert [h["action"] for h in env.history()] == ["add", "rm"]


def test_dry_run_changes_nothing(env):
    ct = setup(env)
    assert env.run("cron", "add", "alpha", "0 3 * * *", "/bin/true").returncode == 0
    before = ct.read_text()
    for args in (("--dry-run", "rm", "alpha"), ("rm", "alpha", "--dry-run")):
        r = env.run("cron", *args)
        assert r.returncode == 0, r.stderr
        assert "dry run, would remove: 0 3 * * * /bin/true  # assistant:alpha" in r.stdout
    r = env.run("cron", "--dry-run", "add", "beta", "*/5 * * * *", "/bin/true")
    assert r.returncode == 0 and "would add: */5 * * * * /bin/true  # assistant:beta" in r.stdout
    assert env.run("cron", "--dry-run", "rm", "nope").returncode == 3  # the same refusals as a real edit
    assert env.run("cron", "--dry-run", "add", "alpha", "* * * * *", "x").returncode == 3
    assert ct.read_text() == before and [h["action"] for h in env.history()] == ["add"]


def test_invalid_lines_refused(env):
    ct = setup(env)
    assert env.run("cron", "add", "bad", "* * *", "/bin/true").returncode == 2
    assert env.run("cron", "add", "bad", "* * * * *", "").returncode == 2
    assert env.run("cron", "add", "bad name", "* * * * *", "/bin/true").returncode == 2
    assert env.run("cron", "add", "bad", "* * * * *\n* * * * *", "/bin/true").returncode == 2
    assert ct.read_text() == CRON


def test_install_failure_restores_file(env):
    ct = setup(env)
    r = env.run("cron", "add", "x", "0 3 * * *", "/bin/true", extra={"CRONTAB_STUB_RC": "1"})
    assert r.returncode == 1 and ct.read_text() == CRON


def test_symlinked_crontab_kept_a_link(env, tmp_path):
    real = tmp_path / "real-crontab"
    real.write_text(CRON)
    (env.home / "crontab").symlink_to(real)
    assert env.run("cron", "add", "y", "@daily", "/bin/true").returncode == 0
    assert (env.home / "crontab").is_symlink() and "assistant:y" in real.read_text()


def test_install_core_from_the_registry(env):
    from core import schedules

    reg = schedules.load()
    core = sorted(schedules.install_tags(reg, set(schedules.CORE_FEATURES))[0])
    assert {"watch", "cue-idea-scan", "cue-self-cleanup", "cue-digest", "log-rotate", "events-ingest"} <= set(core)
    assert "cue-daily-plan" not in core  # goals' daily cue is owner_only; its weekly idea-scan cue is core
    ct = env.home / "crontab"
    r = env.run("cron", "install", "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "(default: core only)" in r.stdout and "dry run, nothing written" in r.stdout and not ct.exists()
    r = env.run("cron", "install")
    assert r.returncode == 0, r.stderr
    lines = ct.read_text().splitlines()
    assert lines[0] == f"PATH={env.home}/bin:{env.home}/.local/bin:/usr/local/bin:/usr/bin:/bin"
    assert sorted(ln.rsplit("# assistant:", 1)[1] for ln in lines[1:]) == core
    assert all("$HOME/" in ln and str(env.home) not in ln for ln in lines[1:])  # home-neutral registry lines
    assert env.cron_log.read_text().strip().endswith(str(ct))
    assert schedules.parity(ct.read_text(), reg).changed == []
    r = env.run("cron", "install")  # idempotent
    assert r.returncode == 0 and "nothing to add" in r.stdout and ct.read_text().splitlines() == lines


def test_install_keeps_lines_and_refuses_unknown_features(env):
    ct = setup(env)
    r = env.run("cron", "install", "--features", "media,nope")
    assert r.returncode == 2 and "unknown feature(s) nope" in r.stderr and ct.read_text() == CRON
    r = env.run("cron", "install", "--features", "media")
    assert r.returncode == 0, r.stderr
    text = ct.read_text()
    assert text.startswith("PATH=") and CRON.strip() in text and "# assistant:media-doctor" in text
    assert "# assistant:log-rotate" in text  # core comes with every selection


def test_parity_reads_a_literal_home_as_home():
    from core import schedules

    reg = schedules.load()
    e = reg.jobs["events-ingest"]
    assert "$HOME/bin/events" in e.cron_line
    lit = e.cron_line.replace("$HOME/", "/srv/bot/")
    p = schedules.parity(lit + "  # assistant:events-ingest\n", reg)
    assert p.changed  # another home: changed
    assert schedules.home_form(lit, "/srv/bot") == e.cron_line
    assert schedules.home_form("/srv/bot2/x /srv/bot", "/srv/bot") == "/srv/bot2/x $HOME"


def test_every_feature_switch_maps_to_registry_features():
    from core import schedules

    known = {e.feature for e in schedules.load().jobs.values()}
    for switch, feats in schedules.FEATURE_SWITCHES.items():
        assert set(feats) <= known, (switch, set(feats) - known)
    assert schedules.switch_features({"swiss", "media", "nope"}, known) == ({"deals", "media"}, {"nope"})
    assert "shopping" in schedules.FEATURE_SWITCHES["home_assistant"]
    every = {f for fs in schedules.FEATURE_SWITCHES.values() for f in fs}
    assert schedules.switch_features({"all"}, known) == (every, set())


def test_install_takes_the_profiles_switches(env, tmp_path):
    prof = tmp_path / "profile.json"
    prof.write_text('{"features": {"hub": true, "swiss": false, "unraid": true, "made_up": true}}')
    r = env.run("cron", "install", "--dry-run", extra={"ASSISTANT_PROFILE": str(prof)})
    if "(profile)" not in r.stdout:
        pytest.skip("profile-get does not know the features key in this tree yet")
    assert r.returncode == 0, r.stderr
    assert "made_up schedule nothing" in r.stdout
    assert "# assistant:tg-status" in r.stdout and "# assistant:unraid-health" in r.stdout
    assert "# assistant:deadlines-sync" not in r.stdout and "# assistant:log-rotate" in r.stdout


def _tags(stdout: str) -> set[str]:
    return {ln.rsplit("# assistant:", 1)[1].strip() for ln in stdout.splitlines() if ln.startswith("  + ")
            and "# assistant:" in ln}  # fmt: skip


def test_owner_only_jobs_are_parsed_and_never_installed(env):
    from core import schedules

    reg = schedules.load()
    assert reg.jobs["deals-tutti"].owner_only and not reg.jobs["deals-hdd"].owner_only
    for name in ("deadlines-sync", "bills-scan-paperless", "contracts-scan", "missing-docs", "cue-daily-plan"):
        assert reg.jobs[name].owner_only, name
    r = env.run("cron", "install", "--features", "swiss", "--dry-run")
    assert r.returncode == 0, r.stderr
    got = _tags(r.stdout)
    assert "deals-hdd" in got and "deals-tutti" not in got
    assert "owner-only, never scheduled here: deals-tutti" in r.stdout
    r = env.run("cron", "install", "--features", "deadlines,bills,deals", "--dry-run")  # registry names: same rule
    assert r.returncode == 0, r.stderr
    got = _tags(r.stdout)
    assert not {t for t in got if reg.lines[t][0].owner_only} and "deals-hdd" in got
    assert "deadlines-sync" not in got and "bills-week" not in got


def test_owner_only_must_be_a_bool(tmp_path):
    from core import schedules

    reg = schedules.load()
    src = reg.path.read_text()
    bad = tmp_path / "s.toml"
    bad.write_text(src.replace("[jobs.deals-tutti]\n", '[jobs.deals-tutti]\nowner_only = "yes"\n', 1))
    with pytest.raises(schedules.RegistryError, match="owner_only"):
        schedules.load(bad)


def test_features_all_is_every_switch_and_never_owner_only(env):
    from core import schedules

    reg = schedules.load()
    on_cron = {e.feature for e in reg.jobs.values() if e.runs_on == "cron"}
    every = {f for fs in schedules.FEATURE_SWITCHES.values() for f in fs}
    want = (every | set(schedules.CORE_FEATURES)) & on_cron
    r = env.run("cron", "install", "--features", "all", "--dry-run")
    assert r.returncode == 0, r.stderr
    got = _tags(r.stdout)
    assert got == set(schedules.install_tags(reg, want)[0])
    assert not any(e.owner_only for t in got for e in reg.lines[t])
    assert all(any(e.feature in want for e in reg.lines[t]) for t in got)
    assert "assistant-publish" not in got and "cat-autopilot" not in got  # no switch names these features


def test_check_names_missing_owner_only_lines(env):
    r = env.run("cron", "check")
    assert r.returncode == 3
    lines = r.stdout.splitlines()
    assert "  missing deals-tutti (owner-only)" in lines and "  missing deals-hdd" in lines


def test_every_shipped_skill_cron_dependency_is_scheduled_when_its_switches_are_on():
    """templates/manifest.toml [cron]: the jobs a shipped skill or agent relies on are scheduled by `cron install`
    once the switches that install it are on, and none of them is owner_only."""
    import tomllib
    from pathlib import Path

    from core import schedules

    man = tomllib.loads((Path(schedules.__file__).parents[1] / "templates" / "manifest.toml").read_text())
    reg = schedules.load()
    known = {e.feature for e in reg.jobs.values()}
    on_cron = {e.feature for e in reg.jobs.values() if e.runs_on == "cron"}
    assert man["cron"], "no [cron] table"
    for name, jobs in man["cron"].items():
        need = man["skills"].get(name, man["agents"].get(name))
        assert need is not None, f"[cron] {name}: not a shipped skill or agent"
        need = {need} if isinstance(need, str) else set(need)
        mapped, unknown = schedules.switch_features(need - {"core"}, known)
        assert not unknown, (name, unknown)
        want = (mapped | set(schedules.CORE_FEATURES)) & on_cron
        tags = set(schedules.install_tags(reg, want)[0])
        for job in jobs:
            e = reg.jobs.get(job)
            assert e is not None, f"{name}: no registry job {job}"
            assert not e.owner_only, f"{name}: {job} is owner_only"
            if e.runs_on == "cron":
                assert e.line in tags, f"{name}: {job} not scheduled with switches {sorted(need)}"
            else:  # a hub schedule: runs on the hub once the hub switch is on
                assert e.runs_on == "hub" and "hub" in need and e.feature in mapped, f"{name}: {job} ({e.runs_on})"
    shipped_with_jobs = {"watch", "deals", "shopping", "idea-scan", "self-cleanup", "weekly-digest"}
    assert shipped_with_jobs <= set(man["cron"])
