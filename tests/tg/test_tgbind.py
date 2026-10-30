"""tg-bind: rows in the bindings table, the registry and effect gate, the bindings-off switch, the
instance DBs, and (with the real bun) that tg-adapter resolves a row tg-bind wrote."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest
from tg_testlib import REPO

from core import paths

TG_BIND = REPO / "scripts" / "tg-bind"
ADAPTER = REPO / "adapters" / "tg"
BUN = shutil.which("bun") or "/usr/local/bin/bun"

TOOLS = """schema = 1
[tools."hello"]
argv = ["~/bin/hello"]
effect = "read"
[tools."noted"]
argv = ["~/bin/noted"]
effect = "home"
note = true
[tools."ring"]
argv = ["~/bin/ring"]
effect = "send_owner"
[tools."pay"]
argv = ["~/bin/pay"]
effect = "spend"
[tools."broken"]
argv = "not a list"
effect = "read"
"""


@pytest.fixture
def reg(home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = home / "tools.toml"
    p.write_text(TOOLS)
    monkeypatch.setenv("TG_ADAPTER_TOOLS", str(p))
    return p


def tgb(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(TG_BIND), *args], capture_output=True, text=True, timeout=60, env={**os.environ, **(env or {})}
    )


def rows(db: Path) -> list[sqlite3.Row]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return con.execute("SELECT * FROM bindings ORDER BY id").fetchall()
    finally:
        con.close()


def live_db(home: Path) -> Path:
    return home / ".local/state/assistant/events.db"


def test_add_list_replace_rm(home, reg):
    r = tgb("add", "command", "/Hi", "hello", "--why", "greets", "--arg", "a b", "--arg", "c")
    assert r.returncode == 0 and "bound #1: /hi → hello (read)" in r.stdout, r.stderr
    (row,) = rows(live_db(home))
    assert (
        row["kind"],
        row["key"],
        row["tool"],
        json.loads(row["args"]),
        row["note"],
        row["on_error"],
        row["active"],
    ) == ("command", "hi", "hello", ["a b", "c"], 0, "reply", 1)
    assert row["expires"] is None and row["why"] == "greets"
    # same kind + key = replaced in place; note defaults to the tool's
    assert tgb("add", "command", "hi", "noted", "--why", "now noted", "--forward").returncode == 0
    (row,) = rows(live_db(home))
    assert (row["id"], row["tool"], row["note"], row["on_error"], json.loads(row["args"])) == (
        1,
        "noted",
        1,
        "forward",
        [],
    )
    assert tgb("add", "command", "hi2", "noted", "--why", "x", "--no-note").returncode == 0
    assert rows(live_db(home))[1]["note"] == 0
    assert tgb("add", "prefix", "zz:", "ring", "--why", "ring taps", "--expires", "2d").returncode == 0
    exp = rows(live_db(home))[2]["expires"]
    assert abs(exp - (time.time() + 2 * 86400)) < 60
    out = tgb("list").stdout
    assert "#1 command hi → noted · note forward why: now noted" in out and "#3 prefix zz: → ring" in out
    assert tgb("rm", "#2").returncode == 0
    assert tgb("rm", "prefix", "zz:").stdout.strip() == "retired"
    r = tgb("rm", "prefix", "zz:")
    assert r.returncode == 1 and "no active binding" in r.stderr
    assert [x["key"] for x in json.loads(tgb("list", "--json").stdout)] == ["hi"]
    everything = json.loads(tgb("list", "--all", "--json").stdout)
    assert [(x["key"], x["active"]) for x in everything] == [("hi", 1), ("hi2", 0), ("zz:", 0)]


def test_list_hides_expired_and_card_rows(home, reg):
    assert tgb("add", "button", "go", "hello", "--why", "x").returncode == 0
    con = sqlite3.connect(live_db(home))
    con.execute("UPDATE bindings SET expires = ? WHERE key = 'go'", (time.time() - 5,))
    con.execute(
        "INSERT INTO bindings(kind, key, tool, args, note, on_error, why, created, expires, active)"
        " VALUES ('card', '1:2:x', 'hello', '[]', 0, 'reply', 'card button', ?, ?, 1)",
        (time.time(), time.time() + 99),
    )
    con.commit()
    con.close()
    assert json.loads(tgb("list", "--json").stdout) == []
    assert len(json.loads(tgb("list", "--all", "--json").stdout)) == 2


@pytest.mark.parametrize(
    "args, err",
    [
        (["add", "command", "hi", "nope", "--why", "x"], "not in the tools registry"),
        (["add", "command", "hi", "broken", "--why", "x"], "not in the tools registry"),
        (["add", "button", "pay:1", "pay", "--why", "x"], "pay has effect spend: above send_owner"),
        (["add", "button", "perm:abc", "ring", "--why", "x"], "permission relay"),
        (["add", "prefix", "pe", "ring", "--why", "x"], "permission relay"),
        (["add", "button", "ap:" + "0" * 32 + ":y", "ring", "--why", "x"], "approvals"),
        (["add", "prefix", "ap", "ring", "--why", "x"], "approvals"),
        (["add", "prefix", "ap:0", "ring", "--why", "x"], "approvals"),
        (["add", "prefix", "z", "ring", "--why", "x"], "a prefix is 2-63 bytes"),
        (["add", "button", "x" * 65, "ring", "--why", "x"], "button data is 1-64 bytes"),
        (["add", "command", "bad-name", "hello", "--why", "x"], "a command name"),
        (["add", "command", "hi", "hello", "--why", "  "], "--why is required"),
        (["add", "command", "hi", "hello"], "--why"),
        (["add", "command", "hi", "hello", "--why", "x", "--expires", "soon"], "--expires"),
        (["add", "command", "hi", "hello", "--why", "x", "--expires", "2001-01-01"], "in the past"),
        (["rm", "nonsense"], "rm takes ID or KIND KEY"),
    ],
)
def test_refusals(home, reg, args, err):
    r = tgb(*args)
    assert r.returncode == 2 and err in r.stderr, (r.stdout, r.stderr)
    assert not live_db(home).exists() or rows(live_db(home)) == []


def test_tools_and_gate(home, reg):
    out = tgb("tools").stdout
    assert (
        "pay" in out and "(not from a tap)" in out.splitlines()[[ln.split()[0] for ln in out.splitlines()].index("pay")]
    )
    tools = json.loads(tgb("tools", "--json").stdout)
    assert sorted(tools) == ["hello", "noted", "pay", "ring"]
    assert tools["noted"]["note"] is True and tools["ring"]["effect"] == "send_owner"


def test_repo_registry_parses(home, monkeypatch):
    monkeypatch.setenv("TG_ADAPTER_TOOLS", str(REPO / "schemas" / "tools.toml"))
    tools = json.loads(tgb("tools", "--json").stdout)
    assert len(tools) >= 31 and tools["tg-status.show"]["note"] is True
    assert all(t["effect"] in ("read", "home", "send_owner") for t in tools.values())


def test_switch_and_status(home, reg):
    assert tgb("status").stdout.startswith("bindings on · 0 active")
    assert tgb("off").returncode == 0
    assert (home / ".config/tg-adapter/bindings-off").exists()
    assert tgb("status").stdout.startswith("bindings off (switch file)")
    r = tgb("add", "command", "hi", "hello", "--why", "x")
    assert r.returncode == 0 and "bindings are off" in r.stdout
    assert tgb("on").returncode == 0 and not (home / ".config/tg-adapter/bindings-off").exists()
    assert tgb("on").returncode == 0  # idempotent
    other = home / "elsewhere-off"
    assert tgb("off", env={"TG_ADAPTER_BINDINGS_OFF": str(other)}).returncode == 0 and other.exists()


def test_instances_and_config_db(home, reg):
    assert tgb("--help").returncode == 0
    assert tgb("add", "command", "hi", "hello", "--why", "x", "--instance", "test").returncode == 0
    assert [r["key"] for r in rows(home / ".local/state/tg-adapter/test.db")] == ["hi"]
    assert not live_db(home).exists()
    cfgd = home / ".config/tg-adapter"
    cfgd.mkdir(parents=True, exist_ok=True)
    (cfgd / "test.json").write_text(json.dumps({"db": "~/custom.db"}))
    assert tgb("add", "command", "yo", "hello", "--why", "x", "--instance", "test").returncode == 0
    assert [r["key"] for r in rows(home / "custom.db")] == ["yo"]
    assert tgb("add", "command", "yo", "hello", "--why", "x", "--db", str(home / "other.db")).returncode == 0
    assert [r["key"] for r in rows(home / "other.db")] == ["yo"]
    r = tgb("list", "--db", str(paths.real_home() / ".local/state/assistant/events.db"))  # the runner's: its job HOME
    assert r.returncode == 1 and "real home" in r.stderr


@pytest.mark.skipif(
    not (os.access(BUN, os.X_OK) and (ADAPTER / "node_modules").is_dir()),
    reason="bun or adapters/tg/node_modules missing",
)
def test_adapter_resolves_a_tg_bind_row(home, reg, monkeypatch):
    """The bun side reads what tg-bind wrote: `tg-bind resolve` / `table` go through `tg-adapter bindings`."""
    tok = home / "tok.env"
    tok.write_text("TELEGRAM_BOT_TOKEN=4242:TEST-not-a-real-token\n")  # secretscan:allow (fake token)
    tok.chmod(0o600)
    cfgd = home / ".config/tg-adapter"
    cfgd.mkdir(parents=True, exist_ok=True)
    (cfgd / "test.json").write_text(
        json.dumps(
            {
                "mode": "test",
                "bot": "testadapter_bot",
                "token_file": "~/tok.env",
                "allow_from": ["111"],
                "send": True,
                "instant_commands": False,
                "side_effect_callbacks": False,
            }
        )
    )
    monkeypatch.setenv("TG_ADAPTER_DIR", str(ADAPTER))
    monkeypatch.setenv("TG_ADAPTER_TEST", "1")
    assert tgb("add", "command", "hi", "hello", "--why", "x", "--instance", "test").returncode == 0
    assert tgb("add", "prefix", "zz:", "ring", "--why", "x", "--instance", "test").returncode == 0
    assert (
        json.loads(tgb("resolve", "/hi there", "--json", "--instance", "test").stdout)["resolved"] is None
    )  # no text_arg: exact only
    r = tgb("resolve", "/hi", "--json", "--instance", "test")
    assert r.returncode == 0, r.stderr
    res = json.loads(r.stdout)["resolved"]
    assert (res["source"], res["tool"], res["runs"], res["argv"][0]) == (
        "tg-bind",
        "hello",
        True,
        str(home / "bin/hello"),
    )
    res = json.loads(tgb("resolve", "--data", "zz:42", "--json", "--instance", "test").stdout)["resolved"]
    assert (res["source"], res["kind"], res["tool"], res["runs"]) == ("tg-bind", "button", "ring", True)
    con = sqlite3.connect(home / ".local/state/tg-adapter/test.db")
    con.execute(
        "UPDATE bindings SET tool = 'pay' WHERE key = 'hi'"
    )  # a row naming a gated tool (written around tg-bind)
    con.commit()
    con.close()
    res = json.loads(tgb("resolve", "/hi", "--json", "--instance", "test").stdout)["resolved"]
    assert res["runs"] is False and "needs an approval" in res["why"]
    assert tgb("off").returncode == 0
    out = tgb("table", "--instance", "test").stdout
    assert out.startswith("bindings off (switch file)") and "tg-bind" in out


# ---------------------------------------------------------------- the compat seed (tg-bind import)


def _seed(home: Path, rows_: list) -> Path:
    p = home / "seed.json"
    p.write_text(json.dumps({"rows": rows_}))
    return p


def test_import_the_compat_seed_against_the_repo_registry(home, monkeypatch):
    monkeypatch.setenv("TG_ADAPTER_TOOLS", str(REPO / "schemas" / "tools.toml"))
    seed = REPO / "ops" / "rollout" / "tg-bind-seed.json"
    r = tgb("import", str(seed), "--dry-run")
    assert r.returncode == 0 and "31 rows ok (31 new" in r.stdout, r.stderr
    assert not live_db(home).exists() or not rows(live_db(home))
    r = tgb("import", str(seed))
    assert r.returncode == 0 and "31 new, 0 updated, 0 unchanged" in r.stdout, r.stderr
    got = rows(live_db(home))
    want = json.loads(seed.read_text())["rows"]
    assert [(g["kind"], g["key"], g["tool"]) for g in got] == [(w["kind"], w["key"], w["tool"]) for w in want]
    st = next(g for g in got if g["key"] == "status")
    assert (st["note"], st["on_error"]) == (1, "forward")
    assert all(
        g["note"] == 1 and g["on_error"] == "reply" for g in got if g["kind"] == "command" and g["key"] != "status"
    )
    assert all(g["note"] == 0 for g in got if g["kind"] != "command") and all(g["why"] for g in got)
    # idempotent; an edited row is updated in place (same id = same menu position)
    assert "0 new, 0 updated, 31 unchanged" in tgb("import", str(seed)).stdout
    assert tgb("rm", "command", "bills").returncode == 0
    assert "0 new, 1 updated, 30 unchanged" in tgb("import", str(seed)).stdout
    assert [g["id"] for g in rows(live_db(home))] == [g["id"] for g in got]


def test_import_refuses_the_whole_file_on_any_bad_row(home, reg):
    ok = {"kind": "command", "key": "hi", "tool": "hello", "why": "x"}
    for bad, err in [
        ({"kind": "command", "key": "yo", "tool": "nope", "why": "x"}, "not in the tools registry"),
        ({"kind": "command", "key": "yo", "tool": "hello"}, "why is required"),
        ({"kind": "button", "key": "p", "tool": "pay", "why": "x"}, "above send_owner"),
        ({"kind": "prefix", "key": "ap:", "tool": "hello", "why": "x"}, "approvals"),
        (ok, "duplicate"),
    ]:
        r = tgb("import", str(_seed(home, [ok, bad])))
        assert r.returncode == 2 and err in r.stderr and "nothing imported" in r.stderr, (bad, r.stderr)
    assert not live_db(home).exists() or not rows(live_db(home))


def test_export_round_trips_through_import(home, reg):
    assert tgb("add", "command", "hi", "hello", "--why", "greets", "--arg", "a b").returncode == 0
    assert tgb("add", "button", "go:1", "noted", "--why", "taps", "--no-note", "--expires", "7d").returncode == 0
    assert tgb("add", "prefix", "zz:", "ring", "--why", "zz taps", "--forward").returncode == 0
    assert tgb("add", "command", "old", "hello", "--why", "retired one").returncode == 0
    assert tgb("add", "button", "gone", "hello", "--why", "expired one").returncode == 0
    assert tgb("rm", "command", "old").returncode == 0
    con = sqlite3.connect(live_db(home))
    con.execute("UPDATE bindings SET expires = ? WHERE key = 'gone'", (time.time() - 5,))
    con.execute(
        "INSERT INTO bindings(kind, key, tool, args, note, on_error, why, created, expires, active)"
        " VALUES ('card', '1:2:x', 'hello', '[]', 0, 'reply', 'card button', ?, ?, 1)",
        (time.time(), time.time() + 99),
    )
    con.commit()
    con.close()
    r = tgb("export")
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    # active command/button/prefix rows only, in row order; no card binds, nothing retired or expired
    assert [(x["kind"], x["key"]) for x in doc["rows"]] == [("command", "hi"), ("button", "go:1"), ("prefix", "zz:")]
    assert doc["rows"][0] == {
        "kind": "command",
        "key": "hi",
        "tool": "hello",
        "why": "greets",
        "note": False,
        "forward": False,
        "args": ["a b"],
    }
    assert "expires" in doc["rows"][1] and "expires" not in doc["rows"][2] and doc["rows"][2]["forward"] is True

    def fields(db: Path) -> list[tuple]:
        return [
            (g["kind"], g["key"], g["tool"], json.loads(g["args"]), g["note"], g["on_error"], g["why"], g["expires"])
            for g in rows(db)
            if g["active"] and g["kind"] != "card" and (g["expires"] is None or g["expires"] > time.time())
        ]

    dump = home / "export.json"
    dump.write_text(r.stdout)
    fresh = home / "fresh.db"
    r = tgb("import", str(dump), "--db", str(fresh))
    assert r.returncode == 0 and "3 new, 0 updated, 0 unchanged" in r.stdout, r.stderr
    assert fields(fresh) == fields(live_db(home))
    # the same export back into the source changes nothing; the restored DB exports the same file
    assert "0 new, 0 updated, 3 unchanged" in tgb("import", str(dump)).stdout
    assert json.loads(tgb("export", "--db", str(fresh)).stdout) == doc
    r = tgb("export", "--db", str(home / "missing.db"))
    assert r.returncode == 1 and "no DB" in r.stderr


def test_compat_counter_view(home, reg):
    cfgd = home / ".config/tg-adapter"
    cfgd.mkdir(parents=True, exist_ok=True)
    (cfgd / "live.json").write_text(json.dumps({"bot": "livebot"}))
    assert tgb("add", "command", "hi", "hello", "--why", "x").returncode == 0  # creates the store
    assert "not counted yet" in tgb("compat").stdout
    con = sqlite3.connect(live_db(home))
    since = time.time() - 50 * 3600
    con.execute(
        "INSERT INTO meta(k, v) VALUES ('compat_reads:livebot', ?)",
        (json.dumps({"since": since, "instant": {"n": 2, "last": since + 3600, "key": "/bills"}}),),
    )
    con.commit()
    con.close()
    out = tgb("compat").stdout
    assert "instant 2" in out and "(/bills)" in out and "unread for 49.0 h · 48 h reached" in out, out
    assert json.loads(tgb("compat", "--json").stdout)["compat_reads"]["instant"]["n"] == 2


@pytest.mark.skipif(
    not (os.access(BUN, os.X_OK) and (ADAPTER / "node_modules").is_dir()),
    reason="bun or adapters/tg/node_modules missing",
)
def test_menu_from_rows_and_the_diff_with_0122(home, reg, monkeypatch):
    reg.write_text(TOOLS.replace('[tools."noted"]', 'menu = "Say hello"\n[tools."noted"]'))
    tok = home / "tok.env"
    tok.write_text("TELEGRAM_BOT_TOKEN=4242:TEST-not-a-real-token\n")  # secretscan:allow (fake token)
    tok.chmod(0o600)
    (home / "tg").mkdir()
    (home / "tg/instant.json").write_text(
        json.dumps([{"command": "hi", "description": "Say hello", "run": "~/bin/hello"}])
    )
    (home / "custom.json").write_text(json.dumps([{"command": "lights", "description": "Lights"}]))
    cfgd = home / ".config/tg-adapter"
    cfgd.mkdir(parents=True, exist_ok=True)
    cfg = {"mode": "test", "bot": "testadapter_bot", "token_file": "~/tok.env", "allow_from": ["111"], "send": True}
    (cfgd / "test.json").write_text(
        json.dumps({**cfg, "compat_instant_file": "~/tg/instant.json", "compat_callbacks_file": ""})
    )
    monkeypatch.setenv("TG_ADAPTER_DIR", str(ADAPTER))
    monkeypatch.setenv("TG_ADAPTER_TEST", "1")
    monkeypatch.chdir(home)
    assert tgb("add", "command", "hi", "hello", "--why", "x", "--instance", "test").returncode == 0
    r = tgb("menu", "--diff", "--custom", "custom.json", "--json", "--instance", "test")
    assert r.returncode == 0, r.stderr
    m = json.loads(r.stdout)
    assert m["identical"] is True and [c["command"] for c in m["shown"]] == ["hi", "lights"]
    assert m["bound"] == [{"command": "hi", "description": "Say hello"}]
    assert (
        "identical to the 0.1.22 menu" in tgb("menu", "--diff", "--custom", "custom.json", "--instance", "test").stdout
    )
    reg.write_text(TOOLS.replace('[tools."noted"]', 'menu = "Hi there"\n[tools."noted"]'))
    r = tgb("menu", "--diff", "--custom", "custom.json", "--instance", "test")
    assert r.returncode == 1 and "#1: 0.1.22 /hi — Say hello | now /hi — Hi there" in r.stdout, r.stdout
    assert "not counted yet" in tgb("compat", "--instance", "test").stdout  # the CLI never counts
