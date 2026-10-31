"""arr-restart: Radarr then Sonarr restarted through `unraid-do docker-restart`, skipped while an item imports or the
remux job runs (its lock; held through the restart), the wait for /api/v3/system/status, the MemAvailable line, the
off switch and --dry-run. Offline: a fake ~/bin/media, subprocess.run captured, a fake /proc/meminfo."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import itertools
from pathlib import Path

import pytest

from core import locks

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "arr-restart"


class ApiError(Exception):
    pass


class FakeArr:
    """get(svc, path) over per-service queues and status answers; every GET is logged as (svc, path)."""

    ApiError = ApiError

    def __init__(self):
        self.calls = []
        self.queue = {"radarr": [[]], "sonarr": [[]]}  # successive answers (the last one repeats)
        self.down = {"radarr": 0, "sonarr": 0}  # status calls that fail before it answers (-1: never)

    def get(self, svc, path, timeout=None):
        self.calls.append((svc, path))
        if path.startswith("/api/v3/queue"):
            q = self.queue[svc]
            recs = q.pop(0) if len(q) > 1 else q[0]
            return {"records": recs, "totalRecords": len(recs)}
        if path == "/api/v3/system/status":
            if self.down[svc]:
                self.down[svc] -= 1
                raise ApiError(f"cannot reach {svc}")
            return {"version": "6.4.4"}
        raise ApiError(f"{svc}: no route for GET {path}")


@pytest.fixture
def tool(tmp_path, monkeypatch):
    def make(fake=None, rc=0, **env):
        mem = tmp_path / "meminfo"
        vals = itertools.cycle(["1200000", "16300000"])
        base = {
            "ARR_RESTART_OFF": tmp_path / "off",
            "ARR_RESTART_MEMINFO": mem,
            "ARR_RESTART_UNRAID_DO": "/nonexistent/unraid-do",
            "ARR_RESTART_REMUX_LOCK": tmp_path / "remux" / "lock",
            "ARR_RESTART_REMUX_TV_LOCK": tmp_path / "remux-tv" / "lock",
            "ARR_RESTART_BLOCK_WAIT": 0,
            "ARR_RESTART_UP_WAIT": 60,
            "ARR_RESTART_POLL": 1,
        }
        base.update(env)
        for k, v in base.items():
            monkeypatch.setenv(k, str(v))
        ldr = importlib.machinery.SourceFileLoader("arr_restart_under_test", str(SCRIPT))
        mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(ldr.name, ldr))
        ldr.exec_module(mod)
        mod.M = fake or FakeArr()
        mod.SLEEP = lambda s: None
        mod.restarts = []
        mod.lock_busy_during = []

        def mem_avail():
            mem.write_text(f"MemTotal: 65618092 kB\nMemAvailable: {next(vals)} kB\n")
            return orig_mem()

        orig_mem = mod.mem_avail_gb
        mod.mem_avail_gb = mem_avail

        class R:
            returncode = rc
            stdout = ""
            stderr = "" if rc == 0 else "unraid-do: server refused"

        def run(argv, **kw):
            mod.restarts.append(argv[1:])
            lock = mod.SERVICES[argv[2]][2]
            fd = locks.acquire(lock, wait=0)
            mod.lock_busy_during.append(fd is None)
            locks.release(fd)
            return R()

        mod.RUN = run  # the module's own hook: subprocess.run itself stays untouched for other tests
        return mod

    return make


def test_restarts_radarr_then_sonarr_and_logs_memory(tool, capsys):
    mod = tool()
    assert mod.main([]) == 0
    assert mod.restarts == [["docker-restart", "radarr"], ["docker-restart", "sonarr"]]
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 2
    assert "radarr: restarted, up after" in out[0] and "host MemAvailable 1.2 -> 16.3 GB" in out[0]
    assert "sonarr: restarted" in out[1]
    # status is polled after each restart; the queue read is the targeted one (no movie/series payload)
    assert ("radarr", "/api/v3/system/status") in mod.M.calls
    assert ("radarr", "/api/v3/queue?page=1&pageSize=200&includeMovie=false") in mod.M.calls


def test_remux_lock_is_held_through_the_restart(tool):
    mod = tool()
    assert mod.main([]) == 0
    assert mod.lock_busy_during == [True, True]
    for svc in ("radarr", "sonarr"):  # released afterwards
        fd = locks.acquire(mod.SERVICES[svc][2], wait=0)
        assert fd is not None
        locks.release(fd)


def test_importing_item_skips_that_service_only(tool, capsys):
    fake = FakeArr()
    fake.queue["radarr"] = [[{"title": "Big.Epic.2001.2160p", "trackedDownloadState": "importPending"}]]
    fake.queue["sonarr"] = [[{"title": "x", "trackedDownloadState": "downloading"}]]
    mod = tool(fake)
    assert mod.main([]) == 0
    assert mod.restarts == [["docker-restart", "sonarr"]]
    out = capsys.readouterr().out
    assert "radarr: skipped, 1 queue item(s) importing (Big.Epic.2001.2160p)" in out


def test_sonarr_importing_skips_sonarr(tool, capsys):
    fake = FakeArr()
    fake.queue["sonarr"] = [[{"title": "Show.S01E01", "trackedDownloadState": "importing"}]]
    mod = tool(fake)
    assert mod.main([]) == 0
    assert mod.restarts == [["docker-restart", "radarr"]]
    assert "sonarr: skipped, 1 queue item(s) importing" in capsys.readouterr().out


def test_remux_run_in_progress_skips(tool, capsys, tmp_path):
    mod = tool()
    fd = locks.acquire(tmp_path / "remux" / "lock", wait=0)
    try:
        assert mod.main(["--only", "radarr"]) == 0
    finally:
        locks.release(fd)
    assert mod.restarts == []
    assert "radarr: skipped, a media-remux run is in progress" in capsys.readouterr().out


def test_remux_tv_run_in_progress_skips_sonarr(tool, capsys, tmp_path):
    mod = tool()
    fd = locks.acquire(tmp_path / "remux-tv" / "lock", wait=0)
    try:
        assert mod.main([]) == 0
    finally:
        locks.release(fd)
    assert mod.restarts == [["docker-restart", "radarr"]]
    assert "sonarr: skipped, a media-remux-tv run is in progress" in capsys.readouterr().out


def test_block_that_clears_within_the_wait_restarts(tool, capsys):
    fake = FakeArr()
    fake.queue["radarr"] = [[{"title": "a", "trackedDownloadState": "importing"}], []]
    mod = tool(fake, ARR_RESTART_BLOCK_WAIT=300)
    assert mod.main(["--only", "radarr"]) == 0
    assert mod.restarts == [["docker-restart", "radarr"]]


def test_status_never_answers_is_a_failure(tool, capsys):
    fake = FakeArr()
    fake.down["radarr"] = -1
    mod = tool(fake, ARR_RESTART_UP_WAIT=0)
    assert mod.main([]) == 2
    out = capsys.readouterr().out
    assert "radarr: restarted, but /api/v3/system/status did not answer" in out
    assert "sonarr: restarted, up after" in out  # the second one still runs


def test_status_answers_after_a_few_tries(tool):
    fake = FakeArr()
    fake.down["radarr"] = 3
    mod = tool(fake)
    assert mod.main(["--only", "radarr"]) == 0
    assert mod.M.calls.count(("radarr", "/api/v3/system/status")) == 4


def test_failed_docker_restart_exits_2(tool, capsys):
    mod = tool(rc=3)
    assert mod.main(["--only", "sonarr"]) == 2
    assert "sonarr: docker-restart failed (exit 3: unraid-do: server refused)" in capsys.readouterr().out


def test_unreadable_queue_is_not_restarted(tool, capsys):
    class Down(FakeArr):
        def get(self, svc, path, timeout=None):
            raise ApiError(f"cannot reach {svc}")

    mod = tool(Down())
    assert mod.main(["--only", "radarr"]) == 2
    assert mod.restarts == []
    assert "radarr: cannot read its queue" in capsys.readouterr().out


def test_off_switch_and_dry_run(tool, capsys, tmp_path):
    mod = tool()
    assert mod.main(["--dry-run"]) == 0
    assert mod.restarts == []
    assert "radarr: would restart (dry run)" in capsys.readouterr().out
    (tmp_path / "off").touch()
    assert mod.main([]) == 0
    assert mod.restarts == [] and "off switch" in capsys.readouterr().out
