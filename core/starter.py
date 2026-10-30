"""core.starter: `assistant init`, the starter files for a new user's ~/.claude and ~/workspace. Stdlib only.

The repo's templates/ tree holds a neutral CLAUDE.md and its workspace files, skills, agents and an example
settings.json; templates/manifest.toml names the profile feature switches (features.* in
~/.config/assistant/profile.json, read through assistant_profile) each one needs. init copies what the enabled
features need, fills {{dotted.key}} placeholders from the profile, and never overwrites an existing file without
force (a forced overwrite keeps the old bytes as <file>.bak). Running it twice changes nothing.

  plan(repo, home, profile, on=None)   the list of Item (kind, name, src, dest, features, on)
  run(repo, home, force, dry_run)      do it; returns the Result rows (installed/updated/kept/same/skipped)
  filter_settings(obj, hooks, on)      settings with the hook entries of features that are off removed
  new_features(profile, home)          the `features` section a new instance gets ({switch: bool}), or None

Feature switches: a switch the profile leaves out counts as on (an existing install without a `features` section
keeps everything). A new instance (no ~/workspace/CLAUDE.md yet) whose profile has no `features` section gets an
explicit one first: core and telegram on, every other switch false (STARTER_ON); init writes it into the profile
(the file is created, mode 0600, when there is none) and says so, then installs from it.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PLACEHOLDER = re.compile(r"\{\{\s*([a-z_]+(?:\.[a-z_]+)+)\s*\}\}")
DIRS = ("workspace", "inbox", ".claude/skills", ".claude/agents")
STARTER_ON = ("telegram",)  # a new instance's switches besides core; every other switch is written as false
SETTINGS_STARTER = ".claude/settings.starter.json"  # the filtered example, beside a settings.json init keeps


@dataclass
class Item:
    kind: str  # workspace | settings | skill | agent | link
    name: str
    src: Path
    dest: Path
    features: list[str] = field(default_factory=list)
    on: bool = True


@dataclass
class Result:
    action: str  # installed | updated | kept | same | skipped | would-install | would-update
    item: Item
    note: str = ""


def profile_module(repo: Path):
    """assistant_profile from the repo (the same tree as the templates), else the deployed ~/bin/lib copy."""
    for cand in (
        repo / "scripts" / "lib" / "legacy" / "assistant_profile.py",
        Path(os.path.expanduser("~")) / "bin" / "lib" / "assistant_profile.py",
    ):
        if cand.is_file():
            spec = importlib.util.spec_from_file_location("assistant_profile", cand)
            assert spec and spec.loader
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    raise FileNotFoundError("assistant_profile.py not found (repo scripts/lib/legacy or ~/bin/lib)")


def load_manifest(repo: Path) -> dict:
    with open(repo / "templates" / "manifest.toml", "rb") as f:
        return tomllib.load(f)


def _needs(v) -> list[str]:
    return [v] if isinstance(v, str) else list(v)


def new_features(profile, home: Path) -> dict[str, bool] | None:
    """The explicit `features` section for a new instance, or None: only when ~/workspace/CLAUDE.md does not exist
    yet and the profile file has no `features` key (or there is no profile file)."""
    if (home / "workspace" / "CLAUDE.md").exists():
        return None
    p = Path(profile.path())
    if p.exists():
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None  # a broken profile: the profile module reports it (never its values)
        if not isinstance(raw, dict) or "features" in raw:
            return None
    return {f: f in STARTER_ON for f in profile.FEATURES if f != "core"}


def write_features(profile, feats: dict[str, bool]) -> Path:
    """Add the `features` section to the profile file (other keys and their order kept), or create the file with
    only that section (folder 0700, file 0600)."""
    p = Path(profile.path())
    if p.exists():
        raw = json.loads(p.read_text(encoding="utf-8"))
        mode = p.stat().st_mode & 0o777
    else:
        p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        raw, mode = {}, 0o600
    raw["features"] = feats
    _write(p, (json.dumps(raw, indent=2, ensure_ascii=False) + "\n").encode(), mode)
    return p


def _on(profile, on) -> set[str]:
    return set(profile.features_on() if on is None else on) | {"core"}


def plan(repo: Path, home: Path, profile, on=None) -> list[Item]:
    man = load_manifest(repo)
    t = repo / "templates"
    on = _on(profile, on)
    items: list[Item] = []
    for src, dest in man.get("workspace", {}).items():
        items.append(Item("workspace", dest, t / src, home / "workspace" / dest, ["core"]))
    s = man.get("settings") or {}
    if s:
        items.append(Item("settings", "settings.json", t / s["src"], home / s["dest"], ["core"]))
    for name, need in sorted(man.get("skills", {}).items()):
        items.append(
            Item(
                "skill",
                name,
                t / "claude" / "skills" / name / "SKILL.md",
                home / ".claude" / "skills" / name / "SKILL.md",
                _needs(need),
            )
        )
    for name, need in sorted(man.get("agents", {}).items()):
        items.append(
            Item(
                "agent",
                name,
                t / "claude" / "agents" / f"{name}.md",
                home / ".claude" / "agents" / f"{name}.md",
                _needs(need),
            )
        )
    for it in items:
        it.on = all(f in on for f in it.features)
    return items


def render(text: str, profile) -> tuple[str, list[str]]:
    """Fill {{dotted.key}} from the profile; unknown keys stay as they are and are returned."""
    unknown: list[str] = []

    def sub(m: re.Match) -> str:
        try:
            return profile.as_text(profile.get(m.group(1)))
        except KeyError:
            unknown.append(m.group(1))
            return m.group(0)

    return PLACEHOLDER.sub(sub, text), unknown


def _hook_files(cmd: str, hooks: dict) -> list[str]:
    return [h for h in hooks if re.search(r"(^|[/\s\"'])" + re.escape(h) + r"($|[\s\"';|&])", cmd)]


def filter_settings(obj: dict, hooks: dict, on: set[str]) -> dict:
    """A copy of the settings without the hook commands whose file (manifest [hooks]) needs a feature that is off;
    matchers and events left empty are dropped."""
    out = json.loads(json.dumps(obj))
    events = out.get("hooks") or {}
    for ev in list(events):
        groups = []
        for g in events[ev]:
            hs = [
                h
                for h in g.get("hooks", [])
                if all(f in on for name in _hook_files(h.get("command", ""), hooks) for f in _needs(hooks[name]))
            ]
            if hs:
                groups.append({**g, "hooks": hs})
        if groups:
            events[ev] = groups
        else:
            del events[ev]
    return out


def content(it: Item, repo: Path, profile, on=None) -> tuple[bytes, list[str]]:
    if it.kind == "settings":
        man = load_manifest(repo)
        on = _on(profile, on)
        obj = filter_settings(json.loads(it.src.read_text(encoding="utf-8")), man.get("hooks", {}), on)
        return (json.dumps(obj, indent=2, ensure_ascii=False) + "\n").encode(), []
    text, unknown = render(it.src.read_text(encoding="utf-8"), profile)
    return text.encode(), unknown


def _write(dest: Path, data: bytes, mode: int = 0o644) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.init-tmp")
    tmp.write_bytes(data)
    os.chmod(tmp, mode)
    os.replace(tmp, dest)


def run(repo: Path, home: Path, force: bool = False, dry_run: bool = False) -> list[Result]:
    profile = profile_module(repo)
    out: list[Result] = []
    on = None  # the profile's switches; a dry run of a new instance plans with the section it would write
    feats = new_features(profile, home)
    if feats is not None:
        pp = Path(profile.path())
        note = "features: core, " + ", ".join(STARTER_ON) + " on; every other switch false"
        if dry_run:
            on = ["core", *STARTER_ON]
        else:
            write_features(profile, feats)
        out.append(Result("would-install" if dry_run else "installed", Item("profile", "features", pp, pp), note))
    if not dry_run:
        for d in DIRS:
            (home / d).mkdir(parents=True, exist_ok=True)
    for link, target in load_manifest(repo).get("links", {}).items():
        lp = home / link
        if lp.is_symlink() or lp.exists() or not (lp.parent / target).exists():
            continue
        if not dry_run:
            lp.symlink_to(target)
        out.append(
            Result(
                "would-install" if dry_run else "installed",
                Item("link", lp.name, lp.parent / target, lp),
                f"-> {target}",
            )
        )
    have = _on(profile, on)
    for it in plan(repo, home, profile, on):
        if not it.on:
            off = [f for f in it.features if f not in have]
            out.append(Result("skipped", it, f"feature {', '.join(off)} off"))
            continue
        if not it.src.is_file():
            raise FileNotFoundError(f"template missing: {it.src}")
        data, unknown = content(it, repo, profile, on)
        note = ("unknown placeholder(s): " + ", ".join(sorted(set(unknown)))) if unknown else ""
        exists = it.dest.exists()
        if exists and it.dest.read_bytes() == data:
            out.append(Result("same", it, note))
            continue
        if exists and not force:
            if it.kind == "settings" and not dry_run:
                _write(home / SETTINGS_STARTER, data)
                note = f"starter version in ~/{SETTINGS_STARTER}: merge its hooks, or --force"
            out.append(Result("kept", it, note or "exists; --force overwrites"))
            continue
        if dry_run:
            out.append(Result("would-update" if exists else "would-install", it, note))
            continue
        if exists:
            shutil.copy2(it.dest, it.dest.with_name(it.dest.name + ".bak"))
        _write(it.dest, data)
        out.append(Result("updated" if exists else "installed", it, note))
    return out


def summary(results: list[Result], features: list[str]) -> str:
    counts: dict[str, int] = {}
    for r in results:
        counts[r.action] = counts.get(r.action, 0) + 1
    parts = ", ".join(f"{n} {a}" for a, n in sorted(counts.items()))
    return f"init: {parts or 'nothing to do'} (features on: {', '.join(features)})"
