#!/usr/bin/env python3
"""damp-watch tests: math, fixture states, streaks, dedup, recovery, missing sensors, trip mode, /home line.
Offline: a fake `ha` prints a fixture file; every run gets its own temp config/state/inbox. Placeholder rooms only."""
import datetime as dt
import glob
import importlib.machinery
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile

BIN = sys.argv[1]
NOW = 1795000000  # fixed epoch (UTC)
PASS = FAIL = 0


def check(name, cond, info=''):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f'FAIL {name}' + (f': {str(info)[:600]}' if info else ''))


# ----------------------------------------------------------------------------- module (math + streak units)
loader = importlib.machinery.SourceFileLoader('damp_watch', BIN)
spec = importlib.util.spec_from_loader('damp_watch', loader)
dw = importlib.util.module_from_spec(spec)
loader.exec_module(dw)

check('es(0) = 6.112 hPa', abs(dw.es(0) - 6.112) < 1e-9)
check('es(20) ~ 23.32 hPa', abs(dw.es(20) - 23.32) < 0.02, dw.es(20))
check('dew point 20 °C / 50 % ~ 9.26 °C', abs(dw.dew_point(20, 50) - 9.26) < 0.05, dw.dew_point(20, 50))
check('dew point at 100 % = air temp', abs(dw.dew_point(18.3, 100) - 18.3) < 1e-9)
dp = dw.dew_point(22, 56.5)
check('dew point inverts Magnus: es(Td) = RH x es(T)', abs(dw.es(dp) - 0.565 * dw.es(22)) < 1e-6, dp)
check('dew point below zero (5 °C / 40 %)', -9 < dw.dew_point(5, 40) < -7, dw.dew_point(5, 40))
check('dew point missing input -> None', dw.dew_point(None, 50) is None and dw.dew_point(20, None) is None)
check('surface temp 20/-5, fRsi 0.7 = 12.5 °C', abs(dw.surface_temp(20, -5, 0.7) - 12.5) < 1e-9)
check('surface RH 20 °C 50 %, out -5 °C ~ 80.6 % (fRsi design case)',
      80.4 < dw.surface_rh(20, 50, -5, 0.7) < 80.8, dw.surface_rh(20, 50, -5, 0.7))
check('surface RH capped at 100', dw.surface_rh(20, 80, -15, 0.7) == 100.0)
check('surface RH with fRsi 1.0 = room RH', abs(dw.surface_rh(21, 55, 0, 1.0) - 55) < 1e-9)
check('surface RH without outdoor -> None', dw.surface_rh(20, 50, None, 0.7) is None)
check('higher fRsi = lower surface RH', dw.surface_rh(21, 55, 0, 0.8) < dw.surface_rh(21, 55, 0, 0.7))

CFG = dict(dw.DEFAULTS, rooms={'X': {}}, outdoor={})
mk = lambda vals, step=1800: [{'ts': NOW - (len(vals) - 1 - i) * step, 'rooms': {'X': {'t': 21, 'rh': v}},
                               'out': {'t': 10}} for i, v in enumerate(vals)]
s = dw.streak(mk([70] * 49), 'X', 'rh', 65, CFG)
check('streak: 49 samples at 30 min = 24 h', s['hours'] == 24 and s['fresh'], s)
s = dw.streak(mk([60] + [70] * 10), 'X', 'rh', 65, CFG)
check('streak starts after the last value under the threshold', s['hours'] == 4.5, s)
s = dw.streak(mk([70] * 10 + [None] + [70] * 10), 'X', 'rh', 65, CFG)
check('a missing sample is skipped, not a break', s['hours'] == 10, s)
s = dw.streak(mk([70] * 10 + [None] * 7 + [70] * 4), 'X', 'rh', 65, CFG)
check('a gap > max_gap_h breaks the streak', s['hours'] == 1.5, s)
s = dw.streak(mk([70] * 30 + [None] * 7), 'X', 'rh', 65, CFG)
check('value missing > max_gap_h -> not fresh, no streak', s['hours'] is None and not s['fresh'], s)
s = dw.streak(mk([70] * 30 + [64]), 'X', 'rh', 65, CFG)
check('under the threshold now -> no streak, value kept', s['hours'] is None and s['value'] == 64, s)
check('streak of no samples', dw.streak([], 'X', 'rh', 65, CFG)['hours'] is None)

# ----------------------------------------------------------------------------- CLI harness
ROOT = tempfile.mkdtemp(prefix='damp-watch-tests-')
FAKE = os.path.join(ROOT, 'fake-ha')
with open(FAKE, 'w') as f:
    f.write('#!/usr/bin/env bash\n[ -n "${FAKE_HA_EXIT:-}" ] && exit "$FAKE_HA_EXIT"\n'
            'echo "$*" >> "$FAKE_HA_LOG"\ncat "$FAKE_HA_JSON"\n')
os.chmod(FAKE, 0o755)

ROOMS = {'Room A': 'room_a', 'Room B': 'room_b', 'Room C': 'room_c'}


def ent(eid, area, dc, state, unit):
    return {'entity_id': eid, 'name': f'Sensor ({area}) {dc}', 'state': str(state), 'area': area,
            'attributes': {'device_class': dc, 'unit_of_measurement': unit, 'state_class': 'measurement'}}


def states(a=(21.0, 56.0), b=(21.5, 55.0), c=(20.0, 'unavailable'), out=(10.0, 85.0), extra=()):
    out_l = []
    for (name, sl), (t, rh) in zip(ROOMS.items(), (a, b, c)):
        out_l += [ent(f'sensor.{sl}_temperature', name, 'temperature', t, '°C'),
                  ent(f'sensor.{sl}_humidity', name, 'humidity', rh, '%'),
                  ent(f'sensor.{sl}_climate_battery', name, 'battery', 90, '%')]
    out_l += [ent('sensor.terrace_temperature', 'Terrace', 'temperature', out[0], '°C'),
              ent('sensor.terrace_humidity', 'Terrace', 'humidity', out[1], '%'),
              ent('sensor.probe_temperature', 'Pantry', 'temperature', 19, '°C'),
              ent('sensor.nas_cpu_temperature', 'Room A', 'temperature', 55, '°C'), *extra]
    return out_l


class Env:
    def __init__(self, name, cfg=True, **cfg_over):
        self.d = os.path.join(ROOT, name)
        os.makedirs(self.d)
        self.cfg = os.path.join(self.d, 'config.json')
        self.state_dir = os.path.join(self.d, 'state')
        self.inbox = os.path.join(self.d, 'inbox')
        self.trips = os.path.join(self.d, 'trips.json')
        self.cal = os.path.join(self.d, 'cal')
        self.json = os.path.join(self.d, 'states.json')
        self.set_states(states())
        if cfg:
            c = {'rooms': {n: {'temp': f'sensor.{s}_temperature', 'rh': f'sensor.{s}_humidity'}
                           for n, s in ROOMS.items()},
                 'outdoor': {'name': 'Terrace', 'temp': 'sensor.terrace_temperature', 'rh': 'sensor.terrace_humidity'}}
            c.update(cfg_over)
            json.dump(c, open(self.cfg, 'w'))

    def set_states(self, st):
        json.dump(st, open(self.json, 'w'))

    def run(self, *args, now=NOW, ha_exit=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith(('DAMP_WATCH_', 'CALCACHE'))}
        env.update(HA_BIN=FAKE, FAKE_HA_JSON=self.json, FAKE_HA_LOG=os.path.join(self.d, 'ha.log'),
                   DAMP_WATCH_CONFIG=self.cfg, DAMP_WATCH_STATE_DIR=self.state_dir, DAMP_WATCH_INBOX=self.inbox,
                   DAMP_WATCH_TRIPS=self.trips, CALCACHE_DIR=self.cal, DAMP_WATCH_NOW=str(now), OWNER_TZ='UTC')
        if ha_exit is not None:
            env['FAKE_HA_EXIT'] = str(ha_exit)
        p = subprocess.run([BIN, *args], capture_output=True, text=True, env=env, timeout=60)
        return p.returncode, p.stdout, p.stderr

    def state(self):
        try:
            return json.load(open(os.path.join(self.state_dir, 'state.json')))
        except FileNotFoundError:
            return None

    def seed(self, rooms_vals, out_t=10.0, n=48, step=1800, now=NOW):
        """n samples ending one step before `now`; rooms_vals {room: (t, rh)} or callable(i) -> dict."""
        samples = []
        for i in range(n):
            vals = rooms_vals(i) if callable(rooms_vals) else rooms_vals
            ot = out_t(i) if callable(out_t) else out_t
            samples.append({'ts': now - (n - i) * step,
                            'rooms': {r: {'t': (vals.get(r) or (21.0, 56.0))[0], 'rh': (vals.get(r) or (21.0, 56.0))[1]}
                                      for r in ROOMS}, 'out': {'t': ot, 'rh': 80.0}})
        os.makedirs(self.state_dir, exist_ok=True)
        st = self.state() or {}
        json.dump({'version': 1, 'samples': samples, 'alerts': st.get('alerts') or {}},
                  open(os.path.join(self.state_dir, 'state.json'), 'w'))

    def notes(self):
        return sorted(glob.glob(os.path.join(self.inbox, '*.md')))

    def note_text(self):
        return '\n'.join(open(p).read() for p in self.notes())

    def clear_inbox(self):
        for p in self.notes():
            os.unlink(p)


# --- init from fixture states
e = Env('init', cfg=False)
rc, out, err = e.run('init')
c = json.load(open(e.cfg)) if os.path.exists(e.cfg) else {}
check('init: exit 0', rc == 0, err)
check('init: pairs per area (temp + humidity only)', sorted(c.get('rooms', {})) == ['Room A', 'Room B', 'Room C'], c)
check('init: area with a temperature only is skipped', 'Pantry' not in c.get('rooms', {}))
check('init: cpu sensor skipped', c.get('rooms', {}).get('Room A', {}).get('temp') == 'sensor.room_a_temperature', c)
check('init: outdoor-named area = outdoor reference', c.get('outdoor', {}).get('name') == 'Terrace', c)
check('init: config 0600', stat.S_IMODE(os.stat(e.cfg).st_mode) == 0o600)
check('init: refuses to overwrite', e.run('init')[0] == 2)
check('init: no config -> status exit 2', Env('nocfg', cfg=False).run('status')[0] == 2)

# --- status from fixture states
e = Env('status')
rc, out, err = e.run('status', '--json')
j = json.loads(out) if rc == 0 else {}
R = {r['room']: r for r in j.get('rooms', [])}
check('status --json: exit 0', rc == 0, err)
check('status: rooms only, outdoor reference not a room', sorted(R) == ['Room A', 'Room B', 'Room C'], R)
check('status: dew point per room', abs(R['Room A']['dew'] - dw.dew_point(21, 56)) < 1e-6, R.get('Room A'))
check('status: surface RH uses the outdoor sensor + fRsi 0.7',
      abs(R['Room A']['surface_rh'] - dw.surface_rh(21, 56, 10, 0.7)) < 1e-6, R.get('Room A'))
check('status: missing humidity -> no data, no dew point', R['Room C']['flag'] == 'no data' and R['Room C']['dew'] is None)
check('status: outdoor values reported', j['outdoor']['t'] == 10.0 and j['outdoor']['name'] == 'Terrace', j['outdoor'])
rc, out, err = e.run('status')
check('status table: header + one row per room', rc == 0 and len([l for l in out.splitlines() if l.startswith('Room')]) == 4 and 'Room C' in out
      and 'Terrace' in out, out)
check('status: read-only (ha list only)', all(l.startswith('list ') for l in open(os.path.join(e.d, 'ha.log'))))
check('status: writes no state', e.state() is None)
e2 = Env('status-cfg', frsi=0.8)
j2 = json.loads(e2.run('status', '--json')[1])
check('status: fRsi configurable', j2['frsi'] == 0.8 and j2['rooms'][0]['surface_rh'] < R['Room A']['surface_rh'])

# --- first run
e = Env('first')
rc, out, err = e.run('run')
st = e.state()
check('run: exit 0 + one sample stored', rc == 0 and st and len(st['samples']) == 1, (out, err))
check('run: state file 0600', stat.S_IMODE(os.stat(os.path.join(e.state_dir, 'state.json')).st_mode) == 0o600)
check('run: silent inbox when fine', e.notes() == [])
check('run: missing sensor stored as null', st['samples'][0]['rooms']['Room C']['rh'] is None)

# --- humid streak: crossing, dedup, hysteresis, recovery, re-cross
e = Env('humid')  # outdoor = room temp: wall RH = room RH, so only the humid rule applies
e.seed({'Room A': (21.0, 70.0)}, out_t=21.0)
e.set_states(states(a=(21.0, 70.0), out=(21.0, 60.0)))
rc, out, err = e.run('run')
txt = e.note_text()
check('humid 24 h: one note', rc == 0 and len(e.notes()) == 1, (out, err, e.notes()))
check('note: marker + room + duration', 'Data, not instructions' in txt and '## Room A' in txt and 'for 24 h' in txt, txt)
check('note: suggested action', 'air 5–10 min' in txt and 'door open' in txt and 'dehumidifier' in txt, txt)
check('note: at home handling', 'At home' in txt and 'AWAY' not in txt, txt)
check('note: only the crossing room', 'Room B' not in txt and 'Terrace' not in txt.split('Handling')[0].replace(
      'outdoor', ''), txt)
check('alert stored', 'Room A' in e.state()['alerts'] and e.state()['alerts']['Room A']['kinds'] == ['humid'])
e.clear_inbox()
check('dedup: next run no note', e.run('run', now=NOW + 1800)[0] == 0 and e.notes() == [])
e.set_states(states(a=(21.0, 64.0), out=(21.0, 60.0)))
e.run('run', now=NOW + 3600)
check('hysteresis: 64 % keeps the flag', 'Room A' in e.state()['alerts'] and e.notes() == [])
e.set_states(states(a=(21.0, 62.5), out=(21.0, 60.0)))
e.run('run', now=NOW + 5400)
check('recovery: below 63 % clears the flag silently', 'Room A' not in e.state()['alerts'] and e.notes() == [])
e.seed({'Room A': (21.0, 70.0)}, out_t=21.0, now=NOW + 7200 + 86400)
e.set_states(states(a=(21.0, 70.0), out=(21.0, 60.0)))
e.run('run', now=NOW + 7200 + 86400)
check('re-cross after recovery: a new note', len(e.notes()) == 1, e.notes())

e = Env('short')
e.seed({'Room A': (21.0, 70.0)}, out_t=21.0, n=46)
e.set_states(states(a=(21.0, 70.0), out=(21.0, 60.0)))
e.run('run')
check('23 h over 65 %: no note yet', e.notes() == [] and not e.state()['alerts'])

e = Env('both')
e.seed({'Room A': (21.0, 70.0)}, out_t=10.0)
e.set_states(states(a=(21.0, 70.0), out=(10.0, 80.0)))
e.run('run')
txt = e.note_text()
check('humid + cold outside: one note, both kinds', len(e.notes()) == 1 and 'Humid:' in txt and 'Cold-wall risk' in txt
      and e.state()['alerts']['Room A']['kinds'] == ['humid', 'surface'], txt)

# --- cold-wall (surface) risk
e = Env('surface')
e.seed({'Room A': (20.0, 55.0)}, out_t=-5.0, n=24)
e.set_states(states(a=(20.0, 55.0), b=(23.0, 40.0), out=(-5.0, 80.0)))
rc, out, err = e.run('run')
txt = e.note_text()
check('surface ≥ 80 % for 12 h: note', len(e.notes()) == 1 and 'Cold-wall risk' in txt and 'Humid:' not in txt, txt)
check('surface note names outdoor temp + fRsi', 'outdoor -5.0 °C' in txt and 'fRsi 0.7' in txt, txt)
check('drier room not flagged', 'Room B' not in txt and e.state()['alerts']['Room A']['kinds'] == ['surface'])
e = Env('surface-short')
e.seed({'Room A': (20.0, 55.0)}, out_t=-5.0, n=22)
e.set_states(states(a=(20.0, 55.0), out=(-5.0, 80.0)))
e.run('run')
check('surface 11 h: no note', e.notes() == [])

e = Env('no-outdoor')
e.seed({'Room A': (20.0, 55.0)}, out_t=None, n=30)
e.set_states(states(a=(20.0, 55.0), out=('unavailable', 'unavailable')))
rc, out, err = e.run('run')
j = json.loads(e.run('status', '--json')[1])
check('outdoor sensor missing: no surface flag, no crash', rc == 0 and e.notes() == [], (out, err))
check('outdoor sensor missing: wall columns empty', j['rooms'][0]['surface_rh'] is None and j['outdoor']['t'] is None)

# --- missing sensors inside a streak
e = Env('gap-ok')
e.seed(lambda i: {'Room A': (21.0, None if i == 20 else 70.0)})
e.set_states(states(a=(21.0, 70.0)))
e.run('run')
check('one missing sample inside 24 h: still flags', len(e.notes()) == 1)
e = Env('gap-break')
e.seed(lambda i: {'Room A': (21.0, None if 20 <= i < 28 else 70.0)})
e.set_states(states(a=(21.0, 70.0)))
e.run('run')
check('4 h without data inside the run: no flag', e.notes() == [])
e = Env('stale')
e.seed({'Room A': (21.0, 70.0)})
e.set_states(states(a=(21.0, 70.0)))
e.run('run')
e.clear_inbox()
e.set_states(states(a=(21.0, 'unavailable')))
for k in range(1, 9):
    e.run('run', now=NOW + k * 1800)
check('sensor gone 4 h: flag kept (no recovery), no note', 'Room A' in e.state()['alerts'] and e.notes() == [])
check('status shows the kept flag', '⚠️' in e.run('status', now=NOW + 9 * 1800)[1])

# --- one note per run, outdoor never flagged
e = Env('multi')
e.seed({'Room A': (21.0, 70.0), 'Room B': (21.0, 72.0)})
e.set_states(states(a=(21.0, 70.0), b=(21.0, 72.0), out=(9.0, 99.0)))
e.run('run')
txt = e.note_text()
check('two rooms crossing in one run: ONE note with both', len(e.notes()) == 1 and '## Room A' in txt
      and '## Room B' in txt, e.notes())
check('outdoor reference never flagged', 'Terrace' not in e.state()['alerts'] and '## Terrace' not in txt)

# --- trip mode
day = dt.datetime.fromtimestamp(NOW, dt.timezone.utc).date()
trips = {'_comment': 'test', 'trips': {'t1': {'name': 'Island trip', 'tz': 'UTC',
                                              'from': (day - dt.timedelta(days=3)).isoformat(),
                                              'to': (day + dt.timedelta(days=10)).isoformat()}}}
e = Env('away')
json.dump(trips, open(e.trips, 'w'))
check('trip: away from trips.json', 'Island trip' in e.run('trip')[1])
e.seed({'Room A': (21.0, 70.0)})
e.set_states(states(a=(21.0, 70.0)))
e.run('run')
txt = e.note_text()
check('away: crossing note says AWAY + 48 h rule', len(e.notes()) == 1 and 'AWAY (Island trip' in txt
      and 'still flagged at 48 h' in txt, txt)
e.clear_inbox()
e.seed({'Room A': (21.0, 70.0)}, n=96, now=NOW + 86400)
e.run('run', now=NOW + 86400)
txt = e.note_text()
check('away: 48 h -> ONE escalation note', len(e.notes()) == 1 and 'away48' in e.notes()[0]
      and 'tell him about' in txt and 'for 48 h' in txt, (e.notes(), txt))
e.clear_inbox()
e.run('run', now=NOW + 86400 + 1800)
check('away: escalation only once', e.notes() == [])

e = Env('away-direct')
json.dump(trips, open(e.trips, 'w'))
e.seed({'Room A': (21.0, 70.0)}, n=100)
e.set_states(states(a=(21.0, 70.0)))
e.run('run')
check('away: new + 48 h in one run -> only the escalation note', len(e.notes()) == 1 and 'away48' in e.notes()[0],
      e.notes())
e = Env('home-48')
e.seed({'Room A': (21.0, 70.0)}, n=100)
e.set_states(states(a=(21.0, 70.0)))
e.run('run')
check('at home: no escalation note at 48 h', len(e.notes()) == 1 and 'away48' not in e.notes()[0])

e = Env('trip-past')
json.dump({'trips': {'t': {'name': 'old', 'from': '2020-01-01', 'to': '2020-01-05'}}}, open(e.trips, 'w'))
check('trip: past trip -> at home', 'at home' in e.run('trip')[1])
e = Env('cal')
os.makedirs(e.cal)
ev = lambda a, b, away=True: {'id': 'x', 'start': a.isoformat(), 'end': b.isoformat(), 'all_day': True, 'title': 't',
                              'flags': {'travel': None, 'busy_evening': False, 'away_overnight': away}}
json.dump({'events': [ev(day - dt.timedelta(days=1), day + dt.timedelta(days=4))]}, open(os.path.join(e.cal, 'events.json'), 'w'))
j = json.loads(e.run('trip', '--json')[1])
check('trip: calendar all-day away 5 days -> away', j['away'] and j['trip']['source'] == 'calendar', j)
json.dump({'events': [ev(day, day + dt.timedelta(days=2)), ev(day - dt.timedelta(days=1), day + dt.timedelta(days=5), False)]},
          open(os.path.join(e.cal, 'events.json'), 'w'))
check('trip: 2-day or not-away events -> at home', not json.loads(e.run('trip', '--json')[1])['away'])
tm = lambda a, b: {'id': 'y', 'start': a.isoformat(), 'end': b.isoformat(), 'all_day': False, 'title': 't',
                   'flags': {'travel': None, 'busy_evening': True, 'away_overnight': True}}
eve = dt.datetime.combine(day - dt.timedelta(days=1), dt.time(20), dt.timezone.utc)
json.dump({'events': [tm(eve, eve + dt.timedelta(hours=14)), ev(day, day + dt.timedelta(days=2))]},
          open(os.path.join(e.cal, 'events.json'), 'w'))
j = json.loads(e.run('trip', '--json')[1])
check('trip: overnight timed event + 2-day all-day = 3 nights in a row -> away (core.away)',
      j['away'] and (j['trip']['from'], j['trip']['to']) == ((day - dt.timedelta(days=1)).isoformat(),
                                                             (day + dt.timedelta(days=1)).isoformat()), j)
json.dump({'events': [tm(eve - dt.timedelta(days=1), eve - dt.timedelta(hours=10)), ev(day, day + dt.timedelta(days=2))]},
          open(os.path.join(e.cal, 'events.json'), 'w'))
check('trip: a gap night breaks the run -> at home', not json.loads(e.run('trip', '--json')[1])['away'])

# --- retention, HA failure, dry run
e = Env('retention')
e.seed({}, n=3, step=8 * 86400 // 3)
e.run('run')
sm = e.state()['samples']
check('retention: samples older than 7 days dropped', len(sm) == 3 and min(x['ts'] for x in sm) > NOW - 7 * 86400, sm)
e = Env('ha-down')
e.seed({'Room A': (21.0, 70.0)})
before = open(os.path.join(e.state_dir, 'state.json')).read()
rc, out, err = e.run('run', ha_exit=1)
check('HA unreadable: exit 1, state untouched, no note', rc == 1 and e.notes() == [] and
      open(os.path.join(e.state_dir, 'state.json')).read() == before, (rc, err))
rc, out, err = e.run('status', ha_exit=1)
check('HA unreadable: status falls back to the last sample', rc == 0 and 'last sample' in out, (out, err))
e = Env('dry')
e.seed({'Room A': (21.0, 70.0)})
e.set_states(states(a=(21.0, 70.0)))
before = open(os.path.join(e.state_dir, 'state.json')).read()
rc, out, err = e.run('run', '--dry-run')
check('dry run: reports, writes nothing', rc == 0 and '(dry run) new: Room A' in out and e.notes() == [] and
      open(os.path.join(e.state_dir, 'state.json')).read() == before, out)

# --- /home line
e = Env('line')
e.seed({}, n=4)
e.run('run')
rc, out, err = e.run('line')
check('line: ok summary', rc == 0 and out.startswith('💧 Damp ok · RH 55–56 %') and 'walls ≤' in out
      and out.count('\n') == 1, out)
check('line: stale state -> nothing', e.run('line', now=NOW + 3 * 3600)[1] == '')
check('line: no config -> nothing, exit 0', Env('line-nocfg', cfg=False).run('line')[:2] == (0, ''))
e = Env('line-bad')
e.seed({'Room A': (21.0, 70.0)}, out_t=21.0)
e.set_states(states(a=(21.0, 70.0), out=(21.0, 60.0)))
e.run('run')
out = e.run('line')[1]
check('line: flagged room, one line', out.startswith('💧 ⚠️ Room A humid 24 h (RH 70 %)') and out.count('\n') == 1, out)

shutil.rmtree(ROOT, ignore_errors=True)
print(f'damp-watch tests: {PASS} passed, {FAIL} failed')
sys.exit(1 if FAIL else 0)
