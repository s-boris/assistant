"""Offline tests for lastcall: fake gohome / vault / relay / tg commands, a fictional profile and calendar, an isolated
HOME and a simulated clock. Never rings or texts for real. LASTCALL_BIN=/path/to/copy to test a copy."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import datetime as dt
from zoneinfo import ZoneInfo

BIN = os.environ.get('LASTCALL_BIN') or os.path.expanduser('~/bin/lastcall')
EVENT_CARD = os.environ.get('EVENT_CARD_BIN') or os.path.expanduser('~/bin/event-card')
TMP = tempfile.mkdtemp(prefix='lastcall-tests-')
FAKE = os.path.join(TMP, 'fake')
os.makedirs(FAKE)
os.makedirs(os.path.join(TMP, 'home'))
FAILS = []


def write(name, obj):
    path = os.path.join(TMP, name)
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

# gohome stand-in: next 3 from GOHOME_NOW and the latest one reaching the door by 04:45, from a schedule file
GOHOME = fake('gohome', r'''
import datetime as dt, json, os, sys
with open(os.environ['FAKE_LOG'], 'a') as f:
    f.write('gohome %s %s\n' % (sys.argv[1], os.environ.get('GOHOME_NOW')))
if os.path.exists(os.environ['FAKE_GOHOME_FAIL']):
    sys.exit(1)
now = dt.datetime.strptime(os.environ['GOHOME_NOW'], '%Y-%m-%d %H:%M')
cs = sorted(json.load(open(os.environ['FAKE_SCHEDULE'])), key=lambda c: c['dep'])
p = lambda s: dt.datetime.fromisoformat(s)
cut = dt.datetime.combine(now.date() + dt.timedelta(days=0 if now.time() < dt.time(4, 45) else 1), dt.time(4, 45))
nxt = [c for c in cs if p(c['dep']) >= now - dt.timedelta(minutes=1) and p(c['dep']) <= now + dt.timedelta(hours=14)][:3]
night = [c for c in cs if p(c['arr']) <= cut and p(c['dep']) >= now - dt.timedelta(hours=14)]
print(json.dumps({'home': False, 'origin': sys.argv[1], 'source': 'argument', 'now': now.isoformat(), 'next': nxt,
                  'last': max(night, key=lambda c: c['dep']) if night else None}))
''', 'py')
GOHOME_DST = fake('gohome-dst', r'''
import datetime as dt, json, os, sys
from zoneinfo import ZoneInfo
with open(os.environ['FAKE_LOG'], 'a') as f:
    f.write('gohome %s %s\n' % (sys.argv[1], os.environ.get('GOHOME_NOW')))
now = dt.datetime.strptime(os.environ['GOHOME_NOW'], '%Y-%m-%d %H:%M').replace(tzinfo=ZoneInfo('Europe/Zurich'))
cs = sorted(json.load(open(os.environ['FAKE_SCHEDULE'])), key=lambda c: dt.datetime.fromisoformat(c['dep']).timestamp())
ts = lambda c: dt.datetime.fromisoformat(c['dep']).timestamp()
nxt = [c for c in cs if now.timestamp() - 60 <= ts(c) <= now.timestamp() + 14 * 3600][:3]
last = next((x for x, y in zip(cs, cs[1:]) if ts(x) >= now.timestamp() - 60 and ts(y) - ts(x) > 7200), None)
print(json.dumps({'home': False, 'origin': sys.argv[1], 'source': 'argument', 'now': now.isoformat(), 'next': nxt,
                  'last': last}))
''', 'py')
VAULT = fake('vault', 'cat "$FAKE_VAULT_JSON" 2>/dev/null || exit 1')
RELAY = fake('relay', 'echo "$@" >> "$FAKE_RELAY_LOG"; exit $(cat "$FAKE_RELAY_RC" 2>/dev/null || echo 0)')
TG = fake('tg', 'printf "%s\\n---\\n" "$1" >> "$FAKE_TG_LOG"')


def conn(dep, arr, lines=('S9',), platform='33', wait=None, stop='Alphastadt HB', off=None):
    c = {'dep': dep, 'arr': arr, 'changes': len(lines) - 1, 'stop': stop, 'platform': platform,
         'lines': list(lines), 'wait': wait, 'link': None}
    if off:                                       # gohome's last mile: (final stop, its arrival, walk minutes)
        c.update(off=off[0], off_arr=off[1], walk_min=off[2])
    return c


WEEKDAY = [conn(f'{D}T22:07', f'{D}T23:14'), conn(f'{D}T22:37', f'{D}T23:44'), conn(f'{D}T23:07', f'{D2}T00:14'),
           conn(f'{D}T23:37', f'{D2}T00:44'), conn(f'{D2}T00:08', f'{D2}T01:15', off=('Testdorf', f'{D2}T00:58', 17)),
           conn(f'{D2}T05:07', f'{D2}T06:14', off=('Testdorf, Musterweg', f'{D2}T06:11', 3)),
           conn(f'{D2}T05:37', f'{D2}T06:44')]
# a Friday-style night: 00:41 (58 min wait, still a way home), 02:14 (64 min wait = taxi gap, skipped), 03:55, 05:14
FRIDAY = [conn(f'{D}T23:41', f'{D2}T03:16', ('Tram 9', 'IC8', 'SN9'), 'A', {'at': 'Gamma HB', 'min': 40}, 'Venue Stop'),
          conn(f'{D2}T00:41', f'{D2}T04:16', ('Tram 9', 'IC8', 'SN9'), 'A', {'at': 'Gamma HB', 'min': 58}, 'Venue Stop'),
          conn(f'{D2}T02:14', f'{D2}T06:13', ('Bus M72', 'IR N1'), None, {'at': 'Gamma HB', 'min': 64}, 'Venue Stop'),
          conn(f'{D2}T03:55', f'{D2}T06:55', ('Bus M71', 'IR'), 'A', None, 'Venue Stop'),
          conn(f'{D2}T05:14', f'{D2}T07:30', ('Tram 8', 'IC1'), 'A', None, 'Venue Stop')]
ALLNIGHT = [conn(f'{D}T22:{m:02d}', f'{D}T23:{m:02d}') for m in (5, 35)] + \
           [conn(f'{D2}T{h:02d}:05', f'{D2}T{h + 1:02d}:05') for h in range(0, 8)]


def ev(eid, title, start, end, town, travel=None, train=None):
    return {'id': eid, 'title': title, 'start': start + ':00+01:00', 'end': end + ':00+01:00', 'town': town,
            'all_day': False, 'free': False, 'flags': {'travel': travel}, 'train': train}


CAL_WEEKDAY = write('cal1.json', {'version': 1, 'events': [
    ev('o1', '🚆 Home → Alphastadt HB', f'{D}T18:14', f'{D}T19:23', 'Testdorf', 'train',
       {'from': 'home', 'to': 'Alphastadt HB', 'lines': ['S9']}),
    ev('e1', 'Testfest', f'{D}T19:00', f'{D}T22:30', 'Alphastadt'),
    ev('b1', '🚆 Alphastadt HB → Home', f'{D}T23:07', f'{D2}T00:14', 'Alphastadt', 'train',
       {'from': 'Alphastadt HB', 'to': 'home', 'lines': ['S9']})]})
CAL_FRIDAY = write('cal2.json', {'version': 1, 'events': [
    ev('e2', 'Night Rave', f'{D}T20:00', f'{D2}T06:00', 'Betastadt'),
    ev('b2', '🚆 Venue Stop → Home', f'{D2}T05:14', f'{D2}T07:30', 'Betastadt', 'train',
       {'from': 'Venue Stop', 'to': 'home', 'lines': ['Tram 8', 'IC1']})]})
CAL_NOBACK = write('cal3.json', {'version': 1, 'events': [
    ev('o3', '🚆 Home → Alphastadt HB', f'{D}T18:14', f'{D}T19:23', 'Testdorf', 'train',
       {'from': 'home', 'to': 'Alphastadt HB', 'lines': ['S9']}),
    ev('e3', 'Testfest', f'{D}T19:00', f'{D}T22:30', 'Alphastadt')]})

F = {k: os.path.join(TMP, k) for k in ('log', 'relay.log', 'relay.rc', 'tg.log', 'vault.json', 'gohome.fail')}


def place(kind='away', town='Alphastadt', band='region', drive=False, activity='still', age=3, zone=None):
    return {'place': {'kind': kind, 'zone': zone, 'band': band, 'town': town, 'flag': 'away', 'fix_age_min': age},
            'drive': {'active': drive}, 'activity': activity}


def reset(cal=CAL_WEEKDAY, sched=WEEKDAY, vault=None, rc=0, state=True):
    for k in ('log', 'relay.log', 'tg.log', 'gohome.fail', 'vault.json', 'relay.rc'):
        if os.path.exists(F[k]):
            os.remove(F[k])
    if state and os.path.isdir(os.path.join(TMP, 'state')):
        shutil.rmtree(os.path.join(TMP, 'state'))
    write('sched.json', sched)
    if vault is not None:
        write('vault.json', vault)
    with open(F['relay.rc'], 'w') as f:
        f.write(str(rc))
    ENV['EVENT_CARD_CALCACHE'] = cal


ENV = dict(os.environ, HOME=os.path.join(TMP, 'home'), ASSISTANT_PROFILE=PROFILE, LASTCALL_DIR=os.path.join(TMP, 'state'),
           LASTCALL_GOHOME=GOHOME, LASTCALL_VAULT=VAULT, LASTCALL_CALL_CMD=RELAY + ' call', LASTCALL_TG=TG,
           LASTCALL_EVENT_CARD=EVENT_CARD, LASTCALL_PLACE_STATE='off', EVENT_CARD_DIR=os.path.join(TMP, 'ec'),
           FAKE_LOG=F['log'], FAKE_SCHEDULE=os.path.join(TMP, 'sched.json'), FAKE_GOHOME_FAIL=F['gohome.fail'],
           FAKE_VAULT_JSON=F['vault.json'], FAKE_RELAY_LOG=F['relay.log'], FAKE_RELAY_RC=F['relay.rc'],
           FAKE_TG_LOG=F['tg.log'], PYTHONDONTWRITEBYTECODE='1')
for k in ('LASTCALL_NOW', 'GOHOME_NOW'):
    ENV.pop(k, None)


def run(*args, env=None):
    r = subprocess.run([BIN, *args], capture_output=True, text=True, env=dict(ENV, **(env or {})), timeout=60)
    return r.returncode, r.stdout + r.stderr


def read(k):
    try:
        with open(F[k]) as f:
            return f.read()
    except OSError:
        return ''


def state():
    try:
        with open(os.path.join(TMP, 'state', 'state.json')) as f:
            return json.load(f)['nights']
    except (OSError, ValueError, KeyError):
        return {}


def check(name, cond, extra=''):
    print(('ok   ' if cond else 'FAIL ') + name)
    if not cond:
        FAILS.append(name)
        if extra:
            print('     ' + extra.replace('\n', '\n     ')[:1500])


def main():
    # 1 cheap exit: nothing under way
    reset()
    rc, out = run('run', '--now', f'{D}T15:00')
    check('cheap exit outside the event window: no gohome, no state', rc == 0 and not read('log') and not state(), out)

    # 2 before the ring window: plan only
    reset(vault=place())
    rc, out = run('run', '--now', f'{D}T23:00')
    st = state()
    plan = next(iter(st.values()), {}).get('plan') or {}
    check('plan: last = 00:08, next after the gap 05:07', (plan.get('last') or {}).get('dep', '').endswith('00:08')
          and (plan.get('after') or {}).get('dep', '').endswith('05:07') and plan.get('gap_min') == 299, json.dumps(st))
    check('no ring 68 min before', not read('relay.log'), read('relay.log'))
    calls = read('log').count('gohome')
    rc, out = run('run', '--now', f'{D}T23:10')
    check('cached plan: no new lookups 10 min later', read('log').count('gohome') == calls, read('log'))

    # 3 in the window, at the venue, answered → one ring, no text, never twice
    rc, out = run('run', '--now', f'{D}T23:45')
    check('rings at 23:45 with the reason', 'call --reason the last train home leaves in 23 minutes' in read('relay.log'),
          read('relay.log') + out)
    check('answered: no text', not read('tg.log'), read('tg.log'))
    check('answered: done', next(iter(state().values())).get('done') == 'answered', json.dumps(state()))
    run('run', '--now', f'{D}T23:47')
    run('run', '--now', f'{D}T23:55')
    check('once per night', read('relay.log').count('call') == 1, read('relay.log'))

    # 4 not answered → ONE text with train, platform and the next one
    reset(vault=place(), rc=3)
    run('run', '--now', f'{D}T23:44')
    run('run', '--now', f'{D}T23:46')
    tg = read('tg.log')
    check('exit 3: one text', tg.count('---') == 1, tg)
    check('text names the train, platform, next', all(x in tg for x in ('00:08 from Alphastadt HB, platform 33', 'S9',
                                                                         'nothing until 05:07', 'tried to call')), tg)
    check('text: a long last mile is spelled out (door time from gohome)',
          'Testdorf 00:58, home 01:15 on foot (17 min)' in tg and 'home 01:15 ·' not in tg, tg)
    check('exit 3: rang once', read('relay.log').count('call') == 1, read('relay.log'))

    # 5 relay down / failed → the text too
    for code in (2, 5):
        reset(vault=place(), rc=code)
        run('run', '--now', f'{D}T23:50')
        check(f'exit {code}: one text', read('tg.log').count('---') == 1 and 'relay down' in read('tg.log'), read('tg.log'))

    # 6 busy (exit 4) → retried next run; still busy when < 5 min left → the text
    reset(vault=place(), rc=4)
    run('run', '--now', f'{D}T23:44')
    run('run', '--now', f'{D}T23:46')
    check('exit 4: retried next run', read('relay.log').count('call') == 2 and not read('tg.log'), read('relay.log'))
    run('run', '--now', f'{D2}T00:05')
    run('run', '--now', f'{D2}T00:06')
    check('exit 4 to the end: one "line busy" text', read('tg.log').count('---') == 1 and 'line busy' in read('tg.log'),
          read('tg.log'))
    reset(vault=place(), rc=4)
    run('run', '--now', f'{D}T23:44')
    with open(F['relay.rc'], 'w') as f:
        f.write('0')
    run('run', '--now', f'{D}T23:46')
    check('exit 4 then answered: no text, done', not read('tg.log') and
          next(iter(state().values())).get('done') == 'answered', json.dumps(state()))

    # 7 he has left: home, near home, driving, on the train → silent for the night
    for label, pl in (('home', place(kind='home', town=None, band=None)), ('near home', place(band='near', town=None)),
                      ('home town', place(town='Testdorf')), ('driving', place(drive=True)),
                      ('on the train', place(activity='vehicle'))):
        reset(vault=pl)
        run('run', '--now', f'{D}T23:44')
        write('vault.json', place())
        run('run', '--now', f'{D}T23:50')
        check(f'{label}: silent all night', not read('relay.log') and not read('tg.log') and
              next(iter(state().values())).get('done'), json.dumps(state()))

    # 7b a stale drive flag (core.drive: the answer is older than 60 min) is not "he left": it still rings
    reset(vault=dict(place(drive=True), age_min=120))
    run('run', '--now', f'{D}T23:44')
    check('stale drive flag (age 120 min): rings', read('relay.log').count('call') == 1, read('relay.log'))
    reset(vault=dict(place(drive=True), age_min=20))
    run('run', '--now', f'{D}T23:44')
    check('fresh drive flag (age 20 min): silent', not read('relay.log'), read('relay.log'))

    # 8 another town (fresh fix) → silent this run, rings once back in the venue town; a stale fix doesn't count
    reset(vault=place(town='Gammadorf'))
    run('run', '--now', f'{D}T23:44')
    check('another town: silent, not done', not read('relay.log') and not next(iter(state().values())).get('done'),
          json.dumps(state()))
    write('vault.json', place(town='Alphastadt'))
    run('run', '--now', f'{D}T23:48')
    check('back in the venue town: rings', read('relay.log').count('call') == 1, read('relay.log'))
    reset(vault=place(town='Gammadorf', age=90))
    run('run', '--now', f'{D}T23:44')
    check('stale fix in another town: rings', read('relay.log').count('call') == 1, read('relay.log'))
    reset(vault=place(kind='zone', zone='Work', town=None))
    run('run', '--now', f'{D}T23:44')
    check('zone Work = the work town, not the venue: silent', not read('relay.log'), read('relay.log'))

    # 9 vault unavailable → unknown → rings; place-state home → silent
    reset(vault=None)
    run('run', '--now', f'{D}T23:44')
    check('vault down: rings anyway', read('relay.log').count('call') == 1, read('relay.log'))
    reset(vault=None)
    ps = write('ps.json', {'zone': 'home', 'seen': f'{D}T23:43'})
    run('run', '--now', f'{D}T23:44', env={'LASTCALL_PLACE_STATE': ps})
    check('vault down, place-state home: silent', not read('relay.log'), read('relay.log'))

    # 9b a stale vault "home" (began before the night's 18:14 outbound leg, or a feed > 90 min old) → unknown → rings;
    # a home begun tonight, the phone app's home, or a stale home while on the train → silent
    def homed(since=None, age_min=None, source=None, fix=3, **kw):
        d = place(kind='home', town=None, band=None, age=fix, **kw)
        if since:
            d['place']['since'] = since
        if age_min is not None:
            d['age_min'] = age_min
        if source:
            d['source'] = source
        return d
    for label, pl, want in (
            ('vault home since 17:30 (before the 18:14 leg): rings', homed(f'{D}T17:30:00+01:00', 4, 'vault'), 1),
            ('vault home, no since, last fix 400 min old: rings', homed(fix=400), 1),
            ('vault home since 23:20 but its feed 120 min old: rings', homed(f'{D}T23:20:00+01:00', 120, 'vault'), 1),
            ('vault home since 23:20 (came home tonight): silent', homed(f'{D}T23:20:00+01:00', 4, 'vault'), 0),
            ('phone app home since 17:30: silent', homed(f'{D}T17:30:00+01:00', 4, 'phone'), 0),
            ('stale vault home but on the train: silent', homed(f'{D}T17:30:00+01:00', 4, 'vault',
                                                               activity='vehicle'), 0)):
        reset(vault=pl)
        run('run', '--now', f'{D}T23:44')
        rec = next(iter(state().values()))
        check(label, read('relay.log').count('call') == want and
              (not want or str(rec.get('place', '')).startswith('stale home')), (read('relay.log'), json.dumps(rec)))
    reset(vault=None)
    ps = write('ps2.json', {'zone': 'home', 'seen': f'{D}T23:43', 'since': f'{D} 17:30'})
    run('run', '--now', f'{D}T23:44', env={'LASTCALL_PLACE_STATE': ps})
    check('vault down, place-state home since 17:30 (before the leg): rings', read('relay.log').count('call') == 1,
          read('relay.log'))

    # 10 night network all night → no last train, no call
    reset(sched=ALLNIGHT, vault=place())
    for t in ('23:40', '23:50'):
        run('run', '--now', f'{D}T{t}')
    run('run', '--now', f'{D2}T03:45')
    check('all-night service: no call', not read('relay.log') and
          (next(iter(state().values())).get('plan') or {}).get('why') == 'allnight', json.dumps(state()))

    # 10b the last train has left already: no call
    reset(vault=place())
    run('run', '--now', f'{D2}T00:20')
    check('last train gone: no call', not read('relay.log') and
          (next(iter(state().values())).get('plan') or {}).get('why') == 'gone', json.dumps(state()))

    # 11 Friday night: calendar leg 05:14, last before the night gap = 00:41 (02:14 has a 64-min wait: skipped)
    reset(cal=CAL_FRIDAY, sched=FRIDAY, vault=place(town='Betastadt', band='far'))
    rc, out = run('status', '--json', '--now', f'{D}T20:00')
    n = (json.loads(out).get('nights') or [{}])[0] if rc == 0 else {}
    check('status: last 00:41, rings from 00:16', (n.get('plan') or {}).get('last', {}).get('dep', '').endswith('00:41')
          and str(n.get('ring_from')).startswith(f'{D2}T00:16') and n['plan']['after']['dep'].endswith('03:55'), out)
    run('run', '--now', f'{D2}T00:10')
    check('Friday: no ring at 00:10', not read('relay.log'), read('relay.log'))
    run('run', '--now', f'{D2}T00:16')
    check('Friday: rings at 00:16 for the 00:41', 'leaves in 25 minutes' in read('relay.log'), read('relay.log'))

    # 12 dry run: prints, rings nothing, writes nothing
    reset(vault=place())
    rc, out = run('run', '--dry-run', '--now', f'{D}T23:44')
    check('dry-run: would ring, nothing done', '[dry-run] would ring' in out and not read('relay.log') and
          not read('tg.log') and not os.path.exists(os.path.join(TMP, 'state', 'state.json')), out)

    # 13 test state without a relay command never rings for real
    reset(vault=place())
    rc, out = run('run', '--now', f'{D}T23:44', env={'LASTCALL_CALL_CMD': '', 'LASTCALL_TG': ''})
    check('test state without LASTCALL_CALL_CMD: no ring', not read('relay.log') and
          next(iter(state().values())).get('done') == 'calls off (test state)', out + json.dumps(state()))

    # 14 off, no leg home, gohome failing, a crash after the mark
    reset(vault=place())
    run('off', '--now', f'{D}T21:00')
    run('run', '--now', f'{D}T23:44')
    check('off: no call tonight', not read('relay.log'), read('relay.log'))
    reset(cal=CAL_NOBACK, vault=place())
    run('run', '--now', f'{D}T23:44')
    check('no leg home in the calendar: nothing', not read('relay.log') and not read('log'), read('log'))
    reset(vault=place())
    open(F['gohome.fail'], 'w').close()
    rc, out = run('run', '--now', f'{D}T23:44')
    check('gohome failing: exit 0, no ring', rc == 0 and not read('relay.log'), out)
    reset(vault=place())
    run('run', '--now', f'{D}T23:00')
    s = json.load(open(os.path.join(TMP, 'state', 'state.json')))
    k = next(iter(s['nights']))
    s['nights'][k].update(ring_at=f'{D}T23:44:00+01:00', rc=None)
    write(os.path.join('state', 'state.json'), s)
    run('run', '--now', f'{D}T23:46')
    check('a ring mark without a result: never rings again', not read('relay.log'), read('relay.log'))

    rc, out = run('status', '--now', f'{D}T19:30')
    check('status text', rc == 0 and 'last train 00:08' in out and 'rings 23:43–00:03' in out, out)

    # 5 the autumn clock change (CEST -> CET at 03:00: 02:00-02:59 twice): minutes are real minutes
    def dst_cal(back_dep, back_arr):
        return write('cal-dst.json', {'version': 1, 'events': [
            {'id': 'e9', 'title': 'Herbstfest', 'start': '2026-10-24T20:00:00+02:00', 'end': '2026-10-25T02:00:00+02:00',
             'town': 'Alphastadt', 'all_day': False, 'free': False, 'flags': {'travel': None}, 'train': None},
            {'id': 'b9', 'title': '🚆 Alphastadt HB → Home', 'start': back_dep, 'end': back_arr, 'town': 'Alphastadt',
             'all_day': False, 'free': False, 'flags': {'travel': 'train'},
             'train': {'from': 'Alphastadt HB', 'to': 'home', 'lines': ['SN9']}}]})
    rides = lambda deps: [conn(d, (dt.datetime.fromisoformat(d) + dt.timedelta(minutes=68)).astimezone(
        ZoneInfo('Europe/Zurich')).isoformat(), ('SN9',)) for d in deps]
    first = ['2026-10-25T01:38:00+02:00', '2026-10-25T02:08:00+02:00', '2026-10-25T02:38:00+02:00']
    late = ['2026-10-25T05:07:00+01:00']
    # 5a the last train runs in the SECOND 02:xx (02:38 CET): no ring at 02:15 CEST (83 min left), one at 02:15 CET
    reset(cal=dst_cal('2026-10-25T02:38:00+01:00', '2026-10-25T03:46:00+01:00'), vault=place(),
          sched=rides(first + ['2026-10-25T02:08:00+01:00', '2026-10-25T02:38:00+01:00'] + late), rc=3)
    rc, out = run('run', '--now', '2026-10-25T02:15:00+02:00', env={'LASTCALL_GOHOME': GOHOME_DST})
    check('DST: no ring at 02:15 CEST for the 02:38 CET train (83 min away)', not read('relay.log'), read('relay.log') + out)
    rc, out = run('run', '--now', '2026-10-25T02:15:00+01:00', env={'LASTCALL_GOHOME': GOHOME_DST})
    check('DST: rings at 02:15 CET, 23 min before 02:38 CET',
          'leaves in 23 minutes' in read('relay.log') and read('relay.log').count('call') == 1, read('relay.log') + out)
    check('DST: the text names 02:38 and home 03:46', '02:38 from' in read('tg.log') and 'home 03:46' in read('tg.log'),
          read('tg.log'))
    # 5b the last train runs in the FIRST 02:xx (02:38 CEST): rings 02:15 CEST; at 02:15 CET it left an hour ago
    reset(cal=dst_cal('2026-10-25T02:38:00+02:00', '2026-10-25T02:46:00+01:00'), vault=place(), sched=rides(first + late))
    run('run', '--now', '2026-10-25T02:15:00+02:00', env={'LASTCALL_GOHOME': GOHOME_DST})
    check('DST: rings at 02:15 CEST for 02:38 CEST', 'leaves in 23 minutes' in read('relay.log'), read('relay.log'))
    reset(cal=dst_cal('2026-10-25T02:38:00+02:00', '2026-10-25T02:46:00+01:00'), vault=place(), sched=rides(first + late))
    rc, out = run('run', '--now', '2026-10-25T02:15:00+01:00', env={'LASTCALL_GOHOME': GOHOME_DST})
    check('DST: no ring at 02:15 CET for the 02:38 CEST train (left 37 min ago)', not read('relay.log'), read('relay.log') + out)
    # 5c place-state from the second pass ("02:12", naive) is fresh at 02:15 CET: home, no ring
    reset(cal=dst_cal('2026-10-25T02:38:00+01:00', '2026-10-25T03:46:00+01:00'),
          sched=rides(first + ['2026-10-25T02:08:00+01:00', '2026-10-25T02:38:00+01:00'] + late))
    ps = write('ps-dst.json', {'zone': 'home', 'seen': '2026-10-25 02:12', 'since': '2026-10-25 02:12', 'fails': 0})
    rc, out = run('run', '--now', '2026-10-25T02:15:00+01:00', env={'LASTCALL_GOHOME': GOHOME_DST, 'LASTCALL_PLACE_STATE': ps})
    check('DST: a second-pass place-state reading counts as fresh (home: no ring)',
          not read('relay.log') and 'place-state' in json.dumps(state()), read('relay.log') + json.dumps(state()) + out)

    # 9 Live Update chip in the phone app (livecount; stub phone): from 25 min before the last train, once, ended after
    phone = fake('phone', 'echo "$*" >> "$FAKE_PHONE_LOG"; rc=$(cat "$FAKE_PHONE_RC" 2>/dev/null || echo 0); '
                 'if [ "$rc" = 4 ]; then echo \'{"ok": false, "status": "no_device"}\'; '
                 'else echo \'{"ok": true, "status": "sent", "id": "x"}\'; fi; exit $rc')
    live_dir, plog, prc = os.path.join(TMP, 'live'), os.path.join(TMP, 'phone.log'), os.path.join(TMP, 'phone.rc')
    lenv = {'LIVECOUNT_PHONE': phone, 'LIVECOUNT_DIR': live_dir, 'FAKE_PHONE_LOG': plog, 'FAKE_PHONE_RC': prc}

    def live_reset(rc=0, **kw):
        reset(**kw)
        shutil.rmtree(live_dir, ignore_errors=True)
        for x in (plog, prc):
            if os.path.exists(x):
                os.remove(x)
        with open(prc, 'w') as f:
            f.write(str(rc))

    def plines():
        try:
            with open(plog) as f:
                return [x for x in f.read().splitlines() if x]
        except OSError:
            return []
    until = int(dt.datetime(2031, 3, 7, 0, 8, tzinfo=ZoneInfo('Europe/Zurich')).timestamp())
    live_reset(vault=place())
    run('run', '--now', f'{D}T23:00', env=lenv)
    check('live: no chip 68 min before', not plines(), '\n'.join(plines()))
    rc, out = run('run', '--now', f'{D}T23:45', env=lenv)
    pl = plines()
    check('live: one chip at 23:45: info level, "Last train 00:08", countdown to 00:08',
          len(pl) == 1 and pl[0].startswith('push --level info --title Last train 00:08 --body S9')
          and '--live countdown:%d' % until in pl[0] and '--tag live:lastcall:' in pl[0], '\n'.join(pl) + out)
    check('live: the ring still happens', 'leaves in 23 minutes' in read('relay.log'), read('relay.log'))
    run('run', '--now', f'{D}T23:50', env=lenv)
    check('live: answered, unchanged: no second push', len(plines()) == 1, '\n'.join(plines()))
    run('run', '--now', f'{D2}T00:09', env=lenv)
    pl = plines()
    check('live: ended once the train has left', len(pl) == 2 and pl[1].startswith('cancel live:lastcall:'), '\n'.join(pl))
    run('run', '--now', f'{D2}T00:11', env=lenv)
    check('live: nothing more after the end', len(plines()) == 2, '\n'.join(plines()))
    live_reset(vault=place(kind='home', town=None, band=None))
    run('run', '--now', f'{D}T23:45', env=lenv)
    check('live: home: no chip', not plines(), '\n'.join(plines()))
    live_reset(vault=place())
    run('run', '--now', f'{D}T23:45', env=lenv)
    with open(F['vault.json'], 'w') as f:
        json.dump(place(drive=True), f)
    rc, out = run('run', '--now', f'{D}T23:47', env=lenv)
    check('live: he left (driving) after the ring: still the chip (done night keeps it)', len(plines()) == 1,
          '\n'.join(plines()))
    live_reset(vault=place(), rc=4)
    rc1, _ = run('run', '--now', f'{D}T23:45', env=lenv)
    rc2, _ = run('run', '--now', f'{D}T23:47', env=lenv)
    check('live: no paired phone (exit 4): silent, one try, then a 10-min back-off', rc1 == rc2 == 0 and len(plines()) == 1
          and 'leaves in 23 minutes' in read('relay.log'), '\n'.join(plines()))
    live_reset(vault=place())
    run('run', '--now', f'{D}T23:45')
    check('live: test state without LIVECOUNT_PHONE: no phone call, no live state', not plines()
          and not os.path.exists(live_dir), '\n'.join(plines()))
    live_reset(vault=place())
    rc, out = run('run', '--dry-run', '--now', f'{D}T23:45', env=lenv)
    check('live: --dry-run prints the chip, calls nothing', 'live: start chip "Last train 00:08"' in out and not plines(),
          out)

    shutil.rmtree(TMP, ignore_errors=True)
    print('\n%d failed' % len(FAILS) if FAILS else '\nall passed')
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(main())
