#!/usr/bin/env python3
"""Quiet-hours gates (00:00-08:00 Europe/Zurich) at both DST switch nights, across the tools that hold messages.
Each gate is loaded as a module and asked about pinned instants (epoch seconds) with TZ=UTC in the environment, so
a gate built on a fixed UTC offset, on naive wall-clock maths or on the container's TZ shows up here.
Run: python3 ~/opt/dst-tests/run.py
Covered: hddheat.is_quiet, unifi-health.is_quiet, ai-releases.quiet, vault-events.quiet, agent-watch.awake,
the heartbeat mod's capMin (node), tg-adapter's quiet.ts (~/opt/assistant/adapters/tg/src) inQuietHours with
its quietZone() (bun). Exit 0 = all pass.
Other DST tests live with their tools: dash-tests (day_fraction, hour slots), hb-gate-tests and the repo's
tests/legacy/tg-status (Due times in the repeated / skipped hour), cputemp-log-tests (watchdog ages), remind-tests
(REMIND_NOW=@EPOCH on
a UTC container: wall time kept, 02:30 in the skipped / repeated hour fires once, month and year turn, downtime),
unifi-health-tests (UNIFI_HEALTH_NOW: held texts and ack expiry), self-review-tests (SELF_REVIEW_NOW: report day,
ISO-week key, 7-day window), the repo's tests/legacy/kk (KK_NOW: the letter's default year).
"""
import datetime as dt
import importlib.machinery
import importlib.util
import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from zoneinfo import ZoneInfo

HOME = os.path.expanduser('~')
BIN = os.path.join(HOME, 'bin')
Z = ZoneInfo('Europe/Zurich')
os.environ['HOME'] = tempfile.mkdtemp(prefix='dst-tests-')   # imports must not touch real state
atexit.register(shutil.rmtree, os.environ['HOME'], True)
sys.dont_write_bytecode = True
passed = failed = 0


def ok(cond, name):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print('FAIL', name)


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name.replace('-', '_'), path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def zur(*t, fold=0):
    return dt.datetime(*t, fold=fold, tzinfo=Z).timestamp()


def utc(*t):
    return dt.datetime(*t, tzinfo=dt.timezone.utc).timestamp()


# (label, epoch, quiet?) — the switch nights' edges, plus the instants a fixed offset would get wrong
CASES = [
    ('autumn 23:59 CEST Sat', zur(2026, 10, 24, 23, 59), False),
    ('autumn 00:00 CEST', zur(2026, 10, 25, 0, 0), True),
    ('autumn 02:30 CEST (first pass)', zur(2026, 10, 25, 2, 30), True),
    ('autumn 02:30 CET (second pass)', zur(2026, 10, 25, 2, 30, fold=1), True),
    ('autumn 07:30 CET (06:30Z; UTC+2 would say 08:30)', utc(2026, 10, 25, 6, 30), True),
    ('autumn 07:59 CET', zur(2026, 10, 25, 7, 59), True),
    ('autumn 08:00 CET', zur(2026, 10, 25, 8, 0), False),
    ('spring 23:59 CET Sat', zur(2027, 3, 27, 23, 59), False),
    ('spring 01:59 CET', zur(2027, 3, 28, 1, 59), True),
    ('spring 03:00 CEST (02:xx skipped)', zur(2027, 3, 28, 3, 0), True),
    ('spring 07:59 CEST', zur(2027, 3, 28, 7, 59), True),
    ('spring 08:00 CEST (06:00Z; UTC+1 would say 07:00)', utc(2027, 3, 28, 6, 0), False),
    ('spring 08:30 CEST', zur(2027, 3, 28, 8, 30), False),
]

os.environ['TZ'] = 'UTC'   # the gates must name the owner's zone themselves; children (node, bun) inherit this
time.tzset()
ok(time.localtime(utc(2026, 7, 1, 12, 0)).tm_hour == 12, 'process clock is UTC')
mods = {}
for name, path in (('hddheat', f'{BIN}/hddheat'), ('unifi-health', f'{BIN}/unifi-health'),
                   ('ai-releases', f'{BIN}/ai-releases'), ('vault-events', f'{BIN}/vault-events'),
                   ('agent-watch', f'{BIN}/agent-watch')):
    try:
        mods[name] = load(name, path)
    except Exception as e:  # noqa: BLE001
        ok(False, f'{name}: loads as a module ({type(e).__name__}: {e})')


GATES = {
    'hddheat': lambda m, t: m.is_quiet(t),
    'unifi-health': lambda m, t: m.is_quiet(t),
    'ai-releases': lambda m, t: m.quiet(t),
    'vault-events': lambda m, t: m.quiet(t),
    'agent-watch': lambda m, t: not m.awake(t),
}
for name, f in GATES.items():
    m = mods.get(name)
    if m is None:
        continue
    for label, t, want in CASES:
        try:
            got = bool(f(m, t))
        except Exception as e:  # noqa: BLE001
            got = f'{type(e).__name__}: {e}'
        ok(got == want, f'{name} {label}: quiet={got} (want {want})')

# the heartbeat mod (node): capMin = 120 min in quiet hours, else 60
reg = os.path.join(HOME, 'plugins/heartbeat/hooks/register.js')
js = ("const m = await import(process.argv[1]); const ts = JSON.parse(process.argv[2]);"
      "console.log(JSON.stringify(ts.map(t => m.capMin(t * 1000))))")
try:
    r = subprocess.run(['node', '--input-type=module', '-e', js, reg, json.dumps([t for _, t, _ in CASES])],
                       capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    caps = json.loads(r.stdout.strip().splitlines()[-1])
    for (label, _, want), cap in zip(CASES, caps):
        ok(cap == (120 if want else 60), f'heartbeat capMin {label}: {cap}')
except Exception as e:  # noqa: BLE001
    ok(False, f'heartbeat capMin via node ({type(e).__name__}: {e})')

# tg-adapter's outbox quiet hours (bun): quiet.ts with the zone it picks (no TELEGRAM_QUIET_TZ, empty HOME)
qts = os.path.join(HOME, 'opt/assistant/adapters/tg/src/quiet.ts')
js = ("const m = await import(process.argv[1]); const ts = JSON.parse(process.argv[2]); const z = m.quietZone();"
      "console.log(JSON.stringify({z, q: ts.map(t => m.inQuietHours('0-8', t * 1000, z))}))")
try:
    env = {k: v for k, v in os.environ.items() if k not in ('TELEGRAM_QUIET_TZ', 'ASSISTANT_PROFILE')}
    bun = shutil.which('bun') or os.path.join(HOME, '.bun/bin/bun')
    r = subprocess.run([bun, '-e', js, qts, json.dumps([t for _, t, _ in CASES])], capture_output=True,
                       text=True, timeout=30, stdin=subprocess.DEVNULL, env=env)
    res = json.loads(r.stdout.strip().splitlines()[-1])
    ok(res['z'] == 'Europe/Zurich', f"tg-adapter quietZone: {res['z']}")
    for (label, _, want), got in zip(CASES, res['q']):
        ok(got == want, f'tg-adapter inQuietHours {label}: quiet={got} (want {want})')
except Exception as e:  # noqa: BLE001
    ok(False, f'tg-adapter quiet.ts via bun ({type(e).__name__}: {e})')

print(f'dst-tests: {passed} passed, {failed} failed')
sys.exit(1 if failed else 0)
