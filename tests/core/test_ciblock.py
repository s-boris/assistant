"""core.ciblock: shared CI blocker keys, one-line redaction, the ~2 h dedupe, first-seen/cleared log and the 24 h
blocked-time stats the self-review's loop section reads."""

from __future__ import annotations

import json

import pytest

from core import ciblock

T0 = 1_800_000_000.0


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ASSISTANT_TEST", "1")
    monkeypatch.delenv("ASSISTANT_NOW", raising=False)
    return tmp_path


def log_lines(home):
    p = home / ".local/state/assistant/ci-blockers.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def test_key_is_stable_across_numbers_and_one_line_masks_secrets(home):
    a = ciblock.key_for("runner-submit", "HubError: bundle is 5 MB (limit 5 MB)")
    b = ciblock.key_for("runner-submit", "HubError: bundle is 6 MB (limit 5 MB)")
    assert a == b and a.startswith("runner-submit:huberror: bundle is # mb (limit # mb)")
    s = ciblock.one_line(
        f"failed\n  token=abcd1234 at {home}/x Authorization: Bearer Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MGFiY2RlZg"
    )
    assert "\n" not in s and "abcd1234" not in s and "Zm9vYmFy" not in s and str(home) not in s and "~/x" in s
    tree = "0123456789abcdef0123456789abcdef01234567"
    assert tree in ciblock.one_line(f"no record for {tree}")  # pure hex ids stay
    assert len(ciblock.one_line("word " * 200)) == 240


def test_seen_emits_first_then_dedupes_for_two_hours(home):
    k = "legacy:fifodemo"
    r1 = ciblock.seen(k, "b1", "fail-fast: fifodemo red on main", now=T0)
    assert r1["emit"] and r1["new"] and r1["first_seen"] == T0
    r2 = ciblock.seen(k, "b2", "fail-fast: fifodemo red on main", now=T0 + 600)
    assert not r2["emit"] and not r2["new"] and r2["count"] == 2
    other = ciblock.seen("runner-submit:x", "b2", "refused", now=T0 + 700)
    assert other["emit"]  # per key
    r3 = ciblock.seen(k, "b3", "still red", now=T0 + ciblock.DEDUPE_S + 1)
    assert r3["emit"] and not r3["new"] and r3["first_seen"] == T0  # first-seen kept while it lasts
    cur = {e["key"]: e for e in ciblock.current()}
    assert cur[k]["branch"] == "b3" and cur[k]["count"] == 3
    assert [x["op"] for x in log_lines(home)] == ["seen", "seen"]  # one line per first sighting


def test_clear_logs_duration_and_a_recurrence_is_new(home):
    ciblock.seen("legacy:a", "b", "red", now=T0)
    ciblock.seen("runner:lost:seed missing", "b", "lost", now=T0)
    assert ciblock.clear(prefix="runner", now=T0 + 900, why="signed green") == ["runner:lost:seed missing"]
    assert ciblock.clear(["nope"], now=T0 + 900) == []
    assert [e["key"] for e in ciblock.current()] == ["legacy:a"]
    cl = [x for x in log_lines(home) if x["op"] == "cleared"]
    assert cl == [{"ts": T0 + 900, "op": "cleared", "key": "runner:lost:seed missing", "first_seen": T0,
                   "secs": 900.0, "why": "signed green"}]  # fmt: skip
    again = ciblock.seen("runner:lost:seed missing", "c", "lost", now=T0 + 1000)
    assert again["new"] and again["emit"]
    assert ciblock.clear(now=T0 + 1100) == ["legacy:a", "runner:lost:seed missing"]
    assert ciblock.current() == []


def test_stats_union_longest_window_and_open(home):
    now = T0 + 30 * 3600
    ciblock.seen("legacy:old", "b", "x", now=T0)  # 26 h before the window start...
    ciblock.clear(["legacy:old"], now=T0 + 7 * 3600)  # ...cleared 1 h into it: 60 min count
    ciblock.seen("runner-submit:a", "b", "x", now=T0 + 10 * 3600)
    ciblock.seen("legacy:b", "b", "x", now=T0 + 10 * 3600 + 600)  # overlaps a: the union counts once
    ciblock.clear(["runner-submit:a"], now=T0 + 10 * 3600 + 1200)
    ciblock.clear(["legacy:b"], now=T0 + 10 * 3600 + 1800)
    ciblock.seen("legacy:open", "b", "x", now=now - 900)  # still open: counts until now
    st = ciblock.stats(24, now=now)
    assert st["n"] == 4 and st["open"] == 1
    assert st["secs"] == 3600 + 1800 + 900 and st["minutes"] == 105
    assert st["longest"] == {"key": "legacy:old", "secs": 3600.0}
    assert ciblock.summary_line(st) == "CI blocked: 4 blockers, 105 min total (longest: legacy:old, 60 min)"
    empty = ciblock.stats(24, now=T0 - 10)
    assert ciblock.summary_line(empty) == "CI blocked: 0 blockers, 0 min total (longest: none)"
