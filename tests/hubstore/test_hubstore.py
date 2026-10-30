"""The hub store (hubd + Postgres) and the brain side: spool, units of work, the agenda hook, ingest.
A throwaway Postgres + hubd per session (hubfix); every test works in its own t_* schema and a temp HOME."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid

import hubfix
import pytest


@pytest.fixture(scope="session")
def hubsrv(tmp_path_factory):
    why = hubfix.missing()
    if why:
        pytest.skip(why)
    root = tmp_path_factory.mktemp("hub")
    pg = hubfix.Pg(root).start()
    hub = hubfix.Hubd(root, pg, hubfix.vendor_tree(root / "vendor")).start()
    yield hub
    hub.stop()
    pg.discard()


@pytest.fixture
def schema(hubsrv):
    name = "t_" + uuid.uuid4().hex[:12]
    yield name
    hubsrv.call("POST", "/admin/drop", {"schema": name})


@pytest.fixture
def brain(tmp_path, monkeypatch, hubsrv, schema):
    home = tmp_path / "home"
    (home / ".config" / "assistant").mkdir(parents=True)
    (home / "inbox").mkdir()
    (home / "workspace").mkdir()
    tok = tmp_path / "hub-token"
    tok.write_text(hubsrv.tokens["brain"])
    os.chmod(tok, 0o600)
    for k, v in {
        "HOME": str(home), "ASSISTANT_TEST": "1", "HUB_URL": hubsrv.url, "HUB_TOKEN_FILE": str(tok),
        "HUB_SCHEMA": schema, "ASSISTANT_MAIN_CWD": str(home / "workspace"), "TZ": "Europe/Zurich",
    }.items():  # fmt: skip
        monkeypatch.setenv(k, v)
    return home


def api(hubsrv, schema, method, path, body=None, who="brain"):
    st, got = hubsrv.call(method, path, body, who=who, schema=schema)
    return st, got


def hub_events(hubsrv, schema):
    st, got = api(hubsrv, schema, "GET", "/admin/dump?table=events&limit=5000")
    assert st == 200
    return got["rows"]


# ------------------------------------------------------------------------------------------------ the API


def test_emit_dup_occurrence_and_rid(hubsrv, schema):
    e = {"kind": "note.t", "subject": "A", "idem_key": "a-1.md", "note_path": "/x/a-1.md", "rid": "r-aaaaaaaa"}
    assert api(hubsrv, schema, "POST", "/events/emit", e)[1]["dup"] is False
    assert api(hubsrv, schema, "POST", "/events/emit", e)[1]["replayed"] is True
    assert api(hubsrv, schema, "POST", "/events/emit", {**e, "rid": None})[1]["dup"] is True
    first = hub_events(hubsrv, schema)[0]
    snap = api(hubsrv, schema, "POST", "/ledger/snapshot", {"profile": "full"})[1]
    old = snap["rows"]["events"][0]
    ch = [{"t": "events", "op": "upd", "pk": old["id"], "set": {"status": "acked"}, "old": old}]
    assert api(hubsrv, schema, "POST", "/ledger/apply", {"changes": ch})[0] == 200
    assert api(hubsrv, schema, "POST", "/ledger/apply", {"changes": ch})[0] == 409  # stale old row
    got = api(hubsrv, schema, "POST", "/events/emit", {**e, "rid": None, "note_gone": True})[1]
    assert got["dup"] is False and got["id"] != first["id"]
    keys = sorted(r["idem_key"] for r in hub_events(hubsrv, schema))
    assert keys == ["a-1.md", f"a-1.md#{first['id']}"]


def test_outbox_claim_vs_withdraw_atomic(hubsrv, schema):
    ids = [api(hubsrv, schema, "POST", "/outbox", {"op": "send", "bot": "live", "chat": "1",
                                                   "body": {"text": f"m{i}"}})[1]["id"] for i in range(40)]  # fmt: skip
    won: dict[str, list[int]] = {"c1": [], "c2": [], "w": []}

    def consumer(name):
        for i in ids:
            if api(hubsrv, schema, "POST", f"/outbox/{i}/claim", {"attempts": 1}, who="tg")[1]["claimed"]:
                won[name].append(i)

    def withdrawer():
        for i in reversed(ids):
            if api(hubsrv, schema, "POST", f"/outbox/{i}/withdraw", {"why": "t"})[1]["ok"]:
                won["w"].append(i)

    ts = [threading.Thread(target=consumer, args=(n,)) for n in ("c1", "c2")] + [threading.Thread(target=withdrawer)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)
    every = won["c1"] + won["c2"] + won["w"]
    assert sorted(every) == sorted(ids)  # each row exactly once: claimed by one consumer or withdrawn
    assert api(hubsrv, schema, "POST", "/outbox/queued", {"bots": ["live"]})[0] == 403  # the brain never consumes


def test_approvals_roles_and_codes(hubsrv, schema):
    sha = "a" * 64
    n = api(hubsrv, schema, "POST", "/approvals", {"action_sha": sha, "summary": "Pay", "ttl": 60})[1]["nonce"]
    assert api(hubsrv, schema, "POST", "/approvals", {"action_sha": sha, "summary": "x"}, who="tg")[0] == 403
    assert api(hubsrv, schema, "POST", f"/approvals/{n}/decide", {"decision": "y", "by": "1"})[0] == 403
    assert api(hubsrv, schema, "POST", f"/approvals/{n}/consume", {"action_sha": sha})[1]["code"] == 5
    assert api(hubsrv, schema, "POST", f"/approvals/{n}/decide", {"decision": "y", "by": "1"}, who="tg")[1]["ok"]
    assert api(hubsrv, schema, "POST", f"/approvals/{n}/consume", {"action_sha": "b" * 64})[1]["code"] == 5
    assert api(hubsrv, schema, "POST", f"/approvals/{n}/consume", {"action_sha": sha})[1]["code"] == 0
    assert api(hubsrv, schema, "POST", f"/approvals/{n}/consume", {"action_sha": sha})[1]["code"] == 4
    m = api(hubsrv, schema, "POST", "/approvals", {"action_sha": sha, "summary": "Late", "ttl": 1})[1]
    late = {"decision": "y", "by": "1", "now": m["expires"] + 1}
    assert not api(hubsrv, schema, "POST", f"/approvals/{m['nonce']}/decide", late, who="tg")[1]["ok"]
    got = api(hubsrv, schema, "POST", f"/approvals/{m['nonce']}/consume", {"action_sha": sha, "now": m["expires"] + 1})
    assert got[1]["code"] == 3
    assert api(hubsrv, schema, "GET", "/approvals/audit")[1]["violations"] == 0


# ------------------------------------------------------------------------------------------------ brain side


def test_emit_hub_spool_and_ordered_replay(brain, hubsrv, schema, monkeypatch):
    from core import events, hub

    r = events.emit(kind="note.t", subject="first")
    assert r.id >= 10_000_000 and not r.dup and os.path.exists(r.note_path)
    monkeypatch.setenv("HUB_URL", f"http://127.0.0.1:{hubfix.free_port()}")  # hub down
    spooled = [events.emit(kind="note.t", subject=f"s{i}", note=False, idem_key=f"k{i}") for i in range(3)]
    assert all(s.id is None and "spooled" in (s.db_error or "") for s in spooled)
    assert hub.spool_depth() == 3
    monkeypatch.setenv("HUB_URL", hubsrv.url)
    assert events.update(idem_key="k1", payload={"x": 1}).status == "updated"  # drains first, then updates
    rows = [r for r in hub_events(hubsrv, schema) if r["idem_key"] in ("k0", "k1", "k2")]
    assert [r["idem_key"] for r in sorted(rows, key=lambda r: r["id"])] == ["k0", "k1", "k2"]
    assert json.loads(next(r for r in rows if r["idem_key"] == "k1")["payload"]) == {"x": 1}
    assert hub.spool_depth() == 0


def _seed_hub(hubsrv, schema):
    """Four notes, the note.* beat wake rule and an earlier beat on the hub."""
    from core import events

    for i in range(4):
        events.emit(kind="note.t", subject=f"note {i}")
    now = time.time()
    for table, row in (
        ("wake_rules", {"id": 1, "created": now, "kind_glob": "note.*", "klass": "beat", "why": "t", "active": 1}),
        ("beats", {"id": 1, "ts": now - 600, "source": "idle", "ok": 1, "ms": 3, "chars": 10, "listed": "[]"}),
    ):
        st, got = api(hubsrv, schema, "POST", "/admin/import", {"table": table, "rows": [row], "mode": "skip"})
        assert st == 200 and got["inserted"] == 1, got


def test_hook_beat_and_ack_routing(brain, hubsrv, schema):
    import io

    from inbox import hook

    from core import events

    _seed_hub(hubsrv, schema)
    new = events.emit(kind="note.t", subject="born on the hub")
    prompt = {"prompt": "[heartbeat] tick", "cwd": str(brain / "workspace")}
    out = io.StringIO()
    t0 = time.monotonic()
    assert hook.hook_main(io.StringIO(json.dumps(prompt)), out) == 0
    ms = (time.monotonic() - t0) * 1000
    text = json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]
    assert f"#{new.id}" in text and ms < 2000
    rows = {r["id"]: r for r in hub_events(hubsrv, schema)}
    assert rows[new.id]["status"] == "listed" and rows[new.id]["first_beat"] is not None
    res = events.ack_ids([new.id], note="done")
    assert all(r.ok for r in res), res
    assert not os.path.exists(new.note_path)
    assert {r["id"]: r for r in hub_events(hubsrv, schema)}[new.id]["status"] == "acked"


def test_ingest_on_hub(brain, hubsrv, schema):
    from inbox import cli

    note = brain / "inbox" / "watch-123.md"
    note.write_text("# A watch fired\n\nNext: look\n")
    os.utime(note, (time.time() - 60, time.time() - 60))
    assert cli.main(["ingest"]) == 0
    rows = [r for r in hub_events(hubsrv, schema) if r["idem_key"] == "watch-123.md"]
    assert len(rows) == 1 and rows[0]["status"] == "new"
    note.unlink()
    assert cli.main(["ingest"]) == 0
    assert [r["status"] for r in hub_events(hubsrv, schema) if r["idem_key"] == "watch-123.md"] == ["gone"]


def test_restarts_mid_write_lose_nothing(brain, hubsrv, schema, tmp_path, monkeypatch):
    """Two hard kills of hubd during a burst of writes (with and without idem keys): after the spool drains every
    write is there exactly once."""
    from core import events, hub

    own = hubfix.Hubd(tmp_path / "own", hubsrv.pg, hubsrv.vendor)
    own.tokens = hubsrv.tokens
    for who, t in hubsrv.tokens.items():
        (own.secrets / f"token_{who}").write_text(t)
    own.start()
    monkeypatch.setenv("HUB_URL", own.url)
    n = 240
    done = threading.Event()

    def writer():
        for i in range(n):
            if i % 2:
                events.emit(kind="note.t", subject=f"w{i}", note=False, idem_key=f"w{i}")
            else:
                events.emit(kind="note.t", subject=f"w{i}", note=False, payload={"seq": i})
        done.set()

    t = threading.Thread(target=writer)
    t.start()
    for _ in range(2):
        time.sleep(0.4)
        own.kill()
        time.sleep(0.3)
        own.start()
    t.join(120)
    assert done.is_set()
    for _ in range(20):
        if hub.drain(10)[1] == 0:
            break
    own.stop()
    rows = [r for r in hub_events(hubsrv, schema) if r["subject"].startswith("w")]
    subjects = sorted(r["subject"] for r in rows)
    assert len(rows) == n and len(set(subjects)) == n, (len(rows), len(set(subjects)))


def test_core_tg_hub_outbox(brain, hubsrv, schema):
    from core import tg

    cfg = brain / ".config" / "tg-adapter"
    cfg.mkdir(parents=True)
    (cfg / "live.json").write_text(json.dumps({"allow_from": ["123"]}))
    assert tg.route("tg")[0] == "direct"  # no consumer on the hub: the direct path
    api(hubsrv, schema, "POST", "/outbox/queued", {"bots": ["live"]}, who="tg")
    assert tg.route("tg")[0] == "hub"

    def consumer():
        for _ in range(100):
            rows = api(hubsrv, schema, "POST", "/outbox/queued", {"bots": ["live"]}, who="tg")[1]["rows"]
            for r in rows:
                if api(hubsrv, schema, "POST", f"/outbox/{r['id']}/claim", {"attempts": 1}, who="tg")[1]["claimed"]:
                    fields_sent = {"status": "sent", "tg_message_id": 777, "sent_ts": time.time()}
                    api(hubsrv, schema, "POST", f"/outbox/{r['id']}/set", {"fields": fields_sent}, who="tg")
                    return
            time.sleep(0.05)

    t = threading.Thread(target=consumer)
    t.start()
    out = tg.send("tg", "send", {"text": "hello"}, wait=8)
    t.join(10)
    assert out.status == "sent" and out.ids == [777] and out.row_id >= 10_000_000


def test_approvals_library_on_hub(brain, hubsrv, schema, monkeypatch):
    import approvals

    got = approvals.new("pay invoice 42", "Pay invoice 42")
    n = got["nonce"]
    assert approvals.get(n)["status"] == "pending"
    assert approvals.consume(n, "pay invoice 42")[0] == 5
    api(hubsrv, schema, "POST", f"/approvals/{n}/decide", {"decision": "y", "by": "1"}, who="tg")
    assert approvals.wait(n, timeout=2)[0] == 0
    assert approvals.consume(n, "pay invoice 43")[0] == 5
    assert approvals.consume(n, "pay invoice 42")[0] == 0
    assert approvals.consume(n, "pay invoice 42")[0] == 4
    assert approvals.audit()["violations"] == 0
    monkeypatch.setenv("HUB_URL", f"http://127.0.0.1:{hubfix.free_port()}")
    with pytest.raises(RuntimeError):
        approvals.new("x", "y")  # fails closed: nothing minted anywhere
    assert not os.path.exists(approvals.db_path())


def test_sqlite_untouched_by_hub_events(brain, hubsrv, schema):
    from core import db, events

    events.emit(kind="note.t", subject="hub only")
    if os.path.exists(db.default_path()):
        con = sqlite3.connect(db.default_path())
        assert con.execute("SELECT COUNT(*) FROM events WHERE kind='note.t'").fetchone()[0] == 0
        con.close()
