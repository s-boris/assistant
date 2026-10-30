"""hubkit: the brain side of the claude-hub stack. Stdlib only; deployed next to core in ~/bin/lib.

  hubkit.release   releases in ~/.local/share/claude-hub/releases (one dir per release id + release.json with a
                   sha256 per file), the CURRENT pointer the hub's loader follows, prune
  hubkit.kit       the install kit in ~/.local/share/claude-hub/kit: compose.yaml, hub.env (values from the
                   profile), the loader, the credential names to copy, the install/rollback pastes and the step card
  hubkit.client    the hub's /health and /whoami (HUB_URL, default http://claude-hub:8700; token ~/.secrets/hub-token)
  hubkit.cli       the `hub` command

The hub reaches these files through its read-only bind of the releases folder; the owner's console paste reads the
kit through the server path of this home (profile server.bot_home).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from core import paths

__all__ = [
    "CONTAINERS",
    "DEFAULT_URL",
    "HubError",
    "kit_dir",
    "releases_dir",
    "secrets_list_file",
    "settings",
    "share_dir",
    "token_file",
]

DEFAULT_URL = "http://claude-hub:8700"
CONTAINERS = (
    "claude-hub",
    "claude-hub-pg",
    "claude-hub-phoenix",
    "claude-hub-ci",
    "claude-hub-tg",
    "claude-hub-voice",
    "claude-hub-phone",
)
# Public placeholders, as in assistant_profile.DEFAULTS; the profile holds the real values.
PROFILE_DEFAULTS = {
    "server.appdata": "/mnt/user/appdata",
    "server.bot_home": "/mnt/user/appdata/claude-bot/home",
    "server.bot_network": "claude",
    "locale.tz": "UTC",
}
HOST_PATH = re.compile(r"^/(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+$")
NET_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
TZ_NAME = re.compile(r"^[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)*$")


class HubError(Exception):
    def __init__(self, msg: str, code: int = 1):
        super().__init__(msg)
        self.code = code


def share_dir(*parts: str) -> Path:
    return paths.guard(paths.home().joinpath(".local", "share", "claude-hub", *parts))


def releases_dir(*parts: str) -> Path:
    return share_dir("releases", *parts)


def kit_dir(*parts: str) -> Path:
    return share_dir("kit", *parts)


def token_file() -> Path:
    return paths.guard(os.environ.get("HUB_TOKEN_FILE") or paths.home() / ".secrets" / "hub-token")


def secrets_list_file() -> Path:
    """Names of the credential files the install paste copies into the hub (main session's file; names only)."""
    return paths.guard(paths.home() / ".config" / "claude-hub" / "secrets.copy")


def _profile_file() -> Path:
    return paths.guard(os.environ.get("ASSISTANT_PROFILE") or paths.home() / ".config" / "assistant" / "profile.json")


def settings() -> dict:
    """The kit's host-side values from the profile (never printed by callers except where the kit needs them):
    hub_root, bot_home, brain_network, tz, plus `source` ("profile" or "defaults") and `defaulted` (key names)."""
    pf = _profile_file()
    raw: dict = {}
    if pf.is_file():
        try:
            raw = json.loads(pf.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise HubError(f"profile unreadable ({type(e).__name__}): fix it, then render again") from None
        if not isinstance(raw, dict):
            raise HubError("profile is not a JSON object")
    vals: dict[str, str] = {}
    defaulted: list[str] = []
    for key, dflt in PROFILE_DEFAULTS.items():
        sec, leaf = key.split(".")
        section = raw.get(sec)
        v = section.get(leaf) if isinstance(section, dict) else None
        if v is None or v == "":
            v = dflt
            defaulted.append(key)
        if not isinstance(v, str):
            raise HubError(f"profile {key}: not a string")
        vals[key] = v.rstrip("/") if key.startswith("server.") else v
    for key in ("server.appdata", "server.bot_home"):
        if not HOST_PATH.match(vals[key]) or ".." in vals[key].split("/"):
            raise HubError(f"profile {key}: want an absolute path of plain names")
    if not NET_NAME.match(vals["server.bot_network"]):
        raise HubError("profile server.bot_network: not a docker network name")
    if not TZ_NAME.match(vals["locale.tz"]):
        raise HubError("profile locale.tz: not a zone name")
    return {
        "hub_root": vals["server.appdata"] + "/claude-hub",
        "bot_home": vals["server.bot_home"],
        "brain_network": vals["server.bot_network"],
        "tz": vals["locale.tz"],
        "source": "profile" if not defaulted else "defaults" if len(defaulted) == len(PROFILE_DEFAULTS) else "mixed",
        "defaulted": defaulted,
    }
