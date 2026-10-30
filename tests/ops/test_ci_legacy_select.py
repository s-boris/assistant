"""legacy_select: a CI run with a base runs the legacy suites its change can affect (ops/assistant.py's impact map:
a suite's script, the lib modules it imports, the data beside them, what it names, its suite dir, impact.toml), and
every suite when it can't tell (no base, the map's extras, a file in that area no suite claims, most suites)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

SUITES = ("s1", "s2", "s3", "s4", "s5", "s6")


def _entry(src: str, dest: str, suite: str = "", seam: str = "") -> str:
    out = f'\n[[file]]\nsrc = "{src}"\ndest = "{dest}"\n'
    return out + (f'suite = "{suite}"\n' if suite else "") + (f'seam = "{seam}"\n' if seam else "")


def make_tree(tmp: Path) -> Path:
    """s1 imports core.a (core/a.py opens rates.toml beside it), s2 imports botlib (scripts/lib/botlib.py ->
    bin/lib/botlib.py, which imports core.b), s3 runs s1 by name, s4..s6 plain shell; each has tests/legacy/<s>/."""
    t = tmp / "tree"
    files = {
        "core/__init__.py": "",
        "core/a.py": 'import os\nRATES = os.path.join(os.path.dirname(__file__), "rates.toml")\n',
        "core/rates.toml": "x = 1\n",
        "core/b.py": "B = 1\n",
        "core/lonely.py": "L = 1\n",
        "core/blob.json": "{}\n",
        "scripts/lib/botlib.py": "from core import b\n",
        "scripts/s1": "#!/usr/local/bin/python3.13 -I\nfrom core import a\n",
        "scripts/s2": "#!/usr/local/bin/python3.13 -I\nimport botlib\n",
        "scripts/s3": '#!/bin/sh\n"$HOME/bin/s1" --json\n',
        "packages/inbox/__init__.py": "",
        "packages/hub/x.py": "X = 1\n",
        "tests/legacy/slow.txt": "s1\n",
        "tests/runner-skip.txt": "tests/ops/test_x.py\n",
    }
    for n in SUITES[3:]:
        files[f"scripts/{n}"] = "#!/bin/sh\necho ok\n"
    for n in SUITES:
        files[f"tests/legacy/{n}/run.sh"] = "#!/bin/sh\nexit 0\n"
    man = '[core]\nsrc = "core"\ndest = "bin/lib/core"\n' + _entry("scripts/lib/botlib.py", "bin/lib/botlib.py")
    man += "".join(_entry(f"scripts/{n}", f"bin/{n}", n) for n in SUITES)
    files["ops/deploy/manifest.toml"] = man
    for rel, text in files.items():
        (t / rel).parent.mkdir(parents=True, exist_ok=True)
        (t / rel).write_text(text)
    return t


def git(t: Path, *a: str) -> str:
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid")
    env.update(GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    return subprocess.run(["git", "-C", str(t), *a], env=env, capture_output=True, text=True, check=True).stdout.strip()


def sel(tool, t: Path, changed, base=None) -> tuple[str, list[str]]:
    s = tool.legacy_select(t, tool.load_manifest(t), changed, base)
    return s["mode"], s.get("suites", [])


def test_the_selector_picks_the_suites_a_change_reaches(tool, tmp_path, monkeypatch):
    monkeypatch.setattr(tool.paths, "real_home", lambda: tmp_path / "rh")
    t = make_tree(tmp_path)
    assert sel(tool, t, ["scripts/s4"]) == ("impacted", ["s4"])  # its own script
    assert sel(tool, t, ["tests/legacy/s5/run.sh"]) == ("impacted", ["s5"])  # its suite dir (old: nothing ran)
    assert sel(tool, t, ["core/a.py"]) == ("impacted", ["s1"])  # a core module it imports (old: every suite)
    assert sel(tool, t, ["core/b.py"]) == ("impacted", ["s2"])  # through a bin/lib module's own import
    assert sel(tool, t, ["scripts/lib/botlib.py"]) == ("impacted", ["s2"])
    assert sel(tool, t, ["core/rates.toml"]) == ("impacted", ["s1"])  # a data file its module names beside it
    assert sel(tool, t, ["scripts/s1"]) == ("impacted", ["s1", "s3"])  # s3 runs s1 by name (the runner's ~/bin)
    assert sel(tool, t, ["core/lonely.py", "packages/hub/x.py", "docs/x.md"]) == ("impacted", [])  # nobody's
    assert sel(tool, t, ["tests/legacy/slow.txt"]) == ("impacted", [])  # start order only
    assert sel(tool, t, ["ops/assistant.py"]) == ("impacted", [])  # a land's CI runs the deployed driver


def test_it_falls_back_to_every_suite_when_it_cannot_tell(tool, tmp_path, monkeypatch):
    monkeypatch.setattr(tool.paths, "real_home", lambda: tmp_path / "rh")
    t = make_tree(tmp_path)
    assert sel(tool, t, None)[0] == "all"  # no base
    assert sel(tool, t, ["core/blob.json"])[0] == "all"  # a data file in the lib area that no suite names
    assert sel(tool, t, ["tests/legacy/helpers.sh"])[0] == "all"  # shared harness
    (t / "tests/legacy/impact.toml").write_text("[suites]\n")
    assert sel(tool, t, ["tests/legacy/impact.toml"])[0] == "all"  # the map's extras
    assert sel(tool, t, ["ops/deploy/manifest.toml"])[0] == "all"  # no base to diff the manifest against
    assert sel(tool, t, ["scripts/s1", "scripts/s2", "scripts/s4", "scripts/s5"])[0] == "all"  # > 60 % of them
    monkeypatch.setenv("ASSISTANT_CI_SELECT", "full")
    assert sel(tool, t, ["scripts/s4"])[0] == "all"
    monkeypatch.delenv("ASSISTANT_CI_SELECT")
    (t / "tests/legacy/impact.toml").write_text('[suites]\ns4 = ["core/blob.json", "docs/*.md"]\n')
    assert sel(tool, t, ["core/blob.json", "docs/a.md"]) == ("impacted", ["s4"])  # extras claim them


def test_manifest_and_runner_skip_changes_select_the_entries_they_change(tool, tmp_path, monkeypatch):
    monkeypatch.setattr(tool.paths, "real_home", lambda: tmp_path / "rh")
    t = make_tree(tmp_path)
    git(t, "init", "-q", "-b", "main")
    git(t, "add", "-A")
    git(t, "commit", "-q", "-m", "base")
    base = git(t, "rev-parse", "HEAD")
    m = t / "ops/deploy/manifest.toml"
    m.write_text(m.read_text().replace('suite = "s5"\n', 'suite = "s5"\nseam = "S5_BIN"\n'))
    (t / "tests/runner-skip.txt").write_text("tests/ops/test_x.py\ntests/legacy/s6\n")
    git(t, "commit", "-qam", "seam + skip")
    changed = ["ops/deploy/manifest.toml", "tests/runner-skip.txt"]
    assert sel(tool, t, changed, base) == ("impacted", ["s5", "s6"])
    s = tool.legacy_select(t, tool.load_manifest(t), ["core/a.py"], base)
    work = tmp_path / "w"
    work.mkdir()
    monkeypatch.setattr(tool, "_netguard", lambda: "")
    res: dict = {}
    picked: dict = {}
    _n, ok, why = tool._ci_legacy(t, ["core/a.py"], work, results=res, base=base, sel=picked)
    assert ok and list(res) == ["s1"] and picked["mode"] == s["mode"] == "impacted", why
    assert why.startswith("impacted 1/6 (") and why.endswith(": ran s1")


def test_land_size_on_this_repo(tool, monkeypatch):
    """The real tree: a one-script land runs its suite and a handful more, a leaf core module well under the
    fallback share, and the map builds in seconds (it runs twice per land: here and on the runner)."""
    import time

    repo = Path(__file__).resolve().parents[2]
    man = tool.load_manifest(repo)
    n = sum(1 for e in man["file"] if e.get("suite"))
    t0 = time.monotonic()
    one = tool.legacy_select(repo, man, ["scripts/kk-compare"], None)
    assert time.monotonic() - t0 < 60
    assert one["mode"] == "impacted" and "kk" in one["suites"] and len(one["suites"]) <= max(3, n // 10), one
    leaf = tool.legacy_select(repo, man, ["core/schedules.py"], None)
    assert leaf["mode"] == "impacted" and len(leaf["suites"]) < tool.LEGACY_FULL_SHARE * n, leaf
    assert tool.legacy_select(repo, man, ["core/__init__.py"], None)["mode"] == "all"  # imported everywhere


def test_legacy_main_runs_every_suite_on_main_once_and_notes_a_new_red(tool, tmp_path, monkeypatch):
    """The post-land main check: every suite on a checkout of main into main's record (how post-land); a suite green
    before and red now -> one ci-nightly note; main's sha already checked -> nothing runs."""
    monkeypatch.setattr(tool.paths, "real_home", lambda: tmp_path / "rh")
    t = make_tree(tmp_path)
    git(t, "init", "-q", "-b", "main")
    git(t, "add", "-A")
    git(t, "commit", "-q", "-m", "base")
    monkeypatch.setattr(tool.paths, "repo", lambda: t)
    seen: list[set] = []

    def fake_run(tree, changed, suites):
        seen.append(set(suites))
        assert changed is None and (tree / "ops/deploy/manifest.toml").is_file()
        return {s: {"ok": s != "s2", "detail": "exit 1" if s == "s2" else "", "src": f"scripts/{s}"} for s in suites}

    monkeypatch.setattr(tool, "_legacy_run", fake_run)
    monkeypatch.setattr(tool.os, "getloadavg", lambda: (0.0, 0.0, 0.0))  # the host's load: not this test's
    tool.legacy_main_record({"s2": {"ok": True, "detail": "", "src": "scripts/s2"}}, "old", "land")
    assert tool.legacy_main_check("branch-x") == 0
    assert seen == [set(SUITES)]
    rec = tool.legacy_main()
    assert (rec["s2"]["ok"], rec["s2"]["how"], rec["s1"]["ok"]) == (False, "post-land", True)
    notes = list(tool.paths.inbox().glob("ci-nightly-*.md"))
    assert len(notes) == 1 and "s2" in notes[0].read_text() and "branch-x" in notes[0].read_text()
    assert tool.legacy_main_check("branch-y") == 0 and len(seen) == 1  # same main: already checked
