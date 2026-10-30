"""Running an existing CLI for a typed tool: one subprocess, no shell, stdin closed, typed failures.

Domains wrap the live CLIs (~/bin/vault, ~/bin/ha, ~/bin/calcache, ...) so their own checks stay in force: the
vault's policy filter, ha's allowlist and read-back, calcache's privacy reduction. Each domain names its binary with
an env seam (TOOLS_<NAME>_BIN) for tests. Values a caller passes become argv elements of their own; a value that
starts with "-" is refused by the input models (`no_dash`), so it can never be read as an option.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from typing import Any

from tools.core import ToolError


def home() -> str:
    return os.path.expanduser("~")


def bin_path(env: str, name: str) -> str:
    """The binary for a tool: $ENV (tests), else ~/bin/NAME."""
    return os.environ.get(env) or os.path.join(home(), "bin", name)


def run(exe: str, argv: list[str], *, timeout: float = 30.0, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    """(exit code, stdout, stderr) of EXE ARGV; a missing binary = unavailable, a timeout = timeout."""
    name = os.path.basename(exe)
    try:
        p = subprocess.run(
            [exe, *argv],
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env=dict(os.environ, **(env or {})),
        )
    except FileNotFoundError as e:
        raise ToolError("unavailable", f"{name} is not installed", f"is ~/bin/{name} there?") from e
    except subprocess.TimeoutExpired as e:
        raise ToolError("timeout", f"{name} {argv[0] if argv else ''} took over {timeout:.0f} s", "retry once") from e
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def first_line(*texts: str, default: str = "") -> str:
    for t in texts:
        for ln in (t or "").splitlines():
            if ln.strip():
                return ln.strip()[:300]
    return default


def parse_json(out: str, what: str) -> Any:
    try:
        return json.loads(out)
    except ValueError as e:
        raise ToolError("failed", f"{what}: not JSON", f"run `{what}` by hand to see why") from e


def no_dash(v: str | None) -> str | None:
    """Pydantic after-validator for free text that becomes an argv element: never an option."""
    if v is not None and v.lstrip().startswith("-"):
        raise ValueError("must not start with '-' (it would read as an option)")
    return v


def fail_default(rc: int, out: str, err: str, what: str) -> ToolError:
    """A CLI's failure as a typed error: exit 2 = invalid_args (argparse and usage refusals), else failed."""
    msg = first_line(err, out, default=f"{what}: exit {rc}")
    return ToolError("invalid_args" if rc == 2 else "failed", msg, f"run `{what}` by hand to see why")


def run_json(
    exe: str,
    argv: list[str],
    what: str,
    *,
    ok: tuple[int, ...] = (0,),
    timeout: float = 30.0,
    fail: Callable[[int, str, str, str], ToolError] | None = None,
    env: dict[str, str] | None = None,
) -> tuple[int, Any]:
    """(exit code, parsed stdout) of a CLI that prints JSON; an exit outside OK (or no output) -> FAIL's ToolError."""
    rc, out, err = run(exe, argv, timeout=timeout, env=env)
    if rc not in ok or not out:
        raise (fail or fail_default)(rc, out, err, what)
    return rc, parse_json(out, what)
