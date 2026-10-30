"""Every hub release carries the phone tree, like voice (tests/hub/test_voice_release.py has the trap and
the in-place amend). A release without it is marked bad by claude-hub-phone's loader ('no phone entry'): the last good
release runs on, or with none the loader idles and his app is offline. `hub phone status` names what the loader would
run and `hub phone start` refuses a CURRENT without a runnable phone tree."""

import importlib.util
import json
from pathlib import Path

from hubkit import release as R

REPO = Path(__file__).resolve().parents[2]
RID, SHA = "abcdef123456", "a" * 40


def _mod(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


PL = _mod("phoneloader_rel", REPO / "ops/hub/phoneloader.py")


def _phone_tree(src: Path) -> Path:
    """A small gateway tree (and the voice filter it loads) in src, its pins = the fake wheel of ops/hub/vendor.toml."""
    for rel, body in {"adapters/phone/run.py": "print('phone')\n", "adapters/phone/phonegw/__init__.py": "",
                      "adapters/phone/protocol/schema/a.json": "{}\n",
                      "adapters/voice/voice_relay/answer_filter.py": "X=1\n"}.items():  # fmt: skip
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_text(body)
    (src / R.PHONE_PINS).write_text((src / "ops/hub/vendor.toml").read_text())
    return src


def _meta(d: Path) -> dict:
    return json.loads((d / "release.json").read_text())


def test_phone_is_in_every_release(hubhome, srctree):
    assert R.PHONE_IN_RELEASE is True and "HUB_PHONE_IN_RELEASE" not in Path(R.__file__).read_text(), "in the code"
    assert "phone" not in R.optional(srctree) and R.phone_sources(srctree) == {}, "a tree without the gateway"
    _phone_tree(srctree)
    assert "phone" in R.optional(srctree)
    d, built = R.build(srctree, RID, SHA)
    meta = _meta(d)
    assert built and meta["entry"]["phone"] == R.PHONE_ENTRY and "phone/app/answer_filter.py" in meta["files"]
    assert any(k.startswith("phone/vendor/fakepkg/") for k in meta["files"])
    assert R.runnable(RID, "phone") is None
    assert PL.verify_release(str(d.parent), RID) is None, "the loader's own check passes"


def test_the_real_tree_builds_the_phone_entry():
    """The repo itself (no build: the sources a deploy of this commit would copy)."""
    assert "phone" in R.optional(REPO)
    src = R.phone_sources(REPO)
    assert src[R.PHONE_ENTRY].is_file() and all(p.is_file() for p in src.values())


def test_a_release_built_without_phone_gets_it_added(hubhome, srctree, monkeypatch):
    _phone_tree(srctree)
    monkeypatch.setattr(R, "PHONE_IN_RELEASE", False)
    d, _ = R.build(srctree, RID, SHA)
    assert PL.verify_release(str(d.parent), RID).startswith("release.json: no phone entry")
    assert R.runnable(RID, "phone") == "no phone entry (built without it)"
    monkeypatch.setattr(R, "PHONE_IN_RELEASE", True)
    assert R.missing(srctree, RID) == {"phone"}
    d2, built = R.build(srctree, RID, SHA)
    assert d2 == d and built and _meta(d)["entry"]["phone"] == R.PHONE_ENTRY
    assert R.runnable(RID, "phone") is None and PL.verify_release(str(d.parent), RID) is None


def test_hub_phone_status_and_start_name_the_release_the_loader_would_run(hubhome, srctree, monkeypatch, capsys):
    from hubkit import phone as HP

    assert HP.current() == {"release": None, "phone": "no CURRENT"}
    _phone_tree(srctree)
    monkeypatch.setattr(R, "PHONE_IN_RELEASE", False)
    R.build(srctree, RID, SHA)
    R.write_current(RID)
    assert HP.current() == {"release": RID, "phone": "no phone entry (built without it)"}
    (hubhome / ".phonegw-off").write_text("")
    monkeypatch.setattr(HP, "gateway_running", lambda: False)
    posts = []
    monkeypatch.setattr(HP.hub, "call", lambda *a, **k: posts.append(a) or {"changed": True, "ts": 0})
    a = type("A", (), {"force": False, "why": "w", "wait": 0.0, "json": False})()
    assert HP.start(a) == 2 and "can't run the gateway (no phone entry" in capsys.readouterr().out
    assert posts == [], "the hold stays"
    monkeypatch.setattr(R, "PHONE_IN_RELEASE", True)
    R.build(srctree, RID, SHA)
    assert HP.current() == {"release": RID, "phone": "ok"}
    assert HP.start(a) == 0 and posts and posts[0][:2] == ("POST", "/phone/hold")
    held = {"held": True, "why": "x", "loader": {"state": "held", "release": None, "age_s": 3}}
    monkeypatch.setattr(HP, "_get", lambda: dict(held))
    capsys.readouterr()
    assert HP.status(a) == 0 and capsys.readouterr().out.strip().endswith(f"· CURRENT {RID} phone ok")
    a.json = True
    assert HP.status(a) == 0 and json.loads(capsys.readouterr().out)["current"] == {"release": RID, "phone": "ok"}
