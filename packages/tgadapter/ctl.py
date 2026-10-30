"""tgadapter.ctl: keep the bun tg-adapter (adapters/tg) up, report on it, stop it. Stdlib only.

Every call takes an instance, "test" (default) or "live": config ~/.config/tg-adapter/<i>.json (test: config.json
while test.json is absent), switch ~/.config/tg-adapter/<i>.off (test: also ~/.config/tg-adapter/off), state
~/.local/state/tg-adapter/<i>/ (adapter.pid, adapter.log, ensure.log, inbox/). A test adapter running with its PID
file in the state root (~/.local/state/tg-adapter/adapter.pid) is stopped by the next ensure, which then starts the
instance's own.

  ensure(force=False)   nothing at all while ~/.config/tg-adapter/config.json is absent ("not configured"); the
                        switch file ~/.config/tg-adapter/off keeps it down. Running → nothing (restarted when
                        adapters/tg changed on the repo's main checkout since it started). Down → `cli.ts check`
                        (config + token interlocks, no network), then started detached from the main checkout
                        (`bun install --frozen-lockfile` first when node_modules is missing). Held down without a
                        restart: after an exit that won't heal by itself (2 config, 3 409 Conflict, 4 401, 5 wrong
                        bot) until config.json or the token file changes or --force; after 3 starts within 15 min
                        (crash loop) until 15 min after the first of them. Each held-down state is reported once as
                        an event + ~/inbox note (core.events.emit).
  status()              configured, token present (yes/no, never the file's path), switch, running pid, last exit,
                        the adapter's own status (`cli.ts status --json`), cron entry
  stop(keep_down)       SIGTERM (10 s, then SIGKILL) to the adapter; keep_down also writes the switch file
Hub mode (the live instance with ~/.config/tg-adapter/live.hub present; TG_ADAPTER_HUB_FILE in tests): the adapter
runs in the hub (claude-hub-tg), never here. ensure keeps the in-container one down (stopped, live.off written) and
keeps its brain-side companion up instead (`bun adapters/tg/src/cli.ts companion --instance live`: bind runs,
transcripts, scans and presence for the hub's adapter; companion.pid, companion.log, companion-ensure.log in the
instance state dir; ~/.config/tg-adapter/live.companion-off keeps it down; restarted when adapters/tg changed, the
same crash-loop backoff). status then reports the hub side: the companion, the hub's outbox consumer and the
adapter's counters (GET /tg/meta), never a secret.
Files: ~/.local/state/tg-adapter/ (TG_ADAPTER_STATE_DIR): adapter.pid (the adapter's), adapter.log (its stderr,
0600, rotated at 1 MiB at a start), ensure.log (start/exit lines). Seams (tests): TG_ADAPTER_CONFIG,
TG_ADAPTER_OFF_FILE, TG_ADAPTER_STATE_DIR (shared with the bun side), TG_ADAPTER_DIR (the adapter source,
default <repo>/adapters/tg), TG_ADAPTER_BUN (default `bun` on PATH, else /usr/local/bin/bun).
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core import clock, events, paths

__all__ = ["EXIT_MEANING", "PERMANENT", "ensure", "status", "stop"]

EXIT_MEANING = {
    0: "stopped",
    1: "error",
    2: "config or token file refused",
    3: "409 Conflict: another poller or a webhook holds this bot's token",
    4: "401 Unauthorized: the token is not valid",
    5: "getMe names another bot than config.bot",
    6: "another instance holds the PID file",
    7: "switch file present",
}
PERMANENT = (2, 3, 4, 5)
LOOP_N, LOOP_S = 3, 900.0
LOG_MAX = 1 << 20
MARKER = "tg-adapter"
COMPANION_MARKER = "tg-adapter-companion"  # adapters/tg/src/companion.ts: the companion.pid's second line
# The companion's launcher: as LAUNCH, its exit code into companion-ensure.log.
COMPANION_LAUNCH = (
    'umask 077; "$0" "$1" companion --instance "$2"; rc=$?; '
    'printf "%s exit %s\\n" "$(date +%s)" "$rc" >> "$TG_ADAPTER_COMPANION_LOG"; exit "$rc"'
)
# The launcher: runs the adapter, then appends its exit code to ensure.log (umask 077: files 0600).
LAUNCH = (
    'umask 077; "$0" "$1" run --instance "$2"; rc=$?; '
    'printf "%s exit %s\\n" "$(date +%s)" "$rc" >> "$TG_ADAPTER_ENSURE_LOG"; exit "$rc"'
)


# ------------------------------------------------------------------------------------------------ paths


def _env_path(var: str, *default: str) -> Path:
    v = os.environ.get(var)
    p = Path(os.path.expanduser(v)) if v else paths.home().joinpath(*default)
    return paths.guard(p)


INSTANCES = ("test", "live")


def _inst(instance: str) -> str:
    if instance not in INSTANCES:
        raise ValueError(f"instance must be one of {'|'.join(INSTANCES)}")
    return instance


def config_file(instance: str = "test") -> Path:
    if os.environ.get("TG_ADAPTER_CONFIG"):
        return _env_path("TG_ADAPTER_CONFIG")
    p = paths.guard(paths.home() / ".config" / "tg-adapter" / f"{_inst(instance)}.json")
    legacy = p.with_name("config.json")
    return legacy if instance == "test" and not p.exists() and legacy.exists() else p


def off_files(instance: str = "test") -> list[Path]:
    if os.environ.get("TG_ADAPTER_OFF_FILE"):
        return [_env_path("TG_ADAPTER_OFF_FILE")]
    d = paths.guard(paths.home() / ".config" / "tg-adapter")
    return [d / f"{_inst(instance)}.off", *([d / "off"] if instance == "test" else [])]


def hub_file(instance: str = "test") -> Path:
    """The instance runs in the hub while this marker exists (live.hub)."""
    if os.environ.get("TG_ADAPTER_HUB_FILE"):
        return _env_path("TG_ADAPTER_HUB_FILE")
    return paths.guard(paths.home() / ".config" / "tg-adapter" / f"{_inst(instance)}.hub")


def hub_mode(instance: str = "test") -> bool:
    return hub_file(instance).exists()


def companion_off_file(instance: str = "test") -> Path:
    return paths.guard(paths.home() / ".config" / "tg-adapter" / f"{_inst(instance)}.companion-off")


def off_file(instance: str = "test") -> Path:
    """The instance's own switch file (the one stop --keep-down writes)."""
    return off_files(instance)[0]


def switch_on(instance: str = "test") -> bool:
    return any(f.exists() for f in off_files(instance))


def state_root() -> Path:
    return _env_path("TG_ADAPTER_STATE_DIR", ".local", "state", "tg-adapter")


def state_dir(instance: str = "test") -> Path:
    return state_root() / _inst(instance)


def adapter_dir() -> Path:
    v = os.environ.get("TG_ADAPTER_DIR")
    return Path(v) if v else paths.repo("adapters", "tg")  # source code: read-only, never guarded


def bun() -> str:
    return os.environ.get("TG_ADAPTER_BUN") or shutil.which("bun") or "/usr/local/bin/bun"


def _home_path(p: str) -> Path:
    """A config path: "~/x" is $HOME-relative (the bun side expands it the same way)."""
    p = p.strip()
    return paths.home() / p[2:] if p.startswith("~/") else Path(p)


def _default_token_file() -> Path:
    # Where the owner saves the test bot's token (plan §10); only consulted while no config names one.
    return paths.home() / ".secrets" / "tg-test-bot.env"


# ------------------------------------------------------------------------------------------------ state


def read_config(instance: str = "test") -> dict[str, Any] | None:
    try:
        c = json.loads(config_file(instance).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {}
    return c if isinstance(c, dict) else {}


def token_state(cfg: dict[str, Any] | None, instance: str = "test") -> dict[str, Any]:
    """Present = a non-empty regular file (stat only: the token is never read here). Never returns the path."""
    tf = cfg.get("token_file") if cfg else None
    if not (isinstance(tf, str) and tf.strip()) and instance != "test":
        return {"present": False, "mode_ok": None, "from": "config"}
    p = _home_path(tf) if isinstance(tf, str) and tf.strip() else _default_token_file()
    try:
        st = os.stat(p)
    except OSError:
        return {"present": False, "mode_ok": None, "from": "config" if tf else "default"}
    ok = (st.st_mode & 0o170000) == 0o100000 and st.st_size > 0
    return {"present": ok, "mode_ok": (st.st_mode & 0o077) == 0 if ok else None, "from": "config" if tf else "default"}


def _pid_from(f: Path, marker: str = MARKER) -> int | None:
    try:
        first, second, *_ = f.read_text(encoding="utf-8").split("\n") + ["", ""]
        pid = int(first)
    except (OSError, ValueError):
        return None
    if pid <= 1 or second.strip() != marker:
        return None
    try:
        os.kill(pid, 0)
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (OSError, ProcessLookupError):
        return None
    return pid if (b"cli.ts" in cmd or MARKER.encode() in cmd) else None


def running_pid(instance: str = "test") -> int | None:
    return _pid_from(state_dir(instance) / "adapter.pid")


def companion_pid(instance: str = "test") -> int | None:
    return _pid_from(state_dir(instance) / "companion.pid", COMPANION_MARKER)


def legacy_pid(instance: str = "test") -> int | None:
    """A test adapter running with its PID file in the state root."""
    return _pid_from(state_root() / "adapter.pid") if instance == "test" else None


def _elog(instance: str = "test") -> Path:
    return state_dir(instance) / "ensure.log"


def _clog(instance: str = "test") -> Path:
    return state_dir(instance) / "companion-ensure.log"


def _note(line: str, instance: str = "test") -> None:
    events.append_private(_elog(instance), f"{clock.now():.3f} {line}")


def history(instance: str = "test", log: Path | None = None) -> list[tuple[float, str, str]]:
    try:
        text = (log or _elog(instance)).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for ln in text.splitlines()[-200:]:
        parts = ln.split(" ", 2)
        try:
            out.append((float(parts[0]), parts[1], parts[2] if len(parts) > 2 else ""))
        except (ValueError, IndexError):
            continue
    return out


def last_start(h: list[tuple[float, str, str]]) -> tuple[float, str] | None:
    for ts, kind, rest in reversed(h):
        if kind == "start":
            tree = rest.split("tree=", 1)[1].split()[0] if "tree=" in rest else "-"
            return ts, tree
    return None


def last_exit(h: list[tuple[float, str, str]]) -> tuple[float, int] | None:
    """The exit after the last start (None while it runs or before any start)."""
    for ts, kind, rest in reversed(h):
        if kind == "exit":
            try:
                return ts, int(rest.split()[0])
            except (ValueError, IndexError):
                return ts, 1
        if kind == "start":
            return None
    return None


def tree_id() -> str:
    """The git tree of the adapter dir at HEAD ("" when it is not a git checkout)."""
    try:
        r = subprocess.run(
            ["git", "-C", str(adapter_dir()), "rev-parse", "HEAD:./"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.strip()[:12] if r.returncode == 0 else ""


def _mtime(p: Path) -> float:
    try:
        return os.stat(p).st_mtime
    except OSError:
        return 0.0


def _changed_since(ts: float, cfg: dict[str, Any] | None, instance: str = "test") -> bool:
    tf = cfg.get("token_file") if cfg else None
    tok = _mtime(_home_path(tf)) if isinstance(tf, str) and tf.strip() else 0.0
    return max(_mtime(config_file(instance)), tok) > ts


def _report(kind: str, name: str, subject: str, nxt: str, payload: dict[str, Any], instance: str = "test") -> None:
    """One event + inbox note per held-down episode (the note name is the idem key: repeats are no-ops)."""
    try:
        events.emit(
            kind=kind,
            subject=subject,
            next=nxt,
            payload=payload,
            labels=("tg", "tg-adapter"),
            source="tg-adapter",
            note_name=name,
        )
    except (OSError, ValueError) as e:
        _note(f"report-failed {type(e).__name__}", instance)


# ------------------------------------------------------------------------------------------------ actions


def _run_cli(args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "TELEGRAM_BOT_TOKEN"}
    return subprocess.run(
        [bun(), str(adapter_dir() / "src" / "cli.ts"), *args],
        cwd=str(adapter_dir()),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def install() -> str | None:
    """`bun install --frozen-lockfile` when node_modules is missing (before check, run, enqueue, status); None = ok."""
    return _install()


def _install() -> str | None:
    d = adapter_dir()
    if (d / "node_modules").is_dir():
        return None
    try:
        r = subprocess.run(
            [bun(), "install", "--frozen-lockfile"], cwd=str(d), capture_output=True, text=True, timeout=300
        )
    except (OSError, subprocess.SubprocessError) as e:
        return f"bun install failed: {type(e).__name__}"
    return None if r.returncode == 0 else "bun install failed: " + ((r.stderr.strip().splitlines() or ["?"])[-1])[:160]


def _start(tree: str, instance: str = "test") -> tuple[int | None, str]:
    sd = state_dir(instance)
    old = os.umask(0o077)
    try:
        sd.mkdir(parents=True, exist_ok=True)
        log = sd / "adapter.log"
        if _mtime(log) and os.stat(log).st_size > LOG_MAX:
            os.replace(log, sd / "adapter.log.1")
        fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    finally:
        os.umask(old)
    env = {k: v for k, v in os.environ.items() if k != "TELEGRAM_BOT_TOKEN"}
    env["TG_ADAPTER_ENSURE_LOG"] = str(_elog(instance))
    _note(f"start tree={tree or '-'}", instance)
    try:
        p = subprocess.Popen(
            ["sh", "-c", LAUNCH, bun(), str(adapter_dir() / "src" / "cli.ts"), instance],
            cwd=str(adapter_dir()),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=fd,
            stderr=fd,
            start_new_session=True,
        )
    finally:
        os.close(fd)
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        pid = running_pid(instance)
        if pid:
            return pid, f"started (pid {pid})"
        if p.poll() is not None:
            ex = last_exit(history(instance))
            code = ex[1] if ex else p.returncode
            return None, f"exited at once: {code} ({EXIT_MEANING.get(code, 'error')}); see adapter.log"
        time.sleep(0.1)
    return None, "starting (no PID file yet)"


def _kill(pid: int, wait_s: float, alive: Callable[[], int | None]) -> bool:
    """SIGTERM, then SIGKILL after wait_s; True when it took SIGKILL."""
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline and alive() == pid:
        time.sleep(0.1)
    if alive() == pid:
        os.kill(pid, signal.SIGKILL)
        return True
    return False


def stop(keep_down: bool = False, wait_s: float = 10.0, instance: str = "test") -> tuple[int, str]:
    msg = []
    cpid = companion_pid(instance)
    if cpid and (not hub_mode(instance) or companion_off_file(instance).exists()):  # nothing to do / held off
        _kill(cpid, wait_s, lambda: companion_pid(instance))
        msg.append(f"stopped the companion (pid {cpid})")
    old = legacy_pid(instance)
    if old:
        _kill(old, wait_s, lambda: legacy_pid(instance))
        _note(f"stopped-legacy pid={old}", instance)
        msg.append(f"stopped the state-root adapter (pid {old})")
    if keep_down:
        f = off_file(instance)
        f.parent.mkdir(parents=True, exist_ok=True)
        if not f.exists():
            f.write_text(f"tg-adapter stop --keep-down at {clock.now():.0f}\n", encoding="utf-8")
        msg.append("switch file written: ensure keeps it down")
    pid = running_pid(instance)
    if not pid:
        return 0, "; ".join(["not running", *msg])
    if _kill(pid, wait_s, lambda: running_pid(instance)):
        _note(f"killed pid={pid}", instance)
        msg.insert(0, f"stopped pid {pid} (SIGKILL after {wait_s:g} s)")
    else:
        msg.insert(0, f"stopped pid {pid}")
    if not keep_down:
        msg.append(f"cron ensure starts it again unless ~/.config/tg-adapter/{instance}.off exists (stop --keep-down)")
    return 0, "; ".join(msg)


def _start_companion(tree: str, instance: str) -> tuple[int | None, str]:
    sd = state_dir(instance)
    old = os.umask(0o077)
    try:
        sd.mkdir(parents=True, exist_ok=True)
        log = sd / "companion.log"
        if _mtime(log) and os.stat(log).st_size > LOG_MAX:
            os.replace(log, sd / "companion.log.1")
        fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with open(_clog(instance), "a", encoding="utf-8") as f:
            f.write(f"{clock.now():.0f} start tree={tree or '-'}\n")
    finally:
        os.umask(old)
    env = {k: v for k, v in os.environ.items() if k != "TELEGRAM_BOT_TOKEN"}
    env["TG_ADAPTER_COMPANION_LOG"] = str(_clog(instance))
    try:
        p = subprocess.Popen(
            ["sh", "-c", COMPANION_LAUNCH, bun(), str(adapter_dir() / "src" / "cli.ts"), instance],
            cwd=str(adapter_dir()),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=fd,
            stderr=fd,
            start_new_session=True,
        )
    finally:
        os.close(fd)
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        pid = companion_pid(instance)
        if pid:
            return pid, f"companion started (pid {pid})"
        if p.poll() is not None:
            ex = last_exit(history(instance, _clog(instance)))
            return None, f"companion exited at once: {ex[1] if ex else p.returncode}; see companion.log"
        time.sleep(0.1)
    return None, "companion starting (no PID file yet)"


def _ensure_hub(force: bool, instance: str) -> tuple[int, str]:
    """Hub mode: the in-container adapter stays down (stopped, switch file written); the companion stays up."""
    notes = []
    if running_pid(instance) or legacy_pid(instance) or not off_file(instance).exists():
        _code, msg = stop(keep_down=True, instance=instance)
        notes.append(f"in-container adapter: {msg}")
    if companion_off_file(instance).exists():
        pid = companion_pid(instance)
        return 0, "; ".join([*notes, f"hub mode: companion switch off ({instance}.companion-off)"
                             + (f", pid {pid} still running: tg-adapter stop" if pid else "")])  # fmt: skip
    h = history(instance, _clog(instance))
    tree = tree_id()
    pid = companion_pid(instance)
    if pid:
        ls = last_start(h)
        if not (tree and ls and ls[1] not in ("-", tree)):
            return 0, "; ".join([*notes, f"hub mode: companion running (pid {pid})"])
        _kill(pid, 10.0, lambda: companion_pid(instance))
        notes.append(f"companion restarted for tree {tree}")
        h = history(instance, _clog(instance))
    if not force:
        starts = [ts for ts, kind, _ in h if kind == "start" and clock.now() - ts < LOOP_S]
        if len(starts) >= LOOP_N:
            first = starts[-LOOP_N]
            return 4, "; ".join([*notes, f"hub mode: companion crash loop ({len(starts)} starts in 15 min); next try"
                                 f" after {time.strftime('%H:%M', time.localtime(first + LOOP_S))}"])  # fmt: skip
    err = _install()
    if err:
        return 1, "; ".join([*notes, err])
    pid, msg = _start_companion(tree, instance)
    return (0 if pid or "starting" in msg else 1), "; ".join([*notes, f"hub mode: {msg}"])


def ensure(force: bool = False, instance: str = "test") -> tuple[int, str]:
    """(code, message): 0 = fine (not configured, switch off, running, started), 3 = held down (exit that won't heal
    by itself), 4 = crash-loop backoff, 1 = could not start. Hub mode: _ensure_hub."""
    if hub_mode(instance):
        return _ensure_hub(force, instance)
    cfg = read_config(instance)
    if cfg is None:
        name = "test.json or config.json" if instance == "test" else f"{instance}.json"
        return 0, f"not configured (no ~/.config/tg-adapter/{name}): nothing runs"
    if switch_on(instance):
        pid = running_pid(instance) or legacy_pid(instance)
        return 0, f"switch off (~/.config/tg-adapter/{instance}.off): staying down" + (
            f"; pid {pid} stops itself within one poll round" if pid else ""
        )
    old = legacy_pid(instance)
    if old:
        stop(instance=instance)  # the state-root adapter: replaced by this instance's
    h = history(instance)
    tree = tree_id()
    pid = running_pid(instance)
    if pid:
        ls = last_start(h)
        if tree and ls and ls[1] not in ("-", tree):
            stop(instance=instance)
            _note(f"restart tree {ls[1]} -> {tree}", instance)
            h = history(instance)
        else:
            return 0, f"running (pid {pid})"
    ex = last_exit(h)
    now = clock.now()
    if not force:
        ls = last_start(h)
        # the config/token in use by that run were the ones present at its start (date +%s is whole seconds)
        if ex and ex[1] in PERMANENT and not _changed_since(ls[0] if ls else ex[0], cfg, instance):
            why = EXIT_MEANING[ex[1]]
            tag = "" if instance == "test" else f"{instance}-"
            _report(
                "tg-adapter.down",
                f"tg-adapter-{tag}down-{int(ex[0])}.md",
                f"tg-adapter ({instance}) is down: {why}",
                f"fix it (tg-adapter status --instance {instance}),"
                f" then: tg-adapter ensure --force --instance {instance}",
                {"exit": ex[1], "since": ex[0], "instance": instance},
                instance,
            )
            return 3, f"held down since {time.strftime('%d.%m %H:%M', time.localtime(ex[0]))}: {why}"
        starts = [ts for ts, kind, _ in h if kind == "start" and now - ts < LOOP_S]
        if len(starts) >= LOOP_N and not (ex and ex[1] in (0, 7)):
            first = starts[-LOOP_N]
            tag = "" if instance == "test" else f"{instance}-"
            _report(
                "tg-adapter.crashloop",
                f"tg-adapter-{tag}crashloop-{int(first)}.md",
                f"tg-adapter ({instance}) crashed {len(starts)} times in 15 min",
                f"check ~/.local/state/tg-adapter/{instance}/adapter.log; ensure retries after 15 min",
                {"starts": len(starts), "last_exit": ex[1] if ex else None, "instance": instance},
                instance,
            )
            return (
                4,
                f"crash loop: {len(starts)} starts in 15 min;"
                f" next try after {time.strftime('%H:%M', time.localtime(first + LOOP_S))}",
            )
    err = _install()
    if err:
        _note("install-failed", instance)
        return 1, err
    try:
        chk = _run_cli(["check", "--instance", instance], timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        return 1, f"check could not run: {type(e).__name__}"
    if chk.returncode != 0:
        _note("start tree=check", instance)
        _note(f"exit {chk.returncode if chk.returncode in EXIT_MEANING else 2}", instance)
        first = (chk.stderr.strip().splitlines() or ["?"])[0][:200]
        return 3, f"check refused the config: {first}"
    pid, msg = _start(tree, instance)
    return (0 if pid or msg.startswith("starting") else 1), msg


def status(instance: str = "test") -> dict[str, Any]:
    cfg = read_config(instance)
    tok = token_state(cfg, instance)
    h = history(instance)
    pid = running_pid(instance) or legacy_pid(instance)
    ex = last_exit(h)
    ls = last_start(h)
    tree = tree_id()
    info: dict[str, Any] = {
        "instance": instance,
        "configured": cfg is not None,
        "config_valid_json": cfg != {} if cfg is not None else None,
        "token_present": tok["present"],
        "token_mode_ok": tok["mode_ok"],
        "token_from": tok["from"],
        "switch_off": switch_on(instance),
        "running": pid is not None,
        "pid": pid,
        "started": ls[0] if ls and pid else None,
        "tree_current": (ls[1] == tree) if ls and pid and tree else None,
        "last_exit": {"code": ex[1], "ts": ex[0], "meaning": EXIT_MEANING.get(ex[1], "error")} if ex else None,
        "adapter": None,
    }
    if hub_mode(instance):
        info["hub"] = _hub_status(instance)
    elif cfg is not None and (adapter_dir() / "src" / "cli.ts").is_file() and _install() is None:
        try:
            r = _run_cli(["status", "--json", "--instance", instance], timeout=20)
            info["adapter"] = json.loads(r.stdout) if r.returncode == 0 else {"error": f"status exit {r.returncode}"}
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            info["adapter"] = {"error": type(e).__name__}
    try:
        info["cron"] = "tg-adapter ensure" in (paths.home() / "crontab").read_text(encoding="utf-8")
    except OSError:
        info["cron"] = False
    return info


def _hub_status(instance: str) -> dict[str, Any]:
    """Hub mode: the companion, the hub's outbox consumer for this bot and the adapter's counters (no text)."""
    from core import hub

    out: dict[str, Any] = {"companion_pid": companion_pid(instance),
                           "companion_off": companion_off_file(instance).exists()}  # fmt: skip
    try:
        r = _run_cli(["companion", "status", "--json", "--instance", instance], timeout=20)
        out["companion"] = json.loads(r.stdout) if r.returncode == 0 else {"error": f"status exit {r.returncode}"}
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        out["companion"] = {"error": type(e).__name__}
    try:
        cons = hub.call("GET", "/outbox/consumers", timeout=3.0).get("consumers") or []
        out["consumers"] = [{k: c.get(k) for k in ("bot", "caller", "age")} for c in cons]
        out["meta"] = {**hub.tg_meta("offset:", timeout=3.0), **hub.tg_meta("dropped:", timeout=3.0),
                       **hub.tg_meta("tg.", timeout=3.0)}  # fmt: skip
    except hub.HubError as e:
        out["hub_error"] = str(e)[:120]
    return out


def render_status(s: dict[str, Any]) -> str:
    yn = {True: "yes", False: "no", None: "n/a"}
    head = f"tg-adapter ({s.get('instance', 'test')}): configured {yn[s['configured']]}"
    if s["configured"] and s["config_valid_json"] is False:
        head += " (config.json is not a JSON object)"
    tok = f"token present {yn[s['token_present']]}"
    if s["token_present"] and s["token_mode_ok"] is False:
        tok += " (group/other can read it: chmod 600)"
    if s["token_from"] == "default":
        tok += " (default location)"
    lines = [f"{head} · {tok} · switch off {yn[s['switch_off']]}"]
    if s["running"]:
        cur = {True: "", False: " (adapters/tg changed since: ensure restarts it)", None: ""}[s["tree_current"]]
        lines.append(f"adapter: running, pid {s['pid']}{cur}")
    elif s["last_exit"]:
        le = s["last_exit"]
        when = time.strftime("%d.%m %H:%M", time.localtime(le["ts"]))
        lines.append(f"adapter: not running (last exit {le['code']} = {le['meaning']}, {when})")
    else:
        lines.append("adapter: not running" + ("" if s["configured"] else " (nothing runs until the config exists)"))
    a = s.get("adapter")
    if isinstance(a, dict) and "error" not in a:
        lines.append(
            f"  offset {a.get('offset')} · events {json.dumps(a.get('events'))} · outbox {json.dumps(a.get('outbox'))}"
        )
        ack = a.get("callback_ack_ms") or {}
        if ack.get("n"):
            lines.append(f"  callback ack ms p50 {ack['p50']} · p95 {ack['p95']} · max {ack['max']} (n {ack['n']})")
    elif isinstance(a, dict):
        lines.append(f"  adapter status: {a['error']}")
    hs = s.get("hub")
    if isinstance(hs, dict):
        cp = hs.get("companion_pid")
        lines.append(
            "hub mode (live.hub): the adapter runs in the hub · companion "
            + (f"running, pid {cp}" if cp else "not running")
            + (" (switch off)" if hs.get("companion_off") else "")
        )
        if hs.get("hub_error"):
            lines.append(f"  hub: {hs['hub_error']}")
        else:
            cons = ", ".join(f"{c.get('bot')} {c.get('age')} s ago" for c in hs.get("consumers") or []) or "none"
            lines.append(f"  hub outbox consumers: {cons}")
            meta = hs.get("meta") or {}
            if meta:
                lines.append("  adapter meta: " + " · ".join(f"{k} {v}" for k, v in sorted(meta.items())[:12]))
    lines.append(f"cron ensure: {'present' if s['cron'] else 'absent'}")
    return "\n".join(lines)
