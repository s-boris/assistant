"""vault-events -> drive-brief's event window (event workflows runbook step 5): an HA drive start in the vault feed
opens `eventwf open drive ha-<start UTC minute> --minutes 360`, its end shortens it to end + 20 min; the same start seen
again opens nothing, a start older than 6 h opens nothing, a failed open is retried by the next run, --show only
prints. Runs the real script against the fake vault client of test_writers_vault_rave_unraid and a fake eventwf
that logs argv."""

from __future__ import annotations

import json
from pathlib import Path

from test_writers_vault_rave_unraid import overlay, ve

FAKE_EVENTWF = '#!/bin/sh\necho "$*" >> "$EVW_LOG"\n[ -f "$EVW_FAIL" ] && exit 1\nexit 0\n'


def drive(cursor: int, state: str, ts: str) -> dict:
    return {
        "cursor": cursor,
        "source": "homeassistant",
        "ts": ts,
        "chat_key": "drive",
        "label": "drive",
        "actionable": True,
        "priority": False,
        "ha": {"state": state, "significant": True},
    }


def page(*events: dict) -> dict:
    return {"events": list(events), "next_cursor": max([e["cursor"] for e in events] or [10]), "more": False}


def run(tmp_path: Path, home: Path, pg: dict, *args: str, fail: bool = False):
    b = tmp_path / "ovl"
    if not b.exists():
        overlay(tmp_path, ("vault-events",))
    evw = tmp_path / "fake-eventwf"
    evw.write_text(FAKE_EVENTWF, encoding="utf-8")
    evw.chmod(0o755)
    flag = tmp_path / "evw-fail"
    if fail:
        flag.write_text("1", encoding="utf-8")
    elif flag.exists():
        flag.unlink()
    extra = {"VAULT_EVENTS_EVENTWF": str(evw), "EVW_LOG": str(tmp_path / "evw.log"), "EVW_FAIL": str(flag)}
    r = ve(b, home, pg, *(args or ("--cron",)), extra=extra)
    assert r.returncode == 0, r.stderr
    return r


def calls(tmp_path: Path) -> list[str]:
    f = tmp_path / "evw.log"
    return f.read_text(encoding="utf-8").splitlines() if f.exists() else []


def win(home: Path) -> dict:
    f = home / ".local" / "state" / "vault-events" / "drive-window.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


# NOW = 14.10.2026 16:00 Europe/Zurich = 14:00 UTC
START = drive(20, "start", "2026-10-14T15:58:30+02:00")
END = drive(21, "end", "2026-10-14T15:59:30+02:00")


def test_start_opens_then_same_start_is_quiet(home: Path, tmp_path: Path) -> None:
    run(tmp_path, home, page(START))
    assert calls(tmp_path) == ["open drive ha-202610141358 --minutes 360"]
    w = win(home)
    assert w["key"] == "ha-202610141358" and w["ok"] is True
    st = json.loads((home / ".local" / "state" / "vault-events" / "status.json").read_text(encoding="utf-8"))
    assert st["drive_window"] == "opened 360 min"
    run(tmp_path, home, page(START))  # the fake client hands the same page again
    assert len(calls(tmp_path)) == 1


def test_end_shortens_to_tail(home: Path, tmp_path: Path) -> None:
    run(tmp_path, home, page(START))
    run(tmp_path, home, page(END))
    assert calls(tmp_path)[-1] == "open drive ha-202610141358 --minutes 20"  # 15:59:30 + 20 min - 16:00, rounded up
    assert win(home)["ok"] is True


def test_start_and_end_in_one_batch(home: Path, tmp_path: Path) -> None:
    run(tmp_path, home, page(START, END))
    assert calls(tmp_path) == ["open drive ha-202610141358 --minutes 20"]


def test_backlog_start_opens_nothing(home: Path, tmp_path: Path) -> None:
    run(tmp_path, home, page(drive(20, "start", "2026-10-14T09:30:00+02:00"), END))
    assert calls(tmp_path) == [] and win(home) == {}


def test_failed_open_is_retried(home: Path, tmp_path: Path) -> None:
    run(tmp_path, home, page(START), fail=True)
    assert win(home)["ok"] is False
    run(tmp_path, home, page())  # nothing new in the feed: the next run retries
    assert calls(tmp_path) == ["open drive ha-202610141358 --minutes 360"] * 2
    assert win(home)["ok"] is True


def test_show_only_prints(home: Path, tmp_path: Path) -> None:
    r = run(tmp_path, home, page(START), "--show")
    assert "drive window: would open ha-202610141358" in r.stdout
    assert calls(tmp_path) == [] and win(home) == {}


def test_other_ha_events_open_nothing(home: Path, tmp_path: Path) -> None:
    zone = dict(START, label="zone", ha={"place": "home", "significant": True})
    run(tmp_path, home, page(zone))
    assert calls(tmp_path) == []
