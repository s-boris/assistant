"""Board v2: hubboard's routes on a throwaway hubd + Postgres (hubfix): insert, read, versioned update, 409 on a stale
version or a second insert (all or nothing), meta, log, the rid's replay, the 7-day window, validation, callers."""

from __future__ import annotations

import uuid

import hubfix
import pytest


@pytest.fixture(scope="module")
def hub(tmp_path_factory):
    why = hubfix.missing()
    if why:
        pytest.skip(why)
    root = tmp_path_factory.mktemp("hubboard")
    pg = hubfix.Pg(root).start()
    h = hubfix.Hubd(root, pg, hubfix.vendor_tree(root / "vendor")).start()
    yield h
    h.stop()
    pg.discard()


@pytest.fixture
def s(hub):
    name = "t_" + uuid.uuid4().hex[:12]
    yield name
    hub.call("POST", "/admin/drop", {"schema": name})


def row(i: str, **kw) -> dict:
    r = {"id": i, "version": 0, "kind": "item", "section": "Due", "pos": 1.0, "line": "**A** — x", "title": "A",
         "notes": "[]", "due_ts": None, "probes": "[]", "state": "open", "created": 1.0, "updated": 1.0,
         "closed_ts": None, "closed_by": None}  # fmt: skip
    r.update(kw)
    return r


def apply(hub, s, rid, **body):
    return hub.call("POST", "/board/apply", {"rid": rid, **body}, schema=s)


def test_board_routes(hub, s):
    st, got = hub.call("GET", "/board", schema=s)
    assert st == 200 and got["items"] == [] and got["meta"] == {}
    st, first = apply(hub, s, "rid-board-0001", upserts=[row("abc123"), row("def456", kind="raw", line="", title="")],
                      meta={"header": "# TODO\n", "sections": '["Due"]'},
                      log=[{"item": "abc123", "op": "add", "by": "cli", "detail": "A"}])  # fmt: skip
    assert st == 200 and first["versions"] == {"abc123": 1, "def456": 1}
    assert apply(hub, s, "rid-board-0001", upserts=[row("abc123")]) == (
        200,
        {**first, "replayed": True},
    )  # no 2nd write
    st, got = hub.call("GET", "/board", schema=s)
    assert [i["id"] for i in got["items"]] == ["abc123", "def456"] and got["meta"]["header"] == "# TODO\n"
    assert got["items"][0]["version"] == 1 and got["items"][0]["due_ts"] is None
    assert apply(hub, s, "rid-board-0002", upserts=[row("abc123", version=1, line="**A** — y")])[0] == 200
    assert apply(hub, s, "rid-board-0003", upserts=[row("def456", version=1, kind="raw", line="z", title=""),
                                                    row("abc123", version=1)])[0] == 409  # fmt: skip
    assert apply(hub, s, "rid-board-0004", upserts=[row("abc123")])[0] == 409
    items = {i["id"]: i for i in hub.call("GET", "/board", schema=s)[1]["items"]}
    assert items["abc123"]["line"] == "**A** — y" and items["def456"]["line"] == "" and items["def456"]["version"] == 1
    gone = row("def456", version=1, kind="raw", line="", title="", state="dropped", closed_ts=1.0, closed_by="x")
    assert apply(hub, s, "rid-board-0005", upserts=[gone])[0] == 200
    assert [i["id"] for i in hub.call("GET", "/board", schema=s)[1]["items"]] == ["abc123"]  # closed > 7 days ago
    assert len(hub.call("GET", "/board?all=1", schema=s)[1]["items"]) == 2
    st, got = hub.call("GET", "/board/log?item=abc123", schema=s)
    assert st == 200 and [r["op"] for r in got["rows"]] == ["add"]


def test_board_validation_and_callers(hub, s):
    bad = [row("AB"), row("abc123", state="gone"), row("abc123", kind="x"), row("abc123", pos="1"),
           row("abc123", line="x" * 30_000), row("abc123", section=""), row("abc123", version=-1)]  # fmt: skip
    for n, r in enumerate(bad):
        assert apply(hub, s, f"rid-bad-{n:05d}", upserts=[r])[0] == 400, r
    assert apply(hub, s, "rid-bad-meta1", meta={"Bad-Key": "x"})[0] == 400
    assert apply(hub, s, "rid-bad-dupe1", upserts=[row("abc123"), row("abc123")])[0] == 400
    assert hub.call("GET", "/board", who="tg", schema=s)[0] == 403
    assert hub.call("GET", "/board", schema=s)[1]["items"] == []
