"""Derive hooks/guards/risk_check_h11.py from the verbatim baseline hooks/guards/risk_check.py.
Every replacement must match exactly once; anything else aborts (no partial file)."""

import sys
from pathlib import Path

src_path, dst_path = Path(sys.argv[1]), Path(sys.argv[2])
s = src_path.read_text(encoding="utf-8")


def sub(old, new, count=1):
    global s
    n = s.count(old)
    if n != count:
        sys.exit(f"patch: expected {count} match(es), found {n}: {old[:70]!r}")
    s = s.replace(old, new)


# 1. the H11 note, before the first constant
sub(
    "HOME = '/home/node'\n",
    """\
# --- H11 (v2 Phase 2, "guards -> backstop"): THE REDUCED RULE SET -------------------------------------------------
# This file is risk_check.py minus the rules the owner approved to retire (the list, its evidence and the keep
# classes: ~/reports/plans/v2-h11-guards.md; machine-readable: h11_rules.toml next to the repo copy). It runs only
# while the switch file next to the wrapper (risk-check.h11) holds the line "reduced-v1"; absent, unreadable or any
# other content runs the full risk_check.py. Rollback: delete the switch file (instant, no restart).
# Differences from risk_check.py, nothing else:
#   - add() drops a finding whose rule is retired in the machine the command runs on (H11_RETIRED; analyze() keeps
#     the stack of contexts, a docker container counts as the machine it runs on);
#   - retirements on the server hold only while the SSH lockdown is on (lockdown_on(): the server answers with the
#     dispatcher, so no shell, docker or disk command can run there); otherwise the server rules fire as before;
#   - the Unraid-only rules array, server-config and mover are gone (the mdcmd, emcmd and mover handlers);
#   - log lines carry prof=h11.
# Keep classes (never retired here): vault flags, Telegram state, public repos, crontab, force-push, secrets,
# subagent and owner-OK integrity; see KEEP_CLASS_RULES.
HOME = '/home/node'
""",
)

# 2. the filtering add() + the lockdown check
sub(
    """def add(found, level, rule, why, amb=False):
    found.append((level, rule, why, amb))
""",
    '''ALL_KINDS = frozenset({'local', 'server', 'host', 'remote'})
H11_RETIRED = {
    # rule id: the machines where it no longer fires
    'rm-home': ALL_KINDS,                  # log-only (never blocks); deletes inside the container are his OK'd space
    'rm-unresolved': ALL_KINDS,            # duplicate: Claude Code's own rm check + no-approval + the rm-hang rule
    # no block device, no docker CLI here; the server runs only dispatcher verbs (lockdown_on)
    'docker-change': {'local', 'server'}, 'docker-remove': {'local', 'server'}, 'docker-volume': {'local', 'server'},
    'format': {'local', 'server'}, 'partition': {'local', 'server'}, 'disk-erase': {'local', 'server'},
    'disk-change': {'local', 'server'}, 'fs-repair': {'local', 'server'}, 'device-write': {'local', 'server'},
    'btrfs': {'local', 'server'}, 'zfs-*': {'local', 'server'}, 'zpool-*': {'local', 'server'},
    # shell-level rules on the dispatcher-locked server (they stay for his other boxes)
    'server-delete': {'server'}, 'server-write': {'server'}, 'server-perms': {'server'},
    'server-script': {'server'}, 'server-kill': {'server'}, 'server-mount': {'server'},
    'server-service': {'server'}, 'server-power': {'server'}, 'flash-config': {'server'},
}
KEEP_CLASS_RULES = {
    'vault flags': {'vault-admin', 'mail-send'},
    'Telegram state': {'secret-send', 'rm-protected', 'move-core'},
    'public repos': {'gh-public', 'gh-write', 'gh-delete', 'gh-api-delete', 'git-delete-branch'},
    'crontab': {'crontab-wipe', 'crontab-replace'},
    'force-push': {'git-force-main', 'git-force'},
    'secrets': {'secret-print', 'secret-send', 'secrets-copy', 'secrets-move', 'secrets-write', 'secrets-overwrite',
                'secrets-delete'},
    'subagent / owner OK': {'risk-ok-subagent', 'owner-ok-subagent'},
}
LOCKDOWN_MODE = os.environ.get('RISK_CHECK_H11_MODE_FILE') or os.path.join(HOME, '.config', 'unraid-lockdown', 'mode')
LOCKDOWN_PROBE = os.environ.get('RISK_CHECK_H11_PROBE') or os.path.join(HOME, '.local', 'state', 'unraid-lockdown',
                                                                         'probe')
_KINDS = []                                            # the analysis contexts, outermost first (analyze() pushes)
_LOCKDOWN = []                                         # one reading per process


def lockdown_on():
    """True only while the server key is forced into the dispatcher: the configured mode is verbs or auto (or unset)
    and unraid-ssh's probe cache says verbs. A missing or unreadable file, root, emulate or anything else: False, so
    the server rules fire as in risk_check.py."""
    if not _LOCKDOWN:
        ok = False
        try:
            try:
                with open(LOCKDOWN_MODE, encoding='utf-8') as f:
                    mode = (f.read(64).split() or ['auto'])[0]
            except FileNotFoundError:
                mode = 'auto'
            if mode in ('auto', 'verbs'):
                with open(LOCKDOWN_PROBE, encoding='utf-8') as f:
                    ok = (f.read(64).split() or [''])[0] == 'verbs'
        except (OSError, ValueError):
            ok = False
        _LOCKDOWN.append(ok)
    return _LOCKDOWN[0]


def machine():
    """The machine the current command runs on: the innermost context that is not a container."""
    for k in reversed(_KINDS):
        if k != 'container':
            return k
    return 'local'


def retired(rule, kind):
    where = H11_RETIRED.get(rule)
    if where is None and '-' in rule:
        where = H11_RETIRED.get(rule.split('-', 1)[0] + '-*')
    if not where or kind not in where:
        return False
    return kind != 'server' or lockdown_on()


def add(found, level, rule, why, amb=False):
    if retired(rule, machine()):
        return None
    found.append((level, rule, why, amb))
''',
)

# 3. analyze() keeps the context stack
sub(
    """def analyze(text, ctx, found):
    if ctx.depth > 8 or not text or not text.strip():
""",
    """def analyze(text, ctx, found):
    _KINDS.append(ctx.kind)
    try:
        return _analyze(text, ctx, found)
    finally:
        _KINDS.pop()


def _analyze(text, ctx, found):
    if ctx.depth > 8 or not text or not text.strip():
""",
)

# 4. the Unraid-only rules are gone
for fn in ("r_mdcmd", "r_emcmd", "r_mover"):
    i = s.index(f"def {fn}(")
    j = s.index("\ndef ", i + 1)
    s = s[:i] + s[j + 1 :].lstrip("\n")
sub("'mdcmd': r_mdcmd, 'emcmd': r_emcmd, 'mover': r_mover, 'shutdown': r_power", "'shutdown': r_power")

# 5. log lines say which profile decided
sub(
    """    parts = [NAMES[d.level], action, f'rule={d.rule or "-"}', f'hash={d.hash}', f'who={who}']""",
    """    parts = [NAMES[d.level], action, f'rule={d.rule or "-"}', f'hash={d.hash}', f'who={who}', 'prof=h11']""",
)
sub(
    """f'rule={d.hang}', f'hash={d.hash}', f'who={who}',""",
    """f'rule={d.hang}', f'hash={d.hash}', f'who={who}', 'prof=h11',""",
)

for gone in ("r_mdcmd", "r_emcmd", "r_mover"):
    if gone in s:
        sys.exit(f"patch: {gone} still referenced")
dst_path.write_text(s, encoding="utf-8")
print("ok", len(s.splitlines()), "lines")
