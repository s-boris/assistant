"""The Telegram transport on the hub (hubtg): the adapter's rows, meta, jobs and presence, the bridge's inbound
poll, deliveries and receipts, named reads, bindings, files. A throwaway Postgres + hubd (hubfix), each test in its
own t_* schema; job timings shortened through HUB_TG_TIMES. Never the real hub, never a real token."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid

import hubfix
import pytest

TIMES = "1,4,1"  # unclaimed after 1 s, companion seen 4 s, expiry grace 1 s


class TgHubd(hubfix.Hubd):
    def env(self) -> dict[str, str]:
        e = super().env()
        e["HUB_TGFILES"] = str(self.root / "tgfiles")
        e["HUB_TG_TIMES"] = TIMES
        return e


@pytest.fixture(scope="module")
def hub(tmp_path_factory):
    why = hubfix.missing()
    if why:
        pytest.skip(why)
    root = tmp_path_factory.mktemp("h6hub")
    for d in ("tgfiles/in", "tgfiles/out"):
        (root / d).mkdir(parents=True)
    pg = hubfix.Pg(root).start()
    h = TgHubd(root, pg, hubfix.vendor_tree(root / "vendor")).start()
    yield h
    h.stop()
    pg.discard()


@pytest.fixture
def sch(hub):
    name = "t_" + uuid.uuid4().hex[:12]
    yield name
    hub.call("POST", "/admin/drop", {"schema": name})


def call(hub, sch, method, path, body=None, who="brain", timeout=10.0):
    return hub.call(method, path, body, who=who, schema=sch, timeout=timeout)


def ok(hub, sch, method, path, body=None, who="brain", timeout=10.0):
    st, got = call(hub, sch, method, path, body, who, timeout)
    assert st == 200, (path, st, got)
    return got


def rid() -> str:
    return "t-" + uuid.uuid4().hex


def row(kind="tg.message", key=None, **kw):
    r = {"ts": time.time(), "source": "tg-adapter", "kind": kind, "subject": "s", "payload": json.dumps(
        {"chat_id": "42", "message_id": 7, "text": "hi", "update_id": 100}), "labels": "[]", "delivery": "now",
        "idem_key": key or f"tg:test:{uuid.uuid4().hex}", "status": "new"}  # fmt: skip
    r.update(kw)
    return r


def test_rows_poll_permissions(hub, sch):
    a, b = row(), row("tg.button", binding_receipt=True)
    c = row("outbox.failed", delivery="beat")
    got = ok(hub, sch, "POST", "/tg/rows", {"rows": [a, b, c], "rid": rid()}, who="tg")
    assert len(got["ids"]) == 3 and len(set(got["ids"])) == 3
    again = ok(hub, sch, "POST", "/tg/rows", {"rows": [a]}, who="tg")
    assert again["ids"] == got["ids"][:1], "a known idem_key answers its id"
    r1 = rid()
    first = ok(hub, sch, "POST", "/tg/rows", {"rows": [row()], "rid": r1}, who="tg")
    assert ok(hub, sch, "POST", "/tg/rows", {"rows": [row()], "rid": r1}, who="tg")["ids"] == first["ids"]
    rows = ok(hub, sch, "POST", "/tg/inbound/poll", {"after": 0})["rows"]
    assert [r["kind"] for r in rows] == ["tg.message", "tg.button", "tg.message"], "channel kinds only, id order"
    assert rows[1]["receipt_ts"] is not None and rows[1]["tries"] == 0, "binding_receipt writes the receipt"
    assert set(rows[0]) == {"id", "ts", "kind", "payload", "trace_id", "status", "tries", "receipt_ts"}
    assert call(hub, sch, "POST", "/tg/rows", {"rows": [row()]})[0] == 403, "rows are the tg caller's"
    assert call(hub, sch, "POST", "/tg/inbound/poll", {"after": 0}, who="tg")[0] == 403
    assert call(hub, sch, "POST", "/tg/rows", {"rows": [row("note.x")]}, who="tg")[0] == 400
    assert call(hub, sch, "POST", "/tg/rows", {"rows": [row(status="pending")]}, who="tg")[0] == 400
    assert call(hub, sch, "POST", "/tg/rows", {"rows": [row(idem_key="")]}, who="tg")[0] == 400
    assert call(hub, sch, "POST", "/tg/rows", {"rows": [row(payload="[1]")]}, who="tg")[0] == 400
    top = ok(hub, sch, "GET", "/tg/inbound/max")["id"]
    assert top == max(got["ids"] + first["ids"])
    one = ok(hub, sch, "GET", f"/tg/inbound/{got['ids'][1]}")["row"]
    assert one["kind"] == "tg.button"
    assert ok(hub, sch, "GET", f"/tg/inbound/{got['ids'][2]}")["row"] is None, "outbox.failed is no channel row"


def test_long_poll_wakes_on_insert(hub, sch):
    after = ok(hub, sch, "GET", "/tg/inbound/max")["id"]
    out = {}

    def poll():
        t0 = time.monotonic()
        out["rows"] = ok(hub, sch, "POST", "/tg/inbound/poll", {"after": after, "wait": 15}, timeout=30)["rows"]
        out["secs"] = time.monotonic() - t0

    t = threading.Thread(target=poll)
    t.start()
    time.sleep(0.5)
    ok(hub, sch, "POST", "/tg/rows", {"rows": [row()]}, who="tg")
    t.join(20)
    assert len(out["rows"]) == 1 and out["secs"] < 5
    t = threading.Thread(target=poll)
    after = out["rows"][0]["id"]
    t.start()
    time.sleep(0.5)
    got = ok(hub, sch, "POST", "/tg/bridge-event", {"kind": "bridge.handoff", "subject": "x", "payload": {"text": "t"},
              "labels": ["voice"], "delivery": "now", "idem_key": "h:" + uuid.uuid4().hex})  # fmt: skip
    t.join(20)
    assert out["rows"][0]["id"] == got["id"] and out["rows"][0]["kind"] == "bridge.handoff"
    assert ok(hub, sch, "POST", "/tg/bridge-event", {"kind": "bridge.handoff", "payload": {}, "labels": [],
              "idem_key": "h:dup", "rid": rid()})["id"] is not None  # fmt: skip
    assert ok(hub, sch, "POST", "/tg/bridge-event", {"kind": "bridge.handoff", "payload": {}, "labels": [],
              "idem_key": "h:dup"})["id"] is None, "a known idem_key: null"  # fmt: skip
    assert call(hub, sch, "POST", "/tg/bridge-event", {"kind": "tg.message", "idem_key": "x"})[0] == 400
    t0 = time.monotonic()
    assert ok(hub, sch, "POST", "/tg/inbound/poll", {"after": 10**12, "wait": 1})["rows"] == []
    assert 0.9 <= time.monotonic() - t0 < 4


def test_patch_and_meta(hub, sch):
    r = row("tg.button")
    rid_ = ok(hub, sch, "POST", "/tg/rows", {"rows": [r]}, who="tg")["ids"][0]
    got = ok(hub, sch, "POST", "/tg/rows/patch", {"idem_key": r["idem_key"], "payload_patch": {"ack_ms": 31}}, who="tg")
    assert got["id"] == rid_
    p = json.loads(ok(hub, sch, "POST", "/tg/q", {"name": "event_brief", "params": {"id": rid_}})["rows"][0]["payload"])
    assert p["ack_ms"] == 31 and p["text"] == "hi"
    assert call(hub, sch, "POST", "/tg/rows/patch", {"idem_key": "nope", "payload_patch": {}}, who="tg")[0] == 404
    ok(hub, sch, "POST", "/tg/meta", {"set": {"dropped:bot": "2", "offset:bot": 991, "compat_reads:bot": {"n": 1}}},
       who="tg")  # fmt: skip
    m = ok(hub, sch, "GET", "/tg/meta?prefix=dropped:")["meta"]
    assert m == {"dropped:bot": "2"}
    assert ok(hub, sch, "GET", "/tg/meta?prefix=compat_reads:", who="tg")["meta"] == {"compat_reads:bot": '{"n":1}'}
    assert call(hub, sch, "POST", "/tg/meta", {"set": {"schema": "9"}}, who="tg")[0] == 400, "ledger keys stay out"
    assert call(hub, sch, "GET", "/tg/meta?prefix=sche")[0] == 400
    ok(hub, sch, "POST", "/tg/meta", {"set": {"dropped:bound busy": "3", "dropped:approval not his": "1"}}, who="tg")
    m = ok(hub, sch, "GET", "/tg/meta?prefix=dropped:bou")["meta"]
    assert m == {"dropped:bound busy": "3"}, "the adapter's counters name their reason in words"
    assert call(hub, sch, "POST", "/tg/meta", {"set": {"dropped:a\nb": "1"}}, who="tg")[0] == 400, (
        "no control characters"
    )


def test_deliveries_and_receipts(hub, sch):
    a, b, c = row(), row("tg.button"), row()
    ids = ok(hub, sch, "POST", "/tg/rows", {"rows": [a, b, c]}, who="tg")["ids"]
    assert ok(hub, sch, "POST", "/tg/deliveries/sent", {"event_id": ids[0]})["tries"] == 1
    assert ok(hub, sch, "POST", "/tg/deliveries/sent", {"event_id": ids[0], "ts": time.time()})["tries"] == 2
    ok(hub, sch, "POST", "/tg/deliveries/sent", {"event_id": ids[1]})
    t0 = time.time()
    assert ok(hub, sch, "GET", f"/tg/deliveries/seen?since_ts={t0 - 60}")["seen"] is False
    un = ok(hub, sch, "POST", "/tg/inbound/unreceipted", {"since_ts": t0 - 60, "max_id": ids[2]})["rows"]
    assert [r["id"] for r in un] == ids
    got = ok(hub, sch, "POST", "/tg/deliveries/receipt", {"event_id": ids[0], "ts": t0, "message_id": 7, "rid": rid()})
    assert got == {"ok": True, "result": "ok", "first": True}
    again = ok(hub, sch, "POST", "/tg/deliveries/receipt", {"event_id": ids[0], "ts": t0 + 5, "how": "transcript"})
    assert again["first"] is False
    rc = ok(hub, sch, "POST", "/tg/deliveries/receipts", {"ids": ids})["receipts"]
    assert rc == {str(ids[0]): t0, str(ids[1]): None}, "the first receipt stays; rows without a deliveries row absent"
    assert ok(hub, sch, "GET", f"/tg/deliveries/seen?since_ts={t0 - 60}")["seen"] is True
    st = json.loads(json.dumps(ok(hub, sch, "POST", "/tg/q", {"name": "event_brief", "params": {"id": ids[0]}})))
    assert st["rows"][0]["status"] == "acked"
    assert (
        ok(hub, sch, "POST", "/tg/deliveries/receipt", {"event_id": ids[1], "message_id": 99})["result"] == "mismatch"
    )
    assert (
        ok(hub, sch, "POST", "/tg/deliveries/receipt", {"event_id": ids[2], "kinds": ["tg.button"]})["result"] == "kind"
    )
    assert ok(hub, sch, "POST", "/tg/deliveries/receipt", {"event_id": 10**15})["result"] == "unknown"
    un = ok(hub, sch, "POST", "/tg/inbound/unreceipted", {"since_ts": t0 - 60, "max_id": ids[2]})["rows"]
    assert [r["id"] for r in un] == ids[1:]
    lat = ok(hub, sch, "GET", f"/tg/latest-delivered?chat=42&since_ts={t0 - 60}")["row"]
    assert lat["id"] == ids[1]
    assert ok(hub, sch, "GET", f"/tg/latest-delivered?chat=43&since_ts={t0 - 60}")["row"] is None


def test_bindings(hub, sch):
    b = {"kind": "command", "key": "status", "tool": "tg-status.show", "args": [], "note": True, "why": "the board"}
    v0 = ok(hub, sch, "GET", "/tg/bindings", who="tg")["version"]
    ids = ok(hub, sch, "POST", "/tg/bindings/upsert", {"rows": [b, {**b, "kind": "button", "key": "rm:x"}],
             "rid": rid()})["ids"]  # fmt: skip
    got = ok(hub, sch, "GET", "/tg/bindings", who="tg")
    assert got["version"] != v0 and [r["id"] for r in got["rows"]] == ids
    assert got["rows"][0]["args"] == "[]" and got["rows"][0]["note"] == 1 and got["rows"][0]["active"] == 1
    ok(hub, sch, "POST", "/admin/import", {"table": "bindings", "rows": [{"id": 999, "kind": "card", "key": "c:1",
       "tool": "t", "why": "card", "created": time.time()}]})  # fmt: skip
    assert [r["kind"] for r in ok(hub, sch, "GET", "/tg/bindings")["rows"]] == ["command", "button"], "no card rows"
    assert len(ok(hub, sch, "POST", "/tg/q", {"name": "bindings_all"})["rows"]) == 3
    assert ok(hub, sch, "POST", "/tg/bindings/retire", {"id": ids[0]})["n"] == 1
    assert ok(hub, sch, "POST", "/tg/bindings/retire", {"kind": "button", "key": "rm:x"})["n"] == 1
    assert ok(hub, sch, "POST", "/tg/bindings/retire", {"kind": "button", "key": "rm:x"})["n"] == 0
    v2 = ok(hub, sch, "GET", "/tg/bindings")
    assert v2["version"] != got["version"] and all(r["active"] == 0 for r in v2["rows"])
    again = ok(hub, sch, "POST", "/tg/bindings/upsert", {"rows": [b]})["ids"]
    assert again == ids[:1], "an upsert of a retired key revives its row"
    assert call(hub, sch, "POST", "/tg/bindings/upsert", {"rows": [b]}, who="tg")[0] == 403
    assert call(hub, sch, "POST", "/tg/bindings/upsert", {"rows": [{**b, "why": ""}]})[0] == 400
    assert call(hub, sch, "POST", "/tg/bindings/upsert", {"rows": [{**b, "kind": "card"}]})[0] == 400


def test_named_queries_and_prune(hub, sch):
    t0 = time.time()
    old = row(ts=t0 - 40 * 86400, payload=json.dumps({"chat_id": "42", "text": "secret words", "message_id": 1}))
    m, btn, bound = row(ts=t0 - 600), row("tg.button", ts=t0 - 500), row("tg.bound", delivery="digest", ts=t0 - 400)
    btn["payload"] = json.dumps({"chat_id": "42", "ack_ms": 40, "update_id": 101, "bot": "b"})
    ids = ok(hub, sch, "POST", "/tg/rows", {"rows": [old, m, btn, bound]}, who="tg")["ids"]
    q = lambda name, **p: ok(hub, sch, "POST", "/tg/q", {"name": name, "params": p})["rows"]  # noqa: E731
    und = q("undelivered", kinds=["tg.message", "tg.button"], before_ts=t0 - 300)
    assert [r["id"] for r in und] == ids[:3]
    ok(hub, sch, "POST", "/tg/deliveries/receipt", {"event_id": ids[1], "ts": t0 - 590})
    ok(hub, sch, "POST", "/tg/deliveries/receipt", {"event_id": ids[2], "ts": t0 - 490})
    ok(hub, sch, "POST", "/outbox", {"op": "send", "bot": "live", "chat": "42", "body": {"text": "re"},
       "reply_to_event": ids[2], "trace_id": "ab" * 16, "files": ["hubfile:out/x"], "rid": rid()})  # fmt: skip
    ru = q("received_unanswered", kinds=["tg.message", "tg.button"], after_ts=t0 - 3600, before_ts=t0 - 60)
    assert [r["id"] for r in ru] == [ids[1]], "the button got an answer, the message didn't"
    ch = q("channel_rows", kinds=["tg.message", "tg.button"], since=t0 - 3600, until=t0)
    assert [(r["id"], r["receipt_ts"] is not None) for r in ch] == [(ids[1], True), (ids[2], True)]
    assert json.loads(q("kind_payloads", kind="tg.button", since=t0 - 3600, until=t0)[0]["payload"])["ack_ms"] == 40
    assert len(q("source_payloads", source="tg-adapter", since=t0 - 3600, until=t0)) == 3
    ob = q("outbox_live", since=t0 - 60, until=t0 + 60)
    assert len(ob) == 1 and ob[0]["op"] == "send"
    full = ok(hub, sch, "GET", f"/outbox/{ob[0]['id']}")
    assert (
        full["reply_to_event"] == ids[2]
        and full["trace_id"] == "ab" * 16
        and json.loads(full["files"]) == ["hubfile:out/x"]
    )
    assert q("kind_count", kind="tg.bound", since=t0 - 3600, until=t0) == [{"n": 1}]
    assert call(hub, sch, "POST", "/tg/q", {"name": "drop table"})[0] == 400
    assert (
        call(hub, sch, "POST", "/tg/q", {"name": "kind_count", "params": {"kind": "x';--", "since": 0, "until": 1}})[0]
        == 400
    )
    pr = ok(hub, sch, "POST", "/tg/prune-text", {"before_ts": t0 - 30 * 86400, "keep_keys": ["chat_id", "message_id"]})
    assert pr == {"n": 1, "more": False}
    p = json.loads(q("event_brief", id=ids[0])[0]["payload"])
    assert "text" not in p and p["chat_id"] == "42" and "_nulled" in p
    assert ok(hub, sch, "POST", "/tg/prune-text", {"before_ts": t0 - 30 * 86400, "keep_keys": []})["n"] == 0


def test_jobs_lifecycle(hub, sch):
    time.sleep(max(0.0, 4.2 - (time.time() - hub.started_at)))  # past the start grace: no companion yet
    first = ok(hub, sch, "POST", "/tg/jobs", {"kind": "bind", "args": {"tool": "x"}}, who="tg")
    assert first["state"] == "unclaimed", "no companion polled: unclaimed at once"
    assert ok(hub, sch, "POST", "/tg/jobs/claim", {"wait": 0})["job"] is None  # the companion shows up
    j = ok(hub, sch, "POST", "/tg/jobs", {"kind": "transcribe", "args": {"file": "hubfile:in/a.ogg"}, "timeout_s": 30,
          "rid": rid()}, who="tg")  # fmt: skip
    assert j["state"] == "queued"
    got = ok(hub, sch, "POST", "/tg/jobs/claim", {"kinds": ["transcribe"], "wait": 2})["job"]
    assert got["id"] == j["id"] and got["args"] == {"file": "hubfile:in/a.ogg"} and got["timeout_s"] == 30
    assert ok(hub, sch, "POST", f"/tg/jobs/{j['id']}/wait", {"wait": 0}, who="tg")["state"] == "running"
    out = {}
    t = threading.Thread(target=lambda: out.update(ok(hub, sch, "POST", f"/tg/jobs/{j['id']}/wait", {"wait": 10},
                                                      who="tg", timeout=30)))  # fmt: skip
    t.start()
    time.sleep(0.3)
    assert ok(hub, sch, "POST", f"/tg/jobs/{j['id']}/result", {"ok": True, "result": {"text": "hello"}, "rid": rid()})[
        "ok"
    ]
    t.join(15)
    assert out["state"] == "done" and out["result"] == {"text": "hello"}
    # unclaimed: nobody claims within 1 s
    u = ok(hub, sch, "POST", "/tg/jobs", {"kind": "scan", "args": {}}, who="tg")
    assert ok(hub, sch, "POST", f"/tg/jobs/{u['id']}/wait", {"wait": 5}, who="tg")["state"] == "unclaimed"
    assert ok(hub, sch, "POST", "/tg/jobs/claim", {"kinds": ["scan"], "wait": 0})["job"] is None, "too old to claim"
    # expired: claimed, no result within timeout + grace
    e = ok(hub, sch, "POST", "/tg/jobs", {"kind": "bind", "args": {}, "timeout_s": 1}, who="tg")
    assert ok(hub, sch, "POST", "/tg/jobs/claim", {"kinds": ["bind"], "wait": 1})["job"]["id"] == e["id"]
    assert ok(hub, sch, "POST", f"/tg/jobs/{e['id']}/wait", {"wait": 5}, who="tg")["state"] == "expired"
    late = ok(hub, sch, "POST", f"/tg/jobs/{e['id']}/result", {"ok": True, "result": {}})
    assert late == {"ok": False, "state": "expired"}
    # failed + a claim that long-polls until a job arrives
    res = {}
    t = threading.Thread(target=lambda: res.update(ok(hub, sch, "POST", "/tg/jobs/claim", {"wait": 10}, timeout=30)))
    t.start()
    time.sleep(0.5)
    f = ok(hub, sch, "POST", "/tg/jobs", {"kind": "bind", "args": {"n": 1}}, who="tg")
    t.join(15)
    assert res["job"]["id"] == f["id"]
    ok(hub, sch, "POST", f"/tg/jobs/{f['id']}/result", {"ok": False, "error": "exit 1", "result": {}})
    assert ok(hub, sch, "POST", f"/tg/jobs/{f['id']}/wait", {}, who="tg")["state"] == "failed"
    assert call(hub, sch, "POST", "/tg/jobs/claim", {"wait": 0}, who="tg")[0] == 403
    assert call(hub, sch, "POST", "/tg/jobs", {"kind": "rm -rf", "args": {}}, who="tg")[0] == 400
    assert call(hub, sch, "POST", "/tg/jobs/123456789/wait", {}, who="tg")[0] == 404


def test_presence(hub, sch):
    assert ok(hub, sch, "GET", "/tg/presence?after=0&wait=0", who="tg") == {"version": 0, "state": None}
    v = ok(hub, sch, "POST", "/tg/presence", {"state": {"working": True}})["version"]
    got = ok(hub, sch, "GET", "/tg/presence?after=0&wait=5", who="tg")
    assert got == {"version": v, "state": {"working": True}}
    out = {}
    t = threading.Thread(target=lambda: out.update(ok(hub, sch, "GET", f"/tg/presence?after={v}&wait=10", who="tg",
                                                      timeout=30)))  # fmt: skip
    t.start()
    time.sleep(0.3)
    v2 = ok(hub, sch, "POST", "/tg/presence", {"state": {"working": False}})["version"]
    t.join(15)
    assert out == {"version": v2, "state": {"working": False}} and v2 > v
    assert call(hub, sch, "POST", "/tg/presence", {"state": {}}, who="tg")[0] == 403


def _raw(hub, req: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", hub.port), timeout=10) as s:
        s.sendall(req)
        s.shutdown(socket.SHUT_WR)
        data = b""
        while chunk := s.recv(65536):
            data += chunk
    return data


def _get(hub, path, who="tg"):
    req = urllib.request.Request(hub.url + path)
    req.add_header("Authorization", f"Bearer {hub.tokens[who]}")
    try:
        with hubfix.web.open(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_files(hub):
    data = os.urandom(300_000)
    req = urllib.request.Request(hub.url + "/tg/files", data=data, method="POST")
    req.add_header("Authorization", f"Bearer {hub.tokens['brain']}")
    req.add_header("X-File-Name", "../../etc/My Photo (1).jpg")
    with hubfix.web.open(req, timeout=20) as r:
        got = json.load(r)
    assert (
        got["ref"].startswith("hubfile:out/") and got["ref"].endswith("-My_Photo_1_.jpg") and got["size"] == len(data)
    )
    with hubfix.web.open(req, timeout=20) as r:
        assert json.load(r)["ref"] == got["ref"], "same bytes, same ref"
    name = got["ref"].split("/", 1)[1]
    assert _get(hub, f"/tg/files/out/{name}") == (200, data)
    (hub.root / "tgfiles" / "in" / "p1.jpg").write_bytes(b"photo")
    assert _get(hub, "/tg/files/in/p1.jpg", who="brain") == (200, b"photo")
    assert _get(hub, "/tg/files/in/nope.jpg")[0] == 404
    assert _get(hub, "/tg/files/in/.hidden")[0] == 400
    assert _get(hub, "/tg/files/in/..%2f..%2fpg_password")[0] in (400, 404)
    assert _get(hub, "/tg/files/etc/passwd")[0] == 400
    assert b" 401 " in _raw(hub, b"GET /tg/files/in/p1.jpg HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    big = _raw(hub, f"POST /tg/files HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {hub.tokens['brain']}\r\n"
               f"Content-Length: {60 << 20}\r\n\r\n".encode())  # fmt: skip
    assert b" 413 " in big.split(b"\r\n", 1)[0]
    os.rename(hub.root / "tgfiles", hub.root / "tgfiles.off")
    try:
        assert _get(hub, "/tg/files/in/p1.jpg")[0] == 503
    finally:
        os.rename(hub.root / "tgfiles.off", hub.root / "tgfiles")
    old = hub.root / "tgfiles" / "in" / "old.jpg"
    old.write_bytes(b"x")
    t = time.time() - 8 * 86400
    os.utime(old, (t, t))
    code = ("import os, sys; sys.path[:0] = [sys.argv[1], sys.argv[2]]; os.environ['HUB_TGFILES'] = sys.argv[3];"
            " import hubtg; print(hubtg.prune_files())")  # fmt: skip
    py = hubfix.py313()
    assert py
    r = subprocess.run([py, "-I", "-c", code, str(hubfix.REPO / "packages/hub"), str(hub.vendor),
                        str(hub.root / "tgfiles")], capture_output=True, text=True, timeout=60)  # fmt: skip
    assert r.stdout.strip() == "1" and not old.exists() and (hub.root / "tgfiles" / "in" / "p1.jpg").exists(), r.stderr
    h = json.load(hubfix.web.open(hub.url + "/health", timeout=5))
    assert h["tg"]["files"] == "ok"


def test_500_rows_two_restarts_job_survives(hub, sch):
    ok(hub, sch, "POST", "/tg/jobs/claim", {"wait": 0})
    j = ok(hub, sch, "POST", "/tg/jobs", {"kind": "bind", "args": {"k": 1}, "timeout_s": 120}, who="tg")
    assert ok(hub, sch, "POST", "/tg/jobs/claim", {"kinds": ["bind"], "wait": 2})["job"]["id"] == j["id"]
    keys = [f"tg:load:{i}" for i in range(520)]
    batches = [keys[i : i + 20] for i in range(0, len(keys), 20)]
    sent: dict[str, int] = {}
    for n, b in enumerate(batches):
        if n in (8, 17):
            hub.stop()
            hub.start()
        body = {"rows": [row(key=k) for k in b], "rid": f"t-load-{sch}-{n}"}
        for _attempt in range(20):
            try:
                st, got = call(hub, sch, "POST", "/tg/rows", body, who="tg")
            except OSError:
                st, got = 0, {}
            if st == 200:
                break
            time.sleep(0.5)
        assert st == 200, got
        sent.update(zip(b, got["ids"], strict=True))
    assert len(set(sent.values())) == 520
    seen, after = [], 0
    while True:
        rows = ok(hub, sch, "POST", "/tg/inbound/poll", {"after": after, "limit": 500})["rows"]
        if not rows:
            break
        seen += [r["id"] for r in rows]
        after = rows[-1]["id"]
    assert sorted(seen) == sorted(sent.values()), "0 lost, 0 duplicated over 2 hub restarts"
    assert ok(hub, sch, "POST", f"/tg/jobs/{j['id']}/result", {"ok": True, "result": {"text": "after"}})["ok"]
    assert ok(hub, sch, "POST", f"/tg/jobs/{j['id']}/wait", {}, who="tg")["state"] == "done"


@pytest.fixture(autouse=True)
def _started(hub):
    if not hasattr(hub, "started_at"):
        hub.started_at = time.time() - 0.0
    yield
