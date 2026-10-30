#!/usr/local/bin/python3.13 -I
"""risk-check: PreToolUse safety net for Bash and Monitor commands (Hermes idea 2).

The main session runs in bypassPermissions mode with no classifier behind it. This hook rates each Bash command
before it runs (a Monitor call's `command` is a shell script too and is rated exactly the same way; see HOOK_TOOLS):
  HIGH    irreversible loss of the owner's data: deleting shares, disks, appdata, docker volumes, backups or flash
          config; formatting/partitioning; array commands; wiping a crontab; force-pushing main; plus printing or
          sending credentials and contacting other people (mail, posts on others' GitHub repos, public repos)
          -> blocked; the reason tells Claude to ask the owner on Telegram first
  MEDIUM  reversible server changes (container start/stop/update/rm, config edits with a backup, writes to the
          server, vault admin deploy, ...) -> allowed, logged to ~/.local/state/risk-check.log
  LOW     everything else -> allowed silently
  HANG    (whatever the level) an rm whose target Claude Code's own Bash check can't resolve ($D/*, $PF/$s, a
          relative glob after cd): it would stop for an approval nobody gives and the call would never return
          -> denied with a rewrite hint, no owner OK needed or possible; logged as HANG. See hang_check().
A regex prefilter lets almost every command through untouched (risk-check.sh's grep even skips python for most).
Candidates are parsed (quotes, pipes, $(...), heredocs, ssh / bash -c / docker exec nesting, cd and simple
variables) and rated by rules. Gray cases (one file on a share, a DELETE to a service, rm -r on an unresolved
variable) ask the local LLM (llm-local --json, 8 s, cached), which may only choose medium or high; without an
answer the rule's fallback level applies.

His values (GitHub account, the server's name and address, LAN prefix, domain, web VM) come from the private
profile, never from this file: keys owner.github and network.* (see PROFILE). If it is missing or broken, stricter
fallbacks apply, so a config failure can only add findings, never remove one.

Owner OK: the main session asks him on Telegram; after his yes it runs `risk-ok HASH "his words"`, which writes a
one-time code to ~/.local/state/risk-check/ok-HASH (30 min) and prints it. The exact command rerun as
`RISK_OK=<code> <command>` passes once, logged as OVERRIDE. Subagents can't issue codes.

FAILS OPEN: a safety net, not a gate. Any error is logged (ERROR line) and the call is allowed; risk-check.sh also
turns a crash or a hang into an allow. CLI (with arguments; ~/bin/risk-ok): HASH "ok" | check | list | log | revoke.
Tests: test_risk_check.py. Env for tests: RISK_CHECK_STATE, RISK_CHECK_LOG, RISK_CHECK_LLM(=off),
RISK_CHECK_LLM_CMD, RISK_CHECK_LLM_TIMEOUT.
"""
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import time

HOME = '/home/node'
STATE = os.environ.get('RISK_CHECK_STATE') or os.path.join(HOME, '.local', 'state', 'risk-check')
LOG = os.environ.get('RISK_CHECK_LOG') or os.path.join(HOME, '.local', 'state', 'risk-check.log')
OK_TTL = 30 * 60
# His values come from the private profile ($ASSISTANT_PROFILE, else ~/.config/assistant/profile.json): owner.github
# and the network keys below. No file, a broken one (not JSON, not a regular file, over 1 MB) or a key that is
# missing, empty or not a string switches to the stricter fallbacks: no GitHub account counts as his, every host
# may be one of his boxes (the server and service rules apply to all of them) and no host gets the owner-host
# relief for credentials. So a config failure can only add findings.
PROFILE = os.environ.get('ASSISTANT_PROFILE') or os.path.join(HOME, '.config', 'assistant', 'profile.json')
PROFILE_NET_KEYS = ('server_name', 'server_host', 'lan_prefix', 'domain', 'hosts.web_vm')


def profile_values(path):
    """{dotted key: value} for owner.github and network.<PROFILE_NET_KEYS> that are non-empty strings. Never raises."""
    try:
        if not os.path.isfile(path) or os.path.getsize(path) > 1_000_000:
            return {}
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except Exception:                                  # unreadable, not JSON: the fallbacks
        return {}
    out = {}
    for key in ('owner.github',) + tuple('network.' + k for k in PROFILE_NET_KEYS):
        cur = data
        for part in key.split('.'):
            cur = cur.get(part) if isinstance(cur, dict) else None
        if isinstance(cur, str) and cur.strip():
            out[key] = cur
    return out


_PROFILE = profile_values(PROFILE)
GH_OWNER = _PROFILE['owner.github'].lower() if 'owner.github' in _PROFILE else None   # None: no repo is his
NET_OK = all('network.' + k in _PROFILE for k in PROFILE_NET_KEYS)                    # False: the fallbacks
LOW, MEDIUM, HIGH = 0, 1, 2
NAMES = ('LOW', 'MEDIUM', 'HIGH')
HASH_RE = re.compile(r'^[0-9a-f]{10}$')

# --- prefilter: no match = LOW without parsing. Keep risk-check.sh's grep a superset of this. ---
PREFILTER = re.compile(
    r'(?<![\w.-])(?:unraid-do|unraid-ssh|ssh|scp|rsync|sftp|rm|unlink|shred|truncate|crontab|curl|wget|docker|docker-compose|tg|gh|'
    r'mail|mailx|sendmail|msmtp|mutt|swaks|mkfs(?:\.\w+)?|mke2fs|mkswap|wipefs|parted|fdisk|cfdisk|sgdisk|gdisk|'
    r'sfdisk|blkdiscard|dd|zfs|zpool|btrfs|xfs_repair|cryptsetup|hdparm|risk-ok)(?![\w-])'
    r'|(?<![\w.-])find\b[^\n]*\s-(?:delete|exec|execdir|ok)\b'
    r'|(?<![\w.-])git\b[^\n]*\spush\b'
    r'|(?<![\w.-])vault\b[^\n]*\s(?:admin|gmail)\b'
    r'|(?<![\w.-])proxy\s+push\b'
    r'|\.secrets\b|\.env\b|/dev/(?:sd|nvme|md|hd|vd|xvd|mapper|disk|loop|dm-|mmcblk|nbd)')


# --- shell lexer: words (quotes removed, $(...) kept raw + collected), operators with fd, heredoc bodies ---
class LexError(Exception):
    pass


OPS = ('&>>', '<<<', '<<-', '&&', '||', ';;', '>>', '<<', '&>', '>&', '<&', '>|', '|&', ';', '&', '|', '(', ')',
       '<', '>')
REDIR_OPS = {'>', '>>', '<', '<<', '<<-', '<<<', '&>', '&>>', '>&', '<&', '>|'}
WORD_END = set(' \t\r\n;&|()<>')


def _close(s, i, close):
    """Index of the ')' or '`' that closes a $( or ` opened just before s[i] (quote-aware)."""
    depth, n = 1, len(s)
    while i < n:
        c = s[i]
        if c == '\\':
            i += 2
            continue
        if close == '`':
            if c == '`':
                return i
        elif c == "'":
            j = s.find("'", i + 1)
            if j < 0:
                raise LexError('quote')
            i = j
        elif c == '"':
            i = _dquote(s, i + 1, [])[1] - 1
        elif c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise LexError('unclosed substitution')


def _dquote(s, i, subs):
    """s[i:] follows an opening double quote: (text, index after the closing quote)."""
    buf, n = [], len(s)
    while i < n:
        c = s[i]
        if c == '"':
            return ''.join(buf), i + 1
        if c == '\\' and i + 1 < n:
            if s[i + 1] in '$`"\\':
                buf.append(s[i + 1])
            elif s[i + 1] != '\n':
                buf.append(s[i:i + 2])
            i += 2
            continue
        if s.startswith('$(', i) and not s.startswith('$((', i):
            j = _close(s, i + 2, ')')
            subs.append(s[i + 2:j])
            buf.append(s[i:j + 1])
            i = j + 1
            continue
        if c == '`':
            j = _close(s, i + 1, '`')
            subs.append(s[i + 1:j])
            buf.append('$(' + s[i + 1:j] + ')')
            i = j + 1
            continue
        buf.append(c)
        i += 1
    raise LexError('unclosed double quote')


def _word(s, i):
    buf, subs, n, start = [], [], len(s), i
    while i < n:
        c = s[i]
        if c in WORD_END:
            break
        if c == '\\':
            if i + 1 < n and s[i + 1] != '\n':
                buf.append(s[i + 1])
            i += 2
            continue
        if c == "'":
            j = s.find("'", i + 1)
            if j < 0:
                raise LexError('unclosed single quote')
            buf.append(s[i + 1:j])
            i = j + 1
            continue
        if c == '"':
            text, i = _dquote(s, i + 1, subs)
            buf.append(text)
            continue
        if s.startswith('$((', i):
            j = s.find('))', i + 3)
            if j < 0:
                raise LexError('arithmetic')
            buf.append(s[i:j + 2])
            i = j + 2
            continue
        if s.startswith('$(', i):
            j = _close(s, i + 2, ')')
            subs.append(s[i + 2:j])
            buf.append(s[i:j + 1])
            i = j + 1
            continue
        if c == '`':
            j = _close(s, i + 1, '`')
            subs.append(s[i + 1:j])
            buf.append('$(' + s[i + 1:j] + ')')
            i = j + 1
            continue
        if s.startswith('${', i):
            j = s.find('}', i)
            if j < 0:
                raise LexError('brace')
            buf.append(s[i:j + 1])
            i = j + 1
            continue
        buf.append(c)
        i += 1
    if i < n and s[i] in '<>' and s[start:i].isdigit():
        return i, None, s[start:i]                    # "2>": an fd number belongs to the redirection
    return i, ''.join(buf), subs


def lex(s):
    toks, pending, want, fd = [], [], None, ''
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c in ' \t\r':
            i += 1
            continue
        if s.startswith('\\\n', i):
            i += 2
            continue
        if c == '\n':
            toks.append(('op', '\n', ''))
            i += 1
            for delim, strip, idx in pending:         # heredoc bodies start on the next line
                body = []
                while i < n:
                    j = s.find('\n', i)
                    line = s[i:] if j < 0 else s[i:j]
                    i = n if j < 0 else j + 1
                    if (line.lstrip('\t') if strip else line) == delim:
                        break
                    body.append(line)
                toks[idx] = ('hd', '\n'.join(body))
            pending = []
            continue
        if c == '#':
            j = s.find('\n', i)
            i = n if j < 0 else j
            continue
        op = next((o for o in OPS if s.startswith(o, i)), None)
        if op:
            toks.append(('op', op, fd))
            fd = ''
            i += len(op)
            if op in ('<<', '<<-'):
                want = op
            continue
        j = i
        i, word, extra = _word(s, i)
        if word is None:
            fd = extra
            continue
        toks.append(('w', word, extra, s[j:i]))       # raw text too: quotes matter for the rm-hang check
        if want:
            pending.append((word, want == '<<-', len(toks)))
            toks.append(('hd', ''))
            want = None
    return toks


class Simple:
    """One simple command: words (and their raw text), redirections (op, target, fd), heredoc/here-string, $(...)
    texts, separators, nesting (see nesting())."""
    __slots__ = ('words', 'raw', 'redirs', 'heredoc', 'subs', 'sep_before', 'sep_after', 'out_safe', 'nest')

    def __init__(self, sep_before=None):
        self.words, self.raw, self.redirs, self.subs = [], [], [], []
        self.heredoc, self.sep_before, self.sep_after, self.out_safe = None, sep_before, None, False
        self.nest = None


def parse(toks):
    cmds, cur, i = [], Simple(), 0
    while i < len(toks):
        t = toks[i]
        if t[0] == 'w':
            cur.words.append(t[1])
            cur.raw.append(t[3])
            cur.subs.extend(t[2])
        elif t[0] == 'hd':
            cur.heredoc = t[1]
        elif t[1] in REDIR_OPS:
            if i + 1 < len(toks) and toks[i + 1][0] == 'w':
                tgt = toks[i + 1]
                cur.subs.extend(tgt[2])
                if t[1] == '<<<':
                    cur.heredoc = tgt[1]
                else:
                    cur.redirs.append((t[1], tgt[1], t[2]))
                i += 1
        else:
            cur.sep_after = t[1]
            cmds.append(cur)
            cur = Simple(t[1])
        i += 1
    cmds.append(cur)
    return cmds


def pipelines(cmds):
    out, cur = [], []
    for c in cmds:
        cur.append(c)
        if c.sep_after not in ('|', '|&'):
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


NEST_OPEN, NEST_CLOSE = {'for', 'while', 'until', 'if', 'select', '{'}, {'done', 'fi', '}'}
NEST_PASS = {'do', 'then', 'else', 'elif', '!', 'time', 'coproc'}
NEST_UNCLEAR = {'case', 'esac', 'function', ';;', 'in'}


def nesting(cmds):
    """Set c.nest = (open subshells, open for/while/until/if/select/{ } bodies) around each simple command, from
    the separators and the leading keywords (raw: a quoted "do" is no keyword). Leaves None on every command when
    the structure is unclear (case, function, unbalanced)."""
    marks, par, comp = [], 0, 0
    for c in cmds:
        par += (c.sep_before == '(') - (c.sep_before == ')')
        if c.sep_before == ')' and c.raw[:1] == ['{']:
            return                                     # f() { ...: a function body runs later, if at all
        for w in c.raw:
            if w in NEST_UNCLEAR:
                return
            if w in NEST_OPEN:
                comp += 1
                if w in ('for', 'select'):
                    break                              # the rest is the loop header
            elif w in NEST_CLOSE:
                comp -= 1
            elif w not in NEST_PASS:
                break
        if par < 0 or comp < 0:
            return
        marks.append((par, comp))
    for c, m in zip(cmds, marks):
        c.nest = m


# --- context and paths ---
class Ctx:
    """Where a command runs: local (this container), server (Unraid, root), host (another owner box), remote,
    container (docker exec/run on the server). Tracks cwd, simple variables and files backed up so far."""

    def __init__(self, kind='local', host='', cwd=None, env=None, out_safe=False, depth=0, agent=None,
                 backups=None):
        self.kind, self.host, self.cwd, self.env = kind, host, cwd, dict(env or {})
        self.out_safe, self.depth, self.agent = out_safe, depth, agent
        self.backups = backups if backups is not None else set()
        self.hang, self.cd_top, self.has_cd, self.text_len = [], False, False, 0   # rm-hang check (top level)

    def same(self, out_safe=None):                     # bash -c, $(...): same machine, a copy of the variables
        return Ctx(self.kind, self.host, self.cwd, self.env, self.out_safe if out_safe is None else out_safe,
                   self.depth + 1, self.agent, self.backups)

    def remote(self, kind, host, cwd, out_safe):
        return Ctx(kind, host, cwd, None, out_safe, self.depth + 1, self.agent)

    @property
    def home(self):
        return HOME if self.kind == 'local' else '/root'

    @property
    def where(self):
        return {'local': 'on this box', 'server': 'on the server (Unraid)',
                'container': 'inside a container'}.get(self.kind, f'on {self.host}')


SECRETS = HOME + '/.secrets'
PROTECTED = [HOME + '/' + p for p in (
    'workspace', '.claude', '.claude/projects', '.claude/projects/-home-node-workspace/memory', '.claude/skills',
    '.claude/agents', '.claude/hooks', '.claude/settings.json', '.claude/channels', '.claude/plugins', '.brain.git',
    'bin', '.secrets', '.config', '.local/share', '.local/state', 'opt', 'reports', 'crontab', 'boot.sh', '.ssh',
    'plugins', 'attic')]
SYSTEM_TOP = {'/bin', '/boot', '/dev', '/etc', '/home', '/lib', '/lib64', '/mnt', '/opt', '/proc', '/root', '/run',
              '/sbin', '/srv', '/sys', '/usr', '/var', '/usr/bin', '/usr/lib', '/usr/local', '/usr/sbin', '/var/lib',
              '/var/log', '/var/cache'}
TMP_DIRS = ('/tmp', '/var/tmp', '/dev/shm')
SCRATCH = (HOME + '/.cache/', HOME + '/inbox/', HOME + '/.claude/channels/telegram/inbox/')
CACHE_DIRS = {'__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache', 'node_modules'}
GLOB = re.compile(r'[*?\[]')
DEVICE = re.compile(r'^/dev/(?:sd[a-z]|nvme\d|md\d|hd[a-z]|vd[a-z]|xvd[a-z]|mapper/|disk/|dm-\d|mmcblk\d|nbd\d|loop\d)')
QUIET = {'/dev/null', '/dev/stdout', '/dev/stderr', '/dev/tty', '/dev/zero', '/dev/random', '/dev/urandom'}
VAR = re.compile(r'\$\{(\w+)\}|\$(\w+)')
MKTEMP = re.compile(r'^\$\(\s*mktemp\b')
ASSIGN = re.compile(r'^([A-Za-z_]\w*)(?:\[[^\]]*\])?\+?=(.*)$', re.S)
LABEL = {'root': 'the whole filesystem', 'system-top': 'a system directory', 'boot-core': 'the flash drive (boot config)',
         'boot-plugins': 'the flash drive (plugin config)', 'share-top': 'a whole share or disk',
         'docker-data': "docker's data"}


def expand(w, ctx):
    out, pos = [], 0
    for m in VAR.finditer(w):
        k = m.group(1) or m.group(2)
        v = ctx.env[k] if k in ctx.env else ctx.home if k == 'HOME' else ctx.cwd if k == 'PWD' else None
        if v is None:
            return None
        out += [w[pos:m.start()], v]
        pos = m.end()
    out.append(w[pos:])
    return ''.join(out)


def resolve(word, ctx):
    """Absolute normalized path for a word, or None when it can't be known ($1, $(cmd), unknown variables)."""
    if not word:
        return None
    if MKTEMP.match(word):
        return '/tmp/mktemp'
    w = ctx.home + word[1:] if word == '~' or word.startswith('~/') else word
    w = expand(w, ctx)
    if not w or '$' in w or '`' in w:
        return None
    if not w.startswith('/'):
        if not ctx.cwd:
            return None
        w = ctx.cwd.rstrip('/') + '/' + w
    return '/' + os.path.normpath(w).lstrip('/')


def assigned(val, ctx):
    if MKTEMP.match(val):
        return '/tmp/mktemp'
    if '$(' in val:
        return None
    return expand(val, ctx) if val else ''


def glob_base(p):
    out = []
    for part in p.split('/'):
        if GLOB.search(part):
            break
        out.append(part)
    return '/'.join(out) or '/'


def under(p, d):
    return p == d or p.startswith(d.rstrip('/') + '/')


def classify_local(p):
    if p is None:
        return 'unknown'
    if p == '/':
        return 'root'
    if p in TMP_DIRS:
        return 'tmpdir'
    if p.startswith(tuple(d + '/' for d in TMP_DIRS) + SCRATCH) or p + '/' in SCRATCH or \
            os.path.basename(p) in CACHE_DIRS:
        return 'tmp'                                   # incl. handled ~/inbox notes and Telegram attachments
    if under(p, SECRETS):
        return 'secret'
    if under(HOME, p) or any(under(q, p) for q in PROTECTED):
        return 'protected'                             # the path itself or a parent of something precious
    if any(under(p, q) for q in PROTECTED):
        return 'in-protected'
    if p.startswith(HOME + '/'):
        return 'home'
    return 'system-top' if p in SYSTEM_TOP else 'system'


def classify_server(p):
    if p is None:
        return 'unknown'
    if p == '/':
        return 'root'
    if p in TMP_DIRS:
        return 'tmpdir'
    if p.startswith(tuple(d + '/' for d in TMP_DIRS)):
        return 'tmp'
    if under(p, '/boot'):
        return 'boot-plugins' if p.startswith(('/boot/config/plugins/', '/boot/logs/')) else 'boot-core'
    if under(p, '/mnt'):
        return 'share-top' if p.count('/') <= 3 else 'data'
    if under(p, '/var/lib/docker'):
        return 'docker-data'
    return 'system-top' if p in SYSTEM_TOP else 'ram'    # Unraid's rootfs lives in RAM: reset at reboot


def server_class(p, ctx):
    cls = classify_server(p)
    if cls == 'ram' and ctx.kind != 'server':          # other boxes and containers: no RAM rootfs, may be a mount
        return 'data'
    return cls


TIER_A = re.compile(r'(?:^|/)(?:\.env(?:\.[\w.-]+)?|\.netrc|\.pgpass|secrets?\.(?:json|ya?ml|env|txt)|credentials'
                    r'(?:\.json)?|id_(?:rsa|ed25519|ecdsa|dsa)|[\w.-]*\.(?:key|p12|pfx|keystore)|[\w.-]*key\.pem|'
                    r'smbpasswd|shadow)$')
TIER_B = re.compile(r'(?:/templates-user/[^/]+\.xml|(?:^|/)(?:docker-)?compose[\w.-]*\.ya?ml|/appdata/[^/]+/config\.xml)$')


def secret_tier(p):
    """A: credential stores (any read prints secrets). B: configs that often embed secrets (whole-file dumps only)."""
    if not p:
        return None
    if '/.secrets/' in p + '/' and not p.endswith('/.secrets') or TIER_A.search(p):
        return 'A' if not p.endswith(('.example', '.sample', '.template', '.dist', '.pub')) else None
    return 'B' if TIER_B.search(p) else None


def has_backup(p, ctx):
    return bool(p) and any(under(p, b) for b in ctx.backups)


def nonempty_file(p):
    try:
        return os.path.isfile(p) and os.path.getsize(p) > 0
    except OSError:
        return False


def short(s, n=70):
    s = re.sub(r'\s+', ' ', str(s)).strip()
    return s if len(s) <= n else s[:n - 1] + '…'


def add(found, level, rule, why, amb=False):
    found.append((level, rule, why, amb))


# --- the walk ---
KEYWORDS = {'if', 'then', 'else', 'elif', 'fi', 'do', 'done', 'while', 'until', '{', '}', '!', 'time', 'esac',
            'coproc'}
WRAPPERS = {'sudo': {'-u', '-g', '-h', '-p', '-C', '-D', '-r', '-t', '-U', '--user', '--group'},
            'timeout': {'-k', '-s', '--kill-after', '--signal'}, 'nice': {'-n', '--adjustment'},
            'ionice': {'-c', '-n', '-p', '-P', '-u'}, 'env': {'-u', '--unset', '-C', '--chdir', '-S'},
            'stdbuf': {'-i', '-o', '-e'}, 'flock': {'-w', '-E', '--timeout', '--conflict-exit-code'},
            'nohup': set(), 'command': set(), 'exec': set(), 'time': set(), 'setsid': set(), 'chronic': set(),
            'builtin': set(), 'unbuffer': set()}
SHELLS = {'bash', 'sh', 'dash', 'zsh', 'ksh', 'ash'}


def strip_wrappers(w):
    for _ in range(8):
        name = os.path.basename(w[0]) if w else ''
        if name not in WRAPPERS:
            break
        opts, i = WRAPPERS[name], 1
        while i < len(w) and w[i].startswith('-') and w[i] != '-':
            if w[i] == '--':
                i += 1
                break
            i += 2 if w[i] in opts else 1
        if name == 'env':
            while i < len(w) and ASSIGN.match(w[i]):
                i += 1
        if name in ('timeout', 'flock') and i < len(w):
            i += 1                                     # the duration / the lock file
        w = w[i:]
    return w


def prepare(c, ctx):
    """argv of a simple command after keywords, variable assignments (recorded) and wrappers (sudo, timeout...)."""
    w = list(c.words)
    while w and w[0] in KEYWORDS:
        w.pop(0)
    if not w or w[0] in ('case', 'function', 'select'):
        return None
    if w[0] == 'for':
        if len(w) > 1:
            items = w[3:] if len(w) > 2 and w[2] == 'in' else []
            paths = [resolve(x, ctx) for x in items]
            ctx.env[w[1]] = ('/tmp/for-item' if paths and all(p and p.startswith(TMP_DIRS) for p in paths)
                             else paths[0] if paths else None)
        return None
    while w and ASSIGN.match(w[0]):
        m = ASSIGN.match(w[0])
        ctx.env[m.group(1)] = assigned(m.group(2), ctx)
        w.pop(0)
    return strip_wrappers(w) or None


def stdout_to_file(c):
    for op, tgt, fd in c.redirs:
        if tgt in ('/dev/stdout', '/dev/stderr', '/dev/tty'):
            continue
        if op in ('&>', '&>>') or (op in ('>', '>>', '>|') and fd in ('', '1')) or \
                (op == '>&' and fd in ('', '1') and not tgt.isdigit() and tgt != '-'):
            return True
    return False


SINKS = {'secretscan', 'wc', 'sha256sum', 'sha1sum', 'sha512sum', 'md5sum', 'b2sum', 'cksum', 'ssh', 'scp', 'gh',
         'docker', 'curl', 'wget', 'crontab', 'test', '[', 'true', 'false', 'cmp', 'install', 'gpg', 'xclip'}
GREPS = {'grep', 'egrep', 'fgrep', 'rg'}
DUMP_B = {'cat', 'tac', 'head', 'tail', 'less', 'more', 'bat', 'batcat', 'xxd', 'od', 'hexdump', 'strings',
          'base64', 'nl'}
DUMP_A = DUMP_B | GREPS | {'jq', 'yq', 'awk', 'gawk', 'sed', 'cut', 'sort', 'uniq', 'column', 'paste', 'fold', 'tr',
                           'diff'}
PROGRAM_FIRST = GREPS | {'jq', 'yq', 'awk', 'gawk', 'sed'}


def grep_quiet(argv):
    for a in argv[1:]:
        if a in ('--count', '--quiet', '--silent', '--files-with-matches', '--files-without-match'):
            return True
        if a.startswith('-') and not a.startswith('--') and set(a[1:]) & set('clLq'):
            return True
    return False


def jq_keys(argv):
    filt = next((a for a in argv[1:] if not a.startswith('-')), '')
    return re.fullmatch(r'\s*(?:keys(?:_unsorted)?(?:\[\])?|length|type|has\([^)]*\)|paths)\s*', filt) is not None


def is_sink(argv, c):
    if not argv:
        return False
    name = os.path.basename(argv[0])
    return (name in SINKS or (name in GREPS and grep_quiet(argv)) or (name == 'jq' and jq_keys(argv))
            or stdout_to_file(c))


def reads_secret(argv, c, ctx):
    """The path of a credential this command would print to stdout, else None."""
    name, args = os.path.basename(argv[0]), argv[1:]
    if name == 'gh' and ([a for a in args if not a.startswith('-')][:2] == ['auth', 'token'] or
                         '--show-token' in args):
        return 'the GitHub token'
    if name not in DUMP_A:
        return None
    if (name in GREPS and grep_quiet(argv)) or (name == 'jq' and jq_keys(argv)):
        return None
    if name in GREPS and any(a == '-o' or (a.startswith('-') and not a.startswith('--') and 'o' in a) for a in args):
        pat = next((a for a in args if not a.startswith('-')), '')
        if not re.search(r'=[.\[(\\]', pat):           # grep -o '^[A-Z_]*=' lists key names only
            return None
    if name == 'cut' and any(a in ('-f1', '-f', '1') for a in args) and any(a.startswith('-d') for a in args):
        return None                                    # cut -d= -f1: key names
    if name == 'sed' and any(a.startswith('-i') or a.startswith('--in-place') for a in args):
        return None
    if name in ('head', 'tail') and (any(a in ('-c0', '-n0', '--bytes=0', '--lines=0') for a in args) or
                                     re.search(r'(?:^| )-[cn] 0(?: |$)', ' '.join(args))):
        return None
    files = [a for a in args if not a.startswith('-')]
    if files and name in PROGRAM_FIRST and not any(a in ('-e', '-f', '--regexp', '--file', '--expression')
                                                    for a in args):
        files = files[1:]                              # jq/grep/sed/awk: the first word is the filter or pattern
    files += [t for op, t, _ in c.redirs if op == '<']
    for f in files:
        p = resolve(f, ctx)
        tier = secret_tier(p)
        if tier == 'A' or (tier == 'B' and name in DUMP_B):
            return f
    return None


def analyze(text, ctx, found):
    if ctx.depth > 8 or not text or not text.strip():
        return
    try:
        cmds = parse(lex(text))
    except LexError:
        return coarse(text, ctx, found)
    if ctx.depth == 0 and ctx.kind == 'local':
        nesting(cmds)
        ctx.text_len = len(text)
        ctx.has_cd = bool(re.search(r'(?<![\w.-])(?:cd|pushd|popd)(?![\w.-])', text))
    for pl in pipelines(cmds):
        argvs = []
        for c in pl:
            for s in c.subs:                           # $(...) runs here too; its output is captured
                analyze(s, ctx.same(out_safe=True), found)
            argvs.append(prepare(c, ctx))
        for i, (c, argv) in enumerate(zip(pl, argvs)):
            c.out_safe = (ctx.out_safe or stdout_to_file(c) or
                          any(is_sink(a, s) for a, s in zip(argvs[i + 1:], pl[i + 1:])))
            check_redirs(c, ctx, found)
            if not argv:
                continue
            rule_command(argv, c, ctx, found)
            what = reads_secret(argv, c, ctx)
            if what and not c.out_safe:
                add(found, HIGH, 'secret-print',
                    f'would print a credential into the transcript ({short(what, 50)} {ctx.where}); pipe it through '
                    'secretscan --redact, count with grep -c, or read only key names')


COARSE = (
    (re.compile(r'\b(?:mkfs(?:\.\w+)?|blkdiscard)\b|\bwipefs\s+-\w*a'), 'format', 'formats or wipes a disk'),
    (re.compile(r'\brm\s+-\w*[rR]\w*\s+(?:-\S+\s+)*["\']?(?:/|~/?|\$HOME/?)\*?["\']?(?:\s|$|;)'), 'rm-protected',
     'rm -r on / or the home directory'),
    (re.compile(r'\brm\s+-\w*[rR][^;&|\n]*\s["\']?/mnt/'), 'server-delete', 'rm -r on a share or disk'),
    (re.compile(r'\bcrontab\s+(?:-\w+\s+)*-\w*r'), 'crontab-wipe', 'crontab -r deletes every scheduled job'),
    (re.compile(r'\bdocker\s+(?:volume\s+(?:rm|prune)|system\s+prune\b.*--volumes|compose\b.*\bdown\b.*(?:\s-v\b|--volumes))'),
     'docker-volume', 'deletes docker volumes'),
    (re.compile(r'\bgit\s+push\b.*(?:--force|\s-f\b|--mirror|\s\+\w)'), 'git-force-main', 'force-push'),
)


def coarse(text, ctx, found):
    """Unparseable text (bash would mostly refuse it too): only the clearest patterns count."""
    for rx, rule, why in COARSE:
        if rx.search(text):
            add(found, HIGH, rule, f'{why} (unparsed command, coarse match)')


def check_redirs(c, ctx, found):
    for op, tgt, fd in c.redirs:
        if op in ('<', '<<', '<<-', '<&') or (op == '>&' and (tgt.isdigit() or tgt == '-')):
            continue
        write_finding(tgt, ctx, found, 'a redirect', overwrite=op in ('>', '>|', '&>', '>&'))


def write_finding(word, ctx, found, how, overwrite=True):
    p = resolve(word, ctx)
    if p in QUIET or (p or '').startswith(('/proc/self/', '/dev/fd/')):
        return
    if p and DEVICE.match(p):
        return add(found, HIGH, 'device-write', f'{how} writes straight onto the disk device {p} {ctx.where}')
    if ctx.kind == 'local':
        if classify_local(p) == 'secret':
            if overwrite and nonempty_file(p) and not has_backup(p, ctx):
                add(found, HIGH, 'secrets-overwrite', f'{how} overwrites the existing credential {short(word)}')
            else:
                add(found, MEDIUM, 'secrets-write', f'{how} writes to {short(word)}')
        return
    cls = server_class(p, ctx)
    if cls in ('tmp', 'tmpdir'):
        return
    if cls == 'boot-core' and overwrite and not has_backup(p, ctx):
        return add(found, HIGH, 'flash-config', f'{how} changes {p} on the flash drive without a backup (cp it '
                                                'first, e.g. to /boot/config/x.bak)')
    add(found, MEDIUM, 'server-write', f'{how} changes {short(p or word)} {ctx.where}')


def delete_finding(word, multi, ctx, found, verb='rm', filtered=False):
    p = resolve(word, ctx)
    glob = p is not None and bool(GLOB.search(p))
    base = glob_base(p) if glob else p
    multi = multi or glob
    show = short(word)
    if ctx.kind == 'local':
        cls = classify_local(base)
        if cls == 'tmp':
            return
        if cls == 'secret':
            return add(found, HIGH, 'secrets-delete', f'{verb} deletes credentials ({show})')
        if cls in ('root', 'protected'):
            return add(found, MEDIUM if filtered else HIGH, 'rm-protected',
                       f'{verb} would delete {show}: my home, memory, config, scripts or the system')
        if cls == 'system-top':
            return add(found, HIGH, 'rm-protected', f'{verb} on the system directory {show}') if multi else None
        if cls == 'unknown':
            return add(found, MEDIUM, 'rm-unresolved', f"{verb} on a path I can't resolve ({show})",
                       amb=True) if multi else None
        if cls == 'in-protected' and not filtered:
            return add(found, MEDIUM, 'rm-home', f'{verb} deletes {show}')
        if multi and not filtered:
            add(found, MEDIUM, 'rm-home', f'{verb} deletes {show}')
        return
    cls = server_class(base, ctx)
    where = ctx.where
    if cls == 'tmp':
        return
    if cls == 'unknown':
        return add(found, HIGH if multi else MEDIUM, 'server-delete',
                   f"{verb} on a path I can't resolve ({show}) {where}", amb=True)
    if cls in LABEL:
        return add(found, HIGH, 'server-delete', f'{verb} deletes {show} {where}: {LABEL[cls]}',
                   amb=filtered and cls not in ('root', 'system-top'))
    if cls == 'data':
        maybe_mount = classify_server(base) == 'ram'   # container/other box: may or may not be persistent
        if multi and not filtered and not maybe_mount:
            return add(found, HIGH, 'server-delete', f'{verb} deletes {show} {where} (irreversible)')
        return add(found, HIGH if multi else MEDIUM if maybe_mount else HIGH, 'server-delete',
                   f'{verb} deletes {show} {where}', amb=True)
    if multi:                                          # tmpdir itself, RAM rootfs
        add(found, MEDIUM, 'server-delete', f'{verb} deletes {show} {where}')


# --- rm targets Claude Code's own check stops on: an approval nobody gives, so the call hangs ---
# Claude Code's Bash tool asks before some rm commands ("Dangerous rm operation ... requires explicit approval and
# cannot be auto-allowed by permission rules"). That is a safety check, which bypassPermissions does not skip; with
# nobody at the terminal the call waits forever (the PermissionRequest hook no-approval.sh answers it with a deny).
# hang_check() denies the clearest of those shapes before Claude Code's check runs, with a rewrite hint. It copies a
# strict subset of Claude Code's own patterns, so whatever it denies would have stopped there too:
#   rm-hang-var  a target starting with a variable, then / and a glob, a variable, a quote or nothing: $D/*, $PF/$s,
#                "$D"/*, ${D}/, $T/*.log. Not: "${D:?}"/"$x", $D/file, "$PF/b", $HOME/x, '$D'/* (literal).
#   rm-hang-cd   a relative glob target (* or dir/*) after a top-level cd or pushd in the same command.
# Only for a plain rm at the top level of the command: not inside ( ), $( ), bash -c, eval, ssh or docker exec, not
# in a command with a case statement or a function definition, and only behind keywords, assignments and the
# wrappers Claude Code skips too. Every shape in test_risk_check.py's HANG_DENY was stopped by Claude Code itself,
# every HANG_PASS command marked "cc ok" ran there (claude -p probes). Claude Code's check is remotely tunable: if
# it ever gets looser, this check only asks for a rewrite that is safer anyway.
HANG_VAR = re.compile(r'''^["']*\$(?:\{[A-Za-z_][A-Za-z0-9_]*(?::?-(?:["']{2}|"?\$\{?[A-Za-z_][A-Za-z0-9_]*\}?"?)?)?\}'''
                      r'''|[A-Za-z_][A-Za-z0-9_]*)["']*\\?/(?:[*?\[{$/"'`]|\Z)''')
HANG_LITERAL = re.compile(r"^'[^']*\$")               # '$D'/* is literal text: Claude Code skips it too
HANG_LEAD = {'do', 'then', 'else', 'elif', 'if', 'while', 'until', '!', '{', 'coproc'}
HANG_WRAPPERS = {'sudo', 'exec', 'command', 'env', 'nice', 'nohup', 'time', 'timeout', 'stdbuf', 'setsid', 'ionice'}
HANG_ASSIGN = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*\+?=')
HANG_RM = re.compile(r'^\\?(?:[\w.~-]*/)*rm\Z')
HANG_SIMPLE_VAR = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)')
HANG_GLOB_END = re.compile(r'(?:/\*+)+/*\Z')           # ends in /*: even "${D:?}"/* is stopped (unresolvable glob)
HANG_MAX_LEN = 4000                                    # the cd shape needs Claude Code's full parse of the command


def _hang_prefix_ok(prefix):
    """The raw words before rm are ones Claude Code's rm finder skips too: keywords and assignments, then wrappers
    with their options and numbers (it would also skip an option's value; this check doesn't: stricter)."""
    wrapped = False
    for w in prefix:
        if not wrapped and (w in HANG_LEAD or HANG_ASSIGN.match(w)):
            continue
        if w in HANG_WRAPPERS:
            wrapped = True
        elif not (wrapped and (w.startswith('-') or w[:1].isdigit() or HANG_ASSIGN.match(w))):
            return False
    return True


def _hang_var_why(raw, ctx):
    t = raw.replace('"', '')
    guarded = None
    if not (ctx.has_cd and HANG_GLOB_END.search(t)) and not re.search(r"[\s'\\`]|\$\(", t) and \
            '$' not in HANG_SIMPLE_VAR.sub('', t):             # "${D:?}"/* passes, but not in a command with a cd
        guarded = HANG_SIMPLE_VAR.sub(lambda m: '"${%s:?}"' % (m.group(1) or m.group(2)), t)
    fix = (f'{guarded} (the shell then stops on an empty variable) or a literal absolute path' if guarded else
           'a literal absolute path (like rm -rf /abs/dir/*) or find "${D:?}" -mindepth 1 -delete')
    return (f'this rm would hang: Claude Code\'s own "Dangerous rm" check stops on the target {short(raw, 40)} (a '
            'variable followed by /: if it is empty, rm reaches / or a top-level directory) and waits for an approval '
            f'nobody gives here. Rewrite and retry, no owner OK needed: {fix}')


def _hang_cd_why(word, ctx):
    p = resolve(word, ctx)
    d = p[:-2] if p and p.endswith('/*') and len(p) > 2 and '~' not in p else None   # S=~/x; cd $S: not expanded
    fix = (f'the absolute path {short(p, 60)} (no cd needed) or find {short(d, 60)} -mindepth 1 -delete'
           if d and not GLOB.search(d) else
           'the absolute path (like /abs/dir/*, no cd needed) or find /abs/dir -mindepth 1 -delete')
    return (f'this rm would hang: the command changes directory before it, so Claude Code\'s own "Dangerous rm" check '
            f'can\'t resolve the relative glob {short(word, 40)} and waits for an approval nobody gives here. Rewrite '
            f'and retry, no owner OK needed: {fix}')


def hang_check(args, c, ctx):
    """Records (rule, why) in ctx.hang when Claude Code's own check would stop this rm (args: after the command
    word) for an approval: see the comment above."""
    if ctx.depth or ctx.kind != 'local' or c.nest is None or c.nest[0] or c.sep_before == ')' or \
            len(c.raw) != len(c.words):
        return
    k = len(c.words) - len(args) - 1
    if k < 0 or c.words[k + 1:] != list(args) or not HANG_RM.match(c.raw[k]) or not _hang_prefix_ok(c.raw[:k]):
        return
    pairs = list(zip(args, c.raw[k + 1:]))
    for word, raw in pairs:
        tok = re.sub(r'[)\]}]+\Z', '', raw)
        if tok and not tok.startswith('-') and not HANG_LITERAL.match(tok) and HANG_VAR.match(tok):
            ctx.hang.append(('rm-hang-var', _hang_var_why(raw, ctx)))
            return
    if not ctx.cd_top or k or c.nest != (0, 0) or ctx.text_len > HANG_MAX_LEN:
        return
    for word, raw in pairs:
        if raw == word and (word == '*' or word.endswith('/*')) and not word.startswith(('/', '~', '-')) and \
                not re.search(r'[$`]', word):
            ctx.hang.append(('rm-hang-cd', _hang_cd_why(word, ctx)))
            return


# --- rules per command ---
def r_cd(name, args, c, ctx, found):
    a = [x for x in args if not x.startswith('-') or x == '-']
    ctx.cwd = ctx.home if not a else None if a[0] == '-' else resolve(a[0], ctx)
    if not ctx.depth and c.nest == (0, 0) and c.raw[:1] == [name]:
        ctx.cd_top = True                              # a plain top-level cd/pushd: see hang_check()


def r_export(name, args, c, ctx, found):
    for a in args:
        m = ASSIGN.match(a)
        if m:
            ctx.env[m.group(1)] = assigned(m.group(2), ctx)


def r_rm(name, args, c, ctx, found):
    hang_check(args, c, ctx)
    rec, targets, end = False, [], False
    for a in args:
        if not end and a == '--':
            end = True
        elif not end and a.startswith('--'):
            rec = rec or a == '--recursive'
        elif not end and a.startswith('-') and len(a) > 1:
            rec = rec or 'r' in a or 'R' in a
        else:
            targets.append(a)
    for t in targets:
        delete_finding(t, rec, ctx, found, verb='rm -r' if rec else name)


FIND_FILTERS = {'-name', '-iname', '-path', '-ipath', '-wholename', '-iwholename', '-regex', '-iregex', '-mtime',
                '-mmin', '-ctime', '-cmin', '-atime', '-amin', '-newer', '-size', '-empty', '-user', '-group',
                '-perm', '-links', '-inum', '-samefile'}


def r_find(name, args, c, ctx, found):
    execs = [i for i, a in enumerate(args) if a in ('-exec', '-execdir', '-ok', '-okdir')]
    if '-delete' not in args and not any(i + 1 < len(args) and os.path.basename(args[i + 1]) in
                                         ('rm', 'shred', 'unlink') for i in execs):
        return
    starts = []
    for a in args:
        if a.startswith('-') or a in ('(', '!', ')'):
            break
        starts.append(a)
    filtered = any(a in FIND_FILTERS for a in args)
    for s in starts or ['.']:
        delete_finding(s, True, ctx, found, verb='find -delete', filtered=filtered)


def r_unlink(name, args, c, ctx, found):
    for a in args:
        if not a.startswith('-'):
            delete_finding(a, False, ctx, found, verb='unlink')


def r_shred(name, args, c, ctx, found):
    i = 0
    while i < len(args):
        a = args[i]
        if a in ('-n', '-s', '--iterations', '--size', '--random-source'):
            i += 2
            continue
        if not a.startswith('-'):
            p = resolve(a, ctx)
            if p and DEVICE.match(p):
                add(found, HIGH, 'disk-erase', f'shred overwrites the disk device {p} {ctx.where}')
            else:
                delete_finding(a, True, ctx, found, verb='shred')
        i += 1


def r_truncate(name, args, c, ctx, found):
    i = 0
    while i < len(args):
        if args[i] in ('-s', '--size', '-r', '--reference'):
            i += 2
            continue
        if not args[i].startswith('-'):
            write_finding(args[i], ctx, found, 'truncate')
        i += 1


def r_sed(name, args, c, ctx, found):
    suffix, files, script, i = None, [], False, 0
    while i < len(args):
        a = args[i]
        if a in ('-e', '--expression', '-f', '--file', '-l', '--line-length'):
            script = script or a in ('-e', '--expression', '-f', '--file')
            i += 2
            continue
        if a.startswith(('--expression=', '--file=')):
            script = True
        elif a == '--in-place' or a.startswith('--in-place='):
            suffix = a.split('=', 1)[1] if '=' in a else ''
        elif a.startswith('-') and not a.startswith('--') and len(a) > 1:
            if 'i' in a[1:]:
                suffix = a[a.index('i', 1) + 1:]       # GNU: -i.bak / -Ei (the rest of the word is the suffix)
        elif script:
            files.append(a)
        else:
            script = True                              # the first plain word is the script
        i += 1
    if suffix is None:
        return
    for f in files:
        if suffix:
            ctx.backups.add(resolve(f, ctx) or '')
        write_finding(f, ctx, found, 'sed -i')


def r_perl(name, args, c, ctx, found):
    inplace, files, i = None, [], 0
    while i < len(args):
        a = args[i]
        if a.startswith('-') and not a.startswith('--'):
            if 'i' in a[1:]:
                inplace = a[a.index('i', 1) + 1:]
            if a[-1] in 'eE':
                i += 1                                 # -e / -pe 'script'
        else:
            files.append(a)
        i += 1
    if inplace is not None:
        for f in files:
            write_finding(f, ctx, found, 'perl -i')


def r_tee(name, args, c, ctx, found):
    append = any(a in ('-a', '--append') for a in args)
    for a in args:
        if not a.startswith('-'):
            write_finding(a, ctx, found, 'tee -a' if append else 'tee', overwrite=not append)


def _dest_srcs(args, value_opts=()):
    nonopt, target, i = [], None, 0
    while i < len(args):
        a = args[i]
        if a in ('-t', '--target-directory'):
            target = args[i + 1] if i + 1 < len(args) else None
            i += 2
            continue
        if a in value_opts:
            i += 2
            continue
        if a.startswith('--target-directory='):
            target = a.split('=', 1)[1]
        elif not a.startswith('-'):
            nonopt.append(a)
        i += 1
    if target is None:
        return (nonopt[-1], nonopt[:-1]) if len(nonopt) >= 2 else (None, [])
    return target, nonopt


def r_cp(name, args, c, ctx, found):
    dest, srcs = _dest_srcs(args, ('-S', '--suffix'))
    if dest is None:
        return
    for s in srcs:
        p = resolve(s, ctx)
        if p:
            ctx.backups.add(p)
    if ctx.kind == 'local':
        if any(classify_local(resolve(s, ctx)) == 'secret' for s in srcs) and \
                classify_local(resolve(dest, ctx)) != 'secret':
            add(found, MEDIUM, 'secrets-copy', f'copies a credential out of ~/.secrets (to {short(dest)})')
        return
    write_finding(dest, ctx, found, name, overwrite=False)


def r_mv(name, args, c, ctx, found):
    dest, srcs = _dest_srcs(args, ('-S', '--suffix'))
    if dest is None:
        return
    if resolve(dest, ctx) == '/dev/null':
        return add(found, HIGH, 'rm-protected' if ctx.kind == 'local' else 'server-delete',
                   f'mv onto /dev/null destroys {short(" ".join(srcs))} {ctx.where}')
    if ctx.kind == 'local':
        for s in srcs:
            cls = classify_local(resolve(s, ctx))
            if cls == 'secret':
                add(found, MEDIUM, 'secrets-move', f'moves the credential {short(s)}')
            elif cls in ('protected', 'root'):
                add(found, MEDIUM, 'move-core', f'moves {short(s)} (part of my home or config)')
        return
    if any(server_class(resolve(x, ctx), ctx) not in ('tmp', 'tmpdir') for x in srcs + [dest]):
        add(found, MEDIUM, 'server-write', f'mv {short(" ".join(srcs), 40)} -> {short(dest, 40)} {ctx.where}')


def r_tar(name, args, c, ctx, found):
    mode = args[0].lstrip('-') if args else ''
    nonopt = [a for a in args[1:] if not a.startswith('-')]
    if 'c' in mode:
        ctx.backups.update(p for p in (resolve(a, ctx) for a in nonopt) if p)
    elif 'x' in mode and ctx.kind != 'local':
        add(found, MEDIUM, 'server-write', f'tar extracts files over existing ones {ctx.where}')


def r_dd(name, args, c, ctx, found):
    for a in args:
        if a.startswith('of='):
            write_finding(a[3:], ctx, found, 'dd')


def r_chmod(name, args, c, ctx, found):
    if ctx.kind == 'local' or not any(a == '--recursive' or (a.startswith('-') and not a.startswith('--') and
                                                              'R' in a) for a in args):
        return
    for t in [a for a in args if not a.startswith('-')][1:]:
        cls = server_class(resolve(t, ctx), ctx)
        if cls in ('root', 'system-top'):
            return add(found, HIGH, 'server-perms', f'{name} -R on {short(t)} {ctx.where} (breaks the system)')
        if cls not in ('tmp', 'tmpdir', 'ram'):
            return add(found, MEDIUM, 'server-perms', f'{name} -R on {short(t)} {ctx.where}')


def r_disk(name, args, c, ctx, found):
    flags = [a for a in args if a.startswith('-')]
    nonopt = [a for a in args if not a.startswith('-')]
    dev = short(nonopt[-1]) if nonopt else 'a device'
    if name.startswith('mkfs') or name in ('mke2fs', 'mkswap', 'blkdiscard'):
        return add(found, HIGH, 'format', f'{name} formats or erases {dev} {ctx.where}')
    if name == 'wipefs':
        if any(a in ('--all', '-o', '--offset') or re.match(r'^-[a-zA-Z]*a', a) for a in flags) and \
                not any(a in ('-n', '--no-act') for a in flags):
            add(found, HIGH, 'format', f'wipefs erases the filesystem signatures on {dev} {ctx.where}')
        return
    if name in ('fdisk', 'gdisk', 'cfdisk', 'sgdisk', 'sfdisk', 'parted'):
        ro = {'fdisk': {'-l', '--list'}, 'gdisk': {'-l'}, 'cfdisk': set(),
              'sgdisk': {'-p', '--print', '-v', '--verify', '-i', '--info', '-O', '--print-mbr', '-P', '--pretend'},
              'sfdisk': {'-l', '--list', '-d', '--dump', '-s', '--show-size', '-J', '--json', '-V', '--verify',
                         '-g', '--show-geometry', '-F', '--list-free', '-n', '--no-act'},
              'parted': {'-l', '--list'}}[name]
        if any(a.split('=')[0] in ro for a in flags):
            return
        if name == 'parted' and {a.lower() for a in nonopt[1:]} <= {'print', 'unit', 's', 'b', 'mb', 'gb', 'tb',
                                                                     'mib', 'gib', 'free', 'list', 'all'}:
            return
        return add(found, HIGH, 'partition', f'{name} can rewrite the partition table of {dev} {ctx.where}')
    if name == 'cryptsetup':
        sub = nonopt[0] if nonopt else ''
        if sub in ('luksFormat', 'erase', 'luksErase', 'luksKillSlot', 'luksRemoveKey', 'reencrypt', 'convert'):
            add(found, HIGH, 'format', f'cryptsetup {sub} can make the encrypted disk unreadable')
        elif sub in ('open', 'luksOpen', 'close', 'luksClose', 'luksAddKey', 'luksChangeKey', 'resize'):
            add(found, MEDIUM, 'disk-change', f'cryptsetup {sub} changes an encrypted device {ctx.where}')
        return
    if name == 'hdparm':
        if any(a.startswith(('--security-erase', '--trim-sector-ranges', '--make-bad-sector', '--write-sector',
                             '--dco-restore', '--dco-setmax', '--fwdownload')) for a in flags):
            add(found, HIGH, 'disk-erase', f'hdparm can erase or cripple {dev}')
        return
    if name == 'xfs_repair':
        if '-n' in flags:
            return
        if any('L' in a[1:] and not a.startswith('--') for a in flags):
            return add(found, HIGH, 'fs-repair', 'xfs_repair -L zeroes the log (recent writes are lost)')
        add(found, MEDIUM, 'fs-repair', f'xfs_repair modifies the filesystem on {dev}')


ZFS = {'destroy': HIGH, 'rollback': HIGH, 'receive': MEDIUM, 'recv': MEDIUM, 'set': MEDIUM, 'inherit': MEDIUM,
       'create': MEDIUM, 'rename': MEDIUM, 'promote': MEDIUM, 'mount': MEDIUM, 'unmount': MEDIUM, 'umount': MEDIUM,
       'clone': MEDIUM, 'release': MEDIUM, 'change-key': MEDIUM}
ZPOOL = {'destroy': HIGH, 'labelclear': HIGH, 'create': HIGH, 'add': HIGH, 'split': HIGH, 'remove': MEDIUM,
         'detach': MEDIUM, 'attach': MEDIUM, 'replace': MEDIUM, 'offline': MEDIUM, 'online': MEDIUM,
         'export': MEDIUM, 'import': MEDIUM, 'clear': MEDIUM, 'reguid': MEDIUM, 'upgrade': MEDIUM, 'set': MEDIUM,
         'initialize': MEDIUM, 'checkpoint': MEDIUM}
BTRFS_GROUPS = ('subvolume', 'filesystem', 'device', 'balance', 'check', 'rescue', 'property', 'replace', 'scrub',
                'quota', 'qgroup', 'send', 'receive', 'restore', 'inspect-internal')
BTRFS = (('subvolume', 'delete', HIGH), ('device', 'remove', HIGH), ('device', 'delete', HIGH),
         ('filesystem', 'resize', MEDIUM), ('filesystem', 'defragment', MEDIUM), ('device', 'add', MEDIUM),
         ('property', 'set', MEDIUM), ('replace', 'start', MEDIUM), ('quota', 'disable', MEDIUM))


def r_zfs(name, args, c, ctx, found):
    nonopt = [a for a in args if not a.startswith('-')]
    sub = nonopt[0] if nonopt else ''
    if name == 'zpool' and '-n' in args and sub in ('create', 'add', 'remove', 'split'):
        return
    lv = (ZFS if name == 'zfs' else ZPOOL).get(sub)
    if lv is not None:
        add(found, lv, f'{name}-{sub}', f'{name} {sub} {short(" ".join(nonopt[1:3]), 50)} {ctx.where}' +
            (' (irreversible)' if lv == HIGH else ''))


def r_btrfs(name, args, c, ctx, found):
    nonopt = [a for a in args if not a.startswith('-')]
    if not nonopt:
        return
    grp = next((g for g in BTRFS_GROUPS if g.startswith(nonopt[0])), nonopt[0])
    sub = nonopt[1] if len(nonopt) > 1 else ''
    what = f'btrfs {grp} {sub} {short(" ".join(nonopt[2:]), 40)}'.strip()
    if grp == 'rescue' or (grp == 'check' and '--repair' in args):
        return add(found, HIGH, 'btrfs', f'{what}: repairs that can lose data {ctx.where}')
    if grp == 'balance' and (not sub or sub.startswith('/') or 'start'.startswith(sub)):
        return add(found, MEDIUM, 'btrfs', f'btrfs balance rewrites a pool {ctx.where}')
    for g, s, lv in BTRFS:
        if grp == g and len(sub) >= 2 and s.startswith(sub):
            return add(found, lv, 'btrfs', f'{what} {ctx.where}' + (' (irreversible)' if lv == HIGH else ''))


def r_mdcmd(name, args, c, ctx, found):
    sub = args[0] if args else ''
    if sub in ('', 'status', 'dump', 'spindown', 'spinup'):
        return
    if sub in ('check', 'nocheck'):
        return add(found, MEDIUM, 'array', f'mdcmd {sub}: starts or stops a parity check')
    add(found, HIGH, 'array', f'mdcmd {short(" ".join(args), 40)} changes the array (stop/start/disk assignments)')


def r_emcmd(name, args, c, ctx, found):
    a = ' '.join(args)
    if re.search(r'cmd(?:Start|Stop|NewConfig|Init|Format|Clear|Shutdown|Reboot)', a, re.I):
        add(found, HIGH, 'array', f'emcmd {short(a, 40)} starts/stops the array or changes disks')
    elif not re.search(r'spin(?:down|up)', a, re.I):
        add(found, MEDIUM, 'server-config', f'emcmd {short(a, 40)} changes a server setting')


def r_mover(name, args, c, ctx, found):
    if ctx.kind == 'server' and args[:1] != ['status']:
        add(found, MEDIUM, 'mover', 'runs the mover (moves data between cache and array)')


def r_power(name, args, c, ctx, found):
    if ctx.kind == 'local' or (name in ('init', 'telinit') and args[:1] not in (['0'], ['6'])) or \
            (name == 'shutdown' and '-c' in args):
        return
    add(found, HIGH, 'server-power', f'{name} takes {"the whole server (all services, me included)" if ctx.kind == "server" else ctx.host} down')


def r_service(name, args, c, ctx, found):
    if ctx.kind == 'local':
        return
    nonopt = [a for a in args if not a.startswith('-')]
    if name in ('kill', 'pkill', 'killall'):
        return add(found, MEDIUM, 'server-kill', f'{name} kills processes {ctx.where}')
    if name in ('mount', 'umount'):
        return add(found, MEDIUM, 'server-mount', f'{name} {short(" ".join(nonopt), 40)} {ctx.where}') if nonopt else None
    if name == 'systemctl':
        sub = nonopt[0] if nonopt else ''
        if sub in ('poweroff', 'reboot', 'halt', 'kexec'):
            return add(found, HIGH, 'server-power', f'systemctl {sub} takes {ctx.host or "the box"} down')
        if sub in ('stop', 'restart', 'disable', 'mask', 'kill', 'isolate', 'reload'):
            return add(found, MEDIUM, 'server-service', f'systemctl {sub} {short(" ".join(nonopt[1:]), 30)} {ctx.where}')
        return
    if name.startswith('rc.') and nonopt[:1] in (['stop'], ['restart'], ['start'], ['reload']):
        add(found, MEDIUM, 'server-service', f'{name} {nonopt[0]} restarts a server service')


def r_crontab(name, args, c, ctx, found):
    flags = ''.join(a[1:] for a in args if a.startswith('-') and len(a) > 1 and not a.startswith('--'))
    nonopt = [a for a in args if not a.startswith('-')]
    if 'u' in flags and nonopt:
        nonopt = nonopt[1:]                            # -u USER
    stdin_null = any(op == '<' and t == '/dev/null' for op, t, _ in c.redirs)
    if 'r' in flags:
        return add(found, HIGH, 'crontab-wipe', f'crontab -r deletes every scheduled job {ctx.where}')
    if 'l' in flags or 'e' in flags:
        return
    if not nonopt or nonopt == ['-']:
        if stdin_null or (c.heredoc is not None and not c.heredoc.strip()):
            return add(found, HIGH, 'crontab-wipe', f'replaces the crontab with nothing {ctx.where}')
        return add(found, MEDIUM, 'crontab-replace', f'replaces the whole crontab from stdin {ctx.where}')
    f = resolve(nonopt[0], ctx)
    if f == '/dev/null':
        return add(found, HIGH, 'crontab-wipe', f'replaces the crontab with nothing {ctx.where}')
    if not (ctx.kind == 'local' and f == HOME + '/crontab'):   # `crontab ~/crontab` is the routine install
        add(found, MEDIUM, 'crontab-replace', f'replaces the whole crontab with {short(nonopt[0])} {ctx.where}')


MAIN_BRANCHES = {'main', 'master', 'HEAD', '@'}


def r_git(name, args, c, ctx, found):
    i = 0
    while i < len(args) and args[i].startswith('-'):
        i += 2 if args[i] in ('-C', '-c', '--git-dir', '--work-tree', '--namespace') else 1
    if i >= len(args) or args[i] != 'push':
        return
    rest = args[i + 1:]
    opts = [a for a in rest if a.startswith('-')]
    pos = [a for a in rest if not a.startswith('-')]
    letters = ''.join(a[1:] for a in opts if not a.startswith('--'))
    force = 'f' in letters or any(a == '--force' or a.startswith('--force-with-lease') or a == '--force-if-includes'
                                  for a in opts)
    if '--mirror' in opts:
        return add(found, HIGH, 'git-force-main', 'git push --mirror overwrites and deletes refs on the remote')
    specs = []
    for r in pos[1:]:
        r2 = r.lstrip('+')
        dst = (r2.split(':')[-1] if ':' in r2 else r2).replace('refs/heads/', '')
        specs.append((dst, r.startswith('+'), r2.startswith(':')))
    if 'd' in letters or '--delete' in opts or any(s[2] for s in specs):
        dsts = [s[0] for s in specs]
        return add(found, HIGH if set(dsts) & MAIN_BRANCHES else MEDIUM, 'git-delete-branch',
                   f'git push deletes the remote branch {short(" ".join(dsts), 40)}')
    if force or any(s[1] for s in specs):
        forced = [s[0] for s in specs if s[1] or force]
        if not specs or '--all' in opts or set(forced) & MAIN_BRANCHES:
            return add(found, HIGH, 'git-force-main',
                       'force-push that can rewrite main/master on the remote' + ('' if specs else ' (no branch given)'))
        add(found, MEDIUM, 'git-force', f'force-push to {short(", ".join(forced), 40)}')


def _opt_value(args, names):
    for i, a in enumerate(args):
        if a in names and i + 1 < len(args):
            return args[i + 1]
        for n in names:
            if n.startswith('--') and a.startswith(n + '='):
                return a.split('=', 1)[1]
    return None


def gh_own(repo, ctx):
    if not GH_OWNER:                                   # no profile: no repo counts as his
        return False
    if repo:
        return repo.lower().split('github.com/')[-1].split('/')[0] == GH_OWNER
    if ctx.kind != 'local' or not ctx.cwd:
        return False
    try:
        r = subprocess.run(['git', '-C', ctx.cwd, 'remote', 'get-url', 'origin'], capture_output=True, text=True,
                           timeout=2)
        return re.search(rf'[:/]{re.escape(GH_OWNER)}/', r.stdout, re.I) is not None
    except (OSError, subprocess.SubprocessError):
        return False


def r_gh(name, args, c, ctx, found):
    nonopt = [a for a in args if not a.startswith('-')]
    grp, sub = (nonopt + ['', ''])[:2]
    repo = _opt_value(args, ('-R', '--repo'))
    if grp == 'repo':
        target = nonopt[2] if len(nonopt) > 2 else repo or 'this repo'
        if sub == 'delete':
            return add(found, HIGH, 'gh-delete', f'gh repo delete {short(target, 40)}: deletes a GitHub repository')
        public = '--public' in args or _opt_value(args, ('--visibility',)) == 'public'
        if sub in ('create', 'edit') and public:
            return add(found, HIGH, 'gh-public', f'gh repo {sub} makes a repository public (posting publicly)')
        if sub in ('create', 'edit', 'archive', 'unarchive', 'rename', 'sync', 'deploy-key'):
            add(found, MEDIUM, 'gh-write', f'gh repo {sub} {short(target, 40)}')
    elif grp == 'gist':
        if sub in ('create', 'new', 'edit'):
            add(found, HIGH if ('--public' in args or '-p' in args) else MEDIUM,
                'gh-public' if ('--public' in args or '-p' in args) else 'gh-write', f'gh gist {sub}')
        elif sub == 'delete':
            add(found, MEDIUM, 'gh-write', 'gh gist delete')
    elif grp in ('issue', 'pr', 'release'):
        if sub in ('create', 'new', 'comment', 'review', 'close', 'reopen', 'merge', 'edit', 'lock', 'unlock',
                   'ready', 'delete', 'transfer', 'pin', 'unpin', 'develop', 'upload', 'delete-asset'):
            if gh_own(repo, ctx):
                add(found, MEDIUM, 'gh-write', f'gh {grp} {sub} on the owner\'s repo {repo or ""}'.strip())
            else:
                add(found, HIGH, 'gh-public', f'gh {grp} {sub} posts on a GitHub repo that is not the owner\'s '
                                              f'({repo or "this checkout"}): contacting other people / posting publicly')
    elif grp == 'api':
        method = (_opt_value(args, ('-X', '--method')) or '').upper()
        if not method:
            method = 'POST' if any(a in ('-f', '-F', '--field', '--raw-field', '--input') for a in args) else 'GET'
        endpoint = sub
        if method == 'DELETE':
            return add(found, HIGH, 'gh-api-delete', f'gh api DELETE {short(endpoint, 40)}')
        if method in ('POST', 'PUT', 'PATCH'):
            m = re.match(r'^/?repos/([^/]+)/', endpoint)
            if m and m.group(1).lower() != GH_OWNER:
                add(found, HIGH, 'gh-public', f'gh api {method} on someone else\'s repo ({m.group(1)})')
            else:
                add(found, MEDIUM, 'gh-write', f'gh api {method} {short(endpoint, 40)}')
    elif grp == 'auth' and sub == 'logout':
        add(found, MEDIUM, 'gh-write', 'gh auth logout (needs the owner to log in again)')
    elif grp in ('secret', 'variable', 'ssh-key', 'gpg-key', 'workflow', 'run') and \
            sub in ('set', 'delete', 'remove', 'add', 'run', 'enable', 'disable', 'rerun', 'cancel'):
        add(found, MEDIUM, 'gh-write', f'gh {grp} {sub}')


if NET_OK:                                             # his hosts, from the profile
    SERVER_HOSTS = ('unraid', _PROFILE['network.server_name'].lower(), _PROFILE['network.server_host'].lower())
    LAN_HOST = re.compile(r'^' + re.escape(_PROFILE['network.lan_prefix']) + r'\d{1,3}$')
    DOMAIN = _PROFILE['network.domain'].lower()
    PROXY_HOST = _PROFILE['network.hosts.web_vm'].split('@')[-1]          # the nginx reverse proxy (web VM)
    OWNER_HOSTS = (re.escape(_PROFILE['network.lan_prefix']) + r'\d{1,3}|unraid|' +
                   re.escape(_PROFILE['network.server_name'].lower()) + r'|[\w.-]*' + re.escape(DOMAIN) +
                   r'|homeassistant(?:\.local)?')
else:                                                  # no profile: the ssh alias is the server, any host is his
    SERVER_HOSTS, LAN_HOST, DOMAIN, PROXY_HOST = ('unraid',), None, None, None
    OWNER_HOSTS = r'[\w.-]+'
OWNER_URL = re.compile(r'^(?:\w+://)?(?:[^/@\s]+@)?(' + OWNER_HOSTS + r')(?::(\d+))?(?:[/?#]|$)', re.I)
SERVICE_PORTS = {'7878': 'Radarr', '8989': 'Sonarr', '8000': 'paperless', '8123': 'Home Assistant', '8443': 'UniFi',
                 '5055': 'Seerr', '8096': 'Emby', '9696': 'Prowlarr', '8920': 'Emby'}
DESTRUCTIVE_WORDS = re.compile(r'delete|remove|purge|wipe|erase|destroy|format|factory|forget|reset|cmdStop|cmdStart|'
                               r'NewConfig|shutdown|reboot|restore', re.I)
HIGH_DELETE = re.compile(r'delete_?files=true|/api/documents/|/api/v\d+/(?:movie|series|episodefile|moviefile|'
                         r'artist|album|book|author)(?:file)?/', re.I)
CURL_VALUE = {'-H', '--header', '-o', '--output', '-u', '--user', '-A', '--user-agent', '-e', '--referer', '-b',
              '--cookie', '-c', '--cookie-jar', '-K', '--config', '-m', '--max-time', '--connect-timeout', '-w',
              '--write-out', '--retry', '-x', '--proxy', '--cacert', '--cert', '--key', '--resolve', '--interface',
              '-r', '--range', '-C', '--continue-at', '-E', '-U', '--proxy-user', '--limit-rate', '--max-filesize',
              '--retry-delay', '--retry-max-time', '-y', '-Y', '-z', '--time-cond', '--dns-servers', '--unix-socket',
              '--oauth2-bearer', '--noproxy', '--variable', '--retry-connrefused'}
CURL_DATA = {'-d', '--data', '--data-raw', '--data-binary', '--data-urlencode', '--data-ascii', '--json', '-F',
             '--form', '--form-string'}


def service_label(url):
    m = OWNER_URL.match(url)
    if not m:
        return 'a service'
    if PROXY_HOST and m.group(1) == PROXY_HOST:
        return 'the nginx proxy'
    return SERVICE_PORTS.get(m.group(2) or '', f'{m.group(1)}{":" + m.group(2) if m.group(2) else ""}')


def r_curl(name, args, c, ctx, found):
    method, data, uploads, urls, get, i = None, [], [], [], False, 0
    while i < len(args):
        a, nxt = args[i], args[i + 1] if i + 1 < len(args) else ''
        if name == 'curl':
            if a in ('-X', '--request'):
                method, i = nxt.upper(), i + 2
                continue
            if a.startswith('-X') and len(a) > 2:
                method = a[2:].upper()
            elif a.startswith('--request='):
                method = a.split('=', 1)[1].upper()
            elif a in CURL_DATA:
                data.append(nxt)
                i += 2
                continue
            elif a in ('-T', '--upload-file'):
                uploads.append(nxt)
                i += 2
                continue
            elif a == '--url':
                urls.append(nxt)
                i += 2
                continue
            elif a in CURL_VALUE:
                i += 2
                continue
            elif a in ('-G', '--get'):
                get = True
            elif a in ('-I', '--head'):
                method = method or 'HEAD'
            elif not a.startswith('-'):
                urls.append(a)
        else:
            if a.startswith('--method='):
                method = a.split('=', 1)[1].upper()
            elif a.startswith(('--post-data=', '--body-data=')):
                data.append(a.split('=', 1)[1])
            elif a.startswith(('--post-file=', '--body-file=')):
                data.append('@' + a.split('=', 1)[1])
            elif a in ('-O', '-o', '-P', '-U', '--header', '-e', '--user', '--password', '-t', '-T', '-w', '-a',
                       '-i', '--input-file'):
                i += 2
                continue
            elif not a.startswith('-'):
                urls.append(a)
        i += 1
    method = method or ('PUT' if uploads else 'POST' if data and not get else 'GET')
    owner = [u for u in urls if OWNER_URL.match(u)]
    files = [d[1:] for d in data if d.startswith('@')] + [d.split('=@', 1)[1] for d in data if '=@' in d] + uploads
    for f in files:
        if secret_tier(resolve(f.split(';')[0], ctx)) == 'A':
            to_owner = NET_OK and bool(owner) and len(owner) == len(urls)   # no profile: no relief
            add(found, MEDIUM if to_owner else HIGH, 'secret-send',
                f'uploads the credential file {short(f, 40)} ' + ('to the owner\'s server' if to_owner else 'off this box'))
    if not owner or method in ('GET', 'HEAD', 'OPTIONS'):
        return
    svc, blob = service_label(owner[0]), ' '.join(owner + data)
    if method == 'DELETE':
        if HIGH_DELETE.search(blob):
            return add(found, HIGH, 'service-delete', f'DELETE on {svc} deletes media files or documents')
        return add(found, MEDIUM, 'service-delete', f'DELETE request to {svc}', amb=True)
    if DESTRUCTIVE_WORDS.search(blob):
        return add(found, HIGH, 'service-write', f'{method} to {svc} that looks destructive', amb=True)
    add(found, MEDIUM, 'service-write', f'{method} request changes {svc}')


def r_tg(name, args, c, ctx, found):
    if any(re.search(r'\.secrets/[\w.-]+', a) for a in args):
        add(found, HIGH, 'secret-send', 'tg would send a credential from ~/.secrets through Telegram')


def r_mail(name, args, c, ctx, found):
    add(found, HIGH, 'mail-send', f'{name} sends e-mail: contacting other people needs the owner\'s OK')


def r_vault(name, args, c, ctx, found):
    nonopt = [a for a in args if not a.startswith('-')]
    if nonopt[:1] == ['admin'] and len(nonopt) > 1 and nonopt[1] in ('deploy', 'restart', 'down', 'init'):
        add(found, MEDIUM, 'vault-admin', f'vault admin {nonopt[1]} changes the privacy vault on the server')
    elif nonopt[:1] == ['gmail'] and len(nonopt) > 1 and nonopt[1] in ('send', 'reply', 'forward'):
        add(found, HIGH, 'mail-send', f'vault gmail {nonopt[1]} sends e-mail: contacting other people needs the owner\'s OK')


def r_proxy(name, args, c, ctx, found):
    if args[:1] == ['push']:
        add(found, MEDIUM, 'proxy-push', 'pushes nginx config to the proxy (tested, auto-rollback)')


def r_riskok(name, args, c, ctx, found):
    if ctx.agent and args and HASH_RE.match(args[0]):
        add(found, HIGH, 'risk-ok-subagent', 'only the main session issues risk-ok codes, after the owner\'s OK on Telegram')


SSH_VALUE = set('BbcDEeFIiJLlmOoPpQRSWw')


def parse_ssh(args):
    i = 0
    while i < len(args):
        a = args[i]
        if a == '--':
            i += 1
            break
        if not a.startswith('-') or a == '-':
            break
        for j, ch in enumerate(a[1:], 1):
            if ch in SSH_VALUE:
                i += 1 if j == len(a) - 1 else 0       # -p 22 takes the next word, -p22 doesn't
                break
        i += 1
    return (args[i], args[i + 1:]) if i < len(args) else (None, [])


def host_kind(h):
    h = h.split('@')[-1].lower()
    if h in SERVER_HOSTS:
        return 'server'
    if not NET_OK or LAN_HOST.match(h) or h.endswith(DOMAIN):   # no profile: any host may be one of his boxes
        return 'host'
    return 'remote'


def r_ssh(name, args, c, ctx, found):
    host, rest = parse_ssh(args)
    if not host:
        return
    kind = host_kind(host)
    sub = ctx.remote(kind, host.split('@')[-1], '/root' if kind == 'server' else None, c.out_safe)
    first = rest[0].split()[0] if rest and rest[0].split() else ''
    if c.heredoc is not None and (not rest or (os.path.basename(first) in SHELLS and '-c' not in ' '.join(rest).split())):
        analyze(c.heredoc, sub, found)                 # ssh host [bash -s] <<EOF: the heredoc is the script
    elif rest:
        analyze(' '.join(rest), sub, found)


def remote_spec(a):
    m = re.match(r'^(?:[\w.-]+@)?([\w.-]+):(.*)$', a)
    return (m.group(1), m.group(2)) if m and not a.startswith(('/', '.', '~')) else None


RSYNC_VALUE = {'-e', '--rsh', '-f', '--filter', '--exclude', '--include', '--exclude-from', '--include-from',
               '--files-from', '--chmod', '--chown', '--log-file', '--partial-dir', '-T', '--temp-dir',
               '--rsync-path', '--port', '--password-file', '-B', '--block-size', '--backup-dir', '--suffix',
               '--compare-dest', '--copy-dest', '--link-dest', '--max-size', '--min-size', '--timeout', '--bwlimit',
               '-M', '--remote-option'}
SCP_VALUE = {'-c', '-F', '-i', '-l', '-o', '-P', '-S', '-J', '-D', '-X'}


def r_copy(name, args, c, ctx, found):
    takes = RSYNC_VALUE if name == 'rsync' else SCP_VALUE
    opts, nonopt, i = [], [], 0
    while i < len(args):
        a = args[i]
        if a.startswith('-') and len(a) > 1:
            opts.append(a)
            i += 2 if a in takes else 1
            continue
        nonopt.append(a)
        i += 1
    if name == 'sftp' or len(nonopt) < 2:
        return
    dest, srcs = nonopt[-1], nonopt[:-1]
    d = remote_spec(dest)
    owner_dest = d is not None and host_kind(d[0]) != 'remote'
    if owner_dest:
        p = d[1] or '/root'
        path = '/' + os.path.normpath(p if p.startswith('/') else '/root/' + p).lstrip('/')
        cls = classify_server(path)
        if cls not in ('tmp', 'tmpdir'):
            if any(o.startswith('--delete') for o in opts):
                add(found, HIGH, 'server-delete', f'rsync --delete removes files under {path} on {d[0]} that are not in the source')
            else:
                add(found, MEDIUM, 'server-write', f'{name} copies files onto {d[0]} ({short(path, 50)})')
    for s in srcs:
        rs = remote_spec(s)
        if rs and host_kind(rs[0]) != 'remote' and '--remove-source-files' in opts:
            add(found, HIGH, 'server-delete', f'rsync --remove-source-files deletes the copied files on {rs[0]}')
        if not rs and d and secret_tier(resolve(s, ctx)) == 'A':
            add(found, MEDIUM if owner_dest and NET_OK else HIGH, 'secret-send',
                f'{name} copies the credential {short(s, 40)} to {d[0]}')


def r_shell(name, args, c, ctx, found):
    for i, a in enumerate(args):
        if a.startswith('-') and not a.startswith('--') and 'c' in a[1:]:
            if i + 1 < len(args):
                analyze(args[i + 1], ctx.same(c.out_safe), found)
            return
        if not a.startswith('-'):
            break
    if '-n' in args:
        return                                         # syntax check only
    files = [a for a in args if not a.startswith('-')]
    if not files:
        if c.heredoc is not None:
            analyze(c.heredoc, ctx.same(c.out_safe), found)
    elif ctx.kind != 'local':
        add(found, MEDIUM, 'server-script', f'runs the script {short(files[0], 50)} {ctx.where} (content not checked)')


def r_eval(name, args, c, ctx, found):
    analyze(' '.join(args), ctx.same(c.out_safe), found)


DOCKER_GLOBAL = {'-H', '--host', '-c', '--context', '--config', '-l', '--log-level', '--tlscacert', '--tlscert',
                 '--tlskey'}
DOCKER_CHANGE = {'start', 'stop', 'restart', 'kill', 'pause', 'unpause', 'update', 'rename', 'create', 'cp',
                 'commit', 'tag', 'pull', 'push', 'build', 'load', 'import', 'rmi', 'connect', 'disconnect'}
EXEC_VALUE = {'-u', '--user', '-w', '--workdir', '-e', '--env', '--env-file', '--detach-keys'}
COMPOSE_GLOBAL = {'-f', '--file', '-p', '--project-name', '--project-directory', '--env-file', '--profile', '--ansi',
                  '--progress', '--parallel'}
INNER_START = {'rm', 'sh', 'bash', 'find', 'dd', 'shred', 'wipefs', 'truncate', 'sed', 'tee', 'mv', 'unlink'}


def _exec_inner(rest, ctx, c, found):
    i = 0
    while i < len(rest) and rest[i].startswith('-'):
        i += 2 if rest[i] in EXEC_VALUE else 1
    inner = rest[i + 1:]                               # after the container / service name
    if inner:
        analyze(shlex.join(inner), ctx.remote('container', 'a container', '/', c.out_safe), found)


def _flag(rest, short_letter, long_name):
    return long_name in rest or any(a.startswith('-') and not a.startswith('--') and short_letter in a[1:]
                                    for a in rest)


def r_docker(name, args, c, ctx, found):
    i = 0
    while i < len(args) and args[i].startswith('-'):
        i += 2 if args[i] in DOCKER_GLOBAL else 1
    args = args[i:]
    if name == 'docker-compose':
        return docker_compose(args, c, ctx, found)
    if not args:
        return
    sub, rest = args[0], args[1:]
    where = ctx.where
    if sub == 'compose':
        return docker_compose(rest, c, ctx, found)
    if sub in ('container', 'image', 'volume', 'network', 'system', 'builder', 'plugin'):
        nonopt = [a for a in rest if not a.startswith('-')]
        sub2 = nonopt[0] if nonopt else ''
        if sub == 'volume' and sub2 in ('rm', 'remove', 'prune'):
            return add(found, HIGH, 'docker-volume', f'docker volume {sub2} deletes docker volumes and their data {where}')
        if sub == 'system' and sub2 == 'prune':
            return add(found, HIGH if '--volumes' in rest else MEDIUM, 'docker-volume' if '--volumes' in rest else
                       'docker-remove', f'docker system prune {"incl. volumes " if "--volumes" in rest else ""}{where}')
        if sub == 'container' and sub2 in ('rm', 'remove', 'exec', 'run'):
            return r_docker(name, ['rm' if sub2 == 'remove' else sub2] + rest[rest.index(sub2) + 1:], c, ctx, found)
        if sub2 in ('rm', 'remove', 'prune', 'rmi'):
            return add(found, MEDIUM, 'docker-remove', f'docker {sub} {sub2} {where}')
        if sub2 in DOCKER_CHANGE:
            add(found, MEDIUM, 'docker-change', f'docker {sub} {sub2} {where}')
        return
    if sub == 'rm':
        if _flag(rest, 'v', '--volumes'):
            return add(found, HIGH, 'docker-volume', f'docker rm -v deletes the container\'s volumes {where}')
        return add(found, MEDIUM, 'docker-remove', f'docker rm removes a container {where} (its appdata stays)')
    if sub == 'exec':
        return _exec_inner(rest, ctx, c, found)
    if sub == 'run':
        add(found, MEDIUM, 'docker-change', f'docker run starts a new container {where}')
        k = next((j for j, a in enumerate(rest) if os.path.basename(a) in INNER_START or a.startswith('mkfs')), None)
        if k is not None:
            analyze(shlex.join(rest[k:]), ctx.remote('container', 'a container', '/', c.out_safe), found)
        return
    if sub in DOCKER_CHANGE:
        add(found, MEDIUM, 'docker-change', f'docker {sub} {short(" ".join(a for a in rest if not a.startswith("-")), 40)} {where}')


def docker_compose(args, c, ctx, found):
    i = 0
    while i < len(args) and args[i].startswith('-'):
        i += 2 if args[i] in COMPOSE_GLOBAL else 1
    if i >= len(args):
        return
    sub, rest, where = args[i], args[i + 1:], ctx.where
    if sub in ('down', 'rm'):
        if _flag(rest, 'v', '--volumes'):
            return add(found, HIGH, 'docker-volume', f'docker compose {sub} -v deletes the project\'s volumes {where}')
        return add(found, MEDIUM, 'docker-remove', f'docker compose {sub} {where}')
    if sub == 'exec':
        return _exec_inner(rest, ctx, c, found)
    if sub in ('up', 'start', 'stop', 'restart', 'kill', 'pause', 'unpause', 'create', 'pull', 'build', 'push',
               'scale', 'cp', 'run', 'watch'):
        add(found, MEDIUM, 'docker-change', f'docker compose {sub} {where}')


RULES = {
    'cd': r_cd, 'pushd': r_cd, 'export': r_export, 'declare': r_export, 'local': r_export, 'readonly': r_export,
    'typeset': r_export, 'rm': r_rm, 'unlink': r_unlink, 'find': r_find, 'shred': r_shred, 'truncate': r_truncate,
    'sed': r_sed, 'perl': r_perl, 'tee': r_tee, 'cp': r_cp, 'mv': r_mv, 'tar': r_tar, 'dd': r_dd, 'chmod': r_chmod,
    'chown': r_chmod, 'chgrp': r_chmod, 'mke2fs': r_disk, 'mkswap': r_disk, 'blkdiscard': r_disk, 'wipefs': r_disk,
    'fdisk': r_disk, 'gdisk': r_disk, 'cfdisk': r_disk, 'sgdisk': r_disk, 'sfdisk': r_disk, 'parted': r_disk,
    'cryptsetup': r_disk, 'hdparm': r_disk, 'xfs_repair': r_disk, 'zfs': r_zfs, 'zpool': r_zfs, 'btrfs': r_btrfs,
    'mdcmd': r_mdcmd, 'emcmd': r_emcmd, 'mover': r_mover, 'shutdown': r_power, 'reboot': r_power,
    'poweroff': r_power, 'halt': r_power, 'powerdown': r_power, 'init': r_power, 'telinit': r_power,
    'kill': r_service, 'pkill': r_service, 'killall': r_service, 'mount': r_service, 'umount': r_service,
    'systemctl': r_service, 'crontab': r_crontab, 'git': r_git, 'gh': r_gh, 'curl': r_curl, 'wget': r_curl,
    'tg': r_tg, 'sendmail': r_mail, 'msmtp': r_mail, 'mail': r_mail, 'mailx': r_mail, 'mutt': r_mail,
    'swaks': r_mail, 'vault': r_vault, 'proxy': r_proxy, 'risk-ok': r_riskok, 'ssh': r_ssh, 'scp': r_copy,
    'rsync': r_copy, 'sftp': r_copy, 'bash': r_shell, 'sh': r_shell, 'dash': r_shell, 'zsh': r_shell,
    'ksh': r_shell, 'ash': r_shell, 'eval': r_eval, 'docker': r_docker, 'docker-compose': r_docker,
}
# the owner's rights on his server (dispatcher v3): write verbs are MEDIUM (logged), through unraid-do, unraid-ssh run
# or a raw `ssh unraid VERB`; only the main session may pass his OK (--owner-ok / an ok-<12 hex> reference).
RIGHTS_WRITE = {'docker-restart', 'docker-update', 'vm-start', 'vm-stop', 'vm-create', 'userscript-run', 'appdata-put',
                'appdata-restore', 'plugin-update', 'iso-fetch'}   # iso-fetch: a new file in his ISO folder, no OK needed
OKREF_RE = re.compile(r'^ok-[0-9a-f]{12}$')


def _rights(verb, owner_ok, ctx, found, how):
    if verb not in RIGHTS_WRITE:
        return
    if owner_ok and ctx.agent:
        add(found, HIGH, 'owner-ok-subagent', "only the main session passes the owner's OK (his words from Telegram)")
    else:
        add(found, MEDIUM, 'unraid-rights', f'{how} {verb} changes the server (it is logged there: rights-log)')


def r_unraid_do(name, args, c, ctx, found):
    verb, owner_ok, i = None, False, 0
    while i < len(args):
        a = args[i]
        if a == '--dry-run':
            return
        if a in ('--owner-ok', '--file'):
            owner_ok = owner_ok or a == '--owner-ok'
            i += 2
            continue
        owner_ok = owner_ok or a.startswith('--owner-ok=')
        if verb is None and not a.startswith('-'):
            verb = a
        i += 1
    _rights(verb, owner_ok, ctx, found, 'unraid-do')


def r_unraid_ssh(name, args, c, ctx, found):
    i = 0
    while i < len(args) and args[i].startswith('--host'):
        i += 1 if '=' in args[i] else 2
    if args[i:i + 1] == ['run'] and len(args) > i + 1:
        _rights(args[i + 1], any(OKREF_RE.match(a) for a in args[i + 2:]), ctx, found, 'unraid-ssh run')


def r_rights_verb(name, args, c, ctx, found):         # `ssh unraid docker-update NAME ...`: the remote command
    _rights(name, any(OKREF_RE.match(a) for a in args), ctx, found, 'ssh')


RULES.update({'unraid-do': r_unraid_do, 'unraid-ssh': r_unraid_ssh}, **{v: r_rights_verb for v in RIGHTS_WRITE})
XARGS_VALUE = {'-n', '-L', '-P', '-d', '-s', '-E', '-a', '--max-args', '--max-lines', '--max-procs', '--delimiter',
               '--arg-file'}


def rule_command(argv, c, ctx, found):
    name, args = os.path.basename(argv[0]), argv[1:]
    if name == 'xargs':                                # ... | xargs rm -rf: the items are unknown paths
        i, repl = 0, None
        while i < len(args) and args[i].startswith('-'):
            a = args[i]
            if a == '-I':
                repl, i = (args[i + 1] if i + 1 < len(args) else None), i + 2
                continue
            if a.startswith('-I') or a.startswith('-i'):
                repl = a[2:] or '{}'
            i += 2 if a in XARGS_VALUE else 1
        inner = args[i:]
        if inner:
            inner = [x.replace(repl, '$__XARGS') for x in inner] if repl else inner + ['$__XARGS']
            rule_command(inner, c, ctx, found)
        return
    fn = RULES.get(name) or (r_disk if name.startswith('mkfs') else r_service if name.startswith('rc.') else None)
    if fn:
        fn(name, args, c, ctx, found)


# --- local LLM for the gray cases ---
LLM_SYSTEM = (
    'You rate the risk of ONE shell command an AI assistant is about to run for its owner. The assistant lives in a '
    'Docker container on the owner\'s Unraid home server; `ssh unraid ...` runs as root on that server (shares and '
    'appdata under /mnt, the flash drive at /boot). Reply with JSON only: {"risk":"low|medium|high","why":"<= 12 '
    'words"}. high = irreversible loss of the owner\'s data or server config: deleting shares, disks, appdata, '
    'docker volumes, backups, documents or media files; formatting; wiping a crontab; force-pushing main; sending '
    'credentials or messages to other people. medium = reversible: restarting or updating containers, editing a '
    'config that has a backup, deleting temporary, cache, log or helper files. low = read-only or harmless. If '
    'unsure between medium and high, answer high.')


def llm_enabled():
    return os.environ.get('RISK_CHECK_LLM', 'on') != 'off'


def _cache_file():
    return os.path.join(STATE, 'llm-cache.json')


def _load_cache():
    try:
        with open(_cache_file(), encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_cache(d):
    try:
        os.makedirs(STATE, mode=0o700, exist_ok=True)
        for k in sorted(d, key=lambda k: d[k][2])[:max(0, len(d) - 300)]:
            del d[k]
        tmp = f'{_cache_file()}.{os.getpid()}'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(d, f)
        os.replace(tmp, _cache_file())
    except (OSError, TypeError, IndexError, ValueError):
        pass


def ask_llm(cmd, whys):
    """('low'|'medium'|'high', why) from the local LLM, or None (off, timeout, error). Cached 7 days."""
    if not llm_enabled():
        return None
    key = hashlib.sha256(('v1\n' + cmd + '\n' + '; '.join(whys)).encode()).hexdigest()[:20]
    cache = _load_cache()
    hit = cache.get(key)
    if isinstance(hit, list) and len(hit) == 3 and time.time() - hit[2] < 7 * 86400:
        return hit[0], hit[1]
    prompt = f'Command:\n{redact(cmd)[:1500]}\n\nThe rule check is unsure about: {"; ".join(whys)[:300]}'
    argv = [os.environ.get('RISK_CHECK_LLM_CMD') or os.path.join(HOME, 'bin', 'llm-local'), '--json',
            '--max-tokens', '60', '--temp', '0', '--system', LLM_SYSTEM]
    try:
        p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             text=True, start_new_session=True)
    except OSError:
        return None
    try:
        out, _ = p.communicate(prompt, timeout=float(os.environ.get('RISK_CHECK_LLM_TIMEOUT') or 8))
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, 9)                        # the whole group: llm-local's own children too
        except OSError:
            pass
        try:
            p.communicate(timeout=1)
        except (subprocess.SubprocessError, OSError, ValueError):
            pass
        return None
    if p.returncode != 0:
        return None
    try:
        d = json.loads(out.strip().splitlines()[-1])
        risk = str(d.get('risk', '')).lower().strip()
    except (ValueError, IndexError, AttributeError):
        return None
    if risk not in ('low', 'medium', 'high'):
        return None
    why = short(d.get('why', ''), 120)
    cache[key] = [risk, why, time.time()]
    _save_cache(cache)
    return risk, why


# --- decision, OK codes, log ---
OVERRIDE = re.compile(r'^\s*(?:export\s+)?RISK_OK=([A-Za-z0-9]{4,32})(?:\s*(?:;|&&)\s*|\s+)')


def split_override(cmd):
    m = OVERRIDE.match(cmd)
    return (m.group(1), cmd[m.end():]) if m else (None, cmd)


def cmd_hash(cmd):
    return hashlib.sha256(' '.join(cmd.split()).encode()).hexdigest()[:10]


class Decision:
    def __init__(self, level, rule, why, h, code, llm, hang=None):
        self.level, self.rule, self.why, self.hash, self.code, self.llm = level, rule, why, h, code, llm
        self.hang, self.hang_why = hang or (None, None)   # (rule, why) when the rm would hang: see hang_check()


def decide(cmd, cwd=None, agent=None, use_llm=True):
    code, rest = split_override(cmd)
    found = []
    ctx = Ctx('local', cwd=cwd or HOME, agent=agent)
    analyze(rest, ctx, found)
    hang = ctx.hang[0] if ctx.hang else None
    use_llm = use_llm and not hang                     # denied anyway: no need to wait for the local LLM
    certain = [f for f in found if not f[3]]
    level = max((f[0] for f in certain), default=LOW)
    top = next((f for f in certain if f[0] == level), None)
    rule, why = (top[1], top[2]) if top else (None, '')
    llm = None
    amb =[f for f in found if f[3] and f[0] > level]
    if amb:
        a = max(amb, key=lambda f: f[0])
        verdict = ask_llm(rest, [f[2] for f in amb]) if use_llm else None
        if verdict:
            llm = verdict[0]
            lv = HIGH if verdict[0] == 'high' else MEDIUM  # the LLM may only choose medium or high
            if lv > level:
                level, rule, why = lv, a[1], f'{a[2]} (local LLM: {verdict[1] or verdict[0]})'
        else:
            llm = 'unavailable' if use_llm and llm_enabled() else 'off'
            level, rule, why = a[0], a[1], f'{a[2]} (local LLM {llm}: rule fallback)'
    return Decision(level, rule, why, cmd_hash(rest), code, llm, hang)


def ok_path(h):
    return os.path.join(STATE, f'ok-{h}')


def consume_ok(h, code):
    """The owner's OK text if `code` is the live one-time code for hash h (then it's used up), else None."""
    path = ok_path(h)
    try:
        with open(path, encoding='utf-8') as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or not hmac.compare_digest(str(d.get('code', '')), code):
        return None
    try:
        os.remove(path)
    except OSError:
        return None
    if time.time() - float(d.get('created', 0)) > OK_TTL:
        return None
    return short(d.get('reason', ''), 200)


REDACTIONS = (
    (re.compile(r'(?i)\b(x-api-key|api[-_]?key|apikey|access[-_]?token|token|passw(?:or)?d|secret|authorization)'
                r'(["\']?\s*[:=]\s*["\']?)(?:(?:bearer|token|basic)\s+)?[^\s"\'&]{4,}'), r'\1\2[redacted]'),
    (re.compile(r'(?i)\bbearer\s+[^\s"\']{8,}'), 'Bearer [redacted]'),
    (re.compile(r'(?<![\w-])(-u|--user)\s+\S+'), r'\1 [redacted]'),
    (re.compile(r'\b(?=[A-Za-z0-9_+-]*\d)(?=[A-Za-z0-9_+-]*[A-Za-z])[A-Za-z0-9_+-]{32,}\b'), '[redacted]'),
)


def redact(s):
    for rx, rep in REDACTIONS:
        s = rx.sub(rep, s)
    return s


def log_line(text):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        try:
            if os.path.getsize(LOG) > 2_000_000:
                os.replace(LOG, LOG + '.1')
        except OSError:
            pass
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write(f'{dt.datetime.now().isoformat(timespec="seconds")} {text}\n')
    except OSError:
        pass


def q(s, n):
    return '"' + short(s, n).replace('"', "'") + '"'


def log_decision(action, d, who, cmd, extra=''):
    parts = [NAMES[d.level], action, f'rule={d.rule or "-"}', f'hash={d.hash}', f'who={who}']
    if d.llm:
        parts.append(f'llm={d.llm}')
    if extra:
        parts.append(extra)
    log_line(' '.join(parts + [f'why={q(d.why, 200)}', f'cmd={q(redact(cmd), 300)}']))


REMEDY = {   # HIGH rules with a safe variant that needs no owner OK
    'secret-print': 'No OK needed if you avoid printing it: pipe through secretscan --redact, count with grep -c, '
                    'or print key names only.',
    'flash-config': 'Or make it reversible: copy the file to a .bak earlier in the same command (then it is MEDIUM).',
    'secrets-overwrite': 'Or keep the old one: copy it to a .bak earlier in the same command (then it is MEDIUM).',
}


def block_reason(d, agent):
    head = f'risk-check: HIGH — {d.why}.' + (' ' + REMEDY[d.rule] if d.rule in REMEDY else '')
    other = ' Otherwise ask' if d.rule in REMEDY else ' Ask'
    if agent:
        return head + (' You are a subagent: do not retry the same command or work around this; report the exact '
                       'command and this block to the main session, which asks the owner.')
    if d.code:
        return head + (f' RISK_OK={d.code} is not valid for this exact command (hash {d.hash}): unknown, used, '
                       f'expired (30 min) or the command changed. After the owner\'s OK on Telegram: risk-ok {d.hash} '
                       '"<his OK>" and rerun with the new code.')
    return head + (f'{other} the owner first on Telegram; if he OKs, run: risk-ok {d.hash} "<his OK, quoted>" '
                   'and rerun the exact command prefixed with the RISK_OK=<code> it prints (one use, 30 min).')


def hang_reason(d):
    also = (f' The command is also HIGH ({short(d.why, 120)}): the rewritten one will need the owner\'s OK.'
            if d.level == HIGH else '')
    return f'risk-check: {d.hang_why}.{also}'


# Tools whose tool_input.command is a shell command, rated alike (vault-guard and abx-raw-guard match the same pair).
# A Monitor call with `ws` instead of `command` runs no shell and passes.
HOOK_TOOLS = ('Bash', 'Monitor')


def hook():
    try:
        try:
            ev = json.loads(sys.stdin.read() or '{}')
        except ValueError:
            return 0
        if not isinstance(ev, dict) or ev.get('tool_name') not in HOOK_TOOLS:
            return 0
        if not isinstance(ev.get('tool_input'), dict):
            return 0
        cmd = ev['tool_input'].get('command')
        if not isinstance(cmd, str) or not PREFILTER.search(cmd):
            return 0
        agent = ev.get('agent_id') if isinstance(ev.get('agent_id'), str) else None
        cwd = ev.get('cwd') if isinstance(ev.get('cwd'), str) else None
        d = decide(cmd, cwd=cwd, agent=agent)
        who = f'agent:{agent[:12]}' if agent else 'main'
        if d.hang:                                     # before any level and any RISK_OK: Claude Code would stop it
            print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                                                     'permissionDecisionReason': hang_reason(d)}}))
            log_line(' '.join(['HANG', 'block', f'rule={d.hang}', f'hash={d.hash}', f'who={who}',
                               f'level={NAMES[d.level]}', f'why={q(d.hang_why, 200)}', f'cmd={q(redact(cmd), 300)}']))
            return 0
        if d.level == MEDIUM:
            log_decision('allow', d, who, cmd)
        elif d.level == HIGH:
            ok = consume_ok(d.hash, d.code) if d.code and not agent else None
            if ok is not None:
                log_decision('OVERRIDE', d, who, cmd, f'owner_ok={q(ok, 200)}')
                return 0
            print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                                                     'permissionDecisionReason': block_reason(d, agent)}}))
            log_decision('block', d, who, cmd)
        return 0
    except Exception as e:                             # fail OPEN: a broken safety net must not block every call
        tb = e.__traceback__
        while tb is not None and tb.tb_next is not None:
            tb = tb.tb_next
        log_line(f'ERROR {type(e).__name__} at line {tb.tb_lineno if tb else "?"}; call allowed (fail open)')
        return 0


USAGE = """risk-ok: owner-OK codes for commands the risk-check hook blocked (hook: ~/.claude/hooks/risk_check.py)
  risk-ok HASH "his OK, quoted + time"   one-time code for the blocked command with that hash (main session only,
                                        after the owner's yes on Telegram); rerun as: RISK_OK=<code> <same command>
  risk-ok check [--no-llm] "COMMAND"     dry run: level, rule, why, hash (nothing logged)
  risk-ok list                           pending codes
  risk-ok log [N]                        the last N medium/high decisions (default 20)
  risk-ok revoke HASH                    drop a pending code"""


def cli(argv):
    if not argv or argv[0] in ('-h', '--help', 'help'):
        print(USAGE)
        return 0
    verb = argv[0]
    if verb == 'check':
        rest = [a for a in argv[1:] if a != '--no-llm']
        command = ' '.join(rest) if rest else sys.stdin.read()
        d = decide(command, cwd=os.getcwd(), use_llm='--no-llm' not in argv)
        print(f'{NAMES[d.level]}  rule={d.rule or "-"}  hash={d.hash}' + (f'  llm={d.llm}' if d.llm else ''))
        if d.why:
            print(f'why: {d.why}')
        if d.hang:
            print(f'HANG (denied whatever the level)  rule={d.hang}\nwhy: {d.hang_why}')
        return 0
    if verb == 'list':
        try:
            names = sorted(n for n in os.listdir(STATE) if n.startswith('ok-'))
        except OSError:
            names = []
        for n in names:
            try:
                with open(os.path.join(STATE, n), encoding='utf-8') as f:
                    d = json.load(f)
                age = int(time.time() - float(d.get('created', 0)))
                state = 'expired' if age > OK_TTL else f'{(OK_TTL - age) // 60} min left'
                print(f'{n[3:]}  {state}  ok={q(d.get("reason", ""), 80)}')
            except (OSError, ValueError, TypeError):
                continue
        if not names:
            print('no pending codes')
        return 0
    if verb == 'log':
        n = int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else 20
        try:
            with open(LOG, encoding='utf-8') as f:
                print(''.join(f.readlines()[-n:] if n else []), end='')
        except OSError:
            print('no log yet')
        return 0
    if verb == 'revoke' and len(argv) > 1 and HASH_RE.match(argv[1]):
        try:
            os.remove(ok_path(argv[1]))
            print(f'revoked {argv[1]}')
        except OSError:
            print(f'no pending code for {argv[1]}')
        return 0
    if HASH_RE.match(verb):
        reason = ' '.join(argv[1:]).strip()
        if len(reason) < 3:
            print('say what the owner OK\'d: risk-ok HASH "<his words, time>"', file=sys.stderr)
            return 2
        code = secrets.token_hex(3)
        os.makedirs(STATE, mode=0o700, exist_ok=True)
        fd = os.open(ok_path(verb), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump({'code': code, 'reason': reason, 'created': time.time()}, f)
        log_line(f'OK-ISSUED hash={verb} owner_ok={q(reason, 200)}')
        print(f'RISK_OK={code}\nOne use, 30 min, only for the command with hash {verb}. Rerun it exactly as:\n'
              f'  RISK_OK={code} <the same command>')
        return 0
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(cli(sys.argv[1:]) if len(sys.argv) > 1 else hook())
