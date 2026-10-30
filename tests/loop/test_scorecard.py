"""The continuous scorecard (loop.scorecard, `scorecard`): metrics from their own stores (temp HOME: a traces.db,
deploys.jsonl, cron-fail's run log, a fake `vault`), the 3-hours-in-a-row rule against the 7-day baseline, no-data
instead of a number, one note per metric per day, the spans, the feed and the CLI."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest
from loop import scorecard as sc

from core import clock, notes

REPO = Path(__file__).resolve().parents[2]
NOW_ISO = "2026-10-22T12:20:00+02:00"
H = 3600.0


def _vault(p: Path, live: dict | None) -> str:
    body = {"llm": {"wait": {"live": live}}} if live is not None else {}
    p.write_text(f"#!/bin/sh\ncat <<'EOF'\n{json.dumps(body)}\nEOF\n")
    p.chmod(0o755)
    return str(p)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from core.paths import protected

    assert not protected(tmp_path), "tmp_path must lie outside the real home"
    h = tmp_path / "home"
    for d in ("inbox", "bin", ".local/state"):
        (h / d).mkdir(parents=True)
    for k in list(os.environ):
        if k.startswith(("ASSISTANT_", "LOOP_", "SCORECARD_", "CRON_RUNS_", "HUB_PHONE_")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("ASSISTANT_TEST", "1")
    return h


@pytest.fixture
def src(home: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setenv("ASSISTANT_NOW", NOW_ISO)
    monkeypatch.setenv("ASSISTANT_OTLP", "off")
    now = clock.now()
    h0 = sc._hour(now)
    st = home / ".local" / "state"
    tr = st / "traces" / "traces.db"
    tr.parent.mkdir(parents=True)
    con = sqlite3.connect(tr)
    cols = "start_ts REAL, end_ts REAL, cost REAL, input INT, output INT, cache_read INT, cache_w5 INT, cache_w1h INT"
    con.execute(f"CREATE TABLE turns ({cols}, trigger TEXT, is_open INT, first_reply_ms INT)")
    con.execute(f"CREATE TABLE agents ({cols}, dur_ms INT, file_status TEXT)")
    t = h0 - 2 * H - 6 * H  # the baseline ends where the first checked window starts
    k = 0
    while t > h0 - 9 * 86400:  # baseline: an owner turn every 2 h at 10 s, $1 and 1M tokens, a 100 s helper
        t -= 2 * H
        k += 1
        con.execute("INSERT INTO turns VALUES (?, ?, 1.0, 1000000, 0, 0, 0, 0, 'owner', 0, 10000)", (t, t + 30))
        con.execute("INSERT INTO agents VALUES (?, ?, 0.5, 10, 0, 0, 0, 0, 100000, 'final')", (t, t + 100))
    t = h0 - 8 * H
    while t < h0 + 0.2 * H:  # the last 8 h: a turn every 30 min at 60 s (slow), same cost per turn
        con.execute("INSERT INTO turns VALUES (?, ?, 1.0, 1000000, 0, 0, 0, 0, 'owner:telegram', 0, 60000)",
                    (t, t + 70))  # fmt: skip
        con.execute("INSERT INTO turns VALUES (?, ?, 0, 0, 0, 0, 0, 0, 'owner', 1, 99000)", (t + 1, None))  # open
        con.execute("INSERT INTO turns VALUES (?, ?, 0, 0, 0, 0, 0, 0, 'heartbeat', 0, 999000)", (t + 2, t + 9))
        t += 0.5 * H
    con.commit()
    con.close()
    # deploys.jsonl: green lands of ~300 s for a week (land_start), then land_cmd_start rows; a refused one
    dep = st / "assistant" / "deploys.jsonl"
    dep.parent.mkdir(parents=True)
    rows = []
    for i in range(60):
        e = h0 - 12 * H - 3 * H * (i + 1)
        rows.append({"op": "land", "ok": True, "land_start": e - 300, "end": e, "branch": "secret-name"})
    for i in range(8):
        e = h0 - 1.2 * H * i - 60
        rr = {"suite": {"ids": ["t1"]}} if i == 1 else {}
        rows.append({"op": "land", "ok": i != 3, "land_start": e - 12, "land_cmd_start": e - 280, "end": e,
                     "rerun": rr, "why": "ci" if i == 3 else None})  # fmt: skip
    rows.append({"op": "cron", "ok": False, "ts": h0 - 60})
    dep.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
    # cron-fail's run log: every 10 min a run, one failure a day
    cr = st / "cron-fail"
    cr.mkdir(parents=True)
    monkeypatch.setenv("CRON_RUNS_DIR", str(cr))
    by_day: dict[str, list[str]] = {}
    t = h0 - 8 * 86400
    n = 0
    while t < now:
        day = datetime.fromtimestamp(t).strftime("%Y%m%d")
        n += 1
        by_day.setdefault(day, []).append(json.dumps({"ts": t, "job": "x", "rc": 0 if n % 144 else 1,
                                                       "ok": 0 if n % 144 == 0 else 1, "ms": 50}))  # fmt: skip
        t += 600
    for day, lines in by_day.items():
        (cr / f"runs-{day}.jsonl").write_text("\n".join(lines) + "\n")
    monkeypatch.setenv("SCORECARD_VAULT_BIN", _vault(home / "bin" / "vault", {"n": 30, "p95_s": 40.0}))
    return {"now": now, "h0": h0, "home": home}


def _m(res: dict, sid: str) -> dict:
    return next(m for m in res["metrics"] if m["id"] == sid)


def test_three_slow_hours_regress_and_the_baseline_ignores_open_and_beat_turns(src):
    m = _m(sc.compute(), "reply_p95")
    assert m["base"] == 10.0 and m["base_n"] >= 20
    assert [h["value"] for h in m["hours"]] == [60.0, 60.0, 60.0] and all(h["n"] >= 5 for h in m["hours"])
    assert m["status"] == "regressed" and m["ratio"] == 6.0
    assert len(m["series"]) == sc.SERIES_H and m["series"][-1]["t"] == src["h0"]


def test_sums_are_scaled_to_the_window_and_helpers_count_finished_runs(src):
    res = sc.compute()
    cost = _m(res, "cost_day")
    assert cost["base"] == pytest.approx(18.0)  # a $1 turn + a $0.5 helper every 2 h = $18 per 24 h
    assert cost["base_n"] >= 50 and cost["status"] != "no-data"
    assert _m(res, "tokens_day")["base"] == pytest.approx(12 * 1_000_010)
    helper = _m(res, "helper_p95")
    assert helper["base"] == 100.0 and helper["status"] == "no-data"  # no helper ended in the last 6 h


def test_land_time_uses_the_whole_command_and_failures_count_attempts(src):
    res = sc.compute()
    land = _m(res, "land_p95")
    assert land["base"] == 300.0
    assert land["hours"][0]["value"] == 280.0  # land_cmd_start, not the 12 s land_start
    fail = _m(res, "land_fail")
    assert fail["hours"][0]["n"] == 8 and fail["hours"][0]["value"] == pytest.approx(1 / 8, abs=1e-4)
    assert fail["base"] == 0.0 and fail["base_n"] >= 20
    assert fail["status"] == "ok"  # 1 of 8 is over 1.5 x 0 but under the 0.15 floor


def test_too_few_samples_is_no_data_never_a_number(src, monkeypatch):
    monkeypatch.setenv("SCORECARD_TRACES_DB", str(src["home"] / "nope.db"))
    res = sc.compute()
    for sid in ("reply_p95", "helper_p95", "cost_day", "tokens_day"):
        m = _m(res, sid)
        assert m["status"] == "no-data" and m["value"] is None and "unreadable" in m["note"]
    v = _m(res, "vault_wait_p95")  # no run yet: no samples
    assert v["status"] == "no-data" and v["value"] is None


def test_one_slow_hour_is_high_not_regressed(src, monkeypatch):
    con = sqlite3.connect(src["home"] / ".local/state/traces/traces.db")
    con.execute("UPDATE turns SET first_reply_ms = 10000 WHERE start_ts < ?", (src["h0"] - 2 * H,))
    con.execute("UPDATE turns SET first_reply_ms = 200000 WHERE trigger = 'owner:telegram' AND start_ts >= ?",
                (src["h0"] - 2 * H,))  # fmt: skip
    con.commit()
    con.close()
    m = _m(sc.compute(), "reply_p95")
    assert [h["over"] for h in m["hours"]] == [True, True, False]
    assert m["status"] == "high"


def test_run_writes_one_note_per_metric_per_day(src, monkeypatch):
    res = sc.run(with_targets=False)
    assert res["summary"]["regressed"] >= 1
    inbox = src["home"] / "inbox"
    first = sorted(inbox.glob("scorecard-*.md"))
    assert len(first) == res["summary"]["regressed"] == len(res["notes"])
    text = next(p.read_text() for p in first if "first reply" in p.read_text())
    assert notes.check(text) == [] and "secret-name" not in text and "60.0 s vs baseline 10.0 s" in text
    # the next hour, same day: still regressed, no second note
    monkeypatch.setenv("ASSISTANT_NOW", "2026-10-22T13:20:00+02:00")
    sc.run(with_targets=False)
    assert sorted(inbox.glob("scorecard-*.md")) == first
    st = sc.load_state()
    assert st["alerts"]["reply_p95"] == "2026-10-22"
    assert [s[1:] for s in st["vault"]] == [[40.0, 30], [40.0, 30]] and st["vault_base"] == [40.0, 30]
    # a new day: a new note for what is still regressed
    st["alerts"] = {k: "2026-10-21" for k in st["alerts"]}
    sc.save_state(st)
    sc.run(with_targets=False)
    assert len(sorted(inbox.glob("scorecard-*.md"))) == 2 * len(first)


def test_vault_samples_build_the_hours_and_a_dead_vault_is_no_data(src, monkeypatch):
    for hh in (10, 11, 12):
        monkeypatch.setenv("ASSISTANT_NOW", f"2026-10-22T{hh}:05:00+02:00")
        sc.vault_sample(st := sc.load_state(), clock.now())
        sc.save_state(st)
    st = sc.load_state()
    m = _m(sc.compute(vault={"samples": st["vault"], "base": [10.0, 200]}), "vault_wait_p95")
    assert [h["value"] for h in m["hours"]] == [40.0, 40.0, 40.0] and m["status"] == "regressed"
    monkeypatch.setenv("SCORECARD_VAULT_BIN", _vault(src["home"] / "bin" / "vault2", None))
    st = sc.load_state()
    v = sc.vault_sample(st, clock.now())
    assert v["base"] is None
    assert _m(sc.compute(vault=v), "vault_wait_p95")["status"] == "no-data"


def test_spans_are_numbers_only_and_land_spans_move_the_cursor(src, monkeypatch):
    from core import otlp

    sent: list = []
    monkeypatch.setattr(otlp, "emit", lambda spans, service, **kw: sent.append((service, spans)) or True)
    st: dict = {}
    res = sc.compute()
    n = sc.spans(res, st, src["now"])
    assert sent[0][1][0]["name"] == "scorecard"
    attrs = sent[0][1][0]["attributes"]
    assert attrs and all(set(a["value"]) & {"intValue", "doubleValue"} for a in attrs)
    assert all(otlp.NUM_KEY_RE.match(a["key"]) for a in attrs)
    lands = [s for _, b in sent[1:] for s in b]
    week = [r for r in sc.land_rows(src["now"] - 7 * 86400, src["now"] + 1) if r["t"] > src["now"] - 7 * 86400]
    assert {s["name"] for s in lands} == {"land"} and n == 1 + len(lands) == 1 + len(week) and len(week) > 50
    assert sum(1 for s in lands if s["status"]["code"] == 2) == 1  # the one failed land
    assert "secret-name" not in json.dumps(sent)
    assert st["land_cursor"] == src["h0"] - 60  # the newest land's end
    sent.clear()
    assert sc.spans(res, st, src["now"]) == 1  # nothing new


def test_feed_is_the_last_run_while_fresh(src, monkeypatch):
    assert sc.feed() is None
    sc.run(with_targets=False)
    d = sc.feed()
    assert d and {m["id"] for m in d["metrics"]} == {s[0] for s in sc.SPECS} and d["rule"] == sc.RULE
    assert oct(os.stat(sc.feed_path()).st_mode & 0o777) == "0o600"
    monkeypatch.setenv("ASSISTANT_NOW", "2026-10-22T15:30:00+02:00")
    assert sc.feed() is None


def test_targets_read_the_loop_sources(src, monkeypatch, home):
    b = home / "loopbin"
    b.mkdir()

    def script(name: str, obj: object) -> None:
        p = b / name
        p.write_text(f"#!/bin/sh\ncat <<'EOF'\n{json.dumps(obj)}\nEOF\n")
        p.chmod(0o755)

    script(
        "traces",
        {
            "owner_turns": 20,
            "owner_first_p50": 9000,
            "owner_first_p95": 30000,
            "tools": 1000,
            "errors": 5,
            "error_rate": 0.005,
            "error_counts": {"Bash:hook:x": 4},
            "hook_blocks": 4,
        },
    )
    script("events", {"beats": {"beat_turns": 10, "tokens": {"mean": 300000, "p95": 600000}, "tokens_per_day": 9e6}})
    script("tool", {"domains": {"media": {"calls": 3, "denies": 0, "unexpected": 0}}})
    script("evals", {"run_id": "r1", "score": 0.93, "new_fail": []})
    script("hub", {"local": {"radarr": "shadow", "ha": "hub"}})
    monkeypatch.setenv("LOOP_BIN", str(b))
    tg = sc.targets(1)
    by = {r["id"]: r for r in tg["rows"]}
    assert by["G1"]["value"] == 300000 and by["G1"]["met"] is False and by["G1"]["exit"] == "E7"
    assert by["G2"]["value"] == 30.0 and by["G2"]["met"] is True and by["G2"]["exit"] == "E8"
    assert by["G3h"]["value"] == 4.0 and by["G3p"]["value"] == 0 and by["G4"]["value"] == 0
    assert by["E9"]["value"] is None and by["E9"]["met"] is None
    assert by["E10"]["value"] == 1 and by["E10"]["met"] is False
    assert tg["errors"] == []


def test_cli_show_and_help(src):
    env = dict(os.environ, ASSISTANT_LIB=str(REPO))
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "scorecard"), "--help"], capture_output=True,
                       text=True, env=env, check=False)  # fmt: skip
    assert r.returncode == 0 and "regress" in r.stdout
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "scorecard"), "show", "--json"], capture_output=True,
                       text=True, env=env, check=False)  # fmt: skip
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout)
    assert _m(d, "reply_p95")["status"] == "regressed" and d["summary"]["regressed"] >= 1
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "scorecard"), "show", "--days", "99"],
                       capture_output=True, text=True, env=env, check=False)  # fmt: skip
    assert r.returncode == 2


def test_push_p95_reads_the_phone_mirror_without_probes(src, monkeypatch):
    """push_p95: the app's ack time per push from the gateway mirror's push_log (ms columns), probes and unacked
    pushes left out; slow acks for the last day against a week of 2 s acks regress."""
    h0 = src["h0"]
    mirror = src["home"] / "mirror.db"
    con = sqlite3.connect(mirror)
    con.execute("CREATE TABLE push_log(id TEXT PRIMARY KEY, at INT, level TEXT, tag TEXT, delivered_ms INT,"
                " acked_ms INT, verb TEXT, action_at INT)")  # fmt: skip
    k = 0
    t = h0 - 26 * H
    while t > h0 - 8 * 86400:  # the baseline: a push every 3 h, acked in 2 s
        con.execute("INSERT INTO push_log VALUES (?, ?, 'info', 'x', 0, 2000, NULL, NULL)", (f"b{k}", int(t * 1000)))
        t -= 3 * H
        k += 1
    for i in range(8):  # the last day: one every 3 h, acked in 40 s; a probe and an unacked push in between
        t = h0 - 3 * H * i + 60
        con.execute("INSERT INTO push_log VALUES (?, ?, 'timed', 'x', 0, 40000, NULL, NULL)", (f"s{i}", int(t * 1000)))
        con.execute(
            "INSERT INTO push_log VALUES (?, ?, 'probe', 'probe', 0, 900000, NULL, NULL)", (f"p{i}", int(t * 1000))
        )
        con.execute("INSERT INTO push_log VALUES (?, ?, 'urgent', 'x', 0, NULL, NULL, NULL)", (f"u{i}", int(t * 1000)))
    con.commit()
    con.close()
    monkeypatch.setenv("HUB_PHONE_MIRROR", str(mirror))
    m = sc.metric("push_p95", src["now"])
    assert m["base"] == 2.0 and m["base_n"] >= 10 and m["hours"][0]["value"] == 40.0
    assert m["status"] == "regressed" and m["unit"] == "s"
    monkeypatch.setenv("HUB_PHONE_MIRROR", str(src["home"] / "none.db"))
    m = sc.metric("push_p95", src["now"])
    assert m["status"] == "no-data" and "unreadable" in m["note"] and m["value"] is None
