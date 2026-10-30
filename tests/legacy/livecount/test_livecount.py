"""Offline tests for livecount: stub phone (logs argv, exit code + JSON status from files), temp state dir, the
repo's phonegw lint (adapters/phone) for parity. Nothing reaches the gateway."""
import importlib.machinery
import importlib.util
import json
import os
import re
import sys
import tempfile
import time
import unittest

LIB = os.environ.get('LIVECOUNT_LIB') or os.path.expanduser('~/bin/lib/livecount.py')
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
PGW_LINT = os.environ.get('PHONEGW_LINT') or os.path.join(REPO, 'adapters/phone/phonegw/lint.py')  # the tree's gateway
os.environ.setdefault('PHONEGW_FILTER', os.path.join(REPO, 'adapters/voice/voice_relay/answer_filter.py'))
TAG_RE = re.compile(r'^[a-z][a-z0-9_:.-]{0,47}$')          # the push schema's tag pattern
STUB = """#!/bin/sh
echo "$*" >> "$PH_LOG"
sleep "$(cat "$PH_SLEEP" 2>/dev/null || echo 0)"
rc=$(cat "$PH_RC" 2>/dev/null || echo 0)
st=$(cat "$PH_STATUS" 2>/dev/null || echo sent)
echo "{\\"ok\\": true, \\"status\\": \\"$st\\"}"
exit $rc
"""


def load(path, name):
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class LC(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.f = {k: os.path.join(d, k) for k in ('log', 'rc', 'status', 'sleep')}
        stub = os.path.join(d, 'phone')
        with open(stub, 'w') as fh:
            fh.write(STUB)
        os.chmod(stub, 0o755)
        self.old = dict(os.environ)
        os.environ.update(LIVECOUNT_PHONE=stub, LIVECOUNT_DIR=os.path.join(d, 'live'), PH_LOG=self.f['log'],
                          PH_RC=self.f['rc'], PH_STATUS=self.f['status'], PH_SLEEP=self.f['sleep'])
        os.environ.pop('LIVECOUNT', None)
        self.lc = load(LIB, 'livecount_t')                 # fresh module: its 5 s budget starts at 0
        self.now = 1_900_000_000

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.old)
        self.tmp.cleanup()

    def put(self, k, v):
        with open(self.f[k], 'w') as fh:
            fh.write(str(v))

    def calls(self):
        try:
            with open(self.f['log']) as fh:
                return fh.read().splitlines()
        except OSError:
            return []

    def item(self, key='a', mins=20, title='Train 16:31', body='S5 · platform 1'):
        return {'key': key, 'until': self.now + mins * 60, 'title': title, 'body': body}

    def test_push_args_ttl_and_tag(self):
        acts = self.lc.sync('train', [self.item()], now=self.now)
        (c,) = self.calls()
        tag = self.lc.tag_for('train', 'a')
        self.assertRegex(tag, TAG_RE)
        self.assertEqual(c, 'push --level info --title Train 16:31 --body S5 · platform 1 --tag %s --live countdown:%d '
                            '--ttl %ds --json' % (tag, self.now + 1200, 1200 + 300))
        self.assertEqual(acts, ['live: start %s' % tag])
        st = json.load(open(os.path.join(os.environ['LIVECOUNT_DIR'], 'train.json')))
        self.assertEqual(oct(os.stat(os.path.join(os.environ['LIVECOUNT_DIR'], 'train.json')).st_mode & 0o777), '0o600')
        self.assertEqual(st['chips'][tag]['until'], self.now + 1200)

    def test_unchanged_no_call_changed_updates_same_tag(self):
        self.lc.sync('train', [self.item()], now=self.now)
        self.lc.sync('train', [self.item()], now=self.now + 60)
        self.assertEqual(len(self.calls()), 1)
        self.lc.sync('train', [self.item(mins=25, title='Train 16:31 +5 min')], now=self.now + 120)
        c = self.calls()
        self.assertEqual(len(c), 2)
        self.assertIn('--tag %s ' % self.lc.tag_for('train', 'a'), c[1])

    def test_unlisted_chip_is_cancelled_or_held(self):
        self.lc.sync('drive', [self.item()], now=self.now)
        self.lc.sync('drive', [], now=self.now + 60)
        self.assertEqual(self.calls()[-1], 'cancel %s --json' % self.lc.tag_for('drive', 'a'))
        self.lc.sync('train', [self.item(key='b')], now=self.now)
        n = len(self.calls())
        self.lc.sync('train', [], now=self.now + 60, hold=True)
        self.assertEqual(len(self.calls()), n, 'held until its time')
        self.lc.sync('train', [], now=self.now + 60, hold=True, ended=['b'])
        self.assertEqual(self.calls()[-1], 'cancel %s --json' % self.lc.tag_for('train', 'b'))
        self.lc.sync('train', [self.item(key='c')], now=self.now)
        self.lc.sync('train', [], now=self.now + 20 * 60 + 200, hold=True)
        self.assertEqual(self.calls()[-1], 'cancel %s --json' % self.lc.tag_for('train', 'c'), 'past until + grace')
        self.assertFalse(os.path.exists(os.path.join(os.environ['LIVECOUNT_DIR'], 'train.json')), 'empty state removed')

    def test_no_device_backs_off_queued_does_not(self):
        self.put('rc', 4)
        self.put('status', 'no_device')
        self.assertEqual(self.lc.sync('flight', [self.item(), self.item(key='b')], now=self.now),
                         ['live: no device (no_device), back off'])
        self.lc.sync('flight', [self.item()], now=self.now + 300)
        self.assertEqual(len(self.calls()), 1, 'one call, then quiet for 10 min')
        self.lc.sync('flight', [self.item()], now=self.now + 601)
        self.assertEqual(len(self.calls()), 2)
        self.put('status', 'queued')
        lc2 = load(LIB, 'livecount_t2')
        lc2.sync('lastcall', [self.item()], now=self.now)
        lc2.sync('lastcall', [self.item()], now=self.now + 60)
        self.assertEqual(len(self.calls()), 3, 'queued for an offline phone counts as sent')

    def test_gateway_down_backs_off(self):
        self.put('rc', 7)
        self.lc.sync('train', [self.item()], now=self.now)
        self.lc.sync('train', [self.item()], now=self.now + 120)
        self.assertEqual(len(self.calls()), 1)

    def test_lint_skips_or_drops_body(self):
        acts = self.lc.sync('train', [self.item(title='Train 12345'), self.item(key='b', body='see https://x.example'),
                                      self.item(key='c', title='T' * 41)], now=self.now)
        c = self.calls()
        self.assertEqual(len(c), 1, c)
        self.assertIn('--title Train 16:31 --body  --tag', c[0])          # body dropped, chip kept
        self.assertEqual(sum('lint' in a for a in acts), 2, acts)

    def test_lint_parity_with_the_gateway(self):
        pg = load(PGW_LINT, 'pgw_lint')
        good = [('Last train 00:08', 'S9 · the last way home tonight'), ('Last train 01:12', 'IR 36 → S9 · the last way '
                'home tonight'), ('Train 16:31 +6 min', 'S5 · platform 41'), ('Bus 23:58', 'Bus 21'),
                ('Leave 16:20', 'B7 → S5'), ('Leave by 20:15', 'Traffic: leave 15 min earlier'),
                ('Leave by 18:05', 'Light traffic: 4 min later'), ('Leave by 07:40', 'Traffic as usual'),
                ('Flight 21:35 +20 min', 'terminal 1 · gate B42'), ('Flight 07:50', 'gate not out yet')]
        bad = [('Train 12345', ''), ('Flight 21:35', 'pay CHF 40 at the desk'), ('Leave by 20:15', 'www.example.com'),
               ('Last train 00:08', 'call +41 44 123 45 67'), ('L' * 41, ''), ('Train 16:31', 'x' * 81)]
        for t, b in good:
            self.assertEqual((self.lc.lint(t, b), pg.lint(t, b, True)[2]), ([], []), (t, b))
        for t, b in bad:
            self.assertTrue(self.lc.lint(t, b) and pg.lint(t, b, True)[2], (t, b, self.lc.lint(t, b)))

    def test_slow_phone_capped_at_5s(self):
        self.put('sleep', 10)
        t0 = time.monotonic()
        self.lc.sync('train', [self.item(key=k) for k in 'abc'], now=self.now)
        self.assertLess(time.monotonic() - t0, 5.8)
        self.assertEqual(len(self.calls()), 1, 'the first call times out (4 s), the rest are skipped (budget)')

    def test_off_test_mode_dry_and_missing_binary(self):
        os.environ['LIVECOUNT'] = 'off'
        self.assertEqual(self.lc.sync('train', [self.item()], now=self.now), [])
        os.environ.pop('LIVECOUNT')
        stub = os.environ.pop('LIVECOUNT_PHONE')
        self.assertEqual(self.lc.sync('train', [self.item()], now=self.now, test=True), [])
        self.assertIn('live: start chip "Train 16:31" (S5 · platform 1)',
                      self.lc.sync('train', [self.item()], now=self.now, test=True, dry=True)[0])
        self.assertFalse(os.path.exists(os.environ['LIVECOUNT_DIR']))
        os.environ['LIVECOUNT_PHONE'] = stub + '-missing'
        self.assertEqual(self.lc.sync('train', [self.item()], now=self.now), [])
        self.assertEqual(self.calls(), [])

    def test_never_raises(self):
        self.assertEqual(self.lc.sync('train', [{'key': 'a', 'until': 'soon', 'title': 'x'}], now=self.now),
                         ['live: error ValueError'])
        self.assertEqual(self.lc.sync('train', [], now=self.now), [])      # nothing live: no state, no call
        self.assertEqual(self.calls(), [])


if __name__ == '__main__':
    unittest.main(verbosity=1)
