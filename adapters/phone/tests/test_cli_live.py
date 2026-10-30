"""The phone CLIs against a live test gateway: `phone ask|widget|card|cards|fence|alarms`, `whereabouts`, `phone-routine`.
PHONE_BIN / WHEREABOUTS_BIN / PHONE_ROUTINE_BIN override the ~/bin copies (edit on a copy, test, then mv)."""
import asyncio
import json
import os
import stat
import time

import pytest

from conftest import PHONE_BIN, PROTOCOL
from phonegw.util import now_s, ulid
from test_gateway_features import STATE, online, script

WHEREABOUTS_BIN = os.environ.get("WHEREABOUTS_BIN") or os.path.expanduser("~/bin/whereabouts")
ROUTINE_BIN = os.environ.get("PHONE_ROUTINE_BIN") or os.path.expanduser("~/bin/phone-routine")
SHAPE = json.load(open(os.path.join(PROTOCOL, "fixtures/vault/ha-now.shape.json")))


async def run(env, argv, stdin=None, extra=None):
    e = env.cli_env()
    e.update(extra or {})
    p = await asyncio.create_subprocess_exec(*argv, env=e, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(p.communicate(stdin.encode() if stdin else None), 60)
    return p.returncode, out.decode(), err.decode()


def phone(*args):
    return ["python3", "-I", PHONE_BIN] + list(args)


async def test_ask_now_json_is_the_vault_key_set(env, phone_dev):
    code, out, _ = await run(env, phone("ask", "now", "--json"))
    v = json.loads(out)
    assert code == 0 and set(v) - set(SHAPE["phone_extras"]) == set(SHAPE["shape"]) and v["age_min"] is None  # no state yet
    await phone_dev.http("POST", "/v1/events", {"items": [{"v": 1, "id": ulid(), "t": "state", "ts": now_s(), "body": STATE}]})
    code, out, _ = await run(env, phone("ask", "now", "--json"))
    v = json.loads(out)
    assert code == 0 and set(v) - set(SHAPE["phone_extras"]) == set(SHAPE["shape"]) and v["place"]["zone"] == "Work"
    code, out, _ = await run(env, phone("ask", "battery"))
    assert code == 0 and '"level": 65' in out
    code, out, _ = await run(env, phone("ask", "near", "gym", "--json"))
    assert code == 4 and json.loads(out)["status"] == "offline"
    code, _, _ = await run(env, phone("ask", "near"))
    assert code == 2


async def test_ask_now_no_device_exit_4(env):
    code, out, _ = await run(env, phone("ask", "now", "--json"))
    assert code == 4 and json.loads(out)["connection"]["state"] == "no_device"


async def test_widget_card_fence_alarms_cli(env, phone_dev, monkeypatch, tmp_path):
    await online(env, phone_dev)
    board = {"state": "idle", "agents": 0, "due": 0, "waiting": 1, "items": []}
    (tmp_path / "b.json").write_text(json.dumps(board))
    code, out, _ = await run(env, phone("widget", "board", "--json-file", str(tmp_path / "b.json")))
    assert code == 0 and "widget board: sent" in out
    code, out, _ = await run(env, phone("widget", "board", "--json-file", str(tmp_path / "b.json"), "--changed"))
    assert code == 0 and "unchanged" in out
    code, out, _ = await run(env, phone("card", "info"), stdin=json.dumps({"title": "Hi", "text": "x"}))
    assert code == 0 and "sent" in out
    code, out, _ = await run(env, phone("cards"))
    assert code == 0 and "Hi" in out
    code, out, _ = await run(env, phone("fence", "add", "gym", "--at", "46.0,7.0", "--r", "120"))
    assert code == 0 and "1 fence" in out
    code, out, _ = await run(env, phone("fence", "list"))
    assert code == 0 and "gym" in out and "46.0" not in out
    code, out, _ = await run(env, phone("fence", "rm", "gym"))
    assert code == 0 and "0 fence" in out
    (tmp_path / "rem.json").write_text(json.dumps([{"id": "1a2b3c4d", "owner": True, "text": "Gym bag",
                                                    "at": time.strftime("%Y-%m-%d %H:%M", time.localtime(now_s() + 3600))}]))
    env.gw.cfg["alarms"] = "on"
    env.gw.cfg["remind_db"] = str(tmp_path / "rem.json")   # never the home's real store (scratch HOMEs too)
    code, out, _ = await run(env, phone("alarms", "sync"), extra={"REMIND_DB": str(tmp_path / "rem.json")})
    assert code == 4 or code == 0, out          # REMIND_DB is read by the gateway process: see below
    monkeypatch.setenv("REMIND_DB", str(tmp_path / "rem.json"))
    code, out, _ = await run(env, phone("alarms", "sync", "--force"))
    assert code == 0 and "1 alarm" in out


async def test_whereabouts_phone_first_then_vault(env, phone_dev, tmp_path):
    vault = script(tmp_path, "vault", "echo '{\"device\": \"v\", \"place\": {\"kind\": \"home\"}, \"age_min\": 3}'\n")
    extra = {"WHEREABOUTS_VAULT_CMD": vault, "WHEREABOUTS_CONFIG": str(tmp_path / "none.json")}
    code, out, _ = await run(env, ["python3", WHEREABOUTS_BIN, "ha", "now", "--json"], extra=extra)
    assert code == 0 and json.loads(out)["source"] == "vault"          # phone has no state yet
    await phone_dev.http("POST", "/v1/events", {"items": [{"v": 1, "id": ulid(), "t": "state", "ts": now_s(), "body": STATE}]})
    code, out, _ = await run(env, ["python3", WHEREABOUTS_BIN, "ha", "now", "--json"], extra=extra)
    d = json.loads(out)
    assert code == 0 and d["source"] == "phone" and d["place"]["zone"] == "Work"
    code, out, _ = await run(env, ["python3", WHEREABOUTS_BIN, "now", "--json"], extra=dict(extra, WHEREABOUTS_ORDER="vault"))
    assert json.loads(out)["source"] == "vault"
    (tmp_path / "cfg.json").write_text(json.dumps({"order": "phone,vault", "consumers": {"driving": "vault"}}))
    e2 = dict(extra, WHEREABOUTS_CONFIG=str(tmp_path / "cfg.json"))
    code, out, _ = await run(env, ["python3", WHEREABOUTS_BIN, "now", "--json", "--for", "driving"], extra=e2)
    assert json.loads(out)["source"] == "vault"
    code, out, _ = await run(env, ["python3", WHEREABOUTS_BIN, "now", "--json"], extra=dict(e2, WHEREABOUTS_FOR="gohome"))
    assert json.loads(out)["source"] == "phone"
    env.gw.db.x("UPDATE state SET at=at-3600 WHERE device_id=?", phone_dev.device_id)     # phone goes stale
    code, out, _ = await run(env, ["python3", WHEREABOUTS_BIN, "now", "--json"], extra=extra)
    assert json.loads(out)["source"] == "vault"
    code, out, _ = await run(env, ["python3", WHEREABOUTS_BIN, "now", "--json"], extra=dict(extra, WHEREABOUTS_VAULT_CMD="false"))
    assert code == 0 and json.loads(out)["source"] == "phone" and json.loads(out)["age_min"] >= 60   # stale beats nothing
    code, out, _ = await run(env, ["python3", WHEREABOUTS_BIN, "ha", "now", "--json", "--fresh-only"],
                             extra=dict(extra, WHEREABOUTS_VAULT_CMD="false"))
    assert code == 1 and out == ""                                     # fresh only: vault failure stays a failure
    code, out, _ = await run(env, ["python3", WHEREABOUTS_BIN, "now", "--json"],
                             extra=dict(extra, WHEREABOUTS_VAULT_CMD="false", WHEREABOUTS_FRESH_ONLY="1"))
    assert code == 1 and out == ""
    code, out, _ = await run(env, ["python3", WHEREABOUTS_BIN, "ha", "now", "--json", "--fresh-only"], extra=extra)
    assert code == 0 and json.loads(out)["source"] == "vault"          # stale phone + vault up: the vault's answer
    code, out, _ = await run(env, ["python3", WHEREABOUTS_BIN, "status"], extra=extra)
    assert code == 0 and "phone: stale" in out and "answer: vault" in out


async def test_whereabouts_nothing_answers_exit_1(env, tmp_path):
    code, out, err = await run(env, ["python3", WHEREABOUTS_BIN, "ha", "now", "--json"],
                               extra={"WHEREABOUTS_VAULT_CMD": "false", "WHEREABOUTS_CONFIG": str(tmp_path / "x.json")})
    assert code == 1 and out == ""


async def test_whereabouts_and_routine_default_to_the_hub_gateway(env, phone_dev, monkeypatch, tmp_path):
    """whereabouts and phone-routine default to the `phone` CLI's gateway and token: http://claude-hub-phone:8796
    and ~/.secrets/phone-cli-token."""
    for k, v in (("TRAIN_WATCH_BIN", "/bin/false"), ("CALCACHE_BIN", "/bin/false"), ("REMIND_DB", str(tmp_path / "n.json")),
                 ("TG_STATUS_TODO", str(tmp_path / "none.md")), ("TG_STATUS_DIR", str(tmp_path / "tgs"))):
        monkeypatch.setenv(k, v)
    home = tmp_path / "h"
    (home / ".secrets").mkdir(parents=True)
    (home / ".secrets" / "phone-cli-token").write_text(open(env.paths.token).read())
    extra = {"HOME": str(home), "PHONEGW_TOKEN_FILE": ""}
    lib = os.path.join(os.path.dirname(os.path.realpath(WHEREABOUTS_BIN)), "lib")
    probe = ["python3", "-I", "-c", "import sys; sys.path.insert(0, sys.argv[1]); import whereabouts as W; print(*W.gateway())", lib]
    _, out, _ = await run(env, probe, extra=dict(extra, PHONEGW_URL=""))
    assert out.split() == ["http://claude-hub-phone:8796", str(home / ".secrets/phone-cli-token")]
    code, out, _ = await run(env, [ROUTINE_BIN, "leaving"], extra=extra)
    assert code == 4 and "auth" not in out and "queued" in out         # the CLI's token: accepted, the phone is offline


async def test_missing_token_file_is_named(env, monkeypatch, tmp_path):
    """Without ~/.secrets/phone-cli-token phone, phone-routine and `whereabouts status` name it. Exit 7 (gateway
    down) for the CLIs, the vault answers for whereabouts."""
    home = tmp_path / "h"
    (home / ".secrets").mkdir(parents=True)
    extra = {"HOME": str(home), "PHONEGW_TOKEN_FILE": "", "WHEREABOUTS_VAULT_CMD": "false",
             "WHEREABOUTS_CONFIG": str(tmp_path / "x.json")}
    tok = str(home / ".secrets/phone-cli-token")
    code, _, err = await run(env, phone("status"), extra=extra)
    assert code == 7 and tok in err
    code, _, err = await run(env, [ROUTINE_BIN, "leaving"], extra=extra)
    assert code == 7 and tok in err
    code, out, _ = await run(env, ["python3", WHEREABOUTS_BIN, "status"], extra=extra)
    assert code == 1 and "phone: no answer (no token file %s" % tok in out


async def test_phone_routine_leaving(env, phone_dev, monkeypatch):
    monkeypatch.setenv("TRAIN_WATCH_BIN", "/bin/false")
    monkeypatch.setenv("CALCACHE_BIN", "/bin/false")
    monkeypatch.setenv("TG_STATUS_TODO", str(env.tmp / "none.md"))
    monkeypatch.setenv("TG_STATUS_DIR", str(env.tmp / "tgs"))
    monkeypatch.setenv("REMIND_DB", str(env.tmp / "none.json"))
    code, out, _ = await run(env, [ROUTINE_BIN, "leaving"])
    assert code == 4                                                   # paired but offline: queued
    await online(env, phone_dev)
    c = await phone_dev.recv("card")                                   # the queued card replays
    assert c["body"]["title"] == "🚪 Leaving"
    code, out, _ = await run(env, [ROUTINE_BIN, "leaving"])
    assert code == 0 and "Nothing pending" in (await phone_dev.recv("card"))["body"]["text"]
    code, _, _ = await run(env, [ROUTINE_BIN, "bogus"])
    assert code == 2


@pytest.fixture
async def phone_dev(env):
    from conftest import FakePhone
    p = FakePhone(env)
    st, _ = await p.pair(env.code())
    assert st == 200
    yield p
    await p.close()


async def test_release_and_geo_cli(env, phone_dev, tmp_path):
    apk = tmp_path / "claude-0.3.0-3.apk"
    apk.write_bytes(b"PK\x03\x04" + b"x" * 100)
    code, out, _ = await run(env, phone("release", str(apk), "--notes", "Test"))
    assert code == 4 and "code 3" in out and "1 device(s), queued" in out
    code, out, _ = await run(env, phone("geo"))
    assert code == 1 and "missing" in out
    code, _, _ = await run(env, phone("release", str(tmp_path / "x.txt")))
    assert code == 2
