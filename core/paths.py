"""core.paths: where the harness keeps things. Stdlib only.

  home()            $HOME (the only source of truth; tests point it at a temp dir)
  state(*parts)     ~/.local/state/assistant/...
  config(*parts)    ~/.config/assistant/...
  inbox(*parts)     ~/inbox/...
  bin_dir(*parts)   ~/bin/...
  repo(*parts)      the harness repo (~/opt/assistant, or $ASSISTANT_REPO)
  real_home()       the account's home from the password database ($HOME is ignored)

Test guard: with ASSISTANT_TEST=1 every path these functions return must lie outside real_home(); a path under
it raises RealHomeRefused, so a test that forgot its temp HOME fails before it can touch live state. The one
exception is ci_scratch() (~/.cache/aci): local CI run dirs (pytest's basetemp, so every tmp_path and temp HOME)
live there, on the appdata disk instead of /tmp in the docker.img layer; it never holds live state.
  ci_scratch()      ~/.cache/aci under the real home (local CI run dirs)
  protected(p)      True when p (or what it resolves to) is under the real home and not under ci_scratch()
"""

from __future__ import annotations

import os
import pwd
from pathlib import Path

__all__ = [
    "RealHomeRefused",
    "bin_dir",
    "ci_scratch",
    "config",
    "guard",
    "home",
    "inbox",
    "protected",
    "real_home",
    "repo",
    "state",
    "test_mode",
]


class RealHomeRefused(RuntimeError):
    """A path under the real home was requested while ASSISTANT_TEST=1."""


def test_mode() -> bool:
    return os.environ.get("ASSISTANT_TEST", "") not in ("", "0")


def real_home() -> Path:
    """The account's home from the password database; a uid without an entry (the hub's ci container) has no real
    home to protect: ASSISTANT_REAL_HOME, else a path that never exists."""
    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    except KeyError:
        return Path(os.environ.get("ASSISTANT_REAL_HOME") or "/nonexistent-home")


def _under(p: Path, root: Path) -> bool:
    return p == root or root in p.parents


def ci_scratch() -> Path:
    """Local CI run dirs (ops/assistant.py CI_ROOT): throwaway, never live state, so the test guard allows them."""
    return real_home() / ".cache" / "aci"


def protected(path: str | os.PathLike[str]) -> bool:
    """True when path or its resolved target lies under the real home outside ci_scratch() (the run dir itself
    is protected too: only paths strictly inside it are scratch)."""
    p = Path(os.path.abspath(os.fspath(path)))
    rh, sc = real_home(), ci_scratch()
    return any(_under(q, rh) and sc not in q.parents for q in (p, p.resolve()))


def guard(path: str | os.PathLike[str]) -> Path:
    """Return path (absolute, normalized); in test mode refuse anything under the real home (ci_scratch() aside)."""
    p = Path(os.path.abspath(os.fspath(path)))
    if test_mode():
        if protected(p):
            raise RealHomeRefused(f"ASSISTANT_TEST=1: refusing a path under the real home: {p}")
    return p


def home() -> Path:
    h = os.environ.get("HOME", "")
    if not h:
        if test_mode():
            raise RealHomeRefused("ASSISTANT_TEST=1 needs HOME set to a temp dir")
        return real_home()
    return guard(h)


def state(*parts: str) -> Path:
    return guard(home().joinpath(".local", "state", "assistant", *parts))


def config(*parts: str) -> Path:
    return guard(home().joinpath(".config", "assistant", *parts))


def inbox(*parts: str) -> Path:
    return guard(home().joinpath("inbox", *parts))


def bin_dir(*parts: str) -> Path:
    return guard(home().joinpath("bin", *parts))


def repo(*parts: str) -> Path:
    r = os.environ.get("ASSISTANT_REPO", "")
    base = Path(r) if r else home() / "opt" / "assistant"
    return guard(base.joinpath(*parts))
