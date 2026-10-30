"""approvals: the store (one DDL with tg-adapter, 0600, the action never stored), decisions only from
his tap in the inbound journal (poll mode: his user id, his chat, inside the lifetime), consume exactly once for the
same action before the expiry (0 / 3 expired / 4 used / 5 not his), cancel, wait (incl. a decision arriving while it
waits), the T8 audit, single use under concurrency, pruning, the CLI's exit codes and the real-home guard."""

from __future__ import annotations

import datetime as dt
import json
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import approvals as A
import pytest

from core import paths

REPO = Path(__file__).resolve().parents[2]
CLI = REPO / "scripts" / "approvals"
ACTION = "webagent:wa-test:1:pay:Pay now:#pay:https://shop.example/checkout:EUR 139.62"
OWNER = "111"


def iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def tap(
    home: Path,
    nonce: str,
    yn: str = "y",
    *,
    ts: float | None = None,
    user: str = OWNER,
    chat: str = OWNER,
    owner: bool = True,
    kind: str = "button",
) -> None:
    """One entry in the fake inbound journal, shaped like telegram-plus 0.1.22 writes a button press."""
    meta = {
        "chat_id": chat,
        "message_id": "55",
        "user": "o",
        "user_id": user,
        "ts": iso(ts or time.time()),
        "event_type": "button",
        "callback_data": f"ap:{nonce}:{yn}",
        "button_text": "✅ Approve",
    }
    with open(home / "journal.jsonl", "a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "id": "x",
                    "at": meta["ts"],
                    "kind": kind,
                    "owner": owner,
                    "chat_id": chat,
                    "content": "(pressed button: ✅ Approve)",
                    "meta": meta,
                }
            )
            + "\n"
        )


def cli(home: Path, *args: str, inp: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(CLI), *args],
        capture_output=True,
        text=True,
        input=inp,
        env=dict(os.environ),
        timeout=60,
    )


def test_one_ddl() -> None:
    sql = (REPO / "schemas" / "approvals.sql").read_text(encoding="utf-8")
    assert A.DDL == sql == (REPO / "adapters" / "tg" / "src" / "approvals.sql").read_text(encoding="utf-8")


def test_new_store_modes_and_no_action_stored(home: Path) -> None:
    r = A.new(ACTION, "  Book AZ 153\n· EUR 139.62  ", ref="webagent:wa-test")
    n = r["nonce"]
    assert len(n) == 32 and int(n, 16) >= 0 and r["summary"] == "Book AZ 153 · EUR 139.62"
    assert r["buttons"] == [[{"text": "✅ Approve", "data": f"ap:{n}:y"}, {"text": "❌ Decline", "data": f"ap:{n}:n"}]]
    db = home / ".local/state/assistant/approvals.db"
    assert A.db_path() == db and db.exists()
    assert stat.S_IMODE(db.stat().st_mode) == 0o600 and stat.S_IMODE(db.parent.stat().st_mode) == 0o700
    raw = b"".join(p.read_bytes() for p in db.parent.glob("approvals.db*"))
    assert ACTION.encode() not in raw and A.action_sha(ACTION).encode() in raw
    row = A.get(n)
    assert row is not None and row["status"] == "pending" and row["ref"] == "webagent:wa-test"
    assert row["expires"] - row["created"] == pytest.approx(900)
    assert A.state(row) == "pending" and A.get(f"ap:{n}:y") == row
    assert len(A.new("x", "y" * 200)["summary"]) == 80
    for bad in [("", "s"), ("  ", "s"), ("x", " "), ("x" * 8193, "s")]:
        with pytest.raises(ValueError):
            A.new(*bad)
    for kw in [{"ttl": 0}, {"ttl": 86_401}, {"ref": "has space"}, {"ref": "x" * 81}]:
        with pytest.raises(ValueError):
            A.new("x", "s", **kw)


def test_journal_decision_consumed_exactly_once(home: Path) -> None:
    n = A.new(ACTION, "Pay")["nonce"]
    assert A.consume(n, ACTION)[0] == A.EXIT_NOT_HIS  # not decided yet
    tap(home, n, "y")
    code, info = A.consume(n, ACTION, "webagent:wa-test")
    assert (code, info["state"], info["decided_via"], info["used_by"]) == (0, "used", "journal", "webagent:wa-test")
    code, info = A.consume(n, ACTION)
    assert code == A.EXIT_USED and "used already" in info["why"]
    row = A.get(n)
    assert row is not None and row["decided_by"] == OWNER and row["refused"] == 1  # the early, undecided attempt
    assert A.audit()["ok"] is True


def test_journal_ignores_everything_that_is_not_his_tap(home: Path) -> None:
    n = A.new(ACTION, "Pay")["nonce"]
    row = A.get(n)
    assert row is not None
    tap(home, n, "y", owner=False)
    tap(home, n, "y", user="222")  # allowlisted, not the owner
    tap(home, n, "y", chat="222")  # his user id, another chat
    tap(home, n, "y", chat="-100123")  # a group
    tap(home, n, "y", kind="message")
    tap(home, n, "y", ts=row["created"] - 5)  # before the card
    tap(home, n, "y", ts=row["expires"] + 0.002)  # after the expiry (journal stamps are in ms)
    tap(home, n, "x")  # the inert button
    tap(home, "0" * 32, "y")  # another nonce
    with open(home / "journal.jsonl", "a", encoding="utf-8") as f:
        f.write(f'not json {n}\n["ap:{n}:y"]\n')
    assert A.sync(n) is None
    assert A.consume(n, ACTION)[0] == A.EXIT_NOT_HIS
    assert A.get(n)["status"] == "pending"  # type: ignore[index]
    # no owner known (unreadable access file): nothing decides
    tap(home, n, "y")
    assert A.sync(n, owner="") is None and A.sync(n, journal=home / "missing.jsonl") is None
    assert A.sync(n) == "approved"


def test_card_chat_wins_and_attach(home: Path) -> None:
    n = A.new(ACTION, "Pay")["nonce"]
    assert A.attach(n, "999", 7) is True  # a card in another chat (tests only: the adapter refuses that)
    assert A.attach(n, OWNER, 8) is False  # bound to its chat
    tap(home, n, "y")  # his DM, but the card is in 999
    assert A.sync(n) is None
    tap(home, n, "y", chat="999")
    assert A.sync(n) == "approved"
    assert A.attach(n, "999", 9) is False  # decided: no more cards
    assert A.attach("f" * 32, OWNER, 1) is False


def test_declined_cancelled_and_expired(home: Path) -> None:
    n = A.new(ACTION, "Pay")["nonce"]
    tap(home, n, "n")
    code, info = A.consume(n, ACTION)
    assert (code, info["why"], info["state"]) == (A.EXIT_NOT_HIS, "he declined", "denied")
    c = A.new(ACTION, "Pay")["nonce"]
    assert A.cancel(c) is True and A.cancel(c) is False
    tap(home, c, "y")
    assert A.consume(c, ACTION)[0] == A.EXIT_NOT_HIS and A.get(c)["status"] == "cancelled"  # type: ignore[index]
    t0 = time.time()
    e = A.new(ACTION, "Pay", 60, now=t0)["nonce"]
    tap(home, e, "y", ts=t0 + 5)
    assert A.consume(e, ACTION, now=t0 + 60)[0] == A.EXIT_EXPIRED  # approved, but used too late
    p = A.new(ACTION, "Pay", 60, now=t0)["nonce"]
    code, info = A.consume(p, ACTION, now=t0 + 61)
    assert code == A.EXIT_EXPIRED and A.get(p)["status"] == "expired"  # type: ignore[index]
    assert A.cancel(A.new("x", "s", 60, now=t0 - 120)["nonce"]) is False  # past its expiry
    assert A.consume("nonsense", ACTION)[0] == A.EXIT_NOT_HIS and A.consume("f" * 32, ACTION)[0] == A.EXIT_NOT_HIS


def test_wrong_action_is_refused_and_counted(home: Path) -> None:
    n = A.new(ACTION, "Pay")["nonce"]
    tap(home, n, "y")
    code, info = A.consume(n, ACTION + " ")
    assert code == A.EXIT_NOT_HIS and "another action" in info["why"]
    assert A.consume(n, ACTION)[0] == 0
    assert A.get(n)["refused"] == 1  # type: ignore[index]


def test_wait(home: Path) -> None:
    n = A.new(ACTION, "Pay")["nonce"]
    t = time.monotonic()
    code, info = A.wait(n, 0.3, interval=0.05)
    assert code == A.EXIT_PENDING and info["state"] == "pending" and time.monotonic() - t < 3
    th = threading.Timer(0.3, lambda: tap(home, n, "y"))
    th.start()
    code, info = A.wait(n, 10, interval=0.05)
    th.join()
    assert (code, info["state"]) == (0, "approved")
    assert A.consume(n, ACTION)[0] == 0 and A.wait(n, 0)[0] == 0  # used counts as approved, never consumes
    d = A.new(ACTION, "Pay")["nonce"]
    tap(home, d, "n")
    assert A.wait(d, 0)[0] == A.EXIT_NOT_HIS
    assert A.wait("f" * 32, 0)[0] == A.EXIT_NOT_HIS
    e = A.new(ACTION, "Pay", 1, now=time.time() - 5)["nonce"]
    assert A.wait(e, 0)[0] == A.EXIT_EXPIRED


def test_single_use_under_concurrency(home: Path) -> None:
    n = A.new(ACTION, "Pay")["nonce"]
    tap(home, n, "y")
    A.sync(n)
    codes: list[int] = []
    lock = threading.Lock()

    def go() -> None:
        c, _ = A.consume(n, ACTION, "thread")
        with lock:
            codes.append(c)

    ts = [threading.Thread(target=go) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(codes) == [0] + [A.EXIT_USED] * 7


def test_audit_flags_violations(home: Path) -> None:
    A.new("x", "s")
    con = A.connect()
    q = (
        "INSERT INTO approvals(nonce, action_sha, created, expires, status, decided_ts, used_ts) "
        "VALUES (?, 's', 0, 10, ?, ?, ?)"
    )
    con.execute(q, ("a" * 32, "pending", None, 5))  # used without his yes
    con.execute(q, ("b" * 32, "approved", 2, 10))  # used at the expiry
    con.execute(q, ("c" * 32, "approved", 12, None))  # decided after the expiry
    con.execute(q, ("d" * 32, "approved", 2, 9))  # fine
    con.close()
    r = A.audit()
    assert (r["ok"], r["violations"], r["approvals"], r["used"]) == (False, 3, 5, 3)
    out = cli(home, "audit")
    assert out.returncode == 1 and "VIOLATIONS: 3" in out.stdout


def test_prune_keeps_30_days(home: Path) -> None:
    t0 = time.time()
    old = A.new("x", "s", now=t0 - 31 * 86_400)["nonce"]
    keep = A.new("x", "s", now=t0 - 29 * 86_400)["nonce"]
    A.new("x", "s", now=t0)
    assert A.get(old) is None and A.get(keep) is not None


def test_cli(home: Path) -> None:
    assert cli(home, "--help").returncode == 0
    r = cli(
        home,
        "new",
        "--action",
        "-",
        "--summary",
        "Pay EUR 139.62",
        "--ttl",
        "600",
        "--ref",
        "webagent:wa-1",
        "--yes",
        "✅ Do it",
        "--json",
        inp=ACTION,
    )
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout)
    n = d["nonce"]
    assert d["buttons"][0][0] == {"text": "✅ Do it", "data": f"ap:{n}:y"} and d["ref"] == "webagent:wa-1"
    assert A.get(n)["action_sha"] == A.action_sha(ACTION)  # type: ignore[index]
    assert cli(home, "new", "--action", "", "--summary", "s").returncode == 2
    st = json.loads(cli(home, "status", n, "--json").stdout)
    assert (st["state"], st["summary"], st["ref"]) == ("pending", "Pay EUR 139.62", "webagent:wa-1")
    assert "action_sha" not in st
    assert cli(home, "status", "f" * 32).returncode == 5
    assert cli(home, "sent", n, "--msg", "77").returncode == 0  # chat = the owner from the access file
    assert A.get(n)["chat"] == OWNER and A.get(n)["message_id"] == 77  # type: ignore[index]
    assert cli(home, "sent", n, "--msg", "78", "--chat", "222").returncode == 5
    lst = json.loads(cli(home, "list", "--json").stdout)
    assert [x["nonce"] for x in lst] == [n] and "pending" in cli(home, "list").stdout
    assert cli(home, "wait", n, "--timeout", "0").returncode == 6
    assert cli(home, "consume", n, "--action", ACTION).returncode == 5
    tap(home, n, "y")
    assert cli(home, "wait", n, "--timeout", "1").returncode == 0
    out = cli(home, "consume", f"ap:{n}:y", "--action", "-", "--json", inp=ACTION)
    assert out.returncode == 0 and json.loads(out.stdout)["state"] == "used"
    assert cli(home, "consume", n, "--action", ACTION).returncode == 4
    assert cli(home, "list").stdout.strip() == "no open approvals"
    c = json.loads(cli(home, "new", "--action", "a", "--summary", "s", "--json").stdout)["nonce"]
    assert cli(home, "cancel", c).returncode == 0 and cli(home, "cancel", c).returncode == 5
    assert cli(home, "audit").returncode == 0


def test_the_real_home_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    import pwd

    monkeypatch.setenv("ASSISTANT_TEST", "1")
    monkeypatch.setenv("HOME", next((e.pw_dir for e in pwd.getpwall() if e.pw_uid == os.getuid()), "/nonexistent-home"))
    monkeypatch.delenv("APPROVALS_DB", raising=False)
    with pytest.raises(paths.RealHomeRefused):
        A.db_path()


def test_names() -> None:
    n = "ab" * 16
    assert A.norm_nonce(f"ap:{n}:y") == n and A.norm_nonce(n.upper()) == n
    assert A.parse_data(f"ap:{n}:n") == (n, "n") and A.parse_data(f"ap:{n}:x") is None
    assert A.state({"used_ts": None, "status": "pending", "expires": 10}, 10) == "expired"
    assert A.state({"used_ts": 5, "status": "approved", "expires": 10}, 1) == "used"
