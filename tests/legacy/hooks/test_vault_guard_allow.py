#!/usr/bin/python3 -I
"""Allow-side tests for the vault-guard PreToolUse hook (vault_guard.py in HOOKS_DIR, default the passwd home's
~/.claude/hooks): normal commands, loops over literal directories and long-but-normal commands below the guard's
check limits must pass, in-process (decide) and through the hook command (vault-guard.sh, JSON on stdin). The
counterpart: loops over values the guard can't know stay unknown (denied where an unknown reaches a protected path).
Deny side: probe_caps.py next to this file (padding past the check limits and the closed open shapes must be denied,
its `allow:` cases must pass, every case fast). Run: python3 -I test_vault_guard_allow.py (hooks-tests/run.sh)."""
import atexit
import importlib.machinery
import importlib.util
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
HOOKS = os.environ.get('HOOKS_DIR') or os.path.join(pwd.getpwuid(os.getuid()).pw_dir, '.claude', 'hooks')
GUARD = os.path.join(HOOKS, 'vault_guard.py')
TMP = tempfile.mkdtemp(prefix='vg-allow-')
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
    loader = importlib.machinery.SourceFileLoader('vault_guard_allow', GUARD)
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader('vault_guard_allow', loader))
    loader.exec_module(m)
    # strictest trusted config (vault mode on, a synthetic profile); ~ read as the passwd home only (a test runner's
    # temp $HOME would add a reading in which ~/workspace doesn't exist)
    m.MODE_FILES, m.PROFILES, m.TILDE_HOMES = (MODE_ON,), (PROFILE,), m.HOMES
    return m


G = load()
H = G.HOME
W = H + '/workspace'
FAST = 4.0                                                # seconds per decision (the hook command's limit is 8)

NORMAL = [
    'echo ok',
    'ls -la ~/workspace && head -20 ~/workspace/TODO.md',
    'git -C ~/workspace status --short',
    'grep -rn "def main" ~/bin | head -5',
    'grep -rln TODO ~/workspace ~/opt',
    'rg -n foo ~/bin ~/opt --glob "*.py"',
    'find ~/opt -maxdepth 2 -name "*.py" -newer ~/workspace/TODO.md',
    'cd ~/opt && diff -u a/x.py b/x.py | head -50',
    'cd ~/workspace && grep -rn foo .',
    'python3 -I -m py_compile ~/bin/x.py && echo compiled',
    'tail -n 20 ~/.local/state/test-all/last.txt',
    'crontab -l | grep -c agent-watch',
    'T=$(mktemp -d) && cp ~/bin/x "$T"/ && ls "$T"',
    'cd "$T" && ls -la',
    'for f in ~/inbox/*.md; do head -3 "$f"; done',
    'wc -l ~/.claude/hooks/*.py',
    'echo "$HOME" && pwd',
]

LOOPS = [  # (command, cwd): loops over literal words, the variable read as each of them
    ('for d in bin opt; do grep -rn foo ~/$d | head -3; done', W),
    ('for d in bin opt workspace; do du -sh ~/$d; done', W),
    ('for d in bin opt; do (cd ~/$d && grep -rn foo .); done', W),
    ('for d in bin opt; do cd ~/$d && grep -rn foo .; cd -; done', W),
    ('for d in ~/bin ~/opt; do grep -rln foo "$d"; done', W),
    ('for d in bin opt; do grep -rn foo "$HOME/$d"; done', W),
    ('for d in "bin" \'opt\'; do grep -rn foo ~/"$d"; done', W),
    ('for d in bin opt; do grep -rn foo ${d}; done', H),
    ('for d in bin opt; do rg -n foo ~/$d; done', W),
    ('for d in bin opt; do find ~/$d -name "*.py" | xargs grep -n foo; done', W),
    ('for d in bin .claude/skills; do grep -rln TODO ~/$d; done', W),
    ('for s in hooks-tests test-all-tests; do bash ~/opt/$s/run.sh; done', W),
    ('for a in bin opt; do for b in x y; do ls ~/$a/$b; done; done', W),
    ('select d in bin opt; do ls ~/$d; break; done', W),
    ('for i in 1 2 3; do echo "$i"; done', W),
    ('for d in bin opt\ndo\n  grep -rn foo ~/$d\ndone', W),
]

LONG = [  # (label, command): far longer than real calls, still below every check limit
    ('5000 words', 'echo ' + ' '.join(['word'] * 5000)),
    ('1000 commands', '; '.join(f'grep -c foo ~/workspace/f{i}.md' for i in range(1000))),
    ('60 brace alternatives', 'ls ~/workspace/{' + ','.join(f'a{i}' for i in range(60)) + '}.md'),
    ('60 loop values', 'for d in ' + ' '.join(f'd{i}' for i in range(60)) + '; do ls ~/opt/$d; done'),
    ('1500 paths', 'ls ' + ' '.join(f'/tmp/x/p{i}' for i in range(1500))),
    ('300 sed expressions', 'sed -i ' + ' '.join(f"-e 's/a{i}/b{i}/'" for i in range(300)) + ' ~/workspace/x.md'),
    ('commit message, 400 lines', "git commit -q -F - <<'EOF'\n" +
     '\n'.join(f'- line {i}: tidy the docs' for i in range(400)) + '\nEOF'),
    ('python, 500 path literals', "python3 - <<'EOF'\nimport os\n" +
     ''.join(f"p{i} = '{W}/data/x{i}.json'\n" for i in range(500)) + 'print(open(p0).read())\nEOF'),
    ('python -c, 90 kB', 'python3 -c "' + 'x = 1\n' * 15000 + 'print(x)"'),
]

UNKNOWN = [  # (command, rule): a loop over values it can't know (or changed in the body) stays unknown
    ('for d in $DIRS; do grep -rn TOKEN ~/$d; done', 'telegram-token'),
    ('for d in $(cat /tmp/x/list); do grep -rn TOKEN ~/$d; done', 'telegram-token'),
    ('for d in "$@"; do grep -rn TOKEN ~/$d; done', 'telegram-token'),
    ('for d; do grep -rn TOKEN ~/$d; done', 'telegram-token'),
    ('for d in b*; do grep -rn TOKEN ~/$d; done', 'telegram-token'),
    ('for d in bin opt; do read -r d; grep -rn TOKEN ~/$d; done', 'telegram-token'),
    ('for d in bin opt; do d=.claude; grep -rn TOKEN ~/$d; done', 'telegram-token'),
    ('for d in bin opt; do sh -c "grep -rn TOKEN ~/$d"; done', 'telegram-token'),
    ('for a in ' + ' '.join(f'a{i}' for i in range(9)) + '; do for b in ' + ' '.join(f'b{i}' for i in range(9)) +
     '; do grep -rn TOKEN ~/$a$b; done; done', 'telegram-token'),            # 81 combinations: past the cap
    ('for d in bin .claude; do grep -rn TOKEN ~/$d; done', 'telegram-token'),  # literal, one of them protected
    ('for d in .claude; do cd ~/$d && grep -rn TOKEN .; done', 'telegram-token'),
    ('for f in x tgread.session; do cat ~/.secrets/$f; done', 'tgread-secrets'),
]


def bash(cmd, cwd=W):
    return {'tool_name': 'Bash', 'tool_input': {'command': cmd}, 'cwd': cwd}


class AllowInProcess(unittest.TestCase):
    def check_allow(self, cmd, cwd=W, label=None):
        t = time.time()
        r = G.decide(bash(cmd, cwd))
        took = time.time() - t
        self.assertIsNone(r, label or cmd)
        self.assertLess(took, FAST, label or cmd)

    def test_normal(self):
        for cmd in NORMAL:
            self.check_allow(cmd)

    def test_loops_over_literal_words(self):
        for cmd, cwd in LOOPS:
            self.check_allow(cmd, cwd)

    def test_long_but_normal(self):
        for label, cmd in LONG:
            self.check_allow(cmd, label=label)

    def test_unknown_loop_values_stay_unknown(self):
        for cmd, rule in UNKNOWN:
            r = G.decide(bash(cmd))
            self.assertEqual(r and r[0], rule, cmd[:120])


class AllowThroughHook(unittest.TestCase):
    """The real hook command: exit 0 and no output (= allow), well inside its 8 s limit."""

    def run_hook(self, cmd):
        t = time.time()
        p = subprocess.run([os.path.join(HOOKS, 'vault-guard.sh')], input=json.dumps(bash(cmd)), text=True,
                           capture_output=True, timeout=30, cwd=TMP)
        return p, time.time() - t

    def test_allow(self):
        cases = [NORMAL[0], LOOPS[0][0], LONG[0][1], LONG[1][1], LONG[8][1]]
        for cmd in cases:
            p, took = self.run_hook(cmd)
            self.assertEqual((p.returncode, p.stdout.strip()), (0, ''), (cmd[:80], p.stderr[-300:]))
            self.assertLess(took, 6.0, cmd[:80])


class ProbeCaps(unittest.TestCase):
    """probe_caps.py: every case denied except its `allow:` ones, and every decision fast."""

    def test_probe(self):
        p = subprocess.run([sys.executable, '-I', os.path.join(HERE, 'probe_caps.py'), GUARD], text=True,
                           capture_output=True, timeout=200, cwd=TMP)
        self.assertEqual(p.returncode, 0, p.stderr[-1000:])
        lines = [x for x in p.stdout.splitlines() if x.strip()]
        self.assertGreaterEqual(len(lines), 19)
        for line in lines:
            label, rest = line[:45].strip(), line[45:].split()
            decision, took = ' '.join(rest[:-1]), float(rest[-1].rstrip('s'))
            if label.startswith('allow:'):
                self.assertEqual(decision, 'None', line)
            else:
                self.assertNotEqual(decision, 'None', line)
                self.assertFalse(decision.startswith('EXC'), line)
            self.assertLess(took, FAST, line)


if __name__ == '__main__':
    unittest.main(verbosity=1)
