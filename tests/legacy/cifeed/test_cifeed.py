#!/usr/bin/env python3
"""cifeed tests (the dashboard CI tab's data: land queue, lands, nightly, hub runner, headline, legacy suites, deploys). Offline: a temp
state dir, a fake /proc, a pinned clock. CIFEED_LIB=/dir or CIFEED_SRC=/path/cifeed.py tests another copy (default
~/bin/lib)."""
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile

LIB = os.environ.get('CIFEED_LIB', os.path.expanduser('~/bin/lib'))
FAILS, COUNT = [], [0]


def ok(cond, name):
    COUNT[0] += 1
    if not cond:
        FAILS.append(name)
        print('FAIL', name)


T = tempfile.mkdtemp(prefix='cifeed-tests-')
ST, PROC = os.path.join(T, 'state'), os.path.join(T, 'proc')
os.makedirs(os.path.join(ST, 'deploy.queue'))
os.makedirs(os.path.join(ST, 'ci-signed'))
os.makedirs(PROC)
REPO = os.path.join(T, 'repo')   # a throwaway git repo for the lands' commit subjects (desc)
os.environ.update(CIFEED_STATE=ST, CIFEED_PROC=PROC, CIFEED_GATE=os.path.join(T, 'gate'),
                  CIFEED_CRONTAB=os.path.join(T, 'crontab'), CIFEED_TZ='Europe/Zurich', CIFEED_REPO=REPO)
src = os.environ.get('CIFEED_SRC') or os.path.join(LIB, 'cifeed.py')   # CIFEED_SRC: CI's staged copy (the manifest seam)
if not os.path.exists(src) and os.path.exists(src + '.new'):
    src += '.new'
spec = importlib.util.spec_from_loader('cifeed', importlib.machinery.SourceFileLoader('cifeed', src))
C = importlib.util.module_from_spec(spec)
spec.loader.exec_module(C)

NOW = 1793206800.0            # 28.10.2026 18:00 Zurich
CUT = 1793204181.0            # the speed-up's land row


def proc(pid, argv, start='5000', state='S'):
    d = os.path.join(PROC, str(pid))
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, 'stat'), 'w') as f:
        f.write(f'{pid} (python3) {state} 1 ' + ' '.join(['0'] * 17) + f' {start} 0 0\n')
    with open(os.path.join(d, 'cmdline'), 'wb') as f:
        f.write(b'\0'.join(a.encode() for a in argv) + b'\0')


def wj(path, obj):
    with open(path, 'w') as f:
        json.dump(obj, f)


def git(*a):
    return subprocess.run(['git', '-C', REPO, '-c', 'user.name=t', '-c', 'user.email=t@example.invalid', *a],
                          capture_output=True, text=True, check=True).stdout.strip()


def commit(subject):
    git('commit', '-q', '--allow-empty', '-m', subject)
    return git('rev-parse', 'HEAD')


def deploys(rows):
    with open(os.path.join(ST, 'deploys.jsonl'), 'w') as f:
        for r in rows:
            f.write(json.dumps(r) + '\n')


try:
    # empty: nothing invented
    s = C.snapshot(NOW)
    ok(s['queue'] == {'holder': None, 'waiting': [], 'starting': []} and s['lands'] == [], 'empty: queue free, no lands')
    ok(s['headline']['today'] == {'n': 0, 'green': 0, 'share': None, 'median_s': None}, 'empty: no rate, no median')
    tl = s['timeline']
    ok(len(tl['hours']) == 24 and not any(x['green'] or x['red'] for x in tl['hours']) and tl['marks'] == []
       and tl['at'] == '2026-10-27T19:00:00+01:00' and tl['hours'][-1]['at'] == '2026-10-28T18:00:00+01:00',
       'empty: a 24-hour timeline by local hour, no lands, no markers')
    ok(s['nightly'] == {'scheduled': False, 'next': None, 'last': None} and s['runner']['record'] is None
       and s['runner']['gate'] == 'shadow', 'empty: nightly unscheduled, no runner record, gate default shadow')
    ok(s['legacy'] == {'suites': 0, 'red_n': 0, 'red': [], 'last': None, 'skipped': None, 'runs_7d': 0, 'green_7d': 0,
                       'grid': {'runs': [], 'rows': [], 'green': []}}
       and s['deploys']['recent'] == [] and s['deploys']['core']['release'] is None and s['deploys']['hub']['release'] is None,
       'empty: no legacy state, no deploys')

    green_new = {'op': 'land', 'ok': True, 'branch': 'one-pkg', 'release': 'aaaaaaaaaaaa1234', 'land_start': NOW - 900,
                 'end': NOW - 600, 'ts': NOW - 600, 'selection': 'impacted',
                 'gate': {'ci_local_secs': 126.0, 'effective': 'shadow'},
                 'runner': {'secs': 265.0, 'ok': True, 'signed_green': True, 'state': 'done'},
                 'tests': {'local pytest-3.11': {'files': 5, 'of': 137, 'tests': 76},
                           'local pytest-3.13': {'files': 5, 'of': 137, 'tests': 76}},
                 'rerun': {'local pytest-3.11': {'ids': ['tests/x.py::test_zzrerun_marker'], 'ok': True, 'still': []},
                           'local pytest-3.13': {'ids': ['tests/x.py::test_zzrerun_marker'], 'ok': True, 'still': []}},
                 'quarantined': {'runner': ['tests/y.py::test_zzq_marker']}}
    rows = [
        {'op': 'land', 'ok': True, 'branch': 'old-a', 'land_start': CUT - 4000, 'end': CUT - 3600, 'ts': CUT - 3600},
        {'op': 'land', 'ok': False, 'branch': 'old-b', 'why': 'ci', 'ts': CUT - 3000},
        {'op': 'land', 'ok': True, 'branch': 'land-speed', 'release': '696a6a7101978e90', 'land_start': CUT - 300,
         'end': CUT, 'ts': CUT},
        green_new,
        {'op': 'land', 'ok': False, 'branch': 'gated-x', 'why': 'no signed record', 'ts': NOW - 500,
         'gate': {'ci_local_secs': 431.9, 'runner': {'failed': ['pytest-3.13'], 'secs': 279.6, 'ok': False}}},
        {'op': 'land', 'ok': False, 'branch': 'conf', 'why': 'rebase conflict', 'ts': NOW - 400},
        {'op': 'land', 'ok': False, 'branch': 'leg', 'why': 'known red on main', 'suites': ['dash'], 'ts': NOW - 300},
        {'op': 'land', 'ok': False, 'branch': '<b>x</b> y;$(z)', 'why': 'ci', 'ts': NOW - 200},
        {'op': 'deploy', 'ok': True, 'ts': NOW - 100},
        {'op': 'ci-nightly', 'ok': False, 'rc': 6, 'head': 'bbbbbbbbbbbbcc', 'secs': 410, 'red': ['pytest-3.13', 'legacy'],
         'ts': NOW - 50},
    ]
    deploys(rows)
    with open(os.path.join(T, 'crontab'), 'w') as f:
        f.write('# 10 2 * * * old\n10 2 * * * nice -n 10 cron-fail ci-nightly /x/assistant ci-nightly >/dev/null 2>&1\n')
    with open(os.path.join(T, 'gate'), 'w') as f:
        f.write('enforce-gated\n')
    s = C.snapshot(NOW)
    L = s['lands']
    ok([x['branch'] for x in L][:3] == ['bx/byz', 'leg', 'conf'] and len(L) == 8, 'lands: newest first, only land rows')
    g = next(x for x in L if x['branch'] == 'one-pkg')
    ok(g['ok'] and g['total_s'] == 300 and g['local_s'] == 126.0 and g['runner_s'] == 265.0 and g['runner'] == 'signed'
       and g['sel'] == 'impacted' and (g['files'], g['of'], g['tests']) == (5, 137, 76), f'lands: green row fields ({g})')
    ok(g['reruns'] == 1 and g['rerun_ok'] is True and g['quarantined'] == 1 and g['failed'] == [],
       'lands: the same rerun id in both suites counts once; quarantine counted')
    fails = {x['branch']: x['failed'] for x in L if not x['ok']}
    ok(fails.get('gated-x') == ['runner pytest-3.13'] and fails.get('conf') == ['rebase conflict']
       and fails.get('leg') == ['suite dash'] and fails.get('bx/byz') == ['local CI'], f'lands: failed step per red row ({fails})')
    red = next(x for x in L if x['branch'] == 'gated-x')
    ok(red['total_s'] is None and red['local_s'] == 431.9 and red['runner_s'] == 279.6 and red['runner'] == 'red',
       'lands: a red row has no total, keeps local/runner seconds from the gate')
    dump = json.dumps(s)
    ok(not __import__('re').search(r'\b\d{10}\b', dump) and s['now'] == '2026-10-28T18:00:00+01:00' and L[0]['t'].startswith('2026-10-28T'),
       'times: local ISO strings, no bare epochs')
    ok('zzrerun_marker' not in dump and 'zzq_marker' not in dump and '<' not in dump and '$(' not in dump and ';' not in dump,
       'privacy: no test ids, markup or shell text in the snapshot')

    # a land's time is the whole `assistant land` (land_cmd_start -> end); land_start -> end is only merge + deploy.
    # Its timeline from the command's start: local CI and the runner side by side, the runner's queue, the lock
    # wait, merge + deploy; exact with the row's wall times, else the CI from the start.
    full = {'op': 'land', 'ok': True, 'branch': 'steps-x', 'land_cmd_start': NOW - 400, 'land_start': NOW - 62, 'end': NOW - 50,
            'ts': NOW - 50, 'lock_wait_secs': 30.0, 'verify': {'tries': 1},
            'gate': {'ci_local_secs': 300.0, 'ci_local_at': NOW - 395,
                     'ci_local_steps': {'sync': 3.0, 'pytest-3.13': 120.44, 'legacy': 250.25, '<b>x</b>': 1, 'bad': 'no'}},
            'runner': {'secs': 200.0, 'created': NOW - 394, 'started': NOW - 390, 'ended': NOW - 190, 'signed_green': True,
                       'steps': {'pytest-3.13': 191.08, 'legacy': 168.65, 'pyright': 29.12, 'ruff': 0.12, 'bun': 0.0}}}
    f = C.land(full)
    ok(f['total_s'] == 350 and f['merge_s'] == 12 and f['wait_s'] == 30 and f['queue_s'] == 4,
       f"land timeline: whole land, merge + deploy, lock wait, runner queue ({f['total_s']}, {f['merge_s']}, {f['wait_s']}, {f['queue_s']})")
    ok(f['tl'] == {'span': 350, 'segs': [['local', 5, 300], ['queue', 6, 4], ['runner', 10, 200], ['lock', 308, 30], ['merge', 338, 12]]},
       f"land timeline: exact segments from the row's wall times ({f['tl']})")
    ok(f['steps']['runner'] == [['pytest-3.13', 191.1], ['legacy', 168.7], ['pyright', 29.1], ['ruff', 0.1], ['bun', 0.0]]
       and f['steps']['local'] == [['legacy', 250.2], ['pytest-3.13', 120.4], ['sync', 3.0], ['bx/b', 1.0]],
       f"land steps: runner and local steps, slowest first, plain names, numbers only ({f['steps']})")
    pa = C.land({'op': 'land', 'ok': True, 'branch': 'pub-audit', 'land_cmd_start': NOW - 352.7, 'land_start': NOW - 11.9, 'end': NOW,
                 'lock_wait_secs': 0.0, 'verify': {'tries': 1}, 'gate': {'ci_local_secs': 340.6},
                 'runner': {'secs': 208.36, 'steps': {'pytest-3.13': 191.08}}})
    ok(pa['total_s'] == 353 and pa['merge_s'] == 12 and pa['queue_s'] is None and pa['steps']['local'] == []
       and pa['tl'] == {'span': 353, 'segs': [['local', 0, 340.6], ['runner', 0, 208.4], ['merge', 341, 12]]},
       f"land timeline: a row without wall times: 353 s, not the 12 s merge; the CI from the start ({pa['total_s']}, {pa['tl']})")
    again = C.land({'op': 'land', 'ok': True, 'branch': 'moved', 'land_cmd_start': NOW - 600, 'land_start': NOW - 10, 'end': NOW,
                    'lock_wait_secs': 5.0, 'verify': {'tries': 2}, 'gate': {'ci_local_secs': 100.0}, 'runner': {'secs': 150.0}})
    ok(again['tl']['segs'] == [['local', 435, 100], ['runner', 435, 150], ['lock', 585, 5], ['merge', 590, 10]],
       f"land timeline: main moved (2 passes): the last pass's CI just before the lock ({again['tl']})")
    old_r = C.land({'op': 'land', 'ok': True, 'branch': 'old', 'land_start': NOW - 300, 'end': NOW, 'gate': {'ci_local_secs': 120.0}})
    ok(old_r['total_s'] == 300 and old_r['merge_s'] is None and old_r['tl'] == {'span': 300, 'segs': [['local', 0, 120]]},
       f"land timeline: a row before land_cmd_start: land_start covered the whole land ({old_r['tl']})")
    red_r = next(x for x in L if x['branch'] == 'gated-x')
    ok(red_r['tl'] == {'span': 431.9, 'segs': [['local', 0, 431.9], ['runner', 0, 279.6]]} and red_r['merge_s'] is None
       and red_r['wait_s'] is None, f"land timeline: a red land: the CI lanes only ({red_r['tl']})")
    ok(C._rate([f, pa, old_r])['median_s'] == 350, 'headline: the median land is the whole land')

    tl = s['timeline']
    by = {x['at'][11:16]: (x['green'], x['red']) for x in tl['hours']}
    ok(by['16:00'] == (1, 1) and by['17:00'] == (2, 4) and by['18:00'] == (0, 0) and sum(g + r for g, r in by.values()) == 8,
       f'timeline: green and red lands per local hour ({[(k, v) for k, v in by.items() if v != (0, 0)]})')
    ok(tl['marks'] == [{'t': C.iso(NOW - 100), 'kind': 'deploy', 'ok': True, 'target': None},
                       {'t': C.iso(NOW - 50), 'kind': 'nightly', 'ok': False, 'target': None}],
       f"timeline: deploy and nightly markers ({tl['marks']})")

    H = s['headline']
    ok(H['speedup']['at'] == C.iso(CUT), 'headline: the cut is the speed-up land row')
    ok(H['after']['n'] == 5 and H['after']['green'] == 1 and H['after']['median_s'] == 300,
       f"headline: after = lands after the speed-up's own land ({H['after']})")
    ok(H['before']['n'] == 2 and H['before']['green'] == 1 and H['before']['median_s'] == 400,
       f"headline: before = the 7 d before the cut ({H['before']})")
    ok(H['week']['n'] == 8 and H['today']['n'] == 8 and H['week']['share'] == round(3 / 8, 3), 'headline: today and 7 d rates')

    N = s['nightly']
    ok(N['scheduled'] and N['last']['ok'] is False and N['last']['red'] == ['pytest-3.13', 'legacy'] and N['last']['secs'] == 410
       and N['last']['head'] == 'bbbbbbbbbbbb', 'nightly: last result with its red steps')
    ok(N['next'] == '2026-10-29T02:10:00+01:00', 'nightly: next run 02:10 Zurich tomorrow (local ISO time)')

    # legacy suites on main (legacy-main.json + legacy-main rows) and deploys (deployed.json, hub.json, deploy rows)
    wj(os.path.join(ST, 'legacy-main.json'), {'suites': {
        'dash': {'ok': False, 'how': 'post-land', 'ts': NOW - 300, 'detail': 'exit 1 zzlegacy_detail', 'src': 'scripts/x'},
        'cifeed': {'ok': True, 'how': 'land', 'ts': NOW - 200}, '<b>x</b>': {'ok': False, 'ts': NOW - 100}}})
    wj(os.path.join(ST, 'deployed.json'), {'release': 'dddddddddddd99', 'ts': NOW - 600, 'files': {'a': 1, 'b': 2}, 'core': {}})
    wj(os.path.join(ST, 'hub.json'), {'release': 'hhhhhhhhhhhh77', 'line': 'zzhubline'})
    deploys([{'op': 'legacy-main', 'ok': True, 'head': 'aaa', 'red': [], 'secs': 100, 'after': 'one-pkg', 'ts': NOW - 9 * 86400},
             {'op': 'deploy', 'ok': True, 'target': 'hub', 'release': 'r1', 'ts': NOW - 8 * 86400}] + rows +
            [{'op': 'legacy-main', 'ok': False, 'head': 'eeeeeeeeeeee11', 'red': ['dash'], 'secs': 164, 'after': 'leg', 'ts': NOW - 40},
             {'op': 'legacy-main', 'ok': True, 'head': 'fff', 'skipped': 'load 33; rm', 'ts': NOW - 30},
             {'op': 'deploy', 'ok': False, 'target': 'hub', 'release': 'r2r2r2r2r2r2r2', 'ts': NOW - 20}])
    s2 = C.snapshot(NOW)
    Lg, Dp = s2['legacy'], s2['deploys']
    ok(Lg['suites'] == 3 and Lg['red_n'] == 2 and [x['suite'] for x in Lg['red']] == ['bx/b', 'dash']
       and Lg['red'][1] == {'suite': 'dash', 't': C.iso(NOW - 300), 'how': 'post-land'}, f'legacy: red suites, newest first ({Lg["red"]})')
    ok(Lg['last'] == {'t': C.iso(NOW - 40), 'ok': False, 'secs': 164, 'after': 'leg', 'head': 'eeeeeeeeeeee', 'red': ['dash']},
       f'legacy: the last run that ran ({Lg["last"]})')
    ok(Lg['skipped'] == {'t': C.iso(NOW - 30), 'why': 'load 33 rm'} and Lg['runs_7d'] == 1 and Lg['green_7d'] == 0,
       f'legacy: a newer skip with its plain reason; 7-day counts without skips ({Lg["skipped"]})')
    ok(Dp['core'] == {'release': 'dddddddddddd', 't': C.iso(NOW - 600), 'files': 2} and Dp['hub'] == {'release': 'hhhhhhhhhhhh'},
       f'deploys: core and hub releases ({Dp["core"]}, {Dp["hub"]})')
    ok([(x['target'], x['ok'], x['release']) for x in Dp['recent']] == [('hub', False, 'r2r2r2r2r2r2'), (None, True, None), ('hub', True, 'r1')]
       and Dp['n_7d'] == 2 and Dp['failed_7d'] == 1, f'deploys: rows newest first, 7-day counts ({Dp["recent"]})')
    G = Lg['grid']
    ok([(r['t'], r['ok'], r['head'], r['after']) for r in G['runs']] == [(C.iso(NOW - 9 * 86400), True, 'aaa', 'one-pkg'),
                                                                          (C.iso(NOW - 40), False, 'eeeeeeeeeeee', 'leg')],
       f"legacy grid: the runs that ran, oldest first, no skips ({G['runs']})")
    ok(G['rows'] == [{'suite': 'dash', 'red_now': True, 'cells': [0, 1]}, {'suite': 'bx/b', 'red_now': True, 'cells': [0, 0]}]
       and G['green'] == ['cifeed'], f"legacy grid: a row per suite red then or now, every other suite by name ({G})")
    ok([m['kind'] for m in s2['timeline']['marks']] == ['deploy', 'nightly', 'legacy', 'deploy']
       and s2['timeline']['marks'][-1] == {'t': C.iso(NOW - 20), 'kind': 'deploy', 'ok': False, 'target': 'hub'},
       f"timeline: a legacy-main run is a marker, its skip is not; the failed hub deploy ({s2['timeline']['marks']})")
    many = [{'op': 'legacy-main', 'ok': False, 'head': 'h%d' % i, 'red': ['s%02d' % j for j in range(i % 3, 14)], 'secs': 9,
             'ts': NOW - 3000 + i} for i in range(15)]
    deploys(many)
    G = C.snapshot(NOW)['legacy']['grid']
    ok(len(G['runs']) == 12 and G['runs'][-1]['head'] == 'h14' and len(G['rows']) == 16 and G['green'] == ['cifeed']
       and [r['suite'] for r in G['rows'][:3]] == ['bx/b', 'dash', 's02'] and all(len(r['cells']) == 12 for r in G['rows']),
       f"legacy grid: the last 12 runs, every suite (red now first, no cap), the green ones by name ({len(G['rows'])}, {G['green']})")
    deploys([{'op': 'legacy-main', 'ok': True, 'head': 'aaa', 'red': [], 'secs': 100, 'after': 'one-pkg', 'ts': NOW - 9 * 86400},
             {'op': 'deploy', 'ok': True, 'target': 'hub', 'release': 'r1', 'ts': NOW - 8 * 86400}] + rows +
            [{'op': 'legacy-main', 'ok': False, 'head': 'eeeeeeeeeeee11', 'red': ['dash'], 'secs': 164, 'after': 'leg', 'ts': NOW - 40},
             {'op': 'legacy-main', 'ok': True, 'head': 'fff', 'skipped': 'load 33; rm', 'ts': NOW - 30},
             {'op': 'deploy', 'ok': False, 'target': 'hub', 'release': 'r2r2r2r2r2r2r2', 'ts': NOW - 20}])
    ok(len(s2['lands']) == 8 and 'zzlegacy_detail' not in json.dumps(s2) and 'zzhubline' not in json.dumps(s2)
       and 'scripts/x' not in json.dumps(s2), 'legacy/deploys: land rows unchanged; no suite output, paths or hub line shown')

    # hub runner records: newest signed_at wins; workers recorded / xdist / serial
    def rec(name, at, steps, ok_=True):
        wj(os.path.join(ST, 'ci-signed', name + '.json'), {'sig': 'x', 'record': {
            'signed_at': at, 'ok': ok_, 'secs': 264.6, 'branch': 'one-pkg', 'hub_release': 'c6215512a696',
            'runner': {'release': 'c6215512a696', 'toolchain': '28a2b8dfa556', 'python': '3.13.15'}, 'steps': steps}})
    rec('t1', NOW - 900, [{'name': 'pytest-3.13', 'ok': True}])
    rec('t2', NOW - 100, [{'name': 'ruff', 'ok': True}, {'name': 'pytest-3.13', 'ok': False,
                                                          'failed': ['FAILED tests/z.py::test_zzrunner_marker']}], ok_=False)
    R = C.snapshot(NOW)['runner']
    r = R['record']
    ok(R['gate'] == 'enforce-gated' and R['signed_green_lands'] == 1, 'runner: gate mode and signed green lands')
    ok(r['t'] == C.iso(NOW - 100) and r['ok'] is False and r['failed'] == ['pytest-3.13'] and r['release'] == 'c6215512a696'
       and r['python'] == '3.13.15' and r['signed'], f'runner: the newest record ({r})')
    ok(r['workers'] == 1 and 'serial' in r['workers_note'], 'runner: no selection in the record -> serial (old release)')
    ok('zzrunner_marker' not in json.dumps(R), 'runner: failed test ids never shown')
    rec('t3', NOW - 50, [{'name': 'pytest-3.13', 'ok': True, 'selection': {'mode': 'full'}}])
    ok(C.snapshot(NOW)['runner']['record']['workers'] is None, 'runner: xdist release without a count -> unknown, not guessed')
    rec('t4', NOW - 40, [{'name': 'pytest-3.13', 'ok': True, 'workers': 4, 'selection': {'mode': 'full'}}])
    ok(C.snapshot(NOW)['runner']['record']['workers'] == 4, 'runner: a recorded worker count is shown')

    # queue: lock holder, live tickets in order, dead / reused pids skipped, land processes without a ticket
    proc(4101, ['/usr/bin/python3', '-I', '/home/x/bin/assistant', 'land', 'holder-br'])
    wj(os.path.join(ST, 'deploy.lock'), {'pid': 4101, 'what': 'land holder-br', 'since': NOW - 120})
    proc(4102, ['/usr/bin/python3', '-I', '/home/x/bin/assistant', 'land', 'second'], start='777')
    proc(4103, ['/usr/bin/python3', '-I', '/home/x/bin/assistant', 'land', 'reused'], start='999')
    proc(4104, ['/usr/bin/python3', '-I', '/home/x/bin/assistant', 'land', '--override', 'why', 'third'], start='888')
    proc(4105, ['/bin/sh', '-c', 'cron-fail x', '/home/x/bin/assistant', 'land', 'starting-br'])
    proc(4106, ['/usr/bin/python3', '-I', '/home/x/bin/assistant', 'land', 'starting-br'])
    proc(4107, ['/usr/bin/python3', '-I', '/home/x/bin/assistant', 'land', 'zombie'], state='Z', start='1')
    wj(os.path.join(ST, 'deploy.queue', '12.ticket'), {'pid': 4104, 'start': '888', 'what': 'land third', 'since': NOW - 30})
    wj(os.path.join(ST, 'deploy.queue', '9.ticket'), {'pid': 4102, 'start': '777', 'what': 'land second', 'since': NOW - 60})
    wj(os.path.join(ST, 'deploy.queue', '10.ticket'), {'pid': 4103, 'start': '123', 'what': 'land reused', 'since': NOW - 50})
    wj(os.path.join(ST, 'deploy.queue', '11.ticket'), {'pid': 4999, 'start': '1', 'what': 'land gone', 'since': NOW - 40})
    wj(os.path.join(ST, 'deploy.queue', '13.ticket'), {'pid': 4107, 'start': '1', 'what': 'land zombie', 'since': NOW - 20})
    Q = C.snapshot(NOW)['queue']
    ok(Q['holder'] == {'op': 'land', 'branch': 'holder-br', 'since': C.iso(NOW - 120)}, f"queue: the lock holder ({Q['holder']})")
    ok([w['branch'] for w in Q['waiting']] == ['second', 'third'], f"queue: live tickets in number order ({Q['waiting']})")
    ok([w['branch'] for w in Q['starting']] == ['reused', 'starting-br'],
       f"queue: land processes without a live ticket, one row per branch ({Q['starting']})")
    ok(os.path.exists(os.path.join(ST, 'deploy.queue', '11.ticket')), 'queue: read-only (never prunes tickets)')
    # a starting land's phase record (assistant writes deploy.queue/land-<pid>.phase before the lock)
    wj(os.path.join(ST, 'deploy.queue', 'land-4106.phase'), {'pid': 4106, 'start': '5000', 'what': 'land starting-br',
                                                             'phase': 'CI host + runner', 'since': NOW - 90, 'land_since': NOW - 400, 'rerun': 1})
    wj(os.path.join(ST, 'deploy.queue', 'land-4103.phase'), {'pid': 4103, 'start': '123', 'what': 'land reused',
                                                             'phase': 'rebase', 'since': NOW - 5, 'land_since': NOW - 5})
    wj(os.path.join(ST, 'deploy.queue', 'land-4105.phase'), {'pid': 4105, 'start': '5000', 'what': 'land starting-br',
                                                             'phase': 'rm -rf /tmp/x', 'since': NOW - 5})
    S = {w['branch']: w for w in C.snapshot(NOW)['queue']['starting']}
    sb = S['starting-br']
    ok(sb['phase'] == 'CI host + runner' and sb['rerun'] == 1 and sb['since'] == C.iso(NOW - 400)
       and sb['phase_since'] == C.iso(NOW - 90), f'queue: a starting land shows its phase, re-run count and times ({sb})')
    ok(S['reused']['phase'] is None and S['reused']['since'] is None, 'queue: a phase record of a reused pid is ignored')
    ok('rm -rf' not in json.dumps(S), 'queue: only known phase words are shown')
    wj(os.path.join(ST, 'deploy.lock'), {'pid': 4999, 'what': 'land gone', 'since': NOW - 120})
    ok(C.snapshot(NOW)['queue']['holder'] is None, 'queue: a dead holder pid = lock free')
    proc(4108, ['/usr/bin/sleep', '100'])
    wj(os.path.join(ST, 'deploy.lock'), {'pid': 4108, 'what': 'land reused-pid', 'since': NOW - 120})
    ok(C.snapshot(NOW)['queue']['holder'] is None, 'queue: a holder pid that is no assistant process = lock free')
    with open(os.path.join(ST, 'deploy.lock'), 'w') as f:
        f.write('{broken')
    ok(C.snapshot(NOW)['queue']['holder'] is None, 'queue: an unreadable lock file = no holder')

    # desc / body / commits: the landed commit's subject and the first lines of its body (trailers skipped), several
    # commits as their subjects (max 5, then "+N more"), a red land's branch tip, plain and short; git asked once per row
    # (cached by sha), only for the shown lands, never for a row without commit or branch
    os.makedirs(REPO)
    git('init', '-q', '-b', 'main')
    base = commit('base')
    one = commit('CI tab lands: a short description per land\n\nThe fold shows what a land was about:\nits `subject` and '
                 'body.\n- item two\n\nA second paragraph that is never shown.\n\nCo-Authored-By: x <x@example.invalid>')
    git('checkout', '-q', '-b', 'red-x')
    commit('Red attempt \u2192 its tip <b>subject</b> $(z)\n\nCo-Authored-By: x <x@example.invalid>')
    git('checkout', '-q', 'main')
    commit('First of three')
    commit('fixup two')
    c = commit('fixup three')
    lng = commit('A very long subject line that keeps going well past the seventy character cut on a word boundary and '
                 'then some more words\n\n' + ' '.join(['word%d' % i for i in range(120)]))
    for i in range(6):
        commit('step %d of seven' % i)
    sev = commit('step 6 of seven')
    calls = []
    real = C._git
    C._git = lambda *x: calls.append(x) or real(*x)
    drows = [{'op': 'land', 'ok': True, 'branch': 'one', 'from': base[:12], 'sha': one, 'release': one[:12], 'ts': NOW - 900},
             {'op': 'land', 'ok': True, 'branch': 'three', 'from': one[:12], 'sha': c, 'release': c[:12], 'ts': NOW - 800},
             {'op': 'land', 'ok': True, 'branch': 'nofrom', 'release': lng[:12], 'ts': NOW - 700},
             {'op': 'land', 'ok': True, 'branch': 'seven', 'from': lng[:12], 'sha': sev, 'release': sev[:12], 'ts': NOW - 650},
             {'op': 'land', 'ok': True, 'branch': 'gone', 'release': 'f' * 12, 'ts': NOW - 600},
             {'op': 'land', 'ok': False, 'branch': 'red-x', 'why': 'ci', 'ts': NOW - 500},
             {'op': 'land', 'ok': False, 'branch': 'red-deleted', 'why': 'ci', 'ts': NOW - 400},
             {'op': 'land', 'ok': False, 'branch': '--output=x', 'why': 'ci', 'ts': NOW - 300},
             {'op': 'land', 'ok': False, 'why': 'ci', 'ts': NOW - 200}]
    deploys([dict(r, ts=NOW - 86400 * 2) for r in drows[:2]] * 10 + drows)
    X = {x['branch']: x for x in C.snapshot(NOW)['lands']}
    D = {k: v['desc'] for k, v in X.items()}
    ok(len(D) == 9 and D['one'] == 'CI tab lands: a short description per land' and X['one']['commits'] == 1
       and X['one']['body'] == ['The fold shows what a land was about: its subject and body.', '- item two'],
       f"desc: one commit: its subject, its body's first paragraph (wrapped lines joined, items kept) ({X['one']})")
    ok(D['three'] == 'First of three +2 more' and X['three']['body'] == ['First of three', 'fixup two', 'fixup three']
       and X['three']['commits'] == 3, f"desc: a land of 3 commits: each subject ({X['three']})")
    ok(X['seven']['commits'] == 7 and X['seven']['body'] == ['step %d of seven' % i for i in range(5)] + ['+2 more'],
       f"desc: a land of 7 commits: 5 subjects, then +2 more ({X['seven']['body']})")
    bl = X['nofrom']['body']
    ok(D['nofrom'].startswith('A very long subject') and len(D['nofrom']) <= 121 and D['nofrom'].endswith('\u2026')
       and len(bl) == 1 and len(bl[0]) <= 161 and bl[0].endswith('\u2026') and sum(map(len, bl)) <= 300,
       f"desc: no 'from': the commit's own message, subject and body cut on word boundaries ({X['nofrom']})")
    ok(D['red-x'] == 'Red attempt -> its tip <b>subject</b> (z)' and X['red-x']['body'] == [] and D['red-deleted'] is None
       and D['gone'] is None and D['--outputx'] is None and D[None] is None and X['gone']['body'] == [],
       f'desc: a red land shows its branch tip (plain charset, trailers skipped), nothing when the branch or commit is gone ({D})')
    n = len(calls)
    C.snapshot(NOW)
    ok(n == 7 and len(calls) == n and not any(a_[-1].startswith('-') and a_[0] == 'log' and len(a_) < 4 for a_ in calls),
       f'desc: git asked once per shown land with a commit or branch, cached after ({n} calls)')
    C._git = real
    ok(C.line('x' * 99) == 'x' * 70 + '\u2026' and C.line(' \t ') is None and C.line('a\nb') == 'a b',
       'line(): one line, cut, None when empty')
    ok(C._body('a\nb\n\nc') == ['a b'] and C._body('Co-Authored-By: x\n\nreal') == ['real'] and C._body('') == []
       and len(C._body('\n'.join('- item %d' % i for i in range(9)))) == 4, '_body(): first paragraph, trailers out, 4 lines')

    # the deploy log is re-read only when it changed; a broken line is skipped
    deploys(rows[:1])
    with open(os.path.join(ST, 'deploys.jsonl'), 'a') as f:
        f.write('{not json\n')
    ok(len(C.snapshot(NOW)['lands']) == 1, 'log: re-read after a change, broken lines skipped')
    ok(C.word('p3-11 switch<on>') == 'p3-11switchon' and C.word('') is None and len(C.word('x' * 99)) == 48,
       'word(): plain charset, cut, None when empty')
finally:
    shutil.rmtree(T, ignore_errors=True)
print(f'{COUNT[0] - len(FAILS)}/{COUNT[0]} passed' + (f', {len(FAILS)} FAILED' if FAILS else ''))
sys.exit(1 if FAILS else 0)
