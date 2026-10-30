"""Shared protocol contract: fixtures, privacy keys, lint parity with ~/bin/push."""
import glob
import importlib.machinery
import importlib.util
import json
import os

import pytest

from conftest import PROTOCOL
from phonegw.lint import lint
from phonegw.schema import Protocol, ProtocolError

PRIVATE_KEYS = {"lat", "lon", "coords", "ssid", "bssid", "mac", "text", "title"}


@pytest.fixture(scope="module")
def proto():
    return Protocol(PROTOCOL)


def direction(name):
    return name.split(".")[0]


def test_valid_fixtures(proto):
    files = glob.glob(os.path.join(PROTOCOL, "fixtures/valid/*.json"))
    assert len(files) >= 20
    for f in files:
        env = json.load(open(f))
        proto.check(direction(os.path.basename(f)), env)


def test_invalid_fixtures_rejected(proto):
    files = glob.glob(os.path.join(PROTOCOL, "fixtures/invalid/*.json"))
    assert len(files) >= 10
    for f in files:
        name = os.path.basename(f)
        d = direction(name) if direction(name) in ("s2c", "c2s") else "c2s"
        with pytest.raises(ProtocolError):
            proto.check(d, json.load(open(f)))


def test_ack_required_fixtures_carry_ack():
    """Frames of an ack_required type always go through the outbox with ack true; hello/config are sent direct."""
    req = json.load(open(os.path.join(PROTOCOL, "schema", "index.json")))["ack_required"]
    assert "config" not in req["s2c"] and "hello" not in req["s2c"]
    for f in glob.glob(os.path.join(PROTOCOL, "fixtures/valid/*.json")):
        name = os.path.basename(f)
        env = json.load(open(f))
        if direction(name) in req and env.get("t") in req[direction(name)]:
            assert env.get("ack") is True, name


def _keys(schema):
    out = set()
    if isinstance(schema, dict):
        out |= set((schema.get("properties") or {}).keys())
        for v in schema.values():
            out |= _keys(v)
    elif isinstance(schema, list):
        for v in schema:
            out |= _keys(v)
    return out


def test_no_raw_keys_in_state_or_event():
    for name in ("c2s.state.json", "c2s.event.json"):
        s = json.load(open(os.path.join(PROTOCOL, "schema", name)))
        assert not (_keys(s) & PRIVATE_KEYS), name


def test_error_messages_carry_no_values(proto):
    env = {"v": 1, "id": "01JAB3K6Q2W8Z7M4D5N0P9R1S0", "t": "state", "ts": 1, "body": {"place": {"zone": "home", "lat": 46.9480}}}
    with pytest.raises(ProtocolError) as e:
        proto.check("c2s", env)
    assert "46.9480" not in e.value.msg and "lat" in e.value.msg


def test_s2c_types_cannot_come_from_the_app(proto):
    env = {"v": 1, "id": "01JAB3K6Q2W8Z7M4D5N0P9R1S0", "t": "push.cancel", "ts": 1, "body": {"tag": "x"}}
    with pytest.raises(ProtocolError) as e:
        proto.check("c2s", env)
    assert e.value.code == "unknown_type"


def lint_cases():
    return json.load(open(os.path.join(PROTOCOL, "fixtures/lint/lockscreen.json")))["cases"]


@pytest.mark.parametrize("case", lint_cases(), ids=lambda c: c["case"])
def test_lint_fixture(case):
    _, _, problems, _ = lint(case["title"], case["body"], True)
    assert bool(problems) == case["blocked"], problems


def test_lint_parity_with_push():
    path = os.environ.get("PUSH_BIN") or os.path.expanduser("~/bin/push")
    if not os.path.exists(path):
        pytest.skip("~/bin/push not installed")
    loader = importlib.machinery.SourceFileLoader("push_for_parity", path)
    spec = importlib.util.spec_from_loader("push_for_parity", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    for c in lint_cases():
        a = lint(c["title"], c["body"], True)
        b = mod.lint(c["title"], c["body"], True)
        assert bool(a[2]) == bool(b[2]) == c["blocked"], c["case"]
