"""Deploy tools against a stub `assistant`: status reads status + ci-gate, plan is the default (bin = drift, hub =
--plan), a real deploy passes target/rev, hub deploys bound the call guard (--wait-calls 5), exit 8 (protected paths)
= approval_required, the lock and a live voice call (exit 9) = conflict."""

from __future__ import annotations

from tools import gateway
from tools.core import load
from tools.domains import deploy  # noqa: F401  (registers the domain)


def call(name, **args):
    return gateway.call(name, args)


def test_status_and_plan(stubs):
    load()
    stubs.on("assistant", ["status", "--json"], {"release": "abc123def456", "drift": []})
    stubs.on(
        "assistant",
        ["ci-gate", "status", "--json"],
        {"mode": "shadow", "signed_green_lands": 2, "recent": [{"release": "r", "branch": "b"}]},
    )
    r = call("deploy.status")
    assert r.ok, r.error
    assert r.data["release"] == "abc123def456" and r.data["gate"] == "shadow"
    stubs.on("assistant", ["drift"], "no drift")
    r = call("deploy.run")
    assert r.ok and r.data["done"] is False and stubs.calls("assistant")[-1] == ["drift"]
    stubs.on("assistant", ["deploy", "--target", "hub", "--plan"], "plan (deploy --target hub; nothing changed):")
    r = call("deploy.run", target="hub")
    assert r.ok and stubs.calls("assistant")[-1] == ["deploy", "--target", "hub", "--plan", "--wait-calls", "5"]


def test_deploy_errors(stubs):
    load()
    stubs.on(
        "assistant",
        ["deploy", "--target", "hub", "--rev", "abcdef1"],
        "",
        rc=8,
        err="assistant: hub release abcdef1 changes protected paths: needs the owner's ✅",
    )
    r = call("deploy.run", target="hub", rev="abcdef1", plan=False)
    assert not r.ok and r.code == "approval_required"
    assert stubs.calls("assistant")[-1] == ["deploy", "--target", "hub", "--rev", "abcdef1", "--wait-calls", "5"]
    live = "assistant: hub release abcdef123456: 1 voice call(s) still live after 5 min; CURRENT unchanged"
    stubs.on("assistant", ["deploy", "--target", "hub", "--wait-calls"], "", rc=9, err=live)
    r = call("deploy.run", target="hub", plan=False)
    assert not r.ok and r.code == "conflict" and "voice call" in r.error["message"]
    stubs.on("assistant", ["deploy"], "", rc=7, err="assistant: lock held")
    r = call("deploy.run", target="bin", plan=False)
    assert not r.ok and r.code == "conflict"
    r = call("deploy.run", target="bin", rev="main; rm -rf /")
    assert not r.ok and r.code == "invalid_args"
