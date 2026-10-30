"""hub kit apply-request / apply / apply-log (one-tap kit apply, dispatcher 3.3): the brain only carries the server's
review; fakes for unraid-ssh and approvals, nothing reaches a server or the hub."""

import json
import sys

import pytest
from hubkit import HubError
from hubkit import cli as C
from hubkit import kitapply as KA

FACTS = "f" * 64
NONCE = "a" * 32


def server_reply(tier: str, part: str = "tg", **kw) -> str:
    rows = [
        ("part", part),
        ("container", f"claude-hub-{part.removesuffix('-off')}"),
        ("op", kw.get("op", "update")),
        ("tier", tier),
        ("facts", FACTS),
        ("summary", kw.get("summary", f"kit {part}: image - ffffffff")),
        ("action", kw.get("action", KA.action(part, FACTS))),
        ("card", f"claude-hub kit: {part}"),
        ("card", "Image: oven/bun:1.4.4-slim -> oven/bun:1.4.5-slim"),
    ] + [("why", w) for w in kw.get("why", [])]
    return "".join(f"{k}\t{v}\n" for k, v in rows)


@pytest.fixture
def fakes(tmp_path, monkeypatch):
    """Fake unraid-ssh (prints tmp/reply.txt, exits tmp/rc) and approvals (records argv + stdin, prints a nonce)."""
    log = tmp_path / "calls.jsonl"
    ssh = tmp_path / "unraid-ssh"
    ssh.write_text(
        "#!/usr/bin/env python3\nimport json, sys, pathlib\nd = pathlib.Path(sys.argv[0]).parent\n"
        "open(d / 'calls.jsonl', 'a').write(json.dumps({'bin': 'ssh', 'argv': sys.argv[1:]}) + '\\n')\n"
        "sys.stdout.write((d / 'reply.txt').read_text())\nsys.exit(int((d / 'rc').read_text() or 0))\n"
    )
    ap = tmp_path / "approvals"
    ap.write_text(
        "#!/usr/bin/env python3\nimport json, sys, pathlib\nd = pathlib.Path(sys.argv[0]).parent\n"
        "stdin = sys.stdin.read()\n"
        "row = {'bin': 'approvals', 'argv': sys.argv[1:], 'stdin': stdin}\n"
        "open(d / 'calls.jsonl', 'a').write(json.dumps(row) + '\\n')\n"
        "a = sys.argv[1:]\nsumm = a[a.index('--summary') + 1]\n"
        f"print(json.dumps({{'nonce': '{NONCE}', 'expires': 1.0, 'summary': summ, 'ref': a[a.index('--ref') + 1]}}))\n"
    )
    for f in (ssh, ap):
        f.chmod(0o755)
    (tmp_path / "rc").write_text("0")
    monkeypatch.setenv("HUBKIT_UNRAID_SSH_BIN", str(ssh))
    monkeypatch.setenv("HUBKIT_APPROVALS_BIN", str(ap))

    class F:
        def reply(self, text: str, rc: int = 0) -> None:
            (tmp_path / "reply.txt").write_text(text)
            (tmp_path / "rc").write_text(str(rc))

        def calls(self) -> list[dict]:
            return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []

    return F()


def test_action_string_matches_the_servers_printf():
    # hub-kit-runner.sh builds it with printf '{"action":"hub.kit.apply","facts":"%s","part":"%s"}'
    assert KA.action("voice", FACTS) == '{"action":"hub.kit.apply","facts":"' + FACTS + '","part":"voice"}'


def test_tier1_mints_an_approval_for_exactly_the_servers_action_and_summary(fakes):
    fakes.reply(server_reply("1"))
    rc, out = KA.request("tg")
    assert rc == 0 and out["tier"] == "1"
    ssh, ap = fakes.calls()
    assert ssh["argv"] == ["run", "hub-kit-review", "tg"]
    assert ap["stdin"] == KA.action("tg", FACTS)
    assert ap["argv"][ap["argv"].index("--summary") + 1] == "kit tg: image - ffffffff"
    assert ap["argv"][ap["argv"].index("--ref") + 1] == "hubkit:tg:ffffffffffff"
    assert ap["argv"][ap["argv"].index("--ttl") + 1] == str(KA.TTL)
    assert out["send"] == {"text": out["card"], "approval": {"nonce": NONCE}}
    assert out["card"].startswith("claude-hub kit: tg\nImage:")
    assert out["next"] == f"after his ✅: hub kit apply tg --facts {FACTS} --nonce {NONCE}"


def test_tier0_and_no_approval_mint_nothing(fakes):
    fakes.reply(server_reply("0"))
    rc, out = KA.request("tg")
    assert rc == 0 and out["next"] == f"hub kit apply tg --facts {FACTS}" and "send" not in out
    fakes.reply(server_reply("1"))
    rc, out = KA.request("tg", mint=False)
    assert rc == 0 and "send" not in out
    assert [c["bin"] for c in fakes.calls()] == ["ssh", "ssh"]


def test_paste_names_the_paste_and_the_reasons(fakes):
    fakes.reply(server_reply("paste", part="voice-off", why=["claude-hub-voice does not exist: nothing to stop"]))
    rc, out = KA.request("voice-off")
    assert rc == 3 and out["next"] == "hub kit paste voice-rollback"
    assert out["why"] == ["claude-hub-voice does not exist: nothing to stop"]


@pytest.mark.parametrize(
    "reply",
    [
        server_reply("1", action='{"action":"hub.kit.apply","facts":"' + "0" * 64 + '","part":"tg"}'),
        server_reply("2"),
        server_reply("1", summary="x" * 81),
        server_reply("1", part="voice"),
    ],
)
def test_a_malformed_or_skewed_review_is_refused(fakes, reply):
    fakes.reply(reply)
    with pytest.raises(HubError):
        KA.request("tg")
    assert all(c["bin"] == "ssh" for c in fakes.calls())


def test_review_failure_carries_the_servers_reason(fakes):
    fakes.reply("result\trefused\nwhy\tthe kit has no plain compose.tg.yaml\n", rc=3)
    with pytest.raises(HubError, match="no plain compose.tg.yaml"):
        KA.review("tg")


def test_unknown_part_never_reaches_the_server(fakes):
    for bad in ("hub", "pg", "claude-vault", "bot", "tg;id"):
        with pytest.raises(HubError):
            KA.request(bad)
    assert fakes.calls() == []


def test_apply_passes_facts_and_nonce_and_the_servers_answer(fakes):
    fakes.reply("result\tok\ntier\t1\nsummary\tkit tg: image - ffffffff\n")
    rc, res = KA.apply("tg", FACTS, KA.nonce_of(f"ap:{NONCE}:y"))
    assert rc == 0 and res["result"] == "ok"
    assert fakes.calls()[-1]["argv"] == ["run", "hub-kit-apply", "tg", FACTS, NONCE]
    rc, _ = KA.apply("tg", FACTS)
    assert fakes.calls()[-1]["argv"] == ["run", "hub-kit-apply", "tg", FACTS]
    for facts, nonce in (("abc", None), (FACTS, "zz"), (FACTS, "ap:" + NONCE + ":n:x")):
        with pytest.raises(HubError):
            KA.apply("tg", facts, KA.nonce_of(nonce))


def test_cli_apply_request_json_and_apply_exit_code(fakes, capsys):
    fakes.reply(server_reply("1"))
    assert C.main(["kit", "apply-request", "tg", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["approval"]["nonce"] == NONCE and out["tier"] == "1"
    fakes.reply("result\trefused\nwhy\tapproval: not approved (state pending)\n", rc=3)
    assert C.main(["kit", "apply", "tg", "--facts", FACTS, "--nonce", NONCE]) == 3
    assert "refused: approval: not approved" in capsys.readouterr().out
    fakes.reply("2026-10-23T10:00:00+0200\tapply\ttg\t0\tffffffffffff\t\tok\tkit tg: image - ffffffff\n")
    assert C.main(["kit", "apply-log"]) == 0
    assert "\tapply\ttg\t0" in capsys.readouterr().out
    assert sys.modules["hubkit.kitapply"] is KA
