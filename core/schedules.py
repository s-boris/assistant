"""core.schedules: the schedule registry. Every periodic job as data. Stdlib only (tomllib).

The data is core/schedules.toml (one [jobs.<name>] table each; its header documents the fields). It started as the
census of ~/crontab (135 lines, 138 jobs) and is what the hub's schedules, `hub wf cron`, `assistant cron tag|check`,
`keepers` and the brain's workflow tools read. A crontab line carries its registry name as a trailing tag
`  # assistant:<line>` (several jobs may share one line); `cron_line` keeps the exact line for rollback.

  load(path=None) -> Registry      parsed and checked; $ASSISTANT_SCHEDULES names another file (tests)
  Registry.jobs {name: Entry} · .lines {tag: [Entry]} · .tz · .keepers() · .entry(name)
                                   .tz: the file's top-level tz when set, else the owner's zone (core.todo.owner_tz:
                                   $ASSISTANT_TZ, the profile's locale.tz, else UTC); no zone name in the repo
  QUEUES                           {queue: (DBOS queue name, concurrency)}: critical 2 · fast 4 · slow 2
  active_lines(text)               [(line number, raw line, body without tag, tag or None)] (no comments/env/blank)
  parity(text, reg) -> Parity      untracked lines, registry lines missing from the crontab, changed lines
  tag_text(text, reg)              (new text, [(line number, tag)]): tags the untagged lines whose exact text is a
                                   registry cron_line; nothing else changes (idempotent)
  comment_only(old, new)           [] when every changed line is the old line plus a trailing comment the shell
                                   ignores (no unescaped %, the line ends at shell top level); else the problems
  end_state_ok(cmd)                True when sh would read "  # x" appended to cmd as a comment
  home_form(line, home=None)       the line with its literal home path spelled $HOME (parity and tag_text compare
                                   in that form: the registry is home-neutral, older crontabs name the home)
  live_form(line, home=None)       the line as `assistant cron add|install` writes it to ~/crontab: each $HOME
                                   outside double quotes spelled as the literal home (home_form's inverse: parity
                                   still holds); "$HOME/..." state paths stay
  render_cron(e, home="$HOME")     the ~/crontab line of a registry job (cron-back shape)
  CORE_FEATURES, FEATURE_SWITCHES  what `assistant cron install` schedules: core always, the profile's switches mapped
  switch_features(names, known)    to registry features ((features, unknown names)); `all` = every switch
  Entry.owner_only                 this deployment's own job: `cron check` keeps checking it, `cron install` never
                                   schedules it (its skill is not shipped in templates/)
  install_tags(reg, want)          (tags `cron install` schedules for registry features want, owner-only tags left out)
  cadence(cron)                    how often a 5-field cron fires at most: daily (one tick a day or fewer), hourly
                                   (one minute value: at most once an hour), minute; "" when unreadable
  Entry.catches_up                 a hub schedule runs a tick it missed while the hub was down, once and late (within
                                   min(cadence/2, 6 h)): `catch_up` when set, else on exactly for daily or slower
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
CRON_FIELD = re.compile(r"^[0-9A-Za-z*/,\-]+$")
ENV_LINE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\s*=")
TAG_RE = re.compile(r"\s+#\s*assistant:([A-Za-z0-9_.-]+)\s*$")
CLASSES = {"W": "watch", "X": "critical", "T": "timer", "C": "cue", "V": "vault", "M": "moves", "R": "retire",
           "K": "stays in cron"}  # fmt: skip
QUEUES = {"critical": ("jobs-critical", 2), "fast": ("jobs-fast", 4), "slow": ("jobs-slow", 2)}
WHERE = ("brain", "hub", "adapter", "cron", "retire")
# keepers = run by `keepers ensure`; planned = no runner yet; window = run by the hub's event-window clocks while a
# window is open (the event workflows, `eventwf`: no cron line, no schedule of its own)
RUNS_ON = ("cron", "hub", "keepers", "planned", "retired", "window")
HUB_JOB_LIMIT = 600  # a hub release without per-job limits gives up on any job after this (workflow.job)
HUB_TIMEOUT_MAX = 3600  # the longest job a hub schedule waits for (hubwf.JOB_TIMEOUT_MAX)
CATCH_UP_CADENCES = ("hourly", "daily")  # catch_up = true needs one of these; minute-level schedules never catch up
DEFAULT = Path(__file__).with_name("schedules.toml")


class RegistryError(ValueError):
    pass


@dataclass(frozen=True)
class Entry:
    name: str
    line: str
    cron: str
    cls: str
    group: str
    queue: str
    where: str
    runs_on: str
    argv: tuple[str, ...]
    timeout: int
    after: int
    feature: str
    owner_facing: bool
    why: str
    target: str
    cron_line: str
    shell: bool = False
    out: str | None = None
    err: str | None = None
    dirs: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()
    ok: tuple[int, ...] = ()
    keeper: bool = False
    every: int = 0
    catch_up: bool | None = None  # None = the default: on exactly for daily-or-slower crons (see catches_up)
    owner_only: bool = False  # never scheduled by `assistant cron install` (a fresh install); parity still checks it

    @property
    def queue_name(self) -> str | None:
        return QUEUES[self.queue][0] if self.queue else None

    @property
    def catches_up(self) -> bool:
        """After a hub restart its missed tick runs once, late (hubwf: within min(cadence/2, 6 h))."""
        c = cadence(self.cron)
        if self.catch_up is not None:
            return self.catch_up and c in CATCH_UP_CADENCES
        return c == "daily"

    def shown(self) -> str:
        if self.shell:
            return f"(shell line: {self.cron_line[:80]})"
        out = " ".join(self.argv)
        out += f" >> ~/.local/state/{self.out}" if self.out else " > /dev/null"
        if self.err == "out":
            out += " 2>&1"
        elif self.err:
            out += f" 2>> ~/.local/state/{self.err}"
        return out

    def movable(self) -> str | None:
        """None when a hub schedule could run it; else why not (yet)."""
        if self.cls in ("K", "R"):
            return f"class {self.cls}: {CLASSES[self.cls]}"
        if self.shell or not self.argv:
            return "a shell line: needs a managed ~/bin wrapper first"
        return None


@dataclass
class Registry:
    tz: str
    jobs: dict[str, Entry]
    path: Path
    lines: dict[str, list[Entry]] = field(default_factory=dict)

    def entry(self, name: str) -> Entry | None:
        return self.jobs.get(name)

    def keepers(self) -> list[Entry]:
        return [e for e in self.jobs.values() if e.keeper]


@dataclass
class Parity:
    active: int = 0
    tagged: int = 0
    untracked: list[tuple[int, str]] = field(default_factory=list)  # (line number, raw) without a registry tag
    missing: list[str] = field(default_factory=list)  # registry lines (runs_on cron) not in the crontab
    changed: list[tuple[int, str, str]] = field(default_factory=list)  # (n, tag, why): line text != cron_line
    by_tag: dict[str, int] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return not (self.untracked or self.missing or self.changed)


def _path(path: str | os.PathLike[str] | None) -> Path:
    return Path(path or os.environ.get("ASSISTANT_SCHEDULES") or DEFAULT)


def _tup(v: object, what: str, name: str) -> tuple:
    if v is None:
        return ()
    if not isinstance(v, list):
        raise RegistryError(f"{name}: {what} must be a list")
    return tuple(v)


def cron_ok(cron: str) -> bool:
    parts = cron.split()
    return len(parts) == 5 and all(CRON_FIELD.match(p) for p in parts)


def _values(f: str, lo: int, hi: int) -> set[int] | None:
    """A numeric cron field's values (*, n, a-b, lists, /steps); None when it can't be read."""
    out: set[int] = set()
    for part in f.split(","):
        rng, slash, step = part.partition("/")
        try:
            n = int(step) if slash else 1
            if rng == "*":
                a, b = lo, hi
            elif "-" in rng:
                a, b = (int(x) for x in rng.split("-", 1))
            else:
                a = int(rng)
                b = hi if slash else a
        except ValueError:
            return None
        if n < 1 or not lo <= a <= b <= hi:
            return None
        out.update(range(a, b + 1, n))
    return out


def cadence(cron: str) -> str:
    """How often a 5-field cron fires at most: "daily" (one minute and one hour value: a tick a day or fewer),
    "hourly" (one minute value), "minute" (several minute values); "" when it can't be read."""
    parts = cron.split()
    if len(parts) != 5:
        return ""
    m, h = _values(parts[0], 0, 59), _values(parts[1], 0, 23)
    if not m or not h:
        return ""
    if len(m) > 1:
        return "minute"
    return "daily" if len(h) == 1 else "hourly"


def _owner_zone() -> str:
    """The zone the cron fields are written in when the file names none: the owner's (the profile's locale.tz)."""
    from core import todo  # lazy: only for a file without its own tz

    return str(todo.owner_tz())


def load(path: str | os.PathLike[str] | None = None) -> Registry:
    p = _path(path)
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise RegistryError(f"{p}: {e}") from None
    if data.get("schema") != 1:
        raise RegistryError(f"{p}: schema 1 expected")
    tz = data.get("tz") or _owner_zone()
    jobs: dict[str, Entry] = {}
    for name, d in (data.get("jobs") or {}).items():
        jobs[name] = _entry(name, d)
    reg = Registry(tz=tz, jobs=jobs, path=p)
    for e in jobs.values():
        reg.lines.setdefault(e.line, []).append(e)
    for tag, es in reg.lines.items():
        if len({(e.cron, e.cron_line, e.runs_on) for e in es}) != 1:
            raise RegistryError(f"line {tag}: its jobs disagree on cron, cron_line or runs_on")
    return reg


def _entry(name: str, d: dict) -> Entry:
    if not NAME_RE.match(name):
        raise RegistryError(f"{name}: names are [a-z0-9-], 1-40")
    try:
        e = Entry(
            name=name,
            line=d.get("line") or name,
            cron=d["cron"],
            cls=d["class"],
            group=d.get("group", ""),
            queue=d.get("queue", ""),
            where=d.get("where", "brain"),
            runs_on=d.get("runs_on", "cron"),
            argv=_tup(d.get("argv"), "argv", name),
            timeout=int(d.get("timeout", 300)),
            after=int(d.get("after", 0)),
            feature=d.get("feature", ""),
            owner_facing=bool(d.get("owner_facing", False)),
            why=d.get("why", ""),
            target=d.get("target", ""),
            cron_line=d["cron_line"],
            shell=bool(d.get("shell", False)),
            out=d.get("out"),
            err=d.get("err"),
            dirs=_tup(d.get("dirs"), "dirs", name),
            env=tuple(sorted((str(k), str(v)) for k, v in (d.get("env") or {}).items())),
            ok=tuple(int(x) for x in _tup(d.get("ok"), "ok", name)),
            keeper=bool(d.get("keeper", False)),
            every=int(d.get("every", 0)),
            catch_up=d.get("catch_up"),
            owner_only=d.get("owner_only", False),
        )
    except KeyError as k:
        raise RegistryError(f"{name}: {k.args[0]} missing") from None
    except (TypeError, ValueError) as x:
        raise RegistryError(f"{name}: {x}") from None
    bad = []
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", e.line):
        bad.append("line: 1-40 of [A-Za-z0-9_.-]")
    if not cron_ok(e.cron):
        bad.append(f"cron {e.cron!r}: 5 fields")
    if e.cls not in CLASSES:
        bad.append(f"class {e.cls!r}")
    if e.queue and e.queue not in QUEUES:
        bad.append(f"queue {e.queue!r}")
    if bool(e.queue) == (e.cls in ("K", "R")):
        bad.append("queue: none exactly for K and R")
    if e.where not in WHERE or e.runs_on not in RUNS_ON:
        bad.append("where / runs_on")
    if e.shell == bool(e.argv):
        bad.append("argv: empty exactly when shell")
    if not all(isinstance(a, str) and a for a in e.argv) or "\n" in e.cron_line:
        bad.append("argv strings / one-line cron_line")
    if not 1 <= e.timeout <= 7200 or e.after < 0 or (e.keeper and e.every not in (1, 2, 3, 5, 10, 15, 30, 60)):
        bad.append("timeout 1-7200 · after >= 0 · keeper every in 1,2,3,5,10,15,30,60")
    if not isinstance(e.owner_only, bool):
        bad.append("owner_only: true or false")
    if e.catch_up is not None and not isinstance(e.catch_up, bool):
        bad.append("catch_up: true or false")
    elif e.catch_up and cadence(e.cron) not in CATCH_UP_CADENCES:
        bad.append("catch_up: minute-level schedules never catch up (one minute value at most)")
    if e.runs_on in ("hub", "window") and e.timeout > HUB_TIMEOUT_MAX:
        bad.append(f"timeout: at most {HUB_TIMEOUT_MAX} s on the hub")
    if TAG_RE.search(e.cron_line) or not e.cron_line.startswith(e.cron + " "):
        bad.append("cron_line: the untagged line, starting with cron")
    for k, v in (("out", e.out), ("err", e.err)):
        if v is not None and (v.startswith(("/", "~")) or ".." in v.split("/")) and not (k == "err" and v == "out"):
            bad.append(f"{k}: a path under ~/.local/state")
    if bad:
        raise RegistryError(f"{name}: " + "; ".join(bad))
    return e


def active_lines(text: str) -> list[tuple[int, str, str, str | None]]:
    out = []
    for i, raw in enumerate(text.splitlines(), 1):
        s = raw.strip()
        if not s or s.startswith("#") or ENV_LINE.match(s):
            continue
        m = TAG_RE.search(raw)
        out.append((i, raw, raw[: m.start()] if m else raw, m.group(1) if m else None))
    return out


def parity(text: str, reg: Registry) -> Parity:
    p = Parity()
    seen: set[str] = set()
    for n, raw, body, tag in active_lines(text):
        p.active += 1
        if tag is None or tag not in reg.lines:
            p.untracked.append((n, raw))
            continue
        p.tagged += 1
        p.by_tag[tag] = n
        if tag in seen:
            p.changed.append((n, tag, "tag used twice"))
        seen.add(tag)
        e = reg.lines[tag][0]
        if home_form(body).rstrip() != e.cron_line.rstrip():
            why = "schedule differs" if body.split()[:5] != e.cron.split() else "command differs"
            p.changed.append((n, tag, why))
        elif e.runs_on != "cron":
            p.changed.append((n, tag, f"registry says runs_on {e.runs_on}: two runners"))
    for tag, es in reg.lines.items():
        if es[0].runs_on == "cron" and tag not in seen:
            p.missing.append(tag)
    return p


def tag_text(text: str, reg: Registry) -> tuple[str, list[tuple[int, str]]]:
    by_line: dict[str, str] = {}
    for tag, es in reg.lines.items():
        if es[0].runs_on == "cron":
            by_line.setdefault(es[0].cron_line.rstrip(), tag)
    used = {t for _, _, _, t in active_lines(text) if t}
    lines = text.split("\n")
    done: list[tuple[int, str]] = []
    for n, raw, _body, tag in active_lines(text):
        if tag is not None:
            continue
        t = by_line.get(home_form(raw).rstrip())
        if t is None or t in used or not end_state_ok(raw.split(None, 5)[5] if len(raw.split(None, 5)) == 6 else ""):
            continue
        lines[n - 1] = raw.rstrip() + f"  # assistant:{t}"
        used.add(t)
        done.append((n, t))
    return "\n".join(lines), done


def end_state_ok(cmd: str) -> bool:
    """POSIX sh lexing of one cron command: True when it ends outside quotes, $( ), backticks and escapes, so
    appended "  # ..." is a comment. Unescaped % (cron's newline) or a here-doc make it False."""
    if not cmd.strip() or "<<" in cmd or re.search(r"(?<!\\)%", cmd):
        return False
    stack: list[str] = []  # "'", '"', "`", "("
    i, n = 0, len(cmd)
    while i < n:
        c = cmd[i]
        top = stack[-1] if stack else ""
        if top == "'":
            if c == "'":
                stack.pop()
        elif c == "\\":
            if i + 1 >= n:
                return False
            i += 1
        elif top == '"':
            if c == '"':
                stack.pop()
            elif c == "`":
                stack.append("`")
            elif cmd.startswith("$(", i):
                stack.append("(")
                i += 1
        elif top == "`":
            if c == "`":
                stack.pop()
            elif c in "'\"":
                stack.append(c)
        else:  # top level or inside $( )
            if c in "'\"`":
                stack.append(c)
            elif cmd.startswith("$(", i):
                stack.append("(")
                i += 1
            elif c == "(":
                stack.append("(")
            elif c == ")":
                if top != "(":
                    return False
                stack.pop()
            elif c == "#" and (i == 0 or cmd[i - 1] in " \t;&|(") and not stack:
                return True  # the line already ends in a comment: anything appended is part of it
        i += 1
    return not stack


# The features a fresh install schedules without asking (`assistant cron install`): they need only the core tools and
# no account, server or hub: housekeeping, the events ledger, page watches (the watch skill) and the weekly cues of
# the shipped agents (idea-scan, self-cleanup, weekly-digest). Every other feature is opt-in (--features, or the
# profile's `features`). Jobs marked owner_only (core/schedules.toml) are never scheduled, whatever their feature.
CORE_FEATURES = ("housekeeping", "events", "watch", "goals", "self-cleanup", "weekly-digest")

# The profile's coarse feature switches (`features`: {switch: bool}; QUICKSTART, docs/profile.example.json) -> the
# registry's job features they schedule. One place; tests/ops/test_cron.py checks every name here is a registry
# feature, and that every cron job a shipped skill or agent relies on (templates/manifest.toml [cron]) is scheduled
# when the switches that install it are on. A switch without periodic jobs maps to (). Registry feature names are
# accepted as they are, too.
FEATURE_SWITCHES: dict[str, tuple[str, ...]] = {
    "telegram": ("telegram",),
    "hub": ("hub", "reminders", "web-agent", "status-board"),  # the board's store and the pinned status live on the hub
    "vault": ("vault", "vault-llm"),
    "home_assistant": ("home", "home-autopilot", "shopping"),  # shopping: `shop watch` (the shopping skill)
    "media": ("media",),
    "unraid": ("server-health", "capacity", "security", "network"),
    "phone": ("phone",),
    "voice": ("voice",),
    "browser": ("browser",),
    "calendar": ("calendar", "trips"),
    "swiss": ("deals",),  # `deals hdd --cron` (the deals skill); the deadlines and bills jobs are owner_only
}


def switch_features(names: set[str] | list[str], known: set[str]) -> tuple[set[str], set[str]]:
    """(registry features, names that are neither a switch nor a registry feature) for switch or feature names.
    "all" = every switch (the features the switches map to; never a feature no switch names)."""
    out: set[str] = set()
    unknown: set[str] = set()
    for n in names:
        if n == "all":
            out.update(f for fs in FEATURE_SWITCHES.values() for f in fs)
        elif n in FEATURE_SWITCHES:
            out.update(FEATURE_SWITCHES[n])
        elif n in known:
            out.add(n)
        else:
            unknown.add(n)
    return out, unknown


def install_tags(reg: Registry, want: set[str]) -> tuple[list[str], list[str]]:
    """What `assistant cron install` may schedule for the registry features `want`: (crontab tags of the cron lines
    with a wanted job that is not owner_only, tags of the lines left out only because their wanted jobs are
    owner_only), each in registry order."""
    ok: list[str] = []
    owner: list[str] = []
    for tag, es in reg.lines.items():
        if es[0].runs_on != "cron":
            continue
        hit = [e for e in es if e.feature in want]
        if any(not e.owner_only for e in hit):
            ok.append(tag)
        elif hit:
            owner.append(tag)
    return ok, owner


CRON_BACK = "cron-back 29.10"  # the target of the jobs that went back from hub schedules to ~/crontab
RUN_LOCK = ".local/state/cron-fail/{name}.run"  # flock -n: a tick that finds the last run still going skips


def home_form(line: str, home: str | None = None) -> str:
    """A crontab line with its literal home path spelled $HOME, the way the registry writes it: lines installed before
    the registry went home-neutral name the home literally (cron runs them under sh with HOME set, so both forms do
    the same). home: default $HOME of this process."""
    h = (os.environ.get("HOME", "") if home is None else home).rstrip("/")
    if not h.startswith("/") or h == "/":
        return line
    return re.sub(re.escape(h) + r"(?![A-Za-z0-9_.-])", "$HOME", line)


def live_form(line: str, home: str | None = None) -> str:
    """A registry line the way every ~/crontab write spells it (cron add and cron install alike): each $HOME not
    right after a double quote becomes the literal home ($HOME/bin/x -> /home/u/bin/x, also inside $( ) and
    single-quoted sh -c text); quoted "$HOME/..." paths stay (JOBS.md: never a literal home path in redirects).
    home_form(live_form(x)) == x. home: default $HOME of this process; one that is not a plain absolute path (a
    space, a quote) leaves the line as it is, since cron's sh would split it."""
    h = (os.environ.get("HOME", "") if home is None else home).rstrip("/")
    if not h.startswith("/") or not re.fullmatch(r"[A-Za-z0-9._/+-]+", h):
        return line
    return re.sub(r'(?<!")\$HOME(?![A-Za-z0-9_])', lambda _m: h, line)


def render_cron(e: Entry, home: str = "$HOME") -> str:
    """The ~/crontab line that runs a registry job as its hub schedule did (cron-back, 29.10): its state folders
    first, then cron-fail (--after = the entry's failure streak; 0 = record the run, no notes), flock -n on its own
    run lock (no overlapping runs), `timeout -k 10 T` (the hub's limit) unless the argv already starts with timeout,
    the argv (~/ = home, env pairs via `env`), stdout to its log or /dev/null, cron-fail's own stderr with it (err =
    "out" or none) or appended to its err file. State paths are "$HOME/..." (JOBS.md: never a literal home path in
    redirects; the lint runs each line under a temp HOME). Output is one line; % is escaped for cron."""
    import shlex

    if e.shell or not e.argv:
        raise ValueError(f"{e.name}: a shell line has no argv to render")

    def hp(p: str) -> str:
        rel = p[2:] if p.startswith("~/") else f".local/state/{p}"
        if not re.fullmatch(r"[A-Za-z0-9._/+-]+", rel):
            raise ValueError(f"{e.name}: unsafe path {p!r}")
        return f'"$HOME/{rel}"'

    dirs = [hp(d) for d in e.dirs]
    for f in (e.out, e.err if e.err not in (None, "out") else None):
        if f:  # the shell opens the redirect before cron-fail runs: its folder must exist (a fresh HOME too)
            d = hp(f.rsplit("/", 1)[0]) if "/" in f else '"$HOME/.local/state"'
            if d not in dirs:
                dirs.append(d)

    def arg(a: str) -> str:  # ~/x = "$HOME/x" unquoted when x is a plain path (cron's sh expands it), else quoted
        if a.startswith("~/") and home == "$HOME" and re.fullmatch(r"[A-Za-z0-9._/+-]+", a[2:]):
            return f"$HOME/{a[2:]}"
        return shlex.quote(f"{home}/{a[2:]}" if a.startswith("~/") else a)

    run = ["cron-fail", "--after", str(e.after)]
    if e.ok:
        run += ["--ok", ",".join(str(c) for c in e.ok)]
    run += [e.name, "flock", "-n", "-E", "0"]
    tail = []
    if e.argv[0] != "timeout":
        tail += ["timeout", "-k", "10", str(e.timeout)]
    if e.env:
        tail += ["env"] + [f"{k}={v}" for k, v in e.env]
    cmd = " ".join(shlex.quote(a) for a in run) + " " + hp(RUN_LOCK.format(name=e.name)[len(".local/state/") :])
    cmd += " " + " ".join([shlex.quote(a) for a in tail] + [arg(a) for a in e.argv])
    cmd += f" >> {hp(e.out)}" if e.out else " >/dev/null"
    cmd += f" 2>> {hp(e.err)}" if e.err not in (None, "out") else " 2>&1"
    if dirs:
        cmd = "mkdir -p " + " ".join(dirs) + " && " + cmd
    return f"{e.cron} {cmd}".replace("%", "\\%")


def comment_only(old: str, new: str) -> list[str]:
    """Proof for a tag edit: same lines, and each changed line = old + whitespace + "# assistant:<tag>", where the
    old line's command ends at shell top level (end_state_ok). Returns the problems ([] = comment-only)."""
    a, b = old.split("\n"), new.split("\n")
    if len(a) != len(b):
        return [f"line count {len(a)} -> {len(b)}"]
    bad = []
    for i, (x, y) in enumerate(zip(a, b, strict=True), 1):
        if x == y:
            continue
        m = TAG_RE.search(y)
        if not m or y[: m.start()] != x.rstrip() or TAG_RE.search(x):
            bad.append(f"line {i}: not a pure tag suffix")
            continue
        parts = x.split(None, 5)
        if x.lstrip().startswith("#") or len(parts) < 6 or not end_state_ok(parts[5]):
            bad.append(f"line {i}: the shell would not read the suffix as a comment")
    return bad
