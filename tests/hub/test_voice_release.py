"""Every hub release carries the voice tree, and a release id built while a switch was off never comes back
without it. The trap: the release id is the commit's, so a second deploy of the same commit with a switch turned on
must not reuse the first build (no voice entry): "live (ok)" while claude-hub-voice's loader idles with "no runnable
release (CURRENT bad, no last good)". Voice is on in the code (VOICE_IN_RELEASE), build() adds a missing tree in
place (nothing the release has changes), `hub voice status` names what the loader would run, `hub voice start`
refuses a CURRENT without a runnable voice tree, and the deploy says what the live release carries."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from hubkit import HubError
from hubkit import release as R

REPO = Path(__file__).resolve().parents[2]
TOOL = REPO / "ops" / "assistant.py"
RID, SHA = "abcdef123456", "a" * 40


def _mod(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


VL = _mod("voiceloader_rel", REPO / "ops/hub/voiceloader.py")


def _relay_tree(src: Path, phone: bool = False) -> Path:
    """A small relay (and phone gateway) tree in src, its pins = the fake wheel of ops/hub/vendor.toml (offline)."""
    voice = {
        "run.py": "print('relay')\n",
        "voice_relay/__init__.py": "",
        "voice_relay/answer_filter.py": "X = 1\n",
        "prompts/voice-agent.md": "# p\n",
        "static/index.html": "<p>x</p>\n",
    }
    gw = {"run.py": "print('phone')\n", "phonegw/__init__.py": "", "protocol/schema/a.json": "{}\n"}
    pins = (src / "ops/hub/vendor.toml").read_text()
    trees = [("adapters/voice", voice, R.VOICE_PINS)] + ([("adapters/phone", gw, R.PHONE_PINS)] if phone else [])
    for base, files, pin in trees:
        for rel, body in files.items():
            (src / base / rel).parent.mkdir(parents=True, exist_ok=True)
            (src / base / rel).write_text(body)
        (src / pin).write_text(pins)
    return src


def _meta(d: Path) -> dict:
    return json.loads((d / "release.json").read_text())


def test_voice_is_in_every_release(hubhome, srctree):
    assert R.VOICE_IN_RELEASE is True and "HUB_VOICE_IN_RELEASE" not in Path(R.__file__).read_text(), "in the code"
    assert R.optional(srctree) == set() and R.voice_sources(srctree) == {}, "a tree without the relay: no voice"
    _relay_tree(srctree)
    assert R.optional(srctree) == {"voice"}
    d, built = R.build(srctree, RID, SHA)
    meta = _meta(d)
    assert built and meta["entry"]["voice"] == R.VOICE_ENTRY
    assert (
        "voice/app/voice_relay/answer_filter.py" in meta["files"]
        and "voice/app/prompts/voice-agent.md" in meta["files"]
    )
    assert any(k.startswith("voice/vendor/fakepkg/") for k in meta["files"])
    assert R.verify(RID) is None and R.runnable(RID, "voice") is None
    assert VL.verify_release(str(d.parent), RID) is None, "the loader's own check passes"
    assert R.runnable(RID, "phone") == "no phone entry (built without it)"


def test_a_release_built_with_the_switch_off_gets_the_tree_added_in_place(hubhome, srctree, monkeypatch):
    _relay_tree(srctree)
    monkeypatch.setattr(R, "VOICE_IN_RELEASE", False)
    d, built = R.build(srctree, RID, SHA)
    old = _meta(d)
    assert built and "voice" not in old["entry"]
    assert VL.verify_release(str(d.parent), RID).startswith("release.json: no voice entry")
    hubd = d / "app/hubd.py"
    ino, body = hubd.stat().st_ino, hubd.read_bytes()
    monkeypatch.setattr(R, "VOICE_IN_RELEASE", True)
    assert R.missing(srctree, RID) == {"voice"}
    d2, built = R.build(srctree, RID, SHA)  # the 2nd deploy of the same commit: must not reuse the voice-less build
    new = _meta(d2)
    assert d2 == d and built and new["entry"]["voice"] == R.VOICE_ENTRY and new["amended"] >= old["built"]
    assert new["built"] == old["built"] and set(old["files"]) < set(new["files"])
    assert hubd.stat().st_ino == ino and hubd.read_bytes() == body, "files it had stay as they are (no swap)"
    assert R.verify(RID) is None and VL.verify_release(str(d.parent), RID) is None
    assert oct((d / "voice/app/voice_relay").stat().st_mode & 0o777) == "0o755"
    assert oct((d / "voice/run.py").stat().st_mode & 0o777) == "0o644"
    assert not list(d.parent.glob(".stage-*")) and not list(d.glob(".release.json.*"))
    assert R.build(srctree, RID, SHA) == (d, False), "complete now: reused"


def test_amend_refuses_when_the_same_id_would_get_other_bytes(hubhome, srctree, monkeypatch):
    _relay_tree(srctree)
    monkeypatch.setattr(R, "VOICE_IN_RELEASE", False)
    d, _ = R.build(srctree, RID, SHA)
    before = (d / "release.json").read_bytes()
    (srctree / "packages/hub/hubd.py").write_text((srctree / "packages/hub/hubd.py").read_text() + "# other\n")
    monkeypatch.setattr(R, "VOICE_IN_RELEASE", True)
    with pytest.raises(HubError, match="app/hubd.py differs from a fresh build"):
        R.build(srctree, RID, SHA)
    assert (d / "release.json").read_bytes() == before and not (d / "voice").exists() and R.verify(RID) is None
    assert not list(d.parent.glob(".stage-*"))


def test_a_release_with_more_trees_is_reused(hubhome, srctree, monkeypatch):
    _relay_tree(srctree)
    d, _ = R.build(srctree, RID, SHA)
    monkeypatch.setattr(R, "VOICE_IN_RELEASE", False)
    assert R.build(srctree, RID, SHA) == (d, False) and "voice" in R.entries(RID)


def test_hub_voice_status_and_start_name_the_release_the_loader_would_run(hubhome, srctree, monkeypatch, capsys):
    from hubkit import voice as HV

    assert HV.current() == {"release": None, "voice": "no CURRENT"}
    _relay_tree(srctree)
    monkeypatch.setattr(R, "VOICE_IN_RELEASE", False)
    R.build(srctree, RID, SHA)
    R.write_current(RID)
    assert HV.current() == {"release": RID, "voice": "no voice entry (built without it)"}
    (hubhome / ".voice-relay-off").write_text("")
    monkeypatch.setattr(HV, "relay_running", lambda: False)
    a = type("A", (), {"force": False, "why": "w", "wait": 0.0, "json": False})()
    assert HV.start(a) == 2 and "can't run the relay (no voice entry" in capsys.readouterr().out
    monkeypatch.setattr(R, "VOICE_IN_RELEASE", True)
    R.build(srctree, RID, SHA)
    assert HV.current() == {"release": RID, "voice": "ok"}
    held = {"held": True, "why": "x", "loader": {"state": "held", "release": None, "age_s": 3}}
    monkeypatch.setattr(HV, "_get", lambda: dict(held))
    assert HV.status(a) == 0 and capsys.readouterr().out.strip().endswith(f"· CURRENT {RID} voice ok")
    a.json = True
    assert HV.status(a) == 0 and json.loads(capsys.readouterr().out)["current"] == {"release": RID, "voice": "ok"}


def _run(repo: Path, *args: str, **env: str):
    e = dict(os.environ, ASSISTANT_REPO=str(repo), ASSISTANT_LOCK_WAIT="30")
    e.pop("HUB_PHONE_IN_RELEASE", None)
    e.update(env)
    return subprocess.run([sys.executable, "-I", str(TOOL), *args], env=e, capture_output=True, text=True, timeout=120)


def test_deploy_of_the_same_commit_with_a_switch_on_adds_the_tree(hubhome, hubrepo, fakehub, monkeypatch):
    """The deploy path end to end: the commit's release built earlier without phone (here: in-process with the switch
    off, as an older brain built it) gets the tree added by the next deploy of that commit."""
    repo, commit = hubrepo
    _relay_tree(repo, phone=True)
    sha = commit("relay + phone trees")
    rid = sha[:12]
    rel = hubhome / ".local/share/claude-hub/releases"
    monkeypatch.setattr(R, "PHONE_IN_RELEASE", False)
    R.build(repo, rid, sha, root=rel)
    monkeypatch.setattr(R, "PHONE_IN_RELEASE", True)
    assert R.entries(rid, rel) == {"ci", "hub", "voice"}
    r = _run(repo, "deploy", "--target", "hub", "--plan")
    assert r.returncode == 0 and "kept already; adds phone" in r.stdout, r.stderr + r.stdout
    r = _run(repo, "deploy", "--target", "hub", "--wait", "10")
    assert r.returncode == 0, r.stderr + r.stdout
    assert f"release {rid}: kept; phone added" in r.stdout and "carries ci hub phone voice" in r.stdout
    meta = _meta(rel / rid)
    assert meta["entry"]["phone"] == R.PHONE_ENTRY and meta["entry"]["voice"] == R.VOICE_ENTRY
