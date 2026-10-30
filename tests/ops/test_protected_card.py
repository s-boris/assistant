"""assistant deploy --target hub, protected set not accepted: the hub mints the approval and the deploy itself sends his
✅ card through the outbox (one row naming the nonce, idem per nonce), then exits 8 with CURRENT unchanged. The hub
never sends a card by itself, so without this the approval waits for a card nobody sends."""

import json

import pytest

from core import hub as ch
from core import tg

NONCE = "8f66c238e3a1f1be7534af9e7611164f"
NEW = "2240f34754ef4738" + "0" * 48
CUR = "b7216dc7e1af5006" + "0" * 48


@pytest.fixture
def stubs(tool, tmp_path, monkeypatch):
    from hubkit import release as hr

    rel = tmp_path / "rel"
    rel.mkdir()
    (rel / "release.json").write_text(json.dumps({"protected": {"digest": NEW, "files": ["app/hubverify.py"] * 7}}))
    monkeypatch.setattr(hr, "releases_dir", lambda rid, *p: str(rel.joinpath(*p)))
    import hubkit

    monkeypatch.setattr(hubkit, "settings", lambda: {"hub_root": "/hub"}, raising=False)
    calls: list = []

    def call(method, path, body=None, timeout=5.0):
        calls.append((method, path, body))
        if path.startswith("/protected/"):
            return {"accepted": False, "action_sha": "a" * 64}
        if path == "/approvals":
            return {"nonce": NONCE, "expires": 1.0, "summary": body["summary"], "ref": body["ref"]}
        raise AssertionError(path)

    monkeypatch.setattr(ch, "call", call)
    sent: list = []
    out = {"o": tg.Outcome("sent", 7, [42], "", {}, via="hub")}

    def send(sender, op, body, **kw):
        sent.append((sender, op, body, kw))
        o = out["o"]
        if isinstance(o, Exception):
            raise o
        return o

    monkeypatch.setattr(tg, "send", send)
    return tool, calls, sent, out


def _check(tool):
    with pytest.raises(tool.Fail) as e:
        tool._protected_check("7f6a9f1dd795", {"protected": {"digest": CUR}}, "deploy")
    return e.value


def test_card_goes_out_with_the_nonce(stubs):
    tool, calls, sent, _ = stubs
    f = _check(tool)
    assert f.code == 8
    assert [c[1] for c in calls] == [f"/protected/{NEW}", "/approvals"]
    assert len(sent) == 1
    sender, op, body, kw = sent[0]
    assert (sender, op) == ("assistant-deploy", "send")
    assert body["approval"]["nonce"] == NONCE  # the hub's tg adds the ✅/❌ row and records the card
    assert "buttons" not in body
    assert kw["idem"] == f"hubrel-card:{NONCE}"  # a re-run deploy replays the same approval: no second card
    assert "7f6a9f1dd795" in body["text"] and "7 files" in body["text"]
    assert "card sent" in str(f)
    last = [r for r in tool.history() if r.get("op") == "deploy"][-1]
    assert last["card"] == "card sent" and last["nonce"] == NONCE


@pytest.mark.parametrize(
    "outcome, says",
    [
        (tg.Outcome("queued", 7, via="hub"), "card queued"),
        (tg.Outcome("skipped", via="hub"), "already sent"),
        (tg.Outcome("direct", error="hub tg (H6): no consumer drains it"), f"ap:{NONCE}:y|n"),
        (tg.Outcome("failed", 7, error="approval x: not a pending approval", via="hub"), f"ap:{NONCE}:y|n"),
        (OSError("boom"), "card not sent (OSError)"),
    ],
)
def test_card_trouble_never_hides_the_approval(stubs, outcome, says):
    tool, _, sent, out = stubs
    out["o"] = outcome
    f = _check(tool)
    assert says in str(f) and NONCE in str(f) and "protected-approved" in str(f)


def test_no_nonce_no_card(tool):
    assert tool._protected_card("r", NEW, 1, "") == "no card: the hub returned no nonce"
