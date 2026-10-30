"""assistant init (core/starter.py) and the profile's feature switches (assistant_profile features.*)."""

import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TEMPLATES = REPO / "templates"
PROFILE_PY = REPO / "scripts" / "lib" / "legacy" / "assistant_profile.py"
EXAMPLE = REPO / "docs" / "profile.example.json"
FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)


def load_profile_module(monkeypatch, path=None):
    if path is not None:
        monkeypatch.setenv("ASSISTANT_PROFILE", str(path))
    spec = importlib.util.spec_from_file_location("assistant_profile_t", PROFILE_PY)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def manifest():
    return tomllib.loads((TEMPLATES / "manifest.toml").read_text())


def write_profile(home: Path, features=None, **extra) -> Path:
    prof = json.loads(EXAMPLE.read_text())
    prof["owner"]["first_name"] = "Robin"
    if features is not None:
        prof["features"].update(features)
    prof.update(extra)
    p = home / ".config" / "assistant" / "profile.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(prof))
    p.chmod(0o600)
    return p


@pytest.fixture
def ienv(env):
    """The CLI env with the templates and the profile module in its fake repo."""
    shutil.copytree(TEMPLATES, env.repo / "templates")
    lib = env.repo / "scripts" / "lib" / "legacy"
    lib.mkdir(parents=True)
    shutil.copy(PROFILE_PY, lib / "assistant_profile.py")
    (env.home / "bin").mkdir()
    (env.home / "bin" / "secretscan.py").write_text("#!/bin/sh\n")
    return env


def installed(home: Path) -> set[str]:
    return {str(p.relative_to(home)) for p in home.rglob("*") if p.is_file() and ".config" not in p.parts}


# --- templates -----------------------------------------------------------------------------------------------------


def test_every_listed_template_exists_and_every_template_is_listed():
    m = manifest()
    skills = {p.name for p in (TEMPLATES / "claude" / "skills").iterdir() if p.is_dir()}
    agents = {p.stem for p in (TEMPLATES / "claude" / "agents").glob("*.md")}
    assert skills == set(m["skills"])
    assert agents == set(m["agents"])
    for src in m["workspace"]:
        assert (TEMPLATES / src).is_file(), src
    known = set(m["features"])
    for table in ("skills", "agents", "hooks"):
        for name, need in m[table].items():
            assert set([need] if isinstance(need, str) else need) <= known, (table, name)


def test_feature_names_match_the_profile_switches(monkeypatch, tmp_path):
    prof = load_profile_module(monkeypatch, tmp_path / "none.json")
    assert set(manifest()["features"]) == set(prof.FEATURES)
    assert set(json.loads(EXAMPLE.read_text())["features"]) == set(prof.FEATURES) - {"core"}


def test_frontmatter_is_valid_yaml_with_matching_names():
    yaml = pytest.importorskip("yaml")
    files = list((TEMPLATES / "claude").rglob("*.md"))
    assert len(files) == len(manifest()["skills"]) + len(manifest()["agents"])
    for f in files:
        m = FRONTMATTER.match(f.read_text())
        assert m, f
        fm = yaml.safe_load(m.group(1))
        want = f.parent.name if f.name == "SKILL.md" else f.stem
        assert fm["name"] == want, f
        assert isinstance(fm["description"], str) and len(fm["description"]) > 20, f


def test_placeholders_are_profile_keys(monkeypatch, tmp_path):
    prof = load_profile_module(monkeypatch, tmp_path / "none.json")
    keys = set(prof.keys(prof.DEFAULTS))
    for f in TEMPLATES.rglob("*.md"):
        for key in re.findall(r"\{\{\s*([a-z_.]+)\s*\}\}", f.read_text()):
            assert key in keys, (f, key)


def test_settings_example_loads_and_names_only_listed_hooks():
    s = json.loads((TEMPLATES / "claude" / "settings.example.json").read_text())
    hooks = manifest()["hooks"]
    cmds = [h["command"] for groups in s["hooks"].values() for g in groups for h in g["hooks"]]
    assert cmds
    for c in cmds:
        assert any(name in c for name in hooks), c
        assert "/home/" not in c and "token" not in c.lower(), c


def test_hook_files_ship_with_the_deploy():
    deployed = (REPO / "ops" / "deploy" / "manifest.toml").read_text()
    for name in manifest()["hooks"]:
        assert f'dest = ".claude/hooks/{name}"' in deployed or f'dest = "bin/{name}"' in deployed, name


# --- feature switches ----------------------------------------------------------------------------------------------


def test_features_default_on_and_example_is_core_telegram(monkeypatch, tmp_path):
    prof = load_profile_module(monkeypatch, tmp_path / "missing.json")
    assert prof.features_on() == list(prof.FEATURES)
    p = tmp_path / "p.json"
    p.write_text(EXAMPLE.read_text())
    p.chmod(0o600)
    prof = load_profile_module(monkeypatch, p)
    assert prof.features_on() == ["core", "telegram"]
    assert prof.feature_on("media") is False
    assert prof.feature_on("not-a-feature") is True


def test_core_cannot_be_turned_off_and_require_feature_exits_3(monkeypatch, tmp_path, capsys):
    p = tmp_path / "p.json"
    p.write_text(json.dumps({"features": {"media": False}}))
    prof = load_profile_module(monkeypatch, p)
    assert prof.feature_on("core") is True
    prof.require_feature("hub", "x")  # on: returns
    with pytest.raises(SystemExit) as e:
        prof.require_feature("media", "media")
    assert e.value.code == 3
    assert "media: feature media is off (features.media in" in capsys.readouterr().err


def test_wrong_type_for_a_switch_is_a_profile_error(monkeypatch, tmp_path):
    p = tmp_path / "p.json"
    p.write_text(json.dumps({"features": {"media": "no"}}))
    prof = load_profile_module(monkeypatch, p)
    with pytest.raises(prof.ProfileError):
        prof.feature_on("media")


def test_profile_get_feature_flags(tmp_path):
    p = tmp_path / "p.json"
    p.write_text(json.dumps({"features": {"media": False}}))
    p.chmod(0o600)
    env = {"ASSISTANT_PROFILE": str(p), "HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    tool = REPO / "scripts" / "profile-get"

    def run(*a):
        return subprocess.run(
            [sys.executable, "-I", str(tool), *a], env=env, capture_output=True, text=True, timeout=30
        )

    r = run("--feature", "media")
    assert (r.returncode, r.stdout.strip()) == (3, "off")
    r = run("--feature", "hub")
    assert (r.returncode, r.stdout.strip()) == (0, "on")
    assert run("--feature", "nope").returncode == 2
    assert "media" not in run("--features").stdout.split()
    assert run("--check").returncode == 0
    assert run("features").stdout.strip() == '{"media":false}'
    p.write_text(json.dumps({"features": {"medai": True}}))
    r = run("--check")
    assert "unknown key features.medai" in r.stderr
    p.unlink()
    assert run("features").stdout.strip() == "{}"  # no section: cron install's "core only" default


@pytest.mark.parametrize("tool,feature", [("browser", "browser"), ("unraid-ssh", "unraid"), ("unraid-do", "unraid")])
def test_shell_tools_stop_when_their_feature_is_off(tmp_path, tool, feature):
    b = tmp_path / "bin"
    (b / "lib").mkdir(parents=True)
    for f in (tool, "profile-get"):
        shutil.copy(REPO / "scripts" / f, b / f)
        (b / f).chmod(0o755)
    shutil.copy(PROFILE_PY, b / "lib" / "assistant_profile.py")
    p = tmp_path / "p.json"
    p.write_text(json.dumps({"features": {feature: False}}))
    p.chmod(0o600)
    py = shutil.which("python3.13") or "/usr/local/bin/python3.13"
    if not Path(py).exists():  # profile-get's shebang
        pytest.skip("no python3.13")
    env = {"ASSISTANT_PROFILE": str(p), "HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    r = subprocess.run([str(b / tool), "status"], env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 3
    assert f"{tool}: feature {feature} is off" in r.stderr


# --- assistant init ------------------------------------------------------------------------------------------------


def test_init_installs_core_and_telegram_only(ienv):
    write_profile(ienv.home)
    r = ienv.run("init")
    assert r.returncode == 0, r.stderr
    got = installed(ienv.home)
    m = manifest()
    for dest in m["workspace"].values():
        assert f"workspace/{dest}" in got
    assert ".claude/settings.json" in got
    for name, need in m["skills"].items():
        want = set(need) <= {"core", "telegram"}
        assert (f".claude/skills/{name}/SKILL.md" in got) is want, name
    for name in m["agents"]:
        assert f".claude/agents/{name}.md" in got
    claude_md = (ienv.home / "workspace" / "CLAUDE.md").read_text()
    assert "Robin" in claude_md and "{{" not in claude_md
    s = json.loads((ienv.home / ".claude" / "settings.json").read_text())
    cmds = json.dumps(s)
    assert "hub-stamp.py" not in cmds and "agenda.sh" in cmds and "tg-status" in cmds
    assert (ienv.home / "bin" / "secretscan").is_symlink()
    assert "features on: core, telegram" in r.stdout
    assert "skipped" in r.stdout and "feature media off" in r.stdout


def test_init_is_idempotent_and_keeps_existing_files(ienv):
    write_profile(ienv.home)
    assert ienv.run("init").returncode == 0
    before = {p: (ienv.home / p).read_bytes() for p in installed(ienv.home)}
    r = ienv.run("init")
    assert r.returncode == 0 and "installed" not in r.stdout and "updated" not in r.stdout
    assert {p: (ienv.home / p).read_bytes() for p in installed(ienv.home)} == before
    claude_md = ienv.home / "workspace" / "CLAUDE.md"
    claude_md.write_text("# mine\n")
    r = ienv.run("init")
    assert "kept" in r.stdout and claude_md.read_text() == "# mine\n"
    r = ienv.run("init", "--force")
    assert r.returncode == 0 and "updated" in r.stdout
    assert "Robin" in claude_md.read_text()
    assert (claude_md.parent / "CLAUDE.md.bak").read_text() == "# mine\n"


def test_init_keeps_an_existing_settings_json_and_writes_the_starter_beside_it(ienv):
    write_profile(ienv.home)
    s = ienv.home / ".claude" / "settings.json"
    s.parent.mkdir(parents=True)
    s.write_text('{"theme": "dark"}\n')
    r = ienv.run("init")
    assert r.returncode == 0
    assert s.read_text() == '{"theme": "dark"}\n'
    starter = json.loads((ienv.home / ".claude" / "settings.starter.json").read_text())
    assert "hooks" in starter and "settings.starter.json" in r.stdout


def test_init_installs_more_when_features_go_on(ienv):
    write_profile(ienv.home, features={"media": True, "hub": True, "browser": True})
    r = ienv.run("init", "--json")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert {"media", "hub", "browser"} <= set(out["features"])
    acts = {(i["kind"], i["name"]): i["action"] for i in out["items"]}
    assert acts[("skill", "media-requests")] == "installed"
    assert acts[("skill", "reminders")] == "installed"
    assert acts[("skill", "web-agent")] == "installed"
    assert acts[("skill", "home-assistant")] == "skipped"
    assert "hub-stamp.py" in (ienv.home / ".claude" / "settings.json").read_text()


def test_init_dry_run_writes_nothing(ienv):
    write_profile(ienv.home)
    r = ienv.run("init", "--dry-run")
    assert r.returncode == 0 and "would-install" in r.stdout
    assert installed(ienv.home) == {"bin/secretscan.py"}


def _core_telegram_only(home: Path, got: set[str]) -> None:
    for name, need in manifest()["skills"].items():
        want = set([need] if isinstance(need, str) else need) <= {"core", "telegram"}
        assert (f".claude/skills/{name}/SKILL.md" in got) is want, name


def test_init_without_a_profile_creates_one_with_core_and_telegram(ienv):
    r = ienv.run("init")
    assert r.returncode == 0, r.stderr
    p = ienv.home / ".config" / "assistant" / "profile.json"
    assert json.loads(p.read_text()) == {
        "features": {f: f == "telegram" for f in manifest()["features"] if f != "core"}
    }
    assert p.stat().st_mode & 0o777 == 0o600
    assert "installed     profile" in r.stdout and "features on: core, telegram" in r.stdout
    _core_telegram_only(ienv.home, installed(ienv.home))


def test_init_writes_the_features_section_into_a_new_profile_without_one(ienv):
    p = write_profile(ienv.home)
    prof = json.loads(p.read_text())
    del prof["features"]
    p.write_text(json.dumps(prof))
    r = ienv.run("init", "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "would-install profile" in r.stdout and "features on: core, telegram" in r.stdout
    assert "feature media off" in r.stdout and "features" not in json.loads(p.read_text())  # dry run: unchanged
    r = ienv.run("init")
    assert r.returncode == 0, r.stderr
    after = json.loads(p.read_text())
    assert after["features"] == {f: f == "telegram" for f in manifest()["features"] if f != "core"}
    assert {k: v for k, v in after.items() if k != "features"} == prof  # the other keys as they were
    assert p.stat().st_mode & 0o777 == 0o600
    _core_telegram_only(ienv.home, installed(ienv.home))
    r = ienv.run("init")  # idempotent: the section is there now
    assert r.returncode == 0 and "profile" not in r.stdout


def test_init_keeps_an_existing_install_without_features_all_on(ienv):
    """An instance that already has its CLAUDE.md and a profile without `features` (an older install): nothing is
    written into the profile and every switch stays on."""
    p = write_profile(ienv.home)
    prof = json.loads(p.read_text())
    del prof["features"]
    p.write_text(json.dumps(prof))
    before = p.read_text()
    (ienv.home / "workspace").mkdir()
    (ienv.home / "workspace" / "CLAUDE.md").write_text("# mine\n")
    r = ienv.run("init")
    assert r.returncode == 0, r.stderr
    assert p.read_text() == before and "profile" not in r.stdout
    got = installed(ienv.home)
    for name in manifest()["skills"]:
        assert f".claude/skills/{name}/SKILL.md" in got


def test_starter_settings_deny_the_unsafe_browser_tool(ienv):
    write_profile(ienv.home)
    assert ienv.run("init").returncode == 0
    s = json.loads((ienv.home / ".claude" / "settings.json").read_text())
    assert "mcp__browser__browser_run_code_unsafe" in s["permissions"]["deny"]


def test_proactive_log_ships_and_the_templates_name_it():
    assert manifest()["workspace"]["workspace/PROACTIVE.md"] == "PROACTIVE.md"
    assert "a dated PROACTIVE.md line" in (TEMPLATES / "workspace" / "CLAUDE.md").read_text()
    users = [f.name for f in (TEMPLATES / "claude").rglob("*.md") if "PROACTIVE.md" in f.read_text()]
    assert users  # the skills and agents that read or log it: the file they name ships


def test_weekly_digest_has_no_fixed_topics():
    text = (TEMPLATES / "claude" / "agents" / "weekly-digest.md").read_text()
    assert "interests.topics" in text
    for fixed in ("**AI**", "**Markets**", "**Games**", "Tech & science", "BTC"):
        assert fixed not in text, fixed


def test_init_refuses_a_broken_profile_without_values(ienv):
    p = write_profile(ienv.home)
    p.write_text('{"features": {"media": "secret-looking-value"}}')
    r = ienv.run("init")
    assert r.returncode == 1
    assert "features.media" in r.stderr and "secret-looking-value" not in r.stderr
    assert not (ienv.home / "workspace" / "CLAUDE.md").exists()


def test_filter_settings_drops_empty_groups_and_events():

    sys.path.insert(0, str(REPO))
    from core import starter

    s = {
        "hooks": {
            "PreToolUse": [{"matcher": "mcp__hub__.*", "hooks": [{"command": '"$HOME/.claude/hooks/hub-stamp.py"'}]}],
            "Stop": [{"hooks": [{"command": "x/conduct.py stop"}, {"command": "y/tg-status hook Stop"}]}],
        }
    }
    out = starter.filter_settings(
        s, {"hub-stamp.py": ["hub"], "conduct.py": ["core"], "tg-status": ["telegram"]}, {"core"}
    )
    assert out == {"hooks": {"Stop": [{"hooks": [{"command": "x/conduct.py stop"}]}]}}
    assert "PreToolUse" in s["hooks"]  # the input is untouched
