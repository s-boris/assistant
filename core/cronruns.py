"""core.cronruns: the cron jobs' run log (cron-back, 29.10) for health and Phoenix. Stdlib only.

cron-fail appends one JSON line per run to ~/.local/state/cron-fail/runs-YYYYMMDD.jsonl:
{"ts": start epoch s, "job": its NAME, "rc": exit code, "ok": 1 when rc is 0 or one of its --ok codes, "ms": duration}.
The hub's job schedules used to give health.py and Phoenix one record per run; this keeps that for cron:
  runs(since, now)       the run rows since an epoch (oldest first; bad lines skipped)
  failures(rows)         {job: [ts of each failed run]}
  spans(rows)            one OTLP span per run: name "cron.<job>", start/end from ts/ms, job.rc / job.ms numbers,
                         status error when ok is 0 (core.otlp keeps names, times, status and numbers only)
  export(endpoint)       new rows since the cursor -> spans -> the local OTLP receiver (:4318, `traces otel`, which
                         forwards to Phoenix); the cursor moves only after a batch was taken; prunes run files older
                         than KEEP_DAYS -> (sent, pending)
State: ~/.local/state/cron-fail/ (runs-*.jsonl, export.json = the cursor {file: byte offset}). CRON_RUNS_DIR
overrides it (tests). Off switch: ~/.config/assistant/cron-runs-export-off stops the export (health still reads
the rows).
"""

from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

KEEP_DAYS = 8
BATCH = 200
SERVICE = "cron"


def state_dir() -> Path:
    return Path(os.environ.get("CRON_RUNS_DIR") or Path.home() / ".local" / "state" / "cron-fail")


def files(d: Path | None = None) -> list[Path]:
    return sorted((d or state_dir()).glob("runs-[0-9]*.jsonl"))


def _day(p: Path) -> str:
    return p.stem.split("-", 1)[1]


def _row(line: str) -> dict[str, Any] | None:
    try:
        r = json.loads(line)
    except ValueError:
        return None
    if not isinstance(r, dict) or not isinstance(r.get("job"), str):
        return None
    try:
        return {"ts": float(r["ts"]), "job": r["job"], "rc": int(r["rc"]), "ok": int(r["ok"]), "ms": int(r["ms"])}
    except (KeyError, TypeError, ValueError):
        return None


def runs(since: float, now: float | None = None, d: Path | None = None) -> list[dict[str, Any]]:
    now = time.time() if now is None else now
    first = time.strftime("%Y%m%d", time.localtime(since - 86400))
    out: list[dict[str, Any]] = []
    for p in files(d):
        if _day(p) < first:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for ln in text.splitlines():
            r = _row(ln)
            if r and since <= r["ts"] <= now:
                out.append(r)
    out.sort(key=lambda r: r["ts"])
    return out


def failures(rows: list[dict[str, Any]]) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    for r in rows:
        if not r["ok"]:
            out.setdefault(r["job"], []).append(r["ts"])
    return out


def spans(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from core import otlp

    out = []
    for r in rows:
        start = r["ts"]
        out.append(
            otlp.make_span(
                f"cron.{r['job']}",
                secrets.token_hex(16),
                start=start,
                end=start + r["ms"] / 1000,
                attrs={"span.type": "cron", "job.rc": r["rc"], "job.ms": r["ms"], "job.ok": r["ok"]},
                error=not r["ok"],
            )
        )
    return out


def _cursor(d: Path) -> dict[str, int]:
    try:
        c = json.loads((d / "export.json").read_text())
        return {k: int(v) for k, v in c.items()} if isinstance(c, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _save(d: Path, c: dict[str, int]) -> None:
    tmp = d / "export.json.tmp"
    tmp.write_text(json.dumps(c, sort_keys=True))
    tmp.replace(d / "export.json")


def off() -> bool:
    return (Path.home() / ".config" / "assistant" / "cron-runs-export-off").exists()


def export(endpoint: str | None = None, now: float | None = None, d: Path | None = None) -> tuple[int, int]:
    """New run rows -> spans -> the OTLP receiver, BATCH at a time; the cursor moves per accepted batch."""
    from core import otlp

    d = d or state_dir()
    now = time.time() if now is None else now
    cur = _cursor(d)
    oldest = time.strftime("%Y%m%d", time.localtime(now - KEEP_DAYS * 86400))
    sent = pending = 0
    for p in files(d):
        if _day(p) < oldest:
            p.unlink(missing_ok=True)
            cur.pop(p.name, None)
            continue
        off_ = cur.get(p.name, 0)
        try:
            with p.open("rb") as f:
                f.seek(off_)
                data = f.read()
        except OSError:
            continue
        end = data.rfind(b"\n") + 1  # only whole lines; a run being written stays for the next export
        lines = data[:end].decode("utf-8", "replace").splitlines(keepends=True)
        i = 0
        while i < len(lines):
            chunk = lines[i : i + BATCH]
            rows = [r for r in (_row(x) for x in chunk) if r]
            if rows and not otlp.emit(spans(rows), SERVICE, endpoint=endpoint, timeout=5.0):
                pending += sum(1 for x in lines[i:] if x.strip())
                break
            sent += len(rows)
            off_ += sum(len(x.encode()) for x in chunk)
            cur[p.name] = off_
            i += BATCH
        else:
            continue
        break  # the receiver refused: keep the rest (later files too) for the next run
    known = {p.name for p in files(d)}
    _save(d, {k: v for k, v in cur.items() if k in known})
    return sent, pending
