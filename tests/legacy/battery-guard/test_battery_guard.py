"""Offline tests for battery-guard: fake gohome / vault / relay / tg, a fictional profile and calendar, an isolated HOME
and a simulated clock; the real lastcall and event-card are loaded as modules. Never rings or texts for real.
BATTERY_GUARD_BIN=/path/to/copy to test a copy."""
import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile

BIN = os.environ.get('BATTERY_GUARD_BIN') or os.path.expanduser('~/bin/battery-guard')
LASTCALL = os.environ.get('LASTCALL_BIN') or os.path.expanduser('~/bin/lastcall')
EVENT_CARD = os.environ.get('EVENT_CARD_BIN') or os.path.expanduser('~/bin/event-card')
TMP = tempfile.mkdtemp(prefix='battery-guard-tests-')
FAKE = os.path.join(TMP, 'fake')
os.makedirs(FAKE)
os.makedirs(os.path.join(TMP, 'home'))
FAILS = []


def write(name, obj):
    path = os.path.join(TMP, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        json.dump(obj, f)
    return path


def fake(name, body, lang='sh'):
    path = os.path.join(FAKE, name)
    with open(path, 'w') as f:
        f.write(('#!/bin/sh\n' if lang == 'sh' else '#!/usr/bin/env python3\n') + body + '\n')
    os.chmod(path, 0o755)
    return path


PROFILE = write('profile.json', {'home': {'street': 'Musterweg 7', 'postcode': '9999', 'town': 'Testdorf'},
                                 'work': {'town': 'Betaburg'}, 'locale': {'tz': 'Europe/Zurich'}})
D, D2 = '2031-03-06', '2031-03-07'

GOHOME = fake('gohome', r'''
import datetime as dt, json, os, sys
with open(os.environ['FAKE_LOG'], 'a') as f:
    f.write('gohome %s %s\n' % (sys.argv[1], os.environ.get('GOHOME_NOW')))
now = dt.datetime.strptime(os.environ['GOHOME_NOW'], '%Y-%m-%d %H:%M')
cs = sorted(json.load(open(os.environ['FAKE_SCHEDULE'])), key=lambda c: c['dep'])
p = lambda s: dt.datetime.fromisoformat(s)
cut = dt.datetime.combine(now.date() + dt.timedelta(days=0 if now.time() < dt.time(4, 45) else 1), dt.time(4, 45))
nxt = [c for c in cs if p(c['dep']) >= now - dt.timedelta(minutes=1) and p(c['dep']) <= now + dt.timedelta(hours=14)][:3]
night = [c for c in cs if p(c['arr']) <= cut and p(c['dep']) >= now - dt.timedelta(hours=14)]
print(json.dumps({'home': False, 'origin': sys.argv[1], 'source': 'argument', 'now': now.isoformat(), 'next': nxt,
                  'last': max(night, key=lambda c: c['dep']) if night else None}))
''', 'py')
VAULT = fake('vault', 'echo vault >> "$FAKE_LOG"; cat "$FAKE_VAULT_JSON" 2>/dev/null || exit 1')
RELAY = fake('relay', 'echo "$@" >> "$FAKE_RELAY_LOG"; exit $(cat "$FAKE_RELAY_RC" 2>/dev/null || echo 0)')
TG = fake('tg', '[ -e "$FAKE_TG_FAIL" ] && exit 1; printf "%s\\n---\\n" "$1" >> "$FAKE_TG_LOG"')


def conn(dep, arr, lines=('S9',), platform='33', stop='Alphastadt HB', off=None):
    c = {'dep': dep, 'arr': arr, 'changes': len(lines) - 1, 'stop': stop, 'platform': platform,
         'lines': list(lines), 'wait': None, 'link': None}
    if off:
        c.update(off=off[0], off_arr=off[1], walk_min=off[2])
    return c


WEEKDAY = [conn(f'{D}T22:07', f'{D}T23:14'), conn(f'{D}T22:37', f'{D}T23:44'), conn(f'{D}T23:07', f'{D2}T00:14'),
           conn(f'{D}T23:37', f'{D2}T00:44'), conn(f'{D2}T00:08', f'{D2}T01:15', off=('Testdorf', f'{D2}T00:58', 17)),
           conn(f'{D2}T05:07', f'{D2}T06:14'), conn(f'{D2}T05:37', f'{D2}T06:44')]
RAVE = [conn(f'{D}T23:41', f'{D2}T01:16', ('IC8',), 'A', 'Betastadt'),
        conn(f'{D2}T00:41', f'{D2}T02:16', ('IC8',), 'A', 'Betastadt'),
        conn(f'{D2}T05:14', f'{D2}T06:30', ('IC1',), 'A', 'Betastadt')]


def ev(eid, title, start, end, town, travel=None, train=None):
    return {'id': eid, 'title': title, 'start': start + ':00+01:00', 'end': end + ':00+01:00', 'town': town,
            'all_day': False, 'free': False, 'flags': {'travel': travel}, 'train': train}


CAL = write('cal1.json', {'version': 1, 'events': [
    ev('o1', '🚆 Home → Alphastadt HB', f'{D}T18:14', f'{D}T19:23', 'Testdorf', 'train',
       {'from': 'home', 'to': 'Alphastadt HB', 'lines': ['S9']}),
    ev('e1', 'Testfest', f'{D}T19:00', f'{D}T22:30', 'Alphastadt'),
    ev('b1', '🚆 Alphastadt HB → Home', f'{D}T23:07', f'{D2}T00:14', 'Alphastadt', 'train',
       {'from': 'Alphastadt HB', 'to': 'home', 'lines': ['S9']})]})
CAL_RAVE = write('cal2.json', {'version': 1, 'events': [ev('r1', 'Warehouse Rave', f'{D}T22:00', f'{D2}T04:00',
                                                           'Betastadt')]})
KEY, KEY_RAVE = f'e1@{D}', f'r1@{D}'

F = {k: os.path.join(TMP, k) for k in ('log', 'relay.log', 'relay.rc', 'tg.log', 'tg.fail', 'vault.json')}


def phone(level, charging=False, kind='away', town='Alphastadt', band='region', drive=False, activity='still',
          zone=None, age=3):
    return {'battery': {'level': level, 'band': 'low', 'charging': charging, 'charger': 'none'},
            'place': {'kind': kind, 'zone': zone, 'band': band, 'town': town, 'flag': 'away', 'fix_age_min': age},
            'drive': {'active': drive}, 'activity': activity}


def reset(vault=None, cal=CAL, sched=WEEKDAY, key=KEY, rc=0, card=True):
    for k in ('log', 'relay.log', 'tg.log', 'tg.fail', 'vault.json'):
        if os.path.exists(F[k]):
            os.remove(F[k])
    for d in ('state', 'lc', 'ec'):
        shutil.rmtree(os.path.join(TMP, d), ignore_errors=True)
    if card:
        write('ec/state.json', {'night': key, 'message_id': 101, 'done': {}, 'alerts': []})
    write('sched.json', sched)
    if vault is not None:
        write('vault.json', vault)
    with open(F['relay.rc'], 'w') as f:
        f.write(str(rc))
    ENV['EVENT_CARD_CALCACHE'] = cal


def set_phone(v):
    write('vault.json', v)


ENV = dict(os.environ, HOME=os.path.join(TMP, 'home'), ASSISTANT_PROFILE=PROFILE,
           BATTERY_GUARD_DIR=os.path.join(TMP, 'state'), BATTERY_GUARD_VAULT=VAULT, BATTERY_GUARD_TG=TG,
           BATTERY_GUARD_LASTCALL=LASTCALL, BATTERY_GUARD_LOCK_WAIT='2',
           LASTCALL_DIR=os.path.join(TMP, 'lc'), LASTCALL_GOHOME=GOHOME, LASTCALL_VAULT=VAULT,
           LASTCALL_CALL_CMD=RELAY + ' call', LASTCALL_TG=TG, LASTCALL_EVENT_CARD=EVENT_CARD,
           LASTCALL_PLACE_STATE='off', EVENT_CARD_DIR=os.path.join(TMP, 'ec'), EVENT_CARD_RAVE_RADAR='off',
           FAKE_LOG=F['log'], FAKE_SCHEDULE=os.path.join(TMP, 'sched.json'), FAKE_VAULT_JSON=F['vault.json'],
           FAKE_RELAY_LOG=F['relay.log'], FAKE_RELAY_RC=F['relay.rc'], FAKE_TG_LOG=F['tg.log'],
           FAKE_TG_FAIL=F['tg.fail'], PYTHONDONTWRITEBYTECODE='1')
for k in ('BATTERY_GUARD_NOW', 'LASTCALL_NOW', 'GOHOME_NOW', 'EVENT_CARD_NOW'):
    ENV.pop(k, None)


def run(*args, binary=None):
    r = subprocess.run([binary or BIN, *args], capture_output=True, text=True, env=ENV, timeout=90)
    return r.returncode, r.stdout + r.stderr


def bg(level, now, **kw):
    set_phone(phone(level, **kw))
    return run('run', '--now', now)


def read(k):
    try:
        with open(F[k]) as f:
            return f.read()
    except OSError:
        return ''


def texts():
    return [t for t in read('tg.log').split('\n---\n') if t.strip()]


def rings():
    return [x for x in read('relay.log').splitlines() if x.strip()]


def jstate(d):
    try:
        with open(os.path.join(TMP, d, 'state.json')) as f:
            return json.load(f).get('nights', {})
    except (OSError, ValueError):
        return {}


def check(name, cond, extra=''):
    print(('ok   ' if cond else 'FAIL ') + name)
    if not cond:
        FAILS.append(name)
        if extra:
            print('     ' + str(extra).replace('\n', '\n     ')[:1500])


def main():
    # cheap exit
    reset(card=False, vault=phone(5))
    rc, out = run('run', '--now', f'{D}T23:10')
    check('no live card: exit 0, no vault, no state', rc == 0 and not read('log') and not jstate('state'), out)

    # thresholds and away
    for name, kw, lvl in (('battery 45 %: nothing', {}, 45), ('charging at 15 %: nothing', {'charging': True}, 15),
                          ('home at 15 %: nothing', {'kind': 'home', 'town': 'Testdorf'}, 15),
                          ('near home at 15 %: nothing', {'band': 'near', 'town': 'Zetaville'}, 15),
                          ('home town at 15 %: nothing', {'town': 'Testdorf'}, 15)):
        reset()
        rc, out = bg(lvl, f'{D}T21:00', **kw)
        check(name, rc == 0 and not texts() and 'gohome' not in read('log') and not rings(), out + read('log'))
    reset()
    open(F['vault.json'], 'w').close()
    rc, out = run('run', '--now', f'{D}T21:00')
    check('vault failing: exit 0, nothing', rc == 0 and not texts(), out)

    # the warning
    reset()
    rc, out = bg(18, f'{D}T21:00')
    t = texts()
    check('18 % away: ONE warning', rc == 0 and len(t) == 1, (out, t))
    w = t[0] if t else ''
    check('warning: battery, last train, platform, door time', 'Phone at 18 %' in w and
          'Last train 00:08 from Alphastadt HB, platform 33' in w and 'Testdorf 00:58, home 01:15 on foot (17 min)' in w
          and 'After that: nothing until 05:07' in w, w)
    check('warning: the next connection from now', 'Next: 22:07 from Alphastadt HB' in w, w)
    check('warning: no call', not rings())
    rc, out = bg(17, f'{D}T21:02')
    check('once per night: no second warning', len(texts()) == 1, texts())
    rc, out = bg(9, f'{D}T22:30')
    check('9 % but the last train 98 min away: no call', not rings() and len(texts()) == 1, (out, rings()))

    # the call, then lastcall stays silent
    rc, out = bg(9, f'{D}T23:10')
    r = rings()
    check('9 % and 58 min: ONE call with battery + minutes', len(r) == 1 and '9 percent' in r[0]
          and '58 minutes' in r[0], (out, r))
    lc = jstate('lc').get(KEY, {})
    check('lastcall state: ring mark by battery-guard, night done', lc.get('by') == 'battery-guard'
          and lc.get('ring_at') and str(lc.get('done', '')).startswith('answered'), lc)
    check('own state: call answered', jstate('state').get(KEY, {}).get('call') == 'answered', jstate('state'))
    set_phone(phone(9))
    rc, out = run('run', '--now', f'{D}T23:44', binary=LASTCALL)
    check('lastcall at 23:44 after the battery call: no second ring', len(rings()) == 1 and len(texts()) == 1,
          (out, rings()))
    bg(5, f'{D}T23:50')
    check('battery-guard after its call: nothing more', len(rings()) == 1 and len(texts()) == 1)

    # lastcall rang first
    reset(vault=phone(30))
    rc, out = run('run', '--now', f'{D}T23:44', binary=LASTCALL)
    check('lastcall rings at 23:44', len(rings()) == 1, (out, rings()))
    bg(8, f'{D}T23:50')
    check('battery 8 % after lastcall rang: warning, no second ring', len(rings()) == 1 and len(texts()) == 1
          and jstate('state').get(KEY, {}).get('call', '').startswith('lastcall done'), (rings(), jstate('state')))

    # unanswered: lastcall's text only when the warning is old
    reset(rc=3)
    bg(18, f'{D}T21:00')
    bg(9, f'{D}T23:12')
    t = texts()
    check('unanswered, warning 2 h old: lastcall text', len(rings()) == 1 and len(t) == 2
          and 'I tried to call you' in t[1] and 'Last train home leaves in 56 min' in t[1], t)
    run('run', '--now', f'{D}T23:44', binary=LASTCALL)
    t, lcr = texts(), jstate('lc').get(KEY, {})
    check('unanswered battery call: lastcall still rings at 23:44, then its text', len(rings()) == 2
          and 'leaves in 24 minutes' in rings()[1] and len(t) == 3 and 'I tried to call you' in t[2]
          and lcr.get('bat_ring_at') and lcr.get('bat_rc') == 3 and str(lcr.get('done')).startswith('texted'),
          (rings(), t, lcr))
    run('run', '--now', f'{D}T23:50', binary=LASTCALL)
    bg(5, f'{D}T23:51')
    check('then nothing more', len(rings()) == 2 and len(texts()) == 3, rings())
    reset(rc=3)
    bg(9, f'{D}T23:41')
    run('run', '--now', f'{D}T23:44', binary=LASTCALL)
    check('lastcall 3 min after the unanswered battery call: waits', len(rings()) == 1, rings())
    run('run', '--now', f'{D}T23:46', binary=LASTCALL)
    check('lastcall 5 min after it: rings', len(rings()) == 2 and 'leaves in 22 minutes' in rings()[1], rings())
    reset(rc=3)
    rc, out = bg(8, f'{D}T23:10')
    t = texts()
    check('straight to 8 %: warning + call, no extra text', len(rings()) == 1 and len(t) == 1
          and 'Phone at 8 %' in t[0], (out, t))

    # a stale vault "home" (began before the 18:14 leg) on the event night counts as away: warning + call
    def homed(since, source='vault', age_min=4):
        d = phone(9, kind='home', zone='home', town=None, band=None)
        d['place']['since'], d['source'], d['age_min'] = since, source, age_min
        return d
    for label, d, want in (('stale vault home since 17:30: warning + call', homed(f'{D}T17:30:00+01:00'), 1),
                           ('vault home, feed 120 min old: warning + call', homed(f'{D}T23:00:00+01:00', age_min=120), 1),
                           ('vault home since 22:50 (came home): nothing', homed(f'{D}T22:50:00+01:00'), 0),
                           ('phone app home since 17:30: nothing', homed(f'{D}T17:30:00+01:00', 'phone'), 0)):
        reset(vault=d)
        rc, out = run('run', '--now', f'{D}T23:10')
        check(label, rc == 0 and len(rings()) == want and len(texts()) == want
              and (not want or '9 percent' in rings()[0]), (out, rings(), texts()))

    # busy: retried
    reset(rc=4)
    bg(9, f'{D}T23:10')
    check('busy: no call mark', not jstate('state').get(KEY, {}).get('call')
          and not jstate('lc').get(KEY, {}).get('ring_at'), (jstate('state'), jstate('lc')))
    with open(F['relay.rc'], 'w') as f:
        f.write('0')
    bg(9, f'{D}T23:12')
    check('busy: retried next run, then answered', len(rings()) == 2
          and jstate('state').get(KEY, {}).get('call') == 'answered', rings())

    # lastcall off, driving, another town
    reset(vault=phone(30))
    run('off', '--now', f'{D}T20:00', binary=LASTCALL)
    bg(8, f'{D}T23:10')
    check('lastcall off: the warning, no call', len(texts()) == 1 and not rings()
          and 'off' in jstate('state').get(KEY, {}).get('call', ''), jstate('state'))
    reset()
    bg(8, f'{D}T23:10', drive=True, activity='vehicle')
    check('driving: warning, no call, settled', len(texts()) == 1 and not rings()
          and 'driving' in jstate('state').get(KEY, {}).get('call', ''), jstate('state'))
    reset()
    bg(8, f'{D}T23:10', town='Omegadorf', age=5)
    check('another town (fresh fix): no call this run, not settled', not rings()
          and not jstate('state').get(KEY, {}).get('call'), jstate('state'))
    bg(8, f'{D}T23:14')
    check('back in the venue town: the call', len(rings()) == 1, rings())

    # too late
    reset()
    bg(8, f'{D2}T00:05')
    check('3 min before the last train: warning, no call', len(texts()) == 1 and not rings()
          and jstate('state').get(KEY, {}).get('call') == 'too late', jstate('state'))

    # a rave night without legs: the way home from the event's town
    reset(cal=CAL_RAVE, sched=RAVE, key=KEY_RAVE)
    rc, out = bg(15, f'{D}T23:00', town='Betastadt')
    t = texts()
    check('no-leg night: warning from the event town', len(t) == 1 and 'Last train 00:41 from Betastadt, platform A'
          in t[0] and 'nothing until 05:14' in t[0] and 'gohome Betastadt' in read('log'), (out, t, read('log')))
    bg(7, f'{D}T23:50', town='Betastadt')
    check('no-leg night: the call, record in lastcall state', len(rings()) == 1
          and jstate('lc').get(KEY_RAVE, {}).get('by') == 'battery-guard', (rings(), jstate('lc')))

    # tg failing: retried, 3 tries
    reset()
    open(F['tg.fail'], 'w').close()
    for m in ('00', '02', '04', '06'):
        bg(18, f'{D}T21:{m}')
    check('tg failing: 3 tries, then given up', jstate('state').get(KEY, {}).get('tries') == 3
          and read('log').count('gohome Alphastadt HB 2031-03-06 21:') <= 6, jstate('state'))

    # lastcall's lock held (it is ringing): next run
    reset()
    os.makedirs(os.path.join(TMP, 'lc'), exist_ok=True)
    fd = os.open(os.path.join(TMP, 'lc', '.lock'), os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    bg(8, f'{D}T23:10')
    check('lastcall busy: nothing this run', not texts() and not rings())
    os.close(fd)
    bg(8, f'{D}T23:12')
    check('lock free: warning + call', len(texts()) == 1 and len(rings()) == 1, (texts(), rings()))

    # dry run and status
    reset()
    set_phone(phone(8))
    rc, out = run('run', '--dry-run', '--now', f'{D}T23:10')
    check('dry run: prints, sends and writes nothing', rc == 0 and 'would text' in out and 'would ring' in out
          and not texts() and not rings() and not jstate('state') and not jstate('lc'), out)
    bg(18, f'{D}T21:00')
    rc, out = run('status', '--now', f'{D}T21:05')
    check('status', rc == 0 and 'sent 21:00 at 18 %' in out, out)

    shutil.rmtree(TMP, ignore_errors=True)
    print('\n%d failed' % len(FAILS) if FAILS else '\nall passed')
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(main())
