"""core.hostmem: the host's free memory. Stdlib only.

This container's /proc/meminfo is the host's, so MemAvailable here is the server's (the hub's ci container's too:
Docker does not namespace /proc/meminfo; a memory limit shows only in the cgroup).

  read(path=None)           (MemAvailable kB, MemTotal kB), or None when unreadable
  avail_gb(path=None)       MemAvailable in GB, or None when unreadable
  min_gb(env, conf, default)  a guard threshold in GB: the env var, else the conf file's first line, else default
  low(min_gb, path=None)    "host MemAvailable 6.1 GB < 8 GB" while below min_gb, else None (also when min_gb <= 0
                            = off, or meminfo is unreadable: the guard never blocks on a reading it does not have)
  SearchGuard               the search budget of a job whose searches Radarr / Sonarr keep in memory until they
                            restart (the interactive-search release cache): before each search, take() raises
                            SearchStop when the host is low on memory or the cap is used up (check(): the same
                            test without counting)
  LAND_MIN_GB               the CI memory gate: the land queue (ops/assistant.py land_min_mem) starts no batch and
                            the hub's CI runner (packages/hub/ciworker.py) no job below this MemAvailable
  ci_workers(avail_gb)      pytest / legacy workers MemAvailable allows: CI_FLOOR_GB kept free, CI_WORKER_GB each
  sample(path, ...)         append one `ts MemAvailable_kB MemTotal_kB` row (tab-separated), keep_days of rows
  rows(path)                the sampler's rows as (epoch, avail_kB, total_kB)

GB here are decimal (kB / 1e6), as arr-restart prints them.
"""

from __future__ import annotations

import os
from datetime import datetime

from core import clock, jsonstate

__all__ = [
    "CI_FLOOR_GB",
    "CI_WORKER_GB",
    "LAND_MIN_GB",
    "MEMINFO",
    "SearchGuard",
    "SearchStop",
    "avail_gb",
    "ci_workers",
    "low",
    "min_gb",
    "read",
    "rows",
    "sample",
]

MEMINFO = "/proc/meminfo"
KB_PER_GB = 1e6
LAND_MIN_GB = 6.0  # a CI batch on a box short of memory pushed load to ~50 and starved the hub (DNS EAI_AGAIN)
CI_FLOOR_GB = 2.0  # CI workers leave this much MemAvailable free (ci_workers) ...
CI_WORKER_GB = 0.5  # ... and count this much each


def read(path: str | None = None) -> tuple[int, int] | None:
    vals: dict[str, int] = {}
    try:
        with open(path or MEMINFO, encoding="ascii", errors="replace") as f:
            for line in f:
                key, _, rest = line.partition(":")
                if key in ("MemAvailable", "MemTotal"):
                    vals[key] = int(rest.split()[0])
    except (OSError, ValueError, IndexError):
        return None
    if "MemAvailable" not in vals or "MemTotal" not in vals:
        return None
    return vals["MemAvailable"], vals["MemTotal"]


def avail_gb(path: str | None = None) -> float | None:
    m = read(path)
    return None if m is None else m[0] / KB_PER_GB


def ci_workers(avail: float) -> int:
    """Workers (pytest xdist or legacy suites) a CI run may start with avail GB of MemAvailable: CI_FLOOR_GB kept
    free, CI_WORKER_GB each, at least 1. Bites only on a host short of memory (28 CPUs need 16 GB)."""
    return max(1, int((avail - CI_FLOOR_GB) / CI_WORKER_GB))


def min_gb(env: str, conf: str | None, default: float) -> float:
    for raw in (os.environ.get(env), _first_line(conf)):
        if raw not in (None, ""):
            try:
                return float(raw)
            except ValueError:
                pass
    return default


def _first_line(path: str | None) -> str | None:
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return f.readline().strip()
    except OSError:
        return None


def low(threshold_gb: float, path: str | None = None) -> str | None:
    if threshold_gb <= 0:
        return None
    m = read(path)
    if m is None:
        return None
    avail = m[0] / KB_PER_GB
    if avail >= threshold_gb:
        return None
    return f"host MemAvailable {avail:.1f} GB < {threshold_gb:g} GB"


class SearchStop(Exception):
    """No more searches this run; str() = the line for the job's log."""


class SearchGuard:
    """take() before each search: SearchStop("memory guard: ...") while the host's MemAvailable is under min_gb,
    SearchStop("stop: ...") once cap searches are used. tally: a dict whose 'searches' counts across runs (a night's
    record in the job's state; the cap is then the night's), else the cap counts this run's searches only."""

    def __init__(self, threshold_gb: float, cap: int, label: str, meminfo: str | None = None,
                 tally: dict | None = None):  # fmt: skip
        self.threshold_gb, self.cap, self.label, self.meminfo, self.tally = threshold_gb, cap, label, meminfo, tally
        self.n = 0

    def used(self) -> int:
        return self.tally.get("searches", 0) if self.tally is not None else self.n

    def check(self) -> None:
        """Raise SearchStop when the next search may not run; counts nothing."""
        why = low(self.threshold_gb, self.meminfo)
        if why:
            raise SearchStop(f"memory guard: {why}: no more searches this run")
        if self.used() >= self.cap:
            raise SearchStop(f"stop: {self.label} search cap {self.cap} reached")

    def take(self) -> None:
        """check(), then count this search."""
        self.check()
        self.n += 1
        if self.tally is not None:
            self.tally["searches"] = self.tally.get("searches", 0) + 1


def sample(path: str, keep_days: float = 14, meminfo: str | None = None, slack_days: float = 1) -> str | None:
    """Append one row for now; None when meminfo is unreadable (nothing written). Rows older than keep_days go once
    the oldest is keep_days + slack_days old (one rewrite a day, atomic)."""
    m = read(meminfo)
    if m is None:
        return None
    now = clock.now()
    row = f"{datetime.fromtimestamp(now).astimezone().isoformat(timespec='seconds')}\t{m[0]}\t{m[1]}\n"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    old = rows(path, raw=True)
    if old and old[0][0] < now - (keep_days + slack_days) * 86400:
        cut = now - keep_days * 86400
        kept = "".join(line for ts, line in old if ts >= cut)
        jsonstate.write_text(path, kept + row, mode=0o644)
    else:
        with open(path, "a", encoding="utf-8") as f:
            f.write(row)
    return row.rstrip("\n")


def rows(path: str, raw: bool = False) -> list:
    """(epoch, avail_kB, total_kB) per parsable row in file order; raw = (epoch, line) instead."""
    out: list = []
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return out
    for line in lines:
        parts = line.rstrip("\n").split("\t")
        if len(parts) != 3:
            continue
        try:
            ts = datetime.fromisoformat(parts[0]).timestamp()
            avail, total = int(parts[1]), int(parts[2])
        except ValueError:
            continue
        out.append((ts, line if line.endswith("\n") else line + "\n") if raw else (ts, avail, total))
    return out
