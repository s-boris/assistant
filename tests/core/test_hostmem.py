"""core.hostmem (MemAvailable reads, the low-memory guard, the search budget, the sampler's rows and their 14-day
retention) and the hostmem CLI (sample, tail, low). Offline: a fake /proc/meminfo in tmp_path, the clock pinned with
ASSISTANT_NOW."""

from __future__ import annotations

import importlib.machinery
import importlib.util
from datetime import datetime
from pathlib import Path

import pytest

from core import hostmem

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "hostmem"
NOW = "2026-10-31T22:00:00+01:00"


def meminfo(path: Path, avail_kb: int, total_kb: int = 65618092) -> Path:
    path.write_text(f"MemTotal:       {total_kb} kB\nMemFree:         1000000 kB\nMemAvailable:   {avail_kb} kB\n")
    return path


def test_read_parses_and_fails_soft(tmp_path):
    assert hostmem.read(str(meminfo(tmp_path / "m", 28600000))) == (28600000, 65618092)
    (tmp_path / "partial").write_text("MemTotal: 1 kB\n")
    assert hostmem.read(str(tmp_path / "partial")) is None
    (tmp_path / "junk").write_text("MemTotal: x kB\nMemAvailable: y kB\n")
    assert hostmem.read(str(tmp_path / "junk")) is None
    assert hostmem.read(str(tmp_path / "missing")) is None


def test_low_threshold_off_and_unknown(tmp_path):
    m = str(meminfo(tmp_path / "m", 6100000))
    assert hostmem.low(8, m) == "host MemAvailable 6.1 GB < 8 GB"
    assert hostmem.low(6, m) is None  # 6.1 >= 6
    assert hostmem.low(0, m) is None  # 0 = off
    assert hostmem.low(8, str(tmp_path / "missing")) is None  # no reading: never blocks


def test_ci_gate_shared_by_the_land_queue_and_the_runner(tmp_path):
    """avail_gb, LAND_MIN_GB and ci_workers: the numbers ops/assistant.py (land queue, CI driver) and the hub's CI
    runner (packages/hub/ciworker.py) both use."""
    assert hostmem.avail_gb(str(meminfo(tmp_path / "m", 3900000))) == 3.9
    assert hostmem.avail_gb(str(tmp_path / "missing")) is None
    assert hostmem.LAND_MIN_GB == 6.0 and hostmem.CI_FLOOR_GB == 2.0 and hostmem.CI_WORKER_GB == 0.5
    assert hostmem.ci_workers(3.9) == 3 and hostmem.ci_workers(1.0) == 1 and hostmem.ci_workers(60.0) == 116


def test_min_gb_env_then_conf_then_default(tmp_path, monkeypatch):
    conf = tmp_path / "min-mem-gb"
    monkeypatch.delenv("X_MIN_MEM_GB", raising=False)
    assert hostmem.min_gb("X_MIN_MEM_GB", str(conf), 8.0) == 8.0
    conf.write_text("12\n")
    assert hostmem.min_gb("X_MIN_MEM_GB", str(conf), 8.0) == 12.0
    monkeypatch.setenv("X_MIN_MEM_GB", "4.5")
    assert hostmem.min_gb("X_MIN_MEM_GB", str(conf), 8.0) == 4.5
    monkeypatch.setenv("X_MIN_MEM_GB", "lots")  # unparsable: the next source
    assert hostmem.min_gb("X_MIN_MEM_GB", str(conf), 8.0) == 12.0


def test_search_guard_memory_and_caps(tmp_path):
    m = meminfo(tmp_path / "m", 20000000)
    g = hostmem.SearchGuard(8, 2, "per-run", str(m))
    g.take()
    g.take()
    with pytest.raises(hostmem.SearchStop, match=r"^stop: per-run search cap 2 reached$"):
        g.take()
    # a night tally: the cap counts across runs, and every search lands in the tally
    tally = {"grabs": 0, "searches": 39}
    g = hostmem.SearchGuard(8, 40, "night", str(m), tally=tally)
    g.take()
    assert tally == {"grabs": 0, "searches": 40}
    with pytest.raises(hostmem.SearchStop, match="night search cap 40"):
        g.check()
    # memory first: low memory stops even with searches left, and counts nothing
    meminfo(m, 7900000)
    g = hostmem.SearchGuard(8, 40, "per-run", str(m))
    with pytest.raises(hostmem.SearchStop) as e:
        g.take()
    assert str(e.value) == "memory guard: host MemAvailable 7.9 GB < 8 GB: no more searches this run"
    assert g.n == 0


def test_sample_appends_and_keeps_14_days(tmp_path, monkeypatch):
    m = str(meminfo(tmp_path / "m", 28600000))
    f = tmp_path / "state" / "samples.tsv"
    monkeypatch.setenv("ASSISTANT_NOW", NOW)
    row = hostmem.sample(str(f), meminfo=m)
    ts = datetime.fromisoformat(NOW).astimezone().isoformat(timespec="seconds")
    assert row == f"{ts}\t28600000\t65618092"
    assert f.read_text() == row + "\n"
    # an unreadable meminfo writes nothing
    assert hostmem.sample(str(f), meminfo=str(tmp_path / "missing")) is None
    assert f.read_text().count("\n") == 1

    # 16 days of rows: the oldest is past 14 + 1 days -> one rewrite down to 14 days
    end = datetime.fromisoformat(NOW).timestamp()
    old = [
        f"{datetime.fromtimestamp(end - d * 86400).astimezone().isoformat(timespec='seconds')}\t{1000 + d}\t65618092\n"
        for d in range(16, 0, -1)
    ]
    f.write_text("".join(old) + "garbage line\n")
    hostmem.sample(str(f), meminfo=m)
    rows = hostmem.rows(str(f))
    assert [r[1] for r in rows] == [1000 + d for d in range(14, 0, -1)] + [28600000]
    assert all(r[0] >= end - 14 * 86400 for r in rows)
    # within the slack day nothing is rewritten: plain appends
    f.write_text(old[1])  # 15 days old exactly: not past 14 + 1
    hostmem.sample(str(f), meminfo=m)
    assert len(hostmem.rows(str(f))) == 2


@pytest.fixture
def cli(tmp_path, monkeypatch):
    def make(avail_kb=28600000, now=NOW):
        monkeypatch.setenv("HOSTMEM_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.setenv("HOSTMEM_MEMINFO", str(meminfo(tmp_path / "meminfo", avail_kb)))
        monkeypatch.setenv("ASSISTANT_NOW", now)
        ldr = importlib.machinery.SourceFileLoader("hostmem_cli_under_test", str(SCRIPT))
        mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(ldr.name, ldr))
        ldr.exec_module(mod)
        return mod

    return make


def test_cli_sample_tail_low(cli, tmp_path, capsys):
    mod = cli(avail_kb=12000000, now="2026-10-31T21:00:00+01:00")
    assert mod.main(["sample"]) == 0
    mod = cli(avail_kb=30000000, now="2026-10-31T21:10:00+01:00")
    assert mod.main(["sample"]) == 0
    assert capsys.readouterr().out == ""  # cron: silent when fine
    assert len((tmp_path / "state" / "samples.tsv").read_text().splitlines()) == 2
    assert mod.main(["tail", "1"]) == 0
    assert capsys.readouterr().out.strip().endswith("30.0 GB free of 65.6")
    assert mod.main(["low", "--hours", "2"]) == 0
    out = capsys.readouterr().out
    assert "2 samples: lowest 12.0 GB" in out and "mean 21.0 GB" in out and "latest 30.0 GB of 65.6 GB" in out


def test_cli_sample_fails_without_meminfo(cli, tmp_path, monkeypatch, capsys):
    mod = cli()
    monkeypatch.setattr(mod, "MEMINFO", str(tmp_path / "missing"))
    assert mod.main(["sample"]) == 2
    assert "cannot read MemAvailable" in capsys.readouterr().err
    assert not (tmp_path / "state" / "samples.tsv").exists()
    assert mod.main(["low"]) == 2
