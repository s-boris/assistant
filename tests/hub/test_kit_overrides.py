"""Every console paste that brings up claude-hub passes every installed compose override that changes the hub service.

`docker compose up -d hub` with a smaller file set than the running hub was created with recreates it without the
missing overrides (the tg files mount, HUB_VOICE, HUB_PHONE). The browser override does not change the hub service,
but a paste without it leaves claude-hub-browser out of the model: compose then warns "Found orphan containers
([claude-hub-browser]) … --remove-orphans" in the middle of his paste. So every paste passes every installed override,
the browser one with its env files (its image tag and uid come from browser/browser.env). Runs each paste's own
`set -- …` file-list statement in bash, in a folder where every override exists and in one where none does. Also: the
voice paste recreates the voice container, so a changed loader/voiceloader.py (a bind mount compose can't see)
actually runs."""

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HUB = REPO / "ops/hub"
OVERRIDES = ("compose.tg.yaml", "compose.voice.yaml", "compose.phone.yaml", "compose.browser.yaml")
# paste -> its own override (always in the list)
PASTES = {
    "install.sh": None,
    "tg-install.sh": "compose.tg.yaml",
    "voice-install.sh": "compose.voice.yaml",
    "phone-install.sh": "compose.phone.yaml",
    "browser-install.sh": "compose.browser.yaml",
}


def _hub_overrides() -> set[str]:
    return {o for o in OVERRIDES if re.search(r"(?m)^  hub:\s*$", (HUB / o).read_text())}


def _args(paste: str, folder: Path) -> list[str]:
    line = next(ln for ln in (HUB / paste).read_text().splitlines() if 'cd "$H"; set -- ' in ln)
    stmt = line.split('cd "$H"; ', 1)[1].split(" docker compose", 1)[0]
    return subprocess.run(
        ["bash", "-c", "set -euo pipefail; " + stmt + ' printf "%s\\n" "$@"'],
        cwd=folder,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()


def _file_list(paste: str, folder: Path) -> list[str]:
    out = _args(paste, folder)
    return [out[i + 1] for i, w in enumerate(out) if w == "-f"]


def _env_files(paste: str, folder: Path) -> list[str]:
    out = _args(paste, folder)
    return [out[i + 1] for i, w in enumerate(out) if w == "--env-file"]


def _installed(folder: Path, browser_env: bool = True) -> None:
    folder.mkdir()
    for o in OVERRIDES:
        (folder / o).write_text("x")
    if browser_env:
        (folder / "browser").mkdir()
        (folder / "browser/browser.env").write_text("x")


def test_the_hub_overrides_are_known():
    assert _hub_overrides() == {"compose.tg.yaml", "compose.voice.yaml", "compose.phone.yaml"}


@pytest.mark.parametrize("paste", sorted(PASTES))
def test_paste_passes_every_installed_hub_override(paste, tmp_path):
    allf, none = tmp_path / "all", tmp_path / "none"
    _installed(allf)
    none.mkdir()
    own = PASTES[paste]
    got = _file_list(paste, allf)
    assert got[0] == "compose.yaml"
    missing = _hub_overrides() - set(got)
    assert not missing, f"{paste}: `up -d hub` would recreate the hub without {sorted(missing)}"
    assert len(got) == len(set(got))
    if own:
        assert own in got
    # nothing installed besides compose.yaml: only the base and the paste's own override
    assert _file_list(paste, none) == ["compose.yaml"] + ([own] if own else [])


@pytest.mark.parametrize("paste", sorted(PASTES))
def test_paste_passes_every_installed_override_so_no_orphan_warning(paste, tmp_path):
    allf, none = tmp_path / "all", tmp_path / "none"
    _installed(allf)
    none.mkdir()
    got = _file_list(paste, allf)
    assert set(got) == {"compose.yaml", *OVERRIDES}, f"{paste}: compose would call an installed part an orphan"
    # the browser override interpolates BROWSER_TAG/UID/...: its env files come with it (.env first, as compose's own)
    assert _env_files(paste, allf) == [".env", "browser/browser.env"]
    assert _env_files(paste, none) == (
        [".env", "browser/browser.env"] if PASTES[paste] == "compose.browser.yaml" else []
    )


@pytest.mark.parametrize("paste", sorted(p for p in PASTES if PASTES[p] != "compose.browser.yaml"))
def test_browser_override_without_its_env_file_is_left_out(paste, tmp_path):
    half = tmp_path / "half"
    _installed(half, browser_env=False)  # the override without its env file (a half-done browser paste): left out
    assert "compose.browser.yaml" not in _file_list(paste, half)
    assert _env_files(paste, half) == []


def test_browser_paste_brings_the_hub_up_with_the_same_list():
    text = (HUB / "browser-install.sh").read_text()
    assert 'docker restart claude-hub >/dev/null; docker compose "$@" up -d --wait --wait-timeout 180 hub' in text


def test_voice_paste_recreates_the_voice_container():
    text = (HUB / "voice-install.sh").read_text()
    assert 'docker compose "$@" --profile voice up -d --force-recreate --wait --wait-timeout 150 voice' in text
