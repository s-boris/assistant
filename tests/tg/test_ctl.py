"""tg-adapter ensure/status/stop with a fake bun standing in for the adapter process: nothing at all without a config,
the switch file, start/running/restart on an adapters/tg change, held down after exits that won't heal (one note per
episode), crash-loop backoff, stop, and a status that says whether the token is present without naming its file."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from tg_testlib import db_rows, fake, fake_calls, run_cli, wait_for, write_config, write_token
from tgadapter import ctl


def exited() -> bool:
    return ctl.running_pid() is None and ctl.last_exit(ctl.history()) is not None


def notes(home: Path, prefix: str) -> list[str]:
    return sorted(p.name for p in (home / "inbox").iterdir() if p.name.startswith(prefix))


def test_not_configured_does_nothing_at_all(home, fake_adapter):
    before = sorted(str(p) for p in home.rglob("*"))
    assert ctl.ensure() == (0, "not configured (no ~/.config/tg-adapter/test.json or config.json): nothing runs")
    r = run_cli("ensure", "--cron")
    assert (r.returncode, r.stdout, r.stderr) == (0, b"", b"")
    assert sorted(str(p) for p in home.rglob("*")) == before  # no state dir, no DB, no note
    assert fake_calls(home) == []  # bun never ran
    s = run_cli("status")
    assert s.returncode == 0 and b"configured no" in s.stdout and b"nothing runs until the config exists" in s.stdout


def test_status_token_present_never_names_the_file(home, fake_adapter):
    def status() -> tuple[dict, str]:
        j = json.loads(run_cli("status", "--json").stdout)
        t = run_cli("status").stdout.decode()
        return j, t

    j, t = status()
    assert (j["token_present"], j["token_from"]) == (False, "default") and "token present no (default location)" in t
    default = home / ".secrets" / "tg-test-bot.env"
    default.parent.mkdir()
    default.write_text("TELEGRAM_BOT_TOKEN=4242:TEST-not-a-real-token\n")  # secretscan:allow (fake token)
    default.chmod(0o644)
    j, t = status()
    assert (j["token_present"], j["token_mode_ok"]) == (True, False) and "chmod 600" in t
    write_config(home)
    j, t = status()
    assert (j["configured"], j["token_present"], j["token_from"]) == (True, False, "config")
    write_token(home)
    j, t = status()
    assert (j["token_present"], j["token_mode_ok"]) == (True, True) and "token present yes" in t
    for out in (json.dumps(j), t):
        assert "secrets" not in out and "test.env" not in out and "tok/" not in out and "4242" not in out
    assert j["adapter"]["offset"] == 7  # the bun CLI's own status, handed through


def test_start_running_switch_and_stop(home, fake_adapter):
    write_config(home)
    write_token(home)
    code, msg = ctl.ensure()
    assert code == 0 and msg.startswith("started (pid ")
    pid = ctl.running_pid()
    assert pid and fake_calls(home)[:2] == ["check", "run"]
    assert ctl.ensure() == (0, f"running (pid {pid})")
    assert fake_calls(home).count("run") == 1
    st = ctl.status()
    assert (st["running"], st["pid"]) == (True, pid)
    log = home / ".local/state/tg-adapter/test/adapter.log"
    assert log.stat().st_mode & 0o777 == 0o600
    (home / ".config/tg-adapter/off").touch()
    assert ctl.ensure()[1].startswith("switch off") and "stops itself" in ctl.ensure()[1]
    (home / ".config/tg-adapter/off").unlink()
    code, msg = ctl.stop()
    assert code == 0 and msg.startswith(f"stopped pid {pid}") and "cron ensure starts it again" in msg
    assert ctl.running_pid() is None
    assert wait_for(lambda: ctl.last_exit(ctl.history()) is not None)
    assert ctl.last_exit(ctl.history())[1] == 0
    # a clean stop is restarted by ensure
    assert ctl.ensure()[1].startswith("started")
    code, msg = ctl.stop(keep_down=True)
    assert "switch file written" in msg and (home / ".config/tg-adapter/test.off").exists()
    assert ctl.ensure()[1].startswith("switch off")
    assert ctl.stop() == (0, "not running")


def test_permanent_exit_held_down_with_one_note(home, fake_adapter):
    cfg = write_config(home)
    write_token(home)
    fake(home, "run", "exit:3")
    # like the real adapter: PID file claimed after getMe, then 409 on getUpdates → exit 3
    assert ctl.ensure()[0] == 0
    assert wait_for(exited)
    for _ in range(3):
        code, msg = ctl.ensure()
        assert code == 3 and "409 Conflict" in msg
    assert fake_calls(home).count("run") == 1  # never fights for the poller
    assert len(notes(home, "tg-adapter-down-")) == 1
    rows = db_rows(home, "SELECT kind, subject, next, source FROM events WHERE kind='tg-adapter.down'")
    assert len(rows) == 1 and "409 Conflict" in rows[0]["subject"] and "ensure --force" in rows[0]["next"]
    r = run_cli("ensure", "--cron")
    assert r.returncode == 0 and b"held down" in r.stdout  # cron: the note is the report, no cron-fail noise
    assert run_cli("ensure").returncode == 3
    # a changed config lifts the hold (the owner or the brain fixed something)
    time.sleep(0.01)
    fake(home, "run", "serve")
    os.utime(cfg, None)
    assert ctl.ensure()[1].startswith("started")
    ctl.stop()
    # --force lifts it too
    fake(home, "run", "exit:5")
    ctl.ensure()
    assert wait_for(exited)
    assert ctl.ensure()[0] == 3
    fake(home, "run", "serve")
    assert ctl.ensure(force=True)[1].startswith("started")


def test_check_refusal_counts_as_config_exit(home, fake_adapter):
    write_config(home)
    write_token(home)
    fake(home, "check_rc", "2")
    code, msg = ctl.ensure()
    assert code == 3 and msg.startswith("check refused the config: tg-adapter: config is not valid JSON")
    assert "run" not in fake_calls(home)
    code, msg = ctl.ensure()
    assert code == 3 and "config or token file refused" in msg
    assert len(notes(home, "tg-adapter-down-")) == 1


def test_crash_loop_backoff(home, fake_adapter):
    write_config(home)
    write_token(home)
    fake(home, "run", "exit:1")
    for _ in range(3):
        assert ctl.ensure()[0] == 0
        assert wait_for(exited)
        assert ctl.last_exit(ctl.history())[1] == 1
    code, msg = ctl.ensure()
    assert code == 4 and msg.startswith("crash loop: 3 starts in 15 min")
    assert ctl.ensure()[0] == 4 and fake_calls(home).count("run") == 3
    assert len(notes(home, "tg-adapter-crashloop-")) == 1
    # 15 min later it tries again
    later = time.time() + 901
    r = run_cli("ensure", env={"ASSISTANT_NOW": str(later)})
    assert r.returncode == 0 and r.stdout.startswith(b"tg-adapter: started")


def test_install_when_node_modules_missing(home, fake_adapter):
    write_config(home)
    write_token(home)
    (fake_adapter / "node_modules").rmdir()
    assert ctl.ensure()[1].startswith("started")
    assert (fake_adapter / "node_modules").is_dir()
    ctl.stop()
    (fake_adapter / "node_modules").rmdir()
    os.environ["FAKE_INSTALL_RC"] = "1"
    try:
        code, msg = ctl.ensure()
    finally:
        del os.environ["FAKE_INSTALL_RC"]
    assert code == 1 and msg.startswith("bun install failed")


def test_restart_when_the_adapter_tree_changes(home, fake_adapter):
    git = ["git", "-C", str(fake_adapter), "-c", "user.name=t", "-c", "user.email=t@t.invalid"]
    (fake_adapter / ".gitignore").write_text("node_modules/\n")
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "one"], check=True)
    write_config(home)
    write_token(home)
    assert ctl.ensure()[1].startswith("started")
    first = ctl.running_pid()
    assert ctl.ensure()[1].startswith("running") and ctl.status()["tree_current"] is True
    (fake_adapter / "src" / "cli.ts").write_text("// fake v2\n")
    subprocess.run([*git, "commit", "-qam", "two"], check=True)
    assert ctl.status()["tree_current"] is False
    code, msg = ctl.ensure()
    assert code == 0 and msg.startswith("started") and ctl.running_pid() != first
    assert any("restart tree" in kind + " " + rest for _, kind, rest in ctl.history())


def test_passthrough_to_the_bun_cli(home, fake_adapter):
    r = run_cli("check")
    assert r.returncode == 0 and fake_calls(home)[-1] == "check"
    fake(home, "check_rc", "2")
    assert run_cli("check").returncode == 2
    assert run_cli("nonsense").returncode == 2


def test_instances_live_config_state_and_legacy_adapter(home, fake_adapter):
    """The live instance has its own config, switch and state dir; ensure starts it with --instance live and
    leaves the test instance alone; an older test adapter (PID file in the state root) is stopped and replaced."""
    import json
    import subprocess as sp
    import sys as _sys

    write_token(home)
    assert ctl.ensure(instance="live") == (0, "not configured (no ~/.config/tg-adapter/live.json): nothing runs")
    live = home / ".config" / "tg-adapter" / "live.json"
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_text(
        json.dumps({"mode": "live", "bot": "testadapter_bot", "token_file": "~/tok/test.env", "allow_from": ["111"]})
    )
    code, msg = ctl.ensure(instance="live")
    assert code == 0 and msg.startswith("started"), msg
    assert ctl.running_pid("live") and ctl.running_pid("test") is None
    assert "run --instance live" in fake_calls(home, raw=True)
    assert (home / ".local/state/tg-adapter/live/ensure.log").exists()
    assert ctl.status("live")["instance"] == "live"
    (home / ".config/tg-adapter/live.off").write_text("")
    assert ctl.ensure(instance="live")[1].startswith("switch off (~/.config/tg-adapter/live.off)")
    assert ctl.stop(instance="live")[0] == 0 and ctl.running_pid("live") is None
    # an older adapter still running from the old layout
    root = home / ".local/state/tg-adapter"
    old = sp.Popen([_sys.executable, "-c", "import time; time.sleep(30)  # tg-adapter cli.ts"])
    (root / "adapter.pid").write_text(f"{old.pid}\ntg-adapter\n")
    try:
        write_config(home)
        assert ctl.legacy_pid() == old.pid
        code, msg = ctl.ensure()
        assert code == 0 and msg.startswith("started"), msg
        assert old.wait(timeout=10) is not None
        assert ctl.running_pid("test")
    finally:
        old.kill()


def test_passthrough_installs_node_modules_first(home, fake_adapter):
    """22.10: `tg-adapter check` on a checkout without node_modules ran bun without its packages."""
    import shutil

    from tgadapter import cli

    shutil.rmtree(fake_adapter / "node_modules")
    write_config(home)
    write_token(home)
    assert cli.main(["check"]) == 0
    assert (fake_adapter / "node_modules").is_dir()
    assert fake_calls(home)[-1] == "check"
