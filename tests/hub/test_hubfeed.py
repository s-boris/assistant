"""Unit tests for hubfeed (no Postgres, no network beyond loopback): the parts registry and its states, the brain's
report and watch rules, gateway counts from a decision log, change-only publishing, and a snapshot that
carries states, counts and ids only. The end-to-end suite (hubd + Postgres + the stream) is
tests/hubstore/test_status_feed.py."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import socket
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("hubfeed_t", REPO / "packages" / "hub" / "hubfeed.py")
assert _spec and _spec.loader
hf = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = hf  # dataclasses look the module up while the class is built
_spec.loader.exec_module(hf)


def calls_db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE calls (id INTEGER PRIMARY KEY, ts REAL NOT NULL, tool TEXT, domain TEXT, decision TEXT,"
        " code TEXT, ms_total INTEGER, test INTEGER)"
    )
    return db


class FakeGW:
    def __init__(self, state: str = "ok", db: sqlite3.Connection | None = None, domains=("ha", "media")) -> None:
        self.state = state
        typed = {f"{d}.x": SimpleNamespace(domain=d) for d in domains}
        db = db or calls_db()
        self.gw = SimpleNamespace(
            reg=SimpleNamespace(typed=typed),
            store=SimpleNamespace(db=db),
            health=lambda: {"tools": len(typed), "executor": {"online": True}, "mode": {"enforce": []}},
        )

    def brief(self) -> dict:
        return {"state": self.state}


def feed(probes=None, gw=None, query_one=None) -> hf.Feed:
    return hf.Feed(
        probes=probes or {"pg": lambda: "ok", "phoenix": lambda: "down", "ci": lambda: "stale"},
        query_one=query_one,
        gateway=lambda: gw,
        release="abcdef1234567890",
        phoenix_url="",
    )


def run(coro):
    return asyncio.run(coro)


def states(f: hf.Feed) -> dict[str, str]:
    return {p["id"]: p["state"] for p in f.compose()["parts"]}


def test_registry_ids_are_map_ids_and_unique():
    ids = [p.id for p in hf.PARTS]
    assert len(ids) == len(set(ids))
    assert all(hf.ID_RE.match(i) for i in ids)
    assert {"claude-hub", "claude-hub-pg", "claude-hub-phoenix", "claude-hub-ci", "hub-gateway"} <= set(ids)


def test_part_states_from_probes_gateway_and_domains():
    db = calls_db()
    now = time.time()
    db.executemany(
        "INSERT INTO calls (ts, tool, domain, decision, code, ms_total, test) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(now - 10, "ha.x", "ha", "allow", "unavailable", 30, 0), (now - 20, "media.x", "media", "allow", "ok", 5, 0)],
    )
    f = feed(gw=FakeGW("ok", db))

    async def go():
        await f.stats()
        await f.probe_parts()

    run(go())
    s = states(f)
    assert s["claude-hub"] == "ok" and s["claude-hub-pg"] == "ok"
    assert s["claude-hub-phoenix"] == "down" and s["claude-hub-ci"] == "warn"
    assert s["claude-hub-tg"] == "unknown" and s["hub-gateway"] == "ok"  # no tg probe injected here
    assert s["domain-ha"] == "warn" and s["domain-media"] == "ok"  # a fault in the last 15 min
    assert s["tg-adapter"] == "unknown"
    # the gateway goes away: it and every domain read down
    f.gateway = lambda: None
    run(f.probe_parts())
    s = states(f)
    assert s["hub-gateway"] == "down"
    assert not [k for k in s if k.startswith("domain-")]  # no registry, no domains


def test_adapter_containers_are_live_parts():
    """claude-hub-tg, -voice, -browser and -phone run: probed parts (map ids), none planned."""
    by = {p.id: p for p in hf.PARTS}
    for pid, probe in (("claude-hub-tg", "tg"), ("claude-hub-voice", "voice"), ("claude-hub-browser", "browser"),
                       ("claude-hub-phone", "phone")):  # fmt: skip
        assert by[pid].probe == probe and by[pid].kind == "container" and probe in hf.PROBED
    assert not [p.id for p in hf.PARTS if p.probe == "planned"]


def test_loader_probe_words():
    lp = hf.loader_probe
    assert lp(None) == "no report" and lp({}) == "no report"
    assert lp({"state": "running", "age_s": 3.0}) == "ok"
    assert lp({"state": "running", "age_s": 61.0}) == "silent"  # the container stopped reporting
    assert lp({"state": "running", "age_s": None}) == "silent" and lp({"state": "running", "age_s": True}) == "silent"
    assert lp({"state": "held", "age_s": 1}) == "held" and lp({"state": "starting", "age_s": 1}) == "starting"
    assert lp({"state": "conflict", "age_s": 1}) == "conflict" and lp({"state": "weird", "age_s": 1}) == "unknown"
    ps = hf.probe_state
    assert [ps(w) for w in ("ok", "no report", "unset", "stale", "starting", "idle", "held", "silent", "down",
                            "conflict", "missing")] == ["ok", "unknown", "unknown", "warn", "warn", "warn", "off",
                                                        "down", "down", "down", "down"]  # fmt: skip


def test_adapter_container_states_from_their_probes():
    box = {"tg": {"state": "running", "age_s": 2.0}, "voice": {"state": "held", "age_s": 4.0}, "phone": None}
    f = feed(probes={
        "tg": lambda: hf.loader_probe(box["tg"]),
        "voice": lambda: hf.loader_probe(box["voice"]),
        "phone": lambda: hf.loader_probe(box["phone"]),
        "browser": lambda: "ok",
    })  # fmt: skip
    run(f.probe_parts())
    s = states(f)
    assert (s["claude-hub-tg"], s["claude-hub-voice"], s["claude-hub-phone"], s["claude-hub-browser"]) == (
        "ok", "off", "unknown", "ok")  # fmt: skip
    assert f.parts["claude-hub-voice"]["detail"] == "held" and f.parts["claude-hub-phone"]["detail"] == "no report"
    box["tg"] = {"state": "running", "age_s": 90.0}
    run(f.probe_parts())
    assert f.parts["claude-hub-tg"]["state"] == "down" and f.parts["claude-hub-tg"]["detail"] == "silent"
    # a probe that raises reads unknown, never breaks the round
    f.probes["browser"] = lambda: 1 / 0
    run(f.probe_parts())
    assert states(f)["claude-hub-browser"] == "unknown"


def test_since_moves_only_on_a_state_change():
    box = {"pg": "ok"}
    f = feed(probes={"pg": lambda: box["pg"]})
    run(f.probe_parts())
    t0 = f.parts["claude-hub-pg"]["since"]
    time.sleep(0.02)
    run(f.probe_parts())
    assert f.parts["claude-hub-pg"]["since"] == t0
    time.sleep(0.15)  # since is kept to 0.1 s
    box["pg"] = "down"
    run(f.probe_parts())
    assert f.parts["claude-hub-pg"]["state"] == "down" and f.parts["claude-hub-pg"]["since"] > t0


def test_gateway_stats_windows_and_test_calls_left_out():
    db = calls_db()
    now = time.time()
    rows = [
        (now - 60, "ha.x", "ha", "allow", "ok", 10, 0),
        (now - 120, "ha.x", "ha", "deny", "policy_denied", 2, 0),
        (now - 7200, "ha.x", "ha", "allow", "bad_input", 3, 0),
        (now - 30, "ha.x", "ha", "allow", "ok", 999, 1),  # a test call
        (now - 90000, "ha.x", "ha", "allow", "ok", 1, 0),  # older than 24 h
    ]
    db.executemany("INSERT INTO calls (ts, tool, domain, decision, code, ms_total, test) VALUES (?,?,?,?,?,?,?)", rows)
    g = hf.gateway_stats(db, now)
    assert (g["calls_1h"], g["errors_1h"], g["denies_1h"]) == (2, 1, 1)
    assert (g["calls_24h"], g["errors_24h"], g["denies_24h"]) == (3, 2, 1)
    assert g["domains"]["ha"] == {"calls_24h": 3, "errors_24h": 2, "faults_15m": 0}
    assert g["p95_ms_24h"] == 10


def test_brain_report_rules_and_staleness():
    f = feed()
    with pytest.raises(hf.BadInput):
        f.brain_report({"text": "hi"})
    with pytest.raises(hf.BadInput):
        f.brain_report({"tg": {"sent_24h": True}})
    with pytest.raises(hf.BadInput):
        f.brain_report({"parts": {"claude-hub-pg": {"state": "ok"}}})  # the hub probes its own parts
    with pytest.raises(hf.BadInput):
        f.brain_report({"parts": {"tg-adapter": {"state": "ok", "detail": "x" * 41}}})
    f.brain_report({"parts": {"tg-adapter": {"state": "ok", "detail": "running"}}, "ttl": 30})
    run(f.probe_parts())
    assert f.parts["tg-adapter"]["state"] == "ok"
    f.brain["at"] -= 31  # the report outlives its ttl
    run(f.probe_parts())
    assert f.parts["tg-adapter"] == {**f.parts["tg-adapter"], "state": "unknown", "detail": "report stale"}


def test_brain_pending_approvals_drive_live_pending():
    f = feed()
    for bad in ({"approvals": {"pending": -1}}, {"approvals": {"pending": True}}, {"approvals": {"summary": "x"}}):
        with pytest.raises(hf.BadInput):
            f.brain_report(bad)
    f.brain_report({"stores": {"approvals": "sqlite"}, "approvals": {"pending": 2}, "ttl": 30})
    assert f.compose()["approvals"]["live_pending"] == 2
    f.brain["at"] -= 31  # stale report: unknown, never a stale number
    assert f.compose()["approvals"]["live_pending"] is None
    f.blocks["approvals"]["pending"] = 1
    f.brain_report({"stores": {"approvals": "hub"}, "approvals": {"pending": 5}})
    assert f.compose()["approvals"]["live_pending"] == 1  # the hub store is live: its own count


def test_watch_rules_probe_and_expiry():
    f = feed()
    with pytest.raises(hf.BadInput):
        f.set_watch({"id": "hub-gateway", "tcp": "127.0.0.1:1"})
    with pytest.raises(hf.BadInput):
        f.set_watch({"id": "domain-ha", "tcp": "127.0.0.1:1"})
    with pytest.raises(hf.BadInput):
        f.set_watch({"id": "w-1", "tcp": "127.0.0.1:1", "ttl": 10})
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    f.set_watch({"id": "w-1", "tcp": f"127.0.0.1:{srv.getsockname()[1]}", "ttl": 30, "label": "throwaway"})
    run(f.probe_parts())
    assert f.parts["w-1"]["state"] == "ok" and f.parts["w-1"]["kind"] == "watch"
    srv.close()
    run(f.probe_parts())
    assert f.parts["w-1"]["state"] == "down"
    f.watch["w-1"]["until"] = time.monotonic() - 1
    run(f.probe_parts())
    assert "w-1" not in f.parts
    for i in range(hf.MAX_WATCH):
        f.set_watch({"id": f"w-{i + 10}", "tcp": "127.0.0.1:9"})
    with pytest.raises(hf.BadInput):
        f.set_watch({"id": "w-99", "tcp": "127.0.0.1:9"})


def test_publish_only_on_change_and_to_every_client():
    f = feed()
    run(f.probe_parts())
    q: asyncio.Queue[str] = asyncio.Queue(maxsize=4)
    f.clients.add(q)
    assert f.publish() is True and f.seq == 1
    assert f.publish() is False and f.seq == 1  # nothing changed: no frame, no new seq
    f.parts["claude-hub"]["state"] = "warn"
    assert f.publish() is True and f.seq == 2
    frames = [q.get_nowait() for _ in range(q.qsize())]
    assert len(frames) == 2 and frames[-1].startswith("event: feed\nid: 2\ndata: ")
    for _ in range(6):  # a slow client keeps only the newest frames
        f.parts["claude-hub"]["state"] = "ok" if f.parts["claude-hub"]["state"] != "ok" else "warn"
        f.publish()
    assert q.qsize() == 4


def test_snapshot_is_states_counts_and_ids_only():
    async def q1(sql, params):
        return {
            "ap_pending": 2,
            "ap_decided_24h": 1,
            "ob_queued": 0,
            "ob_sending": 0,
            "ob_failed_24h": 0,
            "ob_sent_24h": 3,
            "dl_sent_24h": 3,
            "dl_receipts_24h": 3,
            "consumers": 1,
            "ev_open": 4,
            "ev_new": 1,
        }

    f = feed(gw=FakeGW("ok"), query_one=q1)
    f.brain_report({"stores": {"approvals": "hub"}, "tg": {"sent_24h": 5}})

    async def go():
        await f.stats()
        await f.probe_parts()

    run(go())
    f.publish()
    s = f.snap
    assert s["approvals"] == {"state": "ok", "pending": 2, "decided_24h": 1, "store": "hub", "live_pending": 2}
    assert s["tg"]["hub"]["sent_24h"] == 3 and s["tg"]["brain"]["sent_24h"] == 5
    assert s["events"] == {"open": 4, "new": 1, "store": None}

    def leaves(o):
        if isinstance(o, dict):
            for v in o.values():
                yield from leaves(v)
        elif isinstance(o, list):
            for v in o:
                yield from leaves(v)
        else:
            yield o

    for v in leaves(s):  # no free text: short words, numbers, booleans, null
        assert v is None or isinstance(v, (int, float, bool)) or (isinstance(v, str) and len(v) <= 40), v
    json.dumps(s)


def test_release_ships_the_feed():
    sys.path.insert(0, str(REPO / "packages"))
    from hubkit import release

    assert release.FILES["app/hubfeed.py"] == "packages/hub/hubfeed.py"


def test_brain_slo_tiles_validated_and_composed():
    """The brain's SLO tiles: fixed ids, statuses and units, numbers only; shown while the report is fresh."""
    f = feed()
    good = {"id": "first_reply", "value": 63.2, "target": 20.0, "op": "<", "unit": "s", "n": 493, "status": "miss",
            "window_days": 7}  # fmt: skip
    for bad in ({"slo": "x"}, {"slo": [{**good, "label": "hi"}]}, {"slo": [{**good, "id": "x"}]},
                {"slo": [{**good, "status": "red"}]}, {"slo": [{**good, "value": "63"}]},
                {"slo": [{**good, "n": True}]}, {"slo": [good] * 13}):  # fmt: skip
        with pytest.raises(hf.BadInput):
            f.brain_report(bad)
    f.brain_report({"ttl": 30})
    assert "slo" not in f.compose()  # the brain's switch is off: no block
    f.brain_report({"slo": [good, {**good, "id": "vault_answer", "value": None, "n": 0, "status": "no-data"}],
                    "ttl": 30})  # fmt: skip
    blk = f.compose()["slo"]
    assert blk["fresh"] is True and [t["id"] for t in blk["tiles"]] == ["first_reply", "vault_answer"]
    f.brain["at"] -= 31
    assert f.compose()["slo"]["fresh"] is False


def test_ci_runner_slots_and_queue_from_the_spool(tmp_path):
    now = time.time()
    assert hf.ci_runner(str(tmp_path), now)["state"] == "unknown"  # no runner yet: no heartbeat, no job dirs
    for d in ("jobs", "run"):
        (tmp_path / "ci" / d).mkdir(parents=True)
    (tmp_path / "ci" / "jobs" / "aaaaaaaaaaaaaaaa.json").write_text("{}")
    (tmp_path / "ci" / "jobs" / "aaaaaaaaaaaaaaaa.bundle").write_text("x")
    for jid in ("bbbbbbbbbbbbbbbb", "cccccccccccccccc"):
        (tmp_path / "ci" / "run" / f"{jid}.json").write_text("{}")
    (tmp_path / "ci" / "slots.json").write_text(json.dumps({"slots": 3, "by": "brain"}))
    hb = {"ts": now - 5, "slots": 3, "jobs": ["bbbbbbbbbbbbbbbb", "cccccccccccccccc"], "toolchain_ok": True}
    (tmp_path / "ci-heartbeat.json").write_text(json.dumps(hb))
    ci = hf.ci_runner(str(tmp_path), now)
    assert ci == {"state": "ok", "slots": 3, "set": 3, "busy": 2, "queued": 1, "heartbeat_s": 5.0, "toolchain_ok": True}
    assert hf.ci_runner(str(tmp_path), now + 120)["state"] == "warn"  # a silent runner
    f = feed()
    f.spool = str(tmp_path)
    run(f.stats())
    snap = f.compose()
    assert snap["ci"]["busy"] == 2 and "bbbbbbbbbbbbbbbb" not in json.dumps(snap)  # counts, never job ids
