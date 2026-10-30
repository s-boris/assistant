#!/usr/bin/python3 -I
"""vault-guard's approvals-state rule (v2 P7; vault_guard.py in HOOKS_DIR, default the passwd home's ~/.claude/hooks),
table-driven: the approvals store and its -wal/-shm files x Bash forms (named, quoted, after cd, through variables,
interpreter code, Monitor) and the writing file tools, each as a simulated PreToolUse event in a main session and in a
subagent; plus the allow side (the approvals CLI, listing the state directory, Read, docs and code that name the store,
the Grep tool). Hook events only: no file is ever opened. Run: python3 -I test_vault_guard_approvals.py (run.sh)."""
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
TMP = tempfile.mkdtemp(prefix='vg-approvals-')
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
    loader = importlib.machinery.SourceFileLoader('vault_guard_approvals', GUARD)
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader('vault_guard_approvals', loader))
    loader.exec_module(m)
    m.MODE_FILES, m.PROFILES, m.TILDE_HOMES = (MODE_ON,), (PROFILE,), m.HOMES
    return m


G = load()
H = G.HOME
W = H + '/workspace'
S = H + '/.local/state/assistant'
FAST = 4.0
RULE = 'approvals-state'
SESSIONS = {'main': None, 'subagent': 'a0test'}
NAME = 'approvals' + '.db'
FILES = [NAME, NAME + '-wal', NAME + '-shm']

# {p} ~ path, {a} absolute path, {n} file name
BASH = ['sqlite3 {p} "UPDATE approvals SET status=\'approved\'"', 'sqlite3 {a} .dump', 'cat {p} | base64',
        'cp /tmp/x {p}', 'rm -f {a}', 'echo x > {p}', 'mv /tmp/y {a}', 'cd ~/.local/state/assistant && sqlite3 {n}',
        'cd ~/.local/state && cp /tmp/x assistant/{n}', 'python3 -c "import sqlite3; sqlite3.connect(\'{a}\')"',
        "sqlite3 '{a}'", 'sqlite3 "$HOME/.local/state/assistant/{n}"', 'truncate -s 0 {p}']
DENY_EXTRA = [  # (command, cwd): pieces the shell puts together, a cwd inside the state directory, any approvals.db
    ('F=approvals; sqlite3 ~/.local/state/assistant/$F.db', W),
    ('D=~/.local/state/assistant; N=approvals; sqlite3 "$D/$N.db"', W),
    ("sqlite3 ~/.local/state/assistant/appr''ovals.db", W),
    ('sqlite3 ' + NAME, S),
    ('sqlite3 /tmp/elsewhere/' + NAME, W),
    ('ls ~/.local/state/assistant/approvals*', W),
]
ALLOW_BASH = [
    'approvals list', 'approvals status ap:0123:y', 'approvals consume 0123 --action x --json', '~/bin/approvals audit',
    'approvals new --action "webagent:wa1" --summary "Pay"', 'ls ~/.local/state/assistant', 'ls -la ' + S + '/',
    'du -sh ~/.local/state/assistant/*', 'events list', 'tg-adapter status --json', 'echo ok',
    'grep -rn approvals ~/opt/assistant/packages | head -3', 'cd ~/opt/assistant && git status --short',
    'python3 -c "print(1)"', 'webagent approve ap:0123:y',
]
ALLOW_TOOLS = [
    ('Read', {'file_path': S + '/' + NAME}),
    ('Edit', {'file_path': W + '/TOOLS.md', 'old_string': 'x', 'new_string': 'store: ~/.local/state/assistant/' + NAME}),
    ('Write', {'file_path': H + '/opt/assistant/schemas/approvals.sql', 'content': '-- ' + NAME}),
    ('Grep', {'pattern': 'approvals\\.db', 'path': H + '/opt/assistant'}),
    ('Glob', {'pattern': '*.db', 'path': S}),
    ('Write', {'file_path': S + '/events.db-note', 'content': 'x'}),
]


def ev(tool, ti, agent=None, cwd=W):
    e = {'tool_name': tool, 'tool_input': ti, 'cwd': cwd}
    if agent:
        e['agent_id'] = agent
    return e


def bash(cmd, agent=None, cwd=W):
    return ev('Bash', {'command': cmd}, agent, cwd)


def deny_cases():
    for n in FILES:
        fmt = dict(p='~/.local/state/assistant/' + n, a=S + '/' + n, n=n)
        for t in BASH:
            cmd = t.format(**fmt)
            yield f'bash: {cmd}', bash(cmd)
        for tool, ti in (('Write', {'file_path': fmt['a'], 'content': 'x'}),
                         ('Edit', {'file_path': fmt['a'], 'old_string': 'x', 'new_string': 'y'}),
                         ('MultiEdit', {'file_path': fmt['a'], 'edits': []}),
                         ('NotebookEdit', {'notebook_path': fmt['a']}),
                         ('Write', {'file_path': fmt['p'], 'content': 'x'})):
            yield f'{tool} {ti.get("file_path") or ti.get("notebook_path")}', ev(tool, ti)
        yield f'Write relative {n}', ev('Write', {'file_path': n, 'content': 'x'}, cwd=S)
    for cmd, cwd in DENY_EXTRA:
        yield f'extra: {cmd} (cwd {cwd})', bash(cmd, cwd=cwd)
    yield 'Monitor', ev('Monitor', {'command': 'tail -f ~/.local/state/assistant/' + NAME + '-wal'})


class Approvals(unittest.TestCase):
    def verdict(self, event, label):
        t = time.time()
        r = G.decide(event)
        self.assertLess(time.time() - t, FAST, label)
        return r and r[0]

    def test_deny_every_session(self):
        n = 0
        for label, event in deny_cases():
            for s, agent in SESSIONS.items():
                e = dict(event, agent_id=agent) if agent else event
                with self.subTest(session=s, case=label):
                    self.assertEqual(self.verdict(e, label), RULE)
                n += 1
        self.assertGreater(n, 100)

    def test_allow_main_and_subagent(self):
        for s, agent in SESSIONS.items():
            for cmd in ALLOW_BASH:
                with self.subTest(session=s, cmd=cmd):
                    self.assertIsNone(self.verdict(bash(cmd, agent), cmd))
            for tool, ti in ALLOW_TOOLS:
                with self.subTest(session=s, tool=tool, ti=ti):
                    self.assertIsNone(self.verdict(ev(tool, ti, agent), tool))

    def test_an_earlier_rule_keeps_its_name(self):
        cmd = 'cat ~/.secrets/passport.json ~/.local/state/assistant/' + NAME
        self.assertEqual(self.verdict(bash(cmd), cmd), 'personal-data')


class ThroughHook(unittest.TestCase):
    """The real hook command: deny JSON naming the rule, and an allow with no output."""

    def run_hook(self, event):
        p = subprocess.run([os.path.join(HOOKS, 'vault-guard.sh')], input=json.dumps(event), text=True,
                           capture_output=True, timeout=30, cwd=TMP)
        return p.returncode, p.stdout.strip()

    def test_hook(self):
        rc, out = self.run_hook(bash('sqlite3 ~/.local/state/assistant/' + NAME + ' .dump'))
        self.assertEqual(rc, 0)
        d = json.loads(out)['hookSpecificOutput']
        self.assertEqual(d['permissionDecision'], 'deny')
        self.assertIn('vault-guard (approvals-state)', d['permissionDecisionReason'])
        self.assertEqual(self.run_hook(bash('approvals list')), (0, ''))


if __name__ == '__main__':
    unittest.main(verbosity=1)
